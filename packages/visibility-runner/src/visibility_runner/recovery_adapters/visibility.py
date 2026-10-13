"""Visibility workflow binding for the shared recovery coordinator.

This adapter interprets existing measurement evidence. The coordinator remains
responsible for authorization, finite allowances, uncertainty and revisions.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
import re
from types import MappingProxyType, SimpleNamespace
from typing import Mapping
from urllib.parse import quote

from ..errors import AmbiguousSendError, IntegrityError
from ..planning.hashing import canonical_sha256, json_value
from ..result_acceptance import accepted_result
from ..recovery.api import (
    AllowanceExceeded, RecoveryBlocked,
    RecoveryCoordinator, RecoveryLedger, RecoveryRequest, RecoverySnapshot,
    RunManifest, WorkItem, OperationSpec, ItemState, WorkerObservation,
    ProcessObservation, inspect_process,
)
from ..storage.send_attempts import SendAttemptState
from ..recovery.service import active_items
from ..websites.chat_actions import PROVIDER_PREPARATION_TIMEOUT_SECONDS, RECOVERY_INSPECTION_TIMEOUT_SECONDS

ADAPTER_VERSION = "1"
WORKFLOW_VERSION = "visibility-two-turn-1"

# Coordinator refusal codes that concern one item only. Codes about the pass
# or its ownership (stale_execution, pass_already_terminal,
# pass_not_authorized) are deliberately absent: those still end the packet.
ITEM_SCOPED_REFUSALS = frozenset({
    "dependency_incomplete", "operation_not_selected",
    "replacement_not_authorized", "original_response_check_required",
    "preparation_allowance_exhausted", "effect_allowance_exhausted",
    "cost_allowance_exhausted",
})


def _events(run_dir):
    return RecoveryLedger(run_dir).read()


def _payload(event):
    return event.get("payload", {})



def _unreleased_attachments(events, manifest):
    latest = {}
    for event in events:
        if event.get("kind") == "resource_observed":
            row = _payload(event)
            latest[row["resource_id"]] = row
    allowed_scopes = {scope for operation in manifest.operations for scope in operation.resource_scopes}
    attachments = []
    for row in latest.values():
        if row["status"] == "released":
            continue
        reference = row.get("reference")
        named_attachment = row["resource_id"].startswith("attachment:")
        if not isinstance(reference, Mapping):
            if named_attachment:
                raise IntegrityError("attachment_resource_reference_invalid")
            continue
        if reference.get("resource_kind") != "local_attachment":
            if named_attachment:
                raise IntegrityError("attachment_resource_reference_invalid")
            continue
        scopes = reference.get("resource_scopes")
        if (not isinstance(scopes, (tuple, list)) or not scopes
                or any(not isinstance(scope, str) or not scope or scope not in allowed_scopes for scope in scopes)):
            raise IntegrityError("attachment_resource_scopes_invalid")
        attachments.append(row)
    return tuple(attachments)


def _attachment_owner_observation(row):
    reference = row.get("reference") or {}
    identity = reference.get("process_identity")
    execution_id = reference.get("execution_id")
    if (not isinstance(identity, Mapping) or not isinstance(execution_id, str) or not execution_id
            or row.get("resource_id") != "attachment:" + execution_id
            or identity.get("execution_id") != execution_id):
        return ProcessObservation("unknown", "attachment_owner_identity_invalid",
                                  datetime.now(timezone.utc).isoformat())
    return inspect_process(identity)


def effective_results(
    repository, run_id, originals, *, observation_ids=None,
    plan=None, observation_index=None, evidence_reads=None, attempt_records=None,
    recovery_ledger=None,
):
    root = repository.run_dir(run_id)
    selected = None if observation_ids is None else set(observation_ids)
    current = {str(row["observation_id"]): row for row in originals}
    ledger = recovery_ledger or RecoveryLedger(root)
    revisions = ledger.effective_results()
    if revisions and plan is None:
        from ..storage.measurement_files import _observation_index
        plan = repository.read_plan(run_id)
        observation_index = _observation_index(plan)
    for revision in revisions.values():
        item_id = str(revision["item_id"])
        if selected is not None and item_id not in selected:
            continue
        ref = revision.get("output_reference")
        if not isinstance(ref, Mapping):
            continue
        import json
        result = json.loads(repository.read_evidence(
            run_id, ref,
            _read_bytes=evidence_reads.read_bytes if evidence_reads else None,
        ))
        result = repository._canonical_observation_result(
            run_id, item_id, result, plan=plan, observation_index=observation_index,
            _evidence_reads=evidence_reads, _attempt_records=attempt_records,
        )
        result["recovery_lineage"] = {
            "pass_id": revision.get("pass_id"),
            "revision_id": revision.get("revision_id"),
            "prior_revision_id": revision.get("prior_revision_id"),
        }
        current[item_id] = result
    return list(current.values())


def collection_metadata(value, records, accepted_attempt_ids):
    """Project recorded acquisition/retrieval times without inventing provider times."""
    source_id=accepted_attempt_ids.get("identification_source") or accepted_attempt_ids.get("copilot_answer") or accepted_attempt_ids.get("google_search") or accepted_attempt_ids.get("measurement_turn_1")
    by_id={record.fence_id:record for record in records}
    source=by_id.get(source_id)
    if source is None:
        return {}
    chain=[source]
    seen={source.fence_id}
    while chain[-1].recovered_from and chain[-1].recovered_from in by_id:
        original=by_id[chain[-1].recovered_from]
        if original.fence_id in seen:
            raise IntegrityError('recovered_attempt_lineage_cycle')
        seen.add(original.fence_id)
        chain.append(original)
    original=chain[-1]
    capture_at=original.response_captured_at or source.response_captured_at
    retrieved_at=next((record.response_captured_at for record,parent in zip(chain,chain[1:])
                       if record.response_captured_at and record.response_captured_at!=parent.response_captured_at),None)
    historical=sorted((record for record in records if record.observation_id==source.observation_id
                       and record.action in {'google_search','measurement_turn_1','copilot_answer','identification_source'}
                       and not record.recovered_from and record.response_captured_at),
                      key=lambda record:record.response_captured_at)
    lineage=value.get('recovery_lineage') or {}
    return {key:item for key,item in {
        'captured_at':capture_at,
        'original_captured_at':historical[0].response_captured_at if historical else None,
        'retrieved_at':retrieved_at,
        'recovered_at':retrieved_at,
        'request_sent_at':original.send_started_at,
        'source_attempt_id':original.fence_id,
        'recovery_pass_id':lineage.get('pass_id'),
        'result_revision_id':lineage.get('revision_id'),
        'prior_revision_id':lineage.get('prior_revision_id'),
    }.items() if item is not None}


def manifest_for(plan, *, execution_policy=None):
    operations, items = [], []
    policy = plan.get("retry_policy") or {}
    max_attempts = int(policy.get("max_attempts", 2)) if policy.get("enabled", True) else 1
    max_attempts = max(1, max_attempts)
    for row in plan["observation_plan"]["observations"]:
        item_id = str(row["observation_id"])
        platform = str(row["platform"])
        profile = next((p for p in plan["packets"] if item_id in p["observation_ids"] and p.get("kind", "measurement") in {"measurement", "source_parser"}), {})
        resource = f"{profile.get('profile_id', row.get('profile_id', 'unknown'))}:{platform}"
        direct = row.get("client_research_capability") == "client_research.identification.v1"
        actions = ("identification_source", "identification_judge") if direct else ("copilot_answer", "gemini_parse") if platform == "github_copilot" else ("google_search", "gaio_gemini_extractor") if platform == "google_ai_overview" else ("measurement_turn_1", "same_chat_turn_2")
        source_config = row.get("github_copilot") or {}
        ids = tuple(f"{item_id}:{action}" for action in actions)
        for index, (operation_id, action) in enumerate(zip(ids, actions)):
            timeout_key = "google_ai_overview_seconds" if action=="google_search" else "gaio_gemini_parser_seconds" if action=="gaio_gemini_extractor" else f"{platform}_turn_{index+1}_seconds"
            deadline = float((policy.get("response_timeouts") or {}).get(timeout_key,240)) + float(policy.get("late_response_grace_seconds",30))
            operation_max_attempts = max_attempts
            operation_resource = resource.rsplit(":",1)[0]+":gemini" if action=="gaio_gemini_extractor" else resource
            if direct:
                operation_resource = f"{row['judge_profile_id']}:{row['judge_provider']}" if action == "identification_judge" else resource
                deadline = float((policy.get("response_timeouts") or {}).get(f"{row['judge_provider'] if action == 'identification_judge' else platform}_turn_1_seconds",240)) + float(policy.get("late_response_grace_seconds",30))
            if platform == "github_copilot":
                stage = "source" if action == "copilot_answer" else "parser"
                operation_max_attempts = int(source_config["max_" + stage + "_attempts"])
                deadline = float(source_config[stage + "_timeout_seconds"]) + float(source_config["reconciliation_grace_seconds"])
                operation_resource = "github-account:" + source_config["account_ref"] if action == "copilot_answer" else resource.rsplit(":",1)[0] + ":gemini"
            operations.append(OperationSpec(
                operation_id=operation_id, item_id=item_id,
                adapter_id=f"visibility.{action}", adapter_version=ADAPTER_VERSION,
                input_digest=canonical_sha256({"observation": row, "action": action}),
                dependencies=ids[:index], resource_scopes=(operation_resource,),
                max_attempts=operation_max_attempts + 1, max_effects=operation_max_attempts + 3,
                deadline_seconds=deadline, restart_unit=operation_id if direct or platform in {"google_ai_overview", "github_copilot"} else item_id,
                parser_version="ordered-names-1", validator_version="visibility-1",
                cost_limits=(("submissions", operation_max_attempts),),
            ))
        items.append(WorkItem(item_id, ids, output_contract="observation-result"))
    scope=canonical_sha256({'plan_sha256':plan['plan_sha256'],'execution_policy':execution_policy}) if execution_policy is not None else str(plan['plan_sha256'])
    return RunManifest(str(plan["run_id"]), scope, "visibility-source-parser-1" if any(row["platform"] == "github_copilot" for row in plan["observation_plan"]["observations"]) else WORKFLOW_VERSION,
                       "explicit-refetch-1", tuple(items), tuple(operations))


def snapshot_for(repository, run_id, *, worker_observation=None, verified=None, runtime=None, verification_store=None, verification_runtime=None):
    # One request owns this ledger view. Reuse its verified prefix through
    # result projection and normalization, but never cache between API calls.
    ledger = RecoveryLedger(repository.run_dir(run_id), watch_changes=False)
    if verified is None:
        verified = repository.read_verified_snapshot(run_id, _recovery_ledger=ledger)
    from .execution_policy import read_policy
    policy=read_policy(repository,run_id,verified.plan,runtime=runtime,state=verified.runner_state)
    manifest = manifest_for(verified.plan,execution_policy=policy)
    ledger_events = ledger.read()
    results = {r["observation_id"]: r for r in verified.observation_results}
    checked = {}
    failures = {}
    for event in ledger_events:
        if event.get("kind") == "failure":
            row = _payload(event)
            checked.setdefault(row.get("item_id"), set()).update(row.get("checked_attempt_ids", ()))
            failures[row.get("item_id")]=str(row.get("reason_code") or "measurement_incomplete")
    # Packet faults retain the causal reason when pass settlement later adds
    # a generic cancellation/dependency failure for the same unfinished item.
    packet_failures = {}
    for packet in verified.plan["packets"]:
        for issue in verified.group_progress[packet["packet_id"]].get("issues", ()):
            for item_id in packet["observation_ids"]:
                if issue.get("observation_id") in {None, item_id}:
                    detail = issue.get("detail")
                    access = detail.get("access_block") if isinstance(detail, Mapping) else None
                    access_reason = access.get("reason") if isinstance(access, Mapping) else None
                    packet_failures[item_id] = (access_reason if access_reason in {
                        "authentication_required", "human_verification_required",
                        "unusual_activity", "usage_limit", "provider_access_blocked",
                    } else str(issue.get("code") or "measurement_incomplete"))
    failures.update(packet_failures)
    # The effect ledger is authoritative even if a crash occurred between
    # submission admission and the legacy send-fence transition.
    fence_by_attempt={_payload(event)['attempt_id']:_payload(event)['fence_id']
                      for event in ledger_events if event.get('kind')=='visibility_attempt'}
    observed_effects={_payload(event)['effect_id']:_payload(event)['certainty']
                      for event in ledger_events if event.get('kind')=='effect_observed'}
    resolved={_payload(event)['original_attempt_token'] for event in ledger_events
              if event.get('kind')=='effect_observed' and _payload(event).get('certainty')=='succeeded'
              and _payload(event).get('original_attempt_token')}
    superseded={attempt_id for event in ledger_events if event.get('kind')=='effect_admitted'
                for attempt_id in _payload(event).get('replaces_attempt_ids',())}
    admitted_uncertain=set()
    for event in ledger_events:
        row=_payload(event)
        if event.get('kind')!='effect_admitted' or row.get('action')!='submission':
            continue
        if observed_effects.get(row['effect_id'],'admitted') in {'succeeded','failed','confirmed_not_sent'}:
            continue
        fence_id=fence_by_attempt.get(row['attempt_id'])
        if fence_id is None or not any(record.fence_id==fence_id for record in verified.attempt_records):
            raise IntegrityError('visibility_submission_attempt_binding_missing')
        admitted_uncertain.add(fence_id)
    states = []
    for item in manifest.items:
        result = results.get(item.item_id)
        records = tuple(r for r in verified.attempt_records if r.observation_id == item.item_id)
        succeeded = accepted_result(result)
        all_unresolved = tuple(r.fence_id for r in records if (r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} or r.fence_id in admitted_uncertain) and r.fence_id not in resolved)
        unresolved = tuple(attempt_id for attempt_id in all_unresolved if attempt_id not in superseded)
        historical_unresolved = tuple(attempt_id for attempt_id in all_unresolved if attempt_id in superseded)
        accepted = tuple(r for r in records if r.state is SendAttemptState.ACKNOWLEDGED)
        source = next((r for r in reversed(accepted) if r.action in {"google_search", "copilot_answer"}), None)
        complete_chat = any(r.action == "same_chat_turn_2" and r.validation_status != "rejected" for r in accepted)
        complete_google = source is not None and any(
            r.action in {"gaio_gemini_extractor", "gemini_parse"} and r.validation_status != "rejected" for r in accepted)
        op_states = []
        for op in manifest.operations:
            if op.item_id != item.item_id:
                continue
            action = op.adapter_id.split(".")[-1]
            if (not succeeded and op.adapter_id!='visibility.google_search' and action=='measurement_turn_1'
                and any(r.action==action for r in accepted) and not any(r.action=='same_chat_turn_2' for r in records)):
                op_states.append((op.operation_id,'reconcile_required'))
            elif succeeded or any(r.action == action for r in accepted):
                op_states.append((op.operation_id, "succeeded"))
            elif any(r.action == action for r in records):
                op_states.append((op.operation_id, "failed"))
        links=[]
        for record in records:
            if record.response_reference:
                links.append(str(record.response_reference))
        for event in ledger_events:
            if event.get("kind")=="correlation" and _payload(event).get("item_id")==item.item_id:
                links.append("conversations/"+canonical_sha256({"attempt":_payload(event)["source_attempt_id"]}))
        states.append(ItemState(
            item.item_id,
            status="succeeded" if succeeded else ("cancelled" if failures.get(item.item_id)=="verification_observation_cancelled" else "failed" if result is not None or records or item.item_id in packet_failures else "cancelled" if verified.runner_state.get("cancel_requested") else "never_started"),
            reason_code="" if succeeded else failures.get(item.item_id,"unresolved_send" if unresolved else "cancelled" if verified.runner_state.get("cancel_requested") else "measurement_incomplete"),
            unresolved_attempt_ids=unresolved,
            historical_unresolved_attempt_ids=historical_unresolved,
            checked_attempt_ids=tuple(sorted(checked.get(item.item_id, ()))),
            reconstructable=not succeeded and (complete_chat or complete_google or bool(source and source.validation_status == "not_shown")),
            saved_input_available=source is not None,
            operation_states=tuple(op_states),
            evidence_links=tuple(dict.fromkeys(links)),
        ))
    worker = None
    if isinstance(worker_observation, Mapping):
        worker = WorkerObservation(**{k:v for k,v in worker_observation.items() if k in {f.name for f in dataclasses.fields(WorkerObservation)}})
    revision = canonical_sha256({
        "plan": verified.plan["plan_sha256"],
        "results": verified.observation_results,
        "attempts": [r.to_mapping() for r in verified.attempt_records],
        "events": ledger_events,
        "cancel": bool(verified.runner_state.get("cancel_requested")),
    })
    normalized = ledger.normalized_events() if any(e.get("kind")=="manifest" for e in ledger_events) else normalized_legacy_events(verified.attempt_records,manifest,ledger_events)+ledger_events
    resources={}
    for event in ledger_events:
        if event.get("kind")=="resource_observed":
            row=_payload(event)
            resources[row["resource_id"]]=row["status"]
    pending_attachment_scopes = set()
    blocked_attachment_scopes = set()
    for row in _unreleased_attachments(ledger_events, manifest):
        reference = row.get('reference') or {}
        if (row['status'] == 'owned' and worker is not None and worker.status == 'alive'
                and worker.ownership_status == 'owned'
                and worker.execution_id == reference.get('execution_id')):
            continue
        scopes = set(reference.get('resource_scopes', ()))
        pending_attachment_scopes.update(scopes)
        if _attachment_owner_observation(row).status not in {'dead', 'reused'}:
            blocked_attachment_scopes.update(scopes)
    # A new explicit pass may authorize the original natural action once the
    # old process is provably gone. Provider admission still waits for durable
    # attachment and exact-tab containment; no signed selection is upgraded.
    if pending_attachment_scopes:
        states = [dataclasses.replace(
            state,
            status='blocked' if scopes & blocked_attachment_scopes else state.status,
            reason_code='previous_attachment_cleanup_unconfirmed',
        ) if state.status != 'succeeded' and scopes & pending_attachment_scopes else state
            for state in states
            for scopes in ({scope for op in manifest.operations if op.item_id == state.item_id
                            for scope in op.resource_scopes},)]
    running_items = active_items(normalized, worker)
    states = [dataclasses.replace(state, status="active", reason_code="operation_in_progress")
              if state.status not in {"succeeded", "blocked", "cancelled"}
              and state.item_id in running_items and state.item_id not in packet_failures else state
              for state in states]
    latest_resources = {event['payload']['resource_id']: event['payload'] for event in ledger_events
                        if event.get('kind') == 'resource_observed'}
    held_scopes = {scope for row in latest_resources.values()
                   if (row.get('reference') or {}).get('verification_hold', {}).get('retained') is True
                   for scope in (row.get('reference') or {}).get('resource_scopes', ())}
    hold_runtime = verification_runtime or runtime
    if verification_store is None and hold_runtime is not None:
        from ..browser.verification_controls import store_for_runtime
        verification_store = store_for_runtime(hold_runtime, run_id, verified.plan["plan_sha256"])
    if verification_store is not None:
        held_scopes.update(row["profile_id"] + ":copilot" for row in verification_store.rows(own_run=False))
    if held_scopes:
        states = [dataclasses.replace(state, status='blocked', reason_code='copilot_verification_required')
                  if state.status != 'succeeded' and any(scope in held_scopes
                      for op in manifest.operations if op.item_id == state.item_id for scope in op.resource_scopes)
                  else state for state in states]
    return RecoverySnapshot(manifest, revision, tuple(states), normalized, worker=worker,
        resources=tuple(resources.items()),sampled_at=datetime.now(timezone.utc).isoformat()), verified


def plan_refetch(repository, run_id, *, request_id, runtime=None):
    snapshot, _ = snapshot_for(repository, run_id,runtime=runtime)
    coordinator = RecoveryCoordinator(RecoveryLedger(repository.run_dir(run_id)))
    return RecoveryRequest.from_preview(coordinator.plan(snapshot), request_id).to_dict()


class VisibilityExecution:
    """Adapter context passed into the existing frozen packet scheduler."""

    def __init__(self, repository, snapshot, verified, recovery_pass, coordinator, owner, *, initial=False,
                 page_open_limit=None, replace_checked_sends=True):
        self.repository, self.snapshot, self.verified = repository, snapshot, verified
        self.pass_record, self.coordinator, self.owner = recovery_pass, coordinator, owner
        self.pass_id = recovery_pass.pass_id
        self.initial = initial
        self.replace_checked_sends = replace_checked_sends
        # Pass-wide limit on page openings in flight (an ``asyncio.Semaphore``
        # or ``None``). Runner creates one per pass and gives the same object
        # to the group worker, so a ``reopen`` here waits on the counter the
        # packets' ``open`` calls use.
        self.page_open_limit = page_open_limit
        # The admitted selections are the sole authority for membership/actions.
        self.selections = MappingProxyType({s.item_id: s for s in recovery_pass.selections})
        self.attempts = {}
        self.pending = {}
        self.operations = {op.operation_id:op for op in snapshot.manifest.operations}
        self.effects = {}
        events = coordinator.ledger.read()
        self.finished = {e["payload"]["item_id"] for e in events
                         if e["kind"]=="revision_committed" and e["payload"].get("pass_id")==self.pass_id}
        self.failed = {e["payload"]["item_id"] for e in events
                       if e["kind"]=="failure" and e["payload"].get("pass_id")==self.pass_id
                       and e["payload"].get("attempt_id") is None}
        self.adopted = set()
        self.recovered = set()
        self.replacement_ready = {}
        self.contain = None
        self.resource_tracking = False
        self.pending_attachments = _unreleased_attachments(snapshot.events, snapshot.manifest)
        self.blocked_scopes = {scope for row in self.pending_attachments
                               for scope in (row.get('reference') or {}).get('resource_scopes', ())}
        latest_resources = {event['payload']['resource_id']: event['payload'] for event in snapshot.events
                            if event.get('kind') == 'resource_observed'}
        self.blocked_scopes.update(scope for row in latest_resources.values()
            if (row.get('reference') or {}).get('verification_hold', {}).get('retained') is True
            for scope in (row.get('reference') or {}).get('resource_scopes', ()))
        self.verification_holds = None
        self.requires_attachment_containment = bool(self.pending_attachments) and any(
            selection.action not in {'blocked', 'wait'} for selection in recovery_pass.selections)
        self.deadlines = {}
        self.continuations = {}
        self.source_records = {}
        for record in sorted(verified.attempt_records,key=lambda r:(r.response_captured_at or "",r.prepared_at,r.fence_id)):
            if record.action == "google_search" and record.state is SendAttemptState.ACKNOWLEDGED:
                self.source_records[record.observation_id] = record
        self.old_records = {r.fence_id:r for r in verified.attempt_records}
        self.old_results = {r["observation_id"]:r for r in verified.observation_results}
        self.new_results = {}
        self._result_attempt_store = None

    @property
    def selected(self):
        return self.selections.keys()

    def close(self):
        """Release the execution-scoped read index after all result commits."""
        if self._result_attempt_store is not None:
            self._result_attempt_store.close()
            self._result_attempt_store = None

    def _result_attempts(self, item_id):
        # Lineage includes older passes, unlike the worker's execution-filtered
        # index. Reuse this unfiltered index for the whole execution, but refresh
        # file identities and verify selected evidence on every snapshot.
        if self._result_attempt_store is None:
            from ..storage.send_attempts import SendAttemptStore
            self._result_attempt_store = SendAttemptStore(
                self.repository.run_dir(self.snapshot.manifest.run_id) / "fences"
            )
        return self._result_attempt_store.snapshot(observation_ids=(item_id,)).records

    def assert_owned(self):
        self.owner.assert_owned()
        if self.owner.pass_id != self.pass_id:
            raise IntegrityError("stale_recovery_pass_callback")

    def record_resource(self, *args, **kwargs):
        self.assert_owned()
        return self.coordinator.record_resource(*args, **kwargs)

    def progress(self, item_id=None, *, deadline=None, complete=False):
        self.assert_owned()
        if item_id is not None:
            if complete:
                self.deadlines.pop(item_id,None)
                self.owner.complete_operation(item_id)
                return
            if deadline is not None:
                self.deadlines[item_id]=deadline
        self.owner.heartbeat(progress=True,operation_id=item_id,
            deadline_monotonic=self.deadlines.get(item_id) if item_id else None)

    def logical_id(self, item_id, suffix):
        if suffix == "google-search" and item_id in self.source_records:
            return self.source_records[item_id].normalized_logical_turn_id
        return f"{item_id}:{suffix}" if self.initial else f"{item_id}:recovery-{self.pass_id}:{suffix}"

    def includes(self, record):
        if self.initial:
            return True
        if f":recovery-{self.pass_id}:" in record.fence_id or (record.action == "copilot_answer" and f":recovery-{self.pass_id}:" in record.normalized_logical_turn_id):
            return True
        source = self.source_records.get(record.observation_id)
        return source is not None and source.fence_id == record.fence_id

    def current_results(self, originals, *, observation_ids=None):
        selected=None if observation_ids is None else set(observation_ids)
        current={r["observation_id"]:r for r in originals}
        current.update({key:row for key,row in self.old_results.items() if selected is None or key in selected})
        current.update({key:row for key,row in self.new_results.items() if selected is None or key in selected})
        return [row for key,row in current.items() if key not in self.selected or key in self.finished]

    def eligible_ids(self, observation_ids):
        return tuple(value for value in observation_ids if value in self.selected and value not in self.failed)

    def preserve_resumable_preparation(self):
        if not self.initial:
            return False
        from ..storage.send_attempts import SendAttemptStore
        records=SendAttemptStore(self.repository.run_dir(self.snapshot.manifest.run_id)/"fences",watch_changes=False).iter_records()
        pending=[record for record in records if record.observation_id in self.selected
                 and record.observation_id not in self.finished
                 and record.state in {SendAttemptState.PREPARING,SendAttemptState.PREPARED,SendAttemptState.PRE_SEND_FAILED}]
        mappings={e['payload']['fence_id']:e['payload']['attempt_id'] for e in self.coordinator.ledger.read()
                  if e['kind']=='visibility_attempt'}
        for record in pending:
            self.assert_owned()
            attempt_id=mappings.get(record.fence_id) or 'legacy-attempt:'+canonical_sha256({
                'run_id':self.snapshot.manifest.run_id,'fence_id':record.fence_id})
            self.coordinator.record_failure(self.pass_id,record.observation_id,'preparation_incomplete',self.owner,
                attempt_id=attempt_id,operation_id=f'{record.observation_id}:{record.action}')
            self.progress(record.observation_id,complete=True)
        return bool(pending)

    def resume_prepared_attempt(self, item_id, action):
        candidates=[r for r in self.old_records.values() if r.observation_id==item_id
                    and r.action==action and self.includes(r)
                    and r.state in {SendAttemptState.PREPARING,SendAttemptState.PREPARED,SendAttemptState.PRE_SEND_FAILED}]
        if not candidates:
            return None
        fence=max(candidates,key=lambda r:(r.prepared_at,r.fence_id))
        mappings=[e["payload"] for e in self.coordinator.ledger.select("visibility_attempt", fence_id=fence.fence_id)]
        attempt_id=mappings[-1]["attempt_id"] if mappings else "legacy-attempt:"+canonical_sha256({"run_id":self.snapshot.manifest.run_id,"fence_id":fence.fence_id})
        return self.coordinator.resume_attempt(attempt_id,self.owner)

    def before_prepare(self, values):
        from ..platform_availability import require_active_action
        require_active_action(values["action"])
        holds = self.verification_holds
        if holds is not None and values.get('observation_id'):
            item_id = values['observation_id']
            operation = self.operations.get(f"{item_id}:{values['action']}")
            if operation is not None:
                for scope in operation.resource_scopes:
                    profile_id, _, platform = scope.partition(':')
                    if platform == 'copilot' and holds.store.scope_held(profile_id, item_id=item_id):
                        from ..errors import HumanVerificationRequiredError
                        raise HumanVerificationRequiredError('verification_pair_held')
        with self.coordinator.ledger.transaction(self.owner):
            self.assert_owned()
            fence_id = values.get("fence_id") or values.get("successor_fence_id")
            item_id = values.get("observation_id")
            if item_id is None:
                parent = self.old_records.get(values.get("confirmed_fence_id"))
                if parent is None:
                    from ..storage.send_attempts import SendAttemptStore
                    parent = SendAttemptStore(self.repository.run_dir(self.snapshot.manifest.run_id)/"fences", watch_changes=False).load(values["confirmed_fence_id"])
                item_id = parent.observation_id
            if item_id not in self.selected:
                raise IntegrityError("recovery_item_not_authorized")
            if fence_id not in self.attempts and (item_id,values['action']) not in self.pending:
                resumed=self.resume_prepared_attempt(item_id,values['action'])
                if resumed is not None:
                    self.pending[(item_id,values['action'])]=resumed
            if fence_id not in self.attempts and (item_id,values['action']) in self.pending:
                self.attempts[fence_id] = self.pending.pop((item_id,values['action']))
            if fence_id not in self.attempts:
                # Strict resume never resets pass consumption. A remaining,
                # prepared operation uses a newly admitted remaining attempt;
                # previous dispatched effects still consume their allowance.
                self.attempts[fence_id] = self.coordinator.begin_attempt(
                    self.pass_id, f"{item_id}:{values['action']}", self.owner,
                    action="continue" if item_id in self.replacement_ready or any(r in self.recovered for r in self.old_records if self.old_records[r].observation_id == item_id) else "execute", predecessor_ids=tuple(filter(None, (values.get("retried_from"),))),
                )
            if self.resource_tracking and values["action"] != "copilot_answer" and isinstance(values.get("identity"),Mapping):
                identity=values["identity"]
                self.record_resource("tab:"+str(identity["tab_reference"]),"owned",self.owner,
                    attempt_id=self.attempts[fence_id].attempt_id,reference={"identity":dict(identity),"item_id":item_id,"resource_scopes":self.operations[self.attempts[fence_id].operation_id].resource_scopes})
            self.coordinator.ledger.append("visibility_attempt", {
                "pass_id":self.pass_id,"fence_id":fence_id,
                "attempt_id":self.attempts[fence_id].attempt_id,
                "operation_id":self.attempts[fence_id].operation_id,
            },self.owner)

    def _admit_send(self, fence):
        from ..platform_availability import require_active_action
        require_active_action(fence.action)
        with self.coordinator.ledger.transaction(self.owner):
            self.assert_owned()
            if fence.fence_id not in self.attempts:
                self.before_prepare({"fence_id":fence.fence_id,"observation_id":fence.observation_id,"action":fence.action})
            attempt = self.attempts[fence.fence_id]
            try:
                effect = self.coordinator.admit_effect(
                    self.pass_id, attempt.operation_id, "submission", self.operations[attempt.operation_id].input_digest,
                    self.owner, attempt_id=attempt.attempt_id, cost=(("submissions", 1),),
                    replaces_attempt_ids=attempt.predecessor_ids if attempt.action=='replace' else (),
                )
                self.effects[fence.fence_id] = effect
                self.coordinator.mark_dispatched(effect.effect_id, self.owner)
                deadline=__import__('time').monotonic()+self.operations[attempt.operation_id].deadline_seconds
                self.progress(fence.observation_id,deadline=deadline)
            except BaseException as exc:
                # A journal append can publish its immutable record and fail
                # before returning. A fresh reader sees that suffix even when
                # this transaction's cached view has not adopted it yet.
                ledger = RecoveryLedger(self.repository.run_dir(self.snapshot.manifest.run_id), watch_changes=False)
                try:
                    admitted = any(event['payload']['action'] == 'submission'
                                   for event in ledger.select('effect_admitted', attempt_id=attempt.attempt_id))
                except BaseException as read_error:
                    raise IntegrityError("send_admission_record_unavailable") from read_error
                finally:
                    ledger.close()
                if admitted:
                    raise AmbiguousSendError(f"{fence.fence_id} has an interrupted durable send admission") from exc
                raise

    def captured(self, fence):
        with self.coordinator.ledger.transaction(self.owner):
            effect = self.effects.get(fence.fence_id)
            if effect is not None:
                self.coordinator.record_effect_outcome(effect.effect_id, "succeeded", self.owner)
            attempt = self.attempts.get(fence.fence_id)
            if attempt is not None:
                self.coordinator.finish_attempt(attempt.attempt_id, "completed", self.owner)
            self.progress(fence.observation_id, complete=True)

    def resolve_original_submission(self, original, reconciliation_attempt, reference):
        self.assert_owned()
        ledger=self.coordinator.ledger
        bindings=[event['payload']['attempt_id'] for event in ledger.select('visibility_attempt', fence_id=original.fence_id)]
        original_attempt_id=bindings[-1] if bindings else 'legacy-attempt:'+canonical_sha256({
            'run_id':self.snapshot.manifest.run_id,'fence_id':original.fence_id})
        effects=[event['payload'] for event in ledger.select('effect_admitted', attempt_id=original_attempt_id)
                 if event['payload']['action']=='submission']
        if not effects:
            if original.state is SendAttemptState.ACKNOWLEDGED and original.recovered_from:
                # This is already a committed adopted capture, being verified
                # again only to continue its same conversation after a crash.
                return
            raise IntegrityError('original_submission_admission_missing')
        for effect in effects:
            self.coordinator.resolve_original_effect(effect['effect_id'],
                reconciliation_attempt_id=reconciliation_attempt.attempt_id,
                original_attempt_token=original.fence_id,owner=self.owner,receipt_reference=reference)

    def checkpoint(self, fence, binding):
        self.assert_owned()
        identity = binding.get("identity", {})
        if binding.get("prompt_sha256") != fence.prompt_sha256 or any(
            str(identity.get(field, "")) != str(fence.identity.get(field, ""))
            for field in ("profile_id", "chrome_directory", "browser_context_reference", "tab_reference")
        ):
            raise IntegrityError("conversation_checkpoint_binding_mismatch")
        self.coordinator.ledger.append("correlation", {
            "pass_id": self.pass_id, "item_id":fence.observation_id,
            "operation_id":f"{fence.observation_id}:{fence.action}",
            "source_attempt_id":fence.fence_id,
            "binding":dict(binding),
        }, self.owner)

    def correlation(self, fence_id):
        ledger = self.coordinator.ledger
        seen=set()
        while fence_id and fence_id not in seen:
            seen.add(fence_id)
            matches=ledger.select("correlation", source_attempt_id=fence_id)
            binding=_payload(matches[-1])["binding"] if matches else None
            if binding is not None:
                return binding
            record=self.old_records.get(fence_id)
            fence_id=record.recovered_from if record is not None else None
        return None

    def commit_result(self, item_id, result):
        self.assert_owned()
        reference = self.repository.write_evidence(self.snapshot.manifest.run_id,
            __import__('json').dumps(result, ensure_ascii=False, sort_keys=True),
            media_type="application/json", suffix=".result-revision.json")
        source_ids=result.get("accepted_attempt_ids") or {}
        records=self._result_attempts(item_id)
        collection=collection_metadata(result,records,source_ids)
        captured_at=collection.get('captured_at')
        recovered_at=collection.get('recovered_at')
        accepted = accepted_result(result)
        revision=self.coordinator.commit_revision(self.pass_id, item_id, result, self.owner, output_reference=reference,
            accepted=accepted,captured_at=captured_at,recovered_at=recovered_at,
            lineage={"workflow_version":WORKFLOW_VERSION,"adapter_version":ADAPTER_VERSION,
                     "parser_version":"ordered-names-1","validator_version":"visibility-1",
                     "source_attempt_id":collection.get("source_attempt_id"),"accepted_attempt_ids":source_ids,
                     "collection":collection})
        self.new_results[item_id]={**result,"recovery_lineage":{
            "pass_id":self.pass_id,"revision_id":revision["revision_id"],
            "prior_revision_id":revision.get("prior_revision_id")}}
        self.finished.add(item_id)
        self.progress(item_id,complete=True)
        if not accepted:
            # Project stable engine codes, never arbitrary provider text.
            issue_codes = {issue.get("code") for issue in result.get("issues", ())
                           if isinstance(issue, Mapping)}
            reason = next((code for code in (
                "extraction_rejected", "retry_exhausted", "provider_no_response",
            ) if code in issue_codes), "measurement_failed")
            self.failure(item_id, reason)
        return self.repository.run_dir(self.snapshot.manifest.run_id)/reference["path"]

    def failure(self, item_id, reason, *, checked=()):
        self.assert_owned()
        self.coordinator.record_failure(self.pass_id,item_id,reason,self.owner,checked_attempt_ids=tuple(checked))
        self.failed.add(item_id)
        self.progress(item_id,complete=True)

    def item_failure_reason(self, item_id):
        """Read this pass's recorded item refusal without duplicating state."""
        for event in reversed(self.coordinator.ledger.select("failure", item_id=item_id)):
            payload = event["payload"]
            if payload.get("pass_id") == self.pass_id and payload.get("attempt_id") is None:
                return payload.get("reason_code") or None
        return None

    def item_refusal(self, item_id, error):
        """Record a coordinator refusal that concerns this item only.

        Returns the refusal code, or None when the error is anything else and
        must keep propagating. The code is a fixed engine identifier, never
        website text. Nothing was sent for a refused item.
        """
        code = str(error)
        if not isinstance(error, (RecoveryBlocked, AllowanceExceeded)) or code not in ITEM_SCOPED_REFUSALS:
            return None
        self.assert_owned()
        ledger = self.coordinator.ledger
        with ledger.transaction(self.owner):
            # A refusal can arrive after begin_attempt (for example from
            # admit_effect). Close that attempt so the completed pass never
            # holds a started attempt without an end record. finish_attempt
            # writes the item's failure record itself.
            for event in ledger.select("attempt_started", item_id=item_id):
                started = event["payload"]
                if started["pass_id"] != self.pass_id or ledger.select("attempt_finished", attempt_id=started["attempt_id"]):
                    continue
                self.coordinator.finish_attempt(started["attempt_id"], "failed", self.owner, reason_code=code)
            self.failure(item_id, code)
        return code

    def settle(self):
        self.assert_owned()
        for item_id in self.selected - self.finished - self.failed:
            self.failure(item_id, "cancelled_or_dependency_blocked")
        for item_id in tuple(self.deadlines):
            self.progress(item_id,complete=True)
        self.progress()
        self.coordinator.complete_pass(self.pass_id, self.owner)

    async def wait_for_active_blocker(self, worker, profile_id, item_id):
        holds = self.verification_holds
        if holds is None:
            return None
        handler = getattr(worker.providers.drivers.get('copilot'), 'human_verification_handler', None)
        active = getattr(handler, 'active_profiles', {}).get(profile_id)
        entry = holds.live.get(active)
        if entry is None:
            return None
        row = holds.store.episode(entry['episode_id'])
        if (not row['retained'] or row.get('blocker_kind') not in {'sign_in', 'verification'}
                or row['profile_id'] != profile_id
                or row['execution_id'] != self.owner.execution_id
                or row['status'] in {'unsupported_held', 'expired_held'}):
            return None
        from ..errors import ProviderAccessBlockedError
        from ..websites.chat_actions import _BrowserOperationDeadline
        deadline = _BrowserOperationDeadline(row['deadline_monotonic'], 'copilot blocker peer wait expired')
        self.progress(item_id, deadline=deadline.expires_at)
        try:
            await worker._await_with_cancellation(self.snapshot.manifest.run_id,
                handler.gate(SimpleNamespace(profile_id=profile_id), deadline),
                detail='cancellation interrupted Copilot blocker peer wait')
        except ProviderAccessBlockedError as error:
            return error.reason
        except TimeoutError:
            return 'copilot_blocker_deadline_expired'
        return None

    async def prepare_item(self, worker, item_id, observation, packet):
        scopes={scope for operation in self.snapshot.manifest.operations if operation.item_id==item_id for scope in operation.resource_scopes}
        if self.verification_holds is not None:
            for scope in scopes:
                profile_id, _, platform = scope.partition(':')
                if platform == 'copilot' and self.verification_holds.store.scope_held(profile_id, item_id=item_id):
                    reason = await self.wait_for_active_blocker(worker, profile_id, item_id)
                    if self.verification_holds.store.scope_held(profile_id, item_id=item_id):
                        self.failure(item_id, reason or 'copilot_verification_required')
                        return False
        if scopes.intersection(self.blocked_scopes):
            self.failure(item_id,"owned_resource_cleanup_blocked")
            return False
        if item_id in self.failed:
            return False
        if item_id in self.adopted or self.initial:
            return True
        self.adopted.add(item_id)
        selection = self.selections[item_id]
        if selection.action in {"blocked", "wait"}:
            self.failure(item_id, selection.reason_code or "recovery_blocked")
            return False
        records = [r for r in self.verified.attempt_records if r.observation_id == item_id]
        item_state=next(state for state in self.snapshot.items if state.item_id==item_id)
        uncertain_ids=item_state.unresolved_attempt_ids
        prior_checked=set(item_state.checked_attempt_ids)
        uncertain = [r for r in records if r.fence_id in uncertain_ids]
        platform = str(observation["platform"])
        if observation.get("client_research_capability") == "client_research.identification.v1":
            from ..execution.run_client_identification import reconcile_identification
            return await reconcile_identification(worker, self, observation, packet, records, uncertain)
        if platform == "github_copilot":
            from ..execution.recover_source_parser import reconcile_source_parser
            return await reconcile_source_parser(worker, self, observation, packet, records, uncertain)
        if selection.action == "reconstruct":
            self._adopt_accepted(records)
            return True
        # A parser consumes the exact accepted Google source. Reopening search
        # history is deliberately never used as historical evidence.
        if not uncertain and platform == "google_ai_overview":
            return True
        if not uncertain:
            # A complete rejected conversation restarts its declared unit.
            if any(r.action == "same_chat_turn_2" for r in records):
                return True
            accepted = [r for r in records if r.action == "measurement_turn_1" and r.state is SendAttemptState.ACKNOWLEDGED]
            if not accepted:
                return True
            uncertain = [accepted[-1]]
        recovered_records = []
        checked = []
        failed_checks=[]
        replacements=[]
        for original in uncertain:
            checked.append(original.fence_id)
            op_id = f"{item_id}:{original.action}"
            attempt = self.coordinator.begin_attempt(self.pass_id,op_id,self.owner,
                action="reconcile",predecessor_ids=(original.fence_id,))
            known_previously_checked=(original.state is SendAttemptState.ACKNOWLEDGED
                and original.action=='measurement_turn_1'
                and original.fence_id in prior_checked
                and original.fence_id not in uncertain_ids)
            response_check_seconds=0 if original.fence_id in selection.replacement_attempt_ids or known_previously_checked else self.operations[op_id].deadline_seconds
            inspection_seconds = (min(RECOVERY_INSPECTION_TIMEOUT_SECONDS, self.operations[op_id].deadline_seconds)
                                  if response_check_seconds == 0 else response_check_seconds)
            binding = self.correlation(original.fence_id)
            recovered = None
            submission = None
            reason = "conversation_reference_missing"
            if original.action == "google_search":
                reason = "google_history_not_reusable"
            elif binding is not None:
                driver_platform = "gemini" if original.action == "gaio_gemini_extractor" else platform
                driver = worker.providers.require(driver_platform)
                canonical_reference = getattr(driver, "canonical_conversation_reference", None) or (lambda value: value)
                reopen = getattr(driver,"reopen",None)
                if callable(reopen):
                    # Reopening a saved conversation is a page opening: wait
                    # for a place under the pass-wide limit first, then admit
                    # the retrieval effect and start its deadline. The place is
                    # given back as soon as ``reopen`` returns or raises; the
                    # response check below runs without it.
                    try:
                        slot = await worker._acquire_page_open_slot(self.snapshot.manifest.run_id, self.page_open_limit)
                    except Exception as exc:
                        # Run cancellation reached the wait. Nothing was
                        # admitted or dispatched, so there is no effect to
                        # settle; the check is recorded like one that could
                        # not reopen the conversation.
                        slot = None
                        reason = "original_history_unavailable"
                        self.coordinator.ledger.append('original_history_check_failed',{
                            'pass_id':self.pass_id,'item_id':item_id,'attempt_id':attempt.attempt_id,
                            'source_attempt_id':original.fence_id,
                            'failure_context':_history_check_failure_context(exc),
                        },self.owner)
                    if slot is not None:
                        try:
                            effect = self.coordinator.admit_effect(self.pass_id,op_id,"retrieve_existing",
                                self.operations[op_id].input_digest,self.owner,attempt_id=attempt.attempt_id)
                            self.coordinator.mark_dispatched(effect.effect_id,self.owner)
                            self.progress(item_id,deadline=__import__('time').monotonic()+inspection_seconds+PROVIDER_PREPARATION_TIMEOUT_SECONDS)
                        except BaseException:
                            slot.release()
                            raise
                        try:
                            try:
                                submission = await worker._await_with_cancellation(
                                    self.snapshot.manifest.run_id,reopen(binding,observation_id=item_id),
                                    detail="original_conversation_recovery_cancelled")
                            finally:
                                slot.release()
                            if submission is not None:
                                prompt = str(observation["prompt_text"])
                                if original.action == "same_chat_turn_2":
                                    from ..client_research import RECOMMENDATION_EXTRACTION_PROMPT
                                    if observation.get("client_research_capability") == "client_research.recommendations.v1":
                                        prompt = RECOMMENDATION_EXTRACTION_PROMPT
                                    else:
                                        from ..names.prompts import same_chat_extraction_prompt
                                        source = next((record for record in reversed(records + recovered_records)
                                            if record.action == "measurement_turn_1"
                                            and record.state is SendAttemptState.ACKNOWLEDGED
                                            and (record.observation_attempt_ordinal or 1) == (original.observation_attempt_ordinal or 1)
                                            and canonical_reference((record.ack_identity or record.identity).get("conversation_reference"))
                                            == canonical_reference(binding["identity"]["conversation_reference"])), None)
                                        if source is None:
                                            raise IntegrityError("recovered_extraction_source_missing")
                                        source_path = self.repository.run_dir(self.snapshot.manifest.run_id) / source.response_reference
                                        source_text = self.repository.read_evidence(self.snapshot.manifest.run_id, {
                                            "path": source.response_reference, "sha256": source.response_sha256,
                                            "byte_length": source_path.stat().st_size,
                                        }).decode("utf-8")
                                        prompt = same_chat_extraction_prompt(
                                            source_text, expected_prompt_sha256=original.prompt_sha256,
                                        )
                                elif original.action == "gaio_gemini_extractor":
                                    prompt = self._parser_prompt(worker,item_id,original)
                                submission.restore_recovery_attempt(prompt=prompt,
                                    response_baseline=binding["response_baseline"],binding=binding)
                                outcome = await worker._await_with_cancellation(self.snapshot.manifest.run_id,
                                    submission.reconcile_no_response(response_check_seconds), detail="original_response_check_cancelled")
                                recovered = getattr(outcome,"answer",None)
                                if recovered is None:
                                    reason = "original_response_unresolved"
                            self.coordinator.record_effect_outcome(effect.effect_id,"succeeded",self.owner)
                        except Exception as exc:
                            reason = "original_history_unavailable"
                            # The failure record keeps its fixed code; the cause is
                            # kept beside it as type name and reason code only.
                            self.coordinator.ledger.append('original_history_check_failed',{
                                'pass_id':self.pass_id,'item_id':item_id,'attempt_id':attempt.attempt_id,
                                'source_attempt_id':original.fence_id,
                                'failure_context':_history_check_failure_context(exc),
                            },self.owner)
                            self.coordinator.record_effect_outcome(effect.effect_id,"unknown",self.owner)
                else:
                    reason = "original_history_unsupported"
            if recovered is not None:
                from ..storage.send_attempts import SendAttempt
                ref = self.repository.write_evidence(self.snapshot.manifest.run_id,recovered.text,
                    media_type="text/plain; charset=utf-8",suffix=".recovered-response.txt")
                data = original.to_mapping()
                data.update(state="acknowledged",ack_identity=recovered.identity.to_mapping(),
                    response_reference=ref["path"],response_sha256=ref["sha256"],
                    validation_status="collected",response_captured_at=datetime.now(timezone.utc).isoformat(),
                    acknowledged_at=datetime.now(timezone.utc).isoformat())
                if original.action in {'same_chat_turn_2','gaio_gemini_extractor'}:
                    from ..names.check_chat_names import find_name_positions,parse_closed_name_array,MentionExtractionError
                    candidates=records+recovered_records
                    if original.action=='gaio_gemini_extractor':
                        source=self.source_records.get(item_id)
                    else:
                        source=next((record for record in reversed(candidates) if record.action=='measurement_turn_1'
                            and record.state is SendAttemptState.ACKNOWLEDGED
                            and (record.observation_attempt_ordinal or 1)==(original.observation_attempt_ordinal or 1)
                            and canonical_reference((record.ack_identity or record.identity).get('conversation_reference'))
                            == canonical_reference(recovered.identity.conversation_reference)),None)
                    if source is None:
                        self.coordinator.finish_attempt(attempt.attempt_id,'failed',self.owner,reason_code='recovered_extraction_source_missing')
                        self.failure(item_id,'recovered_extraction_source_missing',checked=checked)
                        return False
                    source_path=self.repository.run_dir(self.snapshot.manifest.run_id)/source.response_reference
                    source_text=self.repository.read_evidence(self.snapshot.manifest.run_id,{'path':source.response_reference,
                        'sha256':source.response_sha256,'byte_length':source_path.stat().st_size}).decode('utf-8')
                    try:
                        if observation.get("client_research_capability") == "client_research.recommendations.v1":
                            from ..client_research import validate_recommendation_extraction
                            validate_recommendation_extraction(recovered.text, source_text)
                        else:
                            find_name_positions(source_text,parse_closed_name_array(recovered.text))
                    except MentionExtractionError:
                        data.update(state='rejected',validation_status='rejected',detail='recovered_extraction_rejected')
                    else:
                        data['validation_status']='accepted'
                recovered_record=SendAttempt.from_mapping(data)
                recovered_records.append(recovered_record)
                # Publish reusable accepted/rejected capture before resolving
                # the old effect. A crash cannot discard the recovered answer
                # and turn the next pass into another send.
                related=[record for record in records if record.state is SendAttemptState.ACKNOWLEDGED
                         and (record.observation_attempt_ordinal or 1)==(original.observation_attempt_ordinal or 1)]
                self._adopt_accepted(related+recovered_records,identity=recovered.identity.to_mapping(),
                    canonical_reference=canonical_reference)
                self.resolve_original_submission(original,attempt,ref)
                self.coordinator.finish_attempt(attempt.attempt_id,"completed",self.owner)
                self.recovered.add(original.fence_id)
                if original.action=='measurement_turn_1':
                    self.continuations[item_id]=(submission,recovered.identity)
                # The new verified tab remains owned for a proven-unsent turn2.
                continue
            self.coordinator.finish_attempt(attempt.attempt_id,"failed",self.owner)
            if submission is not None:
                await submission.close()
            if known_previously_checked:
                # A previous deliberate pass already failed to restore this
                # exact accepted source's conversation. Its model send remains
                # known successful; this new pass may restart the declared unit
                # after one late-answer sample, without manufacturing uncertainty.
                self.coordinator.ledger.append('visibility_known_unit_restart',{
                    'pass_id':self.pass_id,'item_id':item_id,
                    'source_attempt_id':original.fence_id,
                    'checked_attempt_id':original.fence_id,
                    'original_effect_outcome':'succeeded',
                },self.owner)
                continue
            if self.replace_checked_sends and original.fence_id in selection.replacement_attempt_ids:
                # One previous explicit pass checked this exact old attempt.
                # This pass may now replace it; new uncertain attempts never
                # inherit that authorization.
                replacements.append(original.fence_id)
                continue
            failed_checks.append(reason)
        if failed_checks:
            self.failure(item_id,failed_checks[0],checked=checked)
            return False
        if replacements:
            self.replacement_ready[item_id]=tuple(replacements)
        if recovered_records:
            # Adopt only the exact conversation ordinal that was recovered.
            ordinals = {r.observation_attempt_ordinal or 1 for r in recovered_records}
            accepted = [r for r in records if r.state is SendAttemptState.ACKNOWLEDGED
                        and (r.observation_attempt_ordinal or 1) in ordinals]
            identity = recovered_records[-1].ack_identity
            self._adopt_accepted(accepted + recovered_records, identity=identity,
                canonical_reference=canonical_reference)
        return True

    async def contain_resources(self, attachment, *, cancelled=None, timeout_seconds=30):
        return await _contain_resources(self, attachment, cancelled=cancelled, timeout_seconds=timeout_seconds)

    def contain_local_attachments(self, *, cancelled=None):
        # Strict original-pass resume restores its existing same-chat tabs.
        # Only process-local transport handles are settled here; closing tabs
        # would destroy that continuation's required identity.
        rows = tuple(row for row in _unreleased_attachments(
            self.coordinator.ledger.read(), self.snapshot.manifest)
            if row['reference'].get('execution_id') != self.owner.execution_id)
        scopes = {scope for row in rows for scope in row['reference']['resource_scopes']}
        _release_local_attachments(self, rows, self.blocked_scopes - scopes, cancelled=cancelled)

    def owned_resource_references(self, platform, profile_ids):
        selected=set(profile_ids)
        latest={}
        for event in self.coordinator.ledger.read():
            if event['kind']=='resource_observed':
                latest[event['payload']['resource_id']]=(event['execution_id'],event['payload'])
        return tuple(sorted(resource_id for resource_id,(execution_id,row) in latest.items()
            if execution_id==self.owner.execution_id and row['status']=='owned'
            and (row.get('reference') or {}).get('identity',{}).get('profile_id') in selected
            and ((row.get('reference') or {}).get('platform')==platform
                 or any(scope.endswith(':'+platform) for scope in (row.get('reference') or {}).get('resource_scopes',())))))

    def record_cleanup(self, confirmed):
        latest={}
        for event in self.coordinator.ledger.read():
            if event["kind"]=="resource_observed" and event["execution_id"]==self.owner.execution_id:
                latest[event["payload"]["resource_id"]]=event["payload"]
        for resource_id,row in latest.items():
            if row["status"]=="owned":
                self.record_resource(resource_id,"released" if confirmed else "blocked",self.owner,
                    reference=row.get("reference"))

    def _parser_prompt(self, worker, item_id, original):
        from ..gaio_compatibility import build_gaio_gemini_extraction_prompt
        source = self.source_records.get(item_id)
        if source is None:
            raise IntegrityError("parser_source_missing")
        path=self.repository.run_dir(self.snapshot.manifest.run_id)/source.response_reference
        payload=self.repository.read_evidence(self.snapshot.manifest.run_id,{
            "path":source.response_reference,"sha256":source.response_sha256,"byte_length":path.stat().st_size,
        }).decode("utf-8")
        return build_gaio_gemini_extraction_prompt(payload)

    def _adopt_accepted(self, records, *, identity=None, canonical_reference=None):
        from ..storage.filesystem import atomic_write_json
        from ..storage.send_attempts import SendAttemptStore, SendAttempt
        store=SendAttemptStore(self.repository.run_dir(self.snapshot.manifest.run_id)/"fences",watch_changes=False)
        accepted = [r for r in records if r.state in {SendAttemptState.ACKNOWLEDGED,SendAttemptState.REJECTED}]
        extraction = max((r for r in accepted if r.action=="same_chat_turn_2"),
                         key=lambda r:(r.acknowledged_at or "",r.fence_id), default=None)
        if extraction is not None:
            expected_identity=extraction.ack_identity or extraction.identity
            canonical_reference = canonical_reference or (lambda value: value)
            source = max((r for r in accepted if r.action=="measurement_turn_1"
                          and (r.observation_attempt_ordinal or 1)==(extraction.observation_attempt_ordinal or 1)
                          and canonical_reference((r.ack_identity or r.identity).get("conversation_reference"))
                          ==canonical_reference(expected_identity.get("conversation_reference"))
                          and (r.ack_identity or r.identity).get("profile_id")==expected_identity.get("profile_id")),
                         key=lambda r:(r.acknowledged_at or "",r.fence_id), default=None)
            if source is None:
                raise IntegrityError("accepted_extraction_source_correspondence_missing")
            records=[source,extraction]
        elif accepted:
            latest={}
            for record in sorted(accepted,key=lambda r:(r.acknowledged_at or "",r.fence_id)):
                latest[record.action]=record
            records=list(latest.values())
        suffixes={"measurement_turn_1":"turn-1","same_chat_turn_2":"turn-2","gaio_gemini_extractor":"gaio-gemini-extractor"}
        for original in records:
            if original.state not in {SendAttemptState.ACKNOWLEDGED,SendAttemptState.REJECTED} or original.action=="google_search":
                continue
            suffix=suffixes[original.action]
            logical=self.logical_id(original.observation_id,suffix)
            if original.fence_id==logical and original.recovered_from:
                continue
            data=original.to_mapping()
            data.update(fence_id=logical,logical_turn_id=logical,attempt_ordinal=1,
                        observation_attempt_ordinal=1,retried_from=original.fence_id,retried_by=None,recovered_from=original.fence_id)
            if identity is not None:
                data["identity"]=dict(identity)
                data["ack_identity"]=dict(identity)
            record=SendAttempt.from_mapping(data)
            path=store.path(record.fence_id)
            if not path.exists():
                self.coordinator.ledger.append("visibility_adopted_attempt", {
                    "pass_id":self.pass_id,"fence_id":record.fence_id,
                    "original_attempt_id":original.fence_id,
                },self.owner)
                atomic_write_json(path,record.to_mapping(),exclusive=True)
            elif __import__('json').loads(path.read_text(encoding="utf-8")) != record.to_mapping():
                raise IntegrityError('recovered_attempt_revision_conflict')


