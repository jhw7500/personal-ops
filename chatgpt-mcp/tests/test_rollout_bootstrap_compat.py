from pathlib import Path
import unittest
from unittest.mock import patch

from host_runtime_config import HostRuntimeError
import rollout_target as target


class BootstrapCompatTest(unittest.TestCase):
    def setUp(self):
        self.root = Path('/approved/frozen/source')
        self.current = 'c' * 64
        self.bootstrap = 'b' * 64

    def test_accepts_only_named_v1_toss_bootstrap_digest(self):
        fixed = {'control_digest': self.bootstrap}
        with patch.object(target, 'expected_rollout_snapshots',
                          return_value=('a' * 20, self.current)), \
             patch.object(target, '_bootstrap_v1_control_digest',
                          return_value=self.bootstrap):
            self.assertEqual(target.approved_control_digest(self.root, fixed),
                             self.bootstrap)

    def test_rejects_other_historical_control_digest(self):
        fixed = {'control_digest': '0' * 64}
        with patch.object(target, 'expected_rollout_snapshots',
                          return_value=('a' * 20, self.current)), \
             patch.object(target, '_bootstrap_v1_control_digest',
                          return_value=self.bootstrap), \
             self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_SOURCE_CHANGED'):
            target.approved_control_digest(self.root, fixed)

    def test_never_relabels_invalid_contract_as_source_compatibility(self):
        fixed = {'control_contract_version': 99,
                 'control_digest': self.bootstrap}
        with self.assertRaisesRegex(HostRuntimeError,
                                    'ROLLOUT_CONTROL_CONTRACT_INVALID'):
            target.approved_control_digest(self.root, fixed)


if __name__ == '__main__':
    unittest.main()
