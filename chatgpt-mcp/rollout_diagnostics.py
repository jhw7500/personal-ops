"""Private, durable rollout metadata and isolated, bounded subprocesses.

Command arguments, environment, raw stdout/stderr and exception messages never
enter a receipt. This module controls only processes it starts in a new session.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import uuid

from jobs import private_directory, private_file


RECEIPT_ROOT = Path.home() / ".local/state/chatgpt-coding-mcp/rollouts"
MAX_EVENTS = 1024
MAX_CLI_SESSIONS = 64
MAX_PROC_ENTRIES = 8192
_RECEIPT_FAILURES = {"ROLLOUT_RECEIPT_WRITE_FAILED", "ROLLOUT_RECEIPT_LIMIT"}
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_DIGEST = re.compile(r"[0-9a-f]{20}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TEST_ID = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}\Z")
_UNITTEST_FAILURE = re.compile(
    rb"(?:FAIL|ERROR): ([A-Za-z_][A-Za-z0-9_]{0,127}) "
    rb"\(([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\)\r?\Z")
_UNITTEST_STAGES = frozenset({"checks_targeted", "checks_full", "checks_approval_ui",
                              "checks_workspace_approval_browser",
                              "checks_schema014_ui", "checks_runtime_rollout_ui",
                              "checks_host_command_ui"})
_NODE_TEST_STAGE = "checks_workspace_ui"
_NODE_TEST_FILE = re.compile(r"test_[a-z0-9_]{1,118}\.mjs\Z")
_NODE_TAP_NOT_OK = re.compile(rb"not ok [1-9][0-9]{0,5} - [^\r\n]{1,300}\r?\Z")
_NODE_LOCATION_SUFFIX = re.compile(rb":[1-9][0-9]{0,9}:[1-9][0-9]{0,9}\Z")
_LABEL_FIELDS = {"status", "admin_status", "rollback_status", "catalog_status"}
_CODE_FIELDS = {"code", "cause_code", "output_code"}
_DIGEST_FIELDS = {"snapshot", "previous_snapshot", "live_snapshot"}
_HASH_FIELDS = {"stdout_sha256", "stderr_sha256", "control_sha256", "cgroup_sha256"}
_TEST_LIST_FIELDS = {"failed_tests"}
_TEST_FILE_LIST_FIELDS = {"failed_test_files"}
_BOOL_FIELDS = {"installed", "switched", "tunnel_healthy", "tunnel_id_available",
                "profile_aligned", "service_aligned", "runtime_verified",
                "schema_013_applied", "tools_registered", "connector_schemas_verified",
                "cli_sessions_preserved", "cli_observation_complete", "cgroup_observed",
                "exit_status_available"}
_INT_FIELDS = {"returncode", "duration_ms", "stdout_bytes", "stderr_bytes",
               "source_files", "test_count", "pid", "start_ticks", "tunnel_pid",
               "tunnel_start_ticks", "mcp_pid", "mcp_start_ticks", "admin_pid",
               "tool_count", "mcp_tool_count", "codex_pid", "codex_start_ticks",
               "ppid", "pgid", "sid", "tty_nr", "cli_session_count", "cli_changed_count",
               "observed_start_ticks"}
_PROCESS_STATES = {"R": "running", "S": "sleeping", "D": "disk_sleep", "Z": "zombie",
                   "T": "stopped", "t": "tracing_stop", "X": "dead", "I": "idle"}
_CLI_ENUM_FIELDS = {
    "cli_kind": {"codex", "claude"},
    "process_state": set(_PROCESS_STATES.values()) | {"unknown"},
    "observation_status": {"same_process", "missing", "identity_changed", "exited", "unavailable"},
}
_OUTPUT_ERRORS = (
    (b"ModuleNotFoundError:", "PYTHON_IMPORT_ERROR"),
    (b"ImportError:", "PYTHON_IMPORT_ERROR"),
    (b"Permission denied", "COMMAND_PERMISSION_DENIED"),
    (b"unrecognized arguments:", "COMMAND_ARGUMENTS_REJECTED"),
    (b"Address already in use", "COMMAND_ADDRESS_IN_USE"),
    (b"FAILED (", "TESTS_FAILED"),
)


def safe_error(exc):
    """Preserve an explicit error code, never arbitrary exception text."""
    code = getattr(exc, "code", None)
    if code is None and isinstance(exc, RuntimeError) and len(exc.args) == 1:
        code = exc.args[0]
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    if isinstance(exc, subprocess.TimeoutExpired):
        return "ROLLOUT_COMMAND_TIMEOUT"
    if isinstance(exc, OSError):
        return "ROLLOUT_OS_ERROR"
    if isinstance(exc, KeyboardInterrupt):
        return "ROLLOUT_INTERRUPTED"
    return "ROLLOUT_UNEXPECTED_ERROR"


def is_interruption(exc):
    return (isinstance(exc, (KeyboardInterrupt, SystemExit))
            or safe_error(exc) in {"ROLLOUT_TERMINATED", "ROLLOUT_INTERRUPTED"})


def _safe_fields(values):
    for key, value in values.items():
        if key in _CLI_ENUM_FIELDS:
            valid = isinstance(value, str) and value in _CLI_ENUM_FIELDS[key]
        elif key in _LABEL_FIELDS:
            valid = isinstance(value, str) and _LABEL.fullmatch(value)
        elif key in _CODE_FIELDS:
            valid = isinstance(value, str) and _CODE.fullmatch(value)
        elif key in _DIGEST_FIELDS:
            valid = isinstance(value, str) and _DIGEST.fullmatch(value)
        elif key in _HASH_FIELDS:
            valid = isinstance(value, str) and _HASH.fullmatch(value)
        elif key in _BOOL_FIELDS:
            valid = type(value) is bool
        elif key in _INT_FIELDS:
            valid = type(value) is int and (key == "returncode" or value >= 0)
        elif key in _TEST_LIST_FIELDS:
            valid = (isinstance(value, list) and 0 < len(value) <= 32
                     and all(isinstance(item, str) and len(item) <= 300
                             and _TEST_ID.fullmatch(item) for item in value)
                     and len(value) == len(set(value)))
        elif key in _TEST_FILE_LIST_FIELDS:
            valid = (isinstance(value, list) and 0 < len(value) <= 32
                     and all(isinstance(item, str) and _NODE_TEST_FILE.fullmatch(item)
                             for item in value)
                     and len(value) == len(set(value)))
        else:
            valid = False
        if not valid:
            raise ValueError("Unsafe rollout receipt field")
    return dict(values)


class RolloutReceipt:
    def __init__(self, root=RECEIPT_ROOT, *, mode="preflight"):
        if mode not in ("preflight", "apply", "verify"):
            raise ValueError("Invalid rollout mode")
        base = private_directory(root)
        folder = Path(tempfile.mkdtemp(prefix="rollout-", dir=base))
        folder.chmod(0o700)
        self.path = folder / "receipt.json"
        self.data = {"version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
                     "operator_pid": os.getpid(), "mode": mode,
                     "status": "running", "events": []}
        self.write_failed = False
        self._started_monotonic = time.monotonic()
        self._write_checked()

    def _write_checked(self):
        try:
            self._save()
        except (OSError, ValueError, RuntimeError):
            self.write_failed = True
            raise RuntimeError("ROLLOUT_RECEIPT_WRITE_FAILED") from None

    def _save(self):
        if self.path.exists() or self.path.is_symlink():
            private_file(self.path)
        temporary = self.path.parent / ("." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self.data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def event(self, stage, state, **details):
        if not isinstance(stage, str) or not _LABEL.fullmatch(stage):
            raise ValueError("Unsafe rollout stage")
        if state not in ("started", "completed", "failed"):
            raise ValueError("Unsafe rollout state")
        if len(self.data["events"]) >= MAX_EVENTS:
            self.write_failed = True
            raise RuntimeError("ROLLOUT_RECEIPT_LIMIT")
        self.data["events"].append({"stage": stage, "state": state,
                                    "observed_at": datetime.now(timezone.utc).isoformat(),
                                    "elapsed_ms": int((time.monotonic() - self._started_monotonic) * 1000),
                                    **_safe_fields(details)})
        self._write_checked()

    @contextmanager
    def phase(self, stage):
        self.event(stage, "started")
        try:
            yield
        except BaseException as exc:
            cause = getattr(exc, "cause_code", None)
            extra = {"cause_code": cause} if isinstance(cause, str) and _CODE.fullmatch(cause) else {}
            rollback = getattr(exc, "rollback_status", None)
            if rollback in ("succeeded", "failed"):
                extra["rollback_status"] = rollback
            try:
                self.event(stage, "failed", code=safe_error(exc), **extra)
            except RuntimeError as log_error:
                if safe_error(log_error) not in _RECEIPT_FAILURES:
                    raise
                exc.receipt_write_failed = True
            raise
        else:
            self.event(stage, "completed")

    def finish(self, result, exit_code):
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise ValueError("Invalid rollout exit code")
        safe = _safe_fields(result)
        self.data.update(status=safe.get("status", "failed"), result=safe,
                         exit_code=exit_code, finished_at=datetime.now(timezone.utc).isoformat())
        self._write_checked()


def _read_proc_file(folder, name):
    path = folder / name
    if path.is_symlink():
        raise ValueError("Unsafe process metadata")
    with path.open("rb") as stream:
        data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError("Oversized process metadata")
    return data


def _proc_stat(folder):
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Unowned process")
    raw = _read_proc_file(folder, "stat").decode("ascii", errors="replace")
    fields = raw[raw.rindex(")") + 1:].split()
    if raw.split(" ", 1)[0] != folder.name or len(fields) < 50:
        raise ValueError("Invalid process metadata")
    row = {"pid": int(folder.name), "ppid": int(fields[1]), "pgid": int(fields[2]),
           "sid": int(fields[3]), "tty_nr": int(fields[4]) & 0xffffffff,
           "start_ticks": int(fields[19]),
           "process_state": _PROCESS_STATES.get(fields[0], "unknown")}
    if any(row[key] < 0 for key in ("ppid", "pgid", "sid", "start_ticks")):
        raise ValueError("Invalid process identity")
    row["exit_status_available"] = fields[0] == "Z"
    if row["exit_status_available"]:
        row["returncode"] = os.waitstatus_to_exitcode(int(fields[49]))
    return row


def _cli_kind(folder):
    argv = _read_proc_file(folder, "cmdline").split(b"\0")
    name = argv[0].rsplit(b"/", 1)[-1]
    if name == b"codex" and not any(arg in {b"app-server", b"exec"} for arg in argv[1:]):
        return "codex"
    if name == b"claude" and not any(arg in {b"-p", b"--print"} for arg in argv[1:]):
        return "claude"
    return None


class _ProcessIdentityChanged(ValueError):
    def __init__(self, observed_start_ticks):
        super().__init__("Process identity changed during observation")
        self.observed_start_ticks = observed_start_ticks


def _observe_process(folder):
    before = _proc_stat(folder)
    try:
        cgroup = _read_proc_file(folder, "cgroup")
    except (OSError, ValueError):
        cgroup = None
    after = _proc_stat(folder)
    if before["start_ticks"] != after["start_ticks"]:
        raise _ProcessIdentityChanged(after["start_ticks"])
    after["cgroup_observed"] = cgroup is not None
    if cgroup is not None:
        after["cgroup_sha256"] = hashlib.sha256(cgroup).hexdigest()
    return after


def _process_directory_missing(folder):
    try:
        folder.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


class CliSessionObserver:
    """Read-only switch-boundary evidence; never signals or waits on a CLI.

    A reaped non-child has no observable exit status. Missing/reused PIDs remain
    changes with unknown cause; even a stable sample cannot prove no brief stop.
    """

    def __init__(self, receipt, *, proc_root=Path("/proc"), excluded_pids=()):
        self.receipt = receipt
        self.proc_root = Path(proc_root)
        self.excluded_pids = set(excluded_pids)
        self.baseline = {}
        self.changed = set()
        self.summary = {"cli_observation_complete": True, "cli_sessions_preserved": True,
                        "cli_session_count": 0, "cli_changed_count": 0}

    def _event(self, stage, **details):
        if self.receipt is not None:
            self.receipt.event(stage, "completed", **details)

    def __enter__(self):
        try:
            for index, folder in enumerate(self.proc_root.iterdir()):
                if index >= MAX_PROC_ENTRIES:
                    self.summary["cli_observation_complete"] = False
                    break
                if not folder.name.isdecimal() or int(folder.name) in self.excluded_pids:
                    continue
                try:
                    info = folder.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                        continue
                    first = _proc_stat(folder)
                    kind = _cli_kind(folder)
                    if kind is None:
                        continue
                    row = _observe_process(folder)
                    if first["start_ticks"] != row["start_ticks"]:
                        raise ValueError("Process identity changed during discovery")
                    if row["process_state"] in {"zombie", "dead"}:
                        continue
                    if len(self.baseline) >= MAX_CLI_SESSIONS:
                        self.summary["cli_observation_complete"] = False
                        break
                    self.baseline[row["pid"]] = {**row, "cli_kind": kind}
                except FileNotFoundError:
                    # A live PID with a missing metadata file is incomplete;
                    # only an actually vanished directory can be ignored here.
                    if _process_directory_missing(folder):
                        continue
                    self.summary["cli_observation_complete"] = False
                except (OSError, ValueError):
                    self.summary["cli_observation_complete"] = False
        except OSError:
            self.summary["cli_observation_complete"] = False
        self.summary["cli_session_count"] = len(self.baseline)
        if any(not row["cgroup_observed"] for row in self.baseline.values()):
            self.summary["cli_observation_complete"] = False
        self.summary["cli_sessions_preserved"] = self.summary["cli_observation_complete"]
        for row in self.baseline.values():
            self._event("cli_before_switch_session", observation_status="same_process", **row)
        self._event("cli_before_switch", **self.summary)
        return self

    def checkpoint(self, stage):
        for pid, before in self.baseline.items():
            row = {"pid": pid, "start_ticks": before["start_ticks"],
                   "cli_kind": before["cli_kind"], "exit_status_available": False}
            try:
                current = _observe_process(self.proc_root / str(pid))
                if current["start_ticks"] != before["start_ticks"]:
                    status = "identity_changed"
                    row["observed_start_ticks"] = current["start_ticks"]
                else:
                    row.update(current)
                    status = "exited" if current["process_state"] in {"zombie", "dead"} else "same_process"
                    if not current["cgroup_observed"]:
                        self.summary["cli_observation_complete"] = False
            except _ProcessIdentityChanged as changed:
                status = "identity_changed"
                row["observed_start_ticks"] = changed.observed_start_ticks
            except FileNotFoundError:
                status = "missing" if _process_directory_missing(self.proc_root / str(pid)) else "unavailable"
                if status == "unavailable":
                    self.summary["cli_observation_complete"] = False
            except (OSError, ValueError):
                status = "unavailable"
                self.summary["cli_observation_complete"] = False
            if status in {"missing", "identity_changed", "exited"}:
                self.changed.add(pid)
            self._event(stage + "_session", observation_status=status, **row)
        self.summary["cli_changed_count"] = len(self.changed)
        self.summary["cli_sessions_preserved"] = (not self.changed
                                                   and self.summary["cli_observation_complete"])
        self._event(stage, **self.summary)

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.checkpoint("cli_after_switches")
        except RuntimeError as error:
            if exc is None or safe_error(error) not in _RECEIPT_FAILURES:
                raise
            exc.receipt_write_failed = True
        return False


def _group_is_active(pid):
    for folder in Path("/proc").iterdir():
        if not folder.name.isdecimal():
            continue
        try:
            fields = (folder / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[2:4] == [str(pid), str(pid)] and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def _stop_created_group(process):
    # Keep our created session leader unreaped with WNOWAIT until all cleanup
    # signals have been sent. Its PID/PGID cannot be reused during cleanup.
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 1.5
        while _group_is_active(process.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _group_is_active(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=2)


def _wait_unreaped(process, deadline):
    while True:
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is not None:
            return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
        if time.monotonic() >= deadline:
            raise RuntimeError("ROLLOUT_COMMAND_TIMEOUT")
        time.sleep(0.01)


def _node_test_inputs(argv):
    """Return safe basenames keyed by the exact Node --test input bytes."""
    try:
        first_input = argv.index("--test") + 1
    except (AttributeError, ValueError):
        return {}
    inputs = {}
    for value in argv[first_input:]:
        if not isinstance(value, str) or value.startswith("-"):
            continue
        name = Path(value).name
        encoded = os.fsencode(value)
        if _NODE_TEST_FILE.fullmatch(name) and len(encoded) <= 480:
            inputs[encoded] = name
    return inputs


def _node_location_input(line, inputs, *, tap):
    """Match a Node failure location only to an exact, reviewed argv input."""
    if tap:
        prefix, suffix = b"  location: '", b"'"
    else:
        prefix, suffix = b"test at ", b""
    if not line.startswith(prefix) or (suffix and not line.endswith(suffix)):
        return None
    location = line[len(prefix):len(line) - len(suffix) if suffix else None]
    for encoded, name in inputs.items():
        if location.startswith(encoded):
            remainder = location[len(encoded):]
            if _NODE_LOCATION_SUFFIX.fullmatch(remainder):
                return name
    return None


def _unittest_failure_id(line):
    match = _UNITTEST_FAILURE.fullmatch(line)
    if not match:
        return None
    method = match.group(1)
    identifier = match.group(2)
    if identifier.rsplit(b".", 1)[-1] != method:
        identifier += b"." + method
    return identifier.decode("ascii")


def run_command(argv, *, stage, timeout, cwd=None, capture=False,
                max_output=4 * 1024 * 1024, receipt=None):
    """Drain both pipes with a size/time bound; persist only safe metadata."""
    if not _LABEL.fullmatch(stage) or timeout <= 0 or max_output <= 0:
        raise ValueError("Invalid bounded rollout command")
    started = time.monotonic()
    if receipt and not receipt.write_failed:
        receipt.event(stage, "started")
    process = None
    output = bytearray()
    sizes = {"stdout": 0, "stderr": 0}
    hashes = {name: hashlib.sha256() for name in sizes}
    tails = {name: b"" for name in sizes}
    output_code = None
    test_count = None
    failed_tests = set()
    unittest_tail = b""
    unittest_line_oversize = False
    node_inputs = _node_test_inputs(argv) if stage == _NODE_TEST_STAGE else {}
    failed_test_files = set()
    node_tail = b""
    node_line_oversize = False
    node_tap_failure = False
    node_spec_failures = False
    code = None
    original_error = None
    with selectors.DefaultSelector() as selector:
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            for name in sizes:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            deadline = started + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("ROLLOUT_COMMAND_TIMEOUT")
                for key, _ in selector.select(min(remaining, 0.2)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    sizes[name] += len(chunk)
                    hashes[name].update(chunk)
                    # Only a fixed category survives; neither matching text nor
                    # arbitrary nearby output is persisted.
                    probe = tails[name] + chunk
                    if stage in _UNITTEST_STAGES and name == "stderr":
                        summary = re.search(rb"Ran ([0-9]{1,6}) tests? in ", probe)
                        if summary:
                            test_count = int(summary.group(1))
                        unittest_tail += chunk
                        while b"\n" in unittest_tail:
                            line, unittest_tail = unittest_tail.split(b"\n", 1)
                            if not unittest_line_oversize and len(line) <= 512:
                                failed_test = _unittest_failure_id(line)
                                if failed_test and len(failed_tests) < 32:
                                    failed_tests.add(failed_test)
                            unittest_line_oversize = False
                        if len(unittest_tail) > 512:
                            unittest_tail = b""
                            unittest_line_oversize = True
                    if node_inputs and name == "stdout":
                        node_tail += chunk
                        while b"\n" in node_tail:
                            line, node_tail = node_tail.split(b"\n", 1)
                            if not node_line_oversize and len(line) <= 512:
                                if _NODE_TAP_NOT_OK.fullmatch(line):
                                    node_tap_failure = True
                                elif line == b"  ...":
                                    node_tap_failure = False
                                elif line == b"\xe2\x9c\x96 failing tests:":
                                    node_spec_failures = True
                                elif node_tap_failure or node_spec_failures:
                                    failed_input = _node_location_input(
                                        line, node_inputs, tap=node_tap_failure)
                                    if failed_input and len(failed_test_files) < 32:
                                        failed_test_files.add(failed_input)
                            node_line_oversize = False
                        if len(node_tail) > 512:
                            node_tail = b""
                            node_line_oversize = True
                    for marker, category in _OUTPUT_ERRORS:
                        if marker in probe:
                            output_code = category
                            break
                    tails[name] = probe[-128:]
                    if sum(sizes.values()) > max_output:
                        raise RuntimeError("ROLLOUT_OUTPUT_TOO_LARGE")
                    if capture and name == "stdout":
                        output.extend(chunk)
            returncode = _wait_unreaped(process, deadline)
            if (stage in _UNITTEST_STAGES
                    and unittest_tail and not unittest_line_oversize):
                failed_test = _unittest_failure_id(unittest_tail)
                if failed_test and len(failed_tests) < 32:
                    failed_tests.add(failed_test)
            if node_inputs and node_tail and not node_line_oversize:
                failed_input = _node_location_input(
                    node_tail, node_inputs, tap=node_tap_failure)
                if failed_input and len(failed_test_files) < 32:
                    failed_test_files.add(failed_input)
            return subprocess.CompletedProcess(stage, returncode, bytes(output), b"")
        except BaseException as exc:
            original_error = exc
            code = safe_error(exc)
            raise
        finally:
            if process is not None:
                _stop_created_group(process)
                process.stdout.close()
                process.stderr.close()
            if receipt and not receipt.write_failed:
                details = {"duration_ms": int((time.monotonic() - started) * 1000),
                           **{name + "_bytes": sizes[name] for name in sizes},
                           **{name + "_sha256": hashes[name].hexdigest() for name in hashes}}
                if process is not None and process.returncode is not None:
                    details["returncode"] = process.returncode
                if code:
                    details["code"] = code
                if output_code and process is not None and process.returncode != 0:
                    details["output_code"] = output_code
                if test_count is not None:
                    details["test_count"] = test_count
                if (failed_tests and process is not None
                        and process.returncode is not None and process.returncode != 0):
                    details["failed_tests"] = sorted(failed_tests)
                if (failed_test_files and process is not None
                        and process.returncode is not None and process.returncode != 0):
                    details["failed_test_files"] = sorted(failed_test_files)
                failed = code is not None or (process is not None and process.returncode != 0)
                try:
                    receipt.event(stage, "failed" if failed else "completed", **details)
                except RuntimeError as log_error:
                    if original_error is None or safe_error(log_error) not in _RECEIPT_FAILURES:
                        raise
                    original_error.receipt_write_failed = True
