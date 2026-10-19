"""Normalized Compose actions for registered targets and the fake host runner.

No real Docker or subprocess is used.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_actions import validate_action  # noqa: E402
from host_operation_fixtures import FakeHostRunner, fixture_v3  # noqa: E402
from host_registry import HostTargetRegistry  # noqa: E402
from host_runtime_config import TARGET, HostRuntimeError  # noqa: E402


def impact(recreated=(), interrupted=(), stopped=(), preserved=(), exec_service=None,
           db_connections="unaffected"):
    return {"recreated": list(recreated), "interrupted": list(interrupted),
            "stopped": list(stopped), "preserved": list(preserved),
            "exec_service": exec_service, "volumes": "preserved",
            "db_connections": db_connections}


class HostActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "tossApp"
        root.mkdir()
        self.projects = {"tossApp": root}
        self.payload = fixture_v3(root)
        self.target = self.registered()

    def registered(self, add=(), remove=(), profiles=None):
        payload = copy.deepcopy(self.payload)
        target = payload["targets"][TARGET]
        target["capabilities"] = [c for c in target["capabilities"] if c not in remove] + list(add)
        target["exec_profiles"].update(profiles or {})
        return HostTargetRegistry.from_payload(payload, self.projects).require_target("tossApp", TARGET)

    def assertCode(self, code, *args):
        with self.assertRaises(HostRuntimeError) as caught:
            validate_action(*args)
        self.assertEqual(caught.exception.code, code)

    def test_service_actions_are_normalized_with_impact(self):
        parameters = {"services": ["worker", "api"]}
        self.assertEqual(validate_action(self.target, "compose.apply", parameters), {
            "action": "compose.apply", "parameters": {"services": ["api", "worker"]},
            "impact": impact(recreated=["api", "worker"], interrupted=["api", "worker"],
                             preserved=["db"])})
        self.assertEqual(parameters, {"services": ["worker", "api"]})
        expected = {
            "compose.build": impact(preserved=["api", "worker", "db"]),
            "compose.up": impact(recreated=["api"], interrupted=["api"], preserved=["worker", "db"]),
            "compose.restart": impact(interrupted=["api"], preserved=["worker", "db"]),
            "compose.stop": impact(interrupted=["api"], stopped=["api"], preserved=["worker", "db"]),
        }
        for action, result in expected.items():
            with self.subTest(action):
                self.assertEqual(validate_action(self.target, action, {"services": ["api"]}),
                                 {"action": action, "parameters": {"services": ["api"]},
                                  "impact": result})

    def test_exec_uses_registered_profile_only(self):
        self.assertEqual(validate_action(self.target, "compose.exec", {"profile": "node-version"}), {
            "action": "compose.exec", "parameters": {"profile": "node-version"},
            "impact": impact(preserved=["api", "worker", "db"], exec_service="api")})

    def test_exec_in_db_service_requires_db_service(self):
        profiles = {"db-version": {"service": "db", "argv": ["postgres", "--version"],
                                   "workdir": "/tmp", "timeout": 30}}
        self.assertCode("POLICY_DENIED", self.registered(profiles=profiles), "compose.exec",
                        {"profile": "db-version"})
        granted = self.registered(add=["db_service"], profiles=profiles)
        self.assertEqual(validate_action(granted, "compose.exec", {"profile": "db-version"}), {
            "action": "compose.exec", "parameters": {"profile": "db-version"},
            "impact": impact(preserved=["api", "worker", "db"], exec_service="db")})

    def test_free_form_inputs_rejected(self):
        target = self.registered(add=["compose.down", "db_service"])
        cases = [
            ("compose.pull", {"services": ["api"]}),
            ("compose.prune", {}),
            ("compose.rm", {"services": ["api"]}),
            ("compose.down -v", {}),
            ("docker.run", {"services": ["api"]}),
            ("compose.apply", None),
            ("compose.apply", ["api"]),
            ("compose.apply", {}),
            ("compose.apply", {"services": []}),
            ("compose.apply", {"services": "api"}),
            ("compose.apply", {"services": ["api", "api"]}),
            ("compose.apply", {"services": ["ghost"]}),
            ("compose.apply", {"services": [1]}),
            ("compose.apply", {"services": ["api"], "flags": ["--force-recreate"]}),
            ("compose.apply", {"services": ["api"], "env": {"APP_ENV": "production"}}),
            ("compose.apply", {"services": ["api"], "target": "other"}),
            ("compose.apply", {"services": ["api"], "path": "/"}),
            ("compose.restart", {"services": ["api"], "project": "tossApp"}),
            ("compose.down", {"volumes": True}),
            ("compose.down", {"remove_orphans": True}),
            ("compose.down", {"services": ["api"]}),
            ("compose.exec", {"profile": "ghost"}),
            ("compose.exec", {"argv": ["sh", "-c", "id"]}),
            ("compose.exec", {"profile": "node-version", "argv": ["sh"]}),
            ("compose.exec", {"profile": "node-version", "workdir": "/"}),
            ("compose.exec", {"profile": ["node-version"]}),
        ]
        for action, parameters in cases:
            with self.subTest(action=action, parameters=parameters):
                self.assertCode("UNSUPPORTED_SCOPE", target, action, parameters)

    def test_capabilities_and_db_service_are_required(self):
        self.assertCode("POLICY_DENIED", self.target, "compose.down", {})
        for action in ("compose.apply", "compose.up", "compose.restart", "compose.stop"):
            with self.subTest(action):
                self.assertCode("POLICY_DENIED", self.target, action, {"services": ["api", "db"]})
                self.assertCode("POLICY_DENIED", self.registered(remove=[action]), action,
                                {"services": ["api"]})
        self.assertCode("POLICY_DENIED", self.registered(remove=["compose.exec"]), "compose.exec",
                        {"profile": "node-version"})
        db_target = self.registered(add=["db_service"])
        self.assertEqual(validate_action(db_target, "compose.restart", {"services": ["db"]})["impact"],
                         impact(interrupted=["db"], preserved=["api", "worker"],
                                db_connections="interrupted"))
        self.assertEqual(validate_action(self.target, "compose.build", {"services": ["db"]})["impact"],
                         impact(preserved=["api", "worker", "db"]))

    def test_down_stops_whole_target_and_preserves_volumes(self):
        self.assertCode("POLICY_DENIED", self.registered(add=["compose.down"]), "compose.down", {})
        target = self.registered(add=["compose.down", "db_service"])
        self.assertEqual(validate_action(target, "compose.down", {}), {
            "action": "compose.down", "parameters": {},
            "impact": impact(interrupted=["api", "worker", "db"], stopped=["api", "worker", "db"],
                             db_connections="interrupted")})

    def test_fake_runner_records_effects_without_running_processes(self):
        runner = FakeHostRunner()
        runner.responses["ps"] = b'[{"Service":"api"}]'
        runner.responses["config"] = HostRuntimeError("WRONG_COMPOSE_CONFIG")
        blocked = AssertionError("real process started")
        with patch("subprocess.run", side_effect=blocked), \
                patch("subprocess.Popen", side_effect=blocked):
            self.assertEqual(runner.run(["docker", "compose", "-p", TARGET, "ps", "--format", "json"]),
                             b'[{"Service":"api"}]')
            with self.assertRaises(HostRuntimeError):
                runner.run(["docker", "compose", "-p", TARGET, "config"])
            self.assertEqual(runner.effects, [])
            up = ["docker", "compose", "-p", "up", "up", "--no-deps", "--no-build",
                  "--pull", "never", "api"]
            self.assertEqual(runner.run(up, timeout=60), b"")
            runner.run(["docker", "image", "tag", "sha256:" + "1" * 64, "tarororo-test-api:op"])
            self.assertEqual(runner.effects, ["up", "image tag"])
            self.assertEqual(runner.last_command("up"), up)
            self.assertIsNone(runner.last_command("down"))
            runner.fail_phase("RESTART")
            with self.assertRaises(HostRuntimeError) as caught:
                runner.run(["docker", "compose", "-p", TARGET, "restart", "api"])
            self.assertEqual((caught.exception.code, caught.exception.phase),
                             ("HOST_COMMAND_FAILED", "RESTART"))
            self.assertEqual(runner.effects, ["up", "image tag"])
            self.assertFalse(runner.used_project_migration_script)
            runner.run(["docker", "compose", "-p", TARGET, "exec", "api", "npm", "run", "db:migrate"])
            self.assertTrue(runner.used_project_migration_script)
        self.assertEqual([call["key"] for call in runner.calls],
                         ["ps", "config", "up", "image tag", "restart", "exec"])
        self.assertEqual(runner.calls[2]["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
