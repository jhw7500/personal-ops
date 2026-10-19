"""Offline probes preserve actual frontend homes and test real alias denial."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

import agent_probe


class ProbeEnvironmentTest(unittest.TestCase):
    def test_failed_command_retains_private_bounded_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'probe-output.json'
            with self.assertRaisesRegex(agent_probe.ProbeError, 'PROBE_EXECUTION_FAILED'):
                agent_probe._bounded_command(
                    [sys.executable, '-c', 'import sys; sys.stderr.write("fixture error"*10000); sys.exit(1)'],
                    cwd=root, env={'PATH': '/usr/bin:/bin'}, output=output)
            diagnostic = output.with_suffix('.stderr.log')
            self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(diagnostic.stat().st_size, 65536)
            self.assertIn(b'fixture error', diagnostic.read_bytes())

    def test_unsandboxed_external_peer_is_a_real_negative_control(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sentinel = root / 'outside-read'
            sentinel.write_bytes(b'synthetic fixture only')
            peer = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                                    env={'PATH': '/usr/bin:/bin'},
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: peer.poll() is None and peer.kill())
            try:
                process = subprocess.run(
                    [sys.executable, '-c', agent_probe.PROBE_SCRIPT, str(os.getuid()),
                     str(root), str(peer.pid), str(sentinel), os.readlink('/proc/self/ns/pid')],
                    cwd=root, env={'PATH': '/usr/bin:/bin'}, capture_output=True, check=True)
                report = json.loads(process.stdout)
                self.assertFalse(report['external_environ_denied'])
                self.assertFalse(report['external_cmdline_denied'])
                self.assertFalse(report['external_root_alias_denied'])
                self.assertFalse(report['private_pid_namespace'])
            finally:
                peer.terminate()
                peer.wait(timeout=5)

    def test_probe_does_not_replace_frontend_home_or_codex_home(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recorded = {}
            def run(argv, **kwargs):
                recorded.update(argv=argv, env=kwargs['env'])
                return json.dumps({name: True for name in agent_probe.SANDBOX_ASSERTIONS}).encode()
            with mock.patch.object(agent_probe, '_bounded_command', side_effect=run):
                agent_probe._run_sandbox(root/'codex', 'policy', root,
                                         root/'fake-home', root/'fake-codex', root/'probe.py')
            self.assertEqual(recorded['env']['HOME'], os.environ['HOME'])
            self.assertEqual(recorded['env'].get('CODEX_HOME'), os.environ.get('CODEX_HOME'))
            self.assertIn(str(root/'fake-home'), recorded['argv'])


if __name__ == '__main__':
    unittest.main()
