#!/usr/bin/env python3
"""Guarded new-run, strict-resume, Refetch and output-finalization worker.

Execution and host output publication share the engine's exclusive owner.

The external ``visibility_runner`` package must be supplied by the host
environment.  This script does not vendor or alter that package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import contextlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from visibility_panel.execution.worker_registry import (
    Heartbeat, WorkerRegistry, publish_json, read_record, utc_now,
    discover_legacy_worker,
)
from visibility_panel.execution.measurement_browser import (
    BrowserStartupCancelled, MeasurementBrowserCoordinator, measurement_browser_session,
)
from visibility_panel.execution.measurement_inputs import (
    FrozenExecutionInputs, FrozenExecutionRequest, measurement_send_limit as _measurement_send_limit,
)
from visibility_panel.execution.measurement_recovery import run_recovery_rounds
from visibility_panel.execution.measurement_platforms import require_active_measurement

from visibility_panel.integrations.visibility_runner import (
    VisibilityMeasurementComponent,
    adapt_result,
    coerce_measurement_config,
    coerce_runtime_config,
)
from visibility_runner.api import MeasurementConfig, RunPlan, ensure_descriptor_limit
from visibility_panel.visibility_reports.host_job_artifacts import (
    snapshot_policy_records, verify_snapshot_policy,
)

# The launchd default of 256 open files is too small for a run that watches
# every send-attempt file and holds the browser connection. Raise the soft
# limit towards 8192 (never above the hard limit) before anything is opened;
# the effective value is recorded in the worker record for the readout.
DESCRIPTOR_LIMIT = ensure_descriptor_limit(8192)


def _read_json_file(path: Path) -> tuple[object, str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _json_value(value: Any) -> Any:
    """Convert public immutable runner contracts without changing ordering."""

    if isinstance(value, RunPlan):
        from visibility_runner import serialize_run_plan
        return serialize_run_plan(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
            if not (isinstance(value, MeasurementConfig)
                    and field.name == "diagnostic_prompts"
                    and value.scope_kind != "diagnostic"
                    and not value.diagnostic_prompts)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot serialize value of type {type(value).__name__}")


def _write_new_json(path: Path, payload: object) -> None:
    publish_json(path, payload)


def _safe_capability_detail(value: object) -> dict[str, object] | None:
    """Keep only the engine's content-free source admission diagnosis."""
    if not isinstance(value, Mapping):
        return None
    stage, reason = value.get("stage"), value.get("reason")
    if not isinstance(stage, str) or not isinstance(reason, str) or stage not in {
        "runtime_tool_catalog", "session_tools",
    } or reason not in {
        "missing_required_tools", "unexpected_tools", "metadata_malformed", "metadata_rpc_failed",
    }:
        return None
    detail: dict[str, object] = {"stage": stage, "reason": reason}
    key = {"missing_required_tools": "missing_tools", "unexpected_tools": "unexpected_tools"}.get(reason)
    if key:
        names = value.get(key)
        if not isinstance(names, (list, tuple)) or not names or len(names) > 100:
            return None
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
               or re.match(r"(?:gh[pousr]_|github_pat_)", name) for name in names):
            return None
        if key == "missing_tools" and any(name not in {"web_search", "web_fetch"} for name in names):
            return None
        detail[key] = list(dict.fromkeys(names))
    return detail


def _result_issue_messages(result: object) -> list[dict[str, object]]:
    """Expose unique failure reasons and typed capability facts, without raw details."""

    sources = (
        result,
        *getattr(result, "packets", ()),
        *getattr(result, "observations", ()),
    )
    messages = []
    seen = set()
    for source in sources:
        for issue in getattr(source, "issues", ()):
            raw_detail = getattr(issue, "detail", None)
            capability = _safe_capability_detail(raw_detail.get("capability_detail")) if (
                issue.code == "copilot_source_capability_unavailable" and isinstance(raw_detail, Mapping)
            ) else None
            message = issue.message
            if capability and capability["reason"] == "missing_required_tools":
                message = (
                    "GitHub Copilot could not start: the runtime is missing required tools "
                    f"({', '.join(capability['missing_tools'])}). No source prompt was sent."
                )
            key = (issue.code, message, json.dumps(capability, sort_keys=True))
            if key not in seen:
                seen.add(key)
                messages.append({"code": issue.code, "message": message,
                                 **({"capability_detail": capability} if capability else {})})
    return messages


