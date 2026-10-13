"""Authorized restart disposition and context recovery, with no live browser."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from visibility_runner.browser import restart
from visibility_runner.errors import StateTransitionError
from visibility_runner.websites import chat_actions as chat
from visibility_runner.websites.conversation_binding import ConversationCorrespondenceError
from support.conversation import Page, Item, SPEC, binding, identity


def restart_authorizer():
    owner = SimpleNamespace(assert_owned=lambda: None)
    receipt = restart.RestartAuthorization("synthetic-start", owner, frozenset({("fixture", "Profile 99")}))
    session = SimpleNamespace(owner=owner, restart_authorization=receipt)
    repository = SimpleNamespace(active_session=session)
    attachment = SimpleNamespace(attached_health=SimpleNamespace(browser_session_id="synthetic-generation"),
        session=SimpleNamespace(anchors=(SimpleNamespace(profile=SimpleNamespace(id="fixture", chrome_directory="Profile 99"),
            browser_context_id="new-context"),)))
    return restart.attachment_rebind_authorizer(repository, attachment), repository, attachment


@pytest.mark.parametrize("permitted", [False, True])
def test_reopened_history_after_restart_preserves_send_fence(monkeypatch, permitted):
    async def observed(page, **kwargs):
        return replace(identity(page), browser_context_reference="new-context")
    monkeypatch.setattr(chat, "strict_page_identity", observed)
    async def run():
        page = Page()
        async def factory(*args): return page
        if permitted:
            factory._authorize_restarted_context = restart_authorizer()[0]
        provider = chat.PageProvider(factory, SPEC)
        saved = binding(); before = copy.deepcopy(saved)
        if not permitted:
            with pytest.raises(ConversationCorrespondenceError, match="context_mismatch"):
                await provider.reopen(saved, observation_id="synthetic")
            assert page.closed
            return
        submission = await provider.reopen(saved, observation_id="synthetic")
        submission.restore_recovery_attempt(prompt="measurement", response_baseline=saved["response_baseline"], binding=saved)
        assert submission._submitted
        with pytest.raises(Exception):
            await submission.submit_once()
        recovered = await submission.reconcile_no_response(.5)
        assert recovered.answer.text == "Alpha Clinic" and saved == before
        assert submission._recovery_binding["identity"]["browser_context_reference"] == "new-context"
        await submission.close()
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["profile", "history", "context_after_navigation"])
def test_restart_never_relaxes_profile_history_or_navigation_checks(monkeypatch, changed):
    page = Page()
    if changed == "history": page.users.append(Item("operator turn", "u2"))
    async def observed(page, **kwargs):
        value = replace(identity(page), browser_context_reference="new-context")
        if changed == "profile": value = replace(value, profile_id="other")
        if changed == "context_after_navigation" and page.navigated:
            value = replace(value, browser_context_reference="another-context")
        return value
    monkeypatch.setattr(chat, "strict_page_identity", observed)
    async def run():
        async def factory(*args): return page
        factory._authorize_restarted_context, repository, _attachment = restart_authorizer()
        with pytest.raises(ConversationCorrespondenceError):
            await chat.PageProvider(factory, SPEC).reopen(binding(), observation_id="synthetic")
        assert page.closed
        if changed == "context_after_navigation":
            assert repository.active_session.restart_authorization is None
    asyncio.run(run())


def fixture(tmp_path, monkeypatch, *, outcome="completed", state="available"):
    from test_copilot_verification_holds import case, deadline
    c = case(tmp_path, monkeypatch)
    row = asyncio.run(c.holds.begin(c.submission, deadline()))
    asyncio.run(c.queue.aclose())
    runtime = SimpleNamespace(profiles=SimpleNamespace(lease_lock_root=c.manager.lock_root),
        browser=SimpleNamespace(user_data_dir=c.manager.namespace), selected_profiles=(c.profile,), tabs_per_provider=2)
    owner = SimpleNamespace(execution_id="execution-current", assert_owned=lambda: None)
    plan = {"plan_sha256": c.store.plan_sha256, "packets": [{"profile_id": c.profile.id,
        "platform": "copilot", "observation_ids": [row["observation_id"]]}]}
    runner = SimpleNamespace(runtime=runtime, _owners={row["run_id"]: owner}, browser_runtime_factory=None,
        repository=SimpleNamespace(read_plan=lambda run_id: plan,
            active_session=SimpleNamespace(owner=owner, restart_authorization=None)),
        ownership=lambda run_id: {"state": state, "execution_id": owner.execution_id})
    calls = []
    def dispose(target, run_id, **command):
        calls.append(command)
        if outcome == "completed":
            c.store.transition(row["episode_id"], owner=owner, audit=c.record,
                changes={"retained": False, "status": "closed"})
        return {"status": outcome}
    monkeypatch.setattr(restart, "control", dispose)
    return c, runner, row, calls


def test_restart_disposes_original_hold_without_changing_effects(tmp_path, monkeypatch):
    c, runner, row, calls = fixture(tmp_path, monkeypatch)
    result = restart.prepare(runner, request_id="authorized-start")
    assert result["closed_verification_episodes"] == [row["episode_id"]]
    assert c.store.rows() == ()
    assert calls[0]["action"] == "close" and calls[0]["execution_id"] == row["execution_id"]
    receipt = runner.repository.active_session.restart_authorization
    assert receipt.profiles == {(c.profile.id, c.profile.chrome_directory)} and receipt.owner is runner._owners[row["run_id"]]
    assert not c.native and c.page.focus_count == 0


def test_restart_refuses_unconfirmed_close_and_preserves_hold(tmp_path, monkeypatch):
    c, runner, row, calls = fixture(tmp_path, monkeypatch, outcome="refused")
    with pytest.raises(StateTransitionError, match="close_unconfirmed"):
        restart.prepare(runner, request_id="authorized-start")
    assert c.store.rows()[0]["retained"] and runner.repository.active_session.restart_authorization is None


@pytest.mark.parametrize("changed", ["generation", "context", "owner_lost"])
def test_restart_receipt_is_invalidated_when_live_binding_changes(changed):
    authorize, repository, attachment = restart_authorizer()
    owner = repository.active_session.owner
    assert authorize("fixture", "Profile 99", "new-context")
    if changed == "generation":
        attachment.attached_health.browser_session_id = "other-generation"
    elif changed == "context":
        attachment.session.anchors[0].browser_context_id = "other-context"
    else:
        def lost(): raise StateTransitionError("synthetic_owner_lost")
        owner.assert_owned = lost
    with pytest.raises(StateTransitionError):
        restart.attachment_rebind_authorizer(repository, attachment)
    assert repository.active_session.restart_authorization is None
    assert not authorize("fixture", "Profile 99", "new-context")


def test_restart_permission_spans_real_provider_scopes_but_not_later_execution(tmp_path, monkeypatch):
    from support.execution import _runtime, _typed_plan
    from visibility_runner.api import Runner, Platform
    runtime, digest = _runtime(tmp_path)
    plan = _typed_plan(Platform.CHATGPT, run_id="restart-scopes", manifest_sha256=digest)
    profile = runtime.selected_profiles[0]
    pages = []
    class Runtime:
        def __init__(self, _config): pass
        async def attach(self):
            async def open_page(*_args):
                page = Page(); pages.append(page); return page
            anchor = SimpleNamespace(profile=profile, browser_context_id="new-context")
            return SimpleNamespace(attached_health=SimpleNamespace(browser_session_id="new-generation"),
                session=SimpleNamespace(anchors=(anchor,)), open_page=open_page,
                manifest=SimpleNamespace(profile=lambda _id: profile),
                lease_manager=SimpleNamespace(acquire=lambda *_args, **_kwargs: SimpleNamespace(release=lambda: None)))
        async def close(self):
            for page in pages: page.closed = True
    runner = Runner(runtime, browser_runtime_factory=Runtime)
    runner.create(plan)
    async def observed(page, **_kwargs):
        return replace(identity(page), profile_id=profile.id, chrome_directory=profile.chrome_directory,
                       browser_context_reference="new-context")
    monkeypatch.setattr(chat, "strict_page_identity", observed)
    saved = binding()
    saved["identity"].update(profile_id=profile.id, chrome_directory=profile.chrome_directory)
    before = copy.deepcopy(saved)
    async def reopen():
        async with runner._provider_scope(plan.run_id, runner.repository.read_plan(plan.run_id)["packets"]) as providers:
            provider = providers.require("chatgpt")
            provider.spec = SPEC
            submission = await provider.reopen(saved, observation_id=plan.observation_ids[0])
            await submission.close()
    with runner.execution(plan.run_id, execution_id="authorized-execution"):
        runner.prepare_browser_restart(request_id="one-startup")
        receipt = runner.repository.active_session.restart_authorization
        asyncio.run(reopen())
        asyncio.run(reopen())
        assert runner.repository.active_session.restart_authorization is receipt
        assert len(pages) == 2 and saved == before
    assert receipt.browser_session_id == "new-generation"
    with runner.execution(plan.run_id, execution_id="later-resume"):
        assert runner.repository.active_session.restart_authorization is None
        with pytest.raises(ConversationCorrespondenceError, match="context_mismatch"):
            asyncio.run(reopen())
    assert len(pages) == 3 and saved == before


def test_restart_refuses_unknown_owner_before_disposition(tmp_path, monkeypatch):
    c, runner, row, calls = fixture(tmp_path, monkeypatch, state="unknown")
    with pytest.raises(StateTransitionError, match="owner_unavailable"):
        restart.prepare(runner, request_id="authorized-start")
    assert not calls and c.store.rows()[0]["retained"]


def test_restart_refuses_unselected_hold_without_touching_it(tmp_path, monkeypatch):
    c, runner, row, calls = fixture(tmp_path, monkeypatch)
    from visibility_runner.browser.profile_matching import BrowserProfile
    runner.runtime.selected_profiles = (BrowserProfile(id="other", display_name="Other", chrome_directory="Other Profile"),)
    with pytest.raises(StateTransitionError, match="unselected_verification_hold"):
        restart.prepare(runner, request_id="authorized-start")
    assert not calls and c.store.rows()[0]["retained"]


@pytest.mark.parametrize("owned,deferred", [(False, False), (True, True)])
def test_public_restart_closes_stopped_hold_before_browser_generation_changes(tmp_path, monkeypatch, owned, deferred):
    from contextlib import nullcontext
    from test_copilot_verification_holds import case, deadline
    from support.execution import _runtime, _typed_plan
    from visibility_runner.api import Runner
    from visibility_runner.config import Platform
    from visibility_runner.browser.verification_controls import store_for_runtime
    from visibility_runner.browser.verification_holds import CopilotVerificationHolds
    from visibility_runner.recovery.api import RecoveryLedger, RecoveryCoordinator
    c = case(tmp_path, monkeypatch, profile_id="profile_a", directory="Default")
    runtime, digest = _runtime(tmp_path)
    runtime = replace(runtime, profiles=replace(runtime.profiles, lease_lock_root=c.manager.lock_root),
        browser=replace(runtime.browser, user_data_dir=tmp_path / "synthetic-user-data",
            copilot_verification_policy="manual_hold_v1"))
    plan = _typed_plan(Platform.COPILOT, run_id="stopped-hold", manifest_sha256=digest)
    plan = replace(plan, scope_manifest={**plan.scope_manifest,
        "platform_data_modes": {"copilot": "copilot_browser_same_chat_v1"}})
    from visibility_runner.planning.hashing import canonical_sha256, json_value
    plan = replace(plan, plan_sha256=canonical_sha256({k: v for k, v in json_value(plan).items() if k != "plan_sha256"}))
    attaches = []
    class FakeRuntime:
        def __init__(self, actual):
            assert actual.browser.background_only and actual.browser.launch_policy.value == "attach_only"
        async def attach(self):
            attaches.append("attach"); return c.attachment
        async def close(self): return await c.registry.close_all()
    runner = Runner(runtime, browser_runtime_factory=FakeRuntime)
    runner.create(plan)
    c.submission.observation_id = plan.observation_ids[0]
    c.store = store_for_runtime(runtime, plan.run_id, plan.plan_sha256)
    c.manager = c.store.manager
    c.attachment.lease_manager = c.manager
    c.attachment.manifest = SimpleNamespace(profile=lambda _: c.profile)
    async def restore(platform, profile_id, item, fresh, expected):
        assert not fresh and expected.tab_reference == c.identity.tab_reference
        return c.page
    c.attachment.provider_page_factory = restore
    ledger = RecoveryLedger(runner.repository.run_dir(plan.run_id), watch_changes=False)
    coordinator = RecoveryCoordinator(ledger)
    c.recovery.coordinator = coordinator; c.recovery.record_resource = coordinator.record_resource
    with runner.execution(plan.run_id) as owner:
        c.recovery.owner = owner
        c.holds = CopilotVerificationHolds(c.store, c.attachment, c.recovery, c.queue, cancelled=lambda: False)
        row = asyncio.run(c.holds.begin(c.submission, deadline()))
    ledger.close()
    recovery_request = runner.plan_refetch(plan.run_id, request_id="refetch-click")
    original_request = copy.deepcopy(recovery_request)
    with runner.execution(plan.run_id, execution_id="new-execution") if owned else nullcontext():
        def assert_restart(result):
            # Foreign work on another platform cannot acquire the saved profile
            # while Chrome is being shut down/reopened.
            with pytest.raises(Exception):
                c.manager.acquire((c.profile,), owner="foreign", provider="gemini", timeout_seconds=0)
            return result
        if deferred:
            from contextlib import AsyncExitStack
            from visibility_runner.execution.manage_measurements import _SynchronousContexts, _run_sync
            contexts = _SynchronousContexts()
            async def startup():
                async with AsyncExitStack() as stack:
                    result = await contexts.enter_context(stack, lambda: runner.browser_restart(
                        request_id="authorized-start", recovery_request=recovery_request))
                    return assert_restart(result)
            result = _run_sync(startup(), synchronous_contexts=contexts)
        else:
            with runner.browser_restart(request_id="authorized-start", recovery_request=recovery_request) as result:
                assert_restart(result)
    assert result["closed_verification_episodes"] == [row["episode_id"]]
    assert c.page.closed and c.store.rows() == () and attaches == ["attach"]
    assert not c.native and c.page.focus_count == 0 and not c.submission._submitted
    fresh = runner.plan_refetch(plan.run_id, request_id="refetch-click")
    assert result["recovery_request"] == fresh
    assert runner._restart_recovery_request == (original_request, fresh)
    assert recovery_request == original_request and original_request["scope_digest"] == fresh["scope_digest"]
    with c.manager.acquire((c.profile,), owner="after-startup", provider="gemini", timeout_seconds=.1):
        pass
    asyncio.run(c.queue.aclose())
