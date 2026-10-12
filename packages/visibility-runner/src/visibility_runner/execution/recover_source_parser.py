"""Read existing SDK/browser replies for uncertain source/parser sends."""
from __future__ import annotations
import asyncio
from ..storage.send_attempts import SendAttemptStore, SendAttemptState
from ..names.parse_source_answer import source_parser_prompt, validate_source_names
from ..names.check_chat_names import MentionExtractionError
from .run_source_parser_observation import _config, _source, _read, _source_evidence, _freeze_source_journal


async def reconcile_source_parser(worker, context, observation, packet, records, uncertain):
    if not uncertain:
        return True
    run_id = context.snapshot.manifest.run_id
    item_id = observation["observation_id"]
    config = _config(context.verified.plan, observation)
    # Unfiltered historical records belong to this new protocol only.
    with SendAttemptStore(worker.repository.run_dir(run_id) / "fences", watch_changes=False) as fences:
        for original in uncertain:
            attempt = context.coordinator.begin_attempt(context.pass_id, f"{item_id}:{original.action}",
                context.owner, action="reconcile", predecessor_ids=(original.fence_id,))
            source = submission = None
            source_slot = False
            try:
                current = fences.load(original.fence_id)
                if current.state is SendAttemptState.RESPONSE_CAPTURED:
                    text, reference = _read(worker, run_id, current)
                    evidence = current.response_evidence
                    identity = current.identity
                elif original.action == "copilot_answer":
                    await worker._await_with_cancellation(run_id, worker._copilot_source_limit.acquire(), detail="copilot_recovery_queue_cancelled")
                    source_slot = True
                    source = _source(worker, run_id, config)
                    await worker._await_with_cancellation(run_id, asyncio.wait_for(source.initialize(), config["source_timeout_seconds"]),
                                                         detail="copilot_recovery_preflight_cancelled")
                    answer = await worker._await_with_cancellation(run_id,
                        asyncio.wait_for(source.inspect_session(original.identity["session_id"], original.fence_id),
                                         max(1, config["reconciliation_grace_seconds"])), detail="copilot_recovery_inspection_cancelled")
                    evidence = _freeze_source_journal(worker, run_id, answer, _source_evidence(answer, original, config))
                    text = answer["text"]
                    identity = original.identity
                    reference = worker.repository.write_evidence(run_id, text, suffix=".github-copilot-source.txt")
                    fences.capture_response(original.fence_id, response_reference=reference["path"],
                                            response_sha256=reference["sha256"], response_evidence=evidence)
                else:
                    binding = context.correlation(original.fence_id)
                    driver = worker.providers.require("gemini")
                    if not binding or not callable(getattr(driver, "reopen", None)):
                        raise ValueError("parser_conversation_binding_missing")
                    # Use the original immutable source to reconstruct exactly
                    # the submitted parser prompt, never regenerate the source.
                    source_record = next(r for r in reversed(records) if r.action == "copilot_answer" and r.state is SendAttemptState.ACKNOWLEDGED)
                    source_text, _ = _read(worker, run_id, source_record)
                    prompt = source_parser_prompt(source_text)
                    submission = await worker._await_with_cancellation(run_id, driver.reopen(binding, observation_id=item_id),
                                                                        detail="source_parser_reopen_cancelled")
                    submission.restore_recovery_attempt(prompt=prompt, response_baseline=binding["response_baseline"], binding=binding)
                    outcome = await worker._await_with_cancellation(run_id,
                        submission.reconcile_no_response(config["reconciliation_grace_seconds"]), detail="source_parser_reconcile_cancelled")
                    answer = getattr(outcome, "answer", None)
                    if answer is None:
                        raise ValueError("parser_original_response_unresolved")
                    text, identity, evidence = answer.text, answer.identity.to_mapping(), None
                    reference = worker.repository.write_evidence(run_id, text, suffix=".source-parser-recovered.txt")
                    fences.capture_response(original.fence_id, response_reference=reference["path"], response_sha256=reference["sha256"])
                validation = "collected"
                if original.action == "gemini_parse":
                    source_record = next(r for r in reversed(records) if r.action == "copilot_answer" and r.state is SendAttemptState.ACKNOWLEDGED)
                    source_text, source_ref = _read(worker, run_id, source_record)
                    try:
                        validate_source_names(source_text, text, source_sha256=source_ref["sha256"], prompt=source_parser_prompt(source_text))
                        validation = "accepted"
                    except MentionExtractionError:
                        validation = "rejected"
                acknowledged = fences.acknowledge(original.fence_id, validation_status=validation, identity=identity)
                context.resolve_original_submission(original, attempt, reference)
                context.coordinator.finish_attempt(attempt.attempt_id, "completed", context.owner)
                context.recovered.add(original.fence_id)
                records = [r if r.fence_id != original.fence_id else acknowledged for r in records]
            except asyncio.CancelledError:
                raise
            except Exception:
                if original.action == "copilot_answer":
                    worker._copilot_account_failure = "copilot_source_unresolved"
                context.coordinator.finish_attempt(attempt.attempt_id, "failed", context.owner, reason_code="source_parser_original_unresolved")
                context.failure(item_id, "source_parser_original_unresolved", checked=(original.fence_id,))
                return False
            finally:
                try:
                    if source is not None:
                        await source.close()
                finally:
                    if source_slot:
                        worker._copilot_source_limit.release()
                if submission is not None:
                    await submission.close()
    return True
