"""Official validation must use known local tools and retain private test homes."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("approval_dependency_rollout", ROOT / "rollout-host-runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class RolloutDependencyTest(unittest.TestCase):
    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
        path.chmod(0o700)
        return str(path)

    def test_recognizes_existing_wsl_node_without_ambient_path_search(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            expected = self.executable(home / "mcp-comparison/tools/node-v24.12.0-linux-x64/bin/node")
            with patch.object(Path, "home", return_value=home):
                self.assertEqual(module.runtime_node(), expected)
            Path(expected).unlink()
            with patch.object(Path, "home", return_value=home), patch("shutil.which", return_value="/ambient/node"):
                with self.assertRaisesRegex(RuntimeError, "ROLLOUT_NODE_UNAVAILABLE"):
                    module.runtime_node()

    def test_browser_checks_use_the_isolated_playwright_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            expected = self.executable(home / ".local/share/uv/tools/playwright/bin/python")
            calls = []
            def run(argv, **options):
                calls.append((argv,options))
                return subprocess.CompletedProcess(argv,0)
            with patch.object(Path, "home", return_value=home), \
                 patch.object(module, "runtime_node", return_value="/fixture/node"), \
                 patch.object(module, "runtime_interpreter", return_value="/fixture/python"), \
                 patch.object(module, "run_command", side_effect=run):
                module.run_checks()
            browsers = [(argv,options) for argv,options in calls if options["stage"] not in {
                "checks_workspace_ui", "checks_targeted", "checks_full"}]
            interpreters = dict.fromkeys({
                "checks_approval_ui", "checks_schema014_ui", "checks_host_command_ui",
                "checks_development_approval_ui", "checks_runtime_rollout_chat_ui",
                "checks_runtime_rollout_ui", "checks_workspace_approval_browser",
            }, expected)
            interpreters["checks_git_commit_access_browser"] = "/usr/bin/python3"
            self.assertEqual({options["stage"] for _, options in browsers}, set(interpreters))
            for argv,options in browsers:
                with self.subTest(stage=options["stage"]):
                    self.assertIn(interpreters[options["stage"]], argv)
                    self.assertTrue(any(value.startswith("HOME=") for value in argv))
                    self.assertTrue(any(value.startswith("XDG_STATE_HOME=") for value in argv))

    def test_legacy_system_browser_interpreter_remains_available(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path,"home",return_value=Path(directory)):
            self.assertEqual(module.browser_test_interpreter(),"/usr/bin/python3")

if __name__ == "__main__":
    unittest.main()
