"""An unconnected, source-only boundary for owner-authorized Toss Console MCP sessions.

Does not establish OAuth, open URLs, read token stores, or create HTTP clients.
A trusted integration must *independently* prove endpoint/TLS, OAuth audience,
client identity, and user consent before passing an authenticated MCP session.
Passing a boolean here is NOT an authentication mechanism.

The module contains one owner-reviewed bundle_list schema/argument pin. No
default contract, authenticated session, credential loader, or live client is
configured.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from toss_console_evidence import ALLOWED_DEPLOYMENT_IDS, DEPLOYMENT_ID, MINIAPP_ID, WORKSPACE_ID
from toss_console_tools import (
    CALL_TIMEOUT_SECONDS, CONSOLE_MCP_ORIGIN, OAUTH_CLIENT_ID, VerifiedProviderContext,
)

OAUTH_RESOURCE = "https://mcp.toss.im"
TOOL_NAME = "bundle_list"
MAX_RESPONSE_BYTES = 131072
MAX_ARG_FIELDS = 4
ARG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()
REVIEWED_CONSOLE_SCHEMA_SHA256 = (
    "bd7021bac9ba68f043abdccedd16494539f2bfab9b895161512cebc6b3d5e614"
)
_REVIEWED_CONSOLE_ARGUMENT_ITEMS = (
    ("workspaceId", 93125),
    ("miniAppId", 75257),
    ("page", 0),
    ("size", 20),
    ("search", DEPLOYMENT_ID),
)
REVIEWED_CONSOLE_ARGUMENTS = dict(_REVIEWED_CONSOLE_ARGUMENT_ITEMS)


class ConsoleSessionContractError(ValueError):
    """Constant, non-sensitive error code. No provider exception content."""


def reviewed_console_arguments(deployment_id: str = DEPLOYMENT_ID) -> dict[str, str | int]:
    """Return a fresh canonical query for one source-reviewed deployment only."""
    if type(deployment_id) is not str or deployment_id not in ALLOWED_DEPLOYMENT_IDS:
        raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
    return {**dict(_REVIEWED_CONSOLE_ARGUMENT_ITEMS), "search": deployment_id}


@dataclass(frozen=True)
class ReviewedBundleListContract:
    """Owned, source-pinned contract, based on the *actual* OAuth tool schema.

    Never instantiate from chat arguments, uploaded text or a remote tool result.
    The provided argument values must be the exact reviewed TEST identifiers.
    """

    schema_sha256: str
    arguments: Mapping[str, str | int]
    endpoint: str = CONSOLE_MCP_ORIGIN
    oauth_resource: str = OAUTH_RESOURCE
    client_id: str = OAUTH_CLIENT_ID
    readonly_policy: str = "provider_hint"
    deployment_id: str = DEPLOYMENT_ID

    def __post_init__(self):
        canonical = reviewed_console_arguments(self.deployment_id)
        if self.deployment_id != DEPLOYMENT_ID and self.readonly_policy != "pinned_bundle_list":
            raise ConsoleSessionContractError("CONSOLE_SCOPE_NOT_REVIEWED")
        if (self.endpoint != CONSOLE_MCP_ORIGIN
                or self.oauth_resource != OAUTH_RESOURCE
                or self.client_id != OAUTH_CLIENT_ID):
            raise ConsoleSessionContractError("CONSOLE_SCOPE_NOT_REVIEWED")
        if type(self.schema_sha256) is not str or not SHA256.fullmatch(self.schema_sha256):
            raise ConsoleSessionContractError("CONSOLE_SCHEMA_NOT_PINNED")
        if self.readonly_policy not in ("provider_hint", "pinned_bundle_list"):
            raise ConsoleSessionContractError("CONSOLE_READONLY_POLICY_NOT_REVIEWED")
        if self.readonly_policy == "pinned_bundle_list":
            if self.schema_sha256 != REVIEWED_CONSOLE_SCHEMA_SHA256:
                raise ConsoleSessionContractError("CONSOLE_SCHEMA_NOT_PINNED")
            if type(self.arguments) is not dict or len(self.arguments) != len(
                    _REVIEWED_CONSOLE_ARGUMENT_ITEMS):
                raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
            for key, expected in canonical.items():
                actual = self.arguments.get(key)
                if type(actual) is not type(expected) or actual != expected:
                    raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
            return
        if type(self.arguments) is not dict or not 1 <= len(self.arguments) <= MAX_ARG_FIELDS:
            raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
        values = set()
        for key, value in self.arguments.items():
            if type(key) is not str or not ARG_NAME.fullmatch(key):
                raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
            if type(value) not in (str, int) or str(value) not in {WORKSPACE_ID, MINIAPP_ID}:
                raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")
            values.add(str(value))
        # A generic account-wide call, or call to a different mini-app, is forbidden.
        if not {WORKSPACE_ID, MINIAPP_ID} <= values:
            raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_NOT_REVIEWED")


def schema_sha256(schema: Any) -> str:
    if type(schema) is not dict or schema.get("type") != "object":
        raise ConsoleSessionContractError("CONSOLE_SCHEMA_UNVERIFIED")
    try:
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ConsoleSessionContractError("CONSOLE_SCHEMA_UNVERIFIED") from None
    if len(encoded) > 32768:
        raise ConsoleSessionContractError("CONSOLE_SCHEMA_UNVERIFIED")
    return hashlib.sha256(encoded).hexdigest()


def _field(obj: Any, key: str, default=None):
    return obj.get(key, default) if type(obj) is dict else getattr(obj, key, default)


def _member(obj: Any, key: str):
    if type(obj) is dict:
        return obj[key] if key in obj else _MISSING
    try:
        return getattr(obj, key)
    except (AttributeError, TypeError):
        return _MISSING


def _decode_result(response: Any) -> Any:
    if _field(response, "isError", _field(response, "is_error", False)) is not False:
        raise ConsoleSessionContractError("CONSOLE_PROVIDER_REJECTED")
    structured = _field(response, "structuredContent", _field(response, "structured_content"))
    if structured is None:
        contents = _field(response, "content")
        if type(contents) is not list or len(contents) != 1:
            raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED")
        part = contents[0]
        if _field(part, "type") != "text":
            raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED")
        text = _field(part, "text")
        if type(text) is not str or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED")
        try:
            structured = json.loads(text)
        except (ValueError, UnicodeError):
            raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED") from None
    if type(structured) not in (dict, list):
        raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED")
    try:
        encoded = json.dumps(structured, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED") from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ConsoleSessionContractError("CONSOLE_RESPONSE_UNVERIFIED")
    if type(structured) is dict and (
        "nextCursor" in structured or "next_cursor" in structured
        or "hasMore" in structured or "has_more" in structured
    ):
        # Do not convert a partial response into a false not-found claim.
        raise ConsoleSessionContractError("CONSOLE_PAGINATION_UNREVIEWED")
    return structured


class ReviewedMcpBundleListProvider:
    """An owner-constructed adapter to an *already authenticated* MCP session.

    Instantiating this type cannot acquire credentials or prove authentication.
    The owner must attach a vetted client session outside model-accessible APIs.
    """

    def __init__(self, session: Any, contract: ReviewedBundleListContract, *,
                 trusted_oauth_session: bool = False) -> None:
        if type(contract) is not ReviewedBundleListContract:
            raise ConsoleSessionContractError("CONSOLE_SCOPE_NOT_REVIEWED")
        self._session = session
        self._contract = contract
        # The owner-reviewed argument mapping must not be mutable by a caller
        # after catalog verification. Retain a private copy for the actual call.
        self._fixed_arguments = dict(contract.arguments)
        self._trusted_oauth_session = trusted_oauth_session is True
        self._ready = False

    @property
    def context(self) -> VerifiedProviderContext:
        return VerifiedProviderContext(
            endpoint=self._contract.endpoint,
            client_id=self._contract.client_id,
            oauth_verified=self._trusted_oauth_session,
            reviewed_tool_schema=self._ready and self._trusted_oauth_session,
        )

    async def verify_catalog(self) -> None:
        """Only list_tools: no provider mutation and no OAuth login.

        Refuse paginated tool catalogs: their completeness must be reviewed first.
        """
        self._ready = False
        if not self._trusted_oauth_session:
            raise ConsoleSessionContractError("CONSOLE_OAUTH_NOT_CONFIGURED")
        try:
            tools_response = await asyncio.wait_for(
                self._session.list_tools(), timeout=CALL_TIMEOUT_SECONDS)
            if (_field(tools_response, "nextCursor") is not None
                    or _field(tools_response, "next_cursor") is not None):
                raise ConsoleSessionContractError("CONSOLE_CATALOG_INCOMPLETE")
            tools = _field(tools_response, "tools")
            if type(tools) is not list or len(tools) > 256:
                raise ConsoleSessionContractError("CONSOLE_CATALOG_UNVERIFIED")
            matches = [t for t in tools if _field(t, "name") == TOOL_NAME]
            if len(matches) != 1:
                raise ConsoleSessionContractError("CONSOLE_TOOL_UNAVAILABLE")
            descriptor = matches[0]
            meta = _member(descriptor, "annotations")
            if meta is _MISSING:
                if (self._contract.readonly_policy != "pinned_bundle_list"
                        or type(descriptor) is not dict):
                    raise ConsoleSessionContractError("CONSOLE_READONLY_NOT_VERIFIED")
            else:
                if meta is None or type(meta) in (
                        str, bytes, list, tuple, set, int, float, bool):
                    raise ConsoleSessionContractError("CONSOLE_READONLY_NOT_VERIFIED")
                camel_hint = _member(meta, "readOnlyHint")
                snake_hint = _member(meta, "read_only_hint")
                if ((camel_hint is _MISSING) == (snake_hint is _MISSING)):
                    raise ConsoleSessionContractError("CONSOLE_READONLY_NOT_VERIFIED")
                annotation = snake_hint if camel_hint is _MISSING else camel_hint
                if annotation is not True:
                    raise ConsoleSessionContractError("CONSOLE_READONLY_NOT_VERIFIED")
            actual_schema = _field(descriptor, "inputSchema", _field(descriptor, "input_schema"))
            observed_sha = schema_sha256(actual_schema)
            if observed_sha != self._contract.schema_sha256:
                raise ConsoleSessionContractError("CONSOLE_SCHEMA_MISMATCH")
            properties = actual_schema.get("properties")
            required = actual_schema.get("required", [])
            if (type(properties) is not dict or type(required) is not list
                    or not all(type(k) is str for k in required)
                    or not set(required) <= set(self._fixed_arguments)
                    or not set(self._fixed_arguments) <= set(properties)):
                raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_SCHEMA_MISMATCH")
            for name, value in self._fixed_arguments.items():
                spec = properties.get(name)
                if (type(spec) is not dict
                        or spec.get("type") != ("integer" if type(value) is int else "string")):
                    raise ConsoleSessionContractError("CONSOLE_ARGUMENTS_SCHEMA_MISMATCH")
        except ConsoleSessionContractError:
            raise
        except Exception:
            raise ConsoleSessionContractError("CONSOLE_CATALOG_READ_FAILED") from None
        self._ready = True

    async def bundle_list(self, *, workspace_id: str, miniapp_id: str) -> Any:
        if not self._trusted_oauth_session:
            raise ConsoleSessionContractError("CONSOLE_OAUTH_NOT_CONFIGURED")
        if not self._ready:
            raise ConsoleSessionContractError("CONSOLE_TOOL_SCHEMA_NOT_REVIEWED")
        if (workspace_id, miniapp_id) != (WORKSPACE_ID, MINIAPP_ID):
            raise ConsoleSessionContractError("CONSOLE_SCOPE_NOT_REVIEWED")
        try:
            # Never forward a user/model-provided arbitrary tool name or payload.
            args = dict(self._fixed_arguments)
            response = await self._session.call_tool(TOOL_NAME, arguments=args)
            return _decode_result(response)
        except ConsoleSessionContractError:
            raise
        except Exception:
            raise ConsoleSessionContractError("CONSOLE_READ_FAILED") from None
