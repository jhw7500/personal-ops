"""Strict MCP facade for expiring host access and dynamic executions."""
from __future__ import annotations

import asyncio

from host_execution import HostExecution
from host_runtime_config import HostRuntimeError


PROJECT = {"type": "string"}
REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}
OPERATION = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
NAMES = (
    "request_host_access", "get_host_access", "start_host_execution",
    "get_host_execution", "read_host_execution_log", "cancel_host_execution",
)
REQUIRED = {
    "request_host_access": {"project"},
    "get_host_access": {"project"},
    "start_host_execution": {"project", "executable", "argv", "cwd", "request_id"},
    "get_host_execution": {"project", "operation_id"},
    "read_host_execution_log": {"project", "operation_id"},
    "cancel_host_execution": {"project", "operation_id"},
}
OPTIONAL = {
    "request_host_access": set(), "get_host_access": set(),
    "start_host_execution": {"timeout_seconds", "input_text", "task_label"},
    "get_host_execution": set(),
    "read_host_execution_log": {"stream", "cursor", "max_bytes"},
    "cancel_host_execution": set(),
}
_RENEWABLE_ACCESS_REASONS = frozenset({
    "NO_GRANT", "EXPIRED", "REVOKED", "RUNTIME_CHANGED", "BOOT_CHANGED",
})


def _access_approval_route(result, project, reason):
    return {**result, "approval_kind": "host_access", "approval_reason": reason,
            "approval_card": {"tool": "open_host_access_approval",
                              "arguments": {"project": project}}}


def _spec(name, title, description, fields, *, readonly):
    properties = {"project": PROJECT, **fields}
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": sorted(REQUIRED[name]), "additionalProperties": False},
        "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                        "idempotentHint": readonly, "openWorldHint": True},
    }


HOST_EXECUTION_TOOL_SPECS = [
    _spec("request_host_access", "호스트 접근 요청", "Legacy management-web access request. "
          "For chat approval, call open_host_access_approval directly instead of this tool. "
          "This tool cannot approve, issue, or extend a grant.", {}, readonly=False),
    _spec("get_host_access", "호스트 접근 상태", "Read the current owner-controlled host access state. "
          "Reuse an active grant; chat/plugin refresh does not revoke or extend it. When renewal "
          "is needed, follow the supplied approval_card to the inline chat card.",
          {}, readonly=True),
    _spec("start_host_execution", "호스트 명령 시작",
          "Start an argv-only command under the current expiring grant. The selected project is "
          "cwd context, not filesystem containment. Ordinary commands reuse the active grant "
          "without per-command approval. Preserve the user's execution request; an already "
          "requested action with all required valid approvals needs no extra execution prompt. "
          "Separate operation plans retain their approval requirements. Provide task_label so the dashboard can "
          "identify the work without inspecting private execution inputs.", {
              "executable": {"type": "string", "minLength": 1, "maxLength": 4096},
              "argv": {"type": "array", "items": {"type": "string", "maxLength": 1024},
                       "maxItems": 32},
              "cwd": {"type": "string", "minLength": 1, "maxLength": 512},
              "request_id": REQUEST,
              "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
              "input_text": {"type": "string", "maxLength": 32768},
              "task_label": {
                  "type": "string", "minLength": 1, "maxLength": 160,
                  "description": "Short public human-readable work purpose and target for the "
                                 "dashboard. Provide it when starting work. Do not include secrets, "
                                 "raw command arguments, or prompt/objective text. Display metadata "
                                 "only; it grants no permission and proves no progress.",
              },
          }, readonly=False),
    _spec("get_host_execution", "호스트 명령 상태", "Read bounded execution state and grant binding.",
          {"operation_id": OPERATION}, readonly=True),
    _spec("read_host_execution_log", "호스트 명령 로그", "Read a bounded best-effort-masked log page.", {
        "operation_id": OPERATION,
        "stream": {"type": "string", "enum": ["stdout", "stderr"]},
        "cursor": {"type": "integer", "minimum": 0},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
    }, readonly=True),
    _spec("cancel_host_execution", "호스트 명령 취소",
          "Cancel only the recorded owned execution scope; external effects are not rolled back.",
          {"operation_id": OPERATION}, readonly=False),
]


def create_host_execution_manager(projects, state_dir, access=None, workspace=None):
    if access is None:
        from host_access import HostAccess
        access = HostAccess(projects, state_dir, workspace=workspace)
    return HostExecution(projects, state_dir, access=access)


class HostExecutionTools:
    def __init__(self, projects, state_dir, project_locks, manager=None, access=None,
                 workspace=None):
        self.projects = projects
        self.state_dir = state_dir
        self.project_locks = project_locks
        self.names = frozenset(NAMES)
        self._manager = manager
        self._access = access
        self.workspace = workspace

    def manager(self):
        if self._manager is None:
            self._manager = create_host_execution_manager(
                self.projects, self.state_dir, access=self.access(), workspace=self.workspace)
        return self._manager

    def access(self):
        if self._access is None:
            if self._manager is not None:
                self._access = self._manager.access
            else:
                from host_access import HostAccess
                self._access = HostAccess(
                    self.projects, self.state_dir, workspace=self.workspace)
        return self._access

    async def call(self, name, arguments):
        try:
            return await self._call(name, arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except (OSError, KeyError, TypeError, ValueError):
            raise ValueError("HOST_EXECUTION_UNAVAILABLE") from None

    async def _call(self, name, arguments):
        if (name not in self.names or not isinstance(arguments, dict)
                or not REQUIRED[name].issubset(arguments)
                or not set(arguments).issubset(REQUIRED[name] | OPTIONAL[name])):
            raise HostRuntimeError("INVALID_HOST_EXECUTION_ARGUMENTS")
        project = arguments["project"]
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if name == "request_host_access":
            return await asyncio.to_thread(self.access().request, project)
        if name == "get_host_access":
            result = await asyncio.to_thread(self.access().get, project)
            if result.get("active") is False and result.get("reason") in _RENEWABLE_ACCESS_REASONS:
                return _access_approval_route(result, project, result["reason"])
            return result
        manager = self.manager()
        if name == "start_host_execution":
            values = {key: value for key, value in arguments.items() if key != "project"}
            async with self.project_locks[project]:
                prepared = await asyncio.to_thread(manager.prepare, project, **values)
            return await manager.execute(project, prepared["operation_id"])
        if name == "get_host_execution":
            return await asyncio.to_thread(manager.get, project, arguments["operation_id"])
        if name == "read_host_execution_log":
            values = {key: value for key, value in arguments.items()
                      if key not in {"project", "operation_id"}}
            return await asyncio.to_thread(
                manager.read_log, project, arguments["operation_id"], **values)
        return await asyncio.to_thread(manager.cancel, project, arguments["operation_id"])

    async def close(self):
        if self._manager is not None:
            await self._manager.close()
