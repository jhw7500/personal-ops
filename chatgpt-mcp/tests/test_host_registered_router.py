"""Registered host-operation MCP tools through the real MCP SDK and stdio server.

The server runs from a small launcher that only redirects the host config path to a
temporary file and, for the end-to-end case, injects a fake runner + Compose adapter.
No Docker command, real host configuration, service or database is touched.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODULE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(TESTS_DIR))
from host_actions import ACTIONS  # noqa: E402
from host_operation_fixtures import (  # noqa: E402
    FakeHostRunner, fake_registered_manager, write_registered_fixture,
)
from project_state import ProjectState  # noqa: E402
import host_registered_tools  # noqa: E402
from host_registered_tools import HOST_REGISTERED_TOOL_SPECS, RegisteredHostTools  # noqa: E402
from host_runtime_config import PROJECT, TARGET, HostRuntimeError  # noqa: E402
from host_runtime_tools import HOST_RUNTIME_TOOL_SPECS  # noqa: E402

REGISTERED = {"list_host_targets", "inspect_host_target", "prepare_host_operation",
              "execute_host_operation", "read_host_operation_events"}
LAUNCHER = """
import asyncio, json, sys
from pathlib import Path
setup = json.loads(Path(sys.argv[1]).read_text())
sys.path[:0] = [setup["module_dir"], setup["tests_dir"]]
if setup.get("hide_registry"):
    sys.modules["host_registry"] = None  # Simulates a runtime without the registry module.
import server
if server.host_registered_tools is not None:
    import test_host_registered_router as harness
    harness.configure_child(server.host_registered_tools, setup)
asyncio.run(server.serve_with_shutdown(server.load_projects(Path(setup["projects"]))))
"""


class PersistentRunner(FakeHostRunner):
    """Child-process fake runner: every call is mirrored to a file the test reads."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.lock = threading.Lock()

    def run(self, argv, **kwargs):
        try:
            return super().run(argv, **kwargs)
        finally:
            with self.lock:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"effects": self.effects,
                                                 "keys": [call["key"] for call in self.calls]}))
                temporary.replace(self.path)


def configure_child(tools, setup):
    """Runs inside the launched server before serving."""
    tools.CONFIG_PATH = Path(setup["config"])
    if setup["fake"]:
        runner = PersistentRunner(Path(setup["effects"]))

        def create(projects, state_dir=None):
            return fake_registered_manager(projects, Path(setup["config"]), state_dir, runner)[0]
        tools.create_registered_manager = create


