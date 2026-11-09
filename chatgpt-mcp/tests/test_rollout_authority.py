"""Exact rollout-proof admission using only real disposable authority state."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from rollout_layout import known_layout
import runtime_rollout_web as web
from tests import test_runtime_rollout_web as web_tests
from workspace_authority import WorkspaceAuthority
from workspace_control import verify_launch, verify_release_manifest, transition_release


class RolloutAuthorityTest(unittest.TestCase):
    prepare = web_tests.RuntimeRolloutWebTest.prepare
    approve = web_tests.RuntimeRolloutWebTest.approve
    assert_error = web_tests.RuntimeRolloutWebTest.assert_error

    def setUp(self):
        web_tests.RuntimeRolloutWebTest.setUp(self)
        self.home = self.base / 'owner-home'
        self.home.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_STATE_HOME': str(self.home / '.local/state'),
            'XDG_CACHE_HOME': str(self.home / '.cache'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.layout = known_layout('wsl-pc1', self.home)
        self.layout['unit_path'].parent.mkdir(parents=True, mode=0o700)
        for parent in (self.home / '.config', self.home / '.config/systemd'):
            parent.chmod(0o700)
        self.layout['unit_path'].write_text('[Service]\nType=simple\n')
        self.layout['unit_path'].chmod(0o600)
        config = self.home / '.config/chatgpt-coding-mcp/projects.json'
        config.parent.mkdir(mode=0o700)
        config.write_text(json.dumps({'projects': {web.PROJECT: str(self.root)}}))
        config.chmod(0o600)
        state = self.home / '.local/state/chatgpt-coding-mcp'
        self.ops = web.RuntimeRolloutWeb(
            {web.PROJECT: self.root}, state_dir=state,
            launcher=lambda argv: self.started.append(argv) or True,
            runtime_reader=lambda: self.runtime_identity,
            boot_reader=lambda: self.boot_id,
            uid_reader=lambda: self.uid,
            layout_reader=lambda: self.layout, test_only=True)
        self.releases = self.home / '.local/lib/chatgpt-coding-mcp/releases'
        current_module = self.load_rollout(self.root)
        installer = current_module._import_installer()
        server = self.root / 'chatgpt-mcp/server.py'
        original = server.read_bytes()
        try:
            server.write_bytes(original + b'\n# prior admitted fixture release\n')
            current = installer.install(self.root / 'chatgpt-mcp', self.releases,
                                        {web.PROJECT: self.root})
        finally:
            server.write_bytes(original)
        self.current_runtime = Path(current['runtime_dir'])
        self.current_digest = verify_release_manifest(self.current_runtime)['release_digest']
        self.authority = WorkspaceAuthority(
            {web.PROJECT: self.root}, state_dir=self.ops.activation_authority_dir)

    def load_rollout(self, root):
        spec = importlib.util.spec_from_file_location(
            'admission_fixture_rollout', root / 'chatgpt-mcp/rollout-host-runtime.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def bridge(self):
        spec = importlib.util.find_spec('rollout_authority')
        self.assertIsNotNone(spec, 'exact consumed-rollout release admission is required')
        return importlib.import_module('rollout_authority')

    def claim_target(self, *, abi=None, generation=None, initialize_authority=True):
        control = self.root / 'chatgpt-mcp/workspace_control.py'
        source = control.read_text()
        if abi is not None:
            self.assertIn('ENFORCEMENT_ABI = 3', source)
            source = source.replace('ENFORCEMENT_ABI = 3', 'ENFORCEMENT_ABI = ' + str(abi), 1)
        if generation is not None:
            self.assertIn('HOST_ACCESS_CAPABILITY_GENERATION = 1', source)
            source = source.replace('HOST_ACCESS_CAPABILITY_GENERATION = 1',
                                    'HOST_ACCESS_CAPABILITY_GENERATION = ' + str(generation), 1)
        control.write_text(source)
        public = self.prepare('admission-fixture')
        self.approve(public)
        self.ops.execute(web.PROJECT, public['operation_id'])
        self.frozen, self.grant = self.ops.claim_for_worker(public['operation_id'])
        self.module = self.load_rollout(self.frozen)
        self.installer = self.module._import_installer()
        installed = self.installer.install(self.frozen / 'chatgpt-mcp', self.releases,
                                            {web.PROJECT: self.root})
        self.target_runtime = Path(installed['runtime_dir'])
        self.target_digest = verify_release_manifest(self.target_runtime)['release_digest']
        self.assertNotEqual(self.current_digest, self.target_digest)
        if not initialize_authority:
            return
        self.binding = self.authority.initialize('a' * 64, {self.current_digest: 3}, minimum_abi=3)
        self.external = self.base / 'owner-result.json'
        self.external.write_text('{}')
        request = self.authority.request(web.PROJECT, str(self.external), ['read'], 1800,
                                         'fixture owner result', 'a' * 64)
        self.workspace_grant = self.authority.confirm(
            web.PROJECT, request['request_id'], request['_confirmation'], 'a' * 64)
        self.pending = self.authority.request(
            web.PROJECT, str(self.external), ['write'], 1800, 'pending fixture request', 'a' * 64)

    def preflight(self):
        return self.bridge().preflight_rollout_authority(
            self.layout, self.current_runtime.name, source_root=self.frozen)

    def admit(self, *, grant=None, preflight=None, root=None):
        return self.bridge().admit_rollout_release(
            self.grant if grant is None else grant,
            self.frozen if root is None else root,
            self.installer, self.module.inventory, self.module.control_inventory,
            preflight=self.preflight() if preflight is None else preflight)

    def state_bytes(self):
        return (self.authority.state_dir / 'authority.json').read_bytes()

    def test_admission_adds_only_reviewed_release_preserving_existing_authority(self):
        self.claim_target()
        before = json.loads(self.state_bytes())
        result = self.admit()
        after = json.loads(self.state_bytes())
        self.assertEqual(result['status'], 'admitted')
        self.assertEqual(result['release_digest'], self.target_digest)
        self.assertEqual(result['release_abi'], 3)
        self.assertTrue(result['authority_active'])
        expected = copy.deepcopy(before)
        expected['allowed_releases'][self.target_digest] = 3
        self.assertEqual(after, expected)
        self.assertEqual(self.authority.require(web.PROJECT, str(self.external), 'read',
            self.workspace_grant['grant_id'], 'a' * 64)['grant_id'], self.workspace_grant['grant_id'])
        self.assertEqual(self.authority.get(web.PROJECT, self.pending['request_id'])['status'], 'pending')
        observed = verify_launch(self.target_runtime, self.authority.state_dir, enforcement_required=True)
        self.assertEqual(observed['binding'], self.binding)
        transitioned = transition_release(self.target_runtime, self.authority.state_dir)
        self.assertEqual(transitioned['binding']['authority_epoch'], self.binding['authority_epoch'] + 1)

    def test_idempotent_admission_does_not_rewrite_state_or_add_epoch(self):
        self.claim_target()
        pinned = self.preflight()
        self.admit(preflight=pinned)
        before = self.state_bytes()
        result = self.admit(preflight=pinned)
        self.assertEqual(result['status'], 'already_admitted')
        self.assertEqual(self.state_bytes(), before)

    def test_preflight_does_not_admit_or_change_authority(self):
        self.claim_target()
        before = self.state_bytes()
        pinned = self.preflight()
        self.assertEqual(pinned['binding'], self.binding)
        self.assertEqual(pinned['target_release_digest'], self.target_digest)
        self.assertEqual(pinned['target_abi'], 3)
        self.assertEqual(pinned['target_capability_generation'], 1)
        self.assertEqual(self.state_bytes(), before)
        self.assertNotIn(self.target_digest, json.loads(before)['allowed_releases'])

    def test_fake_grant_and_grant_subclass_cannot_issue_admission(self):
        self.claim_target()
        class DerivedGrant(web.WebRolloutGrant):
            pass
        fake = SimpleNamespace(**self.grant.__dict__)
        derived = DerivedGrant(self.grant.operation_id, self.grant.source_digest,
            self.grant.target_snapshot, self.grant.control_digest, self.grant.state_root)
        before = self.state_bytes()
        for grant in (fake, derived, None):
            with self.subTest(kind=type(grant).__name__):
                with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
                    self.bridge().admit_rollout_release(grant, self.frozen, self.installer,
                        self.module.inventory, self.module.control_inventory, preflight=self.preflight())
                self.assertEqual(self.state_bytes(), before)

    def test_unconsumed_or_other_worker_grant_cannot_issue_admission(self):
        self.claim_target()
        pinned = self.preflight()
        plan = self.ops._load(self.grant.operation_id)
        before = self.state_bytes()
        for state, worker in (('queued', os.getpid()), ('running', os.getpid() + 111)):
            with self.subTest(state=state, worker=worker):
                plan.update(state=state, worker_pid=worker)
                self.ops._save(plan)
                with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
                    self.admit(preflight=pinned)
                self.assertEqual(self.state_bytes(), before)

    def test_unfrozen_root_cannot_issue_admission(self):
        self.claim_target()
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
            self.admit(root=self.root)
        self.assertEqual(self.state_bytes(), before)

    def test_frozen_source_drift_prevents_authority_write(self):
        self.claim_target()
        pinned = self.preflight()
        path = self.frozen / 'chatgpt-mcp/rollout_target.py'
        path.write_bytes(path.read_bytes() + b'\n# altered reviewed source\n')
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_SOURCE_CHANGED'):
            self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_installed_manifest_or_file_drift_prevents_authority_write(self):
        self.claim_target()
        pinned = self.preflight()
        path = self.target_runtime / 'server.py'
        path.write_bytes(path.read_bytes() + b'\n# altered installed target\n')
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_RELEASE_TAMPERED'):
            self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_full_manifest_digest_must_match_even_when_short_snapshot_matches(self):
        self.claim_target()
        bridge = self.bridge()
        pinned = self.preflight()
        verified = verify_release_manifest(self.target_runtime)
        other = self.target_digest[:20] + ('0' * 44 if self.target_digest[20:] != '0' * 44 else '1' * 44)
        before = self.state_bytes()
        with mock.patch.object(bridge, 'verify_release_manifest',
                               return_value={**verified, 'release_digest': other}):
            with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_RELEASE_MANIFEST_MISMATCH'):
                self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_policy_epoch_and_installation_drift_prevent_admission(self):
        self.claim_target()
        pinned = self.preflight()
        before = self.state_bytes()
        for name, value in (('policy_digest', 'b' * 64),
                            ('authority_epoch', self.binding['authority_epoch'] + 1),
                            ('installation_id', 'f' * 32)):
            with self.subTest(field=name):
                stale = copy.deepcopy(pinned)
                stale['binding'][name] = value
                with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_AUTHORITY_CHANGED'):
                    self.admit(preflight=stale)
                self.assertEqual(self.state_bytes(), before)

    def test_conflicting_existing_release_abi_is_not_overwritten(self):
        self.claim_target()
        self.authority.advance('a' * 64, {self.current_digest: 3, self.target_digest: 4})
        pinned = self.preflight()
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_RELEASE_ADMISSION_CONFLICT'):
            self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_abi_downgrade_is_refused_during_readonly_preflight(self):
        self.claim_target(abi=2)
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_RELEASE_INCOMPATIBLE'):
            self.preflight()
        self.assertEqual(self.state_bytes(), before)

    def test_capability_generation_change_requires_separate_migration(self):
        self.claim_target(generation=2)
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_RELEASE_INCOMPATIBLE'):
            self.preflight()
        self.assertEqual(self.state_bytes(), before)

    def test_floor_change_after_preflight_is_not_silently_accepted(self):
        self.claim_target()
        pinned = self.preflight()
        pinned['minimum_abi'] = 2
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_AUTHORITY_CHANGED'):
            self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_grants_for_other_configured_projects_are_preserved(self):
        other = self.base / 'other-project'
        other.mkdir()
        projects = {web.PROJECT: str(self.root), 'pim-check': str(other)}
        config = self.home / '.config/chatgpt-coding-mcp/projects.json'
        config.write_text(json.dumps({'projects': projects}))
        self.authority = WorkspaceAuthority(projects, state_dir=self.ops.activation_authority_dir)
        self.claim_target()
        request = self.authority.request('pim-check', str(self.external), ['read'], 1800,
                                         'other project fixture', 'a' * 64)
        grant = self.authority.confirm('pim-check', request['request_id'],
                                        request['_confirmation'], 'a' * 64)
        before = json.loads(self.state_bytes())
        self.admit()
        after = json.loads(self.state_bytes())
        self.assertEqual(after['grants'], before['grants'])
        self.assertEqual(after['requests'], before['requests'])
        self.assertEqual(self.authority.require('pim-check', str(self.external), 'read',
            grant['grant_id'], 'a' * 64)['grant_id'], grant['grant_id'])

    def test_project_configuration_drift_rejects_admission(self):
        self.claim_target()
        pinned = self.preflight()
        config = self.home / '.config/chatgpt-coding-mcp/projects.json'
        config.write_text(config.read_text() + '\n')
        before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_AUTHORITY_CHANGED'):
            self.admit(preflight=pinned)
        self.assertEqual(self.state_bytes(), before)

    def test_binding_is_rechecked_after_acquiring_exclusive_authority_lock(self):
        self.claim_target()
        pinned = self.preflight()
        original = WorkspaceAuthority._locked_state
        fixture = self
        @contextmanager
        def raced(instance, *, shared=False):
            with mock.patch.object(WorkspaceAuthority, '_locked_state', original):
                fixture.authority.advance('a' * 64)
            with original(instance, shared=shared) as value:
                yield value
        with mock.patch.object(WorkspaceAuthority, '_locked_state', raced):
            with self.assertRaisesRegex(RuntimeError, 'ROLLOUT_AUTHORITY_CHANGED'):
                self.admit(preflight=pinned)
        state = json.loads(self.state_bytes())
        self.assertEqual(state['authority_epoch'], self.binding['authority_epoch'] + 1)
        self.assertNotIn(self.target_digest, state['allowed_releases'])

    def test_legacy_without_active_authority_does_not_initialize_it(self):
        self.layout['unit_path'].unlink()
        self.layout = known_layout('legacy', self.home)
        self.layout['unit_path'].write_text('[Service]\nType=oneshot\n')
        self.layout['unit_path'].chmod(0o600)
        (self.root / 'chatgpt-mcp/rollout-target.json').write_text(
            '{"version":1,"layout":"legacy"}\n')
        self.claim_target(initialize_authority=False)
        self.assertFalse(self.authority.state_dir.exists())
        result = self.admit()
        self.assertEqual(result['status'], 'not_required')
        self.assertFalse(result['authority_active'])
        self.assertFalse(self.authority.state_dir.exists())
        self.assertFalse(self.authority.enforcement_path.exists())


if __name__ == '__main__':
    unittest.main()