class VisibilityDriver:
    """Admit navigation separately from the irreversible measurement send."""
    def __init__(self, driver, context, *, worker=None):
        self.driver,self.context,self.worker=driver,context,worker
        self.platform=driver.platform
    def __getattr__(self,name):
        return getattr(self.driver,name)
    async def _page_open_place(self, item_id):
        """Wait for a place under the pass-wide page-opening limit.

        The group worker owns the cancellation-aware wait; without a worker
        (a direct caller) nothing is held and nothing is released.
        """
        if self.worker is None:
            return None
        return await self.worker._acquire_page_open_slot(self.context.snapshot.manifest.run_id, self.context.page_open_limit)
    async def open(self,**kwargs):
        context=self.context
        item_id=kwargs['observation_id']
        observation=next(r for r in context.verified.plan['observation_plan']['observations'] if r['observation_id']==item_id)
        if observation.get('client_research_capability') == 'client_research.identification.v1':
            action='identification_judge' if self.platform==observation['judge_provider'] else 'identification_source'
        elif self.platform=='google_ai_overview': action='google_search'
        elif observation['platform']=='github_copilot': action='gemini_parse'
        elif observation['platform']=='google_ai_overview': action='gaio_gemini_extractor'
        else: action='measurement_turn_1'
        op_id=f'{item_id}:{action}'
        with context.coordinator.ledger.transaction(context.owner):
            attempt=context.resume_prepared_attempt(item_id,action)
            if attempt is None:
                attempt=context.coordinator.begin_attempt(context.pass_id,op_id,context.owner,
                    action='replace' if item_id in context.replacement_ready else 'continue' if any(
                        record_id in context.recovered for record_id,record in context.old_records.items() if record.observation_id==item_id) else 'execute',
                    predecessor_ids=context.replacement_ready.get(item_id,()))
            context.pending[(item_id,action)]=attempt
        # The attempt is started before the wait for a page-opening place and
        # the navigation effect is admitted after it, so the wait is visible in
        # the journal as the gap between ``attempt_started`` and the
        # ``navigate`` ``effect_admitted``. Cancellation while waiting ends the
        # attempt exactly like a failed opening.
        try:
            slot=await self._page_open_place(item_id)
        except BaseException:
            with context.coordinator.ledger.transaction(context.owner):
                context.coordinator.finish_attempt(attempt.attempt_id,'failed',context.owner,reason_code='preparation_failed')
            context.pending.pop((item_id,action),None)
            raise
        try:
            with context.coordinator.ledger.transaction(context.owner):
                operation=context.operations[op_id]
                effect=context.coordinator.admit_effect(context.pass_id,op_id,'navigate',operation.input_digest,
                    context.owner,attempt_id=attempt.attempt_id)
                context.coordinator.mark_dispatched(effect.effect_id,context.owner)
            # Admission writes precede the provider's navigation timer. Start the
            # advertised phase beside that timer, after those writes complete.
            context.progress(item_id,deadline=__import__('time').monotonic()+PROVIDER_PREPARATION_TIMEOUT_SECONDS)
            try:
                result=await self.driver.open(**kwargs)
            except BaseException:
                with context.coordinator.ledger.transaction(context.owner):
                    context.coordinator.record_effect_outcome(effect.effect_id,'unknown',context.owner)
                    context.coordinator.finish_attempt(attempt.attempt_id,'failed',context.owner,reason_code='preparation_failed')
                context.pending.pop((item_id,action),None)
                raise
        finally:
            # The place is given back as soon as the opening returns or raises;
            # it is never held through preparation or a send.
            if slot is not None:
                slot.release()
        context.coordinator.record_effect_outcome(effect.effect_id,'succeeded',context.owner)
        return result


