"""Real registered-host plan chat approval: fake Git tree + fake Docker only."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_operation_fixtures import write_registered_fixture, fake_registered_manager
from host_registered_approval_ui import (
    RegisteredHostApprovals, RegisteredHostApprovalTools, tool_specs,
)
from host_runtime_config import HostRuntimeError, PROJECT, TARGET


class RegisteredHostChatApprovalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="registered-chat-card-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.fixture = write_registered_fixture(self.home)
        self.manager, self.runner, self.docker = fake_registered_manager(
            self.fixture["projects"], self.fixture["config"], self.home/"state")
        self.runtime_identity = "a"*64
        self.cards = RegisteredHostApprovals(
            self.manager, trusted_runtime=lambda: self.runtime_identity)
        self.plan = self.manager.prepare(
            PROJECT, TARGET, action="compose.restart",
            parameters={"services":["api"]}, request_id="chat-plan-one")

    def open(self):
        return self.cards.open(PROJECT, TARGET, self.plan["operation_id"])

    def args(self, opened):
        return {
            "card_id": opened["structuredContent"]["card_id"],
            "confirmation_token": opened["_meta"]["registered_host_confirmation"],
        }

    def assert_code(self, code, callback):
        with self.assertRaisesRegex(HostRuntimeError, code):
            callback()

    def test_open_binds_exact_registered_target_without_leaking_capability(self):
        opened = self.open()
        info = opened["structuredContent"]
        token = self.args(opened)["confirmation_token"]
        self.assertEqual(info["component"], "registered_host")
        self.assertEqual(info["project"], PROJECT)
        self.assertEqual(info["target"], TARGET)
        self.assertEqual(info["operation_id"], self.plan["operation_id"])
        self.assertEqual(info["action"], "compose.restart")
        self.assertEqual(info["parameters"], {"services":["api"]})
        self.assertTrue(info["approval_required"])
        self.assertFalse(info["receipt_verified"])
        self.assertFalse(info["executes_on_approval"])
        self.assertNotIn("approval_url", info["plan"])
        self.assertNotIn(token, json.dumps(info))
        self.assertNotIn(token, opened["content"][0]["text"])
        self.assertEqual(self.runner.effects, [])

    def test_confirm_issues_one_use_host_receipt_without_action(self):
        opened = self.open()
        args = self.args(opened)
        approved = self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"], **args)
        body = approved["structuredContent"]
        self.assertTrue(body["approved"])
        self.assertTrue(body["receipt_verified"])
        self.assertEqual(body["state"], "approved")
        self.assertEqual(body["next_action"], {
            "tool": "execute_host_operation",
            "arguments": {"project": PROJECT, "target": TARGET,
                          "operation_id": self.plan["operation_id"]},
        })
        self.assertEqual(self.manager.get(PROJECT,TARGET,self.plan["operation_id"])["state"],
                         "approved")
        self.assertTrue(self.manager._receipt_path(self.plan["operation_id"]).exists())
        self.assertEqual(self.runner.effects, [])
        self.assert_code("REGISTERED_CARD_SPENT", lambda: self.cards.confirm(
            PROJECT,TARGET,self.plan["operation_id"], **args))
        reopened = self.open()
        self.assertNotIn("_meta", reopened)
        self.assertTrue(reopened["structuredContent"]["receipt_verified"])
        self.assertEqual(reopened["structuredContent"]["state"],"approved")

    def test_wrong_scope_token_and_runtime_fail_closed(self):
        opened=self.open()
        args=self.args(opened)
        # alpha is an allowed project name, but this card is bound to tossApp.
        self.assert_code("REGISTERED_CARD_DENIED", lambda: self.cards.confirm(
            "alpha", TARGET, self.plan["operation_id"], **args))
        self.assert_code("REGISTERED_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"],
            card_id=args["card_id"], confirmation_token="b"*64))
        self.runtime_identity="b"*64
        self.assert_code("REGISTERED_CARD_STALE", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **args))

    def test_reopen_rotates_hidden_capability(self):
        first=self.open()
        second=self.open()
        self.assertNotEqual(self.args(first)["confirmation_token"],
                            self.args(second)["confirmation_token"])
        self.assert_code("REGISTERED_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT,TARGET,self.plan["operation_id"], **self.args(first)))

    def test_cancel_only_rejects_pending_plan(self):
        opened=self.open()
        response=self.cards.cancel(PROJECT,TARGET,self.plan["operation_id"],
                                   **self.args(opened))
        self.assertEqual(response["structuredContent"]["state"],"cancelled")
        self.assertFalse(response["structuredContent"]["approved"])
        self.assertEqual(self.manager.get(PROJECT,TARGET,self.plan["operation_id"])["state"],
                         "rejected")
        self.assertEqual(self.runner.effects, [])
        self.assertFalse(self.manager._receipt_path(self.plan["operation_id"]).exists())

    def test_cancel_does_not_revoke_concurrent_existing_admin_approval(self):
        opened=self.open()
        self.manager.approve_web(PROJECT,TARGET,operation_id=self.plan["operation_id"],
                                 plan_digest=self.plan["plan_digest"],
                                 source_digest=self.plan["source_digest"],
                                 confirmation=self.plan["operation_id"])
        response=self.cards.cancel(PROJECT,TARGET,self.plan["operation_id"],
                                   **self.args(opened))
        self.assertEqual(response["structuredContent"]["state"],"approved")
        self.assertTrue(response["structuredContent"]["receipt_verified"])
        self.assertEqual(self.runner.effects, [])

    def test_invalid_input_schema_is_exact_and_callback_app_only(self):
        specs={x["name"]:x for x in tool_specs(self.fixture["projects"])}
        self.assertEqual(set(specs), {"open_registered_host_approval",
                                     "confirm_registered_host_approval",
                                     "cancel_registered_host_approval"})
        self.assertEqual(specs["open_registered_host_approval"]["_meta"]["ui"]["visibility"],
                         ["model"])
        for name in ("confirm_registered_host_approval","cancel_registered_host_approval"):
            with self.subTest(name=name):
                self.assertEqual(specs[name]["_meta"]["ui"]["visibility"],["app"])
                self.assertTrue(specs[name]["_meta"]["openai/widgetAccessible"])
                self.assertFalse(specs[name]["inputSchema"]["additionalProperties"])
        class Stub:
            projects={PROJECT:Path("/tmp"),"alpha":Path("/tmp")}
        router=RegisteredHostApprovalTools(Stub())
        with self.assertRaisesRegex(HostRuntimeError,"INVALID_REGISTERED_CARD_ARGUMENTS"):
            router.validate_arguments("confirm_registered_host_approval",{
                "project":PROJECT,"target":TARGET,"operation_id":"1"*32,
                "card_id":"2"*32,"confirmation_token":"3"*64,"unknown":"model",
            })


if __name__ == "__main__":
    unittest.main()
