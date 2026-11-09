"""New offline-only Schema 015+016 migration safety tests.

Synthetic temp files and a fake Docker runner. Never contacts a real DB.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host_runtime import PREDECESSOR_NAMES
from host_runtime_config import HostRuntimeError
from host_schema014 import FIXED_NAME
from host_schema015016 import FIXED_SOURCES, OBJECTS, migrate_schema015016
from tests.test_host_schema014 import REVIEWED_SQL
from tests.test_host_schema015016_status import (
    FakeRuntime, GENERATION, SQL_FILES,
)


class NewSchemaMigrationSafetyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="new-schema015016-safety-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "tossApp"
        migrations = self.root / "server/migrations"
        migrations.mkdir(parents=True)
        for n, name in enumerate(PREDECESSOR_NAMES):
            (migrations / name).write_bytes(f"-- fixture {n}\nSELECT 1;\n".encode())
        (migrations / FIXED_NAME).write_text(REVIEWED_SQL)
        for name, sql in SQL_FILES.items():
            (migrations / name).write_text(sql)
        self.checksums = {
            name: sha256((migrations / name).read_bytes()).hexdigest()
            for name in (*PREDECESSOR_NAMES, FIXED_NAME, *FIXED_SOURCES)
        }
        self.runtime = FakeRuntime(self.root, self.checksums)
        self.raw_fake_run = self.runtime.run
        self.events = []
        self.migration_sql = None
        self.runtime.run = self.intercept_run
        self.immutable = {
            "identities": self.runtime.identities(),
            "authorization": {"mode": "per_operation"},
            "source_review": {"policy": "reviewed_snapshot"},
            "migration_checksums": {
                name: self.checksums[name] for name in (*PREDECESSOR_NAMES, FIXED_NAME)
            },
            "migration015016_checksums": {
                name: self.checksums[name] for name in FIXED_SOURCES
            },
            "database_generation": dict(GENERATION),
        }
        self.operation_id = "c" * 32

    def intercept_run(self, argv, **kwargs):
        sql = argv[-1]
        if "BEGIN READ ONLY;" in sql:
            self.events.append("readonly")
            return self.raw_fake_run(argv, **kwargs)
        self.events.append("live_ddl")
        self.assertIsNone(self.migration_sql)
        self.migration_sql = sql
        self.assertEqual(
            argv[:4], ["docker", "exec", "-i", "tarororo-test-db-1"])
        self.assertEqual(argv[-3:-1], ["tarororo_test", "-c"])
        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertTrue(sql.endswith("COMMIT;"))
        self.assertEqual(sql.count("CREATE TABLE "), 2)
        self.assertEqual(sql.count("CREATE UNIQUE INDEX "), 1)
        self.assertEqual(sql.count("CREATE INDEX "), 1)
        self.assertEqual(
            sql.count("INSERT INTO public.schema_migrations("), 2)
        self.assertIn("schema 014 history mismatch", sql)
        self.assertIn("schema 015/016 object collision", sql)
        self.assertIn("SET LOCAL lock_timeout='5s';", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        for name, checksum in self.immutable["migration015016_checksums"].items():
            self.assertIn(name, sql)
            self.assertIn(checksum, sql)
        self.assertLess(sql.index(SQL_FILES["015_public_demo_video_unlocks.sql"]),
                        sql.index(SQL_FILES["016_rewarded_ad_attempts.sql"]))
        self.runtime.applied.update(FIXED_SOURCES)
        self.runtime.present.update(OBJECTS)
        return b""

    def backup(self, output):
        self.events.append("backup")
        payload = b"PGDMP synthetic archive"
        output.write(payload)
        return len(payload), sha256(payload).hexdigest()

    def validate(self, archive):
        self.events.append("validate")
        self.assertTrue(Path(archive).read_bytes().startswith(b"PGDMP"))

    def restore(self, runtime, archive, operation_id, *,
                expected_history, expected_checksums):
        self.events.append("restore")
        self.assertIs(runtime, self.runtime)
        self.assertEqual(operation_id, self.operation_id)
        self.assertEqual(expected_history, 14)
        self.assertEqual(expected_checksums, self.immutable["migration_checksums"])

    def migrate(self):
        return migrate_schema015016(
            self.runtime, self.base / "state", self.immutable,
            self.root, self.operation_id,
            backup_fn=self.backup, validate_fn=self.validate,
            restore_fn=self.restore,
        )

    def test_backup_and_disposable_restore_precede_both_ddl_in_one_transaction(self):
        outcome = self.migrate()
        self.assertIsNotNone(self.migration_sql)
        self.assertEqual(
            [event for event in self.events if event != "readonly"],
            ["backup", "validate", "restore", "live_ddl"])
        self.assertEqual(outcome["backup_bytes"], len(b"PGDMP synthetic archive"))
        self.assertEqual(outcome["backup_sha256"],
                         sha256(b"PGDMP synthetic archive").hexdigest())
        self.assertTrue(all(name in self.runtime.applied for name in FIXED_SOURCES))
        self.assertTrue(all(name in self.runtime.present for name in OBJECTS))

    def test_reject_old_target_grant_and_unreviewed_snapshot_without_any_sql(self):
        for invalid in ("inherited_grant", "unreviewed"):
            with self.subTest(invalid=invalid):
                self.events.clear()
                if invalid == "inherited_grant":
                    self.immutable["authorization"] = {
                        "mode": "target", "grant_id": "0" * 32}
                else:
                    self.immutable["authorization"] = {"mode": "per_operation"}
                    self.immutable["source_review"] = {"policy": "git_head"}
                with self.assertRaises(HostRuntimeError) as error:
                    self.migrate()
                self.assertEqual(error.exception.code,
                                 "SCHEMA015016_APPROVAL_REQUIRED")
                self.assertEqual(self.events, [])

    def test_changed_hash_and_history_collision_fail_before_backup(self):
        self.immutable["migration015016_checksums"]["015_public_demo_video_unlocks.sql"] = "0" * 64
        with self.assertRaisesRegex(HostRuntimeError, "STALE_PLAN"):
            self.migrate()
        self.assertNotIn("backup", self.events)
        self.assertNotIn("live_ddl", self.events)
        self.immutable["migration015016_checksums"] = {
            name: self.checksums[name] for name in FIXED_SOURCES
        }
        self.events.clear()
        self.runtime.other_history = 1
        with self.assertRaisesRegex(HostRuntimeError, "SCHEMA_NOT_SAFE_TO_MIGRATE"):
            self.migrate()
        self.assertNotIn("backup", self.events)
        self.assertNotIn("live_ddl", self.events)

    def test_failed_disposable_restore_never_starts_live_ddl(self):
        def bad_restore(*a, **kw):
            self.events.append("restore_fail")
            raise HostRuntimeError("BACKUP_RESTORE_FAILED")
        with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_FAILED"):
            migrate_schema015016(
                self.runtime, self.base / "state", self.immutable,
                self.root, self.operation_id,
                backup_fn=self.backup, validate_fn=self.validate,
                restore_fn=bad_restore)
        self.assertNotIn("live_ddl", self.events)

    def test_tool_specs_reject_arbitrary_sql_and_have_separate_execute(self):
        from host_runtime_tools import HOST_RUNTIME_TOOL_SPECS
        specs = {item["name"]: item for item in HOST_RUNTIME_TOOL_SPECS}
        names = ("get_schema015016_migration_status",
                 "prepare_schema015016_migration",
                 "execute_schema015016_migration")
        for name in names:
            schema = specs[name]["inputSchema"]
            self.assertEqual(schema["properties"]["project"]["enum"], ["tossApp"])
            self.assertEqual(schema["properties"]["target"]["enum"], ["tarororo-test"])
            self.assertFalse(schema["additionalProperties"])
            self.assertNotIn("sql", schema["properties"])
        self.assertEqual(
            set(specs[names[1]]["inputSchema"]["required"]),
            {"project", "target", "request_id"})
        self.assertEqual(
            set(specs[names[2]]["inputSchema"]["required"]),
            {"project", "target", "operation_id"})
        self.assertTrue(specs[names[0]]["annotations"]["readOnlyHint"])
        self.assertFalse(specs[names[1]]["annotations"]["readOnlyHint"])
        self.assertFalse(specs[names[2]]["annotations"]["readOnlyHint"])
        resume = specs["get_host_operation"]["description"]
        self.assertIn("schema015016", resume)
        self.assertIn("open_test_plan_approval", resume)

    def test_disposable_restore_checks_all_fourteen_predecessors(self):
        import json
        import subprocess
        from host_db_migration import _restore_check

        history = self.immutable["migration_checksums"]
        calls = []

        class RestoreRuntime:
            def run(self, argv, **kwargs):
                calls.append(argv)
                return (json.dumps(history) + "\n").encode() if "psql" in argv else b""

        with tempfile.NamedTemporaryFile(dir=self.base) as archive:
            archive.write(b"PGDMP-offline-fixture")
            archive.flush()
            with patch("host_db_migration.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0)) as restore:
                _restore_check(RestoreRuntime(), Path(archive.name), self.operation_id,
                               expected_history=14, expected_checksums=history)
            self.assertIn("createdb", calls[0])
            self.assertIn("dropdb", calls[-1])
            self.assertIn("tarororo_restore_" + self.operation_id,
                          restore.call_args.args[0])
            self.assertNotIn("tarororo_test", restore.call_args.args[0])



class NewSchemaManagerApprovalTest(__import__("tests.test_host_target_authorization",
                                               fromlist=["TargetAuthorizationFixture"]).TargetAuthorizationFixture):
    def manager_with_sources(self, suffix):
        directory = self.root / "server/migrations"
        (directory / FIXED_NAME).write_text(REVIEWED_SQL)
        for name, sql in SQL_FILES.items():
            (directory / name).write_text(sql)
        manager, runtime = self.manager(suffix)
        checksums = {
            name: sha256((directory / name).read_bytes()).hexdigest()
            for name in (*PREDECESSOR_NAMES, FIXED_NAME, *FIXED_SOURCES)
        }
        fake = FakeRuntime(self.root, checksums)
        runtime.run = fake.run
        return manager, runtime, fake

    def test_web_fallback_reports_exact_database_effect_before_receipt(self):
        from host_runtime_config import PROJECT, TARGET
        from host_runtime_web import approve_web, pending_operations

        manager, _runtime, fake = self.manager_with_sources("015016-web-impact")
        plan = manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                               "015016-web-impact-plan")
        displayed = next(p for p in pending_operations(manager)["plans"]
                         if p["operation_id"] == plan["operation_id"])
        self.assertIsNone(displayed["impact"]["recreate"])
        self.assertEqual(displayed["impact"]["preserve"], ["api", "worker"])
        self.assertEqual(displayed["impact"]["schema_change"], "015+016_fixed_sources")
        self.assertEqual(displayed["migration_range"], ["015", "016"])
        for field in ("migration_checksums", "migration015016_checksums", "database_generation"):
            self.assertEqual(displayed[field], plan[field])
        receipt = manager.operations_dir / (plan["operation_id"] + ".approval.json")
        self.assertFalse(receipt.exists())
        with self.assertRaisesRegex(HostRuntimeError, "HOST_APPROVAL_DENIED"):
            approve_web(manager, operation_id=plan["operation_id"],
                        plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
                        confirmation="wrong-operation")
        self.assertFalse(receipt.exists())
        approved = approve_web(manager, operation_id=plan["operation_id"],
                               plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
                               confirmation=plan["operation_id"])
        self.assertTrue(approved["approved"])
        self.assertTrue(receipt.exists())
        self.assertEqual(fake.applied, set())

    def test_real_manager_consumes_receipt_and_records_one_fake_transaction(self):
        import asyncio
        from host_runtime_config import PROJECT, TARGET
        from host_test_approval_ui import TestPlanApprovals

        manager, runtime, fake = self.manager_with_sources("015016-worker")
        plan = manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                               "015016-real-manager-plan")
        cards = TestPlanApprovals(manager, trusted_runtime=lambda: "a" * 64)
        opened = cards.open(PROJECT, TARGET, plan["operation_id"])
        cards.confirm(PROJECT, TARGET, plan["operation_id"],
                      card_id=opened["structuredContent"]["card_id"],
                      confirmation_token=opened["_meta"]["test_plan_confirmation"])
        receipt = manager.operations_dir / (plan["operation_id"] + ".approval.json")
        self.assertTrue(receipt.exists())
        original_run = runtime.run
        writes = []

        def fake_run(argv, **kwargs):
            if "BEGIN READ ONLY;" in argv[-1]:
                return original_run(argv, **kwargs)
            self.assertIn("$schema015016_guard$", argv[-1])
            writes.append(argv[-1])
            fake.applied.update(FIXED_SOURCES)
            fake.present.update(OBJECTS)
            return b""

        def fake_backup(output):
            output.write(b"PGDMP-offline-fixture")
            return 20, "d" * 64

        async def execute_and_wait():
            queued = await manager.execute(PROJECT, TARGET, plan["operation_id"],
                                           expected_component={"schema015016"})
            await asyncio.gather(*tuple(manager._tasks))
            return queued

        runtime.run = fake_run
        with patch("host_db_migration._run_backup", side_effect=fake_backup), \
                patch("host_db_migration._validate_archive"), \
                patch("host_db_migration._restore_check") as restore:
            queued = asyncio.run(execute_and_wait())
        self.assertEqual(queued["state"], "queued")
        state = manager._load(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["phase"], "COMPLETE")
        self.assertEqual(state["backup_sha256"], "d" * 64)
        self.assertEqual(state["backup_bytes"], 20)
        self.assertFalse(receipt.exists())
        self.assertEqual(len(writes), 1)
        self.assertEqual(restore.call_args.kwargs["expected_history"], 14)

    def test_reviewed_015016_plan_requires_new_receipt_despite_existing_grant(self):
        import asyncio
        from host_runtime_config import PROJECT, TARGET
        manager, _runtime, fake = self.manager_with_sources("015016-new-approval")
        self.enable(manager)
        plan = manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                               "reviewed-015016-plan")
        self.assertEqual(plan["state"], "pending_host_approval")
        self.assertTrue(plan["approval_required"])
        self.assertEqual(plan["authorization"], {"mode": "per_operation"})
        self.assertEqual(plan["impact"]["schema_change"], "015+016_fixed_sources")
        self.assertEqual(plan["migration_range"], ["015", "016"])
        self.assertEqual(plan["migration015016_checksums"], {
            name: fake.expected[name] for name in FIXED_SOURCES
        })
        self.assertEqual(len(plan["migration_checksums"]), 14)
        with self.assertRaises(HostRuntimeError):
            asyncio.run(manager.execute(
                PROJECT, TARGET, plan["operation_id"],
                expected_component={"schema015016"}))
        self.assertEqual(fake.applied, set())
        self.assertEqual(fake.present, set())


    def test_new_015016_card_issues_one_use_receipt_but_does_not_execute(self):
        from host_runtime_config import PROJECT, TARGET
        from host_test_approval_ui import TestPlanApprovals
        manager, _runtime, fake = self.manager_with_sources("015016-card")
        plan = manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                               "015016-one-use-approval")
        cards = TestPlanApprovals(manager, trusted_runtime=lambda: "a" * 64)
        opened = cards.open(PROJECT, TARGET, plan["operation_id"])
        public = opened["structuredContent"]
        self.assertEqual(public["component"], "schema015016")
        self.assertTrue(public["approval_required"])
        self.assertFalse(public["executes_on_approval"])
        token = opened["_meta"]["test_plan_confirmation"]
        self.assertNotIn(token, str(public))
        approved = cards.confirm(
            PROJECT, TARGET, plan["operation_id"],
            card_id=public["card_id"], confirmation_token=token)
        verified = approved["structuredContent"]
        self.assertTrue(verified["receipt_verified"])
        self.assertEqual(verified["next_action"]["tool"],
                         "execute_schema015016_migration")
        self.assertEqual(fake.applied, set())
        self.assertEqual(fake.present, set())
        with self.assertRaises(HostRuntimeError):
            cards.confirm(PROJECT, TARGET, plan["operation_id"],
                          card_id=public["card_id"], confirmation_token=token)

    def test_015016_plan_rejects_old_tool_type_and_unreviewed_target(self):
        import asyncio
        import json
        from host_runtime_config import PROJECT, TARGET
        manager, _runtime, fake = self.manager_with_sources("015016-not-old-013")
        plan = manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                               "only-new-015016-tool")
        with self.assertRaisesRegex(HostRuntimeError, "WRONG_OPERATION_TYPE"):
            asyncio.run(manager.execute(
                PROJECT, TARGET, plan["operation_id"],
                expected_component={"migration", "schema_update", "schema014"}))
        self.assertEqual(fake.applied, set())
        other_manager, _runtime, _fake = self.manager_with_sources("015016-unreviewed")
        data = json.loads(self.config.read_text())
        data["targets"][TARGET]["source_review"] = "git_head"
        self.write_config(data)
        with self.assertRaisesRegex(
                HostRuntimeError, "SCHEMA015016_REVIEWED_SOURCE_REQUIRED"):
            other_manager.prepare(PROJECT, TARGET, "schema015016", "disabled",
                                  "unreviewed-source-must-fail")

if __name__ == "__main__":
    unittest.main()
