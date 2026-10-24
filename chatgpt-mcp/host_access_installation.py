"""Private installation-wide HostAccess state and irreversible v2 marker."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re

from host_runtime_config import HostRuntimeError, private_dir, private_file
from host_runtime_operations import _lock, _read_private_json, _save_private_json


MARKER_NAME = "installation-scope-v2.enabled"
STATE_NAME = "installation-scope-v2.json"
LOCK_NAME = "installation.lock"
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")


def _invalid(cause=None):
    error = HostRuntimeError("HOST_ACCESS_STATE_INVALID")
    if cause is None:
        raise error
    raise error from cause


def _timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed
    except (TypeError, ValueError, AttributeError) as exc:
        _invalid(exc)


class InstallationGrantState:
    """Exact private persistence contract for HostAccess v2."""

    def __init__(self, root):
        self.root = private_dir(Path(root))
        self.marker_path = self.root / MARKER_NAME
        self.state_path = self.root / STATE_NAME
        self.lock_path = self.root / LOCK_NAME

    def lock(self):
        return _lock(self.lock_path)

    def _marker(self):
        if not self.marker_path.exists() and not self.marker_path.is_symlink():
            return None
        try:
            marker = _read_private_json(self.marker_path)
        except (OSError, HostRuntimeError) as exc:
            _invalid(exc)
        if marker != {"version": 2, "state": STATE_NAME}:
            _invalid()
        return marker

    def enabled(self):
        if self._marker() is None:
            return False
        self.load()
        return True

    def mark_enabled(self):
        if self._marker() is not None:
            _invalid()
        _save_private_json(
            self.marker_path, {"version": 2, "state": STATE_NAME},
        )

    def create(self, state):
        if self.state_path.exists() or self.state_path.is_symlink():
            _invalid()
        self.validate(state)
        _save_private_json(self.state_path, state)

    def load(self):
        if self._marker() is None:
            _invalid()
        try:
            state = _read_private_json(self.state_path)
        except (OSError, HostRuntimeError) as exc:
            _invalid(exc)
        self.validate(state)
        return state

    def save(self, state):
        if self._marker() is None:
            _invalid()
        self.validate(state)
        _save_private_json(self.state_path, state)

    def invalidate_legacy(self, records_dir):
        records = private_dir(Path(records_dir))
        try:
            names = list(os.listdir(records))
            for name in names:
                path = records / name
                private_file(path)
                path.unlink()
            descriptor = os.open(
                records, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except (OSError, HostRuntimeError) as exc:
            _invalid(exc)

    @classmethod
    def validate(cls, state):
        required = {
            "version", "installation_id", "uid", "capability_generation",
            "grant_state_revision", "requests", "grant", "updated_at",
        }
        if (not isinstance(state, dict) or set(state) != required
                or state.get("version") != 2
                or not isinstance(state.get("installation_id"), str)
                or _IDENTIFIER.fullmatch(state["installation_id"]) is None
                or type(state.get("uid")) is not int
                or type(state.get("capability_generation")) is not int
                or state["capability_generation"] < 1
                or type(state.get("grant_state_revision")) is not int
                or state["grant_state_revision"] < 1
                or not isinstance(state.get("requests"), dict)
                or state.get("grant") is not None
                and not isinstance(state["grant"], dict)):
            _invalid()
        _timestamp(state["updated_at"])
        for identifier, request in state["requests"].items():
            if identifier != request.get("request_id"):
                _invalid()
            cls._validate_request(request, state)
        if state["grant"] is not None:
            cls._validate_grant(state["grant"], state)

    @staticmethod
    def _validate_common(value, state, *, request):
        identifier = value.get("request_id" if request else "grant_id")
        if (not isinstance(identifier, str)
                or _IDENTIFIER.fullmatch(identifier) is None
                or value.get("installation_id") != state["installation_id"]
                or value.get("uid") != state["uid"]
                or value.get("capability_generation")
                != state["capability_generation"]
                or type(value.get("grant_state_revision")) is not int
                or value["grant_state_revision"] < 1
                or value.get("lifetime") not in {"timed", "until_revoked"}):
            _invalid()
        _timestamp(value.get("created_at"))

    @classmethod
    def _validate_request(cls, request, state):
        expected = {
            "request_id", "project", "created_at", "expires_at", "lifetime",
            "grant_state_revision", "installation_id", "uid",
            "capability_generation",
        }
        if (not isinstance(request, dict) or set(request) != expected
                or not isinstance(request.get("project"), str)
                or not request["project"]):
            _invalid()
        cls._validate_common(request, state, request=True)
        _timestamp(request["expires_at"])

    @classmethod
    def _validate_grant(cls, grant, state):
        expected = {
            "grant_id", "created_at", "expires_at", "lifetime", "allow_codex",
            "revoked_at", "grant_state_revision", "installation_id", "uid",
            "capability_generation",
        }
        if (not isinstance(grant, dict) or set(grant) != expected
                or type(grant.get("allow_codex")) is not bool):
            _invalid()
        cls._validate_common(grant, state, request=False)
        if grant["lifetime"] == "timed":
            _timestamp(grant["expires_at"])
        elif grant["expires_at"] is not None:
            _invalid()
        _timestamp(grant["revoked_at"], nullable=True)
