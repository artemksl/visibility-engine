"""Offline startup, planning and controls. No Chrome or provider input."""
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from visibility_panel.app import create_server
from visibility_panel.web.config import load_settings, selected_snapshot_profile_rows
from visibility_panel.web.jobs import Job, JobStore, RUNNER_FREE_AUDIT_WORKFLOW
from visibility_panel.web.visibility_testing import build_visibility_test_config
from visibility_panel.execution.worker_registry import WorkerRegistry

ROOT = Path(__file__).resolve().parents[2]


def configure(root, ids=('profile_1', 'profile_2')):
    config = root / 'config'; config.mkdir()
    (config / 'profiles.json').write_text(json.dumps([
        {'id': item, 'display_name': item, 'chrome_directory': f'Profile {index}', 'project_url': ''}
        for index, item in enumerate(ids, 1)]))
    (config / 'settings.json').write_text(json.dumps({'selected_profile_ids': list(ids)}))


def test_local_profile_selection_is_explicit_and_exact(tmp_path):
    configure(tmp_path)
    assert load_settings(tmp_path)['tabs_per_provider'] == 2
    assert [row['id'] for row in selected_snapshot_profile_rows(['profile_2'], tmp_path)] == ['profile_2']
    with pytest.raises(ValueError): selected_snapshot_profile_rows([], tmp_path)
    with pytest.raises(ValueError): selected_snapshot_profile_rows(['unknown'], tmp_path)


def test_missing_configuration_has_no_default_selection(tmp_path):
    with pytest.raises(ValueError, match='settings'): load_settings(tmp_path)


def test_diagnostic_start_freezes_plan_without_running_a_provider(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(ROOT / 'panel/src'))
    workflow = replace(RUNNER_FREE_AUDIT_WORKFLOW,
        command=[sys.executable, str(ROOT / 'scripts/run_visibility_measurement.py')])
    store = JobStore(tmp_path, workflows={workflow.id: workflow})
    monkeypatch.setattr(store, '_run_job', lambda *args: None)
    config = build_visibility_test_config('chatgpt', {'request_id': 'test-request-123', 'prompts': ['Prompt A', 'Prompt A', 'Prompt B']}, tmp_path)
    request_id = config.pop('request_id')
    first = store.start(workflow.id, config, request_id=request_id)
    second = store.start(workflow.id, config, request_id=request_id)
    assert first.id == second.id
    assert len(store.list()) == 1
    directory = store.run_root / first.id
    runtime = json.loads((directory / 'runtime.json').read_text())
    measurement = json.loads((directory / 'measurement.json').read_text())
    assert measurement['diagnostic_prompts'] == ['Prompt A', 'Prompt A', 'Prompt B']
    assert runtime['max_concurrency'] == 4
    assert runtime['tabs_per_provider'] == 2
    assert runtime['packet_start_stagger_seconds'] == 10
    assert runtime['retry_policy']['max_attempts'] == 2
    assert runtime['retry_policy']['response_timeouts']['chatgpt_turn_1_seconds'] == 240
    registry = WorkerRegistry(directory)
    start = registry.request(first.run_config['runner_execution_id'])
    assert 'auto_recovery_rounds' not in start['bindings']
    recover_config = dict(first.run_config)
    recover = store._prepare_execution(recover_config, first.id, registry,
        mode='resume', request_id='resume-request-123', expected_execution_id=start['execution_id'])
    assert recover['bindings']['auto_recovery_rounds'] == 5
    assert recover['bindings']['browser_restart_on_start'] is False


