"""Reserve browser profiles and record blocked access.

Each website reserves shared access to the browser profile and exclusive
access to that profile's website. Different websites can therefore use the
same profile concurrently. Maintenance reserves the whole profile and blocks
website work until it finishes. Unconfirmed cleanup keeps access blocked.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import inspect
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable, Iterable, Iterator, Mapping, Optional, Sequence

from ..errors import (
    ProfileLeaseError as _CoreProfileLeaseError,
    ProfileLeaseQuarantined as _CoreProfileLeaseQuarantined,
    ProfileLeaseTimeout as _CoreProfileLeaseTimeout,
)
from .profile_matching import BrowserProfile
from ..recovery.contracts import safe_id
from ..recovery.durable import publish_json, publish_bytes, sync_directory
from platform_runtime.storage import remove_complete

from platform_runtime import file_locks as fcntl


DEFAULT_PROFILE_LEASE_ROOT = (
    Path(tempfile.gettempdir()) / "clinic-geo-profile-leases"
)


class ProfileLeaseError(_CoreProfileLeaseError):
    """Base error for exact profile ownership."""


class ProfileLeaseTimeout(ProfileLeaseError, _CoreProfileLeaseTimeout):
    """A conflicting profile and website pair did not release before the configured timeout."""


class ProfileLeaseCancelled(ProfileLeaseError):
    """Profile reservation acquisition was cancelled before any browser work started."""


class ProfileLeaseQuarantined(ProfileLeaseError, _CoreProfileLeaseQuarantined):
    """A prior owner could not prove that it released this profile and website pair safely."""


DEFAULT_VERIFICATION_HOLD_ROOT = Path.home() / ".local/share/visibility-runner/verification-holds"


class ProfileVerificationHeld(ProfileLeaseError):
    """An intentionally retained verification target blocks this exact lane."""


def verification_marker_path(root, namespace, chrome_directory, provider):
    # Default process locks may live in a temporary directory. Intentional
    # lifetime must survive that directory being cleaned or changed at boot.
    root = Path(root).expanduser().resolve()
    state_root = DEFAULT_VERIFICATION_HOLD_ROOT.expanduser().resolve() if root == DEFAULT_PROFILE_LEASE_ROOT.expanduser().resolve() else root
    key = _profile_website_key(state_root, namespace, chrome_directory, provider)
    return state_root / f"{key}.verification.json"


def _verification_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_verification_marker(path):
    """Read bounded, hash-bound intent; corruption never means no hold."""
    path = Path(path)
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        raise ProfileVerificationHeld("verification_hold_unreadable") from None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("episodes"), dict)
            or not isinstance(value.get("commands"), dict)
            or value.get("state_sha256") != _verification_digest(
                {k: v for k, v in value.items() if k != "state_sha256"})):
        raise ProfileVerificationHeld("verification_hold_invalid")
    try:
        if any(not isinstance(value.get(k), str) or not value[k] for k in (
                "namespace", "profile_id", "chrome_directory", "provider")) or value["provider"] != "copilot":
            raise ValueError()
        for episode_id, row in value["episodes"].items():
            if (not isinstance(row, dict) or row.get("episode_id") != episode_id
                    or type(row.get("retained")) is not bool
                    or type(row.get("revision")) is not int or row["revision"] < 1
                    or not isinstance(row.get("identity"), dict)
                    or type(row.get("deadline_monotonic")) not in (int, float)
                    or not math.isfinite(row["deadline_monotonic"])
                    or row.get("profile_id") != value["profile_id"]
                    or row.get("status") not in {"intent_pending", "manual_required", "native_queued",
                        "native_active", "clearance_check", "expired_held", "unsupported_held", "closed", "resumed"}
                    or type(row.get("background_only")) is not bool
                    or not isinstance(row.get("active_item_ids"), list)
                    or any(not isinstance(i, str) for i in row["active_item_ids"])):
                raise ValueError()
            for k in ("run_id", "plan_sha256", "execution_id", "observation_id", "browser_session_id"):
                if not isinstance(row.get(k), str) or not row[k]:
                    raise ValueError()
            for k in ("profile_id", "chrome_directory", "browser_context_reference",
                      "browser_target_reference", "tab_reference", "conversation_reference"):
                if not isinstance(row["identity"].get(k), str) or not row["identity"][k]:
                    raise ValueError()
            if (row["identity"]["profile_id"] != value["profile_id"]
                    or row["identity"]["chrome_directory"] != value["chrome_directory"]):
                raise ValueError()
        for request_id, c in value["commands"].items():
            if (not isinstance(c, dict) or c.get("request_id") != request_id
                    or c.get("episode_id") not in value["episodes"]
                    or c.get("action") not in {"show", "check", "close"}
                    or c.get("status") not in {"queued", "completed", "refused"}
                    or type(c.get("revision")) is not int or c["revision"] < 1
                    or not isinstance(c.get("execution_id"), str)):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ProfileVerificationHeld("verification_hold_invalid") from None
    return value


@contextmanager
def verification_marker_transaction(path, *, profile, namespace, provider, timeout_seconds=5):
    """Serialize UI commands and worker transitions on a stable lock inode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _profile_website_lock(str(path) + ".state")
    deadline = time.monotonic() + max(0, min(5, timeout_seconds))
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise ProfileVerificationHeld("verification_hold_lock_timeout")
    try:
        with path.with_suffix(".lock").open("a+") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProfileVerificationHeld("verification_hold_lock_timeout") from None
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
            try:
                body = read_verification_marker(path) or {
                    "schema_version": 1, "namespace": str(namespace),
                    "profile_id": profile.id, "chrome_directory": profile.chrome_directory,
                    "provider": provider, "episodes": {}, "commands": {},
                }
                if any(body.get(key) != expected for key, expected in (
                    ("namespace", str(namespace)), ("profile_id", profile.id),
                    ("chrome_directory", profile.chrome_directory), ("provider", provider))):
                    raise ProfileVerificationHeld("verification_hold_scope_changed")
                body.pop("state_sha256", None)
                yield body
                body["state_sha256"] = _verification_digest(body)
                publish_json(path, body)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


