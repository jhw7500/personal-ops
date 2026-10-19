"""Docker Compose adapter for registered version 3 targets (A2 adapter contract).

Every Compose command renders only the operation's frozen snapshot: the registered
project name, `--env-file /dev/null`, the snapshot as project directory and cwd, the
registered files and the host-registered compose.env map. The resolved model must pass
the registered policy at preflight and again right before the first mutation; an
approval never relaxes it. Each registered service is pinned to one labelled container
(docker inspect: id, image id, StartedAt) together with the render digest and the
registered volumes. Builds get operation-scoped tags through a verified image-only
override, and apply recreates only the selected services from it with
--no-deps --no-build --pull never. No command output, environment value, mount path or
log line leaves this module: events carry codes, service names, counts and image IDs.
"""
from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import re
import stat
import urllib.request

from host_process import HostCommandError
from host_runtime_config import HostRuntimeError
from host_runtime_operations import _digest

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
NUMBER_LABEL = "com.docker.compose.container-number"
ONEOFF_LABEL = "com.docker.compose.oneoff"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
NUMBER = re.compile(r"[0-9]{1,6}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
                       r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
IMAGE_TAG = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*:local\Z")
SOCKET = re.compile(r"(?:docker|containerd|podman|crio)\.sock")
READINESS_URL = re.compile(r"http://(?:127\.0\.0\.1|\[::1\]):[0-9]{1,5}/[^\s?#]*\Z")
CONTAINER_STATES = frozenset({"created", "running", "paused", "restarting", "removing",
                              "exited", "dead"})
HEALTH_STATES = frozenset({"starting", "healthy", "unhealthy"})
# Resolved-model allowlists. Any other key with a non-empty value is refused, so
# host-reaching or future Compose options (privileged, cap_add, devices, network_mode,
# pid, ipc, uts, userns_mode, env_file, extra_hosts, sysctls, cgroup_parent, configs,
# volumes_from, container_name, use_api_socket, ...) fail closed. logging and
# security_opt are allowed only in the restricted forms checked by SPECIAL_KEYS.
SERVICE_KEYS = frozenset({
    "annotations", "attach", "build", "cap_drop", "command", "cpu_count", "cpu_percent",
    "cpu_period", "cpu_quota", "cpu_shares", "cpus", "cpuset", "depends_on", "dns", "dns_opt",
    "dns_search", "domainname", "entrypoint", "environment", "expose", "healthcheck",
    "hostname", "image", "init", "labels", "mem_limit", "mem_reservation",
    "mem_swappiness", "memswap_limit", "networks", "oom_score_adj", "pids_limit", "platform",
    "ports", "profiles", "pull_policy", "read_only", "restart", "secrets", "shm_size",
    "stdin_open", "stop_grace_period", "stop_signal", "tmpfs", "tty", "ulimits", "user",
    "volumes", "working_dir"})
SAFE_VALUES = {"ipc": ("private", "shareable", "none"), "network_mode": ("none",)}
LOG_DRIVERS = (None, "json-file", "local", "none")  # Never a remote/network log driver.
LOG_OPTIONS = frozenset({"max-size", "max-file", "mode"})
SECURITY_OPTS = ("no-new-privileges", "no-new-privileges:true")
MAX_SECRET_BYTES = 64 * 1024
BUILD_KEYS = frozenset({"context", "dockerfile", "target"})
TOP_KEYS = frozenset({"name", "services", "networks", "volumes", "secrets"})
PORT_KEYS = frozenset({"mode", "host_ip", "target", "published", "protocol", "name",
                       "app_protocol"})
MAX_CONTAINERS = 256
MAX_READINESS_BODY = 64 * 1024
RENDER_TIMEOUT = 60
INSPECT_TIMEOUT = 30
BUILD_TIMEOUT = 1800
WAIT_TIMEOUT = 120
COMMAND_TIMEOUT = 300
BUILDING = ("compose.build", "compose.apply")
CREATING = ("compose.up", "compose.apply")    # may create a selected service's container
SIMPLE = {  # action -> (fixed Compose arguments before the services, event verb)
    "compose.up": (["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--no-recreate"],
                   "UP"),
    # Compose v2 restart also restarts depends_on.restart dependents unless --no-deps.
    "compose.restart": (["restart", "--no-deps"], "RESTART"),
    # stop only stops the listed services (compose v2 stop filters by name).
    "compose.stop": (["stop"], "STOP"),
    "compose.down": (["down"], "DOWN"),   # Never -v/--volumes/--rmi/--remove-orphans.
}


def _text(value):
    return value if isinstance(value, str) else None


def _json(data, code="HOST_RESPONSE_INVALID"):
    try:
        return json.loads(data)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise HostRuntimeError(code) from None


def _mentions_socket(model) -> bool:
    stack = [model]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and SOCKET.search(value):
            return True
    return False


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def _list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
    return value


def _logging_ok(value) -> bool:
    options = value.get("options") or {} if isinstance(value, dict) else None
    return (isinstance(value, dict) and set(value) <= {"driver", "options"}
            and value.get("driver") in LOG_DRIVERS and isinstance(options, dict)
            and set(options) <= LOG_OPTIONS
            and all(isinstance(option, str) for option in options.values()))


def _security_opt_ok(value) -> bool:
    return isinstance(value, list) and all(item in SECURITY_OPTS for item in value)


SPECIAL_KEYS = {"logging": _logging_ok, "security_opt": _security_opt_ok}


def _mapping(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
    return value


def _check_service(name, service, policy, networks, volumes, secrets, root) -> None:
    if not isinstance(service, dict):
        raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
    for key, value in service.items():
        if key in SPECIAL_KEYS:
            if value and not SPECIAL_KEYS[key](value):
                raise HostRuntimeError("POLICY_DENIED")
        elif value and key not in SERVICE_KEYS and value not in SAFE_VALUES.get(key, ()):
            raise HostRuntimeError("POLICY_DENIED")
    ports = {(item["host_ip"], str(item["host_port"]), item["container_port"], item["protocol"])
             for item in policy["ports"] if item["service"] == name}
    for port in _list(service.get("ports")):
        target = port.get("target") if isinstance(port, dict) else None
        if (not isinstance(port, dict) or set(port) - PORT_KEYS or type(target) is not int
                or (_text(port.get("host_ip")), str(port.get("published")), target,
                    _text(port.get("protocol", "tcp"))) not in ports):
            raise HostRuntimeError("POLICY_DENIED")
    attached = service.get("networks") or {}
    if (not isinstance(attached, (dict, list))
            or any(_text(network) not in networks for network in attached)):
        raise HostRuntimeError("POLICY_DENIED")
    mounts = {(item["name"], item["target"]) for item in policy["volumes"]
              if item["service"] == name}
    for mount in _list(service.get("volumes")):
        kind = mount.get("type") if isinstance(mount, dict) else None
        if kind == "tmpfs":
            continue
        source = volumes.get(_text(mount.get("source"))) if kind == "volume" else None
        if (not isinstance(source, dict)
                or (_text(source.get("name")), _text(mount.get("target"))) not in mounts):
            raise HostRuntimeError("POLICY_DENIED")  # bind, anonymous, unregistered, ...
    allowed = set(policy["secrets"].get(name, ()))
    folder = policy["secret_dir"]
    for item in _list(service.get("secrets")):
        source = _text(item.get("source")) if isinstance(item, dict) else None
        if (source not in allowed or source not in secrets or folder is None
                or secrets[source].get("file") != os.path.join(folder, source)):
            raise HostRuntimeError("POLICY_DENIED")  # A file secret is a host bind mount.
    build = service.get("build")
    if build is not None:
        context = _text(build.get("context")) if isinstance(build, dict) else None
        dockerfile = _text(build.get("dockerfile", "Dockerfile")) if context else None
        if (not context or not dockerfile or not os.path.isabs(context)
                or any(value for key, value in build.items() if key not in BUILD_KEYS)
                or not _inside(Path(context), root)
                or not _inside(Path(context) / dockerfile, root)):
            raise HostRuntimeError("POLICY_DENIED")


def check_raw_model(model) -> None:
    """The uninterpolated render (`--no-interpolate --no-env-resolution`) still lists
    env_file/label_file, which the normal render silently merges from host files."""
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict) or not all(isinstance(item, dict)
                                                 for item in services.values()):
        raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
    if any(item.get("env_file") or item.get("label_file") for item in services.values()):
        raise HostRuntimeError("POLICY_DENIED")


def check_secret_files(target: dict, model) -> None:
    """Every mounted secret is an owner-only 0600 regular file of 1..64 KiB in the owner-only
    0700 registered secret_dir (after check_policy). Metadata only; never read."""
    used = sorted({item["source"] for service in model["services"].values()
                   for item in _list(service.get("secrets"))})
    if not used:
        return
    folder = target["policy"]["secret_dir"]
    if folder is None:
        raise HostRuntimeError("POLICY_DENIED")
    try:
        info = os.lstat(folder)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise HostRuntimeError("UNSAFE_SECRET_DIR")
        for name in used:
            info = os.lstat(os.path.join(folder, name))
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or not 0 < info.st_size <= MAX_SECRET_BYTES):
                raise HostRuntimeError("UNSAFE_SECRET_FILE")
    except OSError:
        raise HostRuntimeError("SECRET_NOT_READY") from None


