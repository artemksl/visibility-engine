"""Ordered stages and effect owners; every admitted action has a durable result."""
from dataclasses import dataclass, field, replace
from typing import Callable
from uuid import uuid4

from automation_core.contracts import (
    AccountOutput, ActionResult, ActionResultAvailable, AuthenticatedAccount, BlockResult,
    CorrelatedEvidence, EffectKind, EmptyPayload, Evidence, EvidenceRef,
    EvidenceSubmission, FactProvenance, Finished, InAction, ObservationDetails,
    OperationIntent, Outcome, PreparedDialogPolicy, Ready, Recording, SensitiveValue,
    StageResult, StateFact, StopReason, StoredActionResult, operation_id_for,
)
from automation_core.storage.memory import StorageFailure, TransitionDenied
from automation_core.storage.persistence import payload_schema, validate
from automation_core.workflow.runtime import stop_reason
from automation_core.workflow.timing import (ActionTiming, ActionWindow, BudgetAdmissionDenied, BudgetSpec,
                     TimePlan, TimeSensitive, utc_microseconds,
                     CONTROL_RESULT_S, PAGE_RESULT_S, CREATION_RESULT_S,
                     RESULT_RETRY_MULTIPLIER)


class ActionPreconditionStop(Exception):
    """A guarded precheck's typed evidence, before effect-bearing input."""
    def __init__(self, evidence):
        if type(evidence) is not Evidence or evidence.outcome != Outcome.NOT_RUN:
            raise ValueError("Precheck stop requires positively undispatched evidence.")
        self.evidence = evidence
        super().__init__("Action prerequisite is blocked.")


def retry_on(*states, stop=StopReason.NEEDS_INPUT, versions=None, allow_unobserved=False):
    """Local failure eligibility; the journal separately enforces no effects/limits.

    Versions are only needed to preserve eligibility of existing saved recipes.
    This predicate does no browser or private-input work.
    """
    def eligible(memory, checkpoint, record, result):
        if result.stop_reason is not stop:
            return False
        if versions is not None and getattr(memory.workflow_inputs(checkpoint.workflow_id),
                'definition_version', None) not in versions:
            return False
        proofs = memory._proofs(checkpoint, result)
        if not proofs:
            return allow_unobserved
        from automation_core.workflow.failure_handling import identify_error
        return (identify_error(result, proofs) in {'unknown', 'loading', 'navigation', 'unexpected_page'}
                and any(type(proof.details) is ObservationDetails
                        and proof.details.state in states for proof in proofs))
    return eligible


def recovery_action(block, step_id):
    """Find the actual registered action, including wrapped blocks."""
    for page in getattr(block, 'stages', ()):
        if step_id in page.step_ids:
            return page.steps[page.step_ids.index(step_id)].action
    return None


def _failed_input_check(memory, checkpoint, record, result):
    # The journal also requires NOT_RUN, no receipt/output, and zero effects.
    # Runtime must reconstruct the exact saved intent before rearming it.
    return result.stop_reason is StopReason.INTENT_MISMATCH


def retry_available(memory, workflow_id, invocations):
    """Use the same local declaration at attachment and runtime admission."""
    cp = memory.load_checkpoint(workflow_id)
    if type(cp.main) is not ActionResultAvailable:
        return False
    step_id = memory.operation(cp.main.operation_id).intent.step_id
    for invocation in invocations:
        action = recovery_action(invocation.block, step_id)
        if action is not None:
            return (memory.action_retry_available(workflow_id, action.recoverable)
                    or memory.action_retry_available(workflow_id, _failed_input_check))
    return False


def observed_evidence(ctx, *, outcome, state, output=None, resource_id=None,
                      correlation_kind="bound_transition", observations=(),
                      stop=None, fact_key=None):
    """Package a component's checked observation with the active operation identity.

    The caller must establish the indicated correlation from the component; this
    helper supplies provenance and performs no observation or success inference.
    """
    ticket = ctx.active_ticket
    if ticket is None:
        raise ValueError("An observation requires an admitted action.")
    scope = ctx.checkpoint.current.scope
    event_id = str(uuid4())
    utc = utc_microseconds(ctx.clock.utc_now())
    evidence = CorrelatedEvidence(
        observer_id="/".join(ctx.path), observer_version=1, scope=scope,
        observed_utc_us=utc, operation_id=ticket.operation_id,
        attempt_ids=(ticket.attempt_id,), correlation_kind=correlation_kind,
        details=ObservationDetails(state, resource_id))
    facts = ()
    if fact_key is not None:
        facts = (StateFact(fact_key, scope.account_key, scope.account_key,
                  payload_schema(output), output, utc, ticket.operation_id, event_id,
                  FactProvenance(evidence.observer_id, 1, ctx.run_id,
                                 ctx.path[1] if len(ctx.path) > 1 else ctx.path[0],
                                 ctx.path[-2] if len(ctx.path) > 1 else ctx.path[0],
                                 ctx.path[-1], scope.profile_id)),)
    return Evidence(outcome, output, observations, facts, stop,
                    (EvidenceSubmission(event_id, evidence),))


def from_stored(stored, event_id):
    return ActionResult(outcome=stored.outcome, operation_id=stored.operation_id,
                        attempt_id=stored.attempt_id, stop_reason=stored.stop_reason,
                        output=stored.output, receipt=stored.receipt,
                        recording=Recording.SAVED, result_event_id=event_id)


