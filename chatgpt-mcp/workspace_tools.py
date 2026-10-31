"""Explicit workspace access and registered, bounded JSON artifact reads.

Confirmation capabilities belong to the trusted MCP app host, not model text.
An installation is the authority boundary; a project/session label is not a
separately authenticated person. No method here initializes owner authority.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time

from workspace_authority import WorkspaceAuthority
from workspace_paths import WorkspaceError, is_sensitive, open_directory, read_file, write_file
from workspace_policy import WorkspacePolicy


UI_URI = "ui://workspace-access/approval-v1.html"
PAGE_BYTES = 65536
MAX_FILE_BYTES = 1048576
MAX_LIST_ENTRIES = 2000
_SECRET_KEYS = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|private[_-]?key|client[_-]?secret)\Z", re.I)
_SECRET_TEXT = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~-]{12,}|\bsk-[A-Za-z0-9_-]{16,}")


def result(payload, *, error=False, meta=None):
    value = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
             "structuredContent": payload, "isError": error}
    if meta:
        value["_meta"] = meta
    return value


def _contains_secret(value):
    if isinstance(value, dict):
        return any(_SECRET_KEYS.fullmatch(key) or _contains_secret(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and bool(_SECRET_TEXT.search(value))


def _properties(extra, required=()):
    return {"type": "object", "properties": {"project": {"type": "string"}, **extra},
            "required": ["project", *required], "additionalProperties": False}


def tool_specs(projects):
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    cursor = {"type": "string", "maxLength": 4096}
    grant = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
    paging = {"max_bytes": {"type": "integer", "minimum": 4, "maximum": PAGE_BYTES},
              "cursor": cursor}
    definitions = [
        ("read_registered_artifact", "등록 산출물 읽기", "Read only owner-registered JSON fields, preserving Git ignore. Bounded signed pages; no regeneration or model call.",
         {"relative_path": path, "grant_id": grant, **paging}, ("relative_path",), True, False),
        ("list_registered_artifacts", "등록 산출물 목록", "List metadata only for a registered directory with list permission. Listing never grants content read.",
         {"relative_path": path, "grant_id": grant}, ("relative_path",), True, False),
        ("read_workspace_file", "작업 영역 파일 읽기", "Read an explicitly requested non-sensitive file. Outside the owner base requires a matching unexpired read grant; never grants execution/network access.",
         {"path": path, "grant_id": grant, **paging}, ("path",), True, False),
        ("list_workspace_files", "작업 영역 목록", "List bounded metadata for an explicitly requested directory; outside base requires list permission separately from read.",
         {"path": path, "grant_id": grant}, ("path",), True, False),
        ("write_workspace_file", "작업 영역 파일 저장", "Create or CAS-replace one non-sensitive file. Outside base requires write permission; does not run commands or publish externally.",
         {"path": path, "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
          "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
          "create": {"type": "boolean", "default": False}, "grant_id": grant},
         ("path", "content"), False, False),
        ("open_workspace_access_approval", "경로 접근 승인 요청", "Show exact path, operations, purpose and duration for owner confirmation. Opening requests access only. Installation scope; not authenticated per-person identity.",
         {"path": path, "operations": {"type": "array", "items": {"enum": ["list", "read", "write"]},
                                          "minItems": 1, "maxItems": 3, "uniqueItems": True},
          "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 2592000,
                               "default": 2592000},
          "purpose": {"type": "string", "minLength": 1, "maxLength": 300}},
         ("path", "operations", "purpose"), False, False),
        ("confirm_workspace_access_approval", "표시한 경로 접근 승인", "Trusted app callback only. Confirm exactly the privately bound request; no command is started.",
         {"request_id": grant, "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
         ("request_id", "confirmation_token"), False, True),
        ("cancel_workspace_access_approval", "경로 승인 요청 취소", "Trusted app callback only. Cancel this pending request without revoking unrelated authority.",
         {"request_id": grant, "confirmation_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
         ("request_id", "confirmation_token"), False, True),
    ]
    specs = []
    for name, title, description, properties, required, readonly, app in definitions:
        schema = _properties(properties, required)
        schema["properties"]["project"]["enum"] = list(projects)
        meta = {"ui": {"visibility": ["app"] if app else ["model"]},
                "openai/widgetAccessible": app}
        if name == "open_workspace_access_approval":
            meta["ui"]["resourceUri"] = UI_URI
            meta["openai/outputTemplate"] = UI_URI
        specs.append({"name": name, "title": title, "description": description,
                      "inputSchema": schema,
                      "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                                      "idempotentHint": readonly, "openWorldHint": False}, "_meta": meta})
    return specs


NAMES = frozenset(spec["name"] for spec in tool_specs({}))


class WorkspaceTools:
    def __init__(self, projects, state_dir=None, *, config_path=None):
        self.projects = projects
        self.policy = WorkspacePolicy(projects, config_path=config_path)
        self.authority = WorkspaceAuthority(projects, state_dir=state_dir)
        self.specs = {spec["name"]: spec for spec in tool_specs(projects)}

    def check_permanent(self, target, snapshot):
        """Owner control data stays outside every model path grant."""
        target = Path(target)
        controls = (*snapshot.protected_paths, self.authority.state_dir.parent,
                    Path.home() / ".local/lib/chatgpt-coding-mcp")
        if any(target == root or target.is_relative_to(root) for root in controls):
            raise WorkspaceError("PROTECTED_PATH")
        module_dir = Path(__file__).resolve().parent
        if (module_dir / "manifest.json").exists() and (
                target == module_dir or target.is_relative_to(module_dir)):
            raise WorkspaceError("PROTECTED_PATH")
        for ancestor in (target, *target.parents):
            if ((ancestor / "controller-manifest.json").exists()
                    or ((ancestor / "manifest.json").exists()
                        and (ancestor / "server.py").exists()
                        and (ancestor / "workspace_control.py").exists())):
                raise WorkspaceError("PROTECTED_PATH")
        return target

    @property
    def enabled(self):
        # Deleting policy after activation must not silently restore legacy access.
        path = self.policy.config_path
        return (path.exists() or path.is_symlink() or self.authority.state_dir.exists()
                or self.authority.state_dir.is_symlink()
                or self.authority.enforcement_path.exists() or self.authority.enforcement_path.is_symlink())

    def _context(self):
        snapshot = self.policy.load(required=True)
        return snapshot, self.authority.binding(snapshot.digest)

    def _current(self, snapshot):
        if self.policy.load(required=True).digest != snapshot.digest:
            raise WorkspaceError("WORKSPACE_POLICY_STALE")
        return self.authority.binding(snapshot.digest)

    def _target(self, snapshot, project, path, operation, grant_id=None):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.projects[project] / candidate
        self.check_permanent(candidate, snapshot)
        try:
            return self.policy.check(project, path, operation, snapshot=snapshot)
        except WorkspaceError as exc:
            if exc.code != "WORKSPACE_SCOPE_REQUIRED":
                raise
            if grant_id is None:
                raise
            target = Path(path)
            if not target.is_absolute() or ".." in target.parts:
                raise WorkspaceError("WORKSPACE_PATH_INVALID") from None
            self.authority.require(project, str(target), operation, grant_id, snapshot.digest)
            return target

    def ordinary_path(self, snapshot, project, target, *, is_directory=False):
        """A broad base/path grant must not bypass artifact field restrictions."""
        from workspace_files import is_ignored
        for name, root in self.projects.items():
            if not target.is_relative_to(root):
                continue
            relative = target.relative_to(root).as_posix()
            try:
                self.policy.artifact_rule(snapshot, name, relative)
            except WorkspaceError as exc:
                if exc.code != "ARTIFACT_NOT_REGISTERED":
                    raise
            else:
                raise WorkspaceError("WORKSPACE_ARTIFACT_REQUIRED")
            if is_ignored(root, relative, is_directory=is_directory):
                raise WorkspaceError("WORKSPACE_ARTIFACT_REQUIRED")
        return target

    def file_tool(self, name, arguments):
        from workspace_files import WorkspaceFiles
        return WorkspaceFiles(self).call(name, arguments)

    def _page(self, project, path, file, data, binding, *, max_bytes=PAGE_BYTES, cursor=None):
        if type(max_bytes) is not int or not 4 <= max_bytes <= PAGE_BYTES:
            raise WorkspaceError("WORKSPACE_PAGE_LIMIT")
        key = self.authority.cursor_key(binding["policy_digest"])
        expected = {"project": project, "path": path, **binding,
                    "file_sha256": file.sha256, "content_sha256": hashlib.sha256(data).hexdigest(),
                    "identity": list(file.identity)}
        offset = 0
        expires = int(time.time()) + 1800
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ValueError
                encoded, signature = cursor.split(".")
                raw = base64.urlsafe_b64decode(encoded.encode())
                if not hmac.compare_digest(hmac.new(key, raw, hashlib.sha256).hexdigest(), signature):
                    raise ValueError
                saved = json.loads(raw)
                if ({k: saved[k] for k in expected} != expected or
                        set(saved) != set(expected) | {"offset", "expires"} or
                        type(saved["offset"]) is not int or not 0 <= saved["offset"] < len(data) or
                        type(saved["expires"]) is not int or saved["expires"] <= time.time()):
                    raise ValueError
                offset, expires = saved["offset"], saved["expires"]
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise WorkspaceError("WORKSPACE_CURSOR_STALE") from None
        end = min(len(data), offset + max_bytes)
        try:
            text = data[offset:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                raise WorkspaceError("WORKSPACE_BINARY_FILE") from None
            end = offset + exc.start
            text = data[offset:end].decode("utf-8")
        more = end < len(data)
        next_cursor = None
        if more:
            raw = json.dumps({**expected, "offset": end, "expires": expires},
                             sort_keys=True, separators=(",", ":")).encode()
            next_cursor = (base64.urlsafe_b64encode(raw).decode() + "." +
                           hmac.new(key, raw, hashlib.sha256).hexdigest())
        return {"status": "ok", "project": project, "path": path, "content": text,
                "file_sha256": file.sha256, "content_sha256": expected["content_sha256"],
                "file_size": file.size, "total_bytes": len(data), "offset": offset,
                "next_offset": end, "next_cursor": next_cursor, "has_more": more,
                "truncated": more, "policy_version": 1, **binding}

    def _artifact(self, snapshot, binding, project, arguments):
        relative = arguments["relative_path"]
        rule = self.policy.artifact_rule(snapshot, project, relative)
        if "read" not in rule["operations"]:
            raise WorkspaceError("WORKSPACE_ARTIFACT_DENIED")
        self._target(snapshot, project, str(self.projects[project] / relative), "read", arguments.get("grant_id"))
        file = read_file(self.projects[project], relative,
                         max_bytes=min(rule["max_file_bytes"], MAX_FILE_BYTES))
        try:
            value = json.loads(file.data.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            selected = {key: value[key] for key in rule["fields"] if key in value}
            if not selected:
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise WorkspaceError("WORKSPACE_ARTIFACT_FORMAT") from None
        if _contains_secret(selected):
            raise WorkspaceError("WORKSPACE_SENSITIVE_CONTENT")
        data = json.dumps(selected, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode()
        return self._page(project, relative, file, data, binding,
                          max_bytes=arguments.get("max_bytes", PAGE_BYTES), cursor=arguments.get("cursor"))

    def _listing(self, project, target, snapshot, *, artifact=False, grant_id=None):
        fd = open_directory(target)
        entries, omitted = [], 0
        try:
            with os.scandir(fd) as iterator:
                for index, item in enumerate(iterator):
                    if index >= MAX_LIST_ENTRIES:
                        raise WorkspaceError("WORKSPACE_DIRECTORY_LIMIT")
                    if is_sensitive(item.name):
                        omitted += 1
                        continue
                    info = item.stat(follow_symlinks=False)
                    regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    directory = stat.S_ISDIR(info.st_mode)
                    if not regular and not directory:
                        omitted += 1
                        continue
                    path = target / item.name
                    display = path.relative_to(self.projects[project]).as_posix() if artifact else str(path)
                    try:
                        self.check_permanent(path, snapshot)
                        if artifact:
                            self._target(snapshot, project, str(path), "list", grant_id)
                        else:
                            self.ordinary_path(snapshot, project, path, is_directory=directory)
                    except WorkspaceError:
                        omitted += 1
                        continue
                    if artifact:
                        if not regular:
                            omitted += 1
                            continue
                        try:
                            rule = self.policy.artifact_rule(snapshot, project, display)
                            if "list" not in rule["operations"]:
                                raise WorkspaceError("WORKSPACE_ARTIFACT_DENIED")
                        except WorkspaceError:
                            omitted += 1
                            continue
                    entries.append({"path": display, "size": info.st_size,
                                    "type": "file" if regular else "directory"})
        finally:
            os.close(fd)
        return {"status": "ok", "project": project, "files": sorted(entries, key=lambda x: x["path"]),
                "file_count": len(entries), "omitted_entries": omitted, "truncated": False}

    def _dispatch(self, name, args):
        project = args["project"]
        snapshot, binding = self._context()
        approval = name in {"open_workspace_access_approval", "confirm_workspace_access_approval",
                            "cancel_workspace_access_approval"}
        # Issuers acquire an exclusive state lock themselves. Never try to
        # upgrade a read admission lock while issuing or consuming a capability.
        admission = nullcontext(binding) if approval else self.authority.admission(snapshot.digest)
        with admission as binding:
            if name == "read_registered_artifact":
                payload = self._artifact(snapshot, binding, project, args)
            elif name == "list_registered_artifacts":
                relative = args["relative_path"]
                rule = self.policy.artifact_rule(snapshot, project, relative)
                if "list" not in rule["operations"] or rule["kind"] != "directory":
                    raise WorkspaceError("WORKSPACE_ARTIFACT_DENIED")
                target = self._target(snapshot, project, str(self.projects[project] / relative), "list", args.get("grant_id"))
                payload = self._listing(project, target, snapshot, artifact=True, grant_id=args.get("grant_id"))
            elif name in {"read_workspace_file", "list_workspace_files", "write_workspace_file"}:
                operation = {"read_workspace_file": "read", "list_workspace_files": "list",
                             "write_workspace_file": "write"}[name]
                target = self._target(snapshot, project, args["path"], operation, args.get("grant_id"))
                self.ordinary_path(snapshot, project, target)
                if operation == "read":
                    file = read_file(target.parent, target.name, max_bytes=MAX_FILE_BYTES)
                    if _SECRET_TEXT.search(file.data.decode("utf-8")):
                        raise WorkspaceError("WORKSPACE_SENSITIVE_CONTENT")
                    payload = self._page(project, str(target), file, file.data, binding,
                                         max_bytes=args.get("max_bytes", PAGE_BYTES), cursor=args.get("cursor"))
                elif operation == "list":
                    payload = self._listing(project, target, snapshot)
                else:
                    payload = write_file(target.parent, target.name, args["content"].encode(),
                                         expected_sha256=args.get("expected_sha256"),
                                         create=args.get("create", False))
                    payload = {"status": "ok", "project": project, **payload}
            elif name == "open_workspace_access_approval":
                # Check permanent secret/policy denies before issuing any request.
                for operation in args["operations"]:
                    try:
                        candidate = Path(args["path"])
                        if not candidate.is_absolute():
                            candidate = self.projects[project] / candidate
                        self.check_permanent(candidate, snapshot)
                        self.policy.check(project, args["path"], operation, snapshot=snapshot)
                    except WorkspaceError as exc:
                        if exc.code != "WORKSPACE_SCOPE_REQUIRED":
                            raise
                request = self.authority.request(project, args["path"], args["operations"],
                                                 args.get("duration_seconds", 2592000), args["purpose"],
                                                 snapshot.digest)
                token = request.pop("_confirmation")
                self._current(snapshot)
                return result({"status": "pending", "scope": "installation", **request},
                              meta={"workspace_confirmation": token})
            elif name == "confirm_workspace_access_approval":
                payload = self.authority.confirm(project, args["request_id"],
                                                 args["confirmation_token"], snapshot.digest)
                payload = {**payload, "status": "approved", "scope": "installation"}
            elif name == "cancel_workspace_access_approval":
                payload = self.authority.cancel(project, args["request_id"],
                                                args["confirmation_token"], snapshot.digest)
                payload = {"status": "cancelled", **(payload or {})}
            else:
                raise WorkspaceError("WORKSPACE_TOOL_UNKNOWN")
            self._current(snapshot)
            return result(payload)

    def call(self, name, arguments):
        try:
            self.validate_arguments(name, arguments)
            lock = (self.authority.project_lock(arguments["project"])
                    if name == "write_workspace_file" else nullcontext())
            with lock:
                return self._dispatch(name, dict(arguments))
        except WorkspaceError as exc:
            return result({"status": "denied", "code": exc.code}, error=True)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return result({"status": "denied", "code": "WORKSPACE_OPERATION_FAILED"}, error=True)

    def validate_arguments(self, name, arguments):
        if name not in self.specs or not isinstance(arguments, dict):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        from jsonschema import Draft202012Validator
        if not Draft202012Validator(self.specs[name]["inputSchema"]).is_valid(arguments):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
