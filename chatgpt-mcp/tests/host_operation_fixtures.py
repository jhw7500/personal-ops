"""Shared fixtures for registered (version 3) host operations.

Never touches real Docker, host configuration, credentials or databases.
- fixture_v3(root) writes a minimal tossApp source tree under root and returns a
  valid version 3 payload mirroring the legacy tarororo-test stack.
- FakeHostRunner records argv and returns canned bytes; it never runs a process.
- commit_all(root) makes the fixture tree a Git repository with a reviewed HEAD.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from host_runtime import HIGH_RISK_INPUTS, SOURCE_DIRS, SOURCE_GLOB, SOURCE_SINGLE  # noqa: E402
from host_runtime_config import (  # noqa: E402
    API_HEALTH, PROJECT, TARGET, TARGET_CONFIG_V2, HostRuntimeError,
)

MIGRATIONS = (
    "001_foundation", "002_interpretation_candidates", "003_reading_flow",
    "004_subject_identity_rotation", "005_full_tarot_catalog", "006_reading_memos",
    "007_reading_purchase_verification", "008_toss_login_challenges",
    "009_account_recovery_lifecycle", "010_commerce_policy_snapshots",
    "011_checkout_fulfillment", "012_checkout_review_intake", "013_staged_reading_access",
)


def fixture_v3(root: Path, secret_dir: Path | None = None) -> dict:
    """secret_dir defaults to the real registration value (~/.config/tarororo-test); tests
    that let an adapter check secret files pass a private temporary directory."""
    root = Path(root)
    secret_dir = str(secret_dir if secret_dir is not None
                     else Path.home() / ".config/tarororo-test")
    for name in SOURCE_SINGLE:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture " + name + "\n")
    for folder in SOURCE_DIRS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    for name in MIGRATIONS:
        (root / "server/migrations" / (name + ".sql")).write_text("SELECT 1;\n")
    (root / "server/src/main.ts").write_text("export default 1;\n")
    (root / "contracts/model.ts").write_text("export const card=1;\n")
    (root / "scripts/content/validate.mjs").write_text("export {}\n")
    return {
        "version": 3,
        "enabled": True,
        "legacy": {"version": 2, "enabled": True,
                   "targets": {TARGET: copy.deepcopy(TARGET_CONFIG_V2)}},
        "targets": {TARGET: {
            "project": PROJECT,
            "environment": "test",
            "compose": {"project_name": TARGET, "files": ["deploy/pc-test/compose.yaml"],
                        "services": ["api", "worker", "db"], "db_services": ["db"],
                        # Fixed strings the legacy runtime injects via host_runtime._safe_env.
                        "env": {"TARORORO_UID": str(os.getuid()),
                                "TARORORO_GID": str(os.getgid()),
                                "TARORORO_CONFIG_DIR": secret_dir,
                                "TARORORO_READING_MONETIZATION_TEST_MODE": "disabled"}},
            "policy": {
                "networks": ["tarororo-test_default", "tarororo-test-content-internal"],
                "ports": [{"service": "api", "host_ip": "127.0.0.1", "host_port": 3200,
                           "container_port": 3000, "protocol": "tcp"}],
                "volumes": [{"service": "db", "name": "tarororo-test_database",
                             "target": "/var/lib/postgresql"}],
                "bind_mounts": [],
                "secrets": {"api": ["db-password", "subject-secret",
                                    "toss-client.crt", "toss-client.key"],
                            "worker": ["db-password", "gemini-api-key"],
                            "db": ["db-password"]},
                "secret_dir": secret_dir,
            },
            "capabilities": ["compose.build", "compose.apply", "compose.up",
                             "compose.restart", "compose.stop", "compose.exec"],
            "exec_profiles": {"node-version": {"service": "api", "argv": ["node", "--version"],
                                               "workdir": "/app", "timeout": 30}},
            "source_review": "git_head",
            "source_inputs": {"files": list(SOURCE_SINGLE),
                              "dirs": {folder: SOURCE_GLOB.get(folder) for folder in SOURCE_DIRS}},
            "high_risk_inputs": sorted(HIGH_RISK_INPUTS),
            "readiness": {"api": API_HEALTH},
            "postgres": None,
        }},
        "agent_profiles": {},
    }


GIT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def git(root: Path, *argv: str) -> None:
    """Run a fixture git command without user/system config or hooks."""
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", *argv],
                   check=True, env=GIT_ENV, stdout=subprocess.DEVNULL)


def commit_all(root: Path, message: str = "Seed reviewed inputs") -> None:
    """Initialise (if needed) and commit every fixture file as the reviewed HEAD."""
    if not (Path(root) / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=GIT_ENV)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


COMPOSE_SUBCOMMANDS = frozenset({
    "build", "config", "create", "down", "exec", "images", "kill", "logs", "ls", "pause",
    "port", "ps", "pull", "push", "restart", "rm", "run", "start", "stop", "top",
    "unpause", "up", "version", "wait",
})
COMPOSE_VALUE_OPTIONS = frozenset({"-p", "--project-name", "-f", "--file", "--env-file",
                                   "--project-directory", "--profile", "--ansi", "--progress"})
MUTATING_KEYS = frozenset({"build", "up", "restart", "stop", "down", "exec", "image tag"})
PHASES = {"build": "BUILD", "image tag": "TAG", "up": "APPLY", "restart": "RESTART",
          "stop": "STOP", "down": "DOWN", "exec": "EXEC", "ps": "INSPECT",
          "config": "RENDER", "docker inspect": "INSPECT"}


def command_key(argv) -> str:
    """`docker compose ... up ...` -> "up"; `docker image tag` -> "image tag";
    other `docker X` -> "docker X"; anything else -> executable basename."""
    argv = [str(token) for token in argv]
    if argv[:2] == ["docker", "compose"]:
        for previous, token in zip(argv[1:], argv[2:]):
            if token in COMPOSE_SUBCOMMANDS and previous not in COMPOSE_VALUE_OPTIONS:
                return token
        return "compose"
    if argv[:3] == ["docker", "image", "tag"]:
        return "image tag"
    if argv[:1] == ["docker"] and len(argv) > 1:
        return "docker " + argv[1]
    return Path(argv[0]).name if argv else ""


class FakeHostRunner:
    """Records host commands; never executes them.

    - calls: every run() as {"argv", "key", "cwd", "timeout", "stdin_data", "credential_env",
      "compose_env"}.
    - responses: command_key(argv) -> bytes | exception to raise | callable(argv) -> bytes.
      Missing keys return b"".
    - effects: keys of successful mutating commands (MUTATING_KEYS), in order.
    - phases: command_key -> phase name. fail_phase(name) makes every command whose phase
      or key equals name raise HostRuntimeError("HOST_COMMAND_FAILED", phase) with no effect.
    - used_project_migration_script: True once any argv token contains "db:migrate".
    """

    def __init__(self):
        self.calls = []
        self.effects = []
        self.responses = {}
        self.phases = dict(PHASES)
        self.failed_phases = set()
        self.used_project_migration_script = False

    def run(self, argv, *, cwd=None, timeout=7, stdin_data=None, credential_env=None,
            compose_env=None):
        argv = [str(token) for token in argv]
        key = command_key(argv)
        self.calls.append({"argv": argv, "key": key, "cwd": cwd, "timeout": timeout,
                           "stdin_data": stdin_data, "credential_env": dict(credential_env or {}),
                           "compose_env": dict(compose_env or {})})
        if any("db:migrate" in token for token in argv):
            self.used_project_migration_script = True
        phase = self.phases.get(key, key)
        if key in self.failed_phases or phase in self.failed_phases:
            raise HostRuntimeError("HOST_COMMAND_FAILED", phase)
        response = self.responses.get(key, b"")
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(argv)
        if key in MUTATING_KEYS:
            self.effects.append(key)
        return response

    def fail_phase(self, phase: str) -> None:
        self.failed_phases.add(phase)

    def last_command(self, subcommand: str):
        for call in reversed(self.calls):
            if call["key"] == subcommand:
                return list(call["argv"])
        return None


def write_registered_fixture(home: Path, *, extra_targets=None) -> dict:
    """Committed tossApp fixture repo, an empty alpha project, owner-only dummy secret
    files, a version 3 config (0600) and projects.json, all under home.
    extra_targets(payload) may add more registered targets before the config is written."""
    import json
    from test_host_compose_actions import make_secret_dir  # Lazy: that module imports this one.
    home = Path(home)
    root = home / "tossApp"
    root.mkdir()
    alpha = home / "alpha"
    alpha.mkdir()
    secrets = make_secret_dir(home / "secrets")
    payload = fixture_v3(root, secret_dir=secrets)
    if extra_targets is not None:
        extra_targets(payload)
    commit_all(root)
    config = home / "config" / "host-runtime.json"
    config.parent.mkdir(mode=0o700)
    config.write_text(json.dumps(payload))
    config.chmod(0o600)
    projects_file = home / "projects.json"
    projects_file.write_text(json.dumps({"projects": {PROJECT: str(root), "alpha": str(alpha)}}))
    return {"home": home, "root": root, "alpha": alpha, "secrets": secrets, "config": config,
            "projects_file": projects_file, "payload": payload,
            "projects": {PROJECT: root, "alpha": alpha}}


def fake_registered_manager(projects: dict, config: Path, state_dir: Path, runner=None):
    """A trusted-by-injection manager (fake runner + Compose adapter) over a deterministic
    FakeDocker world. Worlds built from the same fixture observe identical identities until
    one of them mutates, so two processes can share one state directory."""
    import json
    from host_compose_actions import ComposeActionAdapter
    from host_registered_operations import RegisteredHostOperations
    from host_registry import HostTargetRegistry
    from test_host_compose_actions import FakeDocker  # Lazy: that module imports this one.
    runner = runner or FakeHostRunner()
    docker = FakeDocker(json.loads(Path(config).read_text())["targets"][TARGET])
    docker.install(runner)
    manager = RegisteredHostOperations(
        projects, HostTargetRegistry(projects, config), state_dir=state_dir,
        adapter_factory=lambda target, action, run: ComposeActionAdapter(
            run, http_get=lambda url: True),
        runner=runner.run)
    return manager, runner, docker
