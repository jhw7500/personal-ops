"""Authenticated web approval for delegated coding task. No real CLI or DB."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from admin_server import create_server, APPROVAL_BODIES


class FakeAgentManager:
    def __init__(self):
        self.call_count = 0
        self.pending_task = {
            "project": "demo", "task_id": "a" * 32,
            "profile_id": "codex-repo", "reason": "WORKFLOW_COMPLEXITY",
            "state": "pending_host_approval", "phase": "APPROVAL",
            "objective": "<img src=x onerror=alert(1)>",
            "model": "gpt-5.6", "reasoning_effort": "high",
            "source_digest": "b" * 64, "plan_digest": "c" * 64,
            "expires_at": "2099-01-01T00:00:00Z", "allowed_paths": ["src"],
            "changed_paths": []
        }

    def pending(self, project):
        return {"project": project, "tasks": ([self.pending_task]
                                               if self.pending_task["state"] == "pending_host_approval"
                                               else [])}

    def get(self, project, task_id):
        if task_id != self.pending_task["task_id"]:
            raise ValueError("Invalid task")
        return dict(self.pending_task)

    def approve_web(self, project, *, task_id, plan_digest,
                    source_digest, confirmation):
        self.call_count += 1
        if (project != "demo" or task_id != self.pending_task["task_id"]
                or plan_digest != self.pending_task["plan_digest"]
                or source_digest != self.pending_task["source_digest"]
                or confirmation != task_id
                or self.pending_task["state"] != "pending_host_approval"):
            raise ValueError("Invalid plan")
        self.pending_task["state"] = "approved"
        return {"project": project, "task_id": task_id, "approved": True}


class AdminAgentApprovalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="web-agent-approval-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        repo = self.root / "repo"
        repo.mkdir()
        self.project = {"demo": repo}
        token_path = self.root / "private" / "token"
        token_path.parent.mkdir(mode=0o700)
        self.token = "s" * 40
        token_path.write_text(self.token + "\n")
        token_path.chmod(0o600)
        self.fake = FakeAgentManager()
        self.web = create_server(self.project, self.root / "runtime", token_path=token_path,
                                 port=0, agent_ops_factory=lambda *_args, **_kwargs: self.fake,
                                 host_ops_factory=lambda *_args, **_kwargs: object(),
                                 registered_ops_factory=lambda *_args, **_kwargs: object())
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()
        def cleanup():
            self.web.shutdown()
            self.web.server_close()
            self.thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, method, path, body=None, *, auth=True, origin=True,
                token=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.web.server_port}"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.web.server_port}"
        if body is not None:
            if token is not None:
                body = dict(body, admin_token=token)
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        else:
            data = None
        conn.request(method, path, body=data, headers=headers)
        result = conn.getresponse()
        payload = result.read()
        conn.close()
        return result.status, json.loads(payload) if payload.startswith(b"{") else payload

    def body(self):
        task = self.fake.pending_task
        return {"task_id": task["task_id"], "plan_digest": task["plan_digest"],
                "source_digest": task["source_digest"],
                "confirmation": task["task_id"],
                "admin_token": self.token}

    def test_authenticated_listing_does_not_execute(self):
        self.assertEqual(self.request("GET", "/api/agent-tasks?project=demo", auth=False)[0], 401)
        code, payload = self.request("GET", "/api/agent-tasks?project=demo")
        self.assertEqual(code, 200)
        self.assertEqual(payload["tasks"][0]["task_id"], "a"*32)
        self.assertEqual(self.fake.call_count, 0)

    def test_origin_token_and_exact_body_enforced_before_any_effect(self):
        route = "/api/agent-approve?project=demo"
        invalid = [
            {"origin": False}, {"token": "wrong"}, {"host": "untrusted.test"},
        ]
        for item in invalid:
            with self.subTest(item=item):
                kwargs = dict(item)
                token = kwargs.pop("token", None)
                body = self.body()
                if token is not None:
                    body["admin_token"] = token
                code, _ = self.request("POST", route, body, **kwargs)
                self.assertEqual(code, 403)
                self.assertEqual(self.fake.call_count, 0)
        bad = dict(self.body(), approved=True)
        self.assertEqual(self.request("POST", route, bad)[0], 403)
        self.assertEqual(self.fake.call_count, 0)

    def test_exact_typed_id_issues_one_receipt_not_an_agent_start(self):
        path = "/api/agent-approve?project=demo"
        code, data = self.request("POST", path, self.body())
        self.assertEqual((code, data["approved"]), (200, True))
        self.assertEqual(self.fake.call_count, 1)
        self.assertEqual(self.fake.pending_task["state"], "approved")
        self.assertEqual(self.request("GET", "/api/agent-tasks?project=demo")[1]["tasks"], [])
        self.assertEqual(self.request("POST", path, self.body())[0], 400)

    def test_script_uses_textcontent_and_assets_are_registered(self):
        self.assertIn("/api/agent-approve", APPROVAL_BODIES)
        code, script = self.request("GET", "/agent-admin.js", auth=False, origin=False)
        self.assertEqual(code, 200)
        self.assertIn(b"textContent", script)
        self.assertNotIn(b"innerHTML", script)


if __name__ == "__main__":
    unittest.main()
