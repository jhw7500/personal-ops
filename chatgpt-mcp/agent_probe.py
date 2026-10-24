"""Disposable, offline proofs for the pinned Codex runtime and supervisor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys

from agent_bootstrap import build_bootstrap, validate_bootstrap
from agent_code_mode import CodeModeProbeError, probe_code_mode_host
from agent_profiles import codex_permission_policy


PINNED_CODEX_SHA256 = "628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1"
PINNED_CODE_MODE_HOST_SHA256 = (
    "ec71830690a7c0024916ee0c99fbdc635bc6e747ff665ea4f03a8389bbcb6b30"
)
PINNED_VERSION = "0.162.1"
MAX_CODEX_BYTES = 320 * 1024 * 1024
MAX_PROBE_OUTPUT = 64 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SANDBOX_ASSERTIONS = (
    "workspace_read_allowed",
    "workspace_write_allowed",
    "outside_read_denied",
    "outside_write_denied",
    "fake_credentials_denied",
    "symlink_read_denied",
    "symlink_write_denied",
    "af_inet_denied",
    "manager_bus_denied",
    "manager_private_denied",
    "cgroup_procs_write_open_denied",
    "external_environ_denied",
    "external_cmdline_denied",
    "external_root_alias_denied",
    "private_pid_namespace",
    "unsafe_environment_absent",
)
CODE_MODE_ASSERTIONS = ("code_mode_delegate_executed", "code_mode_shutdown_verified")
REQUIRED_ASSERTIONS = SANDBOX_ASSERTIONS + CODE_MODE_ASSERTIONS


class ProbeError(RuntimeError):
    """A disposable runtime or isolation assertion was not trustworthy."""


def _private_parent(path: Path) -> Path:
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise ProbeError("PROBE_PATH_UNSAFE")
    parent = path.parent
    try:
        info = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise ProbeError("PROBE_PATH_UNSAFE") from exc
    if (not stat.S_ISDIR(info.st_mode) or parent.is_symlink()
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or resolved != parent.absolute()):
        raise ProbeError("PROBE_PATH_UNSAFE")
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise ProbeError("PROBE_PATH_UNSAFE") from exc
    raise ProbeError("PROBE_PATH_EXISTS")


def _open_source(path: Path):
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProbeError("CODEX_SOURCE_UNSAFE") from exc
    opened = os.fstat(descriptor)
    if (path.is_symlink() or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid() or before.st_size < 1
            or before.st_size > MAX_CODEX_BYTES
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_uid != os.getuid() or not stat.S_ISREG(opened.st_mode)):
        os.close(descriptor)
        raise ProbeError("CODEX_SOURCE_UNSAFE")
    return descriptor, before


def _copy_descriptor(source: int, destination: Path) -> None:
    os.lseek(source, 0, os.SEEK_SET)
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700
    )
    try:
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ProbeError("CODEX_STAGE_FAILED")
                view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    os.close(descriptor)


def _descriptor_sha256(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _static_elf_descriptor(descriptor: int) -> None:
    header = os.pread(descriptor, 64, 0)
    size = os.fstat(descriptor).st_size
    if len(header) < 52 or header[:4] != b"\x7fELF" or header[6] != 1:
        raise ProbeError("CODEX_ELF_UNSAFE")
    encoding = "<" if header[5] == 1 else ">" if header[5] == 2 else None
    if encoding is None:
        raise ProbeError("CODEX_ELF_UNSAFE")
    if header[4] == 2:
        program_offset = struct.unpack_from(encoding + "Q", header, 32)[0]
        entry_size = struct.unpack_from(encoding + "H", header, 54)[0]
        entry_count = struct.unpack_from(encoding + "H", header, 56)[0]
    elif header[4] == 1:
        program_offset = struct.unpack_from(encoding + "I", header, 28)[0]
        entry_size = struct.unpack_from(encoding + "H", header, 42)[0]
        entry_count = struct.unpack_from(encoding + "H", header, 44)[0]
    else:
        raise ProbeError("CODEX_ELF_UNSAFE")
    if (entry_count in (0, 0xFFFF) or entry_size < 4 or program_offset < 1
            or program_offset + entry_size * entry_count > size):
        raise ProbeError("CODEX_ELF_UNSAFE")
    table = os.pread(descriptor, entry_size * entry_count, program_offset)
    if len(table) != entry_size * entry_count:
        raise ProbeError("CODEX_ELF_UNSAFE")
    for index in range(entry_count):
        if struct.unpack_from(encoding + "I", table, index * entry_size)[0] == 3:
            raise ProbeError("CODEX_ELF_NOT_STATIC")


def _runtime_file(path: Path, expected_sha256: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProbeError("CODEX_RUNTIME_UNSAFE") from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o700
                or not 1 <= before.st_size <= MAX_CODEX_BYTES):
            raise ProbeError("CODEX_RUNTIME_UNSAFE")
        _static_elf_descriptor(descriptor)
        observed = _descriptor_sha256(descriptor)
        after, linked = os.fstat(descriptor), path.lstat()
        def fields(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if path.is_symlink() or fields(before) != fields(after) or fields(after) != fields(linked):
            raise ProbeError("CODEX_RUNTIME_CHANGED")
        if observed != expected_sha256:
            raise ProbeError("CODEX_RUNTIME_CHANGED")
        return observed
    finally:
        os.close(descriptor)


def stage_codex_runtime(source_executable: Path, expected_sha256: str,
                        destination: Path) -> dict:
    """Copy the exact pinned static CLI/helper and build its static bootstrap."""
    if (not isinstance(expected_sha256, str) or not HEX64.fullmatch(expected_sha256)
            or expected_sha256 != PINNED_CODEX_SHA256):
        raise ProbeError("CODEX_PIN_MISMATCH")
    destination = _private_parent(Path(destination))
    source_executable = Path(source_executable)
    descriptor, before = _open_source(source_executable)
    code_mode_descriptor = None
    created = False
    try:
        digest = _descriptor_sha256(descriptor)
        if digest != expected_sha256:
            raise ProbeError("CODEX_SOURCE_CHANGED")
        code_mode_source = source_executable.parent / "codex-code-mode-host"
        code_mode_descriptor, code_mode_before = _open_source(code_mode_source)
        code_mode_digest = _descriptor_sha256(code_mode_descriptor)
        if code_mode_digest != PINNED_CODE_MODE_HOST_SHA256:
            raise ProbeError("CODEX_CODE_MODE_HOST_SOURCE_CHANGED")
        destination.mkdir(mode=0o700)
        created = True
        executable = destination / "codex"
        helper = destination / "codex-linux-sandbox"
        code_mode_helper = destination / "codex-code-mode-host"
        _copy_descriptor(descriptor, executable)
        _copy_descriptor(descriptor, helper)
        _copy_descriptor(code_mode_descriptor, code_mode_helper)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns):
            raise ProbeError("CODEX_SOURCE_CHANGED")
        code_mode_after = os.fstat(code_mode_descriptor)
        if (code_mode_after.st_dev, code_mode_after.st_ino, code_mode_after.st_size,
                code_mode_after.st_mtime_ns, code_mode_after.st_ctime_ns) != (
                code_mode_before.st_dev, code_mode_before.st_ino, code_mode_before.st_size,
                code_mode_before.st_mtime_ns, code_mode_before.st_ctime_ns):
            raise ProbeError("CODEX_CODE_MODE_HOST_SOURCE_CHANGED")
        _runtime_file(executable, expected_sha256)
        _runtime_file(helper, expected_sha256)
        _runtime_file(code_mode_helper, PINNED_CODE_MODE_HOST_SHA256)
        bootstrap = destination / "personal-ops-agent-bootstrap"
        bootstrap_metadata = build_bootstrap(bootstrap)
        return {
            "executable": str(executable),
            "helper": str(helper),
            "code_mode_helper": str(code_mode_helper),
            "bootstrap": str(bootstrap),
            "version": PINNED_VERSION,
            "executable_sha256": expected_sha256,
            "helper_sha256": expected_sha256,
            "code_mode_helper_sha256": PINNED_CODE_MODE_HOST_SHA256,
            "bootstrap_sha256": bootstrap_metadata["sha256"],
            "bootstrap_source_sha256": bootstrap_metadata["source_sha256"],
        }
    except BaseException:
        if created:
            shutil.rmtree(destination)
        raise
    finally:
        if code_mode_descriptor is not None:
            os.close(code_mode_descriptor)
        os.close(descriptor)


PROBE_SCRIPT = r'''import json, os, socket, sys
uid = int(sys.argv[1])
synthetic_home = sys.argv[2]
peer_pid = int(sys.argv[3])
outside_sentinel = sys.argv[4]
host_pid_namespace = sys.argv[5]
def denied_open(path, flags, follow=False):
    try:
        descriptor = os.open(path, flags if follow else flags | os.O_NOFOLLOW)
    except OSError:
        return True
    os.close(descriptor)
    return False
def controlled_read():
    try:
        with open("allowed-read", "rb") as stream:
            return stream.read() == b"allowed fixture\n"
    except OSError:
        return False
def controlled_write():
    try:
        descriptor = os.open("allowed-write", os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        os.write(descriptor, b"pass\n")
        os.close(descriptor)
        return True
    except OSError:
        return False
def unix_denied(path):
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        channel.connect(path)
    except OSError:
        channel.close()
        return True
    channel.close()
    return False
try:
    internet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError:
    inet_denied = True
else:
    internet.close()
    inet_denied = False
credentials = [
    os.path.join(synthetic_home, ".codex", "auth.json"),
    os.path.join(synthetic_home, ".aws", "credentials"),
    os.path.join(synthetic_home, ".ssh", "id_ed25519"),
    ".codex/auth.json", ".aws/credentials", ".ssh/id_ed25519",
]
result = {
    "workspace_read_allowed": controlled_read(),
    "workspace_write_allowed": controlled_write(),
    "outside_read_denied": denied_open("../outside/outside-read", os.O_RDONLY),
    "outside_write_denied": denied_open("../outside/outside-write", os.O_WRONLY),
    "fake_credentials_denied": all(denied_open(path, os.O_RDONLY) for path in credentials),
    "symlink_read_denied": denied_open("link-read", os.O_RDONLY, follow=True),
    "symlink_write_denied": denied_open("link-write", os.O_WRONLY, follow=True),
    "af_inet_denied": inet_denied,
    "manager_bus_denied": unix_denied(f"/run/user/{uid}/bus"),
    "manager_private_denied": unix_denied(f"/run/user/{uid}/systemd/private"),
    "cgroup_procs_write_open_denied": all(denied_open(path, os.O_WRONLY) for path in (
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.procs",
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/cgroup.procs")),
    # The sandbox mounts its own /proc. Own environ is intentionally readable;
    # the invariant is isolation from a still-live, controlled host process.
    "external_environ_denied": denied_open(f"/proc/{peer_pid}/environ", os.O_RDONLY),
    "external_cmdline_denied": denied_open(f"/proc/{peer_pid}/cmdline", os.O_RDONLY),
    "external_root_alias_denied": denied_open(
        f"/proc/{peer_pid}/root{outside_sentinel}", os.O_RDONLY, follow=True),
    "private_pid_namespace": os.readlink("/proc/self/ns/pid") != host_pid_namespace,
    "unsafe_environment_absent": not any(
        name in {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"}
        or any(marker in name.upper() for marker in ("TOKEN", "API_KEY", "SECRET", "PASSWORD"))
        for name in os.environ),
}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


def _write_private(path: Path, payload: bytes, mode=0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)


def _bounded_command(argv, *, cwd: Path, env: dict, output: Path, bootstrap=None) -> bytes:
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    if bootstrap is not None:
        validate_bootstrap(bootstrap)
        argv = [str(bootstrap), *[f'{key}={value}' for key, value in sorted(env.items())], '--', *argv]
        # Controlled loader variables prove that the static bootstrap strips
        # inherited manager environment before Codex or its sandbox can run.
        process_env = {**env, 'PYTHONPATH': '/synthetic-probe-environment',
                       'LD_PRELOAD': '/synthetic-probe-preload'}
    else:
        process_env = env
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=process_env,
            stdin=subprocess.PIPE if bootstrap is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        os.close(descriptor)
        raise ProbeError("PROBE_EXECUTION_FAILED") from exc
    try:
        try:
            stdout, stderr = process.communicate(input=b'GO\n' if bootstrap is not None else None,
                                                  timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=5)
            _write_private(output.with_suffix('.stderr.log'), stderr[-MAX_PROBE_OUTPUT:])
            raise ProbeError("PROBE_TIMEOUT") from None
        _write_private(output.with_suffix('.stderr.log'), stderr[-MAX_PROBE_OUTPUT:])
        if bootstrap is not None:
            marker = b'PERSONAL_OPS_AGENT_BOOTSTRAP_V1\n'
            if not stdout.startswith(marker):
                raise ProbeError('PROBE_BOOTSTRAP_HANDSHAKE_FAILED')
            stdout = stdout[len(marker):]
        if len(stdout) > MAX_PROBE_OUTPUT:
            raise ProbeError('PROBE_OUTPUT_UNSAFE')
        with os.fdopen(os.dup(descriptor), 'wb') as stream:
            stream.write(stdout)
    finally:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
    if process.returncode != 0:
        raise ProbeError("PROBE_EXECUTION_FAILED")
    try:
        info = output.lstat()
        if (output.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_PROBE_OUTPUT):
            raise ProbeError("PROBE_OUTPUT_UNSAFE")
        return output.read_bytes()
    except OSError as exc:
        raise ProbeError("PROBE_OUTPUT_UNSAFE") from exc


def _run_sandbox(executable: Path, policy: str, workspace: Path, home: Path,
                 codex_home: Path, script: Path, *, peer_pid: int = 1,
                 outside_sentinel: Path | None = None, host_pid_namespace: str = "") -> dict:
    uid = os.getuid()
    for control in (Path(f"/run/user/{uid}/bus"),
                    Path(f"/run/user/{uid}/systemd/private")):
        try:
            if not stat.S_ISSOCK(control.lstat().st_mode):
                raise ProbeError("CONTROL_SOCKET_UNAVAILABLE")
        except OSError as exc:
            raise ProbeError("CONTROL_SOCKET_UNAVAILABLE") from exc
    if not Path("/sys/fs/cgroup/cgroup.procs").is_file():
        raise ProbeError("CGROUP_CONTROL_UNAVAILABLE")
    argv = [
        str(executable),
        "-c", 'default_permissions="delegated"',
        "-c", policy,
        "-c", "features.network_proxy=false",
        "sandbox", "-P", "delegated", "-C", str(workspace),
        "/usr/bin/python3", str(script), str(uid), str(home),
        str(peer_pid), str(outside_sentinel or home / "outside-read"), host_pid_namespace,
    ]
    env = {
        "HOME": os.environ["HOME"],
        "PATH": str(executable.parent) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    if "CODEX_HOME" in os.environ:
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    raw = _bounded_command(argv, cwd=workspace, env=env, output=workspace / "probe-output.json",
                           bootstrap=executable.parent / 'personal-ops-agent-bootstrap')
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("PROBE_OUTPUT_INVALID") from exc
    if (not isinstance(result, dict) or set(result) != set(SANDBOX_ASSERTIONS)
            or any(type(value) is not bool for value in result.values())):
        raise ProbeError("PROBE_OUTPUT_INVALID")
    return result


def _fixture_directory(path: Path) -> Path:
    path = _private_parent(Path(path))
    path.mkdir(mode=0o700)
    return path


def probe_codex_isolation(profile: dict, fixture_root: Path) -> dict:
    """Run the pinned CLI's sandbox only; never contact a model or issue a receipt."""
    if (not isinstance(profile, dict) or profile.get("version") != PINNED_VERSION
            or profile.get("executable_sha256") != PINNED_CODEX_SHA256):
        raise ProbeError("PROFILE_NOT_PINNED")
    executable = Path(profile.get("executable", ""))
    if not executable.is_absolute():
        raise ProbeError("PROFILE_NOT_PINNED")
    helper = executable.parent / "codex-linux-sandbox"
    code_mode_helper = executable.parent / "codex-code-mode-host"
    _runtime_file(executable, PINNED_CODEX_SHA256)
    _runtime_file(helper, PINNED_CODEX_SHA256)
    _runtime_file(code_mode_helper, PINNED_CODE_MODE_HOST_SHA256)
    validate_bootstrap(executable.parent / "personal-ops-agent-bootstrap")
    root = _fixture_directory(Path(fixture_root))
    try:
        code_mode_assertions = probe_code_mode_host(code_mode_helper, root / "code-mode")
    except CodeModeProbeError as exc:
        raise ProbeError(exc.code) from None
    workspace = root / "workspace"
    outside = root / "outside"
    home = root / "synthetic-home"
    codex_home = home / ".codex"
    for folder in (workspace, outside, home, codex_home, home / ".aws", home / ".ssh",
                   workspace / ".codex", workspace / ".aws", workspace / ".ssh"):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    _write_private(workspace / "allowed-read", b"allowed fixture\n")
    _write_private(workspace / "allowed-write", b"")
    _write_private(outside / "outside-read", b"controlled outside fixture\n")
    _write_private(outside / "outside-write", b"controlled outside fixture\n")
    for path in (codex_home / "auth.json", home / ".aws/credentials",
                 home / ".ssh/id_ed25519", workspace / ".codex/auth.json",
                 workspace / ".aws/credentials", workspace / ".ssh/id_ed25519"):
        _write_private(path, b"synthetic credential marker\n")
    (workspace / "link-read").symlink_to(outside / "outside-read")
    (workspace / "link-write").symlink_to(outside / "outside-write")
    script = workspace / "probe.py"
    _write_private(script, PROBE_SCRIPT.encode(), 0o700)
    policy = codex_permission_policy(profile)
    # A denial result is evidence only if the exact aliases were accessible to
    # the unsandboxed caller and the peer is alive throughout the sandbox probe.
    peer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        sentinel = outside / "outside-read"
        aliases = (f"/proc/{peer.pid}/environ", f"/proc/{peer.pid}/cmdline",
                   f"/proc/{peer.pid}/root{sentinel}")
        for alias in aliases:
            descriptor = os.open(alias, os.O_RDONLY)
            os.close(descriptor)
        assertions = _run_sandbox(
            executable, policy, workspace, home, codex_home, script,
            peer_pid=peer.pid, outside_sentinel=sentinel,
            host_pid_namespace=os.readlink("/proc/self/ns/pid"),
        )
        if peer.poll() is not None:
            raise ProbeError("PROBE_PEER_LOST")
    except OSError as exc:
        raise ProbeError("PROBE_NEGATIVE_CONTROL_UNAVAILABLE") from exc
    finally:
        if peer.poll() is None:
            peer.terminate()
        try:
            peer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            peer.kill()
            peer.wait(timeout=5)
    assertions = {**assertions, **code_mode_assertions}
    if (set(assertions) != set(REQUIRED_ASSERTIONS)
            or any(value is not True for value in assertions.values())):
        raise ProbeError("ISOLATION_ASSERTION_FAILED")
    return {
        "passed": True,
        "version": PINNED_VERSION,
        "executable_sha256": PINNED_CODEX_SHA256,
        "helper_sha256": PINNED_CODEX_SHA256,
        "code_mode_helper_sha256": PINNED_CODE_MODE_HOST_SHA256,
        "assertions": {name: assertions[name] for name in REQUIRED_ASSERTIONS},
    }


