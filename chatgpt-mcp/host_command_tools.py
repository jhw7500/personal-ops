"""MCP-only interface to approved host command profiles, never free-form argv.

Authenticated one-use approval takes place through a user-operated MCP App card
or the existing admin fallback. Installing a profile requires an
external owner-only 0600 config; the model cannot create profiles or grant
itself host execution privileges.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from host_command_operations import HostCommandOperations
from host_runtime_config import HostRuntimeError

_PROFILE = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,62}$"}
_REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}
_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
PROJECT = {"type": "string"}  # allowed enum added by server.py
NAMES = (
    "list_host_command_profiles", "prepare_host_command",
    "execute_host_command", "get_host_command", "cancel_host_command",
)
REQUIRED = {
    "list_host_command_profiles": ("project",),
    "prepare_host_command": ("project", "profile_id", "request_id"),
    "execute_host_command": ("project", "operation_id"),
    "get_host_command": ("project", "operation_id"),
    "cancel_host_command": ("project", "operation_id"),
}


def _spec(name: str, title: str, description: str, fields: dict, *, readonly: bool):
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {
            "type": "object", "properties": {"project": PROJECT, **fields},
            "required": list(REQUIRED[name]), "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": readonly, "destructiveHint": not readonly,
            "idempotentHint": readonly, "openWorldHint": False,
        },
    }


HOST_COMMAND_TOOL_SPECS = [
    _spec("list_host_command_profiles", "허용된 호스트 명령 프로필 조회",
          "Read host-owned fixed command profiles (IDs and effects only). "
          "No shell, raw argv, credentials or source paths are exposed. "
          "A missing owner-controlled profile configuration disables command execution.",
          {}, readonly=True),
    _spec("prepare_host_command", "호스트 명령 실행 승인 계획 생성",
          "Freeze one host-owned command profile's exact executable, arguments, "
          "source inputs and checksums into a 7-day plan. No command executes. "
          "Returns an inline confirmation card for a separate one-use user approval. "
          "Use its approval/cancel buttons; do not substitute an external approval page. "
          "This cannot run arbitrary shell text or inherit other Docker/agent target grants.",
          {"profile_id": _PROFILE, "request_id": _REQUEST}, readonly=False),
    _spec("execute_host_command", "승인한 호스트 명령 실행",
          "Start only a still-valid approved command profile plan. "
          "Revalidate executable, exact artifact checksums and owner-controlled config. "
          "Return queued/running state; get_host_command confirms terminal result. "
          "This may perform an external TEST publication if that is the approved profile effect.",
          {"operation_id": _ID}, readonly=False),
    _spec("get_host_command", "호스트 명령 실행 결과 조회",
          "Read one host command's current state and bounded numeric exit metadata. "
          "No command output, environment values or authentication tokens are returned.",
          {"operation_id": _ID}, readonly=True),
    _spec("cancel_host_command", "호스트 명령 중단 요청",
          "Request cancellation of only this MCP's owned running subprocess group. "
          "External publish effects may already have happened and are NEVER described as rolled back.",
          {"operation_id": _ID}, readonly=False),
]


def create_host_command_manager(projects, state_dir, workspace=None):
    return HostCommandOperations(
        projects, config_path=Path.home() / ".config/chatgpt-coding-mcp/host-commands.json",
        state_dir=Path(state_dir) / "host-runtime" / "commands",
        workspace=workspace,
    )


class HostCommandTools:
    def __init__(self, projects, state_dir, project_locks, workspace=None):
        self.projects = projects
        self.state_dir = state_dir
        self.project_locks = project_locks
        self.names = frozenset(NAMES)
        self._manager = None
        self.workspace = workspace

    def manager(self):
        if self._manager is None:
            factory_accepts_workspace = "workspace" in inspect.signature(
                create_host_command_manager).parameters
            if self.workspace is None or not factory_accepts_workspace:
                self._manager = create_host_command_manager(self.projects, self.state_dir)
                if self.workspace is not None:
                    self._manager.workspace = self.workspace
            else:
                self._manager = create_host_command_manager(
                    self.projects, self.state_dir, workspace=self.workspace)
        return self._manager

    async def call(self, name, arguments):
        try:
            payload = await self._call(name, arguments)
            # The authenticated admin fallback remains available in the engine;
            # MCP responses must not steer the user away from their inline card.
            payload.pop("approval_url", None)
            if payload.get("state") in {"pending_host_approval", "approved"}:
                payload["approval_card"] = {
                    "tool": "open_host_command_approval", "arguments": {
                        "project": payload["project"], "operation_id": payload["operation_id"]}}
            return payload
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("HOST_COMMAND_UNAVAILABLE") from None

    async def _call(self, name, args):
        if name not in self.names or set(args) != set(REQUIRED[name]):
            raise HostRuntimeError("INVALID_HOST_COMMAND_ARGUMENTS")
        project = args["project"]
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        manager = self.manager()
        if name == "list_host_command_profiles":
            try:
                return await asyncio.to_thread(manager.list_profiles, project)
            except HostRuntimeError as exc:
                if exc.code in ("HOST_COMMAND_CONFIG_UNAVAILABLE", "UNSAFE_RUNTIME"):
                    return {"project": project, "enabled": False,
                            "reason": exc.code, "profiles": []}
                raise
        if name == "prepare_host_command":
            lock = self.project_locks[project]
            if lock.locked():
                raise HostRuntimeError("PROJECT_BUSY")
            async with lock:
                return await asyncio.to_thread(
                    manager.prepare, project, args["profile_id"], args["request_id"])
        if name == "execute_host_command":
            return await manager.execute(project, args["operation_id"])
        if name == "cancel_host_command":
            return await asyncio.to_thread(manager.cancel, project, args["operation_id"])
        return await asyncio.to_thread(manager.get, project, args["operation_id"])

    async def close(self):
        if self._manager is not None:
            await self._manager.close()
