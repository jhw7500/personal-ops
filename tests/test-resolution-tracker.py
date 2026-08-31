#!/usr/bin/env python3

import json
import os
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


class TrackerGitFixture:
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.summary = self.root / "summary.md"
        self.state = self.root / "unresolved-items.json"
        self.manifest = self.root / "manifest.json"
        self.context = self.root / "context.txt"

    def git(
        self,
        repo: Path,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def init_repo(self, repo: Path) -> Path:
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Resolution Test")
        self.git(repo, "config", "user.email", "resolution@example.test")
        return repo

    def create_repo(self, name: str = "gstApp") -> Path:
        return self.init_repo(self.root / name)

    def commit(
        self,
        repo: Path,
        message: str,
        content: str,
        when: str,
        relative_path: str = "src/config.c",
    ) -> str:
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.git(repo, "add", relative_path)
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        }
        self.git(repo, "commit", "-q", "-m", message, env=commit_env)
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    def write_open_summary(self, text: str = "bps 배열 길이 불일치") -> None:
        self.summary.write_text(
            "# Claude 세션 요약\n\n"
            "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
            "### 미완료 항목\n"
            f"- [ ] {text} — gstApp — 중\n",
            encoding="utf-8",
        )

    def write_marked_summary(self, marker_id: str) -> None:
        self.summary.write_text(
            "# Claude 세션 요약\n\n"
            "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
            "### 미완료 항목\n"
            "- [ ] bps 배열 길이 불일치 — gstApp — 중\n"
            f"<!-- unresolved-id:{marker_id} -->\n",
            encoding="utf-8",
        )

    def write_state(self, marker_id: str, repo: Path, baseline: str) -> None:
        payload = {
            "schema": 1,
            "items": [
                {
                    "id": marker_id,
                    "text": "bps 배열 길이 불일치",
                    "project": "gstApp",
                    "priority": "중",
                    "opened_on": "2026-05-09",
                    "identity_repo_key": str(repo.resolve()),
                    "repo_path": str(repo.resolve()),
                    "baseline_head": baseline,
                    "status": "open",
                    "resolution": None,
                    "verification": "ready",
                }
            ],
        }
        self.state.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def run_prepare(self, *repos: Path) -> subprocess.CompletedProcess[str]:
        command = [
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
        ]
        for repo in repos:
            command.extend(("--repo", str(repo)))
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


