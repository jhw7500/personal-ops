"""Owner-operated MCP App approvals for registered, runtime and isolated-agent plans.

Only app callbacks receive the one-use capability. Existing receipt issuers and
execution engines keep their own source, target, grant and admission checks.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import uuid

import host_access
import host_runtime_web
from host_runtime_config import HostRuntimeError, PROJECT, TARGET, private_dir
from host_runtime_operations import _lock, _read_private_json, _save_private_json, _timestamp

UI_URI = "ui://operations/approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
FAMILIES = frozenset({"registered", "runtime", "agent"})
NAMES = frozenset({"open_operation_approval", "confirm_operation_approval",
                   "confirm_and_execute_operation", "cancel_operation_approval"})
_BASE = {"project", "family", "operation_id"}
_CALLBACK = {"card_id", "confirmation_token"}
_FIELDS = {"version", "family", "project", "target", "operation_id", "card_id",
           "plan_digest", "source_digest", "created_at", "expires_at",
           "runtime_identity", "boot_id", "uid", "token_digest", "state"}


def _token_digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class OperationApprovals:
    def __init__(self, family, operations, *, trusted_runtime=None):
        if family not in FAMILIES:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        self.family = family
        self.operations = operations
        base = operations.root if family == "agent" else (
            operations.registered_dir if family == "registered" else operations.state_dir)
        self.cards_dir = private_dir(base / "chat-approval-cards")
        self.locks_dir = private_dir(base / "chat-approval-locks")
        self.operation_lock = operations.lock if family == "agent" else operations.lock_path
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity() if self._runtime_source is None
                 else self._runtime_source())
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    def _scope(self, project, operation_id, target):
        if not isinstance(project, str) or project not in self.operations.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or _ID.fullmatch(operation_id) is None:
            raise HostRuntimeError("OPERATION_CARD_DENIED")
        if self.family == "agent":
            if target is not None:
                raise HostRuntimeError("OPERATION_CARD_DENIED")
        elif not isinstance(target, str) or _TARGET.fullmatch(target) is None:
            raise HostRuntimeError("OPERATION_CARD_DENIED")
        if self.family == "runtime" and (project, target) != (PROJECT, TARGET):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _load_card(self, operation_id):
        try:
            card = _read_private_json(self._path(operation_id))
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["family"] != self.family
                    or card["project"] not in self.operations.projects
                    or card["operation_id"] != operation_id
                    or card["state"] not in {"pending", "spent", "approved", "cancelled"}
                    or type(card["uid"]) is not int
                    or not isinstance(card["boot_id"], str)
                    or not isinstance(card["card_id"], str)
                    or _ID.fullmatch(card["card_id"]) is None
                    or any(not isinstance(card[key], str) or _DIGEST.fullmatch(card[key]) is None
                           for key in ("plan_digest", "source_digest", "runtime_identity",
                                       "token_digest"))):
                raise HostRuntimeError("OPERATION_CARD_STATE_INVALID")
            self._scope(card["project"], operation_id, card["target"])
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, KeyError, ValueError, HostRuntimeError):
            raise HostRuntimeError("OPERATION_CARD_STATE_INVALID") from None

    def _source(self, state):
        frozen = state["immutable"]
        return frozen["source"]["source_digest"] if self.family == "registered" else frozen["source_digest"]

    def _receipt(self, state, path, *, required=False):
        if not path.exists() and not path.is_symlink():
            if required:
                raise HostRuntimeError("INVALID_HOST_APPROVAL")
            return False
        try:
            receipt = _read_private_json(path)
            key = "task_id" if self.family == "agent" else "operation_id"
            expected = {key: state[key], "plan_digest": state["plan_digest"],
                        "nonce": state["immutable"]["nonce"]}
            if (set(receipt) != {*expected, "approved_at"}
                    or any(receipt.get(key) != value for key, value in expected.items())
                    or _timestamp(receipt["approved_at"]) < _timestamp(state["created_at"])
                    or _timestamp(receipt["approved_at"]) >
                    host_access._utcnow() + timedelta(minutes=1)):
                raise HostRuntimeError("INVALID_HOST_APPROVAL")
            return True
        except (OSError, TypeError, ValueError, KeyError, HostRuntimeError):
            raise HostRuntimeError("INVALID_HOST_APPROVAL") from None

    def _plan_locked(self, project, operation_id, target):
        ops = self.operations
        if self.family == "agent":
            ops._guard(project)
            ops._workspace_guard()
            state = ops._load(project, operation_id)
        else:
            ops._require_trusted_runtime()
            if self.family == "runtime":
                ops.runtime.guard(project, target)
            else:
                ops.registry.reload().require_target(project, target)
            state = ops._load(project, target, operation_id)
            if self.family == "registered":
                state = ops._refresh(state)
        frozen = state["immutable"]
        if self.family == "runtime" and frozen["component"] == "schema014":
            raise HostRuntimeError("SCHEMA014_CARD_REQUIRED")
        if self.family == "runtime" and frozen["component"] == "schema015016":
            raise HostRuntimeError("TEST_PLAN_CARD_REQUIRED")
        if state["state"] == "expired" or _timestamp(frozen["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("PLAN_EXPIRED")
        allowed = {"pending_host_approval", "approved"} if self.family != "runtime" else {"pending_host_approval"}
        if state["state"] not in allowed:
            raise HostRuntimeError("OPERATION_ALREADY_STARTED")
        if (not isinstance(state["plan_digest"], str)
                or _DIGEST.fullmatch(state["plan_digest"]) is None
                or not isinstance(self._source(state), str)
                or _DIGEST.fullmatch(self._source(state)) is None):
            raise HostRuntimeError("PLAN_INVALID")
        if self.family == "registered":
            ops._verify_or_reject(state)
            if state["state"] == "approved":
                ops._check_receipt(state)
                return state, "receipt"
            return state, None
        if self.family == "agent":
            ops._verify(project, state)
            path = ops.receipts_dir / (operation_id + ".json")
            if state["state"] == "approved":
                self._receipt(state, path, required=True)
                return state, "receipt"
            if path.exists() or path.is_symlink():
                raise HostRuntimeError("INVALID_HOST_APPROVAL")
            return state, None
        ops._verify_pending_or_retire(state)
        authorization = frozen.get("authorization", {"mode": "per_operation"})
        if authorization.get("mode") == "target":
            ops._verify_target_authorization(frozen)
            return state, "target"
        if authorization != {"mode": "per_operation"}:
            raise HostRuntimeError("PLAN_INVALID")
        approved = self._receipt(
            state, ops.operations_dir / (operation_id + ".approval.json"))
        return state, "receipt" if approved else None

    def _same_plan(self, card, state):
        if (card["plan_digest"] != state["plan_digest"]
                or card["source_digest"] != self._source(state)
                or card["expires_at"] != state["immutable"]["expires_at"]):
            raise HostRuntimeError("OPERATION_CARD_STALE")

    def _next_action(self, card, state, *, observe=False):
        args = {"project": card["project"]}
        if self.family == "agent":
            args["task_id"] = card["operation_id"]
            tool = "get_agent_task" if observe else "start_agent_task"
        else:
            args.update(target=card["target"], operation_id=card["operation_id"])
            if observe:
                tool = "read_host_operation_events" if self.family == "registered" else "get_host_operation"
            elif self.family == "registered":
                tool = "execute_host_operation"
            else:
                tool = {"api": "execute_test_deploy", "worker": "execute_test_deploy",
                        "migration": "execute_db_migration", "schema_update": "execute_db_migration",
                        "predecessor_migration": "execute_predecessor_migration"}[
                            state["immutable"]["component"]]
        return {"tool": tool, "arguments": args}

    def _public_plan(self, state):
        if self.family == "agent":
            from agent_tasks import _public
            plan = _public(state)
        else:
            plan = (self.operations._public(state) if self.family == "registered"
                    else self.operations._result(state))
        plan.pop("approval_url", None)
        plan.pop("approval_command", None)
        return plan

    def _result(self, card, state, authorization, *, token=None):
        plan = self._public_plan(state)
        public = {
            "component": "operation", "family": self.family,
            "project": card["project"], "target": card["target"],
            "operation_id": card["operation_id"], "card_id": card["card_id"],
            "state": card["state"], "plan_digest": card["plan_digest"],
            "source_digest": card["source_digest"], "expires_at": card["expires_at"],
            "executes_on_approval": False,
            "supports_direct_execution": card["state"] == "pending",
            "approval_required": card["state"] == "pending" and authorization is None,
            "receipt_verified": authorization == "receipt",
            "authorization_verified": authorization is not None,
            "plan": plan,
            "guidance": ("Use the inline owner controls. Approval-only issues a receipt; "
                         "approve-and-execute explicitly requests the exact operation. "
                         "Never ask for the hidden capability or replace the card with a web link."),
        }
        plan["approval_required"] = public["approval_required"]
        if authorization:
            public.update(approved=True, next_action=self._next_action(card, state))
            plan.update(receipt_verified=public["receipt_verified"],
                        authorization_verified=True, next_action=public["next_action"])
            public["guidance"] = (
                "The exact plan is already authorized. Do not ask for another approval. "
                "Call next_action only if the user requested execution; execution rechecks "
                "the plan and consumes the original one-use receipt where required.")
        elif card["state"] == "cancelled":
            public["approved"] = False
            public["guidance"] = "The pending operation was cancelled. No effect was executed."
        result = {"content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
                  "structuredContent": public}
        metadata = {}
        if token is not None:
            metadata["operation_confirmation"] = token
        if self.family == "agent" and card["state"] == "pending":
            frozen = state["immutable"]
            metadata["operation_review"] = {
                "objective": frozen["objective"], "apply_policy": frozen["apply_policy"],
                "timeout_minutes": frozen["timeout_minutes"]}
        if metadata:
            result["_meta"] = metadata
        return result

    def open(self, project, operation_id, *, target=None):
        self._scope(project, operation_id, target)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            with _lock(self.operation_lock):
                state, authorization = self._plan_locked(project, operation_id, target)
                token = None if authorization else secrets.token_hex(32)
                card = {
                    "version": 1, "family": self.family, "project": project, "target": target,
                    "operation_id": operation_id, "card_id": secrets.token_hex(16),
                    "plan_digest": state["plan_digest"], "source_digest": self._source(state),
                    "created_at": host_access._utcnow().isoformat(),
                    "expires_at": state["immutable"]["expires_at"],
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "token_digest": _token_digest(token) if token else "0" * 64,
                    "state": "approved" if authorization else "pending"}
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, authorization, token=token)

    def _checked(self, project, operation_id, target, card_id, confirmation_token):
        card = self._load_card(operation_id)
        if (not isinstance(card_id, str) or _ID.fullmatch(card_id) is None
                or not isinstance(confirmation_token, str)
                or _DIGEST.fullmatch(confirmation_token) is None
                or card["project"] != project or card["target"] != target
                or card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _token_digest(confirmation_token))):
            raise HostRuntimeError("OPERATION_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("OPERATION_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("OPERATION_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id() or card["uid"] != os.getuid()):
            raise HostRuntimeError("OPERATION_CARD_STALE")
        with _lock(self.operation_lock):
            state, authorization = self._plan_locked(project, operation_id, target)
            self._same_plan(card, state)
        return card, state, authorization

    def _approve(self, project, operation_id, target, card):
        kwargs = {"plan_digest": card["plan_digest"], "source_digest": card["source_digest"],
                  "confirmation": operation_id}
        if self.family == "registered":
            host_runtime_web.approve_registered(self.operations, project, target,
                                                operation_id=operation_id, **kwargs)
        elif self.family == "runtime":
            host_runtime_web.approve_web(self.operations, operation_id=operation_id, **kwargs)
        else:
            self.operations.approve_web(project, task_id=operation_id, **kwargs)

    def confirm(self, project, operation_id, *, target=None, card_id, confirmation_token):
        self._scope(project, operation_id, target)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, state, authorization = self._checked(
                project, operation_id, target, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            if authorization is None:
                try:
                    self._approve(project, operation_id, target, card)
                except FileExistsError:
                    # A concurrent trusted issuer may have written the same receipt.
                    # The locked recheck below must still prove the exact approval.
                    pass
                except HostRuntimeError as exc:
                    if exc.code not in {"NOT_PENDING_APPROVAL", "STALE_PLAN"}:
                        raise
            with _lock(self.operation_lock):
                state, authorization = self._plan_locked(project, operation_id, target)
                self._same_plan(card, state)
                if authorization is None:
                    raise HostRuntimeError("INVALID_HOST_APPROVAL")
                card["state"] = "approved"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, authorization)

    def cancel(self, project, operation_id, *, target=None, card_id, confirmation_token):
        self._scope(project, operation_id, target)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, _, _ = self._checked(
                project, operation_id, target, card_id, confirmation_token)
            with _lock(self.operation_lock):
                state, authorization = self._plan_locked(project, operation_id, target)
                self._same_plan(card, state)
                if authorization is None:
                    if self.family == "registered":
                        self.operations._transition(
                            state, ("APPROVAL", "HOST_REJECTED", {}), state="rejected",
                            phase="APPROVAL", code="HOST_REJECTED",
                            finished_at=host_access._utcnow().isoformat())
                    else:
                        state.update(state="cancelled", phase="CANCEL", code="USER_CANCELLED",
                                     finished_at=host_access._utcnow().isoformat())
                        if self.family == "agent":
                            state["termination_verified"] = True
                        self.operations._save(state)
                    card["state"] = "cancelled"
                else:
                    card["state"] = "approved"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, authorization)


def tool_specs(projects):
    properties = {
        "project": {"type": "string", "enum": list(projects)},
        "family": {"type": "string", "enum": sorted(FAMILIES)},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "target": {"type": "string", "minLength": 1, "maxLength": 128},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        opener = name == "open_operation_approval"
        execute = name == "confirm_and_execute_operation"
        required = _BASE if opener else _BASE | _CALLBACK
        metadata = {"ui": {"visibility": ["model"] if opener else ["app"]},
                    "openai/widgetAccessible": not opener}
        if opener:
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {
            "name": name, "title": ("작업 승인 카드" if opener else
                "승인하고 실행" if execute else "이 계획 승인" if name.startswith("confirm") else "작업 승인 취소"),
            "description": (
                "Open a user-operated exact-plan card for registered host operations, legacy "
                "runtime plans or isolated agent tasks. Opening never approves or executes. "
                "Use operation_id=task_id for agent; omit target for agent, require it otherwise. "
                "Verified existing approval is reused without another confirmation."
                if opener else "App-only owner callback with a private one-use capability. "
                "Do not ask for the capability in chat. " +
                ("Approves and executes only this exact plan through its existing engine."
                 if execute else "Never executes the operation.")),
            "inputSchema": {"type": "object", "properties": {
                key: properties[key] for key in sorted(required | {"target"})},
                "required": sorted(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": False, "destructiveHint": execute,
                            "idempotentHint": False, "openWorldHint": execute},
            "_meta": metadata}


class OperationApprovalTools:
    names = NAMES

    def __init__(self, projects, *, registered=None, legacy=None, agents=None):
        self.projects = projects
        self.registered = registered
        self.legacy = legacy
        self.agents = agents
        self.managers = {}

    def validate_arguments(self, name, arguments):
        if name not in NAMES or not isinstance(arguments, dict):
            raise HostRuntimeError("INVALID_OPERATION_CARD_ARGUMENTS")
        required = _BASE if name == "open_operation_approval" else _BASE | _CALLBACK
        if not required <= set(arguments) <= required | {"target"}:
            raise HostRuntimeError("INVALID_OPERATION_CARD_ARGUMENTS")
        if (not isinstance(arguments["project"], str) or arguments["project"] not in self.projects
                or not isinstance(arguments["family"], str) or arguments["family"] not in FAMILIES
                or not isinstance(arguments["operation_id"], str)
                or _ID.fullmatch(arguments["operation_id"]) is None):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if arguments["family"] == "agent":
            if "target" in arguments:
                raise HostRuntimeError("INVALID_OPERATION_CARD_ARGUMENTS")
        elif (not isinstance(arguments.get("target"), str)
              or _TARGET.fullmatch(arguments["target"]) is None):
            raise HostRuntimeError("INVALID_OPERATION_CARD_ARGUMENTS")
        if name != "open_operation_approval" and (
                not isinstance(arguments["card_id"], str)
                or _ID.fullmatch(arguments["card_id"]) is None
                or not isinstance(arguments["confirmation_token"], str)
                or _DIGEST.fullmatch(arguments["confirmation_token"]) is None):
            raise HostRuntimeError("OPERATION_CARD_DENIED")

    def _manager(self, family):
        if family not in self.managers:
            wrapper = {"registered": self.registered, "runtime": self.legacy,
                       "agent": self.agents}[family]
            if wrapper is None:
                raise HostRuntimeError("OPERATION_APPROVAL_UNAVAILABLE")
            operations = wrapper if family == "runtime" else wrapper.manager()
            self.managers[family] = OperationApprovals(family, operations)
        return self.managers[family]

    async def _execute(self, family, arguments):
        project, operation_id = arguments["project"], arguments["operation_id"]
        if family == "agent":
            if self.agents is None:
                raise HostRuntimeError("OPERATION_APPROVAL_UNAVAILABLE")
            return await self.agents.call("start_agent_task",
                                          {"project": project, "task_id": operation_id})
        args = {"project": project, "target": arguments["target"], "operation_id": operation_id}
        if family == "registered":
            if self.registered is None:
                raise HostRuntimeError("OPERATION_APPROVAL_UNAVAILABLE")
            return await self.registered.call("execute_host_operation", args)
        if family != "runtime" or self.legacy is None:
            raise HostRuntimeError("OPERATION_APPROVAL_UNAVAILABLE")
        return await self.legacy.execute(**args)

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            family = arguments["family"]
            manager = await asyncio.to_thread(self._manager, family)
            kwargs = {key: value for key, value in arguments.items() if key != "family"}
            action = "confirm" if name == "confirm_and_execute_operation" else name.split("_", 1)[0]
            result = await asyncio.to_thread(getattr(manager, action), **kwargs)
            if name == "confirm_and_execute_operation":
                plan = await self._execute(family, arguments)
                plan.pop("approval_url", None)
                plan.pop("approval_command", None)
                public = result["structuredContent"]
                public.update(plan=plan, supports_direct_execution=False,
                    execution_requested=True, execution_started=plan.get("started_at") is not None,
                    guidance=("The user explicitly approved and requested execution. Observe the "
                              "returned state; do not submit or approve this operation again."))
                if family == "agent":
                    public["next_action"] = {"tool": "get_agent_task", "arguments": {
                        "project": arguments["project"], "task_id": arguments["operation_id"]}}
                else:
                    observe = ("read_host_operation_events" if family == "registered"
                               else "get_host_operation")
                    public["next_action"] = {"tool": observe, "arguments": {
                        key: arguments[key] for key in ("project", "target", "operation_id")}}
                result["content"] = [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}]
            return result
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except ValueError as exc:
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", str(exc)):
                raise
            raise ValueError("OPERATION_CARD_FAILED") from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(json.dumps({"component": "operation-chat-approval", "event": "unexpected-error",
                              "diagnostic_id": diagnostic_id, "operation": name,
                              "exception_type": type(exc).__name__}), file=sys.stderr)
            raise ValueError("OPERATION_CARD_FAILED diagnostic_id=" + diagnostic_id) from None
