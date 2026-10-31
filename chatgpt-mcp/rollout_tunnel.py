"""Guarded activation of the existing personal-ops tunnel user service.

The module never discovers or creates a tunnel.  It accepts only the known
oneshot unit/profile shape, freezes the actual live MCP child from ``/proc``,
and changes the MCP command while preserving every other setting.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from workspace_control import (DEFAULT_CONTROLLER_DIR, WorkspaceControlError,
                               transition_release, verify_controller, verify_launch)


ALIAS = "personal-ops-coding"
SERVICE = "personal-ops-tunnel.service"
RELEASE_ROOT = Path.home() / ".local/lib/chatgpt-coding-mcp/releases"
PROFILE = Path.home() / ".config/tunnel-client/personal-ops-coding.yaml"
OVERRIDE = (Path.home() / ".config/systemd/user" / (SERVICE + ".d")
            / "20-runtime.conf")
_VALUE_FLAGS = (
    "--alias", "--profile", "--profile-dir", "--tunnel-id",
    "--runtime-api-key", "--control-plane-base-url", "--mcp-command",
)
_PROFILE_KEYS = {"admin_ui", "config_version", "control_plane", "health", "log", "mcp"}


class TunnelSwitchError(RuntimeError):
    def __init__(self, code, *, cause_code=None, rollback_status=None):
        self.code = code
        self.cause_code = cause_code
        self.rollback_status = rollback_status
        parts = [code]
        if cause_code:
            parts.append(cause_code)
        if rollback_status:
            parts.append("ROLLBACK_" + rollback_status.upper())
        super().__init__(":".join(parts))


def _owned_directory(path: Path, mode: int = 0o700) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
        raise TunnelSwitchError("UNSAFE_TUNNEL_PATH")


def _owned_safe_directory(path: Path) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
        raise TunnelSwitchError("UNSAFE_TUNNEL_PATH")


def _owned_file(path: Path, mode: int = 0o600) -> None:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
        raise TunnelSwitchError("UNSAFE_TUNNEL_PATH")


def _owned_tunnel_client(path: Path) -> None:
    try:
        lexical = path.lstat()
        target = path.resolve(strict=True)
        target_info = target.lstat()
    except (OSError, RuntimeError) as exc:
        raise TunnelSwitchError("UNSAFE_TUNNEL_CLIENT") from exc
    if lexical.st_uid != os.getuid():
        raise TunnelSwitchError("UNSAFE_TUNNEL_CLIENT")
    if path.is_symlink():
        trusted = path.parent.parent / "share/openai-tunnel"
        if not target.is_relative_to(trusted):
            raise TunnelSwitchError("UNSAFE_TUNNEL_CLIENT")
    elif not stat.S_ISREG(lexical.st_mode):
        raise TunnelSwitchError("UNSAFE_TUNNEL_CLIENT")
    if (not stat.S_ISREG(target_info.st_mode) or target_info.st_uid != os.getuid()
            or not os.access(target, os.X_OK)):
        raise TunnelSwitchError("UNSAFE_TUNNEL_CLIENT")


def _approved_interpreter(path: Path, approved_interpreters=None) -> Path:
    if not path.is_absolute():
        raise TunnelSwitchError("UNAPPROVED_MCP_INTERPRETER")
    defaults = (
        Path(sys.executable), Path("/usr/bin/python3"), Path("/usr/local/bin/python3"),
        Path.home() / ".local/share/uv/tools/serena-agent/bin/python",
    )
    approved = tuple(Path(item) for item in (approved_interpreters or defaults))
    if path not in approved:
        raise TunnelSwitchError("UNAPPROVED_MCP_INTERPRETER")
    try:
        target = path.resolve(strict=True)
        info = target.lstat()
    except (OSError, RuntimeError) as exc:
        raise TunnelSwitchError("UNAPPROVED_MCP_INTERPRETER") from exc
    trusted_system = (target.is_relative_to(Path("/usr/bin"))
                      or target.is_relative_to(Path("/usr/local/bin")))
    owner_ok = (info.st_uid == os.getuid()
                or (trusted_system and info.st_uid == 0
                    and not stat.S_IMODE(info.st_mode) & 0o022))
    if (not stat.S_ISREG(info.st_mode) or not owner_ok or not os.access(target, os.X_OK)):
        raise TunnelSwitchError("UNAPPROVED_MCP_INTERPRETER")
    return path


def _verify_runtime(runtime, releases=RELEASE_ROOT) -> dict:
    releases = Path(releases).absolute()
    runtime = Path(runtime).absolute()
    try:
        _owned_directory(releases)
        _owned_directory(runtime)
    except OSError as exc:
        raise TunnelSwitchError("UNSAFE_RUNTIME_SNAPSHOT") from exc
    if (runtime.parent != releases or not re.fullmatch(r"[0-9a-f]{20}", runtime.name)):
        raise TunnelSwitchError("UNSAFE_RUNTIME_SNAPSHOT")
    manifest_path = runtime / "manifest.json"
    try:
        _owned_file(manifest_path)
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH") from exc
    if (not isinstance(manifest, dict) or "server.py" not in manifest
            or not all(isinstance(name, str) and isinstance(digest, str)
                       and re.fullmatch(r"[0-9a-f]{64}", digest)
                       for name, digest in manifest.items())):
        raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH")
    expected_snapshot = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    if expected_snapshot != runtime.name:
        raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH")
    for name, expected in manifest.items():
        relative = Path(name)
        if (relative.is_absolute() or not relative.parts
                or any(part in ("", ".", "..") for part in relative.parts)):
            raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH")
        path = runtime / relative
        try:
            if path.parent != runtime:
                _owned_directory(path.parent)
            _owned_file(path)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH") from exc
        if actual != expected:
            raise TunnelSwitchError("RUNTIME_MANIFEST_MISMATCH")
    return {"snapshot": runtime.name, "runtime": runtime,
            "server": runtime / "server.py"}


def _verify_workspace_launch(runtime, authority_dir=None) -> dict:
    try:
        return verify_launch(runtime, authority_dir)
    except WorkspaceControlError as exc:
        raise TunnelSwitchError(exc.code) from exc


def _transition_workspace_launch(runtime, authority_dir=None) -> dict:
    try:
        return transition_release(runtime, authority_dir)
    except WorkspaceControlError as exc:
        raise TunnelSwitchError(exc.code) from exc


def _workspace_command(admission, runtime, interpreter, authority_dir,
                       controller_dir=None, entrypoint="server.py") -> list[str]:
    if not admission["authority_active"]:
        return [str(interpreter), str(runtime / entrypoint)]
    try:
        controller = verify_controller(controller_dir or DEFAULT_CONTROLLER_DIR)
    except WorkspaceControlError as exc:
        raise TunnelSwitchError(exc.code) from exc
    return [
        str(interpreter), controller["launcher"],
        "--authority-dir", admission["authority_dir"],
        "--release", str(runtime), "--interpreter", str(interpreter),
        "--entrypoint", entrypoint,
    ]


def _process_identity(proc_root: Path, pid: int) -> tuple[int, int]:
    folder = proc_root / str(pid)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise TunnelSwitchError("UNOWNED_PROCESS")
    line = (folder / "stat").read_text()
    marker = line.rfind(") ")
    if marker < 0:
        raise TunnelSwitchError("UNEXPECTED_PROCESS_STAT")
    fields = line[marker + 2:].split()
    if len(fields) < 20 or not fields[1].isdecimal() or not fields[19].isdecimal():
        raise TunnelSwitchError("UNEXPECTED_PROCESS_STAT")
    return int(fields[1]), int(fields[19])


def _process_argv(proc_root: Path, pid: int) -> list[str]:
    raw = (proc_root / str(pid) / "cmdline").read_bytes()
    if len(raw) > 64 * 1024:
        raise TunnelSwitchError("UNEXPECTED_PROCESS_COMMAND")
    try:
        return [os.fsdecode(item) for item in raw.rstrip(b"\0").split(b"\0") if item]
    except UnicodeError as exc:
        raise TunnelSwitchError("UNEXPECTED_PROCESS_COMMAND") from exc


def freeze_live_runtime(status, releases=RELEASE_ROOT, proc_root=Path("/proc"),
                        approved_interpreters=None) -> dict:
    """Freeze the live tunnel and its single direct, verified MCP child."""
    if not isinstance(status, dict) or not isinstance(status.get("process"), dict):
        raise TunnelSwitchError("UNEXPECTED_TUNNEL_STATUS")
    tunnel_pid = status["process"].get("pid")
    if isinstance(tunnel_pid, bool) or not isinstance(tunnel_pid, int) or tunnel_pid <= 1:
        raise TunnelSwitchError("UNEXPECTED_TUNNEL_STATUS")
    proc_root = Path(proc_root)
    try:
        tunnel_ppid, tunnel_ticks = _process_identity(proc_root, tunnel_pid)
        candidates = []
        candidate_errors = []
        codex_candidates = []
        for folder in proc_root.iterdir():
            if not folder.name.isdecimal() or int(folder.name) == tunnel_pid:
                continue
            pid = int(folder.name)
            try:
                ppid, started = _process_identity(proc_root, pid)
                if ppid != tunnel_pid:
                    continue
                argv = _process_argv(proc_root, pid)
                if argv == ["codex", "app-server"]:
                    codex_candidates.append((pid, started))
                    continue
                if len(argv) != 2 or Path(argv[1]).name != "server.py":
                    continue
                try:
                    interpreter = _approved_interpreter(
                        Path(argv[0]), approved_interpreters)
                    runtime = _verify_runtime(Path(argv[1]).parent, releases)
                except TunnelSwitchError as exc:
                    candidate_errors.append(exc)
                    continue
                if runtime["server"] != Path(argv[1]):
                    continue
                candidates.append((pid, started, interpreter, runtime))
            except (OSError, TunnelSwitchError):
                continue
        if candidate_errors:
            raise candidate_errors[0]
        if not candidates:
            raise TunnelSwitchError("LIVE_MCP_CHILD_NOT_FOUND")
        if len(candidates) != 1:
            raise TunnelSwitchError("AMBIGUOUS_MCP_CHILD")
        if len(codex_candidates) > 1:
            raise TunnelSwitchError("AMBIGUOUS_CODEX_CHILD")
        mcp_pid, mcp_ticks, interpreter, runtime = candidates[0]
        if _process_identity(proc_root, tunnel_pid) != (tunnel_ppid, tunnel_ticks):
            raise TunnelSwitchError("PROCESS_IDENTITY_CHANGED")
        if _process_identity(proc_root, mcp_pid) != (tunnel_pid, mcp_ticks):
            raise TunnelSwitchError("PROCESS_IDENTITY_CHANGED")
        if codex_candidates:
            codex_pid, codex_ticks = codex_candidates[0]
            if _process_identity(proc_root, codex_pid) != (tunnel_pid, codex_ticks):
                raise TunnelSwitchError("PROCESS_IDENTITY_CHANGED")
    except FileNotFoundError as exc:
        raise TunnelSwitchError("PROCESS_IDENTITY_CHANGED") from exc
    result = {
        "snapshot": runtime["snapshot"],
        "tunnel_pid": tunnel_pid, "tunnel_start_ticks": tunnel_ticks,
        "mcp_pid": mcp_pid, "mcp_start_ticks": mcp_ticks,
        "server": str(runtime["server"]), "interpreter": str(interpreter),
    }
    if codex_candidates:
        result.update(codex_pid=codex_pid, codex_start_ticks=codex_ticks)
    return result


def _effective_commands(unit_text: str, name: str) -> list[str]:
    section = ""
    commands = []
    for source_line in unit_text.splitlines():
        line = source_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section == "[Service]" and line.startswith(name + "="):
            value = line.split("=", 1)[1].strip()
            if value:
                commands.append(value)
            else:
                commands = []
    return commands


def _unit_context(unit_text: str) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Return semantic unit content excluding the one directive this module owns."""
    section = ""
    sections = []
    directives = []
    for source_line in unit_text.splitlines():
        line = source_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            if section not in sections:
                sections.append(section)
            continue
        if not line.startswith("ExecStart="):
            directives.append((section, line))
    return tuple(sections), tuple(directives)


