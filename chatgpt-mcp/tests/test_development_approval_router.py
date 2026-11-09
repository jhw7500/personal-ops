"""Real MCP transport coverage for all new chat approval entry points.

Uses disposable projects/state; never calls a host issuer or starts an operation.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MODULE_DIR = Path(__file__).resolve().parents[1]
WRAPPER = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_access, server
host_access._installed_runtime_identity = lambda: 'a' * 64
projects = {name: Path(value) for name, value in json.loads(sys.argv[2]).items()}
asyncio.run(server.serve_with_shutdown(projects))
"""
GIT_CALLBACKS = (
    "confirm_git_operation", "confirm_and_execute_git_operation",
    "cancel_git_operation_approval",
)
OPERATION_CALLBACKS = (
    "confirm_operation_approval", "confirm_and_execute_operation",
    "cancel_operation_approval",
)


class DevelopmentApprovalRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_cards_are_registered_and_private_validation_never_echoes_capabilities(self):
        with tempfile.TemporaryDirectory(prefix="development-approval-router-") as folder:
            root = Path(folder)
            projects = {}
            for name in ("alpha", "personal-ops", "tossApp"):
                project = root / name
                project.mkdir()
                projects[name] = str(project)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-c", WRAPPER, str(MODULE_DIR), json.dumps(projects)],
                env={**os.environ, "XDG_STATE_HOME": str(root / "state"),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {item.name: item for item in (await client.list_tools()).tools}
                            for name in ("open_git_operation_approval", "open_operation_approval"):
                                self.assertTrue(name in tools, name)
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["model"])
                                self.assertFalse(tools[name].meta["openai/widgetAccessible"])
                            for name in (*GIT_CALLBACKS, *OPERATION_CALLBACKS):
                                self.assertTrue(name in tools, name)
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                                self.assertFalse(tools[name].inputSchema["additionalProperties"])

                            host_fields = tools["open_host_access_approval"].inputSchema["properties"]
                            self.assertEqual(host_fields["allow_git"]["type"], "boolean")
                            resources = {str(item.uri) for item in (await client.list_resources()).resources}
                            for opener in ("open_git_operation_approval", "open_operation_approval"):
                                uri = tools[opener].meta["ui"]["resourceUri"]
                                self.assertIn(uri, resources)
                                resource = (await client.read_resource(uri)).contents[0]
                                self.assertEqual(resource.mimeType, "text/html;profile=mcp-app")
                                self.assertIn("<html", resource.text.lower())
                                self.assertEqual(resource.meta["ui"]["csp"], {
                                    "connectDomains": [], "resourceDomains": [],
                                })

                            secret = "INVALID-PRIVATE-CAPABILITY-MUST-NOT-ECHO"
                            common = {"project": "alpha", "operation_id": "a" * 32,
                                      "card_id": "b" * 32, "confirmation_token": "c" * 64}
                            for name in (*GIT_CALLBACKS, *OPERATION_CALLBACKS):
                                arguments = dict(common)
                                if name in OPERATION_CALLBACKS:
                                    arguments["family"] = "agent"
                                for bad in (
                                    {**arguments, "confirmation_token": {"private": secret}},
                                    {**arguments, secret: True},
                                    {**arguments, "project": secret},
                                    {**arguments, "card_id": secret},
                                ):
                                    result = await client.call_tool(name, bad)
                                    self.assertTrue(result.isError)
                                    self.assertNotIn(secret, result.model_dump_json())
                log.flush()
                self.assertNotIn(secret, (root / "server.log").read_text())


if __name__ == "__main__":
    unittest.main()
