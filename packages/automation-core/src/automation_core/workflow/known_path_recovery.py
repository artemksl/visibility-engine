"""Component-owned return paths, executed by the existing browser owner.

A component supplies RecoveryRoute: a binding, repeatable navigation steps and
one destination reader. Call recover(app, route), or expose recovery_routes() to
run_with_recovery. Callbacks receive the failed action's original saved inputs.
No browser is attached here. Submitted effects use ordinary reconciliation first.
"""
from dataclasses import dataclass, replace
from contextlib import contextmanager
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from automation_core import contracts as c
from automation_core.workflow.blocks import ActionBlock, ActionPreconditionStop, PageBlock, RegisteredBlock, Step, observed_evidence
from automation_core.workflow.composition import compose_registered_workflow, merge_block_definitions
from automation_core.workflow.dispatch_policy import DispatchPurpose, ZeroSpendingDispatchPolicy
from automation_core.diagnostics.failure_capture import FailureCapture, default_failure_directory
from automation_core.browser.interaction import ActionExecutor
from automation_core.storage.memory import TransitionDenied
from automation_core.workflow.runtime import BlockInvocation, Navigator
from automation_core.workflow.timing import ActionTiming, BudgetSpec, utc_microseconds

BLOCK = 'automation.known_path_recovery'
CURRENT_VERSION = 3


@dataclass(frozen=True)
class RecoveryStep:
    """Only navigation/preparation; never Create, publish or other submissions.

    Callbacks take (original_inputs, binding, context, deadline). ready observes
    the effect; satisfied can override when navigation may be skipped (a stale
    screen may require refresh even though its URL already matches).
    """
    key: str
    request: Callable
    ready: Callable
    can_enter: Callable
    satisfied: Callable | None = None
    timing: ActionTiming = ActionTiming(.1, 3., 3., .1, .1)
    private_refs: Callable | None = None
    verify: Callable | None = None
    dialog_policy: c.DialogPolicy | None = None
    dialog_matcher: Callable | None = None
    observation_s: float | None = None


@dataclass(frozen=True)
class RecoveryRoute:
    route_id: str
    version: int
    failed_steps: tuple[str, ...]
    binding: Callable
    steps: tuple[RecoveryStep, ...]
    ready: Callable
    # Optional local qualification for observing an uncertain effect. This is
    # a journal-only predicate; navigation controls still qualify at dispatch.
    recoverable: Callable | None = None

    def __post_init__(self):
        keys = tuple(step.key for step in self.steps)
        if (not self.route_id or self.version < 1 or not self.failed_steps
                or len(set(keys)) != len(keys) or any(k in {'ready', 'completion'} for k in keys)):
            raise ValueError('A return path needs stable, distinct step names.')


def workflow_id(operation_id, namespace=BLOCK):
    # Preserve the identity of already recorded version-1 recoveries.
    return str(uuid5(NAMESPACE_URL, namespace + ':' + operation_id))


def candidate(memory, parent, *, include_pending=False):
    """Common admission only; route selection belongs to the component."""
    cp = memory.load_checkpoint(parent)
    if (cp.status not in {'running', 'suspended'} or type(cp.main) is not c.ActionResultAvailable
            or cp.retry_admission is not None):
        return None
    op = memory.operation(cp.main.operation_id)
    result = memory.load_result(cp.main)
    observing = include_pending and op.pending and op.effect_intents_used > 0
    undispatched = memory.undispatched_restore_available(cp)
    if cp.status == 'running' and not observing and not undispatched:
        return None
    if observing:
        # A pre-activation restoration can be interrupted after its durable
        # admission, while the original NOT_RUN receipt remains unchanged.
        admitted_restore = (op.aggregate_outcome is c.Outcome.UNKNOWN
            and result.outcome is c.Outcome.NOT_RUN and result.receipt is not None
            and not result.receipt.possibly_dispatched)
        if (op.current_phase != 'completed' or result.outcome is not c.Outcome.UNKNOWN and not admitted_restore
                or cp.recovery is not None and cp.recovery.root_operation_id != op.operation_id):
            return None
    elif not undispatched and (cp.recovery is not None or op.current_phase != 'completed' or op.pending or op.effect_intents_used
            or result.outcome is not c.Outcome.NOT_RUN
            or result.output is not None
            or result.stop_reason not in {c.StopReason.NEEDS_INPUT, c.StopReason.PAGE_NOT_READY,
                                          c.StopReason.PREREQUISITE_UNSATISFIED, c.StopReason.STALE_VIEW,
                                          c.StopReason.NETWORK_FAILED, c.StopReason.NAVIGATION_FAILED}
            or result.receipt is not None):
        return None
    if any(p.workflow_id != memory.recovery_workflow_id(op.operation_id)
            and not (observing and p.operation_id == op.operation_id)
            for p in memory.blocking_pending_for(cp.scope, parent)):
        return None
    return c.KnownPathRecoveryInputs(parent, cp.main)


