#!/usr/bin/env python3
"""Track and validate resolution links for session-summary open items."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence


SCHEMA = 1
OPEN_LIMIT = 100
RESOLVED_LIMIT = 200
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


class TrackerError(RuntimeError):
    """An input or resource failure that must not authorize resolution."""


@dataclass(frozen=True)
class SummaryItem:
    text: str
    project: str
    priority: str
    opened_on: str
    marker_id: str | None


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
        if not open_match:
            continue
        marker_id = None
        if index + 1 < len(lines):
            marker_match = MARKER_RE.match(lines[index + 1])
            if marker_match:
                marker_id = marker_match.group("id")
        items.append(
            SummaryItem(
                text=normalize_text(open_match.group("text")),
                project=normalize_text(open_match.group("project")),
                priority=open_match.group("priority"),
                opened_on=current_date,
                marker_id=marker_id,
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
    return all(
        (
            item["id"] == summary.marker_id,
            item["text"] == summary.text,
            item["project"] == summary.project,
            item["priority"] == summary.priority,
            item["opened_on"] == summary.opened_on,
        )
    )


def recover_items(
    summary_items: list[SummaryItem], state_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    occurrences: dict[tuple[str, str, str], int] = {}
    recovered: list[dict[str, Any]] = []
    for summary in summary_items:
        occurrence_key = (summary.opened_on, summary.project, summary.text)
        occurrence = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = occurrence
        fallback_identity = f"unmapped:{occurrence}"
        stored = state_by_id.get(summary.marker_id or "")
        if stored is not None and _state_matches_summary(stored, summary):
            item = dict(stored)
        else:
            item_id = summary.marker_id or make_item_id(
                summary.opened_on,
                summary.project,
                summary.text,
                fallback_identity,
            )
            item = {
                "id": item_id,
                "text": summary.text,
                "project": summary.project,
                "priority": summary.priority,
                "opened_on": summary.opened_on,
                "identity_repo_key": fallback_identity,
                "repo_path": None,
                "baseline_head": None,
                "status": "open",
                "resolution": None,
                "verification": "repo-unmapped",
            }
        item["candidates"] = []
        recovered.append(item)
    return recovered


def render_context(items: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in items:
        if item["status"] != "open":
            continue
        cards.append(
            "\n".join(
                (
                    f"ITEM {item['id']}",
                    "ORIGINAL "
                    f"- [ ] {item['text']} — {item['project']} — {item['priority']}",
                    f"STATUS {item['verification']}",
                    "CANDIDATE (none)",
                )
            )
        )
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
    except (OSError, UnicodeError) as error:
        raise TrackerError(f"cannot read summary: {error}") from error

    summary_items = parse_summary(markdown)
    items = recover_items(summary_items, load_state(Path(args.state)))
    open_count = sum(item["status"] == "open" for item in items)
    if open_count > OPEN_LIMIT:
        raise TrackerError(
            f"open item limit exceeded: {open_count} > {OPEN_LIMIT}"
        )

    manifest = {
        "schema": SCHEMA,
        "prepared_on": date.today().isoformat(),
        "repositories": [],
        "items": items,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    context_bytes = render_context(items).encode("utf-8")
    _atomic_write(Path(args.manifest), manifest_bytes)
    _atomic_write(Path(args.context), context_bytes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--summary", required=True)
    prepare_parser.add_argument("--state", required=True)
    prepare_parser.add_argument("--manifest", required=True)
    prepare_parser.add_argument("--context", required=True)
    prepare_parser.add_argument("--repo", action="append", default=[])
    prepare_parser.set_defaults(handler=prepare)
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
