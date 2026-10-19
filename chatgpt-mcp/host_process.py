"""Bounded backend command runner for registered host operations.

Internal only: no shell, a sanitized fixed environment, a dedicated process group,
concurrent stdin/stdout/stderr handling and a live 256 KiB stdout cap. Errors carry a
code, the exit status and a validated SQLSTATE only, never raw output or stdin.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from host_registry import compose_env_valid
from host_runtime import MAX_OUTPUT, SANITIZED_PATH
from host_runtime_config import HostRuntimeError

PG_ENV_KEYS = frozenset({"PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD", "PGPASSFILE",
                         "PGSSLMODE", "PGAPPNAME", "PGCONNECT_TIMEOUT", "PGOPTIONS"})
MAX_STDERR = 8 * 1024
MAX_STDIN = 16 * 1024 * 1024
MAX_TIMEOUT = 3600
MAX_ENV_VALUE = 4096
# Only the psql VERBOSITY=sqlstate shape counts: a whole line "ERROR:  42P01" (or FATAL/
# PANIC), optionally prefixed "psql:<stdin>:<line>: " when SQL arrives on stdin. Backends
# must run psql with `-v VERBOSITY=sqlstate`; default/verbose messages never match, so a
# message whose first word is five capitals (e.g. "ERROR:  ABORT: ...") is not a SQLSTATE.
SQLSTATE_LINE = re.compile(
    rb"^(?:psql:<stdin>:[0-9]+: )?(?:ERROR|FATAL|PANIC):[ \t]+([0-9A-Z]{5})[ \t]*$", re.M)


class HostCommandError(HostRuntimeError):
    """Sanitized command failure: code, phase, exit status and SQLSTATE only."""

    def __init__(self, code: str, phase: str = "EXECUTE", *, returncode: int | None = None,
                 sqlstate: str | None = None):
        super().__init__(code, phase)
        self.returncode = returncode
        self.sqlstate = sqlstate


def command_env(*, credential_env: dict[str, str] | None = None,
                compose_env: dict[str, str] | None = None) -> dict[str, str]:
    """The only place the child environment is built; nothing is inherited. compose_env is
    the target's host-registered compose.env map, never client input."""
    env = {
        "PATH": SANITIZED_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(Path.home()),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
    }
    if credential_env is not None:
        if (not isinstance(credential_env, dict)
                or any(key not in PG_ENV_KEYS or not isinstance(value, str)
                       or "\0" in value or len(value) > MAX_ENV_VALUE
                       for key, value in credential_env.items())):
            raise HostCommandError("INVALID_CREDENTIAL_ENV", "VALIDATE")
        env.update(credential_env)
    if compose_env is not None:
        if not compose_env_valid(compose_env) or set(compose_env) & set(env):
            raise HostCommandError("INVALID_COMPOSE_ENV", "VALIDATE")
        env.update(compose_env)
    return env


def _sqlstate(stderr: bytes) -> str | None:
    match = SQLSTATE_LINE.search(stderr)
    return match.group(1).decode("ascii") if match else None


def _validate(argv, timeout, stdin_data) -> None:
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(part, str) or not part or "\0" in part for part in argv)):
        raise HostCommandError("INVALID_COMMAND", "VALIDATE")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_TIMEOUT):
        raise HostCommandError("INVALID_COMMAND", "VALIDATE")
    if stdin_data is not None and (not isinstance(stdin_data, bytes)
                                   or len(stdin_data) > MAX_STDIN):
        raise HostCommandError("INVALID_COMMAND", "VALIDATE")


def _stop_group(process: subprocess.Popen) -> None:
    """Terminate only the dedicated session/process group of a still-running leader."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def run_registered_command(argv: list[str], *, cwd: Path | None, timeout: int,
                           stdin_data: bytes | None = None,
                           credential_env: dict[str, str] | None = None,
                           compose_env: dict[str, str] | None = None) -> bytes:
    """Run one fixed argv with shell=False and return at most 256 KiB of stdout."""
    _validate(argv, timeout, stdin_data)
    env = command_env(credential_env=credential_env, compose_env=compose_env)
    process = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    failure = None
    try:
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, shell=False, start_new_session=True, close_fds=True,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, ValueError):
            raise HostCommandError("EXECUTOR_UNAVAILABLE") from None  # No path in __cause__.
        deadline = time.monotonic() + timeout
        pending = memoryview(stdin_data) if stdin_data is not None else None
        for stream, event in ((process.stdout, selectors.EVENT_READ),
                              (process.stderr, selectors.EVENT_READ),
                              (process.stdin, selectors.EVENT_WRITE)):
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
        try:
            while selector.get_map() and failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "HOST_COMMAND_TIMEOUT"
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            written = os.write(stream.fileno(), pending[:65536])
                            pending = pending[written:]
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, ConnectionResetError):
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    if stream is process.stdout:
                        stdout.extend(chunk)
                        if len(stdout) > MAX_OUTPUT:
                            failure = "HOST_OUTPUT_TOO_LARGE"
                            break
                    else:
                        stderr.extend(chunk)
                        del stderr[:-MAX_STDERR]
        except OSError:
            raise HostCommandError("HOST_COMMAND_FAILED") from None  # Pipe I/O failure.
        if failure is None:
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=max(remaining, 0.001))
            except subprocess.TimeoutExpired:
                failure = "HOST_COMMAND_TIMEOUT"
        if failure is not None:
            raise HostCommandError(failure)
        if process.returncode != 0:
            raise HostCommandError("HOST_COMMAND_FAILED", returncode=process.returncode,
                                   sqlstate=_sqlstate(bytes(stderr)))
        return bytes(stdout)
    finally:
        selector.close()
        if process is not None:
            _stop_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
