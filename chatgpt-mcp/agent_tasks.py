"""Least-privilege delegation task state machine. User prompts never become host shell.

This manager intentionally does not execute Codex unless an explicit host-pinned
profile, verified proofs, one-use local web approval, and a separate vetted
runner are all present. Unsupported broker or isolation is fail-closed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat

from host_runtime_config import HostRuntimeError, private_dir, private_file, validate_state_root
from host_runtime_operations import (
    _digest, _lock, _read_private_json, _save_private_json, utc_now,
    _timestamp, _verified_installation,
)
from agent_profiles import AgentProfileRegistry, SENSITIVE_SOURCE_PARTS, profile_digest
from agent_events import sanitize_agent_event
from host_execution import _safe_task_label
from workspace_control import WorkspaceControlError, activation_admission

TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
REASONS = frozenset({"MISSING_TOOL", "WORKFLOW_COMPLEXITY", "EXECUTION_ENV_UNSUPPORTED"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "rejected", "expired", "outcome_unknown",
                      "review_required"})
TTL = 7 * 24 * 60 * 60
MAX_TASKS = 1000
MAX_PLAN_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RETAINED_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PUBLIC_EVENTS = 200
MAX_PUBLIC_EVENTS_BYTES = 64 * 1024
ACTIVE = frozenset({"queued", "running", "cancelling", "waiting"})
SAFE_RUNNER_CODES = frozenset({
    "AGENT_ALREADY_RUNNING", "AGENT_CONTROL_UNAVAILABLE", "AGENT_EVENT_LIMIT",
    "AGENT_EVENT_TOO_LARGE",
    "AGENT_PROCESS_TREE_UNVERIFIED", "AGENT_START_FAILED", "AGENT_TIMEOUT",
    "ISOLATION_UNVERIFIED", "SANDBOX_HELPER_UNVERIFIED",
})


def _retained_bytes(root: Path) -> int:
    total = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        for entry in os.scandir(folder):
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode)
                                                  or stat.S_ISREG(info.st_mode)):
                raise HostRuntimeError("SOURCE_CAPACITY_REACHED")
            if stat.S_ISDIR(info.st_mode):
                pending.append(Path(entry.path))
            else:
                total += info.st_size
                if total > MAX_RETAINED_SOURCE_BYTES:
                    return total
    return total


def _discard_new_snapshot(destination: Path) -> None:
    """Remove only the exact snapshot directory allocated by this prepare call."""
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HostRuntimeError("UNSAFE_STATE_PATH")
    shutil.rmtree(destination)


def _public(data: dict) -> dict:
    frozen = data["immutable"]
    task_label = _safe_task_label(frozen.get("task_label"), error_code="INVALID_AGENT_TASK")
    public = {
        "project": data["project"], "task_id": data["task_id"],
        "profile_id": frozen["profile_id"], "provider": "codex",
        "model": frozen["model"], "reasoning_effort": frozen["reasoning_effort"],
        "reason": frozen["reason"], "state": data["state"], "phase": data["phase"],
        "code": data.get("code"), "plan_digest": data["plan_digest"],
        "source_digest": frozen["source_digest"], "objective_sha256": frozen["objective_sha256"],
        "changed_paths": list(frozen["changed_paths"]),
        "allowed_paths": list(frozen["allowed_paths"]),
        "host_capabilities": list(frozen.get("host_capabilities", [])),
        "created_at": data["created_at"], "expires_at": frozen["expires_at"],
        "started_at": data.get("started_at"), "finished_at": data.get("finished_at"),
        "result": data.get("result"), "model_usage_estimated": False,
        "approval_required": data["state"] == "pending_host_approval",
        "work_context": {
            "task_label": task_label,
            "label_source": "reported" if task_label is not None else "unavailable",
            "command_label": "Codex isolated task",
            "action": "code",
        },
    }
    for field in ("execution_identity", "identity_captured_at", "events", "provider_usage",
                  "termination_verified"):
        if field in data:
            public[field] = data[field]
    return public


def _safe_identity(value, task_id):
    if not isinstance(value, dict):
        return None
    identity = {}
    for key in ("pid", "start_ticks", "uid", "wrapper_pid", "wrapper_start_ticks"):
        item = value.get(key)
        if type(item) is int and 0 < item < 2 ** 63:
            identity[key] = item
    text_rules = {
        "task_id": re.compile(r"[0-9a-f]{32}\Z"),
        "unit": re.compile(r"personal-ops-agent-[0-9a-f]{32}\.service\Z"),
        "invocation_id": re.compile(r"[0-9a-f]{32}\Z"),
        "boot_id": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"),
        "cgroup": re.compile(r"/user\.slice/user-[0-9]+\.slice/user@[0-9]+\.service/app\.slice/"
                             r"personal-ops-agent-[0-9a-f]{32}\.service\Z"),
    }
    for key, pattern in text_rules.items():
        item = value.get(key)
        if isinstance(item, str) and pattern.fullmatch(item):
            identity[key] = item
    if "task_id" in identity and identity["task_id"] != task_id:
        return None
    required = {"task_id", "unit", "invocation_id", "cgroup", "boot_id", "uid", "pid",
                "start_ticks", "wrapper_pid", "wrapper_start_ticks"}
    if set(identity) != required or identity["uid"] != os.getuid():
        return None
    return identity


def _safe_structured_result(value):
    if not isinstance(value, dict) or set(value) != {
            "summary", "changed_files", "validations", "remaining", "approval_required"}:
        return None
    summary = value["summary"]
    changed = value["changed_files"]
    validations = value["validations"]
    remaining = value["remaining"]
    if (not isinstance(summary, str) or not summary or len(summary) > 4000
            or not isinstance(changed, list) or len(changed) > 100
            or not isinstance(validations, list) or len(validations) > 50
            or not isinstance(remaining, list) or len(remaining) > 50
            or type(value["approval_required"]) is not bool):
        return None
    if (any(not isinstance(path, str) or len(path) > 512 for path in changed)
            or any(not isinstance(item, str) or not item or len(item) > 200
                   for item in validations)
            or any(not isinstance(item, str) or not item or len(item) > 500
                   for item in remaining)):
        return None
    safe_paths = []
    for path in changed:
        event = sanitize_agent_event({"phase": "WORKING", "code": "FILE_CHANGE",
                                      "paths": [path]})
        if event is None:
            return None
        safe_paths.extend(event["paths"])
    return {
        "summary": summary, "changed_files": safe_paths,
        "validations": validations, "remaining": remaining,
        "approval_required": value["approval_required"],
        "provenance": "provider_reported",
        "validation_evidence": "model_report_only",
    }


class AgentTaskManager:
    def __init__(self, projects: dict, profiles: AgentProfileRegistry | None = None,
                 *, state_dir=None, runner=None, trusted_runtime=None, event_sink=None,
                 workspace=None):
        self.projects = {name: Path(path).resolve(strict=True) for name, path in projects.items()}
        self.root = validate_state_root(
            state_dir or (Path.home() / ".local/state/chatgpt-coding-mcp/agent-tasks"),
            self.projects)
        self.tasks_dir = private_dir(self.root / "tasks")
        self.sources_dir = private_dir(self.root / "sources")
        self.receipts_dir = private_dir(self.root / "receipts")
        self.leases_dir = private_dir(self.root / "leases")
        self.lock = self.root / "agent-tasks.lock"
        self.profiles = profiles or AgentProfileRegistry(
            self.projects, receipts_dir=self.root.parent / "agent-probes")
        self.runner = runner
        self.event_sink = event_sink
        self.workspace = workspace
        authority = getattr(workspace, "authority", None)
        self.activation_authority_dir = Path(
            getattr(authority, "state_dir", self.root.parent / "workspace-authority")
        ).absolute()
        self._trusted = _verified_installation() if trusted_runtime is None else trusted_runtime
        self._workers = {}
        self._worker_projects = {}
        self._leases = {}
        self._closing = False

    def _lease_path(self, task_id):
        self._path(task_id)
        return self.leases_dir / (task_id + ".lock")

    def _try_lease(self, task_id, *, retain):
        if task_id in self._leases:
            return self._leases[task_id]
        descriptor = os.open(self._lease_path(task_id),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "r+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        if retain:
            self._leases[task_id] = stream
        return stream

    def _release_lease(self, task_id):
        stream = self._leases.pop(task_id, None)
        if stream is not None:
            stream.close()

    def _recover_owner_lost(self, item):
        if item["state"] not in ACTIVE or item["task_id"] in self._leases:
            return item
        probe = self._try_lease(item["task_id"], retain=False)
        if probe is None:
            return item
        probe.close()
        item.update(state="outcome_unknown", phase="RECOVER",
                    code="AGENT_WORKER_OWNER_LOST", finished_at=utc_now())
        self._save(item)
        self._telemetry(item, "unknown")
        return item

    def _telemetry(self, item, state, usage=None):
        if self.event_sink is None:
            return
        frozen = item["immutable"]
        task_label = _safe_task_label(
            frozen.get("task_label"), error_code="INVALID_AGENT_TASK")
        event = {
            "schema_version": 1,
            "source_id": "personal-ops-agent",
            "source_kind": "codex-cli",
            "actor_id": frozen["profile_id"],
            "actor_kind": "agent",
            "actor_label": "Codex agent",
            "task_id": item["task_id"],
            "task_kind": "agent",
            "task_label": task_label or "Delegated code task " + item["task_id"][:8],
            "action": "code",
            "state": state,
            "time": utc_now(),
            "model": frozen["model"],
            "model_source": "configuration",
            "reasoning_effort": frozen["reasoning_effort"],
            "reasoning_effort_source": "configuration",
        }
        if usage:
            mapped = {key: usage[key] for key in (
                "input_tokens", "output_tokens", "total_tokens") if key in usage}
            if "reasoning_output_tokens" in usage:
                mapped["reasoning_tokens"] = usage["reasoning_output_tokens"]
            if mapped:
                event.update(usage=mapped, usage_scope="task", usage_estimated=False)
        try:
            self.event_sink(item["project"], event)
        except (OSError, TypeError, ValueError):
            pass

    def _guard(self, project):
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not self._trusted:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _workspace_guard(self):
        from workspace_host import resolve_workspace
        workspace = resolve_workspace(self.projects, self.workspace)
        if workspace is not None and workspace.enabled:
            # This legacy isolated-agent snapshot/CAS pipeline has a separate
            # contract. Close it until it supports common workspace admission.
            from workspace_paths import WorkspaceError
            try:
                workspace._context()
            except WorkspaceError:
                raise HostRuntimeError("WORKSPACE_AUTHORITY_INVALID") from None
            raise HostRuntimeError("WORKSPACE_ISOLATED_AGENT_UNSUPPORTED")

    def _path(self, task_id):
        if not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None:
            raise HostRuntimeError("INVALID_TASK_ID")
        return self.tasks_dir / (task_id + ".json")

    def _load(self, project, task_id):
        item = _read_private_json(self._path(task_id))
        plan = item.get("immutable", {})
        if (item.get("project") != project or item.get("task_id") != task_id
                or plan.get("project") != project or plan.get("task_id") != task_id
                or item.get("plan_digest") != _digest(plan)):
            raise HostRuntimeError("PLAN_INVALID")
        return item

    def _save(self, item):
        _save_private_json(self._path(item["task_id"]), item)

    def list_profiles(self, project):
        # No mutable-source tool may promote a profile to ready.
        self._guard(project)
        return self.profiles.list_profiles(project)

    def prepare(self, project, *, profile_id, objective, reason, request_id,
                timeout_minutes=30, task_label=None):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_admitted(
                    project, profile_id=profile_id, objective=objective, reason=reason,
                    request_id=request_id, timeout_minutes=timeout_minutes,
                    task_label=task_label)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_admitted(self, project, *, profile_id, objective, reason, request_id,
                          timeout_minutes=30, task_label=None):
        self._guard(project)
        self._workspace_guard()
        # Registered source review is optional for the legacy read-only server.
        # Missing it must close delegation without taking that server down.
        try:
            from host_source_review import freeze_source, inventory_source
        except ImportError:
            raise HostRuntimeError("REGISTRY_NOT_CONFIGURED") from None
        if (not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None
                or not isinstance(objective, str) or not 10 <= len(objective) <= 8192
                or any(ord(ch) < 32 and ch not in "\n\t" for ch in objective)
                or reason not in REASONS or type(timeout_minutes) is not int
                or not 1 <= timeout_minutes <= 60):
            raise HostRuntimeError("INVALID_AGENT_TASK")
        task_label = _safe_task_label(task_label, error_code="INVALID_AGENT_TASK")
        profile = self.profiles.require(project, profile_id)
        # No host actions: this agent is a code-review/edit agent only.
        if profile["host_capabilities"] or profile["apply_policy"] not in {"review_only", "cas"}:
            raise HostRuntimeError("UNSUPPORTED_AGENT_CAPABILITY")
        root = self.projects[project]
        for name in profile["allowed_paths"]:
            if any(part in SENSITIVE_SOURCE_PARTS or part.startswith(".env.")
                   for part in name.split("/")):
                raise HostRuntimeError("UNSAFE_AGENT_PATH")
            target = root / name
            if target.is_symlink() or not target.is_dir():
                raise HostRuntimeError("UNSAFE_AGENT_PATH")
        digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()
        with _lock(self.lock):
            for file in self.tasks_dir.glob("*.json"):
                private_file(file)
                item = _read_private_json(file)
                if item.get("project") == project and item.get("request_id") == request_id:
                    immutable = item["immutable"]
                    if (immutable["profile_id"], immutable["objective_sha256"],
                        immutable["reason"], immutable["timeout_minutes"],
                        immutable.get("task_label")) != (
                            profile_id, digest, reason, timeout_minutes, task_label):
                        raise HostRuntimeError("REQUEST_ID_CONFLICT")
                    return _public(item)
            retained_tasks = 0
            for task_path in self.tasks_dir.glob("*.json"):
                private_file(task_path)
                _read_private_json(task_path)
                retained_tasks += 1
            if retained_tasks >= MAX_TASKS:
                raise HostRuntimeError("HISTORY_CAPACITY_REACHED")
            task_id = secrets.token_hex(16)
            inputs = {"files": [], "dirs": {
                path: None for path in profile["allowed_paths"]
            }}
            planned = inventory_source(root, inputs)
            if (planned["total_bytes"] > MAX_PLAN_SOURCE_BYTES
                    or _retained_bytes(self.sources_dir) + planned["total_bytes"]
                    > MAX_RETAINED_SOURCE_BYTES):
                raise HostRuntimeError("SOURCE_CAPACITY_REACHED")
            destination = self.sources_dir / task_id
            try:
                source = freeze_source(root, destination,
                                       review_policy="reviewed_snapshot",
                                       inputs=inputs, high_risk_inputs=[])
            except BaseException:
                _discard_new_snapshot(destination)
                raise
            current = utc_now()
            frozen = {
                "version": 1, "project": project, "task_id": task_id,
                "profile_id": profile_id, "profile_digest": profile_digest(profile),
                "model": profile["model"], "reasoning_effort": profile["reasoning_effort"],
                "allowed_paths": list(profile["allowed_paths"]),
                "reason": reason, "objective_sha256": digest, "objective": objective,
                "source_digest": source["source_digest"], "git_head": source["git_head"],
                "changed_paths": source["changed_paths"],
                "timeout_minutes": timeout_minutes, "apply_policy": profile["apply_policy"],
                "host_capabilities": list(profile.get("host_capabilities", [])),
                "file_manifest": [{**record, "mode": stat.S_IMODE(
                    (root / record["path"]).lstat().st_mode)}
                                  for record in source["files"]],
                "created_at": current,
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=TTL)).isoformat(),
                "nonce": secrets.token_hex(32),
            }
            if task_label is not None:
                frozen["task_label"] = task_label
            item = {"project": project, "task_id": task_id, "request_id": request_id,
                    "state": "pending_host_approval", "phase": "APPROVAL",
                    "created_at": current, "started_at": None, "finished_at": None,
                    "code": None, "result": None, "immutable": frozen,
                    "plan_digest": _digest(frozen)}
            self._save(item)
            return _public(item)

    def _verify(self, project, item):
        try:
            from host_source_review import inventory_source
        except ImportError:
            raise HostRuntimeError("REGISTRY_NOT_CONFIGURED") from None
        frozen = item["immutable"]
        profile = self.profiles.require(project, frozen["profile_id"])
        if profile_digest(profile) != frozen["profile_digest"]:
            raise HostRuntimeError("STALE_PROFILE")
        source = self.sources_dir / item["task_id"] / "source"
        inputs = {"files": [], "dirs": {p: None for p in frozen["allowed_paths"]}}
        current = inventory_source(self.projects[project], inputs)
        pinned = inventory_source(source, inputs)
        if (current["digest"] != frozen["source_digest"]
                or pinned["digest"] != frozen["source_digest"]):
            raise HostRuntimeError("STALE_PLAN")
        if _timestamp(frozen["expires_at"]) <= datetime.now(timezone.utc):
            raise HostRuntimeError("PLAN_EXPIRED")
        return source, profile

    def pending(self, project):
        self._guard(project)
        with _lock(self.lock):
            out = []
            for path in self.tasks_dir.glob("*.json"):
                item = _read_private_json(path)
                if item.get("project") != project or item.get("state") != "pending_host_approval":
                    continue
                if _timestamp(item["immutable"]["expires_at"]) <= datetime.now(timezone.utc):
                    continue
                metadata = _public(item)
                # Only the authenticated management web receives the approved task objective.
                metadata["objective"] = item["immutable"]["objective"]
                out.append(metadata)
            return {"project": project, "tasks": out[:20]}

    def approve_web(self, project, *, task_id, plan_digest, source_digest, confirmation):
        self._guard(project)
        self._workspace_guard()
        if confirmation != task_id:
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        with _lock(self.lock):
            item = self._load(project, task_id)
            if (item["state"] != "pending_host_approval"
                    or item["plan_digest"] != plan_digest
                    or item["immutable"]["source_digest"] != source_digest):
                raise HostRuntimeError("STALE_PLAN")
            self._verify(project, item)
            receipt = {
                "task_id": task_id, "plan_digest": item["plan_digest"],
                "nonce": item["immutable"]["nonce"], "approved_at": utc_now()
            }
            receipt_path = self.receipts_dir / (task_id + ".json")
            if receipt_path.exists() or receipt_path.is_symlink():
                raise HostRuntimeError("APPROVAL_REPLAY")
            _save_private_json(receipt_path, receipt)
            item["state"] = "approved"
            item["phase"] = "READY"
            self._save(item)
            return {"project": project, "task_id": task_id, "approved": True}

    def _consume(self, item):
        path = self.receipts_dir / (item["task_id"] + ".json")
        value = _read_private_json(path)
        if (set(value) != {"task_id", "plan_digest", "nonce", "approved_at"}
                or any(value[key] != expected for key, expected in (
                    ("task_id", item["task_id"]), ("plan_digest", item["plan_digest"]),
                    ("nonce", item["immutable"]["nonce"])))
                or _timestamp(value["approved_at"]) < _timestamp(item["created_at"])):
            raise HostRuntimeError("INVALID_AGENT_APPROVAL")
        path.unlink()

    def get(self, project, task_id):
        self._guard(project)
        with _lock(self.lock):
            item = self._recover_owner_lost(self._load(project, task_id))
            if (item["state"] == "pending_host_approval"
                    and _timestamp(item["immutable"]["expires_at"]) <= datetime.now(timezone.utc)):
                item.update(state="expired", phase="APPROVAL", code="PLAN_EXPIRED",
                            finished_at=utc_now())
                self._save(item)
            return _public(item)

    async def start(self, project, task_id):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return await self._start_admitted(project, task_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    async def _start_admitted(self, project, task_id):
        self._guard(project)
        self._workspace_guard()
        with _lock(self.lock):
            item = self._recover_owner_lost(self._load(project, task_id))
            if item["state"] in TERMINAL | ACTIVE:
                return _public(item)
            if item["state"] != "approved":
                raise HostRuntimeError("HOST_APPROVAL_REQUIRED")
            source, profile = self._verify(project, item)
            # No default host CLI or user session is ever started. The dedicated
            # runner must be independently proven and wired from the immutable release.
            if self.runner is None:
                raise HostRuntimeError("AGENT_RUNNER_UNAVAILABLE")
            if not self.runner.ready(profile):
                raise HostRuntimeError("ISOLATION_UNVERIFIED")
            lease = self._try_lease(task_id, retain=True)
            if lease is None:
                raise HostRuntimeError("AGENT_ALREADY_RUNNING")
            try:
                self._consume(item)
                item.update(state="queued", phase="QUEUED", started_at=utc_now(),
                            broker_context_nonce=secrets.token_hex(32),
                            events=[], provider_usage={})
                self._save(item)
                self._telemetry(item, "queued")
            except BaseException:
                self._release_lease(task_id)
                raise
        work = asyncio.create_task(self._work(project, task_id, source, profile))
        self._workers[task_id] = work
        self._worker_projects[task_id] = project
        def finished(_):
            self._workers.pop(task_id, None)
            self._worker_projects.pop(task_id, None)
            self._release_lease(task_id)
        work.add_done_callback(finished)
        return _public(item)

    async def _work(self, project, task_id, source, profile):
        # Give a cancellation submitted immediately after start() a deterministic
        # chance to own the still-queued task before any runner process is spawned.
        await asyncio.sleep(0)
        with _lock(self.lock):
            item = self._load(project, task_id)
            if item["state"] != "queued":
                return
            item.update(state="running", phase="AGENT")
            self._save(item)
            self._telemetry(item, "running")
            frozen = item["immutable"]
        def on_identity(metadata):
            identity = _safe_identity(metadata, task_id)
            if identity is None:
                return
            with _lock(self.lock):
                current = self._load(project, task_id)
                if current["state"] not in ACTIVE:
                    return
                current["execution_identity"] = identity
                current["identity_captured_at"] = utc_now()
                self._save(current)

        def on_event(metadata):
            event = sanitize_agent_event(metadata)
            if event is None:
                return
            with _lock(self.lock):
                current = self._load(project, task_id)
                if current["state"] not in ACTIVE:
                    return
                events = list(current.get("events", []))
                if len(events) >= MAX_PUBLIC_EVENTS:
                    return
                candidate = [*events, event]
                if len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) \
                        > MAX_PUBLIC_EVENTS_BYTES:
                    return
                current["events"] = candidate
                if "usage" in event:
                    current["provider_usage"] = event["usage"]
                self._save(current)
        try:
            self._workspace_guard()
            setter = getattr(self.runner, "set_task_callbacks", None)
            if setter is not None:
                setter(task_id, on_identity=on_identity, on_event=on_event)
            result = await self.runner.run({
                "task_id": task_id,
                "project": project,
                "profile_id": frozen["profile_id"],
                "profile_digest": frozen["profile_digest"],
                "source_digest": frozen["source_digest"],
                "host_capabilities": list(frozen["host_capabilities"]),
                "objective": frozen["objective"],
                "timeout_minutes": frozen["timeout_minutes"],
                "started_at": item["started_at"],
                "broker_context_nonce": item["broker_context_nonce"],
                "model": frozen["model"],
                "reasoning_effort": frozen["reasoning_effort"],
            }, source, profile)
            # A reported success is never proof that the tested code changed.
            safe = result if isinstance(result, dict) else {}
            code = safe.get("code") if isinstance(safe.get("code"), str) else None
            if safe.get("verified") is True:
                self._workspace_guard()
                from agent_results import apply_agent_result
                with _lock(self.lock):
                    current = self._load(project, task_id)
                    if current["state"] != "running":
                        return
                    evaluated = apply_agent_result(
                        frozen, source, self.projects[project],
                        apply=frozen["apply_policy"] == "cas")
                structured = _safe_structured_result(safe.get("structured_result"))
                metadata = {"source_review": evaluated}
                if structured is not None:
                    metadata["model_report"] = structured
                # A CLI exit and even a successful CAS are NOT independent tests.
                # Surface reviewed edits, never claim functional completion.
                state, phase = "review_required", "SOURCE_REVIEW"
            else:
                state, phase = "outcome_unknown", "VERIFY"
                code = code or "AGENT_RESULT_UNVERIFIED"
                metadata = {"verified": False}
        except HostRuntimeError as exc:
            state, phase = "outcome_unknown", "RECOVER"
            code = exc.code if exc.code in SAFE_RUNNER_CODES else "AGENT_FAILED"
            metadata = None
        except BaseException:
            state, phase, code, metadata = "outcome_unknown", "RECOVER", "AGENT_FAILED", None
        with _lock(self.lock):
            item = self._load(project, task_id)
            if item["state"] != "running":
                return
            item.update(state=state, phase=phase, code=code,
                        result=metadata, finished_at=utc_now())
            self._save(item)
            self._telemetry(item, "unknown", item.get("provider_usage"))

    async def cancel(self, project, task_id):
        self._guard(project)
        worker = None
        with _lock(self.lock):
            item = self._recover_owner_lost(self._load(project, task_id))
            if item["state"] in TERMINAL:
                return _public(item)
            if item["state"] in {"approved", "pending_host_approval", "queued"}:
                item.update(state="cancelled", phase="CANCEL", code=None,
                            finished_at=utc_now(), termination_verified=True)
                self._save(item)
                self._telemetry(item, "canceled")
                path = self.receipts_dir / (task_id + ".json")
                if path.exists():
                    private_file(path)
                    path.unlink()
                worker = self._workers.get(task_id)
                result = _public(item)
                if worker is None:
                    return result
            else:
                if task_id not in self._leases:
                    raise HostRuntimeError("AGENT_TASK_NOT_OWNED")
                # Mark cancellation before asking the runner to stop. A concurrently
                # returning runner may no longer apply its private source snapshot.
                item.update(state="cancelling", phase="CANCEL", code=None)
                self._save(item)
            # No shared-process PID kill: a runner must prove its own process identity.
            if item["state"] == "cancelling" and (
                    self.runner is None or not hasattr(self.runner, "cancel")):
                raise HostRuntimeError("AGENT_CANCEL_UNAVAILABLE")
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            return result
        cancel_code = "AGENT_STOP_UNVERIFIED"
        termination_verified = False
        try:
            stopped = await self.runner.cancel(task_id)
            termination_verified = (isinstance(stopped, dict)
                                    and stopped.get("termination_verified") is True)
        except HostRuntimeError as exc:
            cancel_code = exc.code if exc.code in SAFE_RUNNER_CODES else "AGENT_STOP_UNVERIFIED"
        worker = self._workers.get(task_id)
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        with _lock(self.lock):
            item = self._load(project, task_id)
            if item["state"] in TERMINAL:
                return _public(item)
            item.update(state="cancelled" if termination_verified else "outcome_unknown",
                        phase="CANCEL", code=None if termination_verified else cancel_code,
                        termination_verified=termination_verified, finished_at=utc_now())
            self._save(item)
            self._telemetry(item, "canceled" if termination_verified else "unknown",
                            item.get("provider_usage"))
            return _public(item)

    async def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            owned = list(self._worker_projects.items())
            for task_id, project in owned:
                try:
                    await self.cancel(project, task_id)
                except HostRuntimeError:
                    pass
            closer = getattr(self.runner, "close", None)
            if closer is not None:
                await closer()
        finally:
            for task_id in list(self._leases):
                self._release_lease(task_id)
