"""Automatic verification uses fake native input and owned temporary runs."""
import asyncio
from dataclasses import replace
import json

import pytest

from visibility_runner.api import Runner
from visibility_runner.config import BrowserConfig, Platform
from visibility_runner.errors import IntegrityError, StateTransitionError
from visibility_runner.execution.verification_policy import effective_policy
from visibility_runner.planning.hashing import canonical_sha256, json_value
from visibility_runner.browser.copilot_verification import CopilotVerificationService
from visibility_runner.browser import copilot_verification as service_module
from visibility_runner.websites import chat_actions as chat
from support.execution import _runtime, _typed_plan
from test_copilot_verification_service import _case, _deadline


def make_runner(tmp_path, *, policy="deadline_cleanup_v1", create=True):
    runtime, digest = _runtime(tmp_path)
    runtime = replace(runtime, browser=replace(runtime.browser, copilot_verification_policy=policy))
    plan = _typed_plan(Platform.COPILOT, run_id="automatic-fixture", manifest_sha256=digest)
    plan = replace(plan, scope_manifest={**plan.scope_manifest,
        "platform_data_modes": {"copilot": "copilot_browser_same_chat_v1"}})
    plan = replace(plan, plan_sha256=canonical_sha256({k: v for k, v in json_value(plan).items() if k != "plan_sha256"}))
    runner = Runner(runtime)
    if create:
        runner.create(plan)
    return runner, plan


def test_legacy_never_started_job_opts_in_without_preventing_initial_creation(tmp_path, monkeypatch):
    runner, plan = make_runner(tmp_path, create=False)
    with runner.execution(plan.run_id, execution_id="initial-retry"):
        receipt = runner.enable_automatic_verification(plan.run_id, request_id="refetch-never-started",
            request_sha256="ab" * 32, plan=plan)
        assert receipt["source"] == "explicit_start"
        assert not (runner.repository.run_dir(plan.run_id) / "plan.json").exists()
        monkeypatch.setattr(runner, "_execute", lambda run_id, *args, **kwargs: effective_policy(runner, run_id))
        assert runner.run(plan) == "automatic_hold_v1"
        original = json.loads((runner.repository.run_dir(plan.run_id) / "execution-policy.json").read_text())
        assert receipt["original_policy_sha256"] == original["policy_sha256"]
        assert "copilot_verification" not in original["settings"]


def test_new_automatic_policy_serializes_without_changing_background_mode(tmp_path):
    runner, plan = make_runner(tmp_path, policy="automatic_hold_v1")
    assert runner.runtime.browser.background_only is True
    assert BrowserConfig.from_mapping(runner.runtime.browser.to_mapping()) == runner.runtime.browser
    from visibility_runner.recovery_adapters.execution_policy import read_policy
    policy = read_policy(runner.repository, plan.run_id, runner.repository.read_plan(plan.run_id), runtime=runner.runtime)
    assert policy["settings"]["copilot_verification"]["policy"] == "automatic_hold_v1"
    assert effective_policy(runner, plan.run_id) == "automatic_hold_v1"


def test_legacy_opt_in_is_owned_audited_and_does_not_change_preview_or_frozen_files(tmp_path):
    runner, plan = make_runner(tmp_path)
    root = runner.repository.run_dir(plan.run_id)
    frozen = {name: (root / name).read_bytes() for name in ("plan.json", "execution-policy.json", "runner_state.json")}
    with pytest.raises(StateTransitionError, match="not_owned"):
        runner.enable_automatic_verification(plan.run_id, request_id="refetch-click", request_sha256="ab" * 32)
    with runner.execution(plan.run_id, execution_id="automatic-execution"):
        before = runner.plan_refetch(plan.run_id, request_id="refetch-click")
        receipt = runner.enable_automatic_verification(plan.run_id, request_id="refetch-click", request_sha256="ab" * 32)
        assert receipt["original_policy_sha256"] == json.loads(frozen["execution-policy.json"])["policy_sha256"]
        assert receipt["request_sha256"] == "ab" * 32 and receipt["background_only"]
        assert effective_policy(runner, plan.run_id) == "automatic_hold_v1"
        assert runner.plan_refetch(plan.run_id, request_id="refetch-click") == before
        assert runner.enable_automatic_verification(plan.run_id, request_id="refetch-click", request_sha256="ab" * 32) == receipt
        with pytest.raises(IntegrityError, match="policy_changed"):
            runner.enable_automatic_verification(plan.run_id, request_id="refetch-click", request_sha256="cd" * 32)
        assert {name: (root / name).read_bytes() for name in frozen} == frozen
    assert effective_policy(runner, plan.run_id) == "deadline_cleanup_v1"
    with runner.execution(plan.run_id, execution_id="another-execution"):
        assert effective_policy(runner, plan.run_id) == "deadline_cleanup_v1"


