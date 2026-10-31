from __future__ import annotations

import copy
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

from rollout_tunnel import ALIAS  # noqa: E402
from workspace_activation_host import (  # noqa: E402
    ActivationHostError,
    SystemdActivationHost,
)
from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_control import (  # noqa: E402
    MANDATORY_RELEASE_FILES,
    install_controller,
    verify_launch,
)


class HostFixture:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.releases = self.home / ".local/lib/chatgpt-coding-mcp/releases"
        self.releases.mkdir(parents=True, mode=0o700)
        self.releases.chmod(0o700)
        self.source = MODULE
        self.target = self.release(b"target")
        self.rollback = self.release(b"rollback")
        self.interpreter = self.home / ".local/share/uv/tools/serena-agent/bin/python"
        self.interpreter.parent.mkdir(parents=True, mode=0o700)
        self.interpreter.write_bytes(b"python")
        self.interpreter.chmod(0o700)
        self.controller = self.home / ".local/lib/chatgpt-coding-mcp/workspace-controller"
        project = self.root / "project"
        project.mkdir()
        install_controller(self.controller, MODULE, projects={"demo": project})
        self.authority = self.home / ".local/state/chatgpt-coding-mcp/workspace-authority"
        self.rollout_lock = (self.home
                             / ".local/state/chatgpt-coding-mcp/runtime-rollout-web"
                             / "global-rollout.lock")
        self.rollout_lock.parent.mkdir(parents=True, mode=0o700)

        self.profile = self.home / ".config/tunnel-client/personal-ops-coding.yaml"
        self.profile.parent.mkdir(parents=True, mode=0o700)
        self.tunnel_override = (self.home / ".config/systemd/user/personal-ops-tunnel.service.d/20-runtime.conf")
        self.admin_override = (self.home / ".config/systemd/user/personal-ops-admin.service.d/20-runtime.conf")
        self.tunnel_override.parent.mkdir(parents=True, mode=0o700)
        self.admin_override.parent.mkdir(parents=True, mode=0o700)
        self.client = self.home / ".local/bin/tunnel-client"
        self.client.parent.mkdir(parents=True, mode=0o700)
        self.client.write_bytes(b"client")
        self.client.chmod(0o700)
        self.write_profile(self.rollback)

        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        self.write_process(4100, 1, 100, [str(self.client), "daemon"], "user.slice/tunnel")
        self.write_process(4101, 4100, 101,
                           [str(self.interpreter), str(self.rollback / "server.py")],
                           "user.slice/tunnel")
        self.write_process(4200, 1, 200,
                           [str(self.interpreter), str(self.rollback / "admin_server.py"),
                            "--port", "8765"], "user.slice/admin")
        self.units = {
            "personal-ops-tunnel.service": {
                "active_state": "active", "sub_state": "exited", "main_pid": 0,
                "job": "", "control_group": "/user.slice/tunnel",
            },
            "personal-ops-admin.service": {
                "active_state": "active", "sub_state": "running", "main_pid": 4200,
                "job": "", "control_group": "/user.slice/admin",
            },
        }
        self.calls = []
        self.tunnel_pid = 4100
        self.workers = []
        self.locked_leases = []
        self.reappear_after_tunnel_stop = False
        self.fail_admin_start_once = False

    def release(self, marker: bytes) -> Path:
        payloads = {
            "server.py": marker + b" server",
            "admin_server.py": marker + b" admin",
            "workspace-launcher.py": b"launcher",
            "workspace_control.py": b"control",
            "workspace_authority.py": b"authority",
            "workspace_activation_host.py": Path(MODULE / "workspace_activation_host.py").read_bytes()
                if (MODULE / "workspace_activation_host.py").exists() else b"pending",
        }
        for name in MANDATORY_RELEASE_FILES:
            payloads.setdefault(name, (marker + b" " + name.encode()))
        manifest = {}
        for name, data in payloads.items():
            manifest[name] = hashlib.sha256(data).hexdigest()
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
        release = self.releases / digest
        release.mkdir(mode=0o700)
        for name, expected in manifest.items():
            if name == "workspace_activation_host.py" and (MODULE / name).exists():
                data = (MODULE / name).read_bytes()
            else:
                data = payloads[name]
            self.assert_digest(data, expected)
            path = release / name
            path.write_bytes(data)
            path.chmod(0o600)
        (release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (release / "manifest.json").chmod(0o600)
        return release

    @staticmethod
    def assert_digest(data: bytes, expected: str):
        if hashlib.sha256(data).hexdigest() != expected:
            raise AssertionError("fixture digest mismatch")

    def write_profile(self, release: Path):
        value = {
            "config_version": 1,
            "control_plane": {"api_key": "file:/private/key", "base_url": "https://api.openai.com",
                              "tunnel_id": "tunnel-placeholder"},
            "mcp": {"commands": [{"channel": "main", "command":
                                     f"{self.interpreter} {release / 'server.py'}"}]},
            "health": {"listen_addr": "127.0.0.1:0", "url_file": "/private/url"},
            "log": {"file": "/private/log", "format": "json", "level": "info"},
            "admin_ui": {"open_browser": False},
        }
        self.profile.write_text(json.dumps(value))
        self.profile.chmod(0o600)

    def tunnel_unit(self):
        command = f"{self.interpreter} {self.rollback / 'server.py'}"
        base = ("[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                f"ExecStart={self.client} runtimes connect --alias {ALIAS} --profile {ALIAS} "
                f"--profile-dir {self.profile.parent} --tunnel-id tunnel-placeholder "
                f"--runtime-api-key file:{self.home / '.config/key'} "
                "--control-plane-base-url https://api.openai.com "
                f"--mcp-command '{command}' --json\n"
                f"ExecStop={self.client} runtimes stop {ALIAS}\n")
        return (base + (self.tunnel_override.read_text() if self.tunnel_override.exists() else "")
                + getattr(self, "unit_tail", ""))

    def admin_unit(self):
        base = ("[Service]\n" f"ExecStart={self.interpreter} "
                f"{self.rollback / 'admin_server.py'} --port 8765\n")
        return base + (self.admin_override.read_text() if self.admin_override.exists() else "")

    def tunnel_status(self):
        if self.tunnel_pid == 0:
            return {"process": None, "process_running": False,
                    "healthy": False, "ready": False}
        return {"process": {"pid": self.tunnel_pid}, "process_running": True,
                "healthy": True, "ready": True}

    def write_process(self, pid: int, ppid: int, ticks: int, argv: list[str], cgroup: str):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        fields = [str(pid), "(fixture)", "S", str(ppid), *(["0"] * 17), str(ticks)]
        (folder / "stat").write_text(" ".join(fields) + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(os.fsencode(item) for item in argv) + b"\0")
        (folder / "cgroup").write_text(f"0::/{cgroup}\n")

    def remove_process(self, pid: int):
        folder = self.proc / str(pid)
        if folder.exists():
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()

    def command(self, argv, **_kwargs):
        self.calls.append(list(argv))
        action = argv[2]
        if action == "stop":
            unit = argv[3]
            old = self.units[unit]["main_pid"]
            if unit == "personal-ops-tunnel.service":
                self.remove_process(self.tunnel_pid)
                self.remove_process(4101)
                self.remove_process(5101)
                self.tunnel_pid = 0
            else:
                self.remove_process(old)
            self.units[unit].update(active_state="inactive", sub_state="dead", main_pid=0,
                                    control_group="")
            if unit == "personal-ops-tunnel.service" and self.reappear_after_tunnel_stop:
                self.workers = [{"kind": "host", "id": "late"}]
        elif action == "start":
            unit = argv[3]
            if unit == "personal-ops-admin.service" and self.fail_admin_start_once:
                self.fail_admin_start_once = False
                raise ActivationHostError("SYSTEMD_COMMAND_FAILED")
            pid = 5100 if unit == "personal-ops-tunnel.service" else 5200
            entry = "server.py" if "tunnel" in unit else "admin_server.py"
            launcher = str(self.controller / "workspace-launcher.py")
            args = [str(self.interpreter), launcher, "--authority-dir", str(self.authority),
                    "--release", str(self.target), "--interpreter", str(self.interpreter),
                    "--entrypoint", entry]
            if entry == "admin_server.py":
                args += ["--", "--port", "8765"]
            main_args = ([str(self.client), "daemon"]
                         if unit == "personal-ops-tunnel.service" else args)
            if unit == "personal-ops-admin.service":
                main_args = [str(self.interpreter), str(self.target / "admin_server.py"),
                             "--port", "8765"]
            self.write_process(pid, 1, pid * 10, main_args, "user.slice/" + unit)
            if unit == "personal-ops-tunnel.service":
                self.tunnel_pid = pid
                self.write_process(
                    5101, pid, 51010,
                    [str(self.interpreter), str(self.target / "server.py")],
                    "user.slice/" + unit)
            self.units[unit].update(active_state="active", sub_state="exited" if "tunnel" in unit else "running",
                                    main_pid=0 if "tunnel" in unit else pid,
                                    control_group="/user.slice/" + unit)
        return b""

    def unit_state(self, unit: str):
        return dict(self.units[unit])

    def inventory(self):
        return {"workers": list(self.workers), "locked_leases": list(self.locked_leases),
                "observed_kinds": ["agent", "command", "host", "job", "registered", "rollout"]}

    def acquire_rollout_lock(self):
        descriptor = os.open(
            self.rollout_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor

    def adapter(self, **overrides):
        arguments = dict(
            releases=self.releases, interpreter=self.interpreter,
            profile_path=self.profile, tunnel_override_path=self.tunnel_override,
            admin_override_path=self.admin_override, client=self.client,
            tunnel_unit_fn=self.tunnel_unit, admin_unit_fn=self.admin_unit,
            tunnel_status_fn=self.tunnel_status, command_fn=self.command,
            unit_state_fn=self.unit_state, process_root=self.proc,
            boot_id_fn=lambda: self.boot_id, worker_inventory_fn=self.inventory,
            admin_health_fn=lambda port: port == 8765,
            approved_interpreters=(self.interpreter,), source_root=MODULE,
        )
        arguments.update(overrides)
        return SystemdActivationHost(**arguments)


class SystemdActivationHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = HostFixture(Path(self.temp.name))
        self.fx.rollout_lock_fd = self.fx.acquire_rollout_lock()
        self.addCleanup(os.close, self.fx.rollout_lock_fd)

    def prepare(self):
        return self.fx.adapter().prepare(self.fx.target, self.fx.rollback,
                                         self.fx.authority, self.fx.controller)

    def bind(self, plan, *, initialize=False):
        manifest = json.loads((self.fx.target / "manifest.json").read_bytes())
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.fx.authority.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.fx.authority.parent, *self.fx.authority.parent.parents):
            if directory.is_relative_to(self.fx.root):
                directory.chmod(0o700)
        authority = WorkspaceAuthority({"demo": self.fx.root / "project"}, self.fx.authority)
        binding = (authority.initialize("b" * 64, {digest: 1}) if initialize else {
            "installation_id": "a" * 32, "authority_epoch": 1, "policy_digest": "b" * 64})
        plan["expected_binding"] = {
            "release_digest": digest, "release_abi": 1,
            "runtime": str(self.fx.target),
            "binding": binding,
        }
        return plan

    def committed_reboot(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        self.fx.boot_id = "87654321-4321-4321-4321-cba987654321"
        return adapter, plan

    def test_prepare_pins_actual_units_processes_and_generated_launcher_argv(self):
        plan = self.prepare()
        json.dumps(plan)
        self.assertEqual(plan["boot_id"], self.fx.boot_id)
        self.assertEqual(plan["services"]["tunnel"]["main"]["start_ticks"], 100)
        self.assertEqual(plan["services"]["tunnel"]["children"][0]["start_ticks"], 101)
        self.assertEqual(plan["services"]["tunnel"]["children"][0]["boot_id"], self.fx.boot_id)
        self.assertEqual(plan["services"]["admin"]["main"]["start_ticks"], 200)
        self.assertNotIn("argv", plan["services"]["tunnel"]["main"])
        self.assertRegex(plan["services"]["tunnel"]["main"]["argv_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(plan["target_snapshot"], self.fx.target.name)
        self.assertEqual(plan["target_argv"]["tunnel"][1],
                         str(self.fx.controller / "workspace-launcher.py"))
        self.assertNotIn("profile_bytes", plan)
        self.assertNotIn("unit_bytes", json.dumps(plan))

    def test_recover_committed_plan_rebases_only_boot_and_absent_service_observations(self):
        adapter, plan = self.committed_reboot()
        original = copy.deepcopy(plan)
        calls_before = list(self.fx.calls)
        self.fx.write_process(4100, 1, 999, ["unrelated"], "user.slice/other")
        self.fx.write_process(4101, 4100, 998, ["unrelated-child"], "user.slice/other")
        self.fx.write_process(4200, 1, 997, ["unrelated-admin"], "user.slice/other")

        recovered = adapter.recover_committed_plan(plan)

        json.dumps(recovered)
        self.assertEqual(plan, original)
        self.assertEqual(self.fx.calls, calls_before)
        self.assertEqual(recovered["boot_id"], self.fx.boot_id)
        self.assertEqual(recovered["services"], {
            "tunnel": {
                "unit": "personal-ops-tunnel.service",
                "state": self.fx.units["personal-ops-tunnel.service"],
                "main": None,
                "children": [],
            },
            "admin": {
                "unit": "personal-ops-admin.service",
                "state": self.fx.units["personal-ops-admin.service"],
                "main": None,
                "children": [],
            },
        })
        for key in original:
            if key not in {"boot_id", "services", "plan_sha256"}:
                self.assertEqual(recovered[key], original[key], key)
        expected_digest = hashlib.sha256(json.dumps(
            {key: value for key, value in recovered.items()
             if key not in {"plan_sha256", "expected_binding"}},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(recovered["plan_sha256"], expected_digest)

    def test_recover_committed_plan_refuses_live_service_or_tunnel(self):
        adapter, plan = self.committed_reboot()
        self.fx.units["personal-ops-admin.service"].update(
            active_state="active", sub_state="running", main_pid=9000,
            control_group="/user.slice/admin")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "SERVICE_NOT_DRAINED")

        self.fx.units["personal-ops-admin.service"].update(
            active_state="inactive", sub_state="dead", main_pid=0, control_group="")
        self.fx.write_process(4100, 1, 100, [str(self.fx.client), "daemon"],
                              "user.slice/tunnel")
        self.fx.write_process(
            4101, 4100, 101,
            [str(self.fx.interpreter), str(self.fx.rollback / "server.py")],
            "user.slice/tunnel")
        self.fx.tunnel_pid = 4100
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "TUNNEL_RUNTIME_STILL_LIVE")

    def test_recover_committed_plan_refuses_workers_and_leases(self):
        adapter, plan = self.committed_reboot()
        self.fx.workers = [{"kind": "registered", "id": "worker-1"}]
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "ACTIVE_WORKERS")

        self.fx.workers = []
        self.fx.locked_leases = [{"kind": "command", "id": "lease-1"}]
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "ACTIVE_LEASES")

    def test_recover_committed_plan_refuses_changed_wiring_or_binding(self):
        adapter, plan = self.committed_reboot()
        canonical_profile = self.fx.profile.read_bytes()
        profile = json.loads(self.fx.profile.read_bytes())
        profile["health"]["url_file"] = "/private/drifted-url"
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")

        self.fx.profile.write_bytes(canonical_profile)
        plan["expected_binding"]["binding"]["authority_epoch"] += 1
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "AUTHORITY_BINDING_MISMATCH")

    def test_precommit_plan_boot_drift_remains_invalid(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        self.fx.boot_id = "87654321-4321-4321-4321-cba987654321"
        with self.assertRaises(ActivationHostError) as caught:
            adapter.stop(plan)
        self.assertEqual(caught.exception.code, "ACTIVATION_PLAN_INVALID")
        self.assertEqual(self.fx.calls, [])

    def test_stop_blocks_active_worker_or_locked_lease_before_touching_systemd(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        for inventory, code in (({"workers": [{"kind": "job", "id": "j1"}], "locked_leases": []},
                                 "ACTIVE_WORKERS"),
                                ({"workers": [], "locked_leases": [{"kind": "agent", "id": "a1"}]},
                                 "ACTIVE_LEASES")):
            with self.subTest(code=code):
                self.fx.workers = inventory["workers"]
                self.fx.locked_leases = inventory["locked_leases"]
                with self.assertRaises(ActivationHostError) as caught:
                    adapter.stop(plan)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.fx.calls, [])

    def test_prepare_requires_inventory_coverage_for_every_known_worker_kind(self):
        adapter = self.fx.adapter(worker_inventory_fn=lambda: {
            "workers": [], "locked_leases": [], "observed_kinds": ["agent"],
        })
        with self.assertRaises(ActivationHostError) as caught:
            adapter.prepare(self.fx.target, self.fx.rollback,
                            self.fx.authority, self.fx.controller)
        self.assertEqual(caught.exception.code, "WORKER_INVENTORY_INCOMPLETE")

    def test_verify_drained_rejects_pid_reuse_worker_reappearance_and_live_cgroup(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        self.fx.write_process(4100, 1, 999, ["unrelated"], "user.slice/other")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_drained(plan)
        self.assertEqual(caught.exception.code, "SERVICE_PID_REUSED")
        self.fx.remove_process(4100)
        self.fx.workers = [{"kind": "rollout", "id": "r1"}]
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_drained(plan)
        self.assertEqual(caught.exception.code, "ACTIVE_WORKERS")
        self.fx.workers = []
        self.fx.units["personal-ops-admin.service"]["control_group"] = "/user.slice/stale"
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_drained(plan)
        self.assertEqual(caught.exception.code, "SERVICE_SCOPE_LIVE")

    def test_verify_drained_reconciles_live_tunnel_behind_inactive_unit(self):
        self.fx.remove_process(4200)
        for state in self.fx.units.values():
            state.update(active_state="inactive", sub_state="dead", main_pid=0,
                         control_group="")
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        self.assertEqual(plan["services"]["tunnel"]["main"]["pid"], 4100)
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_drained(plan)
        self.assertEqual(caught.exception.code, "TUNNEL_RUNTIME_STILL_LIVE")

    def test_verify_drained_rejects_unreadable_tunnel_status(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)

        def unreadable_status():
            raise OSError("status unavailable")

        adapter.tunnel_status_fn = unreadable_status
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_drained(plan)
        self.assertEqual(caught.exception.code, "TUNNEL_RUNTIME_STATUS_UNAVAILABLE")

    def test_worker_reappearance_between_stops_prevents_second_service_stop(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        self.fx.reappear_after_tunnel_stop = True
        with self.assertRaises(ActivationHostError) as caught:
            adapter.stop(plan)
        self.assertEqual(caught.exception.code, "ACTIVE_WORKERS")
        self.assertEqual([call[3] for call in self.fx.calls if call[2] == "stop"],
                         ["personal-ops-tunnel.service"])

    def test_wire_is_cas_guarded_and_partial_resume_is_idempotent(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.verify_drained(plan)
        self.fx.profile.write_bytes(self.fx.profile.read_bytes() + b"\n")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.wire(plan)
        self.assertEqual(caught.exception.code, "HOST_CONFIGURATION_CHANGED")

        self.fx.write_profile(self.fx.rollback)
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        # prepare is also valid when both units are already stopped.
        adapter.wire(plan)
        first = (self.fx.profile.read_bytes(), self.fx.tunnel_override.read_bytes(),
                 self.fx.admin_override.read_bytes())
        adapter.wire(plan)
        self.assertEqual(first, (self.fx.profile.read_bytes(), self.fx.tunnel_override.read_bytes(),
                                 self.fx.admin_override.read_bytes()))
        self.assertTrue(adapter.verify_wired(plan)["verified"])

    def test_verify_wired_accepts_semantically_identical_profile_serialization(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        canonical = self.fx.profile.read_bytes()
        profile = json.loads(canonical)
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")

        self.assertNotEqual(self.fx.profile.read_bytes(), canonical)
        self.assertTrue(adapter.verify_wired(plan)["verified"])

    def test_verify_wired_rejects_profile_data_drift_after_reserialization(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        profile = json.loads(self.fx.profile.read_bytes())
        profile["health"]["url_file"] = "/private/drifted-url"
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_wired(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")

        profile["health"]["url_file"] = "/private/url"
        profile["mcp"]["commands"][0]["command"] = "/tmp/unrelated"
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_wired(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")

    def test_postwire_unit_context_drift_blocks_verify_start_and_reboot_recovery(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        self.fx.unit_tail = "\n[Service]\nEnvironment=DRIFT=1\n"
        calls_before = list(self.fx.calls)

        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_wired(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")
        self.assertEqual(self.fx.calls, calls_before)
        self.fx.boot_id = "87654321-4321-4321-4321-cba987654321"
        with self.assertRaises(ActivationHostError) as caught:
            adapter.recover_committed_plan(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")
        self.assertEqual(self.fx.calls, calls_before)
        self.fx.boot_id = plan["boot_id"]
        with self.assertRaises(ActivationHostError) as caught:
            adapter.start(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")
        self.assertEqual(self.fx.calls, calls_before)

    def test_json_persisted_plan_completes_lifecycle_and_reboot_recovery(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        plan = json.loads(json.dumps(plan))

        adapter.stop(plan)
        adapter.wire(plan)
        self.assertTrue(adapter.verify_wired(plan)["verified"])
        self.bind(plan, initialize=True)
        self.assertTrue(adapter.start(plan)["verified"])
        self.assertTrue(adapter.verify_started(plan)["verified"])

        for pid in (5100, 5101, 5200):
            self.fx.remove_process(pid)
        self.fx.tunnel_pid = 0
        for state in self.fx.units.values():
            state.update(active_state="inactive", sub_state="dead", main_pid=0,
                         job="", control_group="")
        self.fx.boot_id = "87654321-4321-4321-4321-cba987654321"
        profile = json.loads(self.fx.profile.read_bytes())
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")

        recovered = adapter.recover_committed_plan(plan)

        self.assertEqual(recovered["boot_id"], self.fx.boot_id)
        self.assertTrue(adapter.verify_wired(recovered)["verified"])

    def test_wire_requires_process_held_canonical_global_rollout_lock(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        fcntl.flock(self.fx.rollout_lock_fd, fcntl.LOCK_UN)
        with self.assertRaises(ActivationHostError) as caught:
            adapter.wire(plan)
        self.assertEqual(caught.exception.code, "GLOBAL_ROLLOUT_LOCK_REQUIRED")

    def test_wire_rechecks_rollout_lock_after_reobserving_configuration(self):
        reads = 0

        def tunnel_unit_then_drop_lock():
            nonlocal reads
            reads += 1
            value = self.fx.tunnel_unit()
            if reads == 2:
                fcntl.flock(self.fx.rollout_lock_fd, fcntl.LOCK_UN)
            return value

        adapter = self.fx.adapter(tunnel_unit_fn=tunnel_unit_then_drop_lock)
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        with self.assertRaises(ActivationHostError) as caught:
            adapter.wire(plan)
        self.assertEqual(caught.exception.code, "GLOBAL_ROLLOUT_LOCK_REQUIRED")
        self.assertFalse(self.fx.tunnel_override.exists())
        self.assertFalse(self.fx.admin_override.exists())

    def test_plan_cannot_redirect_writes_or_accept_semantic_unit_drift(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        redirected = dict(plan)
        redirected["paths"] = dict(plan["paths"], profile=str(self.fx.root / "other"))
        with self.assertRaises(ActivationHostError) as caught:
            adapter.wire(redirected)
        self.assertEqual(caught.exception.code, "ACTIVATION_PLAN_INVALID")

        self.fx.unit_tail = "\n[Service]\nEnvironment=DRIFT=1\n"
        with self.assertRaises(ActivationHostError) as caught:
            adapter.wire(plan)
        self.assertEqual(caught.exception.code, "HOST_CONFIGURATION_CHANGED")

    def test_start_requires_committed_authority_then_verifies_release_and_health(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.verify_drained(plan)
        adapter.wire(plan)
        adapter.verify_wired(plan)
        self.bind(plan)
        with self.assertRaises(ActivationHostError) as caught:
            adapter.start(plan)
        self.assertEqual(caught.exception.code, "WORKSPACE_ENFORCEMENT_REQUIRED")

        manifest = json.loads((self.fx.target / "manifest.json").read_text())
        release_digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        authority = WorkspaceAuthority(
            {"demo": self.fx.root / "project"}, state_dir=self.fx.authority)
        authority.initialize("b" * 64, {release_digest: 1})
        plan["expected_binding"] = {
            key: value for key, value in verify_launch(
                self.fx.target, self.fx.authority, enforcement_required=True).items()
            if key in {"release_digest", "release_abi", "runtime", "binding"}
        }
        adapter.start(plan)
        result = adapter.verify_started(plan)
        self.assertTrue(result["verified"])
        self.assertEqual(result["snapshot"], self.fx.target.name)
        self.assertEqual(result["binding"]["binding"]["authority_epoch"], 1)
        self.assertEqual(result["services"]["admin"]["main"]["argv"],
                         [str(self.fx.interpreter),
                          str(self.fx.target / "admin_server.py"), "--port", "8765"])
        self.assertEqual(result["tunnel_child"]["argv"],
                         [str(self.fx.interpreter), str(self.fx.target / "server.py")])
        profile = json.loads(self.fx.profile.read_bytes())
        profile["log"]["level"] = "debug"
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_started(plan)
        self.assertEqual(caught.exception.code, "HOST_WIRING_MISMATCH")

    def test_start_waits_for_exact_launcher_processes_to_exec_target_release(self):
        plan = self.prepare()
        self.fx.adapter().stop(plan)
        self.fx.adapter().wire(plan)
        self.bind(plan, initialize=True)
        now = [0.0]
        sleeps = []

        def delayed_start(argv, **kwargs):
            result = self.fx.command(argv, **kwargs)
            if argv[2:4] == ["start", "personal-ops-admin.service"]:
                self.fx.write_process(5200, 1, 52000, plan["target_argv"]["admin"],
                                      "user.slice/personal-ops-admin.service")
            return result

        def finish_exec(delay):
            sleeps.append(delay)
            now[0] += delay
            admin = self.fx.proc / "5200" / "cmdline"
            if admin.exists() and plan["target_argv"]["admin"][1].encode() in admin.read_bytes():
                self.fx.write_process(5200, 1, 52000, plan["exec_argv"]["admin"],
                                      "user.slice/personal-ops-admin.service")

        adapter = self.fx.adapter(command_fn=delayed_start, sleep_fn=finish_exec,
                                  clock_fn=lambda: now[0])

        self.assertTrue(adapter.start(plan)["verified"])
        self.assertEqual(len(sleeps), 1)

    def test_start_does_not_retry_unrelated_admin_argv(self):
        plan = self.prepare()
        adapter = self.fx.adapter()
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        sleeps = []

        def malicious_start(argv, **kwargs):
            result = self.fx.command(argv, **kwargs)
            if argv[2:4] == ["start", "personal-ops-admin.service"]:
                self.fx.write_process(5200, 1, 52000,
                                      [str(self.fx.interpreter), "/tmp/unrelated.py"],
                                      "user.slice/personal-ops-admin.service")
            return result

        adapter = self.fx.adapter(command_fn=malicious_start,
                                  sleep_fn=lambda delay: sleeps.append(delay))
        with self.assertRaises(ActivationHostError) as caught:
            adapter.start(plan)
        self.assertEqual(caught.exception.code, "STARTED_RELEASE_MISMATCH")
        self.assertEqual(sleeps, [])

    def test_start_rejects_pid_reuse_during_launcher_exec_transition(self):
        plan = self.prepare()
        adapter = self.fx.adapter()
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        now = [0.0]

        def delayed_admin(argv, **kwargs):
            result = self.fx.command(argv, **kwargs)
            if argv[2:4] == ["start", "personal-ops-admin.service"]:
                self.fx.write_process(5200, 1, 52000, plan["target_argv"]["admin"],
                                      "user.slice/personal-ops-admin.service")
            return result

        def reuse_pid(delay):
            now[0] += delay
            self.fx.write_process(5200, 1, 99999, plan["exec_argv"]["admin"],
                                  "user.slice/personal-ops-admin.service")

        adapter = self.fx.adapter(command_fn=delayed_admin, sleep_fn=reuse_pid,
                                  clock_fn=lambda: now[0])
        with self.assertRaises(ActivationHostError) as caught:
            adapter.start(plan)
        self.assertEqual(caught.exception.code, "SERVICE_IDENTITY_CHANGED")

    def test_start_times_out_while_exact_launcher_never_execs(self):
        plan = self.prepare()
        adapter = self.fx.adapter()
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        now = [0.0]
        sleeps = []

        def delayed_admin(argv, **kwargs):
            result = self.fx.command(argv, **kwargs)
            if argv[2:4] == ["start", "personal-ops-admin.service"]:
                self.fx.write_process(5200, 1, 52000, plan["target_argv"]["admin"],
                                      "user.slice/personal-ops-admin.service")
            return result

        def advance(delay):
            sleeps.append(delay)
            now[0] += delay

        adapter = self.fx.adapter(command_fn=delayed_admin, sleep_fn=advance,
                                  clock_fn=lambda: now[0], startup_timeout=0.1)
        with self.assertRaises(ActivationHostError) as caught:
            adapter.start(plan)
        self.assertEqual(caught.exception.code, "STARTED_RELEASE_MISMATCH")
        self.assertGreaterEqual(len(sleeps), 1)

    def test_exact_launcher_is_pending_only_during_start_convergence(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        adapter.start(plan)
        self.fx.write_process(5200, 1, 52000, plan["target_argv"]["admin"],
                              "user.slice/personal-ops-admin.service")

        with self.assertRaises(ActivationHostError) as caught:
            adapter.verify_started(plan)
        self.assertEqual(caught.exception.code, "STARTED_RELEASE_MISMATCH")
        with self.assertRaises(ActivationHostError) as caught:
            adapter.stop_started(plan)
        self.assertEqual(caught.exception.code, "STARTED_RELEASE_MISMATCH")

    def test_start_resumes_after_tunnel_started_but_admin_start_was_interrupted(self):
        adapter = self.fx.adapter()
        plan = adapter.prepare(self.fx.target, self.fx.rollback,
                               self.fx.authority, self.fx.controller)
        adapter.stop(plan)
        adapter.wire(plan)
        self.bind(plan, initialize=True)
        self.fx.fail_admin_start_once = True
        with self.assertRaises(ActivationHostError):
            adapter.start(plan)
        self.assertEqual(self.fx.units["personal-ops-tunnel.service"]["active_state"],
                         "active")
        profile = json.loads(self.fx.profile.read_bytes())
        self.fx.profile.write_text(json.dumps(profile, indent=2) + "\n")
        stopped = adapter.stop_started(plan)
        self.assertTrue(stopped["verified"])
        self.assertEqual(self.fx.units["personal-ops-tunnel.service"]["active_state"],
                         "inactive")
        result = adapter.start(plan)
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
