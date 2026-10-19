"""Static agent bootstrap handshake, environment and ELF boundary tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from agent_bootstrap import BootstrapError, build_bootstrap, validate_bootstrap


MARKER = b"PERSONAL_OPS_AGENT_BOOTSTRAP_V1\n"


class AgentBootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="agent-bootstrap-")
        cls.root = Path(cls._temporary.name)
        os.chmod(cls.root, 0o700)
        cls.bootstrap = cls.root / "agent-bootstrap"
        cls.build = build_bootstrap(cls.bootstrap)

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def command(self, executable, *args, assignments=None):
        env = assignments or {
            "HOME": os.environ["HOME"],
            "CODEX_HOME": str(self.root / "codex-home"),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        return [str(self.bootstrap), *(f"{key}={value}" for key, value in env.items()),
                "--", str(executable), *args]

    def parent_env(self):
        env = dict(os.environ)
        env.update({
            "OPENAI_API_KEY": "must-not-cross",
            "PYTHONPATH": "/injected/python",
            "LD_PRELOAD": "/does/not/exist.so",
        })
        return env

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def test_child_cannot_execute_before_exact_go(self):
        touched = self.root / "executed"
        process = subprocess.Popen(
            self.command(sys.executable, "-c", "from pathlib import Path; Path(r'%s').touch()" % touched),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.parent_env(),
        )
        self.addCleanup(self.stop, process)
        self.assertEqual(process.stdout.read(len(MARKER)), MARKER)
        time.sleep(0.05)
        self.assertIsNone(process.poll())
        self.assertFalse(touched.exists())

        process.stdin.write(b"GO\n")
        process.stdin.flush()
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(touched.is_file())
        process.stdout.close()
        process.stderr.close()

    def test_child_receives_only_explicit_whitelisted_environment(self):
        program = "import json,os; print(json.dumps(dict(os.environ),sort_keys=True))"
        result = subprocess.run(
            self.command(sys.executable, "-c", program), input=b"GO\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.parent_env(), timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(MARKER))
        observed = json.loads(result.stdout[len(MARKER):])
        self.assertEqual(set(observed), {
            "HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE",
            "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
        })
        self.assertNotIn("OPENAI_API_KEY", observed)
        self.assertNotIn("PYTHONPATH", observed)
        self.assertNotIn("LD_PRELOAD", observed)

    def test_handshake_read_preserves_stdin_remainder_for_child(self):
        payload = b"objective bytes stay private\n"
        result = subprocess.run(
            self.command(sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
            input=b"GO\n" + payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.parent_env(), timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, MARKER + payload)

    def test_no_go_eof_duplicate_unknown_home_mismatch_and_relative_exec_are_rejected(self):
        valid = self.command("/bin/true")
        cases = {
            "no-go": (valid, b"NO\n", self.parent_env(), MARKER),
            "eof": (valid, b"", self.parent_env(), MARKER),
            "duplicate": ([str(self.bootstrap), f"HOME={os.environ['HOME']}",
                           f"HOME={os.environ['HOME']}", "--", "/bin/true"],
                          b"GO\n", self.parent_env(), b""),
            "unknown": ([str(self.bootstrap), f"HOME={os.environ['HOME']}",
                         "OPENAI_API_KEY=no", "--", "/bin/true"],
                        b"GO\n", self.parent_env(), b""),
            "home-mismatch": ([str(self.bootstrap), "HOME=/different", "--", "/bin/true"],
                              b"GO\n", self.parent_env(), b""),
            "relative": ([str(self.bootstrap), f"HOME={os.environ['HOME']}", "--", "true"],
                         b"GO\n", self.parent_env(), b""),
        }
        for name, (argv, payload, env, output) in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, timeout=5, check=False)
                self.assertEqual(result.returncode, 125)
                self.assertEqual(result.stdout, output)

        missing = subprocess.run(
            self.command("/definitely/missing/agent-program"), input=b"GO\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.parent_env(),
            timeout=5, check=False,
        )
        self.assertEqual(missing.returncode, 126)
        self.assertEqual(missing.stdout, MARKER)

    def test_build_is_private_static_and_rejects_unsafe_destination(self):
        self.assertEqual(validate_bootstrap(self.bootstrap), self.build["sha256"])
        self.assertEqual(self.build["path"], str(self.bootstrap))
        self.assertEqual(len(self.build["sha256"]), 64)
        self.assertEqual(len(self.build["source_sha256"]), 64)
        self.assertEqual(self.bootstrap.stat().st_mode & 0o777, 0o700)

        unsafe_parent = self.root / "unsafe"
        unsafe_parent.mkdir(mode=0o755)
        unsafe_parent.chmod(0o755)  # Host umask may otherwise create 0700.
        with self.assertRaises(BootstrapError):
            build_bootstrap(unsafe_parent / "bootstrap")
        link = self.root / "link"
        link.symlink_to(self.bootstrap)
        with self.assertRaises(BootstrapError):
            validate_bootstrap(link)

    def test_control_failure_does_not_signal_unrelated_child(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.stop, unrelated)
        result = subprocess.run(
            [str(self.bootstrap), f"HOME={os.environ['HOME']}", "BAD=x", "--", "/bin/true"],
            input=b"GO\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.parent_env(), timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 125)
        self.assertIsNone(unrelated.poll())
        self.stop(unrelated)

    def test_compiler_failure_retains_private_bounded_diagnostic(self):
        source = self.root / 'invalid.c'
        source.write_text('this is an invalid controlled C fixture\n')
        with self.assertRaisesRegex(BootstrapError, 'BOOTSTRAP_COMPILE_FAILED'):
            build_bootstrap(self.root / 'failed-bootstrap', source=source)
        logs = list(self.root.glob('.agent-bootstrap-error-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(logs[0].stat().st_size, 65536)
        self.assertIn(b'error', logs[0].read_bytes())
        self.assertFalse((self.root / 'failed-bootstrap').exists())


if __name__ == "__main__":
    unittest.main()
