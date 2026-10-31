#!/usr/bin/env python3
"""One-command, local-TTY rollout of the immutable personal-ops MCP runtime.

No source-side MCP request can invoke this on the actual host: start_command
has no host home, tunnel credentials, or network access. No Docker/DB changes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import site
import stat
import subprocess
import sys
import tempfile

from rollout_catalog import observe_tools
from rollout_diagnostics import (RECEIPT_ROOT, CliSessionObserver, RolloutReceipt,
                                 is_interruption, run_command, safe_error)
from rollout_test_runner import EXCLUDED_TEST_METHODS
from workspace_authority import WorkspaceAuthority
from workspace_control import (DEFAULT_AUTHORITY_DIR, WorkspaceControlError,
                               release_enforcement_abi, verify_launch,
                               verify_release_manifest)

HERE = Path(__file__).resolve().parent
ALIAS = "personal-ops-coding"
KEY_FILE = Path.home() / ".config/chatgpt-coding-mcp/runtime-key"
RELEASE_ROOT = Path.home() / ".local/lib/chatgpt-coding-mcp/releases"
TESTS = (
    "tests.test_host_runtime", "tests.test_host_db_restore",
    "tests.test_host_predecessor_migration", "tests.test_admin_host_approval", "tests.test_configure_host_runtime",
    "tests.test_host_simplified_approval", "tests.test_host_simplified_admin",
    "tests.test_host_target_authorization", "tests.test_host_auto_operations",
    "tests.test_host_schema_update", "tests.test_host_schema_router",
    "tests.test_host_schema014", "tests.test_host_schema014_approval",
    "tests.test_schema014_chat_approval", "tests.test_schema014_chat_router",
    "tests.test_test_plan_chat_approval", "tests.test_test_plan_chat_router",
    "tests.test_host_predecessor_objects",
    "tests.test_admin_target_authorization",
    "tests.test_admin_registered_approval", "tests.test_host_actions",
    "tests.test_host_compose_actions", "tests.test_host_process",
    "tests.test_host_registered_operations", "tests.test_host_registered_router",
    "tests.test_registered_host_chat_approval",
    "tests.test_extended_chat_approval_router",
    "tests.test_host_registry", "tests.test_host_source_review",
    "tests.test_host_config_cas",
    "tests.test_stage1_activation_fixes",
    "tests.test_project_loading", "tests.test_git_chat_approval",
    "tests.test_git_commit_access", "tests.test_git_commit_access_backend_router",
    "tests.test_git_commit_access_router", "tests.test_git_commit_access_concurrency",
    "tests.test_git_commit_access_registration",
    "tests.test_runtime_install", "tests.test_admin_server",
    "tests.test_rollout_host_runtime", "tests.test_rollout_admin",
    "tests.test_rollout_diagnostics", "tests.test_rollout_tunnel",
    "tests.test_rollout_catalog",
    "tests.test_agent_tasks", "tests.test_admin_agent_approval",
    "tests.test_agent_results", "tests.test_agent_runtime_registration",
    "tests.test_agent_bootstrap", "tests.test_agent_probe", "tests.test_agent_code_mode",
    "tests.test_agent_probe_environment", "tests.test_agent_supervision",
    "tests.test_agent_supervised_runner", "tests.test_agent_host_broker",
    "tests.test_agent_proofs", "tests.test_agent_smoke", "tests.test_agent_profile_setup",
    "tests.test_host_backup_validation",
    "tests.test_runtime_rollout_web", "tests.test_runtime_rollout_worker",
    "tests.test_rollout_bootstrap_compat",
    "tests.test_rollout_workspace_authority",
    "tests.test_admin_runtime_rollout_approval",
    "tests.test_runtime_rollout_router", "tests.test_runtime_rollout_chat_approval", "tests.test_runtime_rollout_chat_router", "tests.test_runtime_rollout_chat_bootstrap",
    "tests.test_toss_console_evidence", "tests.test_toss_console_tools",
    "tests.test_toss_console_sdk_bridge", "tests.test_toss_console_codex",
    "tests.test_toss_console_ux019", "tests.test_toss_console_20261026",
    "tests.test_toss_console_new_test_evidence",
    "tests.test_host_command_profiles", "tests.test_host_command_entry",
    "tests.test_host_command_operations", "tests.test_host_command_runner",
    "tests.test_host_command_supervisor", "tests.test_host_command_tools",
    "tests.test_host_command_chat_approval", "tests.test_host_command_chat_router",
    "tests.test_admin_host_command_approval",
    "tests.test_host_access", "tests.test_host_execution", "tests.test_host_execution_tools",
    "tests.test_chat_host_access_approval", "tests.test_chat_host_access_router",
    "tests.test_host_agent", "tests.test_host_agent_entry", "tests.test_admin_host_access",
    "tests.test_execution",
    "tests.test_workspace_policy", "tests.test_workspace_authority",
    "tests.test_workspace_files", "tests.test_workspace_git",
    "tests.test_workspace_jobs", "tests.test_workspace_host",
    "tests.test_workspace_execution", "tests.test_workspace_tools",
    "tests.test_workspace_router", "tests.test_workspace_control",
    "tests.test_workspace_initialization", "tests.test_workspace_activation",
    "tests.test_workspace_activation_transaction", "tests.test_workspace_activation_cli",
    "tests.test_workspace_activation_gate", "tests.test_workspace_activation_host",
    "tests.test_workspace_activation_legacy",
    "tests.test_catalog_refresh", "tests.test_catalog_refresh_router",
    "tests.test_project_resume", "tests.test_resume_fingerprint",
    "tests.test_server_shutdown",
)
# A reviewed, independent contract. Do not derive this from dynamic tool
# registration: a silently missing migration tool must block rollout validation.
TOOL_NAMES = {
    "get_runtime_status", "compare_deployment", "get_db_migration_status",
    "get_predecessor_migration_status", "get_schema014_migration_status",
    "get_toss_test_console_status", "get_toss_ux019_test_console_status",
    "get_toss_20261026_test_console_status",
    "prepare_schema014_migration", "execute_schema014_migration",
    "prepare_db_migration", "prepare_predecessor_migration",
    "execute_db_migration", "execute_predecessor_migration",
    "prepare_test_deploy", "execute_test_deploy", "get_host_operation",
    "list_host_targets", "inspect_host_target", "prepare_host_operation",
    "execute_host_operation", "read_host_operation_events",
    "list_agent_profiles", "prepare_agent_task", "start_agent_task",
    "get_agent_task", "cancel_agent_task",
    "list_host_command_profiles", "prepare_host_command",
    "execute_host_command", "get_host_command", "cancel_host_command",
    "request_host_access", "get_host_access", "start_host_execution",
    "get_host_execution", "read_host_execution_log", "cancel_host_execution",
    "prepare_runtime_rollout", "get_runtime_rollout", "execute_runtime_rollout",
    "open_runtime_rollout_approval", "confirm_runtime_rollout_approval", "cancel_runtime_rollout_approval",
    "open_host_access_approval", "update_host_access_approval",
    "confirm_host_access_approval", "cancel_host_access_approval",
    "open_schema014_approval", "confirm_schema014_approval", "cancel_schema014_approval",
    "open_test_plan_approval", "confirm_test_plan_approval", "cancel_test_plan_approval",
    "open_registered_host_approval", "confirm_registered_host_approval",
    "cancel_registered_host_approval",
    "open_git_approval", "confirm_git_approval", "cancel_git_approval",
    "open_git_commit_access_approval", "get_git_commit_access",
    "confirm_git_commit_access", "cancel_git_commit_access", "revoke_git_commit_access",
    "open_host_command_approval", "confirm_host_command_approval", "cancel_host_command_approval",
    "confirm_and_execute_host_command",
    "get_project_resume", "compact_project_memory",
}
# This surface is configured only when personal-ops is available. Keep the
# expected names independent from catalog_refresh.tool_specs() so registration
# drift still blocks rollout validation.
CATALOG_REFRESH_TOOL_NAMES = frozenset({
    "refresh_mcp_catalog",
    "get_mcp_catalog_refresh_status",
})
# These are registered separately by job_tools under the same workspace
# predicate. Keep them independent of dynamic registration for fail-closed
# discovery validation.
WORKSPACE_OUTPUT_TOOL_NAMES = frozenset({
    "read_workspace_output",
    "publish_workspace_output",
})


def _import_installer():
    script = HERE / "install-runtime.py"
    spec = importlib.util.spec_from_file_location("host_runtime_installer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(installer):
    root = HERE.parent
    paths = [HERE / item for item in installer.FILES]
    paths += [root / "work-dashboard" / name for name in installer.DASHBOARD_FILES]
    result = {}
    for path in paths:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or path.is_symlink():
            raise RuntimeError("UNSAFE_SOURCE_FILE")
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def control_inventory():
    """Pin the rollout helpers and regression inputs as well as runtime files."""
    names = ("rollout-host-runtime.py", "rollout_test_runner.py",
             "rollout_tunnel.py", "rollout_admin.py", "rollout_catalog.py",
             "rollout_diagnostics.py", "install-runtime.py", "configure-host-runtime.py",
             "tossapp_neutral_ab24_command.js")
    paths = [HERE / name for name in names]
    paths.extend(sorted((HERE / "tests").glob("test_*.py")))
    paths.append(HERE / "tests/check_host_access_approval_ui.py")
    paths.append(HERE / "tests/check_schema014_approval_ui.py")
    paths.append(HERE / "tests/check_host_command_approval_ui.py")
    paths.extend(node_ui_test_inputs())
    result = {}
    for path in paths:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or path.is_symlink() or info.st_size > 4 * 1024 * 1024):
            raise RuntimeError("UNSAFE_SOURCE_FILE")
        result[str(path.relative_to(HERE))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def runtime_interpreter(interpreter=sys.executable):
    verified_venv = Path.home() / ".local/share/uv/tools/serena-agent/bin/python"
    if verified_venv.is_file() and os.access(verified_venv, os.X_OK):
        return str(verified_venv)
    return str(Path(interpreter).absolute())


def runtime_node():
    verified_node = Path.home() / ".nvm/versions/node/v24.12.0/bin/node"
    if verified_node.is_file() and os.access(verified_node, os.X_OK):
        return str(verified_node)
    raise RuntimeError("ROLLOUT_NODE_UNAVAILABLE")


def expected_tool_names(projects, *, config_path=None, authority_dir=None):
    """Use the server's workspace activation rule without creating authority."""
    from workspace_tools import NAMES, WorkspaceTools
    if authority_dir is None:
        state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        authority_dir = state / "chatgpt-coding-mcp/workspace-authority"
    expected = TOOL_NAMES | (CATALOG_REFRESH_TOOL_NAMES
                             if "personal-ops" in projects else frozenset())
    workspace = WorkspaceTools(projects, authority_dir, config_path=config_path)
    if workspace.enabled:
        return expected | NAMES | WORKSPACE_OUTPUT_TOOL_NAMES
    return expected


