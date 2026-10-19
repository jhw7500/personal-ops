"""Unavailable registered worktrees must not disable every available repository."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import load_projects  # noqa: E402


class ProjectLoadingTests(unittest.TestCase):
    def test_missing_worktree_keeps_existing_projects_available_and_registration_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            available = base / "available"
            available.mkdir()
            missing = base / "deleted-worktree"
            config = base / "projects.json"
            config.write_text(json.dumps({"projects": {
                "available": str(available), "removed-worktree": str(missing)}}))
            original = config.read_bytes()
            logs = io.StringIO()
            with contextlib.redirect_stderr(logs):
                loaded = load_projects(config)
            self.assertEqual(loaded, {"available": available.resolve()})
            self.assertEqual(config.read_bytes(), original)
            event = json.loads(logs.getvalue())
            self.assertEqual(event["project"], "removed-worktree")
            self.assertEqual(event["reason"], "DIRECTORY_MISSING")
            self.assertNotIn(str(missing), logs.getvalue())

    def test_malformed_names_relative_paths_and_non_directories_still_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            available = base / "available"
            available.mkdir()
            ordinary_file = base / "ordinary-file"
            ordinary_file.write_text("not a directory")
            config = base / "projects.json"
            for name, path in (("../invalid", str(base / "missing")),
                               ("relative", "relative/path"),
                               ("file", str(ordinary_file)), ("invalid", None)):
                with self.subTest(name=name):
                    config.write_text(json.dumps({"projects": {
                        "available": str(available), name: path}}))
                    with self.assertRaises(ValueError):
                        load_projects(config)

    def test_no_available_projects_cannot_start(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "projects.json"
            config.write_text(json.dumps({"projects": {
                "missing": str(Path(folder) / "missing")}}))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                load_projects(config)


if __name__ == "__main__":
    unittest.main()
