#!/usr/bin/env python3
"""Route MCP file tools to an explicitly selected, allowlisted repository."""

import argparse
import asyncio
import copy
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import uuid

from execution import ExecutionPolicy
from git_workflows import GIT_TOOL_SPECS, GitWorkflows
from job_tools import JOB_TOOL_SPECS
from jobs import JobManager
from project_state import KNOWN_TOOLS, PROJECTLESS_TOOLS, ProjectState


MODULE_DIR = Path(__file__).resolve().parent
SERENA_ENV = Path.home() / ".local/share/uv/tools/serena-agent"
CONFIG = Path.home() / ".config/chatgpt-coding-mcp/projects.json"
FILE_TOOLS = {"read_file", "list_dir", "find_file", "search_for_pattern",
              "create_text_file", "replace_content"}
FILE_TOOL_DISPLAY = {
    "read_file": ("파일 읽기", "파일 내용을 읽고 있습니다", "파일 읽기 응답을 받았습니다"),
    "list_dir": ("디렉터리 조회", "디렉터리 내용을 확인하고 있습니다", "디렉터리 조회 응답을 받았습니다"),
    "find_file": ("파일 찾기", "조건에 맞는 파일을 찾고 있습니다", "파일 검색 응답을 받았습니다"),
    "search_for_pattern": ("내용 검색", "프로젝트 내용을 검색하고 있습니다", "내용 검색 응답을 받았습니다"),
    "create_text_file": ("텍스트 파일 저장", "텍스트 파일을 저장하고 있습니다", "파일 저장 응답을 받았습니다"),
    "replace_content": ("파일 내용 수정", "파일 내용을 수정하고 있습니다", "파일 수정 응답을 받았습니다"),
}
UI_URI = "ui://host-repositories/project-picker-v2.html"
UI_MIME = "text/html;profile=mcp-app"
PROJECT_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "projects": {"type": "array", "items": {
            "type": "object", "properties": {
                "project": {"type": "string"}, "path": {"type": "string"},
            }, "required": ["project", "path"], "additionalProperties": False,
        }},
        "selected_project": {"type": "string"},
    },
    "required": ["projects"], "additionalProperties": False,
}
SETTINGS_CAPABILITY = {"openai/settings": {
    "readTool": "read_plugin_settings", "updateTool": "update_plugin_settings",
}}
SETTINGS_SCHEMA = {
    "type": "object", "properties": {
        "schema": {"type": "object"}, "values": {"type": "object"},
        "layout": {"type": "array", "items": {"type": "object"}},
    }, "required": ["schema", "values"],
}


def log_ui_event(event, **details):
    """Trace UI delivery without logging tool arguments, file content, or credentials."""
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                      "component": "repository-ui", "event": event, **details}),
          file=sys.stderr, flush=True)


def load_projects(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    configured = data.get("projects")
    if not isinstance(configured, dict) or not configured:
        raise ValueError("projects must be a nonempty map of names to absolute paths")
    projects = {}
    for name, value in configured.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not isinstance(value, str):
            raise ValueError("Project names must be simple identifiers and paths must be strings")
        path = Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"Project {name!r} must have an existing absolute directory")
        projects[name] = path.resolve(strict=True)
    return projects


def validate_path(root, tool, arguments):
    relative = Path(arguments.get("relative_path", "."))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Use a relative path inside the selected project")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Symlinks outside the selected project are not allowed")
    if any(part == ".git" or part == ".env" or part.startswith(".env.")
           for part in (*relative.parts, *target.relative_to(root).parts)):
        raise ValueError("Repository metadata and environment secret files are excluded")

    # Serena follows links while scanning directories. Reject those scans so
    # project selection cannot be bypassed and link cycles cannot recurse.
    recursive = tool in {"find_file", "search_for_pattern"} or (
        tool == "list_dir" and arguments.get("recursive", False))
    if recursive and target.is_dir():
        for directory, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in dirs + files:
                entry = Path(directory) / name
                if entry.is_symlink():
                    raise ValueError(
                        "Recursive scans through symbolic links are disabled; "
                        f"choose a narrower relative_path (link: {entry.relative_to(root)})")


