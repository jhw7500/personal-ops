"""Regressions for owner-visible recovery from a changed approval runtime.

Only temporary fixture repositories and synthetic authority are used. These
checks do not modify installed grants or authorize host/Git operations.
"""
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_git_commit_access as baseline


class GitCommitRecoveryTest(baseline.GitCommitAccessTest):
    def test_scope_failure_reports_field_names_without_private_state(self):
        self.approve()
        self.runtime = 'b' * 64
        status = self.access.status('repo')
        self.assertFalse(status['active'])
        self.assertEqual(status['reason'], 'SCOPE_MISMATCH')
        self.assertEqual(status.get('scope_mismatch_fields'), ['runtime_digest'])
        public = json.dumps(status)
        self.assertNotIn('a' * 64, public)
        self.assertNotIn('b' * 64, public)
        self.assertNotIn('confirmation_token', public)

    def test_runtime_drift_opens_pending_reapproval_without_issuing_authority(self):
        self.approve()
        prior = copy.deepcopy(self.access._loaded('repo'))
        head = self.head()
        self.runtime = 'b' * 64
        opened = self.opened()
        public = opened['structuredContent']
        self.assertEqual(public['state'], 'pending')
        self.assertEqual(public.get('renewal_reason'), 'RUNTIME_CHANGED')
        self.assertEqual(public.get('replaces_grant_id'), prior['grant']['grant_id'])
        self.assertEqual(self.access._loaded('repo'), prior)
        self.assertFalse(self.access.status('repo')['active'])
        self.assertEqual(self.head(), head)
        self.assert_denied(lambda: self.execute(self.plan()))

    def test_explicit_reapproval_recovers_then_two_commits_reuse_the_new_grant(self):
        self.approve()
        old_id = self.access._loaded('repo')['grant']['grant_id']
        self.runtime = 'b' * 64
        opened = self.opened()
        self.assertEqual(opened['structuredContent']['state'], 'pending')
        self.access.confirm('repo', **self.args(opened))
        current = self.access.status('repo')
        self.assertTrue(current['active'])
        self.assertNotEqual(current['grant_id'], old_id)
        expiry = current['expires_at_epoch']
        for contents in ('recovery-first\n', 'recovery-second\n'):
            plan = self.plan(contents)
            self.assertIs(plan.get('approval_required'), False)
            self.assertEqual(self.execute(plan)['status'], 'completed')
            self.assertTrue(self.access.status('repo')['active'])
            self.assertEqual(self.access.status('repo')['expected_head'], self.head())
            self.assertEqual(self.access.status('repo')['expires_at_epoch'], expiry)
            self.assertFalse(self.git._receipt_path('repo', plan['operation_id']).exists())

    def test_recovery_confirmation_refuses_further_runtime_change(self):
        self.approve()
        previous = copy.deepcopy(self.access._loaded('repo'))
        self.runtime = 'b' * 64
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'pending')
        self.runtime = 'c' * 64
        self.assert_denied(lambda: self.access.confirm('repo', **self.args(card)))
        self.assertEqual(self.access._loaded('repo'), previous)
        self.assertFalse(self.access.status('repo')['active'])

    def test_inflight_outcome_is_not_erased_by_runtime_reapproval(self):
        self.approve()
        public_plan = self.plan()
        plan = self.git._load_plan('repo', public_plan['operation_id'])
        with self.git._lock('repo'):
            self.access.begin_locked(plan)
        previous = copy.deepcopy(self.access._loaded('repo'))
        self.runtime = 'b' * 64
        card = self.opened()
        self.assertNotEqual(card['structuredContent']['state'], 'pending')
        self.assertEqual(self.access._loaded('repo'), previous)
        self.assert_denied(lambda: self.access.confirm('repo', **self.args(card)))

    def _assert_expired_inflight_open_preserves_state(self, *, wall_seconds=0,
                                                     monotonic_ns=0, keep_card=True):
        self.approve(30)
        plan = self.plan()
        stored = self.git._load_plan('repo', plan['operation_id'])
        with self.git._lock('repo'):
            self.access.begin_locked(stored)
        grant_path = self.access.grant_path('repo')
        card_path = self.access._card_path('repo')
        if not keep_card:
            card_path.unlink()
        grant_before = grant_path.read_bytes()
        card_before = card_path.read_bytes() if keep_card else None
        self.wall += wall_seconds
        self.mono += monotonic_ns

        with self.assertRaisesRegex(baseline.HostRuntimeError,
                                    'GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN'):
            self.opened()

        self.assertEqual(grant_path.read_bytes(), grant_before)
        if keep_card:
            self.assertEqual(card_path.read_bytes(), card_before)
        else:
            self.assertFalse(card_path.exists())

    def test_expired_inflight_grant_cannot_offer_a_fresh_approval(self):
        self._assert_expired_inflight_open_preserves_state(
            wall_seconds=31, monotonic_ns=31_000_000_000)

    def test_wall_expired_inflight_grant_preserves_existing_card(self):
        self._assert_expired_inflight_open_preserves_state(wall_seconds=31)

    def test_monotonic_expired_inflight_grant_preserves_existing_card(self):
        self._assert_expired_inflight_open_preserves_state(monotonic_ns=31_000_000_000)

    def test_expired_inflight_grant_does_not_create_a_missing_card(self):
        self._assert_expired_inflight_open_preserves_state(wall_seconds=31, keep_card=False)

    def test_expired_grant_with_missing_completion_plan_preserves_authority_and_card(self):
        self.approve(30)
        plan = self.plan()
        self.assertEqual(self.execute(plan)['status'], 'completed')
        self.assertIsNone(self.access._loaded('repo')['in_flight'])
        self.git._plan_path('repo', plan['operation_id']).unlink()
        grant_path = self.access.grant_path('repo')
        card_path = self.access._card_path('repo')
        grant_before, card_before = grant_path.read_bytes(), card_path.read_bytes()
        self.wall += 31

        with self.assertRaisesRegex(baseline.HostRuntimeError,
                                    'GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN'):
            self.opened()

        self.assertEqual(grant_path.read_bytes(), grant_before)
        self.assertEqual(card_path.read_bytes(), card_before)

    def test_expired_settled_commit_can_offer_fresh_approval_without_granting(self):
        self.approve(30)
        self.assertEqual(self.execute(self.plan())['status'], 'completed')
        grant_path = self.access.grant_path('repo')
        grant_before = grant_path.read_bytes()
        self.wall += 31
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'pending')
        self.assertEqual(grant_path.read_bytes(), grant_before)
        self.assertFalse(self.access.status('repo')['active'])

    def test_active_inflight_grant_can_still_be_revoked_without_erasing_execution(self):
        self.approve()
        plan = self.plan()
        with self.git._lock('repo'):
            self.access.begin_locked(self.git._load_plan('repo', plan['operation_id']))
        previous = copy.deepcopy(self.access._loaded('repo'))
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'active')
        self.access.revoke('repo', **self.args(card))
        current = self.access._loaded('repo')
        self.assertEqual(current['grant']['state'], 'revoked')
        self.assertEqual(current['in_flight'], previous['in_flight'])
        self.assertEqual(current['grant']['grant_id'], previous['grant']['grant_id'])

    def test_revoke_between_renewal_card_and_callback_blocks_replacement(self):
        self.approve();self.runtime='b'*64;card=self.opened()
        record=self.access._loaded('repo');record['grant']['state']='revoked'
        self.access._save(self.access.grant_path('repo'),record)
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
        self.assertEqual(self.access._loaded('repo')['grant']['state'],'revoked')

    def test_runtime_and_external_head_drift_requires_explicit_current_head_confirmation(self):
        from test_git_workflows import run_git
        self.approve();self.runtime='b'*64
        run_git(self.repo,'commit','--allow-empty','-m','separately approved fixture')
        card=self.opened()
        self.assertEqual(card['structuredContent']['state'],'pending')
        self.assertEqual(card['structuredContent']['head'],self.head())
        self.assertFalse(self.access.status('repo')['active'])
        self.access.confirm('repo',**self.args(card))
        self.assertTrue(self.access.status('repo')['active'])



if __name__ == '__main__':
    import unittest
    unittest.main()
