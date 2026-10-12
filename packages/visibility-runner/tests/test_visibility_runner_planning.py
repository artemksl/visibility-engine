from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from importlib.resources import files
from pathlib import Path

from visibility_runner import PacketKind, Platform, RetryPolicy
from visibility_runner.planning import PROMPT_CATALOG_V4_CONTENT_HASH, PROMPT_CATALOG_V4_VERSION, PROMPT_GEOGRAPHY_HASH, PROMPT_GEOGRAPHY_VERSION, PROMPT_TEMPLATE_ARTIFACT_HASH_V4, exact_chat_message_budget, freeze_snapshot
from visibility_runner.planning.catalog import (
    PROMPT_CATALOG_V4,
    PROMPT_GEOGRAPHY_V1,
)
from visibility_runner.planning.geography import (
    GeographyContractError,
    taxonomy_targets,
    treatment_key,
    validate_taxonomy_target,
)
from visibility_runner.planning.hashing import canonical_sha256
from visibility_runner.planning.hashing import json_value


from support.planning import MANIFEST_HASH, area, config, plan


class CatalogResourceTests(unittest.TestCase):
    def test_installed_resources_have_exact_versions_and_hashes(self) -> None:
        prompt_resource = files(
            "visibility_runner.resources.prompts"
        ).joinpath("prompt_catalog.json")
        geography_resource = files(
            "visibility_runner.resources.prompts"
        ).joinpath("prompt_geography.json")
        prompt_bytes = prompt_resource.read_bytes()
        geography_bytes = geography_resource.read_bytes()

        self.assertEqual(len(prompt_bytes), 20_717)
        self.assertEqual(len(geography_bytes), 844)
        self.assertEqual(
            hashlib.sha256(prompt_bytes).hexdigest(),
            "6723e68eca77434fcf2f38ed1d8980f643288b27e79c507cbde265553148ca16",
        )
        self.assertEqual(
            hashlib.sha256(geography_bytes).hexdigest(),
            "22e4dd21646a299da5f113de827003b1524302f7be8a13ef01c18a16716112a7",
        )
        self.assertEqual(
            PROMPT_CATALOG_V4_VERSION,
            "snapshot-prompts-v4-content-2026-07-17",
        )
        self.assertEqual(
            PROMPT_CATALOG_V4_CONTENT_HASH,
            "caf84efc1f217f8eefae2fd28b6cb91d27f860a24b089847bdd27978159190ab",
        )
        self.assertEqual(
            PROMPT_TEMPLATE_ARTIFACT_HASH_V4,
            "725c56dbd095185b8853353b0b419fa732d02e0b0dfb29b8513838c6bd030cf1",
        )
        self.assertEqual(
            PROMPT_GEOGRAPHY_VERSION,
            "prompt-geography-london-2026-07-v1",
        )
        self.assertEqual(
            PROMPT_GEOGRAPHY_HASH,
            "ebe6b80c04fa1f4765f2418c7616f8183a45012c202a9310763f44124441c1c5",
        )
        self.assertEqual(
            canonical_sha256(PROMPT_GEOGRAPHY_V1),
            PROMPT_GEOGRAPHY_HASH,
        )
        self.assertEqual(len(PROMPT_CATALOG_V4["slots"]), 21)

    def test_geography_helpers_are_exact_and_patient_facing(self) -> None:
        self.assertEqual(
            [row["id"] for row in taxonomy_targets("greater_area")],
            [
                "central-london",
                "north-london",
                "west-london",
                "east-london",
                "south-london",
            ],
        )
        validate_taxonomy_target(
            {
                "id": "central-london",
                "name": "Central London",
                "country_code": "GB",
            },
            tier="greater_area",
        )
        with self.assertRaises(GeographyContractError):
            validate_taxonomy_target(
                {
                    "id": "quota-central",
                    "name": "Central London",
                    "country_code": "GB",
                },
                tier="greater_area",
            )
        self.assertEqual(treatment_key("  LÁSER--Hair  Removal "), "laser hair removal")


