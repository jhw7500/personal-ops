"""Explicit Codex host-user execution under an owner grant; never isolation proof."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat

from agent_events import MAX_LINE, parse_codex_event
from agent_profiles import AgentProfileRegistry, code_mode_host_digest, profile_digest
from agent_runner import RESULT_SCHEMA
from agent_tasks import REASONS, REQUEST_ID, _safe_structured_result
from host_execution import _safe_task_label
from host_runtime_config import HostRuntimeError, private_dir, private_file
from host_runtime_operations import _lock, utc_now

MAX_BINARY = 512 * 1024 * 1024
MAX_LOG = 1024 * 1024


def compile_host_codex(profile, binary: Path, schema: Path) -> list[str]:
    """Dedicated 0.162.1 exec, owner-granted host access, pinned model/effort."""
    if profile.get("provider") != "codex" or profile.get("version") != "0.162.1":
        raise HostRuntimeError("UNSUPPORTED_AGENT_PROFILE")
    return [str(binary), "--no-daemon", "-c",
            f'model_reasoning_effort="{profile["reasoning_effort"]}"',
            "--model", profile["model"], "--dangerously-bypass-approvals-and-sandbox",
            "exec", "--ignore-user-config", "--strict-config", "--ephemeral",
            "--json", "--skip-git-repo-check", "--output-schema", str(schema), "-"]


def _digest_file(path: Path) -> str:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            or not 0 < info.st_size <= MAX_BINARY):
        raise HostRuntimeError("EXECUTABLE_UNVERIFIED")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        def metadata(value):
            return tuple(getattr(value, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns"))
        if metadata(before) != metadata(info):
            raise HostRuntimeError("EXECUTABLE_CHANGED")
        digest = hashlib.sha256()
        count = 0
        while block := os.read(fd, 1024 * 1024):
            count += len(block)
            if count > MAX_BINARY:
                raise HostRuntimeError("EXECUTABLE_CHANGED")
            digest.update(block)
        if count != before.st_size or metadata(os.fstat(fd)) != metadata(before):
            raise HostRuntimeError("EXECUTABLE_CHANGED")
        return digest.hexdigest()
    finally:
        os.close(fd)


class HostAgentAdapter:
    def __init__(self, execution, *, profiles=None, event_sink=None):
        self.execution = execution
        self.profiles = profiles or AgentProfileRegistry(execution.projects)
        self.event_sink = event_sink
        self._observed = {}

    def available(self, project):
        grant = self.execution.access.get(project)
        return grant.get("active") is True and grant.get("allow_codex") is True

    def owns(self, project, task_id):
        return self.execution.owns(project, task_id, kind="codex")

    def decorate_profiles(self, project, result):
        if not self.available(project):
            return result
        profiles = []
        for row in result["profiles"]:
            reason = None
            try:
                profile = self._profile(project, row["profile_id"])
                if profile["version"] != "0.162.1":
                    raise HostRuntimeError("UNSUPPORTED_AGENT_PROFILE")
            except (HostRuntimeError, OSError) as exc:
                reason = getattr(exc, "code", "EXECUTABLE_UNVERIFIED")
            profiles.append({**row, "host_ready": reason is None, "host_reason": reason,
                             "host_execution_mode": "host", "filesystem_isolated": False,
                             "host_per_task_approval_required": False})
        return {**result, "profiles": profiles}

    def _profile(self, project, identifier):
        profile = self.profiles._profiles().get(identifier)
        if profile is None or profile["project"] != project:
            raise HostRuntimeError("PROFILE_NOT_FOUND")
        if not profile["enabled"]:
            raise HostRuntimeError("PROFILE_DISABLED")
        # Host execution deliberately does not assert sandbox readiness or CAS.
        # It is separately authorized full host-user access, visible in results.
        binary = Path(profile["executable"])
        if any(parent.is_symlink() for parent in (binary, *binary.parents)):
            raise HostRuntimeError("EXECUTABLE_UNVERIFIED")
        if _digest_file(binary) != profile["executable_sha256"]:
            raise HostRuntimeError("EXECUTABLE_CHANGED")
        return profile

    def _stage_runtime(self, profile):
        source = Path(profile["executable"])
        helper_hash = code_mode_host_digest(profile)
        sources = {"codex": (source, profile["executable_sha256"]),
                   "codex-linux-sandbox": (source.parent / "codex-linux-sandbox", profile["executable_sha256"]),
                   "codex-code-mode-host": (source.parent / "codex-code-mode-host", helper_hash)}
        root = private_dir(self.execution.state_root / "host-agent-runtimes")
        identity = hashlib.sha256((profile_digest(profile) + helper_hash).encode()).hexdigest()
        with _lock(root / "stage.lock"):
            target = private_dir(root / identity)
            for name, (original, expected) in sources.items():
                destination = target / name
                if destination.exists() or destination.is_symlink():
                    if stat.S_IMODE(destination.lstat().st_mode) != 0o500 or _digest_file(destination) != expected:
                        raise HostRuntimeError("EXECUTABLE_CHANGED")
                    continue
                if _digest_file(original) != expected:
                    raise HostRuntimeError("EXECUTABLE_CHANGED")
                src_fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
                dst_fd = None
                try:
                    dst_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500)
                    before = os.fstat(src_fd)
                    digest = hashlib.sha256()
                    count = 0
                    while block := os.read(src_fd, 1024 * 1024):
                        count += len(block)
                        if count > MAX_BINARY:
                            raise HostRuntimeError("EXECUTABLE_CHANGED")
                        digest.update(block)
                        view = memoryview(block)
                        while view:
                            written = os.write(dst_fd, view)
                            if written <= 0:
                                raise OSError("short write")
                            view = view[written:]
                    after = os.fstat(src_fd)
                    if ((after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                            != (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            or digest.hexdigest() != expected):
                        raise HostRuntimeError("EXECUTABLE_CHANGED")
                    os.fchmod(dst_fd, 0o500)
                    os.fsync(dst_fd)
                except BaseException:
                    if dst_fd is not None:
                        destination.unlink(missing_ok=True)
                    raise
                finally:
                    os.close(src_fd)
                    if dst_fd is not None:
                        os.close(dst_fd)
            return target / "codex"

    def _schema(self):
        root = private_dir(self.execution.state_root / "host-agent-runtimes")
        path = root / "result-schema.json"
        with _lock(root / "stage.lock"):
            if path.exists() or path.is_symlink():
                private_file(path)
                if path.read_bytes() != RESULT_SCHEMA:
                    raise HostRuntimeError("AGENT_CONTROL_UNAVAILABLE")
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(RESULT_SCHEMA)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), 0o600)
        return path

    def prepare(self, project, *, profile_id, objective, reason, request_id, timeout_minutes=30,
                task_label=None):
        if (not isinstance(objective, str) or not 10 <= len(objective) <= 8192
                or any(ord(c) < 32 and c not in "\n\t" for c in objective)
                or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)
                or reason not in REASONS or type(timeout_minutes) is not int
                or not 1 <= timeout_minutes <= 60):
            raise HostRuntimeError("INVALID_AGENT_TASK")
        task_label = _safe_task_label(task_label, error_code="INVALID_AGENT_TASK")
        self.execution.access.require(project, codex=True)
        profile = self._profile(project, profile_id)
        binary = self._stage_runtime(profile)
        argv = compile_host_codex(profile, binary, self._schema())
        # stdin carries objectives, never a shell-interpolated command or argv.
        prompt = ("Work in the selected real host checkout using the owner's active host grant. "
                  "Preserve unrelated WIP and existing Codex/Claude sessions. Follow project instructions. "
                  "Do not claim tests you did not run. A model report is not independent verification. "
                  "Do not start Yocto/BitBake builds without their separately required explicit approval.\n\n"
                  + objective)
        metadata = {"profile_id": profile_id, "model": profile["model"],
                    "reasoning_effort": profile["reasoning_effort"],
                    "profile_digest": profile_digest(profile), "reason": reason}
        result = self.execution.prepare(
            project, executable=argv[0], argv=argv[1:], cwd=".", request_id=request_id,
            timeout_seconds=timeout_minutes * 60, input_text=prompt, agent_metadata=metadata,
            task_label=task_label)
        return self._public(result)

    @staticmethod
    def _public(result):
        metadata = result.get("agent_metadata") or {}
        return {**result, "task_id": result["operation_id"], "provider": "codex",
                "profile_id": metadata.get("profile_id"), "model": metadata.get("model"),
                "reasoning_effort": metadata.get("reasoning_effort"),
                "execution_mode": "host", "filesystem_isolated": False,
                "approval_required": False, "model_usage_estimated": False}

    async def start(self, project, task_id):
        prepared = self.execution.get(project, task_id)
        metadata = prepared.get("agent_metadata") or {}
        if prepared["state"] == "prepared":
            profile = await asyncio.to_thread(self._profile, project, metadata["profile_id"])
            if profile_digest(profile) != metadata["profile_digest"]:
                raise HostRuntimeError("STALE_PROFILE")
        result = self._public(await self.execution.execute(project, task_id))
        self._telemetry(result)
        return result

    def _telemetry(self, result):
        if self.event_sink is None:
            return
        state = {"queued": "queued", "running": "running", "cancelling": "canceling",
                 "succeeded": "succeeded", "failed": "failed", "cancelled": "canceled",
                 "outcome_unknown": "unknown"}.get(result.get("state"))
        if state is None:
            return
        usage = result.get("provider_usage", {})
        marker = (state, json.dumps(usage, sort_keys=True))
        if self._observed.get(result["task_id"]) == marker:
            return
        self._observed[result["task_id"]] = marker
        context = result.get("work_context") or {}
        task_label = context.get("task_label")
        event = {"schema_version": 1, "source_id": "personal-ops-agent", "source_kind": "codex-cli",
                 "actor_id": result["profile_id"], "actor_kind": "agent", "actor_label": "Codex host agent",
                 "task_id": result["task_id"], "task_kind": "agent",
                 "task_label": task_label if isinstance(task_label, str)
                 else "Host Codex task " + result["task_id"][:8],
                 "action": "code", "state": state, "time": utc_now(), "model": result["model"],
                 "model_source": "configuration", "reasoning_effort": result["reasoning_effort"],
                 "reasoning_effort_source": "configuration"}
        mapped = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if key in usage}
        if "reasoning_output_tokens" in usage:
            mapped["reasoning_tokens"] = usage["reasoning_output_tokens"]
        if mapped:
            event.update(usage=mapped, usage_scope="turn", usage_estimated=False)
        try:
            self.event_sink(result["project"], event)
        except (OSError, TypeError, ValueError):
            pass

    def get(self, project, task_id):
        from host_execution import _redact
        result = self._public(self.execution.get(project, task_id))
        if hasattr(self.execution, "raw_log_path"):
            # Internal private read only. Public host-log tools restrict Codex JSONL,
            # which can contain reasoning. Only allowlisted metadata/report escapes.
            path = self.execution.raw_log_path(project, task_id, "stdout")
            if path.exists():
                private_file(path)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    text = os.read(fd, MAX_LOG).decode('utf-8', 'replace')
                finally:
                    os.close(fd)
            else:
                text = ""
        else:
            cursor = 0
            text = ""
            while cursor < MAX_LOG:
                page = self.execution.read_log(project, task_id, stream="stdout", cursor=cursor, max_bytes=16384)
                text += page.get("text", page.get("content", ""))
                following = page.get("next_cursor", cursor)
                if following <= cursor or page.get("eof") is True:
                    break
                cursor = following
        events = []
        usage = {}
        report = None
        # Split only complete JSONL records; unfinished stream tails are ignored.
        for line in text.splitlines():
            if len(line.encode()) > MAX_LINE:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            event = parse_codex_event(value)
            if event is not None:
                events.append(event)
                if "usage" in event:
                    usage = event["usage"]
            if isinstance(value, dict) and value.get("type") == "item.completed":
                item = value.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    try:
                        report = _safe_structured_result(json.loads(item.get("text", "")))
                    except (ValueError, TypeError):
                        pass
        result.update(events=events[-100:], provider_usage=usage)
        if report is not None:
            # Mask returned strings after parsing; masking raw JSON can corrupt
            # compact provider records and silently drop the final report.
            for name, value in report.items():
                if isinstance(value, str):
                    report[name] = _redact(value.encode())
                elif isinstance(value, list):
                    report[name] = [_redact(item.encode()) for item in value]
            result["model_report"] = report
        self._telemetry(result)
        return result

    async def cancel(self, project, task_id):
        result = self._public(await asyncio.to_thread(self.execution.cancel, project, task_id))
        self._telemetry(result)
        return result