class ActionBlock:
    def __init__(self, action_id: str, timing: ActionTiming, request: Callable | None,
                 observe: Callable, precondition: Callable | None = None,
                 consequential: bool = True, submission_cutoff: Callable | None = None,
                 *, intent_inputs=None, private_refs=None, semantic_target=None,
                 project_output=None, dialog_policy=None, dialog_matcher=None,
                 provider_request=None, input_authority=None, dispatch_assessment=None,
                 native_witness=None, allow_satisfied_precondition=False, recoverable=None,
                 observation_s=None):
        if observation_s is not None:
            from automation_core.workflow.timing import _number
            _number(observation_s, positive=True)
        self.observation_s = observation_s
        if recoverable is not None and not callable(recoverable):
            raise TypeError('Recovery eligibility must be a local predicate.')
        self.recoverable = recoverable
        if type(allow_satisfied_precondition) is not bool:
            raise ValueError('A fixed precondition-result policy is required.')
        self.allow_satisfied_precondition = allow_satisfied_precondition
        if provider_request is not None and (request is not None or dialog_policy is not None):
            raise ValueError("An action declares exactly one dispatch alternative.")
        if input_authority is not None and (request is None or provider_request is not None
                                            or dialog_policy is not None):
            raise ValueError("Guarded input authority requires one ordinary input action.")
        self.action_id, self.timing = action_id, timing
        self.request, self.observe = request, observe
        self.precondition, self.consequential = precondition, consequential
        self.submission_cutoff = submission_cutoff
        self.intent_inputs = intent_inputs or (lambda inputs: EmptyPayload())
        self.private_refs = private_refs or (lambda inputs: ())
        self.semantic_target = semantic_target or action_id
        self.project_output = project_output or (lambda value: value)
        self.dialog_policy, self.dialog_matcher = dialog_policy, dialog_matcher
        self.provider_request = provider_request
        self.input_authority = input_authority
        self.dispatch_assessment = dispatch_assessment
        if native_witness is not None and (not callable(native_witness) or input_authority is None):
            raise ValueError("A native witness requires a guarded ordinary input action.")
        self.native_witness = native_witness
        self.popup_check = None
        self.effect_kind = (EffectKind.READ_ONLY if request is None and provider_request is None else
                            EffectKind.CONSEQUENTIAL if consequential else EffectKind.REPEATABLE_PREPARATION)
        self.plan = TimePlan("action", action=timing)

    def observation_window(self, request=None, *, receipt=None, retry=False):
        """The component selects its result type; ordinary inputs have defaults.

        Declared cost plans and saved banks stay exact. This passive window is
        always clipped to their remaining deadline by the caller.
        """
        # Reconciliation has a saved receipt, but does not rebuild the input.
        kind = getattr(request, 'kind', None) or getattr(receipt, 'phase', None)
        if self.observation_s is not None:
            seconds = self.observation_s
        elif kind == 'uploadImage':
            seconds = CREATION_RESULT_S
        elif kind in ('fill', 'selectOption', 'setChecked', 'setSlider',
                    'move', 'hover', 'scroll', 'activateTab', 'closeOwnedTab', 'closeTab'):
            seconds = CONTROL_RESULT_S
        elif self.effect_kind is not EffectKind.READ_ONLY:
            seconds = PAGE_RESULT_S
        else:
            seconds = max(CONTROL_RESULT_S, self.timing.observe_s)
        return seconds * (RESULT_RETRY_MULTIPLIER if retry else 1.)

    def _intent(self, inputs, scope, ctx):
        position = ctx.position
        if isinstance(position, (InAction, ActionResultAvailable)):
            record = ctx.memory.operation(position.operation_id)
            position = Ready(record.intent.step_id, record.intent.occurrence_id)
        if not isinstance(position, Ready):
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        definition = next((s for s in ctx.definition.steps if s.step_id == position.step_id), None)
        if definition is None or definition.effect_kind != self.effect_kind:
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        opid = operation_id_for(ctx.checkpoint.current.scope, ctx.workflow_operation_id,
                               position.step_id, position.occurrence_id)
        if (scope.action_id != self.action_id or scope.operation_id != opid
                or scope.parent_workflow_operation_id != ctx.workflow_operation_id):
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        permitted = self.intent_inputs(inputs)
        private = self.private_refs(inputs)
        if self.dialog_policy and self.dialog_policy.prompt_ref:
            private = tuple(dict.fromkeys((*private, self.dialog_policy.prompt_ref)))
        frame = ctx.checkpoint.current.recovery if ctx.permission.mode == "recovery" else None
        return OperationIntent(opid, ctx.workflow_operation_id, ctx.definition.definition_id,
            ctx.definition.version, position.step_id, position.occurrence_id,
            ctx.checkpoint.current.scope, self.effect_kind, self.semantic_target,
            payload_schema(permitted), permitted, private,
            frame.root_operation_id if frame else opid,
            frame.root_operation_id if frame else None, definition.policy.policy_id,
            dialog_policy=self.dialog_policy)

    def _window(self, inputs, ctx, opid, *, submission=True):
        cutoff = self.submission_cutoff(inputs) if submission and self.submission_cutoff else None
        return ActionWindow.allocate(ctx.budget, self.timing, opid, cutoff)

    def _checking_until(self, ctx, ticket, window, *, possible=False):
        retry = bool(ctx.memory.automatic_recoveries_used(
            ctx.memory.operation(ticket.operation_id).intent.root_operation_id))
        receipt = ctx.active_receipt or ctx.memory.original_dispatch_receipt(ticket.operation_id, ticket.attempt_id)
        cp, expires = ctx.memory.checking_window(ticket,
            self.observation_window(ctx.active_request, receipt=receipt, retry=retry),
            expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint(ticket.operation_id))
        ctx.acknowledge(cp)
        return min(window.completion_observe_until if possible else window.normal_observe_until,
            ctx.clock.monotonic() + max(0., (expires - utc_microseconds(ctx.clock.utc_now())) / 1e6))

    @staticmethod
    def _wait(ctx, until, *, possible=False):
        pump = getattr(ctx.observations, 'pump_wait', None)
        remaining = until - ctx.clock.monotonic()
        if not callable(pump) or remaining <= .02:
            return False
        ctx.ownership.assert_owned()
        pump(min(.2, remaining - .01), until, completion=possible)
        return ctx.clock.monotonic() < until

    def _popup(self, inputs, binding, ctx, budget):
        handler = self.popup_check
        if handler is None and self.input_authority is None:
            handler = getattr(ctx.memory, 'check_action_popup', None)
        return handler(inputs, binding, ctx, budget) if handler else None

    @staticmethod
    def _unfamiliar_popup(evidence):
        from automation_core.workflow.failure_handling import identify_error
        return identify_error(evidence, tuple(item.evidence for item in evidence.evidence)) == 'unknown_popup'

    @staticmethod
    def _interruption(evidence):
        from automation_core.workflow.failure_handling import HOLD_ERRORS, identify_error
        return identify_error(evidence, tuple(item.evidence for item in evidence.evidence)) in (
            HOLD_ERRORS | {'cookie_consent', 'known_popup'})

    def _ready_or_result(self, inputs, binding, ctx, ticket, window):
        until = self._checking_until(ctx, ticket, window)
        while (self.precondition or self.popup_check or self.input_authority is None
                and callable(getattr(ctx.memory, 'check_action_popup', None))):
            budget = ctx.call_budget(min(until, window.input_until))
            try:
                eligible = self.precondition(inputs, binding, ctx, budget) if self.precondition else True
            except ActionPreconditionStop as error:
                eligible = error.evidence
            except TransitionDenied as error:
                if error.stop_reason not in (StopReason.NEEDS_INPUT, StopReason.PAGE_NOT_READY,
                        StopReason.PREREQUISITE_UNSATISFIED, StopReason.STALE_VIEW):
                    raise
                eligible = observed_evidence(ctx, outcome=Outcome.NOT_RUN,
                    state='prerequisite_unavailable', stop=error.stop_reason)
            if type(eligible) is Evidence and eligible.outcome == Outcome.CONFIRMED and eligible.stop_reason is None:
                return eligible
            popup = self._popup(inputs, binding, ctx, budget)
            if popup is True:
                continue
            if popup is not None:
                if not self._unfamiliar_popup(popup) or not self._wait(ctx, min(until, window.input_until)):
                    return popup
                continue
            if eligible is True:
                return True
            if type(eligible) is Evidence:
                if (self._interruption(eligible) or eligible.stop_reason not in (StopReason.NEEDS_INPUT,
                        StopReason.PAGE_NOT_READY, StopReason.PREREQUISITE_UNSATISFIED, StopReason.STALE_VIEW)):
                    return eligible
            elif type(eligible) is not bool:
                raise TransitionDenied(StopReason.IMPLEMENTATION_ERROR)
            if not self._wait(ctx, min(until, window.input_until)):
                return eligible if type(eligible) is Evidence else Evidence(Outcome.NOT_RUN,
                    stop_reason=StopReason.PREREQUISITE_UNSATISFIED)
        return True

    def _finish(self, evidence, ticket, ctx, receipt, *, resolution=False, read_resolution=False,
                dispatch_restore=False, error=None):
        from automation_core.diagnostics.live_faults import observe_evidence
        observe_evidence(ctx, evidence)
        reason = evidence.stop_reason
        if ctx.cancellation.is_set():
            reason = reason or StopReason.CANCELLED
        if receipt and receipt.reason in {"cancelled", "unexpected_dialog", "dialog_limit",
                                          "ownership_lost", "input_quarantined"}:
            reason = reason or stop_reason(receipt.reason)
        if receipt and receipt.reason == "expected_dialog_missing":
            reason = reason or StopReason.DEADLINE_EXHAUSTED
        if receipt and getattr(receipt, "cleanup_reason", None):
            reason = reason or StopReason.INTERACTION_UNSETTLED
        if receipt and receipt.reason in {"dialog_unsettled", "dialog_guard_denied", "invalid_dialog_policy"}:
            reason = reason or StopReason.INTERACTION_UNSETTLED
        if receipt and receipt.reason in {"input_expired", "storage_unavailable"}:
            reason = reason or stop_reason(receipt.reason)
        if receipt and any(d.reason == "deadline_exhausted" for d in getattr(receipt, "dialogs", ())):
            reason = reason or StopReason.DEADLINE_EXHAUSTED
        if evidence.outcome in {Outcome.NOT_RUN, Outcome.UNKNOWN}:
            reason = reason or StopReason.NEEDS_INPUT
        runtime = ActionResult(outcome=evidence.outcome, operation_id=ticket.operation_id,
            attempt_id=ticket.attempt_id, output=evidence.output, observations=evidence.observations,
            receipt=receipt, stop_reason=reason, recording=Recording.UNCONFIRMED)
        def remember(result, error=error):
            ctx.checkpoint.remember_result(result)
            from automation_core.diagnostics.failure_capture import capture_failure
            if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                capture_failure(ctx, result, error=error)
            return result
        try:
            ctx.ownership.assert_owned()
        except Exception:
            return remember(replace(runtime, stop_reason=StopReason.OWNERSHIP_LOST))
        try:
            safe_output = self.project_output(evidence.output)
            if safe_output is not None:
                validate(safe_output)
            stored = StoredActionResult(operation_id=ticket.operation_id, attempt_id=ticket.attempt_id,
                outcome=evidence.outcome, stop_reason=reason, output=safe_output,
                evidence_refs=tuple(EvidenceRef(e.event_id) for e in evidence.evidence),
                receipt=receipt.stored() if receipt else None)
            if dispatch_restore:
                cp = ctx.memory.finish_undispatched_restore(ticket, stored, evidence.evidence, evidence.facts,
                    expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint(ticket.operation_id))
            elif read_resolution:
                cp = ctx.memory.complete_read_observation(ticket, stored, evidence.facts,
                    evidence.evidence, expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(ticket.operation_id))
            elif resolution:
                cp = ctx.memory.record_resolution(ticket.operation_id, stored, evidence.facts,
                    evidence.evidence, expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(ticket.operation_id))
            else:
                cp = ctx.memory.complete_action(ticket, stored, evidence.facts, evidence.evidence,
                    expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(ticket.operation_id))
            ctx.acknowledge(cp)
            ref = ctx.memory.result_ref_for(ticket.operation_id)
            runtime = replace(runtime, recording=Recording.SAVED, result_event_id=ref.result_event_id)
            return remember(runtime)
        except StorageFailure:
            return remember(replace(runtime, stop_reason=StopReason.STORAGE_UNAVAILABLE))
        except TransitionDenied as error:
            return remember(replace(runtime, stop_reason=error.stop_reason))
        except BudgetAdmissionDenied as error:
            return remember(replace(runtime, stop_reason=stop_reason(error.reason)))
        except Exception as failure:
            return remember(replace(runtime, stop_reason=StopReason.IMPLEMENTATION_ERROR), failure)

    def _observe(self, inputs, binding, ticket, ctx, window, receipt, *, possible, resolution=False,
                 read_resolution=False, dispatch_restore=False, error=None):
        # Observers retain technical evidence, but cannot reuse the effect port.
        ctx = replace(ctx, active_receipt=receipt,
                      active_request=None if self.provider_request is not None else ctx.active_request,
                      provider_dispatch_allowed=False, provider_dispatch_permit=None)
        until = self._checking_until(ctx, ticket, window, possible=possible)
        budget = ctx.call_budget(until, ticket.operation_id if possible else None)
        def latch_overrun():
            if ctx.clock.monotonic() >= window.normal_observe_until:
                try:
                    ctx.ownership.assert_owned()
                    ctx.scopes.mark_completion_only(ctx.budget.scope_key)
                except Exception:
                    pass  # Recording failure cannot erase the effect or release ownership.
        latch_overrun()
        try:
            while True:
                budget.checkpoint()
                evidence = self.observe(inputs, binding, ctx, budget)
                if not isinstance(evidence, Evidence):
                    raise TypeError("Observer must return typed evidence.")
                if evidence.outcome == Outcome.CONFIRMED and evidence.stop_reason is None:
                    break
                if self._interruption(evidence):
                    break
                popup = self._popup(inputs, binding, ctx, budget)
                if popup is True:
                    continue
                if popup is not None:
                    evidence = replace(popup, outcome=Outcome.UNKNOWN if possible else Outcome.NOT_RUN)
                    if not self._unfamiliar_popup(popup) or not self._wait(ctx, until, possible=possible):
                        break
                    continue
                if (evidence.outcome not in (Outcome.UNKNOWN, Outcome.NOT_RUN)
                        or evidence.stop_reason not in (StopReason.PAGE_NOT_READY, StopReason.NEEDS_INPUT)
                        or not self._wait(ctx, until, possible=possible)):
                    break
        except BudgetAdmissionDenied as failure:
            error = failure
            evidence = Evidence(Outcome.UNKNOWN if possible else Outcome.NOT_RUN,
                                stop_reason=stop_reason(failure.reason))
        except Exception as failure:
            error = failure
            from automation_core.diagnostics.live_faults import observe_read_failure
            observe_read_failure(ctx, failure)
            from automation_core.browser.interaction import InteractionError
            if isinstance(error, TransitionDenied):
                reason, cause = error.stop_reason, error.stop_reason.value
            elif isinstance(error, InteractionError):
                reason = stop_reason(error.reason)
                cause = reason.value
            else:
                reason = StopReason.IMPLEMENTATION_ERROR
                # Keep the failed check and a sanitized cause, never exception
                # text (which may contain private inputs or page contents).
                cause = type(error).__name__ if type(error) in (
                    TypeError, ValueError, AttributeError, KeyError, IndexError,
                    AssertionError, RuntimeError, OSError) else "Exception"
            evidence = observed_evidence(ctx,
                outcome=Outcome.UNKNOWN if possible else Outcome.NOT_RUN,
                state="observe.expected_evidence.observed_" + cause,
                stop=reason)
        # A synchronous observer returning after its cutoff may still prove the effect.
        latch_overrun()
        return self._finish(evidence, ticket, ctx, receipt, resolution=resolution,
            read_resolution=read_resolution, dispatch_restore=dispatch_restore, error=error)

    def _prepare_dialog(self, request, binding, ctx, window):
        if self.dialog_policy is None:
            if request.dialog is not None:
                raise ValueError("Dialog acceptance needs an immutable action declaration.")
            return request
        if self.dialog_matcher is None:
            raise ValueError("Dialog declaration needs a pure matcher.")
        prompt = (ctx.private_inputs(self.dialog_policy.prompt_ref)
                  if self.dialog_policy.prompt_ref else None)
        def guard():
            # Deliberately memory-only: no SQLite, provider, browser or recovery call.
            ctx.ownership.assert_owned()
            if (ctx.permission.mode == "completion" or not ctx.memory.healthy
                    or ctx.cancellation.is_set() or ctx.clock.monotonic() >= window.input_until):
                raise BudgetAdmissionDenied("deadline_exhausted")
            keys = (*ctx.budget.ancestor_keys, ctx.budget.scope_key)
            if ctx.budget.completion_only or any(
                ctx.scopes.records.get(key.token()) is not None and
                ctx.scopes.records[key.token()].limits.completion_only for key in keys):
                raise BudgetAdmissionDenied("completion_only")
            driver = getattr(ctx.executor, "driver", None)
            if driver is not None and binding.view.generation != driver.generation:
                raise BudgetAdmissionDenied("input_expired")
        return replace(request, dialog=PreparedDialogPolicy(
            self.dialog_policy, binding.view, self.dialog_matcher, guard, prompt))

    def execute(self, inputs, binding, scope, ctx):
        ticket, receipt, request, failure = None, None, None, None
        admission_attempted = False
        try:
            try:
                ctx.ownership.assert_owned()
            except Exception:
                raise TransitionDenied(StopReason.OWNERSHIP_LOST) from None
            position = ctx.position
            # A repeated call returns committed evidence; it never starts an observer/retry.
            if isinstance(position, ActionResultAvailable) and position.operation_id == scope.operation_id:
                record = ctx.memory.operation(position.operation_id)
                private = self.private_refs(inputs)
                if self.dialog_policy and self.dialog_policy.prompt_ref:
                    private = tuple(dict.fromkeys((*private, self.dialog_policy.prompt_ref)))
                if (scope.action_id != self.action_id or scope.parent_workflow_operation_id != ctx.workflow_operation_id
                        or record.intent.semantic_target != self.semantic_target
                        or record.intent.effect_kind != self.effect_kind
                        or record.intent.scope != ctx.checkpoint.current.scope
                        or record.intent.permitted_inputs != self.intent_inputs(inputs)
                        or record.intent.private_inputs != private
                        or record.intent.dialog_policy != self.dialog_policy):
                    raise TransitionDenied(StopReason.INTENT_MISMATCH)
                return from_stored(ctx.memory.load_result(position), position.result_event_id)
            intent = self._intent(inputs, scope, ctx)
            ctx.normal()
            window = self._window(inputs, ctx, scope.operation_id)
            ctx.call_budget(window.input_until).checkpoint()
            admission_attempted = True
            if isinstance(position, InAction):
                record = ctx.memory.operation(position.operation_id)
                ticket = ctx.memory.load_ticket(ctx.workflow_operation_id, position)
                if record.intent != intent:
                    raise TransitionDenied(StopReason.INTENT_MISMATCH)
                ctx.acknowledge(ctx.memory.resume_reserved_attempt(ticket,
                    expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(intent.operation_id)))
            else:
                ticket = ctx.memory.reserve_attempt(
                    intent, position, scope_key=ctx.budget.scope_key.token(),
                    expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(intent.operation_id))
                ctx.acknowledge_ticket(ticket)
            ctx = replace(ctx, active_ticket=ticket)
            # Admission commits before website prerequisites and secret resolution.
            from automation_core.diagnostics.live_faults import after_admission
            after_admission(ctx)
            ctx.call_budget(window.input_until).checkpoint()
            if ctx.clock.monotonic() + self.timing.prepare_s + self.timing.dispatch_s > window.input_until:
                raise BudgetAdmissionDenied("planned_work_does_not_fit")
            if self.request is None and self.provider_request is None and self.precondition is None:
                return self._observe(inputs, binding, ticket, ctx, window, None, possible=False)
            eligible = self._ready_or_result(inputs, binding, ctx, ticket, window)
            if type(eligible) is Evidence:
                if eligible.outcome == Outcome.CONFIRMED and eligible.stop_reason is None:
                    if (not self.allow_satisfied_precondition
                            or ctx.memory.operation(ticket.operation_id).effect_intents_used != 0):
                        raise TransitionDenied(StopReason.IMPLEMENTATION_ERROR)
                return self._finish(eligible, ticket, ctx, None)
            if self.request is not None or self.provider_request is not None:
                factory = self.provider_request or self.request
                try:
                    request = factory(inputs, binding, ctx)
                except ActionPreconditionStop as error:
                    return self._finish(error.evidence, ticket, ctx, None)
                if self.provider_request is None:
                    request = self._prepare_dialog(request, binding, ctx, window)
                    if getattr(request, "kind", None) == "uploadImage":
                        from automation_core.contracts import PrivateAssetRef
                        declared = (getattr(intent.permitted_inputs, "asset_ref", None)
                                    or getattr(intent.permitted_inputs, "creative_asset_ref", None))
                        asset = getattr(request, "asset", None)
                        if (type(declared) is not PrivateAssetRef or asset is None
                                or getattr(asset, "reference", None) != declared):
                            raise TransitionDenied(StopReason.INPUT_UNAVAILABLE)
                else:
                    from automation_core.contracts import ProviderCommandRequest
                    if type(request) is not ProviderCommandRequest or request.inputs != intent.permitted_inputs:
                        raise TransitionDenied(StopReason.INTENT_MISMATCH)
                    if ctx.provider_commands is None:
                        return self._finish(Evidence(Outcome.NOT_RUN, stop_reason=StopReason.NEEDS_INPUT),
                                            ticket, ctx, None)
                ctx = replace(ctx, active_request=request)
                until = self._checking_until(ctx, ticket, window)
                window = replace(window, input_until=min(window.input_until, until))
                ctx.call_budget(window.input_until).checkpoint()
                # Repeatable preparation is a mutation too and always gets an intent.
                ticket = ctx.acknowledge_ticket(ctx.memory.begin_action(ticket,
                    expected_revision=ctx.checkpoint.current.revision,
                    at=ctx.time_checkpoint(ticket.operation_id)))
                ctx = replace(ctx, active_ticket=ticket)
                ctx.call_budget(window.input_until).checkpoint()
                # The declared cost still reserves the saved action bank. The
                # native call is clipped to this checking window; a short
                # control window need not fit its entire dispatch allowance.
                from automation_core.contracts import DispatchReceipt, ProviderReceipt
                # Authority construction is a local pre-dispatch guard. If it
                # fails, no executor call has entered; do not invent uncertainty.
                authority = (self.input_authority(inputs, binding, ctx)
                             if self.input_authority is not None else None)
                dispatch = {}
                witness = (self.native_witness(inputs, binding, ctx, request,
                    window.input_until, window.completion_observe_until) if self.native_witness is not None else None)
                if witness is not None:
                    dispatch = {"native_witness": witness, "dispatch_context": ctx}
                if getattr(ctx.executor, "dispatch_policy", None) is not None:
                    assessment = (self.dispatch_assessment(inputs, binding, ctx, request, window.input_until)
                                  if self.dispatch_assessment is not None else None)
                    dispatch.update(dispatch_assessment=assessment, dispatch_context=ctx)
                try:
                    if self.provider_request is None:
                        from automation_core.browser.interaction import ActionExecutor
                        if isinstance(ctx.executor, ActionExecutor):
                            # input_until already protects observation, recording,
                            # cleanup and following steps. Extras use only the
                            # remainder above this action's original dispatch time.
                            dispatch["required_action_reserve"] = self.timing.dispatch_s
                        scoped_execute = getattr(ctx.executor, "execute_scoped", None)
                        if self.input_authority is None and request.kind == "navigate" and callable(scoped_execute):
                            receipt = scoped_execute(request, binding, window.input_until,
                                operation_id=ticket.operation_id, attempt_id=ticket.attempt_id,
                                destination_id=self.semantic_target, **dispatch)
                        elif self.input_authority is None:
                            receipt = ctx.executor.execute(request, binding, window.input_until, **dispatch)
                        else:
                            receipt = ctx.executor.execute(request, binding, window.input_until,
                                                           blocker_authority=authority, **dispatch)
                        if not isinstance(receipt, DispatchReceipt) or receipt.status not in {"not_attempted", "attempted", "returned"}:
                            raise TypeError("Invalid receipt.")
                    else:
                        if ctx.provider_commands is None:
                            raise TypeError("No provider command port configured.")
                        from automation_core.workflow.provider import ProviderDispatchPermit
                        permit = ProviderDispatchPermit(ticket, request, window.input_until)
                        dispatch_ctx = replace(ctx, provider_dispatch_allowed=True, provider_dispatch_permit=permit)
                        receipt = ctx.provider_commands.dispatch(request, context=dispatch_ctx, deadline=window.input_until,
                            **({"dispatch_assessment": dispatch.get("dispatch_assessment")} if dispatch else {}))
                        validate(receipt)
                        if (type(receipt) is not ProviderReceipt or
                                (receipt.provider_id, receipt.command, receipt.request_id) !=
                                (request.inputs.provider_id, request.inputs.command, request.inputs.request_id)):
                            raise TypeError("Invalid provider receipt.")
                        if permit.possibly_dispatched and not receipt.possibly_dispatched:
                            # A wrapper may lose the first response and return a
                            # later denied duplicate. That cannot erase the effect.
                            receipt = ProviderReceipt("attempted", request.inputs.provider_id,
                                request.inputs.command, request.inputs.request_id, reason="provider_failed")
                except Exception as error:
                    failure = error
                    if self.provider_request is None:
                        receipt = DispatchReceipt("attempted", reason="executor_failed", activation_entered=True)
                    else:
                        receipt = ProviderReceipt("attempted", request.inputs.provider_id,
                            request.inputs.command, request.inputs.request_id, reason="provider_failed")
                if not receipt.possibly_dispatched:
                    return self._finish(Evidence(Outcome.NOT_RUN, stop_reason=stop_reason(receipt.reason)),
                                        ticket, ctx, receipt)
                return self._observe(inputs, binding, ticket, ctx, window, receipt, possible=True, error=failure)
            return self._observe(inputs, binding, ticket, ctx, window, None, possible=False)
        except BudgetAdmissionDenied as error:
            reason = stop_reason(error.reason)
        except TransitionDenied as error:
            reason = error.stop_reason
        except StorageFailure:
            reason = StopReason.STORAGE_UNAVAILABLE
        except Exception as error:
            failure = error
            reason = StopReason.IMPLEMENTATION_ERROR
        finally:
            if self.native_witness is not None and 'witness' in locals() and witness is not None:
                witness.close()
            for value in (getattr(request, "value", None),
                          getattr(request, "private_value", None),
                          getattr(getattr(request, "dialog", None), "prompt_value", None)):
                if isinstance(value, SensitiveValue):
                    value.clear()
            asset = getattr(request, "asset", None)
            if asset is not None and hasattr(asset, "clear"):
                asset.clear()
            ctx.settle()
        if ticket is not None:
            # A failed begin acknowledgement may mean intent committed: never repair by input.
            if not ctx.memory.healthy or reason == StopReason.STORAGE_UNAVAILABLE:
                result = ActionResult(outcome=Outcome.UNKNOWN, operation_id=ticket.operation_id,
                    attempt_id=ticket.attempt_id, stop_reason=reason, receipt=receipt,
                    recording=Recording.UNCONFIRMED)
                ctx.checkpoint.remember_result(result)
                return result
            possible = receipt is not None and receipt.possibly_dispatched
            return self._finish(Evidence(Outcome.UNKNOWN if possible else Outcome.NOT_RUN, stop_reason=reason),
                                ticket, ctx, receipt, error=failure)
        result = ActionResult(outcome=Outcome.NOT_RUN, operation_id=scope.operation_id, stop_reason=reason,
            recording=Recording.UNCONFIRMED if admission_attempted and reason == StopReason.STORAGE_UNAVAILABLE
            else Recording.NOT_REQUIRED)
        ctx.checkpoint.remember_result(result)
        if failure is not None:
            from automation_core.diagnostics.failure_capture import capture_failure
            capture_failure(ctx, result, error=failure)
        return result

    def reconcile(self, inputs, binding, pending, ctx):
        """Read-only resolution, callable only inside a runner-admitted episode."""
        try:
            ctx.ownership.assert_owned()
        except Exception:
            return ActionResult(outcome=Outcome.UNKNOWN, operation_id=pending.operation_id,
                                stop_reason=StopReason.OWNERSHIP_LOST)
        record = ctx.memory.operation(pending.operation_id)
        frame = ctx.checkpoint.current.recovery
        private = self.private_refs(inputs)
        if self.dialog_policy and self.dialog_policy.prompt_ref:
            private = tuple(dict.fromkeys((*private, self.dialog_policy.prompt_ref)))
        if (record is None or record.intent.workflow_id != ctx.workflow_operation_id
                or pending.workflow_id != ctx.workflow_operation_id
                or pending.attempt_id != record.current_attempt_id
                or record.intent.semantic_target != self.semantic_target
                or record.intent.effect_kind != self.effect_kind
                or record.intent.permitted_inputs != self.intent_inputs(inputs)
                or record.intent.private_inputs != private
                or record.intent.dialog_policy != self.dialog_policy):
            return ActionResult(outcome=Outcome.UNKNOWN, operation_id=pending.operation_id,
                                stop_reason=StopReason.INTENT_MISMATCH)
        if (frame is None or ctx.permission.mode != "recovery"
                or frame.root_operation_id != record.intent.root_operation_id):
            return ActionResult(outcome=Outcome.UNKNOWN, operation_id=pending.operation_id,
                                stop_reason=StopReason.NEEDS_INPUT)
        ticket = ctx.memory.ticket_for(pending.operation_id)
        budget = ctx.scopes.budget_for(ticket.scope_key)
        ctx = replace(ctx, budget=ctx.scopes.restore_action(ticket.limits, budget), active_ticket=ticket)
        try:
            return self._observe(inputs, binding, ticket, ctx,
                self._window(inputs, ctx, ticket.operation_id, submission=False),
                None, possible=True, resolution=True)
        finally:
            ctx.settle()


