import ast
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
spec = importlib.util.spec_from_file_location("install_runtime", MODULE_DIR / "install-runtime.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
REGISTERED_MODULES = ("host_registry.py", "host_actions.py", "host_process.py",
                      "host_source_review.py", "host_registered_operations.py",
                      "host_compose_actions.py", "host_registered_tools.py")
AGENT_MODULES = ("agent_profiles.py", "agent_tasks.py", "agent_runner.py",
                 "agent_events.py", "agent_results.py", "agent_tools.py",
                 "agent_host_broker.py", "agent_broker_server.py",
                 "agent-bootstrap.c", "agent_bootstrap.py", "agent_supervisor.py",
                 "agent_probe.py", "agent_code_mode.py", "agent_proofs.py", "agent_smoke.py",
                 "agent_profile_setup.py",
                 "configure-agent-profile.py", "agent-admin.js")
HOST_COMMAND_MODULES = ("host_command_profiles.py", "host_command_entry.py",
                        "host_command_runner.py", "host_command_supervisor.py",
                        "host_command_operations.py", "host_command_tools.py",
                        "host-command-admin.js", "host_command_ui.py", "host-command-approval.html")
HOST_ACCESS_MODULES = ("host_access.py", "host_execution.py", "host_execution_tools.py",
                       "host_agent.py", "host-access-admin.js", "host_access_ui.py",
                       "host-access-approval.html")
WORKROOM_AUTHORITY_FILES = ("workroom-frame.html", "workroom-frame.js", "workroom-bridge.js")
PROBE = '''
import sys
sys.path.insert(0, sys.argv[1])
import host_runtime_operations
import host_registered_operations
import codex_observer
manager = host_registered_operations.RegisteredHostOperations({}, None, state_dir=sys.argv[2])
print(host_runtime_operations._verified_installation(), manager._trusted_runtime)
'''


class RuntimeInstallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chatgpt-mcp-install-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "repo"
        self.source.mkdir()
        for name in installer.FILES:
            (self.source / name).write_text("original\n")
        self.dashboard = self.root / 'work-dashboard'
        self.dashboard.mkdir()
        for name in ('model.js', 'motion.js', 'pixels.js', 'visualizer.js', 'visualizer.css'):
            (self.dashboard / name).write_text('dashboard fixture\n')
        self.projects = {"repo": self.source}

    def tearDown(self):
        self.temporary.cleanup()

    def test_snapshot_stays_unchanged_when_the_project_is_edited(self):
        result = installer.install(self.source, self.root / "runtime", self.projects)
        release = Path(result["runtime_dir"])
        self.assertEqual((release / 'workspace_view.py').read_text(), 'original\n')
        for name in ('codex_hooks.py', 'codex_observer.py', 'codex_activity.py', 'codex_hook_report.py',
                     'claude_hooks.py', 'claude_hook_report.py', 'host_runtime.py',
                     'host_runtime_config.py', 'host_runtime_operations.py',
                     'host_runtime_tools.py', 'host_runtime_web.py', 'host_db_migration.py',
                     'toss_console_evidence.py', 'toss_console_tools.py',
                     'toss_console_sdk_bridge.py', 'toss_console_codex.py',
                     'approve-host-operation.py', *REGISTERED_MODULES, *AGENT_MODULES,
                     *HOST_COMMAND_MODULES):
            self.assertTrue((release / name).is_file(), name)
        self.assertEqual(result['file_count'],
                         len(installer.FILES) + len(installer.DASHBOARD_FILES))
        (self.source / "server.py").write_text("changed\n")
        self.assertEqual(Path(result["server"]).read_text(), "original\n")
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        for path in release.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        newer = installer.install(self.source, self.root / "runtime", self.projects)
        self.assertNotEqual(result["snapshot"], newer["snapshot"])
        (Path(newer["runtime_dir"]) / "server.py").write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            installer.install(self.source, self.root / "runtime", self.projects)

    def test_dashboard_snapshot_is_private_and_immutable(self):
        result = installer.install(self.source, self.root / 'runtime', self.projects)
        release = Path(result['runtime_dir'])
        self.assertTrue((release / 'work-dashboard/model.js').is_file())
        self.assertTrue((release / 'work-dashboard/motion.js').is_file())
        self.assertTrue((release / 'work-dashboard/pixels.js').is_file())
        self.assertEqual((release / 'work-dashboard/model.js').read_text(), 'dashboard fixture\n')
        self.assertEqual(stat.S_IMODE((release / 'work-dashboard').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((release / 'work-dashboard/model.js').stat().st_mode), 0o600)
        (self.dashboard / 'model.js').write_text('edited UI\n')
        self.assertEqual((release / 'work-dashboard/model.js').read_text(), 'dashboard fixture\n')
        newer = installer.install(self.source, self.root / 'runtime', self.projects)
        self.assertNotEqual(result['snapshot'], newer['snapshot'])
        target = Path(newer['runtime_dir']) / 'work-dashboard/model.js'
        target.write_text('tampered\n')
        with self.assertRaises(ValueError):
            installer.install(self.source, self.root / 'runtime', self.projects)

    def test_dashboard_symlink_directory_is_rejected(self):
        outside = self.root / 'external-ui'
        self.dashboard.rename(outside)
        self.dashboard.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(self.source, self.root / 'runtime', self.projects)

    def test_exact_release_cannot_be_an_allowed_writable_project(self):
        result = installer.install(self.source, self.root / 'runtime', self.projects)
        release = Path(result['runtime_dir'])
        projects = {**self.projects, 'runtime-as-project': release}
        before = (release / 'server.py').read_bytes()
        with self.assertRaisesRegex(ValueError, 'outside'):
            installer.install(self.source, self.root / 'runtime', projects)
        self.assertEqual((release / 'server.py').read_bytes(), before)

    def test_existing_release_with_unsafe_directory_mode_is_rejected(self):
        result = installer.install(self.source, self.root / 'runtime', self.projects)
        release = Path(result['runtime_dir'])
        release.chmod(0o777)
        with self.assertRaises(ValueError):
            installer.install(self.source, self.root / 'runtime', self.projects)
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o777)

    def test_writable_project_and_symlink_installation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            installer.install(self.source, self.source / "runtime", self.projects)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.install(self.source, link, self.projects)
        (self.source / "server.py").unlink()
        (self.source / "server.py").symlink_to(outside / "server.py")
        with self.assertRaises(ValueError):
            installer.install(self.source, self.root / "runtime", self.projects)


class VerifiedInstallationTest(unittest.TestCase):
    def test_every_local_python_import_in_the_runtime_is_packaged(self):
        packaged = set(installer.FILES)
        local_modules = {path.stem: path.name for path in MODULE_DIR.glob("*.py")}
        missing = {}
        for name in packaged:
            path = MODULE_DIR / name
            if path.suffix != ".py" or not path.exists():
                continue
            imported = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            omitted = sorted(local_modules[module] for module in imported
                             if module in local_modules
                             and local_modules[module] not in packaged)
            if omitted:
                missing[name] = omitted
        self.assertEqual(missing, {})

    def test_registered_runtime_files_are_packaged_and_verified(self):
        import host_runtime_operations
        packaged = set(installer.FILES) | {'work-dashboard/' + name for name in installer.DASHBOARD_FILES}
        verified = set(host_runtime_operations.VERIFIED_RUNTIME_FILES)
        self.assertTrue(set(REGISTERED_MODULES) <= set(installer.FILES))
        self.assertTrue(set(AGENT_MODULES) <= set(installer.FILES))
        self.assertTrue(set(HOST_COMMAND_MODULES) <= set(installer.FILES))
        self.assertTrue(set(HOST_COMMAND_MODULES) <= verified)
        self.assertTrue(set(HOST_ACCESS_MODULES) <= set(installer.FILES))
        self.assertTrue(set(HOST_ACCESS_MODULES) <= verified)
        self.assertTrue(set(WORKROOM_AUTHORITY_FILES) <= set(installer.FILES))
        self.assertTrue(set(WORKROOM_AUTHORITY_FILES) <= verified)
        self.assertIn("toss_console_codex.py", installer.FILES)
        self.assertIn("toss_console_codex.py", verified)
        self.assertTrue(set(REGISTERED_MODULES) <= verified)
        self.assertTrue(set(AGENT_MODULES) <= verified)
        self.assertTrue(verified <= packaged)
        # The legacy list is extended, never replaced.
        self.assertTrue({"server.py", "host_runtime.py", "host_runtime_config.py",
                         "host_runtime_operations.py", "host_runtime_tools.py",
                         "approve-host-operation.py", "host_runtime_web.py", "host_db_migration.py",
                         "admin_server.py", "admin.html", "admin.js", "admin.css"} <= verified)
        self.assertTrue({'work-dashboard/' + name for name in installer.DASHBOARD_FILES} <= verified)

    def test_one_changed_byte_in_any_verified_runtime_file_makes_it_untrusted(self):
        import host_runtime_operations
        with tempfile.TemporaryDirectory(prefix="verified-install-") as folder:
            home = Path(folder) / "home"
            releases = home / ".local/lib/chatgpt-coding-mcp/releases"
            releases.mkdir(parents=True, mode=0o700)
            project = Path(folder) / "project"
            project.mkdir()
            release = Path(installer.install(MODULE_DIR, releases, {"project": project})["runtime_dir"])
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            env["HOME"] = str(home)

            def probe():
                # -B: no bytecode cache, so every probe compiles the bytes on disk.
                result = subprocess.run([sys.executable, "-B", "-c", PROBE, str(release),
                                         str(Path(folder) / "state")],
                                        cwd=release, env=env, capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                return result.stdout.split()

            self.assertEqual(probe(), ["True", "True"])
            for name in (*REGISTERED_MODULES, *AGENT_MODULES, *HOST_COMMAND_MODULES,
                         "work-dashboard/visualizer.js", "admin.js"):
                with self.subTest(name=name):
                    self.assertIn(name, host_runtime_operations.VERIFIED_RUNTIME_FILES)
                    path = release / name
                    original = path.read_bytes()
                    self.assertEqual(original[-1:], b"\n")
                    # One changed byte that keeps the module importable: only the digest differs.
                    path.write_bytes(original[:-1] + b" ")
                    try:
                        self.assertEqual(probe(), ["False", "False"])
                    finally:
                        path.write_bytes(original)
            self.assertEqual(probe(), ["True", "True"])


if __name__ == "__main__":
    unittest.main()
