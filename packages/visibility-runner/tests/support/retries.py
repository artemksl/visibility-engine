"""Scripted offline retry outcomes and frozen retry-plan builders."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from visibility_runner.config import (
    BrowserConfig,
    Platform,
    ProfileConfig,
    ProfileIdentity,
    ProviderResponseTimeouts,
    RetryPolicy,
    RuntimeConfig,
)
from visibility_runner.gaio_compatibility import GAIO_GENERATED_ANSWER_CAPTURE_VERSION
from visibility_runner.errors import BrowserInteractionError
from visibility_runner.models import PacketKind, PacketPlan, RunCounts, RunPlan
from visibility_runner.execution.run_measurement_group import MeasurementGroupWorker
from visibility_runner.planning.count_allowed_sends import (
    build_gaio_extraction_packet_budget,
    build_measurement_packet_budget,
    build_submission_budget,
)
from visibility_runner.planning.hashing import canonical_sha256, json_value
from visibility_runner.websites.chat_actions import (
    BrowserIdentity,
    ProviderAnswer,
    ProviderRegistry,
    ProviderStatus,
    ReconciliationDisposition,
    ReconciliationResult,
    ResponseBaseline,
)
from visibility_runner.storage.measurement_files import MeasurementFiles
from support.gaio import (
    fake_gaio_retry_authorization_evidence,
    fake_gaio_terminal_evidence,
    gaio_response_identity,
)
from support.execution import (
    _measurement,
)


@dataclass(frozen=True)
class _Outcome:
    text: str = ""
    status: ProviderStatus = ProviderStatus.COLLECTED
    capture_contract_version: Optional[str] = None
    timeout: bool = False
    wait_error: Optional[str] = None
    disposition: ReconciliationDisposition = (
        ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
    )
    failure_classification: str = "visible_provider_terminal_error"
    native_retry_available: bool = False
    changed_identity: bool = False
    late_text: Optional[str] = None
    late_status: ProviderStatus = ProviderStatus.COLLECTED
    native_prepare_succeeds: bool = True
    native_prepare_error: Optional[str] = None
    reconciled_conversation: Optional[str] = None
    delay_seconds: float = 0
    submit_delay_seconds: float = 0
    reconciliation_delay_seconds: float = 0
    reconciliation_evidence: Optional[dict[str, object]] = None
    prepare_error: Optional[str] = None


def _prepare_failure(message: str = "scripted composer failure") -> _Outcome:
    """A step whose exact preparation fails before anything is sent."""

    return _Outcome(prepare_error=message)


def _answer(
    text: str,
    *,
    status: ProviderStatus = ProviderStatus.COLLECTED,
    capture_contract_version: Optional[str] = None,
    delay_seconds: float = 0,
    submit_delay_seconds: float = 0,
) -> _Outcome:
    return _Outcome(
        text=text,
        status=status,
        capture_contract_version=capture_contract_version,
        delay_seconds=delay_seconds,
        submit_delay_seconds=submit_delay_seconds,
    )


def _timeout(
    *,
    disposition: ReconciliationDisposition = (
        ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
    ),
    failure_classification: str = "visible_provider_terminal_error",
    native_retry_available: bool = False,
    changed_identity: bool = False,
    late_text: Optional[str] = None,
    late_status: ProviderStatus = ProviderStatus.COLLECTED,
    native_prepare_succeeds: bool = True,
    native_prepare_error: Optional[str] = None,
    reconciled_conversation: Optional[str] = None,
    reconciliation_delay_seconds: float = 0,
    reconciliation_evidence: Optional[dict[str, object]] = None,
) -> _Outcome:
    return _Outcome(
        timeout=True,
        disposition=disposition,
        failure_classification=failure_classification,
        native_retry_available=native_retry_available,
        changed_identity=changed_identity,
        late_text=late_text,
        late_status=late_status,
        native_prepare_succeeds=native_prepare_succeeds,
        native_prepare_error=native_prepare_error,
        reconciled_conversation=reconciled_conversation,
        reconciliation_delay_seconds=reconciliation_delay_seconds,
        reconciliation_evidence=reconciliation_evidence,
    )


def _fake_gaio_not_shown_evidence(
    identity: BrowserIdentity,
    prompt: str,
) -> dict[str, object]:
    return fake_gaio_terminal_evidence(
        identity,
        prompt,
        state="not_shown",
    )


class _ScriptedSubmission:
    def __init__(
        self,
        driver: "_ScriptedDriver",
        observation_id: str,
        identity: BrowserIdentity,
        steps: tuple[_Outcome, ...],
        session_ordinal: int,
    ):
        self.driver = driver
        self.observation_id = observation_id
        self._identity = identity
        self.steps = steps
        self.session_ordinal = session_ordinal
        self.step = 0
        self.prepared = False
        self.submitted = False
        self.current_prompt = ""

    @property
    def identity(self) -> BrowserIdentity:
        return self._identity

    @property
    def response_baseline(self) -> Optional[ResponseBaseline]:
        if not self.driver.provide_response_baseline:
            return None
        prior_answers = [
            step.text
            for step in self.steps[: self.step]
            if not step.timeout
        ]
        newest = prior_answers[-1] if prior_answers else ""
        return ResponseBaseline(
            visible_response_count=len(prior_answers),
            newest_response_sha256=hashlib.sha256(
                newest.encode("utf-8")
            ).hexdigest(),
        )

    async def prepare_exact(
        self, prompt, *, on_pre_send_failure=None, timeout_seconds=None,
    ):
        del on_pre_send_failure, timeout_seconds
        self.current_prompt = prompt
        outcome = self._outcome()
        if outcome.prepare_error is not None:
            self.driver.prepare_failures.append(
                (self.observation_id, self.session_ordinal, self.step)
            )
            raise BrowserInteractionError(outcome.prepare_error)
        if self.driver.platform == "google_ai_overview":
            self._identity = gaio_response_identity(
                self._identity,
                self.current_prompt,
            )
        self.prepared = True
        self.submitted = False
        self.driver.prepared.append(
            (
                self.observation_id,
                self.session_ordinal,
                self.step,
                prompt,
                self._identity.tab_reference,
            )
        )

    async def submit_once(self):
        if not self.prepared or self.submitted:
            raise AssertionError("scripted submission was not prepared once")
        self.submitted = True
        self.driver.sent.append(
            (
                self.observation_id,
                self.session_ordinal,
                self.step,
                self.current_prompt,
                self._identity.tab_reference,
            )
        )
        outcome = self._outcome()
        if outcome.submit_delay_seconds:
            await asyncio.sleep(outcome.submit_delay_seconds)

    async def wait_response(self) -> ProviderAnswer:
        if not self.submitted:
            raise AssertionError("scripted response requested before send")
        outcome = self._outcome()
        if outcome.delay_seconds:
            await asyncio.sleep(outcome.delay_seconds)
        self.driver.waited.append(
            (self.observation_id, self.session_ordinal, self.step)
        )
        if outcome.timeout:
            raise TimeoutError("scripted provider response timeout")
        if outcome.wait_error is not None:
            raise RuntimeError(outcome.wait_error)
        identity = self._identity
        return ProviderAnswer(
            status=outcome.status,
            text=outcome.text,
            identity=identity,
            capture_contract_version=outcome.capture_contract_version,
            capture_evidence=(
                fake_gaio_terminal_evidence(
                    identity,
                    self.current_prompt,
                    state=(
                        "not_shown"
                        if outcome.status is ProviderStatus.NOT_SHOWN
                        else "shown"
                    ),
                    text=outcome.text,
                )
                if self.driver.platform == "google_ai_overview"
                else {}
            ),
        )

    async def reconcile_no_response(
        self,
        grace_seconds: float,
    ) -> ReconciliationResult:
        outcome = self._outcome()
        if not outcome.timeout:
            raise AssertionError("only timed-out steps may be reconciled")
        if outcome.reconciliation_delay_seconds:
            await asyncio.sleep(outcome.reconciliation_delay_seconds)
        identity = self._identity
        if outcome.changed_identity:
            identity = replace(
                identity,
                tab_reference=f"{identity.tab_reference}:lost",
            )
        if outcome.reconciled_conversation is not None:
            identity = replace(
                identity,
                conversation_reference=outcome.reconciled_conversation,
            )
            self._identity = identity
        if self.driver.platform == "google_ai_overview":
            identity = gaio_response_identity(
                identity,
                self.current_prompt,
            )
            self._identity = identity
        answer = None
        if (
            outcome.disposition
            is ReconciliationDisposition.LATE_RESPONSE_CAPTURED
        ):
            answer = ProviderAnswer(
                status=outcome.late_status,
                text=outcome.late_text or "",
                identity=identity,
                capture_contract_version=(
                    GAIO_GENERATED_ANSWER_CAPTURE_VERSION
                    if (
                        self.driver.platform == "google_ai_overview"
                        and outcome.late_status is ProviderStatus.COLLECTED
                    )
                    else None
                ),
                capture_evidence=(
                    fake_gaio_terminal_evidence(
                        identity,
                        self.current_prompt,
                        state=(
                            "not_shown"
                            if outcome.late_status
                            is ProviderStatus.NOT_SHOWN
                            else "shown"
                        ),
                        text=outcome.late_text or "",
                    )
                    if self.driver.platform == "google_ai_overview"
                    else {}
                ),
            )
        failure_classification = outcome.failure_classification
        if (
            outcome.reconciliation_evidence is None
            and self.driver.platform == "google_ai_overview"
            and outcome.disposition
            is ReconciliationDisposition.CONFIRMED_TERMINAL_NO_RESPONSE
        ):
            terminal_failure = (
                failure_classification
                if failure_classification.startswith(
                    "google_terminal_error:"
                )
                else "google_terminal_error:something went wrong"
            )
            failure_classification = terminal_failure
            baseline = (
                self.response_baseline.to_mapping()
                if self.response_baseline is not None
                else None
            )
            assert baseline is not None
            evidence = fake_gaio_retry_authorization_evidence(
                identity,
                self.current_prompt,
                response_baseline=baseline,
                marker=terminal_failure.split(":", 1)[1],
            )
        else:
            evidence = outcome.reconciliation_evidence or {
                "grace_seconds": grace_seconds,
                "response_baseline": (
                    self.response_baseline.to_mapping()
                    if self.response_baseline is not None
                    else None
                ),
                "checks": [
                    {
                        "expected_user_message_present": True,
                        "new_assistant_response_present": answer is not None,
                        "provider_busy": False,
                        "terminal_error_visible": answer is None,
                    },
                    {
                        "expected_user_message_present": True,
                        "new_assistant_response_present": answer is not None,
                        "provider_busy": False,
                        "terminal_error_visible": answer is None,
                    },
                ],
            }
        self.driver.reconciled.append(
            (
                self.observation_id,
                self.session_ordinal,
                self.step,
                outcome.disposition,
            )
        )
        return ReconciliationResult(
            disposition=outcome.disposition,
            identity=identity,
            evidence=evidence,
            answer=answer,
            failure_classification=failure_classification,
            native_retry_available=outcome.native_retry_available,
        )

    async def prepare_native_retry(self) -> bool:
        outcome = self._outcome()
        self.driver.native_retry_probes.append(
            (self.observation_id, self.session_ordinal, self.step)
        )
        if outcome.native_prepare_error is not None:
            raise TimeoutError(outcome.native_prepare_error)
        if (
            not outcome.timeout
            or not outcome.native_retry_available
            or not outcome.native_prepare_succeeds
            or self.step + 1 >= len(self.steps)
        ):
            return False
        self.step += 1
        self.prepared = True
        self.submitted = False
        self.driver.native_retries.append(
            (self.observation_id, self.session_ordinal, self.step)
        )
        return True

    def restore_confirmed_attempt(
        self,
        *,
        prompt: str,
        response_baseline,
        failure_classification: str,
        native_retry: bool = False,
    ):
        del response_baseline, native_retry
        if not prompt or not failure_classification:
            raise AssertionError("durable retry evidence is required")
        self.current_prompt = prompt
        self.prepared = True
        self.submitted = True

    def next_turn(self):
        if not self.submitted:
            raise AssertionError("scripted next_turn requested before send")
        self.step += 1
        if self.step >= len(self.steps):
            raise AssertionError("scripted session has no next turn")
        self.prepared = False
        self.submitted = False
        self.current_prompt = ""
        self.driver.next_turns.append(
            (
                self.observation_id,
                self.session_ordinal,
                self._identity.tab_reference,
            )
        )
        return self

    def use_extraction_timeout(self, seconds: Optional[float] = None):
        self.driver.extraction_timeouts.append(
            (self.observation_id, seconds)
        )

    async def close(self):
        self.driver.closed.append(
            (self.observation_id, self._identity.tab_reference)
        )

    def _outcome(self) -> _Outcome:
        try:
            return self.steps[self.step]
        except IndexError as exc:
            raise AssertionError(
                f"no scripted outcome for {self.observation_id} "
                f"session {self.session_ordinal} step {self.step}"
            ) from exc


class _ScriptedDriver:
    def __init__(
        self,
        platform: str,
        sessions: Optional[
            dict[str, tuple[tuple[_Outcome, ...], ...]]
        ] = None,
        *,
        provide_response_baseline: bool = True,
        open_failures: Optional[dict[str, tuple[int, ...]]] = None,
    ):
        self.platform = platform
        self.sessions = dict(sessions or {})
        self.provide_response_baseline = provide_response_baseline
        # 1-based session ordinals whose ``open`` fails before any chat
        # exists. A failed ordinal still consumes its scripted session slot.
        self.open_failures = dict(open_failures or {})
        self.failed_opens: list[tuple[str, int]] = []
        self.prepare_failures: list[tuple[str, int, int]] = []
        self.open_counts: dict[str, int] = {}
        self.active: dict[str, _ScriptedSubmission] = {}
        self.opens: list[tuple[str, int, BrowserIdentity]] = []
        self.restores: list[tuple[str, BrowserIdentity]] = []
        self.prepared: list[tuple[str, int, int, str, str]] = []
        self.sent: list[tuple[str, int, int, str, str]] = []
        self.waited: list[tuple[str, int, int]] = []
        self.reconciled: list[
            tuple[str, int, int, ReconciliationDisposition]
        ] = []
        self.native_retry_probes: list[tuple[str, int, int]] = []
        self.native_retries: list[tuple[str, int, int]] = []
        self.next_turns: list[tuple[str, int, str]] = []
        self.extraction_timeouts: list[tuple[str, Optional[float]]] = []
        self.closed: list[tuple[str, str]] = []

    async def open(
        self,
        *,
        profile_id: str,
        chrome_directory: str,
        observation_id: str,
        fresh: bool = True,
    ) -> _ScriptedSubmission:
        if not fresh:
            raise AssertionError("automatic retries must open fresh when asked")
        index = self.open_counts.get(observation_id, 0)
        self.open_counts[observation_id] = index + 1
        if index + 1 in self.open_failures.get(observation_id, ()):
            self.failed_opens.append((observation_id, index + 1))
            raise BrowserInteractionError(
                f"scripted open failure for session {index + 1}"
            )
        try:
            steps = self.sessions[observation_id][index]
        except (KeyError, IndexError) as exc:
            raise AssertionError(
                f"no scripted session {index + 1} for {observation_id}"
            ) from exc
        if self.platform == "chatgpt":
            conversation = (
                f"https://chatgpt.com/c/{observation_id}-{index + 1}"
            )
        elif self.platform == "gemini":
            conversation = "https://gemini.google.com/app"
        elif self.platform == "google_ai_overview":
            conversation = (
                f"https://www.google.com/search?q={observation_id}"
            )
        else:
            conversation = f"https://example.test/{observation_id}"
        identity = BrowserIdentity(
            profile_id=profile_id,
            chrome_directory=chrome_directory,
            browser_context_reference=f"context:{profile_id}",
            tab_reference=(
                f"tab:{self.platform}:{observation_id}:{index + 1}"
            ),
            browser_target_reference=(
                f"cdp-target:{self.platform}:{observation_id}:{index + 1}"
            ),
            conversation_reference=conversation,
        )
        submission = _ScriptedSubmission(
            self,
            observation_id,
            identity,
            steps,
            index + 1,
        )
        self.active[identity.tab_reference] = submission
        self.opens.append((observation_id, index + 1, identity))
        return submission

    async def restore(
        self,
        identity: BrowserIdentity,
        *,
        observation_id: str,
    ) -> Optional[_ScriptedSubmission]:
        self.restores.append((observation_id, identity))
        return self.active.get(identity.tab_reference)


def _registry(*drivers: _ScriptedDriver) -> ProviderRegistry:
    registry = ProviderRegistry()
    for driver in drivers:
        registry.register(driver)
    return registry


def _policy(
    *,
    max_attempts: int = 2,
    response_timeouts: Optional[ProviderResponseTimeouts] = None,
) -> RetryPolicy:
    return RetryPolicy(
        enabled=True,
        max_attempts=max_attempts,
        late_response_grace_seconds=0,
        backoff_seconds=0,
        response_timeouts=(
            response_timeouts
            if response_timeouts is not None
            else ProviderResponseTimeouts()
        ),
    )


def _observation(
    observation_id: str,
    platform: str,
    *,
    prompt: str = "Recommend a clinic",
) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "platform": platform,
        "prompt_text": prompt,
        "profile_id": "profile_a",
        "chrome_directory": "Default",
    }


def _raw_plan(
    run_id: str,
    observations: list[dict[str, object]],
    *,
    retry_policy: Optional[RetryPolicy] = None,
) -> dict[str, object]:
    policy = retry_policy or _policy()
    return {
        "run_id": run_id,
        "retry_policy": policy.to_mapping(),
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
                "packet_id": "packet-1",
                "kind": "measurement",
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
    events: Optional[list] = None,
):
    repository = MeasurementFiles(root)
    run_id = str(plan["run_id"])
    if not repository.run_dir(run_id).exists():
        repository.create(plan)
    stored_plan = repository.read_plan(run_id)
    packet = repository.read_measurement_group(run_id, "packet-1")
    worker = (
        MeasurementGroupWorker(repository, providers)
        if events is None
        else MeasurementGroupWorker(repository, providers, event_sink=events.append)
    )
    outcome = await worker.execute(
        run_id=run_id,
        plan=stored_plan,
        packet=packet,
        resume=resume,
    )
    return repository, outcome


def _result(repository: MeasurementFiles, run_id: str, observation_id: str):
    return next(
        row
        for row in repository.iter_observation_results(run_id)
        if row["observation_id"] == observation_id
    )


def _runtime(
    root: Path,
    policy: RetryPolicy,
    *,
    worker_timeout_seconds: float = 900,
) -> tuple[RuntimeConfig, str, ProfileIdentity]:
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
    runtime = RuntimeConfig(
        artifact_root=root / "artifacts",
        profiles=ProfileConfig(
            selected_profile_ids=(profile.id,),
            manifest_path=manifest,
            manifest_sha256=digest,
            selected_profiles=(profile,),
        ),
        browser=BrowserConfig(),
        retry_policy=policy,
        max_concurrency=2,
        worker_timeout_seconds=worker_timeout_seconds,
    )
    return runtime, digest, profile


def _typed_plan(
    platform: Platform,
    *,
    run_id: str,
    manifest_sha256: str,
    profile: ProfileIdentity,
    retry_policy: RetryPolicy,
) -> RunPlan:
    observation_id = f"{platform.value}-observation"
    observation = _observation(observation_id, platform.value)
    budget = build_submission_budget(
        [observation],
        retry_policy=retry_policy,
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
        retry_policy=retry_policy,
        counts=RunCounts(
            platform_prompt_count=1,
            semantic_prompt_count=1,
            planned_observation_count=1,
            platform_prompt_counts={platform.value: 1},
            platform_observation_counts={platform.value: 1},
        ),
        observation_plan={"observations": [observation]},
    )
    payload = json_value(plan)
    payload.pop("plan_sha256")
    return replace(plan, plan_sha256=canonical_sha256(payload))


def _typed_two_observation_plan(
    platform: Platform,
    *,
    run_id: str,
    manifest_sha256: str,
    profile: ProfileIdentity,
    retry_policy: RetryPolicy,
) -> RunPlan:
    observations = [
        _observation(f"{platform.value}-observation-{ordinal}", platform.value)
        for ordinal in (1, 2)
    ]
    observation_ids = tuple(
        str(observation["observation_id"])
        for observation in observations
    )
    budget = build_submission_budget(
        observations,
        retry_policy=retry_policy,
    )
    packet = PacketPlan(
        packet_id=f"{platform.value}-packet",
        platform=platform,
        profile_id=profile.id,
        observation_ids=observation_ids,
        submission_budget=budget,
    )
    plan = RunPlan(
        run_id=run_id,
        measurement_config=_measurement(platform),
        packets=(packet,),
        observation_ids=observation_ids,
        submission_budget=budget,
        selected_profile_ids=(profile.id,),
        selected_profiles=(profile,),
        profile_manifest_sha256=manifest_sha256,
        retry_policy=retry_policy,
        counts=RunCounts(
            platform_prompt_count=2,
            semantic_prompt_count=2,
            planned_observation_count=2,
            platform_prompt_counts={platform.value: 2},
            platform_observation_counts={platform.value: 2},
        ),
        observation_plan={"observations": observations},
    )
    payload = json_value(plan)
    payload.pop("plan_sha256")
    return replace(plan, plan_sha256=canonical_sha256(payload))


def _typed_split_gaio_plan(
    *,
    run_id: str,
    manifest_sha256: str,
    profile: ProfileIdentity,
    retry_policy: RetryPolicy,
) -> RunPlan:
    observation_id = "split-gaio-observation"
    observation = _observation(
        observation_id,
        Platform.GOOGLE_AI_OVERVIEW.value,
    )
    measurement_budget = build_measurement_packet_budget(
        [observation],
        retry_policy=retry_policy,
    )
    extraction_budget = build_gaio_extraction_packet_budget(
        [observation],
        retry_policy=retry_policy,
    )
    full_budget = measurement_budget + extraction_budget
    plan = RunPlan(
        run_id=run_id,
        measurement_config=_measurement(Platform.GOOGLE_AI_OVERVIEW),
        packets=(
            PacketPlan(
                packet_id="gaio-measurement",
                platform=Platform.GOOGLE_AI_OVERVIEW,
                profile_id=profile.id,
                observation_ids=(observation_id,),
                submission_budget=measurement_budget,
                kind=PacketKind.MEASUREMENT,
            ),
            PacketPlan(
                packet_id="gaio-extraction",
                platform=Platform.GEMINI,
                profile_id=profile.id,
                observation_ids=(observation_id,),
                submission_budget=extraction_budget,
                kind=PacketKind.GAIO_EXTRACTION,
                conditional_on="gaio_shown_v1",
            ),
        ),
        observation_ids=(observation_id,),
        submission_budget=full_budget,
        selected_profile_ids=(profile.id,),
        selected_profiles=(profile,),
        profile_manifest_sha256=manifest_sha256,
        retry_policy=retry_policy,
        counts=RunCounts(
            platform_prompt_count=1,
            semantic_prompt_count=1,
            planned_observation_count=1,
            platform_prompt_counts={
                Platform.GOOGLE_AI_OVERVIEW.value: 1
            },
            platform_observation_counts={
                Platform.GOOGLE_AI_OVERVIEW.value: 1
            },
        ),
        observation_plan={"observations": [observation]},
    )
    payload = json_value(plan)
    payload.pop("plan_sha256")
    return replace(plan, plan_sha256=canonical_sha256(payload))
