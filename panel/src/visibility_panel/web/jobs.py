"""Restricted background job runner for the local control UI."""

from __future__ import annotations

import copy

import contextlib

import hashlib

import json

import os

import re

import signal

import shutil

import subprocess

import sys

import threading

import time

import uuid

from dataclasses import asdict, dataclass, field, replace

from datetime import datetime, timezone

from pathlib import Path

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from visibility_panel.execution.measurement_platforms import (
    deferred_measurement_reason, require_active_measurement,
)

from visibility_panel.execution.measurement_inputs import FrozenExecutionInputs, FrozenExecutionRequest

from visibility_panel.execution.measurement_recovery import AUTO_RECOVERY_ROUNDS, auto_recovery_progress

from visibility_panel.integrity import canonical_sha256

from visibility_panel.paths import repo_root, resolve_repo_relative

from visibility_panel.persistence.json_store import read_json_or_none, write_json

from visibility_panel.execution.worker_registry import (
    RegistryError, WorkerRegistry, WorkerWatchdog, publish_json, read_record,
    discover_legacy_worker,
)

from visibility_panel.web.start_receipts import (
    StartRequestConflict, load_start_receipt, locked_start_receipt,
    rejected_start_path, reserve_start_receipt,
)

from visibility_panel.web.record_navigation import (
    PLATFORM_URLS, RecordNavigationError, conversation_url, latest_conversation_reference,
    launch_inspection, verified_profile_rows,
)

from visibility_panel.web.config import selected_snapshot_profile_rows, load_settings
from visibility_panel.integrations.visibility_selection import measurement_from_frozen_snapshot

REPO_ROOT = repo_root()

SRC_DIR = REPO_ROOT / "panel" / "src"

@dataclass(frozen=True)
class Workflow:
    id: str
    label: str
    section: str
    group: str
    command: List[str]
    opens_browser: bool
    supports_project: bool = False
    supports_runs: bool = False
    supports_audit_platforms: bool = False
    default_regime: str = "rotating"
    default_project: bool = False
    accepts_cli_flags: bool = True
    runner_owned_snapshot: bool = False

def _free_audit_workflow(
    *,
    workflow_id: str,
    label: str,
    command: List[str],
    runner_owned_snapshot: bool,
) -> Workflow:
    return Workflow(
        id=workflow_id,
        label=label,
        section="client_work",
        group="Visibility Snapshot",
        command=list(command),
        opens_browser=True,
        supports_project=True,
        supports_runs=True,
        supports_audit_platforms=True,
        default_regime="logged_in",
        runner_owned_snapshot=runner_owned_snapshot,
    )

RUNNER_FREE_AUDIT_WORKFLOW = _free_audit_workflow(
    workflow_id="free_audit_stage1",
    label="Free Visibility Snapshot",
    command=[sys.executable, "scripts/run_visibility_measurement.py"],
    runner_owned_snapshot=True,
)

WORKFLOWS = {RUNNER_FREE_AUDIT_WORKFLOW.id: RUNNER_FREE_AUDIT_WORKFLOW}

PACKET_START_STAGGER_SECONDS = 10

RUNNER_SNAPSHOT_RETRY_POLICY = {
    "enabled": True,
    "max_attempts": 2,
    "late_response_grace_seconds": 30,
    "backoff_seconds": 5,
    "response_timeouts": {
        "chatgpt_turn_1_seconds": 240,
        "chatgpt_turn_2_seconds": 240,
        "gemini_turn_1_seconds": 240,
        "gemini_turn_2_seconds": 240,
        "google_ai_overview_seconds": 90,
        "gaio_gemini_parser_seconds": 240,
    },
}

DEFAULT_RUN_CONFIG = {
    "vpn_enabled": True,
    "relaunch": False,
    "regime": "rotating",
    "project_enabled": False,
    "project_url": "",
    "intelligence_level": "",
    "profile_count": 1,
    "selected_profile_ids": [],
    # Deprecated request/response alias. New callers use selected_profile_ids.
    "selected_profiles": [],
    "parallel_tabs": 3,
    # Runner-owned Snapshot: owned tabs per profile per platform, one chat
    # each, all at the same time (founder decision 2026-09-09; lowered from 3
    # to 2 after 63 tabs saturated the machine).
    "tabs_per_profile": 2,
    "max_prompts_per_profile": 3,
    "runs": "single",
    "audit_platforms": ["chatgpt"],
}

@dataclass
class Job:
    id: str
    workflow_id: str
    label: str
    command: List[str]
    status: str
    returncode: Optional[int]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    log_file: str
    run_config: dict = field(default_factory=dict)
    pid: Optional[int] = None
    stop_requested_at: Optional[str] = None

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def new_job_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )

def _is_snapshot_workflow(workflow: Workflow) -> bool:
    return workflow.runner_owned_snapshot or workflow.id == "free_audit_stage1"

def _job_is_runner_owned(job: Job) -> bool:
    """Use frozen job identity, not the mutable default workflow registry."""

    config = job.run_config if isinstance(job.run_config, dict) else {}
    return bool(
        config.get("runner_owned_snapshot") is True
        or (
            config.get("runner_runtime")
            and config.get("runner_measurement")
            and str(config.get("run_id") or "") == job.id
        )
    )

def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def visibility_measurement_plan_command(
    workflow: Workflow,
    measurement_path: Path,
    runtime_path: Path,
    run_id: str,
    preflight_path: Path,
) -> List[str]:
    if not workflow.runner_owned_snapshot:
        raise ValueError("Visibility measurement planning requires a runner workflow")
    return [
        *workflow.command,
        "--measurement",
        str(measurement_path),
        "--runtime",
        str(runtime_path),
        "--run-id",
        run_id,
        "--mode",
        "plan",
        "--preflight-output",
        str(preflight_path),
    ]