class FakeStore:
    def __init__(self, root):
        self.repo_root = root; self.calls = []
        self.job = Job('run-a', 'free_audit_stage1', 'Visibility test', [], 'needs_attention', 2,
                       '2026-01-01', None, None, 'log.txt', {'runner_owned_snapshot': True})
    def list(self, **kwargs): return [self.job]
    def inspect(self, job_id): return self.job
    def log_text(self, job_id): return 'saved log'
    def failure_readout(self, job_id): return {'run_id': job_id, 'items': [], 'host': {}}
    def start(self, *args, **kwargs): self.calls.append(('start', args, kwargs)); return self.job
    def stop(self, job_id, **kwargs): self.calls.append(('stop', job_id, kwargs)); return self.job
    def resume(self, job_id): self.calls.append(('resume', job_id)); return self.job
    def plan_refetch(self, job_id, **kwargs): self.calls.append(('preview', job_id, kwargs)); return {'run_id': job_id}
    def refetch(self, job_id, request): self.calls.append(('refetch', job_id, request)); return self.job


@pytest.fixture
def panel(tmp_path):
    configure(tmp_path)
    store = FakeStore(tmp_path)
    server = create_server(port=0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', store
    finally: server.shutdown(); server.server_close(); thread.join()


def fetch(base, path, body=None, **headers):
    request = Request(base + path, None if body is None else json.dumps(body).encode(),
                      {'Content-Type': 'application/json', **headers})
    with urlopen(request) as response:
        text = response.read().decode()
        return response.status, json.loads(text) if response.headers['Content-Type'].startswith('application/json') else text


def test_panel_load_and_preview_are_read_only(panel):
    base, store = panel
    assert fetch(base, '/health')[1] == {'ok': True}
    html = fetch(base, '/visibility-testing')[1]
    assert 'visibilityTestForm' in html and 'Client research' not in html
    assert fetch(base, '/api/jobs?scope=visibility')[1]['jobs'][0]['id'] == 'run-a'
    preview = fetch(base, '/api/testing/visibility/chatgpt/preview', {'request_id': 'preview-request-123', 'prompt_count': 15})[1]
    assert preview['prompt_count'] == 15
    assert preview['selected_profile_ids'] == ['profile_1', 'profile_2']
    assert preview['provider_requests_sent'] == 0
    assert store.calls == []


def test_controls_issue_one_correlated_request_each(panel):
    base, store = panel
    assert fetch(base, '/api/jobs/run-a/resume', {})[0] == 200
    assert fetch(base, '/api/jobs/run-a/stop', {'execution_id': 'execution-a'})[0] == 200
    assert fetch(base, '/api/jobs/run-a/refetch-preview', {'request_id': 'retry-123'})[0] == 200
    assert fetch(base, '/api/jobs/run-a/refetch', {'request': {'request_id': 'retry-123'}})[0] == 200
    assert store.calls == [('resume', 'run-a'), ('stop', 'run-a', {'execution_id': 'execution-a'}),
                           ('preview', 'run-a', {'request_id': 'retry-123'}),
                           ('refetch', 'run-a', {'request_id': 'retry-123'})]


def test_cross_origin_actions_and_static_traversal_are_refused(panel):
    base, store = panel
    with pytest.raises(HTTPError) as exc:
        fetch(base, '/api/jobs/run-a/resume', {}, Origin='https://another.example')
    assert exc.value.code == 403
    with pytest.raises(HTTPError) as exc:
        fetch(base, '/static/%2e%2e%2fconfig%2fsettings.json')
    assert exc.value.code == 404
    assert store.calls == []


def test_public_bind_is_refused():
    with pytest.raises(ValueError, match='loopback'): create_server('0.0.0.0', 0)


def test_record_inspection_uses_the_frozen_custom_chrome_directory(tmp_path, monkeypatch):
    from visibility_panel.web.record_navigation import _browser_paths
    frozen = tmp_path / 'automation-chrome'
    monkeypatch.setenv('CHROME_USER_DATA_DIR', str(tmp_path / 'unrelated-chrome'))
    _, actual = _browser_paths(tmp_path, {'browser': {'user_data_dir': str(frozen)}})
    assert actual == frozen


def test_foreign_host_cannot_rebind_the_loopback_action_endpoint(panel):
    base, store = panel
    with pytest.raises(HTTPError) as exc:
        fetch(base, '/api/jobs/run-a/resume', {}, Host='another.example:' + base.rsplit(':', 1)[1])
    assert exc.value.code == 403
    assert store.calls == []
