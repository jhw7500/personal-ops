"""Pure offline contract tests; no OAuth browser, token, HTTP or host access."""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
import unittest

import toss_console_sdk_bridge as bridge
from toss_console_sdk_bridge import (
    CONSOLE_MCP_ORIGIN, MAX_RESPONSE_BYTES, OAUTH_RESOURCE,
    ConsoleSessionContractError, ReviewedBundleListContract,
    ReviewedMcpBundleListProvider, schema_sha256,
)
from toss_console_tools import TossConsoleReadOnlyTools
from toss_console_evidence import WORKSPACE_ID, MINIAPP_ID, DEPLOYMENT_ID


# Synthetic offline input catalog; this is NOT a pin of Toss's live inputSchema.
SCHEMA = {
    "type": "object",
    "properties": {"workspaceId": {"type": "string"}, "miniappId": {"type": "string"}},
    "required": ["workspaceId", "miniappId"],
    "additionalProperties": False,
}
ARGS = {"workspaceId": WORKSPACE_ID, "miniappId": MINIAPP_ID}
OBSERVED_SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "toss_bundle_list_input_schema.json")
    .read_text(encoding="utf-8")
)
OBSERVED_SCHEMA_SHA256 = "bd7021bac9ba68f043abdccedd16494539f2bfab9b895161512cebc6b3d5e614"
OBSERVED_ARGUMENTS = {
    "workspaceId": 93125,
    "miniAppId": 75257,
    "page": 0,
    "size": 20,
    "search": DEPLOYMENT_ID,
}


def contract(**kwargs):
    base = {"schema_sha256": schema_sha256(SCHEMA), "arguments": dict(ARGS)}
    base.update(kwargs)
    return ReviewedBundleListContract(**base)


def pinned_contract(**kwargs):
    base = {
        "schema_sha256": OBSERVED_SCHEMA_SHA256,
        "arguments": dict(OBSERVED_ARGUMENTS),
        "readonly_policy": "pinned_bundle_list",
    }
    base.update(kwargs)
    return ReviewedBundleListContract(**base)


def provider_response(**kwargs):
    item = {
        "deploymentId": DEPLOYMENT_ID,
        "reviewStatus": "CREATED",
        "versionName": "sample-version",
        "isTested": True,
        "deployed": False,
        "appName": "tarororo",
        "apiToken": "DO_NOT_ECHO",
        **kwargs,
    }
    return {"bundles": [item]}


class Session:
    def __init__(self, *, schema=SCHEMA, annotations=None, tools=None,
                 next_cursor=None, result=None, error=None):
        self.methods = []
        self.error = error
        self.result = result if result is not None else SimpleNamespace(
            isError=False, structuredContent=provider_response())
        descriptor = SimpleNamespace(
            name="bundle_list", inputSchema=schema,
            annotations=annotations if annotations is not None
                else SimpleNamespace(readOnlyHint=True))
        self.tools = tools if tools is not None else [descriptor]
        self.next_cursor = next_cursor

    async def list_tools(self):
        self.methods.append(("list_tools",))
        if self.error == "catalog":
            raise RuntimeError("secret catalog exception")
        return SimpleNamespace(tools=self.tools, next_cursor=self.next_cursor)

    async def call_tool(self, name, *, arguments):
        self.methods.append(("call_tool", name, dict(arguments)))
        if self.error == "call":
            raise RuntimeError("secret bearer exception")
        return self.result


