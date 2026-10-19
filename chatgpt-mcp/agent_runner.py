"""Dedicated Codex CLI runner. Never starts an inherited user session.

Only callable after the AgentTaskManager has consumed a real approval receipt.
A verified real-host permission-isolation and smoke receipt are mandatory.
This module has no host Docker/SQL/shell escalation adapter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import stat

from agent_bootstrap import validate_bootstrap
from agent_events import MAX_LINE, parse_codex_event
from agent_profiles import codex_permission_policy, sandbox_helper
from host_runtime_config import HostRuntimeError

MAX_EVENTS = 5000
MAX_EXEC_SECONDS = 3600
MAX_STDERR = 64 * 1024
START_TOTAL_SECONDS = 20

RESULT_SCHEMA = json.dumps({"type": "object", "properties": {
    "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
    "changed_files": {"type": "array", "maxItems": 100,
                      "items": {"type": "string", "maxLength": 512}},
    "validations": {"type": "array", "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200}},
    "remaining": {"type": "array", "maxItems": 50,
                  "items": {"type": "string", "minLength": 1, "maxLength": 500}},
    "approval_required": {"type": "boolean"}},
    "required": ["summary", "changed_files", "validations", "remaining", "approval_required"],
    "additionalProperties": False}).encode() + b"\n"


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o600)


def _prepare_control(workspace: Path) -> Path:
    control = workspace / ".agent-control"
    try:
        os.mkdir(control, 0o700)
    except OSError as exc:
        raise HostRuntimeError("AGENT_CONTROL_UNAVAILABLE") from exc
    info = control.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HostRuntimeError("AGENT_CONTROL_UNAVAILABLE")
    os.chmod(control, 0o700)
    try:
        _write_private(control / "result-schema.json", RESULT_SCHEMA)
    except OSError as exc:
        raise HostRuntimeError("AGENT_CONTROL_UNAVAILABLE") from exc
    return control


def _process_start_ticks(pid: int) -> int:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        return int(raw.rsplit(") ", 1)[1].split()[19])
    except (OSError, ValueError, IndexError) as exc:
        raise HostRuntimeError("AGENT_START_FAILED") from exc


async def _reap_unidentified_owned_group(process) -> None:
    """Spawn succeeded but /proc identity capture failed before the child was reaped.

    The unreaped asyncio child still owns this PID generation and start_new_session
    made PGID equal PID, so killing this exact group cannot target a reused process.
    """
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED") from None


def compile_codex_profile(profile, workspace: Path) -> list[str]:
    """Fixed Codex 0.162.x beta named-permissions argv; no legacy --sandbox."""
    workspace = Path(workspace).resolve(strict=True)
    if (profile.get("provider") != "codex"
            or profile.get("host_capabilities") != []
            or profile.get("apply_policy") != "review_only"
            or not workspace.is_dir()):
        raise HostRuntimeError("UNSUPPORTED_AGENT_PROFILE")
    # Exact 0.162.1 syntax proven by the offline boundary probe. Quoted special
    # filesystem keys must remain inside one inline table; dotted overrides fail.
    rules = [
        'default_permissions="delegated"',
        codex_permission_policy(profile),
        'features.network_proxy=false',
        f'model_reasoning_effort="{profile["reasoning_effort"]}"',
    ]
    control = workspace / ".agent-control"
    return ([profile["executable"], "--no-daemon"]
            + [arg for setting in rules for arg in ("-c", setting)]
            + ["--model", profile["model"], "--ask-for-approval", "never",
               "exec", "--ignore-user-config", "--strict-config", "--ephemeral",
               "--json", "--skip-git-repo-check",
               "--output-schema", str(control / "result-schema.json"),
               "--output-last-message", str(control / "last-message.json"),
               "-C", str(workspace), "-"])


class CodexAgentRunner:
    def __init__(self, *, supervisor=None):
        from agent_supervisor import SystemdAgentSupervisor
        self.supervisor = supervisor or SystemdAgentSupervisor()
        self.processes = {}
        self.process_start_ticks = {}
        self.identities = {}
        self.callbacks = {}
        self.launches = {}
        self.cancelled = set()
        self.termination = {}

    def set_task_callbacks(self, task_id, *, on_identity=None, on_event=None):
        self.callbacks[task_id] = (on_identity, on_event)

    async def _emit(self, task_id, index, payload):
        import inspect
        callback = self.callbacks.get(task_id, (None, None))[index]
        if callback is not None:
            result = callback(payload)
            if inspect.isawaitable(result):
                await result

    def _release_terminated(self, task_id, identity):
        if self.termination.get(task_id) is not True:
            return
        self.supervisor.forget(identity)
        self.processes.pop(task_id, None)
        self.identities.pop(task_id, None)
        self.process_start_ticks.pop(task_id, None)
        launch = self.launches.get(task_id)
        if launch is None or launch.done():
            self.launches.pop(task_id, None)
        self.callbacks.pop(task_id, None)

    @staticmethod
    def ready(profile):
        executable = Path(profile.get("executable", ""))
        if (profile.get("provider") != "codex" or not profile.get("enabled")
                or not profile.get("isolation_receipt") or not profile.get("smoke_receipt")
                or not executable.is_file() or executable.is_symlink()):
            return False
        try:
            return hashlib.sha256(executable.read_bytes()).hexdigest() == profile["executable_sha256"]
        except OSError:
            return False

    async def run(self, task, workspace, profile):
        if not self.ready(profile):
            raise HostRuntimeError("ISOLATION_UNVERIFIED")
        return await self._run_owned(task, workspace, profile)

    async def run_approved_smoke(self, profile, workspace, approval_path, *,
                               plan_digest, on_identity=None):
        from agent_smoke import consume_smoke_authority
        task = consume_smoke_authority(profile, workspace, approval_path, plan_digest=plan_digest)
        self.set_task_callbacks(task['task_id'], on_identity=on_identity)
        return await self._run_owned(task, workspace, profile)

    async def _run_owned(self, task, workspace, profile):
        import math
        ident = task["task_id"]
        if ident in self.launches or ident in self.processes:
            raise HostRuntimeError("AGENT_ALREADY_RUNNING")
        control = _prepare_control(Path(workspace))
        argv = compile_codex_profile(profile, workspace)
        bootstrap = Path(argv[0]).parent / "personal-ops-agent-bootstrap"
        bootstrap_sha256 = validate_bootstrap(bootstrap)
        safe_keys = ("HOME", "CODEX_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE")
        env = {name: os.environ[name] for name in safe_keys if name in os.environ}
        env["PATH"] = str(sandbox_helper(profile).parent) + ":/usr/local/bin:/usr/bin:/bin"
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        timeout = min(MAX_EXEC_SECONDS, task["timeout_minutes"] * 60)
        process = identity = None
        streams = []
        stderr = bytearray()
        events = []
        complete = False
        async def bounded_start():
            return await asyncio.wait_for(
                self.supervisor.start(
                    ident, argv, workspace, env, max(1, math.ceil(timeout)),
                    expected_bootstrap_sha256=bootstrap_sha256),
                timeout=START_TOTAL_SECONDS)

        launch = asyncio.create_task(bounded_start())
        self.launches[ident] = launch
        try:
            # Do not lose a native child if this coroutine is interrupted while
            # systemd starts it. The bootstrap cannot execute before GO below.
            process, identity = await asyncio.shield(launch)
            self.processes[ident] = process
            self.identities[ident] = identity
            self.process_start_ticks[ident] = _process_start_ticks(process.pid)
            if self.process_start_ticks[ident] != identity["wrapper_start_ticks"]:
                raise HostRuntimeError("AGENT_PROCESS_TREE_UNVERIFIED")
            await self._emit(ident, 0, identity)
            if ident in self.cancelled:
                raise asyncio.CancelledError()
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise HostRuntimeError("AGENT_START_FAILED")
            process.stdin.write(b"GO\n" + task["objective"].encode("utf8"))
            await process.stdin.drain()
            process.stdin.close()

            async def pump_stdout():
                nonlocal complete
                for _ in range(MAX_EVENTS):
                    try:
                        raw = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        raise HostRuntimeError("AGENT_EVENT_TOO_LARGE") from None
                    if not raw:
                        return
                    if len(raw) > MAX_LINE:
                        raise HostRuntimeError("AGENT_EVENT_TOO_LARGE")
                    try:
                        event = parse_codex_event(json.loads(raw))
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if event is not None:
                        complete = complete or event["code"] == "TURN_COMPLETED"
                        if len(events) < 200:
                            events.append(event)
                        await self._emit(ident, 1, event)
                raise HostRuntimeError("AGENT_EVENT_LIMIT")

            async def pump_stderr():
                while True:
                    chunk = await process.stderr.read(8192)
                    if not chunk:
                        return
                    stderr.extend(chunk)
                    del stderr[:-MAX_STDERR]

            streams = [asyncio.create_task(pump_stdout()), asyncio.create_task(pump_stderr())]
            async def lifecycle():
                await asyncio.gather(*streams)
                return await process.wait()
            try:
                exit_code = await asyncio.wait_for(lifecycle(), timeout=timeout)
            except TimeoutError:
                await self.cancel(ident)
                raise HostRuntimeError("AGENT_TIMEOUT") from None
            evidence = await self.supervisor.finish(identity)
            self.termination[ident] = evidence.get("termination_verified") is True
            structured = _read_final_result(control / "last-message.json")
            verified = exit_code == 0 and complete and structured is not None and self.termination[ident]
            return {"verified": verified, "exit_code": exit_code,
                    "code": None if verified else "AGENT_RESULT_INVALID" if structured is None else "AGENT_INCOMPLETE",
                    "events": events, "changed_files": [], "structured_result": structured,
                    "termination_verified": self.termination[ident]}
        except BaseException:
            if process is None and not launch.cancelled():
                try:
                    process, identity = await asyncio.shield(launch)
                    self.processes[ident] = process
                    self.identities[ident] = identity
                except (Exception, asyncio.CancelledError):
                    pass
            if identity is not None and self.termination.get(ident) is not True:
                self.termination[ident] = False
                try:
                    evidence = await self.supervisor.stop(identity)
                    self.termination[ident] = evidence.get("termination_verified") is True
                except Exception:
                    pass
            raise
        finally:
            for stream in streams:
                if not stream.done():
                    stream.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
            if process is not None and process.returncode is None:
                # Only the manager wrapper created by this call, after cgroup stop.
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=8)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            diagnostic = control / "stderr.log"
            if not diagnostic.exists():
                try:
                    _write_private(diagnostic, bytes(stderr))
                except OSError:
                    pass
            if identity is not None:
                self._release_terminated(ident, identity)
            elif launch.done():
                self.launches.pop(ident, None)
                self.callbacks.pop(ident, None)

    async def cancel(self, task_id):
        self.cancelled.add(task_id)
        identity = self.identities.get(task_id)
        launch = self.launches.get(task_id)
        if identity is None and launch is not None:
            try:
                process, identity = await asyncio.shield(launch)
                self.processes[task_id] = process
                self.identities[task_id] = identity
            except (Exception, asyncio.CancelledError):
                if launch.done():
                    self.launches.pop(task_id, None)
                return {"termination_verified": False}
        if identity is None:
            return {"termination_verified": self.termination.get(task_id, False)}
        self.termination[task_id] = False
        try:
            evidence = await self.supervisor.stop(identity)
        except Exception:
            return {"termination_verified": False}
        self.termination[task_id] = evidence.get("termination_verified") is True
        if self.termination[task_id]:
            self._release_terminated(task_id, identity)
        return evidence

    async def close(self):
        for task_id in set(self.launches) | set(self.processes) | set(self.identities):
            await self.cancel(task_id)


def _read_final_result(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                return None
            payload = json.loads(stream.read(65537))
        # The same parser is used for public task metadata, with explicit model
        # provenance; it never upgrades reported checks to real validation.
        from agent_tasks import _safe_structured_result
        safe = _safe_structured_result(payload)
        if safe is None:
            return None
        return {key: payload[key] for key in ("summary", "changed_files", "validations",
                                             "remaining", "approval_required")}
    except (OSError, ValueError, TypeError):
        return None
