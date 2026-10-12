"""One owner-thread execution chain, with acknowledged durable continuations."""
from dataclasses import dataclass, field, replace
import threading
from typing import Callable
from uuid import uuid4

from automation_core.contracts import (
    ActionExecutionScope, ActionResultAvailable, BlockResult, Finished, InAction,
    NavigationResult, Outcome, Ready, Recording, RecoveryEvidence, RecoveryKind, RecoverySnapshot, RouteOutput,
    EvidenceSubmission, RunResult, StageResult, StopReason,
    StoredActionResult, operation_id_for,
)
from automation_core.storage.memory import StorageFailure, TransitionDenied
from automation_core.workflow.timing import (BudgetAdmissionDenied, CallBudget, DeadlineBudget, ScopeKey,
                     TimePlan, TimeSensitive, WorkPermission)


def stop_reason(reason):
    """Map finite technical codes, never exception text, to the closed vocabulary."""
    if isinstance(reason, StopReason):
        return reason
    try:
        return StopReason(reason)
    except (ValueError, TypeError):
        if reason in {"completion_only", "planned_work_does_not_fit", "insufficient_call_budget"}:
            return StopReason.DEADLINE_EXHAUSTED
        if reason in {"dispatch_policy_unknown_effect", "dispatch_policy_excluded",
                      "dispatch_policy_cost_unverified", "dispatch_policy_stale_assessment"}:
            return StopReason.PREREQUISITE_UNSATISFIED
        if reason == "input_protection_lost":
            return StopReason.CANCELLED
        if reason in {"input_busy", "input_quarantined", "cleanup_uncertain"}:
            return StopReason.INTERACTION_UNSETTLED
        if reason in {"restart_required", "clock_epoch_mismatch"}:
            return StopReason.CLOCK_UNTRUSTED
        if reason in {"wrong_thread", "ownership_scope_mismatch", "view_not_owned"}:
            return StopReason.OWNERSHIP_LOST
        if reason in {"stale_view", "invalid_view", "binding_changed", "binding_origin_changed", "borrowed_view"}:
            return StopReason.STALE_VIEW
        if reason in {"invalid_blocker_binding", "stale_blocker_binding", "root_changed",
                      "blocker_control_changed", "blocker_control_inaccessible", "cross_frame_blocker",
                      "blocker_authority_required", "blocker_authority_invalid", "unsupported_blocker_input",
                      "blocker_rule_changed", "blocker_observation_failed"}:
            return StopReason.NEEDS_INPUT
        return StopReason.IMPLEMENTATION_ERROR


def blocking_pending(ctx):
    """Use the central execution rule; pending_for retains complete history."""
    qualified = getattr(ctx.memory, "blocking_pending_for", None)
    if callable(qualified):
        return qualified(ctx.checkpoint.current.scope, ctx.workflow_operation_id)
    return ctx.memory.pending_for(ctx.checkpoint.current.scope)


class CheckpointRef:
    """One mutable reference shared by derived contexts; update only after ACK."""
    def __init__(self, current):
        self.current = current
        self._owner = threading.get_ident()
        self.results = {}

    def remember_result(self, result):
        if threading.get_ident() != self._owner:
            raise TransitionDenied(StopReason.OWNERSHIP_LOST)
        self.results[(result.operation_id, result.attempt_id)] = result

    def replace_acknowledged(self, checkpoint):
        if threading.get_ident() != self._owner:
            raise TransitionDenied(StopReason.OWNERSHIP_LOST)
        old = self.current
        if (checkpoint.workflow_id != old.workflow_id or checkpoint.scope != old.scope
                or checkpoint.definition_id != old.definition_id
                or checkpoint.definition_version != old.definition_version
                or checkpoint.revision < old.revision):
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        self.current = checkpoint


@dataclass(frozen=True, repr=False)
class RunContext:
    run_id: str
    account_key: str
    workflow_operation_id: str
    session: object
    executor: object
    memory: object
    navigator: object
    clock: object
    budget: DeadlineBudget
    scopes: object
    cancellation: object
    ownership: object
    private_inputs: Callable
    checkpoint: CheckpointRef
    definition: object
    permission: WorkPermission = field(default_factory=WorkPermission)
    path: tuple[str, ...] = ("run",)
    reservations: tuple[tuple[ScopeKey, float], ...] = ()
    active_ticket: object | None = None
    active_request: object | None = None
    active_receipt: object | None = None
    restoring_dispatch: bool = False
    # Borrowed runtime ports. None are serialized into workflow metadata.
    observations: object | None = None
    private_assets: Callable | None = None
    provider_commands: object | None = None
    provider_dispatch_allowed: bool = False
    provider_dispatch_permit: object | None = None
    invocations: object | None = None

    def __post_init__(self):
        cp = self.checkpoint.current
        if (self.account_key != self.budget.scope_key.account_key
                or self.workflow_operation_id != self.budget.scope_key.workflow_operation_id
                or self.memory.account_key != self.account_key
                or self.clock is not self.scopes.clock or self.memory is not self.scopes.memory
                or cp.workflow_id != self.workflow_operation_id
                or cp.definition_id != self.definition.definition_id
                or cp.definition_version != self.definition.version):
            raise ValueError("Context identity, checkpoint and services must agree.")
        self.memory.bind_run(self.run_id)

    @property
    def position(self):
        cp = self.checkpoint.current
        if self.permission.mode == "recovery":
            if cp.recovery is None or cp.recovery.frame_id != self.permission.frame_id:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            return cp.recovery.position
        return cp.main

    def with_budget(self, budget):
        return replace(self, budget=budget)

    def child_path(self, name):
        return replace(self, path=(*self.path, name))

    def scope_key(self, occurrence_id="0"):
        return ScopeKey(self.account_key, self.workflow_operation_id, self.path, occurrence_id)

    def slot(self, suffix_s=0., finalization_s=0.):
        from automation_core.workflow.timing import _number
        _number(suffix_s)
        _number(finalization_s)
        normal = self.budget.normal_until - suffix_s - finalization_s
        hard = self.budget.completion_until - finalization_s
        return replace(self, budget=self.budget.clipped(normal, hard),
                       reservations=(*self.reservations, (self.budget.scope_key, normal)))

    def normal(self):
        if self.permission.mode == "completion" or self.budget.completion_only:
            raise BudgetAdmissionDenied("completion_only")
        if self.permission.mode == "recovery":
            self.position
        return self.call_budget(self.budget.normal_until).checkpoint()

    def call_budget(self, until, operation_id=None):
        permission = WorkPermission("completion", operation_id) if operation_id else self.permission
        return CallBudget(self, until, permission)

    def time_checkpoint(self, operation_id=None, budget=None):
        if self.budget.completion_only and self.executor is None and operation_id is None:
            # Consume an already committed completion without reconstructing
            # expired/untrusted input windows. Existing saved limits stay intact.
            from automation_core.workflow.timing import empty_time_checkpoint
            return empty_time_checkpoint(self.clock, self.scopes.policy)
        pairs = () if operation_id is None else ((operation_id, budget or self.budget),)
        return self.scopes.checkpoint(action_budgets=pairs)

    def acknowledge(self, checkpoint):
        self.checkpoint.replace_acknowledged(checkpoint)

    def acknowledge_ticket(self, ticket):
        self.acknowledge(self.memory.load_checkpoint(ticket.workflow_id))
        return ticket

    def action_scope(self, action_id, step_id=None, occurrence_id=None):
        position = self.position
        step_id = step_id or (position.step_id if isinstance(position, Ready) else action_id)
        occurrence = occurrence_id or (position.occurrence_id if isinstance(position, Ready) else "0")
        opid = operation_id_for(self.checkpoint.current.scope, self.workflow_operation_id, step_id, occurrence)
        return ActionExecutionScope(action_id, opid, "", self.workflow_operation_id)

    def advance(self, ref, next_position):
        self.acknowledge(self.memory.advance_workflow(
            ref, next_position, expected_revision=self.checkpoint.current.revision,
            at=self.time_checkpoint()))

    def settle(self):
        """Latch overruns without discarding a result already established."""
        try:
            self.ownership.assert_owned()
            if not self.memory.healthy:
                return
            if self.checkpoint.current.status == "finished":
                return
            now = self.clock.monotonic()
            for key, cutoff in (*self.reservations, (self.budget.scope_key, self.budget.normal_until)):
                if now >= cutoff:
                    self.scopes.mark_completion_only(key)
        except Exception:
            # A failed store is unhealthy; lost ownership permits no further writes.
            return


