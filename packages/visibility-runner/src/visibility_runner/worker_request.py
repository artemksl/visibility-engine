"""Versioned subprocess-worker request contract.

The public runner is in-process and synchronous.  Host applications that need
process isolation can persist this small request and invoke the package with
the current Python interpreter.  The request binds execution to both an exact
runtime configuration and the immutable plan hash already stored for the run.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

from .config import RuntimeConfig
from .errors import ConfigurationError, IntegrityError
from .storage.filesystem import atomic_write_json, read_json
from .contract_primitives import WORKER_REQUEST_SCHEMA_VERSION


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkerOperation(str, Enum):
    RUN_PACKET = "run_packet"
    RESUME = "resume"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class WorkerRequest:
    """One explicitly authorized worker action."""

    runtime: RuntimeConfig
    operation: WorkerOperation
    run_id: str
    plan_sha256: str
    packet_id: Optional[str] = None
    schema_version: str = WORKER_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeConfig):
            raise ConfigurationError("worker runtime must be a RuntimeConfig")
        try:
            operation = (
                self.operation
                if isinstance(self.operation, WorkerOperation)
                else WorkerOperation(str(self.operation))
            )
        except ValueError as exc:
            raise ConfigurationError(
                f"unsupported worker operation: {self.operation!r}"
            ) from exc
        run_id = str(self.run_id or "").strip()
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ConfigurationError("worker run_id must be one safe path segment")
        digest = str(self.plan_sha256 or "").strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise ConfigurationError(
                "worker plan_sha256 must be exactly 64 hexadecimal characters"
            )
        packet_id = str(self.packet_id or "").strip() or None
        if (
            packet_id is not None
            and (Path(packet_id).name != packet_id or packet_id in {".", ".."})
        ):
            raise ConfigurationError(
                "worker packet_id must be one safe path segment"
            )
        if operation is WorkerOperation.RUN_PACKET and packet_id is None:
            raise ConfigurationError("run_packet worker requests require packet_id")
        if operation is WorkerOperation.RESUME and packet_id is not None:
            raise ConfigurationError("resume worker requests cannot name packet_id")
        if self.schema_version != WORKER_REQUEST_SCHEMA_VERSION:
            raise ConfigurationError(
                f"unsupported worker request schema: {self.schema_version!r}"
            )
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "plan_sha256", digest)
        object.__setattr__(self, "packet_id", packet_id)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime.to_mapping(),
            "operation": self.operation.value,
            "run_id": self.run_id,
            "plan_sha256": self.plan_sha256,
            "packet_id": self.packet_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WorkerRequest":
        if not isinstance(value, Mapping):
            raise ConfigurationError("worker request must be a JSON object")
        allowed = {
            "schema_version",
            "runtime",
            "operation",
            "run_id",
            "plan_sha256",
            "packet_id",
        }
        unknown = sorted(str(key) for key in value if str(key) not in allowed)
        if unknown:
            raise ConfigurationError(
                "worker request contains unsupported key(s): "
                + ", ".join(unknown)
            )
        runtime = value.get("runtime")
        if not isinstance(runtime, Mapping):
            raise ConfigurationError("worker request runtime must be an object")
        return cls(
            runtime=RuntimeConfig.from_mapping(runtime),
            operation=value.get("operation"),
            run_id=value.get("run_id"),
            plan_sha256=value.get("plan_sha256"),
            packet_id=value.get("packet_id"),
            schema_version=str(value.get("schema_version") or ""),
        )


def write_worker_request(
    path: Path | str,
    request: WorkerRequest,
    *,
    exclusive: bool = True,
) -> Path:
    """Persist one request atomically; exclusive creation is the safe default."""

    if not isinstance(request, WorkerRequest):
        raise TypeError("request must be a WorkerRequest")
    return atomic_write_json(
        path,
        request.to_mapping(),
        exclusive=exclusive,
        canonical=True,
    )


def load_worker_request(path: Path | str) -> WorkerRequest:
    value = read_json(path)
    if not isinstance(value, Mapping):
        raise ConfigurationError("worker request must be a JSON object")
    return WorkerRequest.from_mapping(value)


def worker_command(path: Path | str) -> tuple[str, ...]:
    """Return the required current-interpreter module invocation."""

    return (
        sys.executable,
        "-m",
        "visibility_runner._worker_main",
        "--request",
        str(Path(path)),
    )


def execute_worker_request(request: WorkerRequest):
    """Execute an already validated request without any implicit operation."""

    from .execution.manage_measurements import Runner

    runner = Runner(request.runtime)
    inspected = runner.inspect(request.run_id)
    if inspected.plan.plan_sha256 != request.plan_sha256:
        raise IntegrityError(
            "worker request plan hash differs from the persisted immutable plan"
        )
    if request.operation is WorkerOperation.RUN_PACKET:
        assert request.packet_id is not None
        return runner.run_packet(request.run_id, request.packet_id)
    return runner.resume(request.run_id)


__all__ = [
    "WORKER_REQUEST_SCHEMA_VERSION",
    "WorkerOperation",
    "WorkerRequest",
    "execute_worker_request",
    "load_worker_request",
    "worker_command",
    "write_worker_request",
]
