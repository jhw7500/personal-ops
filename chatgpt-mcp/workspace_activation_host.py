"""Evidence-bound host effects for first Workspace authority activation.

This adapter owns exactly the two established personal-ops user services and
their three runtime-selection files.  It deliberately separates stopping and
wiring from authority publication: callers must commit authority before
``start`` can launch the stable controller.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import urllib.request

from admin_server import validate_lan_host, validate_public_origin
from rollout_admin import ALLOWED_ARGS
from rollout_tunnel import (
    SERVICE as TUNNEL_SERVICE,
    TunnelSwitchError,
    _approved_interpreter,
    _cas_write,
    _owned_directory,
    _owned_file,
    _owned_tunnel_client,
    _parse_profile,
    _parse_unit,
    _target_bytes,
    _unit_context,
    _verify_runtime,
    freeze_live_runtime,
)
from workspace_control import (
    MANDATORY_RELEASE_FILES,
    WorkspaceControlError,
    read_activation_binding,
    verify_controller,
    verify_launch,
)


ADMIN_SERVICE = "personal-ops-admin.service"
SERVICES = (TUNNEL_SERVICE, ADMIN_SERVICE)
_HEX20 = re.compile(r"[0-9a-f]{20}\Z")
_WORKER_KINDS = frozenset({"job", "agent", "command", "host", "registered", "rollout"})
_STARTUP_TIMEOUT_SECONDS = 5.0
_STARTUP_POLL_SECONDS = 0.05


class ActivationHostError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _read_bounded(path: Path, limit: int = 256 * 1024) -> bytes:
    try:
        _owned_file(path)
        data = path.read_bytes()
    except (OSError, TunnelSwitchError) as exc:
        raise ActivationHostError("UNSAFE_HOST_CONFIGURATION") from exc
    if len(data) > limit:
        raise ActivationHostError("UNSAFE_HOST_CONFIGURATION")
    return data


def _optional_file(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_bounded(path)


def _digest_or_none(data: bytes | None) -> str | None:
    return None if data is None else _sha256(data)


def _process(root: Path, pid: int, boot_id: str) -> dict:
    folder = root / str(pid)
    try:
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ActivationHostError("UNOWNED_PROCESS")
        line = (folder / "stat").read_text()
        marker = line.rfind(") ")
        fields = line[marker + 2:].split()
        if marker < 0 or len(fields) < 20:
            raise ValueError
        ppid, ticks = int(fields[1]), int(fields[19])
        raw = (folder / "cmdline").read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError
        argv = [os.fsdecode(item) for item in raw.rstrip(b"\0").split(b"\0") if item]
        groups = []
        for source in (folder / "cgroup").read_text().splitlines():
            _, separator, value = source.rpartition(":")
            if not separator or not value.startswith("/"):
                raise ValueError
            groups.append(value)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ActivationHostError("PROCESS_IDENTITY_UNAVAILABLE") from exc
    if pid <= 1 or ticks <= 0 or not argv or not groups:
        raise ActivationHostError("PROCESS_IDENTITY_UNAVAILABLE")
    return {"pid": pid, "ppid": ppid, "start_ticks": ticks, "boot_id": boot_id,
            "argv": argv, "cgroups": groups}


def _pin_process(process: dict) -> dict:
    return {
        "pid": process["pid"], "ppid": process["ppid"],
        "start_ticks": process["start_ticks"], "boot_id": process["boot_id"],
        "argv_sha256": _json_digest(process["argv"]),
        "cgroups": list(process["cgroups"]),
    }


def _matches_process_pin(process: dict, pinned: dict) -> bool:
    return _pin_process(process) == pinned


def _exec_identity(process: dict) -> dict:
    return {
        "pid": process["pid"], "ppid": process["ppid"],
        "start_ticks": process["start_ticks"], "boot_id": process["boot_id"],
        "cgroups": list(process["cgroups"]),
    }


def _systemd_command(argv, *, stdout=subprocess.PIPE):
    result = subprocess.run(argv, check=False, stdout=stdout, stderr=subprocess.DEVNULL,
                            timeout=20)
    if result.returncode:
        raise ActivationHostError("SYSTEMD_COMMAND_FAILED")
    return result.stdout or b""


def _unit_state(command_fn, unit: str) -> dict:
    raw = command_fn([
        "systemctl", "--user", "show", unit,
        "--property", "ActiveState", "--property", "SubState",
        "--property", "MainPID", "--property", "Job", "--property", "ControlGroup",
    ], stdout=subprocess.PIPE)
    if not isinstance(raw, str):
        raw = raw.decode("ascii", errors="strict")
    expected = {"ActiveState", "SubState", "MainPID", "Job", "ControlGroup"}
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in values:
            raise ActivationHostError("SERVICE_STATE_UNAVAILABLE")
        values[key] = value
    if set(values) != expected or not values["MainPID"].isdecimal():
        raise ActivationHostError("SERVICE_STATE_UNAVAILABLE")
    return {"active_state": values["ActiveState"], "sub_state": values["SubState"],
            "main_pid": int(values["MainPID"]), "job": values["Job"],
            "control_group": values["ControlGroup"]}


def _effective_execstart(unit_text: str) -> list[str]:
    section = ""
    commands = []
    for source in unit_text.splitlines():
        line = source.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Service]" and line.startswith("ExecStart="):
            value = line.partition("=")[2].strip()
            commands = [] if not value else [*commands, value]
    if len(commands) != 1 or any(mark in commands[0] for mark in ("$", "%", "\\", "\n")):
        raise ActivationHostError("UNRECOGNIZED_ADMIN_UNIT")
    try:
        return shlex.split(commands[0])
    except ValueError as exc:
        raise ActivationHostError("UNRECOGNIZED_ADMIN_UNIT") from exc


def _admin_command(unit_text: str, interpreter: Path, source_root: Path,
                   releases: Path, config_root: Path) -> tuple[list[str], int]:
    argv = _effective_execstart(unit_text)
    if len(argv) < 2 or Path(argv[0]) != interpreter:
        raise ActivationHostError("UNRECOGNIZED_ADMIN_UNIT")
    script = Path(argv[1])
    try:
        resolved = script.resolve(strict=True)
    except OSError as exc:
        raise ActivationHostError("UNRECOGNIZED_ADMIN_UNIT") from exc
    if (script.name != "admin_server.py" or script.is_symlink()
            or not (resolved.is_relative_to(source_root.resolve())
                    or resolved.is_relative_to(releases.resolve()))):
        raise ActivationHostError("UNRECOGNIZED_ADMIN_UNIT")
    if len(argv[2:]) % 2:
        raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS")
    port = 8765
    seen = set()
    for index in range(2, len(argv), 2):
        flag, value = argv[index:index + 2]
        if flag in seen or flag not in ALLOWED_ARGS or not value:
            raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS")
        seen.add(flag)
        if flag == "--port":
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
                raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS")
            port = int(value)
        elif flag == "--config":
            config = Path(value)
            if (not config.is_absolute()
                    or not config.resolve(strict=False).is_relative_to(config_root)):
                raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS")
        elif flag == "--public-origin":
            try:
                validate_public_origin(value)
            except (TypeError, ValueError) as exc:
                raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS") from exc
        elif flag == "--lan-host":
            try:
                validate_lan_host(value)
            except (TypeError, ValueError) as exc:
                raise ActivationHostError("UNRECOGNIZED_ADMIN_FLAGS") from exc
    return argv, port


def _admin_health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=4) as response:
            body = response.read(120000)
            return response.status == 200 and b'id="login-form"' in body
    except (OSError, ValueError, UnicodeError):
        return False


def _launcher_command(controller: dict, authority: Path, release: Path,
                      interpreter: Path, entrypoint: str, tail=()) -> list[str]:
    command = [
        str(interpreter), controller["launcher"], "--authority-dir", str(authority),
        "--release", str(release), "--interpreter", str(interpreter),
        "--entrypoint", entrypoint,
    ]
    if tail:
        command.extend(["--", *tail])
    return command


class SystemdActivationHost:
    def __init__(self, *, releases, interpreter, profile_path, tunnel_override_path,
                 admin_override_path, client, tunnel_unit_fn, admin_unit_fn,
                 tunnel_status_fn, command_fn=_systemd_command, unit_state_fn=None,
                 process_root=Path("/proc"), boot_id_fn=None, worker_inventory_fn=None,
                 admin_health_fn=None, approved_interpreters=None, source_root=None,
                 sleep_fn=time.sleep, clock_fn=time.monotonic,
                 startup_timeout=_STARTUP_TIMEOUT_SECONDS):
        self.releases = Path(releases).absolute()
        self.interpreter = Path(interpreter).absolute()
        self.profile_path = Path(profile_path).absolute()
        self.tunnel_override_path = Path(tunnel_override_path).absolute()
        self.admin_override_path = Path(admin_override_path).absolute()
        self.client = Path(client).absolute()
        self.tunnel_unit_fn = tunnel_unit_fn
        self.admin_unit_fn = admin_unit_fn
        self.tunnel_status_fn = tunnel_status_fn
        self.command_fn = command_fn
        self.unit_state_fn = unit_state_fn or (lambda unit: _unit_state(command_fn, unit))
        self.process_root = Path(process_root)
        self.boot_id_fn = boot_id_fn or (lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        self.worker_inventory_fn = worker_inventory_fn
        self.admin_health_fn = admin_health_fn or _admin_health
        self.approved_interpreters = approved_interpreters
        self.source_root = Path(source_root or Path(__file__).resolve().parent).absolute()
        self.sleep_fn = sleep_fn
        self.clock_fn = clock_fn
        self.startup_timeout = startup_timeout
        self.config_root = self.profile_path.parent.parent / "chatgpt-coding-mcp"
        self.rollout_lock_path = (self.releases.parents[3]
                                  / ".local/state/chatgpt-coding-mcp/runtime-rollout-web"
                                  / "global-rollout.lock")
        if (self.releases.parts[-4:] != (".local", "lib", "chatgpt-coding-mcp", "releases")
                or self.profile_path.parts[-3:] != (
                    ".config", "tunnel-client", "personal-ops-coding.yaml")
                or self.tunnel_override_path.parts[-5:] != (
                    ".config", "systemd", "user", TUNNEL_SERVICE + ".d",
                    "20-runtime.conf")
                or self.admin_override_path.parts[-5:] != (
                    ".config", "systemd", "user", ADMIN_SERVICE + ".d",
                    "20-runtime.conf")
                or self.client.name != "tunnel-client"):
            raise ActivationHostError("NONCANONICAL_HOST_PATH")

    @staticmethod
    def _authority_active(authority: Path, release: Path) -> dict:
        try:
            return verify_launch(release, authority, enforcement_required=True)
        except WorkspaceControlError as exc:
            raise ActivationHostError(exc.code) from exc

    def _inventory(self) -> dict:
        if self.worker_inventory_fn is None:
            raise ActivationHostError("WORKER_INVENTORY_REQUIRED")
        value = self.worker_inventory_fn()
        if (not isinstance(value, dict)
                or set(value) != {"workers", "locked_leases", "observed_kinds"}):
            raise ActivationHostError("WORKER_INVENTORY_INVALID")
        if (not isinstance(value["observed_kinds"], list)
                or value["observed_kinds"] != sorted(_WORKER_KINDS)):
            raise ActivationHostError("WORKER_INVENTORY_INCOMPLETE")
        result = {}
        for key in ("workers", "locked_leases"):
            items = value[key]
            if not isinstance(items, list):
                raise ActivationHostError("WORKER_INVENTORY_INVALID")
            clean = []
            for item in items:
                if (not isinstance(item, dict) or set(item) != {"kind", "id"}
                        or item["kind"] not in _WORKER_KINDS
                        or not isinstance(item["id"], str) or not item["id"]):
                    raise ActivationHostError("WORKER_INVENTORY_INVALID")
                clean.append(dict(item))
            result[key] = clean
        result["observed_kinds"] = list(value["observed_kinds"])
        return result

    @staticmethod
    def _assert_no_work(inventory: dict) -> None:
        if inventory["workers"]:
            raise ActivationHostError("ACTIVE_WORKERS")
        if inventory["locked_leases"]:
            raise ActivationHostError("ACTIVE_LEASES")

    def _read_unit(self, function) -> str:
        raw = function()
        if isinstance(raw, bytes):
            raw = raw.decode("utf8", errors="strict")
        if not isinstance(raw, str) or len(raw.encode()) > 128 * 1024:
            raise ActivationHostError("UNRECOGNIZED_SERVICE_UNIT")
        return raw

    def _observe_main(self, state: dict, boot_id: str) -> dict | None:
        pid = state["main_pid"]
        if pid == 0:
            return None
        return _process(self.process_root, pid, boot_id)

    def _live_tunnel(self, boot_id: str) -> dict | None:
        try:
            status = self.tunnel_status_fn()
        except Exception as exc:
            raise ActivationHostError("TUNNEL_RUNTIME_STATUS_UNAVAILABLE") from exc
        if isinstance(status, dict) and status.get("process_running") is False:
            process = status.get("process")
            if process is not None and not isinstance(process, dict):
                raise ActivationHostError("TUNNEL_RUNTIME_STATUS_UNAVAILABLE")
            pid = process.get("pid") if isinstance(process, dict) else None
            if pid is not None:
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
                    raise ActivationHostError("TUNNEL_RUNTIME_STATUS_UNAVAILABLE")
                if (self.process_root / str(pid)).exists():
                    raise ActivationHostError("TUNNEL_RUNTIME_STILL_LIVE")
            return None
        if (not isinstance(status, dict)
                or any(status.get(key) is not True
                       for key in ("process_running", "healthy", "ready"))):
            raise ActivationHostError("TUNNEL_RUNTIME_STATUS_UNAVAILABLE")
        try:
            live = freeze_live_runtime(
                status, releases=self.releases, proc_root=self.process_root,
                approved_interpreters=self.approved_interpreters,
            )
        except TunnelSwitchError as exc:
            raise ActivationHostError(exc.code) from exc
        main = _process(self.process_root, live["tunnel_pid"], boot_id)
        if main["start_ticks"] != live["tunnel_start_ticks"]:
            raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
        children = []
        child_ids = [(live["mcp_pid"], live["mcp_start_ticks"])]
        if "codex_pid" in live:
            child_ids.append((live["codex_pid"], live["codex_start_ticks"]))
        for pid, ticks in child_ids:
            child = _process(self.process_root, pid, boot_id)
            if child["ppid"] != live["tunnel_pid"] or child["start_ticks"] != ticks:
                raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
            children.append(child)
        return {"live": live, "main": main, "children": children}

    @staticmethod
    def _lock_record_matches(line: str, info: os.stat_result) -> bool:
        fields = line.replace("lock:\t", "", 1).split()
        if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
            return False
        try:
            major, minor, inode = fields[5].split(":")
            return (fields[4] == str(os.getpid())
                    and int(major, 16) == os.major(info.st_dev)
                    and int(minor, 16) == os.minor(info.st_dev)
                    and int(inode) == info.st_ino)
        except (ValueError, IndexError):
            return False

    def _assert_rollout_lock(self, descriptor: int) -> None:
        try:
            path_info = self.rollout_lock_path.lstat()
            descriptor_info = os.fstat(descriptor)
            if (not stat.S_ISREG(path_info.st_mode)
                    or path_info.st_uid != os.getuid()
                    or stat.S_IMODE(path_info.st_mode) != 0o600
                    or path_info.st_nlink != 1
                    or (path_info.st_dev, path_info.st_ino)
                    != (descriptor_info.st_dev, descriptor_info.st_ino)):
                raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED")
            fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
            locks = Path("/proc/locks").read_text()
        except (OSError, UnicodeError) as exc:
            raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED") from exc
        if (not any(self._lock_record_matches(line, descriptor_info)
                    for line in fdinfo.splitlines() if line.startswith("lock:"))
                or not any(self._lock_record_matches(line, descriptor_info)
                           for line in locks.splitlines())):
            raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED")

    @contextmanager
    def _held_rollout_lock(self):
        descriptor = None
        try:
            expected = self.rollout_lock_path.lstat()
            if (not stat.S_ISREG(expected.st_mode) or expected.st_uid != os.getuid()
                    or stat.S_IMODE(expected.st_mode) != 0o600 or expected.st_nlink != 1):
                raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED")
            for entry in Path("/proc/self/fd").iterdir():
                if not entry.name.isdecimal():
                    continue
                candidate = int(entry.name)
                try:
                    info = os.fstat(candidate)
                    if ((info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
                            or not any(self._lock_record_matches(line, info)
                                       for line in Path(
                                           f"/proc/self/fdinfo/{candidate}").read_text().splitlines()
                                       if line.startswith("lock:"))):
                        continue
                    descriptor = os.dup(candidate)
                    break
                except (OSError, UnicodeError):
                    continue
            if descriptor is None:
                raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED")
            self._assert_rollout_lock(descriptor)
            yield descriptor
            self._assert_rollout_lock(descriptor)
        except OSError as exc:
            raise ActivationHostError("GLOBAL_ROLLOUT_LOCK_REQUIRED") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _release(self, path: Path, *, activation_host_required: bool) -> dict:
        try:
            result = _verify_runtime(path, self.releases)
        except TunnelSwitchError as exc:
            raise ActivationHostError(exc.code) from exc
        manifest = json.loads((path / "manifest.json").read_text())
        source = Path(__file__).read_bytes()
        if (not MANDATORY_RELEASE_FILES.issubset(manifest)
                or (activation_host_required
                    and manifest.get("workspace_activation_host.py") != _sha256(source))):
            raise ActivationHostError("ACTIVATION_HOST_NOT_ADMITTED")
        return result

    def prepare(self, target_release, rollback_release, authority_dir, controller_dir) -> dict:
        target = self._release(Path(target_release).absolute(), activation_host_required=True)
        rollback = self._release(Path(rollback_release).absolute(), activation_host_required=False)
        authority = Path(authority_dir).absolute()
        controller_path = Path(controller_dir).absolute()
        if (authority.parts[-4:] != (
                ".local", "state", "chatgpt-coding-mcp", "workspace-authority")
                or controller_path.parts[-4:] != (
                    ".local", "lib", "chatgpt-coding-mcp", "workspace-controller")):
            raise ActivationHostError("NONCANONICAL_HOST_PATH")
        try:
            controller = verify_controller(controller_path)
            _approved_interpreter(self.interpreter, self.approved_interpreters)
            _owned_tunnel_client(self.client)
            _owned_directory(self.profile_path.parent)
        except (WorkspaceControlError, TunnelSwitchError) as exc:
            raise ActivationHostError(getattr(exc, "code", "UNSAFE_HOST_CONFIGURATION")) from exc

        boot_id = self.boot_id_fn()
        if not isinstance(boot_id, str) or not re.fullmatch(r"[0-9a-f-]{16,64}", boot_id):
            raise ActivationHostError("BOOT_ID_UNAVAILABLE")
        tunnel_unit = self._read_unit(self.tunnel_unit_fn)
        admin_unit = self._read_unit(self.admin_unit_fn)
        profile = _read_bounded(self.profile_path)
        tunnel_before = _optional_file(self.tunnel_override_path)
        admin_before = _optional_file(self.admin_override_path)
        try:
            profile_data = _parse_profile(profile)
            tunnel_parsed = _parse_unit(tunnel_unit, self.client, self.profile_path)
        except (UnicodeError, TunnelSwitchError) as exc:
            raise ActivationHostError(getattr(exc, "code", "UNRECOGNIZED_TUNNEL_UNIT")) from exc
        admin_argv, port = _admin_command(
            admin_unit, self.interpreter, self.source_root, self.releases,
            self.config_root)
        tunnel_state = self.unit_state_fn(TUNNEL_SERVICE)
        admin_state = self.unit_state_fn(ADMIN_SERVICE)
        for state in (tunnel_state, admin_state):
            if (not isinstance(state, dict)
                    or set(state) != {"active_state", "sub_state", "main_pid", "job",
                                      "control_group"}):
                raise ActivationHostError("SERVICE_STATE_UNAVAILABLE")
        services = {
            "tunnel": {"unit": TUNNEL_SERVICE, "state": dict(tunnel_state),
                       "main": None, "children": []},
            "admin": {"unit": ADMIN_SERVICE, "state": dict(admin_state),
                      "main": None, "children": []},
        }
        tunnel_runtime = self._live_tunnel(boot_id)
        if tunnel_runtime is not None:
            tunnel_main = tunnel_runtime["main"]
            if (tunnel_state["control_group"]
                    and tunnel_state["control_group"] not in tunnel_main["cgroups"]):
                raise ActivationHostError("SERVICE_SCOPE_MISMATCH")
            services["tunnel"]["main"] = _pin_process(tunnel_main)
            for child in tunnel_runtime["children"]:
                if (tunnel_state["control_group"]
                        and tunnel_state["control_group"] not in child["cgroups"]):
                    raise ActivationHostError("SERVICE_SCOPE_MISMATCH")
                services["tunnel"]["children"].append(_pin_process(child))
        inactive = all(
            state["main_pid"] == 0 and state["active_state"] in {"inactive", "failed"}
            for state in (tunnel_state, admin_state)
        )
        if not inactive:
            if (tunnel_state["active_state"], tunnel_state["sub_state"],
                    tunnel_state["main_pid"], tunnel_state["job"]) != (
                    "active", "exited", 0, ""):
                raise ActivationHostError("SERVICE_IDENTITY_UNAVAILABLE")
            if tunnel_runtime is None:
                raise ActivationHostError("SERVICE_IDENTITY_UNAVAILABLE")
            if (admin_state["active_state"] != "active"
                    or admin_state["sub_state"] != "running"
                    or admin_state["main_pid"] <= 1 or admin_state["job"]):
                raise ActivationHostError("SERVICE_IDENTITY_UNAVAILABLE")
            admin_main = self._observe_main(admin_state, boot_id)
            if (admin_state["control_group"]
                    and admin_state["control_group"] not in admin_main["cgroups"]):
                raise ActivationHostError("SERVICE_SCOPE_MISMATCH")
            if admin_main["argv"] != admin_argv:
                raise ActivationHostError("ADMIN_MAINPID_MISMATCH")
            services["admin"]["main"] = _pin_process(admin_main)
        tunnel_target = _launcher_command(controller, authority, target["runtime"],
                                          self.interpreter, "server.py")
        admin_target = _launcher_command(controller, authority, target["runtime"],
                                         self.interpreter, "admin_server.py", admin_argv[2:])
        raw_plan = {"profile_data": profile_data, "unit_argv": tunnel_parsed["argv"],
                    "unit_mcp_index": tunnel_parsed["mcp_index"]}
        target_profile, target_tunnel_override, target_unit_argv = _target_bytes(raw_plan, tunnel_target)
        target_admin_override = ("[Service]\nExecStart=\nExecStart="
                                 + shlex.join(admin_target) + "\n").encode()
        inventory = self._inventory()
        plan = {
            "version": 1, "boot_id": boot_id,
            "target_release": str(target["runtime"]), "target_snapshot": target["snapshot"],
            "rollback_release": str(rollback["runtime"]), "rollback_snapshot": rollback["snapshot"],
            "authority_dir": str(authority), "controller_dir": str(controller_path),
            "services": services,
            "paths": {"profile": str(self.profile_path),
                      "tunnel_override": str(self.tunnel_override_path),
                      "admin_override": str(self.admin_override_path)},
            "before_sha256": {"profile": _sha256(profile),
                              "tunnel_override": _digest_or_none(tunnel_before),
                              "admin_override": _digest_or_none(admin_before)},
            "target_sha256": {"profile": _sha256(target_profile),
                              "tunnel_override": _sha256(target_tunnel_override),
                              "admin_override": _sha256(target_admin_override)},
            "unit_sha256": {"tunnel": _sha256(tunnel_unit.encode()),
                            "admin": _sha256(admin_unit.encode())},
            "unit_context": {"tunnel": [list(value) for value in _unit_context(tunnel_unit)],
                             "admin": [list(value) for value in _unit_context(admin_unit)]},
            "before_argv": {"tunnel": tunnel_parsed["argv"], "admin": admin_argv},
            "target_argv": {"tunnel": tunnel_target, "admin": admin_target,
                            "tunnel_unit": target_unit_argv},
            "exec_argv": {
                "tunnel": [str(self.interpreter), str(target["server"])],
                "admin": [str(self.interpreter),
                          str(target["runtime"] / "admin_server.py"), *admin_argv[2:]],
            },
            "admin_port": port, "inventory": inventory,
            "expected_binding": None,
        }
        plan["plan_sha256"] = _json_digest({
            key: value for key, value in plan.items() if key != "expected_binding"
        })
        return plan

    def _validate_plan_pins(self, plan: dict, *, require_current_boot: bool) -> None:
        target_value = plan.get("target_release", "") if isinstance(plan, dict) else ""
        if (not isinstance(plan, dict) or plan.get("version") != 1
                or plan.get("plan_sha256") != _json_digest({
                    key: value for key, value in plan.items()
                    if key not in {"plan_sha256", "expected_binding"}
                })
                or not isinstance(plan.get("boot_id"), str)
                or not re.fullmatch(r"[0-9a-f-]{16,64}", plan["boot_id"])
                or not _HEX20.fullmatch(plan.get("target_snapshot", ""))
                or Path(target_value).name != plan.get("target_snapshot")
                or not _HEX20.fullmatch(plan.get("rollback_snapshot", ""))
                or Path(plan.get("rollback_release", "")).name
                != plan.get("rollback_snapshot")
                or (require_current_boot and plan.get("boot_id") != self.boot_id_fn())):
            raise ActivationHostError("ACTIVATION_PLAN_INVALID")
        expected_paths = {"profile": str(self.profile_path),
                          "tunnel_override": str(self.tunnel_override_path),
                          "admin_override": str(self.admin_override_path)}
        authority = Path(plan.get("authority_dir", ""))
        controller_path = Path(plan.get("controller_dir", ""))
        target_release = Path(target_value)
        rollback_release = Path(plan.get("rollback_release", ""))
        if (plan.get("paths") != expected_paths
                or target_release.parent != self.releases
                or rollback_release.parent != self.releases
                or authority.parts[-4:] != (
                    ".local", "state", "chatgpt-coding-mcp", "workspace-authority")
                or controller_path.parts[-4:] != (
                    ".local", "lib", "chatgpt-coding-mcp", "workspace-controller")):
            raise ActivationHostError("ACTIVATION_PLAN_INVALID")
        try:
            controller = verify_controller(controller_path)
        except (KeyError, WorkspaceControlError) as exc:
            raise ActivationHostError("ACTIVATION_PLAN_INVALID") from exc
        expected_tunnel = _launcher_command(
            controller, authority, target_release,
            self.interpreter, "server.py")
        if plan.get("target_argv", {}).get("tunnel") != expected_tunnel:
            raise ActivationHostError("ACTIVATION_PLAN_INVALID")
        admin = plan.get("target_argv", {}).get("admin")
        if (not isinstance(admin, list) or admin[:10] != _launcher_command(
                controller, authority, target_release,
                self.interpreter, "admin_server.py") or admin[10:11] != ["--"]):
            raise ActivationHostError("ACTIVATION_PLAN_INVALID")
        self._release(target_release, activation_host_required=True)
        self._release(rollback_release, activation_host_required=False)

    def _validate_plan(self, plan: dict) -> None:
        self._validate_plan_pins(plan, require_current_boot=True)

    @staticmethod
    def _expected_binding(plan: dict) -> dict:
        expected = plan.get("expected_binding")
        if (not isinstance(expected, dict)
                or set(expected) != {"release_digest", "release_abi", "runtime", "binding"}
                or not re.fullmatch(r"[0-9a-f]{64}", expected.get("release_digest", ""))
                or isinstance(expected.get("release_abi"), bool)
                or not isinstance(expected.get("release_abi"), int)
                or expected["release_abi"] < 1
                or expected.get("runtime") != plan["target_release"]
                or not isinstance(expected.get("binding"), dict)
                or set(expected["binding"]) != {
                    "installation_id", "authority_epoch", "policy_digest"}
                or not re.fullmatch(r"[0-9a-f]{32}",
                                    expected["binding"].get("installation_id", ""))
                or isinstance(expected["binding"].get("authority_epoch"), bool)
                or not isinstance(expected["binding"].get("authority_epoch"), int)
                or expected["binding"]["authority_epoch"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}",
                                    expected["binding"].get("policy_digest", ""))):
            raise ActivationHostError("AUTHORITY_BINDING_REQUIRED")
        return expected

    def recover_committed_plan(self, plan: dict) -> dict:
        """Rebase a committed, fully drained plan after a host reboot."""
        current_boot = self.boot_id_fn()
        if not isinstance(current_boot, str) or not re.fullmatch(
                r"[0-9a-f-]{16,64}", current_boot):
            raise ActivationHostError("BOOT_ID_UNAVAILABLE")
        if isinstance(plan, dict) and plan.get("boot_id") == current_boot:
            self._validate_plan(plan)
            return plan

        self._validate_plan_pins(plan, require_current_boot=False)
        expected = self._expected_binding(plan)
        try:
            binding = read_activation_binding(plan["authority_dir"])
        except WorkspaceControlError as exc:
            raise ActivationHostError(exc.code) from exc
        if binding != expected["binding"]:
            raise ActivationHostError("AUTHORITY_BINDING_MISMATCH")
        self._assert_wiring(plan)
        self._assert_no_work(self._inventory())
        if self._live_tunnel(current_boot) is not None:
            raise ActivationHostError("TUNNEL_RUNTIME_STILL_LIVE")

        services = {}
        for key, unit in (("tunnel", TUNNEL_SERVICE), ("admin", ADMIN_SERVICE)):
            state = self.unit_state_fn(unit)
            if (not isinstance(state, dict)
                    or set(state) != {"active_state", "sub_state", "main_pid", "job",
                                      "control_group"}):
                raise ActivationHostError("SERVICE_STATE_UNAVAILABLE")
            if (state["active_state"] not in {"inactive", "failed"}
                    or state["sub_state"] not in {"dead", "failed"}
                    or state["main_pid"] != 0 or state["job"]
                    or state["control_group"]):
                raise ActivationHostError("SERVICE_NOT_DRAINED")
            services[key] = {
                "unit": unit, "state": dict(state), "main": None, "children": [],
            }

        recovered = copy.deepcopy(plan)
        recovered["boot_id"] = current_boot
        recovered["services"] = services
        recovered["plan_sha256"] = _json_digest({
            key: value for key, value in recovered.items()
            if key not in {"plan_sha256", "expected_binding"}
        })
        return recovered

    def stop(self, plan: dict) -> dict:
        self._validate_plan(plan)
        self._assert_no_work(self._inventory())
        for key in ("tunnel", "admin"):
            pinned = plan["services"][key]
            state = self.unit_state_fn(pinned["unit"])
            if state["main_pid"] == 0 and state["active_state"] in {"inactive", "failed"}:
                continue
            if key == "tunnel":
                if (state["active_state"], state["sub_state"], state["main_pid"],
                        state["job"]) != ("active", "exited", 0, ""):
                    raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
                try:
                    live = freeze_live_runtime(
                        self.tunnel_status_fn(), releases=self.releases,
                        proc_root=self.process_root,
                        approved_interpreters=self.approved_interpreters,
                    )
                except TunnelSwitchError as exc:
                    raise ActivationHostError(exc.code) from exc
                observed = _process(self.process_root, live["tunnel_pid"], plan["boot_id"])
            else:
                observed = self._observe_main(state, plan["boot_id"])
            if not _matches_process_pin(observed, pinned["main"]):
                raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
        for unit in SERVICES:
            self._assert_no_work(self._inventory())
            state = self.unit_state_fn(unit)
            if state["main_pid"] or state["active_state"] not in {"inactive", "failed"}:
                self.command_fn(["systemctl", "--user", "stop", unit])
        return self.verify_drained(plan)

    def verify_drained(self, plan: dict) -> dict:
        self._validate_plan(plan)
        self._assert_no_work(self._inventory())
        if self._live_tunnel(plan["boot_id"]) is not None:
            raise ActivationHostError("TUNNEL_RUNTIME_STILL_LIVE")
        for key in ("tunnel", "admin"):
            pinned = plan["services"][key]
            state = self.unit_state_fn(pinned["unit"])
            if (state["active_state"] not in {"inactive", "failed"}
                    or state["sub_state"] not in {"dead", "failed"}
                    or state["main_pid"] != 0 or state["job"]):
                raise ActivationHostError("SERVICE_NOT_DRAINED")
            if state["control_group"]:
                raise ActivationHostError("SERVICE_SCOPE_LIVE")
            for old in ([pinned["main"]] if pinned["main"] is not None else []) + pinned["children"]:
                if (self.process_root / str(old["pid"])).exists():
                    current = _process(self.process_root, old["pid"], plan["boot_id"])
                    if current["start_ticks"] != old["start_ticks"]:
                        raise ActivationHostError("SERVICE_PID_REUSED")
                    raise ActivationHostError("OLD_SERVICE_PROCESS_LIVE")
        return {"verified": True, "boot_id": plan["boot_id"]}

    def _wire_bytes(self, plan: dict) -> dict[str, bytes]:
        profile_raw = _read_bounded(self.profile_path)
        tunnel_unit = self._read_unit(self.tunnel_unit_fn)
        admin_unit = self._read_unit(self.admin_unit_fn)
        try:
            profile_data = _parse_profile(profile_raw)
            tunnel = _parse_unit(tunnel_unit, self.client, self.profile_path)
        except (UnicodeError, TunnelSwitchError) as exc:
            raise ActivationHostError(getattr(exc, "code", "HOST_CONFIGURATION_CHANGED")) from exc
        contexts = {
            "tunnel": [list(value) for value in _unit_context(tunnel_unit)],
            "admin": [list(value) for value in _unit_context(admin_unit)],
        }
        admin_argv = _effective_execstart(admin_unit)
        if (_json_digest(contexts) != _json_digest(plan["unit_context"])
                or tunnel["argv"] not in (plan["before_argv"]["tunnel"],
                                           plan["target_argv"]["tunnel_unit"])
                or admin_argv not in (plan["before_argv"]["admin"],
                                      plan["target_argv"]["admin"])):
            raise ActivationHostError("HOST_CONFIGURATION_CHANGED")
        raw_plan = {"profile_data": profile_data, "unit_argv": tunnel["argv"],
                    "unit_mcp_index": tunnel["mcp_index"]}
        profile, tunnel_override, _ = _target_bytes(raw_plan, plan["target_argv"]["tunnel"])
        admin_override = ("[Service]\nExecStart=\nExecStart="
                          + shlex.join(plan["target_argv"]["admin"]) + "\n").encode()
        return {"profile": profile, "tunnel_override": tunnel_override,
                "admin_override": admin_override}

    def wire(self, plan: dict) -> dict:
        with self._held_rollout_lock() as lock_descriptor:
            self._assert_rollout_lock(lock_descriptor)
            self.verify_drained(plan)
            self._assert_rollout_lock(lock_descriptor)
            targets = self._wire_bytes(plan)
            self._assert_rollout_lock(lock_descriptor)
            paths = {name: Path(path) for name, path in plan["paths"].items()}
            for path in (paths["tunnel_override"], paths["admin_override"]):
                self._assert_rollout_lock(lock_descriptor)
                if not path.parent.exists():
                    path.parent.mkdir(mode=0o700, parents=True)
                _owned_directory(path.parent)
                self._assert_rollout_lock(lock_descriptor)
            for name in ("profile", "tunnel_override", "admin_override"):
                self._assert_rollout_lock(lock_descriptor)
                current = _optional_file(paths[name])
                digest = _digest_or_none(current)
                self._assert_rollout_lock(lock_descriptor)
                if digest == plan["target_sha256"][name]:
                    continue
                if digest != plan["before_sha256"][name]:
                    raise ActivationHostError("HOST_CONFIGURATION_CHANGED")
                try:
                    self._assert_rollout_lock(lock_descriptor)
                    _cas_write(paths[name], current, targets[name])
                    self._assert_rollout_lock(lock_descriptor)
                except (OSError, TunnelSwitchError) as exc:
                    raise ActivationHostError("HOST_CONFIGURATION_CHANGED") from exc
            self._assert_rollout_lock(lock_descriptor)
            self.command_fn(["systemctl", "--user", "daemon-reload"])
            self._assert_rollout_lock(lock_descriptor)
            result = self.verify_wired(plan)
            self._assert_rollout_lock(lock_descriptor)
            return result

    def _assert_wiring(self, plan: dict) -> None:
        for name in ("tunnel_override", "admin_override"):
            path = plan["paths"][name]
            if _digest_or_none(_optional_file(Path(path))) != plan["target_sha256"][name]:
                raise ActivationHostError("HOST_WIRING_MISMATCH")
        try:
            profile = _parse_profile(_read_bounded(self.profile_path))
        except TunnelSwitchError as exc:
            raise ActivationHostError("HOST_WIRING_MISMATCH") from exc
        canonical_profile = json.dumps(
            profile, sort_keys=True, separators=(",", ":")).encode()
        if _sha256(canonical_profile) != plan["target_sha256"]["profile"]:
            raise ActivationHostError("HOST_WIRING_MISMATCH")
        tunnel_unit = self._read_unit(self.tunnel_unit_fn)
        admin_unit = self._read_unit(self.admin_unit_fn)
        contexts = {
            "tunnel": [list(value) for value in _unit_context(tunnel_unit)],
            "admin": [list(value) for value in _unit_context(admin_unit)],
        }
        if _json_digest(contexts) != _json_digest(plan["unit_context"]):
            raise ActivationHostError("HOST_WIRING_MISMATCH")
        tunnel = _parse_unit(tunnel_unit, self.client, self.profile_path)
        if tunnel["argv"] != plan["target_argv"]["tunnel_unit"]:
            raise ActivationHostError("HOST_WIRING_MISMATCH")
        admin = _effective_execstart(admin_unit)
        # Managed launchers cannot be parsed by the legacy parser before authority;
        # the exact generated argv is instead compared while both units are stopped.
        if admin != plan["target_argv"]["admin"]:
            raise ActivationHostError("HOST_WIRING_MISMATCH")

    def verify_wired(self, plan: dict) -> dict:
        self.verify_drained(plan)
        self._assert_wiring(plan)
        return {"verified": True, "target_snapshot": plan["target_snapshot"]}

    def _authority_admission(self, plan: dict) -> dict:
        expected = self._expected_binding(plan)
        observed = self._authority_active(
            Path(plan["authority_dir"]), Path(plan["target_release"]))
        actual = {
            "release_digest": observed.get("release_digest") if isinstance(observed, dict) else None,
            "release_abi": observed.get("release_abi") if isinstance(observed, dict) else None,
            "runtime": observed.get("runtime") if isinstance(observed, dict) else None,
            "binding": observed.get("binding") if isinstance(observed, dict) else None,
        }
        if (not isinstance(observed, dict) or observed.get("authority_active") is not True
                or actual != expected):
            raise ActivationHostError("AUTHORITY_BINDING_MISMATCH")
        return actual

    def _observe_started_service(self, plan: dict, key: str,
                                 launcher_identity: dict | None = None,
                                 allow_launcher: bool = False) -> dict:
        unit = TUNNEL_SERVICE if key == "tunnel" else ADMIN_SERVICE
        state = self.unit_state_fn(unit)
        if key == "admin":
            if (state["active_state"] != "active" or state["sub_state"] != "running"
                    or state["main_pid"] <= 1 or state["job"]
                    or not state["control_group"]):
                raise ActivationHostError("SERVICE_NOT_STARTED")
            main = _process(self.process_root, state["main_pid"], plan["boot_id"])
            if state["control_group"] not in main["cgroups"]:
                raise ActivationHostError("SERVICE_SCOPE_MISMATCH")
            identity = _exec_identity(main)
            if launcher_identity is not None and identity != launcher_identity:
                raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
            if main["argv"] == plan["exec_argv"]["admin"]:
                return {"state": state, "main": main}
            if allow_launcher and main["argv"] == plan["target_argv"]["admin"]:
                return {"state": state, "launcher_identity": identity}
            raise ActivationHostError("STARTED_RELEASE_MISMATCH")
        if (state["active_state"], state["sub_state"], state["main_pid"],
                state["job"]) != ("active", "exited", 0, ""):
            raise ActivationHostError("SERVICE_NOT_STARTED")
        try:
            live = freeze_live_runtime(
                self.tunnel_status_fn(), releases=self.releases,
                proc_root=self.process_root,
                approved_interpreters=self.approved_interpreters,
            )
        except TunnelSwitchError as exc:
            raise ActivationHostError(exc.code) from exc
        main = _process(self.process_root, live["tunnel_pid"], plan["boot_id"])
        if (main["start_ticks"] != live["tunnel_start_ticks"]
                or (state["control_group"]
                    and state["control_group"] not in main["cgroups"])):
            raise ActivationHostError("SERVICE_SCOPE_MISMATCH")
        children = []
        for path in self.process_root.iterdir():
            if path.name.isdecimal() and int(path.name) != main["pid"]:
                try:
                    child = _process(self.process_root, int(path.name), plan["boot_id"])
                except ActivationHostError:
                    continue
                if child["ppid"] == main["pid"] and child["argv"] == plan["exec_argv"]["tunnel"]:
                    children.append(child)
        if (len(children) != 1
                or state["control_group"] not in children[0]["cgroups"]):
            raise ActivationHostError("STARTED_RELEASE_MISMATCH")
        return {"state": state, "main": main, "child": children[0]}

    def _await_started_service(self, plan: dict, key: str) -> dict:
        observed = self._observe_started_service(plan, key, allow_launcher=True)
        launcher_identity = observed.get("launcher_identity")
        if launcher_identity is None:
            return observed
        deadline = self.clock_fn() + self.startup_timeout
        while self.clock_fn() < deadline:
            remaining = deadline - self.clock_fn()
            self.sleep_fn(min(_STARTUP_POLL_SECONDS, max(0.0, remaining)))
            observed = self._observe_started_service(
                plan, key, launcher_identity, allow_launcher=True)
            if "launcher_identity" not in observed:
                return observed
        raise ActivationHostError("STARTED_RELEASE_MISMATCH")

    def start(self, plan: dict) -> dict:
        self._validate_plan(plan)
        self._assert_wiring(plan)
        self._authority_admission(plan)
        for key, unit in (("tunnel", TUNNEL_SERVICE), ("admin", ADMIN_SERVICE)):
            state = self.unit_state_fn(unit)
            if state["main_pid"] == 0 and state["active_state"] in {"inactive", "failed"}:
                self.command_fn(["systemctl", "--user", "start", unit])
            self._await_started_service(plan, key)
        return self.verify_started(plan)

    def stop_started(self, plan: dict) -> dict:
        """Stop only exact post-exec target processes after a committed start failure."""
        self._validate_plan(plan)
        self._assert_wiring(plan)
        expected = plan.get("expected_binding")
        try:
            binding = read_activation_binding(plan["authority_dir"])
        except WorkspaceControlError as exc:
            raise ActivationHostError(exc.code) from exc
        if not isinstance(expected, dict) or binding != expected.get("binding"):
            raise ActivationHostError("AUTHORITY_BINDING_MISMATCH")
        self._assert_no_work(self._inventory())
        observed = {}
        for key, unit in (("tunnel", TUNNEL_SERVICE), ("admin", ADMIN_SERVICE)):
            state = self.unit_state_fn(unit)
            if state["main_pid"] == 0 and state["active_state"] in {"inactive", "failed"}:
                continue
            observed[key] = self._observe_started_service(plan, key)
        for key, unit in (("tunnel", TUNNEL_SERVICE), ("admin", ADMIN_SERVICE)):
            if key not in observed:
                continue
            self._assert_no_work(self._inventory())
            # Re-observe immediately before the fixed stop command. A changed
            # process is a blocker and is never treated as ours.
            if self._observe_started_service(plan, key) != observed[key]:
                raise ActivationHostError("SERVICE_IDENTITY_CHANGED")
            self.command_fn(["systemctl", "--user", "stop", unit])
        self.verify_drained(plan)
        identities = []
        for item in observed.values():
            identities.append(item["main"])
            if "child" in item:
                identities.append(item["child"])
        for identity in identities:
            path = self.process_root / str(identity["pid"])
            if not path.exists():
                continue
            current = _process(self.process_root, identity["pid"], plan["boot_id"])
            if current["start_ticks"] != identity["start_ticks"]:
                raise ActivationHostError("SERVICE_PID_REUSED")
            raise ActivationHostError("TARGET_SERVICE_PROCESS_LIVE")
        self._assert_wiring(plan)
        return {"verified": True, "stopped_target": plan["target_snapshot"]}

    def verify_started(self, plan: dict) -> dict:
        self._validate_plan(plan)
        self._assert_wiring(plan)
        binding = self._authority_admission(plan)
        observed = {key: self._observe_started_service(plan, key)
                    for key in ("tunnel", "admin")}
        if self.admin_health_fn(plan["admin_port"]) is not True:
            raise ActivationHostError("ADMIN_HEALTH_UNVERIFIED")
        return {"verified": True, "snapshot": plan["target_snapshot"],
                "binding": binding, "services": observed,
                "tunnel_child": observed["tunnel"]["child"]}
