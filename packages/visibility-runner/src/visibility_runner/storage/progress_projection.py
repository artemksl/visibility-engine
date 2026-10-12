"""Pure reconstruction of mutable progress from verified durable records."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..models import (ArtifactReference, ArtifactReferences, GroundedOccurrence, Issue,
    ObservationResult, PacketKind, PacketPlan, PacketResult, RunPlan, RunResult, TransportCounts)
from ..planning.hashing import canonical_sha256, json_value
from ..result_acceptance import accepted_result
from ..status import ExtractionStatus, IssueSeverity, ObservationStatus, PacketStatus, RunStatus, reduce_run_status
from .send_attempts import AttemptSnapshot, SendAttempt, SendAttemptState, SendAttemptStore, count_attempt_milestones


def project_result(plan: RunPlan, observation_results, attempt_records, runner_state, group_progress, *, ownership=None) -> RunResult:
    """Project one verified snapshot without reading or changing durable records."""
    live = bool(ownership and ownership.get("state") == "owned"
                and ownership.get("status") == "alive"
                and ownership.get("execution_id")
                and ownership.get("execution_id") == runner_state.get("execution_id"))
    results = {
        str(row.get("observation_id") or ""): row
        for row in observation_results
    }
    attempts = AttemptSnapshot(attempt_records)
    fence_records = attempts.records
    packet_results: list[PacketResult] = []
    run_issues: list[Issue] = [
        _issue_from_mapping(row)
        for row in runner_state.get("issues") or ()
        if isinstance(row, Mapping)
    ]
    for packet in plan.packets:
        state = group_progress[packet.packet_id]
        observations: list[ObservationResult] = []
        packet_records = _measurement_group_attempts(
            packet,
            fence_records,
            plan=plan,
        )
        packet_issues = [
            _issue_from_mapping(row, packet_id=packet.packet_id)
            for row in state.get("issues") or ()
            if isinstance(row, Mapping)
        ]
        attention_observations = {
            issue.observation_id
            for issue in packet_issues
            if issue.requires_attention
        }
        # An admitted send is normally unresolved until its response arrives.
        # Only the matching live owner and running packet establish that wait;
        # recorded faults and abandoned sends retain their existing severity.
        for record in packet_records:
            if (
                record.state
                in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED}
                and record.observation_id not in results
                and record.observation_id not in attention_observations
                and not (live and state.get("status") == "running")
            ):
                packet_issues.append(
                    _issue_from_mapping(
                        {
                            "observation_id": record.observation_id,
                            "code": "ambiguous_send",
                            "message": (
                                f"{record.fence_id} has no durable "
                                "acknowledgement"
                            ),
                            "recoverable": False,
                        },
                        packet_id=packet.packet_id,
                    )
                )
                attention_observations.add(record.observation_id)
        typed_packet_issues = tuple(packet_issues)
        for observation_id in packet.observation_ids:
            value = results.get(observation_id)
            if value is None:
                continue
            if not _packet_owns_observation_result(
                packet,
                value,
                plan=plan,
            ):
                continue
            observations.append(
                _observation_result(
                    value,
                    packet=packet,
                    fences=attempts,
                )
            )
        status = _packet_status(
            state,
            observations,
            expected_count=len(packet.observation_ids),
            issues=typed_packet_issues,
        )
        if ownership is not None and not live and status is PacketStatus.RUNNING:
            status = PacketStatus.FAILED
        counts = _counts_from_records(packet_records)
        packet_result = PacketResult(
            packet_id=packet.packet_id,
            status=status,
            observations=tuple(observations),
            issues=typed_packet_issues,
            transport_counts=counts,
        )
        packet_results.append(packet_result)
        run_issues.extend(typed_packet_issues)
        for observation in observations:
            run_issues.extend(observation.issues)

    run_status = _run_status(
        packet_results,
        runner_state=runner_state,
    )
    if (live and runner_state.get("status") == "running"
            and all(packet.status in {PacketStatus.PLANNED, PacketStatus.RUNNING, PacketStatus.SUCCEEDED}
                    for packet in packet_results)
            and any(packet.status is not PacketStatus.SUCCEEDED for packet in packet_results)):
        run_status = RunStatus.RUNNING
    if run_status is RunStatus.SUCCEEDED:
        # Packet progress is a projection, not final measurement evidence.
        # Search/parser packets may share IDs and expose the final result
        # only once, so this gate belongs at the frozen run boundary.
        incomplete_ids = tuple(
            observation_id for observation_id in plan.observation_ids
            if not accepted_result(results.get(observation_id))
        )
        if incomplete_ids:
            run_status = RunStatus.NEEDS_ATTENTION
            run_issues.append(Issue(
                code="incomplete_accepted_results",
                message=(
                    "Progress records claim completion, but required "
                    "accepted measurement results are missing or incomplete."
                ),
                severity=IssueSeverity.CRITICAL,
                detail={"observation_ids": incomplete_ids},
            ))
    run_counts = TransportCounts()
    for packet in packet_results:
        run_counts = run_counts + packet.transport_counts
    return RunResult(
        run_id=plan.run_id,
        status=run_status,
        packets=tuple(packet_results),
        submission_budget=plan.submission_budget,
        transport_counts=run_counts,
        issues=_deduplicate_issues(run_issues),
        plan=plan,
    )


def progress_fields(result: RunResult) -> dict[str, Any]:
    """The persisted view and public readout share status and actual counts."""
    return {
        "status": result.status.value,
        "packet_statuses": {packet.packet_id: packet.status.value for packet in result.packets},
        "transport_counts": json_value(result.transport_counts),
    }


def _artifact(value: Mapping[str, Any]) -> ArtifactReference:
    return ArtifactReference(
        relative_path=str(value.get("path") or value.get("relative_path") or ""),
        sha256=str(value["sha256"]),
        byte_length=int(value["byte_length"]),
        media_type=str(value.get("media_type") or "application/octet-stream"),
    )


def _collection_metadata(value, records, accepted_attempt_ids):
    from ..recovery_adapters.visibility import collection_metadata
    return collection_metadata(value,records,accepted_attempt_ids)


def _observation_result(
    value: Mapping[str, Any],
    *,
    packet: PacketPlan,
    fences: SendAttemptStore | AttemptSnapshot,
) -> ObservationResult:
    observation_id = str(value["observation_id"])
    collection = str(value.get("collection_status") or "")
    status_value = str(value.get("status") or "").casefold()
    if collection == "not_shown":
        status = ObservationStatus.NOT_SHOWN
    else:
        status = {
            "succeeded": ObservationStatus.SUCCEEDED,
            "partial": ObservationStatus.PARTIAL,
            "needs_attention": ObservationStatus.NEEDS_ATTENTION,
            "cancelled": ObservationStatus.CANCELLED,
        }.get(status_value, ObservationStatus.FAILED)
    extraction = {
        "complete": ExtractionStatus.SUCCEEDED,
        "not_required": ExtractionStatus.NOT_REQUIRED,
        "rejected": ExtractionStatus.REJECTED,
        "needs_attention": ExtractionStatus.NEEDS_ATTENTION,
        "cancelled": ExtractionStatus.CANCELLED,
    }.get(
        str(value.get("extraction_status") or "").casefold(),
        ExtractionStatus.FAILED,
    )
    artifacts = value.get("artifacts") or {}
    turn_1 = artifacts.get("turn_1_response")
    turn_2 = artifacts.get("turn_2_response")
    google_source = artifacts.get("google_source_response")
    gemini_response = artifacts.get("gemini_extractor_response")
    google_capture_evidence = artifacts.get("google_capture_evidence")
    references = ArtifactReferences(
        turn_1=_artifact(turn_1) if isinstance(turn_1, Mapping) else None,
        turn_2=(
            _artifact(turn_2)
            if isinstance(turn_2, Mapping)
            else _artifact(gemini_response)
            if isinstance(gemini_response, Mapping)
            else None
        ),
        google_source=(
            _artifact(google_source)
            if isinstance(google_source, Mapping)
            else None
        ),
        additional=(
            tuple(_artifact(value) for key,value in artifacts.items() if key not in {"turn_1_response","turn_2_response","google_source_response","gemini_extractor_response","google_capture_evidence"} and isinstance(value,Mapping))
            if value.get("client_research")
            else (_artifact(google_capture_evidence),)
            if isinstance(google_capture_evidence, Mapping)
            else ()
        ),
    )
    issues = tuple(
        _issue_from_mapping(row, packet_id=packet.packet_id)
        for row in value.get("issues") or ()
        if isinstance(row, Mapping)
    )
    grounded_occurrences = tuple(
        GroundedOccurrence(
            name=row.get("name"),
            position=row.get("position"),
            character_start=row.get("character_start"),
            character_end=row.get("character_end"),
            byte_start=row.get("byte_start"),
            byte_end=row.get("byte_end"),
        )
        for row in value.get("grounded_occurrences") or ()
        if isinstance(row, Mapping)
    )
    names = tuple(row.name for row in grounded_occurrences)
    snapshot = (
        fences.snapshot(observation_ids=(observation_id,))
        if isinstance(fences, SendAttemptStore)
        else fences
    )
    records = snapshot.for_observation(observation_id)
    attempt_ids = tuple(
        str(item)
        for item in (
            value.get("attempt_ids")
            or [record.fence_id for record in records]
        )
    )
    accepted_attempt_ids_value = value.get("accepted_attempt_ids") or {}
    accepted_attempt_ids = (
        {
            str(key): str(item)
            for key, item in accepted_attempt_ids_value.items()
        }
        if isinstance(accepted_attempt_ids_value, Mapping)
        else {}
    )
    counts = _counts_from_records(records)
    collection_metadata = _collection_metadata(value, records, accepted_attempt_ids)
    return ObservationResult(
        client_research=value.get("client_research") or {},
        **({"collection_metadata": collection_metadata}
           if "collection_metadata" in ObservationResult.__dataclass_fields__ else {}),
        observation_id=observation_id,
        packet_id=packet.packet_id,
        platform=value["platform"],
        profile_id=packet.profile_id,
        status=status,
        extraction_status=extraction,
        prompt_sha256=(
            str(value.get("prompt_sha256") or "") or None
        ),
        chrome_directory=(
            str(value.get("chrome_directory") or "") or None
        ),
        artifacts=references,
        capture_evidence=(
            value.get("capture_evidence")
            if isinstance(value.get("capture_evidence"), Mapping)
            else {}
        ),
        ordered_names=names,
        grounded_occurrences=grounded_occurrences,
        issues=issues,
        transport_counts=counts,
        accepted_attempt_id=(
            str(value.get("accepted_attempt_id"))
            if value.get("accepted_attempt_id")
            else None
        ),
        accepted_attempt_ids=accepted_attempt_ids,
        attempt_ids=attempt_ids,
    )


def _safe_capability_issue_detail(value: Mapping[str, Any]) -> dict | None:
    if value.get("code") != "copilot_source_capability_unavailable":
        return None
    from ..providers.github_copilot import _capability_detail
    detail = value.get("capability_detail")
    if detail is None and isinstance(value.get("detail"), Mapping):
        detail = value["detail"].get("capability_detail")
    return _capability_detail(detail)


def _issue_from_mapping(
    value: Mapping[str, Any],
    *,
    packet_id: Optional[str] = None,
) -> Issue:
    code = str(value.get("code") or "runner_issue")
    detail = dict(value["detail"]) if isinstance(value.get("detail"), Mapping) else {}
    detail.pop("capability_detail", None)
    capability = _safe_capability_issue_detail(value)
    if capability:
        detail["capability_detail"] = capability
    return Issue(
        code=code,
        message=str(value.get("message") or value.get("detail") or code),
        severity=(
            IssueSeverity.CRITICAL
            if code in {
                "needs_attention",
                "ambiguous_send",
                "identity_mismatch",
                "integrity_error",
            }
            else IssueSeverity.ERROR
        ),
        observation_id=(
            str(value.get("observation_id"))
            if value.get("observation_id")
            else None
        ),
        packet_id=packet_id or (
            str(value.get("packet_id")) if value.get("packet_id") else None
        ),
        retry_safe=bool(value.get("recoverable", False)),
        detail=detail,
    )


def _counts_from_nested(value: object) -> TransportCounts:
    rows = value.values() if isinstance(value, Mapping) else ()
    return TransportCounts(
        prepared=sum(int(row.get("prepared") or 0) for row in rows),
        send_started=sum(int(row.get("send_started") or 0) for row in rows),
        responses=sum(int(row.get("responses") or 0) for row in rows),
        acknowledged=sum(int(row.get("acknowledged") or 0) for row in rows),
        retry_count=sum(int(row.get("retry_count") or 0) for row in rows),
        exhausted_retry_count=sum(
            int(row.get("exhausted_retry_count") or 0)
            for row in rows
        ),
    )


def _measurement_group_attempts(
    packet: PacketPlan,
    records: Sequence[SendAttempt],
    *,
    plan: RunPlan,
) -> tuple[SendAttempt, ...]:
    """Select each measurement group's durable transport milestones exactly once."""

    observation_ids = set(packet.observation_ids)
    if packet.kind is PacketKind.GAIO_EXTRACTION:
        return tuple(
            record
            for record in records
            if record.observation_id in observation_ids
            and record.action == "gaio_gemini_extractor"
        )
    split_ids = {
        observation_id
        for candidate in plan.packets
        if candidate.kind is PacketKind.GAIO_EXTRACTION
        for observation_id in candidate.observation_ids
    }
    return tuple(
        record
        for record in records
        if record.observation_id in observation_ids
        and not (
            record.action == "gaio_gemini_extractor"
            and record.observation_id in split_ids
        )
    )