class BackendWorker:
    """Own subprocess contexts in one task; select and call atomically."""

    def __init__(self, projects):
        self.projects = projects
        self.queue = asyncio.Queue()

    async def request(self, project, method, arguments=None):
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((project, method, arguments, result))
        return await result

    async def run(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async with AsyncExitStack() as resources:
            current_project = None
            session = None
            while True:
                request = await self.queue.get()
                if request is None:
                    return
                project, method, arguments, result = request
                if result.cancelled():
                    continue
                try:
                    if current_project != project:
                        await resources.aclose()
                        current_project = None
                        env = os.environ.copy()
                        for key in ("CONTROL_PLANE_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
                            env.pop(key, None)
                        params = StdioServerParameters(
                            command=str(SERENA_ENV / "bin/python"),
                            args=[str(MODULE_DIR / "serena_backend.py"),
                                  "--project", str(self.projects[project])],
                            env=env,
                        )
                        read, write = await resources.enter_async_context(stdio_client(params))
                        session = await resources.enter_async_context(ClientSession(read, write))
                        await asyncio.wait_for(session.initialize(), timeout=60)
                        current_project = project
                    if result.cancelled():
                        continue
                    if method == "tools/list":
                        response = await asyncio.wait_for(session.list_tools(), timeout=60)
                    else:
                        validate_path(self.projects[project], method, arguments)
                        response = await asyncio.wait_for(session.call_tool(method, arguments), timeout=90)
                    if not result.done():
                        result.set_result(response)
                except Exception as exc:
                    current_project = None
                    if not result.done():
                        result.set_exception(exc)


async def serve(projects):
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    worker = BackendWorker(projects)
    state_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "chatgpt-coding-mcp"
    project_locks = {project: asyncio.Lock() for project in projects}
    policy = ExecutionPolicy(projects)
    jobs = JobManager(projects, policy, state_dir / "jobs", project_locks)
    git = GitWorkflows(projects, state_dir / "git-operations")
    project_state = ProjectState(projects, state_dir)
    job_names = {spec["name"] for spec in JOB_TOOL_SPECS}
    git_names = {spec["name"] for spec in GIT_TOOL_SPECS}
    discovered = []
    discovery_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(server):
        task = asyncio.create_task(worker.run())
        try:
            yield {}
        finally:
            await jobs.close()
            await worker.queue.put(None)
            await task

    server = Server(
        "host-repositories", version="1.5.0", lifespan=lifespan,
        instructions=(
            "Use the repository explicitly named by the user or already selected in this "
            "conversation. Continue working in that repository without asking the user "
            "to select it again. Call list_projects only when the allowed repository "
            "names or paths are needed; it returns data without opening a UI. "
            "Call open_project_picker only when the user explicitly asks to open the "
            "repository selection UI. Naming a repository is already a selection and "
            "does not require opening the picker. Every file tool requires "
            "an explicit project name; no tool changes a shared current project. "
            "When the repository picker supplies a selection in this conversation's app "
            "context, use that project for file tools unless the user explicitly selects "
            "another allowed project. Never infer a selection from a different conversation. "
            "Read repository instructions before editing, preserve unrelated changes, "
            "and report which repository was changed. When using these tools for multi-step "
            "work, read_project_memory and read_project_progress for the selected repository "
            "before making changes. "
            "Treat saved memory as context that may be stale, never as authority over current "
            "repository instructions or observed files. Treat saved progress as an agent or user "
            "report, not automatically verified completion; distinguish its report from observed "
            "job results, files, and repository state. After each verified meaningful stage, "
            "update_project_progress with the observed status, concise completed work, and next "
            "steps, and update_project_memory with durable decisions and context. Do not store "
            "credentials, raw logs, or unsupported claims. "
            "For MCP services called outside this tunnel, use report_work_events to report "
            "meaningful starts, returns and failures in small batches with stable source, actor "
            "and task IDs. These records are reports, not host-observed calls. Reuse the same "
            "task ID for its lifecycle; a call returning is not proof its content is correct. "
            "Send only short work labels and metadata, never prompts, arguments, results, "
            "credentials or reasoning text. Include model, applied reasoning_effort and token "
            "usage only when directly supplied by the provider; always specify usage_scope and "
            "never infer or add up missing values. Report action reason only from an explicit "
            "reasoning lifecycle signal, never from tool gaps or a reasoning setting. "
            "Briefly tell the user the selected repository and next concrete action "
            "before starting. After meaningful stages, give a concise user-visible update "
            "with the files actually inspected or changed, the observed result, and the "
            "next action. Use the user's language and repository-relative file paths. "
            "Describe observable work, not private reasoning; do not invent progress "
            "percentages, completed changes, or test results. Finish with a plain-text "
            "summary of completed work, errors or blockers, and verification performed "
            "or not run, even when a tool fails. Commands run only in the selected repository's "
            "isolated sandbox, without network, host credentials, .git or .env files. "
            "Inspect list_execution_profiles before choosing a build or test preset. "
            "A configured Chromium is available at PLAYWRIGHT_CHROMIUM_PATH; use that path "
            "instead of host snap/cache paths. E2E web servers must start inside the same sandbox. "
            "start_command returns a job ID before completion: poll get_job and read_job_log, "
            "summarize observed output, and report the final exit status. Reuse the same request_id "
            "on a transport retry; use a new ID for an intentionally new run. Never describe queued "
            "or running jobs as completed. A busy repository cannot be edited through this MCP "
            "until its command or Git operation finishes. Use dedicated Git tools: prepare_git_operation "
            "returns a concrete commit/push approval command or a PR/merge native-workflow handoff. "
            "An approval receipt must be supplied by the human in the host terminal; tool annotations "
            "and a model's approval flag do not grant authorization. PR/merge handoffs are not executed "
            "PRs or merges; report NATIVE_WORKFLOW_REQUIRED explicitly. Read repository instructions "
            "and follow commit evidence and mandatory PR reviews. Yocto/BitBake tasks and cache "
            "changes require separate user approval and are disabled in these execution tools."
        ),
    )

    @server.list_resources()
    async def list_resources():
        log_ui_event("resources/list")
        return [types.Resource(uri=UI_URI, name="project-picker", title="작업 레포",
                               mimeType=UI_MIME)]

    # This installed SDK's read_resource decorator drops per-resource metadata.
    # Register the typed handler directly to preserve the UI's CSP and border hints.
    async def read_resource(request):
        log_ui_event("resources/read", known_uri=str(request.params.uri) == UI_URI)
        if str(request.params.uri) != UI_URI:
            raise ValueError("Unknown resource")
        return types.ServerResult(types.ReadResourceResult(contents=[types.TextResourceContents(
            uri=UI_URI, mimeType=UI_MIME,
            text=(MODULE_DIR / "project-picker.html").read_text(encoding="utf-8"),
            _meta={"ui": {"prefersBorder": True,
                          "csp": {"connectDomains": [], "resourceDomains": []}},
                   "openai/ui": {"availableDisplayModes": ["inline"]},
                   "openai/widgetDescription": (
                       "일반 ChatGPT 대화에서 호스트의 허용된 로컬 레포를 선택하는 카드. "
                       "선택 후 같은 대화에서 파일 작업을 요청할 수 있습니다."),
                   "openai/widgetPrefersBorder": True,
                   "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}},
        )]))

    server.request_handlers[types.ReadResourceRequest] = read_resource

    @server.list_tools()
    async def list_tools():
        log_ui_event("tools/list", ui_resource=UI_URI)
        async with discovery_lock:
            if not discovered:
                backend = await worker.request(next(iter(projects)), "tools/list")
                if backend.nextCursor:
                    raise RuntimeError("Unexpected pagination from the six-tool backend")
                if {tool.name for tool in backend.tools} != FILE_TOOLS:
                    raise RuntimeError("Serena backend tool set changed; review before exposing it")
                for tool in backend.tools:
                    item = tool.model_copy(deep=True)
                    # The picker only needs list/selection tools. File operations
                    # remain model-only instead of inheriting the default app access.
                    item.meta = dict(item.meta or {})
                    item.meta["ui"] = {**item.meta.get("ui", {}), "visibility": ["model"]}
                    item.meta["openai/widgetAccessible"] = False
                    title, invoking, invoked = FILE_TOOL_DISPLAY[item.name]
                    item.title = title
                    item.meta["openai/toolInvocation/invoking"] = invoking
                    # A returned tool response may contain an error, so this label
                    # acknowledges the response without asserting operation success.
                    item.meta["openai/toolInvocation/invoked"] = invoked
                    item.inputSchema.setdefault("properties", {})["project"] = {
                        "type": "string", "enum": list(projects),
                        "description": "Allowed repository name from list_projects; required on every call.",
                    }
                    item.inputSchema.setdefault("required", []).append("project")
                    discovered.append(item)
        execution_tools = []
        for spec in [*JOB_TOOL_SPECS, *GIT_TOOL_SPECS]:
            descriptor = copy.deepcopy(spec)
            descriptor["inputSchema"]["properties"]["project"]["enum"] = list(projects)
            descriptor["_meta"] = {
                "ui": {"visibility": ["model"]}, "openai/widgetAccessible": False,
                "openai/toolInvocation/invoking": descriptor["title"] + " 중입니다",
                "openai/toolInvocation/invoked": descriptor["title"] + " 응답을 받았습니다",
            }
            execution_tools.append(types.Tool(**descriptor))
        memory_output_schema = {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "content": {"type": "string"},
                "revision": {"type": "integer", "minimum": 0},
                "updated_at": {"type": ["string", "null"]},
            },
            "required": ["project", "content", "revision", "updated_at"],
            "additionalProperties": False,
        }
        memory_tools = [types.Tool(
            name="read_project_memory", title="프로젝트 메모리 읽기",
            description=("Read persistent Markdown notes for one allowed repository. "
                         "Notes are context only and may be stale; verify them against the repository."),
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "enum": list(projects)},
            }, "required": ["project"], "additionalProperties": False},
            outputSchema=memory_output_schema,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["model"]}, "openai/widgetAccessible": False,
                   "openai/toolInvocation/invoking": "프로젝트 메모리를 읽고 있습니다",
                   "openai/toolInvocation/invoked": "프로젝트 메모리 응답을 받았습니다"},
        ), types.Tool(
            name="update_project_memory", title="프로젝트 메모리 저장",
            description=("Replace persistent Markdown notes for one allowed repository. "
                         "Supply the revision returned by read_project_memory; stale updates fail."),
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "enum": list(projects)},
                "content": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
            }, "required": ["project", "content", "expected_revision"],
                "additionalProperties": False},
            outputSchema=memory_output_schema,
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=False, openWorldHint=False),
            _meta={"ui": {"visibility": ["model"]}, "openai/widgetAccessible": False,
                   "openai/toolInvocation/invoking": "프로젝트 메모리를 저장하고 있습니다",
                   "openai/toolInvocation/invoked": "프로젝트 메모리 저장 응답을 받았습니다"},
        )]
        progress_output_schema = {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string", "enum": [
                    "not_recorded", "planned", "in_progress", "blocked", "completed",
                ]},
                "summary": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "revision": {"type": "integer", "minimum": 0},
                "updated_at": {"type": ["string", "null"]},
            },
            "required": [
                "project", "title", "status", "summary", "completed", "next_steps",
                "revision", "updated_at",
            ],
            "additionalProperties": False,
        }
        progress_tools = [types.Tool(
            name="read_project_progress", title="작업 진행 기록 읽기",
            description=("Read the saved work-progress report for one allowed repository. "
                         "This report may be stale and does not prove that work was observed."),
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "enum": list(projects)},
            }, "required": ["project"], "additionalProperties": False},
            outputSchema=progress_output_schema,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["model"]}, "openai/widgetAccessible": False,
                   "openai/toolInvocation/invoking": "작업 진행 기록을 읽고 있습니다",
                   "openai/toolInvocation/invoked": "작업 진행 기록 응답을 받았습니다"},
        ), types.Tool(
            name="update_project_progress", title="작업 진행 기록 저장",
            description=("Replace the saved work-progress report for one allowed repository. "
                         "Supply the current revision; stale updates fail. Record verified "
                         "observations accurately because saved status is not independent proof."),
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "enum": list(projects)},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "status": {"type": "string", "enum": [
                    "planned", "in_progress", "blocked", "completed",
                ]},
                "summary": {"type": "string", "maxLength": 4096},
                "completed": {"type": "array", "items": {
                    "type": "string", "maxLength": 1000,
                },
                              "maxItems": 20},
                "next_steps": {"type": "array", "items": {
                    "type": "string", "maxLength": 1000,
                },
                               "maxItems": 20},
                "expected_revision": {"type": "integer", "minimum": 0},
            }, "required": [
                "project", "title", "status", "summary", "completed", "next_steps",
                "expected_revision",
            ], "additionalProperties": False},
            outputSchema=progress_output_schema,
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=False, openWorldHint=False),
            _meta={"ui": {"visibility": ["model"]}, "openai/widgetAccessible": False,
                   "openai/toolInvocation/invoking": "작업 진행 기록을 저장하고 있습니다",
                   "openai/toolInvocation/invoked": "작업 진행 기록 저장 응답을 받았습니다"},
        )]
        identifier = {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,80}$', 'maxLength': 80}
        event_schema = {
            'type': 'object', 'properties': {
                'schema_version': {'type': 'integer', 'const': 1},
                'source_id': identifier, 'source_kind': {'type': 'string', 'enum': ['mcp', 'chatgpt', 'codex-cli', 'claude-cli']},
                'actor_id': identifier, 'actor_kind': {'type': 'string', 'enum': ['mcp', 'executor', 'agent']},
                'actor_label': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                'task_id': identifier, 'task_kind': {'type': 'string', 'enum': ['call', 'job', 'agent']},
                'task_label': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                'action': {'type': 'string', 'enum': ['read', 'search', 'code', 'build', 'test', 'review', 'execute', 'commit', 'push', 'reason']},
                'state': {'type': 'string', 'enum': ['started', 'running', 'queued', 'canceling', 'returned', 'succeeded',
                                                   'failed', 'timed_out', 'canceled', 'interrupted', 'unknown']},
                'time': {'type': 'string', 'format': 'date-time'},
                'expires_at': {'type': 'string', 'format': 'date-time'},
                'parent_source_id': identifier, 'parent_actor_id': identifier, 'parent_task_id': identifier,
                'exit_code': {'type': 'integer'},
                'model': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:+/-]+$', 'minLength': 1, 'maxLength': 120},
                'model_source': {'type': 'string', 'enum': ['configuration', 'event']},
                'reasoning_effort': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:+/-]+$', 'minLength': 1, 'maxLength': 32},
                'reasoning_effort_source': {'type': 'string', 'enum': ['configuration', 'event']},
                'usage': {'type': 'object', 'minProperties': 1, 'properties': {
                    key: {'type': 'integer', 'minimum': 0, 'maximum': 9007199254740991}
                    for key in ['input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens']
                }, 'additionalProperties': False},
                'usage_scope': {'type': 'string', 'enum': ['request', 'turn', 'session', 'task']},
                'usage_estimated': {'type': 'boolean'},
            }, 'required': ['schema_version', 'source_id', 'source_kind', 'actor_id', 'actor_kind',
                            'actor_label', 'task_id', 'task_kind', 'task_label', 'action', 'state'],
            'additionalProperties': False,
            'dependentRequired': {'usage': ['usage_scope'], 'model_source': ['model'],
                                  'reasoning_effort_source': ['reasoning_effort'], 'usage_estimated': ['usage']},
        }
        observer_tool = types.Tool(
            name='report_work_events', title='다른 MCP·에이전트 작업 보고',
            description=('Report 1–20 metadata-only work events for the dashboard. Other MCP calls made '
                         'by ChatGPT do not pass through this host. Reports are explicitly labeled reported. '
                         'Use stable source/actor/task IDs; call completion is returned, agent completion '
                         'is succeeded. Missing terminal events expire after 120 seconds. Omit unavailable '
                         'model, effort and usage; usage_scope is required for tokens. Never send reasoning '
                         'content or infer an active reasoning phase from settings.'),
            inputSchema={'type': 'object', 'properties': {
                'project': {'type': 'string', 'enum': list(projects)},
                'events': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': event_schema},
            }, 'required': ['project', 'events'], 'additionalProperties': False},
            outputSchema={'type': 'object', 'properties': {
                'project': {'type': 'string'}, 'accepted': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'provenance': {'type': 'string', 'const': 'reported'},
            }, 'required': ['project', 'accepted', 'provenance'], 'additionalProperties': False},
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
            _meta={'ui': {'visibility': ['model']}, 'openai/widgetAccessible': False,
                   'openai/toolInvocation/invoking': '작업 상태를 보고하고 있습니다',
                   'openai/toolInvocation/invoked': '작업 상태가 기록되었습니다'},
        )
        return [types.Tool(
            name="list_projects", title="허용 레포 목록 조회",
            description=("Return allowed host-local repository names and paths without opening "
                         "a selection UI or changing the conversation's selected repository. "
                         "Use only when the repository list is needed. If the user already "
                         "named or selected a repository, continue using it in file tools."),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            outputSchema=PROJECT_LIST_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["model", "app"]},
                   "openai/widgetAccessible": True},
        ), types.Tool(
            name="open_project_picker", title="작업 레포 선택창 열기",
            description=("Open the repository selection UI inline in this ChatGPT conversation. "
                         "Call only when the user explicitly asks to open or show the picker. "
                         "Do not call for routine repository discovery, during ongoing work, "
                         "or merely because the user named a repository. Use list_projects "
                         "for a data-only listing. No ChatGPT Work project is needed."),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            outputSchema=PROJECT_LIST_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            icons=[types.Icon(src=(
                "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
                "viewBox='0 0 20 20' fill='none'%3E%3Cpath "
                "d='M2.5 5h5l2 2h8v9h-15V5Z' stroke='currentColor' "
                "stroke-width='1.33' stroke-linejoin='round'/%3E%3C/svg%3E"
            ), mimeType="image/svg+xml")],
            _meta={"ui": {"resourceUri": UI_URI, "visibility": ["model"]},
                   "openai/outputTemplate": UI_URI, "openai/widgetAccessible": False},
        ), types.Tool(
            name="select_project", title="작업 레포 선택 확인",
            description=("Validate the repository picker selection against the allowlist. "
                         "Returns conversation context; does not change a shared active project."),
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "enum": list(projects)},
            }, "required": ["project"], "additionalProperties": False},
            outputSchema=PROJECT_LIST_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["app"]}, "openai/widgetAccessible": True,
                   "openai/visibility": "private"},
        ), types.Tool(
            name="read_plugin_settings", title="플러그인 설정 조회",
            description="Read a non-mutating structured-settings compatibility probe.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            outputSchema=SETTINGS_SCHEMA,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["app"]}, "openai/visibility": "private",
                   "openai/widgetAccessible": True},
        ), types.Tool(
            name="update_plugin_settings", title="플러그인 설정 확인",
            description="Acknowledge the fixed connection status without changing any state.",
            inputSchema={"type": "object", "properties": {
                "set": {"type": "object", "properties": {
                    "connection_status": {"type": "string", "enum": ["connected"]},
                }, "additionalProperties": False},
            }, "required": ["set"], "additionalProperties": False},
            outputSchema={"type": "object", "properties": {"values": {"type": "object"}},
                          "required": ["values"]},
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["app"]}, "openai/visibility": "private",
                   "openai/widgetAccessible": True},
        ), types.Tool(
            name="check_settings_support", title="설정 UI 지원 확인",
            description="Confirm the plugin settings button is callable; changes no files or selections.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=False),
            _meta={"ui": {"visibility": ["app"]}, "openai/visibility": "private",
                   "openai/widgetAccessible": True},
        ), *memory_tools, *progress_tools, observer_tool, *discovered, *execution_tools]

    async def dispatch_tool(name, arguments):
        if name == 'report_work_events':
            project = arguments.get('project')
            if project not in projects or set(arguments) != {'project', 'events'}:
                raise ValueError('An allowed project and event batch are required')
            payload = await asyncio.to_thread(project_state.record_work_events, project,
                                             arguments['events'], provenance='reported')
            return types.CallToolResult(
                content=[types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
            )
        if name in {"read_project_progress", "update_project_progress"}:
            project = arguments.get("project")
            if project not in projects:
                raise ValueError("An allowed project name is required")
            if name == "read_project_progress":
                payload = await asyncio.to_thread(project_state.read_progress, project)
            else:
                payload = await asyncio.to_thread(
                    project_state.update_progress, project, arguments.get("title"),
                    arguments.get("status"), arguments.get("summary"),
                    arguments.get("completed"), arguments.get("next_steps"),
                    arguments.get("expected_revision"))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
            )
        if name in {"read_project_memory", "update_project_memory"}:
            project = arguments.get("project")
            if project not in projects:
                raise ValueError("An allowed project name is required")
            if name == "read_project_memory":
                payload = await asyncio.to_thread(project_state.read_memory, project)
            else:
                payload = await asyncio.to_thread(
                    project_state.update_memory, project, arguments.get("content"),
                    arguments.get("expected_revision"))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
            )
        if name in job_names | git_names:
            project = arguments.get("project")
            if project not in projects:
                raise ValueError("An allowed project name is required")
            if name in git_names:
                if name in {"prepare_git_operation", "execute_git_operation"}:
                    if project_locks[project].locked():
                        raise ValueError("PROJECT_BUSY: wait for the current operation")
                    async with project_locks[project]:
                        operation = asyncio.create_task(asyncio.to_thread(git.call, name, dict(arguments)))
                        try:
                            payload = await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            # Thread cancellation cannot stop an in-flight Git operation;
                            # retain the mutation lane until it actually returns.
                            await operation
                            raise
                else:
                    payload = await asyncio.to_thread(git.call, name, dict(arguments))
            elif name == "list_execution_profiles":
                payload = {"project": project,
                           "profiles": await asyncio.to_thread(policy.list_profiles, project)}
            elif name == "start_command":
                payload = await jobs.start(project, **{
                    key: value for key, value in arguments.items() if key != "project"})
            elif name == "get_job":
                payload = jobs.get(project, arguments["job_id"])
            elif name == "read_job_log":
                payload = jobs.read_log(project, arguments["job_id"],
                                        arguments.get("offset", 0), arguments.get("max_bytes", 16384))
            elif name == "cancel_job":
                payload = await jobs.cancel(project, arguments["job_id"])
            else:
                payload = jobs.list(project, arguments.get("limit", 20))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload)
        if name in {"read_plugin_settings", "update_plugin_settings", "check_settings_support"}:
            log_ui_event("tools/call", tool=name)
            if name == "check_settings_support":
                return types.CallToolResult(content=[types.TextContent(type="text", text=(
                    f"설정 버튼 연결이 확인됐습니다. 허용된 로컬 레포는 {len(projects)}개입니다. "
                    "파일과 대화의 작업 레포는 변경하지 않았습니다."
                ))])
            if name == "update_plugin_settings":
                if arguments.get("set") not in ({}, {"connection_status": "connected"}):
                    raise ValueError("This probe only accepts the fixed connection status")
                payload = {"values": {"connection_status": "connected"}}
            else:
                payload = {"schema": {"type": "object", "properties": {
                    "connection_status": {
                        "type": "string", "title": "연결 확인", "enum": ["connected"],
                        "description": "설정 UI 확인용 고정 값입니다. 작업 레포를 변경하지 않습니다.",
                    },
                }}, "values": {"connection_status": "connected"},
                           "layout": [{"kind": "group", "title": "로컬 레포 연결 확인", "items": [{
                               "kind": "property", "property": "connection_status",
                           }, {
                               "kind": "tool", "tool": "check_settings_support",
                               "title": "설정 UI 지원 확인",
                               "description": "설정 화면의 버튼 동작만 확인합니다. 파일이나 작업 레포를 바꾸지 않습니다.",
                           }]}]}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
            )
        if name in {"list_projects", "open_project_picker", "select_project"}:
            log_ui_event("tools/call", tool=name)
            listing = [{"project": name, "path": str(path)} for name, path in projects.items()]
            payload = {"projects": listing}
            if name == "select_project":
                project = arguments.get("project")
                if project not in projects:
                    raise ValueError("An allowed project name is required")
                payload["selected_project"] = project
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
            )
        if name not in FILE_TOOLS:
            raise ValueError("Unknown tool")
        arguments = dict(arguments)
        project = arguments.pop("project", None)
        if project not in projects:
            raise ValueError("An allowed project name is required; call list_projects first")
        validate_path(projects[project], name, arguments)
        if name in {"create_text_file", "replace_content"}:
            if project_locks[project].locked():
                raise ValueError("PROJECT_BUSY: wait for the command or Git operation before editing")
            async with project_locks[project]:
                operation = asyncio.create_task(worker.request(project, name, arguments))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    await operation
                    raise
        return await worker.request(project, name, arguments)

    def record_activity(project, name, phase, call_id):
        try:
            project_state.record_activity(project, name, phase, call_id)
        except Exception:
            print("MCP activity recording failed", file=sys.stderr, flush=True)

    @server.call_tool()
    async def call_tool(name, arguments):
        arguments = arguments or {}
        call_id = uuid.uuid4().hex
        activity_project = None
        should_record = name in KNOWN_TOOLS
        if should_record and name not in PROJECTLESS_TOOLS:
            candidate = arguments.get("project")
            if candidate in projects:
                activity_project = candidate
            else:
                should_record = False
        if should_record:
            await asyncio.to_thread(record_activity, activity_project, name, "started", call_id)
        try:
            result = await dispatch_tool(name, arguments)
        except asyncio.CancelledError:
            if should_record:
                await asyncio.to_thread(record_activity, activity_project, name, "canceled", call_id)
            raise
        except Exception:
            if should_record:
                await asyncio.to_thread(record_activity, activity_project, name, "failed", call_id)
            raise
        if should_record:
            phase = "failed" if getattr(result, "isError", False) else "completed"
            await asyncio.to_thread(record_activity, activity_project, name, phase, call_id)
        return result

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options(
            experimental_capabilities=SETTINGS_CAPABILITY))


async def serve_with_shutdown(projects):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    stopping = False

    def stop():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    previous = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        await serve(projects)
    except asyncio.CancelledError:
        if not stopping or task.cancelling() != 1:
            raise
        task.uncancel()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--config", type=Path, default=CONFIG)
    selection.add_argument("--project", type=Path, help="Run the legacy single-project backend")
    args = parser.parse_args()
    if args.project is not None:
        os.execv(sys.executable, [sys.executable, str(MODULE_DIR / "serena_backend.py"),
                                 "--project", str(args.project)])
    if Path(sys.prefix) != SERENA_ENV:
        binary = SERENA_ENV / "bin/python"
        os.execv(binary, [str(binary), str(Path(__file__).resolve()), *sys.argv[1:]])
    projects = load_projects(args.config)
    asyncio.run(serve_with_shutdown(projects))


if __name__ == "__main__":
    main()
