"""Recorded host-command identity inspection without real user units."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_command_supervisor import HostCommandSupervisor  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class NativeFixture:
    def __init__(self):
        self.boot = "11111111-2222-3333-4444-555555555555"
        self.unit = "personal-ops-agent-" + "a" * 32 + ".service"
        self.group = (f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                      f"app.slice/{self.unit}")
        self.current = {
            "unit": self.unit, "load_state": "loaded", "transient": True,
            "invocation_id": "b" * 32, "cgroup": self.group, "pid": 42,
            "active_state": "active", "sub_state": "running",
        }
        self.empty = False
        self._owned = {}
        self.stop_calls = 0
        self.empty_calls = 0
        self.observe_calls = 0

    def _boot(self):
        return self.boot

    @staticmethod
    def _group(unit, group):
        return group.endswith("/" + unit) and "/user.slice/" in group

    def observe(self, unit):
        self.observe_calls += 1
        if unit != self.unit:
            raise AssertionError("wrong unit")
        return dict(self.current)

    def verify_binding(self, identity):
        return (identity["boot_id"] == self.boot
                and identity["invocation_id"] == self.current["invocation_id"]
                and identity["cgroup"] == self.current["cgroup"]
                and identity["pid"] == self.current["pid"]
                and identity["start_ticks"] == 999
                and self.current["active_state"] in {"active", "activating"})

    def cgroup_empty(self, group):
        self.empty_calls += 1
        if group != self.group:
            raise AssertionError("wrong group")
        return self.empty

    async def stop(self, identity):
        self.stop_calls += 1
        if self._owned.get(identity["unit"]) != identity:
            raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
        self.empty = True
        self.current.update(active_state="inactive", sub_state="dead", pid=0)
        return {"termination_verified": True}

    def forget(self, identity):
        self._owned.pop(identity["unit"], None)


class FailedStartNative(NativeFixture):
    async def start(self, *args, **kwargs):
        raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")


class HostCommandSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.native = NativeFixture()
        self.supervisor = HostCommandSupervisor(native=self.native)
        self.identity = {
            "task_id": "c" * 32, "unit": self.native.unit,
            "invocation_id": "b" * 32, "cgroup": self.native.group,
            "boot_id": self.native.boot, "uid": os.getuid(), "pid": 42,
            "start_ticks": 999, "wrapper_pid": 43, "wrapper_start_ticks": 1000,
        }

    def test_live_exact_identity_is_read_only_and_not_terminated(self):
        self.assertEqual(self.supervisor.inspect_identity(self.identity),
                         {"termination_verified": False, "status": "active"})
        self.assertEqual(self.native.stop_calls, 0)

    def test_missing_unit_proves_empty_only_after_full_record_validation(self):
        self.native.current.update(load_state="not-found", active_state="inactive",
                                   sub_state="dead", pid=0)
        self.native.empty = True
        self.assertEqual(self.supervisor.inspect_identity(self.identity),
                         {"termination_verified": True, "status": "terminated"})
        changed = dict(self.identity, uid=os.getuid() + 1)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED"):
            self.supervisor.inspect_identity(changed)
        self.assertEqual(self.native.empty_calls, 1)

    def test_loaded_inactive_same_invocation_and_empty_group_is_terminated(self):
        self.native.current.update(active_state="failed", sub_state="failed", pid=0)
        self.native.empty = True
        self.assertEqual(self.supervisor.inspect_identity(self.identity),
                         {"termination_verified": True, "status": "terminated"})

    async def test_finish_recorded_adopts_only_current_exact_binding(self):
        result = await self.supervisor.finish_recorded(self.identity)
        self.assertEqual(result, {"termination_verified": True})
        self.assertEqual(self.native.stop_calls, 1)
        self.assertEqual(self.native._owned, {})

    async def test_prior_boot_identity_is_terminated_without_touching_reused_unit(self):
        prior = dict(self.identity, boot_id="99999999-8888-7777-6666-555555555555")
        self.native.current["invocation_id"] = prior["invocation_id"]

        self.assertEqual(self.supervisor.inspect_identity(prior),
                         {"termination_verified": True, "status": "terminated"})
        self.assertEqual(await self.supervisor.finish_recorded(prior),
                         {"termination_verified": True})
        self.assertEqual(self.native.observe_calls, 0)
        self.assertEqual(self.native.stop_calls, 0)
        self.assertEqual(self.native._owned, {})

    def test_prior_boot_proof_still_rejects_invalid_static_identity(self):
        prior = dict(self.identity, boot_id="99999999-8888-7777-6666-555555555555")
        invalid = (
            dict(prior, uid=os.getuid() + 1),
            dict(prior, unit="personal-ops-agent-invalid.service"),
            dict(prior, cgroup="/user.slice/reused.service"),
            dict(prior, boot_id="not-a-boot-id"),
        )
        for identity in invalid:
            with self.subTest(identity=identity), self.assertRaisesRegex(
                    HostRuntimeError, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED"):
                self.supervisor.inspect_identity(identity)
        self.assertEqual(self.native.observe_calls, 0)
        self.assertEqual(self.native.empty_calls, 0)

    async def test_reused_invocation_is_never_adopted_or_stopped(self):
        self.native.current["invocation_id"] = "d" * 32
        with self.assertRaisesRegex(HostRuntimeError, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED"):
            await self.supervisor.finish_recorded(self.identity)
        self.assertEqual(self.native.stop_calls, 0)
        self.assertEqual(self.native._owned, {})

        self.native.current["invocation_id"] = self.identity["invocation_id"]
        changed_ticks = dict(self.identity, start_ticks=1001)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED"):
            await self.supervisor.finish_recorded(changed_ticks)
        self.assertEqual(self.native.stop_calls, 0)

    async def test_native_start_cleanup_failure_is_never_reported_verified(self):
        supervisor = HostCommandSupervisor(native=FailedStartNative())
        with self.assertRaises(HostRuntimeError) as caught:
            await supervisor.start("c" * 32, ["/tmp/entry"], Path("/tmp"), {}, 10,
                                   expected_bootstrap_sha256="d" * 64)
        self.assertFalse(caught.exception.termination_verified)


if __name__ == "__main__":
    unittest.main()
