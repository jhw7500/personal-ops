"""Stopped-only owner upgrade from Workspace ABI 2 to installation HostAccess v2."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_access import HostAccess  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402
from workspace_host import host_operation_lane  # noqa: E402
from workspace_control import MANDATORY_RELEASE_FILES, activation_paths  # noqa: E402
from workspace_upgrade import (  # noqa: E402
    ExistingUpgradeHost, UpgradeCoordinator, UpgradeError,
    _verify_installed_self,
)
from tests.test_workspace_activation_host import HostFixture  # noqa: E402


TARGET = "2" * 64
TX = "4" * 32


def _installed_release(root, *, dependency):
    releases = (root / ".local/lib/chatgpt-coding-mcp/releases")
    releases.mkdir(parents=True, mode=0o700)
    contents = {}
    for name in MANDATORY_RELEASE_FILES | {"workspace_upgrade.py"}:
        if name == "workspace_upgrade.py":
            payload = Path(__file__).resolve().parents[1].joinpath(name).read_bytes()
        elif name == "workspace_control.py":
            payload = b"ENFORCEMENT_ABI = 3\n"
        elif name == "server.py":
            payload = dependency
        else:
            payload = (name + "\n").encode()
        contents[name] = payload
    manifest = {name: hashlib.sha256(payload).hexdigest()
                for name, payload in contents.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    release = releases / digest[:20]
    release.mkdir(mode=0o700)
    for name, payload in contents.items():
        path = release / name
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o600)
    manifest_path = release / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_path.chmod(0o600)
    return release, digest


class FakeHost:
    def __init__(self):
        self.workers = []
        self.leases = []
        self.services_stopped = True
        self.residual_process = False
        self.observation = {"boot_id": "boot", "processes": [], "configuration": "5" * 64}

    def prepare(self, _target):
        return dict(self.observation)

    def verify_drained(self, expected):
        if expected != self.observation:
            raise UpgradeError("UPGRADE_HOST_CHANGED")
        if self.workers:
            raise UpgradeError("ACTIVE_WORKERS")
        if self.leases:
            raise UpgradeError("ACTIVE_LEASES")
        if not self.services_stopped:
            raise UpgradeError("SERVICES_NOT_STOPPED")
        if self.residual_process:
            raise UpgradeError("RESIDUAL_PROCESS")
        return {"drained": True}

    def verify_configuration(self, expected):
        if expected != self.observation:
            raise UpgradeError("UPGRADE_HOST_CHANGED")
        return {"configuration_verified": True}


class WorkspaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        self.projects = {"project": self.project}
        self.authority_dir = self.root / "state" / "workspace-authority"
        self.authority_dir.parent.mkdir(mode=0o700)
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(json.dumps({
            "version": 1, "base_root": str(self.root), "artifacts": [],
            "protected_paths": [],
        }))
        self.policy_path.chmod(0o600)
        self.workspace = WorkspaceTools(
            self.projects, state_dir=self.authority_dir,
            config_path=self.policy_path,
        )
        self.authority = self.workspace.authority
        self.policy_digest = self.workspace.policy.load(required=True).digest
        self.original = self.authority.initialize(
            self.policy_digest, {TARGET: 3}, minimum_abi=2)
        self.state_root = self.root / "host-state"
        self.state_root.mkdir(mode=0o700)
        self.access = HostAccess(
            self.projects, self.state_root, trusted_runtime=TARGET,
            workspace=self.workspace,
        )
        # HostAccess construction only needs the workspace for public paths; the
        # owner upgrade uses the explicitly supplied authority and maintenance.
        self.host = FakeHost()
        self.journal_dir = self.root / "upgrade-journal"
        self.coordinator = UpgradeCoordinator(
            self.projects, authority=self.authority, host_access=self.access,
            host=self.host, journal_dir=self.journal_dir,
            rollout_lock=lambda: _NullContext(), workspace=self.workspace,
            source_identity={
                "release_digest": TARGET,
                "release_abi": 3,
                "runtime": str(self.root / "release"),
            },
        )

    def tearDown(self):
        # Keep TemporaryDirectory cleanup deterministic even when a crash fixture
        # deliberately leaves a maintenance intent behind.
        for path in self.authority_dir.parent.glob(".workspace-authority.activation-*"):
            path.unlink(missing_ok=True)

    def prepare(self):
        return self.coordinator.prepare(
            TX, target={"release_digest": TARGET, "release_abi": 3,
                        "runtime": str(self.root / "release")},
            binding=self.original,
        )

    def state(self):
        return json.loads((self.authority_dir / "authority.json").read_text())

    def minimum_abi(self):
        path = self.authority.minimum_abi_path
        return json.loads(path.read_text())["minimum_abi"]

    def assert_upgrade_error(self, code, operation):
        with self.assertRaises(UpgradeError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_prepare_rejects_abi2_and_unadmitted_target(self):
        self.assert_upgrade_error(
            "UPGRADE_TARGET_ABI_REQUIRED",
            lambda: self.coordinator.prepare(
                TX, target={"release_digest": TARGET, "release_abi": 2,
                            "runtime": str(self.root / "release")},
                binding=self.original,
            ),
        )
        self.assert_upgrade_error(
            "UPGRADE_TARGET_UNADMITTED",
            lambda: self.coordinator.prepare(
                TX, target={"release_digest": "9" * 64, "release_abi": 3,
                            "runtime": str(self.root / "release")},
                binding=self.original,
            ),
        )

    def test_upgrade_preserves_installation_policy_and_creates_no_grant(self):
        self.prepare()

        result = self.coordinator.apply(TX)

        state = self.state()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(state["installation_id"], self.original["installation_id"])
        self.assertEqual(state["policy_digest"], self.policy_digest)
        self.assertEqual(state["authority_epoch"], self.original["authority_epoch"] + 1)
        self.assertEqual(state["allowed_releases"], {TARGET: 3})
        self.assertEqual(self.minimum_abi(), 3)
        installation = self.access.installation.load()
        self.assertEqual(installation["requests"], {})
        self.assertIsNone(installation["grant"])
        self.assertEqual(installation["grant_state_revision"], 1)

    def test_each_durable_crash_resumes_without_an_extra_epoch(self):
        for checkpoint in ("authority", "floor", "marker", "legacy", "state"):
            with self.subTest(checkpoint=checkpoint):
                self.tearDown()
                self.setUp()
                self.prepare()
                self.coordinator.crash_after = checkpoint
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    self.coordinator.apply(TX)
                self.assert_upgrade_error(
                    "WORKSPACE_ACTIVATION_IN_PROGRESS",
                    lambda: self.coordinator.assert_admission_open(),
                )
                self.coordinator.crash_after = None
                self.assertEqual(self.coordinator.resume(TX)["status"], "complete")
                self.assertEqual(
                    self.state()["authority_epoch"], self.original["authority_epoch"] + 1)

    def test_drain_is_rechecked_on_resume(self):
        self.prepare()
        self.coordinator.crash_after = "authority"
        with self.assertRaises(RuntimeError):
            self.coordinator.apply(TX)
        self.host.workers = [{"kind": "host", "id": "late"}]
        self.coordinator.crash_after = None

        self.assert_upgrade_error("ACTIVE_WORKERS", lambda: self.coordinator.resume(TX))

    def test_publication_lane_blocks_upgrade_state_mutation(self):
        self.prepare()

        with host_operation_lane(self.workspace):
            self.assert_upgrade_error(
                "WORKSPACE_HOST_LANE_BUSY", lambda: self.coordinator.apply(TX))

    def test_marker_only_repair_requires_the_exact_prepared_absence(self):
        self.access.installation.mark_enabled()

        self.assert_upgrade_error("HOST_ACCESS_PREEXISTING_STATE", self.prepare)

    def test_changed_host_or_binding_rejects_stale_preparation(self):
        self.prepare()
        self.host.observation["configuration"] = "6" * 64
        self.assert_upgrade_error("UPGRADE_HOST_CHANGED", lambda: self.coordinator.apply(TX))

        self.host.observation["configuration"] = "5" * 64
        self.authority.advance(self.policy_digest, {TARGET: 3})
        self.authority.advance(self.policy_digest, {TARGET: 3})
        self.assert_upgrade_error("UPGRADE_BINDING_CHANGED", lambda: self.coordinator.apply(TX))

    def test_apply_rejects_legacy_single_file_source_pin(self):
        self.prepare()
        path = self.journal_dir / (TX + ".json")
        legacy = json.loads(path.read_text())
        legacy["plan"].pop("source", None)
        path.write_text(json.dumps(legacy, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        seal = self.coordinator.journal._plan_seal(
            TX, legacy["source_digest"], legacy["plan"])
        seal_path = self.journal_dir / (TX + ".plan.json")
        seal_path.write_text(json.dumps(seal, sort_keys=True, separators=(",", ":")))
        seal_path.chmod(0o600)

        self.assert_upgrade_error(
            "UPGRADE_SOURCE_CHANGED", lambda: self.coordinator.apply(TX))

    def test_apply_rejects_execution_from_a_different_release(self):
        first, _ = _installed_release(
            self.root / "first", dependency=b"dependency-a\n")
        second, _ = _installed_release(
            self.root / "second", dependency=b"dependency-b\n")
        first_identity = _verify_installed_self(first)
        second_identity = _verify_installed_self(second)
        self.authority.advance(
            self.policy_digest, {first_identity["release_digest"]: 3})
        first_coordinator = UpgradeCoordinator(
            self.projects, authority=self.authority, host_access=self.access,
            host=self.host, journal_dir=self.journal_dir,
            rollout_lock=lambda: _NullContext(), workspace=self.workspace,
            source_identity=first_identity,
        )
        first_coordinator.prepare(TX, target=first_identity)
        other = UpgradeCoordinator(
            self.projects, authority=self.authority, host_access=self.access,
            host=self.host, journal_dir=self.journal_dir,
            rollout_lock=lambda: _NullContext(), workspace=self.workspace,
            source_identity=second_identity,
        )

        self.assert_upgrade_error("UPGRADE_SOURCE_CHANGED", lambda: other.apply(TX))

    def test_same_binding_with_changed_authority_state_rejects_stale_preparation(self):
        self.prepare()
        with self.authority._locked_state() as (root_fd, state):
            state["updated_at"] += 1
            self.authority._save(root_fd, state)

        self.assert_upgrade_error(
            "UPGRADE_AUTHORITY_CHANGED", lambda: self.coordinator.apply(TX))

    def test_completed_resume_is_read_only(self):
        self.prepare()
        first = self.coordinator.apply(TX)
        state_bytes = (self.authority_dir / "authority.json").read_bytes()
        marker_bytes = self.access.installation.marker_path.read_bytes()
        v2_bytes = self.access.installation.state_path.read_bytes()

        second = self.coordinator.resume(TX)

        self.assertEqual(second, first)
        self.assertEqual((self.authority_dir / "authority.json").read_bytes(), state_bytes)
        self.assertEqual(self.access.installation.marker_path.read_bytes(), marker_bytes)
        self.assertEqual(self.access.installation.state_path.read_bytes(), v2_bytes)

    def test_completed_resume_recomputes_result_instead_of_trusting_evidence(self):
        self.prepare()
        expected = self.coordinator.apply(TX)
        path = self.journal_dir / (TX + ".json")
        changed = json.loads(path.read_text())
        changed["evidence"] = {"result": {"status": "complete", "forged": True}}
        path.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)

        self.assertEqual(self.coordinator.resume(TX), expected)

    def test_completed_resume_rejects_later_host_access_request(self):
        self.prepare()
        self.coordinator.apply(TX)
        state = self.access.installation.load()
        request_id = "8" * 32
        state["requests"] = {request_id: {
            "request_id": request_id,
            "project": "project",
            "created_at": "2030-01-01T00:00:00+00:00",
            "expires_at": "2030-01-01T00:05:00+00:00",
            "lifetime": "timed",
            "grant_state_revision": 1,
            "installation_id": state["installation_id"],
            "uid": state["uid"],
            "capability_generation": 1,
        }}
        state["updated_at"] = "2030-01-01T00:00:00+00:00"
        self.access.installation.save(state)
        changed = self.access.installation.state_path.read_bytes()

        self.assert_upgrade_error(
            "UPGRADE_COMPLETION_INVALID", lambda: self.coordinator.resume(TX))

        self.assertEqual(self.access.installation.state_path.read_bytes(), changed)

    def test_forged_complete_phase_does_not_bypass_actual_state_validation(self):
        self.prepare()
        path = self.journal_dir / (TX + ".json")
        forged = json.loads(path.read_text())
        forged["phase"] = "complete"
        forged["evidence"] = {"result": {"status": "complete", "forged": True}}
        path.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        authority_before = (self.authority_dir / "authority.json").read_bytes()

        self.assert_upgrade_error(
            "UPGRADE_COMPLETION_INVALID", lambda: self.coordinator.resume(TX))

        self.assertEqual((self.authority_dir / "authority.json").read_bytes(),
                         authority_before)
        self.assertFalse(self.access.installation.marker_path.exists())

    def test_completed_resume_rejects_later_authority_change_without_overwrite(self):
        self.prepare()
        self.coordinator.apply(TX)
        self.authority.advance(self.policy_digest, {TARGET: 3})
        changed = (self.authority_dir / "authority.json").read_bytes()

        self.assert_upgrade_error(
            "UPGRADE_COMPLETION_INVALID", lambda: self.coordinator.resume(TX))

        self.assertEqual((self.authority_dir / "authority.json").read_bytes(), changed)

    def test_external_floor_raise_after_prepare_is_not_adopted(self):
        self.prepare()
        floor = json.loads(self.authority.minimum_abi_path.read_text())
        floor["minimum_abi"] = 3
        self.authority.minimum_abi_path.write_text(json.dumps(
            floor, sort_keys=True, separators=(",", ":")))
        self.authority.minimum_abi_path.chmod(0o600)
        authority_before = (self.authority_dir / "authority.json").read_bytes()

        self.assert_upgrade_error(
            "UPGRADE_ABI_FLOOR_CHANGED", lambda: self.coordinator.apply(TX))

        self.assertEqual((self.authority_dir / "authority.json").read_bytes(),
                         authority_before)

    def test_forged_marker_phase_cannot_create_recovery_maintenance(self):
        self.prepare()
        path = self.journal_dir / (TX + ".json")
        forged = json.loads(path.read_text())
        forged["phase"] = "marker_intent"
        path.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        self.access.installation.mark_enabled()

        self.assert_upgrade_error(
            "UPGRADE_MAINTENANCE_REQUIRED", lambda: self.coordinator.apply(TX))

        self.assertFalse(self.access.installation.state_path.exists())
        self.assertFalse(activation_paths(self.authority_dir)[1].exists())

    def test_forged_state_phase_cannot_create_recovery_maintenance(self):
        prepared = self.prepare()
        path = self.journal_dir / (TX + ".json")
        forged = json.loads(path.read_text())
        forged["phase"] = "state_intent"
        path.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        self.access.installation.mark_enabled()
        self.access.installation.create(prepared["plan"]["v2_state"])

        self.assert_upgrade_error(
            "UPGRADE_MAINTENANCE_REQUIRED", lambda: self.coordinator.apply(TX))

        self.assertEqual(self.state()["authority_epoch"],
                         self.original["authority_epoch"])
        self.assertFalse(activation_paths(self.authority_dir)[1].exists())

    def test_crash_after_maintenance_finish_reuses_sealed_result(self):
        self.prepare()
        self.coordinator.crash_after = "finish"
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.coordinator.apply(TX)
        sealed = self.coordinator.journal.read(TX)
        self.assertEqual(sealed["phase"], "state_saved")
        epoch = self.state()["authority_epoch"]

        self.coordinator.crash_after = None
        result = self.coordinator.resume(TX)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.state()["authority_epoch"], epoch)
        completed = self.coordinator.journal.read(TX)
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(completed["evidence"], {"result": result})

    def test_crash_after_verified_journal_write_resumes_to_complete(self):
        self.prepare()
        self.coordinator.crash_after = "verified"
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.coordinator.apply(TX)
        self.assertEqual(self.coordinator.journal.read(TX)["phase"], "verified")

        self.coordinator.crash_after = None
        result = self.coordinator.resume(TX)

        completed = self.coordinator.journal.read(TX)
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(completed["evidence"], {"result": result})


class InstalledReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_full_release_digest_distinguishes_equal_entrypoints(self):
        first, first_digest = _installed_release(
            self.root / "first", dependency=b"dependency-a\n")
        second, second_digest = _installed_release(
            self.root / "second", dependency=b"dependency-b\n")

        first_identity = _verify_installed_self(first)
        second_identity = _verify_installed_self(second)

        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(first_identity["release_digest"], first_digest)
        self.assertEqual(second_identity["release_digest"], second_digest)
        self.assertEqual(first_identity["runtime"], str(first))
        self.assertEqual(second_identity["runtime"], str(second))

    def test_installed_identity_rejects_dependency_tampering(self):
        release, _ = _installed_release(
            self.root, dependency=b"original dependency\n")
        _verify_installed_self(release)
        (release / "server.py").write_bytes(b"tampered dependency\n")

        with self.assertRaises(UpgradeError) as caught:
            _verify_installed_self(release)

        self.assertEqual(caught.exception.code, "UPGRADE_SOURCE_UNADMITTED")


class ExistingUpgradeHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = HostFixture(Path(self.temporary.name))
        self._wire_managed_target()
        self.adapter = self.fixture.adapter()
        self.host = ExistingUpgradeHost(
            self.adapter, controller_dir=self.fixture.controller,
            authority_dir=self.fixture.authority,
            pinned_files=(self.fixture.profile,), node_prefix=self.node_prefix,
        )

    def _wire_managed_target(self):
        self.node_prefix = (
            self.fixture.home / ".nvm/versions/node/v24.12.0")
        self.node = self.node_prefix / "bin/node"
        self.wrapper = self.node_prefix / "bin/codex"
        self.script = (self.node_prefix
                       / "lib/node_modules/@openai/codex/bin/codex.js")
        self.native = (self.node_prefix
                       / "lib/node_modules/@openai/codex/node_modules/@openai/"
                       "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex")
        for path in (self.node, self.script, self.native):
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_text(path.name)
            path.chmod(0o700 if path != self.script else 0o744)
        self.wrapper.symlink_to(
            Path("../lib/node_modules/@openai/codex/bin/codex.js"))
        launcher = self.fixture.controller / "workspace-launcher.py"
        common = [str(self.fixture.interpreter), str(launcher),
                  "--authority-dir", str(self.fixture.authority),
                  "--release", str(self.fixture.target),
                  "--interpreter", str(self.fixture.interpreter)]
        tunnel = [*common, "--entrypoint", "server.py"]
        admin = [*common, "--entrypoint", "admin_server.py", "--",
                 "--port", "8765"]
        client = [
            str(self.fixture.client), "runtimes", "connect", "--alias",
            "personal-ops-coding", "--profile", "personal-ops-coding",
            "--profile-dir", str(self.fixture.profile.parent), "--tunnel-id",
            "tunnel-placeholder", "--runtime-api-key",
            f"file:{self.fixture.home / '.config/key'}",
            "--control-plane-base-url", "https://api.openai.com",
            "--mcp-command", shlex.join(tunnel), "--json",
        ]
        profile = json.loads(self.fixture.profile.read_text())
        profile["mcp"]["commands"][0]["command"] = shlex.join(tunnel)
        self.fixture.profile.write_text(json.dumps(profile))
        self.fixture.tunnel_override.parent.mkdir(parents=True, mode=0o700,
                                                  exist_ok=True)
        self.fixture.tunnel_override.write_text(
            "[Service]\nExecStart=\nExecStart=" + shlex.join(client) + "\n")
        self.fixture.tunnel_override.chmod(0o600)
        self.fixture.admin_override.parent.mkdir(parents=True, mode=0o700,
                                                 exist_ok=True)
        self.fixture.admin_override.write_text(
            "[Service]\nExecStart=\nExecStart=" + shlex.join(admin) + "\n")
        self.fixture.admin_override.chmod(0o600)
        self.fixture.remove_process(4100)
        self.fixture.remove_process(4101)
        self.fixture.remove_process(4200)
        daemon = [str(self.fixture.client), "run", "--profile-dir",
                  str(self.fixture.profile.parent), "--profile",
                  "personal-ops-coding", "--log.file"]
        self.fixture.write_process(4100, 1, 100, daemon, "user.slice/tunnel")
        self.fixture.write_process(
            4101, 4100, 101,
            [str(self.fixture.interpreter), str(self.fixture.target / "server.py")],
            "user.slice/tunnel")
        self._write_node_codex(4102, 4103, 4100)
        self.fixture.write_process(
            4200, 1, 200,
            [str(self.fixture.interpreter), str(self.fixture.target / "admin_server.py"),
             "--port", "8765"], "user.slice/admin")

    def _write_node_codex(self, wrapper_pid, native_pid, parent_pid):
        self.fixture.write_process(
            wrapper_pid, parent_pid, wrapper_pid,
            ["node", str(self.wrapper), "app-server"],
            "user.slice/tunnel")
        (self.fixture.proc / str(wrapper_pid) / "exe").symlink_to(self.node)
        self.fixture.write_process(
            native_pid, wrapper_pid, native_pid,
            [str(self.native), "app-server"], "user.slice/tunnel")
        (self.fixture.proc / str(native_pid) / "exe").symlink_to(self.native)

    def test_pins_running_target_then_requires_exact_stopped_observation(self):
        plan = self.host.prepare({"runtime": str(self.fixture.target)})
        self.assertEqual(len(plan["processes"]), 5)

        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-tunnel.service"])
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-admin.service"])

        with self.assertRaises(UpgradeError) as caught:
            self.host.verify_drained(plan)
        self.assertEqual(caught.exception.code, "RESIDUAL_PROCESS")
        self.fixture.remove_process(4102)
        with self.assertRaises(UpgradeError) as caught:
            self.host.verify_drained(plan)
        self.assertEqual(caught.exception.code, "RESIDUAL_PROCESS")
        self.fixture.remove_process(4103)
        self.assertTrue(self.host.verify_drained(plan)["drained"])

    def test_prepare_accepts_direct_codex_process_without_node_wrapper(self):
        self.fixture.remove_process(4102)
        self.fixture.remove_process(4103)
        self.fixture.write_process(4102, 4100, 102, ["codex", "app-server"],
                                   "user.slice/tunnel")

        plan = self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(len(plan["processes"]), 4)

    def test_prepare_accepts_mcp_only_tunnel(self):
        self.fixture.remove_process(4102)
        self.fixture.remove_process(4103)

        plan = self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(len(plan["processes"]), 3)

    def test_fresh_drain_rejects_restarted_direct_codex_pid(self):
        self.fixture.remove_process(4102)
        self.fixture.remove_process(4103)
        self.fixture.write_process(4102, 4100, 102, ["codex", "app-server"],
                                   "user.slice/tunnel")
        plan = self.host.prepare({"runtime": str(self.fixture.target)})
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-tunnel.service"])
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-admin.service"])
        self.fixture.remove_process(4102)
        self.fixture.write_process(5102, 1, 5102, ["codex", "app-server"],
                                   "user.slice/tunnel")

        with self.assertRaises(UpgradeError) as caught:
            self.host.verify_drained(plan)

        self.assertEqual(caught.exception.code, "RESIDUAL_PROCESS")

    def test_fresh_drain_rejects_restarted_node_codex_chain(self):
        plan = self.host.prepare({"runtime": str(self.fixture.target)})
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-tunnel.service"])
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-admin.service"])
        self.fixture.remove_process(4102)
        self.fixture.remove_process(4103)
        self._write_node_codex(5102, 5103, 1)

        with self.assertRaises(UpgradeError) as caught:
            self.host.verify_drained(plan)

        self.assertEqual(caught.exception.code, "RESIDUAL_PROCESS")

    def test_fresh_drain_rejects_partial_node_codex_identity(self):
        plan = self.host.prepare({"runtime": str(self.fixture.target)})
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-tunnel.service"])
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-admin.service"])
        self.fixture.remove_process(4102)
        self.fixture.remove_process(4103)
        self.fixture.write_process(
            5102, 1, 5102,
            ["node", str(self.wrapper), "app-server"],
            "user.slice/tunnel")
        (self.fixture.proc / "5102" / "exe").symlink_to(self.node)

        with self.assertRaises(UpgradeError) as caught:
            self.host.verify_drained(plan)

        self.assertEqual(caught.exception.code,
                         "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_group_writable_codex_wrapper_script(self):
        script = (self.node_prefix
                  / "lib/node_modules/@openai/codex/bin/codex.js")
        script.chmod(0o764)

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_already_stopped_services(self):
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-tunnel.service"])
        self.fixture.command(["systemctl", "--user", "stop",
                              "personal-ops-admin.service"])

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_RUNNING_TARGET_REQUIRED")

    def test_prepare_rejects_unexpected_extra_tunnel_child(self):
        self.fixture.write_process(4103, 4100, 103, ["unexpected", "child"],
                                   "user.slice/tunnel")

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_tunnel_daemon_with_wrong_profile(self):
        self.fixture.remove_process(4100)
        self.fixture.write_process(
            4100, 1, 100, [str(self.fixture.client), "run", "--profile-dir",
                           str(self.fixture.profile.parent), "--profile", "wrong",
                           "--log.file"],
            "user.slice/tunnel")

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_tunnel_daemon_with_wrong_profile_directory(self):
        self.fixture.remove_process(4100)
        self.fixture.write_process(
            4100, 1, 100, [str(self.fixture.client), "run", "--profile-dir",
                           str(self.fixture.home / "wrong"), "--profile",
                           "personal-ops-coding", "--log.file"],
            "user.slice/tunnel")

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_tunnel_daemon_with_different_client(self):
        other = self.fixture.home / "other-client"
        other.write_text("other")
        other.chmod(0o700)
        self.fixture.remove_process(4100)
        self.fixture.write_process(
            4100, 1, 100, [str(other), "run", "--profile-dir",
                           str(self.fixture.profile.parent), "--profile",
                           "personal-ops-coding", "--log.file"],
            "user.slice/tunnel")

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_prepare_rejects_admin_process_running_the_mcp_entrypoint(self):
        self.fixture.remove_process(4200)
        self.fixture.write_process(
            4200, 1, 200,
            [str(self.fixture.interpreter), str(self.fixture.target / "server.py"),
             "--port", "8765"], "user.slice/admin")

        with self.assertRaises(UpgradeError) as caught:
            self.host.prepare({"runtime": str(self.fixture.target)})

        self.assertEqual(caught.exception.code, "UPGRADE_TARGET_PROCESS_MISMATCH")

    def test_process_scan_skips_process_directories_owned_by_other_uids(self):
        with mock.patch("workspace_upgrade.os.getuid", return_value=-1):
            self.assertEqual(self.host._managed_processes(self.fixture.boot_id), [])

    def test_process_scan_rejects_unreadable_owned_live_process(self):
        folder = self.fixture.proc / "9999"
        folder.mkdir()
        (folder / "stat").write_text("malformed")
        (folder / "cmdline").write_bytes(b"python\0")
        (folder / "cgroup").write_text("0::/user.slice/test\n")

        with self.assertRaises(UpgradeError) as caught:
            self.host._managed_processes(self.fixture.boot_id)

        self.assertEqual(caught.exception.code, "PROCESS_IDENTITY_UNAVAILABLE")

    def test_direct_child_scan_does_not_read_unrelated_process_argv(self):
        self.fixture.write_process(9999, 1, 999, ["short-lived"],
                                   "user.slice/other")
        (self.fixture.proc / "9999" / "cmdline").unlink()

        children = self.host._direct_children(4100, self.fixture.boot_id)

        self.assertEqual({child["pid"] for child in children}, {4101, 4102})

    def test_process_scan_ignores_identity_stable_terminal_process(self):
        self.fixture.write_process(9999, 1, 999, ["terminal"],
                                   "user.slice/other")
        stat_path = self.fixture.proc / "9999" / "stat"
        stat_path.write_text(stat_path.read_text().replace(") S ", ") Z "))
        (self.fixture.proc / "9999" / "cmdline").write_bytes(b"")

        pins = self.host._managed_processes(self.fixture.boot_id)

        self.assertNotIn(9999, {pin["pid"] for pin in pins})


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


if __name__ == "__main__":
    unittest.main()
