"""One-use confirmations held by the trusted MCP App host, never by the model.

App-only visibility is routing, not authentication. The authenticated MCP host
and component receive the capability in result _meta. A caller controlling that
host/installation is inside this trust boundary; widget IDs are not identity.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import hmac
import json
import re
import secrets
import sys
import uuid

import host_access
from host_runtime_config import HostRuntimeError, private_dir
from host_runtime_operations import _lock, _read_private_json, _save_private_json


UI_URI = "ui://host-access/approval-v2.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_KEYS = {"boot_id", "runtime_identity", "uid", "project_root"}
_FIELDS_V1 = _BINDING_KEYS | {
    "version", "project", "card_id", "request_id", "created_at", "expires_at",
    "duration_seconds", "allow_codex", "revision", "token_digest", "state",
}
_FIELDS_V2 = _FIELDS_V1 | {"lifetime", "grant_state_revision"}


def _options(lifetime, duration_seconds, allow_codex):
    if lifetime not in {"timed", "until_revoked"}:
        raise HostRuntimeError("INVALID_HOST_ACCESS_LIFETIME")
    if lifetime == "timed":
        if (type(duration_seconds) is not int
                or not 1 <= duration_seconds <= host_access.MAX_DURATION_SECONDS):
            raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
    elif duration_seconds is not None:
        raise HostRuntimeError("INVALID_HOST_ACCESS_DURATION")
    if type(allow_codex) is not bool:
        raise HostRuntimeError("INVALID_HOST_ACCESS_REQUEST")


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class HostAccessApprovals:
    """One latest card per project, with a private lock separate from grant locks.

    Always take the card lock before a HostAccess operation. Confirm persists a
    spent capability BEFORE invoking the existing atomic, one-use issuer. A lost
    response or process crash can require a new card, but cannot replay a grant.
    Existing version-1 HostAccess records and the admin route remain unchanged.
    """

    def __init__(self, access):
        self.access = access
        self.cards_dir = private_dir(access.state_dir / "cards")

    def _path(self, project):
        return self.cards_dir / (self.access._key(project) + ".json")

    def _lock_path(self, project):
        return self.access.locks_dir / (self.access._key(project) + ".card.lock")

    def _load(self, project):
        path = self._path(project)
        if not path.exists() and not path.is_symlink():
            raise HostRuntimeError("HOST_ACCESS_CARD_DENIED")
        try:
            card = _read_private_json(path)
            version = card.get("version")
            expected = _FIELDS_V1 if version == 1 else _FIELDS_V2
            if (set(card) != expected or version not in {1, 2} or card["project"] != project
                    or type(card["revision"]) is not int or card["revision"] < 1
                    or card["state"] not in {"pending", "spent", "approved", "cancelled"}
                    or not isinstance(card["token_digest"], str)
                    or _TOKEN.fullmatch(card["token_digest"]) is None):
                raise HostRuntimeError("HOST_ACCESS_CARD_STATE_INVALID")
            lifetime = card.get("lifetime", "timed")
            _options(lifetime, card["duration_seconds"], card["allow_codex"])
            if version == 1:
                # The request validator also validates every persisted authority binding.
                self.access._validate_request({key: card[key] for key in
                    _BINDING_KEYS | {"request_id", "created_at", "expires_at"}})
            elif (card["grant_state_revision"] is not None
                  and (type(card["grant_state_revision"]) is not int
                       or card["grant_state_revision"] < 1)):
                raise HostRuntimeError("HOST_ACCESS_CARD_STATE_INVALID")
            if not isinstance(card["card_id"], str) or _ID.fullmatch(card["card_id"]) is None:
                raise HostRuntimeError("HOST_ACCESS_CARD_STATE_INVALID")
            return card
        except (OSError, TypeError, KeyError, ValueError, HostRuntimeError):
            raise HostRuntimeError("HOST_ACCESS_CARD_STATE_INVALID") from None

    def _checked(self, project, root, card_id, confirmation_token):
        if (not isinstance(card_id, str) or _ID.fullmatch(card_id) is None
                or not isinstance(confirmation_token, str)
                or _TOKEN.fullmatch(confirmation_token) is None):
            raise HostRuntimeError("HOST_ACCESS_CARD_DENIED")
        card = self._load(project)
        if (card["card_id"] != card_id
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))):
            raise HostRuntimeError("HOST_ACCESS_CARD_DENIED")
        if card["state"] != "pending":
            raise HostRuntimeError("HOST_ACCESS_CARD_SPENT")
        if host_access._timestamp(card["expires_at"]) <= host_access._utcnow():
            raise HostRuntimeError("HOST_ACCESS_CARD_EXPIRED")
        if card["version"] == 1 or card.get("grant_state_revision") is None:
            if self.access._binding_error(card, self.access._binding(root), request=True):
                raise HostRuntimeError("HOST_ACCESS_CARD_STALE")
        elif not self.access.card_request_valid(
                project, request_id=card["request_id"], lifetime=card["lifetime"],
                grant_state_revision=card["grant_state_revision"]):
            raise HostRuntimeError("HOST_ACCESS_CARD_STALE")
        return card

    def _discard_request(self, project, root, card, *, required=False):
        # HostAccess owns both legacy and installation-wide persistence locks.
        self.access.discard_card_request(
            project, request_id=card["request_id"], required=required)

    @staticmethod
    def _result(card, *, token=None, grant=None):
        public = {key: card[key] for key in (
            "project", "card_id", "revision", "state", "expires_at", "project_root",
            "duration_seconds", "allow_codex",
        )}
        public["request_id"] = card.get("request_id")
        public["lifetime"] = card.get("lifetime", "timed")
        public["grant_state_revision"] = card.get("grant_state_revision")
        public["scope"] = host_access.SCOPE
        public["host_identity"] = host_access._host_identity()
        if grant is not None:
            public["grant"] = grant
        result = {"content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}],
                  "structuredContent": public}
        if token is not None:
            result["_meta"] = {"host_access_confirmation": token}
        return result

    def open(self, project, *, lifetime="timed", duration_seconds=None, allow_codex=False):
        if lifetime == "timed" and duration_seconds is None:
            duration_seconds = host_access.DEFAULT_DURATION_SECONDS
        _options(lifetime, duration_seconds, allow_codex)
        root = self.access._project_root(project)
        with _lock(self._lock_path(project)):
            if self._path(project).exists() or self._path(project).is_symlink():
                old = self._load(project)
                if old["state"] == "pending":
                    self._discard_request(project, root, old)
            opened = self.access.begin_card_request(
                project, lifetime=lifetime, duration_seconds=duration_seconds,
                allow_codex=allow_codex)
            grant = opened["grant"]
            if grant is not None:
                now = host_access._utcnow()
                grant_lifetime = grant.get("lifetime", "timed")
                card = {
                    "project": project, "card_id": secrets.token_hex(16),
                    "request_id": None, "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(
                        seconds=host_access.REQUEST_TTL_SECONDS)).isoformat(),
                    "duration_seconds": (duration_seconds
                                         if grant_lifetime == "timed" else None),
                    "lifetime": grant_lifetime,
                    "allow_codex": grant["allow_codex"], "revision": 1,
                    "state": "approved", "project_root": str(root),
                    "grant_state_revision": grant.get("grant_state_revision"),
                }
                return self._result(card, grant=grant)
            request = opened["request"]
            token = secrets.token_hex(32)
            card = {
                "version": 2, "project": project, "card_id": secrets.token_hex(16),
                "request_id": request["request_id"],
                "created_at": host_access._utcnow().isoformat(),
                "expires_at": request["expires_at"],
                "duration_seconds": duration_seconds, "lifetime": lifetime,
                "allow_codex": allow_codex,
                "revision": 1, "token_digest": _digest(token), "state": "pending",
                "grant_state_revision": request.get("grant_state_revision"),
                **self.access._binding(root),
            }
            _save_private_json(self._path(project), card)
            return self._result(card, token=token)

    def update(self, project, *, card_id, confirmation_token, duration_seconds,
               allow_codex, lifetime=None):
        root = self.access._project_root(project)
        with _lock(self._lock_path(project)):
            card = self._checked(project, root, card_id, confirmation_token)
            lifetime = card.get("lifetime", "timed") if lifetime is None else lifetime
            _options(lifetime, duration_seconds, allow_codex)
            if lifetime != card.get("lifetime", "timed"):
                request = self.access.replace_card_request(
                    project, request_id=card["request_id"], lifetime=lifetime,
                    grant_state_revision=card.get("grant_state_revision"))
                card.update(
                    request_id=request["request_id"], expires_at=request["expires_at"],
                    lifetime=request.get("lifetime", "timed"),
                    grant_state_revision=request.get("grant_state_revision"))
            card.setdefault("grant_state_revision", None)
            token = secrets.token_hex(32)
            card.update(version=2, duration_seconds=duration_seconds, allow_codex=allow_codex,
                        lifetime=lifetime,
                        revision=card["revision"] + 1, token_digest=_digest(token))
            _save_private_json(self._path(project), card)
            return self._result(card, token=token)

    def confirm(self, project, *, card_id, confirmation_token):
        root = self.access._project_root(project)
        with _lock(self._lock_path(project)):
            card = self._checked(project, root, card_id, confirmation_token)
            card["state"] = "spent"
            _save_private_json(self._path(project), card)
            grant = self.access.approve_web(
                project, request_id=card["request_id"],
                duration_seconds=card["duration_seconds"], allow_codex=card["allow_codex"],
                confirmation=project, lifetime=card.get("lifetime", "timed"))
            card["state"] = "approved"
            _save_private_json(self._path(project), card)
            return self._result(card, grant=grant)

    def cancel(self, project, *, card_id, confirmation_token):
        root = self.access._project_root(project)
        with _lock(self._lock_path(project)):
            card = self._checked(project, root, card_id, confirmation_token)
            # Delete the issuer's request first under its lock. A competing admin
            # approval must either win (and make cancellation fail) or find no
            # request. A failed save must leave the card pending and retryable.
            self._discard_request(project, root, card, required=True)
            card["state"] = "cancelled"
            _save_private_json(self._path(project), card)
            return self._result(card)


NAMES = frozenset({"open_host_access_approval", "update_host_access_approval",
                   "confirm_host_access_approval", "cancel_host_access_approval"})
_REQUIRED = {
    "open_host_access_approval": {"project"},
    "update_host_access_approval": {"project", "card_id", "confirmation_token",
                                    "duration_seconds", "allow_codex"},
    "confirm_host_access_approval": {"project", "card_id", "confirmation_token"},
    "cancel_host_access_approval": {"project", "card_id", "confirmation_token"},
}
_OPTIONS = {"duration_seconds", "allow_codex", "lifetime"}


def tool_specs(projects):
    properties = {
        "project": {"type": "string", "enum": list(projects)},
        "card_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "duration_seconds": {"type": ["integer", "null"], "minimum": 1,
                             "maximum": host_access.MAX_DURATION_SECONDS},
        "allow_codex": {"type": "boolean"},
        "lifetime": {"type": "string", "enum": ["timed", "until_revoked"]},
    }
    titles = {"open": "호스트 권한 승인 카드", "update": "호스트 권한 옵션 변경",
              "confirm": "호스트 권한 승인", "cancel": "호스트 권한 요청 취소"}
    for name in sorted(NAMES):
        action = name.split("_", 1)[0]
        opener = action == "open"
        fields = _REQUIRED[name] | (
            _OPTIONS if name in {"open_host_access_approval", "update_host_access_approval"}
            else set())
        metadata = {"ui": {"visibility": ["model"] if opener else ["app"]},
                    "openai/widgetAccessible": not opener,
                    "openai/toolInvocation/invoking": titles[action] + " 중입니다",
                    "openai/toolInvocation/invoked": titles[action] + " 응답을 받았습니다"}
        if opener:
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {
            "name": name, "title": titles[action],
            "description": (
                "Open an explicit user confirmation card for installation-wide "
                "host-user access. Does not grant access or start work. Use only when the "
                "user requests host access/renewal. Project selects cwd, not containment."
                if opener else "App-only callback requiring the hidden, one-use card capability."),
            "inputSchema": {"type": "object", "properties": {
                key: properties[key] for key in sorted(fields)},
                "required": sorted(_REQUIRED[name]), "additionalProperties": False},
            "annotations": {"readOnlyHint": False, "destructiveHint": action == "confirm",
                            "idempotentHint": False, "openWorldHint": True},
            "_meta": metadata,
        }


class HostAccessApprovalTools:
    def __init__(self, projects, access_factory):
        self.projects = projects
        self.access_factory = access_factory
        self.names = NAMES
        self._manager = None

    def validate_arguments(self, name, arguments):
        """Run BEFORE SDK validation, whose error text can echo secret values."""
        required = _REQUIRED.get(name)
        optional = (_OPTIONS
                    if name in {"open_host_access_approval", "update_host_access_approval"}
                    else set())
        if (required is None or not isinstance(arguments, dict)
                or not required.issubset(arguments)
                or not set(arguments).issubset(required | optional)):
            raise HostRuntimeError("INVALID_HOST_ACCESS_CARD_ARGUMENTS")
        project = arguments["project"]
        if not isinstance(project, str) or project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        if name in {"open_host_access_approval", "update_host_access_approval"}:
            lifetime = arguments.get("lifetime", "timed")
            duration = arguments.get(
                "duration_seconds",
                None if lifetime == "until_revoked" else host_access.DEFAULT_DURATION_SECONDS)
            _options(lifetime, duration, arguments.get("allow_codex", False))
        if name != "open_host_access_approval":
            if (not isinstance(arguments["card_id"], str)
                    or _ID.fullmatch(arguments["card_id"]) is None
                    or not isinstance(arguments["confirmation_token"], str)
                    or _TOKEN.fullmatch(arguments["confirmation_token"]) is None):
                raise HostRuntimeError("HOST_ACCESS_CARD_DENIED")

    async def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            if self._manager is None:
                self._manager = HostAccessApprovals(self.access_factory())
            action = name.split("_", 1)[0]
            values = {key: value for key, value in arguments.items() if key != "project"}
            return await asyncio.to_thread(getattr(self._manager, action), arguments["project"], **values)
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except Exception as exc:
            diagnostic_id = uuid.uuid4().hex
            # Never print exception text/arguments: even invalid capabilities can
            # appear there. Keep a correlatable, bounded cause in server stderr.
            print(json.dumps({"component": "host-access-approval", "event": "unexpected-error",
                              "diagnostic_id": diagnostic_id, "operation": name,
                              "exception_type": type(exc).__name__,
                              "errno": exc.errno if isinstance(exc, OSError) else None}),
                  file=sys.stderr, flush=True)
            raise ValueError("HOST_ACCESS_CARD_UNAVAILABLE:" + diagnostic_id) from None
