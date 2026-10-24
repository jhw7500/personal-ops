"""Version 3 registered host targets; legacy v1/v2 files grant no registered scope.

Targets come only from the private host configuration. Every object has exact
keys, so unknown or missing fields fail closed with INVALID_HOST_CONFIG.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import urllib.parse

from host_actions import CAPABILITIES
from host_runtime_config import (
    CONFIG_PATH, PROJECT, TARGET, HostRuntimeError, is_registered_config,
    legacy_payload, parse_config, read_config,
)

TARGET_KEYS = {"project", "environment", "compose", "policy", "capabilities", "exec_profiles",
               "source_review", "source_inputs", "high_risk_inputs", "readiness", "postgres"}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
PROJECT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
GLOB = re.compile(r"[A-Za-z0-9_*?-][A-Za-z0-9._*?-]{0,63}\Z")
LOOPBACK = ("127.0.0.1", "::1")
LIMIT = 32
MAX_TARGETS = 16
MAX_EXEC_PROFILES = 16
MAX_INPUT_FILES = 256
MAX_INPUT_DIRS = 64
MAX_COMPOSE_ENV = 32
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
# Variables that would select another Docker daemon, builder, config, binary, locale,
# proxy or agent, or that collide with the fixed runner environment.
ENV_DENIED_PREFIXES = ("DOCKER_", "COMPOSE_", "BUILDX_", "BUILDKIT_", "LD_", "PYTHON", "GIT_",
                       "LC_", "XDG_")
ENV_DENIED = frozenset({"PATH", "HOME", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                        "ALL_PROXY", "FTP_PROXY", "SSH_AUTH_SOCK"})


def _require(condition) -> None:
    if not condition:
        raise HostRuntimeError("INVALID_HOST_CONFIG")


def _exact(value, keys):
    _require(isinstance(value, dict) and set(value) == keys)
    return value


def _strings(value, valid, *, limit=LIMIT, nonempty=False):
    _require(isinstance(value, list) and len(value) <= limit and (value or not nonempty)
             and all(isinstance(item, str) and valid(item) for item in value)
             and len(set(value)) == len(value))
    return value


def _repo_path(value: str) -> bool:
    return (0 < len(value) <= 512 and value.isprintable() and "\\" not in value
            and not any(part in ("", ".", "..", ".git", ".env") or part.startswith(".env.")
                        for part in value.split("/")))


def _container_path(value) -> bool:
    return (isinstance(value, str) and 1 < len(value) <= 512 and value.isprintable()
            and "\\" not in value and value.startswith("/")
            and not any(part in ("", ".", "..") for part in value[1:].split("/")))


def compose_env_valid(env) -> bool:
    """Host-registered fixed Compose variables (never client input)."""
    return (isinstance(env, dict) and len(env) <= MAX_COMPOSE_ENV
            and all(isinstance(key, str) and ENV_KEY.fullmatch(key) and key not in ENV_DENIED
                    and not key.startswith(ENV_DENIED_PREFIXES)
                    and isinstance(value, str) and len(value) <= 1024 and value.isprintable()
                    for key, value in env.items()))


def _port_number(value) -> bool:
    return type(value) is int and 1 <= value <= 65535


def source_input_covers(inputs: dict, path: str) -> bool:
    """Frozen-input rule: an exact `files` entry, or a file below a `dirs` key (null: any
    depth; glob: direct child whose basename matches). Dir matches never include a
    component starting with "."; no path with a .git/.env/.env.* component is covered."""
    if not isinstance(path, str) or not _repo_path(path):
        return False
    if path in inputs["files"]:
        return True
    for base, pattern in inputs["dirs"].items():
        rest = path[len(base) + 1:].split("/") if path.startswith(base + "/") else None
        if rest and not any(part.startswith(".") for part in rest) and (
                pattern is None or len(rest) == 1 and fnmatch.fnmatchcase(rest[0], pattern)):
            return True
    return False


def _readiness_url(service, url, ports) -> None:
    _require(isinstance(url, str) and len(url) <= 256 and url.isprintable())
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise HostRuntimeError("INVALID_HOST_CONFIG") from exc
    netloc = f"[{host}]:{port}" if host and ":" in host else f"{host}:{port}"
    _require(host in LOOPBACK and port is not None and parts.path.startswith("/")
             and url == f"http://{netloc}{parts.path}"
             and any(item["service"] == service and item["host_ip"] == host
                     and item["host_port"] == port and item["protocol"] == "tcp"
                     for item in ports))


def _validate_target(target_id, target, projects) -> None:
    _require(isinstance(target_id, str) and NAME.fullmatch(target_id))
    _exact(target, TARGET_KEYS)
    _require(isinstance(target["project"], str) and PROJECT_NAME.fullmatch(target["project"])
             and (projects is None or target["project"] in projects)
             and target["environment"] == "test" and target["postgres"] is None
             and target["source_review"] in ("git_head", "reviewed_snapshot"))
    compose = _exact(target["compose"],
                     {"project_name", "files", "services", "db_services", "env"})
    _require(compose_env_valid(compose["env"]))
    _require(isinstance(compose["project_name"], str) and NAME.fullmatch(compose["project_name"]))
    _strings(compose["files"], _repo_path, limit=8, nonempty=True)
    services = _strings(compose["services"], LABEL.fullmatch, nonempty=True)
    _strings(compose["db_services"], lambda name: name in services)
    # Only the legacy target id may own the legacy Compose project, so both paths share its lock.
    _require((target_id == TARGET) == (compose["project_name"] == TARGET)
             and (target_id != TARGET or target["project"] == PROJECT))

    policy = _exact(target["policy"],
                    {"networks", "ports", "volumes", "bind_mounts", "secrets", "secret_dir"})
    _strings(policy["networks"], LABEL.fullmatch)
    ports = policy["ports"]
    _require(isinstance(ports, list) and len(ports) <= LIMIT)
    for port in ports:
        _exact(port, {"service", "host_ip", "host_port", "container_port", "protocol"})
        _require(port["service"] in services and port["host_ip"] in LOOPBACK
                 and _port_number(port["host_port"]) and _port_number(port["container_port"])
                 and port["protocol"] in ("tcp", "udp"))
    _require(len({(p["host_ip"], p["host_port"], p["protocol"]) for p in ports}) == len(ports))
    volumes = policy["volumes"]
    _require(isinstance(volumes, list) and len(volumes) <= LIMIT)
    for volume in volumes:
        _exact(volume, {"service", "name", "target"})
        _require(volume["service"] in services and isinstance(volume["name"], str)
                 and LABEL.fullmatch(volume["name"]) and _container_path(volume["target"]))
    _require(len({(v["service"], v["target"]) for v in volumes}) == len(volumes))
    # No host bind mount policy is defined yet; any bind mount stays denied.
    _require(policy["bind_mounts"] == [])
    secrets = policy["secrets"]
    _require(isinstance(secrets, dict) and set(secrets) <= set(services))
    for names in secrets.values():
        _strings(names, LABEL.fullmatch, nonempty=True)
    # Every mounted file secret must be <secret_dir>/<name> (checked by the adapter, which
    # also requires an owner-only 0700 directory of owner-only 0600 files).
    _require(policy["secret_dir"] is None and not secrets
             or _container_path(policy["secret_dir"]))

    _strings(target["capabilities"], lambda name: name in CAPABILITIES, limit=len(CAPABILITIES))
    profiles = target["exec_profiles"]
    _require(isinstance(profiles, dict) and len(profiles) <= MAX_EXEC_PROFILES)
    for profile_id, profile in profiles.items():
        _exact(profile, {"service", "argv", "workdir", "timeout"})
        argv = profile["argv"]
        _require(NAME.fullmatch(profile_id) and profile["service"] in services
                 and isinstance(argv, list) and 0 < len(argv) <= LIMIT
                 and all(isinstance(arg, str) and 0 < len(arg) <= 1024 and arg.isprintable()
                         for arg in argv)
                 and _container_path(profile["workdir"])
                 and type(profile["timeout"]) is int and 1 <= profile["timeout"] <= 3600)

    inputs = _exact(target["source_inputs"], {"files", "dirs"})
    _strings(inputs["files"], _repo_path, limit=MAX_INPUT_FILES)
    dirs = inputs["dirs"]
    _require(isinstance(dirs, dict) and len(dirs) <= MAX_INPUT_DIRS and (inputs["files"] or dirs)
             and all(_repo_path(base) and base not in inputs["files"]
                     and (pattern is None or isinstance(pattern, str) and GLOB.fullmatch(pattern)
                          and "**" not in pattern)
                     for base, pattern in dirs.items()))
    _strings(target["high_risk_inputs"], lambda path: source_input_covers(inputs, path),
             limit=MAX_INPUT_FILES)
    # The Compose files are rendered later from the frozen source snapshot.
    _require(all(source_input_covers(inputs, path) for path in compose["files"]))

    readiness = target["readiness"]
    _require(readiness is None or isinstance(readiness, dict) and 0 < len(readiness) <= LIMIT)
    for service, url in (readiness or {}).items():
        _require(service in services)
        _readiness_url(service, url, ports)


def validate_payload(config: dict, projects: dict | None = None) -> None:
    """Deep v3 checks. projects=None (legacy path, no projects.json) skips only membership."""
    from agent_profiles import validate_agent_profiles
    validate_agent_profiles(config["agent_profiles"], projects)
    _require(len(config["targets"]) <= MAX_TARGETS)
    names, sources = set(), set()
    for target_id, target in config["targets"].items():
        _validate_target(target_id, target, projects)
        compose = target["compose"]
        source = (target["project"], tuple(sorted(compose["files"])))
        _require(compose["project_name"] not in names and source not in sources)
        names.add(compose["project_name"])
        sources.add(source)


class HostTargetRegistry:
    """Immutable snapshot of the host target registry taken at construction."""

    def __init__(self, projects: dict[str, Path], config_path: Path = CONFIG_PATH):
        self._source = Path(config_path)
        self._load(read_config(config_path), projects)

    @classmethod
    def from_payload(cls, payload: dict, projects: dict[str, Path]) -> HostTargetRegistry:
        try:
            data = json.dumps(payload).encode()
        except (TypeError, ValueError) as exc:
            raise HostRuntimeError("INVALID_HOST_CONFIG") from exc
        registry = cls.__new__(cls)
        registry._source = data
        registry._load(parse_config(data), projects)
        return registry

    def _load(self, config, projects) -> None:
        if is_registered_config(config):
            validate_payload(config, projects)
        self._projects = projects
        self._config = config

    def reload(self) -> HostTargetRegistry:
        """A new snapshot from the same source: the file is re-read and re-validated."""
        if isinstance(self._source, bytes):
            fresh = type(self).__new__(type(self))
            fresh._source = self._source
            fresh._load(parse_config(self._source), self._projects)
            return fresh
        return type(self)(self._projects, self._source)

    def availability(self) -> dict:
        if self._config is None:
            reason = "HOST_CONFIG_MISSING"
        elif not is_registered_config(self._config):
            reason = "REGISTRY_NOT_CONFIGURED"
        elif not self._config["enabled"]:
            reason = "HOST_RUNTIME_DISABLED"
        else:
            reason = None
        return {"enabled": reason is None, "reason": reason}

    def list_targets(self, project: str) -> list[dict]:
        if not self.availability()["enabled"]:
            return []
        return [{"id": target_id, **copy.deepcopy(target)}
                for target_id, target in sorted(self._config["targets"].items())
                if target["project"] == project]

    def require_target(self, project: str, target: str) -> dict:
        reason = self.availability()["reason"]
        if reason:
            raise HostRuntimeError(reason)
        targets = self._config["targets"]
        if (not isinstance(target, str) or target not in targets
                or targets[target]["project"] != project):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return {"id": target, **copy.deepcopy(targets[target])}

    def legacy_view(self) -> dict | None:
        return copy.deepcopy(legacy_payload(self._config))

    def config_digest(self) -> str:
        data = json.dumps(self._config, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode()
        return hashlib.sha256(data).hexdigest()
