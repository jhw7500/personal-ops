"""Systemd admin rollout tests; mocks all commands and network interactions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rollout_admin as module
from rollout_admin import (AdminSwitchError, parse_execstart, preflight_admin,
                           switch_admin)
from workspace_authority import WorkspaceAuthority
from workspace_control import MANDATORY_RELEASE_FILES, install_controller


class FakeResponse:
    status = 200
    def __init__(self, content):
        self.content = content
    def read(self, size):
        return self.content[:size]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class AdminRolloutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.release = self.home / ".local/lib/chatgpt-coding-mcp/releases" / ("c" * 20)
        self.release.mkdir(parents=True)
        self.release.parent.chmod(0o700)
        self.release.chmod(0o700)
        (self.release / "admin_server.py").write_text("fixture")
        (self.release / "admin_server.py").chmod(0o600)
        contents = {
            name: ((b"fixture" if name == "admin_server.py" else
                    ("fixture:" + name).encode()))
            for name in MANDATORY_RELEASE_FILES
        }
        for name, content in contents.items():
            path = self.release / name
            path.write_bytes(content)
            path.chmod(0o600)
        manifest = {name: hashlib.sha256(content).hexdigest()
                    for name, content in contents.items()}
        release_digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        (self.release / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (self.release / "manifest.json").chmod(0o600)
        renamed = self.release.with_name(release_digest[:20])
        self.release.rename(renamed)
        self.release = renamed
        self.release_digest = release_digest
        self.source_script = Path(__file__).resolve().parents[1] / "admin_server.py"
        self.serena_python = (self.home
                              / ".local/share/uv/tools/serena-agent/bin/python")
        serena_target = (self.home
                         / ".local/share/uv/python/cpython-fixture/bin/python3.12")
        serena_target.parent.mkdir(parents=True)
        serena_target.write_text("fixture")
        serena_target.chmod(0o700)
        self.serena_python.parent.mkdir(parents=True)
        self.serena_python.symlink_to(serena_target)
        for directory in (self.home / ".local/share", self.home / ".local/share/uv",
                          self.home / ".local/share/uv/tools",
                          self.home / ".local/share/uv/tools/serena-agent",
                          self.home / ".local/share/uv/tools/serena-agent/bin",
                          self.home / ".local/share/uv/python",
                          self.home / ".local/share/uv/python/cpython-fixture",
                          serena_target.parent):
            directory.chmod(0o700)
        self.unit = ("[Unit]\nDescription=fixture\n[Service]\n"
                     f"ExecStart={self.serena_python} {self.source_script} --port 8765 "
                     "--lan-host 192.168.2.70 "
                     "--public-origin https://host.example.ts.net:8443\n")
        self.override = (self.home
                         / ".config/systemd/user/personal-ops-admin.service.d/20-runtime.conf")

    def activate_authority(self, allowed):
        project = self.home / "projects/demo"
        project.mkdir(parents=True, exist_ok=True)
        authority_dir = self.home / "workspace-authority"
        WorkspaceAuthority({"demo": project}, authority_dir).initialize(
            "a" * 64, allowed,
        )
        self.controller_dir = self.home / "workspace-controller"
        install_controller(
            self.controller_dir, Path(__file__).resolve().parents[1],
            projects={"demo": project},
        )
        return authority_dir

    def effective_unit(self):
        if self.override.exists():
            return self.unit + "\n" + self.override.read_text()
        return self.unit

    def fake_sysctl(self, commands=None, *, fail_restart=False, pids=None):
        calls = []
        supplied_pids = iter(pids) if pids is not None else None
        restart_count = 0
        def call(argv, **kw):
            nonlocal restart_count
            calls.append(argv)
            if fail_restart and argv[2:3] == ["restart"]:
                raise AdminSwitchError("FAULT")
            if argv[2:3] == ["cat"]:
                return self.effective_unit().encode()
            if argv[2:3] == ["restart"]:
                restart_count += 1
            if argv[2:4] == ["show", "personal-ops-admin.service"]:
                pid = (next(supplied_pids) if supplied_pids is not None else
                       (100 if restart_count == 0 else 200 if restart_count == 1 else 300))
                return f"{pid}\n".encode()
            return b""
        return call, calls

    def process_argv(self, pid):
        script = self.source_script if pid in {100, 300} else self.release / "admin_server.py"
        return [str(self.serena_python), str(script), "--port", "8765",
                "--lan-host", "192.168.2.70", "--public-origin",
                "https://host.example.ts.net:8443"]

    @staticmethod
    def process_start(pid):
        return pid * 10

    def test_strict_parser_rejects_arbitrary_program_or_options(self):
        root = self.source_script.parent
        releases = self.home / ".local/lib/chatgpt-coding-mcp/releases"
        with patch("rollout_admin.Path.home", return_value=self.home):
            argv, port = parse_execstart(self.unit, root, releases)
        self.assertEqual(port, 8765)
        self.assertEqual(Path(argv[0]), self.serena_python)
        self.assertEqual(Path(argv[1]), self.source_script)
        for cmd in ("ExecStart=/bin/bash -c evil\n",
                    f"ExecStart=/usr/bin/python3 {self.source_script} --listen 0.0.0.0\n",
                    "ExecStart=python3 /tmp/untrusted/admin_server.py\n",
                    f"ExecStart={self.home}/bin/python {self.source_script}\n"):
            with self.subTest(cmd=cmd):
                with patch("rollout_admin.Path.home", return_value=self.home):
                    with self.assertRaises(AdminSwitchError):
                        parse_execstart("[Service]\n" + cmd, root, releases)

    def test_read_only_preflight_accepts_matching_live_process_without_writes(self):
        call, calls = self.fake_sysctl()
        with patch("rollout_admin.Path.home", return_value=self.home):
            argv, port = preflight_admin(
                unit_fn=lambda: self.unit,
                root=self.source_script.parent,
                command_fn=call, process_argv_fn=self.process_argv,
                process_start_fn=self.process_start)
        self.assertEqual(argv[0], str(self.serena_python))
        self.assertEqual(argv[1], str(self.source_script))
        self.assertEqual(port, 8765)
        self.assertEqual(sum(cmd[2:3] == ["show"] for cmd in calls), 1)
        self.assertFalse(any(cmd[2:3] in (["daemon-reload"], ["restart"])
                             for cmd in calls))

    def test_atomic_override_restart_and_http_ui_verification(self):
        call, calls = self.fake_sysctl(self.unit)
        with patch("rollout_admin.Path.home", return_value=self.home):
            result = switch_admin(self.release, unit_fn=self.effective_unit,
                                  command_fn=call,
                                  process_argv_fn=self.process_argv,
                                  process_start_fn=self.process_start,
                                  sleep_fn=lambda _: None,
                                  urlopen_fn=lambda *args, **kwargs:
                                  FakeResponse(b'<section id="host-approval"></section>'))
        self.assertEqual(result, "admin_ready")
        override = self.home / ".config/systemd/user/personal-ops-admin.service.d/20-runtime.conf"
        self.assertEqual(override.stat().st_mode & 0o777, 0o600)
        contents = override.read_text()
        self.assertIn(str(self.serena_python), contents)
        self.assertIn(str(self.release / "admin_server.py"), contents)
        self.assertIn("--lan-host 192.168.2.70", contents)
        self.assertIn("--public-origin https://host.example.ts.net:8443", contents)
        self.assertTrue(any(cmd[-2:] == ["restart", "personal-ops-admin.service"] for cmd in calls))

    def test_active_launcher_exec_converges_with_stable_process_identity(self):
        authority_dir = self.activate_authority({self.release_digest: 1})
        call, _ = self.fake_sysctl()
        expected = self.process_argv(200)
        launcher = module._workspace_admin_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.release, self.serena_python, None, expected[2:], self.controller_dir,
        )
        reads = 0

        def process_argv(pid):
            nonlocal reads
            if pid != 200:
                return self.process_argv(pid)
            reads += 1
            return launcher if reads == 1 else expected

        with patch("rollout_admin.Path.home", return_value=self.home):
            result = switch_admin(
                self.release, unit_fn=self.effective_unit, command_fn=call,
                process_argv_fn=process_argv,
                process_start_fn=self.process_start, sleep_fn=lambda _: None,
                urlopen_fn=lambda *args, **kwargs:
                FakeResponse(b'<section id="host-approval"></section>'),
                authority_dir=authority_dir, controller_dir=self.controller_dir,
            )
        self.assertEqual(result, "admin_ready")
        self.assertGreaterEqual(reads, 3)

    def test_active_launcher_exec_converges_during_rollback(self):
        authority_dir = self.activate_authority({self.release_digest: 1})
        self.unit = self.unit.replace(
            str(self.source_script), str(self.release / "admin_server.py"))
        call, _ = self.fake_sysctl()
        expected = [
            str(self.serena_python), str(self.release / "admin_server.py"),
            "--port", "8765", "--lan-host", "192.168.2.70",
            "--public-origin", "https://host.example.ts.net:8443",
        ]
        launcher = module._workspace_admin_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.release, self.serena_python, None, expected[2:], self.controller_dir,
        )
        reads = {200: 0, 300: 0}

        def process_argv(pid):
            if pid == 100:
                return expected
            reads[pid] += 1
            return launcher if reads[pid] == 1 else expected

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_SWITCH_FAILED:ADMIN_UI_NOT_UPDATED") as raised:
                switch_admin(
                    self.release, unit_fn=self.effective_unit, command_fn=call,
                    process_argv_fn=process_argv,
                    process_start_fn=self.process_start, sleep_fn=lambda _: None,
                    urlopen_fn=lambda *args, **kwargs:
                    FakeResponse(b'<form id="login-form"></form>'),
                    authority_dir=authority_dir, controller_dir=self.controller_dir,
                )
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertGreaterEqual(reads[200], 2)
        self.assertGreaterEqual(reads[300], 2)

    def test_launcher_exec_rejects_unrelated_argv(self):
        authority_dir = self.activate_authority({self.release_digest: 1})
        self.unit = self.unit.replace(
            str(self.source_script), str(self.release / "admin_server.py"))
        call, _ = self.fake_sysctl()
        expected = self.process_argv(200)
        launcher = module._workspace_admin_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.release, self.serena_python, None, expected[2:], self.controller_dir,
        )
        reads = 0

        def process_argv(pid):
            nonlocal reads
            if pid == 100:
                return expected
            if pid != 200:
                return self.process_argv(pid)
            reads += 1
            if reads == 1:
                return launcher
            return [str(self.serena_python), "/unrelated/admin_server.py"]

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaises(AdminSwitchError) as raised:
                switch_admin(
                    self.release, unit_fn=self.effective_unit, command_fn=call,
                    process_argv_fn=process_argv,
                    process_start_fn=self.process_start, sleep_fn=lambda _: None,
                    authority_dir=authority_dir, controller_dir=self.controller_dir,
                )
        self.assertIn("ADMIN_SERVICE_CHANGED", str(raised.exception))

    def test_launcher_exec_rejects_changed_start_ticks(self):
        authority_dir = self.activate_authority({self.release_digest: 1})
        self.unit = self.unit.replace(
            str(self.source_script), str(self.release / "admin_server.py"))
        call, _ = self.fake_sysctl()
        expected = self.process_argv(200)
        launcher = module._workspace_admin_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.release, self.serena_python, None, expected[2:], self.controller_dir,
        )
        argv_reads = 0
        start_reads = 0

        def process_argv(pid):
            nonlocal argv_reads
            if pid == 100:
                return expected
            if pid != 200:
                return self.process_argv(pid)
            argv_reads += 1
            return launcher if argv_reads == 1 else expected

        def process_start(pid):
            nonlocal start_reads
            if pid != 200:
                return self.process_start(pid)
            start_reads += 1
            return 2000 if start_reads == 1 else 2001

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaises(AdminSwitchError) as raised:
                switch_admin(
                    self.release, unit_fn=self.effective_unit, command_fn=call,
                    process_argv_fn=process_argv, process_start_fn=process_start,
                    sleep_fn=lambda _: None, authority_dir=authority_dir,
                    controller_dir=self.controller_dir,
                )
        self.assertIn("ADMIN_SERVICE_CHANGED", str(raised.exception))

    def test_manager_cached_unit_updates_only_after_daemon_reload(self):
        self.override.parent.mkdir(parents=True)
        self.override.parent.chmod(0o700)
        previous = ("[Service]\nExecStart=\nExecStart="
                    + " ".join(self.process_argv(100)) + "\n").encode()
        self.override.write_bytes(previous)
        self.override.chmod(0o600)
        call, calls = self.fake_sysctl()
        loaded_unit = self.effective_unit()

        def cached_unit():
            return loaded_unit

        def cached_call(argv, **kwargs):
            nonlocal loaded_unit
            result = call(argv, **kwargs)
            if argv[2:3] == ["daemon-reload"]:
                loaded_unit = self.effective_unit()
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            result = switch_admin(
                self.release, unit_fn=cached_unit, command_fn=cached_call,
                process_argv_fn=self.process_argv,
                process_start_fn=self.process_start, sleep_fn=lambda _: None,
                urlopen_fn=lambda *args, **kwargs:
                FakeResponse(b'<section id="host-approval"></section>'))
        self.assertEqual(result, "admin_ready")
        self.assertIn(str(self.release / "admin_server.py"), loaded_unit)
        self.assertNotEqual(self.override.read_bytes(), previous)
        self.assertEqual(sum(cmd[2:3] == ["daemon-reload"] for cmd in calls), 1)

    def test_cached_unit_rollback_reloads_restored_override(self):
        call, calls = self.fake_sysctl()
        loaded_unit = self.unit

        def cached_unit():
            return loaded_unit

        def cached_call(argv, **kwargs):
            nonlocal loaded_unit
            result = call(argv, **kwargs)
            if argv[2:3] == ["daemon-reload"]:
                loaded_unit = self.effective_unit()
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_SWITCH_FAILED:ADMIN_UI_NOT_UPDATED") as raised:
                switch_admin(
                    self.release, unit_fn=cached_unit, command_fn=cached_call,
                    process_argv_fn=self.process_argv,
                    process_start_fn=self.process_start, sleep_fn=lambda _: None,
                    urlopen_fn=lambda *args, **kwargs:
                    FakeResponse(b'<form id="login-form"></form>'))
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertEqual(loaded_unit, self.unit)
        self.assertFalse(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["daemon-reload"] for cmd in calls), 2)
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 2)

    def test_waits_for_matching_main_pid_and_ui_startup(self):
        call, calls = self.fake_sysctl(
            pids=[100, 100, 100, 100, 100, 4200, 4200, 4200])
        process_calls = []
        html_calls = []

        def process_argv(pid):
            process_calls.append(pid)
            return self.process_argv(pid)

        def get_html(url, **kwargs):
            html_calls.append(url)
            if len(html_calls) == 1:
                raise OSError("not listening yet")
            return FakeResponse(b'<section id="host-approval"></section>')

        with patch("rollout_admin.Path.home", return_value=self.home):
            result = switch_admin(self.release, unit_fn=self.effective_unit,
                                  command_fn=call, process_argv_fn=process_argv,
                                  process_start_fn=self.process_start,
                                  sleep_fn=lambda _: None, urlopen_fn=get_html)
        self.assertEqual(result, "admin_ready")
        self.assertEqual(process_calls, [100, 100, 100, 100, 100, 4200, 4200, 4200])
        self.assertEqual(html_calls, ["http://127.0.0.1:8765/"] * 2)
        self.assertEqual(sum(cmd[2:3] == ["show"] for cmd in calls), 8)

    def test_stale_main_pid_rolls_back_with_typed_cause(self):
        call, calls = self.fake_sysctl(pids=[100] * 19 + [300] * 3)
        old_argv = self.process_argv(100)
        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_SWITCH_FAILED:ADMIN_MAINPID_MISMATCH"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=lambda _: old_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs: FakeResponse(
                                 b'<form id="login-form"></form>'))
        self.assertFalse((self.home / ".config/systemd/user/personal-ops-admin.service.d/"
                          "20-runtime.conf").exists())
        self.assertEqual(sum(cmd[-2:] == ["restart", "personal-ops-admin.service"]
                             for cmd in calls), 2)

    def test_http_ui_failure_rolls_back_override(self):
        call, calls = self.fake_sysctl(self.unit)
        override = self.override
        override.parent.mkdir(parents=True)
        override.parent.chmod(0o700)
        previous = ("[Service]\nExecStart=\nExecStart="
                    + " ".join(self.process_argv(100)) + "\n").encode()
        override.write_bytes(previous)
        override.chmod(0o600)
        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_SWITCH_FAILED:ADMIN_UI_NOT_UPDATED"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<form id="login-form"></form>'))
        self.assertEqual(override.read_bytes(), previous)
        self.assertEqual(override.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sum(cmd[-2:] == ["restart", "personal-ops-admin.service"]
                             for cmd in calls), 2)

    def test_concurrent_override_change_is_preserved_and_fails_closed(self):
        call, calls = self.fake_sysctl()
        override = self.override
        concurrent = b"[Service]\nExecStart=\nExecStart=/usr/bin/python3 /other/change.py\n"

        def change_override(*args, **kwargs):
            override.write_bytes(concurrent)
            override.chmod(0o600)
            return FakeResponse(b'<section id="host-approval"></section>')

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_OVERRIDE_CHANGED"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None, urlopen_fn=change_override)
        self.assertEqual(override.read_bytes(), concurrent)
        self.assertEqual(sum(cmd[-2:] == ["restart", "personal-ops-admin.service"]
                             for cmd in calls), 1)

    def test_unsafe_release_permissions_are_rejected_before_commands(self):
        self.release.chmod(0o755)
        call, calls = self.fake_sysctl()
        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(AdminSwitchError, "UNSAFE_NEW_RUNTIME"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None)
        self.assertEqual(calls, [])

    def test_active_authority_rejects_unreviewed_admin_before_commands(self):
        authority_dir = self.activate_authority({"f" * 64: 1})
        call, calls = self.fake_sysctl()

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(AdminSwitchError, "WORKSPACE_RELEASE_UNLISTED"):
                switch_admin(
                    self.release, unit_fn=self.effective_unit, command_fn=call,
                    process_argv_fn=self.process_argv,
                    process_start_fn=self.process_start,
                    authority_dir=authority_dir, controller_dir=self.controller_dir,
                )
        self.assertEqual(calls, [])

    def test_active_admin_launcher_command_pins_admitted_authority_path(self):
        authority_dir = self.activate_authority({self.release_digest: 1})
        command = module._workspace_admin_command(
            {"authority_active": True, "authority_dir": str(authority_dir)},
            self.release, self.serena_python, None, ["--port", "8765"],
            self.controller_dir,
        )

        index = command.index("--authority-dir")
        self.assertEqual(command[index + 1], str(authority_dir))

    def test_active_authority_does_not_restart_prohibited_admin_rollback(self):
        authority_dir = self.activate_authority({self.release_digest: 2})
        call, calls = self.fake_sysctl()

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(AdminSwitchError, "ADMIN_ROLLBACK_FAILED") as raised:
                switch_admin(
                    self.release, unit_fn=self.effective_unit, command_fn=call,
                    process_argv_fn=self.process_argv,
                    process_start_fn=self.process_start, sleep_fn=lambda _: None,
                    urlopen_fn=lambda *args, **kwargs:
                    FakeResponse(b'<form id="login-form"></form>'),
                    authority_dir=authority_dir, controller_dir=self.controller_dir,
                )
        self.assertEqual(raised.exception.cause_code, "UNSAFE_WORKSPACE_RELEASE")
        self.assertEqual(
            sum(cmd[-2:] == ["restart", "personal-ops-admin.service"] for cmd in calls),
            1,
        )

    def test_preflight_rejects_unrelated_existing_override_without_commands(self):
        self.override.parent.mkdir(parents=True)
        self.override.parent.chmod(0o700)
        unsupported = b"[Service]\nEnvironment=APP_ENV=production\nTimeoutStopSec=45\n"
        self.override.write_bytes(unsupported)
        self.override.chmod(0o600)
        call, calls = self.fake_sysctl()
        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(AdminSwitchError, "UNSUPPORTED_ADMIN_OVERRIDE"):
                preflight_admin(unit_fn=self.effective_unit,
                                root=self.source_script.parent, command_fn=call)
        self.assertEqual(self.override.read_bytes(), unsupported)
        self.assertEqual(calls, [])

    def test_keyboard_interrupt_after_restart_rolls_back_then_propagates(self):
        call, calls = self.fake_sysctl()

        def interrupted(argv, **kwargs):
            result = call(argv, **kwargs)
            if argv[2:3] == ["is-active"] and sum(
                    cmd[2:3] == ["restart"] for cmd in calls) == 1:
                raise KeyboardInterrupt()
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaises(KeyboardInterrupt):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=interrupted,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<form id="login-form"></form>'))
        self.assertFalse(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 2)

    def test_sigterm_exception_after_restart_rolls_back_then_propagates(self):
        call, calls = self.fake_sysctl()

        def terminated(argv, **kwargs):
            result = call(argv, **kwargs)
            if argv[2:3] == ["is-active"] and sum(
                    cmd[2:3] == ["restart"] for cmd in calls) == 1:
                raise RuntimeError("ROLLOUT_TERMINATED")
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_TERMINATED") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=terminated,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<form id="login-form"></form>'))
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertFalse(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 2)

    def test_interrupt_after_restart_launch_preserves_primary_cancellation(self):
        call, calls = self.fake_sysctl()

        def interrupted(argv, **kwargs):
            result = call(argv, **kwargs)
            if argv[2:3] == ["restart"]:
                raise KeyboardInterrupt()
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaises(KeyboardInterrupt) as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=interrupted,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None)
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertEqual(raised.exception.cause_code, "ADMIN_SERVICE_CHANGED")
        self.assertTrue(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)

    def test_sigterm_after_restart_launch_preserves_primary_cancellation(self):
        call, calls = self.fake_sysctl()

        def terminated(argv, **kwargs):
            result = call(argv, **kwargs)
            if argv[2:3] == ["restart"]:
                raise RuntimeError("ROLLOUT_TERMINATED")
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_TERMINATED") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=terminated,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None)
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertEqual(raised.exception.cause_code, "ADMIN_SERVICE_CHANGED")
        self.assertTrue(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)

    def test_failed_rollback_health_reports_rollback_failure(self):
        call, calls = self.fake_sysctl()

        def wrong_old_process(pid):
            if pid == 300:
                return [str(self.serena_python), "/unexpected/admin_server.py"]
            return self.process_argv(pid)

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_ROLLBACK_MAINPID_MISMATCH") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=wrong_old_process,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<form id="login-form"></form>'))
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 2)

    def test_failed_restart_with_stable_pid_zero_restores_old_runtime(self):
        call, calls = self.fake_sysctl()
        failed = False

        def restart_fails_once(argv, **kwargs):
            nonlocal failed
            if argv[2:3] == ["show"] and "ActiveState" in argv:
                return (b"MainPID=0\nActiveState=failed\nSubState=failed\nJob=\n"
                        if failed else
                        b"MainPID=100\nActiveState=active\nSubState=running\nJob=\n")
            if argv[2:3] == ["show"] and failed:
                return b"0\n"
            result = call(argv, **kwargs)
            if argv[2:3] == ["restart"] and not failed:
                failed = True
                raise AdminSwitchError("FAULT")
            if argv[2:3] == ["restart"]:
                failed = False
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(AdminSwitchError,
                                        "ADMIN_SWITCH_FAILED:FAULT") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=restart_fails_once,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<form id="login-form"></form>'))
        self.assertEqual(raised.exception.rollback_status, "succeeded")
        self.assertFalse(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 2)

    def test_pid_zero_race_to_positive_blocks_rollback_restart(self):
        call, calls = self.fake_sysctl()
        failed = False
        state_reads = 0

        def pid_races_before_rollback_control(argv, **kwargs):
            nonlocal failed, state_reads
            if argv[2:3] == ["show"] and "ActiveState" in argv:
                state_reads += 1
                if state_reads < 4:
                    return b"MainPID=0\nSubState=failed\nActiveState=failed\nJob=\n"
                return b"ActiveState=active\nJob=\nSubState=running\nMainPID=250\n"
            if argv[2:3] == ["show"] and failed:
                return b"0\n"
            result = call(argv, **kwargs)
            if argv[2:3] == ["restart"] and not failed:
                failed = True
                raise AdminSwitchError("FAULT")
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_SERVICE_CHANGED") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=pid_races_before_rollback_control,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None)
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertFalse(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)
        self.assertEqual(sum(cmd[2:3] == ["daemon-reload"] for cmd in calls), 1)

    def test_pid_zero_with_pending_job_blocks_rollback(self):
        call, calls = self.fake_sysctl()
        failed = False

        def pending_job_after_failure(argv, **kwargs):
            nonlocal failed
            if argv[2:3] == ["show"] and "ActiveState" in argv:
                return b"Job=77\nMainPID=0\nActiveState=failed\nSubState=failed\n"
            if argv[2:3] == ["show"] and failed:
                return b"0\n"
            result = call(argv, **kwargs)
            if argv[2:3] == ["restart"] and not failed:
                failed = True
                raise AdminSwitchError("FAULT")
            return result

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_SERVICE_CHANGED") as raised:
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=pending_job_after_failure,
                             process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None)
        self.assertEqual(raised.exception.rollback_status, "failed")
        self.assertTrue(self.override.exists())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)
        self.assertEqual(sum(cmd[2:3] == ["daemon-reload"] for cmd in calls), 1)

    def test_concurrent_effective_unit_change_blocks_rollback_and_preserves_runtime(self):
        call, calls = self.fake_sysctl()

        def change_unit(*args, **kwargs):
            self.unit += "Environment=EXTERNAL_CHANGE=1\n"
            return FakeResponse(b'<section id="host-approval"></section>')

        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_UNIT_CHANGED"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None, urlopen_fn=change_unit)
        self.assertTrue(self.override.exists())
        self.assertIn(str(self.release / "admin_server.py"), self.override.read_text())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)

    def test_concurrent_new_pid_blocks_rollback_and_preserves_runtime(self):
        call, calls = self.fake_sysctl(
            pids=[100, 100, 100, 200, 200, 250, 250])
        with patch("rollout_admin.Path.home", return_value=self.home):
            with self.assertRaisesRegex(
                    AdminSwitchError,
                    "ADMIN_ROLLBACK_FAILED:ADMIN_SERVICE_CHANGED"):
                switch_admin(self.release, unit_fn=self.effective_unit,
                             command_fn=call, process_argv_fn=self.process_argv,
                             process_start_fn=self.process_start,
                             sleep_fn=lambda _: None,
                             urlopen_fn=lambda *args, **kwargs:
                             FakeResponse(b'<section id="host-approval"></section>'))
        self.assertTrue(self.override.exists())
        self.assertIn(str(self.release / "admin_server.py"), self.override.read_text())
        self.assertEqual(sum(cmd[2:3] == ["restart"] for cmd in calls), 1)


if __name__ == "__main__":
    unittest.main()
