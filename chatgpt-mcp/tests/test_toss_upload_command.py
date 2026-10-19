"""Fixed TEST uploads must use the reviewed bytes, never a rebuilt bundle."""
from __future__ import annotations

from contextlib import redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import toss_upload_command as upload  # noqa: E402

DEPLOYMENT = "01a13881-9f25-7b38-b547-0fbad316f71c"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ait(app="tarororo", deployment=DEPLOYMENT):
    fields = b"\x12" + bytes([len(deployment)]) + deployment.encode()
    fields += b"\x1a" + bytes([len(app)]) + app.encode()
    return b"AITBUNDL" + struct.pack(">IQ", 1, len(fields)) + fields + struct.pack(">Q", 4) + b"ZIP!"


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="toss-upload-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "tarororo.ait"
        self.artifact.write_bytes(ait())
        self.artifact.chmod(0o400)
        self.toolchain = self.root / "toolchain"
        self.toolchain.mkdir(mode=0o700)
        self.entry = self.toolchain / "node_modules" / "@apps-in-toss" / "cli" / "dist" / "index.js"
        self.entry.parent.mkdir(parents=True)
        for directory in self.toolchain.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o700)
        self.entry.write_bytes(b"// reviewed test fixture, never executed\n")
        self.entry.chmod(0o400)
        self.manifest = self.toolchain / "manifest.json"
        self.manifest.write_text(json.dumps({
            "version": 1, "cli_version": "3.2.0",
            "entry": self.entry.relative_to(self.toolchain).as_posix(),
            "files": {self.entry.relative_to(self.toolchain).as_posix(): sha(self.entry.read_bytes())},
        }))
        self.manifest.chmod(0o400)
        self.home = self.root / "home"
        (self.home / ".ait").mkdir(mode=0o700, parents=True)
        self.credentials = self.home / ".ait" / "credentials"
        self.credentials.write_text(json.dumps({"tarororo": "test-secret-never-export"}))
        self.credentials.chmod(0o600)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir(mode=0o700)
        self.node = Path("/usr/bin/printf")
        self.args = [
            "--artifact", str(self.artifact), "--sha256", sha(ait()),
            "--deployment-id", DEPLOYMENT, "--node", str(self.node),
            "--node-sha256", sha(self.node.read_bytes()),
            "--toolchain", str(self.toolchain),
            "--toolchain-sha256", sha(self.manifest.read_bytes()),
            "--credentials", str(self.credentials), "--receipt-dir", str(self.receipts),
        ]

    def invoke(self, action):
        output = io.StringIO()
        with redirect_stdout(output):
            code = upload.main([action, *self.args])
        return code, json.loads(output.getvalue())

    def test_sealed_artifact_survives_original_replacement_and_reopen(self):
        fd = upload.seal_file(self.artifact, sha(ait()))
        self.addCleanup(os.close, fd)
        replacement = self.root / "replacement"
        replacement.write_bytes(b"changed after approval")
        os.replace(replacement, self.artifact)
        self.assertEqual(Path(f"/proc/self/fd/{fd}").read_bytes(), ait())
        self.assertEqual(Path(f"/proc/self/fd/{fd}").read_bytes(), ait())
        required = upload.F_SEAL_WRITE | upload.F_SEAL_GROW | upload.F_SEAL_SHRINK | upload.F_SEAL_SEAL
        self.assertEqual(fcntl.fcntl(fd, upload.F_GET_SEALS) & required, required)
        with self.assertRaises(OSError):
            os.write(fd, b"changed")
        with self.assertRaises(OSError):
            os.ftruncate(fd, 0)

    def test_wrong_hash_symlinks_and_writable_input_are_rejected(self):
        with self.assertRaises(upload.UploadError):
            upload.seal_file(self.artifact, "0" * 64)
        linked = self.root / "linked.ait"
        linked.symlink_to(self.artifact)
        with self.assertRaises(upload.UploadError):
            upload.seal_file(linked, sha(ait()))
        linked_dir = self.root / "linked-dir"
        linked_dir.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(upload.UploadError):
            upload.seal_file(linked_dir / "tarororo.ait", sha(ait()))
        self.artifact.chmod(0o664)
        with self.assertRaises(upload.UploadError):
            upload.seal_file(self.artifact, sha(ait()))

    def test_memfd_fallback_when_python_has_no_memfd_api(self):
        with patch.object(upload.os, "memfd_create", None, create=True), \
                patch.object(upload.os, "MFD_CLOEXEC", None, create=True), \
                patch.object(upload.os, "MFD_ALLOW_SEALING", None, create=True):
            fd = upload.seal_file(self.artifact, sha(ait()))
        self.addCleanup(os.close, fd)
        self.assertEqual(Path(f"/proc/self/fd/{fd}").read_bytes(), ait())
        with self.assertRaises(OSError):
            os.write(fd, b"unsealed")

    def test_metadata_rejects_wrong_app_deployment_and_truncation(self):
        self.assertEqual(upload.bundle_identity(ait()), ("tarororo", DEPLOYMENT))
        for data in (ait(app="other"), ait(deployment="not-a-uuid"), ait()[:24], b"not AIT"):
            with self.subTest(data=data[:30]), self.assertRaises(upload.UploadError):
                upload.bundle_identity(data)

    def test_check_has_no_subprocess_or_credential_read(self):
        self.credentials.unlink()
        with patch.object(upload.subprocess, "run", side_effect=AssertionError("must not run")):
            code, result = self.invoke("check")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "preflight_ok")
        self.assertEqual(result["artifact_sha256"], sha(ait()))
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_toolchain_changes_extra_files_and_links_fail_before_launch(self):
        for kind in ("changed", "extra", "symlink"):
            with self.subTest(kind=kind):
                extra = self.toolchain / "extra.js"
                if kind == "changed":
                    self.entry.chmod(0o600)
                    self.entry.write_bytes(b"changed")
                elif kind == "extra":
                    extra.write_bytes(b"unreviewed")
                else:
                    extra.symlink_to(self.entry)
                with patch.object(upload.subprocess, "run", side_effect=AssertionError("must not run")):
                    code, result = self.invoke("test")
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "failed")
                if extra.exists():
                    extra.unlink()
                self.entry.chmod(0o600)
                self.entry.write_bytes(b"// reviewed test fixture, never executed\n")
                self.entry.chmod(0o400)

    def test_missing_credential_never_falls_back_to_interactive_cli(self):
        self.credentials.unlink()
        with patch.object(upload.subprocess, "run", side_effect=AssertionError("must not run")):
            code, result = self.invoke("test")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")

    def test_upload_uses_same_sealed_bytes_fixed_cli_and_clean_environment(self):
        def fake_run(argv, **kwargs):
            self.assertEqual(argv[1], str(self.entry))
            self.assertEqual(argv[2:5], ["deploy", "--profile", "tarororo"])
            self.assertEqual(argv[-1], "--scheme-only")
            location = Path(argv[argv.index("--location") + 1])
            self.assertEqual(location.suffix, ".ait")
            first = location.read_bytes()
            replacement = self.root / "replacement"
            replacement.write_bytes(b"repo changed during HTTP upload")
            os.replace(replacement, self.artifact)
            self.assertEqual(location.read_bytes(), first)
            self.assertEqual(first, ait())
            self.assertNotIn("NODE_OPTIONS", kwargs["env"])
            self.assertNotIn("HTTPS_PROXY", kwargs["env"])
            self.assertNotIn("test-secret-never-export", repr(argv) + repr(kwargs["env"]))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["cwd"], str(self.toolchain))
            self.assertTrue(kwargs["pass_fds"])
            self.assertEqual(Path(kwargs["executable"]).read_bytes(), self.node.read_bytes())
            kwargs["stdout"].write((upload.private_link(DEPLOYMENT) + "\n").encode())
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(os.environ, {"NODE_OPTIONS": "injected", "HTTPS_PROXY": "https://untrusted"}), \
                patch.object(upload.subprocess, "run", side_effect=fake_run) as run:
            code, result = self.invoke("test")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "cli_reported_complete")
        self.assertFalse(result["remote_digest_verified"])
        receipts = list(self.receipts.iterdir())
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["artifact_sha256"], sha(ait()))
        self.assertNotIn("test-secret-never-export", receipts[0].read_text())
        self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)

    def test_cli_failure_and_unexpected_success_output_do_not_claim_upload(self):
        for returncode in (1, 0):
            def fail_run(argv, **kwargs):
                kwargs["stdout"].write(b"provider failure test-secret-never-export\n")
                return subprocess.CompletedProcess(argv, returncode)

            with self.subTest(returncode=returncode), patch.object(upload.subprocess, "run", side_effect=fail_run):
                code, result = self.invoke("test")
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("test-secret-never-export", json.dumps(result))
        for receipt in self.receipts.iterdir():
            self.assertNotIn("test-secret-never-export", receipt.read_text())
            self.assertNotEqual(json.loads(receipt.read_text())["status"], "cli_reported_complete")

    def test_safe_private_receipt_directory_is_required_before_upload(self):
        self.receipts.chmod(0o777)
        with patch.object(upload.subprocess, "run", side_effect=AssertionError("must not run")):
            code, result = self.invoke("test")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")

    def test_native_child_cannot_fill_stdout_spool_beyond_limit(self):
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                ["/usr/bin/python3", "-I", "-c", "import os; os.write(1, b'x' * 65536); os.write(1, b'x')"],
                stdout=output, stderr=subprocess.DEVNULL, check=False,
                preexec_fn=upload.bound_child_output, timeout=5,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertLessEqual(os.fstat(output.fileno()).st_size, 4096)


if __name__ == "__main__":
    unittest.main()
