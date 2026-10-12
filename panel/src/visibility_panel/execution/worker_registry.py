"""Durable host worker identities and conservative local watchdog.

This is infrastructure only: no recovery eligibility, measurement outcomes or
browser resources are inferred here. Losing a heartbeat never grants admission.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


class RegistryError(RuntimeError):
    """A critical record is absent, corrupt or conflicts with its binding."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sync(fd: int, *, full: bool = True) -> None:
    os.fsync(fd)
    if full and sys.platform == "darwin":
        # fsync alone may leave drive caches pending on macOS. A failed full
        # flush must reach the caller before any external effect is admitted.
        fcntl.fcntl(fd, 51)  # F_FULLFSYNC


def publish_json(path: Path, value: object, *, replace: bool = False, durable: bool = True) -> None:
    """Publish a complete flushed file, without exposing a reservation file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            if durable:
                _sync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # atomic no-overwrite publication
        if durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_record(path: Path, *, optional: bool = False) -> dict | None:
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        if optional:
            return None
        raise RegistryError("record_missing") from None
    except (OSError, ValueError):
        raise RegistryError("record_unreadable") from None
    if not isinstance(value, dict):
        raise RegistryError("record_invalid")
    return value


def boot_identity() -> str | None:
    try:
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=2, check=True)
            return "boot-session:" + result.stdout.strip() if result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: float
    executable: str
    boot_id: str | None


def _complete_identity(value: ProcessIdentity | None) -> bool:
    return (isinstance(value, ProcessIdentity)
            and type(value.pid) is int and value.pid > 0
            and type(value.created) in (int, float) and math.isfinite(value.created) and value.created > 0
            and isinstance(value.executable, str) and bool(value.executable.strip()) and os.path.isabs(value.executable)
            and isinstance(value.boot_id, str) and bool(value.boot_id.strip()))


def process_identity(pid: int) -> ProcessIdentity | None:
    """None means proven absent under a sampled boot; failed inspection raises."""
    try:
        sampled_boot = boot_identity()
        if not isinstance(sampled_boot, str) or not sampled_boot.strip():
            raise RegistryError("boot_identity_unavailable")
        if type(pid) is not int or pid <= 0:
            raise RegistryError("process_identity_invalid")
        import psutil
        process = psutil.Process(pid)
        with process.oneshot():
            created = process.create_time()
            executable = process.exe()
        if not isinstance(executable, str) or not executable.strip():
            raise RegistryError("identity_unavailable")
        identity = ProcessIdentity(pid, created, os.path.realpath(executable), sampled_boot)
        if not _complete_identity(identity):
            raise RegistryError("identity_unavailable")
        return identity
    except ImportError:
        raise RegistryError("process_inspection_unavailable") from None
    except Exception as exc:
        if type(exc).__name__ == "NoSuchProcess":
            return None
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError("process_inspection_failed") from None


def compare_identity(expected: ProcessIdentity, observed: ProcessIdentity | None) -> str:
    if not _complete_identity(expected):
        return "unknown"
    if observed is None:
        return "absent"
    if not _complete_identity(observed):
        return "unknown"
    if expected.boot_id != observed.boot_id:
        return "absent"
    if (expected.pid, expected.created) != (observed.pid, observed.created):
        return "reused"
    if expected.executable != observed.executable:
        # exec() preserves PID and creation time. In particular, framework
        # Python on macOS replaces its launcher before registering the worker.
        # This is neither proof of death nor authority to signal the new image.
        return "exec_changed"
    return "alive"


def discover_legacy_worker(command: list[str], *, timeout: float = 2, exclude_pid: int | None = None) -> dict:
    """Bounded exact legacy-worker discovery, never substring PID guessing.

