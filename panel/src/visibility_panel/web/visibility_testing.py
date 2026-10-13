"""Admission and read-only previews for runner-owned diagnostic prompt batches.

These helpers never create jobs, resolve credentials, start browsers, or send
prompts. The web handler submits admitted configurations to the existing worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from visibility_panel.integrations.visibility_runner import coerce_measurement_config
from visibility_runner.api import summarize_measurement
from visibility_panel.web.config import (
    selected_profile_ids,
    github_copilot_defaults,
    selected_snapshot_profile_rows,
)


DEFAULT_VISIBILITY_TEST_PROMPT = "Recommend good clinics for laser hair removal in Marylebone."
VISIBILITY_TEST_PLATFORMS = {
    "chatgpt": "chatgpt",
    "gemini": "gemini",
    "copilot": "copilot",
    "copilot-gemini": "github_copilot",
}
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,128}", re.ASCII)
_PAYLOAD_FIELDS = frozenset({"request_id", "prompt_count", "prompt", "prompts", "selected_profile_ids"})


def _diagnostic_prompts(payload: Mapping) -> list[str]:
    if "prompt" in payload and "prompts" in payload:
        raise ValueError("Supply either prompt or prompts, not both")
    count = payload.get("prompt_count")
    if "prompt_count" in payload and (type(count) is not int or count < 1):
        raise ValueError("prompt_count must be a positive integer")
    if "prompts" in payload:
        prompts = payload["prompts"]
        if not isinstance(prompts, list) or not prompts:
            raise ValueError("prompts must be a nonempty list of nonblank strings")
        if any(not isinstance(prompt, str) or not prompt.strip() for prompt in prompts):
            raise ValueError("prompts must be a nonempty list of nonblank strings")
        if count is not None and count != len(prompts):
            raise ValueError("prompt_count must equal the number of supplied prompts")
        # Do not strip, deduplicate, or reorder the user's measurement prompts.
        return list(prompts)
    prompt = payload.get("prompt", DEFAULT_VISIBILITY_TEST_PROMPT)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a nonblank string")
    return [prompt] * (count if count is not None else 1)


def build_visibility_test_config(
    platform_slug: str, payload: object, root: Path | None = None,
) -> dict:
    """Build one platform's diagnostic run without launching work.

    There is no application-level prompt-count ceiling. A repeated prompt is a
    distinct observation for each occurrence, distributed over selected profiles.
    """
    if not isinstance(platform_slug, str) or platform_slug not in VISIBILITY_TEST_PLATFORMS:
        raise ValueError("Choose a visibility test endpoint: chatgpt, gemini, copilot, or copilot-gemini")
    if not isinstance(payload, Mapping):
        raise ValueError("Visibility test request must be a JSON object")
    if set(payload) - _PAYLOAD_FIELDS:
        # Never echo arbitrary supplied keys or values, which may contain secrets.
        raise ValueError("Visibility test request contains unsupported fields")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("request_id must contain 8–128 letters, numbers, underscores, or hyphens")
    prompts = _diagnostic_prompts(payload)
    permitted = selected_profile_ids(root)
    selected = payload.get("selected_profile_ids", permitted)
    if (not isinstance(selected, list) or not selected
        or any(not isinstance(value, str) or value not in permitted for value in selected)
        or len(set(selected)) != len(selected)):
        raise ValueError("Select at least one configured profile, without duplicate or unknown IDs")
    platform = VISIBILITY_TEST_PLATFORMS[platform_slug]
    measurement = {
        "scope_kind": "diagnostic",
        "diagnostic_prompts": prompts,
        "platforms": [platform],
        "planned_runs": 1,
    }
    result = {
        "request_id": request_id,
        "runs": "single",
        "selected_profile_ids": list(selected),
        "use_all_configured_profiles": False,
        "audit_platforms": [platform],
        "visibility_test": measurement,
    }
    if platform == "github_copilot":
        defaults = github_copilot_defaults(root)
        if defaults is None:
            raise ValueError("Configure GitHub Copilot account settings before running copilot-gemini tests")
        source = {key: value for key, value in defaults.items() if key != "credential_ref"}
        source["repetitions"] = 1
        runtime = {key: defaults[key] for key in ("account_ref", "github_account_id", "credential_ref")}
        measurement["github_copilot"] = dict(source)
        result["github_copilot"] = source
        result["github_copilot_runtime"] = runtime
    return result


def preview_visibility_test(
    platform_slug: str, payload: object, root: Path | None = None,
) -> dict:
    """Validate prompt and local configuration contracts without provider calls."""
    config = build_visibility_test_config(platform_slug, payload, root)
    selected_snapshot_profile_rows(config["selected_profile_ids"], root)
    coerce_measurement_config(config["visibility_test"])
    measurement = config["visibility_test"]
    summary = summarize_measurement(measurement)
    count = summary["counts"]["planned_observation_count"]
    topology = summary["submission_topology"]
    return {
        "request_id": config["request_id"],
        "platform": VISIBILITY_TEST_PLATFORMS[platform_slug],
        "prompt_count": count,
        "selected_profile_ids": list(config["selected_profile_ids"]),
        "measurement": measurement,
        "initial_request_counts": {
            "source": topology["turn_1_messages"] + topology.get("sdk_source_messages", 0),
            "extraction": topology["turn_2_messages"] + topology.get("source_parser_messages", 0),
            "total": topology["total_external_submissions"],
        },
        "count_semantics": "Exactly prompt_count initial observations, distributed across the selected profiles; retries may add requests.",
        "job_created": False,
        "provider_requests_sent": 0,
        "publication_eligible": False,
    }


def visibility_test_options(root=None) -> dict:
    """Describe the diagnostic API and explicit local profile selection without provider calls."""
    base = "/api/testing/visibility"
    permitted = selected_profile_ids(root)
    return {
        "schema_version": "visibility_testing.v1",
        "endpoints": {
            slug: {
                "platform": platform,
                "start": {"method": "POST", "path": f"{base}/{slug}"},
                "preview": {"method": "POST", "path": f"{base}/{slug}/preview"},
            }
            for slug, platform in VISIBILITY_TEST_PLATFORMS.items()
        },
        "default_prompt": DEFAULT_VISIBILITY_TEST_PROMPT,
        "request_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request_id"],
            "properties": {
                "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,128}$"},
                "prompt_count": {"type": "integer", "minimum": 1},
                "prompt": {"type": "string", "minLength": 1},
                "prompts": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "selected_profile_ids": {
                    "type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "string", "enum": permitted},
                    "default": permitted,
                },
            },
            "not": {"required": ["prompt", "prompts"]},
        },
        "count_semantics": (
            "A count-only request repeats the default prompt in that many fresh conversations. "
            "With prompt, that exact text is repeated; with prompts, each item is one observation "
            "and prompt_count is inferred or must equal the list length. Omitting both prompts "
            "and prompt_count uses the default prompt once. There is no configured count ceiling. "
            "The count is total across profiles, not multiplied by profile count; retries may add requests."
        ),
        "profile_ids": permitted,
        "start_behavior": (
            "POST to a start endpoint creates a live diagnostic job using the visibility runner. "
            "The job sends browser or Copilot requests and may consume provider credits. "
            "ChatGPT, Gemini and Copilot answer then extract names in the same fresh browser conversation; "
            "copilot-gemini saves the GitHub Copilot SDK answer and uses a fresh Gemini conversation to parse it."
        ),
        "preview_behavior": "Preview only validates local settings and prompts; it creates no job and sends no provider requests.",
        "copilot_behavior": "Uses Microsoft Copilot in the selected Chrome profiles; answer and extraction share one fresh conversation.",
        "github_copilot_behavior": "The separate copilot-gemini endpoint uses the configured SDK account, source method and request policy; unavailable web capabilities remain blocking.",
        "publication_eligible": False,
    }
