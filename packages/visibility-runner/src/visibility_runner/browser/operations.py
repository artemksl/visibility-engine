"""Bound browser awaits and retain unsettled calls through loop cleanup.

These operations do not authorize input or retries. The action executor and
run owner retain those responsibilities.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

_PENDING_BROWSER_OPERATIONS: set[asyncio.Future[Any]] = set()

class _BrowserOperationDeadlineExceeded(TimeoutError):
    """A browser await consumed the caller's absolute operation deadline."""


@dataclass(frozen=True)
class _BrowserOperationDeadline:
    expires_at: float
    timeout_message: str

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        timeout_message: str,
    ) -> "_BrowserOperationDeadline":
        return cls(
            expires_at=time.monotonic() + float(seconds),
            timeout_message=timeout_message,
        )

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def raise_expired(self) -> None:
        raise _BrowserOperationDeadlineExceeded(self.timeout_message)


async def _await_call(
    callable_value: Callable[..., Any],
    *args: Any,
    _deadline: Optional[_BrowserOperationDeadline] = None,
    **kwargs: Any,
) -> Any:
    if _deadline is not None and _deadline.expired():
        _deadline.raise_expired()
    try:
        value = callable_value(*args, **kwargs)
    except TypeError:
        if kwargs:
            value = callable_value(*args)
        else:
            raise
    if not inspect.isawaitable(value):
        if _deadline is not None and _deadline.expired():
            _deadline.raise_expired()
        return value
    if _deadline is None:
        return await value
    remaining = _deadline.remaining()
    if remaining <= 0:
        cancel = getattr(value, "cancel", None)
        close = getattr(value, "close", None)
        if callable(cancel):
            cancel()
        elif callable(close):
            close()
        _deadline.raise_expired()
    task = asyncio.ensure_future(value)
    try:
        done, _ = await asyncio.wait(
            (task,),
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        _cancel_background_browser_operation(task)
        raise
    if task not in done or _deadline.expired():
        _cancel_background_browser_operation(task)
        _deadline.raise_expired()
    try:
        return task.result()
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise _BrowserOperationDeadlineExceeded(
            _deadline.timeout_message
        ) from exc


def _cancel_background_browser_operation(
    task: asyncio.Future[Any],
) -> None:
    """Cancel and retain a timed-out browser operation until it settles."""

    task.cancel()
    _PENDING_BROWSER_OPERATIONS.add(task)
    task.add_done_callback(_consume_background_task_result)


def _consume_background_task_result(task: asyncio.Future[Any]) -> None:
    """Retrieve an eventually completed timed-out browser task's result."""

    _PENDING_BROWSER_OPERATIONS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _abandon_background_browser_operations_for_loop(
    loop: asyncio.AbstractEventLoop,
) -> set[asyncio.Future[Any]]:
    """Detach timed-out browser calls when their runner-owned loop closes."""

    abandoned: set[asyncio.Future[Any]] = set()
    for task in tuple(_PENDING_BROWSER_OPERATIONS):
        if task.get_loop() is not loop:
            continue
        _PENDING_BROWSER_OPERATIONS.discard(task)
        if task.done():
            _consume_background_task_result(task)
            continue
        # Keep the consumer callback attached in case this final cancellation
        # completes during the loop's bounded shutdown turn.
        task.cancel()
        if hasattr(task, "_log_destroy_pending"):
            task._log_destroy_pending = False  # type: ignore[attr-defined]
        abandoned.add(task)
    return abandoned


