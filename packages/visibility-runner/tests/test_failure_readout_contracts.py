"""Public failure projection keeps secrets out and binds links to the run."""
from __future__ import annotations
from dataclasses import replace
import json
import pytest
from support.execution import _runtime,_typed_plan,FakeDriver,AnswerSpec
from support.tree_snapshot import tree_snapshot
from visibility_runner.api import Runner,Platform,UnknownArtifactReferenceError,IntegrityError
from visibility_runner.errors import ProviderAccessBlockedError
from visibility_runner.websites.usage_limits import project_blocking_usage_limit, project_usage_limit_issue

def example(root,run_id="readout",platform=Platform.CHATGPT):
    root.mkdir(parents=True,exist_ok=True)
    runtime,digest=_runtime(root)
    runtime=replace(runtime, profiles=replace(runtime.profiles, lease_lock_root=root / "profile-locks"),
                    browser=replace(runtime.browser, user_data_dir=root / "synthetic-user-data"))
    plan=_typed_plan(platform,run_id=run_id,manifest_sha256=digest)
    driver=FakeDriver(platform.value,{plan.observation_ids[0]:(AnswerSpec("Alpha Clinic"),AnswerSpec('["Alpha Clinic"]'))})
    return Runner(runtime,providers={platform.value:driver}),plan,driver

def test_raw_adapter_exception_is_not_exposed_by_failure_projection(tmp_path):
    runner,plan,driver=example(tmp_path)
    secret="PROMPT_SENTINEL token-unique /absolute/private/secret"
    async def denied(**kwargs): raise RuntimeError(secret)
    driver.open=denied
    runner.run(plan)
    before=tree_snapshot(runner.repository.run_dir(plan.run_id))
    value=runner.failure_readout(plan.run_id)
    serialized=json.dumps(value)
    assert secret not in serialized and "token-unique" not in serialized
    assert value["items"][0]["reason_code"]
    assert not driver.sent
    assert tree_snapshot(runner.repository.run_dir(plan.run_id))==before


def test_pre_send_auth_failure_is_failed_with_causal_reason_and_remains_refetchable(tmp_path):
    runner, plan, driver = example(tmp_path,platform=Platform.GEMINI)
    async def denied(**_kwargs):
        raise ProviderAccessBlockedError("PRIVATE_NOTICE", reason="authentication_required")
    driver.open = denied
    runner.run(plan)
    before = tree_snapshot(runner.repository.run_dir(plan.run_id))
    row = runner.failure_readout(plan.run_id)["items"][0]
    assert row["status"] == "failed" and row["reason_code"] == "authentication_required"
    assert runner.plan_refetch(plan.run_id, request_id="after-sign-in")["selections"][0]["action"] == "execute"
    assert not driver.sent and tree_snapshot(runner.repository.run_dir(plan.run_id)) == before

@pytest.mark.parametrize("scope,text_allowed", [("text", False), ("unknown", None)])
def test_usage_limit_readout_preserves_valid_clock_without_notice_or_countdown(tmp_path, scope, text_allowed):
    runner, plan, driver = example(tmp_path)
    limit = {"scope": scope, "text_allowed": text_allowed, "blocking": True,
        "reset_day": "tomorrow", "reset_clock": "02:00", "reset_display": "tomorrow at 2:00\u202fAM",
        "reset_timezone": None, "notice_text": "PRIVATE_NOTICE token-sentinel",
        "reset_at": "forged-date", "remaining_seconds": 123, "credential": "secret-token"}
    async def denied(**kwargs):
        raise ProviderAccessBlockedError("PRIVATE_ERROR token-sentinel", reason="usage_limit", usage_limit=limit)
    driver.open = denied
    runner.run(plan)
    before = tree_snapshot(runner.repository.run_dir(plan.run_id))
    readout = runner.failure_readout(plan.run_id)
    item = readout["items"][0]
    assert item["limit_reason"] == "usage_limit"
    assert item["usage_limit"] == {key: limit[key] for key in (
        "scope", "text_allowed", "blocking", "reset_day", "reset_clock", "reset_display", "reset_timezone")}
    serialized = json.dumps(readout)
    for private in ("PRIVATE_", "token-sentinel", "secret-token", "forged-date", "remaining_seconds", "reset_at"):
        assert private not in serialized
    assert not driver.sent
    assert tree_snapshot(runner.repository.run_dir(plan.run_id)) == before