def validate_source(memory, request, recovery_id, scope):
    """The saved failed operation, not UI wording, pins a recovery's source."""
    parent = memory.load_checkpoint(request.workflow_id)
    op = memory.operation(request.failed_result.operation_id)
    observation = (type(request) is c.ComponentRecoveryInputs
        and op is not None and op.pending
        and type(parent.main) is c.ActionResultAvailable
        and (parent.main.operation_id, parent.main.attempt_id) ==
            (request.failed_result.operation_id, request.failed_result.attempt_id))
    if (parent.scope != scope or parent.main != request.failed_result and not observation
            or op is None or op.workflow_id != parent.workflow_id
            or recovery_id != memory.recovery_workflow_id(op.operation_id)):
        raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
    return parent, op


def validate_inputs(memory, inputs, recovery_id, scope, definition):
    parent, op = validate_source(memory, inputs, recovery_id, scope)
    if definition.version in (1, 2):
        valid = memory.validate_legacy_recovery(inputs, recovery_id, scope, definition)
    else:
        record = c.definition_record(definition) if type(definition) is c.WorkflowDefinition else definition
        registered = getattr(memory, '_recovery_registration', None)
        expected = (recovery_id, inputs, record)
        admitted = registered == expected
        if not admitted and memory.workflow_exists(recovery_id):
            saved = memory.load_checkpoint(recovery_id)
            admitted = (memory.workflow_inputs(recovery_id) == inputs
                and memory._saved_definition(saved) == record)
        current = candidate(memory, parent.workflow_id, include_pending=True)
        valid = (definition.definition_id == memory.recovery_namespace and definition.version == 3
            and type(inputs) is c.ComponentRecoveryInputs and admitted and current is not None
            and current.failed_result.operation_id == inputs.failed_result.operation_id
            and current.failed_result.attempt_id == inputs.failed_result.attempt_id)
    if not valid:
        raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)


def observed_pending(memory, inputs, recovery_id, scope, definition):
    """Only the admitted route can observe its own unresolved source effect."""
    validate_inputs(memory, inputs, recovery_id, scope, definition)
    if definition.version < 3:
        return memory.observes_legacy_recovery(inputs)
    return memory.operation(inputs.failed_result.operation_id).pending


@contextmanager
def registered_route(memory, recovery_id, request, definition):
    previous = getattr(memory, '_recovery_registration', None)
    memory._recovery_registration = (recovery_id, request, c.definition_record(definition))
    try:
        yield
    finally:
        memory._recovery_registration = previous


def satisfied_without_dispatch(memory, cp, intent, result):
    return (cp.definition_id == memory.recovery_namespace and cp.definition_version in (2, 3)
            and type(intent.permitted_inputs) is c.ComponentRecoveryInputs
            and memory.workflow_inputs(cp.workflow_id) == intent.permitted_inputs
            and intent.effect_kind is c.EffectKind.REPEATABLE_PREPARATION
            and result.outcome is c.Outcome.CONFIRMED and result.stop_reason is None
            and result.output is None and result.receipt is None and bool(result.evidence_refs))


