"""Compose controlled authentication around the caller's owned session and store."""
from dataclasses import dataclass, replace
from pathlib import Path
import threading

from automation_core.contracts import Finished, Outcome, ResolvedPrivateInput, SensitiveValue, StopReason, ActionResultAvailable, AuthenticationCompletionOutput, BlockCompletionOutput
from automation_core.browser.interaction import ActionExecutor, BrowserDriver
from automation_core.storage.memory import TransitionDenied
from automation_core.workflow.runtime import (CheckpointRef, Navigator, RunContext, SequentialRunner,
                      BlockInvocation, InvocationRegistry, registered_time_plan)
from automation_core.workflow.timing import (BudgetAdmissionDenied, BudgetSpec, DeadlineBudget, RestartClockPolicy, ScopeKey, TimePlan,
                     ScopeRegistry, ScopeStart, SystemClock, empty_time_checkpoint)


def merge_block_definitions(definition_id, version, blocks, *, metadata=None):
    """Merge declared owners and completion successors into one definition.

    A custom conditional LogicBlock supplies explicit metadata; configured
    RegisteredBlocks already expose it. No workflow graph is inferred at runtime.
    """
    from automation_core.contracts import WorkflowDefinition, definition_record
    from automation_core.storage.persistence import validate
    blocks = tuple(blocks)
    metas = tuple(metadata) if metadata is not None else tuple(block.metadata for block in blocks)
    if len(blocks) != len(metas) or any(block.block_id != meta.block_id for block, meta in zip(blocks, metas)):
        raise ValueError("Block implementation and metadata ownership must agree.")
    owners = {meta.block_id: meta for meta in metas}
    steps, continuations, recoveries = [], [], []
    for block, meta in zip(blocks, metas):
        for step in block.definition.steps:
            if step.step_id == meta.completion_step_id:
                successors = tuple(owners[name].entry_step_id for name in meta.allowed_successor_block_ids)
                step = replace(step, normal_next=successors)
            steps.append(step)
        continuations.extend(block.definition.continuations)
        recoveries.extend(block.definition.recovery_rules)
    definition = WorkflowDefinition(definition_id, version, tuple(steps),
        tuple(continuations), tuple(recoveries), metas)
    validate(definition_record(definition))
    return definition


def preflight_workflow(memory, definition, workflow_operation_id, *, create=False, workflow_inputs=None):
    """Read-only durable compatibility and pending/history gate before sessions.

    Store provisioning and initial input persistence remain explicit caller steps.
    Reapply the same gate after acquiring the T01 runtime handoff's lease.
    """
    return memory.preflight_workflow(definition, workflow_operation_id,
        create=create, workflow_inputs=workflow_inputs)


@dataclass(frozen=True)
class SharedWorkflowApplication:
    context: RunContext
    invocations: InvocationRegistry
    runner: SequentialRunner
    driver: object

    def run(self):
        blocks = tuple(self.invocations.block(meta.block_id) for meta in self.context.definition.blocks)
        from automation_core.diagnostics.failure_capture import capture_failure
        try:
            result = self.runner.run(blocks, None, self.context)
        except Exception as error:
            capture_failure(self.context, error=error)
            raise
        if result.outcome.value != 'confirmed' or result.stop_reason is not None:
            capture_failure(self.context, result)
        else:
            capture_failure(self.context, result, kind='boundary')
        return result


_INVOCATION_VALIDATORS = {}


def register_invocation_validator(definition_id, validator):
    """A site supplies recipe checks while the core owns the invocation graph."""
    _INVOCATION_VALIDATORS[definition_id] = validator


def build_invocation_registry(definition, invocations, *, request=None):
    invocations = tuple(invocations)
    validator = _INVOCATION_VALIDATORS.get(definition.definition_id)
    if validator is not None:
        validator(definition, invocations, request)
    elif request is not None:
        raise ValueError("Explicit stage requests belong only to their registered family.")
    return InvocationRegistry(definition, invocations)


