"""TEST per-operation confirmation card: exact source, one-use, no execution.

Synthetic temporary Git repo and fake runtime only; no host Docker, DB or network.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_host_target_authorization import TargetAuthorizationFixture
from host_runtime_config import HostRuntimeError, PROJECT, TARGET
from host_test_approval_ui import TestPlanApprovals, TestPlanApprovalTools, tool_specs
from host_runtime_web import approve_web


class TestPlanChatApprovalTests(TargetAuthorizationFixture):
    def setUp(self):
        super().setUp()
        self.ops, self.fake = self.manager("test-chat")
        self.plan = self.ops.prepare(PROJECT, TARGET, "worker", "disabled", "test-chat")
        self.runtime_identity = "a" * 64
        self.cards = TestPlanApprovals(
            self.ops, trusted_runtime=lambda: self.runtime_identity)

    def open(self):
        return self.cards.open(PROJECT, TARGET, self.plan["operation_id"])

    @staticmethod
    def callback_args(opened):
        return {
            "card_id": opened["structuredContent"]["card_id"],
            "confirmation_token": opened["_meta"]["test_plan_confirmation"],
        }

    def assert_code(self, code, thunk):
        with self.assertRaisesRegex(HostRuntimeError, code):
            thunk()

    def test_pending_one_use_card_hides_capability_and_web_fallback(self):
        opened = self.open()
        data = opened["structuredContent"]
        secret = self.callback_args(opened)["confirmation_token"]
        self.assertEqual(data["component"], "worker")
        self.assertEqual(data["mode"], "disabled")
        self.assertEqual(data["operation_id"], self.plan["operation_id"])
        self.assertEqual(data["plan_digest"], self.plan["plan_digest"])
        self.assertEqual(data["source_digest"], self.plan["source_digest"])
        self.assertFalse(data["executes_on_approval"])
        self.assertTrue(data["approval_required"])
        self.assertFalse(data["receipt_verified"])
        self.assertNotIn("approval_url", data["plan"])
        self.assertNotIn("approval_command", data["plan"])
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn(secret, opened["content"][0]["text"])
        self.assertFalse(self.ops._path(self.plan["operation_id"]).with_suffix(".approval.json").exists())

    def test_confirmation_issues_same_one_use_receipt_without_execution(self):
        opened = self.open()
        args = self.callback_args(opened)
        approved = self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"], **args)
        public = approved["structuredContent"]
        self.assertEqual(public["state"], "approved")
        self.assertTrue(public["receipt_verified"])
        self.assertTrue(public["approved"])
        self.assertFalse(public["approval_required"])
        self.assertFalse(public["executes_on_approval"])
        self.assertEqual(public["plan"]["state"], "pending_host_approval")
        self.assertEqual(public["next_action"], {
            "tool": "execute_test_deploy",
            "arguments": {
                "project": PROJECT, "target": TARGET,
                "operation_id": self.plan["operation_id"],
            },
        })
        receipt = self.ops.operations_dir / (self.plan["operation_id"] + ".approval.json")
        self.assertTrue(receipt.exists())
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.plan["operation_id"])["state"],
                         "pending_host_approval")
        self.assert_code("TEST_PLAN_CARD_SPENT", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **args))

    def test_reopening_uses_new_token_and_returns_existing_receipt(self):
        first = self.open()
        second = self.open()
        self.assertNotEqual(self.callback_args(first)["confirmation_token"],
                            self.callback_args(second)["confirmation_token"])
        self.assert_code("TEST_PLAN_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(first)))
        self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"],
                           **self.callback_args(second))
        reopened = self.open()
        self.assertIn("_meta", reopened)  # fresh cancellation-only capability
        self.assertEqual(reopened["structuredContent"]["state"], "approved")
        self.assertTrue(reopened["structuredContent"]["receipt_verified"])
        self.assertTrue(reopened["structuredContent"]["cancellation_available"])
        self.assertNotIn(self.callback_args(reopened)["confirmation_token"],
                         json.dumps(reopened["structuredContent"]))

    def test_wrong_scope_component_and_changed_runtime_fail_closed(self):
        opened = self.open()
        args = self.callback_args(opened)
        self.assert_code("UNSUPPORTED_SCOPE", lambda: self.cards.confirm(
            "personal-ops", TARGET, self.plan["operation_id"], **args))
        self.assert_code("TEST_PLAN_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], card_id=args["card_id"],
            confirmation_token="b" * 64))
        self.runtime_identity = "b" * 64
        self.assert_code("TEST_PLAN_CARD_STALE", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **args))
        self.runtime_identity = "a" * 64
        state = self.ops._load(PROJECT, TARGET, self.plan["operation_id"])
        state["immutable"]["component"] = "schema014"
        self.ops._save(state)
        # The unchanged stored plan digest is rejected at the durable state layer first.
        self.assert_code("PLAN_INVALID", self.open)

    def test_cancel_rejects_unapproved_plan_never_executes(self):
        opened = self.open()
        cancelled = self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(opened))
        self.assertEqual(cancelled["structuredContent"]["state"], "cancelled")
        self.assertFalse(cancelled["structuredContent"]["approved"])
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.plan["operation_id"])["state"],
                         "rejected")
        self.assertFalse((self.ops.operations_dir /
                          (self.plan["operation_id"] + ".approval.json")).exists())

    def test_cancel_after_admin_receipt_withdraws_unexecuted_approval(self):
        opened = self.open()
        approve_web(self.ops, operation_id=self.plan["operation_id"],
                    plan_digest=self.plan["plan_digest"],
                    source_digest=self.plan["source_digest"],
                    confirmation=self.plan["operation_id"])
        result = self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertFalse(result["structuredContent"]["receipt_verified"])
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.plan["operation_id"])["state"],
                         "rejected")
        self.assertFalse((self.ops.operations_dir /
                          (self.plan["operation_id"] + ".approval.json")).exists())
        self.assert_code("TEST_PLAN_CARD_SPENT", lambda: self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(opened)))

    def test_approved_old_plan_withdrawal_unblocks_new_component_plan(self):
        # Regression: previously approved API-only plans kept TARGET_BUSY forever.
        approved_card = self.open()
        self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"],
                           **self.callback_args(approved_card))
        with self.assertRaisesRegex(HostRuntimeError, "TARGET_BUSY"):
            self.ops.prepare(PROJECT, TARGET, "api", "toss-demo", "new-blocked")

        cancel_card = self.open()
        self.assertEqual(cancel_card["structuredContent"]["state"], "approved")
        self.assertTrue(cancel_card["structuredContent"]["cancellation_available"])
        cancelled = self.cards.cancel(PROJECT, TARGET, self.plan["operation_id"],
                                      **self.callback_args(cancel_card))
        self.assertEqual(cancelled["structuredContent"]["state"], "cancelled")
        self.assertFalse(cancelled["structuredContent"]["receipt_verified"])
        self.assertEqual(cancelled["structuredContent"]["plan"]["state"], "rejected")
        self.assertFalse((self.ops.operations_dir /
                          (self.plan["operation_id"] + ".approval.json")).exists())
        # New plan must have an independent source-bound approval; old receipt gone.
        next_plan = self.ops.prepare(PROJECT, TARGET, "api", "toss-demo", "new-blocked")
        self.assertNotEqual(next_plan["operation_id"], self.plan["operation_id"])
        self.assertEqual(next_plan["state"], "pending_host_approval")
        self.assertFalse((self.ops.operations_dir /
                          (next_plan["operation_id"] + ".approval.json")).exists())

    def test_old_token_or_started_operation_cannot_be_cancelled(self):
        first = self.open()
        self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"],
                           **self.callback_args(first))
        reopened = self.open()
        self.assert_code("TEST_PLAN_CARD_DENIED", lambda: self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(first)))
        state = self.ops._load(PROJECT, TARGET, self.plan["operation_id"])
        state["state"] = "queued"
        self.ops._save(state)
        self.assert_code("TEST_PLAN_CARD_NOT_PENDING", lambda: self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(reopened)))
        self.assertTrue((self.ops.operations_dir /
                         (self.plan["operation_id"] + ".approval.json")).exists())

    def test_tampered_approved_receipt_prevents_plan_withdrawal(self):
        first = self.open()
        self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"],
                           **self.callback_args(first))
        reopened = self.open()
        receipt_path = self.ops.operations_dir / (self.plan["operation_id"] + ".approval.json")
        receipt = json.loads(receipt_path.read_text())
        receipt["nonce"] = "1" * 64
        receipt_path.write_text(json.dumps(receipt))
        self.assert_code("INVALID_HOST_APPROVAL", lambda: self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.callback_args(reopened)))
        self.assertEqual(self.ops.get(PROJECT, TARGET, self.plan["operation_id"])["state"],
                         "pending_host_approval")

    def test_api_and_legacy_migration_exact_routing(self):
        plans = (
            ("api", "toss-demo", "execute_test_deploy"),
            ("migration", "disabled", "execute_db_migration"),
            ("predecessor_migration", "disabled", "execute_predecessor_migration"),
        )
        for i, (component, mode, next_tool) in enumerate(plans):
            with self.subTest(component=component):
                ops, runtime = self.manager("other-" + str(i))
                plan = ops.prepare(PROJECT, TARGET, component, mode,
                                   "test-chat-other-" + str(i))
                cards = TestPlanApprovals(ops, trusted_runtime=lambda: "a" * 64)
                opened = cards.open(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(opened["structuredContent"]["component"], component)
                self.assertEqual(opened["structuredContent"]["mode"], mode)
                result = cards.confirm(PROJECT, TARGET, plan["operation_id"],
                                        **self.callback_args(opened))
                self.assertEqual(result["structuredContent"]["next_action"]["tool"], next_tool)

    def test_target_authorized_plan_does_not_offer_extra_approval(self):
        ops, _ = self.manager("authorized")
        self.enable(ops)
        plan = ops.prepare(PROJECT, TARGET, "worker", "disabled", "target-worker")
        cards = TestPlanApprovals(ops, trusted_runtime=lambda: "a" * 64)
        self.assert_code("TEST_PLAN_CARD_PLAN_REQUIRED", lambda: cards.open(
            PROJECT, TARGET, plan["operation_id"]))

    def test_callbacks_app_only_and_schema_rejects_extra_fields(self):
        specs = {spec["name"]: spec for spec in tool_specs()}
        self.assertEqual(set(specs), self.cards_tool_names())
        self.assertEqual(specs["open_test_plan_approval"]["_meta"]["ui"]["visibility"],
                         ["model"])
        for name in ("confirm_test_plan_approval", "cancel_test_plan_approval"):
            self.assertEqual(specs[name]["_meta"]["ui"]["visibility"], ["app"])
            self.assertTrue(specs[name]["_meta"]["openai/widgetAccessible"])
            self.assertFalse(specs[name]["inputSchema"]["additionalProperties"])
            self.assert_code("INVALID_TEST_PLAN_CARD_ARGUMENTS",
                             lambda name=name: TestPlanApprovalTools.validate_arguments(
                                 name, {"project": PROJECT, "target": TARGET,
                                        "operation_id": self.plan["operation_id"],
                                        "card_id": "0" * 32,
                                        "confirmation_token": "f" * 64, "unreviewed": True}))

    @staticmethod
    def cards_tool_names():
        return {"open_test_plan_approval", "confirm_test_plan_approval",
                "cancel_test_plan_approval"}


if __name__ == "__main__":
    unittest.main()