class _RecoveryBlock(RegisteredBlock):
    def __init__(self, route, request, original_inputs, *, namespace=BLOCK):
        self.route, self.request_ref, self.original_inputs = route, request, original_inputs
        self.actions, pages = {}, []
        for step in (*route.steps, None):
            key = step.key if step else 'ready'
            action = ActionBlock(key, step.timing if step else ActionTiming(.1, 0., 3., .1, .1),
                None if step is None else lambda i, b, ctx, step=step: self.request(step, b, ctx, ctx.budget.normal_until),
                lambda i, b, ctx, budget, step=step: self.observe(step, b, ctx, budget),
                None if step is None else lambda i, b, ctx, budget, step=step: self.precondition(step, b, ctx, budget),
                consequential=False, allow_satisfied_precondition=step is not None,
                dialog_policy=step.dialog_policy if step else None,
                dialog_matcher=step.dialog_matcher if step else None,
                observation_s=step.observation_s if step else None,
                intent_inputs=lambda i: request, semantic_target=namespace + '.' + key,
                private_refs=None if step is None or step.private_refs is None else
                    lambda i, step=step: step.private_refs(self.original_inputs),
                dispatch_assessment=None if step is None else lambda i, b, ctx, req, deadline, step=step:
                    self.assessment(step, b, ctx, req, deadline))
            self.actions[key] = action
            pages.append(PageBlock(namespace + '.' + key, (Step(action, step_id=namespace + '.' + key),)))
        super().__init__(namespace, pages, self, final=True)
        continuations, rules = [], []
        for index, step in enumerate(route.steps):
            name = namespace + '.' + step.key
            continuation = c.Continuation(name + '.observed', pages[index + 1].step_ids[0])
            rule = c.RecoveryRule(continuation.continuation_id, 10, c.RecoveryKind.RECONCILIATION,
                lambda snap, name=name: bool(snap.pending_operations) and all(p.operation_id == c.operation_id_for(
                    snap.checkpoint.scope, snap.checkpoint.workflow_id, name, '0') for p in snap.pending_operations),
                None, lambda proof: proof.original_resolved and proof.prerequisites_satisfied, continuation.continuation_id)
            continuations.append(continuation); rules.append(rule)
        self.definition = replace(self.definition, continuations=tuple(continuations), recovery_rules=tuple(rules))
        self.recovery_rules = tuple(rules)

    def binding(self, ctx):
        return self.route.binding(ctx, ctx.budget.normal_until)

    def ensure_entry(self, ctx):
        return c.NavigationResult(outcome=c.Outcome.CONFIRMED, binding=self.binding(ctx))

    def read(self, reader, binding, ctx, deadline):
        return reader(self.original_inputs, binding or self.binding(ctx), ctx, deadline)

    def request(self, step, binding, ctx, deadline):
        return self.read(step.request, binding, ctx, deadline)

    def precondition(self, step, binding, ctx, budget):
        if (step.key != 'refresh' and self.read(self.route.ready, binding, ctx, budget.until)
                or self.read(step.satisfied or step.ready, binding, ctx, budget.until)):
            return observed_evidence(ctx, outcome=c.Outcome.CONFIRMED, state='known_path_' + step.key)
        if self.read(step.can_enter, binding, ctx, budget.until):
            return True
        raise ActionPreconditionStop(observed_evidence(ctx, outcome=c.Outcome.NOT_RUN,
            state='known_path_entry_unavailable', stop=c.StopReason.NEEDS_INPUT))

    def assessment(self, step, binding, ctx, request, deadline):
        purpose = DispatchPurpose.ACCOUNT_ACCESS if step.private_refs else DispatchPurpose.ACCOUNT_NAVIGATION
        return ctx.executor.dispatch_policy.assessment(self, purpose,
            request, binding, ctx, deadline, verify=lambda until:
                step.verify(self.original_inputs, binding, ctx, request, until) if step.verify else
                request == self.request(step, binding, ctx, until)
                    and self.read(step.can_enter, binding, ctx, until))

    def observe(self, step, binding, ctx, budget):
        reader = step.ready if step else self.route.ready
        if self.read(reader, binding, ctx, budget.until):
            return observed_evidence(ctx, outcome=c.Outcome.CONFIRMED,
                state='known_path_' + (step.key if step else 'ready'))
        if step is None:
            original = ctx.memory.operation(self.request_ref.failed_result.operation_id)
            if original.pending:
                # Navigation finished. The parent's result reader owns the
                # recovery window; this helper must not poll that result too.
                return observed_evidence(ctx, outcome=c.Outcome.CONFIRMED, state='known_path_returned')
            # RegisteredBlock already resumes incomplete reads with the same ticket.
            return c.Evidence(c.Outcome.NOT_RUN, stop_reason=c.StopReason.NEEDS_INPUT)
        return observed_evidence(ctx, outcome=c.Outcome.UNKNOWN, state='known_path_unavailable',
            stop=c.StopReason.NEEDS_INPUT)

    def reconcile(self, pending, inputs, ctx):
        return tuple(self.actions[ctx.memory.operation(p.operation_id).intent.step_id.rsplit('.', 1)[-1]].reconcile(
            inputs, self.binding(ctx), p, ctx) for p in pending)

    def verify_resume(self, continuation_id, inputs, ctx):
        op = ctx.memory.operation(ctx.checkpoint.current.recovery.root_operation_id)
        proofs = tuple(p for p in ctx.memory.evidence_for(ctx.workflow_operation_id)
            if p.operation_id == op.operation_id and op.current_attempt_id in p.attempt_ids
            and type(p.details) is c.ObservationDetails)[-1:]
        pending = ctx.memory.blocking_pending_for(ctx.checkpoint.current.scope, ctx.workflow_operation_id)
        ready = (not pending and op.aggregate_outcome is c.Outcome.CONFIRMED and bool(proofs)
            and proofs[0].details.state == 'known_path_' + op.intent.step_id.rsplit('.', 1)[-1])
        return c.RecoveryEvidence(ctx.checkpoint.current.scope, proofs, True, not pending, ready)


