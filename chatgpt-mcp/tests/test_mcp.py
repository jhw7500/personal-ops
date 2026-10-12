import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).resolve().parents[1] / "server.py"
TOOLS = {"read_file", "list_dir", "find_file", "search_for_pattern",
         "create_text_file", "replace_content"}


def result_text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class CodingMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_file_editing_over_stdio(self):
        with tempfile.TemporaryDirectory(prefix="chatgpt-mcp-test-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside-project-sentinel", encoding="utf-8")
            env = dict(os.environ, XDG_STATE_HOME=str(root / "state"))
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(SERVER), "--project", str(project)],
                env=env,
            )
            with (root / "server.log").open("w+") as log:
                try:
                    async with asyncio.timeout(45):
                        async with stdio_client(params, errlog=log) as (read, write):
                            async with ClientSession(read, write) as client:
                                await client.initialize()
                                tools = await client.list_tools()
                                self.assertEqual({tool.name for tool in tools.tools}, TOOLS)
                                result = await client.call_tool("create_text_file", {
                                    "relative_path": "example.py", "content": "value = 1\n",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertEqual(project.joinpath("example.py").read_text(), "value = 1\n")
                                result = await client.call_tool("read_file", {"relative_path": "example.py"})
                                self.assertIn("value = 1", result_text(result))
                                result = await client.call_tool("replace_content", {
                                    "relative_path": "example.py", "needle": "value = 1",
                                    "repl": "value = 2", "mode": "literal",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertEqual(project.joinpath("example.py").read_text(), "value = 2\n")
                                result = await client.call_tool("search_for_pattern", {
                                    "substring_pattern": "value = 2", "relative_path": ".",
                                })
                                self.assertIn("example.py", result_text(result))
                                for path in ("../outside.txt", str(outside)):
                                    result = await client.call_tool("read_file", {"relative_path": path})
                                    self.assertNotIn("outside-project-sentinel", result_text(result))
                                result = await client.call_tool("create_text_file", {
                                    "relative_path": "../outside.txt", "content": "changed",
                                })
                                self.assertEqual(outside.read_text(), "outside-project-sentinel")
                    self.assertFalse(project.joinpath(".serena").exists())
                    configs = list(root.joinpath("state").glob("chatgpt-coding-mcp/*/project-data/project.yml"))
                    self.assertEqual(len(configs), 1)
                except BaseException:
                    log.seek(0)
                    print(log.read()[-12000:], file=sys.stderr)
                    raise


if __name__ == "__main__":
    unittest.main()
