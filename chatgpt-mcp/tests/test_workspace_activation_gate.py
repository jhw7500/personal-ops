from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_paths import WorkspaceError  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from workspace_control import (  # noqa: E402
    ENFORCEMENT_ABI,
    HOST_ACCESS_CAPABILITY_GENERATION,
    MANDATORY_RELEASE_FILES,
    WorkspaceControlError,
    activation_admission,
    activation_maintenance,
    activation_paths,
    startup_barrier_path,
    minimum_abi_path,
    verify_launch,
)


POLICY = "a" * 64
SOURCE = "b" * 64
TRANSACTION = "c" * 32


class WorkspaceActivationGateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.authority_dir = self.root / "workspace-authority"
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(WorkspaceControlError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def enter_admission(self):
        with activation_admission(self.authority_dir):
            pass

    def authority(self):
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY, {})
        return authority

    def release(self, marker="release", *, enforcement_abi=ENFORCEMENT_ABI):
        manifest = {}
        contents = {}
        for name in MANDATORY_RELEASE_FILES:
            payload = ((f"ENFORCEMENT_ABI = {enforcement_abi}\n"
                        + (f"HOST_ACCESS_CAPABILITY_GENERATION = "
                           f"{HOST_ACCESS_CAPABILITY_GENERATION}\n"
                           if enforcement_abi >= 3 else "")).encode()
                       if name == "workspace_control.py"
                       else f"{marker}:{name}\n".encode())
            contents[name] = payload
            manifest[name] = hashlib.sha256(payload).hexdigest()
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        releases = self.root / "releases"
        releases.mkdir(mode=0o700, exist_ok=True)
        release = releases / digest[:20]
        release.mkdir(mode=0o700)
        for name, payload in contents.items():
            path = release / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o600)
        manifest_path = release / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        manifest_path.chmod(0o600)
        return release, digest

    def test_absent_gate_admission_is_read_only(self):
        lock_path, intent_path = activation_paths(self.authority_dir)

        with activation_admission(self.authority_dir):
            pass

        self.assertFalse(lock_path.exists())
        self.assertFalse(intent_path.exists())

    def test_mutable_admissions_create_and_share_one_permanent_gate_inode(self):
        import workspace_control

        first_writing = threading.Event()
        allow_first_write = threading.Event()
        first_inode = []
        errors = []
        original_write = os.write

        def delayed_write(descriptor, payload):
            if (threading.current_thread().name == "first-admission"
                    and not first_writing.is_set()):
                first_writing.set()
                allow_first_write.wait(2)
            return original_write(descriptor, payload)

        def enter_first():
            try:
                with activation_admission(self.authority_dir, create=True):
                    first_inode.append(
                        activation_paths(self.authority_dir)[0].stat().st_ino)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(workspace_control.os, "write", side_effect=delayed_write):
            thread = threading.Thread(target=enter_first, name="first-admission")
            thread.start()
            self.assertTrue(first_writing.wait(1))
            lock_path, _ = activation_paths(self.authority_dir)
            self.assertFalse(lock_path.exists())
            with activation_admission(self.authority_dir, create=True):
                second_inode = lock_path.stat().st_ino
                self.assert_code(
                    "WORKSPACE_ACTIVATION_BUSY",
                    lambda: activation_maintenance(
                        self.authority_dir, TRANSACTION, SOURCE).__enter__(),
                )
            allow_first_write.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(first_inode, [second_inode])
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
        self.assertEqual(lock_path.stat().st_nlink, 1)

    def test_maintenance_persists_intent_and_denies_even_legacy_launch(self):
        missing_release = self.root / ("0" * 20)
        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            lock_path, intent_path = activation_paths(self.authority_dir)
            self.assertTrue(lock_path.is_file())
            self.assertTrue(intent_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(intent_path.stat().st_mode), 0o600)
            self.assertEqual(lock_path.stat().st_nlink, 1)
            self.assertEqual(maintenance.intent["lock_inode"], lock_path.stat().st_ino)
            self.assert_code(
                "WORKSPACE_ACTIVATION_IN_PROGRESS",
                lambda: verify_launch(missing_release, self.authority_dir),
            )

        self.assertTrue(intent_path.exists())
        self.assert_code(
            "WORKSPACE_ACTIVATION_IN_PROGRESS",
            lambda: verify_launch(missing_release, self.authority_dir),
        )

    def test_process_exit_releases_lock_but_intent_still_denies_and_resumes(self):
        script = (
            "from workspace_control import activation_maintenance\n"
            f"with activation_maintenance({str(self.authority_dir)!r}, "
            f"{TRANSACTION!r}, {SOURCE!r}):\n"
            " import os; os._exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        self.assert_code(
            "WORKSPACE_ACTIVATION_IN_PROGRESS",
            self.enter_admission,
        )
        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            self.assertEqual(maintenance.intent["transaction_id"], TRANSACTION)

    def test_second_owner_fails_closed_while_maintenance_lock_is_busy(self):
        script = (
            "from workspace_control import activation_maintenance\n"
            f"with activation_maintenance({str(self.authority_dir)!r}, "
            f"{TRANSACTION!r}, {SOURCE!r}):\n"
            " print('ready', flush=True); input()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", script],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assert_code(
                "WORKSPACE_ACTIVATION_BUSY",
                lambda: activation_maintenance(
                    self.authority_dir, TRANSACTION, SOURCE).__enter__(),
            )
        finally:
            process.stdin.write("\n")
            process.stdin.flush()
            process.communicate(timeout=5)

    def test_resume_rejects_changed_source_corruption_and_lock_inode(self):
        with activation_maintenance(self.authority_dir, TRANSACTION, SOURCE):
            pass
        self.assert_code(
            "WORKSPACE_ACTIVATION_CHANGED",
            lambda: activation_maintenance(
                self.authority_dir, TRANSACTION, "d" * 64).__enter__(),
        )
        self.assert_code(
            "WORKSPACE_ACTIVATION_CHANGED",
            lambda: activation_maintenance(
                self.authority_dir, "e" * 32, SOURCE).__enter__(),
        )

        lock_path, intent_path = activation_paths(self.authority_dir)
        document = json.loads(intent_path.read_text(encoding="utf-8"))
        document["intent_sha256"] = "0" * 64
        intent_path.write_text(json.dumps(document), encoding="utf-8")
        intent_path.chmod(0o600)
        self.assert_code(
            "WORKSPACE_ACTIVATION_INVALID",
            lambda: activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE).__enter__(),
        )

        intent_path.unlink()
        lock_path.unlink()
        with activation_maintenance(self.authority_dir, TRANSACTION, SOURCE):
            pass
        lock_path.unlink()
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)
        self.assert_code(
            "WORKSPACE_ACTIVATION_CHANGED",
            lambda: activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE).__enter__(),
        )

    def test_finish_requires_exact_live_authority_binding_and_fsyncs_removal(self):
        authority = self.authority()
        binding = authority.binding(POLICY)
        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            self.assert_code(
                "WORKSPACE_AUTHORITY_CHANGED",
                lambda: maintenance.finish({**binding, "authority_epoch": 99}),
            )
            self.assertTrue(activation_paths(self.authority_dir)[1].exists())
            result = maintenance.finish(binding)
            with activation_admission(self.authority_dir):
                pass

        self.assertEqual(result["status"], "activation_maintenance_finished")
        self.assertFalse(activation_paths(self.authority_dir)[1].exists())
        with activation_admission(self.authority_dir):
            pass

    def test_finish_without_complete_authority_keeps_intent(self):
        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            self.assert_code(
                "WORKSPACE_ENFORCEMENT_REQUIRED",
                lambda: maintenance.finish({
                    "installation_id": "d" * 32,
                    "authority_epoch": 1,
                    "policy_digest": POLICY,
                }),
            )
        self.assertTrue(activation_paths(self.authority_dir)[1].exists())

    def test_optional_minimum_abi_floor_is_bound_to_canonical_authority(self):
        release, digest = self.release(enforcement_abi=2)
        pin = minimum_abi_path(self.authority_dir)
        document = {
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.authority_dir.absolute()).encode()).hexdigest(),
            "minimum_abi": 2,
        }
        pin.write_text(json.dumps(document), encoding="utf-8")
        pin.chmod(0o600)
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir)
        authority.initialize(POLICY, {digest: 2})

        admitted = verify_launch(release, self.authority_dir)
        self.assertEqual(admitted["minimum_abi"], 2)

        document["authority_path_sha256"] = "0" * 64
        pin.write_text(json.dumps(document), encoding="utf-8")
        pin.chmod(0o600)
        self.assert_code(
            "WORKSPACE_MINIMUM_ABI_INVALID",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_minimum_abi_two_rejects_unversioned_or_conflicting_release_source(self):
        release, digest = self.release(enforcement_abi=2)
        control = release / "workspace_control.py"
        manifest_path = release / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        control.write_text("legacy control source\n")
        manifest["workspace_control.py"] = hashlib.sha256(control.read_bytes()).hexdigest()
        new_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        new_release = release.parent / new_digest[:20]
        release.rename(new_release)
        manifest_path = new_release / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        manifest_path.chmod(0o600)
        control = new_release / "workspace_control.py"
        control.chmod(0o600)
        pin = minimum_abi_path(self.authority_dir)
        pin.write_text(json.dumps({
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.authority_dir.absolute()).encode()).hexdigest(),
            "minimum_abi": 2,
        }))
        pin.chmod(0o600)
        WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        ).initialize(POLICY, {new_digest: 2})

        self.assert_code(
            "WORKSPACE_RELEASE_ABI_MISMATCH",
            lambda: verify_launch(new_release, self.authority_dir),
        )

    def test_startup_barrier_allows_only_verified_launch_until_exact_finish(self):
        release, digest = self.release()
        pin = minimum_abi_path(self.authority_dir)
        pin.write_text(json.dumps({
            "version": 1,
            "authority_path_sha256": hashlib.sha256(
                str(self.authority_dir.absolute()).encode()).hexdigest(),
            "minimum_abi": ENFORCEMENT_ABI,
        }))
        pin.chmod(0o600)
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY, {digest: ENFORCEMENT_ABI})
        binding = authority.binding(POLICY)

        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            allowed = maintenance.allow_start(binding)
        barrier = startup_barrier_path(self.authority_dir)
        self.assertTrue(barrier.is_file())
        self.assertEqual(allowed["binding"], binding)
        self.assert_code("WORKSPACE_ACTIVATION_IN_PROGRESS", self.enter_admission)
        admitted = verify_launch(release, self.authority_dir)
        self.assertEqual(admitted["binding"], binding)
        self.assertEqual(admitted["release_abi"], ENFORCEMENT_ABI)

        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            maintenance.finish(binding)
        self.assertFalse(barrier.exists())
        self.assertFalse(activation_paths(self.authority_dir)[1].exists())

    def test_startup_barrier_wrong_transaction_binding_and_tamper_fail_closed(self):
        release, digest = self.release()
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY, {digest: 1})
        binding = authority.binding(POLICY)
        with activation_maintenance(
                self.authority_dir, TRANSACTION, SOURCE) as maintenance:
            maintenance.allow_start(binding)

        self.assert_code(
            "WORKSPACE_ACTIVATION_CHANGED",
            lambda: activation_maintenance(
                self.authority_dir, "d" * 32, SOURCE).__enter__(),
        )
        barrier = startup_barrier_path(self.authority_dir)
        document = json.loads(barrier.read_text())
        document["binding"]["authority_epoch"] += 1
        barrier.write_text(json.dumps(document))
        barrier.chmod(0o600)
        self.assert_code(
            "WORKSPACE_STARTUP_BARRIER_INVALID",
            lambda: verify_launch(release, self.authority_dir),
        )

        document["barrier_sha256"] = hashlib.sha256(json.dumps(
            {key: value for key, value in document.items()
             if key != "barrier_sha256"},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        barrier.write_text(json.dumps(document))
        barrier.chmod(0o600)
        self.assert_code(
            "WORKSPACE_STARTUP_BARRIER_INVALID",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_latched_authority_with_deleted_minimum_abi_fails_closed(self):
        release, digest = self.release()
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir)
        authority.initialize(POLICY, {digest: 1})
        pin = minimum_abi_path(self.authority_dir)
        self.assertTrue(pin.is_file())

        pin.unlink()

        self.assert_code(
            "WORKSPACE_MINIMUM_ABI_MISSING",
            lambda: verify_launch(release, self.authority_dir),
        )


class ManagedAdmissionEntryPointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.authority_dir = self.root / "workspace-authority"
        self.maintenance = activation_maintenance(
            self.authority_dir, TRANSACTION, SOURCE)
        self.maintenance.__enter__()

    def tearDown(self):
        self.maintenance.__exit__(None, None, None)
        self.temporary.cleanup()

    def bare(self, manager_class, admitted_name, *, asynchronous=False):
        manager = object.__new__(manager_class)
        manager.activation_authority_dir = self.authority_dir
        admitted = mock.AsyncMock() if asynchronous else mock.Mock()
        setattr(manager, admitted_name, admitted)
        return manager, admitted

    def assert_host_denied(self, operation):
        with self.assertRaises(HostRuntimeError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "WORKSPACE_ACTIVATION_IN_PROGRESS")

    async def assert_host_denied_async(self, operation):
        with self.assertRaises(HostRuntimeError) as caught:
            await operation()
        self.assertEqual(caught.exception.code, "WORKSPACE_ACTIVATION_IN_PROGRESS")

    async def test_all_managed_entrypoints_deny_before_admitted_side_effects(self):
        from agent_tasks import AgentTaskManager
        from host_command_operations import HostCommandOperations
        from host_execution import HostExecution
        from host_registered_operations import RegisteredHostOperations
        from host_runtime_operations import HostRuntimeOperations
        from jobs import JobManager
        from runtime_rollout_web import RuntimeRolloutWeb

        jobs, jobs_admitted = self.bare(JobManager, "_start_admitted", asynchronous=True)
        with self.assertRaises(WorkspaceError) as caught:
            await jobs.start("demo", request_id="r")
        self.assertEqual(caught.exception.code, "WORKSPACE_ACTIVATION_IN_PROGRESS")
        jobs_admitted.assert_not_awaited()

        agents, agent_prepare = self.bare(AgentTaskManager, "_prepare_admitted")
        self.assert_host_denied(lambda: agents.prepare(
            "demo", profile_id="p", objective="long enough objective", reason="MISSING_TOOL",
            request_id="r"))
        agent_prepare.assert_not_called()
        agents._start_admitted = mock.AsyncMock()
        await self.assert_host_denied_async(lambda: agents.start("demo", "a" * 32))
        agents._start_admitted.assert_not_awaited()

        execution, execution_prepare = self.bare(HostExecution, "_prepare_admitted")
        self.assert_host_denied(lambda: execution.prepare(
            "demo", executable="/bin/true", argv=[], cwd=".", request_id="r"))
        execution_prepare.assert_not_called()
        execution._execute_admitted = mock.AsyncMock()
        await self.assert_host_denied_async(lambda: execution.execute("demo", "a" * 32))
        execution._execute_admitted.assert_not_awaited()

        commands, command_prepare = self.bare(
            HostCommandOperations, "_prepare_admitted")
        self.assert_host_denied(lambda: commands.prepare("demo", "p", "r"))
        command_prepare.assert_not_called()
        commands._begin_admitted = mock.Mock()
        self.assert_host_denied(lambda: commands._begin("demo", "a" * 32))
        commands._begin_admitted.assert_not_called()

        registered, registered_prepare = self.bare(
            RegisteredHostOperations, "_prepare_admitted")
        self.assert_host_denied(lambda: registered.prepare(
            "demo", "target", action="deploy", parameters={}, request_id="r"))
        registered_prepare.assert_not_called()
        registered._execute_activation_admitted = mock.AsyncMock()
        await self.assert_host_denied_async(
            lambda: registered.execute("demo", "target", "a" * 32))
        registered._execute_activation_admitted.assert_not_awaited()

        runtime, runtime_prepare = self.bare(
            HostRuntimeOperations, "_prepare_activation_admitted")
        self.assert_host_denied(lambda: runtime.prepare(
            "demo", "target", "api", "default", "r"))
        runtime_prepare.assert_not_called()
        runtime._execute_activation_admitted = mock.AsyncMock()
        await self.assert_host_denied_async(
            lambda: runtime.execute("demo", "target", "a" * 32))
        runtime._execute_activation_admitted.assert_not_awaited()

        rollout, rollout_prepare = self.bare(RuntimeRolloutWeb, "_prepare_admitted")
        self.assert_host_denied(lambda: rollout.prepare("personal-ops", "r"))
        rollout_prepare.assert_not_called()
        rollout._execute_admitted = mock.Mock()
        self.assert_host_denied(lambda: rollout.execute("personal-ops", "a" * 32))
        rollout._execute_admitted.assert_not_called()
        rollout._claim_for_worker_admitted = mock.Mock()
        self.assert_host_denied(lambda: rollout.claim_for_worker("a" * 32))
        rollout._claim_for_worker_admitted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
