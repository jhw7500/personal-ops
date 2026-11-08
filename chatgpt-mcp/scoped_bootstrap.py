"""Prepare a pinned, text-only bootstrap review; never install or grant trust."""
from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

from host_runtime_config import HostRuntimeError
from runtime_rollout_web import _catalog, _expected_snapshots, _source_bytes


APPROVED_PACKAGE_SHA256 = "7da79b4192df1ab16f5f7e35cc27c01d6a4ed0511be9ed02541a9b3cb2bebe4e"
PACKAGE_FILES = frozenset((
    "owner_scoped_delivery.py", "register_owner_scoped_delivery.py",
    "runtime_rollout_scoped_source.py", "make_official_rollout_patch.py",
    "scoped-source-release-v1.json", "README.md", "INTEGRATION.md",
    "tests/test_scoped_delivery.py", "tests/test_runtime_rollout_scoped_source.py",
    "tests/test_make_official_rollout_patch.py"))
ROOT_FILES = ("chatgpt-mcp/runtime_rollout_web.py", "chatgpt-mcp/install-runtime.py",
              "chatgpt-mcp/rollout-host-runtime.py")
NEW_FILES = {
    "chatgpt-mcp/runtime_rollout_scoped_source.py": "runtime_rollout_scoped_source.py",
    "chatgpt-mcp/tests/test_runtime_rollout_scoped_source.py": "tests/test_runtime_rollout_scoped_source.py"}
DEFERRED_TOOL = ('"list_host_command_profiles", "prepare_host_command",',
                 '"list_host_command_profiles", "inspect_host_command_blockers", "prepare_host_command",')
DEFERRED_CANDIDATE_PATHS = ["chatgpt-mcp/" + name for name in (
    "host-command-approval.html", "host_command_tools.py",
    "tests/test_host_command_approval_ui.mjs", "tests/test_host_command_tools.py")]
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024


class PreparationError(ValueError):
    """Public errors contain a bounded code, never source text or paths."""


def _regular_bytes(path: Path, limit: int, code: str) -> bytes:
    try:
        if path.absolute() != path.resolve(strict=True):
            raise PreparationError(code)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_size > limit):
                raise PreparationError(code)
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or len(data) > limit
                    or (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_ctime_ns)):
                raise PreparationError(code)
            return data
        finally:
            os.close(fd)
    except (OSError, RuntimeError):
        raise PreparationError(code) from None


def _package(path: Path) -> dict[str, bytes]:
    data = _regular_bytes(path, MAX_ARCHIVE_BYTES, "PACKAGE_UNSAFE")
    if hashlib.sha256(data).hexdigest() != APPROVED_PACKAGE_SHA256:
        raise PreparationError("PACKAGE_HASH_MISMATCH")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(names) != len(set(names))
                    or set(names) != PACKAGE_FILES | {"INTEGRATION_MANIFEST.json"}
                    or any(entry.is_dir() or entry.flag_bits & 1
                           or entry.file_size > MAX_MEMBER_BYTES
                           or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                           for entry in entries)
                    or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES):
                raise PreparationError("PACKAGE_LAYOUT_UNSAFE")
            files = {name: archive.read(name) for name in names}
        manifest = json.loads(files["INTEGRATION_MANIFEST.json"])
        expected = {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(PACKAGE_FILES)}
        if (manifest.get("package") != "personal_ops_runtime_rollout_scoped_integration_v1"
                or manifest.get("status") != "SOURCE_ONLY_NOT_INSTALLED"
                or manifest.get("files") != expected):
            raise PreparationError("PACKAGE_CONTENT_MISMATCH")
        return files
    except (zipfile.BadZipFile, OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("PACKAGE_CONTENT_MISMATCH") from None


def _git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                 "-C", str(root), *args], env=env, capture_output=True,
                                text=True, timeout=30, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise PreparationError("SOURCE_UNSAFE") from None


def _source_commit(root: Path) -> str:
    try:
        if root.absolute() != root.resolve(strict=True) or not root.is_dir():
            raise PreparationError("SOURCE_UNSAFE")
    except (OSError, RuntimeError):
        raise PreparationError("SOURCE_UNSAFE") from None
    if _git(root, "rev-parse", "--show-toplevel") != str(root):
        raise PreparationError("SOURCE_UNSAFE")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"):
        raise PreparationError("SOURCE_NOT_CLEAN")
    return _git(root, "rev-parse", "--verify", "HEAD")


def _transform_data(files: dict[str, bytes]):
    # Literal AST data only: package code is never imported, executed or extracted.
    try:
        tree = ast.parse(files["make_official_rollout_patch.py"])
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("ROOT_FILES", "NEW_FILES"):
                        if target.id in values:
                            raise ValueError
                        values[target.id] = ast.literal_eval(node.value)
        transforms = values["ROOT_FILES"]
        if set(transforms) != set(ROOT_FILES) or values["NEW_FILES"] != NEW_FILES:
            raise ValueError
        deferred = 0
        for name, pairs in transforms.items():
            if not isinstance(pairs, list) or not 1 <= len(pairs) <= 20:
                raise ValueError
            for pair in pairs:
                if (not isinstance(pair, tuple) or len(pair) != 2
                        or not all(isinstance(x, str) and x for x in pair)):
                    raise ValueError
                if pair == DEFERRED_TOOL:
                    if name != "chatgpt-mcp/rollout-host-runtime.py":
                        raise ValueError
                    deferred += 1
        if deferred != 1:
            raise ValueError
        return transforms
    except (SyntaxError, KeyError, ValueError, TypeError, RecursionError):
        raise PreparationError("PACKAGE_TRANSFORMS_UNSAFE") from None


