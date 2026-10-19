"""Authenticated real HTTP command-approval fixture. No host commands or network calls."""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_server import create_server, APPROVAL_BODIES  # noqa: E402
from host_command_operations import HostCommandOperations  # noqa: E402


class FakeRun:
    def __init__(self):
        self.calls = 0

    def __call__(self, frozen, cancel):
        self.calls += 1
        return {"exit_code": 0, "duration_ms": 1, "stdout_bytes": 0, "stderr_bytes": 0}


class HostCommandWebApprovalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="web-host-command-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        repo = self.root / "demo"
        repo.mkdir()
        artifact = repo / "tarororo.ait"
        artifact.write_bytes(b"isolated-fixture")
        artifact.chmod(0o600)
        program = self.root / "safe-program"
        program.write_bytes(b"#!/bin/sh\nexit 0\n")
        program.chmod(0o700)
        policy = self.root / "profiles.json"
        policy.write_text(json.dumps({"version": 1, "enabled": True, "profiles": {
            "test-publish": {
                "project": "demo", "label": "<img src=x onerror=1>",
                "executable": str(program),
                "executable_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                "argv": ["test"], "cwd": ".", "timeout_seconds": 30,
                "effect": "external_publish",
                "inputs": {"tarororo.ait": hashlib.sha256(artifact.read_bytes()).hexdigest()},
            },
        }}))
        policy.chmod(0o600)
        self.runner = FakeRun()
        self.ops = HostCommandOperations({"demo": repo}, config_path=policy,
                                         state_dir=self.root/"cmd-state",
                                         runner=self.runner, test_only=True)
        self.plan = self.ops.prepare("demo", "test-publish", "once")
        private = self.root / "private"
        private.mkdir(mode=0o700)
        token = private / "admin-token"
        self.token = "s"*40
        token.write_text(self.token + "\n")
        token.chmod(0o600)
        self.web = create_server({"demo": repo}, self.root/"admin-state",
                                 port=0, token_path=token,
                                 command_ops_factory=lambda *_a, **_k: self.ops,
                                 host_ops_factory=lambda *_a, **_k: object(),
                                 registered_ops_factory=lambda *_a, **_k: object(),
                                 agent_ops_factory=lambda *_a, **_k: object())
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()
        def cleanup():
            self.web.shutdown()
            self.web.server_close()
            self.thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, method, url, body=None, *, auth=True, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.web.server_port}"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.web.server_port}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, url, payload, headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            return res.status, json.loads(raw)
        except ValueError:
            return res.status, raw

    def body(self, **changes):
        request = {
            "operation_id": self.plan["operation_id"],
            "plan_digest": self.plan["plan_digest"],
            "source_digest": self.plan["source_digest"],
            "confirmation": self.plan["operation_id"], "admin_token": self.token,
        }
        request.update(changes)
        return request

    def test_no_unauthenticated_metadata_listing(self):
        uri = "/api/host-command-plans?project=demo"
        self.assertEqual(self.request("GET", uri, auth=False)[0], 401)
        code, result = self.request("GET", uri)
        self.assertEqual(code, 200)
        self.assertEqual(result["plans"][0]["operation_id"], self.plan["operation_id"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.request("GET", "/api/host-command?project=demo&operation_id="+
                                           self.plan["operation_id"], auth=False)[0], 401)

    def test_unsafe_web_approval_never_creates_receipt(self):
        uri = "/api/host-command-approve?project=demo"
        for condition in ({"origin": False}, {"host": "outside.test"}):
            with self.subTest(condition=condition):
                code, _ = self.request("POST", uri, self.body(), **condition)
                self.assertEqual(code, 403)
        for body in (self.body(admin_token="wrong"), self.body(approved=True)):
            self.assertEqual(self.request("POST", uri, body)[0], 403)
        self.assertEqual(self.runner.calls, 0)
        self.assertFalse(self.ops._receipt_path(self.plan["operation_id"]).exists())

    def test_bad_plan_digest_and_typed_id_refused(self):
        uri = "/api/host-command-approve?project=demo"
        for change in ({"confirmation": "yes"}, {"plan_digest": "0"*64},
                       {"source_digest": "0"*64}):
            with self.subTest(change=change):
                code, _ = self.request("POST", uri, self.body(**change))
                self.assertEqual(code, 400)
        self.assertFalse(self.ops._receipt_path(self.plan["operation_id"]).exists())

    def test_exact_approval_issues_one_receipt_no_process(self):
        uri = "/api/host-command-approve?project=demo"
        code, result = self.request("POST", uri, self.body())
        self.assertEqual((code, result["approved"]), (200, True))
        self.assertEqual(self.runner.calls, 0)
        self.assertTrue(self.ops._receipt_path(self.plan["operation_id"]).exists())
        self.assertEqual(self.request("POST", uri, self.body())[0], 400)
        self.assertEqual(self.ops.pending("demo")["plans"], [])

    def test_public_approval_page_contains_no_tokens(self):
        operation_id = self.plan["operation_id"]
        self.assertIn("/api/host-command-approve", APPROVAL_BODIES)
        self.assertEqual(self.request("GET", "/host-commands?operation_id="+operation_id,
                                      auth=False, origin=False)[0], 200)
        for query in ("", "?operation_id=xyz", "?operation_id="+operation_id+"&admin_token=secret"):
            self.assertEqual(self.request("GET", "/host-commands"+query, auth=False,
                                          origin=False)[0], 400)
        code, script = self.request("GET", "/host-command-admin.js", auth=False, origin=False)
        self.assertEqual(code, 200)
        self.assertIn(b"textContent", script)
        self.assertNotIn(b"innerHTML", script)
        self.assertNotIn(self.token.encode(), script)


if __name__ == "__main__":
    unittest.main()
