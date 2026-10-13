"""Native regeneration preserves the exact logical turn and prior transcript.

Intent: docs/lifecycle-and-resume.md (saved send attempts/native regeneration)
and AGENTS.md (one fresh, same-chat two-turn observation). These in-memory
protocol-shaped nodes add no user message when the provider Retry is clicked.
Strict tab identity and retained input use fixture seams; preparation, dispatch
qualification, response capture, correlation publication, transcript validation
and reopening use the production code.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from support.conversation import Item, Locator, Page, SPEC, identity
from support.browser_actions import SyntheticBrowserActions
from visibility_runner.errors import IdentityMismatchError
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.conversation_binding import (
    ConversationCorrespondenceError,
    text_digest,
    verify_conversation_history,
)


NATIVE_SPEC = replace(
    SPEC,
    terminal_error_within_response_selectors=("error",),
    native_retry_within_response_selectors=("retry",),
    terminal_error_markers=("something went wrong",),
)
FINAL_ANSWER = "Alpha Clinic"


@pytest.fixture(autouse=True)
def synthetic_input(monkeypatch):
    class VerifiedActions(SyntheticBrowserActions):
        async def retain(self, locator, deadline):
            target = await super().retain(locator, deadline)
            target.native = SimpleNamespace(handle=await locator.element_handle())
            return target

        async def click(self, target, deadline, verify):
            await verify(deadline)
            return await super().click(target, deadline, verify)

    monkeypatch.setattr(chat, "SharedBrowserActions", VerifiedActions)


class Message(Item):
    def __init__(self, text, message_id, *, branch="original-branch", children=None):
        super().__init__(text, message_id)
        self.branch = branch
        self.children = children or {}

    def locator(self, selector):
        return Locator(self.children.get(selector, ()))

    async def get_attribute(self, key):
        if key == "data-branch-id":
            return self.branch
        return await super().get_attribute(key)


class RetryControl:
    def __init__(self, page):
        self.page = page
        self.calls = []
        self.replace_answer = False
        self.mutate = None

    async def element_handle(self):
        class Handle:
            async def evaluate(_self, _script, current):
                return current.owner is self

            async def dispose(_self):
                pass

        result = Handle()
        result.owner = self
        return result

    async def click(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("trial"):
            return
        if self.replace_answer:
            self.page.answers[-1] = Message(FINAL_ANSWER, "regenerated-final")
        if self.mutate is not None:
            self.mutate(self.page)


def native_submission(monkeypatch, turn):
    async def inspect(page, **_kwargs):
        return identity(page, tab=getattr(page, "checkpoint_tab", "native-original-tab"))

    monkeypatch.setattr(chat, "strict_page_identity", inspect)
    page = Page()
    page.checkpoint_tab = "native-original-tab"
    page.url = "https://chatgpt.com/c/native-checkpoint"
    page.users = [Message("measurement", "user-1")]
    page.answers = []
    if turn == 2:
        page.answers.append(Message("Prior Clinic", "prior-answer"))
        page.users.append(Message("name extraction", "user-2"))
    prompt = page.users[-1].text
    prior_text = page.answers[-1].text if page.answers else ""
    control = RetryControl(page)
    page.answers.append(Message("", "failed-answer", children={
        "error": [Message("Something went wrong", "provider-error")],
        "retry": [control],
    }))
    submission = chat.PageSubmission(
        page=page, profile_id="fixture", chrome_directory="Profile 99",
        observation_id="native-checkpoint", spec=NATIVE_SPEC,
        identity=identity(page, tab=page.checkpoint_tab),
    )
    submission._prompt = prompt
    submission._submitted = True
    submission._before_answer_text = prior_text
    submission._response_baseline = chat.ResponseBaseline(turn - 1, text_digest(prior_text))
    submission._confirmed_failure_signature = "provider_terminal_error:something went wrong"
    return submission, control, prompt


def verify_binding(saved, page, prompt):
    async def verify():
        history = await chat._conversation_history(page, NATIVE_SPEC)
        return verify_conversation_history(
            saved, {"identity": identity(page, tab=page.checkpoint_tab).to_mapping(), "transcript": history},
            prefixes=NATIVE_SPEC.conversation_url_prefixes,
            prompt_sha256=text_digest(prompt), expected_profile_id="fixture",
            expected_chrome_directory="Profile 99",
        )
    return verify()


@pytest.mark.parametrize("turn", [1, 2], ids=["measurement", "extraction"])
def test_native_regeneration_keeps_logical_turn_in_saved_checkpoint(monkeypatch, turn):
    async def run():
        submission, control, prompt = native_submission(monkeypatch, turn)
        rows = []
        assert await submission.prepare_native_retry()
        submission.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        try:
            await submission.submit_once()
            await submission._checkpoint_conversation()
            assert rows[-1]["expected_turn"] == turn
            assert len(submission.page.users) == turn
            assert len([call for call in control.calls if not call.get("trial")]) == 1
            proof = await verify_binding(rows[-1], submission.page, prompt)
            assert proof["expected_turn"] == turn and proof["correspondence_verified"] is True
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("turn", [1, 2], ids=["measurement", "extraction"])
def test_native_regeneration_captures_replaced_answer_and_reopens_exact_turn(monkeypatch, turn):
    async def run():
        submission, control, prompt = native_submission(monkeypatch, turn)
        rows = []
        assert await submission.prepare_native_retry()
        submission.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        control.replace_answer = True
        try:
            await submission.submit_once()
            answer = await submission.wait_response()
            assert answer.text == FINAL_ANSWER
            assert submission._correlation_error is None
            saved = rows[-1]
            assert saved["expected_turn"] == turn
            assert saved["transcript"]["assistant_ids"][-1] == "regenerated-final"
            proof = await verify_binding(saved, submission.page, prompt)
            assert proof["response_present"] is True

            # Recovery observes this exact final transcript in a newly owned tab;
            # it never clicks Retry or adds a replacement user message.
            restored_page = Page()
            restored_page.checkpoint_tab = "native-reopened-tab"
            restored_page.users = deepcopy(submission.page.users)
            restored_page.answers = deepcopy(submission.page.answers)
            factory_calls = []
            async def factory(*args):
                factory_calls.append(args)
                return restored_page
            provider = chat.PageProvider(factory, NATIVE_SPEC)
            restored = await provider.reopen(saved, observation_id="native-checkpoint")
            try:
                assert restored.identity.conversation_reference == saved["identity"]["conversation_reference"]
                assert restored.identity.tab_reference != saved["identity"]["tab_reference"]
                assert len(factory_calls) == 1
                assert len(restored_page.users) == turn
                assert len([call for call in control.calls if not call.get("trial")]) == 1
            finally:
                await restored.close()
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("turn", [1, 2], ids=["measurement", "extraction"])
def test_failed_wrapper_checkpoint_preserves_physical_baseline_without_pinning_answer(monkeypatch, turn):
    async def run():
        submission, control, prompt = native_submission(monkeypatch, turn)
        rows = []
        assert await submission.prepare_native_retry()
        assert submission.response_baseline.to_mapping() == {
            "visible_response_count": turn, "newest_response_sha256": text_digest("")}
        submission.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        try:
            # The old failed wrapper is still present while the admitted native
            # action awaits a final response. It is not the awaited answer's ID.
            await submission.submit_once()
            await submission._checkpoint_conversation()
            saved = rows[-1]
            assert saved["response_baseline"] == submission.response_baseline.to_mapping()
            assert saved["expected_turn"] == turn
            assert saved["transcript"]["prior_user_sha256"] == (
                [text_digest("measurement")] if turn == 2 else [])
            assert saved["transcript"]["prior_assistant_sha256"] == (
                [text_digest("Prior Clinic")] if turn == 2 else [])
            assert "failed-answer" not in saved["transcript"]["assistant_ids"]
            submission.page.answers[-1] = Message(FINAL_ANSWER, "regenerated-final")
            answer = await submission.wait_response()
            assert answer.text == FINAL_ANSWER
            proof = await verify_binding(rows[-1], submission.page, prompt)
            assert proof["expected_turn"] == turn and proof["response_present"] is True
            assert len([call for call in control.calls if not call.get("trial")]) == 1
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("turn", [1, 2], ids=["measurement", "extraction"])
def test_reopened_native_attempt_recovers_replaced_original_response_without_resend(monkeypatch, turn):
    async def run():
        submission, control, prompt = native_submission(monkeypatch, turn)
        rows = []
        assert await submission.prepare_native_retry()
        submission.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        try:
            await submission.submit_once()
            await submission._checkpoint_conversation()
            saved = deepcopy(rows[-1])
            await submission._stop_conversation_watch()
            reopened = Page()
            reopened.checkpoint_tab = "native-recovery-tab"
            reopened.users = deepcopy(submission.page.users)
            reopened.answers = deepcopy(submission.page.answers[:-1]) + [Message("", "failed-answer")]
            factory_calls = []
            async def factory(*args):
                factory_calls.append(args)
                return reopened
            restored = await chat.PageProvider(factory, NATIVE_SPEC).reopen(
                saved, observation_id="native-checkpoint")
            try:
                restored.restore_recovery_attempt(prompt=prompt,
                    response_baseline=saved["response_baseline"], binding=saved)
                # The originally admitted native response finishes after reopen.
                # Recovery inspects it and must not admit another Retry/send.
                reopened.answers[-1] = Message(FINAL_ANSWER, "regenerated-final")
                outcome = await restored.reconcile_no_response(.3)
                assert outcome.disposition is chat.ReconciliationDisposition.LATE_RESPONSE_CAPTURED
                assert outcome.answer.text == FINAL_ANSWER
                assert len(factory_calls) == 1 and len(reopened.users) == turn
                assert len([call for call in control.calls if not call.get("trial")]) == 1
            finally:
                await restored.close()
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("drift", ["user", "prior-answer", "url", "branch"])
def test_native_regeneration_still_refuses_unrelated_transcript_drift(monkeypatch, drift):
    async def run():
        submission, control, _prompt = native_submission(monkeypatch, 2)
        rows = []
        assert await submission.prepare_native_retry()
        submission.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
        # Publish the original exact identity before an unrelated external change.
        await submission._checkpoint_conversation()
        before = deepcopy(rows)
        def mutate(page):
            if drift == "user":
                page.users[0].identity = "unrelated-user"
            elif drift == "prior-answer":
                page.answers[0].identity = "unrelated-prior-answer"
            elif drift == "url":
                page.url = "https://chatgpt.com/c/another-conversation"
            else:
                page.answers[-1].branch = "another-branch"
        control.mutate = mutate
        control.replace_answer = True
        try:
            await submission.submit_once()
            with pytest.raises((IdentityMismatchError, ConversationCorrespondenceError)):
                await submission.wait_response()
            assert rows == before
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("drift", ["prior-user-id", "prior-assistant-id", "branch"])
def test_native_preparation_preserves_known_history_before_first_checkpoint(monkeypatch, drift):
    async def run():
        submission, control, _prompt = native_submission(monkeypatch, 2)
        # Exact state captured before the extraction user message was submitted.
        # No canonical correlation checkpoint has been published yet; those
        # already-known earlier identities must still survive regeneration.
        original = await chat._conversation_history(submission.page, NATIVE_SPEC)
        submission._history_before = {
            **original,
            "user_sha256": original["user_sha256"][:1],
            "assistant_sha256": original["assistant_sha256"][:1],
            "user_ids": original["user_ids"][:1],
            "assistant_ids": original["assistant_ids"][:1],
        }
        assert submission._last_correlation_value is None
        assert submission._recovery_binding is None
        if drift == "prior-user-id":
            submission.page.users[0].identity = "unrelated-prior-user"
        elif drift == "prior-assistant-id":
            submission.page.answers[0].identity = "unrelated-prior-answer"
        else:
            for message in submission.page.users + submission.page.answers:
                message.branch = "unrelated-branch"
        try:
            with pytest.raises((IdentityMismatchError, ConversationCorrespondenceError)):
                await submission.prepare_native_retry()
            assert submission._native_retry_prepared is False
            assert not any(not call.get("trial") for call in control.calls)
        finally:
            await submission.close()
    asyncio.run(asyncio.wait_for(run(), 5))


def test_same_tab_confirmed_native_failure_restores_logical_turn_from_durable_binding(monkeypatch):
    async def run():
        original, control, prompt = native_submission(monkeypatch, 1)
        restored = None
        rows = []
        try:
            assert await original.prepare_native_retry()
            original.set_correlation_checkpoint(lambda value: rows.append(deepcopy(value)))
            await original.submit_once()
            await original._checkpoint_conversation()
            await original._stop_conversation_watch()
            saved = rows[-1]
            assert saved["expected_turn"] == 1
            assert saved["response_baseline"]["visible_response_count"] == 1
            # The admitted native attempt subsequently failed with partial model
            # content plus the same scoped terminal error; its physical baseline
            # is one wrapper although its logical turn remains measurement one.
            original.page.answers[-1].text = "Partial rejected answer"
            dispatched = len([call for call in control.calls if not call.get("trial")])
            async def factory(*_args):
                return original.page
            restored = await chat.PageProvider(factory, NATIVE_SPEC).restore(
                original.identity, observation_id="native-checkpoint")
            restored.restore_confirmed_attempt(prompt=prompt,
                response_baseline=saved["response_baseline"],
                failure_classification="provider_terminal_error:something went wrong",
                native_retry=True, binding=saved)
            proof = await restored.reconcile_no_response(.3)
            assert proof.disposition is chat.ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
            assert proof.native_retry_available is True
            assert await restored.prepare_native_retry()
            checkpoints = []
            restored.set_correlation_checkpoint(lambda value: checkpoints.append(deepcopy(value)))
            await restored._checkpoint_conversation()
            assert checkpoints[-1]["expected_turn"] == 1
            assert len(original.page.users) == 1
            assert len([call for call in control.calls if not call.get("trial")]) == dispatched
            correspondence = await verify_binding(checkpoints[-1], original.page, prompt)
            assert correspondence["expected_turn"] == 1
        finally:
            if restored is not None:
                await restored.close()
            await original.close()
    asyncio.run(asyncio.wait_for(run(), 5))
