"""Delegated-agent broker authorization over disposable registered-v3 fixtures.

No real Codex, Docker, database, host service, or user session is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from agent_host_broker import AgentHostBroker  # noqa: E402
from agent_broker_server import _load_projects, load_bound_broker  # noqa: E402
from agent_profiles import profile_digest  # noqa: E402
from host_runtime_config import HostRuntimeError, PROJECT, TARGET  # noqa: E402
from host_runtime_operations import _read_private_json, _save_private_json  # noqa: E402
from host_operation_fixtures import fake_registered_manager, write_registered_fixture  # noqa: E402


TASK_ID = "a" * 32
SOURCE_DIGEST = "b" * 64
CONTEXT_NONCE = "c" * 64
LEASE_ID = "d" * 64
PROFILE_ID = "codex-host-test"


class FixtureProfiles:
    def __init__(self, profile):
        self.profile = profile

    def require(self, project, profile_id):
        if project != PROJECT or profile_id != PROFILE_ID:
            raise HostRuntimeError("PROFILE_NOT_FOUND")
        return dict(self.profile)


class FixtureTasks:
    """Minimum private task-state surface expected from AgentTaskManager."""

    def __init__(self, root: Path, profile: dict, identity: dict):
        self.root = root / "agent-tasks"
        self.root.mkdir(mode=0o700, parents=True)
        self.tasks_dir = self.root / "tasks"
        self.tasks_dir.mkdir(mode=0o700)
        self.sources_dir = self.root / "sources"
        self.sources_dir.mkdir(mode=0o700)
        self.receipts_dir = self.root / "receipts"
        self.receipts_dir.mkdir(mode=0o700)
        self.lock = self.root / "agent-tasks.lock"
        self.profiles = FixtureProfiles(profile)
        self.identity = dict(identity)
        self.profile = profile
        self._write_state()
        control = self.sources_dir / TASK_ID / "source" / ".agent-control"
        control.mkdir(mode=0o700, parents=True)
        self.lease_path = control / "broker-lease.json"
        self.write_lease()

    def _write_state(self, **updates):
        started_at = datetime.now(timezone.utc).isoformat()
        immutable = {
            "project": PROJECT,
            "task_id": TASK_ID,
            "profile_id": PROFILE_ID,
            "profile_digest": profile_digest(self.profile),
            "source_digest": SOURCE_DIGEST,
            "host_capabilities": self.profile["host_capabilities"],
            "timeout_minutes": 30,
        }
        state = {
            "project": PROJECT,
            "task_id": TASK_ID,
            "state": "running",
            "immutable": immutable,
            "broker_context_nonce": CONTEXT_NONCE,
            "execution_identity": dict(self.identity),
            "started_at": started_at,
            **updates,
        }
        _save_private_json(self.tasks_dir / (TASK_ID + ".json"), state)

    def write_lease(self, **updates):
        now = datetime.now(timezone.utc)
        lease = {
            "version": 1,
            "lease_id": LEASE_ID,
            "project": PROJECT,
            "task_id": TASK_ID,
            "profile_id": PROFILE_ID,
            "profile_digest": profile_digest(self.profile),
            "source_digest": SOURCE_DIGEST,
            "host_capabilities": self.profile["host_capabilities"],
            "context_nonce": CONTEXT_NONCE,
            "identity": dict(self.identity),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            **updates,
        }
        if self.lease_path.exists():
            self.lease_path.unlink()
        _save_private_json(self.lease_path, lease)

    def _load(self, project, task_id):
        if project != PROJECT or task_id != TASK_ID:
            raise HostRuntimeError("TASK_NOT_FOUND")
        return _read_private_json(self.tasks_dir / (task_id + ".json"))

    def get(self, project, task_id):
        state = self._load(project, task_id)
        immutable = state["immutable"]
        return {
            "project": project,
            "task_id": task_id,
            "profile_id": immutable["profile_id"],
            "source_digest": immutable["source_digest"],
            "state": state["state"],
        }


class FixtureSupervisor:
    def __init__(self, identity):
        self.identity = dict(identity)
        self.observation = {
            "unit": identity["unit"],
            "load_state": "loaded",
            "transient": True,
            "invocation_id": identity["invocation_id"],
            "cgroup": identity["cgroup"],
            "pid": identity["pid"],
            "active_state": "active",
            "sub_state": "running",
        }
        self.verified_identity = None

    def observe(self, unit):
        if unit != self.identity["unit"]:
            raise HostRuntimeError("AGENT_BROKER_BINDING_UNVERIFIED")
        return dict(self.observation)

    def verify_binding(self, identity, *, peer_pid=None):
        self.verified_identity = dict(identity) if isinstance(identity, dict) else identity
        return identity == self.identity and peer_pid == os.getpid()


class AgentHostBrokerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-host-broker-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.fixture = write_registered_fixture(self.home)
        self.host_state = self.home / "host-state"
        self.host, self.runner, _ = fake_registered_manager(
            self.fixture["projects"], self.fixture["config"], self.host_state)
        self.identity = {
            "task_id": TASK_ID,
            "unit": "personal-ops-agent-" + TASK_ID + ".service",
            "invocation_id": "1" * 32,
            "cgroup": "/user.slice/user-1003.slice/user@1003.service/app.slice/agent.service",
            "boot_id": "22222222-2222-2222-2222-222222222222",
            "uid": os.getuid(),
            "pid": 43210,
            "start_ticks": 9001,
            "wrapper_pid": 43209,
            "wrapper_start_ticks": 9000,
        }
        self.profile = {
            "project": PROJECT,
            "provider": "codex",
            "model": "gpt-5.6",
            "reasoning_effort": "high",
            "host_capabilities": [{
                "target": TARGET,
                "actions": ["docker.inspect", "compose.restart", "compose.stop"],
            }],
        }
        self.tasks = FixtureTasks(self.home, self.profile, self.identity)
        self.supervisor = FixtureSupervisor(self.identity)
        self.broker = AgentHostBroker(
            self.tasks, self.host, supervisor=self.supervisor)

    def operation_files(self):
        folder = self.host_state / "registered" / "operations"
        return list(folder.glob("*.json")) if folder.exists() else []

    def test_missing_verified_supervisor_binding_closes_before_host_access(self):
        broker = AgentHostBroker(self.tasks, self.host)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
            broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.calls, [])

    def test_private_lease_requires_running_task_consumed_approval_and_current_identity(self):
        cases = (
            ("task-state", {"state": "outcome_unknown"}, {}, "AGENT_TASK_NOT_RUNNING"),
            ("source", {}, {"source_digest": "e" * 64}, "AGENT_BROKER_LEASE_INVALID"),
            ("nonce", {}, {"context_nonce": "f" * 64}, "AGENT_BROKER_LEASE_INVALID"),
        )
        for name, state_update, lease_update, code in cases:
            with self.subTest(name=name):
                self.tasks._write_state(**state_update)
                self.tasks.write_lease(**lease_update)
                with self.assertRaisesRegex(HostRuntimeError, code):
                    self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
                self.tasks._write_state()
                self.tasks.write_lease()
        _save_private_json(self.tasks.receipts_dir / (TASK_ID + ".json"), {"unconsumed": True})
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_APPROVAL_NOT_CONSUMED"):
            self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.calls, [])

    def test_expired_or_changed_systemd_invocation_is_rejected(self):
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.tasks.write_lease(expires_at=expired)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_LEASE_EXPIRED"):
            self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.tasks.write_lease()
        self.supervisor.observation["invocation_id"] = "9" * 32
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
            self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.calls, [])

    def test_changed_live_pid_or_cgroup_is_rejected_before_host_access(self):
        for field, value in (
                ("pid", self.identity["pid"] + 1),
                ("cgroup", self.identity["cgroup"] + "/foreign")):
            with self.subTest(field=field):
                self.supervisor.observation[field] = value
                with self.assertRaisesRegex(
                        HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
                    self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
                self.supervisor.observation[field] = self.identity[field]
        self.assertEqual(self.runner.calls, [])

    def test_supervisor_verifies_full_persisted_identity_after_observation(self):
        result = self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(result["target"], TARGET)
        self.assertEqual(self.supervisor.verified_identity, self.identity)

    def test_private_context_binds_one_task_and_does_not_replace_live_authority(self):
        context_path = (self.tasks.sources_dir / TASK_ID / "source" / ".agent-control"
                        / "broker-context.json")
        _save_private_json(context_path, {
            "version": 1, "project": PROJECT, "task_id": TASK_ID,
            "context_nonce": CONTEXT_NONCE,
        })
        bound = load_bound_broker(
            context_path, task_manager=self.tasks, host_operations=self.host,
            supervisor=self.supervisor)
        result = bound.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(result["target"], TARGET)
        prior_calls = list(self.runner.calls)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_CONTEXT_MISMATCH"):
            bound.inspect("other", TASK_ID, TARGET, "docker")

        self.supervisor.observation["invocation_id"] = "9" * 32
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
            bound.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.calls, prior_calls)

    def test_context_json_or_argv_cannot_select_an_untrusted_state_root(self):
        control = self.tasks.sources_dir / TASK_ID / "source" / ".agent-control"
        canonical = control / "broker-context.json"
        payload = {
            "version": 1, "project": PROJECT, "task_id": TASK_ID,
            "context_nonce": CONTEXT_NONCE,
        }
        _save_private_json(canonical, payload)
        forged = self.home / "forged-context.json"
        _save_private_json(forged, payload)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_CONTEXT_INVALID"):
            load_bound_broker(
                forged, task_manager=self.tasks, host_operations=self.host,
                supervisor=self.supervisor)

        canonical.unlink()
        _save_private_json(canonical, {**payload, "agent_state_dir": str(self.tasks.root)})
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_CONTEXT_INVALID"):
            load_bound_broker(
                canonical, task_manager=self.tasks, host_operations=self.host,
                supervisor=self.supervisor)

    def test_readonly_inspection_is_typed_and_never_inherits_legacy_scope(self):
        result = self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual((result["project"], result["area"]), (PROJECT, "docker"))
        self.assertEqual(set(result["services"]), {"api", "worker", "db"})
        self.assertEqual(self.runner.effects, [])
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_HOST_SCOPE_DISABLED"):
            self.broker.inspect(PROJECT, TASK_ID, TARGET, "database")
        payload = self.fixture["payload"]
        self.fixture["config"].write_text(json.dumps(payload["legacy"]))
        with self.assertRaisesRegex(HostRuntimeError, "REGISTRY_NOT_CONFIGURED"):
            self.broker.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.effects, [])

    async def test_prepare_waits_for_real_web_approval_then_executes_once(self):
        pending = self.broker.prepare(
            PROJECT, TASK_ID, TARGET, "compose.restart", {"services": ["api"]}, "restart")
        self.assertEqual(pending["state"], "waiting_host_approval")
        self.assertTrue(pending["approval_required"])
        operation_id = pending["operation_id"]
        stored = self.host.get(PROJECT, TARGET, operation_id)
        self.assertEqual(stored["state"], "pending_host_approval")
        self.assertTrue(stored["request_id"].startswith("agent:" + TASK_ID + ":"))
        self.assertEqual(self.runner.effects, [])
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.broker.prepare(
                PROJECT, TASK_ID, TARGET, "compose.restart", {"services": ["worker"]},
                "restart")

        still_waiting = await self.broker.execute(PROJECT, TASK_ID, operation_id)
        self.assertEqual(still_waiting["state"], "waiting_host_approval")
        self.assertEqual(self.runner.effects, [])
        self.host.approve_web(
            PROJECT, TARGET, operation_id=operation_id,
            plan_digest=pending["plan_digest"], source_digest=pending["source_digest"],
            confirmation=operation_id)
        started = await self.broker.execute(PROJECT, TASK_ID, operation_id)
        self.assertIn(started["state"], {"queued", "running", "succeeded"})
        for worker in list(self.host._workers.values()):
            await __import__("asyncio").to_thread(worker.join, 10)
        done = await self.broker.execute(PROJECT, TASK_ID, operation_id)
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(self.runner.effects, ["restart"])

    async def test_foreign_operation_and_forged_receipt_never_gain_authority(self):
        foreign = self.host.prepare(
            PROJECT, TARGET, action="compose.restart", parameters={"services": ["api"]},
            request_id="foreign-plan")
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_HOST_OPERATION_NOT_OWNED"):
            await self.broker.execute(PROJECT, TASK_ID, foreign["operation_id"])
        self.host.reject_web(PROJECT, TARGET, operation_id=foreign["operation_id"],
                             confirmation=foreign["operation_id"])

        pending = self.broker.prepare(
            PROJECT, TASK_ID, TARGET, "compose.restart", {"services": ["api"]}, "owned")
        operation_id = pending["operation_id"]
        self.host.approve_web(
            PROJECT, TARGET, operation_id=operation_id,
            plan_digest=pending["plan_digest"], source_digest=pending["source_digest"],
            confirmation=operation_id)
        receipt = self.host_state / "registered" / "receipts" / (operation_id + ".json")
        forged = _read_private_json(receipt)
        forged["nonce"] = "0" * 64
        receipt.unlink()
        _save_private_json(receipt, forged)
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_HOST_APPROVAL"):
            await self.broker.execute(PROJECT, TASK_ID, operation_id)
        self.assertEqual(self.runner.effects, [])

    async def test_outcome_unknown_is_observable_but_never_automatically_retried(self):
        pending = self.broker.prepare(
            PROJECT, TASK_ID, TARGET, "compose.restart", {"services": ["api"]}, "unknown")
        path = self.host_state / "registered" / "operations" / (pending["operation_id"] + ".json")
        state = _read_private_json(path)
        state.update(state="outcome_unknown", phase="RECOVER", code="PROCESS_INTERRUPTED")
        path.unlink()
        _save_private_json(path, state)
        observed = self.broker.get(PROJECT, TASK_ID, pending["operation_id"])
        self.assertEqual(observed["state"], "outcome_unknown")
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_HOST_OUTCOME_UNKNOWN"):
            await self.broker.execute(PROJECT, TASK_ID, pending["operation_id"])
        self.assertEqual(self.runner.effects, [])

    def test_arbitrary_argv_sql_and_empty_profile_caps_are_denied(self):
        with self.assertRaisesRegex(HostRuntimeError, "UNSUPPORTED_SCOPE"):
            self.broker.prepare(
                PROJECT, TASK_ID, TARGET, "compose.restart", {"argv": ["sh", "-c", "id"]},
                "argv")
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_HOST_SCOPE_DISABLED"):
            self.broker.prepare(PROJECT, TASK_ID, TARGET, "db.execute", {"sql": "DROP TABLE x"},
                                "sql")
        with self.assertRaisesRegex(HostRuntimeError, "POLICY_DENIED"):
            self.broker.prepare(
                PROJECT, TASK_ID, TARGET, "compose.stop", {"services": ["db"]}, "stop-db")
        self.profile["host_capabilities"] = []
        self.tasks = FixtureTasks(self.home / "deny", self.profile, self.identity)
        denied = AgentHostBroker(self.tasks, self.host, supervisor=self.supervisor)
        with self.assertRaisesRegex(HostRuntimeError, "AGENT_HOST_SCOPE_DISABLED"):
            denied.inspect(PROJECT, TASK_ID, TARGET, "docker")
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.operation_files(), [])


BROKER_LAUNCHER = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from agent_broker_server import serve
class Broker:
    def inspect(self, project, task_id, target, area):
        return {"project": project, "task_id": task_id, "target": target, "area": area}
    def prepare(self, project, task_id, target, action, parameters, request_id):
        return {"state": "waiting_host_approval", "operation_id": "e" * 32}
    async def execute(self, project, task_id, operation_id):
        return {"state": "queued", "operation_id": operation_id}
    def get(self, project, task_id, operation_id):
        return {"state": "running", "operation_id": operation_id}
asyncio.run(serve(Broker()))
"""


