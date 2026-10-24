"""Owner-approved, expiring authority for trusted host-user execution.

The project selects command context.  A grant applies to callers of this MCP
installation and is not a filesystem sandbox or an authenticated caller claim.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket

from host_runtime_config import HostRuntimeError, private_dir, private_file, validate_state_root
from host_runtime_operations import (
    VERIFIED_RUNTIME_FILES, _lock, _read_private_json, _save_private_json, _verified_installation,
)
from host_access_installation import InstallationGrantState
from workspace_control import (
    HOST_ACCESS_CAPABILITY_GENERATION, WorkspaceControlError,
    activation_admission, verify_launch,
)
from workspace_host import publication_lane
from workspace_paths import WorkspaceError


DEFAULT_DURATION_SECONDS = 30 * 24 * 60 * 60
MAX_DURATION_SECONDS = 30 * 24 * 60 * 60
REQUEST_TTL_SECONDS = 7 * 24 * 60 * 60
SCOPE = "installation"
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_RUNTIME_ID = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
# Only the read-only workroom renderer is independent of execution authority.
# Approval/login UI and all backend/unknown files remain bound to the grant.
_DISPLAY_ONLY_FILES = frozenset({
    "work-dashboard/model.js", "work-dashboard/motion.js", "work-dashboard/pixels.js",
    "work-dashboard/visualizer.js", "work-dashboard/visualizer.css",
})
_AUTHORITY_ID_DOMAIN = b"personal-ops/host-access/authority-v1\0"
_WORKSPACE_ID_DOMAIN = b"personal-ops/host-access/workspace-v1\0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value, *, code="HOST_ACCESS_STATE_INVALID") -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed
    except (TypeError, ValueError, AttributeError) as exc:
        raise HostRuntimeError(code) from exc


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip().lower()
    except OSError as exc:
        raise HostRuntimeError("BOOT_ID_UNAVAILABLE") from exc
    if _BOOT_ID.fullmatch(value) is None:
        raise HostRuntimeError("BOOT_ID_UNAVAILABLE")
    return value


def _installed_runtime_identity() -> str | None:
    """Bind grants to verified authority code, independent of workroom assets."""
    if not _verified_installation():
        return None
    folder = Path(__file__).resolve().parent
    manifest_path = folder / "manifest.json"
    try:
        manifest = _read_private_json(manifest_path)
        if not set(VERIFIED_RUNTIME_FILES).issubset(manifest):
            return None
        # Verify excluded assets too. A UI-only release may retain a grant;
        # changing installed bytes without a matching manifest never may.
        for name, expected in manifest.items():
            if (not isinstance(name, str) or _MANIFEST_PATH.fullmatch(name) is None
                    or any(part in {".", ".."} for part in name.split("/"))
                    or not isinstance(expected, str) or _RUNTIME_ID.fullmatch(expected) is None):
                return None
            path = folder / name
            if not path.resolve().is_relative_to(folder):
                return None
            private_file(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return None
    except (OSError, RuntimeError, UnicodeError, ValueError, HostRuntimeError):
        return None
    authority = {name: digest for name, digest in manifest.items() if name not in _DISPLAY_ONLY_FILES}
    payload = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(_AUTHORITY_ID_DOMAIN + payload).hexdigest()


def _identity_label(value, fallback: str, limit: int) -> str:
    text = "".join(character for character in str(value)
                   if character.isprintable() and character not in "\r\n")
    text = text.strip()
    return (text or fallback)[:limit]


def _host_identity() -> dict:
    """Return bounded, read-only facts about this server process's host authority."""
    uid = os.getuid()
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown-host"
    try:
        user = pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        user = f"uid:{uid}"
    try:
        gids = set(os.getgroups())
    except OSError:
        gids = set()
    gids.add(os.getgid())
    groups = []
    for gid in gids:
        try:
            name = grp.getgrgid(gid).gr_name
        except (KeyError, OSError):
            name = f"gid:{gid}"
        groups.append(_identity_label(name, f"gid:{gid}", 128))
    return {
        "hostname": _identity_label(hostname, "unknown-host", 255),
        "user": _identity_label(user, f"uid:{uid}", 128),
        "uid": uid,
        "groups": sorted(groups),
        "filesystem_isolated": False,
        "effective_access": "host_user_filesystem_network_groups",
        "passwordless_sudo_verified": False,
    }


