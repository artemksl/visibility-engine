"""Synthetic native-browser timeout regressions; no browser or network use."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visibility_runner.errors import (  # noqa: E402
    HumanVerificationRequiredError,
    IdentityMismatchError,
    ProviderAccessBlockedError,
)
from visibility_runner.websites import chat_actions as base  # noqa: E402
from visibility_runner.websites import page_readers
from visibility_runner.websites.chatgpt import CHATGPT_SPEC  # noqa: E402
from visibility_runner.websites.gemini import GEMINI_SPEC  # noqa: E402
from visibility_runner.storage.send_attempts import SendAttemptState, SendAttemptStore  # noqa: E402

from visibility_runner.storage.measurement_files import MeasurementFiles
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker

import support.retries as worker_fixtures  # noqa: E402


PROVIDERS = (CHATGPT_SPEC, GEMINI_SPEC)
STAGES = (
    "_visible_access_block_marker",
    "visible_answer_snapshot",
    "any_visible",
    "visible_text_values_within_latest_response",
)
SYNTHETIC_ANSWER = "Synthetic response for an isolated timeout contract."


@pytest.fixture
def native_timeout(request):
    # Import the actual class: matching an exception's name is insufficient.
    name = request.config.getoption("--browser-driver", default="playwright")
    return pytest.importorskip(f"{name}.async_api").TimeoutError


from support.polling import clock


class Probes:
    def __init__(self, monkeypatch, *, stage, error=None, failed_calls=()):
        self.stage = stage
        self.error = error
        self.failed_calls = failed_calls
        self.calls = {name: 0 for name in STAGES}
        self.deadlines = []
        self.failed_at = []
        self.answer_reads_at = []
        self.access_marker = None
        for name in STAGES:
            monkeypatch.setattr(base, name, self.reader(name))

    def reader(self, name):
        async def read(*_args, _deadline=None, **_kwargs):
            self.calls[name] += 1
            self.deadlines.append(_deadline)
            if name == self.stage and self.error is not None:
                if self.failed_calls is None or self.calls[name] in self.failed_calls:
                    self.failed_at.append(base.time.monotonic())
                    raise self.error
            if name == "visible_answer_snapshot":
                self.answer_reads_at.append(base.time.monotonic())
            return {
                "_visible_access_block_marker": self.access_marker,
                "visible_answer_snapshot": base.VisibleAnswerSnapshot(
                    count=1, text=SYNTHETIC_ANSWER
                ),
                "any_visible": False,
                "visible_text_values_within_latest_response": (),
            }[name]

        return read


def poll(spec, *, deadline=None, stable_seconds=0.0):
    return base.wait_for_stable_answer(
        object(),
        replace(spec, response_timeout_seconds=900.0, stable_seconds=stable_seconds),
        before_count=0,
        before_answer="",
        _deadline=deadline,
    )


@pytest.mark.parametrize("stage", STAGES)
def test_native_probe_timeout_then_stable_answer_keeps_original_deadline(
    monkeypatch, native_timeout, clock, stage
):
    probes = Probes(
        monkeypatch,
        stage=stage,
        error=native_timeout("synthetic transient browser read timeout"),
        failed_calls=(1,),
    )
    deadline = base._BrowserOperationDeadline.after(
        1.0, timeout_message="synthetic absolute response budget"
    )

    assert asyncio.run(poll(CHATGPT_SPEC, deadline=deadline)) == SYNTHETIC_ANSWER
    assert probes.failed_at
    assert all(value is deadline for value in probes.deadlines)
    assert clock.value < deadline.expires_at
    assert deadline.expires_at == 1001.0


@pytest.mark.parametrize("stage", ("_visible_access_block_marker", "visible_answer_snapshot"))
def test_repeated_native_probe_timeouts_exhaust_original_budget(
    monkeypatch, native_timeout, clock, stage
):
    probes = Probes(
        monkeypatch,
        stage=stage,
        error=native_timeout("synthetic repeated browser read timeout"),
        failed_calls=None,
    )
    deadline = base._BrowserOperationDeadline.after(
        0.55, timeout_message="synthetic absolute response budget"
    )

    with pytest.raises(TimeoutError):
        asyncio.run(poll(CHATGPT_SPEC, deadline=deadline))
    assert all(value is deadline for value in probes.deadlines)
    assert clock.value == pytest.approx(deadline.expires_at)
    assert deadline.expires_at == pytest.approx(1000.55)


def test_native_probe_timeout_resets_answer_stability(
    monkeypatch, native_timeout, clock
):
    probes = Probes(
        monkeypatch,
        stage="visible_answer_snapshot",
        error=native_timeout("synthetic interrupted stability window"),
        failed_calls=(2,),
    )
    deadline = base._BrowserOperationDeadline.after(
        1.0, timeout_message="synthetic absolute response budget"
    )

    stable_seconds = 0.1
    assert asyncio.run(poll(CHATGPT_SPEC, deadline=deadline, stable_seconds=stable_seconds)) == SYNTHETIC_ANSWER
    resumed_at = next(value for value in probes.answer_reads_at if value > probes.failed_at[-1])
    assert clock.value >= resumed_at + stable_seconds
    assert all(value is deadline for value in probes.deadlines)


@pytest.mark.parametrize(
    ("stage", "error_type"),
    (
        ("_visible_access_block_marker", IdentityMismatchError),
        ("visible_answer_snapshot", ProviderAccessBlockedError),
        ("_visible_access_block_marker", HumanVerificationRequiredError),
    ),
)
def test_identity_and_access_failures_are_never_transient_probe_timeouts(
    monkeypatch, native_timeout, clock, stage, error_type
):
    error = error_type("synthetic fail-closed control")
    probes = Probes(monkeypatch, stage=stage, error=error, failed_calls=None)

    with pytest.raises(error_type) as raised:
        asyncio.run(poll(CHATGPT_SPEC))
    assert raised.value is error
    assert probes.calls[stage] == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("marker", ("authentication_required", "human_verification_required"))
def test_visible_access_gate_after_native_timeout_remains_fail_closed(
    monkeypatch, native_timeout, clock, marker
):
    probes = Probes(
        monkeypatch,
        stage="_visible_access_block_marker",
        error=native_timeout("synthetic transient access-surface read timeout"),
        failed_calls=(1,),
    )
    probes.access_marker = marker
    expected = (
        HumanVerificationRequiredError
        if marker == "human_verification_required"
        else ProviderAccessBlockedError
    )

    with pytest.raises(expected) as raised:
        asyncio.run(poll(CHATGPT_SPEC))
    assert type(raised.value) is expected
    assert probes.calls["_visible_access_block_marker"] == 2
    assert clock.value == pytest.approx(1000.2)


@pytest.mark.parametrize("error_type", (TimeoutError, RuntimeError, asyncio.CancelledError))
def test_non_native_failures_keep_existing_semantics(
    monkeypatch, native_timeout, clock, error_type
):
    error = error_type("synthetic non-native failure")
    probes = Probes(
        monkeypatch, stage="visible_answer_snapshot", error=error, failed_calls=None
    )

    with pytest.raises(error_type) as raised:
        asyncio.run(poll(CHATGPT_SPEC))
    assert raised.value is error
    assert probes.calls["visible_answer_snapshot"] == 1
    assert clock.sleeps == []


def test_unrelated_exception_named_timeout_is_not_retried(
    monkeypatch, native_timeout, clock
):
    unrelated_timeout = type("TimeoutError", (Exception,), {})
    error = unrelated_timeout("synthetic misleading exception name")
    probes = Probes(
        monkeypatch, stage="visible_answer_snapshot", error=error, failed_calls=None
    )

    with pytest.raises(unrelated_timeout) as raised:
        asyncio.run(poll(CHATGPT_SPEC))
    assert raised.value is error
    assert probes.calls["visible_answer_snapshot"] == 1
    assert clock.sleeps == []


def test_timeout_recognition_does_not_load_browser_dependencies(
    monkeypatch, native_timeout
):
    unloaded_modules = {}
    monkeypatch.setattr(page_readers, "sys", SimpleNamespace(modules=unloaded_modules))

    assert not base._is_native_playwright_timeout(native_timeout("synthetic timeout"))
    assert unloaded_modules == {}


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda spec: spec.platform)
def test_native_timeout_recovery_never_repeats_the_worker_send(
    tmp_path, monkeypatch, native_timeout, clock, spec
):
    Probes(
        monkeypatch,
        stage="visible_answer_snapshot",
        error=native_timeout("synthetic transient worker response probe"),
        failed_calls=(1,),
    )
    observation_id = "synthetic-native-timeout"
    run_id = "synthetic-native-timeout-run"
    driver = worker_fixtures._ScriptedDriver(
        spec.platform,
        {observation_id: ((worker_fixtures._answer(SYNTHETIC_ANSWER),),)},
    )

    async def wait_response(submission):
        text = await poll(spec)
        return base.ProviderAnswer(
            status=base.ProviderStatus.COLLECTED,
            text=text,
            identity=submission.identity,
        )

    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "wait_response", wait_response)

    async def exercise():
        plan = worker_fixtures._raw_plan(run_id, [
            worker_fixtures._observation(observation_id, spec.platform),
            worker_fixtures._observation("synthetic-forbidden-peer", spec.platform),
        ])
        repository = MeasurementFiles(tmp_path)
        repository.create(plan)
        worker = MeasurementGroupWorker(repository, worker_fixtures._registry(driver))
        fences = worker._send_attempts(run_id)
        submission = await driver.open(
            profile_id="profile_a", chrome_directory="Default", observation_id=observation_id
        )
        prompt = "Synthetic local worker prompt."
        fence = fences.prepare(
            fence_id=f"{observation_id}:turn-1",
            observation_id=observation_id,
            action="measurement_turn_1",
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            identity=submission.identity.to_mapping(),
        )
        await worker._send(
            run_id, fences, fence, submission, prompt,
            packet_id="packet-1", policy=worker_fixtures._policy(),
        )
        stored = fences.load(fence.fence_id)
        assert stored.state is SendAttemptState.ACKNOWLEDGED
        assert stored.response_reference is not None
        assert stored.retried_by is None

    asyncio.run(exercise())
    assert len(driver.opens) == len(driver.sent) == 1
    assert driver.reconciled == []
    assert driver.native_retries == []
    assert driver.next_turns == []


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda spec: spec.platform)
def test_repeated_native_timeouts_reconcile_without_resend_or_peer(
    tmp_path, monkeypatch, native_timeout, clock, spec
):
    probes = Probes(
        monkeypatch,
        stage="visible_answer_snapshot",
        error=native_timeout("synthetic repeated worker response probe"),
        failed_calls=None,
    )
    observation_id = "synthetic-native-timeout"
    run_id = "synthetic-native-timeout-run"
    driver = worker_fixtures._ScriptedDriver(spec.platform, {
        observation_id: ((worker_fixtures._timeout(
            disposition=base.ReconciliationDisposition.AMBIGUOUS,
            failure_classification="synthetic_unproven_response",
        ),),),
    })
    deadline = base._BrowserOperationDeadline.after(
        0.55, timeout_message="synthetic absolute response budget"
    )

    async def wait_response(_submission):
        return await poll(spec, deadline=deadline)

    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "wait_response", wait_response)

    async def exercise():
        repository, outcome = await worker_fixtures._execute_raw(
            tmp_path,
            worker_fixtures._raw_plan(run_id, [
                worker_fixtures._observation(observation_id, spec.platform),
                worker_fixtures._observation("synthetic-forbidden-peer", spec.platform),
            ]),
            worker_fixtures._registry(driver),
        )
        assert outcome.status == "needs_attention"
        assert outcome.completed_observation_ids == ()
        records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
        assert len(records) == 1
        record = records[0]
        assert record.state is SendAttemptState.SEND_STARTED
        assert record.reconciliation_evidence is not None
        assert record.retry_decision == "needs_attention"
        assert record.retried_by is None
        assert record.response_reference is None
        assert outcome.issues[0]["detail"]["failure_context"]["phase"] == "response_reconciliation"

    asyncio.run(exercise())
    assert clock.value == pytest.approx(deadline.expires_at)
    assert len(driver.opens) == len(driver.sent) == len(driver.reconciled) == 1
    assert driver.native_retries == []
    assert driver.next_turns == []


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda spec: spec.platform)
@pytest.mark.parametrize(
    "error_type",
    (IdentityMismatchError, ProviderAccessBlockedError, HumanVerificationRequiredError),
)
def test_identity_or_access_failure_after_native_timeout_stops_worker_peer(
    tmp_path, monkeypatch, native_timeout, clock, spec, error_type
):
    probes = Probes(
        monkeypatch,
        stage="visible_answer_snapshot",
        error=native_timeout("synthetic transient worker response probe"),
        failed_calls=(1,),
    )
    original_read = base.visible_answer_snapshot

    async def native_then_fail_closed(*args, **kwargs):
        if probes.calls["visible_answer_snapshot"]:
            probes.error = error_type("synthetic fail-closed worker probe")
            probes.failed_calls = None
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(base, "visible_answer_snapshot", native_then_fail_closed)
    observation_id = "synthetic-native-then-blocked"
    run_id = "synthetic-native-then-blocked-run"
    driver = worker_fixtures._ScriptedDriver(spec.platform, {
        observation_id: ((worker_fixtures._answer(SYNTHETIC_ANSWER),),),
    })

    async def wait_response(_submission):
        return await poll(spec)

    monkeypatch.setattr(worker_fixtures._ScriptedSubmission, "wait_response", wait_response)

    async def exercise():
        repository, outcome = await worker_fixtures._execute_raw(
            tmp_path,
            worker_fixtures._raw_plan(run_id, [
                worker_fixtures._observation(observation_id, spec.platform),
                worker_fixtures._observation("synthetic-forbidden-peer", spec.platform),
            ]),
            worker_fixtures._registry(driver),
        )
        assert outcome.status == "needs_attention"
        assert outcome.completed_observation_ids == ()
        records = SendAttemptStore(repository.run_dir(run_id) / "fences").iter_records()
        assert len(records) == 1
        assert records[0].state is SendAttemptState.SEND_STARTED
        assert records[0].retry_decision is None
        assert records[0].retried_by is None
        assert records[0].response_reference is None

    asyncio.run(exercise())
    assert probes.calls["visible_answer_snapshot"] == 2
    assert clock.value == pytest.approx(1000.2)
    assert len(driver.opens) == len(driver.sent) == 1
    assert driver.reconciled == []
    assert driver.native_retries == []
    assert driver.next_turns == []
