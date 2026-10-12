#!/usr/bin/env python3
"""Run one command against a disposable PostgreSQL inside the MCP sandbox."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote


POSTGRES_PREFIX = Path("/opt/postgresql")
ETC_DIR = Path("/etc")
WORKSPACE_PREFIX = Path("/workspace")
ALLOWED_DATABASES = {"tarororo_test", "tarororo_ci"}


def _write_identity() -> None:
    passwd = ETC_DIR / "passwd"
    group = ETC_DIR / "group"
    nsswitch = ETC_DIR / "nsswitch.conf"
    if passwd.exists() or group.exists() or nsswitch.exists():
        raise RuntimeError("PostgreSQL sandbox identity files must start empty")
    passwd.write_text(
        f"mcp:x:{os.getuid()}:{os.getgid()}:MCP sandbox:/home/sandbox:/bin/false\n",
        encoding="utf-8",
    )
    group.write_text(f"mcp:x:{os.getgid()}:\n", encoding="utf-8")
    nsswitch.write_text("passwd: files\ngroup: files\n", encoding="utf-8")
    passwd.chmod(0o644)
    group.chmod(0o644)
    nsswitch.chmod(0o644)


def _clean_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("PG") and key not in {"DATABASE_URL", "TEST_DATABASE_URL"}
    }
    environment["PATH"] = f"{POSTGRES_PREFIX}/bin:{environment.get('PATH', '')}"
    library = POSTGRES_PREFIX / "lib"
    if library.is_dir():
        environment["LD_LIBRARY_PATH"] = str(library)
    return environment


def _binary(name: str) -> str:
    path = POSTGRES_PREFIX / "bin" / name
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(POSTGRES_PREFIX.resolve(strict=True)) or not resolved.is_file():
        raise RuntimeError(f"Unsafe PostgreSQL executable: {name}")
    return str(path)


def run(database: str, command: list[str]) -> int:
    if os.environ.get("MCP_EPHEMERAL_POSTGRES") != "1":
        raise RuntimeError("This wrapper runs only inside the MCP PostgreSQL sandbox")
    if database not in ALLOWED_DATABASES:
        raise ValueError("Unsupported test database")
    if not command:
        raise ValueError("A child command is required")
    cwd = Path(os.getcwd()).resolve(strict=True)
    workspace = WORKSPACE_PREFIX.resolve(strict=True)
    if not cwd.is_relative_to(workspace):
        raise RuntimeError("The child command must run inside the sandbox workspace")

    _write_identity()
    base_environment = _clean_environment()
    initdb = _binary("initdb")
    pg_ctl = _binary("pg_ctl")
    createdb = _binary("createdb")
    temporary = Path(tempfile.mkdtemp(prefix="mcp-postgres-"))
    data = temporary / "data"
    socket_dir = temporary / "socket"
    password_file = temporary / "password"
    log_file = temporary / "postgres.log"
    socket_dir.mkdir(mode=0o700)
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)
    initialized = False
    started = False
    child_code = 1
    cleanup_failed = False

    print("Preparing isolated PostgreSQL test database", flush=True)
    try:
        subprocess.run([
            initdb, "-D", str(data), "--username=mcp", "--locale=C", "--encoding=UTF8",
            "--auth-local=scram-sha-256", "--auth-host=scram-sha-256",
            f"--pwfile={password_file}", "--no-instructions",
        ], env=base_environment, check=True)
        initialized = True
        password_file.unlink(missing_ok=True)
        options = (
            f"-h 127.0.0.1 -p 5432 -k {socket_dir} "
            "-c listen_addresses=127.0.0.1 -c dynamic_shared_memory_type=mmap"
        )
        subprocess.run([
            pg_ctl, "-D", str(data), "-l", str(log_file), "-w", "-t", "30",
            "-o", options, "start",
        ], env=base_environment, check=True)
        started = True
        administration = dict(base_environment)
        administration.update({
            "PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": "mcp",
            "PGPASSWORD": password,
        })
        subprocess.run([createdb, database], env=administration, check=True)
        child_environment = dict(base_environment)
        encoded_password = quote(password, safe="")
        child_environment["TEST_DATABASE_URL"] = (
            f"postgresql://mcp:{encoded_password}@127.0.0.1:5432/{database}")
        print("PostgreSQL test database ready; starting command", flush=True)
        child_code = subprocess.run(command, env=child_environment, check=False).returncode
    finally:
        password_file.unlink(missing_ok=True)
        if initialized:
            print("Stopping isolated PostgreSQL test database", flush=True)
            stop = subprocess.run([
                pg_ctl, "-D", str(data), "-w", "-t", "30", "stop", "-m", "fast",
            ], env=base_environment, check=False)
            cleanup_failed = started and stop.returncode != 0
        shutil.rmtree(temporary, ignore_errors=True)
    if cleanup_failed:
        print("PostgreSQL cleanup failed", file=sys.stderr, flush=True)
        return 1
    return child_code


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        raise SystemExit("usage: postgres_job.py DATABASE -- COMMAND [ARG ...]")
    return run(argv[1], argv[3:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
