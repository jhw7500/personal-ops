"""Exercise an installed server and its actual MCP wire contracts in fixtures."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace_policy import WorkspacePolicy
from workspace_authority import WorkspaceAuthority


class WorkspaceRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_active_policy_routes_artifacts_and_private_callbacks(self):
        with tempfile.TemporaryDirectory(prefix="workspace-sdk-") as folder:
            root = Path(folder)
            base = root / "projects"
            project = base / "alpha"
            project.joinpath("build/results").mkdir(parents=True)
            project.joinpath("source.txt").write_text("ordinary source")
            project.joinpath(".gitignore").write_text("build/\n.env\n")
            project.joinpath(".env").write_text("SYNTHETIC_SECRET_SHOULD_NOT_LEAK")
            project.joinpath("build/results/result.json").write_text(json.dumps({
                "text": "synthetic reading", "private_field": "SYNTHETIC_HIDDEN_VALUE"}))
            projects = {"alpha": project}
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": {"alpha": str(project)}}))
            config_dir = root / "config/chatgpt-coding-mcp"
            config_dir.parent.mkdir(mode=0o700)
            config_dir.mkdir(mode=0o700)
            policy_file = config_dir / "workspace-policy.json"
            policy_file.write_text(json.dumps({"version": 1, "base_root": str(base), "artifacts": [{
                "project": "alpha", "path": "build/results", "kind": "directory",
                "operations": ["list", "read"], "fields": ["text"],
                "recursive": False, "max_file_bytes": 1048576}]}))
            policy_file.chmod(0o600)
            module_dir = Path(__file__).resolve().parents[1]
            spec = importlib.util.spec_from_file_location("workspace_test_installer", module_dir / "install-runtime.py")
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            installed = installer.install(module_dir, root / "releases", projects)
            release = Path(installed["runtime_dir"])
            manifest = json.loads((release / "manifest.json").read_text())
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            authority_path = root / "state/chatgpt-coding-mcp/workspace-authority"
            authority = WorkspaceAuthority(projects, authority_path)
            authority.initialize(WorkspacePolicy(projects, policy_file).load(required=True).digest,
                                 {digest: 1})
            params = StdioServerParameters(command=sys.executable,
                args=["-B", str(release / "server.py"), "--config", str(config)],
                env=dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1",
                         XDG_CONFIG_HOME=str(root / "config"), XDG_STATE_HOME=str(root / "state")))
            with (root / "server.log").open("w+") as log:
                async with asyncio.timeout(150):
                    async with stdio_client(params, errlog=log) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            catalog = await client.list_tools()
                            tools = {tool.name: tool for tool in catalog.tools}
                            self.assertIn("read_registered_artifact", tools)
                            self.assertIn("publish_workspace_output", tools)
                            self.assertTrue(tools["start_command"].inputSchema["properties"]["writable_cwd"])
                            self.assertEqual(tools["confirm_workspace_access_approval"].meta["ui"]["visibility"], ["app"])
                            response = await client.call_tool("read_registered_artifact", {
                                "project": "alpha", "relative_path": "build/results/result.json"})
                            self.assertFalse(response.isError)
                            self.assertEqual(json.loads(response.structuredContent["content"]), {"text": "synthetic reading"})
                            self.assertNotIn("SYNTHETIC_HIDDEN_VALUE", str(response))
                            denied = await client.call_tool("read_file", {
                                "project": "alpha", "relative_path": "build/results/result.json"})
                            self.assertTrue(denied.isError)
                            self.assertNotIn("SYNTHETIC_HIDDEN_VALUE", str(denied))
                            secret = await client.call_tool("read_file", {"project": "alpha", "relative_path": ".env"})
                            self.assertTrue(secret.isError)
                            self.assertNotIn("SYNTHETIC_SECRET_SHOULD_NOT_LEAK", str(secret))
                            source = await client.call_tool("read_file", {"project": "alpha", "relative_path": "source.txt"})
                            self.assertFalse(source.isError)
                            self.assertIn("ordinary source", str(source))
                            started = await client.call_tool("start_command", {"project": "alpha",
                                "request_id": "sdk-output", "argv": ["bash", "-c", "printf published > /output/result.txt"],
                                "writable_cwd": True})
                            self.assertFalse(started.isError, started)
                            job_id = started.structuredContent["job_id"]
                            for _ in range(100):
                                status = await client.call_tool("get_job", {"project": "alpha", "job_id": job_id})
                                if status.structuredContent["state"] not in {"queued", "running", "canceling"}:
                                    break
                                await asyncio.sleep(.02)
                            self.assertEqual(status.structuredContent["state"], "succeeded", status)
                            self.assertTrue(status.structuredContent["termination_verified"])
                            self.assertNotIn(str(root / "state"), str(status))
                            output = await client.call_tool("read_workspace_output", {
                                "project": "alpha", "job_id": job_id, "relative_path": "result.txt"})
                            self.assertFalse(output.isError, output)
                            self.assertEqual(output.structuredContent["content"], "published")
                            published = await client.call_tool("publish_workspace_output", {
                                "project": "alpha", "job_id": job_id, "relative_output": "result.txt",
                                "target_path": "published.txt", "request_id": "sdk-output"})
                            self.assertFalse(published.isError, published)
                            self.assertEqual(project.joinpath("published.txt").read_text(), "published")
                            denied = await client.call_tool("publish_workspace_output", {
                                "project": "alpha", "job_id": job_id, "relative_output": "result.txt",
                                "target_path": ".env", "request_id": "sdk-output"})
                            self.assertTrue(denied.isError)
                            self.assertEqual(project.joinpath(".env").read_text(), "SYNTHETIC_SECRET_SHOULD_NOT_LEAK")
                            invalid = await client.call_tool("confirm_workspace_access_approval", {
                                "project": "alpha", "request_id": "a" * 32,
                                "confirmation_token": "SYNTHETIC_PRIVATE_BAD_TOKEN"})
                            self.assertTrue(invalid.isError)
                            self.assertNotIn("SYNTHETIC_PRIVATE_BAD_TOKEN", str(invalid))
                            activity = authority_path.parent / "activity.jsonl"
                            records = [json.loads(line) for line in activity.read_text().splitlines()]
                            self.assertTrue(any(row["tool"] == "confirm_workspace_access_approval"
                                                and row["phase"] == "failed" for row in records))
                            self.assertTrue(any(row["tool"] == "read_file" and row["phase"] == "failed"
                                                for row in records))
                            self.assertNotIn("SYNTHETIC_PRIVATE_BAD_TOKEN", activity.read_text())
                            self.assertNotIn("SYNTHETIC_HIDDEN_VALUE", activity.read_text())


if __name__ == "__main__":
    unittest.main()
