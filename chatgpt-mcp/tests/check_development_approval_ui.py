"""Real Chromium contracts for Git and other inline approval cards.

Only a local intercepted MCP App host fixture is used. No live approval is
issued, and no Git, Docker, database, agent or external service is executed.
"""
from pathlib import Path
import json
import os
import re
import stat
import unittest

from playwright.sync_api import expect, sync_playwright

MODULE_DIR = Path(__file__).resolve().parents[1]
CONFIGS = {
    "git": {
        "html": "git-operation-approval.html",
        "component": "git_operation",
        "capability": "git_operation_confirmation",
        "approve": "confirm_git_operation",
        "execute": "confirm_and_execute_git_operation",
        "cancel": "cancel_git_operation_approval",
        "get": "get_git_operation",
        "next": "execute_git_operation",
    },
    "operation": {
        "html": "operation-approval.html",
        "component": "operation",
        "capability": "operation_confirmation",
        "approve": "confirm_operation_approval",
        "execute": "confirm_and_execute_operation",
        "cancel": "cancel_operation_approval",
        "get": "read_host_operation_events",
        "next": "execute_host_operation",
    },
}
HOST = r"""<!doctype html><html><head><style>
body { margin: 0; } iframe { display: block; width: 100%; height: 42px; border: 0; }
</style></head><body><script>
const config = __CONFIG__;
const token = window.token = 'a'.repeat(64);
const paths = Array.from({length:48}, (_,i) => 'checks/fixture_' + i + '.py');
const plan = {
  project:'example', operation_id:'1'.repeat(32), action:'commit',
  status:'pending_host_approval', state:'pending_host_approval',
  repository:'example', branch:'feat/approval-fixture',
  head_target:'6'.repeat(40), paths, files:paths,
  commit_identity:{author:'Fixture Author <author@example.test>',committer:'Fixture Committer <committer@example.test>'},
  message:'fix: review fixture\n\nDisposable browser fixture.',
  preview:'48 fixture files, exact source frozen',
  plan_digest:'3'.repeat(64), source_digest:'4'.repeat(64),
  target:'fixture-test', source:{digest:'4'.repeat(64)}, component:'api',
};
window.card = {
  component:config.component, project:'example', family:'registered',
  target:config.component === 'operation' ? 'fixture-test' : null,
  operation_id:'1'.repeat(32), card_id:'2'.repeat(32), state:'pending',
  action:'commit', repository:'example', branch:'feat/approval-fixture',
  file_count:48,
  plan_digest:'3'.repeat(64), source_digest:'4'.repeat(64),
  expires_at:'2099-01-01T00:00:00Z', executes_on_approval:false,
  supports_direct_execution:true, approval_required:true,
  receipt_verified:false, authorization_verified:false, plan,
};
if(new URLSearchParams(location.search).get('action') === 'push') {
  card.action='push';
  card.plan={...plan,action:'push',paths:[],message:'',remote:'origin',
    target:'https://github.com/example/reviewed.git',remote_head:'7'.repeat(40),
    head_target:'refs/heads/feat/approval-fixture -> '+'6'.repeat(40)};
}
window.calls=[]; window.contexts=[]; window.states=[]; window.sizes=[];
window.failNext=false; window.mismatchNext=false; window.refuseStartNext=false;
const result = value => ({content:[{type:'text',text:'bounded fixture'}],
  structuredContent:JSON.parse(JSON.stringify(value))});
window.deliver = (includeCapability=true) => {
  const envelope=result(card);
  if(includeCapability) envelope._meta={[config.capability]:token};
  document.querySelector('iframe').contentWindow.postMessage({
    jsonrpc:'2.0',method:'ui/notifications/tool-result',params:envelope},'*');
};
window.invoke = async (name,args) => {
  calls.push({name,args:JSON.parse(JSON.stringify(args))});
  if(failNext) { failNext=false; throw new Error('transport lost'); }
  if(args.confirmation_token !== token || args.operation_id !== card.operation_id ||
     args.card_id !== card.card_id) return {isError:true,content:[]};
  const direct=name === config.execute;
  const approved=name === config.approve || direct;
  const nextArguments={project:card.project,operation_id:card.operation_id};
  if(config.component === 'operation') nextArguments.target=card.target;
  card={...card,state:approved?'approved':'cancelled',approved,
    approval_required:false,receipt_verified:approved,
    authorization_verified:approved,supports_direct_execution:false,
    ...(direct ? {execution_requested:true,execution_started:true,
      plan:{...card.plan,state:'running',phase:'execute'}} : {}),
    next_action:approved?{tool:direct?config.get:config.next,arguments:nextArguments}:null};
  if(direct && refuseStartNext) {
    refuseStartNext=false; card.execution_started=false;
    card.execution={status:"pending_host_approval"};
  }
  if(mismatchNext) {
    mismatchNext=false; return result({...card,operation_id:'9'.repeat(32)});
  }
  return result(card);
};
window.addEventListener('message',event=>{
  if(event.source !== document.querySelector('iframe').contentWindow ||
     event.data?.jsonrpc !== '2.0') return;
  const {id,method,params}=event.data;
  if(method === 'ui/notifications/size-changed') {
    sizes.push(params); document.querySelector('iframe').style.height=params.height+'px'; return;
  }
  if(method === 'ui/initialize') {
    event.source.postMessage({jsonrpc:'2.0',id,result:{protocolVersion:'2026-01-26',
      hostCapabilities:{updateModelContext:{}},hostContext:{theme:'light'}}},'*'); return;
  }
  if(method === 'ui/notifications/initialized') {
    deliver(new URLSearchParams(location.search).get('missing') !== '1'); return;
  }
  if(method === 'tools/call') {
    Promise.resolve(invoke(params.name,params.arguments)).then(value=>
      event.source.postMessage({jsonrpc:'2.0',id,result:value},'*'),()=>
      event.source.postMessage({jsonrpc:'2.0',id,error:{code:-32603,message:'lost'}},'*'));
    return;
  }
  if(method === 'ui/update-model-context') {
    contexts.push(JSON.parse(JSON.stringify(params)));
    event.source.postMessage({jsonrpc:'2.0',id,result:{}},'*');
  }
});
</script><iframe title="Development approval" src="/approval"></iframe></body></html>"""


