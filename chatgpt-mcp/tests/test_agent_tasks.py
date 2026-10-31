"""Approved agent task C1-C3 tests. Temporary git fixture, synthetic CLI, no model."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import agent_tasks
import agent_runner
from agent_profiles import (
    AgentProfileRegistry,
    validate_agent_profiles,
)
from agent_tasks import AgentTaskManager
from agent_events import parse_codex_event
from agent_runner import CodexAgentRunner, compile_codex_profile
from agent_host_broker import AgentHostBroker
from host_runtime_config import HostRuntimeError
from host_registry import HostTargetRegistry
from tests.test_agent_supervised_runner import FixtureSupervisor


class SyntheticRunner:
    def __init__(self, *, change=True, verified=True):
        self.calls = 0
        self.tasks = []
        self.change = change
        self.verified = verified

    def ready(self, profile):
        return True

    async def run(self, task, workspace, profile):
        self.calls += 1
        self.tasks.append(task)
        if self.change:
            (workspace / "src/demo.py").write_text("changed by fake agent\n")
        return {"verified": self.verified, "changed_files": ["src/demo.py"], "events": []}


class FailingRunner(SyntheticRunner):
    async def run(self, task, workspace, profile):
        self.calls += 1
        raise HostRuntimeError("AGENT_EVENT_TOO_LARGE")


class CancelReturnsRunner(SyntheticRunner):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.released = asyncio.Event()

    async def run(self, task, workspace, profile):
        self.calls += 1
        (workspace / "src/demo.py").write_text("must not be applied after cancel\n")
        self.started.set()
        await self.released.wait()
        return {"verified": True, "changed_files": ["src/demo.py"], "events": []}

    async def cancel(self, task_id):
        self.released.set()


class UnverifiedCancelRunner(CancelReturnsRunner):
    async def cancel(self, task_id):
        raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")


class VerifiedCancelRunner(CancelReturnsRunner):
    async def cancel(self, task_id):
        self.released.set()
        return {"termination_verified": True}


class ObservableRunner(SyntheticRunner):
    def __init__(self):
        super().__init__()
        self.callbacks = {}

    def set_task_callbacks(self, task_id, *, on_identity=None, on_event=None):
        self.callbacks[task_id] = (on_identity, on_event)

    async def run(self, task, workspace, profile):
        self.tasks.append(task)
        identity, event = self.callbacks[task["task_id"]]
        unit = "personal-ops-agent-" + "a" * 32 + ".service"
        identity({
            "task_id": task["task_id"], "unit": unit,
            "invocation_id": "b" * 32,
            "cgroup": (f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                       "app.slice/" + unit),
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "uid": os.getuid(), "pid": 123, "pgid": 123, "start_ticks": 456,
            "wrapper_pid": 122, "wrapper_start_ticks": 455,
            "broker_context_nonce": "c" * 64,
            "command": "secret command",
        })
        event({"phase": "WORKING", "code": "COMMAND_ACTIVITY",
               "prompt": "secret prompt"})
        event({"phase": "COMPLETED", "code": "TURN_COMPLETED",
               "usage": {"input_tokens": 11, "output_tokens": 7},
               "usage_scope": "turn", "model": "gpt-5.6",
               "model_source": "event"})
        (workspace / "src/demo.py").write_text("changed by observable runner\n")
        return {
            "verified": True,
            "structured_result": {
                "summary": "Reviewed the source",
                "changed_files": ["src/demo.py"],
                "validations": ["Static review only"],
                "remaining": ["Run the real test suite"],
                "approval_required": True,
            },
            "events": [{"raw": "must not be trusted"}],
        }


class AgentTaskTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-task-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src/demo.py").write_text("original\n")
        (self.root / "src/second.py").write_text("second\n")
        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_CONFIG_GLOBAL": "/dev/null"})
        git("init", "-q")
        git("add", "src/demo.py", "src/second.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "baseline")
        runtime = self.home / "runtime"
        runtime.mkdir(mode=0o700)
        from agent_bootstrap import build_bootstrap
        build_bootstrap(runtime / 'personal-ops-agent-bootstrap')
        code_host = runtime / "codex-code-mode-host"
        shutil.copy2(runtime / 'personal-ops-agent-bootstrap', code_host)
        self.code_host_sha = hashlib.sha256(code_host.read_bytes()).hexdigest()
        code_host_pin = mock.patch('agent_probe.PINNED_CODE_MODE_HOST_SHA256',
                                   self.code_host_sha)
        code_host_pin.start()
        self.addCleanup(code_host_pin.stop)
        binary = runtime / "codex"
        shutil.copy2(Path(sys.executable).resolve(), binary)
        shutil.copy2(Path(sys.executable).resolve(), runtime / "codex-linux-sandbox")
        self.profile = {
            "project": "testrepo", "provider": "codex",
            "executable": str(binary),
            "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "version": "0.162.1", "model": "gpt-5.6",
            "reasoning_effort": "high", "allowed_paths": ["src"],
            "host_capabilities": [], "apply_policy": "review_only",
            "isolation_receipt": "a" * 64, "smoke_receipt": "b" * 64,
            "enabled": True
        }
        self.config = self.home / "config.json"
        self.receipts = self.home / "agent-probes"
        self.receipts.mkdir(mode=0o700)
        from agent_proofs import record_receipt, SUPERVISION_ASSERTIONS, SMOKE_ASSERTIONS
        from agent_probe import REQUIRED_ASSERTIONS
        # Disposable evidence fixture: production setup must run actual probes.
        self.profile['isolation_receipt'] = record_receipt(self.receipts, self.profile,
            'isolation', {'assertions': {name: True for name in REQUIRED_ASSERTIONS},
                          'supervision': {name: True for name in SUPERVISION_ASSERTIONS}})
        self.profile['smoke_receipt'] = record_receipt(self.receipts, self.profile,
            'smoke', {name: True for name in SMOKE_ASSERTIONS})
        self.write_config()
        self.registry = AgentProfileRegistry(
            {"testrepo": self.root}, config_path=self.config,
            receipts_dir=self.receipts)
        self.runner = SyntheticRunner()
        self.manager = AgentTaskManager(
            {"testrepo": self.root}, self.registry,
            state_dir=self.home / "agent-tasks", runner=self.runner,
            trusted_runtime=True)

    def write_config(self):
        payload = {"version": 3, "enabled": True, "legacy": None,
                   "targets": {}, "agent_profiles": {"codex-repo": self.profile}}
        self.config.write_text(json.dumps(payload))
        self.config.chmod(0o600)

    def prepare(self, ident="task-one", **overrides):
        return self.manager.prepare(
            "testrepo", profile_id="codex-repo",
            objective="Review one synthetic source file only",
            reason="WORKFLOW_COMPLEXITY", request_id=ident, **overrides)

    async def test_active_workspace_denies_unsupported_isolated_agent_before_snapshot_or_runner(self):
        from workspace_tools import WorkspaceTools
        policy_dir = self.home / "workspace-owner"
        policy_dir.mkdir(mode=0o700)
        config = policy_dir / "workspace-policy.json"
        config.write_text(json.dumps({"version": 1, "base_root": str(self.root.parent), "artifacts": []}))
        config.chmod(0o600)
        workspace = WorkspaceTools({"testrepo": self.root}, policy_dir / "authority", config_path=config)
        workspace.authority.initialize(workspace.policy.load(required=True).digest, {"a" * 64: 1})
        old = self.prepare("before-activation")
        self.approve(old)
        self.manager.workspace = workspace
        with self.assertRaisesRegex(HostRuntimeError, "WORKSPACE_ISOLATED_AGENT_UNSUPPORTED"):
            self.prepare("after-activation")
        self.assertEqual(len(list(self.manager.sources_dir.iterdir())), 1)
        with self.assertRaisesRegex(HostRuntimeError, "WORKSPACE_ISOLATED_AGENT_UNSUPPORTED"):
            await self.manager.start("testrepo", old["task_id"])
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")

    def approve(self, item):
        return self.manager.approve_web(
            "testrepo", task_id=item["task_id"],
            plan_digest=item["plan_digest"],
            source_digest=item["source_digest"],
            confirmation=item["task_id"])

    def test_pending_task_can_be_approved_six_days_after_creation(self):
        item = self.prepare("six-day-approval")
        created = datetime.fromisoformat(item["created_at"])
        real_datetime = agent_tasks.datetime

        class SixDaysLater(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return created + timedelta(days=6)

        with mock.patch.object(agent_tasks, "datetime", SixDaysLater):
            approved = self.approve(item)
        self.assertTrue(approved["approved"])
        self.assertEqual(self.manager.get("testrepo", item["task_id"])["state"], "approved")

    def test_pending_task_expires_exactly_seven_days_after_creation(self):
        item = self.prepare("seven-day-expiry")
        expires = datetime.fromisoformat(item["expires_at"])
        real_datetime = agent_tasks.datetime

        class SevenDaysLater(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return expires

        with mock.patch.object(agent_tasks, "datetime", SevenDaysLater):
            with self.assertRaises(HostRuntimeError) as caught:
                self.approve(item)
        self.assertEqual(caught.exception.code, "PLAN_EXPIRED")
        self.assertFalse((self.manager.receipts_dir / (item["task_id"] + ".json")).exists())

    async def wait_result(self, task_id):
        for _ in range(100):
            result = self.manager.get("testrepo", task_id)
            if result["state"] in {"succeeded", "review_required", "outcome_unknown"}:
                return result
            await asyncio.sleep(.01)
        self.fail("Synthetic runner did not terminate")

    def test_registry_validates_exact_profiles_and_rejects_unsafe(self):
        validate_agent_profiles({"codex-repo": self.profile}, {"testrepo": self.root})
        self.assertEqual(HostTargetRegistry.from_payload(json.loads(self.config.read_text()),
                     {"testrepo": self.root}).availability()["enabled"], True)
        for patch in (
            {"host_capabilities": ["compose.exec"]},
            {"allowed_paths": ["../other"]},
            {"allowed_paths": [".env"]},
            {"allowed_paths": ["src", "src/child"]},
            {"allowed_paths": [".ssh"]},
            {"allowed_paths": ["src/.aws"]},
            {"allowed_paths": ["src/.claude"]},
            {"allowed_paths": ["src/.config"]},
            {"provider": "claude"},
            {"apply_policy": "unrestricted"}
        ):
            with self.subTest(patch=patch):
                profile = dict(self.profile, **patch)
                with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_CONFIG"):
                    validate_agent_profiles({"codex-repo": profile}, {"testrepo": self.root})

    def test_registry_requires_two_independent_receipts(self):
        self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["ready"], True)
        (self.receipts / (self.profile['smoke_receipt'] + '.json')).unlink()
        self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                         "SMOKE_UNVERIFIED")
        with self.assertRaisesRegex(HostRuntimeError, "SMOKE_UNVERIFIED"):
            self.prepare()

    def test_registry_default_requires_external_process_supervision_proof(self):
        path = self.receipts / (self.profile['isolation_receipt'] + '.json')
        path.write_text(json.dumps({'passed': True, 'supervision_verified': True}))
        path.chmod(0o600)
        registry = AgentProfileRegistry(
            {"testrepo": self.root}, config_path=self.config, receipts_dir=self.receipts)
        self.assertEqual(registry.list_profiles("testrepo")["profiles"][0]["reason"],
                         "ISOLATION_UNVERIFIED")

    def test_registry_fails_closed_when_static_sandbox_helper_is_missing(self):
        (Path(self.profile["executable"]).parent / "codex-linux-sandbox").unlink()
        self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                         "SANDBOX_HELPER_UNVERIFIED")

    def test_registry_requires_code_mode_host_before_accepting_receipts(self):
        (Path(self.profile["executable"]).parent / "codex-code-mode-host").unlink()
        self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                         "CODE_MODE_HOST_UNVERIFIED")
        with self.assertRaisesRegex(HostRuntimeError, "CODE_MODE_HOST_UNVERIFIED"):
            self.prepare()

    def test_registry_rejects_code_mode_host_hash_mode_and_symlink_drift(self):
        helper = Path(self.profile["executable"]).parent / "codex-code-mode-host"
        original = helper.read_bytes()
        for drift in ("hash", "mode", "symlink"):
            with self.subTest(drift=drift):
                helper.unlink()
                helper.write_bytes(original)
                helper.chmod(0o700)
                if drift == "hash":
                    helper.write_bytes(original + b"changed")
                elif drift == "mode":
                    helper.chmod(0o755)
                else:
                    helper.unlink()
                    helper.symlink_to(helper.parent / "personal-ops-agent-bootstrap")
                self.assertEqual(
                    self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                    "CODE_MODE_HOST_UNVERIFIED")

    def test_code_mode_contract_change_invalidates_receipts(self):
        with mock.patch('agent_profiles.ISOLATION_CONTRACT_VERSION', 'changed-code-host'):
            self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                             "ISOLATION_UNVERIFIED")

    def test_receipt_is_bound_to_current_isolation_contract(self):
        receipt = self.receipts / (self.profile['isolation_receipt'] + '.json')
        document = json.loads(receipt.read_text())
        document['binding']["isolation_contract_digest"] = "0" * 64
        receipt.write_text(json.dumps(document))
        receipt.chmod(0o600)
        self.assertEqual(self.registry.list_profiles("testrepo")["profiles"][0]["reason"],
                         "ISOLATION_UNVERIFIED")

    def test_preparation_is_snapshot_only_and_idempotent(self):
        first = self.prepare()
        again = self.prepare()
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertEqual(first["state"], "pending_host_approval")
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")
        self.assertNotIn("objective", first)
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.manager.prepare("testrepo", profile_id="codex-repo",
                                 objective="Different requested objective",
                                 reason="WORKFLOW_COMPLEXITY", request_id="task-one")

    def test_reported_task_label_is_public_redacted_and_conditionally_frozen(self):
        legacy = self.prepare("legacy-unlabelled")
        legacy_state = json.loads(self.manager._path(legacy["task_id"]).read_text())
        self.assertNotIn("task_label", legacy_state["immutable"])
        self.assertEqual(legacy["work_context"], {
            "task_label": None,
            "label_source": "unavailable",
            "command_label": "Codex isolated task",
            "action": "code",
        })

        with mock.patch("host_execution._known_secret_values",
                        return_value={"known-secret-value"}):
            labelled = self.prepare(
                "labelled-task",
                task_label="Review known-secret-value token=label-secret",
            )
        self.assertEqual(labelled["work_context"], {
            "task_label": "Review [REDACTED] token=[REDACTED]",
            "label_source": "reported",
            "command_label": "Codex isolated task",
            "action": "code",
        })
        self.assertNotIn("known-secret-value", json.dumps(labelled))
        self.assertNotIn("label-secret", json.dumps(labelled))
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.prepare("labelled-task", task_label="Different review")

    async def test_labelled_isolated_task_keeps_context_through_lifecycle_and_telemetry(self):
        observed = []
        self.manager.event_sink = lambda project, event: observed.append((project, event))
        item = self.prepare("labelled-lifecycle", task_label="Review fixture source")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        state = await self.wait_result(item["task_id"])
        self.assertEqual(state["work_context"]["task_label"], "Review fixture source")
        self.assertTrue(observed)
        self.assertTrue(all(event[1]["task_label"] == "Review fixture source"
                            for event in observed))
        self.assertNotIn("Review one synthetic source file only", json.dumps(observed))

    def test_preparation_rejects_source_larger_than_per_plan_cap_without_snapshot(self):
        with mock.patch.object(agent_tasks, "MAX_PLAN_SOURCE_BYTES", 1):
            with self.assertRaisesRegex(HostRuntimeError, "SOURCE_CAPACITY_REACHED"):
                self.prepare("too-large")
        self.assertEqual(list(self.manager.sources_dir.iterdir()), [])

    def test_preparation_preserves_existing_snapshots_when_aggregate_cap_is_full(self):
        retained = self.manager.sources_dir / ("f" * 32)
        retained.mkdir(mode=0o700)
        evidence = retained / "evidence"
        evidence.write_bytes(b"kept")
        evidence.chmod(0o600)
        with mock.patch.object(agent_tasks, "MAX_RETAINED_SOURCE_BYTES", 4):
            with self.assertRaisesRegex(HostRuntimeError, "SOURCE_CAPACITY_REACHED"):
                self.prepare("aggregate-full")
        self.assertEqual(evidence.read_bytes(), b"kept")

    async def test_terminal_history_remains_bounded_and_is_not_pruned(self):
        with mock.patch.object(agent_tasks, "MAX_TASKS", 1):
            first = self.prepare("first-terminal")
            await self.manager.cancel("testrepo", first["task_id"])
            with self.assertRaisesRegex(HostRuntimeError, "HISTORY_CAPACITY_REACHED"):
                self.prepare("after-terminal")
        self.assertEqual(self.manager.get("testrepo", first["task_id"])["state"], "cancelled")

    async def test_unapproved_never_starts_agent(self):
        item = self.prepare()
        with self.assertRaisesRegex(HostRuntimeError, "HOST_APPROVAL_REQUIRED"):
            await self.manager.start("testrepo", item["task_id"])
        self.assertEqual(self.runner.calls, 0)

    def test_web_approval_checks_exact_plan_and_is_one_use(self):
        item = self.prepare()
        with self.assertRaisesRegex(HostRuntimeError, "HOST_APPROVAL_DENIED"):
            self.manager.approve_web("testrepo", task_id=item["task_id"],
                                     plan_digest=item["plan_digest"],
                                     source_digest=item["source_digest"],
                                     confirmation="incorrect")
        with self.assertRaisesRegex(HostRuntimeError, "STALE_PLAN"):
            self.manager.approve_web("testrepo", task_id=item["task_id"],
                                     plan_digest="0"*64,
                                     source_digest=item["source_digest"],
                                     confirmation=item["task_id"])
        self.approve(item)
        with self.assertRaisesRegex(HostRuntimeError, "STALE_PLAN"):
            self.approve(item)
        self.assertEqual(self.runner.calls, 0)

    def test_source_edit_invalidates_approval(self):
        item = self.prepare()
        (self.root / "src/demo.py").write_text("concurrent edit\n")
        with self.assertRaisesRegex(HostRuntimeError, "STALE_PLAN"):
            self.approve(item)
        self.assertEqual(self.runner.calls, 0)

    async def test_model_completion_kept_for_review_without_mutating_project(self):
        item = self.prepare()
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        state = await self.wait_result(item["task_id"])
        self.assertEqual(state["state"], "review_required")
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")
        self.assertNotIn('"objective":', json.dumps(state))
        self.assertEqual(state["result"]["source_review"]["changed_files"],
                         ["src/demo.py"])
        self.assertNotIn("model_report", state["result"])

    async def test_cas_profile_remains_fail_closed_until_isolation_is_proven(self):
        self.profile["apply_policy"] = "cas"
        self.write_config()
        with self.assertRaisesRegex(HostRuntimeError, "CAS_UNVERIFIED"):
            self.prepare()
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")

    async def test_cancel_unstarted_never_calls_runner(self):
        item = self.prepare()
        cancelled = await self.manager.cancel("testrepo", item["task_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.runner.calls, 0)

    async def test_cancelled_queued_worker_cannot_start_runner(self):
        item = self.prepare()
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        cancelled = await self.manager.cancel("testrepo", item["task_id"])
        await asyncio.sleep(0)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.runner.calls, 0)
        self.assertEqual(self.manager.get("testrepo", item["task_id"])["state"], "cancelled")

    async def test_cancelled_running_task_never_accepts_returned_source(self):
        runner = CancelReturnsRunner()
        self.manager.runner = runner
        item = self.prepare("cancel-running")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await runner.started.wait()
        state = await self.manager.cancel("testrepo", item["task_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertEqual(state["code"], "AGENT_STOP_UNVERIFIED")
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")

    async def test_verified_running_cancellation_is_cancelled_with_evidence(self):
        runner = VerifiedCancelRunner()
        self.manager.runner = runner
        item = self.prepare("cancel-verified")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await runner.started.wait()
        state = await self.manager.cancel("testrepo", item["task_id"])
        self.assertEqual(state["state"], "cancelled")
        self.assertTrue(state["termination_verified"])
        self.assertEqual((self.root / "src/demo.py").read_text(), "original\n")

    async def test_second_manager_preserves_live_owner_and_recovers_lease_free_state(self):
        runner = VerifiedCancelRunner()
        self.manager.runner = runner
        item = self.prepare("lease-owner")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await runner.started.wait()
        observer = AgentTaskManager(
            {"testrepo": self.root}, self.registry,
            state_dir=self.home / "agent-tasks", runner=SyntheticRunner(),
            trusted_runtime=True)
        self.assertEqual(observer.get("testrepo", item["task_id"])["state"], "running")
        await self.manager.cancel("testrepo", item["task_id"])

        for stale_state in ("queued", "running", "cancelling", "waiting"):
            with self.subTest(stale_state=stale_state):
                stale = self.prepare("lease-stale-" + stale_state)
                self.approve(stale)
                path = self.manager._path(stale["task_id"])
                document = json.loads(path.read_text())
                document.update(state=stale_state, phase="AGENT",
                                started_at=document["created_at"])
                path.write_text(json.dumps(document))
                path.chmod(0o600)
                recovered = observer.get("testrepo", stale["task_id"])
                self.assertEqual(recovered["state"], "outcome_unknown")
                self.assertEqual(recovered["code"], "AGENT_WORKER_OWNER_LOST")

    async def test_callbacks_persist_only_safe_identity_events_usage_and_result(self):
        observed = []
        self.manager.runner = ObservableRunner()
        self.manager.event_sink = lambda project, event: observed.append((project, event))
        item = self.prepare("observable")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        state = await self.wait_result(item["task_id"])
        identity = state["execution_identity"]
        self.assertEqual(identity["task_id"], item["task_id"])
        self.assertEqual(identity["unit"], "personal-ops-agent-" + "a" * 32 + ".service")
        self.assertEqual(identity["invocation_id"], "b" * 32)
        self.assertEqual(identity["boot_id"], "12345678-1234-1234-1234-123456789abc")
        self.assertEqual(identity["uid"], os.getuid())
        self.assertEqual((identity["pid"], identity["start_ticks"]), (123, 456))
        self.assertEqual(set(identity), {
            "task_id", "unit", "invocation_id", "cgroup", "boot_id", "uid", "pid",
            "start_ticks", "wrapper_pid", "wrapper_start_ticks",
        })
        persisted = json.loads(self.manager._path(item["task_id"]).read_text())
        self.assertRegex(persisted["broker_context_nonce"], r"[0-9a-f]{64}")
        self.assertNotEqual(persisted["broker_context_nonce"], "c" * 64)
        self.assertEqual(self.manager.runner.tasks[0]["broker_context_nonce"],
                         persisted["broker_context_nonce"])
        self.assertEqual([event["code"] for event in state["events"]],
                         ["COMMAND_ACTIVITY", "TURN_COMPLETED"])
        self.assertEqual(state["provider_usage"], {
            "input_tokens": 11, "output_tokens": 7,
        })
        self.assertEqual(state["result"]["model_report"], {
            "summary": "Reviewed the source",
            "changed_files": ["src/demo.py"],
            "validations": ["Static review only"],
            "remaining": ["Run the real test suite"],
            "approval_required": True,
            "provenance": "provider_reported",
            "validation_evidence": "model_report_only",
        })
        self.assertEqual(state["result"]["source_review"], {
            "changed_files": ["src/demo.py"],
            "protected": [],
            "conflicts": [],
            "applied_paths": [],
            "review_required_paths": [],
            "validation_evidence": {"source_files_compared": 2, "changes": 1},
            "status": "review_required",
        })
        serialized = json.dumps(state)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("raw", serialized)
        self.assertEqual(observed[-1][0], "testrepo")
        self.assertEqual(observed[-1][1]["state"], "unknown")
        self.assertEqual(observed[-1][1]["usage"], {
            "input_tokens": 11, "output_tokens": 7,
        })
        self.assertNotIn("summary", json.dumps(observed))

    async def test_runner_receives_only_frozen_execution_and_broker_context(self):
        item = self.prepare("runner-context")
        persisted_before = json.loads(self.manager._path(item["task_id"]).read_text())
        frozen = persisted_before["immutable"]
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await self.wait_result(item["task_id"])
        task = self.runner.tasks[0]
        persisted_after = json.loads(self.manager._path(item["task_id"]).read_text())
        self.assertEqual(task, {
            "task_id": item["task_id"],
            "project": "testrepo",
            "profile_id": frozen["profile_id"],
            "profile_digest": frozen["profile_digest"],
            "source_digest": frozen["source_digest"],
            "host_capabilities": [],
            "objective": frozen["objective"],
            "timeout_minutes": frozen["timeout_minutes"],
            "started_at": persisted_after["started_at"],
            "broker_context_nonce": persisted_after["broker_context_nonce"],
            "model": frozen["model"],
            "reasoning_effort": frozen["reasoning_effort"],
        })
        self.assertRegex(task["broker_context_nonce"], r"[0-9a-f]{64}")
        self.assertNotEqual(task["broker_context_nonce"], frozen["nonce"])

    async def test_manager_shutdown_cancels_only_its_owned_worker(self):
        runner = VerifiedCancelRunner()
        self.manager.runner = runner
        item = self.prepare("shutdown-owned")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await runner.started.wait()
        observer = AgentTaskManager(
            {"testrepo": self.root}, self.registry,
            state_dir=self.home / "agent-tasks", runner=SyntheticRunner(),
            trusted_runtime=True)
        await observer.close()
        self.assertEqual(self.manager.get("testrepo", item["task_id"])["state"], "running")
        await self.manager.close()
        result = observer.get("testrepo", item["task_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["termination_verified"])

    async def test_specific_safe_runner_error_code_is_retained(self):
        self.manager.runner = FailingRunner()
        item = self.prepare("specific-error")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        state = await self.wait_result(item["task_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertEqual(state["code"], "AGENT_EVENT_TOO_LARGE")

    async def test_unverified_cancel_identity_is_retained_as_terminal_code(self):
        runner = UnverifiedCancelRunner()
        self.manager.runner = runner
        item = self.prepare("cancel-identity")
        self.approve(item)
        await self.manager.start("testrepo", item["task_id"])
        await runner.started.wait()
        state = await self.manager.cancel("testrepo", item["task_id"])
        runner.released.set()
        await asyncio.sleep(0)
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertEqual(state["code"], "AGENT_PROCESS_TREE_UNVERIFIED")

    def test_broker_denies_host_side_effects(self):
        item = self.prepare()
        broker = AgentHostBroker(self.manager)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
            broker.prepare("testrepo", item["task_id"], "test", "compose.exec", {}, "req")

    def test_offline_compiler_never_enables_legacy_sandbox(self):
        argv = compile_codex_profile(self.profile, self.root)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("danger-full-access", " ".join(argv))
        permission_settings = [argv[index + 1] for index, value in enumerate(argv[:-1])
                               if value == "-c" and argv[index + 1].startswith(
                                   "permissions.delegated=")]
        self.assertEqual(len(permission_settings), 1)
        self.assertIn('":root"="deny"', permission_settings[0])
        self.assertIn('".aws/**"="deny"', permission_settings[0])
        self.assertNotIn('filesystem.":root"', " ".join(argv))
        self.assertIn("network={enabled=false}", " ".join(argv))
        self.assertIn("--no-daemon", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("--output-last-message", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(argv[-1], "-")

    def test_offline_compiler_rejects_unproven_cas_profile(self):
        profile = dict(self.profile, apply_policy="cas")
        with self.assertRaisesRegex(HostRuntimeError, "UNSUPPORTED_AGENT_PROFILE"):
            compile_codex_profile(profile, self.root)

    def test_event_sanitization_removes_reasoning_and_command(self):
        self.assertIsNone(parse_codex_event({"type": "item.completed", "item": {
            "type": "reasoning", "text": "secret"}}))
        event = parse_codex_event({"type": "item.completed", "item": {
            "type": "command_execution", "command": "cat ~/.ssh/id_rsa",
            "stdout": "secret"}})
        self.assertEqual(event, {"phase": "WORKING", "code": "COMMAND_ACTIVITY"})
        item = parse_codex_event({"type": "turn.completed", "usage": {
            "input_tokens": 4, "output_tokens": 2, "reasoning_output_tokens": -3}})
        self.assertEqual(item["usage"], {"input_tokens": 4, "output_tokens": 2})
        self.assertEqual(item["usage_scope"], "turn")

    async def test_runner_keeps_bounded_stderr_private_and_out_of_result(self):
        executable = self.home / "fake-runtime" / "codex"
        executable.parent.mkdir(mode=0o700)
        shutil.copy2(self.home / "runtime/personal-ops-agent-bootstrap",
                     executable.parent / "personal-ops-agent-bootstrap")
        executable.write_text(
            "#!/usr/bin/python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "sys.stderr.write('private diagnostic detail\\n')\n"
            "path=sys.argv[sys.argv.index('--output-last-message')+1]\n"
            "with open(path,'w') as stream: json.dump({'summary':'Synthetic review','changed_files':[],'validations':['Fixture only'],'remaining':[],'approval_required':True},stream)\n"
            "__import__('os').chmod(path,0o600)\n"
            "print(json.dumps({'type':'turn.completed','usage':{}}), flush=True)\n")
        executable.chmod(0o700)
        shutil.copy2(Path(sys.executable).resolve(), executable.parent / "codex-linux-sandbox")
        profile = dict(self.profile, executable=str(executable),
                       executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        runner = CodexAgentRunner(supervisor=FixtureSupervisor())
        with mock.patch("agent_runner.compile_codex_profile", return_value=[
                str(executable), "--output-last-message",
                str(self.root / ".agent-control" / "last-message.json")]), \
                mock.patch("agent_runner.sandbox_helper", return_value=executable):
            result = await runner.run({"task_id": "e" * 32, "objective": "synthetic only",
                                       "timeout_minutes": 1}, self.root, profile)
        diagnostic = self.root / ".agent-control" / "stderr.log"
        self.assertTrue(result["verified"])
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertEqual(diagnostic.read_text(), "private diagnostic detail\n")
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)

    async def test_runner_timeout_bounds_process_after_stdout_closes(self):
        executable = self.home / "sleep-runtime" / "codex"
        executable.parent.mkdir(mode=0o700)
        shutil.copy2(self.home / "runtime/personal-ops-agent-bootstrap",
                     executable.parent / "personal-ops-agent-bootstrap")
        executable.write_text(
            "#!/usr/bin/python3\n"
            "import os, sys, time\n"
            "sys.stdin.read()\n"
            "os.close(1)\n"
            "time.sleep(10)\n")
        executable.chmod(0o700)
        shutil.copy2(Path(sys.executable).resolve(), executable.parent / "codex-linux-sandbox")
        profile = dict(self.profile, executable=str(executable),
                       executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        runner = CodexAgentRunner(supervisor=FixtureSupervisor())
        with mock.patch("agent_runner.compile_codex_profile", return_value=[str(executable)]), \
                mock.patch("agent_runner.sandbox_helper", return_value=executable):
            with self.assertRaisesRegex(HostRuntimeError, "AGENT_TIMEOUT"):
                await runner.run({"task_id": "d" * 32, "objective": "synthetic only",
                                  "timeout_minutes": .001}, self.root, profile)
        self.assertEqual(runner.processes, {})

    async def test_runner_reaps_owned_group_when_identity_capture_fails(self):
        executable = self.home / "identity-runtime" / "codex"
        executable.parent.mkdir(mode=0o700)
        shutil.copy2(self.home / "runtime/personal-ops-agent-bootstrap",
                     executable.parent / "personal-ops-agent-bootstrap")
        executable.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(10)\n")
        executable.chmod(0o700)
        shutil.copy2(Path(sys.executable).resolve(), executable.parent / "codex-linux-sandbox")
        profile = dict(self.profile, executable=str(executable),
                       executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        observed = []
        def fail_identity(pid):
            observed.append(pid)
            raise HostRuntimeError("AGENT_START_FAILED")
        runner = CodexAgentRunner(supervisor=FixtureSupervisor())
        with mock.patch("agent_runner.compile_codex_profile", return_value=[str(executable)]), \
                mock.patch("agent_runner.sandbox_helper", return_value=executable), \
                mock.patch.object(agent_runner, "_process_start_ticks",
                                  side_effect=fail_identity):
            with self.assertRaisesRegex(HostRuntimeError, "AGENT_START_FAILED"):
                await runner.run({"task_id": "c" * 32, "objective": "synthetic only",
                                  "timeout_minutes": 1}, self.root, profile)
        self.assertEqual(runner.processes, {})
        self.assertEqual(runner.process_start_ticks, {})
        with self.assertRaises(ProcessLookupError):
            os.kill(observed[0], 0)


if __name__ == "__main__":
    unittest.main()
