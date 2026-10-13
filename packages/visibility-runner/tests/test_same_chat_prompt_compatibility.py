"""Source-bound extraction preserves admitted historical prompts on recovery."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from support.execution import AnswerSpec, FakeDriver, FakeSubmission
from support.retries import (
    _ScriptedDriver,
    _answer,
    _execute_raw,
    _observation,
    _policy,
    _raw_plan,
    _registry,
    _runtime,
    _timeout,
    _typed_plan,
)
from visibility_runner.config import Platform
from visibility_runner.execution.manage_measurements import Runner
from visibility_runner.names import prompts
from visibility_runner.names.check_chat_names import MentionExtractionError
from visibility_runner.names.parse_source_answer import MAX_SOURCE_BYTES, source_parser_prompt, text_sha256
from visibility_runner.storage.measurement_files import MeasurementFiles
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore
from visibility_runner.websites.chat_actions import (
    BrowserIdentity,
    PageProvider,
    ProviderAnswer,
    ProviderStatus,
    ReconciliationDisposition,
    ReconciliationResult,
    ResponseBaseline,
)
from visibility_runner.websites.chatgpt import CHATGPT_SPEC


# These are frozen historical bytes, independent of the compatibility helper.
LEGACY_V2 = (
    "Return only the clinic/provider names you mentioned in your immediately "
    "preceding answer, in the same order. Preserve the exact spelling from that "
    "answer. Do not add explanations, classifications, offsets, or names you did "
    "not mention. Return a closed JSON array of strings; return [] if none. "
    "Do not wrap the array in Markdown or a code fence."
)
LEGACY_V3 = (
    "Return only the clinic/provider names you mentioned in your immediately "
    "preceding answer, in the same order. Copy each name as one uninterrupted "
    "verbatim text span from that answer, preserving spelling, accents, "
    "punctuation and spacing. Do not join a clinic name with an adjacent "
    "citation, source label, link title or other text. Preserve repeated name "
    "occurrences as separate entries. Do not add explanations, classifications, "
    "offsets, or names you did "
    "not mention. Return a closed JSON array of strings; return [] if none. "
    "Do not wrap the array in Markdown or a code fence."
)
NAMES = [
    "BioMotion Clinics – Physio and Sports Rehab",
    "BioMotion Clinics- Physio and Sports Rehab",
]
SOURCE = "\n".join(NAMES)
EXTRACTION = json.dumps(NAMES, ensure_ascii=False)


@pytest.mark.parametrize("legacy", [LEGACY_V2, LEGACY_V3], ids=["v2", "v3"])
def test_known_legacy_prompt_is_selected_by_its_admitted_hash(legacy):
    assert prompts.same_chat_extraction_prompt(
        SOURCE, expected_prompt_sha256=text_sha256(legacy)
    ) == legacy


def test_known_legacy_prompts_recover_when_source_exceeds_new_prompt_size_limit():
    oversized_source = "x" * (MAX_SOURCE_BYTES + 1)
    for legacy in (LEGACY_V2, LEGACY_V3):
        assert prompts.same_chat_extraction_prompt(
            oversized_source, expected_prompt_sha256=text_sha256(legacy)
        ) == legacy


def test_new_prompt_uses_exact_source_and_rejects_a_changed_source_hash():
    expected = source_parser_prompt(SOURCE)
    assert prompts.same_chat_extraction_prompt(SOURCE) == expected
    assert prompts.same_chat_extraction_prompt(
        SOURCE, expected_prompt_sha256=text_sha256(expected)
    ) == expected
    with pytest.raises(MentionExtractionError, match="same_chat_extraction_prompt_mismatch"):
        prompts.same_chat_extraction_prompt(
            SOURCE.replace("–", "-"), expected_prompt_sha256=text_sha256(expected)
        )


def test_non_copilot_prompt_remains_exact_source_parser_prompt():
    expected = source_parser_prompt(SOURCE)
    for platform in (None, "chatgpt", "gemini"):
        assert prompts.same_chat_extraction_prompt(SOURCE, platform=platform) == expected


def test_copilot_prompt_quotes_exact_source_and_reproduces_its_bound_hash():
    source = 'Clinic "Łódź"\nClinic "Łódź"\nA \\ B Clinic'
    prompt = prompts.same_chat_extraction_prompt(source, platform="copilot")
    assert json.loads(prompt.split("\n\nSource text:\n", 1)[1]) == source
    assert "every repeated occurrence" in prompt
    assert "closed JSON array" in prompt
    assert "Do no research and add no names absent from the source text" in prompt
    assert "source_sha256" not in prompt
    assert "Extraction contract" not in prompt
    for platform in (None, "copilot"):
        assert prompts.same_chat_extraction_prompt(
            source, platform=platform, expected_prompt_sha256=text_sha256(prompt)
        ) == prompt
    with pytest.raises(MentionExtractionError, match="same_chat_extraction_prompt_mismatch"):
        prompts.same_chat_extraction_prompt(
            source.replace("Łódź", "Lodz"),
            expected_prompt_sha256=text_sha256(prompt),
        )


def test_copilot_recovery_preserves_each_older_bound_prompt():
    for historical in (LEGACY_V2, LEGACY_V3, source_parser_prompt(SOURCE)):
        assert prompts.same_chat_extraction_prompt(
            SOURCE, platform="copilot", expected_prompt_sha256=text_sha256(historical)
        ) == historical


def _seed_chat(root, prompt, *, native=False, prepared_successor=False):
    observation = _observation("historical-chat", "chatgpt")
    plan = _raw_plan("historical-chat-run", [observation])
    repository = MeasurementFiles(root)
    repository.create(plan)
    steps = (
        (_answer(SOURCE), _timeout(native_retry_available=True), _answer(EXTRACTION))
        if native else (_answer(SOURCE), _answer(EXTRACTION))
    )
    driver = _ScriptedDriver("chatgpt", {observation["observation_id"]: (steps,)})
    submission = asyncio.run(driver.open(
        profile_id="profile_a", chrome_directory="Default",
        observation_id=observation["observation_id"], fresh=True,
    ))
    submission.step = 1
    identity = submission.identity.to_mapping()
    store = SendAttemptStore(repository.run_dir(plan["run_id"]) / "fences", watch_changes=False)
    source = store.prepare(
        fence_id="historical-chat:turn-1", observation_id="historical-chat",
        action="measurement_turn_1", prompt_sha256=text_sha256("Recommend a clinic"),
        identity=identity, logical_turn_id="historical-chat:turn-1",
        attempt_ordinal=1, observation_attempt_ordinal=1,
    )
    store.mark_prepared(source.fence_id)
    store.begin_send(source.fence_id)
    source_ref = repository.write_evidence(plan["run_id"], SOURCE, suffix=".turn-1.txt")
    store.capture_response(source.fence_id, response_reference=source_ref["path"],
                           response_sha256=source_ref["sha256"])
    store.acknowledge(source.fence_id, validation_status="collected", identity=identity)
    extraction = store.prepare(
        fence_id="historical-chat:turn-2", observation_id="historical-chat",
        action="same_chat_turn_2", prompt_sha256=text_sha256(prompt),
        identity=identity, logical_turn_id="historical-chat:turn-2",
        attempt_ordinal=1, observation_attempt_ordinal=1,
    )
    store.mark_prepared(extraction.fence_id,
                        response_baseline=ResponseBaseline(1, text_sha256(SOURCE)).to_mapping())
    if native:
        store.begin_send(extraction.fence_id)
        store.confirm_no_response(
            extraction.fence_id, failure_classification="visible_provider_terminal_error",
            reconciliation_evidence={"checks": [{"stable": True}, {"stable": True}]},
            native_retry_available=True, identity=identity,
        )
        if prepared_successor:
            extraction = store.prepare_retry(
                confirmed_fence_id=extraction.fence_id,
                successor_fence_id="historical-chat:turn-2:attempt-2", identity=identity,
                native_retry=True, attempt_ordinal=2, observation_attempt_ordinal=1,
            )
    return repository, plan, driver, store, source_ref, extraction


@pytest.mark.parametrize("legacy", [LEGACY_V2, LEGACY_V3], ids=["v2", "v3"])
def test_prepared_legacy_turn_two_resumes_with_its_original_prompt(tmp_path, legacy):
    repository, plan, driver, store, source_ref, _ = _seed_chat(tmp_path, legacy)
    try:
        source_bytes = repository.read_evidence(plan["run_id"], source_ref)
        _, outcome = asyncio.run(_execute_raw(tmp_path, plan, _registry(driver), resume=True))
        assert outcome.status == "succeeded"
        assert [row[3] for row in driver.sent] == [legacy]
        assert len(driver.opens) == 1
        assert store.load("historical-chat:turn-2").prompt_sha256 == text_sha256(legacy)
        assert repository.read_evidence(plan["run_id"], source_ref) == source_bytes
    finally:
        store.close()


@pytest.mark.parametrize("legacy", [LEGACY_V2, LEGACY_V3], ids=["v2", "v3"])
@pytest.mark.parametrize("prepared_successor", [False, True], ids=["confirmed", "prepared-native"])
def test_native_resume_keeps_the_legacy_turn_two_hash(tmp_path, legacy, prepared_successor):
    _, plan, driver, store, _, _ = _seed_chat(
        tmp_path, legacy, native=True, prepared_successor=prepared_successor
    )
    try:
        _, outcome = asyncio.run(_execute_raw(tmp_path, plan, _registry(driver), resume=True))
        assert outcome.status == "succeeded"
        assert [row[3] for row in driver.sent] == [legacy]
        assert len(driver.native_retries) == 1
        assert driver.prepared == []
        assert len(driver.opens) == 1
        successor = store.load("historical-chat:turn-2:attempt-2")
        assert successor.native_retry is True
        assert successor.prompt_sha256 == text_sha256(legacy)
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True], ids=["prepared", "confirmed-native"])
def test_unknown_turn_two_hash_is_refused_before_input(tmp_path, native):
    _, plan, driver, store, _, extraction = _seed_chat(
        tmp_path, "an unknown historical extraction prompt", native=native
    )
    try:
        prior_bytes = store.path(extraction.fence_id).read_bytes()
        _, outcome = asyncio.run(_execute_raw(tmp_path, plan, _registry(driver), resume=True))
        assert outcome.status != "succeeded"
        assert driver.sent == driver.prepared == driver.native_retries == []
        assert store.path(extraction.fence_id).read_bytes() == prior_bytes
    finally:
        store.close()


class _LostExtractionSubmission(FakeSubmission):
    checkpoint = None

    @property
    def response_baseline(self):
        return ResponseBaseline(self.turn, text_sha256(SOURCE if self.turn else ""))

    def set_correlation_checkpoint(self, callback):
        self.checkpoint = callback

    async def submit_once(self):
        await super().submit_once()
        if self.checkpoint:
            self.checkpoint({
                "identity": self.identity.to_mapping(),
                "prompt_sha256": text_sha256(self.current_prompt),
                "response_baseline": self.response_baseline.to_mapping(),
                "expected_turn": self.turn + 1,
                "schema_version": "browser-conversation-binding-v1",
            })

    async def wait_response(self):
        if self.turn == 1:
            raise RuntimeError("synthetic connection lost after extraction send")
        return await super().wait_response()

    def restore_recovery_attempt(self, *, prompt, response_baseline, binding):
        assert prompt == self.driver.historical_prompt
        assert text_sha256(prompt) == binding["prompt_sha256"]
        self.driver.restored_prompts.append(prompt)
        self.submitted = True

    async def reconcile_no_response(self, grace_seconds):
        return ReconciliationResult(
            ReconciliationDisposition.LATE_RESPONSE_CAPTURED, self.identity, {},
            answer=ProviderAnswer(ProviderStatus.COLLECTED, EXTRACTION, self.identity),
        )


class _LostExtractionDriver(FakeDriver):
    def __init__(self, observation_id, historical_prompt):
        super().__init__("chatgpt", {observation_id: (AnswerSpec(SOURCE), AnswerSpec(EXTRACTION))})
        self.historical_prompt = historical_prompt
        self.restored_prompts = []

    async def open(self, **kwargs):
        old = await super().open(**kwargs)
        return _LostExtractionSubmission(self, old.observation_id, old.identity, old.answers)

    async def reopen(self, binding, *, observation_id):
        identity = replace(
            BrowserIdentity.from_mapping(binding["identity"]),
            tab_reference="verified-reopened", browser_target_reference="verified-target",
        )
        return _LostExtractionSubmission(self, observation_id, identity, self.answers[observation_id])


class _CanonicalSourceSubmission(_LostExtractionSubmission):
    def set_correlation_checkpoint(self, callback):
        def checkpoint(binding):
            if self.turn == 1 and self.driver.different_checkpoint_conversation:
                binding = {**binding, "identity": {
                    **binding["identity"],
                    "conversation_reference": self.identity.conversation_reference + "-different",
                }}
            callback(binding)
        self.checkpoint = checkpoint

    async def wait_response(self):
        answer = await super().wait_response()
        return replace(answer, identity=replace(
            answer.identity,
            conversation_reference=answer.identity.conversation_reference + "?view=source#answer",
        ))


class _CanonicalSourceDriver(_LostExtractionDriver):
    spec = CHATGPT_SPEC
    canonical_conversation_reference = PageProvider.canonical_conversation_reference

    def __init__(self, observation_id, *, different_checkpoint_conversation):
        super().__init__(observation_id, source_parser_prompt(SOURCE))
        self.different_checkpoint_conversation = different_checkpoint_conversation
        self.reopened_conversations = []

    async def open(self, **kwargs):
        old = await FakeDriver.open(self, **kwargs)
        return _CanonicalSourceSubmission(self, old.observation_id, old.identity, old.answers)

    async def reopen(self, binding, *, observation_id):
        identity = BrowserIdentity.from_mapping(binding["identity"])
        identity = replace(
            identity, tab_reference="verified-reopened", browser_target_reference="verified-target",
            conversation_reference=self.canonical_conversation_reference(identity.conversation_reference),
        )
        self.reopened_conversations.append(identity.conversation_reference)
        return _CanonicalSourceSubmission(self, observation_id, identity, self.answers[observation_id])


@pytest.mark.parametrize("different_conversation", [False, True], ids=["query-and-fragment", "different-id"])
def test_original_response_recovery_uses_canonical_source_conversation(tmp_path, different_conversation):
    policy = _policy()
    runtime, digest, profile = _runtime(tmp_path, policy)
    plan = _typed_plan(
        Platform.CHATGPT, run_id="canonical-original-recovery", manifest_sha256=digest,
        profile=profile, retry_policy=policy,
    )
    item_id = plan.observation_ids[0]
    driver = _CanonicalSourceDriver(item_id, different_checkpoint_conversation=different_conversation)
    runner = Runner(runtime, providers={"chatgpt": driver})
    assert runner.run(plan).status.value == "needs_attention"
    store = SendAttemptStore(runner.repository.run_dir(plan.run_id) / "fences", watch_changes=False)
    try:
        source = store.load(f"{item_id}:turn-1")
        source_bytes = store.path(source.fence_id).read_bytes()
        source_url = source.ack_identity["conversation_reference"]
        assert source_url.endswith("?view=source#answer")
        canonical = driver.canonical_conversation_reference(source_url)
        assert canonical == f"https://chatgpt.com/c/{item_id}"
        historical = store.load(f"{item_id}:turn-2")
        original_bytes = store.path(historical.fence_id).read_bytes()
        sent = tuple(driver.sent)
        result = runner.refetch(runner.plan_refetch(plan.run_id, request_id="recover-canonical-source"))
        assert (result.status.value == "succeeded") is (not different_conversation)
        assert tuple(driver.sent) == sent
        assert driver.restored_prompts == ([] if different_conversation else [source_parser_prompt(SOURCE)])
        assert driver.reopened_conversations == [canonical + ("-different" if different_conversation else "")]
        assert store.path(historical.fence_id).read_bytes() == original_bytes
        assert store.path(source.fence_id).read_bytes() == source_bytes
    finally:
        store.close()


@pytest.mark.parametrize("historical_prompt,accepted", [
    pytest.param(LEGACY_V2, True, id="v2"),
    pytest.param(LEGACY_V3, True, id="v3"),
    pytest.param(None, True, id="source-bound"),
    pytest.param(prompts.same_chat_extraction_prompt(SOURCE, platform="copilot"), True,
                 id="copilot-source-bound"),
    pytest.param("unknown historical extraction prompt", False, id="mismatch"),
])
def test_original_response_recovery_reconstructs_admitted_prompt_without_resending(
    tmp_path, monkeypatch, historical_prompt, accepted
):
    import visibility_runner.execution.run_measurement_group as worker_module

    policy = _policy()
    runtime, digest, profile = _runtime(tmp_path, policy)
    runtime = replace(runtime, profiles=replace(
        runtime.profiles, lease_lock_root=tmp_path / "synthetic-profile-locks",
    ))
    plan = _typed_plan(
        Platform.CHATGPT, run_id="legacy-original-recovery", manifest_sha256=digest,
        profile=profile, retry_policy=policy,
    )
    item_id = plan.observation_ids[0]
    expected = historical_prompt or source_parser_prompt(SOURCE)
    driver = _LostExtractionDriver(item_id, expected)
    runner = Runner(runtime, providers={"chatgpt": driver})
    # Produce an actually admitted synthetic historical turn, including its
    # original correlation checkpoint, before exercising the new recovery code.
    with monkeypatch.context() as patch:
        if historical_prompt is not None:
            patch.setattr(prompts, "same_chat_extraction_prompt", lambda *args, **kwargs: expected)
            patch.setattr(worker_module, "same_chat_extraction_prompt",
                          lambda *args, **kwargs: expected, raising=False)
        assert runner.run(plan).status.value == "needs_attention"
    store = SendAttemptStore(runner.repository.run_dir(plan.run_id) / "fences", watch_changes=False)
    try:
        historical = store.load(f"{item_id}:turn-2")
        assert historical.state is SendAttemptState.SEND_STARTED
        assert historical.prompt_sha256 == text_sha256(expected)
        original_bytes = store.path(historical.fence_id).read_bytes()
        sent = tuple(driver.sent)
        result = runner.refetch(runner.plan_refetch(plan.run_id, request_id="recover-legacy-turn"))
        assert (result.status.value == "succeeded") is accepted
        assert tuple(driver.sent) == sent
        assert driver.restored_prompts == ([expected] if accepted else [])
        assert store.path(historical.fence_id).read_bytes() == original_bytes
    finally:
        store.close()
