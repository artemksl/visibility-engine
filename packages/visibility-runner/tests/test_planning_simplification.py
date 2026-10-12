"""Observable planning summaries, send counts and historical refusals."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

from visibility_runner import GitHubCopilotConfig, MeasurementConfig, Platform
from visibility_runner.planning.build_measurement_plan import (
    freeze_snapshot,
    preview_snapshot_counts,
    summarize_measurement,
)
from visibility_runner.planning.count_allowed_sends import (
    build_submission_budget,
    exact_chat_message_budget,
)

from support.planning import config, plan


def measurement(case):
    if case == "current_browser":
        return config(
            platforms=(Platform.CHATGPT, Platform.GEMINI, Platform.COPILOT),
            repetitions=2,
        )
    values = {
        "scope_kind": "diagnostic",
        "diagnostic_prompts": ("  Clinics?\n", "  Clinics?\n", "München?"),
        "platforms": (
            Platform.GITHUB_COPILOT if case == "diagnostic_sdk" else Platform.COPILOT,
        ),
    }
    if case == "diagnostic_sdk":
        values["github_copilot"] = GitHubCopilotConfig(
            account_ref="student", github_account_id="1234",
            max_source_attempts=3, max_parser_attempts=1,
        )
    return MeasurementConfig(**values)


@pytest.mark.parametrize("case", ["current_browser", "diagnostic_browser", "diagnostic_sdk"])
def test_pure_summary_matches_plan_without_profiles_or_allocation(case):
    value = measurement(case)
    result = plan(value)
    summary = summarize_measurement(value)
    assert summary == summarize_measurement(value.to_snapshot_mapping())
    assert summary["counts"] == preview_snapshot_counts(value.to_snapshot_mapping())
    for name in (
        "platform_prompt_count", "semantic_prompt_count", "planned_observation_count",
        "platform_prompt_counts", "platform_observation_counts",
    ):
        # The public preview historically omits the per-platform observation map.
        if name in summary["counts"]:
            assert summary["counts"][name] == getattr(result.counts, name)
    assert summary["submission_topology"] == result.submission_budget.initial_topology.to_mapping()
    assert summary["submission_topology"]["total_external_submissions"] == (
        2 * summary["counts"]["planned_observation_count"]
    )
    assert summary["submission_topology"].get("sdk_source_messages", 0) == (
        3 if case == "diagnostic_sdk" else 0
    )


def test_legacy_google_and_derived_counts_remain_readable():
    observations = [
        {"platform": "chatgpt"},
        {"platform": "copilot", "platform_data_mode": "legacy_derived"},
        {"platform": "google_ai_overview"},
        {"platform": "google_ai_overview", "status": "not_shown"},
        {"platform": "google_ai_overview", "status": "collected",
         "generated_answer_capture_version": "google-ai-overview-generated-answer-v1"},
        {"platform": "unknown"},
    ]
    expected = {
        "conversational_chats": 1, "turn_1_messages": 1, "turn_2_messages": 1,
        "conversational_messages": 2, "google_search_submissions": 3,
        "standalone_extraction_chats": 2, "standalone_extraction_messages": 2,
        "total_chat_count": 3, "total_external_submissions": 7,
    }
    assert exact_chat_message_budget(observations) == expected
    assert build_submission_budget(observations).initial_topology.to_mapping() == expected


def legacy_construction_inputs(version):
    matrix = config().to_snapshot_mapping()["areas"]
    scope = {
        "snapshot_id": "legacy-construction", "prompt_pack_version": version,
        "scope_hash": "a" * 64, "prompt_pack_hash": "b" * 64,
        "planned_runs": 2,
        "platforms": ["chatgpt", "gemini", "google_ai_overview", "copilot"],
        "areas": matrix,
        "catchment": matrix[0]["catchment"], "treatments": matrix[0]["treatments"],
    }
    contract = {
        "templates": [
            {"sequence": 1, "variant_type": "area", "intent": "discovery",
             "template": "Best {treatment} in {area}?"},
            {"sequence": 2, "variant_type": "landmark", "intent": "local",
             "template": "Where near {landmark_or_area} for {treatment}?"},
        ],
        "observation_plan_schema_version": f"{version}-observations",
    }
    return scope, contract


@pytest.mark.parametrize("version", ["snapshot-prompts-v2", "snapshot-prompts-v3"])
def test_historical_generator_move_does_not_enable_old_public_contracts(version):
    planner = importlib.import_module("visibility_runner.planning.build_measurement_plan")
    with pytest.raises(planner.FrozenScopeError, match="unsupported prompt_pack_version"):
        freeze_snapshot(config(), snapshot_id="unsupported", prompt_pack_version=version)


def test_historical_area_guard_keeps_existing_exception_and_message():
    planner = importlib.import_module("visibility_runner.planning.build_measurement_plan")
    scope, contract = legacy_construction_inputs("snapshot-prompts-v3")
    with patch.object(planner, "_prompt_contract", return_value=contract):
        prompts = planner._prompt_rows(scope)
        prompts[0]["area_id"] = "outside-the-frozen-scope"
        with pytest.raises(
            planner.FrozenScopeError,
            match="^prompt references an area outside the frozen scope$",
        ):
            planner._build_observation_plan(scope, {"prompts": prompts})
