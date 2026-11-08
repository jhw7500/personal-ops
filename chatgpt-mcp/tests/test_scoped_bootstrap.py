"""Preparation is a review artifact, never an alternate source executor."""
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "prepare-scoped-bootstrap.py"
DENIED = ("host-command-approval.html", "host_command_tools.py",
          "tests/test_host_command_approval_ui.mjs", "tests/test_host_command_tools.py")
DEFER = ('"list_host_command_profiles", "prepare_host_command",',
         '"list_host_command_profiles", "inspect_host_command_blockers", "prepare_host_command",')


def package_bytes(*, extra=None, duplicate=False, manifest_hash=None, special_member=False,
                  unresolved_test_imports=False):
    transforms = {
        "chatgpt-mcp/runtime_rollout_web.py": [("# web anchor\n", "# scoped web\n")],
        "chatgpt-mcp/install-runtime.py": [("# installer anchor\n", "# scoped installer\n")],
        "chatgpt-mcp/rollout-host-runtime.py": [("# runner anchor\n", "# scoped runner\n"), DEFER],
    }
    new = {
        "chatgpt-mcp/runtime_rollout_scoped_source.py": "runtime_rollout_scoped_source.py",
        "chatgpt-mcp/tests/test_runtime_rollout_scoped_source.py": "tests/test_runtime_rollout_scoped_source.py",
    }
    files = {name: b"# fixture\n" for name in (
        "owner_scoped_delivery.py", "register_owner_scoped_delivery.py",
        "runtime_rollout_scoped_source.py", "scoped-source-release-v1.json",
        "README.md", "INTEGRATION.md", "tests/test_scoped_delivery.py",
        "tests/test_runtime_rollout_scoped_source.py", "tests/test_make_official_rollout_patch.py")}
    files["runtime_rollout_scoped_source.py"] = (
        'import json\n\nclass HostRuntimeError(RuntimeError):\n    pass\n\n'
        'def select_policy(path, project_root):\n'
        '    if not path.exists() and not path.is_symlink():\n'
        '        return project_root, "project", "0" * 64\n'
        '    return "policy"\n\n'
        'def parse_policy(data):\n    try:\n        return json.loads(data)\n'
        '    except (ValueError, UnicodeError, TypeError) as exc:\n'
        '        raise ValueError("SCOPED_POLICY_INVALID") from exc\n').encode()
    # If preparation imports or executes this generator, the fixture fails.
    files["make_official_rollout_patch.py"] = (
        "raise RuntimeError('ZIP CODE MUST NOT EXECUTE')\nROOT_FILES = " + repr(transforms)
        + "\nNEW_FILES = " + repr(new) + "\n").encode()
    if unresolved_test_imports:
        files["tests/test_runtime_rollout_scoped_source.py"] = (
            b"import owner_scoped_delivery\nfrom tests import test_scoped_delivery\n")
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if manifest_hash:
        manifest["runtime_rollout_scoped_source.py"] = manifest_hash
    files["INTEGRATION_MANIFEST.json"] = json.dumps({
        "package": "personal_ops_runtime_rollout_scoped_integration_v1",
        "status": "SOURCE_ONLY_NOT_INSTALLED", "files": manifest}).encode()
    if extra:
        files[extra] = b"unsafe"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files.items():
            if special_member and name == "runtime_rollout_scoped_source.py":
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFIFO | 0o600) << 16
                archive.writestr(entry, data)
            else:
                archive.writestr(name, data)
        if duplicate:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("README.md", b"duplicate")
    return stream.getvalue()


class BootstrapPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        mcp = self.source / "chatgpt-mcp"
        (mcp / "tests").mkdir(parents=True)
        (self.source / "work-dashboard").mkdir()
        from runtime_rollout_web import CONTROL
        names = set(CONTROL) | {"server.py", "runtime_rollout_worker.py", "runtime_rollout_tools.py"}
        names.update("fixture_%02d.py" % i for i in range(45))
        names.update(("tests/check_host_access_approval_ui.py", "tests/check_schema014_approval_ui.py",
                      "tests/check_host_command_approval_ui.py", "tests/test_workspace_approval_ui.mjs"))
        names.update(DENIED)
        for name in names:
            (mcp / name).write_text("# fixture\n")
        (mcp / "runtime_rollout_web.py").write_text("# web anchor\n")
        (mcp / "install-runtime.py").write_text(
            '# installer anchor\nFILES = ("server.py", "runtime_rollout_web.py", '
            '"runtime_rollout_worker.py", "runtime_rollout_tools.py",)\nDASHBOARD_FILES = ("model.js",)\n')
        (mcp / "rollout-host-runtime.py").write_text("# runner anchor\nTOOLS = (" + DEFER[0] + ")\n")
        (self.source / "work-dashboard/model.js").write_text("// fixture\n")
        self.git("init", "-q")
        self.commit()
        self.package = self.base / "approved.zip"
        self.package.write_bytes(package_bytes())
        self.output = self.base / "review"
        self.native_test = self.base / "native-scoped-test.py"
        self.native_test.write_text(
            "import unittest\n\nclass NativeScopedTest(unittest.TestCase):\n"
            "    def test_repository_native_verification(self):\n"
            "        self.assertEqual(1 + 1, 2)\n")
        self.native_test_sha256 = hashlib.sha256(self.native_test.read_bytes()).hexdigest()

    def git(self, *args):
        return subprocess.check_output([
            "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-C", str(self.source), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def prepare(self, **kwargs):
        # Only the in-process fixture trust constant changes; CLI has no hash override.
        module = importlib.import_module("scoped_bootstrap")
        digest = hashlib.sha256(self.package.read_bytes()).hexdigest()
        with patch.object(module, "APPROVED_PACKAGE_SHA256", digest), \
                patch.object(module, "NATIVE_BOOTSTRAP_TEST", self.native_test, create=True), \
                patch.object(module, "NATIVE_BOOTSTRAP_TEST_SHA256", self.native_test_sha256,
                             create=True):
            return module.prepare(self.package, self.source, self.output, **kwargs)

    def rejected(self, code):
        try:
            self.prepare()
        except ValueError as error:
            self.assertEqual(str(error), code)
        else:
            self.fail("unsafe preparation was accepted")
        self.assertFalse(self.output.exists())

    def test_missing_package_cli_fails_with_bounded_code_and_no_output(self):
        result = subprocess.run([sys.executable, "-B", str(ENTRY), "prepare", "--package",
                                 str(self.base / "missing.zip"), "--source-root", str(self.source),
                                 "--output", str(self.output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr.strip(), "FAILED_CLOSED: PACKAGE_UNSAFE")
        self.assertFalse(self.output.exists())

    def test_prepared_patch_can_be_reviewed_without_modifying_source_or_authority(self):
        before = self.git("status", "--porcelain=v1")
        source_files = {p: (self.source / "chatgpt-mcp" / p).read_bytes() for p in DENIED}
        with patch.dict(os.environ, {"HOME": str(self.base / "uncreated-owner-home")}):
            result = self.prepare()
        self.assertEqual(result["status"], "PREPARED_NOT_INSTALLED")
        self.assertFalse(result["baseline_eligible"])
        manifest = json.loads((self.output / "preparation.json").read_text())
        self.assertEqual(manifest, result)
        self.assertEqual(len(manifest["affected_paths"]), 5)
        self.assertEqual(manifest["deferred_candidate_paths"], ["chatgpt-mcp/" + p for p in DENIED])
        patch_data = (self.output / "bootstrap.patch").read_bytes()
        self.assertEqual(manifest["patch_sha256"], hashlib.sha256(patch_data).hexdigest())
        self.assertNotIn(b"inspect_host_command_blockers", patch_data)
        self.assertEqual(before, self.git("status", "--porcelain=v1"))
        for p, content in source_files.items():
            self.assertEqual((self.source / "chatgpt-mcp" / p).read_bytes(), content)
        self.assertFalse((self.base / "uncreated-owner-home").exists())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for name in ("preparation.json", "bootstrap.patch"):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), 0o600)
        # An independent consumer can parse/apply the text only in this disposable fixture.
        self.git("apply", "--check", str(self.output / "bootstrap.patch"))
        self.git("apply", str(self.output / "bootstrap.patch"))
        adapter = self.source / "chatgpt-mcp/runtime_rollout_scoped_source.py"
        self.assertEqual(hashlib.sha256(adapter.read_bytes()).hexdigest(),
                         result["prepared_file_sha256"]["chatgpt-mcp/runtime_rollout_scoped_source.py"])
        self.assertIn("# scoped web", (self.source / "chatgpt-mcp/runtime_rollout_web.py").read_text())

    def test_wrong_outer_hash_is_not_a_cli_override(self):
        module = importlib.import_module("scoped_bootstrap")
        with self.assertRaisesRegex(ValueError, "^PACKAGE_HASH_MISMATCH$"):
            module.prepare(self.package, self.source, self.output)
        result = subprocess.run([sys.executable, str(ENTRY), "prepare", "--expected-hash", "0" * 64],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_inner_hash_mismatch_rejected(self):
        self.package.write_bytes(package_bytes(manifest_hash="0" * 64))
        self.rejected("PACKAGE_CONTENT_MISMATCH")

    def test_bootstrap_uses_reviewed_native_test_instead_of_unprovided_bundle_helpers(self):
        self.package.write_bytes(package_bytes(unresolved_test_imports=True))
        result = self.prepare()
        self.git("apply", str(self.output / "bootstrap.patch"))
        run = subprocess.run([
            sys.executable, "-B", "-m", "unittest", "discover", "-s", "chatgpt-mcp/tests",
            "-p", "test_runtime_rollout_scoped_source.py"], cwd=self.source,
            capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Ran 1 test", run.stderr)
        origin = result["bootstrap_test"]
        self.assertEqual(origin["origin"], "generator_pinned_source")
        self.assertEqual(origin["sha256"], hashlib.sha256(self.native_test.read_bytes()).hexdigest())
        self.assertNotEqual(origin["sha256"], result["package_files"]["tests/test_runtime_rollout_scoped_source.py"])

    def test_bootstrap_adapter_keeps_policy_errors_bounded_and_never_selects_default(self):
        self.native_test.write_text(
            'import sys, unittest\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n'
            'import runtime_rollout_scoped_source as adapter\n\n'
            'class NativeAdapterTest(unittest.TestCase):\n'
            '    def test_unavailable_policy(self):\n'
            '        class Unavailable:\n'
            '            def exists(self): return False\n'
            '            def is_symlink(self): return False\n'
            '            def lstat(self): raise PermissionError("denied")\n'
            '        with self.assertRaisesRegex(adapter.HostRuntimeError, "SCOPED_POLICY_UNAVAILABLE"):\n'
            '            adapter.select_policy(Unavailable(), "project")\n'
            '    def test_deep_json(self):\n'
            '        data = "[" * 16000 + "0" + "]" * 16000\n'
            '        with self.assertRaisesRegex(ValueError, "SCOPED_POLICY_INVALID"):\n'
            '            adapter.parse_policy(data)\n')
        self.native_test_sha256 = hashlib.sha256(self.native_test.read_bytes()).hexdigest()
        result = self.prepare()
        self.git("apply", str(self.output / "bootstrap.patch"))
        run = subprocess.run([
            sys.executable, "-B", "-m", "unittest", "discover", "-s", "chatgpt-mcp/tests",
            "-p", "test_runtime_rollout_scoped_source.py"], cwd=self.source,
            capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Ran 2 tests", run.stderr)
        self.assertEqual(result["bootstrap_adapter"]["origin"], "package_with_exact_hardening")
        self.assertNotEqual(result["bootstrap_adapter"]["sha256"],
                            result["package_files"]["runtime_rollout_scoped_source.py"])

    def test_invalid_native_test_template_rejected_before_review_is_published(self):
        self.native_test.write_text("def broken(\n")
        self.native_test_sha256 = hashlib.sha256(self.native_test.read_bytes()).hexdigest()
        self.rejected("BOOTSTRAP_TEST_TEMPLATE_INVALID")

    def test_symlink_native_test_template_rejected(self):
        self.native_test.unlink()
        self.native_test.symlink_to(self.package)
        self.rejected("BOOTSTRAP_TEST_TEMPLATE_UNSAFE")

    def test_native_template_with_untrusted_writers_is_rejected(self):
        for mode in (0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                self.output = self.base / ("review-" + oct(mode))
                self.native_test.chmod(mode)
                self.rejected("BOOTSTRAP_TEST_TEMPLATE_UNSAFE")

    def test_native_template_must_match_the_generator_pin(self):
        self.native_test.write_text(self.native_test.read_text().replace("1 + 1, 2", "1 + 1, 3"))
        self.rejected("BOOTSTRAP_TEST_TEMPLATE_HASH_MISMATCH")

    def test_duplicate_entry_rejected(self):
        self.package.write_bytes(package_bytes(duplicate=True))
        self.rejected("PACKAGE_LAYOUT_UNSAFE")

    def test_traversal_entry_rejected_without_extraction(self):
        self.package.write_bytes(package_bytes(extra="../escape.py"))
        self.rejected("PACKAGE_LAYOUT_UNSAFE")
        self.assertFalse((self.base / "escape.py").exists())

    def test_special_archive_member_rejected(self):
        self.package.write_bytes(package_bytes(special_member=True))
        self.rejected("PACKAGE_LAYOUT_UNSAFE")

    def test_private_output_permissions_are_independent_of_umask(self):
        previous = os.umask(0o777)
        try:
            self.prepare()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for name in ("preparation.json", "bootstrap.patch"):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), 0o600)

    def test_dirty_source_rejected(self):
        (self.source / "chatgpt-mcp/server.py").write_text("dirty\n")
        self.rejected("SOURCE_NOT_CLEAN")

    def test_clean_but_incompatible_anchor_rejected(self):
        (self.source / "chatgpt-mcp/runtime_rollout_web.py").write_text("# other version\n")
        self.commit()
        self.rejected("SOURCE_VERSION_MISMATCH")

    def test_existing_adapter_rejected(self):
        (self.source / "chatgpt-mcp/runtime_rollout_scoped_source.py").write_text("existing\n")
        self.commit()
        self.rejected("SOURCE_ALREADY_INTEGRATED")

    def test_source_symlink_rejected(self):
        original = self.source / "chatgpt-mcp/runtime_rollout_web.py"
        original.unlink()
        original.symlink_to("server.py")
        self.commit()
        self.rejected("SOURCE_UNSAFE")

    def test_output_inside_source_rejected_without_creating_directories(self):
        self.output = self.source / "review"
        self.rejected("OUTPUT_UNSAFE")

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("existing")
        with self.assertRaisesRegex(ValueError, "^OUTPUT_EXISTS$"):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "existing")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_symlink_output_parent_rejected(self):
        link = self.base / "parent-link"
        link.symlink_to(self.base, target_is_directory=True)
        self.output = link / "review"
        self.rejected("OUTPUT_UNSAFE")

    def test_group_writable_output_parent_rejected(self):
        parent = self.base / "shared-parent"
        parent.mkdir(mode=0o700)
        parent.chmod(0o770)
        self.output = parent / "review"
        self.rejected("OUTPUT_UNSAFE")

    def test_group_writable_output_ancestor_rejected(self):
        ancestor = self.base / "shared-ancestor"
        parent = ancestor / "private-parent"
        parent.mkdir(mode=0o700, parents=True)
        ancestor.chmod(0o770)
        self.output = parent / "review"
        self.rejected("OUTPUT_UNSAFE")

    def test_replaced_output_parent_is_not_published_as_prepared(self):
        module = importlib.import_module("scoped_bootstrap")
        original = module._build_patch
        parent = self.base / "review-parent"
        parent.mkdir(mode=0o700)
        self.output = parent / "review"

        def replace_parent(*args):
            result = original(*args)
            parent.rename(self.base / "original-parent")
            parent.mkdir(mode=0o700)
            return result

        with patch.object(module, "_build_patch", side_effect=replace_parent):
            self.rejected("OUTPUT_PARENT_CHANGED")
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(list((self.base / "original-parent").iterdir()), [])

    def test_source_change_during_catalog_read_rejected(self):
        module = importlib.import_module("scoped_bootstrap")
        original = module._catalog
        count = 0

        def changing(root):
            nonlocal count
            catalog = original(root)
            count += 1
            if count == 1:
                (root / "chatgpt-mcp/server.py").write_text("changed during read\n")
            return catalog

        with patch.object(module, "_catalog", side_effect=changing):
            self.rejected("SOURCE_CHANGED")


if __name__ == "__main__":
    unittest.main()
