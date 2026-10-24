"""Owned Codex protocol fixture: no real OAuth, model, network or existing daemon."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from toss_console_codex import CodexConsoleConnection
from toss_console_evidence import ConsoleEvidenceError, DEPLOYMENT_ID
from toss_console_tools import TossConsoleReadOnlyTools


class CodexConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="console-client-test-")
        self.root = Path(self.folder.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config = self.root / "console.json"
        self.executable = self.root / "codex-fixture"
        self.scenario = self.root / "scenario.json"
        self.record = self.root / "calls.jsonl"
        self.record.touch(mode=0o600)
        self.descriptor = {"name": "bundle_list", "inputSchema": json.loads(
            (Path(__file__).parent / "fixtures/toss_bundle_list_input_schema.json").read_text())}
        self.write_scenario()
        # Only this test-owned process handles the fixture. All messages are
        # recorded so a model turn, wrong scope or write call cannot go unnoticed.
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import json,sys,time\nfrom pathlib import Path\n"
            f"root=Path({str(self.root)!r})\n"
            "def record(value):\n"
            " with (root/'calls.jsonl').open('a') as out: out.write(json.dumps(value)+'\\n')\n"
            "scenario=json.loads((root/'scenario.json').read_text())\n"
            "if sys.argv[1:5]==['--no-daemon','mcp','get','ait-console']:\n"
            " record({'method':'cli/get'})\n"
            " print(json.dumps({'name':'ait-console','enabled':True,'transport':{"
            "'type':'streamable_http','url':scenario['url'], 'http_headers':None,"
            "'env_http_headers':None,'bearer_token_env_var':None,'http_headers_helper':None}}))\n"
            " sys.exit(0)\n"
            "assert sys.argv[1:]==['--no-daemon','app-server','--listen','stdio://']\n"
            "for line in sys.stdin:\n"
            " req=json.loads(line);record(req)\n"
            " if 'id' not in req: continue\n"
            " method=req['method']\n"
            " if scenario.get('hang')==method: time.sleep(30)\n"
            " if method=='initialize': result={'serverInfo':{'name':'codex','version':'fixture'}}\n"
            " elif method=='thread/start': result={'thread':{'id':'owned-console-thread'}}\n"
            " elif method=='mcpServerStatus/list': result={'data':[{'name':'ait-console',"
            "'pluginId':'ait@apps-in-toss','httpOrigin':'https://mcp.toss.im',"
            "'authStatus':scenario['auth'], 'tools':{'bundle_list':scenario['descriptor']},"
            "'toolsError':None}], 'nextCursor':None}\n"
            " elif method=='mcpServer/tool/call': result={'isError':False,'content':[{'type':'text',"
            "'text':json.dumps({'contents':[{'miniAppId':75257,'deploymentId':"
            f"{DEPLOYMENT_ID!r},'appName':'tarororo','reviewStatus':'CREATED',"
            "'versionName':'fixture-version','isTested':True,'deployed':False}],"
            "'currentPage':0,'totalPage':0})}]}\n"
            " else: raise AssertionError('unexpected method')\n"
            " print(json.dumps({'id':req['id'],'result':result}),flush=True)\n"
        )
        self.executable.chmod(0o700)
        self.write_config()
        self.connection = CodexConsoleConnection(
            {"tossApp": self.repo}, self.root / "state", config_path=self.config)

    def write_config(self, **changes):
        cfg = {"version": 1, "enabled": True,
               "codex_executable": str(self.executable),
               "codex_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest()}
        cfg.update(changes)
        self.config.write_text(json.dumps(cfg))
        self.config.chmod(0o600)

    def write_scenario(self, **changes):
        data = {"descriptor": self.descriptor, "auth": "oAuth",
                "url": "https://mcp.toss.im/adapters/apps-in-toss-console/mcp", **changes}
        self.scenario.write_text(json.dumps(data))

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    async def asyncTearDown(self):
        await self.connection.close()
        self.folder.cleanup()

    async def status(self):
        return await TossConsoleReadOnlyTools(provider_factory=self.connection.get_provider).call(
            "get_toss_test_console_status", {"project": "tossApp", "target": "tarororo-test"})

    async def test_missing_or_disabled_config_does_not_launch_a_client(self):
        self.config.unlink()
        self.assertIsNone(await self.connection.get_provider())
        self.write_config(enabled=False)
        self.assertIsNone(await self.connection.get_provider())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "state").exists())

    async def test_fixed_console_read_uses_owned_ephemeral_thread_without_model(self):
        result = await self.status()
        self.assertTrue(result["provider_verified"], result)
        self.assertTrue(result["test_bundle_ready"], result)
        self.assertFalse(result["remote_digest_verified"])
        calls = self.calls()
        self.assertEqual([x["method"] for x in calls], [
            "cli/get", "initialize", "initialized", "thread/start",
            "mcpServerStatus/list", "mcpServer/tool/call"])
        thread = next(x["params"] for x in calls if x["method"] == "thread/start")
        self.assertIs(thread["ephemeral"], True)
        self.assertEqual(thread["sandbox"], "read-only")
        self.assertEqual(thread["approvalPolicy"], "never")
        self.assertFalse(Path(thread["cwd"]).is_relative_to(self.repo))
        invoked = calls[-1]["params"]
        self.assertEqual(invoked, {"threadId": "owned-console-thread", "server": "ait-console",
                                  "tool": "bundle_list", "arguments": {
                                      "workspaceId": 93125, "miniAppId": 75257, "page": 0, "size": 20,
                                      "search": DEPLOYMENT_ID}})
        context = (await self.connection.get_provider()).context
        self.assertEqual(context.authentication_source, "codex_app_server")
        self.assertIsNone(context.client_id)  # Native client owns this opaque OAuth detail.

    async def test_each_query_refreshes_catalog_but_reuses_owned_client(self):
        self.assertTrue((await self.status())["test_bundle_ready"])
        self.assertTrue((await self.status())["test_bundle_ready"])
        methods = [x["method"] for x in self.calls()]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("mcpServerStatus/list"), 2)
        self.assertEqual(methods.count("mcpServer/tool/call"), 2)

    async def test_missing_auth_and_schema_drift_prevent_tool_calls(self):
        for scenario in ({"auth": "notLoggedIn"}, {"auth": "bearerToken"},
                         {"descriptor": {**self.descriptor, "inputSchema": {"type": "object"}}}):
            with self.subTest(scenario=scenario):
                self.write_scenario(**scenario)
                result = await self.status()
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(result["provider_verified"])
                self.assertNotIn("mcpServer/tool/call", [x["method"] for x in self.calls()])
                await self.connection.close()
                self.record.write_text("")

    async def test_different_endpoint_is_rejected_before_thread_creation(self):
        self.write_scenario(url="https://mcp.toss.im/another/mcp")
        result = await self.status()
        self.assertFalse(result["provider_verified"])
        self.assertEqual([x["method"] for x in self.calls()], ["cli/get"])

    async def test_owner_config_mode_symlink_and_duplicate_keys_fail_closed(self):
        self.config.chmod(0o644)
        with self.assertRaises(ConsoleEvidenceError):
            await self.connection.get_provider()
        self.config.chmod(0o600)
        self.config.write_text('{"version":1,"version":1}')
        with self.assertRaises(ConsoleEvidenceError):
            await self.connection.get_provider()
        self.config.unlink()
        self.config.symlink_to(self.scenario)
        with self.assertRaises(ConsoleEvidenceError):
            await self.connection.get_provider()
        self.assertEqual(self.calls(), [])

    async def test_client_drift_or_project_executable_cannot_launch(self):
        self.write_config(codex_sha256="0" * 64)
        with self.assertRaises(ConsoleEvidenceError):
            await self.connection.get_provider()
        unsafe = self.repo / "codex"
        unsafe.write_bytes(self.executable.read_bytes())
        unsafe.chmod(0o700)
        self.write_config(codex_executable=str(unsafe))
        with self.assertRaises(ConsoleEvidenceError):
            await self.connection.get_provider()
        self.assertEqual(self.calls(), [])

    async def test_cancellation_reaps_the_owned_client_and_allows_retry(self):
        self.write_scenario(hang="mcpServerStatus/list")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.connection.get_provider(), timeout=1)
        self.write_scenario()
        self.assertTrue((await self.status())["test_bundle_ready"])
        await self.connection.close()
        self.assertIsNone(self.connection._client)


if __name__ == "__main__":
    unittest.main()
