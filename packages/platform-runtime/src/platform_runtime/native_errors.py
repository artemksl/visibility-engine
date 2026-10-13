"""Expose native Windows failures through the application's OSError contract."""
import errno
from contextlib import contextmanager


def os_error(error, *, filename=None):
    """Translate pywintypes.error, which is not an OSError, without losing its code."""
    if isinstance(error, OSError):
        return error
    code = getattr(error, "winerror", None)
    number = {
        2: errno.ENOENT, 3: errno.ENOENT,
        5: errno.EACCES, 6: errno.EBADF, 17: errno.EXDEV,
        32: errno.EACCES, 33: errno.EAGAIN,
        80: errno.EEXIST, 183: errno.EEXIST,
    }.get(code, errno.EIO)
    converted = OSError(number, getattr(error, "strerror", None) or str(error), filename)
    converted.winerror = code
    return converted


@contextmanager
def windows_errors(*, filename=None):
    """Keep native Windows errors compatible with the shared caller contract."""
    import pywintypes
    try:
        yield
    except pywintypes.error as error:
        raise os_error(error, filename=filename) from error
