"""Offline exec-fd helper layout fixture; never invokes an actual Codex model."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
from host_command_entry import open_runtime_executable, EntryError  # noqa: E402


class HostAgentEntryTest(unittest.TestCase):
    def test_runtime_fd_preserves_helper_directory_and_hash_checks(self):
        with tempfile.TemporaryDirectory(prefix="host-agent-execfd-") as directory:
            root = Path(directory)
            binary = root / "runtime" / "codex"
            binary.parent.mkdir(mode=0o700)
            binary.write_bytes(Path('/usr/bin/python3').resolve().read_bytes())
            binary.chmod(0o500)
            expected = hashlib.sha256(binary.read_bytes()).hexdigest()
            fd = open_runtime_executable(binary, expected, root / "project")
            self.addCleanup(os.close, fd)
            self.assertEqual(Path(os.readlink(f"/proc/self/fd/{fd}")), binary)
            with self.assertRaises(EntryError):
                open_runtime_executable(binary, "a" * 64, root / "project")
            binary.chmod(0o700)
            with self.assertRaises(EntryError):
                open_runtime_executable(binary, expected, root / "project")

    def test_layout_entry_executes_private_runtime_path_and_multiline_stdin(self):
        with tempfile.TemporaryDirectory(prefix="host-agent-layout-") as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            binary = runtime / "codex"
            binary.write_bytes(Path('/usr/bin/python3').resolve().read_bytes())
            binary.chmod(0o500)
            expected = hashlib.sha256(binary.read_bytes()).hexdigest()
            project = root / "project"
            project.mkdir()
            code = "import os,sys; print(os.readlink('/proc/self/exe')); print(sys.stdin.read())"
            result = subprocess.run(
                [sys.executable, "-I", str(MODULE / "host_command_entry.py"), "--runtime-layout",
                 expected, str(binary), str(project), ".", "--", "-c", code],
                input=b"multiline\nobjective\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"HOME": directory, "PATH": "/usr/bin:/bin"}, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(binary).encode(), result.stdout)
            self.assertIn(b"multiline\nobjective", result.stdout)


if __name__ == "__main__":
    unittest.main()
