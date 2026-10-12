"""Application-side integration with the public :mod:`visibility_runner` API.

The runner owns browser interaction, provider conversations, extraction,
grounding, durable send fences, and recovery safety. Google AI Overview is
deferred; its historical evidence remains readable. This module only validates
host mappings through the runner's public
configuration types, delegates lifecycle calls, and exposes immutable results.

In particular, there is no host-side classification phase.  ChatGPT, Gemini and browser
Copilot name extraction is turn 2 of the same conversation as measurement, and
the runner returns the already-grounded names in
``ObservationResult.ordered_names``.
GitHub Copilot uses an explicit SDK account and saved-source Gemini parsing;
the runner keeps both stages inside one ``github_copilot`` observation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar

try:
    from visibility_runner import (
        ArtifactReferences,
        Issue,
        MeasurementConfig,
        ObservationResult,
        PacketResult,
        RunPlan,
        RunResult,
        RunStatus,
        Runner,
        RuntimeConfig,
        SubmissionBudget,
        TransportCounts,
    )
except ImportError as exc:
    raise ImportError(
        "visibility_panel.integrations.visibility_runner requires a compatible "
        "installed visibility_runner package"
    ) from exc


MeasurementInput = MeasurementConfig | Mapping[str, object]
RuntimeInput = RuntimeConfig | Mapping[str, object]
ConsumerReturnT = TypeVar("ConsumerReturnT", covariant=True)


@dataclass(frozen=True)
class ApplicationResult:
    """Read-only host view over one runner-owned result.

    The underlying result and its observation objects are returned directly.
    Consequently, packet order, observation order, surface forms, duplicate
    names, evidence references, issues, budget, and actual transport counts
    remain exactly as supplied by ``visibility_runner``.
    """

    module_result: RunResult

    def __post_init__(self) -> None:
        if not isinstance(self.module_result, RunResult):
            raise TypeError("module_result must be a RunResult")

    @property
    def run_id(self) -> str:
        return self.module_result.run_id

    @property
    def status(self) -> RunStatus:
        return self.module_result.status

    @property
    def packets(self) -> tuple[PacketResult, ...]:
        return self.module_result.packets

    @property
    def observations(self) -> tuple[ObservationResult, ...]:
        """Return observations in their original packet and result order."""

        return self.module_result.observations

    @property
    def issues(self) -> tuple[Issue, ...]:
        return self.module_result.issues

    @property
    def artifacts(self) -> ArtifactReferences:
        return self.module_result.artifacts

    @property
    def submission_budget(self) -> SubmissionBudget:
        """Return the authorization ceiling without treating it as usage."""

        return self.module_result.submission_budget

    @property
    def transport_counts(self) -> TransportCounts:
        """Return the runner-recorded actual transport counts unchanged."""

        return self.module_result.transport_counts

    @property
    def policy_version(self) -> str:
        return self.module_result.policy_version

    @property
    def succeeded(self) -> bool:
        """True only when every required runner operation succeeded."""

        return self.module_result.status is RunStatus.SUCCEEDED


class ResultConsumer(Protocol[ConsumerReturnT]):
    """A host callback that consumes one immutable application result."""

    def __call__(
        self,
        result: ApplicationResult,
        /,
    ) -> ConsumerReturnT: ...


def coerce_measurement_config(value: MeasurementInput) -> MeasurementConfig:
    """Use the runner's validation rather than recreating planning rules."""

    if isinstance(value, MeasurementConfig):
        return value
    return MeasurementConfig.from_mapping(value)


def coerce_runtime_config(value: RuntimeInput) -> RuntimeConfig:
    """Use the exact host-supplied runner infrastructure configuration."""

    if isinstance(value, RuntimeConfig):
        return value
    return RuntimeConfig.from_mapping(value)


def load_measurement_plan(
    value: Mapping[str, object],
    *,
    measurement: MeasurementInput,
    runtime: RuntimeInput,
    run_id: str,
) -> RunPlan:
    """Load the exact approved plan and bind it to the host's frozen inputs.

    Loading validates the artifact without rendering prompts again. The
    runner still owns execution admission and manifest verification.
    """

    from visibility_runner import load_run_plan

    plan = load_run_plan(value)
    if plan.run_id != run_id:
        raise ValueError("frozen plan run ID differs from the requested run")
    if plan.measurement_config != coerce_measurement_config(measurement):
        raise ValueError("frozen plan measurement differs from the input")
    infrastructure = coerce_runtime_config(runtime)
    for name in ("selected_profile_ids", "selected_profiles", "profile_manifest_sha256", "retry_policy"):
        if getattr(plan, name) != getattr(infrastructure, name):
            raise ValueError(f"frozen plan {name} differs from the runtime input")
    return plan