def _same_inputs(inputs, progress):
    return inputs


@dataclass(frozen=True)
class Step:
    action: ActionBlock
    bind_inputs: Callable = _same_inputs
    allocation: BudgetSpec | None = None
    step_id: str | None = None

    @property
    def plan(self):
        return (TimePlan("wrapped", children=(self.action.plan,), allocation=self.allocation)
                if self.allocation else self.action.plan)


@dataclass(frozen=True)
class ActionGroup:
    group_id: str
    start: int
    stop: int
    allocation: BudgetSpec


@dataclass
class StageProgress:
    actions: list[ActionResult] = field(default_factory=list)
    next_index: int = 0
    last_confirmed_action: str | None = None


class PageBlock:
    def __init__(self, stage_id, steps, allocation=None, *, groups=()):
        self.stage_id, self.steps, self.allocation = stage_id, tuple(steps), allocation
        self.groups = tuple(groups)
        self.step_ids = tuple(s.step_id or stage_id + "." + s.action.action_id for s in self.steps)
        if len(set(self.step_ids)) != len(self.steps):
            raise ValueError("Stage step IDs must be unique.")
        last_stop = 0
        for group in self.groups:
            if not (last_stop <= group.start < group.stop <= len(self.steps)):
                raise ValueError("Action groups must be ordered disjoint ranges.")
            last_stop = group.stop
        self.body_plan = TimePlan("sequence", children=tuple(self._range_plans(0, len(self.steps))))
        self.plan = (TimePlan("wrapped", children=(self.body_plan,), allocation=allocation)
                     if allocation else self.body_plan)

    def _range_plans(self, start, stop):
        index = start
        while index < stop:
            group = next((g for g in self.groups if g.start == index and g.stop <= stop), None)
            if group:
                body = TimePlan("sequence", children=tuple(s.plan for s in self.steps[group.start:group.stop]))
                yield TimePlan("wrapped", children=(body,), allocation=group.allocation)
                index = group.stop
            else:
                yield self.steps[index].plan
                index += 1

    def _result(self, progress, *, outcome=None, reason=None):
        last = progress.actions[-1] if progress.actions else None
        if outcome is None:
            outcome = last.outcome if last else Outcome.NOT_RUN
        if outcome == Outcome.NOT_RUN and any(a.outcome == Outcome.CONFIRMED for a in progress.actions):
            outcome = Outcome.UNKNOWN
        return StageResult(outcome=outcome, stop_reason=reason or (last.stop_reason if last else None),
            recording=last.recording if last else Recording.NOT_REQUIRED,
            output=last.output if last else None, actions=tuple(progress.actions),
            last_confirmed_action=progress.last_confirmed_action,
            pending_operation_ids=tuple(a.operation_id for a in progress.actions
                if a.outcome == Outcome.UNKNOWN or a.recording == Recording.UNCONFIRMED))

    def _progress(self, ctx):
        progress = StageProgress()
        for stored in ctx.memory.results_for(ctx.workflow_operation_id):
            record = ctx.memory.operation(stored.operation_id)
            if record.intent.step_id in self.step_ids:
                ref = ctx.memory.result_ref_for(stored.operation_id)
                result = from_stored(stored, ref.result_event_id)
                ctx.checkpoint.remember_result(result)
                progress.actions.append(result)
                if result.can_advance:
                    progress.last_confirmed_action = self.steps[self.step_ids.index(record.intent.step_id)].action.action_id
        return progress

    def consume_saved(self, ctx, *, progress=None):
        """Consume only in-stage successors; LogicBlock owns the last successor."""
        if progress is None:
            progress = self._progress(ctx)
        position = ctx.position
        if isinstance(position, ActionResultAvailable):
            record = ctx.memory.operation(position.operation_id)
            if record.intent.step_id not in self.step_ids:
                return None
            stored = ctx.memory.load_result(position)
            result = from_stored(stored, position.result_event_id)
            index = self.step_ids.index(record.intent.step_id)
            if result.stop_reason is not None and ctx.memory.result_consumption_allowed(position):
                result = replace(result, stop_reason=None)
                progress.actions = [result if a.operation_id == result.operation_id else a for a in progress.actions]
            if not result.can_advance:
                return self._result(progress, reason=result.stop_reason)
            if index + 1 < len(self.steps):
                ctx.advance(position, Ready(self.step_ids[index + 1], record.intent.occurrence_id))
                return None
            return self._result(progress, outcome=Outcome.CONFIRMED)
        if isinstance(position, Ready) and position.step_id in self.step_ids:
            return None
        if isinstance(position, InAction):
            return self._result(progress, outcome=Outcome.UNKNOWN, reason=StopReason.NEEDS_INPUT)
        return self._result(progress, outcome=Outcome.CONFIRMED) if progress.actions else None

    def run(self, inputs, binding, ctx):
        child = ctx.child_path(self.stage_id)
        progress = self._progress(child)
        saved = self.consume_saved(child, progress=progress)
        if saved is not None:
            return saved
        try:
            if self.allocation:
                return TimeSensitive(self.allocation)(child,
                    lambda scoped: self.run_steps(inputs, binding, scoped, start=0,
                        stop=len(self.steps), progress=progress), key=child.scope_key(), plan=self.body_plan)
            return self.run_steps(inputs, binding, child, start=0, stop=len(self.steps), progress=progress)
        except BudgetAdmissionDenied as error:
            return self._result(progress, outcome=Outcome.NOT_RUN, reason=stop_reason(error.reason))
        except StorageFailure:
            return replace(self._result(progress, outcome=Outcome.UNKNOWN,
                           reason=StopReason.STORAGE_UNAVAILABLE), recording=Recording.UNCONFIRMED)
        except TransitionDenied as error:
            return self._result(progress, outcome=Outcome.UNKNOWN, reason=error.stop_reason)
        finally:
            child.settle()

    def run_steps(self, inputs, binding, ctx, *, start, stop, progress, _inside_group=False):
        while True:
            position = ctx.position
            if isinstance(position, ActionResultAvailable):
                saved = self.consume_saved(ctx, progress=progress)
                if saved is not None:
                    return saved
                position = ctx.position
            if not isinstance(position, Ready) or position.step_id not in self.step_ids:
                return self._result(progress, outcome=Outcome.UNKNOWN, reason=StopReason.WORKFLOW_INCOMPATIBLE)
            index = self.step_ids.index(position.step_id)
            if index >= stop:
                return self._result(progress, outcome=Outcome.CONFIRMED)
            if index < start:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            group = None if _inside_group else next((g for g in self.groups if g.start <= index < g.stop), None)
            if group:
                suffix = sum(p.cost_s() for p in self._range_plans(group.stop, stop))
                child = ctx.slot(suffix_s=suffix).child_path(group.group_id)
                body = TimePlan("sequence", children=tuple(s.plan for s in self.steps[group.start:group.stop]))
                result = TimeSensitive(group.allocation)(child, lambda scoped: self.run_steps(
                    inputs, binding, scoped, start=group.start, stop=group.stop,
                    progress=progress, _inside_group=True), key=child.scope_key(), plan=body)
                if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                    return result
                if group.stop == stop:
                    return result
                continue
            step = self.steps[index]
            suffix = sum(p.cost_s() for p in self._range_plans(index + 1, stop))
            child = ctx.slot(suffix_s=suffix).child_path(step.action.action_id)
            scope = child.action_scope(step.action.action_id, self.step_ids[index], position.occurrence_id)
            action_inputs = step.bind_inputs(inputs, progress)
            if step.allocation:
                result = TimeSensitive(step.allocation)(child,
                    lambda scoped: step.action.execute(action_inputs, binding, scope, scoped),
                    key=child.scope_key(position.occurrence_id), plan=step.action.plan)
            else:
                result = step.action.execute(action_inputs, binding, scope, child)
            progress.actions = [a for a in progress.actions if a.operation_id != result.operation_id]
            progress.actions.append(result)
            if not result.can_advance or not ctx.memory.healthy:
                return self._result(progress, reason=result.stop_reason)
            progress.last_confirmed_action = step.action.action_id
            progress.next_index = index + 1
            if index + 1 == len(self.steps):
                return self._result(progress, outcome=Outcome.CONFIRMED)
            ctx.advance(ctx.position, Ready(self.step_ids[index + 1], position.occurrence_id))
            if index + 1 >= stop:
                return self._result(progress, outcome=Outcome.CONFIRMED)


