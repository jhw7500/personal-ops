from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_command_profiles import HostCommandProfiles  # noqa: E402


class ExternalCallProfileTests(unittest.TestCase):
    def test_external_call_effect_is_valid_and_publicly_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            payload = repo / "requests.json"
            payload.write_bytes(b'{"synthetic":true}')
            payload.chmod(0o600)
            executable = root / "neutral-ab24"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            config = root / "host-commands.json"
            profile = {
                "project": "tossApp",
                "label": "Tarororo neutral material A/B24 Gemini experiment",
                "executable": str(executable),
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "argv": ["run"],
                "cwd": ".",
                "timeout_seconds": 1800,
                "effect": "external_call",
                "inputs": {
                    "requests.json": hashlib.sha256(payload.read_bytes()).hexdigest(),
                },
            }
            config.write_text(json.dumps({
                "version": 1, "enabled": True,
                "profiles": {"tossapp-neutral-ab24": profile},
            }))
            config.chmod(0o600)
            registry = HostCommandProfiles({"tossApp": repo}, config)
            listed = registry.list_profiles("tossApp")
            self.assertEqual(listed["profiles"][0]["effect"], "external_call")
            frozen = registry.freeze("tossApp", "tossapp-neutral-ab24")
            self.assertEqual(frozen["effect"], "external_call")
            self.assertEqual(frozen["argv"], ["run"])


if __name__ == "__main__":
    unittest.main()
