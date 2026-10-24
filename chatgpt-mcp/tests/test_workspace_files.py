import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_files import WorkspaceFiles, is_ignored  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class WorkspaceFilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-files-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "projects"
        self.project = self.base / "alpha"
        self.project.mkdir(parents=True)
        self.owner = self.root / "owner"
        self.owner.mkdir(mode=0o700)
        self.config = self.owner / "workspace-policy.json"
        self.config.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.base),
            "artifacts": [{
                "project": "alpha", "path": "build/results", "kind": "directory",
                "operations": ["list", "read"], "fields": ["status"],
                "recursive": False, "max_file_bytes": 1048576,
            }],
            "protected_paths": [str(self.project / "protected")],
        }))
        self.config.chmod(0o600)
        self.tools = WorkspaceTools(
            {"alpha": self.project}, self.owner / "authority", config_path=self.config
        )
        snapshot = self.tools.policy.load(required=True)
        self.tools.authority.initialize(snapshot.digest, allowed_releases={"a" * 64: 1})
        self.files = WorkspaceFiles(self.tools)

    def call(self, name, **arguments):
        result = self.files.call(name, {"project": "alpha", **arguments})
        return result, result.get("structuredContent", {})

    def test_gitignore_parent_nested_and_negation_semantics(self):
        (self.project / ".gitignore").write_text(
            "build/\n*.log\n!keep.log\nnested/*.txt\nblocked/\n"
        )
        nested = self.project / "nested"
        nested.mkdir()
        (nested / ".gitignore").write_text("!visible.txt\nsecret.bin\n")
        blocked = self.project / "blocked"
        blocked.mkdir()
        (blocked / ".gitignore").write_text("!visible.txt\n")

        self.assertTrue(is_ignored(self.project, "build/result.json"))
        self.assertTrue(is_ignored(self.project, "drop.log"))
        self.assertFalse(is_ignored(self.project, "keep.log"))
        self.assertFalse(is_ignored(self.project, "nested/visible.txt"))
        self.assertTrue(is_ignored(self.project, "nested/secret.bin"))
        self.assertTrue(is_ignored(self.project, "blocked/visible.txt"))

    def test_directory_only_gitignore_rule_hides_directory_entry_and_contents(self):
        (self.project / ".gitignore").write_text("build/\n")
        build = self.project / "build"
        build.mkdir()
        (build / "result.txt").write_text("hidden")
        (self.project / "visible.txt").write_text("visible")

        result, payload = self.call("list_dir", relative_path=".", recursive=True)

        self.assertFalse(result["isError"])
        paths = [row["path"] for row in payload["entries"]]
        self.assertEqual(paths, [".gitignore", "visible.txt"])

    def test_read_is_bounded_and_registered_artifact_is_not_a_raw_escape(self):
        (self.project / "source.txt").write_text("one\ntwo\nthree\n")
        result, payload = self.call(
            "read_file", relative_path="source.txt", start_line=1, end_line=2,
            max_answer_chars=4,
        )
        self.assertFalse(result["isError"])
        self.assertEqual(payload["content"], "two\n")
        artifact = self.project / "build/results/result.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"status":"private","secret":"must-not-leak"}')
        denied, payload = self.call("read_file", relative_path="build/results/result.json")
        self.assertTrue(denied["isError"])
        self.assertNotIn("must-not-leak", json.dumps(denied))
        self.assertNotIn("content", payload)

    def test_recursive_list_find_and_search_skip_ignored_sensitive_and_links(self):
        (self.project / ".gitignore").write_text("ignored/\n")
        (self.project / "visible").mkdir()
        (self.project / "visible/code.py").write_text("before needle after\n")
        (self.project / "visible/notes.txt").write_text("needle notes\n")
        (self.project / "ignored").mkdir()
        (self.project / "ignored/hidden.py").write_text("needle hidden\n")
        (self.project / ".env").write_text("needle secret\n")
        (self.project / "outside").symlink_to(self.root, target_is_directory=True)

        listed, listing = self.call("list_dir", relative_path=".", recursive=True)
        found, matches = self.call("find_file", relative_path=".", file_mask="*.py")
        searched, search = self.call(
            "search_for_pattern", substring_pattern="needle", restrict_search_to_code_files=True
        )

        self.assertFalse(listed["isError"])
        self.assertIn("visible/code.py", [row["path"] for row in listing["entries"]])
        directory = next(row for row in listing["entries"] if row["path"] == "visible")
        self.assertEqual(directory["type"], "directory")
        self.assertFalse(found["isError"])
        self.assertEqual(matches["files"], ["visible/code.py"])
        self.assertFalse(searched["isError"])
        self.assertEqual({row["path"] for row in search["matches"]}, {"visible/code.py"})
        combined = json.dumps([listed, found, searched])
        self.assertNotIn("hidden.py", combined)
        self.assertNotIn("needle secret", combined)

    def test_recursive_operations_omit_protected_descendants_before_descent(self):
        protected = self.project / "protected"
        protected.mkdir()
        (protected / "hidden.py").write_text("needle must-not-leak\n")
        (self.project / "visible.py").write_text("needle public\n")

        listed, listing = self.call("list_dir", relative_path=".", recursive=True)
        found, matches = self.call("find_file", relative_path=".", file_mask="*.py")
        searched, search = self.call(
            "search_for_pattern", substring_pattern="needle",
        )

        self.assertFalse(listed["isError"])
        self.assertFalse(found["isError"])
        self.assertFalse(searched["isError"])
        combined = json.dumps([listing, matches, search])
        self.assertIn("visible.py", combined)
        self.assertNotIn("protected", combined)
        self.assertNotIn("must-not-leak", combined)

    def test_create_and_literal_replace_preserve_mode_and_require_occurrence_rules(self):
        created, payload = self.call(
            "create_text_file", relative_path="new.txt", content="alpha alpha"
        )
        self.assertFalse(created["isError"])
        self.assertEqual(stat.S_IMODE((self.project / "new.txt").stat().st_mode), 0o600)
        overwritten, _ = self.call(
            "create_text_file", relative_path="new.txt", content="overwritten"
        )
        self.assertFalse(overwritten["isError"])
        self.assertEqual((self.project / "new.txt").read_text(), "overwritten")
        self.assertEqual(stat.S_IMODE((self.project / "new.txt").stat().st_mode), 0o600)
        target = self.project / "run.sh"
        target.write_text("echo old\n")
        target.chmod(0o755)
        replaced, payload = self.call(
            "replace_content", relative_path="run.sh", needle="old", repl="new",
            mode="literal", allow_multiple_occurrences=False,
        )
        self.assertFalse(replaced["isError"])
        self.assertEqual(target.read_text(), "echo new\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        denied, _ = self.call(
            "replace_content", relative_path="new.txt", needle="t", repl="beta",
            mode="literal", allow_multiple_occurrences=False,
        )
        self.assertTrue(denied["isError"])
        self.assertEqual((self.project / "new.txt").read_text(), "overwritten")

    def test_write_rechecks_policy_inside_admission_before_mutation(self):
        original = self.tools.authority.admission

        @contextmanager
        def changed_policy(_digest):
            document = json.loads(self.config.read_text())
            document["protected_paths"].append(str(self.project / "new.txt"))
            self.config.write_text(json.dumps(document))
            self.config.chmod(0o600)
            with original(_digest) as binding:
                yield binding

        with patch.object(self.tools.authority, "admission", changed_policy):
            denied, payload = self.call(
                "create_text_file", relative_path="new.txt", content="must-not-write",
            )

        self.assertTrue(denied["isError"])
        self.assertEqual(payload["code"], "WORKSPACE_POLICY_STALE")
        self.assertFalse((self.project / "new.txt").exists())

    def test_write_rejects_ignored_sensitive_and_parent_symlink_paths(self):
        (self.project / ".gitignore").write_text("ignored.txt\n")
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / "link").symlink_to(outside, target_is_directory=True)
        for relative in ("ignored.txt", ".env", "link/new.txt", "build/results/new.json"):
            with self.subTest(relative=relative):
                result, _ = self.call(
                    "create_text_file", relative_path=relative, content="must-not-write"
                )
                self.assertTrue(result["isError"])
        self.assertFalse((outside / "new.txt").exists())

    def test_regex_risk_and_answer_bounds_are_enforced(self):
        (self.project / "many.txt").write_text("a" * 100 + "!\nneedle long result\n")
        denied, _ = self.call(
            "search_for_pattern", substring_pattern="(a+)+$", max_answer_chars=32
        )
        self.assertTrue(denied["isError"])
        result, payload = self.call(
            "search_for_pattern", substring_pattern="needle", max_answer_chars=6
        )
        self.assertFalse(result["isError"])
        self.assertLessEqual(len(payload["matches"][0]["text"]), 6)
        listed, payload = self.call(
            "list_dir", relative_path=".", recursive=True, max_answer_chars=8
        )
        self.assertFalse(listed["isError"])
        self.assertTrue(payload["truncated"])

    def test_adversarial_regex_search_and_replace_are_time_bounded(self):
        target = self.project / "adversarial.txt"
        target.write_text("a" * 200000 + "!\n")
        started = time.monotonic()

        for pattern in ("(a|aa)+$", "a+z"):
            with self.subTest(pattern=pattern):
                denied, payload = self.call(
                    "search_for_pattern", substring_pattern=pattern,
                )
                self.assertTrue(denied["isError"])
                self.assertEqual(payload["code"], "WORKSPACE_PATTERN_TIMEOUT")
        denied, payload = self.call(
            "replace_content", relative_path="adversarial.txt",
            needle="(a|aa)+$", repl="x", mode="regex",
            allow_multiple_occurrences=False,
        )

        self.assertTrue(denied["isError"])
        self.assertEqual(payload["code"], "WORKSPACE_PATTERN_TIMEOUT")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(target.read_text(), "a" * 200000 + "!\n")

    def test_bounded_regex_preserves_capture_replacement_and_file_mode(self):
        target = self.project / "versions.txt"
        target.write_text("old-12 old-34\n")
        target.chmod(0o640)

        result, payload = self.call(
            "replace_content", relative_path="versions.txt",
            needle=r"(old)-(\d+)", repl=r"\2-\1", mode="regex",
            allow_multiple_occurrences=True,
        )

        self.assertFalse(result["isError"], payload)
        self.assertEqual(target.read_text(), "12-old 34-old\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
