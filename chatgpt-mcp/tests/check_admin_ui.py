"""Browser checks against the real loopback HTTP API and isolated state."""

import json
import re
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_server import create_server  # noqa: E402
from project_state import ProjectState  # noqa: E402


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
        self.server = create_server(projects, state, token, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/#token={token.read_text().strip()}')
        expect(self.page.locator('#connection')).to_have_text('호스트 연결됨')
        expect(self.page.locator('#memory')).to_have_value('<script>window.pwned=1</script>')
        self.page.locator('#execution-details').evaluate('(element) => element.open = true')
        self.page.locator('#notes-details').evaluate('(element) => element.open = true')

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

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
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        self.page.reload()
        expect(self.page.locator('#connection')).to_have_text('로그인 필요')
        expect(self.page.locator('#dashboard')).to_be_hidden()

    def test_logout_locks_edits_while_response_is_pending(self):
        pending = []
        self.page.route('**/api/session', lambda route: pending.append(route))
        self.page.locator('#logout').click()
        expect(self.page.locator('#memory')).to_be_disabled()
        self.assertTrue(self.page.locator('#dashboard').evaluate('(element) => element.inert'))
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(response=pending[0].fetch())
        expect(self.page.locator('#dashboard')).to_be_hidden()

    def test_initial_memory_load_disables_concurrent_reload_and_editing(self):
        pending = []
        self.page.route('**/api/memory?project=beta', lambda route: pending.append(route))
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#memory')).to_be_disabled()
        expect(self.page.locator('#reload-memory')).to_be_disabled()
        self.page.wait_for_function("document.querySelector('#project').value === 'beta'")
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(response=pending[0].fetch())
        expect(self.page.locator('#memory')).to_be_enabled()
        expect(self.page.locator('#memory')).to_have_value('beta memory')
        self.page.locator('#memory').fill('safe draft')
        expect(self.page.locator('#save')).to_be_enabled()

    def test_work_summary_diff_checks_and_handoff(self):
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
        expect(self.page.locator('#observed-work')).to_be_visible()
        expect(self.page.locator('#observed-status')).to_contain_text('최근 3개')
        for text in ('성공 1', '실패 1', '진행 중 1'):
            expect(self.page.locator('#observed-status')).to_contain_text(text)
        expect(self.page.locator('#recent-work .job')).to_have_count(3)
        expect(self.page.locator('#recent-work')).to_contain_text('typecheck')
        expect(self.page.locator('#recent-work')).to_contain_text('server')
        self.assertEqual(self.store.read_progress('beta')['revision'], 0)
        self.page.locator('#recent-work .job').first.click()
        expect(self.page.locator('#execution-details')).to_have_attribute('open', '')
        expect(self.page.locator('#log-status')).to_contain_text('실행 중')
        self.page.locator('#project').select_option('alpha')
        expect(self.page.locator('#work-title')).to_have_text('화면 작업')
        expect(self.page.locator('#completed')).to_contain_text('화면 분석')
        expect(self.page.locator('#recent-work .job')).to_have_count(1)

    def test_work_rooms_follow_job_state_and_open_real_output(self):
        actor = self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(actor).to_have_attribute('data-room', 'waiting')
        job_path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(job_path.read_text())
        record.update(state='running', exit_code=None)
        job_path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        expect(actor).to_have_attribute('data-room', 'testing')
        expect(self.page.locator('#wd-observation')).to_contain_text('진행 중 1건')
        actor.click()
        expect(self.page.locator('#wd-detail')).to_contain_text('typecheck')
        self.page.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')
        expect(self.page.locator('#log-status')).to_contain_text('실행 중')
        record.update(state='failed', exit_code=2)
        job_path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        expect(actor).to_have_attribute('data-room', 'waiting')
        expect(self.page.locator('#wd-detail')).to_contain_text('실패')

    def test_work_objects_dont_multiply_as_completed_tasks_accumulate(self):
        for number in range(8):
            self.store.record_activity('alpha', 'replace_content', 'completed', f'entity-{number}')
        self.page.locator('#refresh').click()
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(2)
        expect(self.page.locator('#work-map .wd-actor-label').filter(has_text='코딩 MCP')).to_have_count(1)
        expect(self.page.locator('#work-map .wd-actor[data-room=done]')).to_have_count(0)
        expect(self.page.locator('.wd-room-done .wd-room-count')).to_have_text('10')
        expect(self.page.locator('.wd-task[data-room=done]')).to_have_count(6)
        self.page.locator('.wd-room-done .wd-more').click()
        expect(self.page.locator('#wd-detail .wd-detail-item')).to_have_count(10)
        self.page.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').click()
        self.page.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')

    def test_work_objects_show_distinct_live_actions_and_idle_has_none(self):
        self.page.emulate_media(reduced_motion='reduce')
        runner = self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
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
        mcp = self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
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
        runner = self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(runner).to_have_attribute('data-action', 'build')
        expect(runner.locator('.wd-actor-task')).to_contain_text('빌드')

    def test_versioned_cli_sources_render_separate_explicit_identities(self):
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'cli-fixture'; document.body.append(root);
            const common = {schema_version:1, project:'alpha', actor_id:'session-1', actor_kind:'agent',
              actor_label:'Agent', task_id:'work-1', task_kind:'agent', task_label:'코드 수정', action:'code',
              state:'running', time:new Date().toISOString()};
            window.cliFixture = window.WorkDashboard.create(root);
            window.cliFixture.update({project:'alpha', records:[
              {...common, source_id:'codex-local', source_kind:'codex-cli'},
              {...common, source_id:'claude-local', source_kind:'claude-cli'}]});
        }""")
        actors = self.page.locator('#cli-fixture .wd-actor')
        expect(actors).to_have_count(2)
        names = self.page.locator('#cli-fixture .wd-actor-label').all_text_contents()
        self.assertEqual(len(set(names)), 2)
        self.assertEqual(set(actors.evaluate_all('nodes => nodes.map(node => node.dataset.kind)')), {'agent'})
        self.page.evaluate('window.cliFixture.destroy()')

    def test_other_mcp_and_model_metadata_reach_map_through_the_reporting_endpoint(self):
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
        agent = self.page.locator('#work-map .wd-actor[data-actor-id="entity:chat-session:agent-1"]')
        expect(agent).to_have_attribute('data-action', 'reason')
        expect(agent).to_have_attribute('data-provenance', 'reported')
        agent.click()
        expect(self.page.locator('#wd-detail')).to_contain_text('fixture-model')
        expect(self.page.locator('#wd-detail')).to_contain_text('high')
        expect(self.page.locator('#wd-detail')).to_contain_text('요청 단위')
        expect(self.page.locator('#wd-detail')).to_contain_text('출력 0')
        expect(self.page.locator('#wd-detail')).to_contain_text('보고됨')
        expect(self.page.locator('#wd-pipeline')).to_contain_text('fixture-model')
        figma = self.page.locator('#work-map .wd-actor[data-actor-id="entity:figma-cloud:figma"]')
        expect(figma.locator('.wd-actor-label')).to_have_text('Figma')
        figma.click()
        expect(self.page.locator('#wd-detail')).to_contain_text('task:chat-session:agent-1:turn-1')
        self.assertEqual(self.page.evaluate('window.pwned'), None)

    def test_native_mcp_return_reaches_recent_changes_with_readable_tool_title(self):
        self.store.record_activity('alpha', 'git_status', 'started', 'live-git')
        self.store.record_activity('alpha', 'git_status', 'completed', 'live-git')
        change = self.page.locator('#wd-live-changes .wd-change').filter(has_text='Git 상태')
        expect(change.first).to_contain_text('요청 반환')
        expect(change.first).to_contain_text('직접 관측')
        expect(self.page.locator('.wd-room-review')).to_have_attribute('data-signal', 'returned')

    def test_live_changes_highlight_real_transitions_without_repeating_heartbeats(self):
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'live-fixture'; document.body.append(root);
            window.liveFixture = window.WorkDashboard.create(root);
            window.liveFixture.update({project:'alpha', records:[]});
            window.liveRecord = {schema_version:1, project:'alpha', source_id:'codex-local', source_kind:'codex-cli',
              actor_id:'hero', actor_kind:'agent', actor_label:'Codex', task_id:'call-1', task_kind:'call',
              task_label:'코드 작성', action:'code', state:'started', time:new Date().toISOString(), provenance:'observed'};
            window.liveFixture.update({project:'alpha', records:[window.liveRecord]});
        }""")
        changes = self.page.locator('#live-fixture .wd-change')
        expect(changes).to_have_count(1)
        expect(changes.first).to_contain_text('코드 작성')
        expect(changes.first).to_contain_text('시작 기록')
        expect(self.page.locator('#live-fixture .wd-room-coding')).to_have_attribute('data-active-count', '1')
        expect(self.page.locator('#live-fixture #wd-live-status')).to_have_attribute('data-state', 'live')
        self.page.evaluate("""() => {
            window.liveFixture.update({project:'alpha', records:[{...window.liveRecord, time:new Date().toISOString(), model:'config-change'}]});
        }""")
        expect(changes).to_have_count(1)
        self.page.evaluate("""() => {
            window.liveFixture.update({project:'alpha', records:[{...window.liveRecord, state:'returned',
              task_label:'<img src=x onerror="window.pwned=1">', time:new Date().toISOString()}]});
        }""")
        expect(changes).to_have_count(2)
        expect(changes.first).to_contain_text('요청 반환')
        self.assertEqual(changes.locator('img').count(), 0)
        self.assertIsNone(self.page.evaluate('window.pwned'))
        expect(self.page.locator('#live-fixture .wd-room-done')).to_have_attribute('data-signal', 'returned')
        expect(self.page.locator('#live-fixture .wd-actor')).to_have_count(1)
        self.page.evaluate('window.liveFixture.destroy()')

    def test_identical_updates_keep_inspector_focus_and_reduced_motion_has_no_decorative_animation(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'focus-fixture'; document.body.append(root);
            window.focusFixture = window.WorkDashboard.create(root);
            const common = {schema_version:1, project:'alpha', source_id:'codex-local', source_kind:'codex-cli',
              actor_id:'hero', actor_kind:'agent', actor_label:'Codex', task_kind:'call', action:'code',
              provenance:'observed', time:new Date().toISOString()};
            window.focusData = {project:'alpha', records:[{...common, task_id:'call-1', task_label:'코드 작성', state:'started'},
              {...common, task_id:'call-2', task_label:'파일 조회', action:'read', state:'returned'}]};
            window.focusFixture.update(window.focusData);
        }""")
        actor = self.page.locator('#focus-fixture .wd-actor')
        actor.click()
        button = self.page.locator('#focus-fixture .wd-detail-item[data-task-id]').first
        button.evaluate('(element) => {element.focus(); window.focusedDetail = element;}')
        self.page.evaluate('window.focusFixture.update(window.focusData)')
        self.assertTrue(self.page.evaluate('window.focusedDetail.isConnected && document.activeElement === window.focusedDetail'))
        expect(actor).to_have_attribute('data-working', 'true')
        self.assertEqual(self.page.evaluate("document.querySelector('#focus-fixture').getAnimations({subtree:true}).filter(a=>a.playState==='running').length"), 0)
        self.page.evaluate('window.focusFixture.setLive(false)')
        expect(self.page.locator('#focus-fixture #wd-live-status')).to_have_attribute('data-state', 'paused')
        self.page.evaluate('window.focusFixture.destroy()')

    def test_configured_model_and_estimated_usage_have_visible_qualifiers(self):
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'config-fixture'; document.body.append(root);
            window.configFixture = window.WorkDashboard.create(root);
            window.configFixture.update({project:'alpha', records:[{schema_version:1, project:'alpha',
              source_id:'codex-local', source_kind:'codex-cli', actor_id:'session-1', actor_kind:'agent',
              actor_label:'Codex', task_id:'session-activity', task_kind:'agent', task_label:'세션 실행 중',
              action:'execute', state:'running', time:new Date().toISOString(), provenance:'observed',
              model:'codex-model', model_source:'configuration', reasoning_effort:'high',
              usage:{total_tokens:42}, usage_scope:'session', usage_estimated:true}]});
        }""")
        self.page.locator('#config-fixture .wd-actor').click()
        fields = self.page.locator('#config-fixture .wd-fields')
        expect(fields).to_contain_text('codex-model · 설정값')
        expect(fields).to_contain_text('high · 설정값')
        expect(fields).to_contain_text('세션 단위 · 추정')
        expect(fields).to_contain_text('합계 42')
        self.page.evaluate('window.configFixture.destroy()')

    def test_absent_metadata_and_expired_reports_do_not_claim_reasoning_or_live_work(self):
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'expiry-fixture'; document.body.append(root);
            window.expiryFixture = window.WorkDashboard.create(root);
            window.expiryFixture.update({project:'alpha', records:[{schema_version:1, project:'alpha',
              source_id:'chat-session', source_kind:'chatgpt', actor_id:'agent-1', actor_kind:'agent',
              actor_label:'ChatGPT', task_id:'turn-1', task_kind:'agent', task_label:'작업', action:'execute',
              state:'running', time:new Date(Date.now()-180000).toISOString(), thinking:{enabled:true}}]});
        }""")
        actor = self.page.locator('#expiry-fixture .wd-actor')
        expect(actor).to_have_attribute('data-state', 'unknown')
        expect(actor).to_have_attribute('data-working', 'false')
        actor.click()
        expect(self.page.locator('#expiry-fixture .wd-fields')).to_contain_text('모델')
        expect(self.page.locator('#expiry-fixture .wd-fields')).to_contain_text('미수신')
        expect(self.page.locator('#expiry-fixture .wd-pipeline')).to_contain_text('모델·추론 상태 미수신')
        self.page.evaluate('window.expiryFixture.destroy()')

    def test_pruned_call_history_keeps_the_known_object_without_claiming_active_work(self):
        self.page.route('**/api/activity?project=alpha', lambda route: route.fulfill(json={'project':'alpha', 'events':[]}))
        self.page.locator('#refresh').click()
        mcp = self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(2)
        expect(mcp).to_have_attribute('data-state', 'unobserved')
        expect(mcp).to_have_attribute('data-working', 'false')
        expect(self.page.locator('#wd-status')).to_contain_text('진행 0')
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)

    def test_archived_work_outside_the_visible_actor_limit_remains_inspectable(self):
        self.page.evaluate("""() => {
            const root = document.createElement('section'); root.id = 'cap-fixture'; document.body.append(root);
            const records = Array.from({length:25}, (_, index) => ({schema_version:1, project:'alpha',
              source_id:'codex-local', source_kind:'codex-cli', actor_id:'agent-'+String(index).padStart(2,'0'),
              actor_kind:'agent', actor_label:'Agent '+index, task_id:'work-1', task_kind:'agent',
              task_label:'record-'+index, action:'code', state:'succeeded',
              time:new Date(Date.now()+index*1000).toISOString()}));
            window.capFixture = window.WorkDashboard.create(root);
            window.capFixture.update({project:'alpha', records});
        }""")
        expect(self.page.locator('#cap-fixture .wd-actor[data-actor-id="entity:codex-local:agent-24"]')).to_have_count(0)
        self.page.locator('#cap-fixture .wd-task').filter(has_text='record-24').click()
        expect(self.page.locator('#cap-fixture .wd-inspector')).to_contain_text('record-24')
        expect(self.page.locator('#cap-fixture .wd-inspector')).to_contain_text('작업 완료')
        expect(self.page.locator('#cap-fixture .wd-inspector')).to_contain_text('entity:codex-local:agent-24')
        self.page.evaluate('window.capFixture.destroy()')

    def test_work_map_displays_call_return_without_completing_its_job(self):
        self.store.record_activity('alpha', 'start_command', 'completed', 'call-with-job')
        path = self.root / 'state' / 'jobs' / (self.job_id + '.json')
        record = json.loads(path.read_text())
        record.update(state='running', exit_code=None)
        path.write_text(json.dumps(record))
        self.page.locator('#refresh').click()
        call = self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(call).to_have_attribute('data-room', 'waiting')
        call.click()
        expect(self.page.locator('#wd-detail')).to_contain_text('반환')
        expect(self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')).to_have_attribute('data-room', 'testing')
        self.assertIsNone(self.page.evaluate('window.pwned'))
        self.assertEqual(self.page.locator('#work-map img, #work-map script').count(), 0)

    def test_work_map_clears_project_and_marks_failed_observation(self):
        expect(self.page.locator('#work-map .wd-actor')).not_to_have_count(0)
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)
        expect(self.page.locator('#wd-project')).to_have_text('beta')
        self.page.route('**/api/jobs?project=beta', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.locator('#refresh').click()
        expect(self.page.locator('#wd-status')).to_contain_text('조회 실패')
        expect(self.page.locator('#work-map')).to_have_attribute('data-stale', 'true')
        self.page.locator('#logout').click()
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_map_keeps_last_observed_people_when_refresh_fails(self):
        actor = self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        expect(actor).to_have_attribute('data-state', 'idle')
        self.page.route('**/api/jobs?project=alpha', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.route('**/api/activity?project=alpha', lambda route: route.fulfill(status=503, json={'error': 'fixture'}))
        self.page.locator('#refresh').click()
        expect(self.page.locator('#work-map')).to_have_attribute('data-stale', 'true')
        expect(actor).to_have_attribute('data-state', 'idle')
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_rooms_render_people_and_replay_a_quick_observed_call(self):
        avatar = self.page.locator('#work-map .wd-actor .wd-avatar').first
        self.assertTrue(avatar.evaluate("""node => node.tagName === 'CANVAS' &&
            [...node.getContext('2d').getImageData(0, 0, node.width, node.height).data]
                .filter((value, index) => index % 4 === 3 && value > 0).length > 50"""))
        self.assertEqual(self.page.locator('#work-map .wd-actor[data-presentation=replay]').count(), 0)
        self.store.record_activity('alpha', 'read_file', 'started', 'quick-observed')
        self.store.record_activity('alpha', 'read_file', 'completed', 'quick-observed')
        self.page.locator('#refresh').click()
        actor = self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(actor).to_have_attribute('data-state', 'idle')
        expect(actor).to_have_attribute('data-room', 'waiting')
        expect(actor).to_have_attribute('data-presentation', 'replay')
        expect(self.page.locator('#wd-status')).to_contain_text('진행 0')
        expect(self.page.locator('#wd-observation')).to_contain_text('기록 재생')
        initial = actor.bounding_box()
        self.page.wait_for_function("""x => {
            const node = document.querySelector('[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]');
            return Math.abs(node.getBoundingClientRect().x - x) > 15;
        }""", arg=initial['x'])
        self.page.locator('#wd-motion').click()
        frozen = actor.bounding_box()
        self.page.wait_for_timeout(200)
        self.assertEqual(actor.bounding_box(), frozen)
        actor.focus()
        actor.press('Enter')
        expect(self.page.locator('#wd-detail')).to_contain_text('요청 반환')
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)

    def test_work_map_motion_controls_and_mobile_layout(self):
        actor = self.page.locator('#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')
        initial = actor.bounding_box()
        self.page.locator('#wd-motion').click()
        expect(self.page.locator('#work-map')).to_have_class(re.compile(r'.*wd-motion-paused.*'))
        self.page.locator('#wd-motion').click()
        expect(self.page.locator('#work-map')).not_to_have_class(re.compile(r'.*wd-motion-paused.*'))
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        expect(self.page.locator('#work-map')).to_be_visible()
        expect(self.page.locator('#work-map .wd-actor')).not_to_have_count(0)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.page.wait_for_function("""id => {
            const actor = document.querySelector('[data-actor-id="entity:personal-ops-tunnel:commands"]');
            const room = document.querySelector('.wd-room[data-room="waiting"]');
            const a = actor.getBoundingClientRect(), r = room.getBoundingClientRect();
            return a.left >= r.left && a.right <= r.right && a.top >= r.top && a.bottom <= r.bottom;
        }""", arg=self.job_id)
        self.assertNotEqual(actor.bounding_box()['x'], initial['x'])
        self.page.locator('#wd-camera').scroll_into_view_if_needed()
        expect(actor).to_be_in_viewport()
        expect(self.page.locator('#wd-pipeline')).to_contain_text('추론')
        expect(self.page.locator('#wd-pipeline')).to_contain_text('미수신')

    def test_work_room_overflow_exposes_hidden_object_and_its_log(self):
        for number in range(8):
            self.store.record_activity('alpha', 'read_file', 'completed', f'overflow-{number}')
        self.page.locator('#refresh').click()
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(2)
        expect(self.page.locator('#work-map .wd-task[data-room=done]')).to_have_count(6)
        self.page.locator('.wd-room-done .wd-more').click()
        expect(self.page.locator('#wd-detail .wd-detail-item')).to_have_count(10)
        self.page.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').click()
        self.page.locator('#wd-open-log').click()
        expect(self.page.locator('#log')).to_contain_text('start')
        self.page.emulate_media(reduced_motion='reduce')
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.page.wait_for_function("""() => {
                const actors = [...document.querySelectorAll('.wd-task[data-room=done]')];
                const rects = actors.map(node => node.getBoundingClientRect()).sort((a, b) => a.top - b.top || a.left - b.left);
                const room = document.querySelector('.wd-room-done').getBoundingClientRect();
                if (rects.some(box => box.left < room.left || box.right > room.right || box.top < room.top || box.bottom > room.bottom)) return false;
                const more = document.querySelector('.wd-room-done .wd-more').getBoundingClientRect();
                if (rects.some(box => box.left < more.right && box.right > more.left && box.top < more.bottom && box.bottom > more.top)) return false;
                return rects.every((box, i) => !i || Math.abs(box.top - rects[i - 1].top) > 3 || box.left >= rects[i - 1].right);
            }""", timeout=2000)

    def test_mobile_map_centers_new_active_room_after_a_poll(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.store.record_activity('alpha', 'read_file', 'started', 'mobile-active')
        self.page.locator('#refresh').click()
        actor = self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]')
        expect(actor).to_have_attribute('data-room', 'analysis')
        self.page.locator('#wd-camera').scroll_into_view_if_needed()
        expect(actor).to_be_in_viewport()

    def test_mobile_map_centers_person_selected_from_room_records(self):
        self.page.emulate_media(reduced_motion='reduce')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('#wd-camera').scroll_into_view_if_needed()
        self.page.locator('#wd-camera').evaluate('node => node.scrollTo(0, 0)')
        self.page.locator('.wd-room-done .wd-room-count').evaluate('node => node.click()')
        self.page.locator('#wd-detail .wd-detail-item').filter(has_text='typecheck').evaluate('node => node.click()')
        expect(self.page.locator('#wd-detail')).to_contain_text('명령 성공')
        expect(self.page.locator('.wd-actor[data-actor-id="entity:personal-ops-tunnel:commands"]')).to_be_in_viewport()

    def test_progress_conflict_and_polling_preserve_draft(self):
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


if __name__ == '__main__':
    unittest.main()