def test_changed_execution_receipt_refuses_automatic_input(tmp_path):
    runner, plan = make_runner(tmp_path)
    with runner.execution(plan.run_id, execution_id="automatic-execution"):
        runner.enable_automatic_verification(plan.run_id, request_id="refetch-click", request_sha256="ab" * 32)
        path = runner._automatic_verification_execution[2]
        path.write_text('{}')
        with pytest.raises(IntegrityError, match="policy_changed"):
            effective_policy(runner, plan.run_id)


@pytest.mark.parametrize("user_switch", [False, True])
def test_automatic_background_challenge_uses_exact_queue_click_then_validates_same_page(monkeypatch, tmp_path, user_switch):
    async def run():
        case = _case(monkeypatch, tmp_path, background_only=True)
        service = CopilotVerificationService(case.queue, case.service.browser, background_only=True,
            automatic_verification=True, notify=lambda observation, detail: case.notices.append((observation, detail)))
        case.submission.human_verification_handler = service
        monkeypatch.setattr(chat, "strict_page_identity", service_module.strict_page_identity)
        async def marker(*args, **kwargs): return case.page.marker
        monkeypatch.setattr(chat, "_visible_access_block_marker", marker)
        original = case.native.attempt
        def click(**kwargs):
            result = original(**kwargs)
            case.page.marker = None
            if user_switch:
                case.page.focused = False
            return result
        case.native.attempt = click
        try:
            await case.submission._resolve_human_verification(_deadline())
            await service(case.submission, _deadline())
            assert case.native.clicks == 1
            assert case.events.count("focus-owned-target") == 1
            if user_switch:
                assert "restore-prior-app" not in case.events
            else:
                assert case.events.index("fake-checkbox-click") < case.events.index("restore-prior-app")
            assert case.native.calls[0]["expected_pid"] == 4321
            assert case.native.calls[0]["expected_url"] == case.page.url
            assert not case.submission._submitted
            assert "manual_required" not in [d["status"] for _, d in case.notices]
        finally:
            assert await case.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("initial,current,expected", [(21, 4321, [21]), (21, 99, []), (4321, 4321, [])])
def test_focus_return_preserves_user_app_switch(monkeypatch, initial, current, expected):
    from types import SimpleNamespace
    import sys
    active = [initial]
    returned = []
    workspace = SimpleNamespace(frontmostApplication=lambda: SimpleNamespace(processIdentifier=lambda: active[0]))
    appkit = SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid:
            SimpleNamespace(activateWithOptions_=lambda options: returned.append(pid))),
        NSApplicationActivateIgnoringOtherApps=1)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    restore = service_module._capture_focus_return(4321)
    active[0] = current
    restore()
    assert returned == expected


def test_automatic_opt_in_rejects_changed_owned_target_before_focus(monkeypatch, tmp_path):
    async def run():
        case = _case(monkeypatch, tmp_path, background_only=True)
        service = CopilotVerificationService(case.queue, case.service.browser, automatic_verification=True)
        case.page.identity_override = replace(case.submission.identity, browser_target_reference="foreign-target")
        try:
            with pytest.raises(chat.IdentityMismatchError):
                await service(case.submission, _deadline())
            assert not case.factories and not case.native.clicks
            assert "focus-owned-target" not in case.events
        finally:
            assert await case.queue.aclose()
    asyncio.run(run())
