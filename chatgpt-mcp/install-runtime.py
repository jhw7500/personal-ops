#!/usr/bin/env python3
"""Install a verified runtime snapshot outside every writable MCP project."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from jobs import private_directory, private_file
from server import CONFIG, load_projects


FILES = ("server.py", "catalog_refresh.py", "serena_backend.py", "execution.py", "jobs.py", "job_tools.py",
         "host_runtime_config.py", "host_runtime.py", "host_runtime_operations.py",
         "host_runtime_tools.py", "host_runtime_web.py", "host_db_migration.py", "host_schema014.py",
         "toss_console_evidence.py", "toss_console_tools.py", "toss_console_sdk_bridge.py",
         "toss_console_codex.py",
         "runtime_rollout_web.py", "runtime_rollout_worker.py", "runtime_rollout_tools.py", "runtime_rollout_ui.py", "runtime-rollout-approval.html",
         "approve-host-operation.py",
         "postgres_job.py", "git_workflows.py", "approve-git-operation.py", "context.yml",
         "project-picker.html", "project_state.py", "admin_server.py",
         "admin.html", "admin.js", "admin.css", "agent-admin.js",
         "runtime-rollout-admin.js", "workspace_view.py",
         "workroom-frame.html", "workroom-frame.js", "workroom-bridge.js",
         "codex_hooks.py", "codex_observer.py", "codex_activity.py", "codex_hook_report.py",
         "claude_hooks.py", "claude_hook_report.py",
         "host_registry.py", "host_actions.py", "host_process.py", "host_source_review.py",
         "host_registered_operations.py", "host_compose_actions.py", "host_registered_tools.py",
         "agent_profiles.py", "agent_tasks.py", "agent_runner.py", "agent_events.py",
         "agent_results.py", "agent_tools.py", "agent_host_broker.py",
         "agent_broker_server.py", "agent-bootstrap.c", "agent_bootstrap.py",
         "agent_supervisor.py", "agent_probe.py", "agent_code_mode.py", "agent_proofs.py", "agent_smoke.py",
         "agent_profile_setup.py", "configure-agent-profile.py",
         "host_command_profiles.py", "host_command_entry.py",
         "host_command_runner.py", "host_command_supervisor.py",
         "host_command_operations.py", "host_command_tools.py", "host-command-admin.js",
         "host_access.py", "host_access_installation.py",
         "host_execution.py", "host_execution_tools.py", "host_agent.py",
         "host-access-admin.js", "host_access_ui.py", "host-access-approval.html",
         "host_operation_ui.py", "host-operation-approval.html",
         "host_command_ui.py", "host-command-approval.html",
         "workspace_paths.py", "workspace_policy.py", "workspace_authority.py",
         "workspace_activation.py", "workspace_activation_cli.py",
         "workspace_activation_host.py", "workspace_activation_legacy.py",
         "rollout_admin.py", "rollout_tunnel.py", "rollout_diagnostics.py",
         "workspace_files.py", "workspace_jobs.py", "workspace_tools.py", "workspace_host.py",
         "workspace_control.py", "workspace-launcher.py", "workspace-access-approval.html")
DASHBOARD_FILES = ('model.js', 'motion.js', 'pixels.js', 'visualizer.js', 'visualizer.css')


def install(source, destination, projects):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if any(destination.resolve().is_relative_to(path.resolve()) for path in projects.values()):
        raise ValueError("Runtime must be installed outside every writable project")
    private_directory(destination)
    contents = {}
    for name in FILES:
        path = source / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Runtime source must contain only user-owned regular files")
        contents[name] = path.read_bytes()
    dashboard = source.parent / 'work-dashboard'
    if dashboard.is_symlink() or not dashboard.is_dir() or dashboard.stat().st_uid != os.getuid():
        raise ValueError('Dashboard source must be a user-owned non-symlink directory')
    for name in DASHBOARD_FILES:
        path = dashboard / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Dashboard source must contain only user-owned regular files')
        contents['work-dashboard/' + name] = path.read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    release = destination / digest
    if any(release.resolve().is_relative_to(path.resolve()) for path in projects.values()):
        raise ValueError('Runtime release must be outside every writable project')
    if release.exists() or release.is_symlink():
        info = release.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("Existing release is unsafe")
        for name, expected in manifest.items():
            path = release / name
            if name.startswith('work-dashboard/'):
                folder = path.parent
                info = folder.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError('Existing dashboard runtime directory is unsafe')
            private_file(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Existing runtime snapshot changed")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=destination))
        try:
            for name, data in {**contents, "manifest.json": json.dumps(manifest, sort_keys=True).encode()}.items():
                if name.startswith('work-dashboard/'):
                    private_directory(temporary / 'work-dashboard')
                descriptor = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                private_file(temporary / name)
            temporary.rename(release)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"runtime_dir": str(release), "server": str(release / "server.py"),
            "snapshot": digest, "file_count": len(contents)}


if __name__ == "__main__":
    print(json.dumps(install(Path(__file__).resolve().parent,
                             Path.home() / ".local/lib/chatgpt-coding-mcp/releases",
                             load_projects(CONFIG))))
