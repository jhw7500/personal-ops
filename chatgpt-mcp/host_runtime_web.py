"""Narrow local-browser approval bridge for immutable host-runtime plans.

This writes only the existing single-use approval receipt. Deployment execution
remains a separate MCP action with pre/post identity checks. No generic commands.
Registered (version 3) operations are delegated to RegisteredHostOperations; nothing
here changes the host activation configuration of registered targets.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import json
import os
import re

from host_runtime_config import (
    HostRuntimeError, PROJECT, REGISTERED_MODULES, TARGET, is_registered_config,
)
from host_runtime_operations import _lock, _read_private_json, utc_now, _timestamp

try:
    from host_registered_operations import _no_raw_os_errors
except ModuleNotFoundError as exc:
    if exc.name not in REGISTERED_MODULES:
        raise
    def _no_raw_os_errors(function):
        """Missing optional modules close registered operations, not the legacy web bridge."""
        @wraps(function)
        def unavailable(*args, **kwargs):
            raise HostRuntimeError("REGISTERED_OPERATIONS_UNAVAILABLE")
        return unavailable

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_REGISTERED_LISTED = 20
REGISTERED_EVENT_PAGE = 50


def _safe_source_review(immutable):
    review = immutable.get("source_review")
    if review is None:
        return None
    if not isinstance(review, dict) or review.get("policy") not in {"git_head", "reviewed_snapshot"}:
        raise HostRuntimeError("PLAN_INVALID")
    if review["policy"] == "git_head":
        if set(review) != {"policy"}:
            raise HostRuntimeError("PLAN_INVALID")
        return {"policy": "git_head"}
    keys = {"policy", "review_digest", "config_digest", "inventory_digest"}
    if set(review) != keys or any(not isinstance(review[key], str) or not HEX64.fullmatch(review[key])
                                  for key in keys - {"policy"}):
        raise HostRuntimeError("PLAN_INVALID")
    return {key: review[key] for key in ("policy", "review_digest", "config_digest",
                                         "inventory_digest")}


def _safe_authorization(immutable):
    authorization = immutable.get("authorization", {"mode": "per_operation"})
    if authorization == {"mode": "per_operation"}:
        return {"mode": "per_operation"}
    if (not isinstance(authorization, dict)
            or set(authorization) != {"mode", "grant_id", "config_digest"}
            or authorization.get("mode") != "target"
            or not isinstance(authorization.get("grant_id"), str)
            or not OPERATION_ID.fullmatch(authorization["grant_id"])
            or not isinstance(authorization.get("config_digest"), str)
            or not HEX64.fullmatch(authorization["config_digest"])):
        raise HostRuntimeError("PLAN_INVALID")
    return {key: authorization[key] for key in ("mode", "grant_id", "config_digest")}


def pending_operations(operations):
    operations._require_trusted_runtime()
    operations.runtime.guard(PROJECT, TARGET)
    planning_policy = operations.planning_policy(PROJECT, TARGET)
    plans = []
    with _lock(operations.lock_path):
        for path in sorted(operations.operations_dir.glob("*.json")):
            state = _read_private_json(path)
            if state.get("state") != "pending_host_approval":
                continue
            immutable = state.get("immutable", {})
            if _timestamp(immutable.get("expires_at")) <= datetime.now(timezone.utc):
                continue
            plan = {
                "operation_id": state["operation_id"],
                "component": immutable["component"], "mode": immutable["mode"],
                "plan_digest": state["plan_digest"],
                "source_digest": immutable["source_digest"],
                "expires_at": immutable["expires_at"],
                "impact": {"recreate": None if immutable["component"] in (
                                           "migration", "predecessor_migration", "schema_update", "schema014")
                                      else immutable["component"],
                           "preserve": (["api", "worker"]
                                        if immutable["component"] in (
                                            "migration", "predecessor_migration", "schema_update", "schema014")
                                        else [x for x in ("api", "worker", "db")
                                              if x != immutable["component"]]),
                           "schema_change": ("013_staged_reading_access.sql"
                                            if immutable["component"] == "migration" else
                                            "006-012_only" if immutable["component"] == "predecessor_migration"
                                            else "006-013_combined"
                                            if immutable["component"] == "schema_update"
                                            else "014_person_relationship_detail.sql"
                                            if immutable["component"] == "schema014"
                                            else None)},
                "high_risk_changes": immutable["high_risk_changes"],
                "changed_path_count": len(immutable["changed_paths"]),
                "migration_range": (["006", "012"]
                                    if immutable["component"] == "predecessor_migration" else
                                    ["006", "013"]
                                    if immutable["component"] == "schema_update" else None),
                "migration_checksums": immutable.get("migration_checksums")
                    if immutable["component"] in ("predecessor_migration", "schema_update", "schema014") else None,
                "database_generation": immutable.get("database_generation")
                    if immutable["component"] in ("predecessor_migration", "schema_update", "schema014") else None,
                "authorization": _safe_authorization(immutable),
            }
            source_review = _safe_source_review(immutable)
            if source_review is not None:
                plan["source_review"] = source_review
            plans.append(plan)
    return {"project": PROJECT, "target": TARGET, "planning_policy": planning_policy,
            "plans": plans[:20]}


def host_operation_review(operations, project, operation_id):
    """Private reviewed-snapshot diff for the fixed legacy target's admin page."""
    if (project != PROJECT or not isinstance(operation_id, str)
            or not OPERATION_ID.fullmatch(operation_id)):
        raise HostRuntimeError("HOST_APPROVAL_DENIED")
    review = operations.review_artifact(project, TARGET, operation_id)
    return {"project": project, "target": TARGET, "operation_id": operation_id,
            "review": review}


