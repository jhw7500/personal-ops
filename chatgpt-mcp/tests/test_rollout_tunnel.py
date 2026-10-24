"""Safe tunnel activation tests; no real services or host configuration are touched."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rollout_tunnel as module
from rollout_tunnel import (TunnelSwitchError, freeze_live_runtime,
                            preflight_tunnel, switch_tunnel)
from workspace_authority import WorkspaceAuthority
from workspace_control import MANDATORY_RELEASE_FILES, install_controller


ALIAS = "personal-ops-coding"
SERVICE = "personal-ops-tunnel.service"


class TunnelRolloutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        authority_default = patch(
            "workspace_control._default_authority_dir",
            return_value=self.home / "inactive-workspace-authority",
        )
        authority_default.start()
        self.addCleanup(authority_default.stop)
        self.releases = self.home / ".local/lib/chatgpt-coding-mcp/releases"
        self.releases.mkdir(parents=True, mode=0o700)
        self.releases.chmod(0o700)
        self.interpreter = self.home / ".local/share/uv/tools/serena-agent/bin/python"
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.write_text("fixture")
        self.interpreter.chmod(0o700)
        self.proc = self.home / "proc"
        self.proc.mkdir()
        self.client = self.home / ".local/bin/tunnel-client"
        self.client.parent.mkdir(parents=True)
        self.client.write_text("fixture")
        self.client.chmod(0o700)
        self.profile = self.home / ".config/tunnel-client/personal-ops-coding.yaml"
        self.profile.parent.mkdir(parents=True, mode=0o700)
        self.profile.parent.chmod(0o700)
        self.override = (self.home / ".config/systemd/user" / (SERVICE + ".d")
                         / "20-runtime.conf")
        self.override.parent.parent.mkdir(parents=True, mode=0o700)
        self.override.parent.parent.chmod(0o744)
        self.old = self.make_release(b"old")
        self.stale = self.make_release(b"stale")
        self.new = self.make_release(b"new")
        self.write_process(4100, 1, 100, [str(self.client), "daemon"])
        self.write_process(4101, 4100, 101,
                           [str(self.interpreter), str(self.old / "server.py")])
        self.status = {"process": {"pid": 4100}, "process_running": True,
                       "healthy": True, "ready": True}
        self.unit = self.unit_text(self.stale)
        self.write_profile(self.new)

    def make_release(self, server_content):
        contents = {
            name: ((server_content if name == "server.py" else
                    ("fixture:" + name).encode()))
            for name in MANDATORY_RELEASE_FILES
        }
        manifest = {name: hashlib.sha256(content).hexdigest()
                    for name, content in contents.items()}
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
        release = self.releases / digest
        release.mkdir(mode=0o700)
        for name, content in contents.items():
            path = release / name
            path.write_bytes(content)
            path.chmod(0o600)
        (release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (release / "manifest.json").chmod(0o600)
        return release

    def activate_authority(self, allowed_releases):
        project = self.home / "projects/demo"
        project.mkdir(parents=True, exist_ok=True)
        authority_dir = self.home / "workspace-authority"
        authority = WorkspaceAuthority({"demo": project}, authority_dir)
        authority.initialize("a" * 64, allowed_releases)
        self.controller_dir = self.home / "workspace-controller"
        install_controller(
            self.controller_dir, Path(__file__).resolve().parents[1],
            projects={"demo": project},
        )
        return authority_dir

    @staticmethod
    def release_digest(release):
        manifest = json.loads((release / "manifest.json").read_text())
        return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()

    def write_process(self, pid, ppid, started, argv):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        # /proc/<pid>/stat fields 1..22; starttime is field 22.
        fields = [str(pid), "(fixture process)", "S", str(ppid), *(["0"] * 17),
                  str(started)]
        (folder / "stat").write_text(" ".join(fields) + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(os.fsencode(v) for v in argv) + b"\0")

    def remove_process(self, pid):
        folder = self.proc / str(pid)
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()

    def unit_text(self, runtime):
        command = f"{self.interpreter} {runtime / 'server.py'}"
        return ("[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                f"ExecStart={self.client} runtimes connect --alias {ALIAS} "
                f"--profile {ALIAS} --profile-dir {self.profile.parent} "
                "--tunnel-id tunnel-placeholder "
                f"--runtime-api-key file:{self.home / '.config/key'} "
                "--control-plane-base-url https://api.openai.com --mcp-command "
                f"'{command}' --json\n"
                f"ExecStop={self.client} runtimes stop {ALIAS}\n")

    def write_profile(self, runtime):
        data = {
            "config_version": 1,
            "control_plane": {
                "api_key": "file:/private/key",
                "base_url": "https://api.openai.com",
                "tunnel_id": "tunnel-placeholder",
            },
            "mcp": {"commands": [{
                "channel": "main",
                "command": f"{self.interpreter} {runtime / 'server.py'}",
            }]},
            "health": {"listen_addr": "127.0.0.1:0", "url_file": "/private/url"},
            "log": {"file": "/private/log", "format": "json", "level": "info"},
            "admin_ui": {"open_browser": False},
        }
        self.profile.write_text(json.dumps(data))
        self.profile.chmod(0o600)

    def freeze(self, status=None):
        return freeze_live_runtime(status or self.status, releases=self.releases,
                                   proc_root=self.proc,
                                   approved_interpreters=(self.interpreter,))

    def plan(self):
        return preflight_tunnel(
            self.freeze(), unit_fn=self.read_unit, profile_path=self.profile,
            override_path=self.override, releases=self.releases, client=self.client,
            approved_interpreters=(self.interpreter,))

    def read_unit(self):
        result = self.unit
        if self.override.exists():
            result += "\n# runtime override\n" + self.override.read_text()
        return result + getattr(self, "unit_tail", "")

    def test_freeze_uses_the_actual_child_not_stale_healthy_profile_metadata(self):
        self.write_process(4102, 4100, 102, ["codex", "app-server"])
        live = self.freeze()
        self.assertEqual(live["snapshot"], self.old.name)
        self.assertEqual(live["tunnel_pid"], 4100)
        self.assertEqual(live["tunnel_start_ticks"], 100)
        self.assertEqual(live["mcp_pid"], 4101)
        self.assertEqual(live["mcp_start_ticks"], 101)
        self.assertEqual(live["codex_pid"], 4102)
        self.assertEqual(live["codex_start_ticks"], 102)

    def test_freeze_rejects_pid_reuse_and_ambiguous_direct_mcp_children(self):
        original = Path.read_text
        reads = 0

        def racing_read(path, *args, **kwargs):
            nonlocal reads
            value = original(path, *args, **kwargs)
            if path == self.proc / "4100/stat":
                reads += 1
                if reads > 1:
                    return value.replace(" 100\n", " 999\n")
            return value

        with patch.object(Path, "read_text", racing_read):
            with self.assertRaisesRegex(TunnelSwitchError, "PROCESS_IDENTITY_CHANGED"):
                self.freeze()

        self.write_process(4103, 4100, 103,
                           [str(self.interpreter), str(self.old / "server.py")])
        with self.assertRaisesRegex(TunnelSwitchError, "AMBIGUOUS_MCP_CHILD"):
            self.freeze()

    def test_freeze_rejects_a_release_whose_manifest_content_was_modified(self):
        (self.old / "server.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(TunnelSwitchError, "RUNTIME_MANIFEST_MISMATCH"):
            self.freeze()

    def test_preflight_preserves_secret_bearing_flags_and_profile_fields(self):
        before = json.loads(self.profile.read_text())
        plan = self.plan()
        self.assertEqual(plan["live"]["snapshot"], self.old.name)
        self.assertEqual(plan["unit_bytes"], self.unit.encode())
        self.assertEqual(plan["profile_data"]["control_plane"], before["control_plane"])
        self.assertEqual(plan["profile_data"]["mcp"]["commands"][0]["channel"], "main")
        self.assertFalse(plan["profile_aligned"])
        self.assertFalse(plan["service_aligned"])

    def test_profile_parser_rejects_duplicate_keys_and_nonfinite_values(self):
        original = self.profile.read_bytes()
        ambiguous = {
            "duplicate": original.replace(
                b'{"config_version": 1',
                b'{"config_version": 1, "config_version": 1',
                1,
            ),
            "nonfinite": original.replace(
                b'"config_version": 1', b'"config_version": NaN', 1),
        }
        for name, raw in ambiguous.items():
            with self.subTest(name=name):
                self.profile.write_bytes(raw)
                self.profile.chmod(0o600)
                with self.assertRaisesRegex(
                        TunnelSwitchError, "UNRECOGNIZED_TUNNEL_PROFILE"):
                    self.plan()

    def test_profile_semantics_distinguish_boolean_from_integer(self):
        plan = self.plan()
        changed = json.loads(self.profile.read_text())
        changed["config_version"] = True
        self.profile.write_text(json.dumps(changed, indent=2) + "\n")
        self.profile.chmod(0o600)
        state = {"profile": plan["profile_bytes"], "override": plan["override_bytes"]}
        with self.assertRaisesRegex(TunnelSwitchError, "TUNNEL_CONFIGURATION_CHANGED"):
            module._assert_resources(plan, state)

    def test_preflight_accepts_the_owned_tunnel_client_symlink(self):
        target = self.home / ".local/share/openai-tunnel/v1/tunnel-client"
        target.parent.mkdir(parents=True)
        target.write_text("fixture")
        target.chmod(0o700)
        self.client.unlink()
        self.client.symlink_to(target)
        plan = self.plan()
        self.assertEqual(plan["client"], self.client)

    def test_preflight_rejects_an_override_with_unrelated_directives(self):
        self.override.parent.mkdir(mode=0o700)
        self.override.write_text("[Service]\nEnvironment=KEEP=this\n")
        self.override.chmod(0o600)
        with self.assertRaisesRegex(TunnelSwitchError, "UNSUPPORTED_TUNNEL_OVERRIDE"):
            self.plan()

    def test_switch_rejects_a_changed_live_identity_before_writing(self):
        plan = self.plan()
        before_profile = self.profile.read_bytes()
        self.remove_process(4101)
        self.write_process(4101, 4100, 999,
                           [str(self.interpreter), str(self.old / "server.py")])

        def must_not_run(*args, **kwargs):
            self.fail("systemd must not run after the frozen identity changes")

        with self.assertRaisesRegex(TunnelSwitchError, "LIVE_RUNTIME_CHANGED"):
            switch_tunnel(self.new, interpreter=self.interpreter,
                          status_fn=lambda: self.status, command_fn=must_not_run,
                          plan=plan, releases=self.releases, proc_root=self.proc,
                          approved_interpreters=(self.interpreter,))
        self.assertEqual(self.profile.read_bytes(), before_profile)
        self.assertFalse(self.override.exists())

    def test_stop_wait_includes_the_tunnel_owned_codex_sibling(self):
        self.write_process(4102, 4100, 102, ["codex", "app-server"])
        plan = self.plan()

        def command(argv, **kwargs):
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            return b""

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=command, plan=plan,
                releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.cause_code,
                         "TUNNEL_STOP_IDENTITY_STILL_RUNNING")
        self.assertTrue((self.proc / "4102").exists())

    def test_unobserved_stale_successor_is_not_assumed_owned_for_rollback(self):
        plan = self.plan()
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4100, 1, 200 + len(calls), [str(self.client), "daemon"])
                # Both attempted activation and rollback report a healthy old child.
                self.write_process(4101, 4100, 201 + len(calls),
                                   [str(self.interpreter), str(self.old / "server.py")])
            return b""

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(self.new, interpreter=self.interpreter,
                          status_fn=lambda: self.status, command_fn=command, plan=plan,
                          releases=self.releases, proc_root=self.proc,
                          approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                          attempts=1)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.cause_code, "LIVE_RUNTIME_MISMATCH")
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertEqual(sum(cmd[2:4] == ["stop", SERVICE] for cmd in calls), 1)

    def test_switch_updates_only_mcp_targets_and_verifies_new_live_identity(self):
        plan = self.plan()
        before = json.loads(self.profile.read_text())
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4200, 1, 300, [str(self.client), "daemon"])
                self.write_process(4201, 4200, 301,
                                   [str(self.interpreter), str(self.new / "server.py")])
                self.status["process"]["pid"] = 4200
            return b""

        result = switch_tunnel(
            self.new, interpreter=self.interpreter, status_fn=lambda: self.status,
            command_fn=command, plan=plan, releases=self.releases,
            proc_root=self.proc, approved_interpreters=(self.interpreter,),
            sleep_fn=lambda _: None, attempts=1)
        self.assertEqual(result, {
            "status": "tunnel_ready", "snapshot": self.new.name,
            "tunnel_pid": 4200, "tunnel_start_ticks": 300,
            "mcp_pid": 4201, "mcp_start_ticks": 301,
            "server": str(self.new / "server.py"),
            "interpreter": str(self.interpreter),
        })
        after = json.loads(self.profile.read_text())
        expected = json.loads(json.dumps(before))
        expected["mcp"]["commands"][0]["command"] = (
            f"{self.interpreter} {self.new / 'server.py'}")
        self.assertEqual(after, expected)
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.override.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(self.new / "server.py"), self.override.read_text())
        self.assertTrue(any(argv[2:4] == ["stop", SERVICE] for argv in calls))
        self.assertTrue(any(argv[2:4] == ["start", SERVICE] for argv in calls))

    def test_transient_status_failure_is_retried_within_the_poll_deadline(self):
        plan = self.plan()
        calls = []
        status_calls = 0

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4200, 1, 300, [str(self.client), "daemon"])
                self.write_process(4201, 4200, 301,
                                   [str(self.interpreter), str(self.new / "server.py")])
                self.status["process"]["pid"] = 4200
            return b""

        def status():
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                raise RuntimeError("TUNNEL_RUNTIME_STATUS_FAILED")
            return self.status

        result = switch_tunnel(
            self.new, interpreter=self.interpreter, status_fn=status,
            command_fn=command, plan=plan, releases=self.releases,
            proc_root=self.proc, approved_interpreters=(self.interpreter,),
            sleep_fn=lambda _: None, attempts=3, poll_timeout=5)
        self.assertEqual(result["snapshot"], self.new.name)
        self.assertEqual(status_calls, 3)

    def test_rollout_termination_before_observation_does_not_claim_rollback(self):
        plan = self.plan()
        starts = 0
        status_calls = 0

        class RolloutTerminated(RuntimeError):
            code = "ROLLOUT_TERMINATED"

        def command(argv, **kwargs):
            nonlocal starts
            if argv[2:4] == ["stop", SERVICE]:
                for pid in (4101, 4201, 4100, 4200):
                    if (self.proc / str(pid)).exists():
                        self.remove_process(pid)
            elif argv[2:4] == ["start", SERVICE]:
                starts += 1
                runtime = self.new if starts == 1 else self.old
                pid = 4200 if starts == 1 else 4100
                self.write_process(pid, 1, 600 + starts, [str(self.client), "daemon"])
                self.write_process(pid + 1, pid, 610 + starts,
                                   [str(self.interpreter), str(runtime / "server.py")])
                self.status["process"]["pid"] = pid
            return b""

        def status():
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                raise RolloutTerminated("terminated")
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.cause_code, "ROLLOUT_TERMINATED")
        self.assertEqual(starts, 1)

    def test_keyboard_interrupt_after_own_writes_restores_original_config(self):
        plan = self.plan()
        profile_before = self.profile.read_bytes()
        calls = 0

        def interrupted(argv, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return b""

        with self.assertRaises(KeyboardInterrupt):
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=lambda: self.status,
                command_fn=interrupted, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,))
        self.assertEqual(self.profile.read_bytes(), profile_before)
        self.assertFalse(self.override.exists())

    def test_operator_profile_change_during_failure_is_not_overwritten(self):
        plan = self.plan()
        operator_profile = None
        status_calls = 0

        def command(argv, **kwargs):
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4100, 1, 300, [str(self.client), "daemon"])
                self.write_process(4101, 4100, 301,
                                   [str(self.interpreter), str(self.old / "server.py")])
            return b""

        def status():
            nonlocal status_calls, operator_profile
            status_calls += 1
            if status_calls == 2:
                data = json.loads(self.profile.read_text())
                data["log"]["level"] = "debug"
                self.profile.write_text(json.dumps(data))
                self.profile.chmod(0o600)
                operator_profile = self.profile.read_bytes()
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertEqual(self.profile.read_bytes(), operator_profile)

    def test_semantic_profile_reserialization_allows_discovery_failure_rollback(self):
        plan = self.plan()
        starts = 0

        def command(argv, **kwargs):
            nonlocal starts
            if argv[2:4] == ["stop", SERVICE]:
                for pid in (4101, 4100, 4201, 4200, 4301, 4300):
                    if (self.proc / str(pid)).exists():
                        self.remove_process(pid)
            elif argv[2:4] == ["start", SERVICE]:
                starts += 1
                runtime = self.new if starts == 1 else self.old
                pid = 4200 if starts == 1 else 4300
                self.write_process(pid, 1, 300 + starts, [str(self.client), "daemon"])
                self.write_process(pid + 1, pid, 400 + starts,
                                   [str(self.interpreter), str(runtime / "server.py")])
                self.status["process"]["pid"] = pid
                profile = json.loads(self.profile.read_text())
                self.profile.write_text(json.dumps(profile, indent=2) + "\n")
                self.profile.chmod(0o600)
            return b""

        def discovery_failure(_):
            raise TunnelSwitchError("DISCOVERY_FAILED")

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=command, plan=plan,
                releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                attempts=1, poll_timeout=5, verify_fn=discovery_failure,
            )
        self.assertEqual(raised.exception.code, "TUNNEL_SWITCH_FAILED")
        self.assertEqual(raised.exception.cause_code, "DISCOVERY_FAILED")
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertEqual(starts, 2)
        restored = json.loads(self.profile.read_text())
        self.assertEqual(
            restored["mcp"]["commands"][0]["command"],
            f"{self.interpreter} {self.old / 'server.py'}",
        )

    def test_operator_override_change_during_failure_is_not_overwritten(self):
        plan = self.plan()
        operator_override = None
        status_calls = 0

        def command(argv, **kwargs):
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4100, 1, 300, [str(self.client), "daemon"])
                self.write_process(4101, 4100, 301,
                                   [str(self.interpreter), str(self.old / "server.py")])
            return b""

        def status():
            nonlocal status_calls, operator_override
            status_calls += 1
            if status_calls == 2:
                self.override.write_text("[Service]\nEnvironment=OPERATOR=yes\n")
                self.override.chmod(0o600)
                operator_override = self.override.read_bytes()
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(self.override.read_bytes(), operator_override)

    def test_operator_unit_change_during_failure_blocks_more_service_actions(self):
        plan = self.plan()
        calls = []
        status_calls = 0

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4100, 1, 300, [str(self.client), "daemon"])
                self.write_process(4101, 4100, 301,
                                   [str(self.interpreter), str(self.old / "server.py")])
            return b""

        def status():
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                start = next(line for line in self.unit.splitlines()
                             if line.startswith("ExecStart="))
                self.unit_tail = ("\n[Service]\nExecStart=\n"
                                  + start.replace("--json", "--json --operator-change")
                                  + "\n")
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(sum(argv[2:4] == ["stop", SERVICE] for argv in calls), 1)

    def test_operator_non_execstart_unit_change_is_a_rollback_conflict(self):
        plan = self.plan()
        calls = []
        status_calls = 0

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4100, 1, 300, [str(self.client), "daemon"])
                self.write_process(4101, 4100, 301,
                                   [str(self.interpreter), str(self.old / "server.py")])
            return b""

        def status():
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                self.unit_tail = "\n[Service]\nEnvironment=NEW_OPERATOR_SETTING=yes\n"
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(sum(argv[2:4] == ["stop", SERVICE] for argv in calls), 1)

    def test_partial_initial_write_restores_only_unchanged_owned_profile(self):
        plan = self.plan()
        profile_before = self.profile.read_bytes()
        operator_override = b"[Service]\nEnvironment=OPERATOR=yes\n"
        real_cas = module._cas_write

        def conflict(path, expected, value):
            if path == self.override:
                self.override.write_bytes(operator_override)
                self.override.chmod(0o600)
            return real_cas(path, expected, value)

        with patch.object(module, "_cas_write", side_effect=conflict):
            with self.assertRaises(TunnelSwitchError):
                switch_tunnel(
                    self.new, interpreter=self.interpreter,
                    status_fn=lambda: self.status,
                    command_fn=lambda *args, **kwargs: self.fail("no service action"),
                    plan=plan, releases=self.releases, proc_root=self.proc,
                    approved_interpreters=(self.interpreter,))
        self.assertEqual(self.profile.read_bytes(), profile_before)
        self.assertEqual(self.override.read_bytes(), operator_override)

    def test_unexpected_third_runtime_is_not_stopped_during_rollback(self):
        third = self.make_release(b"third")
        plan = self.plan()
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4300, 1, 400, [str(self.client), "daemon"])
                self.write_process(4301, 4300, 401,
                                   [str(self.interpreter), str(third / "server.py")])
                self.status["process"]["pid"] = 4300
            return b""

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=lambda: self.status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=1, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(sum(argv[2:4] == ["stop", SERVICE] for argv in calls), 1)

    def test_manifest_tamper_is_not_retried_as_a_transient_status_error(self):
        plan = self.plan()
        status_calls = 0

        def command(argv, **kwargs):
            if argv[2:4] == ["stop", SERVICE]:
                self.remove_process(4101)
                self.remove_process(4100)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4200, 1, 700, [str(self.client), "daemon"])
                self.write_process(4201, 4200, 701,
                                   [str(self.interpreter), str(self.new / "server.py")])
                self.status["process"]["pid"] = 4200
                (self.new / "server.py").write_bytes(b"tampered")
            return b""

        def status():
            nonlocal status_calls
            status_calls += 1
            return self.status

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter, status_fn=status,
                command_fn=command, plan=plan, releases=self.releases,
                proc_root=self.proc, approved_interpreters=(self.interpreter,),
                sleep_fn=lambda _: None, attempts=9, poll_timeout=5)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.cause_code, "RUNTIME_MANIFEST_MISMATCH")
        self.assertEqual(status_calls, 3)

    def test_verification_callback_failure_rolls_back_before_returning(self):
        plan = self.plan()
        authority_dir = self.activate_authority({
            self.release_digest(self.old): 1,
            self.release_digest(self.new): 2,
        })
        starts = 0

        def command(argv, **kwargs):
            nonlocal starts
            if argv[2:4] == ["stop", SERVICE]:
                if (self.proc / "4101").exists():
                    self.remove_process(4101)
                if (self.proc / "4201").exists():
                    self.remove_process(4201)
                if (self.proc / "4100").exists():
                    self.remove_process(4100)
                if (self.proc / "4200").exists():
                    self.remove_process(4200)
            elif argv[2:4] == ["start", SERVICE]:
                starts += 1
                runtime = self.new if starts == 1 else self.old
                pid = 4200 if starts == 1 else 4100
                self.write_process(pid, 1, 500 + starts, [str(self.client), "daemon"])
                self.write_process(pid + 1, pid, 510 + starts,
                                   [str(self.interpreter), str(runtime / "server.py")])
                self.status["process"]["pid"] = pid
            return b""

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=command, plan=plan,
                releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                attempts=1, poll_timeout=5,
                authority_dir=authority_dir, controller_dir=self.controller_dir,
                verify_fn=lambda actual: (_ for _ in ()).throw(
                    RuntimeError("TOOL_CATALOG_MISMATCH")))
        self.assertEqual(raised.exception.code, "TUNNEL_SWITCH_FAILED")
        self.assertEqual(raised.exception.cause_code, "TOOL_CATALOG_MISMATCH")
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertEqual(starts, 2)
        authority = WorkspaceAuthority(
            {"demo": self.home / "projects/demo"}, authority_dir,
        )
        self.assertEqual(authority.binding("a" * 64)["authority_epoch"], 3)

    def test_active_authority_blocks_unreviewed_target_before_service_actions(self):
        plan = self.plan()
        authority_dir = self.activate_authority({self.release_digest(self.old): 1})
        calls = []

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=lambda argv, **_: calls.append(argv),
                plan=plan, releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), authority_dir=authority_dir,
                controller_dir=self.controller_dir)
        self.assertEqual(raised.exception.code, "WORKSPACE_RELEASE_UNLISTED")
        self.assertEqual(calls, [])

    def test_active_launcher_command_pins_admitted_authority_path(self):
        authority_dir = self.activate_authority({self.release_digest(self.new): 1})
        command = module._workspace_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.new, self.interpreter, None, self.controller_dir,
        )

        index = command.index("--authority-dir")
        self.assertEqual(command[index + 1], str(authority_dir))

    def test_active_authority_never_starts_prohibited_rollback_runtime(self):
        plan = self.plan()
        authority_dir = self.activate_authority({self.release_digest(self.new): 2})
        starts = 0

        def command(argv, **kwargs):
            nonlocal starts
            if argv[2:4] == ["stop", SERVICE]:
                for pid in (4101, 4201, 4100, 4200):
                    if (self.proc / str(pid)).exists():
                        self.remove_process(pid)
            elif argv[2:4] == ["start", SERVICE]:
                starts += 1
                self.write_process(4200, 1, 500, [str(self.client), "daemon"])
                self.write_process(4201, 4200, 501,
                                   [str(self.interpreter), str(self.new / "server.py")])
                self.status["process"]["pid"] = 4200
            return b""

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=command, plan=plan,
                releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                attempts=1, poll_timeout=5, authority_dir=authority_dir,
                controller_dir=self.controller_dir,
                verify_fn=lambda _: (_ for _ in ()).throw(
                    RuntimeError("TOOL_CATALOG_MISMATCH")))
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(raised.exception.cause_code, "TOOL_CATALOG_MISMATCH")
        self.assertEqual(starts, 1)

    def test_same_snapshot_successor_after_verification_is_not_stopped(self):
        plan = self.plan()
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[2:4] == ["stop", SERVICE]:
                for pid in (4101, 4201, 4100, 4200):
                    if (self.proc / str(pid)).exists():
                        self.remove_process(pid)
            elif argv[2:4] == ["start", SERVICE]:
                self.write_process(4200, 1, 800, [str(self.client), "daemon"])
                self.write_process(4201, 4200, 801,
                                   [str(self.interpreter), str(self.new / "server.py")])
                self.status["process"]["pid"] = 4200
            return b""

        def verifier(actual):
            self.remove_process(4201)
            self.remove_process(4200)
            self.write_process(4300, 1, 900, [str(self.client), "daemon"])
            self.write_process(4301, 4300, 901,
                               [str(self.interpreter), str(self.new / "server.py")])
            self.status["process"]["pid"] = 4300
            raise RuntimeError("TOOL_CATALOG_MISMATCH")

        with self.assertRaises(TunnelSwitchError) as raised:
            switch_tunnel(
                self.new, interpreter=self.interpreter,
                status_fn=lambda: self.status, command_fn=command, plan=plan,
                releases=self.releases, proc_root=self.proc,
                approved_interpreters=(self.interpreter,), sleep_fn=lambda _: None,
                attempts=1, poll_timeout=5, verify_fn=verifier)
        self.assertEqual(raised.exception.code, "TUNNEL_ROLLBACK_FAILED")
        self.assertEqual(sum(argv[2:4] == ["stop", SERVICE] for argv in calls), 1)
        self.assertTrue((self.proc / "4300").exists())


if __name__ == "__main__":
    unittest.main()
