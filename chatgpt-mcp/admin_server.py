#!/usr/bin/env python3
"""Management page on loopback and an optional explicit LAN address."""

import argparse
from datetime import datetime, timezone
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import threading
from urllib.parse import parse_qs, urlsplit

from jobs import ACTIVE_STATES, JOB_ID, MAX_LOG_BYTES, MAX_READ_BYTES, private_directory
from project_state import MAX_PROGRESS_STATE_BYTES, MemoryConflict, ProjectState
from server import CONFIG, load_projects
from workspace_view import WorkspaceView
from host_runtime_config import HostRuntimeError, REGISTERED_MODULES, TRUSTED_APPROVAL_ORIGIN
from host_runtime_operations import HostRuntimeOperations
from runtime_rollout_web import RuntimeRolloutWeb
from agent_tasks import AgentTaskManager
from agent_runner import CodexAgentRunner
from host_command_tools import create_host_command_manager
from host_access import HostAccess
from host_execution_tools import create_host_execution_manager
from host_runtime_web import (pending_operations, host_operation_review, approve_web, enable_extended_scope,
                              enable_predecessor_scope, registered_operations,
                              registered_operation, approve_registered, reject_registered,
                              target_authorization, set_target_authorization)

try:
    from host_registered_tools import (TARGET_PATTERN, create_registered_manager, list_targets,
                                       public_code)
except ModuleNotFoundError as exc:
    if exc.name not in REGISTERED_MODULES:
        raise
    TARGET_PATTERN = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}\Z')

    def create_registered_manager(projects, state_dir=None):
        raise HostRuntimeError('REGISTERED_OPERATIONS_UNAVAILABLE')

    def list_targets(get_manager, project):
        return {'project': project, 'enabled': False,
                'reason': 'REGISTERED_OPERATIONS_UNAVAILABLE', 'targets': []}

    def public_code(error):
        return 'REGISTERED_OPERATIONS_UNAVAILABLE'

ASSETS = {'/': ('admin.html', 'text/html; charset=utf-8'),
          '/admin.js': ('admin.js', 'text/javascript; charset=utf-8'),
          '/admin.css': ('admin.css', 'text/css; charset=utf-8'),
          '/workroom-frame.html': ('workroom-frame.html', 'text/html; charset=utf-8'),
          '/workroom-frame.js': ('workroom-frame.js', 'text/javascript; charset=utf-8'),
          '/workroom-bridge.js': ('workroom-bridge.js', 'text/javascript; charset=utf-8'),
          '/agent-admin.js': ('agent-admin.js', 'text/javascript; charset=utf-8'),
          '/host-command-admin.js': ('host-command-admin.js', 'text/javascript; charset=utf-8'),
          '/runtime-rollout-admin.js': ('runtime-rollout-admin.js', 'text/javascript; charset=utf-8'),
          '/host-access-admin.js': ('host-access-admin.js', 'text/javascript; charset=utf-8'),
          '/work-dashboard/model.js': ('work-dashboard/model.js', 'text/javascript; charset=utf-8'),
          '/work-dashboard/motion.js': ('work-dashboard/motion.js', 'text/javascript; charset=utf-8'),
          '/work-dashboard/pixels.js': ('work-dashboard/pixels.js', 'text/javascript; charset=utf-8'),
          '/work-dashboard/visualizer.js': ('work-dashboard/visualizer.js', 'text/javascript; charset=utf-8'),
          '/work-dashboard/visualizer.css': ('work-dashboard/visualizer.css', 'text/css; charset=utf-8')}
ASSET_ROOT = Path(__file__).resolve().parent
TOKEN_PATH = Path.home() / '.config/chatgpt-coding-mcp/admin-token'
MAX_BODY = 200 * 1024
SESSION_TTL = 30 * 86400
JOB_FIELDS = {'job_id', 'project', 'state', 'preset', 'relative_cwd', 'label',
              'timeout_seconds', 'created_at', 'started_at', 'finished_at',
              'exit_code', 'log_bytes', 'log_truncated'}
