"""Verification disposition before an authorized, exclusive Chrome restart.

No prompt, replacement tab, native input, or effect-fence mutation occurs here.
The host holds the Chrome lifecycle lock through this call and the restart.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from ..errors import StateTransitionError
from .profile_access import read_verification_marker
from .verification_controls import control, store_for_runtime


@dataclass
class RestartAuthorization:
    """One startup request's context rebind permission, never send authority."""

    request_id: str
    owner: object
    profiles: frozenset[tuple[str, str]]
    browser_session_id: str = ""
    contexts: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)


def attachment_rebind_authorizer(repository, attachment):
    """Reuse a restart receipt only in its owner's verified browser generation."""
    session = repository.active_session
    receipt = session.restart_authorization if session is not None else None
    if receipt is None:
        return None
    try:
        if session.owner is not receipt.owner:
            raise StateTransitionError("browser_restart_owner_changed")
        receipt.owner.assert_owned()
        generation = str(getattr(getattr(attachment, "attached_health", None), "browser_session_id", "") or "")
        anchors = getattr(getattr(attachment, "session", None), "anchors", ())
        contexts = frozenset((anchor.profile.id, anchor.profile.chrome_directory,
                              str(anchor.browser_context_id or "")) for anchor in anchors)
        if (not generation or {row[:2] for row in contexts} != receipt.profiles
                or any(not row[2] for row in contexts)):
            session.restart_authorization = None
            return None
        if receipt.browser_session_id:
            if generation != receipt.browser_session_id or contexts != receipt.contexts:
                raise StateTransitionError("browser_restart_generation_changed")
        else:
            receipt.browser_session_id, receipt.contexts = generation, contexts
    except BaseException:
        session.restart_authorization = None
        raise

    def authorize(profile_id, chrome_directory, browser_context_reference):
        if (repository.active_session is not session or session.owner is not receipt.owner
                or session.restart_authorization is not receipt):
            if session.restart_authorization is receipt:
                session.restart_authorization = None
            return False
        try:
            receipt.owner.assert_owned()
        except Exception:
            session.restart_authorization = None
            raise
        if (profile_id, chrome_directory, browser_context_reference) not in receipt.contexts:
            session.restart_authorization = None
            return False
        return True

    return authorize


def prepare(runner, *, request_id, recovery_request=None):
    from ..execution.manage_measurements import Runner, _require_sync_caller
    from ..recovery.contracts import safe_id
    _require_sync_caller()
    safe_id(request_id)
    original = None
    admitted = None
    runner._restart_recovery_request = None
    session = runner.repository.active_session
    if session is not None:
        session.restart_authorization = None
    if recovery_request is not None:
        from ..recovery.api import RecoveryRequest
        from ..recovery.contracts import digest as request_digest
        original = RecoveryRequest.from_dict(recovery_request).to_dict()
        admitted = runner.repository.recovery_execution
        if admitted is None:
            matches = original == runner.plan_refetch(original["run_id"], request_id=original["request_id"])
        else:
            # Admission already checked the preview under exclusive ownership
            # and changed the journal. Only this deferred startup may use that
            # exact receipt instead of comparing with its own new revision.
            admitted.assert_owned()
            matches = (
                runner._browser_startup_in_progress
                and runner._execution_thread_id == threading.get_ident()
                and runner._owners.get(original["run_id"]) is admitted.owner
                and original["run_id"] == admitted.snapshot.manifest.run_id
                and request_digest(original) == admitted.pass_record.request_digest
            )
        if not matches:
            raise StateTransitionError("browser_restart_recovery_preview_stale")
    store = store_for_runtime(runner.runtime, "restart-inspection", "")
    profiles = store.profiles
    if not profiles:
        raise StateTransitionError("browser_restart_profiles_missing")
    root = store.path(next(iter(profiles))).parent
    rows = []
    try:
        paths = sorted(path for path in root.iterdir() if path.name.endswith(".verification.json"))
    except FileNotFoundError:
        paths = []
    for path in paths:
        marker = read_verification_marker(path)
        if marker is None:
            continue
        retained = [row for row in marker["episodes"].values() if row["retained"]]
        if not retained:
            continue
        profile = profiles.get(marker["profile_id"])
        if (marker["namespace"] != store.manager.namespace or profile is None
                or marker["chrome_directory"] != profile.chrome_directory
                or path != store.path(profile.id)):
            raise StateTransitionError("browser_restart_unselected_verification_hold")
        rows.extend(retained)

    # Validate every scope before disposing any target. A live foreign executor
    # remains protected even if it does not participate in the host's lock.
    for row in rows:
        plan = runner.repository.read_plan(row["run_id"])
        if plan["plan_sha256"] != row["plan_sha256"] or not any(
                p.get("platform") == "copilot" and p.get("profile_id") == row["profile_id"]
                and row["observation_id"] in p.get("observation_ids", ()) for p in plan.get("packets", ())):
            raise StateTransitionError("browser_restart_hold_binding_changed")
        observed = runner.ownership(row["run_id"])
        current = runner._owners.get(row["run_id"])
        if observed["state"] != "available" and not (
                observed["state"] == "owned" and current is not None
                and current.execution_id == observed.get("execution_id")):
            raise StateTransitionError("browser_restart_owner_unavailable")

    closed = []
    for row in rows:
        # One Runner cannot own two run IDs. A stopped older run gets a separate
        # ownership context, while this worker's own run keeps its current epoch.
        target = runner if row["run_id"] in runner._owners else Runner(
            runner.runtime, browser_runtime_factory=runner.browser_runtime_factory)
        digest = hashlib.sha256((request_id + ":" + row["episode_id"] + ":" + str(row["revision"])).encode()).hexdigest()
        outcome = control(target, row["run_id"], episode_id=row["episode_id"],
            revision=row["revision"], execution_id=row["execution_id"],
            request_id="restart-" + digest, action="close", _restart=True)
        if outcome["status"] != "completed":
            raise StateTransitionError("browser_restart_verification_close_unconfirmed")
        closed.append(row["episode_id"])
    # The owned profile factory may rebind saved-history retrieval to this
    # profile's new context after the host restart, never strict live Resume.
    if session is not None and session.owner is not None:
        session.owner.assert_owned()
        session.restart_authorization = RestartAuthorization(request_id, session.owner,
            frozenset((p.id, p.chrome_directory) for p in profiles.values()))
    result = {"closed_verification_episodes": closed}
    # An admitted pass keeps its immutable selection, including blocked work.
    # Tab disposition alone cannot turn that selection into new send authority.
    if original is not None and closed and admitted is None:
        # This click authorizes failed/remaining work after its own tab cleanup.
        # Only refresh the exact request validated before disposition. Frozen
        # scope/budgets and every ambiguous effect remain authoritative.
        fresh = runner.plan_refetch(original["run_id"], request_id=original["request_id"])
        runner._restart_recovery_request = (original, fresh)
        result["recovery_request"] = fresh
    return result
