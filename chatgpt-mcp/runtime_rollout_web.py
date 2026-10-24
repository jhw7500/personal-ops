"""Durable, one-use web authorization for an exact personal-ops runtime rollout.

All approvals use the existing authenticated admin origin/token gate. Executions
run in a separate systemd user unit so replacing the MCP and admin services
cannot kill the worker. No caller-supplied shell, executable, argv or path.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess

import host_access
from host_runtime_config import HostRuntimeError, TRUSTED_APPROVAL_ORIGIN, private_dir
from host_runtime_operations import (_digest, _lock, _read_private_json,
                                     _save_private_json, _verified_installation,
                                     acquire_flock)
from workspace_control import (DEFAULT_CONTROLLER_DIR, WorkspaceControlError,
                               activation_admission, transition_release,
                               verify_controller, verify_launch)

PROJECT = "personal-ops"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
PLAN_TTL = timedelta(days=7)
QUEUE_RECOVERY_AFTER = timedelta(minutes=2)
RUN_RECOVERY_AFTER = timedelta(minutes=22)
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_FILES = 500
EXTENSIONS = frozenset({".py", ".js", ".html", ".css", ".json", ".yaml", ".yml",
                        ".c", ".mjs", ".txt"})
OMIT_DIRS = frozenset({".git", ".venv", ".serena", ".pytest_cache",
                      "__pycache__", "node_modules", "dist", "build"})
CONTROL = ("rollout-host-runtime.py", "rollout_test_runner.py", "rollout_tunnel.py",
           "rollout_admin.py", "rollout_catalog.py", "rollout_diagnostics.py",
           "install-runtime.py", "configure-host-runtime.py")
RUNNER_NAME = "runtime_rollout_worker.py"
TERMINAL = frozenset({"succeeded", "failed", "verification_required",
                       "outcome_unknown", "expired", "cancelled"})
UNIT = re.compile(r"personal-ops-runtime-rollout-[0-9a-f]{32}\.service\Z")
UNIT_FIELDS = ("Id", "LoadState", "Transient", "ActiveState", "SubState", "Result")
BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
APPROVAL_CONTEXT_FIELDS = frozenset({"version", "runtime_identity", "boot_id", "uid"})


def _utc():
    return datetime.now(timezone.utc)


@contextmanager
def rollout_lock():
    """Serialize legacy interactive rollouts and owner-web unit rollouts."""
    root = private_dir(
        Path.home() / ".local/state/chatgpt-coding-mcp/runtime-rollout-web"
    )
    fd = acquire_flock(root / "global-rollout.lock")
    try:
        yield
    finally:
        os.close(fd)


def _expires(value):
    try:
        date = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HostRuntimeError("ROLLOUT_PLAN_INVALID") from None
    if date.tzinfo is None:
        raise HostRuntimeError("ROLLOUT_PLAN_INVALID")
    return date.astimezone(timezone.utc)


def _observe_unit(unit: str) -> dict:
    """Read one exact transient rollout unit without starting or stopping it."""
    if not isinstance(unit, str) or not UNIT.fullmatch(unit):
        raise HostRuntimeError("ROLLOUT_UNIT_UNVERIFIED")
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    command = ["/usr/bin/systemctl", "--user", "--no-ask-password", "show", unit,
               *["--property=" + name for name in UNIT_FIELDS]]
    environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "XDG_RUNTIME_DIR": runtime,
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + runtime + "/bus"}
    try:
        result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=8, check=False)
        if len(result.stdout) > 65536:
            raise ValueError()
        text = result.stdout.decode("ascii")
        lines = text.splitlines()
        if any("=" not in line for line in lines):
            raise ValueError()
        fields = dict(line.split("=", 1) for line in lines)
        if len(fields) != len(lines) or set(fields) != set(UNIT_FIELDS):
            raise ValueError()
        missing = fields["LoadState"] == "not-found"
        if not missing and (result.returncode != 0 or fields["Id"] != unit
                            or fields["Transient"] != "yes"):
            raise ValueError()
        return {"unit": unit, "load_state": fields["LoadState"],
                "transient": fields["Transient"] == "yes",
                "active_state": fields["ActiveState"], "sub_state": fields["SubState"],
                "result": fields["Result"]}
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        raise HostRuntimeError("ROLLOUT_UNIT_UNVERIFIED") from None


def _boot_id() -> str:
    try:
        fd = os.open("/proc/sys/kernel/random/boot_id", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            value = stream.read(64).decode("ascii").strip()
        if not BOOT_ID.fullmatch(value):
            raise ValueError()
        return value
    except (OSError, UnicodeError, ValueError):
        raise HostRuntimeError("ROLLOUT_UNIT_UNVERIFIED") from None


def _source_bytes(path: Path, root: Path) -> tuple[bytes, int]:
    """Read a stable source fd; refuse symlink ancestors or changed contents."""
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        before = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_size > MAX_FILE_BYTES
                or before.st_mode & (stat.S_ISUID | stat.S_ISGID)):
            raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
            chunks = []
            total = 0
            while True:
                data = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - total))
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if total > MAX_FILE_BYTES:
                    raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
            after = os.fstat(fd)
            if (after.st_size != before.st_size or total != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or after.st_ctime_ns != before.st_ctime_ns):
                raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
        finally:
            os.close(fd)
        return b"".join(chunks), 0o700 if before.st_mode & stat.S_IXUSR else 0o600
    except HostRuntimeError:
        raise
    except (OSError, ValueError):
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE") from None


def _source_names(root: Path) -> list[str]:
    source = root / "chatgpt-mcp"
    dashboard = root / "work-dashboard"
    if not source.is_dir() or not dashboard.is_dir() or source.is_symlink() or dashboard.is_symlink():
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    files = []
    for directory, folders, names in os.walk(source, followlinks=False):
        parent = Path(directory)
        allowed_folders = []
        for folder in folders:
            if folder in OMIT_DIRS or folder.startswith("."):
                continue
            if (parent / folder).is_symlink():
                raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
            allowed_folders.append(folder)
        folders[:] = sorted(allowed_folders)
        for name in sorted(names):
            path = parent / name
            if name.startswith(".") or path.suffix.lower() not in EXTENSIONS:
                continue
            if path.is_symlink():
                raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
            files.append(path.relative_to(root).as_posix())
    for path in sorted(dashboard.iterdir()):
        if path.suffix.lower() not in EXTENSIONS:
            continue
        files.append(path.relative_to(root).as_posix())
    if not 40 <= len(files) <= MAX_FILES:
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    required = ("chatgpt-mcp/rollout-host-runtime.py",
                "chatgpt-mcp/install-runtime.py",
                "chatgpt-mcp/server.py",
                "chatgpt-mcp/runtime_rollout_web.py",
                "chatgpt-mcp/runtime_rollout_worker.py")
    if not all(item in files for item in required):
        raise HostRuntimeError("ROLLOUT_SOURCE_INCOMPLETE")
    return sorted(files)


def _catalog(root: Path, *, copy_to: Path | None = None):
    root = Path(root).resolve(strict=True)
    records = []
    total = 0
    for name in _source_names(root):
        data, mode = _source_bytes(root / name, root)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        records.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data)})
        if copy_to is not None:
            dest = copy_to / name
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), mode)
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {"digest": hashlib.sha256(canonical).hexdigest(),
            "files": records, "count": len(records), "bytes": total}


def _static_tuple(root: Path, label: str) -> tuple[str, ...]:
    path = root / "chatgpt-mcp/install-runtime.py"
    data, _mode = _source_bytes(path, root)
    try:
        tree = ast.parse(data)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == label
                                                    for t in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, tuple) and all(isinstance(x, str) for x in value):
                    return value
    except (SyntaxError, ValueError, TypeError):
        pass
    raise HostRuntimeError("ROLLOUT_SOURCE_INCOMPLETE")


def _expected_snapshots(root: Path) -> tuple[str, str]:
    """Compute digests using the official rollout's public stable formats."""
    files = _static_tuple(root, "FILES")
    dash = _static_tuple(root, "DASHBOARD_FILES")
    if len(set(files)) != len(files) or len(set(dash)) != len(dash):
        raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
    manifest = {}
    for filename in files:
        if filename.startswith("/") or ".." in Path(filename).parts:
            raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        data, _ = _source_bytes(root / "chatgpt-mcp" / filename, root)
        manifest[filename] = hashlib.sha256(data).hexdigest()
    for filename in dash:
        if filename.startswith("/") or ".." in Path(filename).parts:
            raise HostRuntimeError("ROLLOUT_SOURCE_UNSAFE")
        data, _ = _source_bytes(root / "work-dashboard" / filename, root)
        manifest["work-dashboard/" + filename] = hashlib.sha256(data).hexdigest()
    target = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
    control = {}
    for name in CONTROL:
        data, _ = _source_bytes(root / "chatgpt-mcp" / name, root)
        control[name] = hashlib.sha256(data).hexdigest()
    for path in sorted((root / "chatgpt-mcp/tests").glob("test_*.py")):
        data, _ = _source_bytes(path, root)
        control["tests/" + path.name] = hashlib.sha256(data).hexdigest()
    # Match the official rollout's standalone approval UI checks exactly.
    for name in ("check_host_access_approval_ui.py", "check_schema014_approval_ui.py",
                 "check_host_command_approval_ui.py"):
        ui_check = root / "chatgpt-mcp/tests" / name
        data, _ = _source_bytes(ui_check, root)
        control["tests/" + ui_check.name] = hashlib.sha256(data).hexdigest()
    tests_dir = root / "chatgpt-mcp/tests"
    required_node_ui = tests_dir / "test_workspace_approval_ui.mjs"
    if not required_node_ui.is_file() or required_node_ui.is_symlink():
        raise HostRuntimeError("ROLLOUT_SOURCE_INCOMPLETE")
    node_ui_inputs = [required_node_ui]
    node_ui_inputs.extend(sorted(
        path for path in tests_dir.glob("test_*.mjs") if path != required_node_ui
    ))
    for path in node_ui_inputs:
        data, _ = _source_bytes(path, root)
        control["tests/" + path.name] = hashlib.sha256(data).hexdigest()
    control_digest = hashlib.sha256(json.dumps(control, sort_keys=True).encode()).hexdigest()
    return target, control_digest


