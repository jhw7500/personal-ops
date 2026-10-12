import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


SERVER = Path(os.environ.get("CHATGPT_MCP_TEST_SERVER", Path(__file__).resolve().parents[1] / "server.py"))
UI_URI = "ui://host-repositories/project-picker-v2.html"
UI_MIME = "text/html;profile=mcp-app"
PROJECT_TOOLS = {
    "read_file",
    "list_dir",
    "find_file",
    "search_for_pattern",
    "create_text_file",
    "replace_content",
}
SETTINGS_TOOLS = {"read_plugin_settings", "update_plugin_settings", "check_settings_support"}
EXECUTION_TOOLS = {"list_execution_profiles", "start_command", "get_job", "read_job_log", "cancel_job", "list_jobs"}
GIT_TOOLS = {"git_status", "git_diff", "prepare_git_operation", "execute_git_operation", "get_git_operation"}
MEMORY_TOOLS = {"read_project_memory", "update_project_memory"}
PROGRESS_TOOLS = {"read_project_progress", "update_project_progress"}
TOOLS = (PROJECT_TOOLS | {"list_projects", "open_project_picker", "select_project"}
         | SETTINGS_TOOLS | EXECUTION_TOOLS | GIT_TOOLS | MEMORY_TOOLS | PROGRESS_TOOLS
         | {"report_work_events"})
TOOL_PROPERTIES = {
    "read_file": {"relative_path", "start_line", "end_line", "max_answer_chars"},
    "list_dir": {"relative_path", "recursive", "skip_ignored_files", "max_answer_chars"},
    "find_file": {"file_mask", "relative_path"},
    "search_for_pattern": {
        "substring_pattern",
        "context_lines_before",
        "context_lines_after",
        "paths_include_glob",
        "paths_exclude_glob",
        "relative_path",
        "restrict_search_to_code_files",
        "max_answer_chars",
    },
    "create_text_file": {"relative_path", "content"},
    "replace_content": {
        "relative_path",
        "needle",
        "repl",
        "mode",
        "allow_multiple_occurrences",
    },
}
TOOL_REQUIRED = {
    "read_file": {"relative_path"},
    "list_dir": {"relative_path", "recursive"},
    "find_file": {"file_mask", "relative_path"},
    "search_for_pattern": {"substring_pattern"},
    "create_text_file": {"relative_path", "content"},
    "replace_content": {"relative_path", "needle", "repl", "mode"},
}


def result_text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


class MultiProjectRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_routes_each_call_and_contains_paths(self):
        with tempfile.TemporaryDirectory(prefix="chatgpt-mcp-router-test-") as folder:
            root = Path(folder)
            alpha = root / "alpha"
            beta = root / "beta"
            alpha.mkdir()
            beta.mkdir()

            alpha.joinpath("same.txt").write_text("alpha-sentinel\n", encoding="utf-8")
            beta.joinpath("same.txt").write_text("beta-sentinel\n", encoding="utf-8")
            alpha.joinpath("edit.txt").write_text("alpha version 1\n", encoding="utf-8")
            beta.joinpath("edit.txt").write_text("beta must stay unchanged\n", encoding="utf-8")
            alpha.joinpath("beta-link.txt").symlink_to(beta / "same.txt")
            alpha.joinpath(".env").write_text("alpha-env-secret\n", encoding="utf-8")
            alpha.joinpath("env-alias").symlink_to(alpha / ".env")

            config_dir = root / "config"
            config_dir.mkdir()
            config = config_dir / "projects.json"
            config.write_text(json.dumps({
                "projects": {"alpha": str(alpha), "beta": str(beta)},
            }), encoding="utf-8")

            env = dict(os.environ, XDG_STATE_HOME=str(root / "state"))
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(SERVER), "--config", str(config)],
                env=env,
            )
            with (root / "server.log").open("w+") as log:
                try:
                    async with asyncio.timeout(150):
                        async with stdio_client(params, errlog=log) as (read, write):
                            async with ClientSession(read, write) as client:
                                initialized = await client.initialize()
                                self.assertEqual(initialized.serverInfo.version, "1.5.0")
                                self.assertIsNotNone(initialized.capabilities.resources)
                                self.assertEqual(initialized.capabilities.experimental["openai/settings"], {
                                    "readTool": "read_plugin_settings",
                                    "updateTool": "update_plugin_settings",
                                })

                                tools = await client.list_tools()
                                self.assertEqual({tool.name for tool in tools.tools}, TOOLS)
                                tools_by_name = {tool.name: tool for tool in tools.tools}
                                observer = tools_by_name['report_work_events']
                                self.assertFalse(observer.annotations.readOnlyHint)
                                self.assertFalse(observer.annotations.destructiveHint)
                                self.assertEqual(observer.inputSchema['properties']['project']['enum'], ['alpha', 'beta'])
                                self.assertEqual(observer.inputSchema['properties']['events']['maxItems'], 20)
                                event = {'schema_version': 1, 'source_id': 'figma-cloud', 'source_kind': 'mcp',
                                         'actor_id': 'figma', 'actor_kind': 'mcp', 'actor_label': 'Figma',
                                         'task_id': 'fetch-1', 'task_kind': 'call', 'task_label': '프레임 조회',
                                         'action': 'read', 'state': 'started'}
                                reported = await client.call_tool('report_work_events', {'project': 'alpha', 'events': [event]})
                                self.assertFalse(reported.isError, result_text(reported))
                                self.assertEqual(reported.structuredContent,
                                                 {'project': 'alpha', 'accepted': 1, 'provenance': 'reported'})
                                invalid = await client.call_tool('report_work_events', {'project': 'alpha',
                                                                                       'events': [{**event, 'actor_id': '../bad'}]})
                                self.assertTrue(invalid.isError)
                                records = root / 'state/chatgpt-coding-mcp/work-events.jsonl'
                                saved = [json.loads(line) for line in records.read_text().splitlines()]
                                self.assertEqual(len(saved), 1)
                                self.assertEqual(saved[0]['provenance'], 'reported')
                                self.assertIn('expires_at', saved[0])
                                for name in EXECUTION_TOOLS | GIT_TOOLS:
                                    descriptor = tools_by_name[name]
                                    self.assertEqual(descriptor.meta["ui"]["visibility"], ["model"])
                                    self.assertFalse(descriptor.meta["openai/widgetAccessible"])
                                    self.assertNotIn("openai/outputTemplate", descriptor.meta)
                                    self.assertEqual(descriptor.inputSchema["properties"]["project"]["enum"],
                                                     ["alpha", "beta"])
                                    self.assertIn("project", descriptor.inputSchema["required"])
                                self.assertFalse(tools_by_name["start_command"].annotations.readOnlyHint)
                                self.assertTrue(tools_by_name["get_job"].annotations.readOnlyHint)
                                self.assertTrue(
                                    tools_by_name["read_project_memory"].annotations.readOnlyHint
                                )
                                self.assertFalse(
                                    tools_by_name["update_project_memory"].annotations.readOnlyHint
                                )
                                self.assertEqual(
                                    tools_by_name["read_project_memory"]
                                    .inputSchema["properties"]["project"]["enum"],
                                    ["alpha", "beta"],
                                )
                                memory = await client.call_tool(
                                    "read_project_memory", {"project": "alpha"}
                                )
                                self.assertFalse(memory.isError, result_text(memory))
                                self.assertEqual(memory.structuredContent, {
                                    "project": "alpha", "content": "", "revision": 0,
                                    "updated_at": None,
                                })
                                saved_memory = await client.call_tool(
                                    "update_project_memory", {
                                        "project": "alpha", "content": "verified handoff",
                                        "expected_revision": 0,
                                    },
                                )
                                self.assertFalse(saved_memory.isError, result_text(saved_memory))
                                self.assertEqual(saved_memory.structuredContent["revision"], 1)
                                stale_memory = await client.call_tool(
                                    "update_project_memory", {
                                        "project": "alpha", "content": "stale overwrite",
                                        "expected_revision": 0,
                                    },
                                )
                                self.assertTrue(stale_memory.isError, result_text(stale_memory))
                                reread_memory = await client.call_tool(
                                    "read_project_memory", {"project": "alpha"}
                                )
                                self.assertEqual(
                                    reread_memory.structuredContent["content"], "verified handoff"
                                )
                                self.assertTrue(
                                    tools_by_name["read_project_progress"].annotations.readOnlyHint
                                )
                                self.assertFalse(
                                    tools_by_name["update_project_progress"].annotations.readOnlyHint
                                )
                                progress_input = tools_by_name[
                                    "update_project_progress"
                                ].inputSchema["properties"]
                                self.assertEqual(
                                    {key: progress_input["title"][key]
                                     for key in ("minLength", "maxLength")},
                                    {"minLength": 1, "maxLength": 200},
                                )
                                self.assertEqual(progress_input["summary"]["maxLength"], 4096)
                                for field in ("completed", "next_steps"):
                                    self.assertEqual(progress_input[field]["maxItems"], 20)
                                    self.assertEqual(
                                        progress_input[field]["items"]["maxLength"], 1000
                                    )
                                self.assertEqual(
                                    tools_by_name["read_project_progress"]
                                    .inputSchema["properties"]["project"]["enum"],
                                    ["alpha", "beta"],
                                )
                                progress = await client.call_tool(
                                    "read_project_progress", {"project": "alpha"}
                                )
                                self.assertFalse(progress.isError, result_text(progress))
                                self.assertEqual(progress.structuredContent, {
                                    "project": "alpha", "title": "", "status": "not_recorded",
                                    "summary": "", "completed": [], "next_steps": [],
                                    "revision": 0, "updated_at": None,
                                })
                                saved_progress = await client.call_tool(
                                    "update_project_progress", {
                                        "project": "alpha",
                                        "title": "Router integration",
                                        "status": "in_progress",
                                        "summary": "Observed project state tests passing.",
                                        "completed": ["Project state tests"],
                                        "next_steps": ["Complete router integration"],
                                        "expected_revision": 0,
                                    },
                                )
                                self.assertFalse(
                                    saved_progress.isError, result_text(saved_progress)
                                )
                                self.assertEqual(saved_progress.structuredContent["revision"], 1)
                                stale_progress = await client.call_tool(
                                    "update_project_progress", {
                                        "project": "alpha", "title": "Stale report",
                                        "status": "blocked", "summary": "",
                                        "completed": [], "next_steps": [],
                                        "expected_revision": 0,
                                    },
                                )
                                self.assertTrue(stale_progress.isError, result_text(stale_progress))
                                reread_progress = await client.call_tool(
                                    "read_project_progress", {"project": "alpha"}
                                )
                                self.assertEqual(
                                    reread_progress.structuredContent["title"], "Router integration"
                                )
                                profiles = await client.call_tool("list_execution_profiles", {"project": "beta"})
                                self.assertFalse(profiles.isError, result_text(profiles))
                                self.assertEqual(profiles.structuredContent["profiles"], [])
                                command_args = {"project": "beta", "request_id": "router-command",
                                                "argv": ["bash", "-c", "printf router-ok; sleep 1"]}
                                started = await client.call_tool("start_command", command_args)
                                self.assertFalse(started.isError, result_text(started))
                                job_id = started.structuredContent["job_id"]
                                duplicate = await client.call_tool("start_command", command_args)
                                self.assertEqual(duplicate.structuredContent["job_id"], job_id)
                                busy = await client.call_tool("create_text_file", {
                                    "project": "beta", "relative_path": "blocked.txt", "content": "blocked"})
                                self.assertTrue(busy.isError)
                                self.assertIn("PROJECT_BUSY", result_text(busy))
                                for _ in range(40):
                                    status = await client.call_tool("get_job", {"project": "beta", "job_id": job_id})
                                    if status.structuredContent["state"] not in {"queued", "running"}:
                                        break
                                    await asyncio.sleep(.1)
                                self.assertEqual(status.structuredContent["state"], "succeeded", result_text(status))
                                output = await client.call_tool("read_job_log", {"project": "beta", "job_id": job_id})
                                self.assertEqual(output.structuredContent["text"], "router-ok")
                                inaccessible = await client.call_tool("get_job", {"project": "alpha", "job_id": job_id})
                                self.assertTrue(inaccessible.isError)
                                history = await client.call_tool("list_jobs", {"project": "beta"})
                                self.assertEqual(len(history.structuredContent["jobs"]), 1)
                                for name in SETTINGS_TOOLS:
                                    self.assertTrue(tools_by_name[name].annotations.readOnlyHint)
                                    self.assertEqual(tools_by_name[name].meta["ui"]["visibility"], ["app"])
                                original_config = config.read_bytes()
                                settings = await client.call_tool("read_plugin_settings", {})
                                self.assertFalse(settings.isError)
                                properties = settings.structuredContent["schema"]["properties"]
                                self.assertEqual(properties["connection_status"]["enum"], ["connected"])
                                fixed_values = {"connection_status": "connected"}
                                self.assertEqual(settings.structuredContent["values"], fixed_values)
                                layout = settings.structuredContent["layout"][0]["items"]
                                self.assertEqual(layout[0], {
                                    "kind": "property", "property": "connection_status",
                                })
                                action = layout[1]
                                self.assertEqual(action["kind"], "tool")
                                self.assertEqual(action["tool"], "check_settings_support")
                                probe = await client.call_tool(action["tool"], {})
                                self.assertFalse(probe.isError)
                                self.assertIn("2개", result_text(probe))
                                update = await client.call_tool("update_plugin_settings", {"set": {}})
                                self.assertFalse(update.isError)
                                self.assertEqual(update.structuredContent, {"values": fixed_values})
                                fixed_update = await client.call_tool("update_plugin_settings", {
                                    "set": fixed_values,
                                })
                                self.assertFalse(fixed_update.isError)
                                self.assertEqual(fixed_update.structuredContent, {"values": fixed_values})
                                changed_status = await client.call_tool("update_plugin_settings", {
                                    "set": {"connection_status": "disconnected"},
                                })
                                self.assertTrue(changed_status.isError)
                                invalid_update = await client.call_tool("update_plugin_settings", {
                                    "set": {"project": "beta"},
                                })
                                self.assertTrue(invalid_update.isError)
                                self.assertEqual(config.read_bytes(), original_config)
                                list_projects_tool = tools_by_name["list_projects"]
                                self.assertEqual(
                                    list_projects_tool.meta["ui"]["visibility"],
                                    ["model", "app"],
                                )
                                self.assertTrue(
                                    list_projects_tool.meta["openai/widgetAccessible"]
                                )
                                self.assertNotIn(
                                    "resourceUri", list_projects_tool.meta["ui"]
                                )
                                self.assertNotIn(
                                    "openai/outputTemplate", list_projects_tool.meta
                                )
                                self.assertIn("projects", list_projects_tool.outputSchema["required"])
                                self.assertEqual(
                                    list_projects_tool.outputSchema["properties"]["projects"]["type"],
                                    "array",
                                )

                                picker_tool = tools_by_name["open_project_picker"]
                                self.assertTrue(picker_tool.annotations.readOnlyHint)
                                self.assertEqual(
                                    picker_tool.meta["ui"]["visibility"], ["model"]
                                )
                                self.assertFalse(
                                    picker_tool.meta["openai/widgetAccessible"]
                                )
                                self.assertEqual(
                                    picker_tool.meta["ui"]["resourceUri"], UI_URI
                                )
                                self.assertEqual(
                                    picker_tool.meta["openai/outputTemplate"], UI_URI
                                )
                                self.assertTrue(picker_tool.icons)
                                self.assertTrue(
                                    picker_tool.icons[0].src.startswith("data:image/svg+xml")
                                )
                                self.assertEqual(
                                    picker_tool.outputSchema, list_projects_tool.outputSchema
                                )

                                select_tool = tools_by_name["select_project"]
                                self.assertTrue(select_tool.annotations.readOnlyHint)
                                self.assertEqual(select_tool.meta["ui"]["visibility"], ["app"])
                                self.assertEqual(
                                    set(select_tool.inputSchema["properties"]["project"]["enum"]),
                                    {"alpha", "beta"},
                                )
                                aliases = ["alpha", "beta"]
                                for tool in tools.tools:
                                    if tool.name not in PROJECT_TOOLS:
                                        continue
                                    self.assertEqual(
                                        tool.meta["ui"]["visibility"], ["model"], tool.name,
                                    )
                                    self.assertFalse(
                                        tool.meta["openai/widgetAccessible"], tool.name,
                                    )
                                    self.assertTrue(tool.title, tool.name)
                                    for status in ("invoking", "invoked"):
                                        text = tool.meta[f"openai/toolInvocation/{status}"]
                                        self.assertIsInstance(text, str, tool.name)
                                        self.assertTrue(0 < len(text) <= 64, tool.name)
                                    schema = tool.inputSchema
                                    self.assertEqual(
                                        set(schema["properties"]),
                                        TOOL_PROPERTIES[tool.name] | {"project"},
                                        tool.name,
                                    )
                                    self.assertEqual(
                                        set(schema.get("required", [])),
                                        TOOL_REQUIRED[tool.name] | {"project"},
                                        tool.name,
                                    )
                                    project_schema = schema["properties"]["project"]
                                    self.assertEqual(project_schema.get("type"), "string", tool.name)
                                    self.assertEqual(
                                        set(project_schema.get("enum", [])),
                                        set(aliases),
                                        tool.name,
                                    )

                                resources = await client.list_resources()
                                self.assertEqual(len(resources.resources), 1)
                                resource = resources.resources[0]
                                self.assertEqual(str(resource.uri), UI_URI)
                                self.assertEqual(resource.mimeType, UI_MIME)

                                resource_result = await client.read_resource(UI_URI)
                                self.assertEqual(len(resource_result.contents), 1)
                                content = resource_result.contents[0]
                                self.assertEqual(str(content.uri), UI_URI)
                                self.assertEqual(content.mimeType, UI_MIME)
                                self.assertIn("<html", content.text.lower())
                                self.assertEqual(content.meta["ui"]["csp"], {
                                    "connectDomains": [], "resourceDomains": [],
                                })
                                self.assertEqual(content.meta["openai/ui"]["availableDisplayModes"], ["inline"])

                                with self.assertRaises(McpError):
                                    await client.read_resource(
                                        "ui://host-repositories/unknown.html"
                                    )

                                projects = await client.call_tool("list_projects", {})
                                self.assertFalse(projects.isError, result_text(projects))
                                expected_projects = [
                                    {"project": "alpha", "path": str(alpha)},
                                    {"project": "beta", "path": str(beta)},
                                ]
                                self.assertEqual(
                                    projects.structuredContent,
                                    {"projects": expected_projects},
                                )
                                projects_text = result_text(projects)
                                for alias, path in (("alpha", alpha), ("beta", beta)):
                                    self.assertIn(alias, projects_text)
                                    self.assertIn(str(path), projects_text)

                                picker = await client.call_tool("open_project_picker", {})
                                self.assertFalse(picker.isError, result_text(picker))
                                self.assertEqual(picker.structuredContent, projects.structuredContent)
                                self.assertEqual(result_text(picker), projects_text)

                                selected = await client.call_tool(
                                    "select_project", {"project": "alpha"}
                                )
                                self.assertFalse(selected.isError, result_text(selected))
                                self.assertEqual(selected.structuredContent, {
                                    "projects": expected_projects,
                                    "selected_project": "alpha",
                                })
                                repeated_projects = await client.call_tool("list_projects", {})
                                self.assertFalse(
                                    repeated_projects.isError, result_text(repeated_projects)
                                )
                                self.assertEqual(
                                    repeated_projects.structuredContent,
                                    {"projects": expected_projects},
                                )
                                self.assertNotIn(
                                    "selected_project", repeated_projects.structuredContent
                                )
                                result = await client.call_tool("read_file", {
                                    "project": "beta", "relative_path": "same.txt",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertIn("beta-sentinel", result_text(result))

                                selected = await client.call_tool(
                                    "select_project", {"project": "beta"}
                                )
                                self.assertFalse(selected.isError, result_text(selected))
                                self.assertEqual(selected.structuredContent, {
                                    "projects": expected_projects,
                                    "selected_project": "beta",
                                })
                                result = await client.call_tool("read_file", {
                                    "project": "alpha", "relative_path": "same.txt",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertIn("alpha-sentinel", result_text(result))

                                selected = await client.call_tool(
                                    "select_project", {"project": "unknown"}
                                )
                                self.assertTrue(selected.isError, result_text(selected))

                                for project, sentinel in (
                                    ("alpha", "alpha-sentinel"),
                                    ("beta", "beta-sentinel"),
                                    ("alpha", "alpha-sentinel"),
                                    ("beta", "beta-sentinel"),
                                ):
                                    result = await client.call_tool("read_file", {
                                        "project": project,
                                        "relative_path": "same.txt",
                                    })
                                    self.assertFalse(result.isError, result_text(result))
                                    self.assertIn(sentinel, result_text(result))

                                concurrent = await asyncio.gather(*(
                                    client.call_tool("read_file", {
                                        "project": project,
                                        "relative_path": "same.txt",
                                    })
                                    for project in ("alpha", "beta", "beta", "alpha")
                                ))
                                for result, sentinel in zip(
                                    concurrent,
                                    ("alpha-sentinel", "beta-sentinel",
                                     "beta-sentinel", "alpha-sentinel"),
                                    strict=True,
                                ):
                                    self.assertFalse(result.isError, result_text(result))
                                    self.assertIn(sentinel, result_text(result))

                                result = await client.call_tool("create_text_file", {
                                    "project": "alpha",
                                    "relative_path": "created.txt",
                                    "content": "created only in alpha\n",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertEqual(
                                    alpha.joinpath("created.txt").read_text(encoding="utf-8"),
                                    "created only in alpha\n",
                                )
                                self.assertFalse(beta.joinpath("created.txt").exists())

                                result = await client.call_tool("replace_content", {
                                    "project": "alpha",
                                    "relative_path": "edit.txt",
                                    "needle": "version 1",
                                    "repl": "version 2",
                                    "mode": "literal",
                                })
                                self.assertFalse(result.isError, result_text(result))
                                self.assertEqual(
                                    alpha.joinpath("edit.txt").read_text(encoding="utf-8"),
                                    "alpha version 2\n",
                                )
                                self.assertEqual(
                                    beta.joinpath("edit.txt").read_text(encoding="utf-8"),
                                    "beta must stay unchanged\n",
                                )

                                for arguments in (
                                    {"relative_path": "same.txt"},
                                    {"project": "unknown", "relative_path": "same.txt"},
                                    {"project": "alpha", "relative_path": "../beta/same.txt"},
                                    {"project": "alpha", "relative_path": "beta-link.txt"},
                                    {"project": "alpha", "relative_path": "env-alias"},
                                ):
                                    result = await client.call_tool("read_file", arguments)
                                    self.assertTrue(result.isError, result_text(result))
                                    self.assertNotIn("beta-sentinel", result_text(result))
                                    self.assertNotIn("alpha-env-secret", result_text(result))

                                result = await client.call_tool("search_for_pattern", {
                                    "project": "alpha",
                                    "relative_path": ".",
                                    "substring_pattern": "beta-sentinel",
                                })
                                self.assertTrue(result.isError, result_text(result))
                                self.assertNotIn("beta-sentinel", result_text(result))

                                activity_path = (
                                    root / "state" / "chatgpt-coding-mcp" / "activity.jsonl"
                                )
                                activity_events = [
                                    json.loads(line) for line in
                                    activity_path.read_text(encoding="utf-8").splitlines()
                                ]
                                self.assertTrue(any(
                                    event["tool"] == "read_project_memory"
                                    and event["project"] == "alpha"
                                    and event["phase"] == "completed"
                                    for event in activity_events
                                ))
                                self.assertTrue(all(
                                    set(event) == {
                                        "project", "tool", "phase", "call_id", "time",
                                    }
                                    for event in activity_events
                                ))
                                before_unknown = activity_path.read_bytes()
                                unknown = await client.call_tool("unknown_activity_text", {})
                                self.assertTrue(unknown.isError, result_text(unknown))
                                self.assertEqual(activity_path.read_bytes(), before_unknown)

                                # State telemetry is best effort. A broken activity target must
                                # not turn an already valid read into an MCP operation failure.
                                activity_path.unlink()
                                outside_activity = root / "outside-activity"
                                outside_activity.write_text("do not touch", encoding="utf-8")
                                activity_path.symlink_to(outside_activity)
                                memory_with_broken_activity = await client.call_tool(
                                    "read_project_memory", {"project": "alpha"}
                                )
                                self.assertFalse(
                                    memory_with_broken_activity.isError,
                                    result_text(memory_with_broken_activity),
                                )
                                self.assertEqual(
                                    memory_with_broken_activity.structuredContent["revision"], 1
                                )
                                self.assertEqual(
                                    outside_activity.read_text(encoding="utf-8"), "do not touch"
                                )

                    self.assertFalse(alpha.joinpath(".serena").exists())
                    self.assertFalse(beta.joinpath(".serena").exists())
                except BaseException:
                    log.seek(0)
                    print(log.read()[-12000:], file=sys.stderr)
                    raise


if __name__ == "__main__":
    unittest.main()