def _target_authorization_scope(project, target):
    if project != PROJECT or target != TARGET:
        raise HostRuntimeError("UNSUPPORTED_SCOPE")


def target_authorization(operations, project, target):
    """Read the durable authorization for the one fixed reviewed-snapshot target."""
    _target_authorization_scope(project, target)
    reader = getattr(operations, "target_authorization", None)
    if reader is None:
        return {"supported": False, "enabled": False, "project": project,
                "target": target, "components": [], "grant_id": None,
                "granted_at": None, "reason": "AUTHORIZATION_UNAVAILABLE"}
    return reader(project, target)


def set_target_authorization(operations, project, target, *, enabled, confirmation):
    """Persist or revoke the target grant; this never prepares or executes an operation."""
    _target_authorization_scope(project, target)
    if type(enabled) is not bool or confirmation != project + "/" + target:
        raise HostRuntimeError("HOST_APPROVAL_DENIED")
    setter = getattr(operations, "set_target_authorization", None)
    if setter is None:
        raise HostRuntimeError("AUTHORIZATION_UNAVAILABLE")
    return setter(project, target, enabled=enabled, confirmation=confirmation)


def enable_extended_scope(operations, confirmation):
    """Local authenticated browser grants fixed Worker/013 scope, never via MCP."""
    from pathlib import Path
    from host_runtime_config import (CONFIG_PATH, TARGET_CONFIG_V2, load_config,
                                     extended_enabled, read_config)
    import secrets
    import stat
    from host_runtime_config import private_file, _no_parent_links
    operations._require_trusted_runtime()
    path = operations.runtime.config_path or CONFIG_PATH
    if confirmation != "ENABLE WORKER AND DB 013":
        raise HostRuntimeError("HOST_APPROVAL_DENIED")
    if is_registered_config(read_config(path)):
        raise HostRuntimeError("UNSUPPORTED_CONFIG_VERSION")
    from host_runtime_config import config_write_lock
    with config_write_lock(Path(path).absolute()):
        if confirmation != "ENABLE WORKER AND DB 013":
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        if is_registered_config(read_config(path)):
            raise HostRuntimeError("UNSUPPORTED_CONFIG_VERSION")
        if not load_config(path):
            raise HostRuntimeError("HOST_RUNTIME_DISABLED")
        if extended_enabled(path):
            return {"scope": "worker,db-013", "enabled": True, "changed": False}
        # Atomic, owner-only replacement; the config directory can be 0700 or
        # owner-owned 0755, but must never be group/world writable or symlinked.
        path = Path(path).absolute()
        _no_parent_links(path)
        parent = path.parent
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise HostRuntimeError("UNSAFE_STATE_PATH")
        private_file(path)
        temporary = parent / ("." + secrets.token_hex(16) + ".host-upgrade")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 2, "enabled": True,
                           "targets": {TARGET: TARGET_CONFIG_V2}}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            # Reject a concurrent operator replacement instead of overwriting it.
            current = read_config(path)
            if current is None or current["version"] != 1 or not current["enabled"]:
                raise HostRuntimeError("STALE_CONFIG")
            os.replace(temporary, path)
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        if not extended_enabled(path):
            raise HostRuntimeError("EXTENDED_SCOPE_DISABLED")
        return {"scope": "worker,db-013", "enabled": True, "changed": True}




