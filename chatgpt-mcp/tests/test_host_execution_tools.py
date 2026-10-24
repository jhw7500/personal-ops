from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_execution_tools import HOST_EXECUTION_TOOL_SPECS, HostExecutionTools  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_access_ui import HostAccessApprovals  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class FakeAccess:
    def __init__(self, *, request_result=None, status_result=None, error=None):
        self.request_result = request_result or {"project": "demo", "state": "requested"}
        self.status_result = status_result or {
            "project": "demo", "active": True, "reason": None,
            "grant": {"expires_at": "2099-01-01T00:00:00+00:00"},
        }
        self.error = error
        self.request_calls = 0
        self.get_calls = 0

    def request(self, project):
        self.request_calls += 1
        if self.error is not None:
            raise self.error
        return {**self.request_result, "project": project}

    def get(self, project):
        self.get_calls += 1
        if self.error is not None:
            raise self.error
        return {**self.status_result, "project": project}


class FakeManager:
    def __init__(self):
        self.prepared = None

    def prepare(self, project, **values):
        self.prepared = (project, values)
        return {"project": project, "operation_id": "a" * 32, "state": "prepared"}

    async def execute(self, project, operation_id):
        return {"project": project, "operation_id": operation_id, "state": "queued"}

    def get(self, project, operation_id):
        return {"project": project, "operation_id": operation_id, "state": "succeeded"}

    def read_log(self, project, operation_id, **values):
        return {"project": project, "operation_id": operation_id, **values, "text": ""}

    def cancel(self, project, operation_id):
        return {"project": project, "operation_id": operation_id, "state": "cancelled"}

    async def close(self):
        pass


class HostExecutionToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.tools = HostExecutionTools(
            {"demo": Path("/tmp/demo")}, Path("/tmp/state"), {"demo": asyncio.Lock()},
            manager=self.manager, access=FakeAccess())

    def test_exact_schemas_expose_no_grant_issuance(self):
        self.assertEqual({item["name"] for item in HOST_EXECUTION_TOOL_SPECS}, {
            "request_host_access", "get_host_access", "start_host_execution",
            "get_host_execution", "read_host_execution_log", "cancel_host_execution",
        })
        for item in HOST_EXECUTION_TOOL_SPECS:
            schema = item["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertNotIn("approved", schema["properties"])
            self.assertNotIn("grant_id", schema["properties"])
        start = next(item for item in HOST_EXECUTION_TOOL_SPECS
                     if item["name"] == "start_host_execution")["inputSchema"]
        self.assertNotIn("task_label", start["required"])
        task_label = start["properties"]["task_label"]
        self.assertEqual({key: task_label[key] for key in (
            "type", "minLength", "maxLength",
        )}, {"type": "string", "minLength": 1, "maxLength": 160})
        self.assertIsInstance(task_label.get("description"), str)
        self.assertTrue(task_label["description"])

    async def test_start_prepares_and_executes_with_strict_arguments(self):
        result = await self.tools.call("start_host_execution", {
            "project": "demo", "executable": "/usr/bin/id", "argv": [], "cwd": ".",
            "request_id": "one", "input_text": "hello", "timeout_seconds": 20,
            "task_label": "Inspect host identity",
        })
        self.assertEqual(result["state"], "queued")
        self.assertEqual(self.manager.prepared[1]["input_text"], "hello")
        self.assertEqual(self.manager.prepared[1]["task_label"], "Inspect host identity")
        with self.assertRaisesRegex(ValueError, "INVALID_HOST_EXECUTION_ARGUMENTS"):
            await self.tools.call("start_host_execution", {
                "project": "demo", "executable": "/usr/bin/id", "argv": [], "cwd": ".",
                "request_id": "two", "approved": True,
            })

    async def test_legacy_request_does_not_advertise_a_second_request(self):
        result = await self.tools.call("request_host_access", {"project": "demo"})

        self.assertEqual(result, {"project": "demo", "state": "requested"})
        self.assertEqual(self.tools.access().request_calls, 1)
        self.assertNotIn("approval_card", result)
        self.assertNotIn("approval_url", result)

    async def test_advertised_card_flow_has_one_request_and_leaves_none(self):
        for decision in ("confirm", "cancel"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                projects = {"demo": project}
                access = HostAccess(projects, root / "state", trusted_runtime=True)
                tools = HostExecutionTools(
                    projects, root / "state", {"demo": asyncio.Lock()},
                    manager=self.manager, access=access)
                status = await tools.call("get_host_access", {"project": "demo"})
                self.assertEqual(access.get("demo")["requests"], [])
                self.assertEqual(status["approval_card"]["tool"], "open_host_access_approval")
                approvals = HostAccessApprovals(access)
                result = approvals.open(**status["approval_card"]["arguments"])
                self.assertEqual(len(access.get("demo")["requests"]), 1)
                card = result["structuredContent"]
                getattr(approvals, decision)(
                    "demo", card_id=card["card_id"],
                    confirmation_token=result["_meta"]["host_access_confirmation"])
                final = await tools.call("get_host_access", {"project": "demo"})
                self.assertEqual(final["requests"], [])
                self.assertEqual(final["active"], decision == "confirm")
                if final["active"]:
                    self.assertNotIn("approval_card", final)

    async def test_inactive_renewable_status_routes_to_inline_card_without_requesting(self):
        for reason in ("NO_GRANT", "EXPIRED", "REVOKED", "RUNTIME_CHANGED", "BOOT_CHANGED"):
            with self.subTest(reason=reason):
                access = FakeAccess(status_result={
                    "project": "demo", "active": False, "reason": reason, "requests": [],
                })
                tools = HostExecutionTools(
                    {"demo": Path("/tmp/demo")}, Path("/tmp/state"),
                    {"demo": asyncio.Lock()}, manager=self.manager, access=access)

                result = await tools.call("get_host_access", {"project": "demo"})

                self.assertEqual(result["approval_kind"], "host_access")
                self.assertEqual(result["approval_reason"], reason)
                self.assertEqual(result["approval_card"], {
                    "tool": "open_host_access_approval", "arguments": {"project": "demo"},
                })
                self.assertEqual(access.get_calls, 1)
                self.assertEqual(access.request_calls, 0)

    async def test_active_status_keeps_grant_without_reapproval_route(self):
        result = await self.tools.call("get_host_access", {"project": "demo"})

        self.assertTrue(result["active"])
        self.assertNotIn("approval_kind", result)
        self.assertNotIn("approval_reason", result)
        self.assertNotIn("approval_card", result)

    async def test_unsafe_or_changed_scope_status_does_not_recommend_renewal(self):
        for reason in ("UID_CHANGED", "PROJECT_CHANGED", "UNSAFE_RUNTIME"):
            with self.subTest(reason=reason):
                access = FakeAccess(status_result={
                    "project": "demo", "active": False, "reason": reason, "requests": [],
                })
                tools = HostExecutionTools(
                    {"demo": Path("/tmp/demo")}, Path("/tmp/state"),
                    {"demo": asyncio.Lock()}, manager=self.manager, access=access)

                result = await tools.call("get_host_access", {"project": "demo"})

                self.assertNotIn("approval_kind", result)
                self.assertNotIn("approval_reason", result)
                self.assertNotIn("approval_card", result)

    async def test_access_arguments_remain_strict_and_backend_error_code_is_preserved(self):
        with self.assertRaisesRegex(ValueError, "INVALID_HOST_EXECUTION_ARGUMENTS"):
            await self.tools.call("get_host_access", {"project": "demo", "approved": True})

        tools = HostExecutionTools(
            {"demo": Path("/tmp/demo")}, Path("/tmp/state"), {"demo": asyncio.Lock()},
            manager=self.manager, access=FakeAccess(error=HostRuntimeError("UNSAFE_RUNTIME")))
        with self.assertRaisesRegex(ValueError, "UNSAFE_RUNTIME"):
            await tools.call("request_host_access", {"project": "demo"})

    async def test_lazy_manager_reuses_workspace_bound_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir(mode=0o700)
            owner = root / "owner"
            owner.mkdir(mode=0o700)
            policy = owner / "policy.json"
            policy.write_text(json.dumps({
                "version": 1,
                "base_root": str(root),
                "artifacts": [],
                "protected_paths": [],
            }), encoding="utf-8")
            os.chmod(policy, 0o600)
            projects = {"demo": project}
            workspace = WorkspaceTools(
                projects, state_dir=root / "authority", config_path=policy)
            snapshot = workspace.policy.load(required=True)
            workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
            tools = HostExecutionTools(
                projects, root / "host-state", {"demo": asyncio.Lock()},
                workspace=workspace)

            access = tools.access()
            manager = tools.manager()

            self.assertIs(access.workspace, workspace)
            self.assertIs(manager.access, access)
            self.assertIs(tools.access(), access)
            self.assertIs(tools.manager(), manager)
            await tools.close()


if __name__ == "__main__":
    unittest.main()
