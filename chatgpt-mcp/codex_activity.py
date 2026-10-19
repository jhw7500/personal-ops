"""Privacy-bounded activity reader for native Codex JSONL sessions."""

from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat


_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")
_MAX_LINE_BYTES = 64 * 1024
_MAX_READ_BYTES = 256 * 1024
_MAX_EVENTS_PER_READ = 20
_MAX_THREADS = 64
_MAX_PENDING = 128
_MAX_DEDUP = 512
_MAX_FUTURE_SECONDS = 300
_MAX_SAFE_INTEGER = 9007199254740991
_IDENTITY_FIELDS = (
    "schema_version", "project", "source_id", "source_kind", "actor_id",
    "actor_kind", "actor_label", "parent_actor_id", "parent_source_id",
)


def _parsed_time(value):
    if isinstance(value, datetime):
        parsed = value
        rendered = value.isoformat()
    elif isinstance(value, str):
        rendered = value
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except (OverflowError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed, rendered


class _ThreadState:
    def __init__(self, path, info, identity_key, anchor_digest, discarding):
        self.path = path
        self.device = info.st_dev
        self.inode = info.st_ino
        self.identity_key = identity_key
        self.offset = info.st_size
        self.anchor_digest = anchor_digest
        self.discarding = discarding
        self.pending = OrderedDict()
        self.dedup = set()
        self.dedup_order = deque()

    def reset(self, path, info, identity_key, anchor_digest, discarding):
        self.__init__(path, info, identity_key, anchor_digest, discarding)

    def remember(self, key):
        if key in self.dedup:
            return False
        self.dedup.add(key)
        self.dedup_order.append(key)
        while len(self.dedup_order) > _MAX_DEDUP:
            self.dedup.discard(self.dedup_order.popleft())
        return True

    def add_pending(self, call_id, event):
        self.pending[call_id] = event
        self.pending.move_to_end(call_id)
        while len(self.pending) > _MAX_PENDING:
            self.pending.popitem(last=False)


class CodexActivityReader:
    """Tail only authorized native session files without retaining payload text."""

    def __init__(self, codex_home):
        self.codex_home = Path(codex_home).absolute()
        self.sessions_root = self.codex_home / "sessions"
        self._states = OrderedDict()

    def forget_unloaded(self, thread_ids):
        allowed = {value for value in thread_ids if isinstance(value, str)}
        for thread_id in list(self._states):
            if thread_id not in allowed:
                del self._states[thread_id]

    def read(self, thread, base_event, *, now=None):
        context = self._context(thread, base_event)
        current = _parsed_time(now if now is not None else datetime.now(timezone.utc))
        if context is None or current is None:
            return []
        thread_id, path, identity = context
        opened = self._open_session(path, thread_id)
        if opened is None:
            return []
        descriptor, info = opened
        try:
            identity_key = tuple(identity.items())
            anchor_digest = self._anchor_digest(descriptor, info.st_size)
            discarding = not self._ends_with_newline(descriptor, info.st_size)
            state = self._states.get(thread_id)
            if state is None:
                self._install_state(
                    thread_id,
                    _ThreadState(path, info, identity_key, anchor_digest, discarding),
                )
                return []
            self._states.move_to_end(thread_id)
            prior_anchor = self._anchor_digest(descriptor, state.offset)
            if (state.path != path or state.device != info.st_dev or state.inode != info.st_ino
                    or state.identity_key != identity_key or info.st_size < state.offset
                    or prior_anchor != state.anchor_digest):
                state.reset(path, info, identity_key, anchor_digest, discarding)
                return []
            events = self._consume(
                descriptor, info.st_size, state, thread_id, identity, current
            )
            state.anchor_digest = self._anchor_digest(descriptor, state.offset)
        except (OSError, OverflowError, ValueError):
            return []
        finally:
            os.close(descriptor)
        return events

    def _install_state(self, thread_id, state):
        self._states[thread_id] = state
        self._states.move_to_end(thread_id)
        while len(self._states) > _MAX_THREADS:
            self._states.popitem(last=False)

    def _context(self, thread, base_event):
        if not isinstance(thread, dict) or not isinstance(base_event, dict):
            return None
        thread_id = thread.get("id")
        raw_path = thread.get("path")
        if (not isinstance(thread_id, str) or _UUID.fullmatch(thread_id) is None
                or not isinstance(raw_path, str) or not raw_path or "\0" in raw_path):
            return None
        path = Path(raw_path)
        if not path.is_absolute() or base_event.get("actor_id") != thread_id:
            return None
        required = {
            "schema_version": 1, "source_kind": "codex-cli", "actor_kind": "agent",
        }
        if any(base_event.get(key) != value for key, value in required.items()):
            return None
        for key in ("project", "source_id", "actor_label"):
            if not isinstance(base_event.get(key), str) or not base_event[key]:
                return None
        identity = {key: base_event[key] for key in _IDENTITY_FIELDS if key in base_event}
        return thread_id, path, identity

    def _open_session(self, path, thread_id):
        try:
            relative = path.relative_to(self.sessions_root)
        except ValueError:
            return None
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        if re.fullmatch(r"rollout-.+-" + re.escape(thread_id) + r"\.jsonl", path.name) is None:
            return None
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        common_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        parent_descriptor = None
        descriptor = None
        try:
            parent_descriptor = os.open(self.codex_home, common_flags | directory_flag)
            for part in ("sessions", *relative.parts[:-1]):
                next_descriptor = None
                try:
                    next_descriptor = os.open(
                        part, common_flags | directory_flag, dir_fd=parent_descriptor
                    )
                    opened_dir = os.fstat(next_descriptor)
                    if not stat.S_ISDIR(opened_dir.st_mode):
                        raise OSError("Invalid session directory")
                except OSError:
                    if next_descriptor is not None:
                        os.close(next_descriptor)
                    raise
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            descriptor = os.open(relative.name, common_flags, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                return None
            result = descriptor, opened
            descriptor = None
            return result
        except (OSError, RuntimeError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    @staticmethod
    def _anchor_digest(descriptor, offset):
        length = min(64, offset)
        start = offset - length
        data = os.pread(descriptor, length, start)
        if len(data) != length:
            return None
        return hashlib.sha256(data).digest()

    @staticmethod
    def _ends_with_newline(descriptor, size):
        return size == 0 or os.pread(descriptor, 1, size - 1) == b"\n"

    def _consume(self, descriptor, size, state, thread_id, identity, current):
        events = []
        if state.offset >= size:
            return events
        chunk = os.pread(descriptor, min(_MAX_READ_BYTES, size - state.offset), state.offset)
        position = 0
        while position < len(chunk) and len(events) < _MAX_EVENTS_PER_READ:
            newline = chunk.find(b"\n", position)
            if state.discarding:
                if newline < 0:
                    state.offset += len(chunk) - position
                    return events
                consumed = newline + 1 - position
                state.offset += consumed
                position = newline + 1
                state.discarding = False
                continue
            if newline < 0:
                remaining = len(chunk) - position
                if remaining > _MAX_LINE_BYTES:
                    state.offset += remaining
                    state.discarding = True
                break
            line = chunk[position:newline]
            consumed = newline + 1 - position
            state.offset += consumed
            position = newline + 1
            if not line or len(line) > _MAX_LINE_BYTES:
                continue
            try:
                event = self._event(line, state, thread_id, identity, current)
            except (OverflowError, RecursionError, TypeError, ValueError):
                event = None
            if event is not None:
                events.append(event)
        return events

    def _event(self, line, state, thread_id, identity, current):
        try:
            envelope = json.loads(line)
        except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, dict):
            return None
        event_time = _parsed_time(envelope.get("timestamp"))
        if event_time is None or event_time[0] > current[0] + timedelta(seconds=_MAX_FUTURE_SECONDS):
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return None
        if envelope.get("type") == "response_item":
            return self._response_event(payload, event_time[1], state, identity)
        if envelope.get("type") == "event_msg":
            return self._completed_event(payload, event_time[1], state, thread_id, identity)
        return None

    def _response_event(self, payload, time, state, identity):
        kind = payload.get("type")
        if not isinstance(kind, str):
            return None
        if kind in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            tool_name = self._response_tool_name(payload)
            if (not isinstance(call_id, str) or _IDENTIFIER.fullmatch(call_id) is None
                    or tool_name is None or not state.remember(("request", call_id))):
                return None
            task_id = self._task_id("tool", call_id)
            event = self._call_event(identity, task_id, "Codex 도구 호출 요청", tool_name,
                                     "execute", "queued", time)
            state.add_pending(call_id, {
                "task_id": task_id, "task_label": event["task_label"],
                "tool_name": tool_name, "action": "execute",
            })
            return event
        if kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or _IDENTIFIER.fullmatch(call_id) is None:
                return None
            pending = state.pending.pop(call_id, None)
            if pending is None or not state.remember(("output", call_id)):
                return None
            return self._call_event(identity, pending["task_id"], pending["task_label"],
                                    pending["tool_name"], pending["action"], "returned", time)
        return None

    @staticmethod
    def _response_tool_name(payload):
        name = payload.get("name")
        namespace = payload.get("namespace")
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            return None
        if namespace is None:
            return name
        combined = f"{namespace}:{name}"
        if not isinstance(namespace, str) or _TOOL_NAME.fullmatch(combined) is None:
            return None
        return combined

    def _completed_event(self, payload, time, state, thread_id, identity):
        if payload.get("type") != "item_completed" or payload.get("thread_id") != thread_id:
            return None
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if (not isinstance(item_type, str)
                or item_type not in {"CommandExecution", "McpToolCall", "FileChange"}
                or not isinstance(item_id, str) or _IDENTIFIER.fullmatch(item_id) is None):
            return None
        state_name = self._terminal_state(item)
        if state_name is None or not state.remember(("item", item_id)):
            return None
        if item_type == "CommandExecution":
            label, action, tool_name = "Codex 명령 실행 결과", "execute", "CommandExecution"
        elif item_type == "FileChange":
            label, action, tool_name = "Codex 파일 변경 결과", "code", "FileChange"
        else:
            label, action = "Codex MCP 호출 결과", "execute"
            tool_name = self._mcp_tool_name(item)
        event = self._call_event(
            identity, self._task_id("item", item_id), label, tool_name, action, state_name, time
        )
        exit_code = item.get("exit_code")
        if (item_type == "CommandExecution" and isinstance(exit_code, int)
                and not isinstance(exit_code, bool) and abs(exit_code) <= _MAX_SAFE_INTEGER):
            event["exit_code"] = exit_code
        return event

    @staticmethod
    def _task_id(namespace, native_id):
        scoped = f"{namespace}:{native_id}"
        if len(scoped) <= 80:
            return scoped
        return f"{namespace}:{hashlib.sha256(native_id.encode()).hexdigest()}"

    @staticmethod
    def _terminal_state(item):
        status_name = item.get("status")
        if not isinstance(status_name, str):
            return None
        if status_name == "completed":
            exit_code = item.get("exit_code")
            if (item.get("type") == "CommandExecution" and isinstance(exit_code, int)
                    and not isinstance(exit_code, bool) and exit_code != 0):
                return "failed"
            return "returned"
        if status_name == "failed":
            return "failed"
        if status_name in {"interrupted", "canceled", "cancelled"}:
            return "interrupted"
        if status_name == "unknown":
            return "unknown"
        return None

    @staticmethod
    def _mcp_tool_name(item):
        server = item.get("server")
        tool = item.get("tool")
        combined = f"{server}:{tool}"
        if (isinstance(server, str) and isinstance(tool, str)
                and _TOOL_NAME.fullmatch(combined) is not None):
            return combined
        return "McpToolCall"

    @staticmethod
    def _call_event(identity, task_id, label, tool_name, action, state_name, time):
        return {
            **identity,
            "task_id": task_id,
            "task_kind": "call",
            "task_label": label,
            "parent_task_id": "session-activity",
            "action": action,
            "state": state_name,
            "tool_name": tool_name,
            "time": time,
        }
