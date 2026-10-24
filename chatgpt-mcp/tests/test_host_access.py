"""Owner-controlled, expiring host-access grant state."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_access  # noqa: E402
import host_runtime_operations  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_access_ui import HostAccessApprovals  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class HostAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.state_root = self.root / "state"
        self.now = datetime(2026, 10, 15, 1, 2, 3, tzinfo=timezone.utc)
        self.runtime_identity = "a" * 64
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        self.now_patch = mock.patch.object(host_access, "_utcnow", side_effect=lambda: self.now)
        self.boot_patch = mock.patch.object(
            host_access, "_current_boot_id", side_effect=lambda: self.boot_id)
        self.now_patch.start()
        self.boot_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.boot_patch.stop)
        self.access = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity,
        )

    def assert_error(self, code, function):
        with self.assertRaises(HostRuntimeError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def approve(self, *, allow_codex=False, duration_seconds=14_400):
        request = self.access.request("personal-ops")
        return self.access.approve_web(
            "personal-ops", request_id=request["request_id"],
            duration_seconds=duration_seconds, allow_codex=allow_codex,
            confirmation="personal-ops",
        )

    def active_workspace(self):
        os.chmod(self.project_root, 0o700)
        owner = self.root / "workspace-owner"
        owner.mkdir(mode=0o700)
        policy_path = owner / "policy.json"
        policy_path.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.root),
            "artifacts": [],
            "protected_paths": [],
        }), encoding="utf-8")
        os.chmod(policy_path, 0o600)
        workspace = WorkspaceTools(
            {"personal-ops": self.project_root},
            state_dir=self.root / "workspace-authority",
            config_path=policy_path,
        )
        snapshot = workspace.policy.load(required=True)
        workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
        return workspace, policy_path

    def test_active_workspace_epoch_is_part_of_legacy_runtime_binding(self):
        workspace, policy_path = self.active_workspace()
        access = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity, workspace=workspace)
        request = access.request("personal-ops")
        grant = access.approve_web(
            "personal-ops", request_id=request["request_id"], duration_seconds=60,
            allow_codex=False, confirmation="personal-ops")
        pending = access.request("personal-ops")
        original_identity = access._runtime_identity()
        self.assertNotEqual(original_identity, self.runtime_identity)
        stored = access._load("personal-ops", self.project_root)
        self.assertEqual(stored["grant"]["runtime_identity"], original_identity)
        self.assertNotIn("authority_epoch", stored["grant"])
        self.assertNotIn("policy_digest", stored["grant"])
        original_policy = policy_path.read_bytes()

        policy_path.write_text(json.dumps({
            "version": 1,
            "base_root": str(self.root),
            "artifacts": [],
            "protected_paths": [str(self.project_root / "protected")],
        }), encoding="utf-8")
        new_snapshot = workspace.policy.load(required=True)
        workspace.authority.advance(new_snapshot.digest)
        self.assert_error(
            "HOST_ACCESS_RUNTIME_CHANGED",
            lambda: access.require("personal-ops", grant_id=grant["grant_id"]))
        self.assert_error(
            "HOST_ACCESS_REQUEST_STALE",
            lambda: access.approve_web(
                "personal-ops", request_id=pending["request_id"], duration_seconds=60,
                allow_codex=False, confirmation="personal-ops"))

        policy_path.write_bytes(original_policy)
        restored = workspace.policy.load(required=True)
        workspace.authority.advance(restored.digest)
        self.assertNotEqual(access._runtime_identity(), original_identity)
        self.assert_error(
            "HOST_ACCESS_RUNTIME_CHANGED",
            lambda: access.require("personal-ops", grant_id=grant["grant_id"]))

    def test_active_workspace_missing_or_corrupt_authority_is_bounded(self):
        workspace, policy_path = self.active_workspace()
        policy_bytes = policy_path.read_bytes()
        access = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity, workspace=workspace)
        policy_path.unlink()
        self.assert_error("HOST_ACCESS_WORKSPACE_INVALID", access._runtime_identity)
        policy_path.write_text("{}", encoding="utf-8")
        os.chmod(policy_path, 0o600)
        self.assert_error("HOST_ACCESS_WORKSPACE_INVALID", access._runtime_identity)
        policy_path.write_bytes(policy_bytes)
        authority_file = workspace.authority.state_dir / "authority.json"
        authority_file.write_text("{}", encoding="utf-8")
        os.chmod(authority_file, 0o600)
        self.assert_error("HOST_ACCESS_WORKSPACE_INVALID", access._runtime_identity)

    def test_direct_host_access_discovers_installed_default_workspace(self):
        config_home = self.root / "default-config"
        state_home = self.root / "default-workspace-state"
        with mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(config_home), "XDG_STATE_HOME": str(state_home),
        }):
            policy = config_home / "chatgpt-coding-mcp" / "workspace-policy.json"
            policy.parent.mkdir(parents=True, mode=0o700)
            config_home.chmod(0o700)
            policy.write_text(json.dumps({
                "version": 1, "base_root": str(self.root),
                "artifacts": [], "protected_paths": [],
            }), encoding="utf-8")
            os.chmod(policy, 0o600)
            workspace = WorkspaceTools({"personal-ops": self.project_root})
            snapshot = workspace.policy.load(required=True)
            workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
            access = HostAccess(
                {"personal-ops": self.project_root}, self.state_root,
                trusted_runtime=lambda: self.runtime_identity)

        self.assertIsNotNone(access.workspace)
        self.assertNotEqual(access._runtime_identity(), self.runtime_identity)

    def test_no_grant_and_request_only_are_safe_public_status(self):
        status = self.access.get("personal-ops")
        identity = status.pop("host_identity")
        self.assertEqual(status, {
            "project": "personal-ops", "scope": "installation", "active": False,
            "reason": "NO_GRANT", "requests": [],
        })
        self.assertEqual(set(identity), {"hostname", "user", "uid", "groups",
                                         "filesystem_isolated", "effective_access",
                                         "passwordless_sudo_verified"})
        self.assertEqual(identity["uid"], os.getuid())
        self.assertFalse(identity["filesystem_isolated"])
        self.assertEqual(identity["effective_access"], "host_user_filesystem_network_groups")
        self.assertFalse(identity["passwordless_sudo_verified"])
        requested = self.access.request("personal-ops")
        self.assertEqual(set(requested), {"project", "scope", "request_id", "expires_at"})
        status = self.access.get("personal-ops")
        self.assertFalse(status["active"])
        self.assertEqual(status["reason"], "NO_GRANT")
        self.assertEqual(status["requests"], [{
            "request_id": requested["request_id"], "expires_at": requested["expires_at"],
        }])

    def test_get_reports_server_derived_host_user_and_real_groups(self):
        with (mock.patch.object(host_access.socket, "gethostname", return_value="build-host"),
              mock.patch.object(host_access.pwd, "getpwuid",
                                return_value=SimpleNamespace(pw_name="fixture-user")),
              mock.patch.object(host_access.os, "getgid", return_value=100),
              mock.patch.object(host_access.os, "getgroups", return_value=[300, 200, 100]),
              mock.patch.object(host_access.grp, "getgrgid", side_effect=lambda gid:
                                SimpleNamespace(gr_name={100: "users", 200: "docker",
                                                        300: "sudo"}[gid]))):
            identity = self.access.get("personal-ops")["host_identity"]
        self.assertEqual(identity, {
            "hostname": "build-host", "user": "fixture-user", "uid": os.getuid(),
            "groups": ["docker", "sudo", "users"], "filesystem_isolated": False,
            "effective_access": "host_user_filesystem_network_groups",
            "passwordless_sudo_verified": False,
        })

    def test_get_bounds_unknown_account_group_and_hostname_labels(self):
        def unknown(_value):
            raise KeyError("not in local account database")

        with (mock.patch.object(host_access.socket, "gethostname",
                                return_value="host\n" + "x" * 400),
              mock.patch.object(host_access.pwd, "getpwuid", side_effect=unknown),
              mock.patch.object(host_access.os, "getgid", return_value=400),
              mock.patch.object(host_access.os, "getgroups", return_value=[401, 400]),
              mock.patch.object(host_access.grp, "getgrgid", side_effect=unknown)):
            identity = self.access.get("personal-ops")["host_identity"]
        self.assertEqual(identity["user"], f"uid:{os.getuid()}")
        self.assertEqual(identity["groups"], ["gid:400", "gid:401"])
        self.assertNotIn("\n", identity["hostname"])
        self.assertLessEqual(len(identity["hostname"]), 255)

    def test_wrong_confirmation_and_invalid_duration_do_not_grant(self):
        requested = self.access.request("personal-ops")
        base = dict(request_id=requested["request_id"], allow_codex=False)
        self.assert_error("HOST_ACCESS_CONFIRMATION_DENIED", lambda: self.access.approve_web(
            "personal-ops", duration_seconds=60, confirmation="approve", **base))
        for duration in (0, 2_592_001, True, "60"):
            with self.subTest(duration=duration):
                self.assert_error("INVALID_HOST_ACCESS_DURATION", lambda: self.access.approve_web(
                    "personal-ops", duration_seconds=duration,
                    confirmation="personal-ops", **base))
        self.assertFalse(self.access.get("personal-ops")["active"])

    def test_none_duration_uses_thirty_day_default(self):
        requested = self.access.request("personal-ops")
        grant = self.access.approve_web(
            "personal-ops", request_id=requested["request_id"],
            duration_seconds=None, allow_codex=False, confirmation="personal-ops")
        self.assertEqual(
            datetime.fromisoformat(grant["expires_at"]), self.now + timedelta(days=30))

    def test_thirty_day_grant_survives_reconnect_and_expires_at_the_limit(self):
        grant = self.approve(duration_seconds=30 * 24 * 60 * 60)
        before = self.access._record_path("personal-ops").read_bytes()
        self.now += timedelta(days=29)
        reconnected = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity,
        )
        self.assertEqual(reconnected.require("personal-ops"), grant)
        self.assertEqual(reconnected._record_path("personal-ops").read_bytes(), before)
        self.now += timedelta(days=1)
        self.assert_error("HOST_ACCESS_EXPIRED", lambda: reconnected.require("personal-ops"))

    def test_eight_hour_grant_expires_at_the_limit(self):
        grant = self.approve(duration_seconds=28_800)
        self.assertEqual(datetime.fromisoformat(grant["expires_at"]),
                         self.now + timedelta(hours=8))
        self.now += timedelta(seconds=28_799)
        self.assertTrue(self.access.get("personal-ops")["active"])
        self.now += timedelta(seconds=1)
        self.assertEqual(self.access.get("personal-ops")["reason"], "EXPIRED")

    def test_valid_grant_survives_reconnect_and_status_refresh_without_extension(self):
        grant = self.approve(allow_codex=True, duration_seconds=28_800)
        before = self.access._record_path("personal-ops").read_bytes()
        for _ in range(3):
            self.now += timedelta(minutes=1)
            reconnected = HostAccess(
                {"personal-ops": self.project_root}, self.state_root,
                trusted_runtime=lambda: self.runtime_identity,
            )
            status = reconnected.get("personal-ops")
            self.assertTrue(status["active"])
            self.assertIsNone(status["reason"])
            for key in ("grant_id", "expires_at", "allow_codex"):
                self.assertEqual(status[key], grant[key])
            self.assertEqual(reconnected.require(
                "personal-ops", grant_id=grant["grant_id"], codex=True), grant)
            self.assertEqual(reconnected._record_path("personal-ops").read_bytes(), before)

    def test_new_unapproved_request_does_not_replace_or_revoke_valid_grant(self):
        grant = self.approve(allow_codex=False, duration_seconds=28_800)
        self.now += timedelta(minutes=1)
        requested = self.access.request("personal-ops")
        reconnected = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity,
        )
        status = reconnected.get("personal-ops")
        self.assertTrue(status["active"])
        self.assertEqual(status["requests"][0]["request_id"], requested["request_id"])
        self.assertEqual(reconnected.require("personal-ops"), grant)
        self.assertFalse(status["allow_codex"])
        self.assertEqual(status["expires_at"], grant["expires_at"])

    def test_opening_and_cancelling_card_preserves_existing_grant_options(self):
        grant = self.approve(allow_codex=True, duration_seconds=28_800)
        approvals = HostAccessApprovals(self.access)
        result = approvals.open("personal-ops", duration_seconds=60, allow_codex=False)
        self.assertEqual(self.access.require("personal-ops", codex=True), grant)
        card = result["structuredContent"]
        approvals.cancel(
            "personal-ops", card_id=card["card_id"],
            confirmation_token=result["_meta"]["host_access_confirmation"],
        )
        reconnected = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=lambda: self.runtime_identity,
        )
        self.assertEqual(reconnected.require("personal-ops", codex=True), grant)

    def test_approval_receipt_require_expiry_and_revoke(self):
        grant = self.approve(duration_seconds=60)
        self.assertEqual(set(grant), {
            "grant_id", "expires_at", "allow_codex", "project", "scope",
        })
        self.assertEqual(grant["project"], "personal-ops")
        self.assertEqual(grant["scope"], "installation")
        self.assertFalse(grant["allow_codex"])
        self.assertEqual(self.access.require(
            "personal-ops", grant_id=grant["grant_id"]), grant)

        self.assert_error("HOST_ACCESS_CONFIRMATION_DENIED", lambda: self.access.revoke_web(
            "personal-ops", grant_id=grant["grant_id"], confirmation="revoke"))
        revoked = self.access.revoke_web(
            "personal-ops", grant_id=grant["grant_id"], confirmation="personal-ops")
        self.assertEqual(revoked, {"project": "personal-ops", "grant_id": grant["grant_id"],
                                   "revoked": True})
        self.assert_error("HOST_ACCESS_REVOKED", lambda: self.access.require("personal-ops"))

        second = self.approve(duration_seconds=60)
        self.now += timedelta(seconds=61)
        self.assertEqual(self.access.get("personal-ops")["reason"], "EXPIRED")
        self.assert_error("HOST_ACCESS_EXPIRED", lambda: self.access.require(
            "personal-ops", grant_id=second["grant_id"]))

    def test_codex_is_separate_opt_in(self):
        ordinary = self.approve()
        self.access.require("personal-ops", grant_id=ordinary["grant_id"])
        self.assert_error("HOST_ACCESS_CODEX_DENIED", lambda: self.access.require(
            "personal-ops", grant_id=ordinary["grant_id"], codex=True))
        codex = self.approve(allow_codex=True)
        self.assertTrue(self.access.require(
            "personal-ops", grant_id=codex["grant_id"], codex=True)["allow_codex"])

    def test_renewal_invalidates_previous_grant(self):
        first = self.approve(duration_seconds=60)
        second = self.approve(duration_seconds=120)
        self.assertNotEqual(first["grant_id"], second["grant_id"])
        self.assert_error("HOST_ACCESS_GRANT_MISMATCH", lambda: self.access.require(
            "personal-ops", grant_id=first["grant_id"]))
        self.assertEqual(self.access.require("personal-ops"), second)

    def test_request_is_one_use_and_stale_requests_fail(self):
        requested = self.access.request("personal-ops")
        arguments = dict(request_id=requested["request_id"], duration_seconds=60,
                         allow_codex=False, confirmation="personal-ops")
        self.access.approve_web("personal-ops", **arguments)
        self.assert_error("HOST_ACCESS_REQUEST_NOT_FOUND", lambda: self.access.approve_web(
            "personal-ops", **arguments))

        stale = self.access.request("personal-ops")
        self.now += timedelta(days=7)
        self.assert_error("HOST_ACCESS_REQUEST_EXPIRED", lambda: self.access.approve_web(
            "personal-ops", request_id=stale["request_id"], duration_seconds=60,
            allow_codex=False, confirmation="personal-ops"))

    def test_request_and_grant_bind_runtime_boot_uid_and_project_root(self):
        stale_runtime = self.access.request("personal-ops")
        self.runtime_identity = "b" * 64
        self.assert_error("HOST_ACCESS_REQUEST_STALE", lambda: self.access.approve_web(
            "personal-ops", request_id=stale_runtime["request_id"], duration_seconds=60,
            allow_codex=False, confirmation="personal-ops"))

        self.runtime_identity = "a" * 64
        grant = self.approve()
        self.runtime_identity = "b" * 64
        self.assert_error("HOST_ACCESS_RUNTIME_CHANGED", lambda: self.access.require(
            "personal-ops", grant_id=grant["grant_id"]))
        self.runtime_identity = "a" * 64
        self.boot_id = "99999999-8888-7777-6666-555555555555"
        self.assert_error("HOST_ACCESS_BOOT_CHANGED", lambda: self.access.require(
            "personal-ops", grant_id=grant["grant_id"]))

        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        renamed = self.root / "renamed-project"
        self.project_root.rename(renamed)
        self.access.projects["personal-ops"] = renamed
        self.assert_error("HOST_ACCESS_PROJECT_CHANGED", lambda: self.access.require(
            "personal-ops", grant_id=grant["grant_id"]))

    def test_untrusted_runtime_cannot_request_approve_or_require(self):
        checkout = HostAccess(
            {"personal-ops": self.project_root}, self.root / "checkout-state")
        self.assert_error("UNSAFE_RUNTIME", lambda: checkout.request("personal-ops"))
        untrusted = HostAccess(
            {"personal-ops": self.project_root}, self.root / "other-state",
            trusted_runtime=False,
        )
        self.assert_error("UNSAFE_RUNTIME", lambda: untrusted.request("personal-ops"))
        requested = self.access.request("personal-ops")
        untrusted_same_state = HostAccess(
            {"personal-ops": self.project_root}, self.state_root,
            trusted_runtime=False,
        )
        self.assert_error("UNSAFE_RUNTIME", lambda: untrusted_same_state.approve_web(
            "personal-ops", request_id=requested["request_id"], duration_seconds=60,
            allow_codex=False, confirmation="personal-ops"))
        self.approve()
        self.assert_error("UNSAFE_RUNTIME", lambda: untrusted_same_state.require("personal-ops"))

    def test_state_is_under_host_access_with_private_modes(self):
        self.approve()
        access_root = self.state_root / "host-access"
        self.assertEqual(stat.S_IMODE(access_root.stat().st_mode), 0o700)
        for directory in (access_root, access_root / "records", access_root / "locks"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        records = list((access_root / "records").iterdir())
        locks = list((access_root / "locks").iterdir())
        self.assertEqual(len(records), 1)
        self.assertEqual(len(locks), 1)
        for path in records + locks:
            self.assertTrue(stat.S_ISREG(path.stat().st_mode))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        stored = json.loads(records[0].read_text())
        self.assertEqual(stored["project_root"], str(self.project_root.resolve()))
        self.assertEqual(stored["uid"], os.getuid())

    def test_guard_holds_writer_lock_until_command_admission_finishes(self):
        grant = self.approve()
        inside = threading.Event()
        release = threading.Event()
        revoked = threading.Event()

        def guarded():
            with self.access.guard("personal-ops", grant["grant_id"]):
                inside.set()
                release.wait(2)

        def revoke():
            self.access.revoke_web(
                "personal-ops", grant_id=grant["grant_id"], confirmation="personal-ops")
            revoked.set()

        holder = threading.Thread(target=guarded)
        holder.start()
        self.assertTrue(inside.wait(1))
        revoker = threading.Thread(target=revoke)
        revoker.start()
        time.sleep(0.05)
        self.assertFalse(revoked.is_set())
        release.set()
        holder.join(2)
        revoker.join(2)
        self.assertFalse(holder.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertTrue(revoked.is_set())


class InstalledAuthorityIdentityTests(unittest.TestCase):
    """Real private snapshots and grants, entirely under a disposable test home."""

    @classmethod
    def setUpClass(cls):
        module_dir = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "authority_test_installer", module_dir / "install-runtime.py")
        cls.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.installer)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-authority-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.releases = self.home / ".local/lib/chatgpt-coding-mcp/releases"
        self.source = self.root / "source/chatgpt-mcp"
        self.source.mkdir(parents=True)
        self.dashboard = self.source.parent / "work-dashboard"
        self.dashboard.mkdir()
        module_dir = Path(__file__).resolve().parents[1]
        for name in self.installer.FILES:
            (self.source / name).write_bytes((module_dir / name).read_bytes())
        for name in self.installer.DASHBOARD_FILES:
            (self.dashboard / name).write_bytes((module_dir.parent / "work-dashboard" / name).read_bytes())
        self.project = self.root / "project"
        self.project.mkdir()
        self.projects = {"personal-ops": self.project}
        self.access = HostAccess(self.projects, self.root / "state")
        self.base = self.install()

    def install(self):
        return Path(self.installer.install(
            self.source, self.releases, self.projects)["runtime_dir"])

    @contextmanager
    def runtime(self, release):
        with (mock.patch.object(Path, "home", return_value=self.home),
              mock.patch.object(host_access, "__file__", str(release / "host_access.py")),
              mock.patch.object(host_runtime_operations, "__file__",
                                str(release / "host_runtime_operations.py"))):
            yield

    def identity(self, release):
        with self.runtime(release):
            return host_access._installed_runtime_identity()

    def approve(self, *, allow_codex=False):
        request = self.access.request("personal-ops")
        return self.access.approve_web(
            "personal-ops", request_id=request["request_id"], duration_seconds=14_400,
            allow_codex=allow_codex, confirmation="personal-ops")

    def test_every_dashboard_asset_can_change_without_renewing_the_grant(self):
        with self.runtime(self.base):
            self.assertTrue(host_runtime_operations._verified_installation())
            grant = self.approve(allow_codex=True)
        original_record = self.access._record_path("personal-ops").read_bytes()
        identity = self.identity(self.base)
        self.assertRegex(identity, r"^[a-f0-9]{64}$")
        for name in self.installer.DASHBOARD_FILES:
            with self.subTest(asset=name):
                path = self.dashboard / name
                path.write_bytes(path.read_bytes() + b"\n")
                updated = self.install()
                self.assertNotEqual(updated, self.base)
                self.assertEqual(self.identity(updated), identity)
                with self.runtime(updated):
                    self.assertEqual(self.access.require(
                        "personal-ops", grant_id=grant["grant_id"], codex=True), grant)
                self.assertEqual(self.access._record_path("personal-ops").read_bytes(), original_record)

    def test_every_other_packaged_file_still_invalidates_the_grant(self):
        with self.runtime(self.base):
            grant = self.approve()
        identity = self.identity(self.base)
        for name in self.installer.FILES:
            with self.subTest(authority_file=name):
                path = self.source / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    updated = self.install()
                finally:
                    path.write_bytes(original)
                self.assertNotEqual(self.identity(updated), identity)
                with self.runtime(updated):
                    with self.assertRaises(HostRuntimeError) as caught:
                        self.access.require("personal-ops", grant_id=grant["grant_id"])
                    self.assertEqual(caught.exception.code, "HOST_ACCESS_RUNTIME_CHANGED")

    def test_pending_request_survives_ui_change_without_enabling_codex(self):
        with self.runtime(self.base):
            request = self.access.request("personal-ops")
        path = self.dashboard / "visualizer.js"
        path.write_bytes(path.read_bytes() + b"\n")
        updated = self.install()
        with self.runtime(updated):
            grant = self.access.approve_web(
                "personal-ops", request_id=request["request_id"], duration_seconds=60,
                allow_codex=False, confirmation="personal-ops")
            self.assertEqual(self.access.require("personal-ops"), grant)
            with self.assertRaises(HostRuntimeError) as caught:
                self.access.require("personal-ops", codex=True)
            self.assertEqual(caught.exception.code, "HOST_ACCESS_CODEX_DENIED")

    def test_excluded_ui_assets_are_still_checked_for_tampering(self):
        for name in self.installer.DASHBOARD_FILES:
            with self.subTest(asset=name):
                path = self.base / "work-dashboard" / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    self.assertIsNone(self.identity(self.base))
                finally:
                    path.write_bytes(original)

    def test_new_manifest_entries_affect_authority_even_under_dashboard_directory(self):
        original_identity = self.identity(self.base)
        extra = self.source / "future_helper.py"
        extra.write_text("# future authority file\n")
        with mock.patch.object(self.installer, "FILES", (*self.installer.FILES, extra.name)):
            extended = self.install()
        self.assertIsNotNone(self.identity(extended))
        self.assertNotEqual(self.identity(extended), original_identity)
        installed_extra = extended / extra.name
        installed_extra.write_bytes(installed_extra.read_bytes() + b"\n")
        self.assertIsNone(self.identity(extended))
        manifest_path = self.base / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        extra_ui = self.base / "work-dashboard/future.js"
        extra_ui.write_text("// unclassified future asset\n")
        extra_ui.chmod(0o600)
        manifest["work-dashboard/future.js"] = hashlib.sha256(extra_ui.read_bytes()).hexdigest()
        original = manifest_path.read_bytes()
        manifest_path.write_text(json.dumps(manifest))
        try:
            self.assertNotEqual(self.identity(self.base), original_identity)
            self.assertIsNotNone(self.identity(self.base))
        finally:
            manifest_path.write_bytes(original)

    def test_manifest_formatting_does_not_change_authority(self):
        identity = self.identity(self.base)
        manifest_path = self.base / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest_path.write_text(json.dumps(dict(reversed(list(manifest.items()))), indent=2) + "\n")
        self.assertEqual(self.identity(self.base), identity)

    def test_new_policy_does_not_silently_migrate_or_renew_legacy_grants(self):
        legacy = hashlib.sha256(self.base.name.encode() + b"\0"
                                + (self.base / "manifest.json").read_bytes()).hexdigest()
        legacy_access = HostAccess(self.projects, self.root / "state", trusted_runtime=legacy)
        request = legacy_access.request("personal-ops")
        grant = legacy_access.approve_web(
            "personal-ops", request_id=request["request_id"], duration_seconds=60,
            allow_codex=False, confirmation="personal-ops")
        before = self.access._record_path("personal-ops").read_bytes()
        with self.runtime(self.base):
            with self.assertRaises(HostRuntimeError) as caught:
                self.access.require("personal-ops", grant_id=grant["grant_id"])
            self.assertEqual(caught.exception.code, "HOST_ACCESS_RUNTIME_CHANGED")
        self.assertEqual(self.access._record_path("personal-ops").read_bytes(), before)

    def test_unverified_manifest_entries_are_not_trusted_without_matching_private_files(self):
        manifest_path = self.base / "manifest.json"
        original = manifest_path.read_bytes()
        manifest = json.loads(original)
        cases = [{**manifest, "../outside.py": "a" * 64},
                 {**manifest, "work-dashboard/../server.py": manifest["server.py"]},
                 {**manifest, "work-dashboard/future.js": "a" * 64},
                 {**manifest, "future.py": {"not": "a digest"}}]
        for candidate in cases:
            with self.subTest(entries=set(candidate) - set(manifest)):
                manifest_path.write_text(json.dumps(candidate))
                try:
                    self.assertIsNone(self.identity(self.base))
                finally:
                    manifest_path.write_bytes(original)

    def test_symlink_loop_in_new_manifest_entry_is_untrusted(self):
        path = self.base / "loop.py"
        path.symlink_to(path.name)
        manifest_path = self.base / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest[path.name] = "a" * 64
        manifest_path.write_text(json.dumps(manifest))
        self.assertIsNone(self.identity(self.base))


if __name__ == "__main__":
    unittest.main()