def _parse_unit(unit_text: str, client: Path, profile_path: Path) -> dict:
    starts = _effective_commands(unit_text, "ExecStart")
    stops = _effective_commands(unit_text, "ExecStop")
    if len(starts) != 1 or len(stops) != 1:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    if any(marker in starts[0] + stops[0] for marker in ("$", "%", "\\", "\n")):
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    try:
        start = shlex.split(starts[0])
        stop = shlex.split(stops[0])
    except ValueError as exc:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT") from exc
    if start[:3] != [str(client), "runtimes", "connect"]:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    if stop != [str(client), "runtimes", "stop", ALIAS]:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    values = {}
    index = 3
    while index < len(start):
        flag = start[index]
        if flag == "--json" and index == len(start) - 1:
            index += 1
            continue
        if flag not in _VALUE_FLAGS or index + 1 >= len(start) or flag in values:
            raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
        values[flag] = start[index + 1]
        index += 2
    if set(values) != set(_VALUE_FLAGS):
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    parsed_url = urlsplit(values["--control-plane-base-url"])
    if (values["--alias"] != ALIAS or values["--profile"] != ALIAS
            or Path(values["--profile-dir"]) != profile_path.parent
            or not values["--tunnel-id"]
            or not values["--runtime-api-key"].startswith("file:")
            or parsed_url.scheme != "https" or not parsed_url.netloc
            or parsed_url.username or parsed_url.password
            or parsed_url.query or parsed_url.fragment):
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    return {"argv": start, "mcp_index": start.index("--mcp-command") + 1}


