"""Durable delivery identities for the control UI's Start requests.

The receipt is reserved before preparation and never re-bound. A process crash
can therefore leave an interrupted request, but redelivery cannot launch a
second job. These records are independent of the in-memory web server.
"""

from __future__ import annotations

import contextlib
from platform_runtime import file_locks as fcntl
import hashlib
import re
from pathlib import Path

from visibility_panel.execution.worker_registry import RegistryError, publish_json, read_record


class StartRequestConflict(RuntimeError):
    """A Start request cannot safely be replayed or replaced."""


@contextlib.contextmanager
def locked_start_receipt(run_root: Path, request_id: str):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
        raise ValueError("Start request_id must contain 8–128 letters, digits, underscores or hyphens")
    directory = Path(run_root) / ".start-requests"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(request_id.encode()).hexdigest()
    # A stable lock inode also serializes separate server instances. Never
    # unlink it: doing so could admit a second holder during a retry.
    with (directory / f"{key}.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield directory / f"{key}.json"
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_start_receipt(path: Path) -> dict | None:
    try:
        return read_record(path, optional=True)
    except RegistryError as exc:
        raise StartRequestConflict("start_receipt_unreadable: Start outcome requires review; do not create a replacement request") from exc


def reserve_start_receipt(path: Path, *, request_id: str, fingerprint: str, job_id: str) -> dict:
    receipt = {"schema_version": 1, "request_id": request_id,
               "fingerprint": fingerprint, "job_id": job_id}
    publish_json(path, receipt)
    return receipt


def rejected_start_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.rejected.json")
