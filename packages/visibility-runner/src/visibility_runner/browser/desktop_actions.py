"""Run-scoped FIFO access to the one physical desktop.

The native backend is deliberately injected: importing this module never imports
Atomacos, PyAutoGUI, or opens a browser.  Async preparation (for example, selecting
an owned tab) and the following synchronous native action share one advisory
process lock.  Every participating runner on this user account uses the same lock.

A Python native call cannot be forcibly stopped.  If its caller times out or is
cancelled, its cancellation callback is signalled and this queue is quarantined.
The single daemon worker retains the desktop lease until the call actually exits;
no replacement worker is started.  ``aclose`` is bounded and reports incomplete
cleanup, allowing the owner to preserve any tab that the outstanding call uses.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
from platform_runtime import file_locks as fcntl
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Awaitable, Callable, Hashable

from platform_runtime.paths import user_cache_dir


class DesktopActionError(RuntimeError):
    """A desktop request cannot be safely dispatched."""


class DesktopActionClosed(DesktopActionError):
    """The run's desktop stream has already closed."""


class DesktopActionDuplicate(DesktopActionError):
    """An identical job is already queued or running."""


class DesktopActionQuarantined(DesktopActionError):
    """A previous action has not confirmed safe completion."""


class DesktopActionTimeout(DesktopActionError, TimeoutError):
    """The request's total queue/preparation/action deadline expired."""


def default_desktop_lock_path() -> Path:
    """One stable user lock, independent of run ID, profile, and repository."""
    return user_cache_dir() / "visibility-runner" / "desktop-actions.lock"


class _Lease:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(path, flags, 0o600)
        self._guard = threading.Lock()

    def try_acquire(self) -> bool:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def release(self) -> None:
        # Ownership may pass from the event-loop thread to the native thread.
        with self._guard:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@dataclass(eq=False)
class _Job:
    action: Callable[[Any], Any]
    prepare: Callable[[], Awaitable[Any]] | None
    cancel_action: Callable[[], None] | None
    deadline: float
    result: asyncio.Future
    key: Hashable | None
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    native_cancelled: threading.Event = field(default_factory=threading.Event)
    phase: str = "queued"
    cancel_notified: bool = False


class _Abandoned(Exception):
    pass


