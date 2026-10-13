"""Stable OS ownership and conservative local process identification.

The lock inode is never unlinked or replaced. The descriptor remains held
through caller cleanup. A stale heartbeat is not a liveness decision.
"""
from __future__ import annotations

import base64
from platform_runtime import file_locks as fcntl
import hashlib
import json
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from platform_runtime.processes import boot_identity

from ..recovery.contracts import RecoveryBlocked, RecoveryIntegrityError
from ..recovery.durable import publish_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float | None
    executable: str | None
    boot_id: str | None
    execution_id: str | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{k: value.get(k) for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ProcessObservation:
    status: str
    reason_code: str
    sampled_at: str
    identity: ProcessIdentity | None = None

    def to_dict(self):
        return asdict(self)


def _boot_identity() -> str | None:
    return boot_identity()


def process_identity(pid: int | None = None, *, execution_id: str | None = None) -> ProcessIdentity:
    import psutil
    proc = psutil.Process(os.getpid() if pid is None else pid)
    return ProcessIdentity(proc.pid, proc.create_time(), proc.exe(), _boot_identity(), execution_id)


def inspect_process(identity: ProcessIdentity | dict[str, Any]) -> ProcessObservation:
    """Return alive/dead/reused/unknown; unreadable identity never means dead."""
    if isinstance(identity, Mapping):
        try:
            identity = ProcessIdentity.from_dict(identity)
        except (TypeError, ValueError):
            return ProcessObservation("unknown", "process_identity_invalid", utc_now())
    if (not isinstance(identity, ProcessIdentity) or type(identity.pid) is not int or identity.pid <= 0
        or type(identity.create_time) not in (int, float) or not math.isfinite(identity.create_time)
        or identity.create_time <= 0 or not isinstance(identity.executable, str) or not identity.executable
        or (identity.boot_id is not None and not isinstance(identity.boot_id, str))):
        return ProcessObservation("unknown", "process_identity_incomplete", utc_now(), identity if isinstance(identity, ProcessIdentity) else None)
    if not isinstance(identity.boot_id, str) or not identity.boot_id:
        return ProcessObservation("unknown", "boot_identity_unavailable", utc_now(), identity)
    try:
        import psutil
        boot = _boot_identity()
        if not isinstance(boot, str) or not boot:
            return ProcessObservation("unknown", "boot_identity_unavailable", utc_now(), identity)
        if identity.boot_id != boot:
            return ProcessObservation("dead", "different_boot", utc_now(), identity)
        current = process_identity(identity.pid)
        if not isinstance(current.boot_id, str) or not current.boot_id:
            return ProcessObservation("unknown", "boot_identity_unavailable", utc_now(), current)
        if current.boot_id != identity.boot_id:
            return ProcessObservation("dead", "different_boot", utc_now(), identity)
        if (type(current.pid) is not int or current.pid <= 0
            or type(current.create_time) not in (int, float) or not math.isfinite(current.create_time) or current.create_time <= 0
            or not isinstance(current.executable, str) or not current.executable):
            return ProcessObservation("unknown", "process_identity_incomplete", utc_now(), current)
        if current.create_time != identity.create_time or current.executable != identity.executable:
            return ProcessObservation("reused", "process_identity_mismatch", utc_now(), current)
        if psutil.Process(identity.pid).status() == psutil.STATUS_ZOMBIE:
            return ProcessObservation("dead", "process_zombie", utc_now(), current)
        return ProcessObservation("alive", "process_identity_verified", utc_now(), current)
    except psutil.NoSuchProcess:
        return ProcessObservation("dead", "process_absent", utc_now(), identity)
    except (psutil.AccessDenied, OSError, RuntimeError, ValueError):
        return ProcessObservation("unknown", "process_inspection_unavailable", utc_now(), identity)


inspect_worker_identity = inspect_process
capture_process_identity = process_identity


class RunOwnership:
    def __init__(self, run_dir: Path | str, execution_id: str | None = None, pass_id: str | None = None):
        self.run_dir = Path(run_dir)
        self.execution_id = str(uuid.uuid4()) if execution_id is None else safe_execution_id(execution_id)
        self.pass_id = pass_id
        self.lock_path = self.run_dir / ".run.lock"
        self.owner_path = self.run_dir / "execution-owner.json"
        self.fd: int | None = None
        self.identity: ProcessIdentity | None = None
        self._pid = os.getpid()
        self._inode: tuple[int, int] | None = None
        self._guard = threading.RLock()

    def acquire(self):
        if self.fd is not None:
            self.assert_owned()
            return self
        history = self.run_dir / "execution-history" / f"{self.execution_id}.json"
        _contained_paths(self.run_dir, self.lock_path, self.owner_path, history)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryBlocked("run_owned") from None
            if history.exists():
                raise RecoveryBlocked("execution_id_already_used")
            identity = process_identity(execution_id=self.execution_id)
            if (type(identity.pid) is not int or identity.pid <= 0
                or type(identity.create_time) not in (int, float) or not math.isfinite(identity.create_time) or identity.create_time <= 0
                or not isinstance(identity.executable, str) or not identity.executable
                or not isinstance(identity.boot_id, str) or not identity.boot_id):
                raise RecoveryBlocked("process_identity_unavailable")
            self.fd = fd
            self.identity = identity
            stat = os.fstat(fd)
            self._inode = (stat.st_dev, stat.st_ino)
            # Corrupted old owner is retained as a blocker, not silently erased.
            if self.owner_path.exists():
                previous_owner = self._read_owner()
                if previous_owner["status"] == "owned":
                    previous = inspect_process(previous_owner["identity"])
                    if previous.status == "alive":
                        raise RecoveryBlocked("previous_executor_alive_without_lock")
                    if previous.status == "unknown":
                        raise RecoveryBlocked("previous_executor_identity_unknown")
                if previous_owner.get("pass_id"):
                    # Older journals have no independent initialization marker.
                    # Retain their bound pass before replacing the only owner
                    # record, including output-only executions with no append.
                    binding_path = self.run_dir / "execution-history" / f"{previous_owner['execution_id']}.pass-binding.json"
                    _contained_paths(self.run_dir, binding_path)
                    binding = {"schema_version": 1, "execution_id": previous_owner["execution_id"],
                               "pass_id": previous_owner["pass_id"]}
                    if binding_path.exists():
                        try:
                            if json.loads(binding_path.read_text(encoding="utf-8")) != binding:
                                raise ValueError("pass binding changed")
                        except (OSError, ValueError, TypeError):
                            raise RecoveryIntegrityError("execution_pass_binding_invalid") from None
                    else:
                        publish_json(binding_path, binding, exclusive=True)
            owner = {"schema_version": 1, "execution_id": self.execution_id, "pass_id": self.pass_id,
                     "identity": identity.to_dict(), "acquired_at": utc_now(), "status": "owned"}
            publish_json(self.owner_path, owner)
            publish_json(history, owner, exclusive=True)
            return self
        except BaseException:
            self.fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise

    def _read_owner(self):
        try:
            value = json.loads(self.owner_path.read_text(encoding="utf-8"))
            _validate_owner(value)
            return value
        except (OSError, ValueError, TypeError):
            raise RecoveryIntegrityError("execution_owner_corrupt") from None

    def assert_owned(self):
        with self._guard:
            if self.fd is None or os.getpid() != self._pid:
                raise RecoveryBlocked("execution_not_owned")
            try:
                stat = os.stat(self.lock_path)
                if (stat.st_dev, stat.st_ino) != self._inode:
                    raise RecoveryBlocked("run_lock_inode_changed")
                owner = self._read_owner()
                if owner.get("execution_id") != self.execution_id or owner.get("status") != "owned":
                    raise RecoveryBlocked("stale_execution")
            except OSError:
                raise RecoveryBlocked("run_lock_unavailable") from None

    def bind_pass(self, pass_id: str):
        self.assert_owned()
        value = self._read_owner()
        value["pass_id"] = pass_id
        publish_json(self.owner_path, value)
        self.pass_id = pass_id

    def heartbeat(self, *, progress: bool = False, operation_id: str | None = None, deadline_monotonic: float | None = None):
        with self._guard:
            self.assert_owned()
            path = self.run_dir / "execution-heartbeat.json"
            previous = {}
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            now = time.monotonic()
            same = previous.get("execution_id") == self.execution_id
            operations = dict(previous.get("operations", {})) if same else {}
            if operation_id is not None:
                operation = dict(operations.get(operation_id, {}))
                operation["progress_monotonic"] = now if progress or not operation else operation.get("progress_monotonic")
                if deadline_monotonic is not None:
                    operation["deadline_monotonic"] = deadline_monotonic
                operations[operation_id] = operation
            value = {"schema_version": 1, "execution_id": self.execution_id, "pass_id": self.pass_id,
                     "sampled_at": utc_now(), "monotonic": now, "boot_id": self.identity.boot_id,
                     "progress_monotonic": now if progress or not same else previous.get("progress_monotonic"),
                     "operation_id": operation_id if operation_id is not None else previous.get("operation_id") if same else None,
                     "deadline_monotonic": deadline_monotonic if deadline_monotonic is not None else previous.get("deadline_monotonic") if same else None,
                     "operations": operations}
            # The heartbeat is an observation, never authorization: an
            # ordinary fsync is enough and keeps the 2-second writer cheap.
            publish_json(path, value, full_sync=False)

    def complete_operation(self, operation_id: str):
        with self._guard:
            self.assert_owned()
            path = self.run_dir / "execution-heartbeat.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if value.get("execution_id") != self.execution_id:
                return
            operations = dict(value.get("operations", {}))
            operations.pop(operation_id, None)
            value["operations"] = operations
            value.update(sampled_at=utc_now(), monotonic=time.monotonic(), progress_monotonic=time.monotonic())
            if value.get("operation_id") == operation_id:
                value["operation_id"] = next(iter(operations), None)
                value["deadline_monotonic"] = operations.get(value["operation_id"], {}).get("deadline_monotonic")
            publish_json(path, value, full_sync=False)

    def release(self):
        with self._guard:
            if self.fd is None:
                return
            fd, self.fd = self.fd, None
            try:
                if os.getpid() == self._pid:
                    try:
                        current_lock = self.lock_path.stat()
                    except OSError:
                        return
                    if (current_lock.st_dev, current_lock.st_ino) != self._inode:
                        return
                    value = self._read_owner()
                    if value.get("execution_id") == self.execution_id:
                        value.update(status="released", released_at=utc_now())
                        publish_json(self.owner_path, value)
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def _contained_paths(run_dir: Path, *paths: Path) -> None:
    """Reject escaping descendants before state I/O; no concurrent tamper promise."""
    root = run_dir.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise RecoveryIntegrityError("execution_path_escapes_run")


def _validate_owner(value: Any) -> None:
    if (not isinstance(value, dict) or value.get("schema_version") != 1
        or not isinstance(value.get("execution_id"), str)
        or value.get("status") not in {"owned", "released"}
        or not isinstance(value.get("identity"), dict)):
        raise ValueError("invalid owner envelope")
    try:
        safe_execution_id(value["execution_id"])
    except RecoveryBlocked:
        raise ValueError("unsafe stored execution identity") from None
    identity = value["identity"]
    if (type(identity.get("pid")) is not int or identity["pid"] <= 0
        or type(identity.get("create_time")) not in (int, float)
        or not math.isfinite(identity["create_time"]) or identity["create_time"] <= 0
        or not isinstance(identity.get("executable"), str) or not identity["executable"]):
        raise ValueError("invalid owner identity")


def release_stale_ownership(run_dir: Path | str, *, reason: str, released_by: str = "operator") -> dict[str, Any]:
    """Repair a damaged/stale owner under the stable lock with lossless evidence.

    An existing archive is replayable only when it binds the exact original
    bytes. Retrying interrupted publication preserves its first actor/reason.
    Neither an old heartbeat nor an invalid envelope establishes process death.
    """
    run_dir = Path(run_dir)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("a release reason is required")
    owner_path = run_dir / "execution-owner.json"
    lock_path = run_dir / ".run.lock"
    _contained_paths(run_dir, owner_path, lock_path, run_dir / "execution-history")
    if not run_dir.exists():
        raise RecoveryBlocked("owner_record_missing")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryBlocked("run_owned") from None
        # Observe only after taking the lock; a competing repair/acquisition may
        # have published a different owner while this caller was waiting.
        try:
            raw = owner_path.read_bytes()
        except FileNotFoundError:
            raise RecoveryBlocked("owner_record_missing") from None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        execution_id = value.get("execution_id") if isinstance(value, dict) else None
        identity = value.get("identity") if isinstance(value, dict) else None
        observation = inspect_process(identity if isinstance(identity, Mapping) else {}).to_dict()
        try:
            _validate_owner(value)
            corrupt = False
        except (ValueError, TypeError):
            corrupt = True
        if not corrupt and value["status"] == "released":
            return {"released": False, "reason_code": "already_released", "execution_id": execution_id}
        if observation.get("status") == "alive":
            raise RecoveryBlocked("previous_executor_alive")
        raw_digest = hashlib.sha256(raw).hexdigest()
        try:
            archive_name = safe_execution_id(execution_id)
        except RecoveryBlocked:
            archive_name = "corrupt-" + raw_digest[:24]
        archive_path = run_dir / "execution-history" / f"{archive_name}.operator-release.json"
        _contained_paths(run_dir, archive_path)
        archive = {"schema_version": 1, "released_by": released_by, "reason": reason,
                   "released_at": utc_now(), "process_observation": observation,
                   "owner_record_corrupt": corrupt,
                   "owner_record_base64": base64.b64encode(raw).decode("ascii"),
                   "owner_record_sha256": raw_digest}
        # Retain the legacy readable field for valid UTF-8 without treating it
        # as the lossless representation for malformed binary input.
        try:
            archive["owner_record_text"] = raw.decode("utf-8")
        except UnicodeError:
            pass
        if archive_path.exists():
            try:
                saved = json.loads(archive_path.read_bytes())
                saved_raw = base64.b64decode(saved["owner_record_base64"], validate=True)
                if (saved_raw != raw or saved["owner_record_sha256"] != raw_digest
                    or saved.get("schema_version") != 1 or not saved.get("reason")
                    or not saved.get("released_by") or not saved.get("released_at")
                    or not isinstance(saved.get("process_observation"), dict)
                    or saved.get("owner_record_corrupt") is not corrupt):
                    raise ValueError("archive binding differs")
                archive = saved
            except (OSError, ValueError, TypeError, KeyError):
                raise RecoveryIntegrityError("operator_release_archive_mismatch") from None
        else:
            publish_json(archive_path, archive, exclusive=True)
        if corrupt:
            repaired_id = "operator-release:" + raw_digest[:24]
            releasing = process_identity(execution_id=repaired_id)
            released = {"schema_version": 1, "execution_id": repaired_id,
                        "pass_id": None, "identity": releasing.to_dict(), "acquired_at": None}
        else:
            released = dict(value)
        released.update(status="released", released_at=archive["released_at"],
                        released_by=archive["released_by"], reason=archive["reason"])
        publish_json(owner_path, released)
        return {"released": True, "execution_id": execution_id, "process_observation": observation,
                "archive": str(archive_path)}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def safe_execution_id(value: str) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,120}", value):
        raise RecoveryBlocked("execution_id_unsafe")
    return value


def inspect_run_ownership(run_dir: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir)
    path = run_dir / "execution-owner.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        _validate_owner(value)
        identity = ProcessIdentity.from_dict(value["identity"])
        observation = inspect_process(identity)
        return {"execution_id": value.get("execution_id"), "pass_id": value.get("pass_id"),
                "owner_state": value.get("status"), **observation.to_dict()}
    except FileNotFoundError:
        return {"status": "unknown", "reason_code": "owner_record_missing", "sampled_at": utc_now()}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "unknown", "reason_code": "owner_record_corrupt", "sampled_at": utc_now()}
