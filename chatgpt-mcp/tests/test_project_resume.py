import hashlib
from pathlib import Path
import stat
import tempfile
import unittest

from project_resume import ProjectResumeTools, tool_specs
from project_state import MemoryConflict, ProjectState


class FakeGit:
    def git_status(self, args):
        return {
            "project": args["project"],
            "branch": "main",
            "status": "## main\n M safe.txt\n?? new.txt\n",
        }


class FakeJobs:
    def list(self, project, limit=20):
        return {"jobs": [{
            "job_id": "a" * 32,
            "request_id": "secret-request",
            "project": project,
            "state": "succeeded",
            "argv": ["bash", "-c", "token=do-not-expose"],
            "preset": None,
            "relative_cwd": ".",
            "label": "safe test",
            "created_at": "2026-10-23T00:00:00+00:00",
            "started_at": "2026-10-23T00:00:01+00:00",
            "finished_at": "2026-10-23T00:00:02+00:00",
            "exit_code": 0,
            "termination_verified": True,
        }][:limit]}


class FakeAccess:
    def get(self, project):
        return {
            "active": True,
            "reason": None,
            "expires_at": "2026-10-24T00:00:00+00:00",
            "allow_codex": False,
            "scope": "installation",
            "grant_id": "do-not-copy",
            "project": project,
        }


class ProjectResumeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="project-resume-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.projects = {"alpha": self.repo}
        self.state = ProjectState(self.projects, self.root / "state")
        self.tools = ProjectResumeTools(
            self.projects, self.state, FakeJobs(), FakeGit(), access=FakeAccess())

    def tearDown(self):
        self.temporary.cleanup()

    def test_compaction_archives_exact_memory_before_atomic_replacement(self):
        first = self.state.update_memory("alpha", "# old\nimportant history", 0)
        compacted = self.tools.call("compact_project_memory", {
            "project": "alpha",
            "content": "# current\nshort durable decisions",
            "expected_revision": first["revision"],
        })
        self.assertEqual(compacted["revision"], 2)
        self.assertEqual(
            self.state.read_memory("alpha")["content"],
            "# current\nshort durable decisions",
        )
        archive = compacted["archived"]
        self.assertEqual(archive["revision"], 1)
        self.assertEqual(
            archive["content_sha256"],
            hashlib.sha256(b"# old\nimportant history").hexdigest(),
        )
        listed = self.state.list_memory_archives("alpha")
        self.assertEqual(listed["archives"], [archive])
        archive_path = (
            self.state.memory_archive_dir / self.state._memory_key("alpha")
            / (archive["archive_id"] + ".json")
        )
        self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)
        with self.assertRaises(MemoryConflict):
            self.tools.call("compact_project_memory", {
                "project": "alpha", "content": "stale", "expected_revision": 1,
            })
        self.assertEqual(len(self.state.list_memory_archives("alpha")["archives"]), 1)

    def test_resume_is_bounded_and_does_not_copy_raw_job_arguments_or_grant_id(self):
        memory = "A" * 20000
        self.state.update_memory("alpha", memory, 0)
        self.state.update_progress(
            "alpha", "Continue work", "in_progress", "Verified checkpoint",
            ["offline tests"], ["next safe step"], 0,
        )
        self.state.record_work_event("alpha", {
            "schema_version": 1,
            "source_id": "source-1",
            "source_kind": "chatgpt",
            "actor_id": "actor-1",
            "actor_kind": "executor",
            "actor_label": "ChatGPT",
            "task_id": "task-1",
            "task_kind": "job",
            "task_label": "resume test",
            "action": "test",
            "state": "succeeded",
            "exit_code": 0,
        })
        archive_dir = self.state.memory_archive_dir / self.state._memory_key("alpha")
        self.assertFalse(archive_dir.exists())
        result = self.tools.call("get_project_resume", {"project": "alpha"})
        self.assertFalse(archive_dir.exists())
        self.assertEqual(result["progress"]["title"], "Continue work")
        self.assertTrue(result["memory"]["excerpt_truncated"])
        self.assertLessEqual(
            len(result["memory"]["excerpt"].encode("utf-8")), 12 * 1024,
        )
        self.assertEqual(result["next_steps"], ["next safe step"])
        self.assertTrue(result["git"]["available"])
        self.assertTrue(result["git"]["dirty"])
        self.assertEqual(
            result["git"]["status_sha256"],
            hashlib.sha256(b"## main\n M safe.txt\n?? new.txt\n").hexdigest(),
        )
        self.assertNotIn("argv", result["recent_jobs"][0])
        self.assertNotIn("request_id", result["recent_jobs"][0])
        self.assertNotIn("grant_id", result["host_access"])
        self.assertTrue(result["host_access"]["active"])

    def test_resume_keeps_context_when_git_is_unavailable(self):
        class BrokenGit:
            def git_status(self, args):
                raise ValueError("not a repository")
        tools = ProjectResumeTools(
            self.projects, self.state, FakeJobs(), BrokenGit(), access=FakeAccess())
        self.state.update_memory("alpha", "durable context", 0)
        result = tools.call("get_project_resume", {"project": "alpha"})
        self.assertEqual(result["memory"]["revision"], 1)
        self.assertEqual(result["memory"]["excerpt"], "durable context")
        self.assertEqual(result["git"], {
            "available": False,
            "reason": "UNAVAILABLE",
            "branch": None,
            "dirty": None,
            "status_sha256": None,
            "status_lines": [],
            "status_truncated": False,
            "omitted_paths": 0,
        })

    def test_resume_bounds_large_git_status_by_lines_and_bytes(self):
        long_line = " M " + ("x" * 500)
        class LargeGit:
            def git_status(self, args):
                return {
                    "project": args["project"],
                    "branch": "main",
                    "status": "## main\n" + "\n".join([long_line] * 100),
                }
        tools = ProjectResumeTools(
            self.projects, self.state, FakeJobs(), LargeGit(), access=FakeAccess())
        result = tools.call("get_project_resume", {"project": "alpha"})
        rendered = "\n".join(result["git"]["status_lines"]).encode("utf-8")
        self.assertLessEqual(len(rendered), 12 * 1024)
        self.assertLessEqual(len(result["git"]["status_lines"]), 80)
        self.assertTrue(result["git"]["status_truncated"])

    def test_tool_specs_are_fixed_and_project_scoped(self):
        specs = {item["name"]: item for item in tool_specs(self.projects)}
        self.assertEqual(set(specs), {"get_project_resume", "compact_project_memory"})
        for spec in specs.values():
            self.assertEqual(
                spec["inputSchema"]["properties"]["project"]["enum"], ["alpha"])
        self.assertTrue(specs["get_project_resume"]["annotations"]["readOnlyHint"])
        self.assertFalse(specs["compact_project_memory"]["annotations"]["readOnlyHint"])


if __name__ == "__main__":
    unittest.main()
