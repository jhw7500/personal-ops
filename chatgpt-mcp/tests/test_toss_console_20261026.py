"""Offline third exact TEST console route without any provider write or new OAuth."""
from __future__ import annotations
import unittest
from unittest.mock import AsyncMock

from toss_console_evidence import (
    DEPLOYMENT_ID, UX019_DEPLOYMENT_ID, TEST_20261026_DEPLOYMENT_ID as CURRENT,
)
from toss_console_tools import (
    TossConsoleReadOnlyTools, TOSS_CONSOLE_TOOL_SPECS,
)
from toss_console_sdk_bridge import (
    reviewed_console_arguments, ReviewedBundleListContract, REVIEWED_CONSOLE_SCHEMA_SHA256
)
from tests.test_toss_console_tools import FakeProvider

SCOPE={"project":"tossApp","target":"tarororo-test"}
OLD_TOOL="get_toss_test_console_status"
UX019_TOOL="get_toss_ux019_test_console_status"
CURRENT_TOOL="get_toss_20261026_test_console_status"

def page(deployment):
    return {"contents":[{
        "miniAppId":75257,"workspaceId":93125,"appName":"tarororo",
        "deploymentId":deployment, "versionName":"20261026-28",
        "reviewStatus":"CREATED","isTested":True,"deployed":False,
        "providerPrivateToken":"DO_NOT_ECHO"}],
        "currentPage":0,"totalPage":0}

class NewTestRoute(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_fixed_three_tools_and_no_arbitrary_id(self):
        specs={s["name"]:s for s in TOSS_CONSOLE_TOOL_SPECS}
        self.assertEqual(set(specs),{OLD_TOOL,UX019_TOOL,CURRENT_TOOL})
        for spec in specs.values():
            self.assertEqual(set(spec["inputSchema"]["properties"]),{"project","target"})
            self.assertFalse(spec["inputSchema"]["additionalProperties"])
            self.assertTrue(spec["annotations"]["readOnlyHint"])
            self.assertFalse(spec["annotations"]["destructiveHint"])
        f=AsyncMock(return_value=FakeProvider(response=page(CURRENT)))
        facade=TossConsoleReadOnlyTools(test_20261026_provider_factory=f)
        for added in ({"deployment_id":DEPLOYMENT_ID},{"raw_bundle":page(CURRENT)},
                      {"target":"production"}):
            with self.assertRaises(ValueError):
                await facade.call(CURRENT_TOOL,{**SCOPE,**added})
        f.assert_not_awaited()

    async def test_pinned_current_provider_independent_of_old(self):
        old=AsyncMock(return_value=FakeProvider(response=page(DEPLOYMENT_ID)))
        ux019=AsyncMock(return_value=FakeProvider(response=page(UX019_DEPLOYMENT_ID)))
        current=AsyncMock(return_value=FakeProvider(response=page(CURRENT)))
        facade=TossConsoleReadOnlyTools(
            provider_factory=old,ux019_provider_factory=ux019,
            test_20261026_provider_factory=current)
        result=await facade.call(CURRENT_TOOL,dict(SCOPE))
        self.assertEqual(result["deployment_id"],CURRENT)
        self.assertTrue(result["found"])
        self.assertTrue(result["test_bundle_ready"])
        self.assertTrue(result["provider_verified"])
        self.assertFalse(result["remote_digest_verified"])
        self.assertNotIn("DO_NOT_ECHO",repr(result))
        old.assert_not_awaited()
        ux019.assert_not_awaited()
        current.assert_awaited_once_with()
        self.assertEqual((await facade.call(UX019_TOOL,dict(SCOPE)))["deployment_id"],UX019_DEPLOYMENT_ID)
        self.assertEqual((await facade.call(OLD_TOOL,dict(SCOPE)))["deployment_id"],DEPLOYMENT_ID)

    async def test_missing_oauth_fails_without_falling_back(self):
        old=AsyncMock(return_value=FakeProvider(response=page(DEPLOYMENT_ID)))
        facade=TossConsoleReadOnlyTools(provider_factory=old)
        result=await facade.call(CURRENT_TOOL,dict(SCOPE))
        self.assertEqual(result["code"],"CONSOLE_OAUTH_NOT_CONFIGURED")
        self.assertFalse(result["provider_verified"])
        self.assertEqual(result["deployment_id"],CURRENT)
        old.assert_not_awaited()

    async def test_old_provider_response_cannot_verify_current_version(self):
        factory=AsyncMock(return_value=FakeProvider(response=page(UX019_DEPLOYMENT_ID)))
        facade=TossConsoleReadOnlyTools(test_20261026_provider_factory=factory)
        result=await facade.call(CURRENT_TOOL,dict(SCOPE))
        self.assertFalse(result["found"])
        self.assertFalse(result["test_bundle_ready"])

    async def test_fixed_actual_mcp_schema_arguments(self):
        args=reviewed_console_arguments(CURRENT)
        self.assertEqual(args,{"workspaceId":93125,"miniAppId":75257,
                              "page":0,"size":20,"search":CURRENT})
        contract=ReviewedBundleListContract(
            schema_sha256=REVIEWED_CONSOLE_SCHEMA_SHA256,
            arguments=args,readonly_policy="pinned_bundle_list",
            deployment_id=CURRENT)
        self.assertEqual(contract.deployment_id,CURRENT)
        for bad in (CURRENT.upper(),"00000000-0000-4000-8000-000000000000",None):
            with self.subTest(bad=bad),self.assertRaises(Exception):
                reviewed_console_arguments(bad)

if __name__=="__main__":
    unittest.main()
