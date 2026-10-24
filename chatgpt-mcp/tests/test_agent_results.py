"""Delegated source CAS: synthetic files only, no actual user repository."""
from __future__ import annotations
import hashlib
from pathlib import Path
import tempfile
import unittest

from agent_results import apply_agent_result
from host_runtime_config import HostRuntimeError


def entry(relative, data, mode=0o644):
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}


class AgentCASPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-cas-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "live"
        self.snapshot = self.root / "snapshot"
        for base in (self.live, self.snapshot):
            (base / "src").mkdir(parents=True)
            (base / "src/view.py").write_bytes(b"before\n")
            (base / "src/view.py").chmod(0o644)
        self.task = {"apply_policy": "cas", "allowed_paths": ["src"],
                     "file_manifest": [entry("src/view.py", b"before\n")]}

    def test_preview_only_never_writes(self):
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual((self.live / "src/view.py").read_bytes(), b"before\n")

    def test_single_file_cas_applies_after_preimage_match(self):
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["applied_paths"], ["src/view.py"])
        self.assertEqual((self.live / "src/view.py").read_bytes(), b"after\n")

    def test_single_file_cas_preserves_group_write_mode(self):
        target = self.live / "src/view.py"
        target.chmod(0o664)
        self.task["file_manifest"][0]["mode"] = 0o664
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(target.stat().st_mode & 0o777, 0o664)

    def test_concurrent_edit_is_not_overwritten(self):
        (self.live / "src/view.py").write_bytes(b"other session\n")
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["conflicts"], ["src/view.py"])
        self.assertEqual((self.live / "src/view.py").read_bytes(), b"other session\n")

    def test_multiple_file_change_is_review_only(self):
        for base in (self.live, self.snapshot):
            (base / "src/second.py").write_bytes(b"second\n")
        self.task["file_manifest"].append(entry("src/second.py", b"second\n"))
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        (self.snapshot / "src/second.py").write_bytes(b"new\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual((self.live / "src/view.py").read_bytes(), b"before\n")
        self.assertEqual((self.live / "src/second.py").read_bytes(), b"second\n")

    def test_symbolic_link_is_blocked(self):
        (self.live / "src/view.py").unlink()
        (self.live / "src/view.py").symlink_to(self.root / "outside")
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["conflicts"], ["src/view.py"])
        self.assertTrue((self.live / "src/view.py").is_symlink())

    def test_directory_link_is_blocked(self):
        (self.live / "src").rename(self.live / "src_backup")
        (self.live / "src").symlink_to(self.live / "src_backup", target_is_directory=True)
        (self.snapshot / "src/view.py").write_bytes(b"after\n")
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual((self.live / "src_backup/view.py").read_bytes(), b"before\n")

    def test_deletion_does_not_apply(self):
        (self.snapshot / "src/view.py").unlink()
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertEqual(result["review_required_paths"], ["src/view.py"])
        self.assertEqual((self.live / "src/view.py").read_bytes(), b"before\n")

    def test_high_risk_package_json_is_never_autoapplied(self):
        # Even if a profile owns a broad directory, package.json is high risk.
        for base in (self.live, self.snapshot):
            (base / "src/package.json").write_bytes(b'{"v":1}\n')
        self.task["file_manifest"].append(entry("src/package.json", b'{"v":1}\n'))
        (self.snapshot / "src/package.json").write_bytes(b'{"v":2}\n')
        result = apply_agent_result(self.task, self.snapshot, self.live, apply=True)
        self.assertIn("src/package.json", result["protected"])
        self.assertEqual((self.live / "src/package.json").read_bytes(), b'{"v":1}\n')

    def test_parent_traversal_is_rejected_in_manifest(self):
        self.task["file_manifest"][0]["path"] = "../secret"
        with self.assertRaisesRegex(HostRuntimeError, "PLAN_INVALID"):
            apply_agent_result(self.task, self.snapshot, self.live, apply=True)


if __name__ == "__main__":
    unittest.main()
