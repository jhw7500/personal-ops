"""Bounded, optional switch of the existing personal-ops-admin systemd user unit.

Never installs a new service, changes listening interfaces or reads tokens.
Only an existing Python admin_server.py ExecStart can be replaced; original
flags are preserved exactly. Failure attempts rollback of the single override.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import time
import urllib.request

from workspace_control import (DEFAULT_CONTROLLER_DIR, WorkspaceControlError,
                               transition_release, verify_controller, verify_launch)

SERVICE = "personal-ops-admin.service"
ALLOWED_ARGS = {"--port", "--config", "--public-origin", "--lan-host"}


class AdminSwitchError(RuntimeError):
    def __init__(self, code, cause_code=None, rollback_status=None):
        self.code = code
        self.cause_code = cause_code
        self.rollback_status = rollback_status
        super().__init__(code if cause_code is None else f"{code}:{cause_code}")


def _verify_workspace_launch(runtime, authority_dir=None):
    try:
        return verify_launch(runtime, authority_dir)
    except WorkspaceControlError as exc:
        raise AdminSwitchError(exc.code) from exc


def _transition_workspace_launch(runtime, authority_dir=None):
    try:
        return transition_release(runtime, authority_dir)
    except WorkspaceControlError as exc:
        raise AdminSwitchError(exc.code) from exc


def _workspace_admin_command(admission, runtime, interpreter, authority_dir,
                             application_args, controller_dir=None):
    if not admission["authority_active"]:
        return [str(interpreter), str(runtime / "admin_server.py"), *application_args]
    try:
        controller = verify_controller(controller_dir or DEFAULT_CONTROLLER_DIR)
    except WorkspaceControlError as exc:
        raise AdminSwitchError(exc.code) from exc
    return [
        str(interpreter), controller["launcher"],
        "--authority-dir", admission["authority_dir"],
        "--release", str(runtime), "--interpreter", str(interpreter),
        "--entrypoint", "admin_server.py", "--", *application_args,
    ]


def _run(argv, *, stdout=subprocess.DEVNULL):
    from rollout_diagnostics import run_command
    result = run_command(argv, stage="admin_systemd_command", timeout=18,
                         capture=stdout == subprocess.PIPE, max_output=256 * 1024)
    if result.returncode:
        raise AdminSwitchError("ADMIN_SYSTEMD_COMMAND_FAILED")
    return result.stdout


def _safe_owned_directory(path, mode):
    try:
        info = path.lstat()
    except OSError as exc:
        raise AdminSwitchError("UNSAFE_ADMIN_PATH") from exc
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
        raise AdminSwitchError("UNSAFE_ADMIN_PATH")


def _safe_interpreter(path):
    if not path.is_absolute() or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", path.name):
        return False
    if path.parent in (Path("/usr/bin"), Path("/usr/local/bin")):
        try:
            target = path.resolve(strict=True)
            info = target.stat()
        except OSError:
            return False
        return (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and not stat.S_IMODE(info.st_mode) & 0o022)
    expected = Path.home() / ".local/share/uv/tools/serena-agent/bin/python"
    if path != expected:
        return False
    uv_root = Path.home() / ".local/share/uv"
    for parent in (Path.home() / ".local/share", uv_root, uv_root / "tools",
                   uv_root / "tools/serena-agent", expected.parent):
        try:
            info = parent.lstat()
        except OSError:
            return False
        if (not stat.S_ISDIR(info.st_mode) or parent.is_symlink()
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
            return False
    try:
        link = expected.lstat()
        target = expected.resolve(strict=True)
        target_info = target.stat()
        python_root = (uv_root / "python").resolve(strict=True)
    except OSError:
        return False
    return (link.st_uid == os.getuid()
            and (stat.S_ISLNK(link.st_mode) or stat.S_ISREG(link.st_mode))
            and target.is_relative_to(python_root)
            and stat.S_ISREG(target_info.st_mode)
            and target_info.st_uid == os.getuid()
            and not stat.S_IMODE(target_info.st_mode) & 0o022
            and bool(stat.S_IMODE(target_info.st_mode) & 0o100))


def _safe_script(path, root, releases):
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return (path.is_absolute() and path.name == "admin_server.py"
            and stat.S_ISREG(info.st_mode) and not path.is_symlink()
            and info.st_uid == os.getuid()
            and (resolved.is_relative_to(root.resolve())
                 or resolved.is_relative_to(releases.resolve())))


def parse_execstart(unit_text, root, releases):
    from admin_server import validate_lan_host, validate_public_origin
    section = ""
    commands = []
    for line in unit_text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section == "[Service]" and line.startswith("ExecStart="):
            value = line.split("=", 1)[1].strip()
            if not value:
                commands = []
            else:
                commands.append(value)
    if len(commands) != 1 or any(marker in commands[0] for marker in ("$", "%", "\\", "\n")):
        raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT")
    try:
        argv = shlex.split(commands[0])
    except ValueError as exc:
        raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT") from exc
    if len(argv) < 2 or not _safe_interpreter(Path(argv[0])):
        raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT")
    if Path(argv[1]).name == "workspace-launcher.py":
        if (len(argv) < 11 or argv[2] != "--authority-dir"
                or argv[4] != "--release" or argv[6] != "--interpreter"
                or Path(argv[7]) != Path(argv[0])
                or argv[8:11] != ["--entrypoint", "admin_server.py", "--"]):
            raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT")
        try:
            controller = verify_controller(Path(argv[1]).parent)
            if controller["launcher"] != argv[1]:
                raise WorkspaceControlError("UNSAFE_WORKSPACE_CONTROLLER")
            admission = verify_launch(argv[5], argv[3])
        except WorkspaceControlError as exc:
            raise AdminSwitchError(exc.code) from exc
        if not admission["authority_active"]:
            raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT")
        argv = [argv[0], str(Path(argv[5]) / "admin_server.py"), *argv[11:]]
    script = Path(argv[1])
    if not _safe_script(script, root, releases):
        raise AdminSwitchError("UNRECOGNIZED_ADMIN_UNIT")
    if len(argv[2:]) % 2:
        raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS")
    port = 8765
    for index in range(2, len(argv), 2):
        flag, value = argv[index:index + 2]
        if flag not in ALLOWED_ARGS or not value:
            raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS")
        if flag == "--port":
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
                raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS")
            port = int(value)
        elif flag == "--public-origin":
            try:
                validate_public_origin(value)
            except (TypeError, ValueError) as exc:
                raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS") from exc
        elif flag == "--lan-host":
            try:
                validate_lan_host(value)
            except (TypeError, ValueError) as exc:
                raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS") from exc
        elif flag == "--config" and (not Path(value).is_absolute()
                                      or not Path(value).resolve().is_relative_to(
                                          Path.home() / ".config/chatgpt-coding-mcp")):
            raise AdminSwitchError("UNRECOGNIZED_ADMIN_FLAGS")
    return argv, port


def _read_override(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise AdminSwitchError("UNSAFE_ADMIN_OVERRIDE") from exc
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
            or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1):
        raise AdminSwitchError("UNSAFE_ADMIN_OVERRIDE")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise AdminSwitchError("UNSAFE_ADMIN_OVERRIDE") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode)):
            raise AdminSwitchError("ADMIN_OVERRIDE_CHANGED")
        data = stream.read(131073)
    if len(data) > 131072:
        raise AdminSwitchError("UNSAFE_ADMIN_OVERRIDE")
    stamp = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    return data, stamp


def _current_stamp(path):
    return _read_override(path)[1]


def _atomic_override(path, data, expected_stamp):
    if _current_stamp(path) != expected_stamp:
        raise AdminSwitchError("ADMIN_OVERRIDE_CHANGED")
    temporary = path.parent / (".20-runtime." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _current_stamp(path) != expected_stamp:
            raise AdminSwitchError("ADMIN_OVERRIDE_CHANGED")
        os.replace(temporary, path)
        return _current_stamp(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_override(path, expected_stamp):
    if _current_stamp(path) != expected_stamp:
        raise AdminSwitchError("ADMIN_OVERRIDE_CHANGED")
    path.unlink()


def _process_argv(pid):
    if not isinstance(pid, int) or pid < 1:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE")
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE") from exc
    if not command or len(command) > 131072:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE")
    try:
        return [part.decode("utf8", errors="strict")
                for part in command.rstrip(b"\0").split(b"\0")]
    except UnicodeDecodeError as exc:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE") from exc


def _process_start(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        close = raw.rfind(")")
        fields = raw[close + 2:].split()
        value = int(fields[19])
    except (OSError, ValueError, IndexError) as exc:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE") from exc
    if close < 1 or value < 1:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE")
    return value


def _main_pid(command_fn):
    raw = command_fn(["systemctl", "--user", "show", SERVICE,
                      "--property", "MainPID", "--value"], stdout=subprocess.PIPE)
    if not isinstance(raw, str):
        raw = raw.decode("ascii", errors="strict")
    value = raw.strip()
    if not value.isdecimal() or int(value) < 1:
        raise AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE")
    return int(value)


def _service_state(command_fn):
    raw = command_fn([
        "systemctl", "--user", "show", SERVICE,
        "--property", "ActiveState", "--property", "SubState",
        "--property", "MainPID", "--property", "Job",
    ], stdout=subprocess.PIPE)
    try:
        if not isinstance(raw, str):
            raw = raw.decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise AdminSwitchError("ADMIN_SERVICE_STATE_UNAVAILABLE") from exc
    expected = {"ActiveState", "SubState", "MainPID", "Job"}
    properties = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if (not separator or key not in expected or key in properties
                or (not value and key != "Job")):
            raise AdminSwitchError("ADMIN_SERVICE_STATE_UNAVAILABLE")
        properties[key] = value
    if (set(properties) != expected
            or not properties["MainPID"].isdecimal()):
        raise AdminSwitchError("ADMIN_SERVICE_STATE_UNAVAILABLE")
    return (properties["ActiveState"], properties["SubState"],
            int(properties["MainPID"]), properties["Job"])


def _cause_code(cause):
    if isinstance(cause, AdminSwitchError):
        return cause.code
    if isinstance(cause, KeyboardInterrupt):
        return "ROLLOUT_INTERRUPTED"
    if (isinstance(cause, RuntimeError) and len(cause.args) == 1
            and cause.args[0] == "ROLLOUT_TERMINATED"):
        return "ROLLOUT_TERMINATED"
    return "ADMIN_SWITCH_UNEXPECTED"


def _is_interruption(cause):
    return (isinstance(cause, (KeyboardInterrupt, SystemExit))
            or (isinstance(cause, RuntimeError) and len(cause.args) == 1
                and cause.args[0] == "ROLLOUT_TERMINATED"))


def _override_path():
    return (Path.home() / ".config/systemd/user" / (SERVICE + ".d")
            / "20-runtime.conf")


def _validate_override_shape(path):
    data, stamp = _read_override(path)
    if data is None:
        return data, stamp
    try:
        lines = [line.strip() for line in data.decode("utf8", errors="strict").splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", ";"))]
    except UnicodeError as exc:
        raise AdminSwitchError("UNSUPPORTED_ADMIN_OVERRIDE") from exc
    if (len(lines) != 3 or lines[0] != "[Service]" or lines[1] != "ExecStart="
            or not lines[2].startswith("ExecStart=") or lines[2] == "ExecStart="):
        raise AdminSwitchError("UNSUPPORTED_ADMIN_OVERRIDE")
    return data, stamp


def _read_unit(unit_fn, command_fn):
    read_unit = unit_fn or (lambda: command_fn(
        ["systemctl", "--user", "cat", SERVICE], stdout=subprocess.PIPE))
    unit = read_unit()
    if not isinstance(unit, str):
        unit = unit.decode("utf8", errors="strict")
    return unit


def _unit_context(unit):
    section = ""
    context = []
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif not line.startswith("ExecStart="):
            context.append((section, line))
    return tuple(context)


def _process_snapshot(command_fn, process_argv_fn, process_start_fn):
    pid = _main_pid(command_fn)
    return (pid, process_start_fn(pid)), process_argv_fn(pid)


def _assert_process(command_fn, process_argv_fn, process_start_fn, allowed):
    identity, argv = _process_snapshot(command_fn, process_argv_fn, process_start_fn)
    if identity not in allowed or argv != allowed[identity]:
        raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
    return identity


def _wait_process_exec(command_fn, process_argv_fn, process_start_fn,
                       launch_argv, expected_argv, allowed_transient, sleep_fn,
                       attempts=12):
    launch_identity = None
    for attempt in range(attempts):
        identity, current_argv = _process_snapshot(
            command_fn, process_argv_fn, process_start_fn)
        if current_argv == expected_argv:
            if launch_identity is not None and identity != launch_identity:
                raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
            return identity
        if current_argv == launch_argv:
            if launch_identity is None:
                launch_identity = identity
            elif identity != launch_identity:
                raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
        elif (launch_identity is not None or identity not in allowed_transient
              or current_argv != allowed_transient[identity]):
            raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
        if attempt < attempts - 1:
            sleep_fn(0.5)
    raise AdminSwitchError("ADMIN_MAINPID_MISMATCH")


def _assert_effective_unit(unit_fn, command_fn, expected):
    if _read_unit(unit_fn, command_fn) != expected:
        raise AdminSwitchError("ADMIN_UNIT_CHANGED")


def _assert_override(path, expected_stamp):
    if _current_stamp(path) != expected_stamp:
        raise AdminSwitchError("ADMIN_OVERRIDE_CHANGED")


def _assert_stably_inactive(unit_fn, command_fn, expected_unit,
                            override, expected_stamp):
    states = []
    for _ in range(2):
        _assert_override(override, expected_stamp)
        _assert_effective_unit(unit_fn, command_fn, expected_unit)
        state = _service_state(command_fn)
        if (state[0] not in {"inactive", "failed"}
                or state[1] not in {"dead", "failed"}
                or state[2] != 0 or state[3] not in {"", "0"}):
            raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
        states.append(state)
    if states[0] != states[1]:
        raise AdminSwitchError("ADMIN_SERVICE_CHANGED")


def _poll_ui(getter, url, marker, *, attempts=12, sleep_fn=time.sleep):
    last = AdminSwitchError("ADMIN_UI_UNAVAILABLE")
    for attempt in range(attempts):
        try:
            with getter(url, timeout=4) as response:
                body = response.read(120000)
                if response.status == 200 and marker in body:
                    return
                last = AdminSwitchError("ADMIN_UI_NOT_UPDATED")
        except (OSError, ValueError, UnicodeError):
            last = AdminSwitchError("ADMIN_UI_UNAVAILABLE")
        if attempt < attempts - 1:
            sleep_fn(0.5)
    raise last


def _rollback_admin(*, override, previous, deployed_stamp, old_unit, deployed_unit,
                    old_argv, port, command_fn, unit_fn, process_argv_fn,
                    process_start_fn, getter, sleep_fn, allowed_processes,
                    authority_dir, controller_dir):
    old_runtime = Path(old_argv[1]).parent
    old_admission = _verify_workspace_launch(old_runtime, authority_dir)
    old_launch_argv = _workspace_admin_command(
        old_admission, old_runtime, Path(old_argv[0]), authority_dir,
        old_argv[2:], controller_dir,
    )
    _assert_override(override, deployed_stamp)
    _assert_effective_unit(unit_fn, command_fn, deployed_unit)
    inactive_recovery = False
    try:
        identity, current_argv = _process_snapshot(
            command_fn, process_argv_fn, process_start_fn)
        if identity not in allowed_processes or current_argv != allowed_processes[identity]:
            raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
    except AdminSwitchError as cause:
        if cause.code != "ADMIN_MAINPID_UNAVAILABLE":
            raise
        _assert_stably_inactive(unit_fn, command_fn, deployed_unit,
                                override, deployed_stamp)
        inactive_recovery = True
    if previous is None:
        _remove_override(override, deployed_stamp)
        restored_stamp = None
    else:
        restored_stamp = _atomic_override(override, previous, deployed_stamp)
    _assert_override(override, restored_stamp)
    pre_reload_unit = _read_unit(unit_fn, command_fn)
    if pre_reload_unit not in {deployed_unit, old_unit}:
        raise AdminSwitchError("ADMIN_UNIT_CHANGED")
    _assert_effective_unit(unit_fn, command_fn, pre_reload_unit)
    if inactive_recovery:
        _assert_stably_inactive(unit_fn, command_fn, pre_reload_unit,
                                override, restored_stamp)
    else:
        _assert_process(command_fn, process_argv_fn, process_start_fn, allowed_processes)
    _assert_override(override, restored_stamp)
    command_fn(["systemctl", "--user", "daemon-reload"])
    _assert_override(override, restored_stamp)
    _assert_effective_unit(unit_fn, command_fn, old_unit)
    if inactive_recovery:
        _assert_stably_inactive(unit_fn, command_fn, old_unit,
                                override, restored_stamp)
    else:
        _assert_process(command_fn, process_argv_fn, process_start_fn, allowed_processes)
    _assert_override(override, restored_stamp)
    if old_admission["authority_active"]:
        _transition_workspace_launch(old_runtime, authority_dir)
    else:
        _verify_workspace_launch(old_runtime, authority_dir)
    command_fn(["systemctl", "--user", "restart", SERVICE])
    _assert_override(override, restored_stamp)
    _assert_effective_unit(unit_fn, command_fn, old_unit)
    try:
        restored_identity = _wait_process_exec(
            command_fn, process_argv_fn, process_start_fn,
            old_launch_argv, old_argv, allowed_processes, sleep_fn,
        )
    except AdminSwitchError as exc:
        if exc.code in {"ADMIN_MAINPID_MISMATCH", "ADMIN_SERVICE_CHANGED"}:
            raise AdminSwitchError("ADMIN_ROLLBACK_MAINPID_MISMATCH") from exc
        raise
    _assert_override(override, restored_stamp)
    command_fn(["systemctl", "--user", "is-active", "--quiet", SERVICE])
    _assert_override(override, restored_stamp)
    _assert_effective_unit(unit_fn, command_fn, old_unit)
    verified_identity, verified_argv = _process_snapshot(
        command_fn, process_argv_fn, process_start_fn)
    if verified_identity != restored_identity or verified_argv != old_argv:
        raise AdminSwitchError("ADMIN_ROLLBACK_MAINPID_MISMATCH")
    try:
        _poll_ui(getter, f"http://127.0.0.1:{port}/", b'id="login-form"',
                 sleep_fn=sleep_fn)
    except AdminSwitchError as cause:
        raise AdminSwitchError("ADMIN_ROLLBACK_UI_NOT_READY") from cause
    _assert_override(override, restored_stamp)
    _assert_effective_unit(unit_fn, command_fn, old_unit)
    final_identity, final_argv = _process_snapshot(
        command_fn, process_argv_fn, process_start_fn)
    if final_identity != restored_identity or final_argv != old_argv:
        raise AdminSwitchError("ADMIN_ROLLBACK_MAINPID_MISMATCH")


def preflight_admin(*, unit_fn=None, command_fn=_run, root=None, releases=None,
                    process_argv_fn=_process_argv, process_start_fn=_process_start):
    """Validate the existing unit without writing an override or restarting it."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    releases = (Path(releases) if releases is not None else
                Path.home() / ".local/lib/chatgpt-coding-mcp/releases")
    _validate_override_shape(_override_path())
    unit = _read_unit(unit_fn, command_fn)
    argv, port = parse_execstart(unit, root, releases)
    _, live_argv = _process_snapshot(command_fn, process_argv_fn, process_start_fn)
    if live_argv != argv:
        raise AdminSwitchError("ADMIN_MAINPID_MISMATCH")
    return argv, port