def _require_verification_access(root, *, profile, namespace, provider, references):
    # Copilot is currently the only holder, including during whole-profile
    # maintenance. Other provider lanes retain their existing shared access.
    if provider not in (None, "copilot"):
        return
    path = verification_marker_path(root, namespace, profile.chrome_directory, "copilot")
    marker = read_verification_marker(path)
    if marker is None:
        return
    if any(marker.get(key) != expected for key, expected in (
            ("namespace", str(namespace)), ("profile_id", profile.id),
            ("chrome_directory", profile.chrome_directory), ("provider", "copilot"))):
        raise ProfileVerificationHeld("verification_hold_scope_changed")
    held = frozenset("tab:" + str(row["identity"].get("tab_reference", ""))
                     for row in marker["episodes"].values() if row["retained"])
    if held and (provider != "copilot" or not references or references != held):
        raise ProfileVerificationHeld("verification_pair_held")


@dataclass(frozen=True)
class ProfileWebsite:
    profile_id: str
    chrome_directory: str
    provider: Optional[str]


@dataclass(frozen=True)
class BlockedProfileAccess:
    owner: str
    profile_id: str
    chrome_directory: str
    namespace: str
    provider: Optional[str]
    quarantined_at_epoch: float
    detail: str


class _ProfileAccessLock:
    """A fair-enough in-process readers/writer access check mirroring ``flock``."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    def acquire(
        self,
        *,
        shared: bool,
        deadline: float,
        cancelled: Callable[[], bool],
    ) -> bool:
        with self.condition:
            if shared:
                while self.writer or self.waiting_writers:
                    if cancelled():
                        raise ProfileLeaseCancelled(
                            "profile lease acquisition was cancelled"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self.condition.wait(timeout=min(0.05, remaining))
                self.readers += 1
                return True

            self.waiting_writers += 1
            try:
                while self.writer or self.readers:
                    if cancelled():
                        raise ProfileLeaseCancelled(
                            "profile lease acquisition was cancelled"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self.condition.wait(timeout=min(0.05, remaining))
                self.writer = True
                return True
            finally:
                self.waiting_writers -= 1

    def release(self, *, shared: bool) -> None:
        with self.condition:
            if shared:
                if self.readers <= 0:
                    raise ProfileLeaseError("shared profile gate was not acquired")
                self.readers -= 1
            else:
                if not self.writer:
                    raise ProfileLeaseError("exclusive profile gate was not acquired")
                self.writer = False
            self.condition.notify_all()


_PROFILE_ACCESS_LOCKS: dict[str, _ProfileAccessLock] = {}
_PROFILE_ACCESS_LOCKS_MUTEX = threading.Lock()
_PROFILE_WEBSITE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_WEBSITE_LOCKS_MUTEX = threading.Lock()


def _profile_access_lock(key: str) -> _ProfileAccessLock:
    with _PROFILE_ACCESS_LOCKS_MUTEX:
        result = _PROFILE_ACCESS_LOCKS.get(key)
        if result is None:
            result = _ProfileAccessLock()
            _PROFILE_ACCESS_LOCKS[key] = result
        return result


def _profile_website_lock(key: str) -> threading.Lock:
    with _PROFILE_WEBSITE_LOCKS_MUTEX:
        result = _PROFILE_WEBSITE_LOCKS.get(key)
        if result is None:
            result = threading.Lock()
            _PROFILE_WEBSITE_LOCKS[key] = result
        return result


def _provider_value(provider: object) -> Optional[str]:
    if provider is None:
        return None
    raw = getattr(provider, "value", provider)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ProfileLeaseError(
            "provider lane must be non-empty exact text without outer whitespace"
        )
    return raw


def _legacy_profile_lock_key(
    chrome_directory: str,
    namespace: str,
) -> str:
    """Return the host-compatible advisory-lock identity.

    The ClinicGeo rollback backend shipped this exact key and filename contract
    before the package boundary was introduced.  Keeping it here means the old
    and new implementations contend for the same physical/profile-website
    locks during the one-release rollback window.
    """

    material = f"{namespace}\0{chrome_directory}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _browser_profile_key(
    root: Path,
    namespace: str,
    chrome_directory: str,
) -> str:
    del root
    return _legacy_profile_lock_key(chrome_directory, namespace)


def _profile_website_key(
    root: Path,
    namespace: str,
    chrome_directory: str,
    provider: str,
) -> str:
    del root
    scoped_namespace = (
        f"{namespace}\0profile-lane:{provider.strip().casefold()}"
    )
    return _legacy_profile_lock_key(chrome_directory, scoped_namespace)


def _blocked_access_path(
    root: Path,
    namespace: str,
    chrome_directory: str,
    provider: Optional[str],
) -> Path:
    key = (
        _browser_profile_key(root, namespace, chrome_directory)
        if provider is None
        else _profile_website_key(
            root,
            namespace,
            chrome_directory,
            provider,
        )
    )
    return root / f"{key}.quarantine.json"


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    publish_json(path, dict(value))


def _resource_references(values: Iterable[str], *, required: bool = False) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ProfileLeaseError("resource references must be typed tab identifiers")
    try:
        result = tuple(values)
        for value in result:
            if not isinstance(value, str) or not value.startswith("tab:") or not value[4:]:
                raise ValueError()
            safe_id(value)
    except (TypeError, ValueError):
        raise ProfileLeaseError("resource references must be typed tab identifiers") from None
    if len(set(result)) != len(result) or (required and not result):
        raise ProfileLeaseError("containment requires nonempty distinct tab references")
    return frozenset(result)


def _matching_containment_marker(marker, *, profile, namespace, provider, resource_references):
    if (not marker or provider is None or marker.get("profile_id") != profile.id
        or marker.get("chrome_directory") != profile.chrome_directory or marker.get("namespace") != namespace
        or marker.get("provider") != provider or marker.get("lease_scope", provider) != provider):
        return False
    try:
        references = _resource_references(marker.get("resource_references", ()), required=True)
    except ProfileLeaseError:
        return False
    return references.issubset(resource_references)


def _read_marker(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # The marker's existence remains authoritative when its details cannot
        # be read.  Never turn a corrupt blocked access into permission to reuse.
        return {}
    return value if isinstance(value, Mapping) else {}


def _marker_present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ProfileLeaseQuarantined("profile quarantine marker cannot be inspected") from None


def _blocked_website_paths(
    root: Path,
    *,
    namespace: str,
    chrome_directory: str,
) -> tuple[Path, ...]:
    matches: list[Path] = []
    try:
        with os.scandir(root) as entries:
            candidates = [Path(entry.path) for entry in entries if entry.name.endswith(".quarantine.json")]
    except OSError:
        raise ProfileLeaseQuarantined("profile quarantine directory cannot be inspected") from None
    for candidate in candidates:
        marker = _read_marker(candidate)
        if not marker:
            # The marker remains authoritative even if interrupted/corrupt.
            # Its scope cannot be proven, so an unscoped owner fails closed.
            matches.append(candidate)
            continue
        if (
            marker.get("namespace") == namespace
            and marker.get("chrome_directory") == chrome_directory
            and (
                marker.get("provider") is not None
                or bool(str(marker.get("lease_scope") or ""))
            )
        ):
            matches.append(candidate)
    return tuple(sorted(matches))


def _require_profile_access(
    root: Path,
    *,
    profile: BrowserProfile,
    namespace: str,
    provider: Optional[str],
    include_all_websites: bool,
    containment_resource_references: frozenset[str] = frozenset(),
    verification_resource_references: frozenset[str] = frozenset(),
) -> None:
    _require_verification_access(root, profile=profile, namespace=namespace,
        provider=provider, references=verification_resource_references)
    candidates: list[Path] = []
    physical = _blocked_access_path(
        root,
        namespace,
        profile.chrome_directory,
        None,
    )
    if _marker_present(physical):
        candidates.append(physical)
    if provider is not None:
        lane = _blocked_access_path(
            root,
            namespace,
            profile.chrome_directory,
            provider,
        )
        if _marker_present(lane):
            candidates.append(lane)
    if include_all_websites:
        candidates.extend(
            candidate
            for candidate in _blocked_website_paths(
                root,
                namespace=namespace,
                chrome_directory=profile.chrome_directory,
            )
            if candidate not in candidates
        )
    if containment_resource_references and provider is not None:
        exact_lane = _blocked_access_path(root, namespace, profile.chrome_directory, provider)
        candidates = [candidate for candidate in candidates
                      if candidate != exact_lane or not _matching_containment_marker(
                          _read_marker(candidate), profile=profile, namespace=namespace, provider=provider,
                          resource_references=containment_resource_references)]
    if not candidates:
        return
    marker = _read_marker(candidates[0])
    detail = marker.get("detail")
    suffix = f": {detail}" if isinstance(detail, str) and detail else ""
    raise ProfileLeaseQuarantined(
        f"profile {profile.id!r} ({profile.chrome_directory!r}) is quarantined "
        f"by {candidates[0]}{suffix}"
    )


def block_profile_access(
    profiles: Iterable[BrowserProfile],
    *,
    owner: str,
    namespace: str,
    provider: object = None,
    lease_scope: object = None,
    detail: str = "",
    resource_references: Iterable[str] = (),
    lock_root: Path | str,
) -> tuple[Path, ...]:
    """Persist a fail-closed marker for an unconfirmed cleanup.

    ``provider`` and the compatibility alias ``lease_scope`` identify the same
    exact profile and website pair.  A scoped marker blocks only that profile and website pair; an unscoped
    marker blocks the whole physical profile.
    """

    if provider is not None and lease_scope is not None:
        if _provider_value(provider) != _provider_value(lease_scope):
            raise ProfileLeaseError("provider and lease_scope disagree")
    selected_provider = _provider_value(
        provider if provider is not None else lease_scope
    )
    references = _resource_references(resource_references)
    root = Path(lock_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    now = time.time()
    for profile in profiles:
        path = _blocked_access_path(
            root,
            str(namespace),
            profile.chrome_directory,
            selected_provider,
        )
        prior_marker = None
        profile_references = references
        if _marker_present(path):
            prior_marker = _read_marker(path)
            try:
                prior_references = _resource_references(prior_marker.get("resource_references", ()))
            except ProfileLeaseError:
                prior_references = frozenset()
            if not _matching_containment_marker(prior_marker, profile=profile, namespace=str(namespace),
                                               provider=selected_provider, resource_references=prior_references):
                # A new failure cannot erase an older ambiguous quarantine.
                paths.append(path)
                continue
            profile_references = references | _resource_references(prior_marker["resource_references"]) if references else frozenset()
        record = BlockedProfileAccess(
            owner=str(owner),
            profile_id=profile.id,
            chrome_directory=profile.chrome_directory,
            namespace=str(namespace),
            provider=selected_provider,
            quarantined_at_epoch=now,
            detail=str(detail),
        )
        _atomic_json(
            path,
            {
                **asdict(record),
                "resource_references": sorted(profile_references),
                "previous_marker": dict(prior_marker) if prior_marker else None,
                # Compatibility spelling read by the rollback backend.
                "lease_scope": selected_provider or "",
            },
        )
        paths.append(path)
    return tuple(paths)


@dataclass
class _Acquired:
    profile: BrowserProfile
    provider: Optional[str]
    physical_handle: object
    profile_access_lock: _ProfileAccessLock
    shared_physical: bool
    website_lock_file: Optional[object] = None
    website_thread_lock: Optional[threading.Lock] = None


class ProfileReservations:
    """An explicitly releasable set of ordered profile reservations."""

    def __init__(self, acquired: Sequence[_Acquired]):
        self._acquired = list(acquired)
        self._released = False
        self._pid = os.getpid()
        self._containment_binding = None
        self._containment_references = frozenset()

    @property
    def active(self) -> bool:
        return not self._released

    @property
    def lanes(self) -> tuple[ProfileWebsite, ...]:
        return tuple(
            ProfileWebsite(
                profile_id=item.profile.id,
                chrome_directory=item.profile.chrome_directory,
                provider=item.provider,
            )
            for item in self._acquired
        )

    def release(self) -> None:
        if self._released:
            return
        errors: list[BaseException] = []
        for item in reversed(self._acquired):
            try:
                if item.website_lock_file is not None:
                    try:
                        fcntl.flock(item.website_lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        item.website_lock_file.close()
                if item.website_thread_lock is not None:
                    item.website_thread_lock.release()
            except BaseException as exc:
                errors.append(exc)
            try:
                try:
                    fcntl.flock(
                        item.physical_handle.fileno(),
                        fcntl.LOCK_UN,
                    )
                finally:
                    item.physical_handle.close()
            except BaseException as exc:
                errors.append(exc)
            try:
                item.profile_access_lock.release(shared=item.shared_physical)
            except BaseException as exc:
                errors.append(exc)
        self._released = True
        self._acquired.clear()
        if errors:
            raise ProfileLeaseError(
                "one or more profile advisory leases could not be released"
            ) from errors[0]

    def __enter__(self) -> "ProfileReservations":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def _acquire_flock(
    handle: object,
    operation: int,
    *,
    deadline: float,
    cancelled: Callable[[], bool],
    profile: BrowserProfile,
) -> None:
    while True:
        if cancelled():
            raise ProfileLeaseCancelled(
                f"profile lease acquisition cancelled for {profile.id}"
            )
        try:
            fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ProfileLeaseTimeout(
                    f"timed out waiting for profile {profile.id!r} "
                    f"({profile.chrome_directory!r})"
                )
            time.sleep(0.05)


def reserve_profiles(
    profiles: Iterable[BrowserProfile],
    *,
    owner: str,
    namespace: str,
    provider: object = None,
    lease_scope: object = None,
    timeout_seconds: float = 300.0,
    cancelled: Optional[Callable[[], bool]] = None,
    lock_root: Path | str,
    _containment_resource_references: frozenset[str] = frozenset(),
    _verification_resource_references: frozenset[str] = frozenset(),
) -> ProfileReservations:
    """Acquire exact profile and website pairs in stable directory order to avoid deadlocks."""

    if provider is not None and lease_scope is not None:
        if _provider_value(provider) != _provider_value(lease_scope):
            raise ProfileLeaseError("provider and lease_scope disagree")
    selected_provider = _provider_value(
        provider if provider is not None else lease_scope
    )
    requested = sorted(
        tuple(profiles),
        key=lambda row: (row.chrome_directory.casefold(), row.id),
    )
    if len({row.chrome_directory for row in requested}) != len(requested):
        raise ProfileLeaseError("the requested lease repeats a Chrome directory")
    if not requested:
        return ProfileReservations(())

    root = Path(lock_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    namespace_value = str(namespace)
    if not namespace_value:
        raise ProfileLeaseError("lease namespace must be non-empty")
    is_cancelled = cancelled or (lambda: False)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    acquired: list[_Acquired] = []

    try:
        for profile in requested:
            physical_key = _browser_profile_key(
                root,
                namespace_value,
                profile.chrome_directory,
            )
            shared_physical = selected_provider is not None
            profile_access_lock = _profile_access_lock(physical_key)
            if not profile_access_lock.acquire(
                shared=shared_physical,
                deadline=deadline,
                cancelled=is_cancelled,
            ):
                raise ProfileLeaseTimeout(
                    f"timed out waiting for profile {profile.id!r} "
                    f"({profile.chrome_directory!r})"
                )

            physical_handle = None
            website_lock_file = None
            thread_lock = None
            thread_lock_acquired = False
            try:
                physical_handle = (
                    root / f"{physical_key}.lock"
                ).open("a+", encoding="utf-8")
                operation = (
                    fcntl.LOCK_SH
                    if shared_physical
                    else fcntl.LOCK_EX
                )
                _acquire_flock(
                    physical_handle,
                    operation,
                    deadline=deadline,
                    cancelled=is_cancelled,
                    profile=profile,
                )
                _require_profile_access(
                    root,
                    profile=profile,
                    namespace=namespace_value,
                    provider=selected_provider,
                    include_all_websites=not shared_physical,
                    containment_resource_references=_containment_resource_references,
                    verification_resource_references=_verification_resource_references,
                )

                if selected_provider is not None:
                    profile_website_key = _profile_website_key(
                        root,
                        namespace_value,
                        profile.chrome_directory,
                        selected_provider,
                    )
                    thread_lock = _profile_website_lock(profile_website_key)
                    while True:
                        if is_cancelled():
                            raise ProfileLeaseCancelled(
                                f"profile lease acquisition cancelled for {profile.id}"
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ProfileLeaseTimeout(
                                f"timed out waiting for provider lane "
                                f"{selected_provider!r} in profile {profile.id!r}"
                            )
                        if thread_lock.acquire(timeout=min(0.05, remaining)):
                            thread_lock_acquired = True
                            break
                    website_lock_file = (
                        root / f"{profile_website_key}.lock"
                    ).open("a+", encoding="utf-8")
                    _acquire_flock(
                        website_lock_file,
                        fcntl.LOCK_EX,
                        deadline=deadline,
                        cancelled=is_cancelled,
                        profile=profile,
                    )
                    _require_profile_access(
                        root,
                        profile=profile,
                        namespace=namespace_value,
                        provider=selected_provider,
                        include_all_websites=False,
                        containment_resource_references=_containment_resource_references,
                        verification_resource_references=_verification_resource_references,
                    )
                    metadata_handle = website_lock_file
                else:
                    metadata_handle = physical_handle

                metadata_handle.seek(0)
                metadata_handle.truncate()
                json.dump(
                    {
                        "owner": str(owner),
                        "profile_id": profile.id,
                        "chrome_directory": profile.chrome_directory,
                        "namespace": namespace_value,
                        "provider": selected_provider,
                        # Compatibility spelling read by the rollback backend.
                        "lease_scope": selected_provider or "",
                    },
                    metadata_handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                metadata_handle.write("\n")
                metadata_handle.flush()
                acquired.append(
                    _Acquired(
                        profile=profile,
                        provider=selected_provider,
                        physical_handle=physical_handle,
                        profile_access_lock=profile_access_lock,
                        shared_physical=shared_physical,
                        website_lock_file=website_lock_file,
                        website_thread_lock=thread_lock,
                    )
                )
            except BaseException:
                if website_lock_file is not None:
                    try:
                        fcntl.flock(website_lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        website_lock_file.close()
                if thread_lock is not None and thread_lock_acquired:
                    thread_lock.release()
                if physical_handle is not None:
                    try:
                        fcntl.flock(physical_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        physical_handle.close()
                profile_access_lock.release(shared=shared_physical)
                raise
    except BaseException:
        ProfileReservations(acquired).release()
        raise
    return ProfileReservations(acquired)


@contextmanager
def reserve_profiles_exclusively(
    profiles: Iterable[BrowserProfile],
    **kwargs: object,
) -> Iterator[ProfileReservations]:
    lease = reserve_profiles(profiles, **kwargs)
    try:
        yield lease
    finally:
        lease.release()


reserve_profile_website = reserve_profiles_exclusively


class ProfileReservationManager:
    """Runtime-scoped facade that freezes lock root and user-data namespace."""

    def __init__(self, lock_root: Path | str, *, namespace: str):
        self.lock_root = Path(lock_root).expanduser().resolve()
        self.namespace = str(namespace)

    def acquire(
        self,
        profiles: Iterable[BrowserProfile],
        *,
        owner: str,
        provider: object = None,
        timeout_seconds: float = 300.0,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> ProfileReservations:
        return reserve_profiles(
            profiles,
            owner=owner,
            namespace=self.namespace,
            provider=provider,
            timeout_seconds=timeout_seconds,
            cancelled=cancelled,
            lock_root=self.lock_root,
        )

    def containment_requirements(
        self,
        profile: BrowserProfile,
        *,
        provider: object,
    ) -> tuple[str, ...]:
        """Return the tab references this pair's quarantine marker names.

        An absent marker returns an empty tuple. A marker that belongs to a
        different pair, or whose content cannot be read, is never settled here
        and raises instead. This takes no lock and removes nothing: the caller
        must prove every returned reference absent before containment.
        """

        selected_provider = _provider_value(provider)
        path = _blocked_access_path(
            self.lock_root,
            self.namespace,
            profile.chrome_directory,
            selected_provider,
        )
        if not _marker_present(path):
            return ()
        marker = _read_marker(path)
        if (
            not marker
            or selected_provider is None
            or marker.get("profile_id") != profile.id
            or marker.get("chrome_directory") != profile.chrome_directory
            or marker.get("namespace") != self.namespace
            or marker.get("provider") != selected_provider
            or marker.get("lease_scope", selected_provider) != selected_provider
        ):
            raise ProfileLeaseQuarantined(
                "containment marker cannot be settled by this pair"
            )
        try:
            references = _resource_references(
                marker.get("resource_references", ()), required=False
            )
        except ProfileLeaseError:
            raise ProfileLeaseQuarantined(
                "containment marker cannot be settled by this pair"
            ) from None
        return tuple(sorted(references))

    def acquire_verification(self, profile, *, owner, resource_references,
                             timeout_seconds=5, cancelled=None):
        """Bypass only this exact intentional guard for explicit controls.

        Quarantine remains independent and cannot be bypassed by this lease.
        All held references must match; the caller acts on one correlated tab.
        """
        references = _resource_references(resource_references, required=True)
        return reserve_profiles((profile,), owner=owner, namespace=self.namespace,
            provider="copilot", timeout_seconds=timeout_seconds, cancelled=cancelled,
            lock_root=self.lock_root, _verification_resource_references=references)

    def acquire_containment(
        self, profiles: Iterable[BrowserProfile], *, owner: str, provider: object,
        resource_references: Iterable[str], timeout_seconds: float = 300.0,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> ProfileReservations:
        """Acquire the real lane locks solely for exact owned-resource cleanup."""
        selected_provider = _provider_value(provider)
        if selected_provider is None:
            raise ProfileLeaseError("containment requires an OS-locked exact provider lane")
        if isinstance(timeout_seconds, bool) or not math.isfinite(float(timeout_seconds)) or timeout_seconds < 0:
            raise ProfileLeaseError("containment timeout must be finite and nonnegative")
        references = _resource_references(resource_references, required=True)
        lease = reserve_profiles(profiles, owner=owner, namespace=self.namespace,
            provider=selected_provider, timeout_seconds=timeout_seconds, cancelled=cancelled,
            lock_root=self.lock_root, _containment_resource_references=references)
        if not lease.lanes:
            lease.release()
            raise ProfileLeaseError("containment requires an exact profile")
        lease._containment_binding = (self.lock_root, self.namespace)
        lease._containment_references = references
        return lease

    def resolve_containment(
        self, lease: ProfileReservations, *, resource_references: Iterable[str],
        record_resolution: Callable[[Mapping[str, object]], object],
    ) -> tuple[Path, ...]:
        """Archive complete markers durably before removing them under their lane locks.

        Absence returns no resolution and is never evidence of resource cleanup.
        The caller must independently verify every supplied owned resource.
        """
        references = _resource_references(resource_references, required=True)
        if (not isinstance(lease, ProfileReservations) or not lease.active or lease._pid != os.getpid()
            or lease._containment_binding != (self.lock_root, self.namespace)
            or lease._containment_references != references or not callable(record_resolution)):
            raise ProfileLeaseError("containment resolution requires its active exact lease")
        resolved = []
        for item in lease._acquired:
            if item.provider is None or item.website_lock_file is None or item.website_thread_lock is None or not item.website_thread_lock.locked():
                raise ProfileLeaseError("containment lane ownership is unavailable")
            for handle in (item.physical_handle, item.website_lock_file):
                try:
                    held, named = os.fstat(handle.fileno()), os.stat(handle.name)
                    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                        raise ValueError()
                except (OSError, ValueError):
                    raise ProfileLeaseError("containment lock identity changed") from None
            _require_profile_access(self.lock_root, profile=item.profile, namespace=self.namespace,
                provider=item.provider, include_all_websites=False, containment_resource_references=references)
            path = _blocked_access_path(self.lock_root, self.namespace, item.profile.chrome_directory, item.provider)
            try:
                original = path.read_bytes()
                identity = path.stat()
            except FileNotFoundError:
                continue
            marker = _read_marker(path)
            if not _matching_containment_marker(marker, profile=item.profile, namespace=self.namespace,
                                               provider=item.provider, resource_references=references):
                raise ProfileLeaseQuarantined("containment marker does not match the verified resource scope")
            result = record_resolution(dict(marker))
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ProfileLeaseError("containment resolution callback must finish durable publication synchronously")
            latest = path.stat()
            if path.read_bytes() != original or (latest.st_dev, latest.st_ino) != (identity.st_dev, identity.st_ino):
                raise ProfileLeaseQuarantined("containment marker changed during resolution")
            try:
                remove_complete(path, directory_sync=sync_directory)
            except OSError:
                # Retain the fail-closed marker when durable removal cannot be
                # confirmed; the caller's archived resolution remains intact.
                try:
                    publish_bytes(path, original, exclusive=True)
                except OSError:
                    pass
                raise
            resolved.append(path)
        return tuple(resolved)

    @contextmanager
    def lease(
        self,
        profiles: Iterable[BrowserProfile],
        *,
        owner: str,
        provider: object = None,
        timeout_seconds: float = 300.0,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Iterator[ProfileReservations]:
        lease = self.acquire(
            profiles,
            owner=owner,
            provider=provider,
            timeout_seconds=timeout_seconds,
            cancelled=cancelled,
        )
        try:
            yield lease
        finally:
            lease.release()

    def quarantine(
        self,
        profiles: Iterable[BrowserProfile],
        *,
        owner: str,
        provider: object = None,
        detail: str = "",
        resource_references: Iterable[str] = (),
    ) -> tuple[Path, ...]:
        return block_profile_access(
            profiles,
            owner=owner,
            namespace=self.namespace,
            provider=provider,
            detail=detail,
            resource_references=resource_references,
            lock_root=self.lock_root,
        )


__all__ = [
    "DEFAULT_PROFILE_LEASE_ROOT",
    "ProfileWebsite",
    "ProfileLeaseCancelled",
    "ProfileLeaseError",
    "ProfileReservationManager",
    "ProfileLeaseQuarantined",
    "ProfileReservations",
    "ProfileLeaseTimeout",
    "BlockedProfileAccess",
    "reserve_profiles",
    "reserve_profiles_exclusively",
    "reserve_profile_website",
    "block_profile_access",
]
