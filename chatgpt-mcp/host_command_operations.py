"""Separate one-use-approval lifecycle for fixed host command profiles.

Profiles are host-owned and exact: the chat supplies only a project/profile_id.
This is deliberately NOT a generic host shell. No CLI host effects are possible
without an immutable installed release, an external owner-only profile config
and a fresh authenticated admin-web receipt tied to exact source fingerprints.

State stores no command output, environment, credentials, arguments from chat,
raw error text or shell transcripts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import os
from pathlib import Path
import re
import secrets
import threading

from host_command_profiles import HostCommandProfiles
from host_command_runner import run_host_profile
from host_runtime_config import HostRuntimeError, private_dir
from host_runtime_operations import (
    _digest, _lock, _read_private_json, _save_private_json,
    _timestamp, _verified_installation, acquire_flock,
    operation_path,
)
from workspace_host import (
    acquire_host_operation_lane, complete_host_operation, resolve_workspace,
)
from workspace_control import WorkspaceControlError, activation_admission

REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PLAN_TTL = 7 * 24 * 60 * 60
HISTORY_LIMIT = 500
PENDING = frozenset({"pending_host_approval", "approved"})
ACTIVE = frozenset({"queued", "running"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "outcome_unknown"})
VALID_STATES = PENDING | ACTIVE | TERMINAL


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source_digest(frozen: dict) -> str:
    """Hash only immutable command identity and verified input checksums."""
    return _digest({
        "project": frozen["project"], "profile_id": frozen["profile_id"],
        "executable_sha256": frozen["executable_sha256"],
        "inputs": frozen["inputs"], "config_digest": frozen["config_digest"],
    })


class HostCommandOperations:
    def __init__(self, projects: dict, *, config_path: Path, state_dir: Path,
                 runner=None, test_only: bool = False, scope_inspector=None,
                 scope_finisher=None, workspace=None):
        self.projects = {name: Path(value) for name, value in projects.items()}
        self.profiles = HostCommandProfiles(self.projects, config_path)
        self.state_dir = Path(state_dir).absolute()
        # A checkout must never define the host command policy or private state.
        for root in self.projects.values():
            if self.state_dir.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
                raise HostRuntimeError("UNSAFE_STATE_PATH")
        private_dir(self.state_dir)
        self.plans_dir = private_dir(self.state_dir / "plans")
        self.receipts_dir = private_dir(self.state_dir / "receipts")
        self.locks_dir = private_dir(self.state_dir / "locks")
        self.executions_dir = private_dir(self.state_dir / "executions")
        self.lock_path = self.state_dir / "operations.lock"
        self.runner = runner or run_host_profile
        self.workspace = resolve_workspace(projects, workspace)
        authority = getattr(self.workspace, "authority", None)
        overall_state = (self.state_dir.parents[1]
                         if self.state_dir.parts[-2:] == ("host-runtime", "commands")
                         else self.state_dir.parent)
        self.activation_authority_dir = Path(
            getattr(authority, "state_dir", overall_state / "workspace-authority")
        ).absolute()
        self._native_runner = runner is None
        self._scope_inspector = scope_inspector
        self._scope_finisher = scope_finisher
        self._retained_leases: dict[str, tuple[int, ...]] = {}
        # Fake-runner injection is permitted only for isolated unit tests.
        self._trusted = _verified_installation() or bool(test_only and runner is not None)
        self._workers: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def _require_trusted(self) -> None:
        if not self._trusted:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _state_path(self, operation_id: str) -> Path:
        return operation_path(self.plans_dir, operation_id)

    def _receipt_path(self, operation_id: str) -> Path:
        return operation_path(self.receipts_dir, operation_id)

    def _load(self, operation_id: str, project: str | None = None) -> dict:
        if not isinstance(operation_id, str) or not HEX32.fullmatch(operation_id):
            raise HostRuntimeError("INVALID_OPERATION_ID")
        try:
            state = _read_private_json(self._state_path(operation_id))
        except (OSError, HostRuntimeError):
            raise HostRuntimeError("HOST_COMMAND_PLAN_NOT_FOUND") from None
        if (state.get("operation_id") != operation_id
                or state.get("state") not in VALID_STATES
                or state.get("project") not in self.projects
                or project is not None and state["project"] != project
                or not isinstance(state.get("frozen"), dict)
                or state.get("plan_digest") != _digest({
                    "operation_id": operation_id, "frozen": state["frozen"],
                    "nonce": state.get("nonce"), "created_at": state.get("created_at"),
                    "expires_at": state.get("expires_at"),
                })):
            raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN")
        return state

    def _save(self, state: dict) -> None:
        _save_private_json(self._state_path(state["operation_id"]), state)

    @staticmethod
    def _public(state: dict) -> dict:
        frozen = state["frozen"]
        return {
            "project": state["project"], "operation_id": state["operation_id"],
            "profile_id": frozen["profile_id"], "label": frozen["label"],
            "effect": frozen["effect"], "state": state["state"],
            "phase": state["phase"], "code": state.get("code"),
            "approval_required": state["state"] == "pending_host_approval",
            "plan_digest": state["plan_digest"],
            "source_digest": state["source_digest"],
            "created_at": state["created_at"], "expires_at": state["expires_at"],
            "started_at": state.get("started_at"), "finished_at": state.get("finished_at"),
            "result": state.get("result"),
            "termination_verified": state.get("termination_verified"),
            "approval_url": "/host-commands?operation_id=" + state["operation_id"]
            if state["state"] == "pending_host_approval" else None,
        }

    def _refresh(self, state: dict) -> dict:
        if state["state"] in PENDING and _timestamp(state["expires_at"]) <= _now():
            state.update(state="expired", phase="APPROVAL", code="PLAN_EXPIRED",
                         finished_at=_now().isoformat())
            self._save(state)
        if state["state"] in ACTIVE or (state["state"] == "outcome_unknown"
                                        and state.get("termination_verified") is not True):
            ident = state["operation_id"]
            if ident in self._workers or ident in self._retained_leases:
                return state
            leases = []
            try:
                workspace_fd = acquire_host_operation_lane(
                    self.workspace, operation_id=ident, kind="host-command", recovery=True)
                if workspace_fd is not None:
                    leases.append(workspace_fd)
                leases.append(acquire_flock(self._project_lock_path(state["project"])))
            except HostRuntimeError as exc:
                for lease in reversed(leases):
                    os.close(lease)
                if exc.code in {"TARGET_BUSY", "WORKSPACE_HOST_LANE_BUSY"}:
                    return state
                raise
            try:
                # EXECUTE is durably saved before any native launch. These two
                # earlier phases, with no identity and no live project owner,
                # therefore prove that no command process was started.
                prelaunch = (state.get("execution_identity") is None
                             and (state["state"], state["phase"]) in {
                                 ("queued", "QUEUED"), ("running", "PRECHECK")})
                verified = prelaunch or self._inspect_scope(state.get("execution_identity"))
                state.setdefault("recovery_from_phase", state["phase"])
                state.update(state="outcome_unknown", phase="RECOVER",
                             code="HOST_COMMAND_WORKER_OWNER_LOST",
                             termination_verified=verified,
                             finished_at=_now().isoformat())
                self._save(state)
                if verified:
                    complete_host_operation(
                        self.workspace, ident, "host-command", termination_verified=True)
                if not verified:
                    self._retained_leases[ident] = tuple(leases)
                    leases = []
            finally:
                for lease in reversed(leases):
                    os.close(lease)
        return state

    def _inspect_scope(self, identity) -> bool:
        if not isinstance(identity, dict):
            return False
        try:
            if self._scope_inspector is None:
                from host_command_supervisor import HostCommandSupervisor
                result = HostCommandSupervisor().inspect_identity(identity)
            else:
                result = self._scope_inspector(identity)
            return isinstance(result, dict) and result.get("termination_verified") is True
        except (HostRuntimeError, OSError, ValueError, TypeError):
            return False

    def _finish_scope(self, identity) -> None:
        if not isinstance(identity, dict):
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        if self._scope_finisher is None:
            from host_command_supervisor import HostCommandSupervisor
            result = HostCommandSupervisor().finish_recorded(identity)
        else:
            result = self._scope_finisher(identity)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        if not isinstance(result, dict) or result.get("termination_verified") is not True:
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")

    @staticmethod
    def _blocked(state):
        return (state["state"] in PENDING | ACTIVE or
                state["state"] == "outcome_unknown"
                and state.get("termination_verified") is not True)

    def _retire_stale_unstarted_approval(self, state):
        if state["state"] != "approved":
            return False
        try:
            self.profiles.verify_frozen(state["frozen"])
        except HostRuntimeError as exc:
            if exc.code not in {
                    "HOST_COMMAND_INPUT_CHANGED", "HOST_COMMAND_EXECUTABLE_CHANGED",
                    "HOST_COMMAND_PLAN_STALE"}:
                raise
            state.update(state="expired", phase="APPROVAL", code=exc.code,
                         finished_at=_now().isoformat())
            self._save(state)
            self._receipt_path(state["operation_id"]).unlink(missing_ok=True)
            return True
        return False

    @staticmethod
    def _numeric_result(value):
        names = ("exit_code", "duration_ms", "stdout_bytes", "stderr_bytes")
        if (not isinstance(value, dict) or type(value.get("exit_code")) is not int
                or not -255 <= value["exit_code"] <= 255
                or any(type(value.get(n)) is not int or not 0 <= value[n] <= 10_000_000
                       for n in names[1:])):
            return None
        return {name: value[name] for name in names}

    def _all_states(self) -> list[dict]:
        states = []
        for path in self.plans_dir.glob("*.json"):
            if not HEX32.fullmatch(path.stem):
                raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN")
            states.append(self._load(path.stem))
            if len(states) > HISTORY_LIMIT:
                raise HostRuntimeError("HOST_COMMAND_HISTORY_FULL")
        return states

    def list_profiles(self, project: str) -> dict:
        self._require_trusted()
        return self.profiles.list_profiles(project)

    def prepare(self, project: str, profile_id: str, request_id: str) -> dict:
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_admitted(project, profile_id, request_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_admitted(self, project: str, profile_id: str, request_id: str) -> dict:
        self._require_trusted()
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise HostRuntimeError("INVALID_HOST_COMMAND_REQUEST")
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock_path):
            states = self._all_states()
            request_digest = _digest({"project": project, "profile_id": profile_id})
            for existing in states:
                if existing["project"] == project and existing["request_id"] == request_id:
                    if existing["request_digest"] != request_digest:
                        raise HostRuntimeError("REQUEST_ID_CONFLICT")
                    return self._public(self._refresh(existing))
            for existing in states:
                if existing["project"] == project:
                    self._retire_stale_unstarted_approval(self._refresh(existing))
            if any(x["project"] == project and self._blocked(self._refresh(x))
                   for x in states):
                raise HostRuntimeError("HOST_COMMAND_PROJECT_BUSY")
            frozen = self.profiles.freeze(project, profile_id)
            ident = secrets.token_hex(16)
            now = _now()
            state = {
                "operation_id": ident, "project": project,
                "request_id": request_id, "request_digest": request_digest,
                "source_digest": _source_digest(frozen),
                "frozen": frozen, "nonce": secrets.token_hex(32),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=PLAN_TTL)).isoformat(),
                "state": "pending_host_approval", "phase": "APPROVAL",
                "code": None, "result": None,
                "started_at": None, "finished_at": None,
                "execution_identity": None, "termination_verified": None,
            }
            state["plan_digest"] = _digest({
                "operation_id": ident, "frozen": frozen, "nonce": state["nonce"],
                "created_at": state["created_at"], "expires_at": state["expires_at"],
            })
            self._save(state)
            return self._public(state)

    def get(self, project: str, operation_id: str) -> dict:
        self._require_trusted()
        with _lock(self.lock_path):
            return self._public(self._refresh(self._load(operation_id, project)))

    def pending(self, project: str) -> dict:
        self._require_trusted()
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock_path):
            states = [self._public(self._refresh(state)) for state in self._all_states()
                      if state["project"] == project]
        return {"project": project,
                "plans": [s for s in states if s["state"] == "pending_host_approval"][:20]}

    def approve_web(self, project: str, *, operation_id: str, plan_digest: str,
                    source_digest: str, confirmation: str) -> dict:
        """Only expose through the existing authenticated, origin-checked admin server."""
        self._require_trusted()
        if (not isinstance(operation_id, str) or not HEX32.fullmatch(operation_id)
                or confirmation != operation_id
                or not isinstance(plan_digest, str) or not HEX64.fullmatch(plan_digest)
                or not isinstance(source_digest, str) or not HEX64.fullmatch(source_digest)):
            raise HostRuntimeError("HOST_COMMAND_APPROVAL_DENIED")
        with _lock(self.lock_path):
            state = self._refresh(self._load(operation_id, project))
            if state["state"] == "expired":
                raise HostRuntimeError("PLAN_EXPIRED")
            if state["state"] != "pending_host_approval":
                raise HostRuntimeError("NOT_PENDING_APPROVAL")
            if state["plan_digest"] != plan_digest or state["source_digest"] != source_digest:
                raise HostRuntimeError("STALE_PLAN")
            self.profiles.verify_frozen(state["frozen"])
            receipt = self._receipt_path(operation_id)
            approved_at = _now().isoformat()
            fd = None
            try:
                fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f:
                    fd = None
                    import json
                    f.write(json.dumps({
                        "operation_id": operation_id, "nonce": state["nonce"],
                        "plan_digest": plan_digest, "approved_at": approved_at,
                    }, sort_keys=True).encode())
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                raise HostRuntimeError("HOST_COMMAND_APPROVAL_UNAVAILABLE") from None
            finally:
                if fd is not None:
                    os.close(fd)
            state.update(state="approved", phase="APPROVAL", code=None,
                         approved_at=approved_at)
            self._save(state)
            return {"operation_id": operation_id, "state": "approved", "approved": True}

    def _receipt(self, state: dict) -> Path:
        path = self._receipt_path(state["operation_id"])
        try:
            data = _read_private_json(path)
        except (OSError, HostRuntimeError):
            raise HostRuntimeError("INVALID_HOST_COMMAND_APPROVAL") from None
        if (set(data) != {"operation_id", "nonce", "plan_digest", "approved_at"}
                or data["operation_id"] != state["operation_id"]
                or data["nonce"] != state["nonce"]
                or data["plan_digest"] != state["plan_digest"]
                or data["approved_at"] != state.get("approved_at")):
            raise HostRuntimeError("INVALID_HOST_COMMAND_APPROVAL")
        return path

    def _project_lock_path(self, project: str) -> Path:
        return self.locks_dir / (_digest(project) + ".lock")

    def _begin(self, project: str, operation_id: str) -> dict:
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._begin_admitted(project, operation_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _begin_admitted(self, project: str, operation_id: str) -> dict:
        with _lock(self.lock_path):
            state = self._refresh(self._load(operation_id, project))
            if state["state"] == "pending_host_approval":
                raise HostRuntimeError("HOST_COMMAND_APPROVAL_REQUIRED")
            if state["state"] != "approved":
                return self._public(state)
        held_fd = workspace_fd = None
        try:
            workspace_fd = acquire_host_operation_lane(
                self.workspace, operation_id=operation_id, kind="host-command")
            with _lock(self.lock_path):
                state = self._refresh(self._load(operation_id, project))
                if state["state"] == "pending_host_approval":
                    raise HostRuntimeError("HOST_COMMAND_APPROVAL_REQUIRED")
                if state["state"] != "approved":
                    return self._public(state)
                receipt = self._receipt(state)
                held_fd = acquire_flock(self._project_lock_path(project))
                self.profiles.verify_frozen(state["frozen"])
                receipt.unlink()
                cancel = threading.Event()
                worker = threading.Thread(
                    target=self._work, args=(operation_id, held_fd, workspace_fd, cancel),
                    name="approved-host-command-" + operation_id[:8], daemon=True)
                state.update(state="queued", phase="QUEUED",
                             started_at=_now().isoformat())
                self._save(state)
                self._workers[operation_id] = (worker, cancel)
                try:
                    worker.start()
                except RuntimeError:
                    self._workers.pop(operation_id, None)
                    state.update(state="failed", phase="COMPLETE",
                                 code="EXECUTOR_UNAVAILABLE", finished_at=_now().isoformat())
                    self._save(state)
                    raise HostRuntimeError("EXECUTOR_UNAVAILABLE") from None
                held_fd = None  # handed over to the worker thread
                workspace_fd = None
                return self._public(state)
        finally:
            if held_fd is not None:
                os.close(held_fd)
            if workspace_fd is not None:
                complete_host_operation(
                    self.workspace, operation_id, "host-command",
                    termination_verified=True)
                os.close(workspace_fd)

    async def execute(self, project: str, operation_id: str) -> dict:
        self._require_trusted()
        return await asyncio.to_thread(self._begin, project, operation_id)

    def _work(self, operation_id: str, held_fd: int, workspace_fd: int | None,
              cancel: threading.Event) -> None:
        effect_started = False
        termination_verified = True
        safe_result = None
        try:
            with _lock(self.lock_path):
                state = self._load(operation_id)
                frozen = state["frozen"]
                if cancel.is_set():
                    state.update(state="cancelled", phase="COMPLETE",
                                 code="HOST_COMMAND_CANCELLED", finished_at=_now().isoformat())
                    self._save(state)
                    return
                state.update(state="running", phase="PRECHECK")
                self._save(state)
            self.profiles.verify_frozen(frozen)
            if cancel.is_set():
                raise HostRuntimeError("HOST_COMMAND_CANCELLED")
            with _lock(self.lock_path):
                state = self._load(operation_id)
                state.update(phase="EXECUTE")
                self._save(state)
            # Conservative: from this point the program may perform external effects.
            effect_started = True
            termination_verified = False
            if self._native_runner:
                result = self.runner(
                    frozen, cancel, execution_dir=self.executions_dir / operation_id,
                    operation_id=operation_id,
                    on_identity=lambda identity: self._record_identity(operation_id, identity))
            else:
                result = self.runner(frozen, cancel)
            termination_verified = (isinstance(result, dict)
                                    and result.get("termination_verified") is True)
            safe_result = self._numeric_result(result)
            if safe_result is None:
                raise HostRuntimeError("HOST_COMMAND_RESULT_INVALID")
            if not termination_verified:
                raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            if cancel.is_set():
                raise HostRuntimeError("HOST_COMMAND_CANCELLED")
            if result["exit_code"] != 0:
                raise HostRuntimeError("HOST_COMMAND_EXIT_NONZERO")
            self.profiles.verify_frozen(frozen)  # postcheck: no unannounced artifact rewrite
            with _lock(self.lock_path):
                state = self._load(operation_id)
                state.update(state="succeeded", phase="COMPLETE", code=None,
                             result=safe_result, finished_at=_now().isoformat())
                state["termination_verified"] = True
                self._save(state)
        except Exception as exc:
            if hasattr(exc, "termination_verified"):
                termination_verified = exc.termination_verified is True
            if safe_result is None:
                safe_result = self._numeric_result(getattr(exc, "result", None))
            code = getattr(exc, "code", None)
            if not isinstance(exc, HostRuntimeError) or not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code):
                code = "HOST_COMMAND_FAILED"
            with _lock(self.lock_path):
                state = self._load(operation_id)
                state.update(state=("outcome_unknown" if not termination_verified or effect_started and
                                     state["frozen"]["effect"] != "read_only"
                                     else "cancelled" if code == "HOST_COMMAND_CANCELLED"
                                     else "failed"),
                             phase="COMPLETE", code=code, finished_at=_now().isoformat(),
                             result=safe_result, termination_verified=termination_verified)
                self._save(state)
        finally:
            leases = tuple(fd for fd in (workspace_fd, held_fd) if fd is not None)
            if termination_verified:
                complete_host_operation(
                    self.workspace, operation_id, "host-command",
                    termination_verified=True)
                for lease in reversed(leases):
                    os.close(lease)
            else:
                self._retained_leases[operation_id] = leases
            self._workers.pop(operation_id, None)

    def _record_identity(self, operation_id, identity):
        from agent_supervisor import IDENTITY_KEYS
        if (not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS
                or identity.get("task_id") != operation_id or identity.get("uid") != os.getuid()):
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
        with _lock(self.lock_path):
            state = self._load(operation_id)
            if state["state"] != "running" or state.get("execution_identity") is not None:
                raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            state.update(execution_identity=dict(identity), termination_verified=False)
            self._save(state)

    def cancel(self, project: str, operation_id: str) -> dict:
        """Request cooperative termination of only this manager's owned child."""
        self._require_trusted()
        with _lock(self.lock_path):
            state = self._refresh(self._load(operation_id, project))
            owner = self._workers.get(operation_id)
            if owner is not None and state["state"] in ACTIVE:
                owner[1].set()
                return self._public(state)
            if state["state"] not in ACTIVE and not (
                    state["state"] == "outcome_unknown"
                    and state.get("termination_verified") is not True):
                return self._public(state)
            leases = self._retained_leases.get(operation_id)
            borrowed = leases is None
            if borrowed:
                acquired = []
                try:
                    workspace_fd = acquire_host_operation_lane(
                        self.workspace, operation_id=operation_id, kind="host-command",
                        recovery=True)
                    if workspace_fd is not None:
                        acquired.append(workspace_fd)
                    acquired.append(acquire_flock(self._project_lock_path(project)))
                except HostRuntimeError as exc:
                    for lease in reversed(acquired):
                        os.close(lease)
                    if exc.code in {"TARGET_BUSY", "WORKSPACE_HOST_LANE_BUSY"}:
                        raise HostRuntimeError("HOST_COMMAND_PROCESS_NOT_OWNED") from None
                    raise
                leases = tuple(acquired)
            try:
                self._finish_scope(state.get("execution_identity"))
                state.update(state="outcome_unknown", phase="COMPLETE",
                             code="HOST_COMMAND_WORKER_OWNER_LOST", termination_verified=True,
                             finished_at=_now().isoformat())
                self._save(state)
                complete_host_operation(
                    self.workspace, operation_id, "host-command", termination_verified=True)
            except Exception:
                if borrowed:
                    for lease in reversed(leases):
                        os.close(lease)
                raise
            for lease in reversed(leases):
                os.close(lease)
            self._retained_leases.pop(operation_id, None)
            return self._public(state)

    async def close(self) -> None:
        for thread, cancel in list(self._workers.values()):
            cancel.set()
        for thread, _ in list(self._workers.values()):
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, 5)
        for operation_id in list(self._retained_leases):
            try:
                state = self._load(operation_id)
                await asyncio.to_thread(self.cancel, state["project"], operation_id)
            except Exception:
                # Retain the failed scope's lease, but still attempt cleanup of
                # the other scopes. The final check reports a bounded error.
                continue
        if self._workers or self._retained_leases:
            raise HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
