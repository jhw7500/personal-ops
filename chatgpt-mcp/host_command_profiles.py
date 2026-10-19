"""Strict, host-owned command profiles for a future approved host runner.

This module NEVER executes a command.  A ChatGPT request supplies only a
(project, profile_id); argv/cwd/program/content hashes come exclusively from
an owner-controlled config file outside all registered repositories.

A normal model task and an existing Compose/DB approval cannot authorize a
profile execution.  A separate one-use host-command approval is required
before any runner may consume a frozen profile.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from host_runtime_config import HostRuntimeError

VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PROFILES = 32
MAX_INPUTS = 128
MAX_ARGV = 32
MAX_TIMEOUT_SECONDS = 3600
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EFFECTS = frozenset({"read_only", "build", "external_publish"})
PROFILE_FIELDS = frozenset({
    "project", "label", "executable", "executable_sha256", "argv", "cwd",
    "timeout_seconds", "effect", "inputs",
})
FORBIDDEN_PATH_PARTS = frozenset({".git", ".env", ".ssh", ".aws", ".config", ".local"})
SHELL_NAMES = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh", "python", "python3", "node"})
_METADATA_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
    "st_size", "st_mtime_ns", "st_ctime_ns",
)


def _invalid() -> None:
    raise HostRuntimeError("INVALID_HOST_COMMAND_CONFIG")


def _safe_relpath(text: Any, *, dot: bool = False) -> bool:
    if text == ".":
        return dot
    if not isinstance(text, str) or not 0 < len(text) <= 512 or not text.isprintable():
        return False
    if text.startswith("/") or "\\" in text:
        return False
    parts = text.split("/")
    return not any(
        part in ("", ".", "..") or part in FORBIDDEN_PATH_PARTS or part.startswith(".env.")
        for part in parts
    )


def _file_digest(path: Path, *, executable: bool = False,
                 forbidden_roots: tuple[Path, ...] = ()) -> str:
    """Reject linked ancestors and hash one stable, safely owned regular fd."""
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        _invalid()
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                _invalid()
        except OSError:
            raise HostRuntimeError("HOST_COMMAND_SOURCE_UNAVAILABLE") from None
    descriptors: list[int] = []
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(parent)
        for component in path.parts[1:-1]:
            parent = os.open(component,
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        descriptors.append(fd)
        opened_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if (not opened_path.is_absolute() or str(opened_path).endswith(" (deleted)")
                or any(opened_path.is_relative_to(root) for root in forbidden_roots)):
            _invalid()
        prior = os.fstat(fd)
        if not stat.S_ISREG(prior.st_mode) or prior.st_uid not in (0, os.getuid()):
            _invalid()
        if prior.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID):
            _invalid()
        if executable and not prior.st_mode & stat.S_IXUSR:
            _invalid()
        if not 0 <= prior.st_size <= MAX_FILE_BYTES:
            _invalid()
        digest = hashlib.sha256()
        amount = 0
        while amount <= MAX_FILE_BYTES:
            block = os.read(fd, min(1 << 20, MAX_FILE_BYTES + 1 - amount))
            if not block:
                break
            amount += len(block)
            digest.update(block)
        actual = os.fstat(fd)
        if (amount > MAX_FILE_BYTES
                or tuple(getattr(actual, field) for field in _METADATA_FIELDS)
                != tuple(getattr(prior, field) for field in _METADATA_FIELDS)
                or amount != actual.st_size
                or Path(os.readlink(f"/proc/self/fd/{fd}")) != opened_path):
            raise HostRuntimeError("HOST_COMMAND_SOURCE_UNAVAILABLE")
        return digest.hexdigest()
    except HostRuntimeError:
        raise
    except (OSError, ValueError):
        raise HostRuntimeError("HOST_COMMAND_SOURCE_UNAVAILABLE") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _repo_input_digest(root: Path, relative: str) -> str:
    """Open every path component through directory fds without following links.

    This prevents an untrusted checkout from redirecting an approved artifact
    through a symlinked ancestor between validation and opening.
    """
    if not _safe_relpath(relative):
        _invalid()
    fds: list[int] = []
    try:
        fd = os.open(root.resolve(strict=True),
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(fd)
        parts = relative.split("/")
        for name in parts[:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=fd)
            fds.append(fd)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        fds.append(leaf)
        info = os.fstat(leaf)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                or info.st_size > MAX_FILE_BYTES):
            _invalid()
        digest = hashlib.sha256()
        while block := os.read(leaf, 1 << 20):
            digest.update(block)
        return digest.hexdigest()
    except HostRuntimeError:
        raise
    except OSError:
        raise HostRuntimeError("INVALID_HOST_COMMAND_CONFIG") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _private_config(path: Path) -> tuple[dict, str]:
    try:
        prior = path.lstat()
        if (not stat.S_ISREG(prior.st_mode) or prior.st_uid != os.getuid()
                or stat.S_IMODE(prior.st_mode) != 0o600
                or not 0 < prior.st_size <= MAX_CONFIG_BYTES):
            _invalid()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            observed = os.fstat(fd)
            if (observed.st_ino, observed.st_dev, observed.st_mode, observed.st_size) != (
                    prior.st_ino, prior.st_dev, prior.st_mode, prior.st_size):
                _invalid()
            data = os.read(fd, MAX_CONFIG_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        raise HostRuntimeError("HOST_COMMAND_CONFIG_UNAVAILABLE") from None
    def exact_pairs(pairs):
        values = dict(pairs)
        if len(values) != len(pairs):
            _invalid()
        return values

    try:
        obj = json.loads(data, object_pairs_hook=exact_pairs)
    except (ValueError, UnicodeError, TypeError):
        _invalid()
    if not isinstance(obj, dict) or set(obj) != {"version", "enabled", "profiles"}:
        _invalid()
    if type(obj["version"]) is not int or obj["version"] != VERSION or type(obj["enabled"]) is not bool:
        _invalid()
    profiles = obj["profiles"]
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        _invalid()
    return obj, hashlib.sha256(data).hexdigest()


def _secure_project_dir(root: Path, relative: str) -> Path:
    if not _safe_relpath(relative, dot=True):
        _invalid()
    # Refuse intervening links and require the resulting directory to stay under
    # the registered checkout.  Caller never chooses root from untrusted text.
    try:
        if not root.is_dir():
            _invalid()
        candidate = root
        if relative != ".":
            for name in relative.split("/"):
                candidate = candidate / name
                info = candidate.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    _invalid()
        if not candidate.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            _invalid()
        return candidate
    except OSError:
        raise HostRuntimeError("HOST_COMMAND_SOURCE_UNAVAILABLE") from None


def _validate_profile(profile: Any, projects: dict[str, Path]) -> None:
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
        _invalid()
    if not isinstance(profile["project"], str) or profile["project"] not in projects:
        _invalid()
    label = profile["label"]
    if not isinstance(label, str) or not 1 <= len(label) <= 100 or not label.isprintable():
        _invalid()
    executable = profile["executable"]
    if (not isinstance(executable, str) or not 1 < len(executable) <= 1024
            or not executable.isprintable() or not Path(executable).is_absolute()
            or ".." in Path(executable).parts):
        _invalid()
    if not isinstance(profile["executable_sha256"], str) or not SHA256.fullmatch(profile["executable_sha256"]):
        _invalid()
    argv = profile["argv"]
    if (not isinstance(argv, list) or len(argv) > MAX_ARGV
            or any(not isinstance(arg, str) or not 0 < len(arg) <= 1024
                   or not arg.isprintable() for arg in argv)):
        _invalid()
    # Named exact scripts are fine, but no command-interpreter evaluation flags.
    if Path(executable).name in SHELL_NAMES and any(arg in ("-c", "-lc", "-ec", "--eval") for arg in argv):
        _invalid()
    if not _safe_relpath(profile["cwd"], dot=True):
        _invalid()
    if type(profile["timeout_seconds"]) is not int or not 1 <= profile["timeout_seconds"] <= MAX_TIMEOUT_SECONDS:
        _invalid()
    if profile["effect"] not in EFFECTS:
        _invalid()
    inputs = profile["inputs"]
    if (not isinstance(inputs, dict) or len(inputs) > MAX_INPUTS
            or any(not _safe_relpath(name) or not isinstance(digest, str)
                   or not SHA256.fullmatch(digest) for name, digest in inputs.items())):
        _invalid()
    # Mutating or externally publishing commands must pin at least one input;
    # this protects against accidentally deploying a different bundle/script.
    if profile["effect"] != "read_only" and not inputs:
        _invalid()


class HostCommandProfiles:
    """Resolve exact profiles at prepare and re-verify at execution time."""

    def __init__(self, projects: dict[str, Path], config_path: Path):
        self.projects = {name: Path(path).resolve(strict=False)
                         for name, path in projects.items()}
        self.config_path = Path(config_path)

    def _load(self) -> tuple[dict, str]:
        try:
            config_parent = self.config_path.resolve().parent
            # A chat-writable repository must never become the policy source.
            for root in self.projects.values():
                if config_parent.is_relative_to(root.resolve()):
                    _invalid()
        except OSError:
            raise HostRuntimeError("HOST_COMMAND_CONFIG_UNAVAILABLE") from None
        doc, digest = _private_config(self.config_path)
        for name, profile in doc["profiles"].items():
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                _invalid()
            _validate_profile(profile, self.projects)
        return doc, digest

    def list_profiles(self, project: str) -> dict:
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        doc, _ = self._load()
        return {"project": project, "enabled": doc["enabled"],
                "profiles": [
                    {"profile_id": key, "label": v["label"], "effect": v["effect"],
                     "timeout_seconds": v["timeout_seconds"]}
                    for key, v in sorted(doc["profiles"].items()) if v["project"] == project
                ] if doc["enabled"] else []}

    def freeze(self, project: str, profile_id: str) -> dict:
        if project not in self.projects or not isinstance(profile_id, str) or not IDENTIFIER.fullmatch(profile_id):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        doc, config_digest = self._load()
        if not doc["enabled"]:
            raise HostRuntimeError("HOST_COMMAND_DISABLED")
        profile = doc["profiles"].get(profile_id)
        if profile is None or profile["project"] != project:
            raise HostRuntimeError("HOST_COMMAND_PROFILE_NOT_FOUND")
        executable = Path(profile["executable"])
        if _file_digest(executable, executable=True,
                        forbidden_roots=tuple(self.projects.values())) != profile["executable_sha256"]:
            raise HostRuntimeError("HOST_COMMAND_EXECUTABLE_CHANGED")
        root = self.projects[project]
        cwd = _secure_project_dir(root, profile["cwd"])
        for name, digest in profile["inputs"].items():
            if _repo_input_digest(root, name) != digest:
                raise HostRuntimeError("HOST_COMMAND_INPUT_CHANGED")
        frozen = {
            "profile_id": profile_id, "project": project, "label": profile["label"],
            "project_root": str(root.resolve(strict=True)),
            "executable": str(executable), "executable_sha256": profile["executable_sha256"],
            "argv": list(profile["argv"]), "cwd": str(cwd),
            "cwd_relative": profile["cwd"], "timeout_seconds": profile["timeout_seconds"],
            "effect": profile["effect"], "inputs": dict(profile["inputs"]),
            "config_digest": config_digest,
        }
        # Stored privately in an approval-bound plan.  Never return this raw
        # mapping through a chat tool (absolute paths, argv may be sensitive).
        return frozen

    def verify_frozen(self, frozen: dict) -> None:
        if not isinstance(frozen, dict) or "project" not in frozen or "profile_id" not in frozen:
            raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN")
        latest = self.freeze(frozen["project"], frozen["profile_id"])
        if latest != frozen:
            raise HostRuntimeError("HOST_COMMAND_PLAN_STALE")


def public_profile_plan(frozen: dict) -> dict:
    """Metadata safe for approval UI; raw argv and host paths stay private."""
    return {
        "project": frozen["project"], "profile_id": frozen["profile_id"],
        "label": frozen["label"], "effect": frozen["effect"],
        "timeout_seconds": frozen["timeout_seconds"], "input_count": len(frozen["inputs"]),
        "config_digest": frozen["config_digest"],
    }
