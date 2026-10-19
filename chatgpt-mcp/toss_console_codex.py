"""Fixed Console reads through an owned Codex client; no model or token extraction.

An owner config pins the executable outside writable projects. Codex owns OAuth
and its credential store. Only a newly created ephemeral thread is used; this
module never attaches to a daemon, resumes a conversation, or starts a turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
from typing import Any

from jobs import private_directory
from toss_console_evidence import ConsoleEvidenceError, DEPLOYMENT_ID, UX019_DEPLOYMENT_ID
from toss_console_sdk_bridge import (
    REVIEWED_CONSOLE_SCHEMA_SHA256, reviewed_console_arguments, ConsoleSessionContractError,
    ReviewedBundleListContract, ReviewedMcpBundleListProvider,
)
from toss_console_tools import CONSOLE_MCP_ORIGIN, VerifiedProviderContext

CONFIG_PATH = Path.home() / ".config/chatgpt-coding-mcp/toss-console.json"
SERVER_NAME = "ait-console"
PLUGIN_ID = "ait@apps-in-toss"
MAX_CONFIG_BYTES = 4096
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_RPC_BYTES = 8 * 1024 * 1024
RPC_METHODS = frozenset({"initialize", "thread/start", "mcpServerStatus/list", "mcpServer/tool/call"})
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _error(code: str) -> ConsoleEvidenceError:
    return ConsoleEvidenceError(code)


def _json(data: bytes) -> Any:
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate key")
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("CONSOLE_RESPONSE_UNVERIFIED") from None


def _outside_projects(path: Path, roots: tuple[Path, ...]) -> None:
    if (not path.is_absolute() or str(path) != os.path.normpath(str(path))
            or any(path.is_relative_to(root) for root in roots)):
        raise _error("CONSOLE_CONFIGURATION_INVALID")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise _error("CONSOLE_CONFIGURATION_INVALID")


def _executable_digest(path: Path) -> str:
    # The installed native Codex binary exceeds the host-command artifact
    # helper's 128 MiB limit. Bound this read independently at 512 MiB.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.getuid())
                    or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                    or not before.st_mode & stat.S_IXUSR or not 0 < before.st_size <= 512 * 1024 * 1024):
                raise _error("CONSOLE_CLIENT_CHANGED")
            digest = hashlib.sha256()
            count = 0
            while block := source.read(min(1024 * 1024, before.st_size + 1 - count)):
                count += len(block)
                digest.update(block)
            after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            if count != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
                raise _error("CONSOLE_CLIENT_CHANGED")
            return digest.hexdigest()
    except OSError:
        raise _error("CONSOLE_CLIENT_CHANGED") from None


def _configuration(path: Path, roots: tuple[Path, ...]) -> tuple[str, str] | None:
    _outside_projects(path, roots)
    try:
        prior = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise _error("CONSOLE_CONFIGURATION_INVALID") from None
    if (not stat.S_ISREG(prior.st_mode) or prior.st_uid != os.getuid()
            or stat.S_IMODE(prior.st_mode) != 0o600 or prior.st_nlink != 1
            or not 0 < prior.st_size <= MAX_CONFIG_BYTES):
        raise _error("CONSOLE_CONFIGURATION_INVALID")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size) != (
                    prior.st_dev, prior.st_ino, prior.st_mode, prior.st_size):
                raise _error("CONSOLE_CONFIGURATION_INVALID")
            payload = source.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(source.fileno())
            if (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (
                    opened.st_mtime_ns, opened.st_ctime_ns, opened.st_size):
                raise _error("CONSOLE_CONFIGURATION_INVALID")
        cfg = _json(payload)
    except OSError:
        raise _error("CONSOLE_CONFIGURATION_INVALID") from None
    if (len(payload) > MAX_CONFIG_BYTES or type(cfg) is not dict
            or set(cfg) != {"version", "enabled", "codex_executable", "codex_sha256"}
            or type(cfg["version"]) is not int or cfg["version"] != 1
            or type(cfg["enabled"]) is not bool
            or type(cfg["codex_executable"]) is not str
            or not 0 < len(cfg["codex_executable"]) <= 1024
            or not cfg["codex_executable"].isprintable()
            or type(cfg["codex_sha256"]) is not str
            or not SHA256.fullmatch(cfg["codex_sha256"])):
        raise _error("CONSOLE_CONFIGURATION_INVALID")
    if not cfg["enabled"]:
        return None
    executable = Path(cfg["codex_executable"])
    _outside_projects(executable, roots)
    observed = _executable_digest(executable)
    if observed != cfg["codex_sha256"]:
        raise _error("CONSOLE_CLIENT_CHANGED")
    return str(executable), observed


def _client_env() -> dict[str, str]:
    allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "CODEX_HOME",
               "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME",
               "XDG_DATA_HOME", "XDG_STATE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["RUST_LOG"] = "error"
    return env


async def _stop(process) -> None:
    # Every process supplied here was created with start_new_session=True by
    # this module. Never accept a PID, socket or process identity from a caller.
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


class _CodexClient:
    def __init__(self, executable: str, cwd: Path):
        self.executable = executable
        self.cwd = cwd
        self.process = None
        self.thread_id = None
        self.sequence = 0
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        # Public CLI metadata only. Reject headers/bearer overrides; the official
        # client must own an OAuth connection to the exact Console endpoint.
        child = await asyncio.create_subprocess_exec(
            self.executable, "--no-daemon", "mcp", "get", SERVER_NAME, "--json", cwd=self.cwd,
            env=_client_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        try:
            try:
                await child.stdout.readexactly(65537)
            except asyncio.IncompleteReadError as exc:
                data = exc.partial
            else:
                raise _error("CONSOLE_CLIENT_CONFIGURATION_MISMATCH")
            await child.wait()
            if child.returncode != 0 or len(data) > 65536:
                raise _error("CONSOLE_CLIENT_CONFIGURATION_MISMATCH")
            metadata = _json(data)
            transport = metadata.get("transport") if type(metadata) is dict else None
            if (type(metadata) is not dict
                    or metadata.get("name") != SERVER_NAME or metadata.get("enabled") is not True
                    or type(transport) is not dict or transport.get("type") != "streamable_http"
                    or transport.get("url") != CONSOLE_MCP_ORIGIN
                    or any(transport.get(key) not in (None, {}) for key in (
                        "http_headers", "env_http_headers", "bearer_token_env_var", "http_headers_helper"))):
                raise _error("CONSOLE_CLIENT_CONFIGURATION_MISMATCH")
        finally:
            await _stop(child)
        self.process = await asyncio.create_subprocess_exec(
            self.executable, "--no-daemon", "app-server", "--listen", "stdio://", cwd=self.cwd,
            env=_client_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=MAX_LINE_BYTES)
        await self.request("initialize", {
            "clientInfo": {"name": "personal_ops_console", "version": "1.0.0"},
            "capabilities": {"experimentalApi": False, "explicitGatewayOauth": True}})
        await self._send({"method": "initialized", "params": {}})
        thread = await self.request("thread/start", {
            "cwd": str(self.cwd), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
            "config": {f'plugins."{PLUGIN_ID}".mcp_servers."{SERVER_NAME}".enabled_tools': ["bundle_list"]}})
        try:
            self.thread_id = thread["thread"]["id"]
        except (KeyError, TypeError):
            raise _error("CONSOLE_RESPONSE_UNVERIFIED") from None
        if type(self.thread_id) is not str or not 0 < len(self.thread_id) <= 128:
            raise _error("CONSOLE_RESPONSE_UNVERIFIED")

    async def _send(self, value: dict) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            raise _error("CONSOLE_CLIENT_DISCONNECTED")
        process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()

    async def request(self, method: str, params: dict) -> Any:
        if method not in RPC_METHODS:
            raise _error("UNAUTHORIZED_CONSOLE_TOOL")
        async with self.lock:
            self.sequence += 1
            request_id = self.sequence
            await self._send({"id": request_id, "method": method, "params": params})
            consumed = 0
            for _ in range(256):
                line = await self.process.stdout.readline()
                consumed += len(line)
                if not line or consumed > MAX_RPC_BYTES:
                    raise _error("CONSOLE_CLIENT_DISCONNECTED")
                response = _json(line)
                if type(response) is not dict:
                    raise _error("CONSOLE_RESPONSE_UNVERIFIED")
                if (type(response.get("id")) is int and response["id"] == request_id
                        and "method" not in response):
                    if "error" in response or "result" not in response:
                        raise _error("CONSOLE_CLIENT_RPC_FAILED")
                    return response["result"]
                if "id" in response and "method" in response:
                    # No approval, elicitation, tool execution or OAuth callback
                    # can be delegated back to this narrow protocol adapter.
                    await self._send({"id": response["id"], "error": {
                        "code": -32601, "message": "Unsupported request"}})
                elif response.get("method") == "turn/started":
                    raise _error("UNAUTHORIZED_CONSOLE_TOOL")
            raise _error("CONSOLE_RESPONSE_UNVERIFIED")

    async def catalog(self) -> dict:
        result = await self.request("mcpServerStatus/list", {
            "threadId": self.thread_id, "serverName": SERVER_NAME,
            "detail": "toolsAndAuthOnly", "limit": 1})
        if (type(result) is not dict or result.get("nextCursor") is not None
                or type(result.get("data")) is not list or len(result["data"]) != 1):
            raise _error("CONSOLE_RESPONSE_UNVERIFIED")
        row = result["data"][0]
        if (type(row) is not dict or row.get("name") != SERVER_NAME
                or row.get("pluginId") != PLUGIN_ID or row.get("httpOrigin") != "https://mcp.toss.im"
                or row.get("toolsError") is not None):
            raise _error("CONSOLE_CLIENT_CONFIGURATION_MISMATCH")
        if row.get("authStatus") != "oAuth":
            raise _error("CONSOLE_OAUTH_NOT_CONFIGURED")
        tools = row.get("tools")
        if type(tools) is not dict or len(tools) > 256:
            raise _error("CONSOLE_RESPONSE_UNVERIFIED")
        selected = [tool for tool in tools.values() if type(tool) is dict and tool.get("name") == "bundle_list"]
        if len(selected) != 1:
            raise _error("CONSOLE_TOOL_UNAVAILABLE")
        return {"tools": selected}

    async def call_tool(self, name, *, arguments):
        if name != "bundle_list" or type(arguments) is not dict:
            raise _error("UNAUTHORIZED_CONSOLE_TOOL")
        try:
            canonical = reviewed_console_arguments(arguments.get("search"))
        except ConsoleSessionContractError:
            raise _error("UNAUTHORIZED_CONSOLE_TOOL") from None
        if (arguments != canonical
                or any(type(arguments[k]) is not type(v) for k, v in canonical.items())):
            raise _error("UNAUTHORIZED_CONSOLE_TOOL")
        return await self.request("mcpServer/tool/call", {
            "threadId": self.thread_id, "server": SERVER_NAME, "tool": "bundle_list",
            "arguments": canonical})

    async def close(self) -> None:
        process, self.process = self.process, None
        await _stop(process)


class _CatalogSession:
    def __init__(self, client: _CodexClient, catalog: dict):
        self.client, self.catalog = client, catalog

    async def list_tools(self):
        return self.catalog

    async def call_tool(self, name, *, arguments):
        return await self.client.call_tool(name, arguments=arguments)


class _ManagedProvider:
    def __init__(self, reviewed: ReviewedMcpBundleListProvider):
        self.reviewed = reviewed

    @property
    def context(self):
        verified = self.reviewed.context
        # Native status proves managed OAuth, not an exported client ID. Never
        # present the plugin's documented ID as an observed credential claim.
        return VerifiedProviderContext(
            endpoint=verified.endpoint, client_id=None,
            oauth_verified=verified.oauth_verified,
            reviewed_tool_schema=verified.reviewed_tool_schema,
            authentication_source="codex_app_server")

    async def bundle_list(self, **kwargs):
        return await self.reviewed.bundle_list(**kwargs)


class CodexConsoleConnection:
    """Lazy owner connection; missing config never launches a client or network call."""

    def __init__(self, projects, state_dir: Path, *, config_path: Path = CONFIG_PATH):
        self.roots = tuple(Path(p).resolve() for p in projects.values())
        self.state_dir = Path(state_dir).absolute()
        self.config_path = Path(config_path).absolute()
        self._client = None
        self._identity = None
        self._lock = asyncio.Lock()

    async def get_ux019_provider(self):
        return await self.get_provider(deployment_id=UX019_DEPLOYMENT_ID)

    async def get_provider(self, *, deployment_id: str = DEPLOYMENT_ID):
        # Validate before reading configuration, creating a process or resetting a session.
        try:
            canonical = reviewed_console_arguments(deployment_id)
        except ConsoleSessionContractError:
            raise _error("WRONG_CONSOLE_TARGET") from None
        async with self._lock:
            try:
                configuration = _configuration(self.config_path, self.roots)
                if configuration is None:
                    await self._reset()
                    return None
                if (self._client is not None and (configuration != self._identity
                        or self._client.process is None or self._client.process.returncode is not None)):
                    await self._reset()
                if self._client is None:
                    _outside_projects(self.state_dir, self.roots)
                    private_directory(self.state_dir)
                    self._client = _CodexClient(configuration[0], self.state_dir)
                    await self._client.open()
                    self._identity = configuration
                # Refresh the raw catalogue and auth status before EVERY query.
                catalog = await self._client.catalog()
                provider = ReviewedMcpBundleListProvider(
                    _CatalogSession(self._client, catalog),
                    ReviewedBundleListContract(
                        schema_sha256=REVIEWED_CONSOLE_SCHEMA_SHA256,
                        arguments=canonical, readonly_policy="pinned_bundle_list",
                        deployment_id=deployment_id),
                    trusted_oauth_session=True)
                await provider.verify_catalog()
                return _ManagedProvider(provider)
            except BaseException:
                await self._reset()
                raise

    async def _reset(self):
        client, self._client = self._client, None
        self._identity = None
        if client is not None:
            await client.close()

    async def close(self):
        async with self._lock:
            await self._reset()
