"""Offline tests: no real Chrome processes, profile stores or network calls."""

import io
import json
import copy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from visibility_panel.browser.browser_lifecycle import (
    BrowserLifecycleLease, BrowserLifecycleLockError, LOCK_PATH_ENV,
    current_browser_lifecycle_lease,
)
from visibility_panel.execution import measurement_browser as startup
from visibility_panel.integrity import canonical_sha256


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCK_PATH_ENV, str(tmp_path / "chrome.lock"))
    rows = [{"id": "profile_1", "chrome_directory": "Profile 1", "display_name": "Personal", "project_url": ""}]
    manifest = tmp_path / "profiles.json"
    manifest.write_text(json.dumps(rows))
    return {"profiles": {"manifest_path": str(manifest), "manifest_sha256": canonical_sha256(rows),
                         "selected_profile_ids": ["profile_1"]},
            "browser": {"cdp_url": "http://127.0.0.1:9222", "launch_policy": "attach_only",
                        "background_only": False}}


def request(mode="run", restart=True):
    return {"mode": mode, "bindings": {"browser_restart_on_start": restart}}


@pytest.mark.parametrize("mode", ["run", "refetch"])
def test_start_and_refetch_restart_frozen_profiles_before_measurement_and_hold_lease(runtime, tmp_path, monkeypatch, mode):
    events = []

    def launch(command, **kwargs):
        events.append("restart")
        assert command[:2] == ["/bin/bash", str(tmp_path / "config/launch_logged_in.sh")]
        private_manifest = Path(command[command.index("--profiles-path") + 1])
        assert private_manifest != Path(runtime["profiles"]["manifest_path"])
        assert json.loads(private_manifest.read_text()) == json.loads(Path(runtime["profiles"]["manifest_path"]).read_text())
        assert command[-3:] == ["--cdp-url", "http://127.0.0.1:9222", "--force-close"]
        assert kwargs["env"]["CITEUP_BROWSER_LIFECYCLE_LOCK_HELD"] == "1"
        assert current_browser_lifecycle_lease().mode == "exclusive"
        return SimpleNamespace(returncode=0)

    def ready(endpoint, directories, **_kwargs):
        events.append("ready")
        assert directories == ["Profile 1"]
        assert current_browser_lifecycle_lease().mode == "exclusive"

    monkeypatch.setattr(startup.subprocess, "run", launch)
    monkeypatch.setattr(startup, "_wait_for_profiles", ready)
    with startup.measurement_browser_session(runtime, request(mode), root=tmp_path):
        events.append("measurement")
        assert current_browser_lifecycle_lease().mode == "shared"
        contender = BrowserLifecycleLease(tmp_path / "chrome.lock")
        contender.acquire_shared()
        try:
            with pytest.raises(BrowserLifecycleLockError, match="in use"):
                with contender.exclusive(blocking=False):
                    pytest.fail("must not restart under measurement")
        finally:
            contender.release()
    assert events == ["restart", "ready", "measurement"]
    assert current_browser_lifecycle_lease() is None


@pytest.mark.parametrize("mode,restart", [("resume", True), ("finalize", True)])
def test_recovery_legacy_and_finalization_never_restart(runtime, tmp_path, monkeypatch, mode, restart):
    monkeypatch.setattr(startup, "_restart_chrome", lambda *_args, **_kwargs: pytest.fail("unexpected restart"))
    with startup.measurement_browser_session(runtime, request(mode, restart), root=tmp_path):
        assert (current_browser_lifecycle_lease() is None) == (mode == "finalize")


def test_active_browser_job_rejects_restart_before_touching_chrome(runtime, tmp_path, monkeypatch):
    active = BrowserLifecycleLease(tmp_path / "chrome.lock")
    active.acquire_shared()
    monkeypatch.setattr(startup, "_restart_chrome", lambda *_args, **_kwargs: pytest.fail("unexpected restart"))
    try:
        with pytest.raises(BrowserLifecycleLockError, match="in use"):
            with startup.measurement_browser_session(runtime, request(), root=tmp_path):
                pytest.fail("unexpected measurement")
    finally:
        active.release()


