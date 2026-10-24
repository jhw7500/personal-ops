"""Admin web approval of registered (version 3) host operations over real loopback HTTP.

Fake runner + in-memory Docker model and temporary state only: no Docker command, host
configuration, service or database is touched.
"""
from __future__ import annotations

import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from admin_server import TRUSTED_APPROVAL_ORIGIN, create_server  # noqa: E402
from host_operation_fixtures import (  # noqa: E402
    fake_registered_manager, write_registered_fixture,
)
from host_runtime_config import PROJECT, TARGET  # noqa: E402

TAILNET_HOST = TRUSTED_APPROVAL_ORIGIN.removeprefix("https://")


def alpha_target(payload):
    """A second registered target owned by another project (proves no tossApp hard-coding)."""
    target = copy.deepcopy(payload["targets"][TARGET])
    target["project"] = "alpha"
    target["compose"]["project_name"] = "alpha-test"
    target["policy"]["networks"] = ["alpha-test_default", "alpha-test-content-internal"]
    payload["targets"]["alpha-test"] = target


class RegisteredWebApprovalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="admin-registered-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.fx = write_registered_fixture(self.home, extra_targets=alpha_target)
        self.projects = self.fx["projects"]
        self.state_dir = self.home / "state" / "host-runtime"
        self.ops, self.runner, self.docker = fake_registered_manager(
            self.projects, self.fx["config"], self.state_dir)
        self.config_bytes = self.fx["config"].read_bytes()
        self.server = self.start(public_origin=TRUSTED_APPROVAL_ORIGIN)
        self.port = self.server.server_port
        self.token = (self.home / "token" / "admin").read_text().strip()
        self.plan = self.ops.prepare(PROJECT, TARGET, action="compose.restart",
                                     parameters={"services": ["api"]}, request_id="web-1")

    def start(self, **options):
        server = create_server(self.projects, self.home / "admin-state",
                               token_path=self.home / "token" / "admin", port=0,
                               host_ops_factory=lambda _, **kwargs: object(),
                               registered_ops_factory=lambda _, **kwargs: self.ops, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        return server

    # ----- HTTP helpers --------------------------------------------------------------
    def request(self, method, path, payload=None, *, port=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        sent = {}
        if auth:
            sent["Authorization"] = "Bearer " + self.token
        body = None
        if payload is not None:
            body = json.dumps(payload)
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn.request(method, path, body, sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def local(self, port=None):
        return {"Origin": f"http://127.0.0.1:{port or self.port}"}

    def approve_body(self, plan=None, **changes):
        plan = plan or self.plan
        body = {"operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"],
                "source_digest": plan["source_digest"], "confirmation": plan["operation_id"],
                "admin_token": self.token}
        body.update(changes)
        return body

    def query(self, project=PROJECT, target=TARGET):
        return f"?project={project}&target={target}"

    def receipts(self):
        return sorted(path.name for path in (self.state_dir / "registered" / "receipts").iterdir())

    def state(self, plan=None):
        return self.ops.get(PROJECT, TARGET, (plan or self.plan)["operation_id"])["state"]

    def assertNoReceiptOrEffect(self, plan=None):
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.state(plan), "pending_host_approval")

    # ----- reads ---------------------------------------------------------------------
    def test_registered_reads_are_authenticated_and_registry_scoped(self):
        op = self.plan["operation_id"]
        paths = ["/api/registered-targets?project=tossApp",
                 "/api/registered-operations" + self.query(),
                 "/api/registered-operation" + self.query() + "&operation_id=" + op]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, auth=False)[0], 401)
        status, targets = self.request("GET", paths[0])
        self.assertEqual(status, 200, targets)
        self.assertEqual((targets["enabled"], targets["reason"]), (True, None))
        self.assertEqual([row["target"] for row in targets["targets"]], [TARGET])
        self.assertEqual(targets["targets"][0]["services"], ["api", "worker", "db"])
        self.assertNotIn(str(self.fx["secrets"]), json.dumps(targets))
        status, alpha = self.request("GET", "/api/registered-targets?project=alpha")
        self.assertEqual([row["target"] for row in alpha["targets"]], ["alpha-test"])
        status, listing = self.request("GET", paths[1])
        self.assertEqual(status, 200, listing)
        self.assertEqual([row["operation_id"] for row in listing["operations"]], [op])
        self.assertEqual(listing["operations"][0]["state"], "pending_host_approval")
        self.assertIn("server_time", listing)
        status, empty = self.request("GET", "/api/registered-operations" + self.query(
            "alpha", "alpha-test"))
        self.assertEqual((status, empty["operations"]), (200, []))
        status, detail = self.request("GET", paths[2])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["operation"]["plan_digest"], self.plan["plan_digest"])
        self.assertEqual(detail["operation"]["impact"]["interrupted"], ["api"])
        self.assertEqual(detail["operation"]["verifications"], self.plan["verifications"])
        self.assertIsNone(detail["review"])
        self.assertEqual([event["code"] for event in detail["events"]["events"]], ["PLAN_CREATED"])
        text = json.dumps(detail)
        for leaked in (str(self.home), "nonce", str(self.fx["secrets"])):
            self.assertNotIn(leaked, text)
        for path, code in (
                ("/api/registered-operations" + self.query("alpha", TARGET), "UNSUPPORTED_SCOPE"),
                ("/api/registered-operations" + self.query(PROJECT, "alpha-test"), "UNSUPPORTED_SCOPE"),
                ("/api/registered-operations" + self.query(PROJECT, "../etc"), "UNSUPPORTED_SCOPE"),
                ("/api/registered-operation" + self.query() + "&operation_id=" + "0" * 32,
                 "OPERATION_NOT_FOUND")):
            with self.subTest(path=path):
                status, value = self.request("GET", path)
                self.assertEqual((status, value.get("code")), (400, code))
        self.assertEqual(self.request("GET", "/api/registered-operations?project=ghost&target="
                                      + TARGET)[0], 400)
        self.assertEqual(self.runner.effects, [])

    def test_reviewed_snapshot_detail_carries_the_private_review_artifact(self):
        payload = json.loads(self.fx["config"].read_text())
        payload["targets"][TARGET]["source_review"] = "reviewed_snapshot"
        self.fx["config"].write_text(json.dumps(payload))
        self.ops.reject_web(PROJECT, TARGET, operation_id=self.plan["operation_id"],
                            confirmation=self.plan["operation_id"])
        (self.fx["root"] / "deploy/pc-test/manage.py").write_text("print('<img src=x onerror=1>')\n")
        plan = self.ops.prepare(PROJECT, TARGET, action="compose.restart",
                                parameters={"services": ["api"]}, request_id="web-review")
        status, detail = self.request("GET", "/api/registered-operation" + self.query()
                                      + "&operation_id=" + plan["operation_id"])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["operation"]["source"]["high_risk_changes"],
                         ["deploy/pc-test/manage.py"])
        self.assertEqual([row["path"] for row in detail["review"]["files"]],
                         ["deploy/pc-test/manage.py"])
        self.assertIn("onerror", detail["review"]["files"][0]["diff"])

    def test_event_cursor_pages_through_the_operation_log(self):
        op = self.plan["operation_id"]
        status, first = self.request("GET", "/api/registered-operation" + self.query()
                                     + "&operation_id=" + op + "&cursor=0")
        self.assertEqual(first["events"]["next_cursor"], 1)
        status, rest = self.request("GET", "/api/registered-operation" + self.query()
                                    + "&operation_id=" + op + "&cursor=1")
        self.assertEqual((status, rest["events"]["events"], rest["events"]["more"]), (200, [], False))
        self.assertEqual(self.request("GET", "/api/registered-operation" + self.query()
                                      + "&operation_id=" + op + "&cursor=-1")[1].get("code"),
                         "INVALID_EVENT_QUERY")

    # ----- approval boundary -----------------------------------------------------------
    def test_untrusted_approval_requests_never_create_a_receipt_or_effect(self):
        path = "/api/registered-approve" + self.query()
        origin = self.local()
        cases = {
            "no origin": (self.approve_body(), {}),
            "foreign origin": (self.approve_body(), {"Origin": "https://evil.test"}),
            "localhost origin for 127 host": (self.approve_body(),
                                              {"Origin": f"http://localhost:{self.port}"}),
            "stale admin token": (self.approve_body(admin_token="stale-" + self.token), origin),
            "non-string admin token": (self.approve_body(admin_token=None), origin),
            "missing admin token": ({key: value for key, value in self.approve_body().items()
                                     if key != "admin_token"}, origin),
            "extra key": ({**self.approve_body(), "approved": True}, origin),
            "cross-site fetch": (self.approve_body(), {**origin, "Sec-Fetch-Site": "cross-site"}),
            "forwarded": (self.approve_body(), {**origin, "Forwarded": "for=192.0.2.1"}),
            "x-forwarded-host": (self.approve_body(), {**origin, "X-Forwarded-Host": "evil.test"}),
            "tailnet over http": (self.approve_body(), {"Host": TAILNET_HOST,
                                                        "Origin": "http://" + TAILNET_HOST}),
            "tailnet forwarded http": (self.approve_body(), {
                "Host": TAILNET_HOST, "Origin": TRUSTED_APPROVAL_ORIGIN,
                "X-Forwarded-Proto": "http"}),
        }
        for name, (body, headers) in cases.items():
            with self.subTest(name):
                status, value = self.request("POST", path, body, headers=headers)
                self.assertEqual(status, 403, value)
                self.assertNoReceiptOrEffect()
        status, _ = self.request("POST", path, self.approve_body(), auth=False, headers=origin)
        self.assertEqual(status, 401)
        self.assertNoReceiptOrEffect()
        # Fresh authentication passed, but the typed confirmation does not match.
        status, value = self.request("POST", path, self.approve_body(confirmation="f" * 32),
                                     headers=origin)
        self.assertEqual((status, value.get("code")), (400, "HOST_APPROVAL_DENIED"))
        status, value = self.request("POST", path, self.approve_body(plan_digest="0" * 64),
                                     headers=origin)
        self.assertEqual((status, value.get("code")), (400, "STALE_PLAN"))
        self.assertNoReceiptOrEffect()
        # The registry decides the scope: another project's target is refused.
        status, value = self.request("POST", "/api/registered-approve" + self.query(
            "alpha", TARGET), self.approve_body(), headers=origin)
        self.assertEqual((status, value.get("code")), (400, "UNSUPPORTED_SCOPE"))
        self.assertNoReceiptOrEffect()
        self.assertEqual(self.docker.counts.get("restart"), None)

    def test_lan_bound_server_cannot_approve(self):
        with patch("admin_server.ThreadingHTTPServer", side_effect=lambda addr, handler:
                   ThreadingHTTPServer(("127.0.0.1", 0), handler)):
            lan = self.start(bind_host="192.168.2.70")
        host = f"192.168.2.70:{lan.server_port}"
        status, value = self.request("POST", "/api/registered-approve" + self.query(),
                                     self.approve_body(), port=lan.server_port,
                                     headers={"Host": host, "Origin": "http://" + host})
        self.assertEqual(status, 403, value)
        status, value = self.request("POST", "/api/registered-reject" + self.query(),
                                     {"operation_id": self.plan["operation_id"],
                                      "confirmation": self.plan["operation_id"],
                                      "admin_token": self.token},
                                     port=lan.server_port,
                                     headers={"Host": host, "Origin": "http://" + host})
        self.assertEqual(status, 403, value)
        # Reads still work on the LAN listener; only approvals are loopback/tailnet bound.
        status, _ = self.request("GET", "/api/registered-operations" + self.query(),
                                 port=lan.server_port, headers={"Host": host})
        self.assertEqual(status, 200)
        # Even a LAN listener that knows the trusted tailnet origin never approves.
        with patch("admin_server.ThreadingHTTPServer", side_effect=lambda addr, handler:
                   ThreadingHTTPServer(("127.0.0.1", 0), handler)):
            lan_tailnet = self.start(bind_host="192.168.2.70",
                                     public_origin=TRUSTED_APPROVAL_ORIGIN)
        status, value = self.request("POST", "/api/registered-approve" + self.query(),
                                     self.approve_body(), port=lan_tailnet.server_port,
                                     headers={"Host": TAILNET_HOST,
                                              "Origin": TRUSTED_APPROVAL_ORIGIN})
        self.assertEqual(status, 403, value)
        self.assertNoReceiptOrEffect()

    def test_approval_writes_exactly_one_receipt_and_never_executes(self):
        path = "/api/registered-approve" + self.query()
        status, value = self.request("POST", path, self.approve_body(), headers=self.local())
        self.assertEqual(status, 200, value)
        self.assertEqual(value, {"operation_id": self.plan["operation_id"], "approved": True,
                                 "state": "approved", "plan_digest": self.plan["plan_digest"]})
        self.assertEqual(self.receipts(), [self.plan["operation_id"] + ".json"])
        self.assertEqual(self.runner.effects, [])
        self.assertEqual(self.state(), "approved")
        status, value = self.request("POST", path, self.approve_body(), headers=self.local())
        self.assertEqual((status, value.get("code")), (400, "NOT_PENDING_APPROVAL"))
        self.assertEqual(self.receipts(), [self.plan["operation_id"] + ".json"])
        status, value = self.request("POST", "/api/registered-reject" + self.query(), {
            "operation_id": self.plan["operation_id"], "confirmation": self.plan["operation_id"],
            "admin_token": self.token}, headers=self.local())
        self.assertEqual((status, value.get("code")), (400, "NOT_PENDING_APPROVAL"))
        self.assertEqual(self.runner.effects, [])

    def test_trusted_tailnet_origin_approves_with_fresh_token(self):
        status, value = self.request("POST", "/api/registered-approve" + self.query(),
                                     self.approve_body(), headers={
                                         "Host": TAILNET_HOST, "Origin": TRUSTED_APPROVAL_ORIGIN})
        self.assertEqual(status, 200, value)
        self.assertEqual(self.receipts(), [self.plan["operation_id"] + ".json"])
        self.assertEqual(self.runner.effects, [])

    def test_reject_needs_the_same_fresh_authentication(self):
        path = "/api/registered-reject" + self.query()
        body = {"operation_id": self.plan["operation_id"],
                "confirmation": self.plan["operation_id"], "admin_token": self.token}
        for name, (payload, headers) in {
                "no origin": (body, {}),
                "foreign origin": (body, {"Origin": "https://evil.test"}),
                "stale token": ({**body, "admin_token": "old"}, self.local()),
                "approve-shaped body": (self.approve_body(), self.local()),
                "forwarded": (body, {**self.local(), "Forwarded": "for=192.0.2.1"})}.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", path, payload, headers=headers)[0], 403)
                self.assertNoReceiptOrEffect()
        status, value = self.request("POST", path, body, headers=self.local())
        self.assertEqual(status, 200, value)
        self.assertEqual((value["state"], value["code"]), ("rejected", "HOST_REJECTED"))
        status, value = self.request("POST", "/api/registered-approve" + self.query(),
                                     self.approve_body(), headers=self.local())
        self.assertEqual((status, value.get("code")), (400, "NOT_PENDING_APPROVAL"))
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.runner.effects, [])

    def test_unverified_runtime_cannot_list_or_approve(self):
        from host_registered_tools import create_registered_manager
        with patch("host_registered_tools.CONFIG_PATH", self.fx["config"]):
            production = create_registered_manager(self.projects, self.state_dir)
        self.ops = production  # The default wiring: real runner, no injected adapter.
        status, targets = self.request("GET", "/api/registered-targets?project=tossApp")
        self.assertEqual((status, targets["enabled"], targets["reason"]),
                         (200, False, "UNSAFE_RUNTIME"))
        status, value = self.request("GET", "/api/registered-operations" + self.query())
        self.assertEqual((status, value.get("code")), (400, "UNSAFE_RUNTIME"))
        status, value = self.request("POST", "/api/registered-approve" + self.query(),
                                     self.approve_body(), headers=self.local())
        self.assertEqual((status, value.get("code")), (400, "UNSAFE_RUNTIME"))
        self.assertNoReceiptOrEffect()

    # ----- link and activation ---------------------------------------------------------
    def test_approval_link_opens_the_admin_page_and_never_carries_a_token(self):
        link = self.plan["approval_url"]
        self.assertEqual(link, "https://cantopsbuildserver.tail661d29.ts.net:8443"
                               "/host-operations?project=tossApp&target=tarororo-test"
                               "&operation_id=" + self.plan["operation_id"])
        self.assertNotIn("token", link)
        self.assertNotIn("nonce", link)
        parsed = urlsplit(link)
        page_path = parsed.path + "?" + parsed.query
        status, page = self.request("GET", page_path, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(page, (MODULE_DIR / "admin.html").read_bytes())
        for bad in (page_path + "&token=" + self.token, page_path + "&admin_token=x",
                    "/host-operations?project=tossApp&target=tarororo-test",
                    page_path.replace(self.plan["operation_id"], "XYZ"),
                    page_path + "&operation_id=" + "a" * 32):
            with self.subTest(bad=bad):
                self.assertEqual(self.request("GET", bad, auth=False)[0], 400)

    def test_invalid_approval_link_explains_recovery_without_reflecting_query(self):
        secret = "link-private-value"
        for query in ("", "?project=tossApp", "?token=" + secret,
                      "?project=%3Cscript%3E&target=tarororo-test&operation_id=bad"):
            with self.subTest(query=query):
                status, page = self.request("GET", "/host-operations" + query, auth=False)
                self.assertEqual(status, 400)
                self.assertIsInstance(page, bytes)
                self.assertIn("승인 링크를 확인해 주세요".encode(), page)
                self.assertIn('href="/"'.encode(), page)
                self.assertNotIn(secret.encode(), page)
                self.assertNotIn(b"<script", page)
        self.assertNoReceiptOrEffect()

    def test_no_endpoint_changes_activation_and_legacy_shapes_stay(self):
        for path in ("/api/registered-enable", "/api/registered-config",
                     "/api/registered-operations", "/api/registered-targets"):
            with self.subTest(path=path):
                status, _ = self.request("POST", path + self.query(), {"admin_token": self.token},
                                         headers=self.local())
                self.assertEqual(status, 405)
        for method in ("PUT", "DELETE"):
            self.assertIn(self.request(method, "/api/registered-approve" + self.query(),
                                       self.approve_body(), headers=self.local())[0], (405, 400))
        expected = {"project": PROJECT, "target": TARGET, "plans": []}
        with patch("admin_server.pending_operations", return_value=expected):
            self.assertEqual(self.request("GET", "/api/host-operations?project=tossApp"),
                             (200, expected))
        self.assertEqual(self.fx["config"].read_bytes(), self.config_bytes)
        self.assertNoReceiptOrEffect()


if __name__ == "__main__":
    unittest.main()
