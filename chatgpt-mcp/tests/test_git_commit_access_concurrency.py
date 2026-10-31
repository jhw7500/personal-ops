"""Concurrency verification using disposable repositories, never live grants."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock
import tests.test_git_commit_access as fixtures

class CommitAccessConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.GitCommitAccessTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_same_prepared_plan_cannot_commit_twice(self):
        f=self.fx;f.approve();plan=f.plan()
        before=f.head();barrier=threading.Barrier(2)
        def execute():
            barrier.wait(timeout=5)
            try: return ('ok',f.execute(plan))
            except ValueError: return ('denied',None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(execute) for _ in range(2)]
            results=[job.result(timeout=15) for job in jobs]
        self.assertEqual(sorted(status for status,_ in results),['denied','ok'])
        self.assertNotEqual(f.head(),before)
        parent=fixtures.run_git(f.repo,'rev-parse','HEAD^').stdout.strip()
        self.assertEqual(parent,before)
        record=f.access._loaded('repo')
        self.assertEqual(sum(event['event']=='committed' for event in record['events']),1)

    def test_revocation_serializes_with_admitted_commit_and_denies_next(self):
        f=self.fx;f.approve();card=f.opened();args=f.args(card);plan=f.plan()
        entered=threading.Event();release=threading.Event();revoking=threading.Event()
        original=f.git._execute_commit
        def bounded_commit(*a,**kw):
            entered.set()
            if not release.wait(6): raise TimeoutError('fixture release missing')
            return original(*a,**kw)
        def revoke():
            revoking.set();return f.access.revoke('repo',**args)
        with mock.patch.object(f.git,'_execute_commit',side_effect=bounded_commit):
            with ThreadPoolExecutor(max_workers=2) as pool:
                committing=pool.submit(f.execute,plan)
                self.assertTrue(entered.wait(5))
                revocation=pool.submit(revoke)
                try:
                    self.assertTrue(revoking.wait(5))
                    self.assertFalse(revocation.done())
                finally: release.set()
                self.assertEqual(committing.result(timeout=15)['status'],'completed')
                self.assertEqual(revocation.result(timeout=15)['structuredContent']['state'],'revoked')
        self.assertFalse(f.access.status('repo')['active'])
        before=f.head();f.assert_denied(lambda:f.execute(f.plan('third\n')))
        self.assertEqual(f.head(),before)
        self.assertEqual(fixtures.run_git(f.repo,'diff','--cached','--name-only').stdout,'')

    def test_admitted_commit_may_finish_after_expiry_but_next_is_denied(self):
        f=self.fx;f.approve(seconds=1);plan=f.plan();original=f.git._execute_commit
        def expires_during_commit(*a,**kw):
            result=original(*a,**kw);f.wall+=2;f.mono+=2_000_000_000;return result
        with mock.patch.object(f.git,'_execute_commit',side_effect=expires_during_commit):
            result=f.execute(plan)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(f.access.status('repo')['reason'],'EXPIRED')
        before=f.head();f.assert_denied(lambda:f.execute(f.plan('third\n')))
        self.assertEqual(f.head(),before)

if __name__=='__main__':unittest.main()
