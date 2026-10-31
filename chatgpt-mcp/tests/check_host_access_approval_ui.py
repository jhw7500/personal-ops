"""Local MCP Apps bridge fixture for the HostAccess approval card.

This verifies the self-contained UI contract in Chromium. It is not proof that a
particular ChatGPT web or Android release renders the resource.
"""

from pathlib import Path
import os
import stat
import unittest

from playwright.sync_api import expect, sync_playwright


HTML = Path(__file__).resolve().parents[1] / "host-access-approval.html"
HOST = """<!doctype html><html><head><style>
body { margin: 0; }
iframe { display: block; width: min(100%, 600px); height: 80px; border: 0; }
</style></head><body><script>
const future = '2099-01-01T00:00:00Z';
const cards = window.cards = {
  one: {project: 'personal-ops', scope: 'installation', card_id: '11111111111111111111111111111111',
    revision: 1, state: 'pending', expires_at: future, project_root: '/srv/personal-ops',
    duration_seconds: 2592000, allow_codex: true,
    host_identity: {hostname: 'cantopsbuildserver', user: 'jhw', uid: 1000,
      groups: ['jhw', 'docker'], filesystem_isolated: false,
      effective_access: 'host_user_filesystem_network_groups'},
    secret: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'},
  two: {project: 'tossApp', scope: 'installation', card_id: '22222222222222222222222222222222',
    revision: 1, state: 'pending', expires_at: future, project_root: '/srv/tossApp',
    duration_seconds: 3600, allow_codex: false,
    host_identity: {hostname: 'cantopsbuildserver', user: 'jhw', uid: 1000,
      groups: ['jhw'], filesystem_isolated: false,
      effective_access: 'host_user_filesystem_network_groups'},
    secret: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'},
};
window.calls = [];
window.contexts = {};
window.states = {};
window.failNext = false;
window.returnMismatchedNext = false;
window.lastPublic = null;
window.sizes = [];

function publicCard(card) {
  const {secret, ...value} = card;
  return JSON.parse(JSON.stringify(value));
}
function resultFor(card) {
  const result = {content: [{type: 'text', text: JSON.stringify(publicCard(card))}],
    structuredContent: publicCard(card)};
  if (card.state === 'pending') result._meta = {host_access_confirmation: card.secret};
  return result;
}
function frameFor(source) {
  return [...document.querySelectorAll('iframe')].find(frame => frame.contentWindow === source);
}
function invoke(frameId, name, args) {
  const card = cards[frameId];
  if (!card || args.project !== card.project || args.card_id !== card.card_id ||
      args.confirmation_token !== card.secret) {
    return {isError: true, content: [{type: 'text', text: 'HOST_ACCESS_CONFIRMATION_INVALID'}]};
  }
  if (name === 'update_host_access_approval') {
    card.revision += 1;
    card.duration_seconds = args.duration_seconds;
    card.allow_codex = args.allow_codex;
    card.secret = frameId === 'one' ? 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' :
      'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd';
  } else if (name === 'confirm_host_access_approval') {
    card.state = 'approved';
    card.grant = {grant_id: '33333333333333333333333333333333', expires_at: future,
      allow_codex: card.allow_codex, project: card.project, scope: 'installation'};
  } else if (name === 'cancel_host_access_approval') {
    card.state = 'cancelled';
  }
  return resultFor(card);
}
window.addEventListener('message', event => {
  const frame = frameFor(event.source);
  if (!frame || event.data?.jsonrpc !== '2.0') return;
  const {id, method, params} = event.data;
  window.calls.push({frame: frame.id, method, params});
  if (method === 'ui/notifications/size-changed') {
    window.sizes.push({frame: frame.id, params, hasId: id !== undefined});
    if (location.search.includes('autosize') && Number.isFinite(params?.height)) {
      frame.style.height = params.height + 'px';
    }
  }
  const send = data => event.source.postMessage({jsonrpc: '2.0', ...data}, '*');
  const reply = result => send({id, result});
  if (method === 'ui/initialize') {
    if (location.search.includes('legacy')) send({id, error: {code: -32601, message: 'unsupported'}});
    else reply({protocolVersion: '2026-01-26', hostCapabilities: {updateModelContext: {}},
      hostContext: {theme: 'light'}});
  }
  if (method === 'ui/notifications/initialized') {
    send({method: 'ui/notifications/tool-result', params: resultFor(cards[frame.id])});
  }
  if (method === 'tools/call') {
    if (window.failNext) {
      window.failNext = false;
      send({id, error: {code: -32603, message: 'ambiguous transport failure'}});
    } else if (window.returnMismatchedNext) {
      window.returnMismatchedNext = false;
      reply(resultFor(cards[frame.id === 'one' ? 'two' : 'one']));
    } else {
      const result = invoke(frame.id, params.name, params.arguments);
      window.lastPublic = result.structuredContent || null;
      reply(result);
    }
  }
  if (method === 'ui/update-model-context') {
    window.contexts[frame.id] = params;
    reply({});
  }
});
</script>
<iframe id="one" title="Host access one" src="/approval"></iframe>
<iframe id="two" title="Host access two" src="/approval"></iframe>
</body></html>"""


class HostAccessApprovalBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            # Pin the executable resolved by this installed Playwright revision;
            # no arbitrary browser path is accepted from tool input or the UI.
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
          window.openai = {widgetState: null, setWidgetState(value) {
            this.widgetState = value;
            parent.states[window.frameElement.id] = value;
          }};
          if (window !== parent && parent.location.search.includes('legacy')) {
            const id = window.frameElement.id;
            window.openai.toolOutput = parent.publicCard(parent.cards[id]);
            window.openai.toolResponseMetadata = {
              host_access_confirmation: parent.cards[id].secret
            };
            window.openai.callTool = async (name, args) => {
              parent.calls.push({frame: id, method: 'tools/call', params: {name, arguments: args}});
              if (parent.failNext) {
                parent.failNext = false;
                throw new Error('ambiguous transport failure');
              }
              return parent.invoke(id, name, args);
            };
          }
        """)
        self.context.route("https://approval.test/**", lambda route: route.fulfill(
            content_type="text/html",
            body=HTML.read_text(encoding="utf-8") if route.request.url.endswith("/approval") else HOST,
        ))
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def open(self, suffix=""):
        self.page.goto("https://approval.test/" + suffix)
        frame = self.page.frame_locator("#one")
        expect(frame.get_by_role("heading", name="호스트 권한 승인")).to_be_visible()
        expect(frame.get_by_role("button", name="승인")).to_be_enabled()
        return frame

    def tool_calls(self, frame_id="one"):
        return self.page.evaluate(
            "([frame]) => calls.filter(c => c.frame === frame && c.method === 'tools/call')",
            [frame_id],
        )

    def test_inline_height_tracks_content_options_and_viewport_without_feedback(self):
        self.open("?autosize")
        self.page.wait_for_function("""() => {
          const frame = document.querySelector('#one');
          return sizes.some(item => item.frame === 'one') && frame.clientHeight > 500 &&
            frame.contentDocument.documentElement.scrollHeight <= frame.clientHeight;
        }""", timeout=2500)
        collapsed = self.page.locator("#one").evaluate("frame => frame.clientHeight")
        frame = self.page.frame_locator("#one")
        frame.get_by_role("button", name="옵션 변경").click()
        self.page.wait_for_function(
            "height => document.querySelector('#one').clientHeight > height + 100",
            arg=collapsed,
        )
        expanded = self.page.locator("#one").evaluate("frame => frame.clientHeight")
        frame.get_by_role("button", name="변경 닫기").click()
        self.page.wait_for_function(
            "height => document.querySelector('#one').clientHeight < height",
            arg=expanded,
        )
        self.page.set_viewport_size({"width": 1000, "height": 844})
        self.page.wait_for_function("""() => {
          const frame = document.querySelector('#one');
          const doc = frame.contentDocument;
          return sizes.some(item => item.frame === 'one' && item.params.width === 600) &&
            doc.documentElement.scrollHeight <= frame.clientHeight &&
            doc.querySelector('#approve').getBoundingClientRect().bottom <= frame.clientHeight;
        }""")
        before = self.page.evaluate("sizes.length")
        # Several animation frames let a resize feedback loop become observable.
        self.page.evaluate("""() => new Promise(resolve => {
          let frames = 8;
          const tick = () => --frames ? requestAnimationFrame(tick) : resolve();
          requestAnimationFrame(tick);
        })""")
        self.assertEqual(self.page.evaluate("sizes.length"), before)
        sizes = self.page.evaluate("sizes")
        for item in sizes:
            self.assertFalse(item["hasId"])
            self.assertEqual(set(item["params"]), {"height", "width"})
            self.assertGreater(item["params"]["height"], 0)
            self.assertGreater(item["params"]["width"], 0)
        methods = self.page.evaluate("calls.filter(item => item.frame === 'one').map(item => item.method)")
        self.assertLess(methods.index("ui/notifications/initialized"),
                        methods.index("ui/notifications/size-changed"))
        self.assertEqual(self.tool_calls(), [])

    def test_teardown_stops_height_notifications(self):
        frame = self.open("?autosize")
        self.page.wait_for_function("sizes.some(item => item.frame === 'one')")
        self.page.evaluate("""() => document.querySelector('#one').contentWindow.postMessage({
          jsonrpc: '2.0', id: 9001, method: 'ui/resource-teardown', params: {}
        }, '*')""")
        self.page.wait_for_function("""() => document.querySelector('#one').contentWindow.document
          .querySelector('#options').hidden""")
        # The reply proves teardown ran before changing the card's geometry.
        self.page.wait_for_function("""() => calls.some(item => item.frame === 'one' &&
          item.method === undefined)""")
        before = self.page.evaluate("sizes.length")
        frame.locator("main").evaluate("main => main.style.paddingBottom = '300px'")
        self.page.evaluate("""() => new Promise(resolve => {
          let frames = 8;
          const tick = () => --frames ? requestAnimationFrame(tick) : resolve();
          requestAnimationFrame(tick);
        })""")
        self.assertEqual(self.page.evaluate("sizes.length"), before)
        self.assertEqual(self.tool_calls(), [])

    def test_render_is_read_only_until_explicit_approval(self):
        frame = self.open()
        self.assertEqual(self.tool_calls(), [])
        expect(frame.get_by_text("cantopsbuildserver")).to_be_visible()
        expect(frame.get_by_text("jhw (UID 1000)")).to_be_visible()
        expect(frame.get_by_text("jhw, docker", exact=True)).to_be_visible()
        expect(frame.get_by_text("/srv/personal-ops")).to_be_visible()
        expect(frame.locator("#duration")).to_have_text("30일")
        expect(frame.get_by_text("설치 전체 · 호스트 사용자 파일·네트워크·그룹 권한")).to_be_visible()
        expect(frame.get_by_text(
            "이 MCP 설치의 선택 프로젝트 호출자와 대화가 권한을 공유합니다.", exact=True
        )).to_be_visible()
        expect(frame.get_by_text("프로젝트는 작업 문맥이며 격리 경계가 아닙니다.")).to_be_visible()

        frame.get_by_role("button", name="승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인되었습니다")
        calls = self.tool_calls()
        self.assertEqual([call["params"]["name"] for call in calls], ["confirm_host_access_approval"])
        self.assertEqual(calls[0]["params"]["arguments"]["confirmation_token"], "a" * 64)
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        self.assertNotIn("a" * 64, frame.locator("body").inner_text())
        self.assertNotIn("a" * 64, str(self.page.evaluate("contexts")))
        self.assertNotIn("a" * 64, str(self.page.evaluate("states")))

    def test_option_change_rotates_token_and_requires_second_explicit_consent(self):
        frame = self.open()
        frame.get_by_role("button", name="옵션 변경").click()
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        frame.get_by_label("권한 유지 시간").select_option("3600")
        frame.get_by_label("Codex 에이전트 호출 허용").uncheck()
        frame.get_by_role("button", name="변경 닫기").click()
        expect(frame.get_by_role("button", name="승인")).to_be_enabled()
        self.assertTrue(self.page.evaluate("cards.one.allow_codex"))
        frame.get_by_role("button", name="옵션 변경").click()
        frame.get_by_label("권한 유지 시간").select_option("3600")
        frame.get_by_label("Codex 에이전트 호출 허용").uncheck()
        frame.get_by_role("button", name="변경 적용").click()
        expect(frame.get_by_role("status")).to_contain_text("옵션을 변경했습니다")
        self.assertEqual(self.page.evaluate("cards.one.revision"), 2)
        self.assertEqual(self.page.evaluate("cards.one.expires_at"), "2099-01-01T00:00:00Z")
        self.assertEqual(self.page.evaluate("cards.one.state"), "pending")
        calls = self.tool_calls()
        self.assertEqual(calls[0]["params"]["name"], "update_host_access_approval")
        self.assertEqual(calls[0]["params"]["arguments"]["confirmation_token"], "a" * 64)

        frame.get_by_role("button", name="승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인되었습니다")
        calls = self.tool_calls()
        self.assertEqual(calls[1]["params"]["name"], "confirm_host_access_approval")
        self.assertEqual(calls[1]["params"]["arguments"]["confirmation_token"], "c" * 64)
        self.assertFalse(self.page.evaluate("cards.one.grant.allow_codex"))

    def test_eight_hour_option_updates_card(self):
        frame = self.open()
        frame.get_by_role("button", name="옵션 변경").click()
        frame.get_by_label("권한 유지 시간").select_option("28800")
        frame.get_by_role("button", name="변경 적용").click()
        expect(frame.get_by_role("status")).to_contain_text("옵션을 변경했습니다")
        expect(frame.locator("#duration")).to_have_text("8시간")
        calls = self.tool_calls()
        self.assertEqual(calls[0]["params"]["name"], "update_host_access_approval")
        self.assertEqual(calls[0]["params"]["arguments"]["duration_seconds"], 28800)

    def test_thirty_day_option_updates_card_and_can_be_approved(self):
        frame = self.open()
        frame.get_by_role("button", name="옵션 변경").click()
        frame.get_by_label("권한 유지 시간").select_option("2592000", timeout=2000)
        frame.get_by_role("button", name="변경 적용").click()
        expect(frame.locator("#duration")).to_have_text("30일")
        frame.get_by_role("button", name="승인", exact=True).click()
        expect(frame.get_by_role("status")).to_contain_text("승인되었습니다")
        calls = self.tool_calls()
        self.assertEqual(calls[0]["params"]["arguments"]["duration_seconds"], 2592000)
        self.assertEqual(self.page.evaluate("cards.one.grant.allow_codex"), True)

    def test_over_thirty_day_result_fails_closed(self):
        frame = self.open()
        over_limit = self.page.evaluate("""() => {
          const result = resultFor(cards.one);
          result.structuredContent.revision += 1;
          result.structuredContent.duration_seconds = 2592001;
          return result;
        }""")
        self.page.evaluate("""value => document.querySelector('#one').contentWindow.postMessage({
          jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: value
        }, '*')""", over_limit)
        expect(frame.get_by_role("status")).to_contain_text("응답을 확인할 수 없습니다")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        self.assertEqual(self.tool_calls(), [])

    def test_mismatched_rpc_result_fails_closed(self):
        frame = self.open()
        self.page.evaluate("returnMismatchedNext = true")
        frame.get_by_role("button", name="승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인 결과를 확인할 수 없습니다")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        self.assertEqual(len(self.tool_calls()), 1)

    def test_cancel_is_explicit_and_terminal(self):
        frame = self.open()
        frame.get_by_role("button", name="취소").click()
        expect(frame.get_by_role("status")).to_contain_text("요청을 취소했습니다")
        self.assertEqual(self.page.evaluate("cards.one.state"), "cancelled")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        expect(frame.get_by_role("button", name="옵션 변경")).to_be_disabled()
        self.assertEqual(self.tool_calls()[0]["params"]["name"], "cancel_host_access_approval")

    def test_ambiguous_failure_disables_card_without_retry(self):
        frame = self.open()
        self.page.evaluate("failNext = true")
        frame.get_by_role("button", name="승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인 결과를 확인할 수 없습니다")
        expect(frame.get_by_role("status")).to_contain_text("get_host_access")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        self.assertEqual(len(self.tool_calls()), 1)

    def test_stale_and_terminal_rollback_notifications_are_ignored(self):
        frame = self.open()
        stale = self.page.evaluate("resultFor(cards.one)")
        frame.get_by_role("button", name="옵션 변경").click()
        frame.get_by_label("권한 유지 시간").select_option("3600")
        frame.get_by_role("button", name="변경 적용").click()
        expect(frame.get_by_text("개정 2")).to_be_visible()
        self.page.evaluate("""stale => document.querySelector('#one').contentWindow.postMessage({
          jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: stale
        }, '*')""", stale)
        expect(frame.get_by_text("개정 2")).to_be_visible()

        frame.get_by_role("button", name="승인").click()
        expect(frame.get_by_role("status")).to_contain_text("승인되었습니다")
        pending = self.page.evaluate("""() => ({content: [], structuredContent: {
          project: 'personal-ops', scope: 'installation', card_id: '11111111111111111111111111111111',
          revision: 3, state: 'pending', expires_at: '2099-01-01T00:00:00Z',
          project_root: '/srv/personal-ops', duration_seconds: 3600, allow_codex: true,
          host_identity: cards.one.host_identity},
          _meta: {host_access_confirmation: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'}})""")
        self.page.evaluate("""value => document.querySelector('#one').contentWindow.postMessage({
          jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: value
        }, '*')""", pending)
        expect(frame.get_by_role("status")).to_contain_text("승인되었습니다")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()

    def test_standard_frames_and_legacy_bridge_keep_secrets_isolated(self):
        one = self.open("?legacy")
        two = self.page.frame_locator("#two")
        self.assertEqual(self.tool_calls("one"), [])
        self.assertEqual(self.tool_calls("two"), [])
        one.get_by_role("button", name="승인").click()
        two.get_by_role("button", name="승인").click()
        expect(one.get_by_role("status")).to_contain_text("승인되었습니다")
        expect(two.get_by_role("status")).to_contain_text("승인되었습니다")
        self.assertEqual(self.tool_calls("one")[0]["params"]["arguments"]["confirmation_token"], "a" * 64)
        self.assertEqual(self.tool_calls("two")[0]["params"]["arguments"]["confirmation_token"], "b" * 64)
        self.assertNotIn("a" * 64, one.locator("body").inner_text())
        self.assertNotIn("b" * 64, two.locator("body").inner_text())
        self.assertNotIn("confirmation", str(self.page.evaluate("states")))
        self.assertIsNone(one.locator("body").evaluate(
            "() => window.openai.toolResponseMetadata ?? null"))
        self.assertIsNone(two.locator("body").evaluate(
            "() => window.openai.toolResponseMetadata ?? null"))

    def test_missing_secret_and_mobile_layout_fail_closed(self):
        frame = self.open()
        missing = self.page.evaluate("""() => {
          const result = resultFor(cards.one);
          delete result._meta;
          return result;
        }""")
        self.page.evaluate("""value => document.querySelector('#one').contentWindow.postMessage({
          jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: value
        }, '*')""", missing)
        expect(frame.get_by_role("status")).to_contain_text("확인 정보가 없습니다")
        expect(frame.get_by_role("button", name="승인")).to_be_disabled()
        box = frame.locator("main").bounding_box()
        self.assertIsNotNone(box)
        self.assertLessEqual(box["width"], 390)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
