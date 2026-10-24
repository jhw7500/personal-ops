"""Executable identity is pinned before any approved host command runs."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from host_command_entry import (  # noqa: E402
    EntryError, F_GET_SEALS, F_SEAL_GROW, F_SEAL_SEAL, F_SEAL_SHRINK,
    F_SEAL_WRITE, open_verified_executable,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute_fd(fd: int, argv: list[str]) -> tuple[int, bytes]:
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(write_end, 1)
            os.close(read_end)
            os.close(write_end)
            os.execve(fd, argv, {"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        except BaseException:
            os._exit(127)
    os.close(write_end)
    chunks = []
    while block := os.read(read_end, 4096):
        chunks.append(block)
    os.close(read_end)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), b"".join(chunks)


class OpenVerifiedExecutableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="host-command-entry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.program = self.root / "program"
        self.program.write_bytes(b"#!/bin/sh\nprintf 'approved-bytes\\n'\n")
        self.program.chmod(0o700)

    def test_memfd_is_executable_sealed_and_not_close_on_exec(self):
        fd = open_verified_executable(self.program, digest(self.program))
        self.addCleanup(os.close, fd)
        self.assertFalse(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o700)
        required = F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL
        self.assertEqual(fcntl.fcntl(fd, F_GET_SEALS) & required, required)
        with self.assertRaises(OSError):
            os.write(fd, b"changed")

    def test_replacing_original_after_open_executes_approved_script_bytes(self):
        original = str(self.program)
        fd = open_verified_executable(self.program, digest(self.program))
        self.addCleanup(os.close, fd)
        replacement = self.root / "replacement"
        replacement.write_bytes(b"#!/bin/sh\nprintf 'replacement\\n'\n")
        replacement.chmod(0o700)
        os.replace(replacement, self.program)
        code, output = execute_fd(fd, [original])
        self.assertEqual((code, output), (0, b"approved-bytes\n"))

    def test_writing_original_after_open_does_not_change_executed_bytes(self):
        fd = open_verified_executable(self.program, digest(self.program))
        self.addCleanup(os.close, fd)
        self.program.write_bytes(b"#!/bin/sh\nprintf 'modified\\n'\n")
        self.program.chmod(0o700)
        code, output = execute_fd(fd, [str(self.program)])
        self.assertEqual((code, output), (0, b"approved-bytes\n"))

    def test_native_executable_runs_from_sealed_fd(self):
        native = Path("/usr/bin/printf")
        fd = open_verified_executable(native, digest(native))
        self.addCleanup(os.close, fd)
        code, output = execute_fd(fd, [str(native), "native-ok\\n"])
        self.assertEqual((code, output), (0, b"native-ok\n"))

    def test_symlinked_ancestor_unsafe_mode_and_wrong_hash_are_denied(self):
        real = self.root / "real"
        real.mkdir()
        target = real / "tool"
        target.write_bytes(self.program.read_bytes())
        target.chmod(0o700)
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(EntryError):
            open_verified_executable(linked / "tool", digest(target))
        self.program.chmod(0o722)
        with self.assertRaises(EntryError):
            open_verified_executable(self.program, digest(self.program))
        self.program.chmod(0o700)
        with self.assertRaises(EntryError):
            open_verified_executable(self.program, "0" * 64)


class HostCommandEntryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="host-command-cli-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "work").mkdir()
        self.program = self.base / "trusted-bin"
        self.program.write_bytes(
            b"#!/bin/sh\nprintf '%s|%s|%s|%s\\n' \"$PWD\" \"$1\" \"${HOME-}\" \"${PYTHONPATH-unset}\"\n")
        self.program.chmod(0o700)
        self.entry = MODULE_DIR / "host_command_entry.py"

    def run_entry(self, executable=None, cwd="work", args=()):
        executable = executable or self.program
        env = dict(os.environ)
        env.update({"HOME": str(self.base / "actual-home"), "PYTHONPATH": "attacker"})
        return subprocess.run(
            ["/usr/bin/python3", "-I", str(self.entry), digest(executable), str(executable),
             str(self.project.resolve()), cwd, "--", *args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            check=False,
        )

    def test_cli_uses_pinned_cwd_fixed_argv_and_sanitized_environment(self):
        result = self.run_entry(args=("literal;argument",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"{self.project.resolve() / 'work'}|literal;argument|{self.base / 'actual-home'}|unset\n".encode(),
        )

    def test_cli_rejects_executable_under_project_root(self):
        inside = self.project / "tool"
        inside.write_bytes(self.program.read_bytes())
        inside.chmod(0o700)
        result = self.run_entry(inside)
        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stderr, b"HOST_COMMAND_ENTRY_INVALID\n")
        self.assertNotIn(str(inside).encode(), result.stderr)

    def test_cli_rejects_symlinked_cwd_and_root_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for cwd in ("linked", "../outside", "/tmp"):
            with self.subTest(cwd=cwd):
                result = self.run_entry(cwd=cwd)
                self.assertEqual(result.returncode, 125)
                self.assertEqual(result.stderr, b"HOST_COMMAND_ENTRY_INVALID\n")

    def test_cli_rejects_bad_shape_without_echoing_arguments(self):
        secret = "secret-path-value"
        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(self.entry), secret],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stderr, b"HOST_COMMAND_ENTRY_INVALID\n")
        self.assertNotIn(secret.encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
