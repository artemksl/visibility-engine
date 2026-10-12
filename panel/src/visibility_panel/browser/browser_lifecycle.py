"""Cross-process ownership for the persistent Chrome lifecycle.

Provider/profile leases protect individual Chrome identities.  This lock protects
the one destructive resource shared by every identity on a CDP endpoint: the
persistent Chrome process itself.  Browser-driving runs hold a shared lease;
code that may quit or launch Chrome must hold the exclusive form.

The implementation is intentionally stdlib-only so the shell launchers can use
this file as a small wrapper before they touch Chrome.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable, Iterator, Mapping, Optional, Sequence

try:  # Unix/macOS production path.
    import fcntl
except ImportError:  # pragma: no cover - persistent Chrome is not run on Windows.
    fcntl = None


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
LOCK_PATH_ENV = "CITEUP_BROWSER_LIFECYCLE_LOCK_PATH"
LOCK_ROOT_ENV = "CITEUP_BROWSER_LIFECYCLE_LOCK_ROOT"
LOCK_HELD_ENV = "CITEUP_BROWSER_LIFECYCLE_LOCK_HELD"
DEFAULT_LOCK_ROOT = Path(tempfile.gettempdir()) / "clinic-geo-browser-lifecycle"


class BrowserLifecycleLockError(RuntimeError):
    """Raised when the persistent-browser lifecycle cannot be leased safely."""


def browser_lifecycle_lock_path(
    environment: Optional[Mapping[str, object]] = None,
    *,
    cdp_url: Optional[str] = None,
) -> Path:
    """Return the stable lock path for one user's CDP endpoint.

    CDP endpoints, rather than profile manifests, define the destructive
    boundary: two jobs attached to port 9222 share one Chrome process even when
    their profile subsets differ.
    """

    values = dict(os.environ)
    if environment:
        values.update({str(key): str(value) for key, value in environment.items()})
    explicit = str(values.get(LOCK_PATH_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    root_value = str(values.get(LOCK_ROOT_ENV) or "").strip()
    root = Path(root_value).expanduser() if root_value else DEFAULT_LOCK_ROOT
    endpoint = str(cdp_url or values.get("CDP_URL") or DEFAULT_CDP_URL).strip().rstrip("/")
    try:
        user_key = str(os.getuid())
    except AttributeError:  # pragma: no cover - Windows fallback for path stability.
        user_key = str(values.get("USERNAME") or values.get("USER") or "unknown")
    digest = hashlib.sha256(f"{user_key}\0{endpoint}".encode("utf-8")).hexdigest()[:24]
    return (root / f"{digest}.lock").resolve()


class BrowserLifecycleLease:
    """One shared or exclusive advisory lock on the persistent browser."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        self._handle = None
        self._mode = "released"
        self._exclusive_depth = 0

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active(self) -> bool:
        return self._handle is not None and self._mode != "released"

    def _require_fcntl(self) -> None:
        if fcntl is None:  # pragma: no cover - see import guard above.
            raise BrowserLifecycleLockError(
                "Persistent Chrome lifecycle locking requires Unix advisory locks."
            )

    def _open(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")

    def acquire_shared(self, *, blocking: bool = True) -> None:
        self._require_fcntl()
        if self.active:
            raise BrowserLifecycleLockError("Browser lifecycle lease is already active")
        self._open()
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise BrowserLifecycleLockError("Chrome is being restarted by another job; wait for it to finish") from exc
        except BaseException:
            self._handle.close()
            self._handle = None
            raise
        self._mode = "shared"

    def acquire_exclusive(self, *, on_wait: Optional[Callable[[], object]] = None) -> None:
        self._require_fcntl()
        if self.active:
            raise BrowserLifecycleLockError("Browser lifecycle lease is already active")
        self._open()
        try:
            try:
                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                if on_wait is not None:
                    on_wait()
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._handle.close()
            self._handle = None
            raise
        self._mode = "exclusive"

    @contextmanager
    def exclusive(
        self,
        *,
        on_wait: Optional[Callable[[], object]] = None,
        blocking: bool = True,
    ) -> Iterator["BrowserLifecycleLease"]:
        """Temporarily upgrade this run's shared lease to exclusive.

        A failed non-blocking conversion releases this reader before waiting.
        That prevents two collectors which both discover an unready browser
        from deadlocking while each retains a shared lock.  The collector is
        still in preflight at that point and has launched no provider work.
        """

        self._require_fcntl()
        if self._mode == "exclusive":
            self._exclusive_depth += 1
            try:
                yield self
            finally:
                self._exclusive_depth -= 1
            return
        if self._mode != "shared" or self._handle is None:
            raise BrowserLifecycleLockError(
                "Only an active shared browser lifecycle lease can be upgraded"
            )

        try:
            fcntl.flock(
                self._handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            if not blocking:
                raise BrowserLifecycleLockError("Chrome is in use by another job; wait for it to finish before starting a new measurement")
            if on_wait is not None:
                on_wait()
            # Advisory-lock conversion is not guaranteed to be atomic. Release
            # explicitly, mark the transition, then wait as a writer. Competing
            # upgraders do the same, so one can always make progress.
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._mode = "waiting_exclusive"
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                # Preserve the caller's active-run protection if waiting is
                # interrupted before exclusive ownership is obtained.
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_SH)
                self._mode = "shared"
                raise
        self._mode = "exclusive"
        self._exclusive_depth = 1
        try:
            yield self
        finally:
            self._exclusive_depth = 0
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_SH)
            self._mode = "shared"

    def release(self) -> None:
        if self._handle is None:
            self._mode = "released"
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._mode = "released"
            self._exclusive_depth = 0


