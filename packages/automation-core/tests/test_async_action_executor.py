"""Offline API entry and cancellation tests for borrowed async input."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from automation_core.browser.async_actions import AsyncActionError, AsyncActionExecutor
from automation_core.contracts import SensitiveValue
from automation_core.browser.movement import Policy


class Handle:
    def __init__(self):
        self.clicks = 0
        self.connected = True
        self.fail = False

    async def evaluate_handle(self, expression):
        return self

    async def evaluate(self, expression, *args, **kwargs):
        return self.connected

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def dispose(self):
        pass

    async def click(self, **kwargs):
        self.clicks += 1
        if self.fail:
            raise RuntimeError("private browser detail")


class Locator:
    def __init__(self, handle):
        self.handle = handle
        self.matches = 1

    async def count(self):
        return self.matches

    async def element_handle(self, **kwargs):
        return self.handle

    async def evaluate(self, expression, retained, **kwargs):
        return self.handle is retained and retained.connected


class AsyncExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = SimpleNamespace(context=object(), url="https://example.test/chat", is_closed=lambda: False)
        self.owned = True
        self.executor = AsyncActionExecutor(self.page, assert_owned=lambda deadline: self.owned)
        self.handle = Handle()
        self.locator = Locator(self.handle)
        self.deadline = time.monotonic() + 5
        self.target = await self.executor.retain(self.locator, self.deadline)

    async def asyncTearDown(self):
        await self.executor.release(self.target)

    async def test_entered_failure_is_sanitized_and_never_retried(self):
        self.handle.fail = True
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(str(error.exception), "browser_call_failed")
        self.assertTrue(error.exception.receipt.possibly_dispatched)
        self.assertEqual(self.handle.clicks, 1)

    async def test_verifier_replacement_blocks_retained_input(self):
        def replace(deadline):
            self.locator.handle = Handle()
            return True
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=replace)
        self.assertEqual(error.exception.reason, "binding_changed")
        self.assertFalse(error.exception.receipt.possibly_dispatched)
        self.assertEqual(self.handle.clicks, 0)

    async def test_ownership_loss_and_expiry_never_enter_input(self):
        self.owned = False
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(error.exception.reason, "ownership_lost")
        self.owned = True
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, time.monotonic()-1, verify=lambda deadline: True)
        self.assertEqual(error.exception.reason, "deadline_exhausted")
        self.assertEqual(self.handle.clicks, 0)

    async def test_cancelled_caller_leaves_native_input_pending_until_settlement(self):
        started, finish = asyncio.Event(), asyncio.Event()
        native_cancelled = False
        async def delayed(**kwargs):
            nonlocal native_cancelled
            self.handle.clicks += 1
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                native_cancelled = True
                await finish.wait()
        self.handle.click = delayed
        action = asyncio.create_task(self.executor.click(self.target, self.deadline,
                                                        verify=lambda deadline: True))
        await started.wait()
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertTrue(self.executor.last_receipt.possibly_dispatched)
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(error.exception.reason, "input_busy")
        self.assertEqual(self.handle.clicks, 1)
        self.assertFalse(native_cancelled)
        finish.set()
        await asyncio.sleep(.01)

    async def test_cancellation_between_typed_characters_retains_partial_input_receipt(self):
        typed = []
        stopped = False
        self.executor.cancelled = lambda: stopped
        self.handle.is_editable = self.handle.is_enabled
        async def type_character(value, **kwargs):
            typed.append(value)
        self.handle.type = type_character
        def cancel_in_pacing_gap(*args):
            nonlocal stopped
            stopped = True
            return 10.
        sensitive = SensitiveValue("ab")
        try:
            with patch("automation_core.browser.async_actions.random.uniform", cancel_in_pacing_gap):
                with self.assertRaises(AsyncActionError) as error:
                    await self.executor.type_text(self.target, sensitive, self.deadline,
                        verify=lambda deadline: True, min_delay_ms=10., max_delay_ms=10.)
            self.assertEqual(error.exception.reason, "cancelled")
            self.assertTrue(error.exception.receipt.possibly_dispatched)
            self.assertEqual(typed, ["a"])
        finally:
            sensitive.clear()

    def enable_movement(self, move):
        self.executor.movement_policy = Policy(seed=42, target_mode="centroid")
        self.page.viewport_size = {"width": 800, "height": 600}
        self.page.mouse = SimpleNamespace(move=move)
        async def bounds():
            return {"x": 20, "y": 450, "width": 120, "height": 60}
        self.handle.bounding_box = bounds

    async def test_movement_precedes_final_purpose_and_single_retained_click(self):
        events = []
        async def move(x, y):
            events.append(("move", x, y))
        self.enable_movement(move)
        def verify(deadline):
            events.append(("verify",))
            return True
        await self.executor.click(self.target, self.deadline, verify=verify)
        self.assertGreater(len(events), 3)
        self.assertEqual(events[-1], ("verify",))
        self.assertEqual(events[0], ("move", 400, 300))
        self.assertEqual(events[-2], ("move", 80, 480))
        self.assertEqual(self.handle.clicks, 1)

    async def test_pointer_event_changing_target_prevents_activation(self):
        async def move(x, y):
            self.handle.connected = False
        self.enable_movement(move)
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(error.exception.reason, "binding_changed")
        self.assertFalse(error.exception.receipt.possibly_dispatched)
        self.assertEqual(self.handle.clicks, 0)

    async def test_missing_optional_geometry_preserves_retained_activation(self):
        moves = []
        async def move(x, y):
            moves.append((x, y))
        self.enable_movement(move)
        async def missing():
            return None
        self.handle.bounding_box = missing
        await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(moves, [])
        self.assertEqual(self.handle.clicks, 1)

    async def test_cancelled_mouse_waiter_blocks_activation_until_settlement(self):
        started, finish = asyncio.Event(), asyncio.Event()
        native_cancelled = False
        async def move(x, y):
            nonlocal native_cancelled
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                native_cancelled = True
        self.enable_movement(move)
        action = asyncio.create_task(self.executor.click(self.target, self.deadline,
                                                        verify=lambda deadline: True))
        await started.wait()
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertFalse(self.executor.last_receipt.possibly_dispatched)
        with self.assertRaises(AsyncActionError) as error:
            await self.executor.click(self.target, self.deadline, verify=lambda deadline: True)
        self.assertEqual(error.exception.reason, "input_busy")
        self.assertEqual(self.handle.clicks, 0)
        self.assertFalse(native_cancelled)
        finish.set()
        await asyncio.sleep(.01)


if __name__ == "__main__":
    unittest.main()
