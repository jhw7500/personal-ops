"""Tests for local-only opt-in helper; never touches real home."""
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "configure-host-runtime.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("configure_host_runtime", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TTY(io.StringIO):
    def isatty(self):
        return True


class ConfigureHostRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "host-runtime.json"

    def test_non_tty_denied_without_file(self):
        with self.assertRaisesRegex(RuntimeError, "HOST_TTY_REQUIRED"):
            module.enable(self.path, input_stream=io.StringIO("ENABLE\n"), output=TTY())
        self.assertFalse(self.path.exists())

    def test_rejection_does_not_write(self):
        with self.assertRaisesRegex(RuntimeError, "OPT_IN_DECLINED"):
            module.enable(self.path, input_stream=TTY("NO\n"), output=TTY())
        self.assertFalse(self.path.exists())

    def test_explicit_enable_idempotent(self):
        self.assertEqual(module.enable(self.path, input_stream=TTY("ENABLE\n"), output=TTY()), "enabled")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(module.load_config(self.path))
        self.assertEqual(module.enable(self.path, input_stream=TTY(""), output=TTY()), "already_enabled")

    def test_symlink_is_rejected(self):
        target = Path(self.tmp.name) / "other"
        target.write_text("do not modify")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "UNSAFE_CONFIG_PATH"):
            module.enable(self.path, input_stream=TTY("ENABLE\n"), output=TTY())
        self.assertEqual(target.read_text(), "do not modify")

    def test_extended_opt_in_is_separate_and_does_not_downgrade(self):
        module.enable(self.path, input_stream=TTY("ENABLE\n"), output=TTY())
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "OPT_IN_DECLINED"):
            module.enable(self.path, extended=True, input_stream=TTY("ENABLE\n"), output=TTY())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(module.enable(self.path, extended=True,
                                      input_stream=TTY("ENABLE EXTENDED\n"), output=TTY()),
                         "enabled_extended")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["targets"]["tarororo-test"]["allowed_components"],
                         ["api", "worker", "migration"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        upgraded = self.path.read_bytes()
        self.assertEqual(module.enable(self.path, input_stream=TTY(""), output=TTY()),
                         "already_enabled")
        self.assertEqual(self.path.read_bytes(), upgraded)

    def test_config_changed_while_operator_reads_prompt_is_not_overwritten(self):
        module.enable(self.path, input_stream=TTY("ENABLE\n"), output=TTY())
        path = self.path
        class ChangedTTY(TTY):
            def readline(self, *args):
                data = json.loads(path.read_text())
                data["enabled"] = False
                path.write_text(json.dumps(data))
                return "ENABLE EXTENDED\n"
        with self.assertRaisesRegex(RuntimeError, "CONFIG_CHANGED_DURING_APPROVAL"):
            module.enable(self.path, extended=True, input_stream=ChangedTTY(), output=TTY())
        self.assertFalse(json.loads(self.path.read_text())["enabled"])

    def write_projects(self):
        root = Path(self.tmp.name) / "tossApp"
        root.mkdir()
        projects = Path(self.tmp.name) / "projects.json"
        projects.write_text(json.dumps({"projects": {"tossApp": str(root)}}))
        return root, projects

    def files(self):
        return {p.name: (p.read_bytes(), p.stat().st_mode)
                for p in Path(self.tmp.name).iterdir() if p.is_file() or p.is_symlink()}

    def test_v3_candidate_is_validated_and_diffed_without_writes(self):
        from host_operation_fixtures import fixture_v3
        module.enable(self.path, extended=True, input_stream=TTY("ENABLE EXTENDED\n"), output=TTY())
        root, projects = self.write_projects()
        candidate = Path(self.tmp.name) / "candidate.json"
        payload = fixture_v3(root)
        candidate.write_text(json.dumps(payload))
        before = self.files()
        output = io.StringIO()
        self.assertEqual(module.validate_candidate(candidate, current=self.path,
                                                   projects_config=projects, output=output), "valid")
        text = output.getvalue()
        self.assertIn("--- current", text)
        self.assertIn("+++ candidate", text)
        self.assertIn('-  "version": 2', text)
        self.assertIn('+  "version": 3', text)
        self.assertIn("not applied", text)
        self.assertEqual(self.files(), before)
        missing = io.StringIO()
        self.assertEqual(module.validate_candidate(candidate, current=Path(self.tmp.name) / "absent",
                                                   projects_config=projects, output=missing), "valid")
        self.assertIn("Current: missing", missing.getvalue())
        bad = json.loads(json.dumps(payload))
        bad["targets"]["tarororo-test"]["environment"] = "production"
        legacy_only = {"version": 2, "enabled": True, "targets": payload["legacy"]["targets"]}
        link = Path(self.tmp.name) / "link.json"
        link.symlink_to(candidate)
        for content, path, code in ((bad, candidate, "INVALID_HOST_CONFIG"),
                                    (legacy_only, candidate, "UNSUPPORTED_CONFIG_VERSION"),
                                    (payload, link, "UNSAFE_CANDIDATE_PATH")):
            with self.subTest(code):
                candidate.write_text(json.dumps(content))
                before = self.files()
                with self.assertRaisesRegex(Exception, f"^{code}$"):
                    module.validate_candidate(path, current=self.path, projects_config=projects,
                                              output=io.StringIO())
                self.assertEqual(self.files(), before)

    def test_fifo_candidate_is_rejected_without_blocking(self):
        _, projects = self.write_projects()
        fifo = Path(self.tmp.name) / "candidate.fifo"
        os.mkfifo(fifo)
        result = {}

        def validate():
            try:
                module.validate_candidate(fifo, current=self.path, projects_config=projects,
                                          output=io.StringIO())
            except Exception as exc:
                result["error"] = str(exc)

        worker = threading.Thread(target=validate, daemon=True)
        worker.start()
        worker.join(5)
        blocked = worker.is_alive()
        if blocked:  # Release the blocked reader so the failing run still ends.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(5)
        self.assertFalse(blocked)
        self.assertEqual(result.get("error"), "UNSAFE_CANDIDATE_PATH")

    def test_enable_never_overwrites_v3_config(self):
        from host_operation_fixtures import fixture_v3
        root, _ = self.write_projects()
        v1 = {"version": 1, "enabled": True, "targets": {"tarororo-test": module.TARGET_CONFIG}}
        for legacy, extended, answer in ((None, False, "ENABLE\n"),
                                         (v1, True, "ENABLE EXTENDED\n")):
            with self.subTest(extended=extended):
                self.path.write_text(json.dumps(dict(fixture_v3(root), legacy=legacy)))
                self.path.chmod(0o600)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "UNSUPPORTED_CONFIG_VERSION"):
                    module.enable(self.path, extended=extended, input_stream=TTY(answer),
                                  output=TTY())
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
