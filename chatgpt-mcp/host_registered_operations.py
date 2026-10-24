"""Plan, web approval and one-shot execution for registered (version 3) host targets.

State lives under <host-runtime state>/registered/ with owner-only files. Nothing here
runs a shell or a project script; adapters receive only a frozen source snapshot.

Adapter contract (A3 Compose / B3 PostgreSQL implement it; built by
adapter_factory(target, action, runner)):
- preflight(target, spec, context) -> {"identity": dict, "verifications": [check codes]}:
  read-only, deterministic, JSON-serializable, <= 64 KiB; 1..32 unique codes matching
  [A-Z][A-Z0-9_]{0,63}. Runs at prepare (pinned, bound by plan_digest and shown in the
  plan), at web approval and right before execution; any difference is STALE_PLAN.
- execute(target, immutable, context, emit) -> dict: call context.side_effects_started()
  before the first mutating command. emit(phase, code, fields) records whitelisted
  metadata only (see FIELD_RULES); phases PRECHECK -> BACKUP -> EXECUTE -> VERIFY.
- postcheck(target, immutable, result) -> dict: success only when it returns
  {"verified": True, "verifications": [...every declared code...], ...}. Once execute has
  returned or side effects were declared, anything else is outcome_unknown.
Unexpected (non-HostRuntimeError) adapter or factory exceptions surface only as
HOST_OPERATION_FAILED with a random diagnostic_id.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import urllib.parse

from host_actions import validate_action
from host_compose_actions import ComposeActionAdapter
from host_process import run_registered_command
from host_runtime import git_state, verify_reviewed_high_risk_inputs
from host_runtime_config import (
    TARGET, TRUSTED_APPROVAL_ORIGIN, HostRuntimeError, private_dir, private_file, validate_state_root,
)
import host_runtime_operations
from host_runtime_operations import (
    ID_PATTERN, MAX_OPERATIONS, PLAN_TTL_SECONDS, REQUEST_PATTERN, TERMINAL,
    _digest, _lock, _read_private_json, _save_private_json, _timestamp, _verified_installation,
    acquire_flock, default_state_dir, host_admission, lock_busy, operation_path,
)
from host_source_review import freeze_source, inventory_source
from workspace_host import (
    acquire_host_operation_lane, complete_host_operation, resolve_workspace,
)
from workspace_control import WorkspaceControlError, activation_admission

MODULE_DIR = Path(__file__).resolve().parent
_LOGGER = logging.getLogger(__name__)
_RESERVATION_RELEASE_FAILED = "WORKSPACE_HOST_RESERVATION_RELEASE_FAILED"
EXECUTOR_MODULES = ("host_registered_operations.py", "host_process.py", "host_source_review.py",
                    "host_registry.py", "host_actions.py", "host_runtime.py",
                    "host_runtime_config.py", "host_runtime_operations.py")
ACTIVE = ("pending_host_approval", "approved", "queued", "running")
STATES = frozenset(ACTIVE) | TERMINAL
PHASE_ORDER = {"PRECHECK": 0, "BACKUP": 1, "EXECUTE": 2, "VERIFY": 3}
EVENT_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
ARTIFACT = re.compile(r"[A-Za-z0-9._/-]{1,512}\Z")
MAX_VERIFICATIONS = 32
# Source verification failures that mean "the pinned input changed": the plan is stale.
DRIFT_CODES = frozenset({"SOURCE_CHANGED", "SOURCE_INCOMPLETE", "SOURCE_UNREADABLE",
                         "UNSAFE_BUILD_INPUT", "BUILD_INPUT_TOO_LARGE", "REVIEW_REQUIRED"})
# Error codes that may be stored or returned; anything else becomes HOST_OPERATION_FAILED.
PUBLIC_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")
MAX_DIAGNOSTICS = 200
STATE_VERSION = 2  # 2: immutable gained the required "verifications" key.
STATE_KEYS = frozenset({"version", "kind", "project", "target", "operation_id", "request_id",
                        "request_digest", "state", "phase", "code", "diagnostic_id", "result",
                        "created_at", "approved_at", "started_at", "finished_at", "immutable",
                        "plan_digest"})
IMMUTABLE_KEYS = frozenset({"version", "kind", "operation_id", "project", "target", "action",
                            "parameters", "impact", "verifications", "target_identity",
                            "target_lock", "config_digest", "executor_digest", "source",
                            "preflight", "preflight_digest", "nonce", "created_at",
                            "expires_at"})
SOURCE_KEYS = frozenset({"review_policy", "source_digest", "file_count", "total_bytes",
                         "git_head", "changed_paths", "high_risk_changes", "review_digest"})
MAX_EVENTS = 500
MAX_ADAPTER_EVENTS = 480
DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 200
MAX_EVENT_RESPONSE = 64 * 1024


def observed_registered_operation_blocks_host(path: Path) -> bool:
    """Validate an observed registered active record without mutating foreign state."""
    if not ID_PATTERN.fullmatch(path.stem):
        return False
    state = _read_private_json(path)
    if state.get("state") not in {"approved", "queued", "running"}:
        return False
    immutable = state.get("immutable")
    operation_id = path.stem
    if (set(state) != STATE_KEYS or state.get("version") != STATE_VERSION
            or not isinstance(immutable, dict) or set(immutable) != IMMUTABLE_KEYS
            or immutable.get("version") != STATE_VERSION
            or not isinstance(immutable.get("source"), dict)
            or set(immutable["source"]) != SOURCE_KEYS
            or state.get("kind") != "registered" or state.get("operation_id") != operation_id
            or state.get("plan_digest") != _digest(immutable)
            or immutable.get("operation_id") != operation_id
            or immutable.get("project") != state.get("project")
            or immutable.get("target") != state.get("target")):
        raise HostRuntimeError("PLAN_INVALID")
    expires_at = _timestamp(immutable.get("expires_at"))
    return state["state"] in {"queued", "running"} or expires_at > _now()
MAX_PREFLIGHT_BYTES = 64 * 1024
MAX_PUBLIC_PATHS = 200
MAX_PENDING = 20
TERMINAL_EVENT = {"succeeded": "OPERATION_SUCCEEDED", "failed": "OPERATION_FAILED",
                  "rejected": "OPERATION_REJECTED", "outcome_unknown": "OUTCOME_UNKNOWN"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _count(value) -> bool:
    return type(value) is int and 0 <= value <= 10 ** 15


def _flag(value) -> bool:
    return type(value) is bool


def _matches(pattern):
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def _artifact(value) -> bool:
    """Repository-relative path in a strict charset: no spaces, quotes, '=', ';', ':' or
    absolute/parent/secret components, so data or SQL fragments cannot enter the log."""
    return (isinstance(value, str) and ARTIFACT.fullmatch(value) is not None
            and not any(part in ("", ".", "..", ".git", ".env") or part.startswith(".env.")
                        for part in value.split("/")))


FIELD_RULES = {
    **{name: _count for name in ("count", "total", "completed", "rows", "affected_rows",
                                 "statements", "files", "bytes", "duration_ms")},
    **{name: _flag for name in ("verified", "health_verified", "image_verified",
                                "identity_verified", "backup_verified", "restore_verified")},
    "returncode": lambda value: type(value) is int and -255 <= value <= 255,
    "sqlstate": _matches(SQLSTATE),
    "artifact": _artifact,
    "image_id": _matches(IMAGE_ID),
    "sha256": _matches(HEX64),
    "backup_id": _matches(HEX32),
    "diagnostic_id": _matches(HEX32),
    "error_code": _matches(EVENT_CODE),
    "check": _matches(EVENT_CODE),
}


def _clean_fields(fields, services) -> dict:
    """Keep only whitelisted scalar metadata; registered service names only. Strip the rest."""
    if not isinstance(fields, dict):
        return {}
    clean = {}
    for key, value in fields.items():
        if key == "service":
            valid = isinstance(value, str) and value in services
        elif key == "services":
            valid = (isinstance(value, list) and 0 < len(value) <= 32
                     and all(isinstance(name, str) and name in services for name in value)
                     and len(set(value)) == len(value))
        else:
            rule = FIELD_RULES.get(key) if isinstance(key, str) else None
            valid = rule is not None and rule(value)
        if valid:
            clean[key] = list(value) if key == "services" else value
    return clean


def _valid_code(code) -> bool:
    return isinstance(code, str) and PUBLIC_CODE.fullmatch(code) is not None


def _type_name(error) -> str:
    """Exception class only (never its message or arguments) for private diagnostics."""
    name = type(error).__module__ + "." + type(error).__qualname__
    return re.sub(r"[^A-Za-z0-9_.<>]", "_", name)[:200]


def _no_raw_os_errors(method):
    """Public boundary: a residual OSError (which carries a path) never reaches a caller."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OSError:
            raise HostRuntimeError("STATE_UNAVAILABLE") from None
    return wrapper


