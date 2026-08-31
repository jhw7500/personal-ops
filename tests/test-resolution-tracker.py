#!/usr/bin/env python3

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TRACKER = REPO_ROOT / "session-summary" / "resolution-tracker.py"


class ResolutionTrackerPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.summary = self.root / "summary.md"
        self.state = self.root / "unresolved-items.json"
        self.manifest = self.root / "manifest.json"
        self.context = self.root / "context.txt"

    def write_summary(self, incomplete_lines: list[str]) -> None:
        body = "\n".join(incomplete_lines)
        self.summary.write_text(
            "# Claude 세션 요약\n\n"
            "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
            "### 미완료 항목\n"
            f"{body}\n",
            encoding="utf-8",
        )

    def run_prepare(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(TRACKER),
                "prepare",
                "--summary",
                str(self.summary),
                "--state",
                str(self.state),
                "--manifest",
                str(self.manifest),
                "--context",
                str(self.context),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def read_manifest(self) -> dict:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_prepare_imports_legacy_open_item_with_literal_identity(self) -> None:
        self.write_summary(["- [ ] bps 배열 길이 불일치 — gstApp — 중"])

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        item = self.read_manifest()["items"][0]
        self.assertEqual("2026-05-09", item["opened_on"])
        self.assertEqual("unmapped:1", item["identity_repo_key"])
        self.assertEqual("unresolved-c0bf9d34ba97", item["id"])
        self.assertEqual("repo-unmapped", item["verification"])
        self.assertEqual([], item["candidates"])

    def test_prepare_is_deterministic_across_repeated_runs(self) -> None:
        self.write_summary(["- [ ] bps 배열 길이 불일치 — gstApp — 중"])
        first = self.run_prepare()
        self.assertEqual(0, first.returncode, first.stderr)
        first_manifest = self.manifest.read_bytes()
        first_context = self.context.read_bytes()

        second = self.run_prepare()

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(first_manifest, self.manifest.read_bytes())
        self.assertEqual(first_context, self.context.read_bytes())

    def test_prepare_distinguishes_identical_unmapped_occurrences(self) -> None:
        line = "- [ ] 동일한 미완료 — unknown — 낮"
        self.write_summary([line, line])

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        items = self.read_manifest()["items"]
        self.assertEqual(["unmapped:1", "unmapped:2"], [i["identity_repo_key"] for i in items])
        self.assertEqual(2, len({i["id"] for i in items}))

    def test_prepare_preserves_valid_summary_marker(self) -> None:
        marker_id = "unresolved-123456789abc"
        self.write_summary(
            [
                "- [ ] marker 보존 — unknown — 높",
                f"<!-- unresolved-id:{marker_id} -->",
            ]
        )

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(marker_id, self.read_manifest()["items"][0]["id"])

    def test_prepare_rejects_malformed_summary_marker(self) -> None:
        malformed = "unresolved-ABCDEFGHIJKL"
        self.write_summary(
            [
                "- [ ] marker 거부 — unknown — 높",
                f"<!-- unresolved-id:{malformed} -->",
            ]
        )

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        item_id = self.read_manifest()["items"][0]["id"]
        self.assertNotEqual(malformed, item_id)
        self.assertRegex(item_id, r"^unresolved-[0-9a-f]{12}$")

    def test_prepare_recovers_marker_when_state_json_is_malformed(self) -> None:
        marker_id = "unresolved-222222222222"
        self.write_summary(
            [
                "- [ ] state 복구 — unknown — 중",
                f"<!-- unresolved-id:{marker_id} -->",
            ]
        )
        self.state.write_text("{broken", encoding="utf-8")

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        item = self.read_manifest()["items"][0]
        self.assertEqual(marker_id, item["id"])
        self.assertEqual("repo-unmapped", item["verification"])

    def test_prepare_ignores_state_item_absent_from_summary(self) -> None:
        self.summary.write_text("# Claude 세션 요약\n", encoding="utf-8")
        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "items": [
                        {
                            "id": "unresolved-333333333333",
                            "text": "state only",
                            "project": "gstApp",
                            "priority": "중",
                            "opened_on": "2026-05-09",
                            "identity_repo_key": "unmapped:1",
                            "repo_path": None,
                            "baseline_head": None,
                            "status": "open",
                            "resolution": None,
                            "verification": "repo-unmapped",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.read_manifest()["items"])
        self.assertEqual(b"", self.context.read_bytes())

    def test_prepare_accepts_exactly_one_hundred_open_items(self) -> None:
        self.write_summary(
            [f"- [ ] open item {index:03d} — unknown — 낮" for index in range(100)]
        )

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(100, len(self.read_manifest()["items"]))
        self.assertTrue(self.context.read_text(encoding="utf-8").startswith("ITEM unresolved-"))

    def test_prepare_rejects_one_hundred_and_one_without_overwriting_outputs(self) -> None:
        self.write_summary(
            [f"- [ ] open item {index:03d} — unknown — 낮" for index in range(101)]
        )
        self.manifest.write_text("manifest sentinel\n", encoding="utf-8")
        self.context.write_text("context sentinel\n", encoding="utf-8")

        result = self.run_prepare()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("open item limit exceeded", result.stderr)
        self.assertEqual("manifest sentinel\n", self.manifest.read_text(encoding="utf-8"))
        self.assertEqual("context sentinel\n", self.context.read_text(encoding="utf-8"))

    def test_prepare_outputs_are_private(self) -> None:
        self.write_summary(["- [ ] private artifacts — unknown — 낮"])

        result = self.run_prepare()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(0o600, self.manifest.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.context.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
