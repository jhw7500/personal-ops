"""Bounded project continuation snapshots and memory compaction tools."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from workspace_paths import WorkspaceError


NAMES = frozenset({"get_project_resume", "compact_project_memory"})
MAX_MEMORY_EXCERPT_BYTES = 12 * 1024
MAX_STATUS_LINES = 80
MAX_STATUS_BYTES = 12 * 1024
MAX_RECENT_JOBS = 10
MAX_RECENT_EVENTS = 20


def _tool(name, title, description, properties, required, *, read_only):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": False,
        },
    }


def tool_specs(projects):
    project = {"type": "string", "enum": list(projects)}
    return [
        _tool(
            "get_project_resume",
            "프로젝트 이어가기 상태",
            "Return one bounded continuation snapshot: saved progress, bounded memory context, "
            "memory archive metadata, Git dirty fingerprint, recent local jobs/work events, "
            "and current host-access status. Read-only; saved reports remain context, not proof.",
            {"project": project},
            ("project",),
            read_only=True,
        ),
        _tool(
            "compact_project_memory",
            "프로젝트 메모리 압축 저장",
            "Archive the exact current project memory in owner-only state, then atomically replace "
            "the active memory with caller-supplied compact text using revision CAS. "
            "No automatic summarization or deletion of the archive.",
            {
                "project": project,
                "content": {"type": "string", "maxLength": 32768},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            ("project", "content", "expected_revision"),
            read_only=False,
        ),
    ]


def _bounded_excerpt(text):
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_MEMORY_EXCERPT_BYTES:
        return text, False
    marker = "\n\n… [중간 메모 생략] …\n\n"
    marker_bytes = marker.encode("utf-8")
    head_limit = 4096
    tail_limit = MAX_MEMORY_EXCERPT_BYTES - head_limit - len(marker_bytes)
    head = encoded[:head_limit].decode("utf-8", errors="ignore")
    tail = encoded[-tail_limit:].decode("utf-8", errors="ignore")
    return head + marker + tail, True


def _bounded_status_lines(text):
    lines = []
    used = 0
    truncated = False
    source = text.splitlines()
    for line in source:
        encoded = line.encode("utf-8")
        extra = len(encoded) + 1
        if len(lines) >= MAX_STATUS_LINES or used + extra > MAX_STATUS_BYTES:
            truncated = True
            break
        lines.append(line)
        used += extra
    return lines, truncated or len(lines) < len(source)


def _safe_job(record):
    fields = (
        "job_id", "state", "preset", "relative_cwd", "label", "created_at", "started_at",
        "finished_at", "exit_code", "termination_verified", "workspace_mode",
    )
    return {key: record.get(key) for key in fields if key in record}


class ProjectResumeTools:
    def __init__(self, projects, state, jobs, git, access=None):
        self.projects = projects
        self.state = state
        self.jobs = jobs
        self.git = git
        self.access = access
        self.names = NAMES

    def validate(self, name, arguments):
        if name not in self.names:
            raise ValueError("UNKNOWN_PROJECT_RESUME_TOOL")
        if not isinstance(arguments, dict):
            raise ValueError("INVALID_PROJECT_RESUME_ARGUMENTS")
        expected = ({"project"} if name == "get_project_resume"
                    else {"project", "content", "expected_revision"})
        if set(arguments) != expected or arguments.get("project") not in self.projects:
            raise ValueError("INVALID_PROJECT_RESUME_ARGUMENTS")

    def call(self, name, arguments):
        self.validate(name, arguments)
        project = arguments["project"]
        if name == "compact_project_memory":
            return self.state.compact_memory(
                project, arguments["content"], arguments["expected_revision"])
        return self._resume(project)

    def _resume(self, project):
        generated_at = datetime.now(timezone.utc).isoformat()
        progress = self.state.read_progress(project)
        memory = self.state.read_memory(project)
        excerpt, memory_truncated = _bounded_excerpt(memory["content"])
        archives = self.state.list_memory_archives(project, limit=5)["archives"]

        try:
            git_status = self.git.git_status({"project": project})
            status = git_status.get("status", "")
            all_status_lines = status.splitlines()
            status_lines, status_truncated = _bounded_status_lines(status)
            git_snapshot = {
                "available": True,
                "reason": None,
                "branch": git_status.get("branch"),
                "dirty": any(line and not line.startswith("## ") for line in all_status_lines),
                "status_sha256": git_status.get("visible_status_sha256")
                    or hashlib.sha256(status.encode("utf-8")).hexdigest(),
                "status_hash_scope": git_status.get("fingerprint_scope", "returned-status-only"),
                "worktree_content_sha256": git_status.get("worktree_content_sha256"),
                "content_fingerprint_complete": git_status.get("content_fingerprint_complete"),
                "visible_paths": git_status.get("visible_paths"),
                "excluded_paths": git_status.get("excluded_paths"),
                "status_lines": status_lines,
                "status_truncated": status_truncated or bool(git_status.get("truncated", False)),
                "omitted_paths": git_status.get("omitted_paths", 0),
            }
        except (ValueError, OSError, WorkspaceError):
            git_snapshot = {
                "available": False,
                "reason": "UNAVAILABLE",
                "branch": None,
                "dirty": None,
                "status_sha256": None,
                "status_lines": [],
                "status_truncated": False,
                "omitted_paths": 0,
            }

        # Scan more than the displayed recent jobs: a long-running task can
        # otherwise disappear behind newer completed tasks.
        jobs = self.jobs.list(project, limit=100).get("jobs", [])
        active_states = {"queued", "running", "cancelling", "waiting"}
        active_jobs = [item for item in jobs if item.get("state") in active_states]
        work_events = self.state.list_work_events(
            project, limit=MAX_RECENT_EVENTS).get("records", [])

        # Activity telemetry is not process verification. Only expose fresh
        # nonterminal observations as candidates with their provenance intact.
        seen = set()
        activity_signals = []
        for item in work_events:
            key = (item.get("source_id"), item.get("actor_id"), item.get("task_id"))
            if key in seen:
                continue
            seen.add(key)
            if item.get("state") not in {"running", "queued", "started", "in_progress"}:
                continue
            timestamp = item.get("received_at") or item.get("time")
            try:
                observed_at = datetime.fromisoformat(timestamp)
                fresh = (observed_at.utcoffset() is not None
                         and 0 <= (datetime.now(timezone.utc) - observed_at).total_seconds() <= 120)
            except (ValueError, TypeError, AttributeError):
                fresh = False
            if not fresh:
                continue
            activity_signals.append({
                key: item.get(key) for key in (
                    "source_id", "actor_id", "task_id", "task_label", "action",
                    "state", "provenance", "time", "received_at")
                if item.get(key) is not None
            })

        access = None
        if self.access is not None:
            try:
                observed = self.access.get(project)
                access = {key: observed.get(key) for key in (
                    "active", "reason", "expires_at", "allow_codex", "scope"
                )}
            except (ValueError, OSError):
                access = {"active": False, "reason": "UNAVAILABLE",
                          "expires_at": None, "allow_codex": False, "scope": None}

        return {
            "project": project,
            "generated_at": generated_at,
            "progress": progress,
            "memory": {
                "revision": memory["revision"],
                "updated_at": memory["updated_at"],
                "bytes": len(memory["content"].encode("utf-8")),
                "excerpt": excerpt,
                "excerpt_truncated": memory_truncated,
                "archives": archives,
            },
            "git": git_snapshot,
            "recent_jobs": [_safe_job(item) for item in jobs[:MAX_RECENT_JOBS]],
            "active_work": {
                "observed_active_isolated_jobs": [_safe_job(item) for item in active_jobs[:MAX_RECENT_JOBS]],
                "isolated_job_count": len(active_jobs),
                "isolated_scan_limit": 100,
                "isolated_scan_complete": len(jobs) < 100,
                "activity_signals_unverified": activity_signals[:MAX_RECENT_EVENTS],
                "not_included": ["host_executions", "agent_tasks", "docker_operations",
                                 "runtime_rollouts"],
            },
            "recent_work_events": work_events,
            "host_access": access,
            "next_steps": list(progress.get("next_steps", [])),
        }
