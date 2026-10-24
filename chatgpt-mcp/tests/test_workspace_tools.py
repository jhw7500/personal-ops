"""Consumer-visible workspace reads, denials and private approval capabilities."""
import importlib
import importlib.util
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-tools-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "projects"
        self.project = self.base / "alpha"
        self.project.mkdir(parents=True)
        self.owner = self.root / "owner"
        self.owner.mkdir(mode=0o700)
        self.config = self.owner / "workspace-policy.json"
        self.policy = {"version": 1, "base_root": str(self.base), "artifacts": [{
            "project": "alpha", "path": "build/results", "kind": "directory",
            "operations": ["list", "read"], "fields": ["text", "usage"],
            "recursive": False, "max_file_bytes": 1048576,
        }]}
        self.config.write_text(json.dumps(self.policy))
        self.config.chmod(0o600)
        self.project.joinpath(".gitignore").write_text("build/\n.env\n")
        self.project.joinpath("build/results").mkdir(parents=True)
        self.result = self.project / "build/results/result.json"
        self.result.write_text(json.dumps({
            "text": "synthetic reading", "usage": {"input_tokens": 7},
            "unregistered": "never-return-this-value",
        }))

    def tools(self, initialize=True):
        self.assertIsNotNone(importlib.util.find_spec("workspace_tools"),
                             "The workspace tools have not been implemented")
        module = importlib.import_module("workspace_tools")
        tools = module.WorkspaceTools({"alpha": self.project}, self.owner / "authority",
                                      config_path=self.config)
        if initialize:
            tools.authority.initialize(tools.policy.load(required=True).digest,
                                       allowed_releases={"a" * 64: 1})
        return tools

    def call(self, tools, name, **arguments):
        result = tools.call(name, {"project": "alpha", **arguments})
        return result, result.get("structuredContent", {})

    def test_registered_ignored_json_only_returns_owner_selected_fields(self):
        tools = self.tools()
        result, payload = self.call(tools, "read_registered_artifact",
                                    relative_path="build/results/result.json")
        self.assertFalse(result.get("isError", False))
        self.assertEqual(json.loads(payload["content"]), {
            "text": "synthetic reading", "usage": {"input_tokens": 7}})
        self.assertNotIn("never-return-this-value", json.dumps(result))
        self.assertEqual(self.project.joinpath(".gitignore").read_text(), "build/\n.env\n")
        self.assertEqual(len(payload["file_sha256"]), 64)

    def test_unregistered_artifact_is_a_protocol_error_without_payload(self):
        tools = self.tools()
        self.project.joinpath("build/unregistered.json").write_text('{"text":"hidden"}')
        result, payload = self.call(tools, "read_registered_artifact",
                                    relative_path="build/unregistered.json")
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "denied")
        self.assertNotIn("content", payload)
        self.assertNotIn("hidden", json.dumps(result))

    def test_cursor_rejects_file_change_and_cross_path_reuse(self):
        tools = self.tools()
        self.result.write_text(json.dumps({"text": "x" * 300, "usage": {}}))
        _, first = self.call(tools, "read_registered_artifact",
                             relative_path="build/results/result.json", max_bytes=32)
        self.assertTrue(first["has_more"])
        self.result.write_text(json.dumps({"text": "changed", "usage": {}}))
        result, _ = self.call(tools, "read_registered_artifact",
                              relative_path="build/results/result.json", cursor=first["next_cursor"])
        self.assertTrue(result["isError"])
        other = self.project / "build/results/other.json"
        other.write_text(json.dumps({"text": "x" * 300, "usage": {}}))
        result, _ = self.call(tools, "read_registered_artifact",
                              relative_path="build/results/other.json", cursor=first["next_cursor"])
        self.assertTrue(result["isError"])

    def test_selected_nested_credential_is_rejected_without_echo(self):
        tools = self.tools()
        self.result.write_text(json.dumps({"text": {"access_token": "synthetic-secret-value"}}))
        result, _ = self.call(tools, "read_registered_artifact",
                              relative_path="build/results/result.json")
        self.assertTrue(result["isError"])
        self.assertNotIn("synthetic-secret-value", json.dumps(result))

    def test_missing_authority_fails_closed_without_initializing_it(self):
        tools = self.tools(initialize=False)
        result, _ = self.call(tools, "read_registered_artifact",
                              relative_path="build/results/result.json")
        self.assertTrue(result["isError"])
        self.assertFalse((self.owner / "authority").exists())

    def test_activation_latch_prevents_legacy_fallback_after_policy_and_state_loss(self):
        tools = self.tools()
        self.assertTrue(tools.authority.enforcement_path.exists())
        self.config.unlink()
        shutil.rmtree(tools.authority.state_dir)
        self.assertTrue(tools.enabled)
        denied, _ = self.call(tools, "read_workspace_file", path=str(self.result))
        self.assertTrue(denied["isError"])

    def test_list_permission_does_not_imply_read_permission(self):
        self.policy["artifacts"][0]["operations"] = ["list"]
        self.config.write_text(json.dumps(self.policy))
        tools = self.tools()
        result, listing = self.call(tools, "list_registered_artifacts",
                                    relative_path="build/results")
        self.assertFalse(result.get("isError", False))
        self.assertEqual(listing["files"][0]["path"], "build/results/result.json")
        result, _ = self.call(tools, "read_registered_artifact",
                              relative_path="build/results/result.json")
        self.assertTrue(result["isError"])

    def test_outside_file_needs_private_confirmation_and_exact_read_grant(self):
        tools = self.tools()
        outside = self.root / "outside.txt"
        outside.write_text("approved synthetic outside content")
        sibling = self.root / "sibling.txt"
        sibling.write_text("unapproved sibling")
        result, _ = self.call(tools, "read_workspace_file", path=str(outside))
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "WORKSPACE_SCOPE_REQUIRED")
        opened, request = self.call(tools, "open_workspace_access_approval", path=str(outside),
                                    operations=["read"], purpose="read requested synthetic file")
        token = opened["_meta"]["workspace_confirmation"]
        self.assertNotIn(token, json.dumps(request))
        self.assertNotIn(token, json.dumps(opened["content"]))
        approved, grant = self.call(tools, "confirm_workspace_access_approval",
                                    request_id=request["request_id"], confirmation_token=token)
        self.assertFalse(approved.get("isError", False))
        result, payload = self.call(tools, "read_workspace_file", path=str(outside),
                                    grant_id=grant["grant_id"])
        self.assertFalse(result.get("isError", False))
        self.assertEqual(payload["content"], "approved synthetic outside content")
        denied, _ = self.call(tools, "read_workspace_file", path=str(sibling),
                              grant_id=grant["grant_id"])
        self.assertTrue(denied["isError"])
        replay, _ = self.call(tools, "confirm_workspace_access_approval",
                              request_id=request["request_id"], confirmation_token=token)
        self.assertTrue(replay["isError"])

    def test_approval_schema_and_dispatch_default_to_thirty_days(self):
        tools = self.tools()
        spec = tools.specs["open_workspace_access_approval"]
        duration = spec["inputSchema"]["properties"]["duration_seconds"]
        self.assertEqual(duration, {
            "type": "integer", "minimum": 1,
            "maximum": 2592000, "default": 2592000,
        })

        outside = self.root / "long-lived.txt"
        outside.write_text("bounded")
        _, request = self.call(
            tools, "open_workspace_access_approval", path=str(outside),
            operations=["read"], purpose="long-running review",
        )
        self.assertEqual(request["duration_seconds"], 2592000)

    def test_generic_workspace_read_cannot_bypass_artifact_registration(self):
        tools = self.tools()
        hidden = self.project / "build/unregistered.json"
        hidden.write_text('{"unregistered":"must-stay-hidden"}')
        denied, _ = self.call(tools, "read_workspace_file", path=str(hidden))
        self.assertTrue(denied["isError"])
        self.assertNotIn("must-stay-hidden", json.dumps(denied))

    def test_active_source_edit_preserves_mode_and_rejects_protected_creation(self):
        tools = self.tools()
        source = self.project / "run.sh"
        source.write_text("#!/bin/sh\necho before\n")
        source.chmod(0o755)
        result = tools.file_tool("replace_content", {"project": "alpha", "relative_path": "run.sh",
                                                     "needle": "before", "repl": "after", "mode": "literal"})
        self.assertFalse(result.get("isError", False))
        self.assertIn("after", source.read_text())
        self.assertEqual(source.stat().st_mode & 0o777, 0o755)
        denied = tools.file_tool("create_text_file", {"project": "alpha", "relative_path": ".env",
                                                      "content": "sensitive=new"})
        self.assertTrue(denied["isError"])
        self.assertFalse(self.project.joinpath(".env").exists())

    def test_active_source_search_excludes_ignored_and_secret_paths(self):
        tools = self.tools()
        self.project.joinpath("source.txt").write_text("needle source")
        self.project.joinpath(".env").write_text("needle secret")
        result = tools.file_tool("search_for_pattern", {"project": "alpha", "substring_pattern": "needle"})
        self.assertFalse(result.get("isError", False))
        serialized = json.dumps(result)
        self.assertIn("source.txt", serialized)
        self.assertNotIn("needle secret", serialized)
        self.assertNotIn("build/results", serialized)

    def test_authority_runtime_and_protected_descendants_never_become_path_grants(self):
        protected = self.project / "internal"
        protected.mkdir()
        protected.joinpath("private.txt").write_text("protected workspace payload")
        self.policy["protected_paths"] = [str(protected)]
        self.config.write_text(json.dumps(self.policy))
        tools = self.tools()
        for target in (tools.authority.state_dir / "state.json", protected / "private.txt",
                       Path.home() / ".local/lib/chatgpt-coding-mcp/workspace-controller/workspace_control.py"):
            denied, _ = self.call(tools, "open_workspace_access_approval", path=str(target),
                                  operations=["read", "write"], purpose="synthetic denied state access")
            self.assertTrue(denied["isError"], target)
        listed, payload = self.call(tools, "list_workspace_files", path=str(self.project))
        self.assertFalse(listed["isError"])
        self.assertNotIn("internal", json.dumps(payload))

    def test_workspace_listing_does_not_bypass_ignored_or_registered_paths(self):
        tools = self.tools()
        self.project.joinpath("normal").mkdir()
        self.project.joinpath("normal/child.txt").write_text("ordinary")
        listed, payload = self.call(tools, "list_workspace_files", path=str(self.project))
        self.assertFalse(listed["isError"])
        self.assertIn("normal", json.dumps(payload))
        self.assertNotIn("build", json.dumps(payload))

    def test_custom_controller_installation_is_permanently_protected(self):
        controller = self.root / "custom-controller"
        controller.mkdir(mode=0o700)
        controller.joinpath("controller-manifest.json").write_text("{}")
        code = controller / "workspace_control.py"
        code.write_text("custom protected controller")
        tools = self.tools()
        denied, _ = self.call(tools, "open_workspace_access_approval", path=str(code),
                              operations=["read", "write"], purpose="denied controller access")
        self.assertTrue(denied["isError"])
        denied, _ = self.call(tools, "open_workspace_access_approval",
                              path=str(controller / "controller-manifest.json"),
                              operations=["write"], purpose="denied marker overwrite")
        self.assertTrue(denied["isError"])

    def test_registered_artifact_outside_base_still_requires_exact_read_grant(self):
        other_base = self.root / "other-base"
        other_base.mkdir()
        self.policy["base_root"] = str(other_base)
        self.config.write_text(json.dumps(self.policy))
        tools = self.tools()
        denied, _ = self.call(tools, "read_registered_artifact", relative_path="build/results/result.json")
        self.assertTrue(denied["isError"])
        opened, pending = self.call(tools, "open_workspace_access_approval", path=str(self.result),
                                     operations=["read"], purpose="registered external result")
        _, grant = self.call(tools, "confirm_workspace_access_approval", request_id=pending["request_id"],
                              confirmation_token=opened["_meta"]["workspace_confirmation"])
        read, payload = self.call(tools, "read_registered_artifact", relative_path="build/results/result.json",
                                  grant_id=grant["grant_id"])
        self.assertFalse(read["isError"], read)
        self.assertEqual(json.loads(payload["content"])["text"], "synthetic reading")
        self.assertNotIn("never-return-this-value", json.dumps(read))
        denied, _ = self.call(tools, "list_registered_artifacts", relative_path="build/results",
                              grant_id=grant["grant_id"])
        self.assertTrue(denied["isError"])


if __name__ == "__main__":
    unittest.main()
