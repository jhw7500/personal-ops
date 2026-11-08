#!/usr/bin/env python3
"""Write a bounded post-install verification receipt for one rollout plan."""
from __future__ import annotations

import argparse
import json
import sys

from runtime_verification import RuntimeVerificationError, verify_operation


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_operation(args.operation_id)
    except RuntimeVerificationError as exc:
        print(json.dumps({"status": "failed", "code": exc.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "code": "VERIFICATION_FAILED"}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    # Runtime/catalog evidence is complete, but connector adoption is pending.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