class _NativeThread:
    """A single lazy native session, created/used/closed on its own thread."""

    def __init__(self, factory, close_session, lock_path, lock_poll_s):
        self._factory = factory
        self._close_session = close_session
        self._lock_path = lock_path
        self._lock_poll_s = lock_poll_s
        self._requests: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.busy = threading.Event()
        self.closing = threading.Event()
        self.done: Future = Future()
        self.shutdown_error: BaseException | None = None
        # Intentionally retain the descriptor if native cleanup is unconfirmed.
        # Only process exit (or explicit future recovery) releases that lease.
        self.quarantined_lease: _Lease | None = None
        self.thread = threading.Thread(target=self._run, name="visibility-desktop-actions", daemon=True)
        self.thread.start()

    def submit(self, job: _Job, lease: _Lease) -> Future:
        if self._stop.is_set():
            raise DesktopActionClosed("native desktop worker is closing")
        result: Future = Future()
        self.busy.set()
        self._requests.put((job, lease, result))
        return result

    def stop(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self.closing.set()
            self._requests.put(None)

    def _teardown(self, session, lease=None) -> None:
        """Native cleanup may release buttons and therefore also owns the lease."""
        self.closing.set()
        try:
            if lease is None:
                lease = _Lease(self._lock_path)
                while not lease.try_acquire():
                    time.sleep(self._lock_poll_s)
            if self._close_session is not None:
                self._close_session(session)
            else:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
        except BaseException as exc:
            self.shutdown_error = exc
            self.quarantined_lease = lease
        else:
            lease.release()

    def _run(self) -> None:
        session = None
        initialized = False
        teardown_lease = None
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    break
                job, lease, result = request
                try:
                    if job.native_cancelled.is_set():
                        raise _Abandoned()
                    if not initialized:
                        session = self._factory()
                        initialized = True
                    if job.native_cancelled.is_set():
                        raise _Abandoned()
                    value = job.action(session)
                except BaseException as exc:
                    self._stop.set()
                    if initialized:
                        # A failed action may have left a mouse button down.
                        # Complete native teardown before another run can focus.
                        self._teardown(session, lease)
                        initialized = False
                        lease = None
                    # A backend must not send SystemExit/KeyboardInterrupt into
                    # the event loop and strand the run's remaining requests.
                    if not isinstance(exc, Exception):
                        error = DesktopActionError("native desktop action terminated unexpectedly")
                        error.__cause__ = exc
                        exc = error
                    # This future is private and never cancelled by its caller.
                    result.set_exception(exc)
                else:
                    result.set_result(value)
                finally:
                    if lease is not None:
                        if self._stop.is_set() and initialized:
                            self.closing.set()
                            teardown_lease = lease
                        else:
                            lease.release()
                    self.busy.clear()
                if self._stop.is_set():
                    break
        finally:
            if initialized:
                self._teardown(session, teardown_lease)
            self.closing.clear()
            self.done.set_result(None)


class DesktopActionQueue:
    """Serialize desktop work for one run and across cooperating processes.

    ``timeout_s`` on ``execute`` includes FIFO waiting, global-lock waiting,
    preparation, backend startup, and the action.  ``prepare`` runs only after the
    desktop lease is acquired.  The synchronous action receives the reusable
    session created by ``session_factory`` on the native thread.  The optional
    cancellation callback must be nonblocking and thread-safe; native actions
    should also respect their own deadline/cancellation token before every input.

    Keys reject concurrent duplicate requests; they do not cache old results.
    Session teardown also owns the desktop lease because it may release buttons.
    If teardown fails, the lease is retained until the owning process exits.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        close_session: Callable[[Any], None] | None = None,
        lock_path: str | Path | None = None,
        shutdown_timeout_s: float = 2.0,
        lock_poll_s: float = 0.025,
    ):
        self._session_factory = session_factory
        self._close_session = close_session
        self.lock_path = Path(lock_path) if lock_path is not None else default_desktop_lock_path()
        self._shutdown_timeout_s = self._positive(shutdown_timeout_s, "shutdown_timeout_s")
        self._lock_poll_s = self._positive(lock_poll_s, "lock_poll_s")
        self._queue: asyncio.Queue = asyncio.Queue()
        self._jobs: set[_Job] = set()
        self._keys: set[Hashable] = set()
        self._worker: asyncio.Task | None = None
        self._native: _NativeThread | None = None
        self._prepare_task: asyncio.Task | None = None
        self._current: _Job | None = None
        self._loop = None
        self._closed = False
        self._poison_reason: str | None = None

    @staticmethod
    def _positive(value, name):
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than zero")
        return value

    async def __aenter__(self):
        self._start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    def _start(self) -> None:
        if self._closed:
            raise DesktopActionClosed("desktop action queue is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise DesktopActionError("desktop action queue belongs to another event loop")
        self._loop = loop
        if self._worker is None:
            self._worker = loop.create_task(self._run(), name="visibility-desktop-action-queue")

    @property
    def active(self) -> bool:
        return not self.quiescent

    @property
    def poisoned(self) -> bool:
        return self._poison_reason is not None

    @property
    def poison_reason(self) -> str | None:
        return self._poison_reason

    @property
    def quiescent(self) -> bool:
        """No preparation/native operation can still change desktop focus."""
        return not (
            (self._prepare_task is not None and not self._prepare_task.done())
            or (self._native is not None and (self._native.busy.is_set() or self._native.closing.is_set()))
        )

    @property
    def cleanup_pending(self) -> bool:
        return not self.quiescent or (
            self._closed and self._native is not None and (
                not self._native.done.done() or self._native.shutdown_error is not None
            )
        )

    @property
    def pending_count(self) -> int:
        return len(self._jobs)

    async def execute(
        self,
        action: Callable[[Any], Any],
        *,
        prepare: Callable[[], Awaitable[Any]] | None = None,
        key: Hashable | None = None,
        timeout_s: float,
        cancel_action: Callable[[], None] | None = None,
    ) -> Any:
        self._start()
        if self.poisoned:
            raise DesktopActionQuarantined(self._poison_reason)
        timeout_s = self._positive(timeout_s, "timeout_s")
        if key is not None and key in self._keys:
            raise DesktopActionDuplicate("this desktop action identity is already queued or running")
        if key is not None:
            self._keys.add(key)
        loop = asyncio.get_running_loop()
        job = _Job(action, prepare, cancel_action, loop.time() + timeout_s, loop.create_future(), key)
        self._jobs.add(job)
        self._queue.put_nowait(job)
        try:
            done, _ = await asyncio.wait((job.result,), timeout=timeout_s)
            if not done:
                self._abandon(job)
                job.result.cancel()
                raise DesktopActionTimeout("desktop action deadline expired while " + job.phase)
            return job.result.result()
        except asyncio.CancelledError:
            self._abandon(job)
            job.result.cancel()
            raise

    def _abandon(self, job: _Job) -> None:
        job.native_cancelled.set()
        job.stopped.set()
        if job.phase in {"preparing", "native"} and not job.cancel_notified:
            job.cancel_notified = True
            if job.cancel_action is not None:
                try:
                    job.cancel_action()
                except Exception:
                    # Cancellation must still hold the lease and quarantine.
                    self._poison("desktop cancellation callback failed")
        if job.phase == "native" and self._native is not None and self._native.busy.is_set():
            self._poison("native desktop action was cancelled or timed out before it finished")

    def _poison(self, reason: str) -> None:
        if self._poison_reason is None:
            self._poison_reason = reason
        for other in self._jobs:
            if other is self._current:
                continue
            other.native_cancelled.set()
            other.stopped.set()
            if not other.result.done():
                other.result.set_exception(DesktopActionQuarantined(self._poison_reason))
        if self._native is not None:
            self._native.stop()

    async def _prepare(self, job: _Job) -> None:
        if job.prepare is None:
            return
        job.phase = "preparing"
        task = asyncio.create_task(job.prepare())
        self._prepare_task = task
        stopped = asyncio.create_task(job.stopped.wait())
        try:
            done, _ = await asyncio.wait((task, stopped), timeout=max(0, job.deadline - self._loop.time()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done and not job.stopped.is_set():
                task.result()
                return
            self._abandon(job)
            # A cancelled CDP await can leave an already-dispatched focus
            # command running in Chrome. Keep the original preparation alive
            # until its own result confirms completion; never equate Python
            # cancellation acknowledgement with revoked desktop input.
            drained, _ = await asyncio.wait((task,), timeout=0.05)
            if not drained:
                self._poison("desktop preparation outlived its request deadline or cancellation")
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.done():
                        raise
                except Exception:
                    pass
            else:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            raise _Abandoned()
        finally:
            stopped.cancel()

    async def _perform(self, job: _Job) -> Any:
        lease = _Lease(self.lock_path)
        try:
            job.phase = "waiting for desktop lock"
            while not lease.try_acquire():
                remaining = job.deadline - self._loop.time()
                if job.stopped.is_set() or remaining <= 0:
                    raise _Abandoned()
                try:
                    await asyncio.wait_for(job.stopped.wait(), min(self._lock_poll_s, remaining))
                except asyncio.TimeoutError:
                    pass
            if job.stopped.is_set() or self._loop.time() >= job.deadline:
                raise _Abandoned()
            await self._prepare(job)
            if job.stopped.is_set() or self._closed or self._loop.time() >= job.deadline:
                raise _Abandoned()
            if self._native is None:
                self._native = _NativeThread(self._session_factory, self._close_session, self.lock_path, self._lock_poll_s)
            job.phase = "native"
            future = self._native.submit(job, lease)
            lease = None  # Only the native thread may now release the lock.
            # Shielding prevents asyncio cancellation from cancelling the private
            # concurrent Future while its native action still owns the desktop.
            try:
                return await asyncio.shield(asyncio.wrap_future(future))
            except _Abandoned:
                raise
            except Exception:
                self._poison("native desktop action failed; later actions are disabled for this run")
                raise
        finally:
            if lease is not None:
                task = self._prepare_task
                if task is not None and not task.done():
                    # Even event-loop shutdown cannot release a still-active
                    # preparation's lease early. Process exit releases it if the
                    # transport never acknowledges cancellation.
                    def release_after_prepare(done, owned=lease):
                        try:
                            done.exception()
                        except BaseException:
                            pass
                        finally:
                            owned.release()
                    task.add_done_callback(release_after_prepare)
                else:
                    lease.release()

    async def _run(self) -> None:
        try:
            while True:
                job = await self._queue.get()
                if job is None:
                    break
                self._current = job
                try:
                    if job.stopped.is_set() or job.result.done():
                        continue
                    if self.poisoned:
                        raise DesktopActionQuarantined(self._poison_reason)
                    value = await self._perform(job)
                except _Abandoned:
                    if not job.result.done():
                        job.result.set_exception(DesktopActionTimeout("desktop action ended before dispatch"))
                except asyncio.CancelledError:
                    self._abandon(job)
                    if not job.result.done():
                        job.result.cancel()
                    raise
                except Exception as exc:
                    if not job.result.done():
                        job.result.set_exception(exc)
                else:
                    if not job.result.done():
                        job.result.set_result(value)
                finally:
                    job.phase = "finished"
                    self._jobs.discard(job)
                    if job.key is not None:
                        self._keys.discard(job.key)
                    self._current = None
        finally:
            self._closed = True
            for pending in tuple(self._jobs):
                self._abandon(pending)
                if not pending.result.done():
                    pending.result.set_exception(DesktopActionClosed("desktop action queue closed"))
            if self._native is not None:
                self._native.stop()

    async def aclose(self, *, timeout_s: float | None = None) -> bool:
        """Stop accepting work; return False if native/preparation cleanup lingers.

        Returning False never unlocks the desktop or replaces the native thread.
        Calling again after the backend settles can confirm completed cleanup.
        """
        timeout_s = self._shutdown_timeout_s if timeout_s is None else self._positive(timeout_s, "timeout_s")
        self._closed = True
        for job in tuple(self._jobs):
            self._abandon(job)
            if not job.result.done():
                job.result.set_exception(DesktopActionClosed("desktop action queue closed"))
        if self._native is not None:
            self._native.stop()
        if self._worker is None:
            return True
        self._queue.put_nowait(None)
        waits = [self._worker]
        if self._native is not None:
            waits.append(asyncio.wrap_future(self._native.done))
        _, pending = await asyncio.wait(waits, timeout=timeout_s)
        if self._native is not None and self._native.shutdown_error is not None:
            self._poison("native desktop session teardown failed; desktop lease retained")
            raise DesktopActionError("native desktop session teardown failed") from self._native.shutdown_error
        if pending or self.cleanup_pending:
            self._poison("desktop shutdown is incomplete")
            return False
        return True
