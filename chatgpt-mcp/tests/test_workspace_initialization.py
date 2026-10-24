import json
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

import workspace_authority  # noqa: E402
from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_paths import WorkspaceError  # noqa: E402


POLICY = "a" * 64
RELEASE = "b" * 64
TRANSACTION = "c" * 32


class WorkspaceInitialAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "projects" / "alpha"
        self.project.mkdir(parents=True)
        self.owner = self.root / "owner"
        self.owner.mkdir(mode=0o700)
        self.state = self.owner / "authority"
        self.authority = WorkspaceAuthority({"alpha": self.project}, self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(WorkspaceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def prepare(self):
        return self.authority.prepare_initial(
            POLICY, {RELEASE: 2}, TRANSACTION,
        )

    def test_prepare_and_commit_publish_complete_private_authority(self):
        prepared = self.prepare()

        self.assertEqual(set(prepared), {
            "version", "transaction_id", "stage_name", "stage_device",
            "stage_inode", "manifest_sha256", "identity_sha256", "binding",
        })
        self.assertRegex(prepared["binding"]["installation_id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("cursor", repr(prepared))
        stage = self.owner / prepared["stage_name"]
        self.assertTrue(stage.is_dir())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.authority.enforcement_path.exists())

        binding = self.authority.commit_initial(prepared)

        self.assertEqual(binding, prepared["binding"])
        self.assertEqual(self.authority.binding(POLICY), binding)
        self.assertFalse(stage.exists())
        self.assertTrue(self.authority.enforcement_path.is_file())
        for entry in [self.state, *self.state.rglob("*")]:
            expected = 0o700 if entry.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), expected, entry)

    def test_prepare_rejects_malformed_transaction_and_hostile_locations(self):
        for transaction in ("", "C" * 32, "c" * 31, "c" * 33, True):
            self.assert_code(
                "INVALID_AUTHORITY_TRANSACTION",
                lambda transaction=transaction: self.authority.prepare_initial(
                    POLICY, {RELEASE: 1}, transaction,
                ),
            )

        nested = WorkspaceAuthority(
            {"alpha": self.project}, self.project / ".authority",
        )
        self.assert_code(
            "AUTHORITY_STATE_IN_PROJECT",
            lambda: nested.prepare_initial(POLICY, {RELEASE: 1}, TRANSACTION),
        )

        os.chmod(self.owner, 0o770)
        self.assert_code(
            "UNSAFE_AUTHORITY_PATH",
            lambda: self.authority.prepare_initial(POLICY, {RELEASE: 1}, TRANSACTION),
        )

    def test_commit_rejects_malformed_descriptor_and_mutated_stage(self):
        prepared = self.prepare()
        malformed = dict(prepared)
        malformed["stage_name"] = "../authority"
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(malformed),
        )

        stage = self.owner / prepared["stage_name"]
        state_path = stage / "authority.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["authority_epoch"] = 9
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(prepared),
        )
        self.assertFalse(self.authority.enforcement_path.exists())

    def test_interrupted_prepare_retains_private_forensic_stage(self):
        original = workspace_authority._create_private_file
        calls = 0

        def fail_during_state_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WorkspaceError("AUTHORITY_STATE_UNAVAILABLE")
            return original(*args, **kwargs)

        with mock.patch(
            "workspace_authority._create_private_file",
            side_effect=fail_during_state_write,
        ):
            self.assert_code(
                "AUTHORITY_STATE_UNAVAILABLE",
                lambda: self.prepare(),
            )

        stages = list(self.owner.glob(".authority.prepare-*"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(stat.S_IMODE(stages[0].stat().st_mode), 0o700)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.authority.enforcement_path.exists())

    def test_same_transaction_recovers_complete_stage_after_subprocess_exit(self):
        script = """
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from workspace_authority import WorkspaceAuthority
authority = WorkspaceAuthority({'alpha': Path(sys.argv[2])}, Path(sys.argv[3]))
prepared = authority.prepare_initial(sys.argv[4], {sys.argv[5]: 2}, sys.argv[6])
print(json.dumps(prepared, sort_keys=True), flush=True)
os._exit(73)
"""
        child = subprocess.run(
            [
                sys.executable, "-B", "-c", script, str(ROOT), str(self.project),
                str(self.state), POLICY, RELEASE, TRANSACTION,
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(child.returncode, 73, child.stderr)
        original = json.loads(child.stdout)

        recovered = self.prepare()

        self.assertEqual(recovered, original)
        stage = self.owner / recovered["stage_name"]
        info = stage.stat()
        self.assertEqual(
            (recovered["stage_device"], recovered["stage_inode"]),
            (info.st_dev, info.st_ino),
        )
        self.assertEqual(self.authority.commit_initial(recovered), recovered["binding"])

    def test_same_transaction_quarantines_partial_subprocess_stage_then_retries(self):
        script = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import workspace_authority
from workspace_authority import WorkspaceAuthority
original = workspace_authority._create_private_file
calls = 0
def crash(*args, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        os._exit(74)
    return original(*args, **kwargs)
workspace_authority._create_private_file = crash
authority = WorkspaceAuthority({'alpha': Path(sys.argv[2])}, Path(sys.argv[3]))
authority.prepare_initial(sys.argv[4], {sys.argv[5]: 2}, sys.argv[6])
"""
        child = subprocess.run(
            [
                sys.executable, "-B", "-c", script, str(ROOT), str(self.project),
                str(self.state), POLICY, RELEASE, TRANSACTION,
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(child.returncode, 74, child.stderr)

        recovered = self.prepare()

        quarantined = list(self.owner.glob(".authority.prepare-*.quarantine-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(stat.S_IMODE(quarantined[0].stat().st_mode), 0o700)
        self.assertTrue((self.owner / recovered["stage_name"]).is_dir())
        self.assertEqual(self.authority.commit_initial(recovered), recovered["binding"])

    def test_same_transaction_recovery_rejects_changed_initial_contract(self):
        prepared = self.prepare()

        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.prepare_initial(
                "d" * 64, {RELEASE: 2}, TRANSACTION,
            ),
        )
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.prepare_initial(
                POLICY, {RELEASE: 3}, TRANSACTION,
            ),
        )
        self.assertTrue((self.owner / prepared["stage_name"]).is_dir())

    def test_same_transaction_recovery_rejects_nonempty_initial_state(self):
        prepared = self.prepare()
        stage = self.owner / prepared["stage_name"]
        staged = WorkspaceAuthority({"alpha": self.project}, stage)
        target = self.root / "pending.json"
        target.write_text("{}", encoding="utf-8")
        staged.request(
            "alpha", str(target), ["read"], 1800, "must remain empty", POLICY,
        )

        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.prepare(),
        )
        self.assertTrue(stage.is_dir())

    def test_commit_fails_closed_when_stage_and_final_are_absent_or_both_exist(self):
        prepared = self.prepare()
        stage = self.owner / prepared["stage_name"]
        for child in sorted(stage.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        stage.rmdir()
        self.assert_code(
            "AUTHORITY_PREPARATION_MISSING",
            lambda: self.authority.commit_initial(prepared),
        )

        prepared = self.authority.prepare_initial(
            POLICY, {RELEASE: 2}, "d" * 32,
        )
        self.state.mkdir(mode=0o700)
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(prepared),
        )
        self.assertTrue((self.owner / prepared["stage_name"]).exists())
        self.assertTrue(self.state.exists())

    def test_commit_resumes_after_latch_was_fsynced_before_publish(self):
        prepared = self.prepare()
        original = workspace_authority._rename_noreplace

        with mock.patch(
            "workspace_authority._rename_noreplace",
            side_effect=WorkspaceError("AUTHORITY_STATE_UNAVAILABLE"),
        ):
            self.assert_code(
                "AUTHORITY_STATE_UNAVAILABLE",
                lambda: self.authority.commit_initial(prepared),
            )

        self.assertTrue(self.authority.enforcement_path.is_file())
        self.assertTrue((self.owner / prepared["stage_name"]).is_dir())
        self.assertFalse(self.state.exists())
        with mock.patch("workspace_authority._rename_noreplace", wraps=original):
            self.assertEqual(
                self.authority.commit_initial(prepared), prepared["binding"],
            )

    def test_commit_never_replaces_a_final_directory_created_at_publish_race(self):
        prepared = self.prepare()
        original = workspace_authority._rename_noreplace

        def create_final_then_publish(*args, **kwargs):
            self.state.mkdir(mode=0o700)
            original(*args, **kwargs)

        with mock.patch(
            "workspace_authority._rename_noreplace",
            side_effect=create_final_then_publish,
        ):
            self.assert_code(
                "AUTHORITY_PREPARATION_INVALID",
                lambda: self.authority.commit_initial(prepared),
            )

        self.assertEqual(list(self.state.iterdir()), [])
        self.assertTrue((self.owner / prepared["stage_name"]).is_dir())

    def test_commit_resumes_after_publish_before_return_without_regeneration(self):
        prepared = self.prepare()
        original = workspace_authority._rename_noreplace

        def rename_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise WorkspaceError("AUTHORITY_STATE_UNAVAILABLE")

        with mock.patch(
            "workspace_authority._rename_noreplace", side_effect=rename_then_crash,
        ):
            self.assert_code(
                "AUTHORITY_STATE_UNAVAILABLE",
                lambda: self.authority.commit_initial(prepared),
            )

        state = json.loads((self.state / "authority.json").read_text(encoding="utf-8"))
        cursor = (self.state / "cursor.key").read_bytes()
        self.assertEqual(self.authority.commit_initial(prepared), prepared["binding"])
        resumed = json.loads((self.state / "authority.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed["installation_id"], state["installation_id"])
        self.assertEqual((self.state / "cursor.key").read_bytes(), cursor)

    def test_repeated_commit_does_not_reset_active_state_or_grants(self):
        prepared = self.prepare()
        self.authority.commit_initial(prepared)
        target = self.root / "result.json"
        target.write_text("{}", encoding="utf-8")
        request = self.authority.request(
            "alpha", str(target), ["read"], 1800, "retain", POLICY,
        )
        grant = self.authority.confirm(
            "alpha", request["request_id"], request["_confirmation"], POLICY,
        )
        with self.authority.project_lock("alpha"):
            pass
        before = (self.state / "authority.json").read_bytes()
        cursor = (self.state / "cursor.key").read_bytes()
        writer_lock = self.state / "locks/workspace-writers.lock"
        lock_identity = (writer_lock.stat().st_dev, writer_lock.stat().st_ino)

        self.assertEqual(self.authority.commit_initial(prepared), prepared["binding"])

        self.assertEqual((self.state / "authority.json").read_bytes(), before)
        self.assertEqual((self.state / "cursor.key").read_bytes(), cursor)
        self.assertEqual((writer_lock.stat().st_dev, writer_lock.stat().st_ino), lock_identity)
        self.assertEqual(
            self.authority.require(
                "alpha", str(target), "read", grant["grant_id"], POLICY,
            )["grant_id"],
            grant["grant_id"],
        )

    def test_unpublished_stage_rejects_a_writer_lock(self):
        prepared = self.prepare()
        writer_lock = self.owner / prepared["stage_name"] / "locks/workspace-writers.lock"
        writer_lock.touch(mode=0o600)
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(prepared),
        )
        self.assertFalse(self.authority.enforcement_path.exists())

    def test_published_authority_rejects_unsafe_or_unknown_writer_locks(self):
        for variant in ("unknown", "mode", "data", "symlink", "hardlink", "fifo"):
            with self.subTest(variant=variant):
                state = self.owner / ("authority-" + variant)
                authority = WorkspaceAuthority({"alpha": self.project}, state)
                prepared = authority.prepare_initial(POLICY, {RELEASE: 2}, TRANSACTION)
                authority.commit_initial(prepared)
                with authority.project_lock("alpha"):
                    pass
                lock = state / "locks/workspace-writers.lock"
                if variant == "unknown":
                    (lock.parent / "unknown.lock").touch(mode=0o600)
                elif variant == "mode":
                    lock.chmod(0o644)
                elif variant == "data":
                    lock.write_bytes(b"unexpected")
                elif variant == "hardlink":
                    os.link(lock, self.owner / "writer-hardlink")
                elif variant == "symlink":
                    lock.unlink()
                    target = self.owner / "writer-target"
                    target.touch(mode=0o600)
                    lock.symlink_to(target)
                else:
                    lock.unlink()
                    os.mkfifo(lock, mode=0o600)
                self.assert_code(
                    "AUTHORITY_PREPARATION_INVALID",
                    lambda: authority.commit_initial(prepared),
                )

    def test_published_writer_lock_replacement_is_rejected(self):
        prepared = self.prepare()
        self.authority.commit_initial(prepared)
        with self.authority.project_lock("alpha"):
            pass
        writer_lock = self.state / "locks/workspace-writers.lock"
        original_open = workspace_authority._open_private_file

        def replace_after_open(parent_fd, name, flags):
            descriptor = original_open(parent_fd, name, flags)
            if name == "workspace-writers.lock":
                writer_lock.unlink()
                writer_lock.touch(mode=0o600)
            return descriptor

        with mock.patch.object(workspace_authority, "_open_private_file", replace_after_open):
            self.assert_code(
                "AUTHORITY_PREPARATION_INVALID",
                lambda: self.authority.commit_initial(prepared),
            )

    def test_commit_rejects_corrupt_or_replaced_final_state(self):
        prepared = self.prepare()
        self.authority.commit_initial(prepared)
        (self.state / "activated").write_text("0" * 32, encoding="ascii")
        (self.state / "activated").chmod(0o600)

        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(prepared),
        )

        self.state.rename(self.owner / "replaced-authority")
        self.state.mkdir(mode=0o700)
        self.assert_code(
            "AUTHORITY_PREPARATION_INVALID",
            lambda: self.authority.commit_initial(prepared),
        )


if __name__ == "__main__":
    unittest.main()
