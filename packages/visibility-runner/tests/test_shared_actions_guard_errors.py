"""Typed preparation refusals survive the pinned executor; no browser input."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from automation_core.browser.async_actions import AsyncActionError
from visibility_runner.browser.input import write_exact_composer_input
from visibility_runner.browser.shared_actions import RetainedActionTarget, SharedBrowserActions
from visibility_runner.errors import HumanVerificationRequiredError, IdentityMismatchError


def actions_with_native(native):
    async def ready(*args, **kwargs):
        return True
    submission = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False), cancelled=lambda: False)
    actions = SharedBrowserActions(submission)
    actions.executor.validate = ready
    actions.executor.movement_policy = None
    target = RetainedActionTarget(None, SimpleNamespace(handle=SimpleNamespace(
        is_visible=ready, is_editable=ready, press=native, type=native, fill=native)))
    return actions, target


@pytest.mark.parametrize('error_type', [HumanVerificationRequiredError, IdentityMismatchError])
def test_typed_preparation_guard_refusal_is_not_retried_as_editor_corruption(error_type):
    async def run():
        native_calls, guard_calls = [], []
        async def native(*args, **kwargs):
            native_calls.append(True)
        actions, target = actions_with_native(native)
        refusal = error_type('synthetic typed refusal')
        async def verify(until):
            guard_calls.append(True)
            raise refusal
        async def clear(composer):
            await actions.clear(target, time.monotonic()+1, verify)
        with pytest.raises(error_type) as caught:
            await write_exact_composer_input(object(), 'synthetic prompt', max_attempts=3, clear=clear)
        assert caught.value is refusal
        assert native_calls == [] and guard_calls == [True]
        assert not actions.executor.last_receipt.possibly_dispatched
    asyncio.run(run())


def test_typed_native_failure_keeps_sanitized_dispatch_receipt():
    async def run():
        native_calls = []
        async def native(*args, **kwargs):
            native_calls.append(True)
            raise HumanVerificationRequiredError('synthetic private native message')
        async def verify(until):
            return True
        actions, target = actions_with_native(native)
        with pytest.raises(AsyncActionError) as caught:
            await actions.clear(target, time.monotonic()+1, verify)
        assert str(caught.value) == 'browser_call_failed'
        assert caught.value.receipt.possibly_dispatched
        assert caught.value.receipt.phase == 'dispatch' and native_calls == [True]
    asyncio.run(run())


def test_arbitrary_preparation_guard_failure_stays_sanitized():
    async def run():
        native_calls = []
        async def native(*args, **kwargs):
            native_calls.append(True)
        async def verify(until):
            raise RuntimeError('synthetic private guard message')
        actions, target = actions_with_native(native)
        with pytest.raises(AsyncActionError) as caught:
            await actions.clear(target, time.monotonic()+1, verify)
        assert str(caught.value) == 'browser_call_failed'
        assert not caught.value.receipt.possibly_dispatched and native_calls == []
    asyncio.run(run())


def test_guard_failure_after_partial_typing_keeps_possible_input_entry():
    async def run():
        native_calls, guard_calls = [], []
        async def native(text, **kwargs):
            native_calls.append(text)
        async def verify(until):
            guard_calls.append(True)
            if len(guard_calls) > 1:
                raise IdentityMismatchError('synthetic changed conversation')
            return True
        actions, target = actions_with_native(native)
        with pytest.raises(AsyncActionError) as caught:
            await actions.write(target, 'a'*129, time.monotonic()+1, verify, sequential=True)
        assert str(caught.value) == 'browser_call_failed'
        assert caught.value.receipt.possibly_dispatched
        assert caught.value.receipt.phase == 'dispatch' and native_calls == ['a'*128]
    asyncio.run(run())
