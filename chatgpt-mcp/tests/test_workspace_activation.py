"""Owner activation journal tests use actual private files and a fresh process."""
import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_activation import ActivationError, ActivationJournal  # noqa: E402


TRANSACTION = "a" * 32
SOURCE = "b" * 64


class ActivationJournalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.owner = self.root / "owner"
        self.owner.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.projects = {"alpha": self.project}
        self.directory = self.owner / "workspace-activation"
        self.journal = ActivationJournal(self.directory, self.projects)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self):
        return self.journal.create(TRANSACTION, SOURCE, {"sentinel": "owner-data"})

    def assert_code(self, code, operation):
        with self.assertRaises(ActivationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_create_private_journal_and_no_clobber(self):
        result = self.create()
        self.assertEqual(result["transaction_id"], TRANSACTION)
        self.assertEqual(result["revision"], 0)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        path = self.directory / (TRANSACTION + ".json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        seal = self.directory / (TRANSACTION + ".plan.json")
        self.assertEqual(stat.S_IMODE(seal.stat().st_mode), 0o600)
        self.assertEqual(seal.stat().st_nlink, 1)
        self.assert_code("ACTIVATION_JOURNAL_EXISTS", self.create)

    def test_missing_corrupt_or_drifted_plan_seal_fails_closed(self):
        self.create()
        journal = self.directory / (TRANSACTION + ".json")
        seal = self.directory / (TRANSACTION + ".plan.json")
        original_seal = seal.read_bytes()

        seal.unlink()
        self.assert_code("ACTIVATION_PLAN_SEAL_INVALID", lambda: self.journal.read(TRANSACTION))
        seal.write_bytes(b"not-json")
        seal.chmod(0o600)
        self.assert_code("ACTIVATION_PLAN_SEAL_INVALID", lambda: self.journal.read(TRANSACTION))
        seal.write_bytes(original_seal)
        seal.chmod(0o600)

        value = json.loads(journal.read_bytes())
        value["plan"]["sentinel"] = "edited"
        journal.write_text(json.dumps(value, sort_keys=True))
        journal.chmod(0o600)
        self.assert_code("ACTIVATION_PLAN_CHANGED", lambda: self.journal.read(TRANSACTION))

    def test_persist_descriptor_before_return_and_fresh_process_read(self):
        self.create()
        descriptor = {"transaction_id": TRANSACTION, "manifest_sha256": "c" * 64}
        with self.journal.locked(TRANSACTION, SOURCE) as current:
            current.save("authority_prepared", prepared=descriptor)
        script = (
            "import json,sys; from workspace_activation import ActivationJournal; "
            "print(json.dumps(ActivationJournal(sys.argv[1], {'alpha':sys.argv[2]})"
            ".read(sys.argv[3]),sort_keys=True))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.directory),
             str(self.project), TRANSACTION], cwd=ROOT, capture_output=True, check=True,
        )
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded["prepared"], descriptor)
        self.assertEqual(loaded["revision"], 1)

    def test_changed_source_cannot_resume(self):
        self.create()
        def resume():
            with self.journal.locked(TRANSACTION, "c" * 64):
                self.fail("changed source acquired journal")
        self.assert_code("ACTIVATION_SOURCE_CHANGED", resume)

    def test_journal_cas_detects_same_user_replacement(self):
        self.create()
        path = self.directory / (TRANSACTION + ".json")
        with self.journal.locked(TRANSACTION, SOURCE) as current:
            value = json.loads(path.read_bytes())
            value["phase"] = "foreign"
            path.write_text(json.dumps(value))
            self.assert_code("ACTIVATION_JOURNAL_CHANGED", lambda: current.save("next"))

    def test_bad_file_types_modes_and_duplicate_json_are_rejected(self):
        self.create()
        path = self.directory / (TRANSACTION + ".json")
        original = path.read_bytes()
        for payload in (b'{"version":1,"version":1}', b"[]", b"not-json"):
            path.write_bytes(payload)
            self.assert_code("ACTIVATION_JOURNAL_INVALID", lambda: self.journal.read(TRANSACTION))
        path.write_bytes(original)
        os.chmod(path, 0o644)
        self.assert_code("ACTIVATION_JOURNAL_UNSAFE", lambda: self.journal.read(TRANSACTION))
        os.chmod(path, 0o600)
        link = self.directory / "copy"
        os.link(path, link)
        self.assert_code("ACTIVATION_JOURNAL_UNSAFE", lambda: self.journal.read(TRANSACTION))
        link.unlink()
        path.unlink()
        path.symlink_to(self.owner / "elsewhere")
        self.assert_code("ACTIVATION_JOURNAL_UNSAFE", lambda: self.journal.read(TRANSACTION))

    def test_project_overlap_and_invalid_id_do_not_create_files(self):
        bad = ActivationJournal(self.project / "journal", self.projects)
        self.assert_code("ACTIVATION_JOURNAL_IN_PROJECT", lambda: bad.create(TRANSACTION, SOURCE, {}))
        for value in ("../x", "A" * 32, "a" * 31, True):
            self.assert_code("ACTIVATION_ID_INVALID", lambda value=value: self.journal.read(value))
        self.assertFalse(self.directory.exists())

    def test_lock_inode_replacement_is_detected_before_write(self):
        self.create()
        with self.assertRaises(ActivationError) as caught:
            with self.journal.locked(TRANSACTION, SOURCE) as current:
                path = self.directory / "activation.lock"
                path.unlink()
                path.write_bytes(b"")
                os.chmod(path, 0o600)
                current.save("next")
        self.assertEqual(caught.exception.code, "ACTIVATION_LOCK_CHANGED")

    def test_missing_status_does_not_create_directory(self):
        self.assert_code("ACTIVATION_JOURNAL_MISSING", lambda: self.journal.read(TRANSACTION))
        self.assertFalse(self.directory.exists())

    def test_stable_owner_entry_rejects_unverified_release_before_running_code(self):
        import workspace_control
        with mock.patch("subprocess.run") as execute:
            with self.assertRaises(workspace_control.WorkspaceControlError):
                workspace_control.main([
                    "activation", "--release", str(self.project), "--",
                    "status", "--transaction-id", TRANSACTION,
                ])
        execute.assert_not_called()

    def test_owner_entry_rejects_unlisted_coordinator_files(self):
        import workspace_control
        releases = self.owner / "releases"
        releases.mkdir(mode=0o700)
        payload = b"fixture"
        manifest = {name: hashlib.sha256(payload).hexdigest()
                    for name in workspace_control.MANDATORY_RELEASE_FILES}
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        release = releases / digest[:20]
        release.mkdir(mode=0o700)
        for name in (*manifest, "workspace_activation.py", "workspace_activation_cli.py",
                     "workspace_activation_host.py", "workspace_activation_legacy.py"):
            path = release / name
            path.write_bytes(payload)
            path.chmod(0o600)
        path = release / "manifest.json"
        path.write_text(json.dumps(manifest, sort_keys=True))
        path.chmod(0o600)
        with mock.patch("subprocess.run") as execute:
            with self.assertRaises(workspace_control.WorkspaceControlError) as caught:
                workspace_control.main(["activation", "--release", str(release), "--", "--help"])
        self.assertEqual(caught.exception.code, "WORKSPACE_RELEASE_INCOMPATIBLE")
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
