from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

import host_access  # noqa: E402
from agent_tasks import AgentTaskManager  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_command_operations import HostCommandOperations  # noqa: E402
from host_registered_operations import (  # noqa: E402
    IMMUTABLE_KEYS, SOURCE_KEYS, STATE_KEYS, STATE_VERSION, RegisteredHostOperations,
)
from host_runtime_config import HostRuntimeError  # noqa: E402
from host_runtime_operations import (  # noqa: E402
    PREDECESSOR_NAMES, HostRuntimeOperations, _digest, _save_private_json,
)
from runtime_rollout_web import RuntimeRolloutWeb  # noqa: E402
from workspace_activation_legacy import (  # noqa: E402
    LegacyAuthorityInvalidator, LegacyInvalidationError,
)


NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def save(path: Path, value: dict) -> bytes:
    _save_private_json(path, value)
    path.chmod(0o600)
    return path.read_bytes()


class LegacyFixture:
    def __init__(self, root: Path):
        self.root = root
        self.project = private_dir(root / "project")
        self.projects = {"tossApp": self.project}

        self.access = HostAccess(
            self.projects, root / "host-access-parent",
            trusted_runtime=lambda: "a" * 64,
        )
        request = self.access.request("tossApp")
        self.grant = self.access.approve_web(
            "tossApp", request_id=request["request_id"], duration_seconds=3600,
            allow_codex=False, confirmation="tossApp",
        )
        self.pending_request = self.access.request("tossApp")

        self.commands = object.__new__(HostCommandOperations)
        self.commands.projects = self.projects
        self.commands.state_dir = private_dir(root / "commands")
        self.commands.plans_dir = private_dir(self.commands.state_dir / "plans")
        self.commands.receipts_dir = private_dir(self.commands.state_dir / "receipts")
        self.commands.lock_path = self.commands.state_dir / "operations.lock"
        self.commands._trusted = True
        self.command_id = "1" * 32
        frozen = {"profile_id": "fixture", "label": "Fixture", "effect": "fixture"}
        command = {
            "project": "tossApp", "operation_id": self.command_id,
            "state": "approved", "phase": "READY", "code": None,
            "frozen": frozen, "nonce": "2" * 64,
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "source_digest": "3" * 64,
        }
        command["plan_digest"] = _digest({
            "operation_id": self.command_id, "frozen": frozen,
            "nonce": command["nonce"], "created_at": command["created_at"],
            "expires_at": command["expires_at"],
        })
        save(self.commands.plans_dir / f"{self.command_id}.json", command)
        save(self.commands.receipts_dir / f"{self.command_id}.json", {"authority": True})

        self.runtime = object.__new__(HostRuntimeOperations)
        self.runtime.projects = self.projects
        self.runtime.state_dir = private_dir(root / "runtime")
        self.runtime.operations_dir = private_dir(self.runtime.state_dir / "operations")
        self.runtime.lock_path = self.runtime.state_dir / "operations.lock"
        self.runtime.target_authorizations_dir = private_dir(
            self.runtime.state_dir / "target-authorizations")
        self.runtime.target_authorizations_lock_path = (
            self.runtime.state_dir / "target-authorizations.lock")
        self.target_authorization = {
            "version": 1, "project": "tossApp", "target": "tarororo-test",
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "enabled": True, "grant_id": "e" * 32,
            "granted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
            "config_digest": "f" * 64,
            "migration_checksums": {name: "0" * 64 for name in PREDECESSOR_NAMES},
        }
        save(self.runtime.target_authorizations_dir / "tossApp--tarororo-test.json",
             self.target_authorization)
        self.runtime_id = "4" * 32
        immutable = {
            "operation_id": self.runtime_id, "project": "tossApp", "target": "tarororo-test",
            "mode": "disabled", "component": "api", "source_digest": "5" * 64,
        }
        runtime = {
            "project": "tossApp", "target": "tarororo-test", "operation_id": self.runtime_id,
            "state": "pending_host_approval", "phase": "APPROVAL", "code": None,
            "created_at": NOW.isoformat(), "immutable": immutable,
            "plan_digest": _digest(immutable),
        }
        save(self.runtime.operations_dir / f"{self.runtime_id}.json", runtime)
        save(self.runtime.operations_dir / f"{self.runtime_id}.approval.json", {"authority": True})

        self.registered = object.__new__(RegisteredHostOperations)
        self.registered.projects = self.projects
        self.registered.registered_dir = private_dir(root / "registered")
        self.registered.operations_dir = private_dir(self.registered.registered_dir / "operations")
        self.registered.receipts_dir = private_dir(self.registered.registered_dir / "receipts")
        self.registered.events_dir = private_dir(self.registered.registered_dir / "events")
        self.registered.lock_path = self.registered.registered_dir / "operations.lock"
        self.registered._trusted_runtime = True
        self.registered_id = "6" * 32
        source = {key: None for key in SOURCE_KEYS}
        registered_immutable = {key: None for key in IMMUTABLE_KEYS}
        registered_immutable.update({
            "version": STATE_VERSION, "kind": "registered",
            "operation_id": self.registered_id, "project": "tossApp",
            "target": "tarororo-test", "source": source,
        })
        registered = {key: None for key in STATE_KEYS}
        registered.update({
            "version": STATE_VERSION, "kind": "registered", "project": "tossApp",
            "target": "tarororo-test", "operation_id": self.registered_id,
            "state": "approved", "phase": "READY", "immutable": registered_immutable,
            "plan_digest": _digest(registered_immutable),
        })
        save(self.registered.operations_dir / f"{self.registered_id}.json", registered)
        save(self.registered.receipts_dir / f"{self.registered_id}.json", {"authority": True})

        self.agents = object.__new__(AgentTaskManager)
        self.agents.projects = self.projects
        self.agents.root = private_dir(root / "agents")
        self.agents.tasks_dir = private_dir(self.agents.root / "tasks")
        self.agents.receipts_dir = private_dir(self.agents.root / "receipts")
        self.agents.lock = self.agents.root / "agent-tasks.lock"
        self.agents._trusted = True
        self.agents.workspace = None
        self.agent_id = "7" * 32
        agent_immutable = {
            "project": "tossApp", "task_id": self.agent_id,
            "nonce": "8" * 64, "source_digest": "9" * 64,
        }
        agent = {
            "project": "tossApp", "task_id": self.agent_id, "state": "approved",
            "phase": "READY", "code": None, "created_at": NOW.isoformat(),
            "immutable": agent_immutable, "plan_digest": _digest(agent_immutable),
        }
        save(self.agents.tasks_dir / f"{self.agent_id}.json", agent)
        save(self.agents.receipts_dir / f"{self.agent_id}.json", {"authority": True})

        self.rollout = object.__new__(RuntimeRolloutWeb)
        self.rollout.state_root = private_dir(root / "rollout")
        self.rollout.plans_dir = private_dir(self.rollout.state_root / "plans")
        self.rollout.receipts_dir = private_dir(self.rollout.state_root / "receipts")
        self.rollout.lock = self.rollout.state_root / "rollout.lock"
        self.rollout._trusted = True
        self.rollout_id = "a" * 32
        rollout_immutable = {
            "operation_id": self.rollout_id, "source_digest": "b" * 64,
            "target_snapshot": "snapshot", "control_digest": "c" * 64,
        }
        rollout = {
            "project": "personal-ops", "operation_id": self.rollout_id,
            "state": "approved", "phase": "READY", "created_at": NOW.isoformat(),
            "immutable": rollout_immutable, "plan_digest": _digest(rollout_immutable),
        }
        save(self.rollout.plans_dir / f"{self.rollout_id}.json", rollout)
        save(self.rollout.receipts_dir / f"{self.rollout_id}.json", {"authority": True})

        terminal = dict(command)
        terminal.update(operation_id="d" * 32, state="outcome_unknown",
                        phase="RECOVER", code="HISTORICAL_UNCERTAINTY")
        terminal["plan_digest"] = _digest({
            "operation_id": terminal["operation_id"], "frozen": frozen,
            "nonce": terminal["nonce"], "created_at": terminal["created_at"],
            "expires_at": terminal["expires_at"],
        })
        self.terminal_path = self.commands.plans_dir / (terminal["operation_id"] + ".json")
        self.terminal_bytes = save(self.terminal_path, terminal)

        self.invalid = self.new_invalidator()

    def new_invalidator(self):
        return LegacyAuthorityInvalidator(
            host_access_state_dir=self.access.state_dir,
            host_command_state_dir=self.commands.state_dir,
            host_runtime_state_dir=self.runtime.state_dir,
            registered_state_dir=self.registered.registered_dir,
            agent_task_state_dir=self.agents.root,
            rollout_state_dir=self.rollout.state_root,
            host_access_store=self.access,
            host_command_store=self.commands,
            host_runtime_store=self.runtime,
            registered_store=self.registered,
            agent_task_store=self.agents,
            rollout_store=self.rollout,
        )


class LegacyAuthorityInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now_patch = mock.patch.object(host_access, "_utcnow", return_value=NOW)
        self.boot_patch = mock.patch.object(
            host_access, "_current_boot_id",
            return_value="12345678-1234-1234-1234-123456789abc",
        )
        self.now_patch.start()
        self.boot_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.boot_patch.stop)
        self.fx = LegacyFixture(Path(self.temp.name))

    def test_inventory_invalidate_and_verify_use_native_stores(self):
        inventory = self.fx.invalid.inventory()
        self.assertEqual(inventory["blockers"], [])
        self.assertEqual(inventory["action_count"], 13)

        receipt = self.fx.invalid.invalidate(inventory)
        verified = self.fx.invalid.verify_invalidated(receipt)

        self.assertTrue(verified["verified"])
        stored = self.fx.access._load("tossApp", self.fx.project)
        self.assertEqual(stored["requests"], [])
        self.assertIsNotNone(stored["grant"]["revoked_at"])
        self.assertFalse(self.fx.runtime._read_target_authorization_record(
            "tossApp", "tarororo-test")["enabled"])
        with self.assertRaises(HostRuntimeError) as caught:
            self.fx.access.require("tossApp", grant_id=self.fx.grant["grant_id"])
        self.assertEqual(caught.exception.code, "HOST_ACCESS_REVOKED")

        expected = (
            (self.fx.commands._load(self.fx.command_id)["state"], "cancelled"),
            (self.fx.runtime._load("tossApp", "tarororo-test", self.fx.runtime_id)["state"], "rejected"),
            (self.fx.registered._load("tossApp", "tarororo-test", self.fx.registered_id)["state"], "rejected"),
            (self.fx.agents._load("tossApp", self.fx.agent_id)["state"], "cancelled"),
            (self.fx.rollout._load(self.fx.rollout_id)["state"], "cancelled"),
        )
        self.assertTrue(all(actual == wanted for actual, wanted in expected))
        self.assertEqual(self.fx.terminal_path.read_bytes(), self.fx.terminal_bytes)
        self.assertFalse(any(path.exists() for path in (
            self.fx.commands.receipts_dir / f"{self.fx.command_id}.json",
            self.fx.runtime.operations_dir / f"{self.fx.runtime_id}.approval.json",
            self.fx.registered.receipts_dir / f"{self.fx.registered_id}.json",
            self.fx.agents.receipts_dir / f"{self.fx.agent_id}.json",
            self.fx.rollout.receipts_dir / f"{self.fx.rollout_id}.json",
        )))

        native_rejections = (
            ("NOT_PENDING_APPROVAL", lambda: self.fx.commands.approve_web(
                "tossApp", operation_id=self.fx.command_id,
                plan_digest=self.fx.commands._load(self.fx.command_id)["plan_digest"],
                source_digest="3" * 64, confirmation=self.fx.command_id)),
            ("NOT_PENDING_APPROVAL", lambda: self.fx.registered.approve_web(
                "tossApp", "tarororo-test", operation_id=self.fx.registered_id,
                plan_digest=self.fx.registered._read_state(self.fx.registered_id)["plan_digest"],
                source_digest="0" * 64, confirmation=self.fx.registered_id)),
            ("STALE_PLAN", lambda: self.fx.agents.approve_web(
                "tossApp", task_id=self.fx.agent_id,
                plan_digest=self.fx.agents._load("tossApp", self.fx.agent_id)["plan_digest"],
                source_digest="9" * 64, confirmation=self.fx.agent_id)),
            ("ROLLOUT_APPROVAL_DENIED", lambda: self.fx.rollout.approve_web(
                "personal-ops", operation_id=self.fx.rollout_id,
                plan_digest=self.fx.rollout._load(self.fx.rollout_id)["plan_digest"],
                source_digest="b" * 64, confirmation=self.fx.rollout_id)),
        )
        for code, operation in native_rejections:
            with self.assertRaises(HostRuntimeError) as rejected:
                operation()
            self.assertEqual(rejected.exception.code, code)
        with self.assertRaises(HostRuntimeError):
            self.fx.runtime._consume_receipt(
                self.fx.runtime._load("tossApp", "tarororo-test", self.fx.runtime_id))

        repeat = self.fx.invalid.invalidate(inventory)
        self.assertEqual(repeat["changed_count"], receipt["changed_count"])
        self.assertTrue(self.fx.invalid.verify_invalidated(repeat)["verified"])

    def test_changed_record_fails_closed_without_partial_mutation(self):
        inventory = self.fx.invalid.inventory()
        path = self.fx.commands.plans_dir / f"{self.fx.command_id}.json"
        value = json.loads(path.read_text())
        value["phase"] = "DRIFT"
        save(path, value)

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.invalidate(inventory)
        self.assertEqual(caught.exception.code, "LEGACY_RECORD_CHANGED")
        self.assertIsNone(self.fx.access._load("tossApp", self.fx.project)["grant"]["revoked_at"])

    def test_active_execution_blocks_inventory_effect(self):
        path = self.fx.commands.plans_dir / f"{self.fx.command_id}.json"
        value = json.loads(path.read_text())
        value.update(state="running", phase="EXECUTE")
        save(path, value)

        inventory = self.fx.invalid.inventory()
        self.assertEqual(inventory["blockers"][0]["code"], "LEGACY_EXECUTION_ACTIVE")
        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.invalidate(inventory)
        self.assertEqual(caught.exception.code, "LEGACY_EXECUTION_ACTIVE")

    def test_orphan_approval_receipt_fails_closed(self):
        save(self.fx.commands.receipts_dir / ("f" * 32 + ".json"), {"authority": True})

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.inventory()
        self.assertEqual(caught.exception.code, "LEGACY_ORPHAN_RECEIPT")

    def test_resume_accepts_already_removed_pinned_receipt(self):
        inventory = self.fx.invalid.inventory()
        (self.fx.commands.receipts_dir / f"{self.fx.command_id}.json").unlink()
        directory_info = self.fx.commands.receipts_dir.stat()
        synced_directories = []
        real_fsync = os.fsync

        def capture_fsync(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                synced_directories.append((info.st_dev, info.st_ino))
            return real_fsync(descriptor)

        with mock.patch("workspace_activation_legacy.os.fsync", side_effect=capture_fsync):
            receipt = self.fx.invalid.invalidate(inventory)

        self.assertTrue(self.fx.invalid.verify_invalidated(receipt)["verified"])
        self.assertIn(
            (directory_info.st_dev, directory_info.st_ino), synced_directories)

    def test_receipt_directory_fsync_failure_cannot_report_success(self):
        inventory = self.fx.invalid.inventory()
        directory_info = self.fx.commands.receipts_dir.stat()
        real_fsync = os.fsync

        def fail_command_receipt_directory(descriptor):
            info = os.fstat(descriptor)
            if (stat.S_ISDIR(info.st_mode)
                    and (info.st_dev, info.st_ino)
                    == (directory_info.st_dev, directory_info.st_ino)):
                raise OSError("injected receipt directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch(
                "workspace_activation_legacy.os.fsync",
                side_effect=fail_command_receipt_directory):
            with self.assertRaises(LegacyInvalidationError) as caught:
                self.fx.invalid.invalidate(inventory)

        self.assertEqual(caught.exception.code, "LEGACY_INVALIDATION_FAILED")

    def test_receipt_fifo_is_rejected_without_blocking(self):
        receipt_path = self.fx.commands.receipts_dir / "substituted.json"
        os.mkfifo(receipt_path, mode=0o600)
        pin = {
            "path": str(receipt_path), "sha256": "0" * 64,
            "uid": os.getuid(), "mode": 0o600, "nlink": 1,
            "device": 0, "inode": 0, "bytes": 0,
        }
        script = (
            "import json, sys\n"
            "from workspace_activation_legacy import ("
            "LegacyAuthorityInvalidator, LegacyInvalidationError)\n"
            "try:\n"
            "    LegacyAuthorityInvalidator._remove_receipt(json.loads(sys.argv[1]))\n"
            "except LegacyInvalidationError as exc:\n"
            "    raise SystemExit(0 if exc.code == 'LEGACY_RECORD_CHANGED' else 2)\n"
            "raise SystemExit(3)\n"
        )

        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, json.dumps(pin)],
            cwd=MODULE, capture_output=True, text=True, timeout=1, check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_terminal_receipt_appearing_after_inventory_fails_before_revocation(self):
        inventory = self.fx.invalid.inventory()
        receipt_path = self.fx.commands.receipts_dir / ("d" * 32 + ".json")
        save(receipt_path, {"authority": "appeared"})

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.invalidate(inventory)

        self.assertEqual(caught.exception.code, "LEGACY_RECORD_CHANGED")
        self.assertIsNone(self.fx.access._load(
            "tossApp", self.fx.project)["grant"]["revoked_at"])
        self.assertEqual(self.fx.terminal_path.read_bytes(), self.fx.terminal_bytes)

    def test_terminal_receipt_removed_after_inventory_fails_before_revocation(self):
        receipt_path = self.fx.commands.receipts_dir / ("d" * 32 + ".json")
        save(receipt_path, {"authority": "original"})
        inventory = self.fx.invalid.inventory()
        receipt_path.unlink()

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.invalidate(inventory)

        self.assertEqual(caught.exception.code, "LEGACY_RECORD_CHANGED")
        self.assertIsNone(self.fx.access._load(
            "tossApp", self.fx.project)["grant"]["revoked_at"])
        self.assertEqual(self.fx.terminal_path.read_bytes(), self.fx.terminal_bytes)

    def test_terminal_receipt_changed_after_inventory_fails_before_revocation(self):
        receipt_path = self.fx.commands.receipts_dir / ("d" * 32 + ".json")
        save(receipt_path, {"authority": "original"})
        inventory = self.fx.invalid.inventory()
        save(receipt_path, {"authority": "replacement"})

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.invalid.invalidate(inventory)

        self.assertEqual(caught.exception.code, "LEGACY_RECORD_CHANGED")
        self.assertIsNone(self.fx.access._load(
            "tossApp", self.fx.project)["grant"]["revoked_at"])
        self.assertEqual(self.fx.terminal_path.read_bytes(), self.fx.terminal_bytes)

    def test_store_subdirectory_escape_is_rejected_at_construction(self):
        self.fx.commands.receipts_dir = private_dir(self.fx.root / "outside-receipts")

        with self.assertRaises(LegacyInvalidationError) as caught:
            self.fx.new_invalidator()
        self.assertEqual(caught.exception.code, "LEGACY_STORE_MISMATCH")

    def test_resume_removes_receipt_after_record_was_already_cancelled(self):
        inventory = self.fx.invalid.inventory()
        state = self.fx.commands._load(self.fx.command_id)
        state.update(state="cancelled", phase="COMPLETE", code="WORKSPACE_ACTIVATED",
                     finished_at=NOW.isoformat(), termination_verified=True)
        self.fx.commands._save(state)

        receipt = self.fx.invalid.invalidate(inventory)

        self.assertTrue(self.fx.invalid.verify_invalidated(receipt)["verified"])
        self.assertFalse(
            (self.fx.commands.receipts_dir / f"{self.fx.command_id}.json").exists())

    def test_new_approval_during_last_mutation_cannot_be_blessed(self):
        inventory = self.fx.invalid.inventory()
        approved = self.fx.commands._load(self.fx.command_id)
        original = self.fx.invalid._invalidate_rollout

        def late_approval(item):
            original(item)
            approved["operation_id"] = "d" * 32
            approved["plan_digest"] = _digest({
                key: approved[key] for key in (
                    "operation_id", "frozen", "nonce", "created_at", "expires_at")
            })
            save(self.fx.commands.plans_dir / ("d" * 32 + ".json"), approved)
            save(self.fx.commands.receipts_dir / ("d" * 32 + ".json"), {"authority": True})

        with mock.patch.object(self.fx.invalid, "_invalidate_rollout", side_effect=late_approval):
            with self.assertRaises(LegacyInvalidationError) as caught:
                self.fx.invalid.invalidate(inventory)
        self.assertEqual(caught.exception.code, "LEGACY_INVALIDATION_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
