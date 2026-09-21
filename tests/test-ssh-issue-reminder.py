#!/usr/bin/env python3
"""Behavior tests; only the external GitHub process is replaced."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ssh-issue-reminder.py"
HOOK = ROOT / "scripts/ssh-issue-reminder-profile.sh"
OLD_BOOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NEW_BOOT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
URL57 = "https://github.com/jhw7500/personal-ops/issues/57"
URL153 = "https://github.com/jhw7500/jhw-notion/issues/153"


class ReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = None
        if SCRIPT.is_file():
            spec = importlib.util.spec_from_file_location("ssh_reminder", SCRIPT)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.assertIsNotNone(self.module, "SSH issue reminder is not implemented")
        self.tmp = tempfile.TemporaryDirectory(prefix="ssh-reminder-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.boot = self.root / "boot_id"
        self.boot.write_text(NEW_BOOT + "\n", encoding="utf-8")
        self.settings = {
            "enabled": True,
            "activate_after_boot": OLD_BOOT,
            "issues": [
                {"repo": "jhw7500/personal-ops", "number": 57, "label": "재부팅 디버깅 구성"},
                {"repo": "jhw7500/jhw-notion", "number": 153, "label": "노션 안전 배포 절차"},
            ],
        }
        self.save_settings()

    def save_settings(self):
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def render(self, states=None, preview=False):
        states = states or {57: "OPEN", 153: "OPEN"}

        def github(command, **kwargs):
            # Catch querying the wrong issue/repository or losing the timeout.
            expected = {
                57: "github.com/jhw7500/personal-ops",
                153: "github.com/jhw7500/jhw-notion",
            }
            number = int(command[3])
            self.assertEqual(
                ["/usr/bin/gh", "issue", "view", str(number), "--repo", expected[number],
                 "--json", "state"], command,
            )
            self.assertEqual(3, kwargs["timeout"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
            self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
            self.assertEqual("1", kwargs["env"]["GH_PROMPT_DISABLED"])
            value = states[number]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, subprocess.CompletedProcess):
                return value
            return subprocess.CompletedProcess(command, 0, json.dumps({"state": value}))

        output = io.StringIO()
        with patch.object(self.module.subprocess, "run", side_effect=github) as process:
            self.module.show_reminders(self.config, self.boot, output, preview=preview)
        return output.getvalue(), process.call_count

    def test_open_issues_repeat_on_every_login_without_changing_config(self):
        original = self.config.read_bytes()
        for _ in range(2):
            output, count = self.render()
            self.assertEqual(2, count)
            self.assertIn(URL57, output)
            self.assertIn(URL153, output)
            self.assertIn("미완료", output)
        self.assertEqual(original, self.config.read_bytes())

    def test_closed_issue_is_hidden_but_other_open_issue_remains(self):
        output, _ = self.render({57: "CLOSED", 153: "OPEN"})
        self.assertNotIn(URL57, output)
        self.assertIn(URL153, output)

    def test_all_closed_is_silent_and_reopened_issue_returns(self):
        output, _ = self.render({57: "CLOSED", 153: "CLOSED"})
        self.assertEqual("", output)
        output, _ = self.render({57: "OPEN", 153: "CLOSED"})
        self.assertIn(URL57, output)

    def test_installation_boot_does_not_query_or_display(self):
        self.boot.write_text(OLD_BOOT, encoding="utf-8")
        self.assertEqual(("", 0), self.render())

    def test_preview_can_check_current_boot(self):
        self.boot.write_text(OLD_BOOT, encoding="utf-8")
        output, count = self.render(preview=True)
        self.assertEqual(2, count)
        self.assertIn(URL57, output)

    def test_disabled_reminder_never_queries(self):
        self.settings["enabled"] = False
        self.save_settings()
        self.assertEqual(("", 0), self.render())

    def test_timeout_is_unknown_not_closed_and_does_not_hide_other_issue(self):
        timeout = subprocess.TimeoutExpired(["gh"], 3)
        output, _ = self.render({57: timeout, 153: "OPEN"})
        self.assertIn(URL57, output)
        self.assertIn(URL153, output)
        self.assertIn("상태 확인 불가", output)

    def test_api_failure_or_malformed_response_never_leaks_raw_output(self):
        responses = [
            subprocess.CompletedProcess([], 1, "secret-token"),
            subprocess.CompletedProcess([], 0, "secret-token"),
            subprocess.CompletedProcess([], 0, '["secret-token"]'),
            subprocess.CompletedProcess([], 0, '{"state":"unexpected-secret-token"}'),
            FileNotFoundError("secret-token"),
        ]
        for response in responses:
            with self.subTest(response=type(response).__name__):
                output, _ = self.render({57: response, 153: "CLOSED"})
                self.assertIn(URL57, output)
                self.assertIn("상태 확인 불가", output)
                self.assertNotIn("secret-token", output)

    def test_unreadable_boot_id_warns_without_network(self):
        self.boot.unlink()
        output, count = self.render()
        self.assertEqual(0, count)
        self.assertIn("부팅 정보 확인 불가", output)

    def test_bad_config_is_bounded_and_never_queries(self):
        self.config.write_text("not-json secret-token", encoding="utf-8")
        output, count = self.render()
        self.assertEqual(0, count)
        self.assertIn("설정 확인 불가", output)
        self.assertNotIn("secret-token", output)

    def test_control_characters_or_invalid_coordinates_are_rejected(self):
        for change in ({"label": "bad\x1b[2J"}, {"repo": "--bad"}, {"number": True}):
            with self.subTest(change=change):
                original = self.settings["issues"][0].copy()
                self.settings["issues"][0].update(change)
                self.save_settings()
                output, count = self.render()
                self.assertEqual(0, count)
                self.assertIn("설정 확인 불가", output)
                self.assertNotIn("\x1b", output)
                self.settings["issues"][0] = original

    def test_main_requires_ssh_and_both_terminal_streams(self):
        combinations = [
            ({}, True, True),
            ({"SSH_CONNECTION": "test"}, True, True),
            ({"SSH_CONNECTION": "test", "SSH_TTY": "test"}, False, True),
            ({"SSH_CONNECTION": "test", "SSH_TTY": "test"}, True, False),
        ]
        for env, input_tty, output_tty in combinations:
            with self.subTest(env=env, input_tty=input_tty, output_tty=output_tty):
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(sys, "argv", [str(SCRIPT)]), \
                     patch.object(sys.stdin, "isatty", return_value=input_tty), \
                     patch.object(sys, "stdout", output), \
                     patch.object(output, "isatty", return_value=output_tty), \
                     patch.object(self.module.subprocess, "run") as process:
                    self.assertEqual(0, self.module.main())
                self.assertEqual("", output.getvalue())
                process.assert_not_called()

    def test_main_interactive_ssh_displays_real_renderer_after_reboot(self):
        output = io.StringIO()
        response = subprocess.CompletedProcess([], 0, '{"state":"OPEN"}')
        with patch.dict(os.environ, {"SSH_CONNECTION": "test", "SSH_TTY": "test"}), \
             patch.object(sys, "argv", [str(SCRIPT)]), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys, "stdout", output), \
             patch.object(output, "isatty", return_value=True), \
             patch.object(self.module, "CONFIG", self.config), \
             patch.object(self.module, "BOOT_ID", self.boot), \
             patch.object(self.module.subprocess, "run", return_value=response):
            self.assertEqual(0, self.module.main())
        self.assertIn(URL57, output.getvalue())
        self.assertIn(URL153, output.getvalue())


class ShellHookTests(unittest.TestCase):
    def test_noninteractive_ssh_and_local_shell_are_silent(self):
        self.assertTrue(HOOK.is_file(), "SSH login hook is not implemented")
        for interactive, ssh in [(False, True), (True, False), (True, True)]:
            # Even an interactive shell with SSH variables must require a real TTY.
            with self.subTest(interactive=interactive, ssh=ssh):
                env = os.environ.copy()
                env.pop("SSH_CONNECTION", None)
                env.pop("SSH_TTY", None)
                if ssh:
                    env.update(SSH_CONNECTION="test", SSH_TTY="/dev/pts/test")
                command = ["bash", "--noprofile", "--norc", "--login"]
                if interactive:
                    command.append("-i")
                command += ["-c", '. "$1"; printf SENTINEL', "test", str(HOOK)]
                result = subprocess.run(command, env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=2)
                self.assertEqual(0, result.returncode)
                self.assertEqual("SENTINEL", result.stdout)


if __name__ == "__main__":
    unittest.main()
