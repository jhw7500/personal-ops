"""Real MCP protocol coverage for the Schema 014 approval card registration."""
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
CARD_URI = "ui://host-operations/schema014-approval-v1.html"
LAUNCHER = r'''
import asyncio, hashlib, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_access, host_runtime_operations, server
from host_runtime_config import private_dir

host_access._installed_runtime_identity = lambda: "a" * 64

original_schema_call = server.host_operation_ui.Schema014ApprovalTools.call
async def observed_schema_call(self, name, arguments):
    if name == "confirm_schema014_approval":
        Path(sys.argv[3]).write_text("confirm-handler-entered")
    return await original_schema_call(self, name, arguments)
server.host_operation_ui.Schema014ApprovalTools.call = observed_schema_call

class Runtime:
    def guard(self, project, target):
        if project != "tossApp" or target != "tarororo-test": raise ValueError("scope")
        return Path(sys.argv[2])

class Operations:
    def __init__(self, projects, state_dir=None, **kwargs):
        self.state_dir = private_dir(Path(state_dir))
        self.operations_dir = private_dir(self.state_dir / "operations")
        self.lock_path = self.state_dir / "operations.lock"
        self.runtime = Runtime()
        self.states = {}
    def _require_trusted_runtime(self): pass
    def _path(self, operation_id): return self.operations_dir / (operation_id + ".json")
    def _verify(self, immutable): return Path(sys.argv[2])
    def _load(self, project, target, operation_id): return self.states[operation_id]
    def _result(self, state):
        immutable = state["immutable"]
        return {"project": state["project"], "target": state["target"],
          "operation_id": state["operation_id"], "component": immutable["component"],
          "state": state["state"], "plan_digest": state["plan_digest"],
          "source_digest": immutable["source_digest"],
          "migration_checksums": immutable["migration_checksums"],
          "approval_url": "https://example.invalid/legacy-approval",
          "approval_command": "not-executed-fixture",
          "impact": {"schema_change": "014_person_relationship_detail.sql"}}
    def prepare(self, project, target, component, mode, request_id):
        operation_id = "1" * 32
        immutable = {"project": project, "target": target, "operation_id": operation_id,
          "component": "schema014", "authorization": {"mode": "per_operation"},
          "source_review": {"policy": "reviewed_snapshot"}, "source_digest": "2" * 64,
          "migration_checksum": "3" * 64, "migration_checksums": {"001.sql": "4" * 64},
          "database_generation": {"database": "tarororo_test"},
          "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        state = {"state": "pending_host_approval", "immutable": immutable,
          "plan_digest": hashlib.sha256(b"fixture-plan").hexdigest(),
          "operation_id": operation_id, "project": project, "target": target}
        self.states[operation_id] = state
        return {"project": project, "target": target, "operation_id": operation_id,
          "component": component, "state": "pending_host_approval",
          "plan_digest": state["plan_digest"], "source_digest": immutable["source_digest"],
          "migration_checksums": immutable["migration_checksums"],
          "impact": {"schema_change": "014_person_relationship_detail.sql"}}

server.HostRuntimeOperations = Operations
asyncio.run(server.serve_with_shutdown({"tossApp": Path(sys.argv[2])}))
'''


def text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class Schema014ChatRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_and_open_use_exact_card_with_app_only_callbacks(self):
        with tempfile.TemporaryDirectory(prefix="schema014-chat-router-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            confirm_marker = root / "confirm-called"
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(launcher), str(MODULE_DIR), str(project),
                      str(confirm_marker)],
                env={**os.environ, "XDG_STATE_HOME": str(root / "state"),
                     "PYTHONDONTWRITEBYTECODE": "1"})
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(60):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {item.name: item for item in (await client.list_tools()).tools}
                            self.assertEqual(tools["prepare_schema014_migration"].meta["ui"], {
                                "resourceUri": CARD_URI, "visibility": ["model"]})
                            self.assertIn("open_schema014_approval", tools)
                            for name in ("confirm_schema014_approval", "cancel_schema014_approval"):
                                self.assertEqual(tools[name].meta["ui"]["visibility"], ["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                            resources = await client.list_resources()
                            self.assertIn(CARD_URI, {str(item.uri) for item in resources.resources})
                            resource = (await client.read_resource(CARD_URI)).contents[0]
                            self.assertEqual(resource.mimeType, "text/html;profile=mcp-app")
                            self.assertEqual(resource.meta["ui"]["csp"], {
                                "connectDomains": [], "resourceDomains": []})

                            scope = {"project": "tossApp", "target": "tarororo-test"}
                            prepared = await client.call_tool("prepare_schema014_migration", {
                                **scope, "request_id": "router-card"})
                            log.flush()
                            self.assertFalse(prepared.isError,
                                             text(prepared) + "\n" +
                                             (root / "server.log").read_text())
                            self.assertEqual(prepared.structuredContent["component"], "schema014")
                            for key in ("approval_url", "approval_command"):
                                self.assertNotIn(key, prepared.structuredContent["plan"])
                                self.assertNotIn('"' + key + '"', text(prepared))
                            token = prepared.meta["schema014_confirmation"]
                            self.assertNotIn(token, text(prepared))
                            operation_id = prepared.structuredContent["operation_id"]
                            recognizable = "feedface" * 8
                            malformed = await client.call_tool("confirm_schema014_approval", {
                                **scope, "operation_id": operation_id,
                                "card_id": "5" * 32,
                                "confirmation_token": recognizable,
                                "leak_probe": recognizable,
                            })
                            self.assertTrue(malformed.isError)
                            self.assertEqual(text(malformed),
                                             "INVALID_SCHEMA014_CARD_ARGUMENTS")
                            self.assertNotIn(recognizable, text(malformed))
                            log.flush()
                            self.assertNotIn(recognizable,
                                             (root / "server.log").read_text())
                            self.assertFalse(confirm_marker.exists())
                            receipt = (root / "state/chatgpt-coding-mcp/host-runtime/operations" /
                                       (operation_id + ".approval.json"))
                            self.assertFalse(receipt.exists())
                            reopened = await client.call_tool("open_schema014_approval", {
                                **scope, "operation_id": operation_id})
                            self.assertFalse(reopened.isError, text(reopened))
                            self.assertEqual(reopened.structuredContent["operation_id"], operation_id)
                            self.assertNotEqual(reopened.meta["schema014_confirmation"], token)
                            for key in ("approval_url", "approval_command"):
                                self.assertNotIn(key, reopened.structuredContent["plan"])
                                self.assertNotIn('"' + key + '"', text(reopened))
                log.flush()
                self.assertNotIn(recognizable, (root / "server.log").read_text())
                events = []
                for line in (root / "server.log").read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("event") == "resources/read":
                        events.append(event)
                self.assertTrue(any(event.get("known_uri") is True
                                    and event.get("ui_resource") == CARD_URI
                                    for event in events))


if __name__ == "__main__":
    unittest.main()
