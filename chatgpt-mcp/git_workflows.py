#!/usr/bin/env python3
"""Constrained Git workflows for allowlisted host repositories.

The MCP process may prepare operations, but commit and push require a separate
interactive host approval receipt. Pull request and merge operations are handed
off to the repository's native review workflows instead of invoking ``gh``.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable

from workspace_paths import WorkspaceError


GIT = "/usr/bin/git"
GH = "/usr/bin/gh"
CHANGE_EVIDENCE_VALIDATOR = Path(__file__).with_name("validate_change_evidence.py")
CHANGE_EVIDENCE_VALIDATOR_BLOB = "4c07bbd2d36da0e626e8ac8d82ef8336f0272f5f"
MAX_CAPTURE = 512 * 1024
MAX_OPERATIONS = 1000
MAX_TEXT_BYTES = 64 * 1024
MAX_PATHS = 128
MAX_PUSH_COMMITS = 128
MAX_PUSH_PATHS = 128
COMMAND_TIMEOUT = 60.0
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PROTECTED_BRANCHES = frozenset({"main", "master"})
CHANGE_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*"
)
PR_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$"
)
BRANCH_RE = re.compile(r"^(?![./])(?!.*(?:\.\.|//|@\{|\\|[ ~^:?*\[]))(?!.*[/.]$)[A-Za-z0-9._/-]+$")


def _native_workflow_skills() -> tuple[dict[str, Path], list[str]]:
    skills = {
        "jhw-pr": Path.home() / ".codex/skills/jhw-pr/SKILL.md",
        "pre-pr-tribunal": Path.home() / ".codex/skills/pre-pr-tribunal/SKILL.md",
    }
    missing = [name for name, path in skills.items()
               if not path.is_file() or path.is_symlink()]
    return skills, missing


def _tool(name: str, title: str, description: str, properties: dict, required=(), *, read_only=False):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": read_only,
            "openWorldHint": name in {"prepare_git_operation", "execute_git_operation"},
        },
    }


_PROJECT = {"type": "string", "description": "Allowlisted repository name."}
_PATHS = {
    "type": "array", "items": {"type": "string", "minLength": 1},
    "minItems": 1, "uniqueItems": True,
}

GIT_TOOL_SPECS = [
    _tool(
        "git_status", "Git 상태 조회",
        "Return the current branch and concise working-tree status without changing files.",
        {"project": _PROJECT}, ("project",), read_only=True,
    ),
    _tool(
        "git_diff", "Git 변경 내용 조회",
        "Return a bounded Git diff for explicit repository-relative paths.",
        {
            "project": _PROJECT,
            "paths": {"type": "array", "items": {"type": "string", "minLength": 1},
                      "uniqueItems": True},
            "staged": {"type": "boolean", "default": False},
        }, ("project",), read_only=True,
    ),
    _tool(
        "prepare_git_operation", "Git 작업 준비",
        "Prepare a commit, push, PR, or merge plan. Commit and push still require a separate "
        "interactive host approval; PR and merge return a native-workflow handoff.",
        {
            "project": _PROJECT,
            "action": {"type": "string", "enum": ["commit", "push", "pr", "merge"]},
            "paths": _PATHS,
            "message": {"type": "string"},
            "branch": {"type": "string"},
            "new_branch": {"type": "boolean"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "base": {"type": "string"},
            "pull_request_url": {"type": "string"},
        }, ("project", "action"),
    ),
    _tool(
        "execute_git_operation", "승인된 Git 작업 실행",
        "Execute one prepared commit or push only after a matching single-use host approval. "
        "PR and merge operations return their required native-workflow handoff.",
        {"project": _PROJECT, "operation_id": {"type": "string", "minLength": 1}},
        ("project", "operation_id"),
    ),
    _tool(
        "get_git_operation", "Git 작업 상태 조회",
        "Return the current state and handoff metadata for a prepared Git operation.",
        {"project": _PROJECT, "operation_id": {"type": "string", "minLength": 1}},
        ("project", "operation_id"), read_only=True,
    ),
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_env(*, github=False) -> dict[str, str]:
    source = os.environ
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": source.get("HOME", str(Path.home())),
        "LANG": source.get("LANG", "C.UTF-8"),
        "LC_ALL": source.get("LC_ALL", "C.UTF-8"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": (
            "/usr/bin/ssh -F /dev/null -o PermitLocalCommand=no -o ProxyCommand=none "
            "-o BatchMode=yes"
        ),
    }
    for key in ("USER", "LOGNAME", "SSH_AUTH_SOCK"):
        if source.get(key):
            env[key] = source[key]
    if github:
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"):
            if source.get(key):
                env[key] = source[key]
    return env


def _run(
    command: list[str], *, cwd: Path | None = None, github=False,
    timeout: float = COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run with bounded live drains so a child cannot fill memory or run forever."""
    process = subprocess.Popen(
        command, cwd=cwd, env=_safe_env(github=github),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    streams = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"Command exceeded the {timeout:g} second timeout"
                break
            events = streams.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in list(streams.get_map().values())]
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                output = captured[key.data]
                output.extend(chunk)
                if len(output) > MAX_CAPTURE:
                    failure = f"Command {key.data} exceeded the 512 KiB safety limit"
                    break
            if failure:
                break
        if failure:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        failure = failure or "Command did not terminate after it was killed"
        returncode = process.returncode
    finally:
        streams.close()
        for pipe in (process.stdout, process.stderr):
            pipe.close()
    if failure:
        raise ValueError(failure)
    return subprocess.CompletedProcess(
        command, returncode, bytes(captured["stdout"]), bytes(captured["stderr"])
    )


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _terminal_safe(value: object) -> str:
    text = str(value)
    return "".join(
        character if character == "\n" or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F)
        else f"\\x{ord(character):02x}"
        for character in text
    )


