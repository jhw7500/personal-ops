"""Owner-confirmed Git access: legacy commit-only or explicitly bounded push scope.

Source-only until reviewed and installed through the existing runtime rollout.
Only an authenticated app-only callback issues records. Repository identities,
clocks and execution context are observed by the host, not supplied by a model.
A successful commit advances the bound HEAD without extending either deadline.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import host_access
from host_runtime_config import HostRuntimeError
from git_commit_access_policy import evaluate_stored_commit_grant, MAX_DURATION_SECONDS, MAX_PUSHES, _valid_grant

UI_URI = "ui://git-commit-access/approval-v1.html"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CARD_TTL = 600
MAX_STATE_BYTES = 1024 * 1024
PUBLIC_NAMES = {"open_git_commit_access_approval", "get_git_commit_access"}
NAMES = frozenset(PUBLIC_NAMES | {"confirm_git_commit_access", "cancel_git_commit_access", "revoke_git_commit_access"})
CARD_FIELDS = {"version", "project", "card_id", "context", "duration_seconds", "created_at", "expires_at", "created_monotonic_ns", "expires_monotonic_ns", "token_digest", "intent", "state", "grant_id"}


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class CommitAccess:
    def __init__(self, git, *, trusted_runtime=None, wall_clock=None,
                 monotonic_clock=None, boot_source=None):
        self.git = git
        self.runtime = trusted_runtime or host_access._installed_runtime_identity
        self.wall = wall_clock or (lambda: int(time.time()))
        self.monotonic = monotonic_clock or time.monotonic_ns
        self.boot = boot_source or host_access._current_boot_id
        self.directory = git.state_dir / ".commit-access"
        if not self.directory.exists() and not self.directory.is_symlink():
            self.directory.mkdir(mode=0o700)
        self._directory_safe()

    def _directory_safe(self):
        current = Path(self.directory.anchor)
        for part in self.directory.parts[1:]:
            current /= part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise HostRuntimeError("UNSAFE_GIT_COMMIT_ACCESS_STORE")
        for path in (self.git.state_dir, self.directory):
            info = path.lstat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise HostRuntimeError("UNSAFE_GIT_COMMIT_ACCESS_STORE")

    def _scope(self, project):
        if type(project) is not str or project not in self.git.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")

    def grant_path(self, project):
        self._scope(project)
        return self.directory / (project + ".grant.json")

    def _card_path(self, project):
        self._scope(project)
        return self.directory / (project + ".card.json")

    def _read(self, path):
        self._directory_safe()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > MAX_STATE_BYTES):
                raise HostRuntimeError("UNSAFE_GIT_COMMIT_ACCESS_STATE")
            data = stream.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES:
            raise HostRuntimeError("UNSAFE_GIT_COMMIT_ACCESS_STATE")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate state key")
                result[key] = value
            return result
        return json.loads(data, object_pairs_hook=pairs)

    def _save(self, path, value):
        self._directory_safe()
        self._read(path)
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > MAX_STATE_BYTES:
            raise HostRuntimeError("GIT_COMMIT_ACCESS_STATE_TOO_LARGE")
        temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _host_context(self):
        return {"uid": os.getuid(), "boot_id": self.boot(), "runtime_digest": self.runtime()}

    def _observe(self, project):
        from git_workflows import _git, _decode, PROTECTED_BRANCHES
        self._scope(project)
        root = self.git._project(project)
        git_dir = Path(_decode(_git(root, "rev-parse", "--absolute-git-dir").stdout).strip())
        if git_dir.is_symlink() or git_dir.resolve(strict=True) != git_dir:
            raise HostRuntimeError("UNSAFE_GIT_DIRECTORY")
        repo_stat, git_stat = root.stat(), git_dir.stat()
        return {
            "project": project, "repo": str(root),
            "repo_identity": [repo_stat.st_dev, repo_stat.st_ino],
            "git_dir": str(git_dir), "git_identity": [git_stat.st_dev, git_stat.st_ino],
            "branch": self.git._current_branch(root), "head": self.git._head(root),
            **self._host_context(), "protected_branches": sorted(PROTECTED_BRANCHES),
        }

    @staticmethod
    def _event(record, event, **fields):
        record["events"] = (record["events"] + [{"event": event, **fields}])[-256:]

    def _loaded(self, project):
        record = self._read(self.grant_path(project))
        if record is None:
            return None
        if (type(record) is not dict or set(record) != {"version", "grant", "in_flight", "events"}
                or type(record["version"]) is not int or record["version"] != 1
                or type(record["events"]) is not list or len(record["events"]) > 256):
            raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_STATE")
        return record

    def _completion_settled(self, record, project):
        completed = next((event for event in reversed(record["events"])
                          if event.get("event") in {"committed", "pushed"}), None)
        if completed is None:
            return True
        try:
            settled = self.git._load_plan(project, completed["operation_id"])
            action = "push" if completed["event"] == "pushed" else "commit"
            key = "pushed" if action == "push" else "commit"
            auth = settled.get("authorization", {})
            return (settled["status"] == "completed" and settled["action"] == action
                    and settled["project"] == project
                    and completed.get(key) == record["grant"]["expected_head"]
                    and settled.get("result", {}).get(key) == record["grant"]["expected_head"]
                    and auth.get("kind") == "git_commit_access"
                    and auth.get("grant_id") == record["grant"]["grant_id"])
        except (OSError, ValueError, KeyError, TypeError, HostRuntimeError):
            return False

    def _coverage(self, project, action="commit"):
        context = self._observe(project)
        record = self._loaded(project)
        if record and record["in_flight"] is not None:
            return record, context, "UNFINISHED_COMMIT"
        decision = evaluate_stored_commit_grant(
            record["grant"] if record else None, context, action=action,
            now_epoch_seconds=self.wall(), now_monotonic_ns=self.monotonic())
        if decision.allowed:
            if not self._completion_settled(record, project):
                return record, context, "COMPLETION_UNCERTAIN"
            if record["grant"]["version"] == 2:
                from git_workflows import _git, _decode, _reject_dangerous_git_config
                root = self.git._project(project)
                _reject_dangerous_git_config(root)
                origin = _decode(_git(root, "remote", "get-url", "--push", "origin").stdout).strip()
                if origin != record["grant"]["origin"]:
                    return record, context, "ORIGIN_CHANGED"
        return record, context, decision.reason

    def _remote_binding(self, project, context):
        from git_workflows import _git, _decode, _reject_dangerous_git_config
        root = self.git._project(project)
        _reject_dangerous_git_config(root)
        origin = _decode(_git(root, "remote", "get-url", "--push", "origin").stdout).strip()
        target, exists, remote_head = self.git.remote_adapter(root, origin, context["branch"])
        if (not exists or target != origin or type(remote_head) is not str
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", remote_head) is None
                or _git(root, "merge-base", "--is-ancestor", remote_head, context["head"], check=False).returncode != 0):
            raise HostRuntimeError("GIT_PUSH_SCOPE_UNVERIFIED")
        return {"origin": target, "remote_head": remote_head}

    def preparation_binding(self, project):
        try:
            record, context, reason = self._coverage(project)
            if reason == "ACTIVE" and record["grant"]["version"] == 2:
                return record["grant"]["grant_id"]
        except (OSError, ValueError, KeyError, TypeError, HostRuntimeError):
            pass
        return None

    def _push_plan_covered(self, plan, record, context):
        bound_grant_id = plan["immutable"].get("development_grant_id")
        if ((record["grant"]["version"] == 2 or bound_grant_id is not None)
                and bound_grant_id != record["grant"]["grant_id"]):
            return False
        if plan["action"] != "push":
            return True
        grant, imm = record["grant"], plan["immutable"]
        if (grant.get("version") != 2 or imm.get("new_branch") is not False
                or imm.get("branch") != grant["branch"] or imm.get("remote") != "origin"
                or imm.get("trusted_target") != grant["origin"]
                or imm.get("remote_head") != grant["remote_head"]):
            return False
        return self._remote_binding(plan["project"], context) == {
            "origin": grant["origin"], "remote_head": grant["remote_head"]}

    def status(self, project):
        self._scope(project)
        with self.git._lock(project):
            return self.status_locked(project)

    def status_locked(self, project):
        try:
            record, context, reason = self._coverage(project)
            if reason == "ACTIVE":
                grant = record["grant"]
                public = {"project": project, "active": True, "reason": None,
                        "grant_id": grant["grant_id"], "branch": grant["branch"],
                        "repository": grant["repo"], "operations": list(grant["operations"]),
                        "expected_head": grant["expected_head"],
                        "expires_at_epoch": grant["expires_at"], "expires_at": _iso(grant["expires_at"]),
                        "duration_seconds": grant["duration_seconds"]}
                if grant["version"] == 2:
                    public.update(origin=grant["origin"], push_limit=grant["push_limit"],
                                  pushes_remaining=grant["push_limit"] - grant["pushes_used"])
                return public
            public = {"project": project, "active": False, "reason": reason}
            if reason == "SCOPE_MISMATCH" and record:
                from git_commit_access_policy import _SCOPE_FIELDS
                public["scope_mismatch_fields"] = sorted(
                    key for key in _SCOPE_FIELDS if record["grant"].get(key) != context.get(key))
            return public
        except (OSError, ValueError, TypeError, KeyError, HostRuntimeError):
            return {"project": project, "active": False, "reason": "INVALID_STATE"}

    def public_plan(self, plan):
        if plan["action"] not in {"commit", "push"} or plan["status"] != "pending_host_approval":
            return None
        try:
            record, context, reason = self._coverage(plan["project"], plan["action"])
            if reason != "ACTIVE" or not self._push_plan_covered(plan, record, context):
                return None
            root = self.git.projects[plan["project"]]
            if self.git._snapshot(root, plan["immutable"].get("paths", ())) != plan["immutable"]["snapshot"]:
                return None
            return self.status_locked(plan["project"])
        except (OSError, ValueError, KeyError, TypeError, HostRuntimeError):
            return None

    def begin_locked(self, plan):
        if plan["action"] not in {"commit", "push"}:
            return None
        try:
            record, context, reason = self._coverage(plan["project"], plan["action"])
            if reason != "ACTIVE" or not self._push_plan_covered(plan, record, context):
                return None
            decision = evaluate_stored_commit_grant(
                record["grant"], context, action=plan["action"],
                now_epoch_seconds=self.wall(), now_monotonic_ns=self.monotonic())
            if not decision.allowed:
                return None
        except (OSError, ValueError, TypeError, KeyError, HostRuntimeError):
            return None
        record["in_flight"] = {"operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"], "head": context["head"]}
        self._event(record, "admitted", operation_id=plan["operation_id"], action=plan["action"], at=self.wall())
        self._save(self.grant_path(plan["project"]), record)
        return {"kind": "git_commit_access", "grant_id": record["grant"]["grant_id"], "expires_at": _iso(record["grant"]["expires_at"])}

    def finish_locked(self, plan, result):
        from git_workflows import _git, _decode
        project = plan["project"]
        record = self._loaded(project)
        if (not record or record["in_flight"] != {"operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"], "head": record["grant"]["expected_head"]}):
            raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
        context = self._observe(project)
        grant = record["grant"]
        for key in ("project", "repo", "repo_identity", "git_dir", "git_identity", "branch", "uid", "boot_id", "runtime_digest"):
            if context[key] != grant[key]:
                raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
        if plan["action"] == "push":
            if (grant["version"] != 2 or result.get("pushed") != grant["expected_head"]
                    or result.get("pushed") != context["head"] or result.get("branch") != grant["branch"]
                    or grant["pushes_used"] >= grant["push_limit"]):
                raise HostRuntimeError("GIT_PUSH_COMPLETION_UNCERTAIN")
            observed = self._remote_binding(project, context)
            if observed != {"origin": grant["origin"], "remote_head": context["head"]}:
                raise HostRuntimeError("GIT_PUSH_COMPLETION_UNCERTAIN")
            grant["remote_head"] = context["head"]
            grant["pushes_used"] += 1
            record["in_flight"] = None
            self._event(record, "pushed", operation_id=plan["operation_id"], pushed=context["head"], at=self.wall())
            self._save(self.grant_path(project), record)
            return
        new_head = result.get("commit")
        if new_head != context["head"] or new_head == grant["expected_head"]:
            raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
        parents = _decode(_git(Path(context["repo"]), "rev-list", "--parents", "-n", "1", new_head).stdout).split()
        if parents != [new_head, grant["expected_head"]]:
            raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
        grant["expected_head"] = new_head
        record["in_flight"] = None
        self._event(record, "committed", operation_id=plan["operation_id"], commit=new_head, at=self.wall())
        self._save(self.grant_path(project), record)

    def fail_locked(self, plan):
        record = self._loaded(plan["project"])
        if not record:
            return
        authorization = plan.get("authorization", {})
        same_grant = (authorization.get("kind") == "git_commit_access"
                      and authorization.get("grant_id") == record["grant"].get("grant_id"))
        same_admission = (record["in_flight"] is not None
                          and record["in_flight"].get("operation_id") == plan["operation_id"])
        same_completion = (record["in_flight"] is None and same_grant
                           and bool(record["events"])
                           and record["events"][-1].get("event") in {"committed", "pushed"}
                           and record["events"][-1].get("operation_id") == plan["operation_id"])
        if same_admission or same_completion:
            record["grant"]["state"] = "revoked"
            self._event(record, "execution_failed", operation_id=plan["operation_id"], at=self.wall())
            self._save(self.grant_path(plan["project"]), record)

    def _public_card(self, card, state, *, grant=None):
        context = card["context"]
        public = {"component": "git_commit_access", "project": card["project"], "card_id": card["card_id"],
                  "state": state, "repository": context["repo"], "branch": context["branch"],
                  "head": context["head"], "operations": card.get("permissions", {}).get("operations", ["commit"]), "duration_seconds": card["duration_seconds"],
                  "expires_at": _iso(card["expires_at"]), "executes_on_approval": False}
        if card.get("version") == 2:
            permissions = card["permissions"]
            public.update(permission_version=2, origin=permissions["origin"], push_limit=permissions["push_limit"])
        if grant:
            public.update(grant_id=grant["grant_id"], grant_expires_at=_iso(grant["expires_at"]))
            if grant["version"] == 2:
                public["pushes_remaining"] = grant["push_limit"] - grant["pushes_used"]
        return public

    @staticmethod
    def _result(public, token=None):
        result = {"content": [{"type": "text", "text": json.dumps(public, ensure_ascii=False)}], "structuredContent": public}
        if token is not None:
            result["_meta"] = {"git_commit_access_confirmation": token}
        return result

    def _replacement_reason(self, record, context):
        """Explicit owner renewal only; never silently revive old authority."""
        if not record or record["in_flight"] is not None:
            return None
        grant = record["grant"]
        fixed = ("project", "repo", "repo_identity", "git_dir", "git_identity", "branch", "uid")
        if any(grant.get(key) != context.get(key) for key in fixed):
            return None
        if context["branch"] in {"main", "master", *context["protected_branches"]}:
            return None
        if not self._completion_settled(record, context["project"]):
            return None
        if grant["runtime_digest"] != context["runtime_digest"]:
            return "RUNTIME_CHANGED"
        if grant["boot_id"] != context["boot_id"]:
            return "BOOT_CHANGED"
        return None

    def open(self, project, duration_seconds=MAX_DURATION_SECONDS, allow_push=False, push_limit=3):
        self._scope(project)
        if (type(duration_seconds) is not int or not 1 <= duration_seconds <= MAX_DURATION_SECONDS
                or type(allow_push) is not bool or type(push_limit) is not int
                or not 1 <= push_limit <= MAX_PUSHES):
            raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_DURATION_OR_PUSH_SCOPE")
        with self.git._lock(project):
            record = self._loaded(project)
            stored = record["grant"] if record else None
            if stored is not None and (not _valid_grant(stored) or stored["project"] != project):
                raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_STATE")
            identity = self._host_context()
            now, mono = self.wall(), self.monotonic()
            expired = stored is not None and (now >= stored["expires_at"] or
                stored["boot_id"] == identity["boot_id"] and mono >= stored["expires_monotonic_ns"])
            grant = stored if stored is not None and stored["state"] == "active" and not expired else None
            active, replacement = grant is not None, None
            usable = False
            current = None
            if active:
                try:
                    current = self._observe(project)
                    usable = self.status_locked(project)["active"]
                    replacement = self._replacement_reason(record, current)
                    if allow_push and grant["version"] == 1 and usable and self._completion_settled(record, project):
                        replacement = "PUSH_SCOPE_UPGRADE"
                except (OSError, ValueError):
                    replacement = None
                if allow_push and current is not None and current["branch"] in {"main", "master", *current["protected_branches"]}:
                    raise HostRuntimeError("PROTECTED_BRANCH")
                if replacement:
                    context = current
                    active = False
            if active:
                from git_workflows import PROTECTED_BRANCHES
                context = {key: grant[key] for key in
                           ("project", "repo", "repo_identity", "git_dir", "git_identity", "branch")}
                context.update(identity, head=grant["expected_head"], protected_branches=sorted(PROTECTED_BRANCHES))
            else:
                if record and (record["in_flight"] is not None or not self._completion_settled(record, project)):
                    raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
                context = self._observe(project)
                if context["branch"] in {"main", "master", *context["protected_branches"]}:
                    raise HostRuntimeError("PROTECTED_BRANCH")
            token = secrets.token_hex(32)
            card = {"version": 1, "project": project, "card_id": secrets.token_hex(16), "context": context,
                    "duration_seconds": grant["duration_seconds"] if active else duration_seconds,
                    "created_at": now, "expires_at": now + CARD_TTL,
                    "created_monotonic_ns": mono, "expires_monotonic_ns": mono + CARD_TTL * 1000000000,
                    "token_digest": _digest(token),
                    "intent": ("replace_" + replacement.lower()) if replacement else ("revoke" if active else "approve"),
                    "state": "pending", "grant_id": grant["grant_id"] if grant else None}
            if active and grant["version"] == 2:
                card.update(version=2, permissions={"operations": ["commit", "push"],
                    "origin": grant["origin"], "remote_head": grant["remote_head"], "push_limit": grant["push_limit"]})
            elif not active and allow_push:
                card.update(version=2, permissions={"operations": ["commit", "push"],
                    **self._remote_binding(project, context), "push_limit": push_limit})
            self._save(self._card_path(project), card)
            public = self._public_card(card, "active" if active else "pending", grant=grant if active else None)
            if replacement:
                public.update(renewal_reason=replacement, replaces_grant_id=grant["grant_id"])
            if active:
                public["usable_for_current_context"] = usable
            return self._result(public, token)

    def _checked(self, project, card_id, confirmation_token, *, require_context=True):
        self._scope(project)
        if (type(card_id) is not str or not _ID.fullmatch(card_id)
                or type(confirmation_token) is not str or not _DIGEST.fullmatch(confirmation_token)):
            raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_DENIED")
        card = self._read(self._card_path(project))
        version = card.get("version") if type(card) is dict else None
        fields = CARD_FIELDS | ({"permissions"} if version == 2 else set())
        if (type(card) is not dict or type(version) is not int or version not in (1, 2) or set(card) != fields
                or card.get("project") != project or card.get("card_id") != card_id
                or type(card.get("token_digest")) is not str
                or not hmac.compare_digest(card["token_digest"], _digest(confirmation_token))
                or card.get("state") != "pending"):
            raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_DENIED")
        if version == 2:
            p = card["permissions"]
            if (type(p) is not dict or set(p) != {"operations", "origin", "remote_head", "push_limit"}
                    or p["operations"] != ["commit", "push"] or type(p["origin"]) is not str
                    or not p["origin"].isprintable() or not 1 < len(p["origin"]) <= 4096
                    or type(p["remote_head"]) is not str
                    or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", p["remote_head"]) is None
                    or type(p["push_limit"]) is not int or not 1 <= p["push_limit"] <= MAX_PUSHES):
                raise HostRuntimeError("GIT_PUSH_CARD_SCOPE_INVALID")
        now, mono = self.wall(), self.monotonic()
        if not (card["created_at"] <= now < card["expires_at"] and card["created_monotonic_ns"] <= mono < card["expires_monotonic_ns"]):
            raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_EXPIRED")
        if require_context:
            if self._observe(project) != card["context"]:
                raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_STALE")
        elif any(value != card["context"][key] for key, value in self._host_context().items()):
            raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_STALE")
        return card

    def confirm(self, project, *, card_id, confirmation_token):
        self._scope(project)
        with self.git._lock(project):
            card = self._checked(project, card_id, confirmation_token)
            replacing = card["intent"] in {"replace_runtime_changed", "replace_boot_changed", "replace_push_scope_upgrade"}
            if card["intent"] != "approve" and not replacing:
                raise HostRuntimeError("GIT_COMMIT_ACCESS_CONFIRM_NOT_PENDING")
            context = card["context"]
            previous = self._loaded(project)
            if previous and (previous["in_flight"] is not None or not self._completion_settled(previous, project)):
                raise HostRuntimeError("GIT_COMMIT_ACCESS_COMPLETION_UNCERTAIN")
            if replacing:
                if (not previous or previous["grant"].get("grant_id") != card["grant_id"]
                        or previous["grant"]["state"] != "active"):
                    raise HostRuntimeError("GIT_COMMIT_ACCESS_REPLACEMENT_STALE")
                if card["intent"] == "replace_push_scope_upgrade":
                    if previous["grant"]["version"] != 1 or not self.status_locked(project)["active"]:
                        raise HostRuntimeError("GIT_COMMIT_ACCESS_REPLACEMENT_STALE")
                elif self._replacement_reason(previous, context) != card["intent"][8:].upper():
                    raise HostRuntimeError("GIT_COMMIT_ACCESS_REPLACEMENT_STALE")
            if card["version"] == 2:
                observed = self._remote_binding(project, context)
                if observed != {k:card["permissions"][k] for k in ("origin", "remote_head")}:
                    raise HostRuntimeError("GIT_PUSH_CARD_SCOPE_CHANGED")
            now, mono, duration = self.wall(), self.monotonic(), card["duration_seconds"]
            grant = {k: v for k, v in context.items() if k not in {"head", "protected_branches"}}
            grant.update(version=1, grant_id=secrets.token_hex(16), state="active", operations=["commit"],
                         expected_head=context["head"], approved_at=now, expires_at=now + duration,
                         approved_monotonic_ns=mono, expires_monotonic_ns=mono + duration * 1000000000,
                         duration_seconds=duration)
            if card["version"] == 2:
                grant.update(version=2, operations=["commit", "push"], origin=card["permissions"]["origin"],
                             remote_head=card["permissions"]["remote_head"], push_limit=card["permissions"]["push_limit"],
                             pushes_used=0)
            if not evaluate_stored_commit_grant(grant, context, action="commit", now_epoch_seconds=now, now_monotonic_ns=mono).allowed:
                raise HostRuntimeError("GIT_COMMIT_ACCESS_INVALID_GRANT")
            card["state"] = "spent"
            self._save(self._card_path(project), card)
            record = {"version": 1, "grant": grant, "in_flight": None, "events": [{"event": "approved", "at": now}]}
            self._save(self.grant_path(project), record)
            return self._result(self._public_card(card, "active", grant=grant))

    def cancel(self, project, *, card_id, confirmation_token):
        self._scope(project)
        with self.git._lock(project):
            card = self._checked(project, card_id, confirmation_token, require_context=False)
            card["state"] = "cancelled"
            self._save(self._card_path(project), card)
            return self._result(self._public_card(card, "cancelled"))

    def revoke(self, project, *, card_id, confirmation_token):
        self._scope(project)
        with self.git._lock(project):
            card = self._checked(project, card_id, confirmation_token, require_context=False)
            record = self._loaded(project)
            if card["intent"] != "revoke" or not record or record["grant"].get("grant_id") != card["grant_id"]:
                raise HostRuntimeError("GIT_COMMIT_ACCESS_REVOKE_DENIED")
            card["state"] = "spent"
            self._save(self._card_path(project), card)
            record["grant"]["state"] = "revoked"
            self._event(record, "revoked", at=self.wall())
            self._save(self.grant_path(project), record)
            return self._result(self._public_card(card, "revoked", grant=record["grant"]))


def tool_specs(projects):
    for name in sorted(NAMES):
        public = name in PUBLIC_NAMES
        fields = {"project": {"type": "string", "enum": sorted(projects)}}
        required = ["project"]
        if name == "open_git_commit_access_approval":
            fields["duration_seconds"] = {"type": "integer", "minimum": 1, "maximum": MAX_DURATION_SECONDS, "default": MAX_DURATION_SECONDS}
            fields["allow_push"] = {"type": "boolean", "default": False}
            fields["push_limit"] = {"type": "integer", "minimum": 1, "maximum": MAX_PUSHES, "default": 3}
        elif not public:
            fields.update(card_id={"type": "string", "pattern": "^[0-9a-f]{32}$"}, confirmation_token={"type": "string", "pattern": "^[0-9a-f]{64}$"})
            required += ["card_id", "confirmation_token"]
        metadata = {"ui": {"visibility": ["model", "app"] if name == "get_git_commit_access" else (["model"] if public else ["app"])},
                    "openai/widgetAccessible": name == "get_git_commit_access" or not public}
        if name == "open_git_commit_access_approval":
            metadata["ui"]["resourceUri"] = UI_URI
            metadata["openai/outputTemplate"] = UI_URI
        yield {"name": name, "title": "저장소·브랜치 한정 Git 개발 권한",
               "description": ("Open owner confirmation for up to 24 hours of local commits on this exact repository and feature branch. Optional allow_push=true explicitly includes at most push_limit pushes to its verified existing origin branch. Default is commit-only. Never grants reset, amend, force push, protected-branch writes, DB or runtime access. Opening never approves or executes." if name.startswith("open_") else "Read commit-only grant state." if name.startswith("get_") else "App-only owner commit access confirmation or revocation; never executes Git."),
               "inputSchema": {"type": "object", "properties": fields, "required": required, "additionalProperties": False},
               "annotations": {"readOnlyHint": name.startswith("get_"), "destructiveHint": False, "openWorldHint": False}, "_meta": metadata}


class GitCommitAccessTools:
    def __init__(self, git):
        self.git = git
        self.manager = git.commit_access
        self.names = NAMES

    def validate_arguments(self, name, arguments):
        if name not in NAMES or type(arguments) is not dict:
            raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_ARGUMENTS")
        allowed = {"project"}
        required = {"project"}
        if name == "open_git_commit_access_approval":
            allowed |= {"duration_seconds", "allow_push", "push_limit"}
            duration = arguments.get("duration_seconds", MAX_DURATION_SECONDS)
            if (type(duration) is not int or not 1 <= duration <= MAX_DURATION_SECONDS
                    or type(arguments.get("allow_push", False)) is not bool
                    or type(arguments.get("push_limit", 3)) is not int
                    or not 1 <= arguments.get("push_limit", 3) <= MAX_PUSHES):
                raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_DURATION")
        elif name not in PUBLIC_NAMES:
            required |= {"card_id", "confirmation_token"}
            allowed |= required
            if (type(arguments.get("card_id")) is not str or not _ID.fullmatch(arguments["card_id"])
                    or type(arguments.get("confirmation_token")) is not str or not _DIGEST.fullmatch(arguments["confirmation_token"])):
                raise HostRuntimeError("GIT_COMMIT_ACCESS_CARD_DENIED")
        if set(arguments) - allowed or not required <= set(arguments):
            raise HostRuntimeError("INVALID_GIT_COMMIT_ACCESS_ARGUMENTS")
        self.manager._scope(arguments.get("project"))

    async def call(self, name, arguments):
        self.validate_arguments(name, arguments)
        method = "status" if name == "get_git_commit_access" else name.split("_", 1)[0]
        try:
            result = await asyncio.to_thread(getattr(self.manager, method), **arguments)
            return self.manager._result(result) if method == "status" else result
        except HostRuntimeError as exc:
            raise ValueError(exc.code) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("GIT_COMMIT_ACCESS_UNAVAILABLE") from None
