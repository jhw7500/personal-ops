"""Exercise the public DB tool route with schema-update plans and no host IO."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MODULE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_ID = "a" * 32
PREDECESSOR_ID = "b" * 32
LAUNCHER = """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import server
from host_runtime_config import HostRuntimeError

class Operations:
    def __init__(self, *args, **kwargs):
        pass

    async def execute(self, project, target, operation_id, *, expected_component=None):
        component = 'schema_update' if operation_id == 'a' * 32 else 'predecessor_migration'
        if component not in expected_component:
            raise HostRuntimeError('WRONG_OPERATION_TYPE')
        return {'operation_id': operation_id, 'component': component, 'state': 'queued'}

server.HostRuntimeOperations = Operations
asyncio.run(server.serve_with_shutdown({'tossApp': Path(sys.argv[2])}))
"""


class SchemaRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_db_execute_accepts_combined_schema_plan_and_rejects_other_routes(self):
        with tempfile.TemporaryDirectory(prefix="schema-route-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(launcher), str(MODULE_DIR), str(project)],
                env=dict(os.environ, XDG_STATE_HOME=str(root / "state"),
                         PYTHONDONTWRITEBYTECODE="1"),
            )
            with (root / "server.log").open("w") as log:
                async with asyncio.timeout(40):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            scope = {"project": "tossApp", "target": "tarororo-test"}
                            result = await client.call_tool("execute_db_migration", {
                                **scope, "operation_id": SCHEMA_ID,
                            })
                            self.assertFalse(result.isError, result.content)
                            self.assertEqual(result.structuredContent["component"], "schema_update")
                            self.assertEqual(result.structuredContent["state"], "queued")
                            for tool, operation_id in (
                                ("execute_predecessor_migration", SCHEMA_ID),
                                ("execute_test_deploy", SCHEMA_ID),
                                ("execute_db_migration", PREDECESSOR_ID),
                            ):
                                refused = await client.call_tool(tool, {
                                    **scope, "operation_id": operation_id,
                                })
                                self.assertTrue(refused.isError, tool)
                                self.assertIn("WRONG_OPERATION_TYPE", " ".join(
                                    item.text for item in refused.content if item.type == "text"))
            activity = root / "state" / "chatgpt-coding-mcp"
            self.assertTrue(activity.is_dir())


if __name__ == "__main__":
    unittest.main()