def test_another_restart_rejects_admission_without_waiting(runtime, tmp_path):
    active = BrowserLifecycleLease(tmp_path / "chrome.lock")
    active.acquire_exclusive()
    try:
        with pytest.raises(BrowserLifecycleLockError, match="being restarted"):
            with startup.measurement_browser_session(runtime, request(), root=tmp_path):
                pytest.fail("unexpected measurement")
    finally:
        active.release()


@pytest.mark.parametrize("damage", ["manifest", "selection"])
def test_invalid_profile_binding_fails_before_chrome_is_closed(runtime, tmp_path, monkeypatch, damage):
    if damage == "manifest":
        Path(runtime["profiles"]["manifest_path"]).write_text("[]")
    else:
        runtime["profiles"]["selected_profile_ids"] = []
    monkeypatch.setattr(startup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected launch"))
    with pytest.raises(ValueError, match="profile"):
        with startup.measurement_browser_session(runtime, request(), root=tmp_path):
            pytest.fail("unexpected measurement")


def test_failed_readiness_never_enters_measurement(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(startup.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Chrome unavailable")
    monkeypatch.setattr(startup, "_wait_for_profiles", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        with startup.measurement_browser_session(runtime, request(), root=tmp_path):
            pytest.fail("unexpected measurement")
    assert current_browser_lifecycle_lease() is None


@pytest.mark.parametrize("mode", ["run", "refetch"])
def test_stop_during_chrome_restart_prevents_measurement(runtime, tmp_path, monkeypatch, mode):
    stopped = False
    def launch(*_args, **_kwargs):
        nonlocal stopped
        stopped = True
    def check():
        if stopped:
            raise startup.BrowserStartupCancelled("Stop requested")
    monkeypatch.setattr(startup.subprocess, "run", launch)
    monkeypatch.setattr(startup, "_wait_for_profiles", lambda *_args, **_kwargs: pytest.fail("already stopped"))
    with pytest.raises(startup.BrowserStartupCancelled):
        with startup.measurement_browser_session(runtime, request(mode), root=tmp_path, check_cancelled=check):
            pytest.fail("measurement after Stop")


def _restartable(monkeypatch, events):
    monkeypatch.setattr(startup.subprocess, "run", lambda *_a, **_k: events.append("restart") or SimpleNamespace(returncode=0))
    monkeypatch.setattr(startup, "_wait_for_profiles", lambda *_a, **_k: events.append("ready"))


def test_post_restart_wait_is_off_by_default(runtime, tmp_path, monkeypatch):
    events = []
    _restartable(monkeypatch, events)
    monkeypatch.delenv(startup.POST_RESTART_WAIT_ENV, raising=False)
    monkeypatch.setattr(startup.time, "sleep", lambda _s: pytest.fail("no wait is configured"))
    with startup.measurement_browser_session(runtime, request("refetch"), root=tmp_path):
        events.append("measurement")
    assert events == ["restart", "ready", "measurement"]


def test_post_restart_wait_runs_after_readiness_and_before_measurement(runtime, tmp_path, monkeypatch):
    events, clock = [], [0.0]
    _restartable(monkeypatch, events)
    monkeypatch.setenv(startup.POST_RESTART_WAIT_ENV, "60")
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        events.append("wait")
        clock[0] += 30.0
    monkeypatch.setattr(startup.time, "sleep", sleep)
    with startup.measurement_browser_session(runtime, request("refetch"), root=tmp_path):
        events.append("measurement")
    assert events == ["restart", "ready", "wait", "wait", "measurement"]


def test_stop_during_post_restart_wait_prevents_measurement(runtime, tmp_path, monkeypatch):
    events, clock, stopped = [], [0.0], []
    _restartable(monkeypatch, events)
    monkeypatch.setenv(startup.POST_RESTART_WAIT_ENV, "60")
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def sleep(_seconds):
        clock[0] += 1.0
        stopped.append(True)
    def check():
        if stopped:
            raise startup.BrowserStartupCancelled("Stop requested")
    monkeypatch.setattr(startup.time, "sleep", sleep)
    with pytest.raises(startup.BrowserStartupCancelled):
        with startup.measurement_browser_session(runtime, request("refetch"), root=tmp_path, check_cancelled=check):
            pytest.fail("measurement after Stop")


@pytest.mark.parametrize("value", ["abc", "-1", "301", "nan", "inf"])
def test_invalid_post_restart_wait_fails_before_chrome_is_closed(runtime, tmp_path, monkeypatch, value):
    monkeypatch.setenv(startup.POST_RESTART_WAIT_ENV, value)
    monkeypatch.setattr(startup.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected launch"))
    with pytest.raises(ValueError, match=startup.POST_RESTART_WAIT_ENV):
        with startup.measurement_browser_session(runtime, request("refetch"), root=tmp_path):
            pytest.fail("unexpected measurement")


def test_default_background_refetch_restarts_before_measurement(runtime, tmp_path, monkeypatch):
    runtime["browser"].pop("background_only")
    events = []
    _restartable(monkeypatch, events)
    def prepare():
        assert current_browser_lifecycle_lease().mode == "exclusive"
        events.append("dispose-holds")
    with startup.measurement_browser_session(runtime, request("refetch", restart=False), root=tmp_path, prepare_restart=prepare):
        assert current_browser_lifecycle_lease().mode == "shared"
        events.append("measurement")
    assert events == ["dispose-holds", "restart", "ready", "measurement"]


def test_background_readiness_keeps_stop_as_cancellation(runtime, tmp_path, monkeypatch):
    runtime["browser"]["background_only"] = True
    monkeypatch.setattr(startup.subprocess, "run", lambda *_a, **_k: None)
    def stopped(*_a, **_k):
        raise startup.BrowserStartupCancelled("Stop requested")
    monkeypatch.setattr(startup, "_wait_for_profiles", stopped)
    with pytest.raises(startup.BrowserStartupCancelled):
        with startup.measurement_browser_session(runtime, request(), root=tmp_path):
            pytest.fail("measurement after Stop")


def test_hold_disposition_failure_never_closes_chrome(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(startup.subprocess, "run", lambda *_a, **_k: pytest.fail("must not close Chrome"))
    def refuse():
        raise RuntimeError("verification close unconfirmed")
    with pytest.raises(RuntimeError, match="verification close unconfirmed"):
        with startup.measurement_browser_session(runtime, request("refetch"), root=tmp_path, prepare_restart=refuse):
            pytest.fail("measurement after failed disposition")


@pytest.mark.parametrize("extra", ["ordinary", "duplicate", "unselected"])
def test_clean_start_rejects_every_extra_tab(monkeypatch, extra):
    anchor = {"type": "page", "url": "file:///anchor.html#citeup-profile=Profile%201"}
    added = anchor if extra == "duplicate" else {"type": "page", "url":
        "https://copilot.com/chats/old" if extra == "ordinary" else "file:///anchor.html#citeup-profile=Profile%2099"}
    monkeypatch.setattr(startup, "urlopen", lambda *_a, **_k: io.BytesIO(json.dumps([anchor, added]).encode()))
    with pytest.raises(RuntimeError, match="anchors"):
        startup._wait_for_profiles("http://127.0.0.1:9222", ["Profile 1"], timeout=0)


def test_clean_start_accepts_only_selected_anchors(monkeypatch):
    pages = [{"type": "page", "url": "file:///anchor.html#citeup-profile=Profile%201"},
             {"type": "service_worker", "url": "chrome-extension://synthetic"}]
    monkeypatch.setattr(startup, "urlopen", lambda *_a, **_k: io.BytesIO(json.dumps(pages).encode()))
    startup._wait_for_profiles("http://127.0.0.1:9222", ["Profile 1"], timeout=0)


class _RecoveryComponent:
    """Model public engine admission; Chrome remains a subprocess double."""
    def __init__(self, events, *, source_available=True, close_error=None):
        self.events, self.source_available, self.close_error = events, source_available, close_error
        self.original = {"run_id": "held-run", "request_id": "same-click",
                         "scope_digest": "frozen-scope", "manifest_digest": "frozen-manifest",
                         "revision_digest": "held", "selections": [{"action": "blocked"}]}
        self.current = copy.deepcopy(self.original)
        self.authorized = None
        self.maintenance = False
        self.sends = 0

    @contextmanager
    def browser_restart(self, *, request_id, recovery_request):
        assert request_id == "host-execution"
        assert self.authorized is None and recovery_request == self.current
        assert current_browser_lifecycle_lease().mode == "exclusive"
        self.events.append("dispose-hold")
        if self.close_error:
            raise self.close_error
        self.current = {**self.current, "revision_digest": "disposed",
                        "selections": [{"action": "new_attempt"}]}
        self.maintenance = True
        try:
            yield {"closed_verification_episodes": ["exact-held-tab"],
                   "recovery_request": copy.deepcopy(self.current)}
        finally:
            self.maintenance = False

    def plan_refetch(self, run_id, *, request_id):
        assert run_id == "held-run" and request_id == "same-click"
        return copy.deepcopy(self.current)

    def refetch(self, effective, factory):
        if effective == self.authorized:
            self.events.append("duplicate")
            return
        assert effective == self.current and effective["selections"] == [{"action": "new_attempt"}]
        assert self.maintenance and current_browser_lifecycle_lease().mode == "exclusive"
        self.authorized = copy.deepcopy(effective)
        self.current = {**self.current, "revision_digest": "admitted"}
        self.events.extend(["admit", "source-check"])
        if self.source_available:
            with factory():
                assert not self.maintenance and current_browser_lifecycle_lease().mode == "shared"
                self.events.append("measure")
                self.sends += 2


def _coordinator(component, runtime, tmp_path):
    execution_dir = tmp_path / "execution"
    execution_dir.mkdir(exist_ok=True)
    request = {"run_id": "held-run", "mode": "refetch", "execution_id": "host-execution",
               "request_id": "same-click", "request_sha256": "a" * 64,
               "bindings": {}, "recovery": copy.deepcopy(component.original)}
    browser = startup.MeasurementBrowserCoordinator(component, runtime, request,
                root=tmp_path, execution_dir=execution_dir)
    return browser, request, execution_dir


def test_refetch_disposes_and_refreshes_before_admission_then_restarts_after_source_check(runtime, tmp_path, monkeypatch):
    events = []
    _restartable(monkeypatch, events)
    component = _RecoveryComponent(events)
    browser, request, directory = _coordinator(component, runtime, tmp_path)
    original = copy.deepcopy(request)
    with browser.prepare():
        component.refetch(browser.recovery_request, browser.start)
        assert current_browser_lifecycle_lease().mode == "shared"
        events.append("publish")
    assert events == ["dispose-hold", "admit", "source-check", "restart", "ready", "measure", "publish"]
    assert current_browser_lifecycle_lease() is None and not component.maintenance
    receipt = json.loads((directory / "browser-preparation.json").read_bytes())
    assert receipt["original_recovery_request"] == component.original
    assert receipt["effective_recovery_request"] == component.authorized
    assert receipt["request_sha256"] == request["request_sha256"] and request == original
    replay, replay_request, _ = _coordinator(component, runtime, tmp_path)
    with replay.prepare():
        component.refetch(replay.recovery_request, replay.start)
    assert events[-1] == "duplicate" and component.sends == 2 and replay_request == original
    assert events.count("dispose-hold") == events.count("admit") == events.count("restart") == 1


def test_source_refusal_after_hold_disposal_never_restarts_and_releases_barriers(runtime, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(startup, "_restart_chrome", lambda *_a, **_k: pytest.fail("source refused"))
    component = _RecoveryComponent(events, source_available=False)
    browser, _, directory = _coordinator(component, runtime, tmp_path)
    with browser.prepare():
        component.refetch(browser.recovery_request, browser.start)
    assert events == ["dispose-hold", "admit", "source-check"] and component.sends == 0
    assert not component.maintenance and current_browser_lifecycle_lease() is None
    assert (directory / "browser-preparation.json").is_file()
    assert not (directory / "browser-restart.json").exists()


def test_unconfirmed_hold_disposal_refuses_before_admission_and_restart(runtime, tmp_path, monkeypatch):
    events, error = [], RuntimeError("verification close unconfirmed")
    component = _RecoveryComponent(events, close_error=error)
    browser, _, directory = _coordinator(component, runtime, tmp_path)
    monkeypatch.setattr(startup, "_restart_chrome", lambda *_a, **_k: pytest.fail("hold remained"))
    with pytest.raises(RuntimeError) as caught:
        with browser.prepare():
            pytest.fail("must not admit work")
    assert caught.value is error and events == ["dispose-hold"] and component.sends == 0
    assert current_browser_lifecycle_lease() is None and not (directory / "browser-preparation.json").exists()


@pytest.mark.parametrize("damage", ["request_sha256", "recovery_request_id"])
def test_preparation_receipt_replay_requires_exact_execution_and_recovery_identity(runtime, tmp_path, monkeypatch, damage):
    events = []
    _restartable(monkeypatch, events)
    component = _RecoveryComponent(events)
    browser, _, directory = _coordinator(component, runtime, tmp_path)
    with browser.prepare():
        component.refetch(browser.recovery_request, browser.start)
    receipt_path = directory / "browser-preparation.json"
    receipt = json.loads(receipt_path.read_bytes())
    if damage == "request_sha256":
        receipt[damage] = "b" * 64
    else:
        receipt["effective_recovery_request"]["request_id"] = "another-click"
    receipt_path.write_text(json.dumps(receipt))
    before = events[:]
    replay, _, _ = _coordinator(component, runtime, tmp_path)
    with pytest.raises(ValueError, match="binding changed|identity changed"):
        with replay.prepare():
            pytest.fail("must not replay changed receipt")
    assert events == before and component.sends == 2 and current_browser_lifecycle_lease() is None


def test_real_engine_enters_host_startup_on_the_owned_thread_and_releases_maintenance_before_provider_work(tmp_path, monkeypatch):
    from offline_fixtures import _fixture_inputs
    from visibility_runner import Runner, RuntimeConfig
    from visibility_panel.execution.worker_registry import WorkerRegistry
    from threading import get_ident
    runtime, _ = _fixture_inputs(tmp_path)
    runtime["profiles"]["lease_lock_root"] = str(tmp_path / "profile-locks")
    runtime["browser"]["user_data_dir"] = str(tmp_path / "synthetic-user-data")
    monkeypatch.setenv(LOCK_PATH_ENV, str(tmp_path / "chrome.lock"))
    events, owned_thread = [], get_ident()

    class RefusingProvider:
        platform = "chatgpt"
        async def open(self, **kwargs):
            events.append("provider-open")
            raise RuntimeError("offline provider refusal; no tab or send")

    runner = Runner(RuntimeConfig.from_mapping(runtime), providers={"chatgpt": RefusingProvider()})
    plan = runner.plan({"scope_kind": "diagnostic", "platforms": ["chatgpt"],
                        "diagnostic_prompts": ["Synthetic offline prompt"]}, run_id="host-startup-bridge")
    runner.create(plan)
    recovery = runner.plan_refetch(plan.run_id, request_id="bridge-click")
    registry = WorkerRegistry(tmp_path / "job")
    request, _ = registry.create_request(request_id="bridge-click", run_id=plan.run_id, mode="refetch",
        bindings={"plan_sha256": plan.plan_sha256}, recovery=recovery)

    def restart(*args, **kwargs):
        assert get_ident() == owned_thread and current_browser_lifecycle_lease().mode == "exclusive"
        events.append("restart")

    monkeypatch.setattr(startup, "_restart_chrome", restart)
    browser = startup.MeasurementBrowserCoordinator(runner, runtime, request,
        root=tmp_path, execution_dir=registry.path(request["execution_id"]))
    with runner.execution(plan.run_id, execution_id=request["execution_id"]):
        with browser.prepare():
            with runner.defer_browser_startup(browser.start):
                result = runner.refetch(browser.recovery_request)
            assert result.status.value != "succeeded"
            assert current_browser_lifecycle_lease().mode == "shared"
            assert runner.repository.recovery_execution is None
    assert events[0] == "restart" and "provider-open" in events
    assert events.count("restart") == 1 and result.transport_counts.send_started == 0
    assert runner.ownership(plan.run_id)["state"] == "available"
    assert current_browser_lifecycle_lease() is None
