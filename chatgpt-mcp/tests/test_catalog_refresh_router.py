from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).resolve().parents[1] / "server.py"
REFRESH_NAMES = {"refresh_mcp_catalog", "get_mcp_catalog_refresh_status"}


def result_text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class CatalogRefreshRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_notification_and_same_session_list_confirm_refetch(self):
        with tempfile.TemporaryDirectory(prefix="catalog-refresh-router-") as folder:
            root = Path(folder)
            project = root / "personal-ops"
            project.mkdir()
            config_dir = root / "config"
            state_dir = root / "state"
            cache_dir = root / "cache"
            for directory in (config_dir, state_dir, cache_dir):
                directory.mkdir(mode=0o700)
            config = config_dir / "projects.json"
            config.write_text(json.dumps({"projects": {"personal-ops": str(project)}}))
            config.chmod(0o600)
            notifications = asyncio.Queue()

            async def message_handler(message):
                if (isinstance(message, types.ServerNotification)
                        and isinstance(message.root, types.ToolListChangedNotification)):
                    await notifications.put(message.root.method)

            env = dict(os.environ, XDG_CONFIG_HOME=str(config_dir),
                       XDG_STATE_HOME=str(state_dir), XDG_CACHE_HOME=str(cache_dir),
                       PYTHONDONTWRITEBYTECODE="1")
            params = StdioServerParameters(
                command=sys.executable, args=[str(SERVER), "--config", str(config)], env=env)
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(
                            read, write, message_handler=message_handler
                        ) as client:
                            initialized = await client.initialize()
                            self.assertTrue(initialized.capabilities.tools.listChanged)
                            catalog = await client.list_tools()
                            specs = {item.name: item for item in catalog.tools}
                            self.assertTrue(REFRESH_NAMES <= set(specs))
                            for name in REFRESH_NAMES:
                                self.assertEqual(
                                    specs[name].inputSchema["properties"]["project"]["enum"],
                                    ["personal-ops"],
                                )
                                self.assertEqual(specs[name].meta["ui"]["visibility"], ["model"])

                            requested = await client.call_tool(
                                "refresh_mcp_catalog", {"project": "personal-ops"})
                            self.assertFalse(requested.isError, result_text(requested))
                            self.assertEqual(requested.structuredContent["status"], "notification_sent")
                            self.assertFalse(requested.structuredContent["chatgpt_metadata_verified"])
                            self.assertEqual(
                                await asyncio.wait_for(notifications.get(), timeout=5),
                                "notifications/tools/list_changed",
                            )

                            # An SDK cache miss internally lists tools without a client request.
                            unknown = await client.call_tool("unknown_catalog_probe", {})
                            self.assertTrue(unknown.isError)
                            before_list = await client.call_tool(
                                "get_mcp_catalog_refresh_status", {"project": "personal-ops"})
                            self.assertEqual(before_list.structuredContent["status"],
                                             "notification_sent")
                            await client.list_tools()
                            after_list = await client.call_tool(
                                "get_mcp_catalog_refresh_status", {"project": "personal-ops"})
                            self.assertEqual(after_list.structuredContent["status"],
                                             "catalog_refetched")
                            self.assertTrue(after_list.structuredContent["catalog_refetched"])
                            self.assertFalse(after_list.structuredContent[
                                "chatgpt_metadata_verified"])

                            for arguments in (
                                {"project": "other"},
                                {"project": "personal-ops", "extra": True},
                            ):
                                invalid = await client.call_tool("refresh_mcp_catalog", arguments)
                                self.assertTrue(invalid.isError)

    async def test_refresh_tools_are_absent_without_personal_ops(self):
        with tempfile.TemporaryDirectory(prefix="catalog-refresh-absent-") as folder:
            root = Path(folder)
            project = root / "other"
            project.mkdir()
            config_dir = root / "config"
            state_dir = root / "state"
            cache_dir = root / "cache"
            for directory in (config_dir, state_dir, cache_dir):
                directory.mkdir(mode=0o700)
            config = config_dir / "projects.json"
            config.write_text(json.dumps({"projects": {"other": str(project)}}))
            config.chmod(0o600)
            env = dict(os.environ, XDG_CONFIG_HOME=str(config_dir),
                       XDG_STATE_HOME=str(state_dir), XDG_CACHE_HOME=str(cache_dir),
                       PYTHONDONTWRITEBYTECODE="1")
            params = StdioServerParameters(
                command=sys.executable, args=[str(SERVER), "--config", str(config)], env=env)
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            names = {item.name for item in (await client.list_tools()).tools}
                            self.assertTrue(REFRESH_NAMES.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
