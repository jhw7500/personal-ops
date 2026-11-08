"""Owner-approved scoped source selector for the *official* RuntimeRolloutWeb.

This module performs NO staging, installation, service changes, or approvals.
It selects a private candidate only after owner registration, then leaves
all installation effects to the existing approval-bound RuntimeRolloutWeb.

Do not make source path a chat-supplied argument or a host-command override.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

from host_runtime_config import HostRuntimeError

POLICY_ID = "personal-ops-scoped-source-delivery"
POLICY_FILENAME = "scoped-source-delivery.json"
ALLOWED = frozenset({
    "chatgpt-mcp/host-command-approval.html",
    "chatgpt-mcp/tests/test_host_command_approval_ui.mjs",
    "chatgpt-mcp/host_command_tools.py",
    "chatgpt-mcp/tests/test_host_command_tools.py",
})
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX20 = re.compile(r"[0-9a-f]{20}\Z")
POLICY_KEYS = frozenset({
    "version", "policy_id", "project_root", "baseline_root", "baseline_digest",
    "candidate_root", "release_request_sha256", "changes", "baseline_runtime_snapshot",
})
ATTESTATION_KEYS = frozenset({
    "status", "policy_id", "baseline_digest", "candidate_digest",
    "file_count", "modified_paths",
})
MAX_POLICY_BYTES = 65536
MAX_ATTEST_BYTES = 16384


def _deny(code: str):
    raise HostRuntimeError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_file(path: Path, max_bytes: int) -> bytes:
    """Read a private one-link policy/attestation via a stable nofollow fd."""
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 1 <= before.st_size <= max_bytes):
            _deny("SCOPED_POLICY_UNTRUSTED")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            observed = os.fstat(fd)
            data = os.read(fd, max_bytes + 1)
            current = os.fstat(fd)
        finally:
            os.close(fd)
        metadata = lambda st: (st.st_dev, st.st_ino, st.st_mtime_ns,
                               st.st_ctime_ns, st.st_size, st.st_nlink, st.st_mode)
        if (metadata(before) != metadata(observed) or metadata(before) != metadata(current)
                or len(data) != before.st_size):
            _deny("SCOPED_POLICY_CHANGED")
        return data
    except (OSError, ValueError) as exc:
        raise HostRuntimeError("SCOPED_POLICY_UNAVAILABLE") from exc


def _owned_dir(path: Path, *, private: bool = False) -> Path:
    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            _deny("SCOPED_SOURCE_UNSAFE")
        st = path.lstat()
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o022
                or private and stat.S_IMODE(st.st_mode) != 0o700):
            _deny("SCOPED_SOURCE_UNSAFE")
        return path
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, HostRuntimeError):
            raise
        raise HostRuntimeError("SCOPED_SOURCE_UNSAFE") from exc


def _private_json(path: Path, max_bytes: int) -> tuple[dict, str]:
    data = _owner_file(path, max_bytes)
    try:
        obj = json.loads(data, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise HostRuntimeError("SCOPED_POLICY_INVALID") from exc
    if not isinstance(obj, dict):
        _deny("SCOPED_POLICY_INVALID")
    return obj, _sha(data)


def _pairs(items):
    out = dict(items)
    if len(out) != len(items):
        raise ValueError("duplicate key")
    return out


def _check_policy(policy: dict, project: Path, home: Path) -> tuple[Path, Path, Path]:
    if (set(policy) != POLICY_KEYS or type(policy.get("version")) is not int
            or policy["version"] != 1 or policy.get("policy_id") != POLICY_ID
            or not isinstance(policy.get("changes"), dict)
            or set(policy["changes"]) != ALLOWED
            or any(not isinstance(policy.get(key), str)
                   or not HEX64.fullmatch(policy[key]) for key in
                   ("baseline_digest", "release_request_sha256"))
            or not isinstance(policy.get("baseline_runtime_snapshot"), str)
            or not HEX20.fullmatch(policy["baseline_runtime_snapshot"])):
        _deny("SCOPED_POLICY_INVALID")
    for value in policy["changes"].values():
        if (not isinstance(value, dict) or set(value) != {"sha256", "bytes"}
                or not isinstance(value["sha256"], str)
                or not HEX64.fullmatch(value["sha256"])
                or type(value["bytes"]) is not int
                or value["bytes"] <= 0 or value["bytes"] > 4 * 1024 * 1024):
            _deny("SCOPED_POLICY_INVALID")
    # Exact standard owner locations, never arbitrary paths from the chat.
    # The owner approves a *source-layout* baseline, not the installed runtime
    # directory (which has a different layout). Its exact absolute path and
    # baseline hash were frozen at real-TTY registration.
    if not isinstance(policy.get("baseline_root"), str):
        _deny("SCOPED_POLICY_INVALID")
    base_expected = Path(policy["baseline_root"])
    candidate_expected = home / ".local/state/chatgpt-coding-mcp/scoped-source-delivery"
    if (not base_expected.is_absolute() or base_expected == project
            or policy.get("project_root") != str(project)
            or policy.get("candidate_root") != str(candidate_expected)):
        _deny("SCOPED_POLICY_ROOT_MISMATCH")
    _owned_dir(home)
    _owned_dir(home / ".config")
    _owned_dir(home / ".config/chatgpt-coding-mcp")
    _owned_dir(home / ".local")
    _owned_dir(home / ".local/state")
    _owned_dir(home / ".local/state/chatgpt-coding-mcp")
    _owned_dir(candidate_expected, private=True)
    _owned_dir(base_expected)
    if (base_expected.is_relative_to(project) or project.is_relative_to(base_expected)
            or candidate_expected.is_relative_to(project)
            or candidate_expected.is_relative_to(base_expected)):
        _deny("SCOPED_SOURCE_UNSAFE")
    return base_expected, candidate_expected, project


def select_scoped_source(project_root: Path, *, catalog: Callable,
                         expected_snapshots: Callable,
                         owner_home: Path | None = None) -> tuple[Path, str, str]:
    """Return (root, mode, policy hash) for the official RuntimeRolloutWeb.

    An absent policy preserves legacy behavior. A present but invalid policy or
    incomplete candidate always fails closed. No model-supplied paths allowed.
    """
    project_root = Path(project_root).resolve(strict=True)
    home = Path.home() if owner_home is None else Path(owner_home)
    path = home / ".config/chatgpt-coding-mcp" / POLICY_FILENAME
    try:
        path.lstat()
    except FileNotFoundError:
        return project_root, "project", "0" * 64
    except OSError as exc:
        raise HostRuntimeError("SCOPED_POLICY_UNAVAILABLE") from exc
    policy, policy_digest = _private_json(path, MAX_POLICY_BYTES)
    baseline, candidate_parent, _ = _check_policy(policy, project_root, home)
    original = catalog(baseline)
    if (original["digest"] != policy["baseline_digest"]
            or expected_snapshots(baseline)[0] != policy["baseline_runtime_snapshot"]):
        _deny("SCOPED_BASELINE_CHANGED")
    baseline_files = {row["path"]: row for row in original["files"]}
    if not ALLOWED.issubset(baseline_files):
        _deny("SCOPED_BASELINE_INCOMPLETE")
    # Bootstrap dependency: the authorized baseline must already contain the
    # official adapter. A 4-file overlay cannot silently install an adapter.
    if "chatgpt-mcp/runtime_rollout_scoped_source.py" not in baseline_files:
        _deny("SCOPED_ADAPTER_BOOTSTRAP_REQUIRED")
    expected_files = dict(baseline_files)
    for rel, info in policy["changes"].items():
        expected_files[rel] = {"path": rel, "sha256": info["sha256"], "bytes": info["bytes"]}
    expected_records = [expected_files[k] for k in sorted(expected_files)]
    candidate_digest = _sha(json.dumps(expected_records, sort_keys=True,
                                       separators=(",", ":")).encode())
    candidate_id = "candidate-" + candidate_digest[:24]
    candidate = _owned_dir(candidate_parent / candidate_id, private=True)
    observed = catalog(candidate)
    if observed["digest"] != candidate_digest or observed["files"] != expected_records:
        _deny("SCOPED_CANDIDATE_CHANGED")
    attestation, _ = _private_json(candidate / "source-delivery-attestation.json", MAX_ATTEST_BYTES)
    if (set(attestation) != ATTESTATION_KEYS
            or attestation != {
                "status": "CANDIDATE_ONLY_NOT_INSTALLED", "policy_id": POLICY_ID,
                "baseline_digest": policy["baseline_digest"],
                "candidate_digest": candidate_digest,
                "file_count": len(expected_records),
                "modified_paths": sorted(ALLOWED),
            }):
        _deny("SCOPED_ATTESTATION_INVALID")
    # Run the official manifest/control validation, too; it must include every
    # installed file, not only the four changes. No side effects occur here.
    target_snapshot, control_digest = expected_snapshots(candidate)
    if (not HEX20.fullmatch(target_snapshot) or not HEX64.fullmatch(control_digest)):
        _deny("SCOPED_SNAPSHOT_INVALID")
    return candidate, "owner_scoped", policy_digest
