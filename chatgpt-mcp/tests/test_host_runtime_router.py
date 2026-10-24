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
sys.path.insert(0, str(MODULE_DIR))
from project_state import ProjectState  # noqa: E402


HOST_TOOLS = {
    "get_runtime_status", "compare_deployment", "prepare_test_deploy",
    "execute_test_deploy", "get_host_operation", "get_db_migration_status", "prepare_db_migration",
    "get_predecessor_migration_status", "prepare_predecessor_migration",
    "execute_predecessor_migration", "execute_db_migration",
    "get_toss_test_console_status", "get_toss_ux019_test_console_status",
}


class HostRuntimeRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_tools_coexist_with_project_tools_and_record_safe_errors(self):
        with tempfile.TemporaryDirectory(prefix="mcp-host-router-") as folder:
            root = Path(folder)
            projects = {name: root / name for name in ("tossApp", "alpha")}
            for path in projects.values():
                path.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": {k: str(v) for k, v in projects.items()}}))
            state = root / "state"
            # This test remains offline after the owner's real Console profile
            # is enabled. Keep the real home for the installed Serena backend.
            entry = root / "server-entry.py"
            entry.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(MODULE_DIR)!r})\n"
                "import server\n"
                "server.SERENA_ENV = Path(sys.prefix)\n"
                "connection = server.toss_console_codex.CodexConsoleConnection\n"
                "server.toss_console_codex.CodexConsoleConnection = lambda projects, state: "
                f"connection(projects, state, config_path=Path({str(root / 'absent-console.json')!r}))\n"
                "server.main()\n")
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(entry), "--config", str(config)],
                env=dict(os.environ, XDG_STATE_HOME=str(state)),
            )
            with (root / "server.log").open("w") as log:
                async with asyncio.timeout(40):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            listed = await client.list_tools()
                            descriptors = {tool.name: tool for tool in listed.tools}
                            self.assertTrue(HOST_TOOLS <= descriptors.keys())
                            self.assertIn("read_file", descriptors)
                            self.assertIn("report_work_events", descriptors)
                            for name in HOST_TOOLS:
                                schema = descriptors[name].inputSchema
                                self.assertEqual(schema["properties"]["project"]["enum"], ["tossApp"])
                                self.assertEqual(schema["properties"]["target"]["enum"], ["tarororo-test"])
                                self.assertFalse(schema["additionalProperties"])
                            self.assertEqual(descriptors["get_job"].inputSchema["properties"]["project"]["enum"],
                                             ["tossApp", "alpha"])
                            self.assertTrue(descriptors["get_runtime_status"].annotations.readOnlyHint)
                            self.assertTrue(descriptors["get_toss_test_console_status"].annotations.readOnlyHint)
                            blocked_console = await client.call_tool("get_toss_test_console_status", {
                                "project": "tossApp", "target": "tarororo-test",
                            })
                            self.assertFalse(blocked_console.isError)
                            result = json.loads("".join(item.text for item in blocked_console.content
                                                        if item.type == "text"))
                            self.assertEqual(result["state"], "blocked")
                            self.assertEqual(result["code"], "CONSOLE_OAUTH_NOT_CONFIGURED")
                            self.assertFalse(result["provider_verified"])
                            self.assertFalse(result["test_bundle_ready"])
                            self.assertEqual(result["deployment_id"], "01a13e37-27f3-7f9c-923d-6dbd690a5a09")
                            self.assertTrue(descriptors["get_toss_ux019_test_console_status"].annotations.readOnlyHint)
                            ux019 = await client.call_tool("get_toss_ux019_test_console_status", {
                                "project": "tossApp", "target": "tarororo-test",
                            })
                            self.assertFalse(ux019.isError)
                            next_result = json.loads("".join(item.text for item in ux019.content
                                                             if item.type == "text"))
                            self.assertEqual(next_result["deployment_id"], "01a151bd-4015-75cf-8275-fa6b6aef9a47")
                            self.assertEqual(next_result["code"], "CONSOLE_OAUTH_NOT_CONFIGURED")
                            self.assertFalse(next_result["provider_verified"])
                            self.assertFalse(next_result["test_bundle_ready"])
                            self.assertTrue(descriptors["get_db_migration_status"].annotations.readOnlyHint)
                            self.assertTrue(descriptors["get_predecessor_migration_status"].annotations.readOnlyHint)
                            self.assertTrue(descriptors["execute_predecessor_migration"].annotations.destructiveHint)
                            self.assertTrue(descriptors["execute_db_migration"].annotations.destructiveHint)
                            self.assertTrue(descriptors["execute_test_deploy"].annotations.destructiveHint)
                            self.assertEqual(descriptors["prepare_test_deploy"].inputSchema["properties"]["component"]["enum"],
                                             ["api", "worker"])
                            self.assertEqual(descriptors["prepare_db_migration"].inputSchema["required"],
                                             ["project", "target", "request_id"])
                            self.assertEqual(descriptors["prepare_predecessor_migration"].inputSchema["required"],
                                             ["project", "target", "request_id"])
                            self.assertEqual(descriptors["execute_predecessor_migration"].inputSchema["required"],
                                             ["project", "target", "operation_id"])
                            rejected = await client.call_tool("get_runtime_status", {
                                "project": "alpha", "target": "tarororo-test",
                            })
                            self.assertTrue(rejected.isError)
                            missing = await client.call_tool("get_host_operation", {
                                "project": "tossApp", "target": "tarororo-test", "operation_id": "0" * 32,
                            })
                            self.assertTrue(missing.isError)
                            output = "\n".join(item.text for item in missing.content if item.type == "text")
                            self.assertNotIn(str(root), output)
                            record = await client.call_tool("report_work_events", {
                                "project": "alpha", "events": [{
                                    "schema_version": 1, "source_id": "claude-local",
                                    "source_kind": "claude-cli", "actor_id": "session-probe", "actor_kind": "agent",
                                    "actor_label": "Claude", "task_label": "probe",
                                    "task_id": "tool:probe", "task_kind": "call", "action": "read", "state": "returned",
                                    "model": "claude-model", "model_source": "event",
                                }],
                            })
                            self.assertFalse(record.isError)
            store = ProjectState(projects, state / "chatgpt-coding-mcp")
            activity = store.list_activity("tossApp")["events"]
            self.assertEqual({row["phase"] for row in activity if row["tool"] == "get_host_operation"},
                             {"started", "failed"})
            events = store.list_work_events("alpha")["records"]
            self.assertEqual(events[0]["source_kind"], "claude-cli")
            self.assertEqual(events[0]["model_source"], "event")