def block_completion_action(block_id, timing=None, *, output_builder=None):
    """An ordinary read-only action aggregating committed child references.

    Child results must already be consumed. Their evidence remains distinct from
    this completion's fresh read-only lifecycle evidence.
    """
    from automation_core.contracts import BlockCompletionOutput
    timing = timing or ActionTiming(0., 0., .01, .01, .01)
    def observe(inputs, binding, ctx, budget):
        budget.checkpoint()
        if type(inputs) not in ctx.memory.completion_input_types or inputs.block_id != block_id:
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        values, evidence = [], []
        for reference in inputs.child_output_refs:
            record = ctx.memory.operation(reference.operation_id)
            if record is None:
                raise TransitionDenied(StopReason.INTENT_MISMATCH)
            owner = next((meta.block_id for meta in ctx.definition.blocks
                          if record.intent.step_id in meta.step_ids), None)
            value = ctx.memory.resolve_output_ref(reference, workflow_id=ctx.workflow_operation_id,
                producer_block_id=owner, producer_step_id=record.intent.step_id,
                output_schema=reference.output_schema)
            values.append(value)
            saved = ctx.memory.load_result(ctx.memory.result_ref_for(reference.operation_id))
            evidence.extend(saved.evidence_refs)
        lifecycle = observed_evidence(ctx, outcome=Outcome.CONFIRMED, state="block_complete")
        proof_refs = tuple(dict.fromkeys((*evidence,
            *(EvidenceRef(item.event_id) for item in lifecycle.evidence))))
        output = (output_builder(inputs, ctx, tuple(values)) if output_builder else
                  BlockCompletionOutput(block_id, inputs.child_output_refs, inputs.successor_block_id,
                      utc_microseconds(ctx.clock.utc_now()), proof_refs))
        if output_builder and hasattr(output, "evidence_refs"):
            output = replace(output, evidence_refs=tuple(dict.fromkeys((*output.evidence_refs, *proof_refs))))
        return replace(lifecycle, output=output)
    return ActionBlock("completion", timing, None, observe,
                       intent_inputs=lambda value: value, semantic_target=block_id + ".completion")


