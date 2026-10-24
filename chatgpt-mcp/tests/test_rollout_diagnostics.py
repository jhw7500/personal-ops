"""Rollout receipts survive failure without recording private subprocess output."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import rollout_diagnostics as diagnostics
from rollout_diagnostics import RolloutReceipt, run_command, safe_error


class CliObservationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.receipt = RolloutReceipt(self.root / "receipts", mode="apply")

    def process(self, pid=501, *, start=123, state="S", argv=b"/bin/codex\0resume\0",
                cgroup=b"0::/fixture-secret-session.scope\n", exit_status=0):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        # Fields 3..52 of Linux /proc/PID/stat, with literal independent values.
        fields = [state, "17", "500", "400", "34816"] + ["0"] * 45
        fields[19] = str(start)
        fields[49] = str(exit_status)
        (folder / "stat").write_text(f"{pid} (private ) process) " + " ".join(fields))
        (folder / "cmdline").write_bytes(argv)
        (folder / "cgroup").write_bytes(cgroup)
        return folder

    def observe(self, **kwargs):
        return diagnostics.CliSessionObserver(self.receipt, proc_root=self.proc, **kwargs)

    def test_same_identity_records_private_numeric_metadata_and_timestamps(self):
        self.process()
        with self.observe() as observation:
            observation.checkpoint("cli_after_tunnel")
        summary = observation.summary
        self.assertTrue(summary["cli_sessions_preserved"])
        self.assertTrue(summary["cli_observation_complete"])
        self.assertEqual(summary["cli_session_count"], 1)
        saved = self.receipt.path.read_text()
        events = json.loads(saved)["events"]
        row = next(e for e in events if e["stage"] == "cli_before_switch_session")
        self.assertEqual((row["pid"], row["ppid"], row["pgid"], row["sid"], row["tty_nr"]),
                         (501, 17, 500, 400, 34816))
        self.assertEqual(row["cli_kind"], "codex")
        self.assertIn("observed_at", row)
        self.assertIn("elapsed_ms", row)
        self.assertEqual(len(row["cgroup_sha256"]), 64)
        self.assertNotIn("fixture-secret", saved)
        self.assertNotIn("private ) process", saved)
        self.assertNotIn("/bin/codex", saved)
        self.assertEqual(self.receipt.path.stat().st_mode & 0o777, 0o600)

    def test_missing_and_reused_pids_are_changes_without_invented_exit_status(self):
        for change in ("missing", "identity_changed"):
            with self.subTest(change=change):
                folder = self.process()
                with self.observe() as observation:
                    if change == "missing":
                        for file in folder.iterdir():
                            file.unlink()
                        folder.rmdir()
                    else:
                        self.process(start=124)
                self.assertFalse(observation.summary["cli_sessions_preserved"])
                row = self.receipt.data["events"][-2]
                self.assertEqual(row["observation_status"], change)
                self.assertFalse(row["exit_status_available"])
                self.assertNotIn("returncode", row)
                if change == "identity_changed":
                    self.assertEqual(row.get("observed_start_ticks"), 124)

    def test_pid_reused_during_checkpoint_is_identified_without_mixing_cgroup_metadata(self):
        self.process()
        read_file = diagnostics._read_proc_file
        replaced = False
        def replacing_read(folder, name):
            nonlocal replaced
            data = read_file(folder, name)
            if name == "cgroup" and not replaced:
                self.process(start=124)
                replaced = True
            return data
        with self.observe() as observation:
            with patch.object(diagnostics, "_read_proc_file", side_effect=replacing_read):
                observation.checkpoint("cli_after_tunnel")
        row = next(e for e in self.receipt.data["events"]
                   if e["stage"] == "cli_after_tunnel_session")
        self.assertEqual(row["observation_status"], "identity_changed")
        self.assertEqual(row.get("observed_start_ticks"), 124)
        self.assertNotIn("cgroup_sha256", row)
        self.assertFalse(observation.summary["cli_sessions_preserved"])

    def test_missing_cmdline_of_a_still_present_process_is_incomplete(self):
        folder = self.process()
        (folder / "cmdline").unlink()
        with self.observe() as observation:
            pass
        self.assertFalse(observation.summary["cli_observation_complete"])
        self.assertFalse(observation.summary["cli_sessions_preserved"])

    def test_disappearing_process_before_baseline_is_not_a_claimed_cli_loss(self):
        self.process()
        read_file = diagnostics._read_proc_file
        def disappearing_read(folder, name):
            if name == "cmdline":
                for file in folder.iterdir():
                    file.unlink()
                folder.rmdir()
                raise FileNotFoundError()
            return read_file(folder, name)
        with patch.object(diagnostics, "_read_proc_file", side_effect=disappearing_read):
            with self.observe() as observation:
                pass
        self.assertTrue(observation.summary["cli_observation_complete"])
        self.assertEqual(observation.summary["cli_session_count"], 0)

    def test_other_uid_is_not_read_or_reported(self):
        folder = self.process()
        lstat = Path.lstat
        def other_uid(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == folder:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(Path, "lstat", other_uid), self.observe() as observation:
            pass
        self.assertEqual(observation.summary["cli_session_count"], 0)
        self.assertTrue(observation.summary["cli_observation_complete"])
        self.assertFalse(any(e["stage"].endswith("_session") for e in self.receipt.data["events"]))

    def test_zombie_has_only_the_exit_status_actually_observed(self):
        self.process()
        with self.observe() as observation:
            self.process(state="Z", exit_status=7 << 8)
        row = self.receipt.data["events"][-2]
        self.assertEqual(row["observation_status"], "exited")
        self.assertTrue(row["exit_status_available"])
        self.assertEqual(row["returncode"], 7)
        self.assertFalse(observation.summary["cli_sessions_preserved"])

    def test_runtime_children_noninteractive_commands_and_symlinks_are_not_sessions(self):
        self.process(501)
        self.process(502, argv=b"codex\0app-server\0")
        self.process(503, argv=b"codex\0exec\0fixture-secret\0")
        self.process(504, argv=b"claude\0-p\0fixture-secret\0")
        self.process(505, argv=b"claude\0--resume\0")
        (self.proc / "506").symlink_to(self.proc / "505", target_is_directory=True)
        with self.observe(excluded_pids=(501,)) as observation:
            pass
        self.assertEqual(observation.summary["cli_session_count"], 1)
        row = next(e for e in self.receipt.data["events"]
                   if e["stage"] == "cli_before_switch_session")
        self.assertEqual(row["pid"], 505)
        self.assertEqual(row["cli_kind"], "claude")

    def test_unreadable_cgroup_is_incomplete_and_never_claims_preservation(self):
        folder = self.process()
        (folder / "cgroup").unlink()
        with self.observe() as observation:
            pass
        self.assertFalse(observation.summary["cli_observation_complete"])
        self.assertFalse(observation.summary["cli_sessions_preserved"])
        self.assertNotIn("cgroup_sha256", self.receipt.data["events"][-2])

    def test_unreadable_identity_is_unknown_instead_of_a_confirmed_process_change(self):
        self.process()
        with self.observe() as observation:
            (self.proc / "501" / "stat").write_text("invalid private fixture")
        self.assertFalse(observation.summary["cli_observation_complete"])
        self.assertFalse(observation.summary["cli_sessions_preserved"])
        self.assertEqual(observation.summary["cli_changed_count"], 0)
        self.assertEqual(self.receipt.data["events"][-2]["observation_status"], "unavailable")

    def test_missing_stat_file_with_a_present_pid_is_not_reported_as_process_exit(self):
        folder = self.process()
        with self.observe() as observation:
            (folder / "stat").unlink()
        self.assertFalse(observation.summary["cli_observation_complete"])
        self.assertEqual(observation.summary["cli_changed_count"], 0)
        self.assertEqual(self.receipt.data["events"][-2]["observation_status"], "unavailable")

    def test_process_limit_fails_closed_and_is_bounded(self):
        for pid in (501, 502, 503):
            self.process(pid)
        with patch.object(diagnostics, "MAX_CLI_SESSIONS", 2), self.observe() as observation:
            pass
        self.assertEqual(observation.summary["cli_session_count"], 2)
        self.assertFalse(observation.summary["cli_observation_complete"])
        self.assertFalse(observation.summary["cli_sessions_preserved"])

    def test_final_receipt_error_preserves_the_original_switch_failure(self):
        self.process()
        failing_save = patch.object(self.receipt, "_save", side_effect=OSError("private"))
        try:
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_SWITCH_FAILURE") as raised:
                with self.observe():
                    failing_save.start()
                    raise RuntimeError("FIXTURE_SWITCH_FAILURE")
        finally:
            failing_save.stop()
        self.assertTrue(raised.exception.receipt_write_failed)


class RolloutDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_private_receipt_records_command_failure_and_final_exit(self):
        receipt = RolloutReceipt(self.root, mode="preflight")
        secret = "fixture-private-token-and-tunnel-id"
        result = run_command(
            [sys.executable, "-c", f"import sys; print('{secret}'); "
             f"print('{secret}', file=sys.stderr); sys.exit(7)"],
            stage="tunnel_status", timeout=3, receipt=receipt)
        self.assertEqual(result.returncode, 7)
        receipt.finish({"status": "failed", "code": "TUNNEL_RUNTIME_STATUS_FAILED"}, 1)
        text = receipt.path.read_text()
        saved = json.loads(text)
        self.assertEqual(saved["exit_code"], 1)
        event = saved["events"][-1]
        self.assertEqual(event["stage"], "tunnel_status")
        self.assertEqual(event["returncode"], 7)
        self.assertGreater(event["stderr_bytes"], 0)
        self.assertNotIn(secret, text)
        self.assertNotIn(sys.executable, text)
        self.assertEqual(receipt.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(receipt.path.stat().st_uid, os.getuid())

    def test_phase_failure_is_durable_before_main_handles_exception(self):
        receipt = RolloutReceipt(self.root, mode="apply")
        with self.assertRaises(OSError):
            with receipt.phase("snapshot_install"):
                raise OSError("credential=fixture-never-log")
        data = json.loads(receipt.path.read_text())
        self.assertEqual(data["events"][-1]["stage"], "snapshot_install")
        self.assertEqual(data["events"][-1]["state"], "failed")
        self.assertEqual(data["events"][-1]["code"], "ROLLOUT_OS_ERROR")
        self.assertNotIn("fixture-never-log", receipt.path.read_text())

    def test_unverified_result_retains_exit_two_and_safe_snapshot(self):
        receipt = RolloutReceipt(self.root)
        receipt.finish({"status": "verification_required", "switched": False,
                        "snapshot": "c" * 20, "code": "LIVE_RUNTIME_MISMATCH"}, 2)
        result = json.loads(receipt.path.read_text())
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["result"]["switched"])
        self.assertEqual(result["result"]["snapshot"], "c" * 20)

    def test_receipt_rejects_symlink_and_unsafe_metadata(self):
        target = self.root / "other"
        target.mkdir()
        (self.root / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError)):
            RolloutReceipt(self.root / "link")
        receipt = RolloutReceipt(self.root)
        with self.assertRaises(ValueError):
            receipt.event("tunnel_status", "failed", stderr="private output")
        with self.assertRaises(ValueError):
            receipt.event("tunnel_status", "failed", code="private token")

    def test_unknown_exception_message_is_never_used_as_error_code(self):
        self.assertEqual(safe_error(RuntimeError("TUNNEL_SWITCH_FAILED")), "TUNNEL_SWITCH_FAILED")
        self.assertEqual(safe_error(RuntimeError("secret fixture")), "ROLLOUT_UNEXPECTED_ERROR")
        self.assertEqual(safe_error(subprocess.TimeoutExpired(["secret"], 1)), "ROLLOUT_COMMAND_TIMEOUT")

    def test_python_import_failure_retains_category_without_exception_message(self):
        receipt = RolloutReceipt(self.root)
        result = run_command([sys.executable, "-c", "import fixture_private_module_name"],
                             stage="import_probe", timeout=3, receipt=receipt)
        self.assertNotEqual(result.returncode, 0)
        saved = receipt.path.read_text()
        self.assertEqual(json.loads(saved)["events"][-1]["output_code"], "PYTHON_IMPORT_ERROR")
        self.assertNotIn("fixture_private_module_name", saved)

    def test_failed_unittest_names_survive_without_failure_messages(self):
        secret = "fixture-private-database-password"
        fixture = self.root / "test_failure_fixture.py"
        fixture.write_text(
            "import unittest\n"
            "class RolloutFixture(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            f"        self.fail({secret!r})\n"
            "    def test_error(self):\n"
            f"        raise RuntimeError({secret!r})\n")
        for stage in ("checks_targeted", "checks_full", "checks_approval_ui",
                      "checks_workspace_approval_browser",
                      "checks_schema014_ui", "checks_runtime_rollout_ui", "checks_host_command_ui"):
            with self.subTest(stage=stage):
                receipt = RolloutReceipt(self.root / "receipts")
                result = run_command(
                    [sys.executable, "-m", "unittest", "-v", "test_failure_fixture"],
                    stage=stage, timeout=5, cwd=self.root, receipt=receipt)
                self.assertNotEqual(result.returncode, 0)
                saved_text = receipt.path.read_text()
                event = json.loads(saved_text)["events"][-1]
                self.assertEqual(event["test_count"], 2)
                self.assertEqual(event["failed_tests"], [
                    "test_failure_fixture.RolloutFixture.test_error",
                    "test_failure_fixture.RolloutFixture.test_failure",
                ])
                self.assertNotIn(secret, saved_text)
                self.assertNotIn("RuntimeError", saved_text)
                self.assertNotIn("Traceback", saved_text)
                self.assertEqual(receipt.path.stat().st_mode & 0o777, 0o600)

    def test_unittest_failure_id_accepts_full_id_and_class_only_headers(self):
        expected = "tests.test_example.ExampleTest.test_example"
        for line in (
                b"FAIL: test_example (tests.test_example.ExampleTest.test_example)",
                b"FAIL: test_example (tests.test_example.ExampleTest)"):
            with self.subTest(line=line):
                self.assertEqual(diagnostics._unittest_failure_id(line), expected)

    def test_receipt_rejects_unbounded_or_non_identifier_failed_tests(self):
        receipt = RolloutReceipt(self.root)
        for value in (["test.safe"], [{}],
                      ["tests.Safe.test_ok", "private token"],
                      ["tests.Safe.test_" + "x" * 300],
                      ["tests.Safe.test_ok"] * 33):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    receipt.event("checks_targeted", "failed", failed_tests=value)

    def test_failed_node_input_survives_without_reporter_names_or_failure_output(self):
        fixture = self.root / "test_workspace_fixture.mjs"
        fixture.write_text("")
        secret = "fixture-private-node-test-name-and-token"
        tap = ("TAP version 13\n"
               f"# Subtest: {secret}\n"
               f"not ok 1 - {secret}\n"
               "  ---\n"
               f"  location: '{fixture}:1:1'\n"
               f"  error: '{secret}'")
        spec = (f"\u2716 {secret}\n"
                "\u2139 tests 1\n"
                "\u2139 fail 1\n\n"
                "\u2716 failing tests:\n\n"
                f"test at {fixture}:1:1")
        for reporter, output in (("tap", tap), ("spec", spec)):
            with self.subTest(reporter=reporter):
                receipt = RolloutReceipt(self.root / (reporter + "-node-receipts"))
                result = run_command(
                    [sys.executable, "-c",
                     "import sys; sys.stdout.write(sys.argv[1]); sys.exit(1)",
                     output, "--test", str(fixture)],
                    stage="checks_workspace_ui", timeout=3, receipt=receipt)
                self.assertEqual(result.returncode, 1)
                saved_text = receipt.path.read_text()
                event = json.loads(saved_text)["events"][-1]
                self.assertEqual(event["failed_test_files"], [fixture.name])
                self.assertNotIn(secret, saved_text)
                self.assertNotIn(str(fixture), saved_text)
                self.assertEqual(receipt.path.stat().st_mode & 0o777, 0o600)

    def test_node_diagnostics_reject_secret_oversize_and_malformed_locations(self):
        fixture = self.root / "test_workspace_fixture.mjs"
        fixture.write_text("")
        secret = "fixture-private-node-secret"
        output = ("not ok 1 - fixture\n"
                  f"  location: '{secret}.mjs:1:1'\n"
                  + "x" * 513 + f"  location: '{fixture}:2:1'\n"
                  f"  location: '{fixture}:line:column'")
        for stage in ("checks_workspace_ui", "checks_targeted"):
            with self.subTest(stage=stage):
                receipt = RolloutReceipt(self.root / (stage + "-receipts"))
                result = run_command(
                    [sys.executable, "-c",
                     "import sys; sys.stdout.write(sys.argv[1]); sys.exit(1)",
                     output, "--test", str(fixture)],
                    stage=stage, timeout=3, receipt=receipt)
                self.assertEqual(result.returncode, 1)
                saved_text = receipt.path.read_text()
                event = json.loads(saved_text)["events"][-1]
                self.assertNotIn("failed_test_files", event)
                self.assertNotIn(secret, saved_text)
                self.assertNotIn(str(fixture), saved_text)

    def test_receipt_rejects_unbounded_or_unsafe_failed_node_inputs(self):
        receipt = RolloutReceipt(self.root)
        for value in (["workspace_fixture.mjs"], [{}],
                      ["test_safe.mjs", "private-secret.mjs"],
                      ["test_" + "x" * 119 + ".mjs"],
                      ["test_safe.mjs"] * 33):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    receipt.event("checks_workspace_ui", "failed", failed_test_files=value)

    def test_receipt_failure_does_not_replace_original_safe_error(self):
        receipt = RolloutReceipt(self.root)
        with patch.object(receipt, "_save", side_effect=[None, OSError("private-error-fixture")]):
            with self.assertRaisesRegex(RuntimeError, "TUNNEL_SWITCH_FAILED") as caught:
                with receipt.phase("tunnel_switch"):
                    raise RuntimeError("TUNNEL_SWITCH_FAILED")
        self.assertTrue(receipt.write_failed)
        self.assertTrue(caught.exception.receipt_write_failed)

    def test_log_capacity_failure_does_not_block_compensating_command(self):
        receipt = RolloutReceipt(self.root)
        marker = self.root / "compensated"
        with patch("rollout_diagnostics.MAX_EVENTS", 2):
            receipt.event("preflight", "started")
            receipt.event("preflight", "completed")
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_RECEIPT_LIMIT"):
                run_command([sys.executable, "-c", "pass"], stage="activate_probe",
                            timeout=3, receipt=receipt)
            result = run_command([sys.executable, "-c", f"open({str(marker)!r},'w').close()"],
                                 stage="rollback_probe", timeout=3, receipt=receipt)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(marker.exists())

    def test_child_process_group_signal_does_not_terminate_caller(self):
        caller_group = os.getpgrp()
        result = run_command(
            [sys.executable, "-c", "import os,signal; os.killpg(0,signal.SIGTERM)"],
            stage="isolation_probe", timeout=3)
        self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertEqual(os.getpgrp(), caller_group)

    def test_timeout_stops_only_created_child_and_descendant(self):
        pid_file = self.root / "child.pid"
        code = ("import subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(60)")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "ROLLOUT_COMMAND_TIMEOUT"):
            run_command([sys.executable, "-c", code], stage="timeout_probe", timeout=0.4)
        self.assertLess(time.monotonic() - started, 5)
        pid = int(pid_file.read_text())
        stat_file = Path(f"/proc/{pid}/stat")
        if stat_file.exists():
            self.assertEqual(stat_file.read_text().rsplit(")", 1)[1].split()[0], "Z")
        os.kill(os.getpid(), 0)

    def test_large_output_is_bounded_and_failure_remains_visible(self):
        receipt = RolloutReceipt(self.root)
        with self.assertRaisesRegex(RuntimeError, "ROLLOUT_OUTPUT_TOO_LARGE"):
            run_command([sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"],
                        stage="output_probe", timeout=3, capture=True,
                        max_output=8192, receipt=receipt)
        event = json.loads(receipt.path.read_text())["events"][-1]
        self.assertEqual(event["code"], "ROLLOUT_OUTPUT_TOO_LARGE")
        self.assertNotIn("xxxxxxxx", receipt.path.read_text())

    def test_successful_leader_does_not_leave_a_detached_pipe_child(self):
        pid_file = self.root / "orphan.pid"
        code = ("import subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({str(pid_file)!r},'w').write(str(p.pid))")
        result = run_command([sys.executable, "-c", code], stage="descendant_probe", timeout=3)
        pid = int(pid_file.read_text())
        stat_file = Path(f"/proc/{pid}/stat")
        if stat_file.exists():
            state = stat_file.read_text().rsplit(")", 1)[1].split()[0]
            if state != "Z":
                os.kill(pid, signal.SIGKILL)
            self.assertEqual(state, "Z")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
