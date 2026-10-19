import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_state import ProjectState  # noqa: E402


SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / "claude_status_report.py"
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


class ClaudeStatusReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-status-report-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / "repo"
        self.other_project = self.home / "other-repo"
        self.project.mkdir()
        self.other_project.mkdir()
        self.config = self.home / "projects.json"
        self.config.write_text(json.dumps({"projects": {
            "alpha": str(self.project), "beta": str(self.other_project),
        }}))
        self.state = self.home / "state"
        self.projects = {"alpha": self.project, "beta": self.other_project}
        self.store = ProjectState(self.projects, self.state)
        self.runtime = self.make_runtime()

    def make_runtime(self):
        releases = self.home / ".local/lib/chatgpt-coding-mcp/releases"
        releases.mkdir(parents=True, mode=0o700)
        releases.chmod(0o700)
        contents = {}
        for name in ("claude_hooks.py", "codex_observer.py", "codex_activity.py",
                     "project_state.py"):
            contents[name] = (SOURCE / name).read_bytes()
        contents["work-dashboard/model.js"] = b"export const fixture = true;\n"
        manifest = {name: hashlib.sha256(data).hexdigest()
                    for name, data in contents.items()}
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:20]
        runtime = releases / digest
        runtime.mkdir(mode=0o700)
        for name, data in contents.items():
            path = runtime / name
            if path.parent != runtime:
                path.parent.mkdir(mode=0o700)
            path.write_bytes(data)
            path.chmod(0o600)
        manifest_path = runtime / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        manifest_path.chmod(0o600)
        return runtime

    def run_status(self, payload, *, cwd=None, timeout=5, runtime=None,
                   hook_registry=False):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env.pop("PYTHONPATH", None)
        command = [
            sys.executable, str(SCRIPT), "--runtime-dir", str(runtime or self.runtime),
            "--projects", str(self.config), "--state-dir", str(self.state),
        ]
        if hook_registry:
            command.append("--hook-registry")
        return subprocess.run(
            command,
            input=raw, capture_output=True, timeout=timeout, cwd=cwd, env=env,
        )

    def payload(self, tasks=None, *, session=SESSION, cwd=None, **private):
        result = {"session_id": session, "cwd": str(cwd or self.project)}
        if tasks is not None:
            result["tasks"] = tasks
        result.update(private)
        return result

    @staticmethod
    def task(task_id, status="running", agent_type="general-purpose", **private):
        result = {
            "id": task_id, "type": "local_agent", "status": status,
            "agentType": agent_type, "startTime": 1,
        }
        result.update(private)
        return result

    def seed(self, task_id, *, session=SESSION, project="alpha",
             provenance="observed", state="started", label="general-purpose"):
        event = {
            "schema_version": 1,
            "source_id": "claude-local",
            "source_kind": "claude-cli",
            "actor_id": f"sub:{session}:{task_id}",
            "actor_kind": "agent",
            "actor_label": label,
            "task_id": f"agent:{session}:{task_id}",
            "task_kind": "call",
            "task_label": "Claude subagent",
            "action": "execute",
            "state": state,
        }
        self.store.record_work_event(project, event, provenance=provenance)

    def seed_tool(self, task_id, *, session=SESSION, project="alpha"):
        event = {
            "schema_version": 1,
            "source_id": "claude-local",
            "source_kind": "claude-cli",
            "actor_id": f"sub:{session}:{task_id}",
            "actor_kind": "agent",
            "actor_label": "general-purpose",
            "task_id": "tool:recent-child-tool",
            "task_kind": "call",
            "task_label": "Read",
            "action": "read",
            "state": "returned",
        }
        self.store.record_work_event(project, event, provenance="observed")

    def seed_parent(self, *, session=SESSION, project="alpha"):
        event = {
            "schema_version": 1,
            "source_id": "claude-local",
            "source_kind": "claude-cli",
            "actor_id": session,
            "actor_kind": "agent",
            "actor_label": "Claude",
            "task_id": f"session:{session}",
            "task_kind": "agent",
            "task_label": "Claude session activity",
            "action": "execute",
            "state": "unknown",
        }
        self.store.record_work_event(project, event, provenance="observed")

    def records(self, project="alpha"):
        return self.store.list_work_events(project, limit=1000)["records"]

    def test_maps_two_existing_children_and_persists_only_allowlisted_metadata(self):
        self.seed("a23c")
        self.seed("b91d", label="Explore")
        payload = self.payload([
            self.task("a23c", "running", description="private-sentinel"),
            self.task("b91d", "completed", "Explore", output="private-sentinel"),
        ], prompt="private-sentinel", transcript_path="/private-sentinel")

        result = self.run_status(payload)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        emitted = self.records()[:2]
        self.assertEqual({record["task_id"] for record in emitted}, {
            f"agent:{SESSION}:a23c", f"agent:{SESSION}:b91d",
        })
        states = {record["task_id"]: record["state"] for record in emitted}
        self.assertEqual(states[f"agent:{SESSION}:a23c"], "running")
        self.assertEqual(states[f"agent:{SESSION}:b91d"], "returned")
        for record in emitted:
            self.assertEqual(record["source_id"], "claude-local")
            self.assertEqual(record["source_kind"], "claude-cli")
            self.assertEqual(record["task_kind"], "call")
            self.assertEqual(record["task_label"], "Claude subagent")
            self.assertEqual(record["action"], "execute")
            self.assertEqual(record["provenance"], "observed")
            self.assertNotIn("tool_name", record)
        serialized = json.dumps(emitted)
        self.assertNotIn("private-sentinel", serialized)
        self.assertNotIn("startTime", serialized)

    def test_exact_status_mapping_transitions_immediately_and_dedupes_terminal(self):
        self.seed("agent-1")
        expected = [("running", "running"), ("failed", "failed"),
                    ("killed", "interrupted")]
        for supplied, stored in expected:
            result = self.run_status(self.payload([self.task("agent-1", supplied)]))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self.records()[0]["state"], stored)
        before = len(self.records())
        self.run_status(self.payload([self.task("agent-1", "killed")]))
        self.assertEqual(len(self.records()), before)

    def test_identical_running_is_throttled_but_refreshes_after_thirty_seconds(self):
        self.seed("agent-1")
        payload = self.payload([self.task("agent-1")])
        self.run_status(payload)
        self.run_status(payload)
        self.assertEqual([r["state"] for r in self.records()].count("running"), 1)

        cache = next((self.state / "claude-subagent-status").rglob("*.json"))
        data = json.loads(cache.read_text())
        for entry in data["entries"].values():
            entry["last_emitted"] = 1
        cache.write_text(json.dumps(data, separators=(",", ":")))
        cache.chmod(0o600)

        self.run_status(payload)
        self.assertEqual([r["state"] for r in self.records()].count("running"), 2)

    def test_unmatched_reported_cross_session_and_cross_project_records_cannot_associate(self):
        self.seed("reported-only", provenance="reported")
        self.seed("wrong-session", session=OTHER_SESSION)
        self.seed("wrong-project", project="beta")
        payload = self.payload([
            self.task("missing"), self.task("reported-only"),
            self.task("wrong-session"), self.task("wrong-project"),
        ])

        result = self.run_status(payload)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertFalse(any(record["state"] == "running" for record in self.records()))
        self.assertFalse(any(record["state"] == "running" for record in self.records("beta")))

    def test_prior_status_shaped_observation_cannot_bootstrap_a_missing_hook_start(self):
        self.seed("summary-only", state="running")

        result = self.run_status(self.payload([self.task("summary-only", "failed")]))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(len(self.records()), 1)

    def test_recent_observed_child_tool_can_prove_actor_identity_when_start_is_pruned(self):
        self.seed_tool("tool-proven")

        result = self.run_status(self.payload([self.task("tool-proven")]))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(self.records()[0]["task_id"], f"agent:{SESSION}:tool-proven")
        self.assertEqual(self.records()[0]["state"], "running")

    def test_duplicate_conflicting_and_over_limit_rows_reject_the_whole_observation(self):
        self.seed("one")
        for tasks in (
            [self.task("one"), self.task("one")],
            [self.task("one"), self.task("one", "failed")],
            [self.task(f"id-{number}") for number in range(65)],
        ):
            with self.subTest(count=len(tasks)):
                result = self.run_status(self.payload(tasks))
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, b"", b""))
        self.assertEqual(len(self.records()), 1)

    def test_invalid_rows_never_invent_lifecycle_state(self):
        self.seed("valid")
        invalid = [
            self.task("valid", "queued"),
            self.task("valid", agent_type="x" * 81),
            self.task("bad/id"),
            {**self.task("valid"), "type": "remote_agent"},
            {**self.task("valid"), "status": None},
        ]
        for task in invalid:
            with self.subTest(task=task):
                result = self.run_status(self.payload([task]))
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, b"", b""))
        self.assertEqual(len(self.records()), 1)

    def test_missing_and_empty_tasks_do_not_close_existing_children(self):
        self.seed("agent-1")
        self.run_status(self.payload([self.task("agent-1")]))
        before = list(self.records())

        for payload in (self.payload(), self.payload([])):
            result = self.run_status(payload)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

        self.assertEqual(self.records(), before)
        self.assertEqual(self.records()[0]["state"], "running")
        self.assertIn("expires_at", self.records()[0])

    def test_sixty_four_rows_are_persisted_in_batches_no_larger_than_twenty(self):
        task_ids = [f"task-{number}" for number in range(64)]
        for offset in range(0, len(task_ids), 20):
            for task_id in task_ids[offset:offset + 20]:
                self.seed(task_id)

        result = self.run_status(self.payload([self.task(task_id) for task_id in task_ids]))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual([record["state"] for record in self.records()].count("running"), 64)

    def test_cache_is_private_bounded_and_symlink_failure_is_fail_closed(self):
        for batch in range(5):
            tasks = []
            for number in range(64):
                task_id = f"task-{batch}-{number}"
                self.seed(task_id)
                tasks.append(self.task(task_id, "completed"))
            self.run_status(self.payload(tasks))

        cache_root = self.state / "claude-subagent-status"
        cache = next(cache_root.rglob("*.json"))
        self.assertEqual(stat.S_IMODE(cache_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        self.assertLessEqual(len(json.loads(cache.read_text())["entries"]), 256)

        shutil.rmtree(cache_root)
        outside = self.home / "outside"
        outside.mkdir()
        cache_root.symlink_to(outside, target_is_directory=True)
        self.seed("blocked")
        before = len(self.records())
        result = self.run_status(self.payload([self.task("blocked")]))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-status cache unavailable\n")
        self.assertEqual(len(self.records()), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_manifest_tampering_is_rejected_before_import(self):
        (self.runtime / "claude_hooks.py").write_text("raise RuntimeError('private-sentinel')")
        (self.runtime / "claude_hooks.py").chmod(0o600)
        self.seed("agent-1")

        result = self.run_status(self.payload([self.task("agent-1")]))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-status runtime unavailable\n")
        self.assertNotIn(b"private-sentinel", result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_malformed_and_oversized_input_are_silent_and_never_persisted(self):
        self.seed("agent-1")
        for raw in (b"not-json-private-sentinel", b"x" * (256 * 1024 + 1)):
            with self.subTest(length=len(raw)):
                result = self.run_status(raw)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, b"", b""))
        self.assertEqual(len(self.records()), 1)

    def test_blocked_state_lock_is_interrupted_by_internal_deadline(self):
        self.seed("agent-1")
        lock_path = self.store.lock_dir / "work-events.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            try:
                result = self.run_status(
                    self.payload([self.task("agent-1")]), timeout=2.5,
                )
            except subprocess.TimeoutExpired:
                self.fail("Claude status reporter exceeded its internal deadline")
        finally:
            os.close(descriptor)

        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"claude-status association unavailable\n")

    def test_hook_registry_maps_exactly_proven_subagents_and_ignores_shell_privacy(self):
        self.seed_parent()
        self.seed("a122201b76bd4413e")
        self.seed("ab8b169ae1e27169e")
        payload = self.payload(
            hook_event_name="SubagentStop",
            agent_id="ac0f68a6731d46dc2",
            background_tasks=[
                {"id": "a122201b76bd4413e", "type": "subagent", "status": "running",
                 "agent_type": "general-purpose", "description": "private-sentinel"},
                {"id": "ab8b169ae1e27169e", "type": "subagent", "status": "running",
                 "agent_type": "general-purpose", "prompt": "private-sentinel"},
                {"id": "bpjkd60gx", "type": "shell", "status": "running",
                 "command": "private-sentinel"},
            ],
            transcript_path="/private-sentinel",
        )

        result = self.run_status(payload, hook_registry=True)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        emitted = [record for record in self.records() if record["state"] == "running"]
        self.assertEqual({record["actor_id"] for record in emitted}, {
            f"sub:{SESSION}:a122201b76bd4413e",
            f"sub:{SESSION}:ab8b169ae1e27169e",
        })
        self.assertNotIn("ac0f68a6731d46dc2", json.dumps(emitted))
        self.assertNotIn("bpjkd60gx", json.dumps(emitted))
        self.assertNotIn("private-sentinel", json.dumps(emitted))
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-sentinel", path.read_bytes())

    def test_hook_registry_known_parent_cannot_register_an_unproven_child(self):
        self.seed_parent()
        payload = self.payload(
            hook_event_name="Stop",
            background_tasks=[{
                "id": "a122201b76bd4413e", "type": "subagent", "status": "running",
                "agent_type": "general-purpose",
            }],
        )

        result = self.run_status(payload, hook_registry=True)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertFalse(any(record["state"] == "running" for record in self.records()))

    def test_hook_registry_missing_empty_and_unsupported_events_are_noops(self):
        self.seed_parent()
        payloads = [
            self.payload(hook_event_name="Stop"),
            self.payload(hook_event_name="SubagentStop", background_tasks=[]),
            self.payload(
                hook_event_name="UserPromptSubmit",
                background_tasks=[{
                    "id": "a122201b76bd4413e", "type": "subagent", "status": "running",
                    "agent_type": "general-purpose",
                }],
            ),
        ]

        for payload in payloads:
            with self.subTest(hook_event_name=payload["hook_event_name"]):
                result = self.run_status(payload, hook_registry=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, b"", b""))
        self.assertEqual(len(self.records()), 1)

    def test_hook_registry_rejects_more_than_sixty_four_rows(self):
        self.seed_parent()
        tasks = [
            {"id": f"task-{number}", "type": "subagent", "status": "running",
             "agent_type": "general-purpose"}
            for number in range(65)
        ]

        result = self.run_status(
            self.payload(hook_event_name="Stop", background_tasks=tasks),
            hook_registry=True,
        )

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(len(self.records()), 1)


if __name__ == "__main__":
    unittest.main()
