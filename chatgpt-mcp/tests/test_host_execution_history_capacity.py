"""Retained audit receipts must not exhaust the *active* execution budget.

Offline fake host access and fake runner; never launches host commands or
mutates an existing user operation history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_execution import HostExecution  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class Access:
    def __init__(self):
        self.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    def require(self, project, *, codex=False, grant_id=None):
        return {
            "project": project, "grant_id": "d" * 32,
            "expires_at": self.expires_at, "allow_codex": False,
            "scope": "installation", "lifetime": "timed", "version": 2,
        }


class HistoryCapacityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / "repo"
        repo.mkdir(mode=0o700)
        executable = root / "safe"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        self.program = executable
        self.manager = HostExecution(
            {"demo": repo}, root / "state", access=Access(),
            trusted_runtime=True, runner=lambda *_args, **_kw: None,
        )
        self.addCleanup(lambda: asyncio.run(self.manager.close()))

    def plan(self, index, **kwargs):
        values = dict(executable=str(self.program), argv=["read-only"],
                      cwd=".", request_id=f"safe-{index}")
        values.update(kwargs)
        return self.manager.prepare("demo", **values)

    def terminal(self, operation_id, *, verified=True):
        record = self.manager._load(operation_id)
        record.update(
            state="succeeded" if verified else "outcome_unknown",
            phase="COMPLETE", finished_at=datetime.now(timezone.utc).isoformat(),
            termination_verified=verified,
        )
        self.manager._save(record)

    def test_terminal_history_beyond_cap_does_not_block_new_work_or_old_lookup(self):
        first_operation_id = None
        for index in range(5):
            item = self.plan(index)
            if index == 0:
                first_operation_id = item["operation_id"]
            self.terminal(item["operation_id"])
        with mock.patch("host_execution.MAX_HISTORY", 2):
            newest = self.plan("new")
            self.assertEqual(newest["state"], "prepared")
            self.assertEqual(len(self.manager._all()), 6)
            old = self.plan(0)
            self.assertEqual(old["operation_id"], first_operation_id)
            self.assertEqual(self.manager.get("demo", newest["operation_id"])["state"],
                             "prepared")
            self.assertEqual(len(self.manager.list("demo")["executions"]), 6)
            with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
                self.plan(0, argv=["different"])

    def test_new_prepared_operations_still_have_strict_capacity(self):
        with mock.patch("host_execution.MAX_HISTORY", 2):
            one = self.plan(1)
            two = self.plan(2)
            self.assertEqual(self.plan(1)["operation_id"], one["operation_id"])
            with self.assertRaisesRegex(HostRuntimeError, "HOST_EXECUTION_PENDING_LIMIT"):
                self.plan(3)
            self.terminal(one["operation_id"])
            created = self.plan(3)
            self.assertNotEqual(created["operation_id"], two["operation_id"])
            self.assertEqual(len(self.manager._all()), 3)

    def test_unverified_outcome_unknown_counts_until_resolution(self):
        first = self.plan(1)
        second = self.plan(2)
        self.terminal(first["operation_id"], verified=False)
        with mock.patch("host_execution.MAX_HISTORY", 2):
            with self.assertRaisesRegex(HostRuntimeError, "HOST_EXECUTION_PENDING_LIMIT"):
                self.plan("denied")
            old = self.manager._load(first["operation_id"])
            old["termination_verified"] = True
            self.manager._save(old)
            accepted = self.plan("now-allowed")
            self.assertEqual(accepted["state"], "prepared")
            self.assertEqual(len(self.manager._all()), 3)

    def test_no_retained_receipts_are_deleted_or_replaced(self):
        ids = [self.plan(index)["operation_id"] for index in range(4)]
        for name in ids:
            self.terminal(name)
        before = {p.name: p.read_bytes() for p in self.manager.plans_dir.glob("*.json")}
        with mock.patch("host_execution.MAX_HISTORY", 2):
            self.plan("next")
        after = {p.name: p.read_bytes() for p in self.manager.plans_dir.glob("*.json")}
        for name, old_bytes in before.items():
            self.assertIn(name, after)
            self.assertEqual(old_bytes, after[name])

    def test_actual_501_terminal_receipts_remain_readable_and_do_not_block(self):
        import copy
        template = self.plan("template")
        original = self.manager._load(template["operation_id"])
        for i in range(501):
            item = copy.deepcopy(original)
            item.update(
                operation_id=f"{i + 1:032x}", request_id=f"safe-historical-{i}",
                state="succeeded", phase="COMPLETE",
                finished_at=datetime.now(timezone.utc).isoformat(),
                termination_verified=True,
            )
            self.manager._save(item)

        # Reproduces the production threshold with actual files, no monkeypatch.
        created = self.plan("after-overflow")
        self.assertEqual(created["state"], "prepared")
        self.assertEqual(len(self.manager._all()), 503)
        self.assertEqual(len(self.manager.list("demo")["executions"]), 100)
        self.assertEqual(self.manager.get("demo", f"{1:032x}")["state"], "succeeded")
        self.assertEqual(self.plan("historical-0")["operation_id"], f"{1:032x}")
        with self.assertRaisesRegex(HostRuntimeError, "REQUEST_ID_CONFLICT"):
            self.plan("historical-0", argv=["different"])



if __name__ == "__main__":
    unittest.main()
