"""Browser checks against the real loopback HTTP API and isolated state."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from admin_server import APPROVAL_BODIES, create_server  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_operation_fixtures import fake_registered_manager, write_registered_fixture  # noqa: E402
from host_process import HostCommandError  # noqa: E402
from host_registered_operations import RegisteredHostOperations  # noqa: E402
from host_registry import HostTargetRegistry  # noqa: E402
from host_runtime_config import PROJECT, TARGET, HostRuntimeError  # noqa: E402
from project_state import ProjectState  # noqa: E402


def unconfigured_registered(root):
    """Registered-operation manager on private temporary paths with no host config: the
    page never reads the real host configuration or state."""
    return lambda projects, state_dir=None: RegisteredHostOperations(
        projects, HostTargetRegistry(projects, root / 'no-host-runtime.json'),
        state_dir=state_dir or root / 'registered-state')


class ObservedHostExecution:
    """Disposable HTTP-boundary data; never starts a process or uses host state."""

    def __init__(self, projects, state_dir, access):
        self.projects = projects
        self.state_root = state_dir
        self.access = access
        self.rows = []

    def list(self, project):
        return {'project': project, 'executions': copy.deepcopy([
            row for row in self.rows if row['project'] == project])}

    def get(self, project, operation_id):
        for row in self.rows:
            if row['project'] == project and row['operation_id'] == operation_id:
                return copy.deepcopy(row)
        raise HostRuntimeError('HOST_EXECUTION_NOT_FOUND')

    def read_log(self, project, operation_id, *, stream='stdout', cursor=0):
        row = self.get(project, operation_id)
        if row['kind'] == 'codex':
            raise HostRuntimeError('HOST_AGENT_LOG_RESTRICTED')
        text = 'fixture host output <script>window.host_pwned=1</script>\n'
        return {'project': project, 'operation_id': operation_id, 'stream': stream,
                'text': text[cursor:], 'next_cursor': len(text), 'eof': True}

    def raw_log_path(self, project, operation_id, stream):
        self.get(project, operation_id)
        return self.state_root / (operation_id + '.' + stream + '.jsonl')


def observed_host_row(project, operation_id, *, kind='command', state='running'):
    """Public HostExecution response shape with an intentionally old start time."""
    started = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
    row = {'project': project, 'operation_id': operation_id, 'kind': kind,
           'state': state, 'phase': 'EXECUTE', 'code': None,
           'created_at': started, 'started_at': started, 'finished_at': None,
           'result': None, 'termination_verified': False,
           'authorization': {'kind': 'host_access', 'grant_id': 'd' * 32,
                             'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}
    if kind == 'codex':
        row['agent_metadata'] = {'profile_id': 'codex-review', 'model': 'fixture-codex',
                                 'reasoning_effort': 'high', 'profile_digest': 'e' * 64,
                                 'reason': 'WORKFLOW_COMPLEXITY'}
    return row


class AdminBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        projects = {name: self.root / name for name in ('alpha', 'beta')}
        for path in projects.values():
            path.mkdir()
        self.projects = projects
        for args in (['init', '-q'], ['config', 'user.name', 'Fixture'],
                     ['config', 'user.email', 'fixture@example.invalid']):
            subprocess.run(['/usr/bin/git', '-C', str(projects['alpha']), *args], check=True, capture_output=True)
        (projects['alpha'] / 'code.txt').write_text('before\n')
        subprocess.run(['/usr/bin/git', '-C', str(projects['alpha']), 'add', 'code.txt'], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(projects['alpha']), '-c', 'core.hooksPath=/dev/null',
                        'commit', '-qm', 'fixture'], check=True)
        (projects['alpha'] / 'code.txt').write_text('after <script>window.pwned=1</script>\n')
        (projects['alpha'] / '.env').write_text('SECRET=not-for-preview\n')
        state = self.root / 'state'
        self.store = ProjectState(projects, state)
        self.store.update_memory('alpha', '<script>window.pwned=1</script>', 0)
        self.store.update_memory('beta', 'beta memory', 0)
        self.store.update_progress('alpha', '화면 작업', 'in_progress', '요구사항 분석 완료',
                                   ['화면 분석'], ['타입 검사'], 0)
        self.store.record_activity('alpha', 'read_file', 'completed', 'ui-fixture')
        jobs = state / 'jobs'
        jobs.mkdir(mode=0o700)
        self.job_id = 'a' * 32
        record = {'job_id': self.job_id, 'project': 'alpha', 'state': 'succeeded',
                  'created_at': '2026-10-09T00:00:00Z', 'exit_code': 0,
                  'preset': 'typecheck',
                  'label': '<img src=x onerror="window.pwned=1">', 'log_truncated': False}
        job = jobs / (self.job_id + '.json')
        job.write_text(json.dumps(record))
        job.chmod(0o600)
        self.log = job.with_suffix('.log')
        self.log.write_text('start\n<img src=x onerror="window.pwned=1">\n')
        self.log.chmod(0o600)
        token = self.root / 'config' / 'token'
        self.host_access = HostAccess(projects, state, trusted_runtime=True)
        self.host_execution = ObservedHostExecution(projects, state, self.host_access)
        self.server = create_server(projects, state, token, port=0,
                                    registered_ops_factory=unconfigured_registered(self.root),
                                    host_access_factory=lambda *_a, **_k: self.host_access,
                                    host_execution_factory=lambda *_a, **_k: self.host_execution)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/#token={token.read_text().strip()}')
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#memory')).to_have_value('<script>window.pwned=1</script>')
        self.room = self.page.frame_locator('#workroom-frame')
        expect(self.room.locator('#wd-status')).to_be_visible()
        self.room_page = self.page.locator('#workroom-frame').element_handle().content_frame()
        self.page.locator('#execution-details').evaluate('(element) => element.open = true')
        self.page.locator('#notes-details').evaluate('(element) => element.open = true')

    def show_workroom_camera(self):
        # Reveal the outer frame before the child camera's stability/scroll check.
        self.page.locator('#workroom-frame').scroll_into_view_if_needed()
        self.room.locator('#wd-camera').scroll_into_view_if_needed()

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def test_invalid_approval_link_has_a_safe_readable_recovery_page(self):
        response = self.page.goto(
            f'http://127.0.0.1:{self.server.server_port}/host-operations?token=private-link-value')
        self.assertEqual(response.status, 400)
        expect(self.page.get_by_role('heading', name='승인 링크를 확인해 주세요')).to_be_visible()
        expect(self.page.get_by_role('link', name='관리 화면 열기')).to_have_attribute('href', '/')
        expect(self.page.locator('body')).not_to_contain_text('private-link-value')
        expect(self.page.get_by_role('button', name='승인', exact=True)).to_have_count(0)
        for width in (1440, 390, 320):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        self.page.get_by_role('link', name='관리 화면 열기').click()
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')

    def test_mobile_host_access_link_prefocuses_request_without_auto_approval(self):
        first = self.host_access.request('beta')
        requested = self.host_access.request('beta')
        port = self.server.server_port
        self.page.goto(f'http://127.0.0.1:{port}/host-access?project=beta&request_id={requested["request_id"]}')
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#nav-access')).to_have_attribute('aria-selected', 'true')
        expect(self.page.locator('#project')).to_have_value('beta')
        expect(self.page.locator('#host-access-request')).to_have_value(requested['request_id'])
        expect(self.page.locator('#host-access-approve')).to_be_disabled()
        self.assertFalse(self.host_access.get('beta')['active'])

        missing = 'f' * 32
        self.assertNotIn(missing, (first['request_id'], requested['request_id']))
        self.page.goto(f'http://127.0.0.1:{port}/host-access?project=beta&request_id={missing}')
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#host-access-request')).to_have_value('')
        expect(self.page.locator('#host-access-guidance')).to_contain_text('링크의 요청이 만료')
        expect(self.page.locator('#host-access-approve')).to_be_disabled()
        self.assertFalse(self.host_access.get('beta')['active'])

    def prepare_host_access_duration_form(self):
        requested = self.host_access.request('alpha')
        self.page.locator('#nav-access').click()
        expect(self.page.locator('#host-access-request')).to_have_value(requested['request_id'])
        duration = self.page.locator('#host-access-duration')
        self.page.locator('#host-access-confirmation').fill('alpha')
        self.page.locator('#host-access-token').fill(
            (self.root / 'config' / 'token').read_text().strip())
        return duration

    def test_host_access_duration_allows_thirty_days(self):
        duration = self.prepare_host_access_duration_form()
        expect(duration).to_have_attribute('max', '43200')
        expect(duration).to_have_value('43200')
        duration.fill('43200')
        expect(self.page.locator('#host-access-approve')).to_be_enabled()

    def test_host_access_duration_blocks_over_thirty_days(self):
        duration = self.prepare_host_access_duration_form()
        duration.fill('43201')
        expect(self.page.locator('#host-access-approve')).to_be_disabled()

    def test_host_access_lifetime_controls_match_installation_scope(self):
        duration = self.prepare_host_access_duration_form()
        lifetime = self.page.locator('#host-access-lifetime')
        expect(lifetime).to_have_value('timed')
        expect(self.page.locator('#host-access')).to_contain_text(
            '이 MCP 설치 전체의 호출자')
        expect(self.page.locator('#host-access')).to_contain_text('OS 호스트 사용자 계정')
        expect(lifetime).to_be_disabled()
        expect(duration).to_be_enabled()

    def test_rpg_default_map_and_mission_fit_first_viewport(self):
        expect(self.page.locator('#nav-workroom')).to_have_attribute('aria-selected', 'true')
        expect(self.page.locator('#overview-current-work')).to_be_hidden()
        expect(self.room.locator('#wd-context-details')).not_to_have_attribute('open', '')
        expect(self.room.locator('#wd-brains [data-source-kind="chatgpt"]')).to_have_count(0)
        for width, height in ((1440, 900), (390, 844), (320, 844)):
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.evaluate('window.scrollTo(0, 0)')
            camera = self.room.locator('#wd-camera').bounding_box()
            self.assertLess(camera['y'], 480, (width, camera))
            self.assertGreater(min(height - camera['y'], camera['height']), 220)
            self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
            expect(self.room.locator('#wd-mission')).to_be_visible()
        self.page.locator('#nav-overview').click()
        expect(self.page.locator('#work-map')).to_be_hidden()
        expect(self.page.locator('#overview-current-work')).to_be_visible()

    def test_rpg_character_talk_uses_records_and_preserves_log_navigation(self):
        row = observed_host_row('alpha', '7' * 32, state='failed')
        row.update(finished_at=datetime.now(timezone.utc).isoformat(), termination_verified=True,
                   result={'exit_code': 2, 'duration_ms': 2000, 'stdout_bytes': 12, 'stderr_bytes': 5})
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]')
        expect(actor).to_have_attribute('data-job', 'operator')
        actor.click()
        expect(self.room.locator('#wd-detail .wd-job-badge')).to_contain_text('운영자')
        mutations = []
        self.page.on('request', lambda req: mutations.append(req.url) if req.method == 'POST' else None)
        self.room.get_by_role('button', name='왜 멈췄어?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('종료 코드 2')
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('원본 로그')
        self.room.get_by_role('button', name='관련 기록 보여줘', exact=True).click()
        expect(self.room.locator('#wd-related-records')).to_have_attribute('open', '')
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#nav-executions')).to_have_attribute('aria-selected', 'true')
        expect(self.page.locator('#host-execution-log')).to_contain_text('fixture host output')
        self.assertEqual(mutations, [])
        self.page.locator('#nav-workroom').click()
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#wd-detail')).not_to_contain_text('종료 코드 2')
        expect(self.room.locator('#wd-talk-answer')).to_have_count(0)

    def test_rpg_poll_preserves_inspector_keyboard_focus_and_open_details(self):
        row = observed_host_row('alpha', '8' * 32)
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]').click()
        log = self.room.locator('#wd-open-log')
        log.focus()
        row.update(state='failed', finished_at=datetime.now(timezone.utc).isoformat(), termination_verified=True,
                   result={'exit_code': 3, 'duration_ms': 2000, 'stdout_bytes': 12, 'stderr_bytes': 5})
        # A programmatic refresh leaves focus where automatic polling would leave it.
        self.page.locator('#refresh').evaluate('node => node.click()')
        expect(self.room.locator('.wd-summary-numeric')).to_contain_text('종료 코드 3')
        expect(log).to_be_focused()
        technical = self.room.locator('#wd-detail .wd-detail-technical')
        technical.locator('summary').click()
        technical.locator('summary').focus()
        row['result']['stderr_bytes'] = 27
        self.page.locator('#refresh').evaluate('node => node.click()')
        expect(self.room.locator('.wd-summary-numeric')).to_contain_text('27B')
        expect(technical).to_have_attribute('open', '')
        expect(technical.locator('summary')).to_be_focused()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        back = self.room.locator('#wd-back-map')
        back.focus()
        row['result']['stderr_bytes'] = 31
        self.page.locator('#refresh').evaluate('node => node.click()')
        expect(self.room.locator('.wd-summary-numeric')).to_contain_text('31B')
        expect(back).to_be_focused()

    def test_host_character_explains_current_purpose_and_separate_previous_result(self):
        current = observed_host_row('alpha', '5' * 32)
        current['work_context'] = {
            'task_label': 'API·Worker 재빌드', 'label_source': 'reported',
            'command_label': 'npm run build', 'action': 'build',
        }
        previous = observed_host_row('alpha', '6' * 32, state='succeeded')
        previous.update(phase='COMPLETE', termination_verified=True,
                        finished_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                        result={'exit_code': 0, 'duration_ms': 2500,
                                'stdout_bytes': 80, 'stderr_bytes': 0},
                        work_context={'task_label': '이전 타입 검사', 'label_source': 'reported',
                                      'command_label': 'npm run typecheck', 'action': 'test'})
        self.host_execution.rows.extend([current, previous])
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]')
        actor.click()
        summary = self.room.locator('#wd-detail .wd-work-summary')
        expect(summary).to_contain_text('API·Worker 재빌드')
        expect(summary).to_contain_text('npm run build')
        expect(summary.locator('.wd-summary-phase')).to_contain_text('명령 실행')
        expect(summary.locator('.wd-summary-phase')).not_to_contain_text('EXECUTE')
        mutations = []
        self.page.on('request', lambda req: mutations.append(req.url)
                     if req.method in {'POST', 'PUT', 'DELETE'} else None)
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        answer = self.room.locator('#wd-talk-answer')
        expect(answer).to_contain_text('API·Worker 재빌드')
        expect(answer).to_contain_text('npm run build')
        expect(answer).to_contain_text('명령 실행')
        expect(answer).not_to_contain_text('EXECUTE')
        expect(answer).not_to_contain_text('이전 타입 검사')
        self.room.get_by_role('button', name='마지막 결과는?', exact=True).click()
        expect(answer).to_contain_text('이전 타입 검사')
        expect(answer).to_contain_text('종료 코드 0')
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#host-execution')).to_have_value(current['operation_id'])
        expect(self.page.locator('#host-execution option:checked')).to_contain_text('API·Worker 재빌드')
        expect(self.page.locator('#host-execution-state')).to_contain_text('npm run build')
        expect(self.page.locator('#host-execution-state')).to_contain_text('API·Worker 재빌드')
        expect(self.page.locator('#host-execution-log')).to_contain_text('fixture host output')
        self.assertEqual(mutations, [])

    def test_schema014_mcp_call_is_visible_without_claiming_database_success(self):
        call_id = 'schema014-activity'
        self.store.record_activity('alpha', 'prepare_schema014_migration', 'started', call_id)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        actor.click()
        detail = self.room.locator('#wd-detail')
        expect(detail.locator('.wd-summary-purpose')).to_contain_text('014')
        expect(detail.locator('.wd-summary-purpose')).not_to_contain_text('prepare_schema014_migration')
        self.store.record_activity('alpha', 'prepare_schema014_migration', 'completed', call_id)
        self.page.locator('#refresh').click()
        expect(detail.locator('.wd-summary-result')).to_contain_text('요청이 반환')
        expect(detail.locator('.wd-summary-result')).not_to_contain_text('성공 상태')
        self.room.get_by_role('button', name='마지막 결과는?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('요청이 반환')

    def test_unlabelled_completed_host_details_distinguish_identity_command_and_result(self):
        row = observed_host_row('alpha', '4' * 32, state='succeeded')
        row.update(phase='COMPLETE', termination_verified=True,
                   finished_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                   result={'exit_code': 0, 'duration_ms': 103,
                           'stdout_bytes': 261, 'stderr_bytes': 0},
                   work_context={'task_label': None, 'label_source': 'unavailable',
                                 'command_label': 'Host command', 'action': 'execute'})
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]').click()
        detail = self.room.locator('#wd-detail')
        expect(detail.locator('.wd-summary-purpose')).to_contain_text('44444444')
        expect(detail.locator('.wd-summary-purpose')).to_contain_text('작업 목적 미수집')
        expect(detail.locator('.wd-summary-command')).to_contain_text('종류 미수집')
        expect(detail.locator('.wd-summary-phase')).to_contain_text('실행 종료')
        expect(detail.locator('.wd-summary-result')).to_contain_text('명령 종료가 확인')
        expect(detail).to_contain_text('표준 출력 261B')
        for question in ('지금 뭐 해?', '마지막 결과는?'):
            self.room.get_by_role('button', name=question, exact=True).click()
            answer = self.room.locator('#wd-talk-answer')
            expect(answer).to_contain_text('44444444')
            expect(answer).to_contain_text('작업 목적 미수집')
            expect(answer).to_contain_text('종료 코드 0')
        for width in (1440, 390, 320):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
            expect(detail.locator('.wd-summary-purpose')).to_be_visible()

    def test_legacy_host_purpose_is_unavailable_and_opaque_inputs_stay_out_of_frame(self):
        row = observed_host_row('alpha', '4' * 32)
        row.update(phase='PRECHECK', input_text='private prompt never display',
                   argv=['--token=command-secret'], private_log='private raw log',
                   work_context={'task_label': None, 'label_source': 'unavailable',
                                 'command_label': 'Python 명령 실행', 'action': 'execute',
                                 'private_log': 'nested private raw log'})
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]').click()
        detail = self.room.locator('#wd-detail')
        expect(detail).to_contain_text('Python 명령 실행')
        expect(detail).to_contain_text('미수집')
        for secret in ('private prompt never display', 'command-secret', 'private raw log'):
            expect(self.room.locator('body')).not_to_contain_text(secret)
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('Python 명령 실행')
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('미수집')
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('실행 전 점검')
        self.room.locator('#wd-open-log').click()
        state = self.page.locator('#host-execution-state')
        expect(state).to_contain_text('Python 명령 실행')
        for secret in ('private prompt never display', 'command-secret',
                       'private raw log', 'nested private raw log'):
            expect(state).not_to_contain_text(secret)

    def test_codex_public_purpose_does_not_invent_inner_coding_or_reasoning(self):
        row = observed_host_row('alpha', '2' * 32, kind='codex')
        row['work_context'] = {
            'task_label': '변경사항 검토 요청', 'label_source': 'reported',
            'command_label': 'Codex host task', 'action': 'code',
        }
        # The host adapter reports this coarse lifecycle event as well as the
        # polled operation. It is not evidence of a specific inner tool action.
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'personal-ops-agent', 'source_kind': 'codex-cli',
            'actor_id': 'codex-review', 'actor_kind': 'agent', 'actor_label': 'Codex host agent',
            'task_id': row['operation_id'], 'task_kind': 'agent',
            'task_label': '변경사항 검토 요청', 'action': 'code', 'state': 'running',
            'time': datetime.now(timezone.utc).isoformat(),
        }, provenance='observed')
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]')
        actor.click()
        summary = self.room.locator('#wd-detail .wd-work-summary')
        expect(summary).to_contain_text('변경사항 검토 요청')
        expect(summary).to_contain_text('Codex 작업 위임')
        expect(summary).not_to_contain_text('코드 작성')
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('변경사항 검토 요청')
        expect(self.room.locator('#wd-talk-answer')).not_to_contain_text('코드 작성')
        expect(self.room.locator('.wd-role-character[data-owner-id="entity:personal-ops-agent:codex-review"]')).to_have_count(0)

    def test_reported_inner_action_does_not_become_observed_host_activity(self):
        row = observed_host_row('alpha', '9' * 32, kind='codex')
        row['work_context'] = {
            'task_label': '변경사항 검토 요청', 'label_source': 'reported',
            'command_label': 'Codex host task', 'action': 'code',
        }
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'personal-ops-agent', 'source_kind': 'codex-cli',
            'actor_id': 'codex-review', 'actor_kind': 'agent', 'actor_label': 'Codex host agent',
            'task_id': row['operation_id'], 'task_kind': 'agent',
            'task_label': '보고된 내부 테스트', 'action': 'test', 'tool_name': 'run_tests',
            'state': 'running', 'time': datetime.now(timezone.utc).isoformat(),
        }, provenance='reported')
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]').click()
        summary = self.room.locator('#wd-detail .wd-work-summary')
        expect(summary).to_contain_text('변경사항 검토 요청')
        expect(summary).to_contain_text('Codex 작업 위임')
        expect(summary).not_to_contain_text('run_tests')
        expect(summary).not_to_contain_text('테스트 실행')
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).not_to_contain_text('run_tests')
        expect(self.room.locator('#wd-talk-answer')).not_to_contain_text('테스트 실행')

    def test_descriptive_host_work_fits_mobile_and_clears_on_project_change(self):
        row = observed_host_row('alpha', '3' * 32)
        row['work_context'] = {
            'task_label': 'API와 Worker를 다시 빌드하고 테스트 환경에서 설정 반영 여부 확인',
            'label_source': 'reported', 'command_label': 'npm run build', 'action': 'build',
        }
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        for width in (1440, 390, 320):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-host:host-executor"]').click()
            detail = self.room.locator('#wd-detail')
            expect(detail).to_contain_text(row['work_context']['task_label'])
            expect(detail).to_contain_text('npm run build')
            self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
            self.assertLessEqual(detail.evaluate('node => node.scrollWidth - node.clientWidth'), 1)
            self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
            expect(self.room.locator('#wd-talk-answer')).to_contain_text(row['work_context']['task_label'])
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#host-execution option:checked')).to_contain_text(row['work_context']['task_label'])
        self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), 320)
        self.page.locator('#nav-workroom').click()
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#wd-detail')).not_to_contain_text(row['work_context']['task_label'])
        expect(self.room.locator('#wd-talk-answer')).to_have_count(0)

    def test_rpg_pause_status_updates_without_a_new_snapshot(self):
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('reader', 0, {action: 'read'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledFixture.setLive(false);
        }""")
        expect(fixture.locator('#wd-status')).to_contain_text('관찰 일시정지')
        self.room_page.evaluate('calledFixture.setLive(true)')
        expect(fixture.locator('#wd-status')).to_contain_text('관찰 중')

    def test_rpg_work_action_and_mobile_detail_are_readable(self):
        now = datetime.now(timezone.utc).isoformat()
        row = {'schema_version': 1, 'source_id': 'cli', 'source_kind': 'codex-cli',
               'actor_id': 'developer', 'actor_kind': 'agent', 'actor_label': 'Codex',
               'task_id': 'coding', 'task_kind': 'agent', 'task_label': '화면 수정',
               'action': 'code', 'state': 'running', 'time': now}
        self.store.record_work_event('alpha', row, provenance='observed')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:cli:developer"]')
        expect(actor).to_have_attribute('data-job', 'developer')
        expect(actor.locator('.wd-action-mark')).to_contain_text('</>')
        self.room.locator('#wd-mission').click()
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('화면 수정')
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('코드 작성')
        self.page.locator('#follow').click()
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('마지막 기록')
        self.room.locator('#wd-back-map').click()
        expect(self.room.locator('#wd-camera')).to_be_focused()

    def test_native_queued_call_is_visible_instead_of_newer_session_heartbeat(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'terminal-native', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_id': 'session-activity', 'task_kind': 'agent',
                'task_label': 'Codex session active', 'action': 'execute',
                'state': 'running', 'time': now.isoformat()}
        call = {**base, 'task_id': 'call-native-request', 'task_kind': 'call',
                'task_label': '터미널 도구 호출 요청', 'tool_name': 'exec_command',
                'state': 'queued', 'time': (now - timedelta(seconds=1)).isoformat()}
        self.store.record_work_events('alpha', [call, base], provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:codex-local:terminal-native"]')
        expect(actor).to_have_count(1)
        expect(actor).to_have_attribute('data-state', 'queued')
        expect(actor).to_have_attribute('data-room', 'waiting')
        expect(actor).to_have_attribute('data-working', 'false')
        expect(self.room.locator('#wd-mission strong')).to_have_text('터미널 도구 호출 요청')
        expect(self.room.locator('#wd-mission small')).to_have_text('대기 중인 업무')
        expect(self.room.locator('#wd-mission .wd-mission-state')).to_contain_text('대기')
        expect(self.room.locator('#wd-work-board .wd-work-item').first).to_have_attribute(
            'data-task-id', 'task:codex-local:terminal-native:call-native-request')
        actor.click()
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('터미널 도구 호출 요청')
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('exec_command')

    def test_native_mission_tracks_real_call_then_returns_to_session_liveness(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'terminal-mission', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_id': 'session-activity', 'task_kind': 'agent',
                'task_label': 'Codex session active', 'action': 'execute', 'state': 'running',
                'time': now.isoformat()}
        call = {**base, 'task_id': 'call-mission', 'task_kind': 'call',
                'task_label': '명령 상태 확인', 'tool_name': 'exec_command',
                'time': (now - timedelta(seconds=2)).isoformat()}
        self.store.record_work_events('alpha', [call, base], provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        mission = self.room.locator('#wd-mission')
        expect(mission.locator('strong')).to_have_text('명령 상태 확인')
        expect(mission.locator('small')).to_have_text('현재 임무')
        expect(self.room.locator('#wd-work-board .wd-work-item').first).to_have_attribute(
            'data-task-id', 'task:codex-local:terminal-mission:call-mission')
        mission.click()
        expect(self.room.locator('#wd-detail .wd-summary-purpose')).to_have_text('명령 상태 확인')
        self.room.get_by_role('button', name='지금 뭐 해?', exact=True).click()
        expect(self.room.locator('#wd-talk-answer')).to_contain_text('명령 상태 확인')

        self.store.record_work_events('alpha', [
            {**call, 'state': 'returned', 'time': (now - timedelta(seconds=1)).isoformat()},
            base], provenance='observed')
        self.page.locator('#refresh').click()
        expect(mission.locator('strong')).to_have_text('세션 활동 중')
        expect(mission.locator('small')).to_have_text('세션 상태')
        expect(self.room.locator('#wd-work-board .wd-work-item').first).to_have_attribute(
            'data-task-id', 'task:codex-local:terminal-mission:session-activity')
        mission.click()
        expect(self.room.locator('#wd-detail .wd-summary-purpose')).to_have_text('세션 활동 중')
        recent = self.room.locator('.wd-native-activity [data-native-group="recent"]')
        expect(recent).to_contain_text('명령 상태 확인')
        expect(recent).to_contain_text('요청 반환')

    def test_native_mission_does_not_let_other_owners_heartbeat_hide_a_pending_call(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'waiting-owner', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_id': 'actual-call', 'task_kind': 'call', 'task_label': '호출 응답 대기',
                'tool_name': 'exec_command', 'action': 'execute', 'state': 'queued',
                'time': (now - timedelta(seconds=1)).isoformat()}
        heartbeat = {**base, 'actor_id': 'other-owner', 'task_id': 'session-activity',
                     'task_kind': 'agent', 'task_label': 'Codex session active',
                     'state': 'running', 'time': now.isoformat()}
        self.store.record_work_events('alpha', [base, heartbeat], provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        mission = self.room.locator('#wd-mission')
        expect(mission.locator('strong')).to_have_text('호출 응답 대기')
        expect(mission.locator('small')).to_have_text('대기 중인 업무')
        mission.click()
        expect(self.room.locator('#wd-detail .wd-summary-purpose')).to_have_text('호출 응답 대기')
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('응답 대기')
        expect(self.room.locator('#wd-detail .wd-work-summary')).not_to_contain_text('실행 중으로 관측')

    def test_claude_children_distinguish_expired_lifecycle_from_recent_tool_results(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'claude-local', 'source_kind': 'claude-cli',
                'actor_kind': 'agent', 'actor_label': 'general-purpose', 'task_kind': 'call',
                'task_label': 'Claude subagent', 'action': 'execute', 'state': 'started'}
        rows = []
        for child in ('child-a', 'child-b'):
            actor = 'sub:session:' + child
            rows.extend([
                {**base, 'actor_id': actor, 'task_id': 'agent:session:' + child,
                 'time': (now - timedelta(minutes=5)).isoformat()},
                {**base, 'actor_id': actor, 'task_id': 'tool:' + child,
                 'task_label': 'Bash', 'tool_name': 'Bash', 'state': 'started',
                 'time': (now - timedelta(seconds=4)).isoformat()},
                {**base, 'actor_id': actor, 'task_id': 'tool:' + child,
                 'task_label': 'Bash', 'tool_name': 'Bash', 'state': 'returned',
                 'time': (now - timedelta(seconds=3)).isoformat()},
            ])
        self.store.record_work_events('alpha', rows, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        badges = self.room.locator('.wd-actor-observation')
        expect(badges).to_have_count(2)
        child = self.room.locator('.wd-actor[data-actor-id="entity:claude-local:sub%3Asession%3Achild-a"]')
        expect(child).to_have_attribute('data-state', 'idle')
        expect(child).to_have_attribute('data-working', 'false')
        expect(child).to_have_attribute('data-observation-status', 'subagent')
        expect(child).to_have_attribute('aria-label', re.compile('서브에이전트 종료 미확인'))
        child.click()
        note = self.room.locator('#wd-detail .wd-detail-observation')
        expect(note).to_contain_text('서브에이전트 종료 미확인')
        expect(note).to_contain_text('실행 중인 에이전트 수')
        recent = self.room.locator('#wd-detail .wd-native-activity')
        expect(recent).to_contain_text('최근 Claude 호출')
        expect(recent.locator('.wd-native-entry')).to_have_count(1)
        expect(recent).to_contain_text('요청 반환')
        expect(recent).not_to_contain_text('tool:child-b')
        expect(self.room.locator('#wd-map-note')).to_contain_text('완료 후 대기하는 객체')
        stop = {**base, 'actor_id': 'sub:session:child-a', 'task_id': 'agent:session:child-a',
                'state': 'returned', 'time': now.isoformat()}
        self.store.record_work_events('alpha', [stop], provenance='observed')
        self.page.locator('#refresh').click()
        expect(child).to_have_attribute('data-observation-status', '')
        expect(child.locator('.wd-actor-observation')).to_have_count(0)
        expect(self.room.locator('.wd-actor-observation')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_claude_expired_badge_preserves_recorded_replay_and_clears_when_evidence_is_pruned(self):
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            const base = {source_id: 'claude-local', source_kind: 'claude-cli',
                actor_id: 'sub:session:child', actor_label: 'Claude child', task_kind: 'call',
                action: 'execute'};
            calledRecords = [callRecord('unused', 300, {...base, task_id: 'agent:session:child'}),
                callRecord('unused', 3, {...base, task_id: 'tool:done', tool_name: 'Bash'}),
                callRecord('unused', 2, {...base, task_id: 'tool:done', tool_name: 'Bash', state: 'returned'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        child = fixture.locator('.wd-actor[data-actor-id="entity:claude-local:sub%3Asession%3Achild"]')
        expect(child.locator('.wd-actor-observation')).to_have_text('종료 미확인')
        fixture.locator('#wd-replay').click()
        expect(child).to_have_attribute('data-presentation', 'replay')
        expect(child.locator('.wd-actor-task')).to_contain_text('기록 재생')
        expect(child.locator('.wd-actor-observation')).to_have_text('종료 미확인')
        expect(child).to_have_attribute('data-state', 'idle')
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: []})""")
        expect(child).to_have_count(1)
        expect(child).to_have_attribute('data-observation-status', '')
        expect(child.locator('.wd-actor-observation')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_claude_live_registry_shows_two_children_and_concrete_work_without_double_counting_agents(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = ['first', 'second'].map(id => callRecord('unused', 1, {
                source_id: 'claude-local', source_kind: 'claude-cli', actor_id: 'sub:session:' + id,
                actor_label: id, task_id: 'agent:session:' + id, task_kind: 'call',
                task_label: 'Claude subagent', action: 'execute'}));
            calledRecords.push(callRecord('unused', 2, {source_id: 'claude-local', source_kind: 'claude-cli',
                actor_id: 'sub:session:first', actor_label: 'first', task_id: 'tool:live',
                task_kind: 'call', task_label: 'Edit', action: 'code'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-actor')).to_have_count(2)
        expect(fixture.locator('#wd-status')).to_contain_text('하위 에이전트 활동 관측 2명')
        expect(fixture.locator('#wd-mission strong')).to_have_text('Edit')
        first = fixture.locator('.wd-actor[data-actor-id="entity:claude-local:sub%3Asession%3Afirst"]')
        expect(first).to_have_attribute('data-room', 'coding')
        first.click()
        expect(fixture.locator('#wd-detail .wd-summary-purpose')).to_have_text('Edit')
        second = fixture.locator('.wd-actor[data-actor-id="entity:claude-local:sub%3Asession%3Asecond"]')
        expect(second).to_have_attribute('data-state', 'running')
        second.click()
        expect(fixture.locator('#wd-detail .wd-summary-purpose')).to_have_text('하위 에이전트 활동')
        expect(fixture.locator('#wd-detail .wd-summary-action')).not_to_contain_text('명령 실행')
        expect(fixture.locator('#wd-detail .wd-summary-result')).to_contain_text('에이전트 활동')
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: calledRecords,
            now: Date.now() + 121000})""")
        expect(fixture.locator('#wd-status')).to_contain_text('하위 에이전트 활동 관측 0명')
        expect(fixture.locator('.wd-actor[data-state="running"]')).to_have_count(0)

    def test_native_terminal_result_remains_readable_after_a_newer_heartbeat(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'terminal-native', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_id': 'session-activity', 'task_kind': 'agent',
                'task_label': 'Codex session active', 'action': 'execute',
                'state': 'running', 'time': now.isoformat()}
        done = {**base, 'task_id': 'native-result', 'task_kind': 'call',
                'task_label': '터미널 명령 결과', 'tool_name': 'CommandExecution',
                'state': 'failed', 'exit_code': 7,
                'time': (now - timedelta(seconds=1)).isoformat()}
        self.store.record_work_events('alpha', [done, base], provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:codex-local:terminal-native"]')
        expect(actor).to_have_count(1)
        actor.click()
        panel = self.room.locator('#wd-detail .wd-native-activity')
        expect(panel).to_contain_text('최근 터미널 활동')
        expect(panel).to_contain_text('터미널 명령 결과')
        expect(panel).to_contain_text('CommandExecution')
        expect(panel).to_contain_text('종료 코드 7')
        expect(panel).to_contain_text('실패')
        panel.get_by_role('button', name=re.compile('^작업 상세')).click()
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('터미널 명령 결과')

    def test_native_activity_groups_survive_exact_call_selection_and_exclude_other_actors(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'terminal-native', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_kind': 'call', 'action': 'execute'}
        rows = [
            {**base, 'task_id': 'native-current', 'task_label': '현재 터미널 호출',
             'tool_name': 'exec_command', 'state': 'running',
             'time': (now - timedelta(seconds=1)).isoformat()},
            {**base, 'task_id': 'native-result', 'task_label': '실패한 터미널 결과',
             'tool_name': 'CommandExecution', 'state': 'started',
             'time': (now - timedelta(seconds=5)).isoformat()},
            {**base, 'task_id': 'native-result', 'task_label': '실패한 터미널 결과',
             'tool_name': 'CommandExecution', 'state': 'failed', 'exit_code': 7,
             'time': (now - timedelta(seconds=2)).isoformat()},
            {**base, 'actor_id': 'other-terminal', 'task_id': 'other-call',
             'task_label': '다른 세션 호출', 'tool_name': 'read_file', 'state': 'running',
             'time': now.isoformat()},
        ]
        self.store.record_work_events('alpha', rows, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:codex-local:terminal-native"]').click()
        panel = self.room.locator('#wd-detail .wd-native-activity')
        expect(panel).to_be_visible()
        current = panel.locator('[data-native-group="current"]')
        recent = panel.locator('[data-native-group="recent"]')
        expect(current).to_contain_text('현재 호출')
        expect(recent).to_contain_text('최근 결과')
        expect(current.locator('.wd-native-entry')).to_have_count(1)
        expect(recent.locator('.wd-native-entry')).to_have_count(1)
        expect(current.locator('.wd-native-entry')).to_have_attribute(
            'data-task-id', 'task:codex-local:terminal-native:native-current')
        result = recent.locator('.wd-native-entry')
        expect(result).to_have_attribute('data-task-id', 'task:codex-local:terminal-native:native-result')
        expect(result).to_have_attribute('data-state', 'failed')
        expect(result).to_contain_text('종료 코드 7')
        expect(panel).not_to_contain_text('다른 세션 호출')
        result.locator('button[data-task-id="task:codex-local:terminal-native:native-result"]').click()
        expect(self.room.locator('#wd-detail .wd-work-summary')).to_contain_text('실패한 터미널 결과')
        persisted = self.room.locator('#wd-detail .wd-native-activity')
        expect(persisted).to_be_visible()
        expect(persisted.locator('.wd-native-entry')).to_have_count(2)
        expect(persisted).to_contain_text('종료 코드 7')
        expect(persisted.locator(
            'button[data-task-id="task:codex-local:terminal-native:native-result"]'
        )).to_have_attribute('aria-current', 'true')
        self.page.locator('#follow').click()
        expect(persisted.locator('[data-native-group="current"] h4')).to_have_text('마지막 확인 호출')

    def test_native_activity_caps_current_first_and_treats_expired_queue_as_recent_unknown(self):
        now = datetime.now(timezone.utc)
        base = {'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
                'actor_id': 'terminal-cap', 'actor_kind': 'agent', 'actor_label': 'Codex',
                'task_kind': 'call', 'task_label': '터미널 호출', 'action': 'execute',
                'tool_name': 'exec_command'}
        rows = [
            {**base, 'task_id': 'current-new', 'state': 'running',
             'time': (now - timedelta(seconds=1)).isoformat()},
            {**base, 'task_id': 'current-mid', 'state': 'queued',
             'time': (now - timedelta(seconds=2)).isoformat()},
            {**base, 'task_id': 'current-old', 'state': 'started',
             'time': (now - timedelta(seconds=3)).isoformat()},
            {**base, 'task_id': 'current-overflow', 'state': 'canceling',
             'time': (now - timedelta(seconds=4)).isoformat()},
            {**base, 'task_id': 'expired-queue', 'task_label': '만료된 호출 요청',
             'state': 'queued', 'time': (now - timedelta(seconds=121)).isoformat()},
            {**base, 'task_id': 'recent-new', 'state': 'returned',
             'time': (now - timedelta(seconds=130)).isoformat()},
            {**base, 'task_id': 'recent-old', 'state': 'failed', 'exit_code': 9,
             'time': (now - timedelta(seconds=140)).isoformat()},
        ]
        self.store.record_work_events('alpha', rows, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:codex-local:terminal-cap"]').click()
        panel = self.room.locator('#wd-detail .wd-native-activity')
        entries = panel.locator('.wd-native-entry')
        expect(entries).to_have_count(6)
        current = panel.locator('[data-native-group="current"] .wd-native-entry')
        recent = panel.locator('[data-native-group="recent"] .wd-native-entry')
        expect(current).to_have_count(3)
        expect(recent).to_have_count(3)
        self.assertEqual(current.evaluate_all('rows => rows.map(row => row.dataset.taskId)'), [
            'task:codex-local:terminal-cap:current-new',
            'task:codex-local:terminal-cap:current-mid',
            'task:codex-local:terminal-cap:current-old',
        ])
        self.assertEqual(recent.evaluate_all('rows => rows.map(row => row.dataset.taskId)'), [
            'task:codex-local:terminal-cap:expired-queue',
            'task:codex-local:terminal-cap:recent-new',
            'task:codex-local:terminal-cap:recent-old',
        ])
        expired = panel.locator('.wd-native-entry[data-task-id="task:codex-local:terminal-cap:expired-queue"]')
        expect(expired).to_have_attribute('data-state', 'unknown')
        expect(expired).to_contain_text('상태 미확인')
        expect(expired).not_to_contain_text('성공')
        expect(expired).not_to_contain_text('완료')
        expect(panel.locator('[data-task-id="task:codex-local:terminal-cap:current-overflow"]')).to_have_count(0)
        expect(self.room.locator(
            '#wd-related-records button[data-task-id="task:codex-local:terminal-cap:current-overflow"]'
        )).to_have_count(1)

    def test_native_activity_long_tool_name_stays_inside_frame_at_supported_widths(self):
        now = datetime.now(timezone.utc)
        long_tool = 't' * 120
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'codex-local', 'source_kind': 'codex-cli',
            'actor_id': 'terminal-long', 'actor_kind': 'agent', 'actor_label': 'Codex',
            'task_id': 'long-tool', 'task_kind': 'call', 'task_label': '긴 도구 이름 확인',
            'action': 'execute', 'tool_name': long_tool, 'state': 'failed', 'exit_code': 19,
            'time': now.isoformat(),
        }, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#refresh').click()
        self.room.locator('.wd-actor[data-actor-id="entity:codex-local:terminal-long"]').click()
        for width in (1440, 390, 320):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                panel = self.room.locator('#wd-detail .wd-native-activity')
                expect(panel).to_contain_text(long_tool)
                expect(panel).to_contain_text('종료 코드 19')
                self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertTrue(self.room_page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertTrue(panel.evaluate("""node => {
                    const box = node.getBoundingClientRect();
                    return box.left >= 0 && box.right <= document.documentElement.clientWidth;
                }"""))

    def test_workroom_precedes_approvals_and_navigation_is_keyboard_operable(self):
        original_map = self.page.locator('#work-map').element_handle()
        expect(self.page.locator('#nav-workroom')).to_have_attribute('aria-selected', 'true')
        expect(self.page.locator('#view-workroom')).to_be_visible()
        expect(self.page.locator('#host-access')).to_be_hidden()
        expect(self.page.locator('#agent-approval')).to_be_hidden()
        self.page.locator('#nav-workroom').focus()
        self.page.keyboard.press('ArrowRight')
        expect(self.page.locator('#nav-overview')).to_be_focused()
        expect(self.page.locator('#work-map')).to_be_hidden()
        self.page.locator('#nav-access').click()
        expect(self.page.locator('#view-access')).to_be_visible()
        expect(self.page.locator('#host-access')).to_be_visible()
        self.page.locator('#nav-workroom').click()
        expect(self.page.locator('#view-workroom')).to_be_visible()
        expect(self.page.locator('#work-map')).to_have_count(1)
        self.assertTrue(original_map.evaluate("node => node === document.querySelector('#work-map')"))

    def test_workroom_has_no_admin_credentials_dom_or_approval_request_access(self):
        frame = self.page.locator('#workroom-frame')
        expect(frame).to_have_count(1)
        expect(frame).to_have_attribute('sandbox', 'allow-scripts')
        expect(self.page.locator('script[src^="/work-dashboard/"],link[href^="/work-dashboard/"]')).to_have_count(0)
        requested = self.host_access.request('alpha')
        grant = self.host_access.approve_web(
            'alpha', request_id=requested['request_id'], duration_seconds=3600,
            allow_codex=False, confirmation='alpha')
        probe = """
        globalThis.isolationProbe = {hash: location.hash, referrer: document.referrer};
        for (const [name, read] of Object.entries({
          parentHash: () => parent.location.hash,
          parentToken: () => parent.document.getElementById('host-access-token').value,
          parentStorage: () => parent.localStorage.length,
          cookies: () => document.cookie
        })) {
          try { globalThis.isolationProbe[name] = read(); }
          catch (error) { globalThis.isolationProbe[name] = error.name; }
        }
        const stolen = new URLSearchParams(location.hash.slice(1)).get('token') || '';
        fetch('/api/host-access-revoke?project=alpha', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({grant_id: 'GRANT_ID', confirmation: 'alpha', admin_token: stolen})
        }).then(response => { globalThis.isolationProbe.approval = response.status; },
          () => { globalThis.isolationProbe.approval = 'blocked'; });
        parent.postMessage({type: 'request', path: '/api/host-access-revoke', method: 'POST'}, '*');
        """.replace('GRANT_ID', grant['grant_id'])
        source = (Path(__file__).resolve().parents[2] / 'work-dashboard' / 'model.js').read_text()
        self.page.route('**/work-dashboard/model.js', lambda route: route.fulfill(
            content_type='text/javascript', body=source + '\n' + probe))
        styles = (Path(__file__).resolve().parents[2] / 'work-dashboard' / 'visualizer.css').read_text()
        self.page.route('**/work-dashboard/visualizer.css', lambda route: route.fulfill(
            content_type='text/css', body=styles + '\n#host-access { display:none!important }'))
        approval_requests = []
        handler = self.server.RequestHandlerClass
        original_post = handler.do_POST

        def observe_post(request):
            if urlsplit(request.path).path in APPROVAL_BODIES:
                approval_requests.append(urlsplit(request.path).path)
            return original_post(request)

        # A fresh fragment proves that renderer code cannot run in the login document.
        token = (self.root / 'config' / 'token').read_text().strip()
        with mock.patch.object(handler, 'do_POST', observe_post):
            self.page.goto('about:blank')
            self.page.goto(f'http://127.0.0.1:{self.server.server_port}/#token={token}')
            expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
            frame_page = self.page.locator('#workroom-frame').element_handle().content_frame()
            self.assertTrue(frame_page.evaluate('() => Boolean(globalThis.isolationProbe)'),
                            'hostile renderer probe must execute inside the sandbox')
            frame_page.wait_for_function("() => globalThis.isolationProbe?.approval === 'blocked'")
        self.assertEqual(approval_requests, [], 'hostile renderer must not reach approval HTTP endpoints')
        self.assertEqual(frame_page.evaluate('globalThis.isolationProbe'), {
            'hash': '', 'referrer': '', 'parentHash': 'SecurityError',
            'parentToken': 'SecurityError', 'parentStorage': 'SecurityError',
            'cookies': 'SecurityError', 'approval': 'blocked',
        })
        self.assertIsNone(self.page.evaluate('globalThis.isolationProbe'))
        self.assertEqual(self.host_access.get('alpha')['grant_id'], grant['grant_id'])
        self.assertTrue(self.host_access.get('alpha')['active'])
        self.page.locator('#nav-access').click()
        expect(self.page.locator('#host-access')).to_be_visible()

    def test_workroom_frame_and_selection_survive_view_changes(self):
        frame = self.page.locator('#workroom-frame').element_handle().content_frame()
        frame.evaluate("globalThis.navigationMarker = 'same-frame'")
        room = self.page.frame_locator('#workroom-frame')
        room.locator('.wd-actor').first.click()
        selected = room.locator('.wd-actor.selected').get_attribute('data-actor-id')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#nav-access').click()
        self.page.locator('#nav-workroom').click()
        self.assertEqual(frame.evaluate('globalThis.navigationMarker'), 'same-frame')
        expect(room.locator('.wd-actor.selected')).to_have_attribute('data-actor-id', selected)

    def test_overview_access_is_read_only_and_project_scoped(self):
        requested = self.host_access.request('alpha')
        grant = self.host_access.approve_web(
            'alpha', request_id=requested['request_id'], duration_seconds=3600,
            allow_codex=True, confirmation='alpha')
        mutations = []
        self.page.on('request', lambda request: mutations.append(urlsplit(request.url).path)
                     if request.method not in {'GET', 'HEAD'} else None)
        self.page.locator('#refresh').click()
        expect(self.page.locator('#overview-access-state')).to_have_text(re.compile(r'^활성 · '))
        expect(self.page.locator('#overview-codex-state')).to_have_text(
            '호스트 Codex 허용 · 격리 준비 상태와 별개')
        self.assertEqual(self.host_access.get('alpha')['grant_id'], grant['grant_id'])
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#overview-codex-state')).to_contain_text('허용 안 함')
        expect(self.page.locator('#overview-access-state')).to_have_text('활성 액세스 없음')
        self.assertEqual(mutations, [])
        self.assertEqual(self.host_execution.rows, [])

    def test_workroom_names_the_chat_brain_without_inventing_live_thinking(self):
        self.room.locator('#wd-context-details > summary').click()
        self.page.locator('#nav-workroom').click()
        brain = self.room.locator('#wd-brains .wd-brain-card[data-source-kind="chatgpt"]')
        expect(brain).to_have_count(0)
        self.assertEqual(self.room.locator('#wd-brains [data-state="reasoning"]').count(), 0)
        self.assertEqual(self.room.locator('.wd-actor[data-kind="agent"]').count(), 0)

    def test_completed_called_agent_waits_without_unobserved_roles(self):
        self.room.locator('#wd-context-details > summary').click()
        row = observed_host_row('alpha', '1' * 32, kind='codex', state='succeeded')
        row.update(finished_at=datetime.now(timezone.utc).isoformat(), termination_verified=True,
                   result={'exit_code': 0, 'duration_ms': 10, 'stdout_bytes': 0, 'stderr_bytes': 0})
        self.host_execution.rows.append(row)
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        expect(self.room.locator('.wd-actor[data-kind="agent"]')).to_have_count(1)
        expect(self.room.locator('.wd-actor[data-kind="agent"]')).to_have_attribute('data-room', 'waiting')
        expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
        expect(self.room.locator('#wd-brains [data-source-kind="chatgpt"]')).to_have_count(0)
        expect(self.room.locator('.wd-task[data-task-id="task:personal-ops-agent:codex-review:' + '1' * 32 + '"]')).to_have_count(1)

    def test_active_roles_work_in_rooms_and_use_explicit_phase_metadata(self):
        common = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                  'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_kind': 'agent', 'action': 'reason', 'state': 'running',
                  'time': datetime.now(timezone.utc).isoformat(), 'model': 'fixture-role-model'}
        for role in ('analysis', 'planning', 'debugging', 'review', 'summary'):
            self.store.record_work_event('alpha', {**common, 'task_id': role,
                'task_label': '공개 작업 단계 ' + role, 'activity_role': role}, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        for role in ('analysis', 'planning', 'debugging', 'review', 'summary'):
            person = self.room.locator('.wd-role-character[data-role="' + role + '"]')
            expect(person).to_have_count(1)
            expected_room = 'review' if role in ('review', 'summary') else 'analysis'
            expect(person).to_have_attribute('data-display-room', expected_room)
            person.click()
            expect(self.room.locator('#wd-detail')).to_contain_text('공개 작업 단계 ' + role)
            expect(self.room.locator('#wd-detail')).to_contain_text('fixture-role-model')
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 900})
            person = self.room.locator('.wd-role-character[data-role="planning"]')
            person.click()
            self.assertTrue(person.evaluate("""node => {
                const item = node.getBoundingClientRect();
                const camera = document.querySelector('#wd-camera').getBoundingClientRect();
                return item.left + item.width / 2 >= camera.left
                    && item.left + item.width / 2 <= camera.right;
            }"""))
            self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        expect(self.room.locator('.wd-actor[data-kind="agent"]')).to_have_count(0)
        expect(self.room.locator('#wd-status')).to_contain_text('진행 5')
        expect(self.room.locator('.wd-room-analysis .wd-room-count')).to_have_text('3')
        expect(self.room.locator('.wd-room-review .wd-room-count')).to_have_text('2')
        self.page.locator('#follow').click()
        expect(self.room.locator('.wd-role-character[data-role="planning"]')).to_have_attribute('data-state', 'paused')
        self.page.locator('#follow').click()
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('.wd-role-character')).to_have_count(0)

    def test_web_search_role_needs_scope_and_keeps_its_actual_owner(self):
        common = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                  'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_kind': 'call', 'action': 'search', 'state': 'running',
                  'time': datetime.now(timezone.utc).isoformat()}
        for task_id, scope, tool in (('internet', 'web', 'web_search'), ('local', 'files', 'rg')):
            self.store.record_work_event('alpha', {**common, 'task_id': task_id,
                'task_label': '자료 검색 ' + task_id, 'search_scope': scope, 'tool_name': tool}, provenance='reported')
        self.store.record_work_event('alpha', {**common, 'task_id': 'ambiguous', 'task_label': 'WebSearch',
            'action': 'read'}, provenance='reported')
        self.store.record_work_event('alpha', {**common, 'source_id': 'web-provider', 'source_kind': 'mcp',
            'actor_id': 'search', 'actor_kind': 'mcp', 'actor_label': '외부 검색 도구',
            'task_id': 'external', 'task_label': '외부 자료 조회', 'search_scope': 'web'}, provenance='reported')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        web = self.room.locator('.wd-role-character[data-owner-id="entity:chat-session:planner"][data-search-scope="web"]')
        files = self.room.locator('.wd-role-character[data-owner-id="entity:chat-session:planner"][data-search-scope="files"]')
        expect(web).to_have_count(1)
        expect(files).to_have_count(1)
        expect(web).to_contain_text('웹검색')
        expect(files).to_contain_text('파일 검색')
        expect(web).to_have_attribute('data-provenance', 'reported')
        external = self.room.locator('.wd-role-character[data-owner-id="entity:web-provider:search"]')
        expect(external).to_have_count(1)
        external.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('외부 검색 도구')
        expect(self.room.locator('#wd-detail')).not_to_contain_text('ChatGPT')
        self.assertEqual(self.room.locator('#wd-detail .wd-task-link').count(), 1)
        self.assertEqual(self.room.locator('.wd-role-character[data-search-scope="web"]').count(), 2)

    def test_selected_role_preserves_actual_task_when_phase_changes_or_completion_is_immediate(self):
        common = {'schema_version': 1, 'source_id': 'selection', 'source_kind': 'chatgpt',
                  'actor_id': 'owner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_id': 'work', 'task_kind': 'agent', 'task_label': '선택 유지 업무',
                  'action': 'reason', 'state': 'running', 'activity_role': 'analysis',
                  'time': datetime.now(timezone.utc).isoformat()}
        self.store.record_work_event('alpha', common, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        self.room.locator('.wd-role-character[data-role="analysis"]').click()
        self.store.record_work_event('alpha', {**common, 'activity_role': 'planning',
            'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(self.room.locator('.wd-role-character[data-role="analysis"]')).to_have_count(0)
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('선택 유지 업무')
        self.room.locator('.wd-role-character[data-role="planning"]').click()
        self.store.record_work_event('alpha', {**common, 'activity_role': 'planning', 'state': 'succeeded',
            'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(self.room.locator('.wd-role-character[data-role="planning"]')).to_have_count(1)
        expect(self.room.locator('.wd-role-character[data-role="planning"]')).to_have_attribute('data-room', 'waiting')
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('선택 유지 업무')
        expect(self.room.locator('#wd-detail')).to_contain_text('작업 완료')

    def test_selected_failed_role_keeps_its_record_when_new_work_replaces_it(self):
        common = {'schema_version': 1, 'source_id': 'selection', 'source_kind': 'chatgpt',
                  'actor_id': 'owner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_kind': 'agent', 'time': datetime.now(timezone.utc).isoformat()}
        self.store.record_work_event('alpha', {**common, 'task_id': 'review', 'task_label': '검토 실패 기록',
            'action': 'review', 'state': 'failed'}, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        self.room.locator('.wd-role-character[data-role="review"]').click()
        self.store.record_work_event('alpha', {**common, 'task_id': 'code', 'task_label': '새 코드 작업',
            'action': 'code', 'state': 'running', 'time': datetime.now(timezone.utc).isoformat()},
            provenance='observed')
        self.page.locator('#refresh').click()
        expect(self.room.locator('.wd-role-character[data-role="review"]')).to_have_count(0)
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('검토 실패 기록')
        expect(self.room.locator('#wd-detail')).to_contain_text('실패')

    def test_roles_share_six_room_slots_and_evicted_role_can_be_inspected(self):
        now = datetime.now(timezone.utc)
        for index in range(7):
            is_agent = index >= 3
            self.store.record_work_event('alpha', {
                'schema_version': 1, 'source_id': 'room-cap',
                'source_kind': 'chatgpt' if is_agent else 'mcp', 'actor_id': 'owner-' + str(index),
                'actor_kind': 'agent' if is_agent else 'mcp', 'actor_label': '검사 담당 ' + str(index),
                'task_id': 'work', 'task_kind': 'agent' if is_agent else 'call',
                'task_label': '방 수용량 검사 ' + str(index),
                'action': 'reason' if is_agent else 'read', 'state': 'running',
                'time': (now - timedelta(seconds=60 if index == 6 else 10 - index)).isoformat()},
                provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        people = self.room.locator('#wd-world :is(.wd-role-character,.wd-actor)[data-room="analysis"]')
        expect(people).to_have_count(6)
        expect(self.room.locator('.wd-room-analysis .wd-more')).to_contain_text('1명 화면 밖')
        self.room.locator('.wd-room-analysis .wd-more').click()
        visible_ids = self.room.locator('.wd-role-character').evaluate_all('rows => rows.map(row => row.dataset.ownerId)')
        hidden_index = next(index for index in range(3, 7) if 'entity:room-cap:owner-' + str(index) not in visible_ids)
        role_link = self.room.locator('#wd-detail .wd-role-item[data-role-id="role:analysis:generic:entity%3Aroom-cap%3Aowner-' + str(hidden_index) + '"]')
        role_link.click()
        selected = self.room.locator('.wd-role-character[data-owner-id="entity:room-cap:owner-' + str(hidden_index) + '"]')
        expect(selected).to_have_count(0)
        expect(people).to_have_count(6)
        expect(self.room.locator('#wd-detail')).to_contain_text('방 수용량 검사 ' + str(hidden_index))
        expect(self.room.locator('#wd-status')).to_contain_text('진행 7')
        positions = people.evaluate_all('rows => rows.map(row => row.style.transform)')
        self.assertEqual(len(positions), len(set(positions)), 'roles and bodies must not occupy the same slot')

    def test_codex_enters_on_actual_start_and_returns_to_waiting_after_completion(self):
        operation_id = '2' * 32
        row = observed_host_row('alpha', operation_id, kind='codex', state='prepared')
        row.update(started_at=None, phase='PREPARED')
        self.host_execution.rows.append(row)
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        person = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]')
        expect(person).to_have_count(0)
        row.update(state='running', phase='EXECUTE', started_at=datetime.now(timezone.utc).isoformat())
        self.page.locator('#refresh').click()
        expect(person).to_have_count(1)
        expect(person).to_have_attribute('data-walking', 'true')
        expect(person).to_have_attribute('data-display-room', 'execution', timeout=10000)
        expect(person).to_have_attribute('data-action', 'execute')
        row.update(state='succeeded', phase='COMPLETE', finished_at=datetime.now(timezone.utc).isoformat(),
                   termination_verified=True, result={'exit_code': 0, 'duration_ms': 10,
                                                       'stdout_bytes': 0, 'stderr_bytes': 0})
        self.page.locator('#refresh').click()
        expect(person).to_have_attribute('data-room', 'waiting')
        expect(person).to_have_attribute('data-working', 'false')
        expect(person).to_have_attribute('data-display-room', 'waiting', timeout=10000)
        self.page.locator('#refresh').click()
        expect(person).to_have_count(1)
        expect(self.room.locator('.wd-task[data-task-id="task:personal-ops-agent:codex-review:' + operation_id + '"]')).to_have_count(1)

    def test_failed_and_unknown_codex_remain_in_attention(self):
        row = observed_host_row('alpha', '3' * 32, kind='codex', state='failed')
        row.update(finished_at=datetime.now(timezone.utc).isoformat(), termination_verified=True)
        self.host_execution.rows.append(row)
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        person = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]')
        expect(person).to_have_count(1)
        expect(person).to_have_attribute('data-display-room', 'attention')
        unknown = observed_host_row('alpha', '4' * 32, kind='codex', state='outcome_unknown')
        unknown.update(started_at=datetime.now(timezone.utc).isoformat(),
                       finished_at=datetime.now(timezone.utc).isoformat())
        self.host_execution.rows.append(unknown)
        self.page.locator('#refresh').click()
        expect(person).to_have_attribute('data-state', 'unknown')
        expect(person).to_have_attribute('data-display-room', 'attention')

    def test_unobserved_thinking_has_no_placeholder_card_or_map_person(self):
        self.room.locator('#wd-context-details > summary').click()
        self.page.locator('#nav-workroom').click()
        expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
        brain = self.room.locator('#wd-brains [data-source-kind="chatgpt"]')
        expect(brain).to_have_count(0)
        self.assertEqual(self.room.locator('.wd-actor[data-kind="agent"]').count(), 0)
        before = self.room.locator('#wd-status').inner_text()
        self.page.locator('#refresh').click()
        expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
        expect(self.room.locator('#wd-status')).to_have_text(before)
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 900})
            expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
            self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)

    def test_map_thinking_role_follows_only_observed_reasoning_and_keeps_one_owner(self):
        self.room.locator('#wd-context-details > summary').click()
        event = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                 'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                 'task_id': 'review-spec', 'task_kind': 'agent', 'task_label': '설계 요구사항 정리',
                 'action': 'reason', 'state': 'running', 'time': datetime.now(timezone.utc).isoformat(),
                 'model': 'fixture-brain', 'reasoning_effort': 'high'}
        self.store.record_work_event('alpha', event, provenance='observed')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        thinker = self.room.locator('#wd-world .wd-thinking-character[data-owner-id="entity:chat-session:planner"]')
        expect(thinker).to_have_count(1)
        expect(thinker).to_have_attribute('data-state', 'reasoning')
        expect(thinker).to_contain_text('생각 중')
        expect(thinker).to_contain_text('fixture-brain')
        self.assertEqual(self.room.locator('.wd-actor[data-kind="agent"]').count(), 0)
        self.assertEqual(self.room.locator('#wd-work-board [data-task-id="task:chat-session:planner:review-spec"]').count(), 1)
        thinker.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('사고 역할')
        expect(self.room.locator('#wd-detail')).to_contain_text('fixture-brain')
        expect(self.room.locator('#wd-detail')).to_contain_text('설계 요구사항 정리')
        self.room.locator('#wd-motion').click()
        self.assertEqual(thinker.locator('.wd-brain-icon').evaluate(
            'icon => getComputedStyle(icon).animationName'), 'none')
        self.room.locator('#wd-motion').click()
        self.page.locator('#follow').click()
        expect(thinker).to_have_attribute('data-state', 'paused')
        expect(thinker).not_to_contain_text('생각 중')
        self.page.locator('#follow').click()
        expect(thinker).to_have_attribute('data-state', 'reasoning')
        self.page.emulate_media(reduced_motion='reduce')
        self.assertEqual(thinker.locator('.wd-brain-icon').evaluate(
            'icon => getComputedStyle(icon).animationName'), 'none')
        self.page.route('**/api/activity?*', lambda route: route.fulfill(
            status=503, content_type='application/json', body='{}'))
        self.page.locator('#refresh').click()
        expect(thinker).to_have_attribute('data-state', 'stale')
        expect(thinker).not_to_contain_text('생각 중')
        self.page.unroute('**/api/activity?*')
        self.page.locator('#refresh').click()
        expect(thinker).to_have_attribute('data-state', 'reasoning')
        self.store.record_work_event('alpha', {**event, 'state': 'succeeded',
            'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(thinker).to_have_count(1)
        expect(thinker).to_have_attribute('data-room', 'waiting')
        expect(thinker).to_have_attribute('data-working', 'false')
        expect(self.room.locator('.wd-task[data-task-id="task:chat-session:planner:review-spec"]')).to_have_count(1)
        self.page.locator('#project').select_option('beta')
        expect(thinker).to_have_count(0)
        expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
        expect(self.room.locator('#wd-brains')).not_to_contain_text('fixture-brain')

    def test_empty_map_does_not_schedule_animation_without_role_activity(self):
        self.page.locator('#nav-workroom').click()
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#wd-status')).to_have_attribute('title', re.compile('객체 0'))
        expect(self.room.locator('#wd-world .wd-thinking-character')).to_have_count(0)
        self.room_page.evaluate('''() => {
            window.originalThinkingRaf = window.requestAnimationFrame;
            window.thinkingFrames = 0;
            window.requestAnimationFrame = callback => {
                window.thinkingFrames++;
                return window.originalThinkingRaf(callback);
            };
        }''')
        try:
            self.page.wait_for_timeout(150)
            before = self.room_page.evaluate('window.thinkingFrames')
            self.page.wait_for_timeout(350)
            self.assertEqual(self.room_page.evaluate('window.thinkingFrames'), before,
                             'empty map must not keep a render loop running')
        finally:
            self.page.evaluate('() => { window.requestAnimationFrame = window.originalThinkingRaf; }')

    def test_map_thinking_role_keeps_reasoning_visible_during_same_owner_code_work(self):
        self.room.locator('#wd-context-details > summary').click()
        now = datetime.now(timezone.utc)
        common = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                  'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_kind': 'agent', 'state': 'running', 'model': 'fixture-brain'}
        self.store.record_work_event('alpha', {
            **common, 'task_id': 'reason-spec', 'task_label': '설계 판단', 'action': 'reason',
            'time': (now - timedelta(seconds=1)).isoformat()}, provenance='observed')
        self.store.record_work_event('alpha', {
            **common, 'task_id': 'code-screen', 'task_label': '화면 코드 수정', 'action': 'code',
            'time': now.isoformat(), 'model': 'fixture-executor'}, provenance='observed')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        thinker = self.room.locator('#wd-world .wd-thinking-character[data-owner-id="entity:chat-session:planner"]')
        expect(thinker).to_have_attribute('data-state', 'reasoning')
        expect(thinker).to_contain_text('fixture-brain')
        expect(thinker).not_to_contain_text('fixture-executor')
        brain = self.room.locator('#wd-brains [data-actor-id="entity:chat-session:planner"]')
        expect(brain).to_have_attribute('data-state', 'reasoning')
        expect(brain).to_contain_text('fixture-brain')
        executor = self.room.locator('.wd-actor[data-actor-id="entity:chat-session:planner"]')
        expect(executor).to_have_count(1)
        expect(executor).to_have_attribute('data-action', 'code')
        thinker.click()
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('설계 판단')
        expect(self.room.locator('#wd-detail')).to_contain_text('fixture-brain')
        expect(self.room.locator('#wd-detail')).not_to_contain_text('fixture-executor')
        link = self.room.locator('#wd-detail .wd-task-link[data-task-id="task:chat-session:planner:reason-spec"]')
        expect(link).to_contain_text('실제 업무 보기')
        link.click()
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('설계 판단')
        self.assertEqual(self.room.locator('#wd-world .wd-thinking-character.selected').count(), 1)

    def test_map_thinking_role_labels_reported_reasoning_without_live_animation(self):
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
            'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
            'task_id': 'reported-spec', 'task_kind': 'agent', 'task_label': '보고된 설계 정리',
            'action': 'reason', 'state': 'running', 'time': datetime.now(timezone.utc).isoformat()
        }, provenance='reported')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        thinker = self.room.locator('#wd-world .wd-thinking-character[data-owner-id="entity:chat-session:planner"]')
        expect(thinker).to_have_count(1)
        expect(thinker).not_to_have_attribute('data-state', 'reasoning')
        expect(thinker).not_to_contain_text('생각 중')
        thinker.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('보고')

    def test_map_thinking_role_does_not_treat_host_process_as_reasoning(self):
        self.room.locator('#wd-context-details > summary').click()
        self.host_execution.rows.append(observed_host_row('alpha', 'c' * 32, kind='codex'))
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        expect(self.room.locator('#wd-world .wd-thinking-character[data-source-kind="codex-cli"]')).to_have_count(0)
        brain = self.room.locator('#wd-brains [data-source-kind="codex-cli"]')
        expect(brain).not_to_have_attribute('data-state', 'reasoning')
        expect(brain).to_contain_text('설정값')
        expect(brain).not_to_contain_text('생각 중')
        self.assertEqual(self.room.locator('.wd-actor[data-kind="agent"]').count(), 1)
        expect(self.room.locator('.wd-room-execution .wd-room-count')).to_have_text('1')
        expect(self.room.locator('.wd-room-coding .wd-room-count')).to_have_text('0')

    def test_workroom_explains_observed_reasoning_and_explicit_work_links(self):
        self.room.locator('#wd-context-details > summary').click()
        now = datetime.now(timezone.utc).isoformat()
        parent = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                  'actor_id': 'planner', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_id': 'review-spec', 'task_kind': 'agent', 'task_label': '설계 요구사항 정리',
                  'action': 'reason', 'state': 'running', 'time': now,
                  'model': 'fixture-brain', 'reasoning_effort': 'high'}
        child = {'schema_version': 1, 'source_id': 'figma-cloud', 'source_kind': 'mcp',
                 'actor_id': 'figma', 'actor_kind': 'mcp', 'actor_label': 'Figma',
                 'task_id': 'frames', 'task_kind': 'call',
                 'task_label': '화면 프레임 조회 <img src=x onerror=window.flow_pwned=1>',
                 'action': 'read', 'state': 'running', 'time': now,
                 'parent_source_id': 'chat-session', 'parent_actor_id': 'planner',
                 'parent_task_id': 'review-spec'}
        self.store.record_work_event('alpha', parent, provenance='observed')
        self.store.record_work_event('alpha', child, provenance='observed')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        brain = self.room.locator('#wd-brains .wd-brain-card[data-actor-id="entity:chat-session:planner"]')
        expect(brain).to_have_count(1)
        expect(brain).to_have_attribute('data-state', 'reasoning')
        expect(brain).to_contain_text('생각 중')
        expect(brain).to_contain_text('fixture-brain')
        self.room.locator('#wd-motion').click()
        self.assertEqual(brain.locator('.wd-brain-icon').evaluate(
            'icon => getComputedStyle(icon).animationName'), 'none')
        self.room.locator('#wd-motion').click()
        item = self.room.locator('#wd-work-board .wd-work-item[data-task-id="task:figma-cloud:figma:frames"]')
        expect(item).to_contain_text('Figma')
        expect(item).to_contain_text('화면 프레임 조회')
        expect(item).to_contain_text('읽')
        item.click()
        expect(item).to_be_focused()
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('화면 프레임 조회')
        self.assertEqual(self.room.locator('#wd-work-board img').count(), 0)
        self.assertTrue(self.page.evaluate('window.flow_pwned === undefined'))
        parent_link = self.room.locator('#wd-detail .wd-task-link[data-task-id="task:chat-session:planner:review-spec"]')
        expect(parent_link).to_contain_text('설계 요구사항 정리')
        parent_link.click()
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('설계 요구사항 정리')
        expect(self.room.locator('#wd-detail .wd-task-link[data-task-id="task:figma-cloud:figma:frames"]')).to_be_visible()
        self.page.set_viewport_size({'width': 320, 'height': 900})
        self.assertEqual(self.page.evaluate('document.documentElement.scrollWidth'), 320)
        self.store.record_work_event('alpha', {**parent, 'state': 'succeeded',
                                     'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(brain).not_to_have_attribute('data-state', 'reasoning')
        expect(brain).not_to_contain_text('생각 중')
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#wd-brains')).not_to_contain_text('fixture-brain')
        expect(self.room.locator('#wd-work-board')).not_to_contain_text('화면 프레임 조회')

    def test_host_codex_has_readable_work_without_claiming_internal_actions(self):
        self.room.locator('#wd-context-details > summary').click()
        operation_id = '6' * 32
        self.page.emulate_media(reduced_motion='reduce')
        self.host_execution.rows.append(observed_host_row('alpha', operation_id, kind='codex'))
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        brain = self.room.locator('#wd-brains .wd-brain-card[data-source-kind="codex-cli"]')
        expect(brain).to_have_count(1)
        expect(brain).to_contain_text('fixture-codex')
        expect(brain).to_contain_text('설정')
        expect(brain).not_to_contain_text('생각 중')
        expect(self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]')).to_have_attribute('data-action', 'execute')
        item = self.room.locator('#wd-work-board .wd-work-item[data-task-id="task:personal-ops-agent:codex-review:' + operation_id + '"]')
        expect(item).to_contain_text('Codex')
        expect(item).to_contain_text('실행')
        expect(item).to_contain_text('세부')
        item.click()
        summary = self.room.locator('#wd-detail .wd-detail-summary')
        expect(summary).to_contain_text('Codex')
        expect(summary).to_contain_text('세부')
        expect(summary).not_to_contain_text(operation_id)
        expect(summary).not_to_contain_text('코드 수정 중')
        expect(self.room.locator('#wd-open-log')).to_be_visible()
        self.assertEqual(self.room.locator('#wd-detail .wd-task-link').count(), 0)
        self.host_execution.rows[-1].update(
            state='succeeded', phase='COMPLETE', termination_verified=True,
            finished_at=datetime.now(timezone.utc).isoformat(),
            result={'exit_code': 0, 'duration_ms': 480000, 'stdout_bytes': 0, 'stderr_bytes': 0})
        self.page.locator('#refresh').click()
        task_id = 'task:personal-ops-agent:codex-review:' + operation_id
        expect(self.room.locator('.wd-task[data-task-id="' + task_id + '"] .wd-task-icon')).to_have_text('>_')
        expect(self.room.locator('.wd-change[data-task-id="' + task_id + '"] .wd-change-action')).to_have_text('>_')

    def test_work_board_preserves_explicit_host_agent_work_descriptions(self):
        self.room.locator('#wd-context-details > summary').click()
        operation_id = '7' * 32
        self.host_execution.rows.append(observed_host_row('alpha', operation_id, kind='codex'))
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'personal-ops-agent', 'source_kind': 'codex-cli',
            'actor_id': 'codex-review', 'actor_kind': 'agent', 'actor_label': 'Codex 검토 담당',
            'task_id': operation_id, 'task_kind': 'agent', 'task_label': '타입 오류 원인 검토',
            'action': 'review', 'state': 'running', 'time': datetime.now(timezone.utc).isoformat(),
        }, provenance='observed')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        item = self.room.locator('#wd-work-board [data-task-id="task:personal-ops-agent:codex-review:' + operation_id + '"]')
        expect(item).to_contain_text('타입 오류 원인 검토')
        expect(item.locator('.wd-work-action')).to_contain_text('검토')
        expect(item).not_to_contain_text('세부 행동 미수신')
        item.click()
        expect(self.room.locator('#wd-detail .wd-detail-summary')).to_contain_text('타입 오류 원인 검토')

    def test_work_board_separates_waiting_history_unknown_and_paused_observations(self):
        self.room.locator('#wd-context-details > summary').click()
        event = {'schema_version': 1, 'source_id': 'figma-cloud', 'source_kind': 'mcp',
                 'actor_id': 'figma', 'actor_kind': 'mcp', 'actor_label': 'Figma',
                 'task_id': 'waiting', 'task_kind': 'call', 'task_label': '프레임 조회 예약',
                 'action': 'read', 'state': 'queued', 'time': datetime.now(timezone.utc).isoformat()}
        self.store.record_work_event('alpha', event, provenance='observed')
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        item = self.room.locator('#wd-work-board [data-task-id="task:figma-cloud:figma:waiting"]')
        expect(item.locator('.wd-work-state')).to_contain_text('대기')
        expect(item.locator('.wd-work-action')).not_to_contain_text('중')
        self.store.record_work_event('alpha', {**event, 'state': 'returned',
                                     'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(item.locator('.wd-work-state')).to_contain_text('이전 기록')
        expect(item.locator('.wd-work-action')).not_to_contain_text('중')
        unknown = {**event, 'task_id': 'unknown', 'task_label': '이전 조회 활동', 'state': 'running',
                   'time': (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}
        self.store.record_work_event('alpha', unknown, provenance='observed')
        self.page.locator('#refresh').click()
        unknown_item = self.room.locator('#wd-work-board [data-task-id="task:figma-cloud:figma:unknown"]')
        expect(unknown_item.locator('.wd-work-state')).not_to_contain_text('현재')
        expect(unknown_item.locator('.wd-work-action')).not_to_contain_text('중')
        self.store.record_work_event('alpha', {**event, 'state': 'running',
                                     'time': datetime.now(timezone.utc).isoformat()}, provenance='observed')
        self.page.locator('#refresh').click()
        expect(item.locator('.wd-work-state')).to_contain_text('현재')
        self.page.locator('#follow').click()
        expect(item.locator('.wd-work-state')).not_to_contain_text('현재')

    def test_work_board_keeps_unstarted_host_plans_ahead_of_completed_history(self):
        self.room.locator('#wd-context-details > summary').click()
        operation_id = '8' * 32
        prepared = observed_host_row('alpha', operation_id, state='prepared')
        prepared.update(started_at=None, phase='PREPARED', termination_verified=None)
        self.host_execution.rows.append(prepared)
        for digit in 'bcde':
            failed = observed_host_row('alpha', digit * 32, state='failed')
            failed.update(finished_at=datetime.now(timezone.utc).isoformat(), phase='COMPLETE')
            self.host_execution.rows.append(failed)
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        plan = self.room.locator('#wd-work-board [data-task-id="task:personal-ops-host:host-executor:' + operation_id + '"]')
        expect(plan).to_be_visible()
        expect(plan.locator('.wd-work-state')).to_contain_text('실행 전')

    def test_selected_work_explains_stale_reads_and_paused_observation(self):
        self.room.locator('#wd-context-details > summary').click()
        operation_id = '9' * 32
        self.host_execution.rows.append(observed_host_row('alpha', operation_id, kind='codex'))
        self.page.locator('#nav-workroom').click()
        self.page.locator('#refresh').click()
        self.room.locator('#wd-work-board [data-task-id="task:personal-ops-agent:codex-review:' + operation_id + '"]').click()
        summary = self.room.locator('#wd-detail .wd-detail-summary')
        expect(summary).to_contain_text('현재')
        self.page.locator('#follow').click()
        expect(self.room.locator('#wd-status')).to_contain_text('일시정지')
        expect(summary).not_to_contain_text('현재')
        self.page.locator('#follow').click()
        expect(summary).to_contain_text('현재')
        self.page.route('**/api/activity?*', lambda route: route.fulfill(
            status=503, content_type='application/json', body='{}'))
        self.page.locator('#refresh').click()
        expect(self.room.locator('#work-map')).to_have_attribute('data-stale', 'true')
        expect(summary).not_to_contain_text('현재')

    def test_paused_grant_expiry_updates_detail_buttons_without_host_requests(self):
        requested = self.host_access.request('alpha')
        grant = self.host_access.approve_web(
            'alpha', request_id=requested['request_id'], duration_seconds=3600,
            allow_codex=True, confirmation='alpha')
        self.page.locator('#nav-access').click()
        expect(self.page.locator('#overview-access-state')).to_have_text(re.compile(r'^활성 · '))
        self.page.locator('#follow').click()
        self.page.locator('#host-access-confirmation').fill('alpha')
        token = (self.root / 'config' / 'token').read_text().strip()
        self.page.locator('#host-access-token').fill(token)
        expect(self.page.locator('#host-access-revoke')).to_be_enabled()
        host_reads = []
        self.page.on('request', lambda request: host_reads.append(urlsplit(request.url).path)
                     if urlsplit(request.url).path.startswith('/api/host-') else None)
        expired_now = int(datetime.fromisoformat(grant['expires_at']).timestamp() * 1000) + 1000
        self.page.evaluate('(now) => { Date.now = () => now; }', expired_now)
        expect(self.page.locator('#host-access-state')).to_contain_text('만료')
        expect(self.page.locator('#overview-access-state')).to_have_text('액세스 만료됨')
        expect(self.page.locator('#overview-codex-state')).to_have_text(
            '호스트 Codex 허용 안 함 · 격리 준비 상태와 별개')
        expect(self.page.locator('#host-access-revoke')).to_be_disabled()
        self.assertEqual(self.page.locator('#host-access-confirmation').input_value(), 'alpha')
        self.assertEqual(self.page.locator('#host-access-token').input_value(), token)
        self.assertEqual(host_reads, [])

    def test_host_observation_drives_long_running_work_and_completion_without_agent_poll(self):
        operation_id = '2' * 32
        row = observed_host_row('alpha', operation_id, kind='codex')
        self.host_execution.rows.append(row)
        self.store.record_work_event('alpha', {
            'schema_version': 1, 'source_id': 'personal-ops-agent', 'source_kind': 'codex-cli',
            'actor_id': 'codex-review', 'actor_kind': 'agent', 'actor_label': 'Codex host agent',
            'task_id': operation_id, 'task_kind': 'agent', 'task_label': 'Host Codex task 22222222',
            'action': 'code', 'state': 'running', 'time': row['started_at'],
            'model': 'fixture-codex', 'model_source': 'configuration',
            'reasoning_effort': 'high', 'reasoning_effort_source': 'configuration'},
            provenance='observed')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-agent:codex-review"]')
        expect(actor).to_have_count(1)
        expect(actor).to_have_attribute('data-state', 'running')
        actor.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('fixture-codex')
        expect(self.room.locator('#wd-detail')).to_contain_text('high')
        self.page.emulate_media(reduced_motion='reduce')
        row.update(state='succeeded', phase='COMPLETE',
                   finished_at=datetime.now(timezone.utc).isoformat(), termination_verified=True,
                   result={'exit_code': 0, 'duration_ms': 480000, 'stdout_bytes': 0, 'stderr_bytes': 0})
        self.page.locator('#refresh').click()
        expect(actor).to_have_count(1)
        expect(actor).to_have_attribute('data-room', 'waiting')
        expect(self.room.locator('#wd-detail')).to_contain_text('완료')
        expect(self.room.locator('.wd-task[data-task-id="task:personal-ops-agent:codex-review:' + operation_id + '"]')).to_have_count(1)

    def test_host_task_opens_its_host_log_and_mobile_views_do_not_overflow(self):
        operation_id = '3' * 32
        self.host_execution.rows.append(observed_host_row('alpha', operation_id))
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor').filter(has=self.room.locator('.wd-actor-label').filter(has_text='호스트'))
        expect(actor).to_have_count(1)
        actor.click()
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#view-executions')).to_be_visible()
        expect(self.page.locator('#host-execution')).to_have_value(operation_id)
        expect(self.page.locator('#host-execution-log')).to_contain_text('fixture host output <script>')
        self.assertIsNone(self.page.evaluate('window.host_pwned'))
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 844})
            for view in ('overview', 'workroom', 'executions', 'access', 'repository'):
                self.page.locator('#nav-' + view).click()
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width,
                                     f'{view} overflows at {width}px')

    def test_auth_text_safety_incremental_logs_and_project_switch(self):
        self.assertNotIn('#', self.page.url)
        self.assertEqual(self.page.evaluate('Object.keys(sessionStorage).length'), 0)
        self.assertEqual(self.page.evaluate('JSON.stringify(localStorage)'), '{"personal_ops_project":"alpha"}')
        expect(self.page.locator('#log')).to_contain_text('start')
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.assertEqual(self.page.locator('#jobs img, #log img').count(), 0)
        expect(self.page.locator('#activity')).to_contain_text('read_file')
        self.page.locator('#follow').click()
        with self.log.open('a') as stream:
            stream.write('new output\n')
        self.page.locator('#refresh').click()
        expect(self.page.locator('#log')).to_contain_text('new output')
        self.assertEqual(self.page.locator('#log').inner_text().count('start'), 1)
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#memory')).to_have_value('beta memory')
        expect(self.page.locator('#jobs')).to_have_text('저장된 실행 작업이 없습니다.')
        expect(self.page.locator('#log')).to_have_text('')

    def test_memory_conflict_preserves_draft_and_success_persists(self):
        self.page.locator('#nav-repository').click()
        self.page.locator('#memory').fill('my draft')
        self.store.update_memory('alpha', 'another writer', 1)
        self.page.locator('#save').click()
        expect(self.page.locator('#notice')).to_contain_text('다른 곳에서')
        expect(self.page.locator('#memory')).to_have_value('my draft')
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#reload-memory').click()
        expect(self.page.locator('#memory')).to_have_value('another writer')
        self.page.locator('#memory').fill('merged notes')
        self.page.locator('#save').click()
        expect(self.page.locator('#memory-status')).to_contain_text('v3 저장됨')
        self.assertEqual(self.store.read_memory('alpha')['content'], 'merged notes')

    def test_dirty_project_switch_can_be_canceled(self):
        self.page.locator('#nav-repository').click()
        self.page.locator('#memory').fill('unsaved')
        self.page.once('dialog', lambda dialog: dialog.dismiss())
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#project')).to_have_value('alpha')
        expect(self.page.locator('#memory')).to_have_value('unsaved')

    def test_bookmark_resume_and_logout(self):
        cookies = self.context.cookies()
        self.assertEqual(len(cookies), 1)
        self.assertTrue(cookies[0]['httpOnly'])
        self.assertEqual(cookies[0]['sameSite'], 'Strict')
        self.assertGreater(cookies[0]['expires'], 0)
        self.page.reload()
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#memory')).to_have_value('<script>window.pwned=1</script>')
        self.page.locator('.toolbar-details').evaluate('node => node.open = true')
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        self.page.reload()
        expect(self.page.locator('#connection')).to_have_text('로그인 필요')
        expect(self.page.locator('#dashboard')).to_be_hidden()

    def test_logout_locks_edits_while_response_is_pending(self):
        pending = []
        self.page.route('**/api/session', lambda route: pending.append(route))
        self.page.locator('.toolbar-details').evaluate('node => node.open = true')
        self.page.locator('#logout').click()
        expect(self.page.locator('#memory')).to_be_disabled()
        self.assertTrue(self.page.locator('#dashboard').evaluate('(element) => element.inert'))
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(response=pending[0].fetch())
        expect(self.page.locator('#dashboard')).to_be_hidden()

    def test_initial_memory_load_disables_concurrent_reload_and_editing(self):
        self.page.locator('#nav-repository').click()
        pending = []
        self.page.route('**/api/memory?project=beta', lambda route: pending.append(route))
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#memory')).to_be_disabled()
        expect(self.page.locator('#reload-memory')).to_be_disabled()
        expect(self.page.locator('#project')).to_have_value('beta')
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(response=pending[0].fetch())
        expect(self.page.locator('#memory')).to_be_enabled()
        expect(self.page.locator('#memory')).to_have_value('beta memory')
        self.page.locator('#memory').fill('safe draft')
        expect(self.page.locator('#save')).to_be_enabled()

    def test_work_summary_diff_checks_and_handoff(self):
        self.page.locator('#nav-repository').click()
        expect(self.page.locator('#work-title')).to_have_text('화면 작업')
        expect(self.page.locator('#completed')).to_contain_text('화면 분석')
        expect(self.page.locator('#next-steps')).to_contain_text('타입 검사')
        expect(self.page.locator('#checks')).to_contain_text('typecheck')
        expect(self.page.locator('#checks')).to_contain_text('성공')
        self.page.locator('#changes button').filter(has_text='code.txt').click()
        expect(self.page.locator('#diff')).to_contain_text('+after <script>')
        self.assertEqual(self.page.locator('#diff script').count(), 0)
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.assertNotIn('.env', self.page.locator('#changes').inner_text())
        self.page.locator('#diff-mode').select_option('true')
        expect(self.page.locator('#diff-status')).to_contain_text('선택한 범위에 변경 내용이 없습니다')
        self.page.locator('#make-handoff').click()
        expect(self.page.locator('#handoff')).to_have_value(re.compile('.*화면 작업.*', re.DOTALL))
        self.assertNotIn('start\n<img', self.page.locator('#handoff').input_value())
        self.assertNotIn('SECRET=', self.page.locator('#handoff').input_value())
        # The LAN HTTP path cannot assume navigator.clipboard exists.
        self.page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true})")
        self.page.locator('#copy-handoff').click()
        expect(self.page.locator('#handoff-status')).to_contain_text('복사')
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-title')).to_have_text('기록된 작업 목표가 없습니다')
        expect(self.page.locator('#handoff')).to_have_value('')
        self.page.reload()
        expect(self.page.locator('#project')).to_have_value('beta')

    def test_unreported_work_still_shows_observed_execution_without_inventing_progress(self):
        jobs = self.root / 'state' / 'jobs'
        for number, state, code in ((1, 'succeeded', 0), (2, 'failed', 2), (3, 'running', None)):
            record = {'job_id': f'{number:032x}', 'project': 'beta', 'state': state,
                      'created_at': f'2026-10-09T00:00:0{number}Z', 'exit_code': code,
                      'preset': 'typecheck', 'relative_cwd': 'server'}
            target = jobs / (record['job_id'] + '.json')
            target.write_text(json.dumps(record))
            target.chmod(0o600)
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-title')).to_have_text('기록된 작업 목표가 없습니다')
        self.page.locator('#nav-overview').click()
        expect(self.page.locator('#observed-work')).to_be_visible()
        expect(self.page.locator('#observed-status')).to_contain_text('최근 3개')
        for text in ('성공 1', '실패 1', '진행 중 1'):
            expect(self.page.locator('#observed-status')).to_contain_text(text)
        expect(self.page.locator('#recent-work .job')).to_have_count(3)
        expect(self.page.locator('#recent-work')).to_contain_text('typecheck')
        expect(self.page.locator('#recent-work')).to_contain_text('server')
        self.assertEqual(self.store.read_progress('beta')['revision'], 0)
        self.page.locator('#nav-overview').click()
        self.page.locator('#recent-work .job').first.click()
        expect(self.page.locator('#execution-details')).to_have_attribute('open', '')
        expect(self.page.locator('#log-status')).to_contain_text('실행 중')
        self.page.locator('#project').select_option('alpha')
        expect(self.page.locator('#work-title')).to_have_text('화면 작업')
        expect(self.page.locator('#completed')).to_contain_text('화면 분석')
        expect(self.page.locator('#recent-work .job')).to_have_count(1)

    def test_work_rooms_follow_job_state_and_open_real_output(self):
        actor = self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(actor).to_have_attribute('data-room', 'waiting')
        job_path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(job_path.read_text())
        record.update(state='running', exit_code=None)
        job_path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        expect(actor).to_have_attribute('data-room', 'testing')
        expect(self.room.locator('#wd-observation')).to_contain_text('진행 중 1건')
        actor.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('typecheck')
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')
        expect(self.page.locator('#log-status')).to_contain_text('실행 중')
        record.update(state='failed', exit_code=2)
        job_path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        expect(actor).to_have_attribute('data-room', 'attention')
        expect(self.room.locator('#wd-detail')).to_contain_text('실패')

    def test_work_objects_dont_multiply_as_completed_tasks_accumulate(self):
        for number in range(8):
            self.store.record_activity('alpha', 'replace_content', 'completed', f'entity-{number}')
        self.page.locator('#refresh').click()
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(2)
        expect(self.room.locator('#work-map .wd-actor-label').filter(has_text='코딩 MCP')).to_have_count(1)
        expect(self.room.locator('#work-map .wd-actor[data-room=done]')).to_have_count(0)
        expect(self.room.locator('.wd-room-done .wd-room-count')).to_have_text('10')
        expect(self.room.locator('.wd-task[data-room=done]')).to_have_count(6)
        self.room.locator('.wd-room-done .wd-more').click()
        expect(self.room.locator('#wd-detail .wd-detail-item')).to_have_count(10)
        self.room.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').click()
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')

    def test_work_objects_show_distinct_live_actions_and_idle_has_none(self):
        self.page.emulate_media(reduced_motion='reduce')
        runner = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(path.read_text())
        record.update(state='running', exit_code=None, preset='web:build')
        path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        expect(runner).to_have_attribute('data-action', 'build')
        expect(runner).to_have_attribute('data-working', 'true')
        expect(runner.locator('.wd-actor-label')).to_have_text('명령 실행기')
        self.store.record_activity('alpha', 'read_file', 'started', 'action-read')
        self.page.locator('#refresh').click()
        mcp = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(mcp).to_have_attribute('data-action', 'read')
        expect(mcp).to_have_attribute('data-working', 'true')
        self.store.record_activity('alpha', 'read_file', 'completed', 'action-read')
        self.page.locator('#refresh').click()
        expect(mcp).to_have_attribute('data-state', 'idle')
        expect(mcp).to_have_attribute('data-working', 'false')
        expect(mcp.locator('.wd-actor-label')).to_have_text('코딩 MCP')

    def test_direct_build_metadata_reaches_the_visible_work_action(self):
        self.page.emulate_media(reduced_motion='reduce')
        path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(path.read_text())
        record.update(state='running', exit_code=None, preset=None, label='직접 명령 실행', argv=['npm','run','build'])
        path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        runner = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(runner).to_have_attribute('data-action', 'build')
        expect(runner.locator('.wd-actor-task')).to_contain_text('빌드')

    def test_versioned_cli_sources_render_separate_explicit_identities(self):
        self.room_page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'cli-fixture'; document.body.append(root);
            const common = {schema_version:1, project:'alpha', actor_id:'session-1', actor_kind:'agent',
              actor_label:'Agent', task_id:'work-1', task_kind:'agent', task_label:'코드 수정', action:'code',
              state:'running', time:new Date().toISOString()};
            window.cliFixture = window.WorkDashboard.create(root);
            window.cliFixture.update({project:'alpha', records:[
              {...common, source_id:'codex-local', source_kind:'codex-cli'},
              {...common, source_id:'claude-local', source_kind:'claude-cli'}]});
        }""")
        actors = self.room.locator('#cli-fixture .wd-actor')
        expect(actors).to_have_count(2)
        names = self.room.locator('#cli-fixture .wd-actor-label').all_text_contents()
        self.assertEqual(len(set(names)), 2)
        self.assertEqual(set(actors.evaluate_all('nodes => nodes.map(node => node.dataset.kind)')), {'agent'})
        self.room_page.evaluate('window.cliFixture.destroy()')

    def test_other_mcp_and_model_metadata_reach_map_through_the_reporting_endpoint(self):
        self.room.locator('#wd-context-details > summary').click()
        common = {'schema_version': 1, 'source_id': 'chat-session', 'source_kind': 'chatgpt',
                  'actor_id': 'agent-1', 'actor_kind': 'agent', 'actor_label': 'ChatGPT',
                  'task_id': 'turn-1', 'task_kind': 'agent', 'task_label': '프레임 분석',
                  'action': 'reason', 'state': 'running', 'model': 'fixture-model',
                  'reasoning_effort': 'high', 'usage': {'input_tokens': 25, 'output_tokens': 0},
                  'usage_scope': 'request', 'provenance': 'observed'}
        mcp = {key: value for key, value in common.items() if key not in {'model', 'reasoning_effort', 'usage', 'usage_scope'}}
        mcp.update(source_id='figma-cloud', source_kind='mcp', actor_id='figma', actor_kind='mcp',
                   actor_label='Figma', task_id='fetch-1', task_kind='call', action='read',
                   parent_source_id='chat-session', parent_actor_id='agent-1', parent_task_id='turn-1')
        response = self.page.evaluate("""async events => {
            const result = await fetch('/api/work-events?project=alpha', {method:'POST',
              headers:{'Content-Type':'application/json'}, body:JSON.stringify({events})});
            return result.status;
        }""", [common, mcp])
        self.assertEqual(response, 200)
        self.page.locator('#refresh').click()
        agent = self.room.locator('#work-map .wd-role-character[data-owner-id="entity:chat-session:agent-1"]')
        expect(agent).to_have_attribute('data-action', 'reason')
        expect(agent).to_have_attribute('data-provenance', 'reported')
        agent.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('fixture-model')
        expect(self.room.locator('#wd-detail')).to_contain_text('high')
        expect(self.room.locator('#wd-detail')).to_contain_text('요청 단위')
        expect(self.room.locator('#wd-detail')).to_contain_text('출력 0')
        expect(self.room.locator('#wd-detail')).to_contain_text('보고됨')
        expect(self.room.locator('#wd-pipeline')).to_contain_text('fixture-model')
        figma = self.room.locator('#work-map .wd-actor[data-actor-id="entity:figma-cloud:figma"]')
        expect(figma.locator('.wd-actor-label')).to_have_text('Figma')
        figma.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('task:chat-session:agent-1:turn-1')
        self.assertEqual(self.page.evaluate('window.pwned'), None)

    def test_absent_metadata_and_expired_reports_do_not_claim_reasoning_or_live_work(self):
        self.room_page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'expiry-fixture'; document.body.append(root);
            window.expiryFixture = window.WorkDashboard.create(root);
            window.expiryFixture.update({project:'alpha', records:[{schema_version:1, project:'alpha',
              source_id:'chat-session', source_kind:'chatgpt', actor_id:'agent-1', actor_kind:'agent',
              actor_label:'ChatGPT', task_id:'turn-1', task_kind:'agent', task_label:'작업', action:'execute',
              state:'running', time:new Date(Date.now()-180000).toISOString(), thinking:{enabled:true}}]});
        }""")
        expect(self.room.locator('#expiry-fixture .wd-actor')).to_have_count(0)
        expect(self.room.locator('#expiry-fixture .wd-role-character')).to_have_count(0)
        actor = self.room.locator('#expiry-fixture .wd-brain-card[data-actor-id="entity:chat-session:agent-1"]')
        expect(actor).to_have_attribute('data-state', 'unknown')
        self.room.locator('#expiry-fixture #wd-context-details > summary').click()
        actor.click()
        expect(self.room.locator('#expiry-fixture .wd-fields')).to_contain_text('모델')
        expect(self.room.locator('#expiry-fixture .wd-fields')).to_contain_text('미수신')
        expect(self.room.locator('#expiry-fixture .wd-pipeline')).to_contain_text('모델·추론 상태 미수신')
        self.room_page.evaluate('window.expiryFixture.destroy()')

    def test_pruned_call_history_keeps_the_known_object_without_claiming_active_work(self):
        self.page.route('**/api/activity?project=alpha', lambda route: route.fulfill(json={'project':'alpha', 'events':[]}))
        self.page.locator('#refresh').click()
        mcp = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(2)
        expect(mcp).to_have_attribute('data-state', 'idle')
        expect(mcp).to_have_attribute('data-room', 'waiting')
        expect(mcp).to_have_attribute('data-working', 'false')
        expect(self.room.locator('#wd-status')).to_contain_text('진행 0')
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(0)

    def test_archived_work_of_room_evicted_character_remains_inspectable(self):
        self.room_page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'cap-fixture'; document.body.append(root);
            const records = Array.from({length:25}, (_, index) => ({schema_version:1, project:'alpha',
              source_id:'codex-local', source_kind:'codex-cli', actor_id:'agent-'+String(index).padStart(2,'0'),
              actor_kind:'agent', actor_label:'Agent '+index, task_id:'work-1', task_kind:'agent',
              task_label:'record-'+index, action:'code', state:'succeeded',
              time:new Date(Date.now()+index*1000).toISOString()}));
            window.capFixture = window.WorkDashboard.create(root);
            window.capFixture.update({project:'alpha', records});
        }""")
        expect(self.room.locator('#cap-fixture .wd-actor[data-actor-id="entity:codex-local:agent-00"]')).to_have_count(0)
        expect(self.room.locator('#cap-fixture .wd-actor[data-actor-id="entity:codex-local:agent-24"]')).to_have_count(1)
        expect(self.room.locator('#cap-fixture .wd-actor[data-room="waiting"]')).to_have_count(6)
        self.room.locator('#cap-fixture .wd-room-done .wd-more').click()
        self.room.locator('#cap-fixture #wd-detail .wd-detail-item[data-task-id="task:codex-local:agent-00:work-1"]').click()
        expect(self.room.locator('#cap-fixture .wd-inspector')).to_contain_text('record-0')
        expect(self.room.locator('#cap-fixture .wd-inspector')).to_contain_text('작업 완료')
        expect(self.room.locator('#cap-fixture .wd-inspector')).to_contain_text('entity:codex-local:agent-00')
        self.room_page.evaluate('window.capFixture.destroy()')

    def test_work_map_displays_call_return_without_completing_its_job(self):
        self.store.record_activity('alpha', 'start_command', 'completed', 'call-with-job')
        path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(path.read_text())
        record.update(state='running', exit_code=None)
        path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        call = self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(call).to_have_attribute('data-room', 'waiting')
        call.click()
        expect(self.room.locator('#wd-detail')).to_contain_text('반환')
        expect(self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')).to_have_attribute('data-room', 'testing')
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.assertEqual(self.room.locator('#work-map img, #work-map script').count(), 0)

    def test_work_map_clears_project_and_marks_failed_observation(self):
        expect(self.room.locator('#work-map .wd-actor')).not_to_have_count(0)
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(0)
        expect(self.room.locator('#wd-project')).to_have_text('beta')
        self.page.route('**/api/jobs?project=beta', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.locator('#refresh').click()
        expect(self.room.locator('#wd-status')).to_contain_text('조회 실패')
        expect(self.room.locator('#work-map')).to_have_attribute('data-stale', 'true')
        self.page.locator('.toolbar-details').evaluate('node => node.open = true')
        self.page.locator('#logout').click()
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_map_keeps_last_observed_people_when_refresh_fails(self):
        actor = self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(actor).to_have_attribute('data-state', 'idle')
        self.page.route('**/api/jobs?project=alpha', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.route('**/api/activity?project=alpha', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.locator('#refresh').click()
        expect(self.room.locator('#work-map')).to_have_attribute('data-stale', 'true')
        expect(actor).to_have_attribute('data-state', 'idle')
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_rooms_render_people_and_replay_a_quick_observed_call(self):
        avatar = self.room.locator('#work-map .wd-actor .wd-avatar').first
        self.assertTrue(avatar.evaluate("""node => node.tagName === 'CANVAS' &&
            [...node.getContext('2d').getImageData(0, 0, node.width, node.height).data]
                .filter((value, index) => index % 4 === 3 && value > 0).length > 50"""))
        self.assertEqual(self.room.locator('#work-map .wd-actor[data-presentation=replay]').count(), 0)
        self.store.record_activity('alpha', 'read_file', 'started', 'quick-observed')
        self.store.record_activity('alpha', 'read_file', 'completed', 'quick-observed')
        self.page.locator('#refresh').click()
        actor = self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(actor).to_have_attribute('data-state', 'idle')
        expect(actor).to_have_attribute('data-room', 'waiting')
        expect(actor).to_have_attribute('data-presentation', 'replay')
        expect(self.room.locator('#wd-status')).to_contain_text('진행 0')
        expect(self.room.locator('#wd-observation')).to_contain_text('기록 재생')
        initial_x = actor.evaluate('node => node.getBoundingClientRect().x')
        self.room_page.wait_for_function("""x => {
            const node = document.querySelector('[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]');
            return Math.abs(node.getBoundingClientRect().x - x) > 15;
        }""", arg=initial_x)
        self.room.locator('#wd-motion').click()
        frozen = actor.bounding_box()
        self.page.wait_for_timeout(200)
        self.assertEqual(actor.bounding_box(), frozen)
        actor.focus()
        actor.press('Enter')
        expect(self.room.locator('#wd-detail')).to_contain_text('요청 반환')
        self.page.locator('#project').select_option('beta')
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_map_motion_controls_and_mobile_layout(self):
        self.room.locator('#wd-context-details > summary').click()
        actor = self.room.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        initial = actor.bounding_box()
        self.room.locator('#wd-motion').click()
        expect(self.room.locator('#work-map')).to_have_class(re.compile(r'.*wd-motion-paused.*'))
        self.room.locator('#wd-motion').click()
        expect(self.room.locator('#work-map')).not_to_have_class(re.compile(r'.*wd-motion-paused.*'))
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        expect(self.page.locator('#work-map')).to_be_visible()
        expect(self.room.locator('#work-map .wd-actor')).not_to_have_count(0)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.room_page.wait_for_function("""id => {
            const actor = document.querySelector('[data-actor-id="entity:personal-ops-tunnel:commands"]');
            const room = document.querySelector('.wd-room[data-room="waiting"]');
            const a = actor.getBoundingClientRect(), r = room.getBoundingClientRect();
            return a.left >= r.left && a.right <= r.right && a.top >= r.top && a.bottom <= r.bottom;
        }""", arg=self.job_id)
        self.assertNotEqual(actor.bounding_box()['x'], initial['x'])
        self.show_workroom_camera()
        expect(actor).to_be_in_viewport()
        expect(self.room.locator('#wd-pipeline')).to_contain_text('추론')
        expect(self.room.locator('#wd-pipeline')).to_contain_text('미수신')

    def test_work_room_overflow_exposes_hidden_object_and_its_log(self):
        for number in range(8):
            self.store.record_activity('alpha', 'read_file', 'completed', f'overflow-{number}')
        self.page.locator('#refresh').click()
        expect(self.room.locator('#work-map .wd-actor')).to_have_count(2)
        expect(self.room.locator('#work-map .wd-task[data-room=done]')).to_have_count(6)
        self.room.locator('.wd-room-done .wd-more').click()
        expect(self.room.locator('#wd-detail .wd-detail-item')).to_have_count(10)
        self.room.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').click()
        self.room.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')
        self.page.locator('#nav-workroom').click()
        self.page.emulate_media(reduced_motion='reduce')
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.room_page.wait_for_function("""() => {
                const actors = [...document.querySelectorAll('.wd-task[data-room=done]')];
                const rects = actors.map(node => node.getBoundingClientRect()).sort((a, b) => a.top - b.top || a.left - b.left);
                const room = document.querySelector('.wd-room-done').getBoundingClientRect();
                if (rects.some(box => box.left < room.left || box.right > room.right || box.top < room.top || box.bottom > room.bottom)) return false;
                const more = document.querySelector('.wd-room-done .wd-more').getBoundingClientRect();
                if (rects.some(box => box.left < more.right && box.right > more.left && box.top < more.bottom && box.bottom > more.top)) return false;
                return rects.every((box, i) => !i || Math.abs(box.top - rects[i - 1].top) > 3 || box.left >= rects[i - 1].right);
            }""", timeout=2000)

    def create_called_character_fixture(self):
        self.room_page.evaluate("""() => {
            const root = document.createElement('section');
            root.id = 'called-fixture'; document.body.append(root);
            const create = window.WorkDashboardMotion.create;
            window.WorkDashboardMotion.create = (...args) => {
                window.calledMotion = create(...args); return window.calledMotion;
            };
            try { window.calledFixture = window.WorkDashboard.create(root); }
            finally { window.WorkDashboardMotion.create = create; }
            window.callRecord = (actor, age, extra = {}) => ({
                schema_version: 1, project: 'alpha', source_id: 'codex-calls',
                source_kind: 'codex-cli', actor_id: actor, actor_kind: 'agent',
                actor_label: actor, task_id: 'call-' + actor, task_kind: 'agent',
                task_label: 'work ' + actor, action: 'code', state: 'running',
                provenance: 'observed', time: new Date(Date.now() - age * 1000).toISOString(),
                ...extra,
            });
        }""")
        return self.room.locator('#called-fixture')

    def create_skill_character_fixture(self, *, reduced_motion=True):
        if reduced_motion:
            self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.skillRecord = (actor, age = 0, extra = {}) => callRecord(actor, age, {
                source_id: 'claude-skills', source_kind: 'claude-cli', task_kind: 'call',
                task_id: 'skill-' + actor, action: 'execute', state: 'started',
                task_label: '검토 스킬 호출', skill_id: 'plugin:review', skill_name: '코드 검토',
                ...extra,
            });
            calledFixture.update({project: 'alpha', records: []});
        }""")
        return fixture

    def test_skill_metadata_reaches_real_http_bridge_and_exact_task_inspector(self):
        self.page.emulate_media(reduced_motion='reduce')
        event = {
            'schema_version': 1, 'source_id': 'claude-skills', 'source_kind': 'claude-cli',
            'actor_id': 'reviewer', 'actor_kind': 'agent', 'actor_label': '검토 담당',
            'task_id': 'skill-first', 'task_kind': 'call', 'task_label': '승인 코드 검토',
            'action': 'execute', 'state': 'started', 'tool_name': 'Skill',
            'skill_id': 'plugin:review', 'skill_name': '코드 검토',
            'time': datetime.now(timezone.utc).isoformat(),
        }
        self.store.record_work_event('alpha', event, provenance='observed')
        self.page.locator('#refresh').click()
        character = self.room.locator('.wd-actor[data-skill-id="plugin:review"]')
        expect(character).to_have_count(1)
        expect(character.locator('.wd-skill-mark')).to_be_visible()
        expect(character.locator('.wd-actor-task')).to_contain_text('코드 검토')
        character.click()
        detail = self.room.locator('#wd-detail')
        expect(detail).to_contain_text('코드 검토')
        expect(detail).to_contain_text('승인 코드 검토')
        expect(detail).to_contain_text('직접 관측')
        expect(detail).to_contain_text('skill-first')
        self.assertEqual(self.page.locator('#workroom-frame').get_attribute('sandbox'), 'allow-scripts')
        self.store.record_work_event('alpha', {
            key: value for key, value in {**event, 'state': 'returned',
                'time': datetime.now(timezone.utc).isoformat()}.items()
            if key not in {'skill_id', 'skill_name'}
        }, provenance='observed')
        self.page.locator('#refresh').click()
        expect(character).to_have_attribute('data-skill-phase', 'returned')
        expect(detail).to_contain_text('반환')
        expect(detail).to_contain_text('코드 검토')

    def test_skill_start_effect_is_once_per_invocation_and_return_is_not_workflow_success(self):
        fixture = self.create_skill_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [skillRecord('mage')];
            calledFixture.update({project: 'alpha', records: calledRecords});
            window.firstSkillSpeech = document.querySelector('#called-fixture .wd-speech');
        }""")
        character = fixture.locator('.wd-actor[data-skill-id="plugin:review"]')
        expect(character).to_have_count(1)
        expect(character.locator('.wd-skill-mark')).to_be_visible()
        expect(fixture.locator('.wd-speech:not([hidden])')).to_contain_text('스킬')
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.unshift(skillRecord('mage', 0, {state: 'running'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.assertTrue(self.room_page.evaluate("""() => firstSkillSpeech ===
            document.querySelector('#called-fixture .wd-speech')"""))
        self.room_page.evaluate("""() => {
            const returned = skillRecord('mage', 0, {state: 'returned'});
            delete returned.skill_id; delete returned.skill_name;
            calledRecords.unshift(returned);
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(character).to_have_attribute('data-skill-phase', 'returned')
        speech = fixture.locator('.wd-speech:not([hidden])')
        expect(speech).to_contain_text('반환')
        self.assertNotIn('성공!', speech.inner_text())
        character.click()
        expect(fixture.locator('#wd-detail')).to_contain_text('코드 검토')
        self.page.clock.fast_forward(6500)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(skillRecord('mage', 0, {task_id: 'skill-second'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-speech:not([hidden])')).to_contain_text('스킬')
        expect(fixture.locator('.wd-actor')).to_have_count(1)

    def test_skill_effects_clear_on_stale_hidden_pause_and_destroy_without_catchup_bursts(self):
        fixture = self.create_skill_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [skillRecord('mage')];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speech = fixture.locator('.wd-speech:not([hidden])')
        expect(speech).to_have_count(1)
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha',
            records: calledRecords, error: 'fixture observation error'})""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(skillRecord('mage', 0, {state: 'failed'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(speech).to_contain_text('실패')
        self.room_page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
            calledRecords.unshift(skillRecord('hidden-mage'));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: false});
            document.dispatchEvent(new Event('visibilitychange'));
            calledFixture.update({project: 'alpha', records: calledRecords});
            delete document.hidden;
        }""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(skillRecord('fresh-mage'));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(speech).to_have_count(1)
        self.room_page.evaluate('calledFixture.setLive(false)')
        expect(speech).to_have_count(0)
        self.room_page.evaluate('calledFixture.setLive(true)')
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledFixture.clear('beta'); calledFixture.destroy();
        }""")
        expect(fixture.locator('.wd-skill-mark')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_skill_label_is_safe_reported_and_usable_on_phone(self):
        fixture = self.create_skill_character_fixture()
        hostile = '<img src=x onerror=window.skill_pwned=1>'
        self.room_page.evaluate("""label => {
            calledRecords = [skillRecord('reported-mage', 0, {skill_name: label,
                provenance: 'reported'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""", hostile)
        character = fixture.locator('.wd-actor[data-skill-id="plugin:review"]')
        expect(character.locator('.wd-skill-mark')).to_be_visible()
        expect(character.locator('.wd-actor-task')).to_contain_text(hostile)
        expect(fixture.locator('.wd-speech:not([hidden])')).to_contain_text('보고')
        character.click()
        expect(fixture.locator('#wd-detail')).to_contain_text(hostile)
        expect(fixture.locator('#wd-detail')).to_contain_text('보고됨')
        self.assertEqual(fixture.locator('img').count(), 0)
        self.assertIsNone(self.room_page.evaluate('window.skill_pwned'))
        for width in (390, 320):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 844})
                expect(character.locator('.wd-skill-mark')).to_be_visible()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                self.assertTrue(self.room_page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_skill_initial_history_and_ambiguous_ids_do_not_invent_activation(self):
        fixture = self.create_skill_character_fixture()
        self.room_page.evaluate("""() => {
            calledFixture.clear('alpha');
            calledRecords = [skillRecord('history-mage', 5, {state: 'returned'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-speech')).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-skill-id="plugin:review"]')).to_have_count(1)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(skillRecord('ambiguous-mage', 1),
                skillRecord('ambiguous-mage', 0, {skill_id: 'plugin:other', state: 'returned'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        ambiguous = fixture.locator('.wd-actor[data-actor-id="entity:claude-skills:ambiguous-mage"]')
        expect(ambiguous).to_have_count(1)
        self.assertFalse(ambiguous.get_attribute('data-skill-id'))
        expect(ambiguous.locator('.wd-skill-mark:visible')).to_have_count(0)

    def test_skill_record_replay_keeps_skill_marker_without_live_activation(self):
        fixture = self.create_skill_character_fixture(reduced_motion=False)
        self.room_page.evaluate("""() => {
            calledFixture.clear('alpha');
            calledRecords = [skillRecord('replay-mage', 20),
                skillRecord('replay-mage', 15, {state: 'returned'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        character = fixture.locator('.wd-actor[data-skill-id="plugin:review"]')
        expect(character).to_have_count(1)
        expect(fixture.locator('.wd-speech')).to_have_count(0)
        fixture.locator('#wd-replay').click()
        expect(character).to_have_attribute('data-presentation', 'replay')
        expect(character.locator('.wd-skill-mark')).to_be_visible()
        expect(fixture.locator('.wd-speech')).to_have_count(0)
        self.assertNotEqual(character.get_attribute('data-skill-active'), 'true')
        fixture.locator('#wd-motion').click()
        self.assertEqual(character.evaluate("""node => node.getAnimations({subtree: true})
            .filter(animation => animation.playState === 'running').length"""), 0)

    def test_inherited_reported_skill_keeps_visible_and_accessible_provenance(self):
        fixture = self.create_skill_character_fixture()
        self.room_page.evaluate("""() => {
            const start = skillRecord('reported-mage', 1, {provenance: 'reported'});
            const end = skillRecord('reported-mage', 0, {state: 'returned'});
            delete end.skill_id; delete end.skill_name;
            calledRecords = [start, end];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        character = fixture.locator('.wd-actor[data-skill-id="plugin:review"]')
        expect(character.locator('.wd-actor-task')).to_contain_text('보고')
        expect(character).to_have_attribute('data-skill-provenance', 'reported')
        label = character.get_attribute('aria-label')
        self.assertIn('코드 검토', label)
        self.assertIn('보고됨', label)
        board_label = fixture.locator('.wd-work-item[data-task-id="task:claude-skills:reported-mage:skill-reported-mage"]').get_attribute('aria-label')
        self.assertIn('코드 검토', board_label)
        self.assertIn('보고됨', board_label)
        character.click()
        expect(fixture.locator('.wd-summary-skill')).to_contain_text('보고됨')

    def test_skill_burst_waits_for_visible_admission_and_is_not_restarted_by_polling(self):
        fixture = self.create_skill_character_fixture(reduced_motion=False)
        initial = self.room_page.evaluate("""() => {
            calledRecords = [skillRecord('a-first', 1, {action: 'read'}), skillRecord('b-pending')];
            calledFixture.update({project: 'alpha', records: calledRecords});
            const actor = document.querySelector('#called-fixture [data-actor-id="entity:claude-skills:b-pending"]');
            const mark = actor.querySelector('.wd-skill-mark');
            return {pending: actor.dataset.entryPending,
                bursts: mark.getAnimations().filter(animation => !(animation instanceof CSSAnimation)).length};
        }""")
        self.assertEqual(initial['pending'], 'true')
        self.assertEqual(initial['bursts'], 0)
        self.room_page.evaluate("""() => {
            calledMotion.settle();
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        character = fixture.locator('.wd-actor[data-actor-id="entity:claude-skills:b-pending"]')
        expect(character).to_have_attribute('data-entry-pending', 'false')
        expect(character).to_have_attribute('data-skill-active', 'true')
        self.assertTrue(character.evaluate("""node => {
            const mark = node.querySelector('.wd-skill-mark');
            window.skillBurst = mark.getAnimations().find(animation => !(animation instanceof CSSAnimation));
            return Boolean(skillBurst && skillBurst.playState === 'running')
                && mark.getAnimations().some(animation => animation instanceof CSSAnimation
                    && animation.animationName === 'wd-skill-rune');
        }"""))
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: calledRecords})""")
        self.assertTrue(character.evaluate("""node => node.querySelector('.wd-skill-mark')
            .getAnimations().includes(skillBurst)"""))
        fixture.locator('#wd-motion').click()
        expect(character).to_have_attribute('data-skill-active', 'false')
        self.assertEqual(character.evaluate("""node => node.getAnimations({subtree: true})
            .filter(animation => animation.playState === 'running').length"""), 0)

    def test_skill_role_views_show_the_same_marker_name_and_provenance(self):
        fixture = self.create_skill_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = ['review', 'search', 'reason'].map(action => skillRecord(action, 0, {
                action, provenance: 'reported', skill_id: 'plugin:' + action,
                skill_name: '역할 스킬 ' + action, search_scope: action === 'search' ? 'web' : undefined,
            }));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        for action in ('review', 'search', 'reason'):
            with self.subTest(action=action):
                role = fixture.locator(f'.wd-role-character[data-skill-id="plugin:{action}"]')
                expect(role).to_have_count(1)
                expect(role.locator('.wd-skill-mark')).to_be_visible()
                expect(role.locator('.wd-thinking-role')).to_contain_text(f'역할 스킬 {action}')
                expect(role.locator('.wd-thinking-state')).to_contain_text('보고됨')
                self.assertIn(f'역할 스킬 {action}', role.get_attribute('aria-label'))
                self.assertIn('보고됨', role.get_attribute('aria-label'))
                role.click()
                expect(fixture.locator('.wd-summary-skill')).to_contain_text(f'역할 스킬 {action}')
        expect(fixture.locator('.wd-actor[data-skill-id]')).to_have_count(0)
        self.assertEqual(fixture.locator('.wd-role-character[data-skill-id]').count(), 3)

    def test_skill_role_uses_actual_burst_and_pause_cleanup(self):
        fixture = self.create_skill_character_fixture(reduced_motion=False)
        self.room_page.evaluate("""() => {
            calledRecords = [skillRecord('role-mage', 0, {action: 'review', provenance: 'reported'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledMotion.settle();
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        role = fixture.locator('.wd-role-character[data-skill-id="plugin:review"]')
        expect(role.locator('.wd-skill-mark')).to_be_visible()
        expect(role).to_have_attribute('data-skill-active', 'true')
        self.assertTrue(role.evaluate("""node => {
            const animations = node.querySelector('.wd-skill-mark').getAnimations();
            return animations.some(animation => !(animation instanceof CSSAnimation)
                && animation.playState === 'running') && animations.some(animation =>
                animation instanceof CSSAnimation && animation.animationName === 'wd-skill-rune');
        }"""))
        fixture.locator('#wd-motion').click()
        expect(role).to_have_attribute('data-skill-active', 'false')
        self.assertEqual(role.evaluate("""node => node.getAnimations({subtree: true})
            .filter(animation => animation.playState === 'running').length"""), 0)

    def test_archive_latest_work_moves_existing_keyed_records_back_one_slot(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('old-mcp', 30, {source_kind: 'mcp', actor_kind: 'mcp',
                task_kind: 'call', action: 'read', state: 'returned'}),
                callRecord('newer-agent', 20, {state: 'succeeded'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
            window.archiveOrder = () => [...document.querySelectorAll('#called-fixture .wd-task[data-room=done]')]
                .sort((a, b) => parseFloat(a.style.top) - parseFloat(b.style.top)
                    || parseFloat(a.style.left) - parseFloat(b.style.left)).map(node => node.dataset.taskId);
            window.retainedOldCard = document.querySelector('#called-fixture .wd-task[data-task-id$=call-old-mcp]');
        }""")
        expected = ['task:codex-calls:newer-agent:call-newer-agent', 'task:codex-calls:old-mcp:call-old-mcp']
        self.assertEqual(self.room_page.evaluate('archiveOrder()'), expected)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('latest-agent', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.assertEqual(self.room_page.evaluate('archiveOrder()'),
                         ['task:codex-calls:latest-agent:call-latest-agent', *expected])
        self.assertTrue(self.room_page.evaluate("""() => retainedOldCard ===
            document.querySelector('#called-fixture .wd-task[data-task-id$=call-old-mcp]')"""))
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: [...calledRecords].reverse()})""")
        self.assertEqual(self.room_page.evaluate('archiveOrder()'),
                         ['task:codex-calls:latest-agent:call-latest-agent', *expected])
        expect(fixture.locator('.wd-room-done .wd-room-count')).to_have_text('3')

    def test_clicking_work_character_and_role_shows_factual_work_summary(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('builder', 2, {state: 'succeeded', action: 'build',
                task_label: '배포 전 빌드 확인', exit_code: 0})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        fixture.locator('.wd-task[data-room=done]').click()
        summary = fixture.locator('.wd-work-summary')
        expect(summary).to_contain_text('배포 전 빌드 확인')
        expect(summary).to_contain_text('builder')
        expect(summary).to_contain_text('종료 코드 0')
        expect(summary).to_contain_text('직접 관측')
        fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:builder"]').click()
        expect(summary).to_contain_text('배포 전 빌드 확인')
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('planner', 0, {task_kind: 'call', state: 'returned', action: 'reason',
                activity_role: 'planning', task_label: '배포 순서 검토 <img src=x onerror=window.pwned=1>'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        fixture.locator('.wd-role-character').click()
        expect(summary).to_contain_text('배포 순서 검토 <img')
        expect(summary).to_contain_text('요청 반환')
        expect(summary.locator('img')).to_have_count(0)
        self.assertFalse(self.room_page.evaluate('Boolean(window.pwned)'))

    def test_return_waits_for_fresh_snapshot_then_settles_latest_work_and_summarizes_gap(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.initialObservation = new Date(Date.now() - 1000).toISOString();
            calledRecords = [callRecord('finished-while-away', 4)];
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation});
        }""")
        self.page.emulate_media(reduced_motion='no-preference')
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation, following: false});
            calledFixture.setLive(false);
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation, following: true});
            calledFixture.setLive(true);
        }""")
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('최신 상태')
        expect(fixture).to_have_attribute('data-resuming', 'true')
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('finished-while-away', 0, {state: 'succeeded'}),
                callRecord('working-now', 0));
            window.latestObservation = new Date().toISOString();
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: latestObservation, following: true});
            calledFixture.setLive(true);
        }""")
        expect(fixture).to_have_attribute('data-resuming', 'false')
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('작업 성공 1')
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('새로 확인한 일감 1')
        positions = self.room_page.evaluate("""() => calledMotion.snapshot().map(row => ({
            id: row.id, displayRoom: row.displayRoom, walking: row.walking, replaying: row.replaying,
            pending: Boolean(row.entryPending),
        }))""")
        by_actor = {row['id']: row for row in positions}
        self.assertEqual(by_actor['entity:codex-calls:finished-while-away']['displayRoom'], 'waiting')
        self.assertEqual(by_actor['entity:codex-calls:working-now']['displayRoom'], 'coding')
        self.assertTrue(all(not row['walking'] and not row['replaying'] and not row['pending'] for row in positions))
        before = fixture.locator('#wd-resume-summary').inner_text()
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: calledRecords,
            hostObservedAt: latestObservation, following: true})""")
        expect(fixture.locator('#wd-resume-summary')).to_have_text(before)
        self.room_page.evaluate("""() => calledFixture.clear('beta')""")
        expect(fixture.locator('#wd-resume-summary')).to_be_hidden()

    def test_equal_time_archive_keeps_order_focus_and_keyboard_order_when_new_work_arrives(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            const time = new Date(Date.now() - 1000).toISOString();
            calledRecords = ['first', 'second', 'third'].map(id => callRecord(id, 0, {time, state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
            window.historyIds = () => [...document.querySelectorAll('#called-fixture .wd-task[data-room=done]')]
                .map(node => node.dataset.taskId);
            window.initialHistoryIds = historyIds();
            window.focusedHistory = document.querySelector('#called-fixture .wd-task');
            focusedHistory.focus();
            calledFixture.update({project: 'alpha', records: [...calledRecords].reverse()});
        }""")
        expected = self.room_page.evaluate('initialHistoryIds')
        self.assertEqual(self.room_page.evaluate('historyIds()'), expected)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('new-at-same-time', 0,
                {time: calledRecords[0].time, state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.assertEqual(self.room_page.evaluate('historyIds()'),
                         ['task:codex-calls:new-at-same-time:call-new-at-same-time', *expected])
        self.assertTrue(self.room_page.evaluate('document.activeElement === focusedHistory'))
        expect(fixture.locator('.wd-task[data-room=done]').first).to_have_attribute('data-history-slot', '0')

    def test_resume_error_preserves_baseline_and_missing_work_is_not_counted_as_success(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.initialObservation = new Date(Date.now() - 1000).toISOString();
            calledRecords = [callRecord('finisher', 3), callRecord('missing', 3)];
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation});
            calledFixture.setLive(false);
            calledRecords = [callRecord('finisher', 0, {state: 'succeeded'})];
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: new Date().toISOString(), following: true, error: 'partial fixture error'});
        }""")
        expect(fixture).to_have_attribute('data-resuming', 'true')
        expect(fixture.locator('#wd-resume-summary')).not_to_contain_text('작업 성공')
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: calledRecords,
            hostObservedAt: new Date().toISOString(), following: true})""")
        expect(fixture).to_have_attribute('data-resuming', 'false')
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('작업 성공 1')
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('이전 기록 1개는 조회 범위 밖')
        expect(fixture.locator('.wd-work-board')).not_to_contain_text('복귀 동기화 중')

    def test_hidden_document_waits_for_fresh_observation_and_cached_character_retains_summary(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.initialObservation = new Date(Date.now() - 1000).toISOString();
            calledRecords = [callRecord('cached-worker', 3, {state: 'succeeded', task_label: '이전 작업 설명'})];
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation});
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
            Object.defineProperty(document, 'hidden', {configurable: true, value: false});
            document.dispatchEvent(new Event('visibilitychange'));
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation});
        }""")
        expect(fixture).to_have_attribute('data-resuming', 'true')
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: [],
            hostObservedAt: new Date().toISOString()})""")
        expect(fixture).to_have_attribute('data-resuming', 'false')
        expect(fixture.locator('#wd-resume-summary')).not_to_contain_text('작업 성공')
        fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:cached-worker"]').click()
        expect(fixture.locator('.wd-work-summary')).to_contain_text('이전 작업 설명')
        expect(fixture.locator('.wd-work-summary')).to_contain_text('현재 조회 범위를 벗어난 이전 기록')
        self.room_page.evaluate("""() => { delete document.hidden; calledFixture.destroy(); }""")
        expect(fixture.locator('#wd-resume-summary')).to_have_count(0)

    def test_inflight_poll_while_paused_does_not_make_cached_return_snapshot_fresh(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.initialObservation = new Date(Date.now() - 10000).toISOString();
            calledRecords = [callRecord('inflight-worker', 20)];
            calledFixture.update({project: 'alpha', records: calledRecords, hostObservedAt: initialObservation});
            calledFixture.setLive(false);
            window.pausedObservation = new Date().toISOString();
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: pausedObservation, following: false});
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: pausedObservation, following: true});
            calledFixture.setLive(true);
        }""")
        expect(fixture).to_have_attribute('data-resuming', 'true')
        expect(fixture.locator('#wd-resume-summary')).not_to_contain_text('복귀 동기화 완료')
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('inflight-worker', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: new Date(Math.max(Date.now(), Date.parse(pausedObservation) + 1)).toISOString(),
                following: true});
        }""")
        expect(fixture).to_have_attribute('data-resuming', 'false')
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('작업 성공 1')

    def test_event_speech_starts_once_expires_without_animation_and_keeps_text_safe(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.clock.install()
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('history-speaker', 30, {state: 'succeeded'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-speech:not([hidden])')).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('speaker', 0,
                {task_label: '검사 준비 <img src=x onerror=window.speechPwned=1>'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speech = fixture.locator('.wd-speech:not([hidden])')
        expect(speech).to_have_count(1)
        expect(speech).to_contain_text('작업 시작!')
        expect(speech).to_contain_text('검사 준비 <img')
        expect(speech.locator('img')).to_have_count(0)
        self.assertFalse(self.room_page.evaluate('Boolean(window.speechPwned)'))
        self.room_page.evaluate("""() => {
            window.speechVisibilityChanges = 0;
            new MutationObserver(rows => speechVisibilityChanges += rows.length).observe(
                document.querySelector('#called-fixture .wd-speech:not([hidden])'),
                {attributes: true, attributeFilter: ['hidden']});
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.assertEqual(self.room_page.evaluate('speechVisibilityChanges'), 0)
        self.page.clock.fast_forward(3000)
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: [...calledRecords].reverse()})""")
        expect(speech).to_have_count(1)
        self.page.clock.fast_forward(1200)
        expect(speech).to_have_count(0)
        self.page.emulate_media(reduced_motion='no-preference')
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('speaker', 0, {action: 'test'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        fixture.locator('#wd-motion').click()
        expect(speech).to_have_count(1)
        self.page.clock.fast_forward(4200)
        expect(speech).to_have_count(0)

    def test_event_speech_distinguishes_success_returned_response_and_reported_failure(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('success-speaker', 3), callRecord('response-speaker', 3,
                {source_kind: 'mcp', actor_kind: 'mcp', task_kind: 'call', action: 'read'})];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.unshift(callRecord('success-speaker', 0, {state: 'succeeded'}),
                callRecord('response-speaker', 0, {source_kind: 'mcp', actor_kind: 'mcp',
                    task_kind: 'call', action: 'read', state: 'returned'}),
                callRecord('failed-speaker', 0, {state: 'failed', provenance: 'reported'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        success = fixture.locator('.wd-speech[data-speaker-id="entity:codex-calls:success-speaker"]')
        response = fixture.locator('.wd-speech[data-speaker-id="entity:codex-calls:response-speaker"]')
        failed = fixture.locator('.wd-speech[data-speaker-id="entity:codex-calls:failed-speaker"]')
        expect(success).to_contain_text('작업 끝!')
        expect(success).to_have_attribute('data-tone', 'success')
        expect(response).to_contain_text('응답 보냈어요')
        expect(response).not_to_contain_text('작업 끝')
        expect(failed).to_contain_text('앗, 문제가 생겼어요!')
        expect(failed).to_contain_text('보고')
        expect(failed).to_have_attribute('data-tone', 'danger')

    def test_event_speech_works_on_roles_and_clears_on_pause_resume_project_and_destroy(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.clock.install()
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.push(callRecord('speech-planner', 0, {action: 'reason', activity_role: 'planning'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speech = fixture.locator('.wd-speech:not([hidden])')
        expect(speech).to_have_count(1)
        speaker = speech.get_attribute('data-speaker-id')
        self.assertTrue(speaker.startswith('role:planning:'))
        self.room_page.evaluate('calledFixture.setLive(false)')
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('speech-planner', 0,
                {action: 'reason', activity_role: 'planning', state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords, following: true});
            calledFixture.setLive(true);
        }""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('new-speaker', 0));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(speech).to_have_count(1)
        self.room_page.evaluate("""() => calledFixture.clear('beta')""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate('calledFixture.destroy()')
        self.page.clock.fast_forward(12000)
        expect(fixture.locator('.wd-speech')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_event_speech_prioritizes_problems_and_prevents_overlapping_bubbles(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: []});
            calledRecords = Array.from({length: 6}, (_, i) => callRecord('busy-speaker-' + i, 0));
            calledRecords.unshift(callRecord('problem-speaker', 0, {state: 'failed'}),
                callRecord('never-called', 0, {state: 'queued'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speeches = fixture.locator('.wd-speech:not([hidden])')
        self.assertGreaterEqual(speeches.count(), 2)
        self.assertLessEqual(speeches.count(), 3)
        expect(fixture.locator('.wd-speech[data-tone=danger]:not([hidden])')).to_have_count(1)
        expect(fixture.locator('.wd-speech[data-speaker-id="entity:codex-calls:never-called"]')).to_have_count(0)
        self.assertTrue(speeches.evaluate_all("""nodes => {
            const boxes = nodes.map(node => node.getBoundingClientRect());
            return boxes.every((a, i) => boxes.slice(i + 1).every(b =>
                a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
        }"""))

    def test_event_speech_clears_on_error_and_hidden_return_does_not_replay_old_events(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: []});
            calledRecords = [callRecord('hidden-speaker', 0)];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speech = fixture.locator('.wd-speech:not([hidden])')
        expect(speech).to_have_count(1)
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: calledRecords,
            error: 'fixture observation error'})""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('hidden-speaker', 0, {state: 'failed'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-speech[data-tone=danger]:not([hidden])')).to_have_count(1)
        self.room_page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
            calledRecords.unshift(callRecord('while-hidden', 0));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(speech).to_have_count(0)
        self.room_page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: false});
            document.dispatchEvent(new Event('visibilitychange'));
            calledFixture.update({project: 'alpha', records: calledRecords});
            delete document.hidden;
        }""")
        expect(speech).to_have_count(0)
        expect(fixture.locator('#wd-resume-summary')).to_contain_text('복귀 동기화 완료')

    def test_event_speech_first_real_poll_seeds_history_after_empty_cached_snapshot(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: [], hostObservedAt: null});
            calledRecords = [callRecord('initial-recent-history', 0, {state: 'succeeded'})];
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: new Date().toISOString()});
        }""")
        self.assertEqual(fixture.locator('.wd-speech:not([hidden])').count(), 0)
        self.room_page.evaluate("""() => {
            calledRecords.unshift(callRecord('actually-new', 0));
            calledFixture.update({project: 'alpha', records: calledRecords,
                hostObservedAt: new Date().toISOString()});
        }""")
        expect(fixture.locator('.wd-speech:not([hidden])')).to_have_count(1)
        expect(fixture.locator('.wd-speech:not([hidden])')).to_contain_text('작업 시작!')

    def test_event_speech_stays_inside_its_room_at_adjacent_room_edges(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('waiting-one', 8, {state: 'succeeded'}),
                callRecord('waiting-two', 7, {state: 'succeeded'}), callRecord('edge-worker', 6)];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.unshift(callRecord('edge-worker', 0, {state: 'succeeded'}),
                callRecord('edge-planner', 0, {action: 'reason', activity_role: 'planning'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        speeches = fixture.locator('.wd-speech:not([hidden])')
        expect(speeches).to_have_count(2)
        self.assertTrue(speeches.evaluate_all("""nodes => nodes.every(node => {
            const speaker=[...node.closest('.wd-stage').querySelectorAll('.wd-actor,.wd-role-character')]
                .find(actor => actor.dataset.actorId === node.dataset.speakerId
                    || (actor.classList.contains('wd-role-character') && actor.dataset.taskId === node.dataset.taskId));
            if(!speaker) return false;
            const room=node.closest('.wd-stage').querySelector('.wd-room[data-room="' + speaker.dataset.displayRoom + '"]');
            const box=node.getBoundingClientRect(), bounds=room.getBoundingClientRect();
            return box.left >= bounds.left && box.right <= bounds.right;
        })"""))

    def test_called_agent_returns_to_waiting_without_uncalled_figures(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            window.calledRecords = [callRecord('called-agent', 4), callRecord('uncalled-mcp', 2, {
                source_kind: 'mcp', actor_kind: 'mcp', task_kind: 'call', state: 'queued',
            })];
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:called-agent"]')).to_have_attribute('data-room', 'coding')
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:uncalled-mcp"]')).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('called-agent', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        agent = fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:called-agent"]')
        expect(agent).to_have_count(1)
        expect(agent).to_have_attribute('data-room', 'waiting')
        expect(agent).to_have_attribute('data-working', 'false')

    def test_seventh_call_evicts_oldest_invocation_despite_selection_and_heartbeat(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = ['z-oldest', 'f-second', 'e-third', 'd-fourth', 'c-fifth', 'b-sixth']
                .map((id, index) => callRecord(id, 20 - index));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        oldest = fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:z-oldest"]')
        oldest.click()
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('z-oldest', 1), callRecord('a-newest', 0));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(oldest).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-room="coding"]')).to_have_count(6)
        expect(fixture.locator('.wd-room-coding .wd-room-occupancy')).to_contain_text('6/6')
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:f-second"]')).to_have_count(1)
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:a-newest"]')).to_have_count(1)
        expect(fixture.locator('#wd-detail')).to_contain_text('work z-oldest')
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('b-sixth', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(oldest).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:b-sixth"]')).to_have_attribute('data-room', 'waiting')
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('z-oldest', 0, {task_id: 'actually-new-call'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(oldest).to_have_count(1)
        expect(oldest).to_have_attribute('data-room', 'coding')

    def test_completed_called_character_waits_after_history_leaves_the_page(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledFixture.update({project: 'alpha', records: [callRecord('known-completed', 2,
                {state: 'succeeded'})]});
        }""")
        character = fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:known-completed"]')
        expect(character).to_have_attribute('data-room', 'waiting')
        self.room_page.evaluate("""() => calledFixture.update({project: 'alpha', records: []})""")
        expect(character).to_have_count(1)
        expect(character).to_have_attribute('data-room', 'waiting')

    def test_equal_timestamp_calls_keep_observed_arrival_order_after_shuffled_polls(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            const time = new Date(Date.now() - 1000).toISOString();
            calledRecords = [];
            for (const id of ['z-first', 'm-two', 'n-three', 'b-four', 'c-five', 'd-six', 'a-seventh']) {
                calledRecords.push(callRecord(id, 0, {time}));
                calledFixture.update({project: 'alpha', records: [...calledRecords].reverse()});
            }
            calledFixture.update({project: 'alpha', records: [...calledRecords].sort((a, b) => a.actor_id.localeCompare(b.actor_id))});
        }""")
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:z-first"]')).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:a-seventh"]')).to_have_count(1)
        expect(fixture.locator('.wd-actor[data-room="coding"]')).to_have_count(6)
        self.room_page.evaluate("""() => {
            calledFixture.clear('alpha');
            calledFixture.update({project: 'alpha', records: [...calledRecords].reverse()});
            calledFixture.update({project: 'alpha', records: [...calledRecords].sort((a, b) => a.actor_id.localeCompare(b.actor_id))});
        }""")
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:z-first"]')).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:a-seventh"]')).to_have_count(1)

    def test_evicted_invocation_does_not_return_when_older_work_resurfaces(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = [callRecord('evicted', 40, {task_id: 'old-still-running'}),
                callRecord('evicted', 20, {task_id: 'latest-call'}),
                callRecord('evicted', 18, {task_id: 'latest-call', state: 'succeeded'}),
                ...['two', 'three', 'four', 'five', 'six'].map((id, index) => callRecord(id, 19 - index))];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.push(callRecord('seven', 14));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        evicted = fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:evicted"]')
        expect(evicted).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords = calledRecords.filter(row => row.task_id !== 'latest-call');
            calledRecords.push(callRecord('two', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(evicted).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-room="coding"]')).to_have_count(5)

    def test_evicted_character_stays_out_when_equal_time_old_call_resurfaces(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            const time = new Date(Date.now() - 20000).toISOString();
            calledRecords = [callRecord('evicted', 0, {task_id: 'b-old-call', time}),
                callRecord('evicted', 0, {task_id: 'a-selected-call', time}),
                ...['two', 'three', 'four', 'five', 'six'].map((id, index) => callRecord(id, 19 - index))];
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledRecords.push(callRecord('seven', 14));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        evicted = fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:evicted"]')
        expect(evicted).to_have_count(0)
        self.room_page.evaluate("""() => {
            calledRecords = calledRecords.filter(row => row.task_id !== 'a-selected-call');
            calledRecords.push(callRecord('two', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(evicted).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-room="coding"]')).to_have_count(5)
        self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('evicted', 0, {task_id: 'z-new-call-after-eviction',
                time: calledRecords.find(row => row.task_id === 'b-old-call').time}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        expect(evicted).to_have_count(1)

    def test_capacity_replacement_walks_out_before_new_character_enters(self):
        self.page.emulate_media(reduced_motion='reduce')
        fixture = self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = ['oldest', 'two', 'three', 'four', 'five', 'six']
                .map((id, index) => callRecord(id, 20 - index));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.page.emulate_media(reduced_motion='no-preference')
        result = self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('newcomer', 0));
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledFixture.setLive(false);
            const first = calledMotion.snapshot();
            let maximum = 0, oldestInCorridor = false, newcomerEntered = false;
            for (let index = 0; index < 500; index++) {
                const state = calledMotion.advance(0.05);
                maximum = Math.max(maximum, state.filter(row => row.displayRoom === 'coding').length);
                oldestInCorridor ||= state.some(row => row.id.endsWith(':oldest') && row.departing && row.displayRoom === null);
                newcomerEntered ||= state.some(row => row.id.endsWith(':newcomer') && row.displayRoom === 'coding');
            }
            calledFixture.setLive(false);
            return {departingOldest: first.some(row => row.id.endsWith(':oldest') && row.departing),
                maximum, oldestInCorridor, newcomerEntered,
                oldestPresent: calledMotion.snapshot().some(row => row.id.endsWith(':oldest'))};
        }""")
        self.assertTrue(result['departingOldest'])
        self.assertLessEqual(result['maximum'], 6)
        self.assertTrue(result['oldestInCorridor'])
        self.assertTrue(result['newcomerEntered'])
        self.assertFalse(result['oldestPresent'])
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:oldest"]')).to_have_count(0)
        expect(fixture.locator('.wd-actor[data-actor-id="entity:codex-calls:newcomer"]')).to_have_attribute('data-display-room', 'coding')

    def test_waiting_room_replacement_queues_a_call_completed_between_polls(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.create_called_character_fixture()
        self.room_page.evaluate("""() => {
            calledRecords = ['oldest', 'two', 'three', 'four', 'five', 'six']
                .map((id, index) => callRecord(id, 20 - index, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
        }""")
        self.page.emulate_media(reduced_motion='no-preference')
        result = self.room_page.evaluate("""() => {
            calledRecords.push(callRecord('fast-new-call', 0, {state: 'succeeded'}));
            calledFixture.update({project: 'alpha', records: calledRecords});
            calledFixture.setLive(false);
            const first = calledMotion.snapshot();
            let maximum = first.filter(row => row.displayRoom === 'waiting').length;
            for (let index = 0; index < 500; index++) {
                const state = calledMotion.advance(0.05);
                maximum = Math.max(maximum, state.filter(row => row.displayRoom === 'waiting').length);
            }
            calledFixture.setLive(false);
            return {maximum,
                pending: first.some(row => row.id.endsWith(':fast-new-call') && row.entryPending),
                arrived: calledMotion.snapshot().some(row => row.id.endsWith(':fast-new-call')
                    && row.displayRoom === 'waiting' && !row.entryPending),
                oldGone: !calledMotion.snapshot().some(row => row.id.endsWith(':oldest'))};
        }""")
        self.assertLessEqual(result['maximum'], 6)
        self.assertTrue(result['pending'])
        self.assertTrue(result['arrived'])
        self.assertTrue(result['oldGone'])

    def test_mobile_map_centers_new_active_room_after_a_poll(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.store.record_activity('alpha', 'read_file', 'started', 'mobile-active')
        self.page.locator('#refresh').click()
        actor = self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(actor).to_have_attribute('data-room', 'analysis')
        self.show_workroom_camera()
        expect(actor).to_be_in_viewport()

    def test_mobile_map_centers_person_selected_from_room_records(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.show_workroom_camera()
        self.room.locator('#wd-camera').evaluate('node => node.scrollTo(0, 0)')
        self.room.locator('.wd-room-done .wd-room-count').evaluate('node => node.click()')
        self.room.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').evaluate('node => node.click()')
        expect(self.room.locator('#wd-detail')).to_contain_text('명령 성공')
        expect(self.room.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')).to_be_in_viewport()

    def test_progress_conflict_and_polling_preserve_draft(self):
        self.page.locator('#nav-repository').click()
        self.page.locator('#progress-editor').evaluate('(element) => element.open = true')
        self.page.locator('#progress-title').fill('내 작업 초안')
        self.store.update_progress('alpha', '다른 작업 보고', 'blocked', '대기', [], ['재확인'], 1)
        self.page.locator('#refresh').click()
        expect(self.page.locator('#work-title')).to_have_text('다른 작업 보고')
        expect(self.page.locator('#progress-title')).to_have_value('내 작업 초안')
        self.page.locator('#save-progress').click()
        expect(self.page.locator('#notice')).to_contain_text('다른 곳에서')
        expect(self.page.locator('#progress-title')).to_have_value('내 작업 초안')
        self.page.once('dialog', lambda dialog: dialog.dismiss())
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#project')).to_have_value('alpha')
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#reload-progress').click()
        expect(self.page.locator('#progress-title')).to_have_value('다른 작업 보고')
        self.page.locator('#progress-title').fill('합친 작업 보고')
        self.page.locator('#save-progress').click()
        expect(self.page.locator('#progress-status')).to_contain_text('v3 저장됨')
        self.assertEqual(self.store.read_progress('alpha')['title'], '합친 작업 보고')

    def test_late_diff_cannot_cross_project_switch(self):
        self.page.locator('#nav-repository').click()
        pending = []
        self.page.route('**/api/diff?*', lambda route: pending.append(route))
        self.page.locator('#changes button').filter(has_text='code.txt').click()
        expect(self.page.locator('#diff-status')).to_contain_text('불러오는 중')
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#memory')).to_have_value('beta memory')
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(response=pending[0].fetch())
        expect(self.page.locator('#diff')).to_have_text('')
        expect(self.page.locator('#diff-status')).to_have_text('파일을 선택하세요.')

    def test_failed_logout_during_reload_restores_editors_and_preserves_drafts(self):
        self.page.locator('#nav-repository').click()
        self.page.locator('#follow').click()
        expect(self.page.locator('#updated')).to_contain_text('갱신')
        self.page.locator('#progress-editor').evaluate('(element) => element.open = true')
        self.page.locator('#memory').fill('메모 초안')
        self.page.locator('#progress-title').fill('작업 초안')
        pending = []
        self.page.route('**/api/memory?project=alpha', lambda route: pending.append(route))
        self.page.route('**/api/progress?project=alpha', lambda route: pending.append(route))
        self.page.route('**/api/session', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#reload-memory').click()
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#reload-progress').click()
        expect(self.page.locator('#memory')).to_be_disabled()
        expect(self.page.locator('#progress-title')).to_be_disabled()
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('.toolbar-details').evaluate('node => node.open = true')
        self.page.locator('#logout').click()
        expect(self.page.locator('#notice')).to_contain_text('503')
        expect(self.page.locator('#memory')).to_be_enabled()
        expect(self.page.locator('#progress-title')).to_be_enabled()
        expect(self.page.locator('#reload-memory')).to_be_enabled()
        expect(self.page.locator('#reload-progress')).to_be_enabled()
        self.assertEqual(len(pending), 2)
        for route in pending:
            route.fulfill(response=route.fetch())
        expect(self.page.locator('#memory')).to_have_value('메모 초안')
        expect(self.page.locator('#progress-title')).to_have_value('작업 초안')


class RegisteredOperationsBrowserTest(unittest.TestCase):
    """Registered approval UI against the real loopback API, a fake runner and a temporary
    state directory: pending, completed, failed and empty states."""

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        def alpha_target(payload):
            target = copy.deepcopy(payload['targets'][TARGET])
            target['project'] = 'alpha'
            target['compose']['project_name'] = 'alpha-test'
            target['policy']['networks'] = ['alpha-test_default', 'alpha-test-content-internal']
            payload['targets']['alpha-test'] = target
        self.fx = write_registered_fixture(self.root, extra_targets=alpha_target)
        self.ops, self.runner, self.docker = fake_registered_manager(
            self.fx['projects'], self.fx['config'], self.root / 'host-state')
        self.done = self.run_operation('ui-done', ['api'])

        def fail_in_worker():
            count = self.docker.counts.get('docker ps', 0)

            def observe(argv, number):  # Execute's verification preflight observes once first.
                if number > count + 1:
                    raise HostCommandError('HOST_COMMAND_FAILED', returncode=1)
            self.docker.before['docker ps'] = observe
        self.failed = self.run_operation('ui-failed', ['worker'], before_execute=fail_in_worker)
        del self.docker.before['docker ps']
        self.effects = list(self.runner.effects)
        # A repository file name is attacker-shaped text that reaches the page (changed paths).
        self.hostile = 'server/src/<img src=x onerror=window.pwned=1>.ts'
        (self.fx['root'] / self.hostile).write_text('export const x = 1;\n')
        self.pending = self.ops.prepare(PROJECT, TARGET, action='compose.restart',
                                        parameters={'services': ['api']}, request_id='ui-pending')
        token = self.root / 'config-token' / 'admin'
        self.server = create_server(self.fx['projects'], self.root / 'admin-state', token, port=0,
                                    registered_ops_factory=lambda _, **kwargs: self.ops)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = token.read_text().strip()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def run_operation(self, request_id, services, before_execute=None):
        plan = self.ops.prepare(PROJECT, TARGET, action='compose.restart',
                                parameters={'services': services}, request_id=request_id)
        self.ops.approve_web(PROJECT, TARGET, operation_id=plan['operation_id'],
                             plan_digest=plan['plan_digest'], source_digest=plan['source_digest'],
                             confirmation=plan['operation_id'])
        if before_execute:
            before_execute()
        # Playwright's sync API owns this thread's event loop; execute from a helper thread.
        runner = threading.Thread(target=asyncio.run,
                                  args=(self.ops.execute(PROJECT, TARGET, plan['operation_id']),))
        runner.start()
        runner.join(30)
        for worker in list(self.ops._workers.values()):
            worker.join(30)
        return self.ops.get(PROJECT, TARGET, plan['operation_id'])

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def open_link(self, plan):
        # The fixture serves locally; keep the production link's exact path and query.
        link = urlsplit(plan['approval_url'])
        self.page.goto(self.origin + link.path + '?' + link.query + '#token=' + self.token)
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#registered-ops')).to_be_visible()
        expect(self.page.locator('#registered-detail')).to_contain_text(plan['operation_id'])

    def test_fixture_reaches_pending_completed_and_failed_states(self):
        self.assertEqual(self.done['state'], 'succeeded')
        self.assertEqual((self.failed['state'], self.failed['code']), ('failed', 'HOST_COMMAND_FAILED'))
        self.assertEqual(self.effects, ['restart'])

    def test_link_focuses_the_pending_plan_and_approval_never_executes(self):
        self.open_link(self.pending)
        self.assertNotIn('#', self.page.url)
        self.assertNotIn(self.token, self.page.url)
        link = urlsplit(self.pending['approval_url'])
        self.assertEqual(self.page.url, self.origin + link.path + '?' + link.query)
        expect(self.page.locator('#project')).to_have_value(PROJECT)
        expect(self.page.locator('#registered-target')).to_have_value(TARGET)
        detail = self.page.locator('#registered-detail')
        for text in ('승인 대기', 'compose.restart', '보존', 'worker, db', 'RENDER_POLICY', '남음',
                     self.pending['plan_digest']):
            expect(detail).to_contain_text(text)
        self.assertIn(self.hostile, self.pending['source']['changed_paths'])
        expect(detail).to_contain_text(self.hostile)
        expect(self.page.locator('#registered-events')).to_contain_text('PLAN_CREATED')
        list_text = self.page.locator('#registered-list')
        for text in ('승인 대기', '성공', '실패'):
            expect(list_text).to_contain_text(text)
        self.assertEqual(self.page.locator('#registered-ops img').count(), 0)
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.page.locator('#registered-confirmation').fill('f' * 32)
        self.page.locator('#registered-admin-token').fill(self.token)
        self.page.locator('#registered-approve').click()
        expect(self.page.locator('#registered-status')).to_contain_text('다릅니다')
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.pending['operation_id'])['state'],
                         'pending_host_approval')
        self.page.locator('#registered-confirmation').fill(self.pending['operation_id'])
        self.page.locator('#registered-admin-token').fill(self.token)
        self.page.locator('#registered-approve').click()
        expect(self.page.locator('#registered-status')).to_contain_text('execute_host_operation')
        expect(detail).to_contain_text('승인됨')
        expect(self.page.locator('#registered-approve')).to_be_disabled()
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.pending['operation_id'])['state'],
                         'approved')
        self.assertEqual(self.runner.effects, self.effects)
        self.assertEqual(self.page.locator('#registered-admin-token').input_value(), '')

    def test_completed_failed_and_reject_states(self):
        self.open_link(self.pending)
        self.page.locator(f'#registered-list .op[data-id="{self.failed["operation_id"]}"]').click()
        detail = self.page.locator('#registered-detail')
        expect(detail).to_contain_text('HOST_COMMAND_FAILED')
        expect(detail).to_contain_text('실패')
        expect(self.page.locator('#registered-approve')).to_be_disabled()
        self.page.locator(f'#registered-list .op[data-id="{self.done["operation_id"]}"]').click()
        expect(detail).to_contain_text('사후 검증까지 성공')
        expect(detail).to_contain_text('OTHERS_PRESERVED')
        expect(self.page.locator('#registered-events')).to_contain_text('OPERATION_SUCCEEDED')
        self.page.locator(f'#registered-list .op[data-id="{self.pending["operation_id"]}"]').click()
        expect(self.page.locator('#registered-reject')).to_be_enabled()
        self.page.locator('#registered-confirmation').fill(self.pending['operation_id'])
        self.page.locator('#registered-admin-token').fill(self.token)
        self.page.locator('#registered-reject').click()
        expect(self.page.locator('#registered-status')).to_contain_text('거절 완료')
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.pending['operation_id'])['state'],
                         'rejected')
        self.assertEqual(self.runner.effects, self.effects)

    def test_empty_target_and_mobile_layout(self):
        self.open_link(self.pending)
        self.page.locator('#project').select_option('alpha')
        expect(self.page.locator('#registered-target')).to_have_value('alpha-test')
        expect(self.page.locator('#registered-list')).to_contain_text('작업이 없습니다')
        expect(self.page.locator('#registered-approve')).to_be_disabled()
        self.page.locator('#project').select_option(PROJECT)
        expect(self.page.locator('#registered-detail')).to_contain_text('compose.restart')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        expect(self.page.locator('#registered-ops')).to_be_visible()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))


if __name__ == '__main__':
    unittest.main()
