"""Write and verify measurement plans, progress, results and response files."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional

from platform_runtime import file_locks as fcntl

from ..errors import (
    AlreadyExistsError,
    ArtifactHashMismatchError,
    ArtifactIntegrityError,
    ContractError,
    IntegrityError,
    NotFoundError,
    StateConflictError,
    StateTransitionError,
    UnknownArtifactReferenceError,
)
from ..gaio_evidence import canonical_gaio_terminal_evidence
from ..planning.hashing import bytes_sha256, canonical_sha256, json_value
from .evidence_reads import EvidenceReads
from .filesystem import (
    atomic_write_bytes,
    atomic_write_json,
    read_json,
)
from .schemas import (
    OBSERVATION_RESULT_SCHEMA_VERSION,
    PACKET_STATE_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    RUN_STATE_SCHEMA_VERSION,
)


if TYPE_CHECKING:
    from .send_attempts import SendAttempt


@dataclass(frozen=True)
class VerifiedMeasurements:
    """One verified disk traversal, owned by one synchronous inspection.

    This is not a cache or a send-admission token. A subsequent inspection
    reloads durable state, including all artifact integrity checks.
    """

    plan: dict[str, Any]
    observation_results: tuple[dict[str, Any], ...]
    runner_state: dict[str, Any]
    group_progress: dict[str, dict[str, Any]]
    attempt_records: tuple[SendAttempt, ...]


@dataclass
class RunSession:
    """One owner's runtime binding, scoped to the execution lifetime."""

    run_id: str
    thread_id: int
    owner: Any = None
    recovery: Any = None
    startup_factory: Any = None
    startup_in_progress: bool = False
    restart_authorization: Any = None


