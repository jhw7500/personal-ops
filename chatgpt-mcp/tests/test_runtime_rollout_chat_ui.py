"""Standalone browser contract; no Playwright import during MCP test discovery."""

if __name__ == "__main__":
    """Browser contract for the inline host-command approval MCP App.

    The fixture never executes a host command or contacts an external service.
    """

    from pathlib import Path
    import os
    import stat
    import unittest

    from playwright.sync_api import expect, sync_playwright

    HTML = Path(__file__).resolve().parents[1] / "runtime-rollout-approval.html"
    HOST = """<!doctype html><html><head><style>
    body { margin: 0; } iframe { display: block; width: 100%; height: 42px; border: 0; }
    </style></head><body><script>
    const token = window.token = 'a'.repeat(64);
    const base = window.card = {
      project: 'personal-ops', component: 'runtime_rollout', target_snapshot: '6'.repeat(20),
      operation_id: '1'.repeat(32), card_id: '2'.repeat(32), state: 'pending',
      plan_digest: '3'.repeat(64), source_digest: '4'.repeat(64), control_digest: '5'.repeat(64), source_files: 213,
      expires_at: '2099-01-01T00:00:00Z', executes_on_approval: false, receipt_verified: false,
      impact: {restart: ['personal-ops-tunnel.service','personal-ops-admin.service'], preserve: ['API','Worker','PostgreSQL','Toss TEST bundle']},
      plan: {project: 'personal-ops', operation_id: '1'.repeat(32), plan_digest: '3'.repeat(64), source_digest: '4'.repeat(64), control_digest: '5'.repeat(64), target_snapshot: '6'.repeat(20), expires_at:'2099-01-01T00:00:00Z'}
    };
    window.calls = []; window.contexts = []; window.states = []; window.sizes = [];
    window.failNext = false; window.mismatchNext = false; window.approvalWon = false;
    window.result = value => ({content: [{type: 'text', text: 'bounded'}],
      structuredContent: JSON.parse(JSON.stringify(value))});
    window.approved = () => ({...card, state: 'approved', receipt_verified: true, approved: true,
      next_action: {tool: 'execute_runtime_rollout', arguments: {
        project: card.project, operation_id: card.operation_id}}});
    window.invoke = async (name, args) => {
      calls.push({name, args: JSON.parse(JSON.stringify(args))});
      if (failNext) { failNext = false; throw new Error('ambiguous transport failure'); }
      if (window.pinDrift) { const next=approved(); next.plan_digest="f".repeat(64); next.plan={...next.plan,plan_digest:next.plan_digest}; return result(next); }
      if (mismatchNext) { mismatchNext = false; return result({...approved(), operation_id: '9'.repeat(32)}); }
      if (args.confirmation_token !== token || args.operation_id !== card.operation_id ||
          args.card_id !== card.card_id) return {isError: true, content: []};
      if (name.startsWith('confirm') || approvalWon) card = approved();
      else card = {...card, state: 'cancelled', approved: false};
      return result(card);
    };
    window.addEventListener('message', event => {
      if (event.source !== document.querySelector('iframe').contentWindow || event.data?.jsonrpc !== '2.0') return;
      const {id, method, params} = event.data;
      if (method === 'ui/notifications/size-changed') {
        sizes.push(params); document.querySelector('iframe').style.height = params.height + 'px'; return;
      }
      if (method === 'ui/initialize') {
        event.source.postMessage({jsonrpc: '2.0', id, result: {protocolVersion: '2026-01-26',
          hostCapabilities: {updateModelContext: {}}, hostContext: {theme: 'light'}}}, '*'); return;
      }
      if (method === 'ui/notifications/initialized') {
        event.source.postMessage({jsonrpc: '2.0', method: 'ui/notifications/tool-result',
          params: {...result(card), _meta: {runtime_rollout_confirmation: token}}}, '*'); return;
      }
      if (method === 'tools/call') {
        Promise.resolve(invoke(params.name, params.arguments)).then(value =>
          event.source.postMessage({jsonrpc: '2.0', id, result: value}, '*'), () =>
          event.source.postMessage({jsonrpc: '2.0', id, error: {code: -32603, message: 'lost'}}, '*'));
        return;
      }
      if (method === 'ui/update-model-context') {
        contexts.push(JSON.parse(JSON.stringify(params)));
        event.source.postMessage({jsonrpc: '2.0', id, result: {}}, '*');
      }
    });
    </script><iframe title="Host command approval" src="/approval"></iframe></body></html>"""

    class RuntimeRolloutApprovalBrowserTest(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.playwright = sync_playwright().start()
            try:
                executable = Path(cls.playwright.chromium.executable_path).resolve(
                    strict=True
                )
                info = executable.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid not in {0, os.getuid()}
                    or info.st_mode & 0o022
                    or not os.access(executable, os.X_OK)
                ):
                    raise RuntimeError("UNSAFE_APPROVAL_TEST_BROWSER")
                cls.browser = cls.playwright.chromium.launch(
                    headless=True, executable_path=str(executable)
                )
            except Exception:
                cls.playwright.stop()
                raise

        @classmethod
        def tearDownClass(cls):
            cls.browser.close()
            cls.playwright.stop()

        def setUp(self):
            self.context = self.browser.new_context(
                viewport={"width": 390, "height": 844}
            )
            self.context.add_init_script("""
              if (window !== parent) window.openai = {setWidgetState(value) {
                parent.states.push(JSON.parse(JSON.stringify(value)));
              }};
            """)
            self.context.route(
                "https://approval.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=HTML.read_text(encoding="utf-8")
                    if route.request.url.endswith("/approval")
                    else HOST,
                ),
            )
            self.page = self.context.new_page()
            self.errors = []
            self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def tearDown(self):
            self.context.close()
            self.assertEqual(self.errors, [])

        def open(self, width=390):
            self.page.set_viewport_size({"width": width, "height": 844})
            self.page.goto("https://approval.test/")
            frame = self.page.frame_locator("iframe")
            expect(
                frame.get_by_role("heading", name="MCP 런타임 전환 승인")
            ).to_be_visible()
            expect(frame.get_by_role("button", name="이 계획 승인")).to_be_enabled()
            self.page.wait_for_function("""() => {
              const frame = document.querySelector('iframe');
              return sizes.length > 0 && frame.clientHeight > 400 &&
                frame.contentDocument.documentElement.scrollHeight <= frame.clientHeight;
            }""")
            return frame

        def test_autosizes_at_mobile_and_desktop_widths_without_command_side_effect(
            self,
        ):
            for width in (320, 390, 1440):
                with self.subTest(width=width):
                    frame = self.open(width)
                    expect(
                        frame.get_by_text("personal-ops", exact=True)
                    ).to_be_visible()
                    expect(frame.get_by_text("6" * 20, exact=True)).to_be_visible()
                    expect(
                        frame.get_by_text(
                            "personal-ops-tunnel.service · personal-ops-admin.service",
                            exact=True,
                        )
                    ).to_be_visible()
                    self.assertEqual(self.page.evaluate("calls"), [])
                    self.assertNotIn("a" * 64, frame.locator("body").inner_text())
                    self.page.reload()

        def test_confirm_issues_receipt_only_and_keeps_capability_out_of_context(self):
            frame = self.open()
            frame.get_by_role("button", name="이 계획 승인").click()
            expect(frame.get_by_role("status")).to_contain_text("승인 영수증")
            expect(frame.get_by_role("status")).to_contain_text("기존 실행 요청")
            calls = self.page.evaluate("calls")
            self.assertEqual(
                [item["name"] for item in calls], ["confirm_runtime_rollout_approval"]
            )
            self.assertEqual(calls[0]["args"]["confirmation_token"], "a" * 64)
            self.assertFalse(any("execute" in item["name"] for item in calls))
            self.assertNotIn("a" * 64, str(self.page.evaluate("contexts")))
            self.assertNotIn("a" * 64, str(self.page.evaluate("states")))
            self.assertNotIn("next_action", str(self.page.evaluate("contexts")))
            expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()

        def test_cancel_reports_an_approval_that_won_the_race(self):
            frame = self.open()
            self.page.evaluate("approvalWon = true")
            frame.get_by_role("button", name="취소").click()
            expect(frame.get_by_role("status")).to_contain_text("이미 승인")
            expect(frame.get_by_role("status")).not_to_contain_text("취소했습니다")
            self.assertEqual(
                self.page.evaluate("calls[0].name"), "cancel_runtime_rollout_approval"
            )

        def test_mismatch_and_transport_failure_fail_closed_without_retry(self):
            for flag in ("mismatchNext", "failNext"):
                with self.subTest(flag=flag):
                    frame = self.open()
                    self.page.evaluate(f"{flag} = true")
                    frame.get_by_role("button", name="이 계획 승인").click()
                    expect(frame.get_by_role("status")).to_contain_text(
                        "결과를 확정할 수 없습니다"
                    )
                    expect(
                        frame.get_by_role("button", name="이 계획 승인")
                    ).to_be_disabled()
                    expect(frame.get_by_role("button", name="취소")).to_be_disabled()
                    self.assertEqual(len(self.page.evaluate("calls")), 1)
                    self.page.reload()

        def test_same_card_with_changed_plan_digest_is_rejected(self):
            frame = self.open()
            self.page.evaluate("window.pinDrift=true")
            frame.get_by_role("button", name="이 계획 승인").click()
            expect(frame.get_by_role("status")).to_contain_text(
                "결과를 확정할 수 없습니다"
            )
            self.assertEqual(len(self.page.evaluate("calls")), 1)
            self.assertEqual(self.page.evaluate("contexts"), [])
            expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()

        def test_missing_hidden_capability_never_enables_approval(self):
            html = HOST.replace(
                "_meta: {runtime_rollout_confirmation: token}", "_meta: {}"
            )
            self.context.unroute("https://approval.test/**")
            self.context.route(
                "https://approval.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=HTML.read_text()
                    if route.request.url.endswith("/approval")
                    else html,
                ),
            )
            self.page.goto("https://approval.test/")
            frame = self.page.frame_locator("iframe")
            expect(frame.get_by_role("status")).to_contain_text("확인 정보가 없습니다")
            expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()
            self.assertEqual(self.page.evaluate("calls"), [])

    if __name__ == "__main__":
        unittest.main(verbosity=2)
