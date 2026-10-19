"""Byte-and-mode compare-and-swap for approved delegated coding changes.

Review-only profiles never write. CAS profiles may apply exclusively ordinary
text/code files where every live preimage still equals the approved snapshot.
Secret, deployment, DB and MCP control inputs are always held for human review.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from host_runtime_config import HostRuntimeError

NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
MAX_FILES = 1000
MAX_FILE_BYTES = 1024 * 1024
PROTECTED_PARTS = frozenset({".git", ".env", ".codex", ".github", ".ssh"})
PROTECTED_PREFIXES = (
    "chatgpt-mcp/", "deploy/", "server/migrations/", "server/config/",
    "scripts/", "contracts/", "infra/", "ops/"
)


def _allowed(name, roots):
    return (isinstance(name, str) and len(name) <= 512
            and NAME.fullmatch(name) is not None
            and all(part not in PROTECTED_PARTS and not part.startswith(".env.")
                    for part in name.split("/"))
            and any(name.startswith(root + "/") for root in roots))


def _high_risk(name):
    return (name.startswith(PROTECTED_PREFIXES) or name.endswith(
        (".sh", ".sql", ".service", ".toml", ".lock", "Dockerfile", ".yml", ".yaml"))
        or name.rsplit("/", 1)[-1] in {"package.json", "Makefile", "AGENTS.md", "CLAUDE.md"})


def _files(base, roots):
    found = {}
    for root in roots:
        folder = base / root
        if folder.is_symlink() or not folder.is_dir():
            raise HostRuntimeError("UNSAFE_AGENT_PATH")
        for current, dirs, files in os.walk(folder, followlinks=False):
            here = Path(current)
            for part in dirs + files:
                p = here / part
                if p.is_symlink():
                    raise HostRuntimeError("UNSAFE_AGENT_PATH")
            dirs[:] = [name for name in dirs
                       if not name.startswith(".") and name not in PROTECTED_PARTS]
            for name in files:
                target = here / name
                relative = target.relative_to(base).as_posix()
                if not _allowed(relative, roots):
                    raise HostRuntimeError("UNSAFE_AGENT_PATH")
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise HostRuntimeError("UNSAFE_AGENT_FILE")
                payload = target.read_bytes()
                found[relative] = (hashlib.sha256(payload).hexdigest(),
                                   stat.S_IMODE(info.st_mode), payload)
                if len(found) > MAX_FILES:
                    raise HostRuntimeError("AGENT_FILE_LIMIT")
    return found


def apply_agent_result(task: dict, workspace: Path, project_root: Path, *, apply=False):
    """No raw bytes in return values. CAS for the original manifest only."""
    if task.get("apply_policy") not in {"review_only", "cas"}:
        raise HostRuntimeError("INVALID_AGENT_TASK")
    roots = task.get("allowed_paths")
    if not isinstance(roots, list) or not roots or any(not isinstance(x, str) for x in roots):
        raise HostRuntimeError("INVALID_AGENT_TASK")
    workspace, project_root = Path(workspace).resolve(), Path(project_root).resolve()
    original = task.get("file_manifest")
    if not isinstance(original, list) or len(original) > MAX_FILES:
        raise HostRuntimeError("PLAN_INVALID")
    prev = {}
    for record in original:
        if (not isinstance(record, dict) or not _allowed(record.get("path"), roots)
                or record["path"] in prev
                or not isinstance(record.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or type(record.get("mode")) is not int):
            raise HostRuntimeError("PLAN_INVALID")
        prev[record["path"]] = (record["sha256"], stat.S_IMODE(record["mode"]))
    staged = _files(workspace, roots)
    # The frozen copy is mode 0600/0700 by design; original group/other
    # permissions are stored separately and must not create fake changes.
    after = {}
    for name, (digest, mode, payload) in staged.items():
        if name in prev and bool(mode & 0o111) == bool(prev[name][1] & 0o111):
            mode = prev[name][1]
        elif name not in prev:
            mode = 0o755 if mode & 0o111 else 0o644
        after[name] = (digest, mode, payload)
    changed = sorted(path for path in set(prev) | set(after)
                     if prev.get(path) != (after[path][:2] if path in after else None))
    protected = [name for name in changed if _high_risk(name) or name not in after]
    conflicts = []
    # Check ALL live preimages before beginning to modify anything.
    for name in changed:
        path = project_root / name
        try:
            # A parent alias can redirect a read outside the approved repo.
            if any(parent.is_symlink() for parent in path.parents
                   if parent.is_relative_to(project_root)):
                conflicts.append(name)
                continue
            if path.is_symlink():
                conflicts.append(name)
                continue
            if name in prev:
                if not path.is_file():
                    conflicts.append(name)
                    continue
                info = path.lstat()
                before = (hashlib.sha256(path.read_bytes()).hexdigest(),
                          stat.S_IMODE(info.st_mode))
                if before != prev[name]:
                    conflicts.append(name)
            elif path.exists() or path.is_symlink():
                conflicts.append(name)
        except OSError:
            conflicts.append(name)
    result = {"changed_files": changed, "protected": protected,
              "conflicts": sorted(set(conflicts)), "applied_paths": [],
              "review_required_paths": sorted(set(protected + conflicts)),
              "validation_evidence": {"source_files_compared": len(prev), "changes": len(changed)}}
    if not apply or task["apply_policy"] != "cas" or protected or conflicts:
        result["status"] = "review_required" if changed else "unchanged"
        return result
    # First release only applies exactly one ordinary file per task to avoid
    # claiming multi-file atomicity (os.replace cannot guarantee that).
    if len(changed) > 1:
        result["review_required_paths"] = changed
        result["status"] = "review_required"
        return result
    if not changed:
        result["status"] = "unchanged"
        return result
    name = changed[0]
    path = project_root / name
    parent = path.parent
    if any(p.is_symlink() for p in (parent, *parent.parents) if p.is_relative_to(project_root)):
        result["review_required_paths"] = [name]
        result["status"] = "review_required"
        return result
    parent.mkdir(parents=True, exist_ok=True)
    value, mode = after[name][2], after[name][1]
    fd, temp = tempfile.mkstemp(prefix=".agent-cas-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode & 0o777)
        # Check the original one final time just before atomic replacement.
        if name in prev:
            if not path.is_file() or path.is_symlink():
                raise HostRuntimeError("AGENT_CAS_CONFLICT")
            info = path.lstat()
            if (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(info.st_mode)) != prev[name]:
                raise HostRuntimeError("AGENT_CAS_CONFLICT")
        elif path.exists() or path.is_symlink():
            raise HostRuntimeError("AGENT_CAS_CONFLICT")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    result.update(status="applied", applied_paths=[name])
    return result
