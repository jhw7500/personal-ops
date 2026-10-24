"""Normalize documented Claude Code hook metadata into dashboard work events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from uuid import UUID


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")
_SKILL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

_READ_TOOLS = {"read", "read_file", "view_image"}
_FILE_SEARCH_TOOLS = {"find", "find_file", "glob", "grep", "rg", "search_for_pattern"}
_WEB_SEARCH_TOOLS = {"WebSearch", "web_search", "web_search_preview"}
_CODE_TOOLS = {
    "apply_patch", "create_text_file", "edit", "edit_file", "notebookedit",
    "replace_content", "write", "write_file",
}

_SESSION_EVENTS = {"SessionStart", "SessionEnd"}
_PROMPT_STATES = {
    "UserPromptSubmit": ("started", "running"),
    "Stop": ("returned", "unknown"),
    "StopFailure": ("failed", "failed"),
}
_TOOL_STATES = {
    "PreToolUse": "started",
    "PostToolUse": "returned",
    "PostToolUseFailure": "failed",
}
_SUBAGENT_STATES = {"SubagentStart": "started", "SubagentStop": "returned"}


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _session_identifier(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _label(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(character.isprintable() for character in value)


def _valid_time(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _project_for_cwd(cwd, projects):
    if not isinstance(cwd, str) or not cwd or not isinstance(projects, dict):
        return None
    try:
        current = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not current.is_dir():
        return None

    matches = []
    for project, root_value in projects.items():
        if not isinstance(project, str) or not project:
            continue
        try:
            root = Path(root_value).resolve(strict=True)
        except (TypeError, OSError, RuntimeError, ValueError):
            continue
        if current == root or current.is_relative_to(root):
            matches.append((len(root.parts), project, root))
    if not matches:
        return None

    matches.sort(key=lambda item: item[0], reverse=True)
    deepest = matches[0][0]
    deepest_matches = [item for item in matches if item[0] == deepest]
    if len({item[2] for item in deepest_matches}) != len(deepest_matches):
        return None
    return deepest_matches[0][1]


def _tool_action(tool_name):
    normalized = tool_name.lower()
    if normalized in _READ_TOOLS:
        return "read"
    if tool_name in _WEB_SEARCH_TOOLS or normalized in _FILE_SEARCH_TOOLS or normalized == "search":
        return "search"
    if normalized in _CODE_TOOLS:
        return "code"
    return "execute"


def _search_scope(tool_name):
    if tool_name in _WEB_SEARCH_TOOLS:
        return "web"
    if tool_name.lower() in _FILE_SEARCH_TOOLS:
        return "files"
    return None


def _actor(payload, session_id):
    agent_id = payload.get("agent_id")
    if agent_id is None:
        return session_id
    if not _identifier(agent_id):
        return None
    actor_id = f"sub:{session_id}:{agent_id}"
    return actor_id if _identifier(actor_id) else None


def normalize_hook(payload, projects, *, source_id="claude-local", time=None):
    """Return zero or one ``(project, work_event)`` tuple for a Claude hook."""
    if not isinstance(payload, dict) or not _identifier(source_id):
        return []
    if time is not None and not _valid_time(time):
        return []

    hook_name = payload.get("hook_event_name")
    known_hooks = {*_SESSION_EVENTS, *_PROMPT_STATES, *_TOOL_STATES, *_SUBAGENT_STATES}
    if not isinstance(hook_name, str) or hook_name not in known_hooks:
        return []

    project = _project_for_cwd(payload.get("cwd"), projects)
    session_id = payload.get("session_id")
    if project is None or not _session_identifier(session_id):
        return []

    actor_id = _actor(payload, session_id)
    if actor_id is None:
        return []
    if hook_name in _SUBAGENT_STATES and actor_id == session_id:
        return []

    actor_label = payload.get("agent_type")
    if not _label(actor_label, 80):
        actor_label = "Claude"

    event = {
        "schema_version": 1,
        "project": project,
        "source_id": source_id,
        "source_kind": "claude-cli",
        "actor_id": actor_id,
        "actor_kind": "agent",
        "actor_label": actor_label,
    }
    if hook_name in _TOOL_STATES:
        tool_name = payload.get("tool_name")
        tool_use_id = payload.get("tool_use_id")
        task_id = f"tool:{tool_use_id}" if _identifier(tool_use_id) else None
        if not _label(tool_name, 200) or not _identifier(task_id):
            return []
        state = _TOOL_STATES[hook_name]
        if hook_name == "PostToolUseFailure" and payload.get("is_interrupt") is True:
            state = "interrupted"
        event.update({
            "task_id": task_id,
            "task_kind": "call",
            "task_label": tool_name,
            "action": _tool_action(tool_name),
            "state": state,
        })
        if _TOOL_NAME.fullmatch(tool_name) is not None:
            event["tool_name"] = tool_name
        tool_input = payload.get("tool_input")
        skill_id = tool_input.get("skill") if isinstance(tool_input, dict) else None
        if (tool_name == "Skill" and isinstance(skill_id, str)
                and _SKILL_ID.fullmatch(skill_id) is not None):
            event["skill_id"] = skill_id
        search_scope = _search_scope(tool_name)
        if search_scope is not None:
            event["search_scope"] = search_scope
    elif hook_name in _PROMPT_STATES:
        prompt_id = payload.get("prompt_id")
        if prompt_id is None:
            task_id = f"session:{session_id}"
            task_label = "Claude session activity"
            task_kind = "agent"
            state = _PROMPT_STATES[hook_name][1]
        else:
            task_id = f"prompt:{prompt_id}" if _identifier(prompt_id) else None
            task_label = "Claude prompt"
            task_kind = "call"
            state = _PROMPT_STATES[hook_name][0]
        if not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": task_kind,
            "task_label": task_label,
            "action": "execute",
            "state": state,
        })
    elif hook_name in _SUBAGENT_STATES:
        agent_id = payload["agent_id"]
        task_id = f"agent:{session_id}:{agent_id}"
        if not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": "call",
            "task_label": "Claude subagent",
            "action": "execute",
            "state": _SUBAGENT_STATES[hook_name],
        })
    else:
        task_id = f"session:{session_id}"
        if not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": "agent",
            "task_label": "Claude session activity",
            "action": "execute",
            "state": "unknown",
        })

    if time is not None:
        event["time"] = time
    if hook_name == "SessionStart":
        model = payload.get("model")
        if isinstance(model, str) and _MODEL.fullmatch(model) is not None:
            event["model"] = model
    effort = payload.get("effort")
    effort_level = effort.get("level") if isinstance(effort, dict) else None
    if isinstance(effort_level, str) and effort_level in _EFFORT_LEVELS:
        event["reasoning_effort"] = effort_level
    return [(project, event)]
