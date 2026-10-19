import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server.py"
sys.path.insert(0, str(SERVER.parent))
import server  # noqa: E402


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return None if fields[0] == "Z" else fields[19]


class ServerShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def check_signal_cleanup(self, signum, *, command_close_fails=False,
                                   console_close_fails=False):
        with tempfile.TemporaryDirectory(prefix="mcp-signal-cleanup-") as folder:
            root = Path(folder)
            project = root / "repo"
            project.mkdir()
            config = root / "projects.json"
            project_name = "tossApp" if console_close_fails else "alpha"
            config.write_text(json.dumps({"projects": {project_name: str(project)}}))
            state = root / "state"
            child_pid = None
            child_identity = None
            entry = SERVER
            if command_close_fails or console_close_fails:
                entry = root / "launcher.py"
                injections = ""
                if command_close_fails:
                    injections += (
                        "async def broken_close(self):\n"
                        f"    Path({str(root / 'command-close-failed')!r}).touch()\n"
                        "    raise HostRuntimeError('HOST_COMMAND_PROCESS_TREE_UNVERIFIED')\n"
                        "server.host_command_tools.HostCommandTools.close = broken_close\n")
                if console_close_fails:
                    injections += (
                        "class BrokenConsoleConnection:\n"
                        "    def __init__(self, *args, **kwargs): pass\n"
                        "    async def get_provider(self): return None\n"
                        "    async def get_ux019_provider(self): return None\n"
                        "    async def close(self):\n"
                        f"        Path({str(root / 'console-close-failed')!r}).touch()\n"
                        "        raise RuntimeError('private console close failure')\n"
                        "server.toss_console_codex.CodexConsoleConnection = BrokenConsoleConnection\n")
                entry.write_text(
                    "import sys\n"
                    "from pathlib import Path\n"
                    f"sys.path.insert(0, {str(SERVER.parent)!r})\n"
                    "import server\n"
                    "server.SERENA_ENV = Path(sys.prefix)\n"
                    "from host_runtime_config import HostRuntimeError\n"
                    "job_close = server.JobManager.close\n"
                    "async def recorded_job_close(self):\n"
                    "    await job_close(self)\n"
                    f"    Path({str(root / 'job-close-completed')!r}).touch()\n"
                    + injections +
                    "server.JobManager.close = recorded_job_close\n"
                    "server.main()\n")
            with (root / "server.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(entry), "--config", str(config),
                    env=dict(os.environ, XDG_STATE_HOME=str(state)),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                )
                try:
                    async def request(method, parameters, request_id=None):
                        message = {"jsonrpc": "2.0", "method": method, "params": parameters}
                        if request_id is not None:
                            message["id"] = request_id
                        process.stdin.write(json.dumps(message).encode() + b"\n")
                        await process.stdin.drain()
                        if request_id is None:
                            return None
                        while True:
                            line = await asyncio.wait_for(process.stdout.readline(), 10)
                            self.assertTrue(line, "MCP server exited before responding")
                            response = json.loads(line)
                            if response.get("id") == request_id:
                                self.assertNotIn("error", response)
                                return response["result"]

                    await request("initialize", {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "shutdown-test", "version": "1"},
                    }, 1)
                    await request("notifications/initialized", {})
                    started = await request("tools/call", {
                        "name": "start_command", "arguments": {
                            "project": project_name, "request_id": "signal-cleanup",
                            "argv": ["bash", "-c", "printf ready; sleep 30"],
                        },
                    }, 2)
                    self.assertFalse(started.get("isError", False), started)
                    job_id = started["structuredContent"]["job_id"]
                    record_path = state / "chatgpt-coding-mcp/jobs" / (job_id + ".json")
                    for _ in range(100):
                        record = json.loads(record_path.read_bytes())
                        if record["state"] == "running":
                            break
                        await asyncio.sleep(.02)
                    self.assertEqual(record["state"], "running", record)
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                    for candidate in children:
                        command = Path(f"/proc/{candidate}/cmdline").read_bytes().split(b"\0")
                        if command and Path(os.fsdecode(command[0])).name == "bwrap":
                            child_pid = int(candidate)
                            child_identity = process_identity(child_pid)
                            break
                    self.assertIsNotNone(child_pid, "sandbox process must be observed before termination")
                    process.send_signal(signum)
                    # Tunnel replacement also closes the old stdio transport.
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 8)
                    record = json.loads(record_path.read_bytes())
                    self.assertIn(record["state"], {"canceled", "interrupted"}, record)
                    self.assertEqual(record["error"], "Server stopped while the job was active")
                    self.assertIsNotNone(record["finished_at"])
                    self.assertNotEqual(process_identity(child_pid), child_identity,
                                        "sandbox process remained alive after server shutdown")
                    if command_close_fails:
                        self.assertTrue((root / 'command-close-failed').exists(),
                                        "shutdown fault injection must have executed")
                        self.assertTrue((root / 'job-close-completed').exists(),
                                        "existing jobs must finish their explicit cleanup")
                    if console_close_fails:
                        self.assertTrue((root / 'console-close-failed').exists(),
                                        "console shutdown fault injection must have executed")
                        self.assertTrue((root / 'job-close-completed').exists(),
                                        "existing jobs must close after console close fails")
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    if child_pid is not None and process_identity(child_pid) == child_identity:
                        os.killpg(child_pid, signal.SIGKILL)

    async def test_sigterm_finalizes_active_job_and_reaps_sandbox(self):
        await self.check_signal_cleanup(signal.SIGTERM)

    async def test_sigint_finalizes_active_job_and_reaps_sandbox(self):
        await self.check_signal_cleanup(signal.SIGINT)

    async def test_host_command_close_failure_still_finalizes_existing_sandbox_jobs(self):
        await self.check_signal_cleanup(signal.SIGTERM, command_close_fails=True)

    async def test_console_close_failure_still_finalizes_existing_sandbox_jobs(self):
        await self.check_signal_cleanup(signal.SIGTERM, console_close_fails=True)

    async def test_second_cancellation_during_cleanup_is_not_suppressed(self):
        async def interrupted_cleanup(_projects):
            try:
                await asyncio.Event().wait()
            finally:
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        with (mock.patch.object(server, "serve", interrupted_cleanup),
              mock.patch.object(loop, "add_signal_handler") as register,
              mock.patch.object(loop, "remove_signal_handler"),
              mock.patch.object(server.signal, "signal")):
            task = asyncio.create_task(server.serve_with_shutdown({}))
            await asyncio.sleep(0)
            register.call_args.args[1]()
            with self.assertRaises(asyncio.CancelledError):
                await task
