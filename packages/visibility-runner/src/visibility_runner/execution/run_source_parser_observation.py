"""Durable GitHub Copilot source -> Gemini parser observation execution.

Every irreversible send is fenced by the existing recovery coordinator. Source
answers remain immutable across parser retries. Unknown sends are reconciled in
place, never translated into a fresh prompt.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
import uuid

from ..errors import AmbiguousSendError, IntegrityError
from ..names.parse_source_answer import CONTRACT_VERSION, source_parser_prompt, text_sha256, validate_source_names
from ..names.check_chat_names import MentionExtractionError
from ..planning.hashing import json_value
from ..providers.github_copilot import CopilotSourceError
from ..storage.filesystem import atomic_write_json
from ..storage.send_attempts import SendAttemptStore, SendAttemptState, count_attempt_milestones
from ..websites.chat_actions import BrowserIdentity


def _account_failure_code(code):
    return code == "copilot_source_capability_unavailable" or any(
        word in code for word in ("quota", "auth", "account", "entitlement", "rate_limit")
    )


def _check_account_failure(worker):
    if worker._copilot_account_failure:
        raise CopilotSourceError(worker._copilot_account_failure,
            capability_detail=worker._copilot_account_failure_detail)


def _retain_account_failure(worker, exc):
    code = str(getattr(exc, "code", ""))
    if _account_failure_code(code):
        worker._copilot_account_failure = code
        worker._copilot_account_failure_detail = getattr(exc, "capability_detail", None)


def _records(worker, run_id, item_id):
    # Deliberately includes accepted source from earlier passes. Ordinary browser
    # conversations use a pass filter; a frozen external source must survive it.
    with SendAttemptStore(worker.repository.run_dir(run_id) / "fences", watch_changes=False) as store:
        return sorted(store.snapshot(observation_ids=(item_id,)).records,
                      key=lambda r: (r.prepared_at, r.normalized_attempt_ordinal, r.fence_id))


def _read(worker, run_id, record):
    path = worker.repository.run_dir(run_id) / record.response_reference
    ref = {"path": record.response_reference, "sha256": record.response_sha256,
           "byte_length": path.stat().st_size, "media_type": "text/plain; charset=utf-8"}
    return worker.repository.read_evidence(run_id, ref).decode("utf-8"), ref


def _config(plan, observation):
    config = dict(observation.get("github_copilot") or (plan.get("scope_manifest") or {}).get("github_copilot") or
                  (plan.get("measurement_config") or {}).get("github_copilot") or {})
    if not config:
        raise IntegrityError("github_copilot_frozen_config_missing")
    return config


def _stage(worker, run_id, observation, stage, **values):
    worker._assert_execution()
    atomic_write_json(worker.repository.run_dir(run_id) / "source-parser" /
                      (text_sha256(observation["observation_id"]) + ".json"),
                      {"observation_id": observation["observation_id"], "stage": stage,
                       "updated_monotonic": time.monotonic(), **values})


def _finish_failed(worker, record, *, confirmed_not_sent=False):
    context = worker.recovery
    if context is None:
        return
    effect = context.effects.get(record.fence_id)
    if effect is not None and confirmed_not_sent:
        context.coordinator.record_effect_outcome(effect.effect_id, "confirmed_not_sent", context.owner)
    attempt = context.attempts.get(record.fence_id)
    if attempt is not None:
        context.coordinator.finish_attempt(attempt.attempt_id, "failed", context.owner, reason_code="source_parser_pre_send_failed")


def _source(worker, run_id, config):
    from ..providers.github_copilot import GitHubCopilotSource
    runtime = getattr(worker, "copilot_runtime", None)
    runtime = json_value(runtime) if runtime is not None else {}
    if not runtime:
        raise IntegrityError("github_copilot_runtime_missing")
    for key in ("account_ref", "github_account_id"):
        if runtime.get(key) != config.get(key):
            raise IntegrityError("github_copilot_account_mismatch")
    merged = {**config, **runtime, "method": config["source_method"], "model_policy": config["model"]}
    factory = getattr(worker, "copilot_source_factory", None) or GitHubCopilotSource
    return factory(state_directory=worker.repository.run_dir(run_id) / "sdk-sessions", config=merged)


def _source_evidence(answer, record, config):
    if (answer.get("status") != "completed" or not isinstance(answer.get("text"), str)
            or not answer["text"].strip()):
        raise AmbiguousSendError("github_copilot_terminal_answer_unproven")
    actual_account = {str(answer.get("github_account_id", "")).casefold(), str(answer.get("github_login", "")).casefold()}
    if (answer.get("account_ref") != config["account_ref"]
            or str(config["github_account_id"]).casefold() not in actual_account
            or answer.get("source_method") != config["source_method"]
            or answer.get("model_policy") != config["model"]
            or answer.get("sdk_version") != config["sdk_version"]
            or answer.get("runtime_version") != config["runtime_version"]
            or answer.get("request_policy_version", "github_copilot.sdk_default.v1") != config.get("request_policy_version", "github_copilot.sdk_default.v1")):
        raise IntegrityError("github_copilot_source_configuration_mismatch")
    if answer.get("source_sha256") != text_sha256(answer["text"]):
        raise IntegrityError("github_copilot_source_hash_mismatch")
    if answer.get("session_id") != record.identity.get("session_id") or answer.get("request_id") != record.fence_id:
        raise IntegrityError("github_copilot_turn_binding_mismatch")
    return {"contract_version": "github_copilot.source.v1", "account_ref": config["account_ref"],
            "github_account_id": config["github_account_id"], "source_method": config["source_method"],
            **({"request_policy_version": config["request_policy_version"]} if "request_policy_version" in config else {}),
            "model": config["model"], "session_id": answer["session_id"], "request_id": record.fence_id,
            "source_sha256": answer["source_sha256"], "terminal": "completed", "capture": answer}


def _freeze_source_journal(worker, run_id, answer, evidence):
    evidence = dict(evidence)
    journal_path = answer.get("evidence_path")
    if journal_path:
        from pathlib import Path
        import hashlib
        path = Path(journal_path).resolve()
        if not path.is_relative_to((worker.repository.run_dir(run_id) / "sdk-sessions").resolve()):
            raise IntegrityError("github_copilot_journal_path_mismatch")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise IntegrityError("github_copilot_journal_too_large")
        journal = path.read_bytes()
        # Events may arrive after the terminal snapshot. Freeze only its exact
        # fsynced cursor prefix and verify the worker's digest, never truncate
        # source prose or silently discard unverified evidence.
        cursor = answer.get("event_cursor")
        if hashlib.sha256(journal).hexdigest() != answer.get("evidence_sha256"):
            prefix = bytearray()
            for line in journal.splitlines(keepends=True):
                prefix.extend(line)
                if json.loads(line).get("id") == cursor:
                    break
            journal = bytes(prefix)
        if hashlib.sha256(journal).hexdigest() != answer.get("evidence_sha256"):
            raise IntegrityError("github_copilot_journal_hash_mismatch")
        evidence["event_journal"] = worker.repository.write_evidence(run_id, journal, media_type="application/x-ndjson", suffix=".copilot-events.jsonl")
    return evidence


def _persist_source(worker, run_id, fences, record, answer, config):
    evidence = _freeze_source_journal(worker, run_id, answer, _source_evidence(answer, record, config))
    ref = worker.repository.write_evidence(run_id, answer["text"], suffix=".github-copilot-source.txt")
    worker.repository.write_evidence(run_id, json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                                     media_type="application/json", suffix=".github-copilot-capture.json")
    fences.capture_response(record.fence_id, response_reference=ref["path"], response_sha256=ref["sha256"],
                            response_evidence=evidence)
    if worker.recovery is not None:
        worker.recovery.captured(fences.load(record.fence_id))
    return fences.acknowledge(record.fence_id, validation_status="collected")


async def _capture_source(worker, run_id, plan, packet, observation, config):
    from .run_measurement_group import _CancellationRequested
    item_id = observation["observation_id"]
    fences = worker._send_attempts(run_id)
    records = [r for r in _records(worker, run_id, item_id) if r.action == "copilot_answer"]
    accepted = [r for r in records if r.state is SendAttemptState.ACKNOWLEDGED]
    if accepted:
        return accepted[-1]
    if any(r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in records):
        raise AmbiguousSendError("copilot_answer_unresolved")
    _check_account_failure(worker)
    logical_id = worker.recovery.logical_id(item_id, "copilot_answer") if worker.recovery is not None else f"{item_id}:copilot_answer"
    records = [r for r in records if r.normalized_logical_turn_id == logical_id]
    async with worker._copilot_source_limit:
        _check_account_failure(worker)
        with SendAttemptStore(worker.repository.run_dir(run_id) / "fences", watch_changes=False) as account_fences:
            if any(r.action == "copilot_answer" and r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED}
                   for r in account_fences.iter_records()):
                raise AmbiguousSendError("copilot_account_original_response_unresolved")
        source = getattr(worker, "_copilot_admitted_source", None)
        admitted = source is not None
        worker._copilot_admitted_source = None
        source = source if admitted else _source(worker, run_id, config)
        try:
            if not admitted:
                await worker._await_with_cancellation(run_id, asyncio.wait_for(source.initialize(), config["source_timeout_seconds"]),
                                                   detail="github_copilot_preflight_cancelled")
            while len(records) < config["max_source_attempts"]:
                ordinal = len(records) + 1
                fence_id = f"sdk-{text_sha256(logical_id)[:40]}-source-{ordinal}"
                session_id = str(uuid.uuid4())
                record = fences.prepare(fence_id=fence_id, observation_id=item_id, action="copilot_answer",
                    prompt_sha256=text_sha256(observation["prompt_text"]),
                    identity={"platform": "github_copilot", "account_ref": config["account_ref"],
                              "github_account_id": config["github_account_id"], "session_id": session_id},
                    logical_turn_id=logical_id, attempt_ordinal=ordinal,
                    retried_from=records[-1].fence_id if records else None)
                try:
                    await worker._await_with_cancellation(run_id, asyncio.wait_for(source.create_session(session_id), config["source_timeout_seconds"]),
                                                           detail="github_copilot_session_cancelled")
                    fences.mark_prepared(record.fence_id)
                except Exception as exc:
                    fences.fail_before_send(record.fence_id, "github_copilot_session_failed")
                    _finish_failed(worker, record)
                    records.append(fences.load(record.fence_id))
                    _retain_account_failure(worker, exc)
                    if worker._copilot_account_failure:
                        raise
                    if len(records) >= config["max_source_attempts"]:
                        raise
                    continue
                try:
                    fences.admit_send(record.fence_id)
                except _CancellationRequested:
                    worker.cancel_event.set()
                    raise asyncio.CancelledError()
                _stage(worker, run_id, observation, "source_inflight", session_id=session_id)
                try:
                    answer = await worker._await_with_cancellation(run_id,
                        asyncio.wait_for(source.send_once(session_id, observation["prompt_text"], record.fence_id,
                            timeout_seconds=config["source_timeout_seconds"]), config["source_timeout_seconds"]),
                        detail="github_copilot_source_cancelled")
                except BaseException as exc:
                    if getattr(exc, "submitted", None) is False:
                        record = fences.confirm_no_response(record.fence_id,
                            failure_classification="source_confirmed_not_sent",
                            reconciliation_evidence={"provider_receipt": "confirmed_not_sent", "code": getattr(exc, "code", "source_not_sent")},
                            retry_decision="retry_authorized" if ordinal < config["max_source_attempts"] else "retry_exhausted")
                        _finish_failed(worker, record, confirmed_not_sent=True)
                        records.append(record)
                        code = str(getattr(exc, "code", ""))
                        if _account_failure_code(code):
                            _retain_account_failure(worker, exc)
                            raise
                        if ordinal < config["max_source_attempts"]:
                            continue
                    # Cancel only this owned session. Cancellation does not prove
                    # non-submission and therefore leaves the send fence uncertain.
                    try:
                        await asyncio.wait_for(source.cancel(session_id), 5)
                    except BaseException:
                        pass
                    raise
                if answer.get("status") != "completed":
                    code = str(answer.get("code", ""))
                    if _account_failure_code(code):
                        worker._copilot_account_failure = code
                    answer = await worker._await_with_cancellation(run_id,
                        asyncio.wait_for(source.inspect_session(session_id, record.fence_id), config["reconciliation_grace_seconds"]),
                        detail="github_copilot_reconciliation_cancelled")
                return _persist_source(worker, run_id, fences, record, answer, config)
            raise IntegrityError("github_copilot_source_attempts_exhausted")
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if isinstance(exc, AmbiguousSendError) or any(r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in _records(worker, run_id, item_id) if r.action == "copilot_answer"):
                worker._copilot_account_failure = worker._copilot_account_failure or "copilot_source_unresolved"
            _retain_account_failure(worker, exc)
            raise
        finally:
            await source.close()


def _result(worker, run_id, observation, source_record, parser_record=None, *, error=None):
    source_text, source_ref = _read(worker, run_id, source_record)
    prompt = source_parser_prompt(source_text)
    grounded = []
    artifacts = {"turn_1_response": source_ref, "github_copilot_source": source_ref}
    accepted = {"copilot_answer": source_record.fence_id}
    evidence = dict(source_record.response_evidence or {})
    if evidence.get("event_journal"):
        artifacts["source_event_journal"] = evidence["event_journal"]
    if parser_record is not None:
        response, parser_ref = _read(worker, run_id, parser_record)
        artifacts["turn_2_response"] = parser_ref
        if error is None:
            grounded = validate_source_names(source_text, response, source_sha256=source_ref["sha256"], prompt=prompt)
            accepted["gemini_parse"] = parser_record.fence_id
        evidence["parser_identity"] = dict(parser_record.ack_identity or parser_record.identity)
    prompt_ref = worker.repository.write_evidence(run_id, prompt, suffix=".source-parser-prompt.txt")
    artifacts["parser_prompt"] = prompt_ref
    evidence.update(parser_contract_version=CONTRACT_VERSION, parser_prompt_sha256=prompt_ref["sha256"],
                    source_saved=True, stage="accepted" if error is None else "parser_failed")
    issues = [] if error is None else [{"observation_id": observation["observation_id"], "code": error,
             "detail": "Copilot answer saved; Gemini parsing did not complete.", "recoverable": True}]
    records = _records(worker, run_id, observation["observation_id"])
    return {"observation_id": observation["observation_id"], "platform": "github_copilot",
            "status": "succeeded" if error is None else "partial", "collection_status": "collected",
            "extraction_status": "complete" if error is None else "rejected",
            "prompt_sha256": text_sha256(observation["prompt_text"]), "capture_evidence": evidence,
            "grounded_occurrences": grounded, "artifacts": artifacts, "issues": issues,
            "attempt_ids": [r.fence_id for r in records], "accepted_attempt_ids": accepted,
            "accepted_attempt_id": parser_record.fence_id if parser_record is not None and error is None else source_record.fence_id}


async def run_observation(worker, run_id, plan, packet, observation):
    from .run_measurement_group import _retry_policy, _profile_identity
    config = _config(plan, observation)
    runtime_config = json_value(getattr(worker, "copilot_runtime", None)) or {}
    if any(runtime_config.get(key) != config.get(key) for key in ("account_ref", "github_account_id")):
        raise IntegrityError("github_copilot_runtime_account_mismatch")
    item_id = observation["observation_id"]
    _stage(worker, run_id, observation, "source_pending")
    source_record = await _capture_source(worker, run_id, plan, packet, observation, config)
    source_text, source_ref = _read(worker, run_id, source_record)
    prompt = source_parser_prompt(source_text)
    _stage(worker, run_id, observation, "source_saved", source_sha256=source_ref["sha256"])
    fences = worker._send_attempts(run_id)
    records = [r for r in _records(worker, run_id, item_id) if r.action == "gemini_parse"]
    accepted = [r for r in records if r.state is SendAttemptState.ACKNOWLEDGED]
    if accepted:
        return _result(worker, run_id, observation, source_record, accepted[-1])
    if any(r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in records):
        raise AmbiguousSendError("gemini_parse_unresolved")
    logical_id = worker.recovery.logical_id(item_id, "gemini_parse") if worker.recovery is not None else f"{item_id}:gemini_parse"
    records = [r for r in records if r.normalized_logical_turn_id == logical_id]
    profile_id, directory = _profile_identity(plan, packet, observation)
    driver = worker.providers.require("gemini")
    policy = replace(_retry_policy(plan), enabled=True, max_attempts=config["max_parser_attempts"],
                     late_response_grace_seconds=config["reconciliation_grace_seconds"])
    last = records[-1] if records else None
    open_failures = 0
    while len(records) < config["max_parser_attempts"]:
        _stage(worker, run_id, observation, "parser_pending", source_sha256=source_ref["sha256"])
        try:
            submission = await worker._await_with_cancellation(run_id,
                driver.open(profile_id=profile_id, chrome_directory=directory, observation_id=item_id, fresh=True),
                detail="gemini_parser_open_cancelled")
        except Exception:
            open_failures += 1
            if open_failures + len(records) >= config["max_parser_attempts"] or worker._is_cancelled(run_id):
                raise
            continue
        try:
            use_timeout = getattr(submission, "use_extraction_timeout", None)
            if callable(use_timeout):
                use_timeout(config["parser_timeout_seconds"])
            ordinal = len(records) + 1
            record = fences.prepare(fence_id=f"{logical_id}:{ordinal}", observation_id=item_id,
                action="gemini_parse", prompt_sha256=text_sha256(prompt), identity=submission.identity.to_mapping(),
                logical_turn_id=logical_id, attempt_ordinal=ordinal,
                retried_from=records[-1].fence_id if records else None)
            _stage(worker, run_id, observation, "parser_inflight", source_sha256=source_ref["sha256"])
            try:
                response, _, identity = await asyncio.wait_for(worker._send(run_id, fences, record, submission, prompt,
                    packet_id=packet["packet_id"], acknowledge=False, policy=policy),
                    config["parser_timeout_seconds"] + config["reconciliation_grace_seconds"])
                validate_source_names(source_text, response, source_sha256=source_ref["sha256"], prompt=prompt)
            except MentionExtractionError:
                last = fences.acknowledge(record.fence_id, validation_status="rejected")
                records.append(last)
                continue
            except Exception:
                last = fences.load(record.fence_id)
                if last.state in {SendAttemptState.PREPARING, SendAttemptState.PREPARED}:
                    last = fences.fail_before_send(record.fence_id, "gemini_parser_prepare_failed")
                if last.state is SendAttemptState.PRE_SEND_FAILED:
                    _finish_failed(worker, last)
                    records.append(last)
                    continue
                raise
            last = fences.acknowledge(record.fence_id, validation_status="accepted", identity=identity.to_mapping())
            _stage(worker, run_id, observation, "accepted", source_sha256=source_ref["sha256"])
            return _result(worker, run_id, observation, source_record, last)
        finally:
            await submission.close()
    return _result(worker, run_id, observation, source_record,
                   last if last is not None and last.response_reference else None, error="extraction_rejected")


async def execute_packet(worker, *, run_id, plan, packet, resume=False):
    from .run_measurement_group import MeasurementGroupOutcome
    rows = {r["observation_id"]: r for r in plan["observation_plan"]["observations"]}
    issues, completed = [], []
    evidence_exists = False
    existing = {r["observation_id"]: r for r in worker.repository.iter_observation_results(run_id)}
    for item_id in packet["observation_ids"]:
        observation = rows[item_id]
        if observation["platform"] != "github_copilot" or packet["platform"] != "github_copilot":
            raise IntegrityError("source_parser_packet_identity_mismatch")
        if worker.recovery is not None and item_id not in worker.recovery.selected:
            completed.append(item_id)
            continue
        if worker._durable_is_cancelled(run_id):
            break
        if existing.get(item_id, {}).get("status") == "succeeded":
            completed.append(item_id)
            continue
        token = worker._begin_operation_scope(run_id, packet["packet_id"], item_id)
        try:
            if worker.recovery is not None and not await worker.recovery.prepare_item(worker, item_id, observation, packet):
                issues.append({"observation_id": item_id, "code": "needs_attention", "detail": "Original source/parser send is unresolved.", "recoverable": False})
                continue
            result = await run_observation(worker, run_id, plan, packet, observation)
            worker.repository.write_observation_result(run_id, item_id, result)
            completed.append(item_id)
            issues.extend(result["issues"])
            evidence_exists = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never copy SDK/provider exception text (it can contain tokens).
            code = getattr(exc, "code", None) or ("ambiguous_send" if isinstance(exc, AmbiguousSendError) else "source_parser_failed")
            issues.append({"observation_id": item_id, "code": code,
                           "detail": "GitHub Copilot source/parser operation needs attention.", "recoverable": True,
                           **({"capability_detail": exc.capability_detail} if isinstance(exc, CopilotSourceError)
                              and exc.capability_detail is not None else {})})
            source_records = [r for r in _records(worker, run_id, item_id) if r.action == "copilot_answer" and r.state is SendAttemptState.ACKNOWLEDGED]
            if source_records:
                try:
                    partial = _result(worker, run_id, observation, source_records[-1], error=code)
                    if any(r.state in {SendAttemptState.SEND_STARTED, SendAttemptState.RESPONSE_CAPTURED} for r in _records(worker, run_id, item_id)):
                        partial.update(status="needs_attention", extraction_status="needs_attention")
                    worker.repository.write_observation_result(run_id, item_id, partial)
                    evidence_exists = True
                    completed.append(item_id)
                except MentionExtractionError:
                    pass
            if worker.recovery is not None:
                worker.recovery.failure(item_id, code)
        finally:
            worker._finish_operation_scope(run_id, packet["packet_id"], item_id, token)
    all_records = [r for item_id in packet["observation_ids"] for r in _records(worker, run_id, item_id)]
    counts = count_attempt_milestones(all_records)
    status = ("cancelled" if worker._is_cancelled(run_id) else "needs_attention" if any(r["code"] in {"ambiguous_send", "needs_attention"} for r in issues)
              else "partial" if issues and evidence_exists else "failed" if issues else "succeeded" if len(completed) == len(packet["observation_ids"]) else "partial")
    def update(state):
        state.update(status=status, issues=issues, transport_counts=counts, completed_observation_ids=completed)
    worker.repository.update_packet_state(run_id, packet["packet_id"], update)
    return MeasurementGroupOutcome(packet["packet_id"], status, tuple(completed), tuple(issues), counts)
