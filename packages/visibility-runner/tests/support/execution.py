"""Offline provider fakes and immutable execution-plan builders."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from visibility_runner.config import (
    Area,
    BrowserConfig,
    Clinic,
    ClinicArchetype,
    Geography,
    MeasurementConfig,
    Platform,
    ProfileConfig,
    ProfileIdentity,
    RuntimeConfig,
    Treatment,
)
from visibility_runner.models import (
    PacketKind,
    PacketPlan,
    RunCounts,
    RunPlan,
    SubmissionBudget,
)
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
from visibility_runner.planning.hashing import canonical_sha256, json_value
from visibility_runner.websites.chat_actions import (
    BrowserIdentity,
    ProviderAnswer,
    ProviderRegistry,
    ProviderStatus,
)
from visibility_runner.storage.measurement_files import MeasurementFiles
from support.gaio import fake_gaio_terminal_evidence, gaio_response_identity


@dataclass(frozen=True)
class AnswerSpec:
    text: str
    status: ProviderStatus = ProviderStatus.COLLECTED
    capture_contract_version: Optional[str] = None
    identity: Optional[BrowserIdentity] = None


def _fake_gaio_not_shown_evidence(
    identity: BrowserIdentity,
    prompt: str,
) -> dict[str, object]:
    return fake_gaio_terminal_evidence(
        identity,
        prompt,
        state="not_shown",
    )


class FakeSubmission:
    def __init__(
        self,
        driver: "FakeDriver",
        observation_id: str,
        identity: BrowserIdentity,
        answers: tuple[AnswerSpec, ...],
    ):
        self.driver = driver
        self.observation_id = observation_id
        self._identity = identity
        self.answers = answers
        self.turn = 0
        self.prepared = False
        self.submitted = False
        self.current_prompt = ""

    @property
    def identity(self) -> BrowserIdentity:
        return self._identity

    async def prepare_exact(
        self,
        prompt,
        *,
        on_pre_send_failure=None,
        timeout_seconds=None,
    ):
        # Every keyword the worker passes is recorded so a test can check the
        # preparation budget chosen for each message.
        self.driver.prepare_calls.append(
            {
                "observation_id": self.observation_id,
                "turn": self.turn,
                "platform": self.driver.platform,
                "timeout_seconds": timeout_seconds,
                "monotonic": time.monotonic(),
            }
        )
        self.current_prompt = str(prompt)
        if self.driver.platform == "google_ai_overview":
            self._identity = gaio_response_identity(
                self._identity,
                self.current_prompt,
            )
        self.driver.prepared.append(
            (self.observation_id, self.turn, prompt, self._identity)
        )
        failures = self.driver.prepare_failures.get(self.observation_id, 0)
        if failures:
            self.driver.prepare_failures[self.observation_id] = failures - 1
            if on_pre_send_failure is not None:
                value = on_pre_send_failure("injected exact-input failure")
                if asyncio.iscoroutine(value):
                    await value
            raise RuntimeError("injected exact-input failure")
        unreported = self.driver.unreported_prepare_failures.get(
            self.observation_id,
            0,
        )
        if unreported:
            self.driver.unreported_prepare_failures[self.observation_id] = (
                unreported - 1
            )
            raise RuntimeError(
                "cdk-overlay-backdrop intercepts pointer events"
            )
        self.prepared = True

    async def submit_once(self):
        if not self.prepared or self.submitted:
            raise AssertionError("fake submission was not prepared exactly once")
        self.submitted = True
        self.driver.sent.append(
            (self.observation_id, self.turn, self._identity.tab_reference)
        )

    async def wait_response(self):
        if not self.submitted:
            raise AssertionError("fake response requested before send")
        try:
            spec = self.answers[self.turn]
        except IndexError as exc:
            raise AssertionError(
                f"no fake answer for {self.observation_id} turn {self.turn + 1}"
            ) from exc
        identity = spec.identity or self._identity
        if self.driver.platform == "google_ai_overview":
            identity = gaio_response_identity(
                identity,
                self.current_prompt,
            )
        self._identity = identity
        self.driver.responses.append(
            (self.observation_id, self.turn, spec.text)
        )
        return ProviderAnswer(
            status=spec.status,
            text=spec.text,
            identity=identity,
            capture_contract_version=spec.capture_contract_version,
            capture_evidence=(
                fake_gaio_terminal_evidence(
                    identity,
                    self.current_prompt,
                    state=(
                        "not_shown"
                        if spec.status is ProviderStatus.NOT_SHOWN
                        else "shown"
                    ),
                    text=spec.text,
                )
                if self.driver.platform == "google_ai_overview"
                else {}
            ),
        )

    def next_turn(self):
        if not self.submitted:
            raise AssertionError("fake next_turn called before send")
        self.driver.next_turns.append(
            (self.observation_id, self._identity.tab_reference)
        )
        self.turn += 1
        self.prepared = False
        self.submitted = False
        return self

    def use_extraction_timeout(self):
        self.driver.extraction_timeouts.append(self.observation_id)

    async def close(self):
        self.driver.closed.append(self.observation_id)


class FakeDriver:
    def __init__(
        self,
        platform: str,
        answers: Optional[dict[str, tuple[AnswerSpec, ...]]] = None,
        *,
        prepare_failures: Optional[dict[str, int]] = None,
        unreported_prepare_failures: Optional[dict[str, int]] = None,
        open_failures: Optional[dict[str, int]] = None,
    ):
        self.platform = platform
        self.answers = answers or {}
        self.prepare_failures = dict(prepare_failures or {})
        self.unreported_prepare_failures = dict(
            unreported_prepare_failures or {}
        )
        self.open_failures = dict(open_failures or {})
        self.opens: list[tuple[str, bool, BrowserIdentity]] = []
        self.restores: list[tuple[str, BrowserIdentity]] = []
        self.prepared: list[tuple[str, int, str, BrowserIdentity]] = []
        # One row per ``prepare_exact`` call with the keywords it received.
        self.prepare_calls: list[dict[str, object]] = []
        self.sent: list[tuple[str, int, str]] = []
        self.responses: list[tuple[str, int, str]] = []
        self.next_turns: list[tuple[str, str]] = []
        self.extraction_timeouts: list[str] = []
        self.closed: list[str] = []

    async def open(
        self,
        *,
        profile_id: str,
        chrome_directory: str,
        observation_id: str,
        fresh: bool = True,
    ):
        failures = self.open_failures.get(observation_id, 0)
        if failures:
            self.open_failures[observation_id] = failures - 1
            raise RuntimeError("injected provider-open failure")
        conversation_reference = {
            "gemini": "https://gemini.google.com/app",
            "chatgpt": f"https://chatgpt.com/c/{observation_id}",
            "copilot": f"https://copilot.com/chats/{observation_id}",
            "google_ai_overview": (
                f"https://www.google.com/search?q={observation_id}"
            ),
        }.get(self.platform, f"https://example.test/{observation_id}")
        identity = BrowserIdentity(
            profile_id=profile_id,
            chrome_directory=chrome_directory,
            browser_context_reference=f"context:{profile_id}",
            tab_reference=(
                f"tab:{self.platform}:{observation_id}:{len(self.opens) + 1}"
            ),
            browser_target_reference=(
                f"target:{self.platform}:{observation_id}:"
                f"{len(self.opens) + 1}"
            ),
            conversation_reference=conversation_reference,
        )
        self.opens.append((observation_id, fresh, identity))
        return FakeSubmission(
            self,
            observation_id,
            identity,
            self.answers.get(observation_id, ()),
        )

    async def restore(self, identity, *, observation_id: str):
        self.restores.append((observation_id, identity))
        return FakeSubmission(
            self,
            observation_id,
            identity,
            self.answers.get(observation_id, ()),
        )


def _registry(*drivers: FakeDriver) -> ProviderRegistry:
    registry = ProviderRegistry()
    for driver in drivers:
        registry.register(driver)
    return registry


def _raw_plan(
    run_id: str,
    observations: list[dict[str, object]],
    *,
    packet_id: str = "packet-1",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "selected_profiles": [
            {
                "id": "profile_a",
                "display_name": "Account A",
                "chrome_directory": "Default",
                "project_url": "",
            }
        ],
        "observation_plan": {"observations": observations},
        "packets": [
            {
                "packet_id": packet_id,
                "platform": observations[0]["platform"],
                "profile_id": "profile_a",
                "observation_ids": [
                    str(observation["observation_id"])
                    for observation in observations
                ],
            }
        ],
    }


async def _execute_raw(
    root: Path,
    plan: dict[str, object],
    providers: ProviderRegistry,
    *,
    resume: bool = False,
):
    repository = MeasurementFiles(root)
    run_id = str(plan["run_id"])
    packet_id = str(plan["packets"][0]["packet_id"])
    if not repository.run_dir(run_id).exists():
        repository.create(plan)
    stored_plan = repository.read_plan(run_id)
    packet = repository.read_measurement_group(run_id, packet_id)
    outcome = await MeasurementGroupWorker(repository, providers).execute(
        run_id=run_id,
        plan=stored_plan,
        packet=packet,
        resume=resume,
    )
    return repository, outcome


def _measurement(platform: Platform) -> MeasurementConfig:
    return MeasurementConfig(
        clinic=Clinic(
            name="Example Clinic",
            account_id="clinic-001",
            aliases=("Example Aesthetics",),
            archetype=ClinicArchetype(
                id="aesthetics-skin-clinic",
                singular="aesthetics and skin clinic",
                plural="aesthetics and skin clinics",
            ),
        ),
        areas=(
            Area(
                name="Marylebone",
                id="marylebone",
                branch_id="branch-marylebone",
                branch_name="Example Clinic Marylebone",
                greater_area=Geography(
                    "Central London",
                    id="central-london",
                ),
                city=Geography("London", id="london"),
                treatments=(Treatment("fat freezing"),),
            ),
        ),
        platforms=(platform,),
    )


def _budget(platform: Platform) -> SubmissionBudget:
    if platform is Platform.GOOGLE_AI_OVERVIEW:
        return SubmissionBudget(
            conversational_chats=0,
            turn_1_messages=0,
            turn_2_messages=0,
            conversational_messages=0,
            google_search_submissions=1,
            standalone_extraction_chats=1,
            standalone_extraction_messages=1,
            total_chat_count=1,
            total_external_submissions=2,
        )
    return SubmissionBudget(
        conversational_chats=1,
        turn_1_messages=1,
        turn_2_messages=1,
        conversational_messages=2,
        google_search_submissions=0,
        standalone_extraction_chats=0,
        standalone_extraction_messages=0,
        total_chat_count=1,
        total_external_submissions=2,
    )


def _typed_plan(
    platform: Platform,
    *,
    run_id: str,
    manifest_sha256: str,
) -> RunPlan:
    observation_id = f"{platform.value}-observation"
    budget = _budget(platform)
    profile = ProfileIdentity(
        id="profile_a",
        display_name="Account A",
        chrome_directory="Default",
    )
    packet = PacketPlan(
        packet_id=f"{platform.value}-packet",
        platform=platform,
        profile_id=profile.id,
        observation_ids=(observation_id,),
        submission_budget=budget,
    )
    plan = RunPlan(
        run_id=run_id,
        measurement_config=_measurement(platform),
        packets=(packet,),
        observation_ids=(observation_id,),
        submission_budget=budget,
        selected_profile_ids=(profile.id,),
        selected_profiles=(profile,),
        profile_manifest_sha256=manifest_sha256,
        counts=RunCounts(
            platform_prompt_count=1,
            semantic_prompt_count=1,
            planned_observation_count=1,
            platform_prompt_counts={platform.value: 1},
            platform_observation_counts={platform.value: 1},
        ),
        observation_plan={
            "observations": [
                {
                    "observation_id": observation_id,
                    "prompt_id": "prompt-1",
                    "platform": platform.value,
                    "run_ordinal": 1,
                    "prompt_text": "Recommend a clinic",
                    "profile_id": profile.id,
                }
            ]
        },
    )
    payload = json_value(plan)
    payload.pop("plan_sha256")
    return replace(plan, plan_sha256=canonical_sha256(payload))


def _typed_split_gaio_plan(
    *,
    run_id: str,
    manifest_sha256: str,
    observation_ids: tuple[str, ...] = ("gaio-observation",),
) -> RunPlan:
    profile = ProfileIdentity(
        id="profile_a",
        display_name="Account A",
        chrome_directory="Default",
    )
    search_count = len(observation_ids)
    search_budget = SubmissionBudget(
        conversational_chats=0,
        turn_1_messages=0,
        turn_2_messages=0,
        conversational_messages=0,
        google_search_submissions=search_count,
        standalone_extraction_chats=0,
        standalone_extraction_messages=0,
        total_chat_count=0,
        total_external_submissions=search_count,
    )
    extraction_budget = SubmissionBudget(
        conversational_chats=0,
        turn_1_messages=0,
        turn_2_messages=0,
        conversational_messages=0,
        google_search_submissions=0,
        standalone_extraction_chats=search_count,
        standalone_extraction_messages=search_count,
        total_chat_count=search_count,
        total_external_submissions=search_count,
    )
    full_budget = search_budget + extraction_budget
    plan = RunPlan(
        run_id=run_id,
        measurement_config=_measurement(Platform.GOOGLE_AI_OVERVIEW),
        packets=(
            PacketPlan(
                packet_id="gaio-measurement",
                platform=Platform.GOOGLE_AI_OVERVIEW,
                profile_id=profile.id,
                observation_ids=observation_ids,
                submission_budget=search_budget,
                kind=PacketKind.MEASUREMENT,
            ),
            PacketPlan(
                packet_id="gaio-extraction",
                platform=Platform.GEMINI,
                profile_id=profile.id,
                observation_ids=observation_ids,
                submission_budget=extraction_budget,
                kind=PacketKind.GAIO_EXTRACTION,
                conditional_on="gaio_shown_v1",
            ),
        ),
        observation_ids=observation_ids,
        submission_budget=full_budget,
        selected_profile_ids=(profile.id,),
        selected_profiles=(profile,),
        profile_manifest_sha256=manifest_sha256,
        counts=RunCounts(
            platform_prompt_count=search_count,
            semantic_prompt_count=search_count,
            planned_observation_count=search_count,
            platform_prompt_counts={"google_ai_overview": search_count},
            platform_observation_counts={"google_ai_overview": search_count},
        ),
        observation_plan={
            "observations": [
                {
                    "observation_id": observation_id,
                    "prompt_id": f"prompt-{index}",
                    "platform": "google_ai_overview",
                    "run_ordinal": 1,
                    "prompt_text": f"Recommend a clinic {index}",
                    "profile_id": profile.id,
                }
                for index, observation_id in enumerate(
                    observation_ids,
                    start=1,
                )
            ]
        },
    )
    payload = json_value(plan)
    payload.pop("plan_sha256")
    return replace(plan, plan_sha256=canonical_sha256(payload))


def _runtime(root: Path) -> tuple[RuntimeConfig, str]:
    profile = ProfileIdentity(
        id="profile_a",
        display_name="Account A",
        chrome_directory="Default",
    )
    manifest_payload = [profile.to_mapping()]
    manifest = root / "profiles.json"
    manifest.write_text(
        json.dumps(manifest_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    digest = canonical_sha256(manifest_payload)
    return (
        RuntimeConfig(
            artifact_root=root / "artifacts",
            profiles=ProfileConfig(
                selected_profile_ids=(profile.id,),
                manifest_path=manifest,
                manifest_sha256=digest,
                selected_profiles=(profile,),
                lease_lock_root=root / "profile-locks",
            ),
            browser=BrowserConfig(user_data_dir=root / "synthetic-user-data"),
            max_concurrency=2,
        ),
        digest,
    )
