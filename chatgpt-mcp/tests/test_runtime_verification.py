from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runtime_verification


MODULE_DIR = Path(__file__).resolve().parents[1]


class ToolFixture:
    def __init__(self, name, input_schema, output_schema=None):
        self._payload = {
            "name": name,
            "description": "not part of the bounded catalog contract",
            "inputSchema": input_schema,
            "outputSchema": output_schema,
        }

    def model_dump(self, **_kwargs):
        return dict(self._payload)


class RuntimeVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-verification-")
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.state = self.root / "state"
        self.receipts = self.root / "receipts"
        for path in (self.releases, self.state, self.receipts):
            path.mkdir(mode=0o700)
        mandatory = {
            "server.py", "admin_server.py", "execution.py", "jobs.py", "job_tools.py",
            "workspace_paths.py", "workspace_policy.py", "workspace_authority.py",
            "workspace_jobs.py", "workspace_tools.py", "workspace_control.py",
            "workspace_files.py", "workspace_host.py", "workspace-launcher.py",
            "workspace-access-approval.html", "runtime_rollout_web.py",
            "runtime_rollout_worker.py",
        }
        self.release = self._release({name: ("# " + name + "\n").encode()
                                      for name in mandatory})
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        self.identity = runtime_verification.ProcessIdentity(
            boot_id=self.boot_id, pid=4312, start_ticks=987654,
            server_path=str(self.release / "server.py"), uid=os.getuid())

    def tearDown(self):
        self.temp.cleanup()

    def _release(self, files):
        manifest = {name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()}
        snapshot = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
        release = self.releases / snapshot
        release.mkdir(mode=0o700)
        for name, data in files.items():
            path = release / name
            path.write_bytes(data)
            path.chmod(0o600)
        manifest_path = release / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        manifest_path.chmod(0o600)
        return release

    def _observer(self):
        observer = runtime_verification.catalog_observer_for_runtime(
            self.release, self.state, releases_root=self.releases,
            boot_reader=lambda: self.boot_id,
            process_reader=lambda _pid=None: self.identity,
        )
        self.assertIsNotNone(observer)
        return observer

    def _observe(self):
        return self._observer()([
            ToolFixture("zeta", {"type": "object"}),
            ToolFixture("alpha", {"type": "object", "properties": {}},
                        {"type": "object"}),
        ], "a" * 64)

    def _plan(self, operation_id, *, finished_at=None):
        immutable = {
            "project": "personal-ops", "operation_id": operation_id,
            "target_snapshot": self.release.name,
        }
        plan = {
            "project": "personal-ops", "operation_id": operation_id,
            "state": "verification_required", "phase": "COMPLETE",
            "finished_at": finished_at or (
                datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "immutable": immutable,
            "plan_digest": hashlib.sha256(json.dumps(
                immutable, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "result": {"status": "verification_required",
                       "snapshot": self.release.name,
                       "connector_schemas_verified": False},
        }
        plans = self.state / "runtime-rollout-web" / "plans"
        plans.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = plans / f"{operation_id}.json"
        path.write_text(json.dumps(plan, sort_keys=True))
        path.chmod(0o600)
        return path

    def test_numeric_plan_snapshot_fails_with_bounded_plan_error(self):
        operation_id = "d" * 32
        path = self._plan(operation_id)
        plan = json.loads(path.read_text())
        plan["immutable"]["target_snapshot"] = 12345678901234567890
        plan["result"]["snapshot"] = 12345678901234567890
        plan["plan_digest"] = hashlib.sha256(json.dumps(
            plan["immutable"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path.write_text(json.dumps(plan, sort_keys=True))
        with self.assertRaises(runtime_verification.RuntimeVerificationError) as raised:
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)
        self.assertEqual(raised.exception.code, "ROLLOUT_PLAN_UNSUPPORTED")
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_catalog_observer_emits_canonical_full_and_per_tool_digests(self):
        evidence_path = self._observe()

        evidence = json.loads(evidence_path.read_text())
        self.assertEqual(evidence["tool_names"], ["alpha", "zeta"])
        self.assertEqual(set(evidence["tool_digests"]), {"alpha", "zeta"})
        self.assertEqual(evidence["tool_count"], 2)
        self.assertEqual(evidence["release_snapshot"], self.release.name)
        self.assertEqual(evidence["process"]["start_ticks"], 987654)
        self.assertEqual(evidence_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("description", evidence_path.read_text())

    def test_catalog_observer_keeps_distinct_session_generation_records(self):
        observer = self._observer()
        tools = [ToolFixture("one", {"type": "object"})]

        first = observer(tools, "a" * 64)
        second = observer(tools, "b" * 64)

        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_unverified_source_tree_gets_no_catalog_observer(self):
        source = self.root / "source"
        source.mkdir()

        observer = runtime_verification.catalog_observer_for_runtime(
            source, self.state, releases_root=self.releases)

        self.assertIsNone(observer)

    def test_post_install_receipt_binds_plan_release_identity_and_catalog(self):
        evidence_path = self._observe()
        operation_id = "1" * 32
        plan_path = self._plan(operation_id)
        original_plan = plan_path.read_bytes()

        result = runtime_verification.verify_operation(
            operation_id, state_dir=self.state, releases_root=self.releases,
            receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
            process_reader=lambda _pid=None: self.identity)

        self.assertEqual(plan_path.read_bytes(), original_plan)
        self.assertEqual(result["status"], "verification_required")
        self.assertNotIn("host_runtime_verified", result)
        self.assertTrue(result["server_process_manifest_verified"])
        self.assertTrue(result["server_catalog_observed"])
        self.assertFalse(result["tools_registered"])
        self.assertFalse(result["connector_metadata_verified"])
        self.assertFalse(result["connector_schemas_verified"])
        self.assertFalse(result["baseline_eligible"])
        self.assertEqual(result["catalog_sha256"],
                         json.loads(evidence_path.read_text())["catalog_sha256"])
        verification = Path(result["verification_path"])
        receipt = Path(result["receipt_path"])
        self.assertEqual(verification.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        receipt_payload = json.loads(receipt.read_text())
        verification_bytes = verification.read_bytes()
        verification_payload = json.loads(verification_bytes)
        self.assertEqual(receipt_payload["mode"], "verify")
        self.assertFalse(receipt_payload["result"]["runtime_verified"])
        self.assertFalse(receipt_payload["result"]["tools_registered"])
        self.assertTrue(receipt_payload["result"]["server_process_manifest_verified"])
        self.assertEqual(receipt_payload["result"]["server_path_sha256"],
                         hashlib.sha256(self.identity.server_path.encode()).hexdigest())
        self.assertEqual(receipt_payload["result"]["verification_sha256"],
                         hashlib.sha256(verification_bytes).hexdigest())
        self.assertEqual(verification_payload["server_path"], self.identity.server_path)
        self.assertEqual(verification_payload["original_plan_sha256"],
                         hashlib.sha256(original_plan).hexdigest())

    def test_malformed_evidence_types_are_bounded_as_tampered(self):
        evidence_path = self._observe()
        original = json.loads(evidence_path.read_text())
        malformed = (
            {**original, "session_generation": int("1" * 64)},
            {**original, "catalog_sha256": int("2" * 64)},
            {**original, "tool_names": [1, "zeta"]},
            {**original, "tool_digests": {"alpha": 3, "zeta": "4" * 64}},
        )
        for value in malformed:
            body = {key: item for key, item in value.items()
                    if key != "record_sha256"}
            value["record_sha256"] = runtime_verification._digest(body)
            with self.subTest(value=value), self.assertRaisesRegex(
                    runtime_verification.RuntimeVerificationError,
                    "CATALOG_EVIDENCE_TAMPERED"):
                runtime_verification._validated_evidence(value, evidence_path)

    def test_private_json_read_rejects_same_inode_same_size_mutation(self):
        folder = self.root / "read-race"
        folder.mkdir(mode=0o700)
        path = folder / "record.json"
        path.write_bytes(b'{"a":1}\n')
        path.chmod(0o600)
        real_read = os.read
        mutated = False

        def read_then_mutate(descriptor, count):
            nonlocal mutated
            payload = real_read(descriptor, count)
            if not mutated:
                mutated = True
                path.write_bytes(b'{"b":2}\n')
                path.chmod(0o600)
            return payload

        with mock.patch("runtime_verification.os.read", side_effect=read_then_mutate), \
                self.assertRaisesRegex(
                    runtime_verification.RuntimeVerificationError,
                    "CATALOG_EVIDENCE_UNSAFE"):
            runtime_verification._read_private_json(path)

    def test_tampered_catalog_evidence_is_rejected(self):
        evidence_path = self._observe()
        operation_id = "2" * 32
        self._plan(operation_id)
        evidence = json.loads(evidence_path.read_text())
        evidence["catalog_sha256"] = "0" * 64
        evidence_path.write_text(json.dumps(evidence))
        evidence_path.chmod(0o600)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_TAMPERED"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_symlink_or_unsafe_mode_catalog_evidence_is_rejected(self):
        evidence_path = self._observe()
        operation_id = "3" * 32
        self._plan(operation_id)
        evidence_path.chmod(0o644)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_UNSAFE"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_symlink_catalog_evidence_is_rejected(self):
        evidence_path = self._observe()
        operation_id = "9" * 32
        self._plan(operation_id)
        target = evidence_path.with_suffix(".real")
        evidence_path.rename(target)
        evidence_path.symlink_to(target)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_UNSAFE"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_future_catalog_observation_is_rejected(self):
        evidence_path = self._observe()
        operation_id = "c" * 32
        self._plan(operation_id)
        evidence = json.loads(evidence_path.read_text())
        evidence["observed_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        body = {key: value for key, value in evidence.items()
                if key != "record_sha256"}
        evidence["record_sha256"] = runtime_verification._digest(body)
        evidence_path.write_text(json.dumps(evidence))
        evidence_path.chmod(0o600)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_FUTURE"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_wrong_owner_catalog_evidence_is_rejected(self):
        evidence_path = self._observe()

        with mock.patch("runtime_verification.os.getuid", return_value=os.getuid() + 1), \
                self.assertRaisesRegex(
                    runtime_verification.RuntimeVerificationError,
                    "CATALOG_EVIDENCE_UNSAFE"):
            runtime_verification._read_private_json(evidence_path)

    def test_process_identity_is_rechecked_after_evidence_read(self):
        self._observe()
        operation_id = "7" * 32
        self._plan(operation_id)
        changed = runtime_verification.ProcessIdentity(
            boot_id=self.boot_id, pid=self.identity.pid,
            start_ticks=self.identity.start_ticks + 1,
            server_path=self.identity.server_path, uid=self.identity.uid)
        identities = iter((self.identity, changed))

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "RUNTIME_IDENTITY_CHANGED"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: next(identities))

    def test_stale_catalog_observation_is_rejected(self):
        self._observe()
        operation_id = "4" * 32
        self._plan(operation_id, finished_at=(
            datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_STALE"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_catalog_generation_cannot_verify_a_second_operation(self):
        self._observe()
        first = "5" * 32
        second = "6" * 32
        self._plan(first)
        self._plan(second)
        kwargs = dict(state_dir=self.state, releases_root=self.releases,
                      receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                      process_reader=lambda _pid=None: self.identity)
        runtime_verification.verify_operation(first, **kwargs)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "CATALOG_EVIDENCE_REPLAYED"):
            runtime_verification.verify_operation(second, **kwargs)

    def test_well_formed_older_plan_is_reported_as_unsupported(self):
        operation_id = "d" * 32
        plan_path = self._plan(operation_id)
        plan = json.loads(plan_path.read_text())
        plan["state"] = "succeeded"
        plan["result"]["status"] = "ready"
        plan_path.write_text(json.dumps(plan))
        plan_path.chmod(0o600)

        with self.assertRaisesRegex(
                runtime_verification.RuntimeVerificationError,
                "ROLLOUT_PLAN_UNSUPPORTED"):
            runtime_verification.verify_operation(
                operation_id, state_dir=self.state, releases_root=self.releases,
                receipt_root=self.receipts, boot_reader=lambda: self.boot_id,
                process_reader=lambda _pid=None: self.identity)

    def test_cli_exposes_only_operation_id_not_evidence_booleans(self):
        spec = importlib.util.spec_from_file_location(
            "verify_runtime_rollout_cli", MODULE_DIR / "verify-runtime-rollout.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch("sys.stderr", new=io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            module.main(["--operation-id", "8" * 32, "--verified", "true"])

        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
