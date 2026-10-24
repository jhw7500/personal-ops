"""Host command lifecycle tests use disposable children, never real user units."""
from __future__ import annotations

import hashlib
import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_command_runner import MAX_OUTPUT_BYTES, run_host_profile  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


ENTRY_FIXTURE = b"""#!/usr/bin/python3 -I
import hashlib, os, sys
args = sys.argv[1:]
if args[0] == '--runtime-layout':
    args = args[1:]
expected, executable, project_root, relative = args[:4]
assert args[4] == '--'
with open(executable, 'rb') as source:
    assert hashlib.sha256(source.read()).hexdigest() == expected
os.chdir(os.path.join(project_root, relative))
environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
os.execve(executable, [executable, *args[5:]], environment)
"""


def _start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19])


class FixtureSupervisor:
    """Local process-group fixture for runner logic; production uses cgroups."""

    def __init__(self, *, fail_stop=False):
        self.process = None
        self.identity = None
        self.fail_stop = fail_stop
        self.stop_calls = 0
        self.finish_calls = 0
        self.forgot = False
        self.started_argv = None
        self.started_env = None
        self.stdin_limit = None

    async def start(self, task_id, argv, workspace, env, timeout_seconds, *,
                    expected_bootstrap_sha256):
        import asyncio
        self.started_argv = list(argv)
        self.started_env = dict(env)
        gate = ("import os,sys; assert os.read(0, 3) == b'GO\\n'; "
                "os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", gate, *argv, cwd=workspace, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        if self.stdin_limit is not None:
            pipe = self.process.stdin.transport.get_extra_info('pipe')
            fcntl.fcntl(pipe.fileno(), getattr(fcntl, 'F_SETPIPE_SZ', 1031), 4096)
            self.process.stdin.transport.set_write_buffer_limits(high=self.stdin_limit, low=0)
        pid = self.process.pid
        unit = "personal-ops-agent-" + "a" * 32 + ".service"
        group = (f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                 f"app.slice/{unit}")
        self.identity = {
            "task_id": task_id, "unit": unit, "invocation_id": "b" * 32,
            "cgroup": group, "boot_id": "11111111-2222-3333-4444-555555555555",
            "uid": os.getuid(), "pid": pid, "start_ticks": _start_ticks(pid),
            "wrapper_pid": pid, "wrapper_start_ticks": _start_ticks(pid),
        }
        return self.process, dict(self.identity)

    async def _terminate(self):
        import asyncio
        if self.fail_stop:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
            raise HostRuntimeError("synthetic fixture refusal")
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await self.process.wait()
        for _ in range(50):
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return {"termination_verified": True}
            await asyncio.sleep(0.01)
        raise HostRuntimeError("synthetic fixture still populated")

    async def stop(self, identity):
        self.stop_calls += 1
        self.assert_identity(identity)
        return await self._terminate()

    async def finish(self, identity):
        self.finish_calls += 1
        self.assert_identity(identity)
        return await self._terminate()

    def assert_identity(self, identity):
        if identity != self.identity:
            raise HostRuntimeError("synthetic identity mismatch")

    def forget(self, identity):
        self.assert_identity(identity)
        self.forgot = True


class HostCommandRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-command-runner-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir(mode=0o700)
        self.execution = self.base / "execution"
        self.entry_source = self.base / "host_command_entry.py"
        self.entry_source.write_bytes(ENTRY_FIXTURE)
        self.entry_source.chmod(0o600)
        self.script = self.base / "test-script"
        self.write_script("printf 'PRIVATE stdout\\n'; printf 'SECRET stderr\\n' >&2")
        self.supervisor = FixtureSupervisor()
        self.identities = []

    def write_script(self, body: str):
        self.script.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        self.script.chmod(0o700)

    def frozen(self, argv=None, seconds=10):
        return {
            "executable": str(self.script),
            "executable_sha256": hashlib.sha256(self.script.read_bytes()).hexdigest(),
            "argv": argv or [], "project_root": str(self.project), "cwd_relative": ".",
            "timeout_seconds": seconds,
        }

    def execute(self, frozen=None, cancel=None, *, supervisor=None, on_identity=None):
        supervisor = supervisor or self.supervisor
        with mock.patch("host_command_runner.COMMAND_ENTRY_SOURCE", self.entry_source), \
                mock.patch("host_command_runner.build_bootstrap") as build:
            build.return_value = {"path": str(self.execution / "personal-ops-agent-bootstrap"),
                                  "sha256": "c" * 64, "source_sha256": "d" * 64}
            return run_host_profile(
                frozen or self.frozen(), cancel or threading.Event(),
                execution_dir=self.execution, operation_id="e" * 32,
                on_identity=on_identity or self.identities.append,
                supervisor_factory=lambda: supervisor,
            )

    def test_identity_is_durable_before_go_and_result_is_bounded(self):
        marker = self.project / "started"
        self.write_script(f"touch {marker}; printf output; printf error >&2")

        def persisted(identity):
            self.assertFalse(marker.exists())
            self.identities.append(identity)

        result = self.execute(on_identity=persisted)

        self.assertEqual(result, {"exit_code": 0, "duration_ms": result["duration_ms"],
                                  "stdout_bytes": 6, "stderr_bytes": 5,
                                  "termination_verified": True})
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(self.identities, [self.supervisor.identity])
        self.assertTrue(self.supervisor.forgot)
        self.assertEqual((self.execution / "command-entry").stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.supervisor.started_argv[0], str(self.execution / "command-entry"))
        self.assertEqual(self.supervisor.started_argv[1:6], [
            self.frozen()["executable_sha256"], str(self.script), str(self.project), ".", "--"])
        self.assertNotIn("GIT_TERMINAL_PROMPT", self.supervisor.started_env)
        self.assertNotIn("GIT_OPTIONAL_LOCKS", self.supervisor.started_env)

    def test_parent_exit_with_live_descendant_is_stopped_before_success_returns(self):
        child = self.project / "child.pid"
        self.write_script(f"sleep 60 </dev/null >/dev/null 2>&1 & echo $! > {child}; exit 0")
        result = self.execute()
        descendant = int(child.read_text())
        self.assertTrue(result["termination_verified"])
        self.assertEqual(self.supervisor.finish_calls, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_argument_is_not_evaluated_as_shell(self):
        self.write_script("printf '%s\\n' \"$1\"")
        attack = "$(touch " + str(self.base / "pwned") + ")"
        result = self.execute(self.frozen([attack]))
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse((self.base / "pwned").exists())

    def test_copied_production_entry_executes_pinned_bytes(self):
        self.write_script("printf production-entry")
        actual_execution = self.base / "actual-entry-execution"
        result = run_host_profile(
            self.frozen(), threading.Event(), execution_dir=actual_execution,
            operation_id="f" * 32, on_identity=self.identities.append,
            supervisor_factory=lambda: self.supervisor)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout_bytes"], len(b"production-entry"))
        self.assertTrue(result["termination_verified"])
        self.assertEqual((actual_execution / "personal-ops-agent-bootstrap").stat().st_mode
                         & 0o777, 0o700)

    def test_timeout_stops_owned_scope_and_preserves_unrelated_process(self):
        self.write_script("sleep 60")
        unrelated = subprocess.Popen(["/bin/sh", "-c", "sleep 60"], start_new_session=True)
        def cleanup_unrelated():
            if unrelated.poll() is None:
                os.killpg(unrelated.pid, signal.SIGKILL)
            unrelated.wait()
        self.addCleanup(cleanup_unrelated)
        started = time.monotonic()
        with self.assertRaises(HostRuntimeError) as caught:
            self.execute(self.frozen(seconds=1))
        self.assertEqual(caught.exception.code, "HOST_COMMAND_TIMEOUT")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.supervisor.stop_calls, 1)
        self.assertIsNone(unrelated.poll())
        self.assertEqual(set(caught.exception.result),
                         {"exit_code", "duration_ms", "stdout_bytes", "stderr_bytes"})

    def test_cancel_after_start_stops_owned_scope(self):
        self.write_script("sleep 60")
        cancel = threading.Event()
        outcome = []

        def target():
            try:
                outcome.append(self.execute(cancel=cancel))
            except HostRuntimeError as exc:
                outcome.append(exc.code)

        thread = threading.Thread(target=target)
        thread.start()
        for _ in range(100):
            if self.identities:
                break
            time.sleep(0.01)
        cancel.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["HOST_COMMAND_CANCELLED"])
        self.assertEqual(self.supervisor.stop_calls, 1)

    def test_identity_persistence_failure_never_sends_go(self):
        marker = self.project / "started"
        self.write_script(f"touch {marker}")

        def fail(_identity):
            raise RuntimeError("synthetic durable write failure")

        with self.assertRaisesRegex(RuntimeError, "durable write failure"):
            self.execute(on_identity=fail)
        self.assertFalse(marker.exists())
        self.assertEqual(self.supervisor.stop_calls, 1)

    def test_unverified_termination_replaces_timeout_and_keeps_ownership(self):
        self.write_script("sleep 60")
        supervisor = FixtureSupervisor(fail_stop=True)
        with self.assertRaises(HostRuntimeError) as caught:
            self.execute(self.frozen(seconds=1), supervisor=supervisor)
        self.assertEqual(caught.exception.code, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        self.assertFalse(supervisor.forgot)
        self.assertEqual(set(caught.exception.result),
                         {"exit_code", "duration_ms", "stdout_bytes", "stderr_bytes"})

    def test_cancel_before_start_has_no_identity_or_process(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(HostRuntimeError) as caught:
            self.execute(cancel=cancel)
        self.assertEqual(caught.exception.code, "HOST_COMMAND_CANCELLED")
        self.assertIsNone(self.supervisor.process)
        self.assertEqual(self.identities, [])

    def test_output_limit_stops_owned_scope(self):
        self.write_script("head -c " + str(MAX_OUTPUT_BYTES + 80000) + " /dev/zero")
        with self.assertRaises(HostRuntimeError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "HOST_COMMAND_OUTPUT_LIMIT")
        self.assertEqual(self.supervisor.stop_calls, 1)

    def test_optional_guard_stdin_and_output_sink_preserve_streams(self):
        self.write_script("read line; printf 'out:%s' \"$line\"; printf 'err:%s' \"$line\" >&2")
        events = []

        @contextmanager
        def guard():
            events.append("guard-enter")
            try:
                yield
            finally:
                events.append("guard-exit")

        result = self.execute_with_options(
            go_guard=guard,
            input_bytes=b"hello\n",
            on_output=lambda stream, block: events.append((stream, block)),
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(events[:2], ["guard-enter", "guard-exit"])
        self.assertIn(("stdout", b"out:hello"), events)
        self.assertIn(("stderr", b"err:hello"), events)

    def test_codex_kind_uses_runtime_layout_and_helper_path(self):
        frozen = self.frozen(["one"])
        frozen["kind"] = "codex"
        with mock.patch("host_command_runner.COMMAND_ENTRY_SOURCE", self.entry_source), \
                mock.patch("host_command_runner.build_bootstrap") as build:
            build.return_value = {"path": str(self.execution / "personal-ops-agent-bootstrap"),
                                  "sha256": "c" * 64, "source_sha256": "d" * 64}
            result = run_host_profile(
                frozen, threading.Event(), execution_dir=self.execution,
                operation_id="e" * 32, on_identity=self.identities.append,
                supervisor_factory=lambda: self.supervisor,
                path_prefix=self.script.parent)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.supervisor.started_argv[1:7], [
            "--runtime-layout", frozen["executable_sha256"], str(self.script),
            str(self.project), ".", "--"])
        self.assertEqual(self.supervisor.started_env["PATH"].split(os.pathsep)[0],
                         str(self.script.parent))

    def test_blocked_stdin_drain_cannot_delay_cancellation(self):
        self.write_script("sleep 2")
        self.supervisor.stdin_limit = 1024
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        before = time.monotonic()
        with self.assertRaises(HostRuntimeError) as caught:
            self.execute_with_options(cancel=cancel, input_bytes=b'x' * 32768)
        self.assertEqual(caught.exception.code, 'HOST_COMMAND_CANCELLED')
        self.assertLess(time.monotonic() - before, 1)
        self.assertEqual(self.supervisor.stop_calls, 1)

    def execute_with_options(self, cancel=None, **options):
        with mock.patch("host_command_runner.COMMAND_ENTRY_SOURCE", self.entry_source), \
                mock.patch("host_command_runner.build_bootstrap") as build:
            build.return_value = {"path": str(self.execution / "personal-ops-agent-bootstrap"),
                                  "sha256": "c" * 64, "source_sha256": "d" * 64}
            return run_host_profile(
                self.frozen(), cancel or threading.Event(), execution_dir=self.execution,
                operation_id="e" * 32, on_identity=self.identities.append,
                supervisor_factory=lambda: self.supervisor, **options)


if __name__ == "__main__":
    unittest.main()