class JobStore:

    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        run_root: Optional[Path] = None,
        workflows: Optional[Dict[str, Workflow]] = None,
        measurement_component_factory: Optional[Callable[[Mapping[str, object]], object]] = None,
    ):
        self.repo_root = Path(repo_root)
        self.run_root = Path(run_root) if run_root else self.repo_root / "data" / "ui-runs"
        self.workflows_map = dict(WORKFLOWS if workflows is None else workflows)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._stop_requests = set()
        self._runner_cancel_pending: set[tuple[str, str]] = set()
        self._preparing_executions: set[tuple[str, str]] = set()
        self._measurement_component_factory = measurement_component_factory
        self._read_saved_jobs()

    def workflows(self) -> List[dict]:
        return [asdict(w) for w in self.workflows_map.values()]

    def start(self, workflow_id: str, run_config: Optional[dict] = None, *, request_id: str | None = None) -> Job:
        if request_id is None:
            # Existing programmatic callers keep their intentional new-job
            # semantics. Browser Starts always supply a persisted delivery ID.
            return self._start_new(workflow_id, run_config)
        fingerprint = canonical_sha256({"workflow_id": workflow_id, "run_config": run_config or {}})
        with locked_start_receipt(self.run_root, request_id) as path:
            receipt = load_start_receipt(path)
            if receipt is not None:
                if receipt.get("request_id") != request_id or receipt.get("fingerprint") != fingerprint:
                    raise StartRequestConflict("start_request_mismatch: this Start request already belongs to different settings")
                job_id = receipt.get("job_id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                    raise StartRequestConflict("start_receipt_invalid: Start outcome requires review")
                rejected = load_start_receipt(rejected_start_path(path))
                if rejected is not None:
                    raise ValueError(str(rejected.get("error") or "Start validation failed"))
                existing = self.get(job_id)
                if existing is None:
                    # A second server may have loaded its list before this
                    # request was accepted. Do not needlessly require restart.
                    try:
                        existing = Job(**read_record(self.run_root / job_id / "job.json"))
                    except (RegistryError, TypeError, ValueError):
                        raise StartRequestConflict(
                            f"start_interrupted: request reserved job {job_id}, but preparation did not finish; inspect this job before starting another"
                        ) from None
                    with self._lock:
                        self._jobs[job_id] = existing
                if (existing.run_config.get("host_start_request_id") != request_id
                    or existing.run_config.get("host_start_fingerprint") != fingerprint):
                    raise StartRequestConflict("start_job_binding_mismatch: Start outcome requires review")
                return existing
            job_id = new_job_id()
            reserve_start_receipt(path, request_id=request_id, fingerprint=fingerprint, job_id=job_id)
            try:
                return self._start_new(workflow_id, run_config, job_id=job_id,
                                       start_request_id=request_id, start_fingerprint=fingerprint)
            except (KeyError, ValueError) as exc:
                # Only a pre-job rejection is a definite failure. Once saved,
                # a worker may have started even if the response path failed.
                if not (self.run_root / job_id / "job.json").exists():
                    publish_json(rejected_start_path(path), {"error": str(exc)})
                else:
                    raise StartRequestConflict(f"start_outcome_unknown: request reserved job {job_id}; retry the same request") from exc
                raise

    def _start_new(self, workflow_id, run_config=None, *, job_id=None, start_request_id=None, start_fingerprint=None):
        workflow = self.workflows_map.get(workflow_id)
        if workflow is None:
            raise KeyError(f'Unknown workflow: {workflow_id}')
        normalized = normalize_run_config(run_config, workflow, self.repo_root)
        selected_config = {**copy.deepcopy(normalized['visibility_test']),
                           'runs': 'single', 'planned_runs': 1, 'repetitions': 1, 'test_mode': True}
        job_id = job_id or new_job_id()
        if start_request_id is not None:
            normalized.update(host_start_request_id=start_request_id, host_start_fingerprint=start_fingerprint)
        job_dir = self.run_root / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        selected_path = job_dir / 'snapshot-config.json'
        write_json(selected_path, selected_config)
        normalized.update(snapshot_config=relative_display(selected_path, self.repo_root),
                          runner_host_snapshot_policy_version=1,
                          runner_snapshot_config_sha256=file_sha256(selected_path),
                          snapshot_selection_summary={'scope_kind': 'diagnostic',
                            'prompt_count': len(selected_config['diagnostic_prompts']),
                            'platforms': selected_config['platforms'], 'publication_eligible': False})
        self._prepare_visibility_measurement_job(normalized, job_id, job_dir, workflow)
        job = Job(id=job_id, workflow_id=workflow.id,
                  label=f"Visibility test · {normalized['audit_platforms'][0]} · {len(selected_config['diagnostic_prompts'])} prompts",
                  command=build_command(workflow, normalized), status='queued', returncode=None,
                  created_at=utc_now(), started_at=None, finished_at=None,
                  log_file=relative_display(Path(normalized['runner_execution_log']), self.repo_root),
                  run_config=normalized)
        publish_json(job_dir / 'job-origin.json', asdict(job))
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        threading.Thread(target=self._run_job, args=(job, workflow), daemon=True).start()
        return job

    def stop(self, job_id: str, *, execution_id: str | None = None) -> Job:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if _job_is_runner_owned(current):
            return self.cancel(job_id, execution_id=execution_id)
        stopped_at = utc_now()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in {"queued", "running", "stopping"}:
                return job
            self._stop_requests.add(job_id)
            job.status = "stopping"
            job.stop_requested_at = stopped_at
            self._save(job)
            proc = self._processes.get(job_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
        return self.get(job_id)

    def resume(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if _job_is_runner_owned(job):
            measurement, _ = self._verified_measurement_inputs(job)
            require_active_measurement(measurement)
            component = self._measurement_component(job)
            inspected = component.inspect(job.id)
            self._validate_measurement_result_binding(job, inspected)
            return self._launch_existing_measurement(job, mode="resume", request_id=str(uuid.uuid4()))
        return self.start(job.workflow_id, job.run_config)

    def plan_refetch(self, job_id: str, *, request_id: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Refetch is available only for runner-owned measurements")
        measurement, runtime = self._verified_measurement_inputs(job)
        require_active_measurement(measurement)
        component = self._component_from_runtime(runtime)
        try:
            return component.plan_refetch(job.id, request_id=request_id)
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            if not isinstance(exc, RunNotFoundError):
                raise
            self._require_uncreated_measurement(job, component)
            plan, revision = self._initial_remaining_plan(job, component)
            return {"kind": "initial_remaining", "request_id": request_id, "run_id": job.id, "plan_sha256": plan.plan_sha256, "revision_digest": revision}

    def refetch(self, job_id: str, request: Mapping) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job) or not isinstance(request, Mapping):
            raise ValueError("Invalid Refetch request")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Refetch requires its preview request identity")
        if request.get("kind") == "initial_remaining":
            return self._launch_existing_measurement(job, mode="run", request_id=request_id, recovery=request)
        return self._launch_existing_measurement(job, mode="refetch", request_id=request_id, recovery=request)

    def _initial_remaining_plan(self, job: Job, component, *, measurement=None):
        registry = WorkerRegistry(self.run_root / job.id)
        requests = registry.requests()
        if not requests:
            raise RegistryError("initial_execution_not_proven_unsent")
        marks = []
        for request in requests:
            directory = registry.path(request["execution_id"])
            if any((directory / name).exists() for name in ("worker.json", "launch.json", "exit.json")):
                raise RegistryError("initial_execution_not_proven_unsent")
            marker = read_record(directory / "launch-failure.json", optional=True)
            if marker is None:
                # A dead managed launcher cannot write a failure marker. Its
                # intact guard plus absent spawn intent is an independent,
                # restart-safe proof that this request never reached Popen.
                # observe() verifies death before re-reading the spawn boundary
                # and rejects legacy, uncertain, corrupt, or worker-bearing
                # records. Bind the preview to immutable proof, not sample time.
                observation = registry.observe(request["execution_id"])
                if (observation.get("state") != "stopped"
                    or observation.get("reason") != "launcher_abandoned_before_spawn"):
                    raise RegistryError("initial_execution_not_proven_unsent")
                marks.append({"request": request["request_sha256"],
                              "launch_guard_sha256": request["launch_guard_sha256"],
                              "abandoned_before_spawn": True})
                continue
            cancelled_before_spawn = marker.get("reason") == "cancelled_before_spawn"
            failed_before_spawn = (
                marker.get("reason") == "worker_launch_failed"
                and marker.get("spawn_status") == "not_started"
                and marker.get("request_sha256") == request["request_sha256"]
            )
            if marker.get("execution_id") != request["execution_id"] or not (
                cancelled_before_spawn or failed_before_spawn
            ):
                raise RegistryError("initial_execution_not_proven_unsent")
            marks.append({"request": request["request_sha256"], "marker": marker})
        if measurement is None:
            measurement, _ = self._verified_measurement_inputs(job)
        plan = component.plan(measurement, run_id=job.id)
        if plan.plan_sha256 != job.run_config["runner_plan_sha256"]:
            raise RegistryError("initial_plan_changed")
        return plan, canonical_sha256(marks)

    @staticmethod
    def _require_uncreated_measurement(job: Job, component) -> None:
        """A missing plan inside an existing run is corruption, not unsent work."""
        from visibility_runner.api import RunNotFoundError
        try:
            component.ownership(job.id)
        except RunNotFoundError:
            return
        raise RegistryError("initial_run_already_created")

    @staticmethod
    def _ownership_release_available(observation: Mapping, ownership: Mapping) -> bool:
        return bool(
            observation.get("state") not in {"launching", "running", "unresponsive"}
            and ownership.get("state") == "unknown"
            and ownership.get("status") == "unknown"
            and ownership.get("reason_code") in {
                "owner_record_corrupt", "process_identity_invalid", "process_identity_incomplete",
                "boot_identity_unavailable", "process_inspection_unavailable",
            }
        )

    @staticmethod
    def _add_failure_context(readout: dict, plan) -> None:
        """Join display context by frozen observation identity, never by array order."""
        packets = {}
        for packet in getattr(plan, "packets", ()):
            if str(getattr(packet.kind, "value", packet.kind)) == "measurement":
                for observation_id in packet.observation_ids:
                    packets[observation_id] = packet
        observations = getattr(plan, "observation_plan", {}).get("observations", ())
        contexts = {}
        platform_ordinals = {}
        for observation in observations:
            context = {key: observation[key] for key in (
                "prompt_text", "prompt_id", "platform", "semantic_prompt_id", "semantic_slot_id",
                "branch_name", "subject_name", "subject_kind", "treatment_name", "area_id",
                "clinic_archetype_singular", "clinic_archetype_plural",
            ) if key in observation}
            context["run_ordinal"] = observation.get("run")
            platform = observation.get("platform")
            if platform:
                # Number the frozen plan, so filtering, recovery, and readout
                # reordering cannot rename an observation in the console.
                platform_ordinals[platform] = platform_ordinals.get(platform, 0) + 1
                context["platform_ordinal"] = platform_ordinals[platform]
            location = observation.get("location_context") or {}
            target = observation.get("target_geography") or location.get("target_geography") or {}
            area_name = target.get("name") or location.get("area")
            geography_tier = observation.get("geography_tier") or location.get("geography_tier")
            if area_name:
                context["area_name"] = area_name
            if geography_tier:
                context["geography_tier"] = geography_tier
            # A city/wider-area prompt may be shared by several selected local
            # areas; their origins are distinct from the actual search area.
            origins = observation.get("origin_branches") or location.get("origin_branches") or ()
            origin_area_names = list(dict.fromkeys(
                origin["local_area_name"] for origin in origins if origin.get("local_area_name")
            ))
            if origin_area_names:
                context["origin_area_names"] = origin_area_names
            packet = packets.get(observation["observation_id"])
            if packet is not None:
                context.update(profile_id=packet.profile_id, packet_id=packet.packet_id)
            contexts[observation["observation_id"]] = context
        readout["items"] = [
            {**item, "context": contexts[item["item_id"]]} if item.get("item_id") in contexts else dict(item)
            for item in readout.get("items", ())
        ]

    def verification_control(self, job_id: str, **command) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Verification requires a runner-owned measurement")
        _, runtime = self._verified_measurement_inputs(job)
        component = self._component_from_runtime(runtime)
        from visibility_panel.browser.browser_lifecycle import shared_browser_lifecycle
        endpoint = runtime.get("browser", {}).get("cdp_url", "http://127.0.0.1:9222")
        with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False):
            return component.verification_control(job_id, **command)

    def failure_readout(self, job_id: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Failure readout requires a runner-owned measurement")
        registry = WorkerRegistry(self.run_root / job_id)
        execution_id = job.run_config.get("runner_execution_id")
        observation = registry.observe(execution_id) if execution_id else {"state": "unknown", "reason": "legacy_worker_identity_unavailable", "sampled_at": utc_now()}
        if not execution_id:
            legacy = discover_legacy_worker(job.command)
            observation.update(state={"alive": "running", "absent": "stopped"}.get(legacy["state"], "unknown"), reason=legacy["reason"])
        observation["status"] = {"launching": "alive", "running": "alive", "unresponsive": "alive", "finished": "dead", "stopped": "dead"}.get(observation["state"], "unknown")
        observation["reason_code"] = observation.get("reason", "")
        observation["heartbeat_at"] = (observation.get("heartbeat") or {}).get("utc")
        try:
            measurement, runtime = self._verified_measurement_inputs(job)
            component = self._component_from_runtime(runtime)
        except (ValueError, RegistryError, OSError) as exc:
            return {"run_id": job_id, "items": [], "preview": {"selections": []},
                    "host": {"worker_state": observation["state"], "ownership_state": "unknown",
                             "result_available": observation.get("result_available", False),
                             "finalize_available": False, "refetch_available": False,
                             "ownership_release_available": False,
                             "settings_match_current_runtime": False,
                             "blocked_reason": str(exc)}}
        plan = None
        try:
            ownership = component.ownership(job_id)
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            if not isinstance(exc, RunNotFoundError):
                raise
            deferred_reason = deferred_measurement_reason(measurement)
            if deferred_reason:
                # No engine evidence exists to inspect. Keep the frozen job
                # readable without recreating its now-deferred Google plan.
                return {"run_id": job_id, "items": [], "preview": {"selections": []},
                        "host": {"worker_state": observation["state"], "ownership_state": "available",
                                 "result_available": observation.get("result_available", False),
                                 "finalize_available": False, "refetch_available": False,
                                 "ownership_release_available": False,
                                 "settings_match_current_runtime": True,
                                 "blocked_reason": deferred_reason}}
            plan, _ = self._initial_remaining_plan(job, component, measurement=measurement)
            ownership = {"state": "available", "status": "dead", "reason_code": "never_spawned"}
        observation["ownership_status"] = ownership.get("state", "unknown")
        if plan is not None:
            readout = dict(component.failure_readout_for_plan(plan, worker_observation=observation))
        else:
            # An existing run with missing initialized artifacts must remain
            # blocked; only ownership's missing-run result authorizes fallback.
            readout = dict(component.failure_readout(job_id, worker_observation=observation))
        context_reason = ""
        if plan is None and callable(getattr(component, "inspect", None)):
            # Display the original scope, including any subsequently deferred
            # platform. Replanning here would apply today's admission policy
            # to historical evidence and could change its denominator.
            try:
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                plan = inspected.plan
            except Exception:
                context_reason = "The saved measurement plan could not be verified."
        if plan is not None:
            self._add_failure_context(readout, plan)
        # Engine 0.2.1 describes a run whose frozen execution settings differ
        # from the current runtime under ``execution_policy`` but refuses to
        # execute it; the UI must not offer Refetch for such a run.
        policy = readout.get("execution_policy")
        settings_match = not isinstance(policy, Mapping) or policy.get("matches_current_runtime", True) is not False
        deferred_reason = deferred_measurement_reason(measurement)
        saved_scope = getattr(plan, "scope_manifest", {})
        saved_platforms = saved_scope.get("platforms", ()) if isinstance(saved_scope, Mapping) else ()
        if not deferred_reason and (
            "copilot" in (measurement.get("platforms") or ())
            or "copilot" in (saved_platforms or ())
        ):
            # Availability of new Copilot browser runs does not authorize
            # execution of a saved derived/native plan under a new method.
            saved_modes = saved_scope.get("platform_data_modes") if isinstance(saved_scope, Mapping) else None
            if not isinstance(saved_modes, Mapping) or saved_modes.get("copilot") != "copilot_browser_same_chat_v1":
                deferred_reason = (
                    "This saved Copilot run does not declare the restored browser collection method. "
                    "Its evidence remains available for inspection; Refetch is disabled."
                )
        finalize_available = False
        if (
            execution_id
            and observation["state"] in {"stopped", "finished"}
            and not observation.get("result_available")
            and ownership.get("state") == "available"
        ):
            try:
                result = component.inspect(job.id)
                self._validate_measurement_result_binding(job, result)
                raw_status = getattr(result, "status", "")
                finalize_available = str(getattr(raw_status, "value", raw_status)).casefold() == "succeeded"
            except Exception:
                # A repair button needs verified completed measurements, not
                # an inference from absent host output or a dead worker.
                pass
        readout["host"] = {
            "startup_error": (
                "Chrome could not be restarted safely. Open this run's log for details; measurement did not start."
                if (observation.get("completion") or {}).get("error_code") == "browser_startup_failed" else None
            ),
            "worker_state": observation["state"],
            "ownership_state": ownership.get("state", "unknown"),
            "ownership": {key: ownership.get(key) for key in ("state", "status", "reason_code", "execution_id") if ownership.get(key) is not None},
            "ownership_release_available": bool(settings_match and self._ownership_release_available(observation, ownership)),
            "result_available": observation.get("result_available", False),
            "finalize_available": finalize_available,
            "settings_match_current_runtime": settings_match,
            "refetch_available": bool(
                component.recovery_available
                and observation["status"] == "dead"
                and ownership.get("state") == "available"
                and settings_match
                and not deferred_reason
                and not context_reason
                and readout.get("preview", {}).get("selections")
            ),
            **({"blocked_reason": deferred_reason or context_reason} if deferred_reason or context_reason else {}),
        }
        if execution_id:
            try:
                automatic = auto_recovery_progress(registry.request(execution_id), registry.path(execution_id))
            except (RegistryError, OSError):
                automatic = None
            if automatic is not None:
                readout["host"]["auto_recovery"] = automatic
            # Host containment explains a stopped pass even when the engine's
            # item projection now says only cancelled/dependency blocked.
            # This is diagnostic context, never recovery authorization.
            try:
                containment = read_record(registry.path(execution_id) / "containment.json", optional=True)
            except (RegistryError, OSError):
                containment = None
            if (containment and containment.get("execution_id") == execution_id
                    and containment.get("reason") == "operation_deadline_exceeded"):
                readout["host"]["automatic_stop"] = {
                    "execution_id": execution_id,
                    "reason_code": "operation_deadline_exceeded",
                }
        self._add_record_navigation(readout, runtime, job_id=job_id, plan_available=plan is not None)
        return readout

    def _record_navigation_block_reason(self, readout: Mapping, job_id: str) -> str:
        host = readout.get("host") or {}
        if host.get("worker_state") not in {"finished", "stopped"} or host.get("ownership_state") != "available":
            return "Wait until this run has stopped and its browser ownership is released."
        with self._lock:
            if any(job.id != job_id and job.status in {"queued", "running", "stopping", "finalizing"}
                   for job in self._jobs.values()):
                return "Another job is active. Wait for it to finish before opening this profile."
        return ""

    def _add_record_navigation(self, readout: dict, runtime: dict, *, job_id: str, plan_available: bool) -> None:
        reason = self._record_navigation_block_reason(readout, job_id)
        try:
            profiles = verified_profile_rows(runtime, self.repo_root) if plan_available else {}
        except RecordNavigationError as exc:
            profiles = {}
            reason = str(exc)
        for item in readout.get("items", ()):
            context = item.get("context") or {}
            profile_id, platform = context.get("profile_id"), context.get("platform")
            if platform == "github_copilot":
                item["navigation"] = {
                    "kind": "sdk_transcript", "available": False,
                    "reason": ("Read the saved SDK answer and available Gemini parser evidence."
                               if item.get("source_saved") is True or context.get("source_saved") is True
                               else "No SDK answer was saved; this source has no browser chat."),
                    "profile_id": profile_id or "", "platform": platform,
                }
                continue
            row_reason = reason
            if profile_id not in profiles:
                row_reason = "The saved routine profile cannot be verified."
            elif platform not in PLATFORM_URLS:
                row_reason = "The saved platform cannot be verified."
            item["navigation"] = {
                "kind": "conversation" if latest_conversation_reference(item) else "platform",
                "available": not bool(row_reason), "reason": row_reason,
                "profile_id": profile_id or "", "platform": platform or "",
            }

    def open_record(self, job_id: str, *, item_id: str) -> Mapping:
        """Open a human inspection window without attaching or sending anything."""
        from visibility_panel.browser.browser_lifecycle import BrowserLifecycleLockError, shared_browser_lifecycle

        if not isinstance(item_id, str) or not item_id or len(item_id) > 500:
            raise RecordNavigationError("invalid_item", "Choose a saved measurement record.")
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise RecordNavigationError("unsupported_job", "Chat inspection is available only for area visibility measurements.")
        # Serialize with this job's Refetch admission, then lease the browser
        # exclusively. A live owner of any profile on this endpoint refuses
        # the action before a Chrome process or window can be opened.
        registry = WorkerRegistry(self.run_root / job_id)
        with registry.launch_lock():
            _, runtime = self._verified_measurement_inputs(job)
            endpoint = (runtime.get("browser") or {}).get("cdp_url")
            if not isinstance(endpoint, str) or not endpoint:
                raise RecordNavigationError("browser_unavailable", "The saved browser endpoint cannot be verified.")
            try:
                with shared_browser_lifecycle(environment={"CDP_URL": endpoint}, blocking=False) as lease:
                    with lease.exclusive(blocking=False):
                        readout = self.failure_readout(job_id)
                        matches = [item for item in readout.get("items", ()) if item.get("item_id") == item_id]
                        if len(matches) != 1:
                            raise RecordNavigationError("record_unavailable", "This measurement record is unavailable. Refresh the results and try again.")
                        item = matches[0]
                        navigation = item.get("navigation") or {}
                        if navigation.get("available") is not True:
                            raise RecordNavigationError("record_open_blocked", navigation.get("reason") or "This record cannot be opened right now.")
                        profiles = verified_profile_rows(runtime, self.repo_root)
                        reference = latest_conversation_reference(item)
                        if reference:
                            try:
                                link = self._component_from_runtime(runtime).resolve_recovery_link(job_id, reference)
                                url = conversation_url(link, navigation["platform"])
                            except RecordNavigationError:
                                raise
                            except Exception as exc:
                                raise RecordNavigationError("conversation_unavailable", "The saved chat link cannot be verified. Refresh the results and try again.") from exc
                        else:
                            url = PLATFORM_URLS[navigation["platform"]]
                        launch_inspection(root=self.repo_root, runtime=runtime,
                                          profile=profiles[navigation["profile_id"]], url=url)
                        return navigation
            except BrowserLifecycleLockError as exc:
                raise RecordNavigationError("browser_busy", "Chrome is being used by another job. Wait for it to finish before opening this profile.") from exc

    def release_stale_ownership(self, job_id: str, *, reason: str) -> Mapping:
        """Operator release of a blocked owner record; refused for a live worker."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            raise ValueError("Ownership release is available only for runner-owned measurements")
        reason = str(reason or "").strip()
        if len(reason) < 8 or len(reason) > 500:
            raise ValueError("A release reason of 8 to 500 characters is required")
        registry = WorkerRegistry(self.run_root / job_id)
        execution_id = job.run_config.get("runner_execution_id")
        if execution_id:
            observation = registry.observe(execution_id)
            if observation.get("state") in {"launching", "running", "unresponsive"}:
                raise ValueError("The measurement worker is still alive; stop it before releasing ownership")
        component = self._measurement_component(job)
        outcome = dict(component.release_stale_ownership(job_id, reason=reason))
        try:
            with self._log_path(job).open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"[{utc_now()}] ownership release requested by operator: {reason} -> released={outcome.get('released')}\n")
        except OSError:
            pass
        return outcome

    def resolve_recovery_link(self, job_id: str, reference: str) -> Mapping:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not isinstance(reference, str) or len(reference) > 500:
            raise ValueError("Invalid evidence reference")
        component = self._measurement_component(job)
        link = component.resolve_recovery_link(job_id, reference)
        if link.get("kind") == "artifact":
            # Public artifact verification occurs before returning user-
            # requested evidence. Rendering the summary never reads bodies.
            payload = component.read_artifact(job_id, link["reference"])
            return {"kind": "artifact", "text": payload.decode("utf-8", errors="replace")}
        if link.get("kind") == "conversation":
            return {"kind": "conversation", "url": link["url"]}
        raise ValueError("Unsupported recovery link")

    @staticmethod
    def _parsed_recovery(job_id: str, mode: str, request_id: str, recovery: Mapping | None):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id is required")
        if mode not in {"run", "refetch"}:
            if recovery is not None:
                raise ValueError("Unexpected recovery payload")
            return None
        if not isinstance(recovery, Mapping) or recovery.get("run_id") != job_id or recovery.get("request_id") != request_id:
            raise ValueError("Recovery request does not belong to this job")
        if mode == "run":
            if set(recovery) != {"kind", "request_id", "run_id", "plan_sha256", "revision_digest"} or recovery.get("kind") != "initial_remaining":
                raise ValueError("Invalid initial recovery request")
            return dict(recovery)
        from visibility_runner.recovery.api import RecoveryRequest
        try:
            return RecoveryRequest.from_dict(recovery).to_dict()
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise ValueError("Invalid recovery request") from exc

    def _accepted_execution(self, job_id: str, execution_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        # Response-only metadata; dataclass persistence remains the current job.
        job.accepted_execution_id = execution_id
        return job

    def finalize_output(self, job_id: str, *, request_id: str, expected_execution_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job) or not isinstance(expected_execution_id, str) or not expected_execution_id:
            raise RegistryError("execution_changed")
        return self._launch_existing_measurement(job, mode="finalize", request_id=request_id,
                                                 expected_execution_id=expected_execution_id)

    def _launch_existing_measurement(self, job: Job, *, mode: str, request_id: str,
                                     recovery: Mapping | None = None,
                                     expected_execution_id: str | None = None) -> Job:
        if mode != "finalize":
            measurement, _ = self._verified_measurement_inputs(job)
            require_active_measurement(measurement)
        recovery = self._parsed_recovery(job.id, mode, request_id, recovery)
        if mode == "finalize":
            expected_execution_id = expected_execution_id or job.run_config.get("runner_execution_id")
            if not expected_execution_id:
                raise RegistryError("execution_changed")
        registry = WorkerRegistry(self.run_root / job.id)
        with registry.launch_lock():
            existing = registry.find_request(request_id)
            if existing is not None:
                if (existing.get("recovery") != recovery or existing.get("mode") != mode
                    or existing.get("run_id") != job.id
                    or existing.get("expected_execution_id") != expected_execution_id):
                    raise RegistryError("request_identity_conflict")
                return self._accepted_execution(job.id, existing["execution_id"])
            current = self.get(job.id)
            if current is None:
                raise KeyError(job.id)
            current_id = current.run_config.get("runner_execution_id")
            active = registry.active()
            healthy = [value for value in active if value["state"] in {"launching", "running", "unresponsive"}]
            # A queued request in this host is also an active admission, even
            # before a worker identity can exist. Unknown reconstructed workers
            # remain blocked rather than being assumed queued.
            queued = current.status in {"queued", "finalizing"} and current.run_config.get("runner_worker_state") == "launch_requested"
            with self._lock:
                preparing = ((job.id, current_id) in self._preparing_executions
                             and current.run_config.get("runner_worker_state") == "preparing"
                             and current.status in {"running", "finalizing"})
            if healthy or queued or preparing:
                if current_id is None or (healthy and any(value["execution_id"] != current_id for value in healthy)):
                    raise RegistryError("worker_identity_unknown")
                target = registry.request(current_id)
                payload = {"run_id": job.id, "mode": mode, "bindings": target["bindings"], "recovery": recovery}
                if expected_execution_id is not None:
                    payload["expected_execution_id"] = expected_execution_id
                registry.join_request(current_id, request_id=request_id, payload=payload)
                return self._accepted_execution(job.id, current_id)
            if current_id != job.run_config.get("runner_execution_id") or (mode == "finalize" and current_id != expected_execution_id):
                raise RegistryError("execution_changed")
            if active:
                raise RegistryError("worker_identity_unknown")
            job = current
            legacy_clearance = None
            if not registry.requests():
                legacy_clearance = discover_legacy_worker(job.command)
                if legacy_clearance["state"] != "absent":
                    raise RegistryError("legacy_worker_identity_unknown")
            # Read exact frozen bytes and verify all bindings before component
            # construction. No worker/request record exists on these refusals.
            measurement, runtime = self._verified_measurement_inputs(job)
            if mode != "finalize":
                require_active_measurement(measurement)
            component = self._component_from_runtime(runtime)
            if mode == "run":
                self._require_uncreated_measurement(job, component)
                ownership = {"state": "available"}
                plan, revision = self._initial_remaining_plan(job, component, measurement=measurement)
                expected = {"kind": "initial_remaining", "request_id": request_id, "run_id": job.id,
                            "plan_sha256": plan.plan_sha256, "revision_digest": revision}
                if recovery != expected:
                    raise RegistryError("initial_preview_stale")
                from visibility_runner.api import RunNotFoundError
                try:
                    component.inspect(job.id)
                except RunNotFoundError:
                    pass
                else:
                    raise RegistryError("initial_run_already_created")
            else:
                ownership = component.ownership(job.id)
                if ownership.get("state") not in {"available", "unowned", "released"}:
                    raise RegistryError("engine_ownership_unavailable")
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                if mode == "refetch":
                    fresh = self._parsed_recovery(job.id, mode, request_id, component.plan_refetch(job.id, request_id=request_id))
                    if fresh != recovery or not fresh.get("selections"):
                        raise RegistryError("recovery_preview_stale")
                if mode == "finalize":
                    sample = registry.observe(current_id)
                    raw_status = getattr(inspected, "status", "")
                    if (sample["state"] not in {"stopped", "finished"} or sample.get("result_available")
                        or str(getattr(raw_status, "value", raw_status)).casefold() != "succeeded"):
                        raise RegistryError("finalization_unavailable")
            normalized = copy.deepcopy(job.run_config)
            if legacy_clearance is not None:
                normalized["runner_legacy_command"] = list(job.command)
            request_record = self._prepare_execution(normalized, job.id, registry, mode=mode, request_id=request_id,
                                                     recovery=recovery, expected_execution_id=expected_execution_id,
                                                     restart_browser=mode in {"run", "refetch"})
            if legacy_clearance is not None:
                publish_json(registry.path(request_record["execution_id"]) / "legacy-preflight.json", {"execution_id": request_record["execution_id"], "recorded_at": utc_now(), "process_observation": legacy_clearance, "plan_sha256": normalized["runner_plan_sha256"], "engine_ownership": ownership.get("state")})
            workflow = self.workflows_map[job.workflow_id]
            command_prefix = job.command[:job.command.index("--measurement")]
            workflow = replace(workflow, command=command_prefix, runner_owned_snapshot=True)
            current = copy.deepcopy(job)
            current.run_config = normalized
            current.command = build_command(workflow, normalized)
            current.log_file = relative_display(Path(normalized["runner_execution_log"]), self.repo_root)
            current.status, current.returncode, current.pid = ("finalizing" if mode == "finalize" else "queued"), None, None
            current.finished_at, current.stop_requested_at = None, None
            for name in ("runner_cancel_requested", "runner_cancel_execution_id", "runner_cancel_error", "runner_cancel_previous_status", "finalize_available"):
                current.run_config.pop(name, None)
            current.run_config["runner_output_committed"] = False
            current.run_config["runner_worker_state"] = "launch_requested"
            with self._lock:
                self._jobs[job.id] = current
                self._stop_requests.discard(job.id)
                self._runner_cancel_pending = {key for key in self._runner_cancel_pending if key[0] != job.id}
                self._save(current)
            threading.Thread(target=self._run_job, args=(current, workflow), daemon=True).start()
            return self._accepted_execution(job.id, request_record["execution_id"])

    def _prepare_execution(self, config: dict, job_id: str, registry: WorkerRegistry, *, mode: str, request_id: str, recovery: Mapping | None = None, expected_execution_id: str | None = None, restart_browser: bool = False) -> dict:
        inputs = FrozenExecutionInputs.from_job_config(config)
        sign_in = {}
        if mode in {"run", "refetch"} and (self.repo_root / "config/copilot-sign-in.json").exists():
            from visibility_panel.execution.copilot_settings import browser_account_bindings
            from visibility_panel.integrations.visibility_runner import coerce_runtime_config
            frozen_bytes = resolve_repo_relative(config["runner_runtime"], base=self.repo_root).read_bytes()
            if hashlib.sha256(frozen_bytes).hexdigest() != config["runner_runtime_sha256"]:
                raise ValueError("Frozen runtime changed before Copilot sign-in admission")
            frozen_runtime = json.loads(frozen_bytes)
            selected = coerce_runtime_config(frozen_runtime).selected_profiles
            sign_in = browser_account_bindings(self.repo_root,
                [{"id": row.id, "chrome_directory": row.chrome_directory} for row in selected])
        record, _ = registry.create_request(request_id=request_id, run_id=job_id, mode=mode, bindings={
            # Start/Refetch reopen the same selected profiles without old tabs.
            # Normal work stays background; verification has its own opt-in.
            "browser_restart_on_start": restart_browser and mode in {"run", "refetch"},
            **({"auto_recovery_rounds": AUTO_RECOVERY_ROUNDS}
               if mode in {"resume", "refetch"} or (mode == "run" and recovery is not None) else {}),
            **({"copilot_verification_policy": "automatic_hold_v1"} if mode in {"run", "refetch"} else {}),
            **({"copilot_account_emails": sign_in} if sign_in else {}),
            **inputs.request_bindings(),
            "prior_result_reference": config.get("runner_result") if mode != "run" else None,
            "legacy_command": config.get("runner_legacy_command"),
            **({"host_snapshot_policy_version": config["runner_host_snapshot_policy_version"],
                "snapshot_config_sha256": config["runner_snapshot_config_sha256"]}
               if "runner_host_snapshot_policy_version" in config else {}),
        }, recovery=recovery, expected_execution_id=expected_execution_id, managed_launch=True)
        execution = FrozenExecutionRequest.from_record(record)
        directory = registry.path(execution.execution_id)
        config.update({
            "runner_execution_id": execution.execution_id,
            "runner_execution_mode": execution.mode,
            "runner_execution_request": relative_display(directory / "request.json", self.repo_root),
            "runner_execution_log": str(directory / "stdout.log"),
            "runner_run_preflight": relative_display(directory / "preflight.json", self.repo_root),
            "runner_execution_result": relative_display(directory / "result.json", self.repo_root),
        })
        if mode == "run":
            config["runner_result"] = config["runner_execution_result"]
        return record

    def cancel(self, job_id: str, *, execution_id: str | None = None) -> Job:
        """Cancel only the execution explicitly named by the caller."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            return self.stop(job_id)
        registry = WorkerRegistry(self.run_root / job_id)
        key = (job_id, execution_id)
        with registry.launch_lock():
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    raise KeyError(job_id)
                if not execution_id or current.run_config.get("runner_execution_id") != execution_id:
                    raise RegistryError("execution_changed")
                if current.run_config.get("runner_execution_mode") == "finalize" and current.status in {"queued", "running", "finalizing"}:
                    raise RegistryError("finalizer_cannot_be_stopped")
                if current.status not in {"queued", "running", "stopping", "planned"}:
                    return copy.deepcopy(current)
                current.run_config["runner_cancel_requested"] = True
                current.run_config["runner_cancel_execution_id"] = execution_id
                cancel_marker = registry.path(execution_id) / "cancel-request.json"
                if not cancel_marker.exists():
                    publish_json(cancel_marker, {"execution_id": execution_id,
                        "request_sha256": registry.request(execution_id)["request_sha256"], "recorded_at": utc_now()})
                current.run_config.pop("runner_cancel_error", None)
                current.run_config.setdefault("runner_cancel_previous_status", current.status)
                current.stop_requested_at = current.stop_requested_at or utc_now()
                if current.status == "queued" or current.run_config.get("runner_worker_state") == "preparing":
                    self._stop_requests.add(job_id)
                    marker = registry.path(execution_id) / "launch-failure.json"
                    if not marker.exists():
                        publish_json(marker, {"execution_id": execution_id, "reason": "cancelled_before_spawn", "recorded_at": utc_now()})
                    current.status = "cancelled"
                    current.finished_at = current.finished_at or utc_now()
                    self._save(current)
                    return copy.deepcopy(current)
                if key in self._runner_cancel_pending:
                    return copy.deepcopy(current)
                self._runner_cancel_pending.add(key)
                current.status = "stopping"
                self._save(current)
                job = copy.deepcopy(current)
        # Engine cancellation may wait on storage/transport. Keep the store
        # unlocked, retain the captured target and fence every callback.
        try:
            component = self._measurement_component(job)
            inspected = component.inspect(job.id)
            self._validate_measurement_result_binding(job, inspected)
            result = component.cancel(job.id, execution_id=execution_id)
            applied = self._apply_measurement_result(job.id, result, execution_id=execution_id)
            with self._lock:
                self._runner_cancel_pending.discard(key)
            return applied
        except Exception as exc:
            from visibility_runner.api import RunNotFoundError
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    raise KeyError(job_id) from exc
                if current.run_config.get("runner_execution_id") != execution_id:
                    self._runner_cancel_pending.discard(key)
                    return copy.deepcopy(current)
                if not isinstance(exc, RunNotFoundError):
                    current.run_config["runner_cancel_error"] = f"{type(exc).__name__}: {exc}"
                self._save(current)
            threading.Thread(target=self._cancel_runner_when_created,
                             args=(job_id, execution_id), daemon=True).start()
            return self.get(job_id)

    def inspect(self, job_id: str) -> Job:
        """Refresh a runner job from its component-owned durable state."""

        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if not _job_is_runner_owned(job):
            return job
        component = self._measurement_component(job)
        result = component.inspect(job.id)
        return self._apply_measurement_result(job.id, result, execution_id=job.run_config.get("runner_execution_id"))

    def _finalize_measurement_if_ready(self, job: Job, component, result, *, sample=None) -> Job | None:
        """Queue only local output publication after a proven worker exit."""

        self._validate_measurement_result_binding(job, result)
        if job.run_config.get("runner_execution_mode") == "finalize" or not self._execution_is_current(job):
            return None
        execution_id = job.run_config.get("runner_execution_id")
        raw_status = getattr(result, "status", "")
        result_status = str(getattr(raw_status, "value", raw_status)).casefold()
        if execution_id and result_status == "succeeded" and hasattr(component, "ownership"):
            registry = WorkerRegistry(self.run_root / job.id)
            sample = registry.observe(execution_id) if sample is None else sample
            if sample["state"] in {"stopped", "finished"} and not sample.get("result_available"):
                # This authorizes only local output publication. The worker's
                # finalize mode cannot submit a measurement or start recovery.
                # A result without its completion receipt is not committed;
                # a fresh execution preserves that uncommitted file as well.
                return self._launch_existing_measurement(job, mode="finalize", request_id=f"finalize-{execution_id}", expected_execution_id=execution_id)
        return None

    def _cancel_runner_when_created(self, job_id: str, execution_id: str) -> None:
        """Deliver one pending cancel after the guarded worker creates state.

        Runner.create() makes the run visible through public ``inspect`` before
        execution, and Runner.cancel() installs the interprocess admission
        barrier used by all send fences. This watcher never signals the worker
        or reads private runner artifacts; it delegates the explicit request as
        soon as that public run state is available.
        """

        key = (job_id, execution_id)
        while True:
            with self._lock:
                if key not in self._runner_cancel_pending:
                    return
                live = self._jobs.get(job_id)
                if live is None:
                    self._runner_cancel_pending.discard(key)
                    return
                if live.run_config.get("runner_execution_id") != execution_id:
                    self._runner_cancel_pending.discard(key)
                    return
                job = copy.deepcopy(live)
            if job.status in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                with self._lock:
                    self._runner_cancel_pending.discard(key)
                return
            try:
                component = self._measurement_component(job)
                # This is readiness for an already-accepted cancellation, not
                # new-run admission or a uniqueness guard.
                inspected = component.inspect(job.id)
                self._validate_measurement_result_binding(job, inspected)
                result = component.cancel(job.id, execution_id=execution_id)
                self._apply_measurement_result(job.id, result, execution_id=execution_id)
            except Exception as exc:
                from visibility_runner.api import RunNotFoundError

                if not isinstance(exc, RunNotFoundError):
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is not None and current.run_config.get("runner_execution_id") == execution_id:
                            current.run_config["runner_cancel_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                            self._save(current)
                time.sleep(0.05)
                continue
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None and current.run_config.get("runner_execution_id") == execution_id:
                    current.run_config.pop("runner_cancel_error", None)
                    self._save(current)
                    self._runner_cancel_pending.discard(key)
            return

    def list(self, limit: int | None = 25, *, scope: str | None = None) -> List[Job]:
        """Read saved jobs newest first; a named history filters before limiting."""
        if scope not in {None, "visibility", "automation"}:
            raise ValueError("Unknown job history scope")
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            if scope is not None:
                visibility = scope == "visibility"
                jobs = [job for job in jobs if _job_is_runner_owned(job) == visibility]
            return copy.deepcopy(jobs[:limit])

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return copy.deepcopy(self._jobs.get(job_id))

    def log_text(self, job_id: str, max_bytes: int = 100_000) -> str:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        path = self._log_path(job)
        if not path.exists():
            return ""
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    def _run_job(self, job: Job, workflow: Workflow):
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.run_config.get("runner_execution_id") != job.run_config.get("runner_execution_id"):
                return
            if job.id in self._stop_requests:
                job.status = (
                    "cancelled" if _job_is_runner_owned(job) else "stopped"
                )
                job.finished_at = job.finished_at or utc_now()
                self._save(job)
                self._stop_requests.discard(job.id)
                return
            job.status = "finalizing" if job.run_config.get("runner_execution_mode") == "finalize" else "running"
            job.started_at = job.started_at or utc_now()
            if _job_is_runner_owned(job):
                job.run_config["runner_worker_state"] = "preparing"
                self._preparing_executions.add((job.id, job.run_config["runner_execution_id"]))
            self._save(job)
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
        job_dir = self.run_root / job.id
        log_path = self._log_path(job)
        spawn_status = "not_started"
        try:
            env.update(prepare_job_environment(workflow, job.run_config, self.repo_root, job_dir))
            with log_path.open("w", encoding="utf-8", newline="\n") as log:
                log.write("$ " + " ".join(job.command) + "\n\n")
                log.write("[run config]\n")
                log.write(json.dumps(job.run_config, indent=2, ensure_ascii=False) + "\n\n")
                log.flush()
                registry = WorkerRegistry(job_dir) if _job_is_runner_owned(job) else None
                # Serialize the last admission check and Popen with Stop and
                # replacement requests. Environment/log preparation above may
                # block, so its earlier state is never permission to spawn.
                with registry.launch_lock() if registry else contextlib.nullcontext():
                    with self._lock:
                        current = self._jobs.get(job.id)
                        execution_id = job.run_config.get("runner_execution_id")
                        if current is None or current.run_config.get("runner_execution_id") != execution_id:
                            return
                        cancelled = job.id in self._stop_requests or (
                            registry is not None and current.run_config.get("runner_cancel_requested") is True
                            and current.run_config.get("runner_cancel_execution_id") == execution_id)
                        if cancelled:
                            if registry is not None:
                                marker = registry.path(execution_id) / "launch-failure.json"
                                if not marker.exists():
                                    publish_json(marker, {"execution_id": execution_id, "reason": "cancelled_before_spawn", "recorded_at": utc_now()})
                            current.status = "cancelled" if registry else "stopped"
                            current.finished_at = current.finished_at or utc_now()
                            self._runner_cancel_pending.discard((job.id, execution_id))
                            self._save(current)
                            return
                    spawn_status = "unknown"
                    if registry is not None and execution_id:
                        registry.begin_spawn(execution_id)
                    try:
                        proc = subprocess.Popen(
                            job.command, cwd=self.repo_root, env=env, stdout=log,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True,
                        )
                    except OSError:
                        spawn_status = "not_started"
                        raise
                    spawn_status = "started"
                    if registry is not None and execution_id:
                        registry.record_launch(execution_id, proc.pid)
                    with self._lock:
                        current = self._jobs.get(job.id)
                        if current is None or current.run_config.get("runner_execution_id") != execution_id:
                            return
                        self._processes[job.id] = proc
                        stop_requested = job.id in self._stop_requests
                        current.pid = proc.pid
                        if registry is not None:
                            current.run_config["runner_worker_state"] = "launching"
                            self._preparing_executions.discard((job.id, execution_id))
                        self._save(current)
                if _job_is_runner_owned(job) and job.run_config.get("runner_execution_id"):
                    threading.Thread(target=self._monitor_execution, args=(job.id, job.run_config["runner_execution_id"]), daemon=True).start()
                if stop_requested and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                returncode = proc.wait()
            if _job_is_runner_owned(job):
                execution_id = job.run_config.get("runner_execution_id")
                if execution_id:
                    WorkerRegistry(job_dir).observe_exit(execution_id, returncode)
                    sample = WorkerRegistry(job_dir).observe(execution_id)
                    if (sample.get("completion") or {}).get("error_code") == "cancelled_before_measurement":
                        self._update_execution(job, status="cancelled", returncode=returncode, finished_at=utc_now(), pid=None)
                        return
                    if sample.get("result_available"):
                        with self._lock:
                            current = self._jobs[job.id]
                            if current.run_config.get("runner_execution_id") == execution_id:
                                current.run_config["runner_result"] = current.run_config["runner_execution_result"]
                                current.run_config["runner_output_committed"] = True
                                self._save(current)
                if not self._execution_is_current(job):
                    return
                # Only the subprocess supplies its exit code. Record that fact
                # before reconciling durable runner state: the runner may have
                # finished before this worker serialized/fsynced its result,
                # or the worker may have died while runner state says running.
                self._update_execution(job, returncode=returncode, pid=None)
                try:
                    component = self._measurement_component(job)
                    result = component.inspect(job.id)
                    settled = self._apply_measurement_result(job.id, result, execution_id=execution_id)
                    self._finalize_measurement_if_ready(settled, component, result)
                except Exception:
                    # A typed non-SUCCEEDED result normally exits 2 and remains
                    # inspectable. If no durable runner result exists at all,
                    # surface the worker failure without attempting recovery.
                    self._update_execution(
                        job,
                        status="failed",
                        returncode=returncode,
                        finished_at=utc_now(),
                        pid=None,
                    )
                return
        except Exception as exc:
            with log_path.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"\n[runner] {exc}\n")
            if _job_is_runner_owned(job):
                if spawn_status == "not_started" and job.run_config.get("runner_execution_id"):
                    registry = WorkerRegistry(job_dir)
                    try:
                        execution_id = job.run_config["runner_execution_id"]
                        request = registry.request(execution_id)
                        publish_json(registry.path(execution_id) / "launch-failure.json", {
                            "execution_id": execution_id,
                            "request_sha256": request["request_sha256"],
                            "reason": "worker_launch_failed",
                            "spawn_status": "not_started",
                            "recorded_at": utc_now(),
                        })
                    except (OSError, RegistryError):
                        pass
                # Startup/wait failures are host failures, not an observed
                # subprocess exit. Preserve its known code and PID (if any).
                self._update_execution(job, status="failed", finished_at=utc_now())
            else:
                self._update_execution(job, status="failed", returncode=1, finished_at=utc_now())
        finally:
            with self._lock:
                self._preparing_executions.discard((job.id, job.run_config.get("runner_execution_id")))
                if self._jobs[job.id].run_config.get("runner_execution_id") == job.run_config.get("runner_execution_id"):
                    self._processes.pop(job.id, None)
                    self._stop_requests.discard(job.id)

    def _execution_is_current(self, job: Job) -> bool:
        with self._lock:
            current = self._jobs.get(job.id)
            return current is not None and current.run_config.get("runner_execution_id") == job.run_config.get("runner_execution_id")

    def _update_execution(self, job: Job, **fields):
        with self._lock:
            current = self._jobs.get(job.id)
            if current is None or current.run_config.get("runner_execution_id") != job.run_config.get("runner_execution_id"):
                return
            for key, value in fields.items():
                setattr(current, key, value)
            self._save(current)

    def _monitor_execution(self, job_id: str, execution_id: str) -> None:
        registry = WorkerRegistry(self.run_root / job_id)

        def request_cancel():
            current = self.get(job_id)
            if current and current.run_config.get("runner_execution_id") == execution_id:
                def cancel_verified_execution():
                    latest = self.get(job_id)
                    if latest and latest.run_config.get("runner_execution_id") == execution_id:
                        component = self._measurement_component(latest)
                        component.cancel(job_id, execution_id=execution_id)
                # Cancellation can itself wait on a transport. The monitor
                # must remain responsive to enforce its bounded shutdown.
                threading.Thread(target=cancel_verified_execution, args=(), daemon=True).start()

        def sample_progress():
            current = self.get(job_id)
            if current is None or current.run_config.get("runner_execution_id") != execution_id:
                return {}
            return self._measurement_component(current).ownership(job_id)

        watchdog = WorkerWatchdog(registry, execution_id, request_cancel=request_cancel, sample_progress=sample_progress)
        unknown_delay = 1.0
        while True:
            current = self.get(job_id)
            if current is None or current.run_config.get("runner_execution_id") != execution_id:
                return
            try:
                sample = registry.observe(execution_id)
                worker_state = sample["state"]
                if worker_state == "unknown":
                    # A restarted server has no Popen handle. Registration or
                    # temporarily unreadable evidence may still become valid;
                    # keep observing without cancellation or signals. Bound
                    # retry frequency for permanently damaged/unknown records.
                    time.sleep(unknown_delay)
                    unknown_delay = min(30.0, unknown_delay * 2)
                    continue
                unknown_delay = 1.0
                if worker_state in {"finished", "stopped"}:
                    watchdog.tick()
                    # An exit observed by a different parent has no fabricated
                    # returncode. Completion is independent durable evidence.
                    completion = sample.get("completion")
                    with self._lock:
                        live = self._jobs[job_id]
                        if live.run_config.get("runner_execution_id") != execution_id:
                            return
                        live.run_config["runner_worker_state"] = worker_state
                        live.pid = None
                        observed_returncode = sample.get("observed_returncode")
                        if observed_returncode is not None:
                            live.returncode = observed_returncode
                        if completion and completion.get("error_code") == "cancelled_before_measurement":
                            live.status = "cancelled"
                            live.finished_at = completion.get("completed_at")
                        elif completion and completion.get("result_reference"):
                            result_path = registry.path(execution_id) / "result.json"
                            raw = result_path.read_bytes()
                            if hashlib.sha256(raw).hexdigest() != completion.get("result_sha256"):
                                raise RegistryError("committed_host_result_mismatch")
                            live.status = completion.get("measurement_status", "needs_attention")
                            if live.status == "succeeded" and live.returncode not in {None, 0}:
                                live.status = "failed"
                            live.finished_at = completion.get("completed_at")
                            live.run_config["runner_output_committed"] = True
                            live.run_config["runner_result"] = relative_display(result_path, self.repo_root)
                        elif live.status in {"queued", "running", "finalizing", "stopping"}:
                            live.status = "failed" if live.returncode not in {None, 0} else "needs_attention"
                        self._save(live)
                        settled = copy.deepcopy(live)
                    if not sample.get("result_available") and settled.run_config.get("runner_execution_mode") != "finalize":
                        try:
                            component = self._measurement_component(settled)
                            result = component.inspect(job_id)
                            self._finalize_measurement_if_ready(settled, component, result, sample=sample)
                        except Exception:
                            # Missing/invalid engine output or unavailable
                            # ownership cannot authorize finalization. Leave
                            # the job available for explicit inspection.
                            pass
                    # A failed finalizer remains visible instead of spawning
                    # an unbounded chain of automatic local repair workers.
                    return
                watchdog.tick()
                with self._lock:
                    live = self._jobs[job_id]
                    if live.run_config.get("runner_execution_id") == execution_id:
                        live.run_config["runner_worker_state"] = worker_state
                        live.pid = sample.get("pid")
                        if live.status != "stopping":
                            live.status = "finalizing" if live.run_config.get("runner_execution_mode") == "finalize" else "running"
                        live.finished_at = None
                        self._save(live)
            except Exception:
                # Failed inspection means blocked monitoring, never dead. The
                # durable engine lock is still the execution admission gate.
                pass
            time.sleep(5)

    def _read_saved_jobs(self):
        pending_runner_cancels: list[tuple[str, str]] = []
        monitor_executions: list[tuple[str, str]] = []
        metadata_paths = set(self.run_root.glob("*/job.json"))
        metadata_paths.update(path.parent / "job.json" for path in self.run_root.glob("*/job-origin.json"))
        for meta in sorted(metadata_paths):
            data = read_json_or_none(meta)
            rebuild_projection = data is None
            if data is None:
                try:
                    data = read_record(meta.parent / "job-origin.json")
                except RegistryError:
                    # Corruption remains visible as blocked work even when no
                    # trustworthy metadata remains to reconstruct its scope.
                    if (meta.parent / "executions").exists():
                        blocked = Job(id=meta.parent.name, workflow_id="free_audit_stage1", label="Visibility metadata requires repair", command=[], status="needs_attention", returncode=None, created_at="", started_at=None, finished_at=None, log_file=str(meta.parent / "stdout.log"), run_config={"runner_owned_snapshot": True, "runner_worker_state": "unknown", "registry_error": "job_metadata_unreadable"})
                        self._jobs[blocked.id] = blocked
                    continue
            try:
                requests = WorkerRegistry(meta.parent).requests()
                if requests:
                    latest = max(requests, key=lambda item: (item.get("ordinal", 0), item["created_at"]))
                    rebuild_projection = rebuild_projection or data.get("run_config", {}).get("runner_execution_id") != latest["execution_id"]
                    if not rebuild_projection:
                        latest = None
                if requests and latest is not None:
                    directory = WorkerRegistry(meta.parent).path(latest["execution_id"])
                    config = data["run_config"]
                    config.update(runner_execution_id=latest["execution_id"], runner_execution_mode=latest["mode"], runner_execution_request=relative_display(directory / "request.json", self.repo_root), runner_execution_result=relative_display(directory / "result.json", self.repo_root), runner_run_preflight=relative_display(directory / "preflight.json", self.repo_root), runner_execution_log=str(directory / "stdout.log"))
                    data["log_file"] = relative_display(directory / "stdout.log", self.repo_root)
                    data["status"] = "finalizing" if latest["mode"] == "finalize" else "running"
                    # A request may survive a crash before its job projection.
                    # Never attribute the previous worker's exit/output/cancel
                    # evidence to this newly reconstructed execution.
                    data.update(returncode=None, pid=None, finished_at=None, stop_requested_at=None)
                    config["runner_output_committed"] = False
                    config["runner_worker_state"] = "unknown"
                    for name in ("runner_cancel_requested", "runner_cancel_execution_id", "runner_cancel_error", "runner_cancel_previous_status", "finalize_available"):
                        config.pop(name, None)
                    data["command"] = _visibility_measurement_command(WORKFLOWS["free_audit_stage1"], config)
            except (RegistryError, KeyError, ValueError):
                data["status"] = "needs_attention"
                data.setdefault("run_config", {})["registry_error"] = "execution_projection_unavailable"
            try:
                data.setdefault("run_config", {})
                data.setdefault("pid", None)
                data.setdefault("stop_requested_at", None)
                job = Job(**data)
            except Exception:
                continue
            was_active = job.status in {"queued", "running", "finalizing", "stopping"}
            if was_active or (_job_is_runner_owned(job) and job.run_config.get("runner_execution_id")):
                if _job_is_runner_owned(job):
                    execution_id = job.run_config.get("runner_execution_id")
                    if execution_id:
                        sample = WorkerRegistry(self.run_root / job.id).observe(execution_id)
                        job.run_config["runner_worker_state"] = sample["state"]
                        if sample["state"] in {"launching", "running", "unresponsive"}:
                            job.pid = sample.get("pid")
                            if job.run_config.get("runner_execution_mode") == "finalize":
                                job.status = "finalizing"
                            elif not was_active:
                                job.status = "running"
                        elif sample["state"] == "unknown":
                            if was_active:
                                job.status = "needs_attention"
                        else:
                            if was_active:
                                job.status = "needs_attention"
                            job.pid = None
                        monitor_executions.append((job.id, execution_id))
                    else:
                        job.status = "needs_attention"
                        job.run_config["runner_worker_state"] = "unknown"
                    if (job.run_config.get("runner_cancel_requested") is True
                        and execution_id
                        and job.run_config.get("runner_cancel_execution_id") == execution_id
                        and job.run_config.get("runner_execution_mode") != "finalize"
                        and sample["state"] not in {"finished", "stopped"}):
                        job.status = "stopping"
                        job.finished_at = None
                        key = (job.id, execution_id)
                        self._runner_cancel_pending.add(key)
                        pending_runner_cancels.append(key)
                else:
                    job.status = "failed"
                    if not _job_is_runner_owned(job):
                        job.returncode = 1
                    # A restarted host cannot recover a subprocess exit code
                    # it never observed. Preserve known runner-worker codes,
                    # including None, rather than inventing one from status.
                    job.finished_at = job.finished_at or utc_now()
                    job.pid = None
            self._jobs[job.id] = job
        for job_id, execution_id in monitor_executions:
            threading.Thread(target=self._monitor_execution, args=(job_id, execution_id), daemon=True).start()
        for job_id, execution_id in pending_runner_cancels:
            thread = threading.Thread(
                target=self._cancel_runner_when_created,
                args=(job_id, execution_id),
                daemon=True,
            )
            thread.start()

    def _save(self, job: Job):
        write_json(self.run_root / job.id / "job.json", asdict(job))

    def _update(self, job_id: str, **fields):
        with self._lock:
            job = self._jobs[job_id]
            for key, value in fields.items():
                setattr(job, key, value)
            self._save(job)

    def _log_path(self, job: Job) -> Path:
        return resolve_repo_relative(job.log_file, base=self.repo_root)

    def _prepare_visibility_measurement_job(
        self,
        normalized: dict,
        job_id: str,
        job_dir: Path,
        workflow: Workflow,
    ) -> None:
        """Freeze inputs and obtain the worker's pure, hash-bound plan."""

        snapshot_path = resolve_repo_relative(
            str(normalized.get("snapshot_config") or ""),
            base=self.repo_root,
        )
        frozen_snapshot = read_json_or_none(snapshot_path)
        if not isinstance(frozen_snapshot, dict):
            raise ValueError("Runner-owned Snapshot selection is unavailable")
        measurement = measurement_from_frozen_snapshot(frozen_snapshot)

        profile_ids = normalized["selected_profile_ids"]
        profile_rows = selected_snapshot_profile_rows(profile_ids, self.repo_root)
        if not profile_rows:
            raise ValueError("Runner-owned Snapshot requires at least one browser profile")
        manifest_path = job_dir / "runner-profiles.json"
        write_json(manifest_path, profile_rows)
        # The runner verifies the parsed JSON value, not presentation bytes.
        # Bind the exact canonical payload so harmless indentation/key-order
        # changes cannot make the pinned manifest appear to be tampered with.
        manifest_sha256 = canonical_sha256(profile_rows)
        tabs_per_provider = max(
            1,
            int(
                normalized.get("tabs_per_profile")
                or DEFAULT_RUN_CONFIG["tabs_per_profile"]
            ),
        )
        # SDK sources have their own account queue. Copilot parsing uses the
        # same Gemini profile/tab capacity as ordinary Gemini observations.
        browser_platforms = set(measurement.get("platforms") or ()) - {"github_copilot"}
        if "github_copilot" in measurement.get("platforms", ()):
            browser_platforms.add("gemini")
        platform_count = max(1, len(browser_platforms))

        runtime = {
            "artifact_root": str(
                self.repo_root
                / "data"
                / "engine-runs"
            ),
            "profiles": {
                "selected_profile_ids": [row["id"] for row in profile_rows],
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha256,
                "selected_profiles": profile_rows,
                "lease_timeout_seconds": 300,
            },
            "browser": {
                "cdp_url": str(
                    normalized["cdp_url"]
                ).strip(),
                "launch_policy": "attach_only",
                "background_only": True,
                "copilot_verification_policy": "automatic_hold_v1",
            },
            # The runner freezes this exact policy into the hash-bound plan.
            "retry_policy": copy.deepcopy(RUNNER_SNAPSHOT_RETRY_POLICY),
            # Founder decision 2026-09-09: every selected profile runs
            # `tabs_per_provider` owned tabs per platform at the same time,
            # each with its own chat. The runner plans one packet per tab, so
            # max_concurrency admits every packet at once; "Parallel tabs" no
            # longer caps this workflow.
            "max_concurrency": len(profile_rows) * platform_count * tabs_per_provider,
            "tabs_per_provider": tabs_per_provider,
            # Founder decision 2026-09-09, after 63 tabs opening together
            # produced 23 navigation timeouts: a pair's tabs start in waves,
            # one slot per wave, this many seconds apart.
            "packet_start_stagger_seconds": normalized["packet_start_stagger_seconds"],
            "worker_timeout_seconds": 900,
        }
        if normalized.get("user_data_dir"):
            runtime["browser"]["user_data_dir"] = normalized["user_data_dir"]
        if "github_copilot" in measurement.get("platforms", ()):
            from visibility_panel.execution.copilot_settings import runtime_settings
            runtime["github_copilot"] = runtime_settings(
                normalized.get("github_copilot_runtime"), measurement["github_copilot"],
            )
        measurement_path = job_dir / "measurement.json"
        runtime_path = job_dir / "runtime.json"
        write_json(measurement_path, measurement)
        write_json(runtime_path, runtime)

        plan_preflight_path = job_dir / "plan-preflight.json"
        run_preflight_path = job_dir / "run-preflight.json"
        result_path = job_dir / "measurement-result.json"
        plan_command = visibility_measurement_plan_command(
            workflow,
            measurement_path,
            runtime_path,
            job_id,
            plan_preflight_path,
        )
        environment = dict(os.environ)
        existing_pythonpath = environment.get("PYTHONPATH", "")
        local_src = str(self.repo_root / "panel" / "src")
        environment["PYTHONPATH"] = local_src + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        subprocess.run(
            plan_command,
            cwd=self.repo_root,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        preflight = read_json_or_none(plan_preflight_path)
        if not isinstance(preflight, dict):
            raise ValueError("Visibility measurement planning produced no preflight")
        if preflight.get("mode") != "plan" or preflight.get("run_id") != job_id:
            raise ValueError("Visibility measurement preflight identity is invalid")

        measurement_sha256 = file_sha256(measurement_path)
        runtime_sha256 = file_sha256(runtime_path)
        if preflight.get("measurement_file_sha256") != measurement_sha256:
            raise ValueError("Visibility measurement preflight changed its measurement input")
        if preflight.get("runtime_file_sha256") != runtime_sha256:
            raise ValueError("Visibility measurement preflight changed its runtime input")
        plan_sha256 = str(preflight.get("plan_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
            raise ValueError("Visibility measurement preflight has no valid plan hash")
        authorized_max = preflight.get("authorized_max_external_submissions")
        if (
            isinstance(authorized_max, bool)
            or not isinstance(authorized_max, int)
            or authorized_max < 0
        ):
            raise ValueError("Visibility measurement preflight has no valid send ceiling")

        normalized.update(
            {
                "runner_owned_snapshot": True,
                "run_id": job_id,
                "runner_measurement": relative_display(
                    measurement_path, self.repo_root
                ),
                "runner_runtime": relative_display(runtime_path, self.repo_root),
                "runner_plan_preflight": relative_display(
                    plan_preflight_path, self.repo_root
                ),
                "runner_run_preflight": relative_display(
                    run_preflight_path, self.repo_root
                ),
                "runner_result": relative_display(result_path, self.repo_root),
                "runner_plan_sha256": plan_sha256,
                "runner_measurement_sha256": measurement_sha256,
                "runner_runtime_sha256": runtime_sha256,
                "runner_authorized_max_external_submissions": authorized_max,
            }
        )
        if preflight.get("plan_serialization_version") == "visibility_runner.run_plan.v1":
            normalized["runner_frozen_plan_input"] = relative_display(
                plan_preflight_path, self.repo_root,
            )
        registry = WorkerRegistry(job_dir)
        with registry.launch_lock():
            self._prepare_execution(normalized, job_id, registry, mode="run", request_id=str(uuid.uuid4()), restart_browser=True)

    def _verified_measurement_inputs(self, job: Job):
        from visibility_panel.visibility_reports.host_job_artifacts import (
            read_frozen_measurement_inputs, snapshot_policy_records, verify_snapshot_policy,
            SnapshotReportPreparationError,
        )
        job_dir = self.run_root / job.id
        if job_dir.is_symlink():
            raise SnapshotReportPreparationError("Frozen job directory must not be a symbolic link")
        for name, key in (("measurement.json", "runner_measurement"), ("runtime.json", "runner_runtime")):
            value = job.run_config.get(key)
            if not isinstance(value, str) or resolve_repo_relative(value, base=self.repo_root).absolute() != (job_dir / name).absolute():
                raise SnapshotReportPreparationError(f"Frozen {name} path does not match this job")
        measurement, runtime = read_frozen_measurement_inputs(job_dir, job.run_config)
        inputs = FrozenExecutionInputs.from_job_config(job.run_config)
        execution_id = job.run_config.get("runner_execution_id")
        request = WorkerRegistry(job_dir).request(execution_id) if execution_id else None
        policy_records = snapshot_policy_records(job_dir, request)
        for record in policy_records[:2]:
            if record:
                try:
                    inputs.verify_record(record, prefix="runner_", label="job records")
                except ValueError as exc:
                    raise SnapshotReportPreparationError(str(exc)) from exc
        if request is not None:
            if request.get("run_id") != job.id:
                raise RegistryError("request_identity_mismatch")
            try:
                inputs.verify_record(request["bindings"])
            except ValueError as exc:
                raise SnapshotReportPreparationError(str(exc)) from exc
        verify_snapshot_policy(job_dir, job.run_config, *policy_records, measurement=measurement)
        return measurement, runtime

    def _measurement_component(self, job: Job):
        _, runtime = self._verified_measurement_inputs(job)
        return self._component_from_runtime(runtime)

    def _component_from_runtime(self, runtime: Mapping):
        factory = self._measurement_component_factory
        if factory is None:
            from visibility_panel.integrations.visibility_runner import VisibilityMeasurementComponent
            factory = VisibilityMeasurementComponent
        return factory(runtime)

    def _apply_measurement_result(self, job_id: str, result: object, *, execution_id: str | None = None) -> Job:
        raw_status = getattr(result, "status", "")
        status = str(getattr(raw_status, "value", raw_status) or "").casefold()
        allowed = {
            "planned",
            "running",
            "succeeded",
            "partial",
            "needs_attention",
            "failed",
            "cancelled",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported runner status: {status or '(blank)'}")
        terminal_statuses = {
            "succeeded",
            "partial",
            "needs_attention",
            "failed",
            "cancelled",
        }
        # Reconcile under the same lock used to record worker exit. An inspect
        # call can overlap proc.wait(), so a pre-exit copy of the job must not
        # erase a newly observed exit code or resurrect a completed worker.
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if execution_id is not None and current.run_config.get("runner_execution_id") != execution_id:
                return copy.deepcopy(current)
            self._validate_measurement_result_binding(current, result)
            if status == "succeeded":
                if not current.run_config.get("runner_output_committed"):
                    if current.returncode is not None:
                        status = "failed" if current.returncode != 0 else "needs_attention"
                    elif current.status in {"failed", "needs_attention", "cancelled"}:
                        status = current.status
                    else:
                        status = "finalizing" if current.run_config.get("runner_execution_mode") == "finalize" else "running"
                elif current.returncode is not None and current.returncode != 0:
                    status = "failed"
            elif status in {"planned", "running"}:
                if current.returncode is not None:
                    # A stale nonterminal inspect must not erase terminal
                    # proof already recorded by the worker's exit handler.
                    status = (
                        current.status
                        if current.status in terminal_statuses
                        else "failed"
                    )
                elif current.status == "failed":
                    # Store reload or startup failure left no observed exit;
                    # nonterminal runner metadata cannot prove a live worker.
                    status = "failed"
            if (current.run_config.get("runner_execution_mode") == "finalize"
                and current.returncode is None and current.status == "finalizing"
                and status in {"planned", "running"}):
                status = "finalizing"
            current_execution = current.run_config.get("runner_execution_id")
            if (current_execution and current.returncode is None
                and current.run_config.get("runner_cancel_requested") is True
                and current.run_config.get("runner_cancel_execution_id") == current_execution
                and WorkerRegistry(self.run_root / job_id).observe(current_execution)["state"] not in {"finished", "stopped"}):
                # Engine cancellation records a send barrier before browser
                # cleanup/worker exit. Neither terminal engine state nor an
                # unreadable host identity is proof that stopping completed.
                status = "stopping"
            terminal = status in terminal_statuses
            current.status = status
            current.finished_at = (
                current.finished_at or utc_now() if terminal else None
            )
            # Neither PID ownership nor returncode comes from Runner.inspect.
            # They are set only by the host subprocess lifecycle.
            self._save(current)
            return copy.deepcopy(current)

    @staticmethod
    def _validate_measurement_result_binding(job: Job, result: object) -> None:
        """Require one public result to match this job's frozen runner plan."""

        result_run_id = str(getattr(result, "run_id", "") or "")
        if result_run_id != job.id:
            raise ValueError(
                "Runner result identity mismatch: "
                f"expected {job.id!r}, received {result_run_id or '(blank)'!r}"
            )
        expected_plan_sha256 = str(
            job.run_config.get("runner_plan_sha256") or ""
        ).casefold()
        plan = getattr(result, "plan", None)
        actual_plan_sha256 = str(
            getattr(plan, "plan_sha256", "") or ""
        ).casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256)
            or actual_plan_sha256 != expected_plan_sha256
        ):
            raise ValueError(
                "Runner result plan mismatch: "
                f"expected {expected_plan_sha256 or '(blank)'!r}, "
                f"received {actual_plan_sha256 or '(blank)'!r}"
            )

def relative_display(path: Path, root: Path) -> str:
    path = Path(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)

def jobs_to_dicts(jobs: Iterable[Job]) -> List[dict]:
    return [asdict(job) for job in jobs]

def _visibility_measurement_command(
    workflow: Workflow,
    run_config: Mapping[str, object],
) -> List[str]:
    """Build the worker's irreversible, hash-bound run invocation."""

    required = {
        "runner_measurement": "measurement input",
        "runner_runtime": "runtime input",
        "run_id": "run ID",
        "runner_run_preflight": "run preflight output",
        "runner_result": "result output",
        "runner_plan_sha256": "plan hash",
        "runner_measurement_sha256": "measurement hash",
        "runner_runtime_sha256": "runtime hash",
    }
    missing = [label for key, label in required.items() if not run_config.get(key)]
    ceiling = run_config.get("runner_authorized_max_external_submissions")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
        missing.append("authorized send ceiling")
    if missing:
        raise ValueError(
            "Runner-owned Snapshot command is not frozen: " + ", ".join(missing)
        )
    command = [
        *workflow.command,
        "--measurement",
        str(run_config["runner_measurement"]),
        "--runtime",
        str(run_config["runner_runtime"]),
        "--run-id",
        str(run_config["run_id"]),
        "--mode",
        str(run_config.get("runner_execution_mode") or "run"),
        "--preflight-output",
        str(run_config["runner_run_preflight"]),
        "--result-output",
        str(run_config.get("runner_execution_result") or run_config["runner_result"]),
        *FrozenExecutionInputs.from_job_config(run_config).command_arguments(),
    ]
    if run_config.get("runner_execution_request"):
        command.extend(["--execution-request", str(run_config["runner_execution_request"])])
    if (run_config.get("runner_execution_mode") or "run") == "run" and run_config.get("runner_frozen_plan_input"):
        command.extend(["--plan-input", str(run_config["runner_frozen_plan_input"])])
    return command


def normalize_run_config(run_config, workflow, repo_root_path=None):
    """Bind a new diagnostic request to the explicit local profile settings."""
    if not workflow.runner_owned_snapshot:
        raise ValueError('This panel launches visibility measurements only.')
    provided = dict(run_config or {})
    if provided.get('use_all_configured_profiles') or provided.get('retry_missing'):
        raise ValueError('Use explicit selected profiles and the existing job recovery controls.')
    measurement = measurement_from_frozen_snapshot(provided.get('visibility_test'))
    settings = load_settings(repo_root_path)
    selected = provided.get('selected_profile_ids')
    if selected != settings['selected_profile_ids']:
        raise ValueError('The request must use the explicit local profile selection.')
    rows = selected_snapshot_profile_rows(selected, repo_root_path)
    return {**copy.deepcopy(DEFAULT_RUN_CONFIG), **provided, 'regime': 'logged_in',
            'selected_profile_ids': selected, 'selected_profiles': selected,
            'profile_count': len(rows), 'use_all_configured_profiles': False,
            'runs': 'single', 'visibility_test': measurement,
            'audit_platforms': measurement['platforms'],
            'tabs_per_profile': settings['tabs_per_provider'],
            'packet_start_stagger_seconds': settings['packet_start_stagger_seconds'],
            'cdp_url': settings['cdp_url'],
            **({'user_data_dir': settings['user_data_dir']} if settings.get('user_data_dir') else {})}


def build_command(workflow, run_config):
    if not workflow.runner_owned_snapshot:
        raise ValueError('This panel launches visibility measurements only.')
    return _visibility_measurement_command(workflow, run_config)


def prepare_job_environment(workflow, run_config, repo_root_path, job_dir):
    return {}  # Frozen runner inputs remain the only execution settings.


