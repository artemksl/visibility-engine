"""Optional owner-local UI notifications; never an execution gate or journal."""
from contextlib import contextmanager
from contextvars import ContextVar

_listener = ContextVar('run_progress_listener', default=None)


@contextmanager
def observe_runs(listener, browser_listener=None):
    token = _listener.set((listener, browser_listener))
    try:
        yield
    finally:
        _listener.reset(token)


def workflow_entered(checkpoint):
    _notify(0, checkpoint.workflow_id, checkpoint.scope)


def browser_opened(profile_id):
    _notify(1, profile_id)


def _notify(index, *args):
    listener = _listener.get()
    if listener is not None and listener[index] is not None:
        try:
            listener[index](*args)
        except Exception:
            # Reporting failure must not interrupt an input or change recovery.
            pass