def normalized_legacy_events(records, manifest, ledger_events):
    """Safe accounting import; no legacy evidence or send certainty is edited."""
    known={_payload(e).get('fence_id') for e in ledger_events if e.get('kind') in {'visibility_attempt','visibility_adopted_attempt'}}
    operations={o.operation_id:o for o in manifest.operations}
    values=[]
    ordinals={}
    for record in sorted(records,key=lambda r:(r.prepared_at,r.fence_id)):
        if record.fence_id in known:
            continue
        op_id=f'{record.observation_id}:{record.action}'
        if op_id not in operations:
            raise IntegrityError('legacy_operation_binding_invalid')
        ordinal=ordinals.get(op_id,0)+1
        ordinals[op_id]=ordinal
        identity=canonical_sha256({'run_id':manifest.run_id,'fence_id':record.fence_id})
        attempt_id=f'legacy-attempt:{identity}'
        effect_id=f'legacy-effect:{identity}'
        base={'attempt_id':attempt_id,'operation_id':op_id,'item_id':record.observation_id,
              'pass_id':'legacy-original','execution_id':'legacy-import'}
        def event(kind,payload):
            values.append({'kind':kind,'payload':payload,'baseline':True,
                           'recorded_at':record.prepared_at,'execution_id':'legacy-import'})
        event('attempt_started',{**base,'lifetime_ordinal':ordinal,'pass_ordinal':ordinal,
            'action':'execute','predecessor_ids':(),'started_at':record.prepared_at,
            'deadline_seconds':operations[op_id].deadline_seconds})
        if record.send_started_at:
            event('effect_admitted',{**base,'effect_id':effect_id,'action':'submission',
                'input_digest':operations[op_id].input_digest,'cost':(('submissions',1),),
                'idempotency_key':None,'certainty':'admitted','admitted_at':record.send_started_at})
            certainty='unknown' if record.state is SendAttemptState.SEND_STARTED else 'succeeded'
            event('effect_observed',{**base,'effect_id':effect_id,'certainty':certainty,
                'receipt_reference':None,'actual_cost':(),'dispatched':True})
        if record.state not in {SendAttemptState.PREPARING,SendAttemptState.PREPARED,SendAttemptState.PRE_SEND_FAILED,SendAttemptState.SEND_STARTED,SendAttemptState.RESPONSE_CAPTURED}:
            event('attempt_finished',{**base,'status':'completed' if record.state is SendAttemptState.ACKNOWLEDGED else 'failed','reason_code':'','output':None})
    return tuple(values)

    # Methods of VisibilityDriver end here. Durable resource containment belongs
    # to the visibility context and is installed explicitly below.


