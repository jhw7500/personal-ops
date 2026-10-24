"""Installation-scoped host-access grants and irreversible v2 migration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_access  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from workspace_host import host_operation_lane  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class HostAccessInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir(mode=0o700)
        self.beta.mkdir(mode=0o700)
        self.state = self.root / "state"
        owner = self.root / "owner"
        owner.mkdir(mode=0o700)
        self.policy_path = owner / "policy.json"
        self._write_policy([])
        self.workspace = WorkspaceTools(
            {"alpha": self.alpha, "beta": self.beta},
            state_dir=self.root / "workspace-authority",
            config_path=self.policy_path,
        )
        snapshot = self.workspace.policy.load(required=True)
        self.workspace.authority.initialize(
            snapshot.digest, {"f" * 64: 3}, minimum_abi=3,
        )
        self.now = datetime(2026, 10, 24, 1, 2, 3, tzinfo=timezone.utc)
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        self.runtime_identity = "a" * 64
        self.now_patch = mock.patch.object(
            host_access, "_utcnow", side_effect=lambda: self.now,
        )
        self.boot_patch = mock.patch.object(
            host_access, "_current_boot_id", side_effect=lambda: self.boot_id,
        )
        self.launch_patch = mock.patch.object(
            host_access, "verify_launch", create=True, side_effect=self._verify_launch,
        )
        self.now_patch.start()
        self.boot_patch.start()
        self.verify_launch = self.launch_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.boot_patch.stop)
        self.addCleanup(self.launch_patch.stop)
        self.access = self._access()

    def _write_policy(self, protected_paths):
        self.policy_path.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.root),
            "artifacts": [],
            "protected_paths": protected_paths,
        }), encoding="utf-8")
        self.policy_path.chmod(0o600)

    def _access(self, projects=None):
        return HostAccess(
            projects or {"alpha": self.alpha, "beta": self.beta}, self.state,
            trusted_runtime=lambda: self.runtime_identity,
            workspace=self.workspace,
        )

    def _verify_launch(self, release, authority_dir, *, enforcement_required):
        snapshot = self.workspace.policy.load(required=True)
        binding = self.workspace.authority.binding(snapshot.digest)
        return {
            "status": "launch_allowed",
            "authority_active": True,
            "release_digest": self.runtime_identity,
            "release_abi": 3,
            "enforcement_abi": 3,
            "minimum_abi": 3,
            "host_access_capability_generation": 1,
            "runtime": str(Path(release).absolute()),
            "authority_dir": str(Path(authority_dir).absolute()),
            "binding": binding,
        }

    def assert_error(self, code, operation):
        with self.assertRaises(HostRuntimeError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def enable(self):
        return self.access.enable_installation_scope()

    def approve(self, project="alpha", *, lifetime="timed", duration_seconds=60,
                allow_codex=False):
        request = self.access.request(project, lifetime=lifetime)
        return self.access.approve_web(
            project, request_id=request["request_id"], lifetime=lifetime,
            duration_seconds=duration_seconds, allow_codex=allow_codex,
            confirmation=project,
        )

    def test_enable_requires_quiescence_and_creates_no_grant(self):
        with host_operation_lane(self.workspace):
            self.assert_error("WORKSPACE_HOST_LANE_BUSY", self.enable)
        self.assertFalse(self.access.installation_scope_enabled())

        enabled = self.enable()

        self.assertEqual(enabled, {
            "version": 2, "enabled": True, "grant_state_revision": 1,
        })
        self.assertTrue(self.access.installation_scope_enabled())
        status = self.access.get("alpha")
        self.assertFalse(status["active"])
        self.assertEqual(status["reason"], "NO_GRANT")
        self.assertEqual(status["version"], 2)
        self.assertEqual(status["grant_state_revision"], 1)

    def test_one_installation_grant_is_reusable_across_projects(self):
        self.enable()
        grant = self.approve("alpha", allow_codex=True)

        allowed = self.access.require(
            "beta", grant_id=grant["grant_id"], codex=True,
        )

        self.assertEqual(allowed["project"], "beta")
        self.assertEqual(allowed["scope"], "installation")
        self.assertEqual(allowed["version"], 2)
        self.assertEqual(allowed["grant_state_revision"], 2)

    def test_grant_survives_restart_boot_runtime_and_policy_epoch_changes(self):
        self.enable()
        grant = self.approve("alpha", allow_codex=True)
        self.boot_id = "99999999-8888-7777-6666-555555555555"
        self.runtime_identity = "b" * 64
        self._write_policy([str(self.root / "protected")])
        snapshot = self.workspace.policy.load(required=True)
        self.workspace.authority.advance(snapshot.digest, {"f" * 64: 3})
        restarted = self._access()

        allowed = restarted.require(
            "beta", grant_id=grant["grant_id"], codex=True,
        )

        self.assertEqual(allowed["grant_id"], grant["grant_id"])
        self.assertEqual(allowed["grant_state_revision"], 2)
        self.assertGreaterEqual(self.verify_launch.call_count, 2)

    def test_capability_generation_change_invalidates_existing_grant(self):
        self.enable()
        grant = self.approve()
        original = self._verify_launch

        def changed_generation(*args, **kwargs):
            admitted = original(*args, **kwargs)
            admitted["host_access_capability_generation"] = 2
            return admitted

        self.verify_launch.side_effect = changed_generation

        self.assert_error(
            "HOST_ACCESS_OWNER_SETUP_REQUIRED",
            lambda: self.access.require("alpha", grant_id=grant["grant_id"]),
        )

    def test_timed_grant_expires_but_until_revoked_has_null_expiry(self):
        self.enable()
        timed = self.approve(duration_seconds=60)
        self.assertEqual(timed["lifetime"], "timed")
        self.now += timedelta(seconds=60)
        self.assert_error(
            "HOST_ACCESS_EXPIRED",
            lambda: self.access.require("alpha", grant_id=timed["grant_id"]),
        )

        durable = self.approve(
            lifetime="until_revoked", duration_seconds=None,
        )
        self.assertEqual(durable["lifetime"], "until_revoked")
        self.assertIsNone(durable["expires_at"])
        self.now += timedelta(days=3650)
        self.assertEqual(
            self.access.require("beta", grant_id=durable["grant_id"])["grant_id"],
            durable["grant_id"],
        )

    def test_approval_and_revoke_invalidate_all_older_callbacks(self):
        self.enable()
        first = self.access.request("alpha")
        old = self.access.request("beta")
        grant = self.access.approve_web(
            "alpha", request_id=first["request_id"], lifetime="timed",
            duration_seconds=60, allow_codex=False, confirmation="alpha",
        )
        self.assert_error(
            "HOST_ACCESS_REQUEST_NOT_FOUND",
            lambda: self.access.approve_web(
                "beta", request_id=old["request_id"], lifetime="timed",
                duration_seconds=60, allow_codex=False, confirmation="beta",
            ),
        )

        pending = self.access.request("alpha")
        self.access.revoke_web(
            "beta", grant_id=grant["grant_id"], confirmation="beta",
        )
        self.assert_error(
            "HOST_ACCESS_REQUEST_NOT_FOUND",
            lambda: self.access.approve_web(
                "alpha", request_id=pending["request_id"], lifetime="timed",
                duration_seconds=60, allow_codex=False, confirmation="alpha",
            ),
        )

    def test_marker_blocks_v1_fallback_when_v2_state_is_missing_or_corrupt(self):
        legacy = self.approve(duration_seconds=60)
        self.enable()
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        state_path.unlink()
        replacement = self._access()
        self.assert_error(
            "HOST_ACCESS_STATE_INVALID",
            lambda: replacement.require("alpha", grant_id=legacy["grant_id"]),
        )
        state_path.write_text("{}", encoding="utf-8")
        state_path.chmod(0o600)
        self.assert_error("HOST_ACCESS_STATE_INVALID", lambda: replacement.get("alpha"))

    def test_corrupt_no_fallback_marker_fails_closed(self):
        self.enable()
        marker = self.state / "host-access" / "installation-scope-v2.enabled"
        marker.write_text("{}", encoding="utf-8")
        marker.chmod(0o600)

        self.assert_error(
            "HOST_ACCESS_STATE_INVALID",
            lambda: self.access.get("alpha"),
        )

    def test_revoked_v2_state_never_reads_reintroduced_v1_grant(self):
        legacy = self.approve(duration_seconds=60)
        self.enable()
        current = self.approve(duration_seconds=60)
        self.access.revoke_web(
            "alpha", grant_id=current["grant_id"], confirmation="alpha",
        )
        legacy_access = self._access()
        record = legacy_access._new_record("alpha", self.alpha)
        record["grant"] = {
            "grant_id": legacy["grant_id"],
            "created_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(seconds=60)).isoformat(),
            "allow_codex": False,
            "boot_id": self.boot_id,
            "runtime_identity": self.access._runtime_identity(),
            "uid": os.getuid(),
            "project_root": str(self.alpha),
            "revoked_at": None,
        }
        legacy_access._record_path("alpha").write_text(
            json.dumps(record), encoding="utf-8",
        )
        legacy_access._record_path("alpha").chmod(0o600)

        status = legacy_access.get("alpha")

        self.assertFalse(status["active"])
        self.assertEqual(status["reason"], "REVOKED")
        self.assertEqual(status["version"], 2)

    def test_enable_invalidates_records_for_projects_removed_from_configuration(self):
        alpha_grant = self.approve("alpha")
        beta_grant = self.approve("beta")
        alpha_record = self.access._record_path("alpha")
        beta_record = self.access._record_path("beta")
        self.assertTrue(alpha_record.exists())
        self.assertTrue(beta_record.exists())
        reduced = self._access({"alpha": self.alpha})

        reduced.enable_installation_scope()

        self.assertFalse(alpha_record.exists(), alpha_grant)
        self.assertFalse(beta_record.exists(), beta_grant)

    def test_repeated_enable_never_recreates_revision_one_after_interruption(self):
        self.enable()
        self.approve()
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        state_path.unlink()

        self.assert_error("HOST_ACCESS_STATE_INVALID", self.enable)
        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
