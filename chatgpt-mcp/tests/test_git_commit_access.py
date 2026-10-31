import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from git_workflows import GitWorkflows, approve_operation
from git_commit_access import CommitAccess, GitCommitAccessTools, tool_specs
from host_runtime_config import HostRuntimeError
from test_git_workflows import run_git, LocalRemoteAdapter, TTYBuffer

class GitCommitAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='git24-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.repo=self.root/'repo';self.repo.mkdir()
        run_git(self.repo,'init','-b','feature/test')
        run_git(self.repo,'config','user.name','Fixture')
        run_git(self.repo,'config','user.email','fixture@example.com')
        (self.repo/'one.txt').write_text('one\n')
        (self.repo/'untouched.txt').write_text('preserve\n')
        run_git(self.repo,'add','one.txt','untouched.txt');run_git(self.repo,'commit','-m','fixture')
        self.git=GitWorkflows({'repo':self.repo},self.root/'state',message_validator=lambda p:None,reference_checker=lambda u:None,remote_adapter=LocalRemoteAdapter())
        self.wall=1900000000;self.mono=100000000000
        self.runtime='a'*64;self.boot='11111111-1111-1111-1111-111111111111'
        self.access=CommitAccess(self.git,trusted_runtime=lambda:self.runtime,wall_clock=lambda:self.wall,monotonic_clock=lambda:self.mono,boot_source=lambda:self.boot)
        self.git.commit_access=self.access
    def head(self): return run_git(self.repo,'rev-parse','HEAD').stdout.strip()
    def opened(self,seconds=86400): return self.access.open('repo',duration_seconds=seconds)
    def args(self,card): return {'card_id':card['structuredContent']['card_id'],'confirmation_token':card['_meta']['git_commit_access_confirmation']}
    def approve(self,seconds=86400):
        opened=self.opened(seconds)
        return self.access.confirm('repo',**self.args(opened))
    def plan(self,text='two\n'):
        (self.repo/'one.txt').write_text(text)
        return self.git.prepare({'project':'repo','action':'commit','paths':['one.txt'],'message':'Fixture\n\n### Contract version\nv1\n\n### Why\nFixture\n\n### Changes\n- Fixture\n\n### Validation\n- Tests\n\n### References\n- Issue: https://github.com/jhw7500/personal-ops/issues/1\n'})
    def execute(self,plan): return self.git.execute({'project':'repo','operation_id':plan['operation_id']})
    def assert_denied(self,call):
        with self.assertRaises((ValueError,HostRuntimeError)): call()
    def test_missing_grant_never_replaces_owner_approval(self):
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda:self.execute(self.plan()))
    def test_open_never_grants_or_commits_and_hides_capability(self):
        before=self.head();card=self.opened();public=card['structuredContent']
        self.assertEqual(public['state'],'pending')
        self.assertFalse(public['executes_on_approval'])
        self.assertNotIn(card['_meta']['git_commit_access_confirmation'],json.dumps(public))
        self.assertFalse(self.access.status('repo')['active']);self.assertEqual(before,self.head())
    def test_owner_confirmation_records_scope_without_git_effect(self):
        before=self.head();self.approve();s=self.access.status('repo')
        self.assertTrue(s['active']);self.assertEqual(s['project'],'repo')
        self.assertEqual(s['branch'],'feature/test');self.assertEqual(s['operations'],['commit'])
        self.assertEqual(before,self.head());self.assertEqual(s['expires_at_epoch']-self.wall,86400)
    def test_two_commits_reuse_grant_without_one_use_receipts(self):
        self.approve();expiry=self.access.status('repo')['expires_at_epoch']
        for text in ['two\n','three\n']:
            plan=self.plan(text);self.assertFalse(plan['approval_required'])
            result=self.execute(plan);self.assertEqual(result['status'],'completed')
            self.assertFalse(self.git._receipt_path('repo',plan['operation_id']).exists())
            self.assertEqual(self.access.status('repo')['expected_head'],self.head())
        self.assertEqual(self.access.status('repo')['expires_at_epoch'],expiry)
        self.assertEqual(run_git(self.repo,'show','HEAD:untouched.txt').stdout,'preserve\n')
        self.assertEqual(run_git(self.repo,'diff','--cached','--name-only').stdout,'')
    def test_commit_grant_does_not_authorize_push(self):
        self.approve();remote=self.root/'remote.git';run_git(self.root,'init','--bare',str(remote))
        run_git(self.repo,'remote','add','origin',str(remote));run_git(self.repo,'push','origin','feature/test')
        plan=self.git.prepare({'project':'repo','action':'push','branch':'feature/test','new_branch':False})
        self.assert_denied(lambda:self.execute(plan));self.assertTrue(self.access.status('repo')['active'])
    def test_wall_expiry_requires_fresh_owner_approval(self):
        self.approve(30);plan=self.plan();self.wall+=30
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.execute(plan))
    def test_monotonic_expiry_alone_blocks(self):
        self.approve(30);plan=self.plan();self.mono+=30000000000
        self.assert_denied(lambda:self.execute(plan))
    def test_clock_regression_blocks(self):
        self.approve();plan=self.plan();self.wall-=1;self.assert_denied(lambda:self.execute(plan))
    def test_revoke_blocks_next_commit(self):
        self.approve();card=self.opened();self.assertEqual(card['structuredContent']['state'],'active')
        self.access.revoke('repo',**self.args(card))
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.execute(self.plan()))
    def test_cancel_card_never_grants(self):
        card=self.opened();self.access.cancel('repo',**self.args(card))
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
    def test_callback_one_use_and_forged_token(self):
        card=self.opened();bad=self.args(card);bad['confirmation_token']='b'*64
        self.assert_denied(lambda:self.access.confirm('repo',**bad))
        self.access.confirm('repo',**self.args(card))
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
    def test_reopening_rotates_pending_capability(self):
        first=self.opened();second=self.opened()
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(first)))
        self.access.confirm('repo',**self.args(second))
    def test_changed_head_before_confirmation_is_rejected(self):
        card=self.opened();run_git(self.repo,'commit','--allow-empty','-m','external')
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
    def test_external_head_change_invalidates_active_grant(self):
        self.approve();run_git(self.repo,'commit','--allow-empty','-m','external')
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.execute(self.plan()))
    def test_branch_change_is_not_covered(self):
        self.approve();run_git(self.repo,'checkout','-b','feature/other')
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.execute(self.plan()))
    def test_main_and_master_cannot_receive_standing_grant(self):
        for branch in ['main','master']:
            run_git(self.repo,'checkout','-B',branch)
            self.assert_denied(self.opened)
    def test_runtime_or_boot_change_blocks(self):
        self.approve();plan=self.plan();self.runtime='b'*64
        self.assert_denied(lambda:self.execute(plan));self.runtime='a'*64;self.boot='22222222-2222-2222-2222-222222222222'
        self.assert_denied(lambda:self.execute(plan))
    def test_explicit_plan_source_drift_is_still_rejected(self):
        self.approve();plan=self.plan();(self.repo/'one.txt').write_text('drift')
        self.assert_denied(lambda:self.execute(plan))
    def test_preexisting_index_is_still_rejected(self):
        self.approve();(self.repo/'one.txt').write_text('two');run_git(self.repo,'add','one.txt')
        self.assert_denied(self.plan)
    def test_unknown_project_and_duration_are_rejected(self):
        self.assert_denied(lambda:self.access.open('other'))
        for duration in [True,0,-1,86401,'86400']:
            self.assert_denied(lambda:self.opened(duration))
    def test_unsafe_stored_grants_are_not_accepted(self):
        self.approve();path=self.access.grant_path('repo');raw=path.read_bytes()
        path.chmod(0o644);self.assertFalse(self.access.status('repo')['active']);path.chmod(0o600)
        path.write_text('{}');self.assertFalse(self.access.status('repo')['active']);path.write_bytes(raw)
        other=self.root/'copy';other.write_bytes(raw);other.chmod(0o600)
        path.unlink();path.symlink_to(other);self.assertFalse(self.access.status('repo')['active'])
        path.unlink();os.link(other,path);self.assertFalse(self.access.status('repo')['active'])
    def test_failed_commit_revokes_uncertain_grant(self):
        self.approve();plan=self.plan()
        with mock.patch.object(self.git,'_execute_commit',side_effect=ValueError('synthetic failure')):
            self.assert_denied(lambda:self.execute(plan))
        self.assertFalse(self.access.status('repo')['active'])
    def test_reopened_active_card_never_renews_expiry(self):
        self.approve(30);expiry=self.access.status('repo')['expires_at_epoch'];self.wall+=5;self.mono+=5000000000
        card=self.opened(86400);self.assertEqual(card['structuredContent']['state'],'active')
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
        self.assertEqual(self.access.status('repo')['expires_at_epoch'],expiry)
    def test_tools_keep_issuance_callbacks_app_only(self):
        specs={s['name']:s for s in tool_specs({'repo':self.repo})}
        for n in ['confirm_git_commit_access','cancel_git_commit_access','revoke_git_commit_access']:
            self.assertEqual(specs[n]['_meta']['ui']['visibility'],['app'])
            self.assertFalse(specs[n]['inputSchema']['additionalProperties'])
        router=GitCommitAccessTools(self.git)
        self.assert_denied(lambda:router.validate_arguments('confirm_git_commit_access',{'project':'repo','card_id':'a'*32,'confirmation_token':'b'*64,'grant':{}}))
    def test_grant_field_tampering_fails_closed(self):
        self.approve();path=self.access.grant_path('repo');original=path.read_bytes()
        for field,value in [('duration_seconds',86401),('operations',['commit','push']),('project','other'),('repo','/tmp/other'),('uid',os.getuid()+1),('expected_head','f'*40),('repo_identity',[1,2]),('git_identity',[1,2]),('expires_monotonic_ns',True),('version',True)]:
            with self.subTest(field=field):
                state=json.loads(original);state['grant'][field]=value;path.write_text(json.dumps(state))
                self.assertFalse(self.access.status('repo')['active'])
        path.write_bytes(original);self.assertTrue(self.access.status('repo')['active'])
    def test_unfinished_admission_blocks_reuse(self):
        self.approve();plan=self.plan();stored=self.git._load_plan('repo',plan['operation_id'])
        with self.git._lock('repo'):self.access.begin_locked(stored)
        self.assertFalse(self.access.status('repo')['active']);self.assert_denied(lambda:self.execute(plan))
    def test_pending_card_expiry_blocks_issuance(self):
        card=self.opened();self.wall+=601;self.mono+=601000000000
        self.assert_denied(lambda:self.access.confirm('repo',**self.args(card)))
        self.assertFalse(self.access.status('repo')['active'])
    def test_duplicate_json_keys_and_foreign_card_scope_are_rejected(self):
        self.approve();path=self.access.grant_path('repo');path.write_text('{"version":1,"version":1}')
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda:self.access.confirm('../other',card_id='a'*32,confirmation_token='b'*64))
    def test_commit_written_but_grant_completion_failure_is_outcome_unknown(self):
        self.approve();plan=self.plan();before=self.head()
        with mock.patch.object(self.access,'finish_locked',side_effect=OSError('synthetic state failure')):
            with self.assertRaises(OSError): self.execute(plan)
        after=self.head();self.assertNotEqual(before,after)
        recorded=self.git.get({'project':'repo','operation_id':plan['operation_id']})
        self.assertEqual(recorded['status'],'outcome_unknown')
        self.assertEqual(recorded['result']['commit'],after)
        self.assertTrue(recorded['result']['requires_manual_verification'])
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda:self.execute(plan));self.assertEqual(after,self.head())
    def test_admission_without_owner_grant_never_creates_real_receipt(self):
        plan=self.plan()
        self.assertFalse(self.git._receipt_path('repo',plan['operation_id']).exists())
        self.assert_denied(lambda:self.execute(plan))
        self.assertFalse(self.git._receipt_path('repo',plan['operation_id']).exists())
        self.assertEqual(self.git.get({'project':'repo','operation_id':plan['operation_id']})['status'],'pending_host_approval')

    def test_branch_drift_keeps_original_grant_revocable(self):
        original = self.approve()['structuredContent']
        run_git(self.repo, 'checkout', '-b', 'feature/other')
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'active')
        self.assertEqual(card['structuredContent']['grant_id'], original['grant_id'])
        self.assertEqual(card['structuredContent']['branch'], 'feature/test')
        self.assertFalse(card['structuredContent']['usable_for_current_context'])
        self.assert_denied(lambda: self.access.confirm('repo', **self.args(card)))
        self.access.revoke('repo', **self.args(card))
        run_git(self.repo, 'checkout', 'feature/test')
        self.assertFalse(self.access.status('repo')['active'])

    def test_main_checkout_keeps_original_grant_revocable(self):
        self.approve()
        run_git(self.repo, 'checkout', '-b', 'main')
        card = self.opened()
        self.assertEqual(card['structuredContent']['branch'], 'feature/test')
        self.access.revoke('repo', **self.args(card))
        run_git(self.repo, 'checkout', 'feature/test')
        self.assertFalse(self.access.status('repo')['active'])

    def test_detached_head_keeps_original_grant_revocable(self):
        self.approve()
        run_git(self.repo, 'checkout', '--detach', 'HEAD')
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'active')
        self.access.revoke('repo', **self.args(card))
        run_git(self.repo, 'checkout', 'feature/test')
        self.assertFalse(self.access.status('repo')['active'])

    def test_head_drift_keeps_original_grant_revocable(self):
        self.approve()
        before = self.head()
        run_git(self.repo, 'commit', '--allow-empty', '-m', 'external fixture')
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'active')
        self.assertEqual(card['structuredContent']['head'], before)
        self.assertFalse(card['structuredContent']['usable_for_current_context'])
        self.access.revoke('repo', **self.args(card))
        self.assertFalse(self.access.status('repo')['active'])

    def test_revoke_card_remains_usable_after_detaching(self):
        self.approve()
        card = self.opened()
        run_git(self.repo, 'checkout', '--detach', 'HEAD')
        self.access.revoke('repo', **self.args(card))
        self.assertEqual(self.access._loaded('repo')['grant']['state'], 'revoked')

    def test_expired_grant_allows_fresh_explicit_approval_card(self):
        self.approve(30)
        self.wall += 30
        self.mono += 30_000_000_000
        card = self.opened()
        self.assertEqual(card['structuredContent']['state'], 'pending')
        self.assertFalse(self.access.status('repo')['active'])
        self.access.confirm('repo', **self.args(card))
        self.assertTrue(self.access.status('repo')['active'])

    def test_completion_record_replaced_then_error_revokes_same_grant(self):
        self.approve()
        plan = self.plan()
        save = self.access._save
        def replaced_then_failed(path, value):
            save(path, value)
            if path == self.access.grant_path('repo') and value['events'][-1]['event'] == 'committed':
                raise OSError('fixture directory sync failed after replace')
        with mock.patch.object(self.access, '_save', side_effect=replaced_then_failed):
            with self.assertRaises(OSError):
                self.execute(plan)
        self.assertEqual(self.access._loaded('repo')['grant']['state'], 'revoked')
        self.assertFalse(self.access.status('repo')['active'])
        recorded = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(recorded['status'], 'outcome_unknown')
        self.assertEqual(recorded['result']['commit'], self.head())
        next_plan = self.plan('after uncertain completion')
        self.assert_denied(lambda: self.execute(next_plan))

    def test_completed_plan_write_error_revokes_same_grant(self):
        self.approve()
        plan = self.plan()
        save = self.git._save_plan
        def completed_then_failed(value):
            save(value)
            if value['status'] == 'completed':
                raise OSError('fixture completed plan durability failed')
        with mock.patch.object(self.git, '_save_plan', side_effect=completed_then_failed):
            with self.assertRaises(OSError):
                self.execute(plan)
        self.assertEqual(self.access._loaded('repo')['grant']['state'], 'revoked')
        self.assertFalse(self.access.status('repo')['active'])
        self.assertEqual(self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})['status'], 'outcome_unknown')

    def test_unsettled_completed_grant_never_authorizes_next_commit(self):
        self.approve()
        plan = self.plan()
        stored = self.git._load_plan('repo', plan['operation_id'])
        with self.git._lock('repo'):
            stored['authorization'] = self.access.begin_locked(stored)
            stored['status'] = 'executing'
            self.git._save_plan(stored)
            result = self.git._execute_commit(self.repo, stored['immutable'])
            self.access.finish_locked(stored, result)
        self.assertIsNone(self.access._loaded('repo')['in_flight'])
        status = self.access.status('repo')
        self.assertFalse(status['active'])
        self.assertEqual(status['reason'], 'COMPLETION_UNCERTAIN')
        self.assert_denied(lambda: self.execute(self.plan('next fixture')))

    def test_failure_cleanup_does_not_revoke_a_different_grant(self):
        self.approve()
        plan = self.plan()
        self.execute(plan)
        stored = self.git._load_plan('repo', plan['operation_id'])
        card = self.opened()
        self.access.revoke('repo', **self.args(card))
        self.approve()
        different = self.access.status('repo')['grant_id']
        with self.git._lock('repo'):
            self.access.fail_locked(stored)
        self.assertTrue(self.access.status('repo')['active'])
        self.assertEqual(self.access.status('repo')['grant_id'], different)


    def approve_one_use_fixture(self, plan):
        stdin = TTYBuffer('APPROVE ' + plan['operation_id'] + chr(10))
        with mock.patch('sys.stdin', stdin), mock.patch('sys.stdout', TTYBuffer()):
            approve_operation(self.git.state_dir, 'repo', plan['operation_id'])

    def test_one_use_commit_completion_error_reports_actual_commit_uncertain(self):
        plan = self.plan()
        self.approve_one_use_fixture(plan)
        save = self.git._save_plan
        def completed_then_failed(value):
            save(value)
            if value['status'] == 'completed':
                raise OSError('fixture one-use commit completion write failed')
        with mock.patch.object(self.git, '_save_plan', side_effect=completed_then_failed):
            with self.assertRaises(OSError):
                self.execute(plan)
        result = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(result['result']['commit'], self.head())
        self.assertTrue(result['result']['requires_manual_verification'])
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda: self.execute(plan))

    def test_one_use_push_completion_error_reports_actual_push_uncertain(self):
        remote = self.root / 'remote.git'
        run_git(self.root, 'init', '--bare', str(remote))
        run_git(self.repo, 'remote', 'add', 'origin', str(remote))
        run_git(self.repo, 'push', 'origin', 'HEAD:refs/heads/main')
        plan = self.git.prepare({'project': 'repo', 'action': 'push',
                                 'branch': 'feature/test', 'new_branch': True})
        self.approve_one_use_fixture(plan)
        save = self.git._save_plan
        def completed_then_failed(value):
            save(value)
            if value['status'] == 'completed':
                raise OSError('fixture one-use push completion write failed')
        with mock.patch.object(self.git, '_save_plan', side_effect=completed_then_failed):
            with self.assertRaises(OSError):
                self.execute(plan)
        remote_head = run_git(self.repo, 'ls-remote', '--heads', 'origin',
                              'refs/heads/feature/test').stdout.split()[0]
        self.assertEqual(remote_head, self.head())
        result = self.git.get({'project': 'repo', 'operation_id': plan['operation_id']})
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(result['result']['branch'], 'feature/test')
        self.assertTrue(result['result']['requires_manual_verification'])
        self.assertFalse(self.access.status('repo')['active'])
        self.assert_denied(lambda: self.execute(plan))

if __name__=='__main__':unittest.main()
