"""Private, exact source snapshots for registered host operations.

Temp directories and local Git repositories only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from host_operation_fixtures import commit_all, fixture_v3, git  # noqa: E402
import host_source_review  # noqa: E402
from host_source_review import freeze_source, inventory_source  # noqa: E402
from host_runtime import verify_reviewed_high_risk_inputs  # noqa: E402
from host_runtime_config import TARGET, HostRuntimeError  # noqa: E402


class FreezeSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        target = fixture_v3(self.root)["targets"][TARGET]
        self.inputs = target["source_inputs"]
        self.high_risk = target["high_risk_inputs"]
        (self.root / "deploy/pc-test/run.mjs").chmod(0o755)
        commit_all(self.root)
        self.private = self.home / "private"
        self.private.mkdir(mode=0o700)

    def freeze(self, policy="git_head", name="snap"):
        return freeze_source(self.root, self.private / name, review_policy=policy,
                             inputs=self.inputs, high_risk_inputs=self.high_risk)

    def assertRefused(self, code, policy="git_head"):
        with self.assertRaises(HostRuntimeError) as caught:
            self.freeze(policy)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse((self.private / "snap").exists(), "partial snapshot left behind")

    def test_snapshot_is_private_owner_only_and_exact(self):
        frozen = self.freeze()
        snap = Path(frozen["snapshot"])
        self.assertEqual(snap, self.private / "snap" / "source")
        for path in [self.private / "snap", *snap.rglob("*")]:
            info = path.lstat()
            self.assertEqual(info.st_uid, os.getuid())
            self.assertFalse(stat.S_ISLNK(info.st_mode))
            if stat.S_ISDIR(info.st_mode):
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, path)
            else:
                self.assertIn(stat.S_IMODE(info.st_mode), (0o600, 0o700), path)
        self.assertEqual(stat.S_IMODE((snap / "deploy/pc-test/run.mjs").stat().st_mode), 0o700)
        manifest = {item["path"]: item for item in frozen["files"]}
        self.assertEqual(set(manifest), {path.relative_to(snap).as_posix()
                                         for path in snap.rglob("*") if path.is_file()})
        for name, item in manifest.items():
            data = (self.root / name).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(item["size"], len(data))
            self.assertEqual((snap / name).read_bytes(), data)
        self.assertEqual(frozen["source_digest"],
                         inventory_source(snap, self.inputs)["digest"])
        self.assertEqual(frozen["source_digest"],
                         inventory_source(self.root, self.inputs)["digest"])
        self.assertRegex(frozen["git_head"], r"\A[0-9a-f]{40,64}\Z")
        self.assertEqual(frozen["changed_paths"], [])
        self.assertEqual(frozen["high_risk_changes"], [])
        self.assertIsNone(frozen["review_digest"])

    def test_only_covered_files_are_frozen_and_unmatched_files_are_ignored(self):
        (self.root / "server/migrations/README.md").write_text("notes\n")
        (self.root / "server/migrations/nested").mkdir()
        (self.root / "server/migrations/nested/deep.sql").write_text("SELECT 2;\n")
        (self.root / "server/src/.eslintrc").write_text("{}\n")
        (self.root / "server/src/.cache").mkdir()
        (self.root / "server/src/.cache/out.ts").write_text("x\n")
        (self.root / "unrelated.txt").write_text("not an input\n")
        frozen = self.freeze()
        names = {item["path"] for item in frozen["files"]}
        self.assertIn("server/migrations/001_foundation.sql", names)
        self.assertIn(".dockerignore", names)
        for ignored in ("server/migrations/README.md", "server/migrations/nested/deep.sql",
                        "server/src/.eslintrc", "server/src/.cache/out.ts", "unrelated.txt"):
            self.assertNotIn(ignored, names)
            self.assertFalse((Path(frozen["snapshot"]) / ignored).exists())

    def test_dirty_and_untracked_inputs_are_frozen_under_git_head_policy(self):
        (self.root / "server/src/main.ts").write_text("export default 2;\n")
        (self.root / "server/src/new-feature.ts").write_text("export const f=1;\n")
        frozen = self.freeze()
        snap = Path(frozen["snapshot"])
        self.assertEqual((snap / "server/src/main.ts").read_text(), "export default 2;\n")
        self.assertEqual((snap / "server/src/new-feature.ts").read_text(), "export const f=1;\n")
        self.assertEqual(frozen["changed_paths"], ["server/src/main.ts", "server/src/new-feature.ts"])
        self.assertEqual(frozen["high_risk_changes"], [])

    def test_symlink_env_git_escape_and_special_files_fail_closed(self):
        outside = self.home / "outside.ts"
        outside.write_text("secret\n")
        cases = {
            "symlink in null dir": lambda: (self.root / "server/src/link.ts").symlink_to(outside),
            "symlink in glob dir": lambda: (self.root / "server/migrations/x.sql").symlink_to(outside),
            "unmatched symlink in glob dir": lambda: (self.root / "contracts/README").symlink_to(outside),
            "env file": lambda: (self.root / "server/src/.env").write_text("TOKEN=1\n"),
            "env variant": lambda: (self.root / "server/src/sub").mkdir() or
                                   (self.root / "server/src/sub/.env.local").write_text("T=1\n"),
            "nested git": lambda: (self.root / "server/src/.git").mkdir(),
            "fifo": lambda: os.mkfifo(self.root / "server/src/pipe.ts"),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                mutate()
                try:
                    self.assertRefused("UNSAFE_BUILD_INPUT")
                finally:
                    for name in ("server/src/link.ts", "server/migrations/x.sql", "contracts/README",
                                 "server/src/.env", "server/src/sub/.env.local",
                                 "server/src/pipe.ts"):
                        path = self.root / name
                        if path.is_symlink() or path.exists():
                            path.unlink()
                    for folder in ("server/src/sub", "server/src/.git"):
                        if (self.root / folder).is_dir():
                            (self.root / folder).rmdir()
                    shutil.rmtree(self.private / "snap", ignore_errors=True)

    def test_symlinked_parent_or_input_escape_fails_closed(self):
        real = self.home / "real-deploy"
        (self.root / "deploy").rename(real)
        (self.root / "deploy").symlink_to(real)
        self.assertRefused("UNSAFE_BUILD_INPUT")
        (self.root / "deploy").unlink()
        real.rename(self.root / "deploy")
        dockerignore = self.root / ".dockerignore"
        dockerignore.unlink()
        dockerignore.symlink_to(self.home / "outside")
        self.assertRefused("UNSAFE_BUILD_INPUT")
        dockerignore.unlink()
        self.assertRefused("SOURCE_INCOMPLETE")

    def test_oversize_input_fails_closed(self):
        with patch("host_runtime.MAX_FILE_BYTES", 8):
            self.assertRefused("UNSAFE_BUILD_INPUT")

    def test_edit_during_freeze_is_detected_after_copy(self):
        real = host_source_review.inventory_source
        calls = []

        def racing(root, inputs, *, copy_to=None):
            result = real(root, inputs, copy_to=copy_to)
            calls.append(copy_to)
            if copy_to is not None:
                (self.root / "server/src/main.ts").write_text("export default 'raced';\n")
            return result

        with patch.object(host_source_review, "inventory_source", racing):
            self.assertRefused("SOURCE_CHANGED")
        self.assertGreaterEqual(len(calls), 2)

    def test_git_head_policy_requires_reviewed_high_risk_bytes(self):
        dockerfile = self.root / "deploy/pc-test/Dockerfile"
        original = dockerfile.read_text()
        dockerfile.write_text("RUN arbitrary-host-action\n")
        self.assertRefused("REVIEW_REQUIRED")
        dockerfile.write_text(original)
        git(self.root, "update-index", "--skip-worktree", "deploy/pc-test/Dockerfile")
        dockerfile.write_text("RUN hidden-change\n")
        self.assertRefused("REVIEW_REQUIRED")
        dockerfile.write_text(original)
        (self.root / "server/migrations/013_staged_reading_access.sql").unlink()
        self.assertRefused("REVIEW_REQUIRED")

    def test_reviewed_snapshot_records_high_risk_changes_privately(self):
        dockerfile = self.root / "deploy/pc-test/Dockerfile"
        head_bytes = dockerfile.read_bytes()
        dockerfile.write_text("FROM node:22\nRUN reviewed-change\n")
        git(self.root, "update-index", "--assume-unchanged", "deploy/pc-test/Dockerfile")
        (self.root / "server/src/main.ts").write_text("export default 3;\n")
        frozen = self.freeze("reviewed_snapshot")
        self.assertEqual(frozen["high_risk_changes"], ["deploy/pc-test/Dockerfile"])
        self.assertIn("server/src/main.ts", frozen["changed_paths"])
        public_text = json.dumps({k: v for k, v in frozen.items() if k != "snapshot"})
        self.assertNotIn("reviewed-change", public_text)
        review_path = self.private / "snap" / "review.json"
        self.assertEqual(stat.S_IMODE(review_path.stat().st_mode), 0o600)
        review_bytes = review_path.read_bytes()
        self.assertEqual(frozen["review_digest"], hashlib.sha256(review_bytes).hexdigest())
        review = json.loads(review_bytes)
        [entry] = review["files"]
        self.assertEqual(entry["path"], "deploy/pc-test/Dockerfile")
        self.assertEqual(entry["head_size"], len(head_bytes))
        self.assertEqual(entry["head_sha256"], hashlib.sha256(head_bytes).hexdigest())
        self.assertRegex(entry["head_blob"], r"\A[0-9a-f]{40,64}\Z")
        snapshot_bytes = (Path(frozen["snapshot"]) / "deploy/pc-test/Dockerfile").read_bytes()
        self.assertEqual(entry["snapshot_sha256"], hashlib.sha256(snapshot_bytes).hexdigest())
        self.assertEqual(entry["snapshot_size"], len(snapshot_bytes))
        self.assertIn("+RUN reviewed-change", entry["diff"])
        self.assertFalse(entry["diff_truncated"])
        self.assertLessEqual(len(review_bytes), host_source_review.MAX_REVIEW_BYTES)

    def test_reviewed_snapshot_without_high_risk_change_has_empty_review(self):
        frozen = self.freeze("reviewed_snapshot")
        self.assertEqual(frozen["high_risk_changes"], [])
        review = json.loads((self.private / "snap" / "review.json").read_bytes())
        self.assertEqual(review["files"], [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_input_is_coded_without_path(self):
        main = self.root / "server/src/main.ts"
        self.addCleanup(main.chmod, 0o644)
        main.chmod(0)
        with self.assertRaises(HostRuntimeError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")
        self.assertNotIn(str(self.home), str(caught.exception) + repr(caught.exception.args))
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse((self.private / "snap").exists())
        with self.assertRaises(HostRuntimeError) as caught:
            inventory_source(self.root, self.inputs)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")

    def test_open_read_race_and_private_write_errors_are_coded(self):
        vanished = FileNotFoundError(2, "No such file", str(self.home / "gone.ts"))
        with patch.object(host_source_review, "_safe_file", side_effect=vanished):
            with self.assertRaises(HostRuntimeError) as caught:
                inventory_source(self.root, self.inputs)
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")
        self.assertIsNone(caught.exception.__cause__)
        full = OSError(28, "No space left on device", str(self.home / "snap"))
        with patch.object(host_source_review, "_write_private", side_effect=full):
            self.assertRefused("STATE_UNAVAILABLE")
        with patch.object(host_source_review, "_save_private_json", side_effect=full):
            self.assertRefused("STATE_UNAVAILABLE", "reviewed_snapshot")
        with patch.object(host_source_review, "verify_reviewed_high_risk_inputs",
                          side_effect=vanished):
            self.assertRefused("SOURCE_UNREADABLE")
        with patch.object(host_source_review, "_head_tree", side_effect=vanished):
            with self.assertRaises(HostRuntimeError) as caught:
                self.freeze()
        self.assertEqual(caught.exception.code, "SOURCE_UNREADABLE")
        self.assertNotIn(str(self.home), str(caught.exception))
        self.assertFalse((self.private / "snap").exists())

    def test_review_artifact_size_is_bounded(self):
        (self.root / "deploy/pc-test/Dockerfile").write_text("RUN reviewed-step\n" * 50)
        with patch.object(host_source_review, "MAX_REVIEW_BYTES", 300):
            self.assertRefused("REVIEW_TOO_LARGE", "reviewed_snapshot")
        self.assertEqual(self.freeze("reviewed_snapshot")["high_risk_changes"],
                         ["deploy/pc-test/Dockerfile"])

    def test_policy_destination_and_inputs_are_validated(self):
        with self.assertRaises(HostRuntimeError):
            self.freeze("none")
        (self.private / "exists").mkdir(mode=0o700)
        with self.assertRaises(HostRuntimeError):
            self.freeze(name="exists")
        with self.assertRaises(HostRuntimeError):
            freeze_source(self.root, self.private / "x", review_policy="git_head",
                          inputs=self.inputs, high_risk_inputs=["not/covered.txt"])

    def test_verify_reviewed_high_risk_inputs_accepts_explicit_paths(self):
        self.assertIsNone(verify_reviewed_high_risk_inputs(self.root))
        self.assertIsNone(verify_reviewed_high_risk_inputs(self.root, paths=["server/src/main.ts"]))
        (self.root / "server/src/main.ts").write_text("changed\n")
        self.assertIsNone(verify_reviewed_high_risk_inputs(self.root))
        with self.assertRaises(HostRuntimeError) as caught:
            verify_reviewed_high_risk_inputs(self.root, paths=["server/src/main.ts"])
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
        self.assertIsNone(verify_reviewed_high_risk_inputs(self.root, paths=[]))


if __name__ == "__main__":
    unittest.main()