@dataclass(frozen=True)
class RouteEntryRequirement:
    open_page: object
    destination_step_id: str
    remaining_s: float = 0.


def consume_route_saved(requirement, ctx):
    """Consume route decisions before any fresh browser observation or input."""
    route = requirement.open_page
    while isinstance(ctx.position, ActionResultAvailable):
        position = ctx.position
        record = ctx.memory.operation(position.operation_id)
        page = next((p for p in route.stages if record.intent.step_id in p.step_ids), None)
        if page is None:
            return None
        if record.intent.permitted_inputs != route.inputs(ctx) or record.intent.private_inputs:
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        result = page.consume_saved(ctx)
        if result is None or result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
            return result
        output = result.output
        if not isinstance(output, RouteOutput):
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        if page is route.select_stage and output.state == "new":
            following = route.open_stage.step_ids[0]
        elif page is route.open_stage and output.state == "opened":
            following = route.navigate_stage.step_ids[0]
        elif page is route.navigate_stage and output.state == "fallback_allowed" and route.fallback_stage:
            following = route.fallback_stage.step_ids[0]
        elif ((page is route.select_stage and output.state == "reuse") or
              (page in (route.navigate_stage, route.fallback_stage) and output.state == "entry")):
            following = requirement.destination_step_id
        else:
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        ctx.advance(position, Ready(following, record.intent.occurrence_id))
    return None


class Navigator:
    def __init__(self, ensure=None, *, catalog=None):
        self.ensure = ensure or (lambda requirement, ctx: requirement.ensure_entry(ctx))
        self.catalog = catalog

    def ensure_entry(self, requirement, ctx) -> NavigationResult:
        if isinstance(requirement, RouteEntryRequirement):
            entry = self._ensure_route(requirement, ctx)
            if not ctx.memory.healthy:
                return entry
            stages = []
            for page in requirement.open_page.stages:
                progress = page._progress(ctx)
                if progress.actions:
                    stages.append(page._result(progress))
            # Stored history supplements the active result. A failed completion
            # write can leave the store healthy without recording that result.
            for carried in entry.stages:
                operation_ids = {action.operation_id for action in carried.actions}
                for index, stage in enumerate(stages):
                    if any(action.operation_id in operation_ids for action in stage.actions):
                        stages[index] = carried
                        break
                else:
                    stages.append(carried)
                for action in carried.actions:
                    ctx.checkpoint.remember_result(action)
            outcome = entry.outcome
            if outcome == Outcome.NOT_RUN and any(action.outcome != Outcome.NOT_RUN
                                                  for stage in stages for action in stage.actions):
                outcome = Outcome.UNKNOWN
            recording = entry.recording
            if recording == Recording.UNCONFIRMED or any(
                    stage.recording == Recording.UNCONFIRMED for stage in stages):
                recording = Recording.UNCONFIRMED
            elif stages:
                recording = Recording.SAVED
            return replace(entry, outcome=outcome, stages=tuple(stages), recording=recording)
        ctx.normal()
        return self.ensure(requirement, ctx)

    def _ensure_route(self, requirement, ctx):
        route = requirement.open_page
        if (self.catalog is None or self.catalog.get(route.recipe.name,
                namespace=getattr(route.recipe, "namespace", None)) != route.recipe
                or any(step not in ctx.definition.steps for step in route.definitions(requirement.destination_step_id))):
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        route.validate_saved(ctx)
        if blocking_pending(ctx) or ctx.checkpoint.current.status == "suspended":
            return NavigationResult(outcome=Outcome.UNKNOWN, stop_reason=StopReason.NEEDS_INPUT)
        while True:
            failed = consume_route_saved(requirement, ctx)
            if failed is not None:
                return NavigationResult(outcome=failed.outcome, stop_reason=failed.stop_reason,
                                        stages=(failed,), recording=failed.recording)
            ctx.normal()
            position = ctx.position
            page = next((p for p in route.stages if isinstance(position, Ready)
                         and position.step_id in p.step_ids), None)
            if page is None:
                # Past the durable entry recipe: re-observe, never run it again.
                return route.find_entry(ctx, ctx.budget.normal_until)
            view = None
            if page in (route.navigate_stage, route.fallback_stage):
                predecessor = route.open_stage if page is route.navigate_stage else route.navigate_stage
                saved = predecessor._progress(ctx)
                if not saved.actions:
                    raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
                from automation_core.browser.interaction import InteractionError
                try:
                    view = route.resolve_output(saved.actions[-1].output, ctx, owned=True)
                except InteractionError as error:
                    return NavigationResult(outcome=Outcome.NOT_RUN, stop_reason=stop_reason(error.reason))
            suffix = sum(p.plan.cost_s() for p in route.stages[route.stages.index(page) + 1:])
            # The caller's auth stages follow the route; reserve their existing plan too.
            suffix += requirement.remaining_s
            result = route.run_stage(page, ctx.slot(suffix_s=suffix), view)
            if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None or result.recording != Recording.SAVED:
                return NavigationResult(outcome=result.outcome, stop_reason=result.stop_reason,
                                        stages=(result,), recording=result.recording,
                                        assessment=getattr(route.component, "last_assessment", None))


def choose_recovery(snapshot, rules):
    try:
        matches = []
        for rule in rules:
            matched = rule.matches(snapshot)
            if type(matched) is not bool:
                raise TypeError("A recovery predicate must return bool.")
            if matched:
                matches.append(rule)
    except Exception as error:
        raise TransitionDenied(StopReason.IMPLEMENTATION_ERROR) from error
    if not matches:
        raise TransitionDenied(StopReason.NO_RECOVERY_RULE)
    priority = max(rule.priority for rule in matches)
    winners = [rule for rule in matches if rule.priority == priority]
    if len(winners) != 1:
        raise TransitionDenied(StopReason.AMBIGUOUS_RECOVERY_RULE)
    return winners[0]


@dataclass(frozen=True)
class BoundOutput:
    """One exact, consumed upstream result supplied to an invocation binder."""
    binding: object
    reference: object
    value: object


@dataclass(frozen=True)
class BlockInvocation:
    block: object
    bind_inputs: Callable
    input_projection: Callable | None = None