class ReviewPolicyTest(unittest.TestCase):
    def test_contract_exposes_explicit_readonly_policy(self):
        self.assertIn(
            "readonly_policy",
            {field.name for field in fields(ReviewedBundleListContract)},
        )

    def test_observed_schema_fixture_matches_owner_reviewed_pin(self):
        self.assertEqual(
            schema_sha256(OBSERVED_SCHEMA),
            OBSERVED_SCHEMA_SHA256,
        )
        self.assertEqual(
            getattr(bridge, "REVIEWED_CONSOLE_SCHEMA_SHA256", None),
            OBSERVED_SCHEMA_SHA256,
        )
        self.assertEqual(
            getattr(bridge, "REVIEWED_CONSOLE_ARGUMENTS", None),
            OBSERVED_ARGUMENTS,
        )

    def test_exact_owner_reviewed_contract_is_constructible(self):
        try:
            reviewed = pinned_contract()
        except ConsoleSessionContractError as exc:
            self.fail(f"exact reviewed contract was rejected: {exc}")
        self.assertEqual(reviewed.readonly_policy, "pinned_bundle_list")
        self.assertEqual(reviewed.arguments, OBSERVED_ARGUMENTS)

    def test_pinned_policy_requires_exact_schema_and_canonical_arguments(self):
        bad_arguments = [
            {**OBSERVED_ARGUMENTS, "workspaceId": "93125"},
            {**OBSERVED_ARGUMENTS, "miniAppId": True},
            {**OBSERVED_ARGUMENTS, "page": 1},
            {**OBSERVED_ARGUMENTS, "size": 100},
            {**OBSERVED_ARGUMENTS, "search": "other"},
            {key: value for key, value in OBSERVED_ARGUMENTS.items() if key != "search"},
            {**OBSERVED_ARGUMENTS, "extra": DEPLOYMENT_ID},
        ]
        variants = [
            {"readonly_policy": "unknown"},
            {"schema_sha256": "0" * 64},
            *({"arguments": arguments} for arguments in bad_arguments),
        ]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ConsoleSessionContractError):
                pinned_contract(**variant)

    def test_provider_hint_policy_does_not_accept_pagination_arguments(self):
        with self.assertRaisesRegex(
            ConsoleSessionContractError, "CONSOLE_ARGUMENTS_NOT_REVIEWED"
        ):
            contract(arguments=dict(OBSERVED_ARGUMENTS))

    def test_approved_hash_deterministic_independent_of_key_order(self):
        self.assertEqual(schema_sha256(SCHEMA), schema_sha256({
            "required": ["workspaceId", "miniappId"],
            "additionalProperties": False,
            "properties": dict(reversed(list(SCHEMA["properties"].items()))),
            "type": "object",
        }))

    def test_bogus_sha_and_unreviewed_endpoint_refused(self):
        variants = [
            {"schema_sha256": "0" * 10}, {"endpoint": "http://mcp.toss.im"},
            {"oauth_resource": CONSOLE_MCP_ORIGIN},
            {"client_id": "other-client"},
            {"arguments": {}},
            {"arguments": {"tenantId": "another-account"}},
            {"arguments": {"miniappId": "75258"}},
            {"arguments": {"miniappId": MINIAPP_ID}},
            {"arguments": {"workspaceId": WORKSPACE_ID}},
            {"arguments": {"miniappId": True}},
            {"arguments": {"__proto__": MINIAPP_ID}},
        ]
        for kw in variants:
            with self.subTest(kw=kw), self.assertRaises(ConsoleSessionContractError):
                contract(**kw)

    def test_frozen_contract_endpoint_and_resource(self):
        c=contract()
        self.assertEqual(c.endpoint, CONSOLE_MCP_ORIGIN)
        self.assertEqual(c.oauth_resource, OAUTH_RESOURCE)


class SdkReadOnlySessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_policy_accepts_absent_readonly_hint_and_calls_exact_scope(self):
        descriptor = {"name": "bundle_list", "inputSchema": OBSERVED_SCHEMA}
        session = Session(tools=[descriptor])
        provider = ReviewedMcpBundleListProvider(
            session, pinned_contract(), trusted_oauth_session=True
        )

        try:
            await provider.verify_catalog()
        except ConsoleSessionContractError as exc:
            self.fail(f"owner-reviewed descriptor was rejected: {exc}")
        await provider.bundle_list(workspace_id=WORKSPACE_ID, miniapp_id=MINIAPP_ID)

        self.assertTrue(provider.context.reviewed_tool_schema)
        self.assertEqual(session.methods, [
            ("list_tools",),
            ("call_tool", "bundle_list", OBSERVED_ARGUMENTS),
        ])

    async def test_pinned_policy_rejects_false_or_nonboolean_readonly_hint(self):
        for annotation in (False, "true", 1):
            with self.subTest(annotation=annotation):
                descriptor = SimpleNamespace(
                    name="bundle_list",
                    inputSchema=OBSERVED_SCHEMA,
                    annotations=SimpleNamespace(readOnlyHint=annotation),
                )
                session = Session(tools=[descriptor])
                provider = ReviewedMcpBundleListProvider(
                    session, pinned_contract(), trusted_oauth_session=True
                )
                with self.assertRaisesRegex(
                    ConsoleSessionContractError, "CONSOLE_READONLY_NOT_VERIFIED"
                ):
                    await provider.verify_catalog()
                self.assertEqual(session.methods, [("list_tools",)])

    async def test_pinned_policy_rejects_null_malformed_or_ambiguous_annotations(self):
        invalid_annotations = [
            None,
            "readonly",
            [],
            {"readOnlyHint": None},
            {"read_only_hint": "true"},
            {"readOnlyHint": True, "read_only_hint": False},
            {"readOnlyHint": True, "read_only_hint": True},
        ]
        for annotation_payload in invalid_annotations:
            with self.subTest(annotations=annotation_payload):
                descriptor = {
                    "name": "bundle_list",
                    "inputSchema": OBSERVED_SCHEMA,
                    "annotations": annotation_payload,
                }
                session = Session(tools=[descriptor])
                provider = ReviewedMcpBundleListProvider(
                    session, pinned_contract(), trusted_oauth_session=True
                )
                with self.assertRaisesRegex(
                    ConsoleSessionContractError, "CONSOLE_READONLY_NOT_VERIFIED"
                ):
                    await provider.verify_catalog()
                self.assertEqual(session.methods, [("list_tools",)])

    async def test_provider_hint_policy_still_rejects_absent_readonly_hint(self):
        descriptor = SimpleNamespace(name="bundle_list", inputSchema=SCHEMA)
        session = Session(tools=[descriptor])
        provider = ReviewedMcpBundleListProvider(
            session, contract(), trusted_oauth_session=True
        )
        with self.assertRaisesRegex(
            ConsoleSessionContractError, "CONSOLE_READONLY_NOT_VERIFIED"
        ):
            await provider.verify_catalog()
        self.assertEqual(session.methods, [("list_tools",)])

    async def test_observed_console_response_through_sdk_and_facade(self):
        page = {
            "contents": provider_response(miniAppId=75257, versionName="20261016-26")["bundles"],
            "currentPage": 0,
            "totalPage": 0,
        }
        for text_response in (False, True):
            with self.subTest(text_response=text_response):
                result = (SimpleNamespace(isError=False, structuredContent=None, content=[
                    SimpleNamespace(type="text", text=json.dumps(page))]) if text_response
                    else SimpleNamespace(isError=False, structuredContent=page))
                s = Session(result=result)
                p = ReviewedMcpBundleListProvider(s, contract(), trusted_oauth_session=True)
                await p.verify_catalog()
                output = await TossConsoleReadOnlyTools(p).call(
                    "get_toss_test_console_status", {"project": "tossApp", "target": "tarororo-test"})
                self.assertTrue(output["provider_verified"])
                self.assertTrue(output["test_bundle_ready"])
                self.assertEqual(output["version_name"], "20261016-26")
                self.assertFalse(output["remote_digest_verified"])
                self.assertNotIn("DO_NOT_ECHO", repr(output))
                self.assertEqual(s.methods, [
                    ("list_tools",), ("call_tool", "bundle_list", ARGS)])

    async def test_numbered_partial_page_fails_closed_through_facade(self):
        page = {"contents": provider_response(miniAppId=75257)["bundles"],
                "currentPage": 0, "totalPage": 1}
        s = Session(result=SimpleNamespace(isError=False, structuredContent=page))
        p = ReviewedMcpBundleListProvider(s, contract(), trusted_oauth_session=True)
        await p.verify_catalog()
        output = await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project": "tossApp", "target": "tarororo-test"})
        self.assertFalse(output["provider_verified"])
        self.assertFalse(output["test_bundle_ready"])
        self.assertEqual(output["code"], "INCOMPLETE_CONSOLE_PAGE")

    async def test_no_authorization_cannot_even_list_tools(self):
        s=Session()
        p=ReviewedMcpBundleListProvider(s, contract())
        with self.assertRaisesRegex(ConsoleSessionContractError, "CONSOLE_OAUTH_NOT_CONFIGURED"):
            await p.verify_catalog()
        self.assertEqual(s.methods, [])
        self.assertFalse(p.context.oauth_verified)
        self.assertFalse(p.context.reviewed_tool_schema)

    async def test_preverified_catalog_calls_only_fixed_bundle_list(self):
        s=Session()
        p=ReviewedMcpBundleListProvider(s, contract(), trusted_oauth_session=True)
        self.assertFalse(p.context.reviewed_tool_schema)
        await p.verify_catalog()
        self.assertTrue(p.context.reviewed_tool_schema)
        output=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status",
            {"project":"tossApp","target":"tarororo-test"})
        self.assertTrue(output["provider_verified"])
        self.assertTrue(output["test_bundle_ready"])
        self.assertFalse(output["remote_digest_verified"])
        self.assertEqual(s.methods, [
            ("list_tools",), ("call_tool","bundle_list", ARGS)])
        self.assertNotIn("DO_NOT_ECHO",repr(output))

    async def test_cannot_query_without_cleared_catalog(self):
        s=Session()
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        with self.assertRaisesRegex(ConsoleSessionContractError,"CONSOLE_TOOL_SCHEMA_NOT_REVIEWED"):
            await p.bundle_list(workspace_id=WORKSPACE_ID,miniapp_id=MINIAPP_ID)
        self.assertEqual(s.methods, [])

    async def test_schema_mismatch_or_unauthorized_annotation_stops_calls(self):
        variants=[
            {"schema":{"type":"object","properties":{"wrong":{"type":"string"}}}},
            {"annotations":SimpleNamespace(readOnlyHint=False)},
            {"annotations":SimpleNamespace(readOnlyHint=None)},
            {"tools":[]},
            {"next_cursor":"secret-cursor"},
        ]
        for kw in variants:
            with self.subTest(kw=str(kw)[:90]):
                s=Session(**kw)
                p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
                with self.assertRaises(ConsoleSessionContractError):
                    await p.verify_catalog()
                self.assertEqual(s.methods,[("list_tools",)])
                self.assertFalse(p.context.reviewed_tool_schema)

    async def test_wrong_account_or_app_scope_never_reaches_upstream(self):
        s=Session()
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        for ws,app in (("00000",MINIAPP_ID),(WORKSPACE_ID,"0")):
            with self.assertRaisesRegex(ConsoleSessionContractError,"CONSOLE_SCOPE_NOT_REVIEWED"):
                await p.bundle_list(workspace_id=ws,miniapp_id=app)
        self.assertEqual(s.methods,[("list_tools",)])

    async def test_provider_exception_never_appears_in_facade(self):
        s=Session(error="call")
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        out=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertEqual(out["code"],"CONSOLE_READ_FAILED")
        self.assertFalse(out["provider_verified"])
        self.assertNotIn("secret",repr(out))

    async def test_structured_provider_error_is_sanitized(self):
        s=Session(result=SimpleNamespace(
            isError=True, structuredContent={"error":"private bearer"}))
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        out=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertEqual(out["code"],"CONSOLE_READ_FAILED")
        self.assertNotIn("private bearer",repr(out))

    async def test_plain_text_json_mcp_response_accepts_one_text_block(self):
        s=Session(result=SimpleNamespace(isError=False,structuredContent=None,content=[
            SimpleNamespace(type="text",text=json.dumps(provider_response()))]))
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        out=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertTrue(out["test_bundle_ready"])
        self.assertNotIn("DO_NOT_ECHO",repr(out))

    async def test_partial_bundle_page_is_not_final_not_found(self):
        s=Session(result=SimpleNamespace(isError=False,structuredContent={
            **provider_response(), "nextCursor":"more-results"}))
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        result=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertFalse(result["test_bundle_ready"])
        self.assertEqual(result["code"],"CONSOLE_READ_FAILED")

    async def test_response_size_limited(self):
        s=Session(result=SimpleNamespace(isError=False,structuredContent={
            "bundles":[], "secret":"s" * MAX_RESPONSE_BYTES}))
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        await p.verify_catalog()
        result=await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertEqual(result["code"],"CONSOLE_READ_FAILED")

    async def test_current_python_sdk_snake_case_fields(self):
        """Handle the SDK's attribute naming, not just wire camelCase aliases."""
        s = Session(
            tools=[SimpleNamespace(
                name="bundle_list", input_schema=SCHEMA,
                annotations=SimpleNamespace(read_only_hint=True))],
            result=SimpleNamespace(is_error=False,
                structured_content=provider_response()))
        p = ReviewedMcpBundleListProvider(s, contract(), trusted_oauth_session=True)
        await p.verify_catalog()
        output = await TossConsoleReadOnlyTools(p).call(
            "get_toss_test_console_status", {"project":"tossApp","target":"tarororo-test"})
        self.assertTrue(output["test_bundle_ready"])
        self.assertEqual(s.methods[-1], ("call_tool","bundle_list",ARGS))

    async def test_reviewed_arguments_frozen_against_late_mutation(self):
        c=contract()
        s=Session()
        p=ReviewedMcpBundleListProvider(s,c,trusted_oauth_session=True)
        c.arguments["miniappId"]="invalid"
        await p.verify_catalog()
        await p.bundle_list(workspace_id=WORKSPACE_ID,miniapp_id=MINIAPP_ID)
        self.assertEqual(s.methods[-1],("call_tool","bundle_list",ARGS))

    async def test_schema_pinned_but_wrong_field_name_fails_before_call(self):
        s=Session()
        p=ReviewedMcpBundleListProvider(
            s,contract(arguments={**ARGS, "madeUpField":MINIAPP_ID}),trusted_oauth_session=True)
        with self.assertRaisesRegex(ConsoleSessionContractError,
                                    "CONSOLE_ARGUMENTS_SCHEMA_MISMATCH"):
            await p.verify_catalog()
        self.assertEqual(s.methods,[("list_tools",)])

    async def test_catalog_timeout_is_bounded_and_never_calls_tool(self):
        import toss_console_sdk_bridge as bridge
        from unittest.mock import patch
        class SlowSession(Session):
            async def list_tools(self):
                self.methods.append(("list_tools",))
                await asyncio.sleep(0.3)
                return SimpleNamespace(tools=self.tools, next_cursor=None)
        import asyncio
        s=SlowSession()
        p=ReviewedMcpBundleListProvider(s,contract(),trusted_oauth_session=True)
        with patch.object(bridge,"CALL_TIMEOUT_SECONDS",0.01):
            with self.assertRaisesRegex(ConsoleSessionContractError,
                                        "CONSOLE_CATALOG_READ_FAILED"):
                await p.verify_catalog()
        self.assertEqual(s.methods,[("list_tools",)])
        self.assertFalse(p.context.reviewed_tool_schema)


if __name__=="__main__":
    unittest.main()
