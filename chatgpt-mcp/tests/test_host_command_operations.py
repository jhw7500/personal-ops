"""One-use web approval + exact command-plan state machine; fake runner only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_command_operations import HostCommandOperations  # noqa: E402
from host_runtime_operations import acquire_flock  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from workspace_host import publication_lane  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


def digest(data):
    return hashlib.sha256(data).hexdigest()


class SafeFakeRunner:
    def __init__(self):
        self.calls = 0
        self.before = None
        self.output = {"exit_code": 0, "duration_ms": 20, "stdout_bytes": 15, "stderr_bytes": 0}

    def __call__(self, frozen, cancel):
        self.calls += 1
        if self.before:
            self.before(frozen, cancel)
        return {**self.output, "termination_verified": True}


class HostCommandOperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "build").mkdir()
        self.artifact = self.project / "build/out.ait"
        self.artifact.write_bytes(b"FAKE-TEST-BUNDLE")
        self.artifact.chmod(0o600)
        self.script = self.root / "test-publish"
        self.script.write_bytes(b"#!/bin/sh\nprintf 'synthetic only'\n")
        self.script.chmod(0o700)
        self.config = self.root / "profile.json"
        self.profile = {
            "project": "tossApp", "label": "Toss test artifact publishing",
            "executable": str(self.script), "executable_sha256": digest(self.script.read_bytes()),
            "argv": ["test"], "cwd": ".", "timeout_seconds": 60,
            "effect": "external_publish",
            "inputs": {"build/out.ait": digest(self.artifact.read_bytes())},
        }
        self.config_data = {"version": 1, "enabled": True, "profiles": {"test-upload": self.profile}}
        self.save_config()
        self.runner = SafeFakeRunner()
        self.ops = HostCommandOperations(
            {"tossApp": self.project}, config_path=self.config,
            state_dir=self.root / "state", runner=self.runner, test_only=True,
        )

    def save_config(self):
        self.config.write_text(json.dumps(self.config_data))
        self.config.chmod(0o600)

    def active_workspace(self):
        owner = self.root / "workspace-owner"
        owner.mkdir(mode=0o700)
        policy = owner / "policy.json"
        policy.write_text(json.dumps({
            "version": 1, "base_root": str(self.root),
            "artifacts": [], "protected_paths": [],
        }), encoding="utf-8")
        policy.chmod(0o600)
        workspace = WorkspaceTools(
            {"tossApp": self.project}, state_dir=self.root / "workspace-authority",
            config_path=policy)
        snapshot = workspace.policy.load(required=True)
        workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
        return workspace

    def err(self, code, call):
        with self.assertRaises(HostRuntimeError) as x:
            call()
        self.assertEqual(x.exception.code, code)

    def plan(self, request_id="plan1"):
        return self.ops.prepare("tossApp", "test-upload", request_id)

    def approve(self, p):
        return self.ops.approve_web(
            "tossApp", operation_id=p["operation_id"],
            plan_digest=p["plan_digest"], source_digest=p["source_digest"],
            confirmation=p["operation_id"],
        )

    def test_pending_plan_can_be_approved_six_days_after_creation(self):
        import host_command_operations

        plan = self.plan("six-day-approval")
        created = datetime.fromisoformat(plan["created_at"])
        with mock.patch.object(
                host_command_operations, "_now", return_value=created + timedelta(days=6)):
            approved = self.approve(plan)
        self.assertTrue(approved["approved"])
        self.assertEqual(self.ops.get("tossApp", plan["operation_id"])["state"], "approved")

    def test_pending_plan_expires_exactly_seven_days_after_creation(self):
        import host_command_operations

        plan = self.plan("seven-day-expiry")
        created = datetime.fromisoformat(plan["created_at"])
        with mock.patch.object(
                host_command_operations, "_now", return_value=created + timedelta(days=7)):
            self.err("PLAN_EXPIRED", lambda: self.approve(plan))
        self.assertFalse(self.ops._receipt_path(plan["operation_id"]).exists())

    async def terminal(self, op_id):
        for _ in range(100):
            state = self.ops.get("tossApp", op_id)
            if state["state"] in ("succeeded", "failed", "outcome_unknown", "cancelled"):
                return state
            await asyncio.sleep(0.02)
        self.fail("worker never reached terminal state")

    def test_plan_is_immutable_and_safe_to_display(self):
        result = self.plan()
        self.assertEqual(result["state"], "pending_host_approval")
        self.assertTrue(result["approval_required"])
        self.assertEqual(result["effect"], "external_publish")
        self.assertEqual(result["approval_url"], "/host-commands?operation_id="+result["operation_id"])
        self.assertEqual(result["plan_digest"], self.plan()["plan_digest"])
        state_text = json.dumps(result)
        for secret in (str(self.root), "build/out.ait", "argv", "nonce", "FAKE-TEST-BUNDLE"):
            self.assertNotIn(secret, state_text)
        self.assertEqual(self.ops.pending("tossApp")["plans"][0]["operation_id"], result["operation_id"])
        self.err("HOST_COMMAND_PROJECT_BUSY", lambda: self.ops.prepare("tossApp", "test-upload", "new"))
        self.err("REQUEST_ID_CONFLICT", lambda: self.ops.prepare("tossApp", "wrong", "plan1"))

    def test_plan_without_installed_runtime_fails_closed(self):
        untrusted = HostCommandOperations(
            {"tossApp": self.project}, config_path=self.config, state_dir=self.root/"other-state")
        self.err("UNSAFE_RUNTIME", lambda: untrusted.prepare("tossApp", "test-upload", "test"))
        self.err("UNSAFE_RUNTIME", lambda: untrusted.list_profiles("tossApp"))
        self.assertEqual(self.runner.calls, 0)

    async def test_web_approval_then_execute_exactly_once(self):
        p = self.plan()
        self.err("HOST_COMMAND_APPROVAL_REQUIRED",
                 lambda: self.ops._begin("tossApp", p["operation_id"]))
        self.assertEqual(self.runner.calls, 0)
        self.approve(p)
        self.err("NOT_PENDING_APPROVAL", lambda: self.approve(p))
        queued = await self.ops.execute("tossApp", p["operation_id"])
        self.assertIn(queued["state"], ("queued", "running", "succeeded"))
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["result"], self.runner.output)
        self.assertEqual(done["phase"], "COMPLETE")
        self.assertEqual(self.runner.calls, 1)
        again = await self.ops.execute("tossApp", p["operation_id"])
        self.assertEqual(again["state"], "succeeded")
        self.assertEqual(self.runner.calls, 1)
        self.assertFalse(self.ops._receipt_path(p["operation_id"]).exists())

    async def test_workspace_lane_covers_runner_and_unverified_termination(self):
        workspace = self.active_workspace()
        entered = threading.Event()
        release = threading.Event()
        def unverified(_frozen, _cancel):
            entered.set()
            release.wait(3)
            return {**self.runner.output, "termination_verified": False}
        self.ops = HostCommandOperations(
            {"tossApp": self.project}, config_path=self.config,
            state_dir=self.root / "workspace-command-state", runner=unverified,
            test_only=True, workspace=workspace)
        p = self.plan("workspace-lane")
        self.approve(p)
        await self.ops.execute("tossApp", p["operation_id"])
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        release.set()
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["state"], "outcome_unknown")
        with self.assertRaises(HostRuntimeError):
            with publication_lane(workspace):
                pass
        self.ops._finish_scope = lambda _identity: None
        self.ops.cancel("tossApp", p["operation_id"])
        with publication_lane(workspace):
            pass

    async def test_busy_publication_lane_does_not_consume_command_approval(self):
        workspace = self.active_workspace()
        self.ops = HostCommandOperations(
            {"tossApp": self.project}, config_path=self.config,
            state_dir=self.root / "busy-command-state", runner=self.runner,
            test_only=True, workspace=workspace)
        plan = self.plan("busy-lane")
        self.approve(plan)
        with publication_lane(workspace):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute("tossApp", plan["operation_id"])
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        self.assertTrue(self.ops._receipt_path(plan["operation_id"]).exists())
        self.assertEqual(self.ops.get("tossApp", plan["operation_id"])["state"], "approved")

    def test_wrong_approval_digests_confirmation_and_source_drift(self):
        p = self.plan()
        kw = {"operation_id": p["operation_id"], "plan_digest": p["plan_digest"],
              "source_digest": p["source_digest"], "confirmation": p["operation_id"]}
        for change in ({"plan_digest": "0"*64}, {"source_digest": "0"*64},
                       {"confirmation": "approve"}):
            with self.subTest(change=change):
                code = "HOST_COMMAND_APPROVAL_DENIED" if "confirmation" in change else "STALE_PLAN"
                self.err(code, lambda: self.ops.approve_web("tossApp", **{**kw, **change}))
        self.artifact.write_bytes(b"MODIFIED-BUNDLE")
        self.err("HOST_COMMAND_INPUT_CHANGED", lambda: self.ops.approve_web("tossApp", **kw))
        self.assertEqual(self.runner.calls, 0)

    async def test_changed_input_after_approval_cannot_execute(self):
        p = self.plan()
        self.approve(p)
        self.artifact.write_bytes(b"MODIFIED-BUNDLE")
        self.err("HOST_COMMAND_INPUT_CHANGED", lambda: self.ops._begin("tossApp", p["operation_id"]))
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.ops.get("tossApp", p["operation_id"])["state"], "approved")

    def test_new_prepare_retires_stale_unstarted_approved_command(self):
        old = self.plan("stale-approved-command")
        self.approve(old)
        receipt = self.ops._receipt_path(old["operation_id"])
        self.assertTrue(receipt.is_file())
        self.artifact.write_bytes(b"CHANGED-BEFORE-EXECUTION")
        self.config_data["profiles"]["test-upload"]["inputs"]["build/out.ait"] = digest(
            self.artifact.read_bytes())
        self.save_config()

        replacement = self.plan("replacement-after-input-drift")

        retired = self.ops.get("tossApp", old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "HOST_COMMAND_PLAN_STALE"))
        self.assertFalse(receipt.exists())
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        self.err("PLAN_EXPIRED", lambda: self.approve(old))
        self.assertEqual(self.ops._begin("tossApp", old["operation_id"])["state"], "expired")
        self.assertEqual(self.runner.calls, 0)

    def test_new_prepare_keeps_unchanged_approved_command_blocking(self):
        old = self.plan("unchanged-approved-command")
        self.approve(old)
        self.err("HOST_COMMAND_PROJECT_BUSY",
                 lambda: self.plan("must-remain-blocked"))
        self.assertEqual(self.ops.get("tossApp", old["operation_id"])["state"], "approved")

    def test_new_prepare_does_not_retire_command_when_staleness_check_is_uncertain(self):
        old = self.plan("uncertain-command-check")
        self.approve(old)
        receipt = self.ops._receipt_path(old["operation_id"])
        with mock.patch.object(
                self.ops.profiles, "verify_frozen",
                side_effect=HostRuntimeError("HOST_COMMAND_CONFIG_UNAVAILABLE")):
            self.err("HOST_COMMAND_CONFIG_UNAVAILABLE",
                     lambda: self.plan("uncertain-command-replacement"))
        self.assertEqual(self.ops.get("tossApp", old["operation_id"])["state"], "approved")
        self.assertTrue(receipt.is_file())

    async def test_changed_input_after_effect_is_outcome_unknown(self):
        p = self.plan()
        self.approve(p)
        self.runner.before = lambda frozen, cancel: self.artifact.write_bytes(b"changed by upload script")
        await self.ops.execute("tossApp", p["operation_id"])
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["state"], "outcome_unknown")
        self.assertEqual(done["code"], "HOST_COMMAND_INPUT_CHANGED")

    async def test_failure_after_execution_is_not_claimed_rolled_back(self):
        p = self.plan()
        self.approve(p)
        self.runner.output = {**self.runner.output, "exit_code": 1}
        await self.ops.execute("tossApp", p["operation_id"])
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["state"], "outcome_unknown")
        self.assertEqual(done["code"], "HOST_COMMAND_EXIT_NONZERO")
        self.assertEqual(self.runner.calls, 1)

    async def test_nonzero_exit_preserves_observed_numeric_diagnostics(self):
        p = self.plan()
        self.approve(p)
        self.runner.output = {"exit_code": 7, "duration_ms": 123,
                              "stdout_bytes": 51, "stderr_bytes": 28}
        await self.ops.execute("tossApp", p["operation_id"])
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["result"], {"exit_code": 7, "duration_ms": 123,
                                          "stdout_bytes": 51, "stderr_bytes": 28})
        self.assertEqual(done["state"], "outcome_unknown")

    async def test_postcheck_failure_keeps_validated_command_diagnostics(self):
        p = self.plan()
        self.approve(p)
        self.runner.before = lambda frozen, cancel: self.artifact.write_bytes(b"changed")
        await self.ops.execute("tossApp", p["operation_id"])
        done = await self.terminal(p["operation_id"])
        self.assertEqual(done["result"], {"exit_code": 0, "duration_ms": 20,
                                          "stdout_bytes": 15, "stderr_bytes": 0})
        self.assertEqual(done["state"], "outcome_unknown")

    def mark_abandoned_running(self, operation_id):
        state = self.ops._load(operation_id)
        state.update(state="running", phase="EXECUTE", execution_identity={
            "task_id": operation_id, "unit": "personal-ops-agent-" + "a" * 32 + ".service",
            "invocation_id": "b" * 32, "boot_id": "12345678-1234-1234-1234-123456789abc",
            "uid": os.getuid(), "pid": 123, "start_ticks": 456,
            "wrapper_pid": 122, "wrapper_start_ticks": 455,
            "cgroup": (f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                       "app.slice/personal-ops-agent-" + "a" * 32 + ".service"),
        }, termination_verified=False)
        self.ops._save(state)

    def rebuilt_manager(self, *, empty, finisher=None):
        return HostCommandOperations(
            {"tossApp": self.project}, config_path=self.config,
            state_dir=self.root / "state", runner=SafeFakeRunner(), test_only=True,
            scope_inspector=lambda identity: {"termination_verified": empty},
            scope_finisher=finisher,
        )

    def test_restart_recovers_lease_free_completed_scope_without_reexecuting(self):
        p = self.plan()
        self.mark_abandoned_running(p["operation_id"])
        recovered = self.rebuilt_manager(empty=True)
        state = recovered.get("tossApp", p["operation_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertTrue(state["termination_verified"])
        self.assertEqual(recovered.runner.calls, 0)
        next_plan = recovered.prepare("tossApp", "test-upload", "after-restart")
        self.assertEqual(next_plan["state"], "pending_host_approval")

    def test_another_live_lease_owner_is_not_recovered_or_cancelled(self):
        p = self.plan()
        self.mark_abandoned_running(p["operation_id"])
        lease = acquire_flock(self.ops._project_lock_path("tossApp"))
        try:
            observer = self.rebuilt_manager(empty=True)
            self.assertEqual(observer.get("tossApp", p["operation_id"])["state"], "running")
            self.err("HOST_COMMAND_PROCESS_NOT_OWNED",
                     lambda: observer.cancel("tossApp", p["operation_id"]))
            self.assertEqual(self.ops.get("tossApp", p["operation_id"])["state"], "running")
        finally:
            os.close(lease)

    def test_unverified_orphan_blocks_next_plan_until_explicit_verified_cancel(self):
        p = self.plan()
        self.mark_abandoned_running(p["operation_id"])
        observer = self.rebuilt_manager(empty=False,
            finisher=lambda identity: {"termination_verified": True})
        state = observer.get("tossApp", p["operation_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertFalse(state["termination_verified"])
        self.err("HOST_COMMAND_PROJECT_BUSY",
                 lambda: observer.prepare("tossApp", "test-upload", "blocked"))
        cancelled = observer.cancel("tossApp", p["operation_id"])
        self.assertTrue(cancelled["termination_verified"])
        self.assertEqual(observer.prepare("tossApp", "test-upload", "unblocked")["state"],
                         "pending_host_approval")

    async def test_unverified_cleanup_retains_lease_and_numeric_diagnostics_until_retry(self):
        p = self.plan()
        self.approve(p)
        partial = {"exit_code": -9, "duration_ms": 90,
                   "stdout_bytes": 19, "stderr_bytes": 7}

        def interrupted(frozen, cancel):
            self.mark_abandoned_running(p["operation_id"])
            error = HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            error.termination_verified = False
            error.result = {**partial, "raw_output": "private command output"}
            raise error

        self.runner.before = interrupted
        self.ops._scope_finisher = lambda identity: {"termination_verified": False}
        await self.ops.execute("tossApp", p["operation_id"])
        state = await self.terminal(p["operation_id"])
        for _ in range(100):
            if p["operation_id"] not in self.ops._workers:
                break
            await asyncio.sleep(.01)
        self.assertFalse(self.ops._workers)
        self.assertEqual(state["result"], partial)
        self.assertFalse(state["termination_verified"])
        self.assertNotIn("execution_identity", state)
        self.assertNotIn("private command output", json.dumps(state))
        lease = self.ops._retained_leases[p["operation_id"]]
        try:
            self.err("TARGET_BUSY", lambda: acquire_flock(self.ops._project_lock_path("tossApp")))
            with self.assertRaises(HostRuntimeError) as failure:
                await self.ops.close()
            self.assertEqual(failure.exception.code, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED")
            self.assertEqual(self.ops._retained_leases[p["operation_id"]], lease)
            self.err("TARGET_BUSY", lambda: acquire_flock(self.ops._project_lock_path("tossApp")))
        finally:
            self.ops._scope_finisher = lambda identity: {"termination_verified": True}
            await self.ops.close()
        self.assertFalse(self.ops._retained_leases)
        state = self.ops.get("tossApp", p["operation_id"])
        self.assertTrue(state["termination_verified"])
        self.assertEqual(state["result"], partial)
        self.assertEqual(self.ops.prepare("tossApp", "test-upload", "after-retry")["state"],
                         "pending_host_approval")

    async def test_queue_write_failure_never_registers_an_unstarted_worker(self):
        p = self.plan()
        self.approve(p)
        real_save = self.ops._save

        def disk_full(state):
            if state["state"] == "queued":
                raise OSError("synthetic disk full")
            real_save(state)

        with mock.patch.object(self.ops, "_save", side_effect=disk_full):
            with self.assertRaises(OSError):
                await self.ops.execute("tossApp", p["operation_id"])
        self.assertEqual(self.runner.calls, 0)
        self.assertFalse(self.ops._receipt_path(p["operation_id"]).exists())
        self.assertFalse(self.ops._workers)
        await self.ops.close()
        lease = acquire_flock(self.ops._project_lock_path("tossApp"))
        os.close(lease)

    def stopped_before_launch(self, phase, state_name):
        p = self.plan()
        state = self.ops._load(p["operation_id"])
        state.update(state=state_name, phase=phase, execution_identity=None)
        self.ops._save(state)
        observer = self.rebuilt_manager(empty=False)
        recovered = observer.get("tossApp", p["operation_id"])
        self.assertEqual(recovered["state"], "outcome_unknown")
        self.assertTrue(recovered["termination_verified"])
        self.assertTrue(observer.cancel("tossApp", p["operation_id"])["termination_verified"])
        self.assertEqual(observer.runner.calls, 0)
        self.assertEqual(observer.prepare("tossApp", "test-upload", "after-prelaunch-stop")["state"],
                         "pending_host_approval")

    def test_restart_recovers_queued_operation_before_launch(self):
        self.stopped_before_launch("QUEUED", "queued")

    def test_restart_recovers_precheck_before_launch(self):
        self.stopped_before_launch("PRECHECK", "running")

    def test_restart_cannot_clear_execute_without_durable_identity(self):
        p = self.plan()
        state = self.ops._load(p["operation_id"])
        state.update(state="running", phase="EXECUTE", execution_identity=None)
        self.ops._save(state)
        observer = self.rebuilt_manager(empty=False)
        self.assertFalse(observer.get("tossApp", p["operation_id"])["termination_verified"])
        self.err("HOST_COMMAND_PROCESS_TREE_UNVERIFIED",
                 lambda: observer.cancel("tossApp", p["operation_id"]))
        self.err("HOST_COMMAND_PROJECT_BUSY",
                 lambda: observer.prepare("tossApp", "test-upload", "unknown-launch"))

    def test_source_profile_not_registered_cannot_use_existing_receipt(self):
        p = self.plan()
        self.approve(p)
        self.config_data["profiles"].pop("test-upload")
        self.save_config()
        self.err("HOST_COMMAND_PROFILE_NOT_FOUND", lambda: self.ops._begin("tossApp", p["operation_id"]))
        self.assertEqual(self.runner.calls, 0)

    async def test_cancel_current_owned_worker_only(self):
        started = threading.Event()
        def hold(frozen, cancel):
            started.set()
            while not cancel.wait(0.02):
                pass
        self.runner.before = hold
        p = self.plan()
        self.approve(p)
        await self.ops.execute("tossApp", p["operation_id"])
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        self.ops.cancel("tossApp", p["operation_id"])
        done = await self.terminal(p["operation_id"])
        # An external publish may have occurred; cancellation is not a rollback.
        self.assertEqual(done["state"], "outcome_unknown")
        self.assertEqual(done["code"], "HOST_COMMAND_CANCELLED")
        self.assertEqual(self.runner.calls, 1)

    def test_private_state_only_owner_accessible(self):
        p = self.plan()
        for path in (self.ops.state_dir, self.ops.plans_dir, self.ops.receipts_dir):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.ops._state_path(p["operation_id"]).stat().st_mode & 0o777, 0o600)

    def test_missing_config_never_creates_plan(self):
        self.config.unlink()
        self.err("HOST_COMMAND_CONFIG_UNAVAILABLE", lambda: self.plan())
        self.assertEqual(self.runner.calls, 0)


if __name__ == "__main__":
    unittest.main()