async def probe_supervisor_fixture(fixture_root: Path) -> dict:
    """Explicit free proof: stop one newly-created transient unit and its descendants."""
    from agent_supervisor import SystemdAgentSupervisor

    root = _fixture_directory(Path(fixture_root))
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    python = runtime / "python"
    source = Path("/usr/bin/python3").resolve(strict=True)
    try:
        source_info = source.lstat()
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_uid not in (0, os.getuid()) or source_info.st_mode & 0o022:
            raise ProbeError("FIXTURE_EXECUTABLE_UNSAFE")
        descriptor = os.open(python, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o700)
        with source.open("rb", buffering=0) as input_stream, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o700)
    except OSError as exc:
        raise ProbeError("FIXTURE_EXECUTABLE_UNSAFE") from exc
    bootstrap_metadata = build_bootstrap(runtime / "personal-ops-agent-bootstrap")
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    codex_home = root / "synthetic-codex-home"
    codex_home.mkdir(mode=0o700)
    supervisor = SystemdAgentSupervisor()
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process = None
    identity = None
    descendant_pid = None
    try:
        code = (
            "import json,os,time; safe=set(os.environ)<="
            "{'HOME','CODEX_HOME','PATH','LANG','LC_ALL','SSL_CERT_FILE','GIT_CONFIG_NOSYSTEM','GIT_CONFIG_GLOBAL'}; "
            "child=os.fork(); "
            "(os.setsid(),time.sleep(30)) if child==0 else "
            "(print(json.dumps({'descendant_pid':child,'environment_safe':safe}),flush=True),time.sleep(30))"
        )
        env = {
            "HOME": os.environ["HOME"],
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if "CODEX_HOME" in os.environ:
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        process, identity = await supervisor.start(
            secrets.token_hex(16), [str(python), "-c", code], workspace, env, 30,
            expected_bootstrap_sha256=bootstrap_metadata['sha256'],
        )
        process.stdin.write(b"GO\n")
        await process.stdin.drain()
        process.stdin.close()
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        child = json.loads(raw)
        descendant_pid = child.get("descendant_pid")
        if type(descendant_pid) is not int or descendant_pid <= 1 or child.get('environment_safe') is not True:
            raise ProbeError("SUPERVISOR_FIXTURE_INVALID")
        stopped = await supervisor.stop(identity)
        await asyncio.wait_for(process.wait(), timeout=8)
        cgroup_empty = supervisor.cgroup_empty(identity["cgroup"])
        unrelated_alive = unrelated.returncode is None
        descendant_gone = False
        for _ in range(40):
            try:
                raw_state = Path(f"/proc/{descendant_pid}/stat").read_text()
                descendant_gone = raw_state.rsplit(") ", 1)[1].split()[0] in {"Z", "X"}
            except FileNotFoundError:
                descendant_gone = True
            if descendant_gone:
                break
            await asyncio.sleep(0.05)
        if (stopped != {"termination_verified": True} or not cgroup_empty
                or not unrelated_alive or not descendant_gone):
            raise ProbeError("SUPERVISOR_FIXTURE_FAILED")
        return {
            "passed": True,
            "task_pid": identity["pid"],
            "descendant_pid": descendant_pid,
            "unrelated_pid": unrelated.pid,
            "termination_verified": True,
            "cgroup_empty": True,
            "descendant_gone": True,
            "unrelated_alive": True,
            "bootstrap_environment_safe": True,
        }
    finally:
        cleanup_failed = False
        if identity is not None:
            try:
                if not supervisor.cgroup_empty(identity["cgroup"]):
                    await supervisor.stop(identity)
                if supervisor.cgroup_empty(identity['cgroup']):
                    supervisor.forget(identity)
                else:
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                _write_private(root / 'unresolved-supervisor.json',
                               json.dumps(identity, sort_keys=True).encode())
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if unrelated.returncode is None:
            unrelated.terminate()
            try:
                await asyncio.wait_for(unrelated.wait(), timeout=5)
            except TimeoutError:
                unrelated.kill()
                await unrelated.wait()
        if cleanup_failed:
            original = sys.exc_info()[1]
            if original is not None:
                original.add_note('SUPERVISOR_CLEANUP_FAILED: private unresolved identity retained')
            else:
                raise ProbeError('SUPERVISOR_CLEANUP_FAILED')
