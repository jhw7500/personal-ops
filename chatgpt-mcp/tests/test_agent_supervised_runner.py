"""Runner lifecycle at the supervisor boundary; disposable CLI, no models."""
import asyncio
import hashlib
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from agent_runner import CodexAgentRunner, _process_start_ticks
from host_runtime_config import HostRuntimeError


class FixtureSupervisor:
    """Only a test subprocess; production uses the systemd/cgroup supervisor."""
    def __init__(self):
        self.process = None
        self.stopped = False
        self.stop_calls = 0

    async def start(self, task_id, argv, workspace, env, timeout_seconds, *,
                    expected_bootstrap_sha256):
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=workspace, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        pid = self.process.pid
        identity = {"task_id": task_id, "unit": "personal-ops-agent-" + "a" * 32 + ".service",
                    "invocation_id": "b" * 32,
                    "cgroup": f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/personal-ops-agent-" + "a" * 32 + ".service",
                    "boot_id": "11111111-2222-3333-4444-555555555555", "uid": os.getuid(),
                    "pid": pid, "start_ticks": _process_start_ticks(pid),
                    "wrapper_pid": pid, "wrapper_start_ticks": _process_start_ticks(pid)}
        return self.process, identity

    async def stop(self, identity):
        self.stop_calls += 1
        if self.process.returncode is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()
        self.stopped = True
        return {"termination_verified": True}

    async def finish(self, identity):
        return {"termination_verified": self.process.returncode is not None}

    def forget(self, identity):
        pass


class RetryStopSupervisor(FixtureSupervisor):
    def __init__(self):
        super().__init__()
        self.stop_attempts = 0

    async def stop(self, identity):
        self.stop_attempts += 1
        if self.stop_attempts == 1:
            raise RuntimeError("synthetic stop verification failure")
        return await super().stop(identity)


class SlowStartSupervisor(FixtureSupervisor):
    def __init__(self):
        super().__init__()
        self.cancelled = False
        self.cleanup_complete = False

    async def start(self, *args, **kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0.03)
            self.cleanup_complete = True
            raise


class SupervisedRunnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supervised-runner-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.executable = self.root / "codex"
        self.result = {"summary": "Synthetic review", "changed_files": [],
                       "validations": ["Fixture only"], "remaining": [],
                       "approval_required": True}
        self.executable.write_text(
            "#!/usr/bin/python3\nimport json,os,sys\n"
            "assert sys.stdin.readline() == 'GO\\n'\n"
            "sys.stdin.read()\n"
            "path=sys.argv[sys.argv.index('--output-last-message')+1]\n"
            f"payload={self.result!r}\n"
            "with open(path,'w') as stream: json.dump(payload,stream)\n"
            "os.chmod(path,0o600)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}),flush=True)\n")
        self.executable.chmod(0o700)
        self.profile = {"project": "fixture", "provider": "codex", "enabled": True,
                        "executable": str(self.executable),
                        "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                        "model": "fixture", "reasoning_effort": "low", "host_capabilities": [],
                        "apply_policy": "review_only", "isolation_receipt": "a"*64,
                        "smoke_receipt": "b"*64}
        self.task = {"task_id": "d"*32, "objective": "Fixture only", "timeout_minutes": 1}

    async def test_identity_is_persisted_before_go_and_valid_result_is_returned(self):
        supervisor = FixtureSupervisor()
        runner = CodexAgentRunner(supervisor=supervisor)
        identities, events = [], []
        runner.set_task_callbacks(self.task['task_id'], on_identity=identities.append,
                                  on_event=events.append)
        argv = [str(self.executable), '--output-last-message',
                str(self.workspace / '.agent-control/last-message.json')]
        with mock.patch('agent_runner.compile_codex_profile', return_value=argv), \
                mock.patch('agent_runner.sandbox_helper', return_value=self.executable), \
                mock.patch('agent_runner.validate_bootstrap', return_value='c' * 64):
            result = await runner.run(self.task, self.workspace, self.profile)
        self.assertTrue(result['verified'])
        self.assertTrue(result['termination_verified'])
        self.assertEqual(result['structured_result'], self.result)
        self.assertEqual(identities[0]['task_id'], self.task['task_id'])
        self.assertEqual(events[0]['code'], 'TURN_COMPLETED')
        self.assertEqual(runner.processes, {})

    async def test_identity_callback_failure_never_sends_go_and_cleans_owned_task(self):
        supervisor = FixtureSupervisor()
        runner = CodexAgentRunner(supervisor=supervisor)
        def fail(_):
            raise RuntimeError('synthetic persistence failure')
        runner.set_task_callbacks(self.task['task_id'], on_identity=fail)
        argv = [str(self.executable), '--output-last-message',
                str(self.workspace / '.agent-control/last-message.json')]
        with mock.patch('agent_runner.compile_codex_profile', return_value=argv), \
                mock.patch('agent_runner.sandbox_helper', return_value=self.executable), \
                mock.patch('agent_runner.validate_bootstrap', return_value='c' * 64):
            with self.assertRaises(RuntimeError):
                await runner.run(self.task, self.workspace, self.profile)
        self.assertTrue(supervisor.stopped)
        self.assertFalse((self.workspace / '.agent-control/last-message.json').exists())

    async def test_failed_stop_retains_owned_identity_for_close_retry(self):
        supervisor = RetryStopSupervisor()
        runner = CodexAgentRunner(supervisor=supervisor)

        def fail(_):
            raise RuntimeError('synthetic persistence failure')

        runner.set_task_callbacks(self.task['task_id'], on_identity=fail)
        argv = [str(self.executable), '--output-last-message',
                str(self.workspace / '.agent-control/last-message.json')]
        with mock.patch('agent_runner.compile_codex_profile', return_value=argv), \
                mock.patch('agent_runner.sandbox_helper', return_value=self.executable), \
                mock.patch('agent_runner.validate_bootstrap', return_value='c' * 64):
            with self.assertRaises(RuntimeError):
                await runner.run(self.task, self.workspace, self.profile)
        self.assertIn(self.task['task_id'], runner.identities)
        self.assertFalse(runner.termination[self.task['task_id']])
        await runner.close()
        self.assertEqual(supervisor.stop_attempts, 2)
        self.assertEqual(runner.identities, {})

    async def test_start_deadline_cancels_supervisor_start_before_returning(self):
        supervisor = SlowStartSupervisor()
        runner = CodexAgentRunner(supervisor=supervisor)
        argv = [str(self.executable), '--output-last-message',
                str(self.workspace / '.agent-control/last-message.json')]
        with mock.patch('agent_runner.compile_codex_profile', return_value=argv), \
                mock.patch('agent_runner.sandbox_helper', return_value=self.executable), \
                mock.patch('agent_runner.validate_bootstrap', return_value='c' * 64), \
                mock.patch('agent_runner.START_TOTAL_SECONDS', 0.02):
            with self.assertRaises(TimeoutError):
                await runner.run(self.task, self.workspace, self.profile)
        self.assertTrue(supervisor.cancelled)
        self.assertTrue(supervisor.cleanup_complete)
        self.assertEqual(runner.launches, {})

    async def test_timeout_keeps_successful_termination_proof(self):
        self.executable.write_text(
            "#!/usr/bin/python3\nimport sys,time\n"
            "assert sys.stdin.readline() == 'GO\\n'\n"
            "sys.stdin.read()\n"
            "time.sleep(60)\n")
        self.executable.chmod(0o700)
        self.profile['executable_sha256'] = hashlib.sha256(
            self.executable.read_bytes()).hexdigest()
        self.task['timeout_minutes'] = 0.001
        supervisor = FixtureSupervisor()
        runner = CodexAgentRunner(supervisor=supervisor)
        argv = [str(self.executable), '--output-last-message',
                str(self.workspace / '.agent-control/last-message.json')]
        with mock.patch('agent_runner.compile_codex_profile', return_value=argv), \
                mock.patch('agent_runner.sandbox_helper', return_value=self.executable), \
                mock.patch('agent_runner.validate_bootstrap', return_value='c' * 64):
            with self.assertRaises(HostRuntimeError) as caught:
                await runner.run(self.task, self.workspace, self.profile)
        self.assertEqual(str(caught.exception), 'AGENT_TIMEOUT')
        self.assertTrue(runner.termination[self.task['task_id']])
        self.assertEqual(supervisor.stop_calls, 1)
        self.assertEqual(runner.identities, {})


if __name__ == '__main__':
    unittest.main()
