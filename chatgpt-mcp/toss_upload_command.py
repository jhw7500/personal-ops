#!/usr/bin/python3 -I
"""Host-owned, fixed-artifact TEST upload using an audited official AIT CLI.

The host command policy pins this file and all arguments. No build, invitation,
review submission or live-release command is available. The official CLI and
its dependency closure live outside chat-writable repositories. Linux sealed
memfds keep the executable and the uploaded bytes stable across reopened paths.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import secrets
import stat
import struct
import subprocess
import tempfile
import time
import uuid

APP = "tarororo"
ENTRY = "node_modules/@apps-in-toss/cli/dist/index.js"
MAX_FILE_BYTES = 128 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)


class UploadError(Exception):
    """Only constant, non-sensitive codes may reach the command result."""


def read_secure(path: Path, *, limit=MAX_FILE_BYTES, private=False, executable=False):
    """Read a stable regular file without following any symlink component."""
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise UploadError("UNSAFE_PATH")
    fds = []
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(parent)
        for name in path.parts[1:-1]:
            parent = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
            fds.append(parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        fds.append(fd)
        prior = os.fstat(fd)
        if (not stat.S_ISREG(prior.st_mode) or prior.st_uid not in (0, os.getuid())
                or prior.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                or not 0 < prior.st_size <= limit
                or (private and (prior.st_uid != os.getuid() or stat.S_IMODE(prior.st_mode) != 0o600))
                or (executable and not prior.st_mode & stat.S_IXUSR)):
            raise UploadError("UNSAFE_FILE")
        data = bytearray()
        while len(data) <= limit:
            block = os.read(fd, min(1 << 20, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                  "st_size", "st_mtime_ns", "st_ctime_ns")
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (len(data) != prior.st_size or len(data) > limit
                or any(getattr(prior, field) != getattr(after, field) for field in fields)
                or (named.st_dev, named.st_ino) != (prior.st_dev, prior.st_ino)):
            raise UploadError("FILE_CHANGED")
        return bytes(data)
    except OSError:
        raise UploadError("FILE_UNAVAILABLE") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def create_memfd():
    flags = (getattr(os, "MFD_CLOEXEC", None) or 0x0001) | (getattr(os, "MFD_ALLOW_SEALING", None) or 0x0002)
    create = getattr(os, "memfd_create", None)
    if create is not None:
        return create("toss-upload-pinned", flags)
    # The installed SDK Python omits the Python binding although this Linux
    # kernel/libc supports it. Keep the same narrow fallback as the host runner.
    create = ctypes.CDLL(None, use_errno=True).memfd_create
    create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    create.restype = ctypes.c_int
    fd = create(b"toss-upload-pinned", flags)
    if fd < 0:
        raise UploadError("SEAL_FAILED")
    return fd


def seal_file(path: Path, expected_sha: str, *, executable=False):
    data = read_secure(path, executable=executable)
    if not SHA256.fullmatch(expected_sha) or hashlib.sha256(data).hexdigest() != expected_sha:
        raise UploadError("HASH_MISMATCH")
    fd = create_memfd()
    try:
        view = memoryview(data)
        while view:
            amount = os.write(fd, view)
            if amount <= 0:
                raise UploadError("SEAL_FAILED")
            view = view[amount:]
        os.fchmod(fd, 0o500 if executable else 0o400)
        seals = F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL
        fcntl.fcntl(fd, F_ADD_SEALS, seals)
        if fcntl.fcntl(fd, F_GET_SEALS) & seals != seals:
            raise UploadError("SEAL_FAILED")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _varint(data: bytes, offset: int):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        item = data[offset]
        offset += 1
        value |= (item & 127) << shift
        if not item & 128:
            return value, offset
    raise UploadError("INVALID_BUNDLE")


def bundle_identity(data: bytes):
    """Read only AIT v1 protobuf identity fields; never extract ZIP or run JS."""
    if len(data) < 28 or data[:8] != b"AITBUNDL":
        raise UploadError("INVALID_BUNDLE")
    version, length = struct.unpack(">IQ", data[8:20])
    end = 20 + length
    if version != 1 or not 0 < length <= 1024 * 1024 or end + 8 > len(data):
        raise UploadError("INVALID_BUNDLE")
    zip_size = struct.unpack(">Q", data[end:end + 8])[0]
    if not 0 < zip_size <= len(data) - end - 8:
        raise UploadError("INVALID_BUNDLE")
    body, offset, found = data[20:end], 0, {}
    while offset < len(body):
        tag, offset = _varint(body, offset)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise UploadError("INVALID_BUNDLE")
        if wire == 0:
            _, offset = _varint(body, offset)
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        elif wire == 2:
            size, offset = _varint(body, offset)
            if offset + size > len(body):
                raise UploadError("INVALID_BUNDLE")
            if field in (2, 3):
                if field in found or size > 128:
                    raise UploadError("INVALID_BUNDLE")
                try:
                    found[field] = body[offset:offset + size].decode("utf-8")
                except UnicodeError:
                    raise UploadError("INVALID_BUNDLE") from None
            offset += size
        else:
            raise UploadError("INVALID_BUNDLE")
        if offset > len(body):
            raise UploadError("INVALID_BUNDLE")
    deployment = found.get(2, "")
    try:
        parsed = uuid.UUID(deployment)
        if str(parsed) != deployment or parsed.version != 7 or found.get(3) != APP:
            raise ValueError
    except ValueError:
        raise UploadError("BUNDLE_IDENTITY_MISMATCH") from None
    return APP, deployment


def private_link(deployment):
    return f"intoss-private://{APP}?_deploymentId={deployment}&host=appsInTossHost"


def private_directory(path: Path):
    """Return a directory fd after rejecting links and unsafe private root modes."""
    fds = []
    try:
        if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise UploadError("UNSAFE_DIRECTORY")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(fd)
        for name in path.parts[1:]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=fd)
            fds.append(fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise UploadError("UNSAFE_DIRECTORY")
        fds.pop()
        return fd
    except OSError:
        raise UploadError("DIRECTORY_UNAVAILABLE") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def verify_toolchain(root: Path, expected_sha: str):
    fd = private_directory(root)
    os.close(fd)
    data = read_secure(root / "manifest.json", limit=2 * 1024 * 1024)
    if not SHA256.fullmatch(expected_sha) or hashlib.sha256(data).hexdigest() != expected_sha:
        raise UploadError("TOOLCHAIN_CHANGED")
    try:
        doc = json.loads(data)
        if (set(doc) != {"version", "cli_version", "entry", "files"}
                or doc["version"] != 1 or doc["cli_version"] != "3.2.0" or doc["entry"] != ENTRY
                or not isinstance(doc["files"], dict) or not 0 < len(doc["files"]) <= 10000):
            raise ValueError
        expected = doc["files"]
        for name, digest in expected.items():
            parts = PurePosixPath(name).parts
            if (not isinstance(name, str) or not parts or name != PurePosixPath(name).as_posix()
                    or PurePosixPath(name).is_absolute() or "\\" in name
                    or any(part in (".", "..") for part in parts)
                    or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
                raise ValueError
        if ENTRY not in expected or "manifest.json" in expected:
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise UploadError("INVALID_TOOLCHAIN") from None
    actual = set()
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            info = (Path(parent) / name).lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise UploadError("UNSAFE_TOOLCHAIN")
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json":
                continue
            actual.add(relative)
            if (relative not in expected
                    or hashlib.sha256(read_secure(path)).hexdigest() != expected[relative]):
                raise UploadError("TOOLCHAIN_CHANGED")
    if actual != set(expected):
        raise UploadError("TOOLCHAIN_CHANGED")
    return root / ENTRY


def verify_credentials(path: Path):
    if path.name != "credentials" or path.parent.name != ".ait":
        raise UploadError("INVALID_CREDENTIAL_PATH")
    fd = private_directory(path.parent)
    os.close(fd)
    try:
        doc = json.loads(read_secure(path, limit=64 * 1024, private=True))
        token = doc[APP]
        if not isinstance(token, str) or not token or not token.isprintable():
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise UploadError("CREDENTIAL_UNAVAILABLE") from None


def write_receipt(directory_fd, result):
    name = f"{result['deployment_id']}-{secrets.token_hex(8)}.json"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(result, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def bound_child_output():
    """Limit the CLI's private stdout spool before exec, including descendants."""
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "test"))
    for name in ("artifact", "node", "toolchain", "credentials", "receipt-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("sha256", "deployment-id", "node-sha256", "toolchain-sha256"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    fds, receipt_fd = [], None
    result = {"status": "failed", "external_attempted": False, "remote_digest_verified": False}
    code = 1
    try:
        artifact_fd = seal_file(args.artifact, args.sha256)
        fds.append(artifact_fd)
        app, deployment = bundle_identity(Path(f"/proc/self/fd/{artifact_fd}").read_bytes())
        if deployment != args.deployment_id:
            raise UploadError("BUNDLE_IDENTITY_MISMATCH")
        result.update(app=app, deployment_id=deployment, artifact_sha256=args.sha256,
                      artifact_size=os.fstat(artifact_fd).st_size,
                      toolchain_sha256=args.toolchain_sha256)
        cli = verify_toolchain(args.toolchain, args.toolchain_sha256)
        node_fd = seal_file(args.node, args.node_sha256, executable=True)
        fds.append(node_fd)
        if args.action == "check":
            result["status"], code = "preflight_ok", 0
        else:
            verify_credentials(args.credentials)
            receipt_fd = private_directory(args.receipt_dir)
            # The private symlink preserves the CLI's .ait suffix requirement.
            # The child inherits both sealed fds; all of its reopen/stat/stream
            # calls use these same bytes, even if the repository file changes.
            with tempfile.TemporaryDirectory(prefix="upload-", dir=args.receipt_dir) as temporary:
                location = Path(temporary) / "tarororo.ait"
                location.symlink_to(f"/proc/self/fd/{artifact_fd}")
                command = [str(args.node), str(cli), "deploy", "--profile", APP,
                           "--location", str(location), "--scheme-only"]
                environment = {"HOME": str(args.credentials.parent.parent),
                               "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
                with tempfile.TemporaryFile(dir=temporary) as output:
                    result["external_attempted"] = True
                    completed = subprocess.run(command, executable=f"/proc/self/fd/{node_fd}",
                                               pass_fds=(artifact_fd, node_fd), close_fds=True,
                                               cwd=str(args.toolchain), env=environment,
                                               stdin=subprocess.DEVNULL, stdout=output,
                                               stderr=subprocess.DEVNULL, timeout=180, check=False,
                                               preexec_fn=bound_child_output)
                    result["cli_exit_code"] = completed.returncode
                    result["stdout_bytes"] = output.tell()
                    output.seek(0)
                    printed = output.read(4097)
                    if completed.returncode != 0:
                        raise UploadError("CLI_UPLOAD_FAILED")
                    if printed != (private_link(deployment) + "\n").encode():
                        raise UploadError("CLI_RESULT_UNVERIFIED")
                    result.update(status="cli_reported_complete", private_link=private_link(deployment))
                    code = 0
    except UploadError as error:
        result["error"] = str(error)
    except subprocess.TimeoutExpired:
        result["error"] = "CLI_TIMEOUT"
    except Exception:
        result["error"] = "LOCAL_CHECK_FAILED"
    finally:
        if receipt_fd is not None:
            try:
                result["recorded_at_unix"] = int(time.time())
                write_receipt(receipt_fd, result)
            except Exception:
                result.update(status="failed", error="RECEIPT_WRITE_FAILED")
                code = 1
            os.close(receipt_fd)
        for fd in reversed(fds):
            os.close(fd)
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
