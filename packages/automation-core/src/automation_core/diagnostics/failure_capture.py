"""Small, read-only failure bundles. No probes, classification, CDP or retries.

HTML is diagnostic material, never authority to dispatch or replay an action.
The local capture timeout is independent of (and never changes) action deadlines.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
from automation_core.paths import workspace_root
import threading
import time
from uuid import uuid4


HTML_SAMPLE = """element => ({html: (document.doctype ?
  new XMLSerializer().serializeToString(document.doctype) + '\\n' : '') +
  document.documentElement.outerHTML, url: location.href, title: document.title,
  timeOrigin: performance.timeOrigin})"""


def default_failure_directory():
    return workspace_root() / '.state' / 'failures'


def _write(path, value):
    data = value.encode('utf-8') if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False).encode('utf-8')
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve() != path.absolute() or path.is_symlink() or not path.is_dir():
        raise OSError('unsafe_capture_directory')
    os.chmod(path, 0o700)


class CaptureError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _error(error):
    """Store causal type/code/locations, never credentials inside exception text."""
    code = getattr(error, 'reason', None) or getattr(error, 'stop_reason', None)
    code = getattr(code, 'value', code)
    frames, tb = [], error.__traceback__
    while tb:
        frames.append(dict(file=Path(tb.tb_frame.f_code.co_filename).name,
            function=tb.tb_frame.f_code.co_name, line=tb.tb_lineno))
        tb = tb.tb_next
    return dict(type=type(error).__name__, code=code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code) else None, frames=frames)


@dataclass(frozen=True)
class FailureContext:
    reason: str | None = None
    kind: str = 'failure'
    failed_step: str | None = None
    last_action: dict | None = None
    revision: int | None = None
    run_id: str | None = None
    pending_operation_ids: tuple = ()
    exception: dict | None = None


def execution_context(ctx, result=None, error=None, *, kind='failure'):
    cp = ctx.checkpoint.current
    last = None
    try:
        from automation_core import contracts as c
        from automation_core.storage.memory import _unpack
        rows = ctx.memory._read('SELECT operation_id FROM operations WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1',
            (cp.workflow_id,))
        if rows:
            active_id = (cp.main.operation_id if isinstance(cp.main, (c.InAction, c.ActionResultAvailable))
                else rows[0][0])
            op = ctx.memory.operation(active_id)
            last = dict(step=op.intent.step_id, operation_id=op.operation_id,
                attempt_id=op.current_attempt_id, phase=op.current_phase,
                outcome=getattr(op.aggregate_outcome, 'value', op.aggregate_outcome),
                effects=op.effect_intents_used, pending=op.pending)
            last['checking_cycle'] = min(2, 1 + ctx.memory.automatic_recoveries_used(op.intent.root_operation_id))
            ref = ctx.memory.result_ref_for(op.operation_id)
            last['observed_states'] = tuple(dict.fromkeys(proof.details.state
                for proof in ctx.memory._proofs(cp, ctx.memory.load_result(ref))
                if type(proof.details) is c.ObservationDetails))[-8:] if ref else ()
            children = [ctx.memory.recovery_workflow_id(op.operation_id)]
            children.extend(value['popup_response_workflow'] for row in ctx.memory._read(
                "SELECT payload_json FROM operation_events WHERE operation_id=? AND kind='resumed' ORDER BY rowid",
                (op.operation_id,)) if 'popup_response_workflow' in (value := _unpack(row[0])))
            recovery_actions = []
            observed_responses = [value for value in ctx.memory.results_for(cp.workflow_id)
                if ctx.memory.operation(value.operation_id).intent.corrective_parent_id == op.operation_id]
            for child in dict.fromkeys(children):
                if ctx.memory.workflow_exists(child):
                    observed_responses.extend(ctx.memory.results_for(child))
            for observed in observed_responses:
                receipt = observed.receipt
                if receipt is not None and receipt.status in ('attempted', 'returned'):
                    recovery_actions.append(dict(step=ctx.memory.operation(observed.operation_id).intent.step_id,
                        phase=receipt.phase, status=receipt.status, reason=receipt.reason))
            last['recovery_actions'] = tuple(recovery_actions)
    except Exception:
        pass  # A broken journal must not prevent preserving the visible page.
    position = cp.recovery.position if cp.recovery else cp.main
    reason = getattr(result, 'stop_reason', None) or getattr(error, 'stop_reason', None) or getattr(error, 'reason', None)
    return FailureContext(getattr(reason, 'value', reason), kind,
        getattr(position, 'step_id', None) or (last['step'] if last else None), last,
        cp.revision, ctx.run_id, cp.pending_operation_ids, _error(error) if error else None)


class FailureCapture:
    """One workflow's append-only HTML bundles and convenient latest pointer."""
    def __init__(self, root=None, *, profile_uuid, workflow_id, clock=None, utc_clock=None,
                 video_provider=None, run_clock=None):
        self.root = (Path(root) if root is not None else default_failure_directory()).resolve()
        if not isinstance(profile_uuid, str) or not re.fullmatch(r'[a-f0-9]{32}', profile_uuid):
            raise ValueError('invalid_capture_identity')
        if not isinstance(workflow_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', workflow_id):
            raise ValueError('invalid_capture_identity')
        self.profile_uuid, self.workflow_id = profile_uuid, workflow_id
        self._thread = threading.get_ident()
        self._latest = None
        self._bundles = []
        self._seen = set()
        self._errors = 0
        self._reason = 'none'
        self._video = None
        self._last_result = None

    def attach(self, handle):
        handle.ownership.assert_owned()
        if handle.binding.profile_uuid != self.profile_uuid:
            raise ValueError('capture_profile_mismatch')
        handle.session.failure_capture = self
        handle.session.failure_page = handle.page

    def bind_recording(self, recorder, *, clock):
        # Recording has its own owner and gates. HTML never depends on it.
        return True

    def public(self):
        return dict(status='enabled', captured=len(self._bundles), recurrent=0, cleaned=0,
            unavailable=self._errors, last_reason=self._reason, format='html_failure.v1', latest=self._latest)

    def record_failure(self, reason='capture_unavailable'):
        self._errors += 1
        self._reason = reason

    def capture_page(self, page, *, ownership, context=None, timeout_s=3.):
        context = context or FailureContext(kind='boundary')
        episode = uuid4().hex
        folder = self.root / self.profile_uuid / self.workflow_id / episode
        def prepare():
            for path in (self.root, self.root / self.profile_uuid, folder.parent, folder):
                _directory(path)
        record = dict(version=1, profile_uuid=self.profile_uuid, workflow_id=self.workflow_id,
            capture_id=episode, captured_at=datetime.now(timezone.utc).isoformat(),
            **asdict(context), html_status='unavailable', coherence='unconfirmed', pages=[], capture_errors=[], video=self._video)
        until = time.monotonic() + min(10., max(0., timeout_s))
        try:
            if threading.get_ident() != self._thread:
                raise CaptureError('capture_wrong_thread')
            ownership.assert_owned()
            prepare()
            if page is None or page.is_closed():
                raise CaptureError('page_unavailable')
            frames = [page.main_frame, *(f for f in page.frames if f is not page.main_frame)]
            for index, frame in enumerate(frames):
                try:
                    ownership.assert_owned()
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise CaptureError('capture_timeout')
                    sample = frame.locator('html').evaluate(HTML_SAMPLE, timeout=max(1, int(remaining * 1000)))
                    if not isinstance(sample, dict) or not isinstance(sample.get('html'), str):
                        raise CaptureError('invalid_html_sample')
                    filename = 'page.html' if index == 0 else f'frame-{index}.html'
                    from automation_core.diagnostics.live_faults import capture_write_fault
                    with capture_write_fault(folder / filename, self, context):
                        _write(folder / filename, sample.pop('html'))
                    record['pages'].append(dict(file=filename, **sample))
                except Exception as error:
                    record['capture_errors'].append(dict(frame=index, **_error(error)))
            if record['pages'] and record['pages'][0]['file'] == 'page.html':
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise CaptureError('capture_timeout')
                identity = page.main_frame.locator('html').evaluate(
                    'element => ({url: location.href, timeOrigin: performance.timeOrigin})',
                    timeout=max(1, int(remaining * 1000)))
                main = record['pages'][0]
                if not isinstance(identity, dict) or any(identity.get(k) != main[k] for k in ('url', 'timeOrigin')):
                    record['coherence'] = 'changed'
                    raise CaptureError('document_changed')
                record['coherence'] = 'same_document'
            record['html_status'] = ('complete' if len(record['pages']) == len(frames) else
                'partial' if record['pages'] else 'unavailable')
        except Exception as error:
            record['capture_errors'].append(_error(error))
            record['html_status'] = 'partial' if record['pages'] else 'unavailable'
        try:
            prepare()
            _write(folder / 'failure.json', record)
            pointer = dict(profile_uuid=self.profile_uuid, workflow_id=self.workflow_id,
                capture_id=episode, directory=str(folder), record=str(folder / 'failure.json'),
                html=str(folder / 'page.html') if any(p['file'] == 'page.html' for p in record['pages']) else None,
                status=record['html_status'])
            _write(self.root / 'latest.json', pointer)
            _write(folder.parent / 'latest.json', pointer)
            self._bundles.append(folder)
            self._latest = pointer
        except Exception:
            self.record_failure('storage_unavailable')
            return dict(status='unavailable', capture_id=episode, reason='storage_unavailable')
        if record['html_status'] != 'complete':
            self.record_failure('capture_unavailable')
        self._last_result = dict(status=record['html_status'], capture_id=episode, coherence=record['coherence'],
            reason='none' if record['html_status'] == 'complete' else 'capture_unavailable', directory=str(folder))
        return self._last_result

    def capture_context(self, ctx, result=None, error=None, *, kind='failure'):
        if (ctx.checkpoint.current.workflow_id != self.workflow_id
                or ctx.checkpoint.current.scope.profile_id != self.profile_uuid):
            self.record_failure('capture_unavailable')
            return
        info = execution_context(ctx, result, error, kind=kind)
        action = info.last_action or {}
        key = (info.run_id, info.kind, action.get('operation_id'), action.get('attempt_id'),
            action.get('phase'), action.get('outcome'), info.failed_step, info.reason)
        if key in self._seen:
            return self._last_result  # One failure may unwind through several layers.
        self._seen.add(key)
        driver = getattr(ctx.executor, 'driver', None)
        page = getattr(driver, 'last_observed_page', None) if driver else None
        if page is None:
            page = getattr(ctx.session, 'failure_page', None)
        return self.capture_page(page, ownership=ctx.ownership, context=info)

    def finalize_video(self, result, *, budget=None):
        self._video = result
        for folder in self._bundles:
            try:
                path = folder / 'failure.json'
                record = json.loads(path.read_text())
                record['video'] = result
                _write(path, record)
            except Exception:
                self.record_failure('storage_unavailable')


def capture_failure(ctx, result=None, error=None, *, kind='failure'):
    collector = None
    try:
        collector = getattr(ctx.session, 'failure_capture', None)
        if isinstance(collector, FailureCapture):
            collector.capture_context(ctx, result, error, kind=kind)
    except Exception:
        try:
            collector.record_failure('capture_unavailable')
        except Exception:
            pass  # Diagnostics must never replace the original action result.


def capture_exception(collector, handle, error, *, ctx=None, fallback_reason='implementation_error'):
    """Preserve failures in owner assembly/cleanup while its page is still owned."""
    if error is None or collector is None:
        return
    try:
        if ctx is not None:
            collector.capture_context(ctx, error=error)
        else:
            detail = _error(error)
            collector.capture_page(handle.page, ownership=handle.ownership,
                context=FailureContext(reason=detail['code'] or fallback_reason,
                    failed_step='application.startup', exception=detail))
    except Exception:
        try:
            collector.record_failure('capture_unavailable')
        except Exception:
            pass
