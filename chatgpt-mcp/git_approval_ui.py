"""One-use exact Git commit/push confirmation via authenticated MCP App.

The owner views the immutable plan. Only a private app-only token permits a
receipt matching the existing Git executor. No commit or push is performed by
opening or confirming the card. PR/merge remain native review workflows.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid

import host_access
from host_runtime_config import HostRuntimeError, private_dir
from host_runtime_operations import _lock, _read_private_json, _save_private_json, _timestamp
from git_workflows import EMPTY_TREE, _json_bytes, _sha256, _utcnow

UI_URI = "ui://git-operations/approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PROJECT = re.compile(r"[A-Za-z0-9_.-]+\Z")
CARD_TTL = timedelta(days=7)
_STATES = {"pending", "spent", "approved", "cancelled"}
_FIELDS = {
    "version", "project", "operation_id", "card_id", "action",
    "plan_digest", "snapshot_digest", "created_at", "expires_at",
    "runtime_identity", "boot_id", "uid", "token_digest", "state",
}
NAMES = frozenset({"open_git_approval", "confirm_git_approval", "cancel_git_approval"})
_REQUIRED = {n: ({"project", "operation_id"} if n.startswith("open_")
                else {"project", "operation_id", "card_id", "confirmation_token"})
             for n in NAMES}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class GitApprovals:
    def __init__(self, git, *, trusted_runtime=None):
        self.git = git
        self._runtime_source = trusted_runtime
        self.cards_dir = private_dir(git.state_dir / "chat-approval-cards")
        self.locks_dir = private_dir(git.state_dir / "chat-approval-card-locks")

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity()
                 if self._runtime_source is None else self._runtime_source())
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    def _scope(self, project, operation_id):
        if (not isinstance(project, str) or not PROJECT.fullmatch(project)
                or project not in self.git.projects):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise HostRuntimeError("GIT_CARD_DENIED")

    def _path(self, operation_id):
        return self.cards_dir / (operation_id + ".json")

    def _lock_path(self, operation_id):
        return self.locks_dir / (operation_id + ".lock")

    def _load(self, operation_id):
        try:
            card = _read_private_json(self._path(operation_id))
            if (set(card) != _FIELDS or card["version"] != 1
                    or card["project"] not in self.git.projects
                    or card["operation_id"] != operation_id
                    or card["state"] not in _STATES
                    or card["action"] not in ("commit", "push")
                    or type(card["uid"]) is not int
                    or not isinstance(card["boot_id"], str)
                    or not isinstance(card["card_id"], str) or not _ID.fullmatch(card["card_id"])
                    or any(not isinstance(card[key], str)
                           or not _DIGEST.fullmatch(card[key]) for key in (
                               "plan_digest", "snapshot_digest", "runtime_identity",
                               "token_digest"))):
                raise HostRuntimeError("GIT_CARD_STATE_INVALID")
            _timestamp(card["created_at"])
            _timestamp(card["expires_at"])
            return card
        except (OSError, KeyError, TypeError, ValueError, HostRuntimeError):
            raise HostRuntimeError("GIT_CARD_STATE_INVALID") from None

    def _receipt_approved(self, project, operation_id, plan):
        path = self.git._receipt_path(project, operation_id)
        if not path.exists() and not path.is_symlink():
            return False
        self.git._verify_private(path)
        try:
            receipt = json.loads(path.read_text(encoding="utf8"))
            observed = _timestamp(receipt["approved_at"])
            expected = {
                "operation_id": operation_id,
                "plan_digest": plan["plan_digest"],
                "approval_nonce": plan["immutable"]["approval_nonce"],
            }
            if (set(receipt) != {*expected, "approved_at"}
                    or any(receipt.get(k) != v for k, v in expected.items())
                    or observed < _timestamp(plan["created_at"])
                    or observed > host_access._utcnow() + timedelta(minutes=1)):
                raise ValueError
            return True
        except (KeyError, TypeError, OSError, ValueError, HostRuntimeError):
            raise HostRuntimeError("INVALID_GIT_APPROVAL") from None

    def _plan_locked(self, project, operation_id):
        self._scope(project, operation_id)
        root = self.git._project(project)
        plan = self.git._load_plan(project, operation_id)
        if plan["status"] != "pending_host_approval":
            raise HostRuntimeError("GIT_CARD_NOT_PENDING")
        imm = plan["immutable"]
        if imm.get("action") not in ("commit", "push"):
            raise HostRuntimeError("GIT_CARD_PLAN_REQUIRED")
        # Revalidate exactly what GitWorkflows.execute will check, including
        # owner-reviewed workspace write scope, outgoing history and message.
        with self.git._workspace_access(project, write=True) as workspace:
            if plan["action"] == "commit":
                self.git._workspace_paths(workspace, project, imm.get("paths", ()), "write")
                message = Path(imm["message_path"])
                self.git._verify_private(message)
                if _sha256(message.read_bytes()) != imm["message_sha256"]:
                    raise HostRuntimeError("GIT_CARD_STALE")
            elif workspace is not None:
                self.git._validate_outgoing_history(
                    workspace, project, root,
                    imm["remote_head"] or imm.get("base_head") or EMPTY_TREE,
                    imm["snapshot"]["head"])
            if self.git._snapshot(root, imm.get("paths", ())) != imm["snapshot"]:
                raise HostRuntimeError("GIT_CARD_STALE")
        approved = self._receipt_approved(project, operation_id, plan)
        return plan, approved

    def _public(self, card, plan, *, approved=None):
        imm = plan["immutable"]
        details = self.git._public(plan)
        details.pop("approval_command", None)
        approved_receipt = approved is True or card["state"] == "approved"
        required = card["state"] == "pending" and approved is None
        public = {
            "project": card["project"], "operation_id": card["operation_id"],
            "component": "git_operation", "card_id": card["card_id"],
            "action": card["action"], "state": card["state"],
            "plan_digest": card["plan_digest"],
            "snapshot_digest": card["snapshot_digest"],
            "display_command": imm["display_command"],
            "head_target": imm["head_target"],
            "paths": imm.get("paths", []),
            "message_sha256": imm.get("message_sha256"),
            "branch": imm.get("branch"),
            "large_push": imm.get("large_push", False),
            "outgoing_commit_count": imm.get("outgoing_commit_count"),
            "outgoing_path_count": imm.get("outgoing_path_count"),
            "outgoing_path_manifest_sha256": imm.get("outgoing_path_manifest_sha256"),
            "expires_at": card["expires_at"],
            "executes_on_approval": False,
            "approval_required": required, "receipt_verified": approved_receipt,
            "plan": details,
        }
        if required:
            public["guidance"] = (
                "Show this exact commit/push card. Only the owner's app callback "
                "issues a one-use receipt; approval never commits or pushes. "
                "PR and merge remain native CI/review workflows."
            )
        if approved_receipt:
            next_action = {
                "tool": "execute_git_operation",
                "arguments": {
                    "project": card["project"], "operation_id": card["operation_id"]
                },
            }
            public["next_action"] = next_action
            details.update(receipt_verified=True, approval_required=False,
                           next_action=next_action)
        if approved is not None:
            public["approved"] = approved
        return public

    def _result(self, card, plan, *, token=None, approved=None):
        public = self._public(card, plan, approved=approved)
        result = {
            "content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
            "structuredContent": public,
        }
        if token is not None:
            result["_meta"] = {"git_confirmation": token}
        return result

    def open(self, project, operation_id):
        self._scope(project, operation_id)
        with _lock(self._lock_path(operation_id)):
            with self.git._lock(project):
                plan, approved = self._plan_locked(project, operation_id)
                imm = plan["immutable"]
                token = None if approved else secrets.token_hex(32)
                card = {
                    "version": 1, "project": project, "operation_id": operation_id,
                    "card_id": secrets.token_hex(16), "action": imm["action"],
                    "plan_digest": plan["plan_digest"],
                    "snapshot_digest": _sha256(_json_bytes(imm["snapshot"])),
                    "created_at": host_access._utcnow().isoformat(),
                    "expires_at": (host_access._utcnow() + CARD_TTL).isoformat(),
                    "runtime_identity": self._runtime_identity(),
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "token_digest": _digest(token) if token is not None else "0" * 64,
                    "state": "approved" if approved else "pending",
                }
                _save_private_json(self._path(operation_id), card)
                return self._result(card, plan, token=token,
                                    approved=True if approved else None)

    def _checked(self, project, operation_id, card_id, confirmation_token):
        self._scope(project, operation_id)
        if (not isinstance(card_id, str) or not _ID.fullmatch(card_id)
                or not isinstance(confirmation_token, str)
                or not _DIGEST.fullmatch(confirmation_token)):
            raise HostRuntimeError("GIT_CARD_DENIED")
        card = self._load(operation_id)
        if (card["project"] != project or card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("GIT_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("GIT_CARD_SPENT")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("GIT_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id()
                or card["uid"] != os.getuid()):
            raise HostRuntimeError("GIT_CARD_STALE")
        with self.git._lock(project):
            plan, _ = self._plan_locked(project, operation_id)
            imm = plan["immutable"]
            if (card["plan_digest"] != plan["plan_digest"]
                    or card["action"] != imm["action"]
                    or card["snapshot_digest"] != _sha256(_json_bytes(imm["snapshot"]))):
                raise HostRuntimeError("GIT_CARD_STALE")
        return card, plan

    def confirm(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self._lock_path(operation_id)):
            card, _ = self._checked(project, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            with self.git._lock(project):
                plan, already_approved = self._plan_locked(project, operation_id)
                if not already_approved:
                    receipt = {
                        "operation_id": operation_id, "plan_digest": plan["plan_digest"],
                        "approval_nonce": plan["immutable"]["approval_nonce"],
                        "approved_at": _utcnow(),
                    }
                    path = self.git._receipt_path(project, operation_id)
                    fd = os.open(
                        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(_json_bytes(receipt))
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.git._verify_private(path)
            card["state"] = "approved"
            _save_private_json(self._path(operation_id), card)
            return self._result(card, plan, approved=True)

    def cancel(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self._lock_path(operation_id)):
            card, _ = self._checked(project, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(operation_id), card)
            with self.git._lock(project):
                plan, approved = self._plan_locked(project, operation_id)
                if not approved:
                    plan["status"] = "rejected"
                    self.git._save_plan(plan)
            card["state"] = "approved" if approved else "cancelled"
            _save_private_json(self._path(operation_id), card)
            return self._result(card, plan, approved=approved)


def tool_specs(projects):
    fields = {
        "project": {"type": "string", "enum": sorted(projects)},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        action = name.split("_", 1)[0]
        opener = action == "open"
        metadata = {
            "ui": {"visibility": ["model"] if opener else ["app"]},
            "openai/widgetAccessible": not opener,
            "openai/toolInvocation/invoking": "Git 작업 승인 확인 중",
            "openai/toolInvocation/invoked": "Git 승인 응답을 받았습니다",
        }
        if opener:
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {
            "name": name, "title": "Git 커밋·푸시 승인 카드" if opener else "Git 승인 응답",
            "description": (
                "Open one exact prepared Git commit or push in an in-chat "
                "one-use confirmation card. Approval does not execute. "
                "PR/merge still require native reviews."
                if opener else "App-only callback requiring the hidden Git approval."),
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
            "_meta": metadata,
        }


class GitApprovalTools:
    def __init__(self, git):
        self.git = git
        self.names = NAMES
        self.manager = GitApprovals(git)

    def validate_arguments(self, name, arguments):
        required = _REQUIRED.get(name)
        if required is None or not isinstance(arguments, dict) or set(arguments) != required:
            raise HostRuntimeError("INVALID_GIT_CARD_ARGUMENTS")
        if (arguments.get("project") not in self.git.projects
                or not isinstance(arguments.get("operation_id"), str)
                or not _ID.fullmatch(arguments["operation_id"])):
            raise HostRuntimeError("GIT_CARD_DENIED")
        if name != "open_git_approval":
            if (not isinstance(arguments.get("card_id"), str)
                    or not _ID.fullmatch(arguments["card_id"])
                    or not isinstance(arguments.get("confirmation_token"), str)
                    or not _DIGEST.fullmatch(arguments["confirmation_token"])):
                raise HostRuntimeError("GIT_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            return await asyncio.to_thread(
                getattr(self.manager, name.split("_", 1)[0]), **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(json.dumps({
                "component": "git-chat-approval", "operation": name,
                "event": "unexpected-error", "diagnostic_id": diagnostic_id,
                "exception_type": type(exc).__name__,
                "errno": exc.errno if isinstance(exc, OSError) else None,
            }), file=sys.stderr, flush=True)
            raise ValueError("GIT_CARD_UNAVAILABLE:" + diagnostic_id) from None
