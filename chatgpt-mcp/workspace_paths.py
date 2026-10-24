"""Race-resistant file access primitives for registered workspaces."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat
from typing import Iterable, Iterator


MAX_FILE_BYTES = 1024 * 1024
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_uid", "st_mode", "st_nlink", "st_size",
    "st_mtime_ns", "st_ctime_ns",
)
_SENSITIVE_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".npmrc",
    ".pypirc", "credentials",
})


class WorkspaceError(Exception):
    """A bounded workspace denial safe to expose through protocol adapters."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileData:
    data: bytes
    sha256: str
    size: int
    mode: int
    identity: tuple[int, ...]


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(info, field) for field in _IDENTITY_FIELDS)


def _absolute_path(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate) != os.path.normpath(str(candidate)):
        raise WorkspaceError("INVALID_PATH")
    return candidate


def _relative_parts(value: str) -> tuple[str, ...]:
    if (not isinstance(value, str) or not 0 < len(value) <= 4096
            or not value.isprintable() or "\\" in value or value.startswith("/")
            or value.endswith("/")):
        raise WorkspaceError("INVALID_PATH")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise WorkspaceError("INVALID_PATH")
    return parts


def is_sensitive(relative_path: str) -> bool:
    """Return whether a syntactically relative path names protected material."""
    try:
        parts = _relative_parts(relative_path)
    except WorkspaceError:
        return False
    return any(part in _SENSITIVE_PARTS or part == ".env" or part.startswith(".env.")
               for part in parts)


@contextmanager
def _workspace_parent(root: Path, relative_path: str) -> Iterator[tuple[int, str]]:
    parts = _relative_parts(relative_path)
    if is_sensitive(relative_path):
        raise WorkspaceError("SENSITIVE_PATH")
    root = _absolute_path(Path(root))
    descriptors: list[int] = []
    try:
        current = open_directory(root)
        descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, _OPEN_DIRECTORY, dir_fd=current)
            descriptors.append(current)
        yield current, parts[-1]
    except WorkspaceError:
        raise
    except OSError as exc:
        raise WorkspaceError("FILE_NOT_FOUND" if exc.errno == 2 else "UNSAFE_PATH") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular_fd(fd: int, max_bytes: int, unsafe_code: str) -> FileData:
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_FILE_BYTES:
        raise WorkspaceError("INVALID_LIMIT")
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_nlink != 1):
        raise WorkspaceError(unsafe_code)
    if before.st_size > max_bytes:
        raise WorkspaceError("FILE_TOO_LARGE")
    chunks: list[bytes] = []
    amount = 0
    while amount <= max_bytes:
        block = os.read(fd, min(65536, max_bytes + 1 - amount))
        if not block:
            break
        chunks.append(block)
        amount += len(block)
    after = os.fstat(fd)
    data = b"".join(chunks)
    if amount > max_bytes:
        raise WorkspaceError("FILE_TOO_LARGE")
    if _identity(after) != _identity(before) or amount != after.st_size:
        raise WorkspaceError("STALE_FILE")
    return FileData(data, hashlib.sha256(data).hexdigest(), amount,
                    stat.S_IMODE(after.st_mode), _identity(after))


def read_file(root: Path, relative_path: str, *, max_bytes: int = MAX_FILE_BYTES) -> FileData:
    """Read one regular file through a chain of anchored directory fds."""
    with _workspace_parent(root, relative_path) as (parent, leaf):
        try:
            descriptor = os.open(leaf, _OPEN_FILE, dir_fd=parent)
        except OSError as exc:
            raise WorkspaceError("FILE_NOT_FOUND" if exc.errno == 2 else "UNSAFE_PATH") from None
        try:
            return _read_regular_fd(descriptor, max_bytes, "UNSAFE_FILE")
        finally:
            os.close(descriptor)


def _safe_existing_mode(info: os.stat_result) -> int:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWOTH | stat.S_ISUID
                               | stat.S_ISGID | stat.S_ISVTX)):
        raise WorkspaceError("UNSAFE_FILE")
    return stat.S_IMODE(info.st_mode)


def _write_temporary(parent: int, data: bytes, mode: int) -> str:
    name = ".workspace-" + secrets.token_hex(16) + ".tmp"
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WorkspaceError("WRITE_FAILED")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(name, dir_fd=parent)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    return name


def _write_result(parent: int, leaf: str, data: bytes) -> dict:
    descriptor = os.open(leaf, _OPEN_FILE, dir_fd=parent)
    try:
        result = _read_regular_fd(descriptor, MAX_FILE_BYTES, "UNSAFE_FILE")
    finally:
        os.close(descriptor)
    if result.data != data:
        raise WorkspaceError("WRITE_FAILED")
    return {"sha256": result.sha256, "size": result.size, "mode": result.mode,
            "identity": result.identity}