def _counts_from_records(
    records: Sequence[SendAttempt],
) -> TransportCounts:
    """Derive actual counts from send attempts, including mid-crash milestones."""

    return TransportCounts(**count_attempt_milestones(records))


def _packet_status(
    state: Mapping[str, Any],
    observations: Sequence[ObservationResult],
    *,
    expected_count: int,
    issues: Sequence[Issue],
) -> PacketStatus:
    if any(issue.requires_attention for issue in issues):
        return PacketStatus.NEEDS_ATTENTION
    value = str(state.get("status") or "").casefold()
    completed = {
        str(item) for item in state.get("completed_observation_ids") or ()
    }
    if value == "succeeded":
        return (
            PacketStatus.SUCCEEDED
            if len(completed) == expected_count
            else PacketStatus.PARTIAL
            if observations
            else PacketStatus.FAILED
        )
    return {
        "planned": PacketStatus.PLANNED,
        "running": PacketStatus.RUNNING,
        "succeeded": PacketStatus.SUCCEEDED,
        "cancelled": PacketStatus.CANCELLED,
        "needs_attention": PacketStatus.NEEDS_ATTENTION,
        "partial": PacketStatus.PARTIAL,
    }.get(value, PacketStatus.FAILED)


def _packet_owns_observation_result(
    packet: PacketPlan,
    value: Mapping[str, Any],
    *,
    plan: RunPlan,
) -> bool:
    """Expose each final prompt measurement once across the two GAIO phases."""

    observation_id = str(value.get("observation_id") or "")
    has_split = any(
        candidate.kind is PacketKind.GAIO_EXTRACTION
        and observation_id in candidate.observation_ids
        for candidate in plan.packets
    )
    if not has_split:
        return packet.kind in {PacketKind.MEASUREMENT, PacketKind.SOURCE_PARSER}
    if str(value.get("platform") or "") != "google_ai_overview":
        return packet.kind in {PacketKind.MEASUREMENT, PacketKind.SOURCE_PARSER}
    collection = str(value.get("collection_status") or "").casefold()
    extraction = str(value.get("extraction_status") or "").casefold()
    extracted_terminally = (
        collection == "collected"
        and extraction in {"complete", "rejected", "failed"}
    )
    if packet.kind is PacketKind.GAIO_EXTRACTION:
        return extracted_terminally
    return not extracted_terminally


