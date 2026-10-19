"""Disposable DB restore rehearsal contract: mocked Docker, no host access."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from host_db_migration import _restore_check
from host_runtime import PREDECESSOR_NAMES
from host_runtime_config import HostRuntimeError


class FakeRuntime:
    def __init__(self, cleanup_fails=False):
        self.calls = []
        self.cleanup_fails = cleanup_fails

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if "dropdb" in argv and self.cleanup_fails:
            raise HostRuntimeError("HOST_COMMAND_FAILED")
        if "psql" in argv:
            return b"BEGIN\n12\nCOMMIT\n"
        return b""


class RestoreRehearsalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "fixture.dump"
        self.path.write_bytes(b"PGDMP" + b"x" * 120)
        self.path.chmod(0o600)
        self.operation_id = "c" * 32

    def test_creates_restores_validates_and_drops_disposable_db(self):
        rt = FakeRuntime()
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as restore:
            _restore_check(rt, self.path, self.operation_id)
        self.assertEqual(len(rt.calls), 3)
        database = "tarororo_restore_" + self.operation_id
        self.assertIn(database, rt.calls[0])
        self.assertIn("createdb", rt.calls[0])
        self.assertIn("dropdb", rt.calls[-1])
        self.assertIn(database, rt.calls[-1])
        cmd = restore.call_args.args[0]
        self.assertIn("pg_restore", cmd)
        self.assertIn("--exit-on-error", cmd)
        self.assertIn("--single-transaction", cmd)
        self.assertIn(database, cmd)
        self.assertNotIn("tarororo_test", cmd)

    def test_failed_restore_still_drops_disposable_db(self):
        rt = FakeRuntime()
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_FAILED"):
                _restore_check(rt, self.path, self.operation_id)
        self.assertIn("dropdb", rt.calls[-1])

    def test_failed_cleanup_never_looks_like_success(self):
        rt = FakeRuntime(cleanup_fails=True)
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_CLEANUP_FAILED"):
                _restore_check(rt, self.path, self.operation_id)

    def test_predecessor_restore_rejects_wrong_baseline_checksums_and_cleans_up(self):
        expected = {name: "a" * 64 for name in PREDECESSOR_NAMES[:5]}
        class BadChecksumRuntime(FakeRuntime):
            def run(self, argv, **kwargs):
                if "psql" in argv:
                    self.calls.append(tuple(argv))
                    return b'BEGIN\\n{"001_foundation.sql":"bad"}\\nCOMMIT\\n'
                return super().run(argv, **kwargs)
        rt = BadChecksumRuntime()
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_INVALID"):
                _restore_check(rt, self.path, self.operation_id,
                               expected_history=5, expected_checksums=expected)
        self.assertIn("dropdb", rt.calls[-1])
        self.assertNotIn("tarororo_test", rt.calls[-1])

    def test_symlink_archive_denied_before_any_docker_command(self):
        alias = self.path.parent / "alias.dump"
        alias.symlink_to(self.path)
        rt = FakeRuntime()
        with self.assertRaises(HostRuntimeError):
            _restore_check(rt, alias, self.operation_id)
        self.assertEqual(rt.calls, [])


if __name__ == "__main__":
    unittest.main()
