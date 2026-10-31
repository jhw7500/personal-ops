"""Exact-plan chat confirmation for registered host operations.

A host-owned, app-only capability authorizes an already prepared immutable plan.
The existing registered approval issuer still verifies registry, source, target,
preflight and produces a one-use receipt. This never starts an action.
"""
from __future__ import annotations

import asyncio
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

UI_URI = "ui://host-operations/registered-approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_FIELDS = {
    "version", "project", "target", "operation_id", "card_id",
    "action", "plan_digest", "source_digest", "config_digest",
    "executor_digest", "created_at", "expires_at", "runtime_identity",
    "boot_id", "uid", "token_digest", "state",
}
_STATES = {"pending", "spent", "approved", "cancelled"}
NAMES = frozenset({
    "open_registered_host_approval",
    "confirm_registered_host_approval",
    "cancel_registered_host_approval",
})
_REQUIRED = {
    n: ({"project", "target", "operation_id"} if n.startswith("open_")
        else {"project", "target", "operation_id", "card_id", "confirmation_token"})
    for n in NAMES
}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class RegisteredHostApprovals:
    def __init__(self, operations, *, trusted_runtime=None):
        self.operations = operations
        self.cards_dir = private_dir(operations.registered_dir / "approval-cards")
        self.locks_dir = private_dir(operations.registered_dir / "approval-card-locks")
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity() if self._runtime_source is None
                 else self._runtime_source())
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    def _scope(self, project, target, operation_id):
        if not isinstance(project, str) or project not in self.operations.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise HostRuntimeError("REGISTERED_CARD_DENIED")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _lock_path(self, operation_id):
        return self.locks_dir / (operation_id + ".lock")

    def _load(self, operation_id):
        try:
            card = _read_private_json(self._path(operation_id))
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["project"] not in self.operations.projects
                    or not isinstance(card["target"], str)
                    or not _TARGET.fullmatch(card["target"])
                    or card["operation_id"] != operation_id
                    or card["state"] not in _STATES
                    or type(card["uid"]) is not int
                    or not isinstance(card["action"], str)
                    or not isinstance(card["boot_id"], str)
                    or not isinstance(card["card_id"], str) or not _ID.fullmatch(card["card_id"])
                    or any(not isinstance(card[key], str)
                           or not _DIGEST.fullmatch(card[key]) for key in (
                               "plan_digest", "source_digest", "config_digest",
                               "executor_digest", "runtime_identity", "token_digest"))):
                raise HostRuntimeError("REGISTERED_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, KeyError, ValueError, TypeError, HostRuntimeError):
            raise HostRuntimeError("REGISTERED_CARD_STATE_INVALID") from None

    def _plan_locked(self, project, target, operation_id):
        self._scope(project, target, operation_id)
        self.operations._require_trusted_runtime()
        self.operations.registry.reload().require_target(project, target)
        state = self.operations._refresh(self.operations._load(project, target, operation_id))
        if state["state"] not in ("pending_host_approval", "approved"):
            raise HostRuntimeError("REGISTERED_CARD_NOT_PENDING")
        if _timestamp(state["immutable"]["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("REGISTERED_CARD_EXPIRED")
        if state["state"] == "approved":
            self.operations._check_receipt(state)
        else:
            self.operations._verify(state)
        return state

    def _public(self, card, state, *, approved=None):
        payload = self.operations._public(state)
        payload.pop("approval_url", None)
        imm = state["immutable"]
        receipt_verified = approved is True or state["state"] == "approved"
        required = card["state"] == "pending" and approved is None
        result = {
            "project": card["project"], "target": card["target"],
            "operation_id": card["operation_id"], "card_id": card["card_id"],
            "component": "registered_host", "action": card["action"],
            "parameters": imm["parameters"], "impact": imm["impact"],
            "state": card["state"],
            "plan_digest": card["plan_digest"], "source_digest": card["source_digest"],
            "config_digest": card["config_digest"],
            "executor_digest": card["executor_digest"],
            "expires_at": card["expires_at"],
            "source_review": imm["source"]["review_policy"],
            "changed_path_count": len(imm["source"]["changed_paths"]),
            "high_risk_changes": imm["source"]["high_risk_changes"],
            "executes_on_approval": False,
            "receipt_verified": receipt_verified, "approval_required": required,
            "plan": payload,
        }
        if required:
            result["guidance"] = (
                "Display the exact operation in an in-chat card. The hidden app "
                "confirmation issues a one-use receipt but never runs the action."
            )
        if receipt_verified:
            action = {
                "tool": "execute_host_operation",
                "arguments": {
                    "project": card["project"], "target": card["target"],
                    "operation_id": card["operation_id"],
                },
            }
            result["next_action"] = action
            result["plan"].update(approval_required=False, receipt_verified=True,
                                  next_action=action)
        if approved is not None:
            result["approved"] = approved
        return result

    def _result(self, card, state, *, token=None, approved=None):
        public = self._public(card, state, approved=approved)
        result = {
            "content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
            "structuredContent": public,
        }
        if token is not None:
            result["_meta"] = {"registered_host_confirmation": token}
        return result

    def open(self, project, target, operation_id):
        self._scope(project, target, operation_id)
        with _lock(self._lock_path(operation_id)):
            with _lock(self.operations.lock_path):
                state = self._plan_locked(project, target, operation_id)
                imm = state["immutable"]
                approved = state["state"] == "approved"
                token = None if approved else secrets.token_hex(32)
                card = {
                    "version": 1, "project": project, "target": target,
                    "operation_id": operation_id, "card_id": secrets.token_hex(16),
                    "action": imm["action"], "plan_digest": state["plan_digest"],
                    "source_digest": imm["source"]["source_digest"],
                    "config_digest": imm["config_digest"],
                    "executor_digest": imm["executor_digest"],
                    "created_at": host_access._utcnow().isoformat(),
                    "expires_at": imm["expires_at"],
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "token_digest": _digest(token) if token is not None else "0" * 64,
                    "state": "approved" if approved else "pending",
                }
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, token=token,
                                    approved=True if approved else None)

    def _checked(self, project, target, operation_id, card_id, confirmation_token):
        self._scope(project, target, operation_id)
        if (not isinstance(card_id, str) or not _ID.fullmatch(card_id)
                or not isinstance(confirmation_token, str)
                or not _DIGEST.fullmatch(confirmation_token)):
            raise HostRuntimeError("REGISTERED_CARD_DENIED")
        card = self._load(operation_id)
        if (card["project"] != project or card["target"] != target
                or card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("REGISTERED_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("REGISTERED_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("REGISTERED_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id()
                or card["uid"] != os.getuid()):
            raise HostRuntimeError("REGISTERED_CARD_STALE")
        with _lock(self.operations.lock_path):
            state = self._plan_locked(project, target, operation_id)
            imm = state["immutable"]
            if (card["plan_digest"] != state["plan_digest"]
                    or card["source_digest"] != imm["source"]["source_digest"]
                    or card["config_digest"] != imm["config_digest"]
                    or card["executor_digest"] != imm["executor_digest"]
                    or card["action"] != imm["action"]
                    or card["expires_at"] != imm["expires_at"]):
                raise HostRuntimeError("REGISTERED_CARD_STALE")
        return card, state

    def confirm(self, project, target, operation_id, *, card_id, confirmation_token):
        self._scope(project, target, operation_id)
        with _lock(self._lock_path(operation_id)):
            card, _ = self._checked(project, target, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            try:
                self.operations.approve_web(
                    project, target, operation_id=operation_id,
                    plan_digest=card["plan_digest"], source_digest=card["source_digest"],
                    confirmation=operation_id)
            except HostRuntimeError as exc:
                # An owner-admin receipt may race the callback, but may not be faked.
                if exc.code != "NOT_PENDING_APPROVAL":
                    raise
                with _lock(self.operations.lock_path):
                    current = self._plan_locked(project, target, operation_id)
                    if current["state"] != "approved":
                        raise
            with _lock(self.operations.lock_path):
                current = self._plan_locked(project, target, operation_id)
            card["state"] = "approved"
            _save_private_json(self._path(operation_id), card)
            return self._result(card, current, approved=True)

    def cancel(self, project, target, operation_id, *, card_id, confirmation_token):
        self._scope(project, target, operation_id)
        with _lock(self._lock_path(operation_id)):
            card, _ = self._checked(project, target, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            with _lock(self.operations.lock_path):
                current = self._plan_locked(project, target, operation_id)
            if current["state"] == "approved":
                card["state"] = "approved"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, current, approved=True)
            self.operations.reject_web(
                project, target, operation_id=operation_id, confirmation=operation_id)
            card["state"] = "cancelled"
            _save_private_json(self._path(operation_id), card)
            # Return the pre-rejection pinned details without pretending success.
            return self._result(card, current, approved=False)


def tool_specs(projects):
    fields = {
        "project": {"type": "string", "enum": sorted(projects)},
        "target": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,62}$"},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        action = name.split("_", 1)[0]
        opener = action == "open"
        meta = {
            "ui": {"visibility": ["model"] if opener else ["app"]},
            "openai/widgetAccessible": not opener,
            "openai/toolInvocation/invoking": "등록 호스트 계획 확인 중",
            "openai/toolInvocation/invoked": "등록 호스트 계획 응답을 받았습니다",
        }
        if opener:
            meta["ui"]["resourceUri"] = UI_URI
            meta["openai/outputTemplate"] = UI_URI
        yield {
            "name": name,
            "title": "등록 호스트 작업 승인" if opener else "등록 호스트 승인 응답",
            "description": (
                "Open the exact registered host action in an in-chat one-use "
                "confirmation card. Opening and confirming never executes it."
                if opener else
                "App-only callback using the hidden registered-host confirmation."),
            "inputSchema": {
                "type": "object",
                "properties": {key: fields[key] for key in sorted(_REQUIRED[name])},
                "required": sorted(_REQUIRED[name]),
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False, "destructiveHint": action == "confirm",
                "idempotentHint": False, "openWorldHint": False,
            },
            "_meta": meta,
        }


class RegisteredHostApprovalTools:
    def __init__(self, registered_tools):
        self.registered_tools = registered_tools
        self.names = NAMES
        self.manager = None

    def validate_arguments(self, name, args):
        required = _REQUIRED.get(name)
        if required is None or not isinstance(args, dict) or set(args) != required:
            raise HostRuntimeError("INVALID_REGISTERED_CARD_ARGUMENTS")
        if (args.get("project") not in self.registered_tools.projects
                or not isinstance(args.get("target"), str)
                or not _TARGET.fullmatch(args["target"])):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if (not isinstance(args.get("operation_id"), str)
                or not _ID.fullmatch(args["operation_id"])):
            raise HostRuntimeError("REGISTERED_CARD_DENIED")
        if name != "open_registered_host_approval":
            if (not isinstance(args.get("card_id"), str)
                    or not _ID.fullmatch(args["card_id"])
                    or not isinstance(args.get("confirmation_token"), str)
                    or not _DIGEST.fullmatch(args["confirmation_token"])):
                raise HostRuntimeError("REGISTERED_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self.manager is None:
                self.manager = RegisteredHostApprovals(self.registered_tools.manager())
            action = name.split("_", 1)[0]
            return await asyncio.to_thread(getattr(self.manager, action), **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            identifier = uuid.uuid4().hex
            print(json.dumps({
                "component": "registered-host-chat",
                "event": "unexpected-error", "operation": name,
                "diagnostic_id": identifier, "exception_type": type(exc).__name__,
                "errno": exc.errno if isinstance(exc, OSError) else None,
            }), file=sys.stderr, flush=True)
            raise ValueError("REGISTERED_CARD_UNAVAILABLE:" + identifier) from None
