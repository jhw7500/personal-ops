"""Backup failure classification tests. No real Docker, DB, credentials or network."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_db_migration as backup
from host_runtime_config import HostRuntimeError


class BackupValidationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="backup-validation-fixture-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "synthetic.dump"

    def assert_code(self, wanted, func):
        with self.assertRaises(HostRuntimeError) as captured:
            func()
        self.assertEqual(captured.exception.code, wanted)
        # Public errors must not expose pg_dump/pg_restore stderr, SQL or rows.
        self.assertEqual(str(captured.exception), wanted)

    def run_synthetic_dump(self, script):
        with patch.object(backup, "DUMP", (sys.executable, "-c", script)):
            with self.path.open("wb") as output:
                return backup._run_backup(output)

    def test_dump_stream_returns_digest_only_for_complete_synthetic_content(self):
        payload = b"PGDMP" + b"x" * 180
        size, digest = self.run_synthetic_dump(
            "import sys;sys.stdout.buffer.write(b'PGDMP'+b'x'*180)"
        )
        self.assertEqual(size, len(payload))
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.path.read_bytes(), payload)

    def test_dump_under_100_bytes_is_distinct_from_restore_catalog_failure(self):
        self.assert_code(
            "BACKUP_DUMP_TOO_SMALL",
            lambda: self.run_synthetic_dump(
                "import sys;sys.stdout.buffer.write(b'PGDMP'+b'x'*12)"
            ),
        )

    def test_dump_nonzero_remains_failed_without_exposing_child_error(self):
        self.assert_code(
            "BACKUP_FAILED",
            lambda: self.run_synthetic_dump(
                "import sys;sys.stderr.write('secret diagnostic');sys.exit(3)"
            ),
        )

    def test_dump_start_failure_is_specific_and_sanitized(self):
        def attempt():
            with self.path.open("wb") as output:
                backup._run_backup(output)

        with patch("host_db_migration.subprocess.Popen", side_effect=FileNotFoundError("private")):
            self.assert_code("BACKUP_DUMP_START_FAILED", attempt)

    def test_dump_stream_io_failure_is_sanitized(self):
        with patch("host_db_migration.os.set_blocking", side_effect=OSError("private")):
            self.assert_code(
                "BACKUP_DUMP_IO_FAILED",
                lambda: self.run_synthetic_dump(
                    "import sys;sys.stdout.buffer.write(b'PGDMP'+b'x'*180)"
                ),
            )

    def test_dump_stream_timeout_kills_only_its_synthetic_process(self):
        with patch.object(backup, "DUMP_TIMEOUT_SECONDS", 0.04):
            self.assert_code(
                "BACKUP_TIMEOUT",
                lambda: self.run_synthetic_dump(
                    "import sys,time;sys.stdout.buffer.write(b'PGDMP'+b'x'*180);"
                    "sys.stdout.flush();time.sleep(5)"
                ),
            )

    def test_archive_header_invalid_does_not_spawn_pg_restore(self):
        self.path.write_bytes(b"NOTDB" + b"x" * 150)
        with patch("host_db_migration.subprocess.run") as run:
            self.assert_code("BACKUP_HEADER_INVALID",
                             lambda: backup._validate_archive(self.path))
            run.assert_not_called()

    def test_archive_missing_is_specific_and_does_not_spawn_pg_restore(self):
        with patch("host_db_migration.subprocess.run") as run:
            self.assert_code("BACKUP_ARCHIVE_UNREADABLE",
                             lambda: backup._validate_archive(self.path))
            run.assert_not_called()

    def test_catalog_success_reads_existing_dump_only(self):
        self.path.write_bytes(b"PGDMP" + b"x" * 180)
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertIsNone(backup._validate_archive(self.path))
        args, kwargs = run.call_args
        self.assertEqual(args[0], backup.CHECK)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"].name, str(self.path))
        self.assertTrue(kwargs["check"] is False)

    def test_catalog_child_starts_at_archive_header_after_parent_header_check(self):
        self.path.write_bytes(b"PGDMP" + b"x" * 16384)
        # This is a real subprocess, not a mocked catalog success. It reads the
        # inherited descriptor, exposing buffered Python seek's offset mismatch.
        command = (sys.executable, "-c",
                   "import sys;sys.exit(0 if sys.stdin.buffer.read(5)==b'PGDMP' else 7)")
        with patch.object(backup, "CHECK", command):
            try:
                result = backup._validate_archive(self.path)
            except HostRuntimeError as error:
                self.fail("Catalog child did not receive the archive header: " + error.code)
        self.assertIsNone(result)

    def test_catalog_nonzero_is_specific_and_sanitized(self):
        self.path.write_bytes(b"PGDMP" + b"x" * 180)
        with patch("host_db_migration.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, b"", b"secret")):
            self.assert_code("BACKUP_CATALOG_REJECTED",
                             lambda: backup._validate_archive(self.path))

    def test_catalog_timeout_is_distinct_and_sanitized(self):
        self.path.write_bytes(b"PGDMP" + b"x" * 180)
        with patch("host_db_migration.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(backup.CHECK, 60, b"secret")):
            self.assert_code("BACKUP_CATALOG_TIMEOUT",
                             lambda: backup._validate_archive(self.path))

    def test_catalog_start_error_is_distinct_and_sanitized(self):
        self.path.write_bytes(b"PGDMP" + b"x" * 180)
        with patch("host_db_migration.subprocess.run",
                   side_effect=OSError("secret path")):
            self.assert_code("BACKUP_CATALOG_EXEC_FAILED",
                             lambda: backup._validate_archive(self.path))


if __name__ == "__main__":
    unittest.main()