class InvocationRegistry:
    """Runtime factories paired with the immutable durable owner/binding map."""
    def __init__(self, definition, invocations):
        from automation_core.contracts import definition_record
        from automation_core.storage.persistence import validate
        validate(definition_record(definition))
        self.definition = definition
        pairs = tuple(invocations)
        self._invocations = {item.block.block_id: item for item in pairs}
        self._metadata = {item.block_id: item for item in definition.blocks}
        if (len(self._invocations) != len(pairs) or not self._metadata
                or set(self._invocations) != set(self._metadata)):
            raise ValueError("Every registered block requires exactly one runtime invocation.")
        self._owners = {step: meta.block_id for meta in definition.blocks for step in meta.step_ids}
        if len(self._owners) != len(definition.steps):
            raise ValueError("Every step requires exactly one registered owner.")
        # The declared graph is fixed for this assembled registry. Validate and
        # retain its plans before admission; execution only needs suffix costs.
        self._time_plans, self._successor_plans = _assemble_time_plans(self)
        self.time_plan = self._time_plans[definition.blocks[0].block_id]
        self.successor_costs = {name: plan.cost_s()
                                for name, plan in self._successor_plans.items()}

    def owner(self, ctx):
        cp = ctx.checkpoint.current
        if cp.recovery is not None:
            record = ctx.memory.operation(cp.recovery.root_operation_id)
            step = record.intent.step_id if record is not None else None
        elif isinstance(cp.main, Ready):
            step = cp.main.step_id
        elif isinstance(cp.main, (InAction, ActionResultAvailable)):
            record = ctx.memory.operation(cp.main.operation_id)
            step = record.intent.step_id if record is not None else None
        else:
            return None
        try:
            return self._owners[step]
        except KeyError:
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE) from None

    def bind(self, block_id, ctx):
        metadata = self._metadata[block_id]
        bound = []
        for declaration in metadata.input_bindings:
            matches = []
            for ref in ctx.checkpoint.current.output_refs:
                record = ctx.memory.operation(ref.operation_id)
                if record is not None and record.intent.step_id == declaration.producer_step_id:
                    if ref.output_key == declaration.output_key and ref.output_schema == declaration.output_schema:
                        matches.append(ref)
            if len(matches) != 1:
                raise TransitionDenied(StopReason.PREREQUISITE_UNSATISFIED)
            parent = next((item.reference for item in bound
                           if item.binding.input_key == declaration.parent_input_key), None)
            if declaration.parent_input_key is not None and parent is None:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            ref = matches[0]
            value = ctx.memory.resolve_output_ref(ref, workflow_id=ctx.workflow_operation_id,
                producer_block_id=declaration.producer_block_id,
                producer_step_id=declaration.producer_step_id,
                output_schema=declaration.output_schema, parent_ref=parent)
            bound.append(BoundOutput(declaration, ref, value))
        invocation = self._invocations[block_id]
        inputs = invocation.bind_inputs(ctx, tuple(bound))
        from automation_core.contracts import PermittedInputs
        from automation_core.storage.persistence import is_payload, payload_schema, validate
        if invocation.input_projection is not None:
            permitted = invocation.input_projection(inputs)
        else:
            permitted = inputs
        try:
            if not is_payload(permitted, PermittedInputs):
                raise ValueError()
            validate(permitted)
            if payload_schema(permitted) != metadata.input_schema:
                raise ValueError()
        except Exception:
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE) from None
        for output in bound:
            if getattr(permitted, output.binding.input_key, None) != output.reference:
                raise TransitionDenied(StopReason.INTENT_MISMATCH)
        saved = ctx.memory.workflow_inputs(ctx.workflow_operation_id)
        if (saved is not None and payload_schema(saved) == metadata.input_schema and saved != permitted):
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        return inputs

    def block(self, block_id):
        return self._invocations[block_id].block

    def metadata(self, block_id):
        return self._metadata[block_id]


def registered_time_plan(registry, start_block_id=None, *, successors_only=False):
    """Return the plan validated when this registry was assembled."""
    first = start_block_id or registry.definition.blocks[0].block_id
    return (registry._successor_plans if successors_only else registry._time_plans)[first]


def _assemble_time_plans(registry):
    """Aggregate declared exclusive successor alternatives once per assembly.

    This adds no execution/navigation mechanism. The ordinary completion already
    chooses one declared successor; no costs are reserved for an unchosen sibling.
    Cyclic block invocation needs an explicit bounded occurrence policy elsewhere
    and is rejected by this supported sequential composition.
    """
    plans, successors = {}, {}
    def plan(block_id, active):
        if block_id in active:
            raise ValueError("Registered block cycles require a bounded occurrence policy.")
        if block_id in plans:
            return plans[block_id]
        names = registry.metadata(block_id).allowed_successor_block_ids
        paths = tuple(plan(name, (*active, block_id)) for name in names)
        tail = successors[block_id] = TimePlan("choice", children=paths)
        plans[block_id] = TimePlan("sequence",
            children=(SequentialRunner.cost_plan(registry.block(block_id)), tail))
        return plans[block_id]
    plan(registry.definition.blocks[0].block_id, ())
    if plans.keys() != {meta.block_id for meta in registry.definition.blocks}:
        raise ValueError("Every registered owner must be reachable from the declared entry.")
    return plans, successors


