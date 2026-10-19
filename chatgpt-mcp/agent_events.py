"""Codex JSONL metadata allowlist. No commands, output, prompts or reasoning escape."""
from __future__ import annotations

import json
import re

PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
MODEL = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
MAX_LINE = 256 * 1024
PUBLIC_PHASES = frozenset({"STARTED", "WORKING", "COMPLETED", "FAILED"})
PUBLIC_CODES = frozenset({
    "AGENT_STARTED", "TURN_COMPLETED", "AGENT_ERROR",
    "COMMAND_ACTIVITY", "FILE_CHANGE",
})
MAX_PUBLIC_EVENT_BYTES = 4096

def _safe_path(value):
    return (isinstance(value, str) and len(value) <= 512 and PATH.fullmatch(value) is not None
            and all(part not in {".", "..", ".git", ".env", ".codex"}
                    and not part.startswith(".env.") for part in value.split("/")))

def _usage(value):
    if not isinstance(value, dict):
        return None
    keys = ("input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens")
    return {key: value[key] for key in keys
            if key in value and type(value[key]) is int and 0 <= value[key] <= 10 ** 12} or None

def parse_codex_event(value):
    if not isinstance(value, dict):
        return None
    typ = value.get("type")
    if typ in {"thread.started", "turn.started"}:
        return {"phase": "STARTED", "code": "AGENT_STARTED"}
    if typ == "turn.completed":
        event = {"phase": "COMPLETED", "code": "TURN_COMPLETED"}
        usage = _usage(value.get("usage"))
        if usage is not None:
            event["usage"] = usage
            event["usage_scope"] = "turn"
        model = value.get("model")
        if isinstance(model, str) and MODEL.fullmatch(model):
            event["model"] = model
            event["model_source"] = "event"
        return event
    if typ in {"turn.failed", "error"}:
        return {"phase": "FAILED", "code": "AGENT_ERROR"}
    if typ in {"item.started", "item.completed", "item.updated"}:
        item = value.get("item")
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind in {"reasoning", "agent_message", "mcp_tool_call"}:
            return None
        if kind == "command_execution":
            # Never send command/stdout/stderr/exit details from arbitrary code.
            return {"phase": "WORKING", "code": "COMMAND_ACTIVITY"}
        if kind == "file_change":
            paths = item.get("changes", [])
            if not isinstance(paths, list):
                return None
            safe = [entry["path"] for entry in paths
                    if isinstance(entry, dict) and _safe_path(entry.get("path"))]
            return {"phase": "WORKING", "code": "FILE_CHANGE",
                    "paths": safe[:20]} if safe else None
    return None


def sanitize_agent_event(value):
    """Copy only bounded execution metadata suitable for public task state."""
    if (not isinstance(value, dict) or value.get("phase") not in PUBLIC_PHASES
            or value.get("code") not in PUBLIC_CODES):
        return None
    event = {"phase": value["phase"], "code": value["code"]}
    if isinstance(value.get("paths"), list):
        paths = [path for path in value["paths"] if _safe_path(path)][:20]
        if paths:
            event["paths"] = paths
    usage = _usage(value.get("usage"))
    scope = value.get("usage_scope")
    if usage is not None and scope in {"request", "turn", "session", "task"}:
        event["usage"] = usage
        event["usage_scope"] = scope
    model = value.get("model")
    if isinstance(model, str) and MODEL.fullmatch(model):
        event["model"] = model
        if value.get("model_source") in {"configuration", "event"}:
            event["model_source"] = value["model_source"]
    if len(json.dumps(event, separators=(",", ":")).encode("utf-8")) > MAX_PUBLIC_EVENT_BYTES:
        return None
    return event
