"""Offline recovery orchestration; the runner remains the effect authority."""
from types import SimpleNamespace

import pytest

from visibility_panel.execution.measurement_recovery import auto_recovery_progress, run_recovery_rounds
from visibility_panel.execution.worker_registry import publish_json, read_record


def request():
    return {"run_id": "run-a", "request_id": "click-a", "execution_id": "execution-a",
            "request_sha256": "a" * 64, "bindings": {"auto_recovery_rounds": 5}}


class Component:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.planned = []
        self.recovered = []
        self.rounds = 0
        self.current = None

    def first(self):
        self.rounds += 1
        self.current = SimpleNamespace(status=next(self.statuses))
        return self.current

    def plan_refetch(self, run_id, *, request_id):
        self.planned.append(request_id)
        return {"run_id": run_id, "request_id": request_id, "selections": [{"item_id": "missing"}],
                "revision_digest": str(self.rounds)}

    def refetch(self, recovery, *, replace_checked_sends):
        self.recovered.append((recovery, replace_checked_sends))
        return self.first()

    def inspect(self, run_id):
        assert run_id == "run-a"
        return SimpleNamespace(status="cancelled")


@pytest.mark.parametrize("completion_round", [1, 3, 5])
def test_stops_as_soon_as_all_results_are_accepted(tmp_path, completion_round):
    component = Component(["partial"] * (completion_round - 1) + ["succeeded"])
    result = run_recovery_rounds(component, request(), tmp_path, first_round=component.first)
    assert result.status == "succeeded"
    assert component.rounds == completion_round
    assert all(replace is False for _, replace in component.recovered)
    assert auto_recovery_progress(request(), tmp_path) == {
        "round": completion_round, "limit": 5, "status": "completed"}


def test_five_unsuccessful_rounds_leave_manual_work_and_do_not_grant_a_sixth(tmp_path):
    component = Component(["partial"] * 5)
    result = run_recovery_rounds(component, request(), tmp_path, first_round=component.first)
    assert result.status == "partial"
    assert component.rounds == 5
    assert component.planned == [f"click-a:auto:{i}" for i in range(2, 6)]
    assert len(component.recovered) == 4
    assert all(replace is False for _, replace in component.recovered)
    assert auto_recovery_progress(request(), tmp_path) == {"round": 5, "limit": 5, "status": "exhausted"}
    assert not (tmp_path / "auto-recovery-round-6.json").exists()


def test_stop_between_rounds_prevents_another_preview_or_attempt(tmp_path):
    component = Component(["partial"])
    result = run_recovery_rounds(component, request(), tmp_path, first_round=component.first,
                                 cancelled=lambda: component.rounds == 1)
    assert result.status == "cancelled"
    assert component.planned == component.recovered == []
    assert auto_recovery_progress(request(), tmp_path)["status"] == "cancelled"


def test_stop_during_startup_is_reported_as_cancelled(tmp_path):
    from visibility_panel.execution.measurement_browser import BrowserStartupCancelled

    component = Component([])
    stopped = False

    def startup():
        nonlocal stopped
        stopped = True
        raise BrowserStartupCancelled("Stopped before measurement")

    with pytest.raises(BrowserStartupCancelled):
        run_recovery_rounds(component, request(), tmp_path, first_round=startup,
                            cancelled=lambda: stopped)
    assert component.planned == component.recovered == []
    assert auto_recovery_progress(request(), tmp_path)["status"] == "cancelled"


def test_redelivery_reuses_saved_round_requests_instead_of_refreshing_allowance(tmp_path):
    original = Component(["partial"] * 5)
    run_recovery_rounds(original, request(), tmp_path, first_round=original.first)
    saved = [(recovery.copy(), flag) for recovery, flag in original.recovered]
    redelivered = Component(["partial"] * 5)
    run_recovery_rounds(redelivered, request(), tmp_path, first_round=redelivered.first)
    assert redelivered.planned == []
    assert redelivered.recovered == saved


def test_foreign_round_receipt_cannot_recover_a_different_run(tmp_path):
    component = Component(["partial"])
    payload = request()
    publish_json(tmp_path / "auto-recovery-round-2.json", {
        "execution_id": payload["execution_id"], "request_sha256": payload["request_sha256"], "round": 2,
        "request": {"run_id": "another-run", "request_id": "click-a:auto:2", "selections": [{}]}})
    with pytest.raises(ValueError, match="round identity changed"):
        run_recovery_rounds(component, payload, tmp_path, first_round=component.first)
    assert component.recovered == []
    assert auto_recovery_progress(payload, tmp_path)["status"] == "error"


def test_legacy_request_keeps_one_round_and_no_new_recovery_allowance(tmp_path):
    payload = request()
    payload["bindings"] = {}
    component = Component(["partial"])
    result = run_recovery_rounds(component, payload, tmp_path, first_round=component.first)
    assert result.status == "partial"
    assert component.rounds == 1
    assert component.recovered == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", [0, 6, True, "5"])
def test_invalid_round_ceiling_is_refused_before_any_execution(tmp_path, limit):
    payload = request()
    payload["bindings"]["auto_recovery_rounds"] = limit
    component = Component([])
    with pytest.raises(ValueError, match="round limit"):
        run_recovery_rounds(component, payload, tmp_path, first_round=component.first)
    assert component.rounds == 0


def test_readout_ignores_progress_from_another_execution(tmp_path):
    component = Component(["partial"] * 5)
    run_recovery_rounds(component, request(), tmp_path, first_round=component.first)
    progress = read_record(tmp_path / "auto-recovery.json")
    progress["execution_id"] = "old-execution"
    publish_json(tmp_path / "auto-recovery.json", progress, replace=True)
    assert auto_recovery_progress(request(), tmp_path) is None