def _containment_failure_context(error, step):
    """Type name and step only. Never exception text, which can hold paths."""
    name = type(error).__name__
    return {'step': step,
            'exception_type': name if name.isascii() and name.isidentifier() and len(name) <= 80 else 'UnclassifiedError'}


def _history_check_failure_context(error):
    """Type name plus the fixed reason code of a ConversationCorrespondenceError.

    Never free exception text: only a code made of ``[a-z0-9_]`` is kept, as
    raised by ``verify_conversation_history`` and ``canonical_conversation_url``.
    """
    import re
    from ..websites.conversation_binding import ConversationCorrespondenceError
    name = type(error).__name__
    context = {'exception_type': name if name.isascii() and name.isidentifier() and len(name) <= 80 else 'UnclassifiedError',
               'reason_code': None}
    if isinstance(error, ConversationCorrespondenceError):
        code = str(error)
        if re.fullmatch(r'[a-z0-9_]{1,80}', code):
            context['reason_code'] = code
    return context


async def _contain_resources(context, attachment, *, cancelled=None, timeout_seconds=30):
    """Contain exact historical resources under the matching profile lane lock."""
    import asyncio
    from ..browser.profile_matching import browser_target_id
    from ..websites.chat_actions import _await_call, _BrowserOperationDeadline, BrowserIdentity
    latest={}
    for event in context.coordinator.ledger.read():
        if event['kind']=='resource_observed':
            row=event['payload']
            latest[row['resource_id']]=row
    prior_attachments = tuple(row for row in _unreleased_attachments(
        ({'kind': 'resource_observed', 'payload': row} for row in latest.values()), context.snapshot.manifest)
        if (row.get('reference') or {}).get('execution_id') != context.owner.execution_id)
    attachment_scopes = {scope for row in prior_attachments
                         for scope in (row.get('reference') or {}).get('resource_scopes', ())}
    failed_scopes = context.blocked_scopes - attachment_scopes
    context.blocked_scopes.update(attachment_scopes)
    unprovable_scopes = {scope for row in prior_attachments
        if _attachment_owner_observation(row).status not in {'dead', 'reused'}
        for scope in (row.get('reference') or {}).get('resource_scopes', ())}

    def block(scopes):
        failed_scopes.update(scopes)
        context.blocked_scopes.update(scopes)

    identities={}
    for record in context.verified.attempt_records:
        identity=record.ack_identity or record.identity
        reference=str(identity.get('tab_reference') or '')
        if reference:
            identities[reference]=dict(identity)
    for row in latest.values():
        if row['status'] in {'owned','cleanup_pending','blocked','released'} and isinstance(row.get('reference'),Mapping):
            identity=row['reference'].get('identity')
            if isinstance(identity,Mapping):
                identities[str(identity.get('tab_reference') or '')]=dict(identity)
    observation_platforms={r['observation_id']:str(r['platform']) for r in context.verified.plan['observation_plan']['observations']}
    groups={}
    for reference,identity in identities.items():
        if not reference:
            continue
        resource_id='tab:'+reference
        previous=latest.get(resource_id)
        record=next((r for r in context.verified.attempt_records if str((r.ack_identity or r.identity).get('tab_reference'))==reference),None)
        saved=(previous or {}).get('reference') or {}
        if record is None and saved.get('item_id') and saved.get('platform'):
            from types import SimpleNamespace
            declared=next((row for row in context.verified.plan['observation_plan']['observations'] if row['observation_id']==saved['item_id']),None)
            if declared is not None:
                action='google_search' if saved['platform']=='google_ai_overview' else 'gaio_gemini_extractor' if declared['platform']=='google_ai_overview' else 'measurement_turn_1'
                record=SimpleNamespace(observation_id=saved['item_id'],action=action)
        if record is None:
            scopes=saved.get('resource_scopes',())
            scopes=tuple(scopes) or tuple(op.resource_scopes[0] for op in context.snapshot.manifest.operations if op.resource_scopes[0].split(':')[0]==str(identity.get('profile_id')))
            block(scopes)
            context.record_resource(resource_id,'blocked',context.owner,reference={'identity':identity,'resource_scopes':scopes})
            continue
        platform='google_ai_overview' if record.action=='google_search' else 'gemini' if record.action=='gaio_gemini_extractor' else observation_platforms[record.observation_id]
        groups.setdefault((str(identity['profile_id']),platform),[]).append((resource_id,identity,record.observation_id))
    # A pair can hold a quarantine marker without holding any tab this journal
    # recorded, so the manifest's own pairs are checked too.
    manifest_pairs=[]
    for operation in context.snapshot.manifest.operations:
        for resource_scope in operation.resource_scopes[:1]:
            manifest_profile,_,manifest_platform=str(resource_scope).partition(':')
            pair=(manifest_profile,manifest_platform)
            if manifest_profile and manifest_platform and pair not in groups and pair not in manifest_pairs:
                manifest_pairs.append(pair)
    for profile_id,platform in [*groups,*manifest_pairs]:
        resources=groups.get((profile_id,platform),())
        scope=profile_id+':'+platform
        if any((latest.get(resource_id, {}).get('reference') or {}).get(
                'verification_hold', {}).get('retained') is True for resource_id,_,_ in resources):
            block((scope,))
            continue
        if scope in unprovable_scopes or (cancelled is not None and cancelled()):
            block((scope,))
            continue
        if not resources:
            try:
                attachment.manifest.profile(profile_id)
            except Exception:
                # This run's browser does not hold that profile, so no marker
                # of this pair can be named. Nothing is recorded for it.
                continue
        journal_references=tuple(resource_id for resource_id,_,_ in resources)
        recorded=set(journal_references)
        references=journal_references
        marker_references=()
        lease=None
        all_contained=True
        try:
            profile=attachment.manifest.profile(profile_id)
            marker_references=tuple(reference for reference
                in attachment.lease_manager.containment_requirements(profile,provider=platform)
                if reference not in recorded)
            references=tuple(sorted(recorded|set(marker_references)))
            if not references:
                continue
            lease=await asyncio.to_thread(attachment.lease_manager.acquire_containment,
                (profile,),
                owner=f'visibility-runner:{context.snapshot.manifest.run_id}:{context.owner.execution_id}:containment',
                provider=platform,resource_references=references,
                timeout_seconds=timeout_seconds,cancelled=cancelled)
            for resource_id,identity,item_id in resources:
                # A crash after durable closure but before marker removal may
                # leave a quarantined lane. Reuse the committed containment
                # proof only under its exact lane lease; do not revisit it.
                if latest.get(resource_id,{}).get('status')=='released':
                    continue
                reference={'identity':identity,'platform':platform,'item_id':item_id,'resource_scopes':(scope,)}
                context.assert_owned()
                context.record_resource(resource_id,'cleanup_pending',context.owner,reference=reference)
                deadline=_BrowserOperationDeadline.after(timeout_seconds,timeout_message='previous_owned_tab_containment_deadline')
                try:
                    page=await _await_call(attachment.provider_page_factory,platform,profile_id,item_id,False,BrowserIdentity.from_mapping(identity),_deadline=deadline)
                    if page is not None:
                        await _await_call(page.close,_deadline=deadline)
                        is_closed=getattr(page,'is_closed',None)
                        if not callable(is_closed) or not await _await_call(is_closed,_deadline=deadline):
                            raise IntegrityError('previous_owned_tab_closure_unconfirmed')
                    else:
                        target=str(identity.get('browser_target_reference') or '')
                        browser_context=str(identity.get('browser_context_reference') or '')
                        if not target or not browser_context:
                            raise IntegrityError('previous_owned_target_identity_missing')
                        reference['inventory']=await _prove_target_absent(attachment,identity,deadline)
                    context.record_resource(resource_id,'released',context.owner,reference=reference)
                except BaseException as exc:
                    all_contained=False
                    block((scope,))
                    context.record_resource(resource_id,'blocked',context.owner,reference={
                        **reference,'failure_context':_containment_failure_context(exc,'close_or_prove_absent')})
                    if not isinstance(exc,Exception):
                        raise
            for resource_id in marker_references:
                # The marker names a tab no journal row binds to an item. Its
                # creation URL is the only identity it has, so absence is
                # proven from the browser's own target list.
                if latest.get(resource_id,{}).get('status')=='released':
                    continue
                reference={'resource_scopes':(scope,),'platform':platform,'unrecorded':True}
                context.assert_owned()
                deadline=_BrowserOperationDeadline.after(timeout_seconds,timeout_message='previous_owned_tab_containment_deadline')
                try:
                    reference['inventory']=await _prove_marker_target_absent(attachment,resource_id,deadline)
                    context.record_resource(resource_id,'released',context.owner,reference=reference)
                except BaseException as exc:
                    all_contained=False
                    block((scope,))
                    context.record_resource(resource_id,'blocked',context.owner,reference={
                        **reference,'failure_context':_containment_failure_context(exc,'prove_marker_target_absent')})
                    if not isinstance(exc,Exception):
                        raise
            if all_contained:
                def persist_resolution(marker):
                    context.assert_owned()
                    context.coordinator.ledger.append('resource_containment_resolved',{
                        'pass_id':context.pass_id,'resource_scope':scope,
                        'resource_references':references,'unrecorded_references':marker_references,
                        'quarantine_record':dict(marker),
                    },context.owner)
                await asyncio.to_thread(attachment.lease_manager.resolve_containment,
                    lease,resource_references=references,record_resolution=persist_resolution)
        except BaseException as exc:
            block((scope,))
            failure_context=_containment_failure_context(exc,'acquire_or_resolve_pair_lock')
            for resource_id,identity,item_id in resources:
                if latest.get(resource_id,{}).get('status')=='released':
                    continue
                context.record_resource(resource_id,'blocked',context.owner,reference={
                    'identity':identity,'platform':platform,'item_id':item_id,'resource_scopes':(scope,),
                    'failure_context':failure_context})
            # A pair whose tabs were all released earlier writes no row above.
            # Record the refusal itself, so a blocked pair always has a cause.
            context.coordinator.ledger.append('resource_containment_blocked',{
                'pass_id':context.pass_id,'resource_scope':scope,
                'resource_references':references,'failure_context':failure_context,
            },context.owner)
            if not isinstance(exc,Exception):
                raise
        finally:
            if lease is not None:
                await asyncio.to_thread(lease.release)

    _release_local_attachments(context, prior_attachments, failed_scopes, cancelled=cancelled)