def enable_predecessor_scope(operations, confirmation):
    """Explicit local-only v3 opt-in; old Worker/013 v2 is not sufficient."""
    from pathlib import Path
    import secrets
    import stat
    from host_runtime_config import (
        CONFIG_PATH, TARGET_CONFIG_V3, extended_enabled,
        predecessor_enabled, private_file, _no_parent_links, read_config,
    )

    operations._require_trusted_runtime()
    path = Path(operations.runtime.config_path or CONFIG_PATH).absolute()
    if confirmation != "ENABLE DB 006-012":
        raise HostRuntimeError("HOST_APPROVAL_DENIED")
    if is_registered_config(read_config(path)):
        raise HostRuntimeError("UNSUPPORTED_CONFIG_VERSION")
    from host_runtime_config import config_write_lock
    with config_write_lock(Path(path).absolute()):
        if confirmation != "ENABLE DB 006-012":
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        if is_registered_config(read_config(path)):
            raise HostRuntimeError("UNSUPPORTED_CONFIG_VERSION")
        if not extended_enabled(path):
            raise HostRuntimeError("EXTENDED_SCOPE_DISABLED")
        if predecessor_enabled(path):
            return {"scope": "db-006-012", "enabled": True, "changed": False}

        _no_parent_links(path)
        parent = path.parent
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise HostRuntimeError("UNSAFE_STATE_PATH")
        private_file(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = stream.read(8193)
        if len(before) > 8192:
            raise HostRuntimeError("INVALID_HOST_CONFIG")
        temp = parent / ("." + secrets.token_hex(16) + ".predecessor-upgrade")
        new_config = {"version": 3, "enabled": True,
                      "targets": {TARGET: TARGET_CONFIG_V3}}
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(new_config, stream)
                stream.flush()
                os.fsync(stream.fileno())
            # Do not replace concurrent edits, even a still-valid v2 setting.
            private_file(path)
            if (path.read_bytes() != before or is_registered_config(read_config(path))
                    or not extended_enabled(path) or predecessor_enabled(path)):
                raise HostRuntimeError("STALE_CONFIG")
            os.replace(temp, path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)
        if not predecessor_enabled(path):
            raise HostRuntimeError("PREDECESSOR_SCOPE_DISABLED")
        return {"scope": "db-006-012", "enabled": True, "changed": True}

def approve_web(operations, *, operation_id, plan_digest, source_digest, confirmation):
    """Issue a receipt for an authenticated admin or trusted MCP App capability."""
    operations._require_trusted_runtime()
    if (not isinstance(operation_id, str) or not operations._path(operation_id)
            or not isinstance(plan_digest, str) or not HEX64.fullmatch(plan_digest)
            or not isinstance(source_digest, str) or not HEX64.fullmatch(source_digest)
            or confirmation != operation_id):
        raise HostRuntimeError("HOST_APPROVAL_DENIED")
    with _lock(operations.lock_path):
        state = operations._load(PROJECT, TARGET, operation_id)
        if state["state"] != "pending_host_approval":
            raise HostRuntimeError("NOT_PENDING_APPROVAL")
        immutable = state["immutable"]
        if (state["plan_digest"] != plan_digest
                or immutable["source_digest"] != source_digest
                or _timestamp(immutable["expires_at"]) <= datetime.now(timezone.utc)):
            raise HostRuntimeError("STALE_PLAN")
        operations._verify_pending_or_retire(state)
        receipt = operations.operations_dir / (operation_id + ".approval.json")
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"operation_id": operation_id, "plan_digest": plan_digest,
                       "nonce": immutable["nonce"], "approved_at": utc_now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return {"operation_id": operation_id, "approved": True,
                "scope": "tossApp/tarororo-test/" + immutable["component"]}


def _registered_scope(operations, project, target):
    """Registered web views need the verified runtime and a target registered to this
    project in a freshly read registry (never a hard-coded project)."""
    operations._require_trusted_runtime()
    operations.registry.reload().require_target(project, target)


@_no_raw_os_errors
def registered_operations(operations, project, target):
    """Recent registered operations of one target in every state, newest first."""
    _registered_scope(operations, project, target)
    with _lock(operations.lock_path):
        plans = [operations._public(operations._refresh(state)) for state in operations._states()
                 if state["project"] == project and state["target"] == target]
    plans.sort(key=lambda plan: plan["created_at"], reverse=True)
    return {"project": project, "target": target, "server_time": utc_now(),
            "operations": plans[:MAX_REGISTERED_LISTED]}


@_no_raw_os_errors
def registered_operation(operations, project, target, operation_id, cursor=0):
    """Public plan, the private high-risk review artifact (reviewed_snapshot only) and one
    cursor page of events, for the authenticated admin page."""
    _registered_scope(operations, project, target)
    plan = operations.get(project, target, operation_id)
    review = (operations.review_artifact(project, target, operation_id)
              if plan["source"]["review_digest"] is not None else None)
    page = operations.events(project, target, operation_id, cursor=cursor,
                             limit=REGISTERED_EVENT_PAGE)
    return {"project": project, "target": target, "server_time": utc_now(),
            "operation": plan, "review": review, "events": page}


@_no_raw_os_errors
def approve_registered(operations, project, target, *, operation_id, plan_digest, source_digest,
                       confirmation):
    """Only caller: the fresh-auth admin endpoint. Issues the one-use receipt; never executes."""
    _registered_scope(operations, project, target)
    return operations.approve_web(project, target, operation_id=operation_id,
                                  plan_digest=plan_digest, source_digest=source_digest,
                                  confirmation=confirmation)


@_no_raw_os_errors
def reject_registered(operations, project, target, *, operation_id, confirmation):
    """Only caller: the fresh-auth admin endpoint."""
    _registered_scope(operations, project, target)
    return operations.reject_web(project, target, operation_id=operation_id,
                                 confirmation=confirmation)
