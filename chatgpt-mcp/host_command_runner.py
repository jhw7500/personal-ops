"""Run an approved fixed host command inside one proved systemd cgroup."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import inspect
import os
from pathlib import Path
import re
import stat
import threading
import time

from agent_bootstrap import BootstrapError, build_bootstrap
from host_command_supervisor import HostCommandSupervisor
from host_runtime_config import HostRuntimeError

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ENTRY_BYTES = 256 * 1024
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
COMMAND_ENTRY_SOURCE = Path(__file__).with_name("host_command_entry.py")
BOOTSTRAP_ENV_KEYS = frozenset({
    "HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE",
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
})


def _tree_error(result: dict) -> HostRuntimeError:
    error = HostRuntimeError("HOST_COMMAND_PROCESS_TREE_UNVERIFIED", "EXECUTE")
    error.result = dict(result)
    error.termination_verified = False
    return error


def _attach(error: BaseException, result: dict, termination_verified: bool) -> None:
    try:
        error.result = dict(result)
        error.termination_verified = termination_verified
    except (AttributeError, TypeError):
        pass


def _private_execution_dir(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
    try:
        parent = path.parent
        parent_info = parent.lstat()
        if (parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700
                or parent.resolve(strict=True) != parent):
            raise OSError()
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE") from None
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or resolved != path):
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
    return path


def _copy_entry(source: Path, destination: Path) -> None:
    try:
        before = source.lstat()
        if (source.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid() or not 1 <= before.st_size <= MAX_ENTRY_BYTES):
            raise OSError()
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        try:
            opened = os.fstat(source_fd)
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_uid != os.getuid() or not stat.S_ISREG(opened.st_mode)):
                raise OSError()
            copied = 0
            while True:
                block = os.read(source_fd, min(65536, MAX_ENTRY_BYTES + 1 - copied))
                if not block:
                    break
                copied += len(block)
                if copied > MAX_ENTRY_BYTES:
                    raise OSError()
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError()
                    view = view[written:]
            after = os.fstat(source_fd)
            if (copied != before.st_size or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or after.st_ctime_ns != before.st_ctime_ns):
                raise OSError()
            os.fchmod(destination_fd, 0o700)
            os.fsync(destination_fd)
        finally:
            os.close(source_fd)
            os.close(destination_fd)
    except OSError:
        try:
            destination.unlink()
        except OSError:
            pass
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE") from None


def _validate_inputs(frozen: dict, operation_id: str) -> tuple[Path, str, list[str], int]:
    try:
        executable = frozen["executable"]
        executable_sha256 = frozen["executable_sha256"]
        project_root = Path(frozen["project_root"])
        cwd_relative = frozen["cwd_relative"]
        argv = frozen["argv"]
        timeout = frozen["timeout_seconds"]
        relative = Path(cwd_relative)
        cwd = (project_root / relative).resolve(strict=True)
        valid = (
            isinstance(operation_id, str) and HEX32.fullmatch(operation_id)
            and isinstance(executable, str) and Path(executable).is_absolute()
            and isinstance(executable_sha256, str) and HEX64.fullmatch(executable_sha256)
            and project_root.is_absolute() and project_root.resolve(strict=True) == project_root
            and project_root.is_dir() and not project_root.is_symlink()
            and isinstance(cwd_relative, str) and cwd_relative not in ("", "..")
            and not relative.is_absolute() and ".." not in relative.parts
            and cwd.is_dir() and cwd.is_relative_to(project_root)
            and isinstance(argv, list)
            and all(isinstance(item, str) and item and "\0" not in item for item in argv)
            and type(timeout) is int and 1 <= timeout <= 3600
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN", "VALIDATE")
    return project_root, cwd_relative, list(argv), timeout


async def _count_output(process, cancel: threading.Event, deadline: float, amount: dict,
                        on_output=None) -> int:
    if process.stdout is None or process.stderr is None:
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
    reads = {
        asyncio.create_task(process.stdout.read(65536)): "stdout",
        asyncio.create_task(process.stderr.read(65536)): "stderr",
    }
    waiter = asyncio.create_task(process.wait())
    try:
        while reads or not waiter.done():
            if cancel.is_set():
                raise HostRuntimeError("HOST_COMMAND_CANCELLED", "EXECUTE")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostRuntimeError("HOST_COMMAND_TIMEOUT", "EXECUTE")
            pending = set(reads)
            if not waiter.done():
                pending.add(waiter)
            done, _ = await asyncio.wait(pending, timeout=min(remaining, 0.05),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is waiter:
                    continue
                name = reads.pop(task)
                block = task.result()
                if not block:
                    continue
                amount[name] += len(block)
                if amount[name] > MAX_OUTPUT_BYTES:
                    raise HostRuntimeError("HOST_COMMAND_OUTPUT_LIMIT", "EXECUTE")
                if on_output is not None:
                    callback_result = on_output(name, block)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                stream = process.stdout if name == "stdout" else process.stderr
                reads[asyncio.create_task(stream.read(65536))] = name
        return waiter.result()
    finally:
        for task in reads:
            if not task.done():
                task.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        # Process.wait() shares transport completion state. Cancelling that task
        # can poison the supervisor's subsequent verified stop wait.
        if not waiter.done():
            waiter.add_done_callback(
                lambda task: None if task.cancelled() else task.exception())


async def _run_async(frozen: dict, cancel: threading.Event, *, execution_dir: Path,
                     operation_id: str, on_identity, supervisor_factory,
                     go_guard=None, on_output=None, input_bytes=None,
                     path_prefix=None) -> dict:
    project_root, cwd_relative, fixed_argv, timeout = _validate_inputs(frozen, operation_id)
    execution_dir = _private_execution_dir(execution_dir)
    if (not callable(on_identity) or not callable(supervisor_factory)
            or go_guard is not None and not callable(go_guard)
            or on_output is not None and not callable(on_output)
            or input_bytes is not None and not isinstance(input_bytes, bytes)):
        raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN", "VALIDATE")
    if cancel.is_set():
        error = HostRuntimeError("HOST_COMMAND_CANCELLED", "EXECUTE")
        _attach(error, {"exit_code": -1, "duration_ms": 0,
                        "stdout_bytes": 0, "stderr_bytes": 0}, True)
        raise error
    entry = execution_dir / "command-entry"
    bootstrap = execution_dir / "personal-ops-agent-bootstrap"
    _copy_entry(COMMAND_ENTRY_SOURCE, entry)
    try:
        built = build_bootstrap(bootstrap)
    except BootstrapError:
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE") from None
    entry_mode = ["--runtime-layout"] if frozen.get("kind") == "codex" else []
    argv = [str(entry), *entry_mode, frozen["executable_sha256"], frozen["executable"],
            str(project_root), cwd_relative, "--", *fixed_argv]
    # Keep the optional host-command surface importable when registered host
    # operations are absent; command_env is needed only after explicit execution.
    from host_process import command_env
    env = {key: value for key, value in command_env().items()
           if key in BOOTSTRAP_ENV_KEYS}
    if path_prefix is not None:
        prefix = Path(path_prefix)
        if (frozen.get("kind") != "codex" or not prefix.is_absolute()
                or prefix.resolve(strict=True) != prefix
                or not prefix.is_dir() or prefix.is_symlink()):
            raise HostRuntimeError("INVALID_HOST_COMMAND_PLAN", "VALIDATE")
        env["PATH"] = str(prefix) + os.pathsep + env.get("PATH", "")
    supervisor = supervisor_factory()
    started = time.monotonic()
    amount = {"stdout": 0, "stderr": 0}
    process = identity = None
    delivery = None
    terminated = False
    result = {"exit_code": -1, "duration_ms": 0,
              "stdout_bytes": 0, "stderr_bytes": 0}
    try:
        process, identity = await supervisor.start(
            operation_id, argv, project_root, env, timeout,
            expected_bootstrap_sha256=built["sha256"])
        callback_result = on_identity(dict(identity))
        if inspect.isawaitable(callback_result):
            await callback_result
        if cancel.is_set():
            raise HostRuntimeError("HOST_COMMAND_CANCELLED", "EXECUTE")
        if process.stdin is None:
            raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE")
        with (go_guard() if go_guard is not None else nullcontext()):
            process.stdin.write(b"GO\n")

        async def deliver_input():
            try:
                await process.stdin.drain()
                if input_bytes:
                    process.stdin.write(input_bytes)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Like communicate(), a command may exit without reading stdin.
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # Read output and observe cancellation while stdin is under backpressure.
        delivery = asyncio.create_task(deliver_input())
        exit_code = await _count_output(
            process, cancel, started + timeout, amount, on_output=on_output)
        await delivery
        result.update(exit_code=exit_code,
                      duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                      stdout_bytes=amount["stdout"], stderr_bytes=amount["stderr"])
        evidence = await supervisor.finish(identity)
        if evidence != {"termination_verified": True}:
            raise _tree_error(result)
        terminated = True
        supervisor.forget(identity)
        return {**result, "termination_verified": True}
    except BaseException as original:
        result.update(exit_code=(process.returncode if process is not None
                                 and process.returncode is not None else -1),
                      duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                      stdout_bytes=amount["stdout"], stderr_bytes=amount["stderr"])
        if identity is not None and not terminated:
            try:
                evidence = await supervisor.stop(identity)
                if evidence != {"termination_verified": True}:
                    raise _tree_error(result)
            except BaseException:
                raise _tree_error(result) from None
            terminated = True
            supervisor.forget(identity)
        verification = getattr(original, "termination_verified", None)
        if verification is not False:
            verification = terminated or identity is None
        _attach(original, result, verification)
        raise
    finally:
        if delivery is not None:
            if not delivery.done():
                delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        if process is not None and terminated and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except TimeoutError:
                process.kill()
                await process.wait()


def run_host_profile(frozen: dict, cancel: threading.Event, *, execution_dir: Path,
                     operation_id: str, on_identity,
                     supervisor_factory=HostCommandSupervisor, go_guard=None,
                     on_output=None, input_bytes=None, path_prefix=None) -> dict:
    """Return bounded numbers only after whole-cgroup termination is proved."""
    try:
        return asyncio.run(_run_async(
            frozen, cancel, execution_dir=execution_dir, operation_id=operation_id,
            on_identity=on_identity, supervisor_factory=supervisor_factory,
            go_guard=go_guard, on_output=on_output, input_bytes=input_bytes,
            path_prefix=path_prefix))
    except BaseException as error:
        if not hasattr(error, "result"):
            _attach(error, {"exit_code": -1, "duration_ms": 0,
                            "stdout_bytes": 0, "stderr_bytes": 0},
                    getattr(error, "termination_verified", True))
        raise