class BrokerStdioTest(unittest.IsolatedAsyncioTestCase):
    def test_projects_registry_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory(prefix="broker-projects-") as folder:
            path = Path(folder) / "projects.json"
            path.write_text(
                '{"projects":{"tossApp":"/tmp"},'
                '"projects":{"tossApp":"/tmp"}}')
            path.chmod(0o600)
            with self.assertRaisesRegex(HostRuntimeError, "AGENT_BROKER_BINDING_UNVERIFIED"):
                _load_projects(path)

    async def test_private_stdio_catalog_and_exact_argument_boundary(self):
        with tempfile.TemporaryDirectory(prefix="broker-stdio-") as folder:
            root = Path(folder)
            launcher = root / "launcher.py"
            launcher.write_text(BROKER_LAUNCHER)
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
                   if key in os.environ}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            params = StdioServerParameters(
                command=sys.executable, args=[str(launcher), str(MODULE_DIR)], env=env)
            with (root / "stderr.log").open("w") as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                        self.assertEqual(set(tools), {
                            "inspect_agent_host", "prepare_agent_host_operation",
                            "execute_agent_host_operation", "get_agent_host_operation",
                        })
                        for tool in tools.values():
                            self.assertFalse(tool.inputSchema["additionalProperties"])
                        result = await client.call_tool("inspect_agent_host", {
                            "project": PROJECT, "task_id": TASK_ID,
                            "target": TARGET, "area": "docker",
                        })
                        self.assertFalse(result.isError, result.content)
                        self.assertEqual(result.structuredContent["area"], "docker")
                        extra = await client.call_tool("inspect_agent_host", {
                            "project": PROJECT, "task_id": TASK_ID,
                            "target": TARGET, "area": "docker", "approved": True,
                        })
                        self.assertTrue(extra.isError)

    def test_standalone_cli_has_no_context_file_authority_fallback(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_DIR / "agent_broker_server.py")],
            text=True, capture_output=True, timeout=10, check=False,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("AGENT_BROKER_BINDING_UNVERIFIED", result.stderr)


if __name__ == "__main__":
    unittest.main()
