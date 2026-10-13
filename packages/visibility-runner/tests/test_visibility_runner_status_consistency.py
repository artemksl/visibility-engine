"""Context-free partial aggregation and persisted inspect precedence."""

import pytest

from visibility_runner.execution.manage_measurements import _run_status
from visibility_runner.models import PacketResult
from visibility_runner.status import ObservationStatus, PacketStatus, RunStatus, aggregate_status


@pytest.mark.parametrize("target", [PacketStatus, RunStatus])
def test_partial_child_establishes_useful_evidence(target):
    child = ObservationStatus.PARTIAL
    assert aggregate_status([child], target=target) is target.PARTIAL
    for other in ("failed", "rejected", "cancelled", "running", "planned", "succeeded"):
        assert aggregate_status([child, other], target=target) is target.PARTIAL
    assert aggregate_status([child, "needs_attention"], target=target) is target.NEEDS_ATTENTION


@pytest.mark.parametrize("persisted,children,expected", [
    ("needs_attention", ["succeeded"], "needs_attention"),
    ("partial", ["succeeded", "succeeded"], "succeeded"),
    ("failed", ["succeeded", "succeeded"], "succeeded"),
    ("cancelled", ["partial"], "cancelled"),
    ("failed", ["partial", "failed"], "partial"),
    ("failed", ["running", "failed"], "failed"),
    ("partial", ["planned"], "partial"),
    ("", ["running", "failed"], "running"),
])
def test_inspect_keeps_persisted_state_and_concurrent_completion_rules(persisted, children, expected):
    packets = [PacketResult(packet_id=f"packet-{index}", status=status, observations=())
               for index, status in enumerate(children)]
    assert _run_status(packets, runner_state={"status": persisted}) is RunStatus(expected)


def test_inspection_of_another_run_is_independent_of_the_active_pass(tmp_path):
    from support.execution import AnswerSpec, FakeDriver, _runtime, _typed_plan
    from support.tree_snapshot import tree_snapshot
    from visibility_runner.config import Platform
    from visibility_runner.events import EventKind
    from visibility_runner.execution.manage_measurements import Runner

    runtime, digest = _runtime(tmp_path)
    active = _typed_plan(Platform.CHATGPT, run_id="active", manifest_sha256=digest)
    other = _typed_plan(Platform.CHATGPT, run_id="other", manifest_sha256=digest)
    driver = FakeDriver("chatgpt", {
        active.observation_ids[0]: (AnswerSpec("Alpha Clinic"), AnswerSpec('["Alpha Clinic"]')),
    })
    observed = []

    def inspect_other(event):
        if event.kind is EventKind.RUN_STARTED:
            before = tree_snapshot(runner.repository.run_dir(other.run_id))
            observed.append(runner.inspect(other.run_id).status)
            assert tree_snapshot(runner.repository.run_dir(other.run_id)) == before

    runner = Runner(runtime, providers={"chatgpt": driver}, event_sink=inspect_other)
    # Both plans deliberately use the same local observation ID. The active
    # pass's unfinished selection must never hide another run's accepted row.
    runner.create(other)
    runner.repository.write_observation_result(other.run_id, other.observation_ids[0], {
        "platform": "chatgpt", "status": "succeeded", "extraction_status": "complete",
    })
    runner.repository.update_packet_state(other.run_id, other.packets[0].packet_id,
                                         lambda state: state.update(
                                             status="succeeded", completed_observation_ids=list(other.observation_ids)))
    runner.repository.update_state(other.run_id, lambda state: state.update(status="succeeded"))

    assert runner.run(active).status is RunStatus.SUCCEEDED
    assert observed == [RunStatus.SUCCEEDED]


