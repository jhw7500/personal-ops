"""Session-bound MCP tool-catalog refresh notification and observation state."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import weakref


NAMES = frozenset({"refresh_mcp_catalog", "get_mcp_catalog_refresh_status"})


class CatalogRefreshError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class _SessionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    request_sequence: int = 0
    status: str = "idle"


def _schema():
    return {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "status": {"type": "string", "enum": [
                "idle", "notification_sent", "catalog_refetched", "notification_failed",
            ]},
            "request_sequence": {"type": "integer", "minimum": 0},
            "notification_sent": {"type": "boolean"},
            "catalog_refetched": {"type": "boolean"},
            "chatgpt_metadata_verified": {"type": "boolean", "const": False},
        },
        "required": [
            "project", "status", "request_sequence", "notification_sent",
            "catalog_refetched", "chatgpt_metadata_verified",
        ],
        "additionalProperties": False,
    }


def _tool(name, title, description, *, readonly):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": ["personal-ops"]},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
        "outputSchema": _schema(),
        "annotations": {
            "readOnlyHint": readonly,
            "destructiveHint": False,
            "idempotentHint": readonly,
            "openWorldHint": False,
        },
        "_meta": {
            "ui": {"visibility": ["model"]},
            "openai/widgetAccessible": False,
            "openai/toolInvocation/invoking": title + " 중입니다",
            "openai/toolInvocation/invoked": title + " 응답을 받았습니다",
        },
    }


def tool_specs(projects):
    if "personal-ops" not in projects:
        return []
    return [
        _tool(
            "refresh_mcp_catalog",
            "MCP 도구 목록 새로고침 요청",
            "Send one MCP tools/list_changed notification on this connection. The client may "
            "then fetch tools/list. This does not restart services or prove that ChatGPT "
            "metadata, model caches, or approval cards were updated.",
            readonly=False,
        ),
        _tool(
            "get_mcp_catalog_refresh_status",
            "MCP 도구 목록 새로고침 상태",
            "Read this connection's in-memory refresh status. catalog_refetched means only "
            "that a later tools/list request completed on the same MCP session; it does not "
            "verify ChatGPT metadata, model caches, or approval cards.",
            readonly=True,
        ),
    ]


class CatalogRefresh:
    def __init__(self, projects):
        self.enabled = "personal-ops" in projects
        self.names = NAMES if self.enabled else frozenset()
        self._states = weakref.WeakKeyDictionary()

    def validate_arguments(self, name, arguments):
        if name not in self.names:
            raise CatalogRefreshError("UNKNOWN_CATALOG_REFRESH_TOOL")
        if not isinstance(arguments, dict) or set(arguments) != {"project"}:
            raise CatalogRefreshError("INVALID_CATALOG_REFRESH_ARGUMENTS")
        if arguments.get("project") != "personal-ops":
            raise CatalogRefreshError("UNSUPPORTED_SCOPE")

    def _state(self, session):
        if session is None:
            raise CatalogRefreshError("INVALID_REQUEST_CONTEXT")
        try:
            state = self._states.get(session)
            if state is None:
                state = _SessionState()
                self._states[session] = state
            return state
        except TypeError:
            raise CatalogRefreshError("INVALID_REQUEST_CONTEXT") from None

    @staticmethod
    def _payload(state):
        return {
            "project": "personal-ops",
            "status": state.status,
            "request_sequence": state.request_sequence,
            "notification_sent": state.status in {"notification_sent", "catalog_refetched"},
            "catalog_refetched": state.status == "catalog_refetched",
            "chatgpt_metadata_verified": False,
        }

    async def call(self, name, arguments, session):
        self.validate_arguments(name, arguments)
        state = self._state(session)
        async with state.lock:
            if name == "get_mcp_catalog_refresh_status":
                return self._payload(state)
            state.request_sequence += 1
            state.status = "notification_failed"
            try:
                await session.send_tool_list_changed()
            except Exception:
                raise CatalogRefreshError("CATALOG_REFRESH_NOTIFICATION_FAILED") from None
            state.status = "notification_sent"
            return self._payload(state)

    async def list_tools(self, session, build, *, client_requested):
        if not client_requested:
            return await build()
        state = self._state(session)
        async with state.lock:
            observed_sequence = state.request_sequence
        result = await build()
        async with state.lock:
            if (observed_sequence > 0 and state.request_sequence == observed_sequence
                    and state.status == "notification_sent"):
                state.status = "catalog_refetched"
        return result


def result(payload, *, error=False):
    return {
        "isError": error,
        "content": [{"type": "text", "text": (
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        )}],
        **({} if error else {"structuredContent": payload}),
    }
