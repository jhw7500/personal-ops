import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_module("bootstrap", MODULE_DIR / "bootstrap.py")
installer = load_module("install_runtime", MODULE_DIR / "install-runtime.py")
from workspace_control import (  # noqa: E402
    ABI3_MANDATORY_RELEASE_FILES,
    MANDATORY_RELEASE_FILES,
    verify_launch,
    verify_release_manifest,
)


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-bootstrap-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.projects_root = self.home / "projects"
        self.personal_ops = self.projects_root / "personal-ops"
        self.pim_check = self.projects_root / "pim-check"
        self.personal_ops.mkdir(parents=True)
        self.pim_check.mkdir()
        self.library_root = self.home / ".local/lib/chatgpt-coding-mcp"
        self.environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_STATE_HOME": str(self.state_home),
        }
        self.arguments = [
            "--source", str(MODULE_DIR),
            "--project", f"personal-ops={self.personal_ops}",
            "--project", f"pim-check={self.pim_check}",
        ]

    def tearDown(self):
        self.temp.cleanup()

    def run_bootstrap(self, *, apply=False):
        arguments = [*self.arguments]
        if apply:
            arguments.append("--apply")
        with patch.dict(os.environ, self.environment, clear=False):
            return bootstrap.run(arguments)

    def test_default_plan_validates_inputs_without_writing_owner_paths(self):
        result = self.run_bootstrap()

        self.assertEqual(result["status"], "plan")
        self.assertFalse(result["applied"])
        self.assertEqual(result["minimum_abi"], 3)
        self.assertEqual(result["projects"], {
            "personal-ops": str(self.personal_ops),
            "pim-check": str(self.pim_check),
        })
        self.assertFalse(self.config_home.exists())
        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())

    def test_apply_creates_private_admitted_setup_without_issuing_grant(self):
        result = self.run_bootstrap(apply=True)

        self.assertEqual(result["status"], "configured")
        self.assertTrue(result["applied"])
        runtime = Path(result["runtime"])
        authority = self.state_home / "chatgpt-coding-mcp/workspace-authority"
        policy_path = self.config_home / "chatgpt-coding-mcp/workspace-policy.json"
        projects_path = self.config_home / "chatgpt-coding-mcp/projects.json"
        controller = self.library_root / "workspace-controller"
        for directory in (policy_path.parent, authority, controller, runtime):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (policy_path, projects_path, runtime / "manifest.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["version"], 2)
        self.assertEqual(
            [(rule["project"], rule["path"], rule["content_mode"], rule["fields"])
             for rule in policy["artifacts"]],
            [("pim-check", name, "text", [])
             for name in ("reports", "hw-results", "events")],
        )
        manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
        required = MANDATORY_RELEASE_FILES | ABI3_MANDATORY_RELEASE_FILES | {"bootstrap.py"}
        self.assertTrue(required <= manifest.keys())
        verified = verify_release_manifest(runtime)
        launch = verify_launch(runtime, authority, enforcement_required=True)
        self.assertEqual(launch["status"], "launch_allowed")
        self.assertEqual(launch["minimum_abi"], 3)
        self.assertEqual(launch["release_digest"], verified["release_digest"])
        self.assertNotEqual(runtime, MODULE_DIR)

        installation = json.loads(
            (self.state_home / "chatgpt-coding-mcp/host-access/installation-scope-v2.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(installation["version"], 2)
        self.assertEqual(installation["requests"], {})
        self.assertIsNone(installation["grant"])
        self.assertFalse((self.config_home / "chatgpt-coding-mcp/runtime-key").exists())
        self.assertFalse((self.state_home / "chatgpt-coding-mcp/tunnel").exists())

        self.assertEqual(result["launch_argv"][0], sys.executable)
        interpreter_index = result["launch_argv"].index("--interpreter") + 1
        self.assertEqual(result["launch_argv"][interpreter_index], sys.executable)
        completed = subprocess.run(
            [*result["launch_argv"], "--help"], cwd=self.root,
            env=dict(os.environ, **self.environment, PYTHONPATH=str(self.root / "rogue")),
            text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout)

    def test_apply_creates_private_xdg_ancestors_independent_of_umask(self):
        previous = os.umask(0o002)
        try:
            result = self.run_bootstrap(apply=True)
        finally:
            os.umask(previous)

        self.assertEqual(result["status"], "configured")
        for directory in (
            self.config_home,
            self.state_home,
            self.home / ".local",
            self.home / ".local/lib",
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_repeated_apply_preserves_matching_setup(self):
        first = self.run_bootstrap(apply=True)
        authority_state = (
            self.state_home / "chatgpt-coding-mcp/workspace-authority/authority.json"
        )
        installation_state = (
            self.state_home / "chatgpt-coding-mcp/host-access/installation-scope-v2.json"
        )
        before = (authority_state.read_bytes(), installation_state.read_bytes())

        second = self.run_bootstrap(apply=True)

        self.assertEqual(second["status"], "already_configured")
        self.assertEqual(second["runtime"], first["runtime"])
        self.assertEqual(before, (authority_state.read_bytes(), installation_state.read_bytes()))

    def test_apply_refuses_different_existing_config_before_other_writes(self):
        config_dir = self.config_home / "chatgpt-coding-mcp"
        config_dir.mkdir(parents=True, mode=0o700)
        projects_path = config_dir / "projects.json"
        projects_path.write_text('{"projects":{"other":"/tmp"}}', encoding="utf-8")
        projects_path.chmod(0o600)

        with self.assertRaisesRegex(bootstrap.BootstrapError, "EXISTING_SETUP_DIFFERS"):
            self.run_bootstrap(apply=True)

        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())
        self.assertEqual(projects_path.read_text(), '{"projects":{"other":"/tmp"}}')

    def test_apply_refuses_symlinked_config_before_other_writes(self):
        outside = self.root / "outside.json"
        outside.write_text("protected", encoding="utf-8")
        config_dir = self.config_home / "chatgpt-coding-mcp"
        config_dir.mkdir(parents=True, mode=0o700)
        (config_dir / "projects.json").symlink_to(outside)

        with self.assertRaisesRegex(bootstrap.BootstrapError, "UNSAFE_EXISTING_SETUP"):
            self.run_bootstrap(apply=True)

        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())
        self.assertEqual(outside.read_text(), "protected")

    def test_apply_refuses_unsafe_owner_ancestor_before_any_write(self):
        self.home.chmod(0o775)

        with self.assertRaisesRegex(bootstrap.BootstrapError, "UNSAFE_EXISTING_SETUP"):
            self.run_bootstrap(apply=True)

        self.assertFalse(self.config_home.exists())
        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())

    def test_apply_refuses_unsafe_interpreter_before_any_write(self):
        with patch.object(
            bootstrap, "_validated_interpreter",
            side_effect=bootstrap.BootstrapError("UNSAFE_BOOTSTRAP_INTERPRETER"),
        ) as validate:
            with self.assertRaisesRegex(bootstrap.BootstrapError,
                                        "UNSAFE_BOOTSTRAP_INTERPRETER"):
                self.run_bootstrap(apply=True)

        validate.assert_called_once_with(sys.executable)
        self.assertFalse(self.config_home.exists())
        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())

    def test_apply_refuses_config_root_inside_project_before_any_write(self):
        self.environment["XDG_CONFIG_HOME"] = str(self.personal_ops / "owner-config")
        config_root = Path(self.environment["XDG_CONFIG_HOME"]) / "chatgpt-coding-mcp"

        with self.assertRaisesRegex(bootstrap.BootstrapError,
                                    "OWNER_PATH_OVERLAPS_PROJECT"):
            self.run_bootstrap(apply=True)

        self.assertFalse(config_root.exists())
        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())

    def test_apply_refuses_state_root_inside_project_before_any_write(self):
        self.environment["XDG_STATE_HOME"] = str(self.pim_check / "owner-state")
        state_root = Path(self.environment["XDG_STATE_HOME"]) / "chatgpt-coding-mcp"

        with self.assertRaisesRegex(bootstrap.BootstrapError,
                                    "OWNER_PATH_OVERLAPS_PROJECT"):
            self.run_bootstrap(apply=True)

        self.assertFalse(self.config_home.exists())
        self.assertFalse(state_root.exists())
        self.assertFalse(self.library_root.exists())

    def assert_project_layout_refused_without_owner_writes(self, projects):
        arguments = ["--source", str(MODULE_DIR)]
        for name, path in projects:
            arguments.extend(("--project", f"{name}={path}"))
        arguments.append("--apply")

        with patch.dict(os.environ, self.environment, clear=False):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "INVALID_PROJECT_LAYOUT"):
                bootstrap.run(arguments)

        self.assertFalse(self.config_home.exists())
        self.assertFalse(self.state_home.exists())
        self.assertFalse(self.library_root.exists())

    def test_project_equal_to_base_is_refused_before_any_write(self):
        self.assert_project_layout_refused_without_owner_writes([
            ("personal-ops", self.projects_root),
            ("pim-check", self.pim_check),
        ])

    def test_duplicate_project_root_is_refused_before_any_write(self):
        self.assert_project_layout_refused_without_owner_writes([
            ("personal-ops", self.personal_ops),
            ("pim-check", self.personal_ops),
        ])

    def test_nested_project_roots_are_refused_before_any_write(self):
        nested = self.personal_ops / "nested"
        nested.mkdir()
        self.assert_project_layout_refused_without_owner_writes([
            ("personal-ops", self.personal_ops),
            ("pim-check", nested),
        ])

    def test_server_default_config_honors_xdg_config_home(self):
        environment = dict(os.environ, **self.environment, PYTHONPATH=str(MODULE_DIR))
        completed = subprocess.run(
            [sys.executable, "-B", "-c", "import server; print(server.CONFIG)"],
            cwd=MODULE_DIR, env=environment, text=True, capture_output=True, check=True,
        )

        self.assertEqual(
            completed.stdout.strip(),
            str(self.config_home / "chatgpt-coding-mcp/projects.json"),
        )

    def test_runtime_inventory_contains_bootstrap_and_separate_abi3_helpers(self):
        self.assertIn("bootstrap.py", installer.FILES)
        self.assertTrue(ABI3_MANDATORY_RELEASE_FILES <= set(installer.FILES))
        self.assertTrue(ABI3_MANDATORY_RELEASE_FILES.isdisjoint(MANDATORY_RELEASE_FILES))
        self.assertIn("validate_change_evidence.py", ABI3_MANDATORY_RELEASE_FILES)


if __name__ == "__main__":
    unittest.main()
