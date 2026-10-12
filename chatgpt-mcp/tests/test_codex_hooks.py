import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_hooks import normalize_hook  # noqa: E402
from project_state import ProjectState  # noqa: E402


NOW = "2026-10-12T09:30:00+00:00"


class CodexHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-hook-test-")
        self.root = Path(self.temporary.name)
        self.alpha = self.root / "repos" / "alpha"
        self.nested = self.alpha / "packages" / "nested"
        self.beta = self.root / "repos" / "beta"
        self.nested.mkdir(parents=True)
        self.beta.mkdir(parents=True)
        self.projects = {
            "alpha": self.alpha,
            "nested": self.nested,
            "beta": self.beta,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self, event, **overrides):
        return {
            "hook_event_name": event,
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": str(self.alpha),
            **overrides,
        }

    def test_pre_tool_use_emits_private_metadata_only_and_records_as_observed(self):
        payload = self.payload(
            "PreToolUse",
            agent_id="agent-7",
            agent_type="explore",
            model="gpt-5.6-sol",
            tool_name="apply_patch",
            tool_use_id="call-9",
            prompt="private prompt",
            tool_input={"patch": "secret"},
            tool_response={"output": "secret"},
            last_assistant_message="private answer",
            transcript_path="/private/transcript.jsonl",
            future_field="must not survive",
        )

        result = normalize_hook(payload, self.projects, time=NOW)

        self.assertEqual(len(result), 1)
        project, event = result[0]
        self.assertEqual(project, "alpha")
        self.assertEqual(event, {
            "schema_version": 1,
            "project": "alpha",
            "source_id": "codex-local",
            "source_kind": "codex-cli",
            "actor_id": "agent-7",
            "actor_kind": "agent",
            "actor_label": "explore",
            "task_id": "tool:call-9",
            "task_kind": "call",
            "task_label": "apply_patch",
            "action": "code",
            "state": "started",
            "time": NOW,
            "model": "gpt-5.6-sol",
        })
        rendered = json.dumps(event)
        for private_value in (
            "private prompt", "secret", "private answer", "transcript.jsonl",
            "must not survive",
        ):
            self.assertNotIn(private_value, rendered)

        store = ProjectState(self.projects, self.root / "state")
        accepted = store.record_work_events(project, [event], provenance="observed")
        saved = store.list_work_events(project)["records"][0]
        self.assertEqual(accepted["accepted"], 1)
        self.assertEqual(saved["provenance"], "observed")
        self.assertEqual(saved["actor_id"], "agent-7")

    def test_post_tool_use_is_returned_even_when_response_looks_successful(self):
        payload = self.payload(
            "PostToolUse",
            tool_name="shell",
            tool_use_id="call-2",
            tool_input={"command": "npm test"},
            tool_response={"exit_code": 0, "status": "success"},
        )

        _, event = normalize_hook(payload, self.projects, time=NOW)[0]

        self.assertEqual(event["actor_id"], "session-1")
        self.assertNotIn("parent_actor_id", event)
        self.assertEqual(event["action"], "execute")
        self.assertEqual(event["state"], "returned")
        self.assertNotIn("exit_code", event)

    def test_tool_action_uses_only_known_tool_name_and_never_parses_arguments(self):
        cases = [
            ("read_file", {"path": "x"}, "read"),
            ("rg", {"query": "needle"}, "search"),
            ("apply_patch", {"patch": "x"}, "code"),
            ("shell", {"command": "pytest"}, "execute"),
            ("mystery", {"command": "git commit && pytest"}, "execute"),
        ]
        for tool_name, tool_input, expected in cases:
            with self.subTest(tool_name=tool_name):
                payload = self.payload(
                    "PreToolUse",
                    tool_name=tool_name,
                    tool_use_id="call-3",
                    tool_input=tool_input,
                )
                _, event = normalize_hook(payload, self.projects, time=NOW)[0]
                self.assertEqual(event["action"], expected)

    def test_turn_events_use_supplied_turn_id_without_claiming_success(self):
        cases = [
            ("UserPromptSubmit", "started"),
            ("Stop", "returned"),
            ("Interrupt", "interrupted"),
        ]
        for hook_name, expected_state in cases:
            with self.subTest(hook_name=hook_name):
                _, event = normalize_hook(
                    self.payload(hook_name, turn_id="turn-44"), self.projects, time=NOW,
                )[0]
                self.assertEqual(event["task_id"], "turn:turn-44")
                self.assertEqual(event["task_kind"], "call")
                self.assertEqual(event["task_label"], "Codex turn")
                self.assertEqual(event["action"], "execute")
                self.assertEqual(event["state"], expected_state)
                self.assertNotEqual(event["state"], "succeeded")

    def test_subagent_events_keep_child_identity_without_inventing_immediate_parent(self):
        start = self.payload(
            "SubagentStart", agent_id="child-3", agent_type="reviewer", turn_id="turn-8",
        )
        stop = {**start, "hook_event_name": "SubagentStop"}

        _, started = normalize_hook(start, self.projects, time=NOW)[0]
        _, returned = normalize_hook(stop, self.projects, time=NOW)[0]

        self.assertEqual(started["actor_id"], "child-3")
        self.assertNotIn("parent_actor_id", started)
        self.assertEqual(started["actor_label"], "reviewer")
        self.assertEqual(started["task_id"], "agent:child-3")
        self.assertEqual(started["state"], "started")
        self.assertEqual(returned["state"], "returned")
        self.assertNotEqual(returned["state"], "succeeded")

    def test_canonical_cwd_selects_the_deepest_allowed_project(self):
        alias = self.root / "nested-alias"
        alias.symlink_to(self.nested, target_is_directory=True)
        child = alias / "src"
        child.mkdir()

        result = normalize_hook(
            self.payload(
                "PreToolUse", cwd=str(child), tool_name="read_file", tool_use_id="call-5",
            ),
            self.projects,
            time=NOW,
        )

        self.assertEqual(result[0][0], "nested")
        self.assertEqual(result[0][1]["project"], "nested")

    def test_model_is_metadata_only_when_nonempty_and_valid(self):
        root = self.payload("UserPromptSubmit", model="gpt-5.6-sol")
        child = self.payload(
            "SubagentStart", model="gpt-5.6-sol", agent_id="child-4",
            agent_type="gpt-5.6-sol",
        )
        _, root_event = normalize_hook(root, self.projects, time=NOW)[0]
        _, child_event = normalize_hook(child, self.projects, time=NOW)[0]

        self.assertEqual(root_event["actor_id"], "session-1")
        self.assertEqual(root_event["actor_label"], "Codex")
        self.assertEqual(root_event["model"], "gpt-5.6-sol")
        self.assertEqual(child_event["actor_id"], "child-4")
        self.assertEqual(child_event["actor_label"], "gpt-5.6-sol")

        for empty_model in (None, "", 0, False):
            with self.subTest(empty_model=empty_model):
                _, event = normalize_hook(
                    self.payload("UserPromptSubmit", model=empty_model),
                    self.projects,
                    time=NOW,
                )[0]
                self.assertNotIn("model", event)

    def test_invalid_or_incomplete_payloads_are_ignored_safely(self):
        outside = self.root / "outside"
        outside.mkdir()
        cases = [
            None,
            [],
            self.payload("UnknownEvent"),
            self.payload("PreToolUse", cwd=str(outside), tool_name="shell", tool_use_id="call-1"),
            self.payload("PreToolUse", cwd="", tool_name="shell", tool_use_id="call-1"),
            self.payload("PreToolUse", cwd=str(self.root / "missing"), tool_name="shell", tool_use_id="call-1"),
            self.payload("PreToolUse", session_id="", tool_name="shell", tool_use_id="call-1"),
            self.payload("PreToolUse", session_id="bad/id", tool_name="shell", tool_use_id="call-1"),
            self.payload("PreToolUse", tool_name="", tool_use_id="call-1"),
            self.payload("PreToolUse", tool_name="shell", tool_use_id=0),
            self.payload("UserPromptSubmit", turn_id=""),
            self.payload("SubagentStart", agent_id=""),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_hook(payload, self.projects, time=NOW), [])

        valid = self.payload("UserPromptSubmit")
        self.assertEqual(normalize_hook(valid, self.projects, source_id="bad/id", time=NOW), [])
        self.assertEqual(normalize_hook(valid, self.projects, time="not-a-time"), [])


if __name__ == "__main__":
    unittest.main()
