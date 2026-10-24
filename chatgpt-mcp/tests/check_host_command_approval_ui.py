"""Browser contract for the inline host-command approval MCP App.

The fixture never executes a host command or contacts an external service.
"""
from pathlib import Path
import os
import stat
import unittest

from playwright.sync_api import expect, sync_playwright


HTML = Path(__file__).resolve().parents[1] / "host-command-approval.html"
HOST = """<!doctype html><html><head><style>
body { margin: 0; } iframe { display: block; width: 100%; height: 42px; border: 0; }
</style></head><body><script>
const token = window.token = 'a'.repeat(64);
const direct = new URLSearchParams(location.search).get('legacy') !== '1';
const base = window.card = {
  project: 'example', component: 'host_command', profile_id: 'test-upload',
  label: 'Upload exact TEST bundle', effect: 'external_publish',
  operation_id: '1'.repeat(32), card_id: '2'.repeat(32), state: 'pending',
  plan_digest: '3'.repeat(64), source_digest: '4'.repeat(64),
  executable_sha256: '5'.repeat(64), input_count: 1, timeout_seconds: 60,
  expires_at: '2099-01-01T00:00:00Z', executes_on_approval: false,
  receipt_verified: false, ...(direct ? {supports_direct_execution: true} : {}),
  plan: {project: 'example', profile_id: 'test-upload', label: 'Upload exact TEST bundle',
    effect: 'external_publish', input_count: 1, timeout_seconds: 60,
    executable_sha256: '5'.repeat(64)}
};
window.calls = []; window.contexts = []; window.states = []; window.sizes = [];
window.failNext = false; window.mismatchNext = false; window.approvalWon = false;
window.result = value => ({content: [{type: 'text', text: 'bounded'}],
  structuredContent: JSON.parse(JSON.stringify(value))});
window.approved = () => card.supports_direct_execution === true ?
  ({...card, state: 'approved', receipt_verified: true, approved: true,
    supports_direct_execution: false,
    execution_requested: true, execution_started: true,
    plan: {state: 'running', phase: 'execute', code: null, started_at: '2026-10-20T01:02:03Z'},
    next_action: {tool: 'get_host_command', arguments: {
      project: card.project, operation_id: card.operation_id}}}) :
  ({...card, state: 'approved', receipt_verified: true, approved: true,
    next_action: {tool: 'execute_host_command', arguments: {
      project: card.project, operation_id: card.operation_id}}});
window.invoke = async (name, args) => {
  calls.push({name, args: JSON.parse(JSON.stringify(args))});
  if (failNext) { failNext = false; throw new Error('ambiguous transport failure'); }
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
      params: {...result(card), _meta: {host_command_confirmation: token}}}, '*'); return;
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


class HostCommandApprovalBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            executable = Path(cls.playwright.chromium.executable_path).resolve(strict=True)
            info = executable.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                    or info.st_mode & 0o022 or not os.access(executable, os.X_OK)):
                raise RuntimeError("UNSAFE_APPROVAL_TEST_BROWSER")
            cls.browser = cls.playwright.chromium.launch(
                headless=True, executable_path=str(executable))
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.context.add_init_script("""
          if (window !== parent) window.openai = {setWidgetState(value) {
            parent.states.push(JSON.parse(JSON.stringify(value)));
          }};
        """)
        self.context.route("https://approval.test/**", lambda route: route.fulfill(
            content_type="text/html",
            body=HTML.read_text(encoding="utf-8")
            if route.request.url.endswith("/approval") else HOST,
        ))
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
        expect(frame.get_by_role("heading", name="호스트 명령 승인")).to_be_visible()
        button_name = "승인하고 실행" if self.page.evaluate("card.supports_direct_execution === true") else "이 계획 승인"
        expect(frame.get_by_role("button", name=button_name)).to_be_enabled()
        self.page.wait_for_function("""() => {
          const frame = document.querySelector('iframe');
          return sizes.length > 0 && frame.clientHeight > 400 &&
            frame.contentDocument.documentElement.scrollHeight <= frame.clientHeight;
        }""")
        return frame

    def test_autosizes_at_mobile_and_desktop_widths_without_command_side_effect(self):
        for width in (320, 390, 1440):
            with self.subTest(width=width):
                frame = self.open(width)
                expect(frame.get_by_text("example", exact=True)).to_be_visible()
                expect(frame.get_by_text("test-upload", exact=True)).to_be_visible()
                expect(frame.get_by_text("TEST 업로드 · 외부 게시", exact=True)).to_be_visible()
                self.assertEqual(self.page.evaluate("calls"), [])
                self.assertNotIn("a" * 64, frame.locator("body").inner_text())
                self.page.reload()

    def test_one_click_approves_and_starts_execution_without_model_followup(self):
        frame = self.open()
        expect(frame.get_by_text("승인하면 고정된 명령 실행을 시작합니다.", exact=True)).to_be_visible()
        frame.get_by_role("button", name="승인하고 실행").click()
        expect(frame.get_by_role("status")).to_contain_text("실행을 시작했습니다")
        calls = self.page.evaluate("calls")
        self.assertEqual([item["name"] for item in calls], ["confirm_and_execute_host_command"])
        self.assertEqual(calls[0]["args"]["confirmation_token"], "a" * 64)
        self.assertFalse(any(item["name"] == "execute_host_command" for item in calls))
        self.assertNotIn("a" * 64, str(self.page.evaluate("contexts")))
        self.assertNotIn("a" * 64, str(self.page.evaluate("states")))
        self.assertNotIn("supports_direct_execution", str(self.page.evaluate("contexts")))
        summary = self.page.evaluate("contexts[0].structuredContent.hostCommandApproval")
        self.assertTrue(summary["execution_requested"])
        self.assertTrue(summary["execution_started"])
        self.assertEqual(summary["plan"]["state"], "running")
        self.assertNotIn("next_action", summary)
        expect(frame.get_by_role("button", name="승인하고 실행")).to_be_disabled()

    def test_missing_capability_preserves_approval_only_fallback(self):
        self.page.goto("https://approval.test/?legacy=1")
        frame = self.page.frame_locator("iframe")
        expect(frame.get_by_role("button", name="이 계획 승인")).to_be_enabled()
        expect(frame.get_by_text("승인만으로 명령을 실행하지 않습니다.", exact=True)).to_be_visible()
        frame.get_by_role("button", name="이 계획 승인").click()
        expect(frame.get_by_role("status")).to_contain_text("실행은 별도")
        self.assertEqual(self.page.evaluate("calls[0].name"), "confirm_host_command_approval")

    def test_cancel_reports_an_approval_that_won_the_race(self):
        frame = self.open()
        self.page.evaluate("approvalWon = true")
        frame.get_by_role("button", name="취소").click()
        expect(frame.get_by_role("status")).to_contain_text("이미 승인")
        expect(frame.get_by_role("status")).not_to_contain_text("취소했습니다")
        self.assertEqual(self.page.evaluate("calls[0].name"), "cancel_host_command_approval")

    def test_mismatch_and_transport_failure_fail_closed_without_retry(self):
        for flag in ("mismatchNext", "failNext"):
            with self.subTest(flag=flag):
                frame = self.open()
                self.page.evaluate(f"{flag} = true")
                frame.get_by_role("button", name="승인하고 실행").click()
                expect(frame.get_by_role("status")).to_contain_text("결과를 확정할 수 없습니다")
                expect(frame.get_by_role("button", name="승인하고 실행")).to_be_disabled()
                expect(frame.get_by_role("button", name="취소")).to_be_disabled()
                self.assertEqual(len(self.page.evaluate("calls")), 1)
                self.page.reload()


if __name__ == "__main__":
    unittest.main(verbosity=2)
