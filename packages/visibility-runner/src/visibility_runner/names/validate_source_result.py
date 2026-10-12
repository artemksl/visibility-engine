"""Verify source/parser evidence before publication or read-only inspection."""
from __future__ import annotations
from ..errors import IntegrityError
from ..storage.send_attempts import SendAttemptStore, SendAttemptState
from .parse_source_answer import CONTRACT_VERSION, source_parser_prompt, text_sha256, validate_source_names


def validate_source_result(repository, run_id, observation, result, plan, attempt_records=None):
    if result.get("platform") != "github_copilot":
        raise IntegrityError("github_copilot_result_platform_mismatch")
    if result.get("collection_status") != "collected":
        if result.get("status") == "succeeded":
            raise IntegrityError("github_copilot_source_missing")
        return result
    config = observation.get("github_copilot") or plan["scope_manifest"].get("github_copilot") or plan["measurement_config"].get("github_copilot")
    artifacts, evidence = result.get("artifacts") or {}, result.get("capture_evidence") or {}
    ids = result.get("accepted_attempt_ids") or {}
    if attempt_records is None:
        with SendAttemptStore(repository.run_dir(run_id) / "fences", watch_changes=False) as store:
            attempt_records = {r.fence_id: r for r in store.snapshot(observation_ids=(observation["observation_id"],)).records}
    source = attempt_records.get(ids.get("copilot_answer"))
    ref = artifacts.get("turn_1_response")
    if source is None or not ref:
        raise IntegrityError("github_copilot_source_fence_missing")
    if (source.state is not SendAttemptState.ACKNOWLEDGED or source.action != "copilot_answer"
            or source.observation_id != observation["observation_id"] or source.prompt_sha256 != text_sha256(observation["prompt_text"])
            or source.response_reference != ref.get("path") or source.response_sha256 != ref.get("sha256")
            or source.identity.get("account_ref") != config["account_ref"]
            or source.identity.get("github_account_id") != config["github_account_id"]):
        raise IntegrityError("github_copilot_source_fence_mismatch")
    raw = repository.read_evidence(run_id, ref).decode("utf-8")
    frozen_evidence = source.response_evidence or {}
    if (not raw.strip() or frozen_evidence.get("source_sha256") != text_sha256(raw)
            or frozen_evidence.get("terminal") != "completed"
            or frozen_evidence.get("source_method") != config["source_method"]
            or frozen_evidence.get("session_id") != source.identity.get("session_id")
            or frozen_evidence.get("request_id") != source.fence_id):
        raise IntegrityError("github_copilot_source_evidence_mismatch")
    capture = frozen_evidence.get("capture") or {}
    captured_accounts = {str(capture.get("github_account_id", "")).casefold(), str(capture.get("github_login", "")).casefold()}
    if (any(frozen_evidence.get(key) != config[key] for key in ("account_ref", "github_account_id", "source_method", "model"))
            or capture.get("account_ref") != config["account_ref"]
            or str(config["github_account_id"]).casefold() not in captured_accounts
            or capture.get("sdk_version") != config["sdk_version"]
            or capture.get("runtime_version") != config["runtime_version"]
            or capture.get("source_method") != config["source_method"]
            or capture.get("model_policy") != config["model"]
            or capture.get("session_id") != source.identity.get("session_id")
            or capture.get("request_id") != source.fence_id
            or capture.get("source_sha256") != text_sha256(raw)
            or capture.get("status") != "completed" or capture.get("text") != raw):
        raise IntegrityError("github_copilot_capture_configuration_mismatch")
    if frozen_evidence.get("event_journal"):
        import hashlib
        journal = repository.read_evidence(run_id, frozen_evidence["event_journal"])
        if hashlib.sha256(journal).hexdigest() != capture.get("evidence_sha256"):
            raise IntegrityError("github_copilot_journal_changed")
    if any(evidence.get(k) != v for k, v in frozen_evidence.items()):
        raise IntegrityError("github_copilot_result_evidence_changed")
    prompt = source_parser_prompt(raw)
    if (evidence.get("parser_contract_version") != CONTRACT_VERSION
            or evidence.get("parser_prompt_sha256") != text_sha256(prompt)
            or repository.read_evidence(run_id, artifacts["parser_prompt"]).decode("utf-8") != prompt):
        raise IntegrityError("github_copilot_parser_prompt_mismatch")
    if result.get("status") == "succeeded":
        parser = attempt_records.get(ids.get("gemini_parse"))
        parser_ref = artifacts.get("turn_2_response") or {}
        profiles = {p["profile_id"] for p in plan["packets"] if observation["observation_id"] in p["observation_ids"] and p.get("kind") == "source_parser"}
        if (parser is None or parser.state is not SendAttemptState.ACKNOWLEDGED or parser.action != "gemini_parse"
                or parser.validation_status != "accepted" or parser.observation_id != observation["observation_id"]
                or parser.prompt_sha256 != text_sha256(prompt) or parser.identity.get("profile_id") not in profiles
                or parser.response_reference != parser_ref.get("path") or parser.response_sha256 != parser_ref.get("sha256")
                or result.get("extraction_status") != "complete"):
            raise IntegrityError("github_copilot_parser_fence_mismatch")
        response = repository.read_evidence(run_id, parser_ref).decode("utf-8")
        expected = validate_source_names(raw, response, source_sha256=ref["sha256"], prompt=prompt)
        if result.get("grounded_occurrences") != expected:
            raise IntegrityError("github_copilot_grounding_changed")
    return result
