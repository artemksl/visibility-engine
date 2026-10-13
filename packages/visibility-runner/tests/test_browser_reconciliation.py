from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visibility_runner.errors import BrowserInteractionError
from support.browser_actions import SyntheticBrowserActions
from visibility_runner.websites import chat_actions as provider_base
from visibility_runner.websites.chat_actions import (
    BrowserIdentity,
    PageProviderSpec,
    PageSubmission,
    ReconciliationDisposition,
    ResponseBaseline,
)


class _ReconciliationNode:
    def __init__(
        self,
        text: str = "",
        *,
        descendants: dict[str, tuple[object, ...]] | None = None,
    ) -> None:
        self.text = text
        self.click_calls: list[dict[str, object]] = []
        self.descendants = dict(descendants or {})

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.text

    async def click(self, **kwargs: object) -> None:
        self.click_calls.append(dict(kwargs))

    async def element_handle(self):
        class Handle:
            async def evaluate(_self, _script, current):
                return current.owner is self

            async def dispose(_self):
                pass

        result = Handle()
        result.owner = self
        return result

    def locator(self, selector: str) -> "_ReconciliationLocator":
        return _ReconciliationLocator(
            self.descendants.get(selector, ())
        )


RECONCILIATION_TEST_GRACE_SECONDS = 0.6


@pytest.fixture(autouse=True)
def synthetic_input(monkeypatch):
    """Reconciliation fixtures use nodes; real owned-page input has its own suite."""
    class RetryActions(SyntheticBrowserActions):
        async def retain(self, locator, deadline):
            target = await super().retain(locator, deadline)
            target.native = SimpleNamespace(handle=await locator.element_handle())
            return target

        async def click(self, target, deadline, verify):
            if self.submission._native_retry_sent:
                await verify(deadline)
            return await super().click(target, deadline, verify)

    async def action_lookup(submission, selectors, *, timeout_ms, deadline):
        return await provider_base.first_visible(submission.page, selectors,
            timeout_ms=timeout_ms, _deadline=deadline)

    monkeypatch.setattr(provider_base, "SharedBrowserActions", RetryActions)
    monkeypatch.setattr(PageSubmission, "_first_action_with_access", action_lookup)


class _ReconciliationComposer(_ReconciliationNode):
    def __init__(self) -> None:
        super().__init__("")
        self.writes: list[str] = []

    async def fill(self, value: str) -> None:
        self.text = value
        self.writes.append(value)

    async def input_value(self) -> str:
        return self.text

    async def press(self, key: str) -> None:
        assert key == "Enter"


class _ReconciliationLocator:
    def __init__(self, nodes: tuple[object, ...]) -> None:
        self.nodes = nodes

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> object:
        return self.nodes[index]

    async def inner_text(self) -> str:
        if not self.nodes:
            return ""
        reader = getattr(self.nodes[0], "inner_text")
        return str(await reader())


class _ReconciliationPage:
    def __init__(self, *, prompt: str) -> None:
        self.browser_context_id = "context-1"
        self.window_name = "tab-1"
        self.url = "https://chatgpt.com/c/one"
        self.closed = False
        self.body_text = ""
        self.prompt = prompt
        self.answer = ""
        self.error = ""
        self.busy = False
        self.historical_responses: tuple[_ReconciliationNode, ...] = ()
        self.composer = _ReconciliationComposer()
        self.send = _ReconciliationNode()
        self.retry = _ReconciliationNode("Retry")

    async def evaluate(self, script: str, *_args: object) -> str:
        assert script == "() => window.name || ''"
        return self.window_name

    def is_closed(self) -> bool:
        return self.closed

    def locator(self, selector: str) -> _ReconciliationLocator:
        nodes: tuple[object, ...]
        if selector == "body":
            nodes = (_ReconciliationNode(self.body_text),)
        elif selector == "composer":
            nodes = (self.composer,)
        elif selector == "send":
            nodes = (self.send,)
        elif selector == "user":
            nodes = (_ReconciliationNode(self.prompt),)
        elif selector == "response":
            current = (
                _ReconciliationNode(
                    self.answer or self.error,
                    descendants={
                        "error": (
                            (_ReconciliationNode(self.error),)
                            if self.error
                            else ()
                        ),
                        "retry": (
                            (self.retry,)
                            if self.error
                            else ()
                        ),
                    },
                ),
            ) if self.answer or self.error else ()
            nodes = (
                *self.historical_responses,
                *current,
            )
        elif selector == "stop":
            nodes = ((_ReconciliationNode("Stop"),) if self.busy else ())
        elif selector == "error":
            nodes = (
                (_ReconciliationNode(self.error),)
                if self.error
                else ()
            )
        elif selector == "retry":
            nodes = ((self.retry,) if self.error else ())
        else:
            nodes = ()
        return _ReconciliationLocator(nodes)


