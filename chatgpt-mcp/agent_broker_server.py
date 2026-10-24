#!/usr/bin/env python3
"""Private stdio MCP surface for one live, lease-bound delegated task.

The context file selects a task but grants nothing.  Startup reconstructs the fixed host
registries and verifies private task state, consumed approval, the exact lease, and the
calling broker process's live cgroup before exposing any tools.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import pwd
import re
import sys

from host_runtime_config import HostRuntimeError, MAX_CONFIG_BYTES, private_file

TOOL_NAMES = frozenset({
    "inspect_agent_host", "prepare_agent_host_operation",
    "execute_agent_host_operation", "get_agent_host_operation",
})
TASK_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
PROJECT = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"}
TARGET = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,62}$"}
OPERATION_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}
ACTION = {"type": "string", "enum": [
    "compose.build", "compose.apply", "compose.up", "compose.restart",
    "compose.stop",
]}
PARAMETERS = {
    "type": "object",
    "properties": {
        "services": {"type": "array", "minItems": 1, "maxItems": 32,
                     "uniqueItems": True, "items": {"type": "string",
                     "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"}},
    },
    "additionalProperties": False,
}
COMMON = {"project": PROJECT, "task_id": TASK_ID}
REQUIRED = {
    "inspect_agent_host": frozenset({"project", "task_id", "target", "area"}),
    "prepare_agent_host_operation": frozenset({
        "project", "task_id", "target", "action", "parameters", "request_id"}),
    "execute_agent_host_operation": frozenset({"project", "task_id", "operation_id"}),
    "get_agent_host_operation": frozenset({"project", "task_id", "operation_id"}),
}
CONTEXT_KEYS = frozenset({"version", "project", "task_id", "context_nonce"})
HEX64_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class _BoundAgentHostBroker:
    """Keep one broker process scoped to the task named by its private context."""

    def __init__(self, broker, project: str, task_id: str):
        self._broker = broker
        self._project = project
        self._task_id = task_id

    def _check(self, project, task_id) -> None:
        if project != self._project or task_id != self._task_id:
            raise HostRuntimeError("AGENT_BROKER_CONTEXT_MISMATCH")

    def inspect(self, project, task_id, target, area):
        self._check(project, task_id)
        return self._broker.inspect(project, task_id, target, area)

    def prepare(self, project, task_id, target, action, parameters, request_id):
        self._check(project, task_id)
        return self._broker.prepare(project, task_id, target, action, parameters, request_id)

    async def execute(self, project, task_id, operation_id):
        self._check(project, task_id)
        return await self._broker.execute(project, task_id, operation_id)

    def get(self, project, task_id, operation_id):
        self._check(project, task_id)
        return self._broker.get(project, task_id, operation_id)


def _read_context(path: Path) -> dict:
    try:
        private_file(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError()
        duplicate = []

        def pairs(items):
            value = dict(items)
            if len(value) != len(items):
                duplicate.append(True)
            return value

        context = json.loads(raw, object_pairs_hook=pairs)
    except (HostRuntimeError, OSError, ValueError, UnicodeError, RecursionError):
        raise HostRuntimeError("AGENT_BROKER_CONTEXT_INVALID") from None
    if (duplicate or not isinstance(context, dict) or set(context) != CONTEXT_KEYS
            or context.get("version") != 1
            or not isinstance(context.get("project"), str)
            or re.fullmatch(PROJECT["pattern"], context["project"]) is None
            or not isinstance(context.get("task_id"), str)
            or re.fullmatch(TASK_ID["pattern"], context["task_id"]) is None
            or not isinstance(context.get("context_nonce"), str)
            or HEX64_PATTERN.fullmatch(context["context_nonce"]) is None):
        raise HostRuntimeError("AGENT_BROKER_CONTEXT_INVALID")
    return context


def _fixed_roots() -> tuple[Path, Path, Path]:
    """Host-owned locations; environment variables from delegated code cannot redirect them."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError, RuntimeError):
        raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED") from None
    config_dir = home / ".config" / "chatgpt-coding-mcp"
    state_dir = home / ".local" / "state" / "chatgpt-coding-mcp"
    return config_dir / "projects.json", config_dir / "host-runtime.json", state_dir


