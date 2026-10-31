import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_runtime_config import HostRuntimeError  # noqa: E402
from workspace_host import (  # noqa: E402
    acquire_host_operation_lane, complete_host_operation,
    host_operation_lane, publication_lane, resolve_workspace,
)
from workspace_tools import WorkspaceTools  # noqa: E402


class WorkspaceHostLaneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.projects = self.base / "projects"
        self.repo = self.projects / "demo"
        self.repo.mkdir(parents=True)
        os.chmod(self.projects, 0o700)
        os.chmod(self.repo, 0o700)
        self.owner = self.base / "owner"
        self.owner.mkdir(mode=0o700)
        self.policy_path = self.owner / "policy.json"
        self.policy_path.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.projects),
            "artifacts": [],
            "protected_paths": [],
        }), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)
        self.authority_state = self.base / "workspace-authority"
        self.workspace = WorkspaceTools(
            {"demo": self.repo}, state_dir=self.authority_state,
            config_path=self.policy_path)
        snapshot = self.workspace.policy.load(required=True)
        self.workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})

    def tearDown(self):
        self.temp.cleanup()

    def child_lane(self, lane):
        script = """
import sys
from workspace_tools import WorkspaceTools
from workspace_host import host_operation_lane, publication_lane
from host_runtime_config import HostRuntimeError
workspace = WorkspaceTools({'demo': sys.argv[1]}, state_dir=sys.argv[2], config_path=sys.argv[3])
chosen = host_operation_lane if sys.argv[4] == 'host' else publication_lane
try:
    with chosen(workspace, timeout_seconds=0):
        print('acquired')
except HostRuntimeError as exc:
    print(exc.code)
"""
        return subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.repo),
             str(self.authority_state), str(self.policy_path), lane],
            check=True, capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        ).stdout.strip()

    def test_shared_host_lane_blocks_cross_process_publication_until_release(self):
        with host_operation_lane(self.workspace):
            self.assertEqual(self.child_lane("publication"), "WORKSPACE_HOST_LANE_BUSY")
            self.assertEqual(self.child_lane("host"), "acquired")

    def test_first_reservation_persists_parent_directory_before_host_side_effects(self):
        synced = []
        real_fsync = os.fsync

        def observe(descriptor):
            info = os.fstat(descriptor)
            synced.append((info.st_dev, info.st_ino))
            return real_fsync(descriptor)

        root = self.authority_state.stat()
        with mock.patch("workspace_host.os.fsync", side_effect=observe):
            with host_operation_lane(self.workspace):
                self.assertIn((root.st_dev, root.st_ino), synced,
                              "The durable registry's parent entry must survive OS crash")

        self.assertEqual(self.child_lane("publication"), "acquired")
        lock = self.authority_state / ".host-publication.lock"
        self.assertTrue(lock.is_file())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_exclusive_publication_blocks_new_host_operation(self):
        with publication_lane(self.workspace):
            self.assertEqual(self.child_lane("host"), "WORKSPACE_HOST_LANE_BUSY")

    def test_owner_death_keeps_durable_reservation_until_verified_recovery(self):
        operation_id = "1" * 32
        script = """
import os, sys
from workspace_tools import WorkspaceTools
from workspace_host import acquire_host_operation_lane
workspace = WorkspaceTools({'demo': sys.argv[1]}, state_dir=sys.argv[2], config_path=sys.argv[3])
acquire_host_operation_lane(
    workspace, operation_id=sys.argv[4], kind='host-command')
os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.repo),
             str(self.authority_state), str(self.policy_path), operation_id],
            check=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )

        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(self.workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        entry = (self.authority_state / ".host-active" /
                 f"{operation_id}--host-command")
        self.assertEqual(entry.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(entry.parent.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.authority_state / ".host-active.lock").stat().st_mode),
            0o600)

        complete_host_operation(
            self.workspace, operation_id, "host-command", termination_verified=False)
        self.assertTrue(entry.exists())
        complete_host_operation(
            self.workspace, operation_id, "host-command", termination_verified=True)
        with publication_lane(self.workspace):
            pass

    def test_operation_id_collision_across_kinds_fails_closed(self):
        operation_id = "2" * 32
        descriptor = acquire_host_operation_lane(
            self.workspace, operation_id=operation_id, kind="host-command")
        try:
            with self.assertRaises(HostRuntimeError) as caught:
                acquire_host_operation_lane(
                    self.workspace, operation_id=operation_id, kind="registered")
            self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_INVALID")
        finally:
            complete_host_operation(
                self.workspace, operation_id, "host-command", termination_verified=True)
            os.close(descriptor)

    def test_lane_does_not_initialize_missing_workspace_state(self):
        missing_state = self.base / "missing-authority"
        workspace = WorkspaceTools(
            {"demo": self.repo}, state_dir=missing_state, config_path=self.policy_path)

        with self.assertRaises(HostRuntimeError) as caught:
            with host_operation_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_AUTHORITY_INVALID")
        self.assertFalse(missing_state.exists())

    def test_unsafe_existing_lock_is_rejected_without_chmod_side_effect(self):
        victim = self.base / "victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        os.chmod(victim, 0o640)
        os.link(victim, self.authority_state / ".host-publication.lock")

        with self.assertRaises(HostRuntimeError) as caught:
            with host_operation_lane(self.workspace):
                pass

        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_INVALID")
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o640)
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_default_workspace_discovery_is_read_only_and_fail_closed(self):
        config_home = self.base / "default-config"
        state_home = self.base / "default-state"
        with mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(config_home), "XDG_STATE_HOME": str(state_home),
        }):
            self.assertIsNone(resolve_workspace({"demo": self.repo}))
            self.assertFalse(config_home.exists())
            self.assertFalse(state_home.exists())

            policy = config_home / "chatgpt-coding-mcp" / "workspace-policy.json"
            policy.parent.mkdir(parents=True, mode=0o700)
            policy.write_text(json.dumps({
                "version": 1, "base_root": str(self.projects),
                "artifacts": [], "protected_paths": [],
            }), encoding="utf-8")
            os.chmod(policy, 0o600)
            discovered = resolve_workspace({"demo": self.repo})
            self.assertTrue(discovered.enabled)
            with self.assertRaises(HostRuntimeError) as caught:
                with host_operation_lane(discovered):
                    pass
            self.assertEqual(caught.exception.code, "WORKSPACE_AUTHORITY_INVALID")
            self.assertFalse((state_home / "chatgpt-coding-mcp" /
                              "workspace-authority").exists())


if __name__ == "__main__":
    unittest.main()