def _build_patch(root: Path, files: dict[str, bytes], catalog: dict):
    transforms = _transform_data(files)
    chunks = []
    outputs = {}
    catalog_hashes = {record["path"]: record["sha256"] for record in catalog["files"]}
    for name in ROOT_FILES:
        data, _mode = _source_bytes(root / name, root)
        if hashlib.sha256(data).hexdigest() != catalog_hashes.get(name):
            raise PreparationError("SOURCE_CHANGED")
        old = data.decode("utf-8")
        new = old
        for before, after in transforms[name]:
            if (before, after) == DEFERRED_TOOL:
                continue
            if new.count(before) != 1:
                raise PreparationError("SOURCE_VERSION_MISMATCH")
            new = new.replace(before, after, 1)
        outputs[name] = hashlib.sha256(new.encode()).hexdigest()
        chunks.extend(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                           fromfile="a/" + name, tofile="b/" + name))
    for target, member in NEW_FILES.items():
        path = root / target
        if path.exists() or path.is_symlink():
            raise PreparationError("SOURCE_ALREADY_INTEGRATED")
        data = files[member].decode("utf-8")
        outputs[target] = hashlib.sha256(files[member]).hexdigest()
        chunks.extend(difflib.unified_diff([], data.splitlines(keepends=True),
                                           fromfile="/dev/null", tofile="b/" + target))
    return "".join(chunks).encode(), outputs


def _open_output_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        descriptor = os.open("/", flags)
        parts = path.parts[1:]
        for index, part in enumerate((None, *parts)):
            if part is not None:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            leaf = index == len(parts)
            # A root-owned sticky shared ancestor (for example /tmp) protects
            # its owner's private child. The selected parent itself is private.
            sticky_ancestor = not leaf and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or leaf and info.st_uid != os.getuid()
                    or info.st_mode & 0o022 and not sticky_ancestor):
                raise PreparationError("OUTPUT_UNSAFE")
        result, descriptor = descriptor, None
        return result
    except OSError:
        raise PreparationError("OUTPUT_UNSAFE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _output_parent(output: Path, root: Path) -> int:
    if output.exists() or output.is_symlink():
        raise PreparationError("OUTPUT_EXISTS")
    try:
        parent = output.parent
        if parent.absolute() != parent.resolve(strict=True) or output.is_relative_to(root):
            raise PreparationError("OUTPUT_UNSAFE")
        return _open_output_directory(parent)
    except (OSError, RuntimeError):
        raise PreparationError("OUTPUT_UNSAFE") from None


def _assert_output_parent(output: Path, parent_fd: int):
    checked = None
    try:
        checked = _open_output_directory(output.parent)
        before, current = os.fstat(parent_fd), os.fstat(checked)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise PreparationError("OUTPUT_PARENT_CHANGED")
    except PreparationError:
        raise PreparationError("OUTPUT_PARENT_CHANGED") from None
    finally:
        if checked is not None:
            os.close(checked)


def _publish(output: Path, parent_fd: int, patch: bytes, manifest: dict):
    _assert_output_parent(output, parent_fd)
    try:
        os.mkdir(output.name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        raise PreparationError("OUTPUT_EXISTS") from None
    os.chmod(output.name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
    fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent_fd)
    try:
        for name, data in (("bootstrap.patch", patch), ("preparation.json",
                           (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())):
            child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                            0o600, dir_fd=fd)
            with os.fdopen(child, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)
    _assert_output_parent(output, parent_fd)
    os.fsync(parent_fd)


def prepare(package: Path, source_root: Path, output: Path) -> dict:
    """Read validated inputs and exclusively publish text; never apply or install."""
    package, root, output = Path(package).absolute(), Path(source_root).absolute(), Path(output).absolute()
    files = _package(package)
    commit = _source_commit(root)
    parent_fd = _output_parent(output, root)
    try:
        return _prepare_review(root, output, files, commit, parent_fd)
    finally:
        os.close(parent_fd)


def _prepare_review(root: Path, output: Path, files: dict[str, bytes], commit: str, parent_fd: int):
    try:
        catalog = _catalog(root)
        source_target, source_control = _expected_snapshots(root)
        patch, outputs = _build_patch(root, files, catalog)
        if _catalog(root) != catalog:
            raise PreparationError("SOURCE_CHANGED")
        try:
            if _source_commit(root) != commit:
                raise PreparationError("SOURCE_CHANGED")
        except PreparationError:
            raise PreparationError("SOURCE_CHANGED") from None
    except HostRuntimeError:
        raise PreparationError("SOURCE_UNSAFE") from None
    except UnicodeError:
        raise PreparationError("SOURCE_UNSAFE") from None
    manifest = {
        "version": 1, "status": "PREPARED_NOT_INSTALLED", "baseline_eligible": False,
        "package_sha256": APPROVED_PACKAGE_SHA256, "source_commit": commit,
        "source_catalog_sha256": catalog["digest"], "source_files": catalog["files"],
        "source_runtime_snapshot": source_target, "source_control_sha256": source_control,
        "package_files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "affected_paths": list(ROOT_FILES) + list(NEW_FILES), "prepared_file_sha256": outputs,
        "patch_sha256": hashlib.sha256(patch).hexdigest(), "patch_bytes": len(patch),
        "deferred_candidate_paths": DEFERRED_CANDIDATE_PATHS,
        "deferred_tool": "inspect_host_command_blockers",
        "required_next_steps": ["independent_patch_review", "owner_baseline_verification",
                                "separately_authorized_bootstrap", "separate_candidate_approval"],
    }
    try:
        _publish(output, parent_fd, patch, manifest)
    except OSError:
        # A failed write never publishes a PREPARED result on stdout; do not delete an unknown path.
        raise PreparationError("OUTPUT_WRITE_FAILED") from None
    return manifest
