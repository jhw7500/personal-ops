#!/usr/bin/python3 -I
"""Pin and execute one owner-approved host command without reopening its path."""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import sys

CONTROL_ERROR = 125
EXEC_ERROR = 126
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_BYTES = 512 * 1024 * 1024
MAX_ARGV = 32
MAX_ARGUMENT_BYTES = 128 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
SAFE_ENV_KEYS = frozenset({
    "HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE",
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
})
_METADATA_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
    "st_size", "st_mtime_ns", "st_ctime_ns",
)


class EntryError(Exception):
    """Sanitized validation failure with no host path or secret text."""


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(info, field) for field in _METADATA_FIELDS)


def _validate_text(value: str, limit: int) -> None:
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or not value.isprintable() or "\0" in value):
        raise EntryError()


def _absolute_parts(path: Path) -> tuple[str, ...]:
    text = str(path)
    _validate_text(text, 1024)
    if not path.is_absolute() or text != os.path.normpath(text):
        raise EntryError()
    parts = path.parts
    if not parts or parts[0] != "/" or any(part in ("", ".", "..") for part in parts[1:]):
        raise EntryError()
    return parts[1:]


def _open_absolute(path: Path, *, directory: bool = False) -> int:
    parts = _absolute_parts(path)
    if not parts:
        if not directory:
            raise EntryError()
        try:
            return os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise EntryError() from None
    current = -1
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        for component in parts[:-1]:
            following = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = following
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if directory:
            flags |= os.O_DIRECTORY
        result = os.open(parts[-1], flags, dir_fd=current)
        os.close(current)
        return result
    except OSError:
        if current >= 0:
            os.close(current)
        raise EntryError() from None


def _safe_file(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            or not info.st_mode & stat.S_IXUSR
            or not 0 <= info.st_size <= MAX_FILE_BYTES):
        raise EntryError()


def _fd_path(fd: int) -> Path:
    try:
        value = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise EntryError() from None
    if not value.startswith("/") or value.endswith(" (deleted)"):
        raise EntryError()
    result = Path(value)
    if str(result) != os.path.normpath(value):
        raise EntryError()
    return result


def _read_verified_file(path: Path, expected_sha256: str,
                        forbidden_root: Path | None = None) -> bytes:
    if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
        raise EntryError()
    fd = _open_absolute(Path(path))
    try:
        before = os.fstat(fd)
        _safe_file(before)
        opened_path = _fd_path(fd)
        if forbidden_root is not None and opened_path.is_relative_to(forbidden_root):
            raise EntryError()
        content = bytearray()
        while len(content) <= MAX_FILE_BYTES:
            try:
                block = os.read(fd, min(1 << 20, MAX_FILE_BYTES + 1 - len(content)))
            except InterruptedError:
                continue
            if not block:
                break
            content.extend(block)
        if len(content) > MAX_FILE_BYTES:
            raise EntryError()
        after = os.fstat(fd)
        _safe_file(after)
        if (_metadata(before) != _metadata(after) or len(content) != after.st_size
                or _fd_path(fd) != opened_path):
            raise EntryError()
        if not secrets.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256):
            raise EntryError()
        return bytes(content)
    except OSError:
        raise EntryError() from None
    finally:
        os.close(fd)


def _memfd_create() -> int:
    flags = getattr(os, "MFD_ALLOW_SEALING", 0x0002)
    if hasattr(os, "memfd_create"):
        try:
            return os.memfd_create("host-command", flags)
        except OSError:
            raise EntryError() from None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        create = libc.memfd_create
        create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        create.restype = ctypes.c_int
        fd = create(b"host-command", flags)
    except (AttributeError, OSError):
        raise EntryError() from None
    if fd < 0:
        raise EntryError()
    return fd


def _sealed_executable(path: Path, expected_sha256: str,
                       forbidden_root: Path | None = None) -> int:
    """Return a sealed executable memfd containing exactly the approved bytes."""
    content = _read_verified_file(Path(path), expected_sha256, forbidden_root)
    fd = _memfd_create()
    try:
        offset = 0
        while offset < len(content):
            try:
                written = os.write(fd, content[offset:])
            except InterruptedError:
                continue
            if written <= 0:
                raise EntryError()
            offset += written
        os.fchmod(fd, 0o700)
        os.lseek(fd, 0, os.SEEK_SET)
        seals = F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL
        fcntl.fcntl(fd, F_ADD_SEALS, seals)
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        return fd
    except (OSError, EntryError):
        os.close(fd)
        raise EntryError() from None


def open_verified_executable(path: Path, expected_sha256: str) -> int:
    """Return a sealed executable memfd containing exactly the approved bytes."""
    return _sealed_executable(Path(path), expected_sha256)


