"""MCP protocol and app-only registration for TEST deployment approval cards.

Synthetic host manager: does not execute, grant authority, touch Docker or SQL.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODULE_DIR = Path(__file__).resolve().parents[1]
UI_URI = "ui://host-operations/test-plan-approval-v2.html"
LEGACY_UI_URI = "ui://host-operations/test-plan-approval-v1.html"
LAUNCHER = r'''
import asyncio, hashlib, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_access, server
from host_runtime_config import private_dir
from host_runtime_config import HostRuntimeError
import host_schema015016

host_schema015016.schema015016_status = lambda runtime, project, target: {
    "project": project, "target": target, "state": "fixture_read_only"}

host_access._installed_runtime_identity = lambda: "a" * 64

class FakeRuntime:
    def guard(self, project, target):
        if project != "tossApp" or target != "tarororo-test":
            raise ValueError("scope")
        return Path(sys.argv[2])

class FakeOperations:
    def __init__(self, projects, state_dir=None, **kwargs):
        self.state_dir = private_dir(Path(state_dir))
        self.operations_dir = private_dir(self.state_dir / "operations")
        self.lock_path = self.state_dir / "operations.lock"
        self.runtime = FakeRuntime()
        self.states = {}
    def _require_trusted_runtime(self): pass
    def _verify(self, immutable): return Path(sys.argv[2])
    def _load(self, project, target, operation_id): return self.states[operation_id]
    async def execute(self, project, target, operation_id, *, expected_component):
        if expected_component != {"schema015016"}:
            raise ValueError("unexpected execution component")
        raise HostRuntimeError("FIXTURE_EXECUTION_NOT_AUTHORIZED")
    def _result(self, state):
        imm=state["immutable"]
        return {
          "project": state["project"], "target": state["target"],
          "operation_id": state["operation_id"], "component": imm["component"],
          "mode": imm["mode"], "state": state["state"],
          "plan_digest": state["plan_digest"], "source_digest": imm["source_digest"],
          "approval_required": True, "authorization": imm["authorization"],
          "impact": {"recreates": [imm["component"]], "preserves": ["db", "api"],
                     "schema_change": None},
          "approval_url": "https://example.invalid/admin",
          "approval_command": "not-executed-fixture",
        }
    def prepare(self, project, target, component, mode, request_id):
        operation_id = "1" * 32
        immutable = {
            "project": project, "target": target, "operation_id": operation_id,
            "component": component, "mode": mode,
            "source_digest": "2" * 64,
            "authorization": {"mode": "per_operation"},
            "source_review": {"policy": "reviewed_snapshot"},
            "changed_paths": ["server/src/ai/prompt.ts"],
            "high_risk_changes": ["deploy/pc-test/compose.yaml"],
            "nonce": "3" * 64,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
        state = {
            "project": project, "target": target, "operation_id": operation_id,
            "immutable": immutable, "state": "pending_host_approval",
            "plan_digest": hashlib.sha256(b"fixed-plan").hexdigest(),
        }
        self.states[operation_id] = state
        return self._result(state)

server.HostRuntimeOperations = FakeOperations
asyncio.run(server.serve_with_shutdown({"tossApp": Path(sys.argv[2])}))
'''


def response_text(response):
    return "\n".join(x.text for x in response.content if x.type == "text")


class TestPlanChatRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_pending_plan_opens_chat_card_and_rejects_public_callback(self):
        with tempfile.TemporaryDirectory(prefix="test-plan-chat-router-") as folder:
            root = Path(folder)
            project = root / "repo"
            project.mkdir()
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", str(launcher), str(MODULE_DIR), str(project)],
                env=dict(os.environ, XDG_STATE_HOME=str(root / "state"),
                         PYTHONDONTWRITEBYTECODE="1"),
            )
            with (root / "stderr.log").open("w+") as log:
                async with asyncio.timeout(70):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            initialized = await client.initialize()
                            self.assertIn("Schema 015/016", initialized.instructions)
                            tools = {x.name: x for x in (await client.list_tools()).tools}
                            self.assertEqual(
                                tools["prepare_test_deploy"].meta["ui"],
                                {"resourceUri": UI_URI, "visibility": ["model"]})
                            for name in ("confirm_test_plan_approval", "cancel_test_plan_approval"):
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                            self.assertEqual(tools["open_test_plan_approval"].meta["ui"]["resourceUri"],
                                             UI_URI)
                            resources = await client.list_resources()
                            resource_uris = {str(r.uri) for r in resources.resources}
                            self.assertIn(UI_URI, resource_uris)
                            self.assertIn(LEGACY_UI_URI, resource_uris)
                            ui = (await client.read_resource(UI_URI)).contents[0]
                            legacy_ui = (await client.read_resource(LEGACY_UI_URI)).contents[0]
                            self.assertEqual(legacy_ui.text, ui.text)
                            self.assertIn("미실행 승인 철회", ui.text)
                            self.assertEqual(ui.mimeType, "text/html;profile=mcp-app")
                            self.assertEqual(ui.meta["ui"]["csp"], {
                                "connectDomains": [], "resourceDomains": []})
                            prepared = await client.call_tool("prepare_test_deploy", {
                                "project": "tossApp", "target": "tarororo-test",
                                "component": "worker", "mode": "disabled",
                                "request_id": "test-plan-chat-router"})
                            log.flush()
                            self.assertFalse(prepared.isError,
                                response_text(prepared) + "\n" + (root / "stderr.log").read_text())
                            self.assertEqual(prepared.structuredContent["component"], "worker")
                            self.assertEqual(prepared.structuredContent["state"], "pending")
                            secret = prepared.meta["test_plan_confirmation"]
                            self.assertRegex(secret, r"^[0-9a-f]{64}$")
                            self.assertNotIn(secret, response_text(prepared))
                            self.assertNotIn("approval_url", response_text(prepared))
                            self.assertNotIn("approval_command", response_text(prepared))
                            identity = prepared.structuredContent["operation_id"]
                            malformed = await client.call_tool("confirm_test_plan_approval", {
                                "project": "tossApp", "target": "tarororo-test",
                                "operation_id": identity, "card_id": "5"*32,
                                "confirmation_token": "6"*64, "injected": "7"*64})
                            self.assertTrue(malformed.isError)
                            self.assertEqual(response_text(malformed),
                                             "INVALID_TEST_PLAN_CARD_ARGUMENTS")
                            self.assertNotIn("7"*64, response_text(malformed))
                            mismatch = await client.call_tool("confirm_test_plan_approval", {
                                "project": "tossApp", "target": "tarororo-test",
                                "operation_id": identity, "card_id": "5"*32,
                                "confirmation_token": "6"*64})
                            self.assertTrue(mismatch.isError)
                            self.assertIn("TEST_PLAN_CARD_DENIED", response_text(mismatch))
                            reopened = await client.call_tool("open_test_plan_approval", {
                                "project": "tossApp", "target": "tarororo-test",
                                "operation_id": identity})
                            self.assertFalse(reopened.isError, response_text(reopened))
                            self.assertNotEqual(secret, reopened.meta["test_plan_confirmation"])
                            receipt = root / "state/chatgpt-coding-mcp/host-runtime/operations" / (
                                identity + ".approval.json")
                            self.assertFalse(receipt.exists())

                            schema_tools = {
                                "get_schema015016_migration_status",
                                "prepare_schema015016_migration",
                                "execute_schema015016_migration",
                            }
                            self.assertTrue(schema_tools <= tools.keys())
                            self.assertEqual(
                                tools["prepare_schema015016_migration"].meta["ui"],
                                {"resourceUri": UI_URI, "visibility": ["model"]})
                            scope = {"project": "tossApp", "target": "tarororo-test"}
                            observed = await client.call_tool(
                                "get_schema015016_migration_status", scope)
                            self.assertFalse(observed.isError, response_text(observed))
                            self.assertEqual(observed.structuredContent["state"],
                                             "fixture_read_only")
                            schema_plan = await client.call_tool(
                                "prepare_schema015016_migration",
                                dict(scope, request_id="schema015016-router"))
                            self.assertFalse(schema_plan.isError, response_text(schema_plan))
                            self.assertEqual(schema_plan.structuredContent["component"],
                                             "schema015016")
                            self.assertEqual(schema_plan.structuredContent["state"], "pending")
                            execution = await client.call_tool(
                                "execute_schema015016_migration",
                                dict(scope, operation_id=schema_plan.structuredContent["operation_id"]))
                            self.assertTrue(execution.isError)
                            self.assertEqual(response_text(execution),
                                             "FIXTURE_EXECUTION_NOT_AUTHORIZED")
                            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
