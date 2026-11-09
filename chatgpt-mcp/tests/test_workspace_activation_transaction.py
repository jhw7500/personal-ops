"""First-activation transaction tests with real authority and native stores."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(MODULE), str(TESTS)]

import host_access  # noqa: E402
from test_workspace_activation_host import HostFixture  # noqa: E402
from test_workspace_activation_legacy import LegacyFixture, NOW  # noqa: E402
from workspace_activation import (  # noqa: E402
    ActivationCoordinator,
    ActivationError,
    ActivationJournal,
    _JournalSession,
)
from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_activation_host import ActivationHostError  # noqa: E402
from workspace_activation_legacy import LegacyInvalidationError  # noqa: E402
from workspace_control import (  # noqa: E402
    MANDATORY_RELEASE_FILES,
    WorkspaceControlError,
    activation_admission,
    activation_paths,
)


TRANSACTION = "d" * 32


def _private_file(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)
    return path


class FullReleaseHostFixture(HostFixture):
    """Host fixture whose releases can run the semantic admission subprocess."""

    def release(self, marker: bytes) -> Path:
        payloads = {
            path.name: path.read_bytes()
            for path in MODULE.iterdir()
            if path.is_file() and (
                path.suffix == ".py"
                or path.name == "workspace-launcher.py"
                or path.name in MANDATORY_RELEASE_FILES
            )
        }
        self.assertTrueRelease(payloads)
        payloads["fixture-release-marker"] = marker
        manifest = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        }
        snapshot = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()[:20]
        release = self.releases / snapshot
        release.mkdir(mode=0o700)
        for name, payload in payloads.items():
            path = release / name
            path.write_bytes(payload)
            path.chmod(0o600)
        _private_file(release / "manifest.json", manifest)
        return release

    @staticmethod
    def assertTrueRelease(payloads: dict[str, bytes]) -> None:
        missing = MANDATORY_RELEASE_FILES - payloads.keys()
        if missing:
            raise AssertionError(f"fixture release misses {sorted(missing)}")
        for required in (
            "workspace_activation.py",
            "workspace_activation_host.py",
            "workspace_authority.py",
            "workspace_control.py",
            "workspace_paths.py",
        ):
            if required not in payloads:
                raise AssertionError(f"fixture release misses {required}")

    def __init__(self, root: Path):
        super().__init__(root)
        self.units_path = root / "fixture-systemd-units.json"
        self.tunnel_status_path = root / "fixture-tunnel-status.json"
        _private_file(self.units_path, self.units)
        _private_file(self.tunnel_status_path, {"pid": self.tunnel_pid})

    def _load_units(self) -> None:
        self.units = json.loads(self.units_path.read_text())
        self.tunnel_pid = json.loads(self.tunnel_status_path.read_text())["pid"]

    def command(self, argv, **kwargs):
        self._load_units()
        result = super().command(argv, **kwargs)
        _private_file(self.units_path, self.units)
        _private_file(self.tunnel_status_path, {"pid": self.tunnel_pid})
        return result

    def unit_state(self, unit: str):
        self._load_units()
        return super().unit_state(unit)

    def tunnel_status(self):
        self.tunnel_pid = json.loads(self.tunnel_status_path.read_text())["pid"]
        return super().tunnel_status()


class ActivationTransactionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="activation-transaction-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now_patch = mock.patch.object(host_access, "_utcnow", return_value=NOW)
        self.boot_patch = mock.patch.object(
            host_access,
            "_current_boot_id",
            return_value="12345678-1234-1234-1234-123456789abc",
        )
        self.now_patch.start()
        self.boot_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.boot_patch.stop)

        self.host_fixture = FullReleaseHostFixture(self.root)
        self.legacy_fixture = LegacyFixture(self.root)
        self.host_fixture.authority.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # The production readers reject group-writable owner ancestors.  The
        # host fixture deliberately follows the ambient umask, so normalize
        # this composite fixture before exercising the transaction boundary.
        for directory in (path for path in self.root.rglob("*") if path.is_dir()):
            directory.chmod(0o700)
        self.projects = self.legacy_fixture.projects
        self.owner = self.root / "owner"
        self.owner.mkdir(mode=0o700)
        self.journal = ActivationJournal(self.owner / "activation", self.projects)
        self.authority = WorkspaceAuthority(self.projects, self.host_fixture.authority)
        self.policy_candidate = _private_file(
            self.owner / "workspace-policy.candidate.json",
            {
                "version": 1,
                "base_root": str(self.root),
                "artifacts": [{
                    "project": "tossApp",
                    "path": "build/summary.json",
                    "kind": "file",
                    "operations": ["read"],
                    "fields": ["status"],
                    "recursive": False,
                    "max_file_bytes": 4096,
                }],
                "protected_paths": [str(self.owner)],
            },
        )
        self.policy = self.owner / "workspace-policy.json"
        self.projects_path = _private_file(
            self.owner / "projects.json",
            {"projects": {name: str(path) for name, path in self.projects.items()}},
        )
        self.rollout_lock_path = self.host_fixture.rollout_lock
        self.coordinator = ActivationCoordinator(
            self.projects,
            journal=self.journal,
            authority=self.authority,
            host=self.host_fixture.adapter(),
            legacy=self.legacy_fixture.invalid,
            rollout_lock=self.rollout_lock,
            controller_dir=self.host_fixture.controller,
            source_dir=self.host_fixture.target,
        )

    @contextmanager
    def rollout_lock(self):
        descriptor = os.open(
            self.rollout_lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def prepare(self, **updates):
        arguments = {
            "policy_candidate": self.policy_candidate,
            "policy_path": self.policy,
            "projects_path": self.projects_path,
            "target_release": self.host_fixture.target,
            "rollback_release": self.host_fixture.rollback,
            "target_abi": 3,
            "rollback_abi": 3,
            "minimum_abi": 3,
        }
        arguments.update(updates)
        return self.coordinator.prepare(TRANSACTION, **arguments)

    def assert_code(self, code, operation):
        with self.assertRaises(ActivationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def rewrite_plan(self, mutate):
        journal_path = self.journal.directory / (TRANSACTION + ".json")
        seal_path = self.journal.directory / (TRANSACTION + ".plan.json")
        state = json.loads(journal_path.read_text())
        mutate(state["plan"])
        _private_file(journal_path, state)
        seal = {
            "version": 1,
            "transaction_id": TRANSACTION,
            "source_digest": state["source_digest"],
            "plan_sha256": hashlib.sha256(json.dumps(
                state["plan"], sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode()).hexdigest(),
        }
        _private_file(seal_path, seal)

    def unversioned_release(self, release):
        payloads = {
            path.name: path.read_bytes()
            for path in release.iterdir()
            if path.name != "manifest.json"
        }
        payloads["workspace_control.py"] = b"LEGACY_CONTROL = True\n"
        manifest = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        }
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        result = release.parent / digest[:20]
        result.mkdir(mode=0o700)
        for name, payload in payloads.items():
            path = result / name
            path.write_bytes(payload)
            path.chmod(0o600)
        _private_file(result / "manifest.json", manifest)
        return result

    def test_prepare_and_apply_bind_real_authority_and_invalidate_native_stores(self):
        prepared = self.prepare()
        self.assertEqual(prepared["phase"], "prepared")

        result = self.coordinator.apply(TRANSACTION)

        state = self.journal.read(TRANSACTION)
        self.assertTrue(result["verified"])
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(
            self.authority.binding(state["prepared"]["binding"]["policy_digest"]),
            state["prepared"]["binding"],
        )
        self.assertEqual(self.policy.read_bytes(), self.policy_candidate.read_bytes())
        self.assertTrue(
            self.legacy_fixture.invalid.verify_invalidated(state["evidence"]["legacy"])["verified"]
        )
        self.assertEqual(
            result["snapshot"],
            self.host_fixture.target.name,
        )
        call_count = len(self.host_fixture.calls)
        resumed = self.coordinator.apply(TRANSACTION)
        self.assertEqual(resumed["snapshot"], result["snapshot"])
        self.assertEqual(len(self.host_fixture.calls), call_count)

    def crash_after_commit(self, crash_phase="authority_committed"):
        self.prepare()
        child = os.fork()
        if child == 0:
            original = _JournalSession.save

            def crash_after_commit(session, phase, **changes):
                if phase == crash_phase:
                    os._exit(73)
                return original(session, phase, **changes)

            try:
                with mock.patch.object(_JournalSession, "save", crash_after_commit):
                    self.coordinator.apply(TRANSACTION)
            except BaseException:
                os._exit(74)
            os._exit(75)

        waited, status = os.waitpid(child, 0)
        self.assertEqual(waited, child)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 73)
        interrupted = self.journal.read(TRANSACTION)
        self.assertEqual(interrupted["phase"],
                         "authority_commit_intent" if crash_phase == "authority_committed"
                         else "start_intent")
        self.assertIsNotNone(interrupted["prepared"])
        self.assertTrue(self.authority.state_dir.exists())
        return interrupted

    def test_fresh_process_resumes_commit_before_journal_ack(self):
        self.crash_after_commit()

        result = self.coordinator.apply(TRANSACTION)

        self.assertTrue(result["verified"])
        self.assertEqual(self.journal.read(TRANSACTION)["phase"], "complete")

    def test_committed_resume_rebases_drained_plan_after_real_reboot(self):
        state = self.crash_after_commit()
        authority_before = (self.authority.state_dir / "authority.json").read_bytes()
        self.host_fixture.boot_id = "87654321-4321-4321-4321-cba987654321"

        result = self.coordinator.apply(TRANSACTION)

        completed = self.journal.read(TRANSACTION)
        self.assertTrue(result["verified"])
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(
            completed["effects"]["host_plan"]["boot_id"],
            self.host_fixture.boot_id,
        )
        self.assertEqual(
            completed["prepared"]["binding"], state["prepared"]["binding"])
        self.assertEqual(
            (self.authority.state_dir / "authority.json").read_bytes(),
            authority_before,
        )

    def test_committed_resume_rejects_a_late_legacy_approval(self):
        self.crash_after_commit()
        path = self.legacy_fixture.commands.plans_dir / (
            self.legacy_fixture.command_id + ".json")
        record = json.loads(path.read_text())
        record.update(state="approved", phase="READY", code=None)
        _private_file(path, record)
        call_count = len(self.host_fixture.calls)

        with self.assertRaises(LegacyInvalidationError):
            self.coordinator.apply(TRANSACTION)

        self.assertTrue(activation_paths(self.authority.state_dir)[1].exists())
        self.assertFalse(any("start" in call for call in self.host_fixture.calls[call_count:]))
        self.assertEqual(json.loads(path.read_text())["state"], "approved")

    def test_committed_resume_rejects_a_worker_that_appeared_after_commit(self):
        self.crash_after_commit()
        self.host_fixture.workers = [{"kind": "command", "id": "late-command"}]
        call_count = len(self.host_fixture.calls)

        with self.assertRaises(ActivationHostError):
            self.coordinator.apply(TRANSACTION)

        self.assertTrue(activation_paths(self.authority.state_dir)[1].exists())
        self.assertFalse(any("start" in call for call in self.host_fixture.calls[call_count:]))

    def test_resume_after_start_drains_target_and_preserves_a_newer_workspace_grant(self):
        state = self.crash_after_commit("complete")
        target = self.legacy_fixture.project / "existing.txt"
        target.write_text("existing fixture")
        request = self.authority.request(
            "tossApp", str(target), ["read"], 3600, "fixture read",
            state["prepared"]["binding"]["policy_digest"])
        grant = self.authority.confirm(
            "tossApp", request["request_id"], request["_confirmation"],
            state["prepared"]["binding"]["policy_digest"])
        authority_before = (self.authority.state_dir / "authority.json").read_bytes()
        cursor_before = (self.authority.state_dir / "cursor.key").read_bytes()

        result = self.coordinator.apply(TRANSACTION)

        self.assertTrue(result["verified"])
        self.assertEqual(self.journal.read(TRANSACTION)["phase"], "complete")
        self.assertTrue(any("stop" in call for call in self.host_fixture.calls))
        self.assertEqual((self.authority.state_dir / "authority.json").read_bytes(), authority_before)
        self.assertEqual((self.authority.state_dir / "cursor.key").read_bytes(), cursor_before)
        self.authority.require("tossApp", str(target), "read", grant["grant_id"],
                               state["prepared"]["binding"]["policy_digest"])

    def test_prepare_rejects_read_artifact_with_empty_fields(self):
        empty = json.loads(self.policy_candidate.read_text())
        empty["artifacts"][0]["fields"] = []
        _private_file(self.policy_candidate, empty)
        self.assert_code("ACTIVATION_ARTIFACT_FIELDS_REQUIRED", self.prepare)

    def test_prepare_accepts_version_two_text_policy_only_at_abi_three_floor(self):
        policy = json.loads(self.policy_candidate.read_text())
        policy["version"] = 2
        policy["artifacts"][0].update(content_mode="text", fields=[])
        _private_file(self.policy_candidate, policy)

        prepared = self.prepare()

        self.assertEqual(prepared["phase"], "prepared")
        self.assertEqual(prepared["plan"]["minimum_abi"], 3)
        self.assertEqual(set(prepared["plan"]["allowed_releases"].values()), {3})

    def test_prepare_rejects_version_two_with_all_abi_values_at_two_before_journal(self):
        policy = json.loads(self.policy_candidate.read_text())
        policy["version"] = 2
        policy["artifacts"][0].update(content_mode="text", fields=[])
        _private_file(self.policy_candidate, policy)

        self.assert_code(
            "ACTIVATION_ABI_INCOMPATIBLE",
            lambda: self.prepare(target_abi=2, rollback_abi=2, minimum_abi=2),
        )

        self.assertFalse(self.journal.directory.exists())

    def test_prepare_rejects_changed_project_registration(self):
        _private_file(self.projects_path, {"projects": {"tossApp": str(self.root / "changed")}})
        self.assert_code("ACTIVATION_PROJECTS_CHANGED", self.prepare)

    def test_prepare_rejects_rollback_below_minimum_abi(self):
        self.assert_code(
            "ACTIVATION_ABI_INCOMPATIBLE",
            lambda: self.prepare(rollback_abi=2, minimum_abi=3),
        )

    def test_prepare_rejects_legacy_abi_floor_before_creating_journal(self):
        rollback = self.unversioned_release(self.host_fixture.rollback)
        before_calls = list(self.host_fixture.calls)

        self.assert_code(
            "ACTIVATION_ABI_INCOMPATIBLE",
            lambda: self.prepare(
                rollback_release=rollback, rollback_abi=1, minimum_abi=1),
        )

        self.assertEqual(self.host_fixture.calls, before_calls)
        self.assertFalse(self.journal.directory.exists())

    def test_prepare_rejects_abi_value_conflicting_with_release_declaration(self):
        self.assert_code(
            "ACTIVATION_ABI_INCOMPATIBLE",
            lambda: self.prepare(target_abi=2),
        )

    def test_prepare_accepts_same_release_with_one_matching_abi(self):
        prepared = self.prepare(
            rollback_release=self.host_fixture.target,
            rollback_abi=3,
        )

        self.assertEqual(
            prepared["plan"]["allowed_releases"],
            {prepared["plan"]["target_digest"]: 3},
        )

    def test_resealed_plan_with_third_digest_fails_before_host_mutation(self):
        self.prepare()
        before_calls = list(self.host_fixture.calls)
        self.rewrite_plan(
            lambda plan: plan["allowed_releases"].update({"f" * 64: 3}))

        self.assert_code(
            "ACTIVATION_PLAN_INVALID",
            lambda: self.coordinator.apply(TRANSACTION),
        )
        self.assertEqual(self.host_fixture.calls, before_calls)

    def test_resealed_plan_with_swapped_target_fails_before_host_mutation(self):
        self.prepare()
        before_calls = list(self.host_fixture.calls)

        def swap(plan):
            plan["target_release"], plan["rollback_release"] = (
                plan["rollback_release"], plan["target_release"])
            plan["target_digest"], plan["rollback_digest"] = (
                plan["rollback_digest"], plan["target_digest"])

        self.rewrite_plan(swap)
        self.assert_code(
            "ACTIVATION_PLAN_INVALID",
            lambda: self.coordinator.apply(TRANSACTION),
        )
        self.assertEqual(self.host_fixture.calls, before_calls)

    def test_changed_candidate_after_prepare_fails_before_host_mutation(self):
        self.prepare()
        before_calls = list(self.host_fixture.calls)
        candidate = json.loads(self.policy_candidate.read_text())
        candidate["artifacts"][0]["fields"].append("usage")
        _private_file(self.policy_candidate, candidate)

        self.assert_code(
            "ACTIVATION_INPUT_CHANGED",
            lambda: self.coordinator.apply(TRANSACTION),
        )
        self.assertEqual(self.host_fixture.calls, before_calls)
        self.assertFalse(self.authority.state_dir.exists())

    def test_postcommit_start_failure_stops_exact_target_and_keeps_maintenance_closed(self):
        self.prepare()
        self.host_fixture.fail_admin_start_once = True

        with self.assertRaises(ActivationHostError) as caught:
            self.coordinator.apply(TRANSACTION)

        state = self.journal.read(TRANSACTION)
        self.assertEqual(caught.exception.code, "SYSTEMD_COMMAND_FAILED")
        self.assertEqual(state["phase"], "interrupted")
        self.assertTrue(self.authority.state_dir.exists())
        self.assertTrue(activation_paths(self.authority.state_dir)[1].exists())
        self.assertIn(
            self.host_fixture.units["personal-ops-tunnel.service"]["active_state"],
            {"inactive", "failed"},
        )
        self.assertIn(
            self.host_fixture.units["personal-ops-admin.service"]["active_state"],
            {"inactive", "failed"},
        )

    def test_real_fork_admission_stays_denied_between_tunnel_and_failed_admin_start(self):
        self.prepare()
        self.host_fixture.fail_admin_start_once = True
        evidence = self.owner / "fork-admission.json"
        original = self.coordinator.host.command_fn

        def command(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[2:] == ["start", "personal-ops-tunnel.service"]:
                child = os.fork()
                if child == 0:
                    code = "allowed"
                    try:
                        with activation_admission(
                                self.authority.state_dir, create=True):
                            pass
                    except WorkspaceControlError as exc:
                        code = exc.code
                    descriptor = os.open(
                        evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.fchmod(descriptor, 0o600)
                    os.write(descriptor, json.dumps({"code": code}).encode())
                    os.fsync(descriptor)
                    os.close(descriptor)
                    os._exit(0)
                waited, status = os.waitpid(child, 0)
                self.assertEqual(waited, child)
                self.assertEqual(os.WEXITSTATUS(status), 0)
            return result

        self.coordinator.host.command_fn = command
        with self.assertRaises(ActivationHostError):
            self.coordinator.apply(TRANSACTION)

        self.assertEqual(
            json.loads(evidence.read_text())["code"],
            "WORKSPACE_ACTIVATION_IN_PROGRESS",
        )
        self.assertTrue(activation_paths(self.authority.state_dir)[1].exists())
        self.assertEqual(self.host_fixture.workers, [])
        self.assertIn(
            self.host_fixture.units["personal-ops-tunnel.service"]["active_state"],
            {"inactive", "failed"},
        )

    def test_start_failure_barrier_is_resumed_only_by_same_transaction(self):
        from workspace_control import startup_barrier_path

        self.prepare()
        self.host_fixture.fail_admin_start_once = True
        with self.assertRaises(ActivationHostError):
            self.coordinator.apply(TRANSACTION)
        self.assertTrue(startup_barrier_path(self.authority.state_dir).is_file())

        result = self.coordinator.apply(TRANSACTION)

        self.assertTrue(result["verified"])
        self.assertEqual(self.journal.read(TRANSACTION)["phase"], "complete")
        self.assertFalse(startup_barrier_path(self.authority.state_dir).exists())
        self.assertFalse(activation_paths(self.authority.state_dir)[1].exists())


if __name__ == "__main__":
    unittest.main()
