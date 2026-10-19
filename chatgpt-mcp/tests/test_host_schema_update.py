"""Fixed 006-013 schema update executor tests; all Docker and DB I/O is fake."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import host_db_migration
from host_runtime import (PREDECESSOR_FUNCTIONS, PREDECESSOR_INDEXES,
                          PREDECESSOR_NAMES, PREDECESSOR_TABLES, SCHEMA_013_TABLES)
from host_runtime_config import HostRuntimeError


GENERATION = {
    "database": "tarororo_test",
    "user": "tarororo",
    "postmasterStartEpoch": "123.456",
}


def identities():
    return {
        role: {
            "id": role + "-identity",
            "started_at": "2026-10-12T00:00:00Z",
            "image_id": role + "-image",
        }
        for role in ("api", "worker", "db")
    }


def source_fixture(root):
    folder = root / "server" / "migrations"
    folder.mkdir(parents=True)
    digests = {}
    for index, name in enumerate(PREDECESSOR_NAMES):
        prefix = "-- Durable credit packs and reading-scoped content unlocks\n" if index == 12 else ""
        raw = (prefix + "-- fixed schema update test source\nSELECT " + str(index + 1) + ";\n").encode()
        (folder / name).write_bytes(raw)
        digests[name] = hashlib.sha256(raw).hexdigest()
    return digests


class SchemaUpdateRuntime:
    def __init__(self, digests):
        self.digests = digests
        self.applied = False
        self.calls = []
        self.collision = False
        self.drift_generation = False
        self.postcheck_history_failure = False
        self.missing_postcheck_object = None

    def identities(self):
        return identities()

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        assert snapshot
        assert health

    def predecessor_status(self, project, target):
        assert (project, target) == ("tossApp", "tarororo-test")
        generation = dict(GENERATION)
        if self.drift_generation:
            generation["postmasterStartEpoch"] = "999.999"
        status = {
            "ready_to_prepare": not self.applied and not self.collision,
            "complete": False,
            "checksums": {
                name: self.digests[name] for name in PREDECESSOR_NAMES[5:12]
            },
            "generation": generation,
            "history": {
                name: ("missing" if self.postcheck_history_failure and index == 12 else
                       "checksum_match" if self.applied or index < 5 else "missing")
                for index, name in enumerate(PREDECESSOR_NAMES)
            },
            "unexpected_history_count": 0,
            "tables_present": {name: self.applied for name in PREDECESSOR_TABLES},
            "indexes_present": {name: self.applied for name in PREDECESSOR_INDEXES},
            "functions_present": {name: self.applied for name in PREDECESSOR_FUNCTIONS},
            "schema_013_tables_present": {
                name: self.applied for name in SCHEMA_013_TABLES
            },
        }
        if self.applied and self.missing_postcheck_object:
            key, kind = self.missing_postcheck_object
            if kind == "empty":
                status[key] = {}
            elif kind == "missing":
                status[key].pop(next(iter(status[key])))
            else:
                status[key][next(iter(status[key]))] = False
        return status

    def migration_status(self, project, target):
        assert (project, target) == ("tossApp", "tarororo-test")
        return {
            "state": "ready" if self.applied else "missing",
            "prerequisites_ok": self.applied,
            "tables": {name: self.applied for name in SCHEMA_013_TABLES},
        }

    def run(self, argv, *, capture=True, **kwargs):
        self.calls.append((argv, capture, kwargs))
        assert argv[:4] == ["docker", "exec", "-i", "tarororo-test-db-1"]
        assert argv[-2] == "-c"
        self.applied = True
        return b""


class SchemaUpdateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="schema-update-mcp-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.digests = source_fixture(self.root)

    def immutable(self):
        return {
            "identities": identities(),
            "database_generation": dict(GENERATION),
            "migration_checksums": {
                name: self.digests[name] for name in PREDECESSOR_NAMES
            },
            "migration_checksum": self.digests[PREDECESSOR_NAMES[12]],
        }

    @staticmethod
    def backup(output):
        content = b"PGDMP" + b"x" * 140
        output.write(content)
        return len(content), "a" * 64

    @staticmethod
    def restore(*args, **kwargs):
        return None

    def test_incomplete_indexes_or_functions_fail_postcheck_without_retry(self):
        scenarios = [(key, kind) for key in ("indexes_present", "functions_present")
                     for kind in ("false", "empty", "missing")]
        for index, scenario in enumerate(scenarios):
            with self.subTest(scenario=scenario):
                runtime = SchemaUpdateRuntime(self.digests)
                runtime.missing_postcheck_object = scenario
                with self.assertRaises(HostRuntimeError) as caught:
                    host_db_migration.migrate_schema_update(
                        runtime, self.root / "state", self.immutable(), self.root,
                        f"{index:032x}", backup_fn=self.backup,
                        validate_fn=lambda _: None, restore_fn=self.restore,
                    )
                self.assertEqual(caught.exception.code, "MIGRATION_POSTCHECK_FAILED")
                self.assertEqual(len(runtime.calls), 1)

    def test_schema_update_uses_one_backup_and_one_transaction_for_006_through_013(self):
        migrate = getattr(host_db_migration, "migrate_schema_update", None)
        self.assertTrue(callable(migrate), "migrate_schema_update is missing")
        runtime = SchemaUpdateRuntime(self.digests)
        steps = []

        def backup(output):
            steps.append("backup")
            content = b"PGDMP" + b"x" * 140
            output.write(content)
            return len(content), "a" * 64

        def validate(path):
            steps.append("validate")
            self.assertTrue(path.is_file())

        def restore(actual_runtime, path, operation_id, *, expected_history,
                    expected_checksums):
            steps.append("restore")
            self.assertIs(actual_runtime, runtime)
            self.assertEqual(operation_id, "a" * 32)
            self.assertEqual(expected_history, 5)
            self.assertEqual(
                expected_checksums,
                {name: self.digests[name] for name in PREDECESSOR_NAMES[:5]},
            )

        result = migrate(
            runtime,
            self.root / "state",
            self.immutable(),
            self.root,
            "a" * 32,
            backup_fn=backup,
            validate_fn=validate,
            restore_fn=restore,
        )

        self.assertEqual(steps, ["backup", "validate", "restore"])
        self.assertEqual(result, {"backup_sha256": "a" * 64, "backup_bytes": 145})
        self.assertEqual(len(runtime.calls), 1)
        argv, capture, kwargs = runtime.calls[0]
        self.assertFalse(capture)
        self.assertEqual(kwargs["timeout"], 240)
        self.assertIn("ON_ERROR_STOP=1", argv)
        self.assertEqual(argv[argv.index("-U") + 1], "tarororo")
        self.assertEqual(argv[argv.index("-d") + 1], "tarororo_test")
        statement = argv[-1]
        self.assertEqual(statement.count("COMMIT;"), 1)
        self.assertTrue(statement.rstrip().endswith("COMMIT;"))
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertIn("database generation mismatch", statement)
        self.assertIn("predecessor history mismatch", statement)
        self.assertIn("predecessor schema collision", statement)
        self.assertEqual(statement.count("INSERT INTO public.schema_migrations"), 8)
        positions = [statement.index("SELECT " + str(index) + ";")
                     for index in range(6, 14)]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[-1], statement.rindex("COMMIT;"))

    def test_backup_failure_prevents_schema_sql(self):
        runtime = SchemaUpdateRuntime(self.digests)

        def failed_backup(output):
            raise HostRuntimeError("BACKUP_FAILED")

        with self.assertRaisesRegex(HostRuntimeError, "BACKUP_FAILED"):
            host_db_migration.migrate_schema_update(
                runtime, self.root / "state", self.immutable(), self.root,
                "b" * 32, backup_fn=failed_backup,
                validate_fn=lambda path: None, restore_fn=self.restore,
            )

        self.assertEqual(runtime.calls, [])
        self.assertFalse(runtime.applied)

    def test_checksum_and_source_validation_refuse_changed_sql_before_backup(self):
        scenarios = []
        changed_predecessor = self.immutable()
        changed_predecessor["migration_checksums"] = dict(
            changed_predecessor["migration_checksums"]
        )
        changed_predecessor["migration_checksums"][PREDECESSOR_NAMES[5]] = "f" * 64
        scenarios.append(("predecessor", changed_predecessor,
                          "SCHEMA_NOT_SAFE_TO_MIGRATE"))

        changed_baseline = self.immutable()
        changed_baseline["migration_checksums"] = dict(
            changed_baseline["migration_checksums"]
        )
        changed_baseline["migration_checksums"][PREDECESSOR_NAMES[0]] = "d" * 64
        scenarios.append(("baseline", changed_baseline, "STALE_PLAN"))

        changed_013 = self.immutable()
        changed_013["migration_checksum"] = "e" * 64
        scenarios.append(("013-checksum", changed_013, "STALE_PLAN"))

        source = self.root / "server" / "migrations" / PREDECESSOR_NAMES[12]
        source.write_text("SELECT 13;\n")
        unsafe_013 = self.immutable()
        unsafe_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        unsafe_013["migration_checksums"] = dict(unsafe_013["migration_checksums"])
        unsafe_013["migration_checksums"][PREDECESSOR_NAMES[12]] = unsafe_digest
        unsafe_013["migration_checksum"] = unsafe_digest
        scenarios.append(("013-prefix", unsafe_013, "UNSAFE_MIGRATION_SOURCE"))

        for name, immutable, error in scenarios:
            with self.subTest(name=name):
                runtime = SchemaUpdateRuntime(self.digests)
                backups = []

                def observed_backup(output):
                    backups.append(True)
                    return self.backup(output)

                with self.assertRaisesRegex(HostRuntimeError, error):
                    host_db_migration.migrate_schema_update(
                        runtime, self.root / ("state-" + name), immutable, self.root,
                        ("c" if name == "predecessor" else
                         "4" if name == "baseline" else
                         "d" if name == "013-checksum" else "e") * 32,
                        backup_fn=observed_backup,
                        validate_fn=lambda path: None, restore_fn=self.restore,
                    )
                self.assertEqual(backups, [])
                self.assertEqual(runtime.calls, [])

    def test_generation_container_and_collision_checks_refuse_before_backup(self):
        for name in ("generation", "container", "collision"):
            with self.subTest(name=name):
                runtime = SchemaUpdateRuntime(self.digests)
                immutable = self.immutable()
                if name == "generation":
                    runtime.drift_generation = True
                elif name == "container":
                    immutable["identities"] = identities()
                    immutable["identities"]["db"]["id"] = "different-container"
                else:
                    runtime.collision = True
                backups = []

                def observed_backup(output):
                    backups.append(True)
                    return self.backup(output)

                expected = "STALE_PLAN" if name == "container" else "SCHEMA_NOT_SAFE_TO_MIGRATE"
                with self.assertRaisesRegex(HostRuntimeError, expected):
                    host_db_migration.migrate_schema_update(
                        runtime, self.root / ("state-" + name), immutable, self.root,
                        ("f" if name == "generation" else
                         "1" if name == "container" else "2") * 32,
                        backup_fn=observed_backup,
                        validate_fn=lambda path: None, restore_fn=self.restore,
                    )
                self.assertEqual(backups, [])
                self.assertEqual(runtime.calls, [])

    def test_postcheck_failure_is_reported_without_retry_or_restore(self):
        runtime = SchemaUpdateRuntime(self.digests)
        runtime.postcheck_history_failure = True
        restores = []

        def restore(*args, **kwargs):
            restores.append("before-sql")

        with self.assertRaisesRegex(HostRuntimeError, "MIGRATION_POSTCHECK_FAILED"):
            host_db_migration.migrate_schema_update(
                runtime, self.root / "state-postcheck", self.immutable(), self.root,
                "3" * 32, backup_fn=self.backup,
                validate_fn=lambda path: None, restore_fn=restore,
            )

        self.assertEqual(restores, ["before-sql"])
        self.assertEqual(len(runtime.calls), 1)
        self.assertTrue(runtime.applied)


if __name__ == "__main__":
    unittest.main()
