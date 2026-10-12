"""Executable adapter seam for finite declarative workflows.

Adapters receive admitted effect and checkpoint interfaces; local processing
needs neither a process-specific transport nor a correlation identifier.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from typing import Any, Mapping, Protocol

from .contracts import (AllowanceExceeded, EffectCertainty, ItemState, RecoveryBlocked, RecoveryIntegrityError, RecoverySnapshot, RecoveryRequest, StepObservation,
                        digest, json_value)
from .coordinator import RecoveryCoordinator
from .ledger import now
from .readout import build_failure_readout


def active_items(events, worker):
    """Items still executing in the verified owner's current pass.

    Completing one operation does not finish its item. A later failure or pass
    completion does, even while the worker remains alive during cleanup.
    """
    if (worker is None or worker.status != "alive" or worker.ownership_status != "owned"
            or not worker.execution_id):
        return set()
    active = {}
    for event in events:
        payload, kind = event["payload"], event["kind"]
        if event.get("execution_id", payload.get("execution_id")) != worker.execution_id:
            continue
        if kind in {"attempt_started", "attempt_resumed"}:
            active[payload["item_id"]] = payload["pass_id"]
        elif kind == "failure" or (kind == "attempt_finished" and payload["status"] != "completed"):
            active.pop(payload["item_id"], None)
        elif kind in {"pass_completed", "pass_cancelled"}:
            active = {item: pass_id for item, pass_id in active.items() if pass_id != payload["pass_id"]}
    return set(active)


class OperationAdapter(Protocol):
    async def execute(self, operation, context) -> StepObservation: ...
    async def reconcile(self, operation, prior_attempt_ids, context) -> StepObservation: ...
    def validate(self, operation, observation: StepObservation) -> bool: ...


class AttemptContext:
    def __init__(self, coordinator, owner, attempt, operation, *, inputs=None):
        self.coordinator, self.owner, self.attempt, self.operation = coordinator, owner, attempt, operation
        self.inputs = inputs or {}
        self.deadline = time.monotonic() + operation.deadline_seconds

    @property
    def references(self):
        predecessors = set(self.attempt.predecessor_ids)
        return tuple(event["payload"]["reference"] for event in self.coordinator._events("operation_reference")
                     if event["payload"]["attempt_id"] in predecessors
                     and event["payload"]["operation_id"] == self.operation.operation_id)

    @staticmethod
    def _persist(callback, *args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except OSError as exc:
            raise RecoveryIntegrityError("recovery_persistence_failed") from exc

    def checkpoint(self, reference):
        self._persist(self.coordinator.checkpoint, self.attempt.attempt_id, reference, self.owner)

    def check(self):
        self.coordinator._assert_pass(self.attempt.pass_id, self.owner)
        if time.monotonic() >= self.deadline:
            raise RecoveryBlocked("operation_deadline")

    async def effect(self, action: str, callback, *, cost=(), idempotency_key=None, replaces_attempt_ids=()):
        self.check()
        effect = self._persist(self.coordinator.admit_effect, self.attempt.pass_id, self.operation.operation_id, action,
                    self.operation.input_digest, self.owner, attempt_id=self.attempt.attempt_id,
                    cost=cost, idempotency_key=idempotency_key, replaces_attempt_ids=replaces_attempt_ids)
        # Admission and unknown dispatch checkpoint must be durable BEFORE callback.
        self._persist(self.coordinator.mark_dispatched, effect.effect_id, self.owner)
        result = callback()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, max(0.001, self.deadline - time.monotonic()))
        self._persist(self.coordinator.record_effect_outcome, effect.effect_id, EffectCertainty.SUCCEEDED, self.owner)
        return result


class RecoveryService:
    def __init__(self, coordinator: RecoveryCoordinator, adapters: Mapping[tuple[str, str], OperationAdapter], *, compatible_repairs=()):
        self.coordinator = coordinator
        self.adapters = dict(adapters)
        self.compatible_repairs = frozenset(compatible_repairs)

    def snapshot(self, manifest, *, baseline: RecoverySnapshot | None = None, worker=None, resources=()):
        events = self.coordinator.ledger.normalized_events()
        states = {i.item_id: ItemState(i.item_id) for i in manifest.items}
        if baseline:
            states.update({s.item_id: s for s in baseline.items})
        effective = self.coordinator.ledger.effective_results(events)
        operation_states = {i.item_id: dict(states[i.item_id].operation_states) for i in manifest.items}
        checked = {i.item_id: set(states[i.item_id].checked_attempt_ids) for i in manifest.items}
        unknown = {i.item_id: set(states[i.item_id].unresolved_attempt_ids) for i in manifest.items}
        attempt_items = {}
        effects = {}
        superseded = set()
        reconciled = set()
        for event in events:
            payload, kind = event["payload"], event["kind"]
            if kind == "attempt_started":
                attempt_items[payload["attempt_id"]] = payload["item_id"]
                operation_states[payload["item_id"]][payload["operation_id"]] = "active"
                states[payload["item_id"]] = dataclasses.replace(states[payload["item_id"]],
                    status="failed", reason_code="execution_incomplete")
            elif kind == "attempt_resumed":
                item_id = payload["item_id"]
                states[item_id] = dataclasses.replace(states[item_id], status="failed", reason_code="execution_incomplete")
            elif kind == "attempt_finished":
                item_id = payload["item_id"]
                operation_states[item_id][payload["operation_id"]] = "succeeded" if payload["status"] == "completed" else "failed"
                states[item_id] = dataclasses.replace(states[item_id], status="failed", reason_code=payload.get("reason_code", ""))
            elif kind == "effect_admitted":
                effects[payload["effect_id"]] = (payload["attempt_id"], "admitted")
                superseded.update(payload.get("replaces_attempt_ids", ()))
            elif kind == "effect_observed" and payload["effect_id"] in effects:
                effects[payload["effect_id"]] = (effects[payload["effect_id"]][0], payload["certainty"])
                if payload.get("reconciliation_attempt_id") and payload["certainty"] == "succeeded":
                    reconciled.add(payload["original_attempt_token"])
                    reconciled.add(effects[payload["effect_id"]][0])
            elif kind == "failure":
                item_id = payload["item_id"]
                states[item_id] = dataclasses.replace(states[item_id], status="failed", reason_code=payload["reason_code"])
                checked[item_id].update(payload.get("checked_attempt_ids", ()))
        for attempt_id, certainty in effects.values():
            if certainty in {"admitted", "unknown"}:
                unknown[attempt_items[attempt_id]].add(attempt_id)
        for item in manifest.items:
            states[item.item_id] = dataclasses.replace(states[item.item_id],
                status="succeeded" if item.item_id in effective and effective[item.item_id].get("accepted", True) else states[item.item_id].status,
                checked_attempt_ids=tuple(sorted(checked[item.item_id])), unresolved_attempt_ids=tuple(sorted(unknown[item.item_id] - superseded - reconciled)),
                historical_unresolved_attempt_ids=tuple(sorted((set(states[item.item_id].historical_unresolved_attempt_ids) | (unknown[item.item_id] & superseded)) - reconciled)),
                operation_states=tuple(operation_states[item.item_id].items()))
        running_items = active_items(events, worker)
        for item_id, state in states.items():
            if state.status in {"succeeded", "blocked"}:
                continue
            if item_id in running_items:
                states[item_id] = dataclasses.replace(state, status="active", reason_code="operation_in_progress")
            elif state.status == "active":
                states[item_id] = dataclasses.replace(state, status="failed", reason_code="execution_incomplete")
        return RecoverySnapshot(manifest, digest({"baseline": baseline.revision_digest if baseline else manifest.scope_digest,
                                "ledger": next((e["checksum"] for e in reversed(events) if e.get("checksum")), "")}), tuple(states[i.item_id] for i in manifest.items),
                                events=events, worker=worker, resources=tuple(resources), sampled_at=now())

    def readout(self, snapshot):
        return build_failure_readout(snapshot)

    async def execute(self, request, snapshot, owner):
        for repair in request.parser_repairs:
            if (repair.operation_id, repair.from_adapter_version, repair.to_adapter_version, repair.compatibility_id) not in self.compatible_repairs:
                raise RecoveryBlocked("parser_repair_not_registered")
        recovery_pass = self.coordinator.authorize(request, snapshot, owner)
        if recovery_pass.duplicate:
            return recovery_pass
        return await self.execute_pass(recovery_pass, snapshot, owner, parser_repairs=request.parser_repairs)

    @staticmethod
    def _validated(adapter, operation, observation):
        if not isinstance(observation, StepObservation):
            return StepObservation("failed", "invalid_adapter_observation")
        if observation.status == "completed":
            try:
                valid = adapter.validate(operation, observation)
            except Exception:
                return StepObservation("failed", "output_validation_unavailable")
            if not valid:
                return StepObservation("failed", "output_validation_failed")
        return observation

    def _authorized_execution(self, recovery_pass, snapshot, owner, parser_repairs):
        # A caller-supplied pass object or snapshot cannot substitute repair
        # authority. Read the verified journal under the current ownership.
        with self.coordinator.ledger.transaction(owner):
            payload = self.coordinator._assert_pass(recovery_pass.pass_id, owner)
            request = RecoveryRequest.from_dict(payload["request"])
            saved = request.parser_repairs
            if parser_repairs is not None and tuple(parser_repairs) != saved:
                raise RecoveryBlocked("parser_repair_binding_mismatch")
            for repair in saved:
                if (repair.operation_id, repair.from_adapter_version, repair.to_adapter_version,
                    repair.compatibility_id) not in self.compatible_repairs:
                    raise RecoveryBlocked("parser_repair_not_registered")
            manifest = self.coordinator._manifest()
            if digest(snapshot.manifest) != request.manifest_digest or digest(manifest) != request.manifest_digest:
                raise RecoveryIntegrityError("frozen_manifest_changed")
            return request, manifest

    async def _observe(self, adapter, operation, context, *, reconcile=(), replacement=False):
        try:
            if context.attempt.action == "reconcile":
                method = getattr(adapter, "reconcile", None)
                observation = (await asyncio.wait_for(method(operation, reconcile, context), operation.deadline_seconds)
                               if method else StepObservation("unsupported", "original_response_unavailable",
                                                              checked_attempt_ids=reconcile))
            else:
                observation = await asyncio.wait_for(adapter.execute(operation, context), operation.deadline_seconds)
        except TimeoutError:
            observation = StepObservation("failed", "operation_deadline", checked_attempt_ids=reconcile)
        except RecoveryIntegrityError:
            raise
        except (RecoveryBlocked, AllowanceExceeded) as exc:
            observation = StepObservation("blocked", str(exc))
        except Exception:
            # Raw transport exception text never enters shared diagnostics.
            observation = StepObservation("failed", "replacement_operation_failed" if replacement else "adapter_operation_failed",
                                          checked_attempt_ids=reconcile)
        observation = self._validated(adapter, operation, observation)
        if context.attempt.action == "reconcile" and observation.status != "completed":
            observation = dataclasses.replace(observation, reason_code=observation.reason_code or "original_response_not_recovered",
                                               checked_attempt_ids=reconcile)
        return observation

    @staticmethod
    def _conditional_observation(observation, operation, selected, operations, depended_on):
        if observation.status != "completed":
            return observation
        # Only direct same-item leaves may disappear. Check the complete frozen
        # graph, including dependents not selected for this pass.
        if any(child not in selected.operation_ids or child not in operations
               or operations[child].item_id != operation.item_id
               or operation.operation_id not in operations[child].dependencies
               or child in depended_on for child in observation.conditional_not_required):
            return StepObservation("failed", "invalid_conditional_completion")
        return observation

    async def execute_pass(self, recovery_pass, snapshot, owner, *, parser_repairs=None):
        request, manifest = self._authorized_execution(recovery_pass, snapshot, owner, parser_repairs)
        pass_id = recovery_pass.pass_id
        operations = {op.operation_id: op for op in manifest.operations}
        repairs = {r.operation_id: r for r in request.parser_repairs}
        selections = {selection.item_id: selection for selection in request.selections}
        selected_operations = {op for selection in request.selections for op in selection.operation_ids}
        depended_on = {dependency for op in manifest.operations for dependency in op.dependencies}
        blocked_resources = {k for k, v in snapshot.resources if v not in {"available", "released"}}
        states = {state.item_id: state for state in snapshot.items}
        outputs = {}
        for event in snapshot.events:
            if event["kind"] == "attempt_finished":
                payload = event["payload"]
                if payload["status"] == "completed":
                    outputs[payload["operation_id"]] = payload.get("output")
                else:
                    outputs.pop(payload["operation_id"], None)
        # A selected dependency must produce this pass's output. Old validated
        # values remain reusable only for operations omitted by authorization.
        for operation_id in selected_operations:
            outputs.pop(operation_id, None)
        failed_items = set()
        not_required = set()
        for selection in request.selections:
            if selection.action in {"wait", "blocked"}:
                self.coordinator.record_failure(pass_id, selection.item_id,
                    states[selection.item_id].reason_code or "item_blocked", owner)
                failed_items.add(selection.item_id)

        # Manifest order is globally topological; item/display order need not be.
        for declared in manifest.operations:
            operation_id = declared.operation_id
            if operation_id not in selected_operations:
                continue
            selected = selections[declared.item_id]
            if selected.action in {"wait", "blocked"}:
                continue
            if operation_id in not_required:
                outputs[operation_id] = {"conditional_not_required": True}
                continue
            operation = declared
            if operation_id in repairs:
                repair = repairs[operation_id]
                operation = dataclasses.replace(operation, adapter_version=repair.to_adapter_version,
                                                parser_version=repair.parser_version)
            reason = None
            if any(resource in blocked_resources for resource in operation.resource_scopes):
                reason = "resource_blocked"
            elif any(dependency not in outputs for dependency in operation.dependencies):
                reason = "dependency_incomplete"
            adapter = self.adapters.get((operation.adapter_id, operation.adapter_version))
            if reason is None and adapter is None:
                reason = "adapter_version_unavailable"
            if reason:
                self.coordinator.record_failure(pass_id, selected.item_id, reason, owner, operation_id=operation_id)
                failed_items.add(selected.item_id)
                continue
            state = states[selected.item_id]
            action = "reconcile" if selected.action in {"reconcile", "reconcile_or_replace"} else selected.action
            try:
                attempt = self.coordinator.begin_attempt(pass_id, operation_id, owner, action=action,
                    predecessor_ids=state.unresolved_attempt_ids if action == "reconcile" else ())
            except (RecoveryBlocked, AllowanceExceeded) as exc:
                self.coordinator.record_failure(pass_id, selected.item_id, str(exc), owner, operation_id=operation_id)
                failed_items.add(selected.item_id)
                continue
            context = AttemptContext(self.coordinator, owner, attempt, operation, inputs=dict(outputs))
            observation = await self._observe(adapter, operation, context,
                reconcile=state.unresolved_attempt_ids if action == "reconcile" else ())
            observation = self._conditional_observation(observation, operation, selected, operations, depended_on)
            self.coordinator.finish_attempt(attempt.attempt_id, observation, owner)
            if observation.status not in {"completed", "blocked"} and selected.action == "reconcile_or_replace":
                # Only attempts checked BEFORE this pass have replacement authority.
                try:
                    replacement = self.coordinator.begin_attempt(pass_id, operation_id, owner, action="replace",
                        predecessor_ids=selected.replacement_attempt_ids)
                except (RecoveryBlocked, AllowanceExceeded) as exc:
                    self.coordinator.record_failure(pass_id, selected.item_id, str(exc), owner, operation_id=operation_id)
                    failed_items.add(selected.item_id)
                    continue
                context = AttemptContext(self.coordinator, owner, replacement, operation, inputs=dict(outputs))
                observation = await self._observe(adapter, operation, context, replacement=True)
                observation = self._conditional_observation(observation, operation, selected, operations, depended_on)
                self.coordinator.finish_attempt(replacement.attempt_id, observation, owner)
            if observation.status != "completed":
                failed_items.add(selected.item_id)
                continue
            not_required.update(observation.conditional_not_required)
            outputs[operation_id] = observation.output

        for selected in request.selections:
            if selected.item_id in failed_items:
                continue
            item = next(item for item in manifest.items if item.item_id == selected.item_id)
            if any(operation_id not in outputs for operation_id in item.operation_ids):
                self.coordinator.record_failure(pass_id, selected.item_id, "accepted_evidence_unavailable", owner)
                continue
            self.coordinator.commit_revision(pass_id, selected.item_id,
                {operation_id: outputs[operation_id] for operation_id in item.operation_ids}, owner,
                lineage=tuple(item.operation_ids), recovered_at=now())
        self.coordinator.complete_pass(pass_id, owner)
        return recovery_pass
