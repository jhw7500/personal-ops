"""Target-scoped authorization contract; temporary fixtures never touch host services."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_operation_fixtures import commit_all, fixture_v3  # noqa: E402
from host_runtime import PREDECESSOR_NAMES  # noqa: E402
from host_runtime_config import HostRuntimeError, PROJECT, TARGET  # noqa: E402
from host_runtime_operations import HostRuntimeOperations  # noqa: E402


def identities():
    return {role: {"id": role + "-id", "started_at": "2026-10-14T00:00:00Z",
                   "image_id": role + "-image"}
            for role in ("api", "worker", "db")}


class AuthorizationRuntime:
    def __init__(self, root: Path, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.prerequisite_count = 12
        self.predecessor_ready = True
        self.migration_state = "missing"

    def guard(self, project, target):
        if (project, target) != (PROJECT, TARGET):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return self.root

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        if not snapshot:
            raise HostRuntimeError("CONTAINER_NOT_RUNNING")

    def identities(self):
        return identities()

    @staticmethod
    def http_health():
        return "ready"

    @staticmethod
    def readiness():
        return "ready"

    @staticmethod
    def compose_config(root, mode):
        raw = (Path(root) / "deploy/pc-test/compose.yaml").read_bytes() + mode.encode()
        return hashlib.sha256(raw).hexdigest()

    def migration_status(self, project, target):
        self.guard(project, target)
        return {
            "state": self.migration_state,
            "prerequisites_ok": self.prerequisite_count == 12,
            "prerequisites_matched": self.prerequisite_count,
            "tables": {"ad_attempts": False, "section_unlocks": False,
                       "credit_packs": False, "credit_redemptions": False},
        }

    def predecessor_status(self, project, target):
        root = self.guard(project, target)
        checksums = {
            name: hashlib.sha256((root / "server/migrations" / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES[5:12]
        }
        history = {name: "checksum_match" if index < 5 else "missing"
                   for index, name in enumerate(PREDECESSOR_NAMES)}
        return {
            "ready_to_prepare": self.predecessor_ready,
            "complete": False,
            "checksums": checksums,
            "history": history,
            "generation": {"database": "tarororo_test", "user": "tarororo",
                           "postmasterStartEpoch": "123.456"},
            "schema_013_tables_present": {
                "reading_ad_attempts": False,
                "reading_section_unlocks": False,
                "reading_credit_packs": False,
                "reading_credit_redemptions": False,
            },
        }

    @staticmethod
    def check_deploy_secrets():
        return None


class TargetAuthorizationFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="target-authorization-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.payload = fixture_v3(self.root)
        self.payload["targets"][TARGET]["source_review"] = "reviewed_snapshot"
        commit_all(self.root)
        self.config = self.home / "host-runtime.json"
        self.write_config(self.payload)

    def write_config(self, payload):
        self.config.write_text(json.dumps(payload))
        self.config.chmod(0o600)

    def manager(self, suffix="default"):
        runtime = AuthorizationRuntime(self.root, self.config)
        approval = self.home / "immutable" / "approve-host-operation.py"
        approval.parent.mkdir(exist_ok=True)
        approval.touch(exist_ok=True)
        manager = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / ("state-" + suffix),
            config_path=self.config, runtime=runtime, approval_script=approval,
        )
        return manager, runtime

    def enable(self, manager):
        return manager.set_target_authorization(
            PROJECT, TARGET, enabled=True, confirmation=PROJECT + "/" + TARGET)


class TargetAuthorizationTest(TargetAuthorizationFixture):
    def test_grant_requires_exact_scope_and_is_private_idempotent_and_revocable(self):
        manager, _runtime = self.manager("lifecycle")
        with self.assertRaisesRegex(HostRuntimeError, "HOST_APPROVAL_DENIED"):
            manager.set_target_authorization(
                PROJECT, TARGET, enabled=True, confirmation="tossApp/wrong")
        first = self.enable(manager)
        self.assertTrue(first["supported"])
        self.assertTrue(first["enabled"])
        self.assertRegex(first["grant_id"], r"^[0-9a-f]{32}$")
        policy = manager.planning_policy(PROJECT, TARGET)
        self.assertEqual(policy["approval"], "target")
        self.assertEqual(policy["target_authorization"], first)
        grant_file = next((manager.state_dir / "target-authorizations").iterdir())
        self.assertEqual(grant_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.enable(manager)["grant_id"], first["grant_id"])

        disabled = manager.set_target_authorization(
            PROJECT, TARGET, enabled=False, confirmation=PROJECT + "/" + TARGET)
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["grant_id"])
        second = self.enable(manager)
        self.assertNotEqual(second["grant_id"], first["grant_id"])

    def test_legacy_and_non_reviewed_targets_are_unsupported(self):
        manager, _runtime = self.manager("unsupported")
        payload = json.loads(self.config.read_text())
        payload["targets"][TARGET]["source_review"] = "git_head"
        self.write_config(payload)
        status = manager.target_authorization(PROJECT, TARGET)
        self.assertEqual(status, {
            "supported": False, "enabled": False, "project": PROJECT, "target": TARGET,
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "grant_id": None, "granted_at": None, "reason": "UNSUPPORTED_POLICY",
        })

    def test_target_plan_pins_grant_and_executes_without_operation_receipt(self):
        manager, _runtime = self.manager("execute")
        grant = self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "target-api")
        self.assertEqual(plan["authorization"], {
            "mode": "target", "grant_id": grant["grant_id"],
            "config_digest": plan["source_review"]["config_digest"],
        })
        self.assertNotIn("approval_url", plan)
        self.assertFalse((manager.operations_dir /
                          (plan["operation_id"] + ".approval.json")).exists())

        original = manager._run_deploy
        manager._run_deploy = lambda _operation_id, fd: __import__("os").close(fd)
        try:
            queued = asyncio.run(manager.execute(
                PROJECT, TARGET, plan["operation_id"], expected_component={"api", "worker"}))
        finally:
            manager._run_deploy = original
        self.assertEqual(queued["state"], "queued")

    def test_grant_added_after_plan_does_not_authorize_it(self):
        manager, _runtime = self.manager("retroactive")
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "old-api")
        self.assertEqual(plan["authorization"], {"mode": "per_operation"})
        self.enable(manager)
        with self.assertRaises(HostRuntimeError):
            asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"]))

    def test_revoked_rotated_and_config_changed_grants_block_pinned_plan(self):
        for case in ("revoked", "rotated", "config"):
            with self.subTest(case=case):
                manager, _runtime = self.manager(case)
                self.enable(manager)
                plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "plan-" + case)
                if case in ("revoked", "rotated"):
                    manager.set_target_authorization(
                        PROJECT, TARGET, enabled=False,
                        confirmation=PROJECT + "/" + TARGET)
                if case == "rotated":
                    self.enable(manager)
                elif case == "config":
                    payload = json.loads(self.config.read_text())
                    payload["targets"][TARGET]["readiness"] = None
                    self.write_config(payload)
                with self.assertRaisesRegex(HostRuntimeError, "TARGET_AUTHORIZATION_INVALID"):
                    asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"]))

    def test_sql_source_change_invalidates_whole_grant_until_explicit_reauthorization(self):
        manager, _runtime = self.manager("sql-change")
        first = self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "before-sql-change")
        migration = self.root / "server/migrations" / PREDECESSOR_NAMES[5]
        migration.write_text("\\! touch /tmp/must-never-run\n")

        status = manager.target_authorization(PROJECT, TARGET)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["reason"], "MIGRATION_SOURCE_CHANGED")
        with self.assertRaisesRegex(HostRuntimeError, "TARGET_AUTHORIZATION_INVALID"):
            asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"]))

        second = self.enable(manager)
        self.assertNotEqual(second["grant_id"], first["grant_id"])
        self.assertTrue(second["enabled"])
        migration.unlink()
        self.assertEqual(manager.target_authorization(PROJECT, TARGET)["reason"],
                         "MIGRATION_SOURCE_CHANGED")

    def test_revoke_cannot_return_between_final_grant_check_and_queue_acceptance(self):
        manager, _runtime = self.manager("atomic-acceptance")
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "atomic-api")
        final_check_entered = threading.Event()
        allow_queue = threading.Event()
        order = []
        original_verify = manager._verify_target_authorization
        original_save = manager._save
        checks = 0

        def verify(immutable):
            nonlocal checks
            result = original_verify(immutable)
            checks += 1
            if checks == 2:
                final_check_entered.set()
                self.assertTrue(allow_queue.wait(5))
            return result

        def save(state):
            if state.get("state") == "queued":
                order.append("queued")
            return original_save(state)

        manager._verify_target_authorization = verify
        manager._save = save
        manager._run_deploy = lambda _operation_id, fd: __import__("os").close(fd)
        execute_result = []

        def execute():
            execute_result.append(asyncio.run(manager.execute(
                PROJECT, TARGET, plan["operation_id"], expected_component={"api", "worker"})))

        def revoke():
            manager.set_target_authorization(
                PROJECT, TARGET, enabled=False, confirmation=PROJECT + "/" + TARGET)
            order.append("revoked")

        execute_thread = threading.Thread(target=execute)
        revoke_thread = threading.Thread(target=revoke)
        execute_thread.start()
        self.assertTrue(final_check_entered.wait(5))
        revoke_thread.start()
        try:
            revoke_thread.join(0.1)
            self.assertTrue(revoke_thread.is_alive())
        finally:
            allow_queue.set()
        execute_thread.join(5)
        revoke_thread.join(5)
        self.assertFalse(execute_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        self.assertEqual(order, ["queued", "revoked"])
        self.assertEqual(execute_result[0]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
