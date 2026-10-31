#!/usr/bin/env python3
"""Real-browser checks for the owner-only runtime rollout approval UI."""
from __future__ import annotations

# Run with the browser interpreter; MCP discovery must not import Playwright.
if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    import json
    from pathlib import Path
    import shutil
    import sys
    import tempfile
    import threading
    import unittest
    from unittest import mock

    from playwright.sync_api import expect, sync_playwright

    MODULE_DIR = Path(__file__).resolve().parents[1]
    REPOSITORY = MODULE_DIR.parent
    sys.path.insert(0, str(MODULE_DIR))

    import runtime_rollout_web  # noqa: E402
    from admin_server import create_server  # noqa: E402
    from runtime_rollout_web import RuntimeRolloutWeb  # noqa: E402


    PROJECT = "personal-ops"


    class RuntimeRolloutBrowserTest(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)

        @classmethod
        def tearDownClass(cls):
            cls.browser.close()
            cls.playwright.stop()

        def setUp(self):
            self.temporary = tempfile.TemporaryDirectory(prefix="runtime-rollout-ui-")
            self.addCleanup(self.temporary.cleanup)
            self.base = Path(self.temporary.name)
            self.repo = self.base / PROJECT
            shutil.copytree(
                REPOSITORY / "chatgpt-mcp", self.repo / "chatgpt-mcp",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(REPOSITORY / "work-dashboard", self.repo / "work-dashboard")
            self.launched = []

            def observe(unit):
                return {
                    "unit": unit, "load_state": "loaded", "transient": True,
                    "active_state": "active", "sub_state": "running", "result": "success",
                }

            self.manager = RuntimeRolloutWeb(
                {PROJECT: self.repo}, state_dir=self.base / "rollout-state",
                launcher=lambda command: self.launched.append(command) or True,
                observer=observe,
                runtime_reader=lambda: "a" * 64,
                boot_reader=lambda: "11111111-2222-3333-4444-555555555555",
                uid_reader=lambda: 1000,
                test_only=True,
            )
            token_path = self.base / "private" / "admin-token"
            token_path.parent.mkdir(mode=0o700)
            self.token = "q" * 48
            token_path.write_text(self.token + "\n")
            token_path.chmod(0o600)
            self.server = create_server(
                {PROJECT: self.repo}, self.base / "admin-state",
                token_path=token_path, port=0,
                runtime_rollout_factory=lambda *_args, **_kwargs: self.manager,
            )
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.addCleanup(self._stop_server)
            self.context = self.browser.new_context(viewport={"width": 1440, "height": 900})
            self.addCleanup(self.context.close)
            self.page = self.context.new_page()
            self.page_errors = []
            self.requests = []
            self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
            self.page.on("request", lambda request: self.requests.append(request))
            self.origin = f"http://127.0.0.1:{self.server.server_port}"

        def tearDown(self):
            self.assertEqual(self.page_errors, [])

        def _stop_server(self):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

        def prepare(self, operation_id="2" * 32, nonce="b" * 64, request_id="ui-plan"):
            with mock.patch.object(
                runtime_rollout_web.secrets, "token_hex",
                side_effect=[operation_id, nonce, "e" * 32],
            ):
                return self.manager.prepare(PROJECT, request_id)

        def login(self, operation_id):
            self.page.goto(self.origin + "/runtime-rollouts?operation_id=" + operation_id)
            expect(self.page.locator("#login")).to_be_visible()
            expect(self.page.locator("#dashboard")).to_be_hidden()
            self.page.locator("#token").fill(self.token)
            self.page.locator("#login-form button").click()
            expect(self.page.locator("#connection")).to_have_text("호스트 연결됨")
            expect(self.page.locator("#view-access")).to_be_visible()
            expect(self.page.locator("#nav-access")).to_have_attribute("aria-selected", "true")

        def exact_gets(self, operation_id):
            suffix = "/api/runtime-rollout?project=personal-ops&operation_id=" + operation_id
            return [request for request in self.requests if request.url.endswith(suffix)]

        def test_deep_link_survives_login_opens_access_and_fetches_exact_plan(self):
            first = self.prepare("1" * 32, "a" * 64, "first-terminal")
            stored = self.manager._load(first["operation_id"])
            stored.update(state="failed", phase="COMPLETE", code="FIXTURE_TERMINAL",
                          finished_at=datetime.now(timezone.utc).isoformat())
            self.manager._save(stored)
            requested = self.prepare("2" * 32, "b" * 64, "requested-plan")

            self.login(requested["operation_id"])

            expect(self.page.locator("#runtime-rollout-plan")).to_have_value(requested["operation_id"])
            expect(self.page.locator("#runtime-rollout-details")).to_contain_text(
                requested["target_snapshot"])
            self.assertTrue(self.exact_gets(requested["operation_id"]))
            self.assertNotIn(self.token, self.page.locator("body").inner_text())

        def test_missing_requested_plan_fails_closed_then_allows_manual_selection(self):
            available = self.prepare()
            missing = "f" * 32

            self.login(missing)

            expect(self.page.locator("#runtime-rollout-plan")).to_have_value("")
            expect(self.page.locator("#runtime-rollout-approve")).to_be_disabled()
            expect(self.page.locator("#runtime-rollout-status")).to_contain_text(
                "링크의 런타임 계획을 찾을 수 없습니다")
            self.assertTrue(self.exact_gets(missing))
            self.page.locator("#runtime-rollout-plan").select_option(available["operation_id"])
            expect(self.page.locator("#runtime-rollout-details")).to_contain_text(
                available["target_snapshot"])
            expect(self.page.locator("#runtime-rollout-approve")).to_be_enabled()

        def test_expired_plan_cannot_be_approved(self):
            old = (datetime.now(timezone.utc) - runtime_rollout_web.PLAN_TTL
                   - timedelta(seconds=1))
            with mock.patch.object(runtime_rollout_web, "_utc", return_value=old):
                expired = self.prepare()

            self.login(expired["operation_id"])

            expect(self.page.locator("#runtime-rollout-plan")).to_have_value(expired["operation_id"])
            expect(self.page.locator("#runtime-rollout-details")).to_contain_text("만료")
            expect(self.page.locator("#runtime-rollout-approve")).to_be_disabled()
            self.assertFalse(any(request.method == "POST" and
                                 request.url.endswith("/api/runtime-rollout-approve?project=personal-ops")
                                 for request in self.requests))

        def test_approval_posts_exact_frozen_fields_and_never_executes(self):
            plan = self.prepare()
            self.login(plan["operation_id"])
            self.page.locator("#runtime-rollout-confirmation").fill(plan["operation_id"])
            self.page.locator("#runtime-rollout-token").fill(self.token)
            self.page.locator("#runtime-rollout-approve").click()

            expect(self.page.locator("#runtime-rollout-status")).to_contain_text(
                "채팅에서 별도 실행을 요청")
            approval_requests = [
                request for request in self.requests
                if request.method == "POST" and
                request.url.endswith("/api/runtime-rollout-approve?project=personal-ops")
            ]
            self.assertEqual(len(approval_requests), 1)
            self.assertEqual(approval_requests[0].post_data_json, {
                "operation_id": plan["operation_id"],
                "plan_digest": plan["plan_digest"],
                "source_digest": plan["source_digest"],
                "confirmation": plan["operation_id"],
                "admin_token": self.token,
            })
            self.assertEqual(self.manager.get(PROJECT, plan["operation_id"])["state"], "approved")
            self.assertEqual(self.launched, [])
            expect(self.page.locator("#runtime-rollout-confirmation")).to_have_value("")
            expect(self.page.locator("#runtime-rollout-token")).to_have_value("")

        def test_review_result_is_visible_without_html_execution(self):
            review = self.prepare()
            self.manager.approve_web(
                PROJECT, operation_id=review["operation_id"],
                plan_digest=review["plan_digest"], source_digest=review["source_digest"],
                confirmation=review["operation_id"],
            )
            self.manager.execute(PROJECT, review["operation_id"])
            self.manager.claim_for_worker(review["operation_id"])
            payload = '<img src=x onerror="window.runtimeRolloutPwned=1">'
            finished = self.manager.finish_from_worker(review["operation_id"], {
                "status": "outcome_unknown", "snapshot": payload,
                "installed": True, "switched": False, "code": "FIXTURE_REVIEW",
            })

            self.login(review["operation_id"])

            details = self.page.locator("#runtime-rollout-details")
            expect(details).to_contain_text("결과 검토 필요")
            self.assertEqual(json.loads(details.inner_text())["result"]["snapshot"], payload)
            self.assertEqual(self.page.locator("#runtime-rollout-details img").count(), 0)
            self.assertIsNone(self.page.evaluate("window.runtimeRolloutPwned"))
            self.assertTrue(finished["outcome_requires_review"])

        def test_overdue_running_state_is_visible(self):
            overdue = self.prepare()
            self.manager.approve_web(
                PROJECT, operation_id=overdue["operation_id"],
                plan_digest=overdue["plan_digest"], source_digest=overdue["source_digest"],
                confirmation=overdue["operation_id"],
            )
            self.manager.execute(PROJECT, overdue["operation_id"])
            self.manager.claim_for_worker(overdue["operation_id"])
            stored = self.manager._load(overdue["operation_id"])
            stored["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=23)).isoformat()
            self.manager._save(stored)
            self.login(overdue["operation_id"])
            expect(self.page.locator("#runtime-rollout-details")).to_contain_text("실행 시간 초과 관측")

        def test_runtime_approval_view_has_no_horizontal_overflow(self):
            plan = self.prepare()
            self.login(plan["operation_id"])
            for width in (1440, 390, 320):
                self.page.set_viewport_size({"width": width, "height": 900})
                expect(self.page.locator("#runtime-rollout-approval")).to_be_visible()
                self.assertLessEqual(
                    self.page.evaluate("document.documentElement.scrollWidth"), width,
                    f"runtime rollout approval overflows at {width}px",
                )

    unittest.main()
