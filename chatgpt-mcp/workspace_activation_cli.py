"""Owner-local CLI for the crash-safe first Workspace activation transaction.

The module is deliberately absent from the MCP tool catalog.  Status and
construction are read-only.  Host or store mutations occur only through the
explicit ``apply`` and ``resume`` commands.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from workspace_activation import ActivationCoordinator, ActivationJournal
from workspace_activation_host import SystemdActivationHost
from workspace_activation_legacy import LegacyAuthorityInvalidator
from workspace_authority import WorkspaceAuthority
from workspace_paths import WorkspaceError, read_owner_file, validate_private_directory


_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_ROLLOUT_ACTIVE = frozenset({"queued", "running"})


class OwnerCliError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OwnerRoots:
    home: Path
    config_home: Path
    state_home: Path
    config_root: Path
    state_root: Path
    releases: Path
    controller: Path
    authority: Path
    journal: Path
    profile: Path
    tunnel_override: Path
    admin_override: Path
    tunnel_client: Path

    @classmethod
    def from_environment(cls, environ=None):
        values = os.environ if environ is None else environ
        home = Path(values.get("HOME", str(Path.home()))).absolute()
        config_home = Path(values.get("XDG_CONFIG_HOME", str(home / ".config"))).absolute()
        state_home = Path(values.get("XDG_STATE_HOME", str(home / ".local/state"))).absolute()
        config_root = config_home / "chatgpt-coding-mcp"
        state_root = state_home / "chatgpt-coding-mcp"
        library = home / ".local/lib/chatgpt-coding-mcp"
        return cls(
            home, config_home, state_home, config_root, state_root,
            library / "releases", library / "workspace-controller",
            state_root / "workspace-authority", state_root / "workspace-activation",
            config_home / "tunnel-client/personal-ops-coding.yaml",
            config_home / "systemd/user/personal-ops-tunnel.service.d/20-runtime.conf",
            config_home / "systemd/user/personal-ops-admin.service.d/20-runtime.conf",
            home / ".local/bin/tunnel-client",
        )


def _private_directory(path, code):
    path = Path(path).absolute()
    try:
        validate_private_directory(path)
        info = path.lstat()
    except (OSError, WorkspaceError) as exc:
        raise OwnerCliError(code) from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise OwnerCliError(code)
    return path


def load_projects(path):
    try:
        snapshot = read_owner_file(Path(path).absolute())
        value = json.loads(snapshot.data)
    except (WorkspaceError, OSError, UnicodeError, ValueError) as exc:
        raise OwnerCliError("ACTIVATION_PROJECTS_UNSAFE") from exc
    if set(value) != {"projects"} or not isinstance(value["projects"], dict):
        raise OwnerCliError("ACTIVATION_PROJECTS_INVALID")
    result = {}
    for name, raw in value["projects"].items():
        if (not isinstance(name, str) or _PROJECT.fullmatch(name) is None
                or not isinstance(raw, str)):
            raise OwnerCliError("ACTIVATION_PROJECTS_INVALID")
        try:
            root = Path(raw).resolve(strict=True)
        except OSError as exc:
            raise OwnerCliError("ACTIVATION_PROJECTS_INVALID") from exc
        if not root.is_dir():
            raise OwnerCliError("ACTIVATION_PROJECTS_INVALID")
        result[name] = root
    if not result:
        raise OwnerCliError("ACTIVATION_PROJECTS_INVALID")
    return result


def _reader(kind, **attributes):
    """Create an inert native parser view without running mutating constructors."""
    value = object.__new__(kind)
    for name, item in attributes.items():
        setattr(value, name, item)
    return value


def build_legacy_reader(projects, roots):
    """Bind native validators to existing roots; never create or recover state."""
    from agent_tasks import AgentTaskManager
    from host_access import HostAccess
    from host_command_operations import HostCommandOperations
    from host_registered_operations import RegisteredHostOperations
    from host_runtime_operations import HostRuntimeOperations
    from runtime_rollout_web import RuntimeRolloutWeb

    state = _private_directory(roots.state_root, "ACTIVATION_LEGACY_LAYOUT_MISSING")
    access_root = _private_directory(state / "host-access", "ACTIVATION_LEGACY_LAYOUT_MISSING")
    command_root = _private_directory(
        state / "host-runtime/commands", "ACTIVATION_LEGACY_LAYOUT_MISSING")
    runtime_root = _private_directory(state / "host-runtime", "ACTIVATION_LEGACY_LAYOUT_MISSING")
    registered_root = _private_directory(
        runtime_root / "registered", "ACTIVATION_LEGACY_LAYOUT_MISSING")
    agent_root = _private_directory(state / "agent-tasks", "ACTIVATION_LEGACY_LAYOUT_MISSING")
    rollout_root = _private_directory(
        state / "runtime-rollout-web", "ACTIVATION_LEGACY_LAYOUT_MISSING")

    access = _reader(
        HostAccess, projects=projects, state_dir=access_root,
        records_dir=_private_directory(access_root / "records", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        locks_dir=_private_directory(access_root / "locks", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
    )
    commands = _reader(
        HostCommandOperations, projects=projects, state_dir=command_root,
        plans_dir=_private_directory(command_root / "plans", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        receipts_dir=_private_directory(command_root / "receipts", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        locks_dir=_private_directory(command_root / "locks", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        lock_path=command_root / "operations.lock",
    )
    runtime = _reader(
        HostRuntimeOperations, projects=projects, state_dir=runtime_root,
        operations_dir=_private_directory(runtime_root / "operations", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        target_authorizations_dir=_private_directory(
            runtime_root / "target-authorizations", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        lock_path=runtime_root / "operations.lock",
        target_authorizations_lock_path=runtime_root / "target-authorizations.lock",
    )
    registered = _reader(
        RegisteredHostOperations, projects=projects, state_dir=runtime_root,
        registered_dir=registered_root,
        operations_dir=_private_directory(
            registered_root / "operations", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        receipts_dir=_private_directory(
            registered_root / "receipts", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        events_dir=_private_directory(
            registered_root / "events", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        locks_dir=_private_directory(
            registered_root / "locks", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        lock_path=registered_root / "operations.lock",
    )
    agents = _reader(
        AgentTaskManager, projects=projects, root=agent_root,
        tasks_dir=_private_directory(agent_root / "tasks", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        receipts_dir=_private_directory(
            agent_root / "receipts", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        leases_dir=_private_directory(
            agent_root / "leases", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        lock=agent_root / "agent-tasks.lock",
    )
    rollout = _reader(
        RuntimeRolloutWeb, state_root=rollout_root,
        plans_dir=_private_directory(rollout_root / "plans", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        receipts_dir=_private_directory(
            rollout_root / "receipts", "ACTIVATION_LEGACY_LAYOUT_MISSING"),
        lock=rollout_root / "rollout.lock",
    )
    return LegacyAuthorityInvalidator(
        host_access_state_dir=access_root, host_command_state_dir=command_root,
        host_runtime_state_dir=runtime_root, registered_state_dir=registered_root,
        agent_task_state_dir=agent_root, rollout_state_dir=rollout_root,
        host_access_store=access, host_command_store=commands,
        host_runtime_store=runtime, registered_store=registered,
        agent_task_store=agents, rollout_store=rollout,
    )


def _systemctl_cat(unit):
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "cat", unit], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False,
    )
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise OwnerCliError("ACTIVATION_UNIT_UNAVAILABLE")
    return result.stdout


def _systemd_command(argv, *, stdout=subprocess.PIPE):
    if not isinstance(argv, list) or not argv or argv[0] != "systemctl":
        raise OwnerCliError("ACTIVATION_SYSTEMD_COMMAND_INVALID")
    result = subprocess.run(
        ["/usr/bin/systemctl", *argv[1:]], stdin=subprocess.DEVNULL,
        stdout=stdout, stderr=subprocess.DEVNULL, timeout=20, check=False,
    )
    if result.returncode:
        raise OwnerCliError("ACTIVATION_SYSTEMD_COMMAND_FAILED")
    return result.stdout or b""


def _tunnel_status(client):
    result = subprocess.run(
        [str(client), "runtimes", "status", "personal-ops-coding", "--json"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=20, check=False,
    )
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise OwnerCliError("ACTIVATION_TUNNEL_STATUS_UNAVAILABLE")
    try:
        return json.loads(result.stdout)
    except (UnicodeError, ValueError) as exc:
        raise OwnerCliError("ACTIVATION_TUNNEL_STATUS_UNAVAILABLE") from exc


def _lock_busy_readonly(path):
    """Probe one existing owner lock without create, chmod or path replacement races."""
    path = Path(path).absolute()
    descriptor = None
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise OwnerCliError("WORKER_LEASE_UNSAFE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = False
        except BlockingIOError:
            busy = True
        after = path.lstat()
        if ((after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink)
                != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                    opened.st_nlink)):
            raise OwnerCliError("WORKER_LEASE_UNSAFE")
        return busy
    except OwnerCliError:
        raise
    except OSError as exc:
        raise OwnerCliError("WORKER_LEASE_UNSAFE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _locked_directory(directory, kind, pattern):
    result = []
    root = _private_directory(directory, "WORKER_LEASE_UNSAFE")
    for path in sorted(root.iterdir()):
        if not path.name.endswith(".lock") or pattern.fullmatch(path.stem) is None:
            raise OwnerCliError("WORKER_LEASE_UNSAFE")
        if _lock_busy_readonly(path):
            result.append({"kind": kind, "id": path.stem})
    return result


def _read_proc_file(path, limit=65536):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.getuid()):
            raise OSError()
        data = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
        if len(data) > limit or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError()
        return data
    except OSError as exc:
        raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_process_directory(folder, expected):
    try:
        current = folder.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
    if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid)
            != (expected.st_dev, expected.st_ino, expected.st_mode, expected.st_uid)):
        raise OwnerCliError("WORKER_PROCESS_UNSAFE")
    return True


def _known_worker_processes(proc_root=Path("/proc")):
    """Pin only recognized owner worker argv with boot identity and start ticks."""
    proc_root = Path(proc_root)
    try:
        boot = _read_proc_file(proc_root / "sys/kernel/random/boot_id", 128).decode().strip()
    except UnicodeError as exc:
        raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
    if _BOOT.fullmatch(boot) is None:
        raise OwnerCliError("WORKER_PROCESS_UNSAFE")
    workers = []
    identities = {
        "runtime_rollout_worker.py": "rollout",
        "personal-ops-agent-bootstrap": "agent",
        "host_command_runner.py": "command",
    }
    try:
        entries = sorted((path for path in proc_root.iterdir() if path.name.isdecimal()),
                         key=lambda path: int(path.name))
    except OSError as exc:
        raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
    for folder in entries:
        try:
            process_info = folder.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
        if not stat.S_ISDIR(process_info.st_mode) or stat.S_ISLNK(process_info.st_mode):
            if process_info.st_uid != os.getuid():
                continue
            raise OwnerCliError("WORKER_PROCESS_UNSAFE")
        try:
            status = _read_proc_file(folder / "status", 16384).decode()
            uid = next(line for line in status.splitlines() if line.startswith("Uid:"))
            uid_fields = uid.split()
            if len(uid_fields) != 5:
                raise ValueError()
            owner = all(int(value) == os.getuid() for value in uid_fields[1:])
        except (OwnerCliError, UnicodeError, ValueError, StopIteration) as exc:
            if not _same_process_directory(folder, process_info):
                continue
            if process_info.st_uid != os.getuid():
                continue
            raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
        if not owner:
            continue
        try:
            stat_text = _read_proc_file(folder / "stat", 16384).decode()
            start_ticks = int(stat_text.rsplit(") ", 1)[1].split()[19])
            raw_argv = _read_proc_file(folder / "cmdline", 65536)
            argv = [item.decode() for item in raw_argv.rstrip(b"\0").split(b"\0") if item]
            confirmed = _read_proc_file(folder / "stat", 16384).decode()
            if int(confirmed.rsplit(") ", 1)[1].split()[19]) != start_ticks:
                raise OwnerCliError("WORKER_PROCESS_UNSAFE")
        except (OwnerCliError, UnicodeError, ValueError, StopIteration, IndexError) as exc:
            if not _same_process_directory(folder, process_info):
                continue
            raise OwnerCliError("WORKER_PROCESS_UNSAFE") from exc
        basenames = [Path(item).name for item in argv]
        kind = identities.get(basenames[0]) if basenames else None
        python = bool(basenames and re.fullmatch(r"python(?:3(?:\.\d+)?)?", basenames[0]))
        if kind is None and python:
            executable = next((name for name in basenames[1:5] if name in identities), None)
            kind = identities.get(executable)
        if kind is None and python and "workspace-launcher.py" in basenames[1:5]:
            try:
                entrypoint = Path(argv[argv.index("--entrypoint") + 1]).name
            except (ValueError, IndexError):
                entrypoint = ""
            kind = identities.get(entrypoint)
        if kind is not None:
            workers.append({"kind": kind,
                            "id": f"pid:{folder.name}:{start_ticks}:{boot}"})
    return workers


def _uncertain_identity_active(item):
    if item.get("termination_verified") is True:
        return False
    identity = item.get("execution_identity")
    if not isinstance(identity, dict):
        return True
    try:
        from host_command_supervisor import HostCommandSupervisor
        evidence = HostCommandSupervisor().inspect_identity(identity)
        return evidence != {"termination_verified": True, "status": "terminated"}
    except Exception:
        return True


def _worker_inventory(projects, roots, legacy):
    """Conservative native-record inventory for all six admitted worker kinds."""
    from agent_tasks import ACTIVE as AGENT_ACTIVE
    from host_command_operations import ACTIVE as COMMAND_ACTIVE
    from host_execution import ACTIVE as HOST_ACTIVE, HostExecution

    workers = []
    locked = []
    jobs_root = _private_directory(roots.state_root / "jobs", "WORKER_LAYOUT_UNSAFE")
    for path in sorted(jobs_root.glob("*.json")):
        try:
            item = json.loads(read_owner_file(path).data)
        except (WorkspaceError, ValueError, UnicodeError) as exc:
            raise OwnerCliError("WORKER_RECORD_INVALID") from exc
        if (item.get("job_id") != path.stem
                or not isinstance(item.get("project"), str)
                or not isinstance(item.get("state"), str)):
            raise OwnerCliError("WORKER_RECORD_INVALID")
        if item["state"] in {"queued", "running"}:
            workers.append({"kind": "job", "id": path.stem})

    for path in sorted(legacy.agents.tasks_dir.glob("*.json")):
        raw = json.loads(read_owner_file(path).data)
        item = legacy.agents._load(raw.get("project"), path.stem)
        if item.get("state") in AGENT_ACTIVE:
            workers.append({"kind": "agent", "id": path.stem})
        elif item.get("state") == "outcome_unknown" and _uncertain_identity_active(item):
            workers.append({"kind": "agent", "id": path.stem})

    for path in sorted(legacy.commands.plans_dir.glob("*.json")):
        item = legacy.commands._load(path.stem)
        if item.get("state") in COMMAND_ACTIVE:
            workers.append({"kind": "command", "id": path.stem})
        elif item.get("state") == "outcome_unknown" and _uncertain_identity_active(item):
            workers.append({"kind": "command", "id": path.stem})

    execution_root = _private_directory(
        roots.state_root / "host-executions", "WORKER_LAYOUT_UNSAFE")
    executions = _reader(
        HostExecution, projects=projects, state_root=roots.state_root,
        state_dir=execution_root,
        plans_dir=_private_directory(execution_root / "plans", "WORKER_LAYOUT_UNSAFE"),
        locks_dir=_private_directory(execution_root / "locks", "WORKER_LAYOUT_UNSAFE"),
        owner_locks_dir=_private_directory(
            execution_root / "locks/owners", "WORKER_LAYOUT_UNSAFE"),
    )
    for path in sorted(executions.plans_dir.glob("*.json")):
        item = executions._load(path.stem)
        if item.get("state") in HOST_ACTIVE:
            workers.append({"kind": "host", "id": path.stem})
        elif item.get("state") == "outcome_unknown" and _uncertain_identity_active(item):
            workers.append({"kind": "host", "id": path.stem})

    for path in sorted(legacy.runtime.operations_dir.glob("*.json")):
        if path.name.endswith(".approval.json"):
            continue
        raw = json.loads(read_owner_file(path).data)
        item = legacy.runtime._load(raw.get("project"), raw.get("target"), path.stem)
        if item.get("state") in {"queued", "running"}:
            workers.append({"kind": "host", "id": path.stem})

    for path in sorted(legacy.registered.operations_dir.glob("*.json")):
        raw = json.loads(read_owner_file(path).data)
        item = legacy.registered._load(raw.get("project"), raw.get("target"), path.stem)
        if item.get("state") in {"queued", "running"}:
            workers.append({"kind": "registered", "id": path.stem})

    for path in sorted(legacy.rollout.plans_dir.glob("*.json")):
        item = legacy.rollout._load(path.stem)
        if item.get("state") in _ROLLOUT_ACTIVE:
            workers.append({"kind": "rollout", "id": path.stem})
        elif item.get("state") == "outcome_unknown":
            from runtime_rollout_web import _observe_unit
            observed = _observe_unit("personal-ops-runtime-rollout-" + path.stem + ".service")
            if observed["active_state"] in {"activating", "active", "deactivating"}:
                workers.append({"kind": "rollout", "id": path.stem})

    locked.extend(_locked_directory(legacy.agents.leases_dir, "agent", _HEX32))
    locked.extend(_locked_directory(legacy.commands.locks_dir, "command", _HEX64))
    locked.extend(_locked_directory(executions.owner_locks_dir, "host", _HEX32))
    locked.extend(_locked_directory(legacy.registered.locks_dir, "registered", _HEX64))
    for kind, identifier, path in (
            ("host", "host", executions.locks_dir / "host.lock"),
            ("registered", "target", roots.state_root / "host-runtime/target.lock")):
        if path.exists() or path.is_symlink():
            if _lock_busy_readonly(path):
                locked.append({"kind": kind, "id": identifier})
    workers.extend(_known_worker_processes())
    return {"workers": workers, "locked_leases": locked,
            "observed_kinds": sorted({"job", "agent", "command", "host", "registered", "rollout"})}


def build_host(projects, roots, legacy, source_dir):
    return SystemdActivationHost(
        releases=roots.releases, interpreter=Path(sys.executable).absolute(),
        profile_path=roots.profile, tunnel_override_path=roots.tunnel_override,
        admin_override_path=roots.admin_override, client=roots.tunnel_client,
        tunnel_unit_fn=lambda: _systemctl_cat("personal-ops-tunnel.service"),
        admin_unit_fn=lambda: _systemctl_cat("personal-ops-admin.service"),
        tunnel_status_fn=lambda: _tunnel_status(roots.tunnel_client),
        command_fn=_systemd_command,
        worker_inventory_fn=lambda: _worker_inventory(projects, roots, legacy),
        source_root=source_dir,
    )


def _coordinator(projects, roots, source_dir, *, require_host):
    from runtime_rollout_web import rollout_lock
    journal = ActivationJournal(roots.journal, projects)
    authority = WorkspaceAuthority(projects, roots.authority)
    legacy = build_legacy_reader(projects, roots)
    host = build_host(projects, roots, legacy, source_dir) if require_host else None
    return ActivationCoordinator(
        projects, journal=journal, authority=authority, host=host, legacy=legacy,
        rollout_lock=rollout_lock, controller_dir=roots.controller,
        source_dir=source_dir,
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="workspace-activation")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "status", "apply", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--transaction-id", required=True)
        command.add_argument("--projects-json")
    prepare = commands.choices["prepare"]
    prepare.add_argument("--policy-candidate", required=True)
    prepare.add_argument("--policy-path", required=True)
    prepare.add_argument("--target-release", required=True)
    prepare.add_argument("--rollback-release", required=True)
    prepare.add_argument("--target-abi", type=int, required=True)
    prepare.add_argument("--rollback-abi", type=int, required=True)
    prepare.add_argument("--minimum-abi", type=int, required=True)
    return parser


def _print(value, stream):
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv=None, *, environ=None, stdout=None):
    output = sys.stdout if stdout is None else stdout
    try:
        args = build_parser().parse_args(argv)
        if _ID.fullmatch(args.transaction_id) is None:
            raise OwnerCliError("ACTIVATION_ID_INVALID")
        roots = OwnerRoots.from_environment(environ)
        if args.command == "status":
            journal = ActivationJournal(roots.journal, {})
            result = journal.read(args.transaction_id)
        else:
            projects_path = Path(args.projects_json).absolute() if args.projects_json else (
                roots.config_root / "projects.json")
            projects = load_projects(projects_path)
            source = Path(__file__).resolve().parent
            coordinator = _coordinator(
                projects, roots, source, require_host=args.command in {"apply", "resume"})
            if args.command == "prepare":
                result = coordinator.prepare(
                    args.transaction_id, policy_candidate=args.policy_candidate,
                    policy_path=args.policy_path, projects_path=projects_path,
                    target_release=args.target_release, rollback_release=args.rollback_release,
                    target_abi=args.target_abi, rollback_abi=args.rollback_abi,
                    minimum_abi=args.minimum_abi,
                )
            else:
                result = coordinator.apply(args.transaction_id)
        _print(result, output)
        return 0
    except Exception as exc:
        code = getattr(exc, "code", "ACTIVATION_CLI_FAILED")
        _print({"error": code, "status": "error"}, output)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
