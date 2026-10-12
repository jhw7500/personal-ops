import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "runtime_key", Path(__file__).resolve().parents[1] / "save-runtime-key.py")
KEY_WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(KEY_WRITER)


class RuntimeKeyTest(unittest.TestCase):
    def test_private_file_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous_umask = os.umask(0)
            try:
                with patch.object(KEY_WRITER.Path, "home", return_value=root), \
                     patch.object(KEY_WRITER.getpass, "getpass", return_value="sk-test-placeholder"), \
                     patch("sys.stdout", new_callable=io.StringIO) as output:
                    KEY_WRITER.main()
                    self.assertNotIn("sk-test-placeholder", output.getvalue())
            finally:
                os.umask(previous_umask)
            target = root / ".config/chatgpt-coding-mcp/runtime-key"
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_uid, os.getuid())
            self.assertEqual(target.read_text(), "sk-test-placeholder\n")

    def test_existing_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / "outside"
            outside.write_text("unchanged")
            target = root / ".config/chatgpt-coding-mcp/runtime-key"
            target.parent.mkdir(parents=True)
            target.symlink_to(outside)
            with patch.object(KEY_WRITER.Path, "home", return_value=root), \
                 patch.object(KEY_WRITER.getpass, "getpass", return_value="sk-test-placeholder"):
                with self.assertRaises(OSError):
                    KEY_WRITER.main()
            self.assertEqual(outside.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
