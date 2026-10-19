"""Build and validate the static agent environment/bootstrap boundary."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
import stat
import struct
import subprocess
import tempfile


MAX_BOOTSTRAP_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024
COMPILER = "/usr/bin/cc"


class BootstrapError(RuntimeError):
    """The bootstrap source, destination, compiler, or ELF was unsafe."""


def _owned_regular_bytes(path: Path, *, expected_mode=None, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BootstrapError("BOOTSTRAP_FILE_UNAVAILABLE") from exc
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
            or before.st_uid != os.getuid() or before.st_size < 1
            or before.st_size > maximum
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)):
        raise BootstrapError("BOOTSTRAP_FILE_UNSAFE")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BootstrapError("BOOTSTRAP_FILE_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()):
            raise BootstrapError("BOOTSTRAP_FILE_CHANGED")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise BootstrapError("BOOTSTRAP_FILE_UNSAFE")
        after = os.fstat(descriptor)
        if (size != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns):
            raise BootstrapError("BOOTSTRAP_FILE_CHANGED")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _assert_static_elf(payload: bytes) -> None:
    if len(payload) < 64 or payload[:4] != b"\x7fELF" or payload[6] != 1:
        raise BootstrapError("BOOTSTRAP_NOT_ELF")
    elf_class, encoding = payload[4], payload[5]
    if encoding == 1:
        endian = "<"
    elif encoding == 2:
        endian = ">"
    else:
        raise BootstrapError("BOOTSTRAP_NOT_ELF")
    if elf_class == 2:
        if len(payload) < 64:
            raise BootstrapError("BOOTSTRAP_NOT_ELF")
        file_type = struct.unpack_from(endian + "H", payload, 16)[0]
        program_offset = struct.unpack_from(endian + "Q", payload, 32)[0]
        entry_size = struct.unpack_from(endian + "H", payload, 54)[0]
        entry_count = struct.unpack_from(endian + "H", payload, 56)[0]
    elif elf_class == 1:
        if len(payload) < 52:
            raise BootstrapError("BOOTSTRAP_NOT_ELF")
        file_type = struct.unpack_from(endian + "H", payload, 16)[0]
        program_offset = struct.unpack_from(endian + "I", payload, 28)[0]
        entry_size = struct.unpack_from(endian + "H", payload, 42)[0]
        entry_count = struct.unpack_from(endian + "H", payload, 44)[0]
    else:
        raise BootstrapError("BOOTSTRAP_NOT_ELF")
    if file_type not in (2, 3) or entry_count in (0, 0xFFFF) or entry_size < 4:
        raise BootstrapError("BOOTSTRAP_NOT_ELF")
    end = program_offset + entry_size * entry_count
    if program_offset < 1 or end > len(payload):
        raise BootstrapError("BOOTSTRAP_NOT_ELF")
    for index in range(entry_count):
        program_type = struct.unpack_from(endian + "I", payload,
                                          program_offset + index * entry_size)[0]
        if program_type == 3:  # PT_INTERP means the loader can consume inherited variables.
            raise BootstrapError("BOOTSTRAP_NOT_STATIC")


def validate_bootstrap(path: Path) -> str:
    """Validate ownership, mode and static ELF structure; return its SHA-256."""
    payload = _owned_regular_bytes(Path(path), expected_mode=0o700,
                                   maximum=MAX_BOOTSTRAP_BYTES)
    _assert_static_elf(payload)
    return hashlib.sha256(payload).hexdigest()


def _private_destination(destination: Path) -> Path:
    destination = Path(destination)
    if not destination.is_absolute() or destination.name in ("", ".", ".."):
        raise BootstrapError("BOOTSTRAP_DESTINATION_UNSAFE")
    parent = destination.parent
    try:
        parent_info = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError("BOOTSTRAP_DESTINATION_UNSAFE") from exc
    if (not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink()
            or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700
            or resolved != parent.absolute()):
        raise BootstrapError("BOOTSTRAP_DESTINATION_UNSAFE")
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise BootstrapError("BOOTSTRAP_DESTINATION_UNSAFE") from exc
    raise BootstrapError("BOOTSTRAP_DESTINATION_EXISTS")


def _compile(source: Path, output: Path, cwd: Path) -> None:
    argv = [COMPILER, "-static", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-o", str(output), str(source)]
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise BootstrapError("BOOTSTRAP_COMPILER_UNAVAILABLE") from exc
    try:
        _, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, stderr = process.communicate(timeout=5)
        (cwd / 'compile-stderr.log').write_bytes(stderr[-65536:])
        os.chmod(cwd / 'compile-stderr.log', 0o600)
        raise BootstrapError("BOOTSTRAP_COMPILE_TIMEOUT") from None
    if process.returncode != 0:
        (cwd / 'compile-stderr.log').write_bytes(stderr[-65536:])
        os.chmod(cwd / 'compile-stderr.log', 0o600)
        raise BootstrapError("BOOTSTRAP_COMPILE_FAILED")


def build_bootstrap(destination: Path, source: Path | None = None) -> dict:
    """Compile, validate and install a new static bootstrap without overwriting."""
    destination = _private_destination(Path(destination))
    source = Path(source) if source is not None else Path(__file__).with_name("agent-bootstrap.c")
    source_payload = _owned_regular_bytes(source, maximum=MAX_SOURCE_BYTES)
    source_sha256 = hashlib.sha256(source_payload).hexdigest()
    temporary = Path(tempfile.mkdtemp(prefix=".agent-bootstrap-", dir=destination.parent))
    os.chmod(temporary, 0o700)
    output = temporary / "agent-bootstrap"
    try:
        compiled_source = temporary / "agent-bootstrap.c"
        descriptor = os.open(
            compiled_source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source_payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        _compile(compiled_source, output, temporary)
        os.chmod(output, 0o700)
        digest = validate_bootstrap(output)
        try:
            os.link(output, destination, follow_symlinks=False)
        except OSError as exc:
            raise BootstrapError("BOOTSTRAP_INSTALL_FAILED") from exc
        try:
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            try:
                destination.unlink()
            except OSError:
                pass
            raise BootstrapError("BOOTSTRAP_INSTALL_FAILED") from exc
        return {"path": str(destination), "sha256": digest,
                "source_sha256": source_sha256}
    except BootstrapError:
        diagnostic = temporary / 'compile-stderr.log'
        if diagnostic.is_file():
            fd, retained = tempfile.mkstemp(prefix='.agent-bootstrap-error-', suffix='.log',
                                           dir=destination.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(diagnostic.read_bytes()[-65536:])
                os.fchmod(stream.fileno(), 0o600)
        raise
    finally:
        shutil.rmtree(temporary)
