"""Persistent target authorization HTTP boundary; no host operation is executed."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from admin_server import create_server  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from host_runtime_web import (  # noqa: E402
    pending_operations, set_target_authorization, target_authorization,
)


class TargetAuthorizationHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        project = root / "tossApp"
        project.mkdir()
        other = root / "redmine"
        other.mkdir()
        self.host_ops = Mock()
        self.server = create_server(
            {"tossApp": project, "redmine": other}, root / "state",
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

    def request(self, method, path, payload=None, *, auth=True, origin=None,
                host=None, forwarded=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        if forwarded:
            headers["Forwarded"] = "for=example.test"
        body = None if payload is None else json.dumps(payload)
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        result = json.loads(response.read())
        conn.close()
        return response.status, result

    def test_get_is_authenticated_exact_scope_and_allowed_from_lan_read_path(self):
        expected = {
            "supported": True,
            "enabled": False,
            "project": "tossApp",
            "target": "tarororo-test",
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "grant_id": None,
            "granted_at": None,
            "reason": None,
        }
        self.host_ops.target_authorization.return_value = expected
        path = "/api/host-target-authorization?project=tossApp&target=tarororo-test"

        self.assertEqual(self.request("GET", path, auth=False)[0], 401)
        self.assertEqual(self.request("GET", path.replace("tarororo-test", "other"))[0], 400)
        self.assertEqual(self.request("GET", path + "&extra=1")[0], 400)
        status, result = self.request("GET", path)

        self.assertEqual(status, 200, result)
        self.assertEqual(result, expected)
        self.host_ops.target_authorization.assert_called_once_with("tossApp", "tarororo-test")

    def test_post_requires_fresh_local_auth_exact_boolean_body_and_never_executes(self):
        path = "/api/host-target-authorization?project=tossApp&target=tarororo-test"
        origin = f"http://127.0.0.1:{self.port}"
        payload = {"enabled": True, "confirmation": "tossApp/tarororo-test",
                   "admin_token": self.token}
        enabled = {
            "supported": True, "enabled": True, "project": "tossApp",
            "target": "tarororo-test", "components": ["api", "worker", "migration",
                                                               "predecessor_migration"],
            "grant_id": "a" * 32, "granted_at": "2026-10-14T00:00:00+00:00",
            "reason": None,
        }
        self.host_ops.set_target_authorization.return_value = enabled

        self.assertEqual(self.request("POST", path, payload)[0], 403)
        self.assertEqual(self.request("POST", path, payload, origin=origin, auth=False)[0], 401)
        self.assertEqual(self.request("POST", path, payload, origin=origin, forwarded=True)[0], 403)
        self.assertEqual(self.request("POST", path, {**payload, "enabled": 1}, origin=origin)[0], 400)
        self.assertEqual(self.request("POST", path, {**payload, "confirmation": "wrong"},
                                      origin=origin)[0], 400)
        self.assertEqual(self.request("POST", path, {**payload, "extra": True}, origin=origin)[0], 403)
        self.assertEqual(self.request("POST", path + "&extra=1", payload, origin=origin)[0], 400)
        self.assertFalse(self.host_ops.set_target_authorization.called)

        status, result = self.request("POST", path, payload, origin=origin)

        self.assertEqual(status, 200, result)
        self.assertEqual(result, enabled)
        self.host_ops.set_target_authorization.assert_called_once_with(
            "tossApp", "tarororo-test", enabled=True,
            confirmation="tossApp/tarororo-test")
        for name in ("execute", "execute_host_operation", "execute_test_deploy",
                     "execute_db_migration", "execute_predecessor_migration"):
            self.assertFalse(getattr(self.host_ops, name).called, name)

    def test_tailnet_can_revoke_but_untrusted_and_lan_origins_cannot_mutate(self):
        path = "/api/host-target-authorization?project=tossApp&target=tarororo-test"
        host = "cantopsbuildserver.tail661d29.ts.net:8443"
        tailnet = "https://" + host
        payload = {"enabled": False, "confirmation": "tossApp/tarororo-test",
                   "admin_token": self.token}
        revoked = {"supported": True, "enabled": False, "project": "tossApp",
                   "target": "tarororo-test", "components": ["api", "worker", "migration",
                                                                    "predecessor_migration"],
                   "grant_id": None, "granted_at": None, "reason": None}
        self.host_ops.set_target_authorization.return_value = revoked

        self.assertEqual(self.request("POST", path, payload,
                                      host=host, origin="https://evil.test")[0], 403)
        self.assertEqual(self.request("POST", path, payload,
                                      host=host, origin="http://" + host)[0], 403)
        self.assertFalse(self.host_ops.set_target_authorization.called)

        status, result = self.request("POST", path, payload, host=host, origin=tailnet)

        self.assertEqual(status, 200, result)
        self.assertEqual(result, revoked)
        self.host_ops.set_target_authorization.assert_called_once_with(
            "tossApp", "tarororo-test", enabled=False,
            confirmation="tossApp/tarororo-test")

    def test_unavailable_state_is_sanitized(self):
        path = "/api/host-target-authorization?project=tossApp&target=tarororo-test"
        self.host_ops.target_authorization.side_effect = HostRuntimeError("SECRET_INTERNAL")

        status, result = self.request("GET", path)

        self.assertEqual(status, 400)
        self.assertEqual(result, {"error": "Invalid request or unavailable state"})
        self.assertNotIn("SECRET_INTERNAL", json.dumps(result))


class TargetAuthorizationWebWrapperTest(unittest.TestCase):
    def test_wrappers_use_exact_fixed_scope_and_preserve_unsupported_state(self):
        operations = Mock()
        unsupported = {"supported": False, "enabled": False, "project": "tossApp",
                       "target": "tarororo-test", "components": [], "grant_id": None,
                       "granted_at": None, "reason": "AUTHORIZATION_UNAVAILABLE"}
        operations.target_authorization.return_value = unsupported

        self.assertEqual(target_authorization(operations, "tossApp", "tarororo-test"),
                         unsupported)
        operations.target_authorization.assert_called_once_with("tossApp", "tarororo-test")
        with self.assertRaises(HostRuntimeError):
            target_authorization(operations, "redmine", "tarororo-test")
        with self.assertRaises(HostRuntimeError):
            set_target_authorization(operations, "tossApp", "other", enabled=True,
                                     confirmation="tossApp/other")

        class OlderRuntime:
            pass

        fallback = target_authorization(OlderRuntime(), "tossApp", "tarororo-test")
        self.assertEqual(fallback["supported"], False)
        self.assertEqual(fallback["reason"], "AUTHORIZATION_UNAVAILABLE")
        with self.assertRaisesRegex(HostRuntimeError, "AUTHORIZATION_UNAVAILABLE"):
            set_target_authorization(OlderRuntime(), "tossApp", "tarororo-test",
                                     enabled=True, confirmation="tossApp/tarororo-test")

    def test_pending_combined_plan_exposes_safe_target_authorization_and_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation_id = "a" * 32
            operations = Mock()
            operations.operations_dir = root / "operations"
            operations.operations_dir.mkdir(mode=0o700)
            operations.lock_path = root / "operations.lock"
            operations.runtime.guard.return_value = root
            operations.planning_policy.return_value = {
                "mode": "reviewed_snapshot", "components": ["api", "worker", "migration",
                                                                   "predecessor_migration"],
                "commit_required": False, "approval": "target",
            }
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            state = {
                "operation_id": operation_id, "state": "pending_host_approval",
                "plan_digest": "b" * 64,
                "immutable": {
                    "component": "schema_update", "mode": "disabled",
                    "source_digest": "c" * 64, "expires_at": expires,
                    "high_risk_changes": [], "changed_paths": [],
                    "migration_checksums": {"006_x.sql": "d" * 64,
                                              "013_staged_reading_access.sql": "e" * 64},
                    "database_generation": "f" * 64,
                    "authorization": {"mode": "target", "grant_id": "1" * 32,
                                      "config_digest": "2" * 64},
                },
            }
            path = operations.operations_dir / (operation_id + ".json")
            path.write_text(json.dumps(state))
            path.chmod(0o600)

            result = pending_operations(operations)

        self.assertEqual(result["plans"][0]["migration_range"], ["006", "013"])
        self.assertEqual(result["plans"][0]["impact"]["schema_change"],
                         "006-013_combined")
        self.assertEqual(result["plans"][0]["authorization"], {
            "mode": "target", "grant_id": "1" * 32, "config_digest": "2" * 64})


if __name__ == "__main__":
    unittest.main()
