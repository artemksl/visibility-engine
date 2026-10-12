"""Run a recognized response in the existing executor/journal and caller window."""
from dataclasses import replace
from types import SimpleNamespace

from automation_core import contracts as c
from automation_core.workflow.blocks import PageBlock, RegisteredBlock, Step
from automation_core.workflow.composition import compose_registered_workflow, merge_block_definitions
from automation_core.workflow.known_path_recovery import _cap_to_parent
from automation_core.workflow.runtime import BlockInvocation
from automation_core.workflow.timing import BudgetSpec


def run_popup_response(handler_id, action, binding, ctx, budget):
    # This action is the admitted response itself. Its parent rechecks every
    # handler after completion; the response must not recursively handle itself.
    if action.popup_check is None:
        action.popup_check = lambda *args: None
    owner = SimpleNamespace(ensure_entry=lambda current: c.NavigationResult(
        outcome=c.Outcome.CONFIRMED, binding=binding))
    block = RegisteredBlock('automation.popup.' + handler_id,
        (PageBlock('automation.popup.' + handler_id + '.response', (Step(action),)),),
        owner, final=True)
    return run_popup_block(handler_id, block, ctx, budget)


def run_popup_block(handler_id, block, ctx, budget):
    """Borrow a registered handler, without giving it another recovery cycle."""
    ticket = ctx.active_ticket
    cp, child_id = ctx.memory.popup_response(ticket, handler_id,
        expected_revision=ctx.checkpoint.current.revision, at=ctx.time_checkpoint(ticket.operation_id))
    ctx.acknowledge(cp)
    definition = merge_block_definitions(block.block_id, 1, (block,))
    saved = ctx.memory.load_checkpoint(child_id) if ctx.memory.workflow_exists(child_id) else None
    recorded = next((scope for scope in saved.time_scopes
        if scope.scope_key.component_path == ('run', 'workflow')), None) if saved else None
    allocation = (BudgetSpec(recorded.allowance_s, recorded.normal_fraction) if recorded else
        BudgetSpec(max(.001, budget.until - ctx.clock.monotonic()), .999999))
    app = compose_registered_workflow(session=ctx.session, ownership=ctx.ownership,
        memory=ctx.memory, private_inputs=ctx.private_inputs, definition=definition,
        invocations=(BlockInvocation(block, lambda *args: c.EmptyPayload()),),
        workflow_operation_id=child_id, allocation=allocation, clock=ctx.clock,
        cancellation=ctx.cancellation, driver=ctx.observations, executor=ctx.executor,
        create_workflow=saved is None)
    parent_budget = replace(ctx.budget, normal_until=min(ctx.budget.normal_until, budget.until),
        completion_until=min(ctx.budget.completion_until, budget.until))
    app = _cap_to_parent(app, parent_budget, ctx.memory.operation(ticket.operation_id).limits, ctx.clock)
    app.runner.automatic_recovery = False
    result = app.run()
    return result.outcome == c.Outcome.CONFIRMED and result.stop_reason is None