@pytest.mark.parametrize("extraction,expected", [
    ('["Alpha Clinic"]', "succeeded"),
    ("invalid JSON", "partial"),
    (None, "needs_attention"),
])
def test_inspected_persisted_and_rebuilt_progress_share_status_and_counts(tmp_path, extraction, expected):
    from support.execution import AnswerSpec, FakeDriver, _runtime, _typed_plan
    from support.tree_snapshot import tree_snapshot
    from visibility_runner.config import Platform
    from visibility_runner.execution.manage_measurements import Runner
    from visibility_runner.storage.progress_projection import progress_fields

    runtime, digest = _runtime(tmp_path)
    plan = _typed_plan(Platform.CHATGPT, run_id="shared-progress", manifest_sha256=digest)
    driver = FakeDriver("chatgpt", {
        plan.observation_ids[0]: ((AnswerSpec("Alpha Clinic"), AnswerSpec(extraction))
                                if extraction is not None else (AnswerSpec("Alpha Clinic"),)),
    })
    runner = Runner(runtime, providers={"chatgpt": driver})
    result = runner.run(plan)
    assert result.status is RunStatus(expected)
    fields = progress_fields(result)
    persisted = runner.repository.read_progress(plan.run_id)
    assert {key: persisted[key] for key in fields} == fields

    # Lose only the disposable views. Reconstructing them must retain actual
    # send milestones and accepted-result semantics without any execution.
    root = runner.repository.run_dir(plan.run_id)
    (root / "runner_state.json").unlink()
    (root / "packets" / plan.packets[0].packet_id / "state.json").unlink()
    before = tree_snapshot(root)
    sends = list(driver.sent)
    rebuilt = runner.repository.read_progress(plan.run_id)
    packet = runner.repository.read_group_progress(plan.run_id, plan.packets[0].packet_id)
    inspected = runner.inspect(plan.run_id)

    assert progress_fields(inspected) == fields
    assert {key: rebuilt[key] for key in fields} == fields
    assert packet["status"] == fields["packet_statuses"][plan.packets[0].packet_id]
    assert packet["transport_counts"] == fields["transport_counts"]
    assert tree_snapshot(root) == before
    assert driver.sent == sends


def test_inspect_reports_owned_two_turn_work_as_running_until_result_is_saved(tmp_path):
    from support.execution import AnswerSpec, FakeDriver, _runtime, _typed_plan
    from support.tree_snapshot import tree_snapshot
    from visibility_runner.config import Platform
    from visibility_runner.events import EventKind
    from visibility_runner.execution.manage_measurements import Runner

    runtime, digest = _runtime(tmp_path)
    plan = _typed_plan(Platform.CHATGPT, run_id="live-progress", manifest_sha256=digest)
    driver = FakeDriver("chatgpt", {
        plan.observation_ids[0]: (AnswerSpec("Alpha Clinic"), AnswerSpec('["Alpha Clinic"]')),
    })
    observed = []

    def inspect_current(event):
        if event.kind not in {EventKind.OBSERVATION_PREPARED, EventKind.SEND_STARTED,
                              EventKind.SEND_ACKNOWLEDGED, EventKind.EXTRACTION_STARTED}:
            return
        before = tree_snapshot(runner.repository.run_dir(plan.run_id))
        result = runner.inspect(plan.run_id)
        observed.append((event.kind, result.status))
        assert result.status is RunStatus.RUNNING
        assert result.issues == ()
        assert tree_snapshot(runner.repository.run_dir(plan.run_id)) == before

    runner = Runner(runtime, providers={"chatgpt": driver}, event_sink=inspect_current)
    assert runner.run(plan).status is RunStatus.SUCCEEDED
    assert sum(kind is EventKind.SEND_STARTED for kind, _ in observed) == 2
    assert runner.inspect(plan.run_id).status is RunStatus.SUCCEEDED


