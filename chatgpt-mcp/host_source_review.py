"""Exact, private source snapshots and high-risk review for registered host operations.

Only files covered by a registered `source_inputs` set (host_registry.source_input_covers)
are frozen. Symlinks, .git/.env* entries, special files, path escapes, oversize or
edited-during-read files inside an included location fail closed. The snapshot never
executes; it is the only source later actions may use.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import host_runtime
from host_registry import source_input_covers
from host_runtime import _git, _safe_file, git_state, verify_reviewed_high_risk_inputs
from host_runtime_config import HostRuntimeError, private_dir
from host_runtime_operations import _save_private_json

REVIEW_POLICIES = ("git_head", "reviewed_snapshot")
MAX_WALK_ENTRIES = 50_000
MAX_DIFF_INPUT = 200 * 1024
MAX_DIFF_BYTES = 64 * 1024
MAX_REVIEW_DIFF_BYTES = 768 * 1024
MAX_REVIEW_BYTES = 1024 * 1024


def _read_input(path: Path):
    """Read one input (legacy _safe_file); an OS-level read failure (permission, a file
    vanishing between lstat and open, ...) never escapes with its path."""
    try:
        return _safe_file(path)
    except OSError:
        raise HostRuntimeError("SOURCE_UNREADABLE") from None


def _unsafe_name(name: str) -> bool:
    return name in (".git", ".env") or name.startswith(".env.")


def _check_parents(root: Path, name: str) -> None:
    """Every directory between root and name must be a real directory, never a link."""
    current = root
    for part in name.split("/")[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
        if stat.S_ISLNK(info.st_mode):
            raise HostRuntimeError("UNSAFE_BUILD_INPUT")
        if not stat.S_ISDIR(info.st_mode):
            raise HostRuntimeError("SOURCE_INCOMPLETE")


def _walk(root: Path, base: str, pattern, inputs, result: set, budget: list) -> None:
    """Scan an included dir: null pattern = whole subtree, glob = direct children only."""
    pending = [base]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(root / folder))
        except OSError as exc:
            raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise HostRuntimeError("BUILD_INPUT_TOO_LARGE")
            relative = folder + "/" + entry.name
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                raise HostRuntimeError("SOURCE_UNREADABLE") from None
            if _unsafe_name(entry.name) or is_link:
                raise HostRuntimeError("UNSAFE_BUILD_INPUT")
            if is_dir:
                if pattern is None:
                    pending.append(relative)
            elif is_file:
                if source_input_covers(inputs, relative):
                    result.add(relative)
            else:
                raise HostRuntimeError("UNSAFE_BUILD_INPUT")


def _enumerate(root: Path, inputs: dict) -> list[str]:
    result = set()
    for name in inputs["files"]:
        if not source_input_covers(inputs, name):
            raise HostRuntimeError("UNSAFE_BUILD_INPUT")
        _check_parents(root, name)
        try:
            info = (root / name).lstat()
        except OSError as exc:
            raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
        if not stat.S_ISREG(info.st_mode):
            raise HostRuntimeError("UNSAFE_BUILD_INPUT")
        result.add(name)
    budget = [MAX_WALK_ENTRIES]
    for base, pattern in inputs["dirs"].items():
        _check_parents(root, base + "/-")
        try:
            info = (root / base).lstat()
        except OSError as exc:
            raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
        if stat.S_ISLNK(info.st_mode):
            raise HostRuntimeError("UNSAFE_BUILD_INPUT")
        if not stat.S_ISDIR(info.st_mode):
            raise HostRuntimeError("SOURCE_INCOMPLETE")
        _walk(root, base, pattern, inputs, result, budget)
        if len(result) > host_runtime.MAX_FILES:
            raise HostRuntimeError("BUILD_INPUT_TOO_LARGE")
    return sorted(result)


def _private_dirs(base: Path, relative: str) -> Path:
    current = base
    for part in relative.split("/"):
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise HostRuntimeError("UNSAFE_STATE_PATH")
        os.chmod(current, 0o700)
    return current


def _write_private(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)


def inventory_source(root: Path, inputs: dict, *, copy_to: Path | None = None) -> dict:
    """Fingerprint exactly the covered files (path/mode/size/sha256); optionally copy them
    owner-only into copy_to. The digest format matches legacy source_inventory."""
    try:
        root = Path(root).resolve(strict=True)
    except OSError as exc:
        raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
    names = _enumerate(root, inputs)
    try:
        if copy_to is not None:
            for base in inputs["dirs"]:
                _private_dirs(copy_to, base)
    except OSError:
        raise HostRuntimeError("STATE_UNAVAILABLE") from None
    files, total = [], 0
    for name in names:
        _check_parents(root, name)
        data, mode = _read_input(root / name)
        total += len(data)
        if total > host_runtime.MAX_CONTEXT_BYTES:
            raise HostRuntimeError("BUILD_INPUT_TOO_LARGE")
        files.append({"path": name, "mode": mode, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
        if copy_to is not None:
            parent = name.rpartition("/")[0]
            try:
                _write_private((_private_dirs(copy_to, parent) if parent else copy_to) /
                               name.rpartition("/")[2], data, mode)
            except OSError:
                raise HostRuntimeError("STATE_UNAVAILABLE") from None
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":"))
                            .encode()).hexdigest()
    return {"digest": digest, "file_count": len(files), "total_bytes": total, "files": files}


def _head_tree(root: Path, paths: list[str]) -> dict:
    """HEAD entries for exactly these paths: {path: (mode, type, object_id, size)}."""
    if not paths:
        return {}
    tree = {}
    for record in _git(root, "ls-tree", "-z", "-l", "--full-tree", "HEAD", "--", *paths).split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id, size = header.decode("ascii").split()
            name = raw_path.decode("utf8", "strict")
        except (ValueError, UnicodeError) as exc:
            raise HostRuntimeError("REVIEW_REQUIRED") from exc
        tree[name] = (mode, kind, object_id, int(size) if size.isdigit() else None)
    return tree


def _blob_id(data: bytes, object_id: str) -> str:
    algorithm = "sha256" if len(object_id) == 64 else "sha1"
    return hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()


def _diff(path: str, head: bytes | None, current: bytes | None, budget: list) -> dict:
    if head is None and current is None:
        return {"diff": "", "diff_truncated": False, "diff_omitted": "unavailable"}
    if b"\0" in (head or b"") or b"\0" in (current or b""):
        return {"diff": "", "diff_truncated": False, "diff_omitted": "binary"}
    lines = difflib.unified_diff(
        (head or b"").decode("utf8", "replace").splitlines(keepends=True),
        (current or b"").decode("utf8", "replace").splitlines(keepends=True),
        fromfile="HEAD/" + path, tofile="snapshot/" + path)
    text = "".join(lines)
    data = text.encode("utf8")
    limit = min(MAX_DIFF_BYTES, budget[0])
    if limit <= 0:
        return {"diff": "", "diff_truncated": False, "diff_omitted": "review_budget"}
    truncated = len(data) > limit
    if truncated:
        text = data[:limit].decode("utf8", "ignore")
    budget[0] -= len(text.encode("utf8"))
    return {"diff": text, "diff_truncated": truncated, "diff_omitted": None}


def _high_risk_review(root: Path, source: Path, manifest: dict, high_risk: list[str]) -> list:
    """Compare snapshot bytes/exec flag with the HEAD blob of every high-risk path.
    Direct comparison: index flags (assume-unchanged/skip-worktree) cannot hide a change."""
    tree = _head_tree(root, high_risk)
    entries, budget = [], [MAX_REVIEW_DIFF_BYTES]
    for name in high_risk:
        head = tree.get(name)
        snap = manifest.get(name)
        current = _read_input(source / name)[0] if snap else None
        head_ok = (head is not None and head[1] == "blob" and head[0] in ("100644", "100755"))
        if (head_ok and snap is not None and _blob_id(current, head[2]) == head[2]
                and (head[0] == "100755") == bool(snap["mode"] & 0o111)):
            continue
        head_bytes = None
        if head_ok and head[3] is not None and head[3] <= MAX_DIFF_INPUT:
            head_bytes = _git(root, "cat-file", "blob", head[2])
        entry = {
            "path": name,
            "change": ("added" if not head_ok else "deleted" if snap is None else "modified"),
            "head_blob": head[2] if head_ok else None,
            "head_mode": head[0] if head_ok else None,
            "head_size": head[3] if head_ok else None,
            "head_sha256": hashlib.sha256(head_bytes).hexdigest() if head_bytes is not None else None,
            "snapshot_sha256": snap["sha256"] if snap else None,
            "snapshot_size": snap["size"] if snap else None,
            "snapshot_executable": bool(snap["mode"] & 0o111) if snap else None,
        }
        if head_ok and head_bytes is None and snap is not None:
            entry.update(diff="", diff_truncated=False, diff_omitted="too_large")
        elif snap is not None and snap["size"] > MAX_DIFF_INPUT:
            entry.update(diff="", diff_truncated=False, diff_omitted="too_large")
        else:
            entry.update(_diff(name, head_bytes, current, budget))
        entries.append(entry)
    return entries


def freeze_source(root: Path, destination: Path, *, review_policy: str, inputs: dict,
                  high_risk_inputs) -> dict:
    """Freeze covered inputs into destination/source (owner-only) and classify high-risk changes.

    git_head: every high-risk input must equal reviewed HEAD bytes and flags in both the
    worktree and the snapshot, else REVIEW_REQUIRED; other dirty/untracked inputs freeze.
    reviewed_snapshot: high-risk changes are allowed, recorded, and a bounded private
    review artifact is written to destination/review.json (never returned).
    """
    if review_policy not in REVIEW_POLICIES:
        raise HostRuntimeError("UNSUPPORTED_SOURCE_POLICY")
    if (not isinstance(high_risk_inputs, (list, tuple))
            or any(not source_input_covers(inputs, path) for path in high_risk_inputs)):
        raise HostRuntimeError("UNSAFE_BUILD_INPUT")
    high_risk = sorted(set(high_risk_inputs))
    try:
        root = Path(root).resolve(strict=True)
    except OSError as exc:
        raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
    destination = Path(destination).absolute()
    private_dir(destination.parent)
    try:
        os.mkdir(destination, 0o700)
    except FileExistsError as exc:
        raise HostRuntimeError("SNAPSHOT_EXISTS") from exc
    except OSError as exc:
        raise HostRuntimeError("STATE_UNAVAILABLE") from exc
    try:
        private_dir(destination)
        if _git(root, "rev-parse", "--show-prefix").strip():
            raise HostRuntimeError("SOURCE_INCOMPLETE")  # Paths are repository-root relative.
        before = git_state(root)
        source = destination / "source"
        try:
            os.mkdir(source, 0o700)
        except OSError:
            raise HostRuntimeError("STATE_UNAVAILABLE") from None
        frozen = inventory_source(root, inputs, copy_to=source)
        # Close common edit races: the live tree and the private copy must still match.
        if (inventory_source(root, inputs)["digest"] != frozen["digest"]
                or inventory_source(source, inputs)["digest"] != frozen["digest"]):
            raise HostRuntimeError("SOURCE_CHANGED")
        manifest = {item["path"]: item for item in frozen["files"]}
        changed = [path for path in before["changed_paths"] if source_input_covers(inputs, path)]
        review = _high_risk_review(root, source, manifest, high_risk)
        review_path = None
        review_digest = None
        if review_policy == "git_head":
            if review or any(path in high_risk for path in changed):
                raise HostRuntimeError("REVIEW_REQUIRED")
            try:
                verify_reviewed_high_risk_inputs(root, paths=high_risk)
            except OSError:
                raise HostRuntimeError("SOURCE_UNREADABLE") from None
        else:
            review_path = destination / "review.json"
            try:
                _save_private_json(review_path, {"version": 1, "git_head": before["head"],
                                                 "source_digest": frozen["digest"],
                                                 "files": review})
                data = review_path.read_bytes()
            except OSError:
                raise HostRuntimeError("STATE_UNAVAILABLE") from None
            if len(data) > MAX_REVIEW_BYTES:
                raise HostRuntimeError("REVIEW_TOO_LARGE")
            review_digest = hashlib.sha256(data).hexdigest()
        if git_state(root)["head"] != before["head"]:
            raise HostRuntimeError("SOURCE_CHANGED")
        return {
            "review_policy": review_policy,
            "source_digest": frozen["digest"],
            "file_count": frozen["file_count"],
            "total_bytes": frozen["total_bytes"],
            "files": frozen["files"],
            "git_head": before["head"],
            "changed_paths": changed,
            "high_risk_changes": [entry["path"] for entry in review],
            "review_digest": review_digest,
            "snapshot": str(source),
            "review_artifact": str(review_path) if review_path else None,
        }
    except BaseException as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, OSError):
            # Any remaining OS-level failure is an input read; never expose its path.
            raise HostRuntimeError("SOURCE_UNREADABLE") from None
        raise
