"""Git commit/push approval via authentic one-use app-only token.

Only temporary fixture Git repos; never pushes to a real remote or user repo.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_git_workflows import run_git, LocalRemoteAdapter
from git_workflows import GitWorkflows
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