def object_schemas(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from object_schemas(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from object_schemas(value)


class RegisteredStdioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-registered-")
        self.addAsyncCleanup(asyncio.to_thread, self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.fx = write_registered_fixture(self.home)
        self.state = self.home / "xdg-state"
        self.host_state = self.state / "chatgpt-coding-mcp" / "host-runtime"
        self.effects_path = self.home / "effects.json"

    @asynccontextmanager
    async def child(self, *, fake, hide_registry=False):
        launcher = self.home / "launch_server.py"
        launcher.write_text(LAUNCHER)
        setup = self.home / ("setup-fake.json" if fake else "setup.json")
        setup.write_text(json.dumps({
            "module_dir": str(MODULE_DIR), "tests_dir": str(TESTS_DIR), "fake": fake,
            "config": str(self.fx["config"]), "projects": str(self.fx["projects_file"]),
            "effects": str(self.effects_path), "hide_registry": hide_registry}))
        params = StdioServerParameters(command=sys.executable, args=[str(launcher), str(setup)],
                                       env=dict(os.environ, XDG_STATE_HOME=str(self.state)))
        with (self.home / "server.log").open("a") as log:
            async with asyncio.timeout(120):
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        yield client

    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        text = "\n".join(item.text for item in result.content if item.type == "text")
        self.assertNotIn(str(self.home), text)
        if result.isError:
            return True, text
        return False, result.structuredContent

    async def refused(self, client, name, arguments):
        is_error, value = await self.call(client, name, arguments)
        self.assertTrue(is_error, value)
        return value

    def child_effects(self):
        if not self.effects_path.exists():
            return []
        return json.loads(self.effects_path.read_text())["effects"]

    def operation_files(self):
        folder = self.host_state / "registered" / "operations"
        return sorted(path.name for path in folder.glob("*.json")) if folder.exists() else []

    async def test_tools_are_strict_and_listed_beside_unchanged_legacy_tools(self):
        async with self.child(fake=False) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertTrue(REGISTERED <= listed.keys())
        self.assertIn("read_file", listed)
        for name in REGISTERED:
            with self.subTest(name=name):
                schema = listed[name].inputSchema
                self.assertEqual(schema["properties"]["project"]["enum"], [PROJECT, "alpha"])
                for nested in object_schemas(schema):
                    self.assertIs(nested.get("additionalProperties"), False, nested)
                if name != "list_host_targets":
                    self.assertEqual(schema["properties"]["target"],
                                     {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,62}$"})
        self.assertEqual(listed["list_host_targets"].inputSchema["required"], ["project"])
        self.assertEqual(listed["inspect_host_target"].inputSchema["properties"]["area"]["enum"],
                         ["docker"])
        prepare = listed["prepare_host_operation"].inputSchema
        self.assertEqual(prepare["required"],
                         ["project", "target", "action", "parameters", "request_id"])
        self.assertEqual(prepare["properties"]["action"]["enum"], list(ACTIONS))
        self.assertEqual(set(prepare["properties"]["parameters"]["properties"]),
                         {"services", "profile"})
        events = listed["read_host_operation_events"].inputSchema
        self.assertEqual(events["required"], ["project", "target", "operation_id"])
        self.assertEqual((events["properties"]["limit"]["minimum"],
                          events["properties"]["limit"]["maximum"]), (1, 200))
        for name in ("list_host_targets", "inspect_host_target", "read_host_operation_events"):
            self.assertTrue(listed[name].annotations.readOnlyHint, name)
            self.assertFalse(listed[name].annotations.destructiveHint, name)
        for name in ("prepare_host_operation", "execute_host_operation"):
            self.assertFalse(listed[name].annotations.readOnlyHint, name)
            self.assertTrue(listed[name].annotations.destructiveHint, name)
        # The seven legacy tools keep their exact schemas, titles and annotations.
        for spec in HOST_RUNTIME_TOOL_SPECS:
            with self.subTest(legacy=spec["name"]):
                expected = copy.deepcopy(spec["inputSchema"])
                expected["properties"]["project"]["enum"] = [PROJECT]
                tool = listed[spec["name"]]
                self.assertEqual(tool.inputSchema, expected)
                self.assertEqual(tool.annotations.model_dump(exclude_none=True), spec["annotations"])
                self.assertEqual((tool.title, tool.description), (spec["title"], spec["description"]))

    async def test_production_wiring_needs_the_verified_runtime_and_rejects_bad_input(self):
        async with self.child(fake=False) as client:
            is_error, listing = await self.call(client, "list_host_targets", {"project": PROJECT})
            self.assertFalse(is_error, listing)
            self.assertEqual((listing["enabled"], listing["reason"]), (False, "UNSAFE_RUNTIME"))
            self.assertEqual([row["target"] for row in listing["targets"]], [TARGET])
            self.assertNotIn(str(self.fx["secrets"]), json.dumps(listing))
            is_error, alpha = await self.call(client, "list_host_targets", {"project": "alpha"})
            self.assertEqual(alpha["targets"], [])
            good = {"project": PROJECT, "target": TARGET, "action": "compose.restart",
                    "parameters": {"services": ["api"]}, "request_id": "prod-1"}
            self.assertEqual(await self.refused(client, "prepare_host_operation", good),
                             "UNSAFE_RUNTIME")
            self.assertEqual(await self.refused(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": "0" * 32}), "UNSAFE_RUNTIME")
            self.assertEqual(await self.refused(client, "inspect_host_target", {
                "project": PROJECT, "target": TARGET, "area": "docker"}), "UNSAFE_RUNTIME")
            self.assertEqual(await self.refused(client, "read_host_operation_events", {
                "project": PROJECT, "target": TARGET, "operation_id": "0" * 32}),
                "OPERATION_NOT_FOUND")
            for name, arguments in {
                    "extra top-level field": {**good, "approved": True},
                    "unknown parameter": {**good, "parameters": {"services": ["api"], "force": True}},
                    "unknown project": {**good, "project": "ghost"},
                    "target path": {**good, "target": "../tarororo-test"},
                    "unknown action": {**good, "action": "compose.prune"},
                    "missing request": {key: value for key, value in good.items()
                                        if key != "request_id"},
                    "socket in parameters": {**good, "parameters": {"services": ["api"],
                                                                    "socket": "/var/run/docker.sock"}}
            }.items():
                with self.subTest(name):
                    await self.refused(client, "prepare_host_operation", arguments)
            self.fx["config"].unlink()
            is_error, missing = await self.call(client, "list_host_targets", {"project": PROJECT})
            self.assertEqual((missing["enabled"], missing["reason"], missing["targets"]),
                             (False, "HOST_CONFIG_MISSING", []))
        self.assertEqual(self.operation_files(), [])
        self.assertFalse((self.host_state / "registered" / "snapshots").exists()
                         and any((self.host_state / "registered" / "snapshots").iterdir()))

    async def test_disabled_registry_and_scope_refusals_have_no_effect(self):
        payload = json.loads(self.fx["config"].read_text())
        async with self.child(fake=True) as client:
            is_error, listing = await self.call(client, "list_host_targets", {"project": PROJECT})
            self.assertEqual((listing["enabled"], listing["reason"]), (True, None))
            self.assertEqual(listing["targets"][0]["actions"], [
                "compose.build", "compose.apply", "compose.up", "compose.restart",
                "compose.stop", "compose.exec"])
            self.assertEqual(listing["targets"][0]["exec_profiles"],
                             [{"profile": "node-version", "service": "api"}])
            is_error, inspected = await self.call(client, "inspect_host_target", {
                "project": PROJECT, "target": TARGET, "area": "docker"})
            self.assertFalse(is_error, inspected)
            self.assertEqual(set(inspected["services"]), {"api", "worker", "db"})
            self.assertNotIn("hunter2", json.dumps(inspected))
            base = {"project": PROJECT, "target": TARGET, "action": "compose.restart",
                    "parameters": {"services": ["api"]}, "request_id": "scope-1"}
            for arguments, code in (
                    ({**base, "parameters": {"services": ["ghost"]}}, "UNSUPPORTED_SCOPE"),
                    ({**base, "parameters": {"profile": "node-version"}}, "UNSUPPORTED_SCOPE"),
                    ({**base, "action": "compose.down", "parameters": {}}, "POLICY_DENIED"),
                    ({**base, "parameters": {"services": ["db"]}}, "POLICY_DENIED"),
                    ({**base, "project": "alpha"}, "UNSUPPORTED_SCOPE"),
                    ({**base, "target": "other-target"}, "UNSUPPORTED_SCOPE")):
                with self.subTest(arguments=arguments):
                    self.assertEqual(await self.refused(client, "prepare_host_operation",
                                                        arguments), code)
            disabled = dict(payload, enabled=False)
            self.fx["config"].write_text(json.dumps(disabled))
            is_error, off = await self.call(client, "list_host_targets", {"project": PROJECT})
            self.assertEqual((off["enabled"], off["reason"], off["targets"]),
                             (False, "HOST_RUNTIME_DISABLED", []))
            self.assertEqual(await self.refused(client, "prepare_host_operation", base),
                             "HOST_RUNTIME_DISABLED")
            self.assertEqual(await self.refused(client, "inspect_host_target", {
                "project": PROJECT, "target": TARGET, "area": "docker"}), "HOST_RUNTIME_DISABLED")
            self.fx["config"].write_text("{not json")
            is_error, broken = await self.call(client, "list_host_targets", {"project": PROJECT})
            self.assertEqual((broken["enabled"], broken["reason"]), (False, "INVALID_HOST_CONFIG"))
            self.assertEqual(await self.refused(client, "prepare_host_operation", base),
                             "INVALID_HOST_CONFIG")
        self.assertEqual(self.child_effects(), [])
        self.assertEqual(self.operation_files(), [])

    async def test_mcp_plan_web_approval_mcp_execute_events_and_routing(self):
        admin_ops, admin_runner, _ = fake_registered_manager(
            self.fx["projects"], self.fx["config"], self.host_state)
        from admin_server import create_server
        admin = create_server(self.fx["projects"], self.home / "admin-state",
                              token_path=self.home / "token" / "admin", port=0,
                              host_ops_factory=lambda _, **kwargs: object(),
                              registered_ops_factory=lambda _, **kwargs: admin_ops)
        thread = threading.Thread(target=admin.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (admin.shutdown(), admin.server_close(), thread.join(5)))
        token = (self.home / "token" / "admin").read_text().strip()
        origin = f"http://127.0.0.1:{admin.server_port}"

        def web(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", admin.server_port, timeout=10)
            sent = {"Authorization": "Bearer " + token, **(headers or {})}
            if body is not None:
                sent["Content-Type"] = "application/json"
            conn.request(method, path, None if body is None else json.dumps(body), sent)
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            return response.status, data

        receipts = self.host_state / "registered" / "receipts"
        scope = f"?project={PROJECT}&target={TARGET}"
        async with self.child(fake=True) as client:
            is_error, plan = await self.call(client, "prepare_host_operation", {
                "project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["api"]}, "request_id": "e2e-1"})
            self.assertFalse(is_error, plan)
            op = plan["operation_id"]
            self.assertEqual((plan["kind"], plan["state"]), ("registered", "pending_host_approval"))
            self.assertEqual(plan["approval_url"], "https://cantopsbuildserver.tail661d29.ts.net:8443"
                                                   "/host-operations?project=tossApp"
                                                   "&target=tarororo-test&operation_id=" + op)
            self.assertEqual(plan["impact"]["preserved"], ["worker", "db"])
            # Same request ID and input: the same plan, never a second one.
            is_error, again = await self.call(client, "prepare_host_operation", {
                "project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["api"]}, "request_id": "e2e-1"})
            self.assertEqual(again["operation_id"], op)
            self.assertEqual(await self.refused(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op}), "HOST_APPROVAL_REQUIRED")
            self.assertEqual(self.child_effects(), [])
            body = {"operation_id": op, "plan_digest": plan["plan_digest"],
                    "source_digest": plan["source_digest"], "confirmation": op,
                    "admin_token": token}
            # Untrusted approvals: no receipt, no effect.
            for headers in ({}, {"Origin": "https://evil.test"},
                            {"Origin": origin, "Forwarded": "for=192.0.2.1"}):
                self.assertEqual(web("POST", "/api/registered-approve" + scope, body, headers)[0],
                                 403)
            self.assertEqual(web("POST", "/api/registered-approve" + scope,
                                  {**body, "admin_token": "stale"}, {"Origin": origin})[0], 403)
            self.assertEqual(list(receipts.iterdir()), [])
            self.assertEqual(await self.refused(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op}), "HOST_APPROVAL_REQUIRED")
            self.assertEqual(self.child_effects(), [])
            status, listing = web("GET", "/api/registered-operations" + scope)
            self.assertEqual([row["operation_id"] for row in listing["operations"]], [op])
            status, approved = web("POST", "/api/registered-approve" + scope, body,
                                    {"Origin": origin})
            self.assertEqual((status, approved["state"]), (200, "approved"), approved)
            self.assertEqual(len(list(receipts.iterdir())), 1)
            self.assertEqual(self.child_effects(), [])  # Approval never executes.
            status, second = web("POST", "/api/registered-approve" + scope, body,
                                  {"Origin": origin})
            self.assertEqual((status, second.get("code")), (400, "NOT_PENDING_APPROVAL"))
            is_error, started = await self.call(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op})
            self.assertFalse(is_error, started)
            self.assertIn(started["state"], ("queued", "running", "succeeded"))
            final = None
            for _ in range(200):
                is_error, page = await self.call(client, "read_host_operation_events", {
                    "project": PROJECT, "target": TARGET, "operation_id": op, "limit": 200})
                if page["state"] in ("succeeded", "failed", "outcome_unknown", "rejected"):
                    final = page
                    break
                await asyncio.sleep(0.05)
            self.assertIsNotNone(final)
            self.assertEqual(final["state"], "succeeded", final)
            codes = [event["code"] for event in final["events"]]
            for code in ("PLAN_CREATED", "HOST_APPROVED", "OPERATION_QUEUED",
                         "OPERATION_STARTED", "COMPOSE_RESTART_STARTED",
                         "COMPOSE_RESTART_COMPLETED", "POSTCHECK_VERIFIED", "OPERATION_SUCCEEDED"):
                self.assertIn(code, codes)
            self.assertEqual(self.child_effects(), ["restart"])
            self.assertEqual(list(receipts.iterdir()), [])  # Consumed exactly once.
            is_error, page = await self.call(client, "read_host_operation_events", {
                "project": PROJECT, "target": TARGET, "operation_id": op, "cursor": 2, "limit": 3})
            self.assertEqual([event["cursor"] for event in page["events"]], [3, 4, 5])
            self.assertEqual((page["next_cursor"], page["more"]), (5, True))
            # A repeated execute observes; it never runs again.
            is_error, repeat = await self.call(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op})
            self.assertEqual(repeat["state"], "succeeded")
            self.assertEqual(self.child_effects(), ["restart"])
            # Legacy get_host_operation routes by the verified backend kind.
            is_error, routed = await self.call(client, "get_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op})
            self.assertFalse(is_error, routed)
            self.assertEqual((routed["kind"], routed["state"]), ("registered", "succeeded"))
            self.assertEqual(routed["result"]["verifications"], plan["verifications"])
            await self.refused(client, "get_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": "1" * 32})
            legacy_dir = self.host_state / "operations"
            shutil.copy2(self.host_state / "registered" / "operations" / (op + ".json"),
                         legacy_dir / (op + ".json"))
            self.assertEqual(await self.refused(client, "get_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": op}),
                "AMBIGUOUS_OPERATION_ID")
            # Reject path: a rejected plan cannot be executed.
            is_error, other = await self.call(client, "prepare_host_operation", {
                "project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["worker"]}, "request_id": "e2e-2"})
            self.assertFalse(is_error, other)
            status, rejected = web("POST", "/api/registered-reject" + scope, {
                "operation_id": other["operation_id"], "confirmation": other["operation_id"],
                "admin_token": token}, {"Origin": origin})
            self.assertEqual((status, rejected["state"]), (200, "rejected"))
            is_error, refused = await self.call(client, "execute_host_operation", {
                "project": PROJECT, "target": TARGET, "operation_id": other["operation_id"]})
            self.assertEqual(refused["state"], "rejected")
            self.assertEqual(self.child_effects(), ["restart"])
        self.assertEqual(admin_runner.effects, [])
        activity = ProjectState(self.fx["projects"], self.state / "chatgpt-coding-mcp").list_activity(
            PROJECT)["events"]
        phases = {(row["tool"], row["phase"]) for row in activity}
        for tool in ("prepare_host_operation", "execute_host_operation",
                     "read_host_operation_events", "get_host_operation"):
            self.assertIn((tool, "started"), phases)
            self.assertIn((tool, "completed"), phases)
        self.assertIn(("execute_host_operation", "failed"), phases)

    async def test_registered_tools_are_unlisted_when_the_registry_module_is_missing(self):
        async with self.child(fake=False, hide_registry=True) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            self.assertFalse(REGISTERED & listed)
            self.assertTrue({spec["name"] for spec in HOST_RUNTIME_TOOL_SPECS} <= listed)
            is_error, text = await self.call(client, "prepare_host_operation", {
                "project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["api"]}, "request_id": "hidden-1"})
            self.assertTrue(is_error)
        self.assertEqual(self.operation_files(), [])


class RegisteredToolsUnitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="registered-tools-")
        self.addAsyncCleanup(asyncio.to_thread, self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.fx = write_registered_fixture(self.home)
        self.host_state = self.home / "state" / "host-runtime"
        self.ops, self.runner, _ = fake_registered_manager(
            self.fx["projects"], self.fx["config"], self.host_state)
        self.locks = {name: asyncio.Lock() for name in self.fx["projects"]}
        self.tools = RegisteredHostTools(self.fx["projects"], self.host_state, self.locks)
        patcher = patch.object(host_registered_tools, "create_registered_manager",
                               lambda projects, state_dir=None: self.ops)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def code(self, name, arguments):
        with self.assertRaises(ValueError) as caught:
            await self.tools.call(name, arguments)
        return str(caught.exception)

    async def test_arguments_are_exact_and_errors_are_codes(self):
        base = {"project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["api"]}, "request_id": "unit-1"}
        self.assertEqual(await self.code("prepare_host_operation", {**base, "extra": 1}),
                         "INVALID_HOST_ARGUMENTS")
        self.assertEqual(await self.code("prepare_host_operation", {
            key: value for key, value in base.items() if key != "parameters"}),
            "INVALID_HOST_ARGUMENTS")
        self.assertEqual(await self.code("read_host_operation_events", {
            "project": PROJECT, "target": TARGET, "operation_id": "0" * 32, "offset": 1}),
            "INVALID_HOST_ARGUMENTS")
        self.assertEqual(await self.code("prepare_host_operation", {**base, "project": "ghost"}),
                         "UNSUPPORTED_SCOPE")
        self.assertEqual(await self.code("prepare_host_operation", {**base, "target": "A/b"}),
                         "UNSUPPORTED_SCOPE")
        self.assertEqual(await self.code("prepare_host_operation", {
            **base, "parameters": ["api"]}), "UNSUPPORTED_SCOPE")
        self.assertEqual(await self.code("inspect_host_target", {
            "project": PROJECT, "target": TARGET, "area": "database"}), "UNSUPPORTED_SCOPE")
        self.assertEqual(await self.code("read_host_operation_events", {
            "project": PROJECT, "target": TARGET, "operation_id": "0" * 32, "limit": 201}),
            "INVALID_EVENT_QUERY")
        with patch.object(self.ops, "prepare", side_effect=RuntimeError("/secret/path boom")):
            self.assertEqual(await self.code("prepare_host_operation", base),
                             "HOST_OPERATION_FAILED")
        failure = HostRuntimeError("HOST_OPERATION_FAILED")
        failure.diagnostic_id = "ab" * 16
        with patch.object(self.ops, "prepare", side_effect=failure):
            self.assertEqual(await self.code("prepare_host_operation", base),
                             "HOST_OPERATION_FAILED diagnostic_id=" + "ab" * 16)
        with patch.object(self.ops, "prepare", side_effect=HostRuntimeError("bad code /tmp/x")):
            self.assertEqual(await self.code("prepare_host_operation", base),
                             "HOST_OPERATION_FAILED")
        with patch.object(self.ops, "prepare", side_effect=PermissionError(13, "denied", "/x")):
            self.assertEqual(await self.code("prepare_host_operation", base), "STATE_UNAVAILABLE")
        self.assertEqual(self.runner.effects, [])

    async def test_prepare_holds_the_project_lock_like_legacy_prepare(self):
        base = {"project": PROJECT, "target": TARGET, "action": "compose.restart",
                "parameters": {"services": ["api"]}, "request_id": "unit-lock"}
        async with self.locks[PROJECT]:
            self.assertEqual(await self.code("prepare_host_operation", base), "PROJECT_BUSY")
        seen = []
        original = self.ops.prepare

        def observed(*args, **kwargs):
            seen.append(self.locks[PROJECT].locked())
            return original(*args, **kwargs)
        with patch.object(self.ops, "prepare", side_effect=observed):
            plan = await self.tools.call("prepare_host_operation", base)
        self.assertEqual(seen, [True])
        self.assertFalse(self.locks[PROJECT].locked())
        self.assertEqual(plan["state"], "pending_host_approval")
        self.assertEqual(self.runner.effects, [])

    async def test_get_host_operation_routes_by_backend_and_refuses_ambiguity(self):
        class Legacy:
            def __init__(self, folder):
                self.folder = folder
                self.calls = []

            def _path(self, operation_id):
                from host_runtime_operations import operation_path
                return operation_path(self.folder, operation_id)

            def get(self, project, target, operation_id):
                self.calls.append((project, target, operation_id))
                return {"legacy": True}

        legacy_dir = self.host_state / "operations"
        legacy_dir.mkdir(mode=0o700, exist_ok=True)
        legacy = Legacy(legacy_dir)
        plan = self.ops.prepare(PROJECT, TARGET, action="compose.restart",
                                parameters={"services": ["api"]}, request_id="route-1")
        op = plan["operation_id"]
        self.assertEqual(self.tools.registered_operation_path(op), self.ops._path(op))
        routed = self.tools.route_get(legacy, op)
        self.assertEqual((routed["kind"], routed["operation_id"]), ("registered", op))
        self.assertEqual(legacy.calls, [])
        self.assertEqual(self.tools.route_get(legacy, "2" * 32), {"legacy": True})
        self.assertEqual(legacy.calls, [(PROJECT, TARGET, "2" * 32)])
        (legacy_dir / (op + ".json")).write_text("{}")
        with self.assertRaises(HostRuntimeError) as caught:
            self.tools.route_get(legacy, op)
        self.assertEqual(caught.exception.code, "AMBIGUOUS_OPERATION_ID")
        with self.assertRaises(HostRuntimeError) as caught:
            self.tools.route_get(legacy, "../x")
        self.assertEqual(caught.exception.code, "INVALID_OPERATION_ID")

    def test_spec_names_match_the_router_and_known_tool_lists(self):
        import project_state
        self.assertEqual({spec["name"] for spec in HOST_REGISTERED_TOOL_SPECS}, REGISTERED)
        self.assertEqual(self.tools.names, REGISTERED)
        self.assertTrue(REGISTERED <= project_state.PROJECT_TOOLS)


if __name__ == "__main__":
    unittest.main()
