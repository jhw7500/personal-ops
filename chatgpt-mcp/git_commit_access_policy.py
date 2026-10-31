"""Pure fail-closed policy for trusted owner-approved local-commit records.

This module never issues, loads, persists, renews or revokes authority. Inputs
must be measured/loaded by the installed host adapter, not supplied by a tool
caller. Existing source, workspace, message and staged-index checks still apply.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any

MAX_DURATION_SECONDS = 86_400
_NANOSECONDS = 1_000_000_000
_PROJECT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BOOT = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_DEFAULT_PROTECTED = frozenset({"main", "master"})
_SCOPE_FIELDS = frozenset({"project", "repo", "repo_identity", "git_dir", "git_identity", "branch", "uid", "boot_id", "runtime_digest"})
_GRANT_FIELDS = _SCOPE_FIELDS | {"version", "grant_id", "state", "operations", "expected_head", "approved_at", "expires_at", "approved_monotonic_ns", "expires_monotonic_ns", "duration_seconds"}
_CONTEXT_FIELDS = _SCOPE_FIELDS | {"head", "protected_branches"}
_PUSH_FIELDS = {"origin", "remote_head", "push_limit", "pushes_used"}
MAX_PUSHES = 10

@dataclass(frozen=True)
class GrantDecision:
    allowed: bool
    reason: str
    grant_id: str | None = None

def _string_matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None

def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0

def _canonical_path(value: Any) -> bool:
    if type(value) is not str or not 1 < len(value) <= 4096 or not value.isprintable():
        return False
    path = PurePosixPath(value)
    return (value.startswith("/") and not value.startswith("//") and ".." not in path.parts and str(path) == value)

def _identity(value: Any) -> bool:
    return (type(value) is list and len(value) == 2 and _nonnegative_int(value[0]) and type(value[1]) is int and value[1] > 0)

def _branch(value: Any) -> bool:
    if not _string_matches(value, _BRANCH):
        return False
    return (value not in {"HEAD", "@"} and not value.startswith("refs/") and ".." not in value and "//" not in value
            and not value.endswith(("/", ".")) and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))

def _scope(record: dict) -> bool:
    return (_string_matches(record.get("project"), _PROJECT)
            and _canonical_path(record.get("repo")) and _identity(record.get("repo_identity"))
            and _canonical_path(record.get("git_dir")) and _identity(record.get("git_identity"))
            and _branch(record.get("branch")) and _nonnegative_int(record.get("uid"))
            and _string_matches(record.get("boot_id"), _BOOT) and _string_matches(record.get("runtime_digest"), _DIGEST))

def _valid_grant(record: Any) -> bool:
    if type(record) is not dict:
        return False
    version = record.get("version")
    fields = _GRANT_FIELDS | (_PUSH_FIELDS if version == 2 else set())
    if type(version) is not int or version not in (1, 2) or set(record) != fields or not _scope(record):
        return False
    if version == 2 and (record["operations"] != ["commit", "push"]
            or type(record["origin"]) is not str or not 1 < len(record["origin"]) <= 4096
            or not record["origin"].isprintable()
            or not _string_matches(record["remote_head"], _HEAD)
            or type(record["push_limit"]) is not int or not 1 <= record["push_limit"] <= MAX_PUSHES
            or type(record["pushes_used"]) is not int
            or not 0 <= record["pushes_used"] <= record["push_limit"]):
        return False
    if (type(record["version"]) is not int or record["version"] not in (1, 2)
            or not _string_matches(record["grant_id"], _ID)
            or type(record["state"]) is not str or record["state"] not in {"active", "revoked"}
            or type(record["operations"]) is not list
            or record["operations"] != (["commit"] if version == 1 else ["commit", "push"])
            or not _string_matches(record["expected_head"], _HEAD)):
        return False
    duration = record["duration_seconds"]
    if type(duration) is not int or not 1 <= duration <= MAX_DURATION_SECONDS:
        return False
    for field in ("approved_at", "expires_at", "approved_monotonic_ns", "expires_monotonic_ns"):
        if not _nonnegative_int(record[field]):
            return False
    return (record["expires_at"] - record["approved_at"] == duration
            and record["expires_monotonic_ns"] - record["approved_monotonic_ns"] == duration * _NANOSECONDS)

def _valid_context(context: Any) -> bool:
    if type(context) is not dict or set(context) != _CONTEXT_FIELDS or not _scope(context):
        return False
    protected = context["protected_branches"]
    return (_string_matches(context["head"], _HEAD) and type(protected) is list and len(protected) <= 128 and all(_branch(name) for name in protected))

def evaluate_stored_commit_grant(grant: object, context: object, *, action: object,
                                now_epoch_seconds: object, now_monotonic_ns: object) -> GrantDecision:
    """Evaluate trusted data without mutation or authority issuance."""
    if type(action) is not str or action not in {"commit", "push"} or (action == "push" and (type(grant) is not dict or grant.get("version") != 2)):
        return GrantDecision(False, "ACTION_NOT_ALLOWED")
    if grant is None:
        return GrantDecision(False, "MISSING_GRANT")
    if not _valid_grant(grant):
        return GrantDecision(False, "INVALID_GRANT")
    if not _valid_context(context):
        return GrantDecision(False, "INVALID_CONTEXT")
    if not _nonnegative_int(now_epoch_seconds) or not _nonnegative_int(now_monotonic_ns):
        return GrantDecision(False, "INVALID_CLOCK")
    if grant["state"] != "active":
        return GrantDecision(False, "REVOKED")
    if now_epoch_seconds < grant["approved_at"] or now_monotonic_ns < grant["approved_monotonic_ns"]:
        return GrantDecision(False, "CLOCK_REGRESSED")
    if now_epoch_seconds >= grant["expires_at"] or now_monotonic_ns >= grant["expires_monotonic_ns"]:
        return GrantDecision(False, "EXPIRED")
    if context["branch"] in _DEFAULT_PROTECTED | frozenset(context["protected_branches"]):
        return GrantDecision(False, "PROTECTED_BRANCH")
    for field in _SCOPE_FIELDS:
        if grant[field] != context[field]:
            return GrantDecision(False, "SCOPE_MISMATCH")
    if grant["expected_head"] != context["head"]:
        return GrantDecision(False, "HEAD_CHANGED")
    if action == "push" and grant["pushes_used"] >= grant["push_limit"]:
        return GrantDecision(False, "PUSH_LIMIT_REACHED")
    return GrantDecision(True, "ACTIVE", grant["grant_id"])
