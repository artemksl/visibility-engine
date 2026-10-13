"""Durable verification lifecycle; synthetic identities, no browser/network input."""
import asyncio
import copy
from dataclasses import replace
import json
from types import SimpleNamespace
import time

import pytest

from visibility_runner.browser.profile_matching import BrowserProfile
from visibility_runner.browser.profile_access import ProfileReservationManager, ProfileVerificationHeld, read_verification_marker, _verification_digest
from visibility_runner.browser.tabs import OwnedTab, OwnedTabRegistry, TabSessionIdentity
from visibility_runner.browser.verification_holds import VerificationHoldStore, CopilotVerificationHolds
from visibility_runner.browser.verification_controls import execute_live_command, _target_absent, _exact_page
from visibility_runner.browser.desktop_actions import DesktopActionQueue
from visibility_runner.browser.copilot_verification import CopilotVerificationService
from visibility_runner.errors import HumanVerificationRequiredError, IntegrityError, StateTransitionError
from visibility_runner.websites import chat_actions as chat
from visibility_runner.browser import profile_matching, copilot_verification
from visibility_runner.websites.copilot import COPILOT_SPEC


def case(tmp_path, monkeypatch, *, profile_id="synthetic", directory="Synthetic Profile"):
    monkeypatch.setattr(copilot_verification, "_capture_focus_return", lambda pid: lambda: None)
    profile = BrowserProfile(id=profile_id, display_name="Synthetic", chrome_directory=directory)
    manager = ProfileReservationManager(tmp_path / "leases", namespace="synthetic-user-data")
    store = VerificationHoldStore(manager, (profile,), run_id="synthetic-run", plan_sha256="ab"*32)
    owner = SimpleNamespace(execution_id="execution-original", assert_owned=lambda: None)
    audit = []
    def record(*args, **kw): audit.append((args, kw))
    identity = chat.BrowserIdentity(profile_id=profile.id, chrome_directory=profile.chrome_directory,
        browser_context_reference="synthetic-context", tab_reference="synthetic-tab",
        browser_target_reference="cdp-target:synthetic-target", conversation_reference="https://copilot.com/chats/synthetic")
    page = SimpleNamespace(url=identity.conversation_reference, closed=False, marker="human_verification_required", focus_count=0)
    async def close(): page.closed=True
    async def focus(): page.focus_count+=1
    page.close=close; page.is_closed=lambda: page.closed; page.bring_to_front=focus
    registry=OwnedTabRegistry()
    tab=OwnedTab(page, TabSessionIdentity(**identity.to_mapping(), provider="copilot"), "about:blank")
    registry.add(tab)
    history={"messages": [], "generating": False}
    async def observed(*a, **kw): return replace(identity, conversation_reference=page.url)
    async def markers(*a, **kw): return page.marker
    async def read_history(*a, **kw): return copy.deepcopy(history)
    async def target(*a, **kw): return "synthetic-target"
    monkeypatch.setattr(chat, "strict_page_identity", observed)
    monkeypatch.setattr(copilot_verification, "strict_page_identity", observed)
    monkeypatch.setattr(chat, "_visible_access_block_marker", markers)
    monkeypatch.setattr(copilot_verification, "_visible_access_block_marker", markers)
    monkeypatch.setattr(chat, "_conversation_history", read_history)
    monkeypatch.setattr(profile_matching, "browser_target_id", target)
    ledger=SimpleNamespace(select=lambda *a, **kw: [])
    recovery=SimpleNamespace(owner=owner, record_resource=record, pass_id="pass-synthetic", attempts={},
        finished=set(), failed=set(), coordinator=SimpleNamespace(ledger=ledger))
    attachment=SimpleNamespace(attached_health=SimpleNamespace(browser_session_id="browser-generation"),
        session=SimpleNamespace(registry=registry), config=SimpleNamespace(browser=SimpleNamespace(background_only=True)))
    native=[]
    def no_native(): native.append(True); raise AssertionError("native forbidden")
    queue=DesktopActionQueue(no_native, lock_path=tmp_path / "desktop.lock")
    holds=CopilotVerificationHolds(store, attachment, recovery, queue, cancelled=lambda: False)
    submission=chat.PageSubmission(page=page, profile_id=profile.id, chrome_directory=profile.chrome_directory,
        observation_id="synthetic-observation", spec=COPILOT_SPEC, identity=identity)
    service=CopilotVerificationService(queue, None, background_only=True, holds=holds)
    submission.human_verification_handler=service
    return SimpleNamespace(**locals())


def deadline(seconds=1): return chat._BrowserOperationDeadline.after(seconds, timeout_message="synthetic deadline")


