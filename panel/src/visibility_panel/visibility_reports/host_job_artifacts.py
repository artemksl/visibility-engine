"""Verify immutable host job inputs and completed outputs for local reports.

Only host-owned records are read here; runner evidence stays behind its API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

DEFAULT_RUN_ROOT = Path("data/ui-runs")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
HOST_SNAPSHOT_POLICY_VERSION = 1


class SnapshotReportPreparationError(ValueError):
    """The named UI job cannot safely produce a report."""


def _validated_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if (
        not _JOB_ID.fullmatch(job_id)
        or job_id in {".", ".."}
        or ".." in Path(job_id).parts
    ):
        raise SnapshotReportPreparationError(
            "job ID must be a single safe UI-run identifier"
        )
    return job_id


def _directory(path: str | Path, *, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SnapshotReportPreparationError(
            f"{label} is unavailable: {path}"
        ) from exc
    if not resolved.is_dir():
        raise SnapshotReportPreparationError(
            f"{label} is not a directory: {resolved}"
        )
    return resolved


def _run_root(repo_root: Path, value: str | Path | None) -> Path:
    requested = DEFAULT_RUN_ROOT if value is None else Path(value)
    if not requested.is_absolute():
        requested = repo_root / requested
    resolved = _directory(requested, label="UI run root")
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise SnapshotReportPreparationError(
            "UI run root must be inside the repository"
        ) from exc
    return resolved


def _job_directory(run_root: Path, job_id: str) -> Path:
    requested = run_root / job_id
    if requested.is_symlink():
        raise SnapshotReportPreparationError(
            "UI job directory must not be a symbolic link"
        )
    job_dir = _directory(requested, label="UI job directory")
    if job_dir.parent != run_root:
        raise SnapshotReportPreparationError(
            "UI job directory escaped the configured run root"
        )
    return job_dir


def _job_file_bytes(job_dir: Path, filename: str) -> bytes:
    """Read one real job file without following a symbolic-link target."""

    requested = job_dir / filename
    if requested.is_symlink():
        raise SnapshotReportPreparationError(
            f"UI job {filename} must not be a symbolic link"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(requested, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SnapshotReportPreparationError(
                    f"UI job {filename} must be a regular file"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SnapshotReportPreparationError(
            f"UI job {filename} cannot be read safely"
        ) from exc


def _json_object(payload: bytes, *, filename: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotReportPreparationError(
            f"UI job {filename} must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SnapshotReportPreparationError(
            f"UI job {filename} must contain an object"
        )
    return value


def _completed_job_bindings(job_dir: Path, job_id: str) -> dict[str, str]:
    """Admit only a completed host job before runtime or runner access."""

    job = _json_object(
        _job_file_bytes(job_dir, "job.json"),
        filename="job.json",
    )
    if job.get("id") != job_id:
        raise SnapshotReportPreparationError(
            "UI job.json identity does not match the requested job"
        )
    run_config = job.get("run_config")
    if (
        job.get("workflow_id") != "free_audit_stage1"
        or not isinstance(run_config, dict)
        or run_config.get("runner_owned_snapshot") is not True
    ):
        raise SnapshotReportPreparationError(
            "UI job must be a runner-owned free_audit_stage1 workflow"
        )
    if run_config.get("run_id") != job_id:
        raise SnapshotReportPreparationError(
            "UI job run_config.run_id does not match the requested job"
        )
    if job.get("status") != "succeeded":
        raise SnapshotReportPreparationError(
            "report data requires UI job status succeeded"
        )
    returncode = job.get("returncode")
    execution_id = run_config.get("runner_execution_id")
    if (type(returncode) is not int or returncode != 0) and not (returncode is None and execution_id):
        raise SnapshotReportPreparationError(
            "report data requires an integer UI job returncode of 0"
        )
    if "pid" not in job or job["pid"] is not None:
        raise SnapshotReportPreparationError(
            "completed UI job requires a cleared worker pid"
        )
    finished_at = job.get("finished_at")
    if not isinstance(finished_at, str) or not finished_at.strip():
        raise SnapshotReportPreparationError(
            "completed UI job must have finished_at metadata"
        )
    bindings: dict[str, str] = {}
    for key in (
        "runner_runtime_sha256",
        "runner_measurement_sha256",
        "runner_plan_sha256",
    ):
        digest = run_config.get(key)
        if not isinstance(digest, str) or not _SHA256.fullmatch(
            digest.casefold()
        ):
            raise SnapshotReportPreparationError(
                f"UI job requires a valid {key} binding"
            )
        bindings[key] = digest.casefold()
    if execution_id is not None:
        if not isinstance(execution_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", execution_id):
            raise SnapshotReportPreparationError("invalid execution identity")
        bindings["execution_id"] = execution_id
        _completed_execution_directory(job_dir, bindings)
    return bindings


def _completed_execution_directory(job_dir: Path, bindings: Mapping[str, str]) -> Path:
    """Resolve a committed output from this exact execution, never a pointer alone."""
    execution_id = bindings.get("execution_id")
    if not execution_id:
        return job_dir
    parent = job_dir / "executions"
    directory = parent / execution_id
    if parent.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise SnapshotReportPreparationError("execution directory unavailable")
    completion = _json_object(_job_file_bytes(directory, "completion.json"), filename="completion.json")
    encoded = _job_file_bytes(directory, "result.json")
    result = _json_object(encoded, filename="result.json")
    if (completion.get("schema_version") != "host-completion-v1"
        or completion.get("execution_id") != execution_id
        or completion.get("measurement_status") != "succeeded"
        or completion.get("result_reference") != "result.json"
        or completion.get("result_sha256") != hashlib.sha256(encoded).hexdigest()
        or result.get("schema_version") != "visibility-host-result-v1"
        or result.get("execution_id") != execution_id
        or result.get("plan_sha256") != bindings["runner_plan_sha256"]):
        raise SnapshotReportPreparationError("execution completion does not bind its successful result")
    return directory


def _verified_runtime(
    job_dir: Path,
    bindings: Mapping[str, str],
) -> dict[str, object]:
    return read_frozen_measurement_inputs(job_dir, bindings)[1]


def read_frozen_measurement_inputs(job_dir: Path, bindings: Mapping) -> tuple[dict, dict]:
    """Verify exact immutable bytes before parsing either engine input."""
    runtime_bytes = _job_file_bytes(job_dir, "runtime.json")
    measurement_bytes = _job_file_bytes(job_dir, "measurement.json")
    for filename, payload, key in (
        ("runtime.json", runtime_bytes, "runner_runtime_sha256"),
        ("measurement.json", measurement_bytes, "runner_measurement_sha256"),
    ):
        if hashlib.sha256(payload).hexdigest() != bindings.get(key):
            raise SnapshotReportPreparationError(
                f"UI job {filename} byte SHA-256 does not match its binding"
            )
    # Both byte identities are proved before parsing either runtime input.
    return (_json_object(measurement_bytes, filename="measurement.json"),
            _json_object(runtime_bytes, filename="runtime.json"))


def verify_snapshot_policy(job_dir: Path, *records: Mapping, measurement: Mapping,
                           plan=None, required: bool = False) -> dict | None:
    """Verify separate host publication intent without changing engine scope.

    All supplied durable records must carry both keys once any carries either.
    Fully historical records remain recoverable; new report preparation must
    request ``required=True`` because their publication intent is unknowable.
    """
    bindings = []
    present = False
    for record in records:
        if not isinstance(record, Mapping):
            raise SnapshotReportPreparationError("Snapshot policy record is not an object")
        prefixed = any(key in record for key in ("runner_host_snapshot_policy_version", "runner_snapshot_config_sha256"))
        prefix = "runner_" if prefixed else ""
        present = present or any(key in record for key in (
            "runner_host_snapshot_policy_version", "runner_snapshot_config_sha256",
            "host_snapshot_policy_version", "snapshot_config_sha256"))
        version = record.get(prefix + "host_snapshot_policy_version")
        digest = record.get(prefix + "snapshot_config_sha256")
        # Never permit ambiguous duplicate spellings in one record.
        if prefixed and any(key in record for key in ("host_snapshot_policy_version", "snapshot_config_sha256")):
            raise SnapshotReportPreparationError("Snapshot policy binding has conflicting spellings")
        bindings.append((version, digest))
    if not present:
        if measurement.get("scope_kind") == "diagnostic" or "diagnostic_prompts" in measurement:
            raise SnapshotReportPreparationError("Diagnostic runs require a frozen host policy binding")
        if required:
            raise SnapshotReportPreparationError("Frozen host publication mode is unavailable for this historical job.")
        return None
    if not bindings or any(type(version) is not int or version != HOST_SNAPSHOT_POLICY_VERSION
                           or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                           for version, digest in bindings) or len(set(bindings)) != 1:
        raise SnapshotReportPreparationError("Snapshot policy binding is missing, changed or unsupported")
    encoded = _job_file_bytes(job_dir, "snapshot-config.json")
    if hashlib.sha256(encoded).hexdigest() != bindings[0][1]:
        raise SnapshotReportPreparationError("Snapshot configuration byte SHA-256 does not match its binding")
    config = _json_object(encoded, filename="snapshot-config.json")
    mode = config.get("runs")
    expected = 1 if mode == "single" else 3 if mode == "full" else None
    if (expected is None or type(config.get("test_mode")) is not bool
            or config["test_mode"] != (mode == "single")
            or any(type(config.get(name)) is not int or config[name] != expected
                   for name in ("repetitions", "planned_runs"))):
        raise SnapshotReportPreparationError("Frozen Snapshot mode, test mode and repetitions disagree")
    counts = [measurement[name] for name in ("repetitions", "planned_runs") if name in measurement]
    if not counts or any(type(count) is not int or count != expected for count in counts):
        raise SnapshotReportPreparationError("Frozen Snapshot publication mode disagrees with measurement repetitions")
    scope = None
    if plan is not None:
        scope = plan.get("scope_manifest") if isinstance(plan, Mapping) else getattr(plan, "scope_manifest", None)
        if not isinstance(scope, Mapping) or type(scope.get("planned_runs")) is not int or scope["planned_runs"] != expected:
            raise SnapshotReportPreparationError("Frozen Snapshot publication mode disagrees with the engine plan")
    diagnostic_records = [config, measurement, *([scope] if scope is not None else [])]
    diagnostic = any(record.get("scope_kind") == "diagnostic" or "diagnostic_prompts" in record
                     for record in diagnostic_records)
    if diagnostic:
        _verify_diagnostic_policy(diagnostic_records, mode=mode, expected=expected, plan_scope=scope)
        if required:
            raise SnapshotReportPreparationError("Diagnostic runs cannot produce clinic or area reports")
    return {"host_snapshot_policy_version": bindings[0][0], "snapshot_config_sha256": bindings[0][1],
            "runs": mode, "test_mode": config["test_mode"], "repetitions": expected,
            **({"scope_kind": "diagnostic", "publication_eligible": False} if diagnostic else {})}


def _verify_diagnostic_policy(records: list[Mapping], *, mode: str, expected: int,
                              plan_scope: Mapping | None) -> None:
    """Keep arbitrary prompt batches exact and outside clinic-report scope."""
    if mode != "single" or expected != 1:
        raise SnapshotReportPreparationError("Diagnostic runs require exactly one repetition")
    prompts = None
    platform = None
    for record in records:
        batch = record.get("diagnostic_prompts")
        platforms = record.get("platforms")
        if (record.get("scope_kind") != "diagnostic"
                or not isinstance(batch, (list, tuple)) or not batch
                or any(not isinstance(prompt, str) or not prompt.strip() for prompt in batch)
                or not isinstance(platforms, (list, tuple)) or len(platforms) != 1
                or not isinstance(platforms[0], str)
                or platforms[0] not in {"chatgpt", "gemini", "copilot", "github_copilot"}):
            raise SnapshotReportPreparationError("Diagnostic scope requires an exact prompt list and one supported platform")
        if prompts is None:
            prompts, platform = tuple(batch), platforms[0]
        elif tuple(batch) != prompts or platforms[0] != platform:
            raise SnapshotReportPreparationError("Frozen diagnostic prompts or platform differ from the engine inputs")
        counts = [record[key] for key in ("repetitions", "planned_runs") if key in record]
        if not counts or any(type(count) is not int or count != 1 for count in counts):
            raise SnapshotReportPreparationError("Diagnostic runs require exactly one repetition")
        source = record.get("github_copilot")
        if platform == "github_copilot" and isinstance(source, Mapping):
            if type(source.get("repetitions")) is not int or source["repetitions"] != 1:
                raise SnapshotReportPreparationError("Diagnostic Copilot runs require exactly one source repetition")
    if plan_scope is not None and (
            plan_scope.get("publication_eligible") is not False
            or type(plan_scope.get("diagnostic_prompt_count")) is not int
            or plan_scope["diagnostic_prompt_count"] != len(prompts)):
        raise SnapshotReportPreparationError("Diagnostic engine plan must preserve its prompt count and nonpublication policy")


def snapshot_policy_records(job_dir: Path, request: Mapping | None = None) -> tuple[Mapping, ...]:
    """Read host origin/projection and the exact execution request safely."""
    records = []
    for name in ("job.json", "job-origin.json"):
        path = job_dir / name
        records.append(_json_object(_job_file_bytes(job_dir, name), filename=name).get("run_config", {})
                       if path.exists() or path.is_symlink() else {})
    if request is not None:
        records.append(request.get("bindings", {}))
    return tuple(records)
