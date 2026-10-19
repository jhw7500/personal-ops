import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest

from project_state import MAX_ACTIVITY_BYTES, MemoryConflict, ProjectState


def concurrent_update(projects, state_dir, content, gate, results):
    store = ProjectState(projects, state_dir)
    gate.wait()
    try:
        result = store.update_memory("alpha", content, 0)
        results.put(("ok", result["revision"], result["content"]))
    except MemoryConflict:
        results.put(("conflict",))


def concurrent_progress_update(projects, state_dir, title, gate, results):
    store = ProjectState(projects, state_dir)
    gate.wait()
    try:
        result = store.update_progress(
            "alpha", title, "in_progress", "Observed summary", [], ["Next"], 0,
        )
        results.put(("ok", result["revision"], result["title"]))
    except MemoryConflict:
        results.put(("conflict",))


class ProjectStateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="project-state-test-")
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

    def test_memory_persists_with_cas_and_private_modes(self):
        self.assertEqual(self.store.read_memory("alpha"), {
            "project": "alpha", "content": "", "revision": 0, "updated_at": None,
        })
        saved = self.store.update_memory("alpha", "# next\n검증 결과", 0)
        self.assertEqual(saved["revision"], 1)
        self.assertIsInstance(saved["updated_at"], str)
        restarted = ProjectState(self.projects, self.state_dir)
        self.assertEqual(restarted.read_memory("alpha"), saved)
        self.assertEqual(restarted.read_memory("beta")["revision"], 0)
        with self.assertRaises(MemoryConflict):
            restarted.update_memory("alpha", "stale", 0)
        self.assertEqual(restarted.read_memory("alpha")["content"], "# next\n검증 결과")
        for directory in (self.state_dir, self.store.memory_dir, self.store.lock_dir):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in self.state_dir.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

    def test_canonical_roots_separate_same_named_projects(self):
        self.store.update_memory("alpha", "first root", 0)
        other_root = self.root / "other" / "alpha"
        other_root.mkdir(parents=True)
        other = ProjectState({"alpha": other_root}, self.state_dir)
        self.assertEqual(other.read_memory("alpha")["revision"], 0)
        other.update_memory("alpha", "second root", 0)
        self.assertEqual(self.store.read_memory("alpha")["content"], "first root")
        self.assertEqual(other.read_memory("alpha")["content"], "second root")

    def test_progress_persists_isolated_with_cas_and_private_modes(self):
        expected_default = {
            "project": "alpha",
            "title": "",
            "status": "not_recorded",
            "summary": "",
            "completed": [],
            "next_steps": [],
            "revision": 0,
            "updated_at": None,
        }
        self.assertEqual(self.store.read_progress("alpha"), expected_default)
        saved = self.store.update_progress(
            "alpha", "Dashboard progress", "in_progress", "Tests observed passing.",
            ["State tests passed"], ["Run router test"], 0,
        )
        self.assertEqual(saved, {
            "project": "alpha",
            "title": "Dashboard progress",
            "status": "in_progress",
            "summary": "Tests observed passing.",
            "completed": ["State tests passed"],
            "next_steps": ["Run router test"],
            "revision": 1,
            "updated_at": saved["updated_at"],
        })
        self.assertIsInstance(saved["updated_at"], str)
        restarted = ProjectState(self.projects, self.state_dir)
        self.assertEqual(restarted.read_progress("alpha"), saved)
        self.assertEqual(restarted.read_progress("beta")["status"], "not_recorded")
        with self.assertRaises(MemoryConflict):
            restarted.update_progress(
                "alpha", "Stale", "blocked", "Old report", [], [], 0,
            )
        self.assertEqual(restarted.read_progress("alpha"), saved)
        self.assertEqual(stat.S_IMODE(self.store.progress_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.store._progress_path("alpha").stat().st_mode), 0o600,
        )

        other_root = self.root / "progress-other" / "alpha"
        other_root.mkdir(parents=True)
        other = ProjectState({"alpha": other_root}, self.state_dir)
        self.assertEqual(other.read_progress("alpha")["revision"], 0)
        other_saved = other.update_progress(
            "alpha", "Other root", "planned", "", [], [], 0,
        )
        self.assertEqual(other_saved["revision"], 1)
        self.assertEqual(restarted.read_progress("alpha"), saved)

    def test_progress_cross_process_cas_allows_one_winner(self):
        context = multiprocessing.get_context("fork")
        gate = context.Barrier(2)
        results = context.Queue()
        processes = [context.Process(
            target=concurrent_progress_update,
            args=(self.projects, self.state_dir, title, gate, results),
        ) for title in ("First", "Second")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        outcomes = sorted(results.get(timeout=1)[0] for _ in processes)
        self.assertEqual(outcomes, ["conflict", "ok"])
        saved = self.store.read_progress("alpha")
        self.assertEqual(saved["revision"], 1)
        self.assertIn(saved["title"], {"First", "Second"})

    def test_progress_validates_exact_bounds_and_rejects_links(self):
        saved = self.store.update_progress(
            "alpha", "t" * 200, "completed", "s" * 4096,
            ["c" * 1000] * 20, ["n" * 1000] * 20, 0,
        )
        self.assertEqual(saved["revision"], 1)
        invalid_updates = [
            ("", "planned", "", [], [], 0),
            ("한" * 67, "planned", "", [], [], 0),
            ("title", "not_recorded", "", [], [], 0),
            ("title", "unknown", "", [], [], 0),
            ("title", [], "", [], [], 0),
            ("title", "planned", "s" * 4097, [], [], 0),
            ("title", "planned", "", ["x"] * 21, [], 0),
            ("title", "planned", "", [], ["x"] * 21, 0),
            ("title", "planned", "", ["한" * 334], [], 0),
            ("title", "planned", "", [], [123], 0),
            ("title", "planned", "", [], [], True),
            ("title", "planned", "", [], [], -1),
        ]
        for arguments in invalid_updates:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.store.update_progress("beta", *arguments)
        with self.assertRaises(ValueError):
            self.store.read_progress("unknown")

        progress_path = self.store._progress_path("alpha")
        progress_path.unlink()
        progress_path.symlink_to(self.root / "outside-progress.json")
        with self.assertRaises(ValueError):
            self.store.read_progress("alpha")

    def test_cross_process_cas_allows_one_winner(self):
        context = multiprocessing.get_context("fork")
        gate = context.Barrier(2)
        results = context.Queue()
        processes = [context.Process(
            target=concurrent_update,
            args=(self.projects, self.state_dir, content, gate, results),
        ) for content in ("one", "two")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        outcomes = sorted(results.get(timeout=1)[0] for _ in processes)
        self.assertEqual(outcomes, ["conflict", "ok"])
        saved = self.store.read_memory("alpha")
        self.assertEqual(saved["revision"], 1)
        self.assertIn(saved["content"], {"one", "two"})

    def test_memory_bounds_input_types_and_symlinks(self):
        control_text = "\x00\n" * (32 * 1024 // 2)
        saved = self.store.update_memory("beta", control_text, 0)
        self.assertEqual(saved["content"], control_text)
        self.assertEqual(self.store.read_memory("beta")["content"], control_text)
        with self.assertRaises(ValueError):
            self.store.update_memory("alpha", "가" * 10923, 0)
        for revision in (True, -1, "0"):
            with self.assertRaises(ValueError):
                self.store.update_memory("alpha", "value", revision)
        with self.assertRaises(ValueError):
            self.store.read_memory("unknown")
        linked_state = self.root / "linked-state"
        linked_state.symlink_to(self.root / "real-state")
        with self.assertRaises(ValueError):
            ProjectState(self.projects, linked_state)
        self.store.update_memory("alpha", "valid", 0)
        memory_file = self.store._memory_path("alpha")
        memory_file.unlink()
        memory_file.symlink_to(self.root / "outside.json")
        with self.assertRaises(ValueError):
            self.store.read_memory("alpha")

        beta_lock = self.store.lock_dir / (
            "memory-" + self.store._memory_key("beta") + ".lock"
        )
        beta_lock.unlink()
        os.mkfifo(beta_lock, mode=0o600)
        with self.assertRaises(ValueError):
            self.store.read_memory("beta")

    def test_activity_is_metadata_only_filtered_and_newest_first(self):
        secret = "password=do-not-store"
        self.store.record_activity("alpha", "read_file", "started", "call-1")
        self.store.record_activity("beta", "read_file", "completed", "call-2")
        self.store.record_activity("alpha", "read_file", "completed", "call-1")
        listed = self.store.list_activity("alpha")
        self.assertEqual([event["phase"] for event in listed["events"]],
                         ["completed", "started"])
        self.assertEqual(set(listed["events"][0]),
                         {"project", "tool", "phase", "call_id", "time"})
        self.assertNotIn(secret, self.store.activity_path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.store.record_activity("alpha", secret, "failed", "call-3")
        with self.assertRaises(ValueError):
            self.store.record_activity(None, "read_file", "started", "call-4")
        with self.assertRaises(ValueError):
            self.store.record_activity("alpha", "list_projects", "started", "call-5")
        self.store.record_activity(None, "list_projects", "completed", "call-6")
        self.assertEqual(len(self.store.list_activity("alpha", limit=1)["events"]), 1)
        for limit in (0, True, 1001):
            with self.assertRaises(ValueError):
                self.store.list_activity("alpha", limit)

    def test_schema_and_rollout_activity_is_recorded_without_execution_inputs(self):
        tools = ['get_db_migration_status', 'get_predecessor_migration_status',
                 'get_schema014_migration_status', 'prepare_db_migration',
                 'prepare_predecessor_migration', 'prepare_schema014_migration',
                 'execute_db_migration', 'execute_predecessor_migration',
                 'execute_schema014_migration', 'prepare_runtime_rollout',
                 'get_runtime_rollout', 'execute_runtime_rollout',
                 'open_schema014_approval', 'confirm_schema014_approval',
                 'cancel_schema014_approval']
        for index, name in enumerate(tools):
            call_id = 'scope-' + str(index)
            self.store.record_activity('alpha', name, 'started', call_id)
            self.store.record_activity('alpha', name, 'completed', call_id)
        rows = self.store.list_activity('alpha')['events']
        self.assertEqual({row['tool'] for row in rows}, set(tools))
        self.assertEqual(len(rows), len(tools) * 2)
        self.assertTrue(all(set(row) == {'project', 'tool', 'phase', 'call_id', 'time'}
                            for row in rows))
        self.assertEqual(self.store.list_activity('beta')['events'], [])

    def test_activity_rotates_once_and_rejects_unsafe_files(self):
        self.store.activity_path.write_bytes(b" " * MAX_ACTIVITY_BYTES)
        self.store.activity_path.chmod(0o600)
        self.store.record_activity("alpha", "read_file", "started", "rotate-1")
        self.assertTrue(self.store.previous_activity_path.exists())
        self.assertLess(self.store.activity_path.stat().st_size, MAX_ACTIVITY_BYTES)
        self.store.activity_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.record_activity("alpha", "read_file", "completed", "rotate-1")

    def test_rejects_state_inside_project(self):
        with self.assertRaises(ValueError):
            ProjectState(self.projects, self.alpha / ".state")


if __name__ == "__main__":
    unittest.main()
