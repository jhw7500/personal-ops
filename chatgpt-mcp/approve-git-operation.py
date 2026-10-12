#!/usr/bin/env python3
"""Interactive host-side approval for one prepared Git operation."""

import argparse
import json
from pathlib import Path
import sys

from git_workflows import approve_operation


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    try:
        result = approve_operation(args.state_dir, args.project, args.operation_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Approved once: {result['operation_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