def _default_adapter_factory(target, action, runner):
    # By prefix: DB actions added to host_actions.ACTIONS must never reach Compose.
    if isinstance(action, str) and action.startswith("compose."):
        return ComposeActionAdapter(runner)
    raise HostRuntimeError("ADAPTER_UNAVAILABLE")


class OperationContext:
    """Adapter view of one operation. Never contains the approval nonce."""

    def __init__(self, operation_id: str, snapshot: Path, *, work_dir: Path | None = None,
                 read_only: bool = True):
        self.operation_id = operation_id
        self.snapshot = snapshot      # frozen, owner-only source copy (read it, never run it)
        self.work_dir = work_dir      # private scratch dir for execution artifacts (None in preflight)
        self.read_only = read_only
        self._side_effects = False

    def side_effects_started(self) -> None:
        if self.read_only:
            raise HostRuntimeError("PREFLIGHT_MUTATION")
        self._side_effects = True

    @property
    def side_effects(self) -> bool:
        return self._side_effects


class _Emitter:
    def __init__(self, manager, operation_id: str, services, context: OperationContext):
        self.manager = manager
        self.operation_id = operation_id
        self.services = frozenset(services)
        self.context = context
        self.phase = "PRECHECK"
        self.count = 0

    def __call__(self, phase: str, code: str, fields: dict | None = None) -> None:
        if (not isinstance(phase, str) or phase not in PHASE_ORDER
                or PHASE_ORDER[phase] < PHASE_ORDER[self.phase]
                or not isinstance(code, str) or not EVENT_CODE.fullmatch(code)):
            raise HostRuntimeError("INVALID_EVENT", self.phase)
        if phase == "EXECUTE":
            self.context._side_effects = True  # Conservative: EXECUTE may already have mutated.
        self.phase = phase
        self.count += 1
        if self.count > MAX_ADAPTER_EVENTS + 1:
            self.manager._record(self.operation_id, phase=phase)
            return
        event = ((phase, "EVENTS_LIMIT_REACHED", {}) if self.count > MAX_ADAPTER_EVENTS
                 else (phase, code, _clean_fields(fields, self.services)))
        self.manager._record(self.operation_id, event, phase=phase)


