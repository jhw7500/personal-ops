"""Real MCP stdio routing of web runtime tools, with a fake no-effect manager."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parents[1]
FAKE_ID = "a" * 32
LAUNCHER = r"""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import server
from host_runtime_config import HostRuntimeError

def fail_if_requested(value):
    if value in {'unexpected', 'b' * 32}:
        raise OSError('/private/runtime-test/internal-path: fake-secret-marker')
    if value in {'known', 'c' * 32}:
        raise HostRuntimeError('ROLLOUT_SOURCE_CHANGED')

class FakeRuntimeRollout:
    def __init__(self, *args, **kwargs):
        pass

    def prepare(self, project, request_id):
        fail_if_requested(request_id)
        assert project == 'personal-ops' and request_id == 'test-web'
        return {'project': project, 'state': 'pending_host_approval',
                'operation_id': 'a'*32, 'approval_required': True}

    def execute(self, project, operation_id):
        fail_if_requested(operation_id)
        assert project == 'personal-ops' and operation_id == 'a'*32
        return {'state': 'queued', 'operation_id': operation_id,
                'project': project, 'executed': False}

    def get(self, project, operation_id):
        fail_if_requested(operation_id)
        return {'state': 'pending_host_approval', 'operation_id': operation_id,
                'project': project}

server.RuntimeRolloutWeb = FakeRuntimeRollout
asyncio.run(server.serve_with_shutdown({'personal-ops': Path(sys.argv[2])}))
"""


class RuntimeRolloutRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_errors_hide_private_details_and_preserve_known_codes(self):
        with tempfile.TemporaryDirectory(prefix="runtime-web-router-errors-") as folder:
            root = Path(folder)
            project = root / "personal-ops"
            project.mkdir()
            launch = root / "launcher.py"
            launch.write_text(LAUNCHER)
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(launch), str(HERE), str(project)],
                env=dict(os.environ, XDG_STATE_HOME=str(root / "state"),
                         PYTHONDONTWRITEBYTECODE="1"),
            )
            with (root / "stderr.log").open("w") as log:
                async with asyncio.timeout(70):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            for name in ("prepare_runtime_rollout", "get_runtime_rollout",
                                         "execute_runtime_rollout"):
                                for known in (False, True):
                                    with self.subTest(tool=name, known=known):
                                        field = ("request_id" if name == "prepare_runtime_rollout"
                                                 else "operation_id")
                                        value = (("known" if known else "unexpected")
                                                 if field == "request_id"
                                                 else ("c" if known else "b") * 32)
                                        result = await client.call_tool(name, {
                                            "project": "personal-ops", field: value})
                                        self.assertTrue(result.isError)
                                        message = " ".join(item.text for item in result.content
                                                           if item.type == "text")
                                        self.assertIn("ROLLOUT_SOURCE_CHANGED" if known
                                                      else "RUNTIME_ROLLOUT_FAILED", message)
                                        self.assertNotIn("/private/", message)
                                        self.assertNotIn("fake-secret-marker", message)

    async def test_exact_project_and_separate_prepare_execute_routes(self):
        with tempfile.TemporaryDirectory(prefix="runtime-web-router-") as folder:
            root = Path(folder)
            project = root / "personal-ops"
            project.mkdir()
            (project / "example.py").write_text("value=1\n")
            launch = root / "launcher.py"
            launch.write_text(LAUNCHER)
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(launch), str(HERE), str(project)],
                env=dict(os.environ, XDG_STATE_HOME=str(root / "state"),
                         PYTHONDONTWRITEBYTECODE="1"),
            )
            with (root / "stderr.log").open("w") as log:
                async with asyncio.timeout(70):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                            names = {"prepare_runtime_rollout", "get_runtime_rollout",
                                     "execute_runtime_rollout"}
                            self.assertTrue(names <= set(tools))
                            for name in names:
                                schema = tools[name].inputSchema
                                self.assertEqual(schema["properties"]["project"]["enum"],
                                                 ["personal-ops"])
                                self.assertFalse(schema["additionalProperties"])
                                self.assertNotIn("argv", schema["properties"])
                                self.assertNotIn("sql", schema["properties"])
                            self.assertTrue(tools["get_runtime_rollout"].annotations.readOnlyHint)
                            self.assertFalse(tools["execute_runtime_rollout"].annotations.readOnlyHint)

                            prepared = await client.call_tool("prepare_runtime_rollout", {
                                "project": "personal-ops", "request_id": "test-web"})
                            self.assertFalse(prepared.isError, prepared.content)
                            self.assertEqual(prepared.structuredContent["state"],
                                             "pending_host_approval")
                            read = await client.call_tool("get_runtime_rollout", {
                                "project": "personal-ops", "operation_id": FAKE_ID})
                            self.assertFalse(read.isError, read.content)
                            self.assertEqual(read.structuredContent["operation_id"], FAKE_ID)
                            queued = await client.call_tool("execute_runtime_rollout", {
                                "project": "personal-ops", "operation_id": FAKE_ID})
                            self.assertFalse(queued.isError, queued.content)
                            self.assertEqual(queued.structuredContent["state"], "queued")
                            refused = await client.call_tool("execute_runtime_rollout", {
                                "project": "tossApp", "operation_id": FAKE_ID})
                            self.assertTrue(refused.isError)


if __name__ == "__main__":
    unittest.main()