def admit_workflow_timing(memory, definition, workflow_operation_id, *, invocations,
        allocation, clock=None, restart_clock_policy=RestartClockPolicy(),
        workflow_inputs=None, create=False, pre_execution_plan=None, admission_margin_s=0., original_start=None):
    """Open the single durable original timing bank before any session effects.

    This hook borrows the caller's preflight lease. Reopening it under T01's
    runtime lease restores the same limits, rather than allocating a new window.
    It observes no browser/provider/private values and provisions no missing store.
    """
    if not isinstance(allocation, BudgetSpec):
        raise ValueError("A finite shared workflow allocation is required.")
    memory.ownership.assert_owned()
    stage_request = (workflow_inputs if workflow_inputs is not None else memory.workflow_inputs(workflow_operation_id)) if definition.definition_id in _INVOCATION_VALIDATORS else None
    registry = build_invocation_registry(definition, invocations, request=stage_request)
    return _admit_registered_timing(memory, registry, workflow_operation_id,
        allocation=allocation, clock=clock, restart_clock_policy=restart_clock_policy,
        workflow_inputs=workflow_inputs, create=create, pre_execution_plan=pre_execution_plan,
        admission_margin_s=admission_margin_s, original_start=original_start)


def _admit_registered_timing(memory, registry, workflow_operation_id, *, allocation,
        clock, restart_clock_policy, workflow_inputs, create, pre_execution_plan,
        admission_margin_s, original_start):
    """Admit the already compiled graph once, under its original saved limits."""
    definition = registry.definition
    plan = registered_time_plan(registry)
    if pre_execution_plan is not None:
        if type(pre_execution_plan) is not TimePlan:
            raise TypeError("Pre-execution work requires a declared time plan.")
        plan = TimePlan("sequence", children=(pre_execution_plan, plan), overhead_s=admission_margin_s)
    elif admission_margin_s:
        raise ValueError("An admission margin requires a pre-execution plan.")
    TimePlan("wrapped", children=(plan,), allocation=allocation)
    clock = SystemClock() if clock is None else clock
    preflight_workflow(memory, definition, workflow_operation_id,
        create=create, workflow_inputs=workflow_inputs)
    checkpoint = CheckpointRef(memory.open_or_resume_workflow(definition,
        workflow_operation_id, memory.scope, create=create,
        at=empty_time_checkpoint(clock, restart_clock_policy), workflow_inputs=workflow_inputs,
        recovery_policy_version=2))
    scopes = ScopeRegistry(clock, memory, checkpoint, policy=restart_clock_policy)
    root_key = ScopeKey(memory.account_key, workflow_operation_id, ("run",))
    cp = checkpoint.current
    saved_completion = False
    if isinstance(cp.main, ActionResultAvailable) and cp.recovery is None and cp.status == "running":
        record = memory.operation(cp.main.operation_id)
        metadata = next((meta for meta in definition.blocks
                         if record.intent.step_id == meta.completion_step_id), None)
        result = memory.load_result(cp.main)
        saved_completion = (metadata is not None and result.outcome == Outcome.CONFIRMED
            and result.stop_reason is None and type(result.output) in
            (AuthenticationCompletionOutput, BlockCompletionOutput)
            and memory.result_consumption_allowed(cp.main))
    if isinstance(cp.main, Finished):
        # Durable consumption needs no new browser handles or clock trust. This
        # zero-input context can consume completion and then holds any successor.
        now = clock.monotonic()
        budget = DeadlineBudget(root_key, clock.epoch_id, now, now, now, completion_only=True)
    else:
        # An explicit command-entry start charges preflight to the same original
        # bank. Ordinary callers retain admission-entry timing. Only elapsed
        # pre-execution reservations are credited, never registered future work.
        try:
            workflow_key = ScopeKey(memory.account_key, workflow_operation_id, ("run", "workflow"))
            root_saved, workflow_saved = scopes._read(root_key), scopes._read(workflow_key)
            first_start = original_start if root_saved is None else None
            if pre_execution_plan is not None and first_start is None and root_saved is None and create:
                first_start = ScopeStart("entry", clock.epoch_id, clock.monotonic())
            if pre_execution_plan is not None and workflow_saved is None:
                # A crash before original timing was durably captured cannot
                # authorize a fresh bank on a later invocation.
                if root_saved is not None or first_start is None:
                    raise BudgetAdmissionDenied("scope_conflict")
            root = scopes.root_budget(root_key, allocation.allowance_s + 2., start=first_start)
            spent = (min(pre_execution_plan.cost_s(), max(0., clock.monotonic() - first_start.monotonic_at))
                     if pre_execution_plan is not None and first_start is not None else 0.)
            budget = scopes.open_or_reuse(workflow_key, allocation, root, plan=plan,
                start=first_start, elapsed_reservation_s=spent)
            scopes.require_normal(budget, until=budget.normal_until)
        except BudgetAdmissionDenied:
            if not saved_completion:
                raise
            now = clock.monotonic()
            budget = DeadlineBudget(root_key, clock.epoch_id, now, now, now, completion_only=True)
    return checkpoint, scopes, budget


