"""Compose action adapter: render policy, identity pin, per-action argv and postcheck.

FakeHostRunner plus an in-memory Docker model only. No Docker command, host
configuration, real state directory or database is touched. The only network use is
a throwaway HTTP server on 127.0.0.1 for the readiness probe.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_actions import ACTIONS, validate_action  # noqa: E402
from host_compose_actions import (  # noqa: E402
    ComposeActionAdapter, check_policy, check_raw_model, check_secret_files, probe_readiness,
)
from host_operation_fixtures import FakeHostRunner, commit_all, fixture_v3  # noqa: E402
from host_process import HostCommandError  # noqa: E402
import host_registered_operations  # noqa: E402
from host_registered_operations import (  # noqa: E402
    OperationContext, RegisteredHostOperations, _clean_fields,
)
from host_registry import HostTargetRegistry  # noqa: E402
from host_runtime_config import PROJECT, TARGET, HostRuntimeError  # noqa: E402

OP = "0123456789abcdef0123456789abcdef"
SNAP = "{snapshot}"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
EVENT_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
READ_KEYS = {"config", "docker ps", "docker inspect", "docker image", "docker volume"}
LABELS = ("com.docker.compose.project", "com.docker.compose.service",
          "com.docker.compose.container-number", "com.docker.compose.oneoff")
SECRETS = ("db-password", "subject-secret", "toss-client.crt", "toss-client.key",
           "gemini-api-key")
PROJ = "{project_dir}"
COMPOSE_FILE = "deploy/pc-test/compose.yaml"
# Real `docker compose config` output of the registered stack (capture notes inside the file).
REAL = json.loads((Path(__file__).resolve().parent / "fixtures" /
                   "pc_test_compose_config.json").read_text())


def hexid(*parts) -> str:
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()


def values_after(argv, option):
    return [argv[index + 1] for index, token in enumerate(argv[:-1]) if token == option]


def positional_after(argv, subcommand, value_options=()):
    tokens = argv[argv.index(subcommand) + 1:]
    names, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in value_options:
            skip = True
        elif not token.startswith("-"):
            names.append(token)
    return names


def fixture_model(secret_dir) -> dict:
    """The captured real resolved model, with the build context written relative to
    --project-directory (as `context: ../..` resolves) so a wrong project directory
    resolves outside the snapshot."""
    text = json.dumps(REAL["model"]).replace('"{snapshot}"', json.dumps(PROJ + "/../.."))
    return json.loads(text.replace("{secret_dir}", str(secret_dir)))


def make_secret_dir(path: Path) -> Path:
    """Owner-only dummy secret files; the adapter only ever reads their metadata."""
    for leftover in (path, path.with_name(path.name + "-real")):
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()
        elif leftover.is_dir():
            leftover.chmod(0o700)
            for child in leftover.iterdir():  # A test may leave a 0600 directory here.
                if child.is_dir() and not child.is_symlink():
                    child.chmod(0o700)
            shutil.rmtree(leftover)
    path.mkdir()
    path.chmod(0o700)
    for name in SECRETS:
        (path / name).write_bytes(b"x")
        (path / name).chmod(0o600)
    return path


class FakeDocker:
    """In-memory Docker daemon state, driven only through FakeHostRunner responses."""

    def __init__(self, target):
        self.project = target["compose"]["project_name"]
        self.services = list(target["compose"]["services"])
        self.clock = 0
        self.images = {"tarororo-pc-test:local": "sha256:" + hexid("shared")}
        self.containers = {name: self.container(name, "sha256:" + hexid("image", name))
                           for name in self.services}
        self.extra = []
        # A busy host: unrelated and similarly named volumes must never be pinned.
        self.volumes = {"tarororo-test_database": "2026-10-01T00:00:00Z",
                        "tarororo-test_database_old": "2026-01-01T00:00:00Z",
                        "other-stack_data": "2026-01-01T00:00:00Z"}
        self.model = fixture_model(target["policy"]["secret_dir"])
        self.env_files = {}
        self.override_render = None
        self.exec_returncode = 0
        self.before = {}
        self.counts = {}
        self.guard = None
        self.ports = {"api": {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3200"}]}}

    def tick(self) -> str:
        self.clock += 1
        return f"2026-10-13T00:{self.clock // 60:02d}:{self.clock % 60:02d}.000000001Z"

    def container(self, name, image_id):
        return {"id": hexid("container", name, self.clock), "image_id": image_id,
                "started_at": self.tick(), "state": "running",
                "health": "healthy" if name == "api" else None}

    def install(self, runner):
        handlers = {"config": self.config, "docker ps": self.ps, "docker inspect": self.inspect,
                    "docker image": self.image, "docker volume": self.volume,
                    "build": self.build, "up": self.up, "restart": self.restart,
                    "stop": self.stop, "down": self.down, "exec": self.exec}
        for key, handler in handlers.items():
            runner.responses[key] = self._wrap(key, handler)

    def _wrap(self, key, handler):
        def run(argv):
            self.counts[key] = self.counts.get(key, 0) + 1
            if key not in READ_KEYS and self.guard is not None:
                self.guard(key)
            if key in self.before:
                self.before[key](argv, self.counts[key])
            return handler(argv)
        return run

    # ----- read-only -----------------------------------------------------------------
    def config(self, argv):
        project_dir = values_after(argv, "--project-directory")[0]
        files = values_after(argv, "--file")
        snapshot = files[0][:-len("/" + COMPOSE_FILE)]
        if "--no-interpolate" in argv:  # Raw render: env files are listed, never merged.
            raw = json.loads(json.dumps(REAL["raw_model"]).replace("{snapshot}", snapshot))
            for name, path in self.env_files.items():
                raw["services"][name]["env_file"] = [{"path": path, "required": True}]
            return json.dumps(raw).encode()
        model = json.loads(json.dumps(self.model).replace(SNAP, snapshot)
                           .replace(PROJ, project_dir))
        for service in model["services"].values():
            build = service.get("build")
            if isinstance(build, dict) and isinstance(build.get("context"), str):
                build["context"] = os.path.normpath(build["context"])
        for name in self.env_files:  # Like real Compose: contents merged, key dropped.
            model["services"][name].setdefault("environment", {})["INJECTED"] = "host-file"
        for extra in files[1:]:
            override = json.loads(Path(extra).read_text())
            for name, change in override["services"].items():
                model["services"][name]["image"] = change["image"]
            if self.override_render is not None:
                model = self.override_render(model)
        return json.dumps(model).encode()

    def docs(self):
        docs = []
        for name, item in self.containers.items():
            docs.append(self.doc(name, item))
        return docs + self.extra

    def doc(self, name, item, *, number="1", oneoff="False", project=None):
        project = project or self.project
        return {"Id": item["id"], "Name": f"/{project}-{name}-{number}",
                "Image": item["image_id"],
                "Config": {"Labels": dict(zip(LABELS, (project, name, number, oneoff))),
                           "Env": ["DB_PASSWORD=hunter2"]},
                "State": {"Status": item["state"], "StartedAt": item["started_at"],
                          "Health": {"Status": item["health"]} if item["health"] else None},
                "Mounts": [{"Type": "bind", "Source": "/home/secret/db-password"}],
                "NetworkSettings": {"Ports": self.ports.get(name, {})}}

    def ps(self, argv):
        return "".join(doc["Id"] + "\n" for doc in self.docs()).encode()

    def inspect(self, argv):
        wanted = [token for token in argv if HEX64.fullmatch(token)]
        by_id = {doc["Id"]: doc for doc in self.docs()}
        return json.dumps([by_id[cid] for cid in wanted]).encode()

    def image(self, argv):
        tags = argv[argv.index("inspect") + 1:]
        if any(tag not in self.images for tag in tags):
            raise HostCommandError("HOST_COMMAND_FAILED", returncode=1)
        return json.dumps([{"Id": self.images[tag], "RepoTags": [tag]} for tag in tags]).encode()

    def volume(self, argv):
        if argv[2] == "ls":
            filters = [value[5:] for value in values_after(argv, "--filter")
                       if value.startswith("name=")]
            if not filters:  # Every host volume: beyond the output cap on a busy host.
                raise HostCommandError("HOST_OUTPUT_TOO_LARGE")
            return "".join(name + "\n" for name in self.volumes
                           if any(part in name for part in filters)).encode()
        names = argv[3:]
        if any(name not in self.volumes for name in names):
            raise HostCommandError("HOST_COMMAND_FAILED", returncode=1)
        return json.dumps([{"Name": name, "CreatedAt": self.volumes[name],
                            "Mountpoint": "/var/lib/docker/volumes/" + name}
                           for name in names]).encode()

    # ----- mutating ------------------------------------------------------------------
    def override_tags(self, argv):
        files = values_after(argv, "--file")
        if len(files) < 2:
            return {}
        return {name: change["image"] for name, change in
                json.loads(Path(files[1]).read_text())["services"].items()}

    def build(self, argv):
        tags = self.override_tags(argv)
        for name in positional_after(argv, "build"):
            tag = tags.get(name, self.model["services"][name]["image"])
            self.images[tag] = "sha256:" + hexid("built", tag, self.tick())
        return b"build output with secrets\n"

    def up(self, argv):
        tags = self.override_tags(argv)
        for name in positional_after(argv, "up", ("--pull", "--wait-timeout")):
            tag = tags.get(name, self.model["services"][name].get("image"))
            if tag not in self.images:
                raise HostCommandError("HOST_COMMAND_FAILED", returncode=1)
            current = self.containers.get(name)
            if "--force-recreate" in argv or current is None:
                self.containers[name] = self.container(name, self.images[tag])
            elif current["state"] != "running":
                current.update(state="running", started_at=self.tick())
        return b""

    def restart(self, argv):
        names = set(positional_after(argv, "restart"))
        if "--no-deps" not in argv:  # Compose v2.35: depends_on.restart dependents restart too.
            names |= {name for name, service in self.model["services"].items()
                      if any(dependency in names and (rule or {}).get("restart")
                             for dependency, rule in (service.get("depends_on") or {}).items())}
        for name in names:
            self.containers[name].update(state="running", started_at=self.tick())
        return b""

    def stop(self, argv):
        for name in positional_after(argv, "stop"):
            self.containers[name]["state"] = "exited"
        return b""

    def down(self, argv):
        self.containers.clear()
        if {"-v", "--volumes"} & set(argv):
            self.volumes.clear()
        return b""

    def exec(self, argv):
        if self.exec_returncode:
            raise HostCommandError("HOST_COMMAND_FAILED", returncode=self.exec_returncode)
        return b"v20.0.0 secret-output\n"


class RecordingEmit:
    ORDER = {"PRECHECK": 0, "BACKUP": 1, "EXECUTE": 2, "VERIFY": 3}

    def __init__(self, services):
        self.services = services
        self.phase = "PRECHECK"
        self.events = []

    def __call__(self, phase, code, fields=None):
        assert self.ORDER[phase] >= self.ORDER[self.phase], (phase, self.phase)
        assert EVENT_CODE.fullmatch(code), code
        self.phase = phase
        self.events.append((phase, code, copy.deepcopy(fields or {})))

    def codes(self):
        return [code for _, code, _ in self.events]


class ComposeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.secrets = make_secret_dir(self.home / "secrets")
        self.payload = fixture_v3(self.root, secret_dir=self.secrets)
        self.snapshot = self.home / "snapshots" / OP / "source"
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "deploy/pc-test").mkdir(parents=True)
        (self.snapshot / "deploy/pc-test/compose.yaml").write_text("services: {}\n")
        self.runner = FakeHostRunner()
        self.urls = []
        self.ready = True
        self.reset_target()

    def reset_target(self):
        registry = HostTargetRegistry.from_payload(self.payload, {PROJECT: self.root})
        self.target = registry.require_target(PROJECT, TARGET)
        self.docker = FakeDocker(self.target)
        self.runner.calls.clear()
        self.runner.effects.clear()
        self.runner.responses.clear()
        self.docker.install(self.runner)
        self.adapter = ComposeActionAdapter(self.runner.run, http_get=self.probe)

    def probe(self, url):
        self.urls.append(url)
        return self.ready

    def grant(self, *capabilities):
        self.payload["targets"][TARGET]["capabilities"] += list(capabilities)
        self.reset_target()

    def spec(self, action, parameters):
        return validate_action(self.target, action, parameters)

    def preflight(self, action, parameters):
        return self.adapter.preflight(copy.deepcopy(self.target), self.spec(action, parameters),
                                      OperationContext(OP, self.snapshot))

    def run_action(self, action, parameters, *, between=None):
        """preflight -> execute -> postcheck, as the A2 manager drives an adapter."""
        spec = self.spec(action, parameters)
        pinned = self.preflight(action, parameters)
        if between:
            between()
        immutable = {"operation_id": OP, "action": spec["action"],
                     "parameters": spec["parameters"], "impact": spec["impact"],
                     "verifications": pinned["verifications"], "preflight": pinned["identity"]}
        work = self.home / "snapshots" / OP / "work"
        shutil.rmtree(work, ignore_errors=True)  # Each operation gets a fresh private dir.
        work.mkdir(mode=0o700)
        self.context = OperationContext(OP, self.snapshot, work_dir=work, read_only=False)

        def guard(key):
            assert self.context.side_effects, "mutating " + key + " before side_effects_started"
        self.docker.guard = guard
        self.emit = RecordingEmit(self.target["compose"]["services"])
        self.immutable = immutable
        result = self.adapter.execute(copy.deepcopy(self.target), copy.deepcopy(immutable),
                                      self.context, self.emit)
        return self.adapter.postcheck(copy.deepcopy(self.target), copy.deepcopy(immutable),
                                      result)

    def apply_api_fixture(self):
        self.before = copy.deepcopy(self.docker.containers)
        result = self.run_action("compose.apply", {"services": ["api"]})
        self.after = copy.deepcopy(self.docker.containers)
        return result

    def keys(self):
        return [call["key"] for call in self.runner.calls]

    def mutating(self):
        return [key for key in self.keys() if key not in READ_KEYS]

    def assertCode(self, codes, function, *args, **kwargs):
        with self.assertRaises(HostRuntimeError) as caught:
            function(*args, **kwargs)
        self.assertIn(caught.exception.code, (codes,) if isinstance(codes, str) else codes)
        self.assertNotIn(str(self.home), repr(caught.exception.args))
        return caught.exception

    def assertEventsWhitelisted(self):
        for phase, code, fields in self.emit.events:
            self.assertEqual(_clean_fields(fields, self.target["compose"]["services"]), fields,
                             code)

    # ----- the brief's acceptance test -----------------------------------------------
    def test_apply_preserves_db_worker_and_never_pulls(self):
        result = self.apply_api_fixture()
        self.assertEqual(result["verified_services"], ["api"])
        up = self.runner.last_command("up")
        self.assertTrue({"--no-deps", "--no-build", "--pull", "never"} <= set(up))
        self.assertEqual(self.after["db"], self.before["db"])
        self.assertEqual(self.after["worker"], self.before["worker"])

    def test_apply_recreates_only_selected_services_from_the_operation_image(self):
        result = self.apply_api_fixture()
        tag = f"{TARGET}-hostops-{OP}-api:local"
        built = self.docker.images[tag]
        self.assertEqual(self.runner.effects, ["build", "up"])
        up = self.runner.last_command("up")
        override = self.home / "snapshots" / OP / "work" / "compose-image-override.yaml"
        self.assertEqual(up, [
            "docker", "compose", "--project-name", TARGET, "--env-file", "/dev/null",
            "--project-directory", str(self.snapshot / "deploy/pc-test"),
            "--file", str(self.snapshot / "deploy/pc-test/compose.yaml"),
            "--file", str(override),
            "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate",
            "--wait", "--wait-timeout", "120", "api"])
        self.assertEqual(json.loads(override.read_text()),
                         {"services": {"api": {"image": tag}}})
        self.assertEqual(override.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.after["api"]["image_id"], built)
        self.assertNotEqual(self.after["api"]["id"], self.before["api"]["id"])
        # The shared tag the base Compose file names is never rebuilt or retagged.
        self.assertEqual(self.docker.images["tarororo-pc-test:local"], "sha256:" + hexid("shared"))
        self.assertEqual(self.runner.last_command("build")[-3:], ["build", "--quiet", "api"])
        self.assertEqual(set(result["verifications"]), set(self.immutable["verifications"]))
        self.assertEqual(self.immutable["verifications"], [
            "RENDER_POLICY", "IDENTITY_PINNED", "BUILD_IMAGES_RECORDED",
            "SELECTED_SERVICES_STATE", "OTHERS_PRESERVED", "VOLUMES_PRESERVED", "READINESS"])
        self.assertTrue(result["verified"])
        self.assertEqual(self.urls, ["http://127.0.0.1:3200/health/ready"])
        self.assertIn(("EXECUTE", "IMAGE_BUILT", {"service": "api", "image_id": built}),
                      self.emit.events)
        self.assertIn("COMPOSE_APPLIED", self.emit.codes())
        self.assertEventsWhitelisted()

    def test_build_records_operation_images_without_touching_containers(self):
        before = copy.deepcopy(self.docker.containers)
        result = self.run_action("compose.build", {"services": ["api", "worker"]})
        self.assertEqual(self.runner.effects, ["build"])
        self.assertNotIn("up", self.keys())
        self.assertEqual(self.docker.containers, before)
        self.assertEqual(self.runner.last_command("build")[-4:],
                         ["build", "--quiet", "api", "worker"])
        for service in ("api", "worker"):
            self.assertIn(f"{TARGET}-hostops-{OP}-{service}:local", self.docker.images)
        self.assertEqual(self.immutable["verifications"], [
            "RENDER_POLICY", "IDENTITY_PINNED", "BUILD_IMAGES_RECORDED", "OTHERS_PRESERVED",
            "VOLUMES_PRESERVED"])
        self.assertTrue(result["verified"])
        self.assertEqual(self.urls, [])
        self.assertEqual([code for code in self.emit.codes() if code == "IMAGE_BUILT"],
                         ["IMAGE_BUILT", "IMAGE_BUILT"])
        self.assertEventsWhitelisted()

    def test_every_command_uses_the_frozen_snapshot_and_registered_env(self):
        self.apply_api_fixture()
        compose = [call for call in self.runner.calls if call["argv"][:2] == ["docker", "compose"]]
        self.assertTrue(compose)
        for call in self.runner.calls:
            argv = call["argv"]
            self.assertEqual(call["cwd"], self.snapshot, argv)
            self.assertNotIn(str(self.root), " ".join(argv))
            self.assertEqual(call["credential_env"], {})
            if argv[:2] != ["docker", "compose"]:
                self.assertEqual(call["compose_env"], {}, argv)
                continue
            self.assertEqual(argv[2:8], ["--project-name", TARGET, "--env-file", "/dev/null",
                                         "--project-directory",
                                         str(self.snapshot / "deploy/pc-test")])
            self.assertEqual(values_after(argv, "--file")[0],
                             str(self.snapshot / "deploy/pc-test/compose.yaml"))
            self.assertEqual(call["compose_env"], self.target["compose"]["env"])
            self.assertNotIn("--profile", argv)

    def test_preflight_is_read_only_deterministic_and_pins_identities(self):
        first = self.preflight("compose.restart", {"services": ["api"]})
        second = self.preflight("compose.restart", {"services": ["api"]})
        self.assertEqual(first, second)
        self.assertEqual(set(self.keys()), {"config", "docker ps", "docker inspect",
                                            "docker volume"})
        self.assertEqual(self.runner.effects, [])
        identity = first["identity"]
        self.assertEqual(set(identity), {"render", "services", "volumes"})
        self.assertTrue(HEX64.fullmatch(identity["render"]))
        self.assertEqual(identity["volumes"], {"tarororo-test_database": "2026-10-01T00:00:00Z"})
        for service in ("api", "worker", "db"):
            item = self.docker.containers[service]
            self.assertEqual(identity["services"][service],
                             {"id": item["id"], "started_at": item["started_at"],
                              "image_id": item["image_id"]})
        self.assertEqual(first["verifications"], ["RENDER_POLICY", "IDENTITY_PINNED",
                                                  "SELECTED_SERVICES_STATE", "OTHERS_PRESERVED",
                                                  "VOLUMES_PRESERVED"])
        # Health flapping is not identity: it never turns a valid plan stale.
        self.docker.containers["api"]["health"] = "starting"
        self.assertEqual(self.preflight("compose.restart", {"services": ["api"]}), first)

    def test_policy_violations_are_refused_at_preflight_with_zero_mutations(self):
        def svc(name):
            return lambda m: m["services"][name]

        def put(name, **values):
            return lambda m: m["services"][name].update(values)

        def build(**values):
            return lambda m: m["services"]["api"]["build"].update(values)

        cases = {
            "unregistered service": ("WRONG_COMPOSE_CONFIG", lambda m: m["services"].update(
                migrate={"image": "tarororo-pc-test:local"})),
            "missing registered service": ("WRONG_COMPOSE_CONFIG",
                                           lambda m: m["services"].pop("worker")),
            "wrong project name": ("WRONG_COMPOSE_CONFIG", lambda m: m.update(name="other")),
            "privileged": ("POLICY_DENIED", put("api", privileged=True)),
            "root bind mount": ("POLICY_DENIED", put("api", volumes=[
                {"type": "bind", "source": "/", "target": "/host"}])),
            "docker socket bind": ("POLICY_DENIED", put("worker", volumes=[
                {"type": "bind", "source": "/var/run/docker.sock",
                 "target": "/var/run/docker.sock"}])),
            "docker socket anywhere": ("POLICY_DENIED", lambda m: svc("api")(m)[
                "environment"].update(DOCKER_HOST="unix:///run/docker.sock")),
            "api socket option": ("POLICY_DENIED", put("api", use_api_socket=True)),
            "unregistered named volume": ("POLICY_DENIED", lambda m: (
                put("api", volumes=[{"type": "volume", "source": "cache", "target": "/c"}])(m),
                m["volumes"].update(cache={"name": "tarororo-test_cache"}))),
            "registered volume on another service": ("POLICY_DENIED", put("api", volumes=[
                {"type": "volume", "source": "database", "target": "/var/lib/postgresql"}])),
            "volume device bind": ("POLICY_DENIED", lambda m: m["volumes"]["database"].update(
                driver_opts={"type": "none", "o": "bind", "device": "/"})),
            "external volume": ("POLICY_DENIED",
                                lambda m: m["volumes"]["database"].update(external=True)),
            "anonymous volume": ("POLICY_DENIED", put("api", volumes=[
                {"type": "volume", "target": "/data"}])),
            "volumes_from": ("POLICY_DENIED", put("worker", volumes_from=["db"])),
            "host network": ("POLICY_DENIED", put("worker", network_mode="host")),
            "service network join": ("POLICY_DENIED", put("worker", network_mode="service:db")),
            "container network join": ("POLICY_DENIED",
                                       put("worker", network_mode="container:abc")),
            "pid host": ("POLICY_DENIED", put("api", pid="host")),
            "ipc host": ("POLICY_DENIED", put("api", ipc="host")),
            "userns host": ("POLICY_DENIED", put("api", userns_mode="host")),
            "uts host": ("POLICY_DENIED", put("api", uts="host")),
            "devices": ("POLICY_DENIED", put("api", devices=[{"source": "/dev/kmsg"}])),
            "cap_add": ("POLICY_DENIED", put("api", cap_add=["SYS_ADMIN"])),
            "security_opt seccomp": ("POLICY_DENIED",
                                     put("api", security_opt=["seccomp=unconfined"])),
            "security_opt apparmor": ("POLICY_DENIED",
                                      put("api", security_opt=["apparmor=unconfined"])),
            "security_opt label": ("POLICY_DENIED", put("api", security_opt=["label=disable"])),
            "security_opt systempaths": ("POLICY_DENIED",
                                         put("api", security_opt=["systempaths=unconfined"])),
            "security_opt mixed": ("POLICY_DENIED", put("api", security_opt=[
                "no-new-privileges:true", "seccomp=unconfined"])),
            "syslog logging": ("POLICY_DENIED", put("api", logging={
                "driver": "syslog", "options": {"syslog-address": "tcp://203.0.113.5:514"}})),
            "fluentd logging": ("POLICY_DENIED", put("api", logging={"driver": "fluentd"})),
            "logging extra option": ("POLICY_DENIED", put("api", logging={
                "driver": "json-file", "options": {"max-size": "1m", "labels": "x"}})),
            "logging unknown key": ("POLICY_DENIED", put("api", logging={
                "driver": "local", "address": "x"})),
            "ports not a list": ("WRONG_COMPOSE_CONFIG", put("api", ports=3200)),
            "volumes not a list": ("WRONG_COMPOSE_CONFIG",
                                   put("db", volumes="database:/var/lib/postgresql")),
            "secrets not a list": ("WRONG_COMPOSE_CONFIG",
                                   put("api", secrets={"db-password": None})),
            "unregistered secret path": ("POLICY_DENIED", lambda m: m["secrets"][
                "db-password"].update(file="/etc/shadow")),
            "secret path outside secret_dir": ("POLICY_DENIED", lambda m: m["secrets"][
                "db-password"].update(file=str(self.home / "db-password"))),
            "env_file": ("POLICY_DENIED", put("api", env_file=["/home/u/.env"])),
            "extra_hosts": ("POLICY_DENIED", put("api", extra_hosts=["db:10.0.0.1"])),
            "cgroup_parent": ("POLICY_DENIED", put("api", cgroup_parent="/system.slice")),
            "sysctls": ("POLICY_DENIED", put("api", sysctls={"net.ipv4.ip_forward": "1"})),
            "service configs": ("POLICY_DENIED", put("api", configs=[{"source": "x"}])),
            "container_name": ("POLICY_DENIED", put("api", container_name="other")),
            "port on all interfaces": ("POLICY_DENIED", lambda m: svc("api")(m)["ports"][0]
                                       .update(host_ip="0.0.0.0")),
            "port without host ip": ("POLICY_DENIED",
                                     lambda m: svc("api")(m)["ports"][0].pop("host_ip")),
            "unregistered port": ("POLICY_DENIED", lambda m: svc("api")(m)["ports"][0].update(
                published="3201")),
            "port on another service": ("POLICY_DENIED", put("worker", ports=[
                {"mode": "ingress", "host_ip": "127.0.0.1", "target": 3000,
                 "published": "3200", "protocol": "tcp"}])),
            "unregistered network": ("POLICY_DENIED", lambda m: (
                m["networks"].update(lan={"name": "home-lan"}),
                svc("api")(m)["networks"].update(lan=None))),
            "external unregistered network": ("POLICY_DENIED", lambda m: m["networks"].update(
                default={"name": "host-lan", "external": True})),
            "undeclared network key": ("POLICY_DENIED", lambda m: svc("api")(m)[
                "networks"].update(ghost=None)),
            "host driver network": ("POLICY_DENIED",
                                    lambda m: m["networks"]["default"].update(driver="host")),
            "secret not registered for service": ("POLICY_DENIED", lambda m: svc("worker")(m)[
                "secrets"].append({"source": "subject-secret"})),
            "environment secret": ("POLICY_DENIED", lambda m: m["secrets"].update(
                {"db-password": {"name": "x", "environment": "PGPASSWORD"}})),
            "external secret": ("POLICY_DENIED", lambda m: m["secrets"].update(
                {"subject-secret": {"name": "x", "external": True}})),
            "build context outside snapshot": ("POLICY_DENIED", build(context="/")),
            "context outside, dockerfile inside": ("POLICY_DENIED", build(
                context="/", dockerfile=SNAP + "/deploy/pc-test/Dockerfile")),
            "relative build context": ("POLICY_DENIED", lambda m: m["services"]["api"][
                "build"].update(context=os.path.relpath(self.snapshot),
                                dockerfile=str(self.snapshot / "deploy/pc-test/Dockerfile"))),
            "build context parent": ("POLICY_DENIED", build(context=SNAP + "/../..")),
            "build context url": ("POLICY_DENIED",
                                  build(context="https://example.invalid/repo.git")),
            "dockerfile outside snapshot": ("POLICY_DENIED", build(dockerfile="/etc/Dockerfile")),
            "dockerfile parent": ("POLICY_DENIED", build(dockerfile="../../Dockerfile")),
            "build args": ("POLICY_DENIED", build(args={"TOKEN": "x"})),
            "build secrets": ("POLICY_DENIED", build(secrets=[{"source": "db-password"}])),
            "build ssh": ("POLICY_DENIED", build(ssh=["default"])),
            "build additional contexts": ("POLICY_DENIED",
                                          build(additional_contexts={"x": "/etc"})),
            "build network": ("POLICY_DENIED", build(network="host")),
            "build tags": ("POLICY_DENIED", build(tags=["postgres:18"])),
            "top-level configs": ("POLICY_DENIED", lambda m: m.update(
                configs={"x": {"file": "/etc/shadow"}})),
        }
        for label, (code, mutate) in cases.items():
            with self.subTest(label):
                self.reset_target()
                mutate(self.docker.model)
                self.assertCode(code, self.preflight, "compose.restart", {"services": ["api"]})
                self.assertEqual(self.mutating(), [])
                self.assertEqual(self.runner.effects, [])
        # Positive controls: the captured real model and the allowed variants pass.
        allowed = {
            "real model": lambda m: None,
            "json-file logging": put("api", logging={"driver": "json-file", "options": {
                "max-size": "10m", "max-file": "3", "mode": "non-blocking"}}),
            "no logging driver options": put("api", logging={"driver": "none"}),
            "bare no-new-privileges": put("api", security_opt=["no-new-privileges"]),
            "depends_on restart (restart always runs --no-deps)": lambda m: m["services"][
                "worker"]["depends_on"]["db"].update(restart=True),
        }
        for label, mutate in allowed.items():
            with self.subTest(label):
                self.reset_target()
                mutate(self.docker.model)
                self.preflight("compose.restart", {"services": ["api"]})

    def test_identity_pin_requires_one_labelled_container_per_service(self):
        base = FakeDocker(self.target)

        def scaled(docker):
            docker.extra.append(docker.doc("worker", base.container("worker", "sha256:" + "e" * 64),
                                           number="2"))

        def wrong_name(docker):
            docker.extra.append(docker.doc("worker", base.container("worker", "sha256:" + "e" * 64),
                                           number="2") | {"Name": "/evil"})

        def bad_image(docker):
            docker.containers["api"]["image_id"] = "tarororo-pc-test:local"

        def bad_started(docker):
            docker.containers["api"]["started_at"] = "yesterday; rm -rf /"

        def missing_other(docker):
            docker.containers.pop("worker")

        def foreign_project(docker):
            # Target-shaped name, foreign project label: only the label check refuses it.
            docker.extra.append(docker.doc("api", base.container("api", "sha256:" + "e" * 64),
                                           number="2", project="other-stack")
                                | {"Name": f"/{TARGET}-api-2"})

        cases = {"scale 2": ("SCALE_UNSUPPORTED", scaled),
                 "unlabelled name": ("WRONG_DOCKER_TARGET", wrong_name),
                 "malformed image id": ("HOST_RESPONSE_INVALID", bad_image),
                 "malformed started_at": ("HOST_RESPONSE_INVALID", bad_started),
                 "missing container": ("CONTAINER_MISSING", missing_other),
                 "foreign project label": ("WRONG_DOCKER_TARGET", foreign_project)}
        for label, (code, mutate) in cases.items():
            with self.subTest(label):
                self.reset_target()
                mutate(self.docker)
                self.assertCode(code, self.preflight, "compose.restart", {"services": ["api"]})
                self.assertEqual(self.runner.effects, [])
        # One-off `compose run` containers and other projects' services never count.
        self.reset_target()
        self.docker.extra.append(self.docker.doc(
            "api", base.container("api", "sha256:" + "f" * 64), number="7", oneoff="True"))
        self.preflight("compose.restart", {"services": ["api"]})
        # Creating actions may start a selected service that has no container yet.
        self.reset_target()
        self.docker.containers.pop("worker")
        self.assertIsNone(self.preflight("compose.up", {"services": ["worker"]})
                          ["identity"]["services"]["worker"])
        self.assertCode("CONTAINER_MISSING", self.preflight, "compose.restart",
                        {"services": ["worker"]})

    def test_up_restart_stop_touch_only_selected_services(self):
        commands = {
            "compose.up": ["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                           "--no-recreate", "worker"],
            "compose.restart": ["restart", "--no-deps", "worker"],
            "compose.stop": ["stop", "worker"],
        }
        for action, tail in commands.items():
            with self.subTest(action):
                self.reset_target()
                if action == "compose.up":
                    self.docker.containers["worker"]["state"] = "exited"
                before = copy.deepcopy(self.docker.containers)
                result = self.run_action(action, {"services": ["worker"]})
                key = tail[0]
                self.assertEqual(self.runner.effects, [key])
                argv = self.runner.last_command(key)
                self.assertEqual(argv[-len(tail):], tail)
                self.assertEqual(argv[:len(argv) - len(tail)], [
                    "docker", "compose", "--project-name", TARGET, "--env-file", "/dev/null",
                    "--project-directory", str(self.snapshot / "deploy/pc-test"),
                    "--file", str(self.snapshot / "deploy/pc-test/compose.yaml")])
                self.assertTrue(result["verified"])
                self.assertEqual(set(result["verifications"]),
                                 set(self.immutable["verifications"]))
                for other in ("api", "db"):
                    self.assertEqual(self.docker.containers[other], before[other])
                self.assertEqual(self.docker.containers["worker"]["id"], before["worker"]["id"])
                self.assertEqual(self.urls, [])
                self.assertEventsWhitelisted()

    def test_down_preserves_volumes_and_never_passes_volume_flags(self):
        self.grant("compose.down", "db_service")
        result = self.run_action("compose.down", {})
        argv = self.runner.last_command("down")
        self.assertEqual(argv[-1], "down")
        self.assertEqual(self.runner.effects, ["down"])
        for call in self.runner.calls:
            self.assertFalse({"-v", "--volumes", "--rmi", "--remove-orphans"} & set(call["argv"]))
        self.assertEqual(self.docker.volumes["tarororo-test_database"], "2026-10-01T00:00:00Z")
        self.assertEqual(self.docker.containers, {})
        self.assertIn("VOLUMES_PRESERVED", result["verifications"])
        self.assertTrue(result["verified"])
        # Negative control: a down that lost the registered volume is never verified.
        self.reset_target()
        self.docker.before["down"] = lambda argv, n: self.docker.volumes.clear()
        self.assertCode("VOLUME_CHANGED", self.run_action, "compose.down", {})

    def test_exec_runs_only_the_profile_argv_and_returns_no_output(self):
        result = self.run_action("compose.exec", {"profile": "node-version"})
        exec_call = next(call for call in self.runner.calls if call["key"] == "exec")
        self.assertEqual(exec_call["argv"][-7:],
                         ["exec", "-T", "--workdir", "/app", "api", "node", "--version"])
        self.assertEqual(exec_call["timeout"], 30)
        self.assertEqual(self.runner.effects, ["exec"])
        self.assertNotIn("secret-output", json.dumps([result, self.emit.events]))
        self.assertIn(("EXECUTE", "EXEC_COMPLETED", {"service": "api", "returncode": 0}),
                      self.emit.events)
        self.assertEqual(self.immutable["verifications"], [
            "RENDER_POLICY", "IDENTITY_PINNED", "EXEC_EXIT_ZERO", "OTHERS_PRESERVED",
            "VOLUMES_PRESERVED"])
        self.assertTrue(result["verified"])
        self.reset_target()
        self.docker.exec_returncode = 3
        self.assertCode("EXEC_FAILED", self.run_action, "compose.exec", {"profile": "node-version"})
        self.assertTrue(self.context.side_effects)
        self.assertIn(("EXECUTE", "EXEC_FAILED", {"service": "api", "returncode": 3}),
                      self.emit.events)

    def test_operation_tag_or_override_change_blocks_up(self):
        tag = f"{TARGET}-hostops-{OP}-api:local"

        def retag(argv, count):
            if count == 2:  # Second lookup: right before `up`.
                self.docker.images[tag] = "sha256:" + "9" * 64
        self.docker.before["docker image"] = retag
        self.assertCode("BUILD_IMAGE_CHANGED", self.run_action, "compose.apply",
                        {"services": ["api"]})
        self.assertEqual(self.runner.effects, ["build"])
        self.assertTrue(self.context.side_effects)

        self.reset_target()
        renders = []

        def tamper_late(model):
            renders.append(1)
            if len(renders) == 2:  # The re-verification right before `up`.
                model["services"]["worker"]["image"] = tag
            return model
        self.docker.override_render = tamper_late
        self.assertCode("UNSAFE_IMAGE_OVERRIDE", self.run_action, "compose.apply",
                        {"services": ["api"]})
        self.assertEqual(self.runner.effects, ["build"])

        self.reset_target()
        self.docker.override_render = lambda model: (
            model["services"]["api"].update(privileged=True), model)[1]
        self.assertCode("UNSAFE_IMAGE_OVERRIDE", self.run_action, "compose.apply",
                        {"services": ["api"]})
        self.assertEqual(self.runner.effects, [])
        self.assertFalse(self.context.side_effects)

    def test_unexpected_generation_is_stale_before_and_unverified_after_mutation(self):
        def restart_worker(*_):
            self.docker.containers["worker"]["started_at"] = self.docker.tick()
        self.assertCode("STALE_PLAN", self.run_action, "compose.apply", {"services": ["api"]},
                        between=restart_worker)
        self.assertEqual(self.mutating(), [])
        self.assertFalse(self.context.side_effects)

        self.reset_target()
        self.docker.before["up"] = restart_worker
        self.assertCode("STALE_PLAN", self.run_action, "compose.apply", {"services": ["api"]})
        self.assertEqual(self.runner.effects, ["build", "up"])

        self.reset_target()
        self.docker.before["stop"] = lambda argv, n: self.docker.containers["api"].update(
            state="exited")
        self.assertCode("STALE_PLAN", self.run_action, "compose.stop", {"services": ["worker"]})

        self.reset_target()
        self.ready = False
        self.assertCode("SERVICE_NOT_READY", self.run_action, "compose.apply",
                        {"services": ["api"]})

    def test_selected_service_state_must_show_the_effect(self):
        noop = {"compose.restart": "restart", "compose.stop": "stop", "compose.up": "up"}
        for action, key in noop.items():
            with self.subTest(action):
                self.reset_target()
                self.docker.containers["worker"]["state"] = (
                    "exited" if action == "compose.up" else "running")
                self.runner.responses[key] = lambda argv: b""  # Compose "succeeded", no effect.
                self.assertCode("SERVICE_STATE_UNVERIFIED", self.run_action, action,
                                {"services": ["worker"]})
        # apply whose container came back on another image than the operation build.
        self.reset_target()
        stale = self.docker.images["tarororo-pc-test:local"]

        def recreate_shared(argv):
            self.docker.containers["api"] = self.docker.container("api", stale)
            return b""
        self.runner.responses["up"] = recreate_shared
        self.assertCode("SERVICE_STATE_UNVERIFIED", self.run_action, "compose.apply",
                        {"services": ["api"]})

    def test_postcheck_rejects_render_drift_and_changed_build_tags(self):
        self.docker.before["restart"] = lambda argv, n: self.docker.model["services"]["api"][
            "environment"].update(APP_ENV="production")
        self.assertCode("STALE_PLAN", self.run_action, "compose.restart", {"services": ["api"]})
        self.reset_target()
        tag = f"{TARGET}-hostops-{OP}-api:local"
        self.docker.before["docker image"] = lambda argv, n: n == 2 and self.docker.images.update(
            {tag: "sha256:" + "8" * 64})
        self.assertCode("BUILD_IMAGE_CHANGED", self.run_action, "compose.build",
                        {"services": ["api"]})
        self.assertEqual(self.runner.effects, ["build"])

    def test_exec_requires_a_running_container_and_valid_operation_tags(self):
        self.docker.containers["api"]["state"] = "exited"
        self.assertCode("SERVICE_NOT_RUNNING", self.preflight, "compose.exec",
                        {"profile": "node-version"})
        self.reset_target()
        target = copy.deepcopy(self.target)
        target["compose"]["project_name"] = self.docker.project = "tarororo_"
        self.docker.model["name"] = "tarororo_"
        with self.assertRaises(HostRuntimeError) as caught:
            self.adapter.preflight(target, validate_action(target, "compose.build",
                                                           {"services": ["api"]}),
                                   OperationContext(OP, self.snapshot))
        self.assertEqual(caught.exception.code, "INVALID_IMAGE_TAG")
        self.assertEqual(self.runner.effects, [])

    def test_restart_and_stop_affect_exactly_the_selected_services(self):
        self.grant("db_service")
        for action, tail in (("compose.restart", ["restart", "--no-deps", "db"]),
                             ("compose.stop", ["stop", "db"])):
            with self.subTest(action):
                self.reset_target()
                # worker restarts with db unless --no-deps confines the restart (Compose v2.35).
                self.docker.model["services"]["worker"]["depends_on"]["db"]["restart"] = True
                before = copy.deepcopy(self.docker.containers)
                result = self.run_action(action, {"services": ["db"]})
                self.assertEqual(self.runner.last_command(tail[0])[-len(tail):], tail)
                for other in ("api", "worker"):
                    self.assertEqual(self.docker.containers[other], before[other])
                self.assertNotEqual(self.docker.containers["db"], before["db"])
                self.assertTrue(result["verified"])

    def test_env_file_is_refused_although_the_resolved_render_hides_it(self):
        self.docker.env_files["api"] = "/home/u/.aws/credentials"
        resolved = json.loads(self.runner.responses["config"]([
            "docker", "compose", "--project-name", TARGET, "--env-file", "/dev/null",
            "--project-directory", str(self.snapshot / "deploy/pc-test"),
            "--file", str(self.snapshot / COMPOSE_FILE), "config", "--format", "json"]))
        self.assertNotIn("env_file", resolved["services"]["api"])
        check_policy(self.target, resolved, self.snapshot)  # The resolved form alone passes.
        self.runner.calls.clear()
        self.assertCode("POLICY_DENIED", self.preflight, "compose.restart", {"services": ["api"]})
        self.assertEqual(self.mutating(), [])
        raw = [call["argv"] for call in self.runner.calls if "--no-interpolate" in call["argv"]]
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0][-5:], ["config", "--format", "json", "--no-interpolate",
                                       "--no-env-resolution"])
        self.assertEqual(values_after(raw[0], "--project-directory"),
                         [str(self.snapshot / "deploy/pc-test")])

    def test_secret_files_must_be_registered_owner_only_regular_files(self):
        db = self.secrets / "db-password"
        real = self.secrets.with_name("secrets-real")

        def symlink_dir():
            self.secrets.rename(real)
            self.secrets.symlink_to(real)

        cases = {
            "symlinked secret": ("UNSAFE_SECRET_FILE", lambda: (
                db.unlink(), (self.home / "elsewhere").write_text("x"),
                db.symlink_to(self.home / "elsewhere"))),
            "group readable": ("UNSAFE_SECRET_FILE", lambda: db.chmod(0o640)),
            "empty": ("UNSAFE_SECRET_FILE", lambda: db.write_bytes(b"")),
            "oversize": ("UNSAFE_SECRET_FILE", lambda: db.write_bytes(b"x" * (64 * 1024 + 1))),
            # 0600 and non-empty: only the regular-file check refuses it.
            "directory": ("UNSAFE_SECRET_FILE", lambda: (db.unlink(), db.mkdir(), db.chmod(0o600))),
            "missing": ("SECRET_NOT_READY", db.unlink),
            "open secret dir": ("UNSAFE_SECRET_DIR", lambda: self.secrets.chmod(0o750)),
            "symlinked secret dir": ("UNSAFE_SECRET_DIR", symlink_dir),
            # 0700 and owner-owned: only the directory check refuses it.
            "secret dir is a file": ("UNSAFE_SECRET_DIR", lambda: (
                shutil.rmtree(self.secrets), self.secrets.write_text("x"),
                self.secrets.chmod(0o700))),
        }
        for label, (code, mutate) in cases.items():
            with self.subTest(label):
                make_secret_dir(self.secrets)
                self.runner.calls.clear()
                mutate()
                self.assertCode(code, self.preflight, "compose.restart", {"services": ["api"]})
                self.assertEqual(self.mutating(), [])
        make_secret_dir(self.secrets)
        self.preflight("compose.restart", {"services": ["api"]})  # Positive control.
        target = copy.deepcopy(self.target)
        target["policy"]["secret_dir"] = None
        with self.assertRaises(HostRuntimeError) as caught:
            self.adapter.preflight(target, self.spec("compose.restart", {"services": ["api"]}),
                                   OperationContext(OP, self.snapshot))
        self.assertEqual(caught.exception.code, "POLICY_DENIED")

    def test_secret_files_are_rechecked_right_before_mutation(self):
        db = self.secrets / "db-password"
        self.assertCode("UNSAFE_SECRET_FILE", self.run_action, "compose.restart",
                        {"services": ["api"]}, between=lambda: db.chmod(0o644))
        self.assertEqual(self.mutating(), [])
        self.assertFalse(self.context.side_effects)
        make_secret_dir(self.secrets)
        self.reset_target()
        self.docker.before["build"] = lambda argv, n: db.chmod(0o644)
        self.assertCode("UNSAFE_SECRET_FILE", self.run_action, "compose.apply",
                        {"services": ["api"]})
        self.assertEqual(self.runner.effects, ["build"])  # Never reaches `up`.

    def test_volume_discovery_lists_only_registered_names(self):
        identity = self.preflight("compose.restart", {"services": ["api"]})["identity"]
        self.assertEqual(identity["volumes"], {"tarororo-test_database": "2026-10-01T00:00:00Z"})
        self.assertIn(["docker", "volume", "ls", "--quiet", "--filter",
                       "name=tarororo-test_database"],
                      [call["argv"] for call in self.runner.calls])

    def test_apply_requires_buildable_selected_services(self):
        self.grant("db_service")
        self.assertCode("SERVICE_NOT_BUILDABLE", self.preflight, "compose.apply",
                        {"services": ["db"]})
        self.assertEqual(self.runner.effects, [])

    def test_inspect_target_returns_only_whitelisted_metadata(self):
        view = self.adapter.inspect_target(copy.deepcopy(self.target))
        self.assertEqual(set(view), {"target", "compose_project", "services"})
        self.assertEqual(set(view["services"]), {"api", "worker", "db"})
        for service, item in view["services"].items():
            self.assertEqual(set(item), {"present", "count", "state", "health", "image_id",
                                         "started_at", "project", "service", "ports_registered"})
            self.assertEqual((item["project"], item["service"], item["count"]),
                             (TARGET, service, 1))
            self.assertTrue(item["ports_registered"])
        self.assertEqual(view["services"]["api"]["health"], "healthy")
        text = json.dumps(view)
        for secret in ("hunter2", "DB_PASSWORD", "/home/secret", "Mounts", "Env"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.mutating(), [])
        self.docker.ports["api"]["3000/tcp"].append({"HostIp": "0.0.0.0", "HostPort": "3200"})
        self.assertFalse(self.adapter.inspect_target(copy.deepcopy(self.target))
                         ["services"]["api"]["ports_registered"])

    def test_default_factory_maps_compose_actions_only(self):
        for action in ACTIONS:
            adapter = host_registered_operations._default_adapter_factory(
                copy.deepcopy(self.target), action, self.runner.run)
            self.assertIsInstance(adapter, ComposeActionAdapter)
            self.assertEqual(adapter.runner, self.runner.run)
            self.assertIs(adapter.http_get, probe_readiness)
        # Dispatch is by the compose. prefix, so DB actions added to ACTIONS later never
        # reach the Compose adapter.
        with patch.object(host_registered_operations, "ACTIONS", ACTIONS + ("db.apply",),
                          create=True):
            for action in ("db.query", "db.apply", "Compose.up", "", None):
                with self.subTest(action=action):
                    with self.assertRaises(HostRuntimeError) as caught:
                        host_registered_operations._default_adapter_factory(
                            copy.deepcopy(self.target), action, self.runner.run)
                    self.assertEqual(caught.exception.code, "ADAPTER_UNAVAILABLE")


class RealComposeShapeTest(unittest.TestCase):
    """Policy against the captured real `docker compose config` output (R16)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        root = self.home / "tossApp"
        root.mkdir()
        self.secrets = make_secret_dir(self.home / "secrets")
        self.snapshot = self.home / "snapshot" / "source"
        self.snapshot.mkdir(parents=True)
        payload = fixture_v3(root, secret_dir=self.secrets)
        self.target = HostTargetRegistry.from_payload(payload, {PROJECT: root}).require_target(
            PROJECT, TARGET)

    def load(self, key):
        text = json.dumps(REAL[key]).replace("{snapshot}", str(self.snapshot))
        return json.loads(text.replace("{secret_dir}", str(self.secrets)))

    def test_real_render_passes_and_mutated_variants_are_refused(self):
        model, raw = self.load("model"), self.load("raw_model")
        check_policy(self.target, model, self.snapshot)
        check_secret_files(self.target, model)
        check_raw_model(raw)
        self.assertEqual(model["services"]["api"]["ports"][0]["published"], "3200")
        self.assertNotIn("migrate", model["services"])  # profiles: [tools] stay hidden.
        model_cases = {
            "syslog logging": lambda m: m["services"]["api"]["logging"].update(
                driver="syslog", options={"syslog-address": "tcp://203.0.113.5:514"}),
            "unconfined security_opt": lambda m: m["services"]["worker"][
                "security_opt"].append("seccomp=unconfined"),
            "extra bind": lambda m: m["services"]["worker"].setdefault("volumes", []).append(
                {"type": "bind", "source": "/etc", "target": "/host-etc"}),
            "unregistered secret path": lambda m: m["secrets"]["db-password"].update(
                file="/etc/shadow"),
            "context resolved from the snapshot root": lambda m: m["services"]["api"][
                "build"].update(context=os.path.normpath(str(self.snapshot) + "/../..")),
        }
        for label, mutate in model_cases.items():
            with self.subTest(label):
                mutated = copy.deepcopy(model)
                mutate(mutated)
                with self.assertRaises(HostRuntimeError) as caught:
                    check_policy(self.target, mutated, self.snapshot)
                self.assertEqual(caught.exception.code, "POLICY_DENIED")
        raw["services"]["api"]["env_file"] = [{"path": "/home/u/.env", "required": True}]
        with self.assertRaises(HostRuntimeError) as caught:
            check_raw_model(raw)
        self.assertEqual(caught.exception.code, "POLICY_DENIED")
        # depends_on.restart is accepted: compose.restart always runs with --no-deps.
        restart = copy.deepcopy(model)
        restart["services"]["worker"]["depends_on"]["db"]["restart"] = True
        check_policy(self.target, restart, self.snapshot)


