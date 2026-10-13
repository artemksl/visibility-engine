"""Bounded private JSON files and atomic publication, independent of any UI."""
import json
import os
from pathlib import Path
import tempfile
from platform_runtime.security import is_owned_private, open_no_follow, protect_private
from platform_runtime.security import private_directory as _private_directory
from platform_runtime.storage import publish_complete

MAX_JSON_BYTES = 8 * 1024 * 1024


class PrivateFileTooLarge(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result

def read_json(path, limit=65536):
    """Metadata only. Reject links, loose permissions and concurrent replacements."""
    fd = open_no_follow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not is_owned_private(path, fd=stream.fileno()) or before.st_nlink != 1
                or not 0 < before.st_size <= limit):
            raise ValueError()
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                             item.st_mtime_ns, item.st_ctime_ns)
    if identity(before) != identity(after) or len(raw) != before.st_size:
        raise ValueError()
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))

def private_directory(path):
    _private_directory(path)

def atomic_json(path, value, *, exclusive=False):
    raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_JSON_BYTES:
        raise PrivateFileTooLarge("Private JSON exceeds its size limit.")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".registry-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            protect_private(temporary)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        publish_complete(temporary, path, exclusive=exclusive)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)

def _save(path: Path, data: dict) -> None:
    """Replace small non-secret metadata atomically and durably."""
    fd, temporary = tempfile.mkstemp(prefix=".binding-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            protect_private(temporary)
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        publish_complete(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
