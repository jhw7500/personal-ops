"""Chat approval capability tests; all authority/state is temporary and injected."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_access  # noqa: E402
from host_access import HostAccess  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class ChatHostAccessApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.other = self.root / "other"
        self.project.mkdir()
        self.other.mkdir()
        self.now = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)
        self.runtime = "a" * 64
        self.boot = "12345678-1234-1234-1234-123456789abc"
        for patch in (
            mock.patch.object(host_access, "_utcnow", side_effect=lambda: self.now),
            mock.patch.object(host_access, "_current_boot_id", side_effect=lambda: self.boot),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.access = HostAccess({"alpha": self.project, "beta": self.other},
                                 self.root / "state", trusted_runtime=lambda: self.runtime)
        self.assertIsNotNone(importlib.util.find_spec("host_access_ui"),
                             "The chat-only approval capability manager is not implemented")
        from host_access_ui import HostAccessApprovals
        self.cards = HostAccessApprovals(self.access)

    def open(self, **options):
        return self.cards.open("alpha", duration_seconds=options.get("duration_seconds", 3600),
                               allow_codex=options.get("allow_codex", False))

    @staticmethod
    def args(result):
        card = result["structuredContent"]
        return {"card_id": card["card_id"],
                "confirmation_token": result["_meta"]["host_access_confirmation"]}

    def error(self, code, function):
        with self.assertRaises(HostRuntimeError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_open_is_request_only_and_secret_is_only_in_meta(self):
        result = self.open()
        public = result["structuredContent"]
        token = result["_meta"]["host_access_confirmation"]
        self.assertEqual(public["state"], "pending")
        self.assertEqual(public["scope"], "installation")
        self.assertEqual(public["project_root"], str(self.project))
        self.assertFalse(public["host_identity"]["filesystem_isolated"])
        self.assertNotIn(token, json.dumps(public))
        self.assertNotIn(token, json.dumps(result["content"]))
        self.assertFalse(self.access.get("alpha")["active"])
        for path in (self.root / "state").rglob("*.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(token, path.read_text())

    def test_explicit_confirm_uses_exact_displayed_options(self):
        opened = self.open(duration_seconds=900, allow_codex=True)
        result = self.cards.confirm("alpha", **self.args(opened))
        self.assertEqual(result["structuredContent"]["state"], "approved")
        self.assertNotIn("_meta", result)
        granted = self.access.require("alpha", codex=True)
        self.assertEqual(granted["expires_at"], (self.now + timedelta(seconds=900)).isoformat())
        self.assertEqual(result["structuredContent"]["grant"], granted)

    def test_default_card_can_be_confirmed_after_six_days_and_grant_revoked_after_twenty_nine(self):
        opened = self.cards.open("alpha")
        self.now += timedelta(days=6)
        result = self.cards.confirm("alpha", **self.args(opened))
        grant = result["structuredContent"]["grant"]
        self.assertEqual(grant["expires_at"], (self.now + timedelta(days=30)).isoformat())
        self.now += timedelta(days=29)
        self.assertEqual(self.access.require("alpha"), grant)
        self.error("HOST_ACCESS_CARD_SPENT", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.access.revoke_web("alpha", grant_id=grant["grant_id"], confirmation="alpha")
        self.error("HOST_ACCESS_REVOKED", lambda: self.access.require("alpha"))

    def test_replay_cannot_extend_or_replace_a_grant(self):
        opened = self.open()
        self.cards.confirm("alpha", **self.args(opened))
        before = self.access.require("alpha")
        self.now += timedelta(seconds=10)
        self.error("HOST_ACCESS_CARD_SPENT", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertEqual(self.access.require("alpha"), before)

    def test_concurrent_double_confirmation_issues_exactly_one_grant(self):
        opened = self.open()
        def confirm(_index):
            try:
                return self.cards.confirm("alpha", **self.args(opened))["structuredContent"]["state"]
            except HostRuntimeError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(confirm, range(4)))
        self.assertEqual(outcomes.count("approved"), 1)
        self.assertEqual(outcomes.count("HOST_ACCESS_CARD_SPENT"), 3)

    def test_option_changes_rotate_capability_without_extending_card_lifetime(self):
        opened = self.open()
        self.now += timedelta(seconds=50)
        updated = self.cards.update("alpha", **self.args(opened),
                                    duration_seconds=600, allow_codex=True)
        self.assertNotEqual(self.args(opened)["confirmation_token"],
                            self.args(updated)["confirmation_token"])
        self.assertEqual(opened["structuredContent"]["expires_at"],
                         updated["structuredContent"]["expires_at"])
        self.assertEqual(updated["structuredContent"]["revision"], 2)
        self.error("HOST_ACCESS_CARD_DENIED", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertFalse(self.access.get("alpha")["active"])
        self.cards.confirm("alpha", **self.args(updated))
        self.assertTrue(self.access.require("alpha")["allow_codex"])

    def test_cancel_consumes_card_without_revoking_existing_grant(self):
        self.cards.confirm("alpha", **self.args(self.open()))
        before = self.access.require("alpha")
        opened = self.open(allow_codex=True)
        result = self.cards.cancel("alpha", **self.args(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.access.require("alpha"), before)
        self.error("HOST_ACCESS_CARD_SPENT", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertEqual(self.access.get("alpha")["requests"], [])

    def test_cross_project_wrong_capability_and_newer_card_fail_closed(self):
        opened = self.open()
        self.error("HOST_ACCESS_CARD_DENIED", lambda: self.cards.confirm("beta", **self.args(opened)))
        wrong = {**self.args(opened), "confirmation_token": "0" * 64}
        self.error("HOST_ACCESS_CARD_DENIED", lambda: self.cards.confirm("alpha", **wrong))
        newer = self.open()
        self.error("HOST_ACCESS_CARD_DENIED", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.cards.confirm("alpha", **self.args(newer))

    def test_expiry_and_runtime_or_boot_change_are_rejected(self):
        opened = self.open()
        self.now += timedelta(days=7)
        self.error("HOST_ACCESS_CARD_EXPIRED", lambda: self.cards.confirm("alpha", **self.args(opened)))
        opened = self.open()
        self.runtime = "b" * 64
        self.error("HOST_ACCESS_CARD_STALE", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.runtime = "a" * 64
        self.boot = "99999999-8888-7777-6666-555555555555"
        self.error("HOST_ACCESS_CARD_STALE", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertFalse(self.access.get("alpha")["active"])

    def test_invalid_options_do_not_consume_a_valid_card(self):
        opened = self.open()
        for duration in (0, 2592001, True, "600"):
            self.error("INVALID_HOST_ACCESS_DURATION", lambda: self.cards.update(
                "alpha", **self.args(opened), duration_seconds=duration, allow_codex=False))
        self.error("INVALID_HOST_ACCESS_REQUEST", lambda: self.cards.update(
            "alpha", **self.args(opened), duration_seconds=600, allow_codex="yes"))
        self.cards.confirm("alpha", **self.args(opened))

    def test_eight_hour_grant_has_a_seven_day_card_confirmation_window(self):
        opened = self.open(duration_seconds=28800)
        self.assertEqual(opened["structuredContent"]["duration_seconds"], 28800)
        self.assertEqual(opened["structuredContent"]["expires_at"],
                         (self.now + timedelta(days=7)).isoformat())
        self.cards.confirm("alpha", **self.args(opened))
        self.assertEqual(self.access.require("alpha")["expires_at"],
                         (self.now + timedelta(hours=8)).isoformat())

    def test_failed_issuer_leaves_capability_spent_and_no_grant(self):
        opened = self.open()
        with mock.patch.object(self.access, "approve_web", side_effect=HostRuntimeError("UNSAFE_RUNTIME")):
            self.error("UNSAFE_RUNTIME", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.error("HOST_ACCESS_CARD_SPENT", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertFalse(self.access.get("alpha")["active"])

    def test_project_root_or_uid_change_is_rejected(self):
        opened = self.open()
        self.access.projects["alpha"] = self.other
        self.error("HOST_ACCESS_CARD_STALE", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.access.projects["alpha"] = self.project
        with mock.patch.object(host_access.os, "getuid", return_value=1234567):
            # Owner checks on the saved state may reject the file even before binding.
            with self.assertRaises(HostRuntimeError):
                self.cards.confirm("alpha", **self.args(opened))
        self.assertFalse(self.access.get("alpha")["active"])

    def test_pending_capability_and_spent_state_survive_manager_recreation(self):
        from host_access_ui import HostAccessApprovals
        opened = self.open()
        recreated = HostAccessApprovals(self.access)
        recreated.confirm("alpha", **self.args(opened))
        self.error("HOST_ACCESS_CARD_SPENT", lambda: self.cards.confirm("alpha", **self.args(opened)))

    def test_symlink_or_public_card_state_is_rejected(self):
        opened = self.open()
        path = self.cards._path("alpha")
        path.chmod(0o644)
        self.error("HOST_ACCESS_CARD_STATE_INVALID", lambda: self.cards.confirm("alpha", **self.args(opened)))
        path.chmod(0o600)
        moved = path.with_suffix(".old")
        path.rename(moved)
        path.symlink_to(moved)
        self.error("HOST_ACCESS_CARD_STATE_INVALID", lambda: self.cards.confirm("alpha", **self.args(opened)))
        self.assertFalse(self.access.get("alpha")["active"])

    def test_untrusted_checkout_cannot_open_a_card(self):
        self.runtime = None
        self.error("UNSAFE_RUNTIME", lambda: self.open())
        self.assertFalse(self.access.get("alpha")["active"])

    def test_cancel_does_not_claim_success_for_a_request_already_approved_on_web(self):
        opened = self.open()
        request = self.access.get("alpha")["requests"][0]
        grant = self.access.approve_web("alpha", request_id=request["request_id"],
            duration_seconds=60, allow_codex=False, confirmation="alpha")
        self.error("HOST_ACCESS_CARD_REQUEST_CONSUMED", lambda: self.cards.cancel(
            "alpha", **self.args(opened)))
        self.assertEqual(self.access.require("alpha"), grant)

    def test_cancel_storage_failure_keeps_pending_card_retryable(self):
        import host_access_ui
        opened = self.open()
        save = host_access_ui._save_private_json
        def fail_request_save(path, value):
            if path == self.access._record_path("alpha"):
                raise OSError("temporary fixture storage failure")
            return save(path, value)
        with mock.patch.object(host_access_ui, "_save_private_json", side_effect=fail_request_save):
            with self.assertRaises(OSError):
                self.cards.cancel("alpha", **self.args(opened))
        result = self.cards.cancel("alpha", **self.args(opened))
        self.assertEqual(result["structuredContent"]["state"], "cancelled")
        self.assertEqual(self.access.get("alpha")["requests"], [])


class ChatApprovalDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_error_has_private_safe_correlation_diagnostics(self):
        from host_access_ui import HostAccessApprovalTools
        manager = mock.Mock()
        secret = "DO-NOT-ECHO-PRIVATE-TEST-CAPABILITY"
        manager.open.side_effect = RuntimeError(secret)
        tools = HostAccessApprovalTools({"alpha": Path("/fixture")}, lambda: None)
        tools._manager = manager
        output = io.StringIO()
        with mock.patch("sys.stderr", output):
            with self.assertRaisesRegex(ValueError, "HOST_ACCESS_CARD_UNAVAILABLE") as caught:
                await tools.call("open_host_access_approval", {"project": "alpha"})
        self.assertTrue(output.getvalue(), "Unexpected failures must retain safe diagnostics")
        event = json.loads(output.getvalue())
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertRegex(event["diagnostic_id"], "^[0-9a-f]{32}$")
        self.assertIn(event["diagnostic_id"], str(caught.exception))
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
