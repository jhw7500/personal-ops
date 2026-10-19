#!/usr/bin/env python3
"""Local TTY-only preparation and application of a pinned review-only profile."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import sys

from agent_broker_server import _load_projects
from agent_profile_setup import AgentProfileSetup
from host_runtime_config import HostRuntimeError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("INVALID_AGENT_PROFILE_COMMAND")


def _parser():
    parser = _Parser(add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--profile-id", required=True)
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--reasoning-effort", required=True,
                         choices=("low", "medium", "high", "xhigh", "max"))
    prepare.add_argument("--allowed-path", action="append", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--confirmation", required=True)
    apply.add_argument("--approve-model-smoke", action="store_true", required=True)
    return parser


def _setup() -> AgentProfileSetup:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError, RuntimeError):
        raise HostRuntimeError("AGENT_PROFILE_SETUP_DENIED") from None
    config_dir = home / ".config" / "chatgpt-coding-mcp"
    state_dir = home / ".local" / "state" / "chatgpt-coding-mcp"
    projects = _load_projects(config_dir / "projects.json")
    source = (home / ".codex" / "packages" / "standalone" / "releases"
              / "0.162.1-x86_64-unknown-linux-musl" / "bin" / "codex")
    return AgentProfileSetup(
        projects, config_path=config_dir / "host-runtime.json",
        state_dir=state_dir / "agent-profile-setup",
        runtime_root=state_dir / "agent-runtimes",
        receipts_dir=state_dir / "agent-probes", source_executable=source)


def main(argv=None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise HostRuntimeError("HOST_TTY_REQUIRED")
        setup = _setup()
        if arguments.command == "prepare":
            result = setup.prepare(
                profile_id=arguments.profile_id, project=arguments.project,
                model=arguments.model, reasoning_effort=arguments.reasoning_effort,
                allowed_paths=arguments.allowed_path, host_capabilities=[])
        else:
            result = setup.apply(
                arguments.plan_id, confirmation=arguments.confirmation,
                approve_model_smoke=arguments.approve_model_smoke)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False), flush=True)
        return 0
    except (HostRuntimeError, OSError, RuntimeError, TypeError, ValueError):
        print("AGENT_PROFILE_SETUP_DENIED", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
