"""HTTP approval security boundary regression; no Docker or host changes."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from unittest.mock import Mock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from admin_server import create_server  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402


class LocalWebApprovalHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        project = root / "tossApp"
        project.mkdir()
        other_project = root / "redmine"
        other_project.mkdir()
        self.host_ops = Mock()
        self.server = create_server(
            {"tossApp": project, "redmine": other_project}, root / "state",
            token_path=root / "token" / "admin", port=0,
            public_origin="https://cantopsbuildserver.tail661d29.ts.net:8443",
            host_ops_factory=lambda _, **kwargs: self.host_ops,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_port
        self.token = (root / "token" / "admin").read_text().strip()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path, payload, *, origin=None, auth=True, forwarded=False, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if host:
            headers["Host"] = host
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if origin is not None:
            headers["Origin"] = origin
        if forwarded:
            headers["Forwarded"] = "for=example.test"
        conn.request("POST", path, json.dumps(payload), headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, json.loads(body)

    def get(self, path, *, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, json.loads(body)

    def test_pending_operations_are_authenticated_and_tossapp_scoped(self):
        expected = {"project": "tossApp", "target": "tarororo-test", "plans": []}
        with patch("admin_server.pending_operations", return_value=expected) as pending:
            self.assertEqual(
                self.get("/api/host-operations?project=tossApp", auth=False)[0], 401)
            self.assertEqual(
                self.get("/api/host-operations?project=redmine")[0], 404)
            self.assertEqual(
                self.post("/api/host-operations?project=tossApp", {})[0], 405)
            status, result = self.get("/api/host-operations?project=tossApp")
        self.assertEqual(status, 200, result)
        self.assertEqual(result, expected)
        pending.assert_called_once_with(self.host_ops)

    def test_review_endpoint_is_authenticated_fixed_scope_and_read_only(self):
        operation_id = "a" * 32
        review = {"version": 1, "git_head": "b" * 40,
                  "source_digest": "c" * 64, "files": []}
        self.host_ops.review_artifact.return_value = review
        path = f"/api/host-operation-review?project=tossApp&operation_id={operation_id}"

        self.assertEqual(self.get(path, auth=False)[0], 401)
        self.assertEqual(self.get(path.replace("tossApp", "redmine"))[0], 404)
        self.assertEqual(self.get("/api/host-operation-review?project=tossApp&operation_id=bad")[0], 400)
        self.assertEqual(self.get(path + "&target=tarororo-test")[0], 400)
        self.assertEqual(self.post(path, {})[0], 405)
        status, result = self.get(path)

        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"project": "tossApp", "target": "tarororo-test",
                                  "operation_id": operation_id, "review": review})
        self.host_ops.review_artifact.assert_called_once_with(
            "tossApp", "tarororo-test", operation_id)

    def test_review_digest_refusal_is_sanitized(self):
        operation_id = "a" * 32
        self.host_ops.review_artifact.side_effect = HostRuntimeError("STALE_PLAN")

        status, result = self.get(
            f"/api/host-operation-review?project=tossApp&operation_id={operation_id}")

        self.assertEqual(status, 400)
        self.assertEqual(result, {"error": "Invalid request or unavailable state"})
        self.assertNotIn("STALE_PLAN", json.dumps(result))

    def test_approval_requires_fresh_token_exact_local_origin_and_typed_id(self):
        path = "/api/host-approve?project=tossApp"
        origin = f"http://127.0.0.1:{self.port}"
        payload = {"operation_id": "a" * 32, "plan_digest": "b" * 64,
                   "source_digest": "c" * 64, "confirmation": "a" * 32,
                   "admin_token": self.token}
        with patch("admin_server.approve_web", return_value={
            "operation_id": "a" * 32, "approved": True
        }) as approve:
            self.assertEqual(self.post(path, payload)[0], 403)
            self.assertEqual(self.post(path, {**payload, "admin_token": "wrong"}, origin=origin)[0], 403)
            self.assertEqual(self.post(path, payload, origin=origin, forwarded=True)[0], 403)
            self.assertEqual(self.post(path, payload, origin="https://evil.test")[0], 403)
            self.assertEqual(self.post(path, payload, origin=origin, auth=False)[0], 401)
            self.assertFalse(approve.called)
            ok, result = self.post(path, payload, origin=origin)
            self.assertEqual(ok, 200, result)
            self.assertTrue(result["approved"])
            approve.assert_called_once()

    def test_scope_upgrade_needs_fresh_local_auth(self):
        path = "/api/host-scope-enable?project=tossApp"
        origin = f"http://127.0.0.1:{self.port}"
        payload = {"confirmation": "ENABLE WORKER AND DB 013", "admin_token": self.token}
        with patch("admin_server.enable_extended_scope", return_value={"enabled": True}) as enable:
            self.assertEqual(self.post(path, payload)[0], 403)
            self.assertEqual(self.post(path, {**payload, "admin_token": "wrong"}, origin=origin)[0], 403)
            self.assertFalse(enable.called)
            status, result = self.post(path, payload, origin=origin)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["enabled"])
            enable.assert_called_once()

    def test_predecessor_scope_requires_separate_fresh_host_approval(self):
        path = "/api/host-predecessor-scope-enable?project=tossApp"
        origin = f"http://127.0.0.1:{self.port}"
        payload = {"confirmation": "ENABLE DB 006-012", "admin_token": self.token}
        with patch("admin_server.enable_predecessor_scope",
                   return_value={"scope": "db-006-012", "enabled": True}) as enable:
            self.assertEqual(self.post(path, payload)[0], 403)
            self.assertEqual(self.post(path, payload, origin=origin, auth=False)[0], 401)
            self.assertEqual(self.post(path, payload, origin=origin, forwarded=True)[0], 403)
            self.assertEqual(self.post(path, {**payload, "admin_token": "bad"},
                                       origin=origin)[0], 403)
            self.assertEqual(self.post(path, payload,
                                       origin="https://evil.test")[0], 403)
            self.assertFalse(enable.called)
            status, result = self.post(path, payload, origin=origin)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["enabled"])
            enable.assert_called_once_with(self.host_ops, "ENABLE DB 006-012")

    def test_trusted_tailnet_https_origin_can_approve_with_reauthentication(self):
        path = "/api/host-approve?project=tossApp"
        host = "cantopsbuildserver.tail661d29.ts.net:8443"
        origin = "https://" + host
        payload = {"operation_id": "a" * 32, "plan_digest": "b" * 64,
                   "source_digest": "c" * 64, "confirmation": "a" * 32,
                   "admin_token": self.token}
        with patch("admin_server.approve_web", return_value={
            "operation_id": "a" * 32, "approved": True
        }) as approve:
            self.assertEqual(self.post(path, payload, host=host)[0], 403)
            self.assertEqual(self.post(path, payload, host=host,
                                       origin="http://" + host)[0], 403)
            self.assertEqual(self.post(path, payload, host=host,
                                       origin="https://evil.test")[0], 403)
            self.assertEqual(self.post(path, {**payload, "admin_token": "bad"},
                                       host=host, origin=origin)[0], 403)
            self.assertEqual(self.post(path, payload, host=host,
                                       origin=origin, auth=False)[0], 401)
            self.assertFalse(approve.called)
            status, value = self.post(path, payload, host=host, origin=origin)
            self.assertEqual(status, 200, value)
            self.assertTrue(value["approved"])
            approve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
