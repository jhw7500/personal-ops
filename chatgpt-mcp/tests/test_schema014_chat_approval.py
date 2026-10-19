"""Schema 014 chat approval is one-use and never executes the migration.

All plans and files are synthetic temporary fixtures. No live Docker, database,
grant, service, or model is used.
"""
from __future__ import annotations

from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_host_target_authorization import TargetAuthorizationFixture
from test_host_schema014 import FakeRuntime, REVIEWED_SQL
from host_runtime import PREDECESSOR_NAMES
from host_runtime_config import HostRuntimeError, PROJECT, TARGET


class Schema014ChatApprovalTests(TargetAuthorizationFixture):
    def setUp(self):
        super().setUp()
        from host_operation_ui import Schema014Approvals
        path = self.root / "server/migrations/014_person_relationship_detail.sql"
        path.write_text(REVIEWED_SQL)
        self.manager, runtime = self.manager("schema014-chat-card")
        checksums = {
            name: sha256((self.root / "server/migrations" / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES
        }
        self.fake = FakeRuntime(self.root, checksums)
        runtime.run = self.fake.run
        self.plan = self.manager.prepare(
            PROJECT, TARGET, "schema014", "disabled", "schema014-chat-card")
        self.runtime_identity = "a" * 64
        self.cards = Schema014Approvals(
            self.manager, trusted_runtime=lambda: self.runtime_identity)

    def open(self):
        return self.cards.open(PROJECT, TARGET, self.plan["operation_id"])

    @staticmethod
    def args(opened):
        return {
            "card_id": opened["structuredContent"]["card_id"],
            "confirmation_token": opened["_meta"]["schema014_confirmation"],
        }

    def assert_code(self, code, callback):
        with self.assertRaisesRegex(HostRuntimeError, code):
            callback()

    def test_open_exposes_exact_plan_but_keeps_capability_component_only(self):
        opened = self.open()
        public = opened["structuredContent"]
        token = opened["_meta"]["schema014_confirmation"]
        self.assertEqual(public["project"], PROJECT)
        self.assertEqual(public["target"], TARGET)
        self.assertEqual(public["operation_id"], self.plan["operation_id"])
        self.assertEqual(public["component"], "schema014")
        self.assertEqual(public["state"], "pending")
        self.assertEqual(public["plan_digest"], self.plan["plan_digest"])
        self.assertEqual(public["source_digest"], self.plan["source_digest"])
        self.assertEqual(public["schema014_sha256"],
                         self.plan["impact"]["schema_change"] and
                         self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
                         ["immutable"]["migration_checksum"])
        self.assertEqual(public["predecessor_checksums"], self.plan["migration_checksums"])
        self.assertTrue(public["backup_before_change"])
        self.assertFalse(public["service_restart"])
        self.assertFalse(public["executes_on_approval"])
        self.assertTrue(public["approval_required"])
        self.assertFalse(public["receipt_verified"])
        self.assertNotIn("next_action", public)
        self.assertEqual(public["plan"]["state"], "pending_host_approval")
        self.assertEqual(public["plan"]["operation_id"], self.plan["operation_id"])
        self.assertEqual(public["plan"]["changed_paths"], self.plan["changed_paths"])
        self.assertNotIn(token, json.dumps(public))
        self.assertNotIn(token, opened["content"][0]["text"])

    def test_confirm_issues_existing_receipt_once_without_executing(self):
        opened = self.open()
        result = self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened))
        self.assertEqual(result["structuredContent"]["state"], "approved")
        self.assertTrue(result["structuredContent"]["approved"])
        self.assertTrue(result["structuredContent"]["receipt_verified"])
        self.assertFalse(result["structuredContent"]["approval_required"])
        self.assertEqual(result["structuredContent"]["next_action"], {
            "tool": "execute_schema014_migration",
            "arguments": {"project": PROJECT, "target": TARGET,
                          "operation_id": self.plan["operation_id"]},
        })
        self.assertFalse(result["structuredContent"]["plan"]["approval_required"])
        self.assertTrue(result["structuredContent"]["plan"]["receipt_verified"])
        self.assertNotIn("approval_url", result["structuredContent"]["plan"])
        self.assertNotIn("approval_command", result["structuredContent"]["plan"])
        self.assertEqual(result["structuredContent"]["plan"]["state"],
                         "pending_host_approval")
        self.assertIn("does not execute", result["structuredContent"]["guidance"])
        self.assertFalse(self.fake.applied)
        receipt = self.manager.operations_dir / (self.plan["operation_id"] + ".approval.json")
        self.assertTrue(receipt.is_file())
        self.assert_code("SCHEMA014_CARD_SPENT", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened)))
        self.assertFalse(self.fake.applied)

    def test_pending_card_does_not_advertise_external_approval_steps(self):
        self.assertIn("approval_url", self.plan)
        self.assertIn("approval_command", self.plan)
        opened = self.open()
        public = opened["structuredContent"]
        for key in ("approval_url", "approval_command"):
            with self.subTest(key=key):
                self.assertNotIn(key, public["plan"])
                self.assertNotIn('"' + key + '"', opened["content"][0]["text"])
        self.assertTrue(public["approval_required"])
        self.assertFalse(public["receipt_verified"])
        self.assertTrue(public.get("guidance", "").strip())
        self.assertNotIn("next_action", public)
        self.assertFalse((self.manager.operations_dir /
                          (self.plan["operation_id"] + ".approval.json")).exists())
        self.assertFalse(self.fake.applied)
        fallback = self.manager.get(PROJECT, TARGET, self.plan["operation_id"])
        self.assertIn("approval_url", fallback)

    def test_wrong_scope_component_expiry_runtime_and_token_fail_closed(self):
        opened = self.open()
        args = self.args(opened)
        self.assert_code("UNSUPPORTED_SCOPE", lambda: self.cards.confirm(
            "other", TARGET, self.plan["operation_id"], **args))
        self.assert_code("SCHEMA014_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], card_id=args["card_id"],
            confirmation_token="b" * 64))
        self.runtime_identity = "b" * 64
        self.assert_code("SCHEMA014_CARD_STALE", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **args))

        state = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        state["immutable"]["component"] = "migration"
        from host_runtime_operations import _digest
        state["plan_digest"] = _digest(state["immutable"])
        self.manager._save(state)
        self.assert_code("SCHEMA014_PLAN_REQUIRED", lambda: self.cards.open(
            PROJECT, TARGET, self.plan["operation_id"]))

    def test_cancel_never_revokes_an_existing_receipt_or_executes(self):
        opened = self.open()
        private = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        from host_runtime_web import approve_web
        approved = approve_web(
            self.manager, operation_id=self.plan["operation_id"],
            plan_digest=private["plan_digest"],
            source_digest=private["immutable"]["source_digest"],
            confirmation=self.plan["operation_id"])
        result = self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened))
        self.assertEqual(result["structuredContent"]["state"], "approved")
        self.assertTrue(result["structuredContent"]["approved"])
        self.assertTrue(result["structuredContent"]["receipt_verified"])
        self.assertFalse(result["structuredContent"]["approval_required"])
        self.assertEqual(result["structuredContent"]["next_action"]["tool"],
                         "execute_schema014_migration")
        self.assertTrue(approved["approved"])
        self.assertTrue((self.manager.operations_dir /
                         (self.plan["operation_id"] + ".approval.json")).is_file())
        self.assertFalse(self.fake.applied)

    def test_admin_approval_and_card_cancel_are_linearized(self):
        opened = self.open()
        state = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        from host_runtime_web import approve_web

        def admin_approve():
            try:
                return approve_web(
                    self.manager, operation_id=self.plan["operation_id"],
                    plan_digest=state["plan_digest"],
                    source_digest=state["immutable"]["source_digest"],
                    confirmation=self.plan["operation_id"])["approved"]
            except HostRuntimeError as exc:
                return exc.code

        def card_cancel():
            try:
                return self.cards.cancel(
                    PROJECT, TARGET, self.plan["operation_id"], **self.args(opened)
                )["structuredContent"]
            except HostRuntimeError as exc:
                return {"error": exc.code}

        with ThreadPoolExecutor(max_workers=2) as pool:
            admin = pool.submit(admin_approve)
            cancelled = pool.submit(card_cancel)
            admin_result, cancel_result = admin.result(), cancelled.result()
        receipt = self.manager.operations_dir / (self.plan["operation_id"] + ".approval.json")
        if admin_result is True:
            self.assertTrue(receipt.is_file())
            self.assertEqual(cancel_result["state"], "approved")
            self.assertTrue(cancel_result["approved"])
        else:
            self.assertEqual(admin_result, "NOT_PENDING_APPROVAL")
            self.assertFalse(receipt.exists())
            self.assertEqual(cancel_result["state"], "cancelled")
            self.assertFalse(cancel_result["approved"])
        self.assertFalse(self.fake.applied)

    def test_admin_approval_winning_confirm_gap_returns_authoritative_approval(self):
        import host_operation_ui
        from host_runtime_web import approve_web
        opened = self.open()
        state = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        spent = Event()
        release = Event()
        original = host_operation_ui._save_private_json

        def pause_after_spent(path, value):
            original(path, value)
            if value.get("state") == "spent":
                spent.set()
                self.assertTrue(release.wait(2))

        with mock.patch.object(host_operation_ui, "_save_private_json",
                               side_effect=pause_after_spent):
            with ThreadPoolExecutor(max_workers=1) as pool:
                confirmed = pool.submit(
                    self.cards.confirm, PROJECT, TARGET, self.plan["operation_id"],
                    **self.args(opened))
                self.assertTrue(spent.wait(2))
                approved = approve_web(
                    self.manager, operation_id=self.plan["operation_id"],
                    plan_digest=state["plan_digest"],
                    source_digest=state["immutable"]["source_digest"],
                    confirmation=self.plan["operation_id"])
                release.set()
                result = confirmed.result()["structuredContent"]
        self.assertTrue(approved["approved"])
        self.assertEqual(result["state"], "approved")
        self.assertTrue(result["receipt_verified"])
        self.assertFalse(result["approval_required"])
        self.assertFalse(self.fake.applied)

    def test_reopen_after_admin_approval_returns_verified_state_without_new_capability(self):
        first = self.open()
        private = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        from host_runtime_web import approve_web
        approve_web(self.manager, operation_id=self.plan["operation_id"],
                    plan_digest=private["plan_digest"],
                    source_digest=private["immutable"]["source_digest"],
                    confirmation=self.plan["operation_id"])
        reopened = self.open()
        self.assertEqual(reopened["structuredContent"]["state"], "approved")
        self.assertTrue(reopened["structuredContent"]["receipt_verified"])
        self.assertFalse(reopened["structuredContent"]["approval_required"])
        self.assertNotIn("_meta", reopened)
        self.assert_code("SCHEMA014_CARD_DENIED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(first)))

    def test_open_holds_operation_lock_through_card_save(self):
        from host_runtime_web import approve_web
        entered = Event()
        release = Event()
        attempted = Event()
        original = self.cards._plan_locked

        def pause_inside_operation_lock(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)

        def admin_approve():
            attempted.set()
            state = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
            return approve_web(
                self.manager, operation_id=self.plan["operation_id"],
                plan_digest=state["plan_digest"],
                source_digest=state["immutable"]["source_digest"],
                confirmation=self.plan["operation_id"])

        with mock.patch.object(self.cards, "_plan_locked",
                               side_effect=pause_inside_operation_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                opened = pool.submit(self.open)
                self.assertTrue(entered.wait(2))
                admin = pool.submit(admin_approve)
                self.assertTrue(attempted.wait(2))
                self.assertFalse(admin.done(), "admin approval must wait for atomic card open")
                release.set()
                self.assertEqual(opened.result()["structuredContent"]["state"], "pending")
                self.assertTrue(admin.result()["approved"])
        self.assertEqual(self.open()["structuredContent"]["state"], "approved")

    def test_cancel_rejects_pending_plan_and_cannot_reopen_it(self):
        opened = self.open()
        cancelled = self.cards.cancel(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened))
        self.assertEqual(cancelled["structuredContent"]["state"], "cancelled")
        state = self.manager._load(PROJECT, TARGET, self.plan["operation_id"])
        self.assertEqual(state["state"], "rejected")
        self.assertEqual(state["code"], "USER_CANCELLED")
        self.assert_code("SCHEMA014_PLAN_REQUIRED", lambda: self.open())

    def test_response_loss_after_receipt_is_fail_closed_and_not_replayable(self):
        import host_operation_ui
        opened = self.open()
        original = host_operation_ui._save_private_json

        def lose_final_response(path, value):
            if value.get("state") == "approved":
                raise OSError("synthetic response loss")
            return original(path, value)

        with mock.patch.object(host_operation_ui, "_save_private_json",
                               side_effect=lose_final_response):
            with self.assertRaises(OSError):
                self.cards.confirm(PROJECT, TARGET, self.plan["operation_id"],
                                   **self.args(opened))
        self.assert_code("SCHEMA014_CARD_SPENT", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened)))
        self.assertTrue((self.manager.operations_dir /
                         (self.plan["operation_id"] + ".approval.json")).is_file())
        self.assertFalse(self.fake.applied)
        recovered = self.open()
        self.assertEqual(recovered["structuredContent"]["state"], "approved")
        self.assertTrue(recovered["structuredContent"]["receipt_verified"])
        self.assertNotIn("_meta", recovered)

    def test_expired_card_cannot_issue_a_receipt(self):
        from host_runtime_operations import _save_private_json
        opened = self.open()
        card = self.cards._load(self.plan["operation_id"])
        card["expires_at"] = "2000-01-01T00:00:00+00:00"
        _save_private_json(self.cards._path(self.plan["operation_id"]), card)
        self.assert_code("SCHEMA014_CARD_EXPIRED", lambda: self.cards.confirm(
            PROJECT, TARGET, self.plan["operation_id"], **self.args(opened)))
        self.assertFalse((self.manager.operations_dir /
                          (self.plan["operation_id"] + ".approval.json")).exists())
        self.assertFalse(self.fake.applied)

    def test_progressed_operation_never_reoffers_approval_or_execution(self):
        import copy
        from host_runtime_operations import utc_now
        baseline = self.manager._load(PROJECT, TARGET, self.plan['operation_id'])
        for status in ('queued', 'running', 'succeeded', 'failed', 'outcome_unknown'):
            with self.subTest(status=status):
                self.manager._save(copy.deepcopy(baseline))
                opened = self.open()
                state = copy.deepcopy(baseline)
                state.update(state=status, phase='EXECUTE', started_at=utc_now())
                if status in {'succeeded', 'failed', 'outcome_unknown'}:
                    state['finished_at'] = utc_now()
                self.manager._save(state)
                self.assert_code('SCHEMA014_OPERATION_STARTED', lambda: self.cards.cancel(
                    PROJECT, TARGET, self.plan['operation_id'], **self.args(opened)))
                self.assert_code('SCHEMA014_OPERATION_STARTED', self.open)
                self.assertFalse(self.fake.applied)
                self.assertFalse((self.manager.operations_dir /
                                  (self.plan['operation_id'] + '.approval.json')).exists())

    def test_concurrent_confirm_and_cancel_have_one_winner(self):
        opened = self.open()
        args = self.args(opened)

        def invoke(action):
            try:
                result = getattr(self.cards, action)(
                    PROJECT, TARGET, self.plan["operation_id"], **args)
                return result["structuredContent"]["state"]
            except HostRuntimeError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(invoke, ("confirm", "cancel")))
        self.assertIn("SCHEMA014_CARD_SPENT", outcomes)
        self.assertEqual(len(set(outcomes) & {"approved", "cancelled"}), 1)
        self.assertFalse(self.fake.applied)


if __name__ == "__main__":
    unittest.main()
