#!/usr/bin/env python3
"""Record bounded Claude subagent status metadata for existing hook lifecycles."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr
import fcntl
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import time


MAX_INPUT_BYTES = 256 * 1024
MAX_TASKS = 64
MAX_CACHE_ENTRIES = 256
MAX_CACHE_BYTES = 128 * 1024
DEADLINE_SECONDS = 1.0
RUNNING_REFRESH_SECONDS = 30.0
STATUS_STATES = {
    "running": "running",
    "completed": "returned",
    "failed": "failed",
    "killed": "interrupted",
}
REQUIRED_RUNTIME_FILES = {
    "claude_hooks.py", "codex_observer.py", "codex_activity.py", "project_state.py",
}
_RELEASE_ID = re.compile(r"[0-9a-f]{20}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def _deadline_expired(_signum, _frame):
    raise TimeoutError


def _owned_directory(path, mode=0o700):
    info = Path(path).lstat()
    if (Path(path).is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("unsafe directory")


def _owned_file(path, mode=0o600):
    info = Path(path).lstat()
    if (Path(path).is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("unsafe file")


def _verified_runtime(value):
    releases = (Path.home() / ".local/lib/chatgpt-coding-mcp/releases").absolute()
    runtime = Path(value).absolute()
    _owned_directory(releases)
    _owned_directory(runtime)
    if runtime.parent != releases or _RELEASE_ID.fullmatch(runtime.name) is None:
        raise ValueError("invalid runtime")

    manifest_path = runtime / "manifest.json"
    _owned_file(manifest_path)
    raw = manifest_path.read_bytes()
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError("invalid manifest")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or not REQUIRED_RUNTIME_FILES.issubset(manifest)
            or not manifest):
        raise ValueError("invalid manifest")
    for name, expected in manifest.items():
        relative = Path(name)
        if (not isinstance(name, str) or relative.is_absolute() or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or not isinstance(expected, str) or _DIGEST.fullmatch(expected) is None):
            raise ValueError("invalid manifest")
        path = runtime / relative
        parent = path.parent
        while parent != runtime:
            _owned_directory(parent)
            parent = parent.parent
        _owned_file(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("runtime content mismatch")
    snapshot = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()[:20]
    if snapshot != runtime.name:
        raise ValueError("runtime manifest mismatch")
    return runtime


def _runtime_imports(runtime):
    sys.path.insert(0, str(runtime))
    claude_hooks = importlib.import_module("claude_hooks")
    codex_observer = importlib.import_module("codex_observer")
    project_state = importlib.import_module("project_state")
    return claude_hooks, codex_observer._load_projects, project_state


def _normalize_payload(payload, projects, claude_hooks):
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    if not claude_hooks._session_identifier(session_id):
        return None
    project = claude_hooks._project_for_cwd(payload.get("cwd"), projects)
    if project is None:
        return None
    tasks = payload.get("tasks")
    if tasks is None or tasks == []:
        return project, session_id, []
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        return None

    normalized = []
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or task.get("type") != "local_agent":
            return None
        registry_id = task.get("id")
        agent_type = task.get("agentType")
        state = STATUS_STATES.get(task.get("status"))
        if (not claude_hooks._identifier(registry_id)
                or not claude_hooks._label(agent_type, 80) or state is None
                or registry_id in seen):
            return None
        actor_id = f"sub:{session_id}:{registry_id}"
        task_id = f"agent:{session_id}:{registry_id}"
        if not claude_hooks._identifier(actor_id) or not claude_hooks._identifier(task_id):
            return None
        seen.add(registry_id)
        normalized.append({
            "registry_id": registry_id,
            "actor_id": actor_id,
            "task_id": task_id,
            "actor_label": agent_type,
            "state": state,
        })
    return project, session_id, normalized


def _normalize_hook_registry(payload, projects, claude_hooks):
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") not in {"Stop", "SubagentStop"}:
        return None
    session_id = payload.get("session_id")
    if not claude_hooks._session_identifier(session_id):
        return None
    project = claude_hooks._project_for_cwd(payload.get("cwd"), projects)
    if project is None:
        return None
    tasks = payload.get("background_tasks")
    if tasks is None or tasks == []:
        return project, session_id, []
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        return None

    normalized = []
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            return None
        if task.get("type") != "subagent":
            continue
        registry_id = task.get("id")
        agent_type = task.get("agent_type")
        state = STATUS_STATES.get(task.get("status"))
        if (not claude_hooks._identifier(registry_id)
                or not claude_hooks._label(agent_type, 80) or state is None
                or registry_id in seen):
            return None
        actor_id = f"sub:{session_id}:{registry_id}"
        task_id = f"agent:{session_id}:{registry_id}"
        if not claude_hooks._identifier(actor_id) or not claude_hooks._identifier(task_id):
            return None
        seen.add(registry_id)
        normalized.append({
            "registry_id": registry_id,
            "actor_id": actor_id,
            "task_id": task_id,
            "actor_label": agent_type,
            "state": state,
        })
    return project, session_id, normalized


def _cache_paths(state_dir, project, session_id, project_state):
    root = project_state._private_directory(Path(state_dir).absolute()
                                            / "claude-subagent-status")
    project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()
    project_dir = project_state._private_directory(root / project_key)
    session_key = hashlib.sha256(session_id.encode("ascii")).hexdigest()
    return project_dir / (session_key + ".json"), project_dir / (session_key + ".lock")


@contextmanager
def _cache_lock(path, project_state):
    project_state._reject_symlink_ancestors(path)
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("unsafe cache lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _empty_cache(project, session_id):
    return {
        "schema_version": 1,
        "project": project,
        "session_id": session_id,
        "source_id": "claude-local",
        "entries": {},
    }


def _load_cache(path, project, session_id, project_state, claude_hooks):
    if not path.exists() and not path.is_symlink():
        return _empty_cache(project, session_id)
    raw = project_state._read_private_bytes(path, MAX_CACHE_BYTES)
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("project") != project or value.get("session_id") != session_id
            or value.get("source_id") != "claude-local"
            or not isinstance(value.get("entries"), dict)
            or len(value["entries"]) > MAX_CACHE_ENTRIES):
        raise ValueError("invalid cache")
    for registry_id, entry in value["entries"].items():
        if (not claude_hooks._identifier(registry_id) or not isinstance(entry, dict)
                or set(entry) != {"actor_id", "task_id", "state", "last_emitted"}
                or entry.get("actor_id") != f"sub:{session_id}:{registry_id}"
                or entry.get("task_id") != f"agent:{session_id}:{registry_id}"
                or entry.get("state") not in STATUS_STATES.values()
                or isinstance(entry.get("last_emitted"), bool)
                or not isinstance(entry.get("last_emitted"), (int, float))
                or not 0 <= entry["last_emitted"] <= time.time() + 300):
            raise ValueError("invalid cache")
    return value


def _save_cache(path, cache, project_state):
    entries = cache["entries"]
    if len(entries) > MAX_CACHE_ENTRIES:
        oldest = sorted(entries, key=lambda key: (entries[key]["last_emitted"], key))
        for key in oldest[:len(entries) - MAX_CACHE_ENTRIES]:
            del entries[key]
    data = json.dumps(cache, ensure_ascii=True, separators=(",", ":")).encode()
    if len(data) > MAX_CACHE_BYTES:
        raise ValueError("cache too large")
    project_state._reject_symlink_ancestors(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".claude-status-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        project_state._private_file(temporary)
        os.replace(temporary, path)
        project_state._private_file(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _authoritative(record, item):
    common_identity = (
        record.get("source_id") == "claude-local"
        and record.get("source_kind") == "claude-cli"
        and record.get("actor_id") == item["actor_id"]
        and record.get("actor_kind") == "agent"
        and record.get("task_kind") == "call"
        and record.get("provenance") == "observed"
    )
    if not common_identity:
        return False
    native_start = (
        record.get("task_id") == item["task_id"]
        and record.get("task_label") == "Claude subagent"
        and record.get("action") == "execute"
        and record.get("state") == "started"
    )
    child_tool = (
        isinstance(record.get("task_id"), str)
        and record["task_id"].startswith("tool:")
        and record.get("state") in {"started", "returned", "failed", "interrupted"}
    )
    return native_start or child_tool


def _event(item):
    return {
        "schema_version": 1,
        "source_id": "claude-local",
        "source_kind": "claude-cli",
        "actor_id": item["actor_id"],
        "actor_kind": "agent",
        "actor_label": item["actor_label"],
        "task_id": item["task_id"],
        "task_kind": "call",
        "task_label": "Claude subagent",
        "action": "execute",
        "state": item["state"],
    }


def _report(project, session_id, tasks, store, state_dir, project_state, claude_hooks):
    if not tasks:
        return
    cache_path, lock_path = _cache_paths(
        state_dir, project, session_id, project_state,
    )
    with _cache_lock(lock_path, project_state):
        cache = _load_cache(
            cache_path, project, session_id, project_state, claude_hooks,
        )
        entries = cache["entries"]

        unseen = {item["registry_id"]: item for item in tasks
                  if item["registry_id"] not in entries}
        if unseen:
            records = store.list_work_events(project, limit=1000)["records"]
            matched = {
                registry_id for registry_id, item in unseen.items()
                if any(_authoritative(record, item) for record in records)
            }
        else:
            matched = set()

        now = time.time()
        pending = []
        for item in tasks:
            registry_id = item["registry_id"]
            previous = entries.get(registry_id)
            if previous is None and registry_id not in matched:
                continue
            should_emit = previous is None or previous["state"] != item["state"]
            if (not should_emit and item["state"] == "running"
                    and now - previous["last_emitted"] >= RUNNING_REFRESH_SECONDS):
                should_emit = True
            if not should_emit:
                continue
            pending.append(item)

        for offset in range(0, len(pending), 20):
            batch = pending[offset:offset + 20]
            store.record_work_events(
                project, [_event(item) for item in batch], provenance="observed",
            )
            emitted_at = time.time()
            for item in batch:
                entries[item["registry_id"]] = {
                    "actor_id": item["actor_id"],
                    "task_id": item["task_id"],
                    "state": item["state"],
                    "last_emitted": emitted_at,
                }
            _save_cache(cache_path, cache, project_state)


def _parser():
    parser = _QuietParser(add_help=False)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--projects", type=Path,
                        default=Path.home() / ".config/chatgpt-coding-mcp/projects.json")
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/state/chatgpt-coding-mcp")
    parser.add_argument("--hook-registry", action="store_true")
    return parser


def main(argv=None):
    phase = "runtime"
    try:
        args = _parser().parse_args(argv)
    except (SystemExit, ValueError):
        return 0

    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        runtime = _verified_runtime(args.runtime_dir)
        claude_hooks, load_projects, project_state = _runtime_imports(runtime)
        phase = "input"
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return 0
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 0

        phase = "configuration"
        with redirect_stderr(io.StringIO()):
            projects = load_projects(args.projects)
        normalize = _normalize_hook_registry if args.hook_registry else _normalize_payload
        normalized = normalize(payload, projects, claude_hooks)
        if normalized is None:
            return 0
        project, session_id, tasks = normalized
        if not tasks:
            return 0
        store = project_state.ProjectState(projects, args.state_dir)
        phase = "cache"
        try:
            cache_path, lock_path = _cache_paths(
                args.state_dir, project, session_id, project_state,
            )
            with _cache_lock(lock_path, project_state):
                _load_cache(
                    cache_path, project, session_id, project_state, claude_hooks,
                )
        except Exception:
            raise
        phase = "association"
        _report(project, session_id, tasks, store, args.state_dir,
                project_state, claude_hooks)
    except Exception:
        if phase != "input":
            print(f"claude-status {phase} unavailable", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