class LegacyV4ParityTests(unittest.TestCase):
    def test_representative_prompt_and_observation_hashes_are_exact(self) -> None:
        frozen = freeze_snapshot(
            config(platforms=(Platform.CHATGPT,)),
            snapshot_id="contract-example",
        )
        scope = frozen["scope_manifest"]
        prompt_pack = frozen["snapshot_prompts"]
        observation_plan = frozen["observation_plan"]
        first_prompt = prompt_pack["prompts"][0]
        first_observation = observation_plan["observations"][0]

        self.assertEqual(
            scope["scope_hash"],
            "142ea553ee7586bd1c7ffb803b64e21db9931d8fb98d327b7ee312b886e8319a",
        )
        self.assertEqual(
            prompt_pack["prompt_pack_hash"],
            "cf88146c0484e9ce47edc22401e4e5844952561780ff37e89a5ade37c85e58e2",
        )
        self.assertEqual(
            observation_plan["observation_plan_hash"],
            "2adcdaadac9d95c4ee406fd738df11c101da7e1e895d568d7e8244340b8138ac",
        )
        self.assertEqual(
            first_prompt["semantic_prompt_id"],
            "semantic-tl01-bb150e674b76d2d71902",
        )
        self.assertEqual(
            first_prompt["prompt_id"],
            "prompt-v4-chatgpt-tl01-5edbf5838c19a48af872",
        )
        self.assertEqual(
            first_prompt["collection_scope_id"],
            "collection-scope-940172997c54e31a1058992b",
        )
        self.assertEqual(
            first_prompt["evidence_cell_id"],
            "evidence-cell-a820909bd6ecfe3ce22dead2",
        )
        self.assertEqual(
            first_observation["observation_id"],
            "obs-v4-c77af83f606312ca8a14a021",
        )
        self.assertEqual(
            first_prompt["text"],
            "What are the best clinics for fat freezing in Marylebone?",
        )
        self.assertEqual(
            first_prompt["prompt_text_sha256"],
            hashlib.sha256(first_prompt["text"].encode("utf-8")).hexdigest(),
        )

    def test_slot_prompt_and_observation_order_is_frozen(self) -> None:
        frozen = freeze_snapshot(
            config(
                repetitions=2,
                platforms=(Platform.CHATGPT, Platform.GEMINI),
            ),
            snapshot_id="order-contract",
        )
        prompts = frozen["snapshot_prompts"]["prompts"]
        observations = frozen["observation_plan"]["observations"]
        expected_slots = [
            *(f"TL{index:02d}" for index in range(1, 8)),
            *(f"TE{index:02d}" for index in range(1, 8)),
            *(f"CP{index:02d}" for index in range(1, 8)),
        ]
        self.assertEqual(
            [row["semantic_slot_id"] for row in prompts[:21]],
            expected_slots,
        )
        self.assertEqual(
            [row["platform"] for row in prompts],
            ["chatgpt"] * 21 + ["gemini"] * 21,
        )
        self.assertEqual(
            [
                (row["prompt_id"], row["run"])
                for row in observations[:4]
            ],
            [
                (prompts[0]["prompt_id"], 1),
                (prompts[0]["prompt_id"], 2),
                (prompts[1]["prompt_id"], 1),
                (prompts[1]["prompt_id"], 2),
            ],
        )
        self.assertEqual(len(prompts), 42)
        self.assertEqual(len(observations), 84)