@pytest.mark.parametrize("url,allowed", [
    ("https://copilot.com/chat", True),
    ("https://copilot.com/", True),
    ("https://copilot.com/chat?different_tracking=value", True),
    ("https://copilot.com/chat/arbitrary", False),
    ("https://copilot.com/chat/conversation/other", False),
    ("https://another.example/chat", False),
    ("https://copilot.com.evil.example/chat", False),
    ("https://user@copilot.com/chat", False),
    ("http://copilot.com/chat", False),
])
def test_unsent_preparation_control_accepts_only_registered_fresh_routes(tmp_path, monkeypatch, url, allowed):
    async def run():
        c = case(tmp_path, monkeypatch)
        expected = replace(c.identity, conversation_reference="https://copilot.com/chat?fromcode=cmmwuwpzorz&es=Click")
        row = {"identity": expected.to_mapping(), "profile_id": c.profile.id,
               "browser_session_id": "browser-generation", "phase": "preparation",
               "dispatch_state": "not_admitted", "blocker_kind": "other"}
        async def observed(*args, **kwargs):
            return replace(expected, conversation_reference=url)
        monkeypatch.setattr(chat, "strict_page_identity", observed)
        if allowed:
            await _exact_page(c.attachment, c.page, row, deadline())
        else:
            with pytest.raises(chat.IdentityMismatchError):
                await _exact_page(c.attachment, c.page, row, deadline())
        assert c.native == [] and c.page.focus_count == 0 and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("phase,state,kind", [
    ("pre_send", "not_admitted", "other"),
    ("preparation", "admitted_or_unknown", "other"),
    ("response", "admitted_or_unknown", "verification"),
    ("response", "admitted_or_unknown", "sign_in"),
])
def test_control_after_preparation_keeps_conversation_equality(tmp_path, monkeypatch, phase, state, kind):
    async def run():
        c = case(tmp_path, monkeypatch)
        expected = replace(c.identity, conversation_reference="https://copilot.com/chat?fromcode=cmmwuwpzorz&es=Click") if phase != "response" else c.identity
        row = {"identity": expected.to_mapping(), "profile_id": c.profile.id,
               "browser_session_id": "browser-generation", "phase": phase,
               "dispatch_state": state, "blocker_kind": kind}
        current_url = "https://copilot.com/chat" if phase != "response" else "https://copilot.com/chats/other"
        async def observed(*args, **kwargs):
            return replace(expected, conversation_reference=current_url)
        monkeypatch.setattr(chat, "strict_page_identity", observed)
        with pytest.raises(chat.IdentityMismatchError):
            await _exact_page(c.attachment, c.page, row, deadline())
        assert c.native == [] and c.page.focus_count == 0 and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


def test_preparation_sign_in_control_inspects_only_supported_auth_redirects(tmp_path, monkeypatch):
    async def run():
        c = case(tmp_path, monkeypatch)
        expected = replace(c.identity, conversation_reference="https://copilot.com/chat?fromcode=synthetic")
        row = {"identity": expected.to_mapping(), "profile_id": c.profile.id,
               "browser_session_id": "browser-generation", "phase": "preparation",
               "dispatch_state": "not_admitted", "blocker_kind": "sign_in"}
        async def observed(*args, **kwargs):
            return replace(expected, conversation_reference=c.page.url)
        monkeypatch.setattr(chat, "strict_page_identity", observed)
        for url in ("https://login.live.com/oauth20_authorize.srf?state=synthetic",
                    "https://login.microsoftonline.com/common/oauth2/authorize",
                    "https://copilot.com/login?next=synthetic"):
            c.page.url = url
            await _exact_page(c.attachment, c.page, row, deadline())
        for url in ("https://login.live.com.evil.example/", "https://user@login.live.com/"):
            c.page.url = url
            with pytest.raises(chat.IdentityMismatchError):
                await _exact_page(c.attachment, c.page, row, deadline())
        c.page.url = "https://login.live.com/oauth20_authorize.srf?state=synthetic"
        for changes in ({"blocker_kind": "other"}, {"phase": "response"},
                        {"dispatch_state": "admitted_or_unknown"}):
            with pytest.raises(chat.IdentityMismatchError):
                await _exact_page(c.attachment, c.page, {**row, **changes}, deadline())
        assert c.native == [] and c.page.focus_count == 0 and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("changed", ["profile_id", "chrome_directory", "browser_context_reference", "tab_reference", "browser_target_reference"])
def test_fresh_preparation_control_still_requires_exact_owned_identity(tmp_path, monkeypatch, changed):
    async def run():
        c = case(tmp_path, monkeypatch)
        expected = replace(c.identity, conversation_reference="https://copilot.com/chat?fromcode=cmmwuwpzorz&es=Click")
        row = {"identity": expected.to_mapping(), "profile_id": c.profile.id,
               "browser_session_id": "browser-generation", "phase": "preparation",
               "dispatch_state": "not_admitted", "blocker_kind": "other"}
        async def observed(*args, **kwargs):
            return replace(expected, conversation_reference="https://copilot.com/chat", **{changed: "different"})
        monkeypatch.setattr(chat, "strict_page_identity", observed)
        with pytest.raises(chat.IdentityMismatchError):
            await _exact_page(c.attachment, c.page, row, deadline())
        assert c.native == [] and c.page.focus_count == 0 and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("action,submitted,expected", [
    ("navigate", False, "not_admitted"),
    ("retrieve_existing", False, "not_admitted"),
    ("submission", False, "admitted_or_unknown"),
    ("unknown_future_effect", False, "admitted_or_unknown"),
    (None, False, "admitted_or_unknown"),
    ("navigate", True, "admitted_or_unknown"),
])
def test_hold_distinguishes_page_access_from_prompt_admission(tmp_path, monkeypatch, action, submitted, expected):
    async def run():
        c = case(tmp_path, monkeypatch)
        c.submission._submitted = submitted
        def events(kind, **kwargs):
            if kind == "attempt_started":
                return [{"payload": {"pass_id": c.recovery.pass_id, "attempt_id": "current-attempt",
                    "operation_id": "synthetic-observation:measurement_turn_1"}}]
            assert kwargs["attempt_id"] == "current-attempt"
            return [{"payload": {"action": action}}]
        c.ledger.select = events
        budget = deadline()
        row = await c.holds.begin(c.submission, budget)
        assert row["dispatch_state"] == expected
        c.page.marker = None
        if expected == "not_admitted":
            await c.holds.validated(c.submission, budget)
            assert c.store.rows() == () and not c.submission._submitted
        elif not submitted:
            with pytest.raises(HumanVerificationRequiredError, match="dispatch_requires_reconciliation"):
                await c.holds.validated(c.submission, budget)
            assert c.store.rows()[0]["retained"]
        assert c.store.rows(retained_only=False)[0]["deadline_monotonic"] == budget.expires_at
        assert c.native == [] and c.page.focus_count == 0
        assert await c.queue.aclose()
    asyncio.run(run())


