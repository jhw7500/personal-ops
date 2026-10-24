"""Paid fixture has a separate exact, expiring, one-use host approval."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_profiles import profile_digest
from agent_runner import CodexAgentRunner
from agent_smoke import SMOKE_OBJECTIVE, consume_smoke_authority
from host_runtime_config import HostRuntimeError


class SmokeAuthorityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.profile = {'project': 'fixture', 'enabled': True, 'provider': 'codex',
                        'apply_policy': 'review_only', 'host_capabilities': [],
                        'smoke_receipt': None}
        self.plan_digest = 'f' * 64
        now = datetime.now(timezone.utc)
        self.document = {'version': 1, 'kind': 'profile_smoke', 'plan_digest': self.plan_digest,
                         'profile_digest': profile_digest(self.profile),
                         'workspace': str(self.workspace), 'task_id': 'b' * 32,
                         'objective_digest': hashlib.sha256(SMOKE_OBJECTIVE.encode()).hexdigest(),
                         'timeout_minutes': 1, 'created_at': now.isoformat(),
                         'expires_at': (now + timedelta(minutes=2)).isoformat(),
                         'isolation_receipt': 'c' * 64, 'receipts_dir': str(self.root / 'proofs')}
        self.path = self.root / 'approval.json'
        self.save()
        patcher = mock.patch('agent_smoke.verify_receipt', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self):
        self.path.write_text(json.dumps(self.document))
        self.path.chmod(0o600)

    async def test_runner_consumes_real_fixture_approval_before_owned_run_without_fake_smoke_id(self):
        runner = CodexAgentRunner()
        with mock.patch.object(runner, '_run_owned', new=mock.AsyncMock(return_value={'verified': True})) as run:
            result = await runner.run_approved_smoke(self.profile, self.workspace, self.path,
                                                     plan_digest=self.plan_digest)
        self.assertTrue(result['verified'])
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.profile['smoke_receipt'])
        self.assertEqual(run.call_args.args[0]['objective'], SMOKE_OBJECTIVE)
        with self.assertRaises(HostRuntimeError):
            consume_smoke_authority(self.profile, self.workspace, self.path, plan_digest=self.plan_digest)

    def test_changed_plan_profile_expiry_and_unproven_isolation_never_consume(self):
        for key, value in (('plan_digest', '0'*64), ('profile_digest', '0'*64),
                           ('expires_at', datetime.now(timezone.utc).isoformat())):
            original = self.document[key]
            self.document[key] = value
            self.save()
            with self.assertRaisesRegex(HostRuntimeError, 'SMOKE_APPROVAL_INVALID'):
                consume_smoke_authority(self.profile, self.workspace, self.path,
                                        plan_digest=self.plan_digest)
            self.assertTrue(self.path.exists())
            self.document[key] = original
        self.save()
        with mock.patch('agent_smoke.verify_receipt', return_value=False):
            with self.assertRaisesRegex(HostRuntimeError, 'SMOKE_APPROVAL_INVALID'):
                consume_smoke_authority(self.profile, self.workspace, self.path,
                                        plan_digest=self.plan_digest)
        self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