class SequentialRunner:
    recovery_sources = ()
    automatic_recovery = True

    def run_with_recovery(self, app, sources):
        """Install component routes on the existing runner; no outer retry loop."""
        previous = self.recovery_sources
        self.recovery_sources = tuple(sources)
        try:
            return app.run()
        finally:
            self.recovery_sources = previous

    @staticmethod
    def failure_result(ctx, reason):
        """Keep observed child evidence even when storage cannot be read again."""
        actions = tuple(ctx.checkpoint.results.values())
        outcome = Outcome.UNKNOWN if any(a.outcome != Outcome.NOT_RUN for a in actions) else Outcome.NOT_RUN
        recording = Recording.UNCONFIRMED if (reason == StopReason.STORAGE_UNAVAILABLE or
            any(a.recording == Recording.UNCONFIRMED for a in actions)) else (
            Recording.SAVED if actions else Recording.NOT_REQUIRED)
        stage = StageResult(outcome=outcome, stop_reason=reason, recording=recording,
                            actions=actions, pending_operation_ids=ctx.checkpoint.current.pending_operation_ids)
        return BlockResult(outcome=outcome, stop_reason=reason, recording=recording,
                           stages=(stage,) if actions else (),
                           pending_operation_ids=ctx.checkpoint.current.pending_operation_ids)

    @staticmethod
    def cost_plan(block):
        return (block.plan if block.allocation is None else
                TimePlan("wrapped", children=(block.plan,), allocation=block.allocation))

    def _require_owner(self, ctx):
        try:
            ctx.ownership.assert_owned()
        except Exception:
            raise TransitionDenied(StopReason.OWNERSHIP_LOST) from None
        driver = getattr(ctx.executor, "driver", None)
        if driver is not None:
            driver.assert_owner()
            if driver._input_active or driver._quarantined:
                raise TransitionDenied(StopReason.INTERACTION_UNSETTLED)

    def _interruptions(self, ctx):
        cp = ctx.checkpoint.current
        positions = (cp.main,) + ((cp.recovery.position,) if cp.recovery else ())
        for position in positions:
            if not isinstance(position, InAction):
                continue
            ticket = ctx.memory.load_ticket(cp.workflow_id, position)
            record = ctx.memory.operation(ticket.operation_id)
            if (cp.recovery is None and ticket.effect_intent_event_id is None
                    and record.current_phase == 'reserved' and not record.effect_intents_used):
                continue  # Resume its original admission before any effect entry.
            # A pure observation has no uncertain website effect. Its owner can
            # finish the original read rather than manufacture a failed result.
            if record.intent.effect_kind.value == 'read_only':
                continue
            result = StoredActionResult(
                operation_id=ticket.operation_id, attempt_id=ticket.attempt_id,
                outcome=Outcome.UNKNOWN if ticket.effect_intent_event_id else Outcome.NOT_RUN,
                stop_reason=StopReason.NEEDS_INPUT)
            ctx.acknowledge(ctx.memory.complete_action(
                ticket, result, (), (), expected_revision=ctx.checkpoint.current.revision,
                at=ctx.time_checkpoint()))

    def prepare_reconnect(self, ctx):
        self._require_owner(ctx)
        ctx.normal()
        # An interrupted entered action is persisted as uncertain before any
        # browser handles are discarded. The next invocation reconciles it.
        self._interruptions(ctx)
        if isinstance(ctx.checkpoint.current.main, (InAction, ActionResultAvailable)):
            ctx.acknowledge(ctx.memory.claim_automatic_recovery(ctx.workflow_operation_id,
                expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
        ctx.acknowledge(ctx.memory.claim_reconnect(ctx.workflow_operation_id,
            expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))

    def invoke_block(self, block, inputs, ctx):
        result = self._invoke_block(block, inputs, ctx)
        from automation_core.diagnostics.failure_capture import capture_failure
        if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
            capture_failure(ctx, result)
        helper = getattr(block, "blockers", None)
        decision = getattr(helper, "_decision", None)
        if decision is not None:
            result = replace(result, problem_reports=(decision.problem_report,))
        return result

    def recover_failed_action(self, block, inputs, ctx, visited, *, resuming=False):
        """One initial window and one journalled recovery window per action."""
        from automation_core.workflow.failure_handling import HOLD_ERRORS, identify_error
        from automation_core.workflow.timing import utc_microseconds
        from automation_core.diagnostics.failure_capture import capture_failure
        if not self.automatic_recovery:
            fresh = getattr(block, 'has_fresh_blocker_interruption', None)
            if callable(fresh) and fresh(ctx):
                # A registered popup response shares the caller's window. It
                # cannot refresh, follow a return path or claim another cycle.
                return self.recover_or_hold(block, inputs, ctx)
            if resuming:
                return self._recover_failed_action_once(block, inputs, ctx, visited, resuming=True)
            cp = ctx.checkpoint.current
            saved = ctx.memory.load_result(cp.main) if isinstance(cp.main, ActionResultAvailable) else None
            return self.failure_result(ctx, (saved.stop_reason if saved else cp.stop_reason) or StopReason.NEEDS_INPUT)
        cp = ctx.checkpoint.current
        saved = ctx.memory.load_result(cp.main) if isinstance(cp.main, ActionResultAvailable) else None
        proofs = ctx.memory._proofs(cp, saved) if saved is not None else ()
        code = identify_error(saved, proofs)
        if ctx.cancellation.is_set():
            return self.failure_result(ctx, StopReason.CANCELLED)
        if code in HOLD_ERRORS:
            # Interrupted pending observers can still use their original deadline.
            repaired_read = (resuming and code == 'implementation_error'
                and ctx.memory.incomplete_read_available(cp, ctx.memory.operation(cp.main.operation_id)))
            if not (repaired_read or resuming and blocking_pending(ctx)
                    and code in {'deadline_exhausted', 'implementation_error'}):
                return self.failure_result(ctx, (saved.stop_reason if saved else cp.stop_reason) or StopReason.NEEDS_INPUT)
        connected = getattr(getattr(ctx.session, '_browser', None), 'is_connected', None)
        if callable(connected) and connected() is False:
            return self.failure_result(ctx, StopReason.CONNECTION_LOST)
        if not isinstance(cp.main, (InAction, ActionResultAvailable)):
            return self._recover_failed_action_once(block, inputs, ctx, visited, resuming=resuming)
        opid = ctx.memory.operation(cp.main.operation_id).intent.root_operation_id
        used = ctx.memory.automatic_recoveries_used(opid)
        expires = ctx.memory.checking_window_expires(opid)
        remaining = None if expires is None else (expires - utc_microseconds(ctx.clock.utc_now())) / 1e6
        def after_cycle(held):
            expires = ctx.memory.checking_window_expires(opid)
            current = ctx.checkpoint.current
            final = ctx.memory.load_result(current.main) if isinstance(current.main, ActionResultAvailable) else held
            final_proofs = ctx.memory._proofs(current, final) if isinstance(current.main, ActionResultAvailable) else ()
            if (held is not None and ctx.memory.automatic_recoveries_used(opid)
                    and expires is not None and expires - utc_microseconds(ctx.clock.utc_now()) <= 20_000
                    and identify_error(final, final_proofs) not in HOLD_ERRORS):
                ctx.acknowledge(ctx.memory.suspend_workflow(ctx.workflow_operation_id, StopReason.RECOVERY_EXHAUSTED,
                    expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
                return replace(held, stop_reason=StopReason.RECOVERY_EXHAUSTED)
            return held
        fresh = getattr(block, 'has_fresh_blocker_interruption', None)
        if (remaining is None or remaining > .02) and callable(fresh) and fresh(ctx):
            # A known corrective response shares this window; it is not refresh.
            return self.recover_or_hold(block, inputs, ctx)
        if resuming and (remaining is None or remaining > .02):
            held = self._recover_failed_action_once(block, inputs, ctx, visited, resuming=True)
            position = ctx.checkpoint.current.main
            if held is None and isinstance(position, ActionResultAvailable):
                result = ctx.memory.load_result(position)
                if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                    held = self.failure_result(ctx, result.stop_reason or StopReason.NEEDS_INPUT)
            if held is None:
                return None
            if used:
                held = after_cycle(held)
                capture_failure(ctx, held)
                return held
        if used:
            ctx.acknowledge(ctx.memory.suspend_workflow(ctx.workflow_operation_id, StopReason.RECOVERY_EXHAUSTED,
                expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
            held = self.failure_result(ctx, StopReason.RECOVERY_EXHAUSTED)
            capture_failure(ctx, held)
            return held
        self._require_owner(ctx)
        ctx.acknowledge(ctx.memory.claim_automatic_recovery(ctx.workflow_operation_id,
            expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
        held = self._recover_failed_action_once(block, inputs, ctx, set(), resuming=False)
        position = ctx.checkpoint.current.main
        if held is None and isinstance(position, ActionResultAvailable):
            result = ctx.memory.load_result(position)
            if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                held = self.failure_result(ctx, StopReason.RECOVERY_EXHAUSTED)
        if held is not None:
            held = after_cycle(held)
            capture_failure(ctx, held)
        return held

    def _recover_failed_action_once(self, block, inputs, ctx, visited, *, resuming=False):
        """Select one existing recovery path; helpers do not own retry loops."""
        from types import SimpleNamespace
        from automation_core.workflow.failure_handling import HOLD_ERRORS, identify_error
        _recover_from_sources = (ctx.memory.recover_from_sources if self.automatic_recovery
            else lambda *args: None)
        driver = getattr(ctx.executor, 'driver', None)
        parent = SimpleNamespace(context=ctx, driver=driver)
        cp = ctx.checkpoint.current
        pending = blocking_pending(ctx)
        saved = ctx.memory.load_result(cp.main) if isinstance(cp.main, ActionResultAvailable) else None
        held = BlockResult(outcome=Outcome.UNKNOWN if pending else (saved.outcome if saved else Outcome.NOT_RUN),
            stop_reason=(saved.stop_reason if saved else cp.stop_reason) or StopReason.NEEDS_INPUT,
            recording=Recording.SAVED, pending_operation_ids=tuple(p.operation_id for p in pending))
        if ctx.cancellation.is_set():
            return replace(held, stop_reason=StopReason.CANCELLED)
        key = (cp.workflow_id, getattr(cp.main, 'operation_id', None), getattr(cp.main, 'attempt_id', None))
        if key in visited:
            return held
        visited.add(key)
        reserved = getattr(block, 'resume_reserved_action', None)
        if resuming and not pending and callable(reserved) and reserved(inputs, ctx):
            return None
        proofs = ctx.memory._proofs(cp, saved) if saved is not None else ()
        code = identify_error(saved, proofs)
        if (resuming and pending and code in {'deadline_exhausted', 'implementation_error'}
                and ctx.budget.remaining_completion(ctx.clock.monotonic()) > 0):
            # Older observers used these labels for lost reads/local timeouts.
            # Reconciliation still restores and enforces the original ticket's
            # limits; the saved label alone must not prohibit that readback.
            code = 'uncertain'
        undispatched = getattr(block, 'resume_undispatched_action', None)
        def after_return():
            pending = blocking_pending(ctx)
            if not pending and callable(undispatched) and undispatched(inputs, ctx):
                return None
            return self.recover_or_hold(block, inputs, ctx) if pending else None

        if resuming and not any(p.workflow_id != cp.workflow_id for p in pending) and callable(undispatched):
            if undispatched(inputs, ctx):
                return None
        # A pure read interrupted before producing evidence can be reobserved
        # after a code repair, under its original ticket and deadline.
        incomplete = getattr(block, 'resume_incomplete_read', None)
        if resuming and not pending and callable(incomplete) and incomplete(inputs, ctx):
            return None
        if code in HOLD_ERRORS:
            if resuming and code in {'captcha', 'account_security', 'resource_access_denied', 'resource_unavailable'}:
                # The challenge or access problem may have been resolved.
                # An explicit resume uses the owner's registered readback.
                return self.recover_or_hold(block, inputs, ctx)
            return held
        if any(p.workflow_id != cp.workflow_id for p in pending):
            # A previously entered return-path child must reconcile its own
            # pending navigation first. The route's existing candidate check
            # rejects every unrelated workflow; no new owner or effect is made.
            if driver is not None and _recover_from_sources(parent, self.recovery_sources):
                return after_return()
            return held

        if code == 'unknown_popup' and self.recovery_sources and not pending and cp.recovery is None:
            restored = _recover_from_sources(parent, self.recovery_sources)
            if restored is not None:
                return None if restored else held
            if not resuming:
                return held  # No registered response: do not guess a popup control.

        fresh = getattr(block, 'has_fresh_blocker_interruption', None)
        blocker = callable(fresh) and fresh(ctx)
        readback = pending and resuming
        if cp.recovery is not None or blocker or readback:
            held = self.recover_or_hold(block, inputs, ctx)
            if held is None:
                return None

        # Only the owning component can supply this route and authorize its use
        # with a pending effect. The route itself cannot repeat that effect.
        restored = _recover_from_sources(parent, self.recovery_sources) if driver is not None else None
        if restored:
            return after_return()
        if restored is False:
            return held
        if pending and not resuming:
            # When pending navigation is not qualified, use only the registered
            # result reader. An uncertain input is never rearmed here.
            return self.recover_or_hold(block, inputs, ctx)
        retry = getattr(block, 'resume_preflight_read', None)
        if not blocking_pending(ctx) and callable(retry) and retry(ctx):
            return None
        # Saved correction frames and legacy registered recovery rules use the
        # same admission path. Do not admit another scan after a failed one.
        if resuming and not (cp.recovery is not None or blocker or readback):
            return self.recover_or_hold(block, inputs, ctx)
        return held

    def _invoke_block(self, block, inputs, ctx):
        visited = set()
        self._require_owner(ctx)
        ctx.acknowledge(ctx.memory.load_checkpoint(ctx.workflow_operation_id))
        if hasattr(block, "begin_blocker_invocation"):
            block.begin_blocker_invocation()
        if hasattr(block, "validate_inputs"):
            block.validate_inputs(inputs, ctx)
        self._interruptions(ctx)
        pending = blocking_pending(ctx)
        if pending or ctx.checkpoint.current.status == "suspended" or isinstance(ctx.position, InAction):
            held = self.recover_failed_action(block, inputs, ctx, visited, resuming=True)
            if held is not None:
                return held
        done = block.consume_saved(inputs, ctx)
        if done is not None:
            return done
        ctx.normal()
        entry = ctx.navigator.ensure_entry(block.entry_requirement, ctx)
        if entry.assessment is not None and entry.binding is None:
            capture = getattr(block, "collect_entry_assessment", None)
            admissible = getattr(block, "entry_assessment_admissible", None)
            if (callable(capture) and callable(admissible) and admissible(ctx)
                    and entry.outcome == Outcome.NOT_RUN and entry.recording != Recording.UNCONFIRMED
                    and not blocking_pending(ctx)
                    and entry.assessment.technical_stop not in {
                    StopReason.CANCELLED, StopReason.OWNERSHIP_LOST, StopReason.STORAGE_UNAVAILABLE,
                    StopReason.INTERACTION_UNSETTLED}):
                # Only the owning declared read-only entry action can persist an
                # early assessment. Route effects are never repeated here.
                captured = capture(inputs, entry.assessment, ctx)
                if captured.stop_reason is not None:
                    from automation_core.diagnostics.failure_capture import capture_failure
                    capture_failure(ctx, captured)
                fresh = getattr(block, "has_fresh_blocker_interruption", None)
                if (callable(fresh) and fresh(ctx) and captured.recording == Recording.SAVED
                        and captured.stop_reason is not None and ctx.memory.healthy):
                    held = self.recover_failed_action(block, inputs, ctx, visited)
                    if held is not None:
                        return replace(held, stages=(*entry.stages, *captured.stages, *held.stages))
                    done = block.consume_saved(inputs, ctx)
                    if done is not None:
                        return done
                    ctx.normal()
                    # Only fresh entry after acknowledged recovery can provide
                    # an ordinary binding; no route action is entered again.
                    entry = ctx.navigator.ensure_entry(block.entry_requirement, ctx)
                else:
                    return replace(captured, stages=(*entry.stages, *captured.stages))
        if (entry.outcome != Outcome.CONFIRMED or entry.stop_reason is not None
                or entry.recording == Recording.UNCONFIRMED):
            outcome = Outcome.UNKNOWN if entry.outcome == Outcome.CONFIRMED else entry.outcome
            return BlockResult(outcome=outcome, stop_reason=entry.stop_reason,
                               stages=entry.stages, recording=entry.recording,
                               pending_operation_ids=ctx.checkpoint.current.pending_operation_ids)
        ctx.normal()
        while True:
            result = block.run(inputs, entry.binding, ctx)
            if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                from automation_core.diagnostics.failure_capture import capture_failure
                capture_failure(ctx, result)
            if (result.recording != Recording.SAVED or not ctx.memory.healthy
                    or result.outcome == Outcome.CONFIRMED and result.stop_reason is None):
                return result
            held = self.recover_failed_action(block, inputs, ctx, visited)
            if held is not None:
                return held
            # Resume uses the saved original operation and one-use retry token.
            # No new invocation, root, or deadline is manufactured here.
            done = block.consume_saved(inputs, ctx)
            if done is not None:
                return done
            ctx.normal()

    def recover_or_hold(self, block, inputs, ctx):
        def hold(reason):
            cp = ctx.checkpoint.current
            pending = blocking_pending(ctx)
            last = ctx.memory.load_result(cp.main) if isinstance(cp.main, ActionResultAvailable) else None
            if (reason is StopReason.NO_RECOVERY_RULE and last is not None
                    and last.outcome is Outcome.REJECTED and last.stop_reason is not None):
                reason = last.stop_reason
            return BlockResult(outcome=Outcome.UNKNOWN if pending else (last.outcome if last else Outcome.NOT_RUN),
                stop_reason=reason, recording=Recording.SAVED,
                pending_operation_ids=tuple(item.operation_id for item in pending))

        cached_revision, cached_snapshot = None, None

        def snapshot():
            nonlocal cached_revision, cached_snapshot
            cp = ctx.checkpoint.current
            if cached_revision != cp.revision:
                cached_snapshot = RecoverySnapshot(cp, blocking_pending(ctx),
                    ctx.memory.results_for(cp.workflow_id))
                cached_revision = cp.revision
            enrich = getattr(block, "blocker_recovery_snapshot", None)
            return enrich(cached_snapshot, ctx) if callable(enrich) else cached_snapshot

        def admit(root, rule):
            if rule.kind != RecoveryKind.RECONCILIATION:
                ctx.normal()
            ctx.acknowledge(ctx.memory.resume_or_begin_recovery(root, rule.rule_id,
                episode_id=str(uuid4()), episode_kind=rule.kind,
                expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))

        def recovery_context():
            frame = ctx.checkpoint.current.recovery
            original = ctx.memory.operation(frame.root_operation_id)
            budget = ctx.scopes.budget_for(original.timing_scope_key)
            budget = ctx.scopes.restore_action(original.limits, budget)
            return replace(ctx, budget=budget, permission=WorkPermission("recovery", frame_id=frame.frame_id))

        def verified(proof, rule):
            if (type(proof) is not RecoveryEvidence or any(type(value) is not bool for value in (
                    proof.correction_confirmed, proof.original_resolved,
                    proof.prerequisites_satisfied, proof.replay_safe))):
                raise TransitionDenied(StopReason.IMPLEMENTATION_ERROR)
            matched = rule.verify(proof)
            if type(matched) is not bool:
                raise TransitionDenied(StopReason.IMPLEMENTATION_ERROR)
            return matched

        def save_proof(proof):
            if proof.scope != ctx.checkpoint.current.scope:
                raise TransitionDenied(StopReason.IDENTITY_MISMATCH)
            submissions = tuple(EvidenceSubmission(str(uuid4()), item) for item in proof.observations)
            if submissions:
                ctx.acknowledge(ctx.memory.record_observation(ctx.workflow_operation_id, (), submissions,
                    expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))

        try:
            admitted_here = False
            self._require_owner(ctx)
            cp = ctx.checkpoint.current
            pending = blocking_pending(ctx)
            if ctx.cancellation.is_set():
                return hold(StopReason.CANCELLED)
            rules = ctx.definition.recovery_rules
            if getattr(ctx.definition, "blocks", ()):
                local_definition = getattr(block, "definition", None)
                local_rules = getattr(block, "recovery_rules", None)
                if local_rules is None:
                    local_rules = getattr(local_definition, "recovery_rules", ())
                owned_rules = {rule.rule_id for rule in local_rules}
                rules = tuple(rule for rule in rules if rule.rule_id in owned_rules)
            if pending:
                root_ids = {p.root_operation_id for p in pending}
                if len(root_ids) != 1 or any(p.workflow_id != cp.workflow_id for p in pending):
                    return hold(StopReason.NEEDS_INPUT)
                rule = choose_recovery(snapshot(), tuple(r for r in rules if r.kind == RecoveryKind.RECONCILIATION))
                admit(next(iter(root_ids)), rule)
                admitted_here = True
                reconciled = block.reconcile(pending, inputs, recovery_context())
                if not ctx.memory.healthy or (reconciled is not None and any(
                        result.recording == Recording.UNCONFIRMED for result in reconciled)):
                    reason = (StopReason.STORAGE_UNAVAILABLE if not ctx.memory.healthy else
                              next((r.stop_reason for r in reconciled if r.stop_reason is not None),
                                   StopReason.STORAGE_UNAVAILABLE))
                    return self.failure_result(ctx, reason)
                if reconciled is not None:
                    blocked = next((r.stop_reason for r in reconciled if r.stop_reason is not None), None)
                    if blocked is not None:
                        return hold(blocked)
                if blocking_pending(ctx):
                    return hold(StopReason.NEEDS_INPUT)

            cp = ctx.checkpoint.current
            frame = cp.recovery
            needs_scan = getattr(block, "needs_blocker_recovery_scan", None)
            if not pending and callable(needs_scan) and needs_scan(snapshot(), ctx):
                # Historical summaries may select only the read-only rescan
                # rule. Admission commits and charges before fresh browser reads.
                ctx.normal()
                root = (frame.root_operation_id if frame else
                        ctx.memory.operation(cp.main.operation_id).intent.root_operation_id)
                scan_rule = choose_recovery(snapshot(), tuple(r for r in rules
                                                              if r.kind == RecoveryKind.RECONCILIATION))
                admit(root, scan_rule)
                admitted_here = True
                block.scan_blocker_recovery(inputs, recovery_context())
                cp, frame = ctx.checkpoint.current, ctx.checkpoint.current.recovery

            owns = getattr(block, "owns_blocker_recovery", None)
            if (frame is not None and callable(owns) and owns(snapshot(), ctx)
                    and next(r for r in rules if r.rule_id == frame.rule_id).kind == RecoveryKind.RECONCILIATION
                    and not blocking_pending(ctx)):
                try:
                    selected = choose_recovery(snapshot(), tuple(r for r in rules
                                                                 if r.kind != RecoveryKind.RECONCILIATION))
                except TransitionDenied as error:
                    blocker_stop = block.blocker_hold_reason(snapshot())
                    return hold(blocker_stop or error.stop_reason)
                # A scan frame may enter its first correction in-place. Existing
                # corrections cannot nest and the store enforces that distinction.
                if selected.kind == RecoveryKind.CORRECTION:
                    admit(frame.root_operation_id, selected)
                    admitted_here = True
                    frame = ctx.checkpoint.current.recovery
            if frame is None:
                if not isinstance(cp.main, ActionResultAvailable):
                    return hold(StopReason.NEEDS_INPUT)
                root = ctx.memory.operation(cp.main.operation_id).intent.root_operation_id
                try:
                    rule = choose_recovery(snapshot(), tuple(r for r in rules if r.kind != RecoveryKind.RECONCILIATION))
                except TransitionDenied as error:
                    blocker_stop = getattr(block, "blocker_hold_reason", lambda current: None)(snapshot())
                    return hold(blocker_stop or error.stop_reason)
                admit(root, rule)
                admitted_here = True
                frame = ctx.checkpoint.current.recovery
            else:
                rule = next((r for r in rules if r.rule_id == frame.rule_id), None)
                if rule is None:
                    raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)

            if (rule.kind == RecoveryKind.CORRECTION and frame.phase in {"running", "blocked"}
                    and not isinstance(frame.position, Finished)):
                # An existing frame continues its saved branch; no second frame or
                # uncharged re-entry through the corrective branch's first action.
                result = block.run_recovery(rule.rule_id, inputs, recovery_context())
                if (result.outcome != Outcome.CONFIRMED or result.stop_reason is not None
                        or result.recording != Recording.SAVED):
                    return hold(result.stop_reason or StopReason.NEEDS_INPUT)

            frame = ctx.checkpoint.current.recovery
            if not admitted_here or frame.episode_epoch != ctx.clock.epoch_id:
                validation = choose_recovery(snapshot(), tuple(r for r in rules if r.kind == RecoveryKind.RECONCILIATION))
                admit(frame.root_operation_id, validation)
            proof = block.verify_resume(frame.continuation_id, inputs, recovery_context())
            if callable(owns) and owns(snapshot(), ctx):
                # Save independent post-correction blocker evidence even when
                # readiness fails. It is not a stop on the confirmed consent child.
                save_proof(proof)
            if (proof.scope != cp.scope or not proof.correction_confirmed
                    or not proof.original_resolved or not proof.prerequisites_satisfied
                    or not verified(proof, rule)):
                blocker_stop = getattr(block, "blocker_hold_reason", lambda current: None)(snapshot())
                return hold(blocker_stop or StopReason.PREREQUISITE_UNSATISFIED)
            continuation = next(c for c in ctx.definition.continuations if c.continuation_id == frame.continuation_id)
            if continuation.retry_original:
                if not proof.replay_safe:
                    return hold(StopReason.NEEDS_INPUT)
                ctx.normal()
                if ctx.checkpoint.current.recovery.episode_kind != RecoveryKind.RETRY:
                    retry = choose_recovery(snapshot(), tuple(r for r in rules if r.kind == RecoveryKind.RETRY))
                    admit(frame.root_operation_id, retry)
                    # Retry admission is a new episode; its proof must be fresh.
                    proof = block.verify_resume(frame.continuation_id, inputs, recovery_context())
                    if callable(owns) and owns(snapshot(), ctx):
                        save_proof(proof)
                    if not (proof.original_resolved and proof.prerequisites_satisfied and proof.replay_safe
                            and proof.correction_confirmed and verified(proof, retry)):
                        return hold(StopReason.PREREQUISITE_UNSATISFIED)
            # Store validated observations before the resume transaction; the
            # storage layer then verifies their committed identities/provenance.
            if not callable(owns) or not owns(snapshot(), ctx):
                save_proof(proof)
            ctx.acknowledge(ctx.memory.resume_workflow(ctx.workflow_operation_id, frame.continuation_id,
                proof.observations, expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint()))
            # Optional diagnostics receive only the acknowledged continuation.
            # Their failure cannot erase a committed workflow outcome or retry input.
            notified = getattr(block, "blocker_resume_acknowledged", None)
            if callable(notified):
                try:
                    notified(frame, proof, ctx)
                except Exception:
                    pass
            return None
        except BudgetAdmissionDenied as error:
            return hold(stop_reason(error.reason))
        except TransitionDenied as error:
            if error.__cause__ is not None:
                from automation_core.diagnostics.failure_capture import capture_failure
                capture_failure(ctx, error=error.__cause__)
            return hold(error.stop_reason)

    def _consume_block_completion(self, block_id, ctx):
        from automation_core.workflow.blocks import from_stored
        from automation_core.contracts import AuthenticationCompletionOutput, BlockCompletionOutput
        registry = ctx.invocations
        metadata = registry.metadata(block_id)
        position = ctx.position
        if not isinstance(position, ActionResultAvailable) or ctx.checkpoint.current.recovery is not None:
            return None
        record = ctx.memory.operation(position.operation_id)
        if record.intent.step_id != metadata.completion_step_id:
            return None
        saved = ctx.memory.load_result(position)
        result = from_stored(saved, position.result_event_id)
        if not result.can_advance or not ctx.memory.result_consumption_allowed(position):
            return BlockResult(outcome=result.outcome, stop_reason=result.stop_reason or StopReason.NEEDS_INPUT,
                               recording=result.recording, output=result.output)
        output = result.output
        if type(output) is BlockCompletionOutput:
            if output.block_id != block_id:
                raise TransitionDenied(StopReason.INTENT_MISMATCH)
            successor = output.successor_block_id
        elif type(output) is AuthenticationCompletionOutput:
            if len(metadata.allowed_successor_block_ids) > 1:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            successor = next(iter(metadata.allowed_successor_block_ids), None)
        else:
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        if successor is None:
            if not metadata.final:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            target = Finished(position.result_event_id)
        else:
            if metadata.final or successor not in metadata.allowed_successor_block_ids:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            target = Ready(registry.metadata(successor).entry_step_id, record.intent.occurrence_id)
        ctx.advance(position, target)
        return BlockResult(outcome=Outcome.CONFIRMED, recording=Recording.SAVED, output=output,
                           stages=(StageResult(outcome=Outcome.CONFIRMED, recording=Recording.SAVED,
                                               output=output, actions=(result,)),))

    def _run_registered(self, blocks, ctx):
        registry = ctx.invocations
        if registry is None or registry.definition != ctx.definition:
            return RunResult(outcome=Outcome.NOT_RUN, stop_reason=StopReason.WORKFLOW_INCOMPATIBLE)
        blocks = tuple(blocks)
        if (len({block.block_id for block in blocks}) != len(blocks) or
                {block.block_id for block in blocks} != {meta.block_id for meta in ctx.definition.blocks}):
            return RunResult(outcome=Outcome.NOT_RUN, stop_reason=StopReason.WORKFLOW_INCOMPATIBLE)
        results, last = [], None
        while True:
            child = ctx
            try:
                self._require_owner(ctx)
                if not ctx.memory.healthy:
                    ctx.memory.reload()
                    from automation_core.workflow.timing import empty_time_checkpoint
                    ctx.acknowledge(ctx.memory.open_or_resume_workflow(ctx.definition,
                        ctx.workflow_operation_id, ctx.checkpoint.current.scope,
                        at=empty_time_checkpoint(ctx.clock, ctx.scopes.policy),
                        workflow_inputs=ctx.memory.workflow_inputs(ctx.workflow_operation_id)))
                ctx.acknowledge(ctx.memory.load_checkpoint(ctx.workflow_operation_id))
                if last is None:
                    for reference in ctx.checkpoint.current.output_refs:
                        record = ctx.memory.operation(reference.operation_id)
                        completed_owner = next((meta.block_id for meta in ctx.definition.blocks
                            if record is not None and record.intent.step_id == meta.completion_step_id), None)
                        if completed_owner is not None:
                            last = completed_owner
                if isinstance(ctx.checkpoint.current.main, Finished):
                    return RunResult(outcome=Outcome.CONFIRMED, blocks=tuple(results),
                        last_confirmed_block=last or next(meta.block_id for meta in ctx.definition.blocks if meta.final),
                        recording=Recording.SAVED)
                block_id = registry.owner(ctx)
                block = registry.block(block_id)
                # Components may prepare local inputs before binding or reserving
                # their suffix. The component owns its policy and saved identity.
                prepare_entry = getattr(block, 'prepare_entry', None)
                if (callable(prepare_entry) and type(ctx.position) is Ready
                        and ctx.position.step_id == block.metadata.entry_step_id):
                    checkpoint, value_ref = prepare_entry(ctx)
                    ctx.acknowledge(checkpoint)
                    if value_ref is None:
                        result = BlockResult(outcome=Outcome.NOT_RUN,
                            stop_reason=StopReason.NEEDS_INPUT, recording=Recording.SAVED)
                        return RunResult(outcome=Outcome.UNKNOWN if last else Outcome.NOT_RUN,
                            stop_reason=StopReason.NEEDS_INPUT, recording=Recording.SAVED,
                            blocks=(*results, result), last_confirmed_block=last)
                suffix = registry.successor_costs[block_id]
                child = ctx.slot(suffix_s=suffix).child_path(block_id)
                # A committed completion is consumed before any binder, navigation
                # or observation. Completed earlier blocks are never invoked.
                completed = self._consume_block_completion(block_id, child)
                if completed is not None:
                    result = completed
                else:
                    pending = blocking_pending(child)
                    if (not pending and child.checkpoint.current.status != "suspended"
                            and not isinstance(child.position, InAction)):
                        child.normal()
                    bound_inputs = registry.bind(block_id, child)
                    before = child.checkpoint.current.revision
                    if pending or child.checkpoint.current.status == "suspended" or isinstance(child.position, InAction):
                        if child.scopes._read(child.scope_key()) is not None:
                            child = child.with_budget(child.scopes.budget_for(child.scope_key().token()))
                        result = self.invoke_block(block, bound_inputs, child)
                    elif block.allocation is not None:
                        result = TimeSensitive(block.allocation)(child,
                            lambda scoped: self.invoke_block(block, bound_inputs, scoped),
                            key=child.scope_key(), plan=block.plan)
                    else:
                        result = self.invoke_block(block, bound_inputs, child)
                    if (result.outcome == Outcome.CONFIRMED and result.stop_reason is None
                            and result.recording == Recording.SAVED and child.memory.healthy):
                        completed = self._consume_block_completion(block_id, child)
                        if completed is None:
                            if child.checkpoint.current.revision == before:
                                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
                            continue
                        result = completed
                results.append(result)
                if (result.outcome != Outcome.CONFIRMED or result.stop_reason is not None
                        or result.recording != Recording.SAVED or not child.memory.healthy):
                    return RunResult(outcome=Outcome.UNKNOWN if last and result.outcome == Outcome.NOT_RUN else result.outcome,
                        stop_reason=result.stop_reason, recording=result.recording, blocks=tuple(results),
                        last_confirmed_block=last, pending_operation_ids=result.pending_operation_ids,
                        problem_reports=tuple(report for item in results for report in item.problem_reports))
                last = block_id
            except BudgetAdmissionDenied as error:
                reason = stop_reason(error.reason)
            except TransitionDenied as error:
                reason = error.stop_reason
            except StorageFailure:
                reason = StopReason.STORAGE_UNAVAILABLE
            except Exception as error:
                from automation_core.diagnostics.failure_capture import capture_failure
                capture_failure(child, error=error)
                reason = StopReason.IMPLEMENTATION_ERROR
            finally:
                child.settle()
            if 'reason' in locals():
                failed = self.failure_result(child, reason)
                return RunResult(outcome=Outcome.UNKNOWN if last and failed.outcome == Outcome.NOT_RUN else failed.outcome,
                    stop_reason=reason,
                    recording=failed.recording, blocks=(*results, failed), last_confirmed_block=last,
                    pending_operation_ids=failed.pending_operation_ids,
                    problem_reports=tuple(report for item in (*results, failed) for report in item.problem_reports))

    def run(self, blocks, inputs, ctx):
        from automation_core.diagnostics.run_progress import workflow_entered
        workflow_entered(ctx.checkpoint.current)
        if getattr(ctx.definition, "blocks", ()):
            return self._run_registered(blocks, ctx)
        results, last = [], None
        blocks = tuple(blocks)
        for index, block in enumerate(blocks):
            suffix = sum(self.cost_plan(item).cost_s() for item in blocks[index + 1:])
            child = ctx.slot(suffix_s=suffix).child_path(block.block_id)
            try:
                self._require_owner(child)
                if not child.memory.healthy:
                    child.memory.reload()
                    from automation_core.workflow.timing import empty_time_checkpoint
                    child.acknowledge(child.memory.open_or_resume_workflow(
                        child.definition, child.workflow_operation_id, child.checkpoint.current.scope,
                        at=empty_time_checkpoint(child.clock, child.scopes.policy)))
                cp = child.checkpoint.current
                pending = blocking_pending(child)
                saved = None if pending or cp.status == "suspended" else block.consume_saved(inputs, child)
                if saved is not None:
                    result = saved
                elif cp.status == "suspended" or pending or isinstance(cp.main, InAction):
                    if child.scopes._read(child.scope_key()) is not None:
                        child = child.with_budget(child.scopes.budget_for(child.scope_key().token()))
                    result = self.invoke_block(block, inputs, child)
                elif block.allocation is not None:
                    result = TimeSensitive(block.allocation)(
                        child, lambda scoped: self.invoke_block(block, inputs, scoped),
                        key=child.scope_key(), plan=block.plan)
                else:
                    result = self.invoke_block(block, inputs, child)
            except BudgetAdmissionDenied as error:
                result = self.failure_result(child, stop_reason(error.reason))
            except TransitionDenied as error:
                result = self.failure_result(child, error.stop_reason)
            except StorageFailure:
                result = self.failure_result(child, StopReason.STORAGE_UNAVAILABLE)
            except Exception as error:
                from automation_core.diagnostics.failure_capture import capture_failure
                capture_failure(child, error=error)
                result = self.failure_result(child, StopReason.IMPLEMENTATION_ERROR)
            finally:
                child.settle()
            results.append(result)
            if (result.outcome != Outcome.CONFIRMED or result.stop_reason is not None
                    or result.recording == Recording.UNCONFIRMED or not ctx.memory.healthy):
                outcome = result.outcome
                if outcome == Outcome.NOT_RUN and last is not None:
                    outcome = Outcome.UNKNOWN
                return RunResult(outcome=outcome, blocks=tuple(results), last_confirmed_block=last,
                                 pending_operation_ids=result.pending_operation_ids,
                                 stop_reason=result.stop_reason, recording=result.recording,
                                 problem_reports=tuple(report for item in results for report in item.problem_reports))
            last = block.block_id
        return RunResult(outcome=Outcome.CONFIRMED, blocks=tuple(results),
                         last_confirmed_block=last, recording=Recording.SAVED)
