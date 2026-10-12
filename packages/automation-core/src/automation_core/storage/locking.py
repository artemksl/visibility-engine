"""Process-local ownership backed by the existing advisory file-lock namespace.

Only the outer application/session owner acquires these locks. Nested execution
and timing contexts borrow the yielded lease; they never renew or release it.
Different resolved state roots intentionally remain different namespaces.
"""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import threading
from typing import Iterator

from automation_core.storage.models import NeedsAttention
from automation_core.adapters.octo import normalize_profile_id


@dataclass(repr=False)
class ResourceLease:
    root: Path
    keys: tuple[str, ...]
    owner_pid: int
    owner_thread: int
    active: bool = True

    def assert_owned(self) -> None:
        if (not self.active or self.owner_pid != os.getpid()
                or self.owner_thread != threading.get_ident()):
            raise NeedsAttention("The current execution does not own these resource locks.")


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    """Keep the legacy key, nonblocking behavior, error and lock-file inode."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NeedsAttention("Another run is using this browser profile or account.") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def _resource_keys(state_root: Path, keys: tuple[str, ...]) -> Iterator[ResourceLease]:
    root = Path(state_root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    locks = root / "locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    ordered = tuple(sorted(keys))
    with ExitStack() as stack:
        for key in ordered:
            digest = hashlib.sha256(key.encode()).hexdigest()
            stack.enter_context(_lock(locks / (digest + ".lock")))
        lease = ResourceLease(root, ordered, os.getpid(), threading.get_ident())
        try:
            yield lease
        finally:
            lease.active = False


def account_lock_key(account_id: str) -> str:
    """Keep legacy numeric keys; qualify other websites' opaque identities."""
    if (not isinstance(account_id, str)
            or re.fullmatch(r'(?:[0-9]+|[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,255})',
                            account_id) is None):
        raise NeedsAttention("A numeric or site-qualified account ID is required.")
    return "account:" + account_id


@contextmanager
def account_lock(state_root: Path, account_id: str) -> Iterator[ResourceLease]:
    """Capture private inputs under the same nonreentrant account namespace."""
    with _resource_keys(state_root, (account_lock_key(account_id),)) as lease:
        yield lease


@contextmanager
def resource_lock(state_root: Path, profile_id: str,
                  account_id: str | None = None) -> Iterator[ResourceLease]:
    profile_id = normalize_profile_id(profile_id)
    keys = ["profile:" + profile_id]
    if account_id is not None:
        keys.append(account_lock_key(account_id))
    with _resource_keys(state_root, tuple(keys)) as lease:
        yield lease


@contextmanager
def profile_lock(state_root: Path, profile_id: str) -> Iterator[ResourceLease]:
    """Lock a profile without constructing or changing an account binding."""
    with resource_lock(state_root, profile_id) as lease:
        yield lease