def _release_local_attachments(context, prior_attachments, failed_scopes, *, cancelled=None):
    """Release dead process-local handles, leaving separate tab guards intact."""
    attachment_scopes = {scope for row in prior_attachments
                         for scope in row['reference']['resource_scopes']}
    unresolved_scopes = set()
    for row in prior_attachments:
        reference = row.get('reference') or {}
        scopes = set(reference.get('resource_scopes', ()))
        context.assert_owned()
        observation = _attachment_owner_observation(row)
        if (observation.status not in {'dead', 'reused'}
                or (cancelled is not None and cancelled())):
            unresolved_scopes.update(scopes)
            continue
        # Death proves only this process-local transport is gone. Tabs are
        # separate durable resources: failed exact containment keeps its own
        # scope blocked without stopping other proven lanes. Send outcomes and
        # conversation reconciliation are deliberately untouched.
        context.record_resource(row['resource_id'], 'released', context.owner, reference={
            **reference,
            'cleanup_proof': {'kind': 'prior_local_attachment_process_gone',
                              'owner_observation': observation.to_dict(),
                              'containment_pass_id': context.pass_id},
        })
    context.blocked_scopes.difference_update(attachment_scopes)
    context.blocked_scopes.update(failed_scopes | unresolved_scopes)


INVENTORY_READ_ATTEMPTS=3
INVENTORY_RETRY_SECONDS=1.0


