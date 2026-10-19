"""Chat-facing approval guidance uses current grants, never an obsolete pending label."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_runtime_config import PROJECT, TARGET  # noqa: E402
from host_runtime_operations import _digest  # noqa: E402
from test_host_target_authorization import TargetAuthorizationFixture  # noqa: E402


class ChatApprovalTest(TargetAuthorizationFixture):
    def assert_ready(self, plan, tool):
        self.assertEqual(plan["state"], "ready_to_execute")
        self.assertEqual(plan["phase"], "READY")
        self.assertFalse(plan["approval_required"])
        self.assertEqual(plan["next_action"], {
            "tool": tool,
            "arguments": {"project": PROJECT, "target": TARGET,
                          "operation_id": plan["operation_id"]},
        })
        self.assertNotIn("approval_url", plan)
        self.assertNotIn("approval_command", plan)

    def test_authorized_api_and_worker_show_execution_instead_of_web_approval(self):
        for component in ("api", "worker"):
            with self.subTest(component=component):
                manager, _runtime = self.manager(component)
                self.enable(manager)
                plan = manager.prepare(PROJECT, TARGET, component, "disabled", component)
                self.assert_ready(plan, "execute_test_deploy")
                self.assert_ready(manager.get(PROJECT, TARGET, plan["operation_id"]),
                                  "execute_test_deploy")

    def test_each_schema_plan_points_to_its_correct_execute_tool(self):
        for component, prerequisites, tool in (
                ("predecessor_migration", 5, "execute_predecessor_migration"),
                ("migration", 12, "execute_db_migration"),
                ("migration", 5, "execute_db_migration")):
            with self.subTest(component=component, prerequisites=prerequisites):
                manager, runtime = self.manager(component + str(prerequisites))
                runtime.prerequisite_count = prerequisites
                self.enable(manager)
                plan = manager.prepare(PROJECT, TARGET, component, "disabled", "schema")
                self.assert_ready(plan, tool)

    def test_enabling_later_does_not_remove_an_old_plans_required_approval(self):
        manager, _runtime = self.manager("old-plan")
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "old-plan")
        self.enable(manager)
        result = manager.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "pending_host_approval")
        self.assertIn("approval_required", result)
        self.assertTrue(result["approval_required"])
        self.assertIn("approval_url", result)
        self.assertNotIn("next_action", result)

    def test_revoked_or_rotated_grant_never_tells_chat_to_execute(self):
        for rotate in (False, True):
            with self.subTest(rotate=rotate):
                manager, _runtime = self.manager("revoked" + str(rotate))
                self.enable(manager)
                plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "plan")
                manager.set_target_authorization(
                    PROJECT, TARGET, enabled=False, confirmation=PROJECT + "/" + TARGET)
                if rotate:
                    self.enable(manager)
                result = manager.get(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["code"], "TARGET_AUTHORIZATION_INVALID")
                self.assertNotIn("next_action", result)

    def test_changed_sql_never_tells_chat_to_execute(self):
        manager, _runtime = self.manager("changed-sql")
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "plan")
        sql = self.root / "server/migrations/013_staged_reading_access.sql"
        sql.write_text(sql.read_text() + "\n-- changed after grant\n")
        result = manager.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["code"], "TARGET_AUTHORIZATION_INVALID")
        self.assertNotIn("next_action", result)

    def test_expired_idempotent_retry_explains_expiry_instead_of_approval(self):
        manager, _runtime = self.manager("expired")
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "retry")
        state = manager._load(PROJECT, TARGET, plan["operation_id"])
        state["immutable"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        state["plan_digest"] = _digest(state["immutable"])
        manager._save(state)
        retry = manager.prepare(PROJECT, TARGET, "api", "disabled", "retry")
        self.assertEqual(retry["state"], "expired")
        self.assertEqual(retry["code"], "PLAN_EXPIRED")
        self.assertFalse(retry["approval_required"])
        self.assertNotIn("next_action", retry)

    def test_completed_and_running_operations_do_not_advertise_an_execute_call(self):
        manager, _runtime = self.manager("terminal")
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "terminal")
        for status in ("queued", "running", "succeeded", "failed", "outcome_unknown"):
            with self.subTest(status=status):
                state = manager._load(PROJECT, TARGET, plan["operation_id"])
                state.update(state=status, phase=status.upper())
                manager._save(state)
                result = manager.get_without_lock(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(result["state"], status)
                self.assertIn("approval_required", result)
                self.assertFalse(result["approval_required"])
                self.assertNotIn("next_action", result)


if __name__ == "__main__":
    unittest.main()
