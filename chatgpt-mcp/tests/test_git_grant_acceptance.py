"""Real owner authority ordering at Git acceptance and legacy fallback boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import unittest
from unittest import mock

from git_workflows import GitWorkflows
from tests import test_host_access_installation as owner_fixtures
from tests.test_git_chat_approval import git


class GitGrantAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.owner = owner_fixtures.HostAccessInstallationTests(
            "test_one_installation_grant_is_reusable_across_projects")
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.owner.now = datetime.now(timezone.utc)
        self.access = self.owner.access
        self.repo = self.owner.alpha
        git(self.repo, "init", "-b", "feature/atomic-authority")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.test")
        (self.repo / "selected.txt").write_text("before\n")
        git(self.repo, "add", "selected.txt")
        git(self.repo, "commit", "-m", "initial")
        self.head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "selected.txt").write_text("after\n")
        self.workflows = GitWorkflows(
            {"alpha": self.repo}, self.owner.root / "git-state",
            message_validator=lambda path: None, reference_checker=lambda url: None,
            host_access=self.access)

    def grant(self, *, installation=True, allow_git=True):
        if installation:
            self.owner.enable()
            return self.owner.approve_git(
                lifetime="until_revoked", duration_seconds=None, allow_git=allow_git)
        return self.owner.approve_git(duration_seconds=3600, allow_git=allow_git)

    def prepare(self):
        return self.workflows.prepare({
            "project": "alpha", "action": "commit", "paths": ["selected.txt"],
            "message": "Record reviewed update\n\nhttps://github.com/example/repo/issues/1\n"})

    def execute(self, plan):
        return self.workflows.execute({"project": "alpha", "operation_id": plan["operation_id"]})

    def revoke(self, grant):
        return self.access.revoke_web("alpha", grant_id=grant["grant_id"], confirmation="alpha")

    def test_revocation_cannot_finish_between_final_validation_and_acceptance(self):
        grant = self.grant()
        plan = self.prepare()
        revoker_started = threading.Event()
        revoked = threading.Event()
        events = []
        failures = []
        threads = []
        observed = {}
        save = self.workflows._save_plan
        commit = self.workflows._execute_commit

        def revoke():
            revoker_started.set()
            try:
                self.revoke(grant)
                events.append("revoked")
            except BaseException as exc:
                failures.append(exc)
            finally:
                revoked.set()

        def accepting(state):
            if state["status"] == "executing":
                thread = threading.Thread(target=revoke)
                threads.append(thread)
                thread.start()
                self.assertTrue(revoker_started.wait(2))
                observed["revoked_before_acceptance"] = revoked.wait(0.5)
                save(state)
                events.append("accepted")
                return
            save(state)

        def execute_after_acceptance(root, immutable):
            # The authority lock must already be released before the Git subprocess.
            self.assertTrue(revoked.wait(2), "grant lock incorrectly covers Git execution")
            events.append("git-started")
            return commit(root, immutable)

        try:
            with mock.patch.object(self.workflows, "_save_plan", side_effect=accepting), \
                    mock.patch.object(self.workflows, "_execute_commit", side_effect=execute_after_acceptance):
                result = self.execute(plan)
        finally:
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(observed["revoked_before_acceptance"],
                         "revocation completed after validation but before Git acceptance")
        self.assertEqual(events, ["accepted", "revoked", "git-started"])
        self.assertEqual(result["status"], "completed")

    def test_revocation_winning_first_prevents_acceptance_and_commit(self):
        grant = self.grant()
        plan = self.prepare()
        self.revoke(grant)
        with self.assertRaisesRegex(ValueError, "grant"):
            self.execute(plan)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)
        state = self.workflows._load_plan("alpha", plan["operation_id"])
        self.assertEqual(state["status"], "pending_host_approval")

    def check_legacy_fallback(self, boundary):
        self.grant(installation=False, allow_git=False)
        if boundary == "runtime":
            self.owner.runtime_identity = "b" * 64
        else:
            self.owner.boot_id = "99999999-8888-7777-6666-555555555555"
        try:
            plan = self.prepare()
        except ValueError as exc:
            self.fail("inactive legacy grant blocked the single-use approval path: " + str(exc))
        self.assertIs(plan["approval_required"], True)
        self.assertEqual(plan["authorization"], {"mode": "per_operation"})
        with self.assertRaisesRegex(ValueError, "approval"):
            self.execute(plan)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)

    def test_v1_stale_runtime_without_git_opt_in_keeps_single_use_approval(self):
        self.check_legacy_fallback("runtime")

    def test_v1_stale_boot_without_git_opt_in_keeps_single_use_approval(self):
        self.check_legacy_fallback("boot")

    def test_stale_v1_grant_never_falls_back_when_already_bound_to_a_plan(self):
        self.grant(installation=False)
        plan = self.prepare()
        self.owner.runtime_identity = "b" * 64
        with self.assertRaisesRegex(ValueError, "grant"):
            self.execute(plan)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)

    def test_unsafe_runtime_is_not_a_single_use_approval_fallback(self):
        self.grant(installation=False, allow_git=False)
        self.owner.runtime_identity = None
        with self.assertRaisesRegex(ValueError, "UNSAFE_RUNTIME"):
            self.prepare()
        self.assertEqual(list((self.owner.root / "git-state").rglob("*.plan.json")), [])


if __name__ == "__main__":
    unittest.main()
