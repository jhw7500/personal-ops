from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from catalog_refresh import CatalogRefresh, CatalogRefreshError, tool_specs


MODULE_DIR = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, MODULE_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Session:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.notifications = 0

    async def send_tool_list_changed(self):
        self.notifications += 1
        if self.failure is not None:
            raise self.failure


class CatalogRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_is_per_session_and_only_a_later_successful_list_confirms_refetch(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        first = Session()
        second = Session()

        requested = await refresh.call(
            "refresh_mcp_catalog", {"project": "personal-ops"}, first)
        self.assertEqual(requested, {
            "project": "personal-ops", "status": "notification_sent",
            "request_sequence": 1, "notification_sent": True,
            "catalog_refetched": False, "chatgpt_metadata_verified": False,
        })
        self.assertEqual(first.notifications, 1)
        self.assertEqual(
            (await refresh.call(
                "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, second
            ))["status"],
            "idle",
        )

        listed = await refresh.list_tools(first, self._catalog, client_requested=True)
        self.assertEqual(listed, ["one", "two"])
        current = await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, first)
        self.assertEqual(current["status"], "catalog_refetched")
        self.assertTrue(current["catalog_refetched"])
        self.assertFalse(current["chatgpt_metadata_verified"])

    async def test_repeated_and_concurrent_requests_are_ordered_and_each_notifies_once(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session()

        results = await asyncio.gather(*(
            refresh.call("refresh_mcp_catalog", {"project": "personal-ops"}, session)
            for _ in range(4)
        ))

        self.assertEqual(session.notifications, 4)
        self.assertEqual(sorted(item["request_sequence"] for item in results), [1, 2, 3, 4])
        self.assertEqual((await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session
        ))["request_sequence"], 4)
        await refresh.list_tools(session, self._catalog, client_requested=True)
        self.assertEqual((await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session
        ))["status"], "catalog_refetched")

    async def test_list_failure_does_not_claim_refetch(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session()
        await refresh.call("refresh_mcp_catalog", {"project": "personal-ops"}, session)

        async def fail():
            raise RuntimeError("backend secret")

        with self.assertRaisesRegex(RuntimeError, "backend secret"):
            await refresh.list_tools(session, fail, client_requested=True)
        status = await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session)
        self.assertEqual(status["status"], "notification_sent")
        self.assertFalse(status["catalog_refetched"])

    async def test_list_started_before_refresh_does_not_confirm_the_later_request(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session()
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_catalog():
            started.set()
            await release.wait()
            return ["old"]

        listing = asyncio.create_task(refresh.list_tools(
            session, delayed_catalog, client_requested=True))
        await started.wait()
        await refresh.call("refresh_mcp_catalog", {"project": "personal-ops"}, session)
        release.set()
        self.assertEqual(await listing, ["old"])
        status = await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session)
        self.assertEqual(status["status"], "notification_sent")
        self.assertFalse(status["catalog_refetched"])

    async def test_internal_catalog_build_does_not_confirm_client_refetch(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session()
        await refresh.call("refresh_mcp_catalog", {"project": "personal-ops"}, session)

        listed = await refresh.list_tools(session, self._catalog, client_requested=False)

        self.assertEqual(listed, ["one", "two"])
        status = await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session)
        self.assertEqual(status["status"], "notification_sent")
        self.assertFalse(status["catalog_refetched"])

    async def test_transport_failure_is_bounded_and_status_reports_failure(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session(failure=RuntimeError("private transport detail"))

        with self.assertRaisesRegex(CatalogRefreshError, "CATALOG_REFRESH_NOTIFICATION_FAILED") as error:
            await refresh.call("refresh_mcp_catalog", {"project": "personal-ops"}, session)
        self.assertNotIn("private transport detail", str(error.exception))
        status = await refresh.call(
            "get_mcp_catalog_refresh_status", {"project": "personal-ops"}, session)
        self.assertEqual(status["status"], "notification_failed")
        self.assertFalse(status["notification_sent"])
        self.assertFalse(status["catalog_refetched"])

    async def test_wrong_scope_arguments_tool_and_context_are_rejected(self):
        refresh = CatalogRefresh({"personal-ops": object()})
        session = Session()
        invalid = (
            ("refresh_mcp_catalog", {"project": "other"}, session, "UNSUPPORTED_SCOPE"),
            ("refresh_mcp_catalog", {"project": "personal-ops", "extra": True}, session,
             "INVALID_CATALOG_REFRESH_ARGUMENTS"),
            ("unknown", {"project": "personal-ops"}, session, "UNKNOWN_CATALOG_REFRESH_TOOL"),
            ("refresh_mcp_catalog", {"project": "personal-ops"}, None,
             "INVALID_REQUEST_CONTEXT"),
        )
        for name, arguments, context, code in invalid:
            with self.subTest(code=code), self.assertRaisesRegex(CatalogRefreshError, code):
                await refresh.call(name, arguments, context)

    def test_tools_are_advertised_only_for_configured_personal_ops(self):
        self.assertEqual(tool_specs({"other": object()}), [])
        specs = tool_specs({"personal-ops": object(), "other": object()})
        self.assertEqual({item["name"] for item in specs}, {
            "refresh_mcp_catalog", "get_mcp_catalog_refresh_status",
        })
        for spec in specs:
            self.assertEqual(spec["inputSchema"], {
                "type": "object",
                "properties": {"project": {"type": "string", "enum": ["personal-ops"]}},
                "required": ["project"],
                "additionalProperties": False,
            })
            self.assertEqual(spec["annotations"]["openWorldHint"], False)
            self.assertEqual(spec["_meta"]["ui"]["visibility"], ["model"])
            self.assertFalse(spec["_meta"]["openai/widgetAccessible"])

    def test_runtime_packages_module_and_independently_expects_configured_names(self):
        installer = load_script("catalog_refresh_installer", "install-runtime.py")
        rollout = load_script("catalog_refresh_rollout", "rollout-host-runtime.py")
        self.assertIn("catalog_refresh.py", installer.FILES)
        self.assertIn("tests.test_catalog_refresh", rollout.TESTS)
        self.assertIn("tests.test_catalog_refresh_router", rollout.TESTS)
        with tempfile.TemporaryDirectory(prefix="catalog-refresh-rollout-") as folder:
            root = Path(folder)
            project = root / "personal-ops"
            project.mkdir()
            expected = rollout.expected_tool_names(
                {"personal-ops": project}, config_path=root / "missing-policy.json",
                authority_dir=root / "missing-authority",
            )
        self.assertEqual(
            expected,
            rollout.TOOL_NAMES | {
                "refresh_mcp_catalog", "get_mcp_catalog_refresh_status",
            },
        )

    @staticmethod
    async def _catalog():
        return ["one", "two"]


if __name__ == "__main__":
    unittest.main()
