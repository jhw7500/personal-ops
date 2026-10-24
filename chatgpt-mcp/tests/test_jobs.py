import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution import ExecutionPolicy  # noqa: E402
from jobs import JobManager, MAX_LOG_BYTES  # noqa: E402


class JobManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chatgpt-mcp-jobs-")
        self.root = Path(self.temporary.name)
        self.projects = {name: self.root / name for name in ("alpha", "beta")}
        for path in self.projects.values():
            path.mkdir()
        self.policy = ExecutionPolicy(self.projects, config_path=self.root / "missing.json")
        self.manager = JobManager(self.projects, self.policy, self.root / "jobs")

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def finish(self, record):
        task = self.manager.tasks.get(record["job_id"])
        if task:
            await asyncio.wait_for(asyncio.shield(task), 10)
        return self.manager.get(record["project"], record["job_id"])

    async def test_returns_before_completion_and_reads_incremental_logs(self):
        record = await self.manager.start(
            "alpha", request_id="first", argv=["bash", "-c", "printf start; sleep .3; printf end"])
        self.assertEqual(record["state"], "queued")
        self.assertIsNone(record["exit_code"])
        final = await self.finish(record)
        self.assertEqual(final["state"], "succeeded", final)
        first = self.manager.read_log("alpha", record["job_id"], max_bytes=5)
        second = self.manager.read_log("alpha", record["job_id"], offset=first["next_offset"])
        self.assertEqual(first["text"] + second["text"], "startend")
        self.assertTrue(first["has_more"])
        self.assertTrue(second["complete"])
        self.assertFalse(second["has_more"])
        for path in self.manager.state_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())

    async def test_launches_execution_argv_but_records_visible_argv(self):
        original = self.policy.prepare("alpha", argv=["true"])
        execution_argv = ["bash", "-c", "printf wrapped"]
        spec = replace(
            original,
            command=[*original.command[:-len(original.execution_argv)], *execution_argv],
            argv=["npm", "run", "test:integration"],
            execution_argv=execution_argv,
        )
        with mock.patch.object(self.policy, "prepare", return_value=spec):
            record = await self.manager.start(
                "alpha", request_id="wrapped", preset="database-test")
        final = await self.finish(record)
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["argv"], ["npm", "run", "test:integration"])
        self.assertEqual(
            self.manager.read_log("alpha", record["job_id"])["text"], "wrapped")

    async def test_idempotent_retry_does_not_repeat_a_command(self):
        arguments = dict(request_id="same", argv=["bash", "-c", "printf x >> once.txt"])
        one = await self.manager.start("alpha", **arguments)
        retry = await self.manager.start("alpha", **arguments)
        self.assertEqual(one["job_id"], retry["job_id"])
        await self.finish(one)
        with self.assertRaisesRegex(ValueError, "different command"):
            await self.manager.start("alpha", request_id="same", argv=["true"])
        self.assertEqual((self.projects["alpha"] / "once.txt").read_text(), "x")

    async def test_project_lock_cross_project_access_and_parallel_jobs(self):
        one = await self.manager.start("alpha", request_id="one", argv=["sleep", ".2"])
        with self.assertRaisesRegex(ValueError, "PROJECT_BUSY"):
            await self.manager.start("alpha", request_id="other", argv=["true"])
        two = await self.manager.start("beta", request_id="two", argv=["true"])
        with self.assertRaises(ValueError):
            self.manager.get("beta", one["job_id"])
        with self.assertRaises(ValueError):
            self.manager.read_log("beta", one["job_id"])
        await asyncio.gather(self.finish(one), self.finish(two))
        self.assertFalse(self.manager.locks["alpha"].locked())

    async def test_failure_timeout_and_cancel_keep_results(self):
        failure = await self.manager.start("alpha", request_id="failure", argv=["bash", "-c", "exit 7"])
        self.assertEqual((await self.finish(failure))["exit_code"], 7)
        timed = await self.manager.start("alpha", request_id="timeout", argv=["sleep", "30"],
                                         timeout_seconds=1)
        self.assertEqual((await self.finish(timed))["state"], "timed_out")
        canceled = await self.manager.start("alpha", request_id="cancel", argv=["sleep", "30"])
        result = await self.manager.cancel("alpha", canceled["job_id"])
        self.assertEqual(result["state"], "canceled")
        self.assertEqual(len(self.manager.list("alpha")["jobs"]), 3)

    async def test_actual_postgresql_job_isolated_and_torn_down_on_cancel_and_timeout(self):
        prefix = Path(os.environ.get(
            "MCP_TEST_POSTGRESQL_PREFIX",
            "/home/jhw/.local/lib/chatgpt-coding-mcp/toolchains/postgresql-18.6",
        ))
        if not self.policy._bwrap or not prefix.joinpath("bin/postgres").exists():
            self.skipTest("actual PostgreSQL MCP test toolchain is unavailable")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host_port = listener.getsockname()[1]
        host_netns = os.readlink("/proc/self/ns/net")
        child = (
            "import json, os, pathlib, socket, sys, time; "
            "probe=pathlib.Path('/opt/postgresql/chatgpt-mcp-write-probe'); "
            "writable=True; "
            "\ntry: probe.write_text('unsafe')\nexcept OSError: writable=False\n"
            "assert not writable; "
            "sock=socket.socket(); "
            "assert sock.connect_ex(('127.0.0.1', int(sys.argv[2]))) != 0; "
            "sock.close(); "
            "netns=os.readlink('/proc/self/ns/net'); "
            "assert netns != sys.argv[3]; "
            "assert os.environ['TEST_DATABASE_URL'].endswith('/tarororo_test'); "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'netns':netns})); "
            "time.sleep(30)"
        )
        profiles = {}
        for name in ("cancel", "timeout"):
            profiles[f"db-{name}"] = {
                "argv": [
                    "python3", "-c", child, f"started-{name}.json",
                    str(host_port), host_netns,
                ],
                "test_database": "tarororo_test",
                "timeout_seconds": 30,
            }
        config = self.root / "postgresql-execution.json"
        config.write_text(json.dumps({
            "version": 1,
            "toolchains": {"alpha": {"postgresql": str(prefix)}},
            "profiles": {"alpha": profiles},
        }), encoding="utf-8")
        config.chmod(0o600)
        manager = JobManager(
            self.projects,
            ExecutionPolicy(self.projects, config_path=config),
            self.root / "postgresql-jobs",
        )

        async def wait_started(path, task):
            for _ in range(100):
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))["netns"]
                if task.done():
                    break
                await asyncio.sleep(0.1)
            self.fail("PostgreSQL child did not start: " + repr(task.exception()))

        def processes_in_namespace(netns):
            result = []
            for path in Path("/proc").glob("[0-9]*/ns/net"):
                try:
                    if os.readlink(path) == netns:
                        result.append(int(path.parts[2]))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            return result

        try:
            cancel_record = await manager.start(
                "alpha", request_id="postgres-cancel", preset="db-cancel")
            cancel_task = manager.tasks[cancel_record["job_id"]]
            cancel_namespace = await wait_started(
                self.projects["alpha"] / "started-cancel.json", cancel_task)
            canceled = await manager.cancel("alpha", cancel_record["job_id"])
            await asyncio.wait_for(asyncio.shield(cancel_task), 10)
            self.assertEqual(canceled["state"], "canceled")
            self.assertEqual(processes_in_namespace(cancel_namespace), [])

            timeout_record = await manager.start(
                "alpha", request_id="postgres-timeout", preset="db-timeout",
                timeout_seconds=5)
            timeout_task = manager.tasks[timeout_record["job_id"]]
            timeout_namespace = await wait_started(
                self.projects["alpha"] / "started-timeout.json", timeout_task)
            await asyncio.wait_for(asyncio.shield(timeout_task), 15)
            timed_out = manager.get("alpha", timeout_record["job_id"])
            self.assertEqual(timed_out["state"], "timed_out")
            self.assertEqual(processes_in_namespace(timeout_namespace), [])
        finally:
            listener.close()
            await manager.close()

    async def test_output_cap_drains_the_process_and_preserves_utf8(self):
        record = await self.manager.start("alpha", request_id="large", argv=[
            "python3", "-c", "import sys;sys.stdout.write('x'*1100000)"])
        result = await self.finish(record)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["log_bytes"], MAX_LOG_BYTES)
        self.assertTrue(result["log_truncated"])
        unicode_record = await self.manager.start("beta", request_id="utf8", argv=[
            "python3", "-c", "print('작업 완료')"])
        await self.finish(unicode_record)
        self.assertEqual(self.manager.read_log("beta", unicode_record["job_id"])["text"], "작업 완료\n")

    async def test_shutdown_and_restart_do_not_signal_unowned_processes(self):
        record = await self.manager.start("alpha", request_id="shutdown", argv=["sleep", "30"])
        await asyncio.wait_for(self.manager.close(), 10)
        self.assertEqual(self.manager.get("alpha", record["job_id"])["state"], "canceled")
        path = self.manager.state_dir / (record["job_id"] + ".json")
        previous = json.loads(path.read_text())
        previous["state"] = "running"
        path.write_text(json.dumps(previous))
        restored = JobManager(self.projects, self.policy, self.manager.state_dir)
        self.assertEqual(restored.get("alpha", record["job_id"])["state"], "interrupted")
        self.assertIsNone(restored.get("alpha", record["job_id"])["exit_code"])
        await restored.close()

    async def test_state_and_log_symlinks_and_permissions_are_rejected(self):
        self.root.joinpath("link").symlink_to(self.root / "jobs", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            JobManager(self.projects, self.policy, self.root / "link")
        with self.assertRaisesRegex(ValueError, "outside"):
            JobManager(self.projects, self.policy, self.projects["alpha"] / "state")
        record = await self.manager.start("alpha", request_id="path", argv=["true"])
        await self.finish(record)
        log = self.manager.state_dir / (record["job_id"] + ".log")
        log.unlink()
        log.symlink_to(self.projects["alpha"] / "secret")
        with self.assertRaises(ValueError):
            self.manager.read_log("alpha", record["job_id"])
        with self.assertRaises(ValueError):
            self.manager.read_log("alpha", "../state")


if __name__ == "__main__":
    unittest.main()
