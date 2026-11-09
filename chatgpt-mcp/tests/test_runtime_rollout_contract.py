"""Source-bound rollout targets and the explicit first-upgrade control contract.

All approval records, source snapshots and service identities are temporary
fixtures. No real host service or installed approval state is read or changed.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import shutil
import sys
import types
from unittest import mock
import unittest

from rollout_layout import known_layout
import runtime_rollout_web as web
from tests import test_runtime_rollout_web as web_tests


class RolloutContractTest(unittest.TestCase):
    prepare = web_tests.RuntimeRolloutWebTest.prepare
    approve = web_tests.RuntimeRolloutWebTest.approve
    assert_error = web_tests.RuntimeRolloutWebTest.assert_error

    def setUp(self):
        web_tests.RuntimeRolloutWebTest.setUp(self)
        self.layout = known_layout('wsl-pc1', self.base / 'owner')
        self.ops.layout_reader = lambda: self.layout
        self.intent = self.root / 'chatgpt-mcp/rollout-target.json'
        self.intent.write_text('{"version":1,"layout":"wsl-pc1"}\n')

    def target_module(self):
        spec = importlib.util.find_spec('rollout_target')
        self.assertIsNotNone(spec, 'the frozen-source compatibility helper is required')
        return importlib.import_module('rollout_target')

    def legacy_control_digest(self, root):
        names = ('rollout-host-runtime.py', 'rollout_test_runner.py',
                 'rollout_tunnel.py', 'rollout_admin.py', 'rollout_catalog.py',
                 'rollout_diagnostics.py', 'install-runtime.py',
                 'configure-host-runtime.py')
        paths = [root / 'chatgpt-mcp' / name for name in names]
        tests = root / 'chatgpt-mcp/tests'
        paths += sorted(tests.glob('test_*.py'))
        paths += [tests / name for name in ('check_host_access_approval_ui.py',
                  'check_schema014_approval_ui.py', 'check_host_command_approval_ui.py')]
        paths += sorted(tests.glob('test_*.mjs'))
        record = {str(p.relative_to(root / 'chatgpt-mcp')):
                  hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()

    def prepare_legacy(self):
        public = self.prepare('legacy-prepared-contract')
        plan = self.ops._load(public['operation_id'])
        fixed = plan['immutable']
        fixed.pop('control_contract_version', None)
        fixed.pop('target_layout', None)
        fixed['control_digest'] = self.legacy_control_digest(self.root)
        plan['plan_digest'] = web._digest(fixed)
        self.ops._save(plan)
        return self.ops._public(plan)

    def prepare_buildserver_v1_bootstrap(self):
        target = self.target_module()
        public = self.prepare('buildserver-v1-toss-bootstrap')
        plan = self.ops._load(public['operation_id'])
        fixed = plan['immutable']
        fixed.pop('control_contract_version', None)
        fixed.pop('target_layout', None)
        fixed['control_digest'] = target._bootstrap_v1_control_digest(self.root)
        plan['plan_digest'] = web._digest(fixed)
        self.ops._save(plan)
        return self.ops._public(plan)

    def claim(self, plan):
        self.approve(plan)
        self.ops.execute(web.PROJECT, plan['operation_id'])
        frozen, grant = self.ops.claim_for_worker(plan['operation_id'])
        script = frozen / 'chatgpt-mcp/rollout-host-runtime.py'
        spec = importlib.util.spec_from_file_location('contract_fixture_rollout', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return frozen, grant, module

    @staticmethod
    def old_grant_shape(grant):
        return types.SimpleNamespace(**{
            name: getattr(grant, name) for name in (
                'operation_id', 'source_digest', 'target_snapshot',
                'control_digest', 'state_root')
        })

    def test_new_plan_pins_control_version_and_target_in_immutable_digest(self):
        public = self.prepare()
        fixed = self.ops._load(public['operation_id'])['immutable']
        self.assertEqual(fixed.get('control_contract_version'), 3)
        self.assertEqual(fixed.get('target_layout'), 'wsl-pc1')
        self.assertEqual(public['plan_digest'], web._digest(fixed))

    def test_new_plan_public_impact_names_actual_wsl_service(self):
        public = self.prepare()
        self.assertEqual(public['impact']['restart'],
                         ['personal-ops-coding-wsl-pc1.service', 'personal-ops-admin.service'])
        self.assertEqual(public.get('target_layout'), 'wsl-pc1')

    def test_duplicate_intent_keys_fail_before_plan_is_saved(self):
        self.intent.write_text('{"version":1,"layout":"legacy","layout":"wsl-pc1"}')
        self.assert_error('ROLLOUT_TARGET_INVALID', self.prepare)
        self.assertEqual(list(self.ops.plans_dir.glob('*.json')), [])

    def test_intent_and_detected_layout_must_agree_before_prepare(self):
        self.layout = known_layout('legacy', self.base / 'owner')
        self.assert_error('ROLLOUT_TARGET_MISMATCH', self.prepare)
        self.assertFalse(self.started)

    def test_missing_intent_cannot_silently_select_wsl(self):
        self.intent.unlink()
        self.assert_error('ROLLOUT_TARGET_MISMATCH', self.prepare)
        self.assertFalse(self.started)

    def test_layout_change_after_approval_rejects_execution(self):
        plan = self.prepare()
        self.approve(plan)
        self.layout = known_layout('legacy', self.base / 'owner')
        self.assert_error('ROLLOUT_TARGET_MISMATCH',
                          lambda: self.ops.execute(web.PROJECT, plan['operation_id']))
        self.assertFalse(self.started)

    def test_target_intent_drift_after_approval_rejects_execution(self):
        plan = self.prepare('target-intent-drift')
        self.approve(plan)
        self.intent.write_text('{"version":1,"layout":"legacy"}\n')

        self.assert_error(
            'ROLLOUT_SOURCE_CHANGED',
            lambda: self.ops.execute(web.PROJECT, plan['operation_id']))
        self.assertFalse(self.started)

    def test_unknown_contract_version_is_invalid_not_legacy(self):
        public = self.prepare()
        plan = self.ops._load(public['operation_id'])
        for version in (0, 4, 99, True, '2', None):
            with self.subTest(version=version):
                plan['immutable']['control_contract_version'] = version
                plan['plan_digest'] = web._digest(plan['immutable'])
                self.ops._save(plan)
                self.assert_error('ROLLOUT_CONTROL_CONTRACT_INVALID',
                                  lambda: self.ops.get(web.PROJECT, public['operation_id']))

    def test_full_control_digest_binds_new_layout_helper(self):
        before = web._expected_snapshots(self.root)[1]
        path = self.root / 'chatgpt-mcp/rollout_layout.py'
        path.write_bytes(path.read_bytes() + b'\n# reviewed helper changed\n')
        self.assertNotEqual(web._expected_snapshots(self.root)[1], before)

    def test_legacy_approval_accepts_unchanged_source_with_complete_new_controls(self):
        frozen, grant, module = self.claim(self.prepare_legacy())
        try:
            grant.verify(frozen, module._import_installer(),
                         module.inventory, module.control_inventory)
        except RuntimeError as exc:
            self.fail('valid legacy approval must verify all current controls: ' + str(exc))
        self.assertFalse((self.ops.receipts_dir / (grant.operation_id + '.json')).exists())

    def test_named_buildserver_v1_toss_bootstrap_survives_all_web_gates(self):
        frozen, grant, module = self.claim(self.prepare_buildserver_v1_bootstrap())
        self.assertEqual(grant.control_contract_version, 1)
        self.assertIsNone(grant.target_layout)
        try:
            grant.verify(frozen, module._import_installer(),
                         module.inventory, module.control_inventory)
        except RuntimeError as exc:
            self.fail('named buildserver v1 bootstrap must remain consumable: ' + str(exc))

    def test_genuine_old_five_field_grant_uses_digest_bound_v1_plan_metadata(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_buildserver_v1_bootstrap())
        old_grant = self.old_grant_shape(grant)

        self.assertEqual(target.verify_web_rollout_grant(
            old_grant, frozen, module._import_installer(), module.inventory,
            module.control_inventory, layout_reader=lambda: self.layout), self.layout)

    def test_genuine_old_five_field_grant_preserves_scoped_plan_binding(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_buildserver_v1_bootstrap())
        stored = self.ops._load(grant.operation_id)
        stored['immutable']['source_mode'] = 'owner_scoped'
        stored['immutable']['source_policy_digest'] = 'a' * 64
        stored['plan_digest'] = web._digest(stored['immutable'])
        self.ops._save(stored)

        self.assertEqual(target.verify_web_rollout_grant(
            self.old_grant_shape(grant), frozen, module._import_installer(),
            module.inventory, module.control_inventory,
            layout_reader=lambda: self.layout), self.layout)

    def test_legacy_grant_rejects_partial_new_metadata(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_buildserver_v1_bootstrap())
        partial = self.old_grant_shape(grant)
        partial.source_mode = 'project'
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
            target.verify_web_rollout_grant(
                partial, frozen, module._import_installer(), module.inventory,
                module.control_inventory, layout_reader=lambda: self.layout)

    def test_v3_grant_requires_all_new_metadata(self):
        target = self.target_module()
        v3_frozen, v3_grant, v3_module = self.claim(
            self.prepare('v3-old-shape-denied'))
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
            target.verify_web_rollout_grant(
                self.old_grant_shape(v3_grant), v3_frozen,
                v3_module._import_installer(), v3_module.inventory,
                v3_module.control_inventory, layout_reader=lambda: self.layout)

    def test_old_worker_loader_bootstraps_frozen_v3_helpers_over_cached_v2(self):
        target = self.target_module()
        old_target = types.ModuleType('rollout_target')
        for name, value in vars(target).items():
            if name not in {'CONTROL_V3', 'CONTROL_V1_TOSS_BOOTSTRAP'}:
                setattr(old_target, name, value)
        old_target.CONTROL_CONTRACT_VERSION = 2
        runner_path = self.root / 'chatgpt-mcp/rollout-host-runtime.py'
        runner_spec = importlib.util.spec_from_file_location(
            'approved_runtime_rollout', runner_path)

        with mock.patch.dict(sys.modules, {'rollout_target': old_target}):
            loaded = importlib.util.module_from_spec(runner_spec)
            runner_spec.loader.exec_module(loaded)

        self.assertIsNot(loaded._rollout_target, old_target)
        self.assertEqual(loaded._rollout_target.CONTROL_CONTRACT_VERSION, 3)
        self.assertIs(loaded._rollout_authority.verify_web_rollout_grant,
                      loaded._rollout_target.verify_web_rollout_grant)

    def test_bridge_does_not_call_old_expected_snapshots_signature(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_legacy())
        with mock.patch.object(web, '_expected_snapshots',
                               side_effect=AssertionError('old snapshot API must not be extended')):
            actual = target.verify_web_rollout_grant(
                grant, frozen, module._import_installer(), module.inventory,
                module.control_inventory, layout_reader=lambda: self.layout)
        self.assertEqual(actual, self.layout)

    def test_valid_legacy_plan_without_intent_keeps_legacy_target(self):
        target = self.target_module()
        self.intent.unlink()
        self.layout = known_layout('legacy', self.base / 'owner')
        plan = self.prepare_legacy()
        self.assertEqual(plan['impact']['restart'],
                         ['personal-ops-tunnel.service', 'personal-ops-admin.service'])
        frozen, grant, module = self.claim(plan)
        self.assertEqual(target.verify_web_rollout_grant(
            grant, frozen, module._import_installer(), module.inventory,
            module.control_inventory, layout_reader=lambda: self.layout), self.layout)
        wsl = known_layout('wsl-pc1', self.base / 'owner')
        self.assert_error('ROLLOUT_TARGET_MISMATCH', lambda:
            target.verify_web_rollout_grant(grant, frozen, module._import_installer(),
                module.inventory, module.control_inventory, layout_reader=lambda: wsl))

    def test_legacy_bridge_requires_consumed_plan_owned_by_this_worker(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_legacy())
        plan = self.ops._load(grant.operation_id)
        original_state, original_pid = plan['state'], plan['worker_pid']
        for state, worker in (('queued', original_pid), ('running', original_pid + 111)):
            with self.subTest(state=state, worker=worker):
                plan.update(state=state, worker_pid=worker)
                self.ops._save(plan)
                with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
                    target.verify_web_rollout_grant(
                        grant, frozen, module._import_installer(), module.inventory,
                        module.control_inventory, layout_reader=lambda: self.layout)
        plan.update(state=original_state, worker_pid=original_pid)
        self.ops._save(plan)
        path = self.ops._plan_path(grant.operation_id)
        before = path.read_bytes()
        target.verify_web_rollout_grant(grant, frozen, module._import_installer(),
            module.inventory, module.control_inventory, layout_reader=lambda: self.layout)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_approval_still_requires_original_control_digest(self):
        target = self.target_module()
        plan = self.prepare_legacy()
        frozen, grant, module = self.claim(plan)
        stored = self.ops._load(plan['operation_id'])
        stored['immutable']['control_digest'] = 'f' * 64
        stored['plan_digest'] = web._digest(stored['immutable'])
        self.ops._save(stored)
        grant.control_digest = 'f' * 64
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_SOURCE_CHANGED'):
            target.verify_web_rollout_grant(
                grant, frozen, module._import_installer(), module.inventory,
                module.control_inventory, layout_reader=lambda: self.layout)

    def test_legacy_approval_rejects_incomplete_official_new_control_inventory(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_legacy())
        controls = module.control_inventory()
        controls.pop('tossapp_neutral_ab24_command.js', None)
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_SOURCE_CHANGED'):
            target.verify_web_rollout_grant(
                grant, frozen, module._import_installer(), module.inventory,
                lambda: controls, layout_reader=lambda: self.layout)

    def test_explicit_v1_v2_v3_contracts_remain_distinct(self):
        target = self.target_module()
        for version in (1, 2, 3):
            fixed = {'control_digest': '0' * 64}
            if version != 1:
                fixed.update(control_contract_version=version,
                             target_layout='wsl-pc1')
            elif version == 1:
                fixed['control_contract_version'] = 1
            with self.subTest(version=version):
                self.assertEqual(target.control_contract_version(fixed), version)
                inventory = target.control_inventory(
                    self.root, contract_version=version)
                self.assertEqual(
                    'tossapp_neutral_ab24_command.js' in inventory,
                    version == 3)

    def test_project_and_scoped_sources_bind_both_known_layouts(self):
        for mode in ('project', 'owner_scoped'):
            for layout_name in ('legacy', 'wsl-pc1'):
                with self.subTest(mode=mode, layout=layout_name):
                    project = self.base / ('matrix-' + mode + '-' + layout_name)
                    shutil.copytree(self.root, project)
                    candidate = project
                    if mode == 'owner_scoped':
                        candidate = self.base / ('candidate-' + layout_name)
                        shutil.copytree(project, candidate)
                    intent = candidate / 'chatgpt-mcp/rollout-target.json'
                    if layout_name == 'legacy':
                        intent.unlink(missing_ok=True)
                    else:
                        intent.write_text('{"version":1,"layout":"wsl-pc1"}\n')
                    layout = known_layout(layout_name, self.base / ('owner-' + mode + '-' + layout_name))
                    policy = '0' * 64 if mode == 'project' else 'b' * 64
                    state = self.base / ('state-' + mode + '-' + layout_name)
                    ops = web.RuntimeRolloutWeb(
                        {web.PROJECT: project}, state_dir=state,
                        launcher=lambda _argv: True,
                        runtime_reader=lambda: 'a' * 64,
                        boot_reader=lambda: self.boot_id, uid_reader=lambda: self.uid,
                        layout_reader=lambda layout=layout: layout, test_only=True)
                    with mock.patch.object(
                            web, 'select_scoped_source',
                            return_value=(candidate, mode, policy)):
                        public = ops.prepare(web.PROJECT, mode + '-' + layout_name)
                    fixed = ops._load(public['operation_id'])['immutable']
                    self.assertEqual(fixed['source_mode'], mode)
                    self.assertEqual(fixed['source_policy_digest'], policy)
                    self.assertEqual(fixed['target_layout'], layout_name)
                    self.assertEqual(fixed['control_contract_version'], 3)

    def test_source_policy_drift_after_approval_blocks_execution(self):
        selected = (self.root, 'owner_scoped', 'a' * 64)
        with mock.patch.object(web, 'select_scoped_source', return_value=selected):
            plan = self.prepare('policy-drift')
            self.approve(plan)
        drifted = (self.root, 'owner_scoped', 'b' * 64)
        with mock.patch.object(web, 'select_scoped_source', return_value=drifted):
            self.assert_error(
                'ROLLOUT_SOURCE_CHANGED',
                lambda: self.ops.execute(web.PROJECT, plan['operation_id']))
        self.assertFalse(self.started)

    def test_scoped_selector_snapshot_callback_honors_plan_contract_version(self):
        observed = []

        def selector(root, *, catalog, expected_snapshots):
            del catalog
            observed.append(expected_snapshots(root))
            return root, 'project', '0' * 64

        original = web._expected_snapshots
        with mock.patch.object(web, 'select_scoped_source', side_effect=selector), \
                mock.patch.object(
                    web, '_expected_snapshots',
                    side_effect=lambda root, *, contract_version=3: (
                        observed.append(('version', contract_version))
                        or original(root, contract_version=contract_version))):
            self.ops._source_root(2)

        self.assertIn(('version', 2), observed)

    def test_legacy_approval_binds_new_helper_and_intent_through_whole_source(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_legacy())
        for name in ('rollout_target.py', 'rollout_layout.py', 'rollout-target.json',
                     'tests/check_development_approval_ui.py'):
            with self.subTest(name=name):
                path = frozen / 'chatgpt-mcp' / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b'\n ')
                    with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_SOURCE_CHANGED'):
                        target.verify_web_rollout_grant(
                            grant, frozen, module._import_installer(), module.inventory,
                            module.control_inventory, layout_reader=lambda: self.layout)
                finally:
                    path.write_bytes(original)

    def test_target_reader_rejects_unknown_versions_values_extra_keys_and_symlinks(self):
        target = self.target_module()
        invalid = ('{"version":2,"layout":"wsl-pc1"}',
                   '{"version":true,"layout":"wsl-pc1"}',
                   '{"version":1,"layout":"arbitrary.service"}',
                   '{"version":1,"layout":"wsl-pc1","service":"other.service"}',
                   '{"version":1,"version":1,"layout":"wsl-pc1"}',
                   '{"version":1}', '[]')
        for content in invalid:
            with self.subTest(content=content):
                self.intent.write_text(content)
                self.assert_error('ROLLOUT_TARGET_INVALID', lambda:
                    target.resolve_rollout_target(self.root, layout_reader=lambda: self.layout))
        self.intent.unlink()
        real = self.root / 'chatgpt-mcp/target-fixture.json'
        real.write_text('{"version":1,"layout":"wsl-pc1"}')
        self.intent.symlink_to(real)
        self.assert_error('ROLLOUT_TARGET_INVALID', lambda:
            target.resolve_rollout_target(self.root, layout_reader=lambda: self.layout))

    def test_target_reader_allows_only_legacy_when_intent_is_absent(self):
        target = self.target_module()
        self.intent.unlink()
        legacy = known_layout('legacy', self.base / 'owner')
        self.assertEqual(target.resolve_rollout_target(self.root,
                         layout_reader=lambda: legacy), legacy)
        self.assert_error('ROLLOUT_TARGET_MISMATCH', lambda:
            target.resolve_rollout_target(self.root, layout_reader=lambda: self.layout))

    def test_frozen_target_reader_does_not_read_changed_worktree_intent(self):
        target = self.target_module()
        frozen, grant, module = self.claim(self.prepare_legacy())
        self.intent.write_text('{"version":1,"layout":"legacy"}')
        self.assertEqual(target.verify_web_rollout_grant(
            grant, frozen, module._import_installer(), module.inventory,
            module.control_inventory, layout_reader=lambda: self.layout), self.layout)
        with self.assertRaisesRegex(RuntimeError, 'WEB_ROLLOUT_GRANT_INVALID'):
            target.verify_web_rollout_grant(
                grant, self.root, module._import_installer(), module.inventory,
                module.control_inventory, layout_reader=lambda: self.layout)

    def test_pinned_target_disagreement_and_custom_service_override_are_refused(self):
        target = self.target_module()
        self.assert_error('ROLLOUT_TARGET_MISMATCH', lambda:
            target.resolve_rollout_target(self.root, pinned_layout='legacy',
                                           layout_reader=lambda: self.layout))
        forged = {**self.layout, 'service': 'unreviewed.service'}
        self.assert_error('UNRECOGNIZED_RUNTIME_LAYOUT', lambda:
            target.resolve_rollout_target(self.root, layout_reader=lambda: forged))


if __name__ == '__main__':
    unittest.main()
