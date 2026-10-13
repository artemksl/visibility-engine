"""One descriptor-based lock contract on POSIX and Windows.

POSIX delegates to the original flock implementation. Windows uses LockFileEx
on the entire file: real shared/exclusive locks, released when the handle closes.
Lock failures are never converted into permission to proceed without a lease.
"""
import os
import sys
import threading

from .native_errors import os_error

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
if sys.platform != "win32":
    import fcntl as _posix
    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = (_posix.LOCK_SH, _posix.LOCK_EX,
                                        _posix.LOCK_NB, _posix.LOCK_UN)

# Tracking is needed only for conversion/idempotence, never for lock authority.
_held = {}
_guard = threading.RLock()


def _windows_flock(fd, operation):
    import msvcrt
    import pywintypes
    import win32file
    handle = msvcrt.get_osfhandle(fd)
    info = os.fstat(fd)
    signature = (handle, info.st_dev, info.st_ino)
    with _guard:
        previous = _held.get(fd)
        if previous and previous[0] != signature:
            _held.pop(fd, None)  # descriptor was closed and reused
            previous = None
        mode = operation & (LOCK_SH | LOCK_EX)
        if mode == (LOCK_SH | LOCK_EX) or (not mode and not operation & LOCK_UN):
            raise ValueError("a shared or exclusive lock is required")
        if previous:
            try:
                win32file.UnlockFileEx(handle, 0xffffffff, 0xffffffff, previous[2])
            except pywintypes.error as exc:
                if exc.winerror != 158:  # ERROR_NOT_LOCKED; a closed fd was reused
                    raise os_error(exc) from exc
                previous = None
            _held.pop(fd, None)
        if operation & LOCK_UN:
            return
    overlap = pywintypes.OVERLAPPED()
    flags = (2 if mode == LOCK_EX else 0) | (1 if operation & LOCK_NB else 0)
    try:
        # Call outside the bookkeeping mutex: another thread may release the
        # descriptor that a blocking acquisition is waiting for.
        win32file.LockFileEx(handle, flags, 0xffffffff, 0xffffffff, overlap)
    except pywintypes.error as exc:
        if previous:
            # A failed promotion must not turn a caller's retained shared
            # lease into an unlocked descriptor. Restore before reporting it.
            try:
                win32file.LockFileEx(handle, 2 if previous[1] == LOCK_EX else 0,
                                    0xffffffff, 0xffffffff, previous[2])
            except pywintypes.error as restoration:
                raise os_error(restoration) from restoration
            with _guard:
                _held[fd] = previous
        raise os_error(exc) from exc
    with _guard:
        _held[fd] = (signature, mode, overlap)


def flock(fd, operation):
    if sys.platform == "win32":
        return _windows_flock(fd, operation)
    return _posix.flock(fd, operation)


def fcntl(fd, command, *args):
    """Retain the macOS F_FULLFSYNC hook for existing POSIX callers."""
    if sys.platform == "win32":
        raise OSError("fcntl commands are unavailable on Windows")
    return _posix.fcntl(fd, command, *args)
