"""The developer suite must never consume the owner's live XDG state."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


RUNNER = Path(__file__).resolve().parents[1] / "rollout_test_runner.py"


class RolloutTestRunnerIsolationTests(unittest.TestCase):
    def test_cli_isolates_config_state_and_cache_before_test_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            ambient = root / "owner"
            ambient.mkdir()
            sentinel = ambient / "owner-state"
            sentinel.write_text("must remain unchanged")
            fixture = fixtures / "test_environment.py"
            fixture.write_text(textwrap.dedent("""
                import os
                from pathlib import Path
                import stat
                import unittest

                ROOTS_AT_IMPORT = {
                    key: Path(os.environ[key])
                    for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')
                }

                class EnvironmentTest(unittest.TestCase):
                    def test_private_roots(self):
                        ambient = Path(os.environ['TEST_OWNER_ROOT'])
                        for root in ROOTS_AT_IMPORT.values():
                            self.assertNotEqual(root, ambient)
                            self.assertFalse(root.is_relative_to(ambient))
                            self.assertTrue(root.is_dir())
                            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                        state = ROOTS_AT_IMPORT['XDG_STATE_HOME'] / 'fixture-state'
                        state.mkdir()
                        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
                        private_file = state / 'fixture.json'
                        private_file.write_text('{}')
                        self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)
            """))
            environment = dict(os.environ, TEST_OWNER_ROOT=str(ambient))
            for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                environment[key] = str(ambient)
            result = subprocess.run(
                [sys.executable, "-B", str(RUNNER), "--discover", str(fixtures),
                 "--require-excluded-count", "0"],
                env=environment, capture_output=True, text=True, timeout=20, umask=0o002,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(), "must remain unchanged")
            self.assertEqual(set(ambient.iterdir()), {sentinel})

    def test_restores_owner_environment_and_umask_on_success_and_failure(self):
        spec = importlib.util.spec_from_file_location("runner_under_test", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        names = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
        for fails in (False, True):
            with self.subTest(fails=fails), mock.patch.dict(os.environ):
                os.environ["XDG_CONFIG_HOME"] = "/fixture/owner-config"
                os.environ.pop("XDG_STATE_HOME", None)
                os.environ.pop("XDG_CACHE_HOME", None)
                expected = {name: os.environ.get(name) for name in names}
                previous_umask = os.umask(0o002)
                roots = []
                try:
                    try:
                        with runner.isolated_owner_environment():
                            roots = [Path(os.environ[name]) for name in names]
                            self.assertTrue(all(root.is_dir() for root in roots))
                            self.assertNotEqual(os.environ["XDG_CONFIG_HOME"],
                                                expected["XDG_CONFIG_HOME"])
                            if fails:
                                raise RuntimeError("fixture failure")
                    except RuntimeError as error:
                        self.assertTrue(fails)
                        self.assertEqual(str(error), "fixture failure")
                    self.assertEqual({name: os.environ.get(name) for name in names}, expected)
                    self.assertEqual(os.umask(0o002), 0o002)
                    self.assertTrue(all(not root.exists() for root in roots))
                finally:
                    os.umask(previous_umask)
