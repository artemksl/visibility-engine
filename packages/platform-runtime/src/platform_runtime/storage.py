"""Complete-file publication; OS-specific durability stays at this boundary."""
import errno
import os
from pathlib import Path
import sys
import uuid

from .native_errors import os_error


def sync_file(fd, *, full=True):
    os.fsync(fd)
    if full and sys.platform == "darwin":
        import fcntl
        fcntl.fcntl(fd, getattr(fcntl, "F_FULLFSYNC", 51))


def sync_directory(path):
    if sys.platform == "win32":
        # Windows cannot perform POSIX directory fsync. Mutations use the
        # explicit write-through publication/removal operations below instead.
        raise OSError(errno.ENOTSUP, "use write-through publication on Windows")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_path(path):
    # Windows FlushFileBuffers requires a writable handle; retain the original
    # read-only descriptor on POSIX.
    fd = os.open(path, os.O_RDWR if sys.platform == "win32" else os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _windows_move(source, destination, *, replace, durable):
    import win32file
    # Never permit copy/delete cross-volume fallback: partial files must not be
    # observable, and exclusive publication must not overwrite an existing file.
    if os.stat(source).st_dev != os.stat(Path(destination).parent).st_dev:
        raise OSError(errno.EXDEV, "publication requires one filesystem")
    flags = (1 if replace else 0) | (8 if durable else 0)
    try:
        win32file.MoveFileEx(str(source), str(destination), flags)
    except Exception as exc:
        if getattr(exc, "winerror", None) is not None:
            raise os_error(exc, filename=str(destination)) from exc
        raise


def publish_complete(temporary, path, *, exclusive=False, durable=True, directory_sync=None):
    """Publish an already-flushed complete file, preserving no-overwrite intent."""
    if sys.platform == "win32":
        _windows_move(temporary, path, replace=not exclusive, durable=durable)
        return
    if exclusive:
        os.link(temporary, path)
        os.unlink(temporary)
    else:
        os.replace(temporary, path)
    if durable:
        (directory_sync or sync_directory)(Path(path).parent)
        if Path(temporary).parent != Path(path).parent:
            (directory_sync or sync_directory)(Path(temporary).parent)


def remove_complete(path, *, directory_sync=None):
    """Durably remove the original name before releasing an ownership marker."""
    path = Path(path)
    if sys.platform != "win32":
        path.unlink()
        (directory_sync or sync_directory)(path.parent)
        return
    # A same-directory write-through rename durably removes the authoritative
    # name. Leftover quarantine bytes after a crash are never a live marker.
    discarded = path.with_name(f".{path.name}.removed-{uuid.uuid4().hex}")
    _windows_move(path, discarded, replace=False, durable=True)
    discarded.unlink()
