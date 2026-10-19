"""Schema 014 plans cannot inherit the existing 001-013 target grant.

All files, authorizations and tool calls in this test are synthetic and
confined to a temporary repo. No Docker or real database access.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_host_target_authorization import TargetAuthorizationFixture
from test_host_schema014 import FakeRuntime, REVIEWED_SQL
from host_runtime import PREDECESSOR_NAMES
from host_runtime_config import HostRuntimeError, PROJECT, TARGET


class Schema014ApprovalTest(TargetAuthorizationFixture):
    def manager_with_schema(self, suffix):
        path = self.root / "server/migrations/014_person_relationship_detail.sql"
        path.write_text(REVIEWED_SQL)
        manager, runtime = self.manager(suffix)
        checksums = {
            name: sha256((self.root / "server/migrations" / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES
        }
        fake = FakeRuntime(self.root, checksums)
        runtime.run = fake.run
        return manager, runtime, fake

    def test_existing_grant_cannot_authorize_schema014_without_a_separate_receipt(self):
        manager, _runtime, fake = self.manager_with_schema("014-isolated-grant")
        self.enable(manager)  # This authorizes only the historical 001-013 policy.
        plan = manager.prepare(PROJECT, TARGET, "schema014", "disabled", "schema014-approval")
        self.assertEqual(plan["component"], "schema014")
        self.assertEqual(plan["authorization"], {"mode": "per_operation"})
        self.assertEqual(plan["state"], "pending_host_approval")
        self.assertTrue(plan["approval_required"])
        self.assertIsNotNone(plan.get("approval_url"))
        self.assertEqual(plan["impact"]["schema_change"], "014_person_relationship_detail.sql")
        self.assertEqual(plan["migration_checksums"], fake.expected)
        self.assertTrue(any("014_person_relationship_detail.sql" in name
                            for name in plan["changed_paths"]))
        with self.assertRaises(HostRuntimeError):
            asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"],
                                        expected_component={"schema014"}))
        self.assertFalse(fake.applied)

    def test_schema014_uses_separate_exact_scope_tools_and_cannot_run_as_013(self):
        from host_runtime_tools import HOST_RUNTIME_TOOL_SPECS
        specs = {item["name"]: item for item in HOST_RUNTIME_TOOL_SPECS}
        names = ("get_schema014_migration_status",
                 "prepare_schema014_migration", "execute_schema014_migration")
        self.assertTrue(set(names).issubset(specs))
        for name in names:
            schema = specs[name]["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["project"]["enum"], ["tossApp"])
            self.assertEqual(schema["properties"]["target"]["enum"], ["tarororo-test"])
            self.assertNotIn("sql", schema["properties"])
        self.assertTrue(specs[names[0]]["annotations"]["readOnlyHint"])
        self.assertFalse(specs[names[1]]["annotations"]["readOnlyHint"])
        self.assertFalse(specs[names[2]]["annotations"]["readOnlyHint"])
        self.assertEqual(set(specs[names[1]]["inputSchema"]["required"]),
                         {"project", "target", "request_id"})
        self.assertEqual(set(specs[names[2]]["inputSchema"]["required"]),
                         {"project", "target", "operation_id"})
        manager, _runtime, fake = self.manager_with_schema("014-wrong-operation-type")
        plan = manager.prepare(PROJECT, TARGET, "schema014", "disabled", "014-wrong-tool")
        with self.assertRaisesRegex(HostRuntimeError, "WRONG_OPERATION_TYPE"):
            asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"],
                                        expected_component={"migration", "schema_update"}))
        self.assertFalse(fake.applied)

    def test_schema014_plan_rejected_from_unreviewed_legacy_scope(self):
        manager, _runtime, fake = self.manager_with_schema("014-legacy-rejection")
        data = __import__("json").loads(self.config.read_text())
        data["targets"][TARGET]["source_review"] = "git_head"
        self.write_config(data)
        with self.assertRaisesRegex(HostRuntimeError, "SCHEMA014_REVIEWED_SOURCE_REQUIRED"):
            manager.prepare(PROJECT, TARGET, "schema014", "disabled", "legacy014")
        self.assertFalse(fake.applied)

    def test_schema014_plan_never_allows_toss_demo_or_changed_sql(self):
        manager, _runtime, fake = self.manager_with_schema("014-invalid-plan")
        with self.assertRaisesRegex(HostRuntimeError, "UNSUPPORTED_SCOPE"):
            manager.prepare(PROJECT, TARGET, "schema014", "toss-demo", "wrong-mode")
        (self.root / "server/migrations/014_person_relationship_detail.sql").write_text(
            REVIEWED_SQL.replace("ADD COLUMN", "DROP COLUMN")
        )
        with self.assertRaisesRegex(HostRuntimeError, "SCHEMA014_SOURCE_NOT_APPROVED"):
            manager.prepare(PROJECT, TARGET, "schema014", "disabled", "unsafe-source")
        self.assertFalse(fake.applied)


if __name__ == "__main__":
    unittest.main()
