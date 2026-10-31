"""Keep CLI and web rollout controls pinned to the same current inputs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

import runtime_rollout_web

HERE = Path(__file__).resolve().parents[1]


def official():
    spec = importlib.util.spec_from_file_location(
        "rollout_chat_controls", HERE / "rollout-host-runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapFormatTest(unittest.TestCase):
    def test_cli_and_web_control_inventories_match_current_format(self):
        inventory = official().control_inventory()
        for name, digest in inventory.items():
            with self.subTest(name=name):
                self.assertEqual(
                    digest,
                    hashlib.sha256((HERE / name).read_bytes()).hexdigest(),
                )

        cli_digest = hashlib.sha256(
            json.dumps(inventory, sort_keys=True).encode()
        ).hexdigest()
        _target, web_digest = runtime_rollout_web._expected_snapshots(HERE.parent)
        self.assertEqual(cli_digest, web_digest)

        self.assertIn("rollout_test_runner.py", inventory)
        expected_mjs = {
            f"tests/{path.name}" for path in (HERE / "tests").glob("test_*.mjs")
        }
        self.assertTrue(expected_mjs)
        self.assertEqual(
            {name for name in inventory if name.endswith(".mjs")},
            expected_mjs,
        )

    def test_new_ui_is_still_pinned_and_mandatory(self):
        module = official()
        self.assertIn(
            "tests/test_runtime_rollout_chat_ui.py", module.control_inventory()
        )
        self.assertIn(
            "tests/test_runtime_rollout_chat_ui.py",
            (HERE / "rollout-host-runtime.py").read_text(),
        )