class RegisteredHostOperations:
    def __init__(self, projects, registry, *, state_dir=None, adapter_factory=None, runner=None,
                 workspace=None):
        self.projects = projects
        self.registry = registry
        self.state_dir = validate_state_root(state_dir or default_state_dir(), projects)
        self.registered_dir = private_dir(self.state_dir / "registered")
        self.operations_dir = private_dir(self.registered_dir / "operations")
        self.receipts_dir = private_dir(self.registered_dir / "receipts")
        self.events_dir = private_dir(self.registered_dir / "events")
        self.snapshots_dir = private_dir(self.registered_dir / "snapshots")
        self.locks_dir = private_dir(self.registered_dir / "locks")
        self.lock_path = self.registered_dir / "operations.lock"
        self.diagnostics_path = self.registered_dir / "diagnostics.json"
        # Separate from operations.lock: diagnostics are written while that lock is held.
        self.diagnostics_lock_path = self.registered_dir / "diagnostics.lock"
        self.legacy_target_lock_path = self.state_dir / "target.lock"
        self.adapter_factory = adapter_factory or _default_adapter_factory
        self.runner = runner or run_registered_command
        self.workspace = resolve_workspace(projects, workspace)
        authority = getattr(self.workspace, "authority", None)
        self.activation_authority_dir = Path(
            getattr(authority, "state_dir", self.state_dir.parent / "workspace-authority")
        ).absolute()
        self.executor_files = [MODULE_DIR / name for name in EXECUTOR_MODULES]
        # Tests may inject BOTH an adapter factory and a fake runner. Either one alone, the
        # real runner passed explicitly, or a helper script inside a project checkout never
        # bypasses the installed-runtime check.
        installed = (_verified_installation() and
                     Path(host_runtime_operations.__file__).resolve().parent == MODULE_DIR)
        injected = (adapter_factory is not None and runner is not None
                    and runner is not run_registered_command)
        self._trusted_runtime = installed or injected
        self._workers = {}

    # ----- helpers -------------------------------------------------------------------
    def _require_trusted_runtime(self):
        if not self._trusted_runtime:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _project_root(self, project) -> Path:
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        try:
            root = Path(self.projects[project]).resolve(strict=True)
        except OSError as exc:
            raise HostRuntimeError("UNSUPPORTED_SCOPE") from exc
        if not root.is_dir():
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return root

    def _path(self, operation_id) -> Path:
        return operation_path(self.operations_dir, operation_id)

    def _receipt_path(self, operation_id) -> Path:
        return self.receipts_dir / (operation_id + ".json")

    def _events_path(self, operation_id) -> Path:
        return self.events_dir / (operation_id + ".json")

    def _snapshot_dir(self, operation_id) -> Path:
        return self.snapshots_dir / operation_id

    @staticmethod
    def _lock_identity(target_spec) -> str:
        name = target_spec["compose"]["project_name"]
        if target_spec["id"] == TARGET or name == TARGET:
            return "legacy"
        return hashlib.sha256(name.encode()).hexdigest()

    def _lock_path_for(self, identity: str) -> Path:
        if identity == "legacy":
            return self.legacy_target_lock_path
        if not HEX64.fullmatch(identity):
            raise HostRuntimeError("PLAN_INVALID")
        return self.locks_dir / (identity + ".lock")

    def _record_diagnostic(self, diagnostic_id, error, stage, operation_id=None) -> None:
        """Owner-only correlation record: id, time, stage, operation and exception class."""
        entry = {"diagnostic_id": diagnostic_id, "at": _now().isoformat(), "stage": stage,
                 "operation_id": operation_id, "exception": _type_name(error)}
        with contextlib.suppress(OSError, HostRuntimeError):  # Never mask the original error.
            with _lock(self.diagnostics_lock_path):
                entries = []
                if self.diagnostics_path.exists() or self.diagnostics_path.is_symlink():
                    stored = _read_private_json(self.diagnostics_path).get("entries")
                    entries = stored if isinstance(stored, list) else []
                _save_private_json(self.diagnostics_path,
                                   {"entries": (entries + [entry])[-MAX_DIAGNOSTICS:]})

    def _failure(self, error, stage, operation_id=None) -> HostRuntimeError:
        failure = HostRuntimeError("HOST_OPERATION_FAILED")
        failure.diagnostic_id = secrets.token_hex(16)
        self._record_diagnostic(failure.diagnostic_id, error, stage, operation_id)
        return failure

    def _adapter_call(self, stage, operation_id, function, *args):
        """Adapter/factory errors surface only as a valid code, or HOST_OPERATION_FAILED
        with a diagnostic_id recorded privately (never the message)."""
        try:
            return function(*args)
        except HostRuntimeError as exc:
            if _valid_code(exc.code):
                raise
            raise self._failure(exc, stage, operation_id) from None
        except Exception as exc:
            raise self._failure(exc, stage, operation_id) from None

    def _adapter(self, target_spec, action, stage, operation_id=None):
        return self._adapter_call(stage, operation_id, self.adapter_factory,
                                  copy.deepcopy(target_spec), action, self.runner)

    def _executor_digest(self, adapter) -> str:
        files = {}
        try:
            for path in self.executor_files:
                files[str(path)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            module = sys.modules.get(type(adapter).__module__)
            source = getattr(module, "__file__", None)
            files["adapter"] = {
                "class": type(adapter).__module__ + "." + type(adapter).__qualname__,
                "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None,
            }
        except OSError as exc:
            raise HostRuntimeError("UNSAFE_RUNTIME") from exc
        return _digest(files)

    @staticmethod
    def _preflight_result(value):
        """Validate {"identity": dict, "verifications": [codes]} -> (identity, codes, digest)."""
        try:
            data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            raise HostRuntimeError("ADAPTER_RESPONSE_INVALID") from None
        checks = value.get("verifications") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {"identity", "verifications"}
                or not isinstance(value["identity"], dict) or not isinstance(checks, list)
                or not 0 < len(checks) <= MAX_VERIFICATIONS
                or not all(isinstance(code, str) and EVENT_CODE.fullmatch(code) for code in checks)
                or len(set(checks)) != len(checks)
                or len(data.encode()) > MAX_PREFLIGHT_BYTES):
            raise HostRuntimeError("ADAPTER_RESPONSE_INVALID")
        return copy.deepcopy(value["identity"]), list(checks), _digest(value)

    @staticmethod
    def _spec(immutable) -> dict:
        return copy.deepcopy({key: immutable[key] for key in ("action", "parameters", "impact")})

    @staticmethod
    def _adapter_view(immutable) -> dict:
        return copy.deepcopy({key: value for key, value in immutable.items() if key != "nonce"})

    # ----- state ---------------------------------------------------------------------
    def _read_state(self, operation_id) -> dict:
        path = self._path(operation_id)
        if not path.exists() and not path.is_symlink():
            raise HostRuntimeError("OPERATION_NOT_FOUND")
        state = _read_private_json(path)
        immutable = state.get("immutable")
        if (set(state) != STATE_KEYS or state["version"] != STATE_VERSION
                or not isinstance(immutable, dict) or set(immutable) != IMMUTABLE_KEYS
                or immutable["version"] != STATE_VERSION
                or not isinstance(immutable["source"], dict)
                or set(immutable["source"]) != SOURCE_KEYS
                or state.get("kind") != "registered" or state.get("operation_id") != operation_id
                or state.get("plan_digest") != _digest(immutable)
                or immutable.get("operation_id") != operation_id
                or immutable.get("project") != state.get("project")
                or immutable.get("target") != state.get("target")
                or state.get("state") not in STATES):
            raise HostRuntimeError("PLAN_INVALID")
        return state

    def _load(self, project, target, operation_id) -> dict:
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        state = self._read_state(operation_id)
        if state["project"] != project or state["target"] != target:
            raise HostRuntimeError("OPERATION_NOT_FOUND")
        return state

    def _states(self):
        for path in sorted(self.operations_dir.glob("*.json")):
            yield self._read_state(path.stem)

    def _save(self, state) -> None:
        _save_private_json(self._path(state["operation_id"]), state)

    def _read_events(self, operation_id) -> list:
        path = self._events_path(operation_id)
        if not path.exists() and not path.is_symlink():
            return []
        log = _read_private_json(path)
        events = log.get("events")
        if (log.get("operation_id") != operation_id or not isinstance(events, list)
                or any(not isinstance(event, dict) or event.get("cursor") != index + 1
                       or set(event) != {"cursor", "at", "phase", "code", "fields"}
                       for index, event in enumerate(events))):
            raise HostRuntimeError("PLAN_INVALID")
        return events

    def _append_event(self, operation_id, phase, code, fields=None) -> None:
        """Caller holds the operations lock. Cursors only grow; events are never rewritten."""
        events = self._read_events(operation_id)
        if len(events) >= MAX_EVENTS:
            return
        events.append({"cursor": len(events) + 1, "at": _now().isoformat(), "phase": phase,
                       "code": code, "fields": dict(fields or {})})
        _save_private_json(self._events_path(operation_id),
                           {"operation_id": operation_id, "events": events})

    def _transition(self, record, event, **updates) -> dict:
        """Caller holds the operations lock. The event is durable before the new state."""
        self._append_event(record["operation_id"], *event)
        record.update(updates)
        self._save(record)
        return record

    def _record(self, operation_id, *events, **updates) -> dict:
        with _lock(self.lock_path):
            state = self._read_state(operation_id)
            for event in events:
                self._append_event(operation_id, *event)
            if updates:
                state.update(updates)
                self._save(state)
            return state

    def _ours(self, operation_id) -> bool:
        worker = self._workers.get(operation_id)
        return worker is not None and worker.is_alive()

    def _refresh(self, state) -> dict:
        """Caller holds the operations lock: expire stale plans, recover dead executions."""
        status = state["state"]
        now = _now()
        if status in ("pending_host_approval", "approved") and \
                _timestamp(state["immutable"]["expires_at"]) <= now:
            self._receipt_path(state["operation_id"]).unlink(missing_ok=True)
            self._transition(state, ("APPROVAL", "PLAN_EXPIRED", {}), state="expired",
                             phase="APPROVAL", code="PLAN_EXPIRED", finished_at=now.isoformat())
        elif (status in ("queued", "running") and not self._ours(state["operation_id"])
              and not lock_busy(self._lock_path_for(state["immutable"]["target_lock"]))):
            # Never re-run: no live owner holds the target lock, so the effect is unknown.
            self._transition(state, ("RECOVER", "PROCESS_INTERRUPTED", {}),
                             state="outcome_unknown", phase="RECOVER",
                             code="PROCESS_INTERRUPTED", finished_at=now.isoformat())
        return state

    def _public(self, state) -> dict:
        immutable = state["immutable"]
        source = immutable["source"]
        payload = {
            "kind": "registered",
            "project": state["project"], "target": state["target"],
            "operation_id": state["operation_id"], "request_id": state["request_id"],
            "state": state["state"], "phase": state["phase"], "code": state["code"],
            "diagnostic_id": state.get("diagnostic_id"),
            "action": immutable["action"], "parameters": immutable["parameters"],
            "impact": immutable["impact"], "verifications": immutable["verifications"],
            "target_identity": immutable["target_identity"],
            "source": {
                "review_policy": source["review_policy"],
                "source_digest": source["source_digest"],
                "git_head": source["git_head"],
                "file_count": source["file_count"],
                "changed_paths": source["changed_paths"][:MAX_PUBLIC_PATHS],
                "changed_path_count": len(source["changed_paths"]),
                "high_risk_changes": source["high_risk_changes"],
                "review_digest": source["review_digest"],
            },
            "source_digest": source["source_digest"],
            "config_digest": immutable["config_digest"],
            "executor_digest": immutable["executor_digest"],
            "plan_digest": state["plan_digest"],
            "created_at": state["created_at"], "expires_at": immutable["expires_at"],
            "approved_at": state["approved_at"], "started_at": state["started_at"],
            "finished_at": state["finished_at"],
            "result": state["result"],
        }
        if state["state"] == "pending_host_approval":
            # Only project, target and operation ID: never a token, nonce or secret.
            payload["approval_url"] = TRUSTED_APPROVAL_ORIGIN + "/host-operations?" + urllib.parse.urlencode({
                "project": state["project"], "target": state["target"],
                "operation_id": state["operation_id"]})
        return copy.deepcopy(payload)

    # ----- verification --------------------------------------------------------------
    def _verify_source(self, state, target_spec) -> None:
        """Only genuine drift (changed live source, tampered pinned snapshot or review,
        moved HEAD, unreviewed high-risk change, inputs that can no longer be read) is
        STALE_PLAN. Host-side failures (Git errors, output limits, unsafe state files)
        keep their own code."""
        source = state["immutable"]["source"]
        inputs = target_spec["source_inputs"]
        root = self._project_root(state["project"])
        snapshot_dir = self._snapshot_dir(state["operation_id"])

        def checked(function, *args, **kwargs):
            try:
                return function(*args, **kwargs)
            except HostRuntimeError as exc:
                if exc.code in DRIFT_CODES:
                    raise HostRuntimeError("STALE_PLAN") from None
                raise

        try:
            snapshot = snapshot_dir / "source"
            if snapshot.is_symlink() or not snapshot.is_dir():
                raise HostRuntimeError("STALE_PLAN")
            pinned = checked(inventory_source, snapshot, inputs)["digest"]
            current = checked(inventory_source, root, inputs)["digest"]
            if pinned != source["source_digest"] or current != source["source_digest"]:
                raise HostRuntimeError("STALE_PLAN")
            if git_state(root)["head"] != source["git_head"]:
                raise HostRuntimeError("STALE_PLAN")
            if source["review_policy"] == "git_head":
                checked(verify_reviewed_high_risk_inputs, root,
                        paths=target_spec["high_risk_inputs"])
            if source["review_digest"] is not None:
                review = snapshot_dir / "review.json"
                if not review.exists() and not review.is_symlink():
                    raise HostRuntimeError("STALE_PLAN")
                private_file(review)
                if hashlib.sha256(review.read_bytes()).hexdigest() != source["review_digest"]:
                    raise HostRuntimeError("STALE_PLAN")
        except OSError:
            # The approved inputs can no longer be read, so they cannot be proven unchanged.
            raise HostRuntimeError("STALE_PLAN") from None

    def _verify(self, state):
        """A freshly read registry, the executor, the source (live + pinned snapshot) and
        the adapter's read-only identity must all equal the pinned plan."""
        immutable = state["immutable"]
        registry = self.registry.reload()
        if registry.config_digest() != immutable["config_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        target_spec = registry.require_target(state["project"], state["target"])
        adapter = self._adapter(target_spec, immutable["action"], "verify",
                                state["operation_id"])
        if self._executor_digest(adapter) != immutable["executor_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        self._verify_source(state, target_spec)
        context = OperationContext(state["operation_id"],
                                   self._snapshot_dir(state["operation_id"]) / "source")
        observed = self._adapter_call("verify", state["operation_id"], adapter.preflight,
                                      copy.deepcopy(target_spec), self._spec(immutable), context)
        if self._preflight_result(observed)[2] != immutable["preflight_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        return target_spec, adapter

    def _verify_or_reject(self, state):
        """Caller holds the operations lock. Drift permanently rejects the plan."""
        try:
            return self._verify(state)
        except HostRuntimeError as exc:
            if exc.code == "STALE_PLAN":
                self._receipt_path(state["operation_id"]).unlink(missing_ok=True)
                self._transition(state, ("APPROVAL", "STALE_PLAN", {}), state="rejected",
                                 phase="APPROVAL", code="STALE_PLAN",
                                 finished_at=_now().isoformat())
            raise

    # ----- public API ----------------------------------------------------------------
    @_no_raw_os_errors
    def prepare(self, project, target, *, action, parameters, request_id) -> dict:
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_admitted(
                    project, target, action=action, parameters=parameters,
                    request_id=request_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_admitted(self, project, target, *, action, parameters, request_id) -> dict:
        self._require_trusted_runtime()
        if not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        root = self._project_root(project)
        registry = self.registry.reload()
        target_spec = registry.require_target(project, target)
        validated = validate_action(target_spec, action, parameters)
        request_digest = _digest({"target": target, "action": validated["action"],
                                  "parameters": validated["parameters"]})
        with _lock(self.lock_path):
            states = list(self._states())
            for existing in states:
                if existing["project"] == project and existing["request_id"] == request_id:
                    if existing["request_digest"] != request_digest:
                        raise HostRuntimeError("REQUEST_ID_CONFLICT")
                    return self._public(self._refresh(existing))
            if len(states) >= MAX_OPERATIONS:
                raise HostRuntimeError("HISTORY_CAPACITY_REACHED")
            identity = self._lock_identity(target_spec)
            for existing in states:
                if (existing["immutable"]["target_lock"] == identity
                        and existing["state"] in ACTIVE
                        and self._refresh(existing)["state"] in ACTIVE):
                    raise HostRuntimeError("TARGET_BUSY")
            # Probe-and-hold the target lock: no legacy or registered execution may run
            # while the identity is pinned. Prepare itself never mutates the target.
            target_fd = acquire_flock(self._lock_path_for(identity))
            try:
                return self._create_plan(project, target, root, registry, target_spec, validated,
                                         request_id, request_digest, identity)
            finally:
                os.close(target_fd)

    def _create_plan(self, project, target, root, registry, target_spec, validated, request_id,
                     request_digest, identity) -> dict:
        operation_id = secrets.token_hex(16)
        snapshot_dir = self._snapshot_dir(operation_id)
        try:
            frozen = freeze_source(root, snapshot_dir, review_policy=target_spec["source_review"],
                                   inputs=target_spec["source_inputs"],
                                   high_risk_inputs=target_spec["high_risk_inputs"])
            adapter = self._adapter(target_spec, validated["action"], "prepare")
            executor_digest = self._executor_digest(adapter)
            preflight, verifications, preflight_digest = self._preflight_result(self._adapter_call(
                "prepare", None, adapter.preflight, copy.deepcopy(target_spec), copy.deepcopy(validated),
                OperationContext(operation_id, Path(frozen["snapshot"]))))
            now = _now()
            immutable = {
                "version": STATE_VERSION, "kind": "registered", "operation_id": operation_id,
                "project": project, "target": target,
                "action": validated["action"], "parameters": validated["parameters"],
                "impact": validated["impact"], "verifications": verifications,
                "target_identity": {"id": target, "environment": target_spec["environment"],
                                    "compose_project": target_spec["compose"]["project_name"],
                                    "services": list(target_spec["compose"]["services"])},
                "target_lock": identity,
                "config_digest": registry.config_digest(),
                "executor_digest": executor_digest,
                "source": {key: frozen[key] for key in (
                    "review_policy", "source_digest", "file_count", "total_bytes", "git_head",
                    "changed_paths", "high_risk_changes", "review_digest")},
                "preflight": preflight, "preflight_digest": preflight_digest,
                "nonce": secrets.token_hex(32),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
            }
            state = {
                "version": STATE_VERSION, "kind": "registered", "project": project,
                "target": target,
                "operation_id": operation_id, "request_id": request_id,
                "request_digest": request_digest,
                "state": "pending_host_approval", "phase": "APPROVAL", "code": None,
                "diagnostic_id": None, "result": None,
                "created_at": now.isoformat(), "approved_at": None, "started_at": None,
                "finished_at": None, "immutable": immutable, "plan_digest": _digest(immutable),
            }
            self._append_event(operation_id, "APPROVAL", "PLAN_CREATED", {})
            self._save(state)
            return self._public(state)
        except BaseException:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            with contextlib.suppress(OSError):
                self._events_path(operation_id).unlink(missing_ok=True)
            raise

    @_no_raw_os_errors
    def get(self, project, target, operation_id) -> dict:
        with _lock(self.lock_path):
            return self._public(self._refresh(self._load(project, target, operation_id)))

    @_no_raw_os_errors
    def pending(self, project) -> dict:
        self._require_trusted_runtime()
        self._project_root(project)
        plans = []
        with _lock(self.lock_path):
            for state in self._states():
                if (state["project"] == project and state["state"] == "pending_host_approval"
                        and self._refresh(state)["state"] == "pending_host_approval"):
                    plans.append(self._public(state))
        plans.sort(key=lambda plan: plan["created_at"])
        return {"project": project, "plans": plans[:MAX_PENDING]}

    @_no_raw_os_errors
    def approve_web(self, project, target, *, operation_id, plan_digest, source_digest,
                    confirmation) -> dict:
        """Only caller: the authenticated admin web endpoint (A4). Writes a one-use receipt."""
        self._require_trusted_runtime()
        if (not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id)
                or not isinstance(plan_digest, str) or not HEX64.fullmatch(plan_digest)
                or not isinstance(source_digest, str) or not HEX64.fullmatch(source_digest)
                or confirmation != operation_id):
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        with _lock(self.lock_path):
            state = self._refresh(self._load(project, target, operation_id))
            if state["state"] == "expired":
                raise HostRuntimeError("PLAN_EXPIRED")
            if state["state"] != "pending_host_approval":
                raise HostRuntimeError("NOT_PENDING_APPROVAL")
            immutable = state["immutable"]
            if (state["plan_digest"] != plan_digest
                    or immutable["source"]["source_digest"] != source_digest):
                raise HostRuntimeError("STALE_PLAN")
            self._verify_or_reject(state)
            approved_at = _now().isoformat()
            try:
                fd = os.open(self._receipt_path(operation_id),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError as exc:
                raise HostRuntimeError("INVALID_HOST_APPROVAL") from exc
            with os.fdopen(fd, "w") as stream:
                json.dump({"operation_id": operation_id, "plan_digest": plan_digest,
                           "nonce": immutable["nonce"], "approved_at": approved_at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            self._transition(state, ("APPROVAL", "HOST_APPROVED", {}), state="approved",
                             approved_at=approved_at)
            return {"operation_id": operation_id, "approved": True, "state": "approved",
                    "plan_digest": plan_digest}

    @_no_raw_os_errors
    def reject_web(self, project, target, *, operation_id, confirmation) -> dict:
        """Only caller: the authenticated admin web endpoint (A4)."""
        self._require_trusted_runtime()
        if (not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id)
                or confirmation != operation_id):
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        with _lock(self.lock_path):
            state = self._refresh(self._load(project, target, operation_id))
            if state["state"] != "pending_host_approval":
                raise HostRuntimeError("NOT_PENDING_APPROVAL")
            self._receipt_path(operation_id).unlink(missing_ok=True)
            self._transition(state, ("APPROVAL", "HOST_REJECTED", {}), state="rejected",
                             phase="APPROVAL", code="HOST_REJECTED",
                             finished_at=_now().isoformat())
            return self._public(state)

    def _check_receipt(self, state) -> Path:
        path = self._receipt_path(state["operation_id"])
        try:
            value = _read_private_json(path)
            approved_at = _timestamp(value.get("approved_at"))
        except HostRuntimeError as exc:
            raise HostRuntimeError("INVALID_HOST_APPROVAL") from exc
        if (set(value) != {"operation_id", "plan_digest", "nonce", "approved_at"}
                or value["operation_id"] != state["operation_id"]
                or value["plan_digest"] != state["plan_digest"]
                or value["nonce"] != state["immutable"]["nonce"]
                or value["approved_at"] != state["approved_at"]
                or approved_at < _timestamp(state["created_at"])
                or approved_at > _now() + timedelta(minutes=1)):
            raise HostRuntimeError("INVALID_HOST_APPROVAL")
        return path

    @_no_raw_os_errors
    def _begin(self, project, target, operation_id) -> dict:
        with _lock(self.lock_path):
            state = self._refresh(self._load(project, target, operation_id))
            if state["state"] == "pending_host_approval":
                raise HostRuntimeError("HOST_APPROVAL_REQUIRED")
            if state["state"] != "approved":
                return state
        workspace_fd = acquire_host_operation_lane(
            self.workspace, operation_id=operation_id, kind="registered")
        handed_to_worker = False
        try:
            with host_admission(self.state_dir.parent):
                state = self._begin_admitted(
                    project, target, operation_id, workspace_fd=workspace_fd)
                handed_to_worker = state.get("state") == "queued"
                return state
        finally:
            if not handed_to_worker:
                complete_host_operation(
                    self.workspace, operation_id, "registered",
                    termination_verified=True)
            if workspace_fd is not None:
                os.close(workspace_fd)

    def _begin_admitted(self, project, target, operation_id, *, workspace_fd=None) -> dict:
        """Verify, consume the receipt, queue and hand the target lock to a worker thread,
        all in this one synchronous call: lock ownership never crosses an await boundary,
        so cancelling execute() cannot orphan the receipt, the state or the lock."""
        target_fd = worker_workspace_fd = None
        try:
            with _lock(self.lock_path):
                state = self._refresh(self._load(project, target, operation_id))
                if state["state"] == "pending_host_approval":
                    raise HostRuntimeError("HOST_APPROVAL_REQUIRED")
                if state["state"] != "approved":
                    return state  # Repeat requests observe; never re-run.
                receipt = self._check_receipt(state)
                target_fd = acquire_flock(self._lock_path_for(state["immutable"]["target_lock"]))
                target_spec, adapter = self._verify_or_reject(state)
                if workspace_fd is not None:
                    worker_workspace_fd = os.dup(workspace_fd)
                receipt.unlink()  # One use, before any adapter effect.
                self._transition(state, ("QUEUED", "OPERATION_QUEUED", {}), state="queued",
                                 phase="QUEUED", code=None, started_at=_now().isoformat())
                worker = threading.Thread(
                    target=self._run_operation, name="host-operation-" + operation_id[:8],
                    args=(operation_id, target_fd, worker_workspace_fd, target_spec, adapter))
                self._workers[operation_id] = worker
                try:
                    worker.start()
                except RuntimeError:
                    self._workers.pop(operation_id, None)
                    self._transition(state, ("COMPLETE", "OPERATION_FAILED",
                                             {"error_code": "EXECUTOR_UNAVAILABLE"}),
                                     state="failed", code="EXECUTOR_UNAVAILABLE",
                                     finished_at=_now().isoformat())
                    complete_host_operation(
                        self.workspace, operation_id, "registered",
                        termination_verified=True)
                    raise HostRuntimeError("EXECUTOR_UNAVAILABLE") from None
                target_fd = None  # The worker owns and closes it.
                worker_workspace_fd = None
                return state
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if worker_workspace_fd is not None:
                os.close(worker_workspace_fd)

    async def execute(self, project, target, operation_id) -> dict:
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return await self._execute_activation_admitted(
                    project, target, operation_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    async def _execute_activation_admitted(self, project, target, operation_id) -> dict:
        self._require_trusted_runtime()
        state = await asyncio.to_thread(self._begin, project, target, operation_id)
        return self._public(state)

    def _run_operation(self, operation_id, target_fd, workspace_fd, target_spec, adapter) -> None:
        """Owns the target lock from queueing through postcheck."""
        try:
            self._run_body(operation_id, target_spec, adapter)
        finally:
            try:
                try:
                    if self._read_state(operation_id)["state"] in {
                            "succeeded", "failed", "rejected"}:
                        complete_host_operation(
                            self.workspace, operation_id, "registered",
                            termination_verified=True)
                except (HostRuntimeError, OSError):
                    self._record_reservation_release_failure(operation_id)
            finally:
                os.close(target_fd)
                if workspace_fd is not None:
                    os.close(workspace_fd)
                self._workers.pop(operation_id, None)

    def _record_reservation_release_failure(self, operation_id):
        _LOGGER.error(_RESERVATION_RELEASE_FAILED)
        try:
            self._record(
                operation_id,
                ("RECOVER", _RESERVATION_RELEASE_FAILED, {}),
                state="outcome_unknown", phase="RECOVER",
                code=_RESERVATION_RELEASE_FAILED, finished_at=_now().isoformat())
        except (HostRuntimeError, OSError):
            _LOGGER.error("WORKSPACE_HOST_RESERVATION_STATE_WRITE_FAILED")

    def _run_body(self, operation_id, target_spec, adapter) -> None:
        context = None
        emitter = None
        executed = False
        phase = "PRECHECK"
        services = target_spec["compose"]["services"]
        try:
            state = self._record(operation_id, ("PRECHECK", "OPERATION_STARTED", {}),
                                 state="running", phase=phase)
            immutable = self._adapter_view(state["immutable"])
            context = OperationContext(
                operation_id, self._snapshot_dir(operation_id) / "source",
                work_dir=private_dir(self._snapshot_dir(operation_id) / "work"), read_only=False)
            emitter = _Emitter(self, operation_id, services, context)
            result = adapter.execute(copy.deepcopy(target_spec), immutable, context, emitter)
            executed = True  # From here on the effect may exist even if never declared.
            phase = "VERIFY"
            self._record(operation_id, ("VERIFY", "POSTCHECK_STARTED", {}), phase=phase)
            check = adapter.postcheck(copy.deepcopy(target_spec), immutable, result)
            declared = immutable["verifications"]
            reported = check.get("verifications") if isinstance(check, dict) else None
            if (not isinstance(check, dict) or check.get("verified") is not True
                    or not isinstance(reported, list)
                    or not set(declared) <= {code for code in reported if isinstance(code, str)}):
                raise HostRuntimeError("POSTCHECK_UNVERIFIED", "VERIFY")
            public_result = _clean_fields(check, services)
            public_result["verifications"] = list(declared)
            self._record(operation_id, ("VERIFY", "POSTCHECK_VERIFIED", {"verified": True}),
                         ("COMPLETE", "OPERATION_SUCCEEDED", {}),
                         state="succeeded", phase="COMPLETE", code=None,
                         result=public_result, finished_at=_now().isoformat())
        except Exception as exc:
            self._finish_failed(operation_id, phase, emitter, context, executed, exc)

    def _finish_failed(self, operation_id, phase, emitter, context, executed, error) -> None:
        code, diagnostic = getattr(error, "code", None), None
        if not isinstance(error, HostRuntimeError) or not _valid_code(code):
            code, diagnostic = "HOST_OPERATION_FAILED", secrets.token_hex(16)
            self._record_diagnostic(diagnostic, error, "run", operation_id)
        if emitter is not None and phase != "VERIFY":
            phase = emitter.phase
        if executed or (context is not None and context.side_effects):
            final = "outcome_unknown"  # Never failed/rejected once an effect may exist.
        else:
            final = "rejected" if code in ("STALE_PLAN", "PLAN_EXPIRED") else "failed"
        fields = {"error_code": code}
        if diagnostic:
            fields["diagnostic_id"] = diagnostic
        self._record(operation_id, ("COMPLETE", TERMINAL_EVENT[final], fields), state=final,
                     phase=phase, code=code, diagnostic_id=diagnostic,
                     finished_at=_now().isoformat())

    @_no_raw_os_errors
    def events(self, project, target, operation_id, *, cursor=0, limit=DEFAULT_EVENT_LIMIT) -> dict:
        if (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
                or isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_EVENT_LIMIT):
            raise HostRuntimeError("INVALID_EVENT_QUERY")
        with _lock(self.lock_path):
            state = self._refresh(self._load(project, target, operation_id))
            remaining = [event for event in self._read_events(operation_id)
                         if event["cursor"] > cursor]
        page = {"project": project, "target": target, "operation_id": operation_id,
                "state": state["state"], "phase": state["phase"],
                "events": [], "next_cursor": 10 ** 12, "more": False}
        # Upper bound of the default JSON encoding: envelope + items + ", " separators.
        size = len(json.dumps(page).encode())
        for event in remaining[:limit]:
            extra = len(json.dumps(event).encode()) + (2 if page["events"] else 0)
            if size + extra > MAX_EVENT_RESPONSE:
                break
            page["events"].append(event)
            size += extra
        page["next_cursor"] = page["events"][-1]["cursor"] if page["events"] else cursor
        page["more"] = len(page["events"]) < len(remaining)
        return copy.deepcopy(page)

    @_no_raw_os_errors
    def get_for_adapter(self, operation_id) -> dict:
        """Backend-internal verified plan (no nonce). Never return this through MCP."""
        self._require_trusted_runtime()
        with _lock(self.lock_path):
            state = self._read_state(operation_id)
        return {"operation_id": operation_id, "project": state["project"],
                "target": state["target"], "state": state["state"], "phase": state["phase"],
                "plan_digest": state["plan_digest"],
                "immutable": self._adapter_view(state["immutable"]),
                "snapshot": str(self._snapshot_dir(operation_id) / "source")}

    @_no_raw_os_errors
    def review_artifact(self, project, target, operation_id) -> dict:
        """Private high-risk diff for the authenticated admin web only (never MCP)."""
        self._require_trusted_runtime()
        with _lock(self.lock_path):
            state = self._load(project, target, operation_id)
        source = state["immutable"]["source"]
        if source["review_digest"] is None:
            return {"version": 1, "git_head": source["git_head"],
                    "source_digest": source["source_digest"], "files": []}
        path = self._snapshot_dir(operation_id) / "review.json"
        private_file(path)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source["review_digest"]:
            raise HostRuntimeError("STALE_PLAN")
        return json.loads(data)
