"""Host-only profile setup plans. All probes and the paid smoke are observed fakes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_probe import PINNED_CODE_MODE_HOST_SHA256, REQUIRED_ASSERTIONS  # noqa: E402
from agent_profile_setup import AgentProfileSetup  # noqa: E402
from agent_proofs import SMOKE_ASSERTIONS, SUPERVISION_ASSERTIONS  # noqa: E402
from host_runtime_config import HostRuntimeError, PROJECT, save_config_cas  # noqa: E402
from tests.host_operation_fixtures import write_registered_fixture  # noqa: E402


class SetupFixture:
    def __init__(self, root: Path):
        self.root = root
        fixture = write_registered_fixture(root)
        self.config = fixture["config"]
        self.projects = fixture["projects"]
        self.source = root / "codex-source"
        self.source.write_bytes(b"pinned codex fixture\n")
        self.source.chmod(0o700)
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.calls = []
        self.receipts = []

    def stage(self, source, expected_sha, destination):
        self.calls.append(("stage", str(source), expected_sha, str(destination)))
        destination.mkdir(mode=0o700)
        for name in ("codex", "codex-linux-sandbox", "codex-code-mode-host",
                     "personal-ops-agent-bootstrap"):
            path = destination / name
            path.write_bytes(b"fixture")
            path.chmod(0o700)
        return {
            "executable": str(destination / "codex"),
            "helper": str(destination / "codex-linux-sandbox"),
            "code_mode_helper": str(destination / "codex-code-mode-host"),
            "code_mode_helper_sha256": PINNED_CODE_MODE_HOST_SHA256,
            "bootstrap": str(destination / "personal-ops-agent-bootstrap"),
            "version": "0.162.1", "executable_sha256": expected_sha,
            "helper_sha256": expected_sha, "bootstrap_sha256": "1" * 64,
            "bootstrap_source_sha256": "2" * 64,
        }

    def isolation(self, profile, fixture_root):
        self.calls.append(("isolation", profile["model"], str(fixture_root)))
        fixture_root.mkdir(mode=0o700)
        return {"passed": True, "version": "0.162.1",
                "executable_sha256": self.source_sha,
                "helper_sha256": self.source_sha,
                "assertions": {name: True for name in REQUIRED_ASSERTIONS}}

    async def supervision(self, fixture_root):
        self.calls.append(("supervision", str(fixture_root)))
        fixture_root.mkdir(mode=0o700)
        return {"passed": True, **{name: True for name in SUPERVISION_ASSERTIONS}}

    def record(self, root, profile, kind, evidence):
        self.calls.append(("receipt", kind, evidence))
        receipt = ("a" if kind == "isolation" else "b") * 64
        self.receipts.append((receipt, kind, dict(profile)))
        return receipt

    @staticmethod
    def verify(root, receipt, profile, kind):
        return receipt == ("a" if kind == "isolation" else "b") * 64

    async def smoke(self, profile, workspace, approval_path, *, plan_digest):
        approval = json.loads(approval_path.read_bytes())
        assert approval["plan_digest"] == plan_digest
        assert approval["profile_digest"]
        assert approval["workspace"] == str(workspace)
        approval_path.unlink()
        return {name: True for name in SMOKE_ASSERTIONS}

    def setup(self, smoke=None, *, trusted_runtime=True):
        return AgentProfileSetup(
            self.projects, config_path=self.config,
            state_dir=self.root / "profile-setup",
            runtime_root=self.root / "agent-runtimes",
            receipts_dir=self.root / "agent-probes",
            source_executable=self.source,
            stage_runtime=self.stage,
            isolation_probe=self.isolation,
            supervisor_probe=self.supervision,
            receipt_recorder=self.record,
            receipt_verifier=self.verify,
            smoke_runner=self.smoke if smoke is None else smoke,
            smoke_objective="Return the fixed fixture result only.",
            contract_digest=lambda: "c" * 64,
            trusted_runtime=trusted_runtime,
        )


class AgentProfileSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-profile-setup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = SetupFixture(self.root)
        self.pin = mock.patch("agent_profile_setup.PINNED_CODEX_SHA256",
                              self.fixture.source_sha)
        self.pin.start()
        self.addCleanup(self.pin.stop)

    def prepare(self, setup=None, **updates):
        values = {
            "profile_id": "codex-review", "project": PROJECT,
            "model": "gpt-5.6", "reasoning_effort": "high",
            "allowed_paths": ["server", "contracts"], "host_capabilities": [],
        }
        values.update(updates)
        return (setup or self.fixture.setup()).prepare(**values)

    def test_prepare_freezes_explicit_review_profile_without_probe_or_config_change(self):
        before = self.fixture.config.read_bytes()
        plan = self.prepare()
        self.assertEqual(self.fixture.config.read_bytes(), before)
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(plan["profile"]["apply_policy"], "review_only")
        self.assertEqual(plan["profile"]["host_capabilities"], [])
        self.assertEqual(plan["profile"]["model"], "gpt-5.6")
        self.assertEqual(plan["source_executable_sha256"], self.fixture.source_sha)
        self.assertEqual(len(plan["plan_id"]), 32)

    def test_prepare_rejects_any_host_capability(self):
        with self.assertRaisesRegex(HostRuntimeError, "UNSUPPORTED_AGENT_CAPABILITY"):
            self.prepare(host_capabilities=[{
                "target": "tarororo-test", "actions": ["docker.inspect"]}])
        self.assertEqual(self.fixture.calls, [])

    def test_prepare_rejects_symlink_in_allowed_path_parent(self):
        project_root = Path(self.fixture.projects[PROJECT])
        outside = self.root / "outside-allowed-path"
        (outside / "src").mkdir(parents=True)
        (project_root / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(HostRuntimeError, "UNSAFE_AGENT_PATH"):
            self.prepare(allowed_paths=["alias/src"])
        self.assertEqual(self.fixture.calls, [])

    def test_config_drift_blocks_before_runtime_staging(self):
        setup = self.fixture.setup()
        plan = self.prepare(setup)
        payload = json.loads(self.fixture.config.read_bytes())
        payload["enabled"] = False
        self.fixture.config.write_text(json.dumps(payload))
        self.fixture.config.chmod(0o600)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_CONFIG_CHANGED"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=True)
        self.assertEqual(self.fixture.calls, [])

    def test_existing_runtime_destination_blocks_before_staging(self):
        setup = self.fixture.setup()
        plan = self.prepare(setup)
        destination = Path(plan["profile"]["executable"]).parent
        destination.mkdir(mode=0o700)
        with self.assertRaisesRegex(HostRuntimeError, "CODEX_RUNTIME_EXISTS"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=True)
        self.assertEqual(self.fixture.calls, [])

    def test_mutable_checkout_cannot_apply_prepared_plan(self):
        setup = self.fixture.setup(trusted_runtime=False)
        plan = self.prepare(setup)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_RUNTIME_NOT_INSTALLED"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=True)
        self.assertEqual(self.fixture.calls, [])

    def test_apply_preserves_envelope_and_enables_only_after_consumed_smoke(self):
        initial = json.loads(self.fixture.config.read_bytes())
        initial["agent_profiles"]["existing-review"] = {
            "project": PROJECT, "provider": "codex",
            "executable": "/opt/personal-ops/existing/codex",
            "executable_sha256": "d" * 64, "version": "0.161.0",
            "model": "gpt-5.6", "reasoning_effort": "medium",
            "allowed_paths": ["server"], "host_capabilities": [],
            "apply_policy": "review_only", "isolation_receipt": None,
            "smoke_receipt": None, "enabled": False,
        }
        self.fixture.config.write_text(json.dumps(initial))
        self.fixture.config.chmod(0o600)
        setup = self.fixture.setup()
        before = json.loads(self.fixture.config.read_bytes())
        plan = self.prepare(setup)
        result = setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                             approve_model_smoke=True)
        after = json.loads(self.fixture.config.read_bytes())
        self.assertEqual(after["legacy"], before["legacy"])
        self.assertEqual(after["targets"], before["targets"])
        self.assertEqual(set(after["agent_profiles"]), {"existing-review", "codex-review"})
        self.assertEqual(after["agent_profiles"]["existing-review"],
                         before["agent_profiles"]["existing-review"])
        profile = after["agent_profiles"]["codex-review"]
        self.assertTrue(profile["enabled"])
        self.assertEqual(profile["isolation_receipt"], "a" * 64)
        self.assertEqual(profile["smoke_receipt"], "b" * 64)
        self.assertEqual(result["status"], "profile_enabled")
        self.assertFalse(list((self.root / "profile-setup" / "smoke-approvals").rglob(
            "smoke-approval.json")))

    def test_missing_or_wrong_code_mode_metadata_blocks_before_probes_and_model(self):
        for index, (field, value) in enumerate((("code_mode_helper", None),
                             ("code_mode_helper", "/untrusted/code-mode-host"),
                             ("code_mode_helper_sha256", "0" * 64))):
            with self.subTest(field=field, value=value):
                case_root = self.root / str(index)
                case_root.mkdir()
                fixture = SetupFixture(case_root)
                setup = fixture.setup()
                plan = self.prepare(setup)
                original = fixture.config.read_bytes()
                def changed_stage(*args):
                    result = fixture.stage(*args)
                    result[field] = value
                    return result
                setup.stage_runtime = changed_stage
                with self.assertRaisesRegex(HostRuntimeError, "CODEX_RUNTIME_CHANGED"):
                    setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                                approve_model_smoke=True)
                self.assertEqual(fixture.config.read_bytes(), original)
                self.assertFalse(any(call[0] == "isolation" for call in fixture.calls))
                self.assertFalse(list(setup.approvals_dir.rglob("smoke-approval.json")))

    def test_no_smoke_approval_or_failed_smoke_never_activates_profile(self):
        setup = self.fixture.setup()
        original = self.fixture.config.read_bytes()
        plan = self.prepare(setup)
        with self.assertRaisesRegex(HostRuntimeError, "MODEL_SMOKE_APPROVAL_REQUIRED"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=False)
        self.assertEqual(self.fixture.calls, [])

        async def failed_smoke(profile, workspace, approval_path, *, plan_digest):
            approval_path.unlink()
            raise HostRuntimeError("MODEL_SMOKE_FAILED")

        failed = self.fixture.setup(smoke=failed_smoke)
        failed_plan = self.prepare(failed, profile_id="codex-failed")
        with self.assertRaisesRegex(HostRuntimeError, "MODEL_SMOKE_FAILED"):
            failed.apply(failed_plan["plan_id"], confirmation=failed_plan["plan_id"],
                         approve_model_smoke=True)
        self.assertEqual(self.fixture.config.read_bytes(), original)
        logs = list((self.root / "profile-setup" / "failures").glob("*.json"))
        self.assertTrue(logs)
        self.assertTrue(all((path.stat().st_mode & 0o777) == 0o600 for path in logs))

    def test_unconsumed_smoke_approval_never_activates_profile(self):
        async def unconsumed_smoke(profile, workspace, approval_path, *, plan_digest):
            return {name: True for name in SMOKE_ASSERTIONS}

        setup = self.fixture.setup(smoke=unconsumed_smoke)
        original = self.fixture.config.read_bytes()
        plan = self.prepare(setup)
        with self.assertRaisesRegex(HostRuntimeError, "SMOKE_APPROVAL_NOT_CONSUMED"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=True)
        self.assertEqual(self.fixture.config.read_bytes(), original)
        self.assertFalse(list((self.root / "profile-setup" / "smoke-approvals").rglob(
            "smoke-approval.json")))

    def test_drift_during_smoke_does_not_overwrite_new_config(self):
        setup = None

        async def drifting_smoke(profile, workspace, approval_path, *, plan_digest):
            approval_path.unlink()
            payload = json.loads(self.fixture.config.read_bytes())
            payload["enabled"] = False
            self.fixture.config.write_text(json.dumps(payload))
            self.fixture.config.chmod(0o600)
            return {name: True for name in SMOKE_ASSERTIONS}

        setup = self.fixture.setup(smoke=drifting_smoke)
        plan = self.prepare(setup)
        with self.assertRaisesRegex(HostRuntimeError, "HOST_CONFIG_CHANGED"):
            setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                        approve_model_smoke=True)
        self.assertFalse(json.loads(self.fixture.config.read_bytes())["enabled"])

    def test_cas_rejects_legitimate_update_in_final_write_window(self):
        setup = self.fixture.setup()
        original = json.loads(self.fixture.config.read_bytes())
        plan = self.prepare(setup)

        def concurrent_update(path, candidate, *, expected_sha256):
            updated = dict(original)
            updated["enabled"] = False
            save_config_cas(path, updated, expected_sha256=expected_sha256)
            save_config_cas(path, candidate, expected_sha256=expected_sha256)

        with mock.patch("agent_profile_setup.save_config_cas", concurrent_update):
            with self.assertRaisesRegex(HostRuntimeError, "HOST_CONFIG_CHANGED"):
                setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                            approve_model_smoke=True)
        current = json.loads(self.fixture.config.read_bytes())
        self.assertFalse(current["enabled"])
        self.assertEqual(current["agent_profiles"], original["agent_profiles"])

    def test_plan_expiring_during_probes_never_activates_profile(self):
        setup = self.fixture.setup()
        plan = self.prepare(setup)
        original = self.fixture.config.read_bytes()
        real_now = datetime.now(timezone.utc)

        class ExpiringClock:
            calls = 0

            @classmethod
            def now(cls, zone):
                cls.calls += 1
                return real_now if cls.calls < 3 else real_now + timedelta(hours=1)

        with mock.patch("agent_profile_setup.datetime", ExpiringClock):
            with self.assertRaisesRegex(HostRuntimeError, "AGENT_PROFILE_PLAN_EXPIRED"):
                setup.apply(plan["plan_id"], confirmation=plan["plan_id"],
                            approve_model_smoke=True)
        self.assertEqual(self.fixture.config.read_bytes(), original)

    def test_host_cli_fails_closed_without_exact_command_and_smoke_flag(self):
        script = MODULE_DIR / "configure-agent-profile.py"
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        for arguments in ([], ["apply", "--plan-id", "a" * 32,
                               "--confirmation", "a" * 32]):
            result = subprocess.run(
                [sys.executable, str(script), *arguments], text=True,
                capture_output=True, timeout=10, check=False, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.strip(), "AGENT_PROFILE_SETUP_DENIED")

    def test_invalid_plan_id_cannot_escape_failure_log_directory(self):
        setup = self.fixture.setup()
        with self.assertRaisesRegex(HostRuntimeError, "INVALID_AGENT_PROFILE_PLAN"):
            setup.apply("../escape", confirmation="../escape", approve_model_smoke=True)
        self.assertFalse(list((self.root / "profile-setup").glob("escape-*.json")))
        logs = list((self.root / "profile-setup" / "failures").glob("invalid-*.json"))
        self.assertEqual(len(logs), 1)


if __name__ == "__main__":
    unittest.main()