def node_ui_test_inputs(tests_dir=HERE / "tests"):
    """Return the mandatory workspace verifier first, then optional MJS checks."""
    tests_dir = Path(tests_dir)
    required = tests_dir / "test_workspace_approval_ui.mjs"
    try:
        info = required.lstat()
    except OSError:
        raise RuntimeError("RUNTIME_TEST_INPUT_MISSING") from None
    if required.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("RUNTIME_TEST_INPUT_UNSAFE")
    optional = sorted(path for path in tests_dir.glob("test_*.mjs") if path != required)
    return (required, *optional)


@contextmanager
def _private_check_umask():
    # This standalone rollout process restores its creation mode before any
    # live operation. Isolated test subprocesses always create private state.
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def run_checks(interpreter=sys.executable, *, receipt=None):
    # Always run the relevant tests and the complete MCP regression before
    # switching a privileged installed runtime. Failure blocks activation.
    interpreter = runtime_interpreter(interpreter)
    node = runtime_node()
    owner_home = Path.home()
    owner_userbase = site.getuserbase()
    approved_venv = owner_home / ".local/share/uv/tools/serena-agent"
    approved_interpreter = approved_venv / "bin/python"
    print(f"Official excluded unittest IDs ({len(EXCLUDED_TEST_METHODS)}):", flush=True)
    for method in EXCLUDED_TEST_METHODS:
        print(" - test_execution.ExecutionPolicyTest." + method
              + " (or tests.test_execution prefix)", flush=True)
    with _private_check_umask(), tempfile.TemporaryDirectory(prefix="runtime-preflight-") as temporary:
        private = Path(temporary)
        home = private / "home"
        config = private / "config"
        state = private / "state"
        cache = private / "cache"
        for directory in (home, config, state, cache):
            directory.mkdir(mode=0o700)
        if Path(interpreter) == approved_interpreter:
            private_tools = home / ".local/share/uv/tools"
            private_tools.mkdir(mode=0o700, parents=True)
            (private_tools / "serena-agent").symlink_to(
                approved_venv, target_is_directory=True)
        environment = [
            "/usr/bin/env",
            "HOME=" + str(home),
            "XDG_CONFIG_HOME=" + str(config),
            "XDG_STATE_HOME=" + str(state),
            "XDG_CACHE_HOME=" + str(cache),
            "PLAYWRIGHT_BROWSERS_PATH=" + str(owner_home / ".cache/ms-playwright"),
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONUSERBASE=" + owner_userbase,
            "PATH=" + str(Path(node).parent) + ":" + str(owner_home / ".local/bin")
            + ":/usr/local/bin:/usr/bin:/bin",
            "UV_OFFLINE=1",
        ]
        test_runner = str(HERE / "rollout_test_runner.py")
        node_tests = [str(path) for path in node_ui_test_inputs()]
        for stage, argv, timeout in (
            ("checks_approval_ui", [*environment, "/usr/bin/python3", "-B",
                                    str(HERE / "tests/check_host_access_approval_ui.py")], 120),
            ("checks_schema014_ui", [*environment, "/usr/bin/python3", "-B",
                                     str(HERE / "tests/check_schema014_approval_ui.py")], 120),
            ("checks_host_command_ui", [*environment, "/usr/bin/python3", "-B",
                                        str(HERE / "tests/check_host_command_approval_ui.py")], 120),
            ("checks_runtime_rollout_chat_ui", [*environment, "/usr/bin/python3", "-B",
                                                str(HERE / "tests/test_runtime_rollout_chat_ui.py")], 120),
            ("checks_runtime_rollout_ui", [*environment, "/usr/bin/python3", "-B",
                                           str(HERE / "tests/test_runtime_rollout_ui.py")], 120),
            ("checks_workspace_approval_browser", [*environment, "/usr/bin/python3", "-B",
                                                    str(HERE / "tests/test_workspace_approval_browser.py")], 180),
            ("checks_git_commit_access_browser", [*environment, "/usr/bin/python3", "-B",
                                                  str(HERE / "tests/test_git_commit_access_browser.py")], 120),
            ("checks_workspace_ui", [*environment, node, "--test", *node_tests], 180),
            ("checks_targeted", [*environment, interpreter, "-B", test_runner,
                                 "--require-excluded-count", "5", "--modules", *TESTS], 300),
            ("checks_full", [*environment, interpreter, "-B", test_runner,
                             "--require-excluded-count", "5", "--discover", "tests"], 480),
        ):
            check = run_command(argv, stage=stage, cwd=HERE,
                                timeout=timeout, receipt=receipt)
            if check.returncode != 0:
                raise RuntimeError("RUNTIME_TESTS_FAILED")


