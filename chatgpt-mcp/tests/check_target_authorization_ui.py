#!/usr/bin/env python3
"""Real-browser smoke check for durable target authorization; system Python only."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from admin_server import create_server  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        project = root / "tossApp"
        project.mkdir()
        host_ops = Mock()
        authorization = {
            "supported": True,
            "enabled": False,
            "project": "tossApp",
            "target": "tarororo-test",
            "components": ["api", "worker", "migration", "predecessor_migration"],
            "grant_id": None,
            "granted_at": None,
            "reason": "NOT_GRANTED",
        }

        def read_authorization(_operations, project_name, target):
            assert (project_name, target) == ("tossApp", "tarororo-test")
            return copy.deepcopy(authorization)

        def write_authorization(_operations, project_name, target, *, enabled, confirmation):
            assert (project_name, target, confirmation) == (
                "tossApp", "tarororo-test", "tossApp/tarororo-test")
            authorization.update(
                enabled=enabled,
                grant_id="1" * 32 if enabled else None,
                granted_at="2026-10-14T00:00:00+00:00" if enabled else None,
                reason=None if enabled else "REVOKED",
            )
            return copy.deepcopy(authorization)

        patches = (
            patch("admin_server.pending_operations", return_value={
                "project": "tossApp", "target": "tarororo-test",
                "planning_policy": {
                    "mode": "reviewed_snapshot",
                    "components": ["api", "worker", "migration", "predecessor_migration"],
                    "commit_required": False,
                    "approval": "per_operation",
                },
                "plans": [],
            }),
            patch("admin_server.target_authorization", side_effect=read_authorization),
            patch("admin_server.set_target_authorization", side_effect=write_authorization),
        )
        for active in patches:
            active.start()
        try:
            server = create_server(
                {"tossApp": project}, root / "state",
                token_path=root / "token" / "admin", port=0,
                host_ops_factory=lambda _projects, **_kwargs: host_ops,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                token = (root / "token" / "admin").read_text().strip()
                origin = f"http://127.0.0.1:{server.server_port}"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(origin + "/#token=" + token)
                        page.locator("#nav-access").click()
                        page.locator("#host-target-auth-controls:not([hidden])").wait_for()
                        assert "허용 안 됨" in page.locator("#host-target-auth-state").inner_text()

                        page.locator("#host-target-auth-confirmation").fill(
                            "tossApp/tarororo-test")
                        page.locator("#host-target-auth-token").fill(token)
                        page.locator("#host-target-auth-enable").click()
                        page.locator("#host-target-auth-status").get_by_text(
                            "채팅 자동 실행 권한을 허용했습니다", exact=False).wait_for()
                        assert "채팅 자동 실행 허용" in page.locator(
                            "#host-target-auth-state").inner_text()

                        page.locator("#host-target-auth-confirmation").fill(
                            "tossApp/tarororo-test")
                        page.locator("#host-target-auth-token").fill(token)
                        page.locator("#host-target-auth-disable").click()
                        page.locator("#host-target-auth-status").get_by_text(
                            "채팅 자동 실행 권한을 취소했습니다", exact=False).wait_for()
                        assert "허용 안 됨" in page.locator(
                            "#host-target-auth-state").inner_text()
                        assert token not in page.locator("body").inner_text()
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        finally:
            for active in reversed(patches):
                active.stop()
    print("target authorization browser flow: OK")


if __name__ == "__main__":
    main()
