"""Execution-local verification opt-in; never rewrite historical plan policy."""
from __future__ import annotations

import json
import re
import threading

from ..errors import IntegrityError, StateTransitionError
from ..planning.hashing import canonical_sha256, json_value
from ..recovery.contracts import safe_id
from ..recovery.durable import publish_json


def enable(runner, run_id, *, request_id, request_sha256, plan=None, account_emails=None):
    from .manage_measurements import _require_sync_caller, require_active_execution
    _require_sync_caller()
    safe_id(request_id)
    if not isinstance(request_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", request_sha256) is None:
        raise ValueError("verification_request_hash_invalid")
    owner = runner._owners.get(run_id)
    if owner is None or runner._execution_thread_id != threading.get_ident():
        raise StateTransitionError("verification_execution_not_owned")
    owner.assert_owned()
    if run_id in runner._active_browser_runtimes:
        raise StateTransitionError("verification_execution_already_active")
    from ..recovery_adapters.execution_policy import read_policy, make_policy
    initial = plan is not None
    if initial:
        runner._validate_plan(plan)
        runner._verify_profile_manifest()
        if plan.run_id != run_id or (runner.repository.run_dir(run_id) / "plan.json").exists():
            raise StateTransitionError("verification_initial_plan_mismatch")
        require_active_execution(plan)
        plan = json_value(plan)
        original = make_policy(runner.runtime, plan, source="new_run")
    else:
        plan = runner.repository.read_plan(run_id)
        require_active_execution(plan)
        original = read_policy(runner.repository, run_id, plan, runtime=runner.runtime,
                               state=runner.repository.read_progress(run_id))
    body = {
        "schema_version": "copilot-verification-execution-v1",
        "run_id": run_id, "execution_id": owner.execution_id,
        "request_id": request_id, "request_sha256": request_sha256,
        "plan_sha256": plan["plan_sha256"],
        "original_policy_sha256": original["policy_sha256"],
        "policy": "automatic_hold_v1",
        "background_only": runner.runtime.browser.background_only,
        "guard_namespace": str(runner.runtime.browser.user_data_dir or "default-chrome-user-data"),
        "lease_lock_root": str(runner.runtime.profiles.lease_lock_root) if runner.runtime.profiles.lease_lock_root is not None else None,
        "source": "explicit_start" if initial else "explicit_refetch",
    }
    if account_emails is not None:
        from ..browser.copilot_blockers import account_emails as validate_accounts
        body["account_emails"] = validate_accounts(account_emails, runner.runtime.selected_profile_ids)
        body["blocker_policy"] = "copilot_blockers_v1"
    receipt = {**body, "policy_sha256": canonical_sha256(body)}
    path = runner.repository.run_dir(run_id) / "execution-history" / (owner.execution_id + ".verification-policy.json")
    if path.exists():
        if json.loads(path.read_text()) != receipt:
            raise IntegrityError("verification_execution_policy_changed")
    else:
        publish_json(path, receipt, exclusive=True)
    owner.assert_owned()
    runner._automatic_verification_execution = (run_id, owner.execution_id, path, receipt)
    return receipt


def effective_policy(runner, run_id):
    selected = getattr(runner, "_automatic_verification_execution", None)
    owner = runner._owners.get(run_id)
    if selected is not None and owner is not None and selected[:2] == (run_id, owner.execution_id):
        owner.assert_owned()
        try:
            observed = json.loads(selected[2].read_text())
        except (OSError, ValueError) as exc:
            raise IntegrityError("verification_execution_policy_unreadable") from exc
        if observed != selected[3]:
            raise IntegrityError("verification_execution_policy_changed")
        from ..recovery_adapters.execution_policy import read_policy
        plan = runner.repository.read_plan(run_id)
        original = read_policy(runner.repository, run_id, plan, runtime=runner.runtime,
                               state=runner.repository.read_progress(run_id))
        if (observed["plan_sha256"] != plan["plan_sha256"]
                or observed["original_policy_sha256"] != original["policy_sha256"]
                or observed["background_only"] != runner.runtime.browser.background_only
                or observed["guard_namespace"] != str(runner.runtime.browser.user_data_dir or "default-chrome-user-data")
                or observed["lease_lock_root"] != (str(runner.runtime.profiles.lease_lock_root) if runner.runtime.profiles.lease_lock_root is not None else None)):
            raise IntegrityError("verification_execution_policy_binding_changed")
        return "automatic_hold_v1"
    return runner.runtime.browser.copilot_verification_policy


def effective_accounts(runner, run_id):
    # Reuse all execution/plan/policy receipt checks before returning its
    # explicit account bindings. Resume never inherits a past executor opt-in.
    effective_policy(runner, run_id)
    selected = getattr(runner, "_automatic_verification_execution", None)
    owner = runner._owners.get(run_id)
    if selected is None or owner is None or selected[:2] != (run_id, owner.execution_id):
        return {}
    return dict(selected[3].get("account_emails", {}))
