"""Inline, app-only confirmation for an exact runtime-rollout plan.

Only a hidden component capability may invoke confirmation. Issuance delegates
to the existing runtime receipt path; no host grant is converted into approval.
Neither opening nor confirming calls execute or starts a systemd unit.
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
from host_runtime_operations import (
    _lock,
    _read_private_json,
    _save_private_json,
    _timestamp,
)
from runtime_rollout_web import PROJECT

UI_URI = "ui://runtime-rollouts/approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT = re.compile(r"[0-9a-f]{20}\Z")
_FIELDS = {
    "version",
    "project",
    "operation_id",
    "card_id",
    "plan_digest",
    "source_digest",
    "target_snapshot",
    "control_digest",
    "created_at",
    "expires_at",
    "runtime_identity",
    "boot_id",
    "uid",
    "token_digest",
    "state",
}
NAMES = frozenset(
    {
        "open_runtime_rollout_approval",
        "confirm_runtime_rollout_approval",
        "cancel_runtime_rollout_approval",
    }
)
_REQUIRED = {
    name: (
        {"project", "operation_id"}
        if name.startswith("open_")
        else {"project", "operation_id", "card_id", "confirmation_token"}
    )
    for name in NAMES
}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class RuntimeRolloutApprovals:
    def __init__(self, operations, *, trusted_runtime=None):
        self.operations = operations
        self.cards_dir = private_dir(operations.state_root / "approval-cards")
        self.locks_dir = private_dir(operations.state_root / "approval-card-locks")
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        identity = (
            host_access._installed_runtime_identity()
            if self._runtime_source is None
            else self._runtime_source()
        )
        if not isinstance(identity, str) or not _DIGEST.fullmatch(identity):
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return identity

    @staticmethod
    def _scope(project, operation_id):
        if project != PROJECT:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise HostRuntimeError("ROLLOUT_CARD_DENIED")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _load(self, operation_id):
        try:
            card = _read_private_json(self._path(operation_id))
            if (
                set(card) != _FIELDS
                or type(card["version"]) is not int
                or card["version"] != 1
                or card["project"] != PROJECT
                or card["operation_id"] != operation_id
                or card["state"] not in {"pending", "spent", "approved", "cancelled"}
                or type(card["uid"]) is not int
                or not isinstance(card["boot_id"], str)
                or not isinstance(card["card_id"], str)
                or not _ID.fullmatch(card["card_id"])
                or not isinstance(card["target_snapshot"], str)
                or not _SNAPSHOT.fullmatch(card["target_snapshot"])
                or any(
                    not isinstance(card[k], str) or not _DIGEST.fullmatch(card[k])
                    for k in (
                        "plan_digest",
                        "source_digest",
                        "control_digest",
                        "runtime_identity",
                        "token_digest",
                    )
                )
            ):
                raise HostRuntimeError("ROLLOUT_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, ValueError, KeyError, HostRuntimeError):
            raise HostRuntimeError("ROLLOUT_CARD_STATE_INVALID") from None

    def _plan_locked(self, project, operation_id):
        self._scope(project, operation_id)
        self.operations._trusted_only()
        plan = self.operations._reconcile(self.operations._load(operation_id))
        fixed = plan["immutable"]
        if plan.get("code") == "ROLLOUT_APPROVAL_STALE":
            raise HostRuntimeError("ROLLOUT_APPROVAL_STALE")
        if _timestamp(fixed["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("ROLLOUT_CARD_EXPIRED")
        if plan["state"] not in {"pending_host_approval", "approved"}:
            raise HostRuntimeError("ROLLOUT_OPERATION_STARTED")
        self.operations._check_source(plan)
        if plan["state"] == "approved":
            self._receipt(plan)
        return plan

    def _receipt(self, plan):
        self.operations.verify_approval(plan)

    def _result(self, card, plan, *, token=None, approved=None):
        view = self.operations._public(plan)
        view.pop("approval_url", None)
        fixed = plan["immutable"]
        public = {
            "project": PROJECT,
            "component": "runtime_rollout",
            "operation_id": card["operation_id"],
            "card_id": card["card_id"],
            "state": card["state"],
            "plan_digest": card["plan_digest"],
            "source_digest": card["source_digest"],
            "target_snapshot": card["target_snapshot"],
            "control_digest": card["control_digest"],
            "source_files": fixed["source_files"],
            "impact": view["impact"],
            "expires_at": card["expires_at"],
            "executes_on_approval": False,
            "receipt_verified": approved is True,
            "approval_required": card["state"] == "pending" and approved is None,
            "plan": view,
            "guidance": "Show the runtime rollout confirmation card. Only the user's app callback may approve. Opening and approval never install or restart. Do not ask for tokens or substitute a host-access/command approval.",
        }
        if approved is not None:
            public["approved"] = approved
        if approved is True:
            public["next_action"] = {
                "tool": "execute_runtime_rollout",
                "arguments": {"project": PROJECT, "operation_id": card["operation_id"]},
            }
            public["guidance"] = (
                "The exact runtime plan has a verified receipt. Reuse it without another approval. Execute only when the user already requested execution. Approval alone never starts the rollout."
            )
        result = {
            "content": [
                {"type": "text", "text": json.dumps(public, ensure_ascii=False)}
            ],
            "structuredContent": public,
        }
        if token is not None:
            result["_meta"] = {"runtime_rollout_confirmation": token}
        return result

    def open(self, project, operation_id):
        self._scope(project, operation_id)
        with (
            _lock(self.locks_dir / (operation_id + ".lock")),
            _lock(self.operations.lock),
        ):
            plan = self._plan_locked(project, operation_id)
            approved = plan["state"] == "approved"
            fixed = plan["immutable"]
            token = None if approved else secrets.token_hex(32)
            card = {
                "version": 1,
                "project": project,
                "operation_id": operation_id,
                "card_id": secrets.token_hex(16),
                "plan_digest": plan["plan_digest"],
                "source_digest": fixed["source_digest"],
                "target_snapshot": fixed["target_snapshot"],
                "control_digest": fixed["control_digest"],
                "created_at": host_access._utcnow().isoformat(),
                "expires_at": fixed["expires_at"],
                "runtime_identity": self._runtime_identity(),
                "boot_id": host_access._current_boot_id(),
                "uid": os.getuid(),
                "token_digest": _digest(token) if token else "0" * 64,
                "state": "approved" if approved else "pending",
            }
            _save_private_json(self._path(operation_id), card)
            return self._result(
                card, plan, token=token, approved=True if approved else None
            )

    @staticmethod
    def _same_plan(card, plan):
        fixed = plan["immutable"]
        if card["plan_digest"] != plan["plan_digest"] or any(
            card[k] != fixed[k]
            for k in (
                "source_digest",
                "target_snapshot",
                "control_digest",
                "expires_at",
            )
        ):
            raise HostRuntimeError("ROLLOUT_CARD_STALE")

    def _checked(self, project, operation_id, card_id, confirmation_token):
        card = self._load(operation_id)
        if (
            not isinstance(card_id, str)
            or not _ID.fullmatch(card_id)
            or not isinstance(confirmation_token, str)
            or not _DIGEST.fullmatch(confirmation_token)
            or card["project"] != project
            or card["card_id"] != card_id
            or not hmac.compare_digest(
                card["token_digest"], _digest(confirmation_token)
            )
        ):
            raise HostRuntimeError("ROLLOUT_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("ROLLOUT_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("ROLLOUT_CARD_EXPIRED")
        if (
            card["runtime_identity"] != self._runtime_identity()
            or card["uid"] != os.getuid()
            or card["boot_id"] != host_access._current_boot_id()
        ):
            raise HostRuntimeError("ROLLOUT_CARD_STALE")
        with _lock(self.operations.lock):
            plan = self._plan_locked(project, operation_id)
            self._same_plan(card, plan)
        return card, plan

    def confirm(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, plan = self._checked(
                project, operation_id, card_id, confirmation_token
            )
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            if plan["state"] != "approved":
                try:
                    self.operations.approve_web(
                        project,
                        operation_id=operation_id,
                        plan_digest=card["plan_digest"],
                        source_digest=card["source_digest"],
                        confirmation=operation_id,
                    )
                except HostRuntimeError:
                    # A concurrent authenticated owner-web approval may win.
                    # Accept it only after the SAME plan and exact receipt verify.
                    with _lock(self.operations.lock):
                        current = self._plan_locked(project, operation_id)
                        self._same_plan(card, current)
                        if current["state"] != "approved":
                            raise
            with _lock(self.operations.lock):
                plan = self._plan_locked(project, operation_id)
                self._same_plan(card, plan)
                if plan["state"] != "approved":
                    raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED")
                card["state"] = "approved"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, plan, approved=True)

    def cancel(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self.locks_dir / (operation_id + ".lock")):
            card, _ = self._checked(project, operation_id, card_id, confirmation_token)
            with _lock(self.operations.lock):
                plan = self._plan_locked(project, operation_id)
                self._same_plan(card, plan)
                if plan["state"] == "approved":
                    card["state"] = "approved"
                    _save_private_json(self._path(operation_id), card)
                    return self._result(card, plan, approved=True)
                plan.update(
                    state="cancelled",
                    phase="APPROVAL",
                    code="USER_CANCELLED",
                    finished_at=host_access._utcnow().isoformat(),
                )
                self.operations._save(plan)
                card["state"] = "cancelled"
                _save_private_json(self._path(operation_id), card)
                return self._result(card, plan, approved=False)


def tool_specs():
    properties = {
        "project": {"type": "string", "enum": [PROJECT]},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        opener = name.startswith("open_")
        meta = {
            "ui": {"visibility": ["model"] if opener else ["app"]},
            "openai/widgetAccessible": not opener,
        }
        if opener:
            meta["ui"]["resourceUri"] = UI_URI
            meta["openai/outputTemplate"] = UI_URI
        yield {
            "name": name,
            "title": "MCP 런타임 전환 승인"
            if opener
            else "런타임 승인 확인"
            if name.startswith("confirm_")
            else "런타임 승인 취소",
            "description": "Open an exact runtime-plan approval card; never approve, install or restart."
            if opener
            else "App-only callback using a hidden one-use exact-runtime-plan capability. Never executes. Never ask for the capability in chat.",
            "inputSchema": {
                "type": "object",
                "properties": {k: properties[k] for k in sorted(_REQUIRED[name])},
                "required": sorted(_REQUIRED[name]),
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "_meta": meta,
        }


class RuntimeRolloutApprovalTools:
    names = NAMES

    def __init__(self, rollouts):
        self.rollouts = rollouts
        self.manager = None

    def validate_arguments(self, name, arguments):
        if (
            name not in NAMES
            or type(arguments) is not dict
            or set(arguments) != _REQUIRED[name]
        ):
            raise HostRuntimeError("INVALID_ROLLOUT_CARD_ARGUMENTS")
        RuntimeRolloutApprovals._scope(arguments["project"], arguments["operation_id"])
        if not name.startswith("open_"):
            if (
                not isinstance(arguments["card_id"], str)
                or not _ID.fullmatch(arguments["card_id"])
                or not isinstance(arguments["confirmation_token"], str)
                or not _DIGEST.fullmatch(arguments["confirmation_token"])
            ):
                raise HostRuntimeError("ROLLOUT_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self.manager is None:
                self.manager = RuntimeRolloutApprovals(self.rollouts)
            return await asyncio.to_thread(
                getattr(self.manager, name.split("_", 1)[0]), **arguments
            )
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(
                json.dumps(
                    {
                        "component": "runtime-rollout-chat-approval",
                        "event": "unexpected-error",
                        "diagnostic_id": diagnostic_id,
                        "operation": name,
                        "exception_type": type(exc).__name__,
                        "errno": exc.errno if isinstance(exc, OSError) else None,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise ValueError("ROLLOUT_CARD_UNAVAILABLE:" + diagnostic_id) from None
