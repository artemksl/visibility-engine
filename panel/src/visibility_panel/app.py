"""Loopback HTTP server for the extracted visibility testing interface."""
from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from visibility_panel.execution.worker_registry import RegistryError
from visibility_panel.visibility_reports.host_job_artifacts import SnapshotReportPreparationError
from visibility_panel.web.jobs import JobStore, jobs_to_dicts, REPO_ROOT
from visibility_panel.web.start_receipts import StartRequestConflict
from visibility_panel.web.record_navigation import RecordNavigationError
from visibility_panel.web.visibility_testing import build_visibility_test_config, preview_visibility_test, visibility_test_options

ASSET_DIR = Path(__file__).parent / 'web'
MAX_BODY_BYTES = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    @property
    def store(self):
        return self.server.store

    def _send_text(self, value, content_type='text/plain; charset=utf-8', status=200):
        encoded = value.encode('utf-8') if isinstance(value, str) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, value, status=200):
        self._send_text(json.dumps(value, ensure_ascii=False), 'application/json; charset=utf-8', status)

    def _read_json_body(self):
        try:
            count = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('Invalid request length.') from None
        if not 0 <= count <= MAX_BODY_BYTES:
            raise ValueError('Request exceeds the local panel size limit.')
        try:
            value = json.loads(self.rfile.read(count)) if count else {}
        except (ValueError, UnicodeDecodeError):
            raise ValueError('Request must contain valid JSON.') from None
        if not isinstance(value, dict):
            raise ValueError('Request must be a JSON object.')
        return value

    def _local_host(self):
        try:
            target = urlparse('http://' + self.headers.get('Host', ''))
            allowed = (target.hostname in {'127.0.0.1', 'localhost'}
                       and (target.port or 80) == self.server.server_port
                       and not target.username and not target.password)
        except ValueError:
            allowed = False
        if not allowed:
            self._send_json({'error': 'Use the loopback panel address.'}, 403)
        return allowed

    def do_GET(self):
        if not self._local_host():
            return
        path = urlparse(self.path).path
        try:
            if path in {'/', '/visibility-testing'}:
                self._send_text((ASSET_DIR / 'templates/visibility_testing.html').read_text(encoding="utf-8"), 'text/html; charset=utf-8')
            elif path == '/health':
                self._send_json({'ok': True})
            elif path.startswith('/static/'):
                name = unquote(path.removeprefix('/static/'))
                if '/' in name or '\\' in name or name.startswith('.'):
                    self._send_json({'error': 'not found'}, 404); return
                file = ASSET_DIR / 'static' / name
                if not file.is_file():
                    self._send_json({'error': 'not found'}, 404); return
                self._send_text(file.read_bytes(), mimetypes.guess_type(name)[0] or 'application/octet-stream')
            elif path == '/api/testing/visibility':
                self._send_json(visibility_test_options(self.store.repo_root))
            elif path == '/api/jobs':
                params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                if params and params != {'scope': ['visibility']}:
                    raise ValueError('Only visibility run history is supported.')
                self._send_json({'jobs': jobs_to_dicts(self.store.list(scope='visibility'))})
            elif path.startswith('/api/jobs/'):
                parts = path.strip('/').split('/')
                job_id = unquote(parts[2])
                if len(parts) == 4 and parts[3] == 'failures':
                    self._send_json({'readout': self.store.failure_readout(job_id)})
                elif len(parts) == 4 and parts[3] == 'log':
                    self._send_text(self.store.log_text(job_id))
                elif len(parts) == 3:
                    self._send_json({'job': jobs_to_dicts([self.store.inspect(job_id)])[0]})
                else:
                    self._send_json({'error': 'not found'}, 404)
            else:
                self._send_json({'error': 'not found'}, 404)
        except KeyError:
            self._send_json({'error': 'job not found'}, 404)
        except (ValueError, RegistryError, SnapshotReportPreparationError) as exc:
            self._send_json({'error': str(exc)}, 409)
        except Exception:
            self._send_json({'error': 'Saved run state is unavailable. Check the local server log.'}, 500)

    def do_POST(self):
        if not self._local_host():
            return
        # A web page on another origin cannot issue browser actions through this panel.
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{self.headers.get("Host")}':
            self._send_json({'error': 'Use the local panel to perform this action.'}, 403); return
        path = urlparse(self.path).path
        try:
            payload = self._read_json_body()
            if path.startswith('/api/testing/visibility/'):
                parts = path.removeprefix('/api/testing/visibility/').split('/')
                if len(parts) not in {1, 2} or (len(parts) == 2 and parts[1] != 'preview'):
                    self._send_json({'error': 'not found'}, 404); return
                if len(parts) == 2:
                    self._send_json(preview_visibility_test(parts[0], payload, self.store.repo_root)); return
                config = build_visibility_test_config(parts[0], payload, self.store.repo_root)
                request_id = config.pop('request_id')
                job = self.store.start('free_audit_stage1', config, request_id=request_id)
                self._send_json({'job': jobs_to_dicts([job])[0], 'status_url': f'/api/jobs/{job.id}',
                                 'readout_url': f'/api/jobs/{job.id}/failures'}, 201)
                return
            parts = path.strip('/').split('/')
            if len(parts) != 4 or parts[:2] != ['api', 'jobs']:
                self._send_json({'error': 'not found'}, 404); return
            job_id, action = unquote(parts[2]), parts[3]
            if action in {'stop', 'cancel'}:
                job = getattr(self.store, action)(job_id, execution_id=payload.get('execution_id'))
            elif action == 'resume':
                job = self.store.resume(job_id)
            elif action == 'refetch-preview':
                self._send_json({'preview': self.store.plan_refetch(job_id, request_id=payload.get('request_id', ''))}); return
            elif action == 'refetch':
                job = self.store.refetch(job_id, payload.get('request', payload))
            elif action == 'finalize':
                job = self.store.finalize_output(job_id, request_id=payload.get('request_id'),
                                                expected_execution_id=payload.get('expected_execution_id'))
            elif action == 'recovery-link':
                self._send_json({'link': self.store.resolve_recovery_link(job_id, payload.get('reference'))}); return
            elif action == 'verification-control':
                if set(payload) != {'episode_id', 'revision', 'execution_id', 'request_id', 'action'}:
                    raise ValueError('Refresh this verification before acting.')
                self._send_json({'outcome': self.store.verification_control(job_id, **payload)}); return
            elif action == 'open-record':
                if set(payload) != {'item_id'} or not isinstance(payload['item_id'], str) or not 1 <= len(payload['item_id']) <= 500:
                    raise ValueError('Choose one saved measurement record.')
                self._send_json({'navigation': self.store.open_record(job_id, item_id=payload['item_id'])}); return
            elif action == 'release-ownership':
                self._send_json({'outcome': self.store.release_stale_ownership(job_id, reason=payload.get('reason', ''))}); return
            elif action == 'inspect':
                job = self.store.inspect(job_id)
            else:
                self._send_json({'error': 'not found'}, 404); return
            response = {'job': jobs_to_dicts([job])[0]}
            accepted = getattr(job, 'accepted_execution_id', None)
            if accepted:
                response['accepted_execution_id'] = accepted
            self._send_json(response)
        except StartRequestConflict as exc:
            self._send_json({'error': str(exc)}, 409)
        except KeyError:
            self._send_json({'error': 'job not found'}, 404)
        except RecordNavigationError as exc:
            self._send_json({'error': str(exc), 'reason': exc.reason}, 409)
        except (ValueError, RegistryError, SnapshotReportPreparationError, RuntimeError) as exc:
            self._send_json({'error': str(exc), 'start_rejected': path.startswith('/api/testing/visibility/')},
                            400 if path.startswith('/api/testing/visibility/') else 409)
        except Exception:
            self._send_json({'error': 'Action did not complete. Check the local server log; retry the same request ID.'}, 500)


def create_server(host='127.0.0.1', port=8766, *, store=None):
    if host not in {'127.0.0.1', 'localhost'}:
        raise ValueError('The visibility panel must bind to a loopback address.')
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store or JobStore()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', 'localhost'])
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port)
    print(f'Visibility testing panel: http://{args.host}:{server.server_port}/visibility-testing#testing', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
