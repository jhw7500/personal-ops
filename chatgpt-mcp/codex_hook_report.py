#!/usr/bin/env python3
"""Report Codex hook metadata locally without changing a Codex decision."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from codex_hooks import normalize_hook
from codex_observer import _load_projects
from project_state import ProjectState


MAX_INPUT_BYTES = 256 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record metadata from a trusted Codex hook")
    parser.add_argument("--projects", type=Path,
                        default=Path.home() / ".config/chatgpt-coding-mcp/projects.json")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get(
        "XDG_STATE_HOME", str(Path.home() / ".local/state")
    )) / "chatgpt-coding-mcp")
    args = parser.parse_args(argv)
    phase = "input"
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) <= MAX_INPUT_BYTES:
            payload = json.loads(raw)
            phase = "configuration"
            projects = _load_projects(args.projects)
            phase = "normalization"
            events = normalize_hook(payload, projects, time=datetime.now(timezone.utc).isoformat())
            if events:
                phase = "storage"
                store = ProjectState(projects, args.state_dir)
                for project, event in events:
                    if "model" in event:
                        event["model_source"] = "event"
                    store.record_work_event(project, event, provenance="observed")
    except Exception:
        # Hooks stay nonblocking, but operational failures must remain visible.
        # Phase names are fixed constants; exception text and input stay private.
        if phase != "input":
            print(f"codex-hook {phase} unavailable", file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