class CountAndBudgetTests(unittest.TestCase):
    def test_one_area_one_treatment_has_exact_module_budget(self) -> None:
        value = plan(config())
        self.assertEqual(value.counts.platform_prompt_count, 63)
        self.assertEqual(value.counts.semantic_prompt_count, 21)
        self.assertEqual(value.counts.planned_observation_count, 63)
        self.assertEqual(
            dict(value.counts.platform_prompt_counts),
            {
                "chatgpt": 21,
                "gemini": 21,
                "google_ai_overview": 21,
            },
        )
        self.assertEqual(
            value.submission_budget.initial_topology.to_mapping(),
            {
                "conversational_chats": 42,
                "turn_1_messages": 42,
                "turn_2_messages": 42,
                "conversational_messages": 84,
                "google_search_submissions": 21,
                "standalone_extraction_chats": 21,
                "standalone_extraction_messages": 21,
                "total_chat_count": 63,
                "total_external_submissions": 126,
            },
        )
        self.assertEqual(value.retry_policy, RetryPolicy())
        self.assertEqual(
            value.submission_budget.retry_authorized_ceiling.to_mapping(),
            {
                "conversational_chats": 84,
                "turn_1_messages": 84,
                "turn_2_messages": 84,
                "conversational_messages": 168,
                "google_search_submissions": 42,
                "standalone_extraction_chats": 42,
                "standalone_extraction_messages": 42,
                "total_chat_count": 126,
                "total_external_submissions": 252,
            },
        )

    def test_disabled_retry_policy_freezes_initial_as_ceiling(self) -> None:
        value = plan(config(), retry_policy=RetryPolicy.disabled())

        self.assertFalse(value.retry_policy.enabled)
        self.assertEqual(
            value.submission_budget.retry_authorized_ceiling,
            value.submission_budget.initial_topology,
        )
        for packet in value.packets:
            self.assertEqual(
                packet.submission_budget.retry_authorized_ceiling,
                packet.submission_budget.initial_topology,
            )

    def test_two_areas_keep_treatments_isolated_and_counts_exact(self) -> None:
        value = plan(
            config(
                areas=(
                    area(treatment_names=("fat freezing",)),
                    area(
                        "Islington",
                        area_id="islington",
                        branch_id="branch-islington",
                        treatment_names=(
                            "laser hair removal",
                            "skin rejuvenation",
                        ),
                    ),
                ),
                repetitions=2,
            )
        )
        # Per platform: 3 treatment-local * 7, 3 distinct wider
        # treatment subjects * 7, and 10 deduplicated archetype prompts.
        self.assertEqual(value.counts.platform_prompt_count, 156)
        self.assertEqual(value.counts.semantic_prompt_count, 52)
        self.assertEqual(value.counts.planned_observation_count, 312)
        self.assertEqual(
            dict(value.counts.platform_prompt_counts),
            {
                "chatgpt": 52,
                "gemini": 52,
                "google_ai_overview": 52,
            },
        )
        self.assertEqual(
            value.submission_budget.initial_topology.to_mapping(),
            {
                "conversational_chats": 208,
                "turn_1_messages": 208,
                "turn_2_messages": 208,
                "conversational_messages": 416,
                "google_search_submissions": 104,
                "standalone_extraction_chats": 104,
                "standalone_extraction_messages": 104,
                "total_chat_count": 312,
                "total_external_submissions": 624,
            },
        )
        prompts = value.snapshot_prompts["prompts"]
        fat_local = [
            row
            for row in prompts
            if row["treatment_name"] == "fat freezing"
            and row["geography_tier"] == "local"
        ]
        self.assertEqual({row["area_id"] for row in fat_local}, {"marylebone"})
        islington_local = [
            row
            for row in prompts
            if row["area_id"] == "islington"
            and row["geography_tier"] == "local"
            and row["subject_kind"] == "treatment"
        ]
        self.assertEqual(
            {row["treatment_name"] for row in islington_local},
            {"laser hair removal", "skin rejuvenation"},
        )

    def test_evidence_budget_creates_no_extractor_for_not_shown(self) -> None:
        budget = exact_chat_message_budget(
            [
                {"platform": "chatgpt"},
                {
                    "platform": "google_ai_overview",
                    "status": "not_shown",
                },
                {
                    "platform": "google_ai_overview",
                    "status": "collected",
                    "generated_answer_capture_version": (
                        "google-ai-overview-generated-answer-v1"
                    ),
                },
            ]
        )
        self.assertEqual(
            budget,
            {
                "conversational_chats": 1,
                "turn_1_messages": 1,
                "turn_2_messages": 1,
                "conversational_messages": 2,
                "google_search_submissions": 2,
                "standalone_extraction_chats": 1,
                "standalone_extraction_messages": 1,
                "total_chat_count": 2,
                "total_external_submissions": 5,
            },
        )