class ResolutionTrackerGitTests(TrackerGitFixture, unittest.TestCase):
    def test_generic_subject_with_matching_patch_is_candidate(self) -> None:
        repo = self.create_repo()
        baseline = self.commit(
            repo,
            "feat: initial camera config",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        fix = self.commit(
            repo,
            "chore: build directory cleanup",
            "void configure(void) { arg.cam[i].bps = json_array_length(node); }\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary()

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        item = manifest["items"][0]
        self.assertEqual(str(repo.resolve()), item["repo_path"])
        self.assertEqual(baseline, item["baseline_head"])
        self.assertEqual("ready", item["verification"])
        self.assertEqual([fix], [candidate["commit"] for candidate in item["candidates"]])
        candidate = item["candidates"][0]
        self.assertEqual(fix[:7], candidate["short"])
        self.assertEqual("chore: build directory cleanup", candidate["subject"])
        self.assertEqual(["bps"], candidate["matched_tokens"])
        self.assertEqual("src/config.c", candidate["path"])
        self.assertIn("arg.cam[i].bps", candidate["excerpt"])

    def test_relevant_subject_without_patch_token_is_not_candidate(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: initial camera config",
            "void configure(void) { arg.cam[i].bps = 4096; }\n"
            "void unrelated(void) { retries = 1; }\n",
            "2026-05-09T12:00:00+0900",
        )
        self.commit(
            repo,
            "fix: resolve bps array length",
            "void configure(void) { arg.cam[i].bps = 4096; }\n"
            "void unrelated(void) { retries = 3; }\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary()

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual([], item["candidates"])
        self.assertEqual("ready", item["verification"])

    def test_project_name_is_not_patch_evidence(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: initial camera config",
            "void configure(void) { arg.cam[i].bps = 4096; }\n"
            "// old product documentation\n",
            "2026-05-09T12:00:00+0900",
        )
        self.commit(
            repo,
            "docs: rename product comment",
            "void configure(void) { arg.cam[i].bps = 4096; }\n"
            "// gstApp project documentation changed\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary("gstApp bps 추적")

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual([], item["candidates"])

    def test_same_basename_repositories_remain_ambiguous(self) -> None:
        first = self.init_repo(self.root / "first" / "gstApp")
        second = self.init_repo(self.root / "second" / "gstApp")
        for repo in (first, second):
            self.commit(
                repo,
                "feat: baseline",
                "void configure(void) { arg.cam[i].bps = 4096; }\n",
                "2026-05-09T12:00:00+0900",
            )
        self.write_open_summary()

        result = self.run_prepare(first, second)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertIsNone(item["repo_path"])
        self.assertIsNone(item["baseline_head"])
        self.assertEqual("repo-ambiguous", item["verification"])
        self.assertEqual([], item["candidates"])

    def test_nested_or_unlisted_repository_is_not_mapped(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        nested = repo / "src"
        self.write_open_summary()

        result = self.run_prepare(nested)

        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual([], manifest["repositories"])
        self.assertEqual("repo-unmapped", manifest["items"][0]["verification"])

    def test_invalid_baseline_is_history_diverged(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        marker_id = "unresolved-444444444444"
        self.write_marked_summary(marker_id)
        self.write_state(marker_id, repo, "f" * 40)

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual("history-diverged", item["verification"])
        self.assertEqual([], item["candidates"])

    def test_more_than_two_hundred_later_commits_discards_history(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-05-11T12:00:00+0900",
            "GIT_COMMITTER_DATE": "2026-05-11T12:00:00+0900",
        }
        for index in range(201):
            self.git(
                repo,
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                f"chore: unrelated {index:03d}",
                env=commit_env,
            )
        self.write_open_summary()

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual([], item["candidates"])
        self.assertEqual("git-unavailable", item["verification"])

    def test_oversized_patch_discards_partial_candidate(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        self.commit(
            repo,
            "chore: large generated config",
            "bps " + ("x" * (1024 * 1024 + 100)) + "\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary()

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual([], item["candidates"])
        self.assertEqual("git-unavailable", item["verification"])

    def test_item_context_over_four_kib_discards_candidate(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        self.commit(
            repo,
            "chore: verbose config line",
            "bps " + ("x" * 5000) + "\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary()

        result = self.run_prepare(repo)

        self.assertEqual(0, result.returncode, result.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual([], item["candidates"])
        self.assertEqual("git-unavailable", item["verification"])
        self.assertLessEqual(len(self.context.read_bytes()), 4096)


class ResolutionTrackerReconcileTests(TrackerGitFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.generated = self.root / "generated.md"
        self.validated = self.root / "validated.md"
        self.next_state = self.root / "next-state.json"

    def run_reconcile(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(TRACKER),
                "reconcile",
                "--generated",
                str(self.generated),
                "--manifest",
                str(self.manifest),
                "--validated",
                str(self.validated),
                "--next-state",
                str(self.next_state),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def prepare_supported_candidate(self) -> tuple[dict, str]:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: initial camera config",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        fix = self.commit(
            repo,
            "chore: build directory cleanup",
            "void configure(void) { arg.cam[i].bps = json_array_length(node); }\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary()
        prepared = self.run_prepare(repo)
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        return item, fix

    def test_authorized_candidate_resolves_once(self) -> None:
        item, fix = self.prepare_supported_candidate()
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 작업 내역\n"
            "- **gstApp**: 설정 검증 수정\n\n"
            "### 미완료 항목\n"
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {fix[:7]}]\n"
            f"<!-- unresolved-id:{item['id']} -->\n\n"
            "### 기술 메모\n"
            "배열 길이는 실제 배열에서 파생한다.\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        resolved_line = (
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {fix[:7]}]"
        )
        self.assertEqual(1, validated.count(resolved_line))
        self.assertEqual(1, validated.count(f"<!-- unresolved-id:{item['id']} -->"))
        state = json.loads(self.next_state.read_text(encoding="utf-8"))
        self.assertEqual(1, len(state["items"]))
        resolved = state["items"][0]
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual(
            {"commit": fix, "resolved_on": "2026-05-12"},
            resolved["resolution"],
        )
        self.assertNotIn("candidates", resolved)

    def test_exact_unresolved_item_stays_open_without_verification_warning(self) -> None:
        item, _ = self.prepare_supported_candidate()
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            "- [ ] bps 배열 길이 불일치 — gstApp — 중\n"
            f"<!-- unresolved-id:{item['id']} -->\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        self.assertIn("- [ ] bps 배열 길이 불일치 — gstApp — 중\n", validated)
        self.assertNotIn("[검증 필요]", validated)
        state = json.loads(self.next_state.read_text(encoding="utf-8"))
        self.assertEqual("open", state["items"][0]["status"])

    def test_missing_incomplete_section_reinserts_prior_item_open(self) -> None:
        item, _ = self.prepare_supported_candidate()
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 작업 내역\n"
            "- **gstApp**: 작업함\n\n"
            "### 기술 메모\n"
            "근거 없음\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        incomplete_index = validated.index("### 미완료 항목")
        memo_index = validated.index("### 기술 메모")
        self.assertLess(incomplete_index, memo_index)
        self.assertIn(
            "- [ ] bps 배열 길이 불일치 — gstApp — 중 [검증 필요]",
            validated,
        )
        self.assertIn(f"<!-- unresolved-id:{item['id']} -->", validated)

    def test_new_open_item_is_mapped_and_assigned_prepared_head(self) -> None:
        repo = self.create_repo()
        head = self.commit(
            repo,
            "feat: baseline",
            "int retries = 3;\n",
            "2026-05-11T12:00:00+0900",
        )
        self.summary.write_text("# Claude 세션 요약\n", encoding="utf-8")
        prepared = self.run_prepare(repo)
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            "- [ ] 재시도 정책 점검 — gstApp — 낮\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(self.next_state.read_text(encoding="utf-8"))
        self.assertEqual(1, len(state["items"]))
        item = state["items"][0]
        self.assertEqual(str(repo.resolve()), item["repo_path"])
        self.assertEqual(head, item["baseline_head"])
        self.assertEqual("ready", item["verification"])
        self.assertRegex(item["id"], r"^unresolved-[0-9a-f]{12}$")
        validated = self.validated.read_text(encoding="utf-8")
        self.assertIn(f"<!-- unresolved-id:{item['id']} -->", validated)

    def test_unknown_resolved_item_with_priority_is_downgraded(self) -> None:
        self.summary.write_text("# Claude 세션 요약\n", encoding="utf-8")
        prepared = self.run_prepare()
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            "- [x] 근거 없는 완료 — unknown — 중 [resolved by deadbee]\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        self.assertIn("- [ ] 근거 없는 완료 — unknown — 중 [검증 필요]", validated)
        self.assertNotIn("[resolved by deadbee]", validated)
        state = json.loads(self.next_state.read_text(encoding="utf-8"))
        self.assertEqual("open", state["items"][0]["status"])

    def test_unparseable_unknown_resolved_item_fails_without_artifacts(self) -> None:
        self.summary.write_text("# Claude 세션 요약\n", encoding="utf-8")
        prepared = self.run_prepare()
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            "- [x] 필드가 부족한 완료 — unknown [resolved by deadbee]\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.validated.exists())
        self.assertFalse(self.next_state.exists())

    def test_changed_number_unit_and_symbol_cannot_resolve(self) -> None:
        repo = self.create_repo()
        self.commit(
            repo,
            "feat: baseline",
            "void configure(void) { arg.cam[i].bps = 4096; }\n",
            "2026-05-09T12:00:00+0900",
        )
        fix = self.commit(
            repo,
            "chore: build directory cleanup",
            "void configure(void) { arg.cam[i].bps = json_array_length(node); }\n",
            "2026-05-11T12:00:00+0900",
        )
        self.write_open_summary("PASS 5/8, 실패 3건, bps 점검")
        prepared = self.run_prepare(repo)
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        item = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            f"- [x] PASS 10/11, 실패 3개, bitrate 점검 — gstApp [resolved by {fix[:7]}]\n"
            f"<!-- unresolved-id:{item['id']} -->\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        self.assertIn(
            "- [ ] PASS 5/8, 실패 3건, bps 점검 — gstApp — 중 [검증 필요]",
            validated,
        )
        self.assertNotIn("10/11", validated)
        self.assertNotIn("3개", validated)
        self.assertNotIn("bitrate", validated)

    def test_resolved_marker_and_state_are_retained_by_next_prepare(self) -> None:
        item, _ = self.prepare_supported_candidate()
        candidate = item["candidates"][0]
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {candidate['short']}]\n"
            f"<!-- unresolved-id:{item['id']} -->\n",
            encoding="utf-8",
        )
        reconciled = self.run_reconcile()
        self.assertEqual(0, reconciled.returncode, reconciled.stderr)
        self.summary.write_bytes(self.validated.read_bytes())
        self.state.write_bytes(self.next_state.read_bytes())

        prepared_again = self.run_prepare(self.root / "gstApp")

        self.assertEqual(0, prepared_again.returncode, prepared_again.stderr)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(1, len(manifest["items"]))
        self.assertEqual("resolved", manifest["items"][0]["status"])
        self.assertEqual(b"", self.context.read_bytes())

    def test_wrong_marker_cannot_resolve_or_duplicate_prior_item(self) -> None:
        item, fix = self.prepare_supported_candidate()
        self.generated.write_text(
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {fix[:7]}]\n"
            "<!-- unresolved-id:unresolved-999999999999 -->\n",
            encoding="utf-8",
        )

        result = self.run_reconcile()

        self.assertEqual(0, result.returncode, result.stderr)
        validated = self.validated.read_text(encoding="utf-8")
        self.assertEqual(1, validated.count("bps 배열 길이 불일치"))
        self.assertIn(
            "- [ ] bps 배열 길이 불일치 — gstApp — 중 [검증 필요]",
            validated,
        )
        self.assertIn(f"<!-- unresolved-id:{item['id']} -->", validated)
        self.assertNotIn("unresolved-999999999999", validated)
        state = json.loads(self.next_state.read_text(encoding="utf-8"))
        self.assertEqual(1, len(state["items"]))
        self.assertEqual("open", state["items"][0]["status"])

    def test_missing_state_recovers_published_resolution_from_git_candidate(self) -> None:
        item, fix = self.prepare_supported_candidate()
        self.summary.write_text(
            "# Claude 세션 요약\n\n"
            "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
            "### 미완료 항목\n"
            "- [ ] bps 배열 길이 불일치 — gstApp — 중\n"
            f"<!-- unresolved-id:{item['id']} -->\n\n"
            "---\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {fix[:7]}]\n"
            f"<!-- unresolved-id:{item['id']} -->\n",
            encoding="utf-8",
        )
        self.state.unlink(missing_ok=True)

        result = self.run_prepare(self.root / "gstApp")

        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(1, len(manifest["items"]))
        recovered = manifest["items"][0]
        self.assertEqual("resolved", recovered["status"])
        self.assertEqual(
            {"commit": fix, "resolved_on": "2026-05-12"},
            recovered["resolution"],
        )
        self.assertEqual(b"", self.context.read_bytes())

    def test_state_resolution_must_match_published_short_sha(self) -> None:
        item, fix = self.prepare_supported_candidate()
        self.summary.write_text(
            "# Claude 세션 요약\n\n"
            "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
            "### 미완료 항목\n"
            "- [ ] bps 배열 길이 불일치 — gstApp — 중\n"
            f"<!-- unresolved-id:{item['id']} -->\n\n"
            "## 2026-05-12 (2026-05-11 ~ 2026-05-12)\n\n"
            "### 미완료 항목\n"
            f"- [x] bps 배열 길이 불일치 — gstApp [resolved by {fix[:7]}]\n"
            f"<!-- unresolved-id:{item['id']} -->\n",
            encoding="utf-8",
        )
        forged = {key: value for key, value in item.items() if key != "candidates"}
        forged["status"] = "resolved"
        forged["resolution"] = {
            "commit": "f" * 40,
            "resolved_on": "2026-05-12",
        }
        self.state.write_text(
            json.dumps({"schema": 1, "items": [forged]}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        result = self.run_prepare(self.root / "gstApp")

        self.assertEqual(0, result.returncode, result.stderr)
        recovered = json.loads(self.manifest.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(fix, recovered["resolution"]["commit"])
        self.assertNotEqual("f" * 40, recovered["resolution"]["commit"])


if __name__ == "__main__":
    unittest.main()
