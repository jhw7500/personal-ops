"""Disposable Codex host delegation fixtures: no model or host service is called."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_agent import HostAgentAdapter, compile_host_codex  # noqa: E402
from agent_tools import AgentTools  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class FixtureAccess:
    def __init__(self):
        self.active = True
        self.codex = True

    def get(self, project):
        return {"active": self.active, "allow_codex": self.codex}

    def require(self, project, **kwargs):
        if not self.active or kwargs.get("codex") and not self.codex:
            raise HostRuntimeError("HOST_ACCESS_REQUIRED")
        return {"grant_id": "b" * 32}


class FixtureExecution:
    def __init__(self, root):
        self.root = root / "host-executions"
        self.state_root = root
        self.access = FixtureAccess()
        self.prepared = []
        self.record = None
        self.log = ""

    def prepare(self, project, **kwargs):
        self.prepared.append(kwargs)
        task_label = kwargs.get("task_label")
        self.record = {"project": project, "operation_id": "a" * 32,
                       "kind": "codex", "state": "prepared", "agent_metadata": kwargs["agent_metadata"],
                       "approval_required": False, "result": None,
                       "work_context": {
                           "task_label": task_label,
                           "label_source": "reported" if task_label else "unavailable",
                           "command_label": "Codex host task", "action": "code",
                       }}
        return dict(self.record)

    def owns(self, project, ident, kind=None):
        return bool(self.record and self.record["project"] == project
                    and self.record["operation_id"] == ident and kind in (None, "codex"))

    async def execute(self, project, ident):
        self.access.require(project, codex=True)
        self.record["state"] = "queued"
        return dict(self.record)

    def get(self, project, ident):
        return dict(self.record)

    def cancel(self, project, ident):
        self.record["state"] = "cancelled"
        return dict(self.record)

    def read_log(self, project, ident, **kwargs):
        cursor = kwargs.get("cursor", 0)
        encoded = self.log.encode()
        block = encoded[cursor:cursor + kwargs.get("max_bytes", 16384)]
        return {"text": block.decode(), "next_cursor": cursor + len(block),
                "eof": cursor + len(block) >= len(encoded)}


class LegacyManager:
    def __init__(self):
        self.calls = []

    def prepare(self, project, **kwargs):
        self.calls.append(kwargs)
        raise HostRuntimeError("ISOLATION_UNVERIFIED")

    def list_profiles(self, project):
        return {"project": project, "profiles": []}


class HostAgentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="host-agent-fixture-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.execution = FixtureExecution(self.root)
        self.legacy = LegacyManager()
        binary = self.root / "runtime" / "codex"
        binary.parent.mkdir(mode=0o700)
        binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        self.profile = {"project": "demo", "provider": "codex", "enabled": True,
                        "executable": str(binary),
                        "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "version": "0.162.1", "model": "gpt-6.1-sol", "reasoning_effort": "high",
                        "allowed_paths": ["src"], "host_capabilities": [],
                        "apply_policy": "review_only", "isolation_receipt": None,
                        "smoke_receipt": None}
        self.registry = mock.Mock()
        self.registry._profiles.return_value = {"codex-review": self.profile}
        self.adapter = HostAgentAdapter(self.execution, profiles=self.registry)
        self.runtime = mock.patch.object(self.adapter, "_stage_runtime", return_value=binary)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.tools = AgentTools({"demo": self.root}, self.root / "agent-tasks",
                                manager=self.legacy, host_execution=self.execution)
        self.tools._host_agent = self.adapter
        self.args = {"project": "demo", "profile_id": "codex-review", "objective": "Edit the disposable test project.",
                     "reason": "WORKFLOW_COMPLEXITY", "request_id": "fixture-1"}

    async def test_existing_prepare_start_get_cancel_host_without_task_receipt(self):
        plan = await self.tools.call("prepare_agent_task", self.args)
        self.assertFalse(plan["approval_required"])
        self.assertEqual(plan["execution_mode"], "host")
        self.assertEqual(plan["model"], "gpt-6.1-sol")
        self.assertEqual(plan["reasoning_effort"], "high")
        self.assertEqual(self.legacy.calls, [])
        task = {"project": "demo", "task_id": plan["task_id"]}
        self.assertEqual((await self.tools.call("start_agent_task", task))["state"], "queued")
        self.assertEqual((await self.tools.call("get_agent_task", task))["execution_mode"], "host")
        self.assertEqual((await self.tools.call("cancel_agent_task", task))["state"], "cancelled")

    async def test_host_task_label_reaches_execution_and_public_telemetry(self):
        observed = []
        self.adapter.event_sink = lambda project, event: observed.append((project, event))
        plan = await self.tools.call("prepare_agent_task", {
            **self.args, "task_label": "Review payment callback",
        })
        self.assertEqual(self.execution.prepared[0]["task_label"],
                         "Review payment callback")
        self.assertEqual(plan["work_context"]["task_label"],
                         "Review payment callback")
        await self.tools.call("start_agent_task", {
            "project": "demo", "task_id": plan["task_id"],
        })
        self.assertEqual(observed[-1][1]["task_label"],
                         "Review payment callback")
        self.assertNotIn(self.args["objective"], json.dumps(observed))

    async def test_host_task_label_is_validated_before_runtime_staging(self):
        with self.assertRaisesRegex(ValueError, "INVALID_AGENT_TASK"):
            await self.tools.call("prepare_agent_task", {
                **self.args, "task_label": "bad\nlabel",
            })
        self.assertEqual(self.execution.prepared, [])
        self.adapter._stage_runtime.assert_not_called()

    async def test_forced_isolated_never_falls_back_after_failed_proofs(self):
        with self.assertRaisesRegex(ValueError, "ISOLATION_UNVERIFIED"):
            await self.tools.call("prepare_agent_task", {**self.args, "execution_mode": "isolated"})
        self.assertEqual(len(self.legacy.calls), 1)
        self.assertEqual(self.execution.prepared, [])
        self.assertNotIn("execution_mode", self.legacy.calls[0])

    async def test_absent_grant_or_codex_opt_in_preserves_legacy_gate(self):
        for changes in ({"active": False}, {"codex": False}):
            for name, value in changes.items():
                setattr(self.execution.access, name, value)
            with self.assertRaisesRegex(ValueError, "ISOLATION_UNVERIFIED"):
                await self.tools.call("prepare_agent_task", self.args)
            self.execution.access.active = self.execution.access.codex = True
        self.assertEqual(len(self.legacy.calls), 2)
        self.assertEqual(self.execution.prepared, [])

    async def test_explicit_host_denied_without_grant_and_revoked_start_denied(self):
        self.execution.access.active = False
        with self.assertRaisesRegex(ValueError, "HOST_ACCESS_REQUIRED"):
            await self.tools.call("prepare_agent_task", {**self.args, "execution_mode": "host"})
        self.execution.access.active = True
        plan = await self.tools.call("prepare_agent_task", self.args)
        self.execution.access.active = False
        with self.assertRaisesRegex(ValueError, "HOST_ACCESS_REQUIRED"):
            await self.tools.call("start_agent_task", {"project": "demo", "task_id": plan["task_id"]})

    async def test_invalid_mode_or_profile_or_disabled_profile_never_prepares(self):
        for change in ({"execution_mode": "unsafe"}, {"profile_id": "missing"}, {"objective": "tiny"}):
            with self.assertRaises(ValueError):
                await self.tools.call("prepare_agent_task", {**self.args, **change})
        self.profile["enabled"] = False
        with self.assertRaisesRegex(ValueError, "PROFILE_DISABLED"):
            await self.tools.call("prepare_agent_task", self.args)
        self.assertEqual(self.execution.prepared, [])

    async def test_host_report_is_bounded_and_provider_usage_is_observed(self):
        plan = await self.tools.call("prepare_agent_task", self.args)
        report = {"summary": "Edited fixture", "changed_files": ["src/demo.py"],
                  "validations": ["Model claims test pass"], "remaining": [], "approval_required": False}
        self.execution.log = "\n".join(json.dumps(x) for x in (
            {"type": "thread.started", "id": "never expose"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 5}}
        )) + "\n"
        result = await self.tools.call("get_agent_task", {"project": "demo", "task_id": plan["task_id"]})
        self.assertEqual(result["provider_usage"], {"input_tokens": 12, "output_tokens": 5})
        self.assertEqual(result["model_report"]["validation_evidence"], "model_report_only")
        self.assertNotIn("private reasoning", json.dumps(result))
        self.assertNotIn("never expose", json.dumps(result))

    def test_compiler_uses_ephemeral_dedicated_host_mode_and_stdin(self):
        argv = compile_host_codex(self.profile, Path(self.profile["executable"]), self.root / "schema.json")
        self.assertIn("--no-daemon", argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--json", argv)
        self.assertNotIn("resume", argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")

    async def test_real_managers_stage_runtime_and_use_existing_flow_without_task_approval(self):
        from host_access import HostAccess
        from host_execution import HostExecution
        from agent_profiles import profile_digest
        project = self.root / "real-project"
        project.mkdir()
        state_root = self.root / "real-state"
        access = HostAccess({"demo": project}, state_root, trusted_runtime=True)
        request = access.request("demo")
        access.approve_web("demo", request_id=request["request_id"], duration_seconds=60,
                           allow_codex=True, confirmation="demo")
        binary = Path(self.profile["executable"])
        (binary.parent / "codex-linux-sandbox").write_bytes(binary.read_bytes())
        (binary.parent / "codex-linux-sandbox").chmod(0o700)
        helper = binary.parent / "codex-code-mode-host"
        helper.write_bytes(b"fixture-code-mode-helper")
        helper.chmod(0o700)
        def runner(frozen, cancel, **kwargs):
            with kwargs["go_guard"]():
                self.assertIn(b"Edit the disposable", kwargs["input_bytes"])
            kwargs["on_identity"]({"task_id": kwargs["operation_id"]})
            report = {"summary": "Configured token=fixture-secret-value", "changed_files": [],
                      "validations": [], "remaining": [], "approval_required": False}
            event = {"type": "item.completed", "item": {"type": "agent_message",
                     "text": json.dumps(report, separators=(',', ':'))}}
            kwargs["on_output"]("stdout", (json.dumps(event, separators=(',', ':')) + '\n').encode())
            kwargs["on_output"]("stdout", b'{"type":"turn.completed","usage":{"output_tokens":3}}\n')
            return {"exit_code": 0, "duration_ms": 1, "stdout_bytes": 65, "stderr_bytes": 0,
                    "termination_verified": True}
        execution = HostExecution({"demo": project}, state_root, access=access,
                                  runner=runner, trusted_runtime=True)
        adapter = HostAgentAdapter(execution, profiles=self.registry)
        with mock.patch("host_agent.code_mode_host_digest", return_value=hashlib.sha256(helper.read_bytes()).hexdigest()):
            plan = adapter.prepare("demo", **{k: v for k, v in self.args.items() if k != "project"})
        staged = state_root / "host-agent-runtimes"
        self.assertTrue(staged.is_dir())
        self.assertEqual(plan["agent_metadata"]["profile_digest"], profile_digest(self.profile))
        self.assertFalse((state_root / "agent-tasks" / "receipts").exists())
        self.assertEqual(adapter.get("demo", plan["task_id"])["events"], [])
        await adapter.start("demo", plan["task_id"])
        for _ in range(100):
            result = adapter.get("demo", plan["task_id"])
            if result["state"] not in {"queued", "running"}:
                break
            await asyncio.sleep(.01)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["provider_usage"], {"output_tokens": 3})
        self.assertIn('[REDACTED]', result["model_report"]["summary"])
        self.assertNotIn('fixture-secret-value', json.dumps(result))
        with self.assertRaisesRegex(HostRuntimeError, "HOST_AGENT_LOG_RESTRICTED"):
            execution.read_log("demo", plan["task_id"])
        await execution.close()


if __name__ == "__main__":
    unittest.main()
