"""Expiring-grant host command lifecycle with private durable output.

This is trusted host-user automation, not filesystem containment or an
integrity boundary against another process running as the same user.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading

from host_command_runner import MAX_OUTPUT_BYTES, run_host_profile
from host_runtime_config import HostRuntimeError, private_dir, private_file
from host_runtime_operations import (
    _digest, _lock, _read_private_json, _save_private_json, _verified_installation,
    acquire_flock,
)
from workspace_host import acquire_host_operation_lane, complete_host_operation
from workspace_control import WorkspaceControlError, activation_admission


REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
PROFILE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
MODEL = re.compile(r"[A-Za-z0-9_.:+/-]{1,120}\Z")
EFFORT = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
AGENT_REASONS = frozenset({
    "MISSING_TOOL", "WORKFLOW_COMPLEXITY", "EXECUTION_ENV_UNSUPPORTED",
})
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ACTIVE = frozenset({"queued", "running"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})
MAX_HISTORY = 500
MAX_INPUT_BYTES = 32768
MAX_GENERAL_EXECUTABLE = 128 * 1024 * 1024
MAX_CODEX_EXECUTABLE = 512 * 1024 * 1024
WATCH_SECONDS = 0.1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise HostRuntimeError("INVALID_HOST_ACCESS_GRANT") from None


def _safe_result(value):
    names = ("exit_code", "duration_ms", "stdout_bytes", "stderr_bytes")
    if (not isinstance(value, dict) or type(value.get("exit_code")) is not int
            or not -255 <= value["exit_code"] <= 255
            or any(type(value.get(name)) is not int or not 0 <= value[name] <= 10_000_000
                   for name in names[1:])):
        return None
    return {name: value[name] for name in names}


def _known_secret_values() -> set[str]:
    """Known owner credential values only; never log them or return the inventory."""
    from host_runtime_config import _no_parent_links
    config = Path.home() / ".config" / "chatgpt-coding-mcp"
    values = set()
    for path in (config / "admin-token", config / "runtime-key", Path.home() / ".ait" / "credentials"):
        try:
            _no_parent_links(path)
            private_file(path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 65536:
                    continue
                raw = os.read(fd, 65537).decode("utf-8").strip()
            finally:
                os.close(fd)
            if path.name == "credentials":
                parsed = json.loads(raw)
                candidates = parsed.values() if isinstance(parsed, dict) else ()
            else:
                candidates = (raw,)
            values.update(value for value in candidates if isinstance(value, str)
                          and 12 <= len(value) <= 8192 and value.isprintable())
        except (HostRuntimeError, OSError, UnicodeError, ValueError):
            continue
    return values


def _redact(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    for secret in sorted(_known_secret_values(), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    patterns = (
        (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
        (re.compile(r"(?i)\b(token|password|secret|api[_-]?key)\s*([:=])\s*[^\s]+"),
         r"\1\2[REDACTED]"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"), "[REDACTED]"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def _safe_task_label(value, *, error_code="INVALID_HOST_EXECUTION"):
    """Return bounded display metadata without promoting prompts or argv to purpose."""
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 160
            or not value.isprintable() or not value.strip()):
        raise HostRuntimeError(error_code)
    safe = _redact(value.encode("utf-8")).strip()
    if not safe:
        raise HostRuntimeError(error_code)
    return safe[:160]


def _command_work_context(frozen):
    task_label = _safe_task_label(frozen.get("task_label"))
    if frozen.get("kind") == "codex":
        command_label, action = "Codex host task", "code"
    else:
        executable = Path(frozen.get("executable", "")).name.lower()
        argv = frozen.get("argv")
        argv = argv if isinstance(argv, list) else []
        command_label, action = "Host command", "execute"
        families = {
            "bash": "Shell command", "sh": "Shell command",
            "node": "Node command", "npx": "npx command",
            "npm": "npm command", "pnpm": "pnpm command", "yarn": "Yarn command",
            "git": "Git command", "docker": "Docker command",
        }
        if executable.startswith("python") and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            command_label = "Python command"
        elif executable in families:
            command_label = families[executable]
        if executable in {"npm", "pnpm"} and len(argv) >= 2 and argv[0] == "run":
            scripts = {"build": "build", "test": "test", "typecheck": "test",
                       "type-check": "test", "lint": "test", "e2e": "test", "check": "test"}
            if argv[1] in scripts:
                command_label, action = f"{executable} run {argv[1]}", scripts[argv[1]]
        elif executable == "git" and argv[:1] in (["status"], ["diff"]):
            command_label, action = f"git {argv[0]}", "review"
        elif executable == "docker" and argv[:1] == ["build"]:
            command_label, action = "docker build", "build"
        elif executable == "docker" and argv[:2] == ["compose", "build"]:
            command_label, action = "docker compose build", "build"
    return {
        "task_label": task_label,
        "label_source": "reported" if task_label is not None else "unavailable",
        "command_label": command_label,
        "action": action,
    }


class HostExecution:
    def __init__(self, projects, state_dir, *, access, runner=None, trusted_runtime=None):
        self.projects = {name: Path(root).resolve(strict=True)
                         for name, root in projects.items()}
        self.state_root = Path(state_dir).absolute()
        for root in self.projects.values():
            if self.state_root.resolve(strict=False).is_relative_to(root):
                raise HostRuntimeError("UNSAFE_STATE_PATH")
        private_dir(self.state_root)
        self.state_dir = private_dir(self.state_root / "host-executions")
        self.plans_dir = private_dir(self.state_dir / "plans")
        self.logs_dir = private_dir(self.state_dir / "logs")
        self.runs_dir = private_dir(self.state_dir / "runs")
        self.locks_dir = private_dir(self.state_dir / "locks")
        self.owner_locks_dir = private_dir(self.locks_dir / "owners")
        self.lock_path = self.state_dir / "operations.lock"
        self.host_lock_path = self.locks_dir / "host.lock"
        self.access = access
        self.workspace = getattr(access, "workspace", None)
        authority = getattr(self.workspace, "authority", None)
        self.activation_authority_dir = Path(
            getattr(authority, "state_dir", self.state_root / "workspace-authority")
        ).absolute()
        self.runner = runner or run_host_profile
        self._native_runner = runner is None
        self._trusted = (_verified_installation() if trusted_runtime is None
                         else bool(trusted_runtime() if callable(trusted_runtime)
                                   else trusted_runtime))
        self._workers = {}
        self._retained_leases = {}

    def _require_trusted(self):
        if not self._trusted:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _state_path(self, operation_id):
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
            raise HostRuntimeError("INVALID_OPERATION_ID")
        return self.plans_dir / (operation_id + ".json")

    def _load(self, operation_id, project=None):
        try:
            state = _read_private_json(self._state_path(operation_id))
        except (OSError, HostRuntimeError):
            raise HostRuntimeError("HOST_EXECUTION_NOT_FOUND") from None
        if (state.get("operation_id") != operation_id
                or state.get("project") not in self.projects
                or state.get("state") not in ACTIVE | TERMINAL | {"prepared"}
                or project is not None and state.get("project") != project
                or not isinstance(state.get("frozen"), dict)):
            raise HostRuntimeError("INVALID_HOST_EXECUTION")
        return state

    def _save(self, state):
        _save_private_json(self._state_path(state["operation_id"]), state)

    @staticmethod
    def _public(state):
        frozen = state["frozen"]
        result = {
            "project": state["project"], "operation_id": state["operation_id"],
            "kind": frozen["kind"], "state": state["state"], "phase": state["phase"],
            "code": state.get("code"), "created_at": state["created_at"],
            "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
            "result": state.get("result"),
            "termination_verified": state.get("termination_verified"),
            "authorization": {"kind": "host_access", "grant_id": frozen["grant_id"],
                              "expires_at": frozen["grant_expires_at"]},
            "work_context": _command_work_context(frozen),
        }
        if frozen["kind"] == "codex":
            result["agent_metadata"] = dict(frozen["agent_metadata"])
        return result

    def _all(self):
        states = []
        for path in self.plans_dir.glob("*.json"):
            if not OPERATION_ID.fullmatch(path.stem):
                raise HostRuntimeError("INVALID_HOST_EXECUTION")
            states.append(self._load(path.stem))
            if len(states) > MAX_HISTORY:
                raise HostRuntimeError("HOST_EXECUTION_HISTORY_FULL")
        return states

    def _agent_metadata(self, value):
        if value is None:
            return None
        required = {"profile_id", "model", "reasoning_effort", "profile_digest"}
        if (not isinstance(value, dict) or not required.issubset(value)
                or not set(value).issubset(required | {"reason"})):
            raise HostRuntimeError("INVALID_HOST_EXECUTION")
        if (not PROFILE_ID.fullmatch(value.get("profile_id", ""))
                or not MODEL.fullmatch(value.get("model", ""))
                or not EFFORT.fullmatch(value.get("reasoning_effort", ""))
                or not HEX64.fullmatch(value.get("profile_digest", ""))
                or "reason" in value and value["reason"] not in AGENT_REASONS):
            raise HostRuntimeError("INVALID_HOST_EXECUTION")
        return dict(value)

    def _freeze_executable(self, value, *, codex):
        try:
            supplied = Path(value)
            resolved = supplied.resolve(strict=True)
            before = resolved.stat()
            maximum = MAX_CODEX_EXECUTABLE if codex else MAX_GENERAL_EXECUTABLE
            if (not supplied.is_absolute() or not stat.S_ISREG(before.st_mode)
                    or before.st_uid not in (0, os.getuid())
                    or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                    or not before.st_mode & stat.S_IXUSR
                    or not 1 <= before.st_size <= maximum
                    or any(resolved.is_relative_to(root) for root in self.projects.values())):
                raise OSError()
            digest = hashlib.sha256()
            size = 0
            fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError()
                while True:
                    block = os.read(fd, min(1024 * 1024, maximum + 1 - size))
                    if not block:
                        break
                    size += len(block)
                    if size > maximum:
                        raise OSError()
                    digest.update(block)
                after = os.fstat(fd)
                if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_size,
                                            before.st_mtime_ns, before.st_ctime_ns)):
                    raise OSError()
            finally:
                os.close(fd)
            return str(resolved), digest.hexdigest()
        except (OSError, TypeError, ValueError, RuntimeError):
            raise HostRuntimeError("INVALID_HOST_EXECUTION") from None

    def prepare(self, project, *, executable, argv, cwd, request_id,
                timeout_seconds=1800, input_text=None, agent_metadata=None,
                task_label=None):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_admitted(
                    project, executable=executable, argv=argv, cwd=cwd,
                    request_id=request_id, timeout_seconds=timeout_seconds,
                    input_text=input_text, agent_metadata=agent_metadata,
                    task_label=task_label)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_admitted(self, project, *, executable, argv, cwd, request_id,
                          timeout_seconds=1800, input_text=None, agent_metadata=None,
                          task_label=None):
        self._require_trusted()
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        metadata = self._agent_metadata(agent_metadata)
        codex = metadata is not None
        if (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)
                or type(timeout_seconds) is not int
                or not 1 <= timeout_seconds <= (3600 if codex else 1800)
                or not isinstance(argv, list) or len(argv) > 32
                or any(not isinstance(item, str) or not item or len(item) > 1024
                       or not item.isprintable() or "\0" in item for item in argv)
                or input_text is not None and (not isinstance(input_text, str)
                    or len(input_text.encode("utf-8")) > MAX_INPUT_BYTES)):
            raise HostRuntimeError("INVALID_HOST_EXECUTION")
        root = self.projects[project]
        try:
            relative = Path(cwd)
            resolved_cwd = (root / relative).resolve(strict=True)
            if (not isinstance(cwd, str) or not cwd or relative.is_absolute()
                    or ".." in relative.parts or not resolved_cwd.is_dir()
                    or not resolved_cwd.is_relative_to(root)
                    or cwd != "." and any(part in {"", ".", ".git", ".ssh", ".aws",
                                                       ".config", ".local"}
                                              or part.startswith(".env")
                                              for part in relative.parts)):
                raise OSError()
        except (OSError, TypeError, ValueError, RuntimeError):
            raise HostRuntimeError("INVALID_HOST_EXECUTION") from None
        executable, executable_sha256 = self._freeze_executable(executable, codex=codex)
        task_label = _safe_task_label(task_label)
        grant = self.access.require(project, codex=codex)
        if (not isinstance(grant, dict) or not isinstance(grant.get("grant_id"), str)
                or not isinstance(grant.get("expires_at"), str)):
            raise HostRuntimeError("INVALID_HOST_ACCESS_GRANT")
        frozen = {
            "project": project, "project_root": str(root), "kind": "codex" if codex else "command",
            "executable": executable, "executable_sha256": executable_sha256,
            "argv": list(argv), "cwd_relative": cwd, "timeout_seconds": timeout_seconds,
            "input_text": input_text, "agent_metadata": metadata,
            "grant_id": grant["grant_id"], "grant_expires_at": grant["expires_at"],
        }
        if task_label is not None:
            frozen["task_label"] = task_label
        request_digest = _digest(frozen)
        with _lock(self.lock_path):
            for existing in self._all():
                if existing["project"] == project and existing["request_id"] == request_id:
                    if existing["request_digest"] != request_digest:
                        raise HostRuntimeError("REQUEST_ID_CONFLICT")
                    return self._public(existing)
            operation_id = secrets.token_hex(16)
            state = {
                "project": project, "operation_id": operation_id, "request_id": request_id,
                "request_digest": request_digest, "frozen": frozen,
                "created_at": _now().isoformat(), "started_at": None, "finished_at": None,
                "state": "prepared", "phase": "PREPARED", "code": None, "result": None,
                "execution_identity": None, "termination_verified": None,
                "cancel_requested": False,
            }
            self._save(state)
            return self._public(state)

    def _recover(self, state):
        if state["state"] not in ACTIVE or state["operation_id"] in self._workers:
            return state
        leases = []
        try:
            workspace_fd = acquire_host_operation_lane(
                self.workspace, operation_id=state["operation_id"], kind="host-execution",
                recovery=True)
            if workspace_fd is not None:
                leases.append(workspace_fd)
            leases.append(acquire_flock(self._owner_lock_path(state["operation_id"])))
            leases.append(acquire_flock(self.host_lock_path))
            leases.append(acquire_flock(self._project_lock_path(state["project"])))
        except HostRuntimeError as exc:
            for fd in reversed(leases):
                os.close(fd)
            if exc.code in {"TARGET_BUSY", "WORKSPACE_HOST_LANE_BUSY"}:
                return state
            raise
        identity = state.get("execution_identity")
        try:
            verified = identity is None and state["phase"] in {"QUEUED", "PRECHECK"}
            if identity is not None:
                try:
                    from host_command_supervisor import HostCommandSupervisor
                    verified = HostCommandSupervisor().inspect_identity(identity).get(
                        "termination_verified") is True
                except Exception:
                    verified = False
            state.update(state="outcome_unknown", phase="RECOVER",
                         code="HOST_EXECUTION_WORKER_OWNER_LOST",
                         termination_verified=verified, finished_at=_now().isoformat())
            self._save(state)
            if verified:
                complete_host_operation(
                    self.workspace, state["operation_id"], "host-execution",
                    termination_verified=True)
            if not verified:
                self._retained_leases[state["operation_id"]] = tuple(leases)
                leases = []
            return state
        finally:
            for fd in reversed(leases):
                os.close(fd)

    def get(self, project, operation_id):
        self._require_trusted()
        with _lock(self.lock_path):
            return self._public(self._recover(self._load(operation_id, project)))

    def list(self, project):
        self._require_trusted()
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock_path):
            states = [self._public(self._recover(item)) for item in self._all()
                      if item["project"] == project]
        states.sort(key=lambda item: item["created_at"], reverse=True)
        return {"project": project, "executions": states[:100]}

    def owns(self, project, operation_id, kind=None):
        try:
            state = self._load(operation_id, project)
        except HostRuntimeError:
            return False
        return kind is None or state["frozen"]["kind"] == kind

    def _project_lock_path(self, project):
        return self.state_root / "host-runtime" / "commands" / "locks" / (_digest(project) + ".lock")

    def _owner_lock_path(self, operation_id):
        return self.owner_locks_dir / (operation_id + ".lock")

    def _cancel_requested(self, operation_id):
        with _lock(self.lock_path):
            return self._load(operation_id).get("cancel_requested") is True

    def _reject_observed_managed_work(self):
        """Fail closed on durable non-command work observed as startable or active."""
        from host_registered_operations import observed_registered_operation_blocks_host
        from host_runtime_operations import observed_legacy_operation_blocks_host
        roots = (
            (self.state_root / "host-runtime" / "operations",
             observed_legacy_operation_blocks_host),
            (self.state_root / "host-runtime" / "registered" / "operations",
             observed_registered_operation_blocks_host),
        )
        for root, blocks in roots:
            if not root.exists() and not root.is_symlink():
                continue
            info = root.lstat()
            if (root.is_symlink() or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise HostRuntimeError("UNSAFE_STATE_PATH")
            for path in root.glob("*.json"):
                try:
                    blocked = blocks(path)
                except HostRuntimeError:
                    raise HostRuntimeError("HOST_EXECUTION_MANAGED_WORK_BUSY") from None
                if blocked:
                    raise HostRuntimeError("HOST_EXECUTION_MANAGED_WORK_BUSY")

    def _acquire_execution_leases(self, cancel, project, grant_id, codex, operation_id):
        while not cancel.is_set():
            if self._cancel_requested(operation_id):
                cancel.set()
                break
            self.access.require(project, grant_id=grant_id, codex=codex)
            workspace_fd = host_fd = project_fd = None
            try:
                workspace_fd = acquire_host_operation_lane(
                    self.workspace, operation_id=operation_id, kind="host-execution")
                host_fd = acquire_flock(self.host_lock_path)
                self._reject_observed_managed_work()
                project_fd = acquire_flock(self._project_lock_path(project))
                return tuple(fd for fd in (workspace_fd, host_fd, project_fd)
                             if fd is not None)
            except HostRuntimeError as exc:
                if project_fd is not None:
                    os.close(project_fd)
                if host_fd is not None:
                    os.close(host_fd)
                if workspace_fd is not None:
                    complete_host_operation(
                        self.workspace, operation_id, "host-execution",
                        termination_verified=True)
                    os.close(workspace_fd)
                if exc.code not in {"TARGET_BUSY", "WORKSPACE_HOST_LANE_BUSY"}:
                    raise
                cancel.wait(WATCH_SECONDS)
        raise HostRuntimeError("HOST_COMMAND_CANCELLED")

    async def execute(self, project, operation_id):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return await self._execute_admitted(project, operation_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    async def _execute_admitted(self, project, operation_id):
        self._require_trusted()
        with _lock(self.lock_path):
            state = self._recover(self._load(operation_id, project))
            if state["state"] != "prepared":
                return self._public(state)
            owner_fd = acquire_flock(self._owner_lock_path(operation_id))
            cancel = threading.Event()
            worker = threading.Thread(target=self._work, args=(operation_id, cancel, owner_fd),
                                      name="host-execution-" + operation_id[:8], daemon=True)
            state.update(state="queued", phase="QUEUED", started_at=_now().isoformat())
            self._save(state)
            self._workers[operation_id] = (worker, cancel)
            try:
                worker.start()
            except RuntimeError:
                self._workers.pop(operation_id, None)
                os.close(owner_fd)
                state.update(state="failed", phase="COMPLETE", code="EXECUTOR_UNAVAILABLE",
                             finished_at=_now().isoformat(), termination_verified=True)
                self._save(state)
                raise HostRuntimeError("EXECUTOR_UNAVAILABLE") from None
            return self._public(state)

    def _record_identity(self, operation_id, identity):
        if not isinstance(identity, dict) or identity.get("task_id") != operation_id:
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        if self._native_runner:
            from agent_supervisor import IDENTITY_KEYS
            if set(identity) != IDENTITY_KEYS or identity.get("uid") != os.getuid():
                raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        with _lock(self.lock_path):
            state = self._load(operation_id)
            if state["state"] != "running" or state.get("execution_identity") is not None:
                raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            state.update(execution_identity=dict(identity), termination_verified=False)
            self._save(state)

    def _log_path(self, operation_id, stream, *, create=False):
        if stream not in {"stdout", "stderr"}:
            raise HostRuntimeError("INVALID_HOST_EXECUTION_LOG")
        directory = self.logs_dir / operation_id
        if create:
            private_dir(directory)
        path = directory / (stream + ".log")
        if create and not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        return path

    def _append_log(self, operation_id, stream, block):
        if not isinstance(block, bytes):
            raise HostRuntimeError("HOST_COMMAND_OUTPUT_LIMIT")
        path = self._log_path(operation_id, stream, create=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size + len(block) > MAX_OUTPUT_BYTES):
                raise HostRuntimeError("HOST_COMMAND_OUTPUT_LIMIT")
            view = memoryview(block)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise HostRuntimeError("HOST_COMMAND_OUTPUT_LIMIT")
                view = view[written:]
        finally:
            os.close(fd)

    def _work(self, operation_id, cancel, owner_fd):
        leases = None
        termination_verified = True
        effect_started = False
        result = None
        watch_error = []
        watcher = None
        watch_done = threading.Event()
        try:
            with _lock(self.lock_path):
                state = self._load(operation_id)
                frozen = state["frozen"]
                state.update(state="running", phase="PRECHECK")
                self._save(state)
            codex = frozen["kind"] == "codex"
            leases = self._acquire_execution_leases(
                cancel, state["project"], frozen["grant_id"], codex, operation_id)
            grant = self.access.require(
                state["project"], grant_id=frozen["grant_id"], codex=codex)
            remaining = (_timestamp(grant["expires_at"]) - _now()).total_seconds()
            if remaining < 1:
                raise HostRuntimeError("HOST_ACCESS_DENIED")
            frozen = dict(frozen, timeout_seconds=min(frozen["timeout_seconds"], int(remaining)))
            with _lock(self.lock_path):
                current = self._load(operation_id)
                current.update(phase="EXECUTE")
                self._save(current)

            def watch_access():
                while not watch_done.wait(WATCH_SECONDS):
                    try:
                        if self._cancel_requested(operation_id):
                            cancel.set()
                            return
                        self.access.require(state["project"], grant_id=frozen["grant_id"],
                                            codex=codex)
                    except Exception as exc:
                        watch_error.append(getattr(exc, "code", "HOST_ACCESS_DENIED"))
                        cancel.set()
                        return

            watcher = threading.Thread(target=watch_access, daemon=True)
            watcher.start()
            effect_started = True
            termination_verified = False
            options = {
                "execution_dir": self.runs_dir / operation_id,
                "operation_id": operation_id,
                "on_identity": lambda identity: self._record_identity(operation_id, identity),
                "go_guard": lambda: self.access.guard(
                    state["project"], frozen["grant_id"], codex=codex),
                "on_output": lambda stream, block: self._append_log(operation_id, stream, block),
                "input_bytes": (frozen["input_text"].encode("utf-8")
                                if frozen.get("input_text") is not None else None),
                "path_prefix": Path(frozen["executable"]).parent if codex else None,
            }
            result = self.runner(frozen, cancel, **options)
            termination_verified = (isinstance(result, dict)
                                    and result.get("termination_verified") is True)
            safe = _safe_result(result)
            if safe is None:
                raise HostRuntimeError("HOST_COMMAND_RESULT_INVALID")
            if not termination_verified:
                raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            if watch_error:
                raise HostRuntimeError(watch_error[0])
            if cancel.is_set():
                raise HostRuntimeError("HOST_COMMAND_CANCELLED")
            if result["exit_code"] != 0:
                raise HostRuntimeError("HOST_COMMAND_EXIT_NONZERO")
            with _lock(self.lock_path):
                current = self._load(operation_id)
                current.update(state="succeeded", phase="COMPLETE", code=None, result=safe,
                               termination_verified=True, finished_at=_now().isoformat())
                self._save(current)
        except Exception as exc:
            termination_verified = getattr(exc, "termination_verified", termination_verified) is True
            safe = _safe_result(getattr(exc, "result", None)) or _safe_result(result)
            code = watch_error[0] if watch_error else getattr(exc, "code", "HOST_EXECUTION_FAILED")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code):
                code = "HOST_EXECUTION_FAILED"
            with _lock(self.lock_path):
                current = self._load(operation_id)
                current.update(state=("outcome_unknown" if effect_started and not termination_verified
                                      else "cancelled" if cancel.is_set() else "failed"),
                               phase="COMPLETE", code=code, result=safe,
                               termination_verified=termination_verified,
                               finished_at=_now().isoformat())
                self._save(current)
        finally:
            watch_done.set()
            if watcher is not None:
                watcher.join(1)
            if leases is not None:
                if termination_verified:
                    complete_host_operation(
                        self.workspace, operation_id, "host-execution",
                        termination_verified=True)
                    for fd in reversed(leases):
                        os.close(fd)
                else:
                    self._retained_leases[operation_id] = (*leases, owner_fd)
                    owner_fd = None
            if owner_fd is not None:
                os.close(owner_fd)
            self._workers.pop(operation_id, None)

    def raw_log_path(self, project, operation_id, stream="stdout"):
        state = self._load(operation_id, project)
        if state["frozen"]["kind"] != "codex":
            raise HostRuntimeError("HOST_AGENT_LOG_RESTRICTED")
        path = self._log_path(operation_id, stream, create=True)
        private_file(path)
        return path

    def read_log(self, project, operation_id, *, stream="stdout", cursor=0, max_bytes=16384):
        self._require_trusted()
        state = self._load(operation_id, project)
        if state["frozen"]["kind"] == "codex":
            raise HostRuntimeError("HOST_AGENT_LOG_RESTRICTED")
        if (stream not in {"stdout", "stderr"} or type(cursor) is not int or cursor < 0
                or type(max_bytes) is not int or not 1 <= max_bytes <= 65536):
            raise HostRuntimeError("INVALID_HOST_EXECUTION_LOG")
        path = self._log_path(operation_id, stream)
        try:
            if not path.exists() and not path.is_symlink():
                raw = b""
            else:
                private_file(path)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    raw = os.read(fd, MAX_OUTPUT_BYTES + 1)
                finally:
                    os.close(fd)
            if len(raw) > MAX_OUTPUT_BYTES:
                raise HostRuntimeError("INVALID_HOST_EXECUTION_LOG")
        except FileNotFoundError:
            raw = b""
        filtered = _redact(raw).encode("utf-8")
        if cursor > len(filtered):
            raise HostRuntimeError("INVALID_HOST_EXECUTION_LOG")
        end = min(len(filtered), cursor + max_bytes)
        while end > cursor:
            try:
                text = filtered[cursor:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            text = ""
        next_cursor = end
        return {"project": project, "operation_id": operation_id, "stream": stream,
                "next_cursor": next_cursor, "eof": next_cursor >= len(filtered), "text": text}

    def _finish_scope(self, identity):
        if not isinstance(identity, dict):
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        from host_command_supervisor import HostCommandSupervisor
        result = HostCommandSupervisor().finish_recorded(identity)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        if result != {"termination_verified": True}:
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")

    def cancel(self, project, operation_id):
        self._require_trusted()
        with _lock(self.lock_path):
            state = self._recover(self._load(operation_id, project))
            if state["state"] == "prepared":
                state.update(state="cancelled", phase="COMPLETE",
                             code="HOST_COMMAND_CANCELLED", cancel_requested=True,
                             termination_verified=True, finished_at=_now().isoformat())
                self._save(state)
                return self._public(state)
            worker = self._workers.get(operation_id)
            if state["state"] in ACTIVE:
                state["cancel_requested"] = True
                self._save(state)
                if worker is not None:
                    worker[1].set()
                return self._public(state)
            if not (state["state"] == "outcome_unknown"
                    and state.get("termination_verified") is not True):
                return self._public(state)
            leases = self._retained_leases.get(operation_id)
            if leases is None:
                acquired = []
                try:
                    workspace_fd = acquire_host_operation_lane(
                        self.workspace, operation_id=operation_id, kind="host-execution",
                        recovery=True)
                    if workspace_fd is not None:
                        acquired.append(workspace_fd)
                    acquired.append(acquire_flock(self.host_lock_path))
                    acquired.append(acquire_flock(self._project_lock_path(project)))
                except HostRuntimeError as exc:
                    for fd in reversed(acquired):
                        os.close(fd)
                    if exc.code in {"TARGET_BUSY", "WORKSPACE_HOST_LANE_BUSY"}:
                        raise HostRuntimeError("HOST_EXECUTION_PROCESS_NOT_OWNED") from None
                    raise
                leases = tuple(acquired)
                self._retained_leases[operation_id] = leases
            self._finish_scope(state.get("execution_identity"))
            state.update(termination_verified=True, code="HOST_EXECUTION_WORKER_OWNER_LOST",
                         finished_at=_now().isoformat())
            self._save(state)
            complete_host_operation(
                self.workspace, operation_id, "host-execution", termination_verified=True)
            for fd in reversed(leases):
                os.close(fd)
            self._retained_leases.pop(operation_id, None)
            return self._public(state)

    async def close(self):
        for _, cancel in list(self._workers.values()):
            cancel.set()
        for thread, _ in list(self._workers.values()):
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, 5)
        for operation_id in list(self._retained_leases):
            try:
                state = self._load(operation_id)
                await asyncio.to_thread(self.cancel, state["project"], operation_id)
            except Exception:
                continue
        if self._workers or self._retained_leases:
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
