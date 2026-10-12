"""Durable Copilot access holds; this module never submits provider prompts.

The pair marker is the crash-safe retention authority. Generic recovery resource
events audit it without changing prompt effect certainty. Marker publication
precedes input/registry work; disposition audit precedes guard release.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import time
import uuid
from datetime import datetime, timezone

from ..errors import HumanVerificationRequiredError, IntegrityError, StateTransitionError
from .profile_access import (
    read_verification_marker, verification_marker_path,
    verification_marker_transaction,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


class VerificationHoldStore:
    def __init__(self, manager, profiles, *, run_id, plan_sha256, capacity=2):
        self.manager = manager
        self.profiles = {profile.id: profile for profile in profiles}
        self.run_id, self.plan_sha256 = run_id, plan_sha256
        self.capacity = min(2, capacity)

    def path(self, profile_id):
        profile = self.profiles[profile_id]
        return verification_marker_path(self.manager.lock_root, self.manager.namespace,
                                        profile.chrome_directory, "copilot")

    def transaction(self, profile_id, *, timeout_seconds=5):
        return verification_marker_transaction(self.path(profile_id),
            profile=self.profiles[profile_id], namespace=self.manager.namespace,
            provider="copilot", timeout_seconds=timeout_seconds)

    def markers(self):
        for profile_id, profile in self.profiles.items():
            body = read_verification_marker(self.path(profile_id))
            if body is None:
                continue
            if any(body.get(key) != expected for key, expected in (
                    ("namespace", self.manager.namespace), ("profile_id", profile_id),
                    ("chrome_directory", profile.chrome_directory), ("provider", "copilot"))):
                raise IntegrityError("verification_hold_scope_changed")
            yield body

    def rows(self, *, retained_only=True, own_run=True):
        result = []
        for body in self.markers():
            for row in body["episodes"].values():
                if retained_only and not row["retained"]:
                    continue
                if own_run and row["run_id"] != self.run_id:
                    continue
                if row["run_id"] == self.run_id and row.get("plan_sha256") != self.plan_sha256:
                    raise IntegrityError("verification_hold_plan_changed")
                result.append(copy.deepcopy(row))
        return tuple(sorted(result, key=lambda row: (row.get("detected_at", ""), row["episode_id"])))

    def episode(self, episode_id):
        found = [row for row in self.rows(retained_only=False) if row["episode_id"] == episode_id]
        if len(found) != 1:
            raise StateTransitionError("verification_episode_unavailable")
        return found[0]

    def retained(self, tab):
        identity = tab.identity
        if identity.provider != "copilot":
            return False
        body = read_verification_marker(self.path(identity.profile_id))
        if body is None:
            return False
        if (body["profile_id"] != identity.profile_id or body["chrome_directory"] != identity.chrome_directory
                or body["namespace"] != self.manager.namespace):
            raise IntegrityError("verification_hold_scope_changed")
        return any(row["retained"] and row["identity"].get("tab_reference") == identity.tab_reference
                   for row in body["episodes"].values())

    def scope_held(self, profile_id, *, item_id=None):
        body = read_verification_marker(self.path(profile_id))
        if body is None:
            return False
        return any(row["retained"] and (item_id is None or item_id not in row.get("active_item_ids", ()))
                   for row in body["episodes"].values())

    def reference(self, row):
        return {"identity": row["identity"], "item_id": row["observation_id"],
                "platform": "copilot", "resource_scopes": [row["profile_id"] + ":copilot"],
                "verification_hold": copy.deepcopy(row)}

    def begin(self, row, *, owner, audit):
        owner.assert_owned()
        with self.transaction(row["profile_id"], timeout_seconds=max(0, row["deadline_monotonic"] - time.monotonic())) as body:
            owner.assert_owned()
            live = [r for r in body["episodes"].values() if r["retained"]]
            if (len(live) >= self.capacity or any(r["run_id"] != self.run_id
                    or r["identity"]["tab_reference"] == row["identity"]["tab_reference"]
                    or r["identity"]["browser_target_reference"] == row["identity"]["browser_target_reference"] for r in live)):
                raise IntegrityError("verification_hold_capacity_conflict")
            body["episodes"][row["episode_id"]] = copy.deepcopy(row)
            terminal = [key for key, r in body["episodes"].items() if not r["retained"]]
            for key in terminal[:-128]:
                if not any(c["episode_id"] == key for c in body["commands"].values()):
                    del body["episodes"][key]
        # If the audit fails, the already durable intent still protects cleanup
        # and future admission. Do not roll it back to allow another send.
        owner.assert_owned()
        audit("tab:" + row["identity"]["tab_reference"], "blocked", owner,
              reference=self.reference(row))
        return copy.deepcopy(row)

    def transition(self, episode_id, *, owner, audit, changes, expected_revision=None,
                   exclude_request_id=None, timeout_seconds=5):
        original = self.episode(episode_id)
        with self.transaction(original["profile_id"], timeout_seconds=timeout_seconds) as body:
            owner.assert_owned()
            row = body["episodes"][episode_id]
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StateTransitionError("verification_control_stale")
            updated = {**row, **changes, "revision": row["revision"] + 1, "updated_at": _now()}
            release = row["retained"] and not updated["retained"]
            if release:
                # Audit while the old guard still exists. Failure leaves it held.
                status = "owned" if updated["status"] == "resumed" else "released"
                audit("tab:" + row["identity"]["tab_reference"], status, owner,
                      reference=self.reference(updated))
            body["episodes"][episode_id] = updated
            if not updated["retained"]:
                for command in body["commands"].values():
                    if (command["episode_id"] == episode_id and command["status"] == "queued"
                            and command["request_id"] != exclude_request_id):
                        command.update(status="refused", reason="verification_episode_settled")
        if not release:
            owner.assert_owned()
            audit("tab:" + updated["identity"]["tab_reference"], "blocked", owner,
                  reference=self.reference(updated))
        return copy.deepcopy(updated)

    def request(self, *, episode_id, revision, execution_id, request_id, action):
        from ..recovery.contracts import safe_id
        if (action not in {"show", "check", "close"} or type(revision) is not int or revision < 1
                or not isinstance(request_id, str) or len(request_id) > 100):
            raise ValueError("invalid_verification_command")
        try:
            for value in (episode_id, execution_id, request_id):
                safe_id(value)
        except (ValueError, TypeError):
            raise ValueError("invalid_verification_command") from None
        row = self.episode(episode_id)
        command = {"episode_id": episode_id, "revision": revision, "execution_id": execution_id,
                   "request_id": request_id, "action": action}
        with self.transaction(row["profile_id"]) as body:
            existing = body["commands"].get(request_id)
            if existing is not None:
                if any(existing.get(key) != value for key, value in command.items()):
                    raise StateTransitionError("verification_request_conflict")
                return copy.deepcopy(existing)
            row = body["episodes"][episode_id]
            if (row["status"] == "intent_pending" or not row["retained"] or row["revision"] != revision
                    or row["execution_id"] != execution_id):
                raise StateTransitionError("verification_control_stale")
            if sum(c["status"] == "queued" for c in body["commands"].values()) >= 64:
                raise StateTransitionError("verification_command_capacity")
            command.update(status="queued", requested_at=_now())
            body["commands"][request_id] = command
            completed = [key for key, c in body["commands"].items() if c["status"] != "queued"]
            for key in completed[:-256]:
                del body["commands"][key]
        return copy.deepcopy(command)

    def commands(self, episode_id):
        row = self.episode(episode_id)
        body = read_verification_marker(self.path(row["profile_id"]))
        return tuple(copy.deepcopy(c) for c in body["commands"].values()
                     if c["episode_id"] == episode_id and c["status"] == "queued")

    def receipt(self, request_id):
        for body in self.markers():
            if request_id in body["commands"]:
                return copy.deepcopy(body["commands"][request_id])
        raise StateTransitionError("verification_command_unavailable")

    def finish_command(self, command, *, owner, status, reason="", **detail):
        row = self.episode(command["episode_id"])
        with self.transaction(row["profile_id"]) as body:
            owner.assert_owned()
            current = body["commands"][command["request_id"]]
            if current["status"] != "queued":
                return copy.deepcopy(current)
            current.update(status=status, reason=reason, completed_at=_now(), **detail)
            result = copy.deepcopy(current)
        return result

    def public(self):
        from ..execution.run_ownership import _boot_identity
        rows = self.rows()
        boot = _boot_identity() if rows else None
        result = []
        for row in rows:
            remaining = None
            if boot and row.get("boot_id") == boot:
                remaining = max(0, row["deadline_monotonic"] - time.monotonic())
            safe = {key: row.get(key) for key in (
                "episode_id", "revision", "execution_id", "observation_id", "profile_id",
                "turn", "phase", "status", "dispatch_state", "detected_at", "reason",
                "verification_cleared", "background_only", "blocker_kind")}
            safe.update(retained=True, remaining_seconds=remaining,
                        deadline_expired=remaining == 0 or remaining is None,
                        actions=[] if row["status"] == "intent_pending" else ["show", "check", "close"])
            body = read_verification_marker(self.path(row["profile_id"]))
            commands = [c for c in body["commands"].values() if c["episode_id"] == row["episode_id"]]
            # Canonical JSON sorts request-id keys, not command chronology.
            safe["commands"] = [copy.deepcopy(c) for c in sorted(commands,
                key=lambda c: (c.get("requested_at", ""), c["request_id"]))[-8:]]
            result.append(safe)
        return result


class CopilotVerificationHolds:
    """Bind the durable store to this execution's borrowed browser attachment."""
    def __init__(self, store, attachment, recovery, actions, *, cancelled):
        self.store, self.attachment, self.recovery, self.actions = store, attachment, recovery, actions
        self.cancelled = cancelled
        self.live = {}
        self.uncommitted_targets = set()
        def retain(tab):
            if tab.identity.tab_reference in self.uncommitted_targets:
                raise IntegrityError("verification_intent_publication_unconfirmed")
            return store.retained(tab)
        attachment.session.registry.retention_predicate = retain
        recovery.verification_holds = self

    def operation_deadline(self, submission, deadline):
        """A retained episode cannot acquire a later budget on re-entry."""
        entry = self.live.get(id(submission))
        if entry is None:
            return deadline
        row = self.store.episode(entry["episode_id"])
        return replace(deadline, expires_at=min(deadline.expires_at, row["deadline_monotonic"]))

    async def begin(self, submission, deadline, *, blocker_kind="verification"):
        from .profile_matching import browser_target_id
        from ..websites.chat_actions import strict_page_identity, _canonical_reference, _await_call
        from ..websites.chat_actions import _conversation_history, _text_sha256
        from ..execution.run_ownership import _boot_identity
        if self.cancelled() or deadline.expired():
            raise HumanVerificationRequiredError("verification_wait_cancelled_or_expired")
        if id(submission) in self.live:
            bounded = self.operation_deadline(submission, deadline)
            if bounded.expired():
                bounded.raise_expired()
            return self.store.episode(self.live[id(submission)]["episode_id"])
        if blocker_kind not in {"verification", "sign_in", "cookies", "other"}:
            raise ValueError("unsupported_copilot_blocker_kind")
        owned = next((tab for tab in self.attachment.session.registry.tabs
                      if tab.page is submission.page), None)
        if owned is not None:
            # Preserve a known owned target while durable intent publication is
            # uncertain. Cleanup reports unconfirmed and quarantines its lane.
            self.uncommitted_targets.add(owned.identity.tab_reference)
        generation = self.attachment.attached_health.browser_session_id
        if (owned is None or owned.identity.provider != "copilot" or not generation
                or not owned.identity.browser_target_reference):
            raise IntegrityError("verification_target_unproven")
        for field in ("profile_id", "chrome_directory", "browser_context_reference",
                      "browser_target_reference", "tab_reference"):
            if getattr(owned.identity, field) != getattr(submission.identity, field):
                raise IntegrityError("verification_owned_identity_changed")
        episode_id = "verification-" + uuid.uuid4().hex
        attempts = self.recovery.coordinator.ledger.select("attempt_started", item_id=submission.observation_id)
        current = next((event["payload"] for event in reversed(attempts)
                        if event["payload"]["pass_id"] == self.recovery.pass_id), {})
        effects = self.recovery.coordinator.ledger.select("effect_admitted", attempt_id=current.get("attempt_id")) if current.get("attempt_id") else ()
        # Opening/retrieving a page admits no provider prompt. Keep actual send
        # admission, local submission state and unknown effect kinds conservative.
        send_admitted = submission._submitted or any(
            event["payload"].get("action") not in {"navigate", "retrieve_existing"}
            for event in effects)
        operation = str(current.get("operation_id") or "")
        identity = submission.identity.to_mapping()
        identity["conversation_reference"] = _canonical_reference(identity["conversation_reference"], submission.spec)
        active = {attempt.operation_id.rsplit(":", 1)[0] for attempt in self.recovery.attempts.values()}
        active.difference_update(self.recovery.finished | self.recovery.failed)
        active.add(submission.observation_id)
        row = {"episode_id": episode_id, "revision": 1, "retained": True,
               "run_id": self.store.run_id, "plan_sha256": self.store.plan_sha256,
               "execution_id": self.recovery.owner.execution_id,
               "observation_id": submission.observation_id, "profile_id": submission.profile_id,
               "identity": identity, "browser_session_id": generation, "boot_id": _boot_identity(),
               "turn": 2 if operation.endswith(":same_chat_turn_2") else 1,
               "phase": "response" if submission._submitted else "pre_send" if submission._prepared else "preparation",
               "operation_id": operation, "attempt_id": current.get("attempt_id"),
               "prompt_sha256": _text_sha256(submission._prompt) if submission._prompt else None,
               "dispatch_state": "admitted_or_unknown" if send_admitted else "not_admitted",
               "deadline_monotonic": deadline.expires_at,
               "detected_at": _now(), "status": "intent_pending",
               "reason": "human_verification_required" if blocker_kind == "verification" else "copilot_" + blocker_kind + "_required",
               "blocker_kind": blocker_kind,
               "background_only": self.attachment.config.browser.background_only,
               "active_item_ids": sorted(active), "verification_cleared": False}
        self.live[id(submission)] = {"episode_id": episode_id, "history": None}
        try:
            self.store.begin(row, owner=self.recovery.owner, audit=self.recovery.record_resource)
        finally:
            # If the marker committed but the audit failed, durable retention
            # suffices. If publication failed, the in-memory barrier remains.
            try:
                if self.store.retained(owned):
                    self.uncommitted_targets.discard(owned.identity.tab_reference)
            except Exception:
                pass
        # A later read failure leaves the registered target's durable intent
        # intact instead of returning it to ordinary error cleanup.
        observed = await strict_page_identity(submission.page, profile_id=submission.profile_id,
            chrome_directory=submission.chrome_directory, _deadline=deadline)
        submission.identity.assert_same_tab(observed,
            require_same_conversation=blocker_kind != "sign_in" and not submission._submitted,
            canonical=lambda ref: _canonical_reference(ref, submission.spec))
        if blocker_kind != "sign_in" and submission._submitted and not submission._accept_live_identity(observed):
            raise IntegrityError("verification_conversation_changed")
        target = await _await_call(browser_target_id, submission.page, _deadline=deadline)
        if observed.browser_target_reference != "cdp-target:" + target:
            raise IntegrityError("verification_target_unproven")
        if blocker_kind == "sign_in":
            # Keep the original Copilot reference. OAuth URLs may contain
            # credentials; neither persist them nor treat their DOM as history.
            return self.update(submission, status="manual_required", _timeout_seconds=deadline.remaining())
        self.live[id(submission)]["history"] = await _conversation_history(submission.page, submission.spec, _deadline=deadline)
        identity = observed.to_mapping()
        identity["conversation_reference"] = _canonical_reference(identity["conversation_reference"], submission.spec)
        return self.update(submission, identity=identity, status="manual_required", _timeout_seconds=deadline.remaining())

    def update(self, submission, *, _timeout_seconds=5, **changes):
        entry = self.live.get(id(submission))
        if entry is None:
            return None
        return self.store.transition(entry["episode_id"], owner=self.recovery.owner,
            audit=self.recovery.record_resource, changes=changes, timeout_seconds=_timeout_seconds)

    async def validated(self, submission, deadline):
        from ..websites.chat_actions import _conversation_history
        entry = self.live.get(id(submission))
        if entry is None:
            return
        row = self.store.episode(entry["episode_id"])
        deadline = self.operation_deadline(submission, deadline)
        if deadline.expired():
            deadline.raise_expired()
        if (row["dispatch_state"] == "admitted_or_unknown" and not submission._submitted
                and not getattr(submission, "_retrieval_only", False)):
            raise HumanVerificationRequiredError("verification_dispatch_requires_reconciliation")
        if not submission._submitted:
            history = await _conversation_history(submission.page, submission.spec, _deadline=deadline)
            if entry["history"] is None or history != entry["history"]:
                raise HumanVerificationRequiredError("verification_history_changed")
        deadline.raise_expired() if deadline.expired() else None
        target_ref = row["identity"]["tab_reference"]
        self.uncommitted_targets.add(target_ref)
        try:
            self.update(submission, status="resumed", retained=False, verification_cleared=True,
                        reason="", _timeout_seconds=deadline.remaining())
            if deadline.expired():
                self.update(submission, status="expired_held", retained=True, reason="verification_deadline_expired")
                deadline.raise_expired()
        except BaseException:
            # A failed re-hold after a late disposition cannot return the target
            # to ordinary cleanup. Keep the local uncertainty barrier unless a
            # durable retained row is already authoritative.
            try:
                if self.store.episode(entry["episode_id"])["retained"]:
                    self.uncommitted_targets.discard(target_ref)
            except Exception:
                pass
            raise
        self.uncommitted_targets.discard(target_ref)
        self.live.pop(id(submission), None)

    def failed(self, submission, error, deadline):
        entry = self.live.get(id(submission))
        if entry is None:
            return
        try:
            row = self.store.episode(entry["episode_id"])
        except StateTransitionError:
            return  # Failed publication remains protected by the local barrier.
        if not row["retained"]:
            return
        expired = deadline.expired() or time.monotonic() >= row["deadline_monotonic"]
        status = "expired_held" if expired else "unsupported_held"
        kind = row.get("blocker_kind", "verification")
        reason = ("verification_deadline_expired" if kind == "verification" else "copilot_blocker_deadline_expired") if expired else (
            "verification_clearance_failed" if kind == "verification" else row.get("reason", "copilot_blocker_clearance_failed"))
        if row["status"] != status or row.get("reason") != reason:
            self.update(submission, status=status, reason=reason)

    def confirmed_cleanup(self, report):
        closed = set(getattr(report, "closed_tab_references", ()))
        for row in self.store.rows():
            if row["identity"]["tab_reference"] in closed:
                self.store.transition(row["episode_id"], owner=self.recovery.owner,
                    audit=self.recovery.record_resource, changes={"status": "closed", "retained": False})

    async def poll_commands(self, submission, deadline):
        entry = self.live.get(id(submission))
        if entry is None:
            return
        from .verification_controls import execute_live_command
        for command in self.store.commands(entry["episode_id"]):
            await execute_live_command(self, submission, command)