def check_policy(target: dict, model, snapshot) -> None:
    """The resolved model must stay inside the registered policy (fail closed)."""
    compose, policy = target["compose"], target["policy"]
    if (not isinstance(model, dict) or model.get("name") != compose["project_name"]
            or not isinstance(model.get("services"), dict)
            or set(model["services"]) != set(compose["services"])):
        raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
    if (any(value and key not in TOP_KEYS and not key.startswith("x-")
            for key, value in model.items()) or _mentions_socket(model)):
        raise HostRuntimeError("POLICY_DENIED")
    networks, volumes, secrets = (_mapping(model.get(key))
                                  for key in ("networks", "volumes", "secrets"))
    for network in networks.values():
        if (not isinstance(network, dict) or network.get("name") not in policy["networks"]
                or network.get("driver") not in (None, "bridge")):
            raise HostRuntimeError("POLICY_DENIED")   # External ones must be registered too.
    for volume in volumes.values():
        if (not isinstance(volume, dict) or volume.get("external") or volume.get("driver_opts")
                or volume.get("driver") not in (None, "local")):
            raise HostRuntimeError("POLICY_DENIED")
    for secret in secrets.values():
        if (not isinstance(secret, dict) or not _text(secret.get("file"))
                or any(value for key, value in secret.items() if key not in ("name", "file"))):
            raise HostRuntimeError("POLICY_DENIED")   # environment/external secrets refused.
    root = Path(snapshot).resolve()
    for name, service in model["services"].items():
        _check_service(name, service, policy, networks, volumes, secrets, root)


