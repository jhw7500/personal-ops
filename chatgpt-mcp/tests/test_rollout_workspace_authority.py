import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "rollout-host-runtime.py"
spec = importlib.util.spec_from_file_location("rollout_workspace_authority", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RolloutWorkspaceAuthorityTest(unittest.TestCase):
    def admissions(self):
        old_binding = {
            "installation_id": "1" * 32,
            "authority_epoch": 7,
            "policy_digest": "c" * 64,
        }
        new_binding = dict(old_binding, authority_epoch=8)
        rollback = {
            "minimum_abi": 2,
            "release_digest": "a" * 64,
            "release_abi": 2,
            "binding": old_binding,
        }
        target = {
            "release_digest": "b" * 64,
            "release_abi": 2,
            "binding": new_binding,
        }
        restored = {
            "release_digest": "a" * 64,
            "release_abi": 2,
            "binding": new_binding,
        }
        return rollback, target, restored, new_binding

    def test_authorizes_exact_target_and_rollback_releases(self):
        rollback, target, restored, new_binding = self.admissions()
        authority = MagicMock()
        authority.advance.return_value = new_binding
        projects = {"personal-ops": Path("/projects/personal-ops")}
        with patch.object(module, "verify_launch",
                          side_effect=[rollback, target, restored]) as verify, \
             patch.object(module, "verify_release_manifest",
                          return_value={"release_digest": "b" * 64}), \
             patch.object(module, "release_enforcement_abi", return_value=2), \
             patch.object(module, "WorkspaceAuthority",
                          return_value=authority) as factory:
            result = module._authorize_workspace_rollout(
                projects, "/releases/new", "/releases/old",
                authority_dir="/authority",
            )
        factory.assert_called_once_with(projects, state_dir=Path("/authority"))
        authority.advance.assert_called_once_with(
            "c" * 64, {"b" * 64: 2, "a" * 64: 2}
        )
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(result["authority_epoch"], 8)
        self.assertEqual(result["target_digest"], "b" * 64)
        self.assertEqual(result["rollback_digest"], "a" * 64)

    def test_rejects_target_below_minimum_abi_before_advance(self):
        rollback, _target, _restored, _binding = self.admissions()
        authority = MagicMock()
        with patch.object(module, "verify_launch", return_value=rollback), \
             patch.object(module, "verify_release_manifest",
                          return_value={"release_digest": "b" * 64}), \
             patch.object(module, "release_enforcement_abi", return_value=1), \
             patch.object(module, "WorkspaceAuthority",
                          return_value=authority):
            with self.assertRaisesRegex(RuntimeError,
                                        "WORKSPACE_RELEASE_INCOMPATIBLE"):
                module._authorize_workspace_rollout(
                    {}, "/releases/new", "/releases/old",
                    authority_dir="/authority",
                )
        authority.advance.assert_not_called()

    def test_rejects_binding_change_after_authority_advance(self):
        rollback, target, restored, new_binding = self.admissions()
        target = dict(target, binding=dict(new_binding, authority_epoch=9))
        authority = MagicMock()
        authority.advance.return_value = new_binding
        with patch.object(module, "verify_launch",
                          side_effect=[rollback, target, restored]), \
             patch.object(module, "verify_release_manifest",
                          return_value={"release_digest": "b" * 64}), \
             patch.object(module, "release_enforcement_abi", return_value=2), \
             patch.object(module, "WorkspaceAuthority",
                          return_value=authority):
            with self.assertRaisesRegex(RuntimeError,
                                        "WORKSPACE_AUTHORITY_CHANGED"):
                module._authorize_workspace_rollout(
                    {}, "/releases/new", "/releases/old",
                    authority_dir="/authority",
                )


if __name__ == "__main__":
    unittest.main()
