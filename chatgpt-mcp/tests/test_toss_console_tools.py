"""Unit-only contract for the read-only Toss TEST console bridge."""
from __future__ import annotations

import asyncio
import unittest

from toss_console_evidence import (
    DEPLOYMENT_ID, MINIAPP_ID, WORKSPACE_ID, ConsoleEvidenceError,
)
from toss_console_tools import (
    CONSOLE_MCP_ORIGIN, OAUTH_CLIENT_ID, TARGET, PROJECT,
    TOSS_CONSOLE_TOOL_SPECS, TossConsoleReadOnlyTools,
    VerifiedProviderContext,
)


def entry(**overrides):
    item = {
        "deploymentId": DEPLOYMENT_ID, "reviewStatus": "CREATED",
        "versionName": "test-20261016", "isTested": True,
        "deployed": False, "appName": "tarororo",
        "authorizationToken": "private-bearer-value",
        "email": "private@domain.example",
    }
    item.update(overrides)
    return {"bundles": [item]}


class FakeProvider:
    def __init__(self, response=None, *,
                 endpoint=CONSOLE_MCP_ORIGIN,
                 client_id=OAUTH_CLIENT_ID,
                 authorized=True,
                 schema=True,
                 authentication_source="direct_mcp",
                 error=None,
                 sleep=0):
        self.context = VerifiedProviderContext(
            endpoint=endpoint, client_id=client_id,
            oauth_verified=authorized, reviewed_tool_schema=schema,
            authentication_source=authentication_source,
        )
        self.response = response if response is not None else entry()
        self.error = error
        self.sleep = sleep
        self.calls = []

    async def bundle_list(self, *, workspace_id, miniapp_id):
        self.calls.append((workspace_id, miniapp_id))
        if self.error is not None:
            raise self.error
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return self.response


async def check(provider=None):
    return await TossConsoleReadOnlyTools(provider).call(
        "get_toss_test_console_status",
        {"project": PROJECT, "target": TARGET},
    )


class ToolSpecTest(unittest.TestCase):
    def test_exact_scope_readonly_annotations_and_no_payload_input(self):
        self.assertEqual(len(TOSS_CONSOLE_TOOL_SPECS), 2)
        spec = TOSS_CONSOLE_TOOL_SPECS[0]
        self.assertEqual(spec["name"], "get_toss_test_console_status")
        self.assertEqual(spec["inputSchema"]["required"], ["project", "target"])
        self.assertFalse(spec["inputSchema"]["additionalProperties"])
        self.assertEqual(spec["inputSchema"]["properties"]["project"]["enum"], ["tossApp"])
        self.assertEqual(spec["inputSchema"]["properties"]["target"]["enum"], ["tarororo-test"])
        self.assertTrue(spec["annotations"]["readOnlyHint"])
        self.assertFalse(spec["annotations"]["destructiveHint"])
        self.assertFalse(spec["annotations"]["openWorldHint"])


class ReadOnlyToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_oauth_is_explicit_no_provider_call(self):
        result = await check()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["code"], "CONSOLE_OAUTH_NOT_CONFIGURED")
        self.assertFalse(result["provider_verified"])
        self.assertFalse(result["test_bundle_ready"])
        self.assertFalse(result["remote_digest_verified"])

    async def test_exact_bundle_is_reported_only_from_injected_trusted_port(self):
        provider = FakeProvider()
        result = await check(provider)
        self.assertEqual(provider.calls, [(WORKSPACE_ID, MINIAPP_ID)])
        self.assertTrue(result["provider_verified"])
        self.assertTrue(result["test_bundle_ready"])
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["review_status"], "CREATED")
        self.assertFalse(result["remote_digest_verified"])
        self.assertNotIn("private-bearer-value", repr(result))
        self.assertNotIn("private@domain.example", repr(result))

    async def test_unconfigured_or_misconfigured_connector_fails_before_network(self):
        variants = [
            {"endpoint": "http://mcp.toss.im/adapters/apps-in-toss-console/mcp"},
            {"endpoint": "https://mcp.toss.im/other"},
            {"client_id": "anything-else"},
            {"client_id": None},
            {"authentication_source": "codex_app_server"},
            {"authentication_source": "unknown"},
            {"authorized": False},
            {"schema": False},
        ]
        for kwargs in variants:
            with self.subTest(kwargs=kwargs):
                provider = FakeProvider(**kwargs)
                result = await check(provider)
                self.assertEqual(provider.calls, [])
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(result["provider_verified"])

    async def test_codex_managed_oauth_is_trusted_without_claiming_a_client_id(self):
        provider = FakeProvider(client_id=None, authentication_source="codex_app_server")
        result = await check(provider)
        self.assertEqual(provider.calls, [(WORKSPACE_ID, MINIAPP_ID)])
        self.assertTrue(result["provider_verified"])
        self.assertTrue(result["test_bundle_ready"])

    async def test_bad_context_and_unknown_provider_are_denied(self):
        class Invalid:
            context = {"oauth_verified": True}
            def bundle_list(self, **kwargs):
                raise AssertionError("must not be called")
        result = await check(Invalid())
        self.assertEqual(result["code"], "CONSOLE_TRUST_NOT_ESTABLISHED")

    async def test_extra_user_argument_and_cross_target_are_rejected(self):
        provider = FakeProvider()
        tool = TossConsoleReadOnlyTools(provider)
        for arg in (
            {"project": PROJECT, "target": TARGET, "raw_response": entry()},
            {"project": PROJECT, "target": "production"},
            {"project": "personal-ops", "target": TARGET},
            {},
        ):
            with self.subTest(arg=arg), self.assertRaisesRegex(ValueError, "UNSUPPORTED_SCOPE"):
                await tool.call("get_toss_test_console_status", arg)
        with self.assertRaisesRegex(ValueError, "INVALID_CONSOLE_ARGUMENTS"):
            await tool.call("bundle_upload", {"project": PROJECT, "target": TARGET})
        self.assertEqual(provider.calls, [])

    async def test_no_match_is_unconfirmed_not_ready(self):
        provider = FakeProvider({"bundles": [entry(deploymentId="other")["bundles"][0]]})
        result = await check(provider)
        self.assertTrue(result["provider_verified"])
        self.assertFalse(result["found"])
        self.assertEqual(result["state"], "not_ready")
        self.assertFalse(result["test_bundle_ready"])
        self.assertEqual(result["evidence"], "BUNDLE_NOT_CONFIRMED_IN_PAGE")

    async def test_unexpected_provider_payload_is_safe_failure(self):
        provider = FakeProvider({"error": "key=sensitive-value", "items": []})
        result = await check(provider)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["code"], "PROVIDER_RESPONSE_ERROR")
        self.assertNotIn("sensitive-value", repr(result))

    async def test_invalid_schema_fails_closed_not_success(self):
        result = await check(FakeProvider({"items": [entry(isTested="true")["bundles"][0]]}))
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["code"], "UNVERIFIED_CONSOLE_RESPONSE")

    async def test_provider_exception_is_sanitized(self):
        provider = FakeProvider(error=RuntimeError("Authorization Bearer secret"))
        result = await check(provider)
        self.assertEqual(result["code"], "CONSOLE_READ_FAILED")
        self.assertNotIn("Bearer", repr(result))

    async def test_timeout_is_bounded(self):
        # Do not wait 10 seconds: temporarily patch module timeout for unit fixture.
        import toss_console_tools as module
        original = module.CALL_TIMEOUT_SECONDS
        module.CALL_TIMEOUT_SECONDS = 0.01
        try:
            result = await check(FakeProvider(sleep=0.5))
        finally:
            module.CALL_TIMEOUT_SECONDS = original
        self.assertEqual(result["code"], "CONSOLE_READ_TIMEOUT")

    async def test_production_flag_blocks_test_ready_even_with_created_status(self):
        result = await check(FakeProvider(entry(deployed=True)))
        self.assertEqual(result["state"], "not_ready")
        self.assertFalse(result["test_bundle_ready"])
        self.assertTrue(result["provider_verified"])

    async def test_bundle_error_does_not_claim_authenticated_provider_verification(self):
        # An authenticated request may fail even when OAuth is configured.
        result = await check(FakeProvider({"items": [entry(isTested=None)["bundles"][0]]}))
        self.assertFalse(result["provider_verified"])
        self.assertFalse(result["test_bundle_ready"])

    async def test_factory_supplies_a_fresh_provider_for_each_valid_call(self):
        providers = []

        async def factory():
            provider = FakeProvider()
            providers.append(provider)
            return provider

        tool = TossConsoleReadOnlyTools(provider_factory=factory)
        arguments = {"project": PROJECT, "target": TARGET}
        first = await tool.call("get_toss_test_console_status", arguments)
        second = await tool.call("get_toss_test_console_status", arguments)

        self.assertTrue(first["test_bundle_ready"])
        self.assertTrue(second["test_bundle_ready"])
        self.assertEqual(len(providers), 2)
        self.assertEqual([provider.calls for provider in providers], [
            [(WORKSPACE_ID, MINIAPP_ID)], [(WORKSPACE_ID, MINIAPP_ID)],
        ])

    async def test_factory_none_preserves_unconfigured_result(self):
        async def factory():
            return None

        result = await TossConsoleReadOnlyTools(provider_factory=factory).call(
            "get_toss_test_console_status", {"project": PROJECT, "target": TARGET})
        self.assertEqual(result["code"], "CONSOLE_OAUTH_NOT_CONFIGURED")

    async def test_factory_failures_are_bounded_and_sanitized(self):
        import toss_console_tools as module

        async def timeout_factory():
            await asyncio.sleep(0.5)

        async def evidence_factory():
            raise ConsoleEvidenceError("CONSOLE_CATALOG_CHANGED")

        async def unexpected_factory():
            raise RuntimeError("Bearer private-token")

        original = module.CALL_TIMEOUT_SECONDS
        module.CALL_TIMEOUT_SECONDS = 0.01
        try:
            timeout = await TossConsoleReadOnlyTools(
                provider_factory=timeout_factory).call(
                    "get_toss_test_console_status", {"project": PROJECT, "target": TARGET})
        finally:
            module.CALL_TIMEOUT_SECONDS = original
        evidence = await TossConsoleReadOnlyTools(provider_factory=evidence_factory).call(
            "get_toss_test_console_status", {"project": PROJECT, "target": TARGET})
        unexpected = await TossConsoleReadOnlyTools(provider_factory=unexpected_factory).call(
            "get_toss_test_console_status", {"project": PROJECT, "target": TARGET})

        self.assertEqual(timeout["code"], "CONSOLE_READ_TIMEOUT")
        self.assertEqual(evidence["code"], "CONSOLE_CATALOG_CHANGED")
        self.assertEqual(unexpected["code"], "CONSOLE_READ_FAILED")
        self.assertNotIn("private-token", repr(unexpected))

    async def test_invalid_arguments_do_not_invoke_factory(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return FakeProvider()

        tool = TossConsoleReadOnlyTools(provider_factory=factory)
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SCOPE"):
            await tool.call("get_toss_test_console_status", {
                "project": PROJECT, "target": TARGET, "model": "ignored"})
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
