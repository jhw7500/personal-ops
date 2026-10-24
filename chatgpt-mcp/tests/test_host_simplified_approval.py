"""Registered reviewed-snapshot plans for the fixed Toss test host.

All fixtures are temporary Git repositories. No real Docker, database, host config,
service or runtime is touched.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_operation_fixtures import commit_all, fixture_v3  # noqa: E402
from host_runtime import PREDECESSOR_NAMES  # noqa: E402
from host_runtime_config import (  # noqa: E402
    PROJECT, TARGET, TARGET_CONFIG_V2, HostRuntimeError,
)
from host_runtime_operations import HostRuntimeOperations  # noqa: E402


def identities():
    return {role: {"id": role + "-id", "started_at": "2026-10-13T00:00:00Z",
                   "image_id": role + "-image"}
            for role in ("api", "worker", "db")}


class TTY(io.StringIO):
    def isatty(self):
        return True


class PlanningRuntime:
    """Read-only runtime surface used by HostRuntimeOperations.prepare/_verify."""

    def __init__(self, root: Path, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.commands = []

    def guard(self, project, target):
        if (project, target) != (PROJECT, TARGET):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return self.root

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        if not snapshot:
            raise HostRuntimeError("CONTAINER_NOT_RUNNING")

    def identities(self):
        return identities()

    @staticmethod
    def http_health():
        return "ready"

    @staticmethod
    def readiness():
        return "ready"

    @staticmethod
    def compose_config(root, mode):
        data = (Path(root) / "deploy/pc-test/compose.yaml").read_bytes() + mode.encode()
        return hashlib.sha256(data).hexdigest()

    def migration_status(self, project, target):
        self.guard(project, target)
        return {"state": "missing", "prerequisites_ok": True,
                "tables": {"staged_reading_access": False}}

    def predecessor_status(self, project, target):
        root = self.guard(project, target)
        checksums = {
            name: hashlib.sha256((root / "server/migrations" / name).read_bytes()).hexdigest()
            for name in PREDECESSOR_NAMES[5:12]
        }
        return {"ready_to_prepare": True, "complete": False, "checksums": checksums,
                "generation": {"database": "tarororo_test", "user": "tarororo",
                               "postmasterStartEpoch": "123.456"}}

    def run(self, argv, **kwargs):
        self.commands.append((list(argv), dict(kwargs)))
        raise AssertionError("planning tests must not execute a host command")


class SimplifiedApprovalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="simplified-host-approval-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / "tossApp"
        self.root.mkdir()
        self.payload = fixture_v3(self.root)
        self.payload["targets"][TARGET]["source_review"] = "reviewed_snapshot"
        # Legacy source_inventory deliberately excludes content tests while the generic
        # registered snapshot includes every direct *.mjs source input. Their digests are
        # independent even though the frozen tree contains everything both policies need.
        (self.root / "scripts/content/sample.test.mjs").write_text("export {};\n")

        # The fixture mirrors the reported working tree: reviewed baseline without 006-012,
        # followed by seven untracked SQL files and a dirty Dockerfile.
        predecessor_bytes = {}
        for name in PREDECESSOR_NAMES[5:12]:
            path = self.root / "server/migrations" / name
            predecessor_bytes[name] = path.read_bytes()
            path.unlink()
        commit_all(self.root)
        for name, data in predecessor_bytes.items():
            (self.root / "server/migrations" / name).write_bytes(data)
        (self.root / "deploy/pc-test/Dockerfile").write_text(
            "FROM node:24\nRUN printf reviewed-snapshot\n")

        self.config = self.home / "host-runtime.json"
        self.config.write_text(json.dumps(self.payload))
        self.config.chmod(0o600)

    def manager(self, suffix="default"):
        runtime = PlanningRuntime(self.root, self.config)
        approval = self.home / "immutable" / "approve-host-operation.py"
        approval.parent.mkdir(exist_ok=True)
        approval.touch(exist_ok=True)
        return (HostRuntimeOperations(
            {PROJECT: self.root}, state_dir=self.home / ("state-" + suffix),
            config_path=self.config, runtime=runtime, approval_script=approval,
        ), runtime)

    def test_reviewed_snapshot_plans_all_fixed_components_without_git_commit(self):
        for component in ("api", "worker", "migration", "predecessor_migration"):
            with self.subTest(component=component):
                manager, _runtime = self.manager(component)
                plan = manager.prepare(PROJECT, TARGET, component,
                                       "toss-demo" if component == "api" else "disabled",
                                       "request-" + component)
                self.assertEqual(plan["state"], "pending_host_approval")
                self.assertEqual(plan["source_review"]["policy"], "reviewed_snapshot")
                self.assertEqual(set(plan["source_review"]), {
                    "policy", "review_digest", "config_digest", "inventory_digest",
                })
                self.assertNotEqual(plan["source_digest"],
                                    plan["source_review"]["inventory_digest"])
                self.assertEqual(
                    plan["approval_url"],
                    "https://cantopsbuildserver.tail661d29.ts.net:8443/host-operations"
                    "?project=tossApp&target=tarororo-test&host_operation_id="
                    + plan["operation_id"])
                self.assertNotIn("nonce", plan["approval_url"])
                review = manager.review_artifact(PROJECT, TARGET, plan["operation_id"])
                reviewed_paths = {entry["path"] for entry in review["files"]}
                self.assertIn("deploy/pc-test/Dockerfile", reviewed_paths)
                if component == "predecessor_migration":
                    self.assertTrue({"server/migrations/" + name
                                     for name in PREDECESSOR_NAMES[5:12]}
                                    <= reviewed_paths)
                    self.assertEqual(plan["migration_range"], ["006", "012"])
                    self.assertNotIn(PREDECESSOR_NAMES[12], plan["migration_checksums"])
                self.assertTrue(manager.approve(
                    PROJECT, TARGET, plan["operation_id"],
                    input_stream=TTY(plan["operation_id"] + "\n"), output=TTY())["approved"])

    def test_execute_without_operation_approval_never_runs_host_command(self):
        manager, runtime = self.manager("no-approval")
        plan = manager.prepare(PROJECT, TARGET, "predecessor_migration", "disabled",
                               "request-no-approval")
        with self.assertRaises(HostRuntimeError):
            asyncio.run(manager.execute(PROJECT, TARGET, plan["operation_id"]))
        self.assertEqual(runtime.commands, [])

    def test_config_review_and_source_drift_each_block_approval(self):
        cases = ("config", "review", "frozen", "frozen-generic", "live-generic", "live")
        for case in cases:
            with self.subTest(case=case):
                manager, _runtime = self.manager("drift-" + case)
                plan = manager.prepare(PROJECT, TARGET, "api", "disabled", "request-" + case)
                operation_id = plan["operation_id"]
                if case == "config":
                    payload = json.loads(self.config.read_text())
                    payload["targets"][TARGET]["source_review"] = "git_head"
                    self.config.write_text(json.dumps(payload))
                elif case == "review":
                    review = manager.context_dir / operation_id / "review.json"
                    review.write_text('{"version":1,"git_head":"tampered","source_digest":"x","files":[]}')
                    review.chmod(0o600)
                elif case == "frozen":
                    frozen = manager.context_dir / operation_id / "source" / "server/src/main.ts"
                    frozen.write_text("export default 'tampered';\n")
                    frozen.chmod(0o600)
                elif case == "frozen-generic":
                    frozen = (manager.context_dir / operation_id / "source" /
                              "scripts/content/sample.test.mjs")
                    frozen.write_text("export const tampered = true;\n")
                    frozen.chmod(0o600)
                elif case == "live-generic":
                    (self.root / "scripts/content/sample.test.mjs").write_text(
                        "export const changed = true;\n")
                else:
                    (self.root / "server/src/main.ts").write_text("export default 'changed';\n")
                with self.assertRaisesRegex(HostRuntimeError, "STALE_PLAN"):
                    manager.approve(PROJECT, TARGET, operation_id,
                                    input_stream=TTY(operation_id + "\n"), output=TTY())
                # Restore the shared config before the next subtest.
                self.config.write_text(json.dumps(self.payload))
                self.config.chmod(0o600)

    def test_disabled_registry_and_unregistered_target_remain_denied(self):
        manager, _runtime = self.manager("scope")
        disabled = json.loads(self.config.read_text())
        disabled["enabled"] = False
        self.config.write_text(json.dumps(disabled))
        with self.assertRaisesRegex(HostRuntimeError, "HOST_RUNTIME_DISABLED"):
            manager.prepare(PROJECT, TARGET, "api", "disabled", "request-disabled")
        self.config.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(HostRuntimeError, "UNSUPPORTED_SCOPE"):
            manager.prepare(PROJECT, "not-registered", "api", "disabled", "request-target")

    def test_legacy_git_head_policy_keeps_commit_gate_and_old_public_contract(self):
        self.config.write_text(json.dumps({
            "version": 2, "enabled": True, "targets": {TARGET: TARGET_CONFIG_V2},
        }))
        manager, _runtime = self.manager("legacy")
        self.assertEqual(manager.planning_policy(PROJECT, TARGET), {
            "mode": "git_head", "components": ["api", "worker", "migration"],
            "commit_required": True, "approval": "per_operation",
        })
        with self.assertRaisesRegex(HostRuntimeError, "REVIEW_REQUIRED"):
            manager.prepare(PROJECT, TARGET, "worker", "disabled", "request-legacy")
        public = HostRuntimeOperations._public({
            "immutable": {"component": "api", "project": PROJECT, "target": TARGET,
                          "mode": "disabled", "source_digest": "a" * 64,
                          "changed_paths": [], "high_risk_changes": [],
                          "expires_at": "2026-10-13T05:00:00Z"},
            "project": PROJECT, "target": TARGET, "operation_id": "b" * 32,
            "state": "pending_host_approval", "phase": "APPROVAL",
            "plan_digest": "c" * 64, "created_at": "2026-10-13T04:00:00Z",
        })
        self.assertEqual(public["source_review"], {"policy": "git_head"})

    def test_planning_policy_reports_single_review_and_per_operation_approval(self):
        manager, _runtime = self.manager("policy")
        self.assertEqual(manager.planning_policy(PROJECT, TARGET), {
            "mode": "reviewed_snapshot",
            "components": ["api", "worker", "migration", "predecessor_migration", "schema014"],
            "commit_required": False,
            "approval": "per_operation",
        })

    def test_snapshot_cleanup_preserves_the_original_sanitized_error(self):
        manager, _runtime = self.manager("cleanup-error")

        def failed_freeze(_root, destination, **_kwargs):
            destination.mkdir(mode=0o700)
            shutil.rmtree(destination)
            raise HostRuntimeError("SOURCE_CHANGED")

        with patch("host_source_review.freeze_source", side_effect=failed_freeze):
            with self.assertRaises(HostRuntimeError) as raised:
                manager.prepare(PROJECT, TARGET, "api", "disabled", "request-cleanup")
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")
        self.assertEqual(list(manager.context_dir.iterdir()), [])
        self.assertEqual(list(manager.operations_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
