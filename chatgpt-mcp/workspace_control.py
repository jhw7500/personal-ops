"""Stable launch admission for workspace-policy-aware MCP releases.

The deployed controller bundle lives outside rollback releases at
``~/.local/lib/chatgpt-coding-mcp/workspace-controller``.  It contains this
module, ``workspace-launcher.py`` and ``controller-manifest.json`` in a 0700
directory with 0600 files.  The launcher imports only that adjacent bundle;
it never imports Python code from the candidate release before admission.

Owner CLI mutations are offline preparation operations.  Runtime rollout,
process draining, legacy-grant invalidation and service transitions remain a
separate explicitly approved workflow.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


def _default_authority_dir() -> Path:
    state_home = Path(os.environ.get(
        "XDG_STATE_HOME", str(Path.home() / ".local" / "state"),
    ))
    return state_home / "chatgpt-coding-mcp" / "workspace-authority"


def _default_projects_path() -> Path:
    config_home = Path(os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config"),
    ))
    return config_home / "chatgpt-coding-mcp" / "projects.json"


DEFAULT_AUTHORITY_DIR = _default_authority_dir()
DEFAULT_CONTROLLER_DIR = (Path.home() / ".local/lib/chatgpt-coding-mcp"
                          / "workspace-controller")
MANDATORY_RELEASE_FILES = frozenset({
    "server.py", "admin_server.py", "execution.py", "jobs.py", "job_tools.py",
    "workspace_paths.py", "workspace_policy.py", "workspace_authority.py",
    "workspace_jobs.py", "workspace_tools.py", "workspace_control.py",
    "workspace_files.py", "workspace_host.py", "workspace-launcher.py",
    "workspace-access-approval.html", "runtime_rollout_web.py",
    "runtime_rollout_worker.py",
})
ENFORCEMENT_ABI = 2
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
_STATE_LIMIT = 2 * 1024 * 1024
_OPERATIONS = {"list", "read", "write"}
_ENFORCEMENT_SUFFIX = ".enforcement-required"
_ACTIVATION_LOCK_SUFFIX = ".activation-maintenance.lock"
_ACTIVATION_INTENT_SUFFIX = ".activation-maintenance.json"
_STARTUP_BARRIER_SUFFIX = ".activation-startup.json"
_MINIMUM_ABI_SUFFIX = ".minimum-enforcement-abi"


class WorkspaceControlError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code, cause=None):
    error = WorkspaceControlError(code)
    if cause is None:
        raise error
    raise error from cause


def _private_dir(path: Path, code: str) -> int:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        info = os.fstat(descriptor)
    except OSError as exc:
        _fail(code, exc)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(descriptor)
        _fail(code)
    return descriptor


def _private_file(directory_fd: int, name: str, code: str, limit: int) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd,
        )
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
            _fail(code)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            _fail(code)
        return payload
    except WorkspaceControlError:
        raise
    except FileNotFoundError as exc:
        missing = ("WORKSPACE_AUTHORITY_MISSING"
                   if code == "UNSAFE_WORKSPACE_AUTHORITY" else code)
        _fail(missing, exc)
    except OSError as exc:
        _fail(code, exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _json_object(payload: bytes, code: str) -> dict:
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate key")
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        _fail(code, exc)
    if not isinstance(value, dict):
        _fail(code)
    return value


def enforcement_path(authority_dir) -> Path:
    authority = Path(authority_dir).absolute()
    return authority.parent / ("." + authority.name + _ENFORCEMENT_SUFFIX)


def activation_paths(authority_dir=None) -> tuple[Path, Path]:
    """Return the stable gate paths adjacent to the canonical authority root."""
    authority = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    prefix = "." + authority.name
    return (authority.parent / (prefix + _ACTIVATION_LOCK_SUFFIX),
            authority.parent / (prefix + _ACTIVATION_INTENT_SUFFIX))


def startup_barrier_path(authority_dir=None) -> Path:
    authority = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    return authority.parent / ("." + authority.name + _STARTUP_BARRIER_SUFFIX)


def minimum_abi_path(authority_dir=None) -> Path:
    authority = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    return authority.parent / ("." + authority.name + _MINIMUM_ABI_SUFFIX)


def _minimum_enforcement_abi(authority_dir: Path) -> int:
    path = minimum_abi_path(authority_dir)
    if not path.exists() and not path.is_symlink():
        _fail("WORKSPACE_MINIMUM_ABI_MISSING")
    parent_fd = _private_dir(path.parent, "UNSAFE_WORKSPACE_AUTHORITY")
    try:
        value = _json_object(
            _private_file(
                parent_fd, path.name, "UNSAFE_WORKSPACE_AUTHORITY", 1024),
            "WORKSPACE_MINIMUM_ABI_INVALID",
        )
    finally:
        os.close(parent_fd)
    expected = hashlib.sha256(str(authority_dir).encode()).hexdigest()
    if (set(value) != {"version", "authority_path_sha256", "minimum_abi"}
            or value.get("version") != 1
            or value.get("authority_path_sha256") != expected
            or type(value.get("minimum_abi")) is not int
            or value["minimum_abi"] < 1):
        _fail("WORKSPACE_MINIMUM_ABI_INVALID")
    return value["minimum_abi"]


def _activation_parent(authority_dir: Path) -> tuple[int, str, str]:
    lock_path, intent_path = activation_paths(authority_dir)
    parent_fd = _private_dir(lock_path.parent, "UNSAFE_WORKSPACE_ACTIVATION")
    return parent_fd, lock_path.name, intent_path.name


def _activation_file_info(descriptor: int, parent_fd: int, name: str) -> os.stat_result:
    try:
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
        _fail("UNSAFE_WORKSPACE_ACTIVATION")
    if info.st_size != 32:
        _fail("WORKSPACE_ACTIVATION_CHANGED")
    return info


def _rename_activation_lock_noreplace(
        parent_fd: int, source: str, destination: str) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), 1,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _create_activation_lock(parent_fd: int, name: str) -> int | None:
    temporary = ".activation-lock-" + os.urandom(16).hex()
    descriptor = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, 0o600)
        marker = os.urandom(32)
        view = memoryview(marker)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("UNSAFE_WORKSPACE_ACTIVATION")
            view = view[written:]
        os.fsync(descriptor)
        try:
            _rename_activation_lock_noreplace(
                parent_fd, temporary, name)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return None
            raise
        published = True
        os.fsync(parent_fd)
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _open_activation_lock(parent_fd: int, name: str, *, create: bool) -> tuple[int, os.stat_result]:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        if create:
            try:
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                descriptor = _create_activation_lock(parent_fd, name)
                if descriptor is None:
                    descriptor = os.open(name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        return descriptor, _activation_file_info(descriptor, parent_fd, name)
    except WorkspaceControlError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)


def _intent_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _lock_marker_digest(descriptor: int) -> str:
    try:
        marker = os.pread(descriptor, 33, 0)
    except OSError as exc:
        _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
    if len(marker) != 32:
        _fail("WORKSPACE_ACTIVATION_CHANGED")
    return hashlib.sha256(marker).hexdigest()


def _validate_activation_intent(value, lock_info: os.stat_result) -> dict:
    required = {
        "version", "transaction_id", "source_digest", "lock_device",
        "lock_inode", "lock_marker_sha256", "created_at", "intent_sha256",
    }
    if (not isinstance(value, dict) or set(value) != required
            or value.get("version") != 1
            or not isinstance(value.get("transaction_id"), str)
            or _IDENTIFIER.fullmatch(value["transaction_id"]) is None
            or not isinstance(value.get("source_digest"), str)
            or _DIGEST.fullmatch(value["source_digest"]) is None
            or type(value.get("lock_device")) is not int
            or type(value.get("lock_inode")) is not int
            or value["lock_device"] < 0 or value["lock_inode"] < 1
            or not isinstance(value.get("lock_marker_sha256"), str)
            or _DIGEST.fullmatch(value["lock_marker_sha256"]) is None
            or not isinstance(value.get("created_at"), (int, float))
            or not math.isfinite(value["created_at"])
            or not isinstance(value.get("intent_sha256"), str)
            or _DIGEST.fullmatch(value["intent_sha256"]) is None):
        _fail("WORKSPACE_ACTIVATION_INVALID")
    unsigned = {key: item for key, item in value.items() if key != "intent_sha256"}
    if _intent_digest(unsigned) != value["intent_sha256"]:
        _fail("WORKSPACE_ACTIVATION_INVALID")
    if (value["lock_device"], value["lock_inode"]) != (
            lock_info.st_dev, lock_info.st_ino):
        _fail("WORKSPACE_ACTIVATION_CHANGED")
    return value


def _read_activation_intent(parent_fd: int, name: str,
                            lock_info: os.stat_result, lock_fd: int) -> dict:
    value = _json_object(
        _private_file(parent_fd, name, "UNSAFE_WORKSPACE_ACTIVATION", 8192),
        "WORKSPACE_ACTIVATION_INVALID",
    )
    intent = _validate_activation_intent(value, lock_info)
    if intent["lock_marker_sha256"] != _lock_marker_digest(lock_fd):
        _fail("WORKSPACE_ACTIVATION_CHANGED")
    return intent


def _binding(value, code="INVALID_WORKSPACE_ACTIVATION") -> dict:
    required = {"installation_id", "authority_epoch", "policy_digest"}
    if (not isinstance(value, dict) or set(value) != required
            or not isinstance(value.get("installation_id"), str)
            or _IDENTIFIER.fullmatch(value["installation_id"]) is None
            or type(value.get("authority_epoch")) is not int
            or value["authority_epoch"] < 1
            or not isinstance(value.get("policy_digest"), str)
            or _DIGEST.fullmatch(value["policy_digest"]) is None):
        _fail(code)
    return value


def _startup_barrier_digest(value: dict) -> str:
    return _intent_digest(value)


def _validate_startup_barrier(value, intent, lock_info, lock_fd) -> dict:
    required = {
        "version", "transaction_id", "source_digest", "lock_device",
        "lock_inode", "lock_marker_sha256", "binding", "barrier_sha256",
    }
    if (not isinstance(value, dict) or set(value) != required
            or value.get("version") != 1
            or value.get("transaction_id") != intent["transaction_id"]
            or value.get("source_digest") != intent["source_digest"]
            or value.get("lock_device") != lock_info.st_dev
            or value.get("lock_inode") != lock_info.st_ino
            or value.get("lock_marker_sha256") != _lock_marker_digest(lock_fd)
            or not isinstance(value.get("barrier_sha256"), str)
            or _DIGEST.fullmatch(value["barrier_sha256"]) is None):
        _fail("WORKSPACE_STARTUP_BARRIER_INVALID")
    _binding(value.get("binding"), "WORKSPACE_STARTUP_BARRIER_INVALID")
    unsigned = {key: item for key, item in value.items() if key != "barrier_sha256"}
    if _startup_barrier_digest(unsigned) != value["barrier_sha256"]:
        _fail("WORKSPACE_STARTUP_BARRIER_INVALID")
    return value


def _read_startup_barrier(parent_fd, name, intent, lock_info, lock_fd):
    value = _json_object(
        _private_file(parent_fd, name, "UNSAFE_WORKSPACE_ACTIVATION", 8192),
        "WORKSPACE_STARTUP_BARRIER_INVALID",
    )
    return _validate_startup_barrier(value, intent, lock_info, lock_fd)


@contextmanager
def _launch_admission(authority_dir):
    authority = Path(authority_dir).absolute()
    lock_path, intent_path = activation_paths(authority)
    barrier_path = startup_barrier_path(authority)
    lock_present = lock_path.exists() or lock_path.is_symlink()
    intent_present = intent_path.exists() or intent_path.is_symlink()
    barrier_present = barrier_path.exists() or barrier_path.is_symlink()
    if not lock_present and not intent_present and not barrier_present:
        yield None
        return
    if not lock_present or (barrier_present and not intent_present):
        _fail("WORKSPACE_STARTUP_BARRIER_INVALID")
    parent_fd = lock_fd = None
    try:
        parent_fd, lock_name, intent_name = _activation_parent(authority)
        lock_fd, lock_info = _open_activation_lock(parent_fd, lock_name, create=False)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            _fail("WORKSPACE_ACTIVATION_IN_PROGRESS", exc)
        except OSError as exc:
            _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
        if not intent_present:
            yield None
            return
        intent = _read_activation_intent(parent_fd, intent_name, lock_info, lock_fd)
        if not barrier_present:
            _fail("WORKSPACE_ACTIVATION_IN_PROGRESS")
        barrier = _read_startup_barrier(
            parent_fd, barrier_path.name, intent, lock_info, lock_fd)
        yield barrier
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


@contextmanager
def activation_admission(authority_dir=None, *, create=False):
    """Hold the shared activation gate across one fresh durable admission.

    Read-only probes preserve legacy absence when ``create`` is false. Mutable
    admissions atomically create the permanent lock before proceeding so an
    owner maintenance attempt always synchronizes on the same inode.
    """
    authority = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    lock_path, intent_path = activation_paths(authority)
    lock_present = lock_path.exists() or lock_path.is_symlink()
    intent_present = intent_path.exists() or intent_path.is_symlink()
    if not create and not lock_present and not intent_present:
        yield None
        return
    if not create and not lock_present:
        _fail("WORKSPACE_ACTIVATION_INVALID")
    parent_fd = lock_fd = None
    try:
        parent_fd, lock_name, intent_name = _activation_parent(authority)
        lock_fd, lock_info = _open_activation_lock(
            parent_fd, lock_name, create=create)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            _fail("WORKSPACE_ACTIVATION_IN_PROGRESS", exc)
        except OSError as exc:
            _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
        if (intent_path.exists() or intent_path.is_symlink()):
            _read_activation_intent(parent_fd, intent_name, lock_info, lock_fd)
            _fail("WORKSPACE_ACTIVATION_IN_PROGRESS")
        yield None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


class ActivationMaintenance:
    """Exclusive owner gate.  Closing it never clears durable intent."""

    def __init__(self, authority_dir, transaction_id, source_digest):
        self.authority_dir = Path(authority_dir).absolute()
        self.transaction_id = transaction_id
        self.source_digest = source_digest
        self.parent_fd = None
        self.lock_fd = None
        self.intent = None
        self.barrier = None
        self._intent_name = None
        self._finished = False

    def __enter__(self):
        if (not isinstance(self.transaction_id, str)
                or _IDENTIFIER.fullmatch(self.transaction_id) is None
                or not isinstance(self.source_digest, str)
                or _DIGEST.fullmatch(self.source_digest) is None):
            _fail("INVALID_WORKSPACE_ACTIVATION")
        self.parent_fd, lock_name, self._intent_name = _activation_parent(
            self.authority_dir)
        try:
            self.lock_fd, lock_info = _open_activation_lock(
                self.parent_fd, lock_name, create=True)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                _fail("WORKSPACE_ACTIVATION_BUSY", exc)
            except OSError as exc:
                _fail("UNSAFE_WORKSPACE_ACTIVATION", exc)
            intent_path = activation_paths(self.authority_dir)[1]
            if intent_path.exists() or intent_path.is_symlink():
                intent = _read_activation_intent(
                    self.parent_fd, self._intent_name, lock_info, self.lock_fd)
                if (intent["transaction_id"] != self.transaction_id
                        or intent["source_digest"] != self.source_digest):
                    _fail("WORKSPACE_ACTIVATION_CHANGED")
            else:
                unsigned = {
                    "version": 1,
                    "transaction_id": self.transaction_id,
                    "source_digest": self.source_digest,
                    "lock_device": lock_info.st_dev,
                    "lock_inode": lock_info.st_ino,
                    "lock_marker_sha256": _lock_marker_digest(self.lock_fd),
                    "created_at": time.time(),
                }
                intent = {**unsigned, "intent_sha256": _intent_digest(unsigned)}
                _atomic_private_write(
                    activation_paths(self.authority_dir)[1].parent,
                    self._intent_name,
                    json.dumps(intent, sort_keys=True, separators=(",", ":")).encode(),
                )
                intent = _read_activation_intent(
                    self.parent_fd, self._intent_name, lock_info, self.lock_fd)
            self.intent = intent
            barrier_path = startup_barrier_path(self.authority_dir)
            if barrier_path.exists() or barrier_path.is_symlink():
                self.barrier = _read_startup_barrier(
                    self.parent_fd, barrier_path.name, intent, lock_info, self.lock_fd)
            return self
        except BaseException:
            self._close()
            raise

    def _close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None

    def __exit__(self, _type, _value, _traceback):
        self._close()

    def _verify_binding(self, expected_binding):
        if self.parent_fd is None or self.lock_fd is None or self._finished:
            _fail("WORKSPACE_ACTIVATION_INVALID")
        _binding(expected_binding)
        lock_info = _activation_file_info(
            self.lock_fd, self.parent_fd, activation_paths(self.authority_dir)[0].name)
        current_intent = _read_activation_intent(
            self.parent_fd, self._intent_name, lock_info, self.lock_fd)
        if current_intent != self.intent:
            _fail("WORKSPACE_ACTIVATION_CHANGED")
        if not _enforcement_active(self.authority_dir):
            _fail("WORKSPACE_ENFORCEMENT_REQUIRED")
        state = _authority_state(self.authority_dir)
        if state is None:
            _fail("WORKSPACE_AUTHORITY_MISSING")
        actual = {
            "installation_id": state["installation_id"],
            "authority_epoch": state["authority_epoch"],
            "policy_digest": state["policy_digest"],
        }
        if actual != expected_binding:
            _fail("WORKSPACE_AUTHORITY_CHANGED")
        return actual, lock_info

    def allow_start(self, expected_binding) -> dict:
        actual, lock_info = self._verify_binding(expected_binding)
        unsigned = {
            "version": 1,
            "transaction_id": self.transaction_id,
            "source_digest": self.source_digest,
            "lock_device": lock_info.st_dev,
            "lock_inode": lock_info.st_ino,
            "lock_marker_sha256": _lock_marker_digest(self.lock_fd),
            "binding": actual,
        }
        barrier = {
            **unsigned, "barrier_sha256": _startup_barrier_digest(unsigned),
        }
        path = startup_barrier_path(self.authority_dir)
        if path.exists() or path.is_symlink():
            current = _read_startup_barrier(
                self.parent_fd, path.name, self.intent, lock_info, self.lock_fd)
            if current != barrier:
                _fail("WORKSPACE_ACTIVATION_CHANGED")
        else:
            _atomic_private_write(
                path.parent, path.name,
                json.dumps(barrier, sort_keys=True, separators=(",", ":")).encode(),
            )
            current = _read_startup_barrier(
                self.parent_fd, path.name, self.intent, lock_info, self.lock_fd)
            if current != barrier:
                _fail("WORKSPACE_STARTUP_BARRIER_INVALID")
        self.barrier = barrier
        self._close()
        return {
            "status": "workspace_startup_allowed",
            "transaction_id": self.transaction_id,
            "source_digest": self.source_digest,
            "binding": actual,
        }

    def finish(self, expected_binding) -> dict:
        actual, lock_info = self._verify_binding(expected_binding)
        barrier_path = startup_barrier_path(self.authority_dir)
        if barrier_path.exists() or barrier_path.is_symlink():
            barrier = _read_startup_barrier(
                self.parent_fd, barrier_path.name, self.intent, lock_info, self.lock_fd)
            if self.barrier is not None and barrier != self.barrier:
                _fail("WORKSPACE_ACTIVATION_CHANGED")
        try:
            if barrier_path.exists() or barrier_path.is_symlink():
                os.unlink(barrier_path.name, dir_fd=self.parent_fd)
            os.unlink(self._intent_name, dir_fd=self.parent_fd)
            os.fsync(self.parent_fd)
        except OSError as exc:
            _fail("WORKSPACE_ACTIVATION_INVALID", exc)
        self._finished = True
        result = {
            "status": "activation_maintenance_finished",
            "transaction_id": self.transaction_id,
            "source_digest": self.source_digest,
            "binding": actual,
        }
        # The durable intent is gone and its directory entry is synced.  Drop
        # the exclusive gate now so the verified launcher may take shared
        # admission before the surrounding owner context exits.
        self._close()
        return result


def activation_maintenance(authority_dir, transaction_id, source_digest):
    return ActivationMaintenance(authority_dir, transaction_id, source_digest)


def _enforcement_active(authority_dir: Path) -> bool:
    path = enforcement_path(authority_dir)
    if not path.exists() and not path.is_symlink():
        return False
    parent_fd = _private_dir(path.parent, "UNSAFE_WORKSPACE_AUTHORITY")
    try:
        payload = _private_file(
            parent_fd, path.name, "UNSAFE_WORKSPACE_AUTHORITY", 128,
        )
    finally:
        os.close(parent_fd)
    expected = (hashlib.sha256(str(authority_dir).encode()).hexdigest() + "\n").encode()
    if payload != expected:
        _fail("WORKSPACE_AUTHORITY_INVALID")
    return True


def _authority_state(authority_dir: Path) -> dict | None:
    if not authority_dir.exists() and not authority_dir.is_symlink():
        return None
    root_fd = _private_dir(authority_dir, "UNSAFE_WORKSPACE_AUTHORITY")
    lock_fd = None
    try:
        try:
            lock_fd = os.open(
                "authority.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            lock_info = os.fstat(lock_fd)
            lock_current = os.stat(
                "authority.lock", dir_fd=root_fd, follow_symlinks=False,
            )
            if (not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600
                    or lock_info.st_nlink != 1
                    or (lock_info.st_dev, lock_info.st_ino)
                    != (lock_current.st_dev, lock_current.st_ino)):
                _fail("UNSAFE_WORKSPACE_AUTHORITY")
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except FileNotFoundError as exc:
            _fail("WORKSPACE_AUTHORITY_MISSING", exc)
        except OSError as exc:
            _fail("UNSAFE_WORKSPACE_AUTHORITY", exc)

        state = _json_object(
            _private_file(root_fd, "authority.json", "UNSAFE_WORKSPACE_AUTHORITY",
                          _STATE_LIMIT),
            "WORKSPACE_AUTHORITY_INVALID",
        )
        activation = _private_file(
            root_fd, "activated", "UNSAFE_WORKSPACE_AUTHORITY", 64,
        )
        cursor_key = _private_file(
            root_fd, "cursor.key", "UNSAFE_WORKSPACE_AUTHORITY", 64,
        )
        required = {
            "version", "installation_id", "authority_epoch", "policy_digest",
            "allowed_releases", "requests", "grants", "updated_at",
        }
        if (set(state) != required or state.get("version") != 1
                or not isinstance(state.get("installation_id"), str)
                or _IDENTIFIER.fullmatch(state["installation_id"]) is None
                or activation != state["installation_id"].encode("ascii")
                or len(cursor_key) != 32
                or type(state.get("authority_epoch")) is not int
                or state["authority_epoch"] < 1
                or not isinstance(state.get("policy_digest"), str)
                or _DIGEST.fullmatch(state["policy_digest"]) is None
                or type(state.get("updated_at")) not in (int, float)
                or not math.isfinite(state["updated_at"])
                or not isinstance(state.get("allowed_releases"), dict)
                or not isinstance(state.get("requests"), dict)
                or not isinstance(state.get("grants"), dict)):
            _fail("WORKSPACE_AUTHORITY_INVALID")
        for digest, abi in state["allowed_releases"].items():
            if (not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                    or type(abi) is not int or abi < 1):
                _fail("WORKSPACE_AUTHORITY_INVALID")
        for request_id, request in state["requests"].items():
            base = {
                "request_id", "project", "path", "scope_kind", "operations",
                "duration_seconds", "purpose", "requested_at", "request_expires_at",
                "authority_epoch", "policy_digest", "status",
            }
            optional = {
                "confirmation_digest", "grant_id", "cancelled_at", "tombstoned_at",
            }
            if (not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None
                    or not isinstance(request, dict)
                    or not base.issubset(request)
                    or not set(request).issubset(base | optional)
                    or request.get("request_id") != request_id
                    or not isinstance(request.get("project"), str)
                    or not request["project"]
                    or not isinstance(request.get("path"), str)
                    or not Path(request["path"]).is_absolute()
                    or request.get("scope_kind") not in {"file", "directory"}
                    or not isinstance(request.get("operations"), list)
                    or not request["operations"]
                    or any(not isinstance(value, str) or value not in _OPERATIONS
                           for value in request["operations"])
                    or len(request["operations"]) != len(set(request["operations"]))
                    or type(request.get("duration_seconds")) is not int
                    or not 1 <= request["duration_seconds"] <= 2592000
                    or not isinstance(request.get("purpose"), str)
                    or not isinstance(request.get("requested_at"), (int, float))
                    or not math.isfinite(request["requested_at"])
                    or not isinstance(request.get("request_expires_at"), (int, float))
                    or not math.isfinite(request["request_expires_at"])
                    or not request["requested_at"] < request["request_expires_at"]
                    <= request["requested_at"] + 604800
                    or request.get("status") not in {
                        "pending", "confirmed", "cancelled", "expired", "tombstoned",
                    }
                    or type(request.get("authority_epoch")) is not int
                    or not isinstance(request.get("policy_digest"), str)
                    or _DIGEST.fullmatch(request["policy_digest"]) is None):
                _fail("WORKSPACE_AUTHORITY_INVALID")
            confirmation = request.get("confirmation_digest")
            if request["status"] == "pending":
                if (not isinstance(confirmation, str)
                        or _DIGEST.fullmatch(confirmation) is None):
                    _fail("WORKSPACE_AUTHORITY_INVALID")
            elif "confirmation_digest" in request:
                _fail("WORKSPACE_AUTHORITY_INVALID")
            if ("grant_id" in request
                    and (not isinstance(request["grant_id"], str)
                         or _IDENTIFIER.fullmatch(request["grant_id"]) is None)):
                _fail("WORKSPACE_AUTHORITY_INVALID")
            if (request["status"] == "pending"
                    and (request["authority_epoch"] != state["authority_epoch"]
                         or request["policy_digest"] != state["policy_digest"])):
                _fail("WORKSPACE_AUTHORITY_INVALID")
        for grant_id, grant in state["grants"].items():
            base = {
                "grant_id", "request_id", "project", "path", "scope_kind",
                "operations", "issued_at", "expires_at", "authority_epoch",
                "policy_digest", "status",
            }
            optional = {"revoked_at", "tombstoned_at"}
            if (not isinstance(grant_id, str) or _IDENTIFIER.fullmatch(grant_id) is None
                    or not isinstance(grant, dict)
                    or not base.issubset(grant)
                    or not set(grant).issubset(base | optional)
                    or grant.get("grant_id") != grant_id
                    or not isinstance(grant.get("request_id"), str)
                    or _IDENTIFIER.fullmatch(grant["request_id"]) is None
                    or not isinstance(grant.get("project"), str)
                    or not grant["project"]
                    or not isinstance(grant.get("path"), str)
                    or not Path(grant["path"]).is_absolute()
                    or grant.get("scope_kind") not in {"file", "directory"}
                    or not isinstance(grant.get("operations"), list)
                    or not grant["operations"]
                    or any(not isinstance(value, str) or value not in _OPERATIONS
                           for value in grant["operations"])
                    or len(grant["operations"]) != len(set(grant["operations"]))
                    or not isinstance(grant.get("issued_at"), (int, float))
                    or not math.isfinite(grant["issued_at"])
                    or not isinstance(grant.get("expires_at"), (int, float))
                    or not math.isfinite(grant["expires_at"])
                    or not grant["issued_at"] < grant["expires_at"]
                    <= grant["issued_at"] + 2592000
                    or grant.get("status") not in {"active", "revoked", "tombstoned"}
                    or type(grant.get("authority_epoch")) is not int
                    or not isinstance(grant.get("policy_digest"), str)
                    or _DIGEST.fullmatch(grant["policy_digest"]) is None):
                _fail("WORKSPACE_AUTHORITY_INVALID")
            request = state["requests"].get(grant["request_id"])
            if (not isinstance(request, dict) or request.get("grant_id") != grant_id
                    or request.get("project") != grant["project"]
                    or request.get("path") != grant["path"]):
                _fail("WORKSPACE_AUTHORITY_INVALID")
            if (grant["status"] == "active"
                    and (grant["authority_epoch"] != state["authority_epoch"]
                         or grant["policy_digest"] != state["policy_digest"])):
                _fail("WORKSPACE_AUTHORITY_INVALID")
        return state
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)


def _release_manifest(release_dir: Path) -> tuple[dict, str]:
    parent_fd = _private_dir(release_dir.parent, "UNSAFE_WORKSPACE_RELEASE")
    try:
        release_fd = None
        try:
            release_fd = os.open(
                release_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            info = os.fstat(release_fd)
            current = os.stat(
                release_dir.name, dir_fd=parent_fd, follow_symlinks=False,
            )
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                _fail("UNSAFE_WORKSPACE_RELEASE")
            raw = _private_file(
                release_fd, "manifest.json", "UNSAFE_WORKSPACE_RELEASE", 1024 * 1024,
            )
            manifest = _json_object(raw, "WORKSPACE_RELEASE_TAMPERED")
            if (not manifest or not all(
                    isinstance(name, str) and isinstance(digest, str)
                    and _DIGEST.fullmatch(digest) is not None
                    for name, digest in manifest.items())):
                _fail("WORKSPACE_RELEASE_TAMPERED")
            digest = hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest()
            if (release_dir.name != digest[:20]
                    or not re.fullmatch(r"[0-9a-f]{20}", release_dir.name)):
                _fail("WORKSPACE_RELEASE_TAMPERED")
            if not MANDATORY_RELEASE_FILES.issubset(manifest):
                _fail("WORKSPACE_RELEASE_INCOMPATIBLE")
            for name, expected in manifest.items():
                relative = Path(name)
                if (relative.is_absolute() or not relative.parts
                        or any(part in {"", ".", ".."} for part in relative.parts)):
                    _fail("WORKSPACE_RELEASE_TAMPERED")
                current_fd = os.dup(release_fd)
                try:
                    for component in relative.parts[:-1]:
                        next_fd = os.open(
                            component,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=current_fd,
                        )
                        os.close(current_fd)
                        current_fd = next_fd
                        directory_info = os.fstat(current_fd)
                        if (not stat.S_ISDIR(directory_info.st_mode)
                                or directory_info.st_uid != os.getuid()
                                or stat.S_IMODE(directory_info.st_mode) != 0o700):
                            _fail("UNSAFE_WORKSPACE_RELEASE")
                    payload = _private_file(
                        current_fd, relative.parts[-1], "UNSAFE_WORKSPACE_RELEASE",
                        16 * 1024 * 1024,
                    )
                except WorkspaceControlError:
                    raise
                except OSError as exc:
                    _fail("WORKSPACE_RELEASE_TAMPERED", exc)
                finally:
                    os.close(current_fd)
                if hashlib.sha256(payload).hexdigest() != expected:
                    _fail("WORKSPACE_RELEASE_TAMPERED")
            return manifest, digest
        except WorkspaceControlError:
            raise
        except OSError as exc:
            _fail("UNSAFE_WORKSPACE_RELEASE", exc)
        finally:
            if release_fd is not None:
                os.close(release_fd)
    finally:
        os.close(parent_fd)


def verify_release_manifest(release_dir) -> dict:
    """Verify an immutable release without entering runtime launch admission."""
    release_path = Path(release_dir).absolute()
    manifest, release_digest = _release_manifest(release_path)
    return {
        "release_digest": release_digest,
        "runtime": str(release_path),
        "files": len(manifest),
    }


def release_enforcement_abi(release_dir) -> int:
    """Return the literal ABI declared by manifest-covered control source.

    Historical releases without a declaration are ABI 1.  The source is
    parsed as syntax only and is never imported or executed.
    """
    release_path = Path(release_dir).absolute()
    manifest, _ = _release_manifest(release_path)
    if "workspace_control.py" not in manifest:
        _fail("WORKSPACE_RELEASE_INCOMPATIBLE")
    parent_fd = _private_dir(release_path.parent, "UNSAFE_WORKSPACE_RELEASE")
    release_fd = None
    try:
        release_fd = os.open(
            release_path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        payload = _private_file(
            release_fd, "workspace_control.py", "UNSAFE_WORKSPACE_RELEASE",
            16 * 1024 * 1024,
        )
    except OSError as exc:
        _fail("UNSAFE_WORKSPACE_RELEASE", exc)
    finally:
        if release_fd is not None:
            os.close(release_fd)
        os.close(parent_fd)
    try:
        tree = ast.parse(payload.decode("utf-8"), filename="workspace_control.py")
    except (SyntaxError, UnicodeError) as exc:
        _fail("WORKSPACE_RELEASE_ABI_MISMATCH", exc)
    declarations = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id == "ENFORCEMENT_ABI" for target in node.targets)):
            if (not isinstance(node.value, ast.Constant)
                    or type(node.value.value) is not int
                    or node.value.value < 1):
                _fail("WORKSPACE_RELEASE_ABI_MISMATCH")
            declarations.append(node.value.value)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.target.id == "ENFORCEMENT_ABI"):
            if (not isinstance(node.value, ast.Constant)
                    or type(node.value.value) is not int
                    or node.value.value < 1):
                _fail("WORKSPACE_RELEASE_ABI_MISMATCH")
            declarations.append(node.value.value)
    if not declarations:
        return 1
    if len(declarations) != 1:
        _fail("WORKSPACE_RELEASE_ABI_MISMATCH")
    return declarations[0]


def read_activation_binding(authority_dir) -> dict:
    """Read enforced authority for owner recovery; this grants no launch admission."""
    authority_path = Path(authority_dir).absolute()
    if not _enforcement_active(authority_path):
        _fail("WORKSPACE_ENFORCEMENT_REQUIRED")
    state = _authority_state(authority_path)
    if state is None:
        _fail("WORKSPACE_AUTHORITY_MISSING")
    return {key: state[key] for key in (
        "installation_id", "authority_epoch", "policy_digest")}


def verify_launch(release_dir, authority_dir=None, *, enforcement_required=False) -> dict:
    """Read-only launch gate used before target and rollback process starts."""
    authority_path = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    with _launch_admission(authority_path) as startup_barrier:
        enforced = _enforcement_active(authority_path)
        state = _authority_state(authority_path)
        if state is None:
            if enforced:
                _fail("WORKSPACE_AUTHORITY_MISSING")
            if enforcement_required:
                _fail("WORKSPACE_ENFORCEMENT_REQUIRED")
            return {"status": "legacy_allowed", "authority_active": False}
        if not enforced:
            _fail("WORKSPACE_ENFORCEMENT_REQUIRED")
        release_path = Path(release_dir).absolute()
        _, release_digest = _release_manifest(release_path)
        abi = state["allowed_releases"].get(release_digest)
        if abi is None:
            _fail("WORKSPACE_RELEASE_UNLISTED")
        minimum_abi = _minimum_enforcement_abi(authority_path)
        if abi < minimum_abi:
            _fail("WORKSPACE_RELEASE_INCOMPATIBLE")
        declared_abi = abi
        if minimum_abi >= ENFORCEMENT_ABI:
            declared_abi = release_enforcement_abi(release_path)
            if declared_abi != abi:
                _fail("WORKSPACE_RELEASE_ABI_MISMATCH")
        binding = {
            "installation_id": state["installation_id"],
            "authority_epoch": state["authority_epoch"],
            "policy_digest": state["policy_digest"],
        }
        if (startup_barrier is not None
                and startup_barrier["binding"] != binding):
            _fail("WORKSPACE_STARTUP_BARRIER_INVALID")
        return {
            "status": "launch_allowed",
            "authority_active": True,
            "release_digest": release_digest,
            "release_abi": abi,
            "enforcement_abi": declared_abi,
            "minimum_abi": minimum_abi,
            "runtime": str(release_path),
            "authority_dir": str(authority_path),
            "binding": binding,
        }


def _write_authority_state(authority_dir: Path, state: dict) -> None:
    payload = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    if len(payload) > _STATE_LIMIT:
        _fail("WORKSPACE_AUTHORITY_INVALID")
    _atomic_private_write(authority_dir, "authority.json", payload)


def transition_release(release_dir, authority_dir=None) -> dict:
    """Advance the durable epoch once immediately before one managed start."""
    authority_path = Path(
        authority_dir if authority_dir is not None else _default_authority_dir()
    ).absolute()
    release_path = Path(release_dir).absolute()
    _, release_digest = _release_manifest(release_path)
    verified = verify_launch(
        release_path, authority_path, enforcement_required=True,
    )
    root_fd = _private_dir(authority_path, "UNSAFE_WORKSPACE_AUTHORITY")
    lock_fd = None
    try:
        lock_fd = os.open(
            "authority.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            _fail("UNSAFE_WORKSPACE_AUTHORITY")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = _json_object(
            _private_file(root_fd, "authority.json", "UNSAFE_WORKSPACE_AUTHORITY",
                          _STATE_LIMIT),
            "WORKSPACE_AUTHORITY_INVALID",
        )
        if (set(state) != {
                "version", "installation_id", "authority_epoch", "policy_digest",
                "allowed_releases", "requests", "grants", "updated_at",
            } or state.get("version") != 1
                or state.get("installation_id") != verified["binding"]["installation_id"]
                or type(state.get("authority_epoch")) is not int
                or not isinstance(state.get("requests"), dict)
                or not isinstance(state.get("grants"), dict)
                or not isinstance(state.get("allowed_releases"), dict)
                or release_digest not in state["allowed_releases"]):
            _fail("WORKSPACE_AUTHORITY_CHANGED")
        now = time.time()
        state["authority_epoch"] += 1
        for request in state["requests"].values():
            if request.get("status") == "pending":
                request["status"] = "tombstoned"
                request["tombstoned_at"] = now
                request.pop("confirmation_digest", None)
        for grant in state["grants"].values():
            if grant.get("status") == "active":
                grant["status"] = "tombstoned"
                grant["tombstoned_at"] = now
        state["updated_at"] = now
        _write_authority_state(authority_path, state)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)
    result = verify_launch(release_path, authority_path, enforcement_required=True)
    result["status"] = "transitioned"
    return result


def verify_controller(controller_dir=DEFAULT_CONTROLLER_DIR) -> dict:
    """Verify the installed stable controller bundle and return its launcher."""
    directory = Path(controller_dir).absolute()
    root_fd = _private_dir(directory, "UNSAFE_WORKSPACE_CONTROLLER")
    try:
        manifest = _json_object(
            _private_file(root_fd, "controller-manifest.json",
                          "UNSAFE_WORKSPACE_CONTROLLER", 8192),
            "UNSAFE_WORKSPACE_CONTROLLER",
        )
        expected_names = {"workspace_control.py", "workspace-launcher.py"}
        if set(manifest) != expected_names or not all(
                isinstance(value, str) and _DIGEST.fullmatch(value) is not None
                for value in manifest.values()):
            _fail("UNSAFE_WORKSPACE_CONTROLLER")
        for name, expected in manifest.items():
            payload = _private_file(
                root_fd, name, "UNSAFE_WORKSPACE_CONTROLLER", 1024 * 1024,
            )
            if hashlib.sha256(payload).hexdigest() != expected:
                _fail("UNSAFE_WORKSPACE_CONTROLLER")
        return {"path": str(directory),
                "launcher": str(directory / "workspace-launcher.py"),
                "manifest": manifest}
    finally:
        os.close(root_fd)


def _atomic_private_write(directory: Path, name: str, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + name + ".", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        directory_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _controller_projects(projects) -> tuple[Path, ...]:
    if not isinstance(projects, dict) or not projects:
        _fail("INVALID_PROJECTS")
    roots = []
    for name, value in projects.items():
        if not isinstance(name, str) or not name:
            _fail("INVALID_PROJECTS")
        path = Path(value)
        if not path.is_absolute():
            _fail("INVALID_PROJECTS")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            _fail("INVALID_PROJECTS", exc)
        if not resolved.is_dir():
            _fail("INVALID_PROJECTS")
        roots.append(resolved)
    return tuple(roots)


def _configured_projects(path=None) -> dict:
    config = Path(path if path is not None else _default_projects_path())
    try:
        payload = _json_object(config.read_bytes(), "INVALID_PROJECTS")
    except OSError as exc:
        _fail("INVALID_PROJECTS", exc)
    if set(payload) != {"projects"}:
        _fail("INVALID_PROJECTS")
    projects = payload["projects"]
    roots = _controller_projects(projects)
    canonical_config = Path(os.path.realpath(config))
    if any(canonical_config.is_relative_to(root)
           or root.is_relative_to(canonical_config.parent)
           for root in roots):
        _fail("UNSAFE_PROJECTS_CONFIGURATION")
    return projects


def install_controller(destination=DEFAULT_CONTROLLER_DIR, source_dir=None,
                       *, projects=None) -> dict:
    """Install/update the stable two-file controller bundle outside releases."""
    destination = Path(destination).absolute()
    source = Path(source_dir or Path(__file__).resolve().parent).absolute()
    project_roots = _controller_projects(
        projects if projects is not None else _configured_projects()
    )
    canonical_destination = Path(os.path.realpath(destination))
    canonical_source = Path(os.path.realpath(source))
    if (destination.parent.name == "releases"
            or canonical_destination == canonical_source
            or any(canonical_destination.is_relative_to(root)
                   or root.is_relative_to(canonical_destination)
                   for root in project_roots)):
        _fail("UNSAFE_CONTROLLER_DESTINATION")
    payloads = {}
    for name in ("workspace_control.py", "workspace-launcher.py"):
        path = source / name
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o002):
                _fail("UNSAFE_CONTROLLER_SOURCE")
            chunks = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                _fail("UNSAFE_CONTROLLER_SOURCE")
            payloads[name] = b"".join(chunks)
        except OSError as exc:
            _fail("UNSAFE_CONTROLLER_SOURCE", exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    try:
        destination.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chmod(destination, 0o700, follow_symlinks=False)
    except OSError as exc:
        _fail("UNSAFE_CONTROLLER_DESTINATION", exc)
    root_fd = _private_dir(destination, "UNSAFE_CONTROLLER_DESTINATION")
    os.close(root_fd)
    lock_path = destination / "install.lock"
    try:
        try:
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(lock_fd, 0o600)
        except FileExistsError:
            lock_fd = os.open(
                lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            _fail("UNSAFE_CONTROLLER_DESTINATION")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        allowed = {
            "workspace_control.py", "workspace-launcher.py",
            "controller-manifest.json", "install.lock",
        }
        if set(os.listdir(destination)) - allowed:
            _fail("UNSAFE_CONTROLLER_DESTINATION")
        manifest = {name: hashlib.sha256(payload).hexdigest()
                    for name, payload in payloads.items()}
        for name, payload in payloads.items():
            _atomic_private_write(destination, name, payload)
        _atomic_private_write(
            destination, "controller-manifest.json",
            json.dumps(manifest, sort_keys=True).encode(),
        )
        return {"status": "controller_installed", "path": str(destination),
                "manifest": manifest}
    except WorkspaceControlError:
        raise
    except OSError as exc:
        _fail("UNSAFE_CONTROLLER_DESTINATION", exc)
    finally:
        if "lock_fd" in locals():
            os.close(lock_fd)


def _release_map(values) -> dict[str, int]:
    result = {}
    for value in values:
        try:
            digest, abi_text = value.rsplit(":", 1)
            abi = int(abi_text)
        except (ValueError, TypeError) as exc:
            _fail("INVALID_ALLOWED_RELEASES", exc)
        if _DIGEST.fullmatch(digest) is None or abi < 1:
            _fail("INVALID_ALLOWED_RELEASES")
        result[digest] = abi
    return result


def _projects(path) -> dict:
    payload = _json_object(Path(path).read_bytes(), "INVALID_PROJECTS")
    if not payload or not all(isinstance(name, str) and isinstance(root, str)
                              for name, root in payload.items()):
        _fail("INVALID_PROJECTS")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Owner workspace launch controller")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--release", required=True)
    verify.add_argument("--authority-dir")
    install = commands.add_parser("install-controller")
    install.add_argument("--destination", required=True)
    install.add_argument("--source-dir")
    install.add_argument("--projects-json")
    activation = commands.add_parser("activation")
    activation.add_argument("--release", required=True)
    activation.add_argument("activation_args", nargs=argparse.REMAINDER)
    for name in ("initialize", "advance", "migrate"):
        command = commands.add_parser(name)
        command.add_argument("--authority-dir", required=True)
        command.add_argument("--projects-json", required=True)
        command.add_argument("--policy-digest", required=True)
        command.add_argument("--allow-release", action="append", default=[])
        if name == "migrate":
            command.add_argument("--maintenance-drained", action="store_true")
            command.add_argument("--legacy-authority-invalidated", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "activation":
        release = Path(args.release).absolute()
        manifest, _ = _release_manifest(release)
        # Verify the complete release before loading any owner coordinator code.
        parent_fd = _private_dir(release, "UNSAFE_WORKSPACE_RELEASE")
        try:
            for name in ("workspace_activation.py", "workspace_activation_cli.py",
                         "workspace_activation_host.py", "workspace_activation_legacy.py"):
                if name not in manifest:
                    _fail("WORKSPACE_RELEASE_INCOMPATIBLE")
                _private_file(parent_fd, name, "WORKSPACE_RELEASE_INCOMPATIBLE", 1024 * 1024)
        finally:
            os.close(parent_fd)
        forwarded = args.activation_args
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        return subprocess.run(
            [sys.executable, "-B", str(release / "workspace_activation_cli.py"), *forwarded],
            check=False,
        ).returncode
    if args.command == "verify":
        result = verify_launch(args.release, args.authority_dir)
    elif args.command == "install-controller":
        result = install_controller(
            args.destination, args.source_dir,
            projects=_configured_projects(args.projects_json),
        )
    else:
        if args.command in {"initialize", "migrate"}:
            _fail("ACTIVATION_EVIDENCE_REQUIRED")
        from workspace_authority import WorkspaceAuthority
        authority = WorkspaceAuthority(
            _projects(args.projects_json), state_dir=args.authority_dir,
        )
        releases = _release_map(args.allow_release)
        result = authority.advance(args.policy_digest, releases)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkspaceControlError as exc:
        sys.stderr.write(exc.code + "\n")
        raise SystemExit(2)
