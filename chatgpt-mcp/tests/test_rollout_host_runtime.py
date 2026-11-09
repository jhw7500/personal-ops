"""Host-only rollout safety checks without invoking tunnel client or installer."""
import importlib.util
from contextlib import contextmanager
from functools import partial
import io
import json
import os
from pathlib import Path
import site
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "rollout-host-runtime.py"
spec = importlib.util.spec_from_file_location("rollout_host_runtime", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

OLD_LIVE = {"snapshot": "a" * 20, "tunnel_pid": 101, "tunnel_start_ticks": 10,
            "mcp_pid": 102, "mcp_start_ticks": 11,
            "server": "/fixture/old/server.py", "interpreter": "/fixture/python"}


class TTY(io.StringIO):
    def isatty(self):
        return True


class RolloutTest(unittest.TestCase):
    def test_schema015016_source_and_regressions_are_delivered(self):
        installer = module._import_installer()
        self.assertIn("chatgpt-mcp/host_schema015016.py", module.inventory(installer))
        for name in ("test_host_schema015016_status.py",
                     "test_host_schema015016_migration.py"):
            with self.subTest(name=name):
                self.assertIn("tests/" + name, module.control_inventory())

    def setUp(self):
        from rollout_layout import known_layout
        self.layout = known_layout("legacy")
        target = patch.object(module._rollout_target, "resolve_rollout_target",
                              return_value=self.layout)
        target.start()
        self.addCleanup(target.stop)
        self.authority_preflight_data = {
            "version": 1, "layout": "legacy", "live_snapshot": OLD_LIVE["snapshot"],
            "authority_active": False,
        }
        authority = patch.object(module._rollout_authority,
                                 "preflight_rollout_authority",
                                 return_value=self.authority_preflight_data)
        self.authority_preflight = authority.start()
        self.addCleanup(authority.stop)
        admission = patch.object(module._rollout_authority, "admit_rollout_release", return_value={
            "status": "not_required", "authority_active": False,
            "release_digest": "c" * 64, "release_abi": 3,
        })
        self.release_admission = admission.start()
        self.addCleanup(admission.stop)

    def test_approval_browser_fixture_is_pinned_with_preflight_inputs(self):
        for path in ("tests/check_host_access_approval_ui.py",
                     "tests/check_schema014_approval_ui.py",
                     "tests/check_host_command_approval_ui.py",
                     "tests/check_development_approval_ui.py",
                     "tests/test_runtime_rollout_ui.py",
                     "tests/test_workspace_approval_browser.py",
                     "tests/test_git_commit_access_browser.py",
                     "tests/test_workspace_approval_ui.mjs",
                     "rollout_test_runner.py"):
            with self.subTest(path=path):
                self.assertIn(path, module.control_inventory())

    def test_approval_browser_failure_blocks_rollout_checks(self):
        import subprocess
        for stage, filename in (("checks_approval_ui", "check_host_access_approval_ui.py"),
                                ("checks_schema014_ui", "check_schema014_approval_ui.py"),
                                ("checks_host_command_ui", "check_host_command_approval_ui.py"),
                                ("checks_development_approval_ui", "check_development_approval_ui.py"),
                                ("checks_runtime_rollout_ui", "test_runtime_rollout_ui.py"),
                                ("checks_workspace_approval_browser", "test_workspace_approval_browser.py"),
                                ("checks_git_commit_access_browser", "test_git_commit_access_browser.py"),
                                ("checks_workspace_ui", "test_workspace_approval_ui.mjs"),
                                ("checks_targeted", "rollout_test_runner.py"),
                                ("checks_full", "rollout_test_runner.py")):
            with self.subTest(stage=stage):
                def checks(argv, **options):
                    return subprocess.CompletedProcess(argv, 1 if options["stage"] == stage else 0)
                with patch.object(module, "run_command", side_effect=checks) as run, \
                     patch.object(module, "runtime_node", return_value="/fixture/node"):
                    with self.assertRaisesRegex(RuntimeError, "RUNTIME_TESTS_FAILED"):
                        module.run_checks()
                browser = next(call for call in run.call_args_list
                               if call.kwargs["stage"] == stage)
                self.assertIn(filename, " ".join(browser.args[0]))

    def test_official_stages_use_exact_filter_private_home_and_private_xdg(self):
        import subprocess
        calls = []
        owner_home = Path.home()
        owner_userbase = "/owner/userbase"
        ambient = {
            "HOME": str(owner_home),
            "PYTHONUSERBASE": "/ambient/userbase",
            "XDG_CONFIG_HOME": "/ambient/config",
            "XDG_STATE_HOME": "/ambient/state",
            "XDG_CACHE_HOME": "/ambient/cache",
        }

        def checks(argv, **options):
            assignments = dict(item.split("=", 1) for item in argv[1:10])
            home = Path(assignments["HOME"])
            xdg = [Path(assignments[name]) for name in ambient if name.startswith("XDG_")]
            private = home.parent
            self.assertTrue(private.name.startswith("runtime-preflight-"))
            self.assertEqual(home.name, "home")
            self.assertNotEqual(home, owner_home)
            self.assertEqual([path.parent for path in xdg], [private] * 3)
            self.assertEqual([path.name for path in xdg], ["config", "state", "cache"])
            self.assertEqual(len(set(xdg)), 3)
            for path in (private, home, *xdg):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(
                str(path) != ambient[name]
                for path, name in zip(xdg, ("XDG_CONFIG_HOME", "XDG_STATE_HOME",
                                           "XDG_CACHE_HOME"))
            ))
            self.assertEqual(
                assignments["PLAYWRIGHT_BROWSERS_PATH"],
                str(owner_home / ".cache/ms-playwright"),
            )
            self.assertEqual(assignments["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(assignments["PYTHONUSERBASE"], owner_userbase)
            self.assertEqual(assignments["UV_OFFLINE"], "1")
            self.assertEqual(
                assignments["PATH"],
                "/fixture:" + str(owner_home / ".local/bin")
                + ":/usr/local/bin:/usr/bin:/bin",
            )
            self.assertEqual(os.environ["HOME"], str(owner_home))
            self.assertEqual(os.environ["PYTHONUSERBASE"], "/ambient/userbase")
            calls.append((argv, options))
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, ambient), \
             patch.object(module, "run_command", side_effect=checks), \
             patch.object(module, "runtime_interpreter", return_value="/fixture/python"), \
             patch.object(module, "runtime_node", return_value="/fixture/node"), \
             patch.object(site, "getuserbase", return_value=owner_userbase), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            module.run_checks()

        stages = {options["stage"]: argv for argv, options in calls}
        for stage in ("checks_targeted", "checks_full"):
            argv = stages[stage]
            self.assertIn(str(module.HERE / "rollout_test_runner.py"), argv)
            self.assertIn("HOME=" , argv[1])
            self.assertIn("XDG_CONFIG_HOME=" , argv[2])
            self.assertIn("XDG_STATE_HOME=" , argv[3])
            self.assertIn("XDG_CACHE_HOME=" , argv[4])
            self.assertIn("PLAYWRIGHT_BROWSERS_PATH=" , argv[5])
            self.assertEqual(argv[6], "PYTHONDONTWRITEBYTECODE=1")
            self.assertEqual(argv[7], "PYTHONUSERBASE=" + owner_userbase)
        self.assertIn("--modules", stages["checks_targeted"])
        self.assertIn("tests.test_execution", stages["checks_targeted"])
        self.assertIn("--discover", stages["checks_full"])
        self.assertEqual(stages["checks_workspace_ui"][10:12], ["/fixture/node", "--test"])
        node_inputs = stages["checks_workspace_ui"][12:]
        self.assertEqual(Path(node_inputs[0]).name, "test_workspace_approval_ui.mjs")
        self.assertEqual(sum(Path(path).name == "test_workspace_approval_ui.mjs"
                             for path in node_inputs), 1)
        diagnostics = output.getvalue()
        self.assertIn("excluded unittest IDs (5)", diagnostics)
        for method in module.EXCLUDED_TEST_METHODS:
            self.assertIn(method, diagnostics)

    def test_checks_link_private_serena_path_to_approved_owner_venv_and_clean_up(self):
        import subprocess
        private_homes = []

        with tempfile.TemporaryDirectory() as temporary:
            owner_home = Path(temporary) / "owner"
            approved = owner_home / ".local/share/uv/tools/serena-agent"
            interpreter = approved / "bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("fixture\n")
            interpreter.chmod(0o700)
            owner_userbase = str(owner_home / ".local")

            def checks(argv, **_options):
                assignments = dict(item.split("=", 1) for item in argv[1:10])
                home = Path(assignments["HOME"])
                linked = home / ".local/share/uv/tools/serena-agent"
                self.assertTrue(linked.is_symlink())
                self.assertEqual(linked.readlink(), approved)
                self.assertEqual(linked.resolve(), approved.resolve())
                self.assertEqual(assignments["PYTHONDONTWRITEBYTECODE"], "1")
                self.assertEqual(assignments["PYTHONUSERBASE"], owner_userbase)
                self.assertEqual(assignments["UV_OFFLINE"], "1")
                self.assertIn(str(owner_home / ".local/bin"), assignments["PATH"])
                private_homes.append(home)
                return subprocess.CompletedProcess(argv, 0)

            with patch.object(module.Path, "home", return_value=owner_home), \
                 patch.object(module, "runtime_node", return_value="/fixture/node"), \
                 patch.object(module, "run_command", side_effect=checks), \
                 patch.object(site, "getuserbase", return_value=owner_userbase), \
                 patch("sys.stdout", new_callable=io.StringIO):
                module.run_checks()

            self.assertEqual(len(private_homes), 11)
            self.assertTrue(all(not home.exists() for home in private_homes))

    def test_checks_fail_closed_when_pinned_node_is_unavailable(self):
        import subprocess

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(module.Path, "home", return_value=Path(temporary)), \
             patch.object(module.shutil, "which", return_value="/usr/bin/node") as which, \
             patch.object(module, "run_command", return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_NODE_UNAVAILABLE"):
                module.run_checks()

        which.assert_not_called()
        run.assert_not_called()

    def test_runtime_node_accepts_only_the_executable_pinned_version(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(module.Path, "home", return_value=Path(temporary)):
            node = Path(temporary) / ".nvm/versions/node/v24.12.0/bin/node"
            node.parent.mkdir(parents=True)
            node.write_text("fixture\n")
            node.chmod(0o700)
            self.assertEqual(module.runtime_node(), str(node))
            node.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_NODE_UNAVAILABLE"):
                module.runtime_node()

    def test_workspace_node_gate_fails_closed_when_required_input_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            tests = Path(temporary) / "tests"
            tests.mkdir()
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_TEST_INPUT_MISSING"):
                module.node_ui_test_inputs(tests)
            optional = tests / "test_optional_ui.mjs"
            optional.write_text("// optional fixture\n")
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_TEST_INPUT_MISSING"):
                module.node_ui_test_inputs(tests)
            required = tests / "test_workspace_approval_ui.mjs"
            required.write_text("// required fixture\n")
            self.assertEqual(
                tuple(path.name for path in module.node_ui_test_inputs(tests)),
                ("test_workspace_approval_ui.mjs", "test_optional_ui.mjs"),
            )

    def test_workspace_node_gate_rejects_symlink_and_nonregular_required_input(self):
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                tests = Path(temporary) / "tests"
                tests.mkdir()
                required = tests / "test_workspace_approval_ui.mjs"
                if kind == "symlink":
                    target = tests / "target.mjs"
                    target.write_text("// fixture\n")
                    required.symlink_to(target)
                else:
                    required.mkdir()
                with self.assertRaisesRegex(RuntimeError, "RUNTIME_TEST_INPUT_UNSAFE"):
                    module.node_ui_test_inputs(tests)

    def test_exact_filter_excludes_both_module_prefixes_and_propagates_other_failure(self):
        runner_path = module.HERE / "rollout_test_runner.py"
        runner_spec = importlib.util.spec_from_file_location("rollout_test_runner", runner_path)
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)
        executed = []

        class Synthetic(unittest.TestCase):
            def __init__(self, test_id, *, fails=False):
                super().__init__("runTest")
                self.test_id = test_id
                self.fails = fails

            def id(self):
                return self.test_id

            def runTest(self):
                executed.append(self.test_id)
                if self.fails:
                    self.fail("synthetic unrelated failure")

        forbidden = []
        cases = []
        for prefix in ("test_execution", "tests.test_execution"):
            for method in module.EXCLUDED_TEST_METHODS:
                test_id = f"{prefix}.ExecutionPolicyTest.{method}"
                forbidden.append(test_id)
                cases.append(Synthetic(test_id))
        passing_id = "tests.test_other.OtherTest.test_passes"
        failing_id = "tests.test_other.OtherTest.test_fails"
        near_miss_ids = [
            f"tests.test_other.ExecutionPolicyTest.{module.EXCLUDED_TEST_METHODS[0]}",
            f"tests.test_execution.OtherTest.{module.EXCLUDED_TEST_METHODS[0]}",
        ]
        cases.extend(Synthetic(test_id) for test_id in near_miss_ids)
        cases.extend((Synthetic(passing_id), Synthetic(failing_id, fails=True)))
        stream = io.StringIO()
        result = runner.run_suite(unittest.TestSuite(cases), stream=stream)

        self.assertEqual(executed, [*near_miss_ids, passing_id, failing_id])
        self.assertFalse(result.wasSuccessful())
        self.assertIn("excluded unittest IDs (10)", stream.getvalue())
        for test_id in forbidden:
            self.assertIn(test_id, stream.getvalue())

        executed.clear()
        boundary = runner.run_suite(
            unittest.TestSuite([Synthetic(passing_id)]),
            stream=io.StringIO(),
            required_excluded_count=5,
        )
        self.assertEqual(executed, [passing_id])
        self.assertTrue(boundary.wasSuccessful())
        self.assertFalse(boundary.rollout_exclusion_count_ok)

    def test_runner_main_enforces_success_count_and_unrelated_failures(self):
        runner_path = module.HERE / "rollout_test_runner.py"
        runner_spec = importlib.util.spec_from_file_location("rollout_test_runner_main", runner_path)
        runner = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(runner)

        class Synthetic(unittest.TestCase):
            def __init__(self, test_id, *, fails=False):
                super().__init__("runTest")
                self.test_id = test_id
                self.fails = fails

            def id(self):
                return self.test_id

            def runTest(self):
                if self.fails:
                    self.fail("synthetic unrelated failure")

        def suite(excluded_count=5, *, fails=False):
            cases = [Synthetic(
                f"tests.test_execution.ExecutionPolicyTest.{method}"
            ) for method in module.EXCLUDED_TEST_METHODS[:excluded_count]]
            cases.append(Synthetic("tests.test_other.OtherTest.test_result", fails=fails))
            return unittest.TestSuite(cases)

        scenarios = ((5, False, 0), (4, False, 1), (5, True, 1))
        original_run_suite = runner.run_suite
        for excluded_count, fails, expected in scenarios:
            with self.subTest(excluded_count=excluded_count, fails=fails):
                loader = types.SimpleNamespace(
                    loadTestsFromNames=lambda _names, count=excluded_count, failure=fails:
                        suite(count, fails=failure),
                    discover=lambda _path, count=excluded_count, failure=fails:
                        suite(count, fails=failure),
                )
                with patch.object(runner.unittest, "defaultTestLoader", loader), \
                     patch.object(
                         runner,
                         "run_suite",
                         side_effect=lambda loaded, **kwargs: original_run_suite(
                             loaded, stream=io.StringIO(), **kwargs
                         ),
                     ):
                    self.assertEqual(runner.main(["--modules", "synthetic"]), expected)

    def test_workspace_tools_are_expected_only_when_policy_surface_is_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = {"alpha": root / "alpha"}
            projects["alpha"].mkdir()
            policy = root / "config/workspace-policy.json"
            authority = root / "state/workspace-authority"
            legacy = module.expected_tool_names(projects, config_path=policy,
                                                authority_dir=authority)
            self.assertEqual(legacy, module.TOOL_NAMES)
            policy.parent.mkdir()
            policy.write_text("{}")
            enabled = module.expected_tool_names(projects, config_path=policy,
                                                 authority_dir=authority)
        from workspace_tools import NAMES
        self.assertEqual(
            enabled,
            module.TOOL_NAMES | NAMES | {"read_workspace_output", "publish_workspace_output"},
        )

    def test_workspace_catalog_requires_both_output_tools(self):
        from rollout_tunnel import TunnelSwitchError

        class Response(io.BytesIO):
            status = 200

        with tempfile.TemporaryDirectory() as temporary:
            url_file = Path(temporary) / "runtime.url"
            url_file.write_text("http://127.0.0.1:12345")
            url_file.chmod(0o600)
            expected = module.TOOL_NAMES | {
                "read_workspace_output", "publish_workspace_output"
            }

            def getter(names):
                body = {"details": {
                    "channel": "main", "transport": "stdio",
                    "child_state": "running", "evidence": "same_child",
                    "tools_list": {
                        "ok": True, "complete": True, "limited": False,
                        "partial": False, "tool_names": sorted(names),
                        "retained_count": len(names),
                    },
                }}
                return lambda *_args, **_kwargs: Response(json.dumps(body).encode())

            for missing in ("read_workspace_output", "publish_workspace_output"):
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    TunnelSwitchError, "LIVE_TOOLS_MISMATCH"
                ):
                    module.observe_tools(
                        expected,
                        url_file=url_file,
                        urlopen_fn=getter(expected - {missing}),
                    )
            observed = module.observe_tools(
                expected, url_file=url_file, urlopen_fn=getter(expected)
            )
        self.assertTrue(observed["tools_registered"])

    def test_rollout_blocks_activation_until_all_legacy_predecessor_and_registered_tools_are_seen(self):
        expected = {
            "get_runtime_status", "compare_deployment", "get_db_migration_status",
            "get_predecessor_migration_status", "get_schema014_migration_status",
            "get_schema015016_migration_status",
            "get_toss_test_console_status", "get_toss_ux019_test_console_status",
            "get_toss_20261026_test_console_status",
            "prepare_schema014_migration", "execute_schema014_migration",
            "prepare_schema015016_migration", "execute_schema015016_migration",
            "prepare_db_migration", "prepare_predecessor_migration",
            "execute_db_migration", "execute_predecessor_migration",
            "prepare_test_deploy", "execute_test_deploy", "get_host_operation",
            "list_host_targets", "inspect_host_target", "prepare_host_operation",
            "execute_host_operation", "read_host_operation_events",
            "list_agent_profiles", "prepare_agent_task", "start_agent_task",
            "get_agent_task", "cancel_agent_task",
            "list_host_command_profiles", "prepare_host_command",
            "execute_host_command", "get_host_command", "cancel_host_command",
            "request_host_access", "get_host_access", "start_host_execution",
            "get_host_execution", "read_host_execution_log", "cancel_host_execution",
            "prepare_runtime_rollout", "get_runtime_rollout", "execute_runtime_rollout",
            "open_runtime_rollout_approval", "confirm_runtime_rollout_approval", "cancel_runtime_rollout_approval",
            "open_host_access_approval", "update_host_access_approval",
            "confirm_host_access_approval", "cancel_host_access_approval",
            "open_schema014_approval", "confirm_schema014_approval",
            "cancel_schema014_approval",
            "open_test_plan_approval", "confirm_test_plan_approval",
            "cancel_test_plan_approval",
            "open_registered_host_approval", "confirm_registered_host_approval",
            "cancel_registered_host_approval",
            "open_git_approval", "confirm_git_approval", "cancel_git_approval",
            "open_git_commit_access_approval", "get_git_commit_access",
            "confirm_git_commit_access", "cancel_git_commit_access", "revoke_git_commit_access",
            "open_host_command_approval", "confirm_host_command_approval",
            "confirm_and_execute_host_command",
            "cancel_host_command_approval",
            "get_project_resume", "compact_project_memory",
            "open_git_operation_approval", "confirm_git_operation",
            "confirm_and_execute_git_operation", "cancel_git_operation_approval",
            "open_operation_approval", "confirm_operation_approval",
            "confirm_and_execute_operation", "cancel_operation_approval",
        }
        self.assertEqual(module.TOOL_NAMES, expected)
        self.assertIn("tests.test_toss_console_ux019", module.TESTS)
        self.assertIn("tests.test_host_predecessor_migration", module.TESTS)
        self.assertIn("tests.test_host_registered_operations", module.TESTS)
        self.assertIn("tests.test_stage1_activation_fixes", module.TESTS)
        self.assertIn("tests.test_host_simplified_approval", module.TESTS)
        self.assertIn("tests.test_host_simplified_admin", module.TESTS)
        self.assertIn("tests.test_host_config_cas", module.TESTS)
        self.assertIn("tests.test_schema014_chat_approval", module.TESTS)
        self.assertIn("tests.test_schema014_chat_router", module.TESTS)
        self.assertTrue({"tests.test_host_command_profiles",
                         "tests.test_host_command_entry",
                         "tests.test_host_command_operations",
                         "tests.test_host_command_runner",
                         "tests.test_host_command_supervisor",
                         "tests.test_host_command_tools",
                         "tests.test_server_shutdown",
                         "tests.test_admin_host_command_approval"} <= set(module.TESTS))
        self.assertTrue({
            "tests.test_agent_bootstrap",
            "tests.test_agent_probe",
            "tests.test_agent_probe_environment",
            "tests.test_agent_supervision",
            "tests.test_agent_supervised_runner",
            "tests.test_agent_host_broker",
            "tests.test_agent_proofs",
            "tests.test_agent_smoke",
            "tests.test_agent_profile_setup",
        } <= set(module.TESTS))

    def test_service_command_pins_user_bus_environment(self):
        import subprocess
        completed = subprocess.CompletedProcess([], 0, stdout=b"unit")
        with patch.object(module, "run_command", return_value=completed) as run:
            output = module.service_command(
                ["systemctl", "--user", "cat", "personal-ops-tunnel.service"],
                stdout=subprocess.PIPE,
            )
        runtime = f"/run/user/{os.getuid()}"
        self.assertEqual(output, b"unit")
        self.assertEqual(run.call_args.kwargs["env"], {
            "HOME": str(Path.home()),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus",
        })

    def test_existing_tunnel_id_is_discovered_without_terminal_arguments(self):
        self.assertEqual(module._find_id({"runtime": {"tunnel_id": "tunnel-123"}}),
                         "tunnel-123")
        self.assertIsNone(module._find_id({"runtime": {"tunnel_id": "../x"}}))


    def test_wsl_status_uses_verified_service_observer_instead_of_registry(self):
        from rollout_layout import known_layout
        layout = known_layout("wsl-pc1")
        expected = {"process_running": True, "healthy": True, "ready": True}
        command = lambda *args, **kwargs: b""
        with patch("rollout_tunnel.wsl_runtime_status", return_value=expected) as observer, \
             patch.object(module, "run_command", side_effect=AssertionError("stale registry must not run")):
            result = module.runtime_status("ignored-client", layout=layout, command_fn=command)
        self.assertIs(result, expected)
        observer.assert_called_once_with(layout, command_fn=command)

    def test_service_commands_are_bound_to_the_selected_layout(self):
        import subprocess
        from rollout_layout import known_layout
        layout = known_layout("wsl-pc1")
        with patch.object(module, "run_command", return_value=subprocess.CompletedProcess([],0,b"")) as run:
            module.service_command(["systemctl","--user","stop",layout["service"]],layout=layout)
            self.assertEqual(run.call_args.kwargs["stage"],"tunnel_stop")
            with self.assertRaisesRegex(RuntimeError,"UNEXPECTED_SERVICE_COMMAND"):
                module.service_command(["systemctl","--user","stop","personal-ops-tunnel.service"],layout=layout)
            for action in ("start", "stop", "restart"):
                with self.subTest(action=action), self.assertRaisesRegex(RuntimeError,"UNEXPECTED_SERVICE_COMMAND"):
                    service = "personal-ops-admin.service" if action == "restart" else layout["service"]
                    module.service_command(["systemctl","--user",action,service,
                                            "personal-ops-agent-foreign.service"],layout=layout)
            self.assertEqual(run.call_count,1)

    def test_wsl_rollout_uses_the_reviewed_service_profile_and_catalog(self):
        from rollout_layout import known_layout
        layout = known_layout("wsl-pc1")
        commands = []
        marker = {"reviewed_fixture": True}
        def command(argv, **kwargs):
            commands.append(argv)
            return b"fixture unit"
        def preflight(before, *, unit_fn, **kwargs):
            self.assertEqual(kwargs["layout"],layout)
            self.assertEqual(kwargs["profile_path"],layout["profile_path"])
            self.assertEqual(kwargs["override_path"],layout["override_path"])
            self.assertEqual(kwargs["client"],layout["client"])
            self.assertEqual(unit_fn(),b"fixture unit")
            return marker
        observed={"tools_registered":True,"connector_schemas_verified":True,"catalog_status":"observed","tool_count":7}
        with self.apply_fixture() as (switch,admin), \
             patch.object(module._rollout_target, "resolve_rollout_target",
                          return_value=layout), \
             patch("rollout_tunnel.preflight_tunnel",side_effect=preflight), \
             patch.object(module,"observe_tools",return_value=observed) as observe:
            result=module.rollout(apply=True,status_fn=self.healthy_status,command_fn=command)
        self.assertEqual(result["status"],"ready")
        self.assertIn(["systemctl","--user","cat",layout["service"]],commands)
        self.assertFalse(any("personal-ops-tunnel.service" in argv for argv in commands))
        self.assertIs(switch.call_args.kwargs["plan"],marker)
        self.assertEqual(switch.call_args.kwargs["client"],layout["client"])
        observe.assert_called_once_with(module.TOOL_NAMES,url_file=layout["health_url_file"])

    def test_web_legacy_instance_uses_complete_source_contract_verifier(self):
        import hashlib
        from runtime_rollout_web import WebRolloutGrant
        digest=hashlib.sha256(json.dumps({"server.py":"b"*64},sort_keys=True).encode()).hexdigest()[:20]
        grant=WebRolloutGrant("1"*32,"2"*64,digest,"3"*64,Path("/fixture/state"))
        with self.apply_fixture(), \
             patch.object(WebRolloutGrant,"verify",side_effect=AssertionError("old verifier alone is incomplete")), \
             patch.object(module._rollout_target, "verify_web_rollout_grant",
                          return_value=self.layout) as verify:
            expected_inventory = module.inventory
            expected_controls = module.control_inventory
            result=module.rollout(apply=True,approved_web=grant,status_fn=self.healthy_status,
                                  command_fn=lambda *args,**kwargs:b"")
        self.assertEqual(result["status"],"ready")
        self.assertIs(verify.call_args.args[0],grant)
        self.assertEqual(verify.call_args.args[1],module.HERE.parent)
        self.assertIs(verify.call_args.args[3],expected_inventory)
        self.assertIs(verify.call_args.args[4],expected_controls)

    def test_verified_web_layout_mismatch_blocks_install_and_switch(self):
        import hashlib
        from rollout_layout import known_layout
        from runtime_rollout_web import WebRolloutGrant
        digest=hashlib.sha256(json.dumps({"server.py":"b"*64},sort_keys=True).encode()).hexdigest()[:20]
        grant=WebRolloutGrant("1"*32,"2"*64,digest,"3"*64,Path("/fixture/state"))
        with self.apply_fixture() as (switch,admin), \
             patch.object(module._rollout_target, "verify_web_rollout_grant",
                          return_value=known_layout("wsl-pc1")):
            installer=module._import_installer()
            with patch.object(installer,"install",side_effect=AssertionError("unexpected install")) as install:
                with self.assertRaisesRegex(RuntimeError,"ROLLOUT_TARGET_CHANGED"):
                    module.rollout(apply=True,approved_web=grant,status_fn=self.healthy_status,
                                   command_fn=lambda *args,**kwargs:b"")
                install.assert_not_called()
            switch.assert_not_called()
            admin.assert_not_called()

    def test_source_layout_change_blocks_install_before_any_switch(self):
        from rollout_layout import known_layout
        with self.apply_fixture() as (switch,admin), \
             patch.object(module._rollout_target, "resolve_rollout_target",
                          side_effect=[self.layout,known_layout("wsl-pc1")]):
            installer=module._import_installer()
            with patch.object(installer,"install",side_effect=AssertionError("unexpected install")) as install:
                with self.assertRaisesRegex(RuntimeError,"ROLLOUT_TARGET_CHANGED"):
                    module.rollout(apply=True,status_fn=self.healthy_status,
                                   command_fn=lambda *args,**kwargs:b"")
                install.assert_not_called()
            switch.assert_not_called()
            admin.assert_not_called()



    def web_grant_fixture(self):
        import hashlib
        from runtime_rollout_web import WebRolloutGrant
        digest = hashlib.sha256(json.dumps({"server.py": "b" * 64}, sort_keys=True).encode()).hexdigest()[:20]
        return WebRolloutGrant("1" * 32, "2" * 64, digest, "3" * 64, Path("/fixture/state"))

    def test_preflight_reads_authority_without_admission_or_install(self):
        with self.apply_fixture() as (switch, admin):
            installer = module._import_installer()
            with patch.object(installer, "install", side_effect=AssertionError("unexpected install")) as install:
                result = module.rollout(status_fn=self.healthy_status,
                                        command_fn=lambda *args, **kwargs: b"")
        self.assertEqual(result["status"], "preflight_ok")
        self.authority_preflight.assert_called_once_with(
            self.layout, OLD_LIVE["snapshot"], source_root=module.HERE.parent)
        self.release_admission.assert_not_called()
        install.assert_not_called()
        switch.assert_not_called()
        admin.assert_not_called()

    def test_authority_preflight_failure_blocks_before_install(self):
        self.authority_preflight.side_effect = RuntimeError("ROLLOUT_AUTHORITY_CHANGED")
        with self.apply_fixture() as (switch, admin):
            installer = module._import_installer()
            with patch.object(installer, "install", side_effect=AssertionError("unexpected install")) as install:
                with self.assertRaisesRegex(RuntimeError, "ROLLOUT_AUTHORITY_CHANGED"):
                    module.rollout(apply=True, approved_web=self.web_grant_fixture(),
                                   status_fn=self.healthy_status,
                                   command_fn=lambda *args, **kwargs: b"")
                install.assert_not_called()
            switch.assert_not_called()
            admin.assert_not_called()
        self.release_admission.assert_not_called()

    def test_web_admission_between_install_and_switch_uses_frozen_preflight(self):
        effects = []
        grant = self.web_grant_fixture()
        admission = {"status": "admitted", "authority_active": True,
                     "release_digest": "c" * 64, "release_abi": 3}
        def admit(*args, **kwargs):
            effects.append("admit")
            self.assertIs(args[0], grant)
            self.assertEqual(args[1], module.HERE.parent)
            self.assertIs(kwargs["preflight"], self.authority_preflight_data)
            return admission
        self.release_admission.side_effect = admit
        with self.apply_fixture() as (switch, admin), \
             patch.object(module._rollout_target, "verify_web_rollout_grant",
                          return_value=self.layout):
            installer = module._import_installer()
            actual_install = installer.install
            activate = switch.side_effect
            def install_source(*args):
                effects.append("install")
                return actual_install(*args)
            def activate_source(*args, **kwargs):
                effects.append("switch")
                return activate(*args, **kwargs)
            switch.side_effect = activate_source
            admin.side_effect = lambda *args, **kwargs: effects.append("admin") or "admin_ready"
            with patch.object(installer, "install", side_effect=install_source):
                expected_inventory = module.inventory
                expected_controls = module.control_inventory
                result = module.rollout(apply=True, approved_web=grant,
                                        status_fn=self.healthy_status,
                                        command_fn=lambda *args, **kwargs: b"")
        self.assertEqual(effects, ["install", "admit", "switch", "admin"])
        self.assertIs(self.release_admission.call_args.args[2], installer)
        self.assertIs(self.release_admission.call_args.args[3], expected_inventory)
        self.assertIs(self.release_admission.call_args.args[4], expected_controls)
        self.assertEqual(result["status"], "ready")

    def test_failed_web_admission_keeps_services_unchanged(self):
        self.release_admission.side_effect = RuntimeError("ROLLOUT_AUTHORITY_CHANGED")
        with self.apply_fixture() as (switch, admin), \
             patch.object(module._rollout_target, "verify_web_rollout_grant",
                          return_value=self.layout):
            with self.assertRaisesRegex(RuntimeError, "ROLLOUT_AUTHORITY_CHANGED"):
                module.rollout(apply=True, approved_web=self.web_grant_fixture(),
                               status_fn=self.healthy_status,
                               command_fn=lambda *args, **kwargs: b"")
            switch.assert_not_called()
            admin.assert_not_called()

    def test_tty_approval_does_not_issue_web_release_admission(self):
        with self.apply_fixture():
            result = module.rollout(apply=True, status_fn=self.healthy_status,
                                    command_fn=lambda *args, **kwargs: b"")
        self.assertEqual(result["status"], "ready")
        self.authority_preflight.assert_called_once()
        self.release_admission.assert_not_called()


    def test_preflight_tty_and_web_results_finish_the_original_receipt_contract(self):
        from rollout_diagnostics import RolloutReceipt
        for mode in ("preflight", "tty", "web"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary, \
                 self.apply_fixture(), \
                 patch.object(module._rollout_target, "verify_web_rollout_grant",
                              return_value=self.layout):
                receipt = RolloutReceipt(temporary, mode="preflight" if mode == "preflight" else "apply")
                result = module.rollout(
                    apply=mode != "preflight",
                    approved_web=self.web_grant_fixture() if mode == "web" else None,
                    receipt=receipt, status_fn=self.healthy_status,
                    command_fn=lambda *args, **kwargs: b"")
                receipt.finish(result, 0)
                saved = json.loads(receipt.path.read_text())
                self.assertEqual(saved["exit_code"], 0)
                self.assertEqual(saved["status"], "preflight_ok" if mode == "preflight" else "ready")
                admitted = [event for event in saved["events"]
                            if event["stage"] == "release_admission_result"]
                if mode == "web":
                    self.assertEqual(len(admitted), 1)
                    self.assertEqual(admitted[0]["state"], "completed")
                    self.assertEqual(admitted[0]["status"], "not_required")
                    self.assertEqual(admitted[0]["snapshot"], result["snapshot"])
                else:
                    self.assertEqual(admitted, [])

    def test_preflight_requires_checks_and_never_installs(self):
        fake = types.SimpleNamespace()
        def must_not_install(*args):
            raise AssertionError("Install must not run in preflight")
        fake.install = must_not_install
        path = {"chatgpt-mcp/server.py": "a" * 64}
        with patch.object(module, "_import_installer", return_value=fake), \
             patch.object(module, "inventory", return_value=path), \
             patch.object(module, "control_inventory", return_value={}), \
             patch.object(module, "run_checks") as checks, \
             patch.object(module, "tunnel_client", return_value="fake-tunnel-client"), \
             patch.object(module, "_key_check") as keycheck, \
             patch("server.load_projects", return_value={"personal-ops": SCRIPT.parent.parent}), \
             patch("rollout_tunnel.freeze_live_runtime", return_value=OLD_LIVE), \
             patch("rollout_tunnel.preflight_tunnel", return_value={}), \
             patch("rollout_admin.preflight_admin"):
            status = module.rollout(status_fn=lambda _: {"tunnel_id": "tunnel-123"},
                                    command_fn=must_not_install)
        self.assertEqual(status["status"], "preflight_ok")
        checks.assert_called_once()
        keycheck.assert_called_once()
        self.assertEqual(status["live_snapshot"], "a" * 20)

    @contextmanager
    def apply_fixture(self, *, live_after=None, proc_root=None):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / ("c" * 20)
            runtime.mkdir()
            proc = Path(proc_root) if proc_root is not None else Path(temporary) / "proc"
            proc.mkdir(exist_ok=True)
            (runtime / "manifest.json").write_text(json.dumps({"server.py": "b" * 64}))
            installed = {"runtime_dir": str(runtime), "snapshot": runtime.name,
                         "server": str(runtime / "server.py")}
            fake = types.SimpleNamespace(install=lambda *args: installed)
            after = live_after or dict(OLD_LIVE, snapshot=runtime.name,
                                      server=installed["server"], mcp_pid=202,
                                      interpreter="/fixture/python")
            with patch.object(module, "_import_installer", return_value=fake), \
                 patch.object(module, "inventory", return_value={"chatgpt-mcp/server.py": "b" * 64}), \
                 patch.object(module, "control_inventory", return_value={}), \
                 patch.object(module, "run_checks"), \
                 patch.object(module, "tunnel_client", return_value="client"), \
                 patch.object(module, "_key_check"), \
                 patch.object(module, "runtime_interpreter", return_value="/fixture/python"), \
                 patch.object(module, "expected_tool_names", return_value=module.TOOL_NAMES), \
                 patch.object(module, "CliSessionObserver",
                              partial(module.CliSessionObserver, proc_root=proc)), \
                 patch("server.load_projects", return_value={}), \
                 patch("rollout_tunnel.freeze_live_runtime", return_value=OLD_LIVE), \
                 patch("rollout_tunnel.preflight_tunnel", return_value={}), \
                 patch("rollout_tunnel.switch_tunnel") as switch, \
                 patch.object(module, "observe_tools", return_value={
                     "catalog_status": "observed", "tools_registered": True,
                     "connector_schemas_verified": True, "tool_count": 7}), \
                 patch("rollout_admin.preflight_admin"), \
                 patch("rollout_admin.switch_admin", return_value="admin_ready") as admin, \
                 patch("sys.stdin", TTY("ROLLOUT\n")), \
                 patch("sys.stdout", TTY()):
                def activate(*args, verify_fn, **kw):
                    verify_fn(after)
                    return dict(after, status="tunnel_ready")
                switch.side_effect = activate
                yield switch, admin

    def healthy_status(self, _):
        return {"tunnel_id": "tunnel-123", "process": {"pid": 101},
                "process_running": True, "healthy": True, "ready": True,
                "target_value": "/fixture/new/server.py"}


    def test_healthy_profile_target_cannot_hide_old_actual_mcp(self):
        with self.apply_fixture(live_after=OLD_LIVE) as (switch, admin):
            with self.assertRaisesRegex(RuntimeError, "LIVE_RUNTIME_MISMATCH"):
                module.rollout(apply=True, status_fn=self.healthy_status,
                               command_fn=lambda *args, **kw: b"")
            switch.assert_called_once()
            admin.assert_not_called()

    def test_web_mode_rejects_fake_grant_and_tty_mode_stays_required(self):
        with self.apply_fixture() as (switch, admin):
            with patch("sys.stdin", io.StringIO("")), patch("sys.stdout", io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "HOST_TTY_REQUIRED"):
                    module.rollout(apply=True, status_fn=self.healthy_status,
                                   command_fn=lambda *args, **kw: b"")
                with self.assertRaisesRegex(RuntimeError, "WEB_ROLLOUT_GRANT_INVALID"):
                    module.rollout(apply=True, approved_web=object(),
                                   status_fn=self.healthy_status,
                                   command_fn=lambda *args, **kw: b"")
            switch.assert_not_called()
            admin.assert_not_called()

    def test_success_requires_live_snapshot_and_admin_verification(self):
        with self.apply_fixture() as (switch, admin):
            result = module.rollout(apply=True, status_fn=self.healthy_status,
                                    command_fn=lambda *args, **kw: b"")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["snapshot"], result["live_snapshot"])
        self.assertTrue(result["runtime_verified"])
        self.assertFalse(result["schema_013_applied"])
        switch.assert_called_once()
        admin.assert_called_once()

    def test_apply_verification_observes_the_policy_aware_catalog(self):
        policy_tools = module.TOOL_NAMES | {"policy-aware-sentinel"}
        observed = {
            "catalog_status": "observed", "tools_registered": True,
            "connector_schemas_verified": True, "tool_count": len(policy_tools),
        }
        with self.apply_fixture(), \
             patch.object(module, "expected_tool_names", return_value=policy_tools) as expected, \
             patch.object(module, "observe_tools", return_value=observed) as observe:
            module.rollout(apply=True, status_fn=self.healthy_status,
                           command_fn=lambda *args, **kw: b"")
        expected.assert_called_once_with({})
        observe.assert_called_once_with(policy_tools, url_file=self.layout["health_url_file"])

    def test_success_receipt_contains_cli_observations_at_each_switch_boundary(self):
        from rollout_diagnostics import RolloutReceipt
        with tempfile.TemporaryDirectory() as temporary, self.apply_fixture():
            receipt = RolloutReceipt(temporary, mode="apply")
            result = module.rollout(apply=True, receipt=receipt,
                                    status_fn=self.healthy_status,
                                    command_fn=lambda *args, **kw: b"")
            events = json.loads(receipt.path.read_text())["events"]
        stages = [event["stage"] for event in events]
        self.assertIn("cli_before_switch", stages)
        self.assertLess(stages.index("cli_before_switch"), stages.index("tunnel_switch"))
        self.assertIn("cli_after_tunnel", stages)
        self.assertIn("cli_after_switches", stages)
        self.assertIn("cli_observation_complete", result)

    def test_failed_tunnel_still_records_cli_observations(self):
        from rollout_diagnostics import RolloutReceipt
        with tempfile.TemporaryDirectory() as temporary, self.apply_fixture() as (switch, _):
            receipt = RolloutReceipt(temporary, mode="apply")
            switch.side_effect = RuntimeError("FIXTURE_SWITCH_FAILURE")
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_SWITCH_FAILURE"):
                module.rollout(apply=True, receipt=receipt,
                               status_fn=self.healthy_status,
                               command_fn=lambda *args, **kw: b"")
            stages = [event["stage"] for event in receipt.data["events"]]
        self.assertIn("cli_before_switch", stages)
        self.assertIn("cli_after_switches", stages)

    def test_unobservable_cli_sessions_do_not_claim_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "proc"
            with self.apply_fixture(proc_root=proc):
                proc.rmdir()
                result = module.rollout(apply=True, status_fn=self.healthy_status,
                                        command_fn=lambda *args, **kw: b"")
        self.assertEqual(result["status"], "verification_required")
        self.assertFalse(result["cli_observation_complete"])
        self.assertFalse(result["cli_sessions_preserved"])
        self.assertEqual(result.get("code"), "CLI_OBSERVATION_INCOMPLETE")

    def test_lost_cli_keeps_runtime_verified_but_requires_review_with_a_safe_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "proc"
            folder = proc / "501"
            folder.mkdir(parents=True)
            fields = ["S", "17", "500", "400", "34816"] + ["0"] * 45
            fields[19] = "123"
            (folder / "stat").write_text("501 (fixture) " + " ".join(fields))
            (folder / "cmdline").write_bytes(b"codex\0resume\0")
            (folder / "cgroup").write_text("0::/fixture\n")
            with self.apply_fixture(proc_root=proc) as (switch, _):
                activate = switch.side_effect
                def switch_and_exit(*args, **kwargs):
                    for file in folder.iterdir():
                        file.unlink()
                    folder.rmdir()
                    return activate(*args, **kwargs)
                switch.side_effect = switch_and_exit
                result = module.rollout(apply=True, status_fn=self.healthy_status,
                                        command_fn=lambda *args, **kw: b"")
        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(result.get("code"), "CLI_SESSIONS_CHANGED")
        self.assertTrue(result["runtime_verified"])
        self.assertFalse(result["cli_sessions_preserved"])
        self.assertEqual(result["cli_changed_count"], 1)

    def test_admin_failure_reports_partial_rollout_without_false_ready(self):
        from rollout_admin import AdminSwitchError
        with self.apply_fixture(), patch("rollout_admin.switch_admin", side_effect=
                AdminSwitchError("ADMIN_SWITCH_FAILED", "ADMIN_MAINPID_MISMATCH")):
            result = module.rollout(apply=True, status_fn=self.healthy_status,
                                    command_fn=lambda *args, **kw: b"")
        self.assertEqual(result["status"], "verification_required")
        self.assertTrue(result["switched"])
        self.assertEqual(result["code"], "ADMIN_SWITCH_FAILED")
        self.assertEqual(result["cause_code"], "ADMIN_MAINPID_MISMATCH")

    def test_unobserved_connector_discovery_keeps_rollout_pending(self):
        with self.apply_fixture(), patch.object(module, "observe_tools", return_value={
                "catalog_status": "awaiting_discovery", "tools_registered": False,
                "connector_schemas_verified": False, "tool_count": 0}):
            result = module.rollout(apply=True, status_fn=self.healthy_status,
                                    command_fn=lambda *args, **kw: b"")
        self.assertEqual(result["status"], "verification_required")
        self.assertTrue(result["switched"])
        self.assertFalse(result["tools_registered"])
        self.assertFalse(result["connector_schemas_verified"])

    def test_mismatched_existing_tunnel_rejected_before_install(self):
        fake = types.SimpleNamespace(install=lambda *args: self.fail("unexpected install"))
        with patch.object(module, "_import_installer", return_value=fake), \
             patch.object(module, "inventory", return_value={}), \
             patch.object(module, "run_checks"), \
             patch.object(module, "tunnel_client", return_value="client"), \
             patch("server.load_projects", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "TUNNEL_ID_MISMATCH"):
                module.rollout(tunnel_id="other-tunnel", status_fn=lambda _: {
                    "tunnel_id": "tunnel-123"})

    def test_missing_status_id_rejected_before_key_access(self):
        fake = types.SimpleNamespace(install=lambda *args: self.fail("unexpected install"))
        with patch.object(module, "_import_installer", return_value=fake), \
             patch.object(module, "inventory", return_value={}), \
             patch.object(module, "run_checks"), \
             patch.object(module, "tunnel_client", return_value="client"), \
             patch("server.load_projects", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "TUNNEL_ID_UNAVAILABLE"):
                module.rollout(status_fn=lambda _: {"ready": True})

    def test_failed_cli_keeps_safe_receipt_and_exit_code_after_window_closes(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(module, "rollout", side_effect=ValueError("private-token-fixture")), \
             patch("sys.stdout", new_callable=io.StringIO) as out, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            code = module.main(["--diagnostics-dir", temporary])
            receipts = list(Path(temporary).glob("rollout-*/receipt.json"))
            self.assertEqual(len(receipts), 1)
            data = json.loads(receipts[0].read_text())
        self.assertEqual(code, 1)
        self.assertEqual(data["exit_code"], 1)
        self.assertEqual(data["result"]["code"], "ROLLOUT_UNEXPECTED_ERROR")
        self.assertNotIn("private-token-fixture", out.getvalue() + err.getvalue())
        self.assertIn("receipt.json", out.getvalue() + err.getvalue())

    def test_cli_verification_failure_records_exit_two(self):
        result = {"status": "verification_required", "snapshot": "c" * 20,
                  "switched": False, "code": "LIVE_RUNTIME_MISMATCH"}
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(module, "rollout", return_value=result), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(module.main(["--diagnostics-dir", temporary]), 2)
            path = next(Path(temporary).glob("rollout-*/receipt.json"))
            data = json.loads(path.read_text())
        self.assertEqual(data["exit_code"], 2)
        self.assertFalse(data["result"]["switched"])

    def test_cli_receipt_storage_failure_never_leaks_original_or_os_error(self):
        from rollout_diagnostics import RolloutReceipt
        with tempfile.TemporaryDirectory() as temporary:
            receipt = RolloutReceipt(temporary)
            with patch.object(module, "RolloutReceipt", return_value=receipt), \
                 patch.object(module, "rollout", side_effect=RuntimeError("TUNNEL_SWITCH_FAILED")), \
                 patch.object(receipt, "_save", side_effect=[None, OSError("private-log-fixture"),
                                                             OSError("private-log-fixture")]), \
                 patch("sys.stdout", TTY()) as out, \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(module.main([]), 1)
            output = out.getvalue() + err.getvalue()
        self.assertIn("TUNNEL_SWITCH_FAILED", output)
        self.assertIn("ROLLOUT_RECEIPT_WRITE_FAILED", output)
        self.assertNotIn("private-log-fixture", output)

    def test_admin_interruption_is_not_converted_to_partial_success(self):
        with self.apply_fixture(), patch("rollout_admin.switch_admin", side_effect=module.RolloutTerminated()):
            with self.assertRaises(module.RolloutTerminated):
                module.rollout(apply=True, status_fn=self.healthy_status,
                               command_fn=lambda *args, **kw: b"")


if __name__ == "__main__":
    unittest.main()
