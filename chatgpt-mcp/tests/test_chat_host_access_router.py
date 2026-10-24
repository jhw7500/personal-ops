"""Real MCP stdio protocol with injected runtime identity and temporary grant state.

This exercises tool/resource registration and metadata transport, not a live
ChatGPT client. No host command, model, service, or production grant is used.
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
CARD_URI = "ui://host-access/approval-v2.html"
CALLBACKS = {"update_host_access_approval", "confirm_host_access_approval",
             "cancel_host_access_approval"}
WRAPPER = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_access, server
host_access._installed_runtime_identity = lambda: 'a' * 64
projects = {name: Path(value) for name, value in json.loads(sys.argv[2]).items()}
asyncio.run(server.serve(projects))
"""


def text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class ChatHostAccessRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_hidden_capability_and_safe_callback_transport(self):
        with tempfile.TemporaryDirectory(prefix="chat-approval-protocol-") as folder:
            root = Path(folder)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            projects = {name: str(root / name) for name in ("alpha", "beta")}
            params = StdioServerParameters(command=sys.executable, args=[
                "-B", "-c", WRAPPER, str(MODULE_DIR), json.dumps(projects)],
                env={**os.environ, "XDG_STATE_HOME": str(root / "state"),
                     "PYTHONDONTWRITEBYTECODE": "1"})
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                            self.assertIn("open_host_access_approval", tools)
                            opener = tools["open_host_access_approval"]
                            self.assertEqual(
                                opener.inputSchema["properties"]["lifetime"],
                                {"type": "string", "enum": ["timed", "until_revoked"]},
                            )
                            self.assertEqual(opener.meta["ui"], {
                                "resourceUri": CARD_URI, "visibility": ["model"]})
                            self.assertFalse(opener.meta["openai/widgetAccessible"])
                            for name in CALLBACKS:
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                                self.assertFalse(tools[name].inputSchema["additionalProperties"])
                            for name in ("read_file", "start_host_execution", "start_agent_task"):
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["model"])
                                self.assertFalse(tools[name].meta["openai/widgetAccessible"])

                            resources = await client.list_resources()
                            self.assertIn(CARD_URI, {str(item.uri) for item in resources.resources})
                            resource = (await client.read_resource(CARD_URI)).contents[0]
                            self.assertEqual(resource.mimeType, "text/html;profile=mcp-app")
                            self.assertIn("<html", resource.text.lower())
                            self.assertEqual(resource.meta["ui"]["csp"], {
                                "connectDomains": [], "resourceDomains": []})

                            opened = await client.call_tool("open_host_access_approval", {
                                "project": "alpha", "duration_seconds": 2592000, "allow_codex": True})
                            self.assertFalse(opened.isError, text(opened))
                            self.assertEqual(opened.structuredContent["duration_seconds"], 2592000)
                            token = opened.meta["host_access_confirmation"]
                            self.assertNotIn(token, text(opened))
                            self.assertNotIn(token, json.dumps(opened.structuredContent))
                            args = {"project": "alpha", "card_id": opened.structuredContent["card_id"],
                                    "confirmation_token": token}
                            status = await client.call_tool("get_host_access", {"project": "alpha"})
                            self.assertFalse(status.structuredContent["active"])

                            # Malformed inputs must not reach SDK value-echoing validation.
                            secret = "INVALID-PRIVATE-CAPABILITY-DO-NOT-ECHO"
                            invalids = [
                                {**args, "confirmation_token": secret},
                                {**args, "confirmation_token": {"secret": secret}},
                                {**args, "card_id": secret},
                                {**args, secret: True},
                                {**args, "project": secret},
                                {**args, "allow_codex": True},
                            ]
                            for values in invalids:
                                result = await client.call_tool("confirm_host_access_approval", values)
                                self.assertTrue(result.isError)
                                self.assertNotIn(secret, result.model_dump_json())
                                self.assertNotIn(token, result.model_dump_json())

                            approved = await client.call_tool("confirm_host_access_approval", args)
                            self.assertFalse(approved.isError, text(approved))
                            self.assertEqual(approved.structuredContent["state"], "approved")
                            self.assertIsNone(approved.meta)
                            granted = await client.call_tool("get_host_access", {"project": "alpha"})
                            self.assertTrue(granted.structuredContent["active"])
                            self.assertTrue(granted.structuredContent["allow_codex"])
                            replay = await client.call_tool("confirm_host_access_approval", args)
                            self.assertTrue(replay.isError)
                            self.assertIn("HOST_ACCESS_CARD_SPENT", text(replay))
                            still = await client.call_tool("get_host_access", {"project": "alpha"})
                            self.assertEqual(still.structuredContent["grant_id"],
                                             granted.structuredContent["grant_id"])
                            other = await client.call_tool("get_host_access", {"project": "beta"})
                            self.assertFalse(other.structuredContent["active"])
                log.flush()
                for path in (root / "state").rglob("*"):
                    if path.is_file():
                        self.assertNotIn(token.encode(), path.read_bytes(), str(path))
                self.assertNotIn(token, (root / "server.log").read_text())


if __name__ == "__main__":
    unittest.main()