An inaccessible possible candidate makes the result unknown. No durable
authorization is created by this read-only process inventory.
"""
    try:
        import psutil
        if not command or "--run-id" not in command or "--measurement" not in command:
            raise RegistryError("legacy_command_binding_missing")
        expected_exe = os.path.realpath(command[0])
        run_id = command[command.index("--run-id") + 1]
        deadline = time.monotonic() + timeout
        unknown = False
        matches = []
        for process in psutil.process_iter(["pid", "exe", "cmdline", "uids"], ad_value=None):
            if time.monotonic() > deadline:
                return {"state": "unknown", "reason": "legacy_process_discovery_deadline"}
            information = process.info
            if information["pid"] == exclude_pid:
                continue
            uids = information.get("uids")
            if uids is not None and uids.real != os.getuid():
                continue
            executable, arguments = information.get("exe"), information.get("cmdline")
            if executable and os.path.realpath(executable) != expected_exe:
                continue
            if not arguments:
                unknown = True
                continue
            if "--run-id" not in arguments:
                continue
            index = arguments.index("--run-id")
            if len(arguments) <= index + 1 or arguments[index + 1] != run_id:
                continue
            if arguments[1:] != command[1:] or not executable:
                unknown = True
                continue
            identity = process_identity(information["pid"])
            if identity is None:
                continue
            if identity.boot_id is None:
                unknown = True
                continue
            matches.append(asdict(identity))
        if matches:
            return {"state": "alive", "reason": "legacy_worker_verified", "identities": matches}
        return {"state": "unknown" if unknown else "absent", "reason": "legacy_process_inspection_incomplete" if unknown else "legacy_worker_absent"}
    except Exception:
        return {"state": "unknown", "reason": "legacy_process_inspection_failed"}


class WorkerRegistry:
    def __init__(self, job_dir: Path, *, inspect_process: Callable = process_identity):
        self.job_dir = Path(job_dir)
        self.root = self.job_dir / "executions"
        self.inspect_process = inspect_process
        self._launch_local = threading.local()

    def path(self, execution_id: str) -> Path:
        try:
            if str(uuid.UUID(execution_id)) != execution_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RegistryError("execution_identity_invalid") from None
        return self.root / execution_id

    @contextlib.contextmanager
    def launch_lock(self, *, timeout: float = 2):
        if getattr(self._launch_local, "held", False):
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True)
        # The inode is permanent. A process crash releases only the kernel lock.
        fd = os.open(self.root / ".launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RegistryError("launch_ownership_busy") from None
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            self._launch_local.held = True
            yield
        finally:
            self._launch_local.held = False
            os.close(fd)

    def requests(self) -> tuple[dict, ...]:
        if not self.root.exists():
            return ()
        result = []
        for directory in sorted(self.root.iterdir()):
            if directory.name.startswith("."):
                continue
            if not directory.is_dir():
                raise RegistryError("unexpected_execution_record")
            value = self.request(directory.name)
            result.append(value)
        return tuple(result)

    def request(self, execution_id: str) -> dict:
        value = read_record(self.path(execution_id) / "request.json")
        return self.validate_request(execution_id, value)

    def validate_request(self, execution_id: str, value: Mapping) -> dict:
        """Validate an already-read request using the registry's exact contract."""
        self.path(execution_id)
        if not isinstance(value, Mapping):
            raise RegistryError("request_invalid")
        if value.get("execution_id") != execution_id or value.get("schema_version") != "host-execution-v1":
            raise RegistryError("request_identity_mismatch")
        payload = self.request_payload(value)
        if digest(payload) != value.get("request_sha256"):
            raise RegistryError("request_digest_mismatch")
        if "launch_guard" in value or "launch_guard_sha256" in value:
            guard = value.get("launch_guard")
            try:
                launcher = ProcessIdentity(**guard["launcher_identity"])
            except (TypeError, KeyError):
                raise RegistryError("launch_guard_invalid") from None
            if (not isinstance(guard, dict) or guard.get("schema_version") != "host-launch-guard-v1"
                or guard.get("execution_id") != execution_id
                or guard.get("request_sha256") != value["request_sha256"]
                or digest(guard) != value.get("launch_guard_sha256")
                or not _complete_identity(launcher)):
                raise RegistryError("launch_guard_invalid")
        return dict(value)

    def find_request(self, request_id: str) -> dict | None:
        matches = [value for value in self.requests() if value.get("request_id") == request_id]
        receipt_path = self.root / ".request-receipts" / (digest(request_id) + ".json")
        receipt = read_record(receipt_path, optional=True)
        if receipt is not None:
            if (receipt.get("schema_version") != "host-request-receipt-v1"
                or receipt.get("request_id") != request_id
                or receipt.get("request_sha256") != digest(self.request_payload(receipt))):
                raise RegistryError("request_receipt_invalid")
            target = self.request(receipt.get("execution_id"))
            if self.compatible_payload(target) != self.compatible_payload(receipt):
                raise RegistryError("request_receipt_binding_mismatch")
            matches.append(receipt)
        if len(matches) > 1:
            raise RegistryError("duplicate_request_records")
        return matches[0] if matches else None

    @staticmethod
    def request_payload(value: Mapping) -> dict:
        payload = {key: value.get(key) for key in ("run_id", "mode", "bindings", "recovery")}
        # Omit for old records so existing immutable v1 hashes stay valid.
        if "expected_execution_id" in value:
            payload["expected_execution_id"] = value["expected_execution_id"]
        return payload

    @classmethod
    def compatible_payload(cls, value: Mapping) -> dict:
        payload = cls.request_payload(value)
        if isinstance(payload.get("recovery"), Mapping):
            payload["recovery"] = {key: item for key, item in payload["recovery"].items() if key != "request_id"}
        return payload

    def join_request(self, execution_id: str, *, request_id: str, payload: Mapping) -> dict:
        """Persist an accepted alias without creating another execution."""
        with self.launch_lock():
            target = self.request(execution_id)
            expected = {**dict(payload), "request_id": request_id}
            if self.compatible_payload(target) != self.compatible_payload(expected):
                raise RegistryError("active_execution_incompatible")
            existing = self.find_request(request_id)
            if existing:
                if existing.get("request_sha256") != digest(self.request_payload(expected)):
                    raise RegistryError("request_identity_conflict")
                return existing
            record = {"schema_version": "host-request-receipt-v1", "execution_id": execution_id,
                      "created_at": utc_now(), **expected,
                      "request_sha256": digest(self.request_payload(expected))}
            publish_json(self.root / ".request-receipts" / (digest(request_id) + ".json"), record)
            return record

    def create_request(self, *, request_id: str, run_id: str, mode: str, bindings: Mapping, recovery: Mapping | None = None, expected_execution_id: str | None = None, managed_launch: bool = False) -> tuple[dict, bool]:
        with self.launch_lock():
            return self._create_request(request_id=request_id, run_id=run_id, mode=mode, bindings=bindings, recovery=recovery, expected_execution_id=expected_execution_id, managed_launch=managed_launch)

    def _create_request(self, *, request_id: str, run_id: str, mode: str, bindings: Mapping, recovery: Mapping | None = None, expected_execution_id: str | None = None, managed_launch: bool = False) -> tuple[dict, bool]:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id is required")
        if mode not in {"run", "resume", "refetch", "finalize"}:
            raise ValueError("invalid execution mode")
        payload = {"run_id": run_id, "mode": mode, "bindings": dict(bindings), "recovery": dict(recovery) if recovery is not None else None}
        if expected_execution_id is not None:
            self.path(expected_execution_id)
            payload["expected_execution_id"] = expected_execution_id
        existing = self.find_request(request_id)
        if existing:
            if existing.get("request_sha256") != digest(payload):
                raise RegistryError("request_identity_conflict")
            return existing, False
        execution_id = str(uuid.uuid4())
        record = {"schema_version": "host-execution-v1", "execution_id": execution_id, "request_id": request_id, "created_at": utc_now(), "ordinal": len(self.requests()) + 1, "request_sha256": digest(payload), **payload}
        if managed_launch:
            launcher = process_identity(os.getpid())
            if not _complete_identity(launcher):
                raise RegistryError("launcher_identity_unavailable")
            guard = {"schema_version": "host-launch-guard-v1", "execution_id": execution_id,
                     "request_sha256": record["request_sha256"], "launcher_identity": asdict(launcher)}
            record.update(launch_guard=guard, launch_guard_sha256=digest(guard))
        publish_json(self.path(execution_id) / "request.json", record)
        return record, True

    def begin_spawn(self, execution_id: str) -> None:
        """Durably cross the possibly-spawned boundary before calling Popen."""
        with self.launch_lock():
            request = self.request(execution_id)
            guard = request.get("launch_guard")
            if guard is None:
                raise RegistryError("launch_guard_missing")
            expected = ProcessIdentity(**guard["launcher_identity"])
            if compare_identity(expected, process_identity(os.getpid())) != "alive":
                raise RegistryError("launcher_identity_changed")
            if read_record(self.path(execution_id) / "launch-failure.json", optional=True):
                raise RegistryError("launch_already_stopped")
            publish_json(self.path(execution_id) / "spawn-intent.json", {
                "schema_version": "host-spawn-intent-v1", "execution_id": execution_id,
                "launch_guard_sha256": request["launch_guard_sha256"], "recorded_at": utc_now(),
            })

    def _spawn_intent(self, execution_id: str, request: Mapping) -> dict | None:
        intent = read_record(self.path(execution_id) / "spawn-intent.json", optional=True)
        if intent is not None and (
            intent.get("schema_version") != "host-spawn-intent-v1"
            or intent.get("execution_id") != execution_id
            or intent.get("launch_guard_sha256") != request.get("launch_guard_sha256")
        ):
            raise RegistryError("spawn_intent_invalid")
        return intent

    def _unregistered_launch_state(self, execution_id: str) -> tuple[str, str]:
        request = self.request(execution_id)
        guard = request.get("launch_guard")
        if guard is None:
            return "unknown", "worker_not_registered"
        launcher = ProcessIdentity(**guard["launcher_identity"])
        status = compare_identity(launcher, self.inspect_process(launcher.pid))
        # Check the boundary AFTER proving launcher death. Reading it first
        # would race a launcher that publishes intent, spawns, and then exits.
        intent = self._spawn_intent(execution_id, request)
        if intent is not None:
            return "unknown", "spawn_outcome_unverified"
        if status in {"absent", "reused"}:
            # No process can still cross this immutable request's boundary.
            # Records from older launch protocols have no such proof.
            for name in ("worker.json", "launch.json", "preflight.json", "completion.json", "result.json", "heartbeat.json"):
                if read_record(self.path(execution_id) / name, optional=True) is not None:
                    return "unknown", "launch_records_changed"
            return "stopped", "launcher_abandoned_before_spawn"
        if status in {"alive", "exec_changed"}:
            return "launching", "awaiting_spawn"
        return "unknown", "launcher_identity_unavailable"

    def register(self, execution_id: str, *, descriptor_limit: int | None = None) -> dict:
        request = self.request(execution_id)
        if request.get("launch_guard") is not None and self._spawn_intent(execution_id, request) is None:
            raise RegistryError("spawn_intent_missing")
        identity = self.inspect_process(os.getpid())
        if not _complete_identity(identity):
            raise RegistryError("self_identity_unavailable")
        worker = {"schema_version": "host-worker-v1", "execution_id": execution_id, "request_id": request["request_id"], "run_id": request["run_id"], "registered_at": utc_now(), "identity": asdict(identity), "descriptor_limit": descriptor_limit}
        publish_json(self.path(execution_id) / "worker.json", worker)
        return worker

    def record_launch(self, execution_id: str, pid: int) -> None:
        """Parent observation closes the Popen-to-self-registration crash gap."""
        request = self.request(execution_id)
        try:
            identity = self.inspect_process(pid)
        except RegistryError:
            identity = None
        publish_json(self.path(execution_id) / "launch.json", {
            "schema_version": "host-launch-v1", "execution_id": execution_id,
            "request_id": request["request_id"], "observed_at": utc_now(),
            "pid": pid, "identity": asdict(identity) if identity else None,
        })

    def observe(self, execution_id: str, *, now: float | None = None) -> dict:
        observed_at = utc_now()
        try:
            directory = self.path(execution_id)
            worker = read_record(directory / "worker.json", optional=True)
            self_registered = worker is not None
            completion = read_record(directory / "completion.json", optional=True)
            observed_exit = read_record(directory / "exit.json", optional=True)
            if observed_exit and (observed_exit.get("execution_id") != execution_id or isinstance(observed_exit.get("returncode"), bool) or not isinstance(observed_exit.get("returncode"), int)):
                raise RegistryError("observed_exit_invalid")
            result_available = False
            if completion:
                if completion.get("schema_version") != "host-completion-v1" or completion.get("execution_id") != execution_id:
                    raise RegistryError("completion_identity_mismatch")
                if completion.get("result_reference") is not None:
                    if completion.get("result_reference") != "result.json":
                        raise RegistryError("completion_result_reference_invalid")
                    result_path = directory / "result.json"
                    result = read_record(result_path)
                    if hashlib.sha256(result_path.read_bytes()).hexdigest() != completion.get("result_sha256"):
                        raise RegistryError("committed_result_digest_mismatch")
                    result_available = True
            if worker is None:
                launch_failed = read_record(directory / "launch-failure.json", optional=True)
                exited = read_record(directory / "exit.json", optional=True)
                launch = read_record(directory / "launch.json", optional=True)
                if launch and launch.get("schema_version") == "host-launch-v1" and launch.get("execution_id") == execution_id and launch.get("identity"):
                    worker = launch
                else:
                    state, reason = ("stopped", "launch_failed" if launch_failed else "worker_exit_observed") if launch_failed or exited else self._unregistered_launch_state(execution_id)
                    return {"state": "finished" if exited and completion else state, "reason": reason, "execution_id": execution_id, "sampled_at": observed_at,
                            "observed_returncode": observed_exit.get("returncode") if observed_exit else None,
                            "completion": completion, "result_available": result_available}
            if worker.get("execution_id") != execution_id:
                raise RegistryError("worker_identity_mismatch")
            expected = ProcessIdentity(**worker["identity"])
            status = "absent" if observed_exit else compare_identity(expected, self.inspect_process(expected.pid))
            state = {"alive": "running", "absent": "finished" if completion else "stopped", "reused": "finished" if completion else "stopped", "unknown": "unknown", "exec_changed": "unknown"}[status]
            if status in {"alive", "exec_changed"} and not self_registered:
                state = "launching"
            try:
                heartbeat = read_record(directory / "heartbeat.json", optional=True)
            except RegistryError:
                heartbeat = None
            if heartbeat is not None and heartbeat.get("execution_id") != execution_id:
                heartbeat = None
            if status == "alive" and heartbeat:
                age = (time.monotonic() if now is None else now) - float(heartbeat["monotonic"])
                if age > 20 or age < 0:
                    state = "unresponsive"
            return {"state": state, "process_state": status, "execution_id": execution_id, "sampled_at": observed_at, "pid": expected.pid, "heartbeat": heartbeat, "completion": completion, "result_available": result_available, "observed_returncode": observed_exit.get("returncode") if observed_exit else None}
        except (RegistryError, TypeError, ValueError, KeyError):
            return {"state": "unknown", "reason": "worker_records_or_identity_unavailable", "execution_id": execution_id, "sampled_at": observed_at}

    def active(self) -> tuple[dict, ...]:
        return tuple(value for request in self.requests() if (value := self.observe(request["execution_id"]))["state"] in {"unknown", "launching", "running", "unresponsive"})

    def complete(self, execution_id: str, *, status: str, result_path: Path | None, error_code: str | None = None) -> None:
        record = {"schema_version": "host-completion-v1", "execution_id": execution_id, "completed_at": utc_now(), "measurement_status": status, "result_reference": result_path.name if result_path else None, "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path else None, "error_code": error_code}
        publish_json(self.path(execution_id) / "completion.json", record)

    def observe_exit(self, execution_id: str, returncode: int) -> None:
        publish_json(self.path(execution_id) / "exit.json", {"execution_id": execution_id, "observed_at": utc_now(), "returncode": returncode})


class Heartbeat:
    """Responsiveness thread plus independent engine progress/deadline samples."""
    def __init__(self, registry: WorkerRegistry, execution_id: str, *, interval: float = 5):
        self.registry, self.execution_id, self.interval = registry, execution_id, interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._progress: dict = {"control_monotonic": time.monotonic(), "operations": {}}
        self._sequence = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def progress(self, *, operation_id: str | None = None, deadline_monotonic: float | None = None, complete: bool = False) -> None:
        with self._lock:
            self._progress["control_monotonic"] = time.monotonic()
            if operation_id:
                if complete:
                    self._progress["operations"].pop(operation_id, None)
                elif deadline_monotonic is not None:
                    self._progress["operations"][operation_id] = {"deadline_monotonic": deadline_monotonic}

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                self._sequence += 1
                record = {"execution_id": self.execution_id, "sequence": self._sequence, "utc": utc_now(), "wall": time.time(), "monotonic": time.monotonic(), **self._progress}
                try:
                    publish_json(self.registry.path(self.execution_id) / "heartbeat.json", record, replace=True, durable=False)
                except OSError:
                    # Heartbeats are observations, never effect admission.
                    pass
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=max(self.interval, 1))