class DevelopmentApprovalBrowserTest(unittest.TestCase):
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
        self.context = self.browser.new_context(viewport={"width":390,"height":844})
        self.context.add_init_script("""
            if(window !== parent) window.openai={setWidgetState(value) {
                parent.states.push(JSON.parse(JSON.stringify(value)));
            }};
        """)
        self.context.route("https://approval.test/**", self.respond)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.config = CONFIGS["git"]

    def respond(self, route):
        body = ((MODULE_DIR / self.config["html"]).read_text(encoding="utf-8")
                if route.request.url.endswith("/approval")
                else HOST.replace("__CONFIG__", json.dumps(self.config)))
        route.fulfill(content_type="text/html", body=body)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def open(self, family, *, width=390, missing=False, action=None):
        self.config = CONFIGS[family]
        self.page.set_viewport_size({"width":width,"height":844})
        query = (["missing=1"] if missing else []) + (["action=" + action] if action else [])
        self.page.goto("https://approval.test/" + ("?" + "&".join(query) if query else ""))
        frame = self.page.frame_locator("iframe")
        expect(frame.locator("h1")).to_be_visible()
        if not missing:
            expect(self.direct(frame)).to_be_enabled()
        return frame

    @staticmethod
    def direct(frame):
        return frame.get_by_role("button", name=re.compile("승인.*실행"))

    @staticmethod
    def approve(frame):
        return frame.get_by_role("button", name=re.compile("^(승인만|이 .*승인)$"))

    @staticmethod
    def cancel(frame):
        return frame.get_by_role("button", name=re.compile("취소"))

    def assert_private(self, frame):
        token = "a" * 64
        self.assertNotIn(token, frame.locator("body").inner_text())
        self.assertNotIn(token, str(self.page.evaluate("contexts")))
        self.assertNotIn(token, str(self.page.evaluate("states")))

    def test_cards_initialize_and_fit_mobile_without_approval_side_effect(self):
        for family in CONFIGS:
            for width in (320,390,1440):
                with self.subTest(family=family,width=width):
                    frame = self.open(family,width=width)
                    self.assertEqual(self.page.evaluate("calls"), [])
                    self.page.wait_for_function("sizes.length > 0")
                    self.assertTrue(self.page.evaluate("""() => {
                        const doc=document.querySelector('iframe').contentDocument;
                        return doc.documentElement.scrollWidth <= doc.documentElement.clientWidth;
                    }"""))
                    self.assert_private(frame)

    def test_approve_only_does_not_start_and_never_reuses_capability(self):
        for family in CONFIGS:
            with self.subTest(family=family):
                frame = self.open(family)
                self.approve(frame).click()
                self.page.wait_for_function("calls.length === 1 && contexts.length > 0")
                self.assertEqual(self.page.evaluate("calls[0].name"), self.config["approve"])
                self.assertFalse(self.page.evaluate("card.execution_requested === true"))
                expect(self.direct(frame)).to_be_disabled()
                self.assert_private(frame)

    def test_explicit_approve_and_execute_calls_once_without_model_followup(self):
        for family in CONFIGS:
            with self.subTest(family=family):
                frame = self.open(family)
                self.direct(frame).click()
                self.page.wait_for_function("calls.length === 1 && contexts.length > 0")
                self.assertEqual(self.page.evaluate("calls[0].name"), self.config["execute"])
                self.assertTrue(self.page.evaluate("card.execution_requested === true"))
                expect(self.direct(frame)).to_be_disabled()
                self.assertEqual(len(self.page.evaluate("calls")), 1)
                self.assert_private(frame)

    def test_missing_private_capability_never_enables_confirmation(self):
        for family in CONFIGS:
            with self.subTest(family=family):
                frame = self.open(family,missing=True)
                expect(self.direct(frame)).to_be_disabled()
                expect(self.approve(frame)).to_be_disabled()
                self.assertEqual(self.page.evaluate("calls"), [])

    def test_ambiguous_or_mismatched_response_disables_controls_without_retry(self):
        for family in CONFIGS:
            for flag in ("failNext","mismatchNext"):
                with self.subTest(family=family,flag=flag):
                    frame = self.open(family)
                    self.page.evaluate(flag + " = true")
                    self.direct(frame).click()
                    self.page.wait_for_function("calls.length === 1")
                    expect(frame.locator("#status")).to_have_attribute("data-error", "true")
                    expect(self.direct(frame)).to_be_disabled()
                    expect(self.cancel(frame)).to_be_disabled()
                    self.page.wait_for_timeout(100)
                    self.assertEqual(len(self.page.evaluate("calls")), 1)
                    self.assert_private(frame)

    def test_git_card_shows_the_exact_backend_review_fields(self):
        frame = self.open("git")
        expect(frame.locator("#head")).to_have_text("6" * 40)
        expect(frame.locator("#author")).to_have_text("Fixture Author <author@example.test>")
        expect(frame.locator("#committer")).to_have_text("Fixture Committer <committer@example.test>")
        expect(frame.locator("#paths li")).to_have_count(48)
        expect(frame.locator("#message")).to_contain_text("Disposable browser fixture.")
        frame = self.open("git", action="push")
        expect(frame.locator("#target")).to_contain_text("https://github.com/example/reviewed.git")
        expect(frame.locator("#head")).to_contain_text("refs/heads/feat/approval-fixture")

    def test_git_card_rejects_nonboolean_capability_claims(self):
        for field in ("authorization_verified", "supports_direct_execution"):
            with self.subTest(field=field):
                frame = self.open("git")
                self.page.evaluate("(field) => { card[field]='false'; deliver(); }", field)
                expect(frame.locator("#status")).to_have_attribute("data-error", "true")
                expect(self.direct(frame)).to_be_disabled()
                expect(self.approve(frame)).to_be_disabled()
                self.assertEqual(self.page.evaluate("calls"), [])

    def test_git_card_does_not_claim_acceptance_when_start_was_rejected(self):
        frame = self.open("git")
        self.page.evaluate("refuseStartNext=true")
        self.direct(frame).click()
        self.page.wait_for_function("calls.length === 1 && contexts.length > 0")
        expect(frame.locator("#status")).to_contain_text("시작하지")
        expect(frame.locator("#status")).to_have_attribute("data-error", "true")
        self.assertFalse(self.page.evaluate("card.execution_started"))
        self.assertFalse(self.page.evaluate("contexts[0].structuredContent.gitOperationApproval.execution_started"))

    def test_git_card_locks_on_unrelated_host_tool_result(self):
        for mechanism in ("rpc", "legacy"):
            with self.subTest(mechanism=mechanism):
                frame = self.open("git")
                self.page.evaluate("""(mechanism) => {
                    card.operation_id='9'.repeat(32); card.plan.operation_id=card.operation_id;
                    if(mechanism === 'rpc') deliver();
                    else {
                        const child=document.querySelector('iframe').contentWindow;
                        child.dispatchEvent(new child.CustomEvent('openai:set_globals', {
                            detail:{globals:{toolOutput:card,
                                toolResponseMetadata:{git_operation_confirmation:token}}}
                        }));
                    }
                }""", mechanism)
                expect(frame.locator("#status")).to_have_attribute("data-error", "true")
                expect(self.direct(frame)).to_be_disabled()
                expect(self.approve(frame)).to_be_disabled()
                self.assertEqual(self.page.evaluate("calls"), [])

    def test_cancel_only_closes_the_reviewed_card(self):
        for family in CONFIGS:
            with self.subTest(family=family):
                frame = self.open(family)
                self.cancel(frame).click()
                self.page.wait_for_function("calls.length === 1 && card.state === 'cancelled'")
                self.assertEqual(self.page.evaluate("calls[0].name"), self.config["cancel"])
                self.assertFalse(self.page.evaluate("card.execution_requested === true"))
                expect(self.direct(frame)).to_be_disabled()
                self.assert_private(frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
