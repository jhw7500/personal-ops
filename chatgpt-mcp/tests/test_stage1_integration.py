"""Keep predecessor v3 and registered v3 opt-ins separate during integration."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE_DIR), str(Path(__file__).resolve().parent)]
from host_operation_fixtures import fixture_v3  # noqa: E402
from host_registry import HostTargetRegistry  # noqa: E402
from host_runtime_config import (  # noqa: E402
    PROJECT, TARGET, TARGET_CONFIG_V3, HostRuntimeError,
    extended_enabled, load_config, predecessor_enabled,
)
from host_runtime_web import enable_extended_scope, enable_predecessor_scope  # noqa: E402


class ConfigIntegrationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="stage1-config-merge-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / PROJECT
        self.root.mkdir()
        self.projects = {PROJECT: self.root}
        self.path = self.home / "config" / "host-runtime.json"
        self.path.parent.mkdir(mode=0o700)
        self.predecessor = {"version": 3, "enabled": True,
                            "targets": {TARGET: copy.deepcopy(TARGET_CONFIG_V3)}}
        self.registered = fixture_v3(self.root)
        self.operations = SimpleNamespace(runtime=SimpleNamespace(config_path=self.path),
                                          _require_trusted_runtime=lambda: None)

    def write(self, payload):
        self.path.write_text(json.dumps(payload))
        self.path.chmod(0o600)

    def test_existing_predecessor_v3_stays_enabled_without_registered_permissions(self):
        self.write(self.predecessor)
        self.assertTrue(load_config(self.path))
        self.assertTrue(extended_enabled(self.path))
        self.assertTrue(predecessor_enabled(self.path))
        registry = HostTargetRegistry(self.projects, self.path)
        self.assertEqual(registry.availability(),
                         {"enabled": False, "reason": "REGISTRY_NOT_CONFIGURED"})
        self.assertEqual(registry.list_targets(PROJECT), [])
        self.assertEqual(registry.legacy_view(), self.predecessor)

    def test_registered_v3_does_not_grant_predecessor_permission(self):
        self.write(self.registered)
        self.assertTrue(load_config(self.path))
        self.assertTrue(extended_enabled(self.path))
        self.assertFalse(predecessor_enabled(self.path))
        self.assertEqual(HostTargetRegistry(self.projects, self.path).availability(),
                         {"enabled": True, "reason": None})

    def test_explicit_predecessor_legacy_view_can_coexist_with_registered_targets(self):
        payload = copy.deepcopy(self.registered)
        payload["legacy"] = self.predecessor
        self.write(payload)
        self.assertTrue(predecessor_enabled(self.path))
        registry = HostTargetRegistry(self.projects, self.path)
        self.assertEqual(registry.legacy_view(), self.predecessor)
        self.assertEqual([row["id"] for row in registry.list_targets(PROJECT)], [TARGET])

    def test_disabled_registered_envelope_closes_explicit_predecessor_scope(self):
        payload = copy.deepcopy(self.registered)
        payload.update(legacy=self.predecessor, enabled=False)
        self.write(payload)
        self.assertFalse(load_config(self.path))
        self.assertFalse(extended_enabled(self.path))
        self.assertFalse(predecessor_enabled(self.path))
        self.assertEqual(HostTargetRegistry(self.projects, self.path).list_targets(PROJECT), [])

    def test_scope_enable_cannot_flatten_registered_configuration(self):
        for function, confirmation in ((enable_extended_scope, "ENABLE WORKER AND DB 013"),
                                       (enable_predecessor_scope, "ENABLE DB 006-012")):
            with self.subTest(function=function.__name__):
                self.write(self.registered)
                before = self.path.read_bytes()
                with self.assertRaises(HostRuntimeError) as caught:
                    function(self.operations, confirmation)
                self.assertEqual(caught.exception.code, "UNSUPPORTED_CONFIG_VERSION")
                self.assertEqual(self.path.read_bytes(), before)

    def test_existing_predecessor_scope_enable_is_idempotent(self):
        self.write(self.predecessor)
        before = self.path.read_bytes()
        result = enable_extended_scope(self.operations, "ENABLE WORKER AND DB 013")
        self.assertEqual(result, {"scope": "worker,db-013", "enabled": True, "changed": False})
        result = enable_predecessor_scope(self.operations, "ENABLE DB 006-012")
        self.assertEqual(result, {"scope": "db-006-012", "enabled": True, "changed": False})
        self.assertEqual(self.path.read_bytes(), before)

    def test_registered_candidate_validation_rejects_predecessor_only_v3(self):
        self.write(self.predecessor)
        spec = importlib.util.spec_from_file_location(
            "stage1_configure", MODULE_DIR / "configure-host-runtime.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "UNSUPPORTED_CONFIG_VERSION"):
            module.validate_candidate(self.path, projects_config=self.home / "unused.json",
                                      current=self.path, output=io.StringIO())
        self.assertEqual(self.path.read_bytes(), before)
