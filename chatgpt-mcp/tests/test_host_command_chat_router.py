"""Actual MCP protocol for command approval, with disposable profiles and a fake runner."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MODULE_DIR = Path(__file__).resolve().parents[1]
CARD_URI = "ui://host-commands/approval-v1.html"
LAUNCHER = r'''
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_access, server
from host_command_operations import HostCommandOperations

root = Path(sys.argv[2])
host_access._installed_runtime_identity = lambda: "a" * 64
def fake_runner(frozen, cancel):
    marker = root / "executions"
    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else "1")
    return {"exit_code": 0, "duration_ms": 1, "stdout_bytes": 0,
            "stderr_bytes": 0, "termination_verified": True}
manager = HostCommandOperations({"example": root / "repo"},
    config_path=root / "profiles.json", state_dir=root / "command-state",
    runner=fake_runner, test_only=True)
server.host_command_tools.create_host_command_manager = lambda *args: manager
asyncio.run(server.serve_with_shutdown({"example": root / "repo"}))
'''


def text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class HostCommandChatRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_card_and_one_use_callback_then_explicit_execution(self):
        await self.exercise_command(direct=False)

    async def test_explicit_app_callback_starts_without_a_model_execution_call(self):
        await self.exercise_command(direct=True)

    async def exercise_command(self, *, direct):
        with tempfile.TemporaryDirectory(prefix="host-command-card-router-") as folder:
            root = Path(folder)
            (root / "repo").mkdir()
            artifact = root / "repo" / "bundle.ait"
            artifact.write_bytes(b"DISPOSABLE TEST BUNDLE")
            artifact.chmod(0o600)
            program = root / "upload"
            program.write_bytes(b"#!/bin/sh\nexit 0\n")
            program.chmod(0o700)
            config = root / "profiles.json"
            config.write_text(json.dumps({"version": 1, "enabled": True, "profiles": {
                "test-upload": {
                    "project": "example", "label": "Upload exact TEST bundle",
                    "executable": str(program),
                    "executable_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                    "argv": ["test"], "cwd": ".", "effect": "external_publish",
                    "timeout_seconds": 60,
                    "inputs": {"bundle.ait": hashlib.sha256(artifact.read_bytes()).hexdigest()},
                }}}))
            config.chmod(0o600)
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(launcher), str(MODULE_DIR), str(root)],
                env={**os.environ, "XDG_STATE_HOME": str(root / "state"),
                     "PYTHONDONTWRITEBYTECODE": "1"})
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(60):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {item.name: item for item in (await client.list_tools()).tools}
                            self.assertEqual(tools["prepare_host_command"].meta["ui"], {
                                "resourceUri": CARD_URI, "visibility": ["model"]})
                            self.assertIn("open_host_command_approval", tools)
                            for name in ("confirm_host_command_approval", "cancel_host_command_approval",
                                         "confirm_and_execute_host_command"):
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                            self.assertTrue(tools["confirm_and_execute_host_command"].annotations.destructiveHint)
                            self.assertTrue(tools["confirm_and_execute_host_command"].annotations.openWorldHint)
                            resource = (await client.read_resource(CARD_URI)).contents[0]
                            self.assertEqual(resource.mimeType, "text/html;profile=mcp-app")
                            self.assertEqual(resource.meta["ui"]["csp"], {
                                "connectDomains": [], "resourceDomains": []})
                            prepared = await client.call_tool("prepare_host_command", {
                                "project": "example", "profile_id": "test-upload", "request_id": "card"})
                            self.assertFalse(prepared.isError, text(prepared))
                            public = prepared.structuredContent
                            self.assertEqual(public["component"], "host_command")
                            self.assertFalse(public["executes_on_approval"])
                            self.assertTrue(public["supports_direct_execution"])
                            self.assertNotIn("approval_url", public["plan"])
                            self.assertNotIn(str(root), text(prepared))
                            token = prepared.meta["host_command_confirmation"]
                            self.assertNotIn(token, text(prepared))
                            self.assertFalse((root / "executions").exists())
                            operation = {"project": "example", "operation_id": public["operation_id"]}
                            callback = "confirm_and_execute_host_command" if direct else "confirm_host_command_approval"
                            malformed = await client.call_tool(callback, {
                                **operation, "card_id": public["card_id"],
                                "confirmation_token": token, "leak_probe": token})
                            self.assertTrue(malformed.isError)
                            self.assertEqual(text(malformed), "INVALID_HOST_COMMAND_CARD_ARGUMENTS")
                            self.assertNotIn(token, text(malformed))
                            confirmed = await client.call_tool(callback, {
                                **operation, "card_id": public["card_id"], "confirmation_token": token})
                            self.assertFalse(confirmed.isError, text(confirmed))
                            self.assertTrue(confirmed.structuredContent["receipt_verified"])
                            self.assertEqual(confirmed.structuredContent["next_action"], {
                                "tool": "get_host_command" if direct else "execute_host_command",
                                "arguments": operation})
                            if direct:
                                self.assertTrue(confirmed.structuredContent["execution_requested"])
                                self.assertTrue(confirmed.structuredContent["execution_started"])
                                self.assertNotIn("approval_url", confirmed.structuredContent["plan"])
                                replay = await client.call_tool(callback, {
                                    **operation, "card_id": public["card_id"], "confirmation_token": token})
                                self.assertTrue(replay.isError)
                            else:
                                self.assertFalse((root / "executions").exists())
                                reopened = await client.call_tool("open_host_command_approval", operation)
                                self.assertFalse(reopened.isError, text(reopened))
                                self.assertTrue(reopened.structuredContent["receipt_verified"])
                                self.assertNotIn("host_command_confirmation", reopened.meta or {})
                                self.assertNotIn("approval_url", reopened.structuredContent["plan"])
                                executed = await client.call_tool("execute_host_command", operation)
                                self.assertFalse(executed.isError, text(executed))
                            for _ in range(100):
                                current = await client.call_tool("get_host_command", operation)
                                if current.structuredContent["state"] == "succeeded":
                                    break
                                await asyncio.sleep(0.02)
                            self.assertEqual(current.structuredContent["state"], "succeeded")
                            self.assertEqual((root / "executions").read_text(), "1")
                            self.assertNotIn(token, (root / "server.log").read_text())


if __name__ == "__main__":
    unittest.main()