def tunnel_client():
    candidate = shutil.which("tunnel-client")
    if candidate:
        return candidate
    candidate = Path.home() / ".local/bin/tunnel-client"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise RuntimeError("TUNNEL_CLIENT_UNAVAILABLE")


def _find_id(value):
    if isinstance(value, dict):
        for key in ("tunnel_id", "tunnelId"):
            item = value.get(key)
            if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{5,160}", item):
                return item
        for child in value.values():
            found = _find_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_id(child)
            if found:
                return found
    return None


def runtime_status(executable, *, receipt=None):
    result = run_command([executable, "runtimes", "status", ALIAS, "--json"],
                         stage="tunnel_status", capture=True, max_output=64 * 1024,
                         timeout=20, receipt=receipt)
    if result.returncode:
        raise RuntimeError("TUNNEL_RUNTIME_STATUS_FAILED")
    if len(result.stdout) > 64 * 1024:
        raise RuntimeError("UNEXPECTED_TUNNEL_STATUS")
    try:
        data = json.loads(result.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("UNEXPECTED_TUNNEL_STATUS") from exc
    return data


def _key_check(path=KEY_FILE):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size < 16 or info.st_size > 4096):
        raise RuntimeError("UNSAFE_TUNNEL_KEY")


def service_command(argv, *, stdout=subprocess.DEVNULL, receipt=None):
    """Only the existing two user services are controlled by rollout helpers."""
    services = {"personal-ops-tunnel.service": "tunnel", "personal-ops-admin.service": "admin"}
    if argv == ["systemctl", "--user", "daemon-reload"]:
        stage = "systemd_reload"
    elif (len(argv) >= 4 and argv[:2] == ["systemctl", "--user"]
          and argv[3] in services and argv[2] in {"cat", "show", "start", "stop", "restart"}):
        action = argv[2]
        if (action in {"start", "stop"} and services[argv[3]] != "tunnel"
                or action == "restart" and services[argv[3]] != "admin"):
            raise RuntimeError("UNEXPECTED_SERVICE_COMMAND")
        stage = services[argv[3]] + "_" + action
    elif (len(argv) == 5 and argv[:3] == ["systemctl", "--user", "is-active"]
          and argv[3] == "--quiet" and argv[4] in services):
        stage = services[argv[4]] + "_active"
    else:
        raise RuntimeError("UNEXPECTED_SERVICE_COMMAND")
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    environment = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus",
    }
    result = run_command(argv, stage=stage, timeout=40,
                         capture=stdout == subprocess.PIPE, max_output=256 * 1024,
                         receipt=receipt, env=environment)
    if result.returncode:
        raise RuntimeError("ROLLOUT_SERVICE_COMMAND_FAILED")
    return result.stdout


