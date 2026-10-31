"""Owner-controlled workspace policy loading and path decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping

from workspace_paths import (
    WorkspaceError,
    is_sensitive,
    open_directory,
    read_owner_file,
    validate_private_directory,
)


VERSION = 1
MAX_POLICY_BYTES = 1024 * 1024
DEFAULT_BASE_ROOT = Path("/home/jhw/ai/opencode/projects")
_POLICY_FIELDS = frozenset({"version", "base_root", "artifacts", "protected_paths"})
_REQUIRED_POLICY_FIELDS = frozenset({"version", "base_root", "artifacts"})
_ARTIFACT_FIELDS = frozenset({
    "project", "path", "kind", "operations", "fields", "recursive", "max_file_bytes",
})
_OPERATIONS = frozenset({"list", "read", "write"})
_ARTIFACT_OPERATIONS = frozenset({"list", "read"})


@dataclass(frozen=True)
class PolicySnapshot:
    version: int
    digest: str
    base_root: Path
    artifacts: tuple[Mapping, ...]
    protected_paths: tuple[Path, ...]
    _projects: Mapping[str, Path] = field(repr=False)


def _default_config_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_root / "chatgpt-coding-mcp" / "workspace-policy.json"


def _normal_absolute(value, code="INVALID_POLICY") -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkspaceError(code)
    path = Path(value)
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise WorkspaceError(code)
    return path


def _relative(value) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= 4096 or not value.isprintable()
            or value.startswith("/") or value.endswith("/") or "\\" in value):
        raise WorkspaceError("INVALID_POLICY")
    if any(part in ("", ".", "..") for part in value.split("/")) or is_sensitive(value):
        raise WorkspaceError("INVALID_POLICY")
    return value


def _canonical_directory(path: Path, code: str) -> Path:
    try:
        descriptor = open_directory(path)
    except WorkspaceError:
        raise WorkspaceError(code) from None
    try:
        observed = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not observed.is_absolute() or str(observed).endswith(" (deleted)"):
            raise WorkspaceError(code)
        return observed
    except OSError:
        raise WorkspaceError(code) from None
    finally:
        os.close(descriptor)


def _parse_policy(data: bytes) -> dict:
    def exact_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise WorkspaceError("INVALID_POLICY")
        return result

    try:
        value = json.loads(data, object_pairs_hook=exact_object)
    except WorkspaceError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise WorkspaceError("INVALID_POLICY") from None
    if (not isinstance(value, dict) or not _REQUIRED_POLICY_FIELDS <= set(value)
            or not set(value) <= _POLICY_FIELDS):
        raise WorkspaceError("INVALID_POLICY")
    return value


def _string_tuple(value, *, allow_empty: bool) -> tuple[str, ...]:
    if (not isinstance(value, list) or (not allow_empty and not value)
            or any(not isinstance(item, str) or not item or not item.isprintable()
                   or len(item) > 256 for item in value)
            or len(set(value)) != len(value)):
        raise WorkspaceError("INVALID_POLICY")
    return tuple(value)


def _artifact(value, projects: Mapping[str, Path]) -> Mapping:
    if not isinstance(value, dict) or set(value) != _ARTIFACT_FIELDS:
        raise WorkspaceError("INVALID_POLICY")
    project = value["project"]
    if not isinstance(project, str) or project not in projects:
        raise WorkspaceError("INVALID_POLICY")
    relative = _relative(value["path"])
    kind = value["kind"]
    if kind not in ("file", "directory"):
        raise WorkspaceError("INVALID_POLICY")
    operations = _string_tuple(value["operations"], allow_empty=False)
    if not set(operations) <= _ARTIFACT_OPERATIONS:
        raise WorkspaceError("INVALID_POLICY")
    if kind == "file" and ("list" in operations or value["recursive"] is not False):
        raise WorkspaceError("INVALID_POLICY")
    if type(value["recursive"]) is not bool:
        raise WorkspaceError("INVALID_POLICY")
    fields = _string_tuple(value["fields"], allow_empty=True)
    limit = value["max_file_bytes"]
    if type(limit) is not int or not 0 < limit <= MAX_POLICY_BYTES:
        raise WorkspaceError("INVALID_POLICY")
    return MappingProxyType({
        "project": project,
        "path": relative,
        "kind": kind,
        "operations": operations,
        "fields": fields,
        "recursive": value["recursive"],
        "max_file_bytes": limit,
    })


def _validate_artifact_namespaces(artifacts: tuple[Mapping, ...]) -> None:
    """Reject duplicate or nested artifact namespaces within one project."""
    seen = set()
    directories: dict[str, set[str]] = {}
    ordered = sorted(
        artifacts,
        key=lambda rule: (rule["project"], rule["path"].count("/"), rule["path"]),
    )
    for rule in ordered:
        key = (rule["project"], rule["path"])
        if key in seen:
            raise WorkspaceError("INVALID_POLICY")
        seen.add(key)
        ancestors = directories.setdefault(rule["project"], set())
        parts = rule["path"].split("/")
        if any("/".join(parts[:depth]) in ancestors for depth in range(1, len(parts))):
            raise WorkspaceError("INVALID_POLICY")
        if rule["kind"] == "directory":
            ancestors.add(rule["path"])


class WorkspacePolicy:
    """Load immutable decisions from an owner-only policy file."""

    def __init__(self, projects, config_path=None):
        if not isinstance(projects, dict):
            raise WorkspaceError("INVALID_PROJECTS")
        self.projects = dict(projects)
        self.config_path = Path(config_path) if config_path is not None else _default_config_path()

    def load(self, required: bool = False) -> PolicySnapshot | None:
        if type(required) is not bool:
            raise WorkspaceError("INVALID_POLICY_REQUEST")
        declared_projects: dict[str, Path] = {}
        for name, root in self.projects.items():
            root_path = Path(root)
            if (not isinstance(name, str) or not name or not root_path.is_absolute()
                    or str(root_path) != os.path.normpath(str(root_path))):
                raise WorkspaceError("INVALID_PROJECTS")
            declared_projects[name] = root_path
        if (not self.config_path.is_absolute()
                or str(self.config_path) != os.path.normpath(str(self.config_path))):
            raise WorkspaceError("UNSAFE_POLICY_LOCATION")
        if any(self.config_path == root or self.config_path.is_relative_to(root)
               for root in declared_projects.values()):
            raise WorkspaceError("UNSAFE_POLICY_LOCATION")
        try:
            os.lstat(self.config_path)
        except FileNotFoundError:
            current = Path("/")
            for component in self.config_path.parent.parts[1:]:
                current /= component
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    break
                except OSError:
                    raise WorkspaceError("UNSAFE_POLICY_DIRECTORY") from None
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise WorkspaceError("UNSAFE_POLICY_DIRECTORY")
            else:
                try:
                    validate_private_directory(self.config_path.parent)
                except WorkspaceError:
                    raise WorkspaceError("UNSAFE_POLICY_DIRECTORY") from None
            if required:
                raise WorkspaceError("POLICY_MISSING") from None
            return None
        except OSError:
            raise WorkspaceError("UNSAFE_POLICY_FILE") from None

        project_paths: dict[str, Path] = {}
        for name, root_path in declared_projects.items():
            project_paths[name] = _canonical_directory(root_path, "UNSAFE_PROJECT")

        try:
            policy_file = read_owner_file(
                self.config_path,
                max_bytes=MAX_POLICY_BYTES,
                forbidden_roots=project_paths.values(),
            )
        except WorkspaceError as exc:
            translations = {
                "UNSAFE_OWNER_LOCATION": "UNSAFE_POLICY_LOCATION",
                "UNSAFE_OWNER_DIRECTORY": "UNSAFE_POLICY_DIRECTORY",
                "UNSAFE_OWNER_FILE": "UNSAFE_POLICY_FILE",
                "OWNER_FILE_MISSING": "POLICY_MISSING",
                "FILE_TOO_LARGE": "INVALID_POLICY",
                "STALE_FILE": "UNSAFE_POLICY_FILE",
            }
            raise WorkspaceError(translations.get(exc.code, exc.code)) from None

        document = _parse_policy(policy_file.data)
        if type(document["version"]) is not int or document["version"] != VERSION:
            raise WorkspaceError("INVALID_POLICY")
        base_root = _canonical_directory(
            _normal_absolute(document["base_root"]), "INVALID_POLICY"
        )
        for root in project_paths.values():
            # The selected base itself is not a project. Registered projects may
            # live elsewhere, but their paths remain grant-gated by check().
            if root == base_root:
                raise WorkspaceError("PROJECT_OUTSIDE_BASE")

        raw_artifacts = document["artifacts"]
        if not isinstance(raw_artifacts, list):
            raise WorkspaceError("INVALID_POLICY")
        artifacts = tuple(_artifact(value, project_paths) for value in raw_artifacts)
        _validate_artifact_namespaces(artifacts)

        protected = document.get("protected_paths", [])
        if not isinstance(protected, list):
            raise WorkspaceError("INVALID_POLICY")
        protected_paths = tuple(_normal_absolute(value) for value in protected)
        config_parent = self.config_path.parent
        if config_parent not in protected_paths:
            protected_paths += (config_parent,)
        return PolicySnapshot(
            VERSION,
            policy_file.sha256,
            base_root,
            artifacts,
            protected_paths,
            MappingProxyType(project_paths),
        )

    def check(self, project, path, operation, *, snapshot=None) -> Path:
        snapshot = self.load(required=True) if snapshot is None else snapshot
        if not isinstance(snapshot, PolicySnapshot):
            raise WorkspaceError("INVALID_POLICY_SNAPSHOT")
        if not isinstance(project, str) or project not in snapshot._projects:
            raise WorkspaceError("UNKNOWN_PROJECT")
        if operation not in _OPERATIONS:
            raise WorkspaceError("INVALID_OPERATION")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise WorkspaceError("INVALID_PATH")

        requested = Path(path)
        if requested.is_absolute():
            if str(requested) != os.path.normpath(str(requested)):
                raise WorkspaceError("INVALID_PATH")
            candidate = requested
            sensitive_value = "/".join(requested.parts[1:])
        else:
            if path == "." and operation == "list":
                candidate = snapshot._projects[project]
                sensitive_value = ""
            else:
                try:
                    relative = _relative(path)
                except WorkspaceError:
                    if is_sensitive(path):
                        raise WorkspaceError("SENSITIVE_PATH") from None
                    raise WorkspaceError("INVALID_PATH") from None
                candidate = snapshot._projects[project] / relative
                sensitive_value = relative
        if is_sensitive(sensitive_value):
            raise WorkspaceError("SENSITIVE_PATH")
        for protected in snapshot.protected_paths:
            if candidate == protected or candidate.is_relative_to(protected):
                raise WorkspaceError("PROTECTED_PATH")
        project_root = snapshot._projects[project]
        if (candidate == snapshot.base_root or not candidate.is_relative_to(snapshot.base_root)
                or not candidate.is_relative_to(project_root)
                or (candidate == project_root and operation != "list")):
            raise WorkspaceError("WORKSPACE_SCOPE_REQUIRED")
        return candidate

    def artifact_rule(self, snapshot, project, relative_path) -> dict:
        if not isinstance(snapshot, PolicySnapshot):
            raise WorkspaceError("INVALID_POLICY_SNAPSHOT")
        if not isinstance(project, str) or project not in snapshot._projects:
            raise WorkspaceError("UNKNOWN_PROJECT")
        try:
            relative = _relative(relative_path)
        except WorkspaceError:
            raise WorkspaceError("ARTIFACT_NOT_REGISTERED") from None
        matches = []
        for rule in snapshot.artifacts:
            if rule["project"] != project:
                continue
            if rule["kind"] == "file" and relative == rule["path"]:
                matches.append(rule)
            elif rule["kind"] == "directory":
                prefix = rule["path"] + "/"
                if (relative == rule["path"]
                        or (relative.startswith(prefix)
                            and (rule["recursive"]
                                 or "/" not in relative[len(prefix):]))):
                    matches.append(rule)
        if len(matches) > 1:
            raise WorkspaceError("ARTIFACT_AMBIGUOUS")
        if not matches:
            if self.artifact_path(snapshot, project, relative):
                raise WorkspaceError("WORKSPACE_ARTIFACT_DENIED")
            raise WorkspaceError("ARTIFACT_NOT_REGISTERED")
        return dict(matches[0])

    def artifact_path(self, snapshot, project, relative_path) -> bool:
        """Return whether a path belongs to any reserved artifact namespace."""
        if not isinstance(snapshot, PolicySnapshot):
            raise WorkspaceError("INVALID_POLICY_SNAPSHOT")
        if not isinstance(project, str) or project not in snapshot._projects:
            raise WorkspaceError("UNKNOWN_PROJECT")
        try:
            relative = _relative(relative_path)
        except WorkspaceError:
            return False
        for rule in snapshot.artifacts:
            if rule["project"] != project:
                continue
            if relative == rule["path"]:
                return True
            if rule["kind"] == "directory" and relative.startswith(rule["path"] + "/"):
                return True
        return False
