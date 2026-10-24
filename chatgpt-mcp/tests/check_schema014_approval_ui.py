"""Chromium fixture for the Schema 014 inline MCP App approval card.

This validates the local bridge contract only; it does not claim a particular
ChatGPT web or Android build renders the resource.
"""
from pathlib import Path
import os
import stat
import unittest

from playwright.sync_api import expect, sync_playwright


HTML = Path(__file__).resolve().parents[1] / "host-operation-approval.html"
HOST = """<!doctype html><html><head><style>
body { margin: 0; } iframe { display: block; width: 100%; height: 48px; border: 0; }
</style></head><body><script>
const token = window.token = 'a'.repeat(64);
const base = window.card = {
  project: 'tossApp', target: 'tarororo-test', component: 'schema014',
  operation_id: '1'.repeat(32), card_id: '2'.repeat(32), state: 'pending',
  plan_digest: '3'.repeat(64), source_digest: '4'.repeat(64),
  schema014_sha256: '5'.repeat(64),
  predecessor_checksums: {'001.sql': '6'.repeat(64), '013.sql': '7'.repeat(64)},
  database_generation: {database: 'tarororo_test'}, expires_at: '2099-01-01T00:00:00Z',
  schema_change: '014_person_relationship_detail.sql', backup_before_change: true,
  service_restart: false, executes_on_approval: false,
  plan: {project: 'tossApp', target: 'tarororo-test', operation_id: '1'.repeat(32),
    component: 'schema014', state: 'pending_host_approval', changed_paths: ['server/migrations/014.sql']}
};
window.calls = []; window.contexts = []; window.sizes = [];
window.failNext = false; window.mismatchNext = false;
window.result = value => ({content: [{type: 'text', text: JSON.stringify(value)}],
  structuredContent: JSON.parse(JSON.stringify(value))});
window.invoke = async (name, args) => {
  calls.push({name, args: JSON.parse(JSON.stringify(args))});
  if (failNext) { failNext = false; throw new Error('ambiguous transport failure'); }
  if (mismatchNext) {
    mismatchNext = false;
    return result({...card, operation_id: '9'.repeat(32)});
  }
  if (args.confirmation_token !== token || args.operation_id !== card.operation_id ||
      args.card_id !== card.card_id) return {isError: true, content: []};
  card = {...card, state: name.startsWith('confirm') ? 'approved' : 'cancelled',
    approved: name.startsWith('confirm')};
  return result(card);
};
window.addEventListener('message', event => {
  if (event.source !== document.querySelector('iframe').contentWindow || event.data?.jsonrpc !== '2.0') return;
  const {id, method, params} = event.data;
  if (method === 'ui/notifications/size-changed') {
    sizes.push(params); document.querySelector('iframe').style.height = params.height + 'px'; return;
  }
  if (method === 'ui/update-model-context') {
    contexts.push(JSON.parse(JSON.stringify(params)));
    event.source.postMessage({jsonrpc: '2.0', id, result: {}}, '*');
  }
});
</script><iframe title="Schema 014 approval" src="/approval"></iframe></body></html>"""


class Schema014ApprovalBrowserTest(unittest.TestCase):
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
          if (window !== parent) window.openai = {
            toolOutput: parent.result(parent.card).structuredContent,
            toolResponseMetadata: {schema014_confirmation: parent.token},
            callTool: (name, args) => parent.invoke(name, args),
            setWidgetState(value) { parent.contexts.push(JSON.parse(JSON.stringify(value))); }
          };
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
        expect(frame.get_by_role("heading", name="Schema 014 적용 승인")).to_be_visible()
        expect(frame.get_by_role("button", name="이 계획 승인")).to_be_enabled()
        self.page.wait_for_function("""() => {
          const frame = document.querySelector('iframe');
          return sizes.length > 0 && frame.clientHeight > 400 &&
            frame.contentDocument.documentElement.scrollHeight <= frame.clientHeight;
        }""")
        return frame

    def test_tiny_frame_autosizes_and_renders_exact_plan_at_mobile_widths(self):
        for width in (320, 390):
            with self.subTest(width=width):
                frame = self.open(width)
                expect(frame.get_by_text("tossApp / tarororo-test")).to_be_visible()
                expect(frame.get_by_text("014_person_relationship_detail.sql")).to_be_visible()
                expect(frame.get_by_text("3" * 64)).to_be_visible()
                expect(frame.get_by_text("4" * 64)).to_be_visible()
                expect(frame.get_by_text("5" * 64)).to_be_visible()
                expect(frame.get_by_text("2개 checksum 고정")).to_be_visible()
                self.assertEqual(self.page.evaluate("calls"), [])
                self.assertNotIn("a" * 64, frame.locator("body").inner_text())
                self.page.reload()

    def test_explicit_confirm_uses_capability_without_execution_or_model_leak(self):
        frame = self.open()
        frame.get_by_role("button", name="이 계획 승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인을 확인")
        expect(frame.get_by_role("status")).not_to_contain_text("아직 시작되지 않았")
        calls = self.page.evaluate("calls")
        self.assertEqual([item["name"] for item in calls], ["confirm_schema014_approval"])
        self.assertEqual(calls[0]["args"]["confirmation_token"], "a" * 64)
        self.assertFalse(any("execute" in item["name"] for item in calls))
        expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()
        self.assertNotIn("a" * 64, str(self.page.evaluate("contexts")))

    def test_cancel_is_explicit_and_terminal(self):
        frame = self.open()
        frame.get_by_role("button", name="취소").click()
        expect(frame.get_by_role("status")).to_contain_text("승인 요청을 취소했습니다")
        self.assertEqual(self.page.evaluate("calls[0].name"), "cancel_schema014_approval")
        expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()

    def test_mismatch_and_transport_failure_fail_closed_without_retry(self):
        for flag in ("mismatchNext", "failNext"):
            with self.subTest(flag=flag):
                frame = self.open()
                self.page.evaluate(f"{flag} = true")
                frame.get_by_role("button", name="이 계획 승인").click()
                expect(frame.get_by_role("status")).to_contain_text("결과를 확정할 수 없습니다")
                expect(frame.get_by_role("button", name="이 계획 승인")).to_be_disabled()
                expect(frame.get_by_role("button", name="취소")).to_be_disabled()
                self.assertEqual(len(self.page.evaluate("calls")), 1)
                self.page.reload()


if __name__ == "__main__":
    unittest.main()