@pytest.mark.parametrize("ownership", [
    {"state": "available", "status": "dead", "execution_id": "current"},
    {"state": "owned", "status": "unknown", "execution_id": "current"},
    {"state": "owned", "status": "alive", "execution_id": "foreign"},
])
def test_unproven_owner_does_not_hide_unacknowledged_send(tmp_path, ownership):
    from support.execution import AnswerSpec, FakeDriver, _runtime, _typed_plan
    from visibility_runner.config import Platform
    from visibility_runner.events import EventKind
    from visibility_runner.execution.manage_measurements import Runner
    from visibility_runner.plan_serialization import load_run_plan
    from visibility_runner.storage.progress_projection import project_result

    runtime, digest = _runtime(tmp_path)
    plan = _typed_plan(Platform.CHATGPT, run_id="owner-progress", manifest_sha256=digest)
    driver = FakeDriver("chatgpt", {
        plan.observation_ids[0]: (AnswerSpec("Alpha Clinic"), AnswerSpec('["Alpha Clinic"]')),
    })
    observed = []

    def inspect_current(event):
        if event.kind is not EventKind.SEND_STARTED:
            return
        verified = runner.repository.read_verified_snapshot(plan.run_id)
        owner = dict(ownership)
        if owner["execution_id"] == "current":
            owner["execution_id"] = verified.runner_state["execution_id"]
        result = project_result(load_run_plan(verified.plan), verified.observation_results,
                                verified.attempt_records, verified.runner_state, verified.group_progress,
                                ownership=owner)
        observed.append(result.status)
        assert result.status is RunStatus.NEEDS_ATTENTION
        assert any(issue.code == "ambiguous_send" for issue in result.issues)

    runner = Runner(runtime, providers={"chatgpt": driver}, event_sink=inspect_current)
    assert runner.run(plan).status is RunStatus.SUCCEEDED
    assert observed == [RunStatus.NEEDS_ATTENTION, RunStatus.NEEDS_ATTENTION]


def test_live_owner_preserves_recorded_ambiguous_failure(tmp_path):
    from support.execution import AnswerSpec, FakeDriver, _runtime, _typed_plan
    from visibility_runner.config import Platform
    from visibility_runner.execution.manage_measurements import Runner
    from visibility_runner.plan_serialization import load_run_plan
    from visibility_runner.storage.progress_projection import project_result

    runtime, digest = _runtime(tmp_path)
    plan = _typed_plan(Platform.CHATGPT, run_id="failed-progress", manifest_sha256=digest)
    driver = FakeDriver("chatgpt", {plan.observation_ids[0]: (AnswerSpec("Alpha Clinic"),)})
    runner = Runner(runtime, providers={"chatgpt": driver})
    assert runner.run(plan).status is RunStatus.NEEDS_ATTENTION
    verified = runner.repository.read_verified_snapshot(plan.run_id)
    result = project_result(load_run_plan(verified.plan), verified.observation_results,
                            verified.attempt_records, verified.runner_state, verified.group_progress,
                            ownership={"state": "owned", "status": "alive",
                                       "execution_id": verified.runner_state["execution_id"]})
    assert result.status is RunStatus.NEEDS_ATTENTION
    assert any(issue.code == "ambiguous_send" for issue in result.issues)


def test_accepted_packet_and_owned_remaining_work_report_running(tmp_path):
    from dataclasses import replace
    from support.execution import AnswerSpec, FakeDriver, _runtime
    from visibility_runner.events import EventKind
    from visibility_runner.execution.manage_measurements import Runner

    runtime, _ = _runtime(tmp_path)
    runtime = replace(runtime, tabs_per_provider=2)
    driver = FakeDriver("chatgpt", {})
    observed = []

    def inspect_current(event):
        if event.kind is EventKind.PACKET_COMPLETED:
            result = runner.inspect(plan.run_id)
            if len(result.observations) < 2:
                observed.append(result.status)
                assert result.status is RunStatus.RUNNING
                assert result.issues == ()

    runner = Runner(runtime, providers={"chatgpt": driver}, event_sink=inspect_current)
    plan = runner.plan({"scope_kind": "diagnostic", "platforms": ["chatgpt"],
                        "diagnostic_prompts": ["Recommend clinics", "Recommend clinics"]},
                       run_id="two-live-packets")
    driver.answers.update({item: (AnswerSpec("Alpha Clinic"), AnswerSpec('["Alpha Clinic"]'))
                           for item in plan.observation_ids})
    assert runner.run(plan).status is RunStatus.SUCCEEDED
    assert observed == [RunStatus.RUNNING]
