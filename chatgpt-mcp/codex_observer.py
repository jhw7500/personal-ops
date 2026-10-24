#!/usr/bin/env python3
"""Read-only Codex app-server observer for the personal-ops dashboard.

Only allowlisted metadata crosses this boundary. RPC never loads or resumes a
thread or requests turns. A bounded local cursor may extract newly appended
tool identities and outcomes; command text, arguments, output and reasoning
are never retained or sent to the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import time as monotonic_time

from codex_activity import CodexActivityReader
from project_state import ProjectState


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_MODEL = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")
_EFFORT = re.compile(r"[A-Za-z0-9_.:+/-]{1,32}\Z")
_ALLOWED_CALLS = {
    "initialize",
    "thread/loaded/list",
    "thread/read",
    "account/usage/read",
}
_ALLOWED_NOTIFICATIONS = {"initialized"}
_MAX_RPC_BYTES = 2 * 1024 * 1024
_RPC_TIMEOUT_SECONDS = 10.0
_ERROR_CODES = {
    "Invalid projects configuration": "PROJECT_CONFIGURATION_INVALID",
    "No configured project directories are available": "PROJECT_DIRECTORIES_UNAVAILABLE",
    "Invalid managed PID file": "MANAGED_PID_UNAVAILABLE",
    "Managed Codex process is not running": "MANAGED_PROCESS_UNAVAILABLE",
    "Cannot inspect managed Codex process": "MANAGED_PROCESS_UNAVAILABLE",
    "Cannot inspect Unix listeners": "LISTENER_INSPECTION_UNAVAILABLE",
    "Expected exactly one managed Codex listener": "MANAGED_LISTENER_UNAVAILABLE",
    "Invalid managed Codex listener": "MANAGED_LISTENER_INVALID",
    "Managed Codex process changed during discovery": "MANAGED_PROCESS_CHANGED",
    "Invalid managed Codex control listener": "CONTROL_LISTENER_INVALID",
    "Managed Codex listener changed during discovery": "CONTROL_LISTENER_CHANGED",
    "WebSocket transport is unavailable": "TRANSPORT_UNAVAILABLE",
    "Codex RPC timed out": "RPC_TIMEOUT",
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
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
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


def _safe_counter(value):
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= 9007199254740991)


def _usage_for_thread(thread):
    usage = thread.get("threadUsage")
    model = thread.get("model")
    effort = thread.get("reasoningEffort")
    if (not isinstance(usage, dict) or usage.get("threadId") != thread.get("id")
            or not isinstance(model, str) or not isinstance(effort, str)):
        return None
    groups = usage.get("groups")
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        return None
    group = groups[0]
    if group.get("model") != model or group.get("reasoningEffort") != effort:
        return None
    names = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "totalTokens": "total_tokens",
    }
    normalized = {}
    for source, target in names.items():
        if source not in group or group[source] is None:
            continue
        value = group[source]
        if not _safe_counter(value):
            return None
        normalized[target] = value
    return normalized or None


def normalize_thread(thread, projects, *, source_id="codex-local", time=None):
    """Return one allowlisted ``(project, work_event)`` or ``None``.

    ``thread`` may contain private fields, but the returned event is assembled
    from an explicit metadata whitelist rather than copying the input.
    """
    if not isinstance(thread, dict) or not _identifier(source_id):
        return None
    if time is not None and not _valid_time(time):
        return None
    thread_id = thread.get("id")
    project = _project_for_cwd(thread.get("cwd"), projects)
    if project is None or not _identifier(thread_id):
        return None

    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else None
    flags = status.get("activeFlags", []) if isinstance(status, dict) else []
    waiting = (isinstance(flags, list)
               and any(flag in {"waitingOnApproval", "waitingOnUserInput"}
                       for flag in flags))
    if status_type == "active" and waiting:
        state, task_label = "queued", "Codex session waiting"
    elif status_type == "active":
        state, task_label = "running", "Codex session active"
    else:
        state, task_label = "unknown", "Codex session inactive"

    actor_label = thread.get("agentNickname")
    if not _label(actor_label, 80):
        actor_label = thread.get("agentRole")
    if not _label(actor_label, 80):
        actor_label = "Codex"

    event = {
        "schema_version": 1,
        "project": project,
        "source_id": source_id,
        "source_kind": "codex-cli",
        "actor_id": thread_id,
        "actor_kind": "agent",
        "actor_label": actor_label,
        "task_id": "session-activity",
        "task_kind": "agent",
        "task_label": task_label,
        "action": "execute",
        "state": state,
    }
    parent = thread.get("parentThreadId")
    if _identifier(parent):
        event["parent_actor_id"] = parent
    if time is not None:
        event["time"] = time

    model = thread.get("model")
    effort = thread.get("reasoningEffort")
    if isinstance(model, str) and _MODEL.fullmatch(model) is not None:
        event["model"] = model
        event["model_source"] = "configuration"
    if isinstance(effort, str) and _EFFORT.fullmatch(effort) is not None:
        event["reasoning_effort"] = effort
        event["reasoning_effort_source"] = "configuration"

    usage = _usage_for_thread(thread)
    if usage is not None:
        event["usage"] = usage
        event["usage_scope"] = "session"
        event["usage_estimated"] = True
    return project, event


class JsonRpcClient:
    """Minimal JSON-RPC allowlist over an already connected WebSocket."""

    def __init__(self, transport):
        self.transport = transport
        self._next_id = 1

    async def call(self, method, params=None):
        if method not in _ALLOWED_CALLS:
            raise ValueError("RPC method is not allowed")
        request_id = self._next_id
        self._next_id += 1
        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        try:
            return await asyncio.wait_for(self._exchange(request), timeout=_RPC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise RuntimeError("Codex RPC timed out") from None

    async def _exchange(self, request):
        request_id = request["id"]
        await self.transport.send(json.dumps(request, separators=(",", ":")))
        while True:
            raw = await self.transport.recv()
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_RPC_BYTES:
                raise RuntimeError("Invalid RPC response")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Invalid RPC response") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError("Codex RPC request failed")
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("Invalid RPC result")
            return result

    async def notify(self, method, params=None):
        if method not in _ALLOWED_NOTIFICATIONS:
            raise ValueError("RPC notification is not allowed")
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        try:
            await asyncio.wait_for(
                self.transport.send(json.dumps(payload, separators=(",", ":"))),
                timeout=_RPC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Codex RPC timed out") from None


def _loaded_thread_ids(result):
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("Invalid loaded thread list")
    identifiers = []
    for item in data:
        candidate = item if isinstance(item, str) else (
            item.get("id") if isinstance(item, dict) else None
        )
        if not _identifier(candidate):
            raise RuntimeError("Invalid loaded thread identity")
        if candidate not in identifiers:
            identifiers.append(candidate)
    return identifiers


def _inactive_copy(event, timestamp):
    cleared = dict(event)
    cleared["state"] = "unknown"
    cleared["task_label"] = "Codex session inactive"
    for stale_field in (
        "model", "model_source", "reasoning_effort", "reasoning_effort_source",
        "usage", "usage_scope", "usage_estimated",
    ):
        cleared.pop(stale_field, None)
    if timestamp is None:
        cleared.pop("time", None)
    else:
        cleared["time"] = timestamp
    return cleared


def _observation_clock(timestamp):
    if timestamp is None:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(
        timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    )


def _tracking_event(event):
    tracked = dict(event)
    tracked.pop("time", None)
    return tracked


def _tracking_entry(project, current_event, recorded_event, recorded_at):
    return {
        "project": project,
        "current_event": _tracking_event(current_event),
        "last_recorded_event": dict(recorded_event),
        "last_recorded_at": recorded_at.isoformat(),
    }


def _coerce_tracking_entry(value, observed_at):
    if isinstance(value, dict):
        project = value.get("project")
        current = value.get("current_event")
        recorded = value.get("last_recorded_event")
        recorded_at = value.get("last_recorded_at")
        if (isinstance(project, str) and isinstance(current, dict)
                and isinstance(recorded, dict) and isinstance(recorded_at, str)):
            try:
                parsed = _observation_clock(recorded_at)
            except ValueError:
                return None
            return project, current, recorded, parsed
    if isinstance(value, tuple) and len(value) == 2:
        project, event = value
        if isinstance(project, str) and isinstance(event, dict):
            return project, _tracking_event(event), event, observed_at
    return None


def _configuration_signature(project, event):
    fields = (
        "actor_label", "parent_actor_id", "state", "task_label", "model", "model_source",
        "reasoning_effort", "reasoning_effort_source",
    )
    return project, tuple((field, event.get(field)) for field in fields)


def _copy_prior_usage_if_unread(event, previous):
    if not isinstance(previous, dict):
        return
    if (event.get("model") != previous.get("model")
            or event.get("reasoning_effort") != previous.get("reasoning_effort")):
        return
    if "usage" not in previous:
        return
    event["usage"] = dict(previous["usage"])
    event["usage_scope"] = previous["usage_scope"]
    event["usage_estimated"] = previous["usage_estimated"]


async def observe_once(rpc, projects, store, *, read_usage=False, time=None, tracked=None,
                       activity=None):
    """Read loaded Codex threads once and record bounded observed events."""
    if tracked is None:
        tracked = {}
    await rpc.call("initialize", {
        "clientInfo": {"name": "personal-ops-codex-observer", "version": "1.0"},
        "capabilities": {"experimentalApi": True},
    })
    await rpc.notify("initialized", {})
    loaded_ids = _loaded_thread_ids(await rpc.call("thread/loaded/list", {}))

    observed_at = _observation_clock(time)
    normalized = []
    next_tracked = {}
    activity_recorded = 0
    authorized_ids = set()
    for thread_id in loaded_ids:
        result = await rpc.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        })
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise RuntimeError("Invalid thread metadata")
        if read_usage:
            usage_result = await rpc.call("account/usage/read", {"threadId": thread_id})
            thread = dict(thread)
            thread["threadUsage"] = usage_result.get("threadUsage")
        item = normalize_thread(thread, projects, time=time)
        if item is None:
            continue
        project, event = item
        actor_id = event["actor_id"]
        authorized_ids.add(actor_id)
        previous = _coerce_tracking_entry(tracked.get(actor_id), observed_at)
        comparison_event = event
        if previous is not None and not read_usage:
            comparison_event = dict(event)
            _copy_prior_usage_if_unread(comparison_event, previous[1])

        should_record = previous is None
        if previous is not None:
            previous_project, previous_event, _, last_recorded_at = previous
            changed = (_configuration_signature(project, comparison_event)
                       != _configuration_signature(previous_project, previous_event))
            valid_usage_changed = (
                "usage" in event and event.get("usage") != previous_event.get("usage")
            )
            heartbeat_due = (
                event["state"] in {"running", "queued"}
                and (observed_at - last_recorded_at).total_seconds() >= 30
            )
            should_record = changed or valid_usage_changed or heartbeat_due

        if should_record:
            normalized.append((project, event))
            next_tracked[actor_id] = _tracking_entry(
                project, comparison_event, event, observed_at,
            )
        else:
            _, _, last_recorded_event, last_recorded_at = previous
            next_tracked[actor_id] = _tracking_entry(
                project, comparison_event, last_recorded_event, last_recorded_at,
            )

        if activity is not None:
            call_events = activity.read(thread, event, now=observed_at.isoformat())
            normalized.extend((project, call) for call in call_events)
            activity_recorded += len(call_events)

    if activity is not None:
        activity.forget_unloaded(authorized_ids)

    loaded_set = set(loaded_ids)
    for actor_id, previous in tracked.items():
        if actor_id in loaded_set:
            continue
        if not _identifier(actor_id):
            continue
        coerced = _coerce_tracking_entry(previous, observed_at)
        if coerced is None:
            continue
        project, event, last_recorded_event, last_recorded_at = coerced
        if project not in projects or not isinstance(event, dict):
            continue
        if event.get("state") == "unknown":
            next_tracked[actor_id] = _tracking_entry(
                project, event, last_recorded_event, last_recorded_at,
            )
            continue
        inactive = _inactive_copy(event, time)
        normalized.append((project, inactive))
        next_tracked[actor_id] = _tracking_entry(
            project, inactive, inactive, observed_at,
        )

    by_project = {}
    for project, event in normalized:
        by_project.setdefault(project, []).append(event)
    recorded = 0
    for project, events in by_project.items():
        for offset in range(0, len(events), 20):
            batch = events[offset:offset + 20]
            store.record_work_events(project, batch, provenance="observed")
            recorded += len(batch)
    return {"recorded": recorded, "tracked": next_tracked,
            "activity_recorded": activity_recorded}


def _read_managed_pid(pid_file):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(pid_file, flags)
    except OSError as exc:
        raise RuntimeError("Invalid managed PID file") from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > 4096):
            raise RuntimeError("Invalid managed PID file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            data = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Invalid managed PID file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Invalid managed PID file")
    return pid


def _control_endpoint(codex_home, socket_prefix):
    """Read the current CLI control socket without starting or repairing a daemon."""
    directory = codex_home / "app-server-control"
    control = directory / "app-server-control.sock"
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Invalid managed Codex control listener") from exc
    if (not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o022):
        raise RuntimeError("Invalid managed Codex control listener")
    try:
        control_info = control.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Invalid managed Codex control listener") from exc
    if (control_info.st_uid != os.getuid()
            or not (stat.S_ISLNK(control_info.st_mode) or stat.S_ISSOCK(control_info.st_mode))):
        raise RuntimeError("Invalid managed Codex control listener")
    try:
        endpoint = control.resolve(strict=True)
        endpoint_info = endpoint.lstat()
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("Invalid managed Codex control listener") from exc
    if ((stat.S_ISLNK(control_info.st_mode) and not str(endpoint).startswith(socket_prefix))
            or not stat.S_ISSOCK(endpoint_info.st_mode)
            or endpoint_info.st_uid != os.getuid()
            or stat.S_IMODE(endpoint_info.st_mode) & 0o022):
        raise RuntimeError("Invalid managed Codex control listener")

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_ctime_ns)

    try:
        stable = (identity(directory.lstat()) == identity(directory_info)
                  and identity(control.lstat()) == identity(control_info)
                  and control.resolve(strict=True) == endpoint
                  and identity(endpoint.lstat()) == identity(endpoint_info))
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("Managed Codex listener changed during discovery") from exc
    if not stable:
        raise RuntimeError("Managed Codex listener changed during discovery")
    return endpoint


def discover_managed_endpoint(codex_home, *, proc_root=Path("/proc"),
                              socket_prefix="/tmp/codex-daemon-"):
    """Return the stable CLI control listener, or the legacy PID-bound listener."""
    codex_home = Path(codex_home)
    endpoint = _control_endpoint(codex_home, socket_prefix)
    if endpoint is not None:
        return endpoint
    proc_root = Path(proc_root)
    pid_file = codex_home / "app-server-daemon" / "app-server.pid"
    pid = _read_managed_pid(pid_file)
    process_root = proc_root / str(pid)
    if not process_root.is_dir():
        raise RuntimeError("Managed Codex process is not running")

    inodes = set()
    try:
        descriptors = list((process_root / "fd").iterdir())
    except OSError as exc:
        raise RuntimeError("Cannot inspect managed Codex process") from exc
    for descriptor in descriptors:
        try:
            match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(descriptor))
        except OSError:
            continue
        if match:
            inodes.add(match.group(1))

    try:
        lines = (proc_root / "net" / "unix").read_text(encoding="utf-8").splitlines()[1:]
    except OSError as exc:
        raise RuntimeError("Cannot inspect Unix listeners") from exc
    candidates = []
    for line in lines:
        parts = line.split()
        if (len(parts) >= 8 and parts[6] in inodes and parts[3] == "00010000"
                and parts[7].startswith(socket_prefix)):
            candidates.append(Path(parts[7]))
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one managed Codex listener")
    endpoint = candidates[0]
    try:
        info = endpoint.lstat()
    except OSError as exc:
        raise RuntimeError("Invalid managed Codex listener") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Invalid managed Codex listener")
    if _read_managed_pid(pid_file) != pid or not process_root.is_dir():
        raise RuntimeError("Managed Codex process changed during discovery")
    return endpoint


def _load_projects(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Invalid projects configuration") from exc
    configured = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(configured, dict) or not configured:
        raise RuntimeError("Invalid projects configuration")
    projects = {}
    for name, value in configured.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                or not isinstance(value, str) or not Path(value).is_absolute()):
            raise RuntimeError("Invalid projects configuration")
        try:
            root = Path(value).resolve(strict=True)
        except FileNotFoundError:
            print(f"codex-observer project={name} code=DIRECTORY_MISSING",
                  file=sys.stderr, flush=True)
            continue
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Invalid projects configuration") from exc
        if not root.is_dir():
            raise RuntimeError("Invalid projects configuration")
        projects[name] = root
    if not projects:
        raise RuntimeError("No configured project directories are available")
    return projects


def _parser():
    state_root = Path(os.environ.get(
        "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
    )) / "chatgpt-coding-mcp"
    parser = argparse.ArgumentParser(description="Observe loaded Codex sessions read-only")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--projects", type=Path,
                        default=Path.home() / ".config/chatgpt-coding-mcp/projects.json")
    parser.add_argument("--state-dir", type=Path, default=state_root)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--read-usage", action="store_true")
    parser.add_argument("--no-activity", action="store_true",
                        help="Read session metadata only; do not tail local tool activity")
    return parser


async def _run(args):
    if args.interval < 1:
        raise RuntimeError("Observer interval must be at least one second")
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("WebSocket transport is unavailable") from exc

    projects = _load_projects(args.projects)
    store = ProjectState(projects, args.state_dir)
    activity = None if args.no_activity else CodexActivityReader(args.codex_home)
    tracked = {}
    last_usage_read = None
    while True:
        endpoint = discover_managed_endpoint(args.codex_home)
        async with websockets.unix_connect(
            str(endpoint), uri="ws://localhost/", open_timeout=5,
            close_timeout=3, max_size=_MAX_RPC_BYTES, compression=None,
        ) as transport:
            now = monotonic_time.monotonic()
            should_read_usage = bool(
                args.read_usage
                and (last_usage_read is None or now - last_usage_read >= 60)
            )
            result = await observe_once(
                JsonRpcClient(transport), projects, store,
                read_usage=should_read_usage,
                time=datetime.now(timezone.utc).isoformat(),
                tracked=tracked,
                activity=activity,
            )
            tracked = result["tracked"]
            if should_read_usage:
                last_usage_read = now
            print(
                f"codex-observer recorded={result['recorded']} tracked={len(tracked)} "
                f"activity={result['activity_recorded']}",
                flush=True,
            )
        if args.once:
            return
        await asyncio.sleep(args.interval)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except Exception as exc:
        code = _ERROR_CODES.get(str(exc), "OBSERVER_FAILED")
        print(f"codex-observer error={type(exc).__name__} code={code}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