class ReadinessProbeTest(unittest.TestCase):
    def setUp(self):
        self.paths = []
        paths = self.paths

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ready")
                    self.end_headers()
                    return
                status = {"/ready": 200, "/big": 200, "/down": 503}.get(self.path, 404)
                body = b"x" * (1024 * 1024) if self.path == "/big" else b'{"status":"ready"}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # The bounded reader closes early on /big; that is expected.

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_only_a_direct_2xx_from_a_loopback_url_is_ready(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY":
                                     "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}):
            self.assertTrue(probe_readiness(self.url("/ready")))
            self.assertTrue(probe_readiness(self.url("/big")))
            self.assertFalse(probe_readiness(self.url("/down")))
            self.assertFalse(probe_readiness(self.url("/missing")))
            self.paths.clear()
            self.assertFalse(probe_readiness(self.url("/redirect")))
            self.assertEqual(self.paths, ["/redirect"])  # The redirect is never followed.
        for url in (f"http://localhost:{self.port}/ready", "http://192.0.2.1/ready",
                    f"https://127.0.0.1:{self.port}/ready", "file:///etc/passwd",
                    self.url("/ready?x=1"), None):
            with self.subTest(url=url):
                self.assertFalse(probe_readiness(url))
        self.server.shutdown()
        self.server.server_close()
        self.assertFalse(probe_readiness(self.url("/ready")))


class ComposeEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.secrets = make_secret_dir(self.home / "secrets")
        self.payload = fixture_v3(self.root, secret_dir=self.secrets)
        commit_all(self.root)
        self.config = self.home / "config" / "host-runtime.json"
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text(json.dumps(self.payload))
        self.config.chmod(0o600)
        self.projects = {PROJECT: self.root}
        self.registry = HostTargetRegistry(self.projects, self.config)
        target = self.registry.require_target(PROJECT, TARGET)
        self.runner = FakeHostRunner()
        self.docker = FakeDocker(target)
        self.docker.install(self.runner)
        self.urls = []
        self.ops = RegisteredHostOperations(
            self.projects, self.registry, state_dir=self.home / "state" / "host-runtime",
            adapter_factory=self.factory, runner=self.runner.run)

    def factory(self, target, action, runner):
        return ComposeActionAdapter(runner, http_get=lambda url: self.urls.append(url) or True)

    async def finish(self):
        workers = list(self.ops._workers.values())
        await asyncio.to_thread(lambda: [worker.join(30) for worker in workers])

    async def execute(self, action, parameters, request_id="apply-1"):
        plan = self.ops.prepare(PROJECT, TARGET, action=action, parameters=parameters,
                                request_id=request_id)
        self.assertEqual(self.runner.effects, [])
        self.ops.approve_web(PROJECT, TARGET, operation_id=plan["operation_id"],
                             plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
                             confirmation=plan["operation_id"])
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        return plan, self.ops.get(PROJECT, TARGET, plan["operation_id"])

    async def test_prepare_approve_execute_applies_exactly_once(self):
        before = copy.deepcopy(self.docker.containers)
        plan, state = await self.execute("compose.apply", {"services": ["api"]})
        self.assertEqual(plan["verifications"], [
            "RENDER_POLICY", "IDENTITY_PINNED", "BUILD_IMAGES_RECORDED",
            "SELECTED_SERVICES_STATE", "OTHERS_PRESERVED", "VOLUMES_PRESERVED", "READINESS"])
        self.assertEqual((state["state"], state["code"]), ("succeeded", None))
        self.assertEqual(self.runner.effects, ["build", "up"])
        self.assertEqual(state["result"]["verifications"], plan["verifications"])
        self.assertEqual(state["result"]["services"], ["api"])
        self.assertTrue(state["result"]["verified"])
        self.assertEqual(self.docker.containers["db"], before["db"])
        self.assertEqual(self.docker.containers["worker"], before["worker"])
        snapshot = str(self.home / "state" / "host-runtime" / "registered" / "snapshots"
                       / plan["operation_id"] / "source")
        self.assertEqual(values_after(self.runner.last_command("up"), "--project-directory"),
                         [snapshot + "/deploy/pc-test"])
        events = self.ops.events(PROJECT, TARGET, plan["operation_id"], limit=200)["events"]
        codes = [event["code"] for event in events]
        for code in ("PLAN_CREATED", "HOST_APPROVED", "OPERATION_QUEUED", "OPERATION_STARTED",
                     "RENDER_VERIFIED", "IDENTITY_VERIFIED", "IMAGE_OVERRIDE_VERIFIED",
                     "IMAGE_BUILT", "COMPOSE_APPLIED", "POSTCHECK_STARTED", "POSTCHECK_VERIFIED",
                     "OPERATION_SUCCEEDED"):
            self.assertIn(code, codes)
        self.assertLess(codes.index("IMAGE_BUILT"), codes.index("COMPOSE_APPLIED"))
        text = json.dumps([events, state])
        for leaked in (snapshot, str(self.root), "secret-output", "build output", "hunter2",
                       self.payload["targets"][TARGET]["compose"]["env"]["TARORORO_CONFIG_DIR"],
                       str(self.secrets)):
            self.assertNotIn(leaked, text)
        # A repeated execute only observes the finished operation.
        again = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(again["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["build", "up"])

    async def test_generation_change_during_build_is_outcome_unknown_without_up(self):
        self.docker.before["build"] = lambda argv, n: self.docker.containers["worker"].update(
            started_at=self.docker.tick())
        _, state = await self.execute("compose.apply", {"services": ["api"]})
        self.assertEqual((state["state"], state["code"]), ("outcome_unknown", "STALE_PLAN"))
        self.assertEqual(self.runner.effects, ["build"])

    async def test_policy_and_scope_refusals_create_no_plan_or_effect(self):
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.prepare(PROJECT, TARGET, action="compose.restart",
                             parameters={"services": ["ghost"]}, request_id="ghost-1")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")
        self.assertEqual(self.runner.calls, [])
        self.docker.model["services"]["api"]["privileged"] = True
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.prepare(PROJECT, TARGET, action="compose.restart",
                             parameters={"services": ["api"]}, request_id="restart-1")
        self.assertEqual(caught.exception.code, "POLICY_DENIED")
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.ops.pending(PROJECT)["plans"], [])


if __name__ == "__main__":
    unittest.main()
