"""Exact MCP host-command tool contract and fail-closed routing; no real host CLI."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_command_tools import HOST_COMMAND_TOOL_SPECS, HostCommandTools  # noqa: E402
from host_command_operations import HostCommandOperations  # noqa: E402


class FakeCommand:
    def __init__(self):
        self.count = 0

    def __call__(self, frozen, cancel):
        self.count += 1
        return {"exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 3, "duration_ms": 1,
                "termination_verified": True}


class HostCommandToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="command-tools-test-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        package = self.repo / "build"
        package.mkdir()
        archive = package / "tarororo.ait"
        archive.write_bytes(b"test fixture")
        archive.chmod(0o600)
        program = self.base / "run"
        program.write_bytes(b"#!/bin/sh\nexit 0\n")
        program.chmod(0o700)
        config = self.base / "host-commands.json"
        config.write_text(json.dumps({"version": 1, "enabled": True, "profiles": {
            "toss-test": {
                "project": "tossApp", "label": "TEST deployment",
                "executable": str(program),
                "executable_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                "argv": ["test"], "cwd": ".", "effect": "external_publish",
                "timeout_seconds": 60, "inputs": {
                    "build/tarororo.ait": hashlib.sha256(archive.read_bytes()).hexdigest()
                },
            }}}))
        config.chmod(0o600)
        self.fake = FakeCommand()
        manager = HostCommandOperations({"tossApp": self.repo}, config_path=config,
                                        state_dir=self.base / "state",
                                        runner=self.fake, test_only=True)
        self.tools = HostCommandTools({"tossApp": self.repo}, self.base / "state",
                                      {"tossApp": asyncio.Lock()})
        self.tools._manager = manager

    def test_mcp_tools_require_exact_arguments_and_no_raw_command_fields(self):
        names = {v["name"] for v in HOST_COMMAND_TOOL_SPECS}
        self.assertEqual(names, {
            "list_host_command_profiles", "prepare_host_command",
            "execute_host_command", "get_host_command", "cancel_host_command",
        })
        for tool in HOST_COMMAND_TOOL_SPECS:
            schema = tool["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertNotIn("argv", schema["properties"])
            self.assertNotIn("cwd", schema["properties"])
            self.assertNotIn("shell", schema["properties"])
            self.assertNotIn("approved", schema["properties"])

    async def test_readonly_catalog_is_private_and_requires_project(self):
        got = await self.tools.call("list_host_command_profiles", {"project": "tossApp"})
        self.assertEqual(got["profiles"][0]["profile_id"], "toss-test")
        self.assertNotIn(str(self.base), str(got))
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SCOPE"):
            await self.tools.call("list_host_command_profiles", {"project": "other"})
        with self.assertRaisesRegex(ValueError, "INVALID_HOST_COMMAND_ARGUMENTS"):
            await self.tools.call("list_host_command_profiles", {"project": "tossApp", "approved": True})

    async def test_explicit_web_approval_before_execution(self):
        plan = await self.tools.call("prepare_host_command", {
            "project": "tossApp", "profile_id": "toss-test", "request_id": "once"
        })
        self.assertEqual(plan["state"], "pending_host_approval")
        with self.assertRaisesRegex(ValueError, "HOST_COMMAND_APPROVAL_REQUIRED"):
            await self.tools.call("execute_host_command", {
                "project": "tossApp", "operation_id": plan["operation_id"]})
        self.assertEqual(self.fake.count, 0)
        self.tools.manager().approve_web(
            "tossApp", operation_id=plan["operation_id"],
            plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
            confirmation=plan["operation_id"],
        )
        state = await self.tools.call("execute_host_command", {
            "project": "tossApp", "operation_id": plan["operation_id"]})
        self.assertIn(state["state"], ("queued", "running", "succeeded"))
        for _ in range(50):
            status = await self.tools.call("get_host_command", {
                "project": "tossApp", "operation_id": plan["operation_id"]})
            if status["state"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(self.fake.count, 1)
        await self.tools.call("execute_host_command", {
            "project": "tossApp", "operation_id": plan["operation_id"]})
        self.assertEqual(self.fake.count, 1)

    async def test_no_effect_from_unknown_profile_or_untrusted_extra_args(self):
        with self.assertRaisesRegex(ValueError, "HOST_COMMAND_PROFILE_NOT_FOUND"):
            await self.tools.call("prepare_host_command", {
                "project": "tossApp", "profile_id": "arbitrary", "request_id": "fail"})
        with self.assertRaisesRegex(ValueError, "INVALID_HOST_COMMAND_ARGUMENTS"):
            await self.tools.call("prepare_host_command", {
                "project": "tossApp", "profile_id": "toss-test",
                "request_id": "fail", "argv": ["bash", "-c", "rm -rf /"]})
        self.assertEqual(self.fake.count, 0)


if __name__ == "__main__":
    unittest.main()
