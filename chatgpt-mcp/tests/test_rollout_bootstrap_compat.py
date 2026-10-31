import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "rollout-host-runtime.py"
spec = importlib.util.spec_from_file_location("rollout_bootstrap_compat", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Grant:
    def __init__(self, source_digest, target_snapshot, control_digest, error):
        self.source_digest = source_digest
        self.target_snapshot = target_snapshot
        self.control_digest = control_digest
        self.error = error

    def verify(self, *args):
        raise RuntimeError(self.error)


class BootstrapCompatTest(unittest.TestCase):
    def fixture(self):
        official = {"chatgpt-mcp/server.py": "b" * 64}
        manifest = {"server.py": "b" * 64}
        target = module.hashlib.sha256(
            module.json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:20]
        controls = {
            "rollout-host-runtime.py": "c" * 64,
            "tossapp_neutral_ab24_command.js": "d" * 64,
        }
        legacy_controls = dict(controls)
        legacy_controls.pop("tossapp_neutral_ab24_command.js")
        legacy = module.hashlib.sha256(
            module.json.dumps(legacy_controls, sort_keys=True).encode()
        ).hexdigest()
        return official, controls, target, legacy

    def test_accepts_only_exact_legacy_control_after_old_source_changed(self):
        official, controls, target, legacy = self.fixture()
        grant = Grant("a" * 64, target, legacy, "WEB_ROLLOUT_SOURCE_CHANGED")
        with patch.object(module, "inventory", return_value=official), \
             patch.object(module, "control_inventory", return_value=controls):
            module._verify_approved_web(
                grant, object(), catalog_fn=lambda _root: {"digest": "a" * 64}
            )

    def test_rejects_wrong_source_target_or_control(self):
        official, controls, target, legacy = self.fixture()
        cases = [
            Grant("e" * 64, target, legacy, "WEB_ROLLOUT_SOURCE_CHANGED"),
            Grant("a" * 64, "f" * 20, legacy, "WEB_ROLLOUT_SOURCE_CHANGED"),
            Grant("a" * 64, target, "0" * 64, "WEB_ROLLOUT_SOURCE_CHANGED"),
        ]
        with patch.object(module, "inventory", return_value=official), \
             patch.object(module, "control_inventory", return_value=controls):
            for grant in cases:
                with self.subTest(grant=grant.control_digest), self.assertRaisesRegex(
                        RuntimeError, "WEB_ROLLOUT_SOURCE_CHANGED"):
                    module._verify_approved_web(
                        grant, object(), catalog_fn=lambda _root: {"digest": "a" * 64}
                    )

    def test_never_replaces_non_source_failure(self):
        grant = Grant("a" * 64, "b" * 20, "c" * 64, "WEB_ROLLOUT_GRANT_INVALID")
        with self.assertRaisesRegex(RuntimeError, "WEB_ROLLOUT_GRANT_INVALID"):
            module._verify_approved_web(
                grant, object(), catalog_fn=lambda _root: {"digest": "a" * 64}
            )


if __name__ == "__main__":
    unittest.main()