class VisibilityMeasurementComponent:
    """Thin synchronous lifecycle boundary for one host runtime.

    The web worker loads its approved preflight plan, then calls ``run``.
    ``run_new`` remains a convenience for callers planning and executing in
    one process. The component does
    not expose ``Runner.create`` or ``Runner.run_packet`` and never turns a
    partial or attention result into replacement work.
    """

    def __init__(self, runtime: RuntimeInput, *, event_sink=None):
        options = {"event_sink": event_sink} if event_sink is not None else {}
        self._runner = Runner(coerce_runtime_config(runtime), **options)

    def execution(self, run_id: str, *, execution_id: str | None = None):
        """Hold engine ownership through committed host output and cleanup."""
        return self._runner.execution(run_id, execution_id=execution_id)

    def plan_refetch(self, run_id: str, *, request_id: str) -> Mapping:
        """Pure engine preview, including its frozen revision and allowances."""
        return self._runner.plan_refetch(run_id, request_id=request_id)

    def refetch(self, request: Mapping, *, replace_checked_sends: bool = True) -> RunResult:
        """Execute a finite explicit pass; worker callers hold execution()."""
        if replace_checked_sends:
            return self._runner.refetch(request)
        return self._runner.refetch(request, replace_checked_sends=False)

    def failure_readout(self, run_id: str, *, worker_observation=None) -> Mapping:
        """Render the engine's centralized pure failure projection."""
        return self._runner.failure_readout(run_id, worker_observation=worker_observation)

    def ownership(self, run_id: str) -> Mapping:
        """Sample public engine ownership without provider interaction."""
        return self._runner.ownership(run_id)

    def verification_control(self, run_id: str, **command) -> Mapping:
        """Delegate an explicit correlated show/check/close without a send."""
        return self._runner.verification_control(run_id, **command)

    def prepare_browser_restart(self, *, request_id: str, recovery_request=None) -> Mapping:
        """Dispose verified holds before the host closes Chrome; never send."""
        return self._runner.prepare_browser_restart(request_id=request_id, recovery_request=recovery_request)

    def browser_restart(self, *, request_id: str, recovery_request=None):
        """Keep selected profile maintenance leases through Chrome startup."""
        return self._runner.browser_restart(request_id=request_id, recovery_request=recovery_request)

    def defer_browser_startup(self, factory):
        """Let the owned engine admit source capabilities before Chrome opens."""
        return self._runner.defer_browser_startup(factory)

    def enable_automatic_verification(self, run_id: str, *, request_id: str, request_sha256: str, plan=None, account_emails=None) -> Mapping:
        """Bind verification and optional exact-account login to this execution."""
        return self._runner.enable_automatic_verification(run_id, request_id=request_id,
            request_sha256=request_sha256, plan=plan,
            **({"account_emails": account_emails} if account_emails is not None else {}))

    @property
    def recovery_available(self) -> bool:
        """Gate UI on the complete compatible public recovery boundary."""
        return all(callable(getattr(self._runner, method, None)) for method in ("execution", "plan_refetch", "refetch", "failure_readout", "ownership", "resolve_recovery_link"))

    def resolve_recovery_link(self, run_id: str, reference: str) -> Mapping:
        return self._runner.resolve_recovery_link(run_id, reference)

    def release_stale_ownership(self, run_id: str, *, reason: str) -> Mapping:
        """Audited operator release of an owner record that blocks the run.

        The engine refuses it while any process holds the run lock or the
        recorded process is verified alive, and archives the old record.
        """
        return self._runner.release_stale_ownership(run_id, reason=reason)

    def failure_readout_for_plan(self, plan: RunPlan, *, worker_observation=None) -> Mapping:
        return self._runner.failure_readout_for_plan(plan, worker_observation=worker_observation)

    def plan(
        self,
        measurement: MeasurementInput,
        *,
        run_id: str,
    ) -> RunPlan:
        """Build a pure plan for the caller's unique new run ID."""

        return self._runner.plan(
            coerce_measurement_config(measurement),
            run_id=run_id,
        )

    def run(self, plan: RunPlan) -> RunResult:
        """Synchronously execute one authorized new plan exactly once."""

        return self._runner.run(plan)

    def run_new(
        self,
        measurement: MeasurementInput,
        *,
        run_id: str,
    ) -> RunResult:
        """Plan and execute a new run; suitable for a synchronous job worker."""

        plan = self.plan(measurement, run_id=run_id)
        return self.run(plan)

    def inspect(self, run_id: str) -> RunResult:
        """Read current runner state without browser or provider interaction."""

        return self._runner.inspect(run_id)

    def read_artifact(self, run_id: str, reference: object) -> bytes:
        """Read one runner artifact through its hash-verifying public seam."""

        return self._runner.read_artifact(run_id, reference)

    def resume(self, run_id: str) -> RunResult:
        """Request runner-owned safe recovery only when explicitly called."""

        return self._runner.resume(run_id)

    def cancel(self, run_id: str, *, execution_id: str | None = None) -> RunResult:
        """Cancel only runner-owned work and tabs for the named run."""

        return self._runner.cancel(run_id, **({"execution_id": execution_id} if execution_id else {}))


def plan_measurement(
    measurement: MeasurementInput,
    *,
    runtime: RuntimeInput,
    run_id: str,
) -> RunPlan:
    """Build a pure plan without creating or executing a run."""

    component = VisibilityMeasurementComponent(runtime)
    return component.plan(measurement, run_id=run_id)


def run_measurement(
    measurement: MeasurementInput,
    *,
    runtime: RuntimeInput,
    run_id: str,
) -> RunResult:
    """Normal synchronous new-run entry point for an authorized worker."""

    component = VisibilityMeasurementComponent(runtime)
    return component.run_new(measurement, run_id=run_id)


def adapt_result(result: RunResult) -> ApplicationResult:
    """Wrap a result without transforming, persisting, or classifying it."""

    return ApplicationResult(result)


def consume_result(
    result: RunResult,
    consumer: ResultConsumer[ConsumerReturnT],
) -> ConsumerReturnT:
    """Pass one unchanged result view to a host-owned downstream consumer."""

    return consumer(adapt_result(result))


__all__ = [
    "ApplicationResult",
    "MeasurementInput",
    "ResultConsumer",
    "RuntimeInput",
    "VisibilityMeasurementComponent",
    "adapt_result",
    "coerce_measurement_config",
    "coerce_runtime_config",
    "load_measurement_plan",
    "consume_result",
    "plan_measurement",
    "run_measurement",
]
