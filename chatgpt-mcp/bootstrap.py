#!/usr/bin/env python3
"""Plan or create a fresh owner-local MCP installation from a Git clone."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


MODULE_DIR = Path(__file__).resolve().parent
APPLICATION = "chatgpt-coding-mcp"
MINIMUM_ABI = 3
PROJECT_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
REPORT_DIRECTORIES = ("reports", "hw-results", "events")


class BootstrapError(RuntimeError):
    pass


def _load_file_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BootstrapError("BOOTSTRAP_SOURCE_INVALID")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _safe_ancestor(info) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    writable = mode & (stat.S_IWGRP | stat.S_IWOTH)
    return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid in (0, os.getuid())
            and (not writable or info.st_uid == 0 and info.st_mode & stat.S_ISVTX))


def _check_ancestor_chain(path: Path) -> None:
    current = Path(path.absolute().anchor)
    for component in path.absolute().parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
        if not _safe_ancestor(info):
            raise BootstrapError("UNSAFE_EXISTING_SETUP")


def _private_directory(path: Path) -> None:
    path = path.absolute()
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        created = False
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                os.chmod(current, 0o700, follow_symlinks=False)
                info = current.lstat()
                created = True
            except OSError as exc:
                raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
        except OSError as exc:
            raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
        mode = stat.S_IMODE(info.st_mode)
        if not _safe_ancestor(info):
            raise BootstrapError("UNSAFE_EXISTING_SETUP")
        if current == path and (info.st_uid != os.getuid() or mode != 0o700):
            if not created:
                raise BootstrapError("UNSAFE_EXISTING_SETUP")


def _write_new_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _check_expected_file(path: Path, payload: bytes) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1):
        raise BootstrapError("UNSAFE_EXISTING_SETUP")
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
    if current != payload:
        raise BootstrapError("EXISTING_SETUP_DIFFERS")
    return "matching"


def _existing_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BootstrapError("UNSAFE_EXISTING_SETUP") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise BootstrapError("UNSAFE_EXISTING_SETUP")
    return True


def _parse_projects(values: list[str], source: Path, home: Path) -> dict[str, Path]:
    if not values:
        values = [f"personal-ops={source.parent}", f"pim-check={home / 'projects/pim-check'}"]
    projects = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or PROJECT_NAME.fullmatch(name) is None or name in projects:
            raise BootstrapError("INVALID_PROJECT")
        path = Path(raw_path)
        if not path.is_absolute():
            raise BootstrapError("INVALID_PROJECT")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise BootstrapError("INVALID_PROJECT") from exc
        if not resolved.is_dir():
            raise BootstrapError("INVALID_PROJECT")
        projects[name] = resolved
    if "personal-ops" not in projects or "pim-check" not in projects:
        raise BootstrapError("REQUIRED_PROJECT_MISSING")
    projects_root = home / "projects"
    if any(not path.is_relative_to(projects_root) for path in projects.values()):
        raise BootstrapError("PROJECT_OUTSIDE_HOME_PROJECTS")
    return projects


def _policy(projects_root: Path) -> dict:
    return {
        "version": 2,
        "base_root": str(projects_root),
        "artifacts": [
            {
                "project": "pim-check",
                "path": name,
                "kind": "directory",
                "operations": ["list", "read"],
                "fields": [],
                "recursive": True,
                "max_file_bytes": 1048576,
                "content_mode": "text",
            }
            for name in REPORT_DIRECTORIES
        ],
        "protected_paths": [],
    }


def _source_inventory(source: Path, installer) -> tuple[dict[str, str], str, Path]:
    contents = {}
    for name in installer.FILES:
        path = source / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise BootstrapError("BOOTSTRAP_SOURCE_INCOMPLETE") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()):
            raise BootstrapError("BOOTSTRAP_SOURCE_INVALID")
        contents[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    dashboard = source.parent / "work-dashboard"
    if not dashboard.is_dir() or dashboard.is_symlink():
        raise BootstrapError("BOOTSTRAP_SOURCE_INCOMPLETE")
    for name in installer.DASHBOARD_FILES:
        path = dashboard / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise BootstrapError("BOOTSTRAP_SOURCE_INCOMPLETE") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()):
            raise BootstrapError("BOOTSTRAP_SOURCE_INVALID")
        contents[f"work-dashboard/{name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()[:20]
    return contents, snapshot, dashboard


def _installed_access(runtime: Path, projects_path: Path, policy_path: Path,
                      state_root: Path, authority_dir: Path, *, enable: bool) -> dict:
    program = r'''
import json
import sys
from pathlib import Path
runtime, projects_path, policy_path, state_root, authority_dir, action = sys.argv[1:]
sys.path.insert(0, runtime)
from host_access import HostAccess
from workspace_tools import WorkspaceTools
data = json.loads(Path(projects_path).read_text(encoding="utf-8"))
projects = {name: Path(value) for name, value in data["projects"].items()}
workspace = WorkspaceTools(projects, state_dir=Path(authority_dir),
                           config_path=Path(policy_path))
access = HostAccess(projects, Path(state_root), workspace=workspace)
if action == "enable":
    result = access.enable_installation_scope()
else:
    result = {"enabled": access.installation_scope_enabled()}
state = access.installation.load() if result["enabled"] else None
print(json.dumps({"result": result, "state": state}, sort_keys=True))
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", program, str(runtime), str(projects_path),
         str(policy_path), str(state_root), str(authority_dir),
         "enable" if enable else "inspect"],
        text=True, capture_output=True,
    )
    if completed.returncode != 0:
        raise BootstrapError("INSTALLED_SCOPE_SETUP_FAILED") from RuntimeError(
            completed.stderr.strip()
        )
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise BootstrapError("INSTALLED_SCOPE_SETUP_FAILED") from exc


def _configured_result(*, runtime: Path, controller: dict, launch: dict,
                       projects: dict[str, Path], projects_path: Path,
                       authority_dir: Path, status: str) -> dict:
    interpreter = Path(sys.executable).resolve()
    return {
        "status": status,
        "applied": True,
        "minimum_abi": MINIMUM_ABI,
        "projects": {name: str(path) for name, path in projects.items()},
        "runtime": str(runtime),
        "launcher": controller["launcher"],
        "interpreter": str(interpreter),
        "projects_config": str(projects_path),
        "authority": str(authority_dir),
        "launch_argv": [
            str(interpreter), controller["launcher"], "--release", str(runtime),
            "--interpreter", str(interpreter), "--isolated", "--",
            "--config", str(projects_path),
        ],
        "launch": launch,
        "credentials_created": False,
        "host_grant_issued": False,
    }


def run(argv=None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Create the owner-local configuration and installed runtime")
    parser.add_argument("--source", type=Path, default=MODULE_DIR)
    parser.add_argument("--project", action="append", default=[], metavar="NAME=ABSOLUTE_PATH")
    options = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 11):
        raise BootstrapError("PYTHON_3_11_REQUIRED")
    source = options.source.resolve(strict=True)
    if source.name != "chatgpt-mcp":
        raise BootstrapError("BOOTSTRAP_SOURCE_INVALID")
    home = Path.home().resolve()
    projects = _parse_projects(options.project, source, home)
    projects_root = home / "projects"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))).absolute()
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state"))).absolute()
    config_root = config_home / APPLICATION
    state_root = state_home / APPLICATION
    library_root = home / ".local/lib" / APPLICATION
    releases_dir = library_root / "releases"
    controller_dir = library_root / "workspace-controller"
    authority_dir = state_root / "workspace-authority"
    projects_path = config_root / "projects.json"
    policy_path = config_root / "workspace-policy.json"

    installer = _load_file_module("bootstrap_install_runtime", source / "install-runtime.py")
    manifest, snapshot, _dashboard = _source_inventory(source, installer)
    from workspace_control import (
        ABI3_MANDATORY_RELEASE_FILES,
        MANDATORY_RELEASE_FILES,
        install_controller,
        release_enforcement_abi,
        verify_controller,
        verify_launch,
        verify_release_manifest,
    )
    required = MANDATORY_RELEASE_FILES | ABI3_MANDATORY_RELEASE_FILES | {"bootstrap.py"}
    if not required <= manifest.keys():
        raise BootstrapError("BOOTSTRAP_SOURCE_INCOMPLETE")
    projects_payload = _json_bytes({
        "projects": {name: str(path) for name, path in projects.items()},
    })
    policy_payload = _json_bytes(_policy(projects_root))
    runtime = releases_dir / snapshot
    expected_controller = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("workspace_control.py", "workspace-launcher.py")
    }

    plan = {
        "status": "plan",
        "applied": False,
        "minimum_abi": MINIMUM_ABI,
        "projects": {name: str(path) for name, path in projects.items()},
        "config_root": str(config_root),
        "state_root": str(state_root),
        "library_root": str(library_root),
        "runtime": str(runtime),
        "credentials_created": False,
        "host_grant_issued": False,
    }
    if not options.apply:
        return plan

    for target in (config_root, state_root, library_root):
        _check_ancestor_chain(target)
    config_states = (
        _check_expected_file(projects_path, projects_payload),
        _check_expected_file(policy_path, policy_payload),
    )
    existing_parts = [state == "matching" for state in config_states]
    config_root_exists = _existing_directory(config_root)
    state_root_exists = _existing_directory(state_root)
    library_root_exists = _existing_directory(library_root)
    release_exists = _existing_directory(runtime)
    controller_exists = _existing_directory(controller_dir)
    authority_exists = _existing_directory(authority_dir)
    installation_marker = state_root / "host-access/installation-scope-v2.enabled"
    installation_exists = installation_marker.exists() or installation_marker.is_symlink()
    any_existing = any((*existing_parts, config_root_exists, state_root_exists,
                        library_root_exists, release_exists, controller_exists,
                        authority_exists, installation_exists))
    all_existing = all((*existing_parts, config_root_exists, state_root_exists,
                        library_root_exists, release_exists, controller_exists,
                        authority_exists, installation_exists))
    if any_existing and not all_existing:
        raise BootstrapError("EXISTING_SETUP_DIFFERS")

    if all_existing:
        release = verify_release_manifest(runtime)
        if (json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
                != manifest or release_enforcement_abi(runtime) != MINIMUM_ABI):
            raise BootstrapError("EXISTING_SETUP_DIFFERS")
        controller = verify_controller(controller_dir)
        if controller["manifest"] != expected_controller:
            raise BootstrapError("EXISTING_SETUP_DIFFERS")
        launch = verify_launch(runtime, authority_dir, enforcement_required=True)
        if (launch["minimum_abi"] != MINIMUM_ABI
                or launch["release_digest"] != release["release_digest"]
                or launch["binding"]["policy_digest"]
                != hashlib.sha256(policy_payload).hexdigest()):
            raise BootstrapError("EXISTING_SETUP_DIFFERS")
        access = _installed_access(
            runtime, projects_path, policy_path, state_root, authority_dir, enable=False,
        )
        if not access["result"]["enabled"]:
            raise BootstrapError("EXISTING_SETUP_DIFFERS")
        return _configured_result(
            runtime=runtime, controller=controller, launch=launch,
            projects=projects, projects_path=projects_path,
            authority_dir=authority_dir, status="already_configured",
        )

    _private_directory(config_root)
    _write_new_private(projects_path, projects_payload)
    _write_new_private(policy_path, policy_payload)
    _private_directory(library_root)
    _private_directory(releases_dir)
    installed = installer.install(source, releases_dir, projects)
    runtime = Path(installed["runtime_dir"])
    release = verify_release_manifest(runtime)
    if release_enforcement_abi(runtime) != MINIMUM_ABI:
        raise BootstrapError("BOOTSTRAP_SOURCE_INCOMPATIBLE")
    install_controller(controller_dir, source_dir=runtime, projects=projects)
    controller = verify_controller(controller_dir)
    if controller["manifest"] != expected_controller:
        raise BootstrapError("CONTROLLER_INSTALLATION_INVALID")

    from workspace_policy import WorkspacePolicy
    from workspace_authority import WorkspaceAuthority
    snapshot_policy = WorkspacePolicy(projects, config_path=policy_path).load(required=True)
    if snapshot_policy is None:
        raise BootstrapError("POLICY_INSTALLATION_INVALID")
    authority = WorkspaceAuthority(projects, state_dir=authority_dir)
    authority.initialize(
        snapshot_policy.digest,
        {release["release_digest"]: MINIMUM_ABI},
        minimum_abi=MINIMUM_ABI,
    )
    launch = verify_launch(runtime, authority_dir, enforcement_required=True)
    access = _installed_access(
        runtime, projects_path, policy_path, state_root, authority_dir, enable=True,
    )
    if (access["state"]["requests"] != {} or access["state"]["grant"] is not None):
        raise BootstrapError("UNEXPECTED_HOST_GRANT")
    return _configured_result(
        runtime=runtime, controller=controller, launch=launch,
        projects=projects, projects_path=projects_path,
        authority_dir=authority_dir, status="configured",
    )


def main(argv=None) -> int:
    try:
        result = run(argv)
    except (BootstrapError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
