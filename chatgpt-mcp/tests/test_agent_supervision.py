"""Per-attempt systemd identity and cgroup cleanup; no real host units."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from agent_supervisor import SystemdAgentSupervisor
from host_runtime_config import HostRuntimeError


class AgentSupervisionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-supervision-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.cgroups = self.root / "cgroups"
        self.unit = "personal-ops-agent-" + "a" * 32 + ".service"
        self.group = (f"/user.slice/user-{os.getuid()}.slice/"
                      f"user@{os.getuid()}.service/app.slice/{self.unit}")
        self.boot = "11111111-2222-3333-4444-555555555555"
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(self.boot + "\n")
        process = self.proc / "42"
        process.mkdir()
        fields = ["S", "1", "42", "42"] + ["0"] * 15 + ["999"] + ["0"] * 4
        (process / "stat").write_text("42 (bootstrap) " + " ".join(fields))
        (process / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        (process / "cgroup").write_text("0::" + self.group + "\n")
        self.events = self.cgroups / self.group.lstrip("/") / "cgroup.events"
        self.events.parent.mkdir(parents=True)
        self.events.write_text("populated 1\nfrozen 0\n")
        other = self.cgroups / "unrelated/cgroup.events"
        other.parent.mkdir(parents=True)
        other.write_text("populated 1\n")
        self.properties = {
            "Id": self.unit, "LoadState": "loaded", "Transient": "yes",
            "InvocationID": "b" * 32, "ControlGroup": self.group,
            "MainPID": "42", "ActiveState": "active", "SubState": "running",
        }
        self.stops = []

        def command(argv):
            if "stop" in argv:
                self.stops.append(argv[-1])
                self.properties.update(LoadState="not-found", ActiveState="inactive",
                                       SubState="dead", MainPID="0")
                self.events.write_text("populated 0\nfrozen 0\n")
            output = "\n".join(f"{k}={v}" for k, v in self.properties.items()).encode()
            return subprocess.CompletedProcess(argv, 0, output, b"")

        self.supervisor = SystemdAgentSupervisor(
            command_runner=command, proc_root=self.proc, cgroup_root=self.cgroups)
        self.identity = {
            "task_id": "c" * 32, "unit": self.unit,
            "invocation_id": "b" * 32, "cgroup": self.group,
            "boot_id": self.boot, "uid": os.getuid(), "pid": 42,
            "start_ticks": 999, "wrapper_pid": 43, "wrapper_start_ticks": 1000,
        }

    def test_binding_requires_real_unit_and_peer_membership(self):
        self.assertTrue(self.supervisor.verify_binding(self.identity, peer_pid=42))
        self.properties["InvocationID"] = "d" * 32
        self.assertFalse(self.supervisor.verify_binding(self.identity, peer_pid=42))
        self.properties["InvocationID"] = "b" * 32
        (self.proc / "42/cgroup").write_text("0::/unrelated\n")
        self.assertFalse(self.supervisor.verify_binding(self.identity, peer_pid=42))

    async def test_stop_requires_native_owned_identity_and_preserves_other_cgroup(self):
        with self.assertRaises(HostRuntimeError):
            await self.supervisor.stop(self.identity)
        self.supervisor._owned[self.unit] = dict(self.identity)
        result = await self.supervisor.stop(self.identity)
        self.assertTrue(result["termination_verified"])
        self.assertEqual(self.stops, [self.unit])
        self.assertEqual((self.cgroups / "unrelated/cgroup.events").read_text(), "populated 1\n")

    async def test_stop_binding_checks_do_not_block_the_event_loop(self):
        original = self.supervisor.command

        def slow_command(argv):
            if "show" in argv:
                time.sleep(0.08)
            return original(argv)

        self.supervisor.command = slow_command
        self.supervisor._owned[self.unit] = dict(self.identity)
        largest_gap = 0.0
        running = True

        async def heartbeat():
            nonlocal largest_gap
            previous = asyncio.get_running_loop().time()
            while running:
                await asyncio.sleep(0.005)
                current = asyncio.get_running_loop().time()
                largest_gap = max(largest_gap, current - previous)
                previous = current

        pulse = asyncio.create_task(heartbeat())
        try:
            await self.supervisor.stop(self.identity)
        finally:
            running = False
            await pulse
        self.assertLess(largest_gap, 0.05)

    async def test_start_rejects_bootstrap_not_matching_approved_hash(self):
        executable = self.root / "codex"
        executable.write_bytes(b"fixture")
        executable.chmod(0o700)
        bootstrap = self.root / "personal-ops-agent-bootstrap"
        bootstrap.write_bytes(b"bootstrap")
        bootstrap.chmod(0o700)
        with mock.patch("agent_supervisor.validate_bootstrap", return_value="b" * 64), \
                mock.patch("agent_supervisor.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(HostRuntimeError):
                await self.supervisor.start(
                    "c" * 32, [str(executable)], self.root, {}, 60,
                    expected_bootstrap_sha256="a" * 64)
        spawn.assert_not_called()

    async def test_failed_start_cleanup_stops_fresh_unit_and_proves_empty_cgroup(self):
        result = await self.supervisor._cleanup_created_unit(self.unit)
        self.assertTrue(result["termination_verified"])
        self.assertEqual(self.stops, [self.unit])
        self.assertTrue(self.supervisor.cgroup_empty(self.group))

    async def test_reused_unit_invocation_is_never_stopped(self):
        self.supervisor._owned[self.unit] = dict(self.identity)
        self.properties["InvocationID"] = "d" * 32
        with self.assertRaises(HostRuntimeError):
            await self.supervisor.stop(self.identity)
        self.assertEqual(self.stops, [])
        self.assertIn("populated 1", self.events.read_text())

    async def test_collected_owned_unit_requires_empty_cgroup(self):
        self.supervisor._owned[self.unit] = dict(self.identity)
        self.properties.update(LoadState="not-found", ActiveState="inactive", MainPID="0")
        with self.assertRaises(HostRuntimeError):
            await self.supervisor.stop(self.identity)
        self.assertEqual(self.stops, [])
        self.events.write_text("populated 0\n")
        self.assertTrue((await self.supervisor.stop(self.identity))["termination_verified"])

    def test_parent_slice_or_foreign_user_never_counts_as_task_binding(self):
        for group in ["/", "/user.slice", self.group.replace(f"user-{os.getuid()}", "user-0"),
                      self.group + "/../another.service"]:
            with self.subTest(group=group):
                self.properties["ControlGroup"] = group
                identity = dict(self.identity, cgroup=group)
                self.assertFalse(self.supervisor.verify_binding(identity, peer_pid=42))

    def test_unknown_population_cannot_prove_termination(self):
        self.events.write_text("unexpected 0\n")
        with self.assertRaises(HostRuntimeError):
            self.supervisor.cgroup_empty(self.group)

    def test_invalid_unit_does_not_reach_manager(self):
        for unit in ["personal-ops-admin.service", "personal-ops-agent-../.service", self.unit + " "]:
            with self.subTest(unit=unit), self.assertRaises(HostRuntimeError):
                self.supervisor.observe(unit)
        self.assertEqual(self.stops, [])


if __name__ == "__main__":
    unittest.main()
