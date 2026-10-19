"""One ephemeral user service per approved task; never stop a parent slice.

Cgroups supervise fork/setsid descendants. The Codex filesystem/network policy
must separately block user-manager sockets and cgroup migration. This module
does not treat same-UID cgroups as a hostile-code security boundary.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess

from agent_bootstrap import validate_bootstrap
from host_runtime_config import HostRuntimeError

UNIT = re.compile(r"personal-ops-agent-[0-9a-f]{32}\.service\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
IDENTITY_KEYS = frozenset({
    "task_id", "unit", "invocation_id", "cgroup", "boot_id", "uid",
    "pid", "start_ticks", "wrapper_pid", "wrapper_start_ticks",
})
READY = b"PERSONAL_OPS_AGENT_BOOTSTRAP_V1\n"
START_TOTAL_SECONDS = 20
PROPERTIES = ("Id", "LoadState", "Transient", "InvocationID", "ControlGroup",
              "MainPID", "ActiveState", "SubState")


def _manager_env():
    return {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def _command(argv):
    try:
        result = subprocess.run(argv, env=_manager_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HostRuntimeError("AGENT_SUPERVISION_UNAVAILABLE") from None
    if len(result.stdout) > 65536:
        raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
    return result


def _read(path, limit=8192):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError()
        return data.decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED") from None


class SystemdAgentSupervisor:
    def __init__(self, *, command_runner=None, proc_root=Path("/proc"),
                 cgroup_root=Path("/sys/fs/cgroup")):
        self.command = command_runner or _command
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self._owned = {}

    def _boot(self):
        value = _read(self.proc_root / "sys/kernel/random/boot_id")
        if not BOOT_ID.fullmatch(value):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        return value

    def _process(self, pid):
        if type(pid) is not int or pid <= 1:
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        folder = self.proc_root / str(pid)
        raw = _read(folder / "stat")
        try:
            ticks = int(raw.rsplit(") ", 1)[1].split()[19])
            status = _read(folder / "status")
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            uids = [int(value) for value in uid_line.split()[1:]]
            if len(uids) != 4 or any(value != os.getuid() for value in uids):
                raise ValueError()
            groups = _read(folder / "cgroup").splitlines()
            if len(groups) != 1 or not groups[0].startswith("0::/"):
                raise ValueError()
            return {"start_ticks": ticks, "cgroup": groups[0][3:]}
        except (IndexError, ValueError, StopIteration):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED") from None

    @staticmethod
    def _group(unit, group):
        prefix = (f"/user.slice/user-{os.getuid()}.slice/"
                  f"user@{os.getuid()}.service/")
        return (isinstance(group, str) and group in {prefix + unit, prefix + "app.slice/" + unit})

    def observe(self, unit):
        if not isinstance(unit, str) or not UNIT.fullmatch(unit):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        argv = ["/usr/bin/systemctl", "--user", "--no-ask-password", "show", unit,
                *["--property=" + name for name in PROPERTIES]]
        result = self.command(argv)
        try:
            text = result.stdout.decode("ascii") if isinstance(result.stdout, bytes) else result.stdout
            lines = text.splitlines()
            if len(text) > 65536 or any("=" not in line for line in lines):
                raise ValueError()
            values = dict(line.split("=", 1) for line in lines)
            if len(values) != len(lines) or set(values) != set(PROPERTIES):
                raise ValueError()
            missing = values["LoadState"] == "not-found"
            if not missing and (result.returncode != 0 or values["Id"] != unit):
                raise ValueError()
            pid = int(values["MainPID"])
            return {"unit": unit, "load_state": values["LoadState"],
                    "transient": values["Transient"] == "yes",
                    "invocation_id": values["InvocationID"], "cgroup": values["ControlGroup"],
                    "pid": pid, "active_state": values["ActiveState"],
                    "sub_state": values["SubState"]}
        except (UnicodeError, ValueError, TypeError, KeyError):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED") from None

    def verify_binding(self, identity, *, peer_pid=None):
        try:
            if (not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS
                    or not HEX32.fullmatch(identity["task_id"])
                    or not UNIT.fullmatch(identity["unit"])
                    or not HEX32.fullmatch(identity["invocation_id"])
                    or identity["boot_id"] != self._boot()
                    or type(identity["uid"]) is not int or identity["uid"] != os.getuid()
                    or not self._group(identity["unit"], identity["cgroup"])):
                return False
            current = self.observe(identity["unit"])
            if (current["load_state"] != "loaded" or not current["transient"]
                    or current["active_state"] not in {"active", "activating"}
                    or current["invocation_id"] != identity["invocation_id"]
                    or current["cgroup"] != identity["cgroup"] or current["pid"] != identity["pid"]):
                return False
            process = self._process(identity["pid"])
            if process["start_ticks"] != identity["start_ticks"] or process["cgroup"] != identity["cgroup"]:
                return False
            if peer_pid is not None:
                peer = self._process(peer_pid)
                if peer["cgroup"] != identity["cgroup"] and not peer["cgroup"].startswith(identity["cgroup"] + "/"):
                    return False
            return True
        except (HostRuntimeError, TypeError, KeyError, OSError):
            return False

    def cgroup_empty(self, group):
        unit = group.rsplit("/", 1)[-1] if isinstance(group, str) else ""
        if not UNIT.fullmatch(unit) or not self._group(unit, group):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        folder = self.cgroup_root / group.lstrip("/")
        for parent in [folder, *folder.parents]:
            if parent == self.cgroup_root.parent:
                break
            if parent.is_symlink():
                raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        if not folder.exists():
            return True
        text = _read(folder / "cgroup.events")
        populated = [line for line in text.splitlines() if line.startswith("populated ")]
        if populated == ["populated 0"]:
            return True
        if populated == ["populated 1"]:
            return False
        raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")

    async def _cleanup_created_unit(self, unit):
        """Stop the fresh random unit created by this supervisor start attempt."""
        current = await asyncio.to_thread(self.observe, unit)
        if current["load_state"] == "not-found":
            return {"termination_verified": True}
        if (not current["transient"] or not HEX32.fullmatch(current["invocation_id"])
                or not self._group(unit, current["cgroup"])):
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        invocation_id = current["invocation_id"]
        group = current["cgroup"]
        if current["active_state"] not in {"inactive", "failed"}:
            result = await asyncio.to_thread(self.command, [
                "/usr/bin/systemctl", "--user", "--no-ask-password", "stop", unit])
            if result.returncode != 0:
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        for _ in range(40):
            observed = await asyncio.to_thread(self.observe, unit)
            if observed["load_state"] == "not-found":
                if self.cgroup_empty(group):
                    return {"termination_verified": True}
            elif observed["invocation_id"] != invocation_id:
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
            elif observed["active_state"] in {"inactive", "failed"} and self.cgroup_empty(group):
                return {"termination_verified": True}
            await asyncio.sleep(0.05)
        raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")

    async def start(self, task_id, argv, workspace, env, timeout_seconds, *,
                    expected_bootstrap_sha256):
        return await asyncio.wait_for(
            self._start(task_id, argv, workspace, env, timeout_seconds,
                        expected_bootstrap_sha256=expected_bootstrap_sha256),
            timeout=START_TOTAL_SECONDS)

    async def _start(self, task_id, argv, workspace, env, timeout_seconds, *,
                     expected_bootstrap_sha256):
        if (not isinstance(task_id, str) or not HEX32.fullmatch(task_id)
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600
                or not isinstance(argv, list) or not argv or not Path(argv[0]).is_absolute()
                or not isinstance(expected_bootstrap_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_bootstrap_sha256)):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        workspace = Path(workspace).resolve(strict=True)
        info = workspace.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        bootstrap = Path(argv[0]).parent / "personal-ops-agent-bootstrap"
        bootstrap_sha256 = validate_bootstrap(bootstrap)
        if not secrets.compare_digest(bootstrap_sha256, expected_bootstrap_sha256):
            raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
        unit = "personal-ops-agent-" + secrets.token_hex(16) + ".service"
        arguments = ["/usr/bin/systemd-run", "--user", "--no-ask-password", "--quiet",
                     "--pipe", "--wait", "--collect", "--service-type=exec", "--unit=" + unit,
                     "--property=Restart=no", "--property=KillMode=control-group",
                     "--property=SendSIGKILL=yes", "--property=TimeoutStartSec=15s",
                     "--property=TimeoutStopSec=5s", "--property=Delegate=no",
                     "--property=UMask=0077", f"--property=RuntimeMaxSec={timeout_seconds}s",
                     "--working-directory=" + str(workspace), str(bootstrap),
                     *[f"{key}={value}" for key, value in sorted(env.items())], "--", *argv]
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments, cwd=workspace, env=_manager_env(), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=256 * 1024)
        except OSError:
            raise HostRuntimeError("AGENT_START_FAILED") from None
        identity = None
        try:
            marker = await asyncio.wait_for(process.stdout.readline(), timeout=15)
            if marker != READY:
                raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
            current = await asyncio.to_thread(self.observe, unit)
            native = self._process(current["pid"])
            wrapper = self._process(process.pid)
            identity = {"task_id": task_id, "unit": unit,
                        "invocation_id": current["invocation_id"], "cgroup": current["cgroup"],
                        "boot_id": self._boot(), "uid": os.getuid(), "pid": current["pid"],
                        "start_ticks": native["start_ticks"], "wrapper_pid": process.pid,
                        "wrapper_start_ticks": wrapper["start_ticks"]}
            if not await asyncio.to_thread(self.verify_binding, identity):
                raise HostRuntimeError("AGENT_SUPERVISION_UNVERIFIED")
            self._owned[unit] = dict(identity)
            return process, identity
        except BaseException:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=6)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), timeout=6)
            try:
                await self._cleanup_created_unit(unit)
            except Exception as exc:
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED") from exc
            self._owned.pop(unit, None)
            raise

    async def stop(self, identity):
        unit = identity.get("unit") if isinstance(identity, dict) else None
        if not unit or self._owned.get(unit) != identity or identity.get("boot_id") != self._boot():
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        current = await asyncio.to_thread(self.observe, unit)
        if current["load_state"] == "not-found":
            if not self.cgroup_empty(identity["cgroup"]):
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
            return {"termination_verified": True}
        if not await asyncio.to_thread(self.verify_binding, identity):
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        # A fresh never-reused unit name plus immediate invocation revalidation.
        # v249 StopUnit is not an atomic compare-and-stop; sandbox denies manager
        # access to task code. Other trusted same-UID operators remain trusted.
        if not await asyncio.to_thread(self.verify_binding, identity):
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        result = await asyncio.to_thread(self.command, [
            "/usr/bin/systemctl", "--user", "--no-ask-password", "stop", unit])
        if result.returncode != 0:
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        for _ in range(40):
            observed = await asyncio.to_thread(self.observe, unit)
            if (observed["load_state"] == "not-found"
                    or observed["invocation_id"] == identity["invocation_id"]
                    and observed["active_state"] in {"inactive", "failed"}):
                if self.cgroup_empty(identity["cgroup"]):
                    return {"termination_verified": True}
            elif observed["invocation_id"] != identity["invocation_id"]:
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
            await asyncio.sleep(0.05)
        raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")

    async def finish(self, identity):
        if self._owned.get(identity["unit"]) != identity or identity["boot_id"] != self._boot():
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        if self.cgroup_empty(identity["cgroup"]):
            return {"termination_verified": True}
        return await self.stop(identity)

    def forget(self, identity):
        if self._owned.get(identity["unit"]) == identity:
            self._owned.pop(identity["unit"])
