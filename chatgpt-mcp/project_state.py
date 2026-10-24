"""Private cross-process state shared by the MCP and local admin server."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


MAX_MEMORY_BYTES = 32 * 1024
MAX_MEMORY_STATE_BYTES = MAX_MEMORY_BYTES * 6 + 8192
MAX_PROGRESS_TITLE_BYTES = 200
MAX_PROGRESS_SUMMARY_BYTES = 4096
MAX_PROGRESS_ITEMS = 20
MAX_PROGRESS_ITEM_BYTES = 1000
MAX_PROGRESS_STATE_BYTES = 300 * 1024
PROGRESS_STATUSES = {"planned", "in_progress", "blocked", "completed"}
MAX_ACTIVITY_BYTES = 1024 * 1024
MAX_ACTIVITY_LIMIT = 1000
ACTIVITY_PHASES = {"started", "completed", "failed", "canceled"}
MAX_WORK_EVENT_BYTES = 1024 * 1024
MAX_WORK_EVENT_LIMIT = 1000
WORK_EVENT_SOURCE_KINDS = {"mcp", "codex-cli", "claude-cli", "chatgpt"}
WORK_EVENT_ACTOR_KINDS = {"mcp", "executor", "agent"}
WORK_EVENT_TASK_KINDS = {"call", "job", "agent"}
WORK_EVENT_ACTIONS = {
    "read", "search", "code", "build", "test", "review", "execute", "commit",
    "push", "reason",
}
WORK_EVENT_STATES = {
    "started", "running", "queued", "canceling", "returned", "succeeded", "failed",
    "timed_out", "canceled", "interrupted", "unknown",
}
ACTIVE_WORK_EVENT_STATES = {"started", "running", "queued", "canceling"}
WORK_EVENT_PROVENANCE = {"observed", "reported"}
WORK_EVENT_ACTIVITY_ROLES = {
    "analysis", "research", "planning", "debugging", "review", "summary",
}
WORK_EVENT_SEARCH_SCOPES = {"web", "files"}
WORK_EVENT_USAGE_SCOPES = {"request", "turn", "session", "task"}
WORK_EVENT_USAGE_FIELDS = {
    "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
}
MAX_SAFE_INTEGER = 9007199254740991
MAX_WORK_EVENT_FUTURE_SECONDS = 300
PROJECTLESS_TOOLS = {
    "list_projects",
    "open_project_picker",
    "read_plugin_settings",
    "update_plugin_settings",
    "check_settings_support",
}
PROJECT_TOOLS = {
    "read_registered_artifact", "list_registered_artifacts",
    "read_workspace_file", "list_workspace_files", "write_workspace_file",
    "open_workspace_access_approval", "confirm_workspace_access_approval",
    "cancel_workspace_access_approval", "publish_workspace_output", "read_workspace_output",
    "select_project",
    "read_project_memory",
    "update_project_memory",
    "read_project_progress",
    "update_project_progress",
    "read_file",
    "list_dir",
    "find_file",
    "search_for_pattern",
    "create_text_file",
    "replace_content",
    "list_execution_profiles",
    "start_command",
    "get_job",
    "read_job_log",
    "cancel_job",
    "list_jobs",
    "git_status",
    "git_diff",
    "prepare_git_operation",
    "execute_git_operation",
    "get_git_operation",
    "get_runtime_status", "compare_deployment", "prepare_test_deploy",
    "execute_test_deploy", "get_host_operation",
    "get_db_migration_status", "get_predecessor_migration_status",
    "get_schema014_migration_status", "get_toss_test_console_status", "prepare_db_migration",
    "prepare_predecessor_migration", "prepare_schema014_migration",
    "execute_db_migration", "execute_predecessor_migration",
    "execute_schema014_migration",
    "prepare_runtime_rollout", "get_runtime_rollout", "execute_runtime_rollout",
    "list_host_targets", "inspect_host_target", "prepare_host_operation",
    "execute_host_operation", "read_host_operation_events",
    "list_agent_profiles", "prepare_agent_task", "start_agent_task",
    "get_agent_task", "cancel_agent_task",
    "list_host_command_profiles", "prepare_host_command",
    "execute_host_command", "get_host_command", "cancel_host_command",
    "request_host_access", "get_host_access", "start_host_execution",
    "get_host_execution", "read_host_execution_log", "cancel_host_execution",
    "open_host_access_approval", "update_host_access_approval",
    "confirm_host_access_approval", "cancel_host_access_approval",
    "open_schema014_approval", "confirm_schema014_approval", "cancel_schema014_approval",
}
KNOWN_TOOLS = PROJECTLESS_TOOLS | PROJECT_TOOLS
CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
ASCII_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:+/-]+\Z")
SKILL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")


def _invalid_work_event():
    raise ValueError("Invalid work event")


def _work_event_time(value):
    if isinstance(value, datetime):
        parsed = value
        rendered = value.isoformat()
    elif isinstance(value, str):
        rendered = value
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError:
            _invalid_work_event()
    else:
        _invalid_work_event()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid_work_event()
    return parsed, rendered


def _work_event_identifier(value, maximum):
    return (isinstance(value, str) and len(value) <= maximum
            and ASCII_IDENTIFIER.fullmatch(value) is not None)


def _work_event_choice(value, choices):
    return isinstance(value, str) and value in choices


def _work_event_label(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _work_event_printable_label(value, maximum):
    return _work_event_label(value, maximum) and all(character.isprintable() for character in value)


def _sanitize_work_event(project, event, provenance, now, *, preserved_receipt=None):
    if not isinstance(project, str) or not isinstance(event, dict):
        _invalid_work_event()
    if not _work_event_choice(provenance, WORK_EVENT_PROVENANCE):
        _invalid_work_event()
    if event.get("schema_version") != 1 or isinstance(event.get("schema_version"), bool):
        _invalid_work_event()
    if "project" in event and event["project"] != project:
        _invalid_work_event()

    for field in ("source_id", "actor_id", "task_id"):
        if not isinstance(event.get(field), str) or CALL_ID.fullmatch(event[field]) is None:
            _invalid_work_event()
    for field in ("parent_task_id", "parent_actor_id", "parent_source_id"):
        if field in event and (not isinstance(event[field], str)
                               or CALL_ID.fullmatch(event[field]) is None):
            _invalid_work_event()
    if not _work_event_choice(event.get("source_kind"), WORK_EVENT_SOURCE_KINDS):
        _invalid_work_event()
    if not _work_event_choice(event.get("actor_kind"), WORK_EVENT_ACTOR_KINDS):
        _invalid_work_event()
    if not _work_event_choice(event.get("task_kind"), WORK_EVENT_TASK_KINDS):
        _invalid_work_event()
    for field, maximum in (("actor_label", 80), ("task_label", 200)):
        if not _work_event_label(event.get(field), maximum):
            _invalid_work_event()
    action = event.get("action")
    if not _work_event_choice(action, WORK_EVENT_ACTIONS):
        _invalid_work_event()
    state = event.get("state")
    if not _work_event_choice(state, WORK_EVENT_STATES):
        _invalid_work_event()
    task_kind = event["task_kind"]
    if ((task_kind == "call" and state == "succeeded")
            or (task_kind in {"job", "agent"} and state == "returned")):
        _invalid_work_event()

    receipt_value = preserved_receipt if preserved_receipt is not None else now
    receipt, receipt_text = _work_event_time(receipt_value)
    if "time" in event:
        event_time, event_time_text = _work_event_time(event["time"])
    else:
        event_time, event_time_text = receipt, receipt_text
    if event_time > receipt + timedelta(seconds=MAX_WORK_EVENT_FUTURE_SECONDS):
        _invalid_work_event()

    exit_code = event.get("exit_code")
    if "exit_code" in event and (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                                 or abs(exit_code) > MAX_SAFE_INTEGER):
        _invalid_work_event()
    if task_kind == "job" and state == "succeeded" and exit_code != 0:
        state = "unknown"

    record = {
        "schema_version": 1,
        "project": project,
        "source_id": event["source_id"],
        "source_kind": event["source_kind"],
        "actor_id": event["actor_id"],
        "actor_kind": event["actor_kind"],
        "actor_label": event["actor_label"],
        "task_id": event["task_id"],
        "task_kind": task_kind,
        "task_label": event["task_label"],
        "action": action,
        "state": state,
        "time": event_time_text,
    }
    for field in ("parent_task_id", "parent_actor_id", "parent_source_id"):
        if field in event:
            record[field] = event[field]
    if "exit_code" in event:
        record["exit_code"] = exit_code

    if "activity_role" in event:
        if not _work_event_choice(event["activity_role"], WORK_EVENT_ACTIVITY_ROLES):
            _invalid_work_event()
        record["activity_role"] = event["activity_role"]
    if "search_scope" in event:
        if (action != "search"
                or not _work_event_choice(event["search_scope"], WORK_EVENT_SEARCH_SCOPES)):
            _invalid_work_event()
        record["search_scope"] = event["search_scope"]
    if "tool_name" in event:
        if not _work_event_identifier(event["tool_name"], 120):
            _invalid_work_event()
        record["tool_name"] = event["tool_name"]
    if "skill_id" in event:
        if (task_kind != "call" or not isinstance(event["skill_id"], str)
                or SKILL_IDENTIFIER.fullmatch(event["skill_id"]) is None):
            _invalid_work_event()
        record["skill_id"] = event["skill_id"]
    if "skill_name" in event:
        if "skill_id" not in record or not _work_event_printable_label(event["skill_name"], 80):
            _invalid_work_event()
        record["skill_name"] = event["skill_name"]

    for field, maximum in (("model", 120), ("reasoning_effort", 32)):
        if field in event:
            value = event[field]
            if not _work_event_identifier(value, maximum):
                _invalid_work_event()
            record[field] = value

    if "model_source" in event:
        if "model" not in record or event["model_source"] not in ("configuration", "event"):
            _invalid_work_event()
        record["model_source"] = event["model_source"]
    if "reasoning_effort_source" in event:
        if ("reasoning_effort" not in record
                or event["reasoning_effort_source"] not in ("configuration", "event")):
            _invalid_work_event()
        record["reasoning_effort_source"] = event["reasoning_effort_source"]

    if "usage" in event:
        usage = event["usage"]
        if (not isinstance(usage, dict)
                or not _work_event_choice(event.get("usage_scope"), WORK_EVENT_USAGE_SCOPES)):
            _invalid_work_event()
        safe_usage = {}
        for field in WORK_EVENT_USAGE_FIELDS:
            if field not in usage:
                continue
            value = usage[field]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 <= value <= MAX_SAFE_INTEGER):
                _invalid_work_event()
            safe_usage[field] = value
        if not safe_usage:
            _invalid_work_event()
        record["usage"] = safe_usage
        record["usage_scope"] = event["usage_scope"]
    elif "usage_scope" in event:
        _invalid_work_event()

    if "usage_estimated" in event:
        if "usage" not in record or not isinstance(event["usage_estimated"], bool):
            _invalid_work_event()
        record["usage_estimated"] = event["usage_estimated"]

    expiry_limit = min(event_time, receipt) + timedelta(seconds=120)
    if "expires_at" in event:
        expires, expires_text = _work_event_time(event["expires_at"])
        if expires > expiry_limit:
            _invalid_work_event()
        record["expires_at"] = expires_text
    elif state in ACTIVE_WORK_EVENT_STATES:
        record["expires_at"] = expiry_limit.isoformat()

    record["provenance"] = provenance
    record["received_at"] = receipt_text
    return record


def sanitize_work_event(project, event, *, provenance="reported", now=None):
    """Return a bounded v1 work event without copying unregistered fields."""
    if now is None:
        now = _now()
    return _sanitize_work_event(project, event, provenance, now)


class MemoryConflict(ValueError):
    """Raised when a memory update is based on a stale revision."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _reject_symlink_ancestors(path):
    path = Path(path).absolute()
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink():
            raise ValueError("State paths cannot contain symbolic links")


