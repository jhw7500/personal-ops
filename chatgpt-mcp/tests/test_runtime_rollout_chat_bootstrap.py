"""Keep new approval UI pinned by the already-installed rollout control format."""

import hashlib
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1]


def official():
    spec = importlib.util.spec_from_file_location(
        "rollout_chat_controls", HERE / "rollout-host-runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapFormatTest(unittest.TestCase):
    def test_control_inventory_matches_existing_installed_format(self):
        names = (
            "rollout-host-runtime.py",
            "rollout_tunnel.py",
            "rollout_admin.py",
            "rollout_catalog.py",
            "rollout_diagnostics.py",
            "install-runtime.py",
            "configure-host-runtime.py",
        )
        paths = [HERE / n for n in names] + sorted((HERE / "tests").glob("test_*.py"))
        paths += [
            HERE / "tests" / n
            for n in (
                "check_host_access_approval_ui.py",
                "check_schema014_approval_ui.py",
                "check_host_command_approval_ui.py",
            )
        ]
        expected = {
            str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths
        }
        self.assertEqual(official().control_inventory(), expected)

    def test_new_ui_is_still_pinned_and_mandatory(self):
        module = official()
        self.assertIn(
            "tests/test_runtime_rollout_chat_ui.py", module.control_inventory()
        )
        self.assertIn(
            "tests/test_runtime_rollout_chat_ui.py",
            (HERE / "rollout-host-runtime.py").read_text(),
        )