def open_runtime_executable(path: Path, expected_sha256: str, forbidden_root: Path) -> int:
    """Open a private staged native runtime, preserving sibling helper discovery.

    Only the host Codex path selects this mode. The staged binary is owner-only
    read/exec (0500) in an owner-private directory; its fd stabilizes file identity
    rather than creating a memfd. This is trusted same-UID automation, not a
    promise of integrity against a malicious owner changing modes/inodes.
    Fixed command profiles continue using the sealed executable path.
    """
    if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
        raise EntryError()
    fd = _open_absolute(Path(path))
    try:
        before = os.fstat(fd)
        opened_path = _fd_path(fd)
        parent = opened_path.parent.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o500
                or not 0 < before.st_size <= MAX_RUNTIME_BYTES
                or opened_path != path or opened_path.is_relative_to(forbidden_root)
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise EntryError()
        digest = hashlib.sha256()
        count = 0
        while block := os.read(fd, 1 << 20):
            count += len(block)
            if count > MAX_RUNTIME_BYTES:
                raise EntryError()
            digest.update(block)
        if (count != before.st_size or _metadata(os.fstat(fd)) != _metadata(before)
                or _fd_path(fd) != opened_path
                or not secrets.compare_digest(digest.hexdigest(), expected_sha256)):
            raise EntryError()
        os.lseek(fd, 0, os.SEEK_SET)
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
        return fd
    except (OSError, EntryError):
        os.close(fd)
        raise EntryError() from None


def _safe_directory(info: os.stat_result) -> None:
    # Registered checkouts are intentionally writable. Ownership plus pinned
    # O_NOFOLLOW directory fds prevents a pre-start path swap from redirecting
    # cwd, while the command retains its intended ability to modify the checkout.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
        raise EntryError()


def _open_project_cwd(project_root: Path, relative: str) -> tuple[int, int]:
    root_fd = _open_absolute(project_root, directory=True)
    current = -1
    try:
        _safe_directory(os.fstat(root_fd))
        if _fd_path(root_fd) != project_root:
            raise EntryError()
        if relative == ".":
            return root_fd, os.dup(root_fd)
        _validate_text(relative, 512)
        if relative.startswith("/") or "\\" in relative:
            raise EntryError()
        parts = relative.split("/")
        if any(part in ("", ".", "..") or part.startswith(".env")
               or part in {".git", ".ssh", ".aws", ".config", ".local"}
               for part in parts):
            raise EntryError()
        current = os.dup(root_fd)
        for component in parts:
            following = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = following
            _safe_directory(os.fstat(current))
        return root_fd, current
    except (OSError, EntryError):
        if current >= 0:
            os.close(current)
        os.close(root_fd)
        raise EntryError() from None


def _sanitized_environment() -> dict[str, str]:
    home = os.environ.get("HOME")
    if (not isinstance(home, str) or not home.startswith("/") or "\0" in home
            or len(home) > 4096):
        raise EntryError()
    env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    if any("\0" in key or "\0" in value or len(value) > 4096 for key, value in env.items()):
        raise EntryError()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _parse(argv: list[str]) -> tuple[str, Path, Path, str, list[str]]:
    if len(argv) < 6 or argv[5] != "--" or len(argv) - 6 > MAX_ARGV:
        raise EntryError()
    expected, executable, root, relative = argv[1:5]
    fixed = argv[6:]
    for value in (expected, executable, root, relative, *fixed):
        _validate_text(value, 1024)
    if sum(len(value) + 1 for value in argv[1:]) > MAX_ARGUMENT_BYTES:
        raise EntryError()
    executable_path = Path(executable)
    project_root = Path(root)
    _absolute_parts(executable_path)
    _absolute_parts(project_root)
    if executable_path.is_relative_to(project_root):
        raise EntryError()
    return expected, executable_path, project_root, relative, fixed


def main(argv: list[str]) -> int:
    executable_fd = root_fd = cwd_fd = -1
    try:
        runtime_layout = len(argv) > 1 and argv[1] == "--runtime-layout"
        if runtime_layout:
            argv = [argv[0], *argv[2:]]
        expected, executable, project_root, relative, fixed = _parse(argv)
        root_fd, cwd_fd = _open_project_cwd(project_root, relative)
        executable_fd = (open_runtime_executable(executable, expected, project_root)
                         if runtime_layout else _sealed_executable(executable, expected, project_root))
        environment = _sanitized_environment()
        os.fchdir(cwd_fd)
        os.close(cwd_fd)
        cwd_fd = -1
        os.close(root_fd)
        root_fd = -1
        try:
            os.execve(executable_fd, [str(executable), *fixed], environment)
        except OSError:
            os.write(2, b"HOST_COMMAND_ENTRY_EXEC_FAILED\n")
            return EXEC_ERROR
    except (EntryError, OSError, ValueError, TypeError):
        os.write(2, b"HOST_COMMAND_ENTRY_INVALID\n")
        return CONTROL_ERROR
    finally:
        for fd in (cwd_fd, root_fd, executable_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:
        os.write(2, b"HOST_COMMAND_ENTRY_INVALID\n")
        raise SystemExit(CONTROL_ERROR)
