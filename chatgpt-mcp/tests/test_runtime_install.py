import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
spec = importlib.util.spec_from_file_location("install_runtime", MODULE_DIR / "install-runtime.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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
        for name in ('codex_hooks.py', 'codex_observer.py', 'codex_hook_report.py',
                     'claude_hooks.py', 'claude_hook_report.py'):
            self.assertTrue((release / name).is_file(), name)
        self.assertEqual(result['file_count'], 26)
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
