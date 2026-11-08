#!/usr/bin/env python3
"""Create private review text from the pinned ZIP; never apply, install or register."""
import argparse
import json
from pathlib import Path

from scoped_bootstrap import PreparationError, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare",))
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.package, args.source_root, args.output)
    except PreparationError as error:
        parser.exit(3, "FAILED_CLOSED: " + str(error) + "\n")
    print(json.dumps({"status": result["status"], "baseline_eligible": False,
                      "patch_sha256": result["patch_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
