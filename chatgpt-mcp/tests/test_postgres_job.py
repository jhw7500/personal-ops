import contextlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import postgres_job  # noqa: E402


class PostgresJobTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chatgpt-mcp-postgres-job-")
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "postgresql"
        self.bin_dir = self.prefix / "bin"
        self.bin_dir.mkdir(parents=True)
        self.etc = self.root / "etc"
        self.etc.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.events = self.root / "events"
        scripts = {
            "postgres": "#!/bin/sh\nexit 0\n",
            "initdb": """#!/bin/sh
set -eu
while [ "$1" != "-D" ]; do shift; done
mkdir -p "$2"
printf 'initdb\n' >> "$MCP_TEST_EVENTS"
""",
            "pg_ctl": """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$MCP_TEST_EVENTS"
""",
            "createdb": """#!/bin/sh
set -eu
printf 'createdb:%s\n' "$*" >> "$MCP_TEST_EVENTS"
""",
        }
        for name, content in scripts.items():
            path = self.bin_dir / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def test_runs_child_with_private_database_and_always_stops(self):
        result_file = self.workspace / "child-env"
        command = [sys.executable, "-c", (
            "import os, pathlib; "
            "assert os.environ['TEST_DATABASE_URL'].startswith('postgresql://mcp:'); "
            "assert os.environ['TEST_DATABASE_URL'].endswith('/tarororo_test'); "
            "assert 'PGPASSWORD' not in os.environ; "
            "assert 'DATABASE_URL' not in os.environ; "
            f"pathlib.Path({str(result_file)!r}).write_text('ok'); "
            "raise SystemExit(7)"
        )]
        environment = {
            "MCP_EPHEMERAL_POSTGRES": "1",
            "MCP_TEST_EVENTS": str(self.events),
            "TMPDIR": str(self.tmp),
            "PATH": os.environ["PATH"],
            "PGPASSWORD": "must-clear",
            "DATABASE_URL": "must-clear",
        }
        output = io.StringIO()
        with (mock.patch.object(postgres_job, "POSTGRES_PREFIX", self.prefix),
              mock.patch.object(postgres_job, "ETC_DIR", self.etc),
              mock.patch.object(postgres_job, "WORKSPACE_PREFIX", self.workspace),
              mock.patch.dict(os.environ, environment, clear=True),
              mock.patch("os.getcwd", return_value=str(self.workspace)),
              contextlib.redirect_stdout(output)):
            result = postgres_job.run("tarororo_test", command)

        self.assertEqual(result, 7)
        self.assertEqual(result_file.read_text(encoding="utf-8"), "ok")
        events = self.events.read_text(encoding="utf-8")
        self.assertIn("initdb", events)
        self.assertIn("createdb:", events)
        self.assertIn("start", events)
        self.assertIn("stop", events)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertNotIn("postgresql://", output.getvalue())
        self.assertNotIn("must-clear", output.getvalue())
        self.assertEqual(stat.S_IMODE(self.etc.joinpath("passwd").stat().st_mode), 0o644)

    def test_rejects_host_execution_and_unknown_database(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sandbox"):
                postgres_job.run("tarororo_test", ["true"])
        with mock.patch.dict(os.environ, {"MCP_EPHEMERAL_POSTGRES": "1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "database"):
                postgres_job.run("production", ["true"])


if __name__ == "__main__":
    unittest.main()