def test_public_commands_use_request_time_instead_of_sorted_json_keys(tmp_path, monkeypatch):
    async def run():
        c = case(tmp_path, monkeypatch)
        await c.holds.begin(c.submission, deadline())
        for action, request_id in [("show", "z-old-show"), ("check", "a-new-check")]:
            row = c.store.rows()[0]
            command = c.store.request(episode_id=row["episode_id"], revision=row["revision"],
                execution_id=row["execution_id"], request_id=request_id, action=action)
            if action == "check": c.page.marker = None
            await execute_live_command(c.holds, c.submission, command)
        projection = c.store.public()[0]
        assert [command["action"] for command in projection["commands"]] == ["show", "check"]
        assert projection["verification_cleared"] is True and projection["retained"]
        assert not c.submission._submitted and c.native == []
        assert await c.queue.aclose()
    asyncio.run(run())


def test_empty_readout_does_not_probe_boot_or_native_processes(tmp_path, monkeypatch):
    c=case(tmp_path,monkeypatch)
    from visibility_runner.execution import run_ownership
    def forbidden():
        raise AssertionError('empty blocker projection must not start a system process')
    monkeypatch.setattr(run_ownership,'_boot_identity',forbidden)
    assert c.store.public()==[] and c.native==[] and c.page.focus_count==0


def test_deadline_retains_tab_and_blocks_only_exact_pair(tmp_path, monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        with pytest.raises(HumanVerificationRequiredError):
            await c.submission._resolve_human_verification(deadline(.025))
        row=c.store.rows()[0]
        assert row["status"]=="expired_held" and row["dispatch_state"]=="not_admitted"
        assert row["phase"]=="preparation" and row["turn"]==1
        assert c.store.public()[0]["deadline_expired"]
        await c.service.close_page(c.submission)
        report=await c.registry.close_all()
        assert report.complete and report.retained_tab_references==(c.tab.identity.tab_reference,)
        assert report.closed_tab_references==() and not c.page.closed
        with pytest.raises(ProfileVerificationHeld):
            c.manager.acquire((c.profile,), owner="other", provider="copilot", timeout_seconds=.1)
        with c.manager.acquire((c.profile,), owner="other", provider="gemini", timeout_seconds=.1): pass
        with pytest.raises(ProfileVerificationHeld):
            c.manager.acquire_containment((c.profile,), owner="cleanup",provider="copilot",resource_references=("tab:synthetic-tab",),timeout_seconds=.1)
        with c.manager.acquire_verification(c.profile,owner="control",resource_references=("tab:synthetic-tab",),timeout_seconds=.1): pass
        assert c.native==[] and c.page.focus_count==0
        assert await c.queue.aclose()
    asyncio.run(run())


def test_clearance_resumes_same_operation_after_checks_without_mouse(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        async def clear():
            while not c.store.rows(): await asyncio.sleep(.001)
            c.page.marker=None
        task=asyncio.create_task(clear())
        await c.submission._resolve_human_verification(deadline())
        await task
        assert c.store.rows()==() and not c.submission._submitted
        assert c.store.rows(retained_only=False)[0]["status"]=="resumed"
        assert c.native==[] and c.page.focus_count==0
        assert (await c.registry.close_all()).closed_tab_references==("synthetic-tab",)
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["history","conversation"])
def test_operator_changes_do_not_admit_pending_send(tmp_path,monkeypatch,mutation):
    async def run():
        c=case(tmp_path,monkeypatch)
        async def change():
            while not c.store.rows() or c.store.rows()[0]["status"]=="intent_pending": await asyncio.sleep(.001)
            if mutation=="history": c.history["messages"].append({"role":"user","digest":"operator-sent"})
            else: c.page.url="https://copilot.com/chats/other"
            c.page.marker=None
        task=asyncio.create_task(change())
        with pytest.raises((HumanVerificationRequiredError, chat.IdentityMismatchError)):
            await c.submission._resolve_human_verification(deadline())
        await task
        assert c.store.rows()[0]["retained"] and not c.submission._submitted
        assert not c.page.closed and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())


