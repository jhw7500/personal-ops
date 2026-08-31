#!/usr/bin/env python3
"""Track and validate resolution links for session-summary open items."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence


SCHEMA = 1
OPEN_LIMIT = 100
RESOLVED_LIMIT = 200
GIT_TIMEOUT_SECONDS = 5
GIT_OUTPUT_LIMIT = 1024 * 1024
ITEM_CONTEXT_LIMIT = 4 * 1024
TOTAL_CONTEXT_LIMIT = 32 * 1024
STATE_FIELDS = {
    "id",
    "text",
    "project",
    "priority",
    "opened_on",
    "identity_repo_key",
    "repo_path",
    "baseline_head",
    "status",
    "resolution",
    "verification",
}
VERIFICATIONS = {
    "ready",
    "repo-unmapped",
    "repo-ambiguous",
    "git-unavailable",
    "history-diverged",
}
ID_RE = re.compile(r"^unresolved-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_HEADING_RE = re.compile(r"^## (?P<date>\d{4}-\d{2}-\d{2})(?:\s|$)")
OPEN_RE = re.compile(
    r"^- \[ \] (?P<text>.+?) — (?P<project>[^—]+?) — "
    r"(?P<priority>높|중|낮)(?: \[검증 필요\])?$"
)
MARKER_RE = re.compile(
    r"^<!-- unresolved-id:(?P<id>unresolved-[0-9a-f]{12}) -->$"
)
RESOLVED_RE = re.compile(
    r"^- \[x\] (?P<text>.+?) — (?P<project>[^—]+?) "
    r"\[resolved by (?P<sha>[0-9a-f]{7,40})\]$"
)
NEW_RESOLVED_RE = re.compile(
    r"^- \[x\] (?P<text>.+?) — (?P<project>[^—]+?) — "
    r"(?P<priority>높|중|낮) \[resolved by (?P<sha>[0-9a-f]{7,40})\]$"
)


class TrackerError(RuntimeError):
    """An input or resource failure that must not authorize resolution."""


@dataclass(frozen=True)
class SummaryItem:
    text: str
    project: str
    priority: str | None
    opened_on: str
    marker_id: str | None
    status: str
    resolution_prefix: str | None
    resolved_on: str | None


@dataclass(frozen=True)
class Repository:
    path: Path
    name: str
    head: str


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    return " ".join(normalized.split())


def make_item_id(
    opened_on: str,
    project: str,
    normalized_text: str,
    identity_repo_key: str,
) -> str:
    identity = "\0".join(
        (opened_on, project, normalized_text, identity_repo_key)
    ).encode("utf-8")
    return f"unresolved-{hashlib.sha256(identity).hexdigest()[:12]}"


def parse_summary(markdown: str) -> list[SummaryItem]:
    lines = markdown.splitlines()
    current_date: str | None = None
    in_incomplete = False
    items: list[SummaryItem] = []

    for index, line in enumerate(lines):
        date_match = DATE_HEADING_RE.match(line)
        if date_match:
            current_date = date_match.group("date")
            in_incomplete = False
            continue
        if line == "### 미완료 항목":
            in_incomplete = current_date is not None
            continue
        if line.startswith("### "):
            in_incomplete = False
            continue
        if not in_incomplete or current_date is None:
            continue

        open_match = OPEN_RE.match(line)
        resolved_match = RESOLVED_RE.match(line)
        if not open_match and not resolved_match:
            continue
        marker_id = None
        if index + 1 < len(lines):
            marker_match = MARKER_RE.match(lines[index + 1])
            if marker_match:
                marker_id = marker_match.group("id")
        if open_match:
            items.append(
                SummaryItem(
                    text=normalize_text(open_match.group("text")),
                    project=normalize_text(open_match.group("project")),
                    priority=open_match.group("priority"),
                    opened_on=current_date,
                    marker_id=marker_id,
                    status="open",
                    resolution_prefix=None,
                    resolved_on=None,
                )
            )
        elif marker_id is not None and resolved_match:
            items.append(
                SummaryItem(
                    text=normalize_text(resolved_match.group("text")),
                    project=normalize_text(resolved_match.group("project")),
                    priority=None,
                    opened_on=current_date,
                    marker_id=marker_id,
                    status="resolved",
                    resolution_prefix=resolved_match.group("sha"),
                    resolved_on=current_date,
                )
            )
    return items


def _valid_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_resolution(value: Any, status: str) -> bool:
    if status == "open":
        return value is None
    if not isinstance(value, dict) or set(value) != {"commit", "resolved_on"}:
        return False
    return bool(SHA_RE.fullmatch(value["commit"])) and _valid_iso_date(
        value["resolved_on"]
    )


def _valid_state_item(item: Any) -> bool:
    if not isinstance(item, dict) or set(item) != STATE_FIELDS:
        return False
    status = item.get("status")
    repo_path = item.get("repo_path")
    baseline = item.get("baseline_head")
    identity_key = item.get("identity_repo_key")
    return all(
        (
            bool(ID_RE.fullmatch(item.get("id", ""))),
            isinstance(item.get("text"), str) and bool(item["text"]),
            isinstance(item.get("project"), str) and bool(item["project"]),
            item.get("priority") in {"높", "중", "낮"},
            _valid_iso_date(item.get("opened_on")),
            isinstance(identity_key, str) and bool(identity_key),
            repo_path is None
            or (isinstance(repo_path, str) and Path(repo_path).is_absolute()),
            baseline is None
            or (isinstance(baseline, str) and bool(SHA_RE.fullmatch(baseline))),
            status in {"open", "resolved"},
            _valid_resolution(item.get("resolution"), status),
            item.get("verification") in VERIFICATIONS,
        )
    )


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or set(payload) != {"schema", "items"}:
        return {}
    if payload["schema"] != SCHEMA or not isinstance(payload["items"], list):
        return {}
    if not all(_valid_state_item(item) for item in payload["items"]):
        return {}
    ids = [item["id"] for item in payload["items"]]
    if len(ids) != len(set(ids)):
        return {}
    return {item["id"]: item for item in payload["items"]}


def _state_matches_summary(item: dict[str, Any], summary: SummaryItem) -> bool:
    common_match = all(
        (
            item["id"] == summary.marker_id,
            item["text"] == summary.text,
            item["project"] == summary.project,
        )
    )
    if summary.status == "resolved":
        resolution = item.get("resolution")
        return bool(
            common_match
            and item["status"] == "resolved"
            and isinstance(resolution, dict)
            and isinstance(summary.resolution_prefix, str)
            and resolution["commit"].startswith(summary.resolution_prefix)
            and resolution["resolved_on"] == summary.resolved_on
        )
    return (
        common_match
        and item["priority"] == summary.priority
    )


def _collapse_marked_history(summary_items: list[SummaryItem]) -> list[SummaryItem]:
    collapsed: list[SummaryItem] = []
    positions: dict[str, int] = {}
    for summary in summary_items:
        marker_id = summary.marker_id
        if marker_id is None or marker_id not in positions:
            if marker_id is not None:
                positions[marker_id] = len(collapsed)
            collapsed.append(summary)
            continue
        position = positions[marker_id]
        previous = collapsed[position]
        priority = previous.priority if summary.priority is None else summary.priority
        opened_on = (
            previous.opened_on if previous.priority is not None else summary.opened_on
        )
        collapsed[position] = SummaryItem(
            text=summary.text,
            project=summary.project,
            priority=priority,
            opened_on=opened_on,
            marker_id=marker_id,
            status=summary.status,
            resolution_prefix=summary.resolution_prefix,
            resolved_on=summary.resolved_on,
        )
    return collapsed


def run_git(
    repo: Path,
    args: Sequence[str],
    timeout: int = GIT_TIMEOUT_SECONDS,
    max_output: int = GIT_OUTPUT_LIMIT,
) -> str:
    environment = {
        **os.environ,
        "GIT_PAGER": "cat",
        "LC_ALL": "C.UTF-8",
    }
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise TrackerError(f"git command failed for {repo}: {error}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {process.stdout: [], process.stderr: []}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    output_size = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrackerError(f"git command timed out for {repo}")
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is None:
                continue
            for key, _ in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                output_size += len(chunk)
                if output_size > max_output:
                    raise TrackerError(f"git output limit exceeded for {repo}")
                streams[stream].append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrackerError(f"git command timed out for {repo}")
        process.wait(timeout=remaining)
    except (TrackerError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        raise TrackerError(
            f"git command timed out for {repo}"
            if time.monotonic() >= deadline
            else f"git output limit exceeded for {repo}"
        )
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
    stdout_bytes = b"".join(streams[process.stdout])
    stderr_bytes = b"".join(streams[process.stderr])
    try:
        stdout = stdout_bytes.decode("utf-8", errors="strict")
        stderr = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TrackerError(f"git output is not valid UTF-8 for {repo}") from error
    if process.returncode != 0:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
        raise TrackerError(f"git exited {process.returncode} for {repo}: {detail}")
    return stdout


def validate_repositories(paths: Sequence[str]) -> list[Repository]:
    repositories: list[Repository] = []
    seen: set[Path] = set()
    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute() or not candidate.is_dir():
            continue
        normalized = candidate.resolve()
        try:
            top_level_text = run_git(
                normalized, ("rev-parse", "--show-toplevel")
            ).strip()
            top_level = Path(top_level_text).resolve()
            head = run_git(normalized, ("rev-parse", "HEAD")).strip()
        except (TrackerError, OSError):
            continue
        if top_level != normalized or not SHA_RE.fullmatch(head) or top_level in seen:
            continue
        seen.add(top_level)
        repositories.append(
            Repository(path=top_level, name=top_level.name, head=head)
        )
    return repositories


def _dated_baseline(repo: Repository, opened_on: str) -> str | None:
    output = run_git(
        repo.path,
        (
            "log",
            "--all",
            "--no-merges",
            f"--until={opened_on}T23:59:59",
            "--format=%H",
            "-n",
            "1",
        ),
    ).strip()
    return output if SHA_RE.fullmatch(output) else None


def recover_items(
    summary_items: list[SummaryItem],
    state_by_id: dict[str, dict[str, Any]],
    repositories: list[Repository],
) -> list[dict[str, Any]]:
    summary_items = _collapse_marked_history(summary_items)
    occurrences: dict[tuple[str, str, str], int] = {}
    recovered: list[dict[str, Any]] = []
    recovered_positions: dict[str, int] = {}
    for summary in summary_items:
        occurrence_key = (summary.opened_on, summary.project, summary.text)
        occurrence = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = occurrence
        fallback_identity = f"unmapped:{occurrence}"
        stored = state_by_id.get(summary.marker_id or "")
        if stored is not None and _state_matches_summary(stored, summary):
            item = dict(stored)
        else:
            if summary.priority is None:
                continue
            matches = [repo for repo in repositories if repo.name == summary.project]
            mapped_repo = matches[0] if len(matches) == 1 else None
            identity_repo_key = (
                str(mapped_repo.path) if mapped_repo is not None else fallback_identity
            )
            item_id = summary.marker_id or make_item_id(
                summary.opened_on,
                summary.project,
                summary.text,
                identity_repo_key,
            )
            item = {
                "id": item_id,
                "text": summary.text,
                "project": summary.project,
                "priority": summary.priority,
                "opened_on": summary.opened_on,
                "identity_repo_key": identity_repo_key,
                "repo_path": str(mapped_repo.path) if mapped_repo is not None else None,
                "baseline_head": None,
                "status": "open",
                "resolution": None,
                "verification": (
                    "ready"
                    if mapped_repo is not None
                    else "repo-ambiguous"
                    if len(matches) > 1
                    else "repo-unmapped"
                ),
            }
            if mapped_repo is not None:
                try:
                    item["baseline_head"] = _dated_baseline(
                        mapped_repo, summary.opened_on
                    )
                except TrackerError:
                    item["verification"] = "git-unavailable"
            if summary.status == "resolved":
                item["_published_resolution_prefix"] = summary.resolution_prefix
                item["_published_resolved_on"] = summary.resolved_on
        if item["repo_path"] is None:
            matches = [repo for repo in repositories if repo.name == item["project"]]
            if len(matches) == 1:
                mapped_repo = matches[0]
                item["repo_path"] = str(mapped_repo.path)
                try:
                    item["baseline_head"] = _dated_baseline(
                        mapped_repo, item["opened_on"]
                    )
                    item["verification"] = "ready"
                except TrackerError:
                    item["verification"] = "git-unavailable"
            elif len(matches) > 1:
                item["verification"] = "repo-ambiguous"
        item["candidates"] = []
        prior_position = recovered_positions.get(item["id"])
        if prior_position is not None:
            prior = recovered[prior_position]
            if any(
                prior[field] != item[field]
                for field in ("text", "project", "priority")
            ):
                raise TrackerError(f"conflicting summary item id: {item['id']}")
            if prior["status"] != "resolved" and item["status"] == "resolved":
                recovered[prior_position] = item
            else:
                for field in (
                    "_published_resolution_prefix",
                    "_published_resolved_on",
                ):
                    if field in item:
                        prior[field] = item[field]
            continue
        recovered_positions[item["id"]] = len(recovered)
        recovered.append(item)
    return recovered


def extract_evidence_tokens(text: str, project: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.:\[\]]{2,}|\d{2,}", text)
    excluded = {project.casefold(), "todo", "fixme", "pass", "fail"}
    return sorted(
        {
            token
            for token in tokens
            if token.casefold() not in excluded
            and (not token.isdigit() or len(token) >= 2)
        }
    )


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"
    )


def _patch_candidate(
    repo: Repository, commit: str, tokens: list[str]
) -> dict[str, Any] | None:
    subject = run_git(repo.path, ("show", "-s", "--format=%s", commit)).strip()
    patch = run_git(
        repo.path,
        (
            "show",
            "--format=",
            "--no-ext-diff",
            "--no-color",
            "--no-textconv",
            "--unified=2",
            commit,
            "--",
        ),
    )
    lines = patch.splitlines()
    current_path = ""
    for index, line in enumerate(lines):
        if line.startswith("--- a/"):
            current_path = line[6:]
            continue
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        matched = [token for token in tokens if _token_pattern(token).search(line[1:])]
        if not matched:
            continue
        excerpt = "\n".join(lines[max(0, index - 2) : min(len(lines), index + 3)])
        return {
            "commit": commit,
            "short": commit[:7],
            "subject": subject,
            "matched_tokens": matched,
            "path": current_path,
            "excerpt": excerpt,
        }
    return None


def attach_candidates(
    items: list[dict[str, Any]], repositories: list[Repository]
) -> None:
    repositories_by_path = {str(repo.path): repo for repo in repositories}
    for item in items:
        if item["status"] != "open" or item["verification"] != "ready":
            continue
        repo = repositories_by_path.get(item["repo_path"])
        baseline = item["baseline_head"]
        if repo is None or not isinstance(baseline, str):
            item["verification"] = "git-unavailable"
            continue
        try:
            run_git(repo.path, ("cat-file", "-e", f"{baseline}^{{commit}}"))
            run_git(repo.path, ("merge-base", "--is-ancestor", baseline, repo.head))
        except TrackerError:
            item["candidates"] = []
            item["verification"] = "history-diverged"
            continue
        try:
            revisions = run_git(
                repo.path,
                (
                    "rev-list",
                    "--reverse",
                    "--no-merges",
                    "--max-count=201",
                    f"{baseline}..{repo.head}",
                ),
            ).splitlines()
            if len(revisions) > 200:
                raise TrackerError("commit history limit exceeded")
            tokens = extract_evidence_tokens(item["text"], item["project"])
            candidates = []
            for commit in revisions:
                if not SHA_RE.fullmatch(commit):
                    raise TrackerError("malformed commit id")
                candidate = _patch_candidate(repo, commit, tokens)
                if candidate is not None:
                    candidates.append(candidate)
                if len(candidates) == 5:
                    break
            item["candidates"] = candidates
        except TrackerError:
            item["candidates"] = []
            item["verification"] = "git-unavailable"


def recover_published_resolutions(items: list[dict[str, Any]]) -> None:
    for item in items:
        prefix = item.pop("_published_resolution_prefix", None)
        resolved_on = item.pop("_published_resolved_on", None)
        if (
            item["status"] != "open"
            or not isinstance(prefix, str)
            or len(prefix) < 7
            or not _valid_iso_date(resolved_on)
        ):
            continue
        matches = [
            candidate
            for candidate in item["candidates"]
            if candidate["commit"].startswith(prefix)
        ]
        if len(matches) != 1:
            continue
        item["status"] = "resolved"
        item["resolution"] = {
            "commit": matches[0]["commit"],
            "resolved_on": resolved_on,
        }
        item["candidates"] = []


def _render_item_card(item: dict[str, Any]) -> str:
    lines = [
        f"ITEM {item['id']}",
        "ORIGINAL "
        f"- [ ] {item['text']} — {item['project']} — {item['priority']}",
        f"STATUS {item['verification']}",
    ]
    if item["candidates"]:
        for candidate in item["candidates"]:
            lines.extend(
                (
                    "CANDIDATE "
                    f"{candidate['short']} {candidate['subject']} "
                    f"tokens={','.join(candidate['matched_tokens'])} "
                    f"path={candidate['path']}",
                    candidate["excerpt"],
                )
            )
    else:
        lines.append("CANDIDATE (none)")
    return "\n".join(lines)


def apply_context_limits(items: list[dict[str, Any]]) -> None:
    total_bytes = 0
    for item in items:
        if item["status"] != "open":
            continue
        card = _render_item_card(item)
        card_bytes = len(card.encode("utf-8"))
        if card_bytes > ITEM_CONTEXT_LIMIT and item["candidates"]:
            item["candidates"] = []
            item["verification"] = "git-unavailable"
            card = _render_item_card(item)
            card_bytes = len(card.encode("utf-8"))
        if card_bytes > ITEM_CONTEXT_LIMIT:
            raise TrackerError(f"item context limit exceeded for {item['id']}")
        separator_bytes = 2 if total_bytes else 0
        if total_bytes + separator_bytes + card_bytes + 1 > TOTAL_CONTEXT_LIMIT:
            if item["candidates"]:
                item["candidates"] = []
                item["verification"] = "git-unavailable"
                card = _render_item_card(item)
                card_bytes = len(card.encode("utf-8"))
            if total_bytes + separator_bytes + card_bytes + 1 > TOTAL_CONTEXT_LIMIT:
                raise TrackerError("total resolution context limit exceeded")
        total_bytes += separator_bytes + card_bytes


def render_context(items: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in items:
        if item["status"] != "open":
            continue
        cards.append(_render_item_card(item))
    return "\n\n".join(cards) + ("\n" if cards else "")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def prepare(args: argparse.Namespace) -> None:
    summary_path = Path(args.summary)
    try:
        markdown = (
            summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
        )
        if args.prior_summary is not None:
            prior_summary_path = Path(args.prior_summary)
            if not prior_summary_path.is_file():
                raise TrackerError("cannot read prior summary: not a file")
            prior_markdown = prior_summary_path.read_text(encoding="utf-8")
            markdown = f"{prior_markdown.rstrip()}\n\n{markdown.lstrip()}"
    except (OSError, UnicodeError) as error:
        raise TrackerError(f"cannot read summary: {error}") from error

    repositories = validate_repositories(args.repo)
    summary_items = parse_summary(markdown)
    items = recover_items(
        summary_items, load_state(Path(args.state)), repositories
    )
    attach_candidates(items, repositories)
    recover_published_resolutions(items)
    apply_context_limits(items)
    open_count = sum(item["status"] == "open" for item in items)
    if open_count > OPEN_LIMIT:
        raise TrackerError(
            f"open item limit exceeded: {open_count} > {OPEN_LIMIT}"
        )

    manifest = {
        "schema": SCHEMA,
        "prepared_on": date.today().isoformat(),
        "repositories": [
            {"path": str(repo.path), "name": repo.name, "head": repo.head}
            for repo in repositories
        ],
        "items": items,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    context_bytes = render_context(items).encode("utf-8")
    _atomic_write(Path(args.manifest), manifest_bytes)
    _atomic_write(Path(args.context), context_bytes)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TrackerError(f"cannot read manifest: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise TrackerError("unsupported manifest schema")
    if not isinstance(payload.get("items"), list) or not isinstance(
        payload.get("repositories"), list
    ):
        raise TrackerError("malformed manifest")
    for item in payload["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("candidates"), list):
            raise TrackerError("malformed manifest item")
        state_item = {field: item.get(field) for field in STATE_FIELDS}
        if not _valid_state_item(state_item):
            raise TrackerError("malformed manifest state item")
        for candidate in item["candidates"]:
            if (
                not isinstance(candidate, dict)
                or set(candidate)
                != {
                    "commit",
                    "short",
                    "subject",
                    "matched_tokens",
                    "path",
                    "excerpt",
                }
                or not SHA_RE.fullmatch(candidate.get("commit", ""))
                or candidate.get("short") != candidate["commit"][:7]
                or not isinstance(candidate.get("subject"), str)
                or not isinstance(candidate.get("matched_tokens"), list)
                or not all(
                    isinstance(token, str) and token
                    for token in candidate["matched_tokens"]
                )
                or not isinstance(candidate.get("path"), str)
                or not isinstance(candidate.get("excerpt"), str)
            ):
                raise TrackerError("malformed manifest candidate")
    return payload


def _daily_date(lines: list[str]) -> str:
    dates = [match.group("date") for line in lines if (match := DATE_HEADING_RE.match(line))]
    if len(dates) != 1 or not _valid_iso_date(dates[0]):
        raise TrackerError("generated markdown must contain one daily date heading")
    return dates[0]


def _incomplete_section(lines: list[str]) -> tuple[int, int, bool]:
    headings = [index for index, line in enumerate(lines) if line == "### 미완료 항목"]
    if len(headings) > 1:
        raise TrackerError("generated markdown contains duplicate incomplete sections")
    if not headings:
        insertion = next(
            (
                index
                for index, line in enumerate(lines)
                if line == "### 기술 메모"
            ),
            len(lines),
        )
        return insertion, insertion, False
    start = headings[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("### "):
            end = index
            break
    return start, end, True


def _generated_entries(
    lines: list[str], start: int, end: int, section_exists: bool
) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    index = start + 1 if section_exists else start
    while index < end:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if not line.startswith("- ["):
            raise TrackerError("unsafe content in generated incomplete section")
        marker_id = None
        if index + 1 < end:
            marker_match = MARKER_RE.match(lines[index + 1])
            if marker_match:
                marker_id = marker_match.group("id")
                index += 1
        entries.append((line, marker_id))
        index += 1
    return entries


def _authorized_candidate(
    item: dict[str, Any], prefix: str
) -> dict[str, Any] | None:
    if len(prefix) < 7:
        return None
    matches = [
        candidate
        for candidate in item["candidates"]
        if candidate["commit"].startswith(prefix)
    ]
    return matches[0] if len(matches) == 1 else None


def _state_item(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item[field] for field in STATE_FIELDS}


def _manifest_repositories(manifest: dict[str, Any]) -> list[Repository]:
    repositories: list[Repository] = []
    for record in manifest["repositories"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "name", "head"}
            or not isinstance(record["path"], str)
            or not Path(record["path"]).is_absolute()
            or not isinstance(record["name"], str)
            or not record["name"]
            or not SHA_RE.fullmatch(record.get("head", ""))
        ):
            raise TrackerError("malformed manifest repository")
        repositories.append(
            Repository(
                path=Path(record["path"]),
                name=record["name"],
                head=record["head"],
            )
        )
    return repositories


def _new_open_item(
    match: re.Match[str],
    opened_on: str,
    repositories: list[Repository],
    occurrence: int,
) -> dict[str, Any]:
    text = normalize_text(match.group("text"))
    project = normalize_text(match.group("project"))
    priority = match.group("priority")
    matches = [repo for repo in repositories if repo.name == project]
    mapped_repo = matches[0] if len(matches) == 1 else None
    identity_repo_key = (
        str(mapped_repo.path) if mapped_repo is not None else f"unmapped:{occurrence}"
    )
    return {
        "id": make_item_id(opened_on, project, text, identity_repo_key),
        "text": text,
        "project": project,
        "priority": priority,
        "opened_on": opened_on,
        "identity_repo_key": identity_repo_key,
        "repo_path": str(mapped_repo.path) if mapped_repo is not None else None,
        "baseline_head": mapped_repo.head if mapped_repo is not None else None,
        "status": "open",
        "resolution": None,
        "verification": (
            "ready"
            if mapped_repo is not None
            else "repo-ambiguous"
            if len(matches) > 1
            else "repo-unmapped"
        ),
    }


def reconcile(args: argparse.Namespace) -> None:
    manifest = load_manifest(Path(args.manifest))
    try:
        generated = Path(args.generated).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise TrackerError(f"cannot read generated markdown: {error}") from error
    lines = generated.splitlines()
    resolved_on = _daily_date(lines)
    section_start, section_end, section_exists = _incomplete_section(lines)
    generated_entries = _generated_entries(
        lines, section_start, section_end, section_exists
    )
    manifest_ids = {item["id"] for item in manifest["items"]}
    generated_by_id: dict[str, list[str]] = {}
    unknown_entries: list[str] = []
    for line, marker_id in generated_entries:
        if marker_id in manifest_ids:
            generated_by_id.setdefault(marker_id, []).append(line)
        else:
            unknown_entries.append(line)

    canonical_lines: list[str] = []
    open_items: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    for manifest_item in manifest["items"]:
        item = dict(manifest_item)
        if item["status"] == "resolved":
            resolved_items.append(_state_item(item))
            continue
        generated_lines = generated_by_id.get(item["id"], [])
        candidate = None
        exact_open = False
        if len(generated_lines) == 1:
            open_match = OPEN_RE.match(generated_lines[0])
            exact_open = bool(
                open_match
                and normalize_text(open_match.group("text")) == item["text"]
                and normalize_text(open_match.group("project")) == item["project"]
                and open_match.group("priority") == item["priority"]
            )
            resolved_match = RESOLVED_RE.match(generated_lines[0])
            if (
                resolved_match
                and normalize_text(resolved_match.group("text")) == item["text"]
                and normalize_text(resolved_match.group("project")) == item["project"]
            ):
                candidate = _authorized_candidate(item, resolved_match.group("sha"))
        if candidate is not None:
            item["status"] = "resolved"
            item["resolution"] = {
                "commit": candidate["commit"],
                "resolved_on": resolved_on,
            }
            canonical_lines.extend(
                (
                    f"- [x] {item['text']} — {item['project']} "
                    f"[resolved by {candidate['short']}]",
                    f"<!-- unresolved-id:{item['id']} -->",
                )
            )
            resolved_items.append(_state_item(item))
        else:
            verification_suffix = "" if exact_open else " [검증 필요]"
            canonical_lines.extend(
                (
                    f"- [ ] {item['text']} — {item['project']} — "
                    f"{item['priority']}{verification_suffix}",
                    f"<!-- unresolved-id:{item['id']} -->",
                )
            )
            open_items.append(_state_item(item))

    repositories = _manifest_repositories(manifest)
    occurrence_counts: dict[tuple[str, str, str], int] = {}
    known_state_ids = {item["id"] for item in open_items + resolved_items}
    for line in unknown_entries:
        open_match = OPEN_RE.match(line)
        downgraded_match = NEW_RESOLVED_RE.match(line)
        if open_match is None and downgraded_match is None:
            misplaced_resolved = RESOLVED_RE.match(line)
            if misplaced_resolved:
                matching_prior = [
                    item
                    for item in manifest["items"]
                    if item["status"] == "open"
                    and item["text"]
                    == normalize_text(misplaced_resolved.group("text"))
                    and item["project"]
                    == normalize_text(misplaced_resolved.group("project"))
                ]
                if len(matching_prior) == 1:
                    continue
            raise TrackerError("unsupported new resolved or malformed open item")
        match = open_match or downgraded_match
        assert match is not None
        identity = (
            resolved_on,
            normalize_text(match.group("project")),
            normalize_text(match.group("text")),
        )
        occurrence = occurrence_counts.get(identity, 0) + 1
        occurrence_counts[identity] = occurrence
        item = _new_open_item(match, resolved_on, repositories, occurrence)
        if item["id"] in known_state_ids:
            continue
        known_state_ids.add(item["id"])
        verification_suffix = " [검증 필요]" if downgraded_match else ""
        canonical_lines.extend(
            (
                f"- [ ] {item['text']} — {item['project']} — "
                f"{item['priority']}{verification_suffix}",
                f"<!-- unresolved-id:{item['id']} -->",
            )
        )
        open_items.append(item)

    if len(open_items) > OPEN_LIMIT:
        raise TrackerError(
            f"open item limit exceeded: {len(open_items)} > {OPEN_LIMIT}"
        )
    resolved_items.sort(
        key=lambda item: (
            item["resolution"]["resolved_on"] if item["resolution"] else "",
            item["id"],
        ),
        reverse=True,
    )
    next_items = open_items + resolved_items[:RESOLVED_LIMIT]

    replacement = ["### 미완료 항목", *canonical_lines]
    if section_end < len(lines):
        replacement.append("")
    validated_lines = lines[:section_start] + replacement + lines[section_end:]
    validated = "\n".join(validated_lines).rstrip() + "\n"
    next_state = {"schema": SCHEMA, "items": next_items}
    state_bytes = (
        json.dumps(next_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(Path(args.validated), validated.encode("utf-8"))
    _atomic_write(Path(args.next_state), state_bytes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--summary", required=True)
    prepare_parser.add_argument("--prior-summary")
    prepare_parser.add_argument("--state", required=True)
    prepare_parser.add_argument("--manifest", required=True)
    prepare_parser.add_argument("--context", required=True)
    prepare_parser.add_argument("--repo", action="append", default=[])
    prepare_parser.set_defaults(handler=prepare)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--generated", required=True)
    reconcile_parser.add_argument("--manifest", required=True)
    reconcile_parser.add_argument("--validated", required=True)
    reconcile_parser.add_argument("--next-state", required=True)
    reconcile_parser.set_defaults(handler=reconcile)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except TrackerError as error:
        print(f"resolution tracker: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError) as error:
        print(f"resolution tracker: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
