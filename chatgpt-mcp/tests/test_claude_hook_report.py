import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_state import ProjectState  # noqa: E402


SCRIPT = Path(__file__).resolve().parents[1] / "claude_hook_report.py"
SESSION = "11111111-1111-4111-8111-111111111111"


class ClaudeHookReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-hook-report-")
        self.root = Path(self.temporary.name)
        self.project = self.root / "repo"
        self.project.mkdir()
        self.config = self.root / "projects.json"
        self.config.write_text(json.dumps({"projects": {"alpha": str(self.project)}}))
        self.state = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def run_hook(self, payload, *, timeout=5):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--projects", str(self.config),
             "--state-dir", str(self.state)],
            input=raw, capture_output=True, timeout=timeout,
        )

    def records(self):
        return ProjectState({"alpha": self.project}, self.state).list_work_events(
            "alpha"
        )["records"]

    def test_real_hook_process_records_documented_metadata_without_private_fields(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "agent_id": "22222222-2222-2222-2222-222222222222",
            "agent_type": "reviewer",
            "cwd": str(self.project),
            "tool_name": "Edit",
            "tool_use_id": "tool-1",
            "effort": {"level": "high"},
            "prompt": "private-sentinel",
            "transcript_path": "/private/private-sentinel.jsonl",
            "tool_input": {"file_path": "private-sentinel"},
            "tool_response": {"content": "private-sentinel"},
            "env": {"SECRET": "private-sentinel"},
        }

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_id"], "claude-local")
        self.assertEqual(records[0]["source_kind"], "claude-cli")
        self.assertEqual(
            records[0]["actor_id"],
            f"sub:{SESSION}:22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(records[0]["actor_label"], "reviewer")
        self.assertEqual(records[0]["task_id"], "tool:tool-1")
        self.assertEqual(records[0]["action"], "code")
        self.assertEqual(records[0]["state"], "started")
        self.assertEqual(records[0]["reasoning_effort"], "high")
        self.assertEqual(records[0]["reasoning_effort_source"], "event")
        self.assertEqual(records[0]["provenance"], "observed")
        self.assertNotIn("private-sentinel", json.dumps(records))

    def test_session_start_persists_model_without_active_work(self):
        result = self.run_hook({
            "hook_event_name": "SessionStart", "session_id": SESSION,
            "cwd": str(self.project), "model": "claude-opus-4-1",
            "effort": {"level": "high"},
        })

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        record = self.records()[0]
        self.assertEqual(record["task_kind"], "agent")
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["model"], "claude-opus-4-1")
        self.assertEqual(record["model_source"], "event")
        self.assertEqual(record["reasoning_effort"], "high")
        self.assertEqual(record["reasoning_effort_source"], "event")

    def test_invalid_oversized_and_unconfigured_inputs_do_not_block_claude(self):
        for raw in (b"not-json-private-sentinel", b"x" * (256 * 1024 + 1), b"{}"):
            with self.subTest(length=len(raw)):
                result = self.run_hook(raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")
        self.config.unlink()
        result = self.run_hook(b"{}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-hook configuration unavailable\n")
        self.assertFalse((self.state / "work-events.jsonl").exists())

    def test_storage_failure_has_fixed_diagnostic_without_private_input(self):
        self.state.write_text("not-a-directory")
        payload = {
            "hook_event_name": "PreToolUse", "session_id": SESSION,
            "cwd": str(self.project), "tool_name": "Edit", "tool_use_id": "tool-1",
            "tool_input": "private-sentinel",
        }

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-hook storage unavailable\n")
        self.assertNotIn(b"private-sentinel", result.stderr)

    def test_blocked_state_lock_is_interrupted_by_internal_deadline(self):
        store = ProjectState({"alpha": self.project}, self.state)
        lock_path = store.lock_dir / "work-events.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        payload = {
            "hook_event_name": "PreToolUse", "session_id": SESSION,
            "cwd": str(self.project), "tool_name": "Edit", "tool_use_id": "tool-1",
        }
        started = time.monotonic()
        try:
            try:
                result = self.run_hook(payload, timeout=2.5)
            except subprocess.TimeoutExpired:
                self.fail("Claude hook reporter exceeded its internal deadline")
        finally:
            os.close(descriptor)

        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-hook storage unavailable\n")


if __name__ == "__main__":
    unittest.main()