def prerequisite_hold(ctx, *, resource, continuation_id, kind="payment_deferred",
                      challenge_id=None, output_refs=()):
    """Save the exact observed caller/requirement without satisfying or advancing it.

    The consuming page component has established the requirement. This helper
    supplies immutable continuation provenance and performs no prerequisite effect.
    """
    from automation_core.contracts import CallerContinuation, PrerequisiteRequirement
    from automation_core.storage.memory import _payload_refs
    ticket = ctx.active_ticket
    if ticket is None:
        raise TransitionDenied(StopReason.INTENT_MISMATCH)
    intent = ctx.memory.operation(ticket.operation_id).intent
    owner = next((meta.block_id for meta in ctx.definition.blocks if intent.step_id in meta.step_ids), None)
    if owner is None:
        raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
    refs = tuple(dict.fromkeys(ref for _, ref in _payload_refs(intent.permitted_inputs)))
    caller = CallerContinuation(owner, continuation_id, intent.step_id, intent.occurrence_id,
                                intent.input_schema, refs, tuple(output_refs))
    evidence = observed_evidence(ctx, outcome=Outcome.CONFIRMED, state=kind,
                                 stop=StopReason.PREREQUISITE_UNSATISFIED)
    output = PrerequisiteRequirement(kind, resource, intent.scope.expected_account_id,
        intent.operation_id, EvidenceRef(evidence.evidence[0].event_id), challenge_id, caller,
        utc_microseconds(ctx.clock.utc_now()))
    return replace(evidence, output=output)


