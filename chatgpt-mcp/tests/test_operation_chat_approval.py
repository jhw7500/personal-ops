"""Temporary backend fixtures for owner-operated operation approval cards."""
from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_operation_fixtures import FakeHostRunner, commit_all, fixture_v3
from host_registry import HostTargetRegistry
from host_registered_operations import RegisteredHostOperations
from host_runtime_config import HostRuntimeError, PROJECT, TARGET
from host_runtime_operations import _save_private_json
from test_host_registered_operations import FakeAdapter
from test_host_target_authorization import TargetAuthorizationFixture
import test_agent_tasks as agent_fixture
try:
    from operation_approval_ui import OperationApprovals, OperationApprovalTools, tool_specs
except ModuleNotFoundError as exc:
    if exc.name != "operation_approval_ui":
        raise
    OperationApprovals = OperationApprovalTools = tool_specs = None

class Assertions:
    def cards(self, family, manager):
        self.assertIsNotNone(OperationApprovals, "Missing owner-operated chat approval")
        self.runtime_identity = "a" * 64
        return OperationApprovals(family, manager,
                                  trusted_runtime=lambda: self.runtime_identity)
    @staticmethod
    def callback(opened):
        return {"card_id": opened["structuredContent"]["card_id"],
                "confirmation_token": opened["_meta"]["operation_confirmation"]}
    def code(self, code, call):
        with self.assertRaisesRegex(HostRuntimeError, "^" + code + "$"):
            call()
    def private(self, opened):
        token = opened["_meta"]["operation_confirmation"]
        self.assertNotIn(token, json.dumps(opened["structuredContent"]))
        self.assertNotIn(token, json.dumps(opened["content"]))
        self.assertNotIn("approval_url", opened["structuredContent"]["plan"])
        self.assertNotIn("approval_command", opened["structuredContent"]["plan"])

