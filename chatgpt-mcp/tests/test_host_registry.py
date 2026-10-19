"""Version 3 host target registry and legacy v1/v2 compatibility.

Uses temporary private files only; never reads the real host configuration.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_operation_fixtures import fixture_v3  # noqa: E402
from host_registry import HostTargetRegistry, source_input_covers  # noqa: E402
from host_runtime_config import (  # noqa: E402
    TARGET, TARGET_CONFIG, TARGET_CONFIG_V2, HostRuntimeError, extended_enabled, load_config,
)

PREVIEW = "tarororo-preview"


class HostRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.projects = {"tossApp": self.root, "alpha": self.home / "alpha"}
        self.valid_v1 = {"version": 1, "enabled": True,
                         "targets": {TARGET: copy.deepcopy(TARGET_CONFIG)}}
        self.valid_v2 = {"version": 2, "enabled": True,
                         "targets": {TARGET: copy.deepcopy(TARGET_CONFIG_V2)}}
        self.v3 = fixture_v3(self.root)
        self.path = self.home / "config" / "host-runtime.json"
        self.path.parent.mkdir(mode=0o700)

    def write(self, payload, *, raw=None, mode=0o600):
        self.path.write_bytes(raw if raw is not None else json.dumps(payload, indent=2).encode())
        self.path.chmod(mode)
        return self.path

    def assertCode(self, code, function, *args):
        with self.assertRaises(HostRuntimeError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(str(self.home), repr(caught.exception.args))
        return caught.exception

    def second_target(self, payload, name=PREVIEW, *, project="tossApp", files=None,
                      project_name=None):
        target = copy.deepcopy(payload["targets"][TARGET])
        files = files or ["deploy/preview/compose.yaml"]
        target["project"] = project
        target["compose"]["project_name"] = project_name or name
        target["compose"]["files"] = files
        target["source_inputs"]["files"] += [f for f in files
                                             if f not in target["source_inputs"]["files"]]
        payload["targets"][name] = target
        return payload

    def test_v1_v2_do_not_enable_registered_writes(self):
        for payload in (self.valid_v1, self.valid_v2):
            with self.subTest(version=payload["version"]):
                registry = HostTargetRegistry.from_payload(payload, self.projects)
                self.assertEqual(registry.list_targets("tossApp"), [])
                self.assertEqual(registry.legacy_view(), payload)
                self.assertEqual(registry.availability(),
                                 {"enabled": False, "reason": "REGISTRY_NOT_CONFIGURED"})
                self.assertCode("REGISTRY_NOT_CONFIGURED", registry.require_target,
                                "tossApp", TARGET)
                from_file = HostTargetRegistry(self.projects, self.write(payload))
                self.assertEqual(from_file.list_targets("tossApp"), [])
                self.assertEqual(from_file.legacy_view(), payload)
                self.assertEqual(from_file.config_digest(), registry.config_digest())
                self.assertIs(load_config(self.path), True)
                self.assertIs(extended_enabled(self.path), payload["version"] == 2)

    def test_v3_legacy_view_is_exact(self):
        registry = HostTargetRegistry.from_payload(self.v3, self.projects)
        self.assertEqual(registry.legacy_view(), self.valid_v2)
        self.assertEqual(registry.availability(), {"enabled": True, "reason": None})
        [target] = registry.list_targets("tossApp")
        self.assertEqual(target, {"id": TARGET, **self.v3["targets"][TARGET]})
        self.assertEqual(registry.require_target("tossApp", TARGET), target)
        self.assertEqual(registry.list_targets("alpha"), [])
        self.assertCode("UNSUPPORTED_SCOPE", registry.require_target, "alpha", TARGET)
        self.assertCode("UNSUPPORTED_SCOPE", registry.require_target, "tossApp", "other")
        # Callers receive copies and cannot widen the loaded registry.
        target["capabilities"].append("compose.down")
        registry.legacy_view()["enabled"] = False
        self.assertNotIn("compose.down",
                         registry.require_target("tossApp", TARGET)["capabilities"])
        self.assertEqual(registry.legacy_view(), self.valid_v2)
        disabled_v2 = dict(copy.deepcopy(self.valid_v2), enabled=False)
        for legacy, enabled, extended in ((self.valid_v2, True, True),
                                          (self.valid_v1, True, False),
                                          (disabled_v2, False, False),
                                          (None, False, False)):
            with self.subTest(legacy=legacy and (legacy["version"], legacy["enabled"])):
                payload = dict(copy.deepcopy(self.v3), legacy=legacy)
                self.write(payload)
                self.assertIs(load_config(self.path), enabled)
                self.assertIs(extended_enabled(self.path), extended)
                from_file = HostTargetRegistry(self.projects, self.path)
                self.assertEqual(from_file.legacy_view(), legacy)
                self.assertEqual([t["id"] for t in from_file.list_targets("tossApp")], [TARGET])
                self.assertEqual(from_file.config_digest(),
                                 HostTargetRegistry.from_payload(payload, self.projects)
                                 .config_digest())
        # Top-level disabled closes the legacy view and registered targets together.
        self.write(dict(copy.deepcopy(self.v3), enabled=False))
        self.assertIs(load_config(self.path), False)
        self.assertIs(extended_enabled(self.path), False)
        closed = HostTargetRegistry(self.projects, self.path)
        self.assertIsNone(closed.legacy_view())
        self.assertEqual(closed.list_targets("tossApp"), [])
        self.assertEqual(closed.availability(),
                         {"enabled": False, "reason": "HOST_RUNTIME_DISABLED"})
        self.assertCode("HOST_RUNTIME_DISABLED", closed.require_target, "tossApp", TARGET)
        self.assertNotEqual(closed.config_digest(), registry.config_digest())

    def test_invalid_v3_closes_legacy_view_too(self):
        # Positive control: a valid v3 file keeps its exact legacy view readable.
        self.write(self.v3)
        self.assertIs(load_config(self.path), True)
        self.assertIs(extended_enabled(self.path), True)
        cases = {
            "production target": lambda p: p["targets"][TARGET].update(environment="production"),
            "docker socket bind mount": lambda p: p["targets"][TARGET]["policy"].update(
                bind_mounts=[{"service": "api", "source": "/var/run/docker.sock",
                              "target": "/var/run/docker.sock", "read_only": False}]),
            "agent profiles": lambda p: p.update(agent_profiles={"codex": {}}),
            "duplicate compose identity": lambda p: self.second_target(
                p, "preview-2", files=["deploy/pc-test/compose.yaml"]),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = copy.deepcopy(self.v3)
                mutate(payload)
                self.write(payload)
                self.assertCode("INVALID_HOST_CONFIG", load_config, self.path)
                self.assertCode("INVALID_HOST_CONFIG", extended_enabled, self.path)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry, self.projects, self.path)
        # A runtime without the registry module cannot interpret v3 and stays closed.
        self.write(self.v3)
        with patch.dict(sys.modules, {"host_registry": None}):
            self.assertCode("INVALID_HOST_CONFIG", load_config, self.path)
        # Project membership needs projects.json, so only the registry checks it.
        self.write(self.second_target(copy.deepcopy(self.v3), project="beta"))
        self.assertIs(load_config(self.path), True)
        self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry, self.projects, self.path)

    def test_source_input_coverage_excludes_dot_and_secret_paths(self):
        glob = {"files": [".dockerignore"], "dirs": {"server": "*"}}
        tree = {"files": [], "dirs": {"server": None}}
        expectations = [
            (glob, ".dockerignore", True),
            (glob, "server/main.ts", True),
            (glob, "server/sub/main.ts", False),
            (glob, "server/.env", False),
            (glob, "server/.env.local", False),
            (glob, "server/.npmrc", False),
            (tree, "server/sub/main.ts", True),
            (tree, "server/.env", False),
            (tree, "server/sub/.env.production", False),
            (tree, "server/.git/config", False),
            (tree, "server/.cache/main.ts", False),
            (tree, "server/sub/.npmrc", False),
            (tree, "server", False),
            (tree, "serverx/main.ts", False),
            ({"files": ["server/.env"], "dirs": {}}, "server/.env", False),
        ]
        for inputs, path, expected in expectations:
            with self.subTest(path=path, dirs=inputs["dirs"]):
                self.assertIs(source_input_covers(inputs, path), expected)
        # High-risk inputs use the same coverage rule.
        for path in ("server/src/.eslintrc", "server/migrations/.hidden.sql"):
            with self.subTest(path):
                payload = copy.deepcopy(self.v3)
                payload["targets"][TARGET]["high_risk_inputs"].append(path)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry.from_payload,
                                payload, self.projects)
        payload = copy.deepcopy(self.v3)
        payload["targets"][TARGET]["high_risk_inputs"].append("server/src/main.ts")
        HostTargetRegistry.from_payload(payload, self.projects)

    def test_missing_or_unsafe_config_is_closed(self):
        absent = self.home / "absent.json"
        missing = HostTargetRegistry(self.projects, absent)
        self.assertEqual(missing.availability(), {"enabled": False, "reason": "HOST_CONFIG_MISSING"})
        self.assertEqual(missing.list_targets("tossApp"), [])
        self.assertIsNone(missing.legacy_view())
        self.assertCode("HOST_CONFIG_MISSING", missing.require_target, "tossApp", TARGET)
        self.assertIs(load_config(absent), False)
        self.write(self.v3, mode=0o644)
        self.assertCode("UNSAFE_STATE_FILE", HostTargetRegistry, self.projects, self.path)
        self.assertCode("UNSAFE_STATE_FILE", load_config, self.path)
        self.path.chmod(0o600)
        link = self.home / "link.json"
        link.symlink_to(self.path)
        self.assertCode("UNSAFE_STATE_FILE", HostTargetRegistry, self.projects, link)
        linked = self.home / "linked-config"
        linked.symlink_to(self.path.parent)
        self.assertCode("UNSAFE_STATE_PATH", HostTargetRegistry, self.projects,
                        linked / self.path.name)
        text = json.dumps(self.v3)
        nested_duplicate = text.replace('"environment": "test"',
                                        '"environment": "production", "environment": "test"')
        self.assertNotEqual(nested_duplicate, text)
        invalid = {
            "not json": b"{not json",
            "not object": b"[]",
            "deeply nested": b"[" * 60000,
            "version 4": json.dumps(dict(self.v3, version=4)).encode(),
            "v3 over 64 KiB": text.encode() + b" " * (64 * 1024),
            "v2 over 8 KiB": json.dumps(self.valid_v2).encode() + b" " * 8192,
            "v3 duplicate top key": (text[:-1] + ', "enabled": true}').encode(),
            "v3 duplicate nested key": nested_duplicate.encode(),
        }
        for label, raw in invalid.items():
            with self.subTest(label):
                self.write(None, raw=raw)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry, self.projects, self.path)
                self.assertCode("INVALID_HOST_CONFIG", load_config, self.path)
                self.assertCode("INVALID_HOST_CONFIG", extended_enabled, self.path)
        # v3 may use up to 64 KiB, larger than the unchanged 8 KiB v1/v2 cap.
        self.write(None, raw=text.encode() + b" " * (60 * 1024))
        self.assertIs(extended_enabled(self.path), True)
        self.assertEqual(len(HostTargetRegistry(self.projects, self.path).list_targets("tossApp")), 1)

    def test_duplicate_compose_target_denied(self):
        accepted = self.second_target(copy.deepcopy(self.v3))
        accepted = self.second_target(accepted, "alpha-test", project="alpha",
                                      files=["deploy/pc-test/compose.yaml"])
        registry = HostTargetRegistry.from_payload(accepted, self.projects)
        self.assertEqual([t["id"] for t in registry.list_targets("tossApp")], [PREVIEW, TARGET])
        self.assertEqual([t["id"] for t in registry.list_targets("alpha")], ["alpha-test"])
        cases = {
            "same compose project name": lambda p: self.second_target(
                self.second_target(p), "preview-2", files=["deploy/preview2/compose.yaml"],
                project_name=PREVIEW),
            "same project and compose files": lambda p: self.second_target(
                p, "preview-2", files=["deploy/pc-test/compose.yaml"]),
            "legacy compose project under another id": lambda p: p["targets"].update(
                {"other": p["targets"].pop(TARGET)}),
            "legacy id with another compose project": lambda p: p["targets"][TARGET]["compose"]
            .update(project_name="renamed"),
            "legacy id under another project": lambda p: p["targets"][TARGET]
            .update(project="alpha"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = copy.deepcopy(self.v3)
                mutate(payload)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry.from_payload,
                                payload, self.projects)

    def test_unknown_fields_and_production_denied(self):
        HostTargetRegistry.from_payload(self.v3, self.projects)

        def target(p):
            return p["targets"][TARGET]

        def port(p):
            return target(p)["policy"]["ports"][0]

        def profile(p):
            return target(p)["exec_profiles"]["node-version"]

        def readiness(url):
            return lambda p: target(p).update(readiness={"api": url})

        def unready(mutate):
            return lambda p: (mutate(p), target(p).update(readiness=None))

        def no_services(p):
            target(p)["compose"].update(services=[], db_services=[])
            target(p)["policy"].update(ports=[], volumes=[], secrets={})
            target(p).update(exec_profiles={}, readiness=None)

        cases = {
            "top unknown key": lambda p: p.update(extra=1),
            "top missing key": lambda p: p.pop("agent_profiles"),
            "version string": lambda p: p.update(version="3"),
            "enabled not bool": lambda p: p.update(enabled=1),
            "legacy unknown key": lambda p: p["legacy"].update(extra=1),
            "legacy wrong target config": lambda p: p["legacy"].update(version=1),
            "legacy nested v3": lambda p: p.update(legacy=copy.deepcopy(p)),
            "agent profiles not yet defined": lambda p: p.update(agent_profiles={"codex": {}}),
            "targets not map": lambda p: p.update(targets=[]),
            "bad target id": lambda p: self.second_target(p, "Bad Id", project_name="bad-id"),
            "production": lambda p: target(p).update(environment="production"),
            "staging": lambda p: target(p).update(environment="staging"),
            "target unknown key": lambda p: target(p).update(privileged=True),
            "target missing key": lambda p: target(p).pop("postgres"),
            "postgres not yet defined": lambda p: target(p).update(postgres={"database": "x"}),
            "unregistered project": lambda p: self.second_target(p, project="beta"),
            "compose unknown key": lambda p: target(p)["compose"].update(profiles=["x"]),
            "compose name": lambda p: self.second_target(p, project_name="Tarororo Preview"),
            "compose absolute file": lambda p: target(p)["compose"].update(files=["/etc/c.yaml"]),
            "compose file escape": lambda p: target(p)["compose"].update(files=["../c.yaml"]),
            "compose file not frozen": lambda p: target(p)["compose"].update(files=["c.yaml"]),
            "no services": no_services,
            "duplicate services": lambda p: target(p)["compose"].update(
                services=["api", "api", "worker", "db"]),
            "db service unregistered": lambda p: target(p)["compose"].update(db_services=["pg"]),
            "policy unknown key": lambda p: target(p)["policy"].update(privileged=True),
            "port on all interfaces": unready(lambda p: port(p).update(host_ip="0.0.0.0")),
            "port on LAN": unready(lambda p: port(p).update(host_ip="192.168.0.10")),
            "port unknown key": lambda p: port(p).update(mode="host"),
            "port zero": lambda p: port(p).update(container_port=0),
            "port bool": lambda p: port(p).update(container_port=True),
            "port unknown service": unready(lambda p: port(p).update(service="ghost")),
            "volume unknown service": lambda p: target(p)["policy"]["volumes"][0].update(
                service="ghost"),
            "volume relative target": lambda p: target(p)["policy"]["volumes"][0].update(
                target="data"),
            "bind mount": lambda p: target(p)["policy"].update(bind_mounts=[{
                "service": "api", "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock", "read_only": False}]),
            "secret unknown service": lambda p: target(p)["policy"]["secrets"].update(ghost=["x"]),
            "secret bad name": lambda p: target(p)["policy"]["secrets"].update(api=["../x"]),
            "network bad name": lambda p: target(p)["policy"].update(networks=["bad net"]),
            "policy missing secret_dir": lambda p: target(p)["policy"].pop("secret_dir"),
            "secret_dir relative": lambda p: target(p)["policy"].update(secret_dir="cfg/t"),
            "secret_dir parent": lambda p: target(p)["policy"].update(
                secret_dir="/home/u/../../etc"),
            "secret_dir root": lambda p: target(p)["policy"].update(secret_dir="/"),
            "secret_dir trailing slash": lambda p: target(p)["policy"].update(
                secret_dir="/home/u/.config/t/"),
            "secret_dir not string": lambda p: target(p)["policy"].update(secret_dir=1),
            "secret_dir null with secrets": lambda p: target(p)["policy"].update(
                secret_dir=None),
            "unknown capability": lambda p: target(p)["capabilities"].append("compose.prune"),
            "flag capability": lambda p: target(p)["capabilities"].append("compose.down -v"),
            "duplicate capability": lambda p: target(p)["capabilities"].append("compose.up"),
            "exec unknown key": lambda p: profile(p).update(env={"A": "1"}),
            "exec unknown service": lambda p: profile(p).update(service="ghost"),
            "exec empty argv": lambda p: profile(p).update(argv=[]),
            "exec non-string argv": lambda p: profile(p).update(argv=["node", 1]),
            "exec relative workdir": lambda p: profile(p).update(workdir="app"),
            "exec zero timeout": lambda p: profile(p).update(timeout=0),
            "exec long timeout": lambda p: profile(p).update(timeout=3601),
            "source review": lambda p: target(p).update(source_review="none"),
            "inputs unknown key": lambda p: target(p)["source_inputs"].update(exclude=[]),
            "input escape": lambda p: target(p)["source_inputs"]["files"].append("../secret"),
            "input absolute": lambda p: target(p)["source_inputs"]["files"].append("/etc/passwd"),
            "input git": lambda p: target(p)["source_inputs"]["files"].append(".git/config"),
            "input env": lambda p: target(p)["source_inputs"]["files"].append("server/.env"),
            "input env variant": lambda p: target(p)["source_inputs"]["files"].append(
                "server/.env.local"),
            "input git dir": lambda p: target(p)["source_inputs"]["dirs"].update({".git": None}),
            "recursive glob": lambda p: target(p)["source_inputs"]["dirs"].update(
                {"scripts/content": "**/*.mjs"}),
            "nested glob": lambda p: target(p)["source_inputs"]["dirs"].update(
                {"scripts/content": "sub/*.mjs"}),
            "high risk not frozen": lambda p: target(p)["high_risk_inputs"].append("README.md"),
            "readiness https": readiness("https://127.0.0.1:3200/health/ready"),
            "readiness LAN": readiness("http://10.0.0.1:3200/health/ready"),
            "readiness hostname": readiness("http://localhost:3200/health/ready"),
            "readiness userinfo": readiness("http://user@127.0.0.1:3200/health/ready"),
            "readiness unregistered port": readiness("http://127.0.0.1:3300/health/ready"),
            "readiness query": readiness("http://127.0.0.1:3200/health/ready?x=1"),
            "readiness unknown service": lambda p: target(p).update(
                readiness={"ghost": "http://127.0.0.1:3200/health/ready"}),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = copy.deepcopy(self.v3)
                mutate(payload)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry.from_payload,
                                payload, self.projects)

    def test_compose_env_is_a_fixed_registered_map(self):
        target = HostTargetRegistry.from_payload(self.v3, self.projects).require_target(
            "tossApp", TARGET)
        # The legacy runtime injects exactly these into Compose; v3 registers them as data.
        self.assertEqual(target["compose"]["env"], {
            "TARORORO_UID": str(os.getuid()), "TARORORO_GID": str(os.getgid()),
            "TARORORO_CONFIG_DIR": str(Path.home() / ".config/tarororo-test"),
            "TARORORO_READING_MONETIZATION_TEST_MODE": "disabled"})
        empty = copy.deepcopy(self.v3)
        empty["targets"][TARGET]["compose"]["env"] = {}
        HostTargetRegistry.from_payload(empty, self.projects)

        def env(update):
            return lambda p: p["targets"][TARGET]["compose"]["env"].update(update)

        cases = {
            "missing env": lambda p: p["targets"][TARGET]["compose"].pop("env"),
            "env not map": lambda p: p["targets"][TARGET]["compose"].update(env=["A=1"]),
            "lowercase key": env({"tarororo_x": "1"}),
            "leading digit": env({"1A": "1"}),
            "dash key": env({"A-B": "1"}),
            "long key": env({"A" * 65: "1"}),
            "docker host": env({"DOCKER_HOST": "tcp://10.0.0.1:2375"}),
            "docker config": env({"DOCKER_CONFIG": "/tmp/x"}),
            "compose file": env({"COMPOSE_FILE": "/tmp/evil.yaml"}),
            "compose profiles": env({"COMPOSE_PROFILES": "tools"}),
            "buildx builder": env({"BUILDX_BUILDER": "remote"}),
            "buildkit": env({"BUILDKIT_HOST": "tcp://x"}),
            "path": env({"PATH": "/tmp"}),
            "home": env({"HOME": "/tmp"}),
            "lang": env({"LANG": "C"}),
            "locale": env({"LC_CTYPE": "C"}),
            "ld preload": env({"LD_PRELOAD": "/tmp/x.so"}),
            "python path": env({"PYTHONPATH": "/tmp"}),
            "git config": env({"GIT_CONFIG_GLOBAL": "/tmp/x"}),
            "xdg": env({"XDG_RUNTIME_DIR": "/tmp"}),
            "proxy": env({"HTTPS_PROXY": "http://10.0.0.1:3128"}),
            "ssh agent": env({"SSH_AUTH_SOCK": "/tmp/agent"}),
            "non-string value": env({"TARORORO_UID": 1000}),
            "control character": env({"TARORORO_X": "a\nb"}),
            "long value": env({"TARORORO_X": "a" * 1025}),
            "too many": env({f"TARORORO_V{index}": "1" for index in range(29)}),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                payload = copy.deepcopy(self.v3)
                mutate(payload)
                self.assertCode("INVALID_HOST_CONFIG", HostTargetRegistry.from_payload,
                                payload, self.projects)
        # Boundary positive controls: 32 entries, a 64-character key, a 1024-character value.
        full = copy.deepcopy(self.v3)
        full["targets"][TARGET]["compose"]["env"].update(
            {f"TARORORO_V{index}": "1" for index in range(27)})
        full["targets"][TARGET]["compose"]["env"]["A" * 64] = "a" * 1024
        self.assertEqual(len(full["targets"][TARGET]["compose"]["env"]), 32)
        HostTargetRegistry.from_payload(full, self.projects)

    def test_secret_dir_is_registered_with_the_secrets(self):
        target = HostTargetRegistry.from_payload(self.v3, self.projects).require_target(
            "tossApp", TARGET)
        self.assertEqual(target["policy"]["secret_dir"],
                         str(Path.home() / ".config/tarororo-test"))
        self.assertEqual(target["policy"]["secret_dir"],
                         target["compose"]["env"]["TARORORO_CONFIG_DIR"])
        self.assertEqual(target["policy"]["secrets"]["db"], ["db-password"])
        chosen = fixture_v3(self.root, secret_dir=self.home / "secrets")
        self.assertEqual(chosen["targets"][TARGET]["policy"]["secret_dir"],
                         str(self.home / "secrets"))
        self.assertEqual(chosen["targets"][TARGET]["compose"]["env"]["TARORORO_CONFIG_DIR"],
                         str(self.home / "secrets"))
        # A target without secrets needs no secret_dir.
        bare = copy.deepcopy(self.v3)
        bare["targets"][TARGET]["policy"].update(secrets={}, secret_dir=None)
        HostTargetRegistry.from_payload(bare, self.projects)

    def test_reload_takes_a_fresh_snapshot_from_the_same_source(self):
        registry = HostTargetRegistry(self.projects, self.write(self.v3))
        before = registry.config_digest()
        changed = copy.deepcopy(self.v3)
        changed["targets"][TARGET]["exec_profiles"]["node-version"]["timeout"] = 31
        self.write(changed)
        self.assertEqual(registry.config_digest(), before)
        fresh = registry.reload()
        self.assertNotEqual(fresh.config_digest(), before)
        self.assertEqual(fresh.require_target("tossApp", TARGET)["exec_profiles"]
                         ["node-version"]["timeout"], 31)
        self.write(changed, mode=0o644)
        self.assertCode("UNSAFE_STATE_FILE", registry.reload)
        payload_registry = HostTargetRegistry.from_payload(self.v3, self.projects)
        self.assertEqual(payload_registry.reload().config_digest(),
                         payload_registry.config_digest())

    def test_extended_scope_upgrade_refuses_v3_without_write(self):
        from host_runtime_web import enable_extended_scope
        operations = SimpleNamespace(_require_trusted_runtime=lambda: None,
                                     runtime=SimpleNamespace(config_path=self.path))
        self.write(dict(copy.deepcopy(self.v3), legacy=self.valid_v1))
        before = self.path.read_bytes()
        self.assertCode("UNSUPPORTED_CONFIG_VERSION", enable_extended_scope, operations,
                        "ENABLE WORKER AND DB 013")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])
        # Positive control: an exact v1 file is still upgraded to v2.
        self.write(self.valid_v1)
        self.assertTrue(enable_extended_scope(operations, "ENABLE WORKER AND DB 013")["changed"])
        self.assertEqual(json.loads(self.path.read_text()), {**self.valid_v2})


if __name__ == "__main__":
    unittest.main()
