"""Activation regressions using temporary state, fake Docker and isolated imports."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE_DIR), str(Path(__file__).resolve().parent)]
from admin_server import create_server  # noqa: E402
from host_operation_fixtures import fake_registered_manager, write_registered_fixture  # noqa: E402
from host_runtime_config import PROJECT, TARGET  # noqa: E402


class ActivationFixTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stage1-activation-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.fx = write_registered_fixture(self.home)

    def test_default_manager_writes_plans_under_xdg_state_home(self):
        xdg = self.home / "xdg"
        with (patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}),
              patch("host_runtime_operations.Path.home", return_value=self.home / "fake-home")):
            ops, runner, _ = fake_registered_manager(self.fx["projects"], self.fx["config"], None)
            plan = ops.prepare(PROJECT, TARGET, action="compose.restart",
                               parameters={"services": ["api"]}, request_id="xdg")
        expected = xdg / "chatgpt-coding-mcp" / "host-runtime" / "registered" / "operations"
        self.assertTrue((expected / (plan["operation_id"] + ".json")).is_file())
        self.assertEqual(runner.effects, [])

    def test_admin_finds_and_approves_plan_in_supplied_state_directory(self):
        state = self.home / "admin-state"
        ops, runner, _ = fake_registered_manager(
            self.fx["projects"], self.fx["config"], state / "host-runtime")
        plan = ops.prepare(PROJECT, TARGET, action="compose.restart",
                           parameters={"services": ["api"]}, request_id="shared-state")

        def factory(projects, state_dir=None):
            return fake_registered_manager(projects, self.fx["config"],
                                           state_dir or self.home / "wrong-state")[0]

        token_path = self.home / "tokens" / "admin"
        admin = create_server(self.fx["projects"], state, token_path, port=0,
                              registered_ops_factory=factory)
        thread = threading.Thread(target=admin.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (admin.shutdown(), admin.server_close(), thread.join(5)))
        token = token_path.read_text().strip()

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", admin.server_port, timeout=10)
            headers = {"Authorization": "Bearer " + token,
                       "Origin": f"http://127.0.0.1:{admin.server_port}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            return response.status, data

        scope = "?project=tossApp&target=tarororo-test"
        status, listing = request("GET", "/api/registered-operations" + scope)
        self.assertEqual(status, 200, listing)
        self.assertEqual([row["operation_id"] for row in listing["operations"]],
                         [plan["operation_id"]])
        status, result = request("POST", "/api/registered-approve" + scope, {
            "operation_id": plan["operation_id"], "plan_digest": plan["plan_digest"],
            "source_digest": plan["source_digest"], "confirmation": plan["operation_id"],
            "admin_token": token})
        self.assertEqual((status, result["state"]), (200, "approved"), result)
        self.assertEqual(ops.get(PROJECT, TARGET, plan["operation_id"])["state"], "approved")
        self.assertEqual(runner.effects, [])

    def test_plan_links_to_the_trusted_https_approval_page(self):
        ops, runner, _ = fake_registered_manager(
            self.fx["projects"], self.fx["config"], self.home / "state")
        plan = ops.prepare(PROJECT, TARGET, action="compose.restart",
                           parameters={"services": ["api"]}, request_id="clickable-link")
        url = urlsplit(plan["approval_url"])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "cantopsbuildserver.tail661d29.ts.net:8443", "/host-operations"))
        self.assertEqual(url.query, "project=tossApp&target=tarororo-test&operation_id="
                         + plan["operation_id"])
        self.assertFalse(url.fragment)
        self.assertNotIn("token", plan["approval_url"])
        self.assertEqual(runner.effects, [])

    def test_missing_optional_module_keeps_admin_alive_and_registered_approval_closed(self):
        script = r'''
import http.client, json, sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.modules[sys.argv[2]] = None
from admin_server import create_server
with tempfile.TemporaryDirectory(prefix="admin-missing-module-") as home:
    root = Path(home)
    project = root / "project"
    project.mkdir()
    token = root / "tokens" / "admin"
    server = create_server({"tossApp": project}, root / "state", token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": "Bearer " + token.read_text().strip(),
                   "Origin": "http://127.0.0.1:" + str(server.server_port)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body) if body else None, headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data
    try:
        status, _ = request("GET", "/api/status")
        targets_status, targets = request("GET", "/api/registered-targets?project=tossApp")
        approve_status, approval = request("POST", "/api/registered-approve?project=tossApp&target=tarororo-test", {
            "operation_id": "a" * 32, "plan_digest": "b" * 64, "source_digest": "c" * 64,
            "confirmation": "a" * 32, "admin_token": token.read_text().strip()})
        receipts = list((root / "state").rglob("*.approval.json")) + list((root / "state").rglob("receipts/*.json"))
        print(json.dumps({"status": status, "targets_status": targets_status,
                          "enabled": targets["enabled"], "reason": targets["reason"],
                          "approve_status": approve_status, "code": approval["code"],
                          "receipts": len(receipts)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
'''
        for module in ("host_registry", "host_registered_operations", "host_registered_tools"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-c", script, str(MODULE_DIR), module],
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[-1000:])
                self.assertEqual(json.loads(result.stdout), {
                    "status": 200, "targets_status": 200, "enabled": False,
                    "reason": "REGISTERED_OPERATIONS_UNAVAILABLE", "approve_status": 400,
                    "code": "REGISTERED_OPERATIONS_UNAVAILABLE", "receipts": 0})

    def test_present_optional_module_import_defect_is_not_hidden(self):
        script = r'''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class BrokenImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == sys.argv[3]:
            if sys.argv[2] == "api":
                raise ImportError("stage1 fixture import contract broken")
            raise ModuleNotFoundError("stage1 fixture missing dependency",
                                      name="stage1_nonoptional_dependency")
sys.meta_path.insert(0, BrokenImport())
__import__(sys.argv[4])
'''
        for entrypoint in ("server", "admin_server"):
            for module in ("host_registered_tools", "host_registered_operations"):
                for defect in ("api", "dependency"):
                    with self.subTest(entrypoint=entrypoint, module=module, defect=defect):
                        result = subprocess.run(
                            [sys.executable, "-c", script, str(MODULE_DIR), defect, module,
                             entrypoint],
                            text=True, capture_output=True, timeout=15)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("stage1 fixture", result.stderr)