def test_check_expired_is_readonly_and_show_focuses_once_per_request(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        await c.holds.begin(c.submission,deadline())
        c.holds.update(c.submission,status="expired_held",deadline_monotonic=time.monotonic()-1)
        def command(action,rid):
            row=c.store.rows()[0]
            return c.store.request(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id=rid,action=action)
        show=command("show","request-show")
        await execute_live_command(c.holds,c.submission,show)
        assert c.page.focus_count==1 and c.native==[]
        same=c.store.request(**{k:show[k] for k in ("action","episode_id","revision","execution_id","request_id")})
        assert same["status"]=="completed"
        # Polling delivers queued commands only; retries return the saved receipt.
        assert c.store.commands(show["episode_id"])==()
        c.page.marker=None
        check=command("check","request-check")
        await execute_live_command(c.holds,c.submission,check)
        assert c.store.receipt("request-check")["verification_cleared"] is True
        assert c.store.rows()[0]["retained"] and c.store.rows()[0]["status"]=="expired_held"
        assert not c.submission._submitted
        cancelled=[]
        c.recovery.failure=lambda item,reason: cancelled.append((item,reason))
        close=command("close","request-close")
        with pytest.raises(HumanVerificationRequiredError) as raised:
            await execute_live_command(c.holds,c.submission,close)
        assert raised.value.verification_cancelled
        assert cancelled==[(c.submission.observation_id,"verification_observation_cancelled")]
        assert c.page.closed and c.store.rows()==()
        assert c.store.receipt("request-close")["status"]=="completed"
        assert c.native==[] and await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("change",["revision","execution","generation","target","conversation"])
def test_stale_or_changed_control_never_focuses_or_closes(tmp_path,monkeypatch,change):
    async def run():
        c=case(tmp_path,monkeypatch); row=await c.holds.begin(c.submission,deadline())
        cmd=c.store.request(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="request-1",action="show")
        if change=="revision": c.holds.update(c.submission,status="expired_held")
        elif change=="execution": cmd["execution_id"]="other"
        elif change=="generation": c.attachment.attached_health.browser_session_id="restarted"
        elif change=="target":
            async def observed(*a,**kw): return replace(c.identity,browser_target_reference="cdp-target:other")
            monkeypatch.setattr(chat,"strict_page_identity",observed)
        else: c.page.url="https://copilot.com/chats/other"
        await execute_live_command(c.holds,c.submission,cmd)
        assert c.store.receipt("request-1")["status"]=="refused"
        assert c.store.rows() and c.page.focus_count==0 and not c.page.closed and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("failure",["audit","publish","live_read"])
def test_registration_failure_cannot_return_target_to_normal_cleanup(tmp_path,monkeypatch,failure):
    async def run():
        c=case(tmp_path,monkeypatch)
        def fail(*a,**kw): raise OSError("synthetic disk error")
        if failure=="audit": c.recovery.record_resource=fail
        elif failure=="publish":
            from visibility_runner.browser import profile_access
            monkeypatch.setattr(profile_access,"publish_json",fail)
        else:
            async def no_identity(*a,**kw): raise OSError("synthetic identity error")
            monkeypatch.setattr(chat,"strict_page_identity",no_identity)
        with pytest.raises(OSError): await c.holds.begin(c.submission,deadline())
        report=await c.registry.close_all()
        assert not c.page.closed
        assert (report.unconfirmed_tab_references if failure=="publish" else report.retained_tab_references)==("synthetic-tab",)
        if failure!="publish":
            reopened=VerificationHoldStore(c.manager,(c.profile,),run_id="synthetic-run",plan_sha256="ab"*32)
            assert reopened.rows()[0]["status"]=="intent_pending"
            assert reopened.public()[0]["actions"]==[]
        assert await c.queue.aclose()
    asyncio.run(run())


def test_release_audit_failure_and_corrupt_marker_fail_closed(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch); row=await c.holds.begin(c.submission,deadline())
        def fail(*a,**kw): raise OSError("synthetic audit failure")
        with pytest.raises(OSError):
            c.store.transition(row["episode_id"],owner=c.owner,audit=fail,changes={"retained":False,"status":"closed"})
        assert c.store.rows()[0]["retained"]
        path=c.store.path(c.profile.id); path.write_text('{')
        with pytest.raises(ProfileVerificationHeld): c.store.rows()
        assert not (await c.registry.close_all()).complete and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("malformed",["deadline","identity","command"])
def test_hash_valid_but_malformed_guard_is_rejected(tmp_path,monkeypatch,malformed):
    async def run():
        c=case(tmp_path,monkeypatch); row=await c.holds.begin(c.submission,deadline())
        path=c.store.path(c.profile.id); body=json.loads(path.read_text()); row=body["episodes"][row["episode_id"]]
        if malformed=="deadline": row["deadline_monotonic"]="invalid"
        elif malformed=="identity": row["identity"]["chrome_directory"]="other"
        else: body["commands"]["bad"]={"status":"queued"}
        body.pop("state_sha256"); body["state_sha256"]=_verification_digest(body); path.write_text(json.dumps(body))
        with pytest.raises(ProfileVerificationHeld): read_verification_marker(path)
        assert await c.queue.aclose()
    asyncio.run(run())


def test_pair_capacity_and_command_id_conflict(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch); row=await c.holds.begin(c.submission,deadline())
        c.store.begin({**row,"episode_id":"second","identity":{**row["identity"],"tab_reference":"second-tab","browser_target_reference":"cdp-target:second-target"}},owner=c.owner,audit=c.record)
        with pytest.raises(IntegrityError): c.store.begin({**row,"episode_id":"third"},owner=c.owner,audit=c.record)
        args=dict(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="request-id",action="check")
        assert c.store.request(**args)==c.store.request(**args)
        with pytest.raises(StateTransitionError): c.store.request(**{**args,"action":"close"})
        assert c.store.scope_held("synthetic",item_id="new-observation")
        assert not c.store.scope_held("synthetic",item_id=c.submission.observation_id)
        c.store.transition(row["episode_id"],owner=c.owner,audit=c.record,changes={"retained":False,"status":"closed"})
        assert c.store.scope_held("synthetic") and len(c.store.rows())==1
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("inventories,absent", [
    ([{"targetInfos": []},{"targetInfos": []}],True),
    ([{"targetInfos": [{"targetId":"synthetic-target"}]}],False),
    ([{"targetInfos": []},{"targetInfos": [{"targetId":"synthetic-target"}]}],False),
    ([{}],None),([{"targetInfos":[{}]}],None),
])
def test_absence_needs_two_complete_current_generation_inventories(tmp_path,monkeypatch,inventories,absent):
    async def run():
        c=case(tmp_path,monkeypatch); row=await c.holds.begin(c.submission,deadline()); reads=[]
        async def send(method): reads.append(method); return inventories[len(reads)-1]
        async def detach(): reads.append("detach")
        async def new_session(): return SimpleNamespace(send=send,detach=detach)
        c.attachment.browser=SimpleNamespace(new_browser_cdp_session=new_session)
        if absent is None:
            with pytest.raises(StateTransitionError): await _target_absent(c.attachment,row,deadline())
        else: assert await _target_absent(c.attachment,row,deadline()) is absent
        assert reads[-1]=="detach" and c.store.rows()
        assert await c.queue.aclose()
    asyncio.run(run())


def test_public_live_controls_spool_and_unknown_owner_never_attaches(tmp_path,monkeypatch):
    from visibility_runner.browser import verification_controls as controls
    c=case(tmp_path,monkeypatch)
    row=asyncio.run(c.holds.begin(c.submission,deadline()))
    monkeypatch.setattr(controls,"store_for_runtime",lambda *a:c.store)
    calls=[]
    runner=SimpleNamespace(runtime=None,repository=SimpleNamespace(read_plan=lambda _: {"plan_sha256":"ab"*32,"packets":[{"platform":"copilot","profile_id":"synthetic","observation_ids":["synthetic-observation"]}]}),
        ownership=lambda _: {"state":"owned","execution_id":c.owner.execution_id},
        execution=lambda _: calls.append("unexpected execution"))
    args=dict(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="public-request",action="show")
    assert controls.control(runner,"synthetic-run",**args)["status"]=="queued"
    assert controls.control(runner,"synthetic-run",**args)["status"]=="queued"
    assert c.store.commands(row["episode_id"])== (c.store.receipt("public-request"),)
    runner.ownership=lambda _: {"state":"unknown"}
    with pytest.raises(StateTransitionError): controls.control(runner,"synthetic-run",**args)
    runner.ownership=lambda _: {"state":"owned","execution_id":"replacement"}
    with pytest.raises(StateTransitionError): controls.control(runner,"synthetic-run",**args)
    assert calls==[] and not c.page.closed and c.page.focus_count==0
    asyncio.run(c.queue.aclose())


def test_frozen_hold_policy_is_additive_and_rejects_mode_changes(tmp_path):
    from support.execution import _runtime
    from visibility_runner.config import BrowserConfig, RuntimeConfig
    from visibility_runner.recovery_adapters.execution_policy import make_policy, read_policy
    runtime,_=_runtime(tmp_path)
    old=make_policy(runtime,{"plan_sha256":"ab"*32},source="new_plan")
    assert "copilot_verification" not in old["settings"]
    assert "copilot_verification_policy" not in runtime.browser.to_mapping()
    held=replace(runtime,browser=replace(runtime.browser,copilot_verification_policy="manual_hold_v1"))
    new=make_policy(held,{"plan_sha256":"ab"*32},source="new_plan")
    assert new["settings"]["copilot_verification"]=={"policy":"manual_hold_v1","background_only":True,"guard_namespace":"default-chrome-user-data","lease_lock_root":None}
    assert RuntimeConfig.from_mapping(held.to_mapping()).to_mapping()==held.to_mapping()
    (tmp_path/"execution-policy.json").write_text(json.dumps(new))
    repository=SimpleNamespace(run_dir=lambda _:tmp_path)
    assert read_policy(repository,"run",{"plan_sha256":"ab"*32},runtime=held)==new
    with pytest.raises(StateTransitionError): read_policy(repository,"run",{"plan_sha256":"ab"*32},runtime=runtime)
    for changed in [replace(held,browser=replace(held.browser,user_data_dir=tmp_path/"changed")),
                    replace(held,profiles=replace(held.profiles,lease_lock_root=tmp_path/"changed"))]:
        with pytest.raises(StateTransitionError):read_policy(repository,"run",{"plan_sha256":"ab"*32},runtime=changed)
    (tmp_path/"execution-policy.json").unlink()
    with pytest.raises(StateTransitionError): read_policy(repository,"run",{"plan_sha256":"ab"*32},runtime=held)


@pytest.mark.parametrize("action",["show","check","close"])
def test_public_stopped_controls_reacquire_without_new_tabs_or_sends(tmp_path,monkeypatch,action):
    from support.execution import _runtime,_typed_plan
    from visibility_runner.config import Platform
    from visibility_runner.api import Runner
    from visibility_runner.browser.verification_controls import store_for_runtime
    from visibility_runner.recovery.api import RecoveryLedger,RecoveryCoordinator
    c=case(tmp_path,monkeypatch,profile_id="profile_a",directory="Default")
    runtime,digest=_runtime(tmp_path)
    runtime=replace(runtime,profiles=replace(runtime.profiles,lease_lock_root=c.manager.lock_root),
        browser=replace(runtime.browser,user_data_dir=tmp_path/"synthetic-user-data",copilot_verification_policy="manual_hold_v1"))
    plan=_typed_plan(Platform.COPILOT,run_id="stopped-hold",manifest_sha256=digest)
    attaches=[]
    class FakeRuntime:
        def __init__(self,actual):
            assert actual.browser.background_only and actual.browser.launch_policy.value=="attach_only"
        async def attach(self):
            attaches.append("attach")
            return c.attachment
        async def close(self):
            return await c.registry.close_all()
    runner=Runner(runtime,browser_runtime_factory=FakeRuntime)
    c.submission.observation_id=plan.observation_ids[0]
    runner.create(plan)
    c.store=store_for_runtime(runtime,plan.run_id,plan.plan_sha256)
    c.manager=c.store.manager
    c.attachment.lease_manager=c.manager
    c.attachment.manifest=SimpleNamespace(profile=lambda _:c.profile)
    async def restore(platform,profile_id,item,fresh,identity):
        assert (platform,profile_id,fresh)==("copilot","profile_a",False)
        assert identity.tab_reference=="synthetic-tab"
        return c.page
    c.attachment.provider_page_factory=restore
    ledger=RecoveryLedger(runner.repository.run_dir(plan.run_id),watch_changes=False)
    coordinator=RecoveryCoordinator(ledger)
    c.recovery.coordinator=coordinator; c.recovery.record_resource=coordinator.record_resource
    with runner.execution(plan.run_id) as owner:
        c.recovery.owner=owner
        c.holds=CopilotVerificationHolds(c.store,c.attachment,c.recovery,c.queue,cancelled=lambda:False)
        row=asyncio.run(c.holds.begin(c.submission,deadline()))
        c.holds.update(c.submission,status="expired_held",deadline_monotonic=time.monotonic()-1)
    ledger.close()
    row=c.store.rows()[0]
    args=dict(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="stopped-request",action=action)
    result=runner.verification_control(plan.run_id,**args)
    assert result["status"]=="completed",result
    assert runner.verification_control(plan.run_id,**args)==result
    assert attaches==["attach"] and c.native==[] and not c.submission._submitted
    assert c.page.focus_count==(1 if action=="show" else 0)
    assert c.page.closed==(action=="close")
    assert bool(c.store.rows())==(action!="close")
    asyncio.run(c.queue.aclose())


@pytest.mark.parametrize("changed",[False,True])
def test_prepared_composer_rechecked_before_resuming(tmp_path,monkeypatch,changed):
    async def run():
        c=case(tmp_path,monkeypatch)
        c.submission._prepared=True;c.submission._prompt="synthetic pending prompt";c.submission._composer=object()
        async def composer(*a,**kw): return "operator-edited prompt" if changed else "synthetic pending prompt"
        monkeypatch.setattr(chat,"read_copilot_composer_value",composer)
        async def clear():
            while not c.store.rows() or c.store.rows()[0]["status"]=="intent_pending": await asyncio.sleep(.001)
            c.page.marker=None
        task=asyncio.create_task(clear())
        if changed:
            with pytest.raises(chat.BrowserInteractionError): await c.submission._resolve_human_verification(deadline())
            assert c.store.rows()[0]["phase"]=="pre_send"
        else:
            await c.submission._resolve_human_verification(deadline());assert c.store.rows()==()
        await task
        assert not c.submission._submitted and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())


