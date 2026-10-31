"""No real credentials or remote writes: temporary local Git/bare fixtures only."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_git_commit_access as baseline
from test_git_workflows import run_git

class DevelopmentAccessTest(unittest.TestCase):
    setUp = baseline.GitCommitAccessTest.setUp
    head = baseline.GitCommitAccessTest.head
    args = baseline.GitCommitAccessTest.args
    plan = baseline.GitCommitAccessTest.plan
    execute = baseline.GitCommitAccessTest.execute
    assert_denied = baseline.GitCommitAccessTest.assert_denied

    def remote(self):
        bare = self.root / 'remote.git'
        run_git(self.root, 'init', '--bare', str(bare))
        run_git(self.repo, 'remote', 'add', 'origin', str(bare))
        run_git(self.repo, 'push', 'origin', 'feature/test')
        return bare

    def dev_open(self, limit=2):
        return self.access.open('repo', duration_seconds=600, allow_push=True, push_limit=limit)

    def dev_approve(self, limit=2):
        opened = self.dev_open(limit)
        self.access.confirm('repo', **self.args(opened))
        return self.access.status('repo')

    def push_plan(self):
        return self.git.prepare({'project':'repo', 'action':'push', 'branch':'feature/test', 'new_branch':False})

    def test_explicit_scope_card_no_authority_before_user_confirmation(self):
        bare = self.remote()
        before = self.head()
        c = self.dev_open()['structuredContent']
        self.assertEqual(c['state'], 'pending')
        self.assertEqual(c['operations'], ['commit', 'push'])
        self.assertEqual(c['push_limit'], 2)
        self.assertEqual(c['origin'], str(bare))
        self.assertFalse(self.access.status('repo')['active'])
        self.assertEqual(before, self.head())

    def test_same_grant_reuses_two_commits_and_two_bounded_pushes(self):
        bare = self.remote(); initial = self.dev_approve()
        self.assertEqual(initial['operations'], ['commit','push'])
        for i in range(2):
            plan = self.plan('approved-' + str(i))
            self.assertIs(plan['approval_required'], False)
            self.assertEqual(self.execute(plan)['status'], 'completed')
            push = self.push_plan()
            self.assertIs(push['approval_required'], False)
            self.assertEqual(self.execute(push)['status'], 'completed')
            s = self.access.status('repo')
            self.assertEqual(s['grant_id'], initial['grant_id'])
            self.assertEqual(s['expires_at_epoch'], initial['expires_at_epoch'])
            self.assertEqual(s['pushes_remaining'], 1 - i)
            self.assertFalse(self.git._receipt_path('repo',push['operation_id']).exists())
            self.assertEqual(run_git(bare,'rev-parse','refs/heads/feature/test').stdout.strip(),self.head())

    def test_exhausted_push_budget_never_blocks_allowed_commit_or_silently_refills(self):
        self.remote(); self.dev_approve(1)
        self.execute(self.plan('first')); self.execute(self.push_plan())
        self.assertTrue(self.access.status('repo')['active'])
        self.assertEqual(self.access.status('repo')['pushes_remaining'],0)
        self.execute(self.plan('second'))
        p=self.push_plan(); self.assertTrue(p.get('approval_required',True))
        self.assert_denied(lambda:self.execute(p))
        self.access.open('repo',allow_push=True,push_limit=10)
        self.assertEqual(self.access.status('repo')['pushes_remaining'],0)

    def test_large_push_never_reuses_development_push_grant(self):
        self.remote()
        for index in range(129):
            self.repo.joinpath(f"large-{index:03d}.txt").write_text(f"{index}\n")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-m", "large push fixture")
        active = self.dev_approve()
        plan = self.git.prepare({
            "project": "repo", "action": "push", "branch": "feature/test",
            "new_branch": False, "large_push": True,
        })
        self.assertTrue(plan["large_push"])
        self.assertTrue(plan.get("approval_required", True))
        stored = self.git._load_plan("repo", plan["operation_id"])
        self.assertNotIn("development_grant_id", stored["immutable"])
        self.assert_denied(lambda: self.execute(plan))
        status = self.access.status("repo")
        self.assertTrue(status["active"])
        self.assertEqual(status["grant_id"], active["grant_id"])

    def test_existing_commit_only_grant_cannot_be_silently_upgraded(self):
        self.remote(); old=self.access.open('repo');self.access.confirm('repo',**self.args(old))
        card=self.dev_open()
        self.assertEqual(card['structuredContent']['state'],'pending')
        self.assertEqual(self.access.status('repo')['operations'],['commit'])
        self.assert_denied(lambda:self.execute(self.push_plan()))
        self.access.confirm('repo',**self.args(card))
        self.assertEqual(self.access.status('repo')['operations'],['commit','push'])

    def test_prepared_plan_is_pinned_to_specific_development_grant(self):
        self.remote(); initial=self.dev_approve();plan=self.plan()
        stored=self.git._load_plan('repo',plan['operation_id'])
        self.assertEqual(stored['immutable'].get('development_grant_id'),initial['grant_id'])
        card=self.access.open('repo');self.access.revoke('repo',**self.args(card))
        replacement=self.dev_approve()
        self.assertNotEqual(initial['grant_id'],replacement['grant_id'])
        self.assert_denied(lambda:self.execute(plan))

    def test_development_plan_cannot_use_a_replacement_commit_only_grant(self):
        self.remote()
        initial = self.dev_approve()
        plan = self.plan()
        card = self.access.open('repo')
        self.access.revoke('repo', **self.args(card))
        card = self.access.open('repo')
        self.access.confirm('repo', **self.args(card))
        replacement = self.access.status('repo')
        self.assertEqual(replacement['operations'], ['commit'])
        self.assertNotEqual(replacement['grant_id'], initial['grant_id'])
        before_head = self.head()
        grant_path = self.access.grant_path('repo')
        before_grant = grant_path.read_bytes()

        self.assert_denied(lambda: self.execute(plan))

        self.assertEqual(self.head(), before_head)
        self.assertEqual(grant_path.read_bytes(), before_grant)
        self.assertEqual(run_git(self.repo, 'diff', '--cached', '--name-only').stdout, '')

    def _assert_expiry_during_remote_check_blocks_admission(self, *, wall_seconds=0,
                                                          monotonic_ns=0):
        bare = self.remote()
        self.dev_approve()
        plan = self.push_plan()
        stored = self.git._load_plan('repo', plan['operation_id'])
        grant_path = self.access.grant_path('repo')
        before_grant = grant_path.read_bytes()
        before_remote = run_git(bare, 'rev-parse', 'refs/heads/feature/test').stdout
        original_adapter = self.git.remote_adapter

        def remote_check_then_expire(*args):
            result = original_adapter(*args)
            self.wall += wall_seconds
            self.mono += monotonic_ns
            return result

        self.git.remote_adapter = remote_check_then_expire
        with self.git._lock('repo'):
            authorization = self.access.begin_locked(stored)

        self.assertIsNone(authorization)
        self.assertEqual(grant_path.read_bytes(), before_grant)
        self.assertEqual(run_git(bare, 'rev-parse', 'refs/heads/feature/test').stdout,
                         before_remote)

    def test_wall_expiry_during_remote_check_blocks_admission(self):
        self._assert_expiry_during_remote_check_blocks_admission(wall_seconds=600)

    def test_monotonic_expiry_during_remote_check_blocks_admission(self):
        self._assert_expiry_during_remote_check_blocks_admission(
            monotonic_ns=600_000_000_000)

    def test_origin_change_before_callback_refuses_approval(self):
        self.remote();card=self.dev_open()
        run_git(self.repo,'remote','set-url','origin',str(self.root/'other.git'))
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
        self.assertFalse(self.access.status('repo')['active'])

    def test_origin_change_after_approval_refuses_push(self):
        self.remote();self.dev_approve();self.execute(self.plan())
        p=self.push_plan()
        run_git(self.repo,'remote','set-url','origin',str(self.root/'other.git'))
        self.assert_denied(lambda:self.execute(p))

    def test_remote_head_change_after_plan_is_rejected(self):
        bare=self.remote();self.dev_approve();self.execute(self.plan())
        p=self.push_plan()
        run_git(self.repo,'push','origin','feature/test')
        self.assert_denied(lambda:self.execute(p))
        self.assertEqual(run_git(bare,'rev-parse','refs/heads/feature/test').stdout.strip(),self.head())

    def test_missing_remote_branch_cannot_receive_reusable_push_permission(self):
        self.remote(); run_git(self.repo,'push','origin','--delete','feature/test')
        self.assert_denied(self.dev_open)
        self.assertFalse(self.access.status('repo')['active'])

    def test_expiry_and_revocation_deny_reuse(self):
        self.remote();self.dev_approve();p=self.push_plan()
        self.wall+=600;self.mono+=600_000_000_000
        self.assert_denied(lambda:self.execute(p))
        self.dev_approve();c=self.access.open('repo')
        self.access.revoke('repo',**self.args(c))
        self.assert_denied(lambda:self.execute(self.push_plan()))

    def test_invalid_flags_and_bounds_never_issue_authority(self):
        self.remote()
        for opts in ({'allow_push':'yes'}, {'allow_push':1}, {'allow_push':True,'push_limit':0},
                     {'allow_push':True,'push_limit':11}, {'allow_push':True,'push_limit':True}):
            with self.subTest(opts=opts):
                self.assert_denied(lambda:self.access.open('repo',**opts))
        self.assertFalse(self.access.status('repo')['active'])

    def test_protected_branch_and_external_head_change_are_not_covered(self):
        self.remote();self.dev_approve()
        run_git(self.repo,'commit','--allow-empty','-m','outside workflow')
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda:self.execute(self.push_plan()))
        run_git(self.repo,'checkout','-b','main')
        self.assert_denied(self.dev_open)

    def test_failed_push_blocks_reuse_without_automatic_retry(self):
        self.remote();self.dev_approve();self.execute(self.plan());p=self.push_plan()
        with mock.patch.object(self.git,'_execute_push',side_effect=ValueError('unverified outcome')):
            self.assert_denied(lambda:self.execute(p))
        self.assertFalse(self.access.status('repo')['active'])
        state = self.git.get({'project': 'repo', 'operation_id': p['operation_id']})
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('requires_manual_verification', state['result'])
        self.assert_denied(lambda:self.execute(p))

    def test_runtime_change_requires_fresh_explicit_scope_not_automatic_revival(self):
        self.remote();self.dev_approve();self.runtime='b'*64
        self.assertFalse(self.access.status('repo')['active'])
        card=self.dev_open()
        self.assertEqual(card['structuredContent']['state'],'pending')
        self.assertFalse(self.access.status('repo')['active'])
        self.access.confirm('repo',**self.args(card))
        self.assertTrue(self.access.status('repo')['active'])


    def _prepare_staged_inspection_failure(self):
        opened = self.access.open('repo')
        self.access.confirm('repo', **self.args(opened))
        (self.repo / 'untouched.txt').write_text('unrelated worktree edit\n')
        plan = self.plan('selected worktree edit\n')
        original = self.git._staged_index_state

        def inspect(root, paths):
            if run_git(root, 'diff', '--cached', '--name-only').stdout.strip():
                raise ValueError('fixture staged inspection failure')
            return original(root, paths)

        return plan, inspect

    def test_staged_inspection_failure_restores_index_and_preserves_worktree(self):
        plan, inspect = self._prepare_staged_inspection_failure()
        before = self.head()
        files = {name: (self.repo / name).read_bytes()
                 for name in ('one.txt', 'untouched.txt')}
        with mock.patch.object(self.git, '_staged_index_state', side_effect=inspect):
            with self.assertRaises(ValueError):
                self.execute(plan)
        self.assertEqual(run_git(self.repo, 'diff', '--cached', '--name-only').stdout, '')
        self.assertEqual(self.head(), before)
        for name, content in files.items():
            self.assertEqual((self.repo / name).read_bytes(), content)
        self.assertEqual(self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})['status'], 'failed')
        record = self.access._loaded('repo')
        self.assertEqual(record['in_flight']['operation_id'], plan['operation_id'])
        self.assertEqual(record['grant']['state'], 'revoked')
        self.assert_denied(lambda: self.execute(plan))

    def test_staged_inspection_cleanup_failure_requires_index_inspection(self):
        plan, inspect = self._prepare_staged_inspection_failure()
        with mock.patch.object(self.git, '_staged_index_state', side_effect=inspect), \
                mock.patch.object(self.git, '_restore_selected_index', return_value=False):
            with self.assertRaisesRegex(ValueError, 'inspect the index'):
                self.execute(plan)
        self.assertEqual(run_git(self.repo, 'diff', '--cached', '--name-only').stdout.strip(), 'one.txt')
        state = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(state['status'], 'failed')
        self.assertIn('inspect the index', state['result']['error'])
        self.assertIsNotNone(self.access._loaded('repo')['in_flight'])
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda: self.execute(plan))

    def test_staged_inspection_cleanup_exception_requires_index_inspection(self):
        plan, inspect = self._prepare_staged_inspection_failure()
        with mock.patch.object(self.git, '_staged_index_state', side_effect=inspect), \
                mock.patch.object(self.git, '_restore_selected_index',
                                  side_effect=OSError('fixture cleanup unavailable')):
            with self.assertRaisesRegex(ValueError, 'inspect the index'):
                self.execute(plan)
        state = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(state['status'], 'failed')
        self.assertIn('inspect the index', state['result']['error'])
        self.assertIsNotNone(self.access._loaded('repo')['in_flight'])
        self.assertFalse(self.access.status('repo')['active'])

    def _push_result_read_failure(self, *, one_use=False):
        bare = self.remote()
        if one_use:
            (self.repo / 'one.txt').write_text('one-use push fixture\n')
            run_git(self.repo, 'add', 'one.txt')
            run_git(self.repo, 'commit', '-m', 'one-use fixture')
        else:
            self.dev_approve()
            self.execute(self.plan('push result-read fixture\n'))
        plan = self.push_plan()
        receipt = self.git._receipt_path('repo', plan['operation_id'])
        if one_use:
            baseline.GitCommitAccessTest.approve_one_use_fixture(self, plan)
            self.assertTrue(receipt.exists())
        wanted = self.head()
        grant_before = copy.deepcopy(self.access._loaded('repo'))
        adapter = self.git.remote_adapter
        original_head = self.git._head
        pushed = False

        class Remote:
            def __call__(adapter_self, root, url, branch):
                return adapter(root, url, branch)

            def push(adapter_self, root, branch):
                nonlocal pushed
                result = adapter.push(root, branch)
                pushed = True
                return result

        def head_after_push(root):
            if pushed:
                raise ValueError('fixture result read failed after successful push')
            return original_head(root)

        with mock.patch.object(self.git, 'remote_adapter', Remote()), \
                mock.patch.object(self.git, '_head', side_effect=head_after_push):
            with self.assertRaises(ValueError):
                self.execute(plan)
        self.assertTrue(pushed)
        self.assertEqual(run_git(bare, 'rev-parse', 'refs/heads/feature/test').stdout.strip(), wanted)
        state = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(state['status'], 'outcome_unknown')
        self.assertTrue(state['result']['requires_manual_verification'])
        self.assertEqual(state['result']['branch'], 'feature/test')
        self.assertNotIn('pushed', state['result'])
        self.assertFalse(receipt.exists())
        if one_use:
            self.assertIsNone(self.access._loaded('repo'))
        else:
            record = self.access._loaded('repo')
            self.assertEqual(record['in_flight']['operation_id'], plan['operation_id'])
            self.assertEqual(record['grant']['state'], 'revoked')
            self.assertEqual(record['grant']['grant_id'], grant_before['grant']['grant_id'])
            self.assertEqual(record['grant']['expires_at'], grant_before['grant']['expires_at'])
            self.assertEqual(record['grant']['pushes_used'], grant_before['grant']['pushes_used'])
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda: self.execute(plan))
        self.assertEqual(run_git(bare, 'rev-parse', 'refs/heads/feature/test').stdout.strip(), wanted)

    def test_successful_push_with_failed_result_read_is_outcome_unknown(self):
        self._push_result_read_failure()

    def test_one_use_push_result_failure_preserves_consumed_receipt(self):
        self._push_result_read_failure(one_use=True)

    def test_successful_commit_with_failed_result_read_is_outcome_unknown(self):
        opened = self.access.open('repo')
        self.access.confirm('repo', **self.args(opened))
        plan = self.plan('commit result-read fixture\n')
        before = self.head()
        original_head = self.git._head

        def head_after_commit(root):
            if run_git(root, 'rev-parse', 'HEAD').stdout.strip() != before:
                raise ValueError('fixture result read failed after successful commit')
            return original_head(root)

        with mock.patch.object(self.git, '_head', side_effect=head_after_commit):
            with self.assertRaises(ValueError):
                self.execute(plan)
        after = self.head()
        self.assertNotEqual(after, before)
        self.assertEqual(run_git(self.repo, 'diff', '--cached', '--name-only').stdout, '')
        state = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(state['status'], 'outcome_unknown')
        self.assertTrue(state['result']['requires_manual_verification'])
        self.assertNotIn('commit', state['result'])
        record = self.access._loaded('repo')
        self.assertEqual(record['in_flight']['operation_id'], plan['operation_id'])
        self.assertEqual(record['grant']['expected_head'], before)
        self.assertEqual(record['grant']['state'], 'revoked')
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda: self.execute(plan))
        self.assertEqual(self.head(), after)


class SelectedPlanSnapshotTest(unittest.TestCase):
    setUp = baseline.GitCommitAccessTest.setUp
    head = baseline.GitCommitAccessTest.head
    args = baseline.GitCommitAccessTest.args
    plan = baseline.GitCommitAccessTest.plan
    execute = baseline.GitCommitAccessTest.execute
    assert_denied = baseline.GitCommitAccessTest.assert_denied
    def approve(self):
        c=self.access.open('repo');self.access.confirm('repo',**self.args(c))
    def test_unrelated_untracked_note_does_not_stale_selected_commit(self):
        self.approve();p=self.plan();(self.repo/'handoff.md').write_text('unrelated log')
        self.assertEqual(self.execute(p)['status'],'completed')
        self.assertEqual((self.repo/'handoff.md').read_text(),'unrelated log')
        self.assertNotIn('handoff.md',run_git(self.repo,'ls-tree','--name-only','HEAD').stdout)
    def test_unrelated_index_change_still_blocks(self):
        self.approve();p=self.plan();(self.repo/'untouched.txt').write_text('not approved')
        run_git(self.repo,'add','untouched.txt')
        self.assert_denied(lambda:self.execute(p))
    def test_selected_source_change_still_blocks(self):
        self.approve();p=self.plan();(self.repo/'one.txt').write_text('not approved')
        self.assert_denied(lambda:self.execute(p))

if __name__=='__main__':unittest.main()
