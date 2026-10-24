"""Deterministic owner-web rollout plan, receipt and detached-launch safety tests.

No host services, systemd, DB, tunnel client or model calls are performed.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from host_runtime_config import HostRuntimeError, TRUSTED_APPROVAL_ORIGIN
import runtime_rollout_web
from runtime_rollout_web import (
    PLAN_TTL, PROJECT, RuntimeRolloutWeb, WebRolloutGrant, _catalog,
    _expected_snapshots, _observe_unit, _utc,
)


class RuntimeRolloutWebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="runtime-web-approval-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "personal-ops"
        source = Path(__file__).resolve().parents[2]
        shutil.copytree(source / "chatgpt-mcp", self.root / "chatgpt-mcp",
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        shutil.copytree(source / "work-dashboard", self.root / "work-dashboard",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.started = []
        self.runtime_identity = "a" * 64
        self.boot_id = "11111111-2222-3333-4444-555555555555"
        self.uid = os.getuid()
        self.ops = RuntimeRolloutWeb(
            {PROJECT: self.root}, state_dir=self.base / "state",
            launcher=lambda argv: self.started.append(argv) or True,
            runtime_reader=lambda: self.runtime_identity,
            boot_reader=lambda: self.boot_id,
            uid_reader=lambda: self.uid,
            test_only=True,
        )
        self.assertGreater(_catalog(self.root)["count"], 40)

    def prepare(self, name="first"):
        return self.ops.prepare(PROJECT, name)

    def approve(self, plan, **changes):
        args = {
            "operation_id": plan["operation_id"],
            "plan_digest": plan["plan_digest"],
            "source_digest": plan["source_digest"],
            "confirmation": plan["operation_id"],
        }
        args.update(changes)
        return self.ops.approve_web(PROJECT, **args)

    def assert_error(self, code, function):
        with self.assertRaises(HostRuntimeError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_no_execution_before_web_approval(self):
        plan = self.prepare()
        self.assertEqual(plan["state"], "pending_host_approval")
        self.assertEqual(plan["project"], PROJECT)
        self.assertTrue(plan["approval_required"])
        self.assertEqual(plan["approval_url"],
                         TRUSTED_APPROVAL_ORIGIN + "/runtime-rollouts?operation_id="
                         + plan["operation_id"])
        self.assertEqual(plan["impact"]["restart"],
                         ["personal-ops-tunnel.service", "personal-ops-admin.service"])
        self.assertFalse(self.started)
        self.assert_error("ROLLOUT_NOT_APPROVED",
                          lambda: self.ops.execute(PROJECT, plan["operation_id"]))
        self.assertFalse(self.started)

    def test_pending_rollout_can_be_approved_six_days_after_creation(self):
        plan = self.prepare("six-day-approval")
        created = runtime_rollout_web._expires(plan["created_at"])
        with mock.patch.object(
                runtime_rollout_web, "_utc", return_value=created + timedelta(days=6)):
            approved = self.approve(plan)
        self.assertTrue(approved["approved"])
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "approved")

    def test_pending_rollout_expires_exactly_seven_days_after_creation(self):
        plan = self.prepare("seven-day-expiry")
        created = runtime_rollout_web._expires(plan["created_at"])
        with mock.patch.object(
                runtime_rollout_web, "_utc", return_value=created + timedelta(days=7)):
            self.assert_error("ROLLOUT_APPROVAL_DENIED", lambda: self.approve(plan))
        self.assertFalse((self.ops.receipts_dir / (plan["operation_id"] + ".json")).exists())

    def test_explicit_web_approval_does_not_run_anything_and_cannot_be_replayed(self):
        plan = self.prepare()
        for fields in ({"confirmation": "ROLLOUT"},
                       {"plan_digest": "0" * 64},
                       {"source_digest": "0" * 64}):
            with self.subTest(fields=fields):
                self.assert_error("ROLLOUT_APPROVAL_DENIED",
                                  lambda: self.approve(plan, **fields))
        approved = self.approve(plan)
        self.assertEqual(approved, {
            "project": PROJECT, "operation_id": plan["operation_id"],
            "approved": True, "executed": False,
        })
        self.assertFalse(self.started)
        self.assert_error("ROLLOUT_APPROVAL_DENIED", lambda: self.approve(plan))
        receipt = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        self.assertTrue(receipt.is_file())
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "approved")

    def test_single_approved_execution_is_a_typed_systemd_unit(self):
        plan = self.prepare()
        self.approve(plan)
        queued = self.ops.execute(PROJECT, plan["operation_id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(len(self.started), 1)
        argv = self.started[0]
        self.assertEqual(argv[:4], ["/usr/bin/systemd-run", "--user", "--quiet", "--collect"])
        self.assertIn("--unit=personal-ops-runtime-rollout-" + plan["operation_id"], argv)
        self.assertIn("--property=RuntimeMaxSec=1200", argv)
        self.assertIn("--property=Type=exec", argv)
        self.assertTrue(argv[-1] == plan["operation_id"])
        self.assertEqual(self.ops.execute(PROJECT, plan["operation_id"])["state"], "queued")
        self.assertEqual(len(self.started), 1)
        frozen, grant = self.ops.claim_for_worker(plan["operation_id"])
        self.assertIsInstance(grant, WebRolloutGrant)
        self.assertTrue((frozen / "chatgpt-mcp/rollout-host-runtime.py").is_file())
        self.assert_error("ROLLOUT_ALREADY_CONSUMED",
                          lambda: self.ops.claim_for_worker(plan["operation_id"]))
        self.assertFalse((self.ops.receipts_dir / (plan["operation_id"] + ".json")).exists())
        finished = self.ops.finish_from_worker(
            plan["operation_id"], {"status": "ready", "snapshot": plan["target_snapshot"],
                                  "installed": True, "switched": True,
                                  "tools_registered": True,
                                  "connector_schemas_verified": True},
        )
        self.assertEqual(finished["state"], "succeeded")
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "succeeded")

    def test_boot_change_after_approval_rejects_execute_without_launch(self):
        plan = self.prepare()
        self.approve(plan)
        self.boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        self.assert_error(
            "ROLLOUT_APPROVAL_STALE",
            lambda: self.ops.execute(PROJECT, plan["operation_id"]),
        )

        self.assertFalse(self.started)
        state = self.ops.get(PROJECT, plan["operation_id"])
        self.assertEqual((state["state"], state["code"]),
                         ("expired", "ROLLOUT_APPROVAL_STALE"))

    def test_boot_change_after_queue_fails_claim_and_preserves_receipt(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        receipt = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        self.boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        self.assertEqual(
            self.ops.get(PROJECT, plan["operation_id"])["state"], "queued"
        )

        self.assert_error(
            "ROLLOUT_APPROVAL_STALE",
            lambda: self.ops.claim_for_worker(plan["operation_id"]),
        )

        state = self.ops.get(PROJECT, plan["operation_id"])
        self.assertEqual((state["state"], state["phase"], state["code"]),
                         ("failed", "PRECHECK", "ROLLOUT_APPROVAL_STALE"))
        self.assertTrue(receipt.is_file())

    def test_new_plan_pins_exact_issuer_context_in_digest(self):
        plan = self.prepare()
        stored = self.ops._load(plan["operation_id"])

        self.assertEqual(stored["immutable"]["approval_context"], {
            "version": 1,
            "runtime_identity": self.runtime_identity,
            "boot_id": self.boot_id,
            "uid": self.uid,
        })
        self.assertEqual(stored["plan_digest"],
                         runtime_rollout_web._digest(stored["immutable"]))

    def test_runtime_and_uid_changes_after_approval_reject_execute(self):
        for field, changed in (("runtime_identity", "b" * 64), ("uid", self.uid + 1)):
            with self.subTest(field=field):
                plan = self.prepare("stale-" + field)
                self.approve(plan)
                if field == "runtime_identity":
                    self.runtime_identity = changed
                else:
                    self.uid = changed
                self.assert_error(
                    "ROLLOUT_APPROVAL_STALE",
                    lambda: self.ops.execute(PROJECT, plan["operation_id"]),
                )
                self.assertFalse(self.started)
                self.runtime_identity = "a" * 64
                self.uid = os.getuid()

    def test_pending_context_mismatch_expires_and_allows_new_request(self):
        plan = self.prepare()
        self.runtime_identity = "b" * 64

        stale = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((stale["state"], stale["phase"], stale["code"]),
                         ("expired", "APPROVAL", "ROLLOUT_APPROVAL_STALE"))
        replacement = self.prepare("replacement-after-context-change")
        self.assertEqual(replacement["state"], "pending_host_approval")

    def test_context_read_failure_is_not_misclassified_as_stale(self):
        plan = self.prepare()
        self.ops.runtime_reader = lambda: (_ for _ in ()).throw(
            HostRuntimeError("UNSAFE_RUNTIME")
        )

        self.assert_error(
            "UNSAFE_RUNTIME", lambda: self.ops.get(PROJECT, plan["operation_id"])
        )

        self.assertEqual(self.ops._load(plan["operation_id"])["state"],
                         "pending_host_approval")

    def test_legacy_unbound_approval_is_retired_without_rebinding(self):
        plan = self.prepare()
        self.approve(plan)
        stored = self.ops._load(plan["operation_id"])
        del stored["immutable"]["approval_context"]
        stored["plan_digest"] = runtime_rollout_web._digest(stored["immutable"])
        receipt_path = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        receipt = json.loads(receipt_path.read_text())
        receipt["plan_digest"] = stored["plan_digest"]
        receipt_path.write_text(json.dumps(receipt))
        self.ops._save(stored)

        retired = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((retired["state"], retired["code"]),
                         ("expired", "ROLLOUT_APPROVAL_STALE"))
        self.assertNotIn("approval_context", retired)

    def test_legacy_unbound_pending_plan_is_retired_without_rebinding(self):
        plan = self.prepare()
        stored = self.ops._load(plan["operation_id"])
        del stored["immutable"]["approval_context"]
        stored["plan_digest"] = runtime_rollout_web._digest(stored["immutable"])
        self.ops._save(stored)

        retired = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "ROLLOUT_APPROVAL_STALE"))

    def test_running_and_succeeded_are_not_freshness_reconciled(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        self.ops.claim_for_worker(plan["operation_id"])
        self.boot_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        self.assertEqual(
            self.ops.get(PROJECT, plan["operation_id"])["state"], "running"
        )
        self.ops.finish_from_worker(plan["operation_id"], {
            "status": "ready", "snapshot": "b" * 20,
        })
        self.runtime_identity = "c" * 64
        self.assertEqual(
            self.ops.get(PROJECT, plan["operation_id"])["state"], "succeeded"
        )

    def test_malformed_new_context_is_invalid_not_legacy(self):
        plan = self.prepare()
        stored = self.ops._load(plan["operation_id"])
        del stored["immutable"]["approval_context"]["uid"]
        stored["plan_digest"] = runtime_rollout_web._digest(stored["immutable"])
        self.ops._save(stored)

        self.assert_error(
            "ROLLOUT_PLAN_INVALID",
            lambda: self.ops.get(PROJECT, plan["operation_id"]),
        )
        self.assert_error(
            "ROLLOUT_PLAN_INVALID",
            lambda: self.ops._validated_approval_context({
                "version": True,
                "runtime_identity": self.runtime_identity,
                "boot_id": self.boot_id,
                "uid": self.uid,
            }),
        )

    def test_invalid_receipt_at_claim_is_durable_failed_and_preserved(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        receipt_path = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        receipt = json.loads(receipt_path.read_text())
        receipt["unexpected"] = True
        receipt_path.write_text(json.dumps(receipt))

        self.assert_error(
            "ROLLOUT_APPROVAL_DENIED",
            lambda: self.ops.claim_for_worker(plan["operation_id"]),
        )

        state = self.ops.get(PROJECT, plan["operation_id"])
        self.assertEqual((state["state"], state["phase"], state["code"]),
                         ("failed", "PRECHECK", "ROLLOUT_APPROVAL_DENIED"))
        self.assertTrue(receipt_path.is_file())

    def test_worker_claim_rejects_approval_that_expired_after_queue(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        receipt = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        expired = _utc() + PLAN_TTL + timedelta(seconds=1)
        with mock.patch.object(runtime_rollout_web, "_utc", return_value=expired):
            self.assert_error("ROLLOUT_APPROVAL_EXPIRED",
                              lambda: self.ops.claim_for_worker(plan["operation_id"]))
        state = self.ops.get(PROJECT, plan["operation_id"])
        self.assertEqual((state["state"], state["phase"], state["code"]),
                         ("expired", "PRECHECK", "ROLLOUT_APPROVAL_EXPIRED"))
        self.assertTrue(receipt.is_file())

    def test_approval_recovers_matching_receipt_left_before_state_save(self):
        plan = self.prepare()
        self.approve(plan)
        interrupted = self.ops._load(plan["operation_id"])
        interrupted.update(state="pending_host_approval", phase="APPROVAL")
        self.ops._save(interrupted)

        recovered = self.approve(plan)

        self.assertTrue(recovered["approved"])
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "approved")
        self.assertFalse(self.started)

    def test_approval_recovery_rejects_receipt_timestamp_outside_plan_ttl(self):
        plan = self.prepare()
        self.approve(plan)
        interrupted = self.ops._load(plan["operation_id"])
        interrupted.update(state="pending_host_approval", phase="APPROVAL")
        self.ops._save(interrupted)
        receipt = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        recovered = runtime_rollout_web._read_private_json(receipt)
        recovered["approved_at"] = (_utc() + PLAN_TTL + timedelta(minutes=1)).isoformat()
        runtime_rollout_web._save_private_json(receipt, recovered)

        self.assert_error("ROLLOUT_APPROVAL_DENIED", lambda: self.approve(plan))
        self.assertEqual(self.ops._load(plan["operation_id"])["state"],
                         "pending_host_approval")

    def test_claim_keeps_receipt_if_running_state_cannot_be_saved(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        receipt = self.ops.receipts_dir / (plan["operation_id"] + ".json")
        with mock.patch.object(self.ops, "_save", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                self.ops.claim_for_worker(plan["operation_id"])

        self.assertTrue(receipt.is_file())
        self.assertEqual(self.ops._load(plan["operation_id"])["state"], "queued")

    def test_stale_unclaimed_unit_fails_safely_and_allows_a_new_plan(self):
        observed = []
        self.ops.observer = lambda unit: observed.append(unit) or {
            "unit": unit, "load_state": "not-found", "transient": False,
            "active_state": "inactive", "sub_state": "dead", "result": "",
        }
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        stored = self.ops._load(plan["operation_id"])
        stored["started_at"] = (_utc() - timedelta(minutes=3)).isoformat()
        self.ops._save(stored)

        recovered = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((recovered["state"], recovered["phase"], recovered["code"]),
                         ("failed", "RECOVER", "ROLLOUT_WORKER_NOT_STARTED"))
        self.assertEqual(observed,
                         ["personal-ops-runtime-rollout-" + plan["operation_id"] + ".service"])
        self.assertEqual(self.prepare("after-missing-worker")["state"],
                         "pending_host_approval")

    def test_stale_claimed_worker_is_outcome_unknown_without_retry(self):
        self.ops.observer = lambda unit: {
            "unit": unit, "load_state": "loaded", "transient": True,
            "active_state": "failed", "sub_state": "failed", "result": "timeout",
        }
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        self.ops.claim_for_worker(plan["operation_id"])
        stored = self.ops._load(plan["operation_id"])
        stored["started_at"] = (_utc() - timedelta(minutes=23)).isoformat()
        self.ops._save(stored)

        recovered = self.ops.list(PROJECT)["plans"][-1]

        self.assertEqual((recovered["state"], recovered["phase"], recovered["code"]),
                         ("outcome_unknown", "RECOVER", "ROLLOUT_WORKER_TIMEOUT"))
        self.assertTrue(recovered["outcome_requires_review"])
        self.assert_error("ROLLOUT_ALREADY_PENDING", lambda: self.prepare("no-retry"))

    def test_stale_worker_distinguishes_host_restart_from_missing_unit(self):
        launch_boot = "11111111-2222-3333-4444-555555555555"
        current_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.ops.boot_reader = lambda: launch_boot
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        self.ops.claim_for_worker(plan["operation_id"])
        stored = self.ops._load(plan["operation_id"])
        stored["started_at"] = (_utc() - timedelta(minutes=23)).isoformat()
        self.ops._save(stored)
        self.ops.boot_reader = lambda: current_boot
        self.ops.observer = lambda unit: {
            "unit": unit, "load_state": "not-found", "transient": False,
            "active_state": "inactive", "sub_state": "dead", "result": "",
        }

        restarted = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((restarted["state"], restarted["code"]),
                         ("outcome_unknown", "ROLLOUT_HOST_RESTARTED"))

    def test_stale_claimed_worker_missing_on_same_boot_requires_review(self):
        boot = "11111111-2222-3333-4444-555555555555"
        self.ops.boot_reader = lambda: boot
        self.ops.observer = lambda unit: {
            "unit": unit, "load_state": "not-found", "transient": False,
            "active_state": "inactive", "sub_state": "dead", "result": "",
        }
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        self.ops.claim_for_worker(plan["operation_id"])
        stored = self.ops._load(plan["operation_id"])
        stored["started_at"] = (_utc() - timedelta(minutes=23)).isoformat()
        self.ops._save(stored)

        missing = self.ops.get(PROJECT, plan["operation_id"])

        self.assertEqual((missing["state"], missing["code"]),
                         ("outcome_unknown", "ROLLOUT_WORKER_MISSING"))

    def test_uncertain_or_active_unit_observation_never_invents_a_terminal_state(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        stored = self.ops._load(plan["operation_id"])
        stored["started_at"] = (_utc() - timedelta(minutes=3)).isoformat()
        self.ops._save(stored)
        self.ops.observer = lambda _unit: (_ for _ in ()).throw(OSError("no bus"))
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "queued")
        self.ops.observer = lambda unit: {
            "unit": unit, "load_state": "loaded", "transient": True,
            "active_state": "active", "sub_state": "running", "result": "success",
        }
        self.assertEqual(self.ops.get(PROJECT, plan["operation_id"])["state"], "queued")

    def test_systemd_observer_reads_only_the_exact_transient_unit(self):
        operation_id = "a" * 32
        unit = "personal-ops-runtime-rollout-" + operation_id + ".service"
        output = (f"Id={unit}\nLoadState=loaded\nTransient=yes\n"
                  "ActiveState=active\nSubState=running\nResult=success\n").encode()
        completed = subprocess.CompletedProcess([], 0, output, b"")
        with mock.patch.object(runtime_rollout_web.subprocess, "run",
                               return_value=completed) as command:
            observed = _observe_unit(unit)
        self.assertEqual(observed, {
            "unit": unit, "load_state": "loaded", "transient": True,
            "active_state": "active", "sub_state": "running", "result": "success",
        })
        self.assertEqual(command.call_args.args[0][:5], [
            "/usr/bin/systemctl", "--user", "--no-ask-password", "show", unit,
        ])

    def test_stale_source_at_approval_and_after_approval_blocks_execution(self):
        plan = self.prepare()
        # README is deliberately outside the frozen executable source set.
        src = self.root / "chatgpt-mcp" / "host_runtime_tools.py"
        src.write_bytes(src.read_bytes() + b"\n# drift\n")
        self.assert_error("ROLLOUT_SOURCE_CHANGED", lambda: self.approve(plan))
        self.assertFalse(self.started)
        # The first pending plan must be retired before a second is prepared.
        first_record = self.ops._load(plan["operation_id"])
        first_record["state"] = "expired"
        self.ops._save(first_record)
        # A fresh plan pins the new source; subsequent edits revoke it.
        next_plan = self.prepare("next")
        self.approve(next_plan)
        src.write_bytes(src.read_bytes() + b"\n# new drift\n")
        self.assert_error("ROLLOUT_SOURCE_CHANGED",
                          lambda: self.ops.execute(PROJECT, next_plan["operation_id"]))
        self.assertFalse(self.started)

    def test_new_prepare_retires_source_stale_pending_plan(self):
        old = self.prepare("stale-pending")
        source = self.root / "chatgpt-mcp" / "host_runtime_tools.py"
        source.write_bytes(source.read_bytes() + b"\n# stale pending plan\n")

        replacement = self.prepare("replacement-after-source-drift")

        retired = self.ops.get(PROJECT, old["operation_id"])
        self.assertEqual((retired["state"], retired["phase"], retired["code"]),
                         ("expired", "APPROVAL", "ROLLOUT_SOURCE_CHANGED"))
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        self.assert_error("ROLLOUT_APPROVAL_DENIED", lambda: self.approve(old))
        self.assertFalse(self.started)

    def test_new_prepare_retires_source_stale_approved_plan_and_receipt(self):
        old = self.prepare("stale-approved")
        self.approve(old)
        receipt = self.ops.receipts_dir / (old["operation_id"] + ".json")
        self.assertTrue(receipt.is_file())
        source = self.root / "chatgpt-mcp" / "server.py"
        source.write_bytes(source.read_bytes() + b"\n# stale approved plan\n")

        replacement = self.prepare("replacement-after-approved-drift")

        retired = self.ops.get(PROJECT, old["operation_id"])
        self.assertEqual((retired["state"], retired["code"]),
                         ("expired", "ROLLOUT_SOURCE_CHANGED"))
        self.assertFalse(receipt.exists())
        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        result = self.ops.execute(PROJECT, old["operation_id"])
        self.assertEqual(result["state"], "expired")
        self.assertFalse(self.started)

    def test_new_prepare_keeps_unchanged_approved_plan_blocking(self):
        old = self.prepare("unchanged-approved")
        self.approve(old)
        self.assert_error("ROLLOUT_ALREADY_PENDING",
                          lambda: self.prepare("must-remain-blocked"))
        self.assertEqual(self.ops.get(PROJECT, old["operation_id"])["state"], "approved")

    def test_new_prepare_does_not_retire_plan_when_staleness_check_is_uncertain(self):
        old = self.prepare("uncertain-source-check")
        with mock.patch.object(
                self.ops, "_check_source",
                side_effect=HostRuntimeError("ROLLOUT_SNAPSHOT_MISSING")):
            self.assert_error("ROLLOUT_SNAPSHOT_MISSING",
                              lambda: self.prepare("uncertain-replacement"))
        self.assertEqual(self.ops.get(PROJECT, old["operation_id"])["state"],
                         "pending_host_approval")

    def test_expired_pending_plan_with_missing_snapshot_does_not_block_replacement(self):
        old = self.prepare("expired-missing-snapshot")
        stored = self.ops._load(old["operation_id"])
        stored["immutable"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        stored["plan_digest"] = runtime_rollout_web._digest(stored["immutable"])
        self.ops._save(stored)
        shutil.rmtree(self.ops.snapshots_dir / old["operation_id"])

        replacement = self.prepare("replacement-after-expired-snapshot")

        self.assertNotEqual(replacement["operation_id"], old["operation_id"])
        self.assertEqual(replacement["state"], "pending_host_approval")

    def test_changed_source_after_queue_marks_worker_preflight_failed(self):
        plan = self.prepare()
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        source = self.root / "chatgpt-mcp" / "server.py"
        source.write_bytes(source.read_bytes() + b"\n# changed after queue\n")
        self.assert_error("ROLLOUT_SOURCE_CHANGED",
                          lambda: self.ops.claim_for_worker(plan["operation_id"]))
        state = self.ops.get(PROJECT, plan["operation_id"])
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["phase"], "PRECHECK")

    def test_symlinked_mcp_source_refused_before_snapshot(self):
        linked = self.root / "chatgpt-mcp/runtime-rollout-link.py"
        linked.symlink_to(self.root / "chatgpt-mcp/runtime_rollout_web.py")
        # This fixture's existing sources remain intact, but unsafe symlinks fail closed.
        self.assert_error("ROLLOUT_SOURCE_UNSAFE", lambda: self.prepare())
        self.assertFalse(self.started)

    def test_no_invalid_project_or_request_id(self):
        for project in ("tossApp", "../personal-ops", ""):
            self.assert_error("UNSUPPORTED_SCOPE",
                              lambda: self.ops.prepare(project, "start"))
        self.assert_error("UNSUPPORTED_SCOPE",
                          lambda: self.ops.prepare(PROJECT, "../bad"))
        self.assertFalse(self.started)

    def test_launch_uncertain_never_falsely_succeeds_or_retries(self):
        self.ops.launcher = lambda _argv: False
        plan = self.prepare()
        self.approve(plan)
        result = self.ops.execute(PROJECT, plan["operation_id"])
        self.assertEqual(result["state"], "outcome_unknown")
        self.assertEqual(result["code"], "ROLLOUT_LAUNCH_NOT_VERIFIED")
        self.assertNotEqual(result["state"], "succeeded")
        self.assertEqual(self.ops.execute(PROJECT, plan["operation_id"])["state"],
                         "outcome_unknown")
        self.assert_error("ROLLOUT_ALREADY_PENDING",
                          lambda: self.prepare("unsafe-second-rollout"))

    def test_target_snapshots_agree_with_installer_and_rollout_inventory(self):
        plan = self.prepare()
        target, control = _expected_snapshots(self.root)
        self.assertEqual(plan["target_snapshot"], target)
        self.assertEqual(plan["control_digest"], control)
        self.approve(plan)
        self.ops.execute(PROJECT, plan["operation_id"])
        frozen, grant = self.ops.claim_for_worker(plan["operation_id"])
        # Load only a frozen source script after the receipt was consumed.
        script = frozen / "chatgpt-mcp/rollout-host-runtime.py"
        module = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("test_web_rollout", script)
        )
        module.__spec__.loader.exec_module(module)
        installer = module._import_installer()
        grant.verify(frozen, installer, module.inventory, module.control_inventory)
        # A receipt already consumed in another process cannot authorize a
        # second fake grant, even when its digest matches the reviewed source.
        state = self.ops._load(plan["operation_id"])
        state["worker_pid"] = os.getpid() + 111
        self.ops._save(state)
        with self.assertRaisesRegex(RuntimeError, "WEB_ROLLOUT_GRANT_INVALID"):
            grant.verify(frozen, installer, module.inventory, module.control_inventory)
        state["worker_pid"] = os.getpid()
        self.ops._save(state)
        tampered = WebRolloutGrant("f" * 32, grant.source_digest,
                                  grant.target_snapshot, grant.control_digest,
                                  grant.state_root)
        with self.assertRaisesRegex(RuntimeError, "WEB_ROLLOUT_GRANT_INVALID"):
            tampered.verify(frozen, installer, module.inventory, module.control_inventory)

    def test_control_snapshot_pins_test_runner_and_node_ui_inputs(self):
        _target, before = _expected_snapshots(self.root)
        for relative in ("rollout_test_runner.py", "tests/test_workspace_approval_ui.mjs"):
            source = self.root / "chatgpt-mcp" / relative
            original = source.read_bytes()
            source.write_bytes(original + b"\n// fixture drift\n")
            try:
                _target, changed = _expected_snapshots(self.root)
                self.assertNotEqual(changed, before)
            finally:
                source.write_bytes(original)
            self.assertEqual(_expected_snapshots(self.root)[1], before)

    def test_control_snapshot_requires_workspace_node_verifier(self):
        required = self.root / "chatgpt-mcp/tests/test_workspace_approval_ui.mjs"
        required.unlink()
        self.assert_error(
            "ROLLOUT_SOURCE_INCOMPLETE",
            lambda: _expected_snapshots(self.root),
        )


if __name__ == "__main__":
    unittest.main()
