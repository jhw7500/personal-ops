"""Passive discovery evidence from the documented local tunnel health route.

This never sends JSON-RPC on another owner's stdio or claims that a health
read initializes MCP. ChatGPT's explicit Refresh remains the discovery path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from rollout_tunnel import TunnelSwitchError

URL_FILE = Path.home() / ".local/state/tunnel-client/health/personal-ops-coding.url"
PENDING = {"catalog_status": "awaiting_discovery", "tools_registered": False,
           "connector_schemas_verified": False, "tool_count": 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def observe_tools(expected, *, url_file=URL_FILE, urlopen_fn=None):
    path = Path(url_file).absolute()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return dict(PENDING)
    if (any(folder.is_symlink() for folder in [path, *path.parents])
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2048):
        raise TunnelSwitchError("UNSAFE_MCP_HEALTH_URL")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(2049)
        address = raw.decode("ascii", errors="strict").strip()
        url = urlsplit(address)
        valid_port = url.port
    except (OSError, UnicodeError, ValueError) as exc:
        raise TunnelSwitchError("UNSAFE_MCP_HEALTH_URL") from exc
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/") or not valid_port):
        raise TunnelSwitchError("UNSAFE_MCP_HEALTH_URL")
    getter = urlopen_fn or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()).open
    try:
        with getter(address.rstrip("/") + "/health/mcp", timeout=3) as response:
            if response.status != 200:
                return dict(PENDING)
            body = response.read(262145)
    except (OSError, urllib.error.URLError):
        return dict(PENDING)
    try:
        if len(body) > 262144:
            raise ValueError()
        data = json.loads(body)
        details = data["details"]
        observed = details["tools_list"]
        names = observed["tool_names"]
        if (not isinstance(names, list) or len(names) > 2000
                or any(not isinstance(name, str) or
                       not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", name) for name in names)):
            raise ValueError()
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise TunnelSwitchError("UNEXPECTED_MCP_DISCOVERY") from exc
    if (details.get("channel") != "main" or details.get("transport") != "stdio"
            or details.get("child_state") != "running" or details.get("evidence") != "same_child"
            or observed.get("ok") is not True or observed.get("complete") is not True
            or observed.get("partial") is not False or observed.get("limited") is not False):
        return dict(PENDING)
    if type(observed.get("retained_count")) is not int or observed["retained_count"] != len(names):
        raise TunnelSwitchError("UNEXPECTED_MCP_DISCOVERY")
    if not set(expected) <= set(names):
        raise TunnelSwitchError("LIVE_TOOLS_MISMATCH")
    return {"catalog_status": "observed", "tools_registered": True,
            "connector_schemas_verified": False, "tool_count": len(names)}