def switch_admin(new_runtime, *, unit_fn=None, command_fn=_run, urlopen_fn=None,
                 process_argv_fn=_process_argv, process_start_fn=_process_start,
                 sleep_fn=time.sleep, authority_dir=None, controller_dir=None):
    """Switch only a recognizable existing user unit to the new immutable release."""
    root = Path(__file__).resolve().parent
    releases = Path.home() / ".local/lib/chatgpt-coding-mcp/releases"
    runtime_path = Path(new_runtime).absolute()
    script = runtime_path / "admin_server.py"
    if (runtime_path.parent != releases or not re.fullmatch(r"[0-9a-f]{20}", runtime_path.name)
            or runtime_path.is_symlink() or script.is_symlink()):
        raise AdminSwitchError("UNSAFE_NEW_RUNTIME")
    try:
        _safe_owned_directory(releases, 0o700)
        _safe_owned_directory(runtime_path, 0o700)
        script_info = script.lstat()
    except AdminSwitchError as exc:
        raise AdminSwitchError("UNSAFE_NEW_RUNTIME") from exc
    except OSError as exc:
        raise AdminSwitchError("UNSAFE_NEW_RUNTIME") from exc
    if (not stat.S_ISREG(script_info.st_mode) or script_info.st_uid != os.getuid()
            or stat.S_IMODE(script_info.st_mode) != 0o600 or script_info.st_nlink != 1):
        raise AdminSwitchError("UNSAFE_NEW_RUNTIME")
    target_admission = _verify_workspace_launch(runtime_path, authority_dir)
    previous, previous_stamp = _validate_override_shape(_override_path())
    old_unit = _read_unit(unit_fn, command_fn)
    old_argv, port = parse_execstart(old_unit, root, releases)
    _assert_override(_override_path(), previous_stamp)
    old_identity, actual_old_argv = _process_snapshot(
        command_fn, process_argv_fn, process_start_fn)
    if actual_old_argv != old_argv:
        raise AdminSwitchError("ADMIN_MAINPID_MISMATCH")
    _assert_effective_unit(unit_fn, command_fn, old_unit)
    argv = list(old_argv)
    argv[1] = str(script)
    launch_argv = _workspace_admin_command(
        target_admission, runtime_path, Path(argv[0]), authority_dir, argv[2:],
        controller_dir,
    )
    override = _override_path()
    override.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = override.parent
    try:
        _safe_owned_directory(parent, 0o700)
    except AdminSwitchError:
        raise AdminSwitchError("UNSAFE_ADMIN_OVERRIDE")
    _assert_override(override, previous_stamp)
    value = ("[Service]\nExecStart=\nExecStart=" + shlex.join(launch_argv) + "\n").encode()
    deployed_stamp = _atomic_override(override, value, previous_stamp)
    getter = urlopen_fn or urllib.request.urlopen
    deployed_unit = None
    new_identity = None
    try:
        pre_reload_unit = _read_unit(unit_fn, command_fn)
        deployed_unit = pre_reload_unit
        if pre_reload_unit != old_unit:
            pre_reload_argv, pre_reload_port = parse_execstart(
                pre_reload_unit, root, releases)
            if (pre_reload_argv != argv or pre_reload_port != port
                    or _unit_context(pre_reload_unit) != _unit_context(old_unit)):
                raise AdminSwitchError("ADMIN_UNIT_CHANGED")
        _assert_override(override, deployed_stamp)
        _assert_effective_unit(unit_fn, command_fn, pre_reload_unit)
        _assert_process(command_fn, process_argv_fn, process_start_fn,
                        {old_identity: old_argv})
        command_fn(["systemctl", "--user", "daemon-reload"])
        _assert_override(override, deployed_stamp)
        deployed_unit = _read_unit(unit_fn, command_fn)
        deployed_argv, deployed_port = parse_execstart(deployed_unit, root, releases)
        if (deployed_argv != argv or deployed_port != port
                or _unit_context(deployed_unit) != _unit_context(old_unit)):
            raise AdminSwitchError("ADMIN_UNIT_CHANGED")
        _assert_effective_unit(unit_fn, command_fn, deployed_unit)
        _assert_process(command_fn, process_argv_fn, process_start_fn,
                        {old_identity: old_argv})
        current_admission = _verify_workspace_launch(runtime_path, authority_dir)
        if (_workspace_admin_command(
                current_admission, runtime_path, Path(argv[0]), authority_dir,
                argv[2:], controller_dir) != launch_argv):
            raise AdminSwitchError("WORKSPACE_AUTHORITY_CHANGED")
        if current_admission["authority_active"]:
            _transition_workspace_launch(runtime_path, authority_dir)
        command_fn(["systemctl", "--user", "restart", SERVICE])
        _assert_override(override, deployed_stamp)
        _assert_effective_unit(unit_fn, command_fn, deployed_unit)
        identity, current_argv = _process_snapshot(
            command_fn, process_argv_fn, process_start_fn)
        launch_identity = None
        if current_argv == argv:
            new_identity = identity
        elif current_argv == launch_argv and current_argv != argv:
            launch_identity = identity
        elif identity != old_identity or current_argv != old_argv:
            raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
        _assert_effective_unit(unit_fn, command_fn, deployed_unit)
        _assert_override(override, deployed_stamp)
        command_fn(["systemctl", "--user", "is-active", "--quiet", SERVICE])
        last_error = AdminSwitchError("ADMIN_MAINPID_UNAVAILABLE")
        for attempt in range(12):
            _assert_override(override, deployed_stamp)
            _assert_effective_unit(unit_fn, command_fn, deployed_unit)
            try:
                identity, current_argv = _process_snapshot(
                    command_fn, process_argv_fn, process_start_fn)
                if current_argv == launch_argv and current_argv != argv:
                    if new_identity is not None:
                        raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                    if launch_identity is None:
                        launch_identity = identity
                    elif identity != launch_identity:
                        raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                    raise AdminSwitchError("ADMIN_MAINPID_MISMATCH")
                if current_argv != argv:
                    if (launch_identity is not None or identity != old_identity
                            or current_argv != old_argv):
                        raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                    raise AdminSwitchError("ADMIN_MAINPID_MISMATCH")
                if launch_identity is not None and identity != launch_identity:
                    raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                if new_identity is None:
                    new_identity = identity
                elif identity != new_identity:
                    raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                with getter(f"http://127.0.0.1:{port}/", timeout=4) as response:
                    body = response.read(120000)
                    if response.status != 200 or b'id="host-approval"' not in body:
                        raise AdminSwitchError("ADMIN_UI_NOT_UPDATED")
                _assert_override(override, deployed_stamp)
                _assert_effective_unit(unit_fn, command_fn, deployed_unit)
                verified_identity, verified_argv = _process_snapshot(
                    command_fn, process_argv_fn, process_start_fn)
                if verified_identity != new_identity or verified_argv != argv:
                    raise AdminSwitchError("ADMIN_SERVICE_CHANGED")
                return "admin_ready"
            except AdminSwitchError as exc:
                if exc.code in ("ADMIN_OVERRIDE_CHANGED", "ADMIN_UNIT_CHANGED",
                                "ADMIN_SERVICE_CHANGED"):
                    raise
                last_error = exc
            except (OSError, ValueError, UnicodeError):
                last_error = AdminSwitchError("ADMIN_UI_UNAVAILABLE")
            if attempt < 11:
                sleep_fn(0.5)
        raise last_error
    except BaseException as cause:
        try:
            if deployed_unit is None:
                raise AdminSwitchError("ADMIN_UNIT_CHANGED")
            allowed = {old_identity: old_argv}
            if new_identity is not None:
                allowed[new_identity] = argv
            _rollback_admin(
                override=override, previous=previous, deployed_stamp=deployed_stamp,
                old_unit=old_unit, deployed_unit=deployed_unit, old_argv=old_argv,
                port=port, command_fn=command_fn, unit_fn=unit_fn,
                process_argv_fn=process_argv_fn, process_start_fn=process_start_fn,
                getter=getter, sleep_fn=sleep_fn, allowed_processes=allowed,
                authority_dir=authority_dir, controller_dir=controller_dir)
        except BaseException as rollback_error:
            if _is_interruption(cause):
                cause.rollback_status = "failed"
                cause.cause_code = _cause_code(rollback_error)
                raise cause from rollback_error
            raise AdminSwitchError("ADMIN_ROLLBACK_FAILED", _cause_code(rollback_error),
                                   rollback_status="failed") from rollback_error
        if _is_interruption(cause):
            cause.rollback_status = "succeeded"
            raise
        raise AdminSwitchError("ADMIN_SWITCH_FAILED", _cause_code(cause),
                               rollback_status="succeeded") from cause
