"""HostRuntime fail-closed policy, source pinning and fake-Docker deployment tests.

No real Docker, credentials, network, DB or host service is touched.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import io
import json
import os
import sys
import threading
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from host_runtime import (HostRuntime, SOURCE_DIRS, SOURCE_SINGLE, HIGH_RISK_INPUTS,  # noqa: E402
                          source_inventory, verify_reviewed_high_risk_inputs, _default_run)
from host_runtime_config import HostRuntimeError, PROJECT, TARGET, TARGET_CONFIG, load_config  # noqa: E402
import host_runtime_operations  # noqa: E402
from host_runtime_operations import (  # noqa: E402
    HostRuntimeOperations, _digest, host_admission, observed_legacy_operation_blocks_host,
)
from host_runtime_tools import HOST_RUNTIME_TOOL_SPECS  # noqa: E402
from workspace_host import publication_lane  # noqa: E402
from workspace_tools import WorkspaceTools  # noqa: E402


class TTY(io.StringIO):
    def isatty(self):
        return True


class FakeDocker:
    def __init__(self):
        self.api_id = "a" * 64
        self.worker_id = "b" * 64
        self.db_id = "c" * 64
        self.api_image = "sha256:" + "1" * 64
        self.worker_image = "sha256:" + "2" * 64
        self.db_image = "sha256:" + "3" * 64
        self.new_image = "sha256:" + "4" * 64
        self.mode = "disabled"
        self.calls = []
        self.health_fail = False
        self.fail_up = False
        self.fail_build = False
        self.compose_root = None
        self.pinned_image = None
        self.tamper_override = False
        self.migration_ready = False

    def inspect(self, role):
        mapping = {
            "api": (self.api_id, self.api_image), "worker": (self.worker_id, self.worker_image),
            "db": (self.db_id, self.db_image),
        }
        cid, image = mapping[role]
        env = {
            "api": ["APP_ENV=test", "AUTH_MODE=toss",
                    "ALLOWED_ORIGINS=https://tarororo.private-apps.tossmini.com",
                    "READING_MONETIZATION_TEST_MODE=" + self.mode],
            "worker": [],
            "db": ["POSTGRES_DB=tarororo_test"],
        }[role]
        return [{
            "Id": cid, "Image": image, "Name": "/tarororo-test-" + role + "-1",
            "Config": {"Labels": {"com.docker.compose.project": "tarororo-test",
                                  "com.docker.compose.service": role}, "Env": env},
            "State": {"Status": "running",
                      "Health": {"Status": "healthy"}, "StartedAt": "2026-10-12T01:00:00Z"},
            "Mounts": ([{"Type": "volume", "Name": "tarororo-test_database"}]
                       if role == "db" else []),
            "NetworkSettings": {"Ports": (
                {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3200"}]}
                if role == "api" else {}),
                "Networks": {"tarororo-test_default": {}}},
        }]

    def config(self, mode):
        def service(**fields):
            return fields
        result = {
            "services": {
                "api": service(
                    environment={"APP_ENV": "test", "AUTH_MODE": "toss", "HOST": "0.0.0.0",
                                 "PORT": "3000",
                                 "TOSS_MTLS_CERT_PATH": "/run/secrets/toss-client.crt",
                                 "TOSS_MTLS_KEY_PATH": "/run/secrets/toss-client.key",
                                 "ALLOWED_ORIGINS": "https://tarororo.private-apps.tossmini.com",
                                 "READING_MONETIZATION_TEST_MODE": mode},
                    user=f"{os.getuid()}:{os.getgid()}", command=["api"],
                    read_only=True, cap_drop=["ALL"], networks={"default": None},
                    ports=[{"host_ip": "127.0.0.1", "target": 3000, "published": "3200"}],
                    secrets=[{"source": name} for name in
                             ("db-password", "subject-secret", "toss-client.crt", "toss-client.key")]),
                "worker": service(
                    read_only=True, cap_drop=["ALL"],
                    user=f"{os.getuid()}:{os.getgid()}", command=["worker"],
                    networks={"default": None},
                    environment={"AI_PROVIDER": "gemini", "AI_MODEL": "gemini-test",
                                 "AI_GENERATION_TIMEOUT_MS": "60000"},
                    secrets=[{"source": "db-password"}, {"source": "gemini-api-key"}]),
                "db": service(
                    environment={"POSTGRES_DB": "tarororo_test", "POSTGRES_USER": "tarororo",
                                 "POSTGRES_PASSWORD_FILE": "/run/secrets/db-password"},
                    image="postgres:18-alpine@sha256:" + "1" * 64,
                    networks={"default": None, "content-internal": None},
                    volumes=[{"type": "volume", "source": "database",
                              "target": "/var/lib/postgresql"}]),
            },
            "volumes": {"database": {"name": "tarororo-test_database"}},
            "networks": {
                "default": {"name": "tarororo-test_default"},
                "content-internal": {"name": "tarororo-test-content-internal", "internal": True},
            },
        }
        for name in ("api", "worker"):
            service_obj = result["services"][name]
            service_obj["build"] = {"context": str(self.compose_root),
                                    "dockerfile": "deploy/pc-test/Dockerfile"}
            service_obj["image"] = "tarororo-pc-test:local"
        return result

    def run(self, argv, *, cwd=None, mode="disabled", timeout=7, capture=True):
        self.calls.append((tuple(argv), mode, capture))
        if argv[:2] == ["docker", "inspect"]:
            role = next(k for k in ("api", "worker", "db")
                        if argv[2] == f"tarororo-test-{k}-1")
            return json.dumps(self.inspect(role)).encode()
        if argv[:2] == ["docker", "exec"]:
            command = argv[-1]
            if isinstance(command, str) and "json_build_object(" in command:
                value = {"prerequisites_count": 12,
                         "checksum": "missing" if not self.migration_ready else getattr(self, "migration_checksum", "0" * 64),
                         "ad_attempts": self.migration_ready,
                         "section_unlocks": self.migration_ready,
                         "credit_packs": self.migration_ready,
                         "credit_redemptions": self.migration_ready}
                return ("BEGIN" + chr(10) + json.dumps(value) + chr(10) + "COMMIT" + chr(10)).encode()
            if isinstance(command, str) and "INSERT INTO schema_migrations(name,checksum)" in command:
                self.migration_ready = True
                return b""
            return b"BEGIN\nready\nCOMMIT\n"
        if argv[:3] == ["docker", "image", "tag"]:
            if argv[3] != self.new_image:
                raise AssertionError("Unexpected source image ID")
            self.pinned_image = argv[4]
            return b""
        if argv[:3] == ["docker", "image", "inspect"]:
            if argv[3] not in ("tarororo-pc-test:local", self.pinned_image):
                raise AssertionError("Unexpected inspected image")
            return json.dumps([{"Id": self.new_image}]).encode()
        if argv[:2] == ["docker", "compose"]:
            if "config" in argv:
                self.compose_root = Path(argv[argv.index("--file") + 1]).resolve().parents[2]
                result = self.config(mode)
                if argv.count("--file") == 2:
                    override_path = Path(argv[argv.index("--file", argv.index("--file") + 1) + 1])
                    image_json = override_path.read_text().split("image: ", 1)[1].strip()
                    component = "worker" if "\n  worker:\n" in override_path.read_text() else "api"
                    result["services"][component]["image"] = json.loads(image_json)
                    if self.tamper_override:
                        result["services"]["worker"]["environment"]["MALICIOUS"] = "1"
                return json.dumps(result).encode()
            if "build" in argv:
                if self.fail_build:
                    raise HostRuntimeError("HOST_COMMAND_FAILED")
                return b""
            if "up" in argv:
                if self.fail_up:
                    raise HostRuntimeError("HOST_COMMAND_FAILED")
                component = argv[-1]
                if component == "worker":
                    self.worker_id = "e" * 64
                    self.worker_image = self.new_image
                else:
                    self.api_id = "d" * 64
                    self.api_image = self.new_image
                    self.mode = mode
                return b""
        if argv[0] == "/usr/bin/python3":
            raise AssertionError("A project-supplied host script must never execute")
        raise AssertionError("Unexpected command " + repr(argv))


def fixture_repo(root):
    for path in SOURCE_SINGLE:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture " + path + "\n")
    for base in SOURCE_DIRS:
        path = root / base
        path.mkdir(parents=True, exist_ok=True)
    for name in ("001_foundation", "002_interpretation_candidates", "003_reading_flow",
                 "004_subject_identity_rotation", "005_full_tarot_catalog", "006_reading_memos",
                 "007_reading_purchase_verification", "008_toss_login_challenges",
                 "009_account_recovery_lifecycle", "010_commerce_policy_snapshots",
                 "011_checkout_fulfillment", "012_checkout_review_intake"):
        (root / "server/migrations" / (name + ".sql")).write_text("SELECT 1;\n")
    (root / "server/migrations/013_staged_reading_access.sql").write_text("SELECT 1;\n")
    (root / "server/src/main.ts").write_text("export default 1;\n")
    (root / "contracts/model.ts").write_text("export const card=1;\n")
    (root / "scripts/content/validate.mjs").write_text("export {}\n")


class ConfigAndInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "repo"
        self.root.mkdir()
        fixture_repo(self.root)

    def config(self, enabled):
        config = self.home / "host-runtime.json"
        config.write_text(json.dumps({
            "version": 1, "enabled": enabled, "targets": {TARGET: TARGET_CONFIG},
        }))
        config.chmod(0o600)
        return config

    def test_tool_descriptors_are_exact_scope_and_non_generic(self):
        specs = {spec["name"]: spec for spec in HOST_RUNTIME_TOOL_SPECS}
        self.assertEqual(set(specs), {"get_runtime_status", "get_db_migration_status", "compare_deployment",
                                      "prepare_test_deploy", "prepare_db_migration", "execute_test_deploy",
                                      "get_host_operation", "get_predecessor_migration_status",
                                      "prepare_predecessor_migration", "execute_predecessor_migration",
                                      "execute_db_migration", "get_schema014_migration_status",
                                      "prepare_schema014_migration", "execute_schema014_migration"})
        for spec in specs.values():
            schema = spec["inputSchema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["project"]["enum"], ["tossApp"])
            self.assertEqual(schema["properties"]["target"]["enum"], ["tarororo-test"])
            self.assertEqual(schema["required"][:2], ["project", "target"])
            self.assertNotIn("argv", schema["properties"])
        self.assertFalse(specs["execute_test_deploy"]["annotations"]["readOnlyHint"])
        self.assertTrue(specs["get_host_operation"]["annotations"]["readOnlyHint"])

    def test_disabled_is_default_and_activation_requires_private_exact_config(self):
        self.assertFalse(load_config(self.home / "nonexistent.json"))
        path = self.config(False)
        self.assertFalse(load_config(path))
        self.config(True)
        self.assertTrue(load_config(path))
        path.chmod(0o644)
        with self.assertRaises(HostRuntimeError):
            load_config(path)
        path.chmod(0o600)
        path.write_text(json.dumps({"version": 1, "enabled": True, "targets": {}}))
        with self.assertRaises(HostRuntimeError):
            load_config(path)
        path.unlink()
        path.symlink_to(self.home / "nonexistent")
        with self.assertRaises(HostRuntimeError):
            load_config(path)

    def test_inventory_private_copy_captures_dirty_and_untracked_inputs(self):
        before = source_inventory(self.root)
        dest = self.home / "snapshot"
        dest.mkdir(mode=0o700)
        copied = source_inventory(self.root, copy_to=dest)
        self.assertEqual(before["digest"], copied["digest"])
        self.assertEqual(source_inventory(dest)["digest"], before["digest"])
        (self.root / "server/src/new-untracked.ts").write_text("export const changed=1\n")
        self.assertNotEqual(source_inventory(self.root)["digest"], before["digest"])
        (self.root / "server/src/new-untracked.ts").unlink()
        (self.root / "server/src/suspicious").symlink_to(self.home / "outside")
        with self.assertRaises(HostRuntimeError):
            source_inventory(self.root)

    def test_high_risk_inputs_must_match_git_blob_even_with_hidden_worktree_change(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "--",
                        *sorted(HIGH_RISK_INPUTS)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Seed reviewed build files"],
                       check=True)
        self.assertIsNone(verify_reviewed_high_risk_inputs(self.root))
        dockerfile = self.root / "deploy/pc-test/Dockerfile"
        original = dockerfile.read_text()
        dockerfile.write_text("RUN arbitrary-host-action\n")
        with self.assertRaises(HostRuntimeError) as caught:
            verify_reviewed_high_risk_inputs(self.root)
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
        # Git status may hide modified files, but this approval gate may not.
        subprocess.run(["git", "-C", str(self.root), "update-index",
                        "--assume-unchanged", "deploy/pc-test/Dockerfile"],
                       check=True)
        with self.assertRaises(HostRuntimeError):
            verify_reviewed_high_risk_inputs(self.root)
        subprocess.run(["git", "-C", str(self.root), "update-index",
                        "--no-assume-unchanged", "deploy/pc-test/Dockerfile"], check=True)
        dockerfile.write_text(original)
        subprocess.run(["git", "-C", str(self.root), "update-index",
                        "--skip-worktree", "deploy/pc-test/Dockerfile"], check=True)
        with self.assertRaises(HostRuntimeError):
            verify_reviewed_high_risk_inputs(self.root)

    def test_mutable_source_approval_script_cannot_bypass_installation_guard(self):
        config = self.config(True)
        ops = HostRuntimeOperations({PROJECT: self.root},
                                    state_dir=self.home / "host-state",
                                    config_path=config,
                                    approval_script=self.home / "unsafe-approve.py")
        with self.assertRaises(HostRuntimeError) as caught:
            ops.prepare(PROJECT, TARGET, "api", "toss-demo", "request-1")
        self.assertEqual(caught.exception.code, "UNSAFE_RUNTIME")

    def test_compose_rejects_live_commerce_and_privileged_api(self):
        path = self.config(True)
        fake = FakeDocker()
        runtime = HostRuntime({PROJECT: self.root}, config_path=path, runner=fake.run)
        original = fake.config

        def live_commerce(mode):
            value = original(mode)
            value["services"]["api"]["environment"]["READING_IAP_MODE"] = "verify-only"
            return value

        fake.config = live_commerce
        with self.assertRaises(HostRuntimeError) as exc:
            runtime.compose_config(self.root, "toss-demo")
        self.assertEqual(exc.exception.code, "WRONG_COMPOSE_CONFIG")

        def privileged(mode):
            value = original(mode)
            value["services"]["api"]["privileged"] = True
            return value

        fake.config = privileged
        with self.assertRaises(HostRuntimeError):
            runtime.compose_config(self.root, "toss-demo")

    def test_no_arbitrary_root_project_or_private_state(self):
        path = self.config(True)
        rt = HostRuntime({"tossApp": self.root}, config_path=path)
        with self.assertRaises(HostRuntimeError):
            rt.guard("personal-ops", TARGET)
        with self.assertRaises(HostRuntimeError):
            rt.guard(PROJECT, "production")
        self.assertEqual(rt.guard(PROJECT, TARGET), self.root)

    def test_disabled_does_not_probe_docker(self):
        path = self.config(False)
        fake = FakeDocker()
        rt = HostRuntime({PROJECT: self.root}, config_path=path, runner=fake.run)
        self.assertEqual(rt.get_status(PROJECT, TARGET)["availability"], "disabled")
        self.assertFalse(fake.calls)

    def test_status_reveals_only_whitelisted_fields(self):
        path = self.config(True)
        fake = FakeDocker()
        rt = HostRuntime({PROJECT: self.root}, config_path=path, runner=fake.run)
        with patch.object(rt, "http_health", return_value="ready"):
            value = rt.get_status(PROJECT, TARGET)
        self.assertEqual(value["api"]["monetization_mode"], "disabled")
        self.assertEqual(value["database"]["schema_013"], "ready")
        self.assertNotIn("environment", str(value))
        self.assertNotIn("db-password", str(value))


class ProcessAndSecretBoundaryTest(unittest.TestCase):
    def test_host_subprocess_output_is_bounded_during_read(self):
        with self.assertRaises(HostRuntimeError) as caught:
            _default_run([sys.executable, "-c",
                          "import sys;sys.stdout.buffer.write(b'x'*300000)"],
                         timeout=5)
        self.assertEqual(caught.exception.code, "HOST_OUTPUT_TOO_LARGE")

    def test_host_subprocess_timeout_kills_session(self):
        with self.assertRaises(HostRuntimeError) as caught:
            _default_run([sys.executable, "-c",
                          "import time;time.sleep(5)"],
                         timeout=0.1)
        self.assertEqual(caught.exception.code, "HOST_COMMAND_TIMEOUT")

    def test_secret_validation_uses_metadata_not_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "secrets"
            base.mkdir(mode=0o700)
            runtime = HostRuntime({})
            names = ("db-password", "subject-secret", "gemini-api-key",
                     "toss-client.crt", "toss-client.key")
            for name in names:
                file = base / name
                file.write_text("fake-secret-never-read")
                file.chmod(0o600)
            self.assertIsNone(runtime.check_deploy_secrets(base=base))
            key = base / "toss-client.key"
            key.chmod(0o644)
            with self.assertRaises(HostRuntimeError) as caught:
                runtime.check_deploy_secrets(base=base)
            self.assertEqual(caught.exception.code, "UNSAFE_SECRET_FILE")


class OperationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        fixture_repo(self.root)
        self.config = self.home / "config.json"
        self.config.write_text(json.dumps({
            "version": 1, "enabled": True, "targets": {TARGET: TARGET_CONFIG},
        }))
        self.config.chmod(0o600)
        self.docker = FakeDocker()
        self.runtime = HostRuntime({PROJECT: self.root},
                                   config_path=self.config, runner=self.docker.run)
        self.ops = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / "state",
            config_path=self.config, runtime=self.runtime,
            approval_script=self.home / "immutable" / "approve-host-operation.py",
        )
        approval = self.home / "immutable" / "approve-host-operation.py"
        approval.parent.mkdir()
        approval.write_text("# immutable fixture\n")
        self.secret_patch = patch.object(self.runtime, "check_deploy_secrets", return_value=None)
        self.secret_patch.start()
        self.addAsyncCleanup(lambda: self.secret_patch.stop())
        self.health_patch = patch.object(self.runtime, "http_health", return_value="ready")
        self.git_patch = patch("host_runtime_operations.git_state",
                               return_value={"head": "a" * 40, "dirty": True,
                                             "changed_paths": ["server/src/main.ts"]})
        self.health_patch.start()
        self.git_patch.start()
        self.review_patch = patch("host_runtime_operations.verify_reviewed_high_risk_inputs",
                                  return_value=None)
        self.review_patch.start()
        self.addAsyncCleanup(lambda: self.review_patch.stop())
        self.addAsyncCleanup(lambda: self.health_patch.stop())
        self.addAsyncCleanup(lambda: self.git_patch.stop())

    def plan(self, mode="toss-demo", request_id="req-1"):
        return self.ops.prepare(PROJECT, TARGET, "api", mode, request_id)

    def active_workspace(self):
        owner = self.home / "workspace-owner"
        owner.mkdir(mode=0o700)
        policy = owner / "policy.json"
        policy.write_text(json.dumps({
            "version": 1, "base_root": str(self.home),
            "artifacts": [], "protected_paths": [],
        }), encoding="utf-8")
        policy.chmod(0o600)
        workspace = WorkspaceTools(
            {PROJECT: self.root}, state_dir=self.home / "workspace-authority",
            config_path=policy)
        snapshot = workspace.policy.load(required=True)
        workspace.authority.initialize(snapshot.digest, {"f" * 64: 1})
        return workspace

    def approve(self, plan):
        result = self.ops.approve(PROJECT, TARGET, plan["operation_id"],
                                  input_stream=TTY(plan["operation_id"] + "\n"), output=TTY())
        self.assertTrue(result["approved"])

    def test_pending_plan_can_be_approved_six_days_after_creation(self):
        plan = self.plan(request_id="six-day-approval")
        created = host_runtime_operations._timestamp(plan["created_at"])
        real_datetime = host_runtime_operations.datetime

        class SixDaysLater(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return created + timedelta(days=6)

        with patch.object(host_runtime_operations, "datetime", SixDaysLater):
            self.approve(plan)
        receipt = self.ops.operations_dir / (plan["operation_id"] + ".approval.json")
        self.assertTrue(receipt.is_file())

    def test_pending_plan_expires_exactly_seven_days_after_creation(self):
        plan = self.plan(request_id="seven-day-expiry")
        created = host_runtime_operations._timestamp(plan["created_at"])
        real_datetime = host_runtime_operations.datetime

        class SevenDaysLater(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return created + timedelta(days=7)

        with patch.object(host_runtime_operations, "datetime", SevenDaysLater):
            with self.assertRaises(HostRuntimeError) as caught:
                self.approve(plan)
        self.assertEqual(caught.exception.code, "PLAN_EXPIRED")
        receipt = self.ops.operations_dir / (plan["operation_id"] + ".approval.json")
        self.assertFalse(receipt.exists())

    async def test_plan_has_no_build_or_api_restart_and_requires_real_receipt(self):
        plan = self.plan()
        self.assertEqual(plan["state"], "pending_host_approval")
        self.assertIn("approve-host-operation.py", plan["approval_command"])
        self.assertFalse(any("up" in argv for argv, _, _ in self.docker.calls))
        self.assertFalse(any(("build" in argv or "up" in argv) for argv, _, _ in self.docker.calls))
        with self.assertRaises(HostRuntimeError) as exc:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(exc.exception.code, "STATE_UNAVAILABLE")
        again = self.plan()
        self.assertEqual(again["operation_id"], plan["operation_id"])
        with self.assertRaises(HostRuntimeError):
            self.plan("disabled", "req-1")

    async def test_dynamic_host_gate_blocks_legacy_admission_without_consuming_receipt(self):
        plan = self.plan()
        self.approve(plan)
        receipt = self.ops.operations_dir / (plan["operation_id"] + ".approval.json")
        with host_admission(self.ops.state_dir.parent):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "TARGET_BUSY")
        self.assertTrue(receipt.exists())
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")

    async def test_workspace_lane_covers_deploy_through_postcheck(self):
        workspace = self.active_workspace()
        self.ops = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / "workspace-runtime-state",
            config_path=self.config, runtime=self.runtime,
            approval_script=self.home / "immutable" / "approve-host-operation.py",
            workspace=workspace)
        entered = threading.Event()
        release = threading.Event()
        original = self.ops._run_deploy_body

        def paused(operation_id):
            entered.set()
            release.wait(3)
            original(operation_id)

        plan = self.plan(request_id="workspace-lane")
        self.approve(plan)
        with patch.object(self.ops, "_run_deploy_body", side_effect=paused):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            with self.assertRaises(HostRuntimeError) as caught:
                with publication_lane(workspace):
                    pass
            self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
            release.set()
            await asyncio.gather(*list(self.ops._tasks))
        with publication_lane(workspace):
            pass

    async def test_deploy_escape_with_running_state_keeps_durable_reservation(self):
        workspace = self.active_workspace()
        self.ops = HostRuntimeOperations(
            {PROJECT: self.root},
            state_dir=self.home / "workspace-runtime-save-failure",
            config_path=self.config, runtime=self.runtime,
            approval_script=self.home / "immutable" / "approve-host-operation.py",
            workspace=workspace)
        plan = self.plan(request_id="workspace-save-failure")
        self.approve(plan)

        def terminal_save_failed(operation_id):
            self.ops._record(operation_id, state="running", phase="EXECUTE")
            raise RuntimeError("synthetic terminal state save failure")

        with patch.object(self.ops, "_run_deploy_body", side_effect=terminal_save_failed):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await asyncio.gather(*list(self.ops._tasks), return_exceptions=True)

        state = self.ops._load(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(state["state"], "running")
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")

    async def test_reservation_cleanup_failure_is_bounded_and_releases_target_fd(self):
        workspace = self.active_workspace()
        self.ops = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / "workspace-runtime-cleanup-failure",
            config_path=self.config, runtime=self.runtime,
            approval_script=self.home / "immutable" / "approve-host-operation.py",
            workspace=workspace)
        plan = self.plan(request_id="workspace-cleanup-failure")
        self.approve(plan)

        with self.assertLogs("host_runtime_operations", level="ERROR") as logged, \
                patch("host_runtime_operations.complete_host_operation",
                      side_effect=HostRuntimeError("WORKSPACE_HOST_LANE_INVALID")):
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await asyncio.gather(*list(self.ops._tasks))

        state = self.ops._load(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(
            (state["state"], state["phase"], state["code"]),
            ("outcome_unknown", "RECOVER", "WORKSPACE_HOST_RESERVATION_RELEASE_FAILED"))
        self.assertIn("WORKSPACE_HOST_RESERVATION_RELEASE_FAILED", "\n".join(logged.output))
        target_fd = self.ops._acquire_target_lock()
        os.close(target_fd)
        with self.assertRaises(HostRuntimeError) as caught:
            with publication_lane(workspace):
                pass
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")

    async def test_busy_publication_lane_does_not_consume_legacy_approval(self):
        workspace = self.active_workspace()
        self.ops = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / "busy-runtime-state",
            config_path=self.config, runtime=self.runtime,
            approval_script=self.home / "immutable" / "approve-host-operation.py",
            workspace=workspace)
        plan = self.plan(request_id="busy-lane")
        self.approve(plan)
        receipt = self.ops.operations_dir / (plan["operation_id"] + ".approval.json")
        with publication_lane(workspace):
            with self.assertRaises(HostRuntimeError) as caught:
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(caught.exception.code, "WORKSPACE_HOST_LANE_BUSY")
        self.assertTrue(receipt.exists())
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"],
                         "pending_host_approval")

    def test_stale_valid_legacy_approved_record_does_not_block_dynamic_host(self):
        plan = self.plan()
        state = self.ops._load(PROJECT, TARGET, plan["operation_id"])
        state["state"] = "approved"
        state["immutable"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        state["plan_digest"] = _digest(state["immutable"])
        self.ops._save(state)
        self.assertFalse(observed_legacy_operation_blocks_host(
            self.ops.operations_dir / (plan["operation_id"] + ".json")))

    async def test_approval_must_be_tty_and_source_drift_rejected(self):
        plan = self.plan()
        with self.assertRaises(HostRuntimeError) as exc:
            self.ops.approve(PROJECT, TARGET, plan["operation_id"],
                             input_stream=io.StringIO(plan["operation_id"] + "\n"), output=TTY())
        self.assertEqual(exc.exception.code, "TTY_REQUIRED")
        (self.root / "server/src/main.ts").write_text("changed after plan")
        with self.assertRaises(HostRuntimeError) as exc:
            self.ops.approve(PROJECT, TARGET, plan["operation_id"],
                             input_stream=TTY(plan["operation_id"] + "\n"), output=TTY())
        self.assertEqual(exc.exception.code, "STALE_PLAN")

    def test_new_prepare_retires_source_stale_pending_plan(self):
        old = self.plan(request_id="stale-pending-source")
        (self.root / "server/src/main.ts").write_text("changed before approval\n")

        replacement = self.plan(request_id="replacement-after-source-drift")

        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "STALE_PLAN"))
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        self.assertEqual(replacement["state"], "pending_host_approval")

    def test_failed_approval_retires_target_stale_pending_plan_for_replacement(self):
        old = self.plan(request_id="stale-pending-target")
        self.docker.worker_id = "f" * 64

        with self.assertRaises(HostRuntimeError) as caught:
            self.approve(old)
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "STALE_PLAN"))

        replacement = self.plan(request_id="replacement-after-target-drift")
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        self.assertEqual(replacement["state"], "pending_host_approval")

    def test_tty_approval_race_retires_plan_that_turns_stale_after_prompt(self):
        old = self.plan(request_id="tty-race-stale-target")
        fixture = self

        class DriftAfterPrompt(TTY):
            def readline(self, *args, **kwargs):
                fixture.docker.worker_id = "f" * 64
                return super().readline(*args, **kwargs)

        with self.assertRaises(HostRuntimeError) as caught:
            self.ops.approve(
                PROJECT, TARGET, old["operation_id"],
                input_stream=DriftAfterPrompt(old["operation_id"] + "\n"), output=TTY())
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "STALE_PLAN"))

        replacement = self.plan(request_id="replacement-after-tty-race")
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])

    def test_new_prepare_does_not_retire_pending_plan_on_source_check_error(self):
        old = self.plan(request_id="uncertain-pending-source")
        with patch("host_runtime_operations.source_inventory",
                   side_effect=HostRuntimeError("UNSAFE_SOURCE_TREE")):
            with self.assertRaises(HostRuntimeError) as caught:
                self.plan(request_id="uncertain-source-replacement")
        self.assertEqual(caught.exception.code, "UNSAFE_SOURCE_TREE")
        self.assertEqual(
            self.ops.get(PROJECT, TARGET, old["operation_id"])["state"],
            "pending_host_approval")

    def test_new_prepare_retires_pending_plan_after_git_head_only_change(self):
        old = self.plan(request_id="stale-pending-head")
        with patch("host_runtime_operations.git_state", return_value={
                "head": "b" * 40, "dirty": True,
                "changed_paths": ["docs/unchanged-runtime-input.md"]}):
            replacement = self.plan(request_id="replacement-after-head-change")

        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["code"]),
                         ("expired", "STALE_PLAN"))
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])

    async def test_schema_not_ready_blocks_demo_before_any_plan(self):
        original = self.docker.run

        def missing(argv, **kwargs):
            if argv[:2] == ["docker", "exec"]:
                return b"BEGIN\nmissing\nCOMMIT\n"
            return original(argv, **kwargs)

        self.runtime.runner = missing
        with self.assertRaises(HostRuntimeError) as exc:
            self.plan()
        self.assertEqual(exc.exception.code, "SCHEMA_NOT_READY")
        self.assertFalse(list(self.ops.operations_dir.glob("*.json")))

    async def test_uncommitted_privileged_build_files_require_prior_code_review(self):
        with patch("host_runtime_operations.git_state", return_value={
            "head": "a" * 40, "dirty": True,
            "changed_paths": ["deploy/pc-test/Dockerfile", "server/src/main.ts"],
        }):
            with self.assertRaises(HostRuntimeError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
        self.assertFalse(list(self.ops.operations_dir.glob("*.json")))

    async def test_override_changes_must_affect_the_api_image_only(self):
        plan = self.plan()
        self.approve(plan)
        self.docker.tamper_override = True
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await asyncio.gather(*list(self.ops._tasks))
        state = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(state["state"], "outcome_unknown")
        self.assertEqual(state["code"], "UNSAFE_IMAGE_OVERRIDE")
        self.assertFalse(any("up" in args for args, _, _ in self.docker.calls))

    async def test_approval_is_consumed_once_and_only_api_is_recreated(self):
        plan = self.plan()
        self.approve(plan)
        result = await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "queued")
        await asyncio.gather(*list(self.ops._tasks))
        done = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(done["state"], "succeeded", done)
        self.assertEqual(self.docker.api_id, "d" * 64)
        self.assertEqual(self.docker.worker_id, "b" * 64)
        self.assertEqual(self.docker.db_id, "c" * 64)
        self.assertEqual(self.docker.mode, "toss-demo")
        self.assertEqual((await self.ops.execute(PROJECT, TARGET, plan["operation_id"]))["state"], "succeeded")
        ups = [argv for argv, _, _ in self.docker.calls if "up" in argv]
        self.assertEqual(len(ups), 1)
        self.assertIn("--no-deps", ups[0])
        self.assertIn("--no-build", ups[0])
        self.assertIn("--pull", ups[0])
        self.assertEqual(ups[0][-1], "api")
        self.assertIsNotNone(self.docker.pinned_image)
        self.assertEqual(self.ops.attestation()["image_id"], self.docker.new_image)

    async def test_restarted_mcp_never_marks_running_as_success_or_reexecutes(self):
        plan = self.plan()
        with patch.object(self.ops, "_run_deploy", lambda _operation_id, fd: os.close(fd)):
            self.approve(plan)
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await asyncio.gather(*list(self.ops._tasks))
        restarted = HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / "state",
            runtime=self.runtime, config_path=self.config,
            approval_script=self.home / "immutable" / "approve-host-operation.py")
        result = restarted.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "outcome_unknown")

    async def test_other_mcp_process_does_not_mark_an_active_job_interrupted(self):
        plan = self.plan()
        self.approve(plan)
        entered = threading.Event()
        release = threading.Event()

        def paused(_operation_id, target_fd):
            entered.set()
            release.wait(3)
            os.close(target_fd)

        try:
            with patch.object(self.ops, "_run_deploy", paused):
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                other = HostRuntimeOperations(
                    {PROJECT: self.root}, state_dir=self.home / "state",
                    runtime=self.runtime, config_path=self.config,
                    approval_script=self.home / "immutable" / "approve-host-operation.py",
                )
                live = other.get(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(live["state"], "queued", live)
                with self.assertRaises(HostRuntimeError) as caught:
                    other.prepare(PROJECT, TARGET, "api", "disabled", "second-plan")
                self.assertEqual(caught.exception.code, "TARGET_BUSY")
        finally:
            release.set()
            await asyncio.gather(*list(self.ops._tasks))
        after = other.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(after["state"], "outcome_unknown")

    async def test_invalid_receipt_cannot_trigger_build(self):
        plan = self.plan()
        receipt = self.ops.operations_dir / (plan["operation_id"] + ".approval.json")
        receipt.write_text(json.dumps({
            "operation_id": plan["operation_id"],
            "plan_digest": plan["plan_digest"],
            "nonce": "malicious",
            "approved_at": "2026-10-12T12:00:00+00:00",
        }))
        receipt.chmod(0o600)
        with self.assertRaises(HostRuntimeError) as exc:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(exc.exception.code, "INVALID_HOST_APPROVAL")
        self.assertFalse(any(x[0][0] == "/usr/bin/python3" for x in self.docker.calls))

    async def test_docker_build_or_api_failure_never_claims_success(self):
        for stage in ("build", "up"):
            with self.subTest(stage=stage):
                plan = self.plan(request_id="failure-" + stage)
                self.approve(plan)
                if stage == "build":
                    self.docker.fail_build = True
                else:
                    self.docker.fail_up = True
                await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
                await asyncio.gather(*list(self.ops._tasks))
                done = self.ops.get(PROJECT, TARGET, plan["operation_id"])
                self.assertEqual(done["state"], "outcome_unknown", done)
                self.docker.fail_build = False
                self.docker.fail_up = False
                self.assertIsNotNone(done["finished_at"])

    async def test_target_drift_prevents_host_build(self):
        plan = self.plan()
        self.approve(plan)
        self.docker.worker_id = "f" * 64
        with self.assertRaises(HostRuntimeError) as exc:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(exc.exception.code, "STALE_PLAN")
        self.assertFalse(any(("build" in argv or "up" in argv) for argv, _, _ in self.docker.calls))

    async def test_prequeue_target_drift_retires_approved_plan_for_replacement(self):
        old = self.plan(request_id="prequeue-stale-target")
        self.approve(old)
        receipt = self.ops.operations_dir / (old["operation_id"] + ".approval.json")
        self.assertTrue(receipt.is_file())
        self.docker.worker_id = "f" * 64

        with self.assertRaises(HostRuntimeError) as caught:
            await self.ops.execute(PROJECT, TARGET, old["operation_id"])
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "STALE_PLAN"))
        self.assertFalse(receipt.exists())
        self.assertFalse(any(("build" in argv or "up" in argv)
                             for argv, _, _ in self.docker.calls))

        replacement = self.plan(request_id="replacement-after-prequeue-drift")
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])

    async def test_worker_build_recreates_only_worker(self):
        from host_runtime_config import TARGET_CONFIG_V2
        self.config.write_text(json.dumps({"version": 2, "enabled": True,
                                           "targets": {TARGET: TARGET_CONFIG_V2}}))
        plan = self.ops.prepare(PROJECT, TARGET, "worker", "disabled", "req-worker")
        self.assertEqual(plan["impact"]["preserves"], ["api", "db"])
        self.assertEqual(plan["component"], "worker")
        self.approve(plan)
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await asyncio.gather(*list(self.ops._tasks))
        result = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(self.docker.api_id, "a" * 64)
        self.assertEqual(self.docker.db_id, "c" * 64)
        self.assertEqual(self.docker.worker_id, "e" * 64)

    async def test_local_web_approval_is_digest_and_id_bound(self):
        from host_runtime_web import pending_operations, approve_web
        plan = self.plan()
        pending = pending_operations(self.ops)["plans"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["operation_id"], plan["operation_id"])
        with self.assertRaises(HostRuntimeError):
            approve_web(self.ops, operation_id=plan["operation_id"],
                        plan_digest="0" * 64, source_digest=plan["source_digest"],
                        confirmation=plan["operation_id"])
        with self.assertRaises(HostRuntimeError):
            approve_web(self.ops, operation_id=plan["operation_id"],
                        plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
                        confirmation="not-typed")
        approved = approve_web(self.ops, operation_id=plan["operation_id"],
                             plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
                             confirmation=plan["operation_id"])
        self.assertTrue(approved["approved"])
        await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
        await asyncio.gather(*list(self.ops._tasks))
        self.assertEqual(self.ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "succeeded")

    def test_local_web_stale_target_retires_pending_plan_for_replacement(self):
        from host_runtime_web import approve_web
        old = self.plan(request_id="web-stale-target")
        receipt = self.ops.operations_dir / (old["operation_id"] + ".approval.json")
        receipt.write_text("{}")
        receipt.chmod(0o600)
        self.docker.worker_id = "f" * 64

        with self.assertRaises(HostRuntimeError) as caught:
            approve_web(
                self.ops, operation_id=old["operation_id"],
                plan_digest=old["plan_digest"], source_digest=old["source_digest"],
                confirmation=old["operation_id"])
        self.assertEqual(caught.exception.code, "STALE_PLAN")
        retired = self.ops.get(PROJECT, TARGET, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "STALE_PLAN"))
        self.assertFalse(receipt.exists())

        replacement = self.plan(request_id="replacement-after-web-target-drift")
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])

    async def test_db_migration_plan_is_scoped_and_requires_explicit_upgrade(self):
        from host_runtime_config import TARGET_CONFIG_V2
        with self.assertRaises(HostRuntimeError) as denied:
            self.ops.prepare(PROJECT, TARGET, "migration", "disabled", "migrate-013")
        self.assertEqual(denied.exception.code, "EXTENDED_SCOPE_DISABLED")
        self.config.write_text(json.dumps({"version": 2, "enabled": True,
                                           "targets": {TARGET: TARGET_CONFIG_V2}}))
        status = self.runtime.migration_status(PROJECT, TARGET)
        self.assertEqual(status["state"], "missing")
        self.assertTrue(status["prerequisites_ok"])
        plan = self.ops.prepare(PROJECT, TARGET, "migration", "disabled", "migrate-013")
        self.assertEqual(plan["component"], "migration")
        self.assertEqual(plan["impact"]["schema_change"], "013_staged_reading_access.sql")
        self.assertFalse(any("INSERT INTO schema_migrations" in str(call)
                             for call in self.docker.calls))
        self.approve(plan)
        with patch("host_db_migration.migrate_013", return_value={
            "backup_sha256": "f" * 64, "backup_bytes": 10000
        }) as applied:
            await self.ops.execute(PROJECT, TARGET, plan["operation_id"])
            await asyncio.gather(*list(self.ops._tasks))
            self.assertEqual(applied.call_count, 1)
        after = self.ops.get(PROJECT, TARGET, plan["operation_id"])
        self.assertEqual(after["state"], "succeeded", after)
        self.assertEqual(self.docker.api_id, "a" * 64)
        self.assertEqual(self.docker.worker_id, "b" * 64)
        self.assertEqual(self.docker.db_id, "c" * 64)

    async def test_browser_requires_confirmation_for_scope_upgrade(self):
        from host_runtime_config import extended_enabled
        from host_runtime_web import enable_extended_scope
        self.assertFalse(extended_enabled(self.config))
        with self.assertRaises(HostRuntimeError):
            enable_extended_scope(self.ops, "WRONG")
        self.assertFalse(extended_enabled(self.config))
        self.config.parent.chmod(0o755)  # A common user-owned config directory.
        enabled = enable_extended_scope(self.ops, "ENABLE WORKER AND DB 013")
        self.assertTrue(enabled["changed"])
        self.assertTrue(extended_enabled(self.config))
        self.assertFalse(enable_extended_scope(self.ops, "ENABLE WORKER AND DB 013")["changed"])

    async def test_migration_013_backup_precedes_fixed_sql_in_fake_host(self):
        from host_db_migration import migrate_013
        from host_runtime_operations import _identity
        sql_path = self.root / "server/migrations/013_staged_reading_access.sql"
        sql_path.write_text("-- Durable credit packs and reading-scoped content unlocks\n"
                            "CREATE TABLE reading_ad_attempts(id integer);\n")
        sql = sql_path.read_bytes()
        checksum = hashlib.sha256(sql).hexdigest()
        self.docker.migration_checksum = checksum
        observed = []
        def fake_backup(stream):
            observed.append("backup")
            data = b"PGDMP" + b"x" * 200
            stream.write(data)
            return len(data), hashlib.sha256(data).hexdigest()
        def fake_check(path):
            observed.append("verify")
            self.assertEqual(path.read_bytes()[:5], b"PGDMP")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        def fake_restore(runtime, path, operation_id):
            observed.append("restore")
            self.assertEqual(operation_id, "e" * 32)
            self.assertTrue(path.exists())
        immutable = {"identities": _identity(self.runtime.identities()),
                     "migration_checksum": checksum}
        result = migrate_013(self.runtime, self.home / "db-migration-test",
                             immutable, self.root, "e" * 32,
                             backup_fn=fake_backup, validate_fn=fake_check,
                             restore_fn=fake_restore)
        self.assertEqual(observed, ["backup", "verify", "restore"])
        self.assertEqual(result["backup_bytes"], 205)
        self.assertTrue(self.docker.migration_ready)
        self.assertEqual(self.runtime.migration_status(PROJECT, TARGET)["state"], "ready")

    async def test_failed_backup_restore_never_applies_013(self):
        from host_db_migration import migrate_013
        from host_runtime_operations import _identity
        source = self.root / "server/migrations/013_staged_reading_access.sql"
        source.write_text("-- Durable credit packs and reading-scoped content unlocks\n"
                          "CREATE TABLE reading_ad_attempts(id integer);\n")
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        self.docker.migration_checksum = checksum
        immutable = {"identities": _identity(self.runtime.identities()),
                     "migration_checksum": checksum}
        def fake_backup(output):
            content = b"PGDMP" + b"z" * 128
            output.write(content)
            return len(content), hashlib.sha256(content).hexdigest()
        def deny_restore(runtime, path, operation_id):
            raise HostRuntimeError("BACKUP_RESTORE_FAILED")
        with self.assertRaisesRegex(HostRuntimeError, "BACKUP_RESTORE_FAILED"):
            migrate_013(self.runtime, self.home / "failed-restore", immutable,
                        self.root, "d" * 32, backup_fn=fake_backup,
                        validate_fn=lambda path: None, restore_fn=deny_restore)
        self.assertFalse(self.docker.migration_ready)
        self.assertFalse(any("INSERT INTO schema_migrations" in str(args)
                             for args, _, _ in self.docker.calls))

if __name__ == "__main__":
    unittest.main()