def _load_projects(path: Path) -> dict[str, Path]:
    try:
        private_file(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        duplicate = []

        def pairs(items):
            value = dict(items)
            if len(value) != len(items):
                duplicate.append(True)
            return value

        payload = json.loads(raw, object_pairs_hook=pairs)
        if (len(raw) > MAX_CONFIG_BYTES or duplicate or not isinstance(payload, dict)
                or set(payload) != {"projects"}
                or not isinstance(payload["projects"], dict) or not payload["projects"]):
            raise ValueError()
        values = payload["projects"]
        projects = {}
        for name, value in values.items():
            if (not isinstance(name, str) or re.fullmatch(PROJECT["pattern"], name) is None
                    or not isinstance(value, str) or not Path(value).is_absolute()):
                raise ValueError()
            root = Path(value).resolve(strict=True)
            if not root.is_dir():
                raise ValueError()
            projects[name] = root
        return projects
    except (HostRuntimeError, OSError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError):
        raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED") from None


def load_bound_broker(context_path, *, task_manager=None, host_operations=None,
                      supervisor=None):
    """Rebuild and verify authority; injected components are for isolated tests only."""
    from agent_host_broker import AgentHostBroker

    injected = (task_manager is not None, host_operations is not None, supervisor is not None)
    if any(injected) and not all(injected):
        raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
    if not any(injected):
        from agent_profiles import AgentProfileRegistry
        from agent_supervisor import SystemdAgentSupervisor
        from agent_tasks import AgentTaskManager
        from host_registered_operations import RegisteredHostOperations
        from host_registry import HostTargetRegistry

        projects_path, config_path, state_dir = _fixed_roots()
        projects = _load_projects(projects_path)
        profiles = AgentProfileRegistry(
            projects, config_path=config_path, receipts_dir=state_dir / "agent-probes")
        task_manager = AgentTaskManager(
            projects, profiles=profiles, state_dir=state_dir / "agent-tasks")
        host_operations = RegisteredHostOperations(
            projects, HostTargetRegistry(projects, config_path),
            state_dir=state_dir / "host-runtime")
        supervisor = SystemdAgentSupervisor()

    context_path = Path(context_path).absolute()
    context = _read_context(context_path)
    expected_path = (Path(task_manager.sources_dir) / context["task_id"] / "source"
                     / ".agent-control" / "broker-context.json").absolute()
    if context_path != expected_path:
        raise HostRuntimeError("AGENT_BROKER_CONTEXT_INVALID")

    broker = AgentHostBroker(
        task_manager, host_operations, supervisor=supervisor)
    authority = broker.lease_verifier.verify(context["project"], context["task_id"])
    if authority.get("context_nonce") != context["context_nonce"]:
        raise HostRuntimeError("AGENT_BROKER_CONTEXT_INVALID")
    return _BoundAgentHostBroker(broker, context["project"], context["task_id"])


def _schema(name, fields):
    return {"type": "object", "properties": {**COMMON, **fields},
            "required": sorted(REQUIRED[name]), "additionalProperties": False}


BROKER_TOOL_SPECS = ({
    "name": "inspect_agent_host", "title": "승인된 에이전트 호스트 상태 조회",
    "description": ("Inspect only the registered Docker identity and health allowed by this "
                    "running task's private host capability lease."),
    "inputSchema": _schema("inspect_agent_host", {
        "target": TARGET, "area": {"type": "string", "enum": ["docker"]}}),
    "readonly": True,
}, {
    "name": "prepare_agent_host_operation", "title": "에이전트 호스트 작업 승인 계획",
    "description": ("Prepare one typed registered-v3 host operation. This performs no host "
                    "effect and returns waiting_host_approval for a separate admin-web approval."),
    "inputSchema": _schema("prepare_agent_host_operation", {
        "target": TARGET, "action": ACTION, "parameters": PARAMETERS,
        "request_id": REQUEST_ID}),
    "readonly": False,
}, {
    "name": "execute_agent_host_operation", "title": "승인된 에이전트 호스트 작업 실행",
    "description": ("Execute only a plan created by this exact agent task after the existing "
                    "registered host manager verifies and consumes its one-use web approval."),
    "inputSchema": _schema("execute_agent_host_operation", {"operation_id": OPERATION_ID}),
    "readonly": False,
}, {
    "name": "get_agent_host_operation", "title": "에이전트 호스트 작업 조회",
    "description": ("Read a host operation created by this exact task. Unknown outcomes are "
                    "observable but cannot be automatically retried."),
    "inputSchema": _schema("get_agent_host_operation", {"operation_id": OPERATION_ID}),
    "readonly": True,
})


def _exact_arguments(name: str, arguments: object) -> dict:
    if name not in REQUIRED or not isinstance(arguments, dict) or set(arguments) != REQUIRED[name]:
        raise HostRuntimeError("INVALID_AGENT_BROKER_ARGUMENTS")
    return arguments


def create_server(broker):
    """Create a server only around an already supervisor-bound broker instance."""
    if broker is None:
        raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
    from mcp import types
    from mcp.server import Server

    server = Server(
        "personal-ops-agent-host-broker", version="1.0.0",
        instructions=("This private broker serves one dedicated running agent task. Host changes "
                      "always require the existing admin-web approval; waiting is not success."),
    )

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=spec["name"], title=spec["title"], description=spec["description"],
            inputSchema=spec["inputSchema"],
            annotations=types.ToolAnnotations(
                readOnlyHint=spec["readonly"], destructiveHint=not spec["readonly"],
                idempotentHint=spec["readonly"], openWorldHint=False),
        ) for spec in BROKER_TOOL_SPECS]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            args = _exact_arguments(name, arguments or {})
            if name == "inspect_agent_host":
                payload = await asyncio.to_thread(
                    broker.inspect, args["project"], args["task_id"], args["target"], args["area"])
            elif name == "prepare_agent_host_operation":
                payload = await asyncio.to_thread(
                    broker.prepare, args["project"], args["task_id"], args["target"],
                    args["action"], args["parameters"], args["request_id"])
            elif name == "execute_agent_host_operation":
                payload = await broker.execute(
                    args["project"], args["task_id"], args["operation_id"])
            else:
                payload = await asyncio.to_thread(
                    broker.get, args["project"], args["task_id"], args["operation_id"])
        except HostRuntimeError as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) is None:
                code = "AGENT_HOST_BROKER_FAILED"
            raise ValueError(code) from None
        except Exception:
            raise ValueError("AGENT_HOST_BROKER_FAILED") from None
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
        )
    return server


async def serve(broker) -> None:
    from mcp.server.stdio import stdio_server
    server = create_server(broker)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--context":
        print("AGENT_BROKER_BINDING_UNVERIFIED", file=sys.stderr, flush=True)
        return 2
    try:
        broker = load_bound_broker(sys.argv[2])
        asyncio.run(serve(broker))
        return 0
    except (HostRuntimeError, OSError, RuntimeError, ValueError):
        print("AGENT_BROKER_BINDING_UNVERIFIED", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
