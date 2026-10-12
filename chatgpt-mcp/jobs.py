"""Bounded, persistent jobs for isolated repository commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import uuid

from execution import ExecutionPolicy


MAX_LOG_BYTES = 1024 * 1024
MAX_READ_BYTES = 16384
MAX_HISTORY = 1000
MAX_ACTIVE = 4
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
ACTIVE_STATES = {"queued", "running", "canceling"}
READY_MARKER = b"\x1eMCP_JOB_READY\x1e"


def now():
    return datetime.now(timezone.utc).isoformat()


def private_directory(path):
    """Reject symlinks before creating state; never chmod another user's files."""
    path = Path(path).absolute()
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink():
            raise ValueError("State directory cannot contain symbolic links")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("State directory must be owned by the current user")
    path.chmod(0o700)
    return path


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("Job state must be a user-owned regular file with mode 0600")


class JobManager:
    def __init__(self, projects, policy: ExecutionPolicy, state_dir: Path, locks=None):
        state_dir = Path(state_dir).absolute()
        if any(state_dir.resolve().is_relative_to(root.resolve()) for root in projects.values()):
            raise ValueError("Job state must be outside writable projects")
        self.state_dir = private_directory(state_dir)
        self.projects = projects
        self.policy = policy
        self.locks = locks or {name: asyncio.Lock() for name in projects}
        self.jobs = {}
        self.tasks = {}
        self.processes = {}
        self.preparing = 0
        self.closed = False
        for path in self.state_dir.glob("*.json"):
            private_file(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            if not JOB_ID.fullmatch(path.stem) or record.get("job_id") != path.stem:
                raise ValueError("Invalid job state")
            if record["state"] in ACTIVE_STATES:
                record.update(state="interrupted", finished_at=now(), exit_code=None,
                              error="Server restarted; completion could not be confirmed")
                self._save(record)
            self.jobs[path.stem] = record

    def _save(self, record):
        target = self.state_dir / (record["job_id"] + ".json")
        if target.exists() or target.is_symlink():
            private_file(target)
        temporary = self.state_dir / ("." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _get(self, project, job_id):
        if project not in self.projects:
            raise ValueError("An allowed project is required")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        record = self.jobs.get(job_id)
        if record is None or record["project"] != project:
            raise ValueError("Job not found in the selected project")
        return record

    @staticmethod
    def _public(record):
        return {key: value for key, value in record.items() if key != "fingerprint"}

    async def start(self, project, *, request_id, argv=None, preset=None,
                    relative_cwd=".", timeout_seconds=None):
        if self.closed:
            raise ValueError("Job manager is shutting down")
        if project not in self.projects:
            raise ValueError("An allowed project is required")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("A unique request_id (1-80 simple characters) is required")
        fingerprint = hashlib.sha256(json.dumps(
            [argv, preset, relative_cwd, timeout_seconds], sort_keys=True).encode()).hexdigest()
        for record in self.jobs.values():
            if record["project"] == project and record["request_id"] == request_id:
                if record["fingerprint"] != fingerprint:
                    raise ValueError("request_id was already used for a different command")
                return self._public(record)
        if len(self.jobs) >= MAX_HISTORY:
            raise ValueError("Job history capacity reached; host maintenance is required")
        if len(self.tasks) + self.preparing >= MAX_ACTIVE or self.locks[project].locked():
            raise ValueError("PROJECT_BUSY: wait for the current operation before starting another")
        # Own this project's mutation lane before preparing or launching a command.
        await self.locks[project].acquire()
        self.preparing += 1
        try:
            spec = await asyncio.to_thread(
                self.policy.prepare, project, argv=argv, preset=preset,
                relative_cwd=relative_cwd, timeout_seconds=timeout_seconds)
            if self.closed:
                raise ValueError("Job manager is shutting down")
            job_id = uuid.uuid4().hex
            record = {
                "job_id": job_id, "request_id": request_id, "fingerprint": fingerprint,
                "project": project, "state": "queued", "argv": spec.argv,
                "preset": preset, "relative_cwd": spec.relative_cwd, "label": spec.label,
                "timeout_seconds": spec.timeout_seconds, "created_at": now(),
                "started_at": None, "finished_at": None, "exit_code": None,
                "log_bytes": 0, "log_truncated": False,
            }
            self._save(record)
            self.jobs[job_id] = record
            self.tasks[job_id] = asyncio.create_task(self._run(record, spec))
            return self._public(record)
        except BaseException:
            self.locks[project].release()
            raise
        finally:
            self.preparing -= 1

    async def _stop(self, process):
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _run(self, record, spec):
        process = None
        reader = None
        job_id = record["job_id"]
        try:
            log_path = self.state_dir / (job_id + ".log")
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", buffering=0) as log:
                if record["state"] == "canceling":
                    record["state"] = "canceled"
                    return
                # Wait for a shell inside the fully initialized PID namespace
                # before signaling bwrap. Killing its launcher during setup can
                # otherwise leave a child holding the output pipe open.
                command = [*spec.command[:-len(spec.execution_argv)], "/usr/bin/bash", "-c",
                           "printf '\\036MCP_JOB_READY\\036'; exec \"$@\"", "--",
                           *spec.execution_argv]
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=spec.cwd, env=spec.env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                try:
                    marker = await asyncio.wait_for(process.stdout.readexactly(len(READY_MARKER)), 5)
                except asyncio.IncompleteReadError as exc:
                    log.write(exc.partial[:MAX_LOG_BYTES])
                    record["log_bytes"] = min(len(exc.partial), MAX_LOG_BYTES)
                    raise ValueError("Sandbox failed before starting the command; inspect the job log") from exc
                if marker != READY_MARKER:
                    log.write(marker)
                    record["log_bytes"] = len(marker)
                    raise ValueError("Sandbox did not confirm command readiness")
                self.processes[job_id] = process
                record.update(started_at=now())
                if record["state"] != "canceling":
                    record["state"] = "running"
                self._save(record)

                async def consume():
                    while chunk := await process.stdout.read(8192):
                        remaining = MAX_LOG_BYTES - record["log_bytes"]
                        if remaining > 0:
                            log.write(chunk[:remaining])
                            record["log_bytes"] += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            record["log_truncated"] = True

                reader = asyncio.create_task(consume())
                if record["state"] == "canceling":
                    await self._stop(process)
                try:
                    await asyncio.wait_for(process.wait(), spec.timeout_seconds)
                    record["state"] = ("canceled" if record["state"] == "canceling" else
                                       "succeeded" if process.returncode == 0 else "failed")
                except asyncio.TimeoutError:
                    record["state"] = "timed_out"
                    await self._stop(process)
                # Background children may still hold the pipe. Their entire PID
                # namespace is torn down when the bwrap process finishes.
                await asyncio.wait_for(reader, 5)
                record["exit_code"] = process.returncode
        except asyncio.CancelledError:
            if process is not None:
                await self._stop(process)
            record["state"] = "interrupted"
            record["error"] = "Server stopped while the job was active"
        except Exception as exc:
            if process is not None:
                await self._stop(process)
            record.update(state="failed", error=str(exc)[:1000])
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            record["finished_at"] = now()
            self._save(record)
            self.processes.pop(job_id, None)
            self.tasks.pop(job_id, None)
            self.locks[record["project"]].release()

    def get(self, project, job_id):
        return self._public(self._get(project, job_id))

    def list(self, project, limit=20):
        if project not in self.projects:
            raise ValueError("An allowed project is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        records = sorted((record for record in self.jobs.values() if record["project"] == project),
                         key=lambda record: record["created_at"], reverse=True)
        return {"jobs": [self._public(record) for record in records[:limit]]}

    def read_log(self, project, job_id, offset=0, max_bytes=MAX_READ_BYTES):
        record = self._get(project, job_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative byte cursor")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 1 <= max_bytes <= MAX_READ_BYTES):
            raise ValueError(f"max_bytes must be 1-{MAX_READ_BYTES}")
        path = self.state_dir / (job_id + ".log")
        chunk = b""
        if path.exists() or path.is_symlink():
            private_file(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                stream.seek(offset)
                chunk = stream.read(max_bytes)
        return {"project": project, "job_id": job_id, "state": record["state"],
                "offset": offset, "next_offset": offset + len(chunk),
                "text": chunk.decode("utf-8", errors="replace"),
                "log_truncated": record["log_truncated"],
                "complete": record["state"] not in ACTIVE_STATES,
                "has_more": offset + len(chunk) < record["log_bytes"]}

    async def cancel(self, project, job_id):
        record = self._get(project, job_id)
        if record["state"] not in ACTIVE_STATES:
            return self._public(record)
        record["state"] = "canceling"
        self._save(record)
        if process := self.processes.get(job_id):
            await self._stop(process)
        task = self.tasks.get(job_id)
        if task:
            await asyncio.shield(task)
        return self._public(record)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for job_id in self.tasks:
            record = self.jobs[job_id]
            record.update(state="canceling", error="Server stopped while the job was active")
        await asyncio.gather(*(self._stop(process) for process in list(self.processes.values())))
        await asyncio.gather(*tasks, return_exceptions=True)