class WebRolloutGrant:
    """Process-local proof returned only by an atomic approved-plan claim."""
    def __init__(self, operation_id: str, source_digest: str, target_snapshot: str,
                 control_digest: str, state_root: Path):
        self.operation_id = operation_id
        self.source_digest = source_digest
        self.target_snapshot = target_snapshot
        self.control_digest = control_digest
        self.state_root = state_root

    def verify(self, root: Path, installer, inventory_fn, controls_fn) -> None:
        """Require a *consumed* approval owned by THIS worker before any effect."""
        if not isinstance(self.operation_id, str) or not HEX32.fullmatch(self.operation_id):
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
        try:
            plan = _read_private_json(
                self.state_root / "plans" / (self.operation_id + ".json"))
            fixed = plan["immutable"]
            if (plan["state"] != "running" or plan.get("worker_pid") != os.getpid()
                    or fixed.get("source_digest") != self.source_digest
                    or fixed.get("target_snapshot") != self.target_snapshot
                    or fixed.get("control_digest") != self.control_digest
                    or plan.get("plan_digest") != _digest(fixed)):
                raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID")
        except (OSError, HostRuntimeError, KeyError, TypeError):
            raise RuntimeError("WEB_ROLLOUT_GRANT_INVALID") from None
        actual_source = _catalog(root)
        expected, control = _expected_snapshots(root)
        official = inventory_fn(installer)
        official_manifest = {name.removeprefix("chatgpt-mcp/"): digest
                             for name, digest in official.items()}
        observed_snapshot = hashlib.sha256(
            json.dumps(official_manifest, sort_keys=True).encode()
        ).hexdigest()[:20]
        observed_controls = hashlib.sha256(
            json.dumps(controls_fn(), sort_keys=True).encode()
        ).hexdigest()
        if (actual_source["digest"] != self.source_digest
                or expected != self.target_snapshot
                or control != self.control_digest
                or observed_snapshot != self.target_snapshot
                or observed_controls != self.control_digest):
            raise RuntimeError("WEB_ROLLOUT_SOURCE_CHANGED")


