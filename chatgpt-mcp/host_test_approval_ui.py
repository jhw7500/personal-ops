"""MCP App one-use approval of frozen TEST API/Worker and 001-013 plans.

No approval authority is granted by the model or host grant. The component
receives a hidden nonce; confirmation delegates to the existing immutable
receipt issuer. Opening/confirming never builds, migrates or restarts anything.
Schema 014 retains its separately reviewed dedicated approval card.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
import uuid

import host_access
from host_operation_ui import Schema014Approvals, _ID, _DIGEST, _digest
from host_runtime_config import HostRuntimeError, PROJECT, TARGET, private_dir
from host_runtime_operations import _lock, _read_private_json, _save_private_json, _timestamp

LEGACY_UI_URI = "ui://host-operations/test-plan-approval-v1.html"
UI_URI = "ui://host-operations/test-plan-approval-v2.html"
COMPONENTS = frozenset({"api", "worker", "migration", "predecessor_migration", "schema_update", "schema015016"})
_STATES = frozenset({"pending", "spent", "approved", "cancelled"})
_FIELDS = frozenset({
    "version", "project", "target", "operation_id", "component", "mode",
    "card_id", "plan_digest", "source_digest", "created_at", "expires_at",
    "runtime_identity", "boot_id", "uid", "token_digest", "state",
})
_NEXT_TOOLS = {
    "api": "execute_test_deploy",
    "worker": "execute_test_deploy",
    "migration": "execute_db_migration",
    "predecessor_migration": "execute_predecessor_migration",
    "schema_update": "execute_db_migration",
    "schema015016": "execute_schema015016_migration",
}
NAMES = frozenset({
    "open_test_plan_approval",
    "confirm_test_plan_approval",
    "cancel_test_plan_approval",
})
_REQUIRED = {
    name: ({"project", "target", "operation_id"}
           if name.startswith("open_")
           else {"project", "target", "operation_id", "card_id", "confirmation_token"})
    for name in NAMES
}


class TestPlanApprovals(Schema014Approvals):
    """Reuse the reviewed issuer and cancellation's cross-process locking."""

    def __init__(self, operations, *, trusted_runtime=None):
        super().__init__(operations, trusted_runtime=trusted_runtime)
        self.cards_dir = private_dir(operations.state_dir / "test-plan-cards")
        self.locks_dir = private_dir(operations.state_dir / "test-plan-card-locks")

    @staticmethod
    def _scope(project, target, operation_id):
        if project != PROJECT or target != TARGET:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise HostRuntimeError("TEST_PLAN_CARD_DENIED")

    def _load(self, operation_id):
        path = self._path(operation_id)
        try:
            card = _read_private_json(path)
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["project"] != PROJECT or card["target"] != TARGET
                    or card["operation_id"] != operation_id
                    or card["component"] not in COMPONENTS
                    or card["mode"] not in ("disabled", "toss-demo")
                    or card["state"] not in _STATES
                    or type(card["uid"]) is not int
                    or any(not isinstance(card[key], str)
                           or not _DIGEST.fullmatch(card[key]) for key in (
                               "plan_digest", "source_digest", "runtime_identity",
                               "token_digest"))
                    or not isinstance(card["card_id"], str)
                    or not _ID.fullmatch(card["card_id"])
                    or not isinstance(card["boot_id"], str)):
                raise HostRuntimeError("TEST_PLAN_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, KeyError, ValueError, HostRuntimeError):
            raise HostRuntimeError("TEST_PLAN_CARD_STATE_INVALID") from None

    def _plan_locked(self, project, target, operation_id, *, allow_already_approved=False):
        self._scope(project, target, operation_id)
        self.operations._require_trusted_runtime()
        self.operations.runtime.guard(project, target)
        state = self.operations._load(project, target, operation_id)
        immutable = state["immutable"]
        if (immutable.get("component") not in COMPONENTS
                or immutable.get("operation_id") != operation_id
                or immutable.get("authorization", {"mode": "per_operation"})
                   != {"mode": "per_operation"}):
            raise HostRuntimeError("TEST_PLAN_CARD_PLAN_REQUIRED")
        if state["state"] != "pending_host_approval":
            raise HostRuntimeError("TEST_PLAN_CARD_NOT_PENDING")
        receipt = self._receipt_exists_locked(state)
        if receipt and not allow_already_approved:
            raise HostRuntimeError("TEST_PLAN_CARD_ALREADY_APPROVED")
        if (not isinstance(state.get("plan_digest"), str)
                or not _DIGEST.fullmatch(state["plan_digest"])
                or not isinstance(immutable.get("source_digest"), str)
                or not _DIGEST.fullmatch(immutable["source_digest"])
                or not isinstance(immutable.get("expires_at"), str)):
            raise HostRuntimeError("TEST_PLAN_CARD_PLAN_REQUIRED")
        if _timestamp(immutable["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("TEST_PLAN_CARD_EXPIRED")
        # Source/Compose/target identity checks are not skipped for cards.
        self.operations._verify(immutable)
        return state

    def _public(self, card, state, *, approved=None):
        immutable = state["immutable"]
        plan = self.operations._result(state)
        plan.pop("approval_url", None)
        plan.pop("approval_command", None)
        plan.pop("changed_paths", None)  # avoid returning hundreds of source paths
        receipt_verified = approved is True or card["state"] == "approved"
        required = card["state"] == "pending" and approved is None
        public = {
            "project": card["project"], "target": card["target"],
            "operation_id": card["operation_id"], "card_id": card["card_id"],
            "component": card["component"], "mode": card["mode"],
            "state": card["state"], "plan_digest": card["plan_digest"],
            "source_digest": card["source_digest"],
            "expires_at": card["expires_at"],
            "impact": plan.get("impact"),
            "high_risk_changes": immutable.get("high_risk_changes", []),
            "changed_path_count": len(immutable.get("changed_paths", [])),
            "source_review": plan.get("source_review"),
            "executes_on_approval": False,
            "approval_required": required, "receipt_verified": receipt_verified,
            "plan": plan,
        }
        if required:
            public["guidance"] = (
                "Show the exact in-chat confirmation card with its own Approve/Cancel buttons. "
                "Only the user's app callback can approve, and approval issues one receipt "
                "without executing. Do not substitute a host grant, an assertion, or a web link."
            )
        if receipt_verified:
            next_action = {
                "tool": _NEXT_TOOLS[card["component"]],
                "arguments": {
                    "project": PROJECT, "target": TARGET, "operation_id": card["operation_id"]
                },
            }
            plan.update(approval_required=False, receipt_verified=True,
                        next_action=next_action)
            public["next_action"] = next_action
            public["guidance"] = (
                "One-use receipt verified; no execution has started. The user may "
                "withdraw this unused approval through the fresh in-chat cancellation "
                "button. Never execute or revoke it automatically. Use next_action "
                "only if the user explicitly requested this exact execution."
            )
        if approved is not None:
            public["approved"] = approved
        return public

    def _result(self, card, state, *, token=None, approved=None):
        public = self._public(card, state, approved=approved)
        # Only the actual MCP App receives this fresh, one-use cancellation token.
        # An approved-but-unexecuted plan remains cancellable without reapproving it.
        public["cancellation_available"] = (token is not None and
                                            card["state"] in ("pending", "approved"))
        response = {
            "content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
            "structuredContent": public,
        }
        if token is not None:
            response["_meta"] = {"test_plan_confirmation": token}
        return response

    def open(self, project, target, operation_id):
        with _lock(self._lock_path(operation_id)):
            with _lock(self.operations.lock_path):
                state = self._plan_locked(
                    project, target, operation_id, allow_already_approved=True)
                immutable = state["immutable"]
                approved = self._receipt_exists_locked(state)
                # Even an already approved plan needs a fresh, user-operated
                # capability so its unused receipt can be safely withdrawn.
                token = secrets.token_hex(32)
                card = {
                    "version": 1, "project": PROJECT, "target": TARGET,
                    "operation_id": operation_id,
                    "component": immutable["component"], "mode": immutable["mode"],
                    "card_id": secrets.token_hex(16),
                    "plan_digest": state["plan_digest"],
                    "source_digest": immutable["source_digest"],
                    "created_at": host_access._utcnow().isoformat(),
                    "expires_at": immutable["expires_at"],
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(),
                    "uid": os.getuid(),
                    "token_digest": _digest(token) if token is not None else "0" * 64,
                    "state": "approved" if approved else "pending",
                }
                _save_private_json(self._path(operation_id), card)
                return self._result(card, state, token=token,
                                    approved=True if approved else None)

    def _checked(self, project, target, operation_id, card_id, confirmation_token, *,
                 allow_already_approved=False, allow_approved_cancel=False):
        self._scope(project, target, operation_id)
        if (not isinstance(card_id, str) or not _ID.fullmatch(card_id)
                or not isinstance(confirmation_token, str)
                or not _DIGEST.fullmatch(confirmation_token)):
            raise HostRuntimeError("TEST_PLAN_CARD_DENIED")
        card = self._load(operation_id)
        if (card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("TEST_PLAN_CARD_DENIED")
        if card["state"] != "pending" and not (allow_approved_cancel and
                                                   card["state"] == "approved"):
            raise HostRuntimeError("TEST_PLAN_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("TEST_PLAN_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id()
                or card["uid"] != os.getuid()):
            raise HostRuntimeError("TEST_PLAN_CARD_STALE")
        state = self._plan(project, target, operation_id,
                           allow_already_approved=allow_already_approved)
        immutable = state["immutable"]
        if (card["plan_digest"] != state["plan_digest"]
                or card["source_digest"] != immutable["source_digest"]
                or card["component"] != immutable["component"]
                or card["mode"] != immutable["mode"]
                or card["expires_at"] != immutable["expires_at"]):
            raise HostRuntimeError("TEST_PLAN_CARD_STALE")
        return card, state

    def cancel(self, project, target, operation_id, *, card_id, confirmation_token):
        """Owner-operated withdrawal of an unexecuted TEST plan, including its receipt.

        Cancellation is a separate one-use MCP App callback. It can never stop
        queued/running work or reuse the plan's former execution authorization.
        """
        with _lock(self._lock_path(operation_id)):
            card, _state = self._checked(
                project, target, operation_id, card_id, confirmation_token,
                allow_already_approved=True, allow_approved_cancel=True)
            with _lock(self.operations.lock_path):
                latest = self.operations._load(project, target, operation_id)
                if (latest["plan_digest"] != card["plan_digest"]
                        or latest["immutable"]["source_digest"] != card["source_digest"]
                        or latest["immutable"]["component"] != card["component"]
                        or latest["immutable"]["mode"] != card["mode"]):
                    raise HostRuntimeError("TEST_PLAN_CARD_STALE")
                if latest["state"] != "pending_host_approval":
                    raise HostRuntimeError("TEST_PLAN_OPERATION_STARTED")
                receipt_verified = self._receipt_exists_locked(latest)
                if card["state"] == "approved" and not receipt_verified:
                    raise HostRuntimeError("TEST_PLAN_CARD_STALE")
                # Unlink only the receipt whose digest and nonce were validated.
                # A crash before the terminal state write is fail-closed: the
                # plan stays pending without a receipt and can be cancelled again.
                if receipt_verified:
                    (self.operations.operations_dir /
                     (operation_id + ".approval.json")).unlink()
                latest.update(
                    state="rejected", phase="APPROVAL", code="USER_CANCELLED",
                    finished_at=host_access._utcnow().isoformat())
                self.operations._save(latest)
                card["state"] = "cancelled"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, latest, approved=False)


def tool_specs():
    fields = {
        "project": {"type": "string", "enum": [PROJECT]},
        "target": {"type": "string", "enum": [TARGET]},
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
            "openai/toolInvocation/invoking": "TEST 계획 승인 확인 중",
            "openai/toolInvocation/invoked": "TEST 계획 승인 응답을 받았습니다",
        }
        if opener:
            meta["ui"]["resourceUri"] = UI_URI
            meta["openai/outputTemplate"] = UI_URI
        yield {
            "name": name, "title": (
                "TEST 배포·마이그레이션 승인 카드" if opener else "TEST 계획 승인 응답"),
            "description": (
                "Open the exact pending API/Worker/001-013 or separately reviewed 015/016 TEST plan in a one-use "
                "chat confirmation card. No build, Docker operation, migration or "
                "service restart happens on open or approve. Already approved "
                "plans offer owner-operated withdrawal of the unused receipt. "
                "A completed, queued or running operation cannot be withdrawn."
                if opener else "App-only callback requiring the hidden one-use TEST plan capability."),
            "inputSchema": {
                "type": "object",
                "properties": {k: fields[k] for k in sorted(_REQUIRED[name])},
                "required": sorted(_REQUIRED[name]),
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False, "destructiveHint": action == "confirm",
                "idempotentHint": False, "openWorldHint": False,
            },
            "_meta": meta,
        }


class TestPlanApprovalTools:
    def __init__(self, operations):
        self.names = NAMES
        self.operations = operations
        self.manager = None

    @staticmethod
    def validate_arguments(name, arguments):
        required = _REQUIRED.get(name)
        if required is None or not isinstance(arguments, dict) or set(arguments) != required:
            raise HostRuntimeError("INVALID_TEST_PLAN_CARD_ARGUMENTS")
        TestPlanApprovals._scope(
            arguments["project"], arguments["target"], arguments["operation_id"])
        if name != "open_test_plan_approval":
            if (not isinstance(arguments["card_id"], str)
                    or not _ID.fullmatch(arguments["card_id"])
                    or not isinstance(arguments["confirmation_token"], str)
                    or not _DIGEST.fullmatch(arguments["confirmation_token"])):
                raise HostRuntimeError("TEST_PLAN_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self.manager is None:
                self.manager = TestPlanApprovals(self.operations)
            action = name.split("_", 1)[0]
            return await asyncio.to_thread(getattr(self.manager, action), **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            incident = uuid.uuid4().hex
            print(json.dumps({
                "component": "test-plan-chat-approval", "event": "unexpected-error",
                "diagnostic_id": incident, "operation": name,
                "exception_type": type(exc).__name__,
                "errno": exc.errno if isinstance(exc, OSError) else None,
            }), file=sys.stderr, flush=True)
            raise ValueError("TEST_PLAN_CARD_UNAVAILABLE:" + incident) from None