def _reconciliation_submission(
    page: _ReconciliationPage,
) -> PageSubmission:
    return PageSubmission(
        page=page,
        profile_id="first",
        chrome_directory="Profile 7",
        observation_id="obs-reconcile",
        spec=PageProviderSpec(
            platform="chatgpt",
            start_url="https://chatgpt.com/",
            composer_selectors=("composer",),
            send_selectors=("send",),
            answer_selectors=("response",),
            response_node_selectors=("response",),
            user_message_selectors=("user",),
            stop_selectors=("stop",),
            terminal_error_within_response_selectors=("error",),
            terminal_error_markers=("something went wrong",),
            native_retry_within_response_selectors=("retry",),
            conversation_url_prefixes=("https://chatgpt.com/c/",),
            stable_seconds=0.0,
        ),
        identity=BrowserIdentity(
            profile_id="first",
            chrome_directory="Profile 7",
            browser_context_reference="context-1",
            tab_reference="tab-1",
            conversation_reference="https://chatgpt.com/c/one",
        ),
    )


def test_prepare_captures_durable_response_baseline_before_send() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    page.answer = "older assistant answer"
    submission = _reconciliation_submission(page)

    asyncio.run(submission.prepare_exact("exact prompt"))

    baseline = submission.response_baseline
    assert baseline is not None
    assert baseline.visible_response_count == 1
    assert baseline.to_mapping() == {
        "visible_response_count": 1,
        "newest_response_sha256": hashlib.sha256(
            b"older assistant answer"
        ).hexdigest(),
    }
    assert submission._submitted is False


