#!/usr/bin/env python3
"""Owner-only, stopped-service upgrade to Workspace ABI 3 and HostAccess v2."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import time

from host_access_installation import InstallationGrantState
from host_runtime_config import HostRuntimeError
from workspace_activation import ActivationError, ActivationJournal
from workspace_activation_cli import (
    OwnerRoots, build_host, build_legacy_reader, load_projects,
)
from workspace_activation_host import (
    ADMIN_SERVICE, SERVICES, TUNNEL_SERVICE, ActivationHostError,
    _effective_execstart, _launcher_command, _matches_process_pin,
    _pin_process, _process,
)
from workspace_control import (
    WorkspaceControlError, activation_admission, activation_maintenance,
    activation_paths,
    read_activation_binding, release_enforcement_abi, verify_controller,
    verify_release_manifest,
)
from workspace_paths import WorkspaceError, read_owner_file
from workspace_tools import WorkspaceTools
from workspace_host import publication_lane
from rollout_admin import ALLOWED_ARGS
from rollout_tunnel import TunnelSwitchError, _parse_profile, _parse_unit


_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PHASES = (
    "prepared", "authority_saved", "floor_saved", "marker_intent",
    "marker_saved", "legacy_intent", "legacy_invalidated", "state_intent",
    "state_saved", "verified", "complete",
)


class UpgradeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code, cause=None):
    raise UpgradeError(code) from cause


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _legacy_digest(records_dir: Path) -> str:
    values = []
    try:
        for path in sorted(records_dir.iterdir(), key=lambda item: item.name):
            observed = read_owner_file(path)
            values.append([path.name, observed.sha256, list(observed.identity)])
    except (OSError, WorkspaceError) as exc:
        _fail("HOST_ACCESS_PREEXISTING_STATE", exc)
    return hashlib.sha256(_canonical(values)).hexdigest()


class ExistingUpgradeHost:
    """Read-only evidence adapter for the already managed two-service host."""

    def __init__(self, adapter, *, controller_dir, authority_dir, pinned_files,
                 node_prefix=None):
        self.adapter = adapter
        self.controller_dir = Path(controller_dir).absolute()
        self.authority_dir = Path(authority_dir).absolute()
        self.pinned_files = tuple(Path(path).absolute() for path in pinned_files)
        self.node_prefix = Path(node_prefix or (
            Path.home() / ".nvm/versions/node/v24.12.0")).absolute()

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    def _configuration(self):
        pins = {}
        for path in self.pinned_files:
            try:
                observed = read_owner_file(path)
                pins[str(path)] = {
                    "sha256": observed.sha256,
                    "identity": list(observed.identity),
                }
            except WorkspaceError as exc:
                _fail("UPGRADE_CONFIGURATION_UNSAFE", exc)
        try:
            controller = verify_controller(self.controller_dir)
            interpreter = self.adapter.interpreter.lstat()
            if (not self.adapter.interpreter.is_file()
                    or interpreter.st_uid != os.getuid()):
                _fail("UPGRADE_INTERPRETER_UNSAFE")
            units = {
                "tunnel": self.adapter._read_unit(self.adapter.tunnel_unit_fn),
                "admin": self.adapter._read_unit(self.adapter.admin_unit_fn),
            }
            optional = {}
            for path in (self.adapter.tunnel_override_path,
                         self.adapter.admin_override_path):
                optional[str(path)] = (None if not path.exists() and not path.is_symlink()
                                       else read_owner_file(path).sha256)
            return {
                "files": pins,
                "controller": controller["manifest"],
                "interpreter": [interpreter.st_dev, interpreter.st_ino,
                                interpreter.st_size, interpreter.st_mtime_ns],
                "units": {name: self._digest(text.encode("utf-8"))
                          for name, text in units.items()},
                "overrides": optional,
            }
        except (OSError, WorkspaceControlError, WorkspaceError,
                ActivationHostError) as exc:
            if isinstance(exc, UpgradeError):
                raise
            _fail("UPGRADE_CONFIGURATION_UNSAFE", exc)

    @staticmethod
    def _process_header(entry, info):
        try:
            line = (entry / "stat").read_text()
            marker = line.rfind(") ")
            fields = line[marker + 2:].split()
            after = entry.lstat()
            if (marker < 0 or len(fields) < 20
                    or len(fields[0]) != 1
                    or not fields[1].isdecimal()
                    or not fields[19].isdecimal()
                    or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError
            return {
                "state": fields[0], "ppid": int(fields[1]),
                "start_ticks": int(fields[19]),
            }
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            _fail("PROCESS_IDENTITY_UNAVAILABLE", exc)

    @staticmethod
    def _terminal(header):
        return header["state"] in {"Z", "X", "x"}

    def _managed_processes(self, boot_id):
        result = []
        try:
            entries = list(self.adapter.process_root.iterdir())
        except OSError as exc:
            _fail("PROCESS_IDENTITY_UNAVAILABLE", exc)
        for entry in entries:
            if not entry.name.isdecimal() or int(entry.name) <= 1:
                continue
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                _fail("PROCESS_IDENTITY_UNAVAILABLE", exc)
            if info.st_uid != os.getuid():
                continue
            try:
                header = self._process_header(entry, info)
            except FileNotFoundError:
                continue
            if self._terminal(header):
                try:
                    repeated = self._process_header(entry, info)
                except FileNotFoundError:
                    continue
                if repeated != header:
                    _fail("PROCESS_IDENTITY_UNAVAILABLE")
                continue
            try:
                process = _process(self.adapter.process_root, int(entry.name), boot_id)
            except ActivationHostError as exc:
                if exc.code == "PROCESS_IDENTITY_UNAVAILABLE":
                    try:
                        after = entry.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        _fail("PROCESS_IDENTITY_UNAVAILABLE", error)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        continue
                raise UpgradeError(exc.code) from exc
            if (process["ppid"], process["start_ticks"]) != (
                    header["ppid"], header["start_ticks"]):
                _fail("PROCESS_IDENTITY_UNAVAILABLE")
            managed = False
            if process["argv"] == ["codex", "app-server"]:
                managed = True
            elif self._node_wrapper_candidate(process):
                chain = self._codex_wrapper(
                    process, boot_id, process["cgroups"])
                if chain is None:
                    _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
                result.extend(_pin_process(item) for item in chain)
                continue
            elif self._node_native_candidate(process):
                if not self._codex_native(process):
                    _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
                managed = True
            for argument in process["argv"]:
                if argument == str(self.adapter.client):
                    managed = True
                    break
                candidate = Path(argument)
                if (candidate.name in {"server.py", "admin_server.py"}
                        and candidate.is_absolute()
                        and candidate.resolve(strict=False).is_relative_to(
                            self.adapter.releases.resolve(strict=False))):
                    managed = True
                    break
            if managed:
                result.append(_pin_process(process))
        return sorted(result, key=lambda item: (item["pid"], item["start_ticks"]))

    def _direct_children(self, parent_pid, boot_id):
        children = []
        for entry in self.adapter.process_root.iterdir():
            if not entry.name.isdecimal() or int(entry.name) <= 1:
                continue
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                _fail("PROCESS_IDENTITY_UNAVAILABLE", exc)
            if info.st_uid != os.getuid():
                continue
            try:
                header = self._process_header(entry, info)
            except FileNotFoundError:
                continue
            if self._terminal(header):
                try:
                    repeated = self._process_header(entry, info)
                except FileNotFoundError:
                    continue
                if repeated != header:
                    _fail("PROCESS_IDENTITY_UNAVAILABLE")
                continue
            if header["ppid"] != parent_pid:
                continue
            try:
                process = _process(self.adapter.process_root, int(entry.name), boot_id)
            except ActivationHostError as exc:
                if exc.code == "PROCESS_IDENTITY_UNAVAILABLE":
                    try:
                        after = entry.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        _fail("PROCESS_IDENTITY_UNAVAILABLE", error)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        continue
                raise UpgradeError(exc.code) from exc
            if (process["ppid"], process["start_ticks"]) != (
                    header["ppid"], header["start_ticks"]):
                _fail("PROCESS_IDENTITY_UNAVAILABLE")
            children.append(process)
        return children

    def _same_client(self, candidate):
        try:
            actual = Path(candidate).stat()
            expected = self.adapter.client.stat()
            return (stat.S_ISREG(actual.st_mode)
                    and actual.st_uid == os.getuid()
                    and (actual.st_dev, actual.st_ino)
                    == (expected.st_dev, expected.st_ino))
        except OSError:
            return False

    @staticmethod
    def _owned_regular(path, *, executable):
        try:
            info = path.lstat()
        except OSError:
            return False
        return (stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) & 0o022 == 0
                and (not executable or info.st_mode & 0o100))

    def _codex_paths(self):
        return {
            "node": self.node_prefix / "bin/node",
            "wrapper": self.node_prefix / "bin/codex",
            "script": (self.node_prefix
                       / "lib/node_modules/@openai/codex/bin/codex.js"),
            "native": (self.node_prefix
                       / "lib/node_modules/@openai/codex/node_modules/@openai/"
                       "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"),
        }

    def _node_wrapper_candidate(self, process):
        paths = self._codex_paths()
        argv = process["argv"]
        return str(paths["wrapper"]) in argv and "app-server" in argv

    def _node_native_candidate(self, process):
        argv = process["argv"]
        return (str(self._codex_paths()["native"]) in argv
                and "app-server" in argv)

    def _codex_native(self, process, groups=None):
        native = self._codex_paths()["native"]
        try:
            process_exe = (self.adapter.process_root / str(process["pid"])
                           / "exe").resolve(strict=True)
            native_target = native.resolve(strict=True)
        except OSError:
            return False
        return (process["argv"] == [str(native), "app-server"]
                and (groups is None or process["cgroups"] == groups)
                and self._owned_regular(native, executable=True)
                and process_exe == native_target)

    def _codex_wrapper(self, process, boot_id, tunnel_groups):
        paths = self._codex_paths()
        node = paths["node"]
        wrapper = paths["wrapper"]
        script = paths["script"]
        native = paths["native"]
        try:
            wrapper_info = wrapper.lstat()
            wrapper_target = wrapper.resolve(strict=True)
            script_target = script.resolve(strict=True)
            node_target = node.resolve(strict=True)
            process_exe = (self.adapter.process_root / str(process["pid"])
                           / "exe").resolve(strict=True)
        except OSError:
            return None
        if (process["argv"] not in (
                    ["node", str(wrapper), "app-server"],
                    [str(node), str(wrapper), "app-server"])
                or process["cgroups"] != tunnel_groups
                or not stat.S_ISLNK(wrapper_info.st_mode)
                or wrapper_info.st_uid != os.getuid()
                or wrapper_target != script_target
                or process_exe != node_target
                or not self._owned_regular(node, executable=True)
                or not self._owned_regular(script, executable=False)
                or not self._owned_regular(native, executable=True)):
            return None
        children = self._direct_children(process["pid"], boot_id)
        if len(children) != 1:
            return None
        child = children[0]
        if not self._codex_native(child, tunnel_groups):
            return None
        return [process, child]

    def _running_target(self, target, boot_id, states, tunnel):
        tunnel_state = states[TUNNEL_SERVICE]
        admin_state = states[ADMIN_SERVICE]
        if ((tunnel_state["active_state"], tunnel_state["sub_state"],
             tunnel_state["main_pid"], tunnel_state["job"])
                != ("active", "exited", 0, "")
                or tunnel is None
                or admin_state["active_state"] != "active"
                or admin_state["sub_state"] != "running"
                or admin_state["main_pid"] <= 1 or admin_state["job"]):
            _fail("UPGRADE_RUNNING_TARGET_REQUIRED")
        try:
            controller = verify_controller(self.controller_dir)
            runtime = Path(target["runtime"])
            expected_tunnel = _launcher_command(
                controller, self.authority_dir, runtime,
                self.adapter.interpreter, "server.py")
            profile = _parse_profile(read_owner_file(self.adapter.profile_path).data)
            unit = _parse_unit(
                self.adapter._read_unit(self.adapter.tunnel_unit_fn),
                self.adapter.client, self.adapter.profile_path)
            profile_command = shlex.split(profile["mcp"]["commands"][0]["command"])
            unit_command = shlex.split(unit["argv"][unit["mcp_index"]])
            if profile_command != expected_tunnel or unit_command != expected_tunnel:
                _fail("UPGRADE_TARGET_WIRING_MISMATCH")
            admin_start = _effective_execstart(
                self.adapter._read_unit(self.adapter.admin_unit_fn))
            admin_prefix = _launcher_command(
                controller, self.authority_dir, runtime,
                self.adapter.interpreter, "admin_server.py")
            if (admin_start[:len(admin_prefix)] != admin_prefix
                    or admin_start[len(admin_prefix):len(admin_prefix) + 1] != ["--"]):
                _fail("UPGRADE_TARGET_WIRING_MISMATCH")
            admin_tail = admin_start[len(admin_prefix) + 1:]
            if (len(admin_tail) % 2
                    or any(admin_tail[index] not in ALLOWED_ARGS
                           for index in range(0, len(admin_tail), 2))):
                _fail("UPGRADE_TARGET_WIRING_MISMATCH")
        except (OSError, UnicodeError, ValueError, WorkspaceError,
                WorkspaceControlError, TunnelSwitchError) as exc:
            if isinstance(exc, UpgradeError):
                raise
            _fail("UPGRADE_TARGET_WIRING_MISMATCH", exc)

        live = tunnel["live"]
        unit_argv = unit["argv"]
        profile_name = unit_argv[unit_argv.index("--profile") + 1]
        profile_dir = unit_argv[unit_argv.index("--profile-dir") + 1]
        daemon = tunnel["main"]["argv"]
        if (live.get("server") != str(runtime / "server.py")
                or live.get("interpreter") != str(self.adapter.interpreter)
                or len(daemon) != 7
                or not self._same_client(daemon[0])
                or daemon[1:] != [
                    "run", "--profile-dir", profile_dir,
                    "--profile", profile_name, "--log.file"]):
            _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
        by_pid = {process["pid"]: process for process in tunnel["children"]}
        mcp = by_pid.get(live["mcp_pid"])
        codex = by_pid.get(live.get("codex_pid")) if "codex_pid" in live else None
        expected_children = {live["mcp_pid"]}
        if "codex_pid" in live:
            expected_children.add(live["codex_pid"])
        direct = self._direct_children(live["tunnel_pid"], boot_id)
        direct_by_pid = {process["pid"]: process for process in direct}
        if (mcp is None
                or mcp["argv"] != [str(self.adapter.interpreter),
                                    str(runtime / "server.py")]
                or (codex is not None and codex["argv"] != ["codex", "app-server"])):
            _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
        extra = [process for process in direct
                 if process["pid"] not in expected_children]
        roles = [tunnel["main"], mcp]
        if codex is not None:
            if extra:
                _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
            roles.append(codex)
        else:
            if not extra:
                wrapper = []
            elif len(extra) == 1:
                wrapper = self._codex_wrapper(
                    extra[0], boot_id, tunnel["main"]["cgroups"])
            else:
                wrapper = None
            if wrapper is None:
                _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
            roles.extend(wrapper)
        if not expected_children <= set(direct_by_pid):
            _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
        admin = _process(self.adapter.process_root, admin_state["main_pid"], boot_id)
        if admin["argv"] != [str(self.adapter.interpreter),
                             str(runtime / "admin_server.py"), *admin_tail]:
            _fail("UPGRADE_TARGET_PROCESS_MISMATCH")
        return [*roles, admin]

    def prepare(self, target):
        boot_id = self.adapter.boot_id_fn()
        states = {unit: self.adapter.unit_state_fn(unit) for unit in SERVICES}
        tunnel = self.adapter._live_tunnel(boot_id)
        roles = self._running_target(target, boot_id, states, tunnel)
        processes = {
            (process["pid"], process["start_ticks"]): _pin_process(process)
            for process in roles
        }
        for pin in self._managed_processes(boot_id):
            processes[(pin["pid"], pin["start_ticks"])] = pin
        return {
            "boot_id": boot_id,
            "configuration": self._configuration(),
            "processes": sorted(processes.values(),
                                key=lambda item: (item["pid"], item["start_ticks"])),
        }

    def verify_drained(self, expected):
        self.verify_configuration(expected)
        inventory = self.adapter._inventory()
        self.adapter._assert_no_work(inventory)
        states = {unit: self.adapter.unit_state_fn(unit) for unit in SERVICES}
        for state in states.values():
            if (state["active_state"] not in {"inactive", "failed"}
                    or state["sub_state"] not in {"dead", "failed"}
                    or state["main_pid"] != 0 or state["job"]
                    or state["control_group"]):
                _fail("SERVICES_NOT_STOPPED")
        current_boot = self.adapter.boot_id_fn()
        if self.adapter._live_tunnel(current_boot) is not None:
            _fail("TUNNEL_RUNTIME_STILL_LIVE")
        if current_boot == expected["boot_id"]:
            for pin in expected["processes"]:
                if not (self.adapter.process_root / str(pin["pid"])).exists():
                    continue
                folder = self.adapter.process_root / str(pin["pid"])
                try:
                    before = folder.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _fail("PROCESS_IDENTITY_UNAVAILABLE", exc)
                try:
                    current = _process(
                        self.adapter.process_root, pin["pid"], current_boot)
                except ActivationHostError as exc:
                    if exc.code == "PROCESS_IDENTITY_UNAVAILABLE":
                        try:
                            after = folder.lstat()
                        except FileNotFoundError:
                            continue
                        except OSError as error:
                            _fail("PROCESS_IDENTITY_UNAVAILABLE", error)
                        if ((after.st_dev, after.st_ino)
                                != (before.st_dev, before.st_ino)):
                            continue
                    raise UpgradeError(exc.code) from exc
                if _matches_process_pin(current, pin):
                    _fail("RESIDUAL_PROCESS")
        if self._managed_processes(current_boot):
            _fail("RESIDUAL_PROCESS")
        return {"drained": True, "boot_id": current_boot}

    def verify_configuration(self, expected):
        if self._configuration() != expected["configuration"]:
            _fail("UPGRADE_HOST_CHANGED")
        return {"configuration_verified": True}


class UpgradeCoordinator:
    """Crash-resumable state machine; service stop/start remains operator-owned."""

    def __init__(self, projects, *, authority, host_access, host, journal_dir,
                 rollout_lock, workspace=None, source_digest=None,
                 target_verify=None):
        self.projects = {name: Path(root).absolute() for name, root in projects.items()}
        self.authority = authority
        self.host_access = host_access
        self.host = host
        self.workspace = workspace
        self.journal = ActivationJournal(journal_dir, self.projects)
        self.rollout_lock = rollout_lock
        self.source_digest = source_digest or hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest()
        self.target_verify = target_verify
        self.crash_after = None

    def _checkpoint(self, name):
        if self.crash_after == name:
            raise RuntimeError("injected crash after " + name)

    def _authority_snapshot(self):
        with self.authority._locked_state(shared=True) as (_, state):
            return {
                "binding": self.authority._binding(state),
                "allowed_releases": dict(state["allowed_releases"]),
                "fingerprint": self.authority._state_fingerprint(state),
            }

    def _minimum_abi(self):
        parent_fd = os.open(
            self.authority.state_dir.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            return self.authority._read_minimum_abi(parent_fd, required=True)
        finally:
            os.close(parent_fd)

    def _initial_access(self):
        installation = self.host_access.installation
        return {
            "marker_absent": not (
                installation.marker_path.exists() or installation.marker_path.is_symlink()),
            "state_absent": not (
                installation.state_path.exists() or installation.state_path.is_symlink()),
            "legacy_digest": _legacy_digest(self.host_access.records_dir),
        }

    def prepare(self, transaction_id, *, target, binding=None):
        if not isinstance(transaction_id, str) or _ID.fullmatch(transaction_id) is None:
            _fail("UPGRADE_ID_INVALID")
        if (not isinstance(target, dict)
                or set(target) != {"release_digest", "release_abi", "runtime"}
                or not isinstance(target.get("release_digest"), str)
                or _DIGEST.fullmatch(target["release_digest"]) is None
                or not isinstance(target.get("runtime"), str)):
            _fail("UPGRADE_TARGET_INVALID")
        if target["release_abi"] != 3:
            _fail("UPGRADE_TARGET_ABI_REQUIRED")
        authority = self._authority_snapshot()
        if binding is not None and binding != authority["binding"]:
            _fail("UPGRADE_BINDING_CHANGED")
        if authority["allowed_releases"].get(target["release_digest"]) != 3:
            _fail("UPGRADE_TARGET_UNADMITTED")
        if self._minimum_abi() != 2:
            _fail("UPGRADE_ABI_FLOOR_REQUIRED")
        initial_access = self._initial_access()
        if not initial_access["marker_absent"] or not initial_access["state_absent"]:
            _fail("HOST_ACCESS_PREEXISTING_STATE")
        host_plan = self.host.prepare(target)
        transitioned_at = time.time()
        transition = self.authority._owner_upgrade_preview(
            target["release_digest"], transitioned_at)
        if (transition["binding"] != authority["binding"]
                or transition["original_fingerprint"] != authority["fingerprint"]):
            _fail("UPGRADE_AUTHORITY_CHANGED")
        final_binding = transition["final_binding"]
        v2_state = {
            "version": 2,
            "installation_id": authority["binding"]["installation_id"],
            "uid": os.getuid(),
            "capability_generation": 1,
            "grant_state_revision": 1,
            "requests": {},
            "grant": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        plan = {
            "target": dict(target),
            "original_binding": authority["binding"],
            "final_binding": final_binding,
            "authority_original_fingerprint": transition["original_fingerprint"],
            "authority_final_fingerprint": transition["final_fingerprint"],
            "authority_transitioned_at": transitioned_at,
            "initial_access": initial_access,
            "host": host_plan,
            "v2_state": v2_state,
        }
        return self.journal.create(transaction_id, self.source_digest, plan)

    @staticmethod
    def _same_binding(actual, first, second):
        return actual == first or actual == second

    @staticmethod
    def _phase_at_least(phase, expected):
        try:
            return _PHASES.index(phase) >= _PHASES.index(expected)
        except ValueError as exc:
            _fail("UPGRADE_JOURNAL_INVALID", exc)

    def _validate_plan(self, state):
        plan = state["plan"]
        if self.target_verify is not None:
            observed = self.target_verify(plan["target"]["runtime"])
            expected = plan["target"]
            if any(observed.get(key) != expected[key] for key in expected):
                _fail("UPGRADE_TARGET_CHANGED")
        authority = self._authority_snapshot()
        if not self._same_binding(
                authority["binding"], plan["original_binding"], plan["final_binding"]):
            _fail("UPGRADE_BINDING_CHANGED")
        expected_fingerprint = (
            plan["authority_original_fingerprint"]
            if authority["binding"] == plan["original_binding"]
            else plan["authority_final_fingerprint"])
        if authority["fingerprint"] != expected_fingerprint:
            _fail("UPGRADE_AUTHORITY_CHANGED")
        expected_releases = {plan["target"]["release_digest"]: 3}
        if authority["binding"] == plan["original_binding"]:
            if authority["allowed_releases"].get(plan["target"]["release_digest"]) != 3:
                _fail("UPGRADE_TARGET_UNADMITTED")
        elif authority["allowed_releases"] != expected_releases:
            _fail("UPGRADE_BINDING_CHANGED")
        minimum_abi = self._minimum_abi()
        if authority["binding"] == plan["original_binding"]:
            if minimum_abi != 2:
                _fail("UPGRADE_ABI_FLOOR_CHANGED")
        elif minimum_abi not in {2, 3}:
            _fail("UPGRADE_ABI_FLOOR_CHANGED")
        current_access = self._initial_access()
        initial = plan["initial_access"]
        if not initial["marker_absent"] or not initial["state_absent"]:
            _fail("HOST_ACCESS_PREEXISTING_STATE")
        marker_exists = not current_access["marker_absent"]
        state_exists = not current_access["state_absent"]
        if state_exists and not marker_exists:
            _fail("HOST_ACCESS_STATE_CHANGED")
        if marker_exists and not self._phase_at_least(
                state["phase"], "marker_intent"):
            _fail("HOST_ACCESS_PREEXISTING_STATE")
        if state_exists and not self._phase_at_least(
                state["phase"], "state_intent"):
            _fail("HOST_ACCESS_STATE_CHANGED")
        if (not self.host_access.installation.marker_path.exists()
                and not self.host_access.installation.state_path.exists()
                and current_access["legacy_digest"] != initial["legacy_digest"]):
            _fail("HOST_ACCESS_STATE_CHANGED")

    def _verify_maintenance(self, maintenance, binding):
        try:
            actual, _ = maintenance._verify_binding(binding)
        except WorkspaceControlError as exc:
            _fail("UPGRADE_BINDING_CHANGED", exc)
        if actual != binding or read_activation_binding(
                self.authority.state_dir) != binding:
            _fail("UPGRADE_BINDING_CHANGED")

    def _mark(self, maintenance, plan):
        self._verify_maintenance(maintenance, plan["final_binding"])
        installation = self.host_access.installation
        with installation.lock():
            marker = installation.marker_path.exists() or installation.marker_path.is_symlink()
            state = installation.state_path.exists() or installation.state_path.is_symlink()
            if state and not marker:
                _fail("HOST_ACCESS_STATE_CHANGED")
            if not marker:
                if state:
                    _fail("HOST_ACCESS_STATE_CHANGED")
                installation.mark_enabled()

    def _invalidate_legacy(self, maintenance, plan):
        self._verify_maintenance(maintenance, plan["final_binding"])
        with self.host_access.installation.lock():
            self.host_access.installation.invalidate_legacy(
                self.host_access.records_dir)

    def _create_state(self, maintenance, plan):
        self._verify_maintenance(maintenance, plan["final_binding"])
        installation = self.host_access.installation
        with installation.lock():
            if installation.state_path.exists() or installation.state_path.is_symlink():
                if installation.load() != plan["v2_state"]:
                    _fail("HOST_ACCESS_STATE_CHANGED")
            else:
                if not (installation.marker_path.exists()
                        or installation.marker_path.is_symlink()):
                    _fail("HOST_ACCESS_STATE_CHANGED")
                installation.create(plan["v2_state"])

    def _verify_complete(self, maintenance, plan):
        self._verify_maintenance(maintenance, plan["final_binding"])
        if (self._authority_snapshot()["fingerprint"]
                != plan["authority_final_fingerprint"]):
            _fail("UPGRADE_AUTHORITY_CHANGED")
        if self._minimum_abi() != 3:
            _fail("UPGRADE_ABI_FLOOR_CHANGED")
        with self.host_access.installation.lock():
            state = self.host_access.installation.load()
        if state != plan["v2_state"] or state["grant"] is not None or state["requests"]:
            _fail("HOST_ACCESS_STATE_CHANGED")
        self.host.verify_drained(plan["host"])

    @staticmethod
    def _result(transaction_id, plan):
        return {
            "status": "complete",
            "transaction_id": transaction_id,
            "binding": plan["final_binding"],
            "minimum_abi": 3,
            "release_digest": plan["target"]["release_digest"],
            "host_access_version": 2,
            "grant_state_revision": 1,
        }

    def _intent_identity(self):
        path = activation_paths(self.authority.state_dir)[1]
        if not path.exists() and not path.is_symlink():
            return None
        try:
            return read_owner_file(path).identity
        except WorkspaceError as exc:
            _fail("UPGRADE_MAINTENANCE_REQUIRED", exc)

    def _durable_effects_exist(self, plan):
        installation = self.host_access.installation
        return (
            self._authority_snapshot()["binding"] == plan["final_binding"]
            or self._minimum_abi() == 3
            or installation.marker_path.exists()
            or installation.marker_path.is_symlink()
            or installation.state_path.exists()
            or installation.state_path.is_symlink()
        )

    def _validate_completed(self, state, transaction_id):
        try:
            self._validate_plan(state)
            plan = state["plan"]
            authority = self._authority_snapshot()
            if (authority["binding"] != plan["final_binding"]
                    or authority["fingerprint"]
                    != plan["authority_final_fingerprint"]
                    or self._minimum_abi() != 3
                    or self._intent_identity() is not None):
                _fail("UPGRADE_COMPLETION_INVALID")
            with self.host_access.installation.lock():
                access = self.host_access.installation.load()
            if access != plan["v2_state"]:
                _fail("UPGRADE_COMPLETION_INVALID")
            self.host.verify_configuration(plan["host"])
            return self._result(transaction_id, plan)
        except UpgradeError as exc:
            if exc.code == "UPGRADE_COMPLETION_INVALID":
                raise
            _fail("UPGRADE_COMPLETION_INVALID", exc)
        except (HostRuntimeError, WorkspaceError, WorkspaceControlError,
                ActivationHostError) as exc:
            _fail("UPGRADE_COMPLETION_INVALID", exc)

    def apply(self, transaction_id):
        observed = self.journal.read(transaction_id)
        if observed["source_digest"] != self.source_digest:
            _fail("UPGRADE_SOURCE_CHANGED")
        try:
            with self.rollout_lock(), self.journal.locked(
                    transaction_id, self.source_digest) as current:
                plan = current.state["plan"]
                if current.state["phase"] == "complete":
                    with publication_lane(self.workspace):
                        return self._validate_completed(current.state, transaction_id)
                if current.state["phase"] == "verified":
                    with publication_lane(self.workspace):
                        result = self._validate_completed(
                            current.state, transaction_id)
                    current.save("complete", evidence={"result": result})
                    return result
                self._validate_plan(current.state)
                intent_identity = self._intent_identity()
                if (current.state["phase"] == "state_saved"
                        and intent_identity is None):
                    with publication_lane(self.workspace):
                        try:
                            result = self._validate_completed(
                                current.state, transaction_id)
                        except UpgradeError:
                            if self._durable_effects_exist(plan):
                                _fail("UPGRADE_MAINTENANCE_REQUIRED")
                            raise
                    current.save("verified", evidence={"result": result})
                    self._checkpoint("verified")
                    current.save("complete", evidence={"result": result})
                    return result
                if self._durable_effects_exist(plan) and intent_identity is None:
                    _fail("UPGRADE_MAINTENANCE_REQUIRED")
                with activation_maintenance(
                        self.authority.state_dir, transaction_id,
                        self.source_digest) as maintenance:
                    if (intent_identity is not None
                            and self._intent_identity() != intent_identity):
                        _fail("UPGRADE_MAINTENANCE_REQUIRED")
                    with publication_lane(self.workspace):
                        self.host.verify_drained(plan["host"])
                        binding = self.authority._owner_upgrade_state(
                            maintenance, plan["original_binding"],
                            plan["target"]["release_digest"],
                            plan["authority_original_fingerprint"],
                            plan["authority_final_fingerprint"],
                            plan["authority_transitioned_at"])
                        if binding != plan["final_binding"]:
                            _fail("UPGRADE_BINDING_CHANGED")
                        self._checkpoint("authority")
                        if not self._phase_at_least(
                                current.state["phase"], "authority_saved"):
                            current.save("authority_saved")

                        self.authority._owner_upgrade_minimum_abi(
                            maintenance, plan["final_binding"],
                            plan["authority_final_fingerprint"])
                        self._checkpoint("floor")
                        if not self._phase_at_least(
                                current.state["phase"], "floor_saved"):
                            current.save("floor_saved")

                        if not self._phase_at_least(current.state["phase"], "marker_intent"):
                            current.save("marker_intent")
                        self._mark(maintenance, plan)
                        self._checkpoint("marker")
                        if not self._phase_at_least(current.state["phase"], "marker_saved"):
                            current.save("marker_saved")

                        if not self._phase_at_least(current.state["phase"], "legacy_intent"):
                            current.save("legacy_intent")
                        self._invalidate_legacy(maintenance, plan)
                        self._checkpoint("legacy")
                        if not self._phase_at_least(current.state["phase"], "legacy_invalidated"):
                            current.save("legacy_invalidated")

                        if not self._phase_at_least(current.state["phase"], "state_intent"):
                            current.save("state_intent")
                        self._create_state(maintenance, plan)
                        self._checkpoint("state")
                        if not self._phase_at_least(current.state["phase"], "state_saved"):
                            current.save("state_saved")

                        self._verify_complete(maintenance, plan)
                        result = self._result(transaction_id, plan)
                        maintenance.finish(plan["final_binding"])
                        self._checkpoint("finish")
                        if not self._phase_at_least(current.state["phase"], "verified"):
                            current.save("verified", evidence={"result": result})
                        self._checkpoint("verified")
                        current.save("complete", evidence={"result": result})
                        return result
        except UpgradeError:
            raise
        except (ActivationError, WorkspaceError, WorkspaceControlError,
                HostRuntimeError, ActivationHostError) as exc:
            code = getattr(exc, "code", "UPGRADE_FAILED")
            if code in {"AUTHORITY_BINDING_STALE", "WORKSPACE_AUTHORITY_CHANGED"}:
                code = ("UPGRADE_AUTHORITY_CHANGED"
                        if code == "AUTHORITY_BINDING_STALE"
                        else "UPGRADE_BINDING_CHANGED")
            raise UpgradeError(code) from exc

    def resume(self, transaction_id):
        return self.apply(transaction_id)

    def assert_admission_open(self):
        try:
            with activation_admission(self.authority.state_dir, create=True):
                return True
        except WorkspaceControlError as exc:
            raise UpgradeError(exc.code) from exc


def _target_release(path):
    observed = verify_release_manifest(path)
    abi = release_enforcement_abi(path)
    return {**observed, "release_abi": abi}


def _verify_installed_self(source):
    source = Path(source).absolute()
    try:
        manifest = json.loads(read_owner_file(source / "manifest.json").data)
    except (WorkspaceError, OSError, UnicodeError, ValueError) as exc:
        _fail("UPGRADE_SOURCE_UNADMITTED", exc)
    if (re.fullmatch(r"[0-9a-f]{20}", source.name) is None
            or source.parent.parts[-4:] != (
                ".local", "lib", "chatgpt-coding-mcp", "releases")
            or manifest.get("workspace_upgrade.py") != hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest()):
        _fail("UPGRADE_SOURCE_UNADMITTED")
    return manifest


def build_parser():
    parser = argparse.ArgumentParser(prog="workspace-upgrade")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "status", "apply", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--transaction-id", required=True)
        command.add_argument("--projects-json")
    commands.choices["prepare"].add_argument("--target-release", required=True)
    return parser


def _coordinator(projects, roots, source):
    from runtime_rollout_web import rollout_lock
    workspace = WorkspaceTools(projects, state_dir=roots.authority,
                               config_path=roots.config_root / "workspace-policy.json")
    legacy = build_legacy_reader(projects, roots)
    access = legacy.access
    access.installation = InstallationGrantState(legacy.roots["host_access"])
    systemd_host = build_host(projects, roots, legacy, source)
    host = ExistingUpgradeHost(
        systemd_host, controller_dir=roots.controller,
        authority_dir=roots.authority,
        pinned_files=(roots.config_root / "projects.json",
                      roots.config_root / "workspace-policy.json", roots.profile),
    )
    return UpgradeCoordinator(
        projects, authority=workspace.authority, host_access=access, host=host,
        journal_dir=roots.state_root / "workspace-upgrade", rollout_lock=rollout_lock,
        workspace=workspace, target_verify=_target_release,
    )


def main(argv=None, *, environ=None, stdout=None):
    output = sys.stdout if stdout is None else stdout
    try:
        args = build_parser().parse_args(argv)
        if _ID.fullmatch(args.transaction_id) is None:
            _fail("UPGRADE_ID_INVALID")
        roots = OwnerRoots.from_environment(environ)
        source = Path(__file__).resolve().parent
        _verify_installed_self(source)
        if args.command == "status":
            result = ActivationJournal(
                roots.state_root / "workspace-upgrade", {}).read(args.transaction_id)
        else:
            projects_path = (Path(args.projects_json).absolute()
                             if args.projects_json else roots.config_root / "projects.json")
            projects = load_projects(projects_path)
            coordinator = _coordinator(projects, roots, source)
            if args.command == "prepare":
                target = _target_release(Path(args.target_release).absolute())
                if Path(target["runtime"]) != source:
                    _fail("UPGRADE_INSTALLED_RUNTIME_REQUIRED")
                verify_controller(roots.controller)
                result = coordinator.prepare(
                    args.transaction_id, target={key: target[key] for key in (
                        "release_digest", "release_abi", "runtime")},
                    binding=read_activation_binding(roots.authority),
                )
            else:
                result = coordinator.apply(args.transaction_id)
        output.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except Exception as exc:
        output.write(json.dumps({"status": "error", "error": getattr(
            exc, "code", "UPGRADE_FAILED")}, sort_keys=True, separators=(",", ":")) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
