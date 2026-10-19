"""Fail-closed configuration for the dedicated Toss test HostRuntime.

This module does not authorize generic Docker access. Installation and explicit
host configuration are separate from editing a writable repository.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile

TARGET = "tarororo-test"
PROJECT = "tossApp"
ORIGIN = "https://tarororo.private-apps.tossmini.com"
API_HEALTH = "http://127.0.0.1:3200/health/ready"
CONFIG_PATH = Path.home() / ".config/chatgpt-coding-mcp/host-runtime.json"
# The same fixed HTTPS origin used by the admin's human-approval trust check.
TRUSTED_APPROVAL_ORIGIN = "https://cantopsbuildserver.tail661d29.ts.net:8443"
REGISTERED_MODULES = {
    "host_registry", "host_actions", "host_process", "host_source_review",
    "host_registered_operations", "host_compose_actions", "host_registered_tools",
}
TARGET_CONFIG = {
    "project": PROJECT,
    "compose_project": TARGET,
    "api_container": "tarororo-test-api-1",
    "worker_container": "tarororo-test-worker-1",
    "db_container": "tarororo-test-db-1",
    "api_health_url": API_HEALTH,
    "expected_origin": ORIGIN,
    "expected_db_name": "tarororo_test",
    "allowed_component": "api",
}

TARGET_CONFIG_V2 = {k: v for k, v in TARGET_CONFIG.items() if k != "allowed_component"}
TARGET_CONFIG_V2["allowed_components"] = ["api", "worker", "migration"]
# Existing explicit 006-012 scope; distinct from the registered-target v3 envelope.
TARGET_CONFIG_V3 = {**TARGET_CONFIG_V2,
                    "allowed_components": ["api", "worker", "migration",
                                           "predecessor_migration"]}
MAX_LEGACY_CONFIG_BYTES = 8192
MAX_CONFIG_BYTES = 64 * 1024
V3_KEYS = {"version", "enabled", "legacy", "targets", "agent_profiles"}


def is_registered_config(config) -> bool:
    """Registered envelopes and fixed predecessor v3 have distinct exact key sets."""
    return (isinstance(config, dict) and set(config) == V3_KEYS
            and type(config.get("version")) is int and config["version"] == 3)


class HostRuntimeError(ValueError):
    """One sanitized error code, without host paths, output or secrets."""

    def __init__(self, code: str, phase: str = "VALIDATE"):
        super().__init__(code)
        self.code = code
        self.phase = phase


def private_dir(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise HostRuntimeError("UNSAFE_STATE_PATH")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise HostRuntimeError("STATE_UNAVAILABLE") from exc
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise HostRuntimeError("UNSAFE_STATE_PATH")
    return path


def private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HostRuntimeError("STATE_UNAVAILABLE") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or path.is_symlink()):
        raise HostRuntimeError("UNSAFE_STATE_FILE")


def _no_parent_links(path: Path) -> None:
    for parent in path.absolute().parents:
        if parent.is_symlink():
            raise HostRuntimeError("UNSAFE_STATE_PATH")


def _exact_legacy(obj) -> bool:
    return (isinstance(obj, dict) and set(obj) == {"version", "enabled", "targets"}
            and type(obj["version"]) is int and obj["version"] in (1, 2, 3)
            and type(obj["enabled"]) is bool and isinstance(obj["targets"], dict)
            and obj["targets"] == {TARGET: (TARGET_CONFIG if obj["version"] == 1
                                            else TARGET_CONFIG_V2 if obj["version"] == 2
                                            else TARGET_CONFIG_V3)})


def parse_config(payload: bytes) -> dict:
    """Exact v1/v2 (8 KiB) or fully validated v3 (64 KiB) except project membership."""
    duplicates = []

    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            duplicates.append(True)
        return value

    try:
        if len(payload) > MAX_CONFIG_BYTES:
            raise HostRuntimeError("INVALID_HOST_CONFIG")
        obj = json.loads(payload, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, HostRuntimeError):
            raise
        raise HostRuntimeError("INVALID_HOST_CONFIG") from exc
    if _exact_legacy(obj) and len(payload) <= MAX_LEGACY_CONFIG_BYTES:
        return obj
    if (isinstance(obj, dict) and set(obj) == V3_KEYS and not duplicates
            and type(obj["version"]) is int and obj["version"] == 3
            and type(obj["enabled"]) is bool
            and (obj["legacy"] is None or _exact_legacy(obj["legacy"]))
            and isinstance(obj["targets"], dict) and isinstance(obj["agent_profiles"], dict)):
        # Any v3 failure closes the legacy view too. Lazy: host_registry imports this module.
        try:
            from host_registry import validate_payload
        except ImportError as exc:
            raise HostRuntimeError("INVALID_HOST_CONFIG") from exc
        validate_payload(obj)
        return obj
    raise HostRuntimeError("INVALID_HOST_CONFIG")


def read_config(path: Path = CONFIG_PATH) -> dict | None:
    """Return the private config payload, or None when no file exists."""
    path = Path(path).absolute()
    _no_parent_links(path)
    try:
        private_file(path)
    except HostRuntimeError as exc:
        if exc.code == "STATE_UNAVAILABLE" and not path.exists() and not path.is_symlink():
            return None
        raise
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            payload = source.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise HostRuntimeError("INVALID_HOST_CONFIG") from exc
    return parse_config(payload)


@contextmanager
def config_write_lock(path: Path):
    """One advisory lock for all cooperating host config writers.

    Trusted operators must use this boundary too. Arbitrary same-UID manual
    replacement cannot be made atomic with a pathname/hash check on Linux.
    """
    path = Path(path).absolute()
    _no_parent_links(path)
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022):
        raise HostRuntimeError('UNSAFE_STATE_PATH')
    lock = path.with_name('.' + path.name + '.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise HostRuntimeError('UNSAFE_STATE_FILE')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def save_config_cas(path: Path, candidate: dict, *, expected_sha256: str | None):
    """Revalidate and replace under the same shared writer lock; never merge drift."""
    payload = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()
    parse_config(payload)
    path = Path(path).absolute()
    with config_write_lock(path):
        try:
            private_file(path)
        except HostRuntimeError:
            if expected_sha256 is not None or path.exists() or path.is_symlink():
                raise HostRuntimeError('HOST_CONFIG_CHANGED') from None
            actual = None
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as source:
                before = source.read(MAX_CONFIG_BYTES + 1)
            parse_config(before)
            actual = hashlib.sha256(before).hexdigest()
        if actual != expected_sha256:
            raise HostRuntimeError('HOST_CONFIG_CHANGED')
        descriptor, name = tempfile.mkstemp(prefix='.host-runtime-', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def legacy_payload(config: dict | None) -> dict | None:
    """The fixed legacy scope; disabled registered envelopes close their legacy view."""
    if is_registered_config(config):
        return config["legacy"] if config["enabled"] else None
    return config


def load_config(path: Path = CONFIG_PATH) -> bool:
    """Return enabled only for an exact user-owned, private opt-in config."""
    legacy = legacy_payload(read_config(path))
    return legacy is not None and legacy["enabled"]


def extended_enabled(path: Path = CONFIG_PATH) -> bool:
    # A valid v1 file remains read-only/API-only; only locally approved v2
    # explicitly permits Worker or migration plans.
    legacy = legacy_payload(read_config(path))
    return legacy is not None and legacy["enabled"] and legacy["version"] in (2, 3)


def predecessor_enabled(path: Path = CONFIG_PATH) -> bool:
    """Only the exact fixed predecessor opt-in grants 006-012, never envelope version."""
    legacy = legacy_payload(read_config(path))
    return legacy is not None and legacy["enabled"] and legacy["version"] == 3

def validate_scope(project: str, target: str, projects: dict) -> Path:
    if project != PROJECT or target != TARGET or project not in projects:
        raise HostRuntimeError("UNSUPPORTED_SCOPE")
    root = Path(projects[PROJECT]).resolve(strict=True)
    if not root.is_dir():
        raise HostRuntimeError("UNSUPPORTED_SCOPE")
    return root


def validate_state_root(path: Path, projects: dict) -> Path:
    path = Path(path).absolute()
    _no_parent_links(path)
    for root in projects.values():
        resolved = Path(root).resolve(strict=True)
        if path.is_relative_to(resolved) or resolved.is_relative_to(path):
            raise HostRuntimeError("UNSAFE_STATE_PATH")
    return private_dir(path)
