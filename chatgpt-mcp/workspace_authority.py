"""Installation-scoped, owner-confirmed workspace path grants.

The mutable authority root is deliberately separate from writable projects.
Creating or migrating that root is an owner operation; constructing this class
never creates state or grants authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from workspace_paths import WorkspaceError


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
_OPERATIONS = {"list", "read", "write"}
_STATE_NAME = "authority.json"
_ACTIVATION_NAME = "activated"
_CURSOR_KEY_NAME = "cursor.key"
_LOCK_NAME = "authority.lock"
_LOCKS_NAME = "locks"
_ENFORCEMENT_SUFFIX = ".enforcement-required"
_MINIMUM_ABI_SUFFIX = ".minimum-enforcement-abi"
_PREPARATION_MARKER = ".prepare-"
_MAX_STATE_BYTES = 2 * 1024 * 1024
_REQUEST_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_DURATION_SECONDS = 30 * 24 * 60 * 60
_MAX_DURATION_SECONDS = 30 * 24 * 60 * 60


def _fail(code: str, cause: BaseException | None = None):
    error = WorkspaceError(code)
    if cause is None:
        raise error
    raise error from cause


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_digest(value, code="INVALID_POLICY_DIGEST") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail(code)
    return value


def _validate_releases(value) -> dict[str, int]:
    if not isinstance(value, dict):
        _fail("INVALID_ALLOWED_RELEASES")
    result = {}
    for digest, abi in value.items():
        if (not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                or type(abi) is not int or abi < 1):
            _fail("INVALID_ALLOWED_RELEASES")
        result[digest] = abi
    return result


def _validate_ancestor_chain(path: Path) -> None:
    """Reject linked or untrusted writable ancestors without creating anything."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    uid = os.getuid()
    for component in absolute.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            _fail("UNSAFE_AUTHORITY_PATH", exc)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail("UNSAFE_AUTHORITY_PATH")
        writable = stat.S_IMODE(info.st_mode) & 0o022
        trusted_sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in (0, uid) or (writable and not trusted_sticky_root):
            _fail("UNSAFE_AUTHORITY_PATH")


def _no_symlink_ancestors(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            _fail("INVALID_GRANT_PATH", exc)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail("INVALID_GRANT_PATH")


def _private_directory(path: Path, *, create: bool) -> int:
    if create:
        try:
            missing = []
            candidate = path
            while True:
                try:
                    candidate.lstat()
                    break
                except FileNotFoundError:
                    missing.append(candidate)
                    candidate = candidate.parent
            _validate_ancestor_chain(candidate)
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, parents=False, exist_ok=False)
                os.chmod(directory, 0o700, follow_symlinks=False)
        except OSError as exc:
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
    else:
        _validate_ancestor_chain(path.parent)
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        info = os.fstat(descriptor)
    except OSError as exc:
        _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(descriptor)
        _fail("UNSAFE_AUTHORITY_STATE")
    return descriptor


def _validate_file_info(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        _fail("UNSAFE_AUTHORITY_STATE")


def _open_private_file(root_fd: int, name: str, flags: int) -> int:
    descriptor = None
    try:
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd,
        )
        _validate_file_info(os.fstat(descriptor))
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("UNSAFE_AUTHORITY_STATE")
        return descriptor
    except WorkspaceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _fail("AUTHORITY_STATE_UNAVAILABLE", exc)


def _create_private_file(root_fd: int, name: str, payload: bytes) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.fchmod(descriptor, 0o600)
            _validate_file_info(os.fstat(descriptor))
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("AUTHORITY_STATE_UNAVAILABLE")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except WorkspaceError:
        raise
    except OSError as exc:
        _fail("AUTHORITY_STATE_UNAVAILABLE", exc)


def _read_private_bytes(root_fd: int, name: str, limit: int) -> bytes:
    descriptor = _open_private_file(root_fd, name, os.O_RDONLY)
    try:
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
            _fail("AUTHORITY_STATE_INVALID")
        return payload
    finally:
        os.close(descriptor)


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        _fail("AUTHORITY_STATE_UNAVAILABLE", exc)