class RegisteredBlock:
    """A configured sequential LogicBlock for the controlled multi-block harness.

    Business packages with conditional stages implement the same LogicBlock
    contract and register their own owner metadata and ordinary completion action.
    """
    def __init__(self, block_id, pages, entry_requirement, *, input_schema="empty.v1",
                 input_bindings=(), output_bindings=(), successor_block_id=None,
                 final=False, allocation=None, completion_output=None,
                 policy_binding=None, continuity_binding=None, completion_inputs=None):
        from automation_core.contracts import (AdmissionPolicy, BlockDefinition, OutputBinding,
                                StepDefinition, WorkflowDefinition)
        self.block_id, self.pages = block_id, tuple(pages)
        from automation_core.contracts import InputBinding
        if (continuity_binding is not None and policy_binding is None
                or policy_binding is not None and (type(policy_binding) is not InputBinding
                    or policy_binding.input_key != "policy_ref"
                    or policy_binding.output_schema != "ad_account_policy.v1")
                or continuity_binding is not None and (type(continuity_binding) is not InputBinding
                    or continuity_binding.input_key != "continuity_ref"
                    or continuity_binding.output_schema != "business_prefix_continuity.v1")):
            raise ValueError("Exact deferred completion bindings are required.")
        self.policy_binding, self.continuity_binding = policy_binding, continuity_binding
        if completion_inputs is not None and not callable(completion_inputs):
            raise ValueError("A completion input factory must be callable.")
        self.completion_inputs = completion_inputs
        for anchor in (policy_binding, continuity_binding):
            if anchor is not None and anchor not in input_bindings:
                input_bindings = (*input_bindings, anchor)
        self.entry_requirement, self.allocation = entry_requirement, allocation
        if not self.pages or any(not step.startswith(block_id + ".")
                                 for page in self.pages for step in page.step_ids):
            raise ValueError("Registered page steps must use their block namespace.")
        self.child_output_bindings = tuple(output_bindings)
        self.successor_block_id = successor_block_id
        self.completion_action = block_completion_action(block_id, output_builder=completion_output)
        completion_step = block_id + ".completion"
        self.completion = PageBlock(block_id + ".complete", (Step(self.completion_action, step_id=completion_step),))
        self.stages = (*self.pages, self.completion)
        self.plan = TimePlan("sequence", children=tuple(page.plan for page in self.stages))
        flat = tuple((page.step_ids[index], step.action) for page in self.stages
                     for index, step in enumerate(page.steps))
        steps = tuple(StepDefinition(step, action.effect_kind,
            AdmissionPolicy(block_id + "." + action.action_id,
                max_effect_intents=0 if action.effect_kind == EffectKind.READ_ONLY else 1),
            (flat[index + 1][0],) if index + 1 < len(flat) else ())
            for index, (step, action) in enumerate(flat))
        self.definition = WorkflowDefinition(block_id + ".local", 1, steps)
        self.metadata = BlockDefinition(block_id, steps[0].step_id, completion_step,
            tuple(step.step_id for step in steps), input_schema, tuple(input_bindings),
            (*self.child_output_bindings, OutputBinding("completion", "block_completion.v1", completion_step)),
            (successor_block_id,) if successor_block_id else (), final)

    @staticmethod
    def _result(stage):
        return BlockResult(outcome=stage.outcome, stop_reason=stage.stop_reason,
            recording=stage.recording, output=stage.output, stages=(stage,),
            pending_operation_ids=stage.pending_operation_ids)

    def _completion_inputs(self, ctx):
        from automation_core.contracts import BlockCompletionInputs
        owners = tuple(owner for owner in ctx.definition.blocks if owner.block_id == self.block_id)
        if (len(owners) != 1 or ctx.invocations is None
                or ctx.invocations.definition != ctx.definition
                or ctx.invocations.metadata(self.block_id) != owners[0]
                or len(owners[0].allowed_successor_block_ids) > 1):
            raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
        successors = owners[0].allowed_successor_block_ids
        successor = successors[0] if successors else None
        refs = []
        for declaration in self.child_output_bindings:
            matching = tuple(ref for ref in ctx.checkpoint.current.output_refs
                if ref.output_key == declaration.output_key and ref.output_schema == declaration.output_schema
                and ctx.memory.operation(ref.operation_id).intent.step_id == declaration.producer_step_id)
            if len(matching) != 1:
                raise TransitionDenied(StopReason.PREREQUISITE_UNSATISFIED)
            refs.extend(matching)
        child_refs = tuple(refs)
        if self.completion_inputs is not None:
            return self.completion_inputs(ctx, child_refs, successor)
        return BlockCompletionInputs(self.block_id, child_refs, successor)

    def stage_inputs(self, stage, inputs, ctx):
        return self._completion_inputs(ctx) if stage is self.completion else inputs

    def resume_preflight_read(self, ctx):
        cp = ctx.checkpoint.current
        if type(cp.main) is not ActionResultAvailable:
            return False
        action = recovery_action(self, ctx.memory.operation(cp.main.operation_id).intent.step_id)
        if action is None or action.recoverable is None:
            return False
        checkpoint = ctx.memory.retry_action(ctx.workflow_operation_id, action.recoverable,
            expected_revision=cp.revision, at=ctx.time_checkpoint())
        if checkpoint is None:
            return False
        ctx.acknowledge(checkpoint)
        return True

    def resume_reserved_action(self, inputs, ctx):
        """Continue preparation before the original ticket has an effect intent."""
        from automation_core.workflow.failure_handling import HOLD_ERRORS
        cp = ctx.checkpoint.current
        repaired = (type(cp.main) is ActionResultAvailable
            and ctx.memory.action_retry_available(cp.workflow_id, _failed_input_check))
        if (not repaired and type(cp.main) is not InAction
                or cp.recovery is not None or cp.retry_admission is not None):
            return False
        op = ctx.memory.operation(cp.main.operation_id)
        if (not repaired and op.current_phase != 'reserved') or op.effect_intents_used:
            return False
        page = next((p for p in self.stages if op.intent.step_id in p.step_ids), None)
        if page is None:
            return False
        step = page.steps[page.step_ids.index(op.intent.step_id)]
        if step.action.effect_kind is EffectKind.READ_ONLY and op.aggregate_result is None:
            return False  # Existing interrupted-read handling remains the owner.
        supplied = step.bind_inputs(self.stage_inputs(page, inputs, ctx), page._progress(ctx))
        ticket = (ctx.memory.ticket_for(op.operation_id) if repaired else
            ctx.memory.load_ticket(cp.workflow_id, cp.main))
        restored = ctx.scopes.restore_action(ticket.limits, ctx.scopes.budget_for(ticket.scope_key))
        child = replace(ctx.child_path(page.stage_id).child_path(step.action.action_id), budget=restored)
        child.normal()
        try:
            scope = child.action_scope(step.action.action_id, op.intent.step_id, op.intent.occurrence_id)
            if repaired:
                if step.action._intent(supplied, scope, child) != op.intent:
                    return False
                child.acknowledge(ctx.memory.retry_action(cp.workflow_id, _failed_input_check,
                    expected_revision=cp.revision, at=child.time_checkpoint(op.operation_id)))
            entry = ctx.navigator.ensure_entry(self.entry_requirement, child)
            if entry.binding is None:
                if entry.stop_reason is not None and entry.stop_reason.value in HOLD_ERRORS:
                    raise TransitionDenied(entry.stop_reason)
                return False
            step.action.execute(supplied, entry.binding, scope, child)
            return True
        finally:
            child.settle()

    def resume_undispatched_action(self, inputs, ctx):
        """Finish the entered attempt when only optional motion ran before rejection."""
        from automation_core.workflow.failure_handling import HOLD_ERRORS
        cp = ctx.checkpoint.current
        if not ctx.memory.undispatched_restore_available(cp):
            return False
        op = ctx.memory.operation(cp.main.operation_id)
        page = next((p for p in self.stages if op.intent.step_id in p.step_ids), None)
        if page is None:
            return False
        step = page.steps[page.step_ids.index(op.intent.step_id)]
        action = step.action
        if (action.request is None or action.provider_request is not None
                or action.input_authority is not None or action.native_witness is not None or action.dialog_policy):
            return False
        supplied = step.bind_inputs(self.stage_inputs(page, inputs, ctx), page._progress(ctx))
        if (action.semantic_target != op.intent.semantic_target
                or action.intent_inputs(supplied) != op.intent.permitted_inputs
                or action.private_refs(supplied) != op.intent.private_inputs):
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        ticket = ctx.memory.ticket_for(op.operation_id)
        budget = ctx.scopes.restore_action(op.limits, ctx.scopes.budget_for(ticket.scope_key))
        child = replace(ctx.child_path(page.stage_id).child_path(action.action_id),
            budget=budget, active_ticket=ticket, restoring_dispatch=True)
        child.normal()
        admitted = False
        try:
            prepare = getattr(self, 'prepare_dispatch_restore', None)
            if callable(prepare):
                prepare(child)
                if ctx.memory.operation(op.operation_id).aggregate_outcome is Outcome.CONFIRMED:
                    return True
            entry = ctx.navigator.ensure_entry(self.entry_requirement, child)
            if entry.binding is None:
                if entry.stop_reason is not None and entry.stop_reason.value in HOLD_ERRORS:
                    raise TransitionDenied(entry.stop_reason)
                return False
            binding = entry.binding
            window = action._window(supplied, child, op.operation_id)
            eligible = action._ready_or_result(supplied, binding, child, ticket, window)
            satisfied = (type(eligible) is Evidence and eligible.outcome is Outcome.CONFIRMED
                and eligible.stop_reason is None and action.allow_satisfied_precondition)
            if eligible is not True and not satisfied:
                return False
            # Returning to the control is preparation, with its own journal.
            # Admit the original input only after that return has finished,
            # before constructing its protected request and dispatching it.
            child.acknowledge(ctx.memory.admit_undispatched_restore(ticket,
                expected_revision=child.checkpoint.current.revision,
                at=child.time_checkpoint(op.operation_id)))
            admitted = True
            if satisfied:
                action._finish(eligible, ticket, child, None, dispatch_restore=True)
                return True
            request = action.request(supplied, binding, child)
            child = replace(child, active_request=request)
            window = replace(window, input_until=min(window.input_until,
                action._checking_until(child, ticket, window)))
            assessment = (action.dispatch_assessment(supplied, binding, child, request, window.input_until)
                if action.dispatch_assessment else None)
            receipt = child.executor.execute(request, binding, window.input_until,
                dispatch_assessment=assessment, dispatch_context=child,
                required_action_reserve=action.timing.dispatch_s)
            action._observe(supplied, binding, ticket, child, window, receipt,
                possible=receipt.possibly_dispatched, dispatch_restore=True)
            return True
        except ActionPreconditionStop as error:
            if not admitted:
                return False
            from automation_core.contracts import DispatchReceipt
            action._finish(error.evidence, ticket, child,
                DispatchReceipt('not_attempted', reason='stale_view', phase='preparation'),
                dispatch_restore=True, error=error)
            return True
        finally:
            if 'request' in locals() and isinstance(request.value, SensitiveValue):
                request.value.clear()
            child.settle()

    def resume_incomplete_read(self, inputs, ctx):
        """Reobserve an interrupted pure read using its original inputs and limits."""
        cp = ctx.checkpoint.current
        if type(cp.main) not in (InAction, ActionResultAvailable) or cp.recovery is not None:
            return False
        record = ctx.memory.operation(cp.main.operation_id)
        if not ctx.memory.incomplete_read_available(cp, record):
            return False
        page = next((page for page in self.stages if record.intent.step_id in page.step_ids), None)
        if page is None:
            return False
        step = page.steps[page.step_ids.index(record.intent.step_id)]
        action = step.action
        supplied = step.bind_inputs(self.stage_inputs(page, inputs, ctx), page._progress(ctx))
        if (action.effect_kind is not EffectKind.READ_ONLY
                or action.semantic_target != record.intent.semantic_target
                or action.intent_inputs(supplied) != record.intent.permitted_inputs
                or action.private_refs(supplied) != record.intent.private_inputs):
            raise TransitionDenied(StopReason.INTENT_MISMATCH)
        ticket = ctx.memory.ticket_for(record.operation_id)
        restored = ctx.scopes.restore_action(ticket.limits, ctx.scopes.budget_for(ticket.scope_key))
        child = replace(ctx.child_path(page.stage_id).child_path(action.action_id),
            budget=restored, active_ticket=ticket)
        child.normal()
        entry = ctx.navigator.ensure_entry(self.entry_requirement, child)
        if entry.binding is None:
            return False
        child.acknowledge(ctx.memory.resume_incomplete_read(cp.workflow_id, record.operation_id,
            expected_revision=child.checkpoint.current.revision,
            at=child.time_checkpoint(record.operation_id)))
        try:
            action._observe(supplied, entry.binding, ticket, child,
                action._window(supplied, child, record.operation_id, submission=False),
                None, possible=False, read_resolution=type(cp.main) is ActionResultAvailable)
        finally:
            child.settle()
        return True

    def consume_saved(self, inputs, ctx):
        while isinstance(ctx.position, ActionResultAvailable):
            record = ctx.memory.operation(ctx.position.operation_id)
            if record.intent.step_id == self.metadata.completion_step_id:
                return self._result(self.completion.consume_saved(ctx))
            page = next((page for page in self.pages if record.intent.step_id in page.step_ids), None)
            if page is None:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            result = page.consume_saved(ctx)
            if result is None:
                continue
            if result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                return self._result(result)
            index = self.pages.index(page)
            following = self.pages[index + 1] if index + 1 < len(self.pages) else self.completion
            ctx.advance(ctx.position, Ready(following.step_ids[0], record.intent.occurrence_id))
        return None

    def run(self, inputs, entry, ctx):
        while True:
            if isinstance(ctx.position, InAction) and self.resume_reserved_action(inputs, ctx):
                continue
            saved = self.consume_saved(inputs, ctx)
            if saved is not None:
                return saved
            page = next((page for page in self.stages if isinstance(ctx.position, Ready)
                         and ctx.position.step_id in page.step_ids), None)
            if page is None:
                raise TransitionDenied(StopReason.WORKFLOW_INCOMPATIBLE)
            index = self.stages.index(page)
            suffix = sum(later.plan.cost_s() for later in self.stages[index + 1:])
            result = page.run(self.stage_inputs(page, inputs, ctx), entry, ctx.slot(suffix_s=suffix))
            if page is self.completion or result.outcome != Outcome.CONFIRMED or result.stop_reason is not None:
                return self._result(result)
