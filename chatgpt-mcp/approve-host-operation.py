#!/usr/bin/env python3
"""One-use, interactive approval for exactly one pinned HostRuntime API plan.

Must execute from the installed immutable MCP snapshot, not a source checkout.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from host_runtime_config import HostRuntimeError
from host_runtime_operations import HostRuntimeOperations
from server import CONFIG, load_projects


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--project", required=True, choices=["tossApp"])
    parser.add_argument("--target", required=True, choices=["tarororo-test"])
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    try:
        projects = load_projects(CONFIG)
        operation = HostRuntimeOperations(projects, state_dir=args.state_dir,
                                          approval_script=Path(__file__))
        result = operation.approve(args.project, args.target, args.operation_id)
        print("Approved once: " + result["operation_id"])
        return 0
    except (HostRuntimeError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, HostRuntimeError) else "APPROVAL_UNAVAILABLE"
        print("ERROR: " + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
