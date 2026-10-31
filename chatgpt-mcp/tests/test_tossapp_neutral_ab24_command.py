from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


class NeutralAb24CommandTests(unittest.TestCase):
    def test_self_test_is_network_free_and_reports_fixed_contract(self):
        command = Path(__file__).resolve().parents[1] / "tossapp_neutral_ab24_command.js"
        completed = subprocess.run(
            ["node", str(command), "self-test"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload, {
            "status": "SELF_TEST_PASS",
            "callLimit": 24,
            "retries": 0,
            "model": "gemini-3.1-flash-lite",
            "promptVersion": "tarot-grounded.v4",
        })


if __name__ == "__main__":
    unittest.main()
