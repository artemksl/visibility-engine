"""Frozen host bindings shared by the launcher and measurement worker.

The engine remains responsible for interpreting plans and authorizing sends.
These bindings establish which immutable inputs the host asked it to execute.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def measurement_send_limit(plan: object) -> int:
    budget = getattr(plan, "submission_budget")
    ceiling = getattr(budget, "retry_authorized_ceiling", None)
    value = getattr(ceiling, "total_external_submissions", budget.total_external_submissions)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("measurement send limit must be a non-negative integer")
    return value


@dataclass(frozen=True)
class FrozenExecutionInputs:
    measurement_sha256: str
    runtime_sha256: str
    plan_sha256: str
    max_external_submissions: int | None

    @classmethod
    def from_job_config(cls, config: Mapping) -> "FrozenExecutionInputs":
        return cls(config["runner_measurement_sha256"], config["runner_runtime_sha256"],
                   config["runner_plan_sha256"], config.get("runner_authorized_max_external_submissions"))

    def request_bindings(self) -> dict:
        return {"measurement_sha256": self.measurement_sha256,
                "runtime_sha256": self.runtime_sha256, "plan_sha256": self.plan_sha256}

    def verify_record(self, record: Mapping, *, prefix: str = "", label: str = "execution records") -> None:
        for key, value in self.request_bindings().items():
            if record.get(prefix + key) != value:
                raise ValueError(f"Frozen {prefix + key} differs between {label}")

    def verify_files(self, *, measurement_sha256: str, runtime_sha256: str) -> None:
        if measurement_sha256 != self.measurement_sha256:
            raise ValueError("measurement file hash differs from --expected-measurement-file-sha256")
        if runtime_sha256 != self.runtime_sha256:
            raise ValueError("runtime file hash differs from --expected-runtime-file-sha256")

    def verify_plan(self, plan: object) -> None:
        if plan.plan_sha256 != self.plan_sha256:
            raise ValueError("planned hash differs from --expected-plan-sha256")
        if measurement_send_limit(plan) != self.max_external_submissions:
            raise ValueError("planned submission ceiling differs from --expected-max-external-submissions")

    def command_arguments(self) -> list[str]:
        return ["--expected-plan-sha256", self.plan_sha256,
                "--expected-measurement-file-sha256", self.measurement_sha256,
                "--expected-runtime-file-sha256", self.runtime_sha256,
                "--expected-max-external-submissions", str(self.max_external_submissions)]


@dataclass(frozen=True)
class FrozenExecutionRequest:
    """Identity from a registry-validated, immutable execution request."""
    run_id: str
    mode: str
    execution_id: str
    request_id: str
    request_sha256: str
    recovery: Mapping | None

    @classmethod
    def from_record(cls, record: Mapping) -> "FrozenExecutionRequest":
        return cls(record["run_id"], record["mode"], record["execution_id"],
                   record["request_id"], record["request_sha256"], record.get("recovery"))
