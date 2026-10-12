"""One eligibility, authorization and effect-accounting state machine."""
from __future__ import annotations

import dataclasses
import math
import uuid
from typing import Any, Mapping

from .contracts import (
    AllowanceExceeded, AttemptRecord, EffectRecord, EffectCertainty, ItemState,
    OperationSpec, RecoveryBlocked, RecoveryIntegrityError, RecoveryPass,
    RecoveryPreview, RecoveryRequest, RecoverySelection, RecoverySnapshot,
    RunManifest, StaleRecoveryPreview, StepObservation, digest, json_value, safe_id,
)
from .ledger import RecoveryLedger, now


def next_action(state: ItemState) -> str:
    if state.status == "succeeded":
        return "retain"
    if state.status == "active":
        return "wait"
    if state.status == "blocked":
        return "blocked"
    if state.reconstructable:
        return "reconstruct"
    unresolved = set(state.unresolved_attempt_ids)
    if unresolved - set(state.checked_attempt_ids):
        return "reconcile"
    if unresolved:
        return "reconcile_or_replace"
    if state.saved_input_available:
        return "parse_saved"
    return "execute"


def plan_recovery(snapshot: RecoverySnapshot) -> RecoveryPreview:
    by_id = {op.operation_id: op for op in snapshot.manifest.operations}
    states = {state.item_id: state for state in snapshot.items}
    selected = []
    for item in snapshot.manifest.items:
        state = states[item.item_id]
        action = next_action(state)
        if action == "retain":
            continue
        op_states = dict(state.operation_states)
        failed_units = {by_id[op].restart_unit for op in item.operation_ids
                        if op_states.get(op) == "failed" and by_id[op].restart_unit}
        operations = tuple(op for op in item.operation_ids if op_states.get(op) != "succeeded"
                           or by_id[op].restart_unit in failed_units)
        selected.append(RecoverySelection(
            item_id=item.item_id, action=action, reason_code=state.reason_code,
            replacement_attempt_ids=tuple(a for a in state.unresolved_attempt_ids if a in state.checked_attempt_ids),
            operation_ids=operations,
            max_attempts=sum(by_id[op].max_attempts for op in operations),
            max_effects=sum(by_id[op].max_effects for op in operations),
        ))
    values = dict(run_id=snapshot.manifest.run_id, scope_digest=snapshot.manifest.scope_digest,
                  revision_digest=snapshot.revision_digest, manifest_digest=digest(snapshot.manifest),
                  selections=tuple(selected))
    return RecoveryPreview(**values, preview_digest=digest(values))


