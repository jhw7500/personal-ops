import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_state import ProjectState  # noqa: E402


SCRIPT = Path(__file__).resolve().parents[1] / "codex_hook_report.py"


class CodexHookReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-hook-report-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "repo"
        self.project.mkdir()
        self.config = self.root / "projects.json"
        self.config.write_text(json.dumps({"projects": {"alpha": str(self.project)}}))
        self.state = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def run_hook(self, raw):
        return subprocess.run([sys.executable, str(SCRIPT), "--projects", str(self.config),
                               "--state-dir", str(self.state)], input=raw,
                              capture_output=True, timeout=10)

    def test_real_hook_process_persists_only_observed_metadata(self):
        payload = {"hook_event_name": "PreToolUse", "session_id": "session-1",
                   "cwd": str(self.project), "tool_name": "apply_patch", "tool_use_id": "tool-1",
                   "model": "codex-model", "prompt": "private-sentinel",
                   "tool_input": {"patch": "private-sentinel"}, "tool_response": "private-sentinel"}
        result = self.run_hook(json.dumps(payload).encode())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(result.stderr, b"")
        records = ProjectState({"alpha": self.project}, self.state).list_work_events("alpha")["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provenance"], "observed")
        self.assertEqual(records[0]["action"], "code")
        self.assertEqual(records[0]["model_source"], "event")
        self.assertNotIn("private-sentinel", json.dumps(records))

    def test_invalid_oversized_and_unconfigured_inputs_do_not_block_codex(self):
        for raw in (b"not-json-private-sentinel", b"x" * (256 * 1024 + 1), b"{}"):
            with self.subTest(length=len(raw)):
                result = self.run_hook(raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"{}\n")
                self.assertEqual(result.stderr, b"")
        self.config.unlink()
        result = self.run_hook(b"{}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(result.stderr, b"codex-hook configuration unavailable\n")
        self.assertFalse((self.state / "work-events.jsonl").exists())

    def test_storage_failure_has_a_fixed_diagnostic_without_echoing_private_input(self):
        self.state.write_text("not-a-directory")
        payload = {"hook_event_name": "PreToolUse", "session_id": "session-1",
                   "cwd": str(self.project), "tool_name": "apply_patch", "tool_use_id": "tool-1",
                   "tool_input": "private-sentinel"}
        result = self.run_hook(json.dumps(payload).encode())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(result.stderr, b"codex-hook storage unavailable\n")