def _container(item, project, services):
    """(service, record) for a pinned container; None for one-off or unregistered ones."""
    config = item.get("Config") if isinstance(item, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    state = item.get("State") if isinstance(item, dict) else None
    if not isinstance(labels, dict) or not isinstance(state, dict):
        raise HostRuntimeError("HOST_RESPONSE_INVALID")
    if labels.get(PROJECT_LABEL) != project:
        raise HostRuntimeError("WRONG_DOCKER_TARGET")
    service = _text(labels.get(SERVICE_LABEL))
    if labels.get(ONEOFF_LABEL) == "True" or service not in services:
        return None
    number = _text(labels.get(NUMBER_LABEL))
    if not number or not NUMBER.fullmatch(number) or item.get("Name") != (
            f"/{project}-{service}-{number}"):
        raise HostRuntimeError("WRONG_DOCKER_TARGET")
    health = state.get("Health")
    health = health.get("Status") if isinstance(health, dict) else health
    status, started = _text(state.get("Status")), _text(state.get("StartedAt"))
    cid, image = _text(item.get("Id")), _text(item.get("Image"))
    settings = item.get("NetworkSettings")
    ports = settings.get("Ports") if isinstance(settings, dict) else None
    if (not cid or not HEX64.fullmatch(cid) or not image or not IMAGE_ID.fullmatch(image)
            or not started or not TIMESTAMP.fullmatch(started)
            or status not in CONTAINER_STATES
            or not (health is None or _text(health) in HEALTH_STATES)):
        raise HostRuntimeError("HOST_RESPONSE_INVALID")
    return service, {"id": cid, "image_id": image, "started_at": started, "state": status,
                     "health": health, "ports": ports if isinstance(ports, dict) else {}}


def _pin(item):
    return None if item is None else {key: item[key] for key in ("id", "started_at", "image_id")}


def _selected(target, action, parameters) -> list:
    if action == "compose.down":
        return list(target["compose"]["services"])
    if action == "compose.exec":
        return [target["exec_profiles"][parameters["profile"]]["service"]]
    return list(parameters["services"])


def _others(target, action, selected) -> list:
    changed = [] if action in ("compose.build", "compose.exec") else selected
    return [name for name in target["compose"]["services"] if name not in changed]


def _verifications(target, action, selected) -> list:
    codes = ["RENDER_POLICY", "IDENTITY_PINNED"]
    if action in BUILDING:
        codes.append("BUILD_IMAGES_RECORDED")
    if action in SIMPLE or action == "compose.apply":
        codes.append("SELECTED_SERVICES_STATE")
    if action == "compose.exec":
        codes.append("EXEC_EXIT_ZERO")
    if _others(target, action, selected):
        codes.append("OTHERS_PRESERVED")
    codes.append("VOLUMES_PRESERVED")
    if action == "compose.apply" and any(name in (target["readiness"] or {}) for name in selected):
        codes.append("READINESS")
    return codes


def _tags(target, selected, operation_id) -> dict:
    project = target["compose"]["project_name"]
    tags = {name: f"{project}-hostops-{operation_id}-{name.lower()}:local" for name in selected}
    if (len(set(tags.values())) != len(tags)
            or not all(IMAGE_TAG.fullmatch(tag) for tag in tags.values())):
        raise HostRuntimeError("INVALID_IMAGE_TAG")
    return tags


def _write_override(work_dir: Path, tags: dict) -> Path:
    """Image-only override in the private work dir (JSON is valid YAML for Compose)."""
    path = Path(work_dir) / "compose-image-override.yaml"
    data = json.dumps({"services": {name: {"image": tag} for name, tag in tags.items()}},
                      sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise HostRuntimeError("STATE_UNAVAILABLE") from None
    return path


def _selected_state(action, item, pin, built) -> bool:
    if action == "compose.down":
        return item is None or item["state"] != "running"
    if item is None:
        return False
    running = item["state"] == "running"
    if action == "compose.apply":
        return (running and item["image_id"] == built and item["health"] in (None, "healthy")
                and (pin is None or item["id"] != pin["id"]))
    if action == "compose.up":
        return running and item["health"] != "unhealthy" and (pin is None or item["id"] == pin["id"])
    if action == "compose.restart":
        return running and item["id"] == pin["id"] and item["started_at"] != pin["started_at"]
    return item["id"] == pin["id"] and item["state"] in ("exited", "created", "dead")  # stop


def _ports_registered(target, name, ports) -> bool:
    allowed = {(item["host_ip"], str(item["host_port"]),
                f"{item['container_port']}/{item['protocol']}")
               for item in target["policy"]["ports"] if item["service"] == name}
    for key, bindings in ports.items():
        if bindings is None:
            continue
        if not isinstance(bindings, list) or not all(
                isinstance(binding, dict)
                and (_text(binding.get("HostIp")), _text(binding.get("HostPort")), key) in allowed
                for binding in bindings):
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def probe_readiness(url, *, timeout: float = 5) -> bool:
    """GET one registered loopback URL: no proxy, no redirect, bounded read; 2xx = ready."""
    if not isinstance(url, str) or not READINESS_URL.fullmatch(url):
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            response.read(MAX_READINESS_BODY)
            return 200 <= response.status < 300
    except (OSError, ValueError, http.client.HTTPException):
        return False


class ComposeActionAdapter:
    def __init__(self, runner, *, http_get=None):
        self.runner = runner
        self.http_get = http_get or probe_readiness

    # ----- commands ------------------------------------------------------------------
    def _compose(self, target, snapshot, arguments, timeout, override=None) -> bytes:
        compose = target["compose"]
        # Compose's default project directory (the first file's directory, inside the
        # snapshot): relative paths such as `context: ../..` resolve as written.
        project_dir = Path(snapshot) / os.path.dirname(compose["files"][0])
        argv = ["docker", "compose", "--project-name", compose["project_name"],
                "--env-file", "/dev/null", "--project-directory", str(project_dir)]
        for name in compose["files"]:
            argv += ["--file", str(Path(snapshot) / name)]
        if override is not None:
            argv += ["--file", str(override)]
        return self.runner([*argv, *arguments], cwd=Path(snapshot), timeout=timeout,
                           compose_env=dict(compose["env"]))

    def _docker(self, argv, cwd) -> bytes:
        return self.runner(argv, cwd=None if cwd is None else Path(cwd), timeout=INSPECT_TIMEOUT)

    def _render(self, target, snapshot, override=None) -> dict:
        return _json(self._compose(target, snapshot, ["config", "--format", "json"],
                                   RENDER_TIMEOUT, override), "WRONG_COMPOSE_CONFIG")

    def _check_raw(self, target, snapshot) -> None:
        check_raw_model(_json(self._compose(
            target, snapshot,
            ["config", "--format", "json", "--no-interpolate", "--no-env-resolution"],
            RENDER_TIMEOUT), "WRONG_COMPOSE_CONFIG"))

    def _containers(self, target, cwd) -> dict:
        """{registered service: [record, ...]} found by the project label and docker inspect."""
        project = target["compose"]["project_name"]
        services = target["compose"]["services"]
        ids = self._docker(["docker", "ps", "--all", "--no-trunc", "--filter",
                            f"label={PROJECT_LABEL}={project}", "--format", "{{.ID}}"],
                           cwd).decode("ascii", "replace").split()
        if (len(ids) > MAX_CONTAINERS or len(set(ids)) != len(ids)
                or not all(HEX64.fullmatch(cid) for cid in ids)):
            raise HostRuntimeError("HOST_RESPONSE_INVALID")
        found = {name: [] for name in services}
        if not ids:
            return found
        items = _json(self._docker(["docker", "inspect", "--type", "container", *ids], cwd))
        if (not isinstance(items, list) or len(items) != len(ids)
                or sorted(_text(item.get("Id")) or "" if isinstance(item, dict) else ""
                          for item in items) != sorted(ids)):
            raise HostRuntimeError("HOST_RESPONSE_INVALID")
        for item in items:
            parsed = _container(item, project, services)
            if parsed is not None:
                found[parsed[0]].append(parsed[1])
        return found

    def _observe(self, target, cwd) -> dict:
        observed = {}
        for name, items in self._containers(target, cwd).items():
            if len(items) > 1:
                raise HostRuntimeError("SCALE_UNSUPPORTED")
            observed[name] = items[0] if items else None
        return observed

    def _volumes(self, target, cwd) -> dict:
        """Registered volume name -> CreatedAt, or None when it does not exist yet."""
        names = sorted({item["name"] for item in target["policy"]["volumes"]})
        created = dict.fromkeys(names)
        if not names:
            return created
        argv = ["docker", "volume", "ls", "--quiet"]
        for name in names:  # Never list every host volume (output cap on busy hosts).
            argv += ["--filter", f"name={name}"]
        listed = set(self._docker(argv, cwd).decode("utf-8", "replace").split())
        present = [name for name in names if name in listed]
        if not present:
            return created
        items = _json(self._docker(["docker", "volume", "inspect", *present], cwd))
        if not isinstance(items, list) or len(items) != len(present):
            raise HostRuntimeError("HOST_RESPONSE_INVALID")
        for item in items:
            name = _text(item.get("Name")) if isinstance(item, dict) else None
            stamp = _text(item.get("CreatedAt")) if isinstance(item, dict) else None
            if name not in present or not stamp or not TIMESTAMP.fullmatch(stamp):
                raise HostRuntimeError("HOST_RESPONSE_INVALID")
            created[name] = stamp
        return created

    def _images(self, tags, cwd) -> dict:
        names = list(tags)
        items = _json(self._docker(["docker", "image", "inspect", *(tags[n] for n in names)], cwd))
        if not isinstance(items, list) or len(items) != len(names):
            raise HostRuntimeError("BUILD_IMAGE_UNKNOWN")
        built = {}
        for name, item in zip(names, items):
            image = _text(item.get("Id")) if isinstance(item, dict) else None
            repo_tags = item.get("RepoTags") if isinstance(item, dict) else None
            if (not image or not IMAGE_ID.fullmatch(image) or not isinstance(repo_tags, list)
                    or tags[name] not in repo_tags):
                raise HostRuntimeError("BUILD_IMAGE_UNKNOWN")
            built[name] = image
        return built

    def _state(self, target, action, selected, snapshot):
        """Render + policy + identity pin (read-only): preflight and pre-mutation check."""
        model = self._render(target, snapshot)
        check_policy(target, model, snapshot)
        self._check_raw(target, snapshot)
        check_secret_files(target, model)
        observed = self._observe(target, snapshot)
        if any(item is None and not (action in CREATING and name in selected)
               for name, item in observed.items()):
            raise HostRuntimeError("CONTAINER_MISSING")
        if action == "compose.exec" and observed[selected[0]]["state"] != "running":
            raise HostRuntimeError("SERVICE_NOT_RUNNING")
        identity = {"render": _digest(model),
                    "services": {name: _pin(item) for name, item in observed.items()},
                    "volumes": self._volumes(target, snapshot)}
        return model, identity, observed

    def _verify_override(self, target, snapshot, override, expected) -> None:
        if self._render(target, snapshot, override) != expected:
            raise HostRuntimeError("UNSAFE_IMAGE_OVERRIDE")

    # ----- adapter contract ----------------------------------------------------------
    def preflight(self, target, spec, context) -> dict:
        action = spec["action"]
        selected = _selected(target, action, spec["parameters"])
        model, identity, _ = self._state(target, action, selected, Path(context.snapshot))
        if action in BUILDING:
            if any(not model["services"][name].get("build") for name in selected):
                raise HostRuntimeError("SERVICE_NOT_BUILDABLE")
            _tags(target, selected, context.operation_id)
        return {"identity": identity, "verifications": _verifications(target, action, selected)}

    def execute(self, target, immutable, context, emit) -> dict:
        action, parameters = immutable["action"], immutable["parameters"]
        selected = _selected(target, action, parameters)
        snapshot = Path(context.snapshot)
        model, identity, observed = self._state(target, action, selected, snapshot)
        if identity != immutable["preflight"]:
            raise HostRuntimeError("STALE_PLAN")  # Before any mutation.
        emit("PRECHECK", "RENDER_VERIFIED", {"sha256": identity["render"]})
        emit("PRECHECK", "IDENTITY_VERIFIED", {
            "identity_verified": True,
            "count": sum(item is not None for item in observed.values())})
        result = {"snapshot": str(snapshot), "built": {}, "returncode": None,
                  "states": {name: item and item["state"] for name, item in observed.items()}}
        fields = {"services": selected, "count": len(selected)}
        if action in BUILDING:
            result["built"] = self._build(target, action, selected, snapshot, model,
                                          identity, context, emit)
        elif action == "compose.exec":
            result["returncode"] = self._exec(target, parameters["profile"], snapshot,
                                              context, emit)
        else:
            arguments, verb = SIMPLE[action]
            context.side_effects_started()
            emit("EXECUTE", f"COMPOSE_{verb}_STARTED", fields)
            self._compose(target, snapshot,
                          arguments + ([] if action == "compose.down" else selected),
                          COMMAND_TIMEOUT)
            emit("EXECUTE", f"COMPOSE_{verb}_COMPLETED", fields)
        return result

    def _build(self, target, action, selected, snapshot, model, pinned, context, emit) -> dict:
        tags = _tags(target, selected, context.operation_id)
        override = _write_override(context.work_dir, tags)
        expected = copy.deepcopy(model)
        for name, tag in tags.items():
            expected["services"][name]["image"] = tag
        self._verify_override(target, snapshot, override, expected)
        fields = {"services": selected, "count": len(selected)}
        emit("PRECHECK", "IMAGE_OVERRIDE_VERIFIED", fields)
        context.side_effects_started()
        emit("EXECUTE", "COMPOSE_BUILD_STARTED", fields)
        self._compose(target, snapshot, ["build", "--quiet", *selected], BUILD_TIMEOUT, override)
        built = self._images(tags, snapshot)
        for name in selected:
            emit("EXECUTE", "IMAGE_BUILT", {"service": name, "image_id": built[name]})
        if action == "compose.apply":
            # Operation tags, the override and every container generation are proven
            # again immediately before the only container mutation.
            if self._images(tags, snapshot) != built:
                raise HostRuntimeError("BUILD_IMAGE_CHANGED")
            self._verify_override(target, snapshot, override, expected)
            check_secret_files(target, expected)
            current = self._observe(target, snapshot)
            if {name: _pin(item) for name, item in current.items()} != pinned["services"]:
                raise HostRuntimeError("STALE_PLAN")
            self._compose(target, snapshot, [
                "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate",
                "--wait", "--wait-timeout", str(WAIT_TIMEOUT), *selected],
                WAIT_TIMEOUT + 60, override)
            emit("EXECUTE", "COMPOSE_APPLIED", fields)
        return built

    def _exec(self, target, profile_id, snapshot, context, emit) -> int:
        profile = target["exec_profiles"][profile_id]
        service = profile["service"]
        context.side_effects_started()
        emit("EXECUTE", "EXEC_STARTED", {"service": service})
        try:  # Output is discarded; only the exit status becomes an event.
            self._compose(target, snapshot, ["exec", "-T", "--workdir", profile["workdir"],
                                             service, *profile["argv"]], profile["timeout"])
        except HostCommandError as exc:
            if exc.returncode is None:
                raise
            emit("EXECUTE", "EXEC_FAILED", {"service": service, "returncode": exc.returncode})
            raise HostRuntimeError("EXEC_FAILED", "EXECUTE") from None
        emit("EXECUTE", "EXEC_COMPLETED", {"service": service, "returncode": 0})
        return 0

    def postcheck(self, target, immutable, result) -> dict:
        action = immutable["action"]
        pinned = immutable["preflight"]
        selected = _selected(target, action, immutable["parameters"])
        declared = _verifications(target, action, selected)
        snapshot = Path(result["snapshot"])
        passed = []
        model = self._render(target, snapshot)
        check_policy(target, model, snapshot)
        self._check_raw(target, snapshot)
        if _digest(model) != pinned["render"]:
            raise HostRuntimeError("STALE_PLAN")
        passed.append("RENDER_POLICY")
        observed = self._observe(target, snapshot)
        passed.append("IDENTITY_PINNED")
        if action in BUILDING:
            tags = _tags(target, selected, immutable["operation_id"])
            if self._images(tags, snapshot) != result["built"]:
                raise HostRuntimeError("BUILD_IMAGE_CHANGED")
            passed.append("BUILD_IMAGES_RECORDED")
        if "SELECTED_SERVICES_STATE" in declared:
            if not all(_selected_state(action, observed[name], pinned["services"][name],
                                       result["built"].get(name)) for name in selected):
                raise HostRuntimeError("SERVICE_STATE_UNVERIFIED")
            passed.append("SELECTED_SERVICES_STATE")
        if action == "compose.exec":
            if result["returncode"] != 0:
                raise HostRuntimeError("EXEC_FAILED")
            passed.append("EXEC_EXIT_ZERO")
        others = _others(target, action, selected)
        if others:
            if any(_pin(observed[name]) != pinned["services"][name]
                   or (observed[name] and observed[name]["state"]) != result["states"][name]
                   for name in others):
                raise HostRuntimeError("STALE_PLAN")  # Unexpected generation or state.
            passed.append("OTHERS_PRESERVED")
        volumes = self._volumes(target, snapshot)
        if any(created is not None and volumes.get(name) != created
               for name, created in pinned["volumes"].items()):
            raise HostRuntimeError("VOLUME_CHANGED")
        passed.append("VOLUMES_PRESERVED")
        if "READINESS" in declared:
            for name in selected:
                url = (target["readiness"] or {}).get(name)
                if url is not None and not self.http_get(url):
                    raise HostRuntimeError("SERVICE_NOT_READY")
            passed.append("READINESS")
        return {"verified": True, "verifications": passed, "services": selected,
                "verified_services": list(selected), "count": len(selected),
                "identity_verified": True, "image_verified": action in BUILDING,
                "health_verified": "READINESS" in declared}

    def inspect_target(self, target) -> dict:
        """Whitelisted read-only view: state, health, image ID, start time, labels."""
        project = target["compose"]["project_name"]
        services = {}
        for name, items in self._containers(target, None).items():
            item = items[0] if len(items) == 1 else None
            services[name] = {
                "present": bool(items), "count": len(items),
                "state": item and item["state"], "health": item and item["health"],
                "image_id": item and item["image_id"], "started_at": item and item["started_at"],
                "project": project if item else None, "service": name if item else None,
                "ports_registered": (_ports_registered(target, name, item["ports"])
                                     if item else None)}
        return {"target": target["id"], "compose_project": project, "services": services}
