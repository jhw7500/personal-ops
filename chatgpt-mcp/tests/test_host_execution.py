from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_execution import HostExecution  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from host_runtime_operations import _save_private_json  # noqa: E402
from workspace_host import publication_lane  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class FakeAccess:
    def __init__(self):
        self.revoked = False
        self.allow_codex = True
        self.grant_id = "g" * 32
        self.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.lifetime = "timed"

    def require(self, project, *, grant_id=None, codex=False):
        if self.revoked or grant_id not in (None, self.grant_id):
            raise HostRuntimeError("HOST_ACCESS_DENIED")
        if codex and not self.allow_codex:
            raise HostRuntimeError("HOST_ACCESS_CODEX_DENIED")
        return {"project": project, "grant_id": self.grant_id,
                "expires_at": self.expires_at, "allow_codex": self.allow_codex,
                "scope": "host-user", "lifetime": self.lifetime,
                "version": 2, "grant_state_revision": 2}

    @contextmanager
    def guard(self, project, grant_id, *, codex=False):
        yield self.require(project, grant_id=grant_id, codex=codex)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.block = False

    def __call__(self, frozen, cancel, **options):
        self.calls.append((frozen, options))
        options["on_identity"]({"task_id": options["operation_id"], "fixture": True})
        with options["go_guard"]():
            pass
        self.started.set()
        options["on_output"]("stdout", b"token=visible-secret\nhello world\n")
        options["on_output"]("stderr", b"Authorization: Bearer abcdef\n")
        while self.block and not cancel.wait(0.02):
            pass
        if cancel.is_set():
            error = HostRuntimeError("HOST_COMMAND_CANCELLED", "EXECUTE")
            error.termination_verified = True
            error.result = {"exit_code": -1, "duration_ms": 1,
                            "stdout_bytes": 33, "stderr_bytes": 29}
            raise error
        return {"exit_code": 0, "duration_ms": 1, "stdout_bytes": 33,
                "stderr_bytes": 29, "termination_verified": True}


class UnverifiedRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def __call__(self, frozen, cancel, **options):
        options["on_identity"]({"task_id": options["operation_id"], "fixture": True})
        self.started.set()
        while not self.release.wait(0.01) and not cancel.is_set():
            pass
        error = HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED", "EXECUTE")
        error.termination_verified = False
        raise error


class HostExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="host-execution-test-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "repo"
        self.project.mkdir(mode=0o700)
        (self.project / "sub").mkdir()
        self.executable = self.base / "program"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.access = FakeAccess()
        self.runner = FakeRunner()
        self.manager = HostExecution(
            {"demo": self.project}, self.base / "state", access=self.access,
            runner=self.runner, trusted_runtime=True)
        self.addAsyncCleanup(self.manager.close)

    def prepare(self, request_id="request-1", **overrides):
        values = {"executable": str(self.executable), "argv": ["one"],
                  "cwd": "sub", "request_id": request_id}
        values.update(overrides)
        return self.manager.prepare("demo", **values)

    async def wait_terminal(self, operation_id):
        for _ in range(100):
            state = self.manager.get("demo", operation_id)
            if state["state"] not in {"queued", "running"}:
                return state
            await __import__("asyncio").sleep(0.02)
        self.fail("operation did not finish")

    def active_workspace(self, project, name):
        projects_root = project.parent
        os.chmod(projects_root, 0o700)
        os.chmod(project, 0o700)
        owner = self.base / (name + "-owner")
        owner.mkdir(mode=0o700)
        policy = owner / "policy.json"
        policy.write_text(json.dumps({
            "version": 1,
            "base_root": str(projects_root),
            "artifacts": [],
            "protected_paths": [],
        }), encoding="utf-8")
        os.chmod(policy, 0o600)
        workspace = WorkspaceTools(
            {"demo": project}, state_dir=self.base / (name + "-authority"),
            config_path=policy)
        snapshot = workspace.policy.load(required=True)
        workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
        return workspace

    async def test_ten_commands_reuse_one_grant_and_request_ids_remain_idempotent(self):
        plans = [self.prepare(f"request-{index}", argv=[str(index)]) for index in range(10)]
        duplicate = self.prepare("request-0", argv=["0"])
        self.assertEqual(duplicate["operation_id"], plans[0]["operation_id"])
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.prepare("request-0", argv=["different"])
        self.assertEqual(len({plan["operation_id"] for plan in plans}), 10)
        self.assertTrue(all(
            plan["authorization"]["grant_id"] == self.access.grant_id for plan in plans
        ))

        for plan in plans:
            await self.manager.execute("demo", plan["operation_id"])
            self.assertEqual((await self.wait_terminal(plan["operation_id"]))["state"],
                             "succeeded")
            await self.manager.execute("demo", plan["operation_id"])
        self.assertEqual(len(self.runner.calls), 10)

    async def test_until_revoked_grant_keeps_finite_job_timeout(self):
        self.access.lifetime = "until_revoked"
        self.access.expires_at = None

        plan = self.prepare(timeout_seconds=1800)

        self.assertEqual(plan["authorization"]["lifetime"], "until_revoked")
        self.assertIsNone(plan["authorization"]["expires_at"])
        await self.manager.execute("demo", plan["operation_id"])
        self.assertEqual((await self.wait_terminal(plan["operation_id"]))["state"],
                         "succeeded")
        self.assertEqual(self.runner.calls[0][0]["timeout_seconds"], 1800)

    def test_grant_lifetime_and_expiry_combinations_are_exact(self):
        cases = (
            ("timed", None),
            ("until_revoked", self.access.expires_at),
            ("forever", None),
        )
        for lifetime, expires_at in cases:
            with self.subTest(lifetime=lifetime, expires_at=expires_at):
                self.access.lifetime = lifetime
                self.access.expires_at = expires_at
                with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_ACCESS_GRANT"):
                    self.prepare(f"invalid-{lifetime}")

    def test_public_work_context_separates_reported_label_from_safe_command_hint(self):
        npm = self.base / "npm"
        npm.write_bytes(b"#!/bin/sh\nexit 0\n")
        npm.chmod(0o700)
        plan = self.prepare(
            executable=str(npm), argv=["run", "build", "--", "token=do-not-copy"],
            task_label="Build the tossApp test bundle",
            input_text="private objective must never become a display label",
        )
        self.assertEqual(plan["work_context"], {
            "task_label": "Build the tossApp test bundle",
            "label_source": "reported",
            "command_label": "npm run build",
            "action": "build",
        })
        self.assertNotIn("private objective", str(plan))
        self.assertNotIn("do-not-copy", str(plan))

    def test_public_work_context_redacts_labels_and_keeps_opaque_commands_opaque(self):
        with mock.patch("host_execution._known_secret_values",
                        return_value={"known-secret-value"}):
            plan = self.prepare(
                argv=["-c", "deploy known-secret-value; token=argv-secret"],
                task_label="Deploy known-secret-value token=label-secret",
            )
        self.assertEqual(plan["work_context"], {
            "task_label": "Deploy [REDACTED] token=[REDACTED]",
            "label_source": "reported",
            "command_label": "Host command",
            "action": "execute",
        })
        self.assertNotIn("known-secret-value", str(plan))
        self.assertNotIn("argv-secret", str(plan))
        self.assertNotIn("label-secret", str(plan))

    def test_command_hints_are_limited_to_allowlisted_names(self):
        cases = (
            ("npm", ["run", "typecheck", "--", "--token=hidden"],
             "npm run typecheck", "test"),
            ("git", ["diff", "secret/path"], "git diff", "review"),
            ("docker", ["build", "--secret", "id=hidden"], "docker build", "build"),
        )
        for index, (name, argv, command_label, action) in enumerate(cases):
            with self.subTest(name=name):
                executable = self.base / name
                executable.write_bytes(b"#!/bin/sh\nexit 0\n")
                executable.chmod(0o700)
                plan = self.prepare(
                    f"classified-{index}", executable=str(executable), argv=argv)
                self.assertEqual(plan["work_context"]["command_label"], command_label)
                self.assertEqual(plan["work_context"]["action"], action)
                self.assertNotIn("hidden", str(plan["work_context"]))

    async def test_task_label_is_conditional_idempotent_metadata_and_survives_lifecycle(self):
        legacy = self.prepare("legacy-request")
        legacy_state = self.manager._load(legacy["operation_id"])
        self.assertNotIn("task_label", legacy_state["frozen"])
        self.assertEqual(self.prepare("legacy-request")["operation_id"],
                         legacy["operation_id"])

        labelled = self.prepare("labelled-request", task_label="Run fixture checks")
        self.assertEqual(
            self.prepare("labelled-request", task_label="Run fixture checks")["operation_id"],
            labelled["operation_id"],
        )
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.prepare("labelled-request", task_label="Build fixture")
        await self.manager.execute("demo", labelled["operation_id"])
        terminal = await self.wait_terminal(labelled["operation_id"])
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(terminal["work_context"]["task_label"], "Run fixture checks")

    async def test_revocation_before_go_and_while_running_cancels(self):
        before = self.prepare()
        self.access.revoked = True
        await self.manager.execute("demo", before["operation_id"])
        failed = await self.wait_terminal(before["operation_id"])
        self.assertEqual(failed["code"], "HOST_ACCESS_DENIED")

        self.access.revoked = False
        self.runner.block = True
        running = self.prepare("request-2")
        await self.manager.execute("demo", running["operation_id"])
        self.assertTrue(self.runner.started.wait(1))
        self.access.revoked = True
        stopped = await self.wait_terminal(running["operation_id"])
        self.assertEqual(stopped["state"], "cancelled")

    async def test_runner_holds_shared_workspace_lane_through_verified_termination(self):
        projects_root = self.base / "projects-root"
        projects_root.mkdir(mode=0o700)
        lane_project = projects_root / "demo"
        lane_project.mkdir(mode=0o700)
        workspace = self.active_workspace(lane_project, "workspace")

        access = FakeAccess()
        access.workspace = workspace
        runner = FakeRunner()
        runner.block = True
        manager = HostExecution(
            {"demo": lane_project}, self.base / "lane-state", access=access,
            runner=runner, trusted_runtime=True)
        self.addAsyncCleanup(manager.close)
        plan = manager.prepare(
            "demo", executable=str(self.executable), argv=[], cwd=".", request_id="lane")
        await manager.execute("demo", plan["operation_id"])
        self.assertTrue(runner.started.wait(1))

        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        runner.block = False
        for _ in range(100):
            terminal = manager.get("demo", plan["operation_id"])
            if terminal["state"] not in {"queued", "running"}:
                break
            await __import__("asyncio").sleep(0.02)
        else:
            self.fail("workspace-lane operation did not finish")
        self.assertTrue(terminal["termination_verified"])
        for _ in range(100):
            if plan["operation_id"] not in manager._workers:
                break
            await __import__("asyncio").sleep(0.01)
        else:
            self.fail("workspace-lane worker did not release its leases")
        with publication_lane(workspace):
            pass

    async def test_private_redacted_paged_logs_and_public_metadata(self):
        plan = self.prepare(
            input_text="multiline\nprivate objective")
        await self.manager.execute("demo", plan["operation_id"])
        await self.wait_terminal(plan["operation_id"])

        page = self.manager.read_log("demo", plan["operation_id"], max_bytes=100)
        self.assertIn("[REDACTED]", page["text"])
        self.assertNotIn("visible-secret", page["text"])
        self.assertGreater(page["next_cursor"], 0)
        raw = self.manager._log_path(plan["operation_id"], "stdout")
        self.assertIn(b"visible-secret", raw.read_bytes())
        self.assertEqual(stat.S_IMODE(raw.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(raw.parent.stat().st_mode), 0o700)

        codex = self.prepare(
            "request-codex",
            agent_metadata={"profile_id": "codex-review", "model": "gpt-test",
                            "reasoning_effort": "high", "profile_digest": "a" * 64,
                            "reason": "MISSING_TOOL"})
        self.assertEqual(codex["kind"], "codex")
        self.assertEqual(codex["agent_metadata"]["model"], "gpt-test")
        self.assertNotIn("input_text", codex)
        self.assertNotIn("argv", codex)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_AGENT_LOG_RESTRICTED"):
            self.manager.read_log("demo", codex["operation_id"])

    def test_validation_rejects_unsafe_command_inputs(self):
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_EXECUTION"):
            self.prepare(argv=["x"] * 33)
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_EXECUTION"):
            self.prepare(input_text="x" * 32769)
        inside = self.project / "inside"
        inside.write_bytes(b"#!/bin/sh\n")
        inside.chmod(0o700)
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_EXECUTION"):
            self.prepare(executable=str(inside))
        for label in ("", "x" * 161, "bad\nlabel"):
            with self.subTest(label=label):
                with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_EXECUTION"):
                    self.prepare(request_id="bad-label-" + str(len(label)), task_label=label)

    async def test_cross_process_owner_lease_and_durable_cancel_request(self):
        acquiring = threading.Event()
        proceed = threading.Event()
        original = self.manager._acquire_execution_leases

        def delayed(*args):
            acquiring.set()
            proceed.wait(1)
            return original(*args)

        self.manager._acquire_execution_leases = delayed
        plan = self.prepare()
        await self.manager.execute("demo", plan["operation_id"])
        self.assertTrue(acquiring.wait(1))
        other = HostExecution({"demo": self.project}, self.base / "state",
                              access=self.access, runner=FakeRunner(), trusted_runtime=True)
        self.addAsyncCleanup(other.close)
        observed = other.get("demo", plan["operation_id"])
        self.assertIn(observed["state"], {"queued", "running"})
        other.cancel("demo", plan["operation_id"])
        proceed.set()
        stopped = await self.wait_terminal(plan["operation_id"])
        self.assertEqual(stopped["state"], "cancelled")

    async def test_prepared_cancel_and_empty_codex_raw_log(self):
        prepared = self.prepare()
        cancelled = self.manager.cancel("demo", prepared["operation_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        codex = self.prepare(
            "codex-timeout", timeout_seconds=3600,
            agent_metadata={"profile_id": "codex-review", "model": "gpt-test",
                            "reasoning_effort": "high", "profile_digest": "a" * 64,
                            "reason": "WORKFLOW_COMPLEXITY"})
        raw = self.manager.raw_log_path("demo", codex["operation_id"], "stdout")
        self.assertEqual(raw.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(raw.stat().st_mode), 0o600)
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_EXECUTION"):
            self.prepare("general-timeout", timeout_seconds=1801)

    async def test_unverified_cleanup_retains_all_leases_until_verified_stop(self):
        runner = UnverifiedRunner()
        workspace = self.active_workspace(self.project, "unverified-workspace")
        self.access.workspace = workspace
        manager = HostExecution({"demo": self.project}, self.base / "failure-state",
                                access=self.access, runner=runner,
                                trusted_runtime=True)
        self.addAsyncCleanup(manager.close)
        plan = manager.prepare("demo", executable=str(self.executable), argv=[], cwd=".",
                               request_id="failure")
        await manager.execute("demo", plan["operation_id"])
        self.assertTrue(runner.started.wait(1))
        worker = manager._workers[plan["operation_id"]][0]
        runner.release.set()
        await __import__("asyncio").to_thread(worker.join, 1)
        self.assertFalse(worker.is_alive())
        state = manager.get("demo", plan["operation_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertIn(plan["operation_id"], manager._retained_leases)
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        manager._finish_scope = lambda _identity: (_ for _ in ()).throw(
            HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED"))
        with self.assertRaisesRegex(HostRuntimeError, "HOST_COMMAND_PROCESS_TREE_UNVERIFIED"):
            manager.cancel("demo", plan["operation_id"])
        self.assertIn(plan["operation_id"], manager._retained_leases)
        manager._finish_scope = lambda _identity: None
        cleaned = manager.cancel("demo", plan["operation_id"])
        self.assertTrue(cleaned["termination_verified"])
        self.assertNotIn(plan["operation_id"], manager._retained_leases)
        with publication_lane(workspace):
            pass

    async def test_observed_registered_work_blocks_dynamic_start(self):
        _save_private_json(
            self.base / "state" / "host-runtime" / "registered" / "operations"
            / ("f" * 32 + ".json"), {"state": "running"})
        plan = self.prepare()
        await self.manager.execute("demo", plan["operation_id"])
        stopped = await self.wait_terminal(plan["operation_id"])
        self.assertEqual(stopped["code"], "HOST_EXECUTION_MANAGED_WORK_BUSY")
        self.assertEqual(self.runner.calls, [])

    def test_bare_known_admin_token_is_masked_without_altering_private_raw_log(self):
        home = self.base / "credential-fixture"
        config = home / ".config" / "chatgpt-coding-mcp"
        config.mkdir(parents=True)
        secret = "fixture-admin-token-" + "x" * 43
        token = config / "admin-token"
        token.write_text(secret + "\n")
        token.chmod(0o600)
        plan = self.prepare()
        self.manager._append_log(plan["operation_id"], "stdout", (secret + "\n").encode())
        with mock.patch("host_execution.Path.home", return_value=home):
            page = self.manager.read_log("demo", plan["operation_id"])
        self.assertNotIn(secret, page["text"])
        self.assertIn("[REDACTED]", page["text"])
        raw = self.manager.logs_dir / plan["operation_id"] / "stdout.log"
        self.assertEqual(raw.read_text(), secret + "\n")


if __name__ == "__main__":
    unittest.main()