def test_admitted_turn_two_keeps_certainty_and_original_deadline(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch);c.submission._submitted=True
        def events(kind,**kw):
            return [{"payload":{"pass_id":c.recovery.pass_id,"attempt_id":"attempt-original","operation_id":"synthetic-observation:same_chat_turn_2"}}] if kind=="attempt_started" else [{"payload":{"effect_id":"effect-uncertain"}}]
        c.ledger.select=events
        row=await c.holds.begin(c.submission,deadline())
        assert row["turn"]==2 and row["phase"]=="response" and row["dispatch_state"]=="admitted_or_unknown"
        c.holds.update(c.submission,status="expired_held")
        row=c.store.rows()[0]
        cmd=c.store.request(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="read-only",action="check")
        c.page.marker=None
        await execute_live_command(c.holds,c.submission,cmd)
        after=c.store.rows()[0]
        assert after["deadline_monotonic"]==row["deadline_monotonic"] and after["attempt_id"]=="attempt-original"
        assert after["retained"] and after["dispatch_state"]=="admitted_or_unknown"
        assert all(args[0].startswith("tab:") for args,kwargs in c.audit)
        assert await c.queue.aclose()
    asyncio.run(run())


def test_late_owner_cannot_mutate_episode_or_receipt(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch);row=await c.holds.begin(c.submission,deadline())
        cmd=c.store.request(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="old-owner",action="check")
        before=c.store.path(c.profile.id).read_bytes()
        def fenced(): raise StateTransitionError("owner replaced")
        c.owner.assert_owned=fenced
        with pytest.raises(StateTransitionError): c.store.transition(row["episode_id"],owner=c.owner,audit=c.record,changes={"retained":False,"status":"closed"})
        with pytest.raises(StateTransitionError): c.store.finish_command(cmd,owner=c.owner,status="completed")
        assert c.store.path(c.profile.id).read_bytes()==before
        assert await c.queue.aclose()
    asyncio.run(run())