def _live_fields(live):
    return {key: live[key] for key in ("tunnel_pid", "tunnel_start_ticks", "mcp_pid", "mcp_start_ticks",
                                      "codex_pid", "codex_start_ticks") if key in live}


def _verify_approved_web(approved_web, installer, *, catalog_fn=None):
    """Verify a web grant, with one exact legacy-control bootstrap fallback."""
    try:
        approved_web.verify(HERE.parent, installer, inventory, control_inventory)
        return
    except RuntimeError as exc:
        if safe_error(exc) != "WEB_ROLLOUT_SOURCE_CHANGED":
            raise
        original = exc
    if catalog_fn is None:
        from runtime_rollout_web import _catalog as catalog_fn
    actual_source = catalog_fn(HERE.parent)
    official = inventory(installer)
    manifest = {name.removeprefix("chatgpt-mcp/"): digest
                for name, digest in official.items()}
    target = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    controls = control_inventory()
    legacy_key = "tossapp_neutral_ab24_command.js"
    if legacy_key not in controls:
        raise original
    current_digest = hashlib.sha256(
        json.dumps(controls, sort_keys=True).encode()
    ).hexdigest()
    legacy_controls = dict(controls)
    legacy_controls.pop(legacy_key)
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_controls, sort_keys=True).encode()
    ).hexdigest()
    if (actual_source.get("digest") != approved_web.source_digest
            or target != approved_web.target_snapshot
            or approved_web.control_digest == current_digest
            or approved_web.control_digest != legacy_digest):
        raise original


