"""Git commit/push approval via authentic one-use app-only token.

Only temporary fixture Git repos; never pushes to a real remote or user repo.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_git_workflows import run_git, LocalRemoteAdapter
from git_workflows import GitWorkflows, approve_operation
from git_approval_ui import GitApprovals, GitApprovalTools, tool_specs
from host_runtime_config import HostRuntimeError


class GitChatApprovalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="git-chat-approval-")
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.repo=self.root/"repo"
        self.repo.mkdir()
        run_git(self.repo,"init","-b","feature/test")
        run_git(self.repo,"config","user.name","Test User")
        run_git(self.repo,"config","user.email","test@example.com")
        (self.repo/"selected.txt").write_text("one\n")
        (self.repo/"other.txt").write_text("untouched\n")
        run_git(self.repo,"add","selected.txt","other.txt")
        run_git(self.repo,"commit","-m","fixture")
        self.git=GitWorkflows(
            {"repo":self.repo},self.root/"state",
            message_validator=lambda p: None,
            reference_checker=lambda _: None,
            remote_adapter=LocalRemoteAdapter(),
        )
        self.runtime_identity="a"*64
        self.cards=GitApprovals(self.git,trusted_runtime=lambda:self.runtime_identity)
        (self.repo/"selected.txt").write_text("two\n")
        self.plan=self.git.prepare({
            "project":"repo","action":"commit","paths":["selected.txt"],
            "message":"Change safely\n\n### References\n"
                      "- Issue: https://github.com/jhw7500/personal-ops/issues/1\n",
        })

    def args(self, opened):
        return {
            "card_id":opened["structuredContent"]["card_id"],
            "confirmation_token":opened["_meta"]["git_confirmation"],
        }

    def open(self):
        return self.cards.open("repo",self.plan["operation_id"])

    def assert_code(self, code, call):
        with self.assertRaisesRegex(HostRuntimeError,code):
            call()

    def test_hidden_token_and_no_automatic_commit(self):
        before=run_git(self.repo,"rev-parse","HEAD").stdout.strip()
        opened=self.open()
        public=opened["structuredContent"]
        token=self.args(opened)["confirmation_token"]
        self.assertEqual(public["action"],"commit")
        self.assertEqual(public["component"],"git_operation")
        self.assertEqual(public["paths"],["selected.txt"])
        self.assertEqual(public["plan_digest"],
                         self.git._load_plan("repo",self.plan["operation_id"])["plan_digest"])
        self.assertNotIn("approval_command",public["plan"])
        self.assertNotIn(token,json.dumps(public))
        self.assertFalse(public["executes_on_approval"])
        self.assertTrue(public["approval_required"])
        self.assertEqual(before,run_git(self.repo,"rev-parse","HEAD").stdout.strip())

    def test_confirm_issues_existing_one_use_receipt_without_commit(self):
        before=run_git(self.repo,"rev-parse","HEAD").stdout.strip()
        opened=self.open()
        args=self.args(opened)
        result=self.cards.confirm("repo",self.plan["operation_id"],**args)
        self.assertTrue(result["structuredContent"]["approved"])
        self.assertTrue(result["structuredContent"]["receipt_verified"])
        self.assertEqual(result["structuredContent"]["next_action"],{
            "tool":"execute_git_operation",
            "arguments":{"project":"repo","operation_id":self.plan["operation_id"]},
        })
        receipt=self.git._receipt_path("repo",self.plan["operation_id"])
        self.assertTrue(receipt.exists())
        self.assertEqual(before,run_git(self.repo,"rev-parse","HEAD").stdout.strip())
        self.assert_code("GIT_CARD_SPENT",lambda:self.cards.confirm(
            "repo",self.plan["operation_id"],**args))
        reopened=self.open()
        self.assertEqual(reopened["structuredContent"]["state"],"approved")
        self.assertNotIn("_meta",reopened)

    def test_existing_git_executor_consumes_chat_issued_receipt_once(self):
        opened=self.open()
        self.cards.confirm("repo",self.plan["operation_id"],**self.args(opened))
        done=self.git.execute({"project":"repo","operation_id":self.plan["operation_id"]})
        self.assertEqual(done["status"],"completed")
        self.assertEqual(run_git(self.repo,"show","HEAD:selected.txt").stdout,"two\n")
        self.assertFalse(self.git._receipt_path("repo",self.plan["operation_id"]).exists())
        with self.assertRaises(ValueError):
            self.git.execute({"project":"repo","operation_id":self.plan["operation_id"]})

    def test_cancel_makes_plan_nonexecutable(self):
        opened=self.open()
        cancelled=self.cards.cancel("repo",self.plan["operation_id"],**self.args(opened))
        self.assertFalse(cancelled["structuredContent"]["approved"])
        self.assertEqual(cancelled["structuredContent"]["state"],"cancelled")
        self.assertEqual(self.git._load_plan("repo",self.plan["operation_id"])["status"],
                         "rejected")
        self.assertFalse(self.git._receipt_path("repo",self.plan["operation_id"]).exists())
        self.assertEqual(run_git(self.repo,"show","HEAD:selected.txt").stdout,"one\n")

    def test_invalid_scope_nonce_and_runtime_fail_closed(self):
        opened=self.open()
        args=self.args(opened)
        self.assert_code("UNSUPPORTED_SCOPE",lambda:self.cards.confirm(
            "other",self.plan["operation_id"],**args))
        self.assert_code("GIT_CARD_DENIED",lambda:self.cards.confirm(
            "repo",self.plan["operation_id"],
            card_id=args["card_id"],confirmation_token="b"*64))
        self.runtime_identity="b"*64
        self.assert_code("GIT_CARD_STALE",lambda:self.cards.confirm(
            "repo",self.plan["operation_id"],**args))

    def test_source_drift_rejected_without_receipt(self):
        opened=self.open()
        (self.repo/"selected.txt").write_text("three\n")
        self.assert_code("GIT_CARD_STALE",lambda:self.cards.confirm(
            "repo",self.plan["operation_id"],**self.args(opened)))
        self.assertFalse(self.git._receipt_path("repo",self.plan["operation_id"]).exists())

    def test_reopening_rotates_one_time_capability(self):
        first=self.open()
        second=self.open()
        self.assertNotEqual(self.args(first)["confirmation_token"],
                            self.args(second)["confirmation_token"])
        self.assert_code("GIT_CARD_DENIED",lambda:self.cards.confirm(
            "repo",self.plan["operation_id"],**self.args(first)))

    def test_commit_and_push_use_exact_same_receipt_protocol(self):
        self.cards.confirm("repo",self.plan["operation_id"],**self.args(self.open()))
        # No branch named main may be pushed by this workflow.
        bare=self.root/"remote.git"
        run_git(self.root,"init","--bare",str(bare))
        run_git(self.repo,"remote","add","origin",str(bare))
        run_git(self.repo,"push","origin","feature/test")
        push=self.git.prepare({"project":"repo","action":"push",
                               "branch":"feature/test","new_branch":False})
        self.assertEqual(push["action"],"push")
        opened=self.cards.open("repo",push["operation_id"])
        self.assertEqual(opened["structuredContent"]["action"],"push")
        result=self.cards.confirm("repo",push["operation_id"],**self.args(opened))
        self.assertEqual(result["structuredContent"]["next_action"]["tool"],
                         "execute_git_operation")
        self.assertTrue(self.git._receipt_path("repo",push["operation_id"]).is_file())

    def test_pr_handoff_cannot_be_chat_approved(self):
        pr=self.git.prepare({"project":"repo","action":"pr","title":"Fixture",
                             "body":"Draft","base":"main"})
        self.assert_code("GIT_CARD_NOT_PENDING",lambda:self.cards.open(
            "repo",pr["operation_id"]))

    def test_tool_schemas_are_private_for_confirm_and_cancel(self):
        specs={s["name"]:s for s in tool_specs({"repo":self.repo})}
        self.assertEqual(specs["open_git_approval"]["_meta"]["ui"]["visibility"],
                         ["model"])
        for name in ("confirm_git_approval","cancel_git_approval"):
            self.assertEqual(specs[name]["_meta"]["ui"]["visibility"],["app"])
            self.assertTrue(specs[name]["_meta"]["openai/widgetAccessible"])
            self.assertFalse(specs[name]["inputSchema"]["additionalProperties"])
        router=GitApprovalTools(self.git)
        self.assert_code("INVALID_GIT_CARD_ARGUMENTS",
            lambda:router.validate_arguments("confirm_git_approval",{
                "project":"repo","operation_id":self.plan["operation_id"],
                "card_id":"1"*32,"confirmation_token":"2"*64,"override":"true",
            }))


if __name__=="__main__":
    unittest.main()


def git(root, *args, check=True):
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args], capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


class Terminal(io.StringIO):
    def isatty(self):
        return True


class OwnerGitAccess:
    """Substitute only the separate verified owner-authority boundary."""
    def __init__(self, allowed=False):
        self.allowed = allowed
        self.grant = {
            "version": 2, "project": "repo", "scope": "installation",
            "allow_git": True, "allow_codex": False,
            "grant_id": "a" * 32, "grant_state_revision": 7,
            "expires_at": None, "lifetime": "until_revoked",
        }

    def require_git(self, project, action):
        if project != "repo" or action not in {"commit", "push"}:
            raise HostRuntimeError("INVALID_HOST_ACCESS_GIT_ACTION")
        if not self.allowed:
            raise HostRuntimeError("HOST_ACCESS_GIT_DENIED")
        return dict(self.grant)

    @contextmanager
    def guard_git(self, project, action):
        yield self.require_git(project, action)


class GitFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="git-chat-approval-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "feature/chat")
        git(self.repo, "config", "user.name", "Fixture User")
        git(self.repo, "config", "user.email", "fixture@example.test")
        (self.repo / "selected.txt").write_text("before\n")
        git(self.repo, "add", "selected.txt")
        git(self.repo, "commit", "-m", "fixture")
        self.head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "selected.txt").write_text("after\n")
        self.state = self.base / "private-state"
        self.workflows = GitWorkflows(
            {"repo": self.repo}, self.state,
            message_validator=lambda path: None,
            reference_checker=lambda reference: None,
        )

    def prepare(self):
        return self.workflows.prepare({
            "project": "repo", "action": "commit", "paths": ["selected.txt"],
            "message": "Record the reviewed change\n\nhttps://github.com/example/repo/issues/1\n",
        })

    def approve(self, plan):
        with mock.patch("sys.stdin", Terminal("APPROVE " + plan["operation_id"] + "\n")), \
                mock.patch("sys.stdout", Terminal()):
            return approve_operation(self.state, "repo", plan["operation_id"])

    def execute(self, plan):
        return self.workflows.execute({"project": "repo", "operation_id": plan["operation_id"]})

    def get(self, plan):
        return self.workflows.get({"project": "repo", "operation_id": plan["operation_id"]})

    def assert_unchanged(self):
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only").stdout, "")


class GitPreflightAndReceiptTests(GitFixture, unittest.TestCase):
    def test_missing_author_fails_before_creating_plan_or_message(self):
        git(self.repo, "config", "user.name", "")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.prepare()
        self.assertEqual(list(self.state.rglob("*.plan.json")), [])
        self.assertEqual(list(self.state.rglob("*.commit-message.md")), [])
        self.assert_unchanged()

    def test_missing_committer_fails_before_creating_a_plan(self):
        git(self.repo, "config", "user.name", "")
        git(self.repo, "config", "author.name", "Author Only")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.prepare()
        self.assertEqual(list(self.state.rglob("*.plan.json")), [])
        self.assert_unchanged()

    def test_get_recognizes_cli_approval_without_requesting_it_again(self):
        plan = self.prepare()
        self.approve(plan)
        result = self.get(plan)
        self.assertIs(result.get("receipt_verified"), True)
        self.assertIs(result.get("approval_required"), False)
        self.assertNotIn("approval_command", result)
        self.assertEqual(result.get("next_action"), {
            "tool": "execute_git_operation",
            "arguments": {"project": "repo", "operation_id": plan["operation_id"]},
        })
        self.assert_unchanged()

    def test_get_rejects_a_receipt_for_another_plan(self):
        plan = self.prepare()
        self.approve(plan)
        path = self.state / "repo" / (plan["operation_id"] + ".approval.json")
        value = json.loads(path.read_text())
        value["plan_digest"] = "f" * 64
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.get(plan)
        self.assert_unchanged()

    def test_cli_rejects_plan_identity_that_differs_from_the_requested_operation(self):
        plan = self.prepare()
        path = self.state / "repo" / (plan["operation_id"] + ".plan.json")
        value = json.loads(path.read_text())
        value["operation_id"] = "c" * 32
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "identity|integrity"):
            self.approve(plan)
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_identity_change_is_rejected_before_receipt_consumption(self):
        plan = self.prepare()
        self.approve(plan)
        git(self.repo, "config", "user.email", "changed@example.test")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.execute(plan)
        self.assertTrue((self.state / "repo" / (plan["operation_id"] + ".approval.json")).exists())
        self.assert_unchanged()


class GitDevelopmentGrantTests(GitFixture, unittest.TestCase):
    def use_access(self, access):
        self.workflows.host_access = access

    def test_explicit_git_grant_executes_without_a_single_use_receipt(self):
        self.use_access(OwnerGitAccess(allowed=True))
        plan = self.prepare()
        self.assertIs(plan.get("approval_required"), False)
        self.assertEqual(plan.get("authorization", {}).get("grant_id"), "a" * 32)
        self.assertNotIn("approval_command", plan)
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assertEqual(self.execute(plan)["status"], "completed")
        self.assertEqual(git(self.repo, "show", "HEAD:selected.txt").stdout, "after\n")
        with self.assertRaisesRegex(ValueError, "not executable"):
            self.execute(plan)

    def test_no_git_opt_in_keeps_single_use_approval_required(self):
        self.use_access(OwnerGitAccess(allowed=False))
        plan = self.prepare()
        self.assertIs(plan.get("approval_required"), True)
        with self.assertRaisesRegex(ValueError, "approval"):
            self.execute(plan)
        self.assert_unchanged()

    def test_replaced_or_revised_grant_cannot_authorize_an_old_plan(self):
        for field, changed in (("grant_id", "b" * 32), ("grant_state_revision", 8)):
            with self.subTest(field=field):
                access = OwnerGitAccess(allowed=True)
                self.use_access(access)
                plan = self.prepare()
                access.grant[field] = changed
                with self.assertRaisesRegex(ValueError, "grant"):
                    self.execute(plan)
                self.assert_unchanged()

    def test_revoked_git_authority_blocks_execution(self):
        access = OwnerGitAccess(allowed=True)
        self.use_access(access)
        plan = self.prepare()
        access.allowed = False
        with self.assertRaisesRegex(ValueError, "grant|HOST_ACCESS_GIT_DENIED"):
            self.execute(plan)
        self.assert_unchanged()

    def test_new_grant_does_not_approve_a_previously_prepared_plan(self):
        access = OwnerGitAccess(allowed=False)
        self.use_access(access)
        plan = self.prepare()
        access.allowed = True
        with self.assertRaisesRegex(ValueError, "approval"):
            self.execute(plan)
        self.assert_unchanged()


class GitChatCardTests(GitFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("git_operation_ui"),
                             "Git plans need a trusted app-only confirmation component")
        import git_operation_ui
        self.module = git_operation_ui
        self.assertEqual(self.module.UI_URI, "ui://git-operations/approval-v2.html")
        self.runtime = {"id": "d" * 64}
        self.ui = git_operation_ui.GitOperationApprovals(
            self.workflows, trusted_runtime=lambda: self.runtime["id"])

    def test_new_operation_ui_uses_distinct_v2_resource_contract(self):
        specs = {item["name"]: item for item in self.module.tool_specs({"repo": self.repo})}
        opener = specs["open_git_operation_approval"]
        self.assertEqual(opener["_meta"]["ui"]["resourceUri"], self.module.UI_URI)
        self.assertEqual(opener["_meta"]["openai/outputTemplate"], self.module.UI_URI)

    def open(self):
        plan = self.prepare()
        return plan, self.ui.open("repo", plan["operation_id"])

    def callback(self, result):
        public = result["structuredContent"]
        return {"project": "repo", "operation_id": public["operation_id"],
                "card_id": public["card_id"],
                "confirmation_token": result["_meta"]["git_operation_confirmation"]}

    def test_open_displays_exact_change_but_neither_approves_nor_executes(self):
        plan, opened = self.open()
        token = opened["_meta"]["git_operation_confirmation"]
        public = opened["structuredContent"]
        self.assertEqual(public["component"], "git_operation")
        self.assertEqual(public["state"], "pending")
        self.assertEqual(public["plan"]["paths"], ["selected.txt"])
        self.assertIn("Record the reviewed change", public["plan"]["message"])
        self.assertIn("+after", public["plan"]["preview"])
        self.assertIs(public["approval_required"], True)
        self.assertNotIn(token, json.dumps(public))
        self.assertNotIn(token, json.dumps(opened["content"]))
        self.assertNotIn("approval_nonce", json.dumps(opened))
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_confirmation_issues_one_exact_receipt_without_execution(self):
        plan, opened = self.open()
        approved = self.ui.confirm(**self.callback(opened))["structuredContent"]
        self.assertIs(approved["receipt_verified"], True)
        self.assertEqual(approved["next_action"]["tool"], "execute_git_operation")
        self.assert_unchanged()
        self.assertEqual(self.execute(plan)["status"], "completed")

    def test_approve_and_execute_commits_once_and_rejects_replay(self):
        plan, opened = self.open()
        arguments = self.callback(opened)
        result = self.ui.confirm_and_execute(**arguments)["structuredContent"]
        self.assertIs(result["execution_requested"], True)
        self.assertEqual(result["execution"]["status"], "completed")
        self.assertEqual(result["next_action"]["tool"], "get_git_operation")
        with self.assertRaises((ValueError, HostRuntimeError)):
            self.ui.confirm_and_execute(**arguments)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "2")
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assertEqual(self.get(plan)["status"], "completed")

    def test_approve_and_execute_surfaces_pre_admission_source_drift(self):
        plan, opened = self.open()
        arguments = self.callback(opened)
        original_confirm = self.ui.confirm

        def confirm_then_drift(*args, **kwargs):
            confirmed = original_confirm(*args, **kwargs)
            (self.repo / "selected.txt").write_text("drift after confirmation\n")
            return confirmed

        with mock.patch.object(self.ui, "confirm", side_effect=confirm_then_drift):
            with self.assertRaisesRegex(ValueError, "stale"):
                self.ui.confirm_and_execute(**arguments)

        self.assertEqual(self.get(plan)["status"], "pending_host_approval")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only").stdout, "")

    def test_approve_and_execute_returns_persisted_post_admission_failure(self):
        _plan, opened = self.open()
        arguments = self.callback(opened)

        with mock.patch.object(
                self.workflows, "_execute_commit",
                side_effect=ValueError("synthetic admitted failure")):
            result = self.ui.confirm_and_execute(**arguments)["structuredContent"]

        self.assertIs(result["execution_requested"], True)
        self.assertIs(result["execution_started"], True)
        self.assertEqual(result["execution"]["status"], "failed")
        self.assertIn("synthetic admitted failure", result["execution"]["result"]["error"])
        self.assert_unchanged()

    def test_two_simultaneous_callbacks_cannot_create_two_commits(self):
        from concurrent.futures import ThreadPoolExecutor
        _, opened = self.open()
        arguments = self.callback(opened)
        def click():
            try:
                return self.ui.confirm_and_execute(**arguments)["structuredContent"]
            except (ValueError, HostRuntimeError):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: click(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "2")

    def test_wrong_capability_cannot_issue_a_receipt(self):
        _, opened = self.open()
        arguments = self.callback(opened)
        arguments["confirmation_token"] = "0" * 64
        with self.assertRaises((ValueError, HostRuntimeError)):
            self.ui.confirm(**arguments)
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_changed_source_is_rejected_before_user_confirmation(self):
        _, opened = self.open()
        (self.repo / "selected.txt").write_text("different\n")
        with self.assertRaises((ValueError, HostRuntimeError)):
            self.ui.confirm(**self.callback(opened))
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_expired_card_cannot_issue_a_receipt(self):
        from datetime import datetime, timedelta, timezone
        _, opened = self.open()
        future = datetime.now(timezone.utc) + timedelta(days=8)
        with mock.patch.object(self.module.host_access, "_utcnow", return_value=future):
            with self.assertRaises((ValueError, HostRuntimeError)):
                self.ui.confirm(**self.callback(opened))
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_changed_runtime_cannot_confirm_an_old_card(self):
        _, opened = self.open()
        self.runtime["id"] = "e" * 64
        with self.assertRaises((ValueError, HostRuntimeError)):
            self.ui.confirm(**self.callback(opened))
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_changed_boot_cannot_confirm_an_old_card(self):
        _, opened = self.open()
        with mock.patch.object(self.module.host_access, "_current_boot_id",
                               return_value="11111111-1111-1111-1111-111111111111"):
            with self.assertRaises((ValueError, HostRuntimeError)):
                self.ui.confirm(**self.callback(opened))
        self.assertEqual(list(self.state.rglob("*.approval.json")), [])
        self.assert_unchanged()

    def test_reopening_a_cli_approved_plan_needs_no_new_capability(self):
        plan = self.prepare()
        self.approve(plan)
        opened = self.ui.open("repo", plan["operation_id"])
        self.assertNotIn("_meta", opened)
        self.assertIs(opened["structuredContent"]["receipt_verified"], True)
        self.assertIs(opened["structuredContent"]["approval_required"], False)
        self.assert_unchanged()

    def test_grant_authorized_plan_opens_without_asking_for_approval(self):
        self.workflows.host_access = OwnerGitAccess(allowed=True)
        _, opened = self.open()
        self.assertNotIn("_meta", opened)
        self.assertIs(opened["structuredContent"]["authorization_verified"], True)
        self.assertIs(opened["structuredContent"]["approval_required"], False)
        self.assert_unchanged()

    def test_cancel_does_not_overwrite_an_approval_won_by_cli(self):
        plan, opened = self.open()
        self.approve(plan)
        result = self.ui.cancel(**self.callback(opened))["structuredContent"]
        self.assertEqual(result["state"], "approved")
        self.assertIs(result["receipt_verified"], True)
        self.assertEqual(self.execute(plan)["status"], "completed")

    def test_cancel_makes_the_plan_nonexecutable(self):
        plan, opened = self.open()
        result = self.ui.cancel(**self.callback(opened))["structuredContent"]
        self.assertEqual(result["state"], "cancelled")
        with self.assertRaisesRegex(ValueError, "not executable"):
            self.execute(plan)
        self.assert_unchanged()


class GitGrantPushTests(GitFixture, unittest.TestCase):
    def test_large_push_does_not_reuse_host_git_grant(self):
        from tests.test_git_workflows import LocalRemoteAdapter
        remote = self.base / "large-remote.git"
        remote.mkdir()
        git(remote, "init", "--bare")
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "HEAD:refs/heads/feature/chat")
        for index in range(129):
            (self.repo / f"bulk-{index:03d}.txt").write_text(f"{index}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "large push fixture")
        self.workflows.remote_adapter = LocalRemoteAdapter()
        self.workflows.host_access = OwnerGitAccess(allowed=True)

        plan = self.workflows.prepare({
            "project": "repo", "action": "push", "branch": "feature/chat",
            "new_branch": False, "large_push": True,
        })

        self.assertIs(plan["approval_required"], True)
        self.assertEqual(plan["authorization"], {"mode": "per_operation"})

    def test_explicit_git_grant_pushes_only_the_prepared_feature_ref(self):
        from tests.test_git_workflows import LocalRemoteAdapter
        remote = self.base / "remote.git"
        remote.mkdir()
        git(remote, "init", "--bare")
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "HEAD:refs/heads/main")
        published_main = git(remote, "rev-parse", "refs/heads/main").stdout.strip()
        self.workflows.remote_adapter = LocalRemoteAdapter()
        self.workflows.host_access = OwnerGitAccess(allowed=True)
        plan = self.workflows.prepare({
            "project": "repo", "action": "push", "branch": "feature/chat", "new_branch": True,
        })
        self.assertIs(plan["approval_required"], False)
        self.assertEqual(self.execute(plan)["status"], "completed")
        self.assertEqual(git(remote, "rev-parse", "refs/heads/feature/chat").stdout.strip(), self.head)
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main").stdout.strip(), published_main)

    def test_host_or_codex_permission_is_not_a_git_opt_in(self):
        access = OwnerGitAccess(allowed=True)
        access.grant.update(allow_git=False, allow_codex=True)
        self.workflows.host_access = access
        with self.assertRaisesRegex(ValueError, "grant"):
            self.prepare()
        self.assert_unchanged()

    def test_a_git_grant_does_not_remove_the_snapshot_gate(self):
        self.workflows.host_access = OwnerGitAccess(allowed=True)
        plan = self.prepare()
        (self.repo / "selected.txt").write_text("unreviewed\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.execute(plan)
        self.assert_unchanged()


class GitAsyncMutationLaneTests(GitFixture, unittest.IsolatedAsyncioTestCase):
    async def test_pre_admission_drift_is_a_bounded_tool_failure(self):
        import git_operation_ui
        tools = git_operation_ui.GitOperationApprovalTools(self.workflows)
        tools.manager._runtime_source = lambda: "d" * 64
        plan = self.prepare()
        opened = await tools.call("open_git_operation_approval", {
            "project": "repo", "operation_id": plan["operation_id"]})
        public = opened["structuredContent"]
        arguments = {
            "project": "repo", "operation_id": plan["operation_id"],
            "card_id": public["card_id"],
            "confirmation_token": opened["_meta"]["git_operation_confirmation"],
        }
        original_confirm = tools.manager.confirm

        def confirm_then_drift(*args, **kwargs):
            confirmed = original_confirm(*args, **kwargs)
            (self.repo / "selected.txt").write_text("drift after confirmation\n")
            return confirmed

        with mock.patch.object(tools.manager, "confirm", side_effect=confirm_then_drift):
            with self.assertRaisesRegex(ValueError, "GIT_CARD_PLAN_UNAVAILABLE"):
                await tools.call("confirm_and_execute_git_operation", arguments)

        self.assertEqual(self.get(plan)["status"], "pending_host_approval")
        self.assert_unchanged()

    async def test_cancellation_keeps_the_shared_lane_until_git_finishes(self):
        import asyncio
        import threading
        import git_operation_ui
        lock = asyncio.Lock()
        tools = git_operation_ui.GitOperationApprovalTools(
            self.workflows, project_locks={"repo": lock})
        tools.manager._runtime_source = lambda: "d" * 64
        plan = self.prepare()
        opened = await tools.call("open_git_operation_approval", {
            "project": "repo", "operation_id": plan["operation_id"]})
        public = opened["structuredContent"]
        args = {"project": "repo", "operation_id": plan["operation_id"],
                "card_id": public["card_id"],
                "confirmation_token": opened["_meta"]["git_operation_confirmation"]}
        entered = threading.Event()
        release = threading.Event()
        original = self.workflows._execute_commit
        def paused_commit(root, immutable):
            entered.set()
            if not release.wait(5):
                raise ValueError("test release timed out")
            return original(root, immutable)
        with mock.patch.object(self.workflows, "_execute_commit", side_effect=paused_commit):
            task = asyncio.create_task(tools.call("confirm_and_execute_git_operation", args))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(lock.locked())
                self.assertFalse(task.done())
                with self.assertRaisesRegex(ValueError, "PROJECT_BUSY"):
                    await tools.call("confirm_and_execute_git_operation", args)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(lock.locked())
        self.assertEqual(self.get(plan)["status"], "completed")
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").stdout.strip(), "2")


if __name__ == "__main__":
    unittest.main()
