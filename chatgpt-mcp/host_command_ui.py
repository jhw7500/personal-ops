"""App-only confirmation and explicit execution of registered command plans.

The authenticated MCP host keeps the capability in component-only metadata.
The existing command receipt issuer and execution checks remain authoritative.
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
from host_runtime_config import HostRuntimeError, private_dir
from host_runtime_operations import _lock, _read_private_json, _save_private_json, _timestamp


UI_URI = "ui://host-commands/approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"version", "project", "operation_id", "card_id", "plan_digest", "source_digest",
           "created_at", "expires_at", "runtime_identity", "boot_id", "uid",
           "token_digest", "state"}
NAMES = frozenset({"open_host_command_approval", "confirm_host_command_approval",
                   "confirm_and_execute_host_command", "cancel_host_command_approval"})
_REQUIRED = {
    "open_host_command_approval": {"project", "operation_id"},
    "confirm_host_command_approval": {"project", "operation_id", "card_id", "confirmation_token"},
    "confirm_and_execute_host_command": {"project", "operation_id", "card_id", "confirmation_token"},
    "cancel_host_command_approval": {"project", "operation_id", "card_id", "confirmation_token"},
}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class HostCommandApprovals:
    def __init__(self, operations, *, trusted_runtime=None):
        self.operations = operations
        self.cards_dir = private_dir(operations.state_dir / "approval-cards")
        self.locks_dir = private_dir(operations.state_dir / "approval-card-locks")
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity() if self._runtime_source is None
                 else self._runtime_source())
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    def _scope(self, project, operation_id):
        if not isinstance(project, str) or project not in self.operations.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or _ID.fullmatch(operation_id) is None:
            raise HostRuntimeError("HOST_COMMAND_CARD_DENIED")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _load(self, operation_id):
        try:
            card = _read_private_json(self._path(operation_id))
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["project"] not in self.operations.projects
                    or card["operation_id"] != operation_id
                    or card["state"] not in {"pending", "spent", "approved", "cancelled"}
                    or type(card["uid"]) is not int
                    or not isinstance(card["card_id"], str)
                    or _ID.fullmatch(card["card_id"]) is None
                    or not isinstance(card["boot_id"], str)
                    or any(not isinstance(card[key], str) or _DIGEST.fullmatch(card[key]) is None
                           for key in ("plan_digest", "source_digest", "runtime_identity", "token_digest"))):
                raise HostRuntimeError("HOST_COMMAND_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, ValueError, KeyError, HostRuntimeError):
            raise HostRuntimeError("HOST_COMMAND_CARD_STATE_INVALID") from None

    def _plan_locked(self, project, operation_id):
        self.operations._require_trusted()
        state = self.operations._load(operation_id, project)
        if state["state"] in {"pending_host_approval", "approved"}:
            state = self.operations._refresh(state)
        if state["state"] == "expired":
            raise HostRuntimeError("PLAN_EXPIRED")
        if state["state"] not in {"pending_host_approval", "approved"}:
            raise HostRuntimeError("HOST_COMMAND_OPERATION_STARTED")
        self.operations.profiles.verify_frozen(state["frozen"])
        if state["state"] == "approved":
            self.operations._receipt(state)
            approved_at = _timestamp(state["approved_at"])
            if (approved_at < _timestamp(state["created_at"])
                    or approved_at > host_access._utcnow() + timedelta(minutes=1)):
                raise HostRuntimeError("INVALID_HOST_COMMAND_APPROVAL")
        return state

    def _public(self, card, state, *, approved=None):
        plan = self.operations._public(state)
        plan.pop("approval_url", None)
        frozen = state["frozen"]
        receipt_verified = approved is True
        public = {
            "project": card["project"], "operation_id": card["operation_id"],
            "component": "host_command", "card_id": card["card_id"], "state": card["state"],
            "profile_id": frozen["profile_id"], "label": frozen["label"], "effect": frozen["effect"],
            "plan_digest": card["plan_digest"], "source_digest": card["source_digest"],
            "executable_sha256": frozen["executable_sha256"],
            "input_count": len(frozen["inputs"]), "timeout_seconds": frozen["timeout_seconds"],
            "expires_at": card["expires_at"], "executes_on_approval": False,
            "supports_direct_execution": card["state"] == "pending",
            "receipt_verified": receipt_verified,
            "approval_required": card["state"] == "pending" and approved is None,
            "plan": plan,
            "guidance": (
                "Show this inline card's '승인하고 실행' and '취소' buttons. Do not substitute an "
                "external approval link or ask for an admin token. Only the user's app callback "
                "can confirm. The explicit approve-and-execute action issues one exact-plan "
                "receipt and immediately submits that command. Legacy approval-only callbacks "
                "do not execute. If the client cannot show the card, report that limitation; use the "
                "authenticated admin fallback only if the user explicitly requests it."),
        }
        if approved is not None:
            public["approved"] = approved
        if receipt_verified:
            public["next_action"] = {"tool": "execute_host_command", "arguments": {
                "project": card["project"], "operation_id": card["operation_id"]}}
            plan.update(receipt_verified=True, approval_required=False,
                        next_action=public["next_action"])
            public["guidance"] = (
                "The exact command plan has a verified one-use receipt. Do not request another "
                "approval. Call next_action only if the user requested execution; approval "
                "alone does not start a build, upload, invitation or other command.")
        return public

    def _result(self, card, state, *, token=None, approved=None):
        public = self._public(card, state, approved=approved)
        result = {"content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
                  "structuredContent": public}
        if token is not None:
            result["_meta"] = {"host_command_confirmation": token}
        return result

    def open(self, project, operation_id):
        self._scope(project, operation_id)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            with _lock(self.operations.lock_path):
                state = self._plan_locked(project, operation_id)
                approved = state["state"] == "approved"
                token = None if approved else secrets.token_hex(32)
                card = {
                    "version": 1, "project": project, "operation_id": operation_id,
                    "card_id": secrets.token_hex(16), "plan_digest": state["plan_digest"],
                    "source_digest": state["source_digest"],
                    "created_at": host_access._utcnow().isoformat(), "expires_at": state["expires_at"],
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "token_digest": _digest(token) if token else "0" * 64,
                    "state": "approved" if approved else "pending",
                }
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, token=token, approved=True if approved else None)

    def _checked(self, project, operation_id, card_id, confirmation_token):
        card = self._load(operation_id)
        if (not isinstance(card_id, str) or _ID.fullmatch(card_id) is None
                or not isinstance(confirmation_token, str)
                or _DIGEST.fullmatch(confirmation_token) is None
                or card["project"] != project or card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("HOST_COMMAND_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("HOST_COMMAND_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("HOST_COMMAND_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id() or card["uid"] != os.getuid()):
            raise HostRuntimeError("HOST_COMMAND_CARD_STALE")
        with _lock(self.operations.lock_path):
            state = self._plan_locked(project, operation_id)
            self._same_plan(card, state)
        return card, state

    @staticmethod
    def _same_plan(card, state):
        if any(card[key] != state[key] for key in ("plan_digest", "source_digest", "expires_at")):
            raise HostRuntimeError("HOST_COMMAND_CARD_STALE")

    def confirm(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, state = self._checked(project, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            if state["state"] != "approved":
                try:
                    self.operations.approve_web(project, operation_id=operation_id,
                        plan_digest=card["plan_digest"], source_digest=card["source_digest"],
                        confirmation=operation_id)
                except HostRuntimeError as exc:
                    if exc.code != "NOT_PENDING_APPROVAL":
                        raise
            with _lock(self.operations.lock_path):
                state = self._plan_locked(project, operation_id)
                self._same_plan(card, state)
                if state["state"] != "approved":
                    raise HostRuntimeError("INVALID_HOST_COMMAND_APPROVAL")
                card["state"] = "approved"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, approved=True)

    def confirm_and_execute(self, project, operation_id, *, card_id, confirmation_token):
        confirmed = self.confirm(project, operation_id, card_id=card_id,
                                 confirmation_token=confirmation_token)
        # Reuse the receipt-consuming, locked enqueue path. Never issue a second
        # model tool call, extend a deadline, or launch outside that engine.
        plan = self.operations._begin(project, operation_id)
        plan.pop("approval_url", None)
        public = confirmed["structuredContent"]
        public.update(plan=plan, supports_direct_execution=False,
                      execution_requested=True,
                      execution_started=plan.get("started_at") is not None,
                      next_action={"tool": "get_host_command", "arguments": {
                          "project": project, "operation_id": operation_id}},
                      guidance=("The user explicitly approved and requested execution. "
                                "Read the returned plan state and get_host_command; "
                                "do not call execute_host_command again. An expired plan "
                                "was not started and needs a new plan."))
        confirmed["content"] = [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}]
        return confirmed

    def cancel(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, _ = self._checked(project, operation_id, card_id, confirmation_token)
            with _lock(self.operations.lock_path):
                state = self._plan_locked(project, operation_id)
                self._same_plan(card, state)
                if state["state"] == "approved":
                    card["state"] = "approved"
                    _save_private_json(self._path(operation_id), card)
                    return self._result(card, state, approved=True)
                state.update(state="cancelled", phase="APPROVAL", code="USER_CANCELLED",
                             finished_at=host_access._utcnow().isoformat())
                self.operations._save(state)
                card["state"] = "cancelled"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, approved=False)


def tool_specs(projects):
    properties = {
        "project": {"type": "string", "enum": list(projects)},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        opener = name.startswith("open_")
        execute = name == "confirm_and_execute_host_command"
        title = ("호스트 명령 승인 카드" if opener else
                 "호스트 명령 승인하고 실행" if execute else
                 "호스트 명령 계획 승인" if name.startswith("confirm_") else "호스트 명령 승인 취소")
        metadata = {"ui": {"visibility": ["model"] if opener else ["app"]},
                    "openai/widgetAccessible": not opener}
        if opener:
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {"name": name, "title": title,
               "description": ("Open the exact registered command plan in a user confirmation card. "
                   "Opening never approves or executes. An already approved plan returns its "
                   "verified receipt and next action without another approval."
                   if opener else
                   "App-only explicit approve-and-execute action using the hidden exact-plan "
                   "capability. Approve and submit this one pinned command in the same callback; "
                   "read the returned state instead of calling execute_host_command again. "
                   "Never request the capability in chat."
                   if execute else "App-only callback using the hidden one-use exact-plan capability. "
                   "Never execute a command. Never request the capability in chat."),
               "inputSchema": {"type": "object", "properties": {
                   key: properties[key] for key in sorted(_REQUIRED[name])},
                   "required": sorted(_REQUIRED[name]), "additionalProperties": False},
               "annotations": {"readOnlyHint": False, "destructiveHint": execute,
                               "idempotentHint": False, "openWorldHint": execute},
               "_meta": metadata}


class HostCommandApprovalTools:
    names = NAMES

    def __init__(self, commands):
        self.commands = commands
        self.manager = None

    def validate_arguments(self, name, arguments):
        if name not in NAMES or not isinstance(arguments, dict) or set(arguments) != _REQUIRED[name]:
            raise HostRuntimeError("INVALID_HOST_COMMAND_CARD_ARGUMENTS")
        if not isinstance(arguments["project"], str) or arguments["project"] not in self.commands.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(arguments["operation_id"], str) or _ID.fullmatch(arguments["operation_id"]) is None:
            raise HostRuntimeError("HOST_COMMAND_CARD_DENIED")
        if not name.startswith("open_"):
            if (not isinstance(arguments["card_id"], str) or _ID.fullmatch(arguments["card_id"]) is None
                    or not isinstance(arguments["confirmation_token"], str)
                    or _DIGEST.fullmatch(arguments["confirmation_token"]) is None):
                raise HostRuntimeError("HOST_COMMAND_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self.manager is None:
                self.manager = HostCommandApprovals(self.commands.manager())
            action = ("confirm_and_execute" if name == "confirm_and_execute_host_command"
                      else name.split("_", 1)[0])
            return await asyncio.to_thread(getattr(self.manager, action), **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(json.dumps({"component": "host-command-chat-approval", "event": "unexpected-error",
                              "diagnostic_id": diagnostic_id, "operation": name,
                              "exception_type": type(exc).__name__,
                              "errno": exc.errno if isinstance(exc, OSError) else None}),
                  file=sys.stderr, flush=True)
            raise ValueError("HOST_COMMAND_CARD_UNAVAILABLE:" + diagnostic_id) from None
