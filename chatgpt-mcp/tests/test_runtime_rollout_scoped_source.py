"""Scoped-source integration tests with private, synthetic source fixtures only."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from host_runtime_config import HostRuntimeError
import runtime_rollout_scoped_source as scoped_source
import runtime_rollout_web
from runtime_rollout_web import (
    PROJECT, RuntimeRolloutWeb, _catalog, _expected_snapshots,
)


class RuntimeRolloutScopedSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-scoped-source-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.home.chmod(0o700)
        self.source = Path(__file__).resolve().parents[2]
        self.project = self.home / "project"
        self._copy_source(self.project)
        self.policy_path = (
            self.home / ".config/chatgpt-coding-mcp/scoped-source-delivery.json"
        )
        self.candidate_parent = (
            self.home / ".local/state/chatgpt-coding-mcp/scoped-source-delivery"
        )
        self.ops = RuntimeRolloutWeb(
            {PROJECT: self.project}, state_dir=self.home / "state",
            launcher=lambda _argv: True,
            runtime_reader=lambda: "a" * 64,
            boot_reader=lambda: "11111111-2222-3333-4444-555555555555",
            uid_reader=os.getuid,
            test_only=True,
        )

    def _copy_source(self, destination: Path) -> None:
        shutil.copytree(
            self.source / "chatgpt-mcp", destination / "chatgpt-mcp",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        shutil.copytree(
            self.source / "work-dashboard", destination / "work-dashboard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )

    @staticmethod
    def _write_private_json(path: Path, value: dict) -> str:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        path.write_bytes(data)
        path.chmod(0o600)
        return hashlib.sha256(data).hexdigest()

    def _install_policy(self) -> tuple[Path, str]:
        baseline = self.home / "baseline"
        self._copy_source(baseline)
        for directory in (
                self.home / ".config",
                self.home / ".config/chatgpt-coding-mcp",
                self.home / ".local",
                self.home / ".local/state",
                self.home / ".local/state/chatgpt-coding-mcp",
                self.candidate_parent):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)

        draft = self.candidate_parent / "draft"
        shutil.copytree(baseline, draft)
        draft.chmod(0o700)
        changes = {}
        for index, relative in enumerate(sorted(scoped_source.ALLOWED), start=1):
            path = draft / relative
            data = path.read_bytes() + f"\nsynthetic-scoped-change-{index}\n".encode()
            path.write_bytes(data)
            changes[relative] = {
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            }
        candidate_digest = _catalog(draft)["digest"]
        candidate = self.candidate_parent / ("candidate-" + candidate_digest[:24])
        draft.rename(candidate)
        candidate.chmod(0o700)
        baseline_catalog = _catalog(baseline)
        attestation = {
            "status": "CANDIDATE_ONLY_NOT_INSTALLED",
            "policy_id": scoped_source.POLICY_ID,
            "baseline_digest": baseline_catalog["digest"],
            "candidate_digest": candidate_digest,
            "file_count": _catalog(candidate)["count"],
            "modified_paths": sorted(scoped_source.ALLOWED),
        }
        self._write_private_json(
            candidate / "source-delivery-attestation.json", attestation,
        )
        policy = {
            "version": 1,
            "policy_id": scoped_source.POLICY_ID,
            "project_root": str(self.project),
            "baseline_root": str(baseline),
            "baseline_digest": baseline_catalog["digest"],
            "candidate_root": str(self.candidate_parent),
            "release_request_sha256": "c" * 64,
            "changes": changes,
            "baseline_runtime_snapshot": _expected_snapshots(baseline)[0],
        }
        return candidate, self._write_private_json(self.policy_path, policy)

    def _select(self):
        return scoped_source.select_scoped_source(
            self.project, owner_home=self.home,
            catalog=_catalog, expected_snapshots=_expected_snapshots,
        )

    @contextmanager
    def _official_selector(self):
        def select(project_root, *, catalog, expected_snapshots):
            return scoped_source.select_scoped_source(
                project_root, owner_home=self.home,
                catalog=catalog, expected_snapshots=expected_snapshots,
            )
        with mock.patch.object(runtime_rollout_web, "select_scoped_source", select):
            yield

    def _approve(self, plan):
        return self.ops.approve_web(
            PROJECT, operation_id=plan["operation_id"],
            plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
            confirmation=plan["operation_id"],
        )

    def assert_error(self, code: str, action) -> None:
        with self.assertRaises(HostRuntimeError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_selector_without_policy_preserves_project_source(self):
        self.assertEqual(self._select(), (self.project, "project", "0" * 64))

    def test_selector_accepts_only_exact_registered_candidate(self):
        candidate, policy_digest = self._install_policy()

        selected = self._select()

        self.assertEqual(selected, (candidate, "owner_scoped", policy_digest))
        for relative in scoped_source.ALLOWED:
            self.assertNotEqual(
                (candidate / relative).read_bytes(), (self.project / relative).read_bytes(),
            )

    def test_selector_rejects_malformed_policy(self):
        self.policy_path.parent.mkdir(parents=True, mode=0o700)
        self.policy_path.write_text('{"version":1,"version":1}')
        self.policy_path.chmod(0o600)

        self.assert_error("SCOPED_POLICY_INVALID", self._select)

    def test_selector_does_not_fall_back_when_policy_lstat_is_inaccessible(self):
        original_lstat = Path.lstat

        def controlled_lstat(path):
            if path == self.policy_path:
                raise PermissionError("synthetic inaccessible policy parent")
            return original_lstat(path)

        with mock.patch.object(Path, "lstat", controlled_lstat):
            self.assert_error("SCOPED_POLICY_UNAVAILABLE", self._select)

    def test_selector_rejects_recursively_deep_policy_with_bounded_code(self):
        self.policy_path.parent.mkdir(parents=True, mode=0o700)
        self.policy_path.write_bytes(b"[" * 16000 + b"0" + b"]" * 16000)
        self.policy_path.chmod(0o600)

        self.assertLess(self.policy_path.stat().st_size, scoped_source.MAX_POLICY_BYTES)
        self.assert_error("SCOPED_POLICY_INVALID", self._select)

    def test_selector_rejects_drifted_baseline(self):
        self._install_policy()
        baseline_file = self.home / "baseline/chatgpt-mcp/server.py"
        baseline_file.write_bytes(baseline_file.read_bytes() + b"\n# drift\n")

        self.assert_error("SCOPED_BASELINE_CHANGED", self._select)

    def test_prepare_check_and_claim_use_registered_scoped_source(self):
        candidate, policy_digest = self._install_policy()
        with self._official_selector():
            prepared = self.ops.prepare(PROJECT, "registered-scoped-source")
            fixed = self.ops._load(prepared["operation_id"])["immutable"]
            self._approve(prepared)
            self.ops.execute(PROJECT, prepared["operation_id"])
            frozen, _grant = self.ops.claim_for_worker(prepared["operation_id"])

        self.assertEqual(prepared["source_digest"], _catalog(candidate)["digest"])
        self.assertEqual(fixed["source_mode"], "owner_scoped")
        self.assertEqual(fixed["source_policy_digest"], policy_digest)
        self.assertEqual(_catalog(frozen)["digest"], _catalog(candidate)["digest"])

    def test_policy_byte_drift_invalidates_earlier_scoped_approval(self):
        self._install_policy()
        with self._official_selector():
            prepared = self.ops.prepare(PROJECT, "policy-byte-drift")
            document = json.loads(self.policy_path.read_text())
            self.policy_path.write_text(json.dumps(document, indent=2))
            self.policy_path.chmod(0o600)

            self.assert_error("ROLLOUT_SOURCE_CHANGED", lambda: self._approve(prepared))

    def test_default_plan_cannot_switch_to_scoped_source_at_approval(self):
        with self._official_selector():
            prepared = self.ops.prepare(PROJECT, "default-to-scoped-approval")
            fixed = self.ops._load(prepared["operation_id"])["immutable"]
            self._install_policy()

            self.assertEqual(fixed["source_mode"], "project")
            self.assertEqual(fixed["source_policy_digest"], "0" * 64)
            self.assert_error("ROLLOUT_SOURCE_CHANGED", lambda: self._approve(prepared))

    def test_default_plan_cannot_switch_to_scoped_source_at_claim(self):
        with self._official_selector():
            prepared = self.ops.prepare(PROJECT, "default-to-scoped-claim")
            self._approve(prepared)
            self.ops.execute(PROJECT, prepared["operation_id"])
            self._install_policy()

            self.assert_error(
                "ROLLOUT_SOURCE_CHANGED",
                lambda: self.ops.claim_for_worker(prepared["operation_id"]),
            )
        failed = self.ops._load(prepared["operation_id"])
        self.assertEqual((failed["state"], failed["phase"]), ("failed", "PRECHECK"))

    def test_scoped_plan_cannot_fall_back_to_default_at_claim(self):
        self._install_policy()
        with self._official_selector():
            prepared = self.ops.prepare(PROJECT, "scoped-to-default-claim")
            self._approve(prepared)
            self.ops.execute(PROJECT, prepared["operation_id"])
            self.policy_path.unlink()

            self.assert_error(
                "ROLLOUT_SOURCE_CHANGED",
                lambda: self.ops.claim_for_worker(prepared["operation_id"]),
            )


if __name__ == "__main__":
    unittest.main()