def write_file(root: Path, relative_path: str, data: bytes, *,
               expected_sha256: str | None = None, create: bool = False) -> dict:
    """Create or CAS-replace one file without leaving its anchored parent."""
    if not isinstance(data, bytes):
        raise WorkspaceError("INVALID_DATA")
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceError("FILE_TOO_LARGE")
    if type(create) is not bool or (create and expected_sha256 is not None):
        raise WorkspaceError("INVALID_WRITE_MODE")
    if not create and expected_sha256 is None:
        raise WorkspaceError("CAS_REQUIRED")
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)):
        raise WorkspaceError("INVALID_DIGEST")

    with _workspace_parent(root, relative_path) as (parent, leaf):
        if create:
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise WorkspaceError("UNSAFE_FILE") from None
            else:
                raise WorkspaceError("TARGET_EXISTS")
            temporary = _write_temporary(parent, data, 0o600)
            try:
                try:
                    os.link(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent,
                            follow_symlinks=False)
                except FileExistsError:
                    raise WorkspaceError("TARGET_EXISTS") from None
                os.unlink(temporary, dir_fd=parent)
                temporary = ""
                os.fsync(parent)
            finally:
                if temporary:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except OSError:
                        pass
            return _write_result(parent, leaf, data)

        try:
            existing = os.open(leaf, _OPEN_FILE, dir_fd=parent)
        except FileNotFoundError:
            raise WorkspaceError("FILE_NOT_FOUND") from None
        except OSError:
            raise WorkspaceError("UNSAFE_FILE") from None
        try:
            before = os.fstat(existing)
            mode = _safe_existing_mode(before)
            current = _read_regular_fd(existing, MAX_FILE_BYTES, "UNSAFE_FILE")
        finally:
            os.close(existing)
        if current.sha256 != expected_sha256:
            raise WorkspaceError("STALE_FILE")

        temporary = _write_temporary(parent, data, mode)
        try:
            try:
                observed = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise WorkspaceError("STALE_FILE") from None
            if _identity(observed) != _identity(before):
                raise WorkspaceError("STALE_FILE")
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = ""
            os.fsync(parent)
        finally:
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
        return _write_result(parent, leaf, data)


def _open_absolute_directory(path: Path) -> tuple[int, list[int]]:
    path = _absolute_path(path)
    descriptors: list[int] = []
    try:
        current = os.open("/", _OPEN_DIRECTORY)
        descriptors.append(current)
        for index, component in enumerate(path.parts[1:], start=1):
            current = os.open(component, _OPEN_DIRECTORY, dir_fd=current)
            descriptors.append(current)
            info = os.fstat(current)
            mode = stat.S_IMODE(info.st_mode)
            if index == len(path.parts) - 1:
                if info.st_uid != os.getuid() or mode != 0o700:
                    raise WorkspaceError("UNSAFE_OWNER_DIRECTORY")
            elif info.st_uid not in (0, os.getuid()):
                raise WorkspaceError("UNSAFE_OWNER_DIRECTORY")
            elif mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
                    info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise WorkspaceError("UNSAFE_OWNER_DIRECTORY")
        return current, descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def open_directory(path: Path) -> int:
    """Open a trusted absolute directory through no-follow directory fds."""
    path = _absolute_path(Path(path))
    current = None
    try:
        current = os.open("/", _OPEN_DIRECTORY)
        if path == Path("/"):
            return current
        for component in path.parts[1:]:
            child = os.open(component, _OPEN_DIRECTORY, dir_fd=current)
            info = os.fstat(child)
            mode = stat.S_IMODE(info.st_mode)
            if (info.st_uid not in (0, os.getuid())
                    or (info.st_uid == 0 and mode & (stat.S_IWGRP | stat.S_IWOTH)
                        and not info.st_mode & stat.S_ISVTX)):
                os.close(child)
                raise WorkspaceError("UNSAFE_PATH")
            os.close(current)
            current = child
        return current
    except WorkspaceError:
        if current is not None:
            os.close(current)
        raise
    except OSError:
        if current is not None:
            os.close(current)
        raise WorkspaceError("UNSAFE_PATH") from None


def validate_private_directory(path: Path) -> Path:
    """Validate an owner-only 0700 directory and its no-symlink ancestry."""
    descriptors: list[int] = []
    try:
        _, descriptors = _open_absolute_directory(Path(path))
    except WorkspaceError:
        raise
    except OSError:
        raise WorkspaceError("UNSAFE_OWNER_DIRECTORY") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return Path(path)


def _inside(candidate: Path, root: Path) -> bool:
    return candidate == root or candidate.is_relative_to(root)


def read_owner_file(path: Path, *, max_bytes: int = MAX_FILE_BYTES,
                    forbidden_roots: Iterable[Path] = ()) -> FileData:
    """Read a 0600, single-link owner file outside all forbidden roots."""
    path = _absolute_path(Path(path))
    for root in forbidden_roots:
        if _inside(path, _absolute_path(Path(root))):
            raise WorkspaceError("UNSAFE_OWNER_LOCATION")
    descriptors: list[int] = []
    try:
        parent, descriptors = _open_absolute_directory(path.parent)
        try:
            descriptor = os.open(path.name, _OPEN_FILE, dir_fd=parent)
        except FileNotFoundError:
            raise WorkspaceError("OWNER_FILE_MISSING") from None
        except OSError:
            raise WorkspaceError("UNSAFE_OWNER_FILE") from None
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise WorkspaceError("UNSAFE_OWNER_FILE")
            return _read_regular_fd(descriptor, max_bytes, "UNSAFE_OWNER_FILE")
        finally:
            os.close(descriptor)
    except WorkspaceError:
        raise
    except FileNotFoundError:
        raise WorkspaceError("OWNER_FILE_MISSING") from None
    except OSError:
        raise WorkspaceError("UNSAFE_OWNER_DIRECTORY") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
