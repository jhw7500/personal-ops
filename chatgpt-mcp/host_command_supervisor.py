"""Host-command lifecycle boundary backed by exact systemd/cgroup identities.

The inherited ``personal-ops-agent-*`` unit prefix is only the allocator namespace
of the shared supervisor.  It grants no agent capability or broker context.
"""
from __future__ import annotations

import os

from agent_supervisor import (
    BOOT_ID, HEX32, IDENTITY_KEYS, UNIT, SystemdAgentSupervisor,
)
from host_runtime_config import HostRuntimeError


def _tree_unverified() -> HostRuntimeError:
    error = HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED", "EXECUTE")
    error.termination_verified = False
    return error


class HostCommandSupervisor:
    """Compose the agent supervisor without importing any agent authorization."""

    def __init__(self, *, native=None):
        self.native = native or SystemdAgentSupervisor()

    async def start(self, operation_id, argv, workspace, env, timeout_seconds, *,
                    expected_bootstrap_sha256):
        try:
            process, identity = await self.native.start(
                operation_id, argv, workspace, env, timeout_seconds,
                expected_bootstrap_sha256=expected_bootstrap_sha256)
        except HostRuntimeError as exc:
            error = HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
            error.termination_verified = (
                getattr(exc, "termination_verified", None) is True
                or exc.code in {"AGENT_START_FAILED", "AGENT_SUPERVISION_UNVERIFIED"}
                and getattr(exc, "termination_verified", None) is not False)
            raise error from None
        except Exception:
            error = HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
            error.termination_verified = False
            raise error from None
        if identity.get("task_id") != operation_id:
            raise _tree_unverified()
        return process, identity

    async def stop(self, identity):
        try:
            evidence = await self.native.stop(identity)
        except Exception:
            raise _tree_unverified() from None
        if evidence != {"termination_verified": True}:
            raise _tree_unverified()
        return evidence

    async def finish(self, identity):
        try:
            evidence = await self.native.finish(identity)
        except Exception:
            raise _tree_unverified() from None
        if evidence != {"termination_verified": True}:
            raise _tree_unverified()
        return evidence

    def forget(self, identity):
        self.native.forget(identity)

    def _validate_record(self, identity):
        try:
            current_boot = self.native._boot()
            valid = (
                isinstance(identity, dict)
                and set(identity) == IDENTITY_KEYS
                and isinstance(identity["task_id"], str)
                and HEX32.fullmatch(identity["task_id"])
                and isinstance(identity["unit"], str)
                and UNIT.fullmatch(identity["unit"])
                and isinstance(identity["invocation_id"], str)
                and HEX32.fullmatch(identity["invocation_id"])
                and isinstance(identity["boot_id"], str)
                and BOOT_ID.fullmatch(identity["boot_id"])
                and isinstance(current_boot, str)
                and BOOT_ID.fullmatch(current_boot)
                and type(identity["uid"]) is int
                and identity["uid"] == os.getuid()
                and self.native._group(identity["unit"], identity["cgroup"])
                and all(type(identity[name]) is int and identity[name] > 1
                        for name in ("pid", "start_ticks", "wrapper_pid",
                                     "wrapper_start_ticks"))
            )
        except (HostRuntimeError, KeyError, TypeError, OSError):
            valid = False
        if not valid:
            raise _tree_unverified()
        return current_boot

    def inspect_identity(self, identity):
        """Read current exact binding state without stopping or adopting the unit."""
        current_boot = self._validate_record(identity)
        # No process or cgroup membership survives a Linux boot. Never inspect
        # or signal a same-named unit allocated after the recorded boot.
        if identity["boot_id"] != current_boot:
            return {"termination_verified": True, "status": "terminated"}
        try:
            current = self.native.observe(identity["unit"])
            if current["load_state"] == "not-found":
                if self.native.cgroup_empty(identity["cgroup"]):
                    return {"termination_verified": True, "status": "terminated"}
                raise _tree_unverified()
            if (not current["transient"]
                    or current["invocation_id"] != identity["invocation_id"]
                    or current["cgroup"] != identity["cgroup"]):
                raise _tree_unverified()
            if current["active_state"] in {"inactive", "failed"}:
                if self.native.cgroup_empty(identity["cgroup"]):
                    return {"termination_verified": True, "status": "terminated"}
                raise _tree_unverified()
            if not self.native.verify_binding(identity):
                raise _tree_unverified()
            return {"termination_verified": False, "status": "active"}
        except HostRuntimeError as exc:
            if exc.code == "HOST_COMMAND_PROCESS_TREE_UNVERIFIED":
                raise
            raise _tree_unverified() from None
        except (KeyError, TypeError, OSError):
            raise _tree_unverified() from None

    async def finish_recorded(self, identity):
        """Stop a persisted identity only after re-proving its exact live binding."""
        status = self.inspect_identity(identity)
        if status["termination_verified"]:
            return {"termination_verified": True}
        if not self.native.verify_binding(identity):
            raise _tree_unverified()
        unit = identity["unit"]
        if self.native._owned.get(unit) not in (None, identity):
            raise _tree_unverified()
        self.native._owned[unit] = dict(identity)
        try:
            result = await self.stop(identity)
        except Exception:
            raise
        self.native.forget(identity)
        return result
