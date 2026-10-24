"""Offline runtime-card capabilities. No production approval or service calls."""

import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from datetime import timedelta
import host_access
from host_runtime_config import HostRuntimeError
from runtime_rollout_web import RuntimeRolloutWeb, PROJECT, _utc, PLAN_TTL


class RuntimeChatTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("runtime_rollout_ui"),
            "runtime rollout has no inline confirmation implementation",
        )
        self.ui = importlib.import_module("runtime_rollout_ui")
        self.temp = tempfile.TemporaryDirectory(prefix="rollout-chat-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        source = Path(__file__).resolve().parents[2]
        for name in ("chatgpt-mcp", "work-dashboard"):
            shutil.copytree(
                source / name,
                self.root / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        self.started = []
        self.identity = "a" * 64
        self.boot_id = "11111111-2222-3333-4444-555555555555"
        self.ops = RuntimeRolloutWeb(
            {PROJECT: self.root},
            state_dir=self.base / "state",
            launcher=lambda argv: self.started.append(argv) or True,
            runtime_reader=lambda: self.identity,
            boot_reader=lambda: self.boot_id,
            uid_reader=lambda: 1000,
            test_only=True,
        )
        self.cards = self.ui.RuntimeRolloutApprovals(
            self.ops, trusted_runtime=lambda: self.identity
        )
        self.plan = self.ops.prepare(PROJECT, "card-fixture")
        self.op = self.plan["operation_id"]

    def open(self):
        return self.cards.open(PROJECT, self.op)

    def confirm(self, result):
        p = result["structuredContent"]
        return self.cards.confirm(
            PROJECT,
            self.op,
            card_id=p["card_id"],
            confirmation_token=result["_meta"]["runtime_rollout_confirmation"],
        )

    def cancel(self, result):
        p = result["structuredContent"]
        return self.cards.cancel(
            PROJECT,
            self.op,
            card_id=p["card_id"],
            confirmation_token=result["_meta"]["runtime_rollout_confirmation"],
        )

    def test_open_is_no_approval_or_execution_and_hides_capability(self):
        r = self.open()
        p = r["structuredContent"]
        token = r["_meta"]["runtime_rollout_confirmation"]
        self.assertEqual(p["component"], "runtime_rollout")
        self.assertEqual(p["state"], "pending")
        self.assertEqual(p["target_snapshot"], self.plan["target_snapshot"])
        self.assertEqual(
            p["impact"]["restart"],
            ["personal-ops-tunnel.service", "personal-ops-admin.service"],
        )
        self.assertIn("PostgreSQL", p["impact"]["preserve"])
        self.assertNotIn(token, json.dumps(p) + json.dumps(r["content"]))
        self.assertFalse(p["executes_on_approval"])
        self.assertFalse(self.started)
        self.assertFalse((self.ops.receipts_dir / (self.op + ".json")).exists())

    def test_confirm_issues_exact_existing_receipt_without_launch(self):
        r = self.confirm(self.open())
        p = r["structuredContent"]
        self.assertTrue(p["approved"])
        self.assertTrue(p["receipt_verified"])
        self.assertFalse(p["executes_on_approval"])
        self.assertEqual(
            p["next_action"],
            {
                "tool": "execute_runtime_rollout",
                "arguments": {"project": PROJECT, "operation_id": self.op},
            },
        )
        self.assertNotIn("_meta", r)
        self.assertFalse(self.started)
        receipt = json.loads((self.ops.receipts_dir / (self.op + ".json")).read_text())
        self.assertEqual(receipt["plan_digest"], self.plan["plan_digest"])
        queued = self.ops.execute(PROJECT, self.op)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(len(self.started), 1)
        self.ops.execute(PROJECT, self.op)
        self.assertEqual(len(self.started), 1)

    def test_reopen_approved_does_not_request_approval(self):
        self.confirm(self.open())
        receipt = (self.ops.receipts_dir / (self.op + ".json")).read_bytes()
        r = self.open()
        self.assertFalse(r["structuredContent"]["approval_required"])
        self.assertNotIn("_meta", r)
        self.assertEqual(
            receipt, (self.ops.receipts_dir / (self.op + ".json")).read_bytes()
        )
        self.assertFalse(self.started)

    def test_runtime_change_after_confirm_refuses_approved_card_reopen(self):
        self.confirm(self.open())
        self.identity = "b" * 64

        with self.assertRaises(HostRuntimeError) as caught:
            self.open()

        self.assertEqual(caught.exception.code, "ROLLOUT_APPROVAL_STALE")
        self.assertFalse(self.started)

    def test_original_web_receipt_is_reused(self):
        self.ops.approve_web(
            PROJECT,
            operation_id=self.op,
            plan_digest=self.plan["plan_digest"],
            source_digest=self.plan["source_digest"],
            confirmation=self.op,
        )
        self.assertTrue(self.open()["structuredContent"]["receipt_verified"])
        self.assertFalse(self.started)

    def test_missing_or_invalid_token_never_approves(self):
        r = self.open()
        for token in ("", "0" * 64, "wrong"):
            with self.subTest(token=token), self.assertRaises(HostRuntimeError):
                self.cards.confirm(
                    PROJECT,
                    self.op,
                    card_id=r["structuredContent"]["card_id"],
                    confirmation_token=token,
                )
        self.assertEqual(
            self.ops.get(PROJECT, self.op)["state"], "pending_host_approval"
        )

    def test_old_card_is_invalidated_on_reopen(self):
        old = self.open()
        new = self.open()
        with self.assertRaises(HostRuntimeError):
            self.confirm(old)
        self.assertTrue(self.confirm(new)["structuredContent"]["approved"])

    def test_replay_is_refused(self):
        r = self.open()
        self.confirm(r)
        with self.assertRaises(HostRuntimeError):
            self.confirm(r)
        self.assertFalse(self.started)

    def test_runtime_and_boot_changes_deny_capability(self):
        r = self.open()
        self.identity = "b" * 64
        with self.assertRaises(HostRuntimeError):
            self.confirm(r)
        self.identity = "a" * 64
        with mock.patch.object(
            host_access, "_current_boot_id", return_value="different-boot"
        ):
            with self.assertRaises(HostRuntimeError):
                self.confirm(r)

    def test_expired_card_is_refused(self):
        r = self.open()
        with mock.patch.object(
            host_access,
            "_utcnow",
            return_value=_utc() + PLAN_TTL + timedelta(seconds=1),
        ):
            with self.assertRaises(HostRuntimeError):
                self.confirm(r)

    def test_cross_project_and_operation_are_denied(self):
        r = self.open()
        for p, op in [("tossApp", self.op), (PROJECT, "../invalid")]:
            with self.assertRaises(HostRuntimeError):
                self.cards.open(p, op)
        with self.assertRaises(HostRuntimeError):
            self.cards.confirm(
                PROJECT,
                "0" * 32,
                card_id=r["structuredContent"]["card_id"],
                confirmation_token=r["_meta"]["runtime_rollout_confirmation"],
            )

    def test_source_change_prevents_confirm(self):
        r = self.open()
        p = self.root / "chatgpt-mcp/toss_console_tools.py"
        p.write_text(p.read_text() + "\n# changed\n")
        with self.assertRaises(HostRuntimeError):
            self.confirm(r)
        self.assertFalse((self.ops.receipts_dir / (self.op + ".json")).exists())

    def test_cancel_is_terminal_without_receipt_or_execution(self):
        r = self.open()
        p = self.cancel(r)["structuredContent"]
        self.assertEqual(p["state"], "cancelled")
        self.assertFalse(p["approved"])
        self.assertFalse((self.ops.receipts_dir / (self.op + ".json")).exists())
        self.assertFalse(self.started)
        self.assertEqual(self.ops.execute(PROJECT, self.op)["state"], "cancelled")

    def test_missing_or_tampered_receipt_never_claims_approved(self):
        self.confirm(self.open())
        path = self.ops.receipts_dir / (self.op + ".json")
        r = json.loads(path.read_text())
        r["plan_digest"] = "0" * 64
        path.write_text(json.dumps(r))
        with self.assertRaises(HostRuntimeError):
            self.open()

    def test_started_plan_cannot_issue_another_card(self):
        self.confirm(self.open())
        self.ops.execute(PROJECT, self.op)
        with self.assertRaises(HostRuntimeError):
            self.open()

    def test_specs_only_allow_user_app_to_confirm(self):
        specs = {s["name"]: s for s in self.ui.tool_specs()}
        self.assertEqual(
            set(specs),
            {
                "open_runtime_rollout_approval",
                "confirm_runtime_rollout_approval",
                "cancel_runtime_rollout_approval",
            },
        )
        for n, s in specs.items():
            self.assertFalse(s["inputSchema"]["additionalProperties"])
            self.assertEqual(
                s["inputSchema"]["properties"]["project"]["enum"], [PROJECT]
            )
            self.assertEqual(
                s["_meta"]["ui"]["visibility"],
                ["model"] if n.startswith("open_") else ["app"],
            )
        self.assertNotIn(
            "confirmation_token",
            specs["open_runtime_rollout_approval"]["inputSchema"]["properties"],
        )

    def test_cancel_after_owner_web_approval_preserves_receipt(self):
        r = self.open()
        self.ops.approve_web(
            PROJECT,
            operation_id=self.op,
            plan_digest=self.plan["plan_digest"],
            source_digest=self.plan["source_digest"],
            confirmation=self.op,
        )
        self.assertTrue(self.cancel(r)["structuredContent"]["receipt_verified"])
        self.assertEqual(self.ops.get(PROJECT, self.op)["state"], "approved")
        self.assertFalse(self.started)

    def test_only_one_of_concurrent_confirmations_can_spend_the_card(self):
        from concurrent.futures import ThreadPoolExecutor

        r = self.open()

        def attempt():
            try:
                return self.confirm(r)["structuredContent"]["approved"]
            except HostRuntimeError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(self.started)

    def test_forged_card_plan_pin_never_approves(self):
        r = self.open()
        path = self.cards._path(self.op)
        card = json.loads(path.read_text())
        card["plan_digest"] = "f" * 64
        path.write_text(json.dumps(card))
        with self.assertRaises(HostRuntimeError):
            self.confirm(r)
        self.assertFalse((self.ops.receipts_dir / (self.op + ".json")).exists())

    def test_confirmation_failure_is_not_silently_retried(self):
        r = self.open()
        with mock.patch.object(
            self.ops,
            "approve_web",
            side_effect=HostRuntimeError("ROLLOUT_SOURCE_CHANGED"),
        ) as approve:
            with self.assertRaises(HostRuntimeError):
                self.confirm(r)
            with self.assertRaises(HostRuntimeError):
                self.confirm(r)
            self.assertEqual(approve.call_count, 1)
        self.assertFalse(self.started)
        self.assertFalse((self.ops.receipts_dir / (self.op + ".json")).exists())

    def test_future_receipt_time_is_rejected(self):
        self.confirm(self.open())
        path = self.ops.receipts_dir / (self.op + ".json")
        receipt = json.loads(path.read_text())
        receipt["approved_at"] = (_utc() + PLAN_TTL).isoformat()
        path.write_text(json.dumps(receipt))
        with self.assertRaises(HostRuntimeError):
            self.open()

    def test_card_file_is_owner_only_and_does_not_store_raw_token(self):
        r = self.open()
        path = self.cards._path(self.op)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(r["_meta"]["runtime_rollout_confirmation"], path.read_text())
