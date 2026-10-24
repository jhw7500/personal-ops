"""Observed discovery must be distinguished from a healthy idle MCP child."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from rollout_catalog import observe_tools
from rollout_tunnel import TunnelSwitchError


class Response(io.BytesIO):
    status = 200


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.url_file = Path(temporary.name) / "runtime.url"
        self.url_file.write_text("http://127.0.0.1:12345")
        self.url_file.chmod(0o600)
        self.expected = {"get_runtime_status", "get_db_migration_status",
                         "get_predecessor_migration_status",
                         "prepare_db_migration", "prepare_predecessor_migration",
                         "execute_predecessor_migration", "execute_db_migration"}

    def getter(self, *, names, complete=True, ok=True):
        body = {"details": {"channel": "main", "transport": "stdio",
                            "child_state": "running", "evidence": "same_child",
                            "tools_list": {"ok": ok, "complete": complete,
                                           "limited": False, "partial": not complete,
                                           "tool_names": names, "retained_count": len(names)}}}
        def get(url, **kw):
            self.assertEqual(url, "http://127.0.0.1:12345/health/mcp")
            return Response(json.dumps(body).encode())
        return get

    def test_idle_healthy_child_is_awaiting_discovery_not_verified_or_failed(self):
        result = observe_tools(self.expected, url_file=self.url_file,
                               urlopen_fn=self.getter(names=[], complete=False, ok=False))
        self.assertEqual(result["catalog_status"], "awaiting_discovery")
        self.assertFalse(result["tools_registered"])

    def test_complete_discovery_must_include_the_new_tools(self):
        with self.assertRaisesRegex(TunnelSwitchError, "LIVE_TOOLS_MISMATCH"):
            observe_tools(self.expected, url_file=self.url_file,
                          urlopen_fn=self.getter(names=["get_runtime_status"]))

    def test_complete_matching_names_do_not_claim_schema_verification(self):
        result = observe_tools(self.expected, url_file=self.url_file,
                               urlopen_fn=self.getter(names=sorted(self.expected)))
        self.assertTrue(result["tools_registered"])
        self.assertEqual(result["catalog_status"], "observed")
        self.assertFalse(result["connector_schemas_verified"])

    def test_tool_payload_is_bounded_before_json_parse(self):
        with self.assertRaisesRegex(TunnelSwitchError, "UNEXPECTED_MCP_DISCOVERY"):
            observe_tools(self.expected, url_file=self.url_file,
                          urlopen_fn=lambda *a, **kw: Response(b"x" * (262144 + 1)))

    def test_external_or_credentialled_url_is_rejected_without_request(self):
        for address in ("https://example.invalid", "http://token@127.0.0.1:12345",
                        "http://127.0.0.1:12345?key=private", "http://127.0.0.1:12345/#token=private"):
            with self.subTest(address=address):
                self.url_file.write_text(address)
                with self.assertRaisesRegex(TunnelSwitchError, "UNSAFE_MCP_HEALTH_URL"):
                    observe_tools(self.expected, url_file=self.url_file,
                                  urlopen_fn=lambda *a, **kw: self.fail("must not connect"))


if __name__ == "__main__":
    unittest.main()
