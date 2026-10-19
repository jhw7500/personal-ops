"""Real SDK with disposable state and fake systemd launcher only."""

import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODULE_DIR = Path(__file__).resolve().parents[1]
CARD_URI = "ui://runtime-rollouts/approval-v1.html"
LAUNCHER = r"""
import asyncio,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import server,host_access
from runtime_rollout_web import RuntimeRolloutWeb
root=Path(sys.argv[2])
host_access._installed_runtime_identity=lambda:'a'*64
def fake(argv):
 p=root/'executions';p.write_text(str(int(p.read_text())+1) if p.exists() else '1');return True
manager=RuntimeRolloutWeb({'personal-ops':root/'repo'},state_dir=root/'operation-state',launcher=fake,
 runtime_reader=lambda:'a'*64,boot_reader=lambda:'11111111-2222-3333-4444-555555555555',
 uid_reader=lambda:1000,test_only=True)
server.RuntimeRolloutWeb=lambda *a,**k:manager
asyncio.run(server.serve_with_shutdown({'personal-ops':root/'repo'}))
"""


def text(result):
    return "\n".join(x.text for x in result.content if x.type == "text")


class RuntimeRolloutChatRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_protocol_preserves_hidden_capability_and_single_use_execution(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="runtime-chat-router-") as folder:
            root = Path(folder)
            for n in ["chatgpt-mcp", "work-dashboard"]:
                shutil.copytree(
                    MODULE_DIR.parent / n,
                    root / "repo" / n,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", str(launcher), str(MODULE_DIR), str(root)],
                env={
                    **os.environ,
                    "XDG_STATE_HOME": str(root / "state"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {
                                t.name: t for t in (await client.list_tools()).tools
                            }
                            self.assertEqual(
                                tools["open_runtime_rollout_approval"].meta["ui"],
                                {"visibility": ["model"], "resourceUri": CARD_URI},
                            )
                            for n in [
                                "confirm_runtime_rollout_approval",
                                "cancel_runtime_rollout_approval",
                            ]:
                                self.assertEqual(
                                    tools[n].meta["ui"]["visibility"], ["app"]
                                )
                                self.assertTrue(
                                    tools[n].meta["openai/widgetAccessible"]
                                )
                            resource = (await client.read_resource(CARD_URI)).contents[
                                0
                            ]
                            self.assertEqual(
                                resource.mimeType, "text/html;profile=mcp-app"
                            )
                            self.assertEqual(
                                resource.meta["ui"]["csp"],
                                {"connectDomains": [], "resourceDomains": []},
                            )
                            prepared = await client.call_tool(
                                "prepare_runtime_rollout",
                                {"project": "personal-ops", "request_id": "chat-card"},
                            )
                            self.assertFalse(prepared.isError, text(prepared))
                            op = {
                                "project": "personal-ops",
                                "operation_id": prepared.structuredContent[
                                    "operation_id"
                                ],
                            }
                            self.assertEqual(
                                prepared.structuredContent["approval_card"],
                                {
                                    "tool": "open_runtime_rollout_approval",
                                    "arguments": op,
                                },
                            )
                            opened = await client.call_tool(
                                "open_runtime_rollout_approval", op
                            )
                            self.assertFalse(opened.isError, text(opened))
                            p = opened.structuredContent
                            token = opened.meta["runtime_rollout_confirmation"]
                            self.assertNotIn(token, text(opened))
                            self.assertNotIn(str(root), text(opened))
                            self.assertNotIn("approval_url", p["plan"])
                            self.assertFalse((root / "executions").exists())
                            malformed = await client.call_tool(
                                "confirm_runtime_rollout_approval",
                                {
                                    **op,
                                    "card_id": p["card_id"],
                                    "confirmation_token": token,
                                    "leak_probe": token,
                                },
                            )
                            self.assertTrue(malformed.isError)
                            self.assertEqual(
                                text(malformed), "INVALID_ROLLOUT_CARD_ARGUMENTS"
                            )
                            self.assertNotIn(token, text(malformed))
                            confirmed = await client.call_tool(
                                "confirm_runtime_rollout_approval",
                                {
                                    **op,
                                    "card_id": p["card_id"],
                                    "confirmation_token": token,
                                },
                            )
                            self.assertFalse(confirmed.isError, text(confirmed))
                            self.assertTrue(
                                confirmed.structuredContent["receipt_verified"]
                            )
                            self.assertFalse((root / "executions").exists())
                            reopened = await client.call_tool(
                                "open_runtime_rollout_approval", op
                            )
                            self.assertFalse(reopened.isError, text(reopened))
                            self.assertFalse(
                                reopened.structuredContent["approval_required"]
                            )
                            self.assertFalse(reopened.meta)
                            executed = await client.call_tool(
                                "execute_runtime_rollout", op
                            )
                            self.assertFalse(executed.isError, text(executed))
                            self.assertEqual(
                                executed.structuredContent["state"], "queued"
                            )
                            repeated = await client.call_tool(
                                "execute_runtime_rollout", op
                            )
                            self.assertFalse(repeated.isError, text(repeated))
                            self.assertEqual((root / "executions").read_text(), "1")
                            self.assertNotIn(token, (root / "server.log").read_text())