def _run_status(
    packets: Sequence[PacketResult],
    *,
    runner_state: Mapping[str, Any],
) -> RunStatus:
    # Compatibility for internal callers of the former inspection helper.
    return reduce_run_status(
        (packet.status for packet in packets),
        persisted=str(runner_state.get("status") or ""),
    )


def _deduplicate_issues(
    issues: Sequence[Issue],
) -> tuple[Issue, ...]:
    """Flatten measurement group/prompt measurement issue views without duplicating one fault."""

    result: list[Issue] = []
    seen: set[tuple[object, ...]] = set()
    for issue in issues:
        key = (
            issue.code,
            issue.message,
            issue.severity.value,
            issue.observation_id,
            issue.packet_id,
            issue.retry_safe,
            canonical_sha256(json_value(issue.detail)),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return tuple(result)


def project_progress(plan, results, attempts, events, *, policy=None, control=None):
    by_id={row['observation_id']:row for row in results}
    epochs=[event['payload'] for event in events if event['kind'] in {'pass_authorized','pass_resumed'}]
    latest=epochs[-1] if epochs else {}
    execution_id=latest.get('execution_id')
    pass_id=latest.get('pass_id')
    cancelled=bool(control and control.get('cancel_requested') and control.get('execution_id')==execution_id) or any(
        event['kind']=='pass_cancelled' and event['payload'].get('pass_id')==pass_id for event in events)
    resolved={event['payload']['original_attempt_token'] for event in events
        if event['kind']=='effect_observed' and event['payload'].get('certainty')=='succeeded'
        and event['payload'].get('original_attempt_token')}
    superseded={attempt for event in events if event['kind']=='effect_admitted'
        for attempt in event['payload'].get('replaces_attempt_ids',())}
    uncertain={record.observation_id for record in attempts
        if record.state.value in {'send_started','response_captured'}
        and record.fence_id not in resolved|superseded}
    effect_states={event['payload']['effect_id']:event['payload']['certainty'] for event in events if event['kind']=='effect_observed'}
    attempt_items={event['payload']['attempt_id']:event['payload']['item_id'] for event in events
                   if event['kind'] in {'attempt_started', 'attempt_resumed'}}
    for event in events:
        row=event['payload']
        if event['kind']=='effect_admitted' and row.get('action')=='submission' and effect_states.get(row['effect_id'],'admitted') in {'admitted','unknown'}:
            if row.get('attempt_id') not in superseded:
                uncertain.add(row.get('item_id') or attempt_items[row['attempt_id']])
    failed={event['payload']['item_id'] for event in events if event['kind']=='failure'
            and event['payload'].get('pass_id')==pass_id}
    states={}
    split_ids={item for packet in plan['packets'] if packet.get('kind')=='gaio_extraction' for item in packet['observation_ids']}
    for packet in plan['packets']:
        item_ids=tuple(packet['observation_ids'])
        complete=[]
        success=[]
        partial=[]
        attention=[]
        source_only=packet.get('platform')=='google_ai_overview' and packet.get('kind','measurement')=='measurement'
        for item_id in item_ids:
            row=by_id.get(item_id)
            if source_only and item_id in split_ids:
                captured=any(record.observation_id==item_id and record.action=='google_search' and record.state.value=='acknowledged' for record in attempts)
                if captured:
                    complete.append(item_id)
                    success.append(item_id)
                continue
            if row is not None:
                complete.append(item_id)
                if row.get('status') == 'partial':
                    partial.append(item_id)
                if row.get('status') == 'needs_attention':
                    attention.append(item_id)
                if row.get('status') in {'succeeded','not_shown'} and row.get('extraction_status') not in {'failed','rejected','needs_attention'}:
                    success.append(item_id)
        if attention:
            status='needs_attention'
        elif len(complete)==len(item_ids):
            status='succeeded' if len(success)==len(item_ids) else 'partial' if success or partial else 'failed'
        elif cancelled:
            status='cancelled'
        elif set(item_ids)&uncertain:
            status='needs_attention'
        elif set(item_ids)&failed:
            status='partial' if success or partial else 'failed'
        elif complete:
            status='partial'
        else:
            status='planned'
        states[packet['packet_id']]={'schema_version':'visibility_runner.packet_state.v1',
            'packet_id':packet['packet_id'],'status':status,'revision':0,'issues':[],
            'completed_observation_ids':complete,'transport_counts':{},'projection_rebuilt':True}
    # Reconstruct missing live progress first; the same readout reducer used
    # by inspection decides the run status. A journal cannot prove RUNNING.
    packets = [PacketResult(packet_id=key, status=PacketStatus(value['status']), observations=())
               for key, value in states.items()]
    status = _run_status(packets, runner_state={'status': 'cancelled' if cancelled else ''}).value
    runner={'schema_version':'visibility_runner.runner_state.v1','run_id':plan['run_id'],
        'plan_sha256':plan['plan_sha256'],'status':status,'revision':0,'issues':[],
        'packet_statuses':{key:value['status'] for key,value in states.items()},
        'transport_counts':{},'cancel_requested':cancelled,'execution_id':execution_id,
        'recovery_pass_id':pass_id,'projection_rebuilt':True}
    if policy is not None:
        runner['execution_policy_sha256']=policy['policy_sha256']
    return runner,states