def compose_registered_workflow(*, session, ownership, memory, private_inputs,
        definition, invocations, workflow_operation_id, allocation,
        clock=None, cancellation=None, create_workflow=False,
        restart_clock_policy=RestartClockPolicy(), driver=None, executor=None,
        navigator=None, provider_commands=None, private_assets=None,
        workflow_inputs=None, pre_execution_plan=None, admission_margin_s=0., original_start=None):
    """Borrow one caller-owned session, exact private resolver and durable store.

    This function launches no Octo session and loads no account export. The caller
    must run preflight_workflow before its session adapter. Every invocation's
    binder receives exact typed committed outputs after owner selection.
    """
    from automation_core.contracts import definition_record
    from automation_core.storage.persistence import validate
    ownership.assert_owned()
    validate(definition_record(definition))
    if not isinstance(allocation, BudgetSpec):
        raise ValueError("A finite shared workflow allocation is required.")
    if memory.ownership is not ownership:
        raise ValueError("Application and memory must borrow the same ownership.")
    if not Path(memory.path).resolve().is_relative_to(Path(ownership.root).resolve()):
        raise ValueError("Memory must remain inside the owned state root.")
    stage_request = (workflow_inputs if workflow_inputs is not None else memory.workflow_inputs(workflow_operation_id)) if definition.definition_id in _INVOCATION_VALIDATORS else None
    registry = build_invocation_registry(definition, invocations, request=stage_request)
    clock = SystemClock() if clock is None else clock
    cancellation = threading.Event() if cancellation is None else cancellation
    checkpoint, scopes, budget = _admit_registered_timing(memory, registry,
        workflow_operation_id, allocation=allocation,
        clock=clock, restart_clock_policy=restart_clock_policy,
        workflow_inputs=workflow_inputs, create=create_workflow,
        pre_execution_plan=pre_execution_plan, admission_margin_s=admission_margin_s,
        original_start=original_start)
    if budget.completion_only:
        driver = None
        executor = None
    else:
        driver = driver or (getattr(executor, "driver", None) if executor is not None else
                            BrowserDriver(session, clock, cancellation, ownership))
        executor = executor or ActionExecutor(driver)
    def resolve(reference):
        value = private_inputs(reference)
        if (type(value) is not ResolvedPrivateInput or value.reference != reference
                or not isinstance(value.value, SensitiveValue)):
            sensitive = getattr(value, "value", value)
            if isinstance(sensitive, SensitiveValue):
                sensitive.clear()
            raise TransitionDenied(StopReason.INPUT_UNAVAILABLE)
        return value.value
    def resolve_asset(reference):
        from automation_core.assets import ResolvedPrivateAsset
        ownership.assert_owned()
        asset = private_assets(reference)
        try:
            if type(asset) is not ResolvedPrivateAsset or asset.reference != reference:
                raise ValueError()
            asset.validate()
        except Exception:
            if hasattr(asset, "clear"):
                asset.clear()
            raise TransitionDenied(StopReason.INPUT_UNAVAILABLE) from None
        return asset
    context = RunContext(run_id=memory.run_id, account_key=memory.account_key,
        workflow_operation_id=workflow_operation_id, session=session, executor=executor,
        memory=memory, navigator=navigator or Navigator(), clock=clock, budget=budget,
        scopes=scopes, cancellation=cancellation, ownership=ownership,
        private_inputs=resolve, checkpoint=checkpoint, definition=definition,
        observations=driver, private_assets=resolve_asset if private_assets is not None else None,
        provider_commands=provider_commands, invocations=registry)
    return SharedWorkflowApplication(context, registry, SequentialRunner(), driver)
