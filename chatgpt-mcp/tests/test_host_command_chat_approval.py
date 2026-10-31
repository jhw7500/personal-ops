"""Receipt/capability boundaries for exact registered commands; no real host command."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_access  # noqa: E402
from host_command_operations import HostCommandOperations  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class HostCommandChatApprovalTests(unittest.TestCase):
    def setUp(self):
        from host_command_ui import HostCommandApprovals
        self.tmp = tempfile.TemporaryDirectory(prefix="command-card-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.artifact = self.project / "bundle.ait"
        self.artifact.write_bytes(b"SYNTHETIC-BUNDLE")
        self.artifact.chmod(0o600)
        self.program = self.root / "upload"
        self.program.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.program.chmod(0o700)
        self.config = self.root / "profiles.json"
        self.config.write_text(json.dumps({"version": 1, "enabled": True, "profiles": {
            "test-upload": {
                "project": "example", "label": "Upload TEST artifact",
                "executable": str(self.program),
                "executable_sha256": hashlib.sha256(self.program.read_bytes()).hexdigest(),
                "argv": ["test"], "cwd": ".", "effect": "external_publish",
                "timeout_seconds": 60,
                "inputs": {"bundle.ait": hashlib.sha256(self.artifact.read_bytes()).hexdigest()},
            }}}))
        self.config.chmod(0o600)
        self.executions = []
        def runner(frozen, cancel):
            self.executions.append(frozen)
            return {"exit_code": 0, "duration_ms": 1, "stdout_bytes": 0,
                    "stderr_bytes": 0, "termination_verified": True}
        self.ops = HostCommandOperations({"example": self.project}, config_path=self.config,
            state_dir=self.root / "state", runner=runner, test_only=True)
        self.plan = self.ops.prepare("example", "test-upload", "card")
        self.operation_id = self.plan["operation_id"]
        self.cards = HostCommandApprovals(self.ops, trusted_runtime=lambda: "a" * 64)
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for worker, _ in list(self.ops._workers.values()):
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def approve_and_execute(self, args):
        callback = getattr(self.cards, "confirm_and_execute", None)
        self.assertTrue(callable(callback), "Missing explicit approve-and-execute callback")
        return callback(**args)

    def open(self):
        return self.cards.open("example", self.operation_id)

    @staticmethod
    def args(card):
        return {"project": card["structuredContent"]["project"],
                "operation_id": card["structuredContent"]["operation_id"],
                "card_id": card["structuredContent"]["card_id"],
                "confirmation_token": card["_meta"]["host_command_confirmation"]}

    def admin_approve(self):
        return self.ops.approve_web("example", operation_id=self.operation_id,
            plan_digest=self.plan["plan_digest"], source_digest=self.plan["source_digest"],
            confirmation=self.operation_id)

    def test_open_shows_bound_profile_without_secrets_or_approval(self):
        got = self.open()
        public = got["structuredContent"]
        self.assertEqual(public["component"], "host_command")
        self.assertEqual(public["profile_id"], "test-upload")
        self.assertEqual(public["effect"], "external_publish")
        self.assertFalse(public["executes_on_approval"])
        self.assertFalse(public["receipt_verified"])
        self.assertTrue(public["approval_required"])
        self.assertEqual(public["input_count"], 1)
        text = json.dumps(public) + json.dumps(got["content"])
        for secret in (got["_meta"]["host_command_confirmation"], "approval_url",
                       "approval_command", "SYNTHETIC-BUNDLE", str(self.root), "argv", "nonce"):
            self.assertNotIn(secret, text)
        self.assertEqual(list(self.ops.receipts_dir.glob("*.json")), [])
        self.assertEqual(self.executions, [])

    def test_confirm_issues_exact_receipt_without_execution(self):
        got = self.cards.confirm(**self.args(self.open()))
        public = got["structuredContent"]
        self.assertTrue(public["approved"])
        self.assertTrue(public["receipt_verified"])
        self.assertFalse(public["approval_required"])
        self.assertEqual(public["next_action"], {"tool": "execute_host_command", "arguments": {
            "project": "example", "operation_id": self.operation_id}})
        self.assertEqual(public["plan"]["state"], "approved")
        self.ops._receipt(self.ops._load(self.operation_id, "example"))
        self.assertNotIn("_meta", got)
        self.assertEqual(self.executions, [])

    def test_explicit_combined_callback_queues_exact_command_without_model_execute(self):
        opened = self.open()
        self.assertTrue(opened["structuredContent"].get("supports_direct_execution"))
        self.assertEqual(self.executions, [])
        result = self.approve_and_execute(self.args(opened))
        public = result["structuredContent"]
        self.assertTrue(public["execution_requested"])
        self.assertTrue(public["execution_started"])
        self.assertTrue(public["plan"]["started_at"])
        self.assertNotIn("approval_url", public["plan"])
        self.assertEqual(public["next_action"], {"tool": "get_host_command", "arguments": {
            "project": "example", "operation_id": self.operation_id}})
        self.assertNotIn("_meta", result)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())
        self.finish_workers()
        self.assertEqual(self.ops.get("example", self.operation_id)["state"], "succeeded")
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0]["profile_id"], "test-upload")
        self.assertEqual(self.executions[0]["inputs"], {"bundle.ait": hashlib.sha256(b"SYNTHETIC-BUNDLE").hexdigest()})

    def test_simultaneous_combined_callbacks_start_one_command(self):
        args = self.args(self.open())
        def start():
            try:
                return self.approve_and_execute(args)["structuredContent"]["execution_started"]
            except HostRuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.finish_workers()
        self.assertEqual(len(self.executions), 1)
        with self.assertRaises(HostRuntimeError):
            self.approve_and_execute(args)

    def test_combined_callback_rejects_wrong_capability_without_approval_or_execution(self):
        args = self.args(self.open())
        with self.assertRaises(HostRuntimeError):
            self.approve_and_execute({**args, "confirmation_token": "0" * 64})
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())
        self.assertIsNone(self.ops.get("example", self.operation_id)["started_at"])
        self.assertEqual(self.executions, [])

    def test_combined_callback_rejects_changed_input_before_approval_or_execution(self):
        args = self.args(self.open())
        self.artifact.write_bytes(b"CHANGED")
        with self.assertRaises(HostRuntimeError):
            self.approve_and_execute(args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())
        self.assertEqual(self.executions, [])

    def test_combined_callback_expiring_before_enqueue_never_claims_it_started(self):
        import host_command_operations
        args = self.args(self.open())
        begin = self.ops._begin
        later = host_access._utcnow() + timedelta(days=7, seconds=1)
        def enqueue_after_deadline(*pos, **kw):
            with mock.patch.object(host_command_operations, "_now", return_value=later):
                return begin(*pos, **kw)
        with mock.patch.object(self.ops, "_begin", side_effect=enqueue_after_deadline):
            public = self.approve_and_execute(args)["structuredContent"]
        self.assertTrue(public["execution_requested"])
        self.assertFalse(public["execution_started"])
        self.assertEqual(public["plan"]["state"], "expired")
        self.assertNotIn("approval_url", public["plan"])
        self.assertEqual(public["next_action"]["tool"], "get_host_command")
        self.assertEqual(self.executions, [])

    def test_combined_callback_rejects_expired_card_without_execution(self):
        args = self.args(self.open())
        later = host_access._utcnow() + timedelta(days=7, seconds=1)
        with mock.patch.object(host_access, "_utcnow", return_value=later):
            with self.assertRaises(HostRuntimeError):
                self.approve_and_execute(args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())
        self.assertEqual(self.executions, [])

    def test_approved_reopen_recovers_receipt_without_another_capability(self):
        self.admin_approve()
        got = self.open()
        self.assertTrue(got["structuredContent"]["receipt_verified"])
        self.assertEqual(got["structuredContent"]["state"], "approved")
        self.assertNotIn("_meta", got)
        self.assertNotIn("approval_url", got["structuredContent"]["plan"])
        self.assertEqual(self.executions, [])

    def test_cancel_marks_only_pending_plan_cancelled(self):
        got = self.cards.cancel(**self.args(self.open()))
        self.assertFalse(got["structuredContent"]["approved"])
        self.assertEqual(got["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.ops.get("example", self.operation_id)["state"], "cancelled")
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())
        self.assertEqual(self.executions, [])

    def test_wrong_token_and_cross_project_or_card_cannot_approve(self):
        args = self.args(self.open())
        for change in ({"confirmation_token": "0" * 64}, {"card_id": "0" * 32},
                       {"project": "other"}, {"operation_id": "0" * 32}):
            with self.subTest(change=tuple(change)):
                with self.assertRaises(HostRuntimeError):
                    self.cards.confirm(**{**args, **change})
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())

    def test_reopening_rotates_capability_and_rejects_old_card(self):
        old = self.open()
        new = self.open()
        self.assertNotEqual(old["_meta"], new["_meta"])
        with self.assertRaises(HostRuntimeError):
            self.cards.confirm(**self.args(old))
        self.assertTrue(self.cards.confirm(**self.args(new))["structuredContent"]["approved"])

    def test_expired_card_cannot_issue_receipt(self):
        args = self.args(self.open())
        later = host_access._utcnow() + timedelta(days=7, seconds=1)
        with mock.patch.object(host_access, "_utcnow", return_value=later):
            with self.assertRaises(HostRuntimeError):
                self.cards.confirm(**args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())

    def test_runtime_boot_and_uid_changes_deny_old_card(self):
        args = self.args(self.open())
        for target, attr, value in ((host_access, "_current_boot_id", "another-boot"),
                                    (os, "getuid", os.getuid() + 1)):
            with self.subTest(attr=attr), mock.patch.object(target, attr, return_value=value):
                with self.assertRaises(HostRuntimeError):
                    self.cards.confirm(**args)
        self.cards._runtime_source = lambda: "b" * 64
        with self.assertRaises(HostRuntimeError):
            self.cards.confirm(**args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())

    def test_artifact_drift_prevents_receipt(self):
        args = self.args(self.open())
        self.artifact.write_bytes(b"CHANGED")
        with self.assertRaises(HostRuntimeError):
            self.cards.confirm(**args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())

    def test_config_drift_prevents_receipt(self):
        args = self.args(self.open())
        config = json.loads(self.config.read_text())
        config["profiles"]["test-upload"]["argv"] = ["changed"]
        self.config.write_text(json.dumps(config))
        with self.assertRaises(HostRuntimeError):
            self.cards.confirm(**args)
        self.assertFalse(self.ops._receipt_path(self.operation_id).exists())

    def test_replay_and_simultaneous_confirms_issue_one_receipt(self):
        args = self.args(self.open())
        def confirm():
            try:
                return self.cards.confirm(**args)["structuredContent"]["approved"]
            except HostRuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: confirm(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(confirm())
        self.assertEqual(len(list(self.ops.receipts_dir.glob("*.json"))), 1)
        self.assertEqual(self.executions, [])

    def test_admin_winning_cancel_race_is_reported_as_approved(self):
        args = self.args(self.open())
        self.admin_approve()
        got = self.cards.cancel(**args)
        self.assertTrue(got["structuredContent"]["approved"])
        self.assertEqual(self.ops.get("example", self.operation_id)["state"], "approved")

    def test_admin_winning_between_check_and_issue_recovers_only_exact_receipt(self):
        args = self.args(self.open())
        original = self.ops.approve_web
        def race(*pos, **kw):
            original(*pos, **kw)
            return original(*pos, **kw)
        with mock.patch.object(self.ops, "approve_web", side_effect=race):
            got = self.cards.confirm(**args)
        self.assertTrue(got["structuredContent"]["receipt_verified"])
        self.assertEqual(self.executions, [])

    def test_corrupt_approved_receipt_cannot_be_reported_as_approved(self):
        self.admin_approve()
        self.ops._receipt_path(self.operation_id).write_text("{}")
        with self.assertRaises(HostRuntimeError):
            self.open()

    def test_path_traversal_and_untrusted_runtime_deny_before_issuing_capability(self):
        for operation_id in ("../escape", "g" * 32, None):
            with self.subTest(operation_id=operation_id), self.assertRaises(HostRuntimeError):
                self.cards.open("example", operation_id)
        self.ops._trusted = False
        with self.assertRaises(HostRuntimeError):
            self.open()


if __name__ == "__main__":
    unittest.main()
