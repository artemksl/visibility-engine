"""Owned Copilot blocker handling. Authentication never submits a chat prompt."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from automation_core.browser.async_actions import AsyncActionError

from ..errors import ProviderAccessBlockedError, IdentityMismatchError, HumanVerificationRequiredError
from ..websites import chat_actions as chat


WIDGET_ORIGINS = {"https://res.public.onecdn.static.microsoft", "https://account.microsoft.com"}
SIGN_IN_ACTION_REASONS = frozenset({
    "invalid_origin", "invalid_deadline", "cancelled", "deadline_exhausted", "view_closed",
    "ownership_lost", "target_not_unique", "target_unavailable", "invalid_target",
    "binding_changed", "input_busy", "verification_required", "verification_failed",
    "browser_call_failed",
})


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme + "://" + parsed.netloc.casefold()


def kind_for(marker):
    if marker == "human_verification_required":
        return "verification"
    if marker in {"authentication_required", "authentication_redirect"}:
        return "sign_in"
    if marker == "cookie_notice":
        return "cookies"
    return "other"


def account_emails(value, selected_ids):
    if not isinstance(value, dict):
        raise ValueError("copilot_account_bindings_invalid")
    result = {}
    for key, email in value.items():
        if (key not in selected_ids or not isinstance(email, str)
                or re.fullmatch(r'[^\s@<>"\\]+@[^\s@<>"\\]+\.[^\s@<>"\\]+', email) is None):
            raise ValueError("copilot_account_binding_invalid")
        result[key] = email.casefold()
    return result


def _require_blank_history(history):
    if (not isinstance(history, dict) or history.get("history_complete") is not True
            or history.get("history_ambiguous") is True or history.get("branch_changed") is True
            or any(not isinstance(history.get(key), (list, tuple)) or history[key]
                   for key in ("user_sha256", "assistant_sha256"))):
        raise IdentityMismatchError("copilot_sign_in_return_not_blank")


class CopilotBlockerService:
    def __init__(self, verification, *, holds=None, accounts=None):
        self.verification, self.holds = verification, holds
        self.accounts = dict(accounts or {})
        self.locks = {}
        self.active_profiles = {}

    async def gate(self, submission, deadline):
        waited = False
        lock = self.locks.get(submission.profile_id)
        if lock is not None and lock.locked() and self.active_profiles.get(submission.profile_id) != id(submission):
            await chat._await_call(lock.acquire, _deadline=deadline)
            lock.release()
            waited = True
        if self.holds is not None and id(submission) not in self.holds.live:
            retained = next((row for row in self.holds.store.rows()
                if row['profile_id'] == submission.profile_id), None)
            if retained is not None:
                error_type = (HumanVerificationRequiredError
                    if retained.get('blocker_kind', 'verification') == 'verification'
                    else ProviderAccessBlockedError)
                raise error_type("copilot_profile_blocker_retained",
                    reason=retained.get('reason') or 'copilot_profile_blocker_retained')
        return waited

    async def __call__(self, submission, deadline):
        # Preserve the established verification path and its one-click latch.
        await self.verification(submission, deadline)

    async def close_page(self, submission):
        await self.verification.close_page(submission)

    async def validated(self, submission, deadline):
        await self.verification.validated(submission, deadline)

    def failed(self, submission, error, deadline):
        self.verification.failed(submission, error, deadline)

    async def poll_commands(self, submission, deadline):
        await self.verification.poll_commands(submission, deadline)

    async def watch_page(self, page, deadline):
        """Keep JS dialogs observable; never accept unknown confirm/prompt text."""
        if getattr(page, "_visibility_runner_dialog_watch", False):
            return
        on = getattr(page, "on", None)
        if not callable(on):
            return
        def opened(dialog):
            page._visibility_runner_blocking_dialog = dialog
        on("dialog", opened)
        context = getattr(page, "context", None)
        factory = getattr(context, "new_cdp_session", None)
        if callable(factory):
            cdp = await chat._await_call(factory, page, _deadline=deadline)
            cdp.on("Page.javascriptDialogClosed", lambda _event:
                setattr(page, "_visibility_runner_blocking_dialog", None))
            await chat._await_call(cdp.send, "Page.enable", _deadline=deadline)
            page._visibility_runner_dialog_cdp = cdp
        page._visibility_runner_dialog_watch = True

    def _update(self, submission, marker, *, reason=None, deadline):
        submission._last_access_block_marker = marker
        if self.holds is not None:
            self.holds.update(submission, blocker_kind=kind_for(marker),
                status="manual_required", reason=reason or marker,
                _timeout_seconds=deadline.remaining())

    async def _same_tab(self, submission, deadline):
        if submission.cancelled() or deadline.expired():
            raise ProviderAccessBlockedError("copilot_blocker_cancelled_or_expired")
        observed = await chat.strict_page_identity(submission.page,
            profile_id=submission.profile_id, chrome_directory=submission.chrome_directory,
            _deadline=deadline)
        submission.identity.assert_same_tab(observed)
        return observed

    async def _click(self, submission, control, deadline, *, expected_origin=None):
        await self._same_tab(submission, deadline)
        expected_origin = expected_origin or origin(submission.page.url)
        if not await chat._await_call(control.is_enabled, _deadline=deadline):
            raise ProviderAccessBlockedError("copilot_sign_in_control_disabled")
        if origin(submission.page.url) != expected_origin:
            raise IdentityMismatchError("copilot_blocker_action_origin_changed")
        target = await submission._actions.retain(control, deadline.expires_at)

        async def verify(until):
            await self._same_tab(submission, deadline)
            if origin(submission.page.url) != expected_origin:
                raise IdentityMismatchError("copilot_blocker_action_origin_changed")
            return True

        await submission._actions.click(target, deadline.expires_at, verify)
        await self._same_tab(submission, deadline)

    async def _visible(self, locator, deadline):
        count = await chat._await_call(locator.count, _deadline=deadline)
        return [locator.nth(index) for index in range(count)
            if await chat._await_call(locator.nth(index).is_visible, _deadline=deadline)]

    async def _one_button(self, page, name, deadline):
        values = await self._visible(page.get_by_role("button", name=name, exact=True), deadline)
        return values[0] if len(values) == 1 else None

    async def _heading(self, page, text, deadline):
        return bool(await self._visible(page.get_by_role("heading", name=text, exact=True), deadline))

    async def _account_control(self, page, email, deadline, *, google=False):
        if google:
            values = await self._visible(page.locator('[data-identifier="' + email + '"]'), deadline)
            return values[0] if len(values) == 1 else None
        labels = await self._visible(page.get_by_text(email, exact=True), deadline)
        if len(labels) != 1:
            return None
        # The observed tile exposes its email beside the main account button
        # and an independent menu button. Never choose a display name alone.
        card = labels[0].locator('xpath=ancestor-or-self::*[self::button or @role="button" or .//button][1]')
        is_button = await chat._await_call(card.evaluate,
            'e => e.tagName === "BUTTON" || e.getAttribute("role") === "button"', _deadline=deadline)
        if is_button:
            return card
        buttons = await self._visible(card.locator(
            'button:not([aria-haspopup]):not([aria-label^="Open menu"]), '
            '[role="button"]:not([aria-haspopup]):not([aria-label^="Open menu"])'), deadline)
        return buttons[0] if len(buttons) == 1 else None

    async def _verify_account(self, submission, email, deadline):
        page = submission.page
        button = page.get_by_role("button", name=re.compile(r", Personal account$", re.I))
        values = []
        while not deadline.expired():
            await self._same_tab(submission, deadline)
            values = await self._visible(button, deadline)
            if len(values) == 1:
                break
            await asyncio.sleep(min(.2, max(0, deadline.remaining())))
        if len(values) != 1:
            return False
        await self._click(submission, values[0], deadline, expected_origin="https://copilot.com")
        try:
            while not deadline.expired():
                await self._same_tab(submission, deadline)
                for frame in getattr(page, "frames", (page,)):
                    if frame is not page and frame is not getattr(page, "main_frame", None):
                        if origin(frame.url) not in WIDGET_ORIGINS:
                            continue
                    sign_out = frame.get_by_role("button", name="Sign out from " + email, exact=True)
                    if len(await self._visible(sign_out, deadline)) == 1:
                        return True
                await asyncio.sleep(min(.2, max(0, deadline.remaining())))
        finally:
            if not deadline.expired():
                await chat._await_call(page.keyboard.press, "Escape", _deadline=deadline)
                await self._same_tab(submission, deadline)
        return False

    async def _restore_after_sign_in(self, submission, original, binding, email, deadline):
        observed = await self._same_tab(submission, deadline)
        spec = submission.spec
        if origin(observed.conversation_reference) != "https://copilot.com":
            raise ProviderAccessBlockedError("copilot_sign_in_return_unproven")
        original_url = binding["identity"]["conversation_reference"] if binding else original.conversation_reference
        saved_conversation = chat._is_conversation_url(original_url, spec)
        if saved_conversation:
            canonical = chat._canonical_reference(original_url, spec)
            if chat._canonical_reference(observed.conversation_reference, spec) != canonical:
                await chat._await_call(submission.page.goto, canonical, wait_until="domcontentloaded", _deadline=deadline)
                observed = await self._same_tab(submission, deadline)
            if binding is None:
                raise ProviderAccessBlockedError("copilot_sign_in_history_binding_unavailable")
            history = await chat._conversation_history(submission.page, spec, _deadline=deadline)
            chat.verify_conversation_history(binding,
                {"identity": observed.to_mapping(), "transcript": history},
                prefixes=spec.conversation_url_prefixes, prompt_sha256=binding["prompt_sha256"],
                expected_profile_id=submission.profile_id, expected_chrome_directory=submission.chrome_directory,
                temporary_message_id_prefixes=spec.temporary_message_id_prefixes)
        else:
            if submission._submitted or not chat._is_blank_provider_start_url(observed.conversation_reference,
                    spec.start_url, aliases=spec.blank_start_url_aliases):
                raise ProviderAccessBlockedError("copilot_sign_in_original_conversation_unproven")
            history = await chat._conversation_history(submission.page, spec, _deadline=deadline)
            _require_blank_history(history)
        if not await self._verify_account(submission, email, deadline):
            raise ProviderAccessBlockedError("copilot_sign_in_account_unproven")
        verified = await self._same_tab(submission, deadline)
        observed.assert_same_tab(verified, require_same_conversation=saved_conversation,
            canonical=lambda ref: chat._canonical_reference(ref, spec))
        if not saved_conversation and (submission._submitted or not chat._is_blank_provider_start_url(
                verified.conversation_reference, spec.start_url, aliases=spec.blank_start_url_aliases)):
            raise ProviderAccessBlockedError("copilot_sign_in_original_conversation_unproven")
        current_history = await chat._conversation_history(submission.page, spec, _deadline=deadline)
        if saved_conversation:
            chat.verify_conversation_history(binding,
                {"identity": verified.to_mapping(), "transcript": current_history},
                prefixes=spec.conversation_url_prefixes, prompt_sha256=binding["prompt_sha256"],
                expected_profile_id=submission.profile_id, expected_chrome_directory=submission.chrome_directory,
                temporary_message_id_prefixes=spec.temporary_message_id_prefixes)
        else:
            _require_blank_history(current_history)
            if history.get("branch_id") is not None and current_history.get("branch_id") != history["branch_id"]:
                raise IdentityMismatchError("copilot_sign_in_return_branch_changed")
        observed, history = verified, current_history
        if await chat._visible_access_block_marker(submission.page, spec, _deadline=deadline) is not None:
            raise ProviderAccessBlockedError("copilot_sign_in_return_blocked")
        submission._identity = observed
        # Authentication can recreate the editor. Refill only before admission;
        # a dispatched or ambiguous turn stays submitted and cannot be resent.
        if submission._prepared and not submission._submitted:
            prompt = submission._prompt
            composers = await chat.visible_items(submission.page, spec.composer_selectors, _deadline=deadline)
            if not composers or await chat._await_call(chat.read_copilot_composer_value,
                    composers[0], _deadline=deadline) not in ("", prompt):
                raise ProviderAccessBlockedError("copilot_sign_in_prepared_prompt_changed")
            await chat._await_call(submission._prepare_exact, prompt,
                operation_deadline=deadline, _deadline=deadline)
        if self.holds is not None:
            self.holds.live[id(submission)]["history"] = history
            self.holds.update(submission, identity=observed.to_mapping(),
                status="clearance_check", _timeout_seconds=deadline.remaining())

    async def resolve(self, submission, marker, deadline):
        lock = self.locks.setdefault(submission.profile_id, asyncio.Lock())
        acquired = False
        original = submission.identity
        binding = submission._last_correlation_value or submission._recovery_binding or submission._previous_turn_binding
        seen, matched = set(), False
        email = self.accounts.get(submission.profile_id)
        signed_in = kind_for(marker) == "sign_in" and bool(email)
        restored_conversation = False
        try:
            if not lock.locked():
                # A free asyncio lock is acquired without suspending. Publish
                # activity before begin can expose a hold to queued work.
                await lock.acquire()
                acquired = True
                self.active_profiles[submission.profile_id] = id(submission)
            # Publish retention before waiting for the peer's authentication.
            if self.holds is not None:
                await self.holds.begin(submission, deadline, blocker_kind=kind_for(marker))
                deadline = self.holds.operation_deadline(submission, deadline)
            if not acquired:
                await chat._await_call(lock.acquire, _deadline=deadline)
                acquired = True
                self.active_profiles[submission.profile_id] = id(submission)
            while not deadline.expired():
                await self._same_tab(submission, deadline)
                await self.poll_commands(submission, deadline)
                marker = await chat._visible_access_block_marker(submission.page, submission.spec, _deadline=deadline)
                if marker is None:
                    if signed_in:
                        if (origin(submission.page.url) == "https://copilot.com" and not
                                await chat.visible_items(submission.page, submission.spec.composer_selectors, _deadline=deadline)):
                            await asyncio.sleep(min(.2, max(0, deadline.remaining())))
                            continue
                        await self._restore_after_sign_in(submission, original, binding, email, deadline)
                    else:
                        observed = await self._same_tab(submission, deadline)
                        original.assert_same_tab(observed, require_same_conversation=not submission._submitted,
                            canonical=lambda ref: chat._canonical_reference(ref, submission.spec))
                        if submission._submitted and not submission._accept_live_identity(observed):
                            raise IdentityMismatchError("copilot_blocker_conversation_changed")
                        if submission._prepared and not submission._submitted:
                            if await chat._await_call(chat.read_copilot_composer_value,
                                    submission._composer, _deadline=deadline) != submission._prompt:
                                raise ProviderAccessBlockedError("copilot_blocker_prepared_prompt_changed")
                    await self.validated(submission, deadline)
                    return
                self._update(submission, marker, deadline=deadline)
                signed_in = signed_in or kind_for(marker) == "sign_in" and bool(email)
                if marker == "human_verification_required":
                    # This is the same held episode: do not publish a second
                    # hold or reset its deadline. The native latch stays owned.
                    if submission._verification_attempts >= 2:
                        raise ProviderAccessBlockedError("copilot_verification_episode_limit")
                    if signed_in:
                        original_url = binding["identity"]["conversation_reference"] if binding else original.conversation_reference
                        if chat._is_conversation_url(original_url, submission.spec):
                            canonical = chat._canonical_reference(original_url, submission.spec)
                            if chat._canonical_reference(submission.page.url, submission.spec) != canonical:
                                if binding is None or restored_conversation:
                                    raise ProviderAccessBlockedError("copilot_sign_in_original_conversation_unproven")
                                restored_conversation = True
                                await chat._await_call(submission.page.goto, canonical,
                                    wait_until="domcontentloaded", _deadline=deadline)
                                continue  # Recognize the new surface before any input.
                        elif chat._is_blank_provider_start_url(submission.page.url,
                                submission.spec.start_url, aliases=submission.spec.blank_start_url_aliases):
                            submission._identity = await self._same_tab(submission, deadline)
                    submission._verification_attempts += 1
                    try:
                        await self.verification(submission, deadline)
                    except HumanVerificationRequiredError as error:
                        if getattr(error, "verification_input_attempted", None) is not False:
                            raise
                        if await chat._visible_access_block_marker(submission.page,
                                submission.spec, _deadline=deadline) == marker:
                            raise
                    while not deadline.expired() and await chat._visible_access_block_marker(
                            submission.page, submission.spec, _deadline=deadline) == marker:
                        await self.poll_commands(submission, deadline)
                        await asyncio.sleep(min(.2, max(0, deadline.remaining())))
                    continue
                page, control, state = submission.page, None, None
                host = origin(page.url)
                if marker == "cookie_notice":
                    notices = await chat.visible_items(page, submission.spec.cookie_notice_selectors, _deadline=deadline)
                    if len(notices) == 1:
                        control = await self._one_button(notices[0], "Reject", deadline)
                        state = "reject_optional_cookies"
                elif kind_for(marker) == "sign_in" and email:
                    if submission._submitted and binding is None:
                        self._update(submission, marker, reason="sign_in_send_binding_unavailable", deadline=deadline)
                    elif binding is None and chat._is_conversation_url(original.conversation_reference, submission.spec):
                        self._update(submission, marker, reason="sign_in_history_binding_unavailable", deadline=deadline)
                    elif host == "https://copilot.com":
                        control = await self._one_button(page, "Sign in", deadline)
                        if control is None:
                            control = await self._one_button(page, "Sign in with Microsoft", deadline)
                        state = "start_sign_in"
                    elif host == "https://login.live.com":
                        if await self._heading(page, "Choose an account", deadline):
                            control = await self._account_control(page, email, deadline)
                            state, matched = "choose_expected_account", control is not None
                        elif await self._heading(page, "Sign in on Google", deadline) and matched:
                            control = await self._one_button(page, "Next", deadline)
                            state = "google_federation"
                        elif await self._heading(page, "Stay signed in?", deadline):
                            labels = await self._visible(page.get_by_text(email, exact=True), deadline)
                            if len(labels) == 1:
                                control = await self._one_button(page, "No", deadline)
                                state = "session_only"
                    elif host == "https://accounts.google.com" and matched:
                        control = await self._account_control(page, email, deadline, google=True)
                        state = "choose_expected_google_account"
                if control is not None:
                    signature = (host, state)
                    if len(seen) >= 8 and signature not in seen:
                        raise ProviderAccessBlockedError("copilot_blocker_action_loop_refused")
                    if signature not in seen:
                        seen.add(signature)
                        try:
                            await self._click(submission, control, deadline, expected_origin=host)
                        except AsyncActionError as error:
                            if kind_for(marker) != "sign_in":
                                raise
                            receipt = error.receipt
                            reason = error.reason if error.reason in SIGN_IN_ACTION_REASONS else "action_failed"
                            phase = receipt.phase if receipt.phase in {"preparation", "dispatch"} else "unknown"
                            self._update(submission, marker,
                                reason="copilot_sign_in_action_" + reason + "_" + phase, deadline=deadline)
                            if (receipt.status != "not_attempted" or receipt.phase != "preparation"
                                    or receipt.possibly_dispatched):
                                raise
                            # A saved session can navigate during preparation.
                            # Keep the original episode budget and seen latch.
                            await self._same_tab(submission, deadline)
                            observed_marker = await chat._visible_access_block_marker(
                                page, submission.spec, _deadline=deadline)
                            if observed_marker is not None and kind_for(observed_marker) != "sign_in":
                                raise
                            continue  # Existing account/history restoration must still validate.
                        signed_in = signed_in or kind_for(marker) == "sign_in"
                elif kind_for(marker) == "sign_in":
                    if not email:
                        self._update(submission, marker, reason="sign_in_account_not_configured", deadline=deadline)
                    elif not (submission._submitted and binding is None) and not (
                            binding is None and chat._is_conversation_url(original.conversation_reference, submission.spec)):
                        self._update(submission, marker,
                            reason="sign_in_expected_account_unavailable" if state == "choose_expected_account"
                            else "sign_in_requires_manual_step", deadline=deadline)
                await asyncio.sleep(min(.2, max(0, deadline.remaining())))
            raise ProviderAccessBlockedError("copilot_blocker_deadline_expired")
        except BaseException as error:
            error.access_blocker_handled = True
            self.failed(submission, error, deadline)
            raise
        finally:
            if acquired:
                self.active_profiles.pop(submission.profile_id, None)
                lock.release()
