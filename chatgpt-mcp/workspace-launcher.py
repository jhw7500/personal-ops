#!/usr/bin/env python3
"""Stable external launcher for an admitted immutable MCP release."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys


CONTROLLER_ROOT = Path(__file__).resolve().parent
if str(CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_ROOT))

from workspace_control import (_default_authority_dir, WorkspaceControlError,  # noqa: E402
                               verify_launch)


_ISOLATED_BOOTSTRAP = """\
import runpy
import sys
release, entrypoint, *arguments = sys.argv[1:]
sys.path.insert(0, release)
sys.argv = [entrypoint, *arguments]
runpy.run_path(entrypoint, run_name="__main__")
"""


def _interpreter(path: str) -> str:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise WorkspaceControlError("UNSAFE_WORKSPACE_INTERPRETER") from exc
    trusted_system = resolved.is_relative_to(Path("/usr/bin")) or resolved.is_relative_to(
        Path("/usr/local/bin")
    )
    if (not candidate.is_absolute() or not stat.S_ISREG(info.st_mode)
            or not os.access(resolved, os.X_OK)
            or stat.S_IMODE(info.st_mode) & 0o022
            or (info.st_uid != os.getuid() and not (trusted_system and info.st_uid == 0))):
        raise WorkspaceControlError("UNSAFE_WORKSPACE_INTERPRETER")
    return str(candidate)


def _authority_dir(explicit) -> Path:
    """Bind launch admission to the authority path child code will discover."""
    canonical = _default_authority_dir().absolute()
    if explicit is not None and Path(explicit).absolute() != canonical:
        raise WorkspaceControlError("WORKSPACE_AUTHORITY_ENV_MISMATCH")
    return canonical


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify and execute one MCP release")
    parser.add_argument("--authority-dir")
    parser.add_argument("--release", required=True)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--entrypoint", choices=(
        "server.py", "admin_server.py", "runtime_rollout_worker.py",
    ),
                        default="server.py")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    release = Path(args.release).absolute()
    authority_dir = _authority_dir(args.authority_dir)
    verify_launch(release, authority_dir, enforcement_required=True)
    interpreter = _interpreter(args.interpreter)
    entrypoint = release / args.entrypoint
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if args.isolated:
        child = [interpreter, "-I", "-c", _ISOLATED_BOOTSTRAP,
                 str(release), str(entrypoint), *arguments]
    else:
        child = [interpreter, str(entrypoint), *arguments]
    os.execv(interpreter, child)
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkspaceControlError as exc:
        sys.stderr.write(exc.code + "\n")
        raise SystemExit(126)
