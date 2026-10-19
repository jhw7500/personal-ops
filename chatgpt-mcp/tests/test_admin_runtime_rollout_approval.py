"""Authenticated HTTP contract for runtime web approval, with zero host effects."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from admin_server import create_server, APPROVAL_BODIES
from runtime_rollout_web import RuntimeRolloutWeb


class RuntimeRolloutHTTPTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="runtime-rollout-http-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.repo = self.base / "personal-ops"
        source = Path(__file__).resolve().parents[2]
        shutil.copytree(source / "chatgpt-mcp", self.repo / "chatgpt-mcp",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(source / "work-dashboard", self.repo / "work-dashboard")
        self.launched = []
        self.manager = RuntimeRolloutWeb(
            {"personal-ops": self.repo}, state_dir=self.base / "state",
            launcher=lambda command: self.launched.append(command) or True,
            runtime_reader=lambda: "a" * 64,
            boot_reader=lambda: "11111111-2222-3333-4444-555555555555",
            uid_reader=lambda: 1000,
            test_only=True)
        self.plan = self.manager.prepare("personal-ops", "web-approval-test")
        private = self.base / "private"
        private.mkdir(mode=0o700)
        token_file = private / "admin-token"
        self.token = "q" * 48
        token_file.write_text(self.token + "\n")
        token_file.chmod(0o600)
        self.web = create_server(
            {"personal-ops": self.repo}, self.base / "admin-state",
            token_path=token_file, port=0,
            runtime_rollout_factory=lambda *_a, **_k: self.manager,
            host_ops_factory=lambda *_a, **_k: object(),
            registered_ops_factory=lambda *_a, **_k: object(),
            agent_ops_factory=lambda *_a, **_k: object(),
            command_ops_factory=lambda *_a, **_k: object(),
        )
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()
        def cleanup():
            self.web.shutdown()
            self.web.server_close()
            self.thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, method, path, data=None, *, authenticated=True, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=10)
        authority = host or f"127.0.0.1:{self.web.server_port}"
        headers = {"Host": authority}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.web.server_port}"
        body = None
        if data is not None:
            body = json.dumps(data)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return status, parsed, result_headers

    def approval(self, **changes):
        body = {
            "operation_id": self.plan["operation_id"],
            "plan_digest": self.plan["plan_digest"],
            "source_digest": self.plan["source_digest"],
            "confirmation": self.plan["operation_id"],
            "admin_token": self.token,
        }
        body.update(changes)
        return body

    def test_owner_only_metadata_and_exact_plan_page(self):
        url = "/api/runtime-rollout-plans?project=personal-ops"
        self.assertEqual(self.request("GET", url, authenticated=False)[0], 401)
        code, data, _ = self.request("GET", url)
        self.assertEqual(code, 200)
        self.assertEqual(data["plans"][0]["operation_id"], self.plan["operation_id"])
        self.assertNotIn(str(self.repo), json.dumps(data))
        self.assertNotIn(self.token, json.dumps(data))
        self.assertEqual(self.request("GET", "/api/runtime-rollout?project=personal-ops&operation_id=" +
                                           self.plan["operation_id"], authenticated=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/runtime-rollout-plans?project=tossApp")[0], 400)
        self.assertEqual(self.request("GET", "/api/runtime-rollout?project=personal-ops&operation_id=bad")[0], 400)

    def test_fresh_origin_exact_body_and_token_required(self):
        url = "/api/runtime-rollout-approve?project=personal-ops"
        for options in ({"origin": False}, {"host": "attacker.test"}):
            with self.subTest(options=options):
                self.assertEqual(self.request("POST", url, self.approval(), **options)[0], 403)
        for change in ({"admin_token": "wrong"}, {"approved": True}):
            with self.subTest(change=change):
                self.assertEqual(self.request("POST", url, self.approval(**change))[0], 403)
        self.assertEqual(self.request("POST", url, self.approval(), authenticated=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/runtime-rollout-approve?project=tossApp",
                                      self.approval())[0], 400)
        self.assertFalse((self.manager.receipts_dir / (self.plan["operation_id"]+".json")).exists())
        self.assertFalse(self.launched)

    def test_wrong_digest_and_confirmation_are_rejected(self):
        url = "/api/runtime-rollout-approve?project=personal-ops"
        for fields in ({"confirmation": "ROLLOUT"}, {"plan_digest": "0"*64},
                       {"source_digest": "0"*64}):
            self.assertEqual(self.request("POST", url, self.approval(**fields))[0], 400)
        self.assertFalse(self.launched)

    def test_one_use_approval_does_not_start_systemd_unit(self):
        url = "/api/runtime-rollout-approve?project=personal-ops"
        code, result, _ = self.request("POST", url, self.approval())
        self.assertEqual(code, 200)
        self.assertEqual(result, {
            "project": "personal-ops", "operation_id": self.plan["operation_id"],
            "approved": True, "executed": False,
        })
        self.assertFalse(self.launched)
        self.assertEqual(self.request("POST", url, self.approval())[0], 400)
        self.assertEqual(self.manager.get("personal-ops", self.plan["operation_id"])["state"], "approved")

    def test_public_page_and_script_never_expose_token(self):
        op = self.plan["operation_id"]
        self.assertIn("/api/runtime-rollout-approve", APPROVAL_BODIES)
        page = self.request("GET", "/runtime-rollouts?operation_id=" + op,
                            authenticated=False, origin=False)
        self.assertEqual(page[0], 200)
        self.assertIn(b"runtime-rollout-admin.js", page[1])
        csp = page[2]["Content-Security-Policy"]
        self.assertIn("/runtime-rollout-admin.js", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        for url in ("/runtime-rollouts", "/runtime-rollouts?operation_id=bad",
                    "/runtime-rollouts?operation_id="+op+"&admin_token=secret"):
            self.assertEqual(self.request("GET", url, authenticated=False, origin=False)[0], 400)
        js = self.request("GET", "/runtime-rollout-admin.js", authenticated=False, origin=False)
        self.assertEqual(js[0], 200)
        self.assertIn(b"textContent", js[1])
        self.assertNotIn(b"innerHTML", js[1])
        self.assertNotIn(self.token.encode(), js[1])


if __name__ == "__main__":
    unittest.main()
