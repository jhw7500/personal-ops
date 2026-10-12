"""Exercise the self-contained UI in Chromium with an MCP Apps host bridge fixture."""

from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright


HTML = Path(__file__).resolve().parents[1] / "project-picker.html"
HOST = """<!doctype html><html><body><script>
window.calls = [];
window.contexts = {};
window.failSelection = false;
window.failContext = false;
window.listing = [
  {project: 'alpha', path: '/host/alpha'},
  {project: 'beta', path: '/host/beta'},
];
window.contextSupported = !location.search.includes('unsupported');
window.addEventListener('message', event => {
  const frame = [...document.querySelectorAll('iframe')].find(f => f.contentWindow === event.source);
  if (!frame || event.data?.jsonrpc !== '2.0') return;
  const {id, method, params} = event.data;
  window.calls.push({frame: frame.id, method, params});
  const send = data => event.source.postMessage({jsonrpc: '2.0', ...data}, '*');
  const result = data => send({id, result: data});
  if (method === 'ui/initialize') {
    if (location.search.includes('legacy')) send({id, error: {code: -32601, message: 'unsupported'}});
    else result({
      protocolVersion: '2026-01-26',
      hostCapabilities: contextSupported ? {updateModelContext: {}} : {},
      hostContext: {theme: 'light'},
    });
  }
  if (method === 'ui/notifications/initialized') send({
    method: 'ui/notifications/tool-result',
    params: {content: [], structuredContent: {projects: window.listing}},
  });
  if (method === 'tools/call') {
    if (params.name === 'select_project' && window.failSelection) {
      result({isError: true, content: [{type: 'text', text: 'selection rejected'}]});
    } else {
      const data = {content: [], structuredContent: {projects: window.listing}};
      if (params.name === 'select_project') data.structuredContent.selected_project = params.arguments.project;
      result(data);
      // Hosts may also send a result notification for UI-initiated calls.
      send({method: 'ui/notifications/tool-result', params: data});
    }
  }
  if (method === 'ui/update-model-context') {
    if (window.failContext) send({id, error: {code: -32603, message: 'context rejected'}});
    else {window.contexts[frame.id] = params; result({});}
  }
});
</script><iframe id="one" title="Conversation one" src="/picker"></iframe>
<iframe id="two" title="Conversation two" src="/picker"></iframe></body></html>
"""


class ProjectPickerBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.add_init_script("""
            window.openai = {widgetState: null, setWidgetState(value) {
                if (parent.failState) throw new Error('선택 상태 전달 실패');
                this.widgetState = value;
            }};
            if (parent.location.search.includes('no-state')) delete window.openai.setWidgetState;
            if (parent.location.search.includes('legacy')) {
                window.openai.toolOutput = {projects: parent.listing};
                window.openai.callTool = async (name, args) => {
                    parent.calls.push({frame: window.frameElement.id, method: 'tools/call',
                                       params: {name, arguments: args}});
                    if (parent.failSelection) return {isError: true};
                    const structuredContent = {projects: parent.listing};
                    if (name === 'select_project') structuredContent.selected_project = args.project;
                    return {content: [], structuredContent};
                };
            }
        """)
        self.context.route("https://picker.test/**", lambda route: route.fulfill(
            content_type="text/html",
            body=HTML.read_text(encoding="utf-8") if route.request.url.endswith("/picker") else HOST,
        ))
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def open(self, suffix=""):
        self.page.goto("https://picker.test/" + suffix)
        frame = self.page.frame_locator("#one")
        expect(frame.locator("#project")).to_be_enabled()
        return frame

    def select(self, frame, project):
        frame.get_by_label("허용된 레포").select_option(project)
        frame.get_by_role("button", name="이 대화에서 사용").click()

    def test_selection_is_explicit_and_isolated_between_conversations(self):
        one = self.open()
        two = self.page.frame_locator("#two")
        expect(one.locator("#apply")).to_be_disabled()
        self.assertEqual(self.page.evaluate("contexts"), {})
        self.assertFalse(self.page.evaluate("calls.some(c => c.method === 'tools/call')"))

        self.select(one, "alpha")
        expect(one.get_by_role("status")).to_contain_text("alpha 선택을 이 대화에 전달했습니다")
        self.select(two, "beta")
        expect(two.get_by_role("status")).to_contain_text("beta 선택을 이 대화에 전달했습니다")
        contexts = self.page.evaluate("contexts")
        self.assertEqual(contexts["one"]["structuredContent"]["selectedProject"], "alpha")
        self.assertEqual(contexts["two"]["structuredContent"]["selectedProject"], "beta")
        self.select(one, "beta")
        expect(one.get_by_role("status")).to_contain_text("beta 선택을 이 대화에 전달했습니다")
        self.assertEqual(self.page.evaluate("contexts.two"), contexts["two"])
        state = one.locator("body").evaluate("() => window.openai.widgetState")
        self.assertEqual(state["privateContent"], {"selectedProject": "beta"})
        self.assertEqual(state["modelContent"]["selectedProject"], "beta")

    def test_failed_validation_or_context_update_does_not_report_success(self):
        frame = self.open()
        self.page.evaluate("window.failSelection = true")
        self.select(frame, "alpha")
        expect(frame.get_by_role("status")).to_contain_text("이 레포를 선택할 수 없습니다")
        self.assertEqual(self.page.evaluate("contexts"), {})
        self.page.evaluate("window.failSelection = false; window.failContext = true")
        self.select(frame, "beta")
        expect(frame.get_by_role("status")).to_contain_text("ChatGPT 요청에 실패했습니다")
        self.assertEqual(self.page.evaluate("contexts"), {})
        self.assertIsNone(frame.locator("body").evaluate("() => window.openai.widgetState"))
        self.page.evaluate("window.failContext = false")
        self.select(frame, "alpha")
        expect(frame.get_by_role("status")).to_contain_text("alpha 선택을 이 대화에 전달했습니다")

    def test_unsupported_host_disables_apply(self):
        frame = self.open("?unsupported&no-state")
        frame.get_by_label("허용된 레포").select_option("alpha")
        expect(frame.locator("#apply")).to_be_disabled()
        frame.get_by_role("button", name="목록 새로고침").click()
        expect(frame.get_by_role("status")).to_contain_text("선택을 대화에 전달할 수 없습니다")
        self.assertEqual(self.page.evaluate("contexts"), {})

    def test_widget_model_content_fallback_and_failed_delivery(self):
        frame = self.open("?unsupported")
        self.page.evaluate("window.failState = true")
        self.select(frame, "alpha")
        expect(frame.get_by_role("status")).to_contain_text("선택 상태 전달 실패")
        self.assertIsNone(frame.locator("body").evaluate("() => window.openai.widgetState"))
        self.page.evaluate("window.failState = false")
        self.select(frame, "beta")
        expect(frame.get_by_role("status")).to_contain_text("beta 선택을 이 대화에 전달했습니다")
        state = frame.locator("body").evaluate("() => window.openai.widgetState")
        self.assertEqual(state["modelContent"]["selectedProject"], "beta")
        self.assertFalse(self.page.evaluate("calls.some(c => c.method === 'ui/update-model-context')"))

    def test_legacy_chatgpt_bridge_reads_initial_output_and_isolates_selection(self):
        one = self.open("?legacy")
        two = self.page.frame_locator("#two")
        self.assertFalse(self.page.evaluate("calls.some(c => c.method === 'tools/call')"))
        self.select(one, "alpha")
        expect(one.get_by_role("status")).to_contain_text("alpha 선택을 이 대화에 전달했습니다")
        self.select(two, "beta")
        expect(two.get_by_role("status")).to_contain_text("beta 선택을 이 대화에 전달했습니다")
        self.assertEqual(one.locator("body").evaluate(
            "() => window.openai.widgetState.modelContent.selectedProject"), "alpha")
        self.assertEqual(two.locator("body").evaluate(
            "() => window.openai.widgetState.modelContent.selectedProject"), "beta")
        one.get_by_role("button", name="목록 새로고침").click()
        expect(one.locator("#project")).to_have_value("alpha")
        self.assertEqual(self.page.evaluate("contexts"), {})

    def test_list_refresh_handles_empty_and_untrusted_text(self):
        frame = self.open()
        self.page.evaluate("window.listing = []")
        frame.get_by_role("button", name="목록 새로고침").click()
        expect(frame.locator("#project")).to_be_disabled()
        expect(frame.locator("#apply")).to_be_disabled()
        expect(frame.locator("option")).to_have_text("허용된 레포가 없습니다")
        self.page.evaluate("""window.listing = [{
            project: 'safe', path: '<img src=x onerror="window.compromised=true">'
        }]""")
        frame.get_by_role("button", name="목록 새로고침").click()
        frame.get_by_label("허용된 레포").select_option("safe")
        expect(frame.locator("#path")).to_contain_text("<img")
        self.assertEqual(frame.locator("img").count(), 0)
        self.assertFalse(frame.locator("body").evaluate("() => Boolean(window.compromised)"))
        # A message from a sibling frame must not be accepted as host output.
        self.page.frames[2].evaluate("""() => parent.document.querySelector('#one').contentWindow.postMessage({
            jsonrpc: '2.0', method: 'ui/notifications/tool-result',
            params: {structuredContent: {projects: [{project:'intruder',path:'/other'}]}}
        }, '*')""")
        expect(frame.locator("option[value='safe']")).to_have_count(1)
        expect(frame.locator("option[value='intruder']")).to_have_count(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