def _private_directory(path):
    path = Path(path).absolute()
    _reject_symlink_ancestors(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("State directory must be owned by the current user")
    path.chmod(0o700)
    return path


def _private_file(path):
    info = Path(path).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("State file must be a user-owned regular file with mode 0600")


def _read_private_bytes(path, max_bytes):
    path = Path(path)
    _reject_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("State file must be a user-owned regular file with mode 0600")
        if info.st_size > max_bytes:
            raise ValueError("State file exceeds its size limit")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("State file exceeds its size limit")
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ProjectState:
    """Persist per-project memory and bounded metadata-only activity."""

    def __init__(self, projects: dict[str, Path], state_dir: Path):
        if not projects:
            raise ValueError("At least one allowed project is required")
        self.projects = {name: Path(root).resolve(strict=True) for name, root in projects.items()}
        state_dir = Path(state_dir).absolute()
        _reject_symlink_ancestors(state_dir)
        resolved_state = state_dir.resolve(strict=False)
        if any(resolved_state.is_relative_to(root) for root in self.projects.values()):
            raise ValueError("Shared state must be outside writable projects")
        self.state_dir = _private_directory(state_dir)
        self.memory_dir = _private_directory(self.state_dir / "project-memory")
        self.progress_dir = _private_directory(self.state_dir / "project-progress")
        self.lock_dir = _private_directory(self.state_dir / "project-state-locks")
        self.activity_path = self.state_dir / "activity.jsonl"
        self.previous_activity_path = self.state_dir / "activity.previous.jsonl"
        self.work_event_path = self.state_dir / "work-events.jsonl"
        self.previous_work_event_path = self.state_dir / "work-events.previous.jsonl"

    def _root(self, project):
        if project not in self.projects:
            raise ValueError("An allowed project is required")
        return self.projects[project]

    def _memory_key(self, project):
        root = self._root(project)
        return hashlib.sha256(os.fsencode(root)).hexdigest()

    def _progress_key(self, project):
        return self._memory_key(project)

    @contextmanager
    def _lock(self, name, *, exclusive):
        path = self.lock_dir / (name + ".lock")
        _reject_symlink_ancestors(path)
        common_flags = (os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(path, common_flags | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            descriptor = os.open(path, common_flags)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("State lock must be a user-owned regular file with mode 0600")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    def _memory_path(self, project):
        return self.memory_dir / (self._memory_key(project) + ".json")

    def _read_memory_locked(self, project):
        root = self._root(project)
        path = self._memory_path(project)
        if not path.exists() and not path.is_symlink():
            return {"project": project, "content": "", "revision": 0, "updated_at": None}
        try:
            record = json.loads(_read_private_bytes(
                path, MAX_MEMORY_STATE_BYTES).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid project memory state") from exc
        if (not isinstance(record, dict) or record.get("root") != str(root)
                or not isinstance(record.get("content"), str)
                or isinstance(record.get("revision"), bool)
                or not isinstance(record.get("revision"), int)
                or record["revision"] < 1
                or not isinstance(record.get("updated_at"), str)):
            raise ValueError("Invalid project memory state")
        if len(record["content"].encode("utf-8")) > MAX_MEMORY_BYTES:
            raise ValueError("Invalid project memory state")
        return {
            "project": project,
            "content": record["content"],
            "revision": record["revision"],
            "updated_at": record["updated_at"],
        }

    def read_memory(self, project):
        key = self._memory_key(project)
        with self._lock("memory-" + key, exclusive=False):
            return self._read_memory_locked(project)

    def update_memory(self, project, content, expected_revision):
        if not isinstance(content, str):
            raise ValueError("Memory content must be text")
        if len(content.encode("utf-8")) > MAX_MEMORY_BYTES:
            raise ValueError("Memory content exceeds 32768 UTF-8 bytes")
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 0):
            raise ValueError("expected_revision must be a non-negative integer")
        key = self._memory_key(project)
        with self._lock("memory-" + key, exclusive=True):
            current = self._read_memory_locked(project)
            if current["revision"] != expected_revision:
                raise MemoryConflict("Project memory was changed by another writer")
            record = {
                "root": str(self._root(project)),
                "content": content,
                "revision": expected_revision + 1,
                "updated_at": _now(),
            }
            self._atomic_json(self._memory_path(project), record)
            return {"project": project, **{key: record[key] for key in (
                "content", "revision", "updated_at")}}

    def _progress_path(self, project):
        return self.progress_dir / (self._progress_key(project) + ".json")

    @staticmethod
    def _validate_progress_fields(title, status, summary, completed, next_steps):
        if (not isinstance(title, str) or not 1 <= len(title.encode("utf-8"))
                <= MAX_PROGRESS_TITLE_BYTES):
            raise ValueError("Progress title must be 1-200 UTF-8 bytes")
        if not isinstance(status, str) or status not in PROGRESS_STATUSES:
            raise ValueError("Invalid progress status")
        if (not isinstance(summary, str)
                or len(summary.encode("utf-8")) > MAX_PROGRESS_SUMMARY_BYTES):
            raise ValueError("Progress summary exceeds 4096 UTF-8 bytes")
        for name, items in (("completed", completed), ("next_steps", next_steps)):
            if not isinstance(items, list) or len(items) > MAX_PROGRESS_ITEMS:
                raise ValueError(f"{name} must contain at most 20 strings")
            if any(not isinstance(item, str)
                   or len(item.encode("utf-8")) > MAX_PROGRESS_ITEM_BYTES for item in items):
                raise ValueError(f"Each {name} item must be at most 1000 UTF-8 bytes")

    def _read_progress_locked(self, project):
        root = self._root(project)
        path = self._progress_path(project)
        if not path.exists() and not path.is_symlink():
            return {
                "project": project,
                "title": "",
                "status": "not_recorded",
                "summary": "",
                "completed": [],
                "next_steps": [],
                "revision": 0,
                "updated_at": None,
            }
        try:
            record = json.loads(_read_private_bytes(
                path, MAX_PROGRESS_STATE_BYTES).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid project progress state") from exc
        if (not isinstance(record, dict) or record.get("root") != str(root)
                or isinstance(record.get("revision"), bool)
                or not isinstance(record.get("revision"), int)
                or record["revision"] < 1
                or not isinstance(record.get("updated_at"), str)):
            raise ValueError("Invalid project progress state")
        try:
            self._validate_progress_fields(
                record.get("title"), record.get("status"), record.get("summary"),
                record.get("completed"), record.get("next_steps"),
            )
        except ValueError as exc:
            raise ValueError("Invalid project progress state") from exc
        return {"project": project, **{key: record[key] for key in (
            "title", "status", "summary", "completed", "next_steps", "revision", "updated_at")}}

    def read_progress(self, project):
        key = self._progress_key(project)
        with self._lock("progress-" + key, exclusive=False):
            return self._read_progress_locked(project)

    def update_progress(self, project, title, status, summary, completed, next_steps,
                        expected_revision):
        self._validate_progress_fields(title, status, summary, completed, next_steps)
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 0):
            raise ValueError("expected_revision must be a non-negative integer")
        key = self._progress_key(project)
        with self._lock("progress-" + key, exclusive=True):
            current = self._read_progress_locked(project)
            if current["revision"] != expected_revision:
                raise MemoryConflict("Project progress was changed by another writer")
            record = {
                "root": str(self._root(project)),
                "title": title,
                "status": status,
                "summary": summary,
                "completed": list(completed),
                "next_steps": list(next_steps),
                "revision": expected_revision + 1,
                "updated_at": _now(),
            }
            serialized = json.dumps(
                record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(serialized) > MAX_PROGRESS_STATE_BYTES:
                raise ValueError("Progress state exceeds its serialized size limit")
            self._atomic_json(self._progress_path(project), record)
            return {"project": project, **{field: record[field] for field in (
                "title", "status", "summary", "completed", "next_steps", "revision", "updated_at")}}

    def _atomic_json(self, target, record):
        _reject_symlink_ancestors(target)
        if target.exists() or target.is_symlink():
            _private_file(target)
        temporary = target.parent / ("." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def record_activity(self, project_or_none, tool, phase, call_id):
        if tool not in KNOWN_TOOLS:
            raise ValueError("Unknown tool cannot be recorded")
        if tool in PROJECTLESS_TOOLS:
            if project_or_none is not None:
                raise ValueError("Projectless tool activity cannot name a project")
        else:
            self._root(project_or_none)
        if phase not in ACTIVITY_PHASES:
            raise ValueError("Invalid activity phase")
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise ValueError("Invalid activity call ID")
        event = {"project": project_or_none, "tool": tool, "phase": phase,
                 "call_id": call_id, "time": _now()}
        data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self._lock("activity", exclusive=True):
            if self.activity_path.exists() or self.activity_path.is_symlink():
                current = _read_private_bytes(self.activity_path, MAX_ACTIVITY_BYTES)
                if len(current) + len(data) > MAX_ACTIVITY_BYTES:
                    if self.previous_activity_path.exists() or self.previous_activity_path.is_symlink():
                        _private_file(self.previous_activity_path)
                        self.previous_activity_path.unlink()
                    os.replace(self.activity_path, self.previous_activity_path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            _reject_symlink_ancestors(self.activity_path)
            descriptor = os.open(self.activity_path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError(
                        "Activity state must be a user-owned regular file with mode 0600")
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def list_activity(self, project, limit=100):
        self._root(project)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACTIVITY_LIMIT:
            raise ValueError(f"limit must be 1-{MAX_ACTIVITY_LIMIT}")
        events = []
        with self._lock("activity", exclusive=False):
            for path in (self.previous_activity_path, self.activity_path):
                if not path.exists() and not path.is_symlink():
                    continue
                try:
                    lines = _read_private_bytes(path, MAX_ACTIVITY_BYTES).decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for line in lines:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (isinstance(event, dict) and event.get("project") == project
                            and event.get("tool") in PROJECT_TOOLS
                            and event.get("phase") in ACTIVITY_PHASES
                            and isinstance(event.get("call_id"), str)
                            and CALL_ID.fullmatch(event["call_id"])
                            and isinstance(event.get("time"), str)
                            and set(event) == {"project", "tool", "phase", "call_id", "time"}):
                        events.append(event)
        return {"project": project, "events": list(reversed(events[-limit:]))}

    def record_work_event(self, project, event, *, provenance="reported"):
        self._root(project)
        record = sanitize_work_event(project, event, provenance=provenance)
        self._append_work_event_records([record])
        return record

    def record_work_events(self, project, events, *, provenance="reported"):
        self._root(project)
        if not isinstance(events, list) or not 1 <= len(events) <= 20:
            raise ValueError("Invalid work event batch")
        received_at = _now()
        records = [_sanitize_work_event(project, event, provenance, received_at)
                   for event in events]
        self._append_work_event_records(records)
        return {"project": project, "accepted": len(records), "provenance": provenance}

    def _append_work_event_records(self, records):
        data = b"".join(
            (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            for record in records
        )
        if len(data) > MAX_WORK_EVENT_BYTES:
            raise ValueError("Invalid work event")
        with self._lock("work-events", exclusive=True):
            if self.work_event_path.exists() or self.work_event_path.is_symlink():
                current = _read_private_bytes(self.work_event_path, MAX_WORK_EVENT_BYTES)
                if len(current) + len(data) > MAX_WORK_EVENT_BYTES:
                    if (self.previous_work_event_path.exists()
                            or self.previous_work_event_path.is_symlink()):
                        _private_file(self.previous_work_event_path)
                        self.previous_work_event_path.unlink()
                    os.replace(self.work_event_path, self.previous_work_event_path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            _reject_symlink_ancestors(self.work_event_path)
            descriptor = os.open(self.work_event_path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError(
                        "Work event state must be a user-owned regular file with mode 0600")
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _read_work_event_record(event):
        if not isinstance(event, dict):
            return None
        provenance = event.get("provenance")
        received_at = event.get("received_at")
        if (not _work_event_choice(provenance, WORK_EVENT_PROVENANCE)
                or not isinstance(received_at, str)):
            return None
        try:
            sanitized = _sanitize_work_event(
                event.get("project"), event, provenance, received_at,
                preserved_receipt=received_at,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if sanitized != event:
            return None
        return sanitized

    def list_work_events(self, project, limit=200):
        self._root(project)
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_WORK_EVENT_LIMIT):
            raise ValueError(f"limit must be 1-{MAX_WORK_EVENT_LIMIT}")
        records = []
        sequence = 0
        with self._lock("work-events", exclusive=False):
            for path in (self.previous_work_event_path, self.work_event_path):
                if not path.exists() and not path.is_symlink():
                    continue
                try:
                    lines = _read_private_bytes(
                        path, MAX_WORK_EVENT_BYTES).decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for line in lines:
                    sequence += 1
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    record = self._read_work_event_record(event)
                    if record is not None and record["project"] == project:
                        event_time, _ = _work_event_time(record["time"])
                        records.append((event_time, sequence, record))
        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return {"project": project, "records": [item[2] for item in records[:limit]]}