JOB_STATES = ACTIVE_STATES | {'succeeded', 'failed', 'timed_out', 'canceled', 'interrupted'}
# Approval POST endpoints and their exact body keys; all share Handler.fresh_approval().
APPROVAL_BODIES = {
    '/api/host-approve': {'operation_id', 'plan_digest', 'source_digest', 'confirmation', 'admin_token'},
    '/api/host-scope-enable': {'confirmation', 'admin_token'},
    '/api/host-predecessor-scope-enable': {'confirmation', 'admin_token'},
    '/api/host-target-authorization': {'enabled', 'confirmation', 'admin_token'},
    '/api/registered-approve': {'operation_id', 'plan_digest', 'source_digest', 'confirmation',
                                'admin_token'},
    '/api/registered-reject': {'operation_id', 'confirmation', 'admin_token'},
    '/api/agent-approve': {'task_id', 'plan_digest', 'source_digest', 'confirmation', 'admin_token'},
    '/api/host-command-approve': {'operation_id', 'plan_digest', 'source_digest',
                                  'confirmation', 'admin_token'},
    '/api/runtime-rollout-approve': {'operation_id', 'plan_digest', 'source_digest',
                                     'confirmation', 'admin_token'},
    '/api/host-access-approve': {'request_id', 'duration_seconds', 'allow_codex',
                                 'confirmation', 'admin_token'},
    '/api/host-access-revoke': {'grant_id', 'confirmation', 'admin_token'},
    '/api/host-execution-cancel': {'operation_id', 'confirmation', 'admin_token'},
}
# The approval link handed to chat: project, target and operation ID only.
APPROVAL_LINK = '/host-operations'
APPROVAL_LINK_ERROR = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>승인 링크 확인</title></head><body><main>
<h1>승인 링크를 확인해 주세요</h1>
<p>작업을 식별하는 항목이 빠졌거나 링크 형식이 올바르지 않습니다. 이 요청으로 승인한 작업은 없습니다.</p>
<p>채팅에서 해당 작업의 승인 카드를 다시 열어 주세요. Schema014 승인은 DB 적용과 별도입니다.</p>
<p>관리 화면을 이용하려면 로그인 후 정확한 작업 ID를 확인해 선택하세요.</p>
<a href="/">관리 화면 열기</a>
</main></body></html>""".encode('utf-8')
LINK_PROJECT = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
OPERATION_ID = re.compile(r'[0-9a-f]{32}\Z')
DIAGNOSTIC_ID = re.compile(r'[0-9a-f]{32}\Z')


class RegisteredRefusal(Exception):
    """A registered operation refused with a sanitized code (shown by the admin page)."""

    def __init__(self, error):
        super().__init__()
        self.code = public_code(error)
        diagnostic = getattr(error, 'diagnostic_id', None)
        self.diagnostic_id = (diagnostic if isinstance(diagnostic, str)
                              and DIAGNOSTIC_ID.fullmatch(diagnostic) else None)

    def payload(self):
        body = {'error': 'Registered host operation refused', 'code': self.code}
        if self.diagnostic_id:
            body['diagnostic_id'] = self.diagnostic_id
        return body


def approval_link_valid(query):
    """Exactly project, target and operation ID; anything else (a token) is refused."""
    try:
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    except ValueError:
        return False
    if any(len(v) != 1 for v in fields.values()):
        return False
    if set(fields) == {'project', 'target', 'operation_id'}:
        return bool(LINK_PROJECT.fullmatch(fields['project'][0])
                    and TARGET_PATTERN.fullmatch(fields['target'][0])
                    and OPERATION_ID.fullmatch(fields['operation_id'][0]))
    if set(fields) == {'project', 'target', 'host_operation_id'}:
        return bool(fields['project'][0] == 'tossApp'
                    and fields['target'][0] == 'tarororo-test'
                    and OPERATION_ID.fullmatch(fields['host_operation_id'][0]))
    return False


def host_access_link_valid(query, projects):
    """Owner-authenticated approval page link with no token or implicit grant."""
    try:
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    except ValueError:
        return False
    return (set(fields) == {'project', 'request_id'}
            and all(len(values) == 1 for values in fields.values())
            and bool(LINK_PROJECT.fullmatch(fields['project'][0]))
            and fields['project'][0] in projects
            and bool(OPERATION_ID.fullmatch(fields['request_id'][0])))


def _script_action(script):
    name = script.casefold()
    for action, words in (('build', ('build',)),
                          ('test', ('test', 'typecheck', 'lint', 'e2e'))):
        if any(re.search(rf'(?<![a-z0-9]){word}(?![a-z0-9])', name) for word in words):
            return action
    return None


def _job_action(argv):
    if (not isinstance(argv, list) or not argv or len(argv) > 128
            or any(not isinstance(arg, str) or not arg or '\0' in arg or len(arg) > 4096
                   for arg in argv)
            or sum(map(len, argv)) > 32768):
        return None
    program = Path(argv[0]).name.casefold()
    args = argv[1:]
    if program in {'npm', 'pnpm', 'yarn', 'bun'}:
        index = 0
        while index < len(args):
            option = args[index]
            if option in {'--prefix', '--workspace', '-w', '--cwd'}:
                index += 2
            elif any(option.startswith(prefix) for prefix in
                     ('--prefix=', '--workspace=', '--cwd=')):
                index += 1
            else:
                break
        if index < len(args) and program == 'npm' and args[index] == 'test':
            return 'test'
        if index + 1 < len(args) and args[index] == 'run':
            return _script_action(args[index + 1]) or 'execute'
        return 'execute'
    if program == 'npx' and args:
        while args and args[0] in {'--yes', '-y', '--no-install'}:
            args = args[1:]
        if not args:
            return 'execute'
        tool = Path(args[0]).name.casefold()
        if tool in {'tsc', 'vitest', 'jest'} or (tool == 'playwright' and args[1:2] == ['test']):
            return 'test'
        return 'execute'
    if program in {'tsc', 'pytest'}:
        return 'test'
    if re.fullmatch(r'python(?:3(?:\.\d+)?)?', program) and args[:1] == ['-m']:
        return 'test' if args[1:2] in (['pytest'], ['unittest']) else 'execute'
    if program == 'git' and args:
        if args[0] in {'commit', 'push'} and any(value in {'--dry-run', '--help', '-h'} for value in args[1:]):
            return 'review'
        return {'status': 'review', 'diff': 'review', 'commit': 'commit',
                'push': 'push'}.get(args[0], 'execute')
    return 'execute'


def dashboard_root(asset_root):
    """Choose the documented source layout or the release-local asset set."""
    marker = asset_root / 'install-runtime.py'
    source_tree = asset_root.name == 'chatgpt-mcp' and marker.is_file() and not marker.is_symlink()
    folder = (asset_root.parent if source_tree else asset_root) / 'work-dashboard'
    try:
        info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or (not source_tree and stat.S_IMODE(info.st_mode) != 0o700)):
            raise ValueError('Dashboard asset directory is unsafe')
        for filename, _ in ASSETS.values():
            if not filename.startswith('work-dashboard/'):
                continue
            info = (folder / filename.removeprefix('work-dashboard/')).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or (not source_tree and stat.S_IMODE(info.st_mode) != 0o600)):
                raise ValueError('Dashboard asset file is unsafe')
    except OSError as exc:
        raise ValueError('Complete release-local dashboard assets are required') from exc
    return folder


def private_read(path, limit, offset=0):
    """Verify the opened object, including parents, without chmod or recovery."""
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('Unsafe state path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('Unsafe state file')
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, limit), info.st_size
    finally:
        os.close(fd)


def load_token(path, projects):
    path = Path(path).absolute()
    if any(path.resolve().is_relative_to(root.resolve()) for root in projects.values()):
        raise ValueError('Admin token must be outside writable projects')
    private_directory(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(secrets.token_urlsafe(32) + '\n')
    data, size = private_read(path, 256)
    token = data.decode('ascii').strip()
    if size > 256 or len(token) < 32 or not all(c.isalnum() or c in '-_' for c in token):
        raise ValueError('Invalid admin token file')
    return token


class JobReader:
    def __init__(self, projects, state_dir):
        self.projects = projects
        self.directory = Path(state_dir) / 'jobs'

    def get(self, project, job_id):
        if project not in self.projects or not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError('Invalid project or job')
        data, size = private_read(self.directory / (job_id + '.json'), 65537)
        if size > 65536:
            raise ValueError('Invalid job record')
        record = json.loads(data)
        if (not isinstance(record, dict) or record.get('job_id') != job_id
                or record.get('project') != project or not isinstance(record.get('state'), str)
                or record['state'] not in JOB_STATES):
            raise ValueError('Job not found in selected project')
        for key in ('preset', 'relative_cwd', 'label', 'created_at', 'started_at', 'finished_at'):
            value = record.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 4096 or '\0' in value):
                raise ValueError('Invalid job metadata')
        for key in ('exit_code', 'log_bytes', 'timeout_seconds'):
            value = record.get(key)
            if value is not None and type(value) is not int:
                raise ValueError('Invalid job number')
        if 'log_truncated' in record and type(record['log_truncated']) is not bool:
            raise ValueError('Invalid log metadata')
        result = {key: record[key] for key in JOB_FIELDS if key in record}
        action = _job_action(record.get('argv'))
        if action is not None:
            result['action'] = action
        return result

    def list(self, project):
        if project not in self.projects:
            raise ValueError('Invalid project')
        if self.directory.is_symlink():
            raise ValueError('Unsafe job directory')
        records = []
        for path in self.directory.glob('*.json'):
            try:
                records.append(self.get(project, path.stem))
            except (ValueError, OSError):
                continue
        records.sort(key=lambda row: str(row.get('created_at', '')), reverse=True)
        return {'project': project, 'jobs': records[:100]}

    def log(self, project, job_id, offset):
        if not 0 <= offset <= MAX_LOG_BYTES:
            raise ValueError('Invalid log offset')
        record = self.get(project, job_id)
        try:
            data, size = private_read(self.directory / (job_id + '.log'), MAX_READ_BYTES, offset)
        except FileNotFoundError:
            data, size = b'', 0
        # Avoid splitting an otherwise valid UTF-8 code point across polling chunks.
        if data and offset + len(data) < size:
            try:
                data.decode('utf-8')
            except UnicodeDecodeError as exc:
                if exc.reason == 'unexpected end of data' and exc.start > 0:
                    data = data[:exc.start]
        return {'project': project, 'job_id': job_id, 'state': record['state'],
                'offset': offset, 'next_offset': offset + len(data),
                'text': data.decode('utf-8', errors='replace'),
                'complete': record['state'] not in ACTIVE_STATES,
                'has_more': offset + len(data) < min(size, MAX_LOG_BYTES),
                'log_truncated': bool(record.get('log_truncated', False))}


def recommended_project(projects, state, reader):
    """Choose only from observed activity; this does not change MCP routing."""
    latest, selected = 0, next(iter(projects), None)
    for project in projects:
        sources = (lambda: [state.read_progress(project).get('updated_at')],
                   lambda: [row.get('created_at') for row in reader.list(project)['jobs']],
                   lambda: [row.get('time') for row in state.list_activity(project)['events']])
        for source in sources:
            try:
                for value in source():
                    if not isinstance(value, str):
                        continue
                    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    if stamp.tzinfo is not None and stamp.timestamp() > latest:
                        latest, selected = stamp.timestamp(), project
            except (ValueError, OSError, KeyError, TypeError, OverflowError):
                continue
    return selected


def handoff(project, projects, state, reader, workspace):
    """A bounded text snapshot of reported work and directly observed facts."""
    generated = datetime.now(timezone.utc).isoformat()
    lines = [f'{project} 프로젝트를 이어서 진행해줘.', f'호스트 로컬 경로: {projects[project]}',
             f'요약 생성 시각: {generated}', '',
             '먼저 해당 레포의 AGENTS.md, read_project_progress와 read_project_memory를 읽고 현재 상태를 다시 확인해줘.',
             '아래 작업 보고는 저장된 기록이며, Git 변경에는 다른 터미널에서 만든 변경도 포함될 수 있어.', '']
    try:
        progress = state.read_progress(project)
        if progress['revision']:
            lines += ['[저장된 작업 보고]', f"목표: {progress['title']}",
                      f"보고 상태: {progress['status']} · v{progress['revision']} · {progress['updated_at']}",
                      progress['summary'], '완료로 기록한 항목:']
            lines += ['- ' + text for text in progress['completed']] or ['- 없음']
            lines += ['다음 작업:']
            lines += ['- ' + text for text in progress['next_steps']] or ['- 없음']
        else:
            lines.append('기록된 작업 목표가 없습니다. 현재 목표를 사용자 요청에서 확인해줘.')
    except (ValueError, OSError):
        lines.append('작업 보고를 읽을 수 없습니다. 호스트에서 확인이 필요합니다.')
    lines += ['', '[최근 명령의 관찰 결과 — 명령 전체/로그는 포함하지 않음]']
    try:
        jobs = reader.list(project)['jobs'][:10]
        for job in jobs:
            label = json.dumps((job.get('preset') or job.get('label') or job['job_id'])[:200], ensure_ascii=False)
            lines.append(f"- {label}: {job['state']}, exit={job.get('exit_code')}, "
                         f"시작 기록={job.get('created_at')}, 종료={job.get('finished_at')}")
        if not jobs:
            lines.append('- 저장된 실행 결과가 없습니다.')
    except (ValueError, OSError):
        lines.append('- 실행 결과를 읽을 수 없습니다.')
    changes = workspace.changes(project)
    lines += ['', '[현재 Git 변경 — 파일 내용은 포함하지 않음]']
    if changes['available']:
        lines.append(f"브랜치: {changes['branch']} · HEAD: {changes['head']}")
        for row in changes['files'][:50]:
            lines.append(f"- {row['status']} {json.dumps(row['path'], ensure_ascii=False)}")
        if not changes['files']:
            lines.append('- 변경 파일 없음')
        if changes['truncated'] or len(changes['files']) > 50:
            lines.append('- 일부 파일은 목록 한도로 생략했습니다. 현재 Git 상태를 다시 조회해줘.')
    else:
        lines.append(changes['error'])
    lines += ['', '[저장된 레포 메모]']
    try:
        memory = state.read_memory(project)
        lines.append(memory['content'] or '저장된 메모가 없습니다.')
    except (ValueError, OSError):
        lines.append('메모를 읽을 수 없습니다.')
    lines += ['', '현재 파일과 검증 결과를 확인한 뒤 작업하고, 의미 있는 단계마다 작업 보고와 메모를 갱신해줘.']
    return {'project': project, 'text': '\n'.join(lines), 'generated_at': generated}


def validate_public_origin(origin):
    if origin is None:
        return None
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or any(c.isspace() for c in origin) or not origin.isascii()):
        raise ValueError('Public origin must be an exact HTTPS origin without a path')
    # Accessing port also validates malformed/out-of-range port syntax.
    if parsed.port is not None and parsed.port < 1:
        raise ValueError('Invalid HTTPS port')
    return origin


def validate_lan_host(host):
    address = ipaddress.IPv4Address(host)
    if not any(address in ipaddress.IPv4Network(network) for network in (
            '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('LAN listener requires one explicit RFC1918 IPv4 address')
    return str(address)


def create_server(projects, state_dir, token_path=TOKEN_PATH, port=8765, public_origin=None,
                  bind_host='127.0.0.1', host_ops_factory=HostRuntimeOperations,
                  registered_ops_factory=create_registered_manager,
                  agent_ops_factory=None, command_ops_factory=create_host_command_manager,
                  host_access_factory=HostAccess, host_execution_factory=create_host_execution_manager,
                  runtime_rollout_factory=RuntimeRolloutWeb):
    dashboard = dashboard_root(ASSET_ROOT)
    if bind_host != '127.0.0.1':
        bind_host = validate_lan_host(bind_host)
    public_origin = validate_public_origin(public_origin)
    token = load_token(token_path, projects)
    state = ProjectState(projects, Path(state_dir))
    reader = JobReader(projects, state_dir)
    workspace = WorkspaceView(projects)
    host_ops = None

    registered_ops = None
    agent_ops = None
    command_ops = None
    access_ops = None
    execution_ops = None
    rollout_ops = None

    def runtime_rollout():
        nonlocal rollout_ops
        if rollout_ops is None:
            rollout_ops = runtime_rollout_factory(projects, state_dir=Path(state_dir))
        return rollout_ops

    def host_access():
        nonlocal access_ops
        if access_ops is None:
            access_ops = host_access_factory(projects, Path(state_dir))
        return access_ops

    def host_execution():
        nonlocal execution_ops
        if execution_ops is None:
            execution_ops = host_execution_factory(projects, Path(state_dir), access=host_access())
        return execution_ops

    def command_manager():
        nonlocal command_ops
        if command_ops is None:
            command_ops = command_ops_factory(projects, state_dir=Path(state_dir))
        return command_ops

    def agent_manager():
        nonlocal agent_ops
        if agent_ops is None:
            agent_ops = (agent_ops_factory(projects, state_dir=Path(state_dir) / 'agent-tasks')
                         if agent_ops_factory is not None else
                         AgentTaskManager(projects, state_dir=Path(state_dir) / 'agent-tasks',
                                          runner=CodexAgentRunner()))
        return agent_ops

    def host_operations():
        nonlocal host_ops
        if host_ops is None:
            host_ops = host_ops_factory(projects, state_dir=Path(state_dir) / 'host-runtime')
        return host_ops

    def registered_manager():
        nonlocal registered_ops
        if registered_ops is None:
            registered_ops = registered_ops_factory(projects,
                                                    state_dir=Path(state_dir) / 'host-runtime')
        return registered_ops

    def registered(function, *args, **kwargs):
        """Registered-operation calls report their sanitized code to the admin page."""
        try:
            return function(*args, **kwargs)
        except HostRuntimeError as exc:
            raise RegisteredRefusal(exc) from None
        except OSError:
            raise RegisteredRefusal(HostRuntimeError('STATE_UNAVAILABLE')) from None

    def registered_target(query):
        target = query.get('target', [None])[0]
        if not isinstance(target, str) or not TARGET_PATTERN.fullmatch(target):
            raise RegisteredRefusal(HostRuntimeError('UNSUPPORTED_SCOPE'))
        return target

    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalAdmin/1.5.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log paths, headers, tokens or content.

        def send(self, code, payload, content_type='application/json; charset=utf-8', cookie=None,
                 policy=None):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Content-Security-Policy', policy or self.parent_policy())
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            self.wfile.write(payload)

        def error(self, code, text):
            self.send(code, {'error': text})

        def parent_policy(self):
            origin = getattr(self, 'trusted_origin', None)
            if not origin:
                return "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            scripts = ('/workroom-bridge.js', '/admin.js', '/agent-admin.js',
                       '/host-command-admin.js', '/host-access-admin.js',
                       '/runtime-rollout-admin.js')
            return ("default-src 'none'; script-src "
                    + ' '.join(origin + path for path in scripts)
                    + f"; style-src {origin}/admin.css; connect-src {origin}; "
                      f"frame-src {origin}/workroom-frame.html; base-uri 'none'; "
                      "frame-ancestors 'none'; form-action 'none'")

        def frame_policy(self):
            origin = self.trusted_origin
            scripts = ('/work-dashboard/model.js', '/work-dashboard/motion.js',
                       '/work-dashboard/pixels.js', '/work-dashboard/visualizer.js',
                       '/workroom-frame.js')
            styles = ('/admin.css', '/work-dashboard/visualizer.css')
            return ("default-src 'none'; script-src "
                    + ' '.join(origin + path for path in scripts)
                    + "; style-src " + ' '.join(origin + path for path in styles)
                    + f"; connect-src 'none'; base-uri 'none'; frame-ancestors {origin}; "
                      "form-action 'none'; navigate-to 'none'; sandbox allow-scripts")

        def trusted_request(self):
            hosts = self.headers.get_all('Host', [])
            origins = self.headers.get_all('Origin', [])
            authority = f'{bind_host}:{self.server.server_port}'
            valid_hosts = {authority: 'http://' + authority}
            if bind_host == '127.0.0.1':
                valid_hosts[f'localhost:{self.server.server_port}'] = f'http://localhost:{self.server.server_port}'
            if public_origin:
                valid_hosts[urlsplit(public_origin).netloc] = public_origin
            if (len(hosts) != 1 or hosts[0] not in valid_hosts or len(origins) > 1
                    or (origins and origins[0] != valid_hosts[hosts[0]])):
                self.error(403, 'Untrusted host or origin')
                return False
            self.trusted_origin = valid_hosts[hosts[0]].rstrip('/')
            return True

        def cookie_name(self):
            return f'personal_ops_session_{self.server.server_port}'

        def signature(self, value):
            return hmac.new(token.encode(), (self.cookie_name() + ':' + value).encode(), 'sha256').hexdigest()

        def session_cookie(self, remember=False, clear=False):
            value = ''
            if not clear:
                expiry = int(time.time()) + (SESSION_TTL if remember else 86400)
                body = f'{expiry}.{secrets.token_hex(16)}'
                value = body + '.' + self.signature(body)
            cookie = f'{self.cookie_name()}={value}; Path=/; HttpOnly; SameSite=Strict'
            if public_origin and self.headers.get('Host') == urlsplit(public_origin).netloc:
                cookie += '; Secure'
            if clear or remember:
                cookie += f'; Max-Age={0 if clear else SESSION_TTL}'
            return cookie

        def valid_session(self):
            headers = self.headers.get_all('Cookie', [])
            if len(headers) != 1 or len(headers[0]) > 4096:
                return False
            try:
                cookies = SimpleCookie(headers[0])
                value = cookies[self.cookie_name()].value
                expiry, nonce, signature = value.split('.')
                if not int(time.time()) < int(expiry) <= int(time.time()) + SESSION_TTL:
                    return False
                if len(nonce) != 32 or len(signature) != 64:
                    return False
                return hmac.compare_digest(signature.encode(), self.signature(expiry + '.' + nonce).encode())
            except (CookieError, ValueError, KeyError):
                return False

        def fresh_approval(self, body, keys):
            """Shared by every approval POST: only same-origin loopback or this host's
            registered tailnet-only HTTPS Serve address, on a loopback-bound server, without
            forwarding headers, with the exact body keys and a freshly re-entered token."""
            authority = self.headers.get('Host')
            local_hosts = {f'127.0.0.1:{self.server.server_port}',
                           f'localhost:{self.server.server_port}'}
            is_local = authority in local_hosts
            is_tailnet = (public_origin == TRUSTED_APPROVAL_ORIGIN
                          and authority == urlsplit(TRUSTED_APPROVAL_ORIGIN).netloc)
            expected_origin = ((('http://' + authority) if is_local else
                                TRUSTED_APPROVAL_ORIGIN) if (is_local or is_tailnet) else None)
            forwarded_bad = ((self.headers.get('Forwarded')
                              or self.headers.get('X-Forwarded-Host')) if is_local
                             else (self.headers.get('X-Forwarded-Proto') not in (None, 'https')))
            return (bind_host == '127.0.0.1'
                    and (is_local or is_tailnet)
                    and self.headers.get_all('Origin', []) == [expected_origin]
                    and self.headers.get('Sec-Fetch-Site', 'same-origin') == 'same-origin'
                    and not forwarded_bad
                    and set(body) == keys
                    and isinstance(body['admin_token'], str)
                    and hmac.compare_digest(body['admin_token'].encode(), token.encode()))

        def json_body(self, limit=MAX_BODY):
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError('Invalid body framing')
            length = int(lengths[0])
            if length > limit:
                self.error(413, 'Request body too large')
                return None
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('JSON required')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            return body

        def dispatch(self):
            if not self.trusted_request():
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or len(self.path) > 2048:
                self.error(400, 'Invalid request target')
                return
            if parsed.path in ASSETS and self.command == 'GET' and not parsed.query:
                filename, mime = ASSETS[parsed.path]
                asset = (dashboard / filename.removeprefix('work-dashboard/')
                         if filename.startswith('work-dashboard/') else ASSET_ROOT / filename)
                self.send(200, asset.read_bytes(), mime,
                          policy=self.frame_policy() if parsed.path == '/workroom-frame.html' else None)
                return
            if parsed.path == '/runtime-rollouts' and self.command == 'GET':
                fields = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                if (set(fields) != {'operation_id'} or len(fields['operation_id']) != 1
                        or not OPERATION_ID.fullmatch(fields['operation_id'][0])):
                    self.error(400, 'Invalid runtime rollout approval link')
                    return
                self.send(200, (ASSET_ROOT / ASSETS['/'][0]).read_bytes(), ASSETS['/'][1])
                return
            if parsed.path == '/host-commands' and self.command == 'GET':
                # The only public link field is a 32-hex operation ID; no token.
                fields = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                if (set(fields) != {'operation_id'} or len(fields['operation_id']) != 1
                        or not OPERATION_ID.fullmatch(fields['operation_id'][0])):
                    self.error(400, 'Invalid command approval link')
                    return
                self.send(200, (ASSET_ROOT / ASSETS['/'][0]).read_bytes(), ASSETS['/'][1])
                return
            if parsed.path == '/host-access' and self.command == 'GET':
                if not host_access_link_valid(parsed.query, projects):
                    self.error(400, 'Invalid host access approval link')
                    return
                self.send(200, (ASSET_ROOT / ASSETS['/'][0]).read_bytes(), ASSETS['/'][1])
                return
            if parsed.path == APPROVAL_LINK and self.command == 'GET':
                # The page itself: admin.js reads the IDs, and a login is still required.
                if not approval_link_valid(parsed.query):
                    self.send(400, APPROVAL_LINK_ERROR, 'text/html; charset=utf-8')
                    return
                self.send(200, (ASSET_ROOT / ASSETS['/'][0]).read_bytes(), ASSETS['/'][1])
                return
            if not parsed.path.startswith('/api/'):
                self.error(404, 'Not found')
                return
            auth = self.headers.get_all('Authorization', [])
            bearer = len(auth) == 1 and hmac.compare_digest(auth[0].encode(), ('Bearer ' + token).encode())
            if parsed.path == '/api/session' and self.command == 'DELETE':
                self.send(200, {'connected': False}, cookie=self.session_cookie(clear=True))
                return
            if parsed.path == '/api/session' and self.command == 'POST':
                if not bearer:
                    self.error(401, 'Initial token required')
                    return
                body = self.json_body()
                if body is None:
                    return
                if set(body) != {'remember'} or type(body['remember']) is not bool:
                    raise ValueError('Invalid session options')
                self.send(200, {'connected': True}, cookie=self.session_cookie(body['remember']))
                return
            if not bearer and (auth or not self.valid_session()):
                self.error(401, 'Authentication required')
                return
            if self.command not in {'GET', 'PUT', 'POST'}:
                self.error(405, 'Method not allowed')
                return
            if self.command == 'POST' and parsed.path not in {'/api/work-events', *APPROVAL_BODIES}:
                self.error(405, 'Method not allowed')
                return
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=5)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError('Duplicate parameter')
            project = query.get('project', [None])[0]
            if parsed.path not in {'/api/status', '/api/projects'} and project not in projects:
                raise ValueError('Allowed project required')
            if self.command == 'POST':
                body = self.json_body()
                if body is None:
                    return
                if parsed.path in APPROVAL_BODIES:
                    # All approvals require fresh token re-entry plus the exact plan ID.
                    if ((parsed.path in {'/api/host-approve', '/api/host-scope-enable',
                                         '/api/host-predecessor-scope-enable',
                                         '/api/host-target-authorization'}
                         and project != 'tossApp')
                            or (parsed.path == '/api/runtime-rollout-approve'
                                and project != 'personal-ops')
                            or not self.fresh_approval(body, APPROVAL_BODIES[parsed.path])):
                        self.error(403, 'Local approval requires fresh administrator authentication')
                        return
                    fields = {key: value for key, value in body.items() if key != 'admin_token'}
                    if parsed.path == '/api/registered-approve':
                        result = registered(lambda: approve_registered(
                            registered_manager(), project, registered_target(query), **fields))
                    elif parsed.path == '/api/registered-reject':
                        result = registered(lambda: reject_registered(
                            registered_manager(), project, registered_target(query), **fields))
                    elif parsed.path == '/api/agent-approve':
                        result = agent_manager().approve_web(project, **fields)
                    elif parsed.path == '/api/host-command-approve':
                        result = registered(lambda: command_manager().approve_web(project, **fields))
                    elif parsed.path == '/api/runtime-rollout-approve':
                        if set(query) != {'project'}:
                            raise ValueError('Exact runtime rollout project required')
                        result = registered(lambda: runtime_rollout().approve_web(project, **fields))
                    elif parsed.path == '/api/host-access-approve':
                        if set(query) != {'project'}:
                            raise ValueError('Exact host access scope required')
                        result = registered(lambda: host_access().approve_web(project, **fields))
                    elif parsed.path == '/api/host-access-revoke':
                        if set(query) != {'project'}:
                            raise ValueError('Exact host access scope required')
                        result = registered(lambda: host_access().revoke_web(project, **fields))
                    elif parsed.path == '/api/host-execution-cancel':
                        if (set(query) != {'project'} or fields['confirmation'] != project
                                or not isinstance(fields['operation_id'], str)
                                or not OPERATION_ID.fullmatch(fields['operation_id'])):
                            raise ValueError('Exact execution scope required')
                        result = registered(lambda: host_execution().cancel(project, fields['operation_id']))
                    elif parsed.path == '/api/host-scope-enable':
                        result = enable_extended_scope(host_operations(), body['confirmation'])
                    elif parsed.path == '/api/host-predecessor-scope-enable':
                        result = enable_predecessor_scope(host_operations(), body['confirmation'])
                    elif parsed.path == '/api/host-target-authorization':
                        if set(query) != {'project', 'target'}:
                            raise ValueError('Exact target authorization scope required')
                        if type(body['enabled']) is not bool:
                            raise ValueError('Boolean enabled required')
                        result = set_target_authorization(
                            host_operations(), project, registered_target(query),
                            enabled=body['enabled'], confirmation=body['confirmation'])
                    else:
                        result = approve_web(
                            host_operations(), operation_id=body['operation_id'],
                            plan_digest=body['plan_digest'],
                            source_digest=body['source_digest'],
                            confirmation=body['confirmation'])
                else:
                    if set(body) != {'events'}:
                        raise ValueError('Invalid event batch')
                    result = state.record_work_events(project, body['events'], provenance='reported')
            elif self.command == 'PUT':
                if parsed.path not in {'/api/memory', '/api/progress'}:
                    self.error(405, 'Method not allowed')
                    return
                body = self.json_body(MAX_PROGRESS_STATE_BYTES if parsed.path == '/api/progress' else MAX_BODY)
                if body is None:
                    return
                if parsed.path == '/api/memory':
                    if set(body) != {'content', 'expected_revision'}:
                        raise ValueError('Invalid memory update')
                    result = state.update_memory(project, body['content'], body['expected_revision'])
                else:
                    if set(body) != {'title', 'status', 'summary', 'completed', 'next_steps', 'expected_revision'}:
                        raise ValueError('Invalid progress update')
                    result = state.update_progress(project, **body)
            elif parsed.path == '/api/status':
                result = {'status': 'ok', 'version': '1.5.0'}
            elif parsed.path == '/api/projects':
                result = {'projects': [{'project': key, 'path': str(path)} for key, path in projects.items()],
                          'recommended_project': recommended_project(projects, state, reader)}
            elif parsed.path == '/api/host-operations':
                if project != 'tossApp':
                    self.error(404, 'Not found')
                    return
                result = pending_operations(host_operations())
            elif parsed.path == '/api/host-operation-review':
                if project != 'tossApp':
                    self.error(404, 'Not found')
                    return
                if set(query) != {'project', 'operation_id'}:
                    raise ValueError('Exact legacy review scope required')
                result = host_operation_review(
                    host_operations(), project, query['operation_id'][0])
            elif parsed.path == '/api/host-target-authorization':
                if set(query) != {'project', 'target'}:
                    raise ValueError('Exact target authorization scope required')
                result = target_authorization(
                    host_operations(), project, registered_target(query))
            elif parsed.path == '/api/host-access':
                if set(query) != {'project'}:
                    raise ValueError('Exact host access scope required')
                result = registered(lambda: host_access().get(project))
            elif parsed.path == '/api/host-executions':
                if set(query) != {'project'}:
                    raise ValueError('Exact host execution scope required')
                result = registered(lambda: host_execution().list(project))
            elif parsed.path in {'/api/host-execution', '/api/host-execution-log'}:
                allowed = ({'project', 'operation_id'} if parsed.path == '/api/host-execution'
                           else {'project', 'operation_id', 'stream', 'cursor'})
                if (not {'project', 'operation_id'} <= set(query) <= allowed
                        or not OPERATION_ID.fullmatch(query['operation_id'][0])):
                    raise ValueError('Exact host execution query required')
                operation_id = query['operation_id'][0]
                if parsed.path == '/api/host-execution':
                    result = registered(lambda: host_execution().get(project, operation_id))
                    if result.get('kind') == 'codex':
                        from host_agent import HostAgentAdapter
                        result = registered(lambda: HostAgentAdapter(host_execution()).get(project, operation_id))
                else:
                    cursor = query.get('cursor', ['0'])[0]
                    stream = query.get('stream', ['stdout'])[0]
                    if not re.fullmatch(r'[0-9]{1,8}', cursor) or stream not in {'stdout', 'stderr'}:
                        raise ValueError('Invalid log query')
                    result = registered(lambda: host_execution().read_log(
                        project, operation_id, stream=stream, cursor=int(cursor)))
            elif parsed.path == '/api/runtime-rollout-plans':
                if set(query) != {'project'} or project != 'personal-ops':
                    raise ValueError('Exact runtime rollout scope required')
                result = registered(lambda: runtime_rollout().list(project))
            elif parsed.path == '/api/runtime-rollout':
                if (set(query) != {'project', 'operation_id'} or project != 'personal-ops'
                        or not OPERATION_ID.fullmatch(query['operation_id'][0])):
                    raise ValueError('Exact runtime rollout status scope required')
                result = registered(lambda: runtime_rollout().get(project, query['operation_id'][0]))
            elif parsed.path == '/api/host-command-plans':
                if set(query) != {'project'}:
                    raise ValueError('Exact command project scope required')
                result = registered(lambda: command_manager().pending(project))
            elif parsed.path == '/api/host-command':
                if set(query) != {'project', 'operation_id'}:
                    raise ValueError('Exact command status scope required')
                result = registered(lambda: command_manager().get(project, query['operation_id'][0]))
            elif parsed.path == '/api/agent-tasks':
                result = agent_manager().pending(project)
            elif parsed.path == '/api/agent-task':
                if set(query) != {'project', 'task_id'}:
                    raise ValueError('Exact agent task scope required')
                result = agent_manager().get(project, query['task_id'][0])
            elif parsed.path == '/api/registered-targets':
                result = list_targets(registered_manager, project)
            elif parsed.path == '/api/registered-operations':
                result = registered(lambda: registered_operations(
                    registered_manager(), project, registered_target(query)))
            elif parsed.path == '/api/registered-operation':
                cursor = query.get('cursor', ['0'])[0]
                if not re.fullmatch(r'[0-9]{1,12}', cursor):
                    raise RegisteredRefusal(HostRuntimeError('INVALID_EVENT_QUERY'))
                result = registered(lambda: registered_operation(
                    registered_manager(), project, registered_target(query),
                    query.get('operation_id', [''])[0], int(cursor)))
            elif parsed.path == '/api/jobs':
                result = reader.list(project)
            elif parsed.path == '/api/log':
                result = reader.log(project, query.get('job_id', [''])[0], int(query.get('offset', ['0'])[0]))
            elif parsed.path == '/api/activity':
                result = state.list_activity(project)
                result.update(state.list_work_events(project))
            elif parsed.path == '/api/memory':
                result = state.read_memory(project)
            elif parsed.path == '/api/progress':
                result = state.read_progress(project)
            elif parsed.path == '/api/changes':
                result = workspace.changes(project)
            elif parsed.path == '/api/diff':
                staged = query.get('staged', ['false'])[0]
                if staged not in {'true', 'false'}:
                    raise ValueError('Invalid diff selection')
                result = workspace.diff(project, query.get('path', [''])[0], staged == 'true')
            elif parsed.path == '/api/handoff':
                result = handoff(project, projects, state, reader, workspace)
            else:
                self.error(404, 'Not found')
                return
            self.send(200, result)

        def handle_request(self):
            try:
                self.dispatch()
            except MemoryConflict:
                self.error(409, 'Saved record changed; reload before saving')
            except RegisteredRefusal as refusal:
                self.send(400, refusal.payload())
            except (HostRuntimeError, ValueError, OSError, KeyError, TypeError, OverflowError):
                self.error(400, 'Invalid request or unavailable state')

        do_GET = do_PUT = do_POST = do_DELETE = do_OPTIONS = handle_request

    return ThreadingHTTPServer((bind_host, port), Handler)


def main():
    from workspace_control import WorkspaceControlError, verify_launch
    try:
        verify_launch(Path(__file__).resolve().parent)
    except WorkspaceControlError:
        raise SystemExit(126) from None
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--print-login-url', action='store_true')
    parser.add_argument('--lan-host', type=validate_lan_host,
                        help='Also listen on this explicit private IPv4 address (HTTP)')
    parser.add_argument('--print-lan-login-url', action='store_true')
    parser.add_argument('--public-origin', help='Exact HTTPS origin of the trusted reverse proxy')
    args = parser.parse_args()
    projects = load_projects(args.config)
    public_origin = validate_public_origin(args.public_origin)
    if args.print_login_url or args.print_lan_login_url:
        if args.print_lan_login_url and not args.lan_host:
            parser.error('--print-lan-login-url requires --lan-host')
        origin = (f'http://{args.lan_host}:{args.port}' if args.print_lan_login_url
                  else public_origin or f'http://127.0.0.1:{args.port}')
        print(f'{origin}/#token={load_token(TOKEN_PATH, projects)}')
        return
    state_dir = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'chatgpt-coding-mcp'
    servers = []
    threads = []
    try:
        servers.append(create_server(projects, state_dir, port=args.port, public_origin=public_origin))
        if args.lan_host:
            # A distinct socket keeps other NICs closed; no wildcard listener.
            servers.append(create_server(projects, state_dir, port=args.port,
                                         bind_host=args.lan_host))
            thread = threading.Thread(target=servers[-1].serve_forever, daemon=True)
            thread.start()
            threads.append((servers[-1], thread))
        for server in servers:
            print(f'Admin listening on http://{server.server_address[0]}:{server.server_port}', flush=True)
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for server, thread in threads:
            server.shutdown()
            thread.join(timeout=5)
        for server in servers:
            server.server_close()


if __name__ == '__main__':
    main()