def _git(root: Path, *args: str, check=True, literal=True, authenticated=False) -> subprocess.CompletedProcess:
    command = [
        GIT,
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=/dev/null",
        "-c", "commit.gpgSign=false",
        "-c", "diff.external=",
        "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
        "-c", "protocol.ssh.allow=always",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        *(["--literal-pathspecs"] if literal else []),
        "-C", str(root),
        *args,
    ]
    if authenticated:
        # Only this fixed host helper can supply credentials for trusted URLs.
        # Arbitrary repository credential helpers remain unsupported.
        command[1:1] = ["-c", "credential.helper=", "-c",
                        "credential.helper=!/usr/bin/gh auth git-credential"]
    result = _run(command, github=authenticated)
    if check and result.returncode:
        detail = _decode(result.stderr).strip() or _decode(result.stdout).strip()
        raise ValueError(f"Git command failed: {detail[:2000]}")
    return result


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _default_message_validator(message_path: Path) -> None:
    if not CHANGE_EVIDENCE_VALIDATOR.is_file() or CHANGE_EVIDENCE_VALIDATOR.is_symlink():
        raise ValueError("Pinned Change Evidence validator is unavailable or unsafe")
    data = CHANGE_EVIDENCE_VALIDATOR.read_bytes()
    digest = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if digest != CHANGE_EVIDENCE_VALIDATOR_BLOB:
        raise ValueError("Pinned Change Evidence validator changed; review and repin it first")
    # Execute the verified bytes, not a source file another project may edit
    # between checksum verification and interpreter startup.
    with tempfile.NamedTemporaryFile(dir=message_path.parent, suffix=".py") as snapshot:
        os.fchmod(snapshot.fileno(), 0o600)
        snapshot.write(data)
        snapshot.flush()
        result = _run([
            sys.executable, snapshot.name, "--kind", "commit",
            "--expected-version", "v1", "--path", str(message_path), "--format", "json",
        ])
    if result.returncode:
        detail = _decode(result.stdout).strip() or _decode(result.stderr).strip()
        raise ValueError(f"Commit message does not satisfy Change Evidence Contract v1: {detail[:4000]}")


def _default_reference_checker(url: str) -> None:
    match = re.fullmatch(
        r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
        r"(?P<kind>issues|pull)/(?P<number>[1-9][0-9]*)", url,
    )
    if not match:
        raise ValueError(f"Reference must be an exact GitHub Issue or PR URL: {url}")
    command = [GH, "issue" if match.group("kind") == "issues" else "pr",
               "view", url, "--json", "url"]
    result = _run(command, github=True)
    if result.returncode:
        raise ValueError(f"Could not verify reference {url}: {_decode(result.stderr).strip()[:1000]}")
    try:
        observed = json.loads(_decode(result.stdout))["url"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"GitHub returned an invalid reference response for {url}") from exc
    if observed.rstrip("/") != url.rstrip("/"):
        raise ValueError(f"GitHub reference identity mismatch for {url}")


def _default_remote_adapter(root: Path, remote_url: str, branch: str) -> tuple[str, bool, str | None]:
    https = re.fullmatch(r"https://github\.com/jhw7500/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?", remote_url)
    ssh = re.fullmatch(r"git@github\.com:jhw7500/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?", remote_url)
    if not (https or ssh):
        raise ValueError("origin must be an HTTPS or git@github.com SSH URL owned by jhw7500")
    _reject_dangerous_git_config(root)
    result = _git(
        root, "ls-remote", "--exit-code", "--heads", remote_url,
        f"refs/heads/{branch}", check=False, authenticated=True,
    )
    if result.returncode not in (0, 2):
        detail = _decode(result.stderr).strip()
        raise ValueError(f"Could not inspect the origin branch: {detail[:1000]}")
    exists = result.returncode == 0
    remote_head = _decode(result.stdout).split(maxsplit=1)[0] if exists else None
    return remote_url, exists, remote_head


def _reject_dangerous_git_config(root: Path) -> None:
    result = _git(root, "config", "--local", "--includes", "--list", check=False)
    if result.returncode:
        raise ValueError("Could not inspect repository Git configuration")
    forbidden = re.compile(
        r"^(?:filter\..*\.(?:clean|process|required)|url\..*\.(?:insteadof|pushinsteadof)|"
        r"diff(?:\..*\.textconv|\.external)|credential(?:\..*)?\.helper|"
        r"core\.(?:sshcommand|gitproxy|askpass)|"
        r"remote\..*\.(?:vcs|uploadpack|receivepack))=",
        re.IGNORECASE,
    )
    matches = [line.split("=", 1)[0] for line in _decode(result.stdout).splitlines()
               if forbidden.match(line)]
    if matches:
        raise ValueError("Repository Git configuration contains executable settings: "
                         + ", ".join(sorted(set(matches))))


