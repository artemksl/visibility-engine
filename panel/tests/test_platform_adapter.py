"""Synthetic platform-boundary checks; no Chrome, desktop actions or sends."""
import argparse
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from visibility_panel.execution import measurement_browser as startup
from visibility_panel.execution import worker_registry
from visibility_panel.execution.worker_registry import ProcessIdentity, RegistryError
from visibility_panel.web import jobs
from visibility_panel import paths

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_EXE = str(ROOT / ".synthetic-python")
CHANGED_EXE = str(ROOT / ".different-python")
spec = importlib.util.spec_from_file_location("windows_chrome_launcher", ROOT / "config/launch_logged_in.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_installed_panel_uses_explicit_checkout_root(monkeypatch, tmp_path):
    monkeypatch.setenv("VISIBILITY_ENGINE_ROOT", str(tmp_path))
    assert paths.repo_root() == tmp_path.resolve()
    monkeypatch.setenv("VISIBILITY_ENGINE_ROOT", "relative-checkout")
    with pytest.raises(ValueError, match="absolute"):
        paths.repo_root()


def test_normal_install_resolves_dedicated_environment_not_site_packages(monkeypatch, tmp_path):
    monkeypatch.delenv("VISIBILITY_ENGINE_ROOT", raising=False)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_visibility_measurement.py").touch()
    environment = tmp_path / ".venv"
    monkeypatch.setattr(paths.sys, "prefix", str(environment))
    monkeypatch.setattr(paths, "__file__", str(environment / "Lib" / "site-packages" / "visibility_panel" / "paths.py"))
    assert paths.repo_root() == tmp_path


@pytest.mark.parametrize("platform,prefix,name", [
    ("darwin", "/bin/bash", "launch_logged_in.sh"),
    ("win32", startup.sys.executable, "launch_logged_in.py"),
])
def test_startup_changes_only_launcher_mechanics(tmp_path, platform, prefix, name):
    assert startup._launcher_command(tmp_path, platform=platform) == [
        prefix, str(tmp_path / "config" / name)]


@pytest.mark.parametrize("endpoint", [
    "https://127.0.0.1:9222", "http://example.test:9222", "http://127.0.0.1",
    "http://user@127.0.0.1:9222", "http://127.0.0.1:9222/private",
])
def test_windows_launcher_rejects_nonlocal_debug_binding(endpoint):
    with pytest.raises(ValueError, match="loopback"):
        launcher.loopback_address(endpoint)


def test_windows_launcher_preserves_private_selected_manifest_and_lifecycle(tmp_path, monkeypatch):
    user_data = tmp_path / "user-data"
    for directory in ("Profile 1", "Profile 2", "Profile 3"):
        (user_data / directory).mkdir(parents=True)
    manifest = tmp_path / "selected.json"
    manifest.write_text(json.dumps([{"id": "second", "chrome_directory": "Profile 2"},
                                    {"id": "first", "chrome_directory": "Profile 1"}]))
    binary = tmp_path / "chrome.exe"
    binary.touch()
    events = []
    monkeypatch.setattr(launcher, "launcher_values", lambda: {"BROWSER_CMD": str(binary)})
    monkeypatch.setattr(launcher, "prepare", lambda root, directories, **kw:
                        events.append(("validate" if kw.get("validate_only") else "clean", directories)))

    def stop(binary, **kwargs):
        from visibility_panel.browser.browser_lifecycle import current_browser_lifecycle_lease
        assert current_browser_lifecycle_lease().mode == "exclusive"
        events.append(("close", kwargs["force"]))
    monkeypatch.setattr(launcher, "stop_chrome", stop)
    monkeypatch.setattr(launcher, "limit_restoration", lambda root, first: events.append(("restore", first)))
    monkeypatch.setattr(launcher, "open_profile", lambda binary, root, directory, anchor, address:
                        events.append(("open", directory, address)))
    monkeypatch.setattr(launcher, "wait_debug", lambda address: events.append(("ready", address)))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    monkeypatch.setenv("CITEUP_BROWSER_LIFECYCLE_LOCK_PATH", str(tmp_path / "chrome.lock"))
    monkeypatch.delenv("CITEUP_BROWSER_LIFECYCLE_LOCK_HELD", raising=False)
    launcher.launch_windows(argparse.Namespace(profiles_path=str(manifest), cdp_url="http://localhost:9333",
        user_data_dir=str(user_data), clean_start=True, force_close=True))
    assert events == [
        ("validate", ["Profile 2", "Profile 1"]), ("close", True),
        ("clean", ["Profile 2", "Profile 1"]), ("restore", "Profile 2"),
        ("open", "Profile 2", ("127.0.0.1", 9333)), ("ready", ("127.0.0.1", 9333)),
        ("open", "Profile 1", None)]


@pytest.mark.parametrize("rows", [[], [{"id": "x", "chrome_directory": "missing"}],
    [{"id": "x", "chrome_directory": "../saved"}],
    [{"id": "x", "chrome_directory": "Default"}, {"id": "y", "chrome_directory": "Default"}]])
def test_invalid_selection_fails_before_shutdown(tmp_path, monkeypatch, rows):
    binary = tmp_path / "chrome.exe"; binary.touch()
    (tmp_path / "Default").mkdir()
    manifest = tmp_path / "selected.json"; manifest.write_text(json.dumps(rows))
    monkeypatch.setattr(launcher, "launcher_values", lambda: {"BROWSER_CMD": str(binary)})
    monkeypatch.setattr(launcher, "stop_chrome", lambda *_a, **_kw: pytest.fail("invalid selection must not close Chrome"))
    with pytest.raises(ValueError):
        launcher.launch_windows(argparse.Namespace(profiles_path=str(manifest), cdp_url="http://127.0.0.1:9222",
            user_data_dir=str(tmp_path), clean_start=True, force_close=True))


def test_profile_anchor_retains_exact_directory_identity(tmp_path):
    anchor = tmp_path / "profile_anchor.html"
    assert launcher.anchor_url(str(anchor), "Profile 2") == anchor.as_uri() + "#citeup-profile=Profile%202"


def test_restoration_changes_only_selected_startup_fields(tmp_path):
    original = {"profile": {"last_used": "Profile 99", "last_active_profiles": ["Profile 99"],
                             "info_cache": {"Default": {"name": "Saved account"}}},
                "other": {"retained": True}}
    path = tmp_path / "Local State"; path.write_text(json.dumps(original))
    launcher.limit_restoration(tmp_path, "Default")
    expected = {**original, "profile": {**original["profile"], "last_used": "Default", "last_active_profiles": ["Default"]}}
    assert json.loads(path.read_text()) == expected


@pytest.mark.parametrize("observed", [None,
    ProcessIdentity(11, 11.0, SYNTHETIC_EXE, "same-boot"),
    ProcessIdentity(11, 10.0, CHANGED_EXE, "same-boot"),
    ProcessIdentity(11, 10.0, SYNTHETIC_EXE, "different-boot")])
def test_stop_never_targets_absent_reused_or_changed_process(monkeypatch, observed):
    expected = ProcessIdentity(11, 10.0, SYNTHETIC_EXE, "same-boot")
    process = SimpleNamespace(pid=11, poll=lambda: None)
    monkeypatch.setattr(jobs, "process_identity", lambda _: observed)
    monkeypatch.setattr(jobs, "terminate_process_tree", lambda *_a, **_kw: pytest.fail("identity changed"))
    assert jobs._terminate_verified_child(process, expected) is False


def test_stop_cleans_only_verified_child_tree(monkeypatch):
    expected = ProcessIdentity(11, 10.0, SYNTHETIC_EXE, "same-boot")
    calls = []
    monkeypatch.setattr(jobs, "process_identity", lambda _: expected)
    monkeypatch.setattr(jobs, "terminate_process_tree", lambda pid, **guards: calls.append((pid, guards)))
    assert jobs._terminate_verified_child(SimpleNamespace(pid=11, poll=lambda: None), expected)
    assert calls == [(11, {"expected_created": expected.created, "expected_executable": expected.executable})]


def test_stop_retains_unknown_identity_without_signal(monkeypatch):
    expected = ProcessIdentity(11, 10.0, SYNTHETIC_EXE, "same-boot")
    def unavailable(_):
        raise RegistryError("process_inspection_failed")
    monkeypatch.setattr(jobs, "process_identity", unavailable)
    assert jobs._terminate_verified_child(SimpleNamespace(pid=11, poll=lambda: None), expected) is False


@pytest.mark.parametrize("injected", [False, True])
def test_watchdog_keeps_captured_identity_and_callback_contract(tmp_path, monkeypatch, injected):
    identity = ProcessIdentity(11, 10.0, SYNTHETIC_EXE, "same-boot")
    sample = {"state": "unresponsive", "process_state": "alive", "pid": identity.pid,
              "identity": asdict(identity),
              "heartbeat": {"operations": {"overdue": {"deadline_monotonic": 10}}}}
    (tmp_path / "containment.json").write_text(json.dumps({"started_monotonic": 50}))
    registry = SimpleNamespace(path=lambda _: tmp_path, observe=lambda *_a, **_kw: sample)
    calls = []
    monkeypatch.setattr(worker_registry, "platform_signal_process",
                        lambda pid, **guards: calls.append((pid, guards)))
    options = ({"signal_process": lambda pid, number: calls.append((pid, number))} if injected else {})
    watchdog = worker_registry.WorkerWatchdog(registry, "execution", request_cancel=lambda: None,
                                              clock=lambda: 100, **options)
    assert watchdog.tick(wall=1000) == "terminate"
    expected = [(11, worker_registry.signal.SIGTERM)] if injected else [
        (11, {"force": False, "expected_created": identity.created,
              "expected_executable": identity.executable})]
    assert calls == expected
