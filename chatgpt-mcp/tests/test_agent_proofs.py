"""Activation proof must bind current policy, runtime, platform and real assertions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_proofs import record_receipt, verify_receipt
from agent_probe import REQUIRED_ASSERTIONS


class AgentProofTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = {'provider': 'codex', 'model': 'fixture', 'enabled': True}
        self.binding = mock.patch('agent_proofs.proof_binding', return_value={'fixture': 'bound'})
        self.binding.start()
        self.addCleanup(self.binding.stop)
        self.evidence = {
            'assertions': {key: True for key in REQUIRED_ASSERTIONS},
            'supervision': {'termination_verified': True, 'cgroup_empty': True,
                            'descendant_gone': True, 'unrelated_alive': True,
                            'bootstrap_environment_safe': True},
        }

    def test_valid_receipt_and_drift_or_content_tampering(self):
        receipt = record_receipt(self.root, self.profile, 'isolation', self.evidence)
        self.assertTrue(verify_receipt(self.root, receipt, self.profile, 'isolation'))
        with mock.patch('agent_proofs.proof_binding', return_value={'fixture': 'changed'}):
            self.assertFalse(verify_receipt(self.root, receipt, self.profile, 'isolation'))
        path = self.root / (receipt + '.json')
        document = json.loads(path.read_bytes())
        document['evidence']['supervision']['cgroup_empty'] = False
        path.write_text(json.dumps(document))
        self.assertFalse(verify_receipt(self.root, receipt, self.profile, 'isolation'))

    def test_failed_probe_old_boolean_receipt_and_public_file_cannot_activate(self):
        self.evidence['assertions']['symlink_read_denied'] = False
        with self.assertRaisesRegex(ValueError, 'PROOF_ASSERTIONS_INCOMPLETE'):
            record_receipt(self.root, self.profile, 'isolation', self.evidence)
        path = self.root / ('a' * 64 + '.json')
        path.write_text(json.dumps({'passed': True, 'kind': 'isolation'}))
        path.chmod(0o600)
        self.assertFalse(verify_receipt(self.root, 'a' * 64, self.profile, 'isolation'))
        self.evidence['assertions']['symlink_read_denied'] = True
        receipt = record_receipt(self.root, self.profile, 'isolation', self.evidence)
        (self.root / (receipt + '.json')).chmod(0o644)
        self.assertFalse(verify_receipt(self.root, receipt, self.profile, 'isolation'))

    def test_smoke_requires_observed_execution_and_verified_stop(self):
        with self.assertRaisesRegex(ValueError, 'PROOF_ASSERTIONS_INCOMPLETE'):
            record_receipt(self.root, self.profile, 'smoke', {'verified': True})


if __name__ == '__main__':
    unittest.main()