class WorkerWatchdog:
    """Bounded containment of a verified worker with a demonstrable overdue op."""
    def __init__(self, registry: WorkerRegistry, execution_id: str, *, request_cancel: Callable, signal_process: Callable = os.kill, clock: Callable = time.monotonic, shutdown_grace: float = 30, wake_grace: float = 30, sample_progress: Callable | None = None):
        self.registry, self.execution_id = registry, execution_id
        self.request_cancel, self.signal_process, self.clock = request_cancel, signal_process, clock
        self.shutdown_grace, self.wake_grace = shutdown_grace, wake_grace
        self.sample_progress = sample_progress
        self._last_clock: tuple[float, float] | None = None
        self._wake_until = 0.0

    def tick(self, *, wall: float | None = None) -> str:
        now, wall = self.clock(), time.time() if wall is None else wall
        if self._last_clock:
            dm, dw = now - self._last_clock[0], wall - self._last_clock[1]
            if dm < 0 or dw < 0 or abs(dm - dw) > 5 or dm > 20:
                self._wake_until = now + self.wake_grace
        self._last_clock = now, wall
        sample = self.registry.observe(self.execution_id, now=now)
        if sample.get("process_state") != "alive":
            directory = self.registry.path(self.execution_id)
            if sample.get("process_state") in {"absent", "reused"} and (directory / "containment.json").exists():
                path = directory / "containment-exit.json"
                if not path.exists():
                    publish_json(path, {"execution_id": self.execution_id, "observed_at": utc_now(), "process_state": sample["process_state"], "local_worker_exited": True, "resource_cleanup": "unresolved", "remote_outcomes": "unchanged_unknown"})
            return sample["state"]
        heartbeat = sample.get("heartbeat") or {}
        if self.sample_progress is not None:
            try:
                owner = self.sample_progress()
                progress = owner.get("progress")
            except Exception:
                return "unknown"
            if not isinstance(progress, dict) or progress.get("execution_id") != self.execution_id:
                return "unknown"
            worker = read_record(self.registry.path(self.execution_id) / "worker.json", optional=True)
            if worker is None or not progress.get("boot_id") or progress["boot_id"] != worker["identity"].get("boot_id"):
                return "unknown"
            heartbeat = progress
        operations = heartbeat.get("operations") or {}
        # Deadline data is engine supplied. No deadline -> unknown progress,
        # even if the heartbeat thread itself has stopped.
        overdue_operations = {
            operation_id: {
                "deadline_monotonic": float(operation["deadline_monotonic"]),
                "seconds_past_deadline": now - float(operation["deadline_monotonic"]),
            }
            for operation_id, operation in operations.items()
            if isinstance(operation, dict)
            and isinstance(operation.get("deadline_monotonic"), (int, float))
            and now > float(operation["deadline_monotonic"]) + self.shutdown_grace
        }
        if not overdue_operations or now < self._wake_until:
            return sample["state"]
        directory = self.registry.path(self.execution_id)
        history = read_record(directory / "containment.json", optional=True)
        if history is None:
            # Preserve the exact decision sample before cancellation clears
            # active progress. Later inspection must not guess which operation
            # triggered containment from the worker's final heartbeat.
            publish_json(directory / "containment.json", {
                "execution_id": self.execution_id,
                "started_monotonic": now,
                "started_at": utc_now(),
                "reason": "operation_deadline_exceeded",
                "remote_outcomes": "unchanged_unknown",
                "cleanup": "unresolved",
                "shutdown_grace_seconds": self.shutdown_grace,
                "progress_source": "engine_ownership" if self.sample_progress is not None else "host_heartbeat",
                "progress_sample_monotonic": heartbeat.get("monotonic"),
                "progress_sampled_at": heartbeat.get("sampled_at", heartbeat.get("utc")),
                "overdue_operations": overdue_operations,
            })
            self.request_cancel()
            return "cancelling"
        elapsed = now - float(history["started_monotonic"])
        if elapsed < self.shutdown_grace:
            return "cancelling"
        # Recheck exact creation/executable/boot identity immediately before
        # every signal. Never use a process group or signal shared Chrome.
        verified = self.registry.observe(self.execution_id, now=now)
        if verified.get("process_state") != "alive":
            return verified["state"]
        # A restart or long sleep cannot skip TERM. Only a durable receipt
        # that TERM was delivered starts the separate forced-exit grace.
        try:
            terminated = read_record(directory / "terminate-dispatched.json", optional=True)
            if terminated is not None:
                if terminated.get("execution_id") != self.execution_id or not isinstance(terminated.get("monotonic"), (int, float)):
                    return "unknown"
                term_elapsed = now - float(terminated["monotonic"])
                if term_elapsed < 0:
                    return "unknown"
                if term_elapsed < self.shutdown_grace:
                    return "terminate"
            stage = "terminate" if terminated is None else "kill"
            marker = directory / (stage + ".json")
            admitted = read_record(marker, optional=True)
            if admitted is None:
                publish_json(marker, {"execution_id": self.execution_id, "at": utc_now(), "monotonic": now, "signal": stage, "cleanup": "unresolved"})
            elif admitted.get("execution_id") != self.execution_id or admitted.get("signal") != stage:
                return "unknown"
            receipt = directory / (stage + "-dispatched.json")
            dispatched = read_record(receipt, optional=True)
            if dispatched is None:
                # Recheck again after filesystem admission, immediately before
                # signalling. An admitted signal with no receipt may be safely
                # reissued to the same exact local process after restart.
                verified = self.registry.observe(self.execution_id, now=now)
                if verified.get("process_state") != "alive":
                    return verified["state"]
                self.signal_process(verified["pid"], signal.SIGTERM if stage == "terminate" else signal.SIGKILL)
                publish_json(receipt, {"execution_id": self.execution_id, "at": utc_now(), "monotonic": self.clock(), "signal": stage})
            elif dispatched.get("execution_id") != self.execution_id:
                return "unknown"
            return stage
        except (RegistryError, OSError, TypeError, ValueError):
            return "unknown"