def _authorize_workspace_rollout(projects, target_runtime, rollback_runtime,
                                 authority_dir=None):
    """Authorize exactly target + rollback releases before a managed switch."""
    authority_path = Path(
        authority_dir if authority_dir is not None else DEFAULT_AUTHORITY_DIR
    ).absolute()
    target_path = Path(target_runtime).absolute()
    rollback_path = Path(rollback_runtime).absolute()
    try:
        rollback = verify_launch(
            rollback_path, authority_path, enforcement_required=True
        )
        target_release = verify_release_manifest(target_path)
        target_abi = release_enforcement_abi(target_path)
        minimum_abi = rollback["minimum_abi"]
        if target_abi < minimum_abi:
            raise WorkspaceControlError("WORKSPACE_RELEASE_INCOMPATIBLE")
        allowed = {target_release["release_digest"]: target_abi}
        old_digest = rollback["release_digest"]
        old_abi = rollback["release_abi"]
        if old_digest in allowed and allowed[old_digest] != old_abi:
            raise WorkspaceControlError("WORKSPACE_RELEASE_ABI_MISMATCH")
        allowed[old_digest] = old_abi
        authority = WorkspaceAuthority(projects, state_dir=authority_path)
        binding = authority.advance(rollback["binding"]["policy_digest"], allowed)
        target = verify_launch(
            target_path, authority_path, enforcement_required=True
        )
        restored = verify_launch(
            rollback_path, authority_path, enforcement_required=True
        )
        if target["binding"] != binding or restored["binding"] != binding:
            raise WorkspaceControlError("WORKSPACE_AUTHORITY_CHANGED")
        return {
            "target_digest": target["release_digest"],
            "target_abi": target["release_abi"],
            "rollback_digest": restored["release_digest"],
            "rollback_abi": restored["release_abi"],
            "authority_epoch": binding["authority_epoch"],
        }
    except WorkspaceControlError as exc:
        raise RuntimeError(exc.code) from exc