def _unique_objects(values):
    """Playwright facade identity is object identity, not a profile/CDP id."""
    seen=set()
    result=[]
    for value in values:
        if id(value) not in seen:
            seen.add(id(value))
            result.append(value)
    return result


MARKER_TAB_REFERENCE=re.compile(r'tab:visibility-runner-owned-tab:[0-9a-f]{32}')


def _marker_creation_url(tab_reference):
    """Return the URL an owned tab of this reference was created with.

    ``open_owned_tab`` and ``open_measurement_tab`` (browser/tabs.py) create the
    tab at this exact address and a creation without a trustworthy reply is
    never navigated anywhere else.
    """
    if not isinstance(tab_reference,str) or not MARKER_TAB_REFERENCE.fullmatch(tab_reference):
        raise IntegrityError('previous_owned_target_identity_missing')
    return 'about:blank#visibility-runner-owned-tab='+quote(tab_reference.rsplit(':',1)[1],safe='')


async def _prove_marker_target_absent(attachment,tab_reference,deadline):
    """Prove a marker's tab absent when no journal row carries its identity.

    The quarantine marker holds the tab reference and nothing else: no CDP
    target id, no browser context. The complete target list must therefore be
    readable, and no target may still hold the creation URL of this reference.
    """
    from ..browser.profile_matching import _identity_call, _detach_identity_session
    checked_url=_marker_creation_url(tab_reference)
    browser=attachment.browser
    try:
        connected=getattr(browser,'is_connected',None)
        if attachment.closed or not callable(connected) or not connected():
            raise IntegrityError('previous_owned_target_inventory_incomplete')
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError('previous_owned_target_inventory_incomplete') from exc
    session=None
    try:
        session=await _identity_call(browser.new_browser_cdp_session,
            deadline=deadline.expires_at,late_session=True)
        inventory=await _identity_call(lambda:session.send('Target.getTargets'),deadline=deadline.expires_at)
        rows=inventory.get('targetInfos') if isinstance(inventory,Mapping) else None
        if not isinstance(rows,list):
            raise IntegrityError('previous_owned_target_inventory_incomplete')
        for row in rows:
            if (not isinstance(row,Mapping) or not isinstance(row.get('targetId'),str)
                or not row['targetId'] or not isinstance(row.get('url'),str)):
                raise IntegrityError('previous_owned_target_inventory_incomplete')
            if row['url']==checked_url:
                raise IntegrityError('previous_owned_target_unadoptable')
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError('previous_owned_target_inventory_incomplete') from exc
    finally:
        if session is not None:
            await _detach_identity_session(session)
    return {'scope':'creation_url','targets':len(rows),'checked_url':checked_url}


