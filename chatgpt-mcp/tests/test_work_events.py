import json
import multiprocessing
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_state import MAX_WORK_EVENT_BYTES, ProjectState, sanitize_work_event  # noqa: E402


BASE_EVENT = {
    "schema_version": 1,
    "source_id": "codex-local",
    "source_kind": "codex-cli",
    "actor_id": "session-1-agent-1",
    "actor_kind": "agent",
    "actor_label": "Codex",
    "task_id": "work-1",
    "task_kind": "agent",
    "task_label": "Implement persistence",
    "action": "code",
    "state": "running",
    "time": "2026-10-12T01:00:00+00:00",
}


def concurrent_record_work_event(projects, state_dir, task_id, gate):
    store = ProjectState(projects, state_dir)
    gate.wait()
    store.record_work_event("alpha", {**BASE_EVENT, "task_id": task_id})


class WorkEventTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="work-event-test-")
        self.root = Path(self.temporary.name)
        self.alpha = self.root / "repos" / "alpha"
        self.beta = self.root / "repos" / "beta"
        self.alpha.mkdir(parents=True)
        self.beta.mkdir()
        self.projects = {"alpha": self.alpha, "beta": self.beta}
        self.state_dir = self.root / "state" / "chatgpt-coding-mcp"
        self.store = ProjectState(self.projects, self.state_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_keeps_all_phases_newest_first_without_deduplication(self):
        first = self.store.record_work_event("alpha", BASE_EVENT)
        finished = {**BASE_EVENT, "state": "succeeded", "time": "2026-10-12T01:00:02+00:00"}
        second = self.store.record_work_event("alpha", finished)
        middle = {**BASE_EVENT, "state": "running", "time": "2026-10-12T01:00:01+00:00"}
        third = self.store.record_work_event("alpha", middle)

        records = ProjectState(self.projects, self.state_dir).list_work_events("alpha")["records"]

        self.assertEqual(records, [second, third, first])
        self.assertEqual([record["state"] for record in records],
                         ["succeeded", "running", "running"])
        self.assertEqual(first["provenance"], "reported")
        self.assertIn("received_at", first)
        self.assertEqual(first["expires_at"], "2026-10-12T01:02:00+00:00")
        self.assertEqual(stat.S_IMODE(self.store.work_event_path.stat().st_mode), 0o600)

    def test_sanitizer_filters_unknown_fields_and_does_not_mutate_input(self):
        event = {
            **BASE_EVENT,
            "project": "alpha",
            "parent_task_id": "parent-task",
            "parent_actor_id": "parent-actor",
            "parent_source_id": "parent-source",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "usage_scope": "turn",
            "usage": {"input_tokens": 0, "output_tokens": 7, "secret": "hidden"},
            "exit_code": 0,
            "provenance": "observed",
            "prompt": "password=do-not-store",
            "args": ["--token", "secret"],
            "results": "secret output",
            "reasoning": "private chain",
            "env": {"TOKEN": "secret"},
            "tokens": "secret",
        }
        original = json.loads(json.dumps(event))

        saved = sanitize_work_event(
            "alpha", event, provenance="reported", now="2026-10-12T01:00:30+00:00",
        )

        self.assertEqual(event, original)
        self.assertEqual(saved["provenance"], "reported")
        self.assertEqual(saved["received_at"], "2026-10-12T01:00:30+00:00")
        self.assertEqual(saved["usage"], {"input_tokens": 0, "output_tokens": 7})
        self.assertEqual(saved["usage_scope"], "turn")
        self.assertEqual(saved["exit_code"], 0)
        self.assertEqual(saved["parent_task_id"], "parent-task")
        self.assertEqual(saved["parent_actor_id"], "parent-actor")
        self.assertEqual(saved["parent_source_id"], "parent-source")
        self.assertTrue({"prompt", "args", "results", "reasoning", "env", "tokens"}.isdisjoint(saved))
        self.assertNotIn("secret", json.dumps(saved))

    def test_optional_activity_metadata_round_trips_without_changing_provenance(self):
        roles = ["analysis", "research", "planning", "debugging", "review", "summary"]
        events = [{
            **BASE_EVENT,
            "task_id": f"role-{role}",
            "action": "search" if role == "research" else "execute",
            "activity_role": role,
            **({"search_scope": "web"} if role == "research" else {}),
            "tool_name": "web_search" if role == "research" else "functions.exec",
        } for role in roles]

        result = self.store.record_work_events("alpha", events, provenance="observed")
        records = self.store.list_work_events("alpha")["records"]

        self.assertEqual(result["provenance"], "observed")
        self.assertEqual({record["activity_role"] for record in records}, set(roles))
        self.assertEqual({record["provenance"] for record in records}, {"observed"})
        research = next(record for record in records if record["activity_role"] == "research")
        self.assertEqual(research["search_scope"], "web")
        self.assertEqual(research["tool_name"], "web_search")
        planning = next(record for record in records if record["activity_role"] == "planning")
        self.assertEqual(planning["action"], "execute")
        self.assertEqual(planning["tool_name"], "functions.exec")

    def test_explicit_skill_metadata_round_trips_without_copying_private_inputs(self):
        event = {
            **BASE_EVENT,
            "task_kind": "call",
            "state": "started",
            "skill_id": "plugin.example:review-pr",
            "skill_name": "PR 검토",
            "tool_input": {"skill": "plugin.example:review-pr", "args": "private"},
            "prompt": "private prompt",
            "response": "private response",
        }

        saved = self.store.record_work_event("alpha", event, provenance="reported")

        self.assertEqual(saved["skill_id"], "plugin.example:review-pr")
        self.assertEqual(saved["skill_name"], "PR 검토")
        self.assertEqual(saved["provenance"], "reported")
        self.assertTrue({"tool_input", "prompt", "response"}.isdisjoint(saved))
        self.assertNotIn("private", json.dumps(saved))

    def test_labels_require_utf8_and_batch_rejects_before_persistence(self):
        unicode_event = {
            **BASE_EVENT,
            "actor_label": "코덱스 🧑‍💻",
            "task_label": "상태 저장 ✅",
        }
        saved = sanitize_work_event("alpha", unicode_event)
        self.assertEqual(saved["actor_label"], "코덱스 🧑‍💻")
        self.assertEqual(saved["task_label"], "상태 저장 ✅")

        malformed = {**BASE_EVENT, "task_label": "bad\ud800label"}
        with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
            sanitize_work_event("alpha", malformed)

        isolated = ProjectState(self.projects, self.root / "isolated-surrogate-state")
        with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
            isolated.record_work_events("alpha", [BASE_EVENT, malformed])
        self.assertFalse(isolated.work_event_path.exists())

    def test_active_time_defaults_to_receipt_and_expiry_is_bounded(self):
        without_time = {key: value for key, value in BASE_EVENT.items() if key != "time"}
        saved = sanitize_work_event(
            "alpha", without_time, provenance="observed",
            now="2026-10-12T01:00:30+00:00",
        )
        self.assertEqual(saved["time"], "2026-10-12T01:00:30+00:00")
        self.assertEqual(saved["expires_at"], "2026-10-12T01:02:30+00:00")

        accepted = sanitize_work_event("alpha", {
            **BASE_EVENT, "expires_at": "2026-10-12T01:02:00+00:00",
        }, now="2026-10-12T01:00:30+00:00")
        self.assertEqual(accepted["expires_at"], "2026-10-12T01:02:00+00:00")
        already_expired = sanitize_work_event("alpha", {
            **BASE_EVENT, "expires_at": "2026-10-12T00:59:59+00:00",
        }, now="2026-10-12T01:00:30+00:00")
        self.assertEqual(already_expired["expires_at"], "2026-10-12T00:59:59+00:00")

        with self.assertRaisesRegex(ValueError, "Invalid work event"):
            sanitize_work_event("alpha", {
                **BASE_EVENT, "expires_at": "2026-10-12T01:02:01+00:00",
            }, now="2026-10-12T01:00:30+00:00")

    def test_job_success_requires_actual_zero_exit_code(self):
        job = {**BASE_EVENT, "task_kind": "job", "state": "succeeded"}
        self.assertEqual(sanitize_work_event("alpha", job)["state"], "unknown")
        self.assertEqual(sanitize_work_event(
            "alpha", {**job, "exit_code": 9})["state"], "unknown")
        succeeded = sanitize_work_event("alpha", {**job, "exit_code": 0})
        self.assertEqual(succeeded["state"], "succeeded")
        self.assertEqual(succeeded["exit_code"], 0)

    def test_future_event_time_cannot_extend_active_liveness(self):
        future = sanitize_work_event("alpha", {
            **BASE_EVENT, "time": "2026-10-12T01:01:00+00:00",
        }, now="2026-10-12T01:00:30+00:00")
        self.assertEqual(future["expires_at"], "2026-10-12T01:02:30+00:00")

        with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
            sanitize_work_event("alpha", {
                **BASE_EVENT, "time": "2026-10-12T01:05:31+00:00",
            }, now="2026-10-12T01:00:30+00:00")
        with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
            sanitize_work_event("alpha", {
                **BASE_EVENT,
                "time": "2026-10-12T01:01:00+00:00",
                "expires_at": "2026-10-12T01:02:31+00:00",
            }, now="2026-10-12T01:00:30+00:00")

    def test_rejects_invalid_contract_values_with_generic_errors(self):
        invalid = [
            ({key: value for key, value in BASE_EVENT.items() if key != "actor_label"}, {}),
            ({**BASE_EVENT, "schema_version": 2}, {}),
            ({**BASE_EVENT, "source_id": "unsafe/id"}, {}),
            ({**BASE_EVENT, "actor_id": ""}, {}),
            ({**BASE_EVENT, "task_id": "x" * 81}, {}),
            ({**BASE_EVENT, "parent_task_id": "unsafe/id"}, {}),
            ({**BASE_EVENT, "source_kind": "browser"}, {}),
            ({**BASE_EVENT, "actor_kind": "worker"}, {}),
            ({**BASE_EVENT, "task_kind": "call", "state": "succeeded"}, {}),
            ({**BASE_EVENT, "task_kind": "job", "state": "returned"}, {}),
            ({**BASE_EVENT, "task_kind": "agent", "state": "returned"}, {}),
            ({**BASE_EVENT, "state": "done"}, {}),
            ({**BASE_EVENT, "action": "prompt"}, {}),
            ({**BASE_EVENT, "time": "2026-10-12T01:00:00"}, {}),
            ({**BASE_EVENT, "project": "beta"}, {}),
            ({**BASE_EVENT, "usage": {"input_tokens": 1}}, {}),
            ({**BASE_EVENT, "usage": {"input_tokens": True}, "usage_scope": "turn"}, {}),
            ({**BASE_EVENT, "usage": {"total_tokens": 9007199254740992}, "usage_scope": "turn"}, {}),
            ({**BASE_EVENT, "usage": {"input_tokens": 1}, "usage_scope": "response"}, {}),
            ({**BASE_EVENT, "model": "model name"}, {}),
            ({**BASE_EVENT, "reasoning_effort": "x" * 33}, {}),
            ({**BASE_EVENT, "activity_role": "implementation"}, {}),
            ({**BASE_EVENT, "activity_role": ["analysis"]}, {}),
            ({**BASE_EVENT, "search_scope": "web"}, {}),
            ({**BASE_EVENT, "action": "search", "search_scope": "workspace"}, {}),
            ({**BASE_EVENT, "action": "search", "search_scope": 1}, {}),
            ({**BASE_EVENT, "tool_name": ""}, {}),
            ({**BASE_EVENT, "tool_name": "bad tool"}, {}),
            ({**BASE_EVENT, "tool_name": "x" * 121}, {}),
            ({**BASE_EVENT, "tool_name": 1}, {}),
            ({**BASE_EVENT, "skill_id": "bad/skill"}, {}),
            ({**BASE_EVENT, "skill_id": "x" * 121}, {}),
            ({**BASE_EVENT, "skill_id": 1}, {}),
            ({**BASE_EVENT, "skill_name": "Review"}, {}),
            ({**BASE_EVENT, "skill_id": "review", "skill_name": "bad\nlabel"}, {}),
            ({**BASE_EVENT, "skill_id": "review", "skill_name": "x" * 81}, {}),
            ({**BASE_EVENT, "task_kind": "agent", "skill_id": "review"}, {}),
            ({**BASE_EVENT, "task_kind": "job", "skill_id": "review"}, {}),
            ({**BASE_EVENT, "task_kind": "agent", "skill_id": "review",
              "skill_name": "Review"}, {}),
            (BASE_EVENT, {"provenance": "inferred"}),
        ]
        for event, arguments in invalid:
            with self.subTest(event=event, arguments=arguments):
                with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
                    sanitize_work_event("alpha", event, **arguments)

        with self.assertRaisesRegex(ValueError, "An allowed project is required"):
            self.store.record_work_event("unknown", BASE_EVENT)

    def test_cross_process_writers_do_not_drop_records(self):
        context = multiprocessing.get_context("fork")
        gate = context.Barrier(4)
        processes = [context.Process(
            target=concurrent_record_work_event,
            args=(self.projects, self.state_dir, f"task-{index}", gate),
        ) for index in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

        records = self.store.list_work_events("alpha")["records"]
        self.assertEqual(len(records), 4)
        self.assertEqual({record["task_id"] for record in records},
                         {"task-0", "task-1", "task-2", "task-3"})

    def test_batch_validates_every_item_before_writing_and_enforces_bounds(self):
        events = [
            {**BASE_EVENT, "task_id": "task-1"},
            {**BASE_EVENT, "task_id": "task-2", "state": "failed"},
        ]
        result = self.store.record_work_events("alpha", events, provenance="observed")
        self.assertEqual(result, {
            "project": "alpha", "accepted": 2, "provenance": "observed",
        })
        records = self.store.list_work_events("alpha")["records"]
        self.assertEqual({record["task_id"] for record in records}, {"task-1", "task-2"})
        self.assertEqual({record["received_at"] for record in records},
                         {records[0]["received_at"]})

        isolated_state = self.root / "isolated-state"
        isolated = ProjectState(self.projects, isolated_state)
        with self.assertRaisesRegex(ValueError, "^Invalid work event$"):
            isolated.record_work_events("alpha", [BASE_EVENT, {**BASE_EVENT, "task_id": "bad/id"}])
        self.assertFalse(isolated.work_event_path.exists())
        for invalid in ([], [BASE_EVENT] * 21, BASE_EVENT, None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "^Invalid work event batch$"):
                    isolated.record_work_events("alpha", invalid)

    def test_limit_rotation_and_unsafe_files_are_enforced(self):
        self.store.work_event_path.write_bytes(b" " * MAX_WORK_EVENT_BYTES)
        self.store.work_event_path.chmod(0o600)
        saved = self.store.record_work_event("alpha", BASE_EVENT)
        self.assertTrue(self.store.previous_work_event_path.exists())
        self.assertEqual(self.store.list_work_events("alpha", limit=1)["records"], [saved])
        for limit in (0, True, 1001):
            with self.assertRaises(ValueError):
                self.store.list_work_events("alpha", limit)

        self.store.work_event_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.record_work_event("alpha", BASE_EVENT)

        self.store.work_event_path.unlink()
        self.store.work_event_path.symlink_to(self.root / "outside.jsonl")
        with self.assertRaises(ValueError):
            self.store.record_work_event("alpha", BASE_EVENT)

    def test_read_skips_malformed_or_noncanonical_records_without_relabeling(self):
        observed = self.store.record_work_event("alpha", BASE_EVENT, provenance="observed")
        with self.store.work_event_path.open("a", encoding="utf-8") as stream:
            stream.write("not json\n")
            stream.write(json.dumps({**observed, "prompt": "must not escape"}) + "\n")
            stream.write(json.dumps({**observed, "source_id": "unsafe/id"}) + "\n")
            stream.write(json.dumps({**observed, "source_kind": ["codex-cli"]}) + "\n")
            stream.write(json.dumps({**observed, "state": ["running"]}) + "\n")
            stream.write(json.dumps({**observed, "usage": {"input_tokens": 1},
                                     "usage_scope": ["turn"]}) + "\n")
            stream.write(json.dumps({**observed, "provenance": ["observed"]}) + "\n")
            stream.write(json.dumps({**observed, "task_label": "bad\ud800label"}) + "\n")
            stream.write(json.dumps({**observed, "received_at": "9999-12-31T23:59:59+00:00",
                                     "time": "9999-12-31T23:59:59+00:00"}) + "\n")
            stream.write(json.dumps(observed) + "\n")

        listed = self.store.list_work_events("alpha")

        self.assertEqual(listed["records"], [observed, observed])
        self.assertEqual(listed["records"][0]["provenance"], "observed")
        self.assertEqual(listed["records"][0]["received_at"], observed["received_at"])


if __name__ == "__main__":
    unittest.main()
