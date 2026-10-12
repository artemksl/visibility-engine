"""Complete-file publication for authorization, effects and revisions.

``full_sync=True`` (the default) asks the storage device to flush its own
cache after the file's data reached the kernel (``F_FULLFSYNC`` on macOS);
it is required for every record that later decides whether a send may be
repeated: journal records, send attempts, evidence, results, ownership and
control records. ``full_sync=False`` keeps the ordinary ``fsync`` and is used
only for files whose loss or staleness after a power failure is harmless
because they are derived from those records: the journal head, the
execution heartbeat and the mutable progress projections.
"""
from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def sync_file(fd: int) -> None:
    """Flush the file to the storage device itself (F_FULLFSYNC on macOS)."""
    os.fsync(fd)
    if sys.platform == "darwin":
        import fcntl
        # F_FULLFSYNC asks the device to flush its cache, beyond fsync.
        fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))


def sync_file_to_kernel(fd: int) -> None:
    """Ordinary fsync only; enough for files derived from fully synced records."""
    os.fsync(fd)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(fd)


def publish_bytes(path: Path | str, data: bytes, *, exclusive: bool = False, full_sync: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if full_sync:
                sync_file(stream.fileno())
            else:
                sync_file_to_kernel(stream.fileno())
        if exclusive:
            # The destination is never observable with incomplete contents.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def publish_json(path: Path | str, value: Any, *, exclusive: bool = False, full_sync: bool = True) -> Path:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # Serialization/validation completes before opening a publication file.
    json.loads(payload)
    return publish_bytes(path, (payload + "\n").encode(), exclusive=exclusive, full_sync=full_sync)