async def _prove_target_absent(attachment,identity,deadline):
    """Prove absence using verified profile anchors and authoritative CDP state.

    A shared Playwright context may expose several CDP contexts. Its entire
    page list must be readable. Missing/stale client page lists alone never
    prove the old target absent, and no unrelated titles/URLs are retained.
    """
    import asyncio
    from ..browser.profile_matching import (
        browser_target_id, _identity_call, _detach_identity_session,
    )
    from ..websites.chat_actions import _await_call
    browser=attachment.browser
    try:
        connected=getattr(browser,'is_connected',None)
        if attachment.closed or not callable(connected) or not connected():
            raise IntegrityError('previous_owned_target_inventory_incomplete')
        contexts=_unique_objects(browser.contexts)
        if not contexts:
            raise IntegrityError('previous_owned_target_inventory_incomplete')
        profile=attachment.manifest.profile(str(identity.get('profile_id') or ''))
        if profile.chrome_directory != identity.get('chrome_directory'):
            raise IntegrityError('previous_owned_target_profile_mismatch')
        anchors=[anchor for anchor in attachment.session.anchors if anchor.profile.id==profile.id]
        if len(anchors)!=1 or anchors[0].profile.chrome_directory!=profile.chrome_directory:
            raise IntegrityError('previous_owned_target_anchor_invalid')
        anchor=anchors[0]
        anchor_context=anchor.page.context
        matching=(anchor.browser_context_id==identity.get('browser_context_reference')
                  and any(candidate is anchor_context for candidate in contexts))
        inspected=[anchor_context] if matching else contexts
        pages=_unique_objects(page for candidate in inspected for page in candidate.pages)
        target=str(identity.get('browser_target_reference') or '').removeprefix('cdp-target:')
        if not target:
            raise IntegrityError('previous_owned_target_identity_missing')
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError('previous_owned_target_inventory_incomplete') from exc

    session=None
    try:
        session=await _identity_call(browser.new_browser_cdp_session,
            deadline=deadline.expires_at,late_session=True)
        inventory=await _identity_call(lambda:session.send('Target.getTargets'),deadline=deadline.expires_at)
        rows=inventory.get('targetInfos') if isinstance(inventory,Mapping) else None
        if not isinstance(rows,list):
            raise IntegrityError('previous_owned_target_inventory_incomplete')
        for row in rows:
            if (not isinstance(row,Mapping) or not isinstance(row.get('targetId'),str)
                or not row['targetId'] or ('browserContextId' in row and
                (not isinstance(row['browserContextId'],str) or not row['browserContextId']))):
                raise IntegrityError('previous_owned_target_inventory_incomplete')
            if row['targetId']==target:
                raise IntegrityError('previous_owned_target_unadoptable')
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError('previous_owned_target_inventory_incomplete') from exc
    finally:
        if session is not None:
            await _detach_identity_session(session)

    unreadable=0
    for candidate in pages:
        observed_target=None
        for attempt in range(INVENTORY_READ_ATTEMPTS):
            observed_target=await browser_target_id(candidate,deadline=deadline.expires_at)
            if observed_target:
                break
            unreadable+=1
            if attempt+1<INVENTORY_READ_ATTEMPTS:
                await _await_call(asyncio.sleep,INVENTORY_RETRY_SECONDS,_deadline=deadline)
        if not observed_target:
            raise IntegrityError('previous_owned_target_inventory_incomplete')
        if str(observed_target).removeprefix('cdp-target:')==target:
            raise IntegrityError('previous_owned_target_unadoptable')
    if not browser.is_connected():
        raise IntegrityError('previous_owned_target_inventory_incomplete')
    return {'scope':'verified_anchor_context' if matching else 'all_contexts','contexts':len(inspected),
            'pages':len(pages),'unreadable_reads':unreadable}