def _rename_noreplace(source_fd: int, source: str,
                      destination_fd: int, destination: str) -> None:
    """Atomically publish a prepared directory without replacing any target."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


class WorkspaceAuthority:
    def __init__(self, projects, state_dir=None):
        if not isinstance(projects, dict) or not projects:
            _fail("INVALID_PROJECTS")
        normalized = {}
        for name, root in projects.items():
            if not isinstance(name, str) or not name:
                _fail("INVALID_PROJECTS")
            candidate = Path(root).absolute()
            normalized[name] = Path(os.path.realpath(candidate))
        if state_dir is None:
            state_home = Path(os.environ.get(
                "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
            ))
            state_dir = state_home / "chatgpt-coding-mcp" / "workspace-authority"
        self.projects = normalized
        self.state_dir = Path(state_dir).absolute()
        self._local = threading.local()

    @property
    def enforcement_path(self) -> Path:
        """Irreversible activation latch stored outside the deletable state root."""
        return self.state_dir.parent / ("." + self.state_dir.name + _ENFORCEMENT_SUFFIX)

    @property
    def minimum_abi_path(self) -> Path:
        return self.state_dir.parent / ("." + self.state_dir.name + _MINIMUM_ABI_SUFFIX)

    def _enforcement_payload(self) -> bytes:
        return (hashlib.sha256(str(self.state_dir).encode()).hexdigest() + "\n").encode()

    def _enforcement_exists(self) -> bool:
        path = self.enforcement_path
        if not path.exists() and not path.is_symlink():
            return False
        parent_fd = _private_directory(path.parent, create=False)
        try:
            payload = _read_private_bytes(parent_fd, path.name, 128)
        finally:
            os.close(parent_fd)
        if payload != self._enforcement_payload():
            _fail("AUTHORITY_ENFORCEMENT_INVALID")
        return True

    def _minimum_abi_payload(self, minimum_abi: int) -> bytes:
        return json.dumps({
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.state_dir).encode()).hexdigest(),
            "minimum_abi": minimum_abi,
        }, sort_keys=True, separators=(",", ":")).encode("ascii")

    def _read_minimum_abi(self, parent_fd: int, *, required: bool) -> int | None:
        try:
            payload = _read_private_bytes(
                parent_fd, self.minimum_abi_path.name, 1024)
        except WorkspaceError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                if required:
                    _fail("AUTHORITY_MINIMUM_ABI_MISSING", exc)
                return None
            raise

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
            _fail("AUTHORITY_MINIMUM_ABI_INVALID", exc)
        expected = hashlib.sha256(str(self.state_dir).encode()).hexdigest()
        if (not isinstance(value, dict)
                or set(value) != {
                    "version", "authority_path_sha256", "minimum_abi"}
                or value.get("version") != 1
                or value.get("authority_path_sha256") != expected
                or type(value.get("minimum_abi")) is not int
                or value["minimum_abi"] < 1):
            _fail("AUTHORITY_MINIMUM_ABI_INVALID")
        return value["minimum_abi"]

    def _ensure_minimum_abi(self, parent_fd: int, releases: dict[str, int],
                            *, create: bool) -> int:
        minimum_abi = self._read_minimum_abi(parent_fd, required=not create)
        if minimum_abi is None:
            minimum_abi = 1
            _create_private_file(
                parent_fd, self.minimum_abi_path.name,
                self._minimum_abi_payload(minimum_abi),
            )
            _fsync_directory(parent_fd)
        if any(abi < minimum_abi for abi in releases.values()):
            _fail("INVALID_ALLOWED_RELEASES")
        return minimum_abi

    def _allowed_releases_at(self, path: Path) -> dict[str, int]:
        root_fd = _private_directory(path, create=False)
        try:
            return self._load(root_fd)["allowed_releases"]
        finally:
            os.close(root_fd)

    def _check_state_location(self) -> None:
        state = Path(os.path.realpath(self.state_dir))
        for root in self.projects.values():
            if _is_within(state, root) or _is_within(root, state):
                _fail("AUTHORITY_STATE_IN_PROJECT")
        _validate_ancestor_chain(self.state_dir)

    def _root_fd(self, *, create=False) -> int:
        self._check_state_location()
        return _private_directory(self.state_dir, create=create)

    def _open_lock(self, root_fd: int, name=_LOCK_NAME, *, shared=False,
                   create=False) -> int:
        descriptor = None
        try:
            if create:
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=root_fd,
                    )
                    os.fchmod(descriptor, 0o600)
                except FileExistsError:
                    descriptor = os.open(
                        name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd,
                    )
            else:
                descriptor = os.open(
                    name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd,
                )
            _validate_file_info(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                _fail("UNSAFE_AUTHORITY_STATE")
            return descriptor
        except WorkspaceError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except FileNotFoundError as exc:
            if descriptor is not None:
                os.close(descriptor)
            _fail("AUTHORITY_STATE_MISSING", exc)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)

    def _validate_records(self, state: dict) -> None:
        request_base = {
            "request_id", "project", "path", "scope_kind", "operations",
            "duration_seconds", "purpose", "requested_at", "request_expires_at",
            "authority_epoch", "policy_digest", "status",
        }
        request_optional = {
            "confirmation_digest", "grant_id", "cancelled_at", "tombstoned_at",
        }
        for identifier, record in state["requests"].items():
            if (not isinstance(identifier, str) or _IDENTIFIER.fullmatch(identifier) is None
                    or not isinstance(record, dict)
                    or not request_base.issubset(record)
                    or not set(record).issubset(request_base | request_optional)
                    or record.get("request_id") != identifier
                    or record.get("project") not in self.projects
                    or not isinstance(record.get("path"), str)
                    or not Path(record["path"]).is_absolute()
                    or record.get("scope_kind") not in {"file", "directory"}
                    or not isinstance(record.get("operations"), list)
                    or not record["operations"]
                    or any(not isinstance(value, str) or value not in _OPERATIONS
                           for value in record["operations"])
                    or len(record["operations"]) != len(set(record["operations"]))
                    or type(record.get("duration_seconds")) is not int
                    or not 1 <= record["duration_seconds"] <= _MAX_DURATION_SECONDS
                    or not isinstance(record.get("purpose"), str)
                    or not isinstance(record.get("requested_at"), (int, float))
                    or not math.isfinite(record["requested_at"])
                    or not isinstance(record.get("request_expires_at"), (int, float))
                    or not math.isfinite(record["request_expires_at"])
                    or not record["requested_at"] < record["request_expires_at"]
                    <= record["requested_at"] + _REQUEST_TTL_SECONDS
                    or type(record.get("authority_epoch")) is not int
                    or not isinstance(record.get("policy_digest"), str)
                    or _DIGEST.fullmatch(record["policy_digest"]) is None
                    or record.get("status") not in {
                        "pending", "confirmed", "cancelled", "expired", "tombstoned",
                    }):
                _fail("AUTHORITY_STATE_INVALID")
            if record["status"] == "pending":
                digest = record.get("confirmation_digest")
                if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
                    _fail("AUTHORITY_STATE_INVALID")
            elif "confirmation_digest" in record:
                _fail("AUTHORITY_STATE_INVALID")
            if ("grant_id" in record
                    and (not isinstance(record["grant_id"], str)
                         or _IDENTIFIER.fullmatch(record["grant_id"]) is None)):
                _fail("AUTHORITY_STATE_INVALID")

        grant_base = {
            "grant_id", "request_id", "project", "path", "scope_kind", "operations",
            "issued_at", "expires_at", "authority_epoch", "policy_digest", "status",
        }
        grant_optional = {"revoked_at", "tombstoned_at"}
        for identifier, record in state["grants"].items():
            if (not isinstance(identifier, str) or _IDENTIFIER.fullmatch(identifier) is None
                    or not isinstance(record, dict)
                    or not grant_base.issubset(record)
                    or not set(record).issubset(grant_base | grant_optional)
                    or record.get("grant_id") != identifier
                    or not isinstance(record.get("request_id"), str)
                    or _IDENTIFIER.fullmatch(record["request_id"]) is None
                    or record.get("project") not in self.projects
                    or not isinstance(record.get("path"), str)
                    or not Path(record["path"]).is_absolute()
                    or record.get("scope_kind") not in {"file", "directory"}
                    or not isinstance(record.get("operations"), list)
                    or not record["operations"]
                    or any(not isinstance(value, str) or value not in _OPERATIONS
                           for value in record["operations"])
                    or len(record["operations"]) != len(set(record["operations"]))
                    or not isinstance(record.get("issued_at"), (int, float))
                    or not math.isfinite(record["issued_at"])
                    or not isinstance(record.get("expires_at"), (int, float))
                    or not math.isfinite(record["expires_at"])
                    or not record["issued_at"] < record["expires_at"]
                    <= record["issued_at"] + _MAX_DURATION_SECONDS
                    or type(record.get("authority_epoch")) is not int
                    or not isinstance(record.get("policy_digest"), str)
                    or _DIGEST.fullmatch(record["policy_digest"]) is None
                    or record.get("status") not in {"active", "revoked", "tombstoned"}):
                _fail("AUTHORITY_STATE_INVALID")
            request = state["requests"].get(record["request_id"])
            if (not isinstance(request, dict)
                    or request.get("grant_id") != identifier
                    or request.get("project") != record["project"]
                    or request.get("path") != record["path"]):
                _fail("AUTHORITY_STATE_INVALID")

    def _activation_exists(self, root_fd: int) -> bool:
        try:
            os.stat(_ACTIVATION_NAME, dir_fd=root_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)

    def _load(self, root_fd: int) -> dict:
        activated = self._activation_exists(root_fd)
        try:
            payload = _read_private_bytes(root_fd, _STATE_NAME, _MAX_STATE_BYTES)
        except WorkspaceError as exc:
            cause = exc.__cause__
            if isinstance(cause, FileNotFoundError):
                _fail("AUTHORITY_STATE_MISSING" if activated else "AUTHORITY_UNINITIALIZED")
            raise
        if not activated:
            _fail("AUTHORITY_STATE_INVALID")
        try:
            state = json.loads(
                payload,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeError, ValueError, RecursionError) as exc:
            _fail("AUTHORITY_STATE_INVALID", exc)
        required = {
            "version", "installation_id", "authority_epoch", "policy_digest",
            "allowed_releases", "requests", "grants", "updated_at",
        }
        if (not isinstance(state, dict) or set(state) != required
                or state.get("version") != 1
                or not isinstance(state.get("installation_id"), str)
                or _IDENTIFIER.fullmatch(state["installation_id"]) is None
                or type(state.get("authority_epoch")) is not int
                or state["authority_epoch"] < 1
                or not isinstance(state.get("requests"), dict)
                or not isinstance(state.get("grants"), dict)
                or not isinstance(state.get("updated_at"), (int, float))
                or not math.isfinite(state["updated_at"])):
            _fail("AUTHORITY_STATE_INVALID")
        _validate_digest(state.get("policy_digest"), "AUTHORITY_STATE_INVALID")
        try:
            state["allowed_releases"] = _validate_releases(state["allowed_releases"])
        except WorkspaceError:
            _fail("AUTHORITY_STATE_INVALID")
        self._validate_records(state)
        activation = _read_private_bytes(root_fd, _ACTIVATION_NAME, 64)
        if activation.decode("ascii", "replace") != state["installation_id"]:
            _fail("AUTHORITY_STATE_INVALID")
        try:
            key = _read_private_bytes(root_fd, _CURSOR_KEY_NAME, 64)
        except WorkspaceError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                _fail("AUTHORITY_STATE_MISSING")
            raise
        if len(key) != 32:
            _fail("AUTHORITY_STATE_INVALID")
        return state

    def _save(self, root_fd: int, state: dict) -> None:
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_STATE_BYTES:
            _fail("AUTHORITY_STATE_INVALID")
        temporary = ".authority-" + secrets.token_hex(16) + ".tmp"
        _create_private_file(root_fd, temporary, payload)
        try:
            os.replace(
                temporary, _STATE_NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd,
            )
            _fsync_directory(root_fd)
        except OSError as exc:
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _binding(state: dict) -> dict:
        return {
            "installation_id": state["installation_id"],
            "authority_epoch": state["authority_epoch"],
            "policy_digest": state["policy_digest"],
        }

    @staticmethod
    def _bound(state: dict, policy_digest: str) -> None:
        _validate_digest(policy_digest)
        if state["policy_digest"] != policy_digest:
            _fail("AUTHORITY_BINDING_STALE")

    def _preparation_name(self, transaction_id: str) -> str:
        if (not isinstance(transaction_id, str)
                or _IDENTIFIER.fullmatch(transaction_id) is None):
            _fail("INVALID_AUTHORITY_TRANSACTION")
        return "." + self.state_dir.name + _PREPARATION_MARKER + transaction_id

    def _initial_manifest(self, root_fd: int, *, published=False) -> tuple[str, str, dict]:
        expected = {
            _STATE_NAME, _ACTIVATION_NAME, _CURSOR_KEY_NAME, _LOCK_NAME, _LOCKS_NAME,
        }
        try:
            names = set(os.listdir(root_fd))
        except OSError as exc:
            _fail("AUTHORITY_PREPARATION_INVALID", exc)
        if names != expected:
            _fail("AUTHORITY_PREPARATION_INVALID")

        records = []
        root_info = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700
                or root_info.st_nlink != 3):
            _fail("AUTHORITY_PREPARATION_INVALID")
        records.append({
            "path": ".", "type": "directory", "device": root_info.st_dev,
            "inode": root_info.st_ino, "mode": 0o700, "uid": root_info.st_uid,
            "nlink": root_info.st_nlink,
        })

        for name in sorted(expected - {_LOCKS_NAME}):
            descriptor = None
            try:
                descriptor = _open_private_file(root_fd, name, os.O_RDONLY)
                chunks = []
                remaining = _MAX_STATE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                if len(payload) > _MAX_STATE_BYTES:
                    _fail("AUTHORITY_PREPARATION_INVALID")
                info = os.fstat(descriptor)
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    _fail("AUTHORITY_PREPARATION_INVALID")
            except WorkspaceError as exc:
                _fail("AUTHORITY_PREPARATION_INVALID", exc)
            except OSError as exc:
                _fail("AUTHORITY_PREPARATION_INVALID", exc)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            records.append({
                "path": name, "type": "file", "device": info.st_dev,
                "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid, "nlink": info.st_nlink, "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })

        locks_fd = None
        try:
            locks_fd = os.open(
                _LOCKS_NAME,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            info = os.fstat(locks_fd)
            current = os.stat(
                _LOCKS_NAME, dir_fd=root_fd, follow_symlinks=False,
            )
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or info.st_nlink != 2
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
                _fail("AUTHORITY_PREPARATION_INVALID")
            lock_names = set(os.listdir(locks_fd))
            if ((lock_names and not published)
                    or lock_names - {"workspace-writers.lock"}):
                _fail("AUTHORITY_PREPARATION_INVALID")
            # Published writers retain this lock inode. Validate its runtime
            # metadata without adding it to the original preparation identity.
            for name in lock_names:
                descriptor = None
                try:
                    descriptor = _open_private_file(
                        locks_fd, name, os.O_RDONLY | os.O_NONBLOCK,
                    )
                    opened = os.fstat(descriptor)
                    _validate_file_info(opened)
                    current_lock = os.stat(name, dir_fd=locks_fd, follow_symlinks=False)
                    if (opened.st_size != 0
                            or (current_lock.st_dev, current_lock.st_ino)
                            != (opened.st_dev, opened.st_ino)):
                        _fail("AUTHORITY_PREPARATION_INVALID")
                except WorkspaceError as exc:
                    _fail("AUTHORITY_PREPARATION_INVALID", exc)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            if set(os.listdir(locks_fd)) != lock_names:
                _fail("AUTHORITY_PREPARATION_INVALID")
            records.append({
                "path": _LOCKS_NAME, "type": "directory", "device": info.st_dev,
                "inode": info.st_ino, "mode": 0o700, "uid": info.st_uid,
                "nlink": info.st_nlink,
            })
        except WorkspaceError:
            raise
        except OSError as exc:
            _fail("AUTHORITY_PREPARATION_INVALID", exc)
        finally:
            if locks_fd is not None:
                os.close(locks_fd)

        try:
            state = self._load(root_fd)
        except WorkspaceError as exc:
            _fail("AUTHORITY_PREPARATION_INVALID", exc)
        encoded = json.dumps(
            records, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        identity = json.dumps(
            [record for record in records if record["path"] != _STATE_NAME],
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return (hashlib.sha256(encoded).hexdigest(),
                hashlib.sha256(identity).hexdigest(), self._binding(state))

    def _validate_prepared(self, prepared) -> dict:
        required = {
            "version", "transaction_id", "stage_name", "stage_device",
            "stage_inode", "manifest_sha256", "identity_sha256", "binding",
        }
        if (not isinstance(prepared, dict) or set(prepared) != required
                or prepared.get("version") != 1):
            _fail("AUTHORITY_PREPARATION_INVALID")
        transaction_id = prepared.get("transaction_id")
        try:
            expected_name = self._preparation_name(transaction_id)
        except WorkspaceError:
            _fail("AUTHORITY_PREPARATION_INVALID")
        binding = prepared.get("binding")
        if (prepared.get("stage_name") != expected_name
                or type(prepared.get("stage_device")) is not int
                or prepared["stage_device"] < 0
                or type(prepared.get("stage_inode")) is not int
                or prepared["stage_inode"] < 1
                or not isinstance(prepared.get("manifest_sha256"), str)
                or _DIGEST.fullmatch(prepared["manifest_sha256"]) is None
                or not isinstance(prepared.get("identity_sha256"), str)
                or _DIGEST.fullmatch(prepared["identity_sha256"]) is None
                or not isinstance(binding, dict)
                or set(binding) != {
                    "installation_id", "authority_epoch", "policy_digest",
                }
                or not isinstance(binding.get("installation_id"), str)
                or _IDENTIFIER.fullmatch(binding["installation_id"]) is None
                or binding.get("authority_epoch") != 1
                or not isinstance(binding.get("policy_digest"), str)
                or _DIGEST.fullmatch(binding["policy_digest"]) is None):
            _fail("AUTHORITY_PREPARATION_INVALID")
        return prepared

    def _verify_prepared_tree(self, path: Path, prepared: dict, *, published=False) -> dict:
        try:
            root_fd = _private_directory(path, create=False)
        except WorkspaceError as exc:
            _fail("AUTHORITY_PREPARATION_INVALID", exc)
        try:
            info = os.fstat(root_fd)
            if ((info.st_dev, info.st_ino)
                    != (prepared["stage_device"], prepared["stage_inode"])):
                _fail("AUTHORITY_PREPARATION_INVALID")
            manifest, identity, binding = self._initial_manifest(root_fd, published=published)
            if (not hmac.compare_digest(identity, prepared["identity_sha256"])
                    or binding != prepared["binding"]):
                _fail("AUTHORITY_PREPARATION_INVALID")
            if (not published
                    and not hmac.compare_digest(
                        manifest, prepared["manifest_sha256"],
                    )):
                _fail("AUTHORITY_PREPARATION_INVALID")
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                _fail("AUTHORITY_PREPARATION_INVALID")
            return binding
        finally:
            os.close(root_fd)

    def _prepared_descriptor(self, root_fd: int, stage_name: str,
                             transaction_id: str, policy_digest: str,
                             releases: dict[str, int]) -> dict:
        manifest, identity, binding = self._initial_manifest(root_fd)
        state = self._load(root_fd)
        if (state["authority_epoch"] != 1
                or state["policy_digest"] != policy_digest
                or state["allowed_releases"] != releases
                or state["requests"] or state["grants"]):
            _fail("AUTHORITY_PREPARATION_INVALID")
        info = os.fstat(root_fd)
        return {
            "version": 1,
            "transaction_id": transaction_id,
            "stage_name": stage_name,
            "stage_device": info.st_dev,
            "stage_inode": info.st_ino,
            "manifest_sha256": manifest,
            "identity_sha256": identity,
            "binding": binding,
        }

    def _quarantine_preparation(self, parent_fd: int, stage_name: str,
                                stage_fd: int) -> None:
        opened = os.fstat(stage_fd)
        try:
            current = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
        if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISDIR(current.st_mode)
                or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o700):
            _fail("AUTHORITY_PREPARATION_INVALID")
        quarantine = stage_name + ".quarantine-" + secrets.token_hex(16)
        try:
            _rename_noreplace(parent_fd, stage_name, parent_fd, quarantine)
            _fsync_directory(parent_fd)
            moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        except WorkspaceError:
            raise
        except OSError as exc:
            _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
        if (moved.st_dev, moved.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("AUTHORITY_PREPARATION_INVALID")

    def prepare_initial(self, policy_digest, allowed_releases, transaction_id):
        """Create and fsync a complete first authority without activating it."""
        policy_digest = _validate_digest(policy_digest)
        releases = _validate_releases(allowed_releases)
        stage_name = self._preparation_name(transaction_id)
        self._check_state_location()
        if self._enforcement_exists():
            _fail("AUTHORITY_ALREADY_INITIALIZED")

        parent_fd = _private_directory(self.state_dir.parent, create=True)
        stage_fd = None
        try:
            try:
                names = set(os.listdir(parent_fd))
            except OSError as exc:
                _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
            preparation_prefix = "." + self.state_dir.name + _PREPARATION_MARKER
            active_preparations = {
                name for name in names
                if (name.startswith(preparation_prefix)
                    and _IDENTIFIER.fullmatch(name[len(preparation_prefix):]) is not None)
            }
            if self.state_dir.name in names:
                _fail("AUTHORITY_ALREADY_INITIALIZED")
            if active_preparations - {stage_name}:
                _fail("AUTHORITY_PREPARATION_INVALID")
            if stage_name in active_preparations:
                stage_path = self.state_dir.parent / stage_name
                stage_fd = _private_directory(stage_path, create=False)
                try:
                    return self._prepared_descriptor(
                        stage_fd, stage_name, transaction_id, policy_digest, releases,
                    )
                except WorkspaceError:
                    try:
                        self._initial_manifest(stage_fd)
                    except WorkspaceError:
                        self._quarantine_preparation(parent_fd, stage_name, stage_fd)
                        os.close(stage_fd)
                        stage_fd = None
                    else:
                        raise
            try:
                os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
                stage_fd = os.open(
                    stage_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                os.fchmod(stage_fd, 0o700)
            except OSError as exc:
                _fail("AUTHORITY_STATE_UNAVAILABLE", exc)

            lock_fd = self._open_lock(stage_fd, create=True)
            try:
                try:
                    os.fsync(lock_fd)
                except OSError as exc:
                    _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
                now = time.time()
                installation_id = secrets.token_hex(16)
                state = {
                    "version": 1,
                    "installation_id": installation_id,
                    "authority_epoch": 1,
                    "policy_digest": policy_digest,
                    "allowed_releases": releases,
                    "requests": {},
                    "grants": {},
                    "updated_at": now,
                }
                _create_private_file(stage_fd, _CURSOR_KEY_NAME, secrets.token_bytes(32))
                self._save(stage_fd, state)
                _create_private_file(
                    stage_fd, _ACTIVATION_NAME, installation_id.encode("ascii"),
                )
                try:
                    os.mkdir(_LOCKS_NAME, 0o700, dir_fd=stage_fd)
                    locks_fd = os.open(
                        _LOCKS_NAME,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=stage_fd,
                    )
                    try:
                        os.fchmod(locks_fd, 0o700)
                        _fsync_directory(locks_fd)
                    finally:
                        os.close(locks_fd)
                    _fsync_directory(stage_fd)
                except OSError as exc:
                    _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
            finally:
                os.close(lock_fd)
            _fsync_directory(parent_fd)
            return self._prepared_descriptor(
                stage_fd, stage_name, transaction_id, policy_digest, releases,
            )
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            os.close(parent_fd)

    def commit_initial(self, prepared):
        """Irreversibly activate an exactly verified prepared first authority."""
        prepared = self._validate_prepared(prepared)
        self._check_state_location()
        parent_fd = _private_directory(self.state_dir.parent, create=False)
        try:
            names = set(os.listdir(parent_fd))
            stage_exists = prepared["stage_name"] in names
            final_exists = self.state_dir.name in names
            if stage_exists and final_exists:
                _fail("AUTHORITY_PREPARATION_INVALID")
            if not stage_exists and not final_exists:
                _fail("AUTHORITY_PREPARATION_MISSING")

            if final_exists:
                if not self._enforcement_exists():
                    _fail("AUTHORITY_PREPARATION_INVALID")
                binding = self._verify_prepared_tree(
                    self.state_dir, prepared, published=True,
                )
                self._ensure_minimum_abi(
                    parent_fd, self._allowed_releases_at(self.state_dir),
                    create=False,
                )
                _fsync_directory(parent_fd)
                return binding

            stage_path = self.state_dir.parent / prepared["stage_name"]
            binding = self._verify_prepared_tree(stage_path, prepared)
            enforced = self._enforcement_exists()
            self._ensure_minimum_abi(
                parent_fd, self._allowed_releases_at(stage_path),
                create=not enforced,
            )
            if not enforced:
                _create_private_file(
                    parent_fd, self.enforcement_path.name,
                    self._enforcement_payload(),
                )
                _fsync_directory(parent_fd)
            try:
                _rename_noreplace(
                    parent_fd, prepared["stage_name"],
                    parent_fd, self.state_dir.name,
                )
            except WorkspaceError:
                raise
            except OSError as exc:
                if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    _fail("AUTHORITY_PREPARATION_INVALID", exc)
                _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
            _fsync_directory(parent_fd)
            self._verify_prepared_tree(self.state_dir, prepared)
            return binding
        finally:
            os.close(parent_fd)

    def initialize(self, policy_digest, allowed_releases):
        policy_digest = _validate_digest(policy_digest)
        releases = _validate_releases(allowed_releases)
        self._check_state_location()
        if self._enforcement_exists():
            _fail("AUTHORITY_ALREADY_INITIALIZED")
        root_fd = self._root_fd(create=True)
        try:
            parent_fd = _private_directory(self.state_dir.parent, create=False)
            try:
                self._ensure_minimum_abi(parent_fd, releases, create=True)
                _create_private_file(
                    parent_fd, self.enforcement_path.name,
                    self._enforcement_payload(),
                )
                _fsync_directory(parent_fd)
            finally:
                os.close(parent_fd)
            lock_fd = self._open_lock(root_fd, create=True)
            try:
                names = set(os.listdir(root_fd))
                if names - {_LOCK_NAME}:
                    _fail("AUTHORITY_ALREADY_INITIALIZED")
                now = time.time()
                installation_id = secrets.token_hex(16)
                state = {
                    "version": 1,
                    "installation_id": installation_id,
                    "authority_epoch": 1,
                    "policy_digest": policy_digest,
                    "allowed_releases": releases,
                    "requests": {},
                    "grants": {},
                    "updated_at": now,
                }
                _create_private_file(root_fd, _CURSOR_KEY_NAME, secrets.token_bytes(32))
                self._save(root_fd, state)
                _create_private_file(root_fd, _ACTIVATION_NAME, installation_id.encode("ascii"))
                try:
                    os.mkdir(_LOCKS_NAME, 0o700, dir_fd=root_fd)
                    locks_fd = os.open(
                        _LOCKS_NAME,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=root_fd,
                    )
                    try:
                        os.fchmod(locks_fd, 0o700)
                        _fsync_directory(locks_fd)
                    finally:
                        os.close(locks_fd)
                    _fsync_directory(root_fd)
                except OSError as exc:
                    _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
                return self._binding(state)
            finally:
                os.close(lock_fd)
        finally:
            os.close(root_fd)

    @contextmanager
    def _locked_state(self, *, shared=False):
        if (not shared and (getattr(self._local, "admission_depth", 0)
                            or getattr(self._local, "project_lock_depth", 0))):
            _fail("AUTHORITY_LOCK_ORDER")
        try:
            root_fd = self._root_fd()
        except WorkspaceError as exc:
            cause = exc.__cause__
            if isinstance(cause, FileNotFoundError):
                _fail("AUTHORITY_STATE_MISSING" if self._enforcement_exists()
                      else "AUTHORITY_UNINITIALIZED")
            raise
        try:
            lock_fd = self._open_lock(root_fd, shared=shared)
            try:
                yield root_fd, self._load(root_fd)
            finally:
                os.close(lock_fd)
        finally:
            os.close(root_fd)

    def binding(self, policy_digest):
        with self._locked_state(shared=True) as (_, state):
            self._bound(state, policy_digest)
            return self._binding(state)

    def _project(self, project) -> Path:
        if not isinstance(project, str) or project not in self.projects:
            _fail("PROJECT_UNKNOWN")
        return self.projects[project]

    def _scope(self, path, operations) -> tuple[Path, str]:
        if not isinstance(path, str) or not Path(path).is_absolute():
            _fail("INVALID_GRANT_PATH")
        scope = Path(os.path.abspath(path))
        state = Path(os.path.realpath(self.state_dir))
        resolved = Path(os.path.realpath(scope))
        if _is_within(resolved, state) or _is_within(state, resolved):
            _fail("PROTECTED_AUTHORITY_PATH")
        _no_symlink_ancestors(scope.parent)
        try:
            info = scope.lstat()
        except FileNotFoundError:
            if "write" not in operations:
                _fail("GRANT_PATH_MISSING")
            try:
                parent = scope.parent.lstat()
            except OSError as exc:
                _fail("INVALID_GRANT_PATH", exc)
            if not stat.S_ISDIR(parent.st_mode):
                _fail("INVALID_GRANT_PATH")
            return scope, "file"
        except OSError as exc:
            _fail("INVALID_GRANT_PATH", exc)
        if stat.S_ISLNK(info.st_mode):
            _fail("INVALID_GRANT_PATH")
        if stat.S_ISDIR(info.st_mode):
            return scope, "directory"
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            return scope, "file"
        _fail("INVALID_GRANT_PATH")

    @staticmethod
    def _operations(operations) -> list[str]:
        if (not isinstance(operations, (list, tuple)) or not operations
                or any(not isinstance(value, str) or value not in _OPERATIONS
                       for value in operations)
                or len(set(operations)) != len(operations)):
            _fail("INVALID_GRANT_OPERATIONS")
        return list(operations)

    @staticmethod
    def _duration(duration_seconds) -> int:
        if duration_seconds is None:
            return _DEFAULT_DURATION_SECONDS
        if (type(duration_seconds) is not int or duration_seconds < 1
                or duration_seconds > _MAX_DURATION_SECONDS):
            _fail("INVALID_GRANT_DURATION")
        return duration_seconds

    @staticmethod
    def _public_request(record: dict) -> dict:
        return {
            "request_id": record["request_id"],
            "project": record["project"],
            "path": record["path"],
            "scope_kind": record["scope_kind"],
            "operations": list(record["operations"]),
            "duration_seconds": record["duration_seconds"],
            "purpose": record["purpose"],
            "requested_at": _iso(record["requested_at"]),
            "request_expires_at": _iso(record["request_expires_at"]),
            "authority_epoch": record["authority_epoch"],
            "policy_digest": record["policy_digest"],
            "status": record["status"],
        }

    @staticmethod
    def _public_grant(record: dict, *, path=None, operation=None) -> dict:
        result = {
            "grant_id": record["grant_id"],
            "request_id": record["request_id"],
            "project": record["project"],
            "scope_path": record["path"],
            "scope_kind": record["scope_kind"],
            "operations": list(record["operations"]),
            "issued_at": _iso(record["issued_at"]),
            "expires_at": _iso(record["expires_at"]),
            "authority_epoch": record["authority_epoch"],
            "policy_digest": record["policy_digest"],
            "status": record["status"],
        }
        if path is not None:
            result["path"] = path
        if operation is not None:
            result["operation"] = operation
        return result

    def request(self, project, path, operations, duration_seconds, purpose, policy_digest):
        self._project(project)
        operations = self._operations(operations)
        duration = self._duration(duration_seconds)
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 500:
            _fail("INVALID_GRANT_PURPOSE")
        scope, kind = self._scope(path, operations)
        if kind == "file" and "list" in operations:
            _fail("INVALID_GRANT_OPERATIONS")
        now = time.time()
        token = secrets.token_hex(32)
        request_id = secrets.token_hex(16)
        with self._locked_state() as (root_fd, state):
            self._bound(state, policy_digest)
            record = {
                "request_id": request_id,
                "project": project,
                "path": str(scope),
                "scope_kind": kind,
                "operations": operations,
                "duration_seconds": duration,
                "purpose": purpose.strip(),
                "requested_at": now,
                "request_expires_at": now + _REQUEST_TTL_SECONDS,
                "authority_epoch": state["authority_epoch"],
                "policy_digest": state["policy_digest"],
                "confirmation_digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "status": "pending",
            }
            state["requests"][request_id] = record
            state["updated_at"] = now
            self._save(root_fd, state)
        result = self._public_request(record)
        result.pop("status")
        result["_confirmation"] = token
        return result

    def _request_for_action(self, state, project, request_id) -> dict:
        if not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None:
            _fail("REQUEST_UNKNOWN")
        record = state["requests"].get(request_id)
        if not isinstance(record, dict):
            _fail("REQUEST_UNKNOWN")
        if record.get("project") != project:
            _fail("REQUEST_PROJECT_MISMATCH")
        status = record.get("status")
        if status == "tombstoned":
            _fail("REQUEST_TOMBSTONED")
        return record

    @staticmethod
    def _check_confirmation(record, token) -> None:
        if not isinstance(token, str) or len(token) != 64:
            _fail("CONFIRMATION_INVALID")
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expected = record.get("confirmation_digest", "")
        if not hmac.compare_digest(candidate, expected):
            _fail("CONFIRMATION_INVALID")

    def confirm(self, project, request_id, confirmation_token, policy_digest):
        self._project(project)
        now = time.time()
        with self._locked_state() as (root_fd, state):
            self._bound(state, policy_digest)
            request = self._request_for_action(state, project, request_id)
            if request["status"] == "cancelled":
                _fail("REQUEST_CANCELLED")
            if request["status"] != "pending":
                _fail("CONFIRMATION_REPLAYED")
            self._check_confirmation(request, confirmation_token)
            if request["request_expires_at"] <= now:
                request["status"] = "expired"
                request.pop("confirmation_digest", None)
                state["updated_at"] = now
                self._save(root_fd, state)
                _fail("REQUEST_EXPIRED")
            grant_id = secrets.token_hex(16)
            grant = {
                "grant_id": grant_id,
                "request_id": request_id,
                "project": project,
                "path": request["path"],
                "scope_kind": request["scope_kind"],
                "operations": list(request["operations"]),
                "issued_at": now,
                "expires_at": now + request["duration_seconds"],
                "authority_epoch": state["authority_epoch"],
                "policy_digest": state["policy_digest"],
                "status": "active",
            }
            request["status"] = "confirmed"
            request["grant_id"] = grant_id
            request.pop("confirmation_digest", None)
            state["grants"][grant_id] = grant
            state["updated_at"] = now
            self._save(root_fd, state)
            return self._public_grant(grant)

    def cancel(self, project, request_id, confirmation_token, policy_digest):
        self._project(project)
        now = time.time()
        with self._locked_state() as (root_fd, state):
            self._bound(state, policy_digest)
            request = self._request_for_action(state, project, request_id)
            if request["status"] != "pending":
                _fail("CONFIRMATION_REPLAYED")
            self._check_confirmation(request, confirmation_token)
            request["status"] = "cancelled"
            request["cancelled_at"] = now
            request.pop("confirmation_digest", None)
            state["updated_at"] = now
            self._save(root_fd, state)
            return self._public_request(request)

    def get(self, project, request_id=None):
        self._project(project)
        with self._locked_state(shared=True) as (_, state):
            if request_id is not None:
                record = state["requests"].get(request_id)
                if not isinstance(record, dict):
                    _fail("REQUEST_UNKNOWN")
                if record.get("project") != project:
                    _fail("REQUEST_PROJECT_MISMATCH")
                return self._public_request(record)
            requests = [
                self._public_request(record)
                for record in state["requests"].values()
                if isinstance(record, dict) and record.get("project") == project
            ]
            grants = [
                self._public_grant(record)
                for record in state["grants"].values()
                if isinstance(record, dict) and record.get("project") == project
            ]
            return {"binding": self._binding(state), "requests": requests, "grants": grants}

    def require(self, project, path, operation, grant_id, policy_digest):
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            _fail("INVALID_GRANT_OPERATION")
        if not isinstance(path, str) or not Path(path).is_absolute():
            _fail("INVALID_GRANT_PATH")
        requested = Path(os.path.abspath(path))
        resolved_requested = Path(os.path.realpath(requested))
        now = time.time()
        with self._locked_state(shared=True) as (_, state):
            self._bound(state, policy_digest)
            if not isinstance(grant_id, str) or _IDENTIFIER.fullmatch(grant_id) is None:
                _fail("GRANT_UNKNOWN")
            grant = state["grants"].get(grant_id)
            if not isinstance(grant, dict):
                _fail("GRANT_UNKNOWN")
            if grant.get("project") != project:
                _fail("GRANT_PROJECT_MISMATCH")
            if grant.get("status") == "revoked":
                _fail("GRANT_REVOKED")
            if grant.get("status") == "tombstoned":
                _fail("GRANT_TOMBSTONED")
            if grant.get("status") != "active":
                _fail("GRANT_INVALID")
            if (grant.get("authority_epoch") != state["authority_epoch"]
                    or grant.get("policy_digest") != state["policy_digest"]):
                _fail("GRANT_TOMBSTONED")
            if grant.get("expires_at", 0) <= now:
                _fail("GRANT_EXPIRED")
            if operation not in grant.get("operations", []):
                _fail("GRANT_OPERATION_DENIED")
            scope = Path(grant["path"])
            if grant["scope_kind"] == "file":
                in_scope = resolved_requested == scope
            else:
                in_scope = _is_within(resolved_requested, scope)
            if not in_scope:
                _fail("GRANT_SCOPE_MISMATCH")
            return self._public_grant(
                grant, path=str(requested), operation=operation,
            )

    def revoke(self, project, grant_id, policy_digest):
        self._project(project)
        now = time.time()
        with self._locked_state() as (root_fd, state):
            self._bound(state, policy_digest)
            grant = state["grants"].get(grant_id)
            if not isinstance(grant, dict):
                _fail("GRANT_UNKNOWN")
            if grant.get("project") != project:
                _fail("GRANT_PROJECT_MISMATCH")
            if grant.get("status") == "revoked":
                _fail("GRANT_REVOKED")
            if grant.get("status") != "active":
                _fail("GRANT_INVALID")
            grant["status"] = "revoked"
            grant["revoked_at"] = now
            state["updated_at"] = now
            self._save(root_fd, state)
            return self._public_grant(grant)

    def advance(self, policy_digest, allowed_releases=None):
        policy_digest = _validate_digest(policy_digest)
        releases = None if allowed_releases is None else _validate_releases(allowed_releases)
        now = time.time()
        with self._locked_state() as (root_fd, state):
            candidate_releases = (
                state["allowed_releases"] if releases is None else releases)
            parent_fd = _private_directory(self.state_dir.parent, create=False)
            try:
                self._ensure_minimum_abi(
                    parent_fd, candidate_releases, create=False)
            finally:
                os.close(parent_fd)
            state["authority_epoch"] += 1
            state["policy_digest"] = policy_digest
            if releases is not None:
                state["allowed_releases"] = releases
            for request in state["requests"].values():
                if isinstance(request, dict) and request.get("status") == "pending":
                    request["status"] = "tombstoned"
                    request["tombstoned_at"] = now
                    request.pop("confirmation_digest", None)
            for grant in state["grants"].values():
                if isinstance(grant, dict) and grant.get("status") == "active":
                    grant["status"] = "tombstoned"
                    grant["tombstoned_at"] = now
            state["updated_at"] = now
            self._save(root_fd, state)
            return self._binding(state)

    def cursor_key(self, policy_digest):
        with self._locked_state(shared=True) as (root_fd, state):
            self._bound(state, policy_digest)
            return _read_private_bytes(root_fd, _CURSOR_KEY_NAME, 64)

    @contextmanager
    def admission(self, policy_digest):
        """Hold one stable binding; nest only inside ``project_lock`` for writes."""
        with self._locked_state(shared=True) as (_, state):
            self._bound(state, policy_digest)
            self._local.admission_depth = getattr(self._local, "admission_depth", 0) + 1
            try:
                yield self._binding(state)
            finally:
                self._local.admission_depth -= 1

    @contextmanager
    def project_lock(self, project):
        """Serialize cooperating installation writers; acquire before admission.

        Projects may share an external grant destination, so their writers use
        one lock domain. Project identity still gates every operation.
        A writer lock is deliberately non-reentrant. Authority mutations such
        as request/confirm/advance must run outside both public lock contexts.
        """
        self._project(project)
        if getattr(self._local, "project_lock_depth", 0):
            _fail("AUTHORITY_LOCK_REENTRANT")
        if getattr(self._local, "admission_depth", 0):
            _fail("AUTHORITY_LOCK_ORDER")
        root_fd = self._root_fd()
        try:
            global_fd = self._open_lock(root_fd, shared=True)
            try:
                self._load(root_fd)
                try:
                    locks_fd = os.open(
                        _LOCKS_NAME,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=root_fd,
                    )
                    info = os.fstat(locks_fd)
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o700):
                        _fail("UNSAFE_AUTHORITY_STATE")
                except OSError as exc:
                    _fail("AUTHORITY_STATE_UNAVAILABLE", exc)
                try:
                    name = "workspace-writers.lock"
                    lock_fd = self._open_lock(locks_fd, name, create=True)
                    try:
                        self._local.project_lock_depth = 1
                        try:
                            yield
                        finally:
                            self._local.project_lock_depth = 0
                    finally:
                        os.close(lock_fd)
                finally:
                    os.close(locks_fd)
            finally:
                os.close(global_fd)
        finally:
            os.close(root_fd)
