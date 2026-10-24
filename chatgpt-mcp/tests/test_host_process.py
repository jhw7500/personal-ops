"""Bounded backend command runner for registered host operations.

Only local throwaway Python children run here; no Docker, DB or host service.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from host_process import (  # noqa: E402
    PG_ENV_KEYS, HostCommandError, command_env, run_registered_command,
)
from host_runtime import MAX_OUTPUT  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402

PY = sys.executable


def _gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            return stream.read().split(b") ", 1)[1][:1] == b"Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True


class RunRegisteredCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_error_type_is_host_runtime_error_with_bounded_fields(self):
        self.assertTrue(issubclass(HostCommandError, HostRuntimeError))
        with self.assertRaises(HostCommandError) as caught:
            run_registered_command([PY, "-c", (
                "import sys;sys.stderr.write('NOTICE:  x\\nERROR:  42P01\\n"
                "DETAIL:  relation secret_table\\n');sys.stdout.write('row-data');"
                "sys.exit(3)")], cwd=None, timeout=10)
        error = caught.exception
        self.assertEqual(str(error), "HOST_COMMAND_FAILED")
        self.assertEqual(error.code, "HOST_COMMAND_FAILED")
        self.assertEqual(error.returncode, 3)
        self.assertEqual(error.sqlstate, "42P01")
        for raw in ("secret_table", "row-data", "relation"):
            self.assertNotIn(raw, repr(error.args) + str(error))

    def test_sqlstate_requires_a_validated_five_char_code(self):
        for stderr, expected in (("ERROR:  42601\n", "42601"),
                                 ("FATAL:  28P01\n", "28P01"),
                                 ("psql:<stdin>:3: ERROR:  42P01\n", "42P01"),
                                 ("NOTICE:  n\nERROR:  22012  \n", "22012"),
                                 ("FATAL:  28P01: password authentication failed\n", None),
                                 ("ERROR:  ABORT: transaction aborted\n", None),
                                 ("ERROR:  42601 trailing\n", None),
                                 ("psql:/tmp/x.sql:1: ERROR:  42601\n", None),
                                 ("ERROR:  syntax error at end\n", None),
                                 ("ERROR:  4260\n", None),
                                 ("error:  42601\n", None),
                                 ("plain text 42601\n", None)):
            with self.subTest(stderr=stderr):
                with self.assertRaises(HostCommandError) as caught:
                    run_registered_command([PY, "-c", (
                        f"import sys;sys.stderr.write({stderr!r});sys.exit(1)")],
                        cwd=None, timeout=10)
                self.assertEqual(caught.exception.sqlstate, expected)

    def test_stdout_is_capped_at_256_kib(self):
        exact = run_registered_command(
            [PY, "-c", f"import sys;sys.stdout.buffer.write(b'x'*{MAX_OUTPUT})"],
            cwd=None, timeout=10)
        self.assertEqual(len(exact), 256 * 1024)
        with self.assertRaises(HostCommandError) as caught:
            run_registered_command(
                [PY, "-c", f"import sys;sys.stdout.buffer.write(b'x'*{MAX_OUTPUT + 1})"],
                cwd=None, timeout=10)
        self.assertEqual(caught.exception.code, "HOST_OUTPUT_TOO_LARGE")
        self.assertIsNone(caught.exception.returncode)

    def test_timeout_kills_only_the_dedicated_process_group(self):
        pid_file = self.home / "grandchild.pid"
        bystander = subprocess.Popen([PY, "-c", "import time;time.sleep(30)"],
                                     start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        script = (
            "import subprocess,sys,time\n"
            f"p=subprocess.Popen([{PY!r},'-c','import time;time.sleep(30)'])\n"
            f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        with self.assertRaises(HostCommandError) as caught:
            run_registered_command([PY, "-c", script], cwd=None, timeout=1)
        self.assertEqual(caught.exception.code, "HOST_COMMAND_TIMEOUT")
        self.assertIsNone(caught.exception.returncode)
        self.assertLess(time.monotonic() - started, 10)
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while not _gone(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(_gone(grandchild), "grandchild in the dedicated group survived")
        self.assertIsNone(bystander.poll(), "a process outside the group was killed")

    def test_stdin_and_both_output_pipes_are_serviced_concurrently(self):
        payload = os.urandom(2 * 1024 * 1024)
        script = (
            "import hashlib,sys\n"
            "sys.stderr.buffer.write(b'e'*300000);sys.stderr.flush()\n"
            "sys.stdout.buffer.write(b'#'*100000);sys.stdout.flush()\n"
            "h=hashlib.sha256()\n"
            "while True:\n"
            "    chunk=sys.stdin.buffer.read(65536)\n"
            "    if not chunk: break\n"
            "    h.update(chunk);sys.stderr.buffer.write(chunk[:4096])\n"
            "sys.stdout.write(h.hexdigest())\n"
        )
        started = time.monotonic()
        output = run_registered_command([PY, "-c", script], cwd=None, timeout=30,
                                        stdin_data=payload)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(output[:100000], b"#" * 100000)
        self.assertEqual(output[100000:].decode(), hashlib.sha256(payload).hexdigest())

    def test_child_that_ignores_stdin_does_not_hang_or_leak(self):
        output = run_registered_command([PY, "-c", "print('done')"], cwd=None, timeout=10,
                                        stdin_data=b"SELECT secret;\n" * 100000)
        self.assertEqual(output, b"done\n")

    def test_environment_is_sanitized_and_credentials_are_pg_only(self):
        script = "import json,os;print(json.dumps(dict(os.environ)))"
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "leak", "PGPASSWORD": "ambient",
                                     "COMPOSE_FILE": "/tmp/evil.yaml"}):
            import json
            seen = json.loads(run_registered_command(
                [PY, "-c", script], cwd=None, timeout=10,
                credential_env={"PGPASSWORD": "pinned", "PGUSER": "app"}))
        self.assertEqual(seen["PGPASSWORD"], "pinned")
        self.assertEqual(seen["PGUSER"], "app")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", seen)
        self.assertNotIn("COMPOSE_FILE", seen)
        self.assertEqual(set(seen), set(command_env()) | {"PGPASSWORD", "PGUSER"})
        self.assertEqual(seen["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(seen["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertIn("PGOPTIONS", PG_ENV_KEYS)

    def test_compose_env_goes_through_the_single_env_builder(self):
        script = "import json,os;print(json.dumps(dict(os.environ)))"
        registered = {"TARORORO_UID": "1000", "TARORORO_CONFIG_DIR": "/home/u/.config/t"}
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://10.0.0.1:2375",
                                     "COMPOSE_FILE": "/tmp/evil.yaml"}):
            import json
            seen = json.loads(run_registered_command(
                [PY, "-c", script], cwd=None, timeout=10, compose_env=registered))
        self.assertEqual(set(seen), set(command_env()) | set(registered))
        self.assertEqual(seen["TARORORO_CONFIG_DIR"], "/home/u/.config/t")
        self.assertNotIn("DOCKER_HOST", seen)
        self.assertEqual(command_env(compose_env=registered),
                         {**command_env(), **registered})
        marker = self.home / "spawned"
        argv = [PY, "-c", f"open({str(marker)!r},'w').write('x')"]
        for bad in ({"DOCKER_HOST": "tcp://x"}, {"COMPOSE_PROJECT_NAME": "x"}, {"PATH": "/tmp"},
                    {"HOME": "/tmp"}, {"LANG": "C"}, {"LD_PRELOAD": "/x.so"},
                    {"PYTHONPATH": "/x"}, {"GIT_DIR": "/x"}, {"lower": "x"}, {"A": 1},
                    {"A": "a\0b"}, {"A": "x" * 1025}, ["A"]):
            with self.subTest(bad=bad):
                with self.assertRaises(HostCommandError) as caught:
                    run_registered_command(argv, cwd=None, timeout=10, compose_env=bad)
                self.assertEqual(caught.exception.code, "INVALID_COMPOSE_ENV")
                self.assertFalse(marker.exists())
        # A registered value may never override a credential (or any already-built) key.
        with self.assertRaises(HostCommandError) as caught:
            run_registered_command(argv, cwd=None, timeout=10, credential_env={"PGUSER": "a"},
                                   compose_env={"PGUSER": "b"})
        self.assertEqual(caught.exception.code, "INVALID_COMPOSE_ENV")
        self.assertFalse(marker.exists())

    def test_non_pg_credential_keys_are_rejected_before_spawn(self):
        marker = self.home / "spawned"
        argv = [PY, "-c", f"open({str(marker)!r},'w').write('x')"]
        for bad in ({"LD_PRELOAD": "/tmp/x.so"}, {"PATH": "/tmp"}, {"PGPASSWORD": 1},
                    {"PGHOST": "a\0b"}, {"pgpassword": "x"}, ["PGUSER"]):
            with self.subTest(bad=bad):
                with self.assertRaises(HostCommandError) as caught:
                    run_registered_command(argv, cwd=None, timeout=10, credential_env=bad)
                self.assertEqual(caught.exception.code, "INVALID_CREDENTIAL_ENV")
                self.assertFalse(marker.exists())

    def test_no_shell_and_invalid_argv_rejected(self):
        marker = self.home / "shell-ran"
        output = run_registered_command([PY, "-c", "import sys;print(sys.argv[1])",
                                         f"; touch {marker}"], cwd=None, timeout=10)
        self.assertEqual(output.decode().strip(), f"; touch {marker}")
        self.assertFalse(marker.exists())
        for argv in ([], "echo hi", [PY, ""], [PY, "a\0b"], [PY, 3]):
            with self.subTest(argv=argv):
                with self.assertRaises(HostCommandError) as caught:
                    run_registered_command(argv, cwd=None, timeout=10)
                self.assertEqual(caught.exception.code, "INVALID_COMMAND")
        for timeout in (0, -1, True, 3601, "5"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(HostCommandError):
                    run_registered_command([PY, "-c", "pass"], cwd=None, timeout=timeout)
        with self.assertRaises(HostCommandError):
            run_registered_command([PY, "-c", "pass"], cwd=None, timeout=5, stdin_data="text")

    def test_cwd_is_honoured_and_missing_executable_is_sanitized(self):
        output = run_registered_command([PY, "-c", "import os;print(os.getcwd())"],
                                        cwd=self.home, timeout=10)
        self.assertEqual(Path(output.decode().strip()).resolve(), self.home.resolve())
        with self.assertRaises(HostCommandError) as caught:
            run_registered_command([str(self.home / "missing-binary")], cwd=None, timeout=5)
        self.assertEqual(caught.exception.code, "EXECUTOR_UNAVAILABLE")
        self.assertNotIn("missing-binary", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_pipe_read_errors_are_sanitized_and_the_group_is_stopped(self):
        real_read = os.read
        pid_file = self.home / "child.pid"

        def failing(fd, size):
            if size == 65536:
                raise OSError(5, "EIO /secret/pipe")
            return real_read(fd, size)

        script = (f"import os,sys,time;open({str(pid_file)!r},'w').write(str(os.getpid()));"
                  "print('x',flush=True);time.sleep(30)")
        started = time.monotonic()
        with patch("host_process.os.read", side_effect=failing):
            with self.assertRaises(HostCommandError) as caught:
                run_registered_command([PY, "-c", script], cwd=None, timeout=10)
        self.assertLess(time.monotonic() - started, 8)
        error = caught.exception
        self.assertEqual((error.code, error.returncode, error.sqlstate),
                         ("HOST_COMMAND_FAILED", None, None))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn("secret", str(error) + repr(error.args))
        self.assertTrue(_gone(int(pid_file.read_text())))


if __name__ == "__main__":
    unittest.main()
