"""Offline UX019 scope regressions. No live client, token, OAuth or network."""
from __future__ import annotations

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

import toss_console_evidence as evidence
import toss_console_tools as tools
import toss_console_sdk_bridge as bridge
import toss_console_codex as managed
from tests import test_toss_console_tools as facade_fixture
from tests import test_toss_console_sdk_bridge as sdk_fixture
from tests import test_toss_console_codex as client_fixture

OLD = "01a13e37-27f3-7f9c-923d-6dbd690a5a09"
NEW = "01a151bd-4015-75cf-8275-fa6b6aef9a47"
OLD_TOOL = "get_toss_test_console_status"
NEW_TOOL = "get_toss_ux019_test_console_status"
SCOPE = {"project": "tossApp", "target": "tarororo-test"}


def page(deployment=NEW, **changes):
    item = {"miniAppId": 75257, "workspaceId": 93125, "appName": "tarororo",
            "deploymentId": deployment, "reviewStatus": "CREATED", "versionName": "offline-ux019",
            "isTested": True, "deployed": False, "apiToken": "DO_NOT_ECHO", **changes}
    return {"contents": [item], "currentPage": 0, "totalPage": 0}


def project(raw, deployment=NEW):
    return evidence.project_bundle_list(raw, tool_name="bundle_list",
        workspace_id="93125", miniapp_id="75257", deployment_id=deployment)


def arguments(deployment=NEW):
    return {"workspaceId": 93125, "miniAppId": 75257, "page": 0, "size": 20, "search": deployment}


class EvidenceTest(unittest.TestCase):
    def test_two_fixed_targets_keep_legacy_default(self):
        self.assertEqual(evidence.DEPLOYMENT_ID, OLD)
        for target in (OLD, NEW):
            with self.subTest(target=target):
                try:
                    result = project(page(target), target)
                except evidence.ConsoleEvidenceError as exc:
                    self.fail(f"approved fixed target rejected: {exc}")
                self.assertEqual(result["deployment_id"], target)
                self.assertTrue(result["test_bundle_ready"])
                self.assertFalse(result["remote_digest_verified"])
                self.assertNotIn("DO_NOT_ECHO", repr(result))

    def test_old_response_never_claims_ux019_ready(self):
        try:
            result = project(page(OLD))
        except evidence.ConsoleEvidenceError as exc:
            self.fail(f"UX019 projection missing: {exc}")
        self.assertEqual(result["deployment_id"], NEW)
        self.assertFalse(result["found"])
        self.assertFalse(result["test_bundle_ready"])

    def test_mixed_page_selects_requested_id_only(self):
        raw = page(OLD)
        raw["contents"].extend(page(NEW)["contents"])
        try:
            self.assertEqual(project(raw)["deployment_id"], NEW)
        except evidence.ConsoleEvidenceError as exc:
            self.fail(f"UX019 projection missing: {exc}")
        self.assertEqual(project(raw, OLD)["deployment_id"], OLD)

    def test_arbitrary_ids_are_still_rejected(self):
        for target in ("other", NEW.upper(), NEW + " ", True, None, [], {"id": NEW}):
            with self.subTest(target=target), self.assertRaises(evidence.ConsoleEvidenceError):
                project(page(), target)

    def test_ux019_wrong_scope_duplicate_or_partial_page_is_rejected(self):
        variants = [page(miniAppId=75258), page(workspaceId=93126), page(appName="other"),
                    {**page(), "totalPage": 1}, {**page(), "hasMore": True},
                    {**page(), "contents": page()["contents"] * 2}, page(isTested="true")]
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(evidence.ConsoleEvidenceError):
                project(raw)


class FacadeTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        try:
            return tools.TossConsoleReadOnlyTools(**kwargs)
        except TypeError as exc:
            self.fail(f"UX019 provider route missing: {exc}")

    async def call_new(self, facade):
        try:
            return await facade.call(NEW_TOOL, dict(SCOPE))
        except ValueError as exc:
            self.fail(f"UX019 tool route missing: {exc}")

    async def test_new_descriptor_cannot_accept_id_or_raw_payload(self):
        specs = {spec["name"]: spec for spec in tools.TOSS_CONSOLE_TOOL_SPECS}
        self.assertEqual(set(specs), {OLD_TOOL, NEW_TOOL})
        for spec in specs.values():
            self.assertEqual(set(spec["inputSchema"]["properties"]), {"project", "target"})
            self.assertFalse(spec["inputSchema"]["additionalProperties"])
            self.assertTrue(spec["annotations"]["readOnlyHint"])
            self.assertFalse(spec["annotations"]["destructiveHint"])
            self.assertFalse(spec["annotations"]["openWorldHint"])

    async def test_new_route_only_uses_its_factory(self):
        old = AsyncMock(return_value=facade_fixture.FakeProvider(response=page(OLD)))
        new = AsyncMock(return_value=facade_fixture.FakeProvider(response=page(NEW)))
        facade = self.make(provider_factory=old, ux019_provider_factory=new)
        out = await self.call_new(facade)
        self.assertTrue(out["test_bundle_ready"])
        self.assertEqual(out["deployment_id"], NEW)
        old.assert_not_awaited()
        new.assert_awaited_once_with()
        previous = await facade.call(OLD_TOOL, dict(SCOPE))
        self.assertEqual(previous["deployment_id"], OLD)
        old.assert_awaited_once_with()

    async def test_missing_new_factory_does_not_fall_back_to_old(self):
        old = AsyncMock(return_value=facade_fixture.FakeProvider())
        result = await self.call_new(self.make(provider_factory=old))
        self.assertEqual(result["code"], "CONSOLE_OAUTH_NOT_CONFIGURED")
        self.assertEqual(result["deployment_id"], NEW)
        old.assert_not_awaited()

    async def test_legacy_direct_provider_cannot_authorize_ux019(self):
        mixed = page(OLD)
        mixed["contents"].extend(page(NEW)["contents"])
        legacy = facade_fixture.FakeProvider(response=mixed)
        facade = self.make(provider=legacy)
        result = await self.call_new(facade)
        self.assertEqual(result.get("code"), "CONSOLE_OAUTH_NOT_CONFIGURED")
        self.assertEqual(result["deployment_id"], NEW)
        self.assertFalse(result["provider_verified"])
        self.assertEqual(legacy.calls, [])
        previous = await facade.call(OLD_TOOL, dict(SCOPE))
        self.assertTrue(previous["provider_verified"])
        self.assertEqual(previous["deployment_id"], OLD)
        self.assertEqual(len(legacy.calls), 1)

    async def test_ux019_factory_is_used_with_legacy_direct_provider_present(self):
        legacy = facade_fixture.FakeProvider(response=page(OLD))
        factory = AsyncMock(return_value=facade_fixture.FakeProvider(response=page(NEW)))
        facade = self.make(provider=legacy, ux019_provider_factory=factory)
        result = await self.call_new(facade)
        self.assertTrue(result["provider_verified"])
        self.assertTrue(result["found"])
        self.assertEqual(result["deployment_id"], NEW)
        factory.assert_awaited_once_with()
        self.assertEqual(legacy.calls, [])

    async def test_invalid_arguments_never_invoke_new_factory(self):
        factory = AsyncMock(return_value=facade_fixture.FakeProvider())
        facade = self.make(ux019_provider_factory=factory)
        for extra in ({"deployment_id": OLD}, {"raw_response": page()}, {"target": "production"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                await facade.call(NEW_TOOL, {**SCOPE, **extra})
        factory.assert_not_awaited()

    async def test_provider_wrong_version_does_not_report_ready(self):
        factory = AsyncMock(return_value=facade_fixture.FakeProvider(response=page(OLD)))
        result = await self.call_new(self.make(ux019_provider_factory=factory))
        self.assertFalse(result["found"])
        self.assertFalse(result["test_bundle_ready"])
        self.assertEqual(result["deployment_id"], NEW)

    async def test_new_failures_report_new_target_without_secret(self):
        factory = AsyncMock(side_effect=RuntimeError("Bearer private-token"))
        result = await self.call_new(self.make(ux019_provider_factory=factory))
        self.assertEqual(result["deployment_id"], NEW)
        self.assertEqual(result["code"], "CONSOLE_READ_FAILED")
        self.assertNotIn("private-token", repr(result))


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    def contract(self, **changes):
        kw = {"schema_sha256": bridge.REVIEWED_CONSOLE_SCHEMA_SHA256,
              "arguments": arguments(), "readonly_policy": "pinned_bundle_list", "deployment_id": NEW}
        kw.update(changes)
        try:
            return bridge.ReviewedBundleListContract(**kw)
        except TypeError as exc:
            self.fail(f"deployment-specific reviewed contract missing: {exc}")

    async def test_new_contract_sends_exact_search_and_keeps_copy(self):
        try:
            contract = self.contract()
        except bridge.ConsoleSessionContractError as exc:
            self.fail(f"approved UX019 contract rejected: {exc}")
        session = sdk_fixture.Session(tools=[{"name": "bundle_list", "inputSchema": sdk_fixture.OBSERVED_SCHEMA}])
        provider = bridge.ReviewedMcpBundleListProvider(session, contract, trusted_oauth_session=True)
        contract.arguments["search"] = OLD
        await provider.verify_catalog()
        await provider.bundle_list(workspace_id="93125", miniapp_id="75257")
        self.assertEqual(session.methods[-1], ("call_tool", "bundle_list", arguments()))

    async def test_crosswired_contract_or_noncanonical_query_rejected(self):
        for query in (arguments(OLD), {**arguments(), "miniAppId": True}, {**arguments(), "size": 100},
                      {**arguments(), "search": ""}, {**arguments(), "page": 1}):
            with self.subTest(query=query), self.assertRaises(bridge.ConsoleSessionContractError):
                self.contract(arguments=query)

    async def test_protocol_accepts_only_two_canonical_searches(self):
        client = managed._CodexClient("/unused-test-only", Path("/unused-test-only"))
        client.thread_id = "offline-thread"
        client.request = AsyncMock(return_value={"offline": True})
        for target in (OLD, NEW):
            try:
                await client.call_tool("bundle_list", arguments=arguments(target))
            except evidence.ConsoleEvidenceError as exc:
                self.fail(f"approved pinned query rejected: {exc}")
            self.assertEqual(client.request.call_args.args, ("mcpServer/tool/call", {
                "threadId": "offline-thread", "server": "ait-console", "tool": "bundle_list",
                "arguments": arguments(target)}))
        for query in ({**arguments(), "search": "other"}, {**arguments(), "workspaceId": "93125"},
                      {**arguments(), "page": False}, {**arguments(), "size": 100}, {**arguments(), "extra": 1}):
            with self.subTest(query=query), self.assertRaises(evidence.ConsoleEvidenceError):
                await client.call_tool("bundle_list", arguments=query)
        with self.assertRaises(evidence.ConsoleEvidenceError):
            await client.call_tool("bundle_upload", arguments=arguments())
        self.assertEqual(client.request.await_count, 2)

    async def test_public_legacy_argument_map_mutation_cannot_change_pins(self):
        before = dict(bridge.REVIEWED_CONSOLE_ARGUMENTS)
        client = managed._CodexClient("/unused-test-only", Path("/unused-test-only"))
        client.request = AsyncMock()
        try:
            bridge.REVIEWED_CONSOLE_ARGUMENTS["miniAppId"] = 123
            with self.assertRaises(evidence.ConsoleEvidenceError):
                await client.call_tool("bundle_list", arguments=dict(bridge.REVIEWED_CONSOLE_ARGUMENTS))
        finally:
            bridge.REVIEWED_CONSOLE_ARGUMENTS.clear()
            bridge.REVIEWED_CONSOLE_ARGUMENTS.update(before)
        client.request.assert_not_awaited()


class OwnedClientFixtureTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = client_fixture.CodexConsoleTest.asyncSetUp
    asyncTearDown = client_fixture.CodexConsoleTest.asyncTearDown
    write_config = client_fixture.CodexConsoleTest.write_config
    write_scenario = client_fixture.CodexConsoleTest.write_scenario
    calls = client_fixture.CodexConsoleTest.calls

    async def provider(self, deployment):
        try:
            return await self.connection.get_provider(deployment_id=deployment)
        except TypeError as exc:
            self.fail(f"owned connection target selection missing: {exc}")

    async def test_legacy_and_ux019_share_connection_not_query_identity(self):
        source = self.executable.read_text()
        self.assertIn(repr(OLD), source)
        self.executable.write_text(source.replace(repr(OLD), "req['params']['arguments']['search']"))
        self.write_config()
        async def old(): return await self.provider(OLD)
        async def new(): return await self.provider(NEW)
        try:
            facade = tools.TossConsoleReadOnlyTools(provider_factory=old, ux019_provider_factory=new)
        except TypeError as exc:
            self.fail(f"UX019 facade route missing: {exc}")
        results = await asyncio.gather(facade.call(OLD_TOOL, dict(SCOPE)), facade.call(NEW_TOOL, dict(SCOPE)))
        self.assertEqual([r["deployment_id"] for r in results], [OLD, NEW])
        self.assertTrue(all(r["test_bundle_ready"] for r in results))
        messages = self.calls()
        calls = [r["params"] for r in messages if r["method"] == "mcpServer/tool/call"]
        self.assertEqual({r["arguments"]["search"] for r in calls}, {OLD, NEW})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r["tool"] == "bundle_list" for r in calls))
        methods = [r["method"] for r in messages]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertNotIn("turn/start", methods)
        self.assertNotIn("thread/resume", methods)
        self.assertNotIn("mcpServer/oauth/login", methods)

    async def test_arbitrary_target_is_rejected_before_config_or_process(self):
        for target in ("other", "", True, None, [], NEW.upper()):
            try:
                with self.assertRaises(evidence.ConsoleEvidenceError):
                    await self.connection.get_provider(deployment_id=target)
            except TypeError as exc:
                self.fail(f"deployment guard missing: {exc}")
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()