class RuntimeRolloutWeb:
    def __init__(self, projects: dict, *, state_dir: Path, launcher=None, observer=None,
                 boot_reader=None, runtime_reader=None, uid_reader=None, test_only=False):
        if PROJECT not in projects:
            raise HostRuntimeError("ROLLOUT_SCOPE_UNAVAILABLE")
        self.root = Path(projects[PROJECT]).resolve(strict=True)
        self.state_root = Path(state_dir).absolute() / "runtime-rollout-web"
        self.activation_authority_dir = self.state_root.parent / "workspace-authority"
        if self.state_root.resolve(strict=False).is_relative_to(self.root):
            raise HostRuntimeError("ROLLOUT_STATE_UNSAFE")
        private_dir(self.state_root)
        self.plans_dir = private_dir(self.state_root / "plans")
        self.snapshots_dir = private_dir(self.state_root / "snapshots")
        self.receipts_dir = private_dir(self.state_root / "receipts")
        self.lock = self.state_root / "rollout.lock"
        self.launcher = launcher
        self.observer = observer or _observe_unit
        self.boot_reader = boot_reader or _boot_id
        self.runtime_reader = runtime_reader or host_access._installed_runtime_identity
        self.uid_reader = uid_reader or os.getuid
        self._trusted = _verified_installation() or bool(test_only and launcher is not None)

    def _trusted_only(self):
        if not self._trusted:
            raise HostRuntimeError("UNSAFE_RUNTIME")

    def _plan_path(self, operation_id):
        if not isinstance(operation_id, str) or not HEX32.fullmatch(operation_id):
            raise HostRuntimeError("INVALID_OPERATION_ID")
        return self.plans_dir / (operation_id + ".json")

    def _load(self, operation_id):
        plan = _read_private_json(self._plan_path(operation_id))
        fixed = plan.get("immutable")
        if (not isinstance(fixed, dict) or plan.get("operation_id") != operation_id
                or plan.get("project") != PROJECT
                or plan.get("plan_digest") != _digest(fixed)
                or fixed.get("operation_id") != operation_id):
            raise HostRuntimeError("ROLLOUT_PLAN_INVALID")
        if "approval_context" in fixed:
            self._stored_approval_context(plan)
        return plan

    def _save(self, plan):
        _save_private_json(self._plan_path(plan["operation_id"]), plan)

    @staticmethod
    def _validated_approval_context(value):
        if (not isinstance(value, dict) or set(value) != APPROVAL_CONTEXT_FIELDS
                or type(value.get("version")) is not int or value["version"] != 1
                or type(value.get("uid")) is not int
                or value["uid"] < 0
                or not isinstance(value.get("runtime_identity"), str)
                or HEX64.fullmatch(value["runtime_identity"]) is None
                or not isinstance(value.get("boot_id"), str)
                or BOOT_ID.fullmatch(value["boot_id"]) is None):
            raise HostRuntimeError("ROLLOUT_PLAN_INVALID")
        return value

    def _stored_approval_context(self, plan):
        fixed = plan["immutable"]
        if "approval_context" not in fixed:
            return None
        return self._validated_approval_context(fixed["approval_context"])

    def _current_approval_context(self):
        try:
            value = {
                "version": 1,
                "runtime_identity": self.runtime_reader(),
                "boot_id": self.boot_reader(),
                "uid": self.uid_reader(),
            }
        except HostRuntimeError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HostRuntimeError("UNSAFE_RUNTIME") from None
        try:
            return dict(self._validated_approval_context(value))
        except HostRuntimeError:
            raise HostRuntimeError("UNSAFE_RUNTIME") from None

    def _approval_context_is_current(self, plan):
        expected = self._stored_approval_context(plan)
        return expected is not None and expected == self._current_approval_context()

    def _verify_receipt(self, plan):
        fixed = plan["immutable"]
        expected = {
            "operation_id": plan["operation_id"],
            "plan_digest": plan["plan_digest"],
            "nonce": fixed["nonce"],
        }
        try:
            receipt = _read_private_json(
                self.receipts_dir / (plan["operation_id"] + ".json")
            )
            approved_at = _expires(receipt["approved_at"])
            if (set(receipt) != {*expected, "approved_at"}
                    or any(receipt.get(key) != value for key, value in expected.items())
                    or not _expires(plan["created_at"]) <= approved_at <= _utc()
                    or approved_at >= _expires(fixed["expires_at"])):
                raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED")
        except (OSError, KeyError, TypeError, ValueError, HostRuntimeError):
            raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED") from None
        return receipt

    def verify_approval(self, plan):
        if not self._approval_context_is_current(plan):
            raise HostRuntimeError("ROLLOUT_APPROVAL_STALE")
        return self._verify_receipt(plan)

    def _reconcile_approval_context(self, plan):
        if plan.get("state") not in {"pending_host_approval", "approved"}:
            return plan
        if self._approval_context_is_current(plan):
            return plan
        plan.update(state="expired", phase="APPROVAL", code="ROLLOUT_APPROVAL_STALE",
                    finished_at=_utc().isoformat())
        self._save(plan)
        return plan

    @staticmethod
    def _public(plan):
        fixed = plan["immutable"]
        return {"project": PROJECT, "operation_id": plan["operation_id"],
                "state": plan["state"], "phase": plan["phase"], "code": plan.get("code"),
                "source_digest": fixed["source_digest"],
                "plan_digest": plan["plan_digest"],
                "target_snapshot": fixed["target_snapshot"],
                "control_digest": fixed["control_digest"],
                "source_files": fixed["source_files"],
                "source_bytes": fixed["source_bytes"],
                "created_at": plan["created_at"], "expires_at": fixed["expires_at"],
                "started_at": plan.get("started_at"), "finished_at": plan.get("finished_at"),
                "result": plan.get("result"),
                "outcome_requires_review": plan["state"] in {"outcome_unknown", "verification_required"},
                "execution_overdue": (plan["state"] in {"queued", "running"}
                    and isinstance(plan.get("started_at"), str)
                    and (_utc() - _expires(plan["started_at"])).total_seconds() > 1320),
                "approval_required": plan["state"] == "pending_host_approval",
                "approval_card": {"tool": "open_runtime_rollout_approval", "arguments": {
                    "project": PROJECT, "operation_id": plan["operation_id"]}}
                    if plan["state"] in {"pending_host_approval", "approved"} else None,
                "impact": {"restart": ["personal-ops-tunnel.service", "personal-ops-admin.service"],
                           "preserve": ["API", "Worker", "PostgreSQL", "Toss TEST bundle"]},
                "approval_url": TRUSTED_APPROVAL_ORIGIN + "/runtime-rollouts?operation_id="
                + plan["operation_id"]
                if plan["state"] == "pending_host_approval" else None}

    def _reconcile(self, plan):
        """Conservatively settle only stale workers proven absent by their exact unit."""
        plan = self._reconcile_approval_context(plan)
        state = plan.get("state")
        if state not in {"queued", "running"}:
            return plan
        try:
            started = _expires(plan.get("started_at"))
        except HostRuntimeError:
            return plan
        age = _utc() - started
        threshold = QUEUE_RECOVERY_AFTER if state == "queued" else RUN_RECOVERY_AFTER
        if age <= threshold:
            return plan
        unit = "personal-ops-runtime-rollout-" + plan["operation_id"] + ".service"
        try:
            current_boot = self.boot_reader()
            observed = self.observer(unit)
        except (HostRuntimeError, OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            return plan
        if (not isinstance(observed, dict) or observed.get("unit") != unit
                or observed.get("load_state") not in {"loaded", "not-found"}
                or type(observed.get("transient")) is not bool
                or not isinstance(observed.get("active_state"), str)
                or not isinstance(observed.get("sub_state"), str)
                or not isinstance(observed.get("result"), str)):
            return plan
        terminated = (observed["load_state"] == "not-found"
                      or observed["load_state"] == "loaded" and observed["transient"]
                      and observed["active_state"] in {"inactive", "failed"})
        if not terminated:
            return plan
        receipt = self.receipts_dir / (plan["operation_id"] + ".json")
        receipt_present = receipt.exists() and not receipt.is_symlink()
        launch_boot = plan.get("launch_boot_id")
        if not isinstance(launch_boot, str) or not BOOT_ID.fullmatch(launch_boot):
            return plan
        if state == "queued" and receipt_present:
            plan.update(state="failed", phase="RECOVER", code="ROLLOUT_WORKER_NOT_STARTED")
        elif state == "running" and receipt_present:
            plan.update(state="failed", phase="RECOVER", code="ROLLOUT_WORKER_NOT_STARTED")
        else:
            if launch_boot != current_boot:
                code = "ROLLOUT_HOST_RESTARTED"
            elif observed["result"] == "timeout":
                code = "ROLLOUT_WORKER_TIMEOUT"
            elif observed["load_state"] == "not-found":
                code = "ROLLOUT_WORKER_MISSING"
            else:
                code = "ROLLOUT_WORKER_INTERRUPTED"
            plan.update(state="outcome_unknown", phase="RECOVER",
                        code=code)
        plan["finished_at"] = _utc().isoformat()
        self._save(plan)
        return plan

    def _check_source(self, plan):
        fixed = plan["immutable"]
        frozen = self.snapshots_dir / plan["operation_id"]
        if frozen.is_symlink() or not frozen.is_dir():
            raise HostRuntimeError("ROLLOUT_SNAPSHOT_MISSING")
        snapshots = [_catalog(self.root), _catalog(frozen)]
        if any(catalog["digest"] != fixed["source_digest"] for catalog in snapshots):
            raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
        current_target, current_control = _expected_snapshots(self.root)
        frozen_target, frozen_control = _expected_snapshots(frozen)
        if (current_target != fixed["target_snapshot"] or frozen_target != current_target
                or current_control != fixed["control_digest"] or frozen_control != current_control):
            raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
        return frozen

    def _retire_source_stale_unstarted(self, plan):
        if plan["state"] not in {"pending_host_approval", "approved"}:
            return False
        if _expires(plan["immutable"]["expires_at"]) <= _utc():
            return False
        try:
            self._check_source(plan)
        except HostRuntimeError as exc:
            if exc.code != "ROLLOUT_SOURCE_CHANGED":
                raise
            plan.update(state="expired", phase="APPROVAL",
                        code="ROLLOUT_SOURCE_CHANGED", finished_at=_utc().isoformat())
            self._save(plan)
            (self.receipts_dir / (plan["operation_id"] + ".json")).unlink(missing_ok=True)
            return True
        return False

    def prepare(self, project: str, request_id: str):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._prepare_admitted(project, request_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _prepare_admitted(self, project: str, request_id: str):
        self._trusted_only()
        if project != PROJECT or not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        from host_runtime_operations import _lock
        with _lock(self.lock):
            for path in self.plans_dir.glob("*.json"):
                existing = self._reconcile(self._load(path.stem))
                if existing.get("request_id") == request_id:
                    return self._public(existing)
                if self._retire_source_stale_unstarted(existing):
                    continue
                if existing["state"] in ("pending_host_approval", "approved", "queued",
                                         "running", "outcome_unknown"):
                    if (_expires(existing["immutable"]["expires_at"]) > _utc()
                            or existing["state"] in ("queued", "running", "outcome_unknown")):
                        raise HostRuntimeError("ROLLOUT_ALREADY_PENDING")
            if len(list(self.plans_dir.glob("*.json"))) >= 100:
                raise HostRuntimeError("ROLLOUT_HISTORY_FULL")
            operation_id = secrets.token_hex(16)
            frozen = self.snapshots_dir / operation_id
            frozen.mkdir(mode=0o700)
            try:
                base = _catalog(self.root, copy_to=frozen)
                staged = _catalog(frozen)
                if base["digest"] != staged["digest"]:
                    raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
                target, control = _expected_snapshots(frozen)
                if _catalog(self.root)["digest"] != base["digest"]:
                    raise HostRuntimeError("ROLLOUT_SOURCE_CHANGED")
                fixed = {"project": PROJECT, "operation_id": operation_id,
                         "source_digest": base["digest"],
                         "target_snapshot": target, "control_digest": control,
                         "source_files": base["count"], "source_bytes": base["bytes"],
                         "approval_context": self._current_approval_context(),
                         "nonce": secrets.token_hex(32),
                         "expires_at": (_utc() + PLAN_TTL).isoformat()}
                plan = {"project": PROJECT, "operation_id": operation_id,
                        "request_id": request_id, "state": "pending_host_approval",
                        "phase": "APPROVAL", "created_at": _utc().isoformat(),
                        "immutable": fixed, "plan_digest": _digest(fixed)}
                self._save(plan)
                return self._public(plan)
            except BaseException:
                shutil.rmtree(frozen, ignore_errors=True)
                raise

    def list(self, project: str):
        self._trusted_only()
        if project != PROJECT:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock):
            plans = [self._public(self._reconcile(self._load(path.stem)))
                     for path in sorted(self.plans_dir.glob("*.json"))]
        return {"project": PROJECT, "plans": plans[-20:]}

    def get(self, project: str, operation_id: str):
        self._trusted_only()
        if project != PROJECT:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock):
            return self._public(self._reconcile(self._load(operation_id)))

    def approve_web(self, project, *, operation_id, plan_digest, source_digest, confirmation):
        self._trusted_only()
        if project != PROJECT or confirmation != operation_id:
            raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED")
        if not isinstance(plan_digest, str) or not HEX64.fullmatch(plan_digest):
            raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED")
        with _lock(self.lock):
            plan = self._reconcile(self._load(operation_id))
            if (plan["state"] != "pending_host_approval" or plan["plan_digest"] != plan_digest
                    or plan["immutable"]["source_digest"] != source_digest
                    or _expires(plan["immutable"]["expires_at"]) <= _utc()):
                raise HostRuntimeError("ROLLOUT_APPROVAL_DENIED")
            self._check_source(plan)
            receipt = self.receipts_dir / (operation_id + ".json")
            expected = {"operation_id": operation_id, "plan_digest": plan_digest,
                        "nonce": plan["immutable"]["nonce"]}
            if receipt.exists() or receipt.is_symlink():
                self.verify_approval(plan)
            else:
                fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
                with os.fdopen(fd, "w", encoding="utf8") as stream:
                    json.dump({**expected, "approved_at": _utc().isoformat()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            plan.update(state="approved", phase="READY")
            self._save(plan)
            return {"project": PROJECT, "operation_id": operation_id,
                    "approved": True, "executed": False}

    def execute(self, project: str, operation_id: str):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._execute_admitted(project, operation_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _execute_admitted(self, project: str, operation_id: str):
        self._trusted_only()
        if project != PROJECT:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        with _lock(self.lock):
            plan = self._reconcile(self._load(operation_id))
            if plan.get("code") == "ROLLOUT_APPROVAL_STALE":
                raise HostRuntimeError("ROLLOUT_APPROVAL_STALE")
            if plan["state"] in TERMINAL | {"queued", "running"}:
                return self._public(plan)
            if (plan["state"] != "approved"
                    or _expires(plan["immutable"]["expires_at"]) <= _utc()):
                raise HostRuntimeError("ROLLOUT_NOT_APPROVED")
            self._check_source(plan)
            uid = os.getuid()
            bus = Path(f"/run/user/{uid}/bus")
            if self.launcher is None and not bus.exists():
                raise HostRuntimeError("ROLLOUT_USER_BUS_UNAVAILABLE")
            python_binary = Path.home() / ".local/share/uv/tools/serena-agent/bin/python"
            node_binary = Path.home() / ".nvm/versions/node/v24.12.0/bin/node"
            if self.launcher is None and (not python_binary.is_file()
                                          or not os.access(python_binary, os.X_OK)
                                          or not node_binary.is_file()
                                          or not os.access(node_binary, os.X_OK)):
                raise HostRuntimeError("ROLLOUT_RUNTIME_NOT_READY")
            self.verify_approval(plan)
            plan.update(state="queued", phase="QUEUED", started_at=_utc().isoformat(),
                        launch_boot_id=self.boot_reader())
            self._save(plan)
        uid = os.getuid()
        runtime = Path(f"/run/user/{uid}")
        # Use the same inspected Serena interpreter as the official rollout,
        # not a possibly de-virtualized sys.executable from the admin process.
        python = Path.home() / ".local/share/uv/tools/serena-agent/bin/python"
        node = Path.home() / ".nvm/versions/node/v24.12.0/bin"
        env_path = str(node) + ":/usr/local/bin:/usr/bin:/bin"
        worker = Path(__file__).resolve().with_name(RUNNER_NAME)
        authority_dir = self.state_root.parent / "workspace-authority"
        try:
            admission = verify_launch(worker.parent, authority_dir)
            worker_argv = [str(python), "-I", str(worker), operation_id]
            if admission["authority_active"]:
                controller = verify_controller(DEFAULT_CONTROLLER_DIR)
                transition_release(worker.parent, authority_dir)
                worker_argv = [
                    str(python), "-I", controller["launcher"],
                    "--authority-dir", admission["authority_dir"],
                    "--release", str(worker.parent), "--interpreter", str(python),
                    "--isolated", "--entrypoint", RUNNER_NAME, "--", operation_id,
                ]
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc
        command = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect",
                   "--unit=personal-ops-runtime-rollout-" + operation_id,
                   "--property=Type=exec", "--property=TimeoutStartSec=900",
                   "--property=RuntimeMaxSec=1200",
                   "--setenv=PATH=" + env_path,
                   "--setenv=XDG_RUNTIME_DIR=" + str(runtime),
                   "--setenv=XDG_STATE_HOME=" + str(self.state_root.parent.parent),
                   "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=" + str(runtime / "bus"),
                   *worker_argv]
        def start():
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=20, check=False)
            return result.returncode == 0
        try:
            started = self.launcher(command) if self.launcher else start()
        except (OSError, subprocess.TimeoutExpired):
            started = False
        if not started:
            # Submission might have raced with the systemd manager; no automatic retry.
            with _lock(self.lock):
                state = self._load(operation_id)
                if state["state"] == "queued":
                    state.update(state="outcome_unknown", phase="RECOVER",
                                 code="ROLLOUT_LAUNCH_NOT_VERIFIED")
                    self._save(state)
        return self.get(project, operation_id)

    def claim_for_worker(self, operation_id: str):
        try:
            with activation_admission(self.activation_authority_dir, create=True):
                return self._claim_for_worker_admitted(operation_id)
        except WorkspaceControlError as exc:
            raise HostRuntimeError(exc.code) from exc

    def _claim_for_worker_admitted(self, operation_id: str):
        self._trusted_only()
        with _lock(self.lock):
            plan = self._load(operation_id)
            if plan["state"] != "queued":
                raise HostRuntimeError("ROLLOUT_ALREADY_CONSUMED")
            if _expires(plan["immutable"]["expires_at"]) <= _utc():
                plan.update(state="expired", phase="PRECHECK", code="ROLLOUT_APPROVAL_EXPIRED",
                            finished_at=_utc().isoformat())
                self._save(plan)
                raise HostRuntimeError("ROLLOUT_APPROVAL_EXPIRED")
            try:
                self._check_source(plan)
                self.verify_approval(plan)
            except HostRuntimeError as exc:
                # The independent worker has not claimed or executed any side
                # effect yet. Record an explicit failed preflight, not a
                # permanently queued operation with no way to reconcile it.
                plan.update(state="failed", phase="PRECHECK", code=exc.code,
                            finished_at=_utc().isoformat())
                self._save(plan)
                raise
            plan.update(state="running", phase="PRECHECK", worker_pid=os.getpid())
            self._save(plan)
            (self.receipts_dir / (operation_id + ".json")).unlink()
            fixed = plan["immutable"]
            return self.snapshots_dir / operation_id, WebRolloutGrant(
                operation_id, fixed["source_digest"], fixed["target_snapshot"],
                fixed["control_digest"], self.state_root)

    def finish_from_worker(self, operation_id, result: dict):
        self._trusted_only()
        with _lock(self.lock):
            plan = self._load(operation_id)
            if plan["state"] != "running":
                raise HostRuntimeError("ROLLOUT_ALREADY_CONSUMED")
            kind = result.get("status")
            if kind not in {"ready", "verification_required", "failed", "outcome_unknown"}:
                raise HostRuntimeError("ROLLOUT_RESULT_INVALID")
            plan.update(state="succeeded" if kind == "ready" else kind,
                        phase="COMPLETE", finished_at=_utc().isoformat(),
                        result={key: value for key, value in result.items()
                                if key in {"status", "snapshot", "previous_snapshot",
                                           "installed", "switched", "tools_registered",
                                           "connector_schemas_verified", "code"}
                                and isinstance(value, (str, bool))})
            self._save(plan)
            return self._public(plan)
