"""Source-bound rollout intent and explicit approval control contracts.

The legacy installed worker may have already imported runtime_rollout_web.
This module therefore relies only on that module's stable source readers, not
on a new signature or a patched installed verifier. A v1 approval still binds
its original controls and the entire source snapshot; the new official control
inventory must independently equal the complete current inventory before any effect.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from host_runtime_config import HostRuntimeError
from host_runtime_operations import _digest, _read_private_json
if __package__ and __package__.startswith("_approved_runtime_rollout_"):
    from .rollout_layout import LayoutError, select_layout, validate_layout
else:
    from rollout_layout import LayoutError, select_layout, validate_layout


CONTROL_CONTRACT_VERSION = 3
CONTROL_V1 = ("rollout-host-runtime.py", "rollout_test_runner.py", "rollout_tunnel.py",
              "rollout_admin.py", "rollout_catalog.py", "rollout_diagnostics.py",
              "install-runtime.py", "configure-host-runtime.py")
CONTROL_V2 = (*CONTROL_V1, "rollout_layout.py", "rollout_target.py", "rollout_authority.py")
CONTROL_V1_TOSS_BOOTSTRAP = (*CONTROL_V1, "tossapp_neutral_ab24_command.js")
CONTROL_V3 = (*CONTROL_V2, "tossapp_neutral_ab24_command.js")
UI_CHECKS_V1 = ("check_host_access_approval_ui.py", "check_schema014_approval_ui.py",
                "check_host_command_approval_ui.py")
UI_CHECKS_V2 = (*UI_CHECKS_V1, "check_development_approval_ui.py")
TARGET_FILE = "rollout-target.json"
LAYOUT_NAMES = frozenset({"legacy", "wsl-pc1"})


def control_contract_version(fixed):
    """An absent version is the exact old contract, never a future default."""
    version = fixed.get("control_contract_version", 1)
    if (type(version) is not int or version not in {1, 2, 3}
            or version == 1 and "target_layout" in fixed
            or version in {2, 3}
            and (not isinstance(fixed.get("target_layout"), str)
                 or fixed["target_layout"] not in LAYOUT_NAMES)):
        raise HostRuntimeError("ROLLOUT_CONTROL_CONTRACT_INVALID")
    return version


def _source_readers():
    # During the first upgrade this intentionally resolves the already loaded
    # installed module. These three signatures and formats predate v2.
    from runtime_rollout_web import _catalog, _source_bytes, _static_tuple
    return _catalog, _source_bytes, _static_tuple


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def resolve_rollout_target(root: Path, *, pinned_layout=None, layout_reader=None):
    """Read only this source tree's exact intent, then require the actual layout.

    The JSON selects a known layout enum. It cannot supply service names,
    commands, profile paths, or authority. Without an intent file only the
    historical legacy target is allowed, including for unversioned approvals.
    Callers executing a web approval must pass its frozen snapshot root.
    """
    root = Path(root)
    source = root / "chatgpt-mcp"
    path = source / TARGET_FILE
    try:
        if root.is_symlink() or source.is_symlink() or not source.is_dir():
            raise ValueError()
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ValueError()
            _catalog, read, _static = _source_readers()
            raw, _mode = read(path, root)
            if len(raw) > 1024:
                raise ValueError()
            intent = json.loads(raw.decode("utf8"), object_pairs_hook=_unique_object)
            if (not isinstance(intent, dict) or set(intent) != {"version", "layout"}
                    or type(intent["version"]) is not int or intent["version"] != 1
                    or not isinstance(intent["layout"], str)
                    or intent["layout"] not in LAYOUT_NAMES):
                raise ValueError()
            name = intent["layout"]
        else:
            name = "legacy"
    except (OSError, ValueError, UnicodeError, HostRuntimeError):
        raise HostRuntimeError("ROLLOUT_TARGET_INVALID") from None
    if pinned_layout is not None:
        if not isinstance(pinned_layout, str) or pinned_layout not in LAYOUT_NAMES:
            raise HostRuntimeError("ROLLOUT_TARGET_INVALID")
        if pinned_layout != name:
            raise HostRuntimeError("ROLLOUT_TARGET_MISMATCH")
    try:
        actual = validate_layout((layout_reader or select_layout)())
    except LayoutError as exc:
        raise HostRuntimeError(exc.code) from exc
    if actual["name"] != name:
        raise HostRuntimeError("ROLLOUT_TARGET_MISMATCH")
    return actual


def control_inventory(root: Path, *, contract_version=CONTROL_CONTRACT_VERSION):
    """Independently enumerate the reviewed controls for exactly v1, v2 or v3."""
    if type(contract_version) is not int or contract_version not in {1, 2, 3}:
        raise HostRuntimeError("ROLLOUT_CONTROL_CONTRACT_INVALID")
    root = Path(root)
    _catalog, read, _static = _source_readers()
    source = root / "chatgpt-mcp"
    names = {1: CONTROL_V1, 2: CONTROL_V2, 3: CONTROL_V3}[contract_version]
    checks = UI_CHECKS_V1 if contract_version == 1 else UI_CHECKS_V2
    paths = [source / name for name in names]
    tests = source / "tests"
    paths.extend(sorted(tests.glob("test_*.py")))
    paths.extend(tests / name for name in checks)
    required_node = tests / "test_workspace_approval_ui.mjs"
    if not required_node.is_file() or required_node.is_symlink():
        raise HostRuntimeError("ROLLOUT_SOURCE_INCOMPLETE")
    paths.append(required_node)
    paths.extend(sorted(path for path in tests.glob("test_*.mjs") if path != required_node))
    result = {}
    for path in paths:
        raw, _mode = read(path, root)
        result[path.relative_to(source).as_posix()] = hashlib.sha256(raw).hexdigest()
    return result


def _runtime_manifest(root: Path):
    """Reconstruct the complete reviewed installer manifest from exact source."""
    root = Path(root)
    _catalog, read, static = _source_readers()
    files = static(root, "FILES")
    dash = static(root, "DASHBOARD_FILES")
    if len(set(files)) != len(files) or len(set(dash)) != len(dash):
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    manifest = {}
    for directory, names, prefix in (("chatgpt-mcp", files, ""),
                                      ("work-dashboard", dash, "work-dashboard/")):
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts:
                raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
            raw, _mode = read(root / directory / name, root)
            manifest[prefix + name] = hashlib.sha256(raw).hexdigest()
    return manifest


def expected_rollout_snapshots(root: Path, *, contract_version=CONTROL_CONTRACT_VERSION):
    """Use the stable installer format plus the explicitly selected controls."""
    manifest = _runtime_manifest(root)
    target = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    controls = control_inventory(root, contract_version=contract_version)
    control = hashlib.sha256(json.dumps(controls, sort_keys=True).encode()).hexdigest()
    return target, control


def _bootstrap_v1_control_digest(root: Path):
    """Compute the one named buildserver v1 inventory that included Toss JS."""
    source = Path(root) / "chatgpt-mcp"
    _catalog, read, _static = _source_readers()
    regular = control_inventory(root, contract_version=1)
    path = source / "tossapp_neutral_ab24_command.js"
    raw, _mode = read(path, Path(root))
    regular[path.relative_to(source).as_posix()] = hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(json.dumps(regular, sort_keys=True).encode()).hexdigest()


def approved_control_digest(root: Path, fixed: dict):
    """Accept exact versioned digest or the sole named unversioned v1 bootstrap."""
    version = control_contract_version(fixed)
    _target, expected = expected_rollout_snapshots(root, contract_version=version)
    approved = fixed.get("control_digest")
    if approved == expected:
        return expected
    if version == 1 and approved == _bootstrap_v1_control_digest(root):
        return approved
    raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")


def verify_web_rollout_grant(grant, root: Path, installer, inventory_fn, controls_fn,
                             *, layout_reader=None):
    """Verify a consumed old/new grant against its exact frozen source and current gates.

    The caller retains its genuine WebRolloutGrant type check. This function
    does not mint, claim, approve, rewrite, or consume any approval state.
    """
    operation_id = getattr(grant, "operation_id", None)
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
    try:
        state_root = Path(grant.state_root)
        frozen = state_root / "snapshots" / operation_id
        root = Path(root).absolute()
        if (root != frozen.absolute() or root.is_symlink()
                or root.resolve(strict=True) != root):
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
        plan = _read_private_json(state_root / "plans" / (operation_id + ".json"))
        fixed = plan["immutable"]
        version = control_contract_version(fixed)
        source_mode = fixed.get("source_mode", "project")
        source_policy_digest = fixed.get("source_policy_digest", "0" * 64)
        metadata_names = ("source_mode", "source_policy_digest",
                          "control_contract_version", "target_layout")
        metadata_present = tuple(hasattr(grant, name) for name in metadata_names)
        if (source_mode not in {"project", "owner_scoped"}
                or not isinstance(source_policy_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", source_policy_digest) is None
                or source_mode == "project" and source_policy_digest != "0" * 64
                or source_mode == "owner_scoped" and source_policy_digest == "0" * 64
                or any(metadata_present) and not all(metadata_present)):
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
        if all(metadata_present):
            metadata_matches = (
                source_mode == grant.source_mode
                and source_policy_digest == grant.source_policy_digest
                and version == grant.control_contract_version
                and fixed.get("target_layout") == grant.target_layout)
        else:
            # The installed v1/v2 managers minted the genuine five-field grant.
            # V1 compatibility is limited to the named Toss bootstrap inventory;
            # v2 is already explicitly versioned and target-bound by its plan.
            metadata_matches = (
                version == 2
                or version == 1
                and "control_contract_version" not in fixed
                and "target_layout" not in fixed
                and fixed.get("control_digest") == _bootstrap_v1_control_digest(root))
        if (plan["state"] != "running" or plan.get("worker_pid") != os.getpid()
                or plan.get("operation_id") != operation_id or plan.get("project") != "personal-ops"
                or fixed.get("operation_id") != operation_id or fixed.get("project") != "personal-ops"
                or fixed.get("source_digest") != grant.source_digest
                or fixed.get("target_snapshot") != grant.target_snapshot
                or fixed.get("control_digest") != grant.control_digest
                or not metadata_matches
                or plan.get("plan_digest") != _digest(fixed)):
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
    except (OSError, HostRuntimeError, KeyError, TypeError, AttributeError, ValueError):
        raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID") from None
    catalog, _read, _static = _source_readers()
    if catalog(root)["digest"] != grant.source_digest:
        raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")
    expected, _original_control = expected_rollout_snapshots(root, contract_version=version)
    official = inventory_fn(installer)
    official_manifest = {name.removeprefix("chatgpt-mcp/"): digest
                         for name, digest in official.items()}
    observed_snapshot = hashlib.sha256(
        json.dumps(official_manifest, sort_keys=True).encode()).hexdigest()[:20]
    # Historical approvals retain their exact digest, while every execution
    # independently proves the complete current v3 controls.
    approved_control_digest(root, fixed)
    complete_controls = control_inventory(root, contract_version=3)
    if (expected != grant.target_snapshot or observed_snapshot != grant.target_snapshot
            or fixed.get("control_digest") != grant.control_digest
            or controls_fn() != complete_controls):
        raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")
    layout = resolve_rollout_target(root, pinned_layout=fixed.get("target_layout"),
                                   layout_reader=layout_reader)
    if catalog(root)["digest"] != grant.source_digest:
        raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")
    return layout
