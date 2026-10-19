from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_execution_tools import HOST_EXECUTION_TOOL_SPECS, HostExecutionTools  # noqa: E402


class FakeAccess:
    def request(self, project):
        return {"project": project, "state": "requested"}

    def get(self, project):
        return {"project": project, "state": "active"}


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


if __name__ == "__main__":
    unittest.main()
