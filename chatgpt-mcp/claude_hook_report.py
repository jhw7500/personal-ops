#!/usr/bin/env python3
"""Report Claude hook metadata locally without changing a Claude decision."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

from claude_hooks import normalize_hook
from codex_observer import _load_projects
from project_state import ProjectState


MAX_INPUT_BYTES = 256 * 1024
DEADLINE_SECONDS = 1.0


def _deadline_expired(_signum, _frame):
    raise TimeoutError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record metadata from a trusted Claude hook")
    parser.add_argument("--projects", type=Path,
                        default=Path.home() / ".config/chatgpt-coding-mcp/projects.json")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get(
        "XDG_STATE_HOME", str(Path.home() / ".local/state")
    )) / "chatgpt-coding-mcp")
    args = parser.parse_args(argv)

    phase = "input"
    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
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
                    if "reasoning_effort" in event:
                        event["reasoning_effort_source"] = "event"
                    store.record_work_event(project, event, provenance="observed")
    except Exception:
        if phase != "input":
            print(f"claude-hook {phase} unavailable", file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
