import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claude_hooks import normalize_hook  # noqa: E402
from project_state import ProjectState  # noqa: E402


NOW = "2026-10-12T09:30:00+00:00"
SESSION = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"


class ClaudeHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-hook-test-")
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
            "session_id": SESSION,
            "cwd": str(self.alpha),
            **overrides,
        }

    def test_session_start_records_metadata_without_claiming_active_work(self):
        payload = self.payload(
            "SessionStart",
            model="claude-opus-4-1",
            effort={"level": "xhigh", "future": "private-sentinel"},
            agent_type="security-reviewer",
            source="startup",
            transcript_path="/private/private-sentinel.jsonl",
            future_field="private-sentinel",
        )

        project, event = normalize_hook(payload, self.projects, time=NOW)[0]

        self.assertEqual(project, "alpha")
        self.assertEqual(event, {
            "schema_version": 1,
            "project": "alpha",
            "source_id": "claude-local",
            "source_kind": "claude-cli",
            "actor_id": SESSION,
            "actor_kind": "agent",
            "actor_label": "security-reviewer",
            "task_id": f"session:{SESSION}",
            "task_kind": "agent",
            "task_label": "Claude session activity",
            "action": "execute",
            "state": "unknown",
            "time": NOW,
            "model": "claude-opus-4-1",
            "reasoning_effort": "xhigh",
        })
        self.assertNotIn("private-sentinel", json.dumps(event))

    def test_prompt_events_correlate_only_on_supplied_prompt_id(self):
        for hook_name, state in (
            ("UserPromptSubmit", "started"),
            ("Stop", "returned"),
            ("StopFailure", "failed"),
        ):
            with self.subTest(hook_name=hook_name):
                _, event = normalize_hook(
                    self.payload(hook_name, prompt_id="prompt-44", prompt="private"),
                    self.projects, time=NOW,
                )[0]
                self.assertEqual(event["task_id"], "prompt:prompt-44")
                self.assertEqual(event["task_label"], "Claude prompt")
                self.assertEqual(event["state"], state)
                self.assertNotIn("private", json.dumps(event))

        _, generic = normalize_hook(
            self.payload("UserPromptSubmit", prompt="private"), self.projects, time=NOW,
        )[0]
        self.assertEqual(generic["task_id"], f"session:{SESSION}")
        self.assertEqual(generic["task_label"], "Claude session activity")
        self.assertEqual(generic["task_kind"], "agent")
        self.assertEqual(generic["state"], "running")

        _, stopped = normalize_hook(
            self.payload("Stop"), self.projects, time=NOW,
        )[0]
        _, failed = normalize_hook(
            self.payload("StopFailure", error="private-sentinel"), self.projects, time=NOW,
        )[0]
        self.assertEqual(stopped["task_id"], generic["task_id"])
        self.assertEqual(stopped["task_kind"], "agent")
        self.assertEqual(stopped["state"], "unknown")
        self.assertEqual(failed["task_id"], generic["task_id"])
        self.assertEqual(failed["task_kind"], "agent")
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("private-sentinel", json.dumps(failed))

    def test_tool_lifecycle_requires_tool_id_and_never_claims_success(self):
        cases = [
            ("PreToolUse", {}, "started"),
            ("PostToolUse", {"tool_response": {"ok": True}}, "returned"),
            ("PostToolUseFailure", {"error": "private-sentinel"}, "failed"),
            ("PostToolUseFailure", {"error": "private-sentinel", "is_interrupt": True},
             "interrupted"),
        ]
        for hook_name, extra, expected in cases:
            with self.subTest(hook_name=hook_name, expected=expected):
                _, event = normalize_hook(
                    self.payload(
                        hook_name, tool_name="Bash", tool_use_id="tool-9",
                        tool_input={"command": "private-sentinel"}, **extra,
                    ),
                    self.projects, time=NOW,
                )[0]
                self.assertEqual(event["task_id"], "tool:tool-9")
                self.assertEqual(event["state"], expected)
                self.assertEqual(event["tool_name"], "Bash")
                self.assertNotEqual(event["state"], "succeeded")
                self.assertNotIn("private-sentinel", json.dumps(event))

        self.assertEqual(normalize_hook(
            self.payload("PostToolUseFailure", tool_name="Bash", error="private"),
            self.projects, time=NOW,
        ), [])

    def test_skill_tool_extracts_only_valid_explicit_identifier_across_lifecycle(self):
        cases = [
            ("PreToolUse", {}, "started"),
            ("PostToolUse", {"tool_response": {"secret": "private-response"}}, "returned"),
            ("PostToolUseFailure", {"error": "private-error"}, "failed"),
            ("PostToolUseFailure", {"error": "private-error", "is_interrupt": True},
             "interrupted"),
        ]
        for hook_name, extra, state in cases:
            with self.subTest(hook_name=hook_name, state=state):
                _, event = normalize_hook(self.payload(
                    hook_name,
                    tool_name="Skill",
                    tool_use_id="skill-call-7",
                    tool_input={
                        "skill": "plugin.example:review-pr",
                        "args": "private-arguments",
                        "prompt": "private-prompt",
                    },
                    **extra,
                ), self.projects, time=NOW)[0]

                self.assertEqual(event["task_id"], "tool:skill-call-7")
                self.assertEqual(event["state"], state)
                self.assertEqual(event["skill_id"], "plugin.example:review-pr")
                self.assertNotIn("skill_name", event)
                self.assertNotEqual(event["state"], "succeeded")
                self.assertNotIn("private", json.dumps(event))

    def test_skill_metadata_is_omitted_for_other_tools_or_invalid_skill_input(self):
        cases = [
            ("Bash", {"skill": "plugin.example:review-pr"}),
            ("Skill", None),
            ("Skill", {}),
            ("Skill", {"skill": "bad/skill"}),
            ("Skill", {"skill": "x" * 121}),
            ("Skill", {"skill": 7}),
        ]
        for tool_name, tool_input in cases:
            with self.subTest(tool_name=tool_name, tool_input=tool_input):
                _, event = normalize_hook(self.payload(
                    "PreToolUse", tool_name=tool_name, tool_use_id="tool-11",
                    tool_input=tool_input,
                ), self.projects, time=NOW)[0]

                self.assertNotIn("skill_id", event)
                self.assertNotIn("skill_name", event)

    def test_tool_metadata_distinguishes_web_file_generic_and_unknown_search_names(self):
        cases = [
            ("Read", "read", None),
            ("Find", "search", "files"),
            ("Find_File", "search", "files"),
            ("Glob", "search", "files"),
            ("Grep", "search", "files"),
            ("rg", "search", "files"),
            ("search_for_pattern", "search", "files"),
            ("search", "search", None),
            ("WebSearch", "search", "web"),
            ("web_search", "search", "web"),
            ("web_search_preview", "search", "web"),
            ("web.run", "execute", None),
            ("web__run", "execute", None),
            ("Edit", "code", None),
            ("NotebookEdit", "code", None),
            ("Bash", "execute", None),
            ("Mystery", "execute", None),
        ]
        for tool_name, expected_action, expected_scope in cases:
            with self.subTest(tool_name=tool_name):
                _, event = normalize_hook(
                    self.payload(
                        "PreToolUse", tool_name=tool_name, tool_use_id="tool-3",
                        tool_input={"command": "rg WebSearch && git commit && pytest"},
                    ),
                    self.projects, time=NOW,
                )[0]
                self.assertEqual(event["action"], expected_action)
                self.assertEqual(event.get("search_scope"), expected_scope)
                self.assertEqual(event["tool_name"], tool_name)
                self.assertNotIn("activity_role", event)

    def test_tool_name_metadata_is_omitted_when_name_is_not_a_bounded_ascii_identifier(self):
        for tool_name in ("도구", "x" * 121):
            with self.subTest(tool_name=tool_name):
                _, event = normalize_hook(
                    self.payload("PreToolUse", tool_name=tool_name, tool_use_id="tool-4"),
                    self.projects,
                    time=NOW,
                )[0]
                self.assertEqual(event["task_label"], tool_name)
                self.assertNotIn("tool_name", event)

    def test_subagent_identity_and_task_are_namespaced_by_actual_session(self):
        start = self.payload(
            "SubagentStart", agent_id=AGENT, agent_type="Explore",
            prompt="private-sentinel",
        )
        stop = {**start, "hook_event_name": "SubagentStop"}

        _, started = normalize_hook(start, self.projects, time=NOW)[0]
        _, returned = normalize_hook(stop, self.projects, time=NOW)[0]

        self.assertEqual(started["actor_id"], f"sub:{SESSION}:{AGENT}")
        self.assertEqual(started["task_id"], f"agent:{SESSION}:{AGENT}")
        self.assertEqual(started["actor_label"], "Explore")
        self.assertEqual(started["state"], "started")
        self.assertEqual(returned["actor_id"], started["actor_id"])
        self.assertEqual(returned["state"], "returned")
        self.assertNotIn("parent_actor_id", started)
        self.assertNotIn("parent_task_id", started)
        self.assertNotIn("activity_role", started)

    def test_model_is_accepted_only_on_session_start_and_effort_only_from_level(self):
        _, tool = normalize_hook(
            self.payload(
                "PreToolUse", tool_name="Read", tool_use_id="tool-7",
                model="claude-opus-4-1", effort={"level": "high"},
            ),
            self.projects, time=NOW,
        )[0]
        self.assertNotIn("model", tool)
        self.assertEqual(tool["reasoning_effort"], "high")

        for effort in (None, "high", {}, {"level": "turbo"}, {"level": 1}):
            with self.subTest(effort=effort):
                _, event = normalize_hook(
                    self.payload("SessionStart", effort=effort), self.projects, time=NOW,
                )[0]
                self.assertNotIn("reasoning_effort", event)

        _, malformed = normalize_hook(
            self.payload(
                "PreToolUse", tool_name="Read", tool_use_id="tool-8",
                effort={"level": []},
            ),
            self.projects, time=NOW,
        )[0]
        self.assertEqual(malformed["state"], "started")
        self.assertNotIn("reasoning_effort", malformed)

    def test_canonical_cwd_selects_deepest_allowed_project(self):
        alias = self.root / "nested-alias"
        alias.symlink_to(self.nested, target_is_directory=True)
        child = alias / "src"
        child.mkdir()

        project, event = normalize_hook(
            self.payload(
                "PreToolUse", cwd=str(child), tool_name="Read", tool_use_id="tool-5",
            ),
            self.projects, time=NOW,
        )[0]

        self.assertEqual(project, "nested")
        self.assertEqual(event["project"], "nested")

    def test_embedded_nul_paths_are_rejected_without_dropping_valid_roots(self):
        self.assertEqual(normalize_hook(
            self.payload("SessionStart", cwd="bad\x00cwd"), self.projects, time=NOW,
        ), [])

        projects = {"bad": "bad\x00root", **self.projects}
        project, event = normalize_hook(
            self.payload("SessionStart"), projects, time=NOW,
        )[0]
        self.assertEqual(project, "alpha")
        self.assertEqual(event["project"], "alpha")

    def test_session_id_requires_canonical_uuid_but_accepts_uuid_versions(self):
        for session_id in (
            "11111111-1111-1111-8111-111111111111",
            "77777777-7777-7777-8777-777777777777",
        ):
            with self.subTest(session_id=session_id):
                _, event = normalize_hook(
                    self.payload("SessionStart", session_id=session_id),
                    self.projects, time=NOW,
                )[0]
                self.assertEqual(event["actor_id"], session_id)

        for session_id in (
            "session-1",
            "11111111111141118111111111111111",
            "11111111-1111-4111-8111-11111111111",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper(),
        ):
            with self.subTest(session_id=session_id):
                self.assertEqual(normalize_hook(
                    self.payload("SessionStart", session_id=session_id),
                    self.projects, time=NOW,
                ), [])

    def test_invalid_incomplete_and_ambiguous_payloads_are_ignored(self):
        outside = self.root / "outside"
        outside.mkdir()
        cases = [
            None,
            [],
            self.payload("UnknownEvent"),
            self.payload([]),
            self.payload("PreToolUse", cwd=str(outside), tool_name="Bash", tool_use_id="tool-1"),
            self.payload("PreToolUse", session_id="", tool_name="Bash", tool_use_id="tool-1"),
            self.payload("PreToolUse", session_id="ambiguous:id", tool_name="Bash", tool_use_id="tool-1"),
            self.payload("PreToolUse", tool_name="", tool_use_id="tool-1"),
            self.payload("PreToolUse", tool_name="Bash", tool_use_id=0),
            self.payload("UserPromptSubmit", prompt_id="bad/id"),
            self.payload("SubagentStart"),
            self.payload("SubagentStart", agent_id="bad/id"),
            self.payload("SubagentStart", session_id="s" * 80, agent_id="a" * 80),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_hook(payload, self.projects, time=NOW), [])

        valid = self.payload("SessionStart")
        self.assertEqual(normalize_hook(valid, self.projects, source_id="bad/id", time=NOW), [])
        self.assertEqual(normalize_hook(valid, self.projects, time="not-a-time"), [])

    def test_normalized_event_is_accepted_by_project_state(self):
        project, event = normalize_hook(
            self.payload("SessionEnd", reason="logout"), self.projects, time=NOW,
        )[0]
        store = ProjectState(self.projects, self.root / "state")

        saved = store.record_work_event(project, event, provenance="observed")

        self.assertEqual(saved["source_kind"], "claude-cli")
        self.assertEqual(saved["provenance"], "observed")
        self.assertEqual(saved["task_kind"], "agent")
        self.assertEqual(saved["state"], "unknown")
        self.assertNotIn("reason", saved)


if __name__ == "__main__":
    unittest.main()
