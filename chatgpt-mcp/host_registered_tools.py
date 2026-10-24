"""MCP tools for registered (version 3) host targets.

Schemas only narrow input; the registry, validate_action and RegisteredHostOperations
check everything again server-side. Results carry public metadata only and errors are
sanitized codes. Importing this module is the precondition for listing the tools: a
runtime without the registry, actions or manager modules simply does not offer them.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
from pathlib import Path
import re

from host_actions import ACTIONS
from host_compose_actions import ComposeActionAdapter
from host_registered_operations import RegisteredHostOperations
from host_registry import HostTargetRegistry
from host_runtime_config import CONFIG_PATH, PROJECT, TARGET, HostRuntimeError
from host_runtime_operations import operation_path

TARGET_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
PUBLIC_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
DIAGNOSTIC_ID = re.compile(r"[0-9a-f]{32}\Z")
# Read-only inspection areas and their adapters (the PostgreSQL task adds "database").
INSPECT_ADAPTERS = {"docker": ComposeActionAdapter}

_PROJECT = {"type": "string"}  # The server fills the allowed project enum.
_TARGET = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,62}$"}
_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}
_PARAMETERS = {
    "type": "object",
    "properties": {
        "services": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                     "items": {"type": "string",
                               "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"}},
        "profile": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,62}$"},
    },
    "additionalProperties": False,
    "description": ("Exact fields of the action: services for build/apply/up/restart/stop, "
                    "{} for down, profile for exec. Unknown fields are refused."),
}
REQUIRED = {
    "list_host_targets": ("project",),
    "inspect_host_target": ("project", "target", "area"),
    "prepare_host_operation": ("project", "target", "action", "parameters", "request_id"),
    "execute_host_operation": ("project", "target", "operation_id"),
    "read_host_operation_events": ("project", "target", "operation_id"),
}
OPTIONAL = {"read_host_operation_events": ("cursor", "limit")}


def _tool(name, title, description, fields, *, readonly):
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"project": dict(_PROJECT), **copy.deepcopy(fields)},
            "required": list(REQUIRED[name]), "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": readonly,
            "destructiveHint": not readonly,
            "idempotentHint": readonly,
            "openWorldHint": False,
        },
    }


HOST_REGISTERED_TOOL_SPECS = [
    _tool("list_host_targets", "등록 호스트 대상 조회",
          "List the host targets registered for this project in the version 3 host "
          "configuration, their allowed actions and any restriction reason. Read-only.",
          {}, readonly=True),
    _tool("inspect_host_target", "등록 호스트 대상 상태 조회",
          "Read the registered target's Docker identities and health only. No logs, "
          "environment, mounts or credentials are returned.",
          {"target": _TARGET, "area": {"type": "string", "enum": list(INSPECT_ADAPTERS)}},
          readonly=True),
    _tool("prepare_host_operation", "호스트 작업 승인 계획",
          "Freeze inputs and target identity into a plan that expires in 7 days and "
          "return its admin-web approval link. Runs no Docker or SQL. The same request_id "
          "with the same input returns the same plan.",
          {"target": _TARGET, "action": {"type": "string", "enum": list(ACTIONS)},
           "parameters": _PARAMETERS, "request_id": _REQUEST},
          readonly=False),
    _tool("execute_host_operation", "승인된 호스트 작업 실행",
          "Start a plan only after a human approved it in the admin web; the one-use receipt "
          "is consumed. Repeated calls report the existing state and never run it again.",
          {"target": _TARGET, "operation_id": _ID}, readonly=False),
    _tool("read_host_operation_events", "호스트 작업 이벤트 조회",
          "Read the operation state, phase and safe events after a cursor (default 50, "
          "max 200). Queued, running or unknown outcomes are never success.",
          {"target": _TARGET, "operation_id": _ID,
           "cursor": {"type": "integer", "minimum": 0},
           "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
          readonly=True),
]


def create_registered_manager(projects, state_dir=None, workspace=None):
    """Production wiring: no injected runner or adapter, so trust requires the verified
    immutable installation."""
    return RegisteredHostOperations(projects, HostTargetRegistry(projects, CONFIG_PATH),
                                    state_dir=state_dir, workspace=workspace)


def public_code(error) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and PUBLIC_CODE.fullmatch(code) else "HOST_OPERATION_FAILED"


def public_target(spec) -> dict:
    """Registered target metadata safe for chat and the admin page (no env, paths or secrets)."""
    return {
        "target": spec["id"], "environment": spec["environment"],
        "compose_project": spec["compose"]["project_name"],
        "services": list(spec["compose"]["services"]),
        "db_services": list(spec["compose"]["db_services"]),
        "actions": [action for action in ACTIONS if action in spec["capabilities"]],
        "capabilities": list(spec["capabilities"]),
        "exec_profiles": [{"profile": name, "service": profile["service"]}
                          for name, profile in sorted(spec["exec_profiles"].items())],
        "source_review": spec["source_review"],
        "inspect_areas": list(INSPECT_ADAPTERS),
    }


def list_targets(get_manager, project) -> dict:
    """Availability first: a missing, disabled or invalid registry or an unverified runtime
    is reported as a reason instead of an error."""
    try:
        manager = get_manager()
        registry = manager.registry.reload()
    except HostRuntimeError as exc:
        return {"project": project, "enabled": False, "reason": public_code(exc), "targets": []}
    except OSError:
        return {"project": project, "enabled": False, "reason": "STATE_UNAVAILABLE", "targets": []}
    reason = registry.availability()["reason"]
    if reason is None:
        try:
            manager._require_trusted_runtime()
        except HostRuntimeError as exc:
            reason = exc.code
    return {"project": project, "enabled": reason is None, "reason": reason,
            "targets": [public_target(spec) for spec in registry.list_targets(project)]}


def inspect_target(manager, project, target, area) -> dict:
    """Read-only identity and health of a registered target; runs host commands only from
    the verified runtime."""
    manager._require_trusted_runtime()
    spec = manager.registry.reload().require_target(project, target)
    adapter = INSPECT_ADAPTERS[area](manager.runner)
    result = manager._adapter_call("inspect", None, adapter.inspect_target, copy.deepcopy(spec))
    return {"project": project, "area": area, **result}


class RegisteredHostTools:
    """Routes the registered tools. The manager is built lazily, so a broken host config
    closes only the registered scope and is retried on the next call."""

    def __init__(self, projects, state_dir, project_locks, workspace=None):
        self.projects = projects
        self.state_dir = Path(state_dir)
        self.project_locks = project_locks
        self.names = {spec["name"] for spec in HOST_REGISTERED_TOOL_SPECS}
        self._manager = None
        self.workspace = workspace

    def manager(self):
        if self._manager is None:
            factory_accepts_workspace = "workspace" in inspect.signature(
                create_registered_manager).parameters
            if self.workspace is None or not factory_accepts_workspace:
                self._manager = create_registered_manager(self.projects, self.state_dir)
                if self.workspace is not None:
                    self._manager.workspace = self.workspace
            else:
                self._manager = create_registered_manager(
                    self.projects, self.state_dir, workspace=self.workspace)
        return self._manager

    def registered_operation_path(self, operation_id) -> Path:
        # RegisteredHostOperations layout: <state>/registered/operations/<id>.json
        return operation_path(self.state_dir / "registered" / "operations", operation_id)

    def route_get(self, legacy_ops, operation_id):
        """Legacy get_host_operation: a legacy record keeps its exact behavior, a registered
        tossApp/tarororo-test record is read by the registered manager (which verifies its
        kind), and an ID stored by both backends is refused."""
        legacy_path = legacy_ops._path(operation_id)
        registered_path = self.registered_operation_path(operation_id)
        try:
            in_legacy = legacy_path.exists() or legacy_path.is_symlink()
            in_registered = registered_path.exists() or registered_path.is_symlink()
        except OSError:
            raise HostRuntimeError("STATE_UNAVAILABLE") from None
        if in_legacy and in_registered:
            raise HostRuntimeError("AMBIGUOUS_OPERATION_ID")
        if in_registered:
            return self.manager().get(PROJECT, TARGET, operation_id)
        return legacy_ops.get(PROJECT, TARGET, operation_id)

    async def call(self, name, arguments) -> dict:
        try:
            return await self._call(name, arguments)
        except HostRuntimeError as exc:
            code = public_code(exc)
            diagnostic = getattr(exc, "diagnostic_id", None)
            if (code == "HOST_OPERATION_FAILED" and isinstance(diagnostic, str)
                    and DIAGNOSTIC_ID.fullmatch(diagnostic)):
                code += " diagnostic_id=" + diagnostic
            raise ValueError(code) from None
        except OSError:
            raise ValueError("STATE_UNAVAILABLE") from None
        except Exception:
            raise ValueError("HOST_OPERATION_FAILED") from None

    async def _call(self, name, arguments) -> dict:
        required = set(REQUIRED[name])
        if not required <= set(arguments) <= required | set(OPTIONAL.get(name, ())):
            raise HostRuntimeError("INVALID_HOST_ARGUMENTS")
        project = arguments["project"]
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if name == "list_host_targets":
            return await asyncio.to_thread(list_targets, self.manager, project)
        target = arguments["target"]
        if not isinstance(target, str) or not TARGET_PATTERN.fullmatch(target):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if name == "inspect_host_target" and (not isinstance(arguments["area"], str)
                                              or arguments["area"] not in INSPECT_ADAPTERS):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        manager = await asyncio.to_thread(self.manager)
        if name == "inspect_host_target":
            return await asyncio.to_thread(inspect_target, manager, project, target,
                                           arguments["area"])
        if name == "prepare_host_operation":
            lock = self.project_locks[project]
            if lock.locked():
                raise HostRuntimeError("PROJECT_BUSY")
            async with lock:
                return await asyncio.to_thread(
                    manager.prepare, project, target, action=arguments["action"],
                    parameters=arguments["parameters"], request_id=arguments["request_id"])
        if name == "execute_host_operation":
            return await manager.execute(project, target, arguments["operation_id"])
        return await asyncio.to_thread(
            manager.events, project, target, arguments["operation_id"],
            cursor=arguments.get("cursor", 0), limit=arguments.get("limit", 50))
