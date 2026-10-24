"""Source-pinned, separately approved deployments of the dedicated test API/Worker.

No generic shell or SQL API. This manager and its approval CLI must be loaded
from the immutable installed runtime, not an allowed writable repository.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import sys

from host_runtime import (HostRuntime, source_inventory, git_state, _parse_json,
                          HIGH_RISK_INPUTS, PREDECESSOR_NAMES, PREDECESSOR_SQL_INPUTS, SOURCE_SINGLE,
                          SOURCE_DIRS, SOURCE_GLOB, verify_reviewed_high_risk_inputs)
from host_runtime_config import (
    HostRuntimeError, PROJECT, TARGET, TRUSTED_APPROVAL_ORIGIN,
    private_dir, private_file, validate_scope,
    validate_state_root, extended_enabled, predecessor_enabled, is_registered_config,
    read_config,
)
from workspace_host import (
    acquire_host_operation_lane, complete_host_operation, resolve_workspace,
)
from workspace_control import WorkspaceControlError, activation_admission

PLAN_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_OPERATIONS = 1000
ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
REQUEST_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
TERMINAL = {"succeeded", "failed", "rejected", "expired", "outcome_unknown"}
TARGET_AUTHORIZATION_COMPONENTS = ("api", "worker", "migration", "predecessor_migration")
_LOGGER = logging.getLogger(__name__)
_RESERVATION_RELEASE_FAILED = "WORKSPACE_HOST_RESERVATION_RELEASE_FAILED"


# Every module the host managers import plus the approval web assets, pinned against the
# installed manifest.json. Extend this list; never replace the legacy entries.
VERIFIED_RUNTIME_FILES = (
    "server.py", "host_runtime.py", "host_runtime_config.py",
    "host_runtime_operations.py", "host_runtime_tools.py",
    "approve-host-operation.py", "host_runtime_web.py", "host_db_migration.py", "host_schema014.py",
    "toss_console_evidence.py", "toss_console_tools.py", "toss_console_sdk_bridge.py",
    "toss_console_codex.py",
    "runtime_rollout_web.py", "runtime_rollout_worker.py", "runtime_rollout_tools.py", "runtime_rollout_ui.py", "runtime-rollout-approval.html",
    "admin_server.py", "admin.html", "admin.js", "admin.css", "agent-admin.js",
    "runtime-rollout-admin.js",
    "workroom-frame.html", "workroom-frame.js", "workroom-bridge.js",
    "host_registry.py", "host_actions.py", "host_process.py", "host_source_review.py",
    "host_registered_operations.py", "host_compose_actions.py", "host_registered_tools.py",
    "agent_profiles.py", "agent_tasks.py", "agent_runner.py", "agent_events.py",
    "agent_results.py", "agent_tools.py", "agent_host_broker.py",
    "agent_broker_server.py", "agent-bootstrap.c", "agent_bootstrap.py",
    "agent_supervisor.py", "agent_probe.py", "agent_code_mode.py", "agent_proofs.py", "agent_smoke.py",
    "agent_profile_setup.py", "configure-agent-profile.py",
    "host_command_profiles.py", "host_command_entry.py",
    "host_command_runner.py", "host_command_supervisor.py",
    "host_command_operations.py", "host_command_tools.py", "host-command-admin.js",
    "host_access.py", "host_execution.py", "host_execution_tools.py", "host_agent.py",
    "host-access-admin.js", "host_access_ui.py", "host-access-approval.html",
    "host_operation_ui.py", "host-operation-approval.html",
    "host_command_ui.py", "host-command-approval.html",
    "workspace_paths.py", "workspace_policy.py", "workspace_authority.py",
    "workspace_files.py", "workspace_jobs.py", "workspace_tools.py", "workspace_host.py",
    "workspace_control.py", "workspace-launcher.py", "workspace-access-approval.html",
    "work-dashboard/model.js", "work-dashboard/motion.js", "work-dashboard/pixels.js",
    "work-dashboard/visualizer.js", "work-dashboard/visualizer.css",
)


def _verified_installation():
    """Mutable source checkouts cannot host-execute, even if config is enabled."""
    folder = Path(__file__).resolve().parent
    releases = Path.home() / ".local/lib/chatgpt-coding-mcp/releases"
    if (folder.parent != releases.resolve(strict=False)
            or not re.fullmatch(r"[0-9a-f]{20}", folder.name)
            or folder.is_symlink()):
        return False
    try:
        manifest = _read_private_json(folder / "manifest.json")
        for name in VERIFIED_RUNTIME_FILES:
            path = folder / name
            private_file(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get(name):
                return False
    except (HostRuntimeError, OSError, ValueError):
        return False
    return True


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def default_state_dir() -> Path:
    """HostRuntime state root shared by legacy and registered operations."""
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "chatgpt-coding-mcp" / "host-runtime"


def operation_path(directory: Path, operation_id) -> Path:
    if not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id):
        raise HostRuntimeError("INVALID_OPERATION_ID")
    return directory / (operation_id + ".json")


def acquire_flock(path: Path) -> int:
    """Non-blocking exclusive flock on an owner-only regular file; returns the fd."""
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise HostRuntimeError("UNSAFE_STATE_FILE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HostRuntimeError("TARGET_BUSY") from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def host_admission(state_root: Path):
    """Serialize typed admission with unrestricted dynamic host execution."""
    admission_root = private_dir(Path(state_root).absolute() / "host-executions")
    fd = acquire_flock(admission_root / "locks" / "host.lock")
    try:
        yield
    finally:
        os.close(fd)


def observed_legacy_operation_blocks_host(path: Path) -> bool:
    """Validate an observed legacy active record without mutating foreign state."""
    if not ID_PATTERN.fullmatch(path.stem):
        return False
    state = _read_private_json(path)
    if state.get("state") not in {"approved", "queued", "running"}:
        return False
    immutable = state.get("immutable")
    operation_id = path.stem
    if (not isinstance(immutable, dict)
            or state.get("operation_id") != operation_id
            or state.get("plan_digest") != _digest(immutable)
            or immutable.get("operation_id") != operation_id
            or immutable.get("project") != state.get("project")
            or immutable.get("target") != state.get("target")
            or immutable.get("mode") not in ("toss-demo", "disabled")
            or immutable.get("component", "api") not in (
                "api", "worker", "migration", "predecessor_migration", "schema_update", "schema014")):
        raise HostRuntimeError("PLAN_INVALID")
    expires_at = _timestamp(immutable.get("expires_at"))
    return state["state"] in {"queued", "running"} or expires_at > datetime.now(timezone.utc)


def lock_busy(path: Path) -> bool:
    try:
        fd = acquire_flock(path)
    except HostRuntimeError as exc:
        if exc.code == "TARGET_BUSY":
            return True
        raise
    os.close(fd)
    return False


def _digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError("Timezone required")
        return result
    except (ValueError, TypeError, AttributeError) as exc:
        raise HostRuntimeError("PLAN_INVALID") from exc


def _read_private_json(path):
    private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as reader:
            data = reader.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise HostRuntimeError("PLAN_INVALID")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise HostRuntimeError("PLAN_INVALID")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, HostRuntimeError):
            raise
        raise HostRuntimeError("PLAN_INVALID") from exc


def _save_private_json(path, value):
    parent = private_dir(path.parent)
    if path.exists() or path.is_symlink():
        private_file(path)
    temp = parent / ("." + secrets.token_hex(16) + ".tmp")
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _lock(path, *, blocking=True):
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise HostRuntimeError("UNSAFE_STATE_FILE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise HostRuntimeError("TARGET_BUSY") from exc
        yield
    finally:
        os.close(fd)


def _identity(snapshot):
    return {role: {
        "id": snapshot[role]["id"],
        "started_at": snapshot[role]["started_at"],
        "image_id": snapshot[role]["image_id"],
    } for role in ("api", "worker", "db")}


class HostRuntimeOperations:
    def __init__(self, projects, *, state_dir=None, config_path=None, runtime=None,
                 approval_script=None, workspace=None):
        self.projects = projects
        self.state_dir = validate_state_root(state_dir or default_state_dir(), projects)
        self.operations_dir = private_dir(self.state_dir / "operations")
        self.context_dir = private_dir(self.state_dir / "contexts")
        self.target_authorizations_dir = private_dir(self.state_dir / "target-authorizations")
        self.lock_path = self.state_dir / "operations.lock"
        self.target_authorizations_lock_path = self.state_dir / "target-authorizations.lock"
        self.target_lock_path = self.state_dir / "target.lock"
        self.attestation_path = self.state_dir / "deployed-attestation.json"
        self.runtime = runtime or HostRuntime(projects, config_path=config_path)
        self.workspace = resolve_workspace(projects, workspace)
        authority = getattr(self.workspace, "authority", None)
        self.activation_authority_dir = Path(
            getattr(authority, "state_dir", self.state_dir.parent / "workspace-authority")
        ).absolute()
        self.approval_script = Path(approval_script or Path(__file__).with_name("approve-host-operation.py"))
        # Test fixtures may inject BOTH a fake runtime and a synthetic
        # approval helper. Passing approval_script alone (as the real host
        # CLI does) must never bypass immutable-installation verification.
        self._trusted_runtime = (_verified_installation() or
                                 (runtime is not None and approval_script is not None))
        self._tasks = set()

    def _require_trusted_runtime(self):
        if not self._trusted_runtime:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _acquire_target_lock(self):
        """Acquire the same cross-process lock from queueing through postcheck."""
        return acquire_flock(self.target_lock_path)

    def _target_lock_busy(self):
        return lock_busy(self.target_lock_path)

    def _path(self, operation_id):
        return operation_path(self.operations_dir, operation_id)

    def _load(self, project, target, operation_id):
        validate_scope(project, target, self.projects)
        state = _read_private_json(self._path(operation_id))
        immutable = state.get("immutable")
        if (state.get("project") != project or state.get("target") != target
                or state.get("operation_id") != operation_id
                or not isinstance(immutable, dict)
                or state.get("plan_digest") != _digest(immutable)
                or immutable.get("operation_id") != operation_id
                or immutable.get("project") != project
                or immutable.get("target") != target
                or immutable.get("mode") not in ("toss-demo", "disabled")
                or immutable.get("component", "api") not in (
                    "api", "worker", "migration", "predecessor_migration", "schema_update", "schema014")):
            raise HostRuntimeError("PLAN_INVALID")
        return state

    def _config_path(self):
        return self.runtime.config_path or Path.home() / ".config/chatgpt-coding-mcp/host-runtime.json"

    def _registered_reviewed_snapshot(self, project, target):
        """Return the fresh registered target when it authorizes reviewed snapshots.

        Imports stay lazy so a host missing optional registered modules can still use the
        fixed legacy runtime. A registered envelope, however, fails closed if its registry
        implementation is unavailable.
        """
        config_path = self._config_path()
        config = read_config(config_path)
        if not is_registered_config(config):
            return None
        try:
            from host_registry import HostTargetRegistry
        except ImportError as exc:
            raise HostRuntimeError("REGISTRY_NOT_CONFIGURED") from exc
        registry = HostTargetRegistry(self.projects, config_path)
        target_spec = registry.require_target(project, target)
        legacy = registry.legacy_view()
        if legacy is None or not legacy["enabled"]:
            raise HostRuntimeError("HOST_RUNTIME_DISABLED")
        if target_spec["source_review"] != "reviewed_snapshot":
            return None
        return registry, target_spec

    def _target_authorization_path(self, project, target):
        if (project, target) != (PROJECT, TARGET):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return self.target_authorizations_dir / (project + "--" + target + ".json")

    def _read_target_authorization_record(self, project, target):
        path = self._target_authorization_path(project, target)
        if not path.exists() and not path.is_symlink():
            return None
        value = _read_private_json(path)
        if (set(value) != {"version", "project", "target", "components", "enabled",
                           "grant_id", "granted_at", "updated_at", "config_digest",
                           "migration_checksums"}
                or value["version"] != 1
                or value["project"] != project or value["target"] != target
                or value["components"] != list(TARGET_AUTHORIZATION_COMPONENTS)
                or type(value["enabled"]) is not bool
                or not isinstance(value["grant_id"], str)
                or not ID_PATTERN.fullmatch(value["grant_id"])
                or not isinstance(value["config_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["config_digest"])
                or not isinstance(value["migration_checksums"], dict)
                or set(value["migration_checksums"]) != set(PREDECESSOR_NAMES)
                or any(not isinstance(checksum, str)
                       or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                       for checksum in value["migration_checksums"].values())):
            raise HostRuntimeError("TARGET_AUTHORIZATION_INVALID")
        _timestamp(value["granted_at"])
        _timestamp(value["updated_at"])
        return value

    @staticmethod
    def _migration_source_checksums(root):
        result = {}
        migrations = Path(root) / "server/migrations"
        for name in PREDECESSOR_NAMES:
            path = migrations / name
            try:
                if (path.is_symlink() or not path.is_file()
                        or not path.resolve(strict=True).is_relative_to(Path(root).resolve(strict=True))):
                    raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
                raw = path.read_bytes()
            except HostRuntimeError:
                raise
            except OSError as exc:
                raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE") from exc
            if len(raw) > 60 * 1024:
                raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
            result[name] = hashlib.sha256(raw).hexdigest()
        return result

    @staticmethod
    def _authorization_public(project, target, *, supported, enabled=False,
                              record=None, reason=None):
        return {
            "supported": supported,
            "enabled": enabled,
            "project": project,
            "target": target,
            "components": list(TARGET_AUTHORIZATION_COMPONENTS),
            "grant_id": record["grant_id"] if enabled and record else None,
            "granted_at": record["granted_at"] if enabled and record else None,
            "reason": reason,
        }

    def target_authorization(self, project, target):
        """Return safe status for the one fixed reviewed test target."""
        self._require_trusted_runtime()
        try:
            root = self.runtime.guard(project, target)
            reviewed = self._registered_reviewed_snapshot(project, target)
        except HostRuntimeError as exc:
            return self._authorization_public(
                project, target, supported=False, reason=exc.code)
        if reviewed is None or reviewed[1].get("environment") != "test":
            return self._authorization_public(
                project, target, supported=False, reason="UNSUPPORTED_POLICY")
        registry, _target_spec = reviewed
        record = self._read_target_authorization_record(project, target)
        if record is None:
            return self._authorization_public(
                project, target, supported=True, reason="NOT_GRANTED")
        if record["config_digest"] != registry.config_digest():
            return self._authorization_public(
                project, target, supported=True, reason="CONFIG_CHANGED")
        if not record["enabled"]:
            return self._authorization_public(
                project, target, supported=True, reason="REVOKED")
        try:
            current_migrations = self._migration_source_checksums(root)
        except HostRuntimeError:
            current_migrations = None
        if record["migration_checksums"] != current_migrations:
            return self._authorization_public(
                project, target, supported=True, reason="MIGRATION_SOURCE_CHANGED")
        return self._authorization_public(
            project, target, supported=True, enabled=True, record=record)

    def set_target_authorization(self, project, target, *, enabled: bool, confirmation: str):
        """Persist/revoke the fixed grant; repeated enable is idempotent until revocation."""
        self._require_trusted_runtime()
        if type(enabled) is not bool or confirmation != project + "/" + target:
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        root = self.runtime.guard(project, target)
        reviewed = self._registered_reviewed_snapshot(project, target)
        if reviewed is None or reviewed[1].get("environment") != "test":
            raise HostRuntimeError("UNSUPPORTED_POLICY")
        registry, _target_spec = reviewed
        digest = registry.config_digest()
        with _lock(self.target_authorizations_lock_path):
            current = self._read_target_authorization_record(project, target)
            migration_checksums = (
                self._migration_source_checksums(root) if enabled else
                current["migration_checksums"] if current is not None else
                {name: "0" * 64 for name in PREDECESSOR_NAMES}
            )
            if (enabled and current is not None and current["enabled"]
                    and current["config_digest"] == digest
                    and current["migration_checksums"] == migration_checksums):
                return self._authorization_public(
                    project, target, supported=True, enabled=True, record=current)
            now = utc_now()
            record = {
                "version": 1, "project": project, "target": target,
                "components": list(TARGET_AUTHORIZATION_COMPONENTS),
                "enabled": enabled,
                "grant_id": secrets.token_hex(16),
                "granted_at": now,
                "updated_at": now,
                "config_digest": digest,
                "migration_checksums": migration_checksums,
            }
            _save_private_json(self._target_authorization_path(project, target), record)
        return self._authorization_public(
            project, target, supported=True, enabled=enabled,
            record=record, reason=None if enabled else "REVOKED")

    def _active_target_authorization(self, project, target):
        status = self.target_authorization(project, target)
        if not status["supported"] or not status["enabled"]:
            return None
        reviewed = self._registered_reviewed_snapshot(project, target)
        if reviewed is None:
            return None
        return {"mode": "target", "grant_id": status["grant_id"],
                "config_digest": reviewed[0].config_digest()}

    def _verify_target_authorization(self, immutable):
        authorization = immutable.get("authorization", {"mode": "per_operation"})
        if authorization == {"mode": "per_operation"}:
            return False
        if (not isinstance(authorization, dict)
                or set(authorization) != {"mode", "grant_id", "config_digest"}
                or authorization.get("mode") != "target"):
            raise HostRuntimeError("PLAN_INVALID")
        active = self._active_target_authorization(
            immutable.get("project"), immutable.get("target"))
        if active != authorization:
            raise HostRuntimeError("TARGET_AUTHORIZATION_INVALID")
        requested = immutable.get("requested_component", immutable.get("component"))
        if requested not in TARGET_AUTHORIZATION_COMPONENTS:
            raise HostRuntimeError("TARGET_AUTHORIZATION_INVALID")
        return True

    @staticmethod
    def _legacy_source_inputs():
        return {"files": list(SOURCE_SINGLE),
                "dirs": {folder: SOURCE_GLOB.get(folder) for folder in SOURCE_DIRS}}

    @staticmethod
    def _source_review_metadata(immutable):
        review = immutable.get("source_review")
        if review is None:
            return None
        if (not isinstance(review, dict)
                or set(review) != {"policy", "review_digest", "config_digest",
                                   "inventory_digest"}
                or review.get("policy") != "reviewed_snapshot"
                or any(not isinstance(review.get(key), str)
                       or not re.fullmatch(r"[0-9a-f]{64}", review[key])
                       for key in ("review_digest", "config_digest", "inventory_digest"))):
            raise HostRuntimeError("PLAN_INVALID")
        return review

    def _review_data(self, immutable):
        review_meta = self._source_review_metadata(immutable)
        if review_meta is None:
            return None
        path = self.context_dir / immutable["operation_id"] / "review.json"
        try:
            private_file(path)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != review_meta["review_digest"]:
                raise HostRuntimeError("STALE_PLAN")
            review = json.loads(data)
        except HostRuntimeError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise HostRuntimeError("STALE_PLAN") from None
        if (not isinstance(review, dict)
                or set(review) != {"version", "git_head", "source_digest", "files"}
                or review["version"] != 1
                or review["git_head"] != immutable["git_head"]
                or review["source_digest"] != review_meta["inventory_digest"]
                or not isinstance(review["files"], list)):
            raise HostRuntimeError("STALE_PLAN")
        return review

    def _save(self, state):
        _save_private_json(self._path(state["operation_id"]), state)

    @staticmethod
    def _public(state, *, script=None, state_dir=None):
        component = state["immutable"].get("component", "api")
        authorization = copy.deepcopy(state["immutable"].get(
            "authorization", {"mode": "per_operation"}))
        payload = {
            "component": component,
            "requested_component": state["immutable"].get("requested_component", component),
            "project": state["project"], "target": state["target"],
            "operation_id": state["operation_id"], "state": state["state"],
            "phase": state["phase"], "mode": state["immutable"]["mode"],
            "plan_digest": state["plan_digest"],
            "source_digest": state["immutable"]["source_digest"],
            "created_at": state["created_at"],
            "expires_at": state["immutable"]["expires_at"],
            "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
            "code": state.get("code"),
            "backup": ({"sha256": state.get("backup_sha256"),
                        "bytes": state.get("backup_bytes")}
                       if state.get("backup_sha256") else None),
            "postcommit_counts_changed": state.get("postcommit_counts_changed"),
            "impact": {"recreates": [] if component in (
                           "migration", "predecessor_migration", "schema_update", "schema014") else [component],
                       "preserves": (["api", "worker"] if component in (
                           "migration", "predecessor_migration", "schema_update", "schema014")
                                     else [part for part in ("api", "worker", "db")
                                           if part != component]),
                       "schema_change": ("013_staged_reading_access.sql" if component == "migration"
                                         else "006-012_only" if component == "predecessor_migration"
                                         else "006-013_combined" if component == "schema_update"
                                         else "014_person_relationship_detail.sql" if component == "schema014"
                                         else None)},
            "migration_range": (["006", "012"] if component == "predecessor_migration"
                                else ["006", "013"] if component == "schema_update" else None),
            "migration_checksums": (state["immutable"].get("migration_checksums")
                                    if component in ("predecessor_migration", "schema_update", "schema014") else None),
            "database_generation": (state["immutable"].get("database_generation")
                                    if component in ("predecessor_migration", "schema_update", "schema014") else None),
            "changed_paths": state["immutable"]["changed_paths"],
            "high_risk_changes": state["immutable"]["high_risk_changes"],
            "source_review": copy.deepcopy(state["immutable"].get(
                "source_review", {"policy": "git_head"})),
            "authorization": authorization,
            "approval_required": (state["state"] == "pending_host_approval"
                                  and authorization["mode"] == "per_operation"),
        }
        if (script is not None and state["state"] == "pending_host_approval"
                and authorization["mode"] == "per_operation"):
            payload["approval_command"] = shlex.join([
                "/usr/bin/python3", str(script), "--state-dir", str(state_dir),
                "--project", PROJECT, "--target", TARGET,
                "--operation-id", state["operation_id"],
            ])
        if (state["state"] == "pending_host_approval"
                and authorization["mode"] == "per_operation"
                and payload["source_review"]["policy"] == "reviewed_snapshot"):
            payload["approval_url"] = (
                TRUSTED_APPROVAL_ORIGIN + "/host-operations"
                "?project=tossApp&target=tarororo-test&host_operation_id="
                + state["operation_id"])
        return payload

    def _result(self, state):
        # Never offer a mutable-source host helper as an approval command.
        # Tests may inject a synthetic approved helper outside projects.
        approval = self.approval_script
        if any(approval.resolve().is_relative_to(Path(root).resolve()) for root in self.projects.values()):
            payload = self._public(state)
        else:
            payload = self._public(state, script=approval, state_dir=self.state_dir)
        # Keep the stored state/receipt contract compatible with older plans. The
        # chat response must distinguish an authorized plan from a web approval.
        if state["state"] == "pending_host_approval":
            if _timestamp(state["immutable"]["expires_at"]) <= datetime.now(timezone.utc):
                payload.update(state="expired", phase="PREPARE", code="PLAN_EXPIRED",
                               approval_required=False)
                payload.pop("approval_url", None)
                payload.pop("approval_command", None)
            elif payload["authorization"]["mode"] == "target":
                try:
                    self._verify_target_authorization(state["immutable"])
                except HostRuntimeError as exc:
                    payload.update(state="blocked", phase="AUTHORIZATION", code=exc.code)
                    payload["guidance"] = (
                        "The plan's pinned target authorization is no longer valid. "
                        "Check the current target authorization and create a new plan; "
                        "do not execute this plan.")
                else:
                    tool = {"api": "execute_test_deploy", "worker": "execute_test_deploy",
                            "migration": "execute_db_migration",
                            "schema_update": "execute_db_migration",
                            "predecessor_migration": "execute_predecessor_migration",
                            "schema014": "execute_schema014_migration"}[
                                payload["component"]]
                    payload.update(state="ready_to_execute", phase="READY")
                    payload["next_action"] = {
                        "tool": tool,
                        "arguments": {key: payload[key]
                                      for key in ("project", "target", "operation_id")},
                    }
                    payload["guidance"] = (
                        "The target is already authorized. If the user requested execution, "
                        "call next_action directly without another management-web approval. "
                        "Execution still validates the source, expiry, runtime and target grant.")
        if payload["state"] == "expired":
            payload["guidance"] = (
                "The plan expired; it is not awaiting approval. Create a new plan with a "
                "new request_id before execution. Do not approve or execute this expired plan.")
        return payload

    def get(self, project, target, operation_id):
        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
            if state["state"] in {"running", "queued", "approved"}:
                # An operation in another MCP process is still legitimate
                # while its cross-process target lock is held. Mark uncertain
                # only if neither this process nor any process owns that lock.
                ours = operation_id in {
                    getattr(task, "operation_id", None) for task in self._tasks
                }
                if not ours and not self._target_lock_busy():
                    state.update(state="outcome_unknown", phase="RECOVER",
                                 code="PROCESS_INTERRUPTED", finished_at=utc_now())
                    self._save(state)
            if (state["state"] == "pending_host_approval" and
                    _timestamp(state["immutable"]["expires_at"]) <= datetime.now(timezone.utc)):
                state.update(state="expired", phase="APPROVAL",
                             code="PLAN_EXPIRED", finished_at=utc_now())
                self._save(state)
            return self._result(state)

    def attestation(self):
        if not self.attestation_path.exists() and not self.attestation_path.is_symlink():
            return None
        data = _read_private_json(self.attestation_path)
        if set(data) != {"source_digest", "image_id", "operation_id"}:
            raise HostRuntimeError("ATTESTATION_INVALID")
        return data

    def compare(self, project, target):
        return self.runtime.compare(project, target, attestation=self.attestation())

    def planning_policy(self, project, target):
        """Describe plan gates without changing source, Docker, DB or operation state."""
        self._require_trusted_runtime()
        self.runtime.guard(project, target)
        reviewed = self._registered_reviewed_snapshot(project, target)
        if reviewed is not None:
            active = self._active_target_authorization(project, target)
            result = {"mode": "reviewed_snapshot",
                    "components": ["api", "worker", "migration", "predecessor_migration", "schema014"],
                    "commit_required": False,
                    "approval": "target" if active else "per_operation"}
            if active:
                result["target_authorization"] = self.target_authorization(project, target)
            return result
        config = read_config(self._config_path())
        legacy = config["legacy"] if is_registered_config(config) else config
        if legacy is None or not legacy["enabled"]:
            raise HostRuntimeError("HOST_RUNTIME_DISABLED")
        components = ["api"]
        if legacy["version"] in (2, 3):
            components.extend(["worker", "migration"])
        if legacy["version"] == 3:
            components.append("predecessor_migration")
        return {"mode": "git_head", "components": components,
                "commit_required": True, "approval": "per_operation"}

    def review_artifact(self, project, target, operation_id):
        """Return a verified private review document to the authenticated admin only."""
        self._require_trusted_runtime()
        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
        review_meta = self._source_review_metadata(state["immutable"])
        if review_meta is None:
            return None
        registered = self._registered_reviewed_snapshot(project, target)
        if registered is None or registered[0].config_digest() != review_meta["config_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        return copy.deepcopy(self._review_data(state["immutable"]))

    def _verify(self, immutable, *, check_expiration=True):
        root = self.runtime.guard(PROJECT, TARGET)
        self._verify_target_authorization(immutable)
        review_meta = self._source_review_metadata(immutable)
        if review_meta is None:
            if (immutable.get("component") in (
                    "worker", "migration", "predecessor_migration", "schema_update")
                    and not extended_enabled(self._config_path())):
                raise HostRuntimeError("EXTENDED_SCOPE_DISABLED")
            if git_state(root)["head"] != immutable["git_head"]:
                raise HostRuntimeError("STALE_PLAN")
            if (immutable.get("component") == "predecessor_migration"
                    and not predecessor_enabled(self._config_path())):
                raise HostRuntimeError("PREDECESSOR_SCOPE_DISABLED")
            verify_reviewed_high_risk_inputs(
                root, include_predecessors=immutable.get("component") in (
                    "predecessor_migration", "schema_update"))
            context = self.context_dir / immutable["operation_id"]
        else:
            registered = self._registered_reviewed_snapshot(PROJECT, TARGET)
            if registered is None or registered[0].config_digest() != review_meta["config_digest"]:
                raise HostRuntimeError("STALE_PLAN")
            if git_state(root)["head"] != immutable["git_head"]:
                raise HostRuntimeError("STALE_PLAN")
            context = self.context_dir / immutable["operation_id"] / "source"
        current = source_inventory(root)
        if not context.is_dir() or context.is_symlink():
            raise HostRuntimeError("STALE_PLAN")
        pinned = source_inventory(context)
        if (current["digest"] != immutable["source_digest"]
                or pinned["digest"] != immutable["source_digest"]):
            raise HostRuntimeError("STALE_PLAN")
        if review_meta is not None:
            try:
                from host_source_review import inventory_source
            except ImportError as exc:
                raise HostRuntimeError("STALE_PLAN") from exc
            inputs = self._legacy_source_inputs()
            if (inventory_source(root, inputs)["digest"] != review_meta["inventory_digest"]
                    or inventory_source(context, inputs)["digest"] != review_meta["inventory_digest"]):
                raise HostRuntimeError("STALE_PLAN")
            self._review_data(immutable)
        if self.runtime.compose_config(context, immutable["mode"]) != immutable["compose_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        running = self.runtime.identities()
        self.runtime._verify_identity(running, health=True)
        for role in ("api", "worker", "db"):
            if _identity(running)[role] != immutable["identities"][role]:
                raise HostRuntimeError("STALE_PLAN")
        if self.runtime.http_health() != "ready":
            raise HostRuntimeError("SERVICE_NOT_READY")
        if immutable["mode"] == "toss-demo" and self.runtime.readiness() != "ready":
            raise HostRuntimeError("SCHEMA_NOT_READY")
        if immutable.get("component") in ("predecessor_migration", "schema_update"):
            observed = self.runtime.predecessor_status(PROJECT, TARGET)
            if (not observed["ready_to_prepare"]
                    or observed["generation"] != immutable.get("database_generation")):
                raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
            expected_observed = immutable.get("migration_checksums", {})
            if immutable.get("component") == "schema_update":
                expected_observed = {name: expected_observed[name]
                                     for name in PREDECESSOR_NAMES[5:12]}
            if observed["checksums"] != expected_observed:
                raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
            for name, expected in immutable["migration_checksums"].items():
                if hashlib.sha256((context / "server/migrations" / name).read_bytes()).hexdigest() != expected:
                    raise HostRuntimeError("STALE_PLAN")
        if immutable.get("component") in ("migration", "schema_update"):
            expected = immutable.get("migration_checksum")
            if hashlib.sha256((context / "server/migrations/013_staged_reading_access.sql")
                              .read_bytes()).hexdigest() != expected:
                raise HostRuntimeError("STALE_PLAN")
        if immutable.get("component") == "schema014":
            # New migration has its own one-use owner approval. In particular,
            # existing 001-013 fixed-target authorization is never inherited.
            if (immutable.get("authorization") != {"mode": "per_operation"}
                    or review_meta is None):
                raise HostRuntimeError("SCHEMA014_APPROVAL_REQUIRED")
            from host_schema014 import checked_source, schema014_status
            _source, digest = checked_source(context)
            observed = schema014_status(self.runtime, PROJECT, TARGET)
            if (digest != immutable.get("migration_checksum")
                    or not observed["ready_to_prepare"]
                    or observed["generation"] != immutable.get("database_generation")
                    or observed["predecessor_checksums"] != immutable.get("migration_checksums")):
                raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
        if (check_expiration and
                _timestamp(immutable["expires_at"]) <= datetime.now(timezone.utc)):
            raise HostRuntimeError("PLAN_EXPIRED")
        return context

    def _retire_static_stale_pending(self, state, root):
        if state["state"] != "pending_host_approval":
            return False
        context = self.context_dir / state["operation_id"]
        if state["immutable"].get("source_review") is not None:
            context /= "source"
        expected = state["immutable"]["source_digest"]
        current = source_inventory(root)
        pinned = source_inventory(context)
        if current["digest"] == expected and pinned["digest"] == expected:
            if git_state(root)["head"] == state["immutable"]["git_head"]:
                return False
        state.update(state="expired", phase="APPROVAL", code="STALE_PLAN",
                     finished_at=utc_now())
        self._save(state)
        (self.operations_dir / (state["operation_id"] + ".approval.json")).unlink(
            missing_ok=True)
        return True

    def _verify_pending_or_retire(self, state):
        try:
            return self._verify(state["immutable"])
        except HostRuntimeError as exc:
            if exc.code == "STALE_PLAN" and state["state"] == "pending_host_approval":
                state.update(state="expired", phase="APPROVAL", code="STALE_PLAN",
                             finished_at=utc_now())
                self._save(state)
                (self.operations_dir / (state["operation_id"] + ".approval.json")).unlink(
                    missing_ok=True)
            raise

    def prepare(self, project, target, component, mode, request_id):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_activation_admitted(
                    project, target, component, mode, request_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_activation_admitted(self, project, target, component, mode, request_id):
        self._require_trusted_runtime()
        root = self.runtime.guard(project, target)
        reviewed = self._registered_reviewed_snapshot(project, target)
        requested_component = component
        authorization = ({"mode": "per_operation"} if component == "schema014" else
                         (self._active_target_authorization(project, target)
                          or {"mode": "per_operation"}))
        if (component not in ("api", "worker", "migration", "predecessor_migration", "schema014") or mode not in ("toss-demo", "disabled")
                or (component in ("worker", "migration", "predecessor_migration", "schema014") and mode != "disabled")
                or not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id)):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        # Schema 014 always requires the registered, explicitly reviewed TEST source.
        # Legacy/git_head deployments and old target grants cannot authorize it.
        if component == "schema014" and reviewed is None:
            raise HostRuntimeError("SCHEMA014_REVIEWED_SOURCE_REQUIRED")
        if reviewed is None and component in ("worker", "migration", "predecessor_migration"):
            scope_config = self.runtime.config_path or Path.home() / ".config/chatgpt-coding-mcp/host-runtime.json"
            if not extended_enabled(scope_config):
                raise HostRuntimeError("EXTENDED_SCOPE_DISABLED")
        if (reviewed is None and component == "predecessor_migration"
                and not predecessor_enabled(self.runtime.config_path or
                                            Path.home() / ".config/chatgpt-coding-mcp/host-runtime.json")):
            raise HostRuntimeError("PREDECESSOR_SCOPE_DISABLED")
        with _lock(self.lock_path):
            for path in self.operations_dir.glob("*.json"):
                private_file(path)
                existing = _read_private_json(path)
                if existing.get("project") == project and existing.get("request_id") == request_id:
                    if (existing.get("immutable", {}).get("mode") != mode
                            or existing.get("immutable", {}).get(
                                "requested_component",
                                existing.get("immutable", {}).get("component", "api"))
                            != requested_component):
                        raise HostRuntimeError("REQUEST_ID_CONFLICT")
                    return self.get_without_lock(project, target, existing["operation_id"])
            if len(list(self.operations_dir.glob("*.json"))) >= MAX_OPERATIONS:
                raise HostRuntimeError("HISTORY_CAPACITY_REACHED")
            for path in self.operations_dir.glob("*.json"):
                existing = _read_private_json(path)
                if existing.get("state") in {"pending_host_approval", "approved", "queued", "running"}:
                    status = existing["state"]
                    if (status == "pending_host_approval"
                            and _timestamp(existing["immutable"]["expires_at"]) <= datetime.now(timezone.utc)):
                        existing.update(state="expired", code="PLAN_EXPIRED",
                                        phase="APPROVAL", finished_at=utc_now())
                        _save_private_json(path, existing)
                    elif (status == "pending_host_approval"
                          and self._retire_static_stale_pending(existing, root)):
                        continue
                    elif status in {"approved", "queued", "running"} and not self._target_lock_busy():
                        existing.update(state="outcome_unknown", code="PROCESS_INTERRUPTED",
                                        phase="RECOVER", finished_at=utc_now())
                        _save_private_json(path, existing)
                    else:
                        raise HostRuntimeError("TARGET_BUSY")
            runtime_before = self.runtime.identities()
            self.runtime._verify_identity(runtime_before, health=True)
            if self.runtime.http_health() != "ready":
                raise HostRuntimeError("SERVICE_NOT_READY")
            if mode == "toss-demo" and self.runtime.readiness() != "ready":
                raise HostRuntimeError("SCHEMA_NOT_READY")
            predecessor = None
            if component == "predecessor_migration":
                predecessor = self.runtime.predecessor_status(project, target)
                if not predecessor["ready_to_prepare"]:
                    raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
            if component == "migration":
                status = self.runtime.migration_status(project, target)
                if (authorization["mode"] == "target" and status["state"] == "missing"
                        and not status["prerequisites_ok"] and not any(status["tables"].values())):
                    predecessor = self.runtime.predecessor_status(project, target)
                    if predecessor["ready_to_prepare"]:
                        component = "schema_update"
                if (component == "migration" and (status["state"] != "missing"
                        or not status["prerequisites_ok"] or any(status["tables"].values()))):
                    raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
                if component == "schema_update" and not predecessor["ready_to_prepare"]:
                    raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
            schema014 = None
            if component == "schema014":
                from host_schema014 import schema014_status
                schema014 = schema014_status(self.runtime, project, target)
                if not schema014["ready_to_prepare"]:
                    raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
            operation_id = secrets.token_hex(16)
            context = self.context_dir / operation_id
            try:
                source_review = None
                if reviewed is None:
                    context.mkdir(mode=0o700)
                    inventory = source_inventory(root, copy_to=context)
                    if source_inventory(root)["digest"] != inventory["digest"]:
                        raise HostRuntimeError("STALE_PLAN")
                    checked = git_state(root)
                    verify_reviewed_high_risk_inputs(
                        root, include_predecessors=component in (
                            "predecessor_migration", "schema_update"))
                    high_risk = (HIGH_RISK_INPUTS | PREDECESSOR_SQL_INPUTS
                                 if component in ("predecessor_migration", "schema_update")
                                 else HIGH_RISK_INPUTS)
                    risky = [path for path in checked["changed_paths"] if path in high_risk]
                    if risky:
                        raise HostRuntimeError("REVIEW_REQUIRED")
                    frozen_root = context
                else:
                    try:
                        from host_source_review import freeze_source
                    except ImportError as exc:
                        raise HostRuntimeError("REGISTRY_NOT_CONFIGURED") from exc
                    registry, _target_spec = reviewed
                    legacy_inventory = source_inventory(root)
                    high_risk = set(HIGH_RISK_INPUTS)
                    if component in ("predecessor_migration", "schema_update"):
                        high_risk |= PREDECESSOR_SQL_INPUTS
                    if component == "schema014":
                        high_risk.add("server/migrations/014_person_relationship_detail.sql")
                    frozen = freeze_source(
                        root, context, review_policy="reviewed_snapshot",
                        inputs=self._legacy_source_inputs(), high_risk_inputs=sorted(high_risk))
                    frozen_root = context / "source"
                    if (source_inventory(root)["digest"] != legacy_inventory["digest"]
                            or source_inventory(frozen_root)["digest"]
                            != legacy_inventory["digest"]):
                        raise HostRuntimeError("STALE_PLAN")
                    fresh = self._registered_reviewed_snapshot(project, target)
                    if fresh is None or fresh[0].config_digest() != registry.config_digest():
                        raise HostRuntimeError("STALE_PLAN")
                    inventory = legacy_inventory
                    checked = {"head": frozen["git_head"],
                               "changed_paths": frozen["changed_paths"]}
                    risky = frozen["high_risk_changes"]
                    source_review = {
                        "policy": "reviewed_snapshot",
                        "review_digest": frozen["review_digest"],
                        "config_digest": registry.config_digest(),
                        "inventory_digest": frozen["source_digest"],
                    }
                compose_digest = self.runtime.compose_config(frozen_root, mode)
                if _identity(self.runtime.identities()) != _identity(runtime_before):
                    raise HostRuntimeError("STALE_PLAN")
                immutable = {
                    "project": project, "target": target, "operation_id": operation_id,
                    "mode": mode, "component": component,
                    "requested_component": requested_component,
                    "authorization": authorization,
                    "git_head": checked["head"],
                    "changed_paths": checked["changed_paths"],
                    "high_risk_changes": risky,
                    "source_digest": inventory["digest"],
                    "source_file_count": inventory["file_count"],
                    "compose_digest": compose_digest,
                    "identities": _identity(runtime_before),
                    "migration_checksum": (
                        hashlib.sha256((frozen_root / "server/migrations/013_staged_reading_access.sql")
                                       .read_bytes()).hexdigest()
                        if component in ("migration", "schema_update") else
                        schema014["checksum"] if schema014 else None),
                    "migration_checksums": (
                        {name: hashlib.sha256((frozen_root / "server/migrations" / name)
                                              .read_bytes()).hexdigest()
                         for name in PREDECESSOR_NAMES}
                        if component == "schema_update" else
                        schema014["predecessor_checksums"] if schema014 else
                        predecessor["checksums"] if predecessor else None),
                    "database_generation": (schema014["generation"] if schema014 else
                                            predecessor["generation"] if predecessor else None),
                    "nonce": secrets.token_hex(32),
                    "expires_at": (datetime.now(timezone.utc) +
                                   timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
                }
                if source_review is not None:
                    immutable["source_review"] = source_review
                    if component in ("predecessor_migration", "schema_update", "schema014"):
                        for name, expected in immutable["migration_checksums"].items():
                            if hashlib.sha256((frozen_root / "server/migrations" / name)
                                              .read_bytes()).hexdigest() != expected:
                                raise HostRuntimeError("STALE_PLAN")
                state = {
                    "version": 1, "project": project, "target": target,
                    "operation_id": operation_id, "request_id": request_id,
                    "state": "pending_host_approval", "phase": "APPROVAL",
                    "created_at": utc_now(), "started_at": None, "finished_at": None,
                    "code": None, "immutable": immutable, "plan_digest": _digest(immutable),
                }
                self._save(state)
                return self._result(state)
            except BaseException:
                shutil.rmtree(context, ignore_errors=True)
                raise

    def get_without_lock(self, project, target, operation_id):
        """Internal only; caller holds operations lock."""
        return self._result(self._load(project, target, operation_id))

    def approve(self, project, target, operation_id, *, input_stream=None, output=None):
        """Interactive, one-use host approval. Never hold state lock on stdin."""
        self._require_trusted_runtime()
        inp = input_stream or sys.stdin
        out = output or sys.stdout
        if not inp.isatty() or not out.isatty():
            raise HostRuntimeError("TTY_REQUIRED")
        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
            if state["state"] != "pending_host_approval":
                raise HostRuntimeError("NOT_PENDING_APPROVAL")
            self._verify_pending_or_retire(state)
            immutable = state["immutable"]
            expected_digest = state["plan_digest"]

        component = immutable["component"]
        print(f"Target: {project}/{target}/{component}", file=out)
        print(f"Mode: {immutable['mode']}", file=out)
        print(f"Git HEAD: {immutable['git_head']}", file=out)
        print(f"Build SHA-256: {immutable['source_digest']}", file=out)
        print(f"Plan SHA-256: {expected_digest}", file=out)
        print(f"Expiry: {immutable['expires_at']}", file=out)
        if component == "predecessor_migration":
            print("Impact: BACKUP+RESTORE CHECK, then only migrations 006-012 to TEST DB.", file=out)
            print("013 is EXCLUDED; no API/Worker/Docker restart or deployment.", file=out)
            print("DB generation: " + json.dumps(immutable["database_generation"]), file=out)
            print("Reviewed 006-012 SQL checksums:", file=out)
            for name, sha in sorted(immutable["migration_checksums"].items()):
                print("  " + name + " SHA-256 " + sha, file=out)
            print("No automatic rollback, restore, or retry on failure.", file=out)
        elif component == "schema014":
            print("Impact: ONE reviewed Schema 014 SQL to tarororo-test after "
                  "full backup, verified disposable restore and exact 001-013 checks.", file=out)
            print("Requires this operation's separate one-use owner approval; "
                  "older 001-013 target grants do NOT apply.", file=out)
            print("No API/Worker/DB restart. No automatic rollback, restore or retry.", file=out)
            print("DB generation: " + json.dumps(immutable["database_generation"]), file=out)
            print("Schema 014 SHA-256: " + immutable["migration_checksum"], file=out)
        elif component == "migration":
            print("Impact: BACKUP then apply exact migration 013 to TEST DB; no service restart.", file=out)
            print("No automatic rollback, restore, or retry on failure.", file=out)
        else:
            print(f"Impact: rebuild image and recreate only {component}. Other services preserved.", file=out)
        print("Rollback requires another separately approved plan.", file=out)
        if immutable["high_risk_changes"]:
            print("HIGH RISK: deployment scripts/config/Dockerfile changed:", file=out)
            for name in immutable["high_risk_changes"]:
                print("  " + json.dumps(name, ensure_ascii=True), file=out)
            print("Review these source diffs in the host terminal before approving.", file=out)
        print("Changed paths (source and untracked):", file=out)
        for name in immutable["changed_paths"][:200]:
            print("  " + json.dumps(name, ensure_ascii=True), file=out)
        print("Type the exact operation ID to approve once:", file=out)
        out.flush()
        if inp.readline().strip() != operation_id:
            raise HostRuntimeError("HOST_APPROVAL_DENIED")

        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
            if (state["state"] != "pending_host_approval"
                    or state["plan_digest"] != expected_digest):
                raise HostRuntimeError("STALE_PLAN")
            self._verify_pending_or_retire(state)
            receipt = self.operations_dir / (operation_id + ".approval.json")
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"operation_id": operation_id, "plan_digest": expected_digest,
                           "nonce": immutable["nonce"], "approved_at": utc_now()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        return {"operation_id": operation_id, "approved": True}

    def _consume_receipt(self, state):
        receipt = self.operations_dir / (state["operation_id"] + ".approval.json")
        value = _read_private_json(receipt)
        expected = {
            "operation_id": state["operation_id"],
            "plan_digest": state["plan_digest"],
            "nonce": state["immutable"]["nonce"],
        }
        if any(value.get(key) != wanted for key, wanted in expected.items()):
            raise HostRuntimeError("INVALID_HOST_APPROVAL")
        if (_timestamp(value.get("approved_at")) < _timestamp(state["created_at"])
                or _timestamp(value.get("approved_at")) > datetime.now(timezone.utc) + timedelta(minutes=1)):
            raise HostRuntimeError("INVALID_HOST_APPROVAL")
        receipt.unlink()

    async def execute(self, project, target, operation_id, *, expected_component=None):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return await self._execute_activation_admitted(
                    project, target, operation_id,
                    expected_component=expected_component)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    async def _execute_activation_admitted(self, project, target, operation_id, *,
                                           expected_component=None):
        self._require_trusted_runtime()
        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
            if (expected_component is not None
                    and state["immutable"].get("component", "api") not in expected_component):
                raise HostRuntimeError("WRONG_OPERATION_TYPE")
            if state["state"] in {"queued", "running", "succeeded", "failed",
                                  "outcome_unknown", "rejected", "expired"}:
                return self._result(state)
        workspace_fd = await asyncio.to_thread(
            acquire_host_operation_lane, self.workspace,
            operation_id=operation_id, kind="host-runtime")
        handed_to_worker = False
        try:
            with host_admission(self.state_dir.parent):
                result = await self._execute_admitted(
                    project, target, operation_id, expected_component=expected_component,
                    workspace_fd=workspace_fd)
                handed_to_worker = result.get("state") == "queued"
                return result
        finally:
            if not handed_to_worker:
                complete_host_operation(
                    self.workspace, operation_id, "host-runtime",
                    termination_verified=True)
            if workspace_fd is not None:
                os.close(workspace_fd)

    async def _execute_admitted(self, project, target, operation_id, *, expected_component=None,
                                workspace_fd=None):
        self._require_trusted_runtime()
        target_fd = worker_workspace_fd = None
        try:
            with _lock(self.lock_path):
                state = self._load(project, target, operation_id)
                if (expected_component is not None
                        and state["immutable"].get("component", "api") not in expected_component):
                    raise HostRuntimeError("WRONG_OPERATION_TYPE")
                if state["state"] in {"queued", "running", "succeeded", "failed",
                                      "outcome_unknown", "rejected", "expired"}:
                    return self._result(state)
                if state["state"] != "pending_host_approval":
                    raise HostRuntimeError("NOT_PENDING_APPROVAL")
                # Own the cross-process target lock before declaring the job
                # queued. Another MCP process must not mark it interrupted.
                target_fd = self._acquire_target_lock()
                self._verify_pending_or_retire(state)
                if workspace_fd is not None:
                    worker_workspace_fd = os.dup(workspace_fd)
                target_authorized = (state["immutable"].get(
                    "authorization", {"mode": "per_operation"}).get("mode") == "target")
                if target_authorized:
                    # Linearize final grant validation and queue acceptance with
                    # revocation. The short lock ends before the worker/build starts.
                    with _lock(self.target_authorizations_lock_path):
                        self._verify_target_authorization(state["immutable"])
                        state.update(state="queued", phase="QUEUED", code=None,
                                     started_at=utc_now())
                        self._save(state)
                else:
                    self._consume_receipt(state)
                    state.update(state="queued", phase="QUEUED", code=None,
                                 started_at=utc_now())
                    self._save(state)
            work = asyncio.create_task(
                asyncio.to_thread(
                    self._run_deploy_with_lanes,
                    operation_id, target_fd, worker_workspace_fd))
            work.operation_id = operation_id
            self._tasks.add(work)
            work.add_done_callback(self._tasks.discard)
            target_fd = None  # The worker owns this file descriptor.
            worker_workspace_fd = None
            return self._result(state)
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if worker_workspace_fd is not None:
                os.close(worker_workspace_fd)

    def _record(self, operation_id, **updates):
        with _lock(self.lock_path):
            state = self._load(PROJECT, TARGET, operation_id)
            state.update(updates)
            self._save(state)
            return state

    def _run_deploy(self, operation_id, target_lock_fd):
        try:
            self._run_deploy_body(operation_id)
        finally:
            os.close(target_lock_fd)

    def _run_deploy_with_lanes(self, operation_id, target_lock_fd, workspace_fd):
        try:
            self._run_deploy(operation_id, target_lock_fd)
        finally:
            try:
                try:
                    terminal = self._load(PROJECT, TARGET, operation_id)
                    if terminal["state"] in {"succeeded", "failed", "rejected"}:
                        complete_host_operation(
                            self.workspace, operation_id, "host-runtime",
                            termination_verified=True)
                except (HostRuntimeError, OSError):
                    self._record_reservation_release_failure(operation_id)
            finally:
                if workspace_fd is not None:
                    os.close(workspace_fd)

    def _record_reservation_release_failure(self, operation_id):
        _LOGGER.error(_RESERVATION_RELEASE_FAILED)
        try:
            self._record(
                operation_id, state="outcome_unknown", phase="RECOVER",
                code=_RESERVATION_RELEASE_FAILED, finished_at=utc_now())
        except (HostRuntimeError, OSError):
            _LOGGER.error("WORKSPACE_HOST_RESERVATION_STATE_WRITE_FAILED")

    def _run_deploy_body(self, operation_id):
        phase = "PRECHECK"
        side_effects_possible = False
        try:
            state = self._record(operation_id, state="running", phase=phase)
            immutable = state["immutable"]
            context = self._verify(immutable)
            phase = "CHECK"
            self._record(operation_id, phase=phase)
            # Never execute manage.py from a writable project, even after
            # copying it. It would have full access to host credentials.
            self.runtime.check_deploy_secrets()
            if self.runtime.compose_config(context, immutable["mode"]) != immutable["compose_digest"]:
                raise HostRuntimeError("STALE_PLAN")
            # A target grant is revocable. Revalidate it at the final boundary
            # immediately before any backup, SQL, build, tag or container change.
            self._verify_target_authorization(immutable)
            if immutable["component"] == "schema014":
                phase = "BACKUP_AND_MIGRATE_014"
                self._record(operation_id, phase=phase)
                side_effects_possible = True
                from host_schema014 import migrate_schema014
                outcome = migrate_schema014(
                    self.runtime, self.state_dir, immutable, context, operation_id)
                self._record(
                    operation_id, state="succeeded", phase="COMPLETE", code=None,
                    backup_sha256=outcome["backup_sha256"],
                    backup_bytes=outcome["backup_bytes"],
                    postcommit_counts_changed=outcome["postcommit_counts_changed"],
                    finished_at=utc_now(),
                )
                return
            if immutable["component"] == "schema_update":
                phase = "BACKUP_AND_MIGRATE_006_013"
                self._record(operation_id, phase=phase)
                side_effects_possible = True
                from host_db_migration import migrate_schema_update
                outcome = migrate_schema_update(
                    self.runtime, self.state_dir, immutable, context, operation_id)
                self._record(operation_id, state="succeeded", phase="COMPLETE", code=None,
                             backup_sha256=outcome["backup_sha256"],
                             backup_bytes=outcome["backup_bytes"], finished_at=utc_now())
                return
            if immutable["component"] == "predecessor_migration":
                phase = "BACKUP_AND_MIGRATE_006_012"
                self._record(operation_id, phase=phase)
                side_effects_possible = True
                from host_db_migration import migrate_predecessors
                outcome = migrate_predecessors(self.runtime, self.state_dir,
                                               immutable, context, operation_id)
                self._record(operation_id, state="succeeded", phase="COMPLETE", code=None,
                             backup_sha256=outcome["backup_sha256"],
                             backup_bytes=outcome["backup_bytes"], finished_at=utc_now())
                return
            if immutable["component"] == "migration":
                phase = "BACKUP_AND_MIGRATE"
                self._record(operation_id, phase=phase)
                side_effects_possible = True
                from host_db_migration import migrate_013
                outcome = migrate_013(self.runtime, self.state_dir, immutable, context, operation_id)
                self._record(operation_id, state="succeeded", phase="COMPLETE", code=None,
                             backup_sha256=outcome["backup_sha256"],
                             backup_bytes=outcome["backup_bytes"], finished_at=utc_now())
                return
            # Only a fixed Docker Compose subcommand, with validated
            # service and an immutable snapshot as its build context.
            phase = "BUILD"
            self._record(operation_id, phase=phase)
            side_effects_possible = True  # An interrupted build can retag images.
            component = immutable["component"]
            self.runtime.run([
                "docker", "compose", "--project-name", TARGET,
                "--env-file", "/dev/null", "--file",
                str(context / "deploy/pc-test/compose.yaml"),
                "build", component,
            ], cwd=context, mode=immutable["mode"],
               timeout=900, capture=False)
            image = _parse_json(self.runtime.run(
                ["docker", "image", "inspect", "tarororo-pc-test:local"]))
            if (not isinstance(image, list) or len(image) != 1
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", image[0].get("Id", ""))):
                raise HostRuntimeError("BUILD_IMAGE_UNKNOWN")
            built_image = image[0]["Id"]
            # The shared image tag may be replaced between build and recreate.
            # Create a unique, operation-scoped tag and verify an image-only
            # Compose override before any container mutation.
            pinned_name = f"tarororo-hostruntime-{operation_id}:local"
            self.runtime.run(["docker", "image", "tag", built_image, pinned_name],
                             timeout=15, capture=False)
            pinned = _parse_json(self.runtime.run(
                ["docker", "image", "inspect", pinned_name]))
            if (not isinstance(pinned, list) or len(pinned) != 1
                    or pinned[0].get("Id") != built_image):
                raise HostRuntimeError("BUILD_IMAGE_CHANGED")
            override_file = context / (".hostruntime-" + operation_id + ".yaml")
            descriptor = os.open(override_file, os.O_CREAT | os.O_EXCL |
                                 os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                stream.write(f"services:\\n  {component}:\\n    image: ".replace("\\n", "\n") +
                             json.dumps(pinned_name) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            base_argv = [
                "docker", "compose", "--project-name", TARGET,
                "--env-file", "/dev/null", "--file",
                str(context / "deploy/pc-test/compose.yaml"),
            ]
            expected = _parse_json(self.runtime.run(
                [*base_argv, "config", "--format", "json"], cwd=context,
                mode=immutable["mode"], timeout=12))
            merged_argv = [*base_argv, "--file", str(override_file)]
            observed = _parse_json(self.runtime.run(
                [*merged_argv, "config", "--format", "json"], cwd=context,
                mode=immutable["mode"], timeout=12))
            if not isinstance(expected, dict) or not isinstance(observed, dict):
                raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
            expected["services"][component]["image"] = pinned_name
            if observed != expected:
                raise HostRuntimeError("UNSAFE_IMAGE_OVERRIDE")
            # Plan expiry controls approval/start, not a long-running authorized build.
            self._verify(immutable, check_expiration=False)
            phase = "DEPLOY"
            self._record(operation_id, phase=phase)
            self.runtime.run([
                *merged_argv, "up", "-d", "--no-deps", "--no-build",
                "--pull", "never", "--force-recreate",
                "--wait", "--wait-timeout", "60", component,
            ], cwd=context, mode=immutable["mode"], timeout=120,
               capture=False)
            phase = "POSTCHECK"
            self._record(operation_id, phase=phase)
            after = self.runtime.identities()
            self.runtime._verify_identity(after, expected_mode=immutable["mode"], health=True)
            if (after[component]["image_id"] != built_image
                    or any(_identity(after)[key] != immutable["identities"][key]
                           for key in ("api", "worker", "db") if key != component)
                    or self.runtime.http_health() != "ready"
                    or (immutable["mode"] == "toss-demo" and self.runtime.readiness() != "ready")):
                raise HostRuntimeError("POSTCHECK_FAILED")
            if component == "api":
                _save_private_json(self.attestation_path, {
                    "operation_id": operation_id,
                    "source_digest": immutable["source_digest"], "image_id": built_image,
                })
            self._record(operation_id, state="succeeded", phase="COMPLETE", code=None,
                         finished_at=utc_now())
        except HostRuntimeError as exc:
            state_name = ("outcome_unknown" if side_effects_possible else
                          "rejected" if exc.code in {"STALE_PLAN", "PLAN_EXPIRED"} else "failed")
            self._record(operation_id, state=state_name, phase=phase,
                         code=exc.code, finished_at=utc_now())
        except Exception:
            self._record(operation_id, state=("outcome_unknown" if side_effects_possible else "failed"),
                         phase=phase, code="HOST_OPERATION_FAILED", finished_at=utc_now())

    def close(self):
        """No auto-retry or cancellation: active operations become uncertain."""
        for task in list(self._tasks):
            if not task.done():
                # A thread may still be running; do not claim it stopped.
                pass
