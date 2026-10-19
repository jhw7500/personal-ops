"""Deterministic, model-free transport proof for the pinned code-mode host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import stat
import struct
import subprocess
import time


MAX_FRAME_BYTES = 1024 * 1024
MAX_FRAME_COUNT = 16
PROBE_DEADLINE_SECONDS = 2.0
MAX_HELPER_BYTES = 128 * 1024 * 1024

SESSION_ID = "offline-proof"
CELL_ID = "1"
EXPECTED_TEXT = '{"offline_delegate_ok":true,"value":42}'


class CodeModeProbeError(ValueError):
    """A sanitized code-mode transport failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validate_helper(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise CodeModeProbeError("CODE_MODE_HELPER_UNSAFE")
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CodeModeProbeError("CODE_MODE_HELPER_UNSAFE") from exc
    mode = stat.S_IMODE(before.st_mode)
    if (path.is_symlink() or resolved != path.absolute()
            or not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or not 1 <= before.st_size <= MAX_HELPER_BYTES
            or mode & 0o022 or not mode & stat.S_IXUSR):
        raise CodeModeProbeError("CODE_MODE_HELPER_UNSAFE")
    return path


def _create_fixture(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise CodeModeProbeError("CODE_MODE_FIXTURE_UNSAFE")
    parent = path.parent
    try:
        parent_info = parent.lstat()
        if (parent.is_symlink() or parent.resolve(strict=True) != parent.absolute()
                or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700):
            raise CodeModeProbeError("CODE_MODE_FIXTURE_UNSAFE")
        path.lstat()
    except FileNotFoundError:
        pass
    except CodeModeProbeError:
        raise
    except OSError as exc:
        raise CodeModeProbeError("CODE_MODE_FIXTURE_UNSAFE") from exc
    else:
        raise CodeModeProbeError("CODE_MODE_FIXTURE_UNSAFE")
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        for name in ("tmp",):
            (path / name).mkdir(mode=0o700)
            os.chmod(path / name, 0o700)
    except OSError as exc:
        raise CodeModeProbeError("CODE_MODE_FIXTURE_UNSAFE") from exc
    return path


def _environment(cwd: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(cwd / "tmp"),
    }


def _send_frame(process: subprocess.Popen, value: dict) -> None:
    try:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if not payload or len(payload) > MAX_FRAME_BYTES:
            raise CodeModeProbeError("CODE_MODE_FRAME_TOO_LARGE")
        process.stdin.write(struct.pack("<I", len(payload)) + payload)
        process.stdin.flush()
    except CodeModeProbeError:
        raise
    except (BrokenPipeError, OSError, ValueError) as exc:
        if process.poll() not in (None, 0):
            raise CodeModeProbeError("CODE_MODE_EXIT_FAILED") from exc
        raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR") from exc


def _read_exact(process: subprocess.Popen, selector: selectors.BaseSelector,
                size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodeModeProbeError("CODE_MODE_TIMEOUT")
        if not selector.select(remaining):
            raise CodeModeProbeError("CODE_MODE_TIMEOUT")
        try:
            chunk = os.read(process.stdout.fileno(), size - len(chunks))
        except OSError as exc:
            raise CodeModeProbeError("CODE_MODE_FRAME_INVALID") from exc
        if not chunk:
            returncode = process.poll()
            if returncode not in (None, 0):
                raise CodeModeProbeError("CODE_MODE_EXIT_FAILED")
            raise CodeModeProbeError("CODE_MODE_FRAME_INVALID")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive_frame(process: subprocess.Popen, selector: selectors.BaseSelector,
                   deadline: float, count: list[int]) -> dict:
    count[0] += 1
    if count[0] > MAX_FRAME_COUNT:
        raise CodeModeProbeError("CODE_MODE_FRAME_LIMIT")
    header = _read_exact(process, selector, 4, deadline)
    size = struct.unpack("<I", header)[0]
    if size < 2:
        raise CodeModeProbeError("CODE_MODE_FRAME_INVALID")
    if size > MAX_FRAME_BYTES:
        raise CodeModeProbeError("CODE_MODE_FRAME_TOO_LARGE")
    payload = _read_exact(process, selector, size, deadline)
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CodeModeProbeError("CODE_MODE_FRAME_INVALID") from exc
    if not isinstance(value, dict):
        raise CodeModeProbeError("CODE_MODE_FRAME_INVALID")
    return value


def _operation_value(frame: dict, operation_id: int) -> dict:
    if (set(frame) != {"type", "id", "result"}
            or frame.get("type") != "operation/response"
            or frame.get("id") != operation_id):
        raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")
    result = frame.get("result")
    if (not isinstance(result, dict) or set(result) != {"status", "value"}
            or result.get("status") != "ok" or not isinstance(result.get("value"), dict)):
        raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")
    return result["value"]


def _stop_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def probe_code_mode_host(helper: Path, fixture_root: Path) -> dict:
    """Prove the fixed offline delegate exchange and orderly host shutdown."""
    helper = _validate_helper(Path(helper))
    cwd = _create_fixture(Path(fixture_root))
    try:
        process = subprocess.Popen(
            [str(helper), "--listen", "stdio"], cwd=cwd, env=_environment(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0,
        )
    except OSError as exc:
        raise CodeModeProbeError("CODE_MODE_START_FAILED") from exc

    completed = False
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + PROBE_DEADLINE_SECONDS
    count = [0]
    try:
        _send_frame(process, {
            "type": "connection/hello", "supportedVersions": [1],
            "requiredCapabilities": [], "optionalCapabilities": [],
        })
        if _receive_frame(process, selector, deadline, count) != {
                "type": "connection/ready", "selectedVersion": 1, "capabilities": []}:
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")

        _send_frame(process, {
            "type": "operation/request", "id": 1,
            "request": {"method": "session/open", "sessionId": SESSION_ID},
        })
        if _operation_value(_receive_frame(process, selector, deadline, count), 1) != {
                "type": "session/ready", "sessionId": SESSION_ID}:
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")

        _send_frame(process, {
            "type": "operation/request", "id": 2,
            "request": {
                "method": "session/execute", "sessionId": SESSION_ID,
                "request": {
                    "tool_call_id": "offline-call",
                    "enabled_tools": [{
                        "name": "probe",
                        "tool_name": {"name": "probe", "namespace": None},
                        "description": "Offline deterministic echo", "kind": "function",
                        "input_schema": {"type": "object"}, "output_schema": None,
                    }],
                    "source": "const r = await tools.probe({value:42}); text(r);",
                    "yield_time_ms": 1000, "max_output_tokens": 100,
                },
            },
        })
        if _operation_value(_receive_frame(process, selector, deadline, count), 2) != {
                "type": "execution/started", "cellId": CELL_ID}:
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")

        delegate = _receive_frame(process, selector, deadline, count)
        expected_delegate = {
            "type": "delegate/request", "id": 1, "sessionId": SESSION_ID,
            "request": {"type": "tool/invoke", "invocation": {
                "cell_id": CELL_ID, "runtime_tool_call_id": "tool-1",
                "tool_name": {"name": "probe", "namespace": None},
                "tool_kind": "function", "input": {"value": 42},
            }},
        }
        if delegate != expected_delegate:
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")
        _send_frame(process, {
            "type": "delegate/response", "id": 1,
            "result": {"status": "ok", "value": {"type": "tool/result",
                        "result": {"offline_delegate_ok": True, "value": 42}}},
        })

        initial = _receive_frame(process, selector, deadline, count)
        if (set(initial) != {"type", "id", "result"}
                or initial.get("type") != "execute/initialResponse" or initial.get("id") != 2):
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")
        result = initial.get("result")
        value = result.get("value") if isinstance(result, dict) else None
        output = value.get("Result") if isinstance(value, dict) else None
        if (not isinstance(result, dict) or set(result) != {"status", "value"}
                or result.get("status") != "ok" or not isinstance(value, dict)
                or set(value) != {"Result"}
                or not isinstance(output, dict)
                or set(output) != {"cell_id", "content_items", "error_text",
                                   "code_mode_host_duration_ns"}
                or output.get("cell_id") != CELL_ID or output.get("error_text") is not None
                or output.get("content_items") != [{"type": "input_text", "text": EXPECTED_TEXT}]
                or type(output.get("code_mode_host_duration_ns")) is not int
                or output["code_mode_host_duration_ns"] < 0):
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")

        _send_frame(process, {
            "type": "operation/request", "id": 3,
            "request": {"method": "session/shutdown", "sessionId": SESSION_ID},
        })
        shutdown = _receive_frame(process, selector, deadline, count)
        if shutdown == {"type": "cell/closed", "sessionId": SESSION_ID, "cellId": CELL_ID}:
            shutdown = _receive_frame(process, selector, deadline, count)
        if _operation_value(shutdown, 3) != {
                "type": "session/closed", "sessionId": SESSION_ID}:
            raise CodeModeProbeError("CODE_MODE_PROTOCOL_ERROR")

        process.stdin.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodeModeProbeError("CODE_MODE_TIMEOUT")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise CodeModeProbeError("CODE_MODE_TIMEOUT") from None
        if returncode != 0:
            raise CodeModeProbeError("CODE_MODE_EXIT_FAILED")
        completed = True
        return {
            "code_mode_delegate_executed": True,
            "code_mode_shutdown_verified": True,
        }
    finally:
        selector.close()
        if not completed:
            try:
                process.stdin.close()
            except OSError:
                pass
            _stop_process_group(process)
        try:
            process.stdout.close()
        except OSError:
            pass
