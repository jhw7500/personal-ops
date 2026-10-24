#!/usr/bin/env python3
"""Explicit, local-TTY-only opt-in for the dedicated Toss test host runtime.

This is an operator setup utility, not an MCP action. Never run it from
start_command or expose it as a remote MCP tool.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from host_registry import HostTargetRegistry
from host_runtime_config import (CONFIG_PATH, MAX_CONFIG_BYTES, TARGET, TARGET_CONFIG,
                                 TARGET_CONFIG_V2, HostRuntimeError, is_registered_config,
                                 load_config, parse_config,
                                 read_config, config_write_lock)

PROJECTS_PATH = CONFIG_PATH.parent / "projects.json"


def enable(path: Path = CONFIG_PATH, *, input_stream=None, output=None, extended=False) -> str:
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    if not input_stream.isatty() or not output.isatty():
        raise RuntimeError("HOST_TTY_REQUIRED")
    path = Path(path).absolute()
    parent = path.parent
    if (any(folder.is_symlink() for folder in [parent, *parent.parents])
            or not parent.is_dir() or parent.stat().st_uid != os.getuid()
            or stat.S_IMODE(parent.stat().st_mode) & 0o022):
        raise RuntimeError("UNSAFE_CONFIG_DIRECTORY")
    if path.is_symlink():
        raise RuntimeError("UNSAFE_CONFIG_PATH")
    config = {"version": 2 if extended else 1, "enabled": True,
              "targets": {TARGET: TARGET_CONFIG_V2 if extended else TARGET_CONFIG}}
    previous = None
    if path.exists():
        # Existing configs are never silently overwritten; invalid configs fail closed.
        existing = load_config(path)
        previous = path.read_bytes()
        prior_version = json.loads(previous)["version"]
        if prior_version == 3:
            # Neither predecessor v3 nor a registered envelope may be downgraded here.
            raise RuntimeError("UNSUPPORTED_CONFIG_VERSION")
        if existing and (not extended or prior_version == 2):
            return "already_enabled"
        state = "enabled_api_only" if existing else "disabled"
    else:
        state = "missing"
    print(f"Target: tossApp / {TARGET}; current: {state}", file=output)
    confirmation = "ENABLE EXTENDED" if extended else "ENABLE"
    if extended:
        print("Allows fixed API/Worker and migration plans. Each execution still needs its own approval.", file=output)
        print("This changes only configuration; no Docker/DB action or Schema 013 application.", file=output)
    else:
        print("Allows fixed, scoped Docker read inspection and separately TTY-approved API deployments.", file=output)
    print(f"Type {confirmation} to opt in: ", end="", file=output, flush=True)
    if input_stream.readline().strip() != confirmation:
        raise RuntimeError("OPT_IN_DECLINED")
    with config_write_lock(path):
        if path.exists() or path.is_symlink():
            load_config(path)
            if previous is None or path.read_bytes() != previous:
                raise RuntimeError("CONFIG_CHANGED_DURING_APPROVAL")
        elif previous is not None:
            raise RuntimeError("CONFIG_CHANGED_DURING_APPROVAL")
        payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
        temporary = parent / (".host-runtime-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if not load_config(path):
        raise RuntimeError("CONFIG_VERIFICATION_FAILED")
    return "enabled_extended" if extended else "enabled"


def _pretty(config) -> list[str]:
    return (json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n").splitlines(True)


def validate_candidate(candidate: Path, *, current: Path = CONFIG_PATH,
                       projects_config: Path = PROJECTS_PATH, output=None) -> str:
    """Read-only: validate a version 3 candidate and print its diff. Never writes or applies."""
    output = output or sys.stdout
    try:
        # O_NONBLOCK: a FIFO candidate must not block; fstat below rejects it.
        fd = os.open(Path(candidate).absolute(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeError("UNSAFE_CANDIDATE_PATH") from exc
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise RuntimeError("UNSAFE_CANDIDATE_PATH")
        payload = parse_config(source.read(MAX_CONFIG_BYTES + 1))
    if not is_registered_config(payload):
        raise RuntimeError("UNSUPPORTED_CONFIG_VERSION")
    try:
        configured = json.loads(Path(projects_config).read_text(encoding="utf-8"))["projects"]
        if not isinstance(configured, dict):
            raise TypeError("projects")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("INVALID_PROJECTS_CONFIG") from exc
    registry = HostTargetRegistry.from_payload(payload, {name: Path(str(value))
                                                         for name, value in configured.items()})
    try:
        existing = read_config(current)
        state = "missing" if existing is None else f"version {existing['version']}"
    except HostRuntimeError as exc:
        existing, state = None, exc.code
    print(f"Candidate: valid version 3, {len(payload['targets'])} target(s), "
          f"digest {registry.config_digest()}", file=output)
    print(f"Current: {state}", file=output)
    output.writelines(difflib.unified_diff([] if existing is None else _pretty(existing),
                                           _pretty(payload), "current", "candidate"))
    print("Validation only: not applied. No file was written.", file=output)
    return "valid"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", action="store_true",
                      help="Opt in locally with explicit TTY confirmation.")
    mode.add_argument("--validate-v3", type=Path, metavar="CANDIDATE",
                      help="Validate a version 3 candidate and show its diff (read-only).")
    parser.add_argument("--extended", action="store_true",
                        help="Separately opt in to fixed Worker and migration plans (configuration only).")
    parser.add_argument("--projects", type=Path, default=PROJECTS_PATH,
                        help="projects.json used to check v3 target projects (read-only).")
    args = parser.parse_args()
    if args.validate_v3 and args.extended:
        parser.error("--extended applies only to --enable")
    try:
        if args.validate_v3:
            print(validate_candidate(args.validate_v3, projects_config=args.projects))
            sys.exit(0)
        print(enable(extended=args.extended))
    except Exception as exc:
        print(f"Host runtime setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