def _encode_json(payload: object) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _load_preflight_plan(
    path: Path,
    *,
    measurement: MeasurementConfig,
    runtime: object,
    run_id: str,
    measurement_sha256: str,
    runtime_sha256: str,
    expected_plan_sha256: str | None,
):
    """Consume the exact plan already approved during host preflight."""

    from visibility_panel.integrations.visibility_runner import load_measurement_plan

    payload, _ = _read_json_file(path)
    expected = {
        "schema_version": "visibility-host-preflight-v1",
        "mode": "plan",
        "run_id": run_id,
        "measurement_file_sha256": measurement_sha256,
        "runtime_file_sha256": runtime_sha256,
        "plan_sha256": expected_plan_sha256,
    }
    if not isinstance(payload, Mapping) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("frozen plan preflight differs from its execution inputs")
    plan = load_measurement_plan(
        payload.get("plan"), measurement=measurement, runtime=runtime, run_id=run_id,
    )
    if plan.plan_sha256 != expected_plan_sha256:
        raise ValueError("frozen plan hash differs from --expected-plan-sha256")
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute one new runner-owned visibility measurement."
        )
    )
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--mode",
        choices=("plan", "run", "resume", "refetch", "finalize"),
        default="plan",
        help="'plan' performs no runner I/O; 'run' creates and executes once.",
    )
    parser.add_argument("--preflight-output", type=Path, required=True)
    parser.add_argument("--result-output", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-measurement-file-sha256")
    parser.add_argument("--expected-runtime-file-sha256")
    parser.add_argument("--expected-max-external-submissions", type=int)
    parser.add_argument("--execution-request", type=Path)
    parser.add_argument("--plan-input", type=Path, help="Approved planning preflight; run executes its exact frozen plan.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.plan_input is not None and args.mode != "run":
        raise ValueError("--plan-input is supported only for an initial run")
    measurement_payload, measurement_file_sha256 = _read_json_file(
        args.measurement
    )
    runtime_payload, runtime_file_sha256 = _read_json_file(args.runtime)
    if not isinstance(measurement_payload, Mapping):
        raise TypeError("measurement JSON must contain an object")
    if not isinstance(runtime_payload, Mapping):
        raise TypeError("runtime JSON must contain an object")
    if args.mode != "finalize":
        require_active_measurement(measurement_payload)

    measurement = coerce_measurement_config(measurement_payload)
    runtime = coerce_runtime_config(runtime_payload)
    registry = None
    request = None
    heartbeat = None
    host_policy = None
    policy_records = ()
    if args.execution_request is not None:
        request = read_record(args.execution_request)
        execution_id = str(request.get("execution_id") or "")
        registry = WorkerRegistry(args.execution_request.resolve().parents[2])
        execution_dir = registry.path(execution_id)
        request = registry.request(execution_id)
        if args.execution_request.resolve() != execution_dir / "request.json":
            raise ValueError("execution request path mismatch")
        if request.get("run_id") != args.run_id or request.get("mode") != args.mode:
            raise ValueError("execution request identity mismatch")
        FrozenExecutionInputs(measurement_file_sha256, runtime_file_sha256,
            args.expected_plan_sha256, args.expected_max_external_submissions).verify_record(request["bindings"])
        if args.result_output.resolve() != execution_dir / "result.json" or args.preflight_output.resolve() != execution_dir / "preflight.json":
            raise ValueError("execution output path mismatch")
        job_dir = args.execution_request.resolve().parents[2]
        policy_records = snapshot_policy_records(job_dir, request)
        host_policy = verify_snapshot_policy(job_dir, *policy_records, measurement=measurement_payload)
    elif args.mode in {"resume", "refetch", "finalize"}:
        raise ValueError("existing-run execution requires --execution-request")

    def event_sink(event):
        if heartbeat is None or event.run_id != args.run_id:
            return
        kind = str(getattr(event.kind, "value", event.kind))
        if kind == "desktop_action":
            print(json.dumps({"event": kind, "observation_id": event.observation_id,
                "detail": dict(event.detail)}, sort_keys=True), flush=True)
        operation_id = event.observation_id or event.packet_id
        # Adapter events declare their own absolute deadline. A missing
        # deadline is unknown progress and never a reason to terminate.
        deadline = event.detail.get("deadline_monotonic")
        # A settled operation can have failed or yielded to a safe retry. Its
        # old deadline must leave the mirror without implying a successful
        # measurement; the next operation supplies its own deadline.
        heartbeat.progress(operation_id=operation_id, deadline_monotonic=deadline if isinstance(deadline, (int, float)) else None, complete=kind in {"operation_completed", "observation_completed", "packet_completed"})

    component = VisibilityMeasurementComponent(runtime, event_sink=event_sink)
    if args.plan_input is not None:
        plan = _load_preflight_plan(
            args.plan_input, measurement=measurement, runtime=runtime, run_id=args.run_id,
            measurement_sha256=measurement_file_sha256, runtime_sha256=runtime_file_sha256,
            expected_plan_sha256=args.expected_plan_sha256,
        )
    elif args.mode in {"plan", "run"}:
        # Compatibility for standalone callers without a saved preflight.
        plan = component.plan(measurement, run_id=args.run_id)
    else:
        plan = component.inspect(args.run_id).plan
    if registry is not None:
        checked_policy = verify_snapshot_policy(job_dir, *snapshot_policy_records(job_dir, registry.request(execution_id)), measurement=measurement_payload, plan=plan)
        if checked_policy != host_policy:
            raise ValueError("frozen host publication policy changed during worker admission")
        registry.register(execution_id, descriptor_limit=DESCRIPTOR_LIMIT)
        heartbeat = Heartbeat(registry, execution_id)
    authorized_external_submission_ceiling = (
        _measurement_send_limit(plan)
    )
    artifact_run_dir = runtime.artifact_root / args.run_id
    artifact_run_dir_resolved = artifact_run_dir.resolve()
    input_paths = {
        args.measurement.resolve(),
        args.runtime.resolve(),
    }
    if args.plan_input is not None:
        input_paths.add(args.plan_input.resolve())
    preflight_output = args.preflight_output.resolve()
    if preflight_output in input_paths:
        raise ValueError(
            "--preflight-output must be distinct from the input files"
        )
    if preflight_output.is_relative_to(artifact_run_dir_resolved):
        raise ValueError(
            "--preflight-output must be outside the runner artifact run "
            "directory"
        )
    if args.preflight_output.exists():
        raise FileExistsError(
            "refusing to overwrite preflight output: "
            f"{args.preflight_output}"
        )

    preflight = {
        "schema_version": "visibility-host-preflight-v1",
        "mode": args.mode,
        "run_id": args.run_id,
        "measurement_file": str(args.measurement.resolve()),
        "measurement_file_sha256": measurement_file_sha256,
        "runtime_file": str(args.runtime.resolve()),
        "runtime_file_sha256": runtime_file_sha256,
        "artifact_run_dir": str(artifact_run_dir),
        "artifact_run_dir_existed_before_execution": artifact_run_dir.exists(),
        "plan_sha256": plan.plan_sha256,
        "expected_plan_sha256": args.expected_plan_sha256,
        "expected_measurement_file_sha256": (
            args.expected_measurement_file_sha256
        ),
        "expected_runtime_file_sha256": args.expected_runtime_file_sha256,
        "expected_max_external_submissions": (
            args.expected_max_external_submissions
        ),
        "initial_external_submissions": (
            plan.submission_budget.total_external_submissions
        ),
        "authorized_max_external_submissions": (
            authorized_external_submission_ceiling
        ),
        "observation_count": len(plan.observation_ids),
        "packet_ids": [packet.packet_id for packet in plan.packets],
        "selected_profile_ids": list(plan.selected_profile_ids),
        "profile_manifest_sha256": plan.profile_manifest_sha256,
        "submission_budget": _json_value(plan.submission_budget),
        "plan": _json_value(plan),
        **({"plan_serialization_version": plan.schema_version} if isinstance(plan, RunPlan) else {}),
        **({key: host_policy[key] for key in ("host_snapshot_policy_version", "snapshot_config_sha256")} if host_policy else {}),
    }

    if args.mode != "plan":
        if args.result_output is None:
            raise ValueError("--result-output is required in run mode")
        if not args.expected_plan_sha256:
            raise ValueError("--expected-plan-sha256 is required in run mode")
        if not args.expected_measurement_file_sha256:
            raise ValueError(
                "--expected-measurement-file-sha256 is required in run mode"
            )
        if not args.expected_runtime_file_sha256:
            raise ValueError(
                "--expected-runtime-file-sha256 is required in run mode"
            )
        if args.expected_max_external_submissions is None:
            raise ValueError(
                "--expected-max-external-submissions is required in run mode"
            )
        protected_paths = {*input_paths, preflight_output}
        result_output = args.result_output.resolve()
        if result_output in protected_paths:
            raise ValueError(
                "--result-output must be distinct from the inputs and "
                "--preflight-output"
            )
        if result_output.is_relative_to(artifact_run_dir_resolved):
            raise ValueError(
                "--result-output must be outside the runner artifact run "
                "directory"
            )
        if args.result_output.exists():
            raise FileExistsError(
                f"refusing to overwrite result output: {args.result_output}"
            )
        if args.mode == "run" and artifact_run_dir.exists():
            raise FileExistsError(
                f"refusing to run an existing run ID: {artifact_run_dir}"
            )
        inputs = FrozenExecutionInputs(args.expected_measurement_file_sha256,
            args.expected_runtime_file_sha256, args.expected_plan_sha256,
            args.expected_max_external_submissions)
        inputs.verify_files(measurement_sha256=measurement_file_sha256,
                            runtime_sha256=runtime_file_sha256)
        inputs.verify_plan(plan)

    if args.mode == "plan":
        _write_new_json(args.preflight_output, preflight)
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "plan_sha256": plan.plan_sha256,
                    "observation_count": len(plan.observation_ids),
                    "max_external_submissions": (
                        authorized_external_submission_ceiling
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    legacy_outputs = None
    if registry is None:
        # Preserve explicit standalone CLI output paths while adding the same
        # mandatory durable worker registry used by the web launcher. This is
        # the already hash-bound initial run, never a new recovery allowance.
        legacy_outputs = (args.preflight_output, args.result_output)
        registry = WorkerRegistry(args.result_output.parent / ".worker-runs" / hashlib.sha256(args.run_id.encode()).hexdigest()[:24])
        with registry.launch_lock():
            request, created = registry.create_request(request_id="initial-" + args.expected_plan_sha256, run_id=args.run_id, mode=args.mode, bindings={"measurement_sha256": measurement_file_sha256, "runtime_sha256": runtime_file_sha256, "plan_sha256": plan.plan_sha256, "prior_result_reference": None, "external_preflight": str(args.preflight_output.resolve()), "external_result": str(args.result_output.resolve())})
            if not created:
                raise FileExistsError("standalone execution request already recorded")
        directory = registry.path(request["execution_id"])
        args.preflight_output, args.result_output = directory / "preflight.json", directory / "result.json"
        registry.register(request["execution_id"], descriptor_limit=DESCRIPTOR_LIMIT)
        heartbeat = Heartbeat(registry, request["execution_id"])
    execution = FrozenExecutionRequest.from_record(request)
    execution_id = execution.execution_id
    def execution_cancelled():
        marker = read_record(registry.path(execution_id) / "cancel-request.json", optional=True)
        if marker is not None:
            if marker.get("execution_id") != execution_id or marker.get("request_sha256") != request["request_sha256"]:
                raise ValueError("browser startup cancellation identity mismatch")
            return True
        return False

    def check_start_cancelled():
        if execution_cancelled():
            raise BrowserStartupCancelled("Stopped before measurement; no prompts were sent by this execution")

    with component.execution(execution.run_id, execution_id=execution_id):
        with heartbeat if heartbeat is not None else contextlib.nullcontext():
            browser = MeasurementBrowserCoordinator(component, runtime_payload, request, execution=execution,
                root=Path(__file__).resolve().parents[1], execution_dir=registry.path(execution_id),
                check_cancelled=check_start_cancelled, startup_session=measurement_browser_session)
            try:
                if request and request["bindings"].get("legacy_command"):
                    observation = discover_legacy_worker(request["bindings"]["legacy_command"], exclude_pid=os.getpid())
                    if observation["state"] != "absent":
                        raise ValueError("legacy worker absence could not be revalidated")
                _write_new_json(args.preflight_output, preflight)
                if legacy_outputs:
                    _write_new_json(legacy_outputs[0], preflight)
                verification_policy = request["bindings"].get("copilot_verification_policy")
                if verification_policy is not None:
                    if args.mode not in {"run", "refetch"} or verification_policy != "automatic_hold_v1":
                        raise ValueError("unsupported execution verification policy")
                    outcome = component.enable_automatic_verification(args.run_id,
                        request_id=request["request_id"], request_sha256=request["request_sha256"],
                        **({"plan": plan} if args.mode == "run" else {}),
                        **({"account_emails": request["bindings"]["copilot_account_emails"]}
                           if "copilot_account_emails" in request["bindings"] else {}))
                    _write_new_json(registry.path(execution_id) / "verification-policy.json", outcome)
                with browser.prepare():
                    with component.defer_browser_startup(browser.start):
                        def first_round():
                            if args.mode == "refetch":
                                if request["bindings"].get("auto_recovery_rounds", 1) > 1:
                                    return component.refetch(browser.recovery_request, replace_checked_sends=False)
                                return component.refetch(browser.recovery_request)
                            if args.mode == "resume":
                                return component.resume(args.run_id)
                            if args.mode == "finalize":
                                return component.inspect(args.run_id)
                            return component.run(plan)

                        result = run_recovery_rounds(component, request, registry.path(execution_id),
                            first_round=first_round, cancelled=execution_cancelled)
                    app_result = adapt_result(result)
                    result_payload = {
                        "schema_version": "visibility-host-result-v1",
                        "measurement_file_sha256": measurement_file_sha256,
                        "runtime_file_sha256": runtime_file_sha256,
                        "plan_sha256": plan.plan_sha256,
                        "execution_id": execution_id,
                        "request_id": request["request_id"] if request else None,
                        "mode": args.mode,
                        "committed_at": utc_now(),
                        "prior_result_reference": request["bindings"].get("prior_result_reference") if request else None,
                        "recovery_completed_at": utc_now() if args.mode == "refetch" else None,
                        "result": _json_value(result),
                        **({"auto_recovery": read_record(registry.path(execution_id) / "auto-recovery.json")}
                           if request["bindings"].get("auto_recovery_rounds", 1) > 1 else {}),
                        **({key: host_policy[key] for key in ("host_snapshot_policy_version", "snapshot_config_sha256")} if host_policy else {}),
                    }
                    _write_new_json(args.result_output, result_payload)
                    if legacy_outputs:
                        _write_new_json(legacy_outputs[1], result_payload)
                    if registry:
                        registry.complete(execution_id, status=result.status.value, result_path=args.result_output)
            except BrowserStartupCancelled as exc:
                registry.complete(execution_id, status="cancelled", result_path=None, error_code="cancelled_before_measurement")
                print(f"[browser] {exc}", flush=True)
                return 2
            except BaseException:
                if registry:
                    with contextlib.suppress(OSError, FileExistsError):
                        registry.complete(execution_id, status="unknown", result_path=None,
                            error_code="browser_startup_failed" if browser.startup_pending else "worker_execution_failed")
                raise
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status.value,
                "observation_count": len(result.observations),
                "transport_counts": _json_value(result.transport_counts),
                **({"issues": _result_issue_messages(result)} if not app_result.succeeded else {}),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if app_result.succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
