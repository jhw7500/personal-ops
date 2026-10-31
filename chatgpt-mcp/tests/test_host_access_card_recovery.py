"""Fail-closed recovery for orphaned owner-private host approval cards.

Only an explicit recovery-flagged request may archive a corrupt card and then
create a NEW pending approval. In no case does recovery issue host authority.
All tests use a private temporary state root, never the real host state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_access
import host_access_ui
from host_access import HostAccess
from host_access_ui import HostAccessApprovals, HostAccessApprovalTools, tool_specs
from host_runtime_config import HostRuntimeError


class HostAccessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="card-recovery-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
        self.current_runtime = "a" * 64
        self.boot = "12345678-1234-1234-1234-123456789abc"
        for patch in (
            mock.patch.object(host_access, "_utcnow", side_effect=lambda: self.now),
            mock.patch.object(host_access, "_current_boot_id", side_effect=lambda: self.boot),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.access = HostAccess({"alpha": self.repo}, self.root / "state",
                                 trusted_runtime=lambda: self.current_runtime)
        self.cards = HostAccessApprovals(self.access)

    def reject(self, code, callback):
        with self.assertRaises(HostRuntimeError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def opened(self):
        return self.cards.open("alpha", duration_seconds=1800, allow_codex=False)

    def corrupt(self):
        path = self.cards._path("alpha")
        data = json.loads(path.read_text("utf8"))
        data.pop("token_digest")
        path.write_text(json.dumps(data), encoding="utf8")
        path.chmod(0o600)
        return path

    def test_requires_opt_in_and_denies_unexpired_request(self):
        self.opened()
        path = self.corrupt()
        original = path.read_bytes()
        self.reject("HOST_ACCESS_CARD_STATE_INVALID", self.opened)
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.access.get("alpha")["requests"]), 1)
        self.assertFalse(self.access.get("alpha")["active"])

    def test_expired_pending_card_is_archived_and_new_card_still_requires_click(self):
        opened = self.opened()
        old_token = opened["_meta"]["host_access_confirmation"]
        path = self.corrupt()
        before = path.read_bytes()
        self.now += timedelta(days=8)
        self.assertEqual(self.access.get("alpha")["requests"], [])
        replacement = self.cards.open("alpha", duration_seconds=1800,
                                      allow_codex=False, recover_invalid_card=True)
        self.assertEqual(replacement["structuredContent"]["state"], "pending")
        self.assertNotEqual(replacement["_meta"]["host_access_confirmation"], old_token)
        self.assertFalse(self.access.get("alpha")["active"])
        archived = list(path.parent.glob(path.stem + ".rejected-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), before)
        self.assertEqual(stat.S_IMODE(archived[0].stat().st_mode), 0o600)
        self.assertEqual(len(self.access.get("alpha")["requests"]), 1)
        self.reject("HOST_ACCESS_CARD_DENIED", lambda: self.cards.confirm("alpha",
            card_id=opened["structuredContent"]["card_id"], confirmation_token=old_token))
        new_token = replacement["_meta"]["host_access_confirmation"]
        confirmed = self.cards.confirm("alpha",
            card_id=replacement["structuredContent"]["card_id"],
            confirmation_token=new_token)
        self.assertEqual(confirmed["structuredContent"]["state"], "approved")
        self.assertTrue(self.access.get("alpha")["active"])
        self.assertFalse(self.access.require("alpha", codex=False)["allow_codex"])

    def test_unexpired_grant_remains_protected_even_with_bad_card(self):
        opened = self.opened()
        self.cards.confirm("alpha",
            card_id=opened["structuredContent"]["card_id"],
            confirmation_token=opened["_meta"]["host_access_confirmation"])
        path = self.corrupt()
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertTrue(path.exists())
        self.assertTrue(self.access.get("alpha")["active"])

    def test_exposed_permissions_cannot_be_recovered(self):
        self.opened()
        path = self.corrupt()
        self.now += timedelta(days=8)
        path.chmod(0o644)
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_symlink_and_hardlink_are_not_recovered(self):
        self.opened()
        path = self.corrupt()
        self.now += timedelta(days=8)
        old = path.with_name("old-private-card")
        path.rename(old)
        path.symlink_to(old)
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        path.unlink()
        os.link(old, path)
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertEqual(path.stat().st_nlink, 2)

    def test_corrupt_authority_ledger_is_not_reset(self):
        self.opened()
        self.corrupt()
        self.now += timedelta(days=8)
        record = self.access._record_path("alpha")
        record.write_text("{}", encoding="utf8")
        record.chmod(0o600)
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertTrue(self.cards._path("alpha").exists())

    def test_recovery_flag_is_strictly_bool_and_never_mutates_on_error(self):
        self.opened()
        path = self.corrupt()
        before = path.read_bytes()
        for flag in (1, "true", None):
            self.reject("INVALID_HOST_ACCESS_CARD_ARGUMENTS",
                lambda: self.cards.open("alpha", recover_invalid_card=flag))
        self.assertEqual(path.read_bytes(), before)

    def test_tool_contract_limits_recovery_flag_to_new_card_request(self):
        tools = {x["name"]:x for x in tool_specs({"alpha":self.repo})}
        open_schema=tools["open_host_access_approval"]["inputSchema"]
        self.assertEqual(open_schema["properties"]["recover_invalid_card"],{"type":"boolean"})
        self.assertNotIn("recover_invalid_card",
                         tools["confirm_host_access_approval"]["inputSchema"]["properties"])
        validator=HostAccessApprovalTools({"alpha":self.repo}, lambda:self.access)
        validator.validate_arguments("open_host_access_approval",
                                     {"project":"alpha","recover_invalid_card":True})
        with self.assertRaises(HostRuntimeError):
            validator.validate_arguments("open_host_access_approval",
                                         {"project":"alpha","recover_invalid_card":"true"})

    def test_archive_collision_cannot_overwrite_private_evidence(self):
        self.opened()
        path = self.corrupt()
        before = path.read_bytes()
        self.now += timedelta(days=8)
        archive = path.with_name(path.stem + ".rejected-" + "f" * 32 + ".json")
        archive.write_text("archived-sentinel", encoding="utf8")
        archive.chmod(0o600)
        with mock.patch.object(host_access_ui.secrets, "token_hex", return_value="f" * 32):
            self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                        lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertEqual(archive.read_text(), "archived-sentinel")
        self.assertEqual(path.read_bytes(), before)

    def test_stale_but_unexpired_issuer_request_is_not_silently_removed(self):
        self.opened()
        path = self.corrupt()
        self.current_runtime = "b" * 64
        self.assertEqual(self.access.get("alpha")["requests"], [])
        self.reject("HOST_ACCESS_CARD_RECOVERY_DENIED",
                    lambda: self.cards.open("alpha", recover_invalid_card=True))
        self.assertTrue(path.exists())
        self.assertEqual(len(self.access._load("alpha", self.repo)["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
