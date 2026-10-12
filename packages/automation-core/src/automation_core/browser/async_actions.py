"""Guarded native input for an existing asynchronous Playwright owner.

The caller supplies ownership and site-purpose checks and owns the durable
journal. This executor launches no browser, acquires no locks, performs no
recovery and reports API entry/return rather than business success.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import inspect
import math
import random
import time
from urllib.parse import urlsplit

from automation_core.contracts import DispatchReceipt, SensitiveValue
from automation_core.browser.movement import (
    Budget, Geometry, Gesture, MovementStrategy, PointerState, Policy, Stamp,
    Status, Vec, rectangle, validate_policy,
)


class AsyncActionError(RuntimeError):
    """Sanitized failure with a receipt preserving possible input entry."""

    def __init__(self, reason, receipt=None):
        self.reason = reason
        self.receipt = receipt or DispatchReceipt("not_attempted", reason=reason)
        super().__init__(reason)


@dataclass(eq=False, repr=False)
class AsyncTarget:
    """Runtime-only retained element and its original qualified locator."""

    locator: object = field(repr=False)
    handle: object = field(repr=False)
    document: object = field(repr=False)
    context: object = field(repr=False)
    origin: tuple[str, str] = field(repr=False)
    owner: object = field(repr=False)
    released: bool = False


def _origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AsyncActionError("invalid_origin")
    return parsed.scheme, parsed.netloc


class AsyncActionExecutor:
    """One borrowed page, exact retained targets and no automatic input retry.

    ``assert_owned(deadline)`` and each ``verify(deadline)`` must return True.
    They are trusted read-only callbacks, bounded by the same absolute monotonic
    deadline. Locator selection/meaning and result validation belong to the site.
    Input uses ElementHandles so a replacement matching a locator cannot receive
    an admitted action. Native input never activates the desktop or another tab.
    """

    def __init__(self, page, *, assert_owned, cancelled=lambda: False,
                 clock=time.monotonic, movement_policy: Policy | None = None):
        if not callable(assert_owned) or not callable(cancelled) or not callable(clock):
            raise ValueError("An ownership guard, cancellation reader and clock are required.")
        self.page = page
        self.assert_owned = assert_owned
        self.cancelled = cancelled
        self.clock = clock
        if movement_policy is not None:
            validate_policy(movement_policy)
        self.movement_policy = movement_policy
        self._movement_strategy = MovementStrategy()
        self._movement_stream = 0
        self._pointer_position = None
        self._pointer_document = None
        self._pointer_viewport = None
        self._owner = object()
        self._active = False
        self._pending = set()
        self.last_receipt = DispatchReceipt("not_attempted")

    def _checkpoint(self, deadline):
        if not isinstance(deadline, (float, int)) or isinstance(deadline, bool) or not math.isfinite(deadline):
            raise AsyncActionError("invalid_deadline")
        if self.cancelled():
            raise AsyncActionError("cancelled")
        if self.clock() >= deadline:
            raise AsyncActionError("deadline_exhausted")
        if self.page.is_closed():
            raise AsyncActionError("view_closed")

    def _timeout(self, deadline):
        self._checkpoint(deadline)
        return max(1, int((deadline - self.clock()) * 1000))

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    async def _call(self, callback, deadline, *args, _input=False, **kwargs):
        self._checkpoint(deadline)
        value = callback(*args, **kwargs)
        if not inspect.isawaitable(value):
            self._checkpoint(deadline)
            return value
        task = asyncio.ensure_future(value)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        task.add_done_callback(self._consume)
        try:
            while True:
                done, _ = await asyncio.wait((task,), timeout=min(.05, max(0, deadline - self.clock())))
                self._checkpoint(deadline)
                if task in done:
                    return task.result()
        except BaseException:
            if not _input:
                task.cancel()
            # Cancelling a transport waiter does not prove that native input
            # stopped in the browser. Keep its task until the original native
            # timeout/return, and block later input while it is unsettled.
            raise

    async def _guard(self, deadline):
        self._checkpoint(deadline)
        if await self._call(self.assert_owned, deadline, deadline) is not True:
            raise AsyncActionError("ownership_lost")
        self._checkpoint(deadline)

    async def retain(self, locator, deadline):
        await self._guard(deadline)
        handle = document = None
        try:
            count = await self._call(locator.count, deadline)
            if type(count) is not int or count != 1:
                raise AsyncActionError("target_not_unique")
            handle = await self._call(locator.element_handle, deadline, timeout=self._timeout(deadline))
            if handle is None:
                raise AsyncActionError("target_unavailable")
            document = await self._call(handle.evaluate_handle, deadline, "el => el.ownerDocument")
            target = AsyncTarget(locator, handle, document, self.page.context,
                                 _origin(self.page.url), self._owner)
            await self.validate(target, deadline)
            return target
        except BaseException as error:
            for resource in (handle, document):
                if resource is not None:
                    await self._dispose(resource)
            if isinstance(error, (AsyncActionError, asyncio.CancelledError)):
                raise
            raise AsyncActionError("target_unavailable") from None

    @staticmethod
    async def _dispose(resource):
        # Handle disposal is transport cleanup, never a website action.
        try:
            value = resource.dispose()
            if inspect.isawaitable(value):
                task = asyncio.ensure_future(value)
                done, _ = await asyncio.wait((task,), timeout=.25)
                if task not in done:
                    task.cancel()
                task.add_done_callback(AsyncActionExecutor._consume)
        except Exception:
            pass

    async def release(self, target):
        if type(target) is not AsyncTarget or target.owner is not self._owner:
            raise AsyncActionError("invalid_target")
        if not target.released:
            target.released = True
            await self._dispose(target.handle)
            await self._dispose(target.document)

    async def validate(self, target, deadline):
        await self._guard(deadline)
        if type(target) is not AsyncTarget or target.owner is not self._owner or target.released:
            raise AsyncActionError("invalid_target")
        if self.page.context is not target.context or _origin(self.page.url) != target.origin:
            raise AsyncActionError("binding_changed")
        if await self._call(target.document.evaluate, deadline, "doc => doc === document") is not True:
            raise AsyncActionError("binding_changed")
        count = await self._call(target.locator.count, deadline)
        if type(count) is not int or count != 1:
            raise AsyncActionError("target_not_unique")
        # The locator may now resolve a different node even in the same
        # document. Compare its current resolution with the retained handle.
        if await self._call(target.locator.evaluate, deadline,
                            "(el, retained) => el === retained && el.isConnected", target.handle,
                            timeout=self._timeout(deadline)) is not True:
            raise AsyncActionError("binding_changed")
        self._checkpoint(deadline)

    async def _approach(self, target, deadline):
        """Optional virtual motion; bounding boxes guide hover, never activation.

        Reuse the shared curve planner, with at most 1.2 seconds of spare time.
        The retained native action and its final source verifier still decide
        whether input is allowed. No desktop focus, wheel, or raw click occurs.
        """
        policy = self.movement_policy
        if policy is None:
            return
        until = min(self.clock() + 1.2, deadline - policy.reserve_s)
        if self.clock() + .08 >= until:
            return
        # Missing/offscreen geometry is optional. Cancellation, ownership and
        # retained-document checks remain authoritative at native activation.
        try:
            box = await self._call(target.handle.bounding_box, until)
            viewport = self.page.viewport_size
            if viewport is None:
                viewport = await self._call(self.page.evaluate, until,
                    "() => ({width: innerWidth, height: innerHeight})")
        except Exception:
            self._checkpoint(deadline)
            return
        if not box or not viewport:
            return
        width, height = viewport["width"], viewport["height"]
        if not all(math.isfinite(v) for v in (*box.values(), width, height)):
            return
        if (width <= 2 or height <= 2 or box["width"] <= 0 or box["height"] <= 0
                or box["x"] < 0 or box["y"] < 0
                or box["x"] + box["width"] > width
                or box["y"] + box["height"] > height):
            return  # Native handle actionability owns required scrolling.

        async def guard():
            await self._guard(deadline)
            if (self.page.context is not target.context
                    or _origin(self.page.url) != target.origin
                    or target.released):
                raise AsyncActionError("binding_changed")
            if await self._call(target.handle.evaluate, deadline,
                    "(el, doc) => el.isConnected && el.ownerDocument === document && document === doc",
                    target.document) is not True:
                raise AsyncActionError("binding_changed")

        last_move_started = None

        async def move(point):
            nonlocal last_move_started
            await guard()
            if self.clock() >= until:
                return False
            self._pointer_position = None
            # A cancelled mouse waiter remains pending, exactly like other
            # native input. No click/fill may overlap its unsettled transport.
            last_move_started = self.clock()
            await self._call(self.page.mouse.move, deadline, point.x, point.y, _input=True)
            self._pointer_position = point
            self._pointer_document = target.document
            self._pointer_viewport = (width, height)
            return True

        await guard()
        continuous = False
        if (self._pointer_position is not None and self._pointer_document is not None
                and self._pointer_viewport == (width, height)):
            try:
                continuous = await self._call(self._pointer_document.evaluate, deadline,
                                              "doc => doc === document") is True
            except Exception:
                self._checkpoint(deadline)
        if not continuous:
            if self.clock() + .08 >= until:
                return
            # Establish an actual commanded origin; never assume a (0, 0) start.
            if not await move(Vec(width / 2, height / 2)):
                return
        now = self.clock()
        if now >= until:
            return
        stamp = Stamp(str(id(self.page)), id(target.document), 0)
        geometry = Geometry(width, height,
            (rectangle(box["x"], box["y"], box["width"], box["height"]),), stamp, now,
            certified_usable=False)
        self._movement_stream += 1
        plan = self._movement_strategy.plan(
            Gesture("move", PointerState(self._pointer_position, "commanded", stamp, True)),
            geometry, replace(policy, stream_id=self._movement_stream),
            Budget(now, until - now, stamp))
        if plan.status != Status.PLANNED:
            return
        epoch = self.clock()
        for point in plan.points[1:]:
            scheduled = max(epoch + point.relative_time,
                            (last_move_started or epoch) + policy.min_command_gap_s)
            while self.clock() < scheduled:
                await guard()
                await asyncio.sleep(min(.02, scheduled - self.clock()))
            if self.clock() + policy.min_command_gap_s >= until:
                break
            # Drop stale intermediate points rather than replaying a burst.
            if self.clock() - scheduled > .04 and point is not plan.points[-1]:
                continue
            if not await move(Vec(point.x, point.y)):
                break

    async def _execute(self, target, deadline, verify, operation, *, editable=False,
                       approach=False):
        if self._active or any(not task.done() for task in self._pending):
            raise AsyncActionError("input_busy")
        if not callable(verify):
            raise AsyncActionError("verification_required")
        entered = None
        self._active = True
        self.last_receipt = DispatchReceipt("not_attempted")
        try:
            await self.validate(target, deadline)
            if not await self._call(target.handle.is_visible, deadline):
                raise AsyncActionError("target_unavailable")
            readiness = target.handle.is_editable if editable else target.handle.is_enabled
            if not await self._call(readiness, deadline):
                raise AsyncActionError("target_unavailable")
            if approach:
                await self._approach(target, deadline)
            # Source meaning is re-read after readiness. Revalidate retention
            # again afterward; a verifier may await while the DOM changes.
            if await self._call(verify, deadline, deadline) is not True:
                raise AsyncActionError("verification_failed")
            await self.validate(target, deadline)
            entered = self.clock()
            self.last_receipt = DispatchReceipt("entered", entered_at=entered, phase="dispatch")
            await operation()
            returned = self.clock()
            self.last_receipt = DispatchReceipt("returned", entered_at=entered, returned_at=returned, phase="dispatch")
            return self.last_receipt
        except asyncio.CancelledError:
            self.last_receipt = DispatchReceipt("not_attempted" if entered is None else "entered",
                                               reason="cancelled", entered_at=entered,
                                               phase="preparation" if entered is None else "dispatch")
            raise
        except Exception as error:
            reason = error.reason if isinstance(error, AsyncActionError) else "browser_call_failed"
            receipt = DispatchReceipt("not_attempted" if entered is None else "entered",
                                      reason=reason, entered_at=entered,
                                      phase="preparation" if entered is None else "dispatch")
            self.last_receipt = receipt
            raise AsyncActionError(reason, receipt) from None
        finally:
            self._active = False

    async def click(self, target, deadline, *, verify):
        async def operation():
            try:
                await self._call(target.handle.click, deadline, _input=True, timeout=self._timeout(deadline))
            finally:
                # Locator actionability can reposition the virtual pointer.
                self._pointer_position = None
        return await self._execute(target, deadline, verify, operation, approach=True)

    async def press(self, target, key, deadline, *, verify):
        if key not in {"Enter", "Shift+Enter", "Meta+A", "Control+A", "Backspace"}:
            raise AsyncActionError("unsupported_key")
        async def operation():
            await self._call(target.handle.press, deadline, key, _input=True, timeout=self._timeout(deadline))
        return await self._execute(target, deadline, verify, operation, editable=True)

    async def fill(self, target, value, deadline, *, verify):
        if type(value) is not SensitiveValue:
            raise AsyncActionError("invalid_private_input")
        async def operation():
            await self._call(target.handle.fill, deadline, value.reveal(), _input=True, timeout=self._timeout(deadline))
        return await self._execute(target, deadline, verify, operation, editable=True, approach=True)

    async def type_text(self, target, value, deadline, *, verify,
                        min_delay_ms=0., max_delay_ms=0.):
        if type(value) is not SensitiveValue:
            raise AsyncActionError("invalid_private_input")
        if (not math.isfinite(min_delay_ms) or not math.isfinite(max_delay_ms)
                or min_delay_ms < 0 or max_delay_ms < min_delay_ms):
            raise AsyncActionError("invalid_typing_delay")
        text = value.reveal()
        if any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise AsyncActionError("unsupported_input_character")
        # Each native operation has fresh ownership, element and source checks.
        # Without requested pacing, bounded line chunks avoid a full DOM check
        # for every letter. Newlines are always separate Shift+Enter operations.
        chunks = []
        if max_delay_ms == 0:
            for line_index, line in enumerate(text.split("\n")):
                if line_index:
                    chunks.append("\n")
                chunks.extend(line[index:index+128] for index in range(0, len(line), 128))
        else:
            chunks = text
        first_entered = None
        for index, character in enumerate(chunks):
            try:
                if index and max_delay_ms:
                    until = self.clock() + random.uniform(min_delay_ms, max_delay_ms) / 1000
                    while self.clock() < until:
                        self._checkpoint(deadline)
                        await asyncio.sleep(min(.05, until - self.clock(), max(0, deadline-self.clock())))
                if character == "\n":
                    async def operation():
                        await self._call(target.handle.press, deadline, "Shift+Enter", _input=True,
                                         timeout=self._timeout(deadline))
                else:
                    async def operation(character=character):
                        # ElementHandle.type is native keyboard input; unlike
                        # fill, it preserves site/editor key handling.
                        await self._call(target.handle.type, deadline, character, delay=0, _input=True,
                                         timeout=self._timeout(deadline))
                receipt = await self._execute(target, deadline, verify, operation,
                                             editable=True, approach=index == 0)
                if first_entered is None:
                    first_entered = receipt.entered_at
            except (AsyncActionError, asyncio.CancelledError) as error:
                if first_entered is not None:
                    self.last_receipt = DispatchReceipt("entered", entered_at=first_entered,
                        reason=getattr(error, "reason", "cancelled"), phase="dispatch")
                    if isinstance(error, AsyncActionError):
                        error.receipt = self.last_receipt
                raise
        self.last_receipt = DispatchReceipt("returned" if first_entered is not None else "not_attempted",
            entered_at=first_entered, returned_at=self.clock() if first_entered is not None else None,
            phase="dispatch" if first_entered is not None else "preparation")
        return self.last_receipt
