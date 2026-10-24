"""Automatic 006-013 plan selection under an active fixed-target authorization."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_runtime import PREDECESSOR_NAMES  # noqa: E402
from host_runtime_config import HostRuntimeError, PROJECT, TARGET  # noqa: E402
from test_host_target_authorization import TargetAuthorizationFixture  # noqa: E402


class AutoOperationTest(TargetAuthorizationFixture):
    def test_db_prepare_combines_missing_006_through_013_under_target_grant(self):
        manager, runtime = self.manager("combined")
        runtime.prerequisite_count = 5
        grant = self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "migration", "disabled", "combined-db")

        self.assertEqual(plan["component"], "schema_update")
        self.assertEqual(plan["requested_component"], "migration")
        self.assertEqual(plan["migration_range"], ["006", "013"])
        self.assertEqual(plan["authorization"]["grant_id"], grant["grant_id"])
        expected = {
            name: hashlib.sha256((self.root / "server/migrations" / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES
        }
        self.assertEqual(plan["migration_checksums"], expected)
        manager.set_target_authorization(
            PROJECT, TARGET, enabled=False, confirmation=PROJECT + "/" + TARGET)
        runtime.prerequisite_count = 12
        runtime.predecessor_ready = False
        runtime.migration_state = "ready"
        self.assertEqual(
            manager.prepare(PROJECT, TARGET, "migration", "disabled", "combined-db")
            ["operation_id"], plan["operation_id"])

    def test_per_operation_plan_does_not_auto_expand_to_predecessors(self):
        manager, runtime = self.manager("legacy-behaviour")
        runtime.prerequisite_count = 5
        with self.assertRaisesRegex(HostRuntimeError, "SCHEMA_NOT_SAFE_TO_MIGRATE"):
            manager.prepare(PROJECT, TARGET, "migration", "disabled", "no-auto")

    def test_combined_plan_revalidates_revocation_before_database_effects(self):
        manager, runtime = self.manager("revalidate")
        runtime.prerequisite_count = 5
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "migration", "disabled", "combined-run")
        state = manager._load(PROJECT, TARGET, plan["operation_id"])
        state.update(state="queued", phase="QUEUED")
        manager._save(state)
        manager.set_target_authorization(
            PROJECT, TARGET, enabled=False, confirmation=PROJECT + "/" + TARGET)

        with patch("host_db_migration.migrate_schema_update") as migrate:
            manager._run_deploy_body(plan["operation_id"])
        migrate.assert_not_called()
        result = manager.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["code"], "TARGET_AUTHORIZATION_INVALID")

    def test_execute_db_contract_accepts_combined_schema_update(self):
        manager, runtime = self.manager("execute-type")
        runtime.prerequisite_count = 5
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "migration", "disabled", "combined-type")
        original = manager._run_deploy
        manager._run_deploy = lambda _operation_id, fd: __import__("os").close(fd)
        try:
            result = asyncio.run(manager.execute(
                PROJECT, TARGET, plan["operation_id"],
                expected_component={"migration", "schema_update"}))
        finally:
            manager._run_deploy = original
        self.assertEqual(result["state"], "queued")


if __name__ == "__main__":
    unittest.main()