def _observation_index(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for row in (plan.get("observation_plan") or {}).get("observations") or ():
        if not isinstance(row, Mapping):
            continue
        observation_id = str(row.get("observation_id") or "")
        if observation_id in index:
            raise IntegrityError(
                f"observation result {observation_id!r} is not uniquely "
                "bound to the frozen plan"
            )
        index[observation_id] = row
    return index


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def safe_segment(value: object, *, field: str) -> str:
    result = str(value or "").strip()
    if not _SAFE_SEGMENT.fullmatch(result) or result in {".", ".."}:
        raise ValueError(f"{field} must be one safe path segment")
    return result


class MeasurementFiles:
    """Own only measurement plans, evidence, send attempts, and runner state."""

    def __init__(self, artifact_root: Path | str):
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self._session: Optional[RunSession] = None

    def begin_run_session(self, run_id: str, *, thread_id: int) -> RunSession:
        """Reserve the runtime before acquiring durable execution ownership."""
        if self._session is not None:
            raise StateTransitionError("storage_execution_already_owned")
        session = RunSession(run_id, thread_id)
        self._session = session
        return session

    def end_run_session(self, session: RunSession) -> None:
        if self._session is not session:
            raise StateTransitionError("storage_run_session_changed")
        session.restart_authorization = None
        self._session = None

    @property
    def active_session(self) -> Optional[RunSession]:
        return self._session

    def session_for(self, run_id: str) -> Optional[RunSession]:
        session = self._session
        return session if session is not None and session.run_id == run_id else None

    def owner_for(self, run_id: str):
        session = self.session_for(run_id)
        return session.owner if session is not None else None

    @property
    def execution_owners(self) -> Mapping[str, Any]:
        """Compatibility view for workers; storage owns one run session."""
        session = self._session
        return MappingProxyType(
            {session.run_id: session.owner} if session is not None and session.owner is not None else {})

    @property
    def recovery_execution(self):
        session = self._session
        return session.recovery if session is not None else None

    @recovery_execution.setter
    def recovery_execution(self, value):
        session = self._session
        if session is None:
            if value is not None:
                raise StateTransitionError("recovery_requires_owned_run_session")
            return
        if value is not None and value.snapshot.manifest.run_id != session.run_id:
            raise StateTransitionError("recovery_run_session_mismatch")
        session.recovery = value

    def run_dir(self, run_id: str) -> Path:
        path = self.artifact_root / safe_segment(run_id, field="run_id")
        if not path.resolve().is_relative_to(self.artifact_root):
            raise IntegrityError("run directory escapes artifact root")
        return path

    def create(self, plan: object, *, execution_policy: Optional[Mapping[str, Any]] = None) -> Path:
        payload = json_value(plan)
        if not isinstance(payload, Mapping):
            raise TypeError("plan must serialize to an object")
        run_id = safe_segment(payload.get("run_id"), field="plan.run_id")
        run_dir = self.run_dir(run_id)
        created_directory = False
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            created_directory = True
        except FileExistsError:
            allowed = {".run.lock", "execution-owner.json", "execution-history", "execution-heartbeat.json"}
            if not (run_dir / "execution-owner.json").exists() or any(path.name not in allowed for path in run_dir.iterdir()):
                raise AlreadyExistsError(
                    f"run {run_id!r} already exists; run() never implies resume"
                ) from None
        try:
            canonical = dict(payload)
            canonical.setdefault("schema_version", PLAN_SCHEMA_VERSION)
            canonical["plan_sha256"] = canonical_sha256(
                {key: value for key, value in canonical.items() if key != "plan_sha256"}
            )
            atomic_write_json(run_dir / "plan.json", canonical, exclusive=True)
            if execution_policy is not None:
                self.write_execution_control(run_id,{})
                if execution_policy.get('plan_sha256')!=canonical['plan_sha256']:
                    raise IntegrityError('execution_policy_plan_binding_mismatch')
                atomic_write_json(run_dir / 'execution-policy.json',dict(execution_policy),exclusive=True)
            for key, filename in (
                ("scope_manifest", "scope_manifest.json"),
                ("snapshot_prompts", "snapshot_prompts.json"),
                ("observation_plan", "observation_plan.json"),
            ):
                if isinstance(canonical.get(key), Mapping):
                    atomic_write_json(run_dir / filename, canonical[key], exclusive=True)
            packets = list(canonical.get("packets") or ())
            (run_dir / "packets").mkdir()
            for packet in packets:
                packet_id = safe_segment(packet.get("packet_id"), field="packet_id")
                packet_dir = run_dir / "packets" / packet_id
                packet_dir.mkdir()
                atomic_write_json(
                    packet_dir / "plan.json",
                    packet,
                    exclusive=True,
                )
                atomic_write_json(
                    packet_dir / "state.json",
                    {
                        "schema_version": PACKET_STATE_SCHEMA_VERSION,
                        "packet_id": packet_id,
                        "status": "planned",
                        "revision": 0,
                        "issues": [],
                        "transport_counts": {},
                    },
                    exclusive=True,
                )
            atomic_write_json(
                run_dir / "runner_state.json",
                {
                    "schema_version": RUN_STATE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "plan_sha256": canonical["plan_sha256"],
                    "status": "planned",
                    "revision": 0,
                    "packet_statuses": {
                        str(packet["packet_id"]): "planned" for packet in packets
                    },
                    "issues": [],
                    "transport_counts": {},
                    "cancel_requested": False,
                    **({'execution_policy_sha256':execution_policy['policy_sha256']} if execution_policy is not None else {}),
                },
                exclusive=True,
            )
        except BaseException:
            # A failed create has made no sends and may safely remove only the
            # brand-new directory this method exclusively created.
            if created_directory:
                _remove_new_tree(run_dir)
            raise
        return run_dir

    def read_plan(self, run_id: str) -> dict[str, Any]:
        plan = self._read_object(self.run_dir(run_id) / "plan.json")
        expected = str(plan.get("plan_sha256") or "")
        actual = canonical_sha256(
            {key: value for key, value in plan.items() if key != "plan_sha256"}
        )
        if not expected or expected != actual:
            raise IntegrityError(f"run {run_id!r} plan hash mismatch")
        return plan

    def read_progress(self, run_id: str) -> dict[str, Any]:
        path=self.run_dir(run_id)/'runner_state.json'
        try:
            value=self._read_object(path)
            self._validate_progress(value,run_id=run_id)
        except (NotFoundError,IntegrityError,TypeError,ValueError):
            value=self._rebuild_progress(run_id)[0]
        control=self.read_execution_control(run_id)
        if control and control.get('cancel_requested') and control.get('execution_id')==value.get('execution_id'):
            value={**value,'cancel_requested':True}
            if value.get('status') not in {'succeeded','failed','needs_attention'}:
                value['status']='cancelled'
        return value

    def read_execution_control(self,run_id):
        path=self.run_dir(run_id)/'execution-control.json'
        if not path.exists():
            policy_path=self.run_dir(run_id)/'execution-policy.json'
            if policy_path.exists() and self._read_object(policy_path).get('execution_control_required'):
                raise IntegrityError('required_execution_control_missing')
            return None
        value=self._read_object(path)
        digest=canonical_sha256({key:item for key,item in value.items() if key!='sha256'})
        if value.get('schema_version')!=1 or value.get('run_id')!=run_id or value.get('sha256')!=digest or not isinstance(value.get('cancel_requested'),bool):
            raise IntegrityError('execution_control_invalid')
        return value

    def write_execution_control(self,run_id,state):
        from datetime import datetime,timezone
        value={'schema_version':1,'run_id':run_id,'execution_id':state.get('execution_id'),
            'pass_id':state.get('recovery_pass_id'),'cancel_requested':bool(state.get('cancel_requested')),
            'recorded_at':datetime.now(timezone.utc).isoformat()}
        digest=canonical_sha256(value)
        record={**value,'sha256':digest}
        history=self.run_dir(run_id)/'execution-control-history'/f'{digest}.json'
        atomic_write_json(history,record,exclusive=True)
        atomic_write_json(self.run_dir(run_id)/'execution-control.json',record)

    @staticmethod
    def _validate_progress(value,*,run_id=None,packet_id=None):
        schema=RUN_STATE_SCHEMA_VERSION if packet_id is None else PACKET_STATE_SCHEMA_VERSION
        if value.get('schema_version')!=schema or value.get('status') not in {'planned','running','succeeded','partial','failed','needs_attention','cancelled'}:
            raise IntegrityError('mutable_progress_invalid')
        if packet_id is not None and value.get('packet_id')!=packet_id:
            raise IntegrityError('mutable_progress_identity_invalid')
        if run_id is not None and value.get('run_id')!=run_id:
            raise IntegrityError('mutable_progress_identity_invalid')
        if not isinstance(value.get('revision'),int) or isinstance(value.get('revision'),bool):
            raise IntegrityError('mutable_progress_revision_invalid')

    def _rebuild_progress(self,run_id):
        from ..recovery.api import RecoveryLedger
        from ..recovery_adapters.visibility import effective_results,manifest_for
        from ..recovery_adapters.execution_policy import read_policy
        from .send_attempts import SendAttemptStore
        from ..plan_serialization import load_run_plan
        from .progress_projection import project_progress, project_result, progress_fields
        plan=self.read_plan(run_id)
        ledger=RecoveryLedger(self.run_dir(run_id))
        events=ledger.read()
        manifests=[event['payload']['manifest'] for event in events if event['kind']=='manifest']
        if len(manifests)!=1:
            raise IntegrityError('mutable_progress_unrecoverable_without_durable_manifest')
        policy=read_policy(self,run_id,plan)
        if manifests[0]['scope_digest']!=manifest_for(plan,execution_policy=policy).scope_digest:
            raise IntegrityError('frozen_execution_policy_missing_or_changed')
        # The ledger verifies committed revision chains; the ordinary result
        # validator verifies the original files and every referenced capture.
        originals=[]
        root=self.run_dir(run_id)/'observations'
        if root.exists():
            for path in sorted(root.glob('*.json')):
                row=self._read_object(path)
                originals.append(self._canonical_observation_result(run_id,row['observation_id'],row,plan=plan))
        results=effective_results(self,run_id,originals)
        for result in results:
            for reference in (result.get('artifacts') or {}).values():
                if isinstance(reference,Mapping) and reference.get('path'):
                    self.read_evidence(run_id,reference)
        attempts=SendAttemptStore(self.run_dir(run_id)/'fences',watch_changes=False).snapshot().records
        runner, groups = project_progress(
            plan, results, attempts, ledger.normalized_events(),
            policy=policy, control=self.read_execution_control(run_id),
        )
        projection = project_result(load_run_plan(plan), results, attempts, runner, groups)
        runner.update(progress_fields(projection))
        for packet in projection.packets:
            groups[packet.packet_id].update(
                status=packet.status.value, transport_counts=json_value(packet.transport_counts),
            )
        return runner, groups

    def cancellation_requested(self, run_id: str) -> bool:
        """Read the process-independent, monotonic cancellation signal."""

        state = self.read_progress(run_id)
        return bool(state.get("cancel_requested")) or str(
            state.get("status") or ""
        ).casefold() == "cancelled"

    @contextmanager
    def submission_admission(self, run_id: str) -> Iterator[bool]:
        """Linearize durable cancellation against one send attempt transition.

        Callers must keep work inside this guard synchronous.  In particular,
        no browser await may occur while the runner-state lock is held.
        """

        path = self.run_dir(run_id) / "runner_state.json"
        with self._locked(path):
            state = self.read_progress(run_id)
            cancelled = bool(state.get("cancel_requested")) or str(
                state.get("status") or ""
            ).casefold() == "cancelled"
            yield not cancelled

    def read_measurement_group(self, run_id: str, packet_id: str) -> dict[str, Any]:
        path = (
            self.run_dir(run_id)
            / "packets"
            / safe_segment(packet_id, field="packet_id")
            / "plan.json"
        )
        return self._read_object(path)

    def read_group_progress(self, run_id: str, packet_id: str) -> dict[str, Any]:
        path = (
            self.run_dir(run_id)
            / "packets"
            / safe_segment(packet_id, field="packet_id")
            / "state.json"
        )
        try:
            value=self._read_object(path)
            self._validate_progress(value,packet_id=packet_id)
            return value
        except (NotFoundError,IntegrityError,TypeError,ValueError):
            return self._rebuild_progress(run_id)[1][packet_id]

    def update_state(
        self,
        run_id: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        path = self.run_dir(run_id) / "runner_state.json"
        return self._update(path, mutate, run_id=run_id, expected_revision=expected_revision,reader=lambda:self.read_progress(run_id))

    def update_packet_state(
        self,
        run_id: str,
        packet_id: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        path = (
            self.run_dir(run_id)
            / "packets"
            / safe_segment(packet_id, field="packet_id")
            / "state.json"
        )
        return self._update(path, mutate, run_id=run_id, expected_revision=expected_revision,reader=lambda:self.read_group_progress(run_id,packet_id))

    def write_evidence(
        self,
        run_id: str,
        payload: bytes | str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        suffix: str = ".bin",
    ) -> dict[str, Any]:
        owner=self.owner_for(run_id)
        if owner is not None:
            owner.assert_owned()
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if not isinstance(raw, bytes):
            raise TypeError("evidence payload must be bytes or text")
        digest = bytes_sha256(raw)
        extension = suffix if str(suffix).startswith(".") else f".{suffix}"
        path = self.run_dir(run_id) / "evidence" / digest[:2] / f"{digest}{extension}"
        if path.exists():
            if path.read_bytes() != raw:
                raise IntegrityError(f"immutable evidence collision at {path}")
        else:
            try:
                atomic_write_bytes(path, raw, exclusive=True)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise IntegrityError(f"immutable evidence collision at {path}")
        return {
            "path": path.relative_to(self.run_dir(run_id)).as_posix(),
            "sha256": digest,
            "byte_length": len(raw),
            "media_type": media_type,
        }

    def read_evidence(
        self,
        run_id: str,
        reference: Mapping[str, Any],
        *,
        _read_bytes: Optional[Callable[[Path], bytes]] = None,
    ) -> bytes:
        relative = Path(str(reference.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise IntegrityError("evidence reference escapes the run directory")
        root = self.run_dir(run_id)
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise IntegrityError("evidence reference escapes the run directory")
        try:
            payload = path.read_bytes() if _read_bytes is None else _read_bytes(path)
        except (FileNotFoundError, IsADirectoryError):
            raise UnknownArtifactReferenceError(
                f"evidence reference is unknown: {path}"
            ) from None
        except OSError as exc:
            raise ArtifactIntegrityError(
                f"evidence reference cannot be read: {path}"
            ) from exc
        if bytes_sha256(payload) != str(reference.get("sha256") or ""):
            raise ArtifactHashMismatchError(
                f"evidence hash mismatch: {path}"
            )
        if len(payload) != int(reference.get("byte_length", -1)):
            raise ArtifactIntegrityError(
                f"evidence byte length mismatch: {path}"
            )
        return payload

    def write_observation_result(
        self,
        run_id: str,
        observation_id: str,
        result: Mapping[str, Any],
    ) -> Path:
        observation_id = safe_segment(observation_id, field="observation_id")
        path = self.run_dir(run_id) / "observations" / f"{observation_id}.json"
        owner = self.owner_for(run_id)
        if owner is not None:
            owner.assert_owned()
        payload = dict(result)
        payload.setdefault("schema_version", OBSERVATION_RESULT_SCHEMA_VERSION)
        payload.setdefault("observation_id", observation_id)
        payload = self._canonical_observation_result(
            run_id,
            observation_id,
            payload,
        )
        session = self.session_for(run_id)
        recovery = session.recovery if session is not None else None
        if recovery is not None:
            committed = recovery.commit_result(observation_id, payload)
            if not recovery.initial:
                return committed
        if path.exists():
            existing = self._read_object(path)
            if existing != payload:
                raise IntegrityError(
                    f"observation result {observation_id} is immutable"
                )
            return path
        atomic_write_json(path, payload, exclusive=True)
        return path

    def iter_observation_results(
        self, run_id: str, *, observation_ids: Optional[tuple[str, ...]] = None,
    ) -> list[dict[str, Any]]:
        root = self.run_dir(run_id) / "observations"
        if not root.is_dir():
            return self._effective_results(run_id,[],observation_ids=observation_ids)
        plan = self.read_plan(run_id)
        observation_index = _observation_index(plan)
        results: list[dict[str, Any]] = []
        paths = (
            sorted(root.glob("*.json")) if observation_ids is None
            else [root / f"{safe_segment(value, field='observation_id')}.json"
                  for value in dict.fromkeys(observation_ids)]
        )
        for path in paths:
            if not path.exists():
                continue
            payload = self._read_object(path)
            observation_id = safe_segment(
                payload.get("observation_id"),
                field="observation_id",
            )
            results.append(
                self._canonical_observation_result(
                    run_id,
                    observation_id,
                    payload,
                    plan=plan,
                    observation_index=observation_index,
                )
            )
        return self._effective_results(
            run_id, results, observation_ids=observation_ids,
            plan=plan, observation_index=observation_index,
        )

    def _effective_results(
        self, run_id, results, *, observation_ids=None, require_verified=False,
        plan=None, observation_index=None, evidence_reads=None, attempt_records=None,
        recovery_ledger=None,
    ):
        # Committed recovery revisions are immutable; original result files are
        # always validated above and never rewritten or reparsed.
        from ..recovery_adapters.visibility import effective_results
        session = self.session_for(run_id)
        recovery = session.recovery if session is not None else None
        if recovery is not None and not require_verified:
            return recovery.current_results(results,observation_ids=observation_ids)
        if recovery_ledger is None and recovery is not None:
            # Active inspections still verify journal changes and every result
            # artifact. Reuse only the owner-scoped ledger's verified prefix.
            recovery_ledger = recovery.coordinator.ledger
        values = effective_results(
            self, run_id, results, observation_ids=observation_ids,
            plan=plan, observation_index=observation_index,
            evidence_reads=evidence_reads, attempt_records=attempt_records,
            recovery_ledger=recovery_ledger,
        )
        if recovery is not None:
            values=[row for row in values if row["observation_id"] not in recovery.selected or row["observation_id"] in recovery.finished]
        return values

    def verify(self, run_id: str) -> tuple[str, ...]:
        """Return all verification failures, preserving the public error API."""

        _, errors = self._verified_snapshot(run_id)
        return errors

    def read_verified_snapshot(self, run_id: str, *, _recovery_ledger=None) -> VerifiedMeasurements:
        """Load inspect inputs once, retaining plan and evidence verification."""

        snapshot, errors = self._verified_snapshot(run_id, recovery_ledger=_recovery_ledger)
        if errors:
            raise IntegrityError("; ".join(errors))
        assert snapshot is not None
        return snapshot

    def _verified_snapshot(
        self, run_id: str, *, recovery_ledger=None
    ) -> tuple[Optional[VerifiedMeasurements], tuple[str, ...]]:
        errors: list[str] = []
        results: list[dict[str, Any]] = []
        group_progress: dict[str, dict[str, Any]] = {}
        runner_state: dict[str, Any] = {}
        attempt_records: tuple[SendAttempt, ...] = ()
        evidence_reads = EvidenceReads()
        try:
            plan = self.read_plan(run_id)
        except (IntegrityError, NotFoundError) as exc:
            return None, (str(exc),)
        try:
            observation_index = _observation_index(plan)
        except IntegrityError as exc:
            return None, (str(exc),)
        observation_ids = set(observation_index)
        observation_order = tuple(observation_index)
        frozen_top_level = tuple(
            str(value) for value in plan.get("observation_ids") or ()
        )
        if frozen_top_level and frozen_top_level != observation_order:
            errors.append(
                "top-level observation IDs/order differ from observation_plan"
            )
        platform_by_observation = {
            observation_id: str(row.get("platform") or "")
            for observation_id, row in observation_index.items()
        }
        packet_ids: set[str] = set()
        measurement_members: list[str] = []
        gaio_extraction_members: list[str] = []
        saw_gaio_extraction_packet = False
        for packet in plan.get("packets") or ():
            if not isinstance(packet, Mapping):
                errors.append("plan contains a non-object packet")
                continue
            packet_id = str(packet.get("packet_id") or "")
            if packet_id in packet_ids:
                errors.append(f"duplicate packet id: {packet_id}")
            packet_ids.add(packet_id)
            members = [
                str(value)
                for value in packet.get("observation_ids") or ()
            ]
            kind = str(packet.get("kind") or "measurement")
            if kind in {"measurement", "source_parser"}:
                measurement_members.extend(members)
            elif kind == "gaio_extraction":
                saw_gaio_extraction_packet = True
                gaio_extraction_members.extend(members)
            else:
                errors.append(f"unknown packet kind: {kind}")
            try:
                stored_packet = self.read_measurement_group(run_id, packet_id)
                if stored_packet != dict(packet):
                    errors.append(
                        f"stored packet plan differs from run plan: {packet_id}"
                    )
                packet_state = self.read_group_progress(run_id, packet_id)
                group_progress[packet_id] = packet_state
                completed = {
                    str(value)
                    for value in packet_state.get(
                        "completed_observation_ids"
                    )
                    or ()
                }
                if not completed <= set(members):
                    errors.append(
                        f"packet state contains unknown completed members: "
                        f"{packet_id}"
                    )
                if str(packet_state.get("status") or "").casefold() not in {
                    "planned",
                    "running",
                    "succeeded",
                    "partial",
                    "needs_attention",
                    "failed",
                    "cancelled",
                }:
                    errors.append(f"packet has invalid status: {packet_id}")
            except (IntegrityError, NotFoundError) as exc:
                errors.append(str(exc))
        if len(measurement_members) != len(set(measurement_members)):
            errors.append(
                "an observation belongs to more than one measurement packet"
            )
        if set(measurement_members) != observation_ids:
            errors.append(
                "measurement packet membership does not exactly cover the "
                "frozen observations"
            )
        if len(gaio_extraction_members) != len(
            set(gaio_extraction_members)
        ):
            errors.append(
                "an observation belongs to more than one GAIO extraction packet"
            )
        expected_gaio_extraction = {
            observation_id
            for observation_id, platform in platform_by_observation.items()
            if platform == "google_ai_overview"
        }
        if (
            saw_gaio_extraction_packet
            and set(gaio_extraction_members) != expected_gaio_extraction
        ):
            errors.append(
                "GAIO extraction packet membership does not exactly cover "
                "the frozen Google AI Overview observations"
            )

        # Load all attempts once, including orphans left before result creation.
        # This store and its bounded byte reader live only for this traversal;
        # result binding below reuses the same validated attempt records.
        from .send_attempts import SendAttemptStore

        try:
            attempt_records = tuple(SendAttemptStore(
                self.run_dir(run_id) / "fences",
                verified_bytes_reader=evidence_reads.read_bytes,
                watch_changes=False,
            ).iter_records())
        except IntegrityError as exc:
            errors.append(str(exc))
        attempts_by_id = {record.fence_id: record for record in attempt_records}
        result_root = self.run_dir(run_id) / "observations"
        result_paths = (
            sorted(result_root.glob("*.json"))
            if result_root.is_dir()
            else ()
        )
        seen_result_ids: set[str] = set()
        for result_path in result_paths:
            try:
                result = self._read_object(result_path)
                result_observation_id = safe_segment(
                    result.get("observation_id"),
                    field="observation_id",
                )
                if result_observation_id in seen_result_ids:
                    raise IntegrityError(
                        f"duplicate stored observation result: {result_observation_id}"
                    )
                seen_result_ids.add(result_observation_id)
                result = self._canonical_observation_result(
                    run_id,
                    result_observation_id,
                    result,
                    plan=plan,
                    observation_index=observation_index,
                    _evidence_reads=evidence_reads,
                    _attempt_records=attempts_by_id,
                )
                results.append(result)
            except (IntegrityError, NotFoundError, ValueError) as exc:
                errors.append(str(exc))
                continue
            if str(result.get("observation_id") or "") not in observation_ids:
                errors.append("stored result is not a frozen observation")
            for value in (result.get("artifacts") or {}).values():
                if isinstance(value, Mapping) and value.get("path"):
                    try:
                        self.read_evidence(
                            run_id, value, _read_bytes=evidence_reads.read_bytes,
                        )
                    except (IntegrityError, NotFoundError) as exc:
                        errors.append(str(exc))
        try:
            runner_state = self.read_progress(run_id)
            if (
                str(runner_state.get("plan_sha256") or "")
                != str(plan.get("plan_sha256") or "")
            ):
                errors.append("runner state is bound to a different plan hash")
        except (IntegrityError, NotFoundError) as exc:
            errors.append(str(exc))

        # Every fence response retains its own digest check, even if another
        # reference already read and verified these bytes during this traversal.
        try:
            for record in attempt_records:
                if not record.response_reference:
                    continue
                relative = Path(record.response_reference)
                if relative.is_absolute() or ".." in relative.parts:
                    errors.append(
                        f"fence evidence escapes the run directory: "
                        f"{record.fence_id}"
                    )
                    continue
                path = self.run_dir(run_id) / relative
                try:
                    payload = evidence_reads.read_bytes(path)
                except OSError:
                    errors.append(f"missing fence response evidence: {path}")
                    continue
                if bytes_sha256(payload) != str(
                    record.response_sha256 or ""
                ):
                    errors.append(f"fence response hash mismatch: {path}")
        except IntegrityError as exc:
            errors.append(str(exc))
        if errors:
            return None, tuple(errors)
        try:
            results = self._effective_results(
                run_id, results, require_verified=True,
                plan=plan, observation_index=observation_index,
                evidence_reads=evidence_reads, attempt_records=attempts_by_id,
                recovery_ledger=recovery_ledger,
            )
        except (IntegrityError, OSError, ValueError) as exc:
            return None, (str(exc),)
        return VerifiedMeasurements(
            plan=plan,
            observation_results=tuple(results),
            runner_state=runner_state,
            group_progress=group_progress,
            attempt_records=attempt_records,
        ), ()

    def _canonical_observation_result(
        self,
        run_id: str,
        observation_id: str,
        payload: Mapping[str, Any],
        *,
        plan: Optional[Mapping[str, Any]] = None,
        observation_index: Optional[Mapping[str, Mapping[str, Any]]] = None,
        _evidence_reads: Optional[EvidenceReads] = None,
        _attempt_records: Optional[Mapping[str, SendAttempt]] = None,
    ) -> dict[str, Any]:
        """Bind every result to its plan and validate terminal Google evidence."""

        result = deepcopy(dict(payload))
        if str(result.get("observation_id") or "") != observation_id:
            raise IntegrityError(
                "observation result ID differs from its immutable path"
            )
        frozen_plan = plan if plan is not None else self.read_plan(run_id)
        index = (
            observation_index
            if observation_index is not None
            else _observation_index(frozen_plan)
        )
        observation = index.get(observation_id)
        if observation is None:
            raise IntegrityError(
                f"observation result {observation_id!r} is not uniquely "
                "bound to the frozen plan"
            )
        if observation.get("client_research_capability"):
            from .client_research_results import validate_client_result
            return validate_client_result(self, run_id, observation, result, frozen_plan, _attempt_records)
        if str(observation.get("platform") or "") == "github_copilot":
            from ..names.validate_source_result import validate_source_result
            return validate_source_result(self, run_id, observation, result, frozen_plan, _attempt_records)
        if str(observation.get("platform") or "") != "google_ai_overview":
            return result
        if str(result.get("platform") or "") != "google_ai_overview":
            raise IntegrityError(
                "Google observation result changed its frozen platform"
            )
        collection_status = str(
            result.get("collection_status") or ""
        ).casefold()
        result_status = str(result.get("status") or "").casefold()
        if collection_status not in {"collected", "not_shown"}:
            if result_status in {"succeeded", "not_shown"}:
                raise IntegrityError(
                    "terminal Google result lacks a terminal collection status"
                )
            return result
        # A verified collected Overview is still useful evidence when its
        # separate parser failed. Preserve that failure without promoting it
        # to an accepted result; all source/fence checks below still apply.
        parser_failed = (
            collection_status == "collected"
            and result_status == "partial"
            and str(result.get("extraction_status") or "").casefold()
            in {"rejected", "failed"}
        )
        if result_status not in {"succeeded", "not_shown"} and not parser_failed:
            raise IntegrityError(
                "terminal Google collection has a non-terminal result status"
            )
        if (
            result_status == "not_shown"
            and collection_status != "not_shown"
        ):
            raise IntegrityError(
                "Google not_shown result contradicts its collection status"
            )
        prompt = observation.get("prompt_text")
        if not isinstance(prompt, str):
            raise IntegrityError(
                "frozen Google observation lacks its exact prompt text"
            )
        prompt_sha256 = bytes_sha256(prompt.encode("utf-8"))
        frozen_prompt_sha256 = str(
            observation.get("prompt_text_sha256") or ""
        )
        if (
            frozen_prompt_sha256
            and frozen_prompt_sha256 != prompt_sha256
        ):
            raise IntegrityError(
                "frozen Google observation prompt hash is inconsistent"
            )
        packet_rows = tuple(
            row
            for row in frozen_plan.get("packets") or ()
            if isinstance(row, Mapping)
            and observation_id
            in {
                str(item)
                for item in row.get("observation_ids") or ()
            }
            and str(row.get("kind") or "measurement") == "measurement"
            and str(row.get("platform") or "google_ai_overview")
            == "google_ai_overview"
        )
        packet_profiles = {
            str(row.get("profile_id") or "")
            for row in packet_rows
            if str(row.get("profile_id") or "")
        }
        expected_profile_id = str(
            observation.get("profile_id") or ""
        )
        if not expected_profile_id:
            if len(packet_profiles) != 1:
                raise IntegrityError(
                    "frozen Google observation lacks one exact profile binding"
                )
            expected_profile_id = next(iter(packet_profiles))
        elif packet_profiles and packet_profiles != {expected_profile_id}:
            raise IntegrityError(
                "frozen Google observation and packet disagree on profile"
            )
        expected_chrome_directory = str(
            observation.get("chrome_directory") or ""
        )
        packet_directories = {
            str(row.get("chrome_directory") or "")
            for row in packet_rows
            if str(row.get("chrome_directory") or "")
        }
        if not expected_chrome_directory and len(packet_directories) == 1:
            expected_chrome_directory = next(iter(packet_directories))
        elif (
            expected_chrome_directory
            and packet_directories
            and packet_directories != {expected_chrome_directory}
        ):
            raise IntegrityError(
                "frozen Google observation and packet disagree on Chrome "
                "directory"
            )
        elif not expected_chrome_directory and len(packet_directories) > 1:
            raise IntegrityError(
                "frozen Google observation lacks one exact Chrome directory "
                "binding"
            )
        selected_profiles = frozen_plan.get("selected_profiles")
        if selected_profiles is None:
            selected_profiles = frozen_plan.get("profiles")
        for row in selected_profiles or ():
            if (
                isinstance(row, Mapping)
                and str(row.get("id") or "") == expected_profile_id
            ):
                selected_directory = str(
                    row.get("chrome_directory") or ""
                )
                if (
                    expected_chrome_directory
                    and selected_directory
                    and expected_chrome_directory != selected_directory
                ):
                    raise IntegrityError(
                        "frozen Google profile has conflicting Chrome "
                        "directories"
                    )
                expected_chrome_directory = (
                    expected_chrome_directory or selected_directory
                )
        if not expected_chrome_directory:
            raise IntegrityError(
                "frozen Google observation lacks one exact Chrome directory "
                "binding"
            )
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise IntegrityError(
                "terminal Google result lacks artifact references"
            )
        source_reference = artifacts.get("google_source_response")
        capture_reference = artifacts.get("google_capture_evidence")
        if not isinstance(source_reference, Mapping):
            raise IntegrityError(
                "terminal Google result lacks source evidence"
            )
        if not isinstance(capture_reference, Mapping):
            raise IntegrityError(
                "terminal Google result lacks capture-evidence artifact"
            )
        expected_state = (
            "shown" if collection_status == "collected" else "not_shown"
        )
        try:
            evidence = canonical_gaio_terminal_evidence(
                result.get("capture_evidence"),
                expected_state=expected_state,
                expected_prompt_sha256=prompt_sha256,
                expected_response_sha256=(
                    str(source_reference.get("sha256") or "")
                    if expected_state == "shown"
                    else None
                ),
            )
        except ContractError as exc:
            raise IntegrityError(
                f"invalid Google observation terminal evidence: {exc}"
            ) from exc
        evidence_identity = evidence["browser_target_identity_binding"]
        if (
            str(evidence_identity.get("profile_id") or "")
            != expected_profile_id
        ):
            raise IntegrityError(
                "Google terminal evidence is bound to a different frozen "
                "profile"
            )
        if (
            str(evidence_identity.get("chrome_directory") or "")
            != expected_chrome_directory
        ):
            raise IntegrityError(
                "Google terminal evidence is bound to a different frozen "
                "Chrome directory"
            )
        canonical_bytes = (
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        read_bytes = _evidence_reads.read_bytes if _evidence_reads else None
        capture_bytes = self.read_evidence(
            run_id, capture_reference, _read_bytes=read_bytes,
        )
        if capture_bytes != canonical_bytes:
            raise IntegrityError(
                "Google capture-evidence artifact differs from the "
                "canonical terminal evidence"
            )
        source_bytes = self.read_evidence(
            run_id, source_reference, _read_bytes=read_bytes,
        )
        if expected_state == "not_shown" and source_bytes != canonical_bytes:
            raise IntegrityError(
                "Google NOT_SHOWN source artifact differs from its terminal "
                "evidence"
            )
        accepted_attempts = result.get("accepted_attempt_ids")
        google_fence_id = (
            str(accepted_attempts.get("google_search") or "")
            if isinstance(accepted_attempts, Mapping)
            else ""
        )
        if not google_fence_id:
            raise IntegrityError(
                "terminal Google result lacks an accepted search fence"
            )
        from .send_attempts import SendAttemptState, SendAttemptStore

        fence = (
            _attempt_records.get(google_fence_id)
            if _attempt_records is not None
            else SendAttemptStore(
                self.run_dir(run_id) / "fences"
            ).load(google_fence_id)
        )
        expected_validation = {
            "shown": "collected",
            "not_shown": "not_shown",
        }[expected_state]
        if (
            fence is None
            or fence.state is not SendAttemptState.ACKNOWLEDGED
            or fence.observation_id != observation_id
            or fence.action != "google_search"
            or fence.prompt_sha256 != prompt_sha256
            or fence.validation_status != expected_validation
            or str(fence.identity.get("profile_id") or "")
            != expected_profile_id
            or str(fence.identity.get("chrome_directory") or "")
            != expected_chrome_directory
            or dict(fence.response_evidence or {}) != evidence
            or fence.response_reference
            != str(source_reference.get("path") or "")
            or fence.response_sha256
            != str(source_reference.get("sha256") or "")
        ):
            raise IntegrityError(
                "Google result differs from its accepted search fence"
            )
        result["capture_evidence"] = evidence
        result["prompt_sha256"] = prompt_sha256
        result["profile_id"] = expected_profile_id
        result["chrome_directory"] = expected_chrome_directory
        return result

    def _read_object(self, path: Path) -> dict[str, Any]:
        try:
            value = read_json(path)
        except FileNotFoundError:
            raise NotFoundError(f"required run artifact is missing: {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"invalid run artifact {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise IntegrityError(f"run artifact must contain an object: {path}")
        return value

    def _update(
        self,
        path: Path,
        mutate: Callable[[dict[str, Any]], None],
        *,
        run_id: str,
        expected_revision: Optional[int],
        reader=None,
    ) -> dict[str, Any]:
        owner = self.owner_for(run_id)
        if owner is not None:
            owner.assert_owned()
        with self._locked(path):
            if owner is not None:
                owner.assert_owned()
            value = reader() if reader is not None else self._read_object(path)
            revision = int(value.get("revision") or 0)
            if expected_revision is not None and revision != expected_revision:
                raise StateConflictError(
                    f"state revision is {revision}, expected {expected_revision}"
                )
            updated = deepcopy(value)
            mutate(updated)
            updated["revision"] = revision + 1
            # runner_state.json and packet state.json are projections that
            # _rebuild_progress recreates from the fully synced records, so
            # they do not need the device-cache flush.
            atomic_write_json(path, updated, full_sync=False)
            return updated

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        lock = path.with_name(f".{path.name}.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = lock.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _remove_new_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            path.rmdir()
    root.rmdir()


__all__ = ["MeasurementFiles", "safe_segment"]