def _cap_to_parent(app, budget, limits, clock):
    now, utc = clock.monotonic(), utc_microseconds(clock.utc_now())
    normal = min(limits.normal_expires_utc_us, utc + int((budget.normal_until-now)*1e6))
    hard = min(limits.completion_expires_utc_us, utc + int((budget.completion_until-now)*1e6))
    for saved in tuple(app.context.checkpoint.current.time_scopes):
        limits = saved.limits
        app.context.scopes._write(replace(saved, limits=replace(limits,
            normal_expires_utc_us=min(limits.normal_expires_utc_us, normal),
            completion_expires_utc_us=min(limits.completion_expires_utc_us, hard),
            normal_remaining_cap_s=min(limits.normal_remaining_cap_s, budget.remaining_normal(now)),
            completion_remaining_cap_s=min(limits.completion_remaining_cap_s, budget.remaining_completion(now)))))
    return replace(app, context=app.context.with_budget(app.context.scopes.budget_for(app.context.budget.scope_key.token())))


def recover(parent, route):
    """Use the original action state to choose retry versus result observation."""
    ctx, memory = parent.context, parent.context.memory
    failed = candidate(memory, ctx.workflow_operation_id, include_pending=True)
    if failed is None:
        return False
    op = memory.operation(failed.failed_result.operation_id)
    if op.intent.step_id not in route.failed_steps:
        return False
    if op.pending and route.recoverable is None:
        return False
    if route.recoverable is not None and route.recoverable(memory, ctx.checkpoint.current, op) is not True:
        return False
    recovery_id = memory.recovery_workflow_id(op.operation_id)
    request = (memory.workflow_inputs(recovery_id) if memory.workflow_exists(recovery_id) else
        c.ComponentRecoveryInputs(failed.workflow_id, failed.failed_result, route.route_id, route.version))
    if (request.route_id, request.route_version) != (route.route_id, route.version):
        raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
    return run_route(parent, route, request)


