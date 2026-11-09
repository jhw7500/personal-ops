"""Active workspace-policy boundaries for the existing Git workflows."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from git_workflows import GitWorkflows, approve_operation  # noqa: E402
from git_approval_ui import GitApprovals  # noqa: E402
import git_workflows  # noqa: E402
from workspace_paths import WorkspaceError  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


def run_git(root, *args):
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args], check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class LocalRemoteAdapter:
    def __call__(self, root, remote_url, branch):
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(root), "ls-remote", "--exit-code", "--heads",
             "origin", f"refs/heads/{branch}"], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        exists = result.returncode == 0
        head = result.stdout.split(maxsplit=1)[0] if exists else None
        return remote_url, exists, head

    def push(self, root, branch):
        result = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
             "push", "origin", f"HEAD:refs/heads/{branch}"], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace"))
        return result


class WorkspaceGitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-git-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.base = self.temp / "projects"
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        run_git(self.repo, "init", "-b", "feature/test")
        run_git(self.repo, "config", "user.name", "Test User")
        run_git(self.repo, "config", "user.email", "test@example.com")
        self.owner = self.temp / "owner"
        self.owner.mkdir(mode=0o700)
        self.config = self.owner / "workspace-policy.json"
        self.config.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.base),
            "artifacts": [{
                "project": "repo", "path": "build/result.json", "kind": "file",
                "operations": ["read"], "fields": ["status"], "recursive": False,
                "max_file_bytes": 4096,
            }],
            "protected_paths": [str(self.repo / "protected")],
        }))
        self.config.chmod(0o600)
        self.repo.joinpath("build").mkdir()
        self.repo.joinpath("protected").mkdir()
        fixtures = {
            "normal.txt": "normal-old\n",
            ".env": "ENV_SECRET_OLD\n",
            "ignored.txt": "IGNORED_SECRET_OLD\n",
            "build/result.json": '{"status":"old","raw":"ARTIFACT_SECRET_OLD"}\n',
            "protected/locked.txt": "PROTECTED_SECRET_OLD\n",
            ".gitignore": "ignored.txt\nbuild/\n",
        }
        for relative, content in fixtures.items():
            self.repo.joinpath(relative).write_text(content)
        run_git(self.repo, "add", "-f", *fixtures)
        run_git(self.repo, "commit", "-m", "fixture")

        self.workspace = WorkspaceTools(
            {"repo": self.repo}, self.owner / "authority", config_path=self.config,
        )
        snapshot = self.workspace.policy.load(required=True)
        self.workspace.authority.initialize(snapshot.digest, allowed_releases={"a" * 64: 1})
        self.workflows = GitWorkflows(
            {"repo": self.repo}, self.owner / "git-state", workspace=self.workspace,
            message_validator=lambda path: None, reference_checker=lambda url: None,
        )

    def modify_private_and_normal_files(self):
        changes = {
            "normal.txt": "NORMAL_VISIBLE_NEW\n",
            ".env": "ENV_SECRET_NEW\n",
            "ignored.txt": "IGNORED_SECRET_NEW\n",
            "build/result.json": '{"status":"new","raw":"ARTIFACT_SECRET_NEW"}\n',
            "protected/locked.txt": "PROTECTED_SECRET_NEW\n",
        }
        for relative, content in changes.items():
            self.repo.joinpath(relative).write_text(content)

    def message(self):
        return (
            "Workspace-safe commit\n\n### Contract version\nv1\n\n### Why\nFixture.\n\n"
            "### Changes\n- Fixture.\n\n### Validation\n- Tests.\n\n### References\n"
            "- Issue: https://github.com/jhw7500/personal-ops/issues/1\n"
        )

    def approve(self, prepared):
        stdin = TTYBuffer(f"APPROVE {prepared['operation_id']}\n")
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", TTYBuffer()):
            approve_operation(self.owner / "git-state", "repo", prepared["operation_id"])

    def remote_at_head(self):
        bare = self.temp / "remote.git"
        run_git(self.temp, "init", "--bare", str(bare))
        run_git(self.repo, "remote", "add", "origin", str(bare))
        run_git(self.repo, "push", "-u", "origin", "feature/test")
        self.workflows.remote_adapter = LocalRemoteAdapter()
        return bare, run_git(bare, "rev-parse", "refs/heads/feature/test").strip()

    def prepare_push(self):
        return self.workflows.prepare({
            "project": "repo", "action": "push", "branch": "feature/test",
            "new_branch": False,
        })

    def test_implicit_diff_and_status_filter_every_non_source_space(self):
        self.modify_private_and_normal_files()

        diff = self.workflows.git_diff({"project": "repo"})
        status = self.workflows.git_status({"project": "repo"})

        self.assertIn("NORMAL_VISIBLE_NEW", diff["diff"])
        serialized = json.dumps({"diff": diff, "status": status})
        for hidden in ("ENV_SECRET", "IGNORED_SECRET", "ARTIFACT_SECRET", "PROTECTED_SECRET",
                       ".env", "ignored.txt", "build/result.json", "protected/locked.txt"):
            self.assertNotIn(hidden, serialized)
        self.assertGreaterEqual(diff["omitted_paths"], 4)
        self.assertGreaterEqual(status["omitted_paths"], 4)
        self.assertFalse(diff["truncated"])
        self.assertFalse(status["truncated"])

    def test_staged_and_mode_only_diffs_do_not_reveal_protected_paths(self):
        self.modify_private_and_normal_files()
        self.repo.joinpath("protected/locked.txt").chmod(0o755)
        run_git(self.repo, "add", "-f", "normal.txt", ".env", "ignored.txt",
                "build/result.json", "protected/locked.txt")

        result = self.workflows.git_diff({"project": "repo", "staged": True})

        self.assertIn("NORMAL_VISIBLE_NEW", result["diff"])
        self.assertNotIn("ENV_SECRET_NEW", result["diff"])
        self.assertNotIn("ARTIFACT_SECRET_NEW", result["diff"])
        self.assertNotIn("protected/locked.txt", result["diff"])
        self.assertNotIn("new mode 100755", result["diff"])

    def test_registered_artifact_rename_does_not_reappear_under_safe_name(self):
        run_git(self.repo, "mv", "-f", "build/result.json", "exposed.json")

        result = self.workflows.git_diff({"project": "repo", "staged": True})

        self.assertEqual(result["diff"], "")
        self.assertEqual(result["omitted_paths"], 1)
        self.assertNotIn("ARTIFACT_SECRET_OLD", json.dumps(result))
        with self.assertRaises(WorkspaceError):
            self.workflows.git_diff({
                "project": "repo", "staged": True, "paths": ["exposed.json"],
            })

    def remote_with_main_only(self):
        """A trusted remote with published historical sensitive paths in main."""
        bare = self.temp / "new-branch-remote.git"
        run_git(self.temp, "init", "--bare", str(bare))
        run_git(self.repo, "remote", "add", "origin", str(bare))
        run_git(self.repo, "push", "origin", "HEAD:refs/heads/main")
        self.workflows.remote_adapter = LocalRemoteAdapter()
        return bare, run_git(bare, "rev-parse", "refs/heads/main").strip()

    def new_branch_plan(self):
        return self.workflows.prepare({
            "project": "repo", "action": "push", "branch": "feature/test",
            "new_branch": True,
        })

    def test_new_branch_push_rejects_hidden_history_instead_of_filtering_preview(self):
        bare, main_head = self.remote_with_main_only()
        # Historical .env and protected files in published main are not outgoing.
        # A new protected change in the outgoing feature commit still fails.
        self.repo.joinpath("protected/locked.txt").write_text("NEVER_PUBLISH_NEW_SECRET\n")
        run_git(self.repo, "add", "protected/locked.txt")
        run_git(self.repo, "commit", "-m", "add outgoing protected content")

        with self.assertRaises(WorkspaceError):
            self.new_branch_plan()

        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/main").strip(), main_head)
        self.assertEqual(list((self.owner / "git-state" / "repo").glob("*.plan.json")), [])

    def test_large_push_preview_includes_all_authorized_blob_ids_without_patch_capture(self):
        bare, main_head = self.remote_with_main_only()
        payload = "authorized outgoing line\n" * 15000
        self.assertLess(len(payload.encode()), 512 * 1024)
        self.assertGreater(2 * len(payload.encode()), 512 * 1024)
        self.repo.joinpath("normal.txt").write_text(payload)
        self.repo.joinpath("second.txt").write_text(payload + "second file\n")
        run_git(self.repo, "add", "normal.txt", "second.txt")
        run_git(self.repo, "commit", "-m", "large authorized outgoing fixture")
        head = run_git(self.repo, "rev-parse", "HEAD").strip()
        old_blob = run_git(self.repo, "rev-parse", main_head + ":normal.txt").strip()
        first_blob = run_git(self.repo, "rev-parse", "HEAD:normal.txt").strip()
        second_blob = run_git(self.repo, "rev-parse", "HEAD:second.txt").strip()

        plan = self.new_branch_plan()
        preview = self.workflows._load_plan("repo", plan["operation_id"])["immutable"]["preview"]
        for expected in [main_head, head, old_blob, first_blob, second_blob, "normal.txt", "second.txt"]:
            self.assertIn(expected, preview)
        self.assertLessEqual(len(preview.encode()), 512 * 1024)
        self.assertNotIn("ENV_SECRET_OLD", preview)
        self.assertNotIn("PROTECTED_SECRET_OLD", preview)
        self.assertNotIn("IGNORED_SECRET_OLD", preview)
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/main").strip(), main_head)
        self.assertNotIn("refs/heads/feature/test", run_git(bare, "show-ref"))
        with self.assertRaises((OSError, ValueError)):
            self.workflows.execute({"project": "repo", "operation_id": plan["operation_id"]})

    def test_new_branch_push_uses_verified_main_not_historic_protected_paths(self):
        bare, main_head = self.remote_with_main_only()
        self.repo.joinpath("normal.txt").write_text("SAFE_OUTGOING_ONLY\n")
        run_git(self.repo, "add", "normal.txt")
        run_git(self.repo, "commit", "-m", "safe outgoing")
        plan = self.new_branch_plan()
        self.assertEqual(plan["status"], "pending_host_approval")
        self.assertEqual(self.workflows._load_plan("repo", plan["operation_id"])["immutable"]["base_head"], main_head)
        preview = self.workflows._load_plan("repo", plan["operation_id"])["immutable"]["preview"]
        self.assertIn(main_head, preview)
        self.assertIn("normal.txt", preview)
        self.assertIn(run_git(self.repo, "rev-parse", main_head + ":normal.txt").strip(), preview)
        self.assertIn(run_git(self.repo, "rev-parse", "HEAD:normal.txt").strip(), preview)
        self.approve(plan)
        result = self.workflows.execute({"project": "repo", "operation_id": plan["operation_id"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(),
                         run_git(self.repo, "rev-parse", "HEAD").strip())

    def test_new_branch_chat_approval_uses_pinned_main_without_historic_private_paths(self):
        # Full main includes legacy protected records; only feature commits
        # are outgoing. Approval must apply the exact same base as planning.
        bare, main_head = self.remote_with_main_only()
        self.repo.joinpath("normal.txt").write_text("SAFE_APPROVED_OUTGOING\\n")
        run_git(self.repo, "add", "normal.txt")
        run_git(self.repo, "commit", "-m", "safe feature change")
        plan = self.new_branch_plan()
        self.assertEqual(self.workflows._load_plan("repo", plan["operation_id"])
                         ["immutable"]["base_head"], main_head)
        cards = GitApprovals(self.workflows, trusted_runtime=lambda: "a" * 64)
        opened = cards.open("repo", plan["operation_id"])
        self.assertTrue(opened["structuredContent"]["approval_required"])
        token = opened["_meta"]["git_confirmation"]
        confirmation = cards.confirm(
            "repo", plan["operation_id"],
            card_id=opened["structuredContent"]["card_id"],
            confirmation_token=token,
        )
        self.assertTrue(confirmation["structuredContent"]["receipt_verified"])
        done = self.workflows.execute({"project": "repo", "operation_id": plan["operation_id"]})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(),
                         run_git(self.repo, "rev-parse", "HEAD").strip())

    def test_new_branch_push_requires_real_remote_main_and_ancestry(self):
        bare = self.temp / "remote-empty.git"
        run_git(self.temp, "init", "--bare", str(bare))
        run_git(self.repo, "remote", "add", "origin", str(bare))
        self.workflows.remote_adapter = LocalRemoteAdapter()
        with self.assertRaisesRegex(ValueError, "main|base"):
            self.new_branch_plan()
        # Remote main exists, but it is unrelated to the feature history.
        main_branch = self.temp / "separate-main"
        main_branch.mkdir()
        run_git(main_branch, "init", "-b", "main")
        run_git(main_branch, "config", "user.name", "Test")
        run_git(main_branch, "config", "user.email", "test@example.com")
        main_branch.joinpath("unrelated.txt").write_text("not ancestor")
        run_git(main_branch, "add", "unrelated.txt")
        run_git(main_branch, "commit", "-m", "unrelated root")
        run_git(main_branch, "remote", "add", "origin", str(bare))
        run_git(main_branch, "push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "ancestor|base"):
            self.new_branch_plan()

    def test_new_branch_push_fails_if_main_moves_after_owner_approval(self):
        bare, main_head = self.remote_with_main_only()
        self.repo.joinpath("normal.txt").write_text("outgoing\n")
        run_git(self.repo, "add", "normal.txt")
        run_git(self.repo, "commit", "-m", "feature")
        plan = self.new_branch_plan()
        self.approve(plan)
        # Another actor updates remote main before executing the approved push.
        run_git(self.repo, "push", "origin", "HEAD:refs/heads/main")
        self.assertNotEqual(main_head, run_git(bare, "rev-parse", "refs/heads/main").strip())
        with self.assertRaisesRegex(ValueError, "main|base|approval|changed"):
            self.workflows.execute({"project": "repo", "operation_id": plan["operation_id"]})
        self.assertNotIn("refs/heads/feature/test", run_git(bare, "show-ref").strip())

    def test_new_branch_push_rejects_transient_hidden_outgoing_history(self):
        self.remote_with_main_only()
        transient = self.repo / "protected" / "outgoing.txt"
        transient.write_text("sensitive added in outgoing commit")
        run_git(self.repo, "add", "protected/outgoing.txt")
        run_git(self.repo, "commit", "-m", "hidden addition")
        transient.unlink()
        run_git(self.repo, "add", "protected/outgoing.txt")
        run_git(self.repo, "commit", "-m", "hidden deletion")
        # Net diff matches remote main, but intermediate outgoing path is forbidden.
        with self.assertRaises(WorkspaceError):
            self.new_branch_plan()

    def test_push_rejects_each_hidden_path_class_and_leaves_remote_unchanged(self):
        bare, remote_head = self.remote_at_head()
        cases = {
            ".env": "ENV_SECRET_PUSH\n",
            "ignored.txt": "IGNORED_SECRET_PUSH\n",
            "build/result.json": '{"status":"push","raw":"ARTIFACT_SECRET_PUSH"}\n',
            "protected/locked.txt": "PROTECTED_SECRET_PUSH\n",
        }
        for relative, content in cases.items():
            with self.subTest(relative=relative):
                run_git(self.repo, "reset", "--hard", remote_head)
                self.repo.joinpath(relative).write_text(content)
                run_git(self.repo, "add", "-f", relative)
                run_git(self.repo, "commit", "-m", f"unsafe {relative}")
                with self.assertRaises(WorkspaceError):
                    self.prepare_push()
                self.assertEqual(
                    run_git(bare, "rev-parse", "refs/heads/feature/test").strip(), remote_head,
                )

    def test_large_push_uses_bounded_preview_and_keeps_owner_wip(self):
        bare, remote_head = self.remote_at_head()
        payload = "".join(f"line-{index:06d}: {'X' * 70}\n" for index in range(10000))
        self.assertGreater(len(payload.encode("utf-8")), git_workflows.MAX_CAPTURE)
        self.repo.joinpath("normal.txt").write_text(payload)
        run_git(self.repo, "add", "normal.txt")
        run_git(self.repo, "commit", "-m", "large ordinary source update")
        untracked = self.repo / "owner-wip.txt"
        untracked.write_text("preserve owner work\n")

        prepared = self.prepare_push()
        preview = self.workflows._load_plan("repo", prepared["operation_id"])["immutable"]["preview"]
        self.assertIn("OUTGOING_DIFF_SUMMARY_ONLY", preview)
        self.assertIn("normal.txt", preview)
        self.assertIn(remote_head, preview)
        self.assertNotIn("line-000001", preview)
        self.assertLess(len(preview.encode("utf-8")), git_workflows.MAX_TEXT_BYTES)
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(), remote_head)

        self.approve(prepared)
        completed = self.workflows.execute({
            "project": "repo", "operation_id": prepared["operation_id"],
        })
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            run_git(bare, "rev-parse", "refs/heads/feature/test").strip(),
            run_git(self.repo, "rev-parse", "HEAD").strip(),
        )
        self.assertEqual(untracked.read_text(), "preserve owner work\n")

    def test_push_rejects_intermediate_hidden_add_then_delete(self):
        bare, remote_head = self.remote_at_head()
        transient = self.repo / "protected" / "transient.txt"
        transient.write_text("INTERMEDIATE_PROTECTED_SECRET\n")
        run_git(self.repo, "add", "protected/transient.txt")
        run_git(self.repo, "commit", "-m", "add hidden intermediate")
        transient.unlink()
        run_git(self.repo, "add", "protected/transient.txt")
        run_git(self.repo, "commit", "-m", "remove hidden intermediate")
        self.assertEqual(run_git(self.repo, "diff", remote_head, "HEAD"), "")

        with self.assertRaises(WorkspaceError):
            self.prepare_push()

        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(), remote_head)

    def test_push_rejects_commit_and_path_limits(self):
        self.remote_at_head()
        self.repo.joinpath("one.txt").write_text("one\n")
        run_git(self.repo, "add", "one.txt")
        run_git(self.repo, "commit", "-m", "one")
        self.repo.joinpath("two.txt").write_text("two\n")
        run_git(self.repo, "add", "two.txt")
        run_git(self.repo, "commit", "-m", "two")
        with mock.patch.object(git_workflows, "MAX_PUSH_COMMITS", 1):
            with self.assertRaisesRegex(ValueError, "commit limit"):
                self.prepare_push()
        with mock.patch.object(git_workflows, "MAX_PUSH_PATHS", 1):
            with self.assertRaisesRegex(ValueError, "path limit"):
                self.prepare_push()

    def test_execute_revalidates_ignored_rules_before_consuming_receipt(self):
        bare, remote_head = self.remote_at_head()
        self.repo.joinpath("safe.txt").write_text("safe outgoing\n")
        run_git(self.repo, "add", "safe.txt")
        run_git(self.repo, "commit", "-m", "safe outgoing")
        prepared = self.prepare_push()
        self.approve(prepared)
        receipt = self.owner / "git-state" / "repo" / f"{prepared['operation_id']}.approval.json"

        with mock.patch("workspace_files.is_ignored",
                        side_effect=lambda root, relative, **kwargs: relative == "safe.txt"):
            with self.assertRaises(WorkspaceError):
                self.workflows.execute({
                    "project": "repo", "operation_id": prepared["operation_id"],
                })

        self.assertTrue(receipt.exists())
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(), remote_head)

    def test_safe_active_policy_push_still_uses_existing_approval_gate(self):
        bare, remote_head = self.remote_at_head()
        self.repo.joinpath("safe.txt").write_text("safe outgoing\n")
        run_git(self.repo, "add", "safe.txt")
        run_git(self.repo, "commit", "-m", "safe outgoing")
        prepared = self.prepare_push()
        self.assertEqual(run_git(bare, "rev-parse", "refs/heads/feature/test").strip(), remote_head)
        self.approve(prepared)

        completed = self.workflows.execute({
            "project": "repo", "operation_id": prepared["operation_id"],
        })

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            run_git(bare, "rev-parse", "refs/heads/feature/test").strip(),
            run_git(self.repo, "rev-parse", "HEAD").strip(),
        )

    def test_explicit_diff_and_commit_selection_fail_closed(self):
        self.modify_private_and_normal_files()
        for relative in (".env", "ignored.txt", "build/result.json", "protected/locked.txt"):
            with self.subTest(relative=relative), self.assertRaises((WorkspaceError, ValueError)):
                self.workflows.git_diff({"project": "repo", "paths": [relative]})
            with self.subTest(commit=relative), self.assertRaises((WorkspaceError, ValueError)):
                self.workflows.prepare({
                    "project": "repo", "action": "commit", "paths": [relative],
                    "message": self.message(),
                })
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_commit_execution_holds_project_lock_before_admission(self):
        self.repo.joinpath("normal.txt").write_text("committed safely\n")
        prepared = self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["normal.txt"],
            "message": self.message(),
        })
        self.approve(prepared)
        events = []
        authority = self.workspace.authority
        real_project_lock = authority.project_lock
        real_admission = authority.admission

        @contextmanager
        def project_lock(project):
            events.append("project-enter")
            with real_project_lock(project):
                yield
            events.append("project-exit")

        @contextmanager
        def admission(digest):
            events.append("admission-enter")
            with real_admission(digest):
                yield
            events.append("admission-exit")

        with mock.patch.object(authority, "project_lock", project_lock), \
                mock.patch.object(authority, "admission", admission):
            completed = self.workflows.execute({
                "project": "repo", "operation_id": prepared["operation_id"],
            })

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(events, ["project-enter", "admission-enter",
                                  "admission-exit", "project-exit"])
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_no_policy_constructor_keeps_default_diff_shape(self):
        self.repo.joinpath("normal.txt").write_text("legacy-compatible\n")
        workflows = GitWorkflows({"repo": self.repo}, self.owner / "legacy-state")

        result = workflows.git_diff({"project": "repo"})

        self.assertEqual(set(result), {"project", "staged", "paths", "diff"})
        self.assertIn("legacy-compatible", result["diff"])


if __name__ == "__main__":
    unittest.main()
