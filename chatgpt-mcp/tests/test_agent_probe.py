"""Disposable staging and fail-closed Codex isolation probe tests."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_bootstrap import build_bootstrap, validate_bootstrap
import agent_probe
from agent_probe import ProbeError, probe_codex_isolation, stage_codex_runtime


PINNED_SOURCE = Path(
    "/home/jhw/.codex/packages/standalone/releases/"
    "0.162.1-x86_64-unknown-linux-musl/bin/codex"
)
PINNED_SHA256 = "628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1"
PINNED_CODE_MODE_HOST_SHA256 = (
    "ec71830690a7c0024916ee0c99fbdc635bc6e747ff665ea4f03a8389bbcb6b30"
)


class AgentProbeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-probe-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        os.chmod(self.root, 0o700)
        self.fake_source = self.root / "source-bootstrap"
        built = build_bootstrap(self.fake_source)
        self.fake_sha256 = built["sha256"]
        self.fake_code_mode_host = self.root / "codex-code-mode-host"
        shutil.copyfile(self.fake_source, self.fake_code_mode_host)
        os.chmod(self.fake_code_mode_host, 0o700)
        self.fake_code_mode_host_sha256 = self.fake_sha256

    def stage_fake_runtime(self):
        runtime = self.root / "runtime"
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256",
                             self.fake_code_mode_host_sha256):
            metadata = stage_codex_runtime(self.fake_source, self.fake_sha256, runtime)
        return runtime, metadata

    def test_stage_copies_only_verified_static_runtime_and_bootstrap(self):
        runtime, metadata = self.stage_fake_runtime()

        self.assertEqual(set(path.name for path in runtime.iterdir()), {
            "codex", "codex-linux-sandbox", "codex-code-mode-host",
            "personal-ops-agent-bootstrap",
        })
        self.assertEqual(metadata["executable"], str(runtime / "codex"))
        self.assertEqual(metadata["helper"], str(runtime / "codex-linux-sandbox"))
        self.assertEqual(metadata["code_mode_helper"],
                         str(runtime / "codex-code-mode-host"))
        self.assertEqual(metadata["bootstrap"], str(runtime / "personal-ops-agent-bootstrap"))
        self.assertEqual(metadata["executable_sha256"], self.fake_sha256)
        self.assertEqual(metadata["code_mode_helper_sha256"],
                         self.fake_code_mode_host_sha256)
        self.assertEqual(validate_bootstrap(runtime / "codex"), self.fake_sha256)
        self.assertEqual(validate_bootstrap(runtime / "codex-linux-sandbox"), self.fake_sha256)
        self.assertEqual(validate_bootstrap(runtime / "codex-code-mode-host"),
                         self.fake_code_mode_host_sha256)
        self.assertEqual(validate_bootstrap(runtime / "personal-ops-agent-bootstrap"),
                         metadata["bootstrap_sha256"])
        self.assertTrue(all((path.stat().st_mode & 0o777) == 0o700
                            for path in runtime.iterdir()))

    def test_stage_rejects_symlink_hash_drift_and_existing_destination(self):
        link = self.root / "source-link"
        link.symlink_to(self.fake_source)
        scenarios = (
            (link, self.fake_sha256, self.root / "runtime-link"),
            (self.fake_source, "0" * 64, self.root / "runtime-hash"),
        )
        for source, digest, destination in scenarios:
            with self.subTest(source=source.name):
                with patch.object(agent_probe, "PINNED_CODEX_SHA256", digest):
                    with self.assertRaises(ProbeError):
                        stage_codex_runtime(source, digest, destination)

        existing = self.root / "existing"
        existing.mkdir(mode=0o700)
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256):
            with self.assertRaises(ProbeError):
                stage_codex_runtime(self.fake_source, self.fake_sha256, existing)

    def test_stage_rejects_missing_symlinked_or_changed_code_mode_helper(self):
        self.fake_code_mode_host.unlink()
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256", self.fake_sha256):
            with self.assertRaises(ProbeError):
                stage_codex_runtime(self.fake_source, self.fake_sha256,
                                    self.root / "runtime-missing-helper")
        self.assertFalse((self.root / "runtime-missing-helper").exists())

        self.fake_code_mode_host.symlink_to(self.fake_source)
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256", self.fake_sha256):
            with self.assertRaises(ProbeError):
                stage_codex_runtime(self.fake_source, self.fake_sha256,
                                    self.root / "runtime-linked-helper")
        self.assertFalse((self.root / "runtime-linked-helper").exists())

        self.fake_code_mode_host.unlink()
        shutil.copyfile(self.fake_source, self.fake_code_mode_host)
        os.chmod(self.fake_code_mode_host, 0o700)
        with self.fake_code_mode_host.open("ab") as stream:
            stream.write(b"drift")
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256", self.fake_sha256):
            with self.assertRaises(ProbeError):
                stage_codex_runtime(self.fake_source, self.fake_sha256,
                                    self.root / "runtime-changed-helper")
        self.assertFalse((self.root / "runtime-changed-helper").exists())

    def test_stage_removes_partial_runtime_when_bootstrap_build_fails(self):
        destination = self.root / "runtime-partial"
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "build_bootstrap",
                             side_effect=ProbeError("BOOTSTRAP_FAILED")):
            with self.assertRaisesRegex(ProbeError, "BOOTSTRAP_FAILED"):
                stage_codex_runtime(self.fake_source, self.fake_sha256, destination)
        self.assertFalse(destination.exists())

    def test_probe_returns_only_safe_assertions_and_fails_closed(self):
        runtime, _ = self.stage_fake_runtime()
        profile = {"executable": str(runtime / "codex"),
                   "executable_sha256": self.fake_sha256,
                   "version": "0.162.1"}
        assertions = {name: True for name in agent_probe.REQUIRED_ASSERTIONS}
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256",
                             self.fake_code_mode_host_sha256), \
                patch.object(agent_probe, "probe_code_mode_host", return_value={
                    name: True for name in agent_probe.CODE_MODE_ASSERTIONS}), \
                patch.object(agent_probe, "_run_sandbox", return_value=assertions):
            report = probe_codex_isolation(profile, self.root / "fixture-pass")
        self.assertEqual(report, {
            "passed": True,
            "version": "0.162.1",
            "executable_sha256": self.fake_sha256,
            "helper_sha256": self.fake_sha256,
            "code_mode_helper_sha256": self.fake_code_mode_host_sha256,
            "assertions": assertions,
        })
        self.assertNotIn("paths", report)
        self.assertNotIn("output", report)

        assertions["manager_bus_denied"] = False
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256",
                             self.fake_code_mode_host_sha256), \
                patch.object(agent_probe, "probe_code_mode_host", return_value={
                    name: True for name in agent_probe.CODE_MODE_ASSERTIONS}), \
                patch.object(agent_probe, "_run_sandbox", return_value=assertions):
            with self.assertRaises(ProbeError):
                probe_codex_isolation(profile, self.root / "fixture-fail")

    def test_probe_rejects_runtime_with_missing_code_mode_helper(self):
        runtime, _ = self.stage_fake_runtime()
        (runtime / "codex-code-mode-host").unlink()
        profile = {
            "executable": str(runtime / "codex"),
            "executable_sha256": self.fake_sha256,
            "version": "0.162.1",
        }
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256",
                             self.fake_code_mode_host_sha256), \
                patch.object(agent_probe, "_run_sandbox",
                             side_effect=AssertionError("sandbox must not start")):
            with self.assertRaises(ProbeError):
                probe_codex_isolation(profile, self.root / "fixture-missing-helper")

    def test_code_mode_transport_failure_stops_before_sandbox_and_receipt(self):
        runtime, _ = self.stage_fake_runtime()
        profile = {"executable": str(runtime / "codex"),
                   "executable_sha256": self.fake_sha256, "version": "0.162.1"}
        with patch.object(agent_probe, "PINNED_CODEX_SHA256", self.fake_sha256), \
                patch.object(agent_probe, "PINNED_CODE_MODE_HOST_SHA256",
                             self.fake_code_mode_host_sha256), \
                patch.object(agent_probe, "probe_code_mode_host",
                             side_effect=agent_probe.CodeModeProbeError("CODE_MODE_TIMEOUT")), \
                patch.object(agent_probe, "_run_sandbox") as sandbox:
            with self.assertRaisesRegex(ProbeError, "CODE_MODE_TIMEOUT"):
                probe_codex_isolation(profile, self.root / "fixture-transport-failure")
            sandbox.assert_not_called()


@unittest.skipUnless(os.environ.get("PERSONAL_OPS_REAL_CODEX_PROBE") == "1",
                     "explicit free real-Codex probe only")
class RealCodexIsolationProbeTest(unittest.TestCase):
    def test_real_pinned_codex_offline_isolation(self):
        with tempfile.TemporaryDirectory(prefix="real-agent-probe-") as folder:
            root = Path(folder)
            os.chmod(root, 0o700)
            metadata = stage_codex_runtime(PINNED_SOURCE, PINNED_SHA256, root / "runtime")
            self.assertEqual(metadata["code_mode_helper_sha256"],
                             PINNED_CODE_MODE_HOST_SHA256)
            completed = subprocess.run(
                [metadata["code_mode_helper"], "--help"], check=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=10,
            )
            self.assertEqual(completed.returncode, 0)
            profile = {
                "executable": metadata["executable"],
                "executable_sha256": PINNED_SHA256,
                "version": "0.162.1",
            }
            report = probe_codex_isolation(profile, root / "fixture")
            self.assertTrue(report["passed"])
            self.assertTrue(all(report["assertions"].values()))


if __name__ == "__main__":
    unittest.main()