class GitWorkflows:
    def __init__(
        self,
        projects: dict[str, Path],
        state_dir: Path,
        *,
        message_validator: Callable[[Path], None] | None = None,
        reference_checker: Callable[[str], None] | None = None,
        remote_adapter: Callable[[Path, str, str], tuple[str, bool, str | None]] | None = None,
        workspace=None,
    ):
        if not projects or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in projects):
            raise ValueError("Git workflow projects require simple allowlisted names")
        self.projects = {name: Path(path).resolve(strict=True) for name, path in projects.items()}
        self.state_dir = Path(state_dir).expanduser()
        self.message_validator = message_validator or _default_message_validator
        self.reference_checker = reference_checker or _default_reference_checker
        self.remote_adapter = remote_adapter or _default_remote_adapter
        self.workspace = workspace
        self._ensure_state_dir()

    def _ensure_state_dir(self) -> None:
        candidate = self.state_dir.absolute()
        for root in self.projects.values():
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            raise ValueError("Git operation state must be outside every allowlisted project")
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            if current.exists() and current.is_symlink():
                raise ValueError(f"State path component is a symlink: {current}")
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(candidate, 0o700)
        info = candidate.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Git operation state directory must be a current-user-owned directory")
        self.state_dir = candidate.resolve(strict=True)

    def _project(self, name: object) -> Path:
        if not isinstance(name, str) or name not in self.projects:
            raise ValueError("Choose an allowlisted project")
        root = self.projects[name]
        if _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() != b"true":
            raise ValueError("Selected project is not a Git worktree")
        _reject_dangerous_git_config(root)
        top_level = Path(_decode(_git(root, "rev-parse", "--show-toplevel").stdout).strip()).resolve()
        if top_level != root:
            raise ValueError("Git project must be the exact repository root, not a subdirectory")
        return root

    @contextmanager
    def _workspace_access(self, project: str, *, write=False):
        """Bind active-policy work; project writers lock before admission."""
        if self.workspace is None or not self.workspace.enabled:
            yield None
            return
        snapshot, _ = self.workspace._context()
        lock = self.workspace.authority.project_lock(project) if write else nullcontext()
        with lock:
            with self.workspace.authority.admission(snapshot.digest):
                yield snapshot
                self.workspace._current(snapshot)

    def _workspace_path(self, snapshot, project: str, relative: str, operation: str) -> Path:
        if snapshot is None:
            return self.projects[project] / relative
        target = self.workspace.policy.check(project, relative, operation, snapshot=snapshot)
        return self.workspace.ordinary_path(snapshot, project, target)

    def _workspace_paths(self, snapshot, project: str, paths: Iterable[str], operation: str) -> None:
        for relative in paths:
            self._workspace_path(snapshot, project, relative, operation)

    def _safe_changed_paths(
        self, snapshot, project: str, root: Path, *, staged=False,
        comparison: tuple[str, str] | None = None,
        requested: Iterable[str] = (),
    ) -> tuple[list[str], int, bool]:
        command = ["diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames"]
        if staged:
            command.append("--cached")
        if comparison:
            command.extend(comparison)
        requested = set(requested)
        groups = self._changed_path_groups(_git(root, *command).stdout)

        selected = []
        omitted = 0
        truncated = False
        for group in groups:
            if requested and not requested.intersection(group):
                continue
            try:
                for relative in group:
                    self._paths(root, [relative], required=True)
                    self._workspace_path(snapshot, project, relative, "read")
            except (WorkspaceError, ValueError):
                omitted += 1
                continue
            additions = [relative for relative in group if relative not in selected]
            if len(selected) + len(additions) > MAX_PATHS:
                omitted += 1
                truncated = True
                continue
            selected.extend(additions)
        return selected, omitted, truncated

    @staticmethod
    def _changed_path_groups(output: bytes) -> list[list[str]]:
        fields = output.split(b"\0")
        groups = []
        index = 0
        while index < len(fields) and fields[index]:
            status_value = fields[index].decode("ascii")
            index += 1
            count = 2 if status_value.startswith(("R", "C")) else 1
            if index + count > len(fields):
                raise ValueError("Git returned malformed changed-path metadata")
            group = [item.decode("utf-8", errors="surrogateescape")
                     for item in fields[index:index + count]]
            if any(not item for item in group):
                raise ValueError("Git returned malformed changed-path metadata")
            groups.append(group)
            index += count
        if any(fields[index:]):
            raise ValueError("Git returned malformed changed-path metadata")
        return groups

    def _validate_outgoing_history(
        self, snapshot, project: str, root: Path, comparison: str, head: str,
    ) -> None:
        revision_args = [head]
        if comparison != EMPTY_TREE:
            revision_args.append(f"^{comparison}")
        lines = _decode(_git(root, "rev-list", "--parents", *revision_args).stdout).splitlines()
        if len(lines) > MAX_PUSH_COMMITS:
            raise ValueError("Outgoing Git history exceeds the commit limit")
        commits = []
        for line in lines:
            fields = line.split()
            if (not fields or any(not re.fullmatch(r"[0-9a-f]{40,64}", item)
                                  for item in fields)):
                raise ValueError("Git returned malformed outgoing history metadata")
            commits.append((fields[0], fields[1:] or [EMPTY_TREE]))

        observed = set()
        for commit, parents in commits:
            for parent in parents:
                output = _git(
                    root, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                    "--find-renames", parent, commit,
                ).stdout
                for group in self._changed_path_groups(output):
                    for relative in group:
                        observed.add(relative)
                        if len(observed) > MAX_PUSH_PATHS:
                            raise ValueError("Outgoing Git history exceeds the path limit")
                        try:
                            self._paths(root, [relative], required=True)
                            self._workspace_path(snapshot, project, relative, "read")
                        except (WorkspaceError, ValueError):
                            raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED") from None

    def _safe_diff(
        self, snapshot, project: str, root: Path, *, staged=False,
        comparison: tuple[str, str] | None = None,
        requested: Iterable[str] = (),
    ) -> tuple[str, int, bool]:
        paths, omitted, truncated = self._safe_changed_paths(
            snapshot, project, root, staged=staged, comparison=comparison, requested=requested,
        )
        if not paths:
            return "", omitted, truncated
        command = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
        if staged:
            command.append("--cached")
        if comparison:
            command.extend(comparison)
        command.extend(["--", *paths])
        return _decode(_git(root, *command).stdout), omitted, truncated

    @staticmethod
    def _status_path(relative: str) -> str:
        if (relative and relative == relative.strip()
                and all(character.isprintable() for character in relative)):
            return relative
        return json.dumps(relative, ensure_ascii=False)

    def _safe_status(self, snapshot, project: str, root: Path) -> tuple[str, int, bool]:
        raw = _git(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        ).stdout
        rows = []
        omitted = 0
        truncated = False
        used = 0
        for record in raw.split(b"\0"):
            if not record:
                continue
            if len(record) < 4 or record[2:3] != b" ":
                raise ValueError("Git returned malformed status metadata")
            code = record[:2].decode("ascii")
            relative = record[3:].decode("utf-8", errors="surrogateescape")
            try:
                self._paths(root, [relative], required=True)
                self._workspace_path(snapshot, project, relative, "read")
            except (WorkspaceError, ValueError):
                omitted += 1
                continue
            row = f"{code} {self._status_path(relative)}\n"
            size = len(row.encode("utf-8", errors="surrogateescape"))
            if len(rows) >= MAX_PATHS or used + size > MAX_TEXT_BYTES:
                omitted += 1
                truncated = True
                continue
            rows.append(row)
            used += size
        branch = self._current_branch(root)
        return f"## {branch}\n{''.join(rows)}", omitted, truncated

    def _paths(self, root: Path, values: object, *, required=False) -> list[str]:
        if values is None and not required:
            return []
        if not isinstance(values, list) or (required and not values):
            raise ValueError("paths must be a nonempty list")
        if len(values) > MAX_PATHS:
            raise ValueError("At most 128 explicit paths are allowed")
        result = []
        for value in values:
            if not isinstance(value, str) or not value or "\0" in value or len(value) > 4096:
                raise ValueError("Every path must be a nonempty repository-relative string")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise ValueError("Git paths must stay inside the selected project and exclude .git")
            if any(part == ".env" or part.startswith(".env.") for part in path.parts):
                raise ValueError("Environment secret files are excluded from Git workflow tools")
            target = (root / path).resolve(strict=False)
            if not target.is_relative_to(root):
                raise ValueError("Git paths may not escape through symbolic links")
            normalized = path.as_posix()
            if not normalized or normalized in result:
                raise ValueError("Git paths must be distinct repository files")
            if target.exists() and target.is_dir():
                raise ValueError("Select files explicitly; directory pathspecs are not accepted")
            result.append(normalized)
        return result

    def _branch(self, value: object) -> str:
        if not isinstance(value, str) or not BRANCH_RE.fullmatch(value):
            raise ValueError("Provide an explicit valid branch name")
        return value

    def _head(self, root: Path) -> str:
        return _decode(_git(root, "rev-parse", "HEAD").stdout).strip()

    def _current_branch(self, root: Path) -> str:
        result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if result.returncode:
            raise ValueError("Detached HEAD is not supported for Git workflow operations")
        return _decode(result.stdout).strip()

    def _snapshot(self, root: Path, paths: Iterable[str] = ()) -> dict:
        selected = list(paths)
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"]
        if selected:
            diff_args.extend(["--", *selected])
            diff = _git(root, *diff_args).stdout
        else:
            diff_args.extend(["--", ":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*"])
            diff = _git(root, *diff_args, literal=False).stdout
        digest = hashlib.sha256()
        for relative in selected:
            target = root / relative
            digest.update(relative.encode("utf-8") + b"\0")
            try:
                info = target.lstat()
            except FileNotFoundError:
                digest.update(b"deleted\0")
                continue
            digest.update(str(info.st_mode & 0o170777).encode("ascii") + b"\0")
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.readlink(target).encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_CAPTURE:
                    raise ValueError(f"Selected file is too large for an approved commit: {relative}")
                with target.open("rb") as handle:
                    content = handle.read(MAX_CAPTURE + 1)
                if len(content) > MAX_CAPTURE:
                    raise ValueError(f"Selected file is too large for an approved commit: {relative}")
                digest.update(content)
            else:
                raise ValueError(f"Unsupported selected path type: {relative}")
        return {
            "head": self._head(root),
            "branch": self._current_branch(root),
            "status_sha256": _sha256(status),
            "diff_sha256": _sha256(diff),
            "selected_content_sha256": digest.hexdigest(),
            "selected_index_state": self._worktree_index_state(root, selected),
        }

    def _worktree_index_state(self, root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
        entries = {}
        for relative in paths:
            target = root / relative
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                data = os.readlink(target).encode("utf-8", errors="surrogateescape")
                mode = "120000"
            elif stat.S_ISREG(info.st_mode):
                with target.open("rb") as handle:
                    data = handle.read(MAX_CAPTURE + 1)
                if len(data) > MAX_CAPTURE:
                    raise ValueError(f"Selected file is too large for an approved commit: {relative}")
                mode = "100755" if info.st_mode & 0o111 else "100644"
            else:
                raise ValueError(f"Unsupported selected path type: {relative}")
            header = b"blob " + str(len(data)).encode("ascii") + b"\0"
            entries[relative] = {"mode": mode, "oid": hashlib.sha1(header + data).hexdigest()}
        return entries

    def _staged_index_state(self, root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
        output = _git(root, "ls-files", "--stage", "-z", "--", *paths).stdout
        entries = {}
        for raw in output.split(b"\0"):
            if not raw:
                continue
            metadata, separator, encoded_path = raw.partition(b"\t")
            if not separator:
                raise ValueError("Git returned malformed staged-file metadata")
            fields = metadata.decode("ascii").split()
            if len(fields) != 3 or fields[2] != "0":
                raise ValueError("Git index contains an unsupported conflict stage")
            relative = encoded_path.decode("utf-8", errors="surrogateescape")
            entries[relative] = {"mode": fields[0], "oid": fields[1]}
        return entries

    def _project_dir(self, project: str) -> Path:
        path = self.state_dir / project
        if path.exists() and path.is_symlink():
            raise ValueError("Git operation project state path may not be a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("Git operation project state must be an owner-only directory")
        return path

    @contextmanager
    def _lock(self, project: str):
        lock_path = self._project_dir(project) / ".lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield
        finally:
            pass

    def _write_private(self, path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            self._verify_private(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _verify_private(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"Required private state artifact is missing: {path.name}") from exc
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid():
            raise ValueError(f"State artifact is not a safe current-user-owned regular file: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError(f"State artifact mode must be 0600: {path}")

    def _plan_path(self, project: str, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid Git operation id")
        return self._project_dir(project) / f"{operation_id}.plan.json"

    def _receipt_path(self, project: str, operation_id: str) -> Path:
        return self._project_dir(project) / f"{operation_id}.approval.json"

    def _load_plan(self, project: str, operation_id: str) -> dict:
        path = self._plan_path(project, operation_id)
        self._verify_private(path)
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Git operation plan is unreadable") from exc
        if plan.get("operation_id") != operation_id or plan.get("project") != project:
            raise ValueError("Git operation plan identity mismatch")
        immutable = plan.get("immutable")
        if not isinstance(immutable, dict) or plan.get("plan_digest") != _sha256(_json_bytes(immutable)):
            raise ValueError("Git operation plan integrity check failed")
        return plan

    def _save_plan(self, plan: dict) -> None:
        self._write_private(self._plan_path(plan["project"], plan["operation_id"]), _json_bytes(plan))

    def _new_plan(self, project: str, immutable: dict, *, status: str) -> dict:
        operation_id = secrets.token_hex(16)
        immutable = {**immutable, "operation_id": operation_id, "project": project}
        return {
            "version": 1,
            "operation_id": operation_id,
            "project": project,
            "action": immutable["action"],
            "status": status,
            "created_at": _utcnow(),
            "immutable": immutable,
            "plan_digest": _sha256(_json_bytes(immutable)),
        }

    def call(self, name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "git_status":
            return self.git_status(args)
        if name == "git_diff":
            return self.git_diff(args)
        if name == "prepare_git_operation":
            return self.prepare(args)
        if name == "execute_git_operation":
            return self.execute(args)
        if name == "get_git_operation":
            return self.get(args)
        raise ValueError(f"Unknown Git workflow tool: {name}")

    def git_status(self, args: dict) -> dict:
        project = args.get("project")
        root = self._project(project)
        with self._workspace_access(project) as snapshot:
            if snapshot is None:
                output = _decode(_git(root, "status", "--short", "--branch").stdout)
                return {"project": project, "branch": self._current_branch(root), "status": output}
            output, omitted, truncated = self._safe_status(snapshot, project, root)
            return {"project": project, "branch": self._current_branch(root), "status": output,
                    "omitted_paths": omitted, "truncated": truncated}

    def git_diff(self, args: dict) -> dict:
        project = args.get("project")
        root = self._project(project)
        paths = self._paths(root, args.get("paths"))
        staged = args.get("staged") is True
        if args.get("staged") not in (None, False, True):
            raise ValueError("staged must be a boolean")
        with self._workspace_access(project) as snapshot:
            if snapshot is not None:
                if paths:
                    self._workspace_paths(snapshot, project, paths, "read")
                    output, omitted, truncated = self._safe_diff(
                        snapshot, project, root, staged=staged, requested=paths,
                    )
                    if omitted:
                        raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED")
                else:
                    output, omitted, truncated = self._safe_diff(
                        snapshot, project, root, staged=staged,
                    )
                return {"project": project, "staged": staged, "paths": paths, "diff": output,
                        "omitted_paths": omitted, "truncated": truncated}
            command = ["diff", "--no-ext-diff", "--no-textconv"]
            if staged:
                command.append("--cached")
            if paths:
                command.extend(["--", *paths])
                output = _decode(_git(root, *command).stdout)
            else:
                command.extend(["--", ":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*"])
                output = _decode(_git(root, *command, literal=False).stdout)
            return {"project": project, "staged": staged, "paths": paths, "diff": output}

    def prepare(self, args: dict) -> dict:
        project = args.get("project")
        root = self._project(project)
        with self._workspace_access(project) as snapshot:
            if sum(1 for _ in self._project_dir(project).glob("*.plan.json")) >= MAX_OPERATIONS:
                raise ValueError("Git operation history capacity reached; host maintenance is required")
            for field in ("message", "title", "body"):
                value = args.get(field)
                if value is not None and (not isinstance(value, str)
                                          or len(value.encode("utf-8")) > MAX_TEXT_BYTES):
                    raise ValueError(f"{field} must be text of at most 64 KiB")
            action = args.get("action")
            if action == "commit":
                self._reject_unknown_args(args, {"project", "action", "paths", "message"})
                return self._prepare_commit(project, root, args, snapshot)
            if action == "push":
                self._reject_unknown_args(args, {"project", "action", "branch", "new_branch"})
                return self._prepare_push(project, root, args, snapshot)
            if action in {"pr", "merge"}:
                allowed = ({"project", "action", "title", "body", "base"} if action == "pr"
                           else {"project", "action", "pull_request_url"})
                self._reject_unknown_args(args, allowed)
                return self._prepare_native_handoff(project, root, action, args)
            raise ValueError("action must be commit, push, pr, or merge")

    def _reject_unknown_args(self, args: dict, allowed: set[str]) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unsupported or unsafe arguments: {', '.join(unknown)}")

    def _prepare_commit(self, project: str, root: Path, args: dict, workspace_snapshot=None) -> dict:
        paths = self._paths(root, args.get("paths"), required=True)
        self._workspace_paths(workspace_snapshot, project, paths, "write")
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Commit preparation requires the complete commit message")
        if len(message.encode("utf-8")) > 64 * 1024:
            raise ValueError("Commit message exceeds the 64 KiB Change Evidence limit")
        if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
            raise ValueError("Commit preparation requires an empty pre-existing Git index")
        _reject_dangerous_git_config(root)
        changed = _decode(_git(root, "status", "--porcelain=v1", "--", *paths).stdout)
        if not changed.strip():
            raise ValueError("None of the explicitly selected files has a change to commit")
        operation_id = secrets.token_hex(16)
        message_path = self._project_dir(project) / f"{operation_id}.commit-message.md"
        self._write_private(message_path, message.encode("utf-8"))
        try:
            self.message_validator(message_path)
            references = sorted(set(CHANGE_URL_RE.findall(message)))
            if not references:
                raise ValueError("Commit References must include an exact GitHub Issue or PR URL")
            for url in references:
                self.reference_checker(url)
            snapshot = self._snapshot(root, paths)
            preview = self._commit_preview(root, paths)
            immutable = {
                "operation_id": operation_id, "project": project, "action": "commit",
                "paths": paths, "message_path": str(message_path), "message_sha256": _sha256(message.encode()),
                "references": references, "snapshot": snapshot,
                "display_command": f"git commit --file <approved-message> -- {' '.join(paths)}",
                "head_target": snapshot["head"], "preview": preview,
                "approval_nonce": secrets.token_hex(32),
            }
            plan = {
                "version": 1, "operation_id": operation_id, "project": project,
                "action": "commit", "status": "pending_host_approval", "created_at": _utcnow(),
                "immutable": immutable, "plan_digest": _sha256(_json_bytes(immutable)),
            }
            with self._lock(project):
                self._save_plan(plan)
            return self._public(plan)
        except BaseException:
            if message_path.exists():
                message_path.unlink()
            raise

    def _prepare_push(self, project: str, root: Path, args: dict, workspace_snapshot=None) -> dict:
        branch = self._branch(args.get("branch"))
        if branch in PROTECTED_BRANCHES:
            raise ValueError("Direct pushes to main or master are disabled")
        current = self._current_branch(root)
        if current != branch:
            raise ValueError(f"The requested branch {branch!r} is not the checked-out branch {current!r}")
        new_branch = args.get("new_branch")
        if not isinstance(new_branch, bool):
            raise ValueError("Push preparation requires explicit new_branch=true or false")
        _reject_dangerous_git_config(root)
        remote_url = _decode(_git(root, "remote", "get-url", "--push", "origin").stdout).strip()
        trusted_target, exists, remote_head = self.remote_adapter(root, remote_url, branch)
        if new_branch == exists:
            actual = "already exists" if exists else "does not exist"
            raise ValueError(f"new_branch does not match origin: branch {actual}")
        snapshot = self._snapshot(root)
        comparison = remote_head or EMPTY_TREE
        if workspace_snapshot is None:
            preview = _decode(_git(
                root, "diff", "--no-ext-diff", "--no-textconv", "--binary",
                comparison, snapshot["head"], "--",
                ":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*", literal=False,
            ).stdout)
        else:
            self._validate_outgoing_history(
                workspace_snapshot, project, root, comparison, snapshot["head"],
            )
            preview, omitted, truncated = self._safe_diff(
                workspace_snapshot, project, root,
                comparison=(comparison, snapshot["head"]),
            )
            if omitted or truncated:
                raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED")
        immutable = {
            "action": "push", "branch": branch, "new_branch": new_branch,
            "remote": "origin", "trusted_target": trusted_target,
            "remote_head": remote_head, "snapshot": snapshot,
            "display_command": f"git push origin HEAD:refs/heads/{branch}",
            "head_target": f"refs/heads/{branch} -> {snapshot['head']}",
            "preview": preview,
            "approval_nonce": secrets.token_hex(32),
        }
        plan = self._new_plan(project, immutable, status="pending_host_approval")
        with self._lock(project):
            self._save_plan(plan)
        return self._public(plan)

    def _prepare_native_handoff(self, project: str, root: Path, action: str, args: dict) -> dict:
        snapshot = self._snapshot(root)
        skills, missing_prerequisites = _native_workflow_skills()
        artifact = None
        details: dict[str, object] = {}
        operation_id = secrets.token_hex(16)
        if action == "pr":
            title, body = args.get("title"), args.get("body")
            if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
                raise ValueError("PR handoff requires a title and complete body")
            if len(title.encode("utf-8")) > 1024 or len(body.encode("utf-8")) > MAX_CAPTURE:
                raise ValueError("PR title or body exceeds the safe handoff size limit")
            base = self._branch(args.get("base"))
            artifact = self._project_dir(project) / f"{operation_id}.pr-body.md"
            self._write_private(artifact, body.encode("utf-8"))
            details = {"title": title, "base": base, "body_path": str(artifact),
                       "body_sha256": _sha256(body.encode("utf-8"))}
            display = "Use jhw-pr body validation, then pre-pr-tribunal canonical PR creation"
        else:
            url = args.get("pull_request_url")
            if not isinstance(url, str) or not PR_URL_RE.fullmatch(url):
                raise ValueError("Merge handoff requires an exact GitHub pull request URL")
            self.reference_checker(url)
            details = {"pull_request_url": url}
            display = "Use the native jhw-pr merge workflow after exact CI and review verification"
        immutable = {
            "operation_id": operation_id, "project": project, "action": action,
            "snapshot": snapshot, "display_command": display, "head_target": snapshot["head"],
            "preview": display, **details,
            "handoff": {
                "code": "NATIVE_WORKFLOW_REQUIRED",
                "jhw_pr_skill": str(skills["jhw-pr"]),
                "pre_pr_tribunal_skill": str(skills["pre-pr-tribunal"]),
                "missing_prerequisites": missing_prerequisites,
                "reason": (
                    "Install the listed current-user workflow prerequisites explicitly "
                    "before continuing; this MCP does not download skills or create PRs."
                    if missing_prerequisites else
                    "This MCP cannot prove or execute the required native review workflow."
                ),
            },
        }
        plan = {
            "version": 1, "operation_id": operation_id, "project": project,
            "action": action, "status": "waiting_native_workflow", "created_at": _utcnow(),
            "immutable": immutable, "plan_digest": _sha256(_json_bytes(immutable)),
        }
        with self._lock(project):
            self._save_plan(plan)
        return self._public(plan)

    def _public(self, plan: dict) -> dict:
        immutable = plan["immutable"]
        result = {
            "operation_id": plan["operation_id"], "project": plan["project"],
            "action": plan["action"], "status": plan["status"],
            "created_at": plan["created_at"], "head_target": immutable.get("head_target"),
            "display_command": immutable.get("display_command"),
        }
        if plan["status"] == "pending_host_approval":
            result["approval_command"] = shlex.join([
                "/usr/bin/python3", str(Path(__file__).with_name("approve-git-operation.py")),
                "--state-dir", str(self.state_dir), "--project", plan["project"],
                "--operation-id", plan["operation_id"],
            ])
        if "handoff" in immutable:
            result["code"] = "NATIVE_WORKFLOW_REQUIRED"
            result["handoff"] = immutable["handoff"]
            if immutable.get("body_path"):
                result["handoff"]["body_path"] = immutable["body_path"]
        if plan.get("result"):
            result["result"] = plan["result"]
        return result

    def get(self, args: dict) -> dict:
        project = args.get("project")
        self._project(project)
        with self._lock(project):
            return self._public(self._load_plan(project, args.get("operation_id")))

    def execute(self, args: dict) -> dict:
        project = args.get("project")
        root = self._project(project)
        operation_id = args.get("operation_id")
        with self._lock(project):
            with self._workspace_access(project, write=True) as workspace_snapshot:
                plan = self._load_plan(project, operation_id)
                if plan["status"] == "waiting_native_workflow":
                    return self._public(plan)
                if plan["status"] != "pending_host_approval":
                    raise ValueError(f"Git operation is not executable in state {plan['status']!r}")
                immutable = plan["immutable"]
                if plan["action"] == "commit":
                    self._workspace_paths(
                        workspace_snapshot, project, immutable.get("paths", ()), "write",
                    )
                elif plan["action"] == "push" and workspace_snapshot is not None:
                    self._validate_outgoing_history(
                        workspace_snapshot, project, root,
                        immutable["remote_head"] or EMPTY_TREE, immutable["snapshot"]["head"],
                    )
                current = self._snapshot(root, immutable.get("paths", ()))
                if current != immutable["snapshot"]:
                    raise ValueError("Prepared Git operation is stale because HEAD or working state changed")
                receipt_path = self._receipt_path(project, operation_id)
                self._verify_private(receipt_path)
                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError("Host approval receipt is unreadable") from exc
                expected = {
                    "operation_id": operation_id,
                    "plan_digest": plan["plan_digest"],
                    "approval_nonce": immutable["approval_nonce"],
                }
                if any(receipt.get(key) != value for key, value in expected.items()):
                    raise ValueError("Host approval receipt does not match the full prepared plan")
                receipt_path.unlink()
                plan["status"] = "executing"
                self._save_plan(plan)
                try:
                    if plan["action"] == "commit":
                        result = self._execute_commit(root, immutable)
                    elif plan["action"] == "push":
                        result = self._execute_push(root, immutable)
                    else:
                        raise ValueError("Only commit and push may execute through this MCP")
                except BaseException as exc:
                    plan["status"] = "failed"
                    plan["result"] = {"error": str(exc)[:4000]}
                    self._save_plan(plan)
                    raise
                plan["status"] = "completed"
                plan["result"] = result
                self._save_plan(plan)
                return self._public(plan)

    def _execute_commit(self, root: Path, immutable: dict) -> dict:
        message_path = Path(immutable["message_path"])
        self._verify_private(message_path)
        if _sha256(message_path.read_bytes()) != immutable["message_sha256"]:
            raise ValueError("Approved commit message changed after preparation")
        if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
            raise ValueError("Git index changed after approval")
        _reject_dangerous_git_config(root)
        _git(root, "add", "--", *immutable["paths"])
        staged_paths = set(_decode(_git(
            root, "diff", "--cached", "--name-only", "-z"
        ).stdout).rstrip("\0").split("\0"))
        staged_paths.discard("")
        if (not staged_paths or not staged_paths.issubset(set(immutable["paths"]))
                or self._staged_index_state(root, immutable["paths"])
                != immutable["snapshot"]["selected_index_state"]):
            self._restore_selected_index(root, immutable["paths"])
            raise ValueError(
                "The staged paths, selected file bytes, or executable modes changed during commit staging"
            )
        try:
            result = _git(
                root, "commit", "--no-verify", "--file", str(message_path),
            )
        except BaseException as commit_error:
            if not self._restore_selected_index(root, immutable["paths"]):
                raise ValueError(
                    "Commit failed and the selected-path index cleanup also failed; "
                    "inspect the index before retrying"
                ) from commit_error
            raise
        return {"commit": self._head(root), "output": _decode(result.stdout).strip()}

    def _restore_selected_index(self, root: Path, paths: list[str]) -> bool:
        restore = _git(root, "restore", "--staged", "--", *paths, check=False)
        return (restore.returncode == 0
                and _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0)

    def _commit_preview(self, root: Path, paths: list[str]) -> str:
        preview = bytearray(_git(
            root, "diff", "--no-ext-diff", "--no-textconv", "--binary",
            "HEAD", "--", *paths,
        ).stdout)
        for relative in paths:
            tracked = _git(root, "ls-files", "--error-unmatch", "--", relative, check=False)
            if tracked.returncode == 0:
                continue
            target = root / relative
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink():
                content = os.readlink(target).encode("utf-8", errors="surrogateescape")
            else:
                with target.open("rb") as handle:
                    content = handle.read(MAX_CAPTURE + 1)
                if len(content) > MAX_CAPTURE:
                    raise ValueError(f"Untracked file is too large to preview safely: {relative}")
            heading = f"\n--- untracked file: {relative} ({len(content)} bytes) ---\n".encode()
            preview.extend(heading)
            try:
                preview.extend(content.decode("utf-8").encode("utf-8"))
            except UnicodeDecodeError:
                import base64
                preview.extend(b"base64:\n" + base64.b64encode(content))
            preview.extend(b"\n--- end untracked file ---\n")
            if len(preview) > MAX_CAPTURE:
                raise ValueError("Commit preview exceeded the 512 KiB safety limit")
        return _decode(bytes(preview))

    def _execute_push(self, root: Path, immutable: dict) -> dict:
        branch = immutable["branch"]
        if branch in PROTECTED_BRANCHES or self._current_branch(root) != branch:
            raise ValueError("Protected or changed branch cannot be pushed")
        _reject_dangerous_git_config(root)
        remote_url = _decode(_git(root, "remote", "get-url", "--push", "origin").stdout).strip()
        trusted_target, exists, remote_head = self.remote_adapter(root, remote_url, branch)
        if (trusted_target != immutable["trusted_target"]
                or exists == immutable["new_branch"]
                or remote_head != immutable["remote_head"]):
            raise ValueError("Origin identity or branch existence changed after approval")
        fixture_push = getattr(self.remote_adapter, "push", None)
        if fixture_push is None:
            result = _git(root, "push", trusted_target, f"HEAD:refs/heads/{branch}", authenticated=True)
        else:
            # Only an explicitly injected test fixture adapter reaches this path.
            result = fixture_push(root, branch)
        return {"pushed": self._head(root), "branch": branch, "output": _decode(result.stderr).strip()}


def approve_operation(state_dir: Path, project: str, operation_id: str) -> dict:
    """Create a single-use receipt after an exact interactive terminal confirmation."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("Git approval requires a genuine interactive terminal")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
        raise ValueError("Invalid project name")
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("Invalid Git operation id")
    state_dir = Path(state_dir).expanduser().absolute()
    current = Path(state_dir.anchor)
    for part in state_dir.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"State path component is a symlink: {current}")
    state_info = state_dir.lstat()
    if (not stat.S_ISDIR(state_info.st_mode) or state_info.st_uid != os.getuid()
            or stat.S_IMODE(state_info.st_mode) != 0o700):
        raise ValueError("State directory must be a current-user-owned mode-0700 directory")
    state_dir = state_dir.resolve(strict=True)
    project_dir = state_dir / project
    project_info = project_dir.lstat()
    if (not stat.S_ISDIR(project_info.st_mode) or project_dir.is_symlink()
            or project_info.st_uid != os.getuid() or stat.S_IMODE(project_info.st_mode) != 0o700):
        raise ValueError("Project state must be a current-user-owned mode-0700 directory")
    plan_path = project_dir / f"{operation_id}.plan.json"
    lock_path = project_dir / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = plan_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or plan_path.is_symlink()
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Operation plan is not a safe owner-only regular file")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        immutable = plan.get("immutable", {})
        if (plan.get("status") != "pending_host_approval"
                or plan.get("plan_digest") != _sha256(_json_bytes(immutable))):
            raise ValueError("Operation is not pending approval or failed integrity validation")
        print(f"Project: {_terminal_safe(project)}")
        print(f"Action: {_terminal_safe(plan['action'])}")
        print(f"HEAD/target: {_terminal_safe(immutable.get('head_target'))}")
        print(f"Exact command: {_terminal_safe(immutable.get('display_command'))}")
        if plan["action"] == "commit":
            message_path = Path(immutable["message_path"])
            message_info = message_path.lstat()
            if (not stat.S_ISREG(message_info.st_mode) or message_path.is_symlink()
                    or message_info.st_uid != os.getuid()
                    or stat.S_IMODE(message_info.st_mode) != 0o600):
                raise ValueError("Commit message artifact is not a safe owner-only regular file")
            message_bytes = message_path.read_bytes()
            if _sha256(message_bytes) != immutable["message_sha256"]:
                raise ValueError("Commit message changed after preparation")
            print("Commit message:")
            print(_terminal_safe(message_bytes.decode("utf-8", errors="replace")))
        print("Diff/plan:")
        print(_terminal_safe(immutable.get("preview", "")))
        expected = f"APPROVE {operation_id}"
        typed = input(f"Type exactly '{expected}' to grant one execution: ")
        if typed != expected:
            raise ValueError("Approval text did not match; no receipt was created")
        receipt = {
            "operation_id": operation_id,
            "plan_digest": plan["plan_digest"],
            "approval_nonce": immutable["approval_nonce"],
            "approved_at": _utcnow(),
        }
        receipt_path = project_dir / f"{operation_id}.approval.json"
        temporary = project_dir / f".{operation_id}.{secrets.token_hex(8)}.approval.tmp"
        receipt_bytes = _json_bytes(receipt)
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(output, "wb") as handle:
                handle.write(receipt_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, receipt_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"operation_id": operation_id, "status": "approved_once"}
