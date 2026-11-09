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
from host_access_ui import HostAccessApprovals  # noqa: E402
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

    def test_status_shows_only_selected_project_requests_but_grant_remains_global(self):
        self.enable()
        alpha_request = self.access.request("alpha")
        beta_request = self.access.request("beta")

        alpha_status = self.access.get("alpha")
        beta_status = self.access.get("beta")

        self.assertEqual(
            [request["request_id"] for request in alpha_status["requests"]],
            [alpha_request["request_id"]],
        )
        self.assertEqual(
            [request["request_id"] for request in beta_status["requests"]],
            [beta_request["request_id"]],
        )
        grant = self.access.approve_web(
            "alpha", request_id=alpha_request["request_id"], lifetime="timed",
            duration_seconds=60, allow_codex=False, confirmation="alpha",
        )
        self.assertEqual(
            self.access.require("beta", grant_id=grant["grant_id"])["grant_id"],
            grant["grant_id"],
        )

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

    def test_revoked_grant_can_be_replaced_through_public_request_and_approval(self):
        self.enable()
        revoked = self.approve("alpha")
        self.access.revoke_web(
            "beta", grant_id=revoked["grant_id"], confirmation="beta",
        )

        request = self.access.request("beta")
        replacement = self.access.approve_web(
            "beta", request_id=request["request_id"], lifetime="timed",
            duration_seconds=120, allow_codex=True, confirmation="beta",
        )

        self.assertNotEqual(replacement["grant_id"], revoked["grant_id"])
        self.assertEqual(replacement["grant_state_revision"], 4)
        self.assertEqual(
            self.access.require(
                "alpha", grant_id=replacement["grant_id"], codex=True,
            )["grant_id"],
            replacement["grant_id"],
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

    def test_active_grant_revision_must_equal_current_state_revision(self):
        self.enable()
        grant = self.approve()
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["grant"]["grant_state_revision"] = 102
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)

        self.assert_error(
            "HOST_ACCESS_STATE_INVALID",
            lambda: self.access.require("alpha", grant_id=grant["grant_id"]),
        )

    def test_pending_request_revision_must_equal_current_state_revision(self):
        self.enable()
        request = self.access.request("alpha")
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["requests"][request["request_id"]]["grant_state_revision"] = 99
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)

        self.assert_error("HOST_ACCESS_STATE_INVALID", lambda: self.access.get("alpha"))

    def test_revoked_grant_requires_previous_revision_while_pending_stays_current(self):
        self.enable()
        grant = self.approve()
        pending = self.access.request("alpha")
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        before_revoke = json.loads(state_path.read_text(encoding="utf-8"))
        self.access.revoke_web(
            "alpha", grant_id=grant["grant_id"], confirmation="alpha",
        )
        revoked = json.loads(state_path.read_text(encoding="utf-8"))
        revoked["requests"][pending["request_id"]] = before_revoke["requests"][
            pending["request_id"]
        ]
        revoked["requests"][pending["request_id"]]["grant_state_revision"] = revoked[
            "grant_state_revision"
        ]
        state_path.write_text(json.dumps(revoked), encoding="utf-8")
        state_path.chmod(0o600)

        status = self.access.get("alpha")
        self.assertFalse(status["active"])
        self.assertEqual(status["reason"], "REVOKED")
        self.assertEqual(status["requests"][0]["request_id"], pending["request_id"])

        revoked["requests"][pending["request_id"]]["grant_state_revision"] -= 1
        state_path.write_text(json.dumps(revoked), encoding="utf-8")
        state_path.chmod(0o600)
        self.assert_error("HOST_ACCESS_STATE_INVALID", lambda: self.access.get("alpha"))

        revoked["requests"] = {}
        revoked["grant"]["grant_state_revision"] = revoked["grant_state_revision"]
        state_path.write_text(json.dumps(revoked), encoding="utf-8")
        state_path.chmod(0o600)
        self.assert_error("HOST_ACCESS_STATE_INVALID", lambda: self.access.get("alpha"))

    def test_non_object_persisted_request_is_a_bounded_state_error(self):
        self.enable()
        request = self.access.request("alpha")
        state_path = self.state / "host-access" / "installation-scope-v2.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["requests"][request["request_id"]] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)

        self.assert_error("HOST_ACCESS_STATE_INVALID", lambda: self.access.get("alpha"))

    def test_approval_card_reuses_suitable_grant_without_request_or_card(self):
        self.enable()
        grant = self.approve(duration_seconds=3600, allow_codex=True)
        approvals = HostAccessApprovals(self.access)

        result = approvals.open(
            "beta", lifetime="timed", duration_seconds=60, allow_codex=False,
        )

        receipt = result["structuredContent"]
        self.assertEqual(receipt["state"], "approved")
        self.assertEqual(receipt["grant"], {
            **grant, "project": "beta",
        })
        self.assertEqual(receipt["lifetime"], "timed")
        self.assertNotIn("_meta", result)
        self.assertEqual(self.access.get("alpha")["requests"], [])
        self.assertFalse(approvals._path("beta").exists())

    def test_approval_card_requires_owner_decision_for_stronger_capability(self):
        self.enable()
        self.approve(duration_seconds=3600, allow_codex=False)
        approvals = HostAccessApprovals(self.access)

        codex = approvals.open(
            "alpha", lifetime="timed", duration_seconds=60, allow_codex=True,
        )
        codex_card = codex["structuredContent"]
        self.assertEqual(codex_card["state"], "pending")
        self.assertEqual(codex_card["lifetime"], "timed")
        approvals.cancel(
            "alpha", card_id=codex_card["card_id"],
            confirmation_token=codex["_meta"]["host_access_confirmation"],
        )

        durable = approvals.open(
            "beta", lifetime="until_revoked", duration_seconds=None,
            allow_codex=False,
        )
        durable_card = durable["structuredContent"]
        self.assertEqual(durable_card["state"], "pending")
        self.assertEqual(durable_card["lifetime"], "until_revoked")
        self.assertIsNone(durable_card["duration_seconds"])
        self.assertEqual(self.access.get("alpha")["requests"], [])
        self.assertEqual(len(self.access.get("beta")["requests"]), 1)

    def test_revoke_invalidates_pending_card_update_and_confirmation(self):
        self.enable()
        grant = self.approve(duration_seconds=3600)
        approvals = HostAccessApprovals(self.access)
        opened = approvals.open(
            "alpha", lifetime="timed", duration_seconds=60, allow_codex=True,
        )
        card = opened["structuredContent"]
        token = opened["_meta"]["host_access_confirmation"]

        self.access.revoke_web(
            "beta", grant_id=grant["grant_id"], confirmation="beta",
        )

        for callback in (
            lambda: approvals.update(
                "alpha", card_id=card["card_id"], confirmation_token=token,
                lifetime="timed", duration_seconds=120, allow_codex=True,
            ),
            lambda: approvals.confirm(
                "alpha", card_id=card["card_id"], confirmation_token=token,
            ),
        ):
            self.assert_error("HOST_ACCESS_CARD_STALE", callback)

    def test_until_revoked_card_never_parses_or_clamps_duration(self):
        self.enable()
        approvals = HostAccessApprovals(self.access)

        opened = approvals.open(
            "alpha", lifetime="until_revoked", duration_seconds=None,
            allow_codex=False,
        )
        card = opened["structuredContent"]
        approved = approvals.confirm(
            "alpha", card_id=card["card_id"],
            confirmation_token=opened["_meta"]["host_access_confirmation"],
        )["structuredContent"]["grant"]

        self.assertEqual(approved["lifetime"], "until_revoked")
        self.assertIsNone(approved["expires_at"])

    def test_card_lifetime_change_replaces_backing_request(self):
        self.enable()
        approvals = HostAccessApprovals(self.access)
        opened = approvals.open("alpha", duration_seconds=60)
        first = opened["structuredContent"]

        updated = approvals.update(
            "alpha", card_id=first["card_id"],
            confirmation_token=opened["_meta"]["host_access_confirmation"],
            lifetime="until_revoked", duration_seconds=None, allow_codex=False,
        )

        second = updated["structuredContent"]
        self.assertNotEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["lifetime"], "until_revoked")
        self.assertEqual(
            [request["request_id"] for request in self.access.get("alpha")["requests"]],
            [second["request_id"]],
        )

    def approve_git(self, *, lifetime="timed", duration_seconds=60, allow_git=True):
        request = self.access.request("alpha", lifetime=lifetime)
        return self.access.approve_web(
            "alpha", request_id=request["request_id"], lifetime=lifetime,
            duration_seconds=duration_seconds, allow_codex=False, allow_git=allow_git,
            confirmation="alpha",
        )

    def test_legacy_grant_does_not_gain_git_permission_on_read(self):
        self.enable()
        self.approve()
        state = self.access.installation.load()
        state["grant"].pop("allow_git", None)
        self.access.installation.save(state)
        before = self.access.installation.state_path.read_bytes()
        restarted = self._access()
        self.assertIs(restarted.require("alpha").get("allow_git"), False)
        self.assert_error("HOST_ACCESS_GIT_DENIED",
                          lambda: restarted.require_git("alpha", "commit"))
        self.assertEqual(self.access.installation.state_path.read_bytes(), before)

    def test_explicit_git_permission_is_reusable_across_projects_without_codex(self):
        self.enable()
        grant = self.approve_git()
        restarted = self._access()
        for action in ("commit", "push"):
            allowed = restarted.require_git("beta", action)
            self.assertEqual(allowed["grant_id"], grant["grant_id"])
            self.assertEqual(allowed["grant_state_revision"], 2)
            self.assertIs(allowed["allow_git"], True)
            self.assertEqual(allowed["project"], "beta")
        self.assert_error("HOST_ACCESS_CODEX_DENIED",
                          lambda: restarted.require("alpha", codex=True))

    def test_git_permission_rejects_other_operations_and_missing_grants(self):
        self.enable()
        self.assert_error("HOST_ACCESS_REQUIRED",
                          lambda: self.access.require_git("alpha", "commit"))
        self.approve_git()
        for action in ("reset", "fetch", "force_push", "", None, 1, []):
            self.assert_error("INVALID_HOST_ACCESS_GIT_ACTION",
                              lambda: self.access.require_git("alpha", action))

    def test_git_opt_in_is_strict_boolean_without_consuming_request(self):
        self.enable()
        request = self.access.request("alpha")
        for value in (1, 0, "true", None):
            self.assert_error("INVALID_HOST_ACCESS_REQUEST",
                lambda: self.access.approve_web(
                    "alpha", request_id=request["request_id"], duration_seconds=60,
                    allow_codex=False, allow_git=value, confirmation="alpha"))
        grant = self.access.approve_web(
            "alpha", request_id=request["request_id"], duration_seconds=60,
            allow_codex=False, allow_git=True, confirmation="alpha")
        self.assertEqual(self.access.require_git("alpha", "commit")["grant_id"],
                         grant["grant_id"])

    def test_git_permission_stops_at_expiry_and_revocation(self):
        self.enable()
        timed = self.approve_git()
        self.now += timedelta(seconds=60)
        self.assert_error("HOST_ACCESS_EXPIRED",
                          lambda: self.access.require_git("beta", "push"))
        durable = self.approve_git(lifetime="until_revoked", duration_seconds=None)
        self.now += timedelta(days=3650)
        self.assertIsNone(self.access.require_git("beta", "commit")["expires_at"])
        self.assertNotEqual(durable["grant_id"], timed["grant_id"])
        self.access.revoke_web("beta", grant_id=durable["grant_id"], confirmation="beta")
        self.assert_error("HOST_ACCESS_REVOKED",
                          lambda: self.access.require_git("alpha", "push"))

    def test_malformed_persisted_git_capability_fails_closed(self):
        self.enable()
        self.approve_git()
        original = self.access.installation.load()
        path = self.access.installation.state_path
        for value in (1, 0, "true", None, []):
            state = json.loads(json.dumps(original))
            state["grant"]["allow_git"] = value
            path.write_text(json.dumps(state), encoding="utf-8")
            path.chmod(0o600)
            self.assert_error("HOST_ACCESS_STATE_INVALID",
                              lambda: self.access.require_git("alpha", "commit"))

    def test_git_permission_revalidates_installation_and_capability_binding(self):
        self.enable()
        self.approve_git()
        original = self.access.installation.load()
        path = self.access.installation.state_path
        for field, value, code in (
            ("installation_id", "e" * 32, "HOST_ACCESS_INSTALLATION_CHANGED"),
            ("capability_generation", 99, "HOST_ACCESS_CAPABILITY_CHANGED"),
        ):
            state = json.loads(json.dumps(original))
            state[field] = state["grant"][field] = value
            path.write_text(json.dumps(state), encoding="utf-8")
            path.chmod(0o600)
            self.assert_error(code, lambda: self.access.require_git("alpha", "push"))

    def test_git_card_requires_owner_confirmation_without_upgrading_existing_grant(self):
        self.enable()
        previous = self.approve(duration_seconds=3600)
        approvals = HostAccessApprovals(self.access)
        opened = approvals.open("alpha", duration_seconds=60, allow_git=True)
        card = opened["structuredContent"]
        token = opened["_meta"]["host_access_confirmation"]
        self.assertEqual(card["state"], "pending")
        self.assertIs(card["allow_git"], True)
        self.assertNotIn(token, json.dumps(card))
        self.assertNotIn(token, json.dumps(opened["content"]))
        self.assertEqual(self.access.require("alpha")["grant_id"], previous["grant_id"])
        self.assert_error("HOST_ACCESS_GIT_DENIED",
                          lambda: self.access.require_git("alpha", "commit"))
        result = approvals.confirm("alpha", card_id=card["card_id"], confirmation_token=token)
        grant = result["structuredContent"]["grant"]
        self.assertNotIn("_meta", result)
        self.assertNotEqual(grant["grant_id"], previous["grant_id"])
        self.assertEqual(self.access.require_git("beta", "commit")["grant_id"],
                         grant["grant_id"])

    def test_card_git_option_change_rotates_token_without_granting(self):
        self.enable()
        approvals = HostAccessApprovals(self.access)
        opened = approvals.open("alpha", duration_seconds=60)
        card = opened["structuredContent"]
        token = opened["_meta"]["host_access_confirmation"]
        updated = approvals.update(
            "alpha", card_id=card["card_id"], confirmation_token=token,
            lifetime="until_revoked", duration_seconds=None,
            allow_codex=False, allow_git=True)
        self.assertIs(updated["structuredContent"]["allow_git"], True)
        self.assertNotEqual(updated["_meta"]["host_access_confirmation"], token)
        self.assertFalse(self.access.get("alpha")["active"])
        self.assert_error("HOST_ACCESS_CARD_DENIED",
            lambda: approvals.confirm("alpha", card_id=card["card_id"],
                                      confirmation_token=token))
        approvals.confirm("alpha", card_id=card["card_id"],
            confirmation_token=updated["_meta"]["host_access_confirmation"])
        self.assertEqual(self.access.require_git("beta", "push")["lifetime"], "until_revoked")

    def test_suitable_git_grant_reuses_card_without_new_capability(self):
        self.enable()
        grant = self.approve_git(lifetime="until_revoked", duration_seconds=None)
        approvals = HostAccessApprovals(self.access)
        result = approvals.open("beta", lifetime="until_revoked", allow_git=True)
        self.assertEqual(result["structuredContent"]["state"], "approved")
        self.assertEqual(result["structuredContent"]["grant"]["grant_id"], grant["grant_id"])
        self.assertIs(result["structuredContent"]["allow_git"], True)
        self.assertNotIn("_meta", result)
        self.assertEqual(self.access.get("beta")["requests"], [])

    def corrupt_recovery_card(self, *, lifetime="timed", approve=False):
        self.enable()
        cards = HostAccessApprovals(self.access)
        opened = cards.open("alpha", lifetime=lifetime,
                            duration_seconds=60 if lifetime == "timed" else None)
        if approve:
            cards.confirm("alpha", card_id=opened["structuredContent"]["card_id"],
                          confirmation_token=opened["_meta"]["host_access_confirmation"])
        path = cards._path("alpha")
        raw = json.loads(path.read_text())
        raw.pop("token_digest")
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        return cards, path

    def test_recovery_denies_another_projects_installation_request(self):
        cards, path = self.corrupt_recovery_card()
        self.now += timedelta(seconds=host_access.REQUEST_TTL_SECONDS + 1)
        self.access.request("beta")
        before = path.read_bytes()
        self.assert_error("HOST_ACCESS_CARD_RECOVERY_DENIED",
                          lambda: cards.open("alpha", recover_invalid_card=True))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.access.get("beta")["requests"]), 1)

    def test_recovery_denies_live_timed_installation_grant(self):
        cards, path = self.corrupt_recovery_card(approve=True)
        self.assert_error("HOST_ACCESS_CARD_RECOVERY_DENIED",
                          lambda: cards.open("alpha", recover_invalid_card=True))
        self.assertTrue(path.exists())
        self.assertTrue(self.access.get("beta")["active"])

    def test_recovery_denies_until_revoked_installation_grant(self):
        cards, path = self.corrupt_recovery_card(lifetime="until_revoked", approve=True)
        self.assert_error("HOST_ACCESS_CARD_RECOVERY_DENIED",
                          lambda: cards.open("alpha", lifetime="until_revoked",
                                             recover_invalid_card=True))
        self.assertTrue(path.exists())
        self.assertTrue(self.access.get("beta")["active"])

    def test_recovery_after_expired_installation_request_requires_new_click(self):
        cards, path = self.corrupt_recovery_card()
        before = path.read_bytes()
        self.now += timedelta(seconds=host_access.REQUEST_TTL_SECONDS + 1)
        replacement = cards.open("alpha", recover_invalid_card=True)
        self.assertEqual(replacement["structuredContent"]["state"], "pending")
        self.assertIn("host_access_confirmation", replacement["_meta"])
        self.assertFalse(self.access.get("alpha")["active"])
        archives = list(path.parent.glob(path.stem + ".rejected-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), before)

    def test_recovery_after_expired_installation_grant_requires_new_click(self):
        cards, _path = self.corrupt_recovery_card(approve=True)
        self.now += timedelta(seconds=61)
        replacement = cards.open("alpha", recover_invalid_card=True)
        self.assertEqual(replacement["structuredContent"]["state"], "pending")
        self.assertFalse(self.access.get("alpha")["active"])



if __name__ == "__main__":
    unittest.main()
