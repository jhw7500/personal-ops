import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from git_workflows import (  # noqa: E402
    GIT_TOOL_SPECS,
    GitWorkflows,
    _default_message_validator,
    _run,
    approve_operation,
)
import git_workflows  # noqa: E402


def run_git(root, *args, check=True):
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args], check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class LocalRemoteAdapter:
    """Trusted fixture adapter; production never accepts file:// remotes."""

    def __call__(self, root, remote_url, branch):
        result = run_git(root, "ls-remote", "--exit-code", "--heads", "origin",
                         f"refs/heads/{branch}", check=False)
        exists = result.returncode == 0
        head = result.stdout.split(maxsplit=1)[0] if exists else None
        return remote_url, exists, head

    def push(self, root, branch):
        result = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
             "push", "origin", f"HEAD:refs/heads/{branch}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace"))
        return result


class GitWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="git-workflows-")
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-b", "feature/test")
        run_git(self.repo, "config", "user.name", "Test User")
        run_git(self.repo, "config", "user.email", "test@example.com")
        self.repo.joinpath("selected.txt").write_text("one\n", encoding="utf-8")
        self.repo.joinpath("other.txt").write_text("other one\n", encoding="utf-8")
        run_git(self.repo, "add", "selected.txt", "other.txt")
        run_git(self.repo, "commit", "-m", "initial")
        self.state = self.base / "private-state"
        self.checked_references = []
        self.workflows = GitWorkflows(
            {"repo": self.repo}, self.state,
            message_validator=lambda path: self.assertIn("### Contract version", path.read_text()),
            reference_checker=self.checked_references.append,
            remote_adapter=LocalRemoteAdapter(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_credentials_are_only_available_to_the_dedicated_network_git_path(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "fixture-secret", "OPENAI_API_KEY": "excluded"}):
            self.assertNotIn("GH_TOKEN", git_workflows._safe_env())
            self.assertEqual(git_workflows._safe_env(github=True)["GH_TOKEN"], "fixture-secret")
            self.assertNotIn("OPENAI_API_KEY", git_workflows._safe_env(github=True))
            success = subprocess.CompletedProcess([], 0, b"", b"")
            with mock.patch.object(git_workflows, "_run", return_value=success) as run:
                git_workflows._git(self.repo, "status")
                self.assertFalse(run.call_args.kwargs["github"])
                self.assertNotIn("credential.helper=!/usr/bin/gh auth git-credential", run.call_args.args[0])
                git_workflows._git(self.repo, "ls-remote", "https://github.com/jhw7500/fixture.git",
                                   authenticated=True)
                self.assertTrue(run.call_args.kwargs["github"])
                self.assertIn("credential.helper=!/usr/bin/gh auth git-credential", run.call_args.args[0])

    def test_git_plan_input_and_history_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            self.workflows.call("prepare_git_operation", {
                "project": "repo", "action": "pr", "title": "fixture", "base": "main", "body": "x" * 65537})
        with self.assertRaisesRegex(ValueError, "128"):
            self.workflows.call("git_diff", {"project": "repo", "paths": [f"file-{i}" for i in range(129)]})
        self.workflows.call("prepare_git_operation", {
            "project": "repo", "action": "pr", "title": "fixture", "base": "main", "body": "fixture body"})
        with mock.patch.object(git_workflows, "MAX_OPERATIONS", 1):
            with self.assertRaisesRegex(ValueError, "history capacity"):
                self.workflows.call("prepare_git_operation", {
                    "project": "repo", "action": "pr", "title": "fixture", "base": "main", "body": "another body"})

    def test_subdirectory_registration_cannot_expose_the_parent_git_repository(self):
        child = self.repo / "child"
        child.mkdir()
        scoped = GitWorkflows({"child": child}, self.base / "child-state")
        with self.assertRaisesRegex(ValueError, "exact repository root"):
            scoped.call("git_diff", {"project": "child"})

    def message(self):
        return (
            "Implement constrained Git workflow\n\n"
            " ### Contract version\n"
            "v1\n\n"
            " ### Why\n"
            "The approved file change needs a reviewable commit.\n\n"
            " ### Changes\n"
            "- Commit only the explicitly selected file.\n\n"
            " ### Validation\n"
            "- python3 -m unittest: passed.\n\n"
            " ### References\n"
            "- Issue: https://github.com/jhw7500/personal-ops/issues/1\n"
        )

    def approve(self, prepared):
        stdin = TTYBuffer(f"APPROVE {prepared['operation_id']}\n")
        stdout = TTYBuffer()
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
            approve_operation(self.state, "repo", prepared["operation_id"])
        return stdout.getvalue()

    def test_tool_specs_have_expected_names_and_shapes(self):
        self.assertEqual(
            {item["name"] for item in GIT_TOOL_SPECS},
            {"git_status", "git_diff", "prepare_git_operation",
             "execute_git_operation", "get_git_operation"},
        )
        for item in GIT_TOOL_SPECS:
            self.assertEqual(
                set(item), {"name", "title", "description", "inputSchema", "annotations"}
            )
            self.assertIn("project", item["inputSchema"]["properties"])

    def test_pinned_canonical_commit_validator_accepts_v1_message(self):
        message_path = self.base / "valid-message.md"
        message_path.write_text(self.message(), encoding="utf-8")
        _default_message_validator(message_path)

    def test_subprocess_capture_and_runtime_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            _run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 600000)"])
        with self.assertRaisesRegex(ValueError, "timeout"):
            _run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        descendant = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']); "
            "sys.exit(0)"
        )
        with self.assertRaisesRegex(ValueError, "timeout"):
            _run([sys.executable, "-c", descendant], timeout=0.05)

    def test_commit_requires_approval_is_scoped_and_single_use(self):
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        self.repo.joinpath("other.txt").write_text("other two\n", encoding="utf-8")
        prepared = self.workflows.call("prepare_git_operation", {
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        self.assertEqual(prepared["status"], "pending_host_approval")
        self.assertEqual(self.checked_references, [
            "https://github.com/jhw7500/personal-ops/issues/1"
        ])
        with self.assertRaisesRegex(ValueError, "approval"):
            self.workflows.call("execute_git_operation", {
                "project": "repo", "operation_id": prepared["operation_id"],
            })
        approval_output = self.approve(prepared)
        self.assertIn("git commit", approval_output)
        self.assertIn("Implement constrained Git workflow", approval_output)
        completed = self.workflows.call("execute_git_operation", {
            "project": "repo", "operation_id": prepared["operation_id"],
        })
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(run_git(self.repo, "show", "HEAD:selected.txt").stdout, "two\n")
        self.assertEqual(run_git(self.repo, "show", "HEAD:other.txt").stdout, "other one\n")
        self.assertIn(" M other.txt", run_git(self.repo, "status", "--short").stdout)
        with self.assertRaisesRegex(ValueError, "not executable"):
            self.workflows.call("execute_git_operation", {
                "project": "repo", "operation_id": prepared["operation_id"],
            })

    def test_stale_approval_is_rejected(self):
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        self.approve(prepared)
        self.repo.joinpath("selected.txt").write_text("three\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.workflows.execute({
                "project": "repo", "operation_id": prepared["operation_id"],
            })
        self.assertEqual(run_git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_path_traversal_and_preexisting_index_are_rejected(self):
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside"):
            self.workflows.prepare({
                "project": "repo", "action": "commit", "paths": ["../outside"],
                "message": self.message(),
            })
        run_git(self.repo, "add", "selected.txt")
        self.repo.joinpath("other.txt").write_text("other two\n", encoding="utf-8")
        before = run_git(self.repo, "diff", "--cached", "--name-only").stdout
        with self.assertRaisesRegex(ValueError, "pre-existing Git index"):
            self.workflows.prepare({
                "project": "repo", "action": "commit", "paths": ["other.txt"],
                "message": self.message(),
            })
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only").stdout, before)

    def test_pathspec_is_literal_and_env_content_is_filtered(self):
        self.repo.joinpath(".env").write_text("SECRET=old\n", encoding="utf-8")
        run_git(self.repo, "add", ".env")
        run_git(self.repo, "commit", "-m", "add env fixture")
        self.repo.joinpath(".env").write_text("SECRET=must-not-leak\n", encoding="utf-8")
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        entire = self.workflows.git_diff({"project": "repo"})["diff"]
        self.assertIn("selected.txt", entire)
        self.assertNotIn("must-not-leak", entire)
        self.assertEqual(self.workflows.git_diff({"project": "repo", "paths": ["*.txt"]})["diff"], "")
        with self.assertRaisesRegex(ValueError, "secret"):
            self.workflows.git_diff({"project": "repo", "paths": [".env"]})

    def test_textconv_is_never_executed_and_blocks_commit_preparation(self):
        marker = self.base / "textconv-ran"
        self.repo.joinpath(".gitattributes").write_text("*.txt diff=unsafe\n", encoding="utf-8")
        run_git(self.repo, "add", ".gitattributes")
        run_git(self.repo, "commit", "-m", "add attributes fixture")
        run_git(self.repo, "config", "diff.unsafe.textconv", f"touch {marker}")
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "executable settings"):
            self.workflows.git_diff({"project": "repo", "paths": ["selected.txt"]})
        self.assertFalse(marker.exists())

    def test_staging_race_and_executable_mode_change_are_rejected(self):
        selected = self.repo / "selected.txt"
        selected.write_text("approved\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        self.approve(prepared)
        original_git = git_workflows._git

        def racing_git(root, *args, **kwargs):
            if args and args[0] == "add":
                selected.write_text("unapproved-race\n", encoding="utf-8")
            return original_git(root, *args, **kwargs)

        with mock.patch("git_workflows._git", side_effect=racing_git):
            with self.assertRaisesRegex(ValueError, "changed during commit staging"):
                self.workflows.execute({
                    "project": "repo", "operation_id": prepared["operation_id"],
                })
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only").stdout, "")
        self.assertEqual(run_git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "1")

        selected.write_text("approved-mode\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        self.approve(prepared)
        selected.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.workflows.execute({
                "project": "repo", "operation_id": prepared["operation_id"],
            })

    def test_concurrent_staging_cannot_join_an_approved_commit(self):
        selected = self.repo / "selected.txt"
        other = self.repo / "other.txt"
        selected.write_text("approved\n", encoding="utf-8")
        other.write_text("concurrent\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        self.approve(prepared)
        original_git = git_workflows._git

        def concurrent_git(root, *args, **kwargs):
            if args and args[0] == "add":
                original_git(root, "add", "--", "other.txt")
            return original_git(root, *args, **kwargs)

        with mock.patch("git_workflows._git", side_effect=concurrent_git):
            with self.assertRaisesRegex(ValueError, "staged paths"):
                self.workflows.execute({
                    "project": "repo", "operation_id": prepared["operation_id"],
                })
        self.assertEqual(run_git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only").stdout, "other.txt\n")

    def test_approval_output_escapes_terminal_controls(self):
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        message = self.message().replace("The approved", "\x1b]0;owned\x07The approved")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": message,
        })
        output = self.approve(prepared)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertIn("\\x1b", output)
        self.assertIn("\\x07", output)

    def test_untracked_content_is_shown_and_failed_commit_cleans_only_selected_index(self):
        untracked = self.repo / "new.txt"
        untracked.write_text("new-file-preview\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["new.txt"],
            "message": self.message(),
        })
        output = self.approve(prepared)
        self.assertIn("new-file-preview", output)
        run_git(self.repo, "config", "--unset", "user.name")
        run_git(self.repo, "config", "--unset", "user.email")
        with self.assertRaises(ValueError):
            self.workflows.execute({
                "project": "repo", "operation_id": prepared["operation_id"],
            })
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only").stdout, "")
        self.assertEqual(untracked.read_text(), "new-file-preview\n")

    def test_oversized_selected_file_is_rejected_before_approval(self):
        oversized = self.repo / "large.bin"
        with oversized.open("wb") as handle:
            handle.truncate(512 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.workflows.prepare({
                "project": "repo", "action": "commit", "paths": ["large.bin"],
                "message": self.message(),
            })

    def test_push_requires_approval_and_rejects_protected_branch(self):
        remote = self.base / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        run_git(self.repo, "remote", "add", "origin", str(remote))
        with self.assertRaisesRegex(ValueError, "main or master"):
            self.workflows.prepare({
                "project": "repo", "action": "push", "branch": "main", "new_branch": True,
            })
        with self.assertRaisesRegex(ValueError, "unsafe arguments"):
            self.workflows.prepare({
                "project": "repo", "action": "push", "branch": "feature/test",
                "new_branch": True, "force": True,
            })
        prepared = self.workflows.prepare({
            "project": "repo", "action": "push", "branch": "feature/test", "new_branch": True,
        })
        self.approve(prepared)
        completed = self.workflows.execute({
            "project": "repo", "operation_id": prepared["operation_id"],
        })
        self.assertEqual(completed["status"], "completed")
        self.assertIn("refs/heads/feature/test", run_git(remote, "show-ref").stdout)

    def test_pr_and_merge_are_native_workflow_handoffs(self):
        pr = self.workflows.prepare({
            "project": "repo", "action": "pr", "title": "Test PR",
            "body": "PR body\n", "base": "main",
        })
        self.assertEqual(pr["status"], "waiting_native_workflow")
        self.assertEqual(pr["code"], "NATIVE_WORKFLOW_REQUIRED")
        body_path = Path(pr["handoff"]["body_path"])
        self.assertEqual(stat.S_IMODE(body_path.stat().st_mode), 0o600)
        self.assertEqual(body_path.read_text(), "PR body\n")
        self.assertEqual(
            self.workflows.execute({"project": "repo", "operation_id": pr["operation_id"]})["code"],
            "NATIVE_WORKFLOW_REQUIRED",
        )
        merge = self.workflows.prepare({
            "project": "repo", "action": "merge",
            "pull_request_url": "https://github.com/jhw7500/personal-ops/pull/2",
        })
        self.assertEqual(merge["status"], "waiting_native_workflow")
        self.assertIn("https://github.com/jhw7500/personal-ops/pull/2", self.checked_references)

    def test_state_is_outside_projects_and_owner_only(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            GitWorkflows({"repo": self.repo}, self.repo / ".state",
                         message_validator=lambda path: None,
                         reference_checker=lambda url: None,
                         remote_adapter=LocalRemoteAdapter())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.repo.joinpath("selected.txt").write_text("two\n", encoding="utf-8")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": self.message(),
        })
        artifacts = list(self.state.joinpath("repo").glob(f"{prepared['operation_id']}.*"))
        self.assertTrue(artifacts)
        for artifact in artifacts:
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
