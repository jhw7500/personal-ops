#!/usr/bin/env python3
"""Actual Chromium checks for the workspace approval card and host bridge."""
from __future__ import annotations

# Keep ordinary unittest/MCP discovery independent of the browser environment.
if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    import json
    from pathlib import Path
    import unittest

    from playwright.sync_api import expect, sync_playwright

    HTML = (Path(__file__).resolve().parents[1]
            / "workspace-access-approval.html").read_text(encoding="utf-8")
    REQUEST_ID = "1" * 32
    TOKEN = "c" * 64
    POLICY = "d" * 64
    GRANT = "2" * 32


    def pending(*, expires=None, path="/workspace/report.json"):
        return {
            "status": "pending", "scope": "installation",
            "request_id": REQUEST_ID, "project": "alpha", "path": path,
            "scope_kind": "file", "operations": ["read"],
            "duration_seconds": 2592000, "purpose": "검토 결과 확인",
            "request_expires_at": expires or (
                datetime.now(timezone.utc) + timedelta(days=7)
            ).isoformat(),
            "authority_epoch": 7, "policy_digest": POLICY,
        }


    def terminal(*, status="approved", project="alpha"):
        value = {
            "status": status, "request_id": REQUEST_ID,
            "authority_epoch": 7, "policy_digest": POLICY,
        }
        if status == "approved":
            value.update({
                "project": project, "grant_id": GRANT,
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(days=30)).isoformat(),
                "scope_path": "/workspace/report.json", "operations": ["read"],
            })
        return value


    PARENT = """<!doctype html><iframe id=card src='http://widget.test/card'></iframe><script>
    window.calls=[];window.contexts=[];window.notes=[];window.initialized=false;
    window.reply={};
    addEventListener('message',e=>{if(e.source!==card.contentWindow||e.data?.jsonrpc!=='2.0')return;
      const m=e.data;window.notes.push(m);
      if(m.method==='ui/initialize')card.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result:{hostContext:{theme:'dark'}}},'*');
      else if(m.method==='ui/notifications/initialized')window.initialized=true;
      else if(m.method==='tools/call'){window.calls.push(m.params);const r=window.reply[m.params.name];
        setTimeout(()=>card.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result:r},'*'),25);}
      else if(m.method==='ui/update-model-context'){window.contexts.push(m.params);card.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result:{}},'*');}
    });
    window.deliver=x=>card.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',params:x},'*');
    </script>"""


    class WorkspaceApprovalBrowserTest(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)

        @classmethod
        def tearDownClass(cls):
            cls.browser.close()
            cls.playwright.stop()

        def setUp(self):
            self.context = self.browser.new_context(viewport={"width": 1200, "height": 800})
            self.addCleanup(self.context.close)
            self.context.route("http://host.test/", lambda route: route.fulfill(
                status=200, content_type="text/html", body=PARENT,
            ))
            self.context.route("http://widget.test/card", lambda route: route.fulfill(
                status=200, content_type="text/html", body=HTML,
            ))
            self.page = self.context.new_page()
            self.errors = []
            self.page.on("pageerror", lambda error: self.errors.append(str(error)))
            self.page.goto("http://host.test/")
            self.page.wait_for_function("window.initialized")
            self.frame = self.page.frame(url="http://widget.test/card")

        def tearDown(self):
            self.assertEqual(self.errors, [])

        def deliver(self, value, token=TOKEN):
            self.page.evaluate("x=>deliver(x)", {
                "structuredContent": value,
                "_meta": {"workspace_confirmation": token},
            })

        def configure(self, name, value, *, error=False):
            self.page.evaluate("x=>reply[x.name]=x.value", {
                "name": name,
                "value": {"structuredContent": value, "isError": error},
            })

        def test_exact_scope_responsive_modern_init_and_capability_is_private(self):
            card = pending(path="/outside/exact report.json")
            self.deliver(card)
            expect(self.frame.locator("#approve")).to_be_enabled()

            self.assertEqual(self.frame.locator("#project").inner_text(), "alpha")
            self.assertEqual(self.frame.locator("#path").inner_text(), card["path"])
            self.assertIn("읽기", self.frame.locator("#operations").inner_text())
            self.assertNotIn(TOKEN, self.frame.locator("html").inner_text())
            self.assertNotIn(TOKEN, self.frame.locator("html").inner_html())
            self.assertEqual(self.page.evaluate("calls.length"), 0)
            self.assertEqual(self.page.evaluate("contexts.length"), 0)
            self.assertEqual(self.frame.evaluate("document.documentElement.style.colorScheme"), "dark")
            self.assertTrue(self.page.evaluate(
                "notes.some(x=>x.method==='ui/notifications/size-changed')"))
            for width in (1200, 390):
                self.page.set_viewport_size({"width": width, "height": 800})
                self.frame.locator("main").wait_for(state="visible")
                self.assertLessEqual(
                    self.frame.evaluate("document.documentElement.scrollWidth"),
                    self.frame.evaluate("window.innerWidth"),
                )

        def test_approve_double_click_calls_once_and_publishes_only_public_receipt(self):
            approved = terminal()
            self.configure("confirm_workspace_access_approval", approved)
            self.deliver(pending())
            button = self.frame.locator("#approve")
            expect(button).to_be_enabled()
            button.dblclick()
            expect(self.frame.locator("#status")).to_contain_text("승인했습니다")

            self.assertEqual(self.page.evaluate("calls.length"), 1)
            call = self.page.evaluate("calls[0]")
            self.assertEqual(call["name"], "confirm_workspace_access_approval")
            self.assertEqual(call["arguments"], {
                "project": "alpha", "request_id": REQUEST_ID,
                "confirmation_token": TOKEN,
            })
            context = self.page.evaluate("contexts[0].structuredContent.workspaceAccess")
            self.assertEqual({key: context[key] for key in (
                "grant_id", "project", "expires_at"
            )}, {key: approved[key] for key in (
                "grant_id", "project", "expires_at"
            )})
            self.assertNotIn(TOKEN, json.dumps(self.page.evaluate("contexts")))
            expect(button).to_be_disabled()

        def test_mismatch_and_expiry_lock_without_callback(self):
            self.deliver(pending())
            self.deliver(pending(path="/workspace/changed.json"))
            expect(self.frame.locator("#status")).to_contain_text("변경되었습니다")
            expect(self.frame.locator("#approve")).to_be_disabled()
            self.assertEqual(self.page.evaluate("calls.length"), 0)

            self.page.reload()
            self.page.wait_for_function("window.initialized")
            self.frame = self.page.frame(url="http://widget.test/card")
            self.deliver(pending(expires=(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()))
            expect(self.frame.locator("#approve")).to_be_disabled()
            self.assertEqual(self.page.evaluate("calls.length"), 0)

        def test_cancel_is_one_use_and_invalid_terminal_locks_without_context(self):
            self.configure("cancel_workspace_access_approval", terminal(status="cancelled"))
            self.deliver(pending())
            self.frame.locator("#cancel").dblclick()
            expect(self.frame.locator("#status")).to_contain_text("취소했습니다")
            self.assertEqual(self.page.evaluate("calls.length"), 1)
            self.assertEqual(
                self.page.evaluate("contexts[0].structuredContent.workspaceAccess.status"),
                "cancelled",
            )
            self.assertNotIn(TOKEN, json.dumps(self.page.evaluate("contexts")))

            self.page.reload()
            self.page.wait_for_function("window.initialized")
            self.frame = self.page.frame(url="http://widget.test/card")
            self.configure(
                "confirm_workspace_access_approval",
                {"status": "denied", "request_id": REQUEST_ID},
                error=True,
            )
            self.deliver(pending())
            self.frame.locator("#approve").click()
            expect(self.frame.locator("#status")).to_contain_text("확인할 수 없습니다")
            self.assertEqual(self.page.evaluate("contexts.length"), 0)
            expect(self.frame.locator("#approve")).to_be_disabled()

            self.page.reload()
            self.page.wait_for_function("window.initialized")
            self.frame = self.page.frame(url="http://widget.test/card")
            self.configure("confirm_workspace_access_approval", terminal(project="other"))
            self.deliver(pending())
            self.frame.locator("#approve").click()
            expect(self.frame.locator("#status")).to_contain_text("확인할 수 없습니다")
            self.assertEqual(self.page.evaluate("contexts.length"), 0)
            expect(self.frame.locator("#approve")).to_be_disabled()


    unittest.main()
