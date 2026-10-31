"""Existing Codex task flow: isolated approval or an explicit owner host grant."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent_runner import CodexAgentRunner
from agent_tasks import AgentTaskManager
from host_runtime_config import HostRuntimeError

IDENTIFIER = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,47}$"}
TASK_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}
PROJECT = {"type": "string"}

def _tool(name, title, description, fields, required=(), *, readonly=False):
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"project": PROJECT, **fields},
            "required": ["project", *required], "additionalProperties": False
        },
        "annotations": {"readOnlyHint": readonly,
                        "destructiveHint": not readonly,
                        "idempotentHint": readonly, "openWorldHint": False}
    }

AGENT_TOOL_SPECS = [
    _tool("list_agent_profiles", "호스트 코딩 에이전트 프로필 조회",
          "Read only host-registered Codex profile readiness. Disabled and unverified "
          "profiles never authorize isolated execution. Host mode separately requires "
          "an active owner grant that includes Codex account usage. ready/reason describe "
          "ISOLATED readiness only; host_ready/host_reason describe separate granted host execution.", {}, readonly=True),
    _tool("prepare_agent_task", "에이전트 작업 승인 계획",
          "Freeze an exact coding objective and source snapshot into a 7-day plan. "
          "Preparation starts no model. execution_mode=auto uses a real active host grant "
          "including Codex usage, otherwise the existing isolated review and one-use approval. "
          "host mode edits the REAL checkout with host-user access and no per-task approval; "
          "isolated NEVER falls back to host mode after failed proof checks. Provide task_label so "
          "the dashboard can identify the work without inspecting the private objective.",
          {"profile_id": IDENTIFIER,
           "objective": {"type": "string", "minLength": 10, "maxLength": 8192},
           "task_label": {
               "type": "string", "minLength": 1, "maxLength": 160,
               "description": "Short public human-readable work purpose and target for the "
                              "dashboard. Provide it when starting work. Do not include secrets, "
                              "raw command arguments, or prompt/objective text. Display metadata "
                              "only; it grants no permission and proves no progress.",
           },
           "reason": {"type": "string", "enum": ["MISSING_TOOL", "WORKFLOW_COMPLEXITY",
                                                "EXECUTION_ENV_UNSUPPORTED"]},
           "request_id": REQUEST_ID,
           "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
           "execution_mode": {"type": "string", "enum": ["auto", "isolated", "host"], "default": "auto"}},
          ("profile_id", "objective", "reason", "request_id")),
    _tool("start_agent_task", "승인된 에이전트 실행",
          "Only start the exact unexpired agent task after trusted host profile, verified "
          "isolation/smoke and one-use approval OR its still-valid Codex host grant. "
          "Host grant expiry/revocation stops the owned host scope. Never resume/kill "
          "an existing user Codex CLI session.",
          {"task_id": TASK_ID}, ("task_id",)),
    _tool("get_agent_task", "에이전트 작업 상태",
          "Read safe progress, provider usage, a bounded structured model report, "
          "and isolated source-review metadata or labelled host execution results. Model-reported validations "
          "are not verified test evidence. No raw prompt, command, reasoning or logs.", {"task_id": TASK_ID},
          ("task_id",), readonly=True),
    _tool("cancel_agent_task", "전용 에이전트 작업 취소",
          "Cancel only the verified dedicated process created for this agent task. "
          "Stopping is not proof of rollback.", {"task_id": TASK_ID},
          ("task_id",)),
]

_REQUIRED = {item["name"]: set(item["inputSchema"]["required"]) for item in AGENT_TOOL_SPECS}
_ALLOWED = {item["name"]: set(item["inputSchema"]["properties"]) for item in AGENT_TOOL_SPECS}


class AgentTools:
    def __init__(self, projects, state_dir, project_locks=None, manager=None, event_sink=None,
                 host_execution=None, workspace=None):
        self.projects = projects
        self.state_dir = Path(state_dir)
        self.project_locks = project_locks or {}
        self.names = {spec["name"] for spec in AGENT_TOOL_SPECS}
        self._manager = manager
        self.event_sink = event_sink
        self.host_execution = host_execution
        self.workspace = workspace
        self._host_agent = None

    def host_agent(self):
        if self._host_agent is None and self.host_execution is not None:
            from host_agent import HostAgentAdapter
            self._host_agent = HostAgentAdapter(self.host_execution, event_sink=self.event_sink)
        return self._host_agent

    def manager(self):
        if self._manager is None:
            self._manager = AgentTaskManager(
                self.projects, state_dir=self.state_dir,
                runner=CodexAgentRunner(), event_sink=self.event_sink, workspace=self.workspace)
        return self._manager

    async def close(self):
        manager = self._manager
        if manager is not None:
            await manager.close()

    async def call(self, name, arguments):
        try:
            if name not in self.names or not isinstance(arguments, dict):
                raise HostRuntimeError("INVALID_AGENT_ARGUMENTS")
            if not _REQUIRED[name] <= set(arguments) <= _ALLOWED[name]:
                raise HostRuntimeError("INVALID_AGENT_ARGUMENTS")
            project = arguments["project"]
            if not isinstance(project, str) or project not in self.projects:
                raise HostRuntimeError("UNSUPPORTED_SCOPE")
            host = self.host_agent()
            if name == "prepare_agent_task":
                mode = arguments.get("execution_mode", "auto")
                if mode not in {"auto", "isolated", "host"}:
                    raise HostRuntimeError("INVALID_AGENT_ARGUMENTS")
                if mode == "host" or mode == "auto" and host is not None and host.available(project):
                    if host is None:
                        raise HostRuntimeError("HOST_ACCESS_REQUIRED")
                    kwargs = {k: v for k, v in arguments.items() if k not in {"project", "execution_mode"}}
                    return await asyncio.to_thread(host.prepare, project, **kwargs)
            elif name != "list_agent_profiles" and host is not None and host.owns(project, arguments["task_id"]):
                if name == "start_agent_task":
                    return await host.start(project, arguments["task_id"])
                if name == "get_agent_task":
                    return await asyncio.to_thread(host.get, project, arguments["task_id"])
                return await host.cancel(project, arguments["task_id"])
            manager = self.manager()
            if name == "list_agent_profiles":
                result = await asyncio.to_thread(manager.list_profiles, project)
                return await asyncio.to_thread(host.decorate_profiles, project, result) if host is not None else result
            if name == "prepare_agent_task":
                kwargs = {k: v for k, v in arguments.items() if k not in {"project", "execution_mode"}}
                project_lock = self.project_locks.get(project)
                if project_lock is not None:
                    if project_lock.locked():
                        raise HostRuntimeError("PROJECT_BUSY")
                    async with project_lock:
                        return await asyncio.to_thread(manager.prepare, project, **kwargs)
                return await asyncio.to_thread(manager.prepare, project, **kwargs)
            if name == "start_agent_task":
                return await manager.start(project, arguments["task_id"])
            if name == "get_agent_task":
                return await asyncio.to_thread(manager.get, project, arguments["task_id"])
            return await manager.cancel(project, arguments["task_id"])
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except (OSError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).isupper():
                raise
            raise ValueError("AGENT_TASK_FAILED") from None
