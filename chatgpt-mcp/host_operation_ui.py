"""One-use MCP App confirmation for an exact Schema 014 operation plan.

The authenticated MCP host and its component are the trust boundary. The model
sees the frozen plan but never receives the capability. Confirmation delegates
to the existing receipt issuer and never executes the migration.
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


UI_URI = "ui://host-operations/schema014-approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"pending", "spent", "approved", "cancelled"}
_FIELDS = {
    "version", "project", "target", "operation_id", "component", "card_id",
    "plan_digest", "source_digest", "schema014_sha256", "created_at", "expires_at",
    "runtime_identity", "boot_id", "uid", "token_digest", "state",
}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class Schema014Approvals:
    def __init__(self, operations, *, trusted_runtime=None):
        self.operations = operations
        self.cards_dir = private_dir(operations.state_dir / "schema014-cards")
        self.locks_dir = private_dir(operations.state_dir / "schema014-card-locks")
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity() if self._runtime_source is None
                 else self._runtime_source())
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    @staticmethod
    def _scope(project, target, operation_id):
        if project != PROJECT or target != TARGET:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or _ID.fullmatch(operation_id) is None:
            raise HostRuntimeError("SCHEMA014_CARD_DENIED")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _lock_path(self, operation_id):
        return self.locks_dir / (operation_id + ".lock")

    def _load(self, operation_id):
        path = self._path(operation_id)
        if not path.exists() and not path.is_symlink():
            raise HostRuntimeError("SCHEMA014_CARD_DENIED")
        try:
            card = _read_private_json(path)
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["project"] != PROJECT or card["target"] != TARGET
                    or card["operation_id"] != operation_id
                    or card["component"] != "schema014"
                    or card["state"] not in _STATES
                    or type(card["uid"]) is not int
                    or any(not isinstance(card[key], str) or _DIGEST.fullmatch(card[key]) is None
                           for key in ("plan_digest", "source_digest", "schema014_sha256",
                                       "runtime_identity", "token_digest"))
                    or not isinstance(card["card_id"], str)
                    or _ID.fullmatch(card["card_id"]) is None
                    or not isinstance(card["boot_id"], str)):
                raise HostRuntimeError("SCHEMA014_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, KeyError, ValueError, HostRuntimeError):
            raise HostRuntimeError("SCHEMA014_CARD_STATE_INVALID") from None

    def _receipt_exists_locked(self, state):
        path = self.operations.operations_dir / (state["operation_id"] + ".approval.json")
        if not path.exists() and not path.is_symlink():
            return False
        try:
            receipt = _read_private_json(path)
            expected = {
                "operation_id": state["operation_id"],
                "plan_digest": state["plan_digest"],
                "nonce": state["immutable"]["nonce"],
            }
            if (set(receipt) != {*expected, "approved_at"}
                    or any(receipt.get(key) != value for key, value in expected.items())
                    or _timestamp(receipt.get("approved_at")) < _timestamp(state["created_at"])
                    or _timestamp(receipt.get("approved_at")) >
                    host_access._utcnow() + timedelta(minutes=1)):
                raise HostRuntimeError("INVALID_HOST_APPROVAL")
            return True
        except HostRuntimeError:
            raise
        except (OSError, TypeError, ValueError, KeyError):
            raise HostRuntimeError("INVALID_HOST_APPROVAL") from None

    def _plan_locked(self, project, target, operation_id, *, allow_already_approved=False):
        self._scope(project, target, operation_id)
        self.operations._require_trusted_runtime()
        self.operations.runtime.guard(project, target)
        state = self.operations._load(project, target, operation_id)
        immutable = state["immutable"]
        if immutable.get("component") != "schema014":
            raise HostRuntimeError("SCHEMA014_PLAN_REQUIRED")
        if state["state"] in {"approved", "queued", "running", "succeeded", "failed",
                              "outcome_unknown"}:
            raise HostRuntimeError("SCHEMA014_OPERATION_STARTED")
        already_approved = (state["state"] == "pending_host_approval"
                            and self._receipt_exists_locked(state))
        if already_approved and not allow_already_approved:
            raise HostRuntimeError("SCHEMA014_ALREADY_APPROVED")
        if (not already_approved and state["state"] != "pending_host_approval"
                or immutable.get("authorization") != {"mode": "per_operation"}
                or immutable.get("source_review", {}).get("policy") != "reviewed_snapshot"
                or state.get("plan_digest") is None
                or _DIGEST.fullmatch(state["plan_digest"]) is None
                or _DIGEST.fullmatch(immutable.get("source_digest", "")) is None
                or _DIGEST.fullmatch(immutable.get("migration_checksum", "")) is None):
            raise HostRuntimeError("SCHEMA014_PLAN_REQUIRED")
        if _timestamp(immutable["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("SCHEMA014_CARD_EXPIRED")
        self.operations._verify(immutable)
        return state

    def _plan(self, project, target, operation_id, *, allow_already_approved=False):
        with _lock(self.operations.lock_path):
            return self._plan_locked(
                project, target, operation_id,
                allow_already_approved=allow_already_approved)

    def _public(self, card, state, *, approved=None):
        if approved is True and state['state'] != 'pending_host_approval':
            raise HostRuntimeError('SCHEMA014_OPERATION_STARTED')
        immutable = state["immutable"]
        plan = self.operations._result(state)
        # Card responses must not compete with their own user-operated controls.
        # The engine retains these fields for an explicitly requested admin fallback.
        plan.pop("approval_url", None)
        plan.pop("approval_command", None)
        receipt_verified = approved is True
        approval_required = card["state"] == "pending" and approved is None
        public = {
            "project": card["project"], "target": card["target"],
            "operation_id": card["operation_id"], "component": "schema014",
            "card_id": card["card_id"], "state": card["state"],
            "plan_digest": card["plan_digest"], "source_digest": card["source_digest"],
            "schema014_sha256": card["schema014_sha256"],
            "predecessor_checksums": immutable["migration_checksums"],
            "database_generation": immutable["database_generation"],
            "expires_at": card["expires_at"],
            "schema_change": "014_person_relationship_detail.sql",
            "backup_before_change": True, "service_restart": False,
            "executes_on_approval": False,
            "receipt_verified": receipt_verified,
            "approval_required": approval_required,
            "plan": plan,
        }
        if approval_required:
            public["guidance"] = (
                "Show this inline card with its '이 계획 승인' and '취소' buttons. "
                "Do not replace the card with an external approval link or terminal command. "
                "Only the user's card callback can confirm this plan. If the client cannot "
                "display the card, report that limitation and use the authenticated admin "
                "fallback only after the user explicitly requests it. Approval issues a "
                "receipt; it does not execute the migration.")
        if receipt_verified:
            next_action = {
                "tool": "execute_schema014_migration",
                "arguments": {
                    "project": card["project"], "target": card["target"],
                    "operation_id": card["operation_id"],
                },
            }
            # The stored engine state remains pending_host_approval until execution
            # consumes the receipt. Report that state unchanged while making the
            # verified receipt and next user-requested action unambiguous.
            plan.update(approval_required=False, receipt_verified=True,
                        next_action=next_action)
            public["next_action"] = next_action
            public["guidance"] = (
                "The exact Schema 014 plan has a verified one-use approval receipt. "
                "Approval does not execute the migration. Call next_action only if the "
                "user requested actual application; otherwise report the approved plan.")
        if approved is not None:
            public["approved"] = approved
        return public

    def _result(self, card, state, *, token=None, approved=None):
        public = self._public(card, state, approved=approved)
        result = {"content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
                  "structuredContent": public}
        if token is not None:
            result["_meta"] = {"schema014_confirmation": token}
        return result

    def open(self, project, target, operation_id):
        with _lock(self._lock_path(operation_id)):
            with _lock(self.operations.lock_path):
                state = self._plan_locked(
                    project, target, operation_id, allow_already_approved=True)
                immutable = state["immutable"]
                receipt_verified = (
                    state["state"] == "pending_host_approval"
                    and self._receipt_exists_locked(state))
                if state["state"] != "pending_host_approval":
                    raise HostRuntimeError("SCHEMA014_ALREADY_APPROVED")
                token = None if receipt_verified else secrets.token_hex(32)
                card = {
                    "version": 1, "project": project, "target": target,
                    "operation_id": operation_id, "component": "schema014",
                    "card_id": secrets.token_hex(16), "plan_digest": state["plan_digest"],
                    "source_digest": immutable["source_digest"],
                    "schema014_sha256": immutable["migration_checksum"],
                    "created_at": host_access._utcnow().isoformat(),
                    "expires_at": immutable["expires_at"],
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "token_digest": (_digest(token) if token is not None else "0" * 64),
                    "state": "approved" if receipt_verified else "pending",
                }
                _save_private_json(self._path(operation_id), card)
                return self._result(
                    card, state, token=token,
                    approved=True if receipt_verified else None)

    def _checked(self, project, target, operation_id, card_id, confirmation_token, *,
                 allow_already_approved=False):
        self._scope(project, target, operation_id)
        if (not isinstance(card_id, str) or _ID.fullmatch(card_id) is None
                or not isinstance(confirmation_token, str)
                or _DIGEST.fullmatch(confirmation_token) is None):
            raise HostRuntimeError("SCHEMA014_CARD_DENIED")
        card = self._load(operation_id)
        if (card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("SCHEMA014_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("SCHEMA014_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("SCHEMA014_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id()
                or card["uid"] != os.getuid()):
            raise HostRuntimeError("SCHEMA014_CARD_STALE")
        state = self._plan(project, target, operation_id,
                           allow_already_approved=allow_already_approved)
        immutable = state["immutable"]
        if (card["plan_digest"] != state["plan_digest"]
                or card["source_digest"] != immutable["source_digest"]
                or card["schema014_sha256"] != immutable["migration_checksum"]
                or card["expires_at"] != immutable["expires_at"]):
            raise HostRuntimeError("SCHEMA014_CARD_STALE")
        return card, state

    def confirm(self, project, target, operation_id, *, card_id, confirmation_token):
        with _lock(self._lock_path(operation_id)):
            card, state = self._checked(
                project, target, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            try:
                receipt = host_runtime_web.approve_web(
                    self.operations, operation_id=operation_id,
                    plan_digest=card["plan_digest"], source_digest=card["source_digest"],
                    confirmation=operation_id)
            except FileExistsError:
                # A same-plan authenticated admin approval may win after the
                # capability is spent but before this issuer enters its lock.
                # Wait on that lock, then accept only its complete valid receipt.
                with _lock(self.operations.lock_path):
                    latest = self.operations._load(project, target, operation_id)
                    if (latest["plan_digest"] != card["plan_digest"]
                            or latest["immutable"]["source_digest"] != card["source_digest"]
                            or not self._receipt_exists_locked(latest)):
                        raise HostRuntimeError("INVALID_HOST_APPROVAL") from None
                receipt = {"approved": True}
            card["state"] = "approved"
            _save_private_json(self._path(operation_id), card)
            return self._result(card, state, approved=receipt["approved"])

    def cancel(self, project, target, operation_id, *, card_id, confirmation_token):
        with _lock(self._lock_path(operation_id)):
            card, state = self._checked(
                project, target, operation_id, card_id, confirmation_token,
                allow_already_approved=True)
            with _lock(self.operations.lock_path):
                latest = self.operations._load(project, target, operation_id)
                if (latest["plan_digest"] != card["plan_digest"]
                        or latest["immutable"]["source_digest"] != card["source_digest"]):
                    raise HostRuntimeError("SCHEMA014_CARD_STALE")
                if latest["state"] != "pending_host_approval":
                    raise HostRuntimeError("SCHEMA014_OPERATION_STARTED")
                if self._receipt_exists_locked(latest):
                    card["state"] = "approved"
                    _save_private_json(self._path(operation_id), card)
                    return self._result(card, latest, approved=True)
                if latest["state"] != "pending_host_approval":
                    raise HostRuntimeError("SCHEMA014_CARD_STALE")
                latest.update(state="rejected", phase="APPROVAL", code="USER_CANCELLED",
                              finished_at=host_access._utcnow().isoformat())
                self.operations._save(latest)
                card["state"] = "cancelled"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, latest, approved=False)


NAMES = frozenset({"open_schema014_approval", "confirm_schema014_approval",
                   "cancel_schema014_approval"})
_REQUIRED = {
    "open_schema014_approval": {"project", "target", "operation_id"},
    "confirm_schema014_approval": {
        "project", "target", "operation_id", "card_id", "confirmation_token"},
    "cancel_schema014_approval": {
        "project", "target", "operation_id", "card_id", "confirmation_token"},
}


def tool_specs():
    properties = {
        "project": {"type": "string", "enum": [PROJECT]},
        "target": {"type": "string", "enum": [TARGET]},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    titles = {"open": "Schema 014 승인 카드", "confirm": "Schema 014 계획 승인",
              "cancel": "Schema 014 승인 취소"}
    for name in sorted(NAMES):
        action = name.split("_", 1)[0]
        opener = action == "open"
        metadata = {
            "ui": {"visibility": ["model"] if opener else ["app"]},
            "openai/widgetAccessible": not opener,
            "openai/toolInvocation/invoking": titles[action] + " 중입니다",
            "openai/toolInvocation/invoked": titles[action] + " 응답을 받았습니다",
        }
        if opener:
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {
            "name": name, "title": titles[action],
            "description": (
                "Open the exact existing Schema 014 plan in a user confirmation card. "
                "Opening does not approve or execute it. Reopening after a receipt was "
                "issued returns the verified approval and exact next action without a new "
                "capability or approval request."
                if opener else
                "App-only callback requiring the hidden one-use Schema 014 capability."),
            "inputSchema": {"type": "object", "properties": {
                key: properties[key] for key in sorted(_REQUIRED[name])},
                "required": sorted(_REQUIRED[name]), "additionalProperties": False},
            "annotations": {"readOnlyHint": False,
                            "destructiveHint": action == "confirm",
                            "idempotentHint": False, "openWorldHint": True},
            "_meta": metadata,
        }


class Schema014ApprovalTools:
    def __init__(self, operations):
        self.names = NAMES
        self.operations = operations
        self.manager = None

    @staticmethod
    def validate_arguments(name, arguments):
        required = _REQUIRED.get(name)
        if (required is None or not isinstance(arguments, dict)
                or set(arguments) != required):
            raise HostRuntimeError("INVALID_SCHEMA014_CARD_ARGUMENTS")
        Schema014Approvals._scope(
            arguments["project"], arguments["target"], arguments["operation_id"])
        if name != "open_schema014_approval":
            if (not isinstance(arguments["card_id"], str)
                    or _ID.fullmatch(arguments["card_id"]) is None
                    or not isinstance(arguments["confirmation_token"], str)
                    or _DIGEST.fullmatch(arguments["confirmation_token"]) is None):
                raise HostRuntimeError("SCHEMA014_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self.manager is None:
                self.manager = Schema014Approvals(self.operations)
            action = name.split("_", 1)[0]
            return await asyncio.to_thread(getattr(self.manager, action), **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(json.dumps({"component": "schema014-chat-approval",
                              "event": "unexpected-error", "diagnostic_id": diagnostic_id,
                              "operation": name, "exception_type": type(exc).__name__,
                              "errno": exc.errno if isinstance(exc, OSError) else None}),
                  file=sys.stderr, flush=True)
            raise ValueError("SCHEMA014_CARD_UNAVAILABLE:" + diagnostic_id) from None
