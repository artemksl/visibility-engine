"""Exact-page desktop verification using fake CDP/AX sessions only."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
import time

import pytest

from visibility_runner.browser import copilot_verification as service_module
from visibility_runner.browser.desktop_actions import DesktopActionQueue, DesktopActionQuarantined
from visibility_runner.errors import HumanVerificationRequiredError, IdentityMismatchError
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.copilot import COPILOT_SPEC


def _case(monkeypatch, tmp_path, *, url="https://copilot.com/chats/synthetic", background_only=False):
    events = []
    monkeypatch.setattr(service_module, "_capture_focus_return", lambda pid: lambda: events.append("restore-prior-app"))
    identity = chat.BrowserIdentity(
        profile_id="synthetic", chrome_directory="Synthetic Profile",
        browser_context_reference="synthetic-incognito-context",
        browser_target_reference="cdp-target:synthetic-owned-target",
        tab_reference="synthetic-owned-tab", conversation_reference=url,
    )
    page = SimpleNamespace(url=url, marker="human_verification_required", focused=True, closed=False,
                           identity_override=None, before_focus=None)

    async def observed(actual_page, *, profile_id, chrome_directory, **kwargs):
        assert actual_page is page
        assert (profile_id, chrome_directory) == (identity.profile_id, identity.chrome_directory)
        return page.identity_override or replace(identity, conversation_reference=page.url)

    async def marker(actual_page, *_args, **_kwargs):
        assert actual_page is page
        return page.marker

    async def focus():
        events.append("focus-owned-target")
        if page.before_focus:
            page.before_focus()

    async def evaluate(expression):
        assert "document.hasFocus()" in expression
        assert "document.visibilityState" in expression
        return page.focused

    async def close():
        events.append("owned-page-closed")
        page.closed = True

    page.bring_to_front = focus
    page.evaluate = evaluate
    page.close = close
    monkeypatch.setattr(service_module, "strict_page_identity", observed)
    monkeypatch.setattr(service_module, "_visible_access_block_marker", marker)
    cancelled = threading.Event()
    submission = chat.PageSubmission(page=page, profile_id=identity.profile_id,
        chrome_directory=identity.chrome_directory, observation_id="synthetic-observation",
        spec=COPILOT_SPEC, identity=identity, cancelled=cancelled.is_set)
    process_info = [{"type": "browser", "id": 4321}, {"type": "renderer", "id": 9999}]

    class CDP:
        async def send(self, method):
            assert method == "SystemInfo.getProcessInfo"
            events.append("read-browser-process")
            return {"processInfo": process_info}

        async def detach(self):
            events.append("detach-cdp")

    class Browser:
        async def new_browser_cdp_session(self):
            events.append("browser-cdp-session")
            return CDP()

    class Native:
        def __init__(self):
            self.adapter = self
            self.before_check = None
            self.calls = []
            self.clicks = 0
            self.cancel_calls = 0
            self.result = None

        def attempt(self, **kwargs):
            self.calls.append(kwargs)
            if self.before_check:
                self.before_check()
            if self.result is not None:
                return self.result
            if kwargs["cancel"]() or time.monotonic() >= kwargs["deadline"]:
                return {"status": "unsupported", "reason": "cancelled"}
            if not kwargs["foreground_check"](deadline=kwargs["deadline"]):
                return {"status": "unsupported", "reason": "wrong foreground target"}
            self.clicks += 1
            events.append("fake-checkbox-click")
            return {"status": "clicked"}

        def cancel(self):
            self.cancel_calls += 1

        def close(self):
            events.append("fake-native-close")

    native = Native()
    factories = []

    def factory():
        factories.append(threading.get_ident())
        return native

    queue = DesktopActionQueue(factory, lock_path=tmp_path / "synthetic-desktop.lock")
    notices = []
    service = service_module.CopilotVerificationService(queue, Browser(),
        notify=lambda observation, detail: notices.append((observation, detail)),
        background_only=background_only)
    return SimpleNamespace(service=service, queue=queue, native=native, page=page,
        submission=submission, process_info=process_info, factories=factories,
        events=events, notices=notices, cancelled=cancelled)


def _deadline(seconds=1):
    return chat._BrowserOperationDeadline.after(seconds, timeout_message="synthetic deadline")


def test_native_session_reserves_bounded_time_for_fresh_browser_and_ax_guard():
    session = service_module.NativeVerificationSession()
    try:
        assert session.mouse.config.guard_budget == .5
        assert session.mouse.config.settle_range == (.06, .30)
        assert session.mouse.config.settle_limits == (.05, .85)
    finally:
        session.close()


@pytest.mark.parametrize('budget, accepted', [(.05, False), (.5, True)])
def test_slow_fresh_foreground_guard_distinguishes_timeout_without_losing_focus(monkeypatch, tmp_path, budget, accepted):
    async def run():
        case = _case(monkeypatch, tmp_path)
        checking = threading.Event()
        original_identity = service_module.strict_page_identity
        async def slow_identity(*args, **kwargs):
            if checking.is_set():
                await asyncio.sleep(.08)
            return await original_identity(*args, **kwargs)
        monkeypatch.setattr(service_module, 'strict_page_identity', slow_identity)
        def attempt(**kwargs):
            checking.set()
            proven = kwargs['foreground_check'](deadline=min(kwargs['deadline'], time.monotonic() + budget))
            if proven:
                case.native.clicks += 1
            return {'status': 'clicked' if proven else 'unsupported', 'click_attempted': proven,
                'reason': 'synthetic final guard'}
        case.native.attempt = attempt
        try:
            if accepted:
                await case.service(case.submission, _deadline())
                assert case.native.clicks == 1
            else:
                with pytest.raises(HumanVerificationRequiredError):
                    await case.service(case.submission, _deadline())
                assert case.native.clicks == 0
                refusal = case.notices[-1][1]['guard_refusal']
                assert refusal['reason'] == 'guard_timeout'
                assert 0 < refusal['budget_ms'] <= 50
                assert refusal['elapsed_ms'] >= refusal['budget_ms']
            assert case.page.focused
        finally:
            assert await case.queue.aclose()
    asyncio.run(run())


def test_service_binds_browser_pid_exact_owned_target_and_native_thread(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        try:
            await case.service(case.submission, _deadline())
            assert case.native.clicks == 1
            call = case.native.calls[0]
            assert call["expected_pid"] == 4321
            assert call["expected_url"] == case.page.url
            assert call["expected_window_title"] is None
            assert case.factories[0] != threading.get_ident()
            assert case.events.index("detach-cdp") < case.events.index("focus-owned-target")
            assert case.events.index("focus-owned-target") < case.events.index("fake-checkbox-click")
            assert [detail["status"] for _, detail in case.notices] == ["queued", "active", "clicked"]
            assert not case.submission._submitted
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_queued_challenge_cleared_requires_no_native_session(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        case.page.marker = None
        try:
            await case.service(case.submission, _deadline())
            assert case.native.clicks == 0
            assert case.factories == []
            assert "browser-cdp-session" not in case.events
            assert "focus-owned-target" not in case.events
            assert case.notices[-1][1]["status"] == "already_clear"
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other-profile"),
    ("chrome_directory", "Other Profile"),
    ("browser_context_reference", "other-incognito-context"),
    ("browser_target_reference", "cdp-target:other-target"),
    ("tab_reference", "other-tab"),
])
def test_changed_owned_identity_is_refused_before_focus_or_native_session(monkeypatch, tmp_path, field, value):
    async def run():
        case = _case(monkeypatch, tmp_path)
        case.page.identity_override = replace(case.submission.identity, **{field: value})
        try:
            with pytest.raises(IdentityMismatchError):
                await case.service(case.submission, _deadline())
            assert case.factories == []
            assert "focus-owned-target" not in case.events
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_queued_blank_page_cannot_be_adopted_as_a_different_conversation(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path, url="https://copilot.com/")
        original = case.submission.identity
        case.page.url = "https://copilot.com/chats/unrelated-existing-chat"
        try:
            with pytest.raises(IdentityMismatchError):
                await case.service(case.submission, _deadline())
            assert case.submission.identity == original
            assert case.native.clicks == 0
            assert case.factories == []
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_sent_bound_conversation_change_is_refused_before_native_action(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        original = case.submission.identity
        case.submission._submitted = True
        case.page.url = "https://copilot.com/chats/unrelated-existing-chat"
        try:
            with pytest.raises(IdentityMismatchError):
                await case.service(case.submission, _deadline())
            assert case.submission.identity == original
            assert case.native.clicks == 0
            assert case.factories == []
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_sent_checkpoint_prevents_rebinding_an_unbound_stored_identity(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path, url="https://copilot.com/")
        case.submission._submitted = True
        case.submission._last_correlation_value = {
            "identity": {"conversation_reference": "https://copilot.com/chats/first"},
        }
        case.page.url = "https://copilot.com/chats/other"
        try:
            with pytest.raises(IdentityMismatchError):
                await case.service(case.submission, _deadline())
            assert case.native.clicks == 0
            assert case.factories == []
            assert case.submission.identity.conversation_reference == "https://copilot.com/"
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_sent_first_conversation_can_be_verified_without_advancing_send_state(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path, url="https://copilot.com/")
        case.submission._submitted = True
        original = case.submission.identity
        case.page.url = "https://copilot.com/chats/first"
        try:
            await case.service(case.submission, _deadline())
            assert case.native.clicks == 1
            assert case.submission.identity == original
            assert case.submission._submitted
            assert case.submission.response_baseline is None
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["url", "target", "context", "focus", "cancel"])
def test_native_final_guard_rejects_target_url_focus_or_cancellation_changes(monkeypatch, tmp_path, change):
    async def run():
        case = _case(monkeypatch, tmp_path)

        def replace_target():
            if change == "url":
                case.page.url = "https://copilot.com/chats/other"
            elif change == "target":
                case.page.identity_override = replace(case.submission.identity,
                    browser_target_reference="cdp-target:other")
            elif change == "context":
                case.page.identity_override = replace(case.submission.identity,
                    browser_context_reference="other-context")
            elif change == "focus":
                case.page.focused = False
            else:
                case.cancelled.set()

        case.native.before_check = replace_target
        try:
            with pytest.raises(HumanVerificationRequiredError):
                await case.service(case.submission, _deadline())
            assert case.native.clicks == 0
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("processes", [[], [{"type": "renderer", "id": 44}],
    [{"type": "browser", "id": 1}, {"type": "browser", "id": 2}],
    [{"type": "browser", "id": 0}], [{"type": "browser", "id": True}]])
def test_unproven_browser_pid_is_refused_before_focus(monkeypatch, tmp_path, processes):
    async def run():
        case = _case(monkeypatch, tmp_path)
        case.process_info[:] = processes
        try:
            with pytest.raises(IdentityMismatchError):
                await case.service(case.submission, _deadline())
            assert "detach-cdp" in case.events
            assert "focus-owned-target" not in case.events
            assert case.factories == []
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_unsupported_accessible_challenge_remains_typed_failure_without_send(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        case.native.result = {"status": "unsupported", "reason": "image puzzle"}
        try:
            with pytest.raises(HumanVerificationRequiredError):
                await case.service(case.submission, _deadline())
            assert case.native.clicks == 0
            assert not case.submission._submitted
            assert case.notices[-1][1]["status"] == "unsupported"
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_other_platform_cannot_enqueue_a_desktop_action(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        case.submission.spec = replace(COPILOT_SPEC, platform="gemini")
        try:
            with pytest.raises(HumanVerificationRequiredError, match="Copilot only"):
                await case.service(case.submission, _deadline())
            assert case.queue.pending_count == 0
            assert case.events == []
            assert case.factories == []
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["cancel", "deadline"])
def test_cancelled_or_expired_native_action_cannot_click_later(monkeypatch, tmp_path, reason):
    async def run():
        case = _case(monkeypatch, tmp_path)
        entered = threading.Event()
        release = threading.Event()

        def pause_before_guard():
            entered.set()
            assert release.wait(2), "fake native release was not signalled"

        case.native.before_check = pause_before_guard
        task = asyncio.create_task(case.service(case.submission,
            _deadline(.08 if reason == "deadline" else 1)))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            if reason == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(HumanVerificationRequiredError):
                    await task
            assert case.native.clicks == 0
            assert case.native.cancel_calls >= 1
            release.set()
            assert await case.queue.aclose(timeout_s=1)
            assert case.native.clicks == 0
            assert not case.submission._submitted
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await case.queue.aclose(timeout_s=1)

    asyncio.run(run())


def test_late_focus_remains_owned_by_queue_until_it_settles(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_focus():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass  # Deliberately simulate a transport ignoring cancellation.
            case.events.append("late-focus-completed")

        case.page.bring_to_front = delayed_focus
        task = asyncio.create_task(case.service(case.submission, _deadline(.04)))
        try:
            await entered.wait()
            with pytest.raises(HumanVerificationRequiredError):
                await task
            assert not case.queue.quiescent
            assert not await case.queue.aclose(timeout_s=.01)
            assert case.native.clicks == 0
            assert case.factories == []
            release.set()
            assert await case.queue.aclose(timeout_s=1)
            assert case.native.clicks == 0
            assert "late-focus-completed" in case.events
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await case.queue.aclose(timeout_s=1)

    asyncio.run(run())


def test_close_page_waits_for_native_action_to_release_desktop(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        entered, release = threading.Event(), threading.Event()

        def pause_before_guard():
            entered.set()
            assert release.wait(2)

        case.native.before_check = pause_before_guard
        action = asyncio.create_task(case.service(case.submission, _deadline()))
        close = None
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            close = asyncio.create_task(case.service.close_page(case.submission))
            await asyncio.sleep(.005)
            assert not case.page.closed
            assert not close.done()
            release.set()
            await action
            await close
            assert case.events.index("fake-checkbox-click") < case.events.index("owned-page-closed")
            assert len(case.factories) == 1
        finally:
            release.set()
            await asyncio.gather(action, *(tuple([close]) if close is not None else ()),
                                 return_exceptions=True)
            assert await case.queue.aclose(timeout_s=1)

    asyncio.run(run())


def test_close_page_does_not_start_native_backend(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        try:
            await case.service.close_page(case.submission)
            assert case.page.closed
            assert case.events == ["owned-page-closed"]
            assert case.factories == []
        finally:
            assert await case.queue.aclose()

    asyncio.run(run())


def test_poisoned_queue_cannot_close_a_page_still_used_by_native_action(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path)
        entered, release = threading.Event(), threading.Event()

        def pause_before_guard():
            entered.set()
            assert release.wait(2)

        case.native.before_check = pause_before_guard
        action = asyncio.create_task(case.service(case.submission, _deadline(.05)))
        try:
            with pytest.raises(HumanVerificationRequiredError):
                await action
            assert entered.is_set()
            assert case.queue.poisoned
            with pytest.raises(DesktopActionQuarantined):
                await case.service.close_page(case.submission)
            assert not case.page.closed
            release.set()
            assert await case.queue.aclose(timeout_s=1)
            assert not case.page.closed
            assert case.native.clicks == 0
        finally:
            release.set()
            await asyncio.gather(action, return_exceptions=True)
            await case.queue.aclose(timeout_s=1)

    asyncio.run(run())


@pytest.mark.parametrize("blocked", [False, True])
def test_submission_close_uses_desktop_cleanup_without_raw_fallback(monkeypatch, tmp_path, blocked):
    async def run():
        case = _case(monkeypatch, tmp_path)
        calls = []

        async def desktop_close(item):
            assert item is case.submission
            calls.append("guarded-close")
            if blocked:
                raise DesktopActionQuarantined("native work remains active")

        case.submission.human_verification_handler = SimpleNamespace(close_page=desktop_close)
        if blocked:
            with pytest.raises(DesktopActionQuarantined):
                await case.submission.close()
        else:
            await case.submission.close()
        assert calls == ["guarded-close"]
        assert not case.page.closed
        assert "owned-page-closed" not in case.events
        assert await case.queue.aclose()

    asyncio.run(run())


def test_reopen_failure_keeps_page_when_desktop_cleanup_cannot_confirm(monkeypatch, tmp_path):
    from visibility_runner.websites.copilot import CopilotProvider

    async def run():
        case = _case(monkeypatch, tmp_path)
        calls = []

        async def factory(*args):
            return case.page

        async def goto(url, **kwargs):
            assert url == case.page.url

        async def identity(*args, **kwargs):
            return case.submission.identity

        async def history(*args, **kwargs):
            return {}

        async def access_failure(*args, **kwargs):
            raise HumanVerificationRequiredError("synthetic outstanding native action")

        async def desktop_close(item):
            calls.append(item.page)
            raise DesktopActionQuarantined("native work remains active")

        case.page.goto = goto
        monkeypatch.setattr(chat, "strict_page_identity", identity)
        monkeypatch.setattr(chat, "_conversation_history", history)
        monkeypatch.setattr(chat, "verify_conversation_history", lambda *a, **k: None)
        monkeypatch.setattr(chat.PageSubmission, "_assert_access_available", access_failure)
        provider = CopilotProvider(factory,
            human_verification_handler=SimpleNamespace(close_page=desktop_close))
        binding = {"schema_version": "browser-conversation-binding-v1",
                   "identity": case.submission.identity.to_mapping(), "prompt_sha256": "a" * 64}
        with pytest.raises(HumanVerificationRequiredError, match="outstanding native action"):
            await provider.reopen(binding, observation_id="synthetic-recovery")
        assert calls == [case.page]
        assert not case.page.closed
        assert case.factories == []
        assert await case.queue.aclose()

    asyncio.run(run())
