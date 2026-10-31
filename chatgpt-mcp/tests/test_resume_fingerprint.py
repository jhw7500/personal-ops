"""Fingerprint and active-task regressions for the Resume project snapshot."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from git_workflows import GitWorkflows
from project_resume import ProjectResumeTools
from project_state import ProjectState


def git(repo: Path, *argv: str):
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *argv], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-fingerprint-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "test-branch")
        git(self.repo, "config", "user.name", "fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("original")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-m", "baseline")
        self.workflows = GitWorkflows({"repo": self.repo}, Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def fingerprint(self):
        return self.workflows._safe_status_with_fingerprint(None, "repo", self.repo)

    def test_content_change_on_omitted_display_path_changes_content_fingerprint(self):
        for index in range(140):
            (self.repo / f"file-{index:03d}.txt").write_text("one")
        before = self.fingerprint()
        self.assertTrue(before["truncated"])
        self.assertEqual(before["visible_paths"], 140)
        self.assertTrue(before["content_fingerprint_complete"])
        self.assertGreater(before["omitted_paths"], 0)
        (self.repo / "file-139.txt").write_text("two")
        after = self.fingerprint()
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["visible_status_sha256"], after["visible_status_sha256"])
        self.assertNotEqual(before["worktree_content_sha256"], after["worktree_content_sha256"])

        (self.repo / "file-140.txt").write_text("three")
        extra = self.fingerprint()
        self.assertEqual(extra["status"], before["status"])
        self.assertNotEqual(extra["visible_status_sha256"], before["visible_status_sha256"])
        self.assertNotEqual(extra["worktree_content_sha256"], before["worktree_content_sha256"])

    def test_git_status_workspace_branch_exposes_full_fingerprint(self):
        from contextlib import contextmanager

        class FakeAuthority:
            @contextmanager
            def admission(self, digest):
                yield

        class FakePolicy:
            def check(self, project, relative, operation, *, snapshot):
                return relative

        class FakeWorkspace:
            enabled = True
            authority = FakeAuthority()
            policy = FakePolicy()

            def _context(self):
                return type("Snapshot", (), {"digest": "fixture"})(), None

            def ordinary_path(self, snapshot, project, target):
                return self_root / target

            def _current(self, snapshot):
                return None

        self_root = self.repo
        self.workflows.workspace = FakeWorkspace()
        for index in range(135):
            (self.repo / f"untracked-{index:03d}.txt").write_text("first")
        before = self.workflows.git_status({"project": "repo"})
        self.assertTrue(before["truncated"])
        self.assertEqual(before["visible_paths"], 135)
        self.assertEqual(before["fingerprint_scope"], "authorized-visible-dirty-paths")
        (self.repo / "untracked-134.txt").write_text("second")
        after = self.workflows.git_status({"project": "repo"})
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(before["worktree_content_sha256"], after["worktree_content_sha256"])

    def test_protected_file_has_no_effect_on_visible_content_digest(self):
        (self.repo / "tracked.txt").write_text("changed")
        before = self.fingerprint()
        (self.repo / ".env.private").write_text("sensitive-alpha")
        after = self.fingerprint()
        self.assertEqual(before["visible_status_sha256"], after["visible_status_sha256"])
        self.assertEqual(before["worktree_content_sha256"], after["worktree_content_sha256"])
        self.assertGreater(after["excluded_paths"], before["excluded_paths"])
        (self.repo / ".env.private").write_text("sensitive-beta")
        again = self.fingerprint()
        self.assertEqual(after["worktree_content_sha256"], again["worktree_content_sha256"])

    def test_changed_contents_on_already_modified_tracked_file(self):
        (self.repo / "tracked.txt").write_text("modified-1")
        before = self.fingerprint()
        (self.repo / "tracked.txt").write_text("modified-2")
        after = self.fingerprint()
        self.assertEqual(before["visible_status_sha256"], after["visible_status_sha256"])
        self.assertNotEqual(before["worktree_content_sha256"], after["worktree_content_sha256"])


class ResumeActiveTasksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-active-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = ProjectState({"repo": self.repo}, self.root / "private")

    def tearDown(self):
        self.temp.cleanup()

    def test_active_jobs_survive_newer_completed_history(self):
        class FakeJobs:
            def list(self, project, limit=20):
                records = [
                    {"job_id": f"{i:032x}", "state": "succeeded",
                     "created_at": f"2026-10-24T00:{i:02d}:00+00:00", "exit_code": 0}
                    for i in range(20)
                ]
                records[17]["state"] = "running"
                records[17]["exit_code"] = None
                return {"jobs": records[:limit]}

        class FakeGit:
            def git_status(self, arguments):
                return {
                    "branch": "main", "status": "## main\n M visible.txt\n",
                    "visible_status_sha256": "a" * 64,
                    "worktree_content_sha256": "b" * 64,
                    "content_fingerprint_complete": True,
                    "fingerprint_scope": "authorized-visible-dirty-paths",
                    "visible_paths": 150,
                    "excluded_paths": 2,
                    "truncated": True,
                    "omitted_paths": 22,
                }

        tools = ProjectResumeTools({"repo": self.repo}, self.state,
                                   FakeJobs(), FakeGit())
        out = tools.call("get_project_resume", {"project": "repo"})
        self.assertEqual(len(out["recent_jobs"]), 10)
        self.assertEqual(out["active_work"]["isolated_job_count"], 1)
        self.assertEqual(out["active_work"]["observed_active_isolated_jobs"][0]["job_id"], f"{17:032x}")
        self.assertEqual(out["git"]["status_sha256"], "a" * 64)
        self.assertEqual(out["git"]["worktree_content_sha256"], "b" * 64)
        self.assertTrue(out["git"]["content_fingerprint_complete"])
        self.assertEqual(out["git"]["status_hash_scope"], "authorized-visible-dirty-paths")
        self.assertIn("host_executions", out["active_work"]["not_included"])


    def test_running_event_is_unverified_signal_not_process_proof(self):
        self.state.record_work_event("repo", {
            "schema_version": 1,
            "source_id": "reported-monitor",
            "source_kind": "chatgpt",
            "actor_id": "worker-a",
            "actor_kind": "executor",
            "actor_label": "Worker",
            "task_id": "task-1",
            "task_kind": "job",
            "task_label": "test work",
            "action": "execute",
            "state": "running",
        })
        class NoJobs:
            def list(self, project, limit=20):
                return {"jobs": []}
        class NoGit:
            def git_status(self, arguments):
                return {"branch": "main", "status": "## main\n"}
        out = ProjectResumeTools({"repo": self.repo}, self.state, NoJobs(), NoGit()).call(
            "get_project_resume", {"project": "repo"})
        self.assertEqual(out["active_work"]["isolated_job_count"], 0)
        signals = out["active_work"]["activity_signals_unverified"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["source_id"], "reported-monitor")
        self.assertEqual(signals[0]["provenance"], "reported")



if __name__ == "__main__":
    unittest.main()
