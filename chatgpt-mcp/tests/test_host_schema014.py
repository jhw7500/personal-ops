"""Schema014 validator and executor tests; no Docker/PG/network/host side effects."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from host_runtime_config import HostRuntimeError
from host_runtime import PREDECESSOR_NAMES, SCHEMA_013_TABLES
from host_schema014 import (
    FIXED_NAME, FIXED_SHA256, checked_source, schema014_status, migrate_schema014,
)


REVIEWED_SQL = (
    "-- Preserve a user's free-text relationship label independently of the situation description.\n"
    "-- Existing persons (including legacy 'custom' rows) remain valid with NULL detail.\n"
    "ALTER TABLE persons ADD COLUMN relationship_detail text;\n"
    "ALTER TABLE persons ADD CONSTRAINT persons_relationship_detail_valid CHECK (\n"
    "  (relationship_code = 'custom' OR relationship_detail IS NULL)\n"
    "  AND (relationship_detail IS NULL OR\n"
    "    (char_length(btrim(relationship_detail)) BETWEEN 1 AND 60))\n"
    ");\n"
)
GENERATION = {"database": "tarororo_test", "user": "tarororo",
              "postmasterStartEpoch": "1791788137.513100"}


class FakeRuntime:
    def __init__(self, root, predecessor_checksums):
        self.root = root
        self.expected = predecessor_checksums
        self.applied = False
        self.extra_history = 0
        self.column = False
        self.constraint = False
        self.bad_predecessor = False
        self.generation_drift = False
        self.counts_change = False
        self.postcommit_counts_change = False
        self.calls = []

    def guard(self, project, target):
        assert (project, target) == ("tossApp", "tarororo-test")
        return self.root

    def identities(self):
        return {role: {"id": role + "-container", "started_at": role + "-time",
                       "image_id": role + "-image"}
                for role in ("api", "worker", "db")}

    def _verify_identity(self, snapshot, **kwargs):
        assert snapshot and kwargs["health"]

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        assert argv[:4] == ["docker", "exec", "-i", "tarororo-test-db-1"]
        if kwargs.get("capture", True) is False:
            sql = argv[-1]
            assert sql.startswith("BEGIN;")
            assert "SET LOCAL lock_timeout='5s'" in sql
            assert "pg_advisory_xact_lock" in sql
            assert "schema 013 history mismatch" in sql
            assert "schema 014 object collision" in sql
            assert "INSERT INTO public.schema_migrations" in sql
            assert sql.rstrip().endswith("COMMIT;")
            assert "DROP TABLE" not in sql.upper() and "DROP COLUMN" not in sql.upper()
            self.applied = True
            self.counts_change = self.postcommit_counts_change
            return b""
        sql = argv[-1]
        assert "BEGIN READ ONLY" in sql
        assert "fixed_schema014_status_v1" in sql
        items = {name: ("checksum_mismatch" if self.bad_predecessor and i == 6
                        else "checksum_match")
                 for i, name in enumerate(PREDECESSOR_NAMES)}
        items[FIXED_NAME] = "checksum_match" if self.applied else "missing"
        gen = dict(GENERATION)
        if self.generation_drift:
            gen["postmasterStartEpoch"] = "88.99"
        payload = {
            "history": items, "unexpected": self.extra_history,
            "schema013": {name: True for name in SCHEMA_013_TABLES},
            "column": self.column or self.applied,
            "constraint": self.constraint or self.applied,
            "counts": {"users": 2, "persons": 4 if self.counts_change else 3,
                       "readings": 8},
            "generation": gen,
        }
        return (json.dumps(payload) + "\n").encode()


class Schema014Test(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="schema014-dry-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "tossApp"
        self.migrations = self.root / "server" / "migrations"
        self.migrations.mkdir(parents=True)
        self.assertEqual(sha256(REVIEWED_SQL.encode()).hexdigest(), FIXED_SHA256)
        (self.migrations / FIXED_NAME).write_text(REVIEWED_SQL)
        digests = {}
        for i, name in enumerate(PREDECESSOR_NAMES):
            contents = f"-- fixture {i}\nSELECT 1;\n".encode()
            (self.migrations / name).write_bytes(contents)
            digests[name] = sha256(contents).hexdigest()
        self.runtime = FakeRuntime(self.root, digests)
        self.expected = digests
        self.state = Path(folder.name) / "state"
        self.state.mkdir()
        self.context = self.root
        self.operation = "a" * 32

    def immutable(self):
        return {"identities": self.runtime.identities(),
                "migration_checksum": FIXED_SHA256,
                "migration_checksums": deepcopy(self.expected),
                "database_generation": dict(GENERATION)}

    def backup(self, output):
        raw = b"PGDMP" + b"x" * 160
        output.write(raw)
        return len(raw), sha256(raw).hexdigest()

    def restore(self, runtime, archive, operation_id, **kwargs):
        self.assertEqual(runtime, self.runtime)
        self.assertEqual(operation_id, self.operation)
        self.assertEqual(archive.read_bytes(), b"PGDMP" + b"x" * 160)
        self.assertEqual(kwargs["expected_history"], 13)
        self.assertEqual(kwargs["expected_checksums"], self.expected)

    def assert_code(self, code, callback):
        with self.assertRaises(HostRuntimeError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_readonly_preflight_and_successful_status(self):
        before = schema014_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(before["state"], "missing")
        self.assertTrue(before["ready_to_prepare"])
        self.assertFalse(before["complete"])
        self.assertEqual(before["counts"], {"users": 2, "persons": 3, "readings": 8})
        self.assertFalse(before["column_present"])
        self.assertEqual(before["predecessor_checksums"], self.expected)
        self.assertEqual(len(self.runtime.calls), 1)
        self.runtime.applied = True
        after = schema014_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(after["state"], "ready")
        self.assertFalse(after["ready_to_prepare"])
        self.assertTrue(after["complete"])

    def test_rejects_collision_mismatch_and_unexpected_migrations(self):
        for field in ("column", "constraint", "bad_predecessor", "extra_history"):
            with self.subTest(field=field):
                setattr(self.runtime, field, 1 if field == "extra_history" else True)
                status = schema014_status(self.runtime, "tossApp", "tarororo-test")
                self.assertFalse(status["ready_to_prepare"])
                setattr(self.runtime, field, 0 if field == "extra_history" else False)

    def test_refuses_unreviewed_source_or_symlink(self):
        source = self.migrations / FIXED_NAME
        self.assertEqual(checked_source(self.root)[1], FIXED_SHA256)
        source.write_text(REVIEWED_SQL.replace("ADD COLUMN", "DROP COLUMN"))
        self.assert_code("SCHEMA014_SOURCE_NOT_APPROVED",
                         lambda: schema014_status(self.runtime, "tossApp", "tarororo-test"))
        source.unlink()
        source.symlink_to(self.migrations / PREDECESSOR_NAMES[0])
        self.assert_code("UNSAFE_BUILD_INPUT",
                         lambda: schema014_status(self.runtime, "tossApp", "tarororo-test"))

    def test_rejects_symlinked_migration_directory_before_reading_any_sql(self):
        outside = self.root.parent / "moved-migrations"
        self.migrations.rename(outside)
        self.migrations.symlink_to(outside, target_is_directory=True)
        self.assert_code(
            "UNSAFE_MIGRATION_SOURCE",
            lambda: schema014_status(self.runtime, "tossApp", "tarororo-test"),
        )
        self.assertEqual(self.runtime.calls, [])

    def test_executor_requires_backup_and_verified_disposable_restore(self):
        logs = []
        def save(output):
            logs.append("backup")
            return self.backup(output)
        def check(path):
            logs.append("check_archive")
            self.assertTrue(path.exists())
        def verify(runtime, archive, operation_id, **kwargs):
            logs.append("restore")
            self.restore(runtime, archive, operation_id, **kwargs)
        outcome = migrate_schema014(
            self.runtime, self.state, self.immutable(), self.context, self.operation,
            backup_fn=save, validate_fn=check, restore_fn=verify)
        self.assertEqual(logs, ["backup", "check_archive", "restore"])
        self.assertEqual(outcome["backup_bytes"], 165)
        self.assertEqual(sum(kwargs.get("capture", True) is False
                             for _argv, kwargs in self.runtime.calls), 1)
        self.assertTrue(self.runtime.applied)

    def test_postcommit_user_activity_does_not_misreport_a_committed_migration(self):
        # The guard checks the history and column, not a racy user-row count.
        self.runtime.postcommit_counts_change = True
        outcome = migrate_schema014(
            self.runtime, self.state, self.immutable(), self.context, self.operation,
            backup_fn=self.backup, validate_fn=lambda path: None,
            restore_fn=self.restore)
        self.assertTrue(self.runtime.applied)
        self.assertTrue(outcome["postcommit_counts_changed"])
        self.assertEqual(outcome["backup_bytes"], 165)

    def test_restore_failure_never_runs_sql(self):
        def refuse(*args, **kwargs):
            raise HostRuntimeError("BACKUP_RESTORE_FAILED")
        self.assert_code("BACKUP_RESTORE_FAILED", lambda: migrate_schema014(
            self.runtime, self.state, self.immutable(), self.context, self.operation,
            backup_fn=self.backup, validate_fn=lambda _: None, restore_fn=refuse))
        self.assertFalse(self.runtime.applied)
        self.assertFalse(any(kwargs.get("capture", True) is False
                             for _argv, kwargs in self.runtime.calls))

    def test_bad_generation_and_source_hash_never_take_backup(self):
        for change in ("generation", "checksum"):
            with self.subTest(change=change):
                immutable = self.immutable()
                if change == "generation":
                    immutable["database_generation"]["postmasterStartEpoch"] = "123.0"
                else:
                    immutable["migration_checksum"] = "f" * 64
                self.assert_code("SCHEMA_NOT_SAFE_TO_MIGRATE" if change == "generation"
                                 else "STALE_PLAN", lambda: migrate_schema014(
                    self.runtime, self.state, immutable, self.context, self.operation,
                    backup_fn=lambda output: self.fail("backup should not run")))
                self.assertFalse(self.runtime.applied)

    def test_detects_rows_changed_during_backup_without_sql(self):
        def mutating_backup(output):
            result = self.backup(output)
            self.runtime.counts_change = True
            return result
        self.assert_code("SCHEMA_CHANGED", lambda: migrate_schema014(
            self.runtime, self.state, self.immutable(), self.context, self.operation,
            backup_fn=mutating_backup, validate_fn=lambda path: None,
            restore_fn=self.restore))
        self.assertFalse(self.runtime.applied)


if __name__ == "__main__":
    unittest.main()
