import http.client
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_server import create_server  # noqa: E402


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = {name: self.root / name for name in ('alpha', 'beta')}
        for path in self.projects.values():
            path.mkdir()
        self.state = self.root / 'state'
        self.token_path = self.root / 'config' / 'token'
        self.server = create_server(self.projects, self.state, self.token_path, port=0,
                                    public_origin='https://host.example.ts.net:8443')
        self.port = self.server.server_port
        self.token = self.token_path.read_text().strip()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = {'Authorization': 'Bearer ' + self.token} if auth else {}
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        conn.request(method, path, body, h)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def test_auth_host_origin_and_headers(self):
        self.assertEqual(self.request('/api/status', auth=False)[0], 401)
        self.assertEqual(self.request('/api/status', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.request('/api/status', headers={'Origin': 'https://evil.test'})[0], 403)
        self.assertEqual(self.request('/api/status', headers={'Origin': 'null'})[0], 403)
        status, headers, data = self.request('/api/status')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)['version'], '1.5.0')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(os.stat(self.token_path).st_mode & 0o777, 0o600)

    def test_dashboard_assets_are_served_without_exposing_other_files(self):
        root = Path(__file__).resolve().parents[2] / 'work-dashboard'
        for filename, mime in (('model.js', 'text/javascript'),
                               ('motion.js', 'text/javascript'),
                               ('pixels.js', 'text/javascript'),
                               ('visualizer.js', 'text/javascript'),
                               ('visualizer.css', 'text/css')):
            status, headers, body = self.request('/work-dashboard/' + filename, auth=False)
            self.assertEqual(status, 200)
            self.assertTrue(headers['Content-Type'].startswith(mime))
            self.assertEqual(body, (root / filename).read_bytes())
        for suffix in ('../server.py', 'README.md', 'tests/model.test.js'):
            self.assertEqual(self.request('/work-dashboard/' + suffix)[0], 404)
        self.assertEqual(self.request('/work-dashboard/model.js?token=hidden')[0], 404)

    def test_host_access_mobile_deep_link_selects_only_valid_public_identifiers(self):
        request_id = 'a' * 32
        valid = f'/host-access?project=alpha&request_id={request_id}'
        status, headers, body = self.request(valid, auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'id="host-access"', body)
        self.assertIn('text/html', headers['Content-Type'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ('/host-access?project=beta&request_id=bad',
                     f'/host-access?project=missing&request_id={request_id}',
                     f'/host-access?project=alpha&request_id={request_id}&token=secret',
                     f'/host-access?project=alpha&request_id={request_id}&request_id={request_id}',
                     '/host-access', '/host-access?project=alpha&request_id=',
                     f'/host-access?project=alpha%26fake%3D1&request_id={request_id}'):
            self.assertEqual(self.request(path, auth=False)[0], 400, path)

    def test_workroom_frame_is_an_opaque_script_only_document(self):
        status, headers, body = self.request('/workroom-frame.html', auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'id="work-map"', body)
        self.assertNotIn(b'admin.js', body)
        self.assertNotIn(b'host-access-admin.js', body)
        policy = headers['Content-Security-Policy']
        origin = f'http://127.0.0.1:{self.port}'
        for directive in ("default-src 'none'",
                          f"script-src {origin}/work-dashboard/model.js",
                          f"{origin}/workroom-frame.js",
                          f"style-src {origin}/admin.css {origin}/work-dashboard/visualizer.css",
                          "connect-src 'none'", "form-action 'none'",
                          f"frame-ancestors {origin}", "sandbox allow-scripts"):
            self.assertIn(directive, policy)
        self.assertNotIn("'self'", policy)
        self.assertNotIn('allow-same-origin', policy)

        status, headers, script = self.request('/workroom-frame.js', auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/javascript'))
        self.assertNotIn(self.token.encode(), script)

    def test_admin_document_can_embed_only_the_authority_bound_workroom_frame(self):
        status, headers, body = self.request('/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'id="workroom-frame"', body)
        self.assertIn(b'src="/workroom-frame.html"', body)
        self.assertIn(b'sandbox="allow-scripts"', body)
        self.assertIn(b'/workroom-bridge.js', body)
        for asset in (b'/work-dashboard/model.js', b'/work-dashboard/motion.js',
                      b'/work-dashboard/pixels.js', b'/work-dashboard/visualizer.js',
                      b'/work-dashboard/visualizer.css'):
            self.assertNotIn(asset, body)
        policy = headers['Content-Security-Policy']
        origin = f'http://127.0.0.1:{self.port}'
        self.assertIn(f"script-src {origin}/workroom-bridge.js {origin}/admin.js", policy)
        self.assertIn(f"style-src {origin}/admin.css", policy)
        self.assertIn(f"connect-src {origin}", policy)
        self.assertIn(f"frame-src {origin}/workroom-frame.html", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("'self'", policy)

    def test_missing_installed_dashboard_cannot_fall_back_to_sibling_assets(self):
        release = self.root / 'releases' / '1234567890abcdef1234'
        release.mkdir(mode=0o700, parents=True)
        sibling = release.parent / 'work-dashboard'
        sibling.mkdir()
        for name in ('model.js', 'visualizer.js', 'visualizer.css'):
            (sibling / name).write_text('unhashed sibling asset')
        unexpected = None
        try:
            with patch('admin_server.ASSET_ROOT', release):
                with self.assertRaises(ValueError):
                    unexpected = create_server(self.projects, self.state, self.token_path, port=0)
        finally:
            if unexpected:
                unexpected.server_close()

    def test_memory_conflict_and_project_isolation(self):
        path = '/api/memory?project=alpha'
        initial = json.loads(self.request(path)[2])
        self.assertEqual(initial['revision'], 0)
        saved = self.request(path, 'PUT', {'content': '<script>x</script>', 'expected_revision': 0})
        self.assertEqual(saved[0], 200)
        self.assertEqual(self.request(path, 'PUT', {'content': 'stale', 'expected_revision': 0})[0], 409)
        self.assertEqual(json.loads(self.request(path)[2])['content'], '<script>x</script>')
        self.assertEqual(json.loads(self.request('/api/memory?project=beta')[2])['content'], '')
        self.assertEqual(self.request('/api/memory?project=unknown')[0], 400)

    def test_progress_revision_validation_and_isolation(self):
        url = '/api/progress?project=alpha'
        self.assertEqual(json.loads(self.request(url)[2])['status'], 'not_recorded')
        body = {'title': 'UI 작업', 'status': 'in_progress', 'summary': '분석 완료',
                'completed': ['요구사항 확인'], 'next_steps': ['화면 수정'], 'expected_revision': 0}
        self.assertEqual(self.request(url, 'PUT', body)[0], 200)
        self.assertEqual(self.request(url, 'PUT', body)[0], 409)
        self.assertEqual(json.loads(self.request(url)[2])['title'], 'UI 작업')
        self.assertEqual(json.loads(self.request('/api/progress?project=beta')[2])['revision'], 0)
        self.assertEqual(self.request(url, 'PUT', {**body, 'extra': 'bad'})[0], 400)
        self.assertEqual(self.request('/api/progress?project=unknown')[0], 400)
        self.assertEqual(self.request(url, auth=False)[0], 401)

    def test_work_event_ingestion_is_authenticated_scoped_and_reported(self):
        url = '/api/work-events?project=alpha'
        event = {'schema_version': 1, 'source_id': 'figma-cloud', 'source_kind': 'mcp',
                 'actor_id': 'figma', 'actor_kind': 'mcp', 'actor_label': 'Figma',
                 'task_id': 'fetch-1', 'task_kind': 'call', 'task_label': '프레임 조회',
                 'action': 'read', 'state': 'started', 'provenance': 'observed',
                 'arguments': 'private-sentinel'}
        self.assertEqual(self.request(url, 'POST', {'events': [event]}, auth=False)[0], 401)
        self.assertEqual(self.request(url, 'POST', {'events': [event]},
                                      headers={'Origin': 'https://evil.test'})[0], 403)
        response = self.request(url, 'POST', {'events': [event]})
        self.assertEqual(response[0], 200)
        self.assertEqual(json.loads(response[2]), {'project': 'alpha', 'accepted': 1,
                                                  'provenance': 'reported'})
        data = json.loads(self.request('/api/activity?project=alpha')[2])
        self.assertEqual(data['events'], [])
        self.assertEqual(data['records'][0]['actor_label'], 'Figma')
        self.assertEqual(data['records'][0]['provenance'], 'reported')
        self.assertIn('expires_at', data['records'][0])
        self.assertNotIn('private-sentinel', json.dumps(data))
        self.assertEqual(json.loads(self.request('/api/activity?project=beta')[2])['records'], [])
        self.assertEqual(self.request('/api/work-events?project=unknown', 'POST', {'events': [event]})[0], 400)

    def test_invalid_event_batches_cannot_partially_write_or_change_other_state(self):
        url = '/api/work-events?project=alpha'
        event = {'schema_version': 1, 'source_id': 'chat', 'source_kind': 'chatgpt',
                 'actor_id': 'agent', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                 'task_id': 'turn-1', 'task_kind': 'agent', 'task_label': '분석',
                 'action': 'reason', 'state': 'running'}
        for body in ({'events': [event, {**event, 'task_id': '../bad'}]},
                     {'events': [event], 'extra': True}, {'events': []},
                     {'events': [event] * 21}, {'events': [{**event, 'project': 'beta'}]},
                     {'events': [event, {**event, 'actor_label': '\ud800'}]}):
            self.assertEqual(self.request(url, 'POST', body)[0], 400)
        self.assertEqual(json.loads(self.request('/api/activity?project=alpha')[2])['records'], [])
        self.assertEqual(json.loads(self.request('/api/progress?project=alpha')[2])['revision'], 0)

    def initialize_git(self):
        root = self.projects['alpha']
        for args in (['init', '-q'], ['config', 'user.name', 'Fixture'],
                     ['config', 'user.email', 'fixture@example.invalid']):
            subprocess.run(['/usr/bin/git', '-C', str(root), *args], check=True, capture_output=True)
        (root / 'code.txt').write_text('before\n')
        subprocess.run(['/usr/bin/git', '-C', str(root), 'add', 'code.txt'], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                        'commit', '-qm', 'fixture'], check=True)
        (root / 'code.txt').write_text('after\n')
        (root / '.env').write_text('SECRET=never-export\n')

    def test_changes_diff_are_authenticated_read_only_and_scoped(self):
        self.initialize_git()
        index = (self.projects['alpha'] / '.git/index').read_bytes()
        data = json.loads(self.request('/api/changes?project=alpha')[2])
        self.assertTrue(data['available'])
        self.assertEqual([row['path'] for row in data['files']], ['code.txt'])
        diff = json.loads(self.request('/api/diff?project=alpha&path=code.txt&staged=false')[2])
        self.assertIn('+after', diff['text'])
        staged = json.loads(self.request('/api/diff?project=alpha&path=code.txt&staged=true')[2])
        self.assertEqual(staged['text'], '')
        for path in ('../config/token', '.env', '/etc/passwd'):
            self.assertEqual(self.request('/api/diff?project=alpha&path=' + path)[0], 400)
        self.assertEqual(self.request('/api/diff?project=alpha&path=code.txt&staged=bad')[0], 400)
        self.assertEqual(self.request('/api/changes?project=alpha', auth=False)[0], 401)
        self.assertFalse(json.loads(self.request('/api/changes?project=beta')[2])['available'])
        self.assertEqual((self.projects['alpha'] / '.git/index').read_bytes(), index)

    def test_handoff_uses_observed_facts_without_logs_or_command_arguments(self):
        self.initialize_git()
        self.request('/api/progress?project=alpha', 'PUT', {
            'title': 'UI 작업', 'status': 'blocked', 'summary': '테스트 대기',
            'completed': ['분석'], 'next_steps': ['실패 원인 확인'], 'expected_revision': 0})
        self.request('/api/memory?project=alpha', 'PUT', {'content': '결정 사항', 'expected_revision': 0})
        path = self.make_job()
        record = json.loads(path.read_text())
        record.update(state='failed', exit_code=2, preset='typecheck', label='검사')
        path.write_text(json.dumps(record))
        path.with_suffix('.log').write_text('private-log-content')
        path.with_suffix('.log').chmod(0o600)
        before = path.read_bytes()
        status, _, body = self.request('/api/handoff?project=alpha')
        self.assertEqual(status, 200)
        text = json.loads(body)['text']
        for expected in ('UI 작업', '실패 원인 확인', 'typecheck', 'failed', '2',
                         'code.txt', '결정 사항', str(self.projects['alpha']), 'AGENTS.md'):
            self.assertIn(expected, text)
        for hidden in ('private-log-content', 'never-export', self.token, 'fingerprint', 'argv'):
            self.assertNotIn(hidden, text)
        self.assertEqual(path.read_bytes(), before)
        empty = json.loads(self.request('/api/handoff?project=beta')[2])['text']
        self.assertIn('기록된 작업 목표가 없습니다', empty)
        self.assertNotIn('UI 작업', empty)

    def test_recommended_project_comes_from_recent_activity(self):
        from project_state import ProjectState
        ProjectState(self.projects, self.state).record_activity('beta', 'read_file', 'completed', 'fixture')
        self.assertEqual(json.loads(self.request('/api/projects')[2])['recommended_project'], 'beta')

    def test_invalid_job_field_types_are_not_exposed(self):
        path = self.make_job()
        record = json.loads(path.read_text())
        record['label'] = {'untrusted': 'object'}
        path.write_text(json.dumps(record))
        self.assertEqual(json.loads(self.request('/api/jobs?project=alpha')[2])['jobs'], [])

    def test_invalid_and_oversized_requests(self):
        path = '/api/memory?project=alpha'
        self.assertEqual(self.request(path, 'PUT', '{')[0], 400)
        self.assertEqual(self.request(path, 'PUT', 'x' * (200 * 1024 + 1))[0], 413)
        self.assertEqual(self.request(path, 'PUT', {'content': 'a' * 32769, 'expected_revision': 0})[0], 400)
        self.assertEqual(self.request('/api/memory?project=alpha&project=beta')[0], 400)
        self.assertEqual(self.request('/api/execute', 'POST')[0], 405)
        self.assertEqual(self.request('/../server.py')[0], 404)

    def make_job(self, job_id='a' * 32):
        jobs = self.state / 'jobs'
        jobs.mkdir(mode=0o700, exist_ok=True)
        record = {'job_id': job_id, 'project': 'alpha', 'state': 'running',
                  'created_at': '2026-10-09T00:00:00Z', 'exit_code': None,
                  'log_truncated': False, 'fingerprint': 'private', 'argv': ['private']}
        target = jobs / (job_id + '.json')
        target.write_text(json.dumps(record))
        target.chmod(0o600)
        return target

    def test_jobs_are_read_only_and_logs_are_scoped(self):
        path = self.make_job()
        before = path.read_bytes()
        response = self.request('/api/jobs?project=alpha')
        self.assertEqual(response[0], 200)
        job = json.loads(response[2])['jobs'][0]
        self.assertEqual(job['state'], 'running')
        self.assertNotIn('argv', job)
        self.assertNotIn('fingerprint', job)
        url = '/api/log?project=alpha&job_id=' + path.stem
        self.assertEqual(json.loads(self.request(url)[2])['text'], '')
        log = path.with_suffix('.log')
        log.write_bytes(b'first\nsecond\n')
        log.chmod(0o600)
        output = json.loads(self.request(url + '&offset=6')[2])
        self.assertEqual(output['text'], 'second\n')
        self.assertEqual(output['next_offset'], 13)
        self.assertFalse(output['complete'])
        self.assertEqual(self.request(url.replace('alpha', 'beta'))[0], 400)
        self.assertEqual(self.request(url + '&offset=-1')[0], 400)
        self.assertEqual(path.read_bytes(), before)
        log.unlink()
        log.symlink_to(self.token_path)
        self.assertEqual(self.request(url)[0], 400)
        self.assertNotIn(self.token.encode(), self.request(url)[2])

    def test_job_action_is_derived_from_private_argv_without_exporting_command_data(self):
        path = self.make_job()
        record = json.loads(path.read_text())
        record.update(label='직접 명령 실행', argv=['npm', 'run', 'build', '--', '--token=secret-value'],
                      args={'raw': 'private-args'}, env={'TOKEN': 'private-env'})
        path.write_text(json.dumps(record))
        before = path.read_bytes()

        status, _, body = self.request('/api/jobs?project=alpha')

        self.assertEqual(status, 200)
        job = json.loads(body)['jobs'][0]
        self.assertEqual(job['label'], '직접 명령 실행')
        self.assertEqual(job['action'], 'build')
        for hidden in (b'argv', b'args', b'env', b'secret-value', b'private-args', b'private-env'):
            self.assertNotIn(hidden, body)
        self.assertEqual(path.read_bytes(), before)

    def test_job_actions_recognize_bounded_program_and_subcommand_forms(self):
        cases = [
            (['npm', '--prefix', 'web', 'run', 'build'], 'build'),
            (['pnpm', '--workspace', 'api', 'run', 'typecheck'], 'test'),
            (['yarn', '-w', 'web', 'run', 'lint'], 'test'),
            (['bun', '--cwd', 'web', 'run', 'e2e'], 'test'),
            (['npm', 'test'], 'test'),
            (['npx', 'tsc', '--noEmit'], 'test'),
            (['npx', 'vitest', 'run'], 'test'),
            (['npx', 'jest'], 'test'),
            (['npx', 'playwright', 'test'], 'test'),
            (['npx', '--yes', 'playwright', 'test'], 'test'),
            (['npx', '-y', 'tsc', '--noEmit'], 'test'),
            (['tsc', '--noEmit'], 'test'),
            (['pytest', '-q'], 'test'),
            (['python3', '-m', 'unittest'], 'test'),
            (['git', 'status', '--short'], 'review'),
            (['git', 'diff', '--stat'], 'review'),
            (['git', 'commit', '-m', 'message'], 'commit'),
            (['git', 'push', 'origin', 'main'], 'push'),
            (['git', 'commit', '--dry-run'], 'review'),
            (['git', 'push', '--dry-run'], 'review'),
        ]
        expected = {}
        for index, (argv, action) in enumerate(cases, 1):
            job_id = f'{index:032x}'
            path = self.make_job(job_id)
            record = json.loads(path.read_text())
            record.update(label='직접 명령 실행', argv=argv)
            path.write_text(json.dumps(record))
            expected[job_id] = action

        jobs = json.loads(self.request('/api/jobs?project=alpha')[2])['jobs']

        self.assertEqual({job['job_id']: job.get('action') for job in jobs}, expected)

    def test_job_action_treats_shell_code_and_unknown_scripts_as_opaque_execution(self):
        cases = [
            ['bash', '-c', 'npm run build && git push'],
            ['env', 'SECRET=npm run test', 'deploy'],
            ['npm', 'run', 'contest'],
            ['npm', 'run', 'deploy'],
        ]
        expected_ids = []
        for index, argv in enumerate(cases, 101):
            job_id = f'{index:032x}'
            path = self.make_job(job_id)
            record = json.loads(path.read_text())
            record.update(label='직접 명령 실행', argv=argv)
            path.write_text(json.dumps(record))
            expected_ids.append(job_id)

        jobs = {job['job_id']: job for job in
                json.loads(self.request('/api/jobs?project=alpha')[2])['jobs']}

        self.assertEqual([jobs[job_id].get('action') for job_id in expected_ids],
                         ['execute'] * len(expected_ids))

    def test_missing_or_malformed_private_argv_omits_job_action(self):
        malformed = [None, [], 'npm run build', ['npm', 7, 'build']]
        expected_ids = []
        for index, argv in enumerate(malformed, 201):
            job_id = f'{index:032x}'
            path = self.make_job(job_id)
            record = json.loads(path.read_text())
            if argv is None:
                record.pop('argv', None)
            else:
                record['argv'] = argv
            path.write_text(json.dumps(record))
            expected_ids.append(job_id)

        jobs = {job['job_id']: job for job in
                json.loads(self.request('/api/jobs?project=alpha')[2])['jobs']}

        self.assertTrue(all('action' not in jobs[job_id] for job_id in expected_ids))

    def test_unsafe_job_state_is_not_exposed(self):
        target = self.make_job()
        target.chmod(0o644)
        self.assertEqual(json.loads(self.request('/api/jobs?project=alpha')[2])['jobs'], [])
        target.unlink()
        target.symlink_to(self.token_path)
        self.assertEqual(json.loads(self.request('/api/jobs?project=alpha')[2])['jobs'], [])

    def test_browser_session_login_resume_and_logout(self):
        status, headers, _ = self.request('/api/session', 'POST', {'remember': True})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Max-Age=2592000', cookie)
        self.assertNotIn(self.token, cookie)
        auth = {'Cookie': cookie.split(';')[0]}
        self.assertEqual(self.request('/api/status', headers=auth, auth=False)[0], 200)
        self.assertEqual(self.request('/api/memory?project=alpha', 'PUT',
                         {'content': 'session note', 'expected_revision': 0}, headers=auth, auth=False)[0], 200)
        hostile = {**auth, 'Origin': 'http://evil.test'}
        self.assertEqual(self.request('/api/memory?project=alpha', 'PUT',
                         {'content': 'evil', 'expected_revision': 1}, headers=hostile, auth=False)[0], 403)
        tampered = {'Cookie': auth['Cookie'] + 'x'}
        self.assertEqual(self.request('/api/status', headers=tampered, auth=False)[0], 401)
        logout = self.request('/api/session', 'DELETE', headers=auth, auth=False)
        self.assertEqual(logout[0], 200)
        self.assertIn('Max-Age=0', logout[1]['Set-Cookie'])

    def test_session_requires_token_and_valid_remember_flag(self):
        self.assertEqual(self.request('/api/session', 'POST', {'remember': True}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/session', 'POST', {'remember': 'yes'})[0], 400)
        status, headers, _ = self.request('/api/session', 'POST', {'remember': False})
        self.assertEqual(status, 200)
        self.assertNotIn('Max-Age', headers['Set-Cookie'])
        self.assertEqual(self.request('/api/session', 'POST', {'remember': True},
                         headers={'Cookie': headers['Set-Cookie'].split(';')[0]}, auth=False)[0], 401)

    def test_expired_session_is_rejected(self):
        from unittest.mock import patch
        with patch('admin_server.time.time', return_value=time.time() - 31 * 86400):
            _, headers, _ = self.request('/api/session', 'POST', {'remember': True})
        self.assertEqual(self.request('/api/status', headers={
            'Cookie': headers['Set-Cookie'].split(';')[0]}, auth=False)[0], 401)

    def test_exact_https_proxy_origin_and_secure_cookie(self):
        headers = {'Host': 'host.example.ts.net:8443', 'Origin': 'https://host.example.ts.net:8443'}
        self.assertEqual(self.request('/api/status', headers=headers)[0], 200)
        result = self.request('/api/session', 'POST', {'remember': True}, headers=headers)
        self.assertEqual(result[0], 200)
        self.assertIn('; Secure', result[1]['Set-Cookie'])
        bad = {**headers, 'Origin': 'http://host.example.ts.net:8443'}
        self.assertEqual(self.request('/api/status', headers=bad)[0], 403)
        self.assertEqual(self.request('/api/status', headers={'Host': 'host.example.ts.net:443'})[0], 403)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_lan_binding_is_explicit_and_host_scoped(self):
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        from admin_server import validate_lan_host
        for invalid in ('0.0.0.0', '::', '8.8.8.8', 'localhost', '127.0.0.1', '169.254.1.2'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_lan_host(invalid)
        self.assertEqual(validate_lan_host('192.168.2.70'), '192.168.2.70')
        # Keep the test portable: bind the actual test socket on loopback but
        # exercise the LAN handler and assert the requested socket address.
        with patch('admin_server.ThreadingHTTPServer', side_effect=lambda addr, handler:
                   ThreadingHTTPServer(('127.0.0.1', 0), handler)) as factory:
            lan = create_server(self.projects, self.state, self.token_path, port=8765,
                                bind_host='192.168.2.70')
        self.assertEqual(factory.call_args.args[0], ('192.168.2.70', 8765))
        thread = threading.Thread(target=lan.serve_forever, daemon=True)
        thread.start()
        try:
            host = f'192.168.2.70:{lan.server_port}'
            conn = http.client.HTTPConnection('127.0.0.1', lan.server_port, timeout=5)
            conn.request('GET', '/api/status', headers={'Host': host, 'Origin': 'http://' + host,
                         'Authorization': 'Bearer ' + self.token})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', lan.server_port, timeout=5)
            conn.request('GET', '/api/status', headers={'Host': f'192.168.1.2:{lan.server_port}',
                         'Authorization': 'Bearer ' + self.token})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
        finally:
            lan.shutdown()
            lan.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
