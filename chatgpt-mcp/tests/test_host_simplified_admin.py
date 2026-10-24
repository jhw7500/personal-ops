"""Reviewed-snapshot legacy approval UI boundary; no host effects."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import host_runtime_web  # noqa: E402
from admin_server import approval_link_valid  # noqa: E402
from host_runtime_config import HostRuntimeError, PROJECT, TARGET  # noqa: E402


class _Runtime:
    def __init__(self):
        self.guards = []

    def guard(self, project, target):
        self.guards.append((project, target))


class _Operations:
    def __init__(self, root):
        self.runtime = _Runtime()
        self.operations_dir = root / "operations"
        self.operations_dir.mkdir(mode=0o700)
        self.lock_path = root / "operations.lock"
        self.trusted = 0
        self.review_calls = []

    def _require_trusted_runtime(self):
        self.trusted += 1

    def planning_policy(self, project, target):
        self.runtime.guard(project, target)
        return {
            "mode": "reviewed_snapshot",
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "commit_required": False,
            "approval": "per_operation",
        }

    def review_artifact(self, project, target, operation_id):
        self.review_calls.append((project, target, operation_id))
        return {"version": 1, "git_head": "3" * 40, "source_digest": "f" * 64,
                "files": []}


class SimplifiedAdminWebTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.ops = _Operations(self.root)

    def write_pending(self):
        operation_id = "a" * 32
        source_review = {
            "policy": "reviewed_snapshot",
            "review_digest": "b" * 64,
            "config_digest": "c" * 64,
            "inventory_digest": "d" * 64,
        }
        state = {
            "operation_id": operation_id,
            "state": "pending_host_approval",
            "plan_digest": "e" * 64,
            "immutable": {
                "component": "predecessor_migration",
                "mode": "disabled",
                "source_digest": "f" * 64,
                "source_review": source_review,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "high_risk_changes": ["server/migrations/006_reading_memos.sql"],
                "changed_paths": ["server/migrations/006_reading_memos.sql"],
                "migration_checksums": {"006": "1" * 64},
                "database_generation": "2" * 64,
            },
        }
        path = self.ops.operations_dir / f"{operation_id}.json"
        path.write_text(json.dumps(state))
        path.chmod(0o600)
        return operation_id, source_review

    def test_pending_response_includes_policy_and_safe_review_binding(self):
        operation_id, source_review = self.write_pending()

        result = host_runtime_web.pending_operations(self.ops)

        self.assertEqual(result.get("planning_policy"), {
            "mode": "reviewed_snapshot",
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "commit_required": False,
            "approval": "per_operation",
        })
        self.assertEqual(result["plans"][0]["operation_id"], operation_id)
        self.assertEqual(result["plans"][0].get("source_review"), source_review)
        self.assertEqual(self.ops.trusted, 1)
        self.assertIn((PROJECT, TARGET), self.ops.runtime.guards)

    def test_review_reads_the_fixed_legacy_scope_without_executing(self):
        operation_id = "a" * 32
        review_reader = getattr(host_runtime_web, "host_operation_review", None)
        self.assertIsNotNone(review_reader, "authenticated legacy review reader is missing")
        if review_reader is None:
            return

        result = review_reader(self.ops, PROJECT, operation_id)

        self.assertEqual(result, {
            "project": PROJECT,
            "target": TARGET,
            "operation_id": operation_id,
            "review": {"version": 1, "git_head": "3" * 40,
                       "source_digest": "f" * 64, "files": []},
        })
        self.assertEqual(self.ops.review_calls, [(PROJECT, TARGET, operation_id)])

    def test_review_rejects_other_projects_and_invalid_operation_ids_before_read(self):
        for project, operation_id in (("redmine", "a" * 32), (PROJECT, "not-an-id")):
            with self.subTest(project=project, operation_id=operation_id):
                with self.assertRaises(HostRuntimeError) as raised:
                    host_runtime_web.host_operation_review(self.ops, project, operation_id)
                self.assertEqual(raised.exception.code, "HOST_APPROVAL_DENIED")
        self.assertEqual(self.ops.review_calls, [])

    def test_legacy_approval_link_is_exact_and_cannot_mix_registered_id(self):
        operation_id = "a" * 32
        legacy = ("project=tossApp&target=tarororo-test&host_operation_id="
                  + operation_id)
        self.assertTrue(approval_link_valid(legacy))
        for invalid in (
                legacy + "&operation_id=" + "b" * 32,
                legacy + "&token=secret",
                legacy.replace("tossApp", "redmine"),
                legacy.replace("tarororo-test", "other"),
                legacy.replace(operation_id, "invalid")):
            with self.subTest(invalid=invalid):
                self.assertFalse(approval_link_valid(invalid))


if __name__ == "__main__":
    unittest.main()