class HostAccess:
    """Private grant state shared by MCP tools and the authenticated owner web."""

    def __init__(self, projects, state_dir, *, trusted_runtime=None, workspace=None):
        self.projects = {name: Path(root).resolve(strict=True) for name, root in projects.items()}
        if not self.projects or any(not root.is_dir() for root in self.projects.values()):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        overall_state = validate_state_root(Path(state_dir), self.projects)
        self.state_dir = private_dir(overall_state / "host-access")
        self.records_dir = private_dir(self.state_dir / "records")
        self.locks_dir = private_dir(self.state_dir / "locks")
        self.installation = InstallationGrantState(self.state_dir)
        self._runtime_source = trusted_runtime
        try:
            from workspace_host import resolve_workspace
            self.workspace = resolve_workspace(self.projects, workspace)
        except HostRuntimeError as exc:
            raise HostRuntimeError("HOST_ACCESS_WORKSPACE_INVALID") from exc

    def _project_root(self, project) -> Path:
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        try:
            root = self.projects[project].resolve(strict=True)
        except OSError as exc:
            raise HostRuntimeError("UNSUPPORTED_SCOPE") from exc
        if not root.is_dir():
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return root

    def _runtime_identity(self) -> str:
        source = self._runtime_source
        value = (_installed_runtime_identity() if source is None
                 else source() if callable(source) else "test-runtime" if source is True else source)
        if value == "test-runtime":
            value = hashlib.sha256(value.encode()).hexdigest()
        if not isinstance(value, str) or _RUNTIME_ID.fullmatch(value) is None:
            raise HostRuntimeError("UNSAFE_RUNTIME")
        if self.workspace is not None:
            try:
                if self.workspace.enabled:
                    snapshot = self.workspace.policy.load(required=True)
                    binding = self.workspace.authority.binding(snapshot.digest)
                    if (set(binding) != {"installation_id", "authority_epoch", "policy_digest"}
                            or _HEX_ID.fullmatch(binding.get("installation_id", "")) is None
                            or type(binding.get("authority_epoch")) is not int
                            or binding["authority_epoch"] < 1
                            or _RUNTIME_ID.fullmatch(binding.get("policy_digest", "")) is None):
                        raise HostRuntimeError("HOST_ACCESS_WORKSPACE_INVALID")
                    payload = json.dumps(
                        {"runtime_identity": value, **binding},
                        sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                    value = hashlib.sha256(_WORKSPACE_ID_DOMAIN + payload).hexdigest()
            except HostRuntimeError:
                raise
            except Exception as exc:
                raise HostRuntimeError("HOST_ACCESS_WORKSPACE_INVALID") from exc
        return value

    @staticmethod
    def _key(project: str) -> str:
        return hashlib.sha256(project.encode()).hexdigest()

    def _record_path(self, project: str) -> Path:
        return self.records_dir / (self._key(project) + ".json")

    def _lock_path(self, project: str) -> Path:
        return self.locks_dir / (self._key(project) + ".lock")

    def _new_record(self, project: str, root: Path) -> dict:
        return {
            "version": 1,
            "project": project,
            "project_root": str(root),
            "uid": os.getuid(),
            "requests": [],
            "grant": None,
        }

    def _load(self, project: str, root: Path) -> dict:
        path = self._record_path(project)
        if not path.exists() and not path.is_symlink():
            return self._new_record(project, root)
        try:
            record = _read_private_json(path)
        except (OSError, HostRuntimeError) as exc:
            raise HostRuntimeError("HOST_ACCESS_STATE_INVALID") from exc
        if (set(record) != {"version", "project", "project_root", "uid", "requests", "grant"}
                or record["version"] != 1 or record["project"] != project
                or not isinstance(record["project_root"], str)
                or type(record["uid"]) is not int
                or not isinstance(record["requests"], list)
                or record["grant"] is not None and not isinstance(record["grant"], dict)):
            raise HostRuntimeError("HOST_ACCESS_STATE_INVALID")
        for request in record["requests"]:
            self._validate_request(request)
        if record["grant"] is not None:
            self._validate_grant(record["grant"])
        return record

    @staticmethod
    def _validate_binding(binding: dict, *, request=False) -> None:
        expected = ({"request_id", "created_at", "expires_at", "boot_id", "runtime_identity",
                     "uid", "project_root"} if request else
                    {"grant_id", "created_at", "expires_at", "allow_codex", "boot_id",
                     "runtime_identity", "uid", "project_root", "revoked_at"})
        identifier = binding.get("request_id" if request else "grant_id")
        if (set(binding) != expected or not isinstance(identifier, str)
                or _HEX_ID.fullmatch(identifier) is None
                or not isinstance(binding.get("boot_id"), str)
                or _BOOT_ID.fullmatch(binding["boot_id"]) is None
                or not isinstance(binding.get("runtime_identity"), str)
                or _RUNTIME_ID.fullmatch(binding["runtime_identity"]) is None
                or type(binding.get("uid")) is not int
                or not isinstance(binding.get("project_root"), str)):
            raise HostRuntimeError("HOST_ACCESS_STATE_INVALID")
        _timestamp(binding.get("created_at"))
        _timestamp(binding.get("expires_at"))
        if request:
            return
        if type(binding.get("allow_codex")) is not bool:
            raise HostRuntimeError("HOST_ACCESS_STATE_INVALID")
        if binding.get("revoked_at") is not None:
            _timestamp(binding["revoked_at"])

    @classmethod
    def _validate_request(cls, request: dict) -> None:
        if not isinstance(request, dict):
            raise HostRuntimeError("HOST_ACCESS_STATE_INVALID")
        cls._validate_binding(request, request=True)

    @classmethod
    def _validate_grant(cls, grant: dict) -> None:
        cls._validate_binding(grant)

    @staticmethod
    def _request_public(project: str, request: dict) -> dict:
        return {
            "project": project,
            "scope": SCOPE,
            "request_id": request["request_id"],
            "expires_at": request["expires_at"],
        }

    @staticmethod
    def _grant_public(project: str, grant: dict) -> dict:
        return {
            "grant_id": grant["grant_id"],
            "expires_at": grant["expires_at"],
            "allow_codex": grant["allow_codex"],
            "project": project,
            "scope": SCOPE,
        }

    def _binding(self, root: Path) -> dict:
        return {
            "boot_id": _current_boot_id(),
            "runtime_identity": self._runtime_identity(),
            "uid": os.getuid(),
            "project_root": str(root),
        }

    @staticmethod
    def _binding_error(saved: dict, current: dict, *, request=False) -> str | None:
        if saved["uid"] != current["uid"]:
            return "HOST_ACCESS_REQUEST_STALE" if request else "HOST_ACCESS_UID_CHANGED"
        if saved["project_root"] != current["project_root"]:
            return "HOST_ACCESS_REQUEST_STALE" if request else "HOST_ACCESS_PROJECT_CHANGED"
        if saved["runtime_identity"] != current["runtime_identity"]:
            return "HOST_ACCESS_REQUEST_STALE" if request else "HOST_ACCESS_RUNTIME_CHANGED"
        if saved["boot_id"] != current["boot_id"]:
            return "HOST_ACCESS_REQUEST_STALE" if request else "HOST_ACCESS_BOOT_CHANGED"
        return None

    def _request_v1(self, project):
        root = self._project_root(project)
        binding = self._binding(root)
        now = _utcnow()
        request = {
            "request_id": secrets.token_hex(16),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat(),
            **binding,
        }
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            record["requests"] = [item for item in record["requests"]
                                  if _timestamp(item["expires_at"]) > now]
            record["requests"].append(request)
            _save_private_json(self._record_path(project), record)
        return self._request_public(project, request)

    def _get_v1(self, project):
        root = self._project_root(project)
        now = _utcnow()
        host_identity = _host_identity()
        try:
            current = self._binding(root)
            runtime_error = None
        except HostRuntimeError as exc:
            current = None
            runtime_error = exc.code
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            pending = []
            for request in record["requests"]:
                if (_timestamp(request["expires_at"]) > now and current is not None
                        and self._binding_error(request, current, request=True) is None):
                    pending.append({"request_id": request["request_id"],
                                    "expires_at": request["expires_at"]})
            base = {"project": project, "scope": SCOPE, "active": False,
                    "reason": "NO_GRANT", "requests": pending,
                    "host_identity": host_identity}
            grant = record["grant"]
            if grant is None:
                if runtime_error:
                    base["reason"] = runtime_error
                return base
            if grant["revoked_at"] is not None:
                base["reason"] = "REVOKED"
                return base
            if _timestamp(grant["expires_at"]) <= now:
                base["reason"] = "EXPIRED"
                return base
            if current is None:
                base["reason"] = runtime_error
                return base
            error = self._binding_error(grant, current)
            if error:
                base["reason"] = error.removeprefix("HOST_ACCESS_")
                return base
            return {**self._grant_public(project, grant), "active": True,
                    "reason": None, "requests": pending,
                    "host_identity": host_identity}

    def _approve_web_v1(self, project, *, request_id, duration_seconds,
                        allow_codex, confirmation):
        root = self._project_root(project)
        if confirmation != project:
            raise HostRuntimeError("HOST_ACCESS_CONFIRMATION_DENIED")
        if duration_seconds is None:
            duration_seconds = DEFAULT_DURATION_SECONDS
        if (type(duration_seconds) is not int or duration_seconds <= 0
                or duration_seconds > MAX_DURATION_SECONDS):
            raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
        if type(allow_codex) is not bool:
            raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")
        if not isinstance(request_id, str) or _HEX_ID.fullmatch(request_id) is None:
            raise HostRuntimeError("HOST_ACCESS_REQUEST_NOT_FOUND")
        with _lock(self._lock_path(project)):
            current = self._binding(root)
            now = _utcnow()
            record = self._load(project, root)
            request = next((item for item in record["requests"]
                            if item["request_id"] == request_id), None)
            if request is None:
                raise HostRuntimeError("HOST_ACCESS_REQUEST_NOT_FOUND")
            if _timestamp(request["expires_at"]) <= now:
                raise HostRuntimeError("HOST_ACCESS_REQUEST_EXPIRED")
            if self._binding_error(request, current, request=True):
                raise HostRuntimeError("HOST_ACCESS_REQUEST_STALE")
            grant = {
                "grant_id": secrets.token_hex(16),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=duration_seconds)).isoformat(),
                "allow_codex": allow_codex,
                **current,
                "revoked_at": None,
            }
            record["requests"] = [item for item in record["requests"]
                                  if item["request_id"] != request_id
                                  and _timestamp(item["expires_at"]) > now]
            record["grant"] = grant
            record["project_root"] = str(root)
            record["uid"] = os.getuid()
            _save_private_json(self._record_path(project), record)
        return self._grant_public(project, grant)

    def _require_locked(self, project: str, root: Path, grant_id, codex: bool) -> dict:
        if type(codex) is not bool:
            raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")
        if grant_id is not None and (not isinstance(grant_id, str)
                                     or _HEX_ID.fullmatch(grant_id) is None):
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        current = self._binding(root)
        record = self._load(project, root)
        grant = record["grant"]
        if grant is None:
            raise HostRuntimeError("HOST_ACCESS_REQUIRED")
        if grant_id is not None and grant["grant_id"] != grant_id:
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        if grant["revoked_at"] is not None:
            raise HostRuntimeError("HOST_ACCESS_REVOKED")
        if _timestamp(grant["expires_at"]) <= _utcnow():
            raise HostRuntimeError("HOST_ACCESS_EXPIRED")
        error = self._binding_error(grant, current)
        if error:
            raise HostRuntimeError(error)
        if codex and not grant["allow_codex"]:
            raise HostRuntimeError("HOST_ACCESS_CODEX_DENIED")
        return self._grant_public(project, grant)

    def _require_v1(self, project, *, grant_id=None, codex=False):
        root = self._project_root(project)
        with _lock(self._lock_path(project)):
            return self._require_locked(project, root, grant_id, codex)

    @contextmanager
    def _guard_v1(self, project, grant_id, *, codex=False):
        root = self._project_root(project)
        with _lock(self._lock_path(project)):
            yield self._require_locked(project, root, grant_id, codex)

    def _revoke_web_v1(self, project, *, grant_id, confirmation):
        root = self._project_root(project)
        if confirmation != project:
            raise HostRuntimeError("HOST_ACCESS_CONFIRMATION_DENIED")
        if not isinstance(grant_id, str) or _HEX_ID.fullmatch(grant_id) is None:
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            grant = record["grant"]
            if grant is None or grant["grant_id"] != grant_id:
                raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
            if grant["revoked_at"] is None:
                grant["revoked_at"] = _utcnow().isoformat()
                _save_private_json(self._record_path(project), record)
        return {"project": project, "grant_id": grant_id, "revoked": True}

    def _v2_marker_present(self) -> bool:
        path = self.installation.marker_path
        return path.exists() or path.is_symlink()

    def _launch_v2(self) -> dict:
        if self.workspace is None or self.workspace.enabled is not True:
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        try:
            admitted = verify_launch(
                Path(__file__).resolve().parent,
                self.workspace.authority.state_dir,
                enforcement_required=True,
            )
        except (WorkspaceControlError, WorkspaceError, OSError) as exc:
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED") from exc
        if (admitted.get("status") != "launch_allowed"
                or admitted.get("authority_active") is not True
                or admitted.get("release_abi") != 3
                or admitted.get("enforcement_abi") != 3
                or admitted.get("minimum_abi") != 3
                or admitted.get("host_access_capability_generation")
                != HOST_ACCESS_CAPABILITY_GENERATION):
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        return admitted

    @contextmanager
    def _v2_authority_admission(self, launch: dict):
        try:
            snapshot = self.workspace.policy.load(required=True)
            with self.workspace.authority.admission(snapshot.digest) as binding:
                if binding != launch.get("binding"):
                    raise HostRuntimeError("HOST_ACCESS_WORKSPACE_INVALID")
                yield {
                    "installation_id": binding["installation_id"],
                    "uid": os.getuid(),
                    "capability_generation": HOST_ACCESS_CAPABILITY_GENERATION,
                }
        except WorkspaceError as exc:
            raise HostRuntimeError("HOST_ACCESS_WORKSPACE_INVALID") from exc

    @staticmethod
    def _check_v2_binding(state: dict, current: dict) -> None:
        if state["installation_id"] != current["installation_id"]:
            raise HostRuntimeError("HOST_ACCESS_INSTALLATION_CHANGED")
        if state["uid"] != current["uid"]:
            raise HostRuntimeError("HOST_ACCESS_UID_CHANGED")
        if state["capability_generation"] != current["capability_generation"]:
            raise HostRuntimeError("HOST_ACCESS_CAPABILITY_CHANGED")

    @contextmanager
    def _v2_locked(self):
        launch = self._launch_v2()
        with self.installation.lock():
            state = self.installation.load()
            with self._v2_authority_admission(launch) as current:
                self._check_v2_binding(state, current)
                yield state

    def installation_scope_enabled(self) -> bool:
        with self.installation.lock():
            return self.installation.enabled()

    def enable_installation_scope(self) -> dict:
        if self.workspace is None or self.workspace.enabled is not True:
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        authority_dir = self.workspace.authority.state_dir
        try:
            with activation_admission(authority_dir, create=True):
                launch = self._launch_v2()
                with publication_lane(self.workspace):
                    with self.installation.lock():
                        if self.installation.enabled():
                            state = self.installation.load()
                            return {
                                "version": 2, "enabled": True,
                                "grant_state_revision": state["grant_state_revision"],
                            }
                        with self._v2_authority_admission(launch) as current:
                            now = _utcnow().isoformat()
                            state = {
                                "version": 2,
                                **current,
                                "grant_state_revision": 1,
                                "requests": {},
                                "grant": None,
                                "updated_at": now,
                            }
                            self.installation.mark_enabled()
                            self.installation.invalidate_legacy(self.records_dir)
                            self.installation.create(state)
        except WorkspaceControlError as exc:
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED") from exc
        return {"version": 2, "enabled": True, "grant_state_revision": 1}

    @staticmethod
    def _v2_request_public(project: str, request: dict) -> dict:
        return {
            "version": 2,
            "project": project,
            "scope": SCOPE,
            "request_id": request["request_id"],
            "expires_at": request["expires_at"],
            "lifetime": request["lifetime"],
            "grant_state_revision": request["grant_state_revision"],
        }

    @staticmethod
    def _v2_grant_public(project: str, grant: dict, revision: int) -> dict:
        return {
            "version": 2,
            "grant_id": grant["grant_id"],
            "expires_at": grant["expires_at"],
            "lifetime": grant["lifetime"],
            "allow_codex": grant["allow_codex"],
            "grant_state_revision": revision,
            "project": project,
            "scope": SCOPE,
        }

    def _request_v2(self, project, lifetime):
        self._project_root(project)
        if lifetime not in {"timed", "until_revoked"}:
            raise HostRuntimeError("INVALID_HOST_ACCESS_LIFETIME")
        with self._v2_locked() as state:
            now = _utcnow()
            state["requests"] = {
                key: item for key, item in state["requests"].items()
                if _timestamp(item["expires_at"]) > now
            }
            request = {
                "request_id": secrets.token_hex(16),
                "project": project,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat(),
                "lifetime": lifetime,
                "grant_state_revision": state["grant_state_revision"],
                "installation_id": state["installation_id"],
                "uid": state["uid"],
                "capability_generation": state["capability_generation"],
            }
            state["requests"][request["request_id"]] = request
            state["updated_at"] = now.isoformat()
            self.installation.save(state)
            return self._v2_request_public(project, request)

    @staticmethod
    def _grant_satisfies(grant, *, lifetime, duration_seconds, allow_codex, now):
        if grant is None or grant.get("revoked_at") is not None:
            return False
        if allow_codex and grant.get("allow_codex") is not True:
            return False
        grant_lifetime = grant.get("lifetime", "timed")
        if lifetime == "until_revoked" and grant_lifetime != "until_revoked":
            return False
        if grant_lifetime == "until_revoked":
            return True
        expires_at = _timestamp(grant.get("expires_at"))
        return (lifetime == "timed" and expires_at > now
                and (expires_at - now).total_seconds() >= duration_seconds)

    @staticmethod
    def _validate_card_options(*, lifetime, duration_seconds, allow_codex):
        if lifetime not in {"timed", "until_revoked"}:
            raise HostRuntimeError("INVALID_HOST_ACCESS_LIFETIME")
        if lifetime == "timed":
            if (type(duration_seconds) is not int or duration_seconds <= 0
                    or duration_seconds > MAX_DURATION_SECONDS):
                raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
        elif duration_seconds is not None:
            raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
        if type(allow_codex) is not bool:
            raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")

    def _begin_card_request_v1(self, project, *, lifetime, duration_seconds, allow_codex):
        root = self._project_root(project)
        if lifetime != "timed":
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        binding = self._binding(root)
        now = _utcnow()
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            record["requests"] = [item for item in record["requests"]
                                  if _timestamp(item["expires_at"]) > now]
            grant = record["grant"]
            if (grant is not None
                    and self._binding_error(grant, binding) is None
                    and self._grant_satisfies(
                        grant, lifetime=lifetime, duration_seconds=duration_seconds,
                        allow_codex=allow_codex, now=now)):
                return {"grant": self._grant_public(project, grant), "request": None}
            request = {
                "request_id": secrets.token_hex(16),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat(),
                **binding,
            }
            record["requests"].append(request)
            _save_private_json(self._record_path(project), record)
            return {"grant": None, "request": self._request_public(project, request)}

    def _begin_card_request_v2(self, project, *, lifetime, duration_seconds, allow_codex):
        self._project_root(project)
        with self._v2_locked() as state:
            now = _utcnow()
            state["requests"] = {
                key: item for key, item in state["requests"].items()
                if _timestamp(item["expires_at"]) > now
            }
            grant = state["grant"]
            if self._grant_satisfies(
                    grant, lifetime=lifetime, duration_seconds=duration_seconds,
                    allow_codex=allow_codex, now=now):
                return {
                    "grant": self._v2_grant_public(
                        project, grant, state["grant_state_revision"]),
                    "request": None,
                }
            request = {
                "request_id": secrets.token_hex(16),
                "project": project,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat(),
                "lifetime": lifetime,
                "grant_state_revision": state["grant_state_revision"],
                "installation_id": state["installation_id"],
                "uid": state["uid"],
                "capability_generation": state["capability_generation"],
            }
            state["requests"][request["request_id"]] = request
            state["updated_at"] = now.isoformat()
            self.installation.save(state)
            return {"grant": None, "request": self._v2_request_public(project, request)}

    def begin_card_request(self, project, *, lifetime="timed",
                           duration_seconds=DEFAULT_DURATION_SECONDS, allow_codex=False):
        self._validate_card_options(
            lifetime=lifetime, duration_seconds=duration_seconds, allow_codex=allow_codex)
        if lifetime != "timed" and not self._v2_marker_present():
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        return self._versioned(
            lambda: self._begin_card_request_v2(
                project, lifetime=lifetime, duration_seconds=duration_seconds,
                allow_codex=allow_codex),
            lambda: self._begin_card_request_v1(
                project, lifetime=lifetime, duration_seconds=duration_seconds,
                allow_codex=allow_codex),
        )

    def card_request_valid(self, project, *, request_id, lifetime,
                           grant_state_revision=None):
        root = self._project_root(project)
        if self._v2_marker_present():
            with self._v2_locked() as state:
                request = state["requests"].get(request_id)
                return bool(request is not None
                            and request["project"] == project
                            and request["lifetime"] == lifetime
                            and request["grant_state_revision"] == grant_state_revision
                            and _timestamp(request["expires_at"]) > _utcnow())
        if lifetime != "timed" or grant_state_revision is not None:
            return False
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            return any(item["request_id"] == request_id
                       and _timestamp(item["expires_at"]) > _utcnow()
                       and self._binding_error(item, self._binding(root), request=True) is None
                       for item in record["requests"])

    def replace_card_request(self, project, *, request_id, lifetime,
                             grant_state_revision=None):
        root = self._project_root(project)
        if lifetime != "timed" and not self._v2_marker_present():
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        if self._v2_marker_present():
            with self._v2_locked() as state:
                old = state["requests"].get(request_id)
                if (old is None or old["project"] != project
                        or old["grant_state_revision"] != grant_state_revision
                        or _timestamp(old["expires_at"]) <= _utcnow()):
                    raise HostRuntimeError("HOST_ACCESS_REQUEST_STALE")
                now = _utcnow()
                request = {
                    **old,
                    "request_id": secrets.token_hex(16),
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat(),
                    "lifetime": lifetime,
                }
                state["requests"].pop(request_id)
                state["requests"][request["request_id"]] = request
                state["updated_at"] = now.isoformat()
                self.installation.save(state)
                return self._v2_request_public(project, request)
        if lifetime != "timed" or grant_state_revision is not None:
            raise HostRuntimeError("HOST_ACCESS_REQUEST_STALE")
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            old = next((item for item in record["requests"]
                        if item["request_id"] == request_id), None)
            if old is None or _timestamp(old["expires_at"]) <= _utcnow():
                raise HostRuntimeError("HOST_ACCESS_REQUEST_STALE")
            return self._request_public(project, old)

    def discard_card_request(self, project, *, request_id, required=False):
        root = self._project_root(project)
        if self._v2_marker_present():
            with self._v2_locked() as state:
                found = request_id in state["requests"]
                if required and not found:
                    raise HostRuntimeError("HOST_ACCESS_CARD_REQUEST_CONSUMED")
                state["requests"].pop(request_id, None)
                state["updated_at"] = _utcnow().isoformat()
                self.installation.save(state)
                return
        with _lock(self._lock_path(project)):
            record = self._load(project, root)
            found = any(item["request_id"] == request_id for item in record["requests"])
            if required and not found:
                raise HostRuntimeError("HOST_ACCESS_CARD_REQUEST_CONSUMED")
            record["requests"] = [item for item in record["requests"]
                                  if item["request_id"] != request_id]
            _save_private_json(self._record_path(project), record)

    def _get_v2(self, project):
        self._project_root(project)
        with self._v2_locked() as state:
            now = _utcnow()
            pending = [{
                "request_id": request["request_id"],
                "expires_at": request["expires_at"],
                "lifetime": request["lifetime"],
                "grant_state_revision": request["grant_state_revision"],
            } for request in state["requests"].values()
                if _timestamp(request["expires_at"]) > now]
            base = {
                "version": 2,
                "project": project,
                "scope": SCOPE,
                "active": False,
                "reason": "NO_GRANT",
                "requests": pending,
                "host_identity": _host_identity(),
                "lifetime": None,
                "expires_at": None,
                "grant_state_revision": state["grant_state_revision"],
            }
            grant = state["grant"]
            if grant is None:
                return base
            base["lifetime"] = grant["lifetime"]
            base["expires_at"] = grant["expires_at"]
            if grant["revoked_at"] is not None:
                base["reason"] = "REVOKED"
                return base
            if (grant["lifetime"] == "timed"
                    and _timestamp(grant["expires_at"]) <= now):
                base["reason"] = "EXPIRED"
                return base
            return {
                **base,
                **self._v2_grant_public(
                    project, grant, state["grant_state_revision"],
                ),
                "active": True,
                "reason": None,
            }

    def _approve_web_v2(self, project, *, request_id, duration_seconds,
                        allow_codex, confirmation, lifetime):
        self._project_root(project)
        if confirmation != project:
            raise HostRuntimeError("HOST_ACCESS_CONFIRMATION_DENIED")
        if lifetime not in {"timed", "until_revoked"}:
            raise HostRuntimeError("INVALID_HOST_ACCESS_LIFETIME")
        if lifetime == "timed":
            if duration_seconds is None:
                duration_seconds = DEFAULT_DURATION_SECONDS
            if (type(duration_seconds) is not int or duration_seconds <= 0
                    or duration_seconds > MAX_DURATION_SECONDS):
                raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
        elif duration_seconds is not None:
            raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
        if type(allow_codex) is not bool:
            raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")
        if not isinstance(request_id, str) or _HEX_ID.fullmatch(request_id) is None:
            raise HostRuntimeError("HOST_ACCESS_REQUEST_NOT_FOUND")
        with self._v2_locked() as state:
            now = _utcnow()
            request = state["requests"].get(request_id)
            if request is None or request.get("project") != project:
                raise HostRuntimeError("HOST_ACCESS_REQUEST_NOT_FOUND")
            if _timestamp(request["expires_at"]) <= now:
                raise HostRuntimeError("HOST_ACCESS_REQUEST_EXPIRED")
            if (request["grant_state_revision"] != state["grant_state_revision"]
                    or request["lifetime"] != lifetime):
                raise HostRuntimeError("HOST_ACCESS_REQUEST_STALE")
            revision = state["grant_state_revision"] + 1
            grant = {
                "grant_id": secrets.token_hex(16),
                "created_at": now.isoformat(),
                "expires_at": ((now + timedelta(seconds=duration_seconds)).isoformat()
                               if lifetime == "timed" else None),
                "lifetime": lifetime,
                "allow_codex": allow_codex,
                "revoked_at": None,
                "grant_state_revision": revision,
                "installation_id": state["installation_id"],
                "uid": state["uid"],
                "capability_generation": state["capability_generation"],
            }
            state["grant_state_revision"] = revision
            state["requests"] = {}
            state["grant"] = grant
            state["updated_at"] = now.isoformat()
            self.installation.save(state)
            return self._v2_grant_public(project, grant, revision)

    def _require_v2_locked(self, state, project, grant_id, codex):
        if type(codex) is not bool:
            raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")
        if grant_id is not None and (not isinstance(grant_id, str)
                                     or _HEX_ID.fullmatch(grant_id) is None):
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        grant = state["grant"]
        if grant is None:
            raise HostRuntimeError("HOST_ACCESS_REQUIRED")
        if grant_id is not None and grant["grant_id"] != grant_id:
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        if grant["revoked_at"] is not None:
            raise HostRuntimeError("HOST_ACCESS_REVOKED")
        if (grant["lifetime"] == "timed"
                and _timestamp(grant["expires_at"]) <= _utcnow()):
            raise HostRuntimeError("HOST_ACCESS_EXPIRED")
        if codex and not grant["allow_codex"]:
            raise HostRuntimeError("HOST_ACCESS_CODEX_DENIED")
        return self._v2_grant_public(
            project, grant, state["grant_state_revision"],
        )

    def _require_v2(self, project, *, grant_id=None, codex=False):
        self._project_root(project)
        with self._v2_locked() as state:
            return self._require_v2_locked(
                state, project, grant_id, codex,
            )

    @contextmanager
    def _guard_v2(self, project, grant_id, *, codex=False):
        self._project_root(project)
        with self._v2_locked() as state:
            yield self._require_v2_locked(
                state, project, grant_id, codex,
            )

    def _revoke_web_v2(self, project, *, grant_id, confirmation):
        self._project_root(project)
        if confirmation != project:
            raise HostRuntimeError("HOST_ACCESS_CONFIRMATION_DENIED")
        if not isinstance(grant_id, str) or _HEX_ID.fullmatch(grant_id) is None:
            raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
        with self._v2_locked() as state:
            grant = state["grant"]
            if grant is None or grant["grant_id"] != grant_id:
                raise HostRuntimeError("HOST_ACCESS_GRANT_MISMATCH")
            if grant["revoked_at"] is None:
                now = _utcnow().isoformat()
                grant["revoked_at"] = now
                state["grant_state_revision"] += 1
                state["requests"] = {}
                state["updated_at"] = now
                self.installation.save(state)
            return {
                "version": 2,
                "project": project,
                "grant_id": grant_id,
                "grant_state_revision": state["grant_state_revision"],
                "revoked": True,
            }

    def _versioned(self, v2, v1):
        if self._v2_marker_present():
            return v2()
        with self.installation.lock():
            switched = self._v2_marker_present()
            if not switched:
                return v1()
        return v2()

    def request(self, project, *, lifetime="timed"):
        if lifetime != "timed" and not self._v2_marker_present():
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        return self._versioned(
            lambda: self._request_v2(project, lifetime),
            lambda: self._request_v1(project),
        )

    def get(self, project):
        return self._versioned(
            lambda: self._get_v2(project),
            lambda: self._get_v1(project),
        )

    def approve_web(self, project, *, request_id, duration_seconds,
                    allow_codex, confirmation, lifetime="timed"):
        if lifetime != "timed" and not self._v2_marker_present():
            raise HostRuntimeError("HOST_ACCESS_OWNER_SETUP_REQUIRED")
        return self._versioned(
            lambda: self._approve_web_v2(
                project, request_id=request_id,
                duration_seconds=duration_seconds, allow_codex=allow_codex,
                confirmation=confirmation, lifetime=lifetime,
            ),
            lambda: self._approve_web_v1(
                project, request_id=request_id,
                duration_seconds=duration_seconds, allow_codex=allow_codex,
                confirmation=confirmation,
            ),
        )

    def require(self, project, *, grant_id=None, codex=False):
        return self._versioned(
            lambda: self._require_v2(
                project, grant_id=grant_id, codex=codex,
            ),
            lambda: self._require_v1(
                project, grant_id=grant_id, codex=codex,
            ),
        )

    @contextmanager
    def guard(self, project, grant_id, *, codex=False):
        if self._v2_marker_present():
            with self._guard_v2(project, grant_id, codex=codex) as grant:
                yield grant
            return
        with self.installation.lock():
            if not self._v2_marker_present():
                with self._guard_v1(project, grant_id, codex=codex) as grant:
                    yield grant
                return
        with self._guard_v2(project, grant_id, codex=codex) as grant:
            yield grant

    def revoke_web(self, project, *, grant_id, confirmation):
        return self._versioned(
            lambda: self._revoke_web_v2(
                project, grant_id=grant_id, confirmation=confirmation,
            ),
            lambda: self._revoke_web_v1(
                project, grant_id=grant_id, confirmation=confirmation,
            ),
        )
