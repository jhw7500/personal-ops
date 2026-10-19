"""Bounded predecessor preflight and approval-only executor tests; no real Docker."""
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from host_db_migration import migrate_predecessors
from host_runtime import (
    HostRuntime, PREDECESSOR_NAMES, PREDECESSOR_TABLES, PREDECESSOR_INDEXES,
    PREDECESSOR_FUNCTIONS, SCHEMA_013_TABLES, HIGH_RISK_INPUTS,
    verify_reviewed_high_risk_inputs,
)
from host_runtime_config import (HostRuntimeError, TARGET, TARGET_CONFIG_V2,
                                 TARGET_CONFIG_V3, load_config, extended_enabled,
                                 predecessor_enabled)
from host_runtime_operations import HostRuntimeOperations, _digest
from host_runtime_tools import HOST_RUNTIME_TOOL_SPECS
from host_runtime_web import enable_predecessor_scope


GENERATION = {"database": "tarororo_test", "user": "tarororo", "postmasterStartEpoch": "123.456"}


def source_fixture(root):
    folder = root / "server" / "migrations"
    folder.mkdir(parents=True, exist_ok=True)
    digests = {}
    for i, name in enumerate(PREDECESSOR_NAMES):
        data = ("-- synthetic test source\nSELECT " + str(i + 1) + ";\n").encode()
        (folder / name).write_bytes(data)
        digests[name] = hashlib.sha256(data).hexdigest()
    return digests


def identities():
    return {role: {"id": role + "-identity", "started_at": "2026-10-12T00:00:00Z",
                   "image_id": role + "-image"} for role in ("api", "worker", "db")}


class ReadOnlyRuntime(HostRuntime):
    def __init__(self, root, history=None, *, collision=None, unexpected=0):
        self.root = root
        self.history = history or {}
        self.collision = collision
        self.unexpected = unexpected
        self.calls = []

    def guard(self, project, target):
        assert (project, target) == ("tossApp", "tarororo-test")
        return self.root

    def identities(self):
        return identities()

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        assert snapshot

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        query = argv[-1]
        assert "BEGIN READ ONLY;" in query
        assert "fixed_predecessor_status_v1" in query
        checksums = source_fixture_checksums(self.root)
        history = {name: self.history.get(name, "checksum_match" if i < 5 else "missing")
                   for i, name in enumerate(PREDECESSOR_NAMES)}
        objects = {name: (name == self.collision)
                   for name in (*PREDECESSOR_TABLES, *PREDECESSOR_INDEXES, *SCHEMA_013_TABLES)}
        functions = {name: (name == self.collision) for name in PREDECESSOR_FUNCTIONS}
        payload = {
            "history": history, "unexpected_history_count": self.unexpected,
            "objects": objects, "functions": functions, "generation": GENERATION,
        }
        assert all(h in query for h in checksums.values())
        return ("BEGIN\n" + json.dumps(payload) + "\nCOMMIT\n").encode()


def source_fixture_checksums(root):
    folder = root / "server" / "migrations"
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES}


class ExecutorRuntime:
    def __init__(self, digests):
        self.digests = digests
        self.calls = []
        self.applied = False
        self.fail_complete = False
        self.drift_generation = False

    def identities(self):
        return identities()

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        assert snapshot

    def predecessor_status(self, project, target):
        assert (project, target) == ("tossApp", "tarororo-test")
        generation = dict(GENERATION)
        if self.drift_generation:
            generation["postmasterStartEpoch"] = "999.99"
        return {
            "ready_to_prepare": not self.applied,
            "complete": self.applied and not self.fail_complete,
            "checksums": {name: self.digests[name] for name in PREDECESSOR_NAMES[5:12]},
            "generation": generation,
            "history": {name: "missing" if name == PREDECESSOR_NAMES[12] else
                        "checksum_match" for name in PREDECESSOR_NAMES},
            "schema_013_tables_present": {name: False for name in SCHEMA_013_TABLES},
        }

    def run(self, argv, *, capture=True, **kwargs):
        self.calls.append((argv, capture))
        assert argv[:3] == ["docker", "exec", "-i"]
        assert argv[3] == "tarororo-test-db-1"
        assert argv[-2] == "-c"
        sql = argv[-1]
        assert "DO $predecessor_guard$" in sql
        assert "pg_advisory_xact_lock" in sql
        assert "COMMIT;" in sql
        assert "013_staged_reading_access.sql" not in sql
        assert sql.count("INSERT INTO public.schema_migrations") == 7
        for name in PREDECESSOR_NAMES[5:12]:
            assert name in sql
        self.applied = True
        return b""


class PredecessorSafetyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="predecessor-mcp-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.digests = source_fixture(self.root)

    def test_fixed_read_only_inventory_classifies_exact_history_without_rows(self):
        stub = ReadOnlyRuntime(self.root)
        result = stub.predecessor_status("tossApp", "tarororo-test")
        self.assertTrue(result["ready_to_prepare"])
        self.assertFalse(result["complete"])
        self.assertEqual([result["history"][name] for name in PREDECESSOR_NAMES[:5]],
                         ["checksum_match"] * 5)
        self.assertEqual([result["history"][name] for name in PREDECESSOR_NAMES[5:]],
                         ["missing"] * 8)
        self.assertEqual(len(result["tables_present"]), 14)
        self.assertEqual(len(stub.calls), 1)
        self.assertNotIn("INSERT INTO", stub.calls[0][0][-1])

    def test_preflight_blocks_checksum_mismatch_partial_history_objects_and_unknown(self):
        scenarios = [
            {"history": {PREDECESSOR_NAMES[2]: "checksum_mismatch"}},
            {"history": {PREDECESSOR_NAMES[5]: "checksum_match"}},
            {"collision": "reading_memos_owner"},
            {"collision": "reading_credit_packs"},
            {"collision": "protect_checkout_identity"},
            {"unexpected": 1},
        ]
        for spec in scenarios:
            with self.subTest(spec=spec):
                self.assertFalse(ReadOnlyRuntime(self.root, **spec).predecessor_status(
                    "tossApp", "tarororo-test")["ready_to_prepare"])

    def test_reviewed_sql_only_006_through_012_with_backup_and_restore_first(self):
        rt = ExecutorRuntime(self.digests)
        immutable = {
            "identities": identities(), "database_generation": dict(GENERATION),
            "migration_checksums": {name: self.digests[name]
                                    for name in PREDECESSOR_NAMES[5:12]},
        }
        steps = []

        def backup(output):
            steps.append("backup")
            output.write(b"PGDMP" + b"x" * 140)
            return 145, "a" * 64

        def validate(path):
            steps.append("validate")
            self.assertTrue(path.is_file())

        def restore(runtime, path, operation_id, *, expected_history, expected_checksums):
            steps.append("restore")
            self.assertEqual(expected_history, 5)
            self.assertEqual(operation_id, "b" * 32)
            self.assertEqual(expected_checksums,
                             {name: self.digests[name] for name in PREDECESSOR_NAMES[:5]})

        output = migrate_predecessors(rt, self.root / "state", immutable,
                                      self.root, "b" * 32,
                                      backup_fn=backup, validate_fn=validate, restore_fn=restore)
        self.assertEqual(steps, ["backup", "validate", "restore"])
        self.assertEqual(output, {"backup_sha256": "a" * 64, "backup_bytes": 145})
        self.assertEqual(len(rt.calls), 1)
        self.assertFalse(any("compose" in str(command) for command, _ in rt.calls))

    def test_failed_restore_never_runs_sql(self):
        rt = ExecutorRuntime(self.digests)
        immutable = {
            "identities": identities(), "database_generation": dict(GENERATION),
            "migration_checksums": {name: self.digests[name]
                                    for name in PREDECESSOR_NAMES[5:12]},
        }

        def broken_restore(*args, **kwargs):
            raise HostRuntimeError("BACKUP_RESTORE_FAILED")

        def backup(output):
            output.write(b"PGDMP" + b"x" * 120)
            return 125, "a" * 64

        with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_FAILED"):
            migrate_predecessors(rt, self.root / "state", immutable, self.root, "c" * 32,
                                 backup_fn=backup, validate_fn=lambda path: None,
                                 restore_fn=broken_restore)
        self.assertEqual(rt.calls, [])
        self.assertFalse(rt.applied)

    def test_generation_drift_after_backup_blocks_sql(self):
        rt = ExecutorRuntime(self.digests)
        immutable = {
            "identities": identities(), "database_generation": dict(GENERATION),
            "migration_checksums": {name: self.digests[name]
                                    for name in PREDECESSOR_NAMES[5:12]},
        }

        def backup(output):
            output.write(b"PGDMP" + b"x" * 120)
            return 125, "a" * 64

        def restore(*args, **kwargs):
            rt.drift_generation = True

        with self.assertRaisesRegex(HostRuntimeError, "SCHEMA_CHANGED"):
            migrate_predecessors(rt, self.root / "state", immutable, self.root, "d" * 32,
                                 backup_fn=backup, validate_fn=lambda path: None,
                                 restore_fn=restore)
        self.assertEqual(rt.calls, [])

    def test_wrong_tool_cannot_execute_predecessor_plan_or_consume_approval(self):
        project_root = self.root / "project"
        project_root.mkdir()
        manager = HostRuntimeOperations(
            {"tossApp": project_root}, state_dir=self.root / "operation-state",
            runtime=ExecutorRuntime(self.digests), approval_script=self.root / "approval-script",
        )
        operation_id = "f" * 32
        immutable = {
            "operation_id": operation_id, "project": "tossApp",
            "target": "tarororo-test", "mode": "disabled",
            "component": "predecessor_migration",
        }
        manager._save({
            "project": "tossApp", "target": "tarororo-test",
            "operation_id": operation_id, "immutable": immutable,
            "plan_digest": _digest(immutable), "state": "pending_host_approval",
        })
        with self.assertRaisesRegex(HostRuntimeError, "WRONG_OPERATION_TYPE"):
            asyncio.run(manager.execute("tossApp", "tarororo-test", operation_id,
                                        expected_component={"api", "worker"}))
        self.assertFalse((manager.operations_dir / (operation_id + ".approval.json")).exists())
        self.assertEqual(manager._load("tossApp", "tarororo-test", operation_id)["state"],
                         "pending_host_approval")

    def test_predecessor_plan_needs_new_v3_scope_even_when_v2_is_enabled(self):
        path = self.root / "host-runtime.json"
        path.write_text(json.dumps({
            "version": 2, "enabled": True,
            "targets": {TARGET: TARGET_CONFIG_V2},
        }))
        path.chmod(0o600)
        self.assertTrue(load_config(path))
        self.assertTrue(extended_enabled(path))
        self.assertFalse(predecessor_enabled(path))
        project = self.root / "project"
        project.mkdir()
        stub = SimpleNamespace(config_path=path, guard=lambda *_: project)
        manager = HostRuntimeOperations(
            {"tossApp": project}, state_dir=self.root / "ops-state",
            runtime=stub, approval_script=self.root / "mock-approval",
        )
        with self.assertRaisesRegex(HostRuntimeError, "PREDECESSOR_SCOPE_DISABLED"):
            manager.prepare("tossApp", "tarororo-test", "predecessor_migration",
                            "disabled", "scope-probe")
        self.assertEqual(list(manager.operations_dir.glob("*.json")), [])

    def test_predecessor_v3_upgrade_requires_distinct_local_confirmation(self):
        path = self.root / "host-runtime.json"
        before = {"version": 2, "enabled": True,
                  "targets": {TARGET: TARGET_CONFIG_V2}}
        path.write_text(json.dumps(before))
        path.chmod(0o600)
        fake = SimpleNamespace(
            runtime=SimpleNamespace(config_path=path),
            _require_trusted_runtime=lambda: None,
        )
        with self.assertRaisesRegex(HostRuntimeError, "HOST_APPROVAL_DENIED"):
            enable_predecessor_scope(fake, "ENABLE WORKER AND DB 013")
        self.assertEqual(json.loads(path.read_text()), before)
        output = enable_predecessor_scope(fake, "ENABLE DB 006-012")
        self.assertEqual(output, {"scope": "db-006-012", "enabled": True, "changed": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(predecessor_enabled(path))
        self.assertTrue(extended_enabled(path))
        self.assertEqual(json.loads(path.read_text())["targets"][TARGET], TARGET_CONFIG_V3)
        self.assertEqual(enable_predecessor_scope(fake, "ENABLE DB 006-012")["changed"], False)

    def test_predecessor_sql_must_match_a_tracked_reviewed_git_blob(self):
        repo = self.root / "reviewed"
        source_fixture(repo)
        for name in HIGH_RISK_INPUTS:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("reviewed fixture\\n")
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=MigrationTest",
                        "-c", "user.email=test@invalid.example", "commit", "-qm", "fixture"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        verify_reviewed_high_risk_inputs(repo, include_predecessors=True)
        migration = repo / "server/migrations/006_reading_memos.sql"
        migration.write_text("-- changed without review\\n")
        # The old generic high-risk list did not cover this migration file.
        verify_reviewed_high_risk_inputs(repo)
        with self.assertRaisesRegex(HostRuntimeError, "REVIEW_REQUIRED"):
            verify_reviewed_high_risk_inputs(repo, include_predecessors=True)

    def test_tool_contract_scopes_and_operation_type_are_separate(self):
        specs = {item["name"]: item for item in HOST_RUNTIME_TOOL_SPECS}
        for name in ("get_predecessor_migration_status", "prepare_predecessor_migration",
                     "execute_predecessor_migration", "execute_db_migration"):
            self.assertEqual(specs[name]["inputSchema"]["properties"]["project"]["enum"], ["tossApp"])
            self.assertEqual(specs[name]["inputSchema"]["properties"]["target"]["enum"], ["tarororo-test"])
        self.assertTrue(specs["get_predecessor_migration_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(specs["prepare_predecessor_migration"]["annotations"]["readOnlyHint"])
        payload = HostRuntimeOperations._public({
            "immutable": {"component": "predecessor_migration", "project": "tossApp",
                          "target": "tarororo-test", "mode": "disabled",
                          "source_digest": "f" * 64, "changed_paths": [],
                          "high_risk_changes": [], "expires_at": "2026-10-13T05:00:00Z"},
            "project": "tossApp", "target": "tarororo-test",
            "operation_id": "e" * 32, "state": "pending_host_approval",
            "phase": "APPROVAL", "plan_digest": "a" * 64,
            "created_at": "2026-10-13T04:00:00Z",
        })
        self.assertEqual(payload["impact"]["schema_change"], "006-012_only")
        self.assertEqual(payload["impact"]["recreates"], [])
        self.assertEqual(payload["impact"]["preserves"], ["api", "worker"])


if __name__ == "__main__":
    unittest.main()
