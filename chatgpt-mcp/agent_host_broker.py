"""Typed, lease-bound access from a dedicated agent to registered host operations.

The broker never accepts argv, SQL, a caller-asserted approval flag, or a legacy target
grant. Authority comes from private task state, a server-created lease, live supervisor
identity, frozen capabilities, and the registered-v3 host manager together.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from agent_profiles import profile_digest
from host_registered_operations import RegisteredHostOperations
from host_runtime_config import HostRuntimeError, private_dir, private_file
from host_runtime_operations import (
    REQUEST_PATTERN,
    _lock,
    _read_private_json,
    _save_private_json,
    _timestamp,
)

TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
INVOCATION_ID = re.compile(r"[0-9a-f]{32}\Z")
BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
UNIT = re.compile(r"[A-Za-z0-9_.@:-]{1,200}\.(?:service|scope)\Z")
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")

INSPECT_ACTION = "docker.inspect"
HOST_ACTIONS = frozenset({
    "compose.build", "compose.apply", "compose.up", "compose.restart",
    "compose.stop",
})
CAPABILITY_ACTIONS = frozenset({INSPECT_ACTION}) | HOST_ACTIONS
IDENTITY_KEYS = frozenset({
    "task_id", "unit", "invocation_id", "cgroup", "boot_id", "uid", "pid",
    "start_ticks", "wrapper_pid", "wrapper_start_ticks",
})
OBSERVED_IDENTITY_KEYS = frozenset({"unit", "load_state", "transient", "invocation_id",
                                    "cgroup", "pid", "active_state", "sub_state"})
LEASE_KEYS = frozenset({
    "version", "lease_id", "project", "task_id", "profile_id", "profile_digest",
    "source_digest", "host_capabilities", "context_nonce", "identity", "created_at",
    "expires_at",
})
BINDING_KEYS = frozenset({
    "version", "lease_id", "project", "task_id", "profile_id", "profile_digest",
    "agent_source_digest", "host_source_digest", "target", "action", "operation_id",
    "plan_digest", "created_at",
})
MAX_CAPABILITY_TARGETS = 8
MAX_ACTIONS_PER_TARGET = len(CAPABILITY_ACTIONS)
MAX_LEASE_TTL_SECONDS = 3600


def _private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")


def _valid_identity(value: object, task_id: str) -> bool:
    if not isinstance(value, dict) or set(value) != IDENTITY_KEYS:
        return False
    return (
        value["task_id"] == task_id
        and isinstance(value["unit"], str) and UNIT.fullmatch(value["unit"]) is not None
        and isinstance(value["invocation_id"], str)
        and INVOCATION_ID.fullmatch(value["invocation_id"]) is not None
        and isinstance(value["cgroup"], str) and value["cgroup"].startswith("/")
        and 1 < len(value["cgroup"]) <= 1024 and ".." not in value["cgroup"].split("/")
        and isinstance(value["boot_id"], str) and BOOT_ID.fullmatch(value["boot_id"]) is not None
        and type(value["uid"]) is int and value["uid"] == os.getuid()
        and all(type(value[key]) is int and value[key] > 0
                for key in ("pid", "start_ticks", "wrapper_pid", "wrapper_start_ticks"))
    )


def validate_host_capabilities(value: object) -> list[dict]:
    """Validate the frozen v3-only capability schema. An empty list grants nothing."""
    if not isinstance(value, list) or len(value) > MAX_CAPABILITY_TARGETS:
        raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
    normalized = []
    targets = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"target", "actions"}:
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
        target, actions = item["target"], item["actions"]
        if (not isinstance(target, str) or TARGET.fullmatch(target) is None or target in targets
                or not isinstance(actions, list) or not actions
                or len(actions) > MAX_ACTIONS_PER_TARGET or len(set(actions)) != len(actions)
                or any(not isinstance(action, str) or action not in CAPABILITY_ACTIONS
                       for action in actions)):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
        targets.add(target)
        normalized.append({"target": target, "actions": list(actions)})
    return normalized


class PrivateTaskLeaseVerifier:
    """Cross-check a private lease against live task state and systemd identity."""

    def __init__(self, tasks, supervisor):
        self.tasks = tasks
        self.supervisor = supervisor

    def _lease_path(self, task_id: str) -> Path:
        return (Path(self.tasks.sources_dir) / task_id / "source" / ".agent-control"
                / "broker-lease.json")

    def verify(self, project: str, task_id: str) -> dict:
        if (not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None
                or self.supervisor is None or not hasattr(self.supervisor, "observe")
                or not hasattr(self.supervisor, "verify_binding")):
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
        try:
            with _lock(self.tasks.lock):
                task = copy.deepcopy(self.tasks._load(project, task_id))
        except HostRuntimeError:
            raise
        except Exception:
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED") from None
        if task.get("state") != "running":
            raise HostRuntimeError("AGENT_TASK_NOT_RUNNING")
        immutable = task.get("immutable")
        if not isinstance(immutable, dict):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
        receipt = Path(self.tasks.receipts_dir) / (task_id + ".json")
        try:
            if receipt.exists() or receipt.is_symlink():
                raise HostRuntimeError("AGENT_APPROVAL_NOT_CONSUMED")
        except OSError:
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID") from None

        path = self._lease_path(task_id)
        _private_directory(path.parent)
        private_file(path)
        lease = _read_private_json(path)
        if set(lease) != LEASE_KEYS or lease.get("version") != 1:
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
        caps = validate_host_capabilities(lease.get("host_capabilities"))
        frozen_caps = validate_host_capabilities(immutable.get("host_capabilities"))
        profile_id = immutable.get("profile_id")
        source_digest = immutable.get("source_digest")
        timeout_minutes = immutable.get("timeout_minutes")
        context_nonce = task.get("broker_context_nonce")
        lease_id = lease.get("lease_id")
        if (not isinstance(profile_id, str) or PROFILE.fullmatch(profile_id) is None
                or not isinstance(source_digest, str) or HEX64.fullmatch(source_digest) is None
                or type(timeout_minutes) is not int or not 1 <= timeout_minutes <= 60
                or not isinstance(context_nonce, str) or HEX64.fullmatch(context_nonce) is None
                or caps != frozen_caps or not isinstance(lease_id, str)
                or HEX64.fullmatch(lease_id) is None
                or lease.get("project") != project or lease.get("task_id") != task_id
                or lease.get("profile_id") != profile_id
                or lease.get("source_digest") != source_digest
                or lease.get("context_nonce") != context_nonce):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")
        profile = self.tasks.profiles.require(project, profile_id)
        current_digest = profile_digest(profile)
        if (immutable.get("profile_digest") != current_digest
                or lease.get("profile_digest") != current_digest
                or validate_host_capabilities(profile.get("host_capabilities")) != caps):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")

        try:
            created = _timestamp(lease["created_at"])
            expires = _timestamp(lease["expires_at"])
            started = _timestamp(task.get("started_at"))
        except (HostRuntimeError, KeyError, TypeError):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID") from None
        now = datetime.now(timezone.utc)
        if expires <= now:
            raise HostRuntimeError("AGENT_BROKER_LEASE_EXPIRED")
        if (created < started - timedelta(minutes=1) or created > now + timedelta(minutes=1)
                or expires <= created
                or expires - created > timedelta(seconds=MAX_LEASE_TTL_SECONDS)
                or expires > started + timedelta(minutes=timeout_minutes)):
            raise HostRuntimeError("AGENT_BROKER_LEASE_INVALID")

        identity = lease.get("identity")
        if (not _valid_identity(identity, task_id)
                or task.get("execution_identity") != identity):
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
        try:
            observed = self.supervisor.observe(identity["unit"])
            if (not isinstance(observed, dict) or set(observed) != OBSERVED_IDENTITY_KEYS
                    or any(observed[key] != identity[key]
                           for key in ("unit", "invocation_id", "cgroup", "pid"))
                    or observed["load_state"] != "loaded" or observed["transient"] is not True
                    or observed["active_state"] not in {"active", "activating"}
                    or observed["sub_state"] not in {"running", "start"}):
                raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
            bound = self.supervisor.verify_binding(identity, peer_pid=os.getpid())
        except Exception:
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED") from None
        if bound is not True:
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
        return {
            "lease_id": lease_id, "project": project, "task_id": task_id,
            "profile_id": profile_id, "profile_digest": current_digest,
            "source_digest": source_digest, "host_capabilities": caps,
            "context_nonce": context_nonce,
        }


class AgentHostBroker:
    """Four typed operations over one registered-v3 host manager."""

    def __init__(self, task_manager, host_operations=None, *, supervisor=None,
                 lease_verifier=None):
        self.tasks = task_manager
        self.host_operations = host_operations
        self.lease_verifier = (lease_verifier if lease_verifier is not None else
                               PrivateTaskLeaseVerifier(task_manager, supervisor)
                               if supervisor is not None else None)
        root = getattr(task_manager, "root", None)
        self.broker_dir = private_dir(Path(root) / "broker") if root is not None else None
        self.bindings_dir = (private_dir(self.broker_dir / "operations")
                             if self.broker_dir is not None else None)
        self.lock = self.broker_dir / "broker.lock" if self.broker_dir is not None else None

    def _registered(self) -> RegisteredHostOperations:
        if not isinstance(self.host_operations, RegisteredHostOperations):
            raise HostRuntimeError("AGENT_HOST_BROKER_UNVERIFIED")
        self.host_operations._require_trusted_runtime()
        registry = self.host_operations.registry.reload()
        reason = registry.availability()["reason"]
        if reason is not None:
            raise HostRuntimeError(reason)
        return self.host_operations

    def _authorize(self, project: str, task_id: str, target: str, action: str) -> dict:
        if self.lease_verifier is None:
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
        authority = self.lease_verifier.verify(project, task_id)
        manager = self._registered()
        target_spec = manager.registry.reload().require_target(project, target)
        caps = authority["host_capabilities"]
        allowed = next((entry["actions"] for entry in caps if entry["target"] == target), ())
        if action not in allowed:
            raise HostRuntimeError("AGENT_HOST_SCOPE_DISABLED")
        if action != INSPECT_ACTION and action not in target_spec["capabilities"]:
            raise HostRuntimeError("POLICY_DENIED")
        return authority

    @staticmethod
    def _scoped_request_id(task_id: str, request_id: object) -> str:
        if not isinstance(request_id, str) or REQUEST_PATTERN.fullmatch(request_id) is None:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return ("agent:" + task_id + ":"
                + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32])

    def _binding_path(self, operation_id: str) -> Path:
        if (self.bindings_dir is None or not isinstance(operation_id, str)
                or TASK_ID.fullmatch(operation_id) is None):
            raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED")
        return self.bindings_dir / (operation_id + ".json")

    def _bind(self, authority: dict, plan: dict) -> None:
        binding = {
            "version": 1, "lease_id": authority["lease_id"],
            "project": authority["project"], "task_id": authority["task_id"],
            "profile_id": authority["profile_id"],
            "profile_digest": authority["profile_digest"],
            "agent_source_digest": authority["source_digest"],
            "host_source_digest": plan["source_digest"],
            "target": plan["target"], "action": plan["action"],
            "operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self._binding_path(plan["operation_id"])
        with _lock(self.lock):
            if path.exists() or path.is_symlink():
                current = _read_private_json(path)
                comparable = {key: value for key, value in current.items() if key != "created_at"}
                expected = {key: value for key, value in binding.items() if key != "created_at"}
                if set(current) != BINDING_KEYS or comparable != expected:
                    raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED")
                return
            _save_private_json(path, binding)

    def _owned(self, project: str, task_id: str, operation_id: str) -> tuple[dict, dict]:
        path = self._binding_path(operation_id)
        try:
            private_file(path)
            binding = _read_private_json(path)
        except (HostRuntimeError, OSError):
            raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED") from None
        if (set(binding) != BINDING_KEYS or binding.get("version") != 1
                or binding.get("project") != project or binding.get("task_id") != task_id
                or binding.get("operation_id") != operation_id):
            raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED")
        authority = self._authorize(project, task_id, binding["target"], binding["action"])
        if (any(binding[key] != authority[key] for key in (
                "lease_id", "profile_id", "profile_digest"))
                or binding["agent_source_digest"] != authority["source_digest"]):
            raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED")
        manager = self._registered()
        try:
            operation = manager.get_for_adapter(operation_id)
        except HostRuntimeError as exc:
            if exc.code == "OPERATION_NOT_FOUND":
                raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED") from None
            raise
        immutable = operation["immutable"]
        if (operation["project"] != project or operation["target"] != binding["target"]
                or immutable["action"] != binding["action"]
                or operation["plan_digest"] != binding["plan_digest"]
                or immutable["source"]["source_digest"] != binding["host_source_digest"]):
            raise HostRuntimeError("AGENT_HOST_OPERATION_NOT_OWNED")
        return binding, authority

    @staticmethod
    def _waiting(plan: dict) -> dict:
        return {
            "project": plan["project"], "target": plan["target"],
            "operation_id": plan["operation_id"], "action": plan["action"],
            "state": "waiting_host_approval", "approval_required": True,
            "plan_digest": plan["plan_digest"], "source_digest": plan["source_digest"],
            "expires_at": plan["expires_at"], "approval_url": plan.get("approval_url"),
        }

    def inspect(self, project, task_id, target, area):
        if area != "docker":
            self._authorize(project, task_id, target, "inspect." + str(area))
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        self._authorize(project, task_id, target, INSPECT_ACTION)
        from host_registered_tools import inspect_target
        return inspect_target(self._registered(), project, target, area)

    def prepare(self, project, task_id, target, action, parameters, request_id):
        if not isinstance(action, str) or action not in HOST_ACTIONS:
            self._authorize(project, task_id, target, str(action))
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        authority = self._authorize(project, task_id, target, action)
        scoped = self._scoped_request_id(task_id, request_id)
        plan = self._registered().prepare(
            project, target, action=action, parameters=parameters, request_id=scoped)
        self._bind(authority, plan)
        return self._waiting(plan)

    async def execute(self, project, task_id, operation_id):
        binding, _ = self._owned(project, task_id, operation_id)
        manager = self._registered()
        state = manager.get(project, binding["target"], operation_id)
        if state["state"] == "pending_host_approval":
            return self._waiting(state)
        if state["state"] == "expired":
            raise HostRuntimeError("PLAN_EXPIRED")
        if state["state"] == "outcome_unknown":
            raise HostRuntimeError("AGENT_HOST_OUTCOME_UNKNOWN")
        return await manager.execute(project, binding["target"], operation_id)

    def get(self, project, task_id, operation_id):
        binding, _ = self._owned(project, task_id, operation_id)
        return self._registered().get(project, binding["target"], operation_id)
