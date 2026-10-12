"""Normalize trusted Codex hook metadata into dashboard work events.

The normalizer deliberately accepts only documented identity and lifecycle
metadata. Prompt text, tool inputs and outputs, transcripts, and unknown hook
fields never cross this boundary.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")

_READ_TOOLS = {
    "read", "read_file", "view_image",
}
_SEARCH_TOOLS = {
    "find", "find_file", "glob", "grep", "rg", "search", "search_for_pattern",
}
_CODE_TOOLS = {
    "apply_patch", "create_text_file", "edit", "edit_file", "replace_content", "write",
    "write_file",
}

_TURN_STATES = {
    "UserPromptSubmit": "started",
    "Stop": "returned",
    "Interrupt": "interrupted",
}
_TOOL_STATES = {
    "PreToolUse": "started",
    "PostToolUse": "returned",
}
_SUBAGENT_STATES = {
    "SubagentStart": "started",
    "SubagentStop": "returned",
}


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


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
    except (OSError, RuntimeError):
        return None
    if not current.is_dir():
        return None

    matches = []
    for project, root_value in projects.items():
        if not isinstance(project, str) or not project:
            continue
        try:
            root = Path(root_value).resolve(strict=True)
        except (TypeError, OSError, RuntimeError):
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
    if normalized in _SEARCH_TOOLS:
        return "search"
    if normalized in _CODE_TOOLS:
        return "code"
    return "execute"


def normalize_hook(payload, projects, *, source_id="codex-local", time=None):
    """Return zero or one ``(project, work_event)`` tuple for a Codex hook.

    Invalid and incomplete hook messages are ignored. This makes the function
    safe to call directly from a hook process without turning malformed input
    into partial activity records.
    """
    if not isinstance(payload, dict) or not _identifier(source_id):
        return []
    if time is not None and not _valid_time(time):
        return []

    hook_name = payload.get("hook_event_name")
    if hook_name not in {*_TURN_STATES, *_TOOL_STATES, *_SUBAGENT_STATES}:
        return []

    project = _project_for_cwd(payload.get("cwd"), projects)
    session_id = payload.get("session_id")
    if project is None or not _identifier(session_id):
        return []

    agent_id = payload.get("agent_id")
    if agent_id is not None and not _identifier(agent_id):
        return []
    if hook_name in _SUBAGENT_STATES and not _identifier(agent_id):
        return []

    actor_id = agent_id or session_id
    actor_label = payload.get("agent_type")
    if not _label(actor_label, 80):
        actor_label = "Codex"

    event = {
        "schema_version": 1,
        "project": project,
        "source_id": source_id,
        "source_kind": "codex-cli",
        "actor_id": actor_id,
        "actor_kind": "agent",
        "actor_label": actor_label,
    }
    if hook_name in _TOOL_STATES:
        tool_name = payload.get("tool_name")
        tool_use_id = payload.get("tool_use_id")
        if not _identifier(tool_use_id):
            return []
        task_id = "tool:" + tool_use_id
        if not _label(tool_name, 200) or not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": "call",
            "task_label": tool_name,
            "action": _tool_action(tool_name),
            "state": _TOOL_STATES[hook_name],
        })
    elif hook_name in _TURN_STATES:
        turn_id = payload.get("turn_id")
        if not _identifier(turn_id):
            return []
        task_id = "turn:" + turn_id
        if not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": "call",
            "task_label": "Codex turn",
            "action": "execute",
            "state": _TURN_STATES[hook_name],
        })
    else:
        task_id = "agent:" + agent_id
        if not _identifier(task_id):
            return []
        event.update({
            "task_id": task_id,
            "task_kind": "call",
            "task_label": "Codex subagent",
            "action": "execute",
            "state": _SUBAGENT_STATES[hook_name],
        })

    if time is not None:
        event["time"] = time
    model = payload.get("model")
    if isinstance(model, str) and _MODEL.fullmatch(model) is not None:
        event["model"] = model
    return [(project, event)]