class RecoveryCoordinator:
    def __init__(self, ledger: RecoveryLedger, manifest: RunManifest | None = None):
        self.ledger = ledger
        self.manifest = manifest

    def plan(self, snapshot: RecoverySnapshot) -> RecoveryPreview:
        return plan_recovery(snapshot)

    def _events(self, kind=None):
        # Normalized order (baseline events, then journal records), served
        # from the ledger's verified in-memory view.
        return self.ledger.events(kind)

    def _select(self, kind, **field_value):
        return self.ledger.select(kind, **field_value)

    def _latest_observation(self, effect_id):
        observed = self._select("effect_observed", effect_id=effect_id)
        return observed[-1]["payload"] if observed else None

    def _latest_certainty(self, effect_id):
        latest = self._latest_observation(effect_id)
        return latest["certainty"] if latest is not None else None

    def _manifest(self) -> RunManifest:
        if self.manifest is not None:
            return self.manifest
        entries = self._events("manifest")
        if len(entries) != 1:
            raise RecoveryIntegrityError("manifest_missing_or_duplicated")
        value = entries[0]["payload"]["manifest"]
        from .contracts import WorkItem
        self.manifest = RunManifest(
            run_id=value["run_id"], scope_digest=value["scope_digest"],
            workflow_version=value["workflow_version"], policy_version=value["policy_version"],
            items=tuple(WorkItem(i["item_id"], tuple(i["operation_ids"]), i.get("output_contract", "1")) for i in value["items"]),
            operations=tuple(OperationSpec(**{**dict(op), "dependencies": tuple(op.get("dependencies", ())),
                            "resource_scopes": tuple(op.get("resource_scopes", ())),
                            "cost_limits": tuple(tuple(pair) for pair in op.get("cost_limits", ())),
                            "reconciliation_effects": tuple(op.get("reconciliation_effects", ("retrieve_existing", "status_poll", "download", "poll", "fetch")))}) for op in value["operations"]),
        )
        return self.manifest

    def _pass_payload(self, pass_id):
        found = [e["payload"] for e in self._select("pass_authorized", pass_id=pass_id)]
        if len(found) != 1:
            raise RecoveryBlocked("pass_not_authorized")
        return found[0]

    def _pass_is_terminal(self, pass_id) -> bool:
        return bool(self.ledger.select_many(("pass_completed", "pass_cancelled"), pass_id=pass_id))

    @staticmethod
    def _pass(payload, *, duplicate=False, execution_id=None):
        request = RecoveryRequest.from_dict(payload["request"])
        return RecoveryPass(payload["pass_id"], request.request_id,
                            execution_id or payload["execution_id"], payload["request_digest"], request.selections, duplicate)

    def authorize(self, request: RecoveryRequest, snapshot: RecoverySnapshot, owner) -> RecoveryPass:
        safe_id(request.request_id)
        with self.ledger.transaction(owner):
            records = self.ledger.read()
            existing = [e["payload"] for e in records if e["kind"] == "pass_authorized"
                        and e["payload"]["request"]["request_id"] == request.request_id]
            if existing:
                if existing[0]["request_digest"] != digest(request):
                    raise RecoveryBlocked("request_identity_conflict")
                return self._pass(existing[0], duplicate=True)
            sampled_journal = tuple(e.get("checksum") for e in snapshot.events if e.get("checksum") is not None)
            actual_journal = tuple(e["checksum"] for e in records)
            if sampled_journal != actual_journal:
                raise StaleRecoveryPreview("recovery_journal_changed_since_snapshot")
            preview = self.plan(snapshot)
            if (request.run_id != preview.run_id or request.scope_digest != preview.scope_digest
                or request.revision_digest != preview.revision_digest or request.manifest_digest != preview.manifest_digest
                or request.preview_digest != preview.preview_digest or request.selections != preview.selections):
                raise StaleRecoveryPreview("stale_recovery_preview")
            if any(s.action == "wait" for s in request.selections):
                raise RecoveryBlocked("work_already_active")
            active = {e["payload"]["pass_id"] for e in records if e["kind"] == "pass_authorized"}
            terminal = {e["payload"]["pass_id"] for e in records if e["kind"] in {"pass_completed", "pass_cancelled"}}
            # A deliberate new request may retire an interrupted authorization only
            # after current revision validation and exclusive ownership. This grants
            # no refund and makes all callbacks from its old execution stale.
            interrupted = active - terminal
            current_execution_passes = {e["payload"]["pass_id"] for e in records
                                        if e["kind"] in {"pass_authorized", "pass_resumed"}
                                        and e["payload"]["execution_id"] == owner.execution_id}
            if interrupted & current_execution_passes:
                raise RecoveryBlocked("execution_has_active_pass")
            manifest_records = [e for e in records if e["kind"] == "manifest"]
            if manifest_records and manifest_records[0]["payload"]["manifest_digest"] != digest(snapshot.manifest):
                raise RecoveryIntegrityError("frozen_manifest_changed")
            self.manifest = snapshot.manifest
            by_id = {op.operation_id: op for op in self.manifest.operations}
            selected_ops = {o for s in request.selections for o in s.operation_ids}
            for repair in request.parser_repairs:
                op = by_id.get(repair.operation_id)
                if (op is None or repair.operation_id not in selected_ops or not op.parser_version
                    or repair.from_adapter_version != op.adapter_version or repair.input_digest != op.input_digest
                    or not repair.compatibility_id or not repair.parser_version):
                    raise RecoveryBlocked("incompatible_parser_repair")
            if not manifest_records:
                self.ledger.append("manifest", {"manifest": json_value(snapshot.manifest),
                                   "manifest_digest": digest(snapshot.manifest),
                                   "baseline_revision_digest": snapshot.revision_digest,
                                   "baseline_items": json_value(snapshot.items),
                                   "baseline_events": json_value(tuple(e for e in snapshot.events if e.get("baseline") is True))}, owner)
            for interrupted_id in sorted(interrupted):
                old = self._pass_payload(interrupted_id)
                old_terminal = {e["payload"].get("item_id") for e in records if e["kind"] in {"failure", "revision_committed"}
                                and e["payload"].get("pass_id") == interrupted_id}
                for selection in old["request"]["selections"]:
                    if selection["item_id"] not in old_terminal:
                        self.ledger.append("failure", {"pass_id": interrupted_id, "item_id": selection["item_id"],
                                           "reason_code": "execution_interrupted", "checked_attempt_ids": ()}, owner)
                self.ledger.append("pass_cancelled", {"pass_id": interrupted_id, "reason_code": "superseded_by_explicit_request"}, owner)
            pass_id = str(uuid.uuid4())
            payload = {"pass_id": pass_id, "execution_id": owner.execution_id,
                       "request": request.to_dict(), "request_digest": digest(request)}
            self.ledger.append("pass_authorized", payload, owner)
            owner.bind_pass(pass_id)
            return self._pass(payload)

    def import_original_authorization(self, snapshot: RecoverySnapshot, owner, *, attempt_limits: Mapping[str, int], effect_limits=None) -> RecoveryPass:
        """Migrate frozen original authorization with its consumed baseline.

        This is strict continuation, never a recovery grant. The compatibility
        adapter supplies original per-operation preparation ceilings explicitly;
        every ceiling must narrow the manifest, and lifetime baseline events use
        the fixed legacy-original pass identity so they consume these limits.
        """
        pass_id = "legacy-original"
        with self.ledger.transaction(owner):
            existing = self._events("pass_authorized")
            if existing:
                if len(existing) == 1 and existing[0]["payload"]["pass_id"] == pass_id:
                    return self.resume_pass(pass_id, owner)
                raise RecoveryBlocked("original_authorization_already_migrated")
            operations = {op.operation_id: op for op in snapshot.manifest.operations}
            if set(attempt_limits) != set(operations):
                raise RecoveryBlocked("original_attempt_limits_incomplete")
            limits = {}
            effect_limits = effect_limits or {op: spec.max_effects for op, spec in operations.items()}
            for operation_id, spec in operations.items():
                attempts, effects = attempt_limits[operation_id], effect_limits.get(operation_id)
                if (type(attempts) is not int or not 0 <= attempts <= spec.max_attempts
                    or type(effects) is not int or not 0 <= effects <= spec.max_effects):
                    raise RecoveryBlocked("original_allowance_expansion_forbidden")
                limits[operation_id] = {"max_attempts": attempts, "max_effects": effects}
            if self.ledger.read():
                raise RecoveryBlocked("original_ledger_not_empty")
            self.manifest = snapshot.manifest
            preview = self.plan(snapshot)
            request = RecoveryRequest.from_preview(preview, "original:" + snapshot.manifest.run_id)
            # The original authorization is operation-scoped. A legacy uncertain
            # downstream operation must not block an independent untouched one;
            # its own unknown effects are fenced again at admission below.
            request = dataclasses.replace(request, selections=tuple(dataclasses.replace(selection, action="execute")
                                           for selection in request.selections))
            self.ledger.append("manifest", {"manifest": json_value(snapshot.manifest),
                               "manifest_digest": digest(snapshot.manifest),
                               "baseline_revision_digest": snapshot.revision_digest,
                               "baseline_items": json_value(snapshot.items),
                               "baseline_events": json_value(tuple(e for e in snapshot.events if e.get("baseline") is True))}, owner)
            payload = {"pass_id": pass_id, "execution_id": owner.execution_id,
                       "request": request.to_dict(), "request_digest": digest(request),
                       "authorization_origin": "frozen_original_plan", "operation_limits": limits}
            self.ledger.append("pass_authorized", payload, owner)
            owner.bind_pass(pass_id)
            return self._pass(payload)

    def resume_pass(self, pass_id: str, owner) -> RecoveryPass:
        with self.ledger.transaction(owner):
            payload = self._pass_payload(pass_id)
            if self._pass_is_terminal(pass_id):
                raise RecoveryBlocked("pass_already_terminal")
            self.ledger.append("pass_resumed", {"pass_id": pass_id, "execution_id": owner.execution_id}, owner)
            owner.bind_pass(pass_id)
            return self._pass(payload, execution_id=owner.execution_id)

    def _assert_pass(self, pass_id, owner):
        owner.assert_owned()
        payload = self._pass_payload(pass_id)
        if self._pass_is_terminal(pass_id):
            raise RecoveryBlocked("pass_already_terminal")
        epochs = self.ledger.select_many(("pass_authorized", "pass_resumed"), pass_id=pass_id)
        if epochs[-1]["payload"]["execution_id"] != owner.execution_id:
            raise RecoveryBlocked("stale_execution")
        return payload

    def _operation(self, operation_id):
        for op in self._manifest().operations:
            if op.operation_id == operation_id:
                return op
        raise RecoveryBlocked("undeclared_operation")

    def begin_attempt(self, pass_id: str, operation_id: str, owner, *, action="execute", predecessor_ids=()) -> AttemptRecord:
        with self.ledger.transaction(owner):
            payload = self._assert_pass(pass_id, owner)
            operation = self._operation(operation_id)
            selected = [s for s in payload["request"]["selections"] if operation_id in s["operation_ids"]]
            if len(selected) != 1 or selected[0]["action"] in {"blocked", "wait"}:
                raise RecoveryBlocked("operation_not_selected")
            if action == "replace" and (not predecessor_ids or set(predecessor_ids) - set(selected[0]["replacement_attempt_ids"])):
                raise RecoveryBlocked("replacement_not_authorized")
            if action not in {"execute", "reconcile", "replace", "reconstruct", "parse_saved", "continue"}:
                raise RecoveryBlocked("undeclared_attempt_action")
            if selected[0]["action"] in {"reconcile", "reconcile_or_replace"} and action not in {"reconcile", "reconstruct"}:
                replacement = selected[0]["action"] == "reconcile_or_replace" and action == "replace"
                completed_reconciliations = {e["payload"]["attempt_id"] for e in self._select("attempt_finished", pass_id=pass_id)
                                            if e["payload"]["item_id"] == operation.item_id
                                            and e["payload"]["status"] == "completed"}
                proven = any(e["payload"]["action"] in {"reconcile", "replace"}
                             for completed_id in completed_reconciliations
                             for e in self._select("attempt_started", attempt_id=completed_id))
                if not replacement and (action != "continue" or not proven):
                    raise RecoveryBlocked("original_response_check_required")
            # Dependencies are evaluated by this coordinator, including baseline
            # accepted operations omitted from a parser-only selection.
            manifest_record = self._events("manifest")[0]["payload"]
            baseline_completed = {op for item in manifest_record.get("baseline_items", ())
                                  for op, status in item.get("operation_states", ()) if status == "succeeded"}
            for dependency in operation.dependencies:
                local_required = any(dependency in selection["operation_ids"]
                                     for selection in payload["request"]["selections"]) and action != "reconcile"
                completed = [e["payload"] for e in self._select("attempt_finished", operation_id=dependency)
                             if not local_required or e["payload"]["pass_id"] == pass_id]
                if action == "reconcile":
                    # A check only reopens the saved conversation of an earlier
                    # send; admit_effect refuses every non-reconciliation effect
                    # for it. That send was built on a completed dependency. A
                    # later failed replacement of the dependency does not undo
                    # that, so the newest record must not decide alone.
                    satisfied = any(row["status"] == "completed" for row in completed)
                else:
                    satisfied = bool(completed) and completed[-1]["status"] == "completed"
                if not (satisfied or (not local_required and dependency in baseline_completed)):
                    raise RecoveryBlocked("dependency_incomplete")
            attempts = [e["payload"] for e in self._select("attempt_started", operation_id=operation_id)]
            local = [a for a in attempts if a["pass_id"] == pass_id]
            attempt_limit = payload.get("operation_limits", {}).get(operation_id, {}).get("max_attempts", operation.max_attempts)
            if len(local) >= attempt_limit:
                raise AllowanceExceeded("preparation_allowance_exhausted")
            attempt = AttemptRecord(str(uuid.uuid4()), operation_id, operation.item_id, pass_id, owner.execution_id,
                                    len(attempts)+1, len(local)+1, action, tuple(predecessor_ids))
            self.ledger.append("attempt_started", {**json_value(attempt), "started_at": now(),
                               "deadline_seconds": operation.deadline_seconds}, owner)
            return attempt

    def _attempt(self, attempt_id):
        entries = [e["payload"] for e in self._select("attempt_started", attempt_id=attempt_id)]
        if len(entries) != 1:
            raise RecoveryBlocked("attempt_not_admitted")
        payload = dict(entries[0])
        resumes = [e["payload"] for e in self._select("attempt_resumed", attempt_id=attempt_id)]
        if resumes:
            payload["execution_id"] = resumes[-1]["execution_id"]
        return payload

    def _attempt_finished(self, attempt_id) -> bool:
        return bool(self._select("attempt_finished", attempt_id=attempt_id))

    def resume_attempt(self, attempt_id: str, owner) -> AttemptRecord:
        """Continue durable preparation without consuming another attempt.

        Interrupted external effects must be reconciled separately; merely
        adopting the local attempt cannot make their dispatch safe to repeat.
        """
        with self.ledger.transaction(owner):
            attempt = self._attempt(attempt_id)
            self._assert_pass(attempt["pass_id"], owner)
            if self._attempt_finished(attempt_id):
                raise RecoveryBlocked("attempt_already_finished")
            effects = [e["payload"] for e in self._select("effect_admitted", attempt_id=attempt_id)]
            if any(self._latest_certainty(effect["effect_id"]) not in {"succeeded", "failed", "confirmed_not_sent"} for effect in effects):
                raise RecoveryBlocked("attempt_has_unresolved_effect")
            self.ledger.append("attempt_resumed", {"attempt_id": attempt_id, "operation_id": attempt["operation_id"],
                               "item_id": attempt["item_id"], "pass_id": attempt["pass_id"],
                               "execution_id": owner.execution_id}, owner)
            return AttemptRecord(attempt_id, attempt["operation_id"], attempt["item_id"], attempt["pass_id"],
                                 owner.execution_id, attempt.get("lifetime_ordinal", 1), attempt.get("pass_ordinal", 1),
                                 attempt["action"], tuple(attempt.get("predecessor_ids", ())))

    def admit_effect(self, pass_id: str, operation_id: str, action: str, input_digest: str, owner, *,
                     attempt_id: str, cost=(), idempotency_key=None, replaces_attempt_ids=()) -> EffectRecord:
        with self.ledger.transaction(owner):
            pass_payload = self._assert_pass(pass_id, owner)
            operation = self._operation(operation_id)
            attempt = self._attempt(attempt_id)
            if (attempt["pass_id"] != pass_id or attempt["operation_id"] != operation_id
                or attempt["execution_id"] != owner.execution_id):
                raise RecoveryBlocked("stale_attempt")
            replaces_attempt_ids = tuple(safe_id(value) for value in replaces_attempt_ids)
            if replaces_attempt_ids and (attempt["action"] != "replace"
                or len(set(replaces_attempt_ids)) != len(replaces_attempt_ids)
                or set(replaces_attempt_ids) - set(attempt.get("predecessor_ids", ()))):
                raise RecoveryBlocked("replacement_effect_scope_mismatch")
            if attempt["action"] == "reconstruct":
                raise RecoveryBlocked("reconstruction_cannot_dispatch_effect")
            if operation.local_only:
                raise RecoveryBlocked("local_operation_cannot_admit_external_effect")
            if attempt["action"] == "reconcile" and action not in operation.reconciliation_effects:
                raise RecoveryBlocked("reconciliation_cannot_create_replacement_work")
            if input_digest != operation.input_digest:
                raise RecoveryBlocked("effect_input_binding_mismatch")
            if pass_payload.get("authorization_origin") == "frozen_original_plan" and attempt["action"] != "reconcile":
                older = [e["payload"] for e in self._select("effect_admitted", operation_id=operation_id)
                         if e["payload"]["attempt_id"] != attempt_id]
                if any(self._latest_certainty(e["effect_id"]) not in {"succeeded", "failed", "confirmed_not_sent"} for e in older):
                    raise RecoveryBlocked("original_operation_outcome_unknown")
            if self._attempt_finished(attempt_id):
                raise RecoveryBlocked("attempt_already_finished")
            cost = tuple((safe_id(k), float(v)) for k, v in (cost.items() if isinstance(cost, Mapping) else cost))
            limits = dict(operation.cost_limits)
            if len(dict(cost)) != len(cost) or any(not math.isfinite(v) or v < 0 or k not in limits for k, v in cost):
                raise RecoveryBlocked("undeclared_effect_cost")
            effects = [e["payload"] for e in self._select("effect_admitted", operation_id=operation_id)
                       if e["payload"]["pass_id"] == pass_id]
            effect_limit = pass_payload.get("operation_limits", {}).get(operation_id, {}).get("max_effects", operation.max_effects)
            if len(effects) >= effect_limit:
                raise AllowanceExceeded("effect_allowance_exhausted")
            for dimension, amount in cost:
                spent = 0
                for e in effects:
                    latest = self._latest_observation(e["effect_id"])
                    actual = dict(latest.get("actual_cost", ())) if latest is not None else {}
                    spent += max(dict(e.get("cost", ())).get(dimension, 0), actual.get(dimension, 0))
                if spent + amount > limits[dimension]:
                    raise AllowanceExceeded("cost_allowance_exhausted")
            # A dependent effect cannot be hidden behind an unresolved prior request.
            earlier = [e for e in effects if e["attempt_id"] == attempt_id]
            if any(self._latest_certainty(e["effect_id"]) not in {"succeeded", "failed", "confirmed_not_sent"} for e in earlier):
                raise RecoveryBlocked("previous_effect_unresolved")
            effect = EffectRecord(str(uuid.uuid4()), attempt_id, operation_id, pass_id, owner.execution_id,
                                  safe_id(action), input_digest, cost, idempotency_key, replaces_attempt_ids)
            self.ledger.append("effect_admitted", {**json_value(effect), "certainty": "admitted", "admitted_at": now()}, owner)
            return effect

    def mark_dispatched(self, effect_id: str, owner):
        return self.record_effect_outcome(effect_id, EffectCertainty.UNKNOWN, owner, dispatched=True)

    def record_effect_outcome(self, effect_id: str, certainty: EffectCertainty | str, owner, *,
                              receipt_reference=None, actual_cost=(), dispatched=False):
        certainty = EffectCertainty(certainty)
        with self.ledger.transaction(owner):
            admitted = [e["payload"] for e in self._select("effect_admitted", effect_id=effect_id)]
            if len(admitted) != 1:
                raise RecoveryBlocked("effect_not_admitted")
            effect = admitted[0]
            actual_cost = tuple((safe_id(k), float(v)) for k, v in (actual_cost.items() if isinstance(actual_cost, Mapping) else actual_cost))
            dimensions = dict(self._operation(effect["operation_id"]).cost_limits)
            if len(dict(actual_cost)) != len(actual_cost) or any(not math.isfinite(v) or v < 0 or k not in dimensions for k, v in actual_cost):
                raise RecoveryBlocked("invalid_actual_effect_cost")
            self._assert_pass(effect["pass_id"], owner)
            if effect["execution_id"] != owner.execution_id:
                raise RecoveryBlocked("stale_effect_callback")
            prior = [e["payload"] for e in self._select("effect_observed", effect_id=effect_id)]
            if prior and prior[-1]["certainty"] in {"succeeded", "confirmed_not_sent", "failed"}:
                raise RecoveryBlocked("effect_already_terminal")
            if prior and any(p["certainty"] == "unknown" for p in prior) and certainty == EffectCertainty.CONFIRMED_NOT_SENT:
                raise RecoveryBlocked("unknown_effect_cannot_be_rewritten_unsent")
            cumulative_cost = dict(actual_cost)
            for observation in prior:
                for dimension, amount in observation.get("actual_cost", ()):
                    cumulative_cost[dimension] = max(cumulative_cost.get(dimension, 0), amount)
            actual_cost = tuple(sorted(cumulative_cost.items()))
            self.ledger.append("effect_observed", {"effect_id": effect_id, "attempt_id": effect["attempt_id"],
                               "operation_id": effect["operation_id"], "pass_id": effect["pass_id"],
                               "certainty": certainty.value, "receipt_reference": receipt_reference,
                               "actual_cost": json_value(actual_cost), "dispatched": bool(dispatched)}, owner)

    def resolve_original_effect(self, effect_id: str, *, reconciliation_attempt_id: str,
                                original_attempt_token: str, owner, receipt_reference=None):
        """Record verified correspondence through a currently admitted check.

        This is not a callback from the original executor and cannot admit any
        replacement. Its immutable linkage explains why original uncertainty
        became known without rewriting the historical attempt or its costs.
        """
        with self.ledger.transaction(owner):
            reconciliation = self._attempt(reconciliation_attempt_id)
            self._assert_pass(reconciliation["pass_id"], owner)
            if (reconciliation["execution_id"] != owner.execution_id or reconciliation["action"] != "reconcile"
                or original_attempt_token not in reconciliation.get("predecessor_ids", ())):
                raise RecoveryBlocked("original_effect_reconciliation_not_authorized")
            if self._attempt_finished(reconciliation_attempt_id):
                raise RecoveryBlocked("reconciliation_attempt_already_finished")
            admitted = [e["payload"] for e in self._select("effect_admitted", effect_id=effect_id)]
            if len(admitted) != 1 or admitted[0]["operation_id"] != reconciliation["operation_id"]:
                raise RecoveryBlocked("original_effect_correspondence_mismatch")
            effect = admitted[0]
            prior = [e["payload"] for e in self._select("effect_observed", effect_id=effect_id)]
            if prior and prior[-1]["certainty"] == "succeeded" and any(
                row.get("original_attempt_token") == original_attempt_token and row.get("reconciliation_attempt_id")
                for row in prior):
                return prior[-1]
            if prior and prior[-1]["certainty"] in {"confirmed_not_sent", "failed"}:
                raise RecoveryBlocked("original_effect_already_terminal")
            value = {"effect_id": effect_id, "attempt_id": effect["attempt_id"],
                     "operation_id": effect["operation_id"], "pass_id": effect["pass_id"], "certainty": "succeeded",
                     "actual_cost": prior[-1].get("actual_cost", ()) if prior else (),
                     "receipt_reference": json_value(receipt_reference), "reconciliation_attempt_id": reconciliation_attempt_id,
                     "reconciliation_pass_id": reconciliation["pass_id"], "original_attempt_token": safe_id(original_attempt_token)}
            self.ledger.append("effect_observed", value, owner)
            return value

    def checkpoint(self, attempt_id: str, reference, owner):
        with self.ledger.transaction(owner):
            attempt = self._attempt(attempt_id)
            self._assert_pass(attempt["pass_id"], owner)
            if attempt["execution_id"] != owner.execution_id:
                raise RecoveryBlocked("stale_attempt_checkpoint")
            self.ledger.append("operation_reference", {"attempt_id": attempt_id, "pass_id": attempt["pass_id"],
                               "operation_id": attempt["operation_id"], "reference": json_value(reference)}, owner)

    def finish_attempt(self, attempt_id: str, observation: StepObservation | str, owner, *, reason_code="", output=None):
        if isinstance(observation, str):
            observation = StepObservation(observation, reason_code, output)
        with self.ledger.transaction(owner):
            attempt = self._attempt(attempt_id)
            self._assert_pass(attempt["pass_id"], owner)
            if attempt["execution_id"] != owner.execution_id:
                raise RecoveryBlocked("stale_attempt_callback")
            if self._attempt_finished(attempt_id):
                raise RecoveryBlocked("attempt_already_finished")
            self.ledger.append("attempt_finished", {"attempt_id": attempt_id, "operation_id": attempt["operation_id"],
                               "item_id": attempt["item_id"], "pass_id": attempt["pass_id"],
                               **json_value(observation)}, owner)
            if observation.status != "completed":
                self.record_failure(attempt["pass_id"], attempt["item_id"], observation.reason_code or "operation_failed",
                                    owner, checked_attempt_ids=observation.checked_attempt_ids, attempt_id=attempt_id,
                                    operation_id=attempt["operation_id"])

    def record_failure(self, pass_id: str, item_id: str, reason_code: str, owner, *, checked_attempt_ids=(),
                       attempt_id=None, operation_id=None):
        with self.ledger.transaction(owner):
            payload = self._assert_pass(pass_id, owner)
            selected = [s for s in payload["request"]["selections"] if s["item_id"] == item_id]
            if not selected:
                raise RecoveryBlocked("failure_item_not_selected")
            safe_id(reason_code)
            self.ledger.append("failure", {"pass_id": pass_id, "item_id": item_id, "reason_code": reason_code,
                               "attempt_id": attempt_id, "operation_id": operation_id,
                               "checked_attempt_ids": tuple(checked_attempt_ids), "checked_at": now()}, owner)

    def commit_revision(self, pass_id: str, item_id: str, output: Any, owner, *, lineage=(),
                        captured_at=None, recovered_at=None, output_reference=None, accepted=True, **metadata):
        with self.ledger.transaction(owner):
            payload = self._assert_pass(pass_id, owner)
            if item_id not in {s["item_id"] for s in payload["request"]["selections"]}:
                raise RecoveryBlocked("result_item_not_selected")
            current = self.ledger.effective_results().get(item_id)
            if current and current.get("accepted", True):
                if current["output_digest"] == digest(output):
                    return current
                raise RecoveryBlocked("accepted_result_retained")
            revision_id = str(uuid.uuid4())
            revision = {"revision_id": revision_id, "item_id": item_id, "pass_id": pass_id,
                        "accepted": bool(accepted), "parent_revision_id": current["revision_id"] if current else None,
                        "prior_revision_id": current["revision_id"] if current else None,
                        "output": json_value(output), "output_reference": json_value(output if output_reference is None else output_reference),
                        "output_digest": digest(output), "lineage": json_value(lineage),
                        "captured_at": captured_at, "recovered_at": recovered_at,
                        "workflow_version": self._manifest().workflow_version,
                        "policy_version": self._manifest().policy_version,
                        "parser_repairs": payload["request"].get("parser_repairs", ()), "metadata": json_value(metadata)}
            self.ledger.append("revision_prepared", revision, owner)
            # Separate durable commit is deliberate: incomplete prepared revisions are not effective.
            self.ledger.append("revision_committed", revision, owner)
            return revision

    def recover_prepared_revision(self, revision_id: str, owner, *, validator):
        with self.ledger.transaction(owner):
            found = [r for r in self.ledger.incomplete_revisions() if r["revision_id"] == revision_id]
            if len(found) != 1 or not validator(found[0]):
                raise RecoveryBlocked("prepared_revision_not_verified")
            revision = found[0]
            current = self.ledger.effective_results().get(revision["item_id"])
            expected_parent = current["revision_id"] if current else None
            if revision.get("parent_revision_id") != expected_parent or digest(revision["output"]) != revision["output_digest"]:
                raise RecoveryBlocked("prepared_revision_parent_changed")
            self.ledger.append("revision_committed", revision, owner)
            self.ledger.effective_results()
            return found[0]

    def complete_pass(self, pass_id: str, owner, *, cancelled=False):
        with self.ledger.transaction(owner):
            payload = self._assert_pass(pass_id, owner)
            terminal = {e["payload"]["item_id"]
                        for e in self.ledger.select_many(("failure", "revision_committed"), pass_id=pass_id)}
            for selection in payload["request"]["selections"]:
                if selection["item_id"] not in terminal:
                    self.record_failure(pass_id, selection["item_id"], "pass_cancelled" if cancelled else "selected_item_unfinished",
                                        owner)
            self.ledger.append("pass_cancelled" if cancelled else "pass_completed", {"pass_id": pass_id}, owner)

    def cancel_pass(self, pass_id: str, owner):
        self.complete_pass(pass_id, owner, cancelled=True)

    def record_resource(self, resource_id: str, status: str, owner, *, attempt_id=None, reference=None):
        if status not in {"owned", "cleanup_pending", "released", "blocked"}:
            raise ValueError("invalid resource state")
        self.ledger.append("resource_observed", {"resource_id": safe_id(resource_id), "status": status,
                           "attempt_id": attempt_id, "reference": json_value(reference)}, owner)
