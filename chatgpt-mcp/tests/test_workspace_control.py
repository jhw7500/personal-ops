import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import io


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_authority import WorkspaceAuthority  # noqa: E402
from workspace_control import (ABI3_MANDATORY_RELEASE_FILES, MANDATORY_RELEASE_FILES,  # noqa: E402
                               WorkspaceControlError,
                               install_controller, main, transition_release,
                               verify_launch)


POLICY_A = "a" * 64
POLICY_B = "b" * 64


def load_launcher():
    path = ROOT / "workspace-launcher.py"
    spec = importlib.util.spec_from_file_location("workspace_launcher_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspaceControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.releases = self.root / "releases"
        self.releases.mkdir(mode=0o700)
        self.project = self.root / "projects" / "demo"
        self.project.mkdir(parents=True)
        self.authority_dir = self.root / "authority"

    def tearDown(self):
        self.temporary.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(WorkspaceControlError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_release_contract_includes_file_and_host_enforcement_modules(self):
        self.assertTrue({"workspace_files.py", "workspace_host.py"}
                        <= MANDATORY_RELEASE_FILES)

    def make_release(self, marker, *, omit=(), enforcement_abi=None,
                     capability_generation=None, overrides=None):
        manifest = {}
        contents = {}
        overrides = overrides or {}
        files = set(MANDATORY_RELEASE_FILES)
        if enforcement_abi is not None and enforcement_abi >= 3:
            files.update(ABI3_MANDATORY_RELEASE_FILES)
        for name in files:
            if name in omit:
                continue
            content = overrides.get(name)
            if content is None:
                content = ((f"# {marker}:{name}\n" if name == "workspace_control.py"
                            and enforcement_abi is not None else
                            f"{marker}:{name}\n").encode())
            if name == "workspace_control.py" and enforcement_abi is not None:
                content += f"ENFORCEMENT_ABI = {enforcement_abi}\n".encode()
                if capability_generation is not None:
                    content += ("HOST_ACCESS_CAPABILITY_GENERATION = "
                                f"{capability_generation}\n").encode()
            contents[name] = content
            manifest[name] = hashlib.sha256(content).hexdigest()
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        release = self.releases / digest[:20]
        release.mkdir(mode=0o700)
        for name in manifest:
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(contents[name])
            path.chmod(0o600)
        (release / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8",
        )
        (release / "manifest.json").chmod(0o600)
        return release, digest

    def authority(self, allowed):
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY_A, allowed)
        return authority

    def test_absent_authority_preserves_legacy_launch_without_creating_state(self):
        missing_release = self.releases / ("0" * 20)

        result = verify_launch(missing_release, self.authority_dir)

        self.assertEqual(result, {"status": "legacy_allowed", "authority_active": False})
        self.assertFalse(self.authority_dir.exists())

    def test_active_authority_accepts_only_reviewed_full_manifest_digest_and_abi(self):
        release, digest = self.make_release(
            "compatible", enforcement_abi=3, capability_generation=1,
        )
        binding = self.authority({digest: 3}).binding(POLICY_A)

        result = verify_launch(release, self.authority_dir)

        self.assertEqual(result["status"], "launch_allowed")
        self.assertEqual(result["release_digest"], digest)
        self.assertEqual(result["release_abi"], 3)
        self.assertEqual(result["binding"], binding)
        self.assertEqual(result["authority_dir"], str(self.authority_dir.absolute()))

    def test_floor_two_still_validates_declared_release_abi(self):
        release, digest = self.make_release("floor-two", enforcement_abi=1)
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY_A, {digest: 2}, minimum_abi=2)

        self.assert_code(
            "WORKSPACE_RELEASE_ABI_MISMATCH",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_historical_abi_two_release_does_not_require_v3_host_access_helper(self):
        release, digest = self.make_release(
            "historical-two", enforcement_abi=2,
            omit={"host_access.py", "host_access_installation.py"},
        )
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY_A, {digest: 2}, minimum_abi=2)

        admitted = verify_launch(release, self.authority_dir)

        self.assertEqual(admitted["release_abi"], 2)
        self.assertIsNone(admitted["host_access_capability_generation"])

    def test_abi_three_release_requires_v3_host_access_helper(self):
        release, digest = self.make_release(
            "abi-three-missing-helper", enforcement_abi=3,
            capability_generation=1,
            omit={"host_access_installation.py"},
        )
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY_A, {digest: 3}, minimum_abi=3)

        self.assert_code(
            "WORKSPACE_RELEASE_INCOMPATIBLE",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_unknown_future_release_abi_is_rejected(self):
        release, digest = self.make_release("future", enforcement_abi=4)
        self.authority({digest: 4})

        self.assert_code(
            "WORKSPACE_RELEASE_INCOMPATIBLE",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_abi_three_admission_exposes_declared_host_access_generation(self):
        release, digest = self.make_release(
            "abi-three", enforcement_abi=3, capability_generation=7,
        )
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=self.authority_dir,
        )
        authority.initialize(POLICY_A, {digest: 3}, minimum_abi=3)

        result = verify_launch(release, self.authority_dir)

        self.assertEqual(result["enforcement_abi"], 3)
        self.assertEqual(result["host_access_capability_generation"], 7)

    def test_enforcement_latch_blocks_legacy_after_authority_root_deletion(self):
        release, digest = self.make_release("compatible")
        authority = self.authority({digest: 1})
        latch = authority.enforcement_path
        for child in sorted(self.authority_dir.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.authority_dir.rmdir()

        self.assertTrue(latch.exists())
        self.assert_code(
            "WORKSPACE_AUTHORITY_MISSING",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_required_launch_and_transition_advance_epoch_and_tombstone(self):
        release, digest = self.make_release("compatible")
        authority = self.authority({digest: 1})
        request = authority.request(
            "demo", str(release / "server.py"), ["read"], 1800,
            "fixture", POLICY_A,
        )

        transitioned = transition_release(release, self.authority_dir)

        self.assertEqual(transitioned["binding"]["authority_epoch"], 2)
        self.assertEqual(authority.get("demo", request["request_id"])["status"],
                         "tombstoned")
        self.assert_code(
            "WORKSPACE_ENFORCEMENT_REQUIRED",
            lambda: verify_launch(release, self.root / "never", enforcement_required=True),
        )

    def test_default_authority_uses_current_xdg_state_home_and_never_falls_back(self):
        release, digest = self.make_release("compatible")
        xdg_state = self.root / "custom-state"
        authority_dir = xdg_state / "chatgpt-coding-mcp" / "workspace-authority"
        authority = WorkspaceAuthority(
            {"demo": self.project}, state_dir=authority_dir,
        )
        authority.initialize(POLICY_A, {})

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_state)}):
            self.assert_code(
                "WORKSPACE_RELEASE_UNLISTED",
                lambda: verify_launch(release),
            )

        authority.advance(POLICY_B, {digest: 1})
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_state)}):
            admitted = verify_launch(release)
        self.assertTrue(admitted["authority_active"])
        self.assertEqual(admitted["authority_dir"], str(authority_dir))

    def test_launcher_rejects_authority_path_split_from_child_environment(self):
        launcher = load_launcher()
        xdg_state = self.root / "custom-state"
        different = self.root / "different-authority"

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_state)}), \
                mock.patch.object(launcher, "verify_launch") as verify, \
                mock.patch.object(launcher.os, "execv") as execute:
            self.assert_code(
                "WORKSPACE_AUTHORITY_ENV_MISMATCH",
                lambda: launcher.main([
                    "--authority-dir", str(different),
                    "--release", str(self.releases / ("0" * 20)),
                    "--interpreter", "/usr/bin/python3",
                ]),
            )

        verify.assert_not_called()
        execute.assert_not_called()

    def test_launcher_executes_isolated_worker_with_matching_xdg_authority(self):
        launcher = load_launcher()
        xdg_state = self.root / "custom-state"
        canonical = (xdg_state / "chatgpt-coding-mcp" / "workspace-authority")
        release = self.releases / ("0" * 20)

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_state)}), \
                mock.patch.object(launcher, "verify_launch") as verify, \
                mock.patch.object(launcher, "_interpreter", return_value="/usr/bin/python3"), \
                mock.patch.object(launcher.os, "execv") as execute:
            result = launcher.main([
                "--authority-dir", str(canonical),
                "--release", str(release),
                "--interpreter", "/usr/bin/python3", "--isolated",
                "--entrypoint", "runtime_rollout_worker.py", "--", "job-1",
            ])

        self.assertEqual(result, 127)
        verify.assert_called_once_with(
            release.absolute(), canonical.absolute(), enforcement_required=True,
        )
        execute.assert_called_once_with("/usr/bin/python3", [
            "/usr/bin/python3", "-I", "-c", mock.ANY,
            str(release.absolute()),
            str(release.absolute() / "runtime_rollout_worker.py"), "job-1",
        ])

    def test_isolated_launcher_uses_only_admitted_release_for_sibling_imports(self):
        server = b"""\
import json
import os
import sys
from jobs import SIBLING_VALUE
print(json.dumps({
    "argv": sys.argv,
    "prefix": sys.prefix,
    "sibling": SIBLING_VALUE,
    "sys_path": sys.path,
    "xdg_state_home": os.environ.get("XDG_STATE_HOME"),
}, sort_keys=True))
"""
        release, digest = self.make_release(
            "isolated", overrides={"server.py": server,
                                   "jobs.py": b'SIBLING_VALUE = "verified"\n'},
        )
        xdg_state = self.root / "isolated-state"
        authority_dir = xdg_state / "chatgpt-coding-mcp" / "workspace-authority"
        WorkspaceAuthority(
            {"demo": self.project}, state_dir=authority_dir,
        ).initialize(POLICY_A, {digest: 1})
        rogue = self.root / "rogue-pythonpath"
        rogue.mkdir()
        environment = dict(os.environ, XDG_STATE_HOME=str(xdg_state),
                           PYTHONPATH=str(rogue))

        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "workspace-launcher.py"),
             "--authority-dir", str(authority_dir), "--release", str(release),
             "--interpreter", sys.executable, "--isolated", "--", "one", "two"],
            cwd=self.root, env=environment, text=True, capture_output=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["sibling"], "verified")
        self.assertEqual(result["argv"], [str(release / "server.py"), "one", "two"])
        self.assertEqual(Path(result["prefix"]), Path(sys.prefix))
        self.assertEqual(result["sys_path"][0], str(release))
        self.assertNotIn(str(rogue), result["sys_path"])
        self.assertEqual(result["xdg_state_home"], str(xdg_state))

    def test_unlisted_tampered_incomplete_or_replaced_release_is_denied(self):
        compatible, compatible_digest = self.make_release("compatible")
        unlisted, _ = self.make_release("unlisted")
        incomplete, incomplete_digest = self.make_release(
            "incomplete", omit={"workspace_authority.py"},
        )
        self.authority({compatible_digest: 1, incomplete_digest: 1})

        self.assert_code(
            "WORKSPACE_RELEASE_UNLISTED",
            lambda: verify_launch(unlisted, self.authority_dir),
        )
        self.assert_code(
            "WORKSPACE_RELEASE_INCOMPATIBLE",
            lambda: verify_launch(incomplete, self.authority_dir),
        )
        (compatible / "server.py").write_text("tampered", encoding="utf-8")
        self.assert_code(
            "WORKSPACE_RELEASE_TAMPERED",
            lambda: verify_launch(compatible, self.authority_dir),
        )

        (compatible / "server.py").chmod(0o644)
        self.assertEqual(stat.S_IMODE((compatible / "server.py").stat().st_mode), 0o644)
        self.assert_code(
            "UNSAFE_WORKSPACE_RELEASE",
            lambda: verify_launch(compatible, self.authority_dir),
        )

    def test_epoch_transition_prohibits_old_runtime_even_when_policy_rolls_back(self):
        old, old_digest = self.make_release("old")
        new, new_digest = self.make_release("new")
        authority = self.authority({old_digest: 1})
        self.assertEqual(verify_launch(old, self.authority_dir)["release_abi"], 1)

        advanced = authority.advance(POLICY_B, {new_digest: 2})
        returned = authority.advance(POLICY_A, {new_digest: 2})

        self.assertEqual((advanced["authority_epoch"], returned["authority_epoch"]), (2, 3))
        self.assert_code(
            "WORKSPACE_RELEASE_UNLISTED",
            lambda: verify_launch(old, self.authority_dir),
        )
        self.assertEqual(verify_launch(new, self.authority_dir)["binding"], returned)

    def test_corrupt_missing_or_internally_stale_authority_fails_closed(self):
        release, digest = self.make_release("compatible")
        authority = self.authority({digest: 1})
        request = authority.request(
            "demo", str(release / "server.py"), ["read"], 1800,
            "fixture", POLICY_A,
        )
        state_path = self.authority_dir / "authority.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["requests"][request["request_id"]]["authority_epoch"] = 0
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        self.assert_code(
            "WORKSPACE_AUTHORITY_INVALID",
            lambda: verify_launch(release, self.authority_dir),
        )

        state_path.write_text("not-json", encoding="utf-8")
        self.assert_code(
            "WORKSPACE_AUTHORITY_INVALID",
            lambda: verify_launch(release, self.authority_dir),
        )
        state_path.unlink()
        self.assert_code(
            "WORKSPACE_AUTHORITY_MISSING",
            lambda: verify_launch(release, self.authority_dir),
        )

    def test_controller_admits_long_pending_request_and_grant(self):
        release, digest = self.make_release("long-authority")
        authority = self.authority({digest: 1})
        request = authority.request(
            "demo", str(release / "server.py"), ["read"], 2592000,
            "long-running review", POLICY_A,
        )
        authority.confirm(
            "demo", request["request_id"], request["_confirmation"], POLICY_A,
        )

        self.assertEqual(verify_launch(release, self.authority_dir)["release_abi"], 1)

    def test_release_directory_name_must_match_full_manifest_digest_prefix(self):
        release, digest = self.make_release("compatible")
        self.authority({digest: 1})
        replaced = self.releases / ("f" * 20)
        release.rename(replaced)

        self.assert_code(
            "WORKSPACE_RELEASE_TAMPERED",
            lambda: verify_launch(replaced, self.authority_dir),
        )

    def test_controller_install_is_private_and_outside_release_snapshots(self):
        destination = self.root / "stable-controller"

        with mock.patch("os.umask") as changed_umask:
            # Installation does not depend on or alter the ambient umask.
            result = install_controller(
                destination, ROOT, projects={"demo": self.project},
            )
        changed_umask.assert_not_called()
        self.assertEqual(result["status"], "controller_installed")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(set(result["manifest"]), {
            "workspace_control.py", "workspace-launcher.py",
        })
        for name in (*result["manifest"], "controller-manifest.json", "install.lock"):
            self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o600)
        self.assert_code(
            "UNSAFE_CONTROLLER_DESTINATION",
            lambda: install_controller(
                self.releases / "controller", ROOT,
                projects={"demo": self.project},
            ),
        )

    def test_controller_install_rejects_any_location_writable_by_project_grants(self):
        inside_project = self.project / "controller"
        ancestor_of_project = self.project.parent

        self.assert_code(
            "UNSAFE_CONTROLLER_DESTINATION",
            lambda: install_controller(
                inside_project, ROOT, projects={"demo": self.project},
            ),
        )
        self.assertFalse(inside_project.exists())
        self.assert_code(
            "UNSAFE_CONTROLLER_DESTINATION",
            lambda: install_controller(
                ancestor_of_project, ROOT, projects={"demo": self.project},
            ),
        )

    def test_controller_install_rejects_project_writable_canonical_config(self):
        config_home = self.project / "config"
        config_dir = config_home / "chatgpt-coding-mcp"
        config_dir.mkdir(parents=True)
        (config_dir / "projects.json").write_text(json.dumps({
            "projects": {"demo": str(self.project)},
        }), encoding="utf-8")
        destination = self.root / "stable-controller"

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            self.assert_code(
                "UNSAFE_PROJECTS_CONFIGURATION",
                lambda: install_controller(destination, ROOT),
            )
        self.assertFalse(destination.exists())

    def test_activation_cli_requires_externally_verifiable_migration_evidence(self):
        projects = self.root / "projects.json"
        projects.write_text(json.dumps({"demo": str(self.project)}), encoding="utf-8")
        output = io.StringIO()

        self.assert_code(
                "ACTIVATION_EVIDENCE_REQUIRED",
            lambda: main([
                "migrate", "--authority-dir", str(self.authority_dir),
                "--projects-json", str(projects), "--policy-digest", POLICY_A,
                "--maintenance-drained", "--legacy-authority-invalidated",
            ]),
        )
        self.assertFalse(self.authority_dir.exists())
        self.assert_code(
            "ACTIVATION_EVIDENCE_REQUIRED",
            lambda: main([
                "initialize", "--authority-dir", str(self.authority_dir),
                "--projects-json", str(projects), "--policy-digest", POLICY_A,
            ]),
        )
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
