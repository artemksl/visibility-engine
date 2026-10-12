"""Opt-in browser disturbances. Recovery receives only ordinary browser evidence."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import re
from urllib.parse import parse_qs, urlsplit


_current = ContextVar('live_fault', default=None)




@contextmanager
def inject_fault(controller):
    token = _current.set(controller)
    try:
        yield
    finally:
        _current.reset(token)


def before_action(driver, request, binding, context, deadline):
    controller = _current.get()
    if controller is not None and not controller.state:
        controller.apply(driver, request, binding, context, deadline)


def after_action(driver, request, context, receipt, deadline):
    controller = _current.get()
    if controller is not None:
        if not controller.state:
            controller.after_action(driver, request, context, receipt, deadline)
        elif controller.spec['kind'] == 'interrupt_recovery':
            controller.interrupt_recovery(driver, context, receipt)
        elif controller.spec['kind'] == 'navigation_new_tab':
            controller.open_destination(driver, context, receipt, deadline)


def after_admission(ctx):
    controller = _current.get()
    if controller is None or controller.state or controller.spec['kind'] != 'interrupt_before_input':
        return
    ticket = ctx.active_ticket
    op = ctx.memory.operation(ticket.operation_id)
    driver = getattr(ctx.executor, 'driver', None)
    if (driver is None or driver._physical_input_guard is None or op.effect_intents_used
            or controller.spec['step'] and controller.spec['step'] != op.intent.step_id):
        return
    driver.assert_owner()
    driver._physical_input_guard()
    controller.record(status='applied', step=op.intent.step_id,
        workflowId=ticket.workflow_id, operationId=ticket.operation_id, attemptId=ticket.attempt_id,
        encounteredAt=datetime.now(timezone.utc).isoformat(),
        encounter=dict(condition='worker_interruption', phase='admitted_before_effect'), nativeEntered=False)
    controller.active_driver = driver
    _capture_interruption(ctx)
    raise SystemExit('one_shot_worker_interruption')


def _capture_interruption(ctx):
    # A controlled worker interruption can preserve its boundary before exit.
    # Abrupt OS/browser death may still make HTML unavailable.
    from automation_core.diagnostics.failure_capture import capture_failure
    from automation_core.browser.interaction import InteractionError
    capture_failure(ctx, error=InteractionError('owner_interrupted'))


def observe_read_failure(ctx, error):
    controller = _current.get()
    ticket = ctx.active_ticket
    if (controller is None or controller.spec['kind'] not in {'disconnect_after_input', 'close_browser_after_navigation'}
            or controller.state.get('status') != 'applied'
            or controller.state.get('encounteredAt') or ticket is None
            or (ticket.workflow_id, ticket.operation_id, ticket.attempt_id) !=
               tuple(controller.state.get(key) for key in ('workflowId', 'operationId', 'attemptId'))):
        return
    try:
        controller.record(encounteredAt=datetime.now(timezone.utc).isoformat(),
            encounter=dict(condition='readback_disconnected', phase='observation',
                errorType=type(error).__name__))
    except Exception:
        controller.state['measurementError'] = 'encounter_not_saved'


@contextmanager
def capture_write_fault(path, capture, context):
    """One real file replacement failure, only in the affected failure bundle."""
    controller = _current.get()
    applies = (controller is not None and controller.spec['kind'] == 'capture_write_failure'
        and controller.state.get('encounteredAt') and not controller.state.get('captureEncounteredAt')
        and context.kind == 'failure' and capture.workflow_id == controller.state.get('workflowId'))
    if not applies:
        yield
        return
    path.mkdir(mode=0o700)  # The capture's new page.html destination only.
    try:
        yield
    except IsADirectoryError:
        controller.record(captureEncounteredAt=datetime.now(timezone.utc).isoformat(),
            captureEncounter=dict(condition='html_write_rejected', phase='os_replace'))
        raise
    finally:
        path.rmdir()


def observe_storage_failure(memory, error):
    controller = _current.get()
    if (controller is None or getattr(controller, 'memory', None) is not memory
            or controller.state.get('encounteredAt')):
        return
    import sqlite3
    expected = {'write_failure_after_input': sqlite3.SQLITE_READONLY,
        'write_lock_after_input': sqlite3.SQLITE_BUSY}.get(controller.spec['kind'])
    if expected is None or getattr(error, 'sqlite_errorcode', None) != expected:
        return
    try:
        controller.record(encounteredAt=datetime.now(timezone.utc).isoformat(),
            encounter=dict(condition='checkpoint_write_rejected', phase='persistence', sqliteCode=expected))
    except Exception:
        controller.state['measurementError'] = 'encounter_not_saved'


def observe_evidence(ctx, evidence):
    controller = _current.get()
    if (controller is None or controller.state.get('encounteredAt')
            or ctx.workflow_operation_id != controller.state.get('workflowId')
            or controller.spec['kind'] not in {'security_screen', 'persistent_overlay', 'capture_write_failure', 'wrong_page',
                                              'access_denied_screen', 'resource_missing_screen'}):
        return
    from automation_core import contracts as c
    wanted = {'security_screen': 'account_security', 'wrong_page': 'current_page_target_changed',
              'access_denied_screen': 'resource_access_denied', 'resource_missing_screen': 'resource_unavailable'}.get(
        controller.spec['kind'], 'unknown_overlay')
    for submission in evidence.evidence:
        detail = submission.evidence.details
        observed = (isinstance(detail, c.BlockerEvidence) and any(
            item.kind == wanted and item.blocking for item in detail.candidates)) or (
            isinstance(detail, c.InterruptionReport) and any(
                item.kind == wanted and item.relation == 'obstructs' for item in detail.conditions)) or (
            isinstance(detail, c.ObservationDetails) and wanted == 'unknown_overlay'
                and detail.state == 'unknown_popup') or (
            isinstance(detail, c.ObservationDetails) and detail.state == wanted
                and wanted in {'current_page_target_changed', 'resource_access_denied', 'resource_unavailable'})
        if observed:
            try:
                controller.record(encounteredAt=datetime.now(timezone.utc).isoformat(),
                    encounter=dict(condition=wanted + '_recognized', phase='observation'))
            except Exception:
                controller.state['measurementError'] = 'encounter_not_saved'
            return


def observe_failure(driver, request, context, error, *, readiness=None):
    """Consume an existing rejection; never probe the browser or affect recovery."""
    controller = _current.get()
    if controller is not None:
        try:
            controller.observe_failure(driver, request, context, error, readiness=readiness)
        except Exception:
            controller.state['measurementError'] = 'encounter_not_saved'


def measurement_outcome(state, job_status):
    if not state.get('encounteredAt'):
        return 'unexercised' if job_status in {'completed', 'held', 'error'} else 'pending'
    return {'completed': 'recovered', 'held': 'held', 'error': 'held'}.get(job_status, 'pending')


def cleanup_fault(driver):
    controller = _current.get()
    if controller is not None:
        controller.cleanup(driver)


