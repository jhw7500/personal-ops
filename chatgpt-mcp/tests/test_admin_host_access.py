"""Real HTTP grant authentication tests with disposable state, never host commands."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_server import create_server  # noqa: E402
from host_access import HostAccess  # noqa: E402


class FixtureExecution:
    def __init__(self):
        self.cancelled = []

    def list(self, project):
        return {"project": project, "executions": []}

    def cancel(self, project, operation_id):
        self.cancelled.append(operation_id)
        return {"operation_id": operation_id, "state": "cancelled"}


class AdminHostAccessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="owner-host-access-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        project = self.root / "project"
        project.mkdir()
        self.projects = {"demo": project}
        self.access = HostAccess(self.projects, self.root / "state", trusted_runtime=True)
        self.requested = self.access.request("demo")
        self.execution = FixtureExecution()
        token = self.root / "token"
        self.token = "a" * 40
        token.write_text(self.token)
        token.chmod(0o600)
        self.server = create_server(
            self.projects, self.root / "state", token_path=token, port=0,
            host_access_factory=lambda *_a, **_k: self.access,
            host_execution_factory=lambda *_a, **_k: self.execution)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(3)
        self.addCleanup(stop)

    def request(self, method, route, body=None, *, auth=True, origin=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        fields = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        if auth:
            fields["Authorization"] = "Bearer " + self.token
        if origin:
            fields["Origin"] = "http://" + fields["Host"]
        fields.update(headers or {})
        conn.request(method, route, json.dumps(body) if body is not None else None, fields)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw)

    def body(self, **changes):
        return {"request_id": self.requested["request_id"], "duration_seconds": 14400,
                "allow_codex": True, "confirmation": "demo", "admin_token": self.token,
                **changes}

    def test_grant_status_and_logs_require_authentication(self):
        for path in ("/api/host-access?project=demo", "/api/host-executions?project=demo",
                     "/api/host-execution-log?project=demo&operation_id=" + "a" * 32):
            self.assertEqual(self.request("GET", path, auth=False)[0], 401)
        code, state = self.request("GET", "/api/host-access?project=demo")
        self.assertEqual(code, 200)
        self.assertFalse(state["active"])
        self.assertNotIn(self.token, json.dumps(state))

    def test_bad_origin_token_extra_body_or_fetch_site_never_grants(self):
        endpoint = "/api/host-access-approve?project=demo"
        for options in ({"origin": False}, {"headers": {"Origin": "https://attacker.invalid"}},
                        {"headers": {"Host": "outside.invalid"}},
                        {"headers": {"Sec-Fetch-Site": "cross-site"}},
                        {"headers": {"Forwarded": "host=localhost"}}):
            self.assertEqual(self.request("POST", endpoint, self.body(), **options)[0], 403)
        for body in (self.body(admin_token="wrong"), self.body(approved=True)):
            self.assertEqual(self.request("POST", endpoint, body)[0], 403)
        self.assertFalse(self.access.get("demo")["active"])

    def test_owner_grant_and_revoke_no_execution_and_no_replay(self):
        endpoint = "/api/host-access-approve?project=demo"
        code, grant = self.request("POST", endpoint, self.body())
        self.assertEqual(code, 200)
        self.assertTrue(self.access.get("demo")["active"])
        self.assertEqual(self.execution.cancelled, [])
        self.assertEqual(self.request("POST", endpoint, self.body())[0], 400)
        body = {"grant_id": grant["grant_id"], "confirmation": "demo", "admin_token": self.token}
        self.assertEqual(self.request("POST", "/api/host-access-revoke?project=demo", body)[0], 200)
        self.assertFalse(self.access.get("demo")["active"])

    def test_wrong_project_or_confirmation_or_duration_is_denied(self):
        endpoint = "/api/host-access-approve?project=demo"
        for changes in ({"confirmation": "another"}, {"duration_seconds": 2592001},
                        {"duration_seconds": True}, {"allow_codex": "true"}):
            self.assertEqual(self.request("POST", endpoint, self.body(**changes))[0], 400)
        self.assertEqual(self.request("POST", endpoint + "&project=demo", self.body())[0], 400)
        self.assertEqual(self.request("POST", endpoint.replace("demo", "missing"), self.body())[0], 400)
        self.assertFalse(self.access.get("demo")["active"])

    def test_owner_can_grant_thirty_days_without_starting_execution(self):
        code, _grant = self.request("POST", "/api/host-access-approve?project=demo",
                                    self.body(duration_seconds=2592000))
        self.assertEqual(code, 200)
        self.assertTrue(self.access.get("demo")["active"])
        self.assertEqual(self.execution.cancelled, [])

    def test_owner_api_accepts_optional_lifetime_but_legacy_rejects_durable_grant(self):
        endpoint = "/api/host-access-approve?project=demo"
        body = self.body(lifetime="timed")
        self.assertEqual(self.request("POST", endpoint, body)[0], 200)

        replacement = self.access.request("demo")
        durable = self.body(
            request_id=replacement["request_id"], lifetime="until_revoked",
            duration_seconds=None,
        )
        self.assertEqual(self.request("POST", endpoint, durable)[0], 400)

    def test_cancel_requires_fresh_owner_authentication(self):
        body = {"operation_id": "a" * 32, "confirmation": "demo", "admin_token": self.token}
        route = "/api/host-execution-cancel?project=demo"
        self.assertEqual(self.request("POST", route, body, origin=False)[0], 403)
        self.assertEqual(self.execution.cancelled, [])
        self.assertEqual(self.request("POST", route, body)[0], 200)
        self.assertEqual(self.execution.cancelled, ["a" * 32])


if __name__ == "__main__":
    unittest.main()