_CURRENT_LEASE: ContextVar[Optional[BrowserLifecycleLease]] = ContextVar(
    "visibility_panel_browser_lifecycle_lease",
    default=None,
)


def current_browser_lifecycle_lease() -> Optional[BrowserLifecycleLease]:
    """Return the lease owned by the current collection context, if any."""

    lease = _CURRENT_LEASE.get()
    return lease if lease is not None and lease.active else None


@contextmanager
def shared_browser_lifecycle(
    *,
    path: Path | str | None = None,
    environment: Optional[Mapping[str, object]] = None,
    blocking: bool = True,
) -> Iterator[BrowserLifecycleLease]:
    """Hold an active-run/shared browser lease for a collection scope."""

    resolved = Path(path).resolve() if path is not None else browser_lifecycle_lock_path(environment)
    current = current_browser_lifecycle_lease()
    if current is not None and current.path == resolved:
        yield current
        return
    lease = BrowserLifecycleLease(resolved)
    lease.acquire_shared(blocking=blocking)
    token = _CURRENT_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_LEASE.reset(token)
        lease.release()


@contextmanager
def exclusive_browser_lifecycle(
    *,
    path: Path | str | None = None,
    environment: Optional[Mapping[str, object]] = None,
    on_wait: Optional[Callable[[], object]] = None,
) -> Iterator[BrowserLifecycleLease]:
    """Hold exclusive ownership before quitting or launching shared Chrome."""

    current = current_browser_lifecycle_lease()
    if current is not None and (path is None or current.path == Path(path).resolve()):
        with current.exclusive(on_wait=on_wait):
            yield current
        return

    resolved = Path(path).resolve() if path is not None else browser_lifecycle_lock_path(environment)
    lease = BrowserLifecycleLease(resolved)
    lease.acquire_exclusive(on_wait=on_wait)
    token = _CURRENT_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_LEASE.reset(token)
        lease.release()


def run_with_exclusive_browser_lifecycle(command: Sequence[str]) -> int:
    """Run a destructive launcher while this wrapper retains exclusive ownership."""

    if not command:
        raise BrowserLifecycleLockError("A launcher command is required")
    path = browser_lifecycle_lock_path()
    with exclusive_browser_lifecycle(path=path):
        child_env = dict(os.environ)
        child_env[LOCK_HELD_ENV] = "1"
        child_env[LOCK_PATH_ENV] = str(path)
        return subprocess.run(list(command), env=child_env, check=False).returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--run-exclusive":
        args.pop(0)
    if args and args[0] == "--":
        args.pop(0)
    if not args:
        print("usage: browser_lifecycle.py --run-exclusive <launcher> [args...]", file=sys.stderr)
        return 2
    return run_with_exclusive_browser_lifecycle(args)


if __name__ == "__main__":
    raise SystemExit(main())
