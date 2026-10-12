"""Pure custom-prompt planning; normal transports and recovery own execution."""
from __future__ import annotations

import hashlib
from typing import Mapping

from ..config import COPILOT_BROWSER_MEASUREMENT_MODE, DIAGNOSTIC_POLICY_VERSION, DIAGNOSTIC_PROMPT_CATALOG_VERSION, MeasurementConfig
from .catalog import BROWSER_MEASUREMENT_MODE, NAME_RECONCILIATION_ARTIFACT_HASH, NAME_RECONCILIATION_POLICY_VERSION
from .hashing import canonical_sha256, json_value


def prompt_rows(scope: Mapping) -> list[dict]:
    platform = scope["platforms"][0]
    rows = []
    for index, text in enumerate(scope["diagnostic_prompts"], 1):
        identity = {"diagnostic_policy_version": DIAGNOSTIC_POLICY_VERSION, "diagnostic_index": index,
                    "platform": platform, "text": text}
        prompt_id = f"diagnostic-{canonical_sha256(identity)[:24]}"
        rows.append({"prompt_id": prompt_id, "semantic_prompt_id": prompt_id,
                     "diagnostic_index": index, "platform": platform, "text": text,
                     "prompt_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                     "collection_scope_id": f"diagnostic:{platform}", "prompt_intent": "diagnostic",
                     "report_eligible": False})
    return rows


def prompt_hash_payload(scope: Mapping, rows: list[dict]) -> dict:
    return {"schema_version": "visibility_runner.diagnostic_prompts.v1",
            "snapshot_id": scope["snapshot_id"], "prompt_pack_version": DIAGNOSTIC_PROMPT_CATALOG_VERSION,
            "report_eligible": False, "prompts": rows}


def freeze(config: MeasurementConfig | Mapping, *, snapshot_id: str) -> tuple[dict, list[dict]]:
    measurement = config if isinstance(config, MeasurementConfig) else MeasurementConfig.from_mapping(config)
    if measurement.scope_kind != "diagnostic":
        raise ValueError("diagnostic planner requires diagnostic scope")
    platform = measurement.platforms[0].value
    scope = {
        "schema_version": "visibility_runner.diagnostic_scope.v1",
        "scope_kind": "diagnostic", "snapshot_id": snapshot_id,
        "diagnostic_policy_version": DIAGNOSTIC_POLICY_VERSION,
        "diagnostic_prompts": list(measurement.diagnostic_prompts),
        "diagnostic_prompt_count": len(measurement.diagnostic_prompts),
        "platforms": [platform],
        "platform_data_modes": {platform: "github_copilot_sdk_source_gemini_parser_v1" if platform == "github_copilot" else COPILOT_BROWSER_MEASUREMENT_MODE if platform == "copilot" else BROWSER_MEASUREMENT_MODE},
        "planned_runs": 1, "test_mode": False,
        "outside_in": measurement.outside_in,
        "prompt_pack_version": DIAGNOSTIC_PROMPT_CATALOG_VERSION,
        "execution_policy_version": measurement.execution_policy_version,
        "publication_eligible": False,
        "platform_name_reconciliation": True,
        "name_reconciliation_policy_version": NAME_RECONCILIATION_POLICY_VERSION,
        "name_reconciliation_artifact_hash": NAME_RECONCILIATION_ARTIFACT_HASH,
        **({"github_copilot": measurement.github_copilot.to_mapping()} if measurement.github_copilot else {}),
    }
    rows = prompt_rows(scope)
    scope["prompt_pack_hash"] = canonical_sha256(prompt_hash_payload(scope, rows))
    scope["scope_hash"] = canonical_sha256(scope)
    return scope, rows


def validate(scope: Mapping) -> list[dict]:
    """Rebuild the entire diagnostic manifest, rejecting unbound extra fields."""
    config = {"scope_kind": "diagnostic", "diagnostic_prompts": scope.get("diagnostic_prompts"),
              "platforms": scope.get("platforms"), "repetitions": scope.get("planned_runs"),
              "outside_in": scope.get("outside_in"), "prompt_catalog_version": scope.get("prompt_pack_version"),
              "execution_policy_version": scope.get("execution_policy_version"),
              **({"github_copilot": scope["github_copilot"]} if "github_copilot" in scope else {})}
    snapshot_id = scope.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("diagnostic scope requires snapshot_id")
    expected, rows = freeze(config, snapshot_id=snapshot_id)
    # Scope manifests in immutable RunPlans contain tuples/mapping proxies.
    if json_value(scope) != expected:
        raise ValueError("diagnostic scope differs from its frozen contract")
    return rows


def observation_plan(scope: Mapping, prompt_pack: Mapping) -> dict:
    platform = scope["platforms"][0]
    rows = []
    for prompt in prompt_pack["prompts"]:
        identity = {"snapshot_id": scope["snapshot_id"], "prompt_id": prompt["prompt_id"], "run": 1, "platform": platform}
        row = {"observation_id": f"obs-diagnostic-{canonical_sha256(identity)[:24]}",
               "scope_id": prompt["collection_scope_id"], "scope_hash": scope["scope_hash"],
               "prompt_pack_hash": scope["prompt_pack_hash"], "prompt_id": prompt["prompt_id"],
               "prompt_intent": "diagnostic", "prompt_text": prompt["text"],
               "prompt_text_sha256": prompt["prompt_text_sha256"], "diagnostic_index": prompt["diagnostic_index"],
               "run": 1, "platform": platform, "model_or_surface": platform,
               "access_mode": "browser", "location_context": {}, "report_eligible": False,
               "started_at": None, "collected_at": None, "status": None,
               "raw_answer_reference": None, "raw_answer_sha256": None, "citations": [], "attempts": 0}
        if platform == "github_copilot":
            row.update({"access_mode": "sdk", "model_or_surface": scope["github_copilot"]["model"],
                        "github_copilot": json_value(scope["github_copilot"]), "parser_platform": "gemini",
                        "submission_topology_version": "visibility_runner.submission_topology.v2"})
        rows.append(row)
    payload = {"schema_version": "visibility_runner.diagnostic_observations.v1",
               "snapshot_id": scope["snapshot_id"], "scope_hash": scope["scope_hash"],
               "prompt_pack_hash": scope["prompt_pack_hash"], "prompt_pack_version": DIAGNOSTIC_PROMPT_CATALOG_VERSION,
               "observations": rows}
    return {**payload, "observation_plan_hash": canonical_sha256(payload), "planned_observations": len(rows)}
