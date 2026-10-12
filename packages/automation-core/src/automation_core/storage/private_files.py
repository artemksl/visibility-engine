"""Bounded private JSON files and atomic publication, independent of any UI."""
import json
import os
from pathlib import Path
import stat
import tempfile

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
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_nlink != 1
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
    path = Path(path)
    if path != path.resolve():
        raise ValueError()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError()

def atomic_json(path, value, *, exclusive=False):
    raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_JSON_BYTES:
        raise PrivateFileTooLarge("Private JSON exceeds its size limit.")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".registry-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)

import tempfile
def _save(path: Path, data: dict) -> None:
    """Replace small non-secret metadata atomically and durably."""
    fd, temporary = tempfile.mkstemp(prefix=".binding-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