@pytest.mark.parametrize("display,clock,day", [
    ("13pm", "13:00", None), ("tomorrow at 2am", "01:00", "tomorrow"),
    ("PRIVATE_NOTICE token-sentinel", "02:00", None),
])
def test_usage_limit_projection_omits_invalid_or_mismatched_clock(display, clock, day):
    projected = project_blocking_usage_limit({"scope": "text", "blocking": True, "text_allowed": False,
        "reset_display": display, "reset_clock": clock, "reset_day": day, "reset_timezone": "invented"})
    assert projected == {"scope": "text", "blocking": True, "text_allowed": False,
        "reset_display": None, "reset_clock": None, "reset_day": None, "reset_timezone": None}

@pytest.mark.parametrize("limit", [
    {"scope": "tools", "blocking": False, "text_allowed": True},
    {"scope": "unknown", "blocking": False, "text_allowed": True},
])
def test_tool_only_or_text_allowed_notice_is_not_a_public_usage_hold(limit):
    issue = {"code": "needs_attention", "detail": {"user_action_required": True,
        "access_block": {"reason": "usage_limit", "profile_id": "profile-a", "provider": "chatgpt", "usage_limit": limit}}}
    assert project_usage_limit_issue(issue, profile_id="profile-a", provider="chatgpt") is None

def test_recovery_link_registered_only_in_other_run_refuses(tmp_path):
    first,plan_a,driver_a=example(tmp_path/"a","run-a")
    second,plan_b,driver_b=example(tmp_path/"b","run-b")
    # Distinct bytes prevent accidentally constructing an identical legitimate
    # content-addressed reference in both runs.
    driver_b.answers[plan_b.observation_ids[0]]=(AnswerSpec("Beta Clinic"),AnswerSpec('["Beta Clinic"]'))
    result_a=first.run(plan_a);result_b=second.run(plan_b)
    ref_a=result_a.observations[0].artifacts.turn_1
    ref_b=result_b.observations[0].artifacts.turn_1
    assert ref_a.relative_path!=ref_b.relative_path
    before=tree_snapshot(tmp_path)
    resolved = first.resolve_recovery_link(plan_a.run_id, ref_a.relative_path)
    assert resolved["kind"] == "artifact"
    assert resolved["reference"]["path"] == ref_a.relative_path
    assert resolved["reference"]["sha256"] == ref_a.sha256
    assert first.read_artifact(plan_a.run_id, resolved["reference"]) == b"Alpha Clinic"
    assert second.read_artifact(plan_b.run_id,ref_b)==b"Beta Clinic"
    with pytest.raises(UnknownArtifactReferenceError): first.resolve_recovery_link(plan_a.run_id,ref_b.relative_path)
    for route in ("../outside","https://evil.invalid/chat","conversations/unregistered"):
        with pytest.raises(UnknownArtifactReferenceError): first.resolve_recovery_link(plan_a.run_id,route)
    assert tree_snapshot(tmp_path)==before
    assert len(driver_a.sent)==len(driver_b.sent)==2

@pytest.mark.parametrize("method",["inspect","failure_readout","plan_refetch"])
def test_critical_corruption_during_public_read_is_never_repaired(tmp_path,method):
    runner,plan,driver=example(tmp_path)
    runner.run(plan)
    file=runner.repository.run_dir(plan.run_id)/"plan.json"
    file.write_bytes(b"{corrupt authoritative plan")
    before=tree_snapshot(tmp_path)
    with pytest.raises(IntegrityError): getattr(runner,method)(plan.run_id)
    assert tree_snapshot(tmp_path)==before and len(driver.sent)==2


def register_conversation(runner, plan, source_attempt_id, url):
    """Publish a synthetic adapter checkpoint without opening a transport."""
    from visibility_runner.recovery.api import RecoveryLedger
    from visibility_runner.planning.hashing import canonical_sha256
    with runner.execution(plan.run_id) as owner:
        RecoveryLedger(runner.repository.run_dir(plan.run_id)).append("correlation", {
            "item_id": plan.observation_ids[0], "source_attempt_id": source_attempt_id,
            "binding": {"identity": {"conversation_reference": url}},
        }, owner)
    return "conversations/" + canonical_sha256({"attempt": source_attempt_id})


