"""Real MCP SDK/stdio registration and fail-closed optional-import regression."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODULE_DIR = Path(__file__).resolve().parents[1]
AGENT_NAMES = {"list_agent_profiles", "prepare_agent_task", "start_agent_task",
               "get_agent_task", "cancel_agent_task"}
HOST_ACCESS_NAMES = {"request_host_access", "get_host_access", "start_host_execution",
                     "get_host_execution", "read_host_execution_log", "cancel_host_execution"}
LAUNCHER = """
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import server
server.SERENA_ENV = Path(sys.executable).parent.parent
from agent_profiles import AgentProfileRegistry
from agent_tasks import AgentTaskManager
projects = {'demo': Path(sys.argv[2])}
profiles = AgentProfileRegistry(projects, config_path=Path(sys.argv[3]))
# Test-owned source manager, empty synthetic profile registry. Never a host grant.
def fixture_manager(projects, **kwargs):
    return AgentTaskManager(projects, profiles=profiles,
                            state_dir=kwargs['state_dir'], trusted_runtime=True)
server.agent_tools.AgentTaskManager = fixture_manager
asyncio.run(server.serve_with_shutdown(projects))
"""


class AgentRegistrationTest(unittest.IsolatedAsyncioTestCase):
    def test_agent_dependency_import_failure_does_not_silently_remove_tools(self):
        script = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == 'agent_tools':
        raise ImportError('synthetic required agent dependency failure')
    return original(name, *args, **kwargs)
builtins.__import__ = checked
import server
"""
        result = subprocess.run([sys.executable, "-c", script, str(MODULE_DIR)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=15, check=False)
        self.assertNotEqual(result.returncode, 0,
                            "Missing required agent module was silently ignored")

    async def test_five_agent_tools_registered_and_unconfigured_execution_is_denied(self):
        with tempfile.TemporaryDirectory(prefix="agent-sdk-registration-") as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            config = root / "host-runtime.json"
            config.write_text(json.dumps({"version": 3, "enabled": True,
                                          "legacy": None, "targets": {},
                                          "agent_profiles": {}}))
            config.chmod(0o600)
            launcher = root / "launcher.py"
            launcher.write_text(LAUNCHER)
            # This subprocess starts a source server, so it must not discover
            # an owner's installed Workspace policy through the default XDG path.
            for name in ("config", "state", "cache"):
                (root / name).mkdir(mode=0o700)
            env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
                   if key in os.environ}
            env.update(HOME=os.environ["HOME"], XDG_STATE_HOME=str(root / "state"),
                       XDG_CONFIG_HOME=str(root / "config"),
                       XDG_CACHE_HOME=str(root / "cache"),
                       PYTHONDONTWRITEBYTECODE="1")
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(launcher), str(MODULE_DIR), str(project), str(config)], env=env)
            with (root / "server.log").open("w") as log:
                async with asyncio.timeout(40):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            catalog = await client.list_tools()
                            specs = {tool.name: tool for tool in catalog.tools}
                            self.assertTrue(AGENT_NAMES <= set(specs))
                            self.assertTrue(HOST_ACCESS_NAMES <= set(specs))
                            self.assertEqual(specs["prepare_agent_task"].inputSchema["properties"]["execution_mode"]["enum"],
                                             ["auto", "isolated", "host"])
                            for name in HOST_ACCESS_NAMES:
                                self.assertFalse(specs[name].inputSchema["additionalProperties"])
                            access = await client.call_tool("get_host_access", {"project": "demo"})
                            self.assertFalse(access.isError, access.content)
                            self.assertFalse(access.structuredContent["active"])
                            denied_host = await client.call_tool("start_host_execution", {
                                "project": "demo", "executable": "/usr/bin/id", "argv": [],
                                "cwd": ".", "request_id": "unapproved-host-command"})
                            self.assertTrue(denied_host.isError)
                            fake_grant = await client.call_tool("get_host_access", {"project": "demo", "approved": True})
                            self.assertTrue(fake_grant.isError)
                            for name in AGENT_NAMES:
                                self.assertFalse(specs[name].inputSchema["additionalProperties"])
                            self.assertTrue(specs["list_agent_profiles"].annotations.readOnlyHint)
                            self.assertTrue(specs["get_agent_task"].annotations.readOnlyHint)
                            result = await client.call_tool("list_agent_profiles", {"project": "demo"})
                            self.assertFalse(result.isError, result.content)
                            self.assertEqual(result.structuredContent,
                                             {"project": "demo", "profiles": []})
                            denied = await client.call_tool("prepare_agent_task", {
                                "project": "demo", "profile_id": "unregistered",
                                "objective": "Review disposable source files only.",
                                "reason": "WORKFLOW_COMPLEXITY", "request_id": "offline-probe"})
                            self.assertTrue(denied.isError)
                            self.assertIn("PROFILE_NOT_FOUND", " ".join(
                                item.text for item in denied.content if item.type == "text"))
                            extra = await client.call_tool("list_agent_profiles", {
                                "project": "demo", "approved": True})
                            self.assertTrue(extra.isError)


if __name__ == "__main__":
    unittest.main()
