"""Save each send attempt and validate changes to its recorded state."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Collection, Iterable, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility
    fcntl = None

from ..errors import AmbiguousSendError, FenceConflictError, IntegrityError
from ..gaio_evidence import (
    canonical_gaio_identity_binding,
    canonical_gaio_reconciliation_evidence,
    canonical_gaio_retry_authorization_evidence,
    canonical_gaio_terminal_evidence,
)
from ..planning.hashing import text_sha256
from .attempt_changes import AttemptChanges, ChangeTrackingUnavailable
from .filesystem import atomic_write_json
from .schemas import FENCE_SCHEMA_VERSION


MAX_GAIO_FENCE_RECORD_BYTES = 262_144


class SendAttemptState(str, Enum):
    PREPARING = "preparing"
    PREPARED = "prepared"
    SEND_STARTED = "send_started"
    RESPONSE_CAPTURED = "response_captured"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    PRE_SEND_FAILED = "pre_send_failed"
    CONFIRMED_NO_RESPONSE = "confirmed_no_response"


@dataclass(frozen=True)
class SendAttempt:
    schema_version: str
    fence_id: str
    observation_id: str
    action: str
    state: SendAttemptState
    prompt_sha256: str
    identity: Mapping[str, object]
    prepared_at: str
    input_verified_at: Optional[str] = None
    send_started_at: Optional[str] = None
    response_captured_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    response_reference: Optional[str] = None
    response_sha256: Optional[str] = None
    ack_identity: Optional[Mapping[str, object]] = None
    validation_status: Optional[str] = None
    detail: Optional[str] = None
    pre_send_failure: Optional[Mapping[str, Any]] = None
    logical_turn_id: Optional[str] = None
    attempt_ordinal: Optional[int] = None
    observation_attempt_ordinal: Optional[int] = None
    response_baseline: Optional[Mapping[str, Any]] = None
    failure_classification: Optional[str] = None
    reconciliation_evidence: Optional[Mapping[str, Any]] = None
    reconciliation_identity: Optional[Mapping[str, object]] = None
    reconciled_at: Optional[str] = None
    retry_decision: Optional[str] = None
    retried_from: Optional[str] = None
    retried_by: Optional[str] = None
    native_retry_available: Optional[bool] = None
    native_retry: Optional[bool] = None
    response_evidence: Optional[Mapping[str, Any]] = None
    recovered_from: Optional[str] = None

    def __post_init__(self) -> None:
        # Keep legacy serialized fields intact while interpreting identity once.
        # Derived attributes are deliberately not dataclass fields: reading an
        # old attempt must not rewrite its durable schema or hash contract.
        logical_id, marker, suffix = self.fence_id.rpartition(":attempt-")
        try:
            legacy_ordinal = max(0, int(suffix)) if marker else 0
        except ValueError:
            legacy_ordinal = 0
        object.__setattr__(
            self, "_normalized_attempt_ordinal",
            self.attempt_ordinal or legacy_ordinal or 1,
        )
        object.__setattr__(
            self, "_normalized_logical_turn_id",
            self.logical_turn_id
            or (logical_id if legacy_ordinal > 0 else self.fence_id),
        )
        object.__setattr__(
            self,
            "identity",
            _canonical_attempt_identity(self.action, self.identity),
        )
        for field_name in ("ack_identity", "reconciliation_identity"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _canonical_attempt_identity(self.action, value),
                )
        reconciliation: Optional[dict[str, object]] = None
        if (
            self.action == "google_search"
            and self.reconciliation_evidence is not None
        ):
            try:
                reconciliation = canonical_gaio_reconciliation_evidence(
                    self.reconciliation_evidence
                )
            except ValueError as exc:
                raise IntegrityError(
                    f"invalid Google reconciliation evidence: {exc}"
                ) from exc
            object.__setattr__(
                self,
                "reconciliation_evidence",
                reconciliation,
            )
        if (
            self.action == "google_search"
            and self.retry_decision == "retry_authorized"
        ):
            if self.state is not SendAttemptState.CONFIRMED_NO_RESPONSE:
                raise IntegrityError(
                    "Google retry authorization requires a confirmed "
                    "no-response fence"
                )
            if reconciliation is None:
                raise IntegrityError(
                    "Google retry authorization lacks reconciliation evidence"
                )
            if not isinstance(self.response_baseline, Mapping):
                raise IntegrityError(
                    "Google retry authorization lacks a response baseline"
                )
            terminal_identity = (
                self.reconciliation_identity
                or self.ack_identity
                or self.identity
            )
            for key in (
                "profile_id",
                "chrome_directory",
                "browser_context_reference",
                "tab_reference",
                "browser_target_reference",
            ):
                if terminal_identity.get(key) != self.identity.get(key):
                    raise IntegrityError(
                        "Google retry authorization changed the prepared "
                        f"browser target field {key}"
                    )
            try:
                retry_evidence = canonical_gaio_retry_authorization_evidence(
                    reconciliation,
                    expected_prompt_sha256=self.prompt_sha256,
                    expected_identity=terminal_identity,
                    expected_response_baseline=self.response_baseline,
                )
            except ValueError as exc:
                raise IntegrityError(
                    f"invalid Google retry authorization evidence: {exc}"
                ) from exc
            object.__setattr__(
                self,
                "reconciliation_evidence",
                retry_evidence,
            )
            checks = retry_evidence["checks"]
            assert isinstance(checks, list)
            terminal_failure = str(
                checks[-1].get("failure_classification") or ""
            )
            if self.failure_classification != terminal_failure:
                raise IntegrityError(
                    "Google retry authorization failure classification "
                    "does not match the fence"
                )
        if self.action == "google_search" and self.state in {
            SendAttemptState.RESPONSE_CAPTURED,
            SendAttemptState.ACKNOWLEDGED,
            SendAttemptState.REJECTED,
        }:
            evidence = _canonical_google_response_evidence(self)
            object.__setattr__(self, "response_evidence", evidence)
        _check_google_record_size(self)

    @property
    def normalized_attempt_ordinal(self) -> int:
        return self._normalized_attempt_ordinal

    @property
    def normalized_logical_turn_id(self) -> str:
        return self._normalized_logical_turn_id

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SendAttempt":
        _check_google_attempt_size(value)
        return cls(
            schema_version=str(value["schema_version"]),
            fence_id=str(value["fence_id"]),
            observation_id=str(value["observation_id"]),
            action=str(value["action"]),
            state=SendAttemptState(str(value["state"])),
            prompt_sha256=str(value["prompt_sha256"]),
            identity=_canonical_attempt_identity(
                str(value["action"]),
                dict(value.get("identity") or {}),
            ),
            prepared_at=str(value["prepared_at"]),
            input_verified_at=_optional(value.get("input_verified_at")),
            send_started_at=_optional(value.get("send_started_at")),
            response_captured_at=_optional(value.get("response_captured_at")),
            acknowledged_at=_optional(value.get("acknowledged_at")),
            response_reference=_optional(value.get("response_reference")),
            response_sha256=_optional(value.get("response_sha256")),
            ack_identity=(
                _canonical_attempt_identity(
                    str(value["action"]),
                    dict(value.get("ack_identity") or {}),
                )
                if value.get("ack_identity")
                else None
            ),
            validation_status=_optional(value.get("validation_status")),
            detail=_optional(value.get("detail")),
            recovered_from=_optional(value.get("recovered_from")),
            pre_send_failure=(
                {
                    str(key): item
                    for key, item in dict(
                        value.get("pre_send_failure") or {}
                    ).items()
                }
                or None
            ),
            logical_turn_id=_optional(value.get("logical_turn_id")),
            attempt_ordinal=_optional_positive_int(
                value.get("attempt_ordinal"),
                field="attempt_ordinal",
            ),
            observation_attempt_ordinal=_optional_positive_int(
                value.get("observation_attempt_ordinal"),
                field="observation_attempt_ordinal",
            ),
            response_baseline=_optional_mapping(
                value.get("response_baseline"),
                field="response_baseline",
            ),
            response_evidence=_optional_mapping(
                value.get("response_evidence"),
                field="response_evidence",
            ),
            failure_classification=_optional(
                value.get("failure_classification")
            ),
            reconciliation_evidence=_optional_mapping(
                value.get("reconciliation_evidence"),
                field="reconciliation_evidence",
            ),
            reconciliation_identity=(
                _canonical_attempt_identity(
                    str(value["action"]),
                    dict(value.get("reconciliation_identity") or {}),
                )
                if value.get("reconciliation_identity")
                else None
            ),
            reconciled_at=_optional(value.get("reconciled_at")),
            retry_decision=_optional(value.get("retry_decision")),
            retried_from=_optional(value.get("retried_from")),
            retried_by=_optional(value.get("retried_by")),
            native_retry_available=_optional_bool(
                value.get("native_retry_available"),
                field="native_retry_available",
            ),
            native_retry=_optional_bool(
                value.get("native_retry"),
                field="native_retry",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        terminal_evidence = None
        if self.action == "google_search" and self.state in {
            SendAttemptState.RESPONSE_CAPTURED,
            SendAttemptState.ACKNOWLEDGED,
            SendAttemptState.REJECTED,
        }:
            terminal_evidence = _canonical_google_response_evidence(self)
        value = _send_attempt_mapping(self)
        if terminal_evidence is not None:
            value["response_evidence"] = terminal_evidence
        _check_google_attempt_size(value)
        return value


def _send_attempt_mapping(record: SendAttempt) -> dict[str, Any]:
    """Serialize without calling ``to_mapping`` from construction checks."""

    value = asdict(record)
    value["state"] = record.state.value
    value["identity"] = dict(record.identity)
    if record.recovered_from is None:
        value.pop("recovered_from", None)
    return value


def _durable_pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Match ``atomic_write_json``'s durable non-canonical encoding exactly."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _check_google_attempt_size(value: Mapping[str, Any]) -> None:
    if str(value.get("action") or "") != "google_search":
        return
    try:
        encoded = _durable_pretty_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise IntegrityError(
            "Google fence cannot be encoded as durable JSON"
        ) from exc
    if len(encoded) > MAX_GAIO_FENCE_RECORD_BYTES:
        raise IntegrityError(
            "Google fence exceeds the durable evidence byte bound"
        )


def _check_google_record_size(record: SendAttempt) -> None:
    _check_google_attempt_size(_send_attempt_mapping(record))


def _canonical_google_response_evidence(
    record: SendAttempt,
) -> dict[str, object]:
    response_reference = str(record.response_reference or "").strip()
    relative = Path(response_reference)
    if (
        not response_reference
        or relative == Path(".")
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        raise IntegrityError(
            "Google response fence lacks a safe durable response reference"
        )
    response_sha256 = str(record.response_sha256 or "").casefold()
    if (
        len(response_sha256) != 64
        or any(character not in "0123456789abcdef" for character in response_sha256)
    ):
        raise IntegrityError(
            "Google response fence lacks a valid response SHA-256"
        )
    if not isinstance(record.response_evidence, Mapping):
        raise IntegrityError(
            "Google response fence lacks terminal capture evidence"
        )
    capture_state = str(
        record.response_evidence.get("capture_state") or ""
    )
    if capture_state not in {"shown", "not_shown"}:
        raise IntegrityError("Google response fence has an invalid state")
    if record.validation_status is not None:
        expected_validation = {
            "shown": "collected",
            "not_shown": "not_shown",
        }[capture_state]
        if (
            record.validation_status != "rejected"
            and record.validation_status != expected_validation
        ):
            raise IntegrityError(
                "Google fence validation status contradicts capture evidence"
            )
    terminal_identity = (
        record.ack_identity or record.reconciliation_identity
    )
    try:
        evidence = canonical_gaio_terminal_evidence(
            record.response_evidence,
            expected_state=capture_state,
            expected_prompt_sha256=record.prompt_sha256,
            expected_identity=terminal_identity,
            expected_response_sha256=(
                response_sha256
                if capture_state == "shown"
                else None
            ),
        )
    except ValueError as exc:
        raise IntegrityError(
            f"invalid Google fence terminal evidence: {exc}"
        ) from exc
    evidence_identity = evidence["browser_target_identity_binding"]
    for key in (
        "profile_id",
        "chrome_directory",
        "browser_context_reference",
        "tab_reference",
        "browser_target_reference",
    ):
        if (
            str(record.identity.get(key) or "")
            != str(evidence_identity.get(key) or "")
        ):
            raise IntegrityError(
                "Google response evidence changed the prepared browser target"
            )
    if isinstance(record.reconciliation_evidence, Mapping):
        nested = record.reconciliation_evidence.get("response_evidence")
        if nested is not None:
            try:
                nested_evidence = canonical_gaio_terminal_evidence(
                    nested,
                    expected_state=capture_state,
                    expected_prompt_sha256=record.prompt_sha256,
                    expected_identity=terminal_identity,
                    expected_response_sha256=(
                        response_sha256
                        if capture_state == "shown"
                        else None
                    ),
                )
            except ValueError as exc:
                raise IntegrityError(
                    "invalid reconciled Google terminal evidence"
                ) from exc
            if nested_evidence != evidence:
                raise IntegrityError(
                    "reconciled Google evidence differs from the fence"
                )
    if capture_state == "not_shown":
        canonical_payload = (
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if hashlib.sha256(canonical_payload).hexdigest() != response_sha256:
            raise IntegrityError(
                "Google NOT_SHOWN fence response hash differs from its "
                "canonical evidence"
            )
    return evidence


def _optional(value: object) -> Optional[str]:
    text = str(value or "")
    return text or None


def _optional_positive_int(
    value: object,
    *,
    field: str,
) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_bool(value: object, *, field: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _optional_mapping(
    value: object,
    *,
    field: str,
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
    }


def _canonical_identity(
    identity: Mapping[str, object],
) -> dict[str, object]:
    return {
        str(key): str(value)
        for key, value in identity.items()
        if str(value)
    }


def _canonical_attempt_identity(
    action: str,
    identity: Mapping[str, object],
) -> dict[str, object]:
    if str(action) != "google_search":
        return _canonical_identity(identity)
    try:
        return canonical_gaio_identity_binding(identity)
    except ValueError as exc:
        raise IntegrityError(
            f"invalid Google fence browser identity: {exc}"
        ) from exc


def _verify_google_response_artifact(
    record: SendAttempt,
    *,
    run_root: Path,
    read_bytes: Optional[Callable[[Path], bytes]] = None,
) -> None:
    """Verify the immutable blob before a Google response send attempt advances."""

    if record.action != "google_search":
        return
    # Construction already validates the relative reference and declared hash.
    path = run_root / str(record.response_reference)
    try:
        payload = read_bytes(path) if read_bytes is not None else path.read_bytes()
    except OSError as exc:
        raise IntegrityError(
            f"Google response artifact is not durable: {path}"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != record.response_sha256:
        raise IntegrityError(
            f"Google response artifact hash mismatch: {path}"
        )


def _required_reconciliation_evidence(
    evidence: Mapping[str, object],
) -> dict[str, Any]:
    result = _optional_mapping(
        evidence,
        field="reconciliation_evidence",
    )
    if not result:
        raise ValueError("reconciliation_evidence must not be empty")
    return result


def _retry_is_exhausted(decision: object) -> bool:
    value = str(decision or "").strip().casefold()
    return "exhaust" in value or value in {
        "retry_disabled",
        "do_not_retry",
    }


def attempt_allows_retry(record: SendAttempt) -> bool:
    """Whether this confirmed attempt still owns an unused retry decision."""

    return (
        attempt_resume_decision(record) == "retry_confirmed"
        and record.retried_by is None
    )


def attempt_resume_decision(current: Optional[SendAttempt]) -> str:
    """The durable recovery interpretation shared by compatibility readers."""

    if current is None:
        return "prepare"
    return {
        SendAttemptState.PREPARING: "prepare_input",
        SendAttemptState.PREPARED: "send",
        SendAttemptState.PRE_SEND_FAILED: "retry_prepare",
        SendAttemptState.SEND_STARTED: "refuse_ambiguous",
        SendAttemptState.RESPONSE_CAPTURED: "refuse_ambiguous",
        SendAttemptState.ACKNOWLEDGED: "complete",
        SendAttemptState.REJECTED: "rejected",
        SendAttemptState.CONFIRMED_NO_RESPONSE: (
            "retry_confirmed"
            if current.retry_decision == "retry_authorized"
            else "confirmed_exhausted"
            if _retry_is_exhausted(current.retry_decision)
            else "refuse_ambiguous"
        ),
    }[current.state]


def count_attempt_milestones(records: Iterable[SendAttempt]) -> dict[str, int]:
    """Count durable milestones consistently for execution and readout."""

    counts = dict.fromkeys(
        ("prepared", "send_started", "responses", "acknowledged",
         "retry_count", "exhausted_retry_count"), 0,
    )
    for record in records:
        if record.recovered_from is not None:
            continue
        counts["prepared"] += int(record.input_verified_at is not None)
        counts["send_started"] += int(record.send_started_at is not None)
        counts["responses"] += int(record.response_reference is not None)
        counts["acknowledged"] += int(record.state in {
            SendAttemptState.ACKNOWLEDGED, SendAttemptState.REJECTED,
        })
        counts["retry_count"] += int(
            record.send_started_at is not None
            and (record.retried_from is not None
                 or record.normalized_attempt_ordinal > 1)
        )
        counts["exhausted_retry_count"] += int(
            record.retry_decision == "retry_exhausted"
        )
    return counts


def _attempt_platform(
    record: SendAttempt, observation_platforms: Mapping[str, str],
) -> str:
    if record.action in {"identification_source", "identification_judge"}:
        return str(record.identity.get("platform") or "")
    if record.action == "google_search":
        return "google_ai_overview"
    if record.action in {"gaio_gemini_extractor", "gemini_parse"}:
        return "gemini"
    return observation_platforms.get(record.observation_id, "")


class AttemptSnapshot:
    """Validated, indexed records for one synchronous decision.

    A snapshot never authorizes a mutation. Obtain a new one after an await;
    mutation methods independently reload the relevant attempt under its lock.
    """

    def __init__(
        self, records: Iterable[SendAttempt], *,
        observation_platforms: Optional[Mapping[str, str]] = None,
    ):
        self.records = tuple(records)
        logical: dict[str, list[SendAttempt]] = {}
        observations: dict[str, list[SendAttempt]] = {}
        resources: dict[tuple[str, str], list[SendAttempt]] = {}
        for record in self.records:
            logical.setdefault(record.normalized_logical_turn_id, []).append(record)
            observations.setdefault(record.observation_id, []).append(record)
            resource = (
                str(record.identity.get("profile_id") or ""),
                _attempt_platform(record, observation_platforms or {}),
            )
            resources.setdefault(resource, []).append(record)
        self.by_logical_turn = MappingProxyType({
            key: tuple(sorted(values, key=lambda record: (
                record.normalized_attempt_ordinal, record.prepared_at, record.fence_id,
            )))
            for key, values in logical.items()
        })
        self.by_observation = MappingProxyType({
            key: tuple(values) for key, values in observations.items()
        })
        self.by_resource = MappingProxyType({
            key: tuple(values) for key, values in resources.items()
        })

    def attempts_for(self, logical_turn_id: str) -> tuple[SendAttempt, ...]:
        return self.by_logical_turn.get(logical_turn_id, ())

    def for_observation(self, observation_id: str) -> tuple[SendAttempt, ...]:
        return self.by_observation.get(observation_id, ())

    def for_resource(self, profile_id: str, platform: str) -> tuple[SendAttempt, ...]:
        return self.by_resource.get((profile_id, platform), ())


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_send_attempt(path: Path) -> SendAttempt:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise TypeError("durable fence must contain an object")
    if (
        str(value.get("action") or "") == "google_search"
        and len(payload) > MAX_GAIO_FENCE_RECORD_BYTES
    ):
        raise IntegrityError(
            "Google fence exceeds the durable evidence byte bound"
        )
    return SendAttempt.from_mapping(value)


class SendAttemptStore:
    """Durable attempts whose sent send attempts never rewind to a pre-send state."""

    def __init__(
        self, root: Path | str, *,
        verified_bytes_reader: Optional[Callable[[Path], bytes]] = None,
        watch_changes: bool = True,
    ):
        self.root = Path(root)
        # A caller may share verified bytes only for a bounded read traversal.
        # Mutation helpers never use this reader, even on this store instance.
        self._verified_bytes_reader = verified_bytes_reader
        self.record_filter = None
        self.ownership_guard = None
        # Atomic attempt commits replace their inode. Every lookup checks the
        # directory and each file version, including external-process changes;
        # only unchanged parsed metadata is reused. Evidence is checked below
        # for every selected snapshot, even when its attempt file is unchanged.
        self._record_versions: dict[Path, tuple[tuple[int, ...], SendAttempt]] = {}
        self._indexed_paths: dict[str, dict[object, set[Path]]] = {
            "observation": {}, "logical_turn": {}, "profile": {}, "state": {},
        }
        self._watch_changes = watch_changes
        self._changes: Optional[AttemptChanges] = None

    def close(self) -> None:
        """Release optional native change notifications; future reads scan fully."""

        self._watch_changes = False
        if self._changes is not None:
            self._changes.close()
            self._changes = None

    def __enter__(self) -> "SendAttemptStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        changes = getattr(self, "_changes", None)
        if changes is not None:
            changes.close()

    def _clear_metadata(self) -> None:
        self._record_versions.clear()
        for index in self._indexed_paths.values():
            index.clear()

    def _replace_indexed_record(
        self, path: Path, record: Optional[SendAttempt],
    ) -> None:
        previous = self._record_versions.get(path)
        for value, remove in ((previous[1] if previous else None, True), (record, False)):
            if value is None:
                continue
            keys = {
                "observation": value.observation_id,
                "logical_turn": value.normalized_logical_turn_id,
                "profile": str(value.identity.get("profile_id") or ""),
                "state": value.state,
            }
            for field, key in keys.items():
                index = self._indexed_paths[field]
                if remove:
                    owned = index.get(key)
                    if owned is not None:
                        owned.discard(path)
                        if not owned:
                            del index[key]
                else:
                    index.setdefault(key, set()).add(path)

    def _refresh_metadata_unlocked(self, *, force_full: bool = False) -> None:
        changed: Optional[set[Path]] = None
        if self._watch_changes:
            try:
                if self._changes is None:
                    self._changes = AttemptChanges(self.root)
                changed = self._changes.changed_paths()
            except FileNotFoundError:
                # A new run may query its empty store before its first fence
                # creates the directory. Retry registration once it exists.
                if self._changes is not None:
                    self.close()
                changed = None
            except (OSError, ValueError, ChangeTrackingUnavailable):
                # Includes unsupported hosts, descriptor cap/failure, root
                # replacement and uncertain events. Never keep a partial watch.
                self.close()
                changed = None
        if force_full or changed is None:
            names = set(self.root.glob("*.json")) if self.root.is_dir() else set()
            candidates = names | (self._record_versions.keys() - names)
        else:
            candidates = changed
        try:
            for path in sorted(candidates):
                try:
                    watched_identity = self._changes.watch(path) if self._changes else None
                    metadata = path.stat()
                except FileNotFoundError:
                    self._replace_indexed_record(path, None)
                    self._record_versions.pop(path, None)
                    if self._changes:
                        self._changes.forget(path)
                    continue
                if watched_identity is not None and watched_identity != (metadata.st_dev, metadata.st_ino):
                    raise ChangeTrackingUnavailable("attempt replaced while registering its vnode")
                version = (metadata.st_dev, metadata.st_ino, metadata.st_size,
                           metadata.st_mtime_ns, metadata.st_ctime_ns)
                cached = self._record_versions.get(path)
                event_reported_change = changed is not None and path in changed
                if cached is not None and cached[0] == version and not event_reported_change:
                    continue
                record = _read_send_attempt(path)
                if self.path(record.fence_id) != path:
                    raise IntegrityError(f"durable fence path collision for {record.fence_id}")
                self._replace_indexed_record(path, record)
                self._record_versions[path] = (version, record)
        except ChangeTrackingUnavailable:
            self.close()
            self._refresh_metadata_unlocked(force_full=True)
        except OSError:
            if self._changes is None:
                raise
            self.close()
            self._clear_metadata()
            self._refresh_metadata_unlocked(force_full=True)
        except (KeyError, TypeError, ValueError, IntegrityError):
            # Draining an event is not permission to forget a failed refresh.
            # A corrupt cold file or changed cached record must fail again on
            # the next read, even if no writer emits another notification.
            self.close()
            self._clear_metadata()
            raise

    def _selected_paths(
        self, *, observation_ids: Optional[Collection[str]],
        logical_turn_ids: Optional[Collection[str]], profile_id: Optional[str],
        states: Optional[Collection[SendAttemptState]],
    ) -> set[Path]:
        selected: Optional[set[Path]] = None
        for field, values in (
            ("observation", observation_ids), ("logical_turn", logical_turn_ids),
            ("profile", None if profile_id is None else (profile_id,)),
            ("state", states),
        ):
            if values is None:
                continue
            index = self._indexed_paths[field]
            matches = set().union(*(index.get(value, set()) for value in values))
            selected = matches if selected is None else selected & matches
        return set(self._record_versions) if selected is None else selected

    def path(self, fence_id: str) -> Path:
        safe = text_sha256(fence_id)[:24]
        return self.root / f"{safe}.json"

    def prepare(
        self,
        *,
        fence_id: str,
        observation_id: str,
        action: str,
        prompt_sha256: str,
        identity: Mapping[str, object],
        logical_turn_id: Optional[str] = None,
        attempt_ordinal: Optional[int] = None,
        observation_attempt_ordinal: Optional[int] = None,
        retried_from: Optional[str] = None,
        native_retry: Optional[bool] = None,
    ) -> SendAttempt:
        if not all((fence_id, observation_id, action, prompt_sha256)):
            raise ValueError("fence identity, action, and prompt hash are required")
        logical_turn = _optional(logical_turn_id)
        ordinal = _optional_positive_int(
            attempt_ordinal,
            field="attempt_ordinal",
        )
        observation_ordinal = _optional_positive_int(
            observation_attempt_ordinal,
            field="observation_attempt_ordinal",
        )
        retry_parent = _optional(retried_from)
        retry_is_native = _optional_bool(
            native_retry,
            field="native_retry",
        )
        canonical_identity = _canonical_attempt_identity(action, identity)
        requested = SendAttempt(
            schema_version=FENCE_SCHEMA_VERSION,
            fence_id=fence_id,
            observation_id=observation_id,
            action=action,
            state=SendAttemptState.PREPARING,
            prompt_sha256=prompt_sha256,
            identity=canonical_identity,
            prepared_at=_timestamp(),
            logical_turn_id=logical_turn,
            attempt_ordinal=ordinal,
            observation_attempt_ordinal=observation_ordinal,
            retried_from=retry_parent,
            native_retry=retry_is_native,
        )
        with self._locked(logical_turn or fence_id):
            existing = self._read_without_lock(fence_id)
            if existing is None:
                self._assert_unique_attempt_unlocked(requested)
                atomic_write_json(
                    self.path(fence_id),
                    requested.to_mapping(),
                    exclusive=True,
                )
                return requested
            for field in (
                "fence_id",
                "observation_id",
                "action",
                "prompt_sha256",
                "identity",
                "logical_turn_id",
                "attempt_ordinal",
                "observation_attempt_ordinal",
                "retried_from",
                "native_retry",
            ):
                if getattr(existing, field) != getattr(requested, field):
                    raise IntegrityError(
                        f"durable fence {fence_id!r} conflicts on {field}"
                    )
            return existing

    def mark_prepared(
        self,
        fence_id: str,
        *,
        response_baseline: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Record that exact composer readback succeeded before any send."""

        baseline = _optional_mapping(
            response_baseline,
            field="response_baseline",
        )
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.state is SendAttemptState.PREPARED:
                if baseline is None or current.response_baseline == baseline:
                    return current
                if current.response_baseline is not None:
                    raise FenceConflictError(
                        f"{fence_id} already has a different response baseline"
                    )
                updated = replace(current, response_baseline=baseline)
                atomic_write_json(self.path(fence_id), updated.to_mapping())
                return updated
            if current.state is not SendAttemptState.PREPARING:
                raise FenceConflictError(
                    f"{fence_id} cannot verify input from {current.state.value}"
                )
            updated = replace(
                current,
                state=SendAttemptState.PREPARED,
                input_verified_at=_timestamp(),
                detail=None,
                response_baseline=baseline,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def load(self, fence_id: str) -> Optional[SendAttempt]:
        # Every publication replaces a complete file. Read-only inspection
        # must not create lock files; admissions revalidate under the writer lock.
        return self._read_without_lock(fence_id)

    def admit_send(
        self, fence_id: str, *,
        record_admission: Optional[Callable[[SendAttempt], None]] = None,
    ) -> SendAttempt:
        """Record send admission before input, without claiming website success.

        Recovery admission is authoritative if the detailed fence publication
        is interrupted. The callback itself must preserve that same boundary if
        it fails after publishing its durable effect.
        """
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.state is SendAttemptState.SEND_STARTED:
                raise AmbiguousSendError(
                    f"{fence_id} has a send-started fence without acknowledgement"
                )
            if current.state is not SendAttemptState.PREPARED:
                raise FenceConflictError(
                    f"{fence_id} cannot send from {current.state.value}"
                )
            if record_admission is not None:
                record_admission(current)
            try:
                updated = replace(
                    current,
                    state=SendAttemptState.SEND_STARTED,
                    send_started_at=_timestamp(),
                )
                atomic_write_json(self.path(fence_id), updated.to_mapping())
            except BaseException as exc:
                # Admission may already be durable even if this API call did
                # not return. Neither a failed write nor an admitted effect
                # proves that a fresh send is safe.
                if (record_admission is not None
                        or self._required_unlocked(fence_id).state is SendAttemptState.SEND_STARTED):
                    raise AmbiguousSendError(
                        f"{fence_id} was admitted before send fence publication failed"
                    ) from exc
                raise
            return updated

    def begin_send(self, fence_id: str) -> SendAttempt:
        """Compatibility for historical fence-only consumers."""
        return SendAttemptStore.admit_send(self, fence_id)

    def acknowledge(
        self,
        fence_id: str,
        *,
        validation_status: str = "accepted",
        detail: Optional[str] = None,
        identity: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.state is not SendAttemptState.RESPONSE_CAPTURED:
                raise FenceConflictError(
                    f"{fence_id} cannot acknowledge from {current.state.value}"
                )
            terminal = (
                SendAttemptState.REJECTED
                if validation_status == "rejected"
                else SendAttemptState.ACKNOWLEDGED
            )
            updated = replace(
                current,
                state=terminal,
                acknowledged_at=_timestamp(),
                ack_identity=_canonical_attempt_identity(
                    current.action,
                    identity or current.identity,
                ),
                validation_status=str(validation_status),
                detail=str(detail) if detail else None,
            )
            _verify_google_response_artifact(
                updated,
                run_root=self.root.parent,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def capture_response(
        self,
        fence_id: str,
        *,
        response_reference: str,
        response_sha256: str,
        response_evidence: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Bind already-durable response evidence before acknowledgement."""

        evidence = _optional_mapping(
            response_evidence,
            field="response_evidence",
        )
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.state is not SendAttemptState.SEND_STARTED:
                raise FenceConflictError(
                    f"{fence_id} cannot capture a response from {current.state.value}"
                )
            updated = replace(
                current,
                state=SendAttemptState.RESPONSE_CAPTURED,
                response_captured_at=_timestamp(),
                response_reference=str(response_reference),
                response_sha256=str(response_sha256),
                response_evidence=evidence,
            )
            _verify_google_response_artifact(
                updated,
                run_root=self.root.parent,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def capture_late_response(
        self,
        fence_id: str,
        *,
        response_reference: str,
        response_sha256: str,
        reconciliation_evidence: Mapping[str, object],
        response_evidence: Optional[Mapping[str, object]] = None,
        failure_classification: str = "late_response",
        detail: Optional[str] = None,
        native_retry_available: Optional[bool] = None,
        identity: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Atomically bind a response found during timeout reconciliation.

        The response bytes must already be immutable at ``response_reference``.
        This transition records why no resend occurred while moving the
        original sent attempt forward to ``RESPONSE_CAPTURED``.
        """

        evidence = _required_reconciliation_evidence(
            reconciliation_evidence
        )
        terminal_evidence = _optional_mapping(
            response_evidence,
            field="response_evidence",
        )
        classification = str(failure_classification or "").strip()
        if not classification:
            raise ValueError("failure_classification is required")
        native_available = _optional_bool(
            native_retry_available,
            field="native_retry_available",
        )
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.action == "google_search":
                evidence = canonical_gaio_reconciliation_evidence(evidence)
            reconciled_identity = (
                _canonical_attempt_identity(current.action, identity)
                if identity is not None
                else None
            )
            if current.state is SendAttemptState.RESPONSE_CAPTURED:
                expected = (
                    str(response_reference),
                    str(response_sha256),
                    evidence,
                    terminal_evidence,
                    classification,
                    "late_response_captured",
                    native_available,
                    reconciled_identity,
                )
                actual = (
                    current.response_reference,
                    current.response_sha256,
                    current.reconciliation_evidence,
                    current.response_evidence,
                    current.failure_classification,
                    current.retry_decision,
                    current.native_retry_available,
                    current.reconciliation_identity,
                )
                if actual == expected:
                    return current
                raise FenceConflictError(
                    f"{fence_id} already captured different response evidence"
                )
            if current.state is not SendAttemptState.SEND_STARTED:
                raise FenceConflictError(
                    f"{fence_id} cannot reconcile a late response from "
                    f"{current.state.value}"
                )
            updated = replace(
                current,
                state=SendAttemptState.RESPONSE_CAPTURED,
                response_captured_at=_timestamp(),
                response_reference=str(response_reference),
                response_sha256=str(response_sha256),
                response_evidence=terminal_evidence,
                failure_classification=classification,
                reconciliation_evidence=evidence,
                reconciled_at=_timestamp(),
                retry_decision="late_response_captured",
                native_retry_available=native_available,
                reconciliation_identity=reconciled_identity,
                detail=str(detail) if detail else current.detail,
            )
            _verify_google_response_artifact(
                updated,
                run_root=self.root.parent,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def mark_reconciliation_ambiguous(
        self,
        fence_id: str,
        *,
        failure_classification: str,
        reconciliation_evidence: Mapping[str, object],
        detail: Optional[str] = None,
        native_retry_available: Optional[bool] = None,
        identity: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Persist a fail-closed reconciliation without authorizing resend."""

        evidence = _required_reconciliation_evidence(
            reconciliation_evidence
        )
        classification = str(failure_classification or "").strip()
        if not classification:
            raise ValueError("failure_classification is required")
        native_available = _optional_bool(
            native_retry_available,
            field="native_retry_available",
        )
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.action == "google_search":
                evidence = canonical_gaio_reconciliation_evidence(evidence)
            reconciled_identity = (
                _canonical_attempt_identity(current.action, identity)
                if identity is not None
                else None
            )
            if (
                current.state is SendAttemptState.SEND_STARTED
                and current.retry_decision == "needs_attention"
            ):
                expected = (
                    evidence,
                    classification,
                    native_available,
                    reconciled_identity,
                    str(detail) if detail else current.detail,
                )
                actual = (
                    current.reconciliation_evidence,
                    current.failure_classification,
                    current.native_retry_available,
                    current.reconciliation_identity,
                    current.detail,
                )
                if actual == expected:
                    return current
                raise FenceConflictError(
                    f"{fence_id} already has a different reconciliation decision"
                )
            if current.state is not SendAttemptState.SEND_STARTED:
                raise FenceConflictError(
                    f"{fence_id} cannot record ambiguous reconciliation from "
                    f"{current.state.value}"
                )
            updated = replace(
                current,
                failure_classification=classification,
                reconciliation_evidence=evidence,
                reconciled_at=_timestamp(),
                retry_decision="needs_attention",
                native_retry_available=native_available,
                reconciliation_identity=reconciled_identity,
                detail=str(detail) if detail else current.detail,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def confirm_no_response(
        self,
        fence_id: str,
        *,
        failure_classification: str,
        reconciliation_evidence: Mapping[str, object],
        retry_decision: str = "retry_authorized",
        detail: Optional[str] = None,
        native_retry_available: Optional[bool] = None,
        identity: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Record positive terminal no-response evidence for one sent attempt."""

        evidence = _required_reconciliation_evidence(
            reconciliation_evidence
        )
        classification = str(failure_classification or "").strip()
        decision = str(retry_decision or "").strip()
        if not classification:
            raise ValueError("failure_classification is required")
        if not decision:
            raise ValueError("retry_decision is required")
        if decision not in {
            "retry_authorized",
            "retry_disabled",
            "retry_exhausted",
            "do_not_retry",
        }:
            raise ValueError(
                "retry_decision must be retry_authorized, retry_disabled, "
                "retry_exhausted, or do_not_retry"
            )
        native_available = _optional_bool(
            native_retry_available,
            field="native_retry_available",
        )
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.action == "google_search":
                evidence = canonical_gaio_reconciliation_evidence(evidence)
            reconciled_identity = (
                _canonical_attempt_identity(current.action, identity)
                if identity is not None
                else None
            )
            if current.state is SendAttemptState.CONFIRMED_NO_RESPONSE:
                expected = (
                    evidence,
                    classification,
                    decision,
                    native_available,
                    reconciled_identity,
                    str(detail) if detail else current.detail,
                )
                actual = (
                    current.reconciliation_evidence,
                    current.failure_classification,
                    current.retry_decision,
                    current.native_retry_available,
                    current.reconciliation_identity,
                    current.detail,
                )
                if actual == expected:
                    return current
                raise FenceConflictError(
                    f"{fence_id} already has a different confirmed-failure "
                    "decision"
                )
            if current.state is not SendAttemptState.SEND_STARTED:
                raise FenceConflictError(
                    f"{fence_id} cannot confirm no response from "
                    f"{current.state.value}"
                )
            updated = replace(
                current,
                state=SendAttemptState.CONFIRMED_NO_RESPONSE,
                failure_classification=classification,
                reconciliation_evidence=evidence,
                reconciled_at=_timestamp(),
                retry_decision=decision,
                native_retry_available=native_available,
                reconciliation_identity=reconciled_identity,
                detail=str(detail) if detail else current.detail,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def exhaust_authorized_retry(
        self,
        fence_id: str,
        *,
        detail: Optional[str] = None,
    ) -> SendAttempt:
        """Close an authorized decision when no clean successor is possible."""

        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if (
                current.state is SendAttemptState.CONFIRMED_NO_RESPONSE
                and current.retry_decision == "retry_exhausted"
            ):
                return current
            if current.state is not SendAttemptState.CONFIRMED_NO_RESPONSE:
                raise FenceConflictError(
                    f"{fence_id} cannot exhaust retry from "
                    f"{current.state.value}"
                )
            if current.retry_decision != "retry_authorized":
                raise FenceConflictError(
                    f"{fence_id} has no authorized retry to exhaust"
                )
            if current.retried_by is not None:
                raise FenceConflictError(
                    f"{fence_id} already has retry successor "
                    f"{current.retried_by}"
                )
            updated = replace(
                current,
                retry_decision="retry_exhausted",
                detail=str(detail) if detail else current.detail,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def fail_before_send(self, fence_id: str, detail: object) -> SendAttempt:
        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.state not in {SendAttemptState.PREPARING, SendAttemptState.PREPARED}:
                raise FenceConflictError(
                    f"{fence_id} cannot record a pre-send failure from "
                    f"{current.state.value}"
                )
            to_dict = getattr(detail, "to_dict", None)
            structured = (
                to_dict()
                if callable(to_dict)
                else dict(detail)
                if isinstance(detail, Mapping)
                else None
            )
            detail_text = (
                str(structured.get("detail") or "")
                if isinstance(structured, Mapping)
                else str(detail)
            )
            updated = replace(
                current,
                state=SendAttemptState.PRE_SEND_FAILED,
                detail=detail_text or str(detail),
                pre_send_failure=structured,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def retry_pre_send_failure(
        self,
        fence_id: str,
        *,
        identity: Optional[Mapping[str, object]] = None,
    ) -> SendAttempt:
        """Re-arm only a known no-send failure during explicit resume."""

        with self._locked(fence_id):
            current = self._required_unlocked(fence_id)
            if current.send_started_at is not None:
                raise FenceConflictError(
                    f"{fence_id} has durable send activity and cannot be re-armed"
                )
            if current.state not in {
                SendAttemptState.PREPARING,
                SendAttemptState.PREPARED,
                SendAttemptState.PRE_SEND_FAILED,
            }:
                raise FenceConflictError(
                    f"{fence_id} has no safely retryable pre-send state"
                )
            updated = replace(
                current,
                state=SendAttemptState.PREPARING,
                identity=(
                    _canonical_attempt_identity(current.action, identity)
                    if identity is not None
                    else current.identity
                ),
                input_verified_at=None,
                response_baseline=None,
                detail=None,
                pre_send_failure=None,
            )
            atomic_write_json(self.path(fence_id), updated.to_mapping())
            return updated

    def prepare_retry(
        self,
        *,
        confirmed_fence_id: str,
        successor_fence_id: str,
        identity: Mapping[str, object],
        action: Optional[str] = None,
        prompt_sha256: Optional[str] = None,
        logical_turn_id: Optional[str] = None,
        attempt_ordinal: Optional[int] = None,
        observation_attempt_ordinal: Optional[int] = None,
        native_retry: bool = False,
    ) -> SendAttempt:
        """Create and link the one unique successor to a confirmed attempt.

        The prior attempt remains terminal and retains all reconciliation
        evidence.  Repeated calls with the same exact successor are idempotent;
        a different successor is rejected.  The successor is written before
        the prior link, so a crash between the two writes leaves a recoverable
        orphan with ``retried_from`` rather than losing the failed attempt.
        """

        if not confirmed_fence_id or not successor_fence_id:
            raise ValueError("confirmed and successor fence IDs are required")
        if confirmed_fence_id == successor_fence_id:
            raise ValueError("a retry successor must have a new fence identity")
        first = self.load(confirmed_fence_id)
        if first is None:
            raise FenceConflictError(
                f"durable fence {confirmed_fence_id!r} is missing"
            )
        logical_turn = (
            _optional(logical_turn_id)
            or first.normalized_logical_turn_id
        )
        with self._locked(logical_turn):
            current = self._required_unlocked(confirmed_fence_id)
            if current.state is not SendAttemptState.CONFIRMED_NO_RESPONSE:
                raise FenceConflictError(
                    f"{confirmed_fence_id} cannot retry from "
                    f"{current.state.value}"
                )
            decision = str(current.retry_decision or "")
            if decision != "retry_authorized":
                raise FenceConflictError(
                    f"{confirmed_fence_id} does not authorize a retry"
                )
            if (
                current.retried_by is not None
                and current.retried_by != successor_fence_id
            ):
                raise FenceConflictError(
                    f"{confirmed_fence_id} is already linked to a different "
                    "retry successor"
                )
            if native_retry and current.native_retry_available is not True:
                raise FenceConflictError(
                    f"{confirmed_fence_id} lacks positive native-retry evidence"
                )

            ordinal = _optional_positive_int(
                attempt_ordinal,
                field="attempt_ordinal",
            )
            if ordinal is None:
                ordinal = current.normalized_attempt_ordinal + 1
            if ordinal != current.normalized_attempt_ordinal + 1:
                raise FenceConflictError(
                    "retry attempt ordinal must immediately follow the "
                    "confirmed attempt"
                )
            observation_ordinal = _optional_positive_int(
                observation_attempt_ordinal,
                field="observation_attempt_ordinal",
            )
            if observation_ordinal is None:
                observation_ordinal = (
                    current.observation_attempt_ordinal or 1
                )
            successor_action = str(action or current.action)
            successor = SendAttempt(
                schema_version=FENCE_SCHEMA_VERSION,
                fence_id=successor_fence_id,
                observation_id=current.observation_id,
                action=successor_action,
                state=SendAttemptState.PREPARING,
                prompt_sha256=str(
                    prompt_sha256 or current.prompt_sha256
                ),
                identity=_canonical_attempt_identity(
                    successor_action,
                    identity,
                ),
                prepared_at=_timestamp(),
                logical_turn_id=logical_turn,
                attempt_ordinal=ordinal,
                observation_attempt_ordinal=observation_ordinal,
                retried_from=confirmed_fence_id,
                native_retry=bool(native_retry),
            )
            if successor.prompt_sha256 != current.prompt_sha256:
                raise IntegrityError(
                    "retry successor prompt hash differs from the confirmed "
                    "logical turn"
                )

            existing = self._read_without_lock(successor_fence_id)
            if existing is None:
                self._assert_unique_attempt_unlocked(successor)
                atomic_write_json(
                    self.path(successor_fence_id),
                    successor.to_mapping(),
                    exclusive=True,
                )
                existing = successor
            else:
                for field in (
                    "fence_id",
                    "observation_id",
                    "action",
                    "prompt_sha256",
                    "identity",
                    "logical_turn_id",
                    "attempt_ordinal",
                    "observation_attempt_ordinal",
                    "retried_from",
                    "native_retry",
                ):
                    if getattr(existing, field) != getattr(successor, field):
                        raise IntegrityError(
                            f"durable retry {successor_fence_id!r} conflicts "
                            f"on {field}"
                        )

            if current.retried_by is None:
                linked = replace(
                    current,
                    retried_by=successor_fence_id,
                )
                atomic_write_json(
                    self.path(confirmed_fence_id),
                    linked.to_mapping(),
                )
            return existing

    # Explicit spelling used by measurement execution when it already has a durable
    # confirmed-failure decision.
    link_confirmed_retry = prepare_retry

    def iter_records(self) -> tuple[SendAttempt, ...]:
        return self._iter_records_unlocked(read_bytes=self._verified_bytes_reader)

    def resume_decision(self, fence_id: str) -> str:
        return attempt_resume_decision(self.load(fence_id))

    def attempts_for(self, logical_turn_id: str) -> tuple[SendAttempt, ...]:
        """Return freshly validated attempts for a normalized logical turn."""

        logical_turn = str(logical_turn_id or "").strip()
        if not logical_turn:
            raise ValueError("logical_turn_id is required")
        return self.snapshot(logical_turn_ids=(logical_turn,)).attempts_for(logical_turn)

    def snapshot(
        self,
        *,
        observation_ids: Optional[Collection[str]] = None,
        logical_turn_ids: Optional[Collection[str]] = None,
        profile_id: Optional[str] = None,
        platforms: Optional[Collection[str]] = None,
        observation_platforms: Optional[Mapping[str, str]] = None,
    ) -> AttemptSnapshot:
        """Refresh metadata versions, then verify only the selected evidence."""

        return AttemptSnapshot(self._iter_records_unlocked(
            observation_ids=observation_ids,
            logical_turn_ids=logical_turn_ids,
            profile_id=profile_id,
            platforms=platforms,
            observation_platforms=observation_platforms,
        ), observation_platforms=observation_platforms)

    def unresolved_snapshot(
        self, *, profile_id: str, platforms: Collection[str],
        observation_platforms: Mapping[str, str],
        observation_ids: Optional[Collection[str]] = None,
    ) -> AttemptSnapshot:
        """Read only sends that can block a resource pair's next operation.

        Completed-attempt metadata is still validated and refreshed. Its
        evidence is irrelevant to this predicate; captures that remain
        unresolved still receive the full evidence check before returning.
        """

        return AttemptSnapshot(self._iter_records_unlocked(
            profile_id=profile_id, platforms=platforms,
            observation_ids=observation_ids,
            observation_platforms=observation_platforms,
            states={SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED},
        ), observation_platforms=observation_platforms)

    def _iter_records_unlocked(
        self,
        *,
        observation_ids: Optional[Collection[str]] = None,
        logical_turn_ids: Optional[Collection[str]] = None,
        profile_id: Optional[str] = None,
        platforms: Optional[Collection[str]] = None,
        observation_platforms: Optional[Mapping[str, str]] = None,
        states: Optional[Collection[SendAttemptState]] = None,
        read_bytes: Optional[Callable[[Path], bytes]] = None,
        force_full: bool = False,
    ) -> tuple[SendAttempt, ...]:
        records: list[SendAttempt] = []
        try:
            self._refresh_metadata_unlocked(force_full=force_full)
            paths = self._selected_paths(
                observation_ids=observation_ids, logical_turn_ids=logical_turn_ids,
                profile_id=profile_id, states=states,
            )
            provider_platforms = None if platforms is None else set(platforms)
            for path in sorted(paths):
                record = self._record_versions[path][1]
                if self.record_filter is not None and not self.record_filter(record):
                    continue
                if provider_platforms is not None and _attempt_platform(
                    record, observation_platforms or {},
                ) not in provider_platforms:
                    continue
                if record.action == "google_search" and record.state in {
                    SendAttemptState.RESPONSE_CAPTURED,
                    SendAttemptState.ACKNOWLEDGED,
                    SendAttemptState.REJECTED,
                }:
                    _verify_google_response_artifact(
                        record, run_root=self.root.parent, read_bytes=read_bytes,
                    )
                records.append(deepcopy(record))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"invalid durable fences in {self.root}") from exc
        return tuple(records)

    def _assert_unique_attempt_unlocked(self, requested: SendAttempt) -> None:
        if requested.logical_turn_id is None or requested.attempt_ordinal is None:
            return
        # Refresh under the logical-turn lock; a previous decision snapshot
        # must never grant uniqueness across an await or another process's commit.
        for current in self._iter_records_unlocked(
            logical_turn_ids=(requested.normalized_logical_turn_id,),
            force_full=True,
        ):
            if current.fence_id == requested.fence_id:
                continue
            if current.normalized_attempt_ordinal == requested.normalized_attempt_ordinal:
                raise IntegrityError(
                    "logical turn attempt ordinal is already owned by "
                    f"{current.fence_id!r}"
                )

    def _read_without_lock(
        self, fence_id: str, *,
        read_bytes: Optional[Callable[[Path], bytes]] = None,
    ) -> Optional[SendAttempt]:
        path = self.path(fence_id)
        if not path.is_file():
            return None
        try:
            record = _read_send_attempt(path)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"invalid durable fence {path}") from exc
        if record.fence_id != fence_id:
            raise IntegrityError(f"durable fence path collision for {fence_id}")
        if (
            record.action == "google_search"
            and record.state
            in {
                SendAttemptState.RESPONSE_CAPTURED,
                SendAttemptState.ACKNOWLEDGED,
                SendAttemptState.REJECTED,
            }
        ):
            _verify_google_response_artifact(
                record,
                run_root=self.root.parent,
                read_bytes=read_bytes,
            )
        return record

    def _required_unlocked(self, fence_id: str) -> SendAttempt:
        value = self._read_without_lock(fence_id)
        if value is None:
            raise FenceConflictError(f"durable fence {fence_id!r} is missing")
        return value

    @contextmanager
    def _locked(self, fence_id: str, *, shared: bool = False) -> Iterator[None]:
        if not shared and self.ownership_guard is not None:
            self.ownership_guard()
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / f".{text_sha256(fence_id)[:24]}.lock"
        handle = lock_path.open("a+b")
        try:
            if fcntl is not None:
                operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(handle.fileno(), operation)
            if not shared and self.ownership_guard is not None:
                self.ownership_guard()
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


__all__ = [
    "AttemptSnapshot", "SendAttempt", "SendAttemptState", "SendAttemptStore",
    "attempt_allows_retry", "attempt_resume_decision", "count_attempt_milestones",
]