@pytest.mark.parametrize("url,canonical", [
    ("https://chatgpt.com/c/saved-chat?tracking=removed#fragment", "https://chatgpt.com/c/saved-chat"),
    ("https://chatgpt.com:443/g/saved_chat", "https://chatgpt.com/g/saved_chat"),
    ("https://gemini.google.com/app/saved-chat", "https://gemini.google.com/app/saved-chat"),
])
def test_registered_conversation_route_resolves_exact_saved_canonical_url(tmp_path, url, canonical):
    runner, plan, driver = example(tmp_path)
    runner.create(plan)
    route = register_conversation(runner, plan, "saved-attempt", url)
    before = tree_snapshot(tmp_path)
    assert runner.resolve_recovery_link(plan.run_id, route) == {"kind": "conversation", "url": canonical}
    assert route in runner.failure_readout(plan.run_id)["items"][0]["evidence_links"]
    assert tree_snapshot(tmp_path) == before
    assert not driver.opens and not driver.sent


@pytest.mark.parametrize("url", ["https://evil.invalid/c/saved-chat", "http://chatgpt.com/c/saved-chat",
    "https://username:password@chatgpt.com/c/saved-chat", "https://chatgpt.com:444/c/saved-chat",
    "https://chatgpt.com/c/../outside", "https://chatgpt.com/c/saved-chat/extra", "file:///private/saved-chat"])
def test_registered_conversation_refuses_unsafe_canonical_url_without_navigation(tmp_path, url):
    runner, plan, driver = example(tmp_path)
    runner.create(plan)
    route = register_conversation(runner, plan, "unsafe-attempt", url)
    before = tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="conversation_id_missing_or_invalid"):
        runner.resolve_recovery_link(plan.run_id, route)
    assert tree_snapshot(tmp_path) == before
    assert not driver.opens and not driver.sent


def test_conversation_registration_is_run_scoped_with_identical_binding_valid_control(tmp_path):
    first, plan_a, driver_a = example(tmp_path / "a", "run-a")
    second, plan_b, driver_b = example(tmp_path / "b", "run-b")
    first.create(plan_a); second.create(plan_b)
    url = "https://chatgpt.com/c/same-saved-chat"
    route = register_conversation(second, plan_b, "shared-attempt-token", url)
    before = tree_snapshot(tmp_path)
    assert second.resolve_recovery_link(plan_b.run_id, route) == {"kind": "conversation", "url": url}
    with pytest.raises(UnknownArtifactReferenceError): first.resolve_recovery_link(plan_a.run_id, route)
    assert tree_snapshot(tmp_path) == before
    assert register_conversation(first, plan_a, "shared-attempt-token", url) == route
    before = tree_snapshot(tmp_path)
    assert first.resolve_recovery_link(plan_a.run_id, route) == {"kind": "conversation", "url": url}
    assert tree_snapshot(tmp_path) == before
    assert not driver_a.opens and not driver_b.opens and not driver_a.sent and not driver_b.sent


def test_public_preview_nested_mutations_cannot_change_selections_or_allowance(tmp_path):
    runner, plan, driver = example(tmp_path)
    runner.create(plan)
    first = runner.plan_refetch(plan.run_id, request_id="detached-preview")
    expected = json.loads(json.dumps(first, allow_nan=False))
    readout = runner.failure_readout(plan.run_id)
    expected_items = json.loads(json.dumps(readout["items"]))
    before = tree_snapshot(tmp_path)
    first["selections"][0]["max_attempts"] = 999
    first["selections"][0]["operation_ids"].append("foreign.operation")
    first["parser_repairs"].append({"compatibility_id": "forged"})
    readout["preview"]["selections"][0]["max_effects"] = 999
    readout["items"][0]["unresolved_attempt_ids"].append("forged-attempt")
    assert runner.plan_refetch(plan.run_id, request_id="detached-preview") == expected
    assert runner.failure_readout(plan.run_id)["items"] == expected_items
    assert tree_snapshot(tmp_path) == before
    assert not driver.opens and not driver.sent
