#!/usr/bin/env python3
"""Independent, approved personal-ops runtime rollout worker.

Only the owner-approved, one-use plan ID is accepted. Intended to run under a
dedicated transient systemd --user unit, not within the MCP/admin service cgroup.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys

# -I prevents loading the user's Python startup modules. Resolve only the
# verified release containing this worker and the operation manager.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Isolated script execution needs the sibling module path before these imports.
from runtime_rollout_web import RuntimeRolloutWeb, rollout_lock  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from host_runtime_operations import _verified_installation  # noqa: E402
from workspace_control import (  # noqa: E402
    WorkspaceControlError, activation_admission, verify_launch,
)


def _load_frozen_rollout(file: Path, operation_id: str):
    """Load approved rollout helpers without replacing installed modules.

    The installed manager and its genuine WebRolloutGrant must remain cached:
    they own the claimed plan. Frozen v3 target/layout/authority helpers instead
    live in a one-operation namespace rooted at the approved snapshot.
    """
    if (not isinstance(operation_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None):
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    file = Path(file)
    package_name = "_approved_runtime_rollout_" + operation_id
    module_name = package_name + ".host_runtime"
    if any(name == package_name or name.startswith(package_name + ".")
           for name in sys.modules):
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    package_spec = importlib.util.spec_from_loader(
        package_name, loader=None, is_package=True)
    if package_spec is None:
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    package = importlib.util.module_from_spec(package_spec)
    package.__path__ = [str(file.parent)]
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        target = getattr(module, "_rollout_target", None)
        authority = getattr(module, "_rollout_authority", None)
        if (getattr(target, "__name__", None) != package_name + ".rollout_target"
                or getattr(authority, "__name__", None)
                != package_name + ".rollout_authority"
                or getattr(authority, "verify_web_rollout_grant", None)
                is not getattr(target, "verify_web_rollout_grant", None)):
            raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        return module
    except BaseException:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)
        raise


def _safe_failure_code(exc):
    candidates = (getattr(exc, "code", None), str(exc))
    for value in candidates:
        if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", value):
            return value
    return "ROLLOUT_FAILED"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        verify_launch(HERE)
    except WorkspaceControlError:
        return 125
    if (len(args) != 1 or not re.fullmatch(r"[0-9a-f]{32}", args[0])
            or not _verified_installation()):
        return 125
    operation_id = args[0]
    # This root is owned by the host user and NOT contained in a project.
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    state_dir = root / "chatgpt-coding-mcp"
    try:
        with activation_admission(
                state_dir / "workspace-authority", create=True):
            from server import CONFIG, load_projects
            projects = load_projects(CONFIG)
            manager = RuntimeRolloutWeb(projects, state_dir=state_dir)
            frozen, grant = manager.claim_for_worker(operation_id)
    except Exception:
        return 125

    result = {"status": "failed", "code": "ROLLOUT_FAILED"}
    receipt = None
    try:
        file = frozen / "chatgpt-mcp" / "rollout-host-runtime.py"
        if not file.is_file() or file.is_symlink():
            raise HostRuntimeError("ROLLOUT_SNAPSHOT_MISSING")
        module = _load_frozen_rollout(file, operation_id)
        receipt = module.RolloutReceipt(mode="apply")
        with rollout_lock():
            with receipt.phase("rollout"):
                outcome = module.rollout(apply=True, approved_web=grant, receipt=receipt)
        code = 0 if outcome.get("status") == "ready" else 2
        receipt.finish(outcome, code)
        result = {"status": "ready" if code == 0 else "verification_required",
                  **{k: v for k, v in outcome.items()
                     if k in {"snapshot", "previous_snapshot", "installed", "switched",
                              "tools_registered", "connector_schemas_verified", "code"}
                     and isinstance(v, (str, bool))}}
    except BaseException as exc:
        safe = _safe_failure_code(exc)
        # If installer/tunnel/admin effects may have started, never claim a
        # clean failure or replay. Human reconciliation is required.
        events = receipt.data.get("events", []) if receipt is not None else []
        possible_effect = any(event.get("stage") in {
            "snapshot_install", "tunnel_switch", "admin_switch"
        } and event.get("state") in {"started", "completed", "failed"}
            for event in events)
        result = {"status": "outcome_unknown" if possible_effect else "failed",
                  "code": safe}
        if receipt is not None:
            try:
                receipt.finish({"status": "failed", "code": safe}, 1)
            except Exception:
                pass
    try:
        manager.finish_from_worker(operation_id, result)
    except Exception:
        # Fail closed: the durable plan will remain 'running', never 'succeeded'.
        return 126
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
