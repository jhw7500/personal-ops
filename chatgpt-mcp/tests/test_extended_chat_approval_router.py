"""MCP routing for newly added Git and registered host approval cards.

Uses ephemeral fixture repositories and fake registered Docker adapters.
No host operation, real credentials, Git push, or service restart is performed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE=Path(__file__).resolve().parents[1]
TESTS=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(TESTS)]
from host_operation_fixtures import write_registered_fixture  # noqa: E402
from test_git_workflows import run_git  # noqa: E402

LAUNCHER=r'''
import asyncio,json,sys
from pathlib import Path
config=json.loads(Path(sys.argv[1]).read_text())
sys.path[:0]=[config["module_dir"],config["tests_dir"]]
import host_access,server
from host_operation_fixtures import fake_registered_manager
host_access._installed_runtime_identity=lambda:"a"*64
origin_git=server.GitWorkflows
def git_factory(projects,state_dir,workspace=None,host_access=None):
    return origin_git(projects,state_dir,workspace=workspace,host_access=host_access,
        message_validator=lambda path:None, reference_checker=lambda uri:None)
server.GitWorkflows=git_factory
def registered_factory(projects,state_dir=None,workspace=None):
    return fake_registered_manager(projects,Path(config["host_config"]),state_dir)[0]
server.host_registered_tools.create_registered_manager=registered_factory
asyncio.run(server.serve_with_shutdown(server.load_projects(Path(config["projects"]))))
'''


def text_reply(r):
    return "\n".join(x.text for x in r.content if x.type=="text")


class ExtendedChatApprovalRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_cards_register_and_require_private_one_use_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="extended-chat-approval-router-") as folder:
            root=Path(folder)
            fx=write_registered_fixture(root)
            run_git(fx["root"], "config", "--local", "user.name", "Router Fixture")
            run_git(fx["root"], "config", "--local", "user.email", "fixture@example.invalid")
            # Change a previously reviewed file in the private fixture repo.
            (fx["root"]/"server/src/main.ts").write_text("export default 2;\n")
            launcher=root/"launcher.py"
            launcher.write_text(LAUNCHER)
            config=root/"launcher-config.json"
            config.write_text(json.dumps({
                "module_dir":str(HERE),"tests_dir":str(TESTS),
                "host_config":str(fx["config"]),
                "projects":str(fx["projects_file"]),
            }))
            params=StdioServerParameters(
                command=sys.executable,args=["-B",str(launcher),str(config)],
                env=dict(os.environ,XDG_STATE_HOME=str(root/"state"),
                         PYTHONDONTWRITEBYTECODE="1"),
            )
            with (root/"mcp-stderr.log").open("w+") as log:
                async with asyncio.timeout(95):
                    async with stdio_client(params,errlog=log) as (read,write):
                        async with ClientSession(read,write) as client:
                            await client.initialize()
                            tools={x.name:x for x in (await client.list_tools()).tools}
                            for name,uri in (
                                ("open_git_approval","ui://git-operations/approval-v1.html"),
                                ("open_registered_host_approval",
                                 "ui://host-operations/registered-approval-v1.html"),
                            ):
                                self.assertEqual(tools[name].meta["ui"]["resourceUri"],uri)
                                self.assertEqual(tools[name].meta["ui"]["visibility"],["model"])
                            for name in (
                                "confirm_git_approval","cancel_git_approval",
                                "confirm_registered_host_approval",
                                "cancel_registered_host_approval",
                            ):
                                self.assertEqual(tools[name].meta["ui"]["visibility"],["app"])
                                self.assertTrue(tools[name].meta["openai/widgetAccessible"])
                            resources=await client.list_resources()
                            for uri in ("ui://git-operations/approval-v1.html",
                                        "ui://host-operations/registered-approval-v1.html"):
                                self.assertIn(uri,{str(r.uri) for r in resources.resources})
                                ui=(await client.read_resource(uri)).contents[0]
                                self.assertEqual(ui.mimeType,"text/html;profile=mcp-app")
                                self.assertEqual(ui.meta["ui"]["csp"],
                                                 {"connectDomains":[],"resourceDomains":[]})
                            git_plan=await client.call_tool("prepare_git_operation",{
                                "project":"tossApp","action":"commit",
                                "paths":["server/src/main.ts"],
                                "message":"Fixture change\n\n### References\n- Issue: "
                                          "https://github.com/jhw7500/personal-ops/issues/1",
                            })
                            log.flush()
                            self.assertFalse(git_plan.isError,text_reply(git_plan)+
                                (root/"mcp-stderr.log").read_text()[-1000:])
                            inline=git_plan.structuredContent
                            self.assertEqual(inline["component"],"git_operation")
                            self.assertEqual(inline["state"],"pending")
                            self.assertTrue(inline["approval_required"])
                            self.assertEqual(inline["plan"]["status"],
                                             "pending_host_approval")
                            g_id=inline["operation_id"]
                            git_card=await client.call_tool("open_git_approval",{
                                "project":"tossApp","operation_id":g_id})
                            self.assertFalse(git_card.isError,text_reply(git_card))
                            self.assertEqual(git_card.structuredContent["component"],
                                             "git_operation")
                            g_token=git_card.meta["git_confirmation"]
                            self.assertNotIn(g_token,text_reply(git_card))
                            denied=await client.call_tool("confirm_git_approval",{
                                "project":"tossApp","operation_id":g_id,
                                "card_id":"f"*32,"confirmation_token":"b"*64,
                                "injected":"never-approve",
                            })
                            self.assertTrue(denied.isError)
                            self.assertEqual(text_reply(denied),"INVALID_GIT_CARD_ARGUMENTS")
                            self.assertFalse((root/"state/chatgpt-coding-mcp/git-operations/tossApp" /
                                (g_id+".approval.json")).exists())
                            host_plan=await client.call_tool("prepare_host_operation",{
                                "project":"tossApp","target":"tarororo-test",
                                "action":"compose.restart","parameters":{"services":["api"]},
                                "request_id":"registered-chat-1",
                            })
                            self.assertFalse(host_plan.isError,text_reply(host_plan))
                            host_inline=host_plan.structuredContent
                            self.assertEqual(host_inline["component"],"operation")
                            self.assertEqual(host_inline["family"],"registered")
                            self.assertEqual(host_inline["state"],"pending")
                            self.assertTrue(host_inline["approval_required"])
                            self.assertEqual(host_inline["plan"]["state"],
                                             "pending_host_approval")
                            h_id=host_inline["operation_id"]
                            host_card=await client.call_tool("open_registered_host_approval",{
                                "project":"tossApp","target":"tarororo-test",
                                "operation_id":h_id})
                            self.assertFalse(host_card.isError,text_reply(host_card))
                            self.assertEqual(host_card.structuredContent["component"],
                                             "registered_host")
                            h_token=host_card.meta["registered_host_confirmation"]
                            self.assertNotIn(h_token,text_reply(host_card))
                            denied=await client.call_tool(
                                "confirm_registered_host_approval",{
                                    "project":"tossApp","target":"tarororo-test",
                                    "operation_id":h_id,
                                    "card_id":"f"*32,"confirmation_token":"b"*64,
                                    "injected":"never-approve"})
                            self.assertTrue(denied.isError)
                            self.assertEqual(text_reply(denied),
                                             "INVALID_REGISTERED_CARD_ARGUMENTS")
                            receipts=root/"state/chatgpt-coding-mcp/host-runtime/registered/receipts"
                            self.assertEqual(list(receipts.glob("*.json")),[])
                            # Preparation and opening alone cannot mutate either repo or Docker.
                            self.assertEqual(run_git(fx["root"],"show","HEAD:server/src/main.ts")
                                             .stdout,"export default 1;\n")
                            self.assertEqual(run_git(fx["root"],"status","--short")
                                             .stdout.strip(),"M server/src/main.ts")