class PacketAndPurityTests(unittest.TestCase):
    def test_packets_freeze_exact_round_robin_membership(self) -> None:
        first = plan(config(repetitions=2))
        second = plan(config(repetitions=2))
        disabled = plan(
            config(repetitions=2),
            retry_policy=RetryPolicy.disabled(),
        )
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertNotEqual(first.plan_sha256, disabled.plan_sha256)
        serialized = json_value(first)
        supplied_hash = serialized.pop("plan_sha256")
        self.assertEqual(canonical_sha256(serialized), supplied_hash)
        self.assertEqual(first.packet_ids, second.packet_ids)
        self.assertEqual(first.selected_profile_ids, ("profile_one", "profile_two"))
        self.assertEqual(first.profile_manifest_sha256, MANIFEST_HASH)
        self.assertEqual(
            first.selected_chrome_directories,
            ("Profile 1", "Profile 2"),
        )
        self.assertEqual(len(first.packets), 8)

        observations = first.observation_plan["observations"]
        for platform in (
            "chatgpt",
            "gemini",
            "google_ai_overview",
        ):
            platform_ids = [
                row["observation_id"]
                for row in observations
                if row["platform"] == platform
            ]
            platform_packets = [
                packet
                for packet in first.packets
                if packet.platform.value == platform
                and packet.kind is PacketKind.MEASUREMENT
            ]
            self.assertEqual(
                platform_packets[0].observation_ids,
                tuple(platform_ids[::2]),
            )
            self.assertEqual(
                platform_packets[1].observation_ids,
                tuple(platform_ids[1::2]),
            )
            self.assertIsNone(platform_packets[0].run_ordinal)

        gaio_measurement = {
            packet.profile_id: packet
            for packet in first.packets
            if packet.kind is PacketKind.MEASUREMENT
            and packet.platform is Platform.GOOGLE_AI_OVERVIEW
        }
        gaio_extraction = {
            packet.profile_id: packet
            for packet in first.packets
            if packet.kind is PacketKind.GAIO_EXTRACTION
        }
        self.assertEqual(set(gaio_extraction), set(gaio_measurement))
        for profile_id, extraction_packet in gaio_extraction.items():
            self.assertIs(extraction_packet.platform, Platform.GEMINI)
            self.assertEqual(
                extraction_packet.observation_ids,
                gaio_measurement[profile_id].observation_ids,
            )
            self.assertEqual(
                extraction_packet.conditional_on,
                "gaio_shown_v1",
            )
            self.assertEqual(
                gaio_measurement[
                    profile_id
                ].submission_budget.standalone_extraction_chats,
                0,
            )
            self.assertEqual(
                extraction_packet.submission_budget.google_search_submissions,
                0,
            )

        packet_budget = first.submission_budget.zero()
        for packet in first.packets:
            packet_budget += packet.submission_budget
        self.assertEqual(packet_budget, first.submission_budget)

    def test_plan_and_nested_artifacts_are_immutable(self) -> None:
        value = plan(config())
        with self.assertRaises(FrozenInstanceError):
            value.run_id = "changed"
        with self.assertRaises(TypeError):
            value.scope_manifest["changed"] = True
        with self.assertRaises(TypeError):
            value.observation_plan["observations"][0]["status"] = "collected"

    def test_legacy_plan_without_retry_authorization_parses_disabled(self) -> None:
        from visibility_runner.execution.manage_measurements import _plan_from_mapping

        legacy = json_value(plan(config()))
        legacy.pop("retry_policy")
        legacy["submission_budget"].pop("retry_authorized_ceiling")
        for packet in legacy["packets"]:
            packet["submission_budget"].pop("retry_authorized_ceiling")
        before = json.dumps(legacy, sort_keys=True)

        parsed = _plan_from_mapping(legacy)

        self.assertFalse(parsed.retry_policy.enabled)
        self.assertEqual(parsed.retry_policy.authorized_max_attempts, 1)
        self.assertEqual(
            parsed.submission_budget.retry_authorized_ceiling,
            parsed.submission_budget.initial_topology,
        )
        self.assertEqual(json.dumps(legacy, sort_keys=True), before)

    def test_planning_writes_no_files_and_imports_no_app_or_browser(self) -> None:
        before_playwright = {
            name for name in sys.modules if name.startswith("playwright")
        }
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chdir(root)
            try:
                before = sorted(root.rglob("*"))
                value = plan(config(), run_id="pure-plan")
                after = sorted(root.rglob("*"))
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(before, after)
        self.assertEqual(
            before_playwright,
            {name for name in sys.modules if name.startswith("playwright")},
        )
        self.assertEqual(value.run_id, "pure-plan")


if __name__ == "__main__":
    unittest.main()