def test_marker_lock_wait_is_bounded_by_original_deadline(tmp_path,monkeypatch):
    from visibility_runner.browser import profile_access
    async def run():
        c=case(tmp_path,monkeypatch)
        lock=profile_access._profile_website_lock(str(c.store.path(c.profile.id))+".state")
        lock.acquire()
        start=time.monotonic()
        try:
            with pytest.raises(ProfileVerificationHeld): await c.holds.begin(c.submission,deadline(.03))
        finally: lock.release()
        assert time.monotonic()-start<.2
        assert not (await c.registry.close_all()).complete and not c.page.closed
        assert c.native==[] and await c.queue.aclose()
    asyncio.run(run())


def test_admitted_unconfirmed_dispatch_cannot_resume_pending_send(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        c.ledger.select=lambda kind,**kw: [{"payload":{"pass_id":c.recovery.pass_id,"attempt_id":"admitted-attempt","operation_id":"synthetic-observation:measurement"}}] if kind=="attempt_started" else [{"payload":{"effect_id":"ambiguous-effect"}}]
        await c.holds.begin(c.submission,deadline())
        with pytest.raises(HumanVerificationRequiredError,match="requires_reconciliation"):
            await c.holds.validated(c.submission,deadline())
        assert c.store.rows()[0]["retained"] and not c.submission._submitted
        assert c.native==[] and await c.queue.aclose()
    asyncio.run(run())


def test_recovery_preview_uses_guard_when_resource_audit_is_missing(tmp_path,monkeypatch):
    from support.execution import _runtime,_typed_plan
    from visibility_runner.config import Platform
    from visibility_runner.api import Runner
    from visibility_runner.browser.verification_controls import store_for_runtime
    c=case(tmp_path,monkeypatch,profile_id="profile_a",directory="Default")
    runtime,digest=_runtime(tmp_path)
    runtime=replace(runtime,profiles=replace(runtime.profiles,lease_lock_root=c.manager.lock_root),
        browser=replace(runtime.browser,user_data_dir=tmp_path/"synthetic-user-data",copilot_verification_policy="manual_hold_v1"))
    from visibility_runner.config import MeasurementConfig
    runner=Runner(runtime)
    plan=runner.plan(MeasurementConfig(scope_kind="diagnostic",platforms=(Platform.COPILOT,),diagnostic_prompts=("Synthetic prompt",)),run_id="audit-gap")
    c.submission.observation_id=plan.observation_ids[0]
    runner.create(plan)
    c.store=store_for_runtime(runtime,plan.run_id,plan.plan_sha256)
    c.holds=CopilotVerificationHolds(c.store,c.attachment,c.recovery,c.queue,cancelled=lambda:False)
    def fail(*a,**kw): raise OSError("synthetic audit gap")
    c.recovery.record_resource=fail
    with pytest.raises(OSError): asyncio.run(c.holds.begin(c.submission,deadline()))
    preview=runner.plan_refetch(plan.run_id,request_id="preview-only")
    assert all(s["action"]=="blocked" for s in preview["selections"])
    readout=runner.failure_readout(plan.run_id)
    assert readout["verification"][0]["actions"]==[]
    assert all(i["status"]=="blocked" for i in readout["items"])
    assert not c.page.closed and c.native==[]
    asyncio.run(c.queue.aclose())


def test_new_observation_is_fenced_before_attempt_or_browser_admission(tmp_path,monkeypatch):
    from visibility_runner.recovery_adapters.visibility import VisibilityExecution
    async def run():
        c=case(tmp_path,monkeypatch);await c.holds.begin(c.submission,deadline())
        calls=[]
        def transaction(*a,**kw): calls.append("attempt-admission");raise AssertionError("must be fenced first")
        context=SimpleNamespace(verification_holds=c.holds,operations={"new-item:measurement_turn_1":SimpleNamespace(resource_scopes=("synthetic:copilot",))},
            coordinator=SimpleNamespace(ledger=SimpleNamespace(transaction=transaction)))
        with pytest.raises(HumanVerificationRequiredError):
            VisibilityExecution.before_prepare(context,{"observation_id":"new-item","action":"measurement_turn_1"})
        assert calls==[] and c.native==[]
        refused=[]
        context.snapshot=SimpleNamespace(manifest=SimpleNamespace(operations=(SimpleNamespace(item_id="new-item",resource_scopes=("synthetic:copilot",)),)))
        context.failure=lambda *a:refused.append(a)
        assert await VisibilityExecution.prepare_item(context,None,"new-item",{}, {}) is False
        assert refused==[("new-item","copilot_verification_required")]
        assert await c.queue.aclose()
    asyncio.run(run())


def test_cleanup_closes_ordinary_tab_while_preserving_intentional_hold(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch);await c.holds.begin(c.submission,deadline())
        other=SimpleNamespace(closed=False)
        async def close():other.closed=True
        other.close=close;other.is_closed=lambda:other.closed
        identity=TabSessionIdentity(profile_id="synthetic",chrome_directory="Synthetic Profile",provider="chatgpt",
            browser_context_reference="other-context",tab_reference="ordinary-tab",browser_target_reference="cdp-target:ordinary-target")
        c.registry.add(OwnedTab(other,identity,"about:blank"))
        report=await c.registry.close_all()
        assert report.complete and report.retained_tab_references==("synthetic-tab",)
        assert report.closed_tab_references==("ordinary-tab",) and report.unconfirmed_tab_references==()
        assert other.closed and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("native_result",["clicked","unsupported","exception"])
@pytest.mark.parametrize("automatic", [False, True])
def test_opt_in_native_hold_uses_one_guarded_attempt_and_retains_failure(tmp_path,monkeypatch,native_result,automatic):
    async def run():
        c=case(tmp_path,monkeypatch);await c.queue.aclose()
        attempts=[]
        def attempt(**kw):
            assert kw["expected_pid"]==4321 and kw["foreground_check"](deadline=kw["deadline"])
            attempts.append(kw)
            if native_result=="exception":raise OSError("ambiguous synthetic native exception")
            return {"status":native_result,"click_attempted":False if native_result=="unsupported" else True}
        native=SimpleNamespace(adapter=SimpleNamespace(attempt=attempt),cancel=lambda:None,close=lambda:None)
        queue=DesktopActionQueue(lambda:native,lock_path=tmp_path/"native-synthetic.lock")
        c.holds.actions=queue
        c.attachment.config.browser.background_only=automatic
        async def evaluate(expression):return True
        c.page.evaluate=evaluate
        async def send(method):assert method=="SystemInfo.getProcessInfo";return {"processInfo":[{"type":"browser","id":4321}]}
        async def detach():pass
        async def new_session():return SimpleNamespace(send=send,detach=detach)
        service=CopilotVerificationService(queue,SimpleNamespace(new_browser_cdp_session=new_session),
            background_only=automatic, automatic_verification=automatic, holds=c.holds)
        c.submission.human_verification_handler=service
        with pytest.raises(HumanVerificationRequiredError):await c.submission._resolve_human_verification(deadline(.15))
        assert len(attempts)==1 and c.page.focus_count==1
        assert c.store.rows()[0]["retained"] and not c.page.closed
        assert c.store.rows()[0]["status"]==("expired_held" if native_result=="clicked" else "unsupported_held")
        assert await queue.aclose()
    asyncio.run(run())


def test_explicit_close_waits_for_active_desktop_action_before_tab_closure(tmp_path,monkeypatch):
    import threading
    async def run():
        c=case(tmp_path,monkeypatch);await c.queue.aclose()
        events=[];started=threading.Event();release=threading.Event()
        queue=DesktopActionQueue(lambda:SimpleNamespace(close=lambda:None),lock_path=tmp_path/"synthetic-drain.lock")
        c.holds.actions=queue
        await c.holds.begin(c.submission,deadline())
        def action(_):
            events.append("input-start");started.set();assert release.wait(1);events.append("input-settled")
        active=asyncio.create_task(queue.execute(key="prior-native",action=action,timeout_s=1))
        assert await asyncio.to_thread(started.wait,.5)
        old_close=c.page.close
        async def close():events.append("tab-close");await old_close()
        c.page.close=close
        row=c.store.rows()[0]
        cmd=c.store.request(episode_id=row["episode_id"],revision=row["revision"],execution_id=row["execution_id"],request_id="close-after-drain",action="close")
        closing=asyncio.create_task(execute_live_command(c.holds,c.submission,cmd))
        await asyncio.sleep(.03)
        assert not c.page.closed and not closing.done()
        release.set();await active
        with pytest.raises(HumanVerificationRequiredError):await closing
        assert events==["input-start","input-settled","tab-close"]
        assert c.page.closed and c.store.rows()==()
        assert await queue.aclose()
    asyncio.run(run())


def test_target_proof_wait_consumes_only_original_deadline(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        async def blocked(*a,**kw):await asyncio.Event().wait()
        monkeypatch.setattr(profile_matching,"browser_target_id",blocked)
        start=time.monotonic()
        with pytest.raises(TimeoutError):await c.holds.begin(c.submission,deadline(.03))
        assert time.monotonic()-start<.2
        report=await c.registry.close_all()
        assert report.retained_tab_references==("synthetic-tab",) and not c.page.closed and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())


def test_default_hold_location_survives_temporary_lock_directory_loss(tmp_path,monkeypatch):
    from visibility_runner.browser import profile_access as access
    import shutil
    monkeypatch.setattr(access,"DEFAULT_PROFILE_LEASE_ROOT",tmp_path/"temporary-locks")
    monkeypatch.setattr(access,"DEFAULT_VERIFICATION_HOLD_ROOT",tmp_path/"persistent-holds")
    c=case(tmp_path,monkeypatch)
    manager=ProfileReservationManager(access.DEFAULT_PROFILE_LEASE_ROOT,namespace="synthetic-user-data")
    store=VerificationHoldStore(manager,(c.profile,),run_id="synthetic-run",plan_sha256="ab"*32)
    c.holds.store=store
    row=asyncio.run(c.holds.begin(c.submission,deadline()))
    assert store.path(c.profile.id).parent==access.DEFAULT_VERIFICATION_HOLD_ROOT
    manager.lock_root.mkdir(parents=True,exist_ok=True)
    shutil.rmtree(manager.lock_root)
    restarted=VerificationHoldStore(manager,(c.profile,),run_id="synthetic-run",plan_sha256="ab"*32)
    assert restarted.rows()[0]["episode_id"]==row["episode_id"]
    with pytest.raises(ProfileVerificationHeld):manager.acquire((c.profile,),owner="replacement",provider="copilot",timeout_seconds=.1)
    asyncio.run(c.queue.aclose())


@pytest.mark.parametrize("submitted",[False,True])
def test_changed_conversation_at_detection_never_becomes_actionable(tmp_path,monkeypatch,submitted):
    async def run():
        c=case(tmp_path,monkeypatch);c.submission._submitted=submitted;c.page.url="https://copilot.com/chats/other"
        with pytest.raises((chat.IdentityMismatchError,IntegrityError)):await c.holds.begin(c.submission,deadline())
        assert c.store.rows()[0]["identity"]["conversation_reference"]==c.identity.conversation_reference
        assert c.store.public()[0]["actions"]==[]
        assert (await c.registry.close_all()).retained_tab_references==("synthetic-tab",) and not c.page.closed
        assert await c.queue.aclose()
    asyncio.run(run())


def test_first_submitted_start_to_conversation_transition_uses_existing_gate(tmp_path,monkeypatch):
    async def run():
        c=case(tmp_path,monkeypatch)
        c.submission._identity=replace(c.identity,conversation_reference=COPILOT_SPEC.start_url)
        c.submission._submitted=True
        row=await c.holds.begin(c.submission,deadline())
        assert row["identity"]["conversation_reference"]==c.identity.conversation_reference
        assert row["status"]=="manual_required" and c.page.focus_count==0 and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("rehold_fails",[False,True])
def test_expiry_during_release_cannot_close_target_if_rehold_fails(tmp_path,monkeypatch,rehold_fails):
    async def run():
        c=case(tmp_path,monkeypatch);await c.holds.begin(c.submission,deadline())
        clock=[time.monotonic()]
        monkeypatch.setattr(time,"monotonic",lambda:clock[0])
        original=deadline();transition=c.store.transition
        def late(episode,**kw):
            if kw["changes"]["status"]=="resumed":
                result=transition(episode,**kw);clock[0]+=2;return result
            if rehold_fails:raise OSError("synthetic re-hold publication failed")
            return transition(episode,**kw)
        monkeypatch.setattr(c.store,"transition",late)
        with pytest.raises(OSError if rehold_fails else TimeoutError):await c.holds.validated(c.submission,original)
        report=await c.registry.close_all()
        assert not c.page.closed
        assert (report.unconfirmed_tab_references if rehold_fails else report.retained_tab_references)==("synthetic-tab",)
        assert not c.submission._submitted and c.native==[]
        assert await c.queue.aclose()
    asyncio.run(run())