def _parse_profile(raw: bytes) -> dict:
    def exact_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate JSON key")
        return value

    def reject_constant(_):
        raise ValueError("non-finite JSON number")

    try:
        data = json.loads(
            raw, object_pairs_hook=exact_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_PROFILE") from exc
    if not isinstance(data, dict) or set(data) != _PROFILE_KEYS:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_PROFILE")
    try:
        commands = data["mcp"]["commands"]
    except (KeyError, TypeError) as exc:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_PROFILE") from exc
    if (not isinstance(commands, list) or len(commands) != 1
            or not isinstance(commands[0], dict)
            or set(commands[0]) != {"channel", "command"}
            or commands[0]["channel"] != "main"
            or not isinstance(commands[0]["command"], str)):
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_PROFILE")
    return data


def _canonical_profile(raw: bytes) -> bytes:
    return json.dumps(
        _parse_profile(raw), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()


def _validate_override(raw: bytes) -> None:
    try:
        text = raw.decode("utf8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TunnelSwitchError("UNSUPPORTED_TUNNEL_OVERRIDE") from exc
    section = ""
    reset = 0
    command = 0
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            if line != "[Service]":
                raise TunnelSwitchError("UNSUPPORTED_TUNNEL_OVERRIDE")
            section = line
            continue
        if section != "[Service]" or not line.startswith("ExecStart="):
            raise TunnelSwitchError("UNSUPPORTED_TUNNEL_OVERRIDE")
        if line == "ExecStart=":
            reset += 1
        else:
            command += 1
    if reset != 1 or command != 1:
        raise TunnelSwitchError("UNSUPPORTED_TUNNEL_OVERRIDE")


def _validate_mcp_command(command: str, releases: Path, approved_interpreters=None,
                          allow_bare=True) -> None:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND") from exc
    if not argv:
        raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND")
    interpreter = Path(argv[0])
    if interpreter.is_absolute():
        _approved_interpreter(interpreter, approved_interpreters)
    elif not allow_bare or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", argv[0]):
        raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND")
    if len(argv) == 2:
        runtime = _verify_runtime(Path(argv[1]).parent, releases)
        if Path(argv[1]) != runtime["server"]:
            raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND")
        return
    if (len(argv) != 10 or argv[2] != "--authority-dir" or argv[4] != "--release"
            or argv[6] != "--interpreter" or argv[8:]
            != ["--entrypoint", "server.py"]
            or Path(argv[7]) != interpreter):
        raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND")
    try:
        controller = verify_controller(Path(argv[1]).parent)
        if controller["launcher"] != argv[1]:
            raise WorkspaceControlError("UNSAFE_WORKSPACE_CONTROLLER")
        admission = verify_launch(argv[5], argv[3])
    except WorkspaceControlError as exc:
        raise TunnelSwitchError(exc.code) from exc
    if not admission["authority_active"]:
        raise TunnelSwitchError("UNRECOGNIZED_MCP_COMMAND")
    _verify_runtime(Path(argv[5]), releases)


def _read_unit(unit_fn) -> bytes:
    value = unit_fn()
    if isinstance(value, str):
        value = value.encode()
    if not isinstance(value, bytes) or len(value) > 128 * 1024:
        raise TunnelSwitchError("UNRECOGNIZED_TUNNEL_UNIT")
    return value


def preflight_tunnel(live, *, unit_fn, profile_path=PROFILE,
                     override_path=OVERRIDE, releases=RELEASE_ROOT,
                     client=None, approved_interpreters=None) -> dict:
    """Validate the existing live identity, unit and profile without writing."""
    releases = Path(releases).absolute()
    profile_path = Path(profile_path).absolute()
    override_path = Path(override_path).absolute()
    client = Path(client or (Path.home() / ".local/bin/tunnel-client")).absolute()
    runtime = _verify_runtime(Path(live["server"]).parent, releases)
    if (runtime["snapshot"] != live.get("snapshot")
            or str(runtime["server"]) != live.get("server")):
        raise TunnelSwitchError("LIVE_RUNTIME_MISMATCH")
    _approved_interpreter(Path(live["interpreter"]), approved_interpreters)
    _owned_tunnel_client(client)
    _owned_directory(profile_path.parent)
    _owned_file(profile_path)
    profile_bytes = profile_path.read_bytes()
    profile_data = _parse_profile(profile_bytes)
    profile_command = profile_data["mcp"]["commands"][0]["command"]
    _validate_mcp_command(profile_command, releases, approved_interpreters)
    unit_bytes = _read_unit(unit_fn)
    unit = _parse_unit(unit_bytes.decode("utf8", errors="strict"), client, profile_path)
    service_command = unit["argv"][unit["mcp_index"]]
    _validate_mcp_command(service_command, releases, approved_interpreters)
    live_argv = [live["interpreter"], live["server"]]
    if override_path.exists() or override_path.is_symlink():
        _owned_directory(override_path.parent)
        _owned_file(override_path)
        override_bytes = override_path.read_bytes()
        _validate_override(override_bytes)
    else:
        _owned_safe_directory(override_path.parent.parent)
        override_bytes = None
    return {
        "live": dict(live), "unit_bytes": unit_bytes, "unit_fn": unit_fn,
        "unit_argv": unit["argv"], "unit_mcp_index": unit["mcp_index"],
        "unit_context": _unit_context(unit_bytes.decode("utf8", errors="strict")),
        "profile_path": profile_path, "profile_bytes": profile_bytes,
        "profile_data": profile_data, "override_path": override_path,
        "override_bytes": override_bytes, "client": client,
        "profile_aligned": shlex.split(profile_command) == live_argv,
        "service_aligned": shlex.split(service_command) == live_argv,
    }


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".",
                                             dir=path.parent)
    temporary = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _current_override(plan) -> bytes | None:
    path = plan["override_path"]
    if path.exists() or path.is_symlink():
        _owned_file(path)
        return path.read_bytes()
    return None


def _current_file(path: Path) -> bytes | None:
    if path.exists() or path.is_symlink():
        _owned_file(path)
        return path.read_bytes()
    return None


def _cas_ok(plan) -> None:
    if (_read_unit(plan["unit_fn"]) != plan["unit_bytes"]
            or _current_file(plan["profile_path"]) != plan["profile_bytes"]
            or _current_override(plan) != plan["override_bytes"]):
        raise TunnelSwitchError("TUNNEL_CONFIGURATION_CHANGED")


def _target_bytes(plan, command_argv) -> tuple[bytes, bytes, list[str]]:
    command = shlex.join(command_argv)
    profile = json.loads(json.dumps(plan["profile_data"]))
    profile["mcp"]["commands"][0]["command"] = command
    profile_bytes = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    argv = list(plan["unit_argv"])
    argv[plan["unit_mcp_index"]] = command
    override = ("[Service]\nExecStart=\nExecStart=" + shlex.join(argv) + "\n").encode()
    return profile_bytes, override, argv


def _cas_write(path: Path, expected: bytes | None, value: bytes | None) -> None:
    if _current_file(path) != expected:
        raise TunnelSwitchError("TUNNEL_CONFIGURATION_CHANGED")
    if value is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, value)


def _write_target(plan, state, command_argv) -> list[str]:
    profile, override, argv = _target_bytes(plan, command_argv)
    parent = plan["override_path"].parent
    if not parent.exists():
        parent.mkdir(mode=0o700)
    _owned_directory(parent)
    previous = dict(state)
    try:
        _cas_write(plan["profile_path"], state["profile"], profile)
        state["profile"] = profile
        _cas_write(plan["override_path"], state["override"], override)
        state["override"] = override
    except BaseException:
        if state["profile"] != previous["profile"]:
            try:
                _cas_write(plan["profile_path"], state["profile"], previous["profile"])
                state["profile"] = previous["profile"]
            except (OSError, TunnelSwitchError):
                pass
        raise
    return argv


def _restore_original(plan, state) -> None:
    conflicts = []
    for key, path, original in (
        ("profile", plan["profile_path"], plan["profile_bytes"]),
        ("override", plan["override_path"], plan["override_bytes"]),
    ):
        if state[key] == original:
            continue
        try:
            _cas_write(path, state[key], original)
            state[key] = original
        except (OSError, TunnelSwitchError):
            conflicts.append(key)
    if conflicts:
        raise TunnelSwitchError("TUNNEL_CONFIGURATION_CHANGED")


def _run(command_fn, argv) -> bytes:
    result = command_fn(argv, stdout=subprocess.PIPE)
    if result is None:
        return b""
    if isinstance(result, bytes):
        return result
    if hasattr(result, "returncode"):
        if result.returncode:
            raise TunnelSwitchError("TUNNEL_SYSTEMD_COMMAND_FAILED")
        return result.stdout or b""
    raise TunnelSwitchError("TUNNEL_SYSTEMD_COMMAND_FAILED")


def _same_process(proc_root: Path, pid: int, ticks: int) -> bool:
    try:
        return _process_identity(proc_root, pid)[1] == ticks
    except (OSError, TunnelSwitchError):
        return False


def _wait_stopped(live, proc_root: Path, attempts: int, sleep_fn,
                  deadline: float, clock_fn) -> None:
    for _ in range(attempts):
        identities = ((live["tunnel_pid"], live["tunnel_start_ticks"]),
                      (live["mcp_pid"], live["mcp_start_ticks"]))
        if "codex_pid" in live:
            identities += ((live["codex_pid"], live["codex_start_ticks"]),)
        if not any(_same_process(proc_root, pid, ticks) for pid, ticks in identities):
            return
        if clock_fn() >= deadline:
            break
        sleep_fn(0.25)
    raise TunnelSwitchError("TUNNEL_STOP_IDENTITY_STILL_RUNNING")


_TRANSIENT_RUNTIME_CODES = {
    "TUNNEL_RUNTIME_STATUS_FAILED", "UNEXPECTED_TUNNEL_STATUS",
    "PROCESS_IDENTITY_CHANGED", "LIVE_MCP_CHILD_NOT_FOUND",
}


def _error_code(error) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", code):
        return code
    message = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", message):
        return message
    return None


def _is_interrupt(error) -> bool:
    return (isinstance(error, (KeyboardInterrupt, SystemExit))
            or _error_code(error) == "ROLLOUT_TERMINATED")


def _wait_runtime(status_fn, snapshot: str, interpreter: Path, releases: Path,
                  proc_root: Path, approved_interpreters, attempts: int,
                  sleep_fn, deadline: float, clock_fn) -> dict:
    mismatch = False
    for _ in range(attempts):
        try:
            status = status_fn()
            ready = (isinstance(status, dict)
                     and all(status.get(key) is True
                             for key in ("process_running", "healthy", "ready")))
            if ready:
                live = freeze_live_runtime(status, releases, proc_root,
                                           approved_interpreters)
                if (live["snapshot"] == snapshot
                        and Path(live["interpreter"]) == interpreter):
                    return live
                mismatch = True
        except BaseException as exc:
            code = _error_code(exc)
            if _is_interrupt(exc) or code not in _TRANSIENT_RUNTIME_CODES:
                raise
        if clock_fn() >= deadline:
            break
        sleep_fn(0.25)
    if mismatch:
        raise TunnelSwitchError("LIVE_RUNTIME_MISMATCH")
    raise TunnelSwitchError("TUNNEL_START_NOT_READY")


def _assert_resources(plan, state) -> None:
    current_profile = _current_file(plan["profile_path"])
    try:
        profile_matches = (current_profile is not None
                           and _canonical_profile(current_profile)
                           == _canonical_profile(state["profile"]))
    except (TunnelSwitchError, TypeError, ValueError):
        profile_matches = False
    if (not profile_matches or _current_override(plan) != state["override"]):
        raise TunnelSwitchError("TUNNEL_CONFIGURATION_CHANGED")
    state["profile"] = current_profile


def _assert_unit_argv(plan, expected) -> None:
    unit_text = _read_unit(plan["unit_fn"]).decode("utf8", errors="strict")
    if _unit_context(unit_text) != plan["unit_context"]:
        raise TunnelSwitchError("TUNNEL_UNIT_CHANGED")
    unit = _parse_unit(unit_text,
                       plan["client"], plan["profile_path"])
    if unit["argv"] != expected:
        raise TunnelSwitchError("TUNNEL_UNIT_CHANGED")


def _same_live(left, right) -> bool:
    keys = ("snapshot", "tunnel_pid", "tunnel_start_ticks", "mcp_pid",
            "mcp_start_ticks", "server", "interpreter", "codex_pid",
            "codex_start_ticks")
    return all(left.get(key) == right.get(key) for key in keys)


def _rollback_live(status_fn, old, observed_new, *,
                   releases, proc_root, approved_interpreters, attempts,
                   deadline, clock_fn, sleep_fn):
    for _ in range(attempts):
        try:
            status = status_fn()
            if isinstance(status, dict) and status.get("process_running") is False:
                return None
            current = freeze_live_runtime(status, releases, proc_root,
                                          approved_interpreters)
            if _same_live(current, old):
                return current
            if observed_new is not None and _same_live(current, observed_new):
                return current
            raise TunnelSwitchError("UNEXPECTED_ROLLBACK_RUNTIME")
        except BaseException as exc:
            code = _error_code(exc)
            if _is_interrupt(exc) or code not in _TRANSIENT_RUNTIME_CODES:
                raise
        if clock_fn() >= deadline:
            break
        sleep_fn(0.25)
    raise TunnelSwitchError("ROLLBACK_RUNTIME_IDENTITY_UNAVAILABLE")


def switch_tunnel(new_runtime, *, interpreter, status_fn, command_fn, plan=None,
                  unit_fn=None, profile_path=PROFILE, override_path=OVERRIDE,
                  releases=RELEASE_ROOT, client=None, proc_root=Path("/proc"),
                  approved_interpreters=None, sleep_fn=time.sleep, attempts=40,
                  poll_timeout=10.0, clock_fn=time.monotonic, verify_fn=None,
                  authority_dir=None, controller_dir=None) -> dict:
    """Activate a verified release or restore the actual prior live release."""
    releases = Path(releases).absolute()
    proc_root = Path(proc_root)
    interpreter = _approved_interpreter(Path(interpreter), approved_interpreters)
    target = _verify_runtime(new_runtime, releases)
    target_admission = _verify_workspace_launch(target["runtime"], authority_dir)
    target_command = _workspace_command(
        target_admission, target["runtime"], interpreter, authority_dir,
        controller_dir,
    )
    if plan is None:
        live = freeze_live_runtime(status_fn(), releases, proc_root,
                                   approved_interpreters)
        if unit_fn is None:
            raise TunnelSwitchError("TUNNEL_UNIT_READER_REQUIRED")
        plan = preflight_tunnel(
            live, unit_fn=unit_fn, profile_path=profile_path,
            override_path=override_path, releases=releases, client=client,
            approved_interpreters=approved_interpreters)
    current = freeze_live_runtime(status_fn(), releases, proc_root,
                                  approved_interpreters)
    if not _same_live(current, plan["live"]):
        raise TunnelSwitchError("LIVE_RUNTIME_CHANGED")
    _cas_ok(plan)
    old = plan["live"]
    state = {"profile": plan["profile_bytes"], "override": plan["override_bytes"]}
    reload_attempted = False
    service_touched = False
    observed_new = None
    try:
        new_argv = _write_target(plan, state, target_command)
        _assert_resources(plan, state)
        reload_attempted = True
        _run(command_fn, ["systemctl", "--user", "daemon-reload"])
        _assert_resources(plan, state)
        _assert_unit_argv(plan, new_argv)
        service_touched = True
        _run(command_fn, ["systemctl", "--user", "stop", SERVICE])
        deadline = clock_fn() + poll_timeout
        _wait_stopped(old, proc_root, attempts, sleep_fn, deadline, clock_fn)
        _assert_resources(plan, state)
        _assert_unit_argv(plan, new_argv)
        if target_admission["authority_active"]:
            _transition_workspace_launch(target["runtime"], authority_dir)
        else:
            _verify_workspace_launch(target["runtime"], authority_dir)
        _run(command_fn, ["systemctl", "--user", "start", SERVICE])
        deadline = clock_fn() + poll_timeout
        actual = _wait_runtime(status_fn, target["snapshot"], interpreter, releases,
                               proc_root, approved_interpreters, attempts, sleep_fn,
                               deadline, clock_fn)
        observed_new = dict(actual)
        if verify_fn is not None:
            verify_fn(actual)
        return {"status": "tunnel_ready", **actual}
    except BaseException as cause:
        reason = _error_code(cause) or "TUNNEL_SWITCH_FAILED"
        if not service_touched:
            try:
                _restore_original(plan, state)
                if reload_attempted:
                    _run(command_fn, ["systemctl", "--user", "daemon-reload"])
                    _assert_unit_argv(plan, plan["unit_argv"])
            except BaseException as restore_error:
                raise TunnelSwitchError("TUNNEL_CONFIGURATION_RESTORE_FAILED",
                                        cause_code=reason,
                                        rollback_status="failed") from restore_error
            if _is_interrupt(cause) or _error_code(cause) is None:
                raise
            raise TunnelSwitchError("TUNNEL_SWITCH_FAILED", cause_code=reason,
                                    rollback_status="succeeded") from cause
        try:
            _assert_resources(plan, state)
            _assert_unit_argv(plan, new_argv)
            deadline = clock_fn() + poll_timeout
            current = _rollback_live(
                status_fn, old, observed_new,
                releases=releases, proc_root=proc_root,
                approved_interpreters=approved_interpreters, attempts=attempts,
                deadline=deadline, clock_fn=clock_fn, sleep_fn=sleep_fn)
            old_interpreter = _approved_interpreter(
                Path(old["interpreter"]), approved_interpreters)
            old_runtime = _verify_runtime(Path(old["server"]).parent, releases)
            old_admission = _verify_workspace_launch(
                old_runtime["runtime"], authority_dir,
            )
            old_command = _workspace_command(
                old_admission, old_runtime["runtime"], old_interpreter,
                authority_dir, controller_dir,
            )
            old_argv = _write_target(plan, state, old_command)
            _assert_resources(plan, state)
            _run(command_fn, ["systemctl", "--user", "daemon-reload"])
            _assert_resources(plan, state)
            _assert_unit_argv(plan, old_argv)
            if current:
                _run(command_fn, ["systemctl", "--user", "stop", SERVICE])
                deadline = clock_fn() + poll_timeout
                _wait_stopped(current, proc_root, attempts, sleep_fn,
                              deadline, clock_fn)
            _assert_resources(plan, state)
            _assert_unit_argv(plan, old_argv)
            if old_admission["authority_active"]:
                _transition_workspace_launch(old_runtime["runtime"], authority_dir)
            else:
                _verify_workspace_launch(old_runtime["runtime"], authority_dir)
            _run(command_fn, ["systemctl", "--user", "start", SERVICE])
            deadline = clock_fn() + poll_timeout
            _wait_runtime(status_fn, old_runtime["snapshot"], old_interpreter,
                          releases, proc_root, approved_interpreters, attempts,
                          sleep_fn, deadline, clock_fn)
        except BaseException as rollback_error:
            if _is_interrupt(rollback_error):
                raise
            raise TunnelSwitchError("TUNNEL_ROLLBACK_FAILED", cause_code=reason,
                                    rollback_status="failed") from rollback_error
        if _is_interrupt(cause) or _error_code(cause) is None:
            raise
        raise TunnelSwitchError("TUNNEL_SWITCH_FAILED", cause_code=reason,
                                rollback_status="succeeded") from cause
