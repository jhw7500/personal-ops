"""Generic registered host operation manager: plan, web approval, execution, events.

Fake adapter + FakeHostRunner only. No Docker, DB, host config or real state dir.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from host_operation_fixtures import FakeHostRunner, commit_all, fixture_v3, git  # noqa: E402
from host_process import HostCommandError, run_registered_command  # noqa: E402
from host_execution import HostExecution  # noqa: E402
from host_registered_operations import RegisteredHostOperations  # noqa: E402
from host_registry import HostTargetRegistry  # noqa: E402
from host_runtime_config import PROJECT, TARGET, HostRuntimeError  # noqa: E402
import host_registered_operations  # noqa: E402
import host_runtime_operations  # noqa: E402
from host_runtime_operations import HostRuntimeOperations, _digest, host_admission  # noqa: E402
from host_registered_operations import observed_registered_operation_blocks_host  # noqa: E402
from workspace_host import publication_lane  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402

HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class FakeAdapter:
    """Implements the A2 adapter contract on top of a FakeHostRunner."""

    def __init__(self, runner):
        self.runner = runner
        self.identity = {"api": "a" * 64, "worker": "b" * 64, "db": "c" * 64}
        self.preflight_calls = 0
        self.execute_calls = 0
        self.postcheck_calls = 0
        self.fail_before = None
        self.fail_after = None
        self.postcheck_error = None
        self.verified = True
        self.extra_events = []
        self.mutate_in_preflight = False
        self.on_postcheck = None
        self.emit_errors = []
        self.immutable_seen = None
        self.verifications = ["SERVICES_RUNNING", "PROTECTED_IDENTITY_UNCHANGED"]
        self.reported_verifications = None
        self.declare_side_effects = True
        self.preflight_error = None
        self.on_preflight = None

    def preflight(self, target, spec, context):
        self.preflight_calls += 1
        self.runner(["docker", "compose", "--project-name", target["compose"]["project_name"],
                     "ps", "--format", "json"], cwd=context.snapshot, timeout=10)
        if self.mutate_in_preflight:
            context.side_effects_started()
        if self.on_preflight:
            self.on_preflight()
        if self.preflight_error:
            raise self.preflight_error
        return {"identity": {"containers": dict(self.identity),
                             "services": list(spec["parameters"].get("services", []))},
                "verifications": list(self.verifications)}

    def execute(self, target, immutable, context, emit):
        self.execute_calls += 1
        self.immutable_seen = copy.deepcopy(immutable)
        emit("PRECHECK", "IDENTITY_VERIFIED", {"identity_verified": True})
        for event in self.extra_events:
            try:
                emit(*event)
            except HostRuntimeError as exc:
                self.emit_errors.append(exc.code)
        if self.fail_before:
            raise self.fail_before
        services = immutable["parameters"]["services"]
        if self.declare_side_effects:
            context.side_effects_started()
            emit("EXECUTE", "COMPOSE_RESTART", {"services": services, "count": len(services)})
        self.runner(["docker", "compose", "--project-name", target["compose"]["project_name"],
                     "restart", *services], cwd=context.snapshot, timeout=60)
        if self.fail_after:
            raise self.fail_after
        return {"services": services}

    def postcheck(self, target, immutable, result):
        self.postcheck_calls += 1
        if self.on_postcheck:
            self.on_postcheck()
        if self.postcheck_error:
            raise self.postcheck_error
        reported = (self.reported_verifications if self.reported_verifications is not None
                    else self.verifications)
        return {"verified": self.verified, "services": result["services"],
                "verifications": list(reported),
                "health_verified": True, "stdout": "raw container log"}


class RegisteredOperationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.payload = fixture_v3(self.root)
        commit_all(self.root)
        self.config = self.home / "config" / "host-runtime.json"
        self.config.parent.mkdir(mode=0o700)
        self.write_config()
        self.projects = {PROJECT: self.root}
        self.state_dir = self.home / "state" / "host-runtime"
        self.runner = FakeHostRunner()
        self.adapter = FakeAdapter(self.runner.run)
        self.factory_calls = []
        self.ops = self.make_ops()

    def write_config(self):
        self.config.write_text(json.dumps(self.payload))
        self.config.chmod(0o600)

    def active_workspace(self):
        owner = self.home / "workspace-owner"
        owner.mkdir(mode=0o700)
        policy = owner / "policy.json"
        policy.write_text(json.dumps({
            "version": 1, "base_root": str(self.home),
            "artifacts": [], "protected_paths": [],
        }), encoding="utf-8")
        policy.chmod(0o600)
        workspace = WorkspaceTools(
            self.projects, state_dir=self.home / "workspace-authority",
            config_path=policy)
        snapshot = workspace.policy.load(required=True)
        workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
        return workspace

    def factory(self, target, action, runner):
        self.factory_calls.append((target["id"], action, runner))
        return self.adapter

    def make_ops(self, **overrides):
        options = {"state_dir": self.state_dir, "adapter_factory": self.factory,
                   "runner": self.runner.run}
        options.update(overrides)
        registry = HostTargetRegistry(self.projects, self.config)
        return RegisteredHostOperations(self.projects, registry, **options)

    def plan(self, request_id="restart-1", services=("api",), target=TARGET, project=PROJECT,
             ops=None):
        return (ops or self.ops).prepare(project, target, action="compose.restart",
                                         parameters={"services": list(services)},
                                         request_id=request_id)

    def approve(self, plan, ops=None):
        return (ops or self.ops).approve_web(
            plan["project"], plan["target"], operation_id=plan["operation_id"],
            plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
            confirmation=plan["operation_id"])

    async def finish(self, ops=None):
        workers = list((ops or self.ops)._workers.values())
        await asyncio.to_thread(lambda: [worker.join(30) for worker in workers])

    async def run_approved(self, request_id="restart-1"):
        plan = self.plan(request_id)
        self.approve(plan)
        queued = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        return plan, queued, self.ops.get(PROJECT, TARGET, plan["operation_id"])

    def registered(self, *parts):
        return self.state_dir.joinpath("registered", *parts)

    def mutating_calls(self):
        return [call["key"] for call in self.runner.calls
                if call["key"] not in ("ps", "config", "docker inspect")]

    async def test_no_approval_means_no_host_effect(self):
        plan = self.ops.prepare("tossApp", "tarororo-test",
                                action="compose.restart", parameters={"services": ["api"]},
                                request_id="restart-1")
        self.assertEqual(plan["state"], "pending_host_approval")
        with self.assertRaises(HostRuntimeError):
            await self.ops.execute("tossApp", "tarororo-test", plan["operation_id"])
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.adapter.execute_calls, 0)

    async def test_workspace_lane_covers_execute_and_postcheck(self):
        workspace = self.active_workspace()
        self.ops = self.make_ops(workspace=workspace)
        entered = threading.Event()
        release = threading.Event()
        self.adapter.on_postcheck = lambda: (entered.set(), release.wait(3))
        plan = self.plan("workspace-lane")
        self.approve(plan)
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        release.set()
        await self.finish()
        with publication_lane(workspace):
            pass

    async def test_worker_escape_with_running_state_keeps_durable_reservation(self):
        workspace = self.active_workspace()
        self.ops = self.make_ops(workspace=workspace)
        plan = self.plan("workspace-worker-save-failure")
        self.approve(plan)

        def terminal_save_failed(operation_id, _target_spec, _adapter):
            self.ops._record(operation_id, state="running", phase="EXECUTE")
            raise RuntimeError("synthetic terminal state save failure")

        with patch.object(threading, "excepthook"), \
                patch.object(self.ops, "_run_body", side_effect=terminal_save_failed):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await self.finish()

        self.assertEqual(self.ops._read_state(plan["operation_id"])["state"], "running")
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")

    async def test_reservation_cleanup_failure_is_bounded_and_releases_target_fd(self):
        workspace = self.active_workspace()
        self.ops = self.make_ops(workspace=workspace)
        plan = self.plan("workspace-reservation-cleanup-failure")
        self.approve(plan)

        with self.assertLogs("host_registered_operations", level="ERROR") as logged, \
                patch("host_registered_operations.complete_host_operation",
                      side_effect=HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await self.finish()

        state = self.ops._read_state(plan["operation_id"])
        self.assertEqual(
            (state["state"], state["phase"], state["code"]),
            ("outcome_unknown", "RECOVER", "WORKSPACE_HOST_RESERVATION_RELEASE_FAILED"))
        self.assertIn("WORKSPACE_HOST_RESERVATION_RELEASE_FAILED", "\n".join(logged.output))
        target_fd = host_runtime_operations.acquire_flock(
            self.ops._lock_path_for(state["immutable"]["target_lock"]))
        os.close(target_fd)
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")

    async def test_busy_publication_lane_does_not_consume_registered_approval(self):
        workspace = self.active_workspace()
        self.ops = self.make_ops(workspace=workspace)
        plan = self.plan("busy-lane")
        self.approve(plan)
        with publication_lane(workspace):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        self.assertTrue(self.ops._receipt_path(plan["operation_id"]).exists())
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "approved")

    def test_standalone_manager_discovers_default_active_workspace(self):
        config_home = self.home / "default-config"
        state_home = self.home / "default-state"
        with patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(config_home), "XDG_STATE_HOME": str(state_home),
        }):
            policy = config_home / "chatgpt-coding-mcp" / "workspace-policy.json"
            policy.parent.mkdir(parents=True, mode=0o700)
            config_home.chmod(0o700)
            policy.write_text(json.dumps({
                "version": 1, "base_root": str(self.home),
                "artifacts": [], "protected_paths": [],
            }), encoding="utf-8")
            policy.chmod(0o600)
            workspace = WorkspaceTools(self.projects)
            snapshot = workspace.policy.load(required=True)
            workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
            standalone = self.make_ops()

        self.assertIsNotNone(standalone.workspace)
        self.assertTrue(standalone.workspace.enabled)

    async def test_plan_public_output_is_bounded_and_carries_no_secret(self):
        before = datetime.now(timezone.utc)
        plan = self.plan()
        self.assertTrue(HEX32.match(plan["operation_id"]))
        self.assertEqual((plan["state"], plan["phase"]), ("pending_host_approval", "APPROVAL"))
        self.assertEqual(plan["action"], "compose.restart")
        self.assertEqual(plan["parameters"], {"services": ["api"]})
        self.assertEqual(plan["impact"]["interrupted"], ["api"])
        self.assertEqual(plan["verifications"], ["SERVICES_RUNNING", "PROTECTED_IDENTITY_UNCHANGED"])
        self.assertEqual(plan["target_identity"],
                         {"id": TARGET, "environment": "test", "compose_project": TARGET,
                          "services": ["api", "worker", "db"]})
        for key in ("plan_digest", "source_digest", "config_digest", "executor_digest"):
            self.assertTrue(HEX64.match(plan[key]), key)
        self.assertEqual(plan["config_digest"],
                         HostTargetRegistry(self.projects, self.config).config_digest())
        self.assertEqual(plan["source"]["review_policy"], "git_head")
        self.assertEqual(plan["source"]["changed_paths"], [])
        self.assertEqual(plan["source"]["high_risk_changes"], [])
        expires = datetime.fromisoformat(plan["expires_at"])
        self.assertLessEqual(abs((expires - before).total_seconds() - 604800), 5)
        self.assertEqual(plan["approval_url"],
                         f"https://cantopsbuildserver.tail661d29.ts.net:8443"
                         f"/host-operations?project=tossApp&target=tarororo-test"
                         f"&operation_id={plan['operation_id']}")
        state_file = self.registered("operations", plan["operation_id"] + ".json")
        nonce = json.loads(state_file.read_text())["immutable"]["nonce"]
        self.assertTrue(HEX64.match(nonce))
        self.assertNotIn(nonce, json.dumps(plan))
        self.assertNotIn("nonce", json.dumps(plan))
        self.assertNotIn(str(self.state_dir), json.dumps(plan))
        self.assertEqual(self.adapter.preflight_calls, 1)
        self.assertEqual(self.adapter.execute_calls, 0)
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.factory_calls[0][:2], (TARGET, "compose.restart"))
        self.assertEqual(self.factory_calls[0][2], self.runner.run)

    async def test_private_state_is_owner_only(self):
        plan, _, done = await self.run_approved()
        self.assertEqual(done["state"], "succeeded")
        registered = self.registered()
        for path in [registered, *registered.rglob("*")]:
            info = path.lstat()
            self.assertEqual(info.st_uid, os.getuid(), path)
            self.assertFalse(stat.S_ISLNK(info.st_mode), path)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                self.assertEqual(mode, 0o700, path)
            else:
                self.assertIn(mode, (0o600, 0o700), path)
        self.assertTrue(self.registered("events", plan["operation_id"] + ".json").is_file())
        self.assertTrue(self.registered("snapshots", plan["operation_id"], "source").is_dir())
        for path in registered.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"raw container log", path.read_bytes(), path)

    async def test_web_approval_then_execute_has_exactly_one_effect(self):
        plan = self.plan()
        approved = self.approve(plan)
        self.assertEqual(approved, {"operation_id": plan["operation_id"], "approved": True,
                                    "state": "approved", "plan_digest": plan["plan_digest"]})
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "approved")
        self.assertNotIn("approval_url", self.ops.get(PROJECT, TARGET, plan["operation_id"]))
        queued = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(queued["state"], "queued")
        self.assertFalse(self.registered("receipts", plan["operation_id"] + ".json").exists())
        again = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertIn(again["state"], ("queued", "running", "succeeded"))
        await self.finish()
        done = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual((done["state"], done["phase"], done["code"]),
                         ("succeeded", "COMPLETE", None))
        self.assertEqual(done["result"], {"verified": True, "services": ["api"],
                                          "health_verified": True,
                                          "verifications": ["SERVICES_RUNNING",
                                                            "PROTECTED_IDENTITY_UNCHANGED"]})
        self.assertEqual(self.runner.effects, ["restart"])
        repeat = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(repeat["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["restart"])
        self.assertEqual(self.adapter.execute_calls, 1)
        self.assertNotIn("nonce", self.adapter.immutable_seen)
        self.assertEqual(self.adapter.immutable_seen["parameters"], {"services": ["api"]})
        snapshot = self.registered("snapshots", plan["operation_id"], "source")
        self.assertEqual(self.runner.last_command("restart")[-1], "api")
        self.assertEqual([c["cwd"] for c in self.runner.calls if c["key"] == "restart"],
                         [snapshot])

    async def test_web_approval_requires_exact_confirmation_and_digests(self):
        plan = self.plan()
        good = {"operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"],
                "source_digest": plan["source_digest"], "confirmation": plan["operation_id"]}
        for change, code in (({"confirmation": "yes"}, "HOST_APPROVAL_DENIED"),
                             ({"plan_digest": "XYZ"}, "HOST_APPROVAL_DENIED"),
                             ({"source_digest": "0" * 63}, "HOST_APPROVAL_DENIED"),
                             ({"plan_digest": "0" * 64}, "STALE_PLAN"),
                             ({"source_digest": "0" * 64}, "STALE_PLAN")):
            with self.subTest(change=change):
                with self.assertRaises(HostRuntimeError) as caught:
                    self.ops.approve_web(PROJECT, TARGET, **{**good, **change})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                                 "pending_host_approval")
        with self.assertRaises(HostRuntimeError):
            self.ops.approve_web(PROJECT, "other-target", **good)
        self.approve(plan)
        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(plan)
        self.assertEqual(caught.exception.code, "NOT_PENDING_APPROVAL")
        self.assertEqual(self.runner.effects, [])

    async def test_tampered_forged_and_replayed_receipts_cause_no_effect(self):
        plan = self.plan()
        receipt = self.registered("receipts", plan["operation_id"] + ".json")
        private = json.loads(self.registered("operations", plan["operation_id"] + ".json")
                             .read_text())
        forged = {"operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"],
                  "nonce": private["immutable"]["nonce"],
                  "approved_at": datetime.now(timezone.utc).isoformat()}
        receipt.parent.mkdir(mode=0o700, exist_ok=True)
        receipt.write_text(json.dumps(forged))
        receipt.chmod(0o600)
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "HOST_APPROVAL_REQUIRED")
        receipt.unlink()
        self.approve(plan)
        valid = json.loads(receipt.read_text())
        for label, value in (("nonce", {**valid, "nonce": "0" * 64}),
                             ("digest", {**valid, "plan_digest": "f" * 64}),
                             ("extra key", {**valid, "approved": True}),
                             ("future", {**valid, "approved_at": (
                                 datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}),
                             ("missing", None), ("mode", valid)):
            with self.subTest(label=label):
                receipt.unlink(missing_ok=True)
                if value is not None:
                    receipt.write_text(json.dumps(value))
                    receipt.chmod(0o644 if label == "mode" else 0o600)
                with self.assertRaises(HostRuntimeError) as caught:
                    await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(caught.exception.code, "INVALID_HOST_APPROVAL")
                self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                                 "approved")
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.adapter.execute_calls, 0)
        receipt.unlink(missing_ok=True)
        receipt.write_text(json.dumps(valid))
        receipt.chmod(0o600)
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(self.runner.effects, ["restart"])
        receipt.write_text(json.dumps(valid))
        receipt.chmod(0o600)
        replay = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(replay["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["restart"])

    async def test_plans_and_approvals_expire_after_seven_days(self):
        later = datetime.now(timezone.utc) + timedelta(days=7, seconds=1)
        pending = self.plan("expire-pending")
        with patch("host_registered_operations._now", return_value=later):
            with self.assertRaises(HostRuntimeError) as caught:
                self.approve(pending)
            self.assertEqual(caught.exception.code, "PLAN_EXPIRED")
            self.assertEqual(self.ops.get(PROJECT, TARGET, pending["operation_id"])["state"],
                             "expired")
            self.assertEqual(self.ops.pending(PROJECT)["plans"], [])
        approved = self.plan("expire-approved")
        self.approve(approved)
        restarted = self.make_ops()
        self.assertEqual(restarted.get(PROJECT, TARGET, approved["operation_id"])["state"],
                         "approved")
        with patch("host_registered_operations._now", return_value=later):
            result = await self.ops.execute(PROJECT, TARGET, approved["operation_id"])
        self.assertEqual((result["state"], result["code"]), ("expired", "PLAN_EXPIRED"))
        self.assertFalse(self.registered("receipts", approved["operation_id"] + ".json").exists())
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.adapter.execute_calls, 0)

    async def test_request_id_dedupe_and_conflict(self):
        first = self.plan("same-1", services=("worker", "api"))
        again = self.plan("same-1", services=("api", "worker"))
        self.assertEqual((again["operation_id"], again["plan_digest"]),
                         (first["operation_id"], first["plan_digest"]))
        self.assertEqual(self.adapter.preflight_calls, 1)
        for services in (("api",), ("worker",)):
            with self.subTest(services=services):
                with self.assertRaises(HostRuntimeError) as caught:
                    self.plan("same-1", services=services)
                self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.prepare(PROJECT, TARGET, action="compose.stop",
                             parameters={"services": ["api", "worker"]}, request_id="same-1")
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")
        for bad in ("", "has space", "x" * 81, "한글", None, 7):
            with self.subTest(request_id=bad):
                with self.assertRaises(HostRuntimeError) as caught:
                    self.plan(bad)
                self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")
        self.assertEqual(len(list(self.registered("operations").glob("*.json"))), 1)

    async def test_history_cap_and_single_active_plan_per_target(self):
        first = self.plan("active-1")
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("active-2")
        self.assertEqual(caught.exception.code, "TARGET_BUSY")
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.reject_web(PROJECT, TARGET, operation_id=first["operation_id"],
                                confirmation="no")
        self.assertEqual(caught.exception.code, "HOST_APPROVAL_DENIED")
        rejected = self.ops.reject_web(PROJECT, TARGET, operation_id=first["operation_id"],
                                       confirmation=first["operation_id"])
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual((await self.ops.execute(PROJECT, TARGET, first["operation_id"]))["state"],
                         "rejected")
        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(first)
        self.assertEqual(caught.exception.code, "NOT_PENDING_APPROVAL")
        self.plan("active-2")
        with patch("host_registered_operations.MAX_OPERATIONS", 2):
            with self.assertRaises(HostRuntimeError) as caught:
                self.plan("active-3")
            self.assertEqual(caught.exception.code, "HISTORY_CAPACITY_REACHED")
        self.assertEqual(self.runner.effects, [])

    async def test_dynamic_host_gate_blocks_registered_admission_without_consuming_receipt(self):
        plan = self.plan("gate-block")
        self.approve(plan)
        receipt = self.registered("receipts", plan["operation_id"] + ".json")
        with host_admission(self.state_dir.parent):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "TARGET_BUSY")
        self.assertTrue(receipt.exists())
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "approved")

    def test_stale_valid_registered_approved_record_does_not_block_dynamic_host(self):
        plan = self.plan("stale-approved")
        self.approve(plan)
        state = self.ops._load(PROJECT, TARGET, plan["operation_id"])
        state["immutable"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        state["plan_digest"] = _digest(state["immutable"])
        self.ops._save(state)
        self.assertFalse(observed_registered_operation_blocks_host(
            self.registered("operations", plan["operation_id"] + ".json")))

    async def test_registered_admission_durable_state_blocks_dynamic_host_start(self):
        plan = self.plan("typed-first")
        self.approve(plan)
        entered, release = threading.Event(), threading.Event()
        self.adapter.on_postcheck = lambda: (entered.set(), release.wait(5))
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))

        class Access:
            def require(self, project, *, grant_id=None, codex=False):
                return {"project": project, "grant_id": "g" * 32,
                        "expires_at": (datetime.now(timezone.utc)
                                       + timedelta(hours=1)).isoformat()}

            @contextmanager
            def guard(self, project, grant_id, *, codex=False):
                yield self.require(project, grant_id=grant_id, codex=codex)

        effects = []
        self.state_dir.parent.chmod(0o700)
        dynamic = HostExecution(
            self.projects, self.state_dir.parent, access=Access(),
            runner=lambda *_args, **_kwargs: effects.append(True), trusted_runtime=True)
        try:
            dynamic_plan = dynamic.prepare(
                PROJECT, executable="/usr/bin/true", argv=[], cwd=".",
                request_id="dynamic-after-typed")
            await dynamic.execute(PROJECT, dynamic_plan["operation_id"])
            for _ in range(100):
                result = dynamic.get(PROJECT, dynamic_plan["operation_id"])
                if result["state"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(result["code"], "HOST_EXECUTION_MANAGED_WORK_BUSY")
            self.assertEqual(effects, [])
        finally:
            release.set()
            await self.finish()
            await dynamic.close()

    async def test_legacy_target_lock_is_shared_both_ways(self):
        plan = self.plan()
        self.approve(plan)
        legacy = HostRuntimeOperations(self.projects, state_dir=self.state_dir)
        held = legacy._acquire_target_lock()
        try:
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            self.assertEqual(caught.exception.code, "TARGET_BUSY")
            self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                             "approved")
        finally:
            os.close(held)
        self.assertEqual(self.runner.effects, [])
        entered, release = threading.Event(), threading.Event()
        self.adapter.on_postcheck = lambda: (entered.set(), release.wait(5))
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertTrue(legacy._target_lock_busy())
            fd = os.open(self.state_dir / "target.lock", os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            live = self.ops.get(PROJECT, TARGET, plan["operation_id"])
            self.assertEqual((live["state"], live["phase"]), ("running", "VERIFY"))
        finally:
            release.set()
            await self.finish()
        self.assertFalse(legacy._target_lock_busy())
        self.assertEqual(self.runner.effects, ["restart"])

    async def test_prepare_refuses_while_target_lock_is_held(self):
        fd = os.open(self.state_dir / "target.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HostRuntimeError) as caught:
                self.plan()
            self.assertEqual(caught.exception.code, "TARGET_BUSY")
        finally:
            os.close(fd)
        self.assertEqual(self.adapter.preflight_calls, 0)
        self.plan()

    async def test_other_targets_use_a_compose_identity_lock(self):
        other = self.home / "otherApp"
        other.mkdir()
        target = copy.deepcopy(fixture_v3(other)["targets"][TARGET])
        commit_all(other)
        target["project"] = "otherApp"
        target["compose"]["project_name"] = "other-stack"
        self.payload["targets"]["other-test"] = target
        self.write_config()
        self.projects["otherApp"] = other
        self.ops = self.make_ops()
        lock = self.registered("locks", hashlib.sha256(b"other-stack").hexdigest() + ".lock")
        legacy = HostRuntimeOperations({PROJECT: self.root}, state_dir=self.state_dir)
        held = legacy._acquire_target_lock()
        try:
            other_plan = self.plan("other-1", project="otherApp", target="other-test")
        finally:
            os.close(held)
        self.ops.reject_web("otherApp", "other-test", operation_id=other_plan["operation_id"],
                            confirmation=other_plan["operation_id"])
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HostRuntimeError) as caught:
                self.plan("other-2", project="otherApp", target="other-test")
            self.assertEqual(caught.exception.code, "TARGET_BUSY")
            self.plan("legacy-ok")
        finally:
            os.close(fd)
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("cross", project=PROJECT, target="other-test")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")

    async def test_restart_marks_interrupted_execution_outcome_unknown(self):
        plan = self.plan()
        self.approve(plan)
        with patch.object(self.ops, "_run_operation", lambda _op, fd, *_: os.close(fd)):
            queued = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await self.finish()
        self.assertEqual(queued["state"], "queued")
        restarted = self.make_ops()
        result = restarted.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual((result["state"], result["phase"], result["code"]),
                         ("outcome_unknown", "RECOVER", "PROCESS_INTERRUPTED"))
        again = await restarted.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish(restarted)
        self.assertEqual(again["state"], "outcome_unknown")
        self.assertEqual(self.runner.effects, [])
        codes = [e["code"] for e in restarted.events(PROJECT, TARGET, plan["operation_id"],
                                                     limit=200)["events"]]
        self.assertEqual(codes[-1], "PROCESS_INTERRUPTED")

    async def test_dead_worker_in_this_process_is_recovered_as_outcome_unknown(self):
        plan = self.plan()
        self.approve(plan)
        with patch.object(self.ops, "_run_operation", lambda _op, fd, *_: os.close(fd)):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await self.finish()
        result = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual((result["state"], result["code"]),
                         ("outcome_unknown", "PROCESS_INTERRUPTED"))
        self.assertEqual(self.runner.effects, [])

    async def test_live_execution_in_another_process_is_not_marked_unknown(self):
        plan = self.plan()
        self.approve(plan)
        entered, release = threading.Event(), threading.Event()

        def paused(_operation_id, target_fd, *_):
            entered.set()
            release.wait(5)
            os.close(target_fd)

        try:
            with patch.object(self.ops, "_run_operation", paused):
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                other = self.make_ops()
                self.assertEqual(other.get(PROJECT, TARGET, plan["operation_id"])["state"],
                                 "queued")
                with self.assertRaises(HostRuntimeError) as caught:
                    self.plan("second", ops=other)
                self.assertEqual(caught.exception.code, "TARGET_BUSY")
        finally:
            release.set()
            await self.finish()
        self.assertEqual(other.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "outcome_unknown")

    async def test_drift_after_approval_is_stale_plan_with_zero_execute_calls(self):
        extra = self.home / "executor_extra.py"
        extra.write_text("VERSION = 1\n")
        main = self.root / "server/src/main.ts"

        def snapshot_file(plan):
            return self.registered("snapshots", plan["operation_id"], "source",
                                   "server/src/main.ts")

        cases = [
            ("source", lambda plan: main.write_text("export default 'drift';\n"),
             lambda: git(self.root, "checkout", "--", "server/src/main.ts")),
            ("snapshot", lambda plan: snapshot_file(plan).write_text("tampered\n"), None),
            ("unsafe live input", lambda plan: (self.root / "server/src/link.ts").symlink_to(extra),
             lambda: (self.root / "server/src/link.ts").unlink()),
            ("head", lambda plan: git(self.root, "commit", "-q", "--allow-empty", "-m", "x"),
             None),
            ("config", lambda plan: (self.payload["targets"][TARGET]["exec_profiles"]
                                     ["node-version"].update(timeout=31), self.write_config()),
             None),
            ("executor", lambda plan: extra.write_text("VERSION = 2\n"), None),
            ("identity", lambda plan: self.adapter.identity.update(db="d" * 64), None),
            ("verifications", lambda plan: self.adapter.verifications.append("EXTRA_CHECK"),
             None),
        ]
        for index, (label, drift, undo) in enumerate(cases):
            with self.subTest(drift=label):
                self.ops = self.make_ops()
                self.ops.executor_files.append(extra)
                plan = self.plan(f"drift-{index}")
                self.approve(plan)
                drift(plan)
                with self.assertRaises(HostRuntimeError) as caught:
                    await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(caught.exception.code, "STALE_PLAN")
                result = self.ops.get(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual((result["state"], result["code"]), ("rejected", "STALE_PLAN"))
                self.assertFalse(self.registered("receipts",
                                                 plan["operation_id"] + ".json").exists())
                self.assertEqual(self.adapter.execute_calls, 0)
                self.assertEqual(self.runner.effects, [])
                if undo:
                    undo()

    async def test_source_drift_before_approval_is_refused(self):
        plan = self.plan()
        (self.root / "server/src/main.ts").write_text("changed before approval\n")
        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(plan)
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "rejected")
        self.assertFalse(self.registered("receipts", plan["operation_id"] + ".json").exists())

    async def test_failure_classification_before_and_after_side_effects(self):
        cases = [
            ("before-error", {"fail_before": HostRuntimeError("COMPOSE_RENDER_FAILED")},
             "failed", "COMPOSE_RENDER_FAILED", []),
            ("before-stale", {"fail_before": HostRuntimeError("STALE_PLAN")},
             "rejected", "STALE_PLAN", []),
            ("before-crash", {"fail_before": RuntimeError("boom /secret/path")},
             "failed", "HOST_OPERATION_FAILED", []),
            ("before-overflow", {"fail_before": HostCommandError("HOST_OUTPUT_TOO_LARGE")},
             "failed", "HOST_OUTPUT_TOO_LARGE", []),
            ("after-error", {"fail_after": HostRuntimeError("HOST_COMMAND_TIMEOUT")},
             "outcome_unknown", "HOST_COMMAND_TIMEOUT", ["restart"]),
            ("after-crash", {"fail_after": RuntimeError("boom")},
             "outcome_unknown", "HOST_OPERATION_FAILED", ["restart"]),
            ("unverified", {"verified": False}, "outcome_unknown", "POSTCHECK_UNVERIFIED",
             ["restart"]),
            ("postcheck-error", {"postcheck_error": HostRuntimeError("HOST_COMMAND_FAILED")},
             "outcome_unknown", "HOST_COMMAND_FAILED", ["restart"]),
        ]
        for index, (label, settings, state, code, effects) in enumerate(cases):
            with self.subTest(case=label):
                self.runner.effects.clear()
                self.adapter = FakeAdapter(self.runner.run)
                for key, value in settings.items():
                    setattr(self.adapter, key, value)
                _, _, done = await self.run_approved(f"case-{index}")
                self.assertEqual((done["state"], done["code"]), (state, code), done)
                self.assertEqual(self.runner.effects, effects)
                self.assertIsNotNone(done["finished_at"])
                self.assertIsNone(done["result"])
                if code == "HOST_OPERATION_FAILED":
                    self.assertTrue(HEX32.match(done["diagnostic_id"]))
                    self.assertNotIn("secret", json.dumps(done))
        self.runner.effects.clear()
        self.adapter = FakeAdapter(self.runner.run)
        self.runner.fail_phase("RESTART")
        _, _, done = await self.run_approved("runner-failure")
        self.assertEqual((done["state"], done["code"]), ("outcome_unknown", "HOST_COMMAND_FAILED"))
        self.assertEqual(self.runner.effects, [])

    async def test_prepare_runs_no_mutation_and_policy_errors_leave_no_state(self):
        self.adapter.mutate_in_preflight = True
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "PREFLIGHT_MUTATION")
        self.adapter.mutate_in_preflight = False
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.prepare(PROJECT, TARGET, action="compose.down", parameters={},
                             request_id="down-1")
        self.assertEqual(caught.exception.code, "POLICY_DENIED")
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("unknown", services=("nginx",))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")
        (self.root / "deploy/pc-test/Dockerfile").write_text("RUN unreviewed\n")
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("unreviewed")
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
        self.assertEqual(list(self.registered("operations").glob("*.json")), [])
        self.assertEqual(list(self.registered("snapshots").iterdir()), [])
        self.payload["enabled"] = False
        self.write_config()
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("disabled", ops=self.make_ops())
        self.assertEqual(caught.exception.code, "HOST_RUNTIME_DISABLED")
        self.assertEqual(self.runner.effects, [])

    async def test_reviewed_snapshot_plan_exposes_paths_and_digests_only(self):
        self.payload["targets"][TARGET]["source_review"] = "reviewed_snapshot"
        self.write_config()
        self.ops = self.make_ops()
        (self.root / "deploy/pc-test/Dockerfile").write_text("FROM node:22\nRUN reviewed\n")
        plan = self.plan()
        self.assertEqual(plan["source"]["high_risk_changes"], ["deploy/pc-test/Dockerfile"])
        self.assertTrue(HEX64.match(plan["source"]["review_digest"]))
        self.assertNotIn("RUN reviewed", json.dumps(plan))
        review = self.ops.review_artifact(PROJECT, TARGET, plan["operation_id"])
        self.assertIn("+RUN reviewed", review["files"][0]["diff"])
        self.approve(plan)
        review_file = self.registered("snapshots", plan["operation_id"], "review.json")
        review_file.write_text(review_file.read_text().replace("reviewed", "reviewee"))
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        self.assertEqual(self.runner.effects, [])

    async def test_events_are_lossless_bounded_and_whitelisted(self):
        long_path = "server/" + "a" * 500
        self.adapter.extra_events = [
            ("PRECHECK", "PROGRESS", {"count": 1, "stdout": "secret output", "sql": "SELECT 1",
                                      "url": "postgres://u:p@h/db", "sqlstate": "42P01",
                                      "artifact": "https://example.invalid/x",
                                      "services": ["api", "nginx"], "verified": "yes",
                                      "env": {"PGPASSWORD": "pw"}, "rows": -1, "bytes": True}),
            ("PRECHECK", "ARTIFACT", {"artifact": "server/migrations/001_foundation.sql",
                                      "service": "api", "services": ["api", "db"],
                                      "image_id": "sha256:" + "e" * 64}),
            ("PRECHECK", "ESCAPE", {"artifact": "../etc/passwd"}),
            *[("PRECHECK", "DATA", {"artifact": bad}) for bad in (
                "server/a b.sql", "x=1;DROP TABLE t", "'quoted'", "/abs/path.sql",
                "a/../b.sql", "rows\n1", "select*from")],
            ("NOT_A_PHASE", "X", {}),
            ("PRECHECK", "lower-case", {}),
            *[("PRECHECK", "BULK", {"count": i, "artifact": long_path}) for i in range(260)],
        ]
        plan, _, done = await self.run_approved()
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(self.adapter.emit_errors, ["INVALID_EVENT", "INVALID_EVENT"])
        first = self.ops.events(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(len(first["events"]), 50)
        self.assertTrue(first["more"])
        self.assertEqual(first["next_cursor"], first["events"][-1]["cursor"])
        by_code = {e["code"]: e for e in first["events"]}
        self.assertEqual(by_code["PROGRESS"]["fields"], {"count": 1, "sqlstate": "42P01"})
        self.assertEqual(by_code["ARTIFACT"]["fields"],
                         {"artifact": "server/migrations/001_foundation.sql", "service": "api",
                          "services": ["api", "db"], "image_id": "sha256:" + "e" * 64})
        self.assertEqual(by_code["ESCAPE"]["fields"], {})
        data_events = [e for e in first["events"] if e["code"] == "DATA"]
        self.assertEqual([e["fields"] for e in data_events], [{}] * 7)
        for event in first["events"]:
            self.assertEqual(set(event), {"cursor", "at", "phase", "code", "fields"})
        cursors, cursor, pages = [], 0, 0
        while True:
            page = self.ops.events(PROJECT, TARGET, plan["operation_id"], cursor=cursor,
                                   limit=200)
            pages += 1
            self.assertLessEqual(len(json.dumps(page).encode()), 64 * 1024)
            self.assertLessEqual(len(page["events"]), 200)
            cursors += [e["cursor"] for e in page["events"]]
            cursor = page["next_cursor"]
            if not page["more"]:
                break
        self.assertGreater(pages, 2)
        self.assertEqual(cursors, list(range(1, len(cursors) + 1)))
        codes = [e["code"] for e in self.make_ops().events(
            PROJECT, TARGET, plan["operation_id"], cursor=0, limit=200)["events"]]
        self.assertEqual(codes[:3], ["PLAN_CREATED", "HOST_APPROVED", "OPERATION_QUEUED"])
        tail = self.ops.events(PROJECT, TARGET, plan["operation_id"], cursor=cursors[-6],
                               limit=200)["events"]
        self.assertEqual([e["code"] for e in tail][-4:],
                         ["COMPOSE_RESTART", "POSTCHECK_STARTED", "POSTCHECK_VERIFIED",
                          "OPERATION_SUCCEEDED"])
        self.assertEqual([e["phase"] for e in tail][-4:],
                         ["EXECUTE", "VERIFY", "VERIFY", "COMPLETE"])
        empty = self.ops.events(PROJECT, TARGET, plan["operation_id"], cursor=cursors[-1])
        self.assertEqual((empty["events"], empty["more"], empty["next_cursor"]),
                         ([], False, cursors[-1]))
        for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"cursor": -1},
                       {"cursor": "1"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HostRuntimeError) as caught:
                    self.ops.events(PROJECT, TARGET, plan["operation_id"], **kwargs)
                self.assertEqual(caught.exception.code, "INVALID_EVENT_QUERY")
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.events(PROJECT, TARGET, "0" * 32)
        self.assertEqual(caught.exception.code, "OPERATION_NOT_FOUND")
        raw = b"".join(p.read_bytes() for p in self.registered().rglob("*.json"))
        for secret in (b"secret output", b"SELECT 1", b"postgres://", b"PGPASSWORD", b"pw"):
            self.assertNotIn(secret, raw)

    async def test_get_for_adapter_and_pending_are_verified_views(self):
        plan = self.plan()
        listed = self.ops.pending(PROJECT)
        self.assertEqual([p["operation_id"] for p in listed["plans"]], [plan["operation_id"]])
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.pending("otherProject")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")
        internal = self.ops.get_for_adapter(plan["operation_id"])
        self.assertEqual(internal["immutable"]["action"], "compose.restart")
        self.assertNotIn("nonce", internal["immutable"])
        self.assertEqual(Path(internal["snapshot"]),
                         self.registered("snapshots", plan["operation_id"], "source"))
        self.approve(plan)
        self.assertEqual(self.ops.pending(PROJECT)["plans"], [])
        path = self.registered("operations", plan["operation_id"] + ".json")
        state = json.loads(path.read_text())
        state["immutable"]["parameters"] = {"services": ["db"]}
        path.write_text(json.dumps(state))
        for call in (lambda: self.ops.get_for_adapter(plan["operation_id"]),
                     lambda: self.ops.get(PROJECT, TARGET, plan["operation_id"])):
            with self.assertRaises(HostRuntimeError) as caught:
                call()
            self.assertEqual(caught.exception.code, "PLAN_INVALID")
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "PLAN_INVALID")
        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.get(PROJECT, TARGET, "not-an-id")
        self.assertEqual(caught.exception.code, "INVALID_OPERATION_ID")
        self.assertEqual(self.runner.effects, [])

    async def test_trust_needs_both_injected_values_or_verified_install(self):
        (self.root / "approve-host-operation.py").write_text("print('approved')\n")
        plan = self.plan()
        for label, overrides in (("factory only", {"runner": None}),
                                 ("runner only", {"adapter_factory": None}),
                                 ("neither", {"runner": None, "adapter_factory": None}),
                                 ("real runner", {"runner": run_registered_command})):
            with self.subTest(label=label):
                untrusted = self.make_ops(**overrides)
                calls = [
                    lambda: self.plan("untrusted", ops=untrusted),
                    lambda: self.approve(plan, ops=untrusted),
                    lambda: untrusted.reject_web(PROJECT, TARGET,
                                                 operation_id=plan["operation_id"],
                                                 confirmation=plan["operation_id"]),
                    lambda: untrusted.pending(PROJECT),
                    lambda: untrusted.get_for_adapter(plan["operation_id"]),
                    lambda: untrusted.review_artifact(PROJECT, TARGET, plan["operation_id"]),
                ]
                for call in calls:
                    with self.assertRaises(HostRuntimeError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, "UNSAFE_RUNTIME")
                with self.assertRaises(HostRuntimeError) as caught:
                    await untrusted.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(caught.exception.code, "UNSAFE_RUNTIME")
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")
        self.assertEqual(self.runner.effects, [])

    def lock_fds(self):
        target = str(self.state_dir / "target.lock")
        count = 0
        for name in os.listdir("/proc/self/fd"):
            try:
                count += os.readlink(f"/proc/self/fd/{name}") == target
            except OSError:
                pass
        return count

    def wait_state(self, operation_id, states, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            current = self.ops.get(PROJECT, TARGET, operation_id)
            if current["state"] in states or time.monotonic() > deadline:
                return current
            time.sleep(0.05)

    async def test_cancelled_execute_still_owns_and_releases_the_target_lock(self):
        plan = self.plan()
        self.approve(plan)
        entered, release = threading.Event(), threading.Event()
        self.adapter.on_preflight = lambda: (entered.set(), release.wait(5))
        task = asyncio.create_task(self.ops.execute(PROJECT, TARGET, plan["operation_id"]))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.adapter.on_preflight = None
        release.set()
        done = await asyncio.to_thread(self.wait_state, plan["operation_id"],
                                       ("succeeded", "failed", "outcome_unknown", "rejected"))
        self.assertEqual(done["state"], "succeeded", done)
        await self.finish()
        self.assertEqual(self.runner.effects, ["restart"])
        self.assertEqual(self.lock_fds(), 0, "target.lock descriptor leaked")
        legacy = HostRuntimeOperations(self.projects, state_dir=self.state_dir)
        self.assertFalse(legacy._target_lock_busy())
        again = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(again["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["restart"])
        self.assertEqual(self.adapter.execute_calls, 1)

    async def test_postcheck_failure_after_undeclared_execute_is_outcome_unknown(self):
        cases = [
            ("control", {}, "succeeded", None),
            ("unverified", {"verified": False}, "outcome_unknown", "POSTCHECK_UNVERIFIED"),
            ("stale", {"postcheck_error": HostRuntimeError("STALE_PLAN")},
             "outcome_unknown", "STALE_PLAN"),
            ("crash", {"postcheck_error": RuntimeError("boom")},
             "outcome_unknown", "HOST_OPERATION_FAILED"),
            ("missing check", {"reported_verifications": ["SERVICES_RUNNING"]},
             "outcome_unknown", "POSTCHECK_UNVERIFIED"),
        ]
        for index, (label, settings, state, code) in enumerate(cases):
            with self.subTest(case=label):
                self.runner.effects.clear()
                self.adapter = FakeAdapter(self.runner.run)
                self.adapter.declare_side_effects = False
                for key, value in settings.items():
                    setattr(self.adapter, key, value)
                _, _, done = await self.run_approved(f"undeclared-{index}")
                self.assertEqual((done["state"], done["code"]), (state, code), done)
                if state != "succeeded":
                    self.assertEqual(done["phase"], "VERIFY")
                self.assertEqual(self.runner.effects, ["restart"])

    async def test_lock_and_state_helpers_are_shared_with_legacy(self):
        self.assertIs(host_registered_operations.acquire_flock,
                      host_runtime_operations.acquire_flock)
        self.assertIs(host_registered_operations.lock_busy, host_runtime_operations.lock_busy)
        self.assertIs(host_registered_operations.operation_path,
                      host_runtime_operations.operation_path)
        lock = self.state_dir / "target.lock"
        os.close(os.open(lock, os.O_CREAT | os.O_RDWR, 0o600))
        lock.chmod(0o644)
        legacy = HostRuntimeOperations(self.projects, state_dir=self.state_dir)
        for call in (legacy._acquire_target_lock, self.plan):
            with self.assertRaises(HostRuntimeError) as caught:
                call()
            self.assertEqual(caught.exception.code, "UNSAFE_STATE_FILE")
        lock.chmod(0o600)
        fake_home = self.home / "home"
        fake_home.mkdir()
        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            legacy_default = HostRuntimeOperations(self.projects).state_dir
            registered_default = self.make_ops(state_dir=None).state_dir
            self.assertEqual(host_runtime_operations.default_state_dir(), legacy_default)
        self.assertEqual(registered_default, legacy_default)

    async def test_preflight_must_declare_identity_and_verification_items(self):
        plan = self.plan()
        private = json.loads(self.registered("operations", plan["operation_id"] + ".json")
                             .read_text())["immutable"]
        self.assertEqual(private["verifications"],
                         ["SERVICES_RUNNING", "PROTECTED_IDENTITY_UNCHANGED"])
        self.assertEqual(private["preflight"], {"containers": self.adapter.identity,
                                                "services": ["api"]})
        self.ops.reject_web(PROJECT, TARGET, operation_id=plan["operation_id"],
                            confirmation=plan["operation_id"])
        for index, shape in enumerate((
                {"containers": {}},
                {"identity": {}, "verifications": []},
                {"identity": {}, "verifications": ["lower_case"]},
                {"identity": [], "verifications": ["OK"]},
                {"identity": {}, "verifications": ["SAME", "SAME"]},
                {"identity": {}, "verifications": ["OK"], "extra": 1},
                {"identity": {}, "verifications": ["C%d" % n for n in range(33)]})):
            with self.subTest(shape=shape):
                self.adapter.preflight = lambda target, spec, context, shape=shape: shape
                with self.assertRaises(HostRuntimeError) as caught:
                    self.plan(f"shape-{index}")
                self.assertEqual(caught.exception.code, "ADAPTER_RESPONSE_INVALID")
        self.assertEqual(len(list(self.registered("operations").glob("*.json"))), 1)

    async def test_unexpected_adapter_errors_are_sanitized_everywhere(self):
        secret = "boom /secret/adapter/path"

        def check(error):
            self.assertEqual(error.code, "HOST_OPERATION_FAILED")
            self.assertTrue(HEX32.match(error.diagnostic_id))
            self.assertNotIn("secret", str(error) + repr(error.args))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)

        self.adapter.preflight_error = RuntimeError(secret)
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("crash-prepare")
        check(caught.exception)
        self.assertEqual(list(self.registered("operations").glob("*.json")), [])
        self.adapter.preflight_error = None
        plan = self.plan("crash-approve")
        self.adapter.preflight_error = RuntimeError(secret)
        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(plan)
        check(caught.exception)
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")
        self.adapter.preflight_error = None
        self.approve(plan)
        self.adapter.preflight_error = RuntimeError(secret)
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        check(caught.exception)
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "approved")
        self.assertTrue(self.registered("receipts", plan["operation_id"] + ".json").exists())
        self.assertEqual(self.adapter.execute_calls, 0)

        def broken_factory(target, action, runner):
            raise RuntimeError(secret)

        broken = self.make_ops(adapter_factory=broken_factory)
        self.adapter.preflight_error = None
        with self.assertRaises(HostRuntimeError) as caught:
            await broken.execute(PROJECT, TARGET, plan["operation_id"])
        check(caught.exception)
        self.assertEqual(self.runner.effects, [])

    async def test_host_errors_during_verification_do_not_reject_the_plan(self):
        plan = self.plan()
        self.approve(plan)
        failing = HostRuntimeError("HOST_OUTPUT_TOO_LARGE", "EXECUTE")
        for name in ("git_state", "verify_reviewed_high_risk_inputs"):
            with self.subTest(failing=name):
                with patch("host_registered_operations." + name, side_effect=failing):
                    with self.assertRaises(HostRuntimeError) as caught:
                        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(caught.exception.code, "HOST_OUTPUT_TOO_LARGE")
                self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                                 "approved")
                self.assertTrue(self.registered("receipts", plan["operation_id"] + ".json")
                                .exists())
                self.assertEqual(self.adapter.execute_calls, 0)
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["restart"])

    async def test_unsafe_review_artifact_is_not_reported_as_drift(self):
        self.payload["targets"][TARGET]["source_review"] = "reviewed_snapshot"
        self.write_config()
        self.ops = self.make_ops()
        (self.root / "deploy/pc-test/Dockerfile").write_text("FROM node:22\nRUN reviewed\n")
        plan = self.plan()
        self.approve(plan)
        review = self.registered("snapshots", plan["operation_id"], "review.json")
        review.chmod(0o644)
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "UNSAFE_STATE_FILE")
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "approved")
        review.chmod(0o600)
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await self.finish()
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "succeeded")

    async def test_adapter_event_cap_records_one_limit_marker(self):
        self.adapter.extra_events = [("PRECHECK", "BULK", {"count": n}) for n in range(10)]
        with patch("host_registered_operations.MAX_ADAPTER_EVENTS", 4):
            plan, _, done = await self.run_approved()
        self.assertEqual(done["state"], "succeeded")
        codes = [e["code"] for e in self.ops.events(PROJECT, TARGET, plan["operation_id"],
                                                    limit=200)["events"]]
        self.assertEqual(codes.count("EVENTS_LIMIT_REACHED"), 1)
        self.assertEqual(codes.count("BULK"), 3)
        self.assertNotIn("COMPOSE_RESTART", codes)
        self.assertEqual(codes[-2:], ["POSTCHECK_VERIFIED", "OPERATION_SUCCEEDED"])
        self.assertEqual(done["phase"], "COMPLETE")

    def assertCodedWithoutPath(self, error, code):
        self.assertIsInstance(error, HostRuntimeError)
        self.assertEqual(error.code, code)
        self.assertNotIn(str(self.home), str(error) + repr(error.args))
        self.assertTrue(error.__cause__ is None and error.__suppress_context__)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    async def test_unreadable_inputs_are_coded_without_paths(self):
        main = self.root / "server/src/main.ts"
        self.addCleanup(main.chmod, 0o644)
        first = self.plan("unreadable-approve")
        main.chmod(0)
        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(first)
        self.assertCodedWithoutPath(caught.exception, "STALE_PLAN")
        main.chmod(0o644)
        second = self.plan("unreadable-execute")
        self.approve(second)
        main.chmod(0)
        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, second["operation_id"])
        self.assertCodedWithoutPath(caught.exception, "STALE_PLAN")
        self.assertEqual(self.ops.get(PROJECT, TARGET, second["operation_id"])["state"],
                         "rejected")
        main.chmod(0o644)
        third = self.plan("vanished-high-risk")
        self.approve(third)
        vanished = FileNotFoundError(2, "No such file", str(self.root / "deploy/pc-test/run.mjs"))
        with patch("host_registered_operations.verify_reviewed_high_risk_inputs",
                   side_effect=vanished):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, third["operation_id"])
        self.assertCodedWithoutPath(caught.exception, "STALE_PLAN")
        main.chmod(0)
        snapshots = sorted(self.registered("snapshots").iterdir())
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("unreadable-prepare")
        self.assertCodedWithoutPath(caught.exception, "SOURCE_UNREADABLE")
        self.assertEqual(sorted(self.registered("snapshots").iterdir()), snapshots)
        self.assertEqual(len(list(self.registered("operations").glob("*.json"))), 3)
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.adapter.execute_calls, 0)

    async def test_residual_os_errors_never_reach_public_callers(self):
        plan = self.plan()
        leak = PermissionError(13, "Permission denied", str(self.home / "secret-state"))
        calls = {
            "get": lambda: self.ops.get(PROJECT, TARGET, plan["operation_id"]),
            "events": lambda: self.ops.events(PROJECT, TARGET, plan["operation_id"]),
            "pending": lambda: self.ops.pending(PROJECT),
            "get_for_adapter": lambda: self.ops.get_for_adapter(plan["operation_id"]),
            "review_artifact": lambda: self.ops.review_artifact(PROJECT, TARGET,
                                                                plan["operation_id"]),
            "approve": lambda: self.approve(plan),
            "reject": lambda: self.ops.reject_web(PROJECT, TARGET,
                                                  operation_id=plan["operation_id"],
                                                  confirmation=plan["operation_id"]),
            "prepare": lambda: self.plan("other-request"),
        }
        with patch("host_registered_operations._read_private_json", side_effect=leak):
            for name, call in calls.items():
                with self.subTest(call=name):
                    with self.assertRaises(HostRuntimeError) as caught:
                        call()
                    self.assertCodedWithoutPath(caught.exception, "STATE_UNAVAILABLE")
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            self.assertCodedWithoutPath(caught.exception, "STATE_UNAVAILABLE")
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")

    async def test_adapter_codes_are_validated_and_diagnostics_recorded(self):
        self.adapter.preflight_error = HostRuntimeError("bad code " + str(self.home))
        with self.assertRaises(HostRuntimeError) as caught:
            self.plan("bad-code-prepare")
        self.assertCodedWithoutPath(caught.exception, "HOST_OPERATION_FAILED")
        prepare_id = caught.exception.diagnostic_id
        self.adapter.preflight_error = None
        for index, error in enumerate((HostRuntimeError("lower_case"), HostRuntimeError("AB"),
                                       HostRuntimeError("X" * 65), RuntimeError("boom"))):
            with self.subTest(error=repr(error)[:40]):
                self.adapter = FakeAdapter(self.runner.run)
                self.adapter.fail_before = error
                plan, _, done = await self.run_approved(f"bad-code-{index}")
                self.assertEqual((done["state"], done["code"]), ("failed", "HOST_OPERATION_FAILED"))
                self.assertTrue(HEX32.match(done["diagnostic_id"]))
                last = self.ops.events(PROJECT, TARGET, plan["operation_id"],
                                       limit=200)["events"][-1]
                self.assertEqual(last["fields"], {"error_code": "HOST_OPERATION_FAILED",
                                                  "diagnostic_id": done["diagnostic_id"]})
        diagnostics = self.registered("diagnostics.json")
        self.assertEqual(stat.S_IMODE(diagnostics.stat().st_mode), 0o600)
        entries = json.loads(diagnostics.read_text())["entries"]
        by_id = {entry["diagnostic_id"]: entry for entry in entries}
        self.assertEqual(by_id[prepare_id]["stage"], "prepare")
        self.assertEqual(by_id[prepare_id]["exception"], "host_runtime_config.HostRuntimeError")
        self.assertEqual(by_id[done["diagnostic_id"]]["exception"], "builtins.RuntimeError")
        self.assertEqual(by_id[done["diagnostic_id"]]["operation_id"], plan["operation_id"])
        for entry in entries:
            self.assertEqual(set(entry), {"diagnostic_id", "at", "stage", "operation_id",
                                          "exception"})
        raw = diagnostics.read_text()
        for secret in ("boom", "bad code", str(self.home), "lower_case"):
            self.assertNotIn(secret, raw)

    async def test_state_version_and_exact_keys_are_enforced(self):
        plan = self.plan()
        path = self.registered("operations", plan["operation_id"] + ".json")
        original = json.loads(path.read_text())
        self.assertEqual((original["version"], original["immutable"]["version"]), (2, 2))

        def rewrite(mutate):
            state = copy.deepcopy(original)
            mutate(state)
            state["plan_digest"] = host_runtime_operations._digest(state["immutable"])
            path.write_text(json.dumps(state))

        for label, mutate in (
                ("old version", lambda s: s.update(version=1)),
                ("old immutable", lambda s: s["immutable"].update(version=1)),
                ("missing key", lambda s: s.pop("request_id")),
                ("extra key", lambda s: s.update(extra=1)),
                ("missing verifications", lambda s: s["immutable"].pop("verifications")),
                ("bad source", lambda s: s["immutable"]["source"].pop("git_head"))):
            with self.subTest(label=label):
                rewrite(mutate)
                with self.assertRaises(HostRuntimeError) as caught:
                    self.ops.get(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(caught.exception.code, "PLAN_INVALID")
        path.write_text(json.dumps(original))
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")

    async def test_state_dir_default_and_validation(self):
        with self.assertRaises(HostRuntimeError) as caught:
            self.make_ops(state_dir=self.root / ".host-state")
        self.assertEqual(caught.exception.code, "UNSAFE_STATE_PATH")
        fake_home = self.home / "home"
        fake_home.mkdir()
        default_env = {key: value for key, value in os.environ.items() if key != "XDG_STATE_HOME"}
        default_env["HOME"] = str(fake_home)
        with patch.dict(os.environ, default_env, clear=True):
            ops = self.make_ops(state_dir=None)
        expected = fake_home / ".local/state/chatgpt-coding-mcp/host-runtime"
        self.assertEqual(ops.state_dir, expected)
        self.assertTrue((expected / "registered").is_dir())
        self.assertEqual(stat.S_IMODE((expected / "registered").stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
