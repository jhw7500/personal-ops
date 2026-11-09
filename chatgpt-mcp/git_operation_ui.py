"""Trusted MCP App callbacks for exact Git plans; capabilities never reach the model."""
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
from git_workflows import PLAN_TTL_SECONDS, _json_bytes, _timestamp
from host_runtime_config import HostRuntimeError
from host_runtime_operations import _lock


UI_URI = "ui://git-operations/approval-v2.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
NAMES = frozenset({
    "open_git_operation_approval", "confirm_git_operation",
    "confirm_and_execute_git_operation", "cancel_git_operation_approval",
})
_REQUIRED = {
    name: {"project", "operation_id"} | (
        set() if name.startswith("open_") else {"card_id", "confirmation_token"})
    for name in NAMES
}
_FIELDS = {
    "version", "project", "operation_id", "card_id", "plan_digest", "source_digest",
    "runtime_identity", "boot_id", "uid", "token_digest", "expires_at", "state",
}
_STATES = {"pending", "spent", "approved", "cancelled"}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GitOperationApprovals:
    def __init__(self, workflows, *, trusted_runtime=None):
        self.workflows = workflows
        self._runtime_source = trusted_runtime

    def _runtime_identity(self):
        value = (host_access._installed_runtime_identity() if self._runtime_source is None
                 else self._runtime_source())
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise HostRuntimeError("UNSAFE_RUNTIME")
        return value

    def _scope(self, project, operation_id):
        if project not in self.workflows.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(operation_id, str) or _ID.fullmatch(operation_id) is None:
            raise HostRuntimeError("GIT_CARD_DENIED")

    def _path(self, project, operation_id):
        self._scope(project, operation_id)
        return self.workflows._project_dir(project) / (operation_id + ".chat-card.json")

    def _lock_path(self, project, operation_id):
        return self._path(project, operation_id).with_suffix(".lock")

    def _load(self, project, operation_id):
        path = self._path(project, operation_id)
        self.workflows._verify_private(path)
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(card, dict) or set(card) != _FIELDS
                    or card["version"] != 1 or card["project"] != project
                    or card["operation_id"] != operation_id or card["state"] not in _STATES
                    or type(card["uid"]) is not int
                    or not isinstance(card["boot_id"], str)
                    or not isinstance(card["card_id"], str)
                    or _ID.fullmatch(card["card_id"]) is None
                    or any(not isinstance(card[key], str)
                           or _DIGEST.fullmatch(card[key]) is None for key in (
                               "plan_digest", "source_digest", "runtime_identity", "token_digest"))):
                raise ValueError("invalid card")
            _timestamp(card["expires_at"])
            return card
        except (OSError, TypeError, KeyError, ValueError):
            raise HostRuntimeError("GIT_CARD_STATE_INVALID") from None

    def _save(self, card):
        self.workflows._write_private(
            self._path(card["project"], card["operation_id"]), _json_bytes(card))

    @staticmethod
    def _expires_at(plan):
        value = plan["immutable"].get("expires_at")
        return value or (_timestamp(plan["created_at"]) + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat()

    @staticmethod
    def _source_digest(plan):
        return hashlib.sha256(_json_bytes(plan["immutable"]["snapshot"])).hexdigest()

    def _plan_locked(self, project, operation_id):
        root = self.workflows._project(project)
        plan = self.workflows._load_plan(project, operation_id)
        with self.workflows._workspace_access(project, write=True) as snapshot:
            self.workflows._validate_pending_locked(plan, root, snapshot)
        if _timestamp(self._expires_at(plan)) <= host_access._utcnow():
            raise HostRuntimeError("GIT_CARD_EXPIRED")
        return plan

    def _result(self, card, plan, *, public=None, token=None):
        immutable = plan["immutable"]
        public = dict(self.workflows._public(plan) if public is None else public)
        public.pop("approval_command", None)
        public.pop("approval_card", None)
        preview = immutable.get("preview", "")
        message = ""
        if plan["action"] == "commit":
            from pathlib import Path
            path = Path(immutable["message_path"])
            self.workflows._verify_private(path)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != immutable["message_sha256"]:
                raise HostRuntimeError("GIT_CARD_PLAN_CHANGED")
            message = raw.decode("utf-8")
        display = {
            **public, "branch": immutable["snapshot"]["branch"],
            "paths": list(immutable.get("paths", [])), "message": message,
            "preview": preview, "preview_sha256": _digest(preview),
        }
        if immutable.get("commit_identity") is not None:
            display["commit_identity"] = immutable["commit_identity"]
        if plan["action"] == "push":
            display.update(remote=immutable["remote"], target=immutable["trusted_target"],
                           remote_head=immutable["remote_head"])
        value = {
            "component": "git_operation", "project": plan["project"],
            "operation_id": plan["operation_id"], "action": plan["action"],
            "card_id": card["card_id"], "state": card["state"],
            "plan_digest": plan["plan_digest"], "source_digest": self._source_digest(plan),
            "expires_at": card["expires_at"],
            "receipt_verified": public.get("receipt_verified", False),
            "authorization_verified": public.get("authorization_verified", False),
            "approval_required": card["state"] == "pending" and public.get("approval_required", False),
            "supports_direct_execution": card["state"] == "pending",
            "executes_on_approval": False, "plan": display,
        }
        if public.get("next_action") is not None:
            value["next_action"] = public["next_action"]
        result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                  "structuredContent": value}
        if token is not None:
            result["_meta"] = {"git_operation_confirmation": token}
        return result

    def open(self, project, operation_id):
        self._scope(project, operation_id)
        runtime = self._runtime_identity()
        with _lock(self._lock_path(project, operation_id)):
            with self.workflows._lock(project):
                plan = self._plan_locked(project, operation_id)
                public = self.workflows._public(plan)
                card = {
                    "version": 1, "project": project, "operation_id": operation_id,
                    "card_id": secrets.token_hex(16), "plan_digest": plan["plan_digest"],
                    "source_digest": self._source_digest(plan), "runtime_identity": runtime,
                    "boot_id": host_access._current_boot_id(), "uid": os.getuid(),
                    "expires_at": self._expires_at(plan), "token_digest": "0" * 64,
                    "state": "approved" if not public["approval_required"] else "pending",
                }
                if not public["approval_required"]:
                    return self._result(card, plan, public=public)
                token = secrets.token_hex(32)
                card["token_digest"] = _digest(token)
                self._save(card)
                return self._result(card, plan, public=public, token=token)

    def _checked(self, project, operation_id, card_id, confirmation_token):
        if (not isinstance(card_id, str) or _ID.fullmatch(card_id) is None
                or not isinstance(confirmation_token, str)
                or _DIGEST.fullmatch(confirmation_token) is None):
            raise HostRuntimeError("GIT_CARD_DENIED")
        card = self._load(project, operation_id)
        if (card["card_id"] != card_id or card["state"] != "pending"
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("GIT_CARD_DENIED")
        if _timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("GIT_CARD_EXPIRED")
        if (card["runtime_identity"] != self._runtime_identity()
                or card["boot_id"] != host_access._current_boot_id() or card["uid"] != os.getuid()):
            raise HostRuntimeError("GIT_CARD_CONTEXT_CHANGED")
        with self.workflows._lock(project):
            plan = self._plan_locked(project, operation_id)
            if (card["plan_digest"] != plan["plan_digest"]
                    or card["source_digest"] != self._source_digest(plan)
                    or card["expires_at"] != self._expires_at(plan)):
                raise HostRuntimeError("GIT_CARD_PLAN_CHANGED")
        return card, plan

    def confirm(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self._lock_path(project, operation_id)):
            card, plan = self._checked(project, operation_id, card_id, confirmation_token)
            card["state"] = "spent"
            self._save(card)
            public = self.workflows.approve_web(
                project, operation_id=operation_id, plan_digest=card["plan_digest"],
                confirmation=operation_id)
            card["state"] = "approved"
            self._save(card)
            return self._result(card, plan, public=public)

    def confirm_and_execute(self, project, operation_id, *, card_id, confirmation_token):
        confirmed = self.confirm(project, operation_id, card_id=card_id,
                                 confirmation_token=confirmation_token)
        try:
            execution = self.workflows.execute({"project": project, "operation_id": operation_id})
        except ValueError:
            # Only a failure persisted after admission is a normal callback result.
            # Pre-admission drift leaves the plan pending and must stay visible.
            execution = self.workflows.get({"project": project, "operation_id": operation_id})
            if execution["status"] not in {"failed", "outcome_unknown"}:
                raise
        value = confirmed["structuredContent"]
        value.update(
            execution_requested=True,
            execution_started=execution["status"] in {
                "executing", "completed", "failed", "outcome_unknown",
            },
            execution=execution, supports_direct_execution=False,
            next_action={"tool": "get_git_operation",
                         "arguments": {"project": project, "operation_id": operation_id}},
        )
        confirmed["content"] = [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
        return confirmed

    def cancel(self, project, operation_id, *, card_id, confirmation_token):
        self._scope(project, operation_id)
        with _lock(self._lock_path(project, operation_id)):
            card, _ = self._checked(project, operation_id, card_id, confirmation_token)
            with self.workflows._lock(project):
                plan = self._plan_locked(project, operation_id)
                public = self.workflows._public(plan)
                if not public["approval_required"]:
                    card["state"] = "approved"
                else:
                    plan["status"] = "cancelled"
                    self.workflows._save_plan(plan)
                    card["state"] = "cancelled"
                    public = self.workflows._public(plan)
                self._save(card)
                return self._result(card, plan, public=public)


def tool_specs(projects):
    fields = {
        "project": {"type": "string", "enum": list(projects)},
        "operation_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    for name in sorted(NAMES):
        opener = name.startswith("open_")
        execute = name == "confirm_and_execute_git_operation"
        meta = {"ui": {"visibility": ["model"] if opener else ["app"]},
                "openai/widgetAccessible": not opener}
        if opener:
            meta["ui"]["resourceUri"] = UI_URI
            meta["openai/outputTemplate"] = UI_URI
        yield {
            "name": name,
            "title": ("Git 승인 카드" if opener else "Git 승인하고 실행" if execute else
                      "Git 승인 취소" if name.startswith("cancel_") else "Git 계획 승인"),
            "description": (
                "Open the exact commit or push plan in chat. Opening never approves or executes. "
                "An already authorized plan returns its verified authorization and next action."
                if opener else
                "Trusted app-only explicit approve-and-execute callback for one exact Git plan. "
                "Never request or supply the hidden confirmation capability in chat."
                if execute else
                "Trusted app-only one-use callback for the exact displayed Git plan. "
                "Never execute Git or request the hidden confirmation capability in chat."),
            "inputSchema": {"type": "object",
                            "properties": {key: fields[key] for key in sorted(_REQUIRED[name])},
                            "required": sorted(_REQUIRED[name]), "additionalProperties": False},
            "annotations": {"readOnlyHint": False, "destructiveHint": execute,
                            "idempotentHint": False, "openWorldHint": execute},
            "_meta": meta,
        }


class GitOperationApprovalTools:
    names = NAMES

    def __init__(self, workflows, project_locks=None):
        self.workflows = workflows
        self.project_locks = project_locks or {}
        self.manager = GitOperationApprovals(workflows)

    def validate_arguments(self, name, arguments):
        if name not in NAMES or not isinstance(arguments, dict) or set(arguments) != _REQUIRED[name]:
            raise HostRuntimeError("INVALID_GIT_CARD_ARGUMENTS")
        if not isinstance(arguments["project"], str) or arguments["project"] not in self.workflows.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if not isinstance(arguments["operation_id"], str) or _ID.fullmatch(arguments["operation_id"]) is None:
            raise HostRuntimeError("GIT_CARD_DENIED")
        if not name.startswith("open_"):
            if (not isinstance(arguments["card_id"], str)
                    or _ID.fullmatch(arguments["card_id"]) is None
                    or not isinstance(arguments["confirmation_token"], str)
                    or _DIGEST.fullmatch(arguments["confirmation_token"]) is None):
                raise HostRuntimeError("GIT_CARD_DENIED")

    async def _execute_in_lane(self, method, arguments):
        lock = self.project_locks.get(arguments["project"])
        if lock is None:
            return await asyncio.to_thread(method, **arguments)
        if lock.locked():
            raise HostRuntimeError("PROJECT_BUSY")
        async with lock:
            task = asyncio.create_task(asyncio.to_thread(method, **arguments))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Caller cancellation must not free the mutation lane while Git still runs.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if task.done() and not task.cancelled():
                    task.exception()
                raise

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            action = ("confirm_and_execute" if name == "confirm_and_execute_git_operation"
                      else name.split("_", 1)[0])
            method = getattr(self.manager, action)
            if action == "confirm_and_execute":
                return await self._execute_in_lane(method, arguments)
            return await asyncio.to_thread(method, **arguments)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except ValueError:
            raise ValueError("GIT_CARD_PLAN_UNAVAILABLE") from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            print(json.dumps({"component": "git-operation-chat-approval", "event": "unexpected-error",
                              "diagnostic_id": diagnostic_id, "operation": name,
                              "exception_type": type(exc).__name__,
                              "errno": exc.errno if isinstance(exc, OSError) else None}),
                  file=sys.stderr, flush=True)
            raise ValueError("GIT_CARD_UNAVAILABLE:" + diagnostic_id) from None