def test_response_deadline_bounds_post_capture_identity_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from visibility_runner.websites import chat_actions as provider_base

    response_timeout = 0.02
    external_watchdog = 0.15
    real_sleep = asyncio.sleep

    class Page(_ReconciliationPage):
        identity_cancelled = False

        async def evaluate(self, script: str, *_args: object) -> str:
            assert script == "() => window.name || ''"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.identity_cancelled = True
                raise
            raise AssertionError("the never-returning identity read resumed")

    async def skip_poll_delay(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(
        provider_base.asyncio,
        "sleep",
        skip_poll_delay,
    )
    page = Page(prompt="exact prompt")
    submission = _reconciliation_submission(page)
    submission._current_response_timeout_seconds = response_timeout

    async def exercise() -> tuple[float, bool, BaseException]:
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.answer = "captured before identity proof"
        started = time.monotonic()
        response_wait = asyncio.create_task(submission.wait_response())
        try:
            await asyncio.wait_for(
                asyncio.shield(response_wait),
                timeout=external_watchdog,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            watchdog_fired = not response_wait.done()
            if watchdog_fired:
                response_wait.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await response_wait
            return elapsed, watchdog_fired, exc
        raise AssertionError("response wait unexpectedly completed")

    elapsed, watchdog_fired, error = asyncio.run(exercise())

    assert watchdog_fired is False
    assert str(error) == "chatgpt response did not settle"
    assert response_timeout / 2 <= elapsed < external_watchdog / 2
    assert page.identity_cancelled is True
    assert len(page.send.click_calls) == 2
    assert page.retry.click_calls == []


def test_reconciliation_captures_late_response_without_resend() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        original_send_count = len(page.send.click_calls)
        page.answer = "late provider answer"
        result = await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )
        return result, original_send_count

    result, original_send_count = asyncio.run(exercise())

    assert (
        result.disposition
        is ReconciliationDisposition.LATE_RESPONSE_CAPTURED
    )
    assert result.answer is not None
    assert result.answer.text == "late provider answer"
    assert len(page.send.click_calls) == original_send_count == 2
    assert page.retry.click_calls == []


@pytest.mark.parametrize("answer", ["late provider answer", ""])
def test_zero_wait_reads_the_real_correlated_turn_without_polling_or_resend(monkeypatch, answer):
    page = _ReconciliationPage(prompt="exact prompt")
    submission = _reconciliation_submission(page)
    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.answer = answer
        async def unexpected_poll(_delay):
            raise AssertionError("zero-wait inspection must not sleep")
        monkeypatch.setattr(provider_base.asyncio, "sleep", unexpected_poll)
        return await submission.reconcile_no_response(0)
    result = asyncio.run(exercise())
    assert result.disposition is (ReconciliationDisposition.LATE_RESPONSE_CAPTURED
                                  if answer else ReconciliationDisposition.AMBIGUOUS)
    assert len(result.evidence["checks"]) == 2
    assert (result.answer.text if result.answer else "") == answer
    assert len(page.send.click_calls) == 2 and not page.retry.click_calls


def test_zero_wait_slow_identity_read_is_bounded_by_existing_response_budget():
    class Page(_ReconciliationPage):
        cancelled = False
        async def evaluate(self, *_args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
    page = Page(prompt="exact prompt")
    submission = _reconciliation_submission(page)
    submission._submitted = True
    submission._prompt = "exact prompt"
    submission._response_baseline = ResponseBaseline(0, hashlib.sha256(b"").hexdigest())
    submission._current_response_timeout_seconds = .02
    async def exercise():
        return await asyncio.wait_for(submission.reconcile_no_response(0), .2)
    result = asyncio.run(exercise())
    assert result.disposition is ReconciliationDisposition.AMBIGUOUS
    assert result.failure_classification == "reconciliation_deadline_exceeded"
    assert result.evidence["checks"] == [] and page.cancelled
    assert not page.send.click_calls and not page.retry.click_calls


def test_stable_terminal_error_authorizes_scoped_native_retry() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.error = "Something went wrong"
        reconciled = await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )
        prepared = await submission.prepare_native_retry()
        baseline = submission.response_baseline
        with pytest.raises(
            BrowserInteractionError,
            match="do not add a user message",
        ):
            await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.error = ""
        page.answer = "regenerated provider answer"
        answer = await submission.wait_response()
        return reconciled, prepared, baseline, answer

    reconciled, prepared, baseline, answer = asyncio.run(exercise())

    assert (
        reconciled.disposition
        is ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
    )
    assert reconciled.native_retry_available is True
    assert prepared is True
    assert baseline == ResponseBaseline(
        visible_response_count=1,
        newest_response_sha256=hashlib.sha256(
            b"Something went wrong"
        ).hexdigest(),
    )
    assert page.composer.writes == ["exact prompt"]
    assert page.retry.click_calls == [
        {"trial": True, "timeout": 4_000},
        {},
    ]
    assert answer.text == "regenerated provider answer"


def test_native_retry_safety_probe_has_a_browser_operation_deadline() -> None:
    response_timeout = 0.02
    external_watchdog = 0.15

    class HangingUserMessage(_ReconciliationNode):
        cancelled = False

        async def inner_text(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("the native-retry safety probe resumed")

    class Page(_ReconciliationPage):
        def __init__(self, *, prompt: str) -> None:
            super().__init__(prompt=prompt)
            self.hanging_user_message = HangingUserMessage()

        def locator(self, selector: str) -> _ReconciliationLocator:
            if selector == "user":
                return _ReconciliationLocator((self.hanging_user_message,))
            return super().locator(selector)

    page = Page(prompt="exact prompt")
    page.error = "Something went wrong"
    submission = _reconciliation_submission(page)
    submission._current_response_timeout_seconds = response_timeout
    submission.restore_confirmed_attempt(
        prompt="exact prompt",
        response_baseline=ResponseBaseline(
            visible_response_count=0,
            newest_response_sha256=hashlib.sha256(b"").hexdigest(),
        ).to_mapping(),
        failure_classification=(
            "provider_terminal_error:something went wrong"
        ),
    )

    async def exercise() -> tuple[float, bool, BaseException]:
        started = time.monotonic()
        preparation = asyncio.create_task(
            submission.prepare_native_retry()
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(preparation),
                timeout=external_watchdog,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            watchdog_fired = not preparation.done()
            if watchdog_fired:
                preparation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await preparation
            return elapsed, watchdog_fired, exc
        raise AssertionError("native retry preparation unexpectedly completed")

    elapsed, watchdog_fired, error = asyncio.run(exercise())

    assert watchdog_fired is False
    assert str(error) == "chatgpt native retry preparation did not settle"
    assert response_timeout / 2 <= elapsed < external_watchdog / 2
    assert page.hanging_user_message.cancelled is True
    assert page.retry.click_calls == []


def test_old_terminal_error_cannot_authorize_new_turn_retry() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    page.error = "Something went wrong"
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        reconciled = await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )
        prepared = await submission.prepare_native_retry()
        return reconciled, prepared

    reconciled, prepared = asyncio.run(exercise())

    assert reconciled.disposition is ReconciliationDisposition.AMBIGUOUS
    assert reconciled.failure_classification == "no_stable_terminal_failure"
    assert prepared is False
    assert page.retry.click_calls == []


def test_older_response_error_and_retry_control_cannot_authorize() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    older_retry = _ReconciliationNode("Retry")
    older_error = _ReconciliationNode("Something went wrong")
    page.historical_responses = (
        _ReconciliationNode(
            "Something went wrong",
            descendants={
                "error": (older_error,),
                "retry": (older_retry,),
            },
        ),
        _ReconciliationNode("newer completed assistant answer"),
    )
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        reconciled = await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )
        prepared = await submission.prepare_native_retry()
        return reconciled, prepared

    reconciled, prepared = asyncio.run(exercise())

    assert reconciled.disposition is ReconciliationDisposition.AMBIGUOUS
    assert reconciled.failure_classification == "no_stable_terminal_failure"
    assert prepared is False
    assert older_retry.click_calls == []


def test_normal_send_rejects_same_count_response_dom_churn() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    page.answer = "response node before send"
    submission = _reconciliation_submission(page)
    submission._current_response_timeout_seconds = 0.001

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.answer = "same response node changed after send"
        with pytest.raises(TimeoutError, match="response did not settle"):
            await submission.wait_response()
        return await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )

    reconciled = asyncio.run(exercise())

    assert reconciled.disposition is ReconciliationDisposition.AMBIGUOUS
    assert reconciled.failure_classification == "no_stable_terminal_failure"
    assert reconciled.answer is None


