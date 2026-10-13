"""Bind the run's desktop queue to one exact browser-owned Copilot page."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from urllib.parse import urlsplit

from ..errors import HumanVerificationRequiredError, IdentityMismatchError
from ..websites.chat_actions import (
    _await_call, _visible_access_block_marker, _is_blank_provider_start_url,
    _canonical_reference, _is_conversation_url, strict_page_identity,
)


class _AlreadyClear(Exception):
    """Leave the queue before native backend startup when no click is needed."""


def _capture_focus_return(expected_pid):
    from platform_runtime.desktop import capture_focus_return
    return capture_focus_return(expected_pid, mac_factory=_capture_mac_focus_return)


def _capture_mac_focus_return(expected_pid):
    """Return to the prior app only if the user has not switched away.

    Capture and return occur under the same desktop lease. Never select an
    arbitrary prior Chrome tab, and never override a user's later app switch.
    """
    from AppKit import NSWorkspace, NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    workspace = NSWorkspace.sharedWorkspace()
    previous = workspace.frontmostApplication()
    previous_pid = previous.processIdentifier() if previous is not None else None
    def restore():
        current = workspace.frontmostApplication()
        if (previous_pid is None or previous_pid == expected_pid or current is None
                or current.processIdentifier() != expected_pid):
            return
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(previous_pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    return restore


class NativeVerificationSession:
    """Lazy, thread-affine AX reader/mouse session; never types or sends."""

    def __init__(self):
        from axbridge.mouse import HumanMouse, MotionConfig
        from axbridge.verification import CopilotVerificationAdapter
        from platform_runtime.desktop import verification_components
        # This guard includes live CDP/DOM identity and AX checkbox reads, not
        # only a local pointer check. Keep the sampled pause unchanged and leave
        # bounded time for that fresh proof immediately before button-down.
        self.mouse, self.adapter, self._native_close = verification_components(
            mouse_factory=HumanMouse, adapter_factory=CopilotVerificationAdapter,
            config=MotionConfig(guard_budget=.5, settle_limits=(.05, .85)))

    def close(self):
        try:
            self.mouse.__exit__(None, None, None)
        finally:
            self._native_close()

    def cancel(self):
        self.mouse.cancel()


class CopilotVerificationService:
    def __init__(self, actions, browser, *, notify=None, background_only=True, holds=None,
                 automatic_verification=False):
        if type(background_only) is not bool:
            raise TypeError("background_only must be a boolean")
        if type(automatic_verification) is not bool:
            raise TypeError("automatic_verification must be a boolean")
        self.actions, self.browser = actions, browser
        self.notify = notify or (lambda *_args, **_kwargs: None)
        self.background_only = background_only
        self.automatic_verification = automatic_verification
        self.holds = holds

    def _event(self, submission, status, **detail):
        if self.holds is not None and status in {"queued", "active", "clicked", "already_clear"}:
            self.holds.update(submission, status={"queued": "native_queued", "active": "native_active",
                "clicked": "clearance_check", "already_clear": "clearance_check"}[status])
        try:
            self.notify(submission.observation_id, {
                "function": "copilot_verification", "status": status,
                "profile_id": submission.profile_id, **detail,
            })
        except Exception:
            pass  # Observational progress must never authorize input.

    async def close_page(self, submission):
        """A recovery close also shares the desktop lease with native input."""
        if self.holds is not None:
            registry = self.holds.attachment.session.registry
            owned = next((tab for tab in registry.tabs if tab.page is submission.page), None)
            if owned is not None and registry.retention_predicate(owned):
                return  # Intentional retention is reported by the runtime cleanup.
        async def prepare_close():
            await submission.page.close()
            raise _AlreadyClear()  # No native backend is needed for this job.
        try:
            await self.actions.execute(key=("close", submission.identity.tab_reference),
                prepare=prepare_close, action=lambda _session: None, timeout_s=5)
        except _AlreadyClear:
            return

    async def __call__(self, submission, deadline):
        if self.holds is not None:
            try:
                await self.holds.begin(submission, deadline)
                deadline = self.holds.operation_deadline(submission, deadline)
                return await self._attempt(submission, deadline)
            except BaseException as error:
                self.holds.failed(submission, error, deadline)
                raise
        return await self._attempt(submission, deadline)

    async def validated(self, submission, deadline):
        if self.holds is not None:
            await self.holds.validated(submission, deadline)

    def failed(self, submission, error, deadline):
        if self.holds is not None:
            self.holds.failed(submission, error, deadline)

    async def poll_commands(self, submission, deadline):
        if self.holds is not None:
            await self.holds.poll_commands(submission, deadline)

    async def _attempt(self, submission, deadline):
        if submission.spec.platform != "copilot":
            raise HumanVerificationRequiredError("desktop verification supports Copilot only")
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        expires = time.monotonic() + max(0, deadline.remaining())
        native = []
        target = {}
        focus_return = []
        guard_refusal = {}
        expected = submission.identity
        page = submission.page
        enqueued_url = str(page.url)
        canonical = lambda ref: _canonical_reference(ref, submission.spec)
        pinned_conversation = (canonical(enqueued_url)
            if _is_conversation_url(enqueued_url, submission.spec) else None)

        async def same_page(*, focus=False, native_url=None, diagnostic=None):
            nonlocal pinned_conversation
            def refused(reason):
                if diagnostic is not None:
                    diagnostic["reason"] = reason
                return False
            if stop.is_set() or submission.cancelled() or deadline.expired():
                return refused("cancelled" if stop.is_set() or submission.cancelled() else "episode_deadline")
            observed = await strict_page_identity(page, profile_id=expected.profile_id,
                chrome_directory=expected.chrome_directory, _deadline=deadline)
            expected.assert_same_tab(observed)
            if not submission._submitted:
                expected.assert_same_tab(observed, require_same_conversation=True, canonical=canonical)
            elif canonical(expected.conversation_reference) != canonical(observed.conversation_reference):
                if not (_is_blank_provider_start_url(expected.conversation_reference,
                        submission.spec.start_url, aliases=submission.spec.blank_start_url_aliases)
                        and _is_conversation_url(observed.conversation_reference, submission.spec)):
                    return refused("conversation_changed")
            if pinned_conversation is not None and pinned_conversation != canonical(observed.conversation_reference):
                return refused("conversation_changed")
            checkpoint = submission._last_correlation_value
            if checkpoint is not None and canonical(checkpoint["identity"]["conversation_reference"]) != canonical(observed.conversation_reference):
                return refused("checkpoint_changed")
            if _is_conversation_url(observed.conversation_reference, submission.spec):
                pinned_conversation = canonical(observed.conversation_reference)
            if native_url is not None and canonical(native_url) != canonical(observed.conversation_reference):
                return refused("native_url_changed")
            if focus and not bool(await _await_call(page.evaluate,
                "() => document.hasFocus() && document.visibilityState === 'visible'",
                _deadline=deadline)):
                return refused("tab_not_focused")
            return True

        if self.background_only and not self.automatic_verification:
            # Playwright may inspect hidden tabs, but native verification needs
            # the user's foreground and real pointer. Keep waiting on this exact
            # page without entering the native queue or activating any window.
            self._event(submission, "manual_required",
                reason="background mode leaves verification to the user")
            try:
                while not deadline.expired():
                    if submission.cancelled():
                        raise HumanVerificationRequiredError("background verification was cancelled")
                    if self.holds is not None:
                        await self.holds.poll_commands(submission, deadline)
                    if not await same_page():
                        raise IdentityMismatchError("background verification target changed")
                    marker = await _visible_access_block_marker(page, submission.spec, _deadline=deadline)
                    if marker is None:
                        self._event(submission, "already_clear")
                        return
                    if marker != "human_verification_required":
                        raise HumanVerificationRequiredError("verification target no longer has the expected challenge")
                    await asyncio.sleep(min(.2, max(0, deadline.remaining())))
            except TimeoutError as exc:
                raise HumanVerificationRequiredError(
                    "Copilot requires manual verification; background wait reached the operation deadline"
                ) from exc
            raise HumanVerificationRequiredError(
                "Copilot requires manual verification; background wait reached the operation deadline"
            )

        async def prepare():
            if not await same_page():
                raise IdentityMismatchError("verification target changed while queued")
            marker = await _visible_access_block_marker(page, submission.spec, _deadline=deadline)
            if marker is None:
                raise _AlreadyClear()
            if marker != "human_verification_required":
                raise HumanVerificationRequiredError("verification target no longer has the expected challenge")
            url = str(page.url)
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {"copilot.com", "copilot.microsoft.com"}:
                raise IdentityMismatchError("verification page is outside Copilot")
            # This CDP session belongs to the already-attached browser, avoiding
            # ambiguous app-name/PID discovery when several Chromes are open.
            cdp = await _await_call(self.browser.new_browser_cdp_session, _deadline=deadline)
            try:
                info = await _await_call(cdp.send, "SystemInfo.getProcessInfo", _deadline=deadline)
            finally:
                await _await_call(cdp.detach, _deadline=deadline)
            pids = {item["id"] for item in info.get("processInfo", ()) if item.get("type") == "browser"}
            if len(pids) != 1 or not all(type(pid) is int and pid > 0 for pid in pids):
                raise IdentityMismatchError("verification browser PID is unavailable")
            target.update(pid=pids.pop(), url=url)
            if stop.is_set() or submission.cancelled() or deadline.expired():
                raise HumanVerificationRequiredError("verification was cancelled before activation")
            # Queue preparation owns cancellation and drains this focus change
            # before releasing the desktop lease; never abandon a late focus.
            if self.automatic_verification:
                focus_return.append(_capture_focus_return(target["pid"]))
            await page.bring_to_front()
            if not await same_page():
                raise IdentityMismatchError("verification target changed during activation")
            self._event(submission, "active")

        def foreground_check(*, deadline, native_url=None):
            started = time.monotonic()
            remaining = min(expires, deadline) - started
            diagnostic = {}
            def refused(reason):
                if not guard_refusal:
                    guard_refusal.update(reason=reason,
                        budget_ms=round(max(0., remaining) * 1000, 3),
                        elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                return False
            if stop.is_set() or remaining <= 0:
                return refused("cancelled" if stop.is_set() else "guard_deadline")
            future = asyncio.run_coroutine_threadsafe(
                same_page(focus=True, native_url=native_url, diagnostic=diagnostic), loop)
            try:
                if future.result(timeout=remaining) is True:
                    return True
                return refused(diagnostic.get("reason", "guard_refused"))
            except concurrent.futures.TimeoutError:
                future.cancel()
                return refused("guard_timeout")
            except IdentityMismatchError:
                future.cancel()
                return refused("identity_changed")
            except concurrent.futures.CancelledError:
                future.cancel()
                return refused("cancelled")
            except Exception:
                future.cancel()
                return refused("guard_read_failed")

        def action(session):
            native.append(session)
            if stop.is_set() or time.monotonic() >= expires:
                raise HumanVerificationRequiredError("verification action expired before input")
            result = session.adapter.attempt(expected_pid=target["pid"], expected_url=target["url"],
                expected_window_title=None, foreground_check=foreground_check,
                page_url_check=lambda url, *, deadline: foreground_check(deadline=deadline, native_url=url),
                deadline=expires, cancel=stop.is_set)
            # A normal adapter return confirms native button cleanup. An
            # exception delegates cleanup to the queue and never restores early.
            if focus_return and foreground_check(deadline=expires):
                try:
                    focus_return[0]()
                except Exception:
                    pass  # A closed prior app does not change verification proof.
            return result

        def cancel_action():
            stop.set()
            if native:
                native[0].cancel()

        self._event(submission, "queued", deadline_monotonic=expires)
        try:
            result = await self.actions.execute(key=(expected.profile_id, expected.tab_reference),
                prepare=prepare, action=action, timeout_s=deadline.remaining(), cancel_action=cancel_action)
            status = result.get("status") if isinstance(result, dict) else None
            if status not in {"clicked", "already_clear"}:
                self._event(submission, "unsupported", reason=str(result.get("reason", "unknown"))[:160],
                    **({"guard_refusal": dict(guard_refusal)} if guard_refusal else {}))
                error = HumanVerificationRequiredError("Copilot verification has no supported accessible checkbox")
                # Only an explicit backend result proves there was no press.
                # Timeouts/exceptions and ambiguous input cannot take the
                # no-input clearance path in PageSubmission.
                error.verification_input_attempted = result.get("click_attempted")
                raise error
            self._event(submission, status)
            # PageSubmission independently proves challenge clearance and the
            # same conversation before any further collection or preparation.
        except _AlreadyClear:
            self._event(submission, "already_clear")
        except asyncio.CancelledError:
            cancel_action()
            self._event(submission, "cancelled")
            raise
        except (HumanVerificationRequiredError, IdentityMismatchError):
            raise
        except Exception as exc:
            cancel_action()
            self._event(submission, "failed", error_type=type(exc).__name__)
            raise HumanVerificationRequiredError(
                "Copilot desktop verification could not complete (" + type(exc).__name__ + ")"
            ) from exc