def run_route(parent, route, request):
    ctx, memory = parent.context, parent.context.memory
    cp, op = validate_source(memory, request, memory.recovery_workflow_id(request.failed_result.operation_id),
        ctx.checkpoint.current.scope)
    if op.intent.step_id not in route.failed_steps:
        raise TransitionDenied(c.StopReason.INTENT_MISMATCH)
    if op.pending and (route.recoverable is None or route.recoverable(memory, cp, op) is not True):
        return False
    recovery_id = memory.recovery_workflow_id(op.operation_id)
    version = memory.load_checkpoint(recovery_id).definition_version if memory.workflow_exists(recovery_id) else memory.recovery_version
    block = _RecoveryBlock(route, request, op.intent.permitted_inputs, namespace=memory.recovery_namespace)
    definition = merge_block_definitions(memory.recovery_namespace, version, (block,))
    with registered_route(memory, recovery_id, request, definition):
        return _run_route(parent, route, request, block, definition,
            retry=not op.pending and not memory.undispatched_restore_available(cp))


def _run_route(parent, route, request, block, definition, *, retry):
    ctx, memory, driver = parent.context, parent.context.memory, parent.driver
    op = memory.operation(request.failed_result.operation_id)
    recovery_id = memory.recovery_workflow_id(op.operation_id)
    saved = memory.load_checkpoint(recovery_id) if memory.workflow_exists(recovery_id) else None
    if saved is not None and memory.workflow_inputs(recovery_id) != request:
        raise TransitionDenied(c.StopReason.WORKFLOW_INCOMPATIBLE)
    # The parent action remains the time authority, including for Finished children.
    parent_budget = ctx.scopes.restore_action(op.limits, ctx.scopes.budget_for(op.timing_scope_key))
    owner_ctx = ctx.with_budget(parent_budget)
    owner_ctx.normal()
    capture = FailureCapture(memory.recovery_failure_directory(), profile_uuid=memory.scope.profile_id, workflow_id=recovery_id)
    previous_capture = getattr(driver.session, 'failure_capture', None)
    app = None
    try:
        driver.session.failure_capture = capture
        if saved is None or saved.status != 'finished':
            scopes = () if saved is None else saved.time_scopes
            recorded = next((s for s in scopes if s.scope_key.component_path == ('run', 'workflow')), None)
            root = next((s for s in scopes if s.scope_key.component_path == ('run',)), None)
            now = ctx.clock.monotonic()
            allocation = (BudgetSpec(recorded.allowance_s, recorded.normal_fraction) if recorded else
                BudgetSpec(root.allowance_s - 2. if root else parent_budget.remaining_completion(now), .999999))
            app = compose_registered_workflow(session=driver.session, ownership=driver.ownership, memory=memory,
                private_inputs=lambda ref: c.ResolvedPrivateInput(ref, ctx.private_inputs(ref)), definition=definition,
                invocations=(BlockInvocation(block, lambda *args: c.EmptyPayload()),),
                workflow_operation_id=recovery_id, allocation=allocation, clock=ctx.clock,
                cancellation=driver.cancellation, driver=driver,
                executor=ActionExecutor(driver, dispatch_policy=ZeroSpendingDispatchPolicy(driver, (block,))),
                navigator=Navigator(), workflow_inputs=request, restart_clock_policy=ctx.scopes.policy,
                create_workflow=saved is None)
            app = _cap_to_parent(app, parent_budget, op.limits, ctx.clock)
            # This route is one attempt of its parent's recovery loop.
            app.runner.automatic_recovery = False
            result = app.run()
            if result.outcome is not c.Outcome.CONFIRMED or result.stop_reason is not None:
                return False
        else:
            memory.preflight_workflow(definition, recovery_id, workflow_inputs=request)
        if not op.pending and not block.read(route.ready, None, owner_ctx, parent_budget.normal_until):
            return False
        if retry:
            ctx.acknowledge(memory.retry_known_path(request, recovery_id,
                expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
        return True
    except Exception as error:
        (capture if app is not None else previous_capture or capture).capture_context(
            app.context if app is not None else ctx, error=error)
        raise
    finally:
        if app is not None and app.context.checkpoint.current.status != 'finished':
            capture.capture_context(app.context)
        driver.session.failure_capture = previous_capture


def _recover_from_sources(app, sources):
    request = candidate(app.context.memory, app.context.workflow_operation_id, include_pending=True)
    if request is None:
        return None
    memory = app.context.memory
    recovery_id = memory.recovery_workflow_id(request.failed_result.operation_id)
    if memory.workflow_exists(recovery_id) and memory.load_checkpoint(recovery_id).definition_version == 1:
        return memory.resume_legacy_recovery(app, sources)
    step = memory.operation(request.failed_result.operation_id).intent.step_id
    routes = [route for source in sources for route in
              (source.recovery_routes() if callable(getattr(source, 'recovery_routes', None)) else ())
              if step in route.failed_steps]
    if len(routes) > 1:
        raise ValueError('The failed action must have one owning return path.')
    saved_refresh = (memory.workflow_exists(recovery_id) and any(
        step.step_id.endswith('.refresh') for step in memory._saved_definition(memory.load_checkpoint(recovery_id)).steps))
    if routes and (not memory.workflow_exists(recovery_id) or saved_refresh):
        route = routes[0]
        if not any(step.key == 'refresh' for step in route.steps):
            # Pending effects require the component's existing qualification.
            op = memory.operation(request.failed_result.operation_id)
            if not op.pending:
                routes[0] = with_refresh(route, app)
    if not routes:
        readers = [(source, reader) for source in sources
            if callable(getattr(source, 'loading_reader', None))
            for reader in (source.loading_reader(step),) if reader is not None]
        if len(readers) > 1:
            raise ValueError('The failed action must have one readiness reader.')
        route = memory.page_refresh_route(app, request, *(readers[0] if readers else ()))
        if route is None:
            route = refresh_route(app, request)
        if route is not None:
            routes.append(route)
    return recover(app, routes[0]) if routes else None


def with_refresh(route, app):
    """Refresh the same owned view before the component's known return path."""
    driver = app.driver
    def current_url(inputs, binding, ctx, until):
        return driver.url(binding.view, deadline=until)
    def loaded(inputs, binding, ctx, until):
        return driver.observe_page(binding, lambda page, budget: page.evaluate(
            "document.readyState === 'complete' && !!document.body && !!document.body.innerText.trim()"), deadline=until)
    step = RecoveryStep('refresh', lambda i, b, ctx, until:
        c.ActionRequest('navigate', url=current_url(i, b, ctx, until)), loaded,
        lambda *args: True, satisfied=lambda *args: False,
        verify=lambda i, b, ctx, request, until: request.kind == 'navigate'
            and request.url == current_url(i, b, ctx, until),
        dialog_policy=route.steps[0].dialog_policy if route.steps else None,
        dialog_matcher=route.steps[0].dialog_matcher if route.steps else None)
    return replace(route, steps=(step, *route.steps))


def refresh_route(app, request):
    op = app.context.memory.operation(request.failed_result.operation_id)
    if op.pending:
        return None  # Only a registered component may qualify pending navigation.
    page = getattr(app.driver, 'last_observed_page', None)
    if page is None:
        return None
    view = app.driver.bind_page(page)
    url = app.driver.url(view, deadline=app.context.budget.normal_until)
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return None
    binding = c.PageBinding(view, 'automation.refresh', parsed.scheme + '://' + parsed.netloc)
    route = RecoveryRoute('action_refresh', 1, (op.intent.step_id,), lambda *args: binding,
        (), lambda *args: True)
    return with_refresh(route, app)





def run_with_recovery(app, sources):
    """Compatibility entry: the shared runner owns all recovery decisions."""
    return app.runner.run_with_recovery(app, sources)