def test_confirmed_native_retry_can_be_reprobed_after_process_restore() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    original = _reconciliation_submission(page)

    async def exercise():
        await original.prepare_exact("exact prompt")
        baseline = original.response_baseline
        assert baseline is not None
        await original.submit_once()
        page.error = "Something went wrong"
        reconciled = await original.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )

        restored = _reconciliation_submission(page)
        restored.restore_confirmed_attempt(
            prompt="exact prompt",
            response_baseline=baseline.to_mapping(),
            failure_classification=str(
                reconciled.failure_classification
            ),
        )
        prepared = await restored.prepare_native_retry()
        return prepared, restored.response_baseline

    prepared, retry_baseline = asyncio.run(exercise())

    assert prepared is True
    assert retry_baseline == ResponseBaseline(
        visible_response_count=1,
        newest_response_sha256=hashlib.sha256(
            b"Something went wrong"
        ).hexdigest(),
    )
    assert page.retry.click_calls == [
        {"trial": True, "timeout": 4_000},
    ]


def test_terminal_error_node_cannot_satisfy_normal_response_wait() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.error = "Something went wrong"
        page.answer = "Something went wrong"
        await submission.wait_response()

    with pytest.raises(TimeoutError, match="terminal no-response"):
        asyncio.run(exercise())


def test_reconciliation_lost_tab_is_ambiguous_and_never_retries() -> None:
    page = _ReconciliationPage(prompt="exact prompt")
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.error = "Something went wrong"
        page.closed = True
        reconciled = await submission.reconcile_no_response(
            RECONCILIATION_TEST_GRACE_SECONDS
        )
        prepared = await submission.prepare_native_retry()
        return reconciled, prepared

    reconciled, prepared = asyncio.run(exercise())

    assert reconciled.disposition is ReconciliationDisposition.AMBIGUOUS
    assert reconciled.failure_classification == "identity_or_dom_unavailable"
    assert prepared is False
    assert page.retry.click_calls == []


def test_reconciliation_deadline_bounds_never_returning_user_message_read() -> None:
    grace_seconds = 0.02
    external_watchdog = 0.15

    class HangingUserMessage(_ReconciliationNode):
        cancelled = False

        async def inner_text(self) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError(
                "the never-returning reconciliation read resumed"
            )

    class Page(_ReconciliationPage):
        def __init__(self, *, prompt: str) -> None:
            super().__init__(prompt=prompt)
            self.hanging_user_message = HangingUserMessage()
            self.hang_user_reads = False

        def locator(self, selector: str) -> _ReconciliationLocator:
            if selector == "user" and self.hang_user_reads:
                return _ReconciliationLocator((self.hanging_user_message,))
            return super().locator(selector)

    page = Page(prompt="exact prompt")
    submission = _reconciliation_submission(page)

    async def exercise():
        await submission.prepare_exact("exact prompt")
        await submission.submit_once()
        page.hang_user_reads = True
        started = time.monotonic()
        reconcile = asyncio.create_task(
            submission.reconcile_no_response(grace_seconds)
        )
        result = await asyncio.wait_for(
            asyncio.shield(reconcile),
            timeout=external_watchdog,
        )
        return time.monotonic() - started, result

    elapsed, result = asyncio.run(exercise())

    assert elapsed < external_watchdog / 2
    assert result.disposition is ReconciliationDisposition.AMBIGUOUS
    assert (
        result.failure_classification
        == "reconciliation_deadline_exceeded"
    )
    assert result.evidence["checks"] == []
    assert page.hanging_user_message.cancelled is True
    assert len(page.send.click_calls) == 2
    assert page.retry.click_calls == []
