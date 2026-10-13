"""Explicit verification controls on an existing exact target; no sends."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from ..errors import HumanVerificationRequiredError, IdentityMismatchError, StateTransitionError
from .verification_holds import VerificationHoldStore


def store_for_runtime(runtime, run_id, plan_sha256):
    from .profile_access import ProfileReservationManager, DEFAULT_PROFILE_LEASE_ROOT
    manager = ProfileReservationManager(runtime.profiles.lease_lock_root or DEFAULT_PROFILE_LEASE_ROOT,
        namespace=str(runtime.browser.user_data_dir or "default-chrome-user-data"))
    return VerificationHoldStore(manager, runtime.selected_profiles,
        run_id=run_id, plan_sha256=plan_sha256, capacity=runtime.tabs_per_provider)


class _ControlComplete(Exception):
    pass


async def _exact_page(attachment, page, row, deadline, *, conversation=True):
    from ..websites.chat_actions import (
        BrowserIdentity, strict_page_identity, _canonical_reference, _is_blank_provider_start_url,
        _is_authentication_url,
    )
    from ..websites.copilot import COPILOT_SPEC
    if attachment.attached_health.browser_session_id != row["browser_session_id"]:
        raise IdentityMismatchError("verification_browser_generation_changed")
    observed = await strict_page_identity(page, profile_id=row["profile_id"],
        chrome_directory=row["identity"]["chrome_directory"], _deadline=deadline)
    expected = BrowserIdentity.from_mapping(row["identity"])
    fresh_preparation = (
        conversation and row.get("phase") == "preparation"
        and row.get("dispatch_state") == "not_admitted"
        and _is_blank_provider_start_url(expected.conversation_reference, COPILOT_SPEC.start_url,
            aliases=COPILOT_SPEC.blank_start_url_aliases)
    )
    preparation_sign_in = (
        conversation and row.get("phase") == "preparation"
        and row.get("dispatch_state") == "not_admitted"
        and row.get("blocker_kind") == "sign_in"
        and _is_authentication_url(observed.conversation_reference,
            COPILOT_SPEC.authentication_url_prefixes)
    )
    if fresh_preparation and not _is_blank_provider_start_url(
        observed.conversation_reference, COPILOT_SPEC.start_url,
        aliases=COPILOT_SPEC.blank_start_url_aliases,
    ) and not preparation_sign_in:
        raise IdentityMismatchError("verification_preparation_route_changed")
    expected.assert_same_tab(observed,
        require_same_conversation=conversation and not fresh_preparation and not preparation_sign_in,
        canonical=lambda url: _canonical_reference(url, COPILOT_SPEC))
    return observed


async def _target_absent(attachment, row, deadline):
    """Two complete inventories on the same live CDP generation prove closure.

    An unresolved profile/context lookup is never evidence of target absence.
    """
    from ..websites.chat_actions import _await_call
    if attachment.attached_health.browser_session_id != row["browser_session_id"]:
        raise IdentityMismatchError("verification_browser_generation_changed")
    cdp = await _await_call(attachment.browser.new_browser_cdp_session, _deadline=deadline)
    try:
        target = row["identity"]["browser_target_reference"].removeprefix("cdp-target:")
        for _ in range(2):
            observed = await _await_call(cdp.send, "Target.getTargets", _deadline=deadline)
            infos = observed.get("targetInfos") if isinstance(observed, dict) else None
            if (not isinstance(infos, list) or any(not isinstance(i, dict)
                    or not isinstance(i.get("targetId"), str) or not i["targetId"] for i in infos)):
                raise StateTransitionError("verification_target_inventory_incomplete")
            if any(i["targetId"] == target for i in infos):
                return False
        return True
    finally:
        await _await_call(cdp.detach, _deadline=deadline)


async def execute_live_command(holds, submission, command):
    from ..websites.chat_actions import _BrowserOperationDeadline, _visible_access_block_marker, _await_call
    from ..websites.copilot import COPILOT_SPEC
    if command["status"] != "queued":
        return
    store, owner = holds.store, holds.recovery.owner
    row = store.episode(command["episode_id"])
    owner.assert_owned()
    if (not row["retained"] or row["revision"] != command["revision"]
            or row["execution_id"] != command["execution_id"]):
        store.finish_command(command, owner=owner, status="refused", reason="verification_control_stale")
        return
    deadline = _BrowserOperationDeadline.after(5, timeout_message="verification_control_deadline")
    result = {}
    try:
        if command["action"] == "check":
            await _exact_page(holds.attachment, submission.page, row, deadline)
            marker = await _visible_access_block_marker(submission.page, COPILOT_SPEC, _deadline=deadline)
            result["verification_cleared"] = marker is None
            store.transition(row["episode_id"], owner=owner, audit=holds.recovery.record_resource,
                expected_revision=command["revision"],
                changes={"verification_cleared": marker is None})
        else:
            async def prepare():
                owner.assert_owned()
                current = store.episode(row["episode_id"])
                if current["revision"] != command["revision"] or not current["retained"]:
                    raise StateTransitionError("verification_control_stale")
                if holds.cancelled():
                    raise StateTransitionError("verification_control_cancelled")
                await _exact_page(holds.attachment, submission.page, row, deadline,
                                  conversation=command["action"] != "close")
                if command["action"] == "show":
                    await _await_call(submission.page.bring_to_front, _deadline=deadline)
                    await _exact_page(holds.attachment, submission.page, row, deadline)
                else:
                    tab = holds.attachment.session.registry.tab(row["identity"]["tab_reference"])
                    if tab.page is not submission.page or not await tab.close(force=True):
                        raise StateTransitionError("verification_close_unconfirmed")
                raise _ControlComplete()
            try:
                await holds.actions.execute(key=("verification-control", command["request_id"]),
                    prepare=prepare, action=lambda _session: None, timeout_s=deadline.remaining())
            except _ControlComplete:
                pass
            if command["action"] == "close":
                store.transition(row["episode_id"], owner=owner, audit=holds.recovery.record_resource,
                    expected_revision=command["revision"], exclude_request_id=command["request_id"],
                    changes={"status": "closed", "retained": False, "reason": "operator_closed",
                             "observation_cancelled": True})
                cancel_observation = getattr(holds.recovery, "failure", None)
                if callable(cancel_observation):
                    cancel_observation(row["observation_id"], "verification_observation_cancelled")
        store.finish_command(command, owner=owner, status="completed", **result)
    except Exception as error:
        # Do not project arbitrary exception text, which could contain page data.
        store.finish_command(command, owner=owner, status="refused",
                             reason="verification_control_" + type(error).__name__)
        return
    if command["action"] == "close":
        error = HumanVerificationRequiredError("verification_observation_cancelled")
        error.verification_cancelled = True
        raise error


def control(runner, run_id, *, episode_id, revision, execution_id, request_id, action, _restart=False):
    """Spool to a live owner, or execute a stopped-owner inspection under locks."""
    import asyncio
    from ..execution.manage_measurements import _require_sync_caller
    from ..recovery.api import RecoveryCoordinator, RecoveryLedger
    _require_sync_caller()
    plan = runner.repository.read_plan(run_id)
    store = store_for_runtime(runner.runtime, run_id, plan["plan_sha256"])
    observed = runner.ownership(run_id)
    if observed["state"] == "unknown":
        raise StateTransitionError("verification_owner_unknown")
    row = store.episode(episode_id)
    if not any(p.get("platform") == "copilot" and p.get("profile_id") == row["profile_id"]
               and row["observation_id"] in p.get("observation_ids", ()) for p in plan.get("packets", ())):
        raise StateTransitionError("verification_observation_binding_changed")
    if observed["state"] == "owned" and observed.get("execution_id") != row["execution_id"]:
        current = runner._owners.get(run_id) if _restart else None
        if not (_restart and current is not None and current.execution_id == observed.get("execution_id")):
            raise StateTransitionError("verification_owner_changed")
    command = store.request(episode_id=episode_id, revision=revision, execution_id=execution_id,
                            request_id=request_id, action=action)
    if command["status"] != "queued" or (observed["state"] == "owned" and not _restart):
        return command
    with runner.execution(run_id) as owner:
        # A new executor epoch may inspect/dispose the held resource only. It
        # does not change runner state, attempt ownership, or measurement policy.
        async def perform():
            from .runtime import BrowserRuntime
            from .desktop_actions import DesktopActionQueue
            from ..websites.chat_actions import BrowserIdentity, _await_call, _BrowserOperationDeadline
            from .verification_holds import CopilotVerificationHolds
            ledger = RecoveryLedger(runner.repository.run_dir(run_id), watch_changes=False)
            coordinator = RecoveryCoordinator(ledger)
            runtime = replace(runner.runtime, browser=replace(runner.runtime.browser,
                background_only=True, launch_policy="attach_only"))
            factory = runner.browser_runtime_factory or BrowserRuntime
            browser_runtime = factory(runtime)
            attachment = None
            lease = None
            def no_native():
                raise AssertionError("verification controls must never initialize native mouse")
            actions = DesktopActionQueue(session_factory=no_native)
            try:
                await actions.__aenter__()
                row = store.episode(episode_id)
                references = tuple("tab:" + r["identity"]["tab_reference"]
                    for r in store.rows(own_run=False) if r["profile_id"] == row["profile_id"])
                profile = store.profiles[row["profile_id"]]
                lease = await asyncio.to_thread(store.manager.acquire_verification,
                    profile, owner="visibility-runner:" + run_id + ":verification-control",
                    resource_references=references, timeout_seconds=5)
                attachment = await browser_runtime.attach()
                attachment.session.registry.retention_predicate = store.retained
                attached_profile = attachment.manifest.profile(row["profile_id"])
                if (attached_profile.chrome_directory != profile.chrome_directory
                        or attachment.lease_manager.namespace != store.manager.namespace
                        or attachment.lease_manager.lock_root != store.manager.lock_root):
                    raise IdentityMismatchError("verification_profile_binding_changed")
                if attachment.attached_health.browser_session_id != row["browser_session_id"]:
                    raise IdentityMismatchError("verification_browser_generation_changed")
                deadline = _BrowserOperationDeadline.after(5, timeout_message="verification_control_deadline")
                page = await _await_call(attachment.provider_page_factory, "copilot", row["profile_id"],
                    row["observation_id"], False, BrowserIdentity.from_mapping(row["identity"]), _deadline=deadline)
                if page is None:
                    if not await _target_absent(attachment, row, deadline):
                        raise StateTransitionError("verification_target_unresolved")
                    # A complete inventory, not a failed lookup, proved closure.
                    store.transition(episode_id, owner=owner, audit=coordinator.record_resource,
                        changes={"status": "closed", "retained": False, "reason": "target_absent"},
                        exclude_request_id=request_id)
                    return store.finish_command(command, owner=owner, status="completed", reason="target_absent")
                recovery = SimpleNamespace(owner=owner, record_resource=coordinator.record_resource)
                holds = SimpleNamespace(store=store, attachment=attachment, recovery=recovery,
                                        actions=actions, cancelled=lambda: False)
                submission = SimpleNamespace(page=page)
                try:
                    await execute_live_command(holds, submission, command)
                except HumanVerificationRequiredError:
                    pass  # A stopped-owner close has no live observation to cancel.
                return store.receipt(request_id)
            except Exception as error:
                return store.finish_command(command, owner=owner, status="refused",
                    reason="verification_control_" + type(error).__name__)
            finally:
                settled = await actions.aclose(timeout_s=runner.runtime.cleanup_timeout_seconds)
                if attachment is not None and settled:
                    await browser_runtime.close()
                elif attachment is not None and lease is not None:
                    attachment.lease_manager.quarantine((profile,),
                        owner="visibility-runner:" + run_id + ":verification-control",
                        provider="copilot", detail="verification_control_cleanup_unconfirmed",
                        resource_references=references)
                if lease is not None:
                    lease.release()
                ledger.close()
        return asyncio.run(perform())
