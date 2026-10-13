"""Website error proof stays outside narrowed model text; synthetic DOM only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visibility_runner.websites import chat_actions as base  # noqa: E402
from visibility_runner.websites.gemini import GEMINI_SPEC  # noqa: E402
from visibility_runner.websites.chatgpt import CHATGPT_SPEC  # noqa: E402
from visibility_runner.errors import ProviderAccessBlockedError  # noqa: E402
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore  # noqa: E402

from support.access_walls import Locator, Node, Page, local_spec  # noqa: E402
from support.polling import clock  # noqa: E402, F401
import support.retries as worker_fixtures  # noqa: E402


PARTIAL = "Synthetic incomplete model content."
TERMINAL = "Something went wrong"


class ScopedPage(Page):
    def __init__(self, *, narrowed=True, provider_spec=GEMINI_SPEC):
        spec = replace(local_spec(provider_spec), response_timeout_seconds=0.65)
        if not narrowed:
            spec = replace(spec, response_content_selectors=())
        super().__init__(spec)
        self.wrappers = []
        self.busy = False
        self.retry = Node("Retry")

    def locator(self, selector):
        if selector == "response":
            return Locator(self.wrappers)
        if selector == ", ".join(self.spec.stop_selectors):
            return Locator((Node("Stop"),) if self.busy else ())
        return super().locator(selector)

    def wrapper(self, text, *, error=None, error_visible=True, retry=False):
        descendants = {
            ", ".join(self.spec.terminal_error_within_response_selectors): (
                (Node(error, visible=error_visible),) if error else ()
            ),
            ", ".join(self.spec.native_retry_within_response_selectors): (
                (self.retry,) if retry else ()
            ),
        }
        if self.spec.response_content_selectors:
            descendants[", ".join(self.spec.response_content_selectors)] = (
                (Node(text),) if text is not None else ()
            )
        wrapper_text = "\n".join(value for value in (text, error) if value)
        return Node(wrapper_text, descendants=descendants)


def make_submission(page, *, before_count=0, before_text="", native_retry=False):
    submission = base.PageSubmission(
        page=page,
        profile_id="synthetic",
        chrome_directory="Synthetic isolated profile",
        observation_id="synthetic-terminal-scope",
        spec=page.spec,
        identity=base.BrowserIdentity(
            profile_id="synthetic",
            chrome_directory="Synthetic isolated profile",
            browser_context_reference=page.browser_context_id,
            tab_reference=page.window_name,
            conversation_reference=page.url,
        ),
    )
    submission._prompt = page.prompt
    submission._submitted = True
    submission._before_answer_text = before_text
    submission._response_baseline = base.ResponseBaseline(
        visible_response_count=before_count,
        newest_response_sha256=hashlib.sha256(before_text.encode()).hexdigest(),
    )
    submission._native_retry_sent = native_retry
    return submission


async def poll(page, *, before_count=0, before_text="", native_retry=False):
    return await base.wait_for_stable_answer(
        page,
        page.spec,
        before_count=before_count,
        before_answer=before_text,
        allow_same_node_replacement=native_retry,
    )


async def probe_submission(submission):
    # Production reconciliation always supplies a bounded probe deadline.
    return await submission._reconciliation_probe(
        _deadline=base._BrowserOperationDeadline.after(
            1.0, timeout_message="synthetic bounded reconciliation probe"
        )
    )


@pytest.mark.parametrize(("narrowed", "text"), (
    (False, PARTIAL), (False, ""),
    (True, PARTIAL), (True, ""), (True, None),
))
def test_newest_provider_error_blocks_partial_empty_or_missing_content(
    clock, narrowed, text
):
    page = ScopedPage(narrowed=narrowed)
    page.wrappers = [page.wrapper(text, error=TERMINAL)]

    with pytest.raises(TimeoutError):
        asyncio.run(poll(page))
    # A proven terminal card enters reconciliation promptly, not at budget end.
    assert clock.value == 1000.0
    assert page.send.clicks == []
    assert page.retry.clicks == []


@pytest.mark.parametrize(("narrowed", "text"), (
    (False, PARTIAL), (False, ""),
    (True, PARTIAL), (True, ""), (True, None),
))
def test_newest_provider_error_preserves_reconciliation_proof_without_answer_text(
    clock, narrowed, text
):
    page = ScopedPage(narrowed=narrowed)
    page.wrappers = [page.wrapper(text, error=TERMINAL, retry=True)]
    submission = make_submission(page)

    async def exercise():
        probe = await probe_submission(submission)
        assert probe.identity_valid and probe.prompt_exact
        assert probe.new_response is False
        assert probe.evidence["response_changed_from_baseline"] is True
        assert probe.retryable_failure == "provider_terminal_error:something went wrong"
        assert probe.native_retry_count == 1
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
        assert result.answer is None
        assert result.native_retry_available is True
        assert len(result.evidence["checks"]) >= 2

    asyncio.run(exercise())
    assert page.send.clicks == []
    assert page.retry.clicks == []


@pytest.mark.parametrize(
    "text",
    (PARTIAL, "The phrase Something went wrong is ordinary model prose."),
    ids=("ordinary-partial", "error-like-prose"),
)
def test_model_text_without_provider_error_keeps_existing_capture_semantics(clock, text):
    page = ScopedPage()
    page.body_text = TERMINAL
    page.prompt = "A user can also quote Something went wrong."
    page.wrappers = [page.wrapper(text)]
    submission = make_submission(page)

    async def exercise():
        assert await poll(page) == text
        probe = await probe_submission(submission)
        assert probe.new_response is True
        assert probe.retryable_failure is None
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.LATE_RESPONSE_CAPTURED
        assert result.answer.text == text

    asyncio.run(exercise())
    assert page.retry.clicks == []


def test_older_wrapper_error_cannot_poison_newest_valid_response(clock):
    page = ScopedPage()
    page.wrappers = [
        page.wrapper("Synthetic older content.", error=TERMINAL, retry=True),
        page.wrapper(PARTIAL),
    ]
    submission = make_submission(page, before_count=1, before_text="Synthetic older content.")

    async def exercise():
        assert await poll(page, before_count=1, before_text="Synthetic older content.") == PARTIAL
        probe = await probe_submission(submission)
        assert probe.new_response is True
        assert probe.retryable_failure is None
        assert probe.native_retry_count == 0
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.LATE_RESPONSE_CAPTURED

    asyncio.run(exercise())
    assert page.retry.clicks == []


@pytest.mark.parametrize("text", (PARTIAL, ""), ids=("partial", "empty"))
@pytest.mark.parametrize("native_retry", (False, True))
def test_unchanged_current_wrapper_error_never_becomes_post_send_retry_proof(
    clock, text, native_retry
):
    page = ScopedPage()
    page.wrappers = [page.wrapper(text, error=TERMINAL, retry=True)]
    submission = make_submission(page, before_count=1, before_text=text, native_retry=native_retry)

    async def exercise():
        probe = await probe_submission(submission)
        assert probe.evidence["response_changed_from_baseline"] is False
        assert probe.new_response is False
        assert probe.retryable_failure is None
        assert probe.native_retry_count == 0
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.AMBIGUOUS
        assert result.native_retry_available is False

    asyncio.run(exercise())
    assert page.retry.clicks == []


def test_busy_provider_error_suppresses_capture_but_cannot_authorize_retry(clock):
    page = ScopedPage()
    page.busy = True
    page.wrappers = [page.wrapper(PARTIAL, error=TERMINAL, retry=True)]
    submission = make_submission(page)

    async def exercise():
        with pytest.raises(TimeoutError):
            await poll(page)
        assert clock.value == pytest.approx(1000.65)
        probe = await probe_submission(submission)
        assert probe.new_response is False
        assert probe.busy is True
        assert probe.retryable_failure is not None
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.AMBIGUOUS
        assert result.native_retry_available is False

    asyncio.run(exercise())
    assert page.send.clicks == []
    assert page.retry.clicks == []


def test_hidden_provider_error_does_not_block_model_content(clock):
    page = ScopedPage()
    page.wrappers = [page.wrapper(PARTIAL, error=TERMINAL, error_visible=False)]
    assert asyncio.run(poll(page)) == PARTIAL
    probe = asyncio.run(probe_submission(make_submission(page)))
    assert probe.new_response is True
    assert probe.retryable_failure is None


@pytest.mark.parametrize(("notice", "classification"), (
    ("Failed to fetch Retry", "provider_transport_failure"),
    ("Connection interrupted. Waiting for the complete answer", "provider_connection_interrupted"),
    ("Unusual activity has been detected from your device. Try again later. Retry", "provider_access_blocked:unusual_activity"),
    ("You've hit your limit. Please try again later.", "provider_access_blocked:usage_limit"),
    ("You've reached your message limit. Try again tomorrow at 2am.", "provider_access_blocked:usage_limit"),
))
@pytest.mark.parametrize("has_content", (False, True), ids=("no-answer", "partial-answer"))
def test_owned_turn_notices_never_become_answers_or_retry_authority(
    monkeypatch, clock, notice, classification, has_content
):
    page = ScopedPage(provider_spec=CHATGPT_SPEC)
    if has_content:
        page.wrappers = [page.wrapper(PARTIAL, error=TERMINAL, retry=True)]
    submission = make_submission(page)
    if classification != "provider_connection_interrupted":
        submission._identity = replace(submission.identity, conversation_reference=CHATGPT_SPEC.start_url)
        page.url = "https://chatgpt.com/c/local-chatgpt%3Asynthetic"

    async def notices(_page, _spec, expected_prompt, **_kwargs):
        assert expected_prompt == page.prompt
        return (notice,)

    monkeypatch.setattr(base, "current_turn_notices", notices)

    async def exercise():
        if classification.startswith("provider_access_blocked:"):
            with pytest.raises(ProviderAccessBlockedError) as error:
                await submission.wait_response()
            assert error.value.reason == classification.partition(":")[2]
            if error.value.reason == "usage_limit" and "2am" in notice:
                assert error.value.usage_limit["reset_clock"] == "02:00"
        else:
            with pytest.raises(TimeoutError):
                await submission.wait_response()
        if classification == "provider_connection_interrupted":
            assert clock.value == pytest.approx(1000.65)
        else:
            assert clock.value == 1000.0
        probe = await probe_submission(submission)
        assert probe.website_failure == classification
        assert probe.new_response is False
        assert probe.retryable_failure is None
        assert probe.native_retry_count == 0
        result = await submission.reconcile_no_response(0.6)
        assert result.disposition is base.ReconciliationDisposition.AMBIGUOUS
        assert result.failure_classification == classification
        assert result.answer is None
        assert result.native_retry_available is False
        if classification == "provider_connection_interrupted":
            assert clock.value == pytest.approx(1001.25)

    asyncio.run(exercise())
    assert page.send.clicks == []
    assert page.retry.clicks == []


def test_tools_limit_notice_preserves_valid_text_and_reset_time(monkeypatch, clock):
    page = ScopedPage(provider_spec=CHATGPT_SPEC)
    page.wrappers = [page.wrapper(PARTIAL)]
    submission = make_submission(page)
    notice = (
        "Files, images, and data analysis are unavailable until usage resets "
        "tomorrow at 2:00\u202fAM. Continue chatting with text only, or upgrade for more access."
    )

    async def notices(*_args, **_kwargs):
        return (notice,)

    monkeypatch.setattr(base, "current_turn_notices", notices)

    async def exercise():
        answer = await submission.wait_response()
        assert answer.text == PARTIAL
        limit = answer.capture_evidence["usage_limit"]
        assert limit["scope"] == "tools" and limit["text_allowed"] is True
        assert limit["reset_day"] == "tomorrow" and limit["reset_clock"] == "02:00"
        assert limit["reset_timezone"] is None
        probe = await probe_submission(submission)
        assert probe.website_failure is None and probe.new_response is True
        submission.next_turn()
        assert submission.usage_limit_snapshot() is None

    asyncio.run(exercise())
    assert page.send.clicks == page.retry.clicks == []


@pytest.mark.parametrize(("notice", "classification"), (
    ("Failed to fetch Retry", "provider_transport_failure"),
    ("Connection interrupted. Waiting for the complete answer", "provider_connection_interrupted"),
    ("Unusual activity has been detected from your device. Try again later. Retry", "provider_access_blocked:unusual_activity"),
))
def test_owned_turn_notice_retains_one_send_and_never_opens_peer(
    tmp_path, monkeypatch, clock, notice, classification
):
    page = ScopedPage(provider_spec=CHATGPT_SPEC)
    observation_id = "synthetic-owned-turn-notice"
    run_id = "synthetic-owned-turn-notice-run"
    driver = worker_fixtures._ScriptedDriver("chatgpt", {
        observation_id: ((worker_fixtures._timeout(),),),
    })
    shadows = {}

    async def notices(_page, _spec, expected_prompt, **_kwargs):
        assert expected_prompt == page.prompt
        return (notice,)

    async def wait_response(scripted):
        page.browser_context_id = scripted.identity.browser_context_reference
        page.browser_target_id = scripted.identity.browser_target_reference.removeprefix("cdp-target:")
        page.window_name = scripted.identity.tab_reference
        page.url = scripted.identity.conversation_reference
        page.prompt = scripted.current_prompt
        shadow = make_submission(page)
        shadow.profile_id = scripted.identity.profile_id
        shadow.chrome_directory = scripted.identity.chrome_directory
        shadow._identity = scripted.identity
        if classification != "provider_connection_interrupted":
            shadow._identity = replace(shadow.identity, conversation_reference=CHATGPT_SPEC.start_url)
            page.url = "https://chatgpt.com/c/local-chatgpt%3Asynthetic"
        shadows[id(scripted)] = shadow
        return await shadow.wait_response()

    async def reconcile(scripted, grace_seconds):
        return await shadows[id(scripted)].reconcile_no_response(grace_seconds)

    monkeypatch.setattr(base, "current_turn_notices", notices)
    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "wait_response", wait_response)
    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "reconcile_no_response", reconcile)

    async def exercise():
        repository, result = await worker_fixtures._execute_raw(
            tmp_path,
            worker_fixtures._raw_plan(run_id, [
                worker_fixtures._observation(observation_id, "chatgpt"),
                worker_fixtures._observation("synthetic-forbidden-peer", "chatgpt"),
            ], retry_policy=replace(worker_fixtures._policy(), late_response_grace_seconds=0.6)),
            worker_fixtures._registry(driver),
        )
        assert result.status == "needs_attention"
        assert result.completed_observation_ids == ()
        issue = result.issues[0]
        assert issue["code"] == "ambiguous_send"
        if classification.endswith("unusual_activity"):
            assert issue["detail"]["access_block"]["reason"] == "unusual_activity"
        else:
            assert issue["detail"]["failure_classification"] == classification
        records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
        assert len(records) == 1
        assert records[0].state is SendAttemptState.SEND_STARTED
        assert records[0].retry_decision in {None, "needs_attention"}
        assert records[0].retried_by is None
        assert records[0].response_reference is None

    asyncio.run(exercise())
    assert len(driver.opens) == len(driver.sent) == 1
    assert driver.native_retries == []
    assert driver.next_turns == []


def test_interrupted_turn_reason_survives_reconciliation_read_deadline(monkeypatch, clock):
    page = ScopedPage(provider_spec=CHATGPT_SPEC)
    submission = make_submission(page)

    async def notices(*_args, **_kwargs):
        return ("Connection interrupted. Waiting for the complete answer",)

    original_probe = submission._reconciliation_probe
    checks = 0

    async def probe(**kwargs):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise base._BrowserOperationDeadlineExceeded("synthetic expired DOM read")
        return await original_probe(**kwargs)

    monkeypatch.setattr(base, "current_turn_notices", notices)
    monkeypatch.setattr(submission, "_reconciliation_probe", probe)
    result = asyncio.run(submission.reconcile_no_response(0.6))
    assert result.disposition is base.ReconciliationDisposition.AMBIGUOUS
    assert result.failure_classification == "provider_connection_interrupted"
    assert result.native_retry_available is False
    assert page.send.clicks == page.retry.clicks == []


@pytest.mark.parametrize("changed", ("identity", "prompt"))
def test_terminal_card_cannot_overrule_identity_or_exact_prompt_proof(clock, changed):
    page = ScopedPage()
    page.wrappers = [page.wrapper(PARTIAL, error=TERMINAL, retry=True)]
    submission = make_submission(page)
    if changed == "identity":
        page.window_name = "synthetic-different-tab"
    else:
        page.prompt = "Synthetic unrelated current prompt."

    result = asyncio.run(submission.reconcile_no_response(0.6))
    assert result.disposition is base.ReconciliationDisposition.AMBIGUOUS
    assert result.native_retry_available is False
    assert page.retry.clicks == []


@pytest.mark.parametrize("stop", ("identity", "prompt", "authentication", "challenge", "busy"))
def test_scoped_error_fail_closed_guards_keep_one_send_and_never_open_peer(
    tmp_path, monkeypatch, clock, stop
):
    page = ScopedPage()
    observation_id = "synthetic-scoped-terminal"
    run_id = "synthetic-scoped-terminal-run"
    driver = worker_fixtures._ScriptedDriver("gemini", {
        observation_id: ((worker_fixtures._timeout(),),),
    })
    shadows = {}

    async def wait_response(scripted):
        page.browser_context_id = scripted.identity.browser_context_reference
        page.browser_target_id = scripted.identity.browser_target_reference.removeprefix("cdp-target:")
        page.window_name = scripted.identity.tab_reference
        page.url = scripted.identity.conversation_reference
        page.prompt = scripted.current_prompt
        page.wrappers = [page.wrapper(PARTIAL, error=TERMINAL, retry=True)]
        shadow = make_submission(page)
        shadow.profile_id = scripted.identity.profile_id
        shadow.chrome_directory = scripted.identity.chrome_directory
        shadow._identity = scripted.identity
        shadows[id(scripted)] = shadow
        if stop == "identity":
            page.window_name += ":changed"
        elif stop == "prompt":
            page.prompt = "Synthetic unrelated prompt."
        elif stop == "authentication":
            page.auth_wall = (Node("Connexion"),)
        elif stop == "challenge":
            page.challenge = (Node("Synthetic verification gate"),)
        else:
            page.busy = True
        return await shadow.wait_response()

    async def reconcile(scripted, grace_seconds):
        return await shadows[id(scripted)].reconcile_no_response(grace_seconds)

    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "wait_response", wait_response)
    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "reconcile_no_response", reconcile)

    async def exercise():
        repository, result = await worker_fixtures._execute_raw(
            tmp_path,
            worker_fixtures._raw_plan(run_id, [
                worker_fixtures._observation(observation_id, "gemini"),
                worker_fixtures._observation("synthetic-forbidden-peer", "gemini"),
            ], retry_policy=replace(worker_fixtures._policy(), late_response_grace_seconds=0.6)),
            worker_fixtures._registry(driver),
        )
        assert result.status == "needs_attention"
        assert result.completed_observation_ids == ()
        records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
        assert len(records) == 1
        assert records[0].state is SendAttemptState.SEND_STARTED
        assert records[0].retry_decision in {None, "needs_attention"}
        assert records[0].retried_by is None
        assert records[0].response_reference is None

    asyncio.run(exercise())
    assert len(driver.opens) == len(driver.sent) == 1
    assert driver.native_retries == []
    assert driver.next_turns == []
    assert page.retry.clicks == []


@pytest.mark.parametrize('platform', ('chatgpt', 'gemini'))
def test_poll_reads_one_response_wrapper_scope_and_never_page_body(clock, platform):
    from visibility_runner.websites.chatgpt import CHATGPT_SPEC

    spec = local_spec(CHATGPT_SPEC if platform == 'chatgpt' else GEMINI_SPEC)

    class CountedPage(Page):
        response_queries = {}

        def locator(self, selector):
            assert selector != 'body', 'full-page body text is outside the polling contract'
            if selector == 'response':
                self.response_queries[clock.value] = self.response_queries.get(clock.value, 0) + 1
            return super().locator(selector)

    page = CountedPage(spec, answer='Synthetic stable answer')
    assert asyncio.run(poll(page)) == page.answer
    # The immediate synthetic reads share one clock value within each poll.
    assert page.response_queries and all(count == 1 for count in page.response_queries.values())


def test_reconciliation_reuses_response_wrapper_for_terminal_and_retry_checks(clock):
    page = ScopedPage()
    page.wrappers = [page.wrapper('', error=TERMINAL, retry=True)]
    original_locator = page.locator
    queries = []

    def counted_locator(selector):
        assert selector != 'body'
        queries.append(selector)
        return original_locator(selector)

    page.locator = counted_locator
    probe = asyncio.run(probe_submission(make_submission(page)))
    assert probe.retryable_failure == 'provider_terminal_error:something went wrong'
    assert probe.native_retry_count == 1
    assert queries.count('response') == 1