def rollout(*, apply=False, tunnel_id=None, interpreter=sys.executable,
            status_fn=None, command_fn=None, receipt=None, approved_web=None):
    installer = _import_installer()
    from server import CONFIG, load_projects
    from rollout_admin import AdminSwitchError, preflight_admin, switch_admin
    from rollout_tunnel import TunnelSwitchError, freeze_live_runtime, preflight_tunnel, switch_tunnel

    projects = load_projects(CONFIG)
    source_before = inventory(installer)
    controls_before = control_inventory()
    run_checks(interpreter, receipt=receipt)
    if inventory(installer) != source_before or control_inventory() != controls_before:
        raise RuntimeError("SOURCE_CHANGED_DURING_CHECK")
    client = tunnel_client()
    read_status = status_fn or (lambda executable: runtime_status(executable, receipt=receipt))
    call_service = command_fn or (lambda argv, **kw: service_command(argv, receipt=receipt, **kw))
    status = read_status(client)
    discovered_id = _find_id(status)
    actual_id = tunnel_id or discovered_id
    if not actual_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{5,160}", actual_id):
        raise RuntimeError("TUNNEL_ID_UNAVAILABLE")
    if tunnel_id and discovered_id and tunnel_id != discovered_id:
        raise RuntimeError("TUNNEL_ID_MISMATCH")
    _key_check()
    with receipt.phase("live_runtime_preflight") if receipt else nullcontext():
        before = freeze_live_runtime(status)
        plan = preflight_tunnel(before, unit_fn=lambda: call_service(
            ["systemctl", "--user", "cat", "personal-ops-tunnel.service"], stdout=subprocess.PIPE),
            client=client)
        preflight_admin(command_fn=call_service)
    target_manifest = {name.removeprefix("chatgpt-mcp/"): value for name, value in source_before.items()}
    target_digest = hashlib.sha256(json.dumps(target_manifest, sort_keys=True).encode()).hexdigest()[:20]
    alignment = {key: plan[key] for key in ("profile_aligned", "service_aligned") if key in plan}
    if receipt:
        receipt.event("preflight_result", "completed", snapshot=target_digest,
                      live_snapshot=before["snapshot"], source_files=len(source_before),
                      control_sha256=hashlib.sha256(json.dumps(controls_before, sort_keys=True).encode()).hexdigest(),
                      **_live_fields(before), **alignment)
    if not apply:
        return {"status": "preflight_ok", "source_files": len(source_before),
                "tunnel_id_available": True, "installed": False, "switched": False,
                "snapshot": target_digest, "live_snapshot": before["snapshot"],
                **_live_fields(before), **alignment}

    if approved_web is None:
        # The original interactive path is unchanged.
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("HOST_TTY_REQUIRED")
        print("Install reviewed runtime; align only existing tunnel unit/profile MCP command.", flush=True)
        print("Stop/start the tunnel, including its Codex app-server; restart the recognized admin service.", flush=True)
        print("Preserve credentials, network settings and old snapshots. No Docker/DB changes or Schema 013 application.", flush=True)
        print("Type ROLLOUT to continue:", flush=True)
        if sys.stdin.readline().strip() != "ROLLOUT":
            raise RuntimeError("ROLLOUT_DECLINED")
    else:
        from runtime_rollout_web import WebRolloutGrant
        if type(approved_web) is not WebRolloutGrant:
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
        _verify_approved_web(approved_web, installer)
        if approved_web.target_snapshot != target_digest:
            raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")
    # Revalidate the source after operator approval, before snapshot creation.
    if inventory(installer) != source_before or control_inventory() != controls_before:
        raise RuntimeError("SOURCE_CHANGED_DURING_APPROVAL")
    with receipt.phase("snapshot_install") if receipt else nullcontext():
        installed = installer.install(HERE, RELEASE_ROOT, projects)
    # Protect against edits while snapshot installation was in progress.
    if inventory(installer) != source_before or control_inventory() != controls_before:
        raise RuntimeError("SOURCE_CHANGED_DURING_INSTALL")
    manifest = json.loads((Path(installed["runtime_dir"]) / "manifest.json").read_text())
    for name, expected in source_before.items():
        key = name.removeprefix("chatgpt-mcp/")
        if key not in manifest or manifest[key] != expected:
            raise RuntimeError("INSTALLED_SNAPSHOT_MISMATCH")
    if receipt:
        receipt.event("snapshot_installed", "completed", snapshot=installed["snapshot"],
                      previous_snapshot=before["snapshot"], source_files=len(source_before))
    catalog = {}
    def verify_live(actual):
        if (actual["snapshot"] != installed["snapshot"]
                or actual["interpreter"] != runtime_interpreter(interpreter)):
            raise TunnelSwitchError("LIVE_RUNTIME_MISMATCH")
        catalog.update(observe_tools(expected_tool_names(projects)))
        if receipt:
            receipt.event("mcp_discovery", "completed", **catalog)
    excluded = tuple(before[key] for key in ("tunnel_pid", "mcp_pid", "codex_pid") if key in before)
    with CliSessionObserver(receipt, excluded_pids=excluded) as cli:
        with receipt.phase("tunnel_switch") if receipt else nullcontext():
            _authorize_workspace_rollout(
                projects, installed["runtime_dir"], Path(before["server"]).parent
            )
            actual = switch_tunnel(installed["runtime_dir"], interpreter=runtime_interpreter(interpreter),
                                   status_fn=lambda: read_status(client), command_fn=call_service,
                                   plan=plan, client=client, verify_fn=verify_live)
        cli.checkpoint("cli_after_tunnel")
        if receipt:
            receipt.event("live_runtime_verified", "completed", snapshot=actual["snapshot"],
                          previous_snapshot=before["snapshot"], **_live_fields(actual))
        admin_error = None
        admin_cause = None
        try:
            with receipt.phase("admin_switch") if receipt else nullcontext():
                admin_status = switch_admin(installed["runtime_dir"], command_fn=call_service)
        except (AdminSwitchError, OSError, subprocess.TimeoutExpired, UnicodeError, RuntimeError) as exc:
            if is_interruption(exc):
                raise
            admin_status = "manual_review_required"
            admin_error = safe_error(exc)
            admin_cause = getattr(exc, "cause_code", None)
    cli_error = ("CLI_OBSERVATION_INCOMPLETE" if not cli.summary["cli_observation_complete"] else
                 "CLI_SESSIONS_CHANGED" if not cli.summary["cli_sessions_preserved"] else None)
    return {"status": "ready" if admin_status == "admin_ready" and catalog.get("tools_registered") is True
                              and catalog.get("connector_schemas_verified") is True
                              and cli.summary["cli_sessions_preserved"]
                     else "verification_required",
            "source_files": len(source_before), "installed": True,
            "switched": True, "snapshot": installed["snapshot"],
            "live_snapshot": actual["snapshot"], "previous_snapshot": before["snapshot"],
            "runtime_verified": True, "tunnel_healthy": True, "admin_status": admin_status,
            "schema_013_applied": False, **_live_fields(actual),
            **catalog, **cli.summary,
            **({"code": admin_error or cli_error} if admin_error or cli_error else {}),
            **({"cause_code": admin_cause} if isinstance(admin_cause, str)
               and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", admin_cause) else {})}


class RolloutTerminated(RuntimeError):
    def __init__(self):
        super().__init__("ROLLOUT_TERMINATED")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="Interactive install and tunnel switch")
    parser.add_argument("--tunnel-id", help="Only if tunnel-client status cannot expose the existing id")
    parser.add_argument("--diagnostics-dir", type=Path, default=RECEIPT_ROOT,
                        help="Private directory for durable, sanitized rollout receipts")
    pause = parser.add_mutually_exclusive_group()
    pause.add_argument("--pause-on-error", action="store_true", default=None,
                       help="Keep an interactive terminal open until Enter after failure (default with --apply)")
    pause.add_argument("--no-pause-on-error", dest="pause_on_error", action="store_false",
                       help="Disable the interactive failure pause")
    args = parser.parse_args(argv)
    try:
        receipt = RolloutReceipt(args.diagnostics_dir, mode="apply" if args.apply else "preflight")
    except (OSError, ValueError, RuntimeError):
        print("ERROR: ROLLOUT_RECEIPT_UNAVAILABLE", file=sys.stderr)
        return 1
    print("Diagnostic receipt: " + str(receipt.path), flush=True)
    previous_term = signal.getsignal(signal.SIGTERM)
    def terminated(signum, frame):
        raise RolloutTerminated()
    signal.signal(signal.SIGTERM, terminated)
    exit_code = 1
    try:
        with receipt.phase("rollout"):
            # Interactive and web-approved updates share one owner-only transition lock.
            if args.apply:
                from runtime_rollout_web import rollout_lock
            with (rollout_lock() if args.apply else nullcontext()):
                result = rollout(apply=args.apply, tunnel_id=args.tunnel_id, receipt=receipt)
        exit_code = 0 if result["status"] in ("ready", "preflight_ok") else 2
        receipt.finish(result, exit_code)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if (result["status"] == "verification_required" and result.get("admin_status") == "admin_ready"
                and result.get("runtime_verified") is True):
            print("Host runtime verified; refresh ChatGPT plugin metadata and validate the new tools in a new chat.", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        code = safe_error(exc)
        exit_code = 143 if isinstance(exc, RolloutTerminated) else 130 if isinstance(exc, KeyboardInterrupt) else 1
        failure = {"status": "failed", "code": code}
        cause = getattr(exc, "cause_code", None)
        if isinstance(cause, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", cause):
            failure["cause_code"] = cause
        rollback = getattr(exc, "rollback_status", None)
        if rollback in ("succeeded", "failed"):
            failure["rollback_status"] = rollback
        try:
            receipt.finish(failure, exit_code)
        except (Exception, KeyboardInterrupt):
            print("ERROR: ROLLOUT_RECEIPT_WRITE_FAILED", file=sys.stderr, flush=True)
        print("ERROR: " + code + "; receipt: " + str(receipt.path), file=sys.stderr, flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    pause_on_error = args.apply if args.pause_on_error is None else args.pause_on_error
    if exit_code in (1, 2) and pause_on_error and sys.stdin.isatty() and sys.stdout.isatty():
        print("Rollout stopped. The diagnostic receipt is retained. Press Enter to close.", flush=True)
        sys.stdin.readline()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
