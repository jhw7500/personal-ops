"""Observed WSL launch compatibility, exercised only with isolated fixtures."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import unittest
from unittest.mock import patch

import rollout_admin as admin
import rollout_tunnel as tunnel
import tests.test_rollout_tunnel as fixtures
from workspace_authority import WorkspaceAuthority
from workspace_control import install_controller


BOOTSTRAP = ('import runpy\nimport sys\n'
             'release, entrypoint, *arguments = sys.argv[1:]\n'
             'sys.path.insert(0, release)\n'
             'sys.argv = [entrypoint, *arguments]\n'
             'runpy.run_path(entrypoint, run_name="__main__")\n')
WSL_SERVICE = 'personal-ops-coding-wsl-pc1.service'


class WslRolloutCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.TunnelRolloutTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.home = self.fx.home
        home_patch = patch('pathlib.Path.home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.config = self.home / '.config/chatgpt-coding-mcp/projects.json'
        self.authority = self.home / '.local/state/chatgpt-coding-mcp/workspace-authority'
        project = self.home / 'projects/personal-ops'
        project.mkdir(parents=True)
        self.authority.parent.mkdir(parents=True)
        authority = WorkspaceAuthority({'personal-ops': project}, self.authority)
        authority.initialize('a' * 64, {
            self.fx.release_digest(release): 1
            for release in (self.fx.old, self.fx.new, self.fx.stale)
        })
        self.controller = self.home / '.local/lib/chatgpt-coding-mcp/workspace-controller'
        install_controller(self.controller, Path(__file__).resolve().parents[1],
                           projects={'personal-ops': project})
        target = self.home / '.local/share/uv/python/cpython-fixture/bin/python3.12'
        target.parent.mkdir(parents=True)
        target.write_text('fixture')
        target.chmod(0o700)
        self.fx.interpreter.unlink()
        self.fx.interpreter.symlink_to(target)
        self.client = self.home / 'mcp-comparison/tools/tunnel-client-v0.0.16/tunnel-client'
        self.client.parent.mkdir(parents=True)
        self.client.write_text('fixture')
        self.client.chmod(0o700)
        self.profile = self.fx.profile.with_name('personal-ops-coding-wsl-pc1.yaml')
        self.profile.write_bytes(self.fx.profile.read_bytes())
        self.profile.chmod(0o600)
        self.override = self.fx.override.parent.parent / (WSL_SERVICE + '.d') / '20-runtime.conf'
        self.mcp_wrapper = self.home / '.local/bin/personal-ops-mcp'
        self.admin_wrapper = self.home / '.local/bin/personal-ops-admin'
        for entrypoint, wrapper in [('server.py', self.mcp_wrapper),
                                    ('admin_server.py', self.admin_wrapper)]:
            wrapper.write_text(self.wrapper_text(entrypoint, self.fx.old))
            wrapper.chmod(0o700)
        self.env = {
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_STATE_HOME': str(self.home / '.local/state'),
            'PATH': (str(self.home / 'mcp-comparison/tools/node-v24.12.0-linux-x64/bin')
                     + ':' + str(self.home / '.local/bin') + ':/usr/bin:/bin'),
        }
        self.unit = ('[Unit]\nDescription=Dedicated WSL tunnel\n'
                     f'ConditionPathExists={self.profile}\n[Service]\nType=simple\n'
                     f'WorkingDirectory={project}\n'
                     f'Environment=PATH={self.home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n'
                     'UMask=0077\n'
                     f'ExecStart={self.client} run --profile-dir {self.profile.parent} '
                     '--profile personal-ops-coding-wsl-pc1 --log.file=stdout\n'
                     'Restart=on-failure\nRestartSec=15s\nTimeoutStopSec=20s\n'
                     'KillMode=control-group\n[Install]\nWantedBy=default.target\n')
        self.admin_unit = ('[Unit]\nDescription=Personal Ops WSL dashboard\n[Service]\n'
                           f'Type=simple\nExecStart={self.admin_wrapper}\n'
                           f'WorkingDirectory={project}\nEnvironment=PYTHONUNBUFFERED=1\n'
                           'UMask=0077\nRestart=on-failure\nRestartSec=5\n'
                           '[Install]\nWantedBy=default.target\n')
        profile = json.loads(self.profile.read_bytes())
        profile['mcp']['commands'][0]['command'] = str(self.mcp_wrapper)
        self.profile.write_text(json.dumps(profile))
        self.write_process(4101, 4100, 101, self.fx.old)

    def wrapper_text(self, entrypoint, release):
        entry = '' if entrypoint == 'server.py' else ' --entrypoint admin_server.py'
        extra = '' if entrypoint == 'server.py' else ' --port 8765 "$@"'
        return ('#!/bin/sh\nset -eu\n'
                f'export XDG_CONFIG_HOME={self.home}/.config\n'
                f'export XDG_STATE_HOME={self.home}/.local/state\n'
                f'export PATH={self.home}/mcp-comparison/tools/node-v24.12.0-linux-x64/bin:'
                f'{self.home}/.local/bin:"$PATH"\n'
                f'exec {self.fx.interpreter} {self.controller}/workspace-launcher.py '
                f'--release {release} --interpreter {self.fx.interpreter} '
                f'--isolated{entry} -- --config {self.config}{extra}\n')

    def child(self, release, entrypoint='server.py'):
        argv = [str(self.fx.interpreter), '-I', '-c', BOOTSTRAP,
                str(release), str(release / entrypoint), '--config', str(self.config)]
        if entrypoint == 'admin_server.py':
            argv += ['--port', '8765']
        return argv

    def write_process(self, pid, ppid, ticks, release):
        self.fx.write_process(pid, ppid, ticks, self.child(release))
        (self.fx.proc / str(pid) / 'environ').write_bytes(
            b'\0'.join(os.fsencode(k + '=' + v) for k, v in self.env.items()) + b'\0')

    def layout_module(self):
        try:
            return importlib.import_module('rollout_layout')
        except ImportError as exc:
            self.fail(f'WSL rollout layout support is unavailable: {exc}')

    def plan(self):
        layout = self.layout_module().known_layout('wsl-pc1')
        return tunnel.preflight_tunnel(
            self.fx.freeze(), unit_fn=self.read_unit, profile_path=self.profile,
            override_path=self.override, releases=self.fx.releases,
            client=self.client, approved_interpreters=(self.fx.interpreter,), layout=layout)

    def read_unit(self):
        return self.unit + ('\n' + self.override.read_text() if self.override.exists() else '')

    def switch(self, command, **kwargs):
        return tunnel.switch_tunnel(
            self.fx.new, interpreter=self.fx.interpreter, status_fn=lambda: self.fx.status,
            command_fn=command, plan=self.plan(), releases=self.fx.releases,
            proc_root=self.fx.proc, approved_interpreters=(self.fx.interpreter,),
            sleep_fn=lambda _: None, attempts=1, **kwargs)

    def test_freeze_recognizes_exact_isolated_child_and_preserves_launch_context(self):
        try:
            live = self.fx.freeze()
        except tunnel.TunnelSwitchError as exc:
            self.fail('The observed isolated WSL MCP child must be recognized: ' + exc.code)
        self.assertEqual(live['command_argv'], self.child(self.fx.old))
        self.assertEqual(live['application_args'], ['--config', str(self.config)])
        self.assertEqual(live['launch_environment'], self.env)
        self.assertTrue(live['isolated'])

    def test_freeze_rejects_changed_bootstrap_and_arbitrary_application_flags(self):
        for index, value in [(3, BOOTSTRAP + 'print("unapproved")\n'),
                             (6, '--other-config'), (7, '/tmp/foreign.json')]:
            with self.subTest(index=index):
                argv = self.child(self.fx.old)
                argv[index] = value
                self.fx.write_process(4101, 4100, 101, argv)
                with self.assertRaises(tunnel.TunnelSwitchError):
                    self.fx.freeze()

    def test_freeze_rejects_same_pid_exec_after_reading_the_child_command(self):
        original = tunnel._process_identity
        reads = 0
        def identity(proc, pid):
            nonlocal reads
            if pid == 4101:
                reads += 1
                if reads == 2:
                    self.fx.write_process(pid, 4100, 101, ['/usr/bin/python3', '/tmp/foreign.py'])
            return original(proc, pid)
        with patch.object(tunnel, '_process_identity', identity):
            with self.assertRaises(tunnel.TunnelSwitchError):
                self.fx.freeze()

    def test_layout_rejects_shell_expansion_in_home_paths(self):
        module = self.layout_module()
        for home in ['/tmp/$HOME', '/tmp/home%u', '/tmp/home`id`']:
            with self.subTest(home=home):
                with self.assertRaises(module.LayoutError):
                    module.known_layout('wsl-pc1', home=home)

    def test_layout_selection_rejects_ambiguous_and_symlinked_services(self):
        module = self.layout_module()
        wsl = module.known_layout('wsl-pc1')
        legacy = module.known_layout('legacy')
        Path(wsl['unit_path']).write_text(self.unit)
        Path(wsl['unit_path']).chmod(0o600)
        self.assertEqual(module.select_layout()['service'], WSL_SERVICE)
        Path(legacy['unit_path']).write_text('[Service]\n')
        Path(legacy['unit_path']).chmod(0o600)
        with self.assertRaises(module.LayoutError):
            module.select_layout()
        Path(legacy['unit_path']).unlink()
        Path(wsl['unit_path']).unlink()
        Path(wsl['unit_path']).symlink_to(self.mcp_wrapper)
        with self.assertRaises(module.LayoutError):
            module.select_layout()

    def status_fixture(self, *, ready=True):
        layout = self.layout_module().known_layout('wsl-pc1')
        argv = shlex.split(self.unit.split('ExecStart=', 1)[1].splitlines()[0])
        self.fx.write_process(4100, 1, 100, argv)
        descriptor = self.fx.proc / '4100/fd'
        descriptor.mkdir()
        (descriptor / '7').symlink_to('socket:[123456]')
        net = self.fx.proc / '4100/net'
        net.mkdir()
        (net / 'tcp').write_text(
            'sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n'
            f'0: 0100007F:9DDD 00000000:0000 0A 00000000:00000000 '
            f'00:00000000 00000000 {os.getuid()} 0 123456\n')
        layout['health_url_file'].parent.mkdir(parents=True, mode=0o700)
        layout['health_url_file'].write_text('http://127.0.0.1:40413\n')
        layout['health_url_file'].chmod(0o600)
        profile = json.loads(self.profile.read_bytes())
        profile['control_plane']['api_key'] = 'file:' + str(layout['key_file'])
        profile['health']['url_file'] = str(layout['health_url_file'])
        self.profile.write_text(json.dumps(profile))
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ['cat', WSL_SERVICE]:
                return self.unit.encode()
            if argv[2:4] == ['show', WSL_SERVICE]:
                return b'MainPID=4100\nActiveState=active\nSubState=running\n'
            raise AssertionError('Unexpected status command')
        urls = []
        class Response:
            def __init__(self, route):
                self.status = 200 if ready or route.endswith('/healthz') else 503
                self.body = b'live' if route.endswith('/healthz') else b'ready'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return self.body
        def getter(url, **kwargs):
            urls.append(url)
            return Response(url)
        return layout, command, getter, calls, urls

    def read_status(self, layout, command, getter):
        helper = getattr(tunnel, 'wsl_runtime_status', None)
        if helper is None:
            self.fail('WSL status must verify the systemd process and its own health socket')
        return helper(layout, command_fn=command, proc_root=self.fx.proc,
                      releases=self.fx.releases,
                      approved_interpreters=(self.fx.interpreter,), urlopen_fn=getter)

    def test_wsl_status_uses_systemd_and_process_owned_health_socket(self):
        layout, command, getter, calls, urls = self.status_fixture()
        status = self.read_status(layout, command, getter)
        self.assertEqual(status['process']['pid'], 4100)
        self.assertTrue(status['process_running'])
        self.assertTrue(status['healthy'])
        self.assertTrue(status['ready'])
        self.assertEqual(status['tunnel_id'], 'tunnel-placeholder')
        self.assertEqual(urls, ['http://127.0.0.1:40413/healthz',
                                'http://127.0.0.1:40413/readyz'])
        self.assertTrue(all(item[:2] == ['systemctl', '--user'] for item in calls))

    def test_wsl_status_rejects_health_socket_owned_by_another_process(self):
        layout, command, getter, calls, urls = self.status_fixture()
        (self.fx.proc / '4100/fd/7').unlink()
        (self.fx.proc / '4100/fd/7').symlink_to('socket:[999999]')
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.read_status(layout, command, getter)
        self.assertEqual(urls, [])

    def test_wsl_status_rejects_changed_mainpid_and_health_url_during_observation(self):
        layout, command, getter, calls, urls = self.status_fixture()
        def changing_getter(url, **kwargs):
            result = getter(url, **kwargs)
            layout['health_url_file'].write_text('http://127.0.0.1:40414\n')
            return result
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.read_status(layout, command, changing_getter)
        layout['health_url_file'].write_text('http://127.0.0.1:40413\n')
        reads = 0
        def changing_pid(argv, **kwargs):
            nonlocal reads
            result = command(argv, **kwargs)
            if argv[2:3] == ['show']:
                reads += 1
                if reads > 1:
                    return result.replace(b'4100', b'4200')
            return result
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.read_status(layout, changing_pid, getter)
        def process_disappears(url, **kwargs):
            result = getter(url, **kwargs)
            (self.fx.proc / '4100/stat').unlink(missing_ok=True)
            return result
        try:
            with self.assertRaisesRegex(tunnel.TunnelSwitchError, 'PROCESS_IDENTITY_CHANGED'):
                self.read_status(layout, command, process_disappears)
        except OSError as exc:
            self.fail(f'Process exit during status sampling must be a typed retryable race: {exc}')

    def test_wsl_status_preserves_not_ready_and_stopped_states(self):
        layout, command, getter, calls, urls = self.status_fixture(ready=False)
        status = self.read_status(layout, command, getter)
        self.assertTrue(status['healthy'])
        self.assertFalse(status['ready'])
        def stopped(argv, **kwargs):
            if argv[2:3] == ['show']:
                return b'MainPID=0\nActiveState=inactive\nSubState=dead\n'
            return command(argv, **kwargs)
        status = self.read_status(layout, stopped,
                                  lambda *a, **k: self.fail('Stopped service has no live health socket'))
        self.assertFalse(status['process_running'])
        self.assertFalse(status['healthy'])
        self.assertFalse(status['ready'])

    def test_preflight_accepts_only_pinned_wsl_service_and_wrapper(self):
        plan = self.plan()
        self.assertEqual(plan['service'], WSL_SERVICE)
        self.assertTrue(plan['profile_aligned'])
        self.assertTrue(plan['service_aligned'])
        self.assertFalse(self.override.exists())
        for changed in [self.unit.replace('KillMode=control-group', 'KillMode=process'),
                        self.unit + '\n[Service]\nExecStop=/bin/true\n',
                        self.unit.replace(' --log.file=stdout', ' --log.file=stdout --extra')]:
            original, self.unit = self.unit, changed
            try:
                with self.assertRaises(tunnel.TunnelSwitchError):
                    self.plan()
            finally:
                self.unit = original
        self.mcp_wrapper.write_text(self.mcp_wrapper.read_text() + 'echo modified\n')
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.plan()

    def test_wsl_profile_directory_accepts_owned_0755_and_rejects_writable_or_symlinked(self):
        self.profile.parent.chmod(0o755)
        try:
            self.assertEqual(self.plan()['service'], WSL_SERVICE)
        except tunnel.TunnelSwitchError as exc:
            self.fail(f'The observed owner-safe WSL profile directory must be accepted: {exc}')
        self.profile.parent.chmod(0o777)
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.plan()
        self.profile.parent.chmod(0o755)
        directory = self.profile.parent
        moved = directory.with_name('tunnel-client-fixture')
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(tunnel.TunnelSwitchError):
            self.plan()

    def test_wrapper_identity_change_blocks_switch_before_any_live_mutation(self):
        plan = self.plan()
        original = self.profile.read_bytes()
        replacement = self.mcp_wrapper.with_suffix('.replacement')
        replacement.write_bytes(self.mcp_wrapper.read_bytes())
        replacement.chmod(0o700)
        replacement.replace(self.mcp_wrapper)
        def forbidden(*args, **kwargs):
            self.fail('No service command is permitted after wrapper identity changes')
        with self.assertRaises(tunnel.TunnelSwitchError):
            tunnel.switch_tunnel(
                self.fx.new, interpreter=self.fx.interpreter,
                status_fn=lambda: self.fx.status, command_fn=forbidden,
                plan=plan, releases=self.fx.releases, proc_root=self.fx.proc,
                approved_interpreters=(self.fx.interpreter,))
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse(self.override.exists())

    def test_managed_wrapper_rejects_group_writable_virtual_environment(self):
        self.fx.interpreter.parent.chmod(0o775)
        with self.assertRaisesRegex(tunnel.TunnelSwitchError, 'UNSAFE_RUNTIME_INTERPRETER'):
            self.plan()
        args, override, calls = self.admin_switch_fixture()
        with self.assertRaisesRegex(admin.AdminSwitchError, 'UNSAFE_RUNTIME_INTERPRETER'):
            admin.switch_admin(self.fx.new, **args)
        self.assertEqual(calls, [])
        self.assertFalse(override.exists())

    def test_interpreter_permissions_changed_after_preflight_block_service_commands(self):
        plan = self.plan()
        self.fx.interpreter.parent.chmod(0o775)
        before = self.profile.read_bytes()
        with self.assertRaisesRegex(tunnel.TunnelSwitchError, 'UNSAFE_RUNTIME_INTERPRETER'):
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=lambda: self.fx.status,
                                 command_fn=lambda *a, **k: self.fail('No service change permitted'),
                                 plan=plan, releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,))
        self.assertEqual(self.profile.read_bytes(), before)

    def test_switch_preserves_environment_and_uses_only_selected_service(self):
        original = json.loads(self.profile.read_bytes())
        wrappers = (self.mcp_wrapper.read_bytes(), self.admin_wrapper.read_bytes())
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:] == ['stop', WSL_SERVICE]:
                self.fx.remove_process(4101)
                self.fx.remove_process(4100)
            elif argv[2:] == ['start', WSL_SERVICE]:
                self.fx.write_process(4200, 1, 300, [str(self.client), 'run'])
                self.write_process(4201, 4200, 301, self.fx.new)
                self.fx.status['process']['pid'] = 4200
            return b''
        result = self.switch(command)
        self.assertEqual(result['snapshot'], self.fx.new.name)
        self.assertEqual(result['command_argv'], self.child(self.fx.new))
        self.assertEqual(calls, [
            ['systemctl', '--user', 'daemon-reload'],
            ['systemctl', '--user', 'stop', WSL_SERVICE],
            ['systemctl', '--user', 'start', WSL_SERVICE],
        ])
        actual = json.loads(self.profile.read_bytes())
        command_argv = shlex.split(actual['mcp']['commands'][0]['command'])
        self.assertEqual(command_argv[:4], ['/usr/bin/env',
                         'XDG_CONFIG_HOME=' + self.env['XDG_CONFIG_HOME'],
                         'XDG_STATE_HOME=' + self.env['XDG_STATE_HOME'],
                         'PATH=' + self.env['PATH']])
        self.assertIn('--isolated', command_argv)
        self.assertEqual(command_argv[-3:], ['--', '--config', str(self.config)])
        actual['mcp'] = original['mcp']
        self.assertEqual(actual, original)
        self.assertEqual((self.mcp_wrapper.read_bytes(), self.admin_wrapper.read_bytes()), wrappers)
        self.assertEqual(self.plan()['live']['snapshot'], self.fx.new.name)

    def test_service_in_a_frozen_plan_cannot_be_retargeted(self):
        plan = self.plan()
        plan['service'] = 'personal-ops-tunnel.service'
        before = self.profile.read_bytes()
        calls = []
        with self.assertRaises(tunnel.TunnelSwitchError):
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=lambda: self.fx.status,
                                 command_fn=lambda argv, **kwargs: calls.append(argv) or b'',
                                 plan=plan, releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,),
                                 attempts=1, sleep_fn=lambda _: None)
        self.assertEqual(calls, [])
        self.assertEqual(self.profile.read_bytes(), before)

    def test_unlisted_wsl_target_is_refused_before_profile_or_service_changes(self):
        plan = self.plan()
        authority_file = self.authority / 'authority.json'
        data = json.loads(authority_file.read_bytes())
        del data['allowed_releases'][self.fx.release_digest(self.fx.new)]
        authority_file.write_text(json.dumps(data))
        before = self.profile.read_bytes()
        with self.assertRaisesRegex(tunnel.TunnelSwitchError, 'WORKSPACE_RELEASE_UNLISTED'):
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=lambda: self.fx.status,
                                 command_fn=lambda *a, **k: self.fail('No service command expected'),
                                 plan=plan, releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,))
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertFalse(self.override.exists())

    def test_tunnel_failure_restores_old_isolated_runtime_and_environment(self):
        calls = []
        pid = 4100
        def command(argv, **kwargs):
            nonlocal pid
            calls.append(argv)
            if argv[2:] == ['stop', WSL_SERVICE]:
                self.fx.remove_process(pid + 1)
                self.fx.remove_process(pid)
            elif argv[2:] == ['start', WSL_SERVICE]:
                configured = shlex.split(json.loads(self.profile.read_bytes())['mcp']['commands'][0]['command'])
                release = Path(configured[configured.index('--release') + 1])
                pid += 100
                self.fx.write_process(pid, 1, pid, [str(self.client), 'run'])
                self.write_process(pid + 1, pid, pid + 1, release)
                self.fx.status['process']['pid'] = pid
            return b''
        def verifier(actual):
            raise tunnel.TunnelSwitchError('LIVE_TOOLS_MISMATCH')
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            self.switch(command, verify_fn=verifier)
        self.assertEqual(raised.exception.code, 'TUNNEL_SWITCH_FAILED')
        self.assertEqual(raised.exception.rollback_status, 'succeeded')
        restored = self.fx.freeze()
        self.assertEqual(restored['snapshot'], self.fx.old.name)
        self.assertEqual(restored['launch_environment'], self.env)
        self.assertEqual(sum(item[2:] == ['stop', WSL_SERVICE] for item in calls), 2)

    def unready_target_fixture(self):
        state = {'pid': 4100, 'release': self.fx.old}
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:] == ['stop', WSL_SERVICE]:
                self.fx.remove_process(state['pid'] + 1)
                self.fx.remove_process(state['pid'])
            elif argv[2:] == ['start', WSL_SERVICE]:
                configured = shlex.split(json.loads(self.profile.read_bytes())['mcp']['commands'][0]['command'])
                state['release'] = Path(configured[configured.index('--release') + 1])
                state['pid'] += 100
                self.fx.write_process(state['pid'], 1, state['pid'], [str(self.client), 'run'])
                self.write_process(state['pid'] + 1, state['pid'], state['pid'] + 1,
                                   state['release'])
                ready = state['release'] == self.fx.old
                self.fx.status.update(process={'pid': state['pid']}, process_running=True,
                                      healthy=ready, ready=ready)
            return b''
        return state, calls, command

    def test_exact_expected_target_that_never_becomes_ready_is_rolled_back(self):
        state, calls, command = self.unready_target_fixture()
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            self.switch(command)
        self.assertEqual(raised.exception.code, 'TUNNEL_SWITCH_FAILED')
        self.assertEqual(raised.exception.cause_code, 'TUNNEL_START_NOT_READY')
        self.assertEqual(raised.exception.rollback_status, 'succeeded')
        self.assertEqual(self.fx.freeze()['snapshot'], self.fx.old.name)
        self.assertEqual(sum(item[2:] == ['stop', WSL_SERVICE] for item in calls), 2)
        self.assertEqual(sum(item[2:] == ['start', WSL_SERVICE] for item in calls), 2)

    def assert_startup_replacement_is_not_stopped(self, replacement, attempts=1):
        state, calls, command = self.unready_target_fixture()
        reads = 0
        def status():
            nonlocal reads
            if state['release'] == self.fx.new:
                reads += 1
                if reads == 2:
                    if replacement == 'pid_reuse':
                        self.fx.write_process(state['pid'], 1, state['pid'] + 1000,
                                              [str(self.client), 'run'])
                        self.write_process(state['pid'] + 1, state['pid'],
                                           state['pid'] + 1001, self.fx.new)
                    else:
                        self.write_process(state['pid'] + 1, state['pid'],
                                           state['pid'] + 1, self.fx.stale)
            return self.fx.status
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=status, command_fn=command, plan=self.plan(),
                                 releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,),
                                 sleep_fn=lambda _: None, attempts=attempts)
        self.assertEqual(raised.exception.code, 'TUNNEL_ROLLBACK_FAILED')
        self.assertEqual(sum(item[2:] == ['stop', WSL_SERVICE] for item in calls), 1)
        self.assertEqual(sum(item[2:] == ['start', WSL_SERVICE] for item in calls), 1)
        self.assertTrue((self.fx.proc / str(state['pid'])).exists())

    def test_unready_target_pid_reuse_before_rollback_is_not_stopped(self):
        self.assert_startup_replacement_is_not_stopped('pid_reuse')

    def test_unready_target_replaced_by_foreign_snapshot_is_not_stopped(self):
        self.assert_startup_replacement_is_not_stopped('foreign_snapshot')

    def test_unready_target_pid_reuse_during_polling_is_not_adopted(self):
        self.assert_startup_replacement_is_not_stopped('pid_reuse', attempts=2)

    def test_inactive_status_cannot_bypass_observed_new_process_death(self):
        state, calls, command = self.unready_target_fixture()
        reads = 0
        def status():
            nonlocal reads
            if state['release'] == self.fx.new:
                reads += 1
                if reads > 1:
                    return {'process': {'pid': 0}, 'process_running': False,
                            'healthy': False, 'ready': False}
            return self.fx.status
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=status, command_fn=command, plan=self.plan(),
                                 releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,),
                                 sleep_fn=lambda _: None, attempts=1)
        self.assertEqual(raised.exception.code, 'TUNNEL_ROLLBACK_FAILED')
        self.assertEqual(sum(item[2:] == ['stop', WSL_SERVICE] for item in calls), 1)
        self.assertEqual(sum(item[2:] == ['start', WSL_SERVICE] for item in calls), 1)
        self.assertTrue((self.fx.proc / str(state['pid'])).exists())
        configured = shlex.split(json.loads(self.profile.read_bytes())['mcp']['commands'][0]['command'])
        self.assertEqual(configured[configured.index('--release') + 1], str(self.fx.new))

    def assert_startup_health_failure_rolls_back(self, failure):
        layout, _, getter, _, _ = self.status_fixture()
        state = {'pid': 4100, 'running': True}
        calls = []
        tunnel_argv = shlex.split(self.unit.split('ExecStart=', 1)[1].splitlines()[0])
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ['cat', WSL_SERVICE]:
                return self.read_unit().encode()
            if argv[2:4] == ['show', WSL_SERVICE]:
                if not state['running']:
                    return b'MainPID=0\nActiveState=inactive\nSubState=dead\n'
                return f'MainPID={state["pid"]}\nActiveState=active\nSubState=running\n'.encode()
            if argv[2:] == ['stop', WSL_SERVICE]:
                shutil.rmtree(self.fx.proc / str(state['pid'] + 1))
                shutil.rmtree(self.fx.proc / str(state['pid']))
                state['running'] = False
            elif argv[2:] == ['start', WSL_SERVICE]:
                configured = shlex.split(json.loads(self.profile.read_bytes())['mcp']['commands'][0]['command'])
                release = Path(configured[configured.index('--release') + 1])
                state['pid'] += 100
                state['running'] = True
                self.fx.status['process']['pid'] = state['pid']
                self.fx.write_process(state['pid'], 1, state['pid'], tunnel_argv)
                self.write_process(state['pid'] + 1, state['pid'], state['pid'] + 1, release)
                folder = self.fx.proc / str(state['pid'])
                (folder / 'fd').mkdir()
                (folder / 'net').mkdir()
                (folder / 'net/tcp').write_text('sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n')
                if release == self.fx.old:
                    (folder / 'fd/7').symlink_to('socket:[123456]')
                    (folder / 'net/tcp').write_text(
                        'sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n'
                        f'0: 0100007F:9DDD 00000000:0000 0A 00000000:00000000 '
                        f'00:00000000 00000000 {os.getuid()} 0 123456\n')
                    layout['health_url_file'].write_text('http://127.0.0.1:40413\n')
                    layout['health_url_file'].chmod(0o600)
                elif failure == 'missing_url':
                    layout['health_url_file'].unlink()
            return b''
        def status():
            return self.read_status(layout, command, getter)
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            tunnel.switch_tunnel(self.fx.new, interpreter=self.fx.interpreter,
                                 status_fn=status, command_fn=command, plan=self.plan(),
                                 releases=self.fx.releases, proc_root=self.fx.proc,
                                 approved_interpreters=(self.fx.interpreter,),
                                 sleep_fn=lambda _: None, attempts=1)
        self.assertEqual(raised.exception.code, 'TUNNEL_SWITCH_FAILED')
        self.assertEqual(raised.exception.cause_code, 'TUNNEL_START_NOT_READY')
        self.assertEqual(raised.exception.rollback_status, 'succeeded')
        self.assertEqual(self.fx.freeze()['snapshot'], self.fx.old.name)
        self.assertEqual(sum(item[2:] == ['stop', WSL_SERVICE] for item in calls), 2)
        self.assertEqual(sum(item[2:] == ['start', WSL_SERVICE] for item in calls), 2)

    def test_verified_target_without_health_url_can_be_rolled_back(self):
        self.assert_startup_health_failure_rolls_back('missing_url')

    def test_verified_target_without_owned_health_listener_can_be_rolled_back(self):
        self.assert_startup_health_failure_rolls_back('unowned_listener')

    def test_wsl_start_waits_for_the_old_tunnel_identity_to_die(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:] == ['stop', WSL_SERVICE] and (self.fx.proc / '4101').exists():
                self.fx.remove_process(4101)
            return b''
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            self.switch(command)
        self.assertEqual(raised.exception.cause_code, 'TUNNEL_STOP_IDENTITY_STILL_RUNNING')
        self.assertFalse(any(item[2:] == ['start', WSL_SERVICE] for item in calls))

    def test_inactive_status_cannot_bypass_old_process_death_during_rollback(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:] == ['stop', WSL_SERVICE]:
                self.fx.status['process_running'] = False
            return b''
        with self.assertRaises(tunnel.TunnelSwitchError) as raised:
            self.switch(command)
        self.assertEqual(raised.exception.cause_code, 'TUNNEL_STOP_IDENTITY_STILL_RUNNING')
        self.assertFalse(any(item[2:] == ['start', WSL_SERVICE] for item in calls))

    def test_admin_preflight_accepts_wrapper_and_exact_isolated_process(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            return b'1817\n'
        try:
            argv, port = admin.preflight_admin(
                unit_fn=lambda: self.admin_unit, command_fn=command,
                root=Path(__file__).resolve().parents[1], releases=self.fx.releases,
                process_argv_fn=lambda pid: self.child(self.fx.old, 'admin_server.py'),
                process_start_fn=lambda pid: 300,
                process_environment_fn=lambda pid: self.env)
        except (admin.AdminSwitchError, TypeError) as exc:
            self.fail(f'The observed WSL admin wrapper must pass read-only preflight: {exc}')
        self.assertEqual(argv, self.child(self.fx.old, 'admin_server.py'))
        self.assertEqual(port, 8765)
        self.assertFalse(any(item[2] in {'restart', 'daemon-reload'} for item in calls))

    def admin_switch_fixture(self, *, fail_ui=False, change_wrapper=False):
        override = (self.home / '.config/systemd/user/personal-ops-admin.service.d'
                    / '20-runtime.conf')
        state = {'pid': 100, 'restarts': 0}
        calls = []
        def unit():
            return self.admin_unit + ('\n' + override.read_text() if override.exists() else '')
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:3] == ['show']:
                return str(state['pid']).encode() + b'\n'
            if argv[2:3] == ['restart']:
                state['restarts'] += 1
                state['pid'] += 100
            if change_wrapper and argv[2:3] == ['daemon-reload']:
                replacement = self.admin_wrapper.with_suffix('.new')
                replacement.write_bytes(self.admin_wrapper.read_bytes())
                replacement.chmod(0o700)
                replacement.replace(self.admin_wrapper)
            return b''
        def process_argv(pid):
            return self.child(self.fx.new if pid == 200 else self.fx.old, 'admin_server.py')
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return (b'<form id="login-form"></form>' if fail_ui
                        else b'<section id="host-approval"></section>')
        args = dict(unit_fn=unit, command_fn=command, process_argv_fn=process_argv,
                    process_start_fn=lambda pid: pid * 10,
                    process_environment_fn=lambda pid: self.env,
                    sleep_fn=lambda _: None, urlopen_fn=lambda *a, **k: Response())
        return args, override, calls

    def test_admin_switch_preserves_isolation_environment_and_wrapper_bytes(self):
        args, override, calls = self.admin_switch_fixture()
        before = self.admin_wrapper.read_bytes()
        try:
            result = admin.switch_admin(self.fx.new, **args)
        except (TypeError, admin.AdminSwitchError) as exc:
            self.fail(f'WSL admin rollout must preserve its isolated launch contract: {exc}')
        self.assertEqual(result, 'admin_ready')
        effective, port = admin.parse_execstart(args['unit_fn'](),
                                                Path(__file__).resolve().parents[1],
                                                self.fx.releases)
        self.assertEqual(effective, self.child(self.fx.new, 'admin_server.py'))
        self.assertEqual(port, 8765)
        actual = shlex.split(override.read_text().split('ExecStart=')[-1].strip())
        self.assertEqual(actual[1:4], ['XDG_CONFIG_HOME=' + self.env['XDG_CONFIG_HOME'],
                                      'XDG_STATE_HOME=' + self.env['XDG_STATE_HOME'],
                                      'PATH=' + self.env['PATH']])
        self.assertEqual(self.admin_wrapper.read_bytes(), before)
        self.assertEqual(sum(item[2:3] == ['restart'] for item in calls), 1)

    def test_admin_failed_ui_restores_original_wrapper_and_old_isolated_runtime(self):
        args, override, calls = self.admin_switch_fixture(fail_ui=True)
        before = self.admin_wrapper.read_bytes()
        try:
            with self.assertRaises(admin.AdminSwitchError) as raised:
                admin.switch_admin(self.fx.new, **args)
        except TypeError as exc:
            self.fail(f'WSL admin rollback must support the managed wrapper: {exc}')
        self.assertEqual(raised.exception.code, 'ADMIN_SWITCH_FAILED')
        self.assertEqual(raised.exception.rollback_status, 'succeeded')
        self.assertFalse(override.exists())
        self.assertEqual(self.admin_wrapper.read_bytes(), before)
        self.assertEqual(sum(item[2:3] == ['restart'] for item in calls), 2)

    def test_admin_wrapper_replacement_blocks_restart_and_rollback_mutations(self):
        args, override, calls = self.admin_switch_fixture(change_wrapper=True)
        try:
            with self.assertRaises(admin.AdminSwitchError) as raised:
                admin.switch_admin(self.fx.new, **args)
        except TypeError as exc:
            self.fail(f'WSL admin switch must freeze the wrapper identity: {exc}')
        self.assertEqual(raised.exception.code, 'ADMIN_ROLLBACK_FAILED')
        self.assertFalse(any(item[2:3] == ['restart'] for item in calls))
        self.assertTrue(override.exists())

    def test_admin_restart_must_replace_the_frozen_mainpid_identity(self):
        args, override, calls = self.admin_switch_fixture()
        command = args['command_fn']
        def ineffective_restart(argv, **kwargs):
            return b'' if argv[2:3] == ['restart'] else command(argv, **kwargs)
        args['command_fn'] = ineffective_restart
        with self.assertRaises(admin.AdminSwitchError):
            admin.switch_admin(self.fx.old, **args)


if __name__ == '__main__':
    unittest.main()
