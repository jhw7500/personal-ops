"""Source-only, fail-closed read-only tool facade for Apps in Toss TEST console.

This module has no HTTP, OAuth, shell, Docker, file, or credential APIs.
A trusted owner-configured integration must provide a PRE-AUTHORIZED adapter
whose actual provider tool schema has been reviewed. It must invoke ONLY
the official authenticated bundle_list tool using fixed workspace/miniapp IDs.

No such provider is configured by default. Never use raw model-supplied
bundle payloads or a Claude/Codex session token as provider evidence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from toss_console_evidence import (
    APP_NAME, DEPLOYMENT_ID, MINIAPP_ID, WORKSPACE_ID,
    ConsoleEvidenceError, project_bundle_list,
)

PROJECT = "tossApp"
TARGET = "tarororo-test"
CONSOLE_MCP_ORIGIN = "https://mcp.toss.im/adapters/apps-in-toss-console/mcp"
OAUTH_CLIENT_ID = "mcp-gateway"
CALL_TIMEOUT_SECONDS = 10

TOSS_CONSOLE_TOOL_SPECS = [
    {
        "name": "get_toss_test_console_status",
        "title": "Toss TEST 콘솔 번들 읽기 전용 검증",
        "description": (
            "Read only the fixed tarororo Apps in Toss TEST deployment using "
            "a separately authenticated and owner-approved console MCP connection. "
            "No OAuth credentials, raw responses, publish actions or other deployment IDs "
            "can be supplied through chat. Without an authenticated provider returns "
            "CONSOLE_OAUTH_NOT_CONFIGURED. Never triggers upload, invite or review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "enum": [PROJECT]},
                "target": {"type": "string", "enum": [TARGET]},
            },
            "required": ["project", "target"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
]


@dataclass(frozen=True)
class VerifiedProviderContext:
    """Granted by the owning host connector, never from a chat/tool argument.

    oauth_verified and reviewed_tool_schema are assertions by an independently
    trusted integration; the DTO cannot prove their truth in isolation. Direct
    MCP connections pin their observed client ID. Codex-managed authentication
    deliberately leaves client_id unset because app-server does not expose it.
    """

    endpoint: str
    client_id: str | None
    oauth_verified: bool
    reviewed_tool_schema: bool
    authentication_source: str = "direct_mcp"


class ReadOnlyBundleProvider(Protocol):
    @property
    def context(self) -> VerifiedProviderContext: ...

    async def bundle_list(self, *, workspace_id: str, miniapp_id: str) -> Any: ...


ProviderFactory = Callable[[], Awaitable[ReadOnlyBundleProvider | None]]


def _blocked(code: str) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "target": TARGET,
        "app": APP_NAME,
        "workspace_id": WORKSPACE_ID,
        "miniapp_id": MINIAPP_ID,
        "deployment_id": DEPLOYMENT_ID,
        "state": "blocked",
        "code": code,
        "provider_verified": False,
        "test_bundle_ready": False,
        "remote_digest_verified": False,
    }


class TossConsoleReadOnlyTools:
    """Only the injected provider may read the bundle; never runs MCP writes."""

    names = frozenset({"get_toss_test_console_status"})

    def __init__(self, provider: ReadOnlyBundleProvider | None = None, *,
                 provider_factory: ProviderFactory | None = None) -> None:
        self._provider = provider
        self._provider_factory = provider_factory

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "get_toss_test_console_status" or type(arguments) is not dict:
            raise ValueError("INVALID_CONSOLE_ARGUMENTS")
        if arguments != {"project": PROJECT, "target": TARGET}:
            raise ValueError("UNSUPPORTED_SCOPE")
        provider = self._provider
        if provider is None and self._provider_factory is not None:
            try:
                provider = await asyncio.wait_for(
                    self._provider_factory(), timeout=CALL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                return _blocked("CONSOLE_READ_TIMEOUT")
            except ConsoleEvidenceError as exc:
                return _blocked(exc.code)
            except Exception:
                return _blocked("CONSOLE_READ_FAILED")
        if provider is None:
            return _blocked("CONSOLE_OAUTH_NOT_CONFIGURED")

        context = getattr(provider, "context", None)
        if type(context) is not VerifiedProviderContext:
            return _blocked("CONSOLE_TRUST_NOT_ESTABLISHED")
        if context.endpoint != CONSOLE_MCP_ORIGIN:
            return _blocked("CONSOLE_TRUST_NOT_ESTABLISHED")
        trusted_authentication = (
            (context.authentication_source == "direct_mcp"
             and context.client_id == OAUTH_CLIENT_ID)
            or (context.authentication_source == "codex_app_server"
                and context.client_id is None)
        )
        if not trusted_authentication:
            return _blocked("CONSOLE_TRUST_NOT_ESTABLISHED")
        if context.oauth_verified is not True:
            return _blocked("CONSOLE_OAUTH_NOT_CONFIGURED")
        if context.reviewed_tool_schema is not True:
            return _blocked("CONSOLE_TOOL_SCHEMA_NOT_REVIEWED")

        try:
            # The provider implementation must itself pin official bundle_list,
            # reject redirects, validate TLS and OAuth audience, and limit bytes.
            # This facade never receives free-form tool names or arguments.
            raw = await asyncio.wait_for(
                provider.bundle_list(workspace_id=WORKSPACE_ID, miniapp_id=MINIAPP_ID),
                timeout=CALL_TIMEOUT_SECONDS,
            )
            summary = project_bundle_list(
                raw, tool_name="bundle_list",
                workspace_id=WORKSPACE_ID, miniapp_id=MINIAPP_ID,
                deployment_id=DEPLOYMENT_ID,
            )
        except asyncio.TimeoutError:
            return _blocked("CONSOLE_READ_TIMEOUT")
        except ConsoleEvidenceError as exc:
            return _blocked(exc.code)
        except Exception:
            # Do not echo exceptions, URL arguments or raw provider content.
            return _blocked("CONSOLE_READ_FAILED")

        # Provider origin and authorized metadata were checked before the call.
        # This is NOT a remote bundle-SHA claim and is NOT native WebView QA.
        return {
            **summary,
            "state": "verified" if summary["test_bundle_ready"] else "not_ready",
            "provider_verified": True,
            "remote_digest_verified": False,
        }
