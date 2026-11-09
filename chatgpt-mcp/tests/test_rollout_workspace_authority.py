from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from runtime_rollout_web import WebRolloutGrant
import rollout_authority as authority_module


class RolloutWorkspaceAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.binding = {
            'installation_id': '1' * 32,
            'authority_epoch': 7,
            'policy_digest': 'c' * 64,
        }
        self.old_digest = 'a' * 64
        self.target_digest = 'b' * 64
        self.layout = {'name': 'wsl-pc1'}
        self.authority_dir = Path('/state/workspace-authority')
        self.config = Path('/config/projects.json')
        self.releases = Path('/releases')
        self.grant = WebRolloutGrant(
            '1' * 32, '2' * 64, '3' * 20, '4' * 64,
            self.authority_dir.parent / 'runtime-rollout-web')
        self.pinned = {
            'version': 1, 'layout': 'wsl-pc1', 'live_snapshot': '5' * 20,
            'authority_active': True, 'projects_config_sha256': '6' * 64,
            'target_release_digest': self.target_digest, 'target_abi': 2,
            'target_capability_generation': None, 'binding': dict(self.binding),
            'current_release_digest': self.old_digest, 'release_abi': 2,
            'minimum_abi': 2, 'capability_generation': None,
        }

    def test_admits_exact_target_without_dropping_rollback_or_advancing_binding(self):
        authority = MagicMock()
        state = {'allowed_releases': {self.old_digest: 2}}
        authority._binding.return_value = dict(self.binding)
        authority._locked_state.return_value.__enter__.return_value = (object(), state)
        observed = {'binding': dict(self.binding),
                    'release_digest': self.target_digest}
        with patch.object(authority_module, 'verify_web_rollout_grant',
                          return_value=self.layout), \
             patch.object(authority_module, '_paths', return_value=(
                 self.releases, self.authority_dir, self.config)), \
             patch.object(authority_module, '_verify_target',
                          return_value=(self.target_digest, 2)), \
             patch.object(authority_module, 'activation_admission',
                          return_value=nullcontext()), \
             patch.object(authority_module, 'preflight_rollout_authority',
                          return_value=dict(self.pinned)), \
             patch.object(authority_module, '_projects',
                          return_value=({'personal-ops': Path('/project')}, '6' * 64)), \
             patch.object(authority_module, '_minimum_enforcement_abi', return_value=2), \
             patch.object(authority_module, 'read_owner_file',
                          return_value=SimpleNamespace(sha256='6' * 64)), \
             patch.object(authority_module, 'verify_launch', return_value=observed), \
             patch.object(authority_module, 'WorkspaceAuthority',
                          return_value=authority):
            result = authority_module.admit_rollout_release(
                self.grant, Path('/approved/frozen/source'), object(),
                lambda _installer: {}, lambda: {}, preflight=self.pinned)

        self.assertEqual(state['allowed_releases'], {
            self.old_digest: 2, self.target_digest: 2})
        authority._save.assert_called_once()
        self.assertEqual(authority._binding.call_count, 1)
        self.assertEqual(result['status'], 'admitted')
        self.assertEqual(result['release_digest'], self.target_digest)

    def test_rejects_target_below_current_minimum_abi_during_preflight(self):
        current = {
            'authority_active': True, 'release_abi': 2, 'minimum_abi': 2,
            'host_access_capability_generation': None,
            'binding': dict(self.binding), 'release_digest': self.old_digest,
        }
        with patch.object(authority_module, '_paths', return_value=(
                 self.releases, self.authority_dir, self.config)), \
             patch.object(authority_module, '_candidate',
                          return_value=({}, self.target_digest, 1, None)), \
             patch.object(authority_module, '_projects',
                          return_value=({'personal-ops': Path('/project')}, '6' * 64)), \
             patch.object(authority_module, 'verify_launch', return_value=current), \
             self.assertRaisesRegex(RuntimeError, 'ROLLOUT_RELEASE_INCOMPATIBLE'):
            authority_module.preflight_rollout_authority(
                self.layout, '5' * 20, source_root=Path('/source'))

    def test_rejects_binding_change_before_authority_state_mutation(self):
        changed = dict(self.pinned)
        changed['binding'] = dict(self.binding, authority_epoch=8)
        factory = MagicMock()
        with patch.object(authority_module, 'verify_web_rollout_grant',
                          return_value=self.layout), \
             patch.object(authority_module, '_paths', return_value=(
                 self.releases, self.authority_dir, self.config)), \
             patch.object(authority_module, '_verify_target',
                          return_value=(self.target_digest, 2)), \
             patch.object(authority_module, 'activation_admission',
                          return_value=nullcontext()), \
             patch.object(authority_module, 'preflight_rollout_authority',
                          return_value=changed), \
             patch.object(authority_module, 'WorkspaceAuthority', factory), \
             self.assertRaisesRegex(RuntimeError, 'ROLLOUT_AUTHORITY_CHANGED'):
            authority_module.admit_rollout_release(
                self.grant, Path('/approved/frozen/source'), object(),
                lambda _installer: {}, lambda: {}, preflight=self.pinned)
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