class RegisteredChatApprovalTest(Assertions, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="registered-chat-approval-")
        self.addAsyncCleanup(asyncio.to_thread, temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "repo"
        self.root.mkdir()
        payload = fixture_v3(self.root)
        commit_all(self.root)
        self.config = self.home / "config.json"
        self.config.write_text(json.dumps(payload))
        self.config.chmod(0o600)
        self.runner = FakeHostRunner()
        self.adapter = FakeAdapter(self.runner.run)
        self.operations = RegisteredHostOperations(
            {PROJECT: self.root}, HostTargetRegistry({PROJECT: self.root}, self.config),
            state_dir=self.home / "state", runner=self.runner.run,
            adapter_factory=lambda *_: self.adapter)
        self.plan = self.operations.prepare(PROJECT, TARGET, action="compose.restart",
            parameters={"services": ["api"]}, request_id="chat-registered")
        self.ui = self.cards("registered", self.operations)
    def open(self):
        return self.ui.open(PROJECT, self.plan["operation_id"], target=TARGET)
    def confirm(self, opened):
        return self.ui.confirm(PROJECT, self.plan["operation_id"], target=TARGET,
                               **self.callback(opened))
    async def test_open_hides_capability_and_never_approves_or_executes(self):
        opened = self.open()
        self.private(opened)
        public = opened["structuredContent"]
        self.assertEqual(public["family"], "registered")
        self.assertEqual(public["plan"]["action"], "compose.restart")
        self.assertEqual(public["plan"]["parameters"], {"services": ["api"]})
        self.assertTrue(public["approval_required"])
        self.assertFalse(public["receipt_verified"])
        self.assertFalse(self.operations._receipt_path(self.plan["operation_id"]).exists())
        self.assertEqual(self.adapter.execute_calls, 0)
    async def test_confirm_once_and_reopen_reuses_verified_receipt(self):
        opened = self.open()
        result = self.confirm(opened)["structuredContent"]
        self.assertTrue(result["receipt_verified"])
        self.assertFalse(result["approval_required"])
        self.assertEqual(result["next_action"], {"tool": "execute_host_operation",
            "arguments": {"project": PROJECT, "target": TARGET,
                          "operation_id": self.plan["operation_id"]}})
        self.code("OPERATION_CARD_SPENT", lambda: self.confirm(opened))
        reopened = self.open()
        self.assertNotIn("_meta", reopened)
        self.assertTrue(reopened["structuredContent"]["receipt_verified"])
        self.assertEqual(self.adapter.execute_calls, 0)
    async def test_source_drift_rejects_without_receipt(self):
        opened = self.open()
        (self.root / "server" / "package.json").write_text("{}\n")
        self.code("STALE_PLAN", lambda: self.confirm(opened))
        self.assertFalse(self.operations._receipt_path(self.plan["operation_id"]).exists())
    async def test_wrong_scope_token_runtime_boot_owner_and_old_card_fail_closed(self):
        opened = self.open()
        args = self.callback(opened)
        self.code("OPERATION_CARD_DENIED", lambda: self.ui.confirm(
            PROJECT, self.plan["operation_id"], target=TARGET,
            card_id=args["card_id"], confirmation_token="b" * 64))
        self.code("OPERATION_CARD_DENIED", lambda: self.ui.confirm(
            PROJECT, self.plan["operation_id"], target="another-target", **args))
        self.runtime_identity = "b" * 64
        self.code("OPERATION_CARD_STALE", lambda: self.confirm(opened))
        self.runtime_identity = "a" * 64
        with patch("operation_approval_ui.host_access._current_boot_id",
                   return_value="changed-boot"):
            self.code("OPERATION_CARD_STALE", lambda: self.confirm(opened))
        with patch("operation_approval_ui.os.getuid", return_value=os.getuid() + 1):
            self.assertRaises(HostRuntimeError, self.confirm, opened)
        replacement = self.open()
        self.code("OPERATION_CARD_DENIED", lambda: self.confirm(opened))
        self.assertTrue(self.confirm(replacement)["structuredContent"]["receipt_verified"])
    async def test_expiry_and_cancellation_prevent_receipt_replay(self):
        opened = self.open()
        path = self.ui._path(self.plan["operation_id"])
        card = json.loads(path.read_text())
        from host_access import _utcnow
        card["expires_at"] = (_utcnow() - timedelta(seconds=1)).isoformat()
        _save_private_json(path, card)
        self.code("OPERATION_CARD_EXPIRED", lambda: self.confirm(opened))
        opened = self.open()
        result = self.ui.cancel(PROJECT, self.plan["operation_id"], target=TARGET,
                                **self.callback(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.operations.get(PROJECT, TARGET, self.plan["operation_id"])
                         ["state"], "rejected")
        self.code("OPERATION_CARD_SPENT", lambda: self.confirm(opened))
    async def test_cancel_preserves_concurrent_web_receipt(self):
        opened = self.open()
        self.operations.approve_web(PROJECT, TARGET,
            operation_id=self.plan["operation_id"], plan_digest=self.plan["plan_digest"],
            source_digest=self.plan["source_digest"], confirmation=self.plan["operation_id"])
        result = self.ui.cancel(PROJECT, self.plan["operation_id"], target=TARGET,
                                **self.callback(opened))
        self.assertEqual(result["structuredContent"]["state"], "approved")
        self.assertTrue(result["structuredContent"]["receipt_verified"])
        self.assertTrue(self.operations._receipt_path(self.plan["operation_id"]).exists())
    async def test_concurrent_callbacks_issue_one_receipt(self):
        opened = self.open()
        def invoke(_):
            try:
                return self.confirm(opened)["structuredContent"]["state"]
            except HostRuntimeError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(invoke, range(2)))
        self.assertCountEqual(outcomes, ["approved", "OPERATION_CARD_SPENT"])
        self.assertEqual(self.adapter.execute_calls, 0)


    async def test_combined_callback_runs_existing_engine_once_and_observes_registered_scope(self):
        from host_registered_tools import RegisteredHostTools
        wrapper = RegisteredHostTools({PROJECT: self.root}, self.operations.state_dir,
                                      {PROJECT: asyncio.Lock()})
        wrapper._manager = self.operations
        router = OperationApprovalTools({PROJECT: self.root}, registered=wrapper)
        router.managers["registered"] = self.ui
        opened = self.open()
        args = {"project": PROJECT, "family": "registered", "target": TARGET,
                "operation_id": self.plan["operation_id"], **self.callback(opened)}
        result = await router.call("confirm_and_execute_operation", args)
        await asyncio.to_thread(lambda: [thread.join(10)
                                        for thread in list(self.operations._workers.values())])
        self.assertEqual(self.operations.get(PROJECT, TARGET, self.plan["operation_id"])
                         ["state"], "succeeded")
        self.assertEqual(self.adapter.execute_calls, 1)
        public = result["structuredContent"]
        self.assertTrue(public["execution_requested"])
        self.assertTrue(public["execution_started"])
        self.assertEqual(public["next_action"], {
            "tool": "read_host_operation_events", "arguments": {
                "project": PROJECT, "target": TARGET,
                "operation_id": self.plan["operation_id"]}})
        with self.assertRaisesRegex(ValueError, "OPERATION_CARD_SPENT"):
            await router.call("confirm_and_execute_operation", args)
        self.assertEqual(self.adapter.execute_calls, 1)

class RuntimeChatApprovalTest(Assertions, TargetAuthorizationFixture):
    def setUp(self):
        super().setUp()
        self.operations, self.fake = self.manager()
        self.plan = self.operations.prepare(PROJECT, TARGET, "api", "disabled", "chat-runtime")
        self.ui = self.cards("runtime", self.operations)
    def open(self):
        return self.ui.open(PROJECT, self.plan["operation_id"], target=TARGET)
    def test_schema_plans_require_their_dedicated_cards(self):
        for component, code in (("schema014", "SCHEMA014_CARD_REQUIRED"),
                                ("schema015016", "TEST_PLAN_CARD_REQUIRED")):
            with self.subTest(component=component), patch.object(
                    self.operations, "_load", return_value={"immutable": {
                        "component": component}}):
                self.code(code, self.open)
            self.assertFalse(self.ui._path(self.plan["operation_id"]).exists())
            self.assertFalse((self.operations.operations_dir /
                              (self.plan["operation_id"] + ".approval.json")).exists())
    def test_legacy_issuer_and_existing_receipt_need_one_confirmation(self):
        opened = self.open()
        self.private(opened)
        result = self.ui.confirm(PROJECT, self.plan["operation_id"], target=TARGET,
                                 **self.callback(opened))["structuredContent"]
        self.assertTrue(result["receipt_verified"])
        self.assertEqual(result["next_action"]["tool"], "execute_test_deploy")
        self.assertEqual(self.operations.get(PROJECT, TARGET, self.plan["operation_id"])
                         ["state"], "pending_host_approval")
        reopened = self.open()
        self.assertNotIn("_meta", reopened)
        self.assertFalse(reopened["structuredContent"]["approval_required"])
    def test_target_grant_is_reused_without_minting_receipt(self):
        other, _ = self.manager("authorized")
        self.enable(other)
        plan = other.prepare(PROJECT, TARGET, "api", "disabled", "target-granted")
        ui = self.cards("runtime", other)
        opened = ui.open(PROJECT, plan["operation_id"], target=TARGET)
        self.assertNotIn("_meta", opened)
        public = opened["structuredContent"]
        self.assertTrue(public["authorization_verified"])
        self.assertFalse(public["receipt_verified"])
        self.assertFalse(public["approval_required"])
        self.assertEqual(public["state"], "approved")
        self.assertEqual(public["next_action"]["tool"], "execute_test_deploy")
        self.assertFalse((other.operations_dir / (plan["operation_id"] + ".approval.json")).exists())
        other.set_target_authorization(PROJECT, TARGET, enabled=False,
                                       confirmation=PROJECT + "/" + TARGET)
        self.assertRaises(HostRuntimeError, ui.open, PROJECT, plan["operation_id"], target=TARGET)
    def test_invalid_receipt_never_suppresses_owner_approval(self):
        opened = self.open()
        path = self.operations.operations_dir / (self.plan["operation_id"] + ".approval.json")
        _save_private_json(path, {"operation_id": self.plan["operation_id"],
            "plan_digest": "b" * 64, "nonce": "c" * 64,
            "approved_at": self.plan["created_at"]})
        self.code("INVALID_HOST_APPROVAL", self.open)
        self.code("INVALID_HOST_APPROVAL", lambda: self.ui.confirm(
            PROJECT, self.plan["operation_id"], target=TARGET, **self.callback(opened)))
    def test_cancel_closes_pending_runtime_plan(self):
        opened = self.open()
        result = self.ui.cancel(PROJECT, self.plan["operation_id"], target=TARGET,
                                **self.callback(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.operations.get(PROJECT, TARGET, self.plan["operation_id"])
                         ["state"], "cancelled")

class AgentChatApprovalTest(Assertions, unittest.IsolatedAsyncioTestCase):
    setUp = agent_fixture.AgentTaskTest.setUp
    write_config = agent_fixture.AgentTaskTest.write_config
    prepare = agent_fixture.AgentTaskTest.prepare
    async def test_exact_isolated_task_approval_never_starts_a_model(self):
        plan = self.prepare()
        ui = self.cards("agent", self.manager)
        opened = ui.open("testrepo", plan["task_id"])
        self.private(opened)
        self.assertEqual(opened["structuredContent"]["plan"]["objective_sha256"],
                         plan["objective_sha256"])
        self.assertNotIn("objective", opened["structuredContent"]["plan"])
        self.assertEqual(opened["_meta"]["operation_review"]["objective"],
                         "Review one synthetic source file only")
        confirmed = ui.confirm("testrepo", plan["task_id"], **self.callback(opened))
        self.assertTrue(confirmed["structuredContent"]["receipt_verified"])
        self.assertEqual(confirmed["structuredContent"]["next_action"], {
            "tool": "start_agent_task",
            "arguments": {"project": "testrepo", "task_id": plan["task_id"]}})
        self.assertEqual(self.runner.calls, 0)
        again = ui.open("testrepo", plan["task_id"])
        self.assertTrue(again["structuredContent"]["receipt_verified"])
        self.assertNotIn("operation_confirmation", again.get("_meta", {}))
    async def test_agent_source_drift_rejects_and_cancel_closes_task(self):
        plan = self.prepare()
        ui = self.cards("agent", self.manager)
        opened = ui.open("testrepo", plan["task_id"])
        (self.root / "src/demo.py").write_text("drift\n")
        self.code("STALE_PLAN", lambda: ui.confirm(
            "testrepo", plan["task_id"], **self.callback(opened)))
        self.assertFalse((self.manager.receipts_dir / (plan["task_id"] + ".json")).exists())
        (self.root / "src/demo.py").write_text("original\n")
        opened = ui.open("testrepo", plan["task_id"])
        result = ui.cancel("testrepo", plan["task_id"], **self.callback(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.manager.get("testrepo", plan["task_id"])["state"], "cancelled")
        self.assertEqual(self.runner.calls, 0)

class ToolContractTest(unittest.TestCase):
    def test_confirm_callbacks_are_app_only(self):
        self.assertIsNotNone(tool_specs, "Missing app-only approval contracts")
        specs = {spec["name"]: spec for spec in tool_specs({"repo": "/synthetic"})}
        self.assertEqual(set(specs), {"open_operation_approval", "confirm_operation_approval",
            "confirm_and_execute_operation", "cancel_operation_approval"})
        for name, spec in specs.items():
            opener = name == "open_operation_approval"
            self.assertEqual(spec["_meta"]["ui"]["visibility"], ["model"] if opener else ["app"])
            self.assertFalse(spec["inputSchema"]["additionalProperties"])
            required = set(spec["inputSchema"]["required"])
            self.assertTrue({"project", "family", "operation_id"} <= required)
            self.assertEqual("confirmation_token" in required, not opener)
    def test_scope_and_extra_fields_rejected_before_manager_resolution(self):
        self.assertIsNotNone(OperationApprovalTools, "Missing validated card router")
        tools = OperationApprovalTools({"repo": "/synthetic"})
        base = {"project": "repo", "family": "agent", "operation_id": "1" * 32}
        for args in ({**base, "approved": True}, {**base, "target": "test"},
                     {**base, "family": "runtime"}, {**base, "family": "registered"},
                     {**base, "family": "unknown"}, {**base, "operation_id": "../x"}):
            with self.subTest(args=args), self.assertRaises(HostRuntimeError):
                tools.validate_arguments("open_operation_approval", args)

if __name__ == "__main__":
    unittest.main()
