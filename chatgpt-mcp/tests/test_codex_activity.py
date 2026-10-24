import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_activity import CodexActivityReader  # noqa: E402
from project_state import ProjectState  # noqa: E402


THREAD_ID = "0199d5ef-54b1-7112-9394-ea291c75ad21"
NOW = "2026-10-19T05:00:00+00:00"


class CodexActivityReaderTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-activity-test-")
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.session_dir = self.codex_home / "sessions" / "2026" / "10" / "19"
        self.session_dir.mkdir(parents=True)
        self.path = self.session_dir / (
            "rollout-2026-10-19T05-00-00-" + THREAD_ID + ".jsonl"
        )
        self.path.write_text("", encoding="utf-8")
        os.chmod(self.path, 0o664)
        self.thread = {"id": THREAD_ID, "path": str(self.path)}
        self.base = {
            "schema_version": 1,
            "project": "alpha",
            "source_id": "codex-local",
            "source_kind": "codex-cli",
            "actor_id": THREAD_ID,
            "actor_kind": "agent",
            "actor_label": "Codex",
            "task_id": "session-activity",
            "task_kind": "agent",
            "task_label": "Codex session active",
            "action": "execute",
            "state": "running",
            "model": "private-model-context",
            "usage": {"input_tokens": 999},
        }
        self.reader = CodexActivityReader(self.codex_home)

    def tearDown(self):
        self.temporary.cleanup()

    def append(self, envelope, *, newline=True):
        with self.path.open("ab") as stream:
            stream.write(json.dumps(envelope, separators=(",", ":")).encode())
            if newline:
                stream.write(b"\n")

    @staticmethod
    def response(payload, timestamp="2026-10-19T04:59:59+00:00"):
        return {"timestamp": timestamp, "ordinal": 1, "type": "response_item", "payload": payload}

    def baseline(self):
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

    def test_new_source_baselines_at_eof_then_pairs_tool_request_and_output(self):
        self.append(self.response({
            "type": "function_call", "namespace": "collaboration", "name": "send_message",
            "call_id": "call-1", "arguments": "private prompt", "status": "completed",
        }))

        self.baseline()
        self.append(self.response({
            "type": "function_call", "namespace": "collaboration", "name": "send_message",
            "call_id": "call-2", "arguments": "private prompt", "status": "completed",
        }, "2026-10-19T05:00:01+00:00"))
        self.append(self.response({
            "type": "function_call_output", "call_id": "call-2", "output": "private output",
        }, "2026-10-19T05:00:02+00:00"))

        events = self.reader.read(self.thread, self.base, now="2026-10-19T05:00:03+00:00")

        self.assertEqual([event["state"] for event in events], ["queued", "returned"])
        self.assertEqual([event["task_id"] for event in events], ["tool:call-2", "tool:call-2"])
        self.assertEqual(events[0]["tool_name"], "collaboration:send_message")
        self.assertEqual(events[0]["task_label"], "Codex 도구 호출 요청")
        self.assertEqual(events[0]["parent_task_id"], "session-activity")
        rendered = json.dumps(events, ensure_ascii=False)
        for private in ("private prompt", "private output", "private-model-context", "input_tokens"):
            self.assertNotIn(private, rendered)

    def test_partial_line_is_emitted_once_after_completion(self):
        self.baseline()
        envelope = self.response({
            "type": "custom_tool_call", "name": "exec", "call_id": "partial-1",
            "input": "private command", "status": "completed",
        }, "2026-10-19T05:00:01+00:00")
        encoded = json.dumps(envelope, separators=(",", ":")).encode()
        midpoint = len(encoded) // 2
        with self.path.open("ab") as stream:
            stream.write(encoded[:midpoint])
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])
        self.assertNotIn("private command", repr(self.reader._states))
        self.assertNotIn("buffer", vars(next(iter(self.reader._states.values()))))
        with self.path.open("ab") as stream:
            stream.write(encoded[midpoint:] + b"\n")

        first = self.reader.read(self.thread, self.base, now="2026-10-19T05:00:02+00:00")
        second = self.reader.read(self.thread, self.base, now="2026-10-19T05:00:03+00:00")

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["task_id"], "tool:partial-1")
        self.assertEqual(second, [])

    def test_completed_native_items_emit_only_verified_terminal_events(self):
        self.baseline()
        fixtures = [
            ({"type": "CommandExecution", "id": "cmd-ok", "status": "completed", "exit_code": 0,
              "command": "private"}, "returned", "execute", "CommandExecution"),
            ({"type": "CommandExecution", "id": "cmd-bad", "status": "failed", "exit_code": 7,
              "aggregated_output": "private"}, "failed", "execute", "CommandExecution"),
            ({"type": "FileChange", "id": "file-1", "status": "completed", "changes": "private"},
             "returned", "code", "FileChange"),
            ({"type": "McpToolCall", "id": "mcp-1", "status": "interrupted",
              "server": "figma", "tool": "inspect", "arguments": "private"},
             "interrupted", "execute", "figma:inspect"),
        ]
        for index, (item, _state, _action, _tool) in enumerate(fixtures, 1):
            self.append({
                "timestamp": f"2026-10-19T05:00:0{index}+00:00", "type": "event_msg",
                "payload": {"type": "item_completed", "thread_id": THREAD_ID, "item": item},
            })

        events = self.reader.read(self.thread, self.base, now="2026-10-19T05:00:10+00:00")

        self.assertEqual(
            [(event["state"], event["action"], event["tool_name"]) for event in events],
            [(state, action, tool) for _item, state, action, tool in fixtures],
        )
        self.assertTrue(all(event["state"] not in {"started", "running", "succeeded"}
                            for event in events))
        store_root = self.root / "repo"
        store_root.mkdir()
        store = ProjectState({"alpha": store_root}, self.root / "state")
        result = store.record_work_events("alpha", events, provenance="observed")
        self.assertEqual(result["accepted"], 4)

    def test_wrong_thread_future_malformed_and_unpaired_output_are_ignored(self):
        self.baseline()
        rows = [
            {"timestamp": "2026-10-19T05:00:01+00:00", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": "wrong-thread",
                "item": {"type": "CommandExecution", "id": "wrong", "status": "completed", "exit_code": 0}}},
            self.response({"type": "function_call_output", "call_id": "missing", "output": "secret"},
                          "2026-10-19T05:00:01+00:00"),
            self.response({"type": "function_call", "name": "exec", "call_id": "future",
                           "arguments": "secret"}, "2027-10-19T05:00:01+00:00"),
        ]
        with self.path.open("ab") as stream:
            stream.write(b"not-json\n")
            for row in rows:
                stream.write(json.dumps(row).encode() + b"\n")

        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

    def test_nonterminal_item_does_not_block_later_completed_record(self):
        self.baseline()
        running = {"timestamp": "2026-10-19T05:00:01+00:00", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": THREAD_ID,
            "item": {"type": "CommandExecution", "id": "cmd-later", "status": "running"}}}
        self.append(running)
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])
        running["timestamp"] = "2026-10-19T05:00:02+00:00"
        running["payload"]["item"].update({"status": "completed", "exit_code": 0})
        self.append(running)

        events = self.reader.read(self.thread, self.base, now="2026-10-19T05:00:03+00:00")

        self.assertEqual([(event["task_id"], event["state"]) for event in events],
            [("item:cmd-later", "returned")])

    def test_identity_change_discards_pending_pair_and_baselines_again(self):
        self.baseline()
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "old-id"},
                                  "2026-10-19T05:00:01+00:00"))
        self.assertEqual(len(self.reader.read(self.thread, self.base, now=NOW)), 1)
        changed = {**self.base, "project": "beta", "source_id": "codex-secondary"}
        self.append(self.response({"type": "custom_tool_call_output", "call_id": "old-id",
                                   "output": "private"}, "2026-10-19T05:00:02+00:00"))

        self.assertEqual(self.reader.read(self.thread, changed, now=NOW), [])
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "new-id"},
                                  "2026-10-19T05:00:03+00:00"))
        events = self.reader.read(self.thread, changed, now="2026-10-19T05:00:04+00:00")
        self.assertEqual([(event["project"], event["task_id"]) for event in events],
                         [("beta", "tool:new-id")])

    def test_rotation_copytruncate_and_oversized_line_do_not_replay_or_stall(self):
        self.baseline()
        with self.path.open("ab") as stream:
            stream.write(b"{" + b"x" * 70000 + b"}\n")
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "after-big"},
                                  "2026-10-19T05:00:01+00:00"))
        self.assertEqual([event["task_id"] for event in self.reader.read(
            self.thread, self.base, now="2026-10-19T05:00:02+00:00")], ["tool:after-big"])

        self.path.write_text("", encoding="utf-8")
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "old-copy"}))
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "new-copy"},
                                  "2026-10-19T05:00:01+00:00"))
        self.assertEqual([event["task_id"] for event in self.reader.read(
            self.thread, self.base, now="2026-10-19T05:00:02+00:00")], ["tool:new-copy"])

        replacement_body = json.dumps(self.response({
            "type": "custom_tool_call", "name": "exec", "call_id": "same-size-copy",
        })) + " " * 1000 + "\n" + json.dumps(self.response({
            "type": "custom_tool_call", "name": "exec", "call_id": "copied-tail",
        })) + "\n"
        self.path.write_text(replacement_body, encoding="utf-8")
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "after-copy"},
                                  "2026-10-19T05:00:01+00:00"))
        self.assertEqual([event["task_id"] for event in self.reader.read(
            self.thread, self.base, now="2026-10-19T05:00:02+00:00")], ["tool:after-copy"])

        replacement = self.path.with_suffix(".replacement")
        replacement.write_text(json.dumps(self.response({
            "type": "custom_tool_call", "name": "exec", "call_id": "old-rotate"})) + "\n",
                               encoding="utf-8")
        os.replace(replacement, self.path)
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

    def test_rejects_escape_symlink_wrong_owner_metadata_and_forgets_unloaded_threads(self):
        self.baseline()
        outside = self.root / ("rollout-now-" + THREAD_ID + ".jsonl")
        outside.write_text("", encoding="utf-8")
        bad_threads = [
            {"id": THREAD_ID, "path": str(outside)},
            {"id": THREAD_ID, "path": str(self.session_dir / "wrong-name.jsonl")},
            {"id": "wrong", "path": str(self.path)},
        ]
        symlink = self.session_dir / ("rollout-link-" + THREAD_ID + ".jsonl")
        symlink.symlink_to(self.path)
        bad_threads.append({"id": THREAD_ID, "path": str(symlink)})
        for thread in bad_threads:
            self.assertEqual(self.reader.read(thread, self.base, now=NOW), [])
        with mock.patch("codex_activity.os.getuid", return_value=os.getuid() + 1):
            self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

        self.reader.forget_unloaded([])
        self.append(self.response({"type": "custom_tool_call", "name": "exec", "call_id": "history"}))
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

    def test_rejects_parent_traversal_nul_and_fifo_without_blocking(self):
        escape = self.codex_home / "escape" / self.path.name
        escape.parent.mkdir()
        escape.write_text("", encoding="utf-8")
        traversal = str(self.codex_home / "sessions" / ".." / "escape" / self.path.name)
        traversal_reader = CodexActivityReader(self.codex_home)
        traversal_thread = {"id": THREAD_ID, "path": traversal}
        self.assertEqual(traversal_reader.read(traversal_thread, self.base, now=NOW), [])
        with escape.open("ab") as stream:
            stream.write(json.dumps(self.response({
                "type": "custom_tool_call", "name": "exec", "call_id": "escaped",
            })).encode() + b"\n")
        self.assertEqual(traversal_reader.read(traversal_thread, self.base, now=NOW), [])
        self.assertEqual(self.reader.read(
            {"id": THREAD_ID, "path": str(self.path) + "\0suffix"}, self.base, now=NOW), [])

        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

    def test_validation_errors_close_opened_file_and_directory_descriptors(self):
        real_fstat = os.fstat
        for failed_type in (stat.S_ISREG, stat.S_ISDIR):
            with self.subTest(failed_type=failed_type.__name__):
                baseline_descriptors = set(os.listdir("/proc/self/fd"))

                def fail_for_selected_type(descriptor):
                    info = real_fstat(descriptor)
                    if failed_type(info.st_mode):
                        raise OSError("simulated validation race")
                    return info

                with mock.patch("codex_activity.os.fstat", side_effect=fail_for_selected_type):
                    self.assertEqual(self.reader.read(self.thread, self.base, now=NOW), [])

                self.assertEqual(set(os.listdir("/proc/self/fd")), baseline_descriptors)

    def test_initial_partial_line_is_discarded_before_new_complete_record(self):
        history = json.dumps(self.response({
            "type": "custom_tool_call", "name": "exec", "call_id": "history-partial",
        })).encode()
        split = len(history) - 5
        with self.path.open("ab") as stream:
            stream.write(history[:split])
        self.baseline()
        with self.path.open("ab") as stream:
            stream.write(history[split:] + b"\n")
        self.append(self.response({"type": "custom_tool_call", "name": "exec",
                                   "call_id": "fresh"}, "2026-10-19T05:00:01+00:00"))

        events = self.reader.read(self.thread, self.base, now=NOW)

        self.assertEqual([event["task_id"] for event in events], ["tool:fresh"])

    def test_unsafe_json_and_huge_exit_code_do_not_poison_valid_batch(self):
        self.baseline()
        with self.path.open("ab") as stream:
            stream.write(("[" * 2000 + "]" * 2000 + "\n").encode())
        self.append({
            "timestamp": "2026-10-19T05:00:01+00:00", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": THREAD_ID, "item": {
                    "type": "CommandExecution", "id": "huge-exit", "status": "failed",
                    "exit_code": 10 ** 30,
                }},
        })

        events = self.reader.read(self.thread, self.base, now=NOW)

        self.assertEqual([(event["task_id"], event["state"]) for event in events],
                         [("item:huge-exit", "failed")])
        self.assertNotIn("exit_code", events[0])
        store_root = self.root / "safe-repo"
        store_root.mkdir()
        store = ProjectState({"alpha": store_root}, self.root / "safe-state")
        self.assertEqual(store.record_work_events(
            "alpha", events, provenance="observed")["accepted"], 1)

    def test_unhashable_discriminators_are_skipped_before_later_valid_row(self):
        self.baseline()
        malformed = [
            self.response({"type": [], "name": "exec", "call_id": "bad"}),
            {"timestamp": "2026-10-19T05:00:01+00:00", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": THREAD_ID,
                "item": {"type": {}, "id": "bad", "status": []}}},
            {"timestamp": "2026-10-19T05:00:01+00:00", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": THREAD_ID,
                "item": {"type": "CommandExecution", "id": "bad-status", "status": []}}},
        ]
        with self.path.open("ab") as stream:
            for row in malformed:
                stream.write(json.dumps(row).encode() + b"\n")
        self.append(self.response({"type": "custom_tool_call", "name": "exec",
                                   "call_id": "valid-after-bad"},
                                  "2026-10-19T05:00:02+00:00"))

        events = self.reader.read(self.thread, self.base, now=NOW)

        self.assertEqual([event["task_id"] for event in events], ["tool:valid-after-bad"])


if __name__ == "__main__":
    unittest.main()
