"""Trusted host-only preparation and activation of one review-only Codex profile.

Preparation records immutable intent and performs no probe or model call. Applying a plan
requires exact typed confirmation plus a separate paid-smoke opt-in, then runs the pinned
offline proofs and a one-use approved disposable smoke before changing host configuration.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import os
from pathlib import Path
import re
import secrets
import stat

from agent_probe import (
    PINNED_CODEX_SHA256, PINNED_CODE_MODE_HOST_SHA256, PINNED_VERSION, probe_codex_isolation,
    probe_supervisor_fixture, stage_codex_runtime,
)
from agent_profiles import profile_digest, validate_agent_profiles
from agent_proofs import (
    SMOKE_ASSERTIONS, SUPERVISION_ASSERTIONS, record_receipt,
    source_contract_digest, verify_receipt,
)
from host_registry import validate_payload
from host_runtime_config import (
    MAX_CONFIG_BYTES, HostRuntimeError, is_registered_config, private_dir,
    private_file, read_config, save_config_cas, validate_state_root,
)
from host_runtime_operations import (
    _digest, _lock, _read_private_json, _save_private_json, _timestamp,
    _verified_installation, utc_now,
)

PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")
PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
PLAN_TTL_SECONDS = 600
PLAN_KEYS = frozenset({"version", "plan_id", "plan_digest", "created_at", "immutable"})
IMMUTABLE_KEYS = frozenset({
    "version", "plan_id", "config_sha256", "source_contract_digest",
    "source_executable", "source_executable_sha256", "runtime_destination",
    "profile_id", "profile", "created_at", "expires_at",
})


def _safe_code(error: Exception) -> str:
    code = getattr(error, "code", None) or str(error)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else \
        "AGENT_PROFILE_SETUP_FAILED"


def _source_sha256(path: Path) -> str:
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise HostRuntimeError("CODEX_SOURCE_UNSAFE") from None
    try:
        opened = os.fstat(descriptor)
        if (path.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid() or not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise HostRuntimeError("CODEX_SOURCE_UNSAFE")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise HostRuntimeError("CODEX_SOURCE_CHANGED")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _safe_allowed_directory(project_root: Path, relative: str) -> None:
    """Reject links at every component instead of following a linked parent."""
    if not isinstance(relative, str):
        raise HostRuntimeError("INVALID_AGENT_PROFILE")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise HostRuntimeError("INVALID_AGENT_PROFILE")
    current = project_root
    try:
        for part in candidate.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise HostRuntimeError("UNSAFE_AGENT_PATH")
    except OSError:
        raise HostRuntimeError("UNSAFE_AGENT_PATH") from None


def _await(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


class AgentProfileSetup:
    def __init__(self, projects, *, config_path, state_dir, runtime_root, receipts_dir,
                 source_executable, stage_runtime=stage_codex_runtime,
                 isolation_probe=probe_codex_isolation,
                 supervisor_probe=probe_supervisor_fixture,
                 receipt_recorder=record_receipt, receipt_verifier=verify_receipt,
                 smoke_runner=None, smoke_objective=None, smoke_fixture_input=None,
                 contract_digest=source_contract_digest, trusted_runtime=None):
        self.projects = {name: Path(root).resolve(strict=True)
                         for name, root in projects.items()}
        self.config_path = Path(config_path).absolute()
        self.root = validate_state_root(Path(state_dir), self.projects)
        self.plans_dir = private_dir(self.root / "plans")
        self.failures_dir = private_dir(self.root / "failures")
        self.approvals_dir = private_dir(self.root / "smoke-approvals")
        self.fixtures_dir = private_dir(self.root / "fixtures")
        self.lock = self.root / "setup.lock"
        self.runtime_root = validate_state_root(Path(runtime_root), self.projects)
        self.receipts_dir = validate_state_root(Path(receipts_dir), self.projects)
        self.source_executable = Path(source_executable).absolute()
        for project_root in self.projects.values():
            if (self.source_executable.is_relative_to(project_root)
                    or project_root.is_relative_to(self.source_executable)):
                raise HostRuntimeError("CODEX_SOURCE_UNSAFE")
        self.stage_runtime = stage_runtime
        self.isolation_probe = isolation_probe
        self.supervisor_probe = supervisor_probe
        self.receipt_recorder = receipt_recorder
        self.receipt_verifier = receipt_verifier
        self.contract_digest = contract_digest
        self.trusted_runtime = (_verified_installation() if trusted_runtime is None
                                else trusted_runtime is True)
        (self.smoke_runner, self.smoke_objective,
         self.smoke_fixture_input) = self._resolve_smoke(
             smoke_runner, smoke_objective, smoke_fixture_input)

    @staticmethod
    def _resolve_smoke(runner, objective, fixture_input):
        if runner is not None:
            if not isinstance(objective, str) or not objective:
                raise HostRuntimeError("AGENT_SMOKE_RUNNER_UNAVAILABLE")
            if fixture_input is None:
                fixture_input = b"approved disposable fixture input\n"
            if not isinstance(fixture_input, bytes) or not fixture_input:
                raise HostRuntimeError("AGENT_SMOKE_RUNNER_UNAVAILABLE")
            return runner, objective, fixture_input
        try:
            from agent_smoke import FIXTURE_INPUT, SMOKE_OBJECTIVE, run_approved_fixture
        except ImportError:
            return None, None, None
        return run_approved_fixture, SMOKE_OBJECTIVE, FIXTURE_INPUT

    def _raw_config(self) -> tuple[bytes, str]:
        try:
            private_file(self.config_path)
            descriptor = os.open(self.config_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                raw = source.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise HostRuntimeError("INVALID_HOST_CONFIG")
        except OSError:
            raise HostRuntimeError("INVALID_HOST_CONFIG") from None
        return raw, hashlib.sha256(raw).hexdigest()

    def _config_snapshot(self) -> tuple[dict, bytes, str]:
        raw, digest = self._raw_config()
        config = read_config(self.config_path)
        if not is_registered_config(config) or not config["enabled"]:
            raise HostRuntimeError("REGISTRY_NOT_CONFIGURED")
        validate_payload(config, self.projects)
        return config, raw, digest

    def _plan_path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or PLAN_ID.fullmatch(plan_id) is None:
            raise HostRuntimeError("INVALID_AGENT_PROFILE_PLAN")
        return self.plans_dir / (plan_id + ".json")

    def _load_plan(self, plan_id: str) -> dict:
        plan = _read_private_json(self._plan_path(plan_id))
        immutable = plan.get("immutable")
        if (set(plan) != PLAN_KEYS or plan.get("version") != 1
                or not isinstance(immutable, dict) or set(immutable) != IMMUTABLE_KEYS
                or immutable.get("version") != 1 or plan.get("plan_id") != plan_id
                or immutable.get("plan_id") != plan_id
                or plan.get("plan_digest") != _digest(immutable)):
            raise HostRuntimeError("INVALID_AGENT_PROFILE_PLAN")
        self._require_current_plan(plan)
        return plan

    @staticmethod
    def _require_current_plan(plan: dict) -> None:
        if _timestamp(plan["immutable"]["expires_at"]) <= datetime.now(timezone.utc):
            raise HostRuntimeError("AGENT_PROFILE_PLAN_EXPIRED")

    def _public(self, plan: dict) -> dict:
        immutable = plan["immutable"]
        profile = immutable["profile"]
        return {
            "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
            "project": profile["project"], "profile_id": immutable["profile_id"],
            "model": profile["model"], "reasoning_effort": profile["reasoning_effort"],
            "allowed_paths": list(profile["allowed_paths"]), "apply_policy": "review_only",
            "host_capabilities": [], "source_executable_sha256":
                immutable["source_executable_sha256"],
            "source_contract_digest": immutable["source_contract_digest"],
            "expires_at": immutable["expires_at"], "model_smoke_required": True,
            "profile": copy.deepcopy(profile),
        }

    def prepare(self, *, profile_id, project, model, reasoning_effort, allowed_paths,
                host_capabilities=None) -> dict:
        if host_capabilities not in (None, []):
            raise HostRuntimeError("UNSUPPORTED_AGENT_CAPABILITY")
        if (not isinstance(profile_id, str) or PROFILE_ID.fullmatch(profile_id) is None
                or project not in self.projects or not isinstance(allowed_paths, list)):
            raise HostRuntimeError("INVALID_AGENT_PROFILE")
        config, _, config_sha = self._config_snapshot()
        if profile_id in config["agent_profiles"]:
            raise HostRuntimeError("AGENT_PROFILE_EXISTS")
        for relative in allowed_paths:
            _safe_allowed_directory(self.projects[project], relative)
        source_sha = _source_sha256(self.source_executable)
        if source_sha != PINNED_CODEX_SHA256:
            raise HostRuntimeError("CODEX_SOURCE_CHANGED")
        plan_id = secrets.token_hex(16)
        destination = self.runtime_root / f"codex-{PINNED_VERSION}-{plan_id}"
        if destination.exists() or destination.is_symlink():
            raise HostRuntimeError("CODEX_RUNTIME_EXISTS")
        profile = {
            "project": project, "provider": "codex",
            "executable": str(destination / "codex"),
            "executable_sha256": PINNED_CODEX_SHA256, "version": PINNED_VERSION,
            "model": model, "reasoning_effort": reasoning_effort,
            "allowed_paths": list(allowed_paths), "host_capabilities": [],
            "apply_policy": "review_only", "isolation_receipt": None,
            "smoke_receipt": None, "enabled": True,
        }
        validate_agent_profiles({profile_id: profile}, self.projects)
        now = datetime.now(timezone.utc)
        immutable = {
            "version": 1, "plan_id": plan_id, "config_sha256": config_sha,
            "source_contract_digest": self.contract_digest(),
            "source_executable": str(self.source_executable),
            "source_executable_sha256": source_sha,
            "runtime_destination": str(destination), "profile_id": profile_id,
            "profile": profile, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
        }
        plan = {"version": 1, "plan_id": plan_id, "plan_digest": _digest(immutable),
                "created_at": now.isoformat(), "immutable": immutable}
        with _lock(self.lock):
            _save_private_json(self._plan_path(plan_id), plan)
        return self._public(plan)

    def _failure(self, plan_id: str, error: Exception) -> None:
        _save_private_json(
            self.failures_dir / f"{plan_id}-{secrets.token_hex(8)}.json",
            {"version": 1, "plan_id": plan_id, "phase": "apply",
             "code": _safe_code(error), "recorded_at": utc_now()},
        )

    @staticmethod
    def _supervision_evidence(result: object) -> dict:
        if not isinstance(result, dict):
            raise HostRuntimeError("SUPERVISOR_FIXTURE_FAILED")
        evidence = {name: result.get(name) for name in SUPERVISION_ASSERTIONS}
        if any(value is not True for value in evidence.values()):
            raise HostRuntimeError("SUPERVISOR_FIXTURE_FAILED")
        return evidence

    def _write_smoke_approval(self, plan, profile, isolation_receipt, workspace):
        task_id = secrets.token_hex(16)
        now = datetime.now(timezone.utc)
        approval = {
            "version": 1, "kind": "profile_smoke",
            "plan_digest": plan["plan_digest"],
            "profile_digest": profile_digest(profile), "workspace": str(workspace),
            "task_id": task_id,
            "objective_digest": hashlib.sha256(self.smoke_objective.encode()).hexdigest(),
            "timeout_minutes": 1, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=2)).isoformat(),
            "isolation_receipt": isolation_receipt,
            "receipts_dir": str(self.receipts_dir),
        }
        path = private_dir(self.approvals_dir / task_id) / "smoke-approval.json"
        _save_private_json(path, approval)
        return path

    def apply(self, plan_id, *, confirmation, approve_model_smoke=False) -> dict:
        if confirmation != plan_id:
            raise HostRuntimeError("HOST_APPROVAL_DENIED")
        if approve_model_smoke is not True:
            raise HostRuntimeError("MODEL_SMOKE_APPROVAL_REQUIRED")
        if self.smoke_runner is None or not self.smoke_objective:
            raise HostRuntimeError("AGENT_SMOKE_RUNNER_UNAVAILABLE")
        approval_path = None
        try:
            if not self.trusted_runtime:
                raise HostRuntimeError("HOST_RUNTIME_NOT_INSTALLED")
            with _lock(self.lock):
                plan = self._load_plan(plan_id)
                immutable = plan["immutable"]
                if self._raw_config()[1] != immutable["config_sha256"]:
                    raise HostRuntimeError("HOST_CONFIG_CHANGED")
                config, _, config_sha = self._config_snapshot()
                if config_sha != immutable["config_sha256"]:
                    raise HostRuntimeError("HOST_CONFIG_CHANGED")
                if immutable["source_contract_digest"] != self.contract_digest():
                    raise HostRuntimeError("AGENT_SOURCE_CHANGED")
                if (_source_sha256(self.source_executable)
                        != immutable["source_executable_sha256"]):
                    raise HostRuntimeError("CODEX_SOURCE_CHANGED")
                destination = Path(immutable["runtime_destination"])
                if destination.exists() or destination.is_symlink():
                    raise HostRuntimeError("CODEX_RUNTIME_EXISTS")
                staged = self.stage_runtime(
                    self.source_executable, immutable["source_executable_sha256"], destination)
                profile = copy.deepcopy(immutable["profile"])
                if (staged.get("executable") != profile["executable"]
                        or staged.get("version") != profile["version"]
                        or staged.get("executable_sha256") != profile["executable_sha256"]
                        or staged.get("code_mode_helper") != str(destination / "codex-code-mode-host")
                        or staged.get("code_mode_helper_sha256") != PINNED_CODE_MODE_HOST_SHA256):
                    raise HostRuntimeError("CODEX_RUNTIME_CHANGED")

                fixture = private_dir(self.fixtures_dir / plan_id)
                isolation = self.isolation_probe(profile, fixture / "isolation")
                assertions = isolation.get("assertions") if isinstance(isolation, dict) else None
                supervision = self._supervision_evidence(
                    _await(self.supervisor_probe(fixture / "supervision")))
                isolation_receipt = self.receipt_recorder(
                    self.receipts_dir, profile, "isolation",
                    {"assertions": assertions, "supervision": supervision})
                profile["isolation_receipt"] = isolation_receipt

                smoke_workspace = private_dir(fixture / "smoke-workspace")
                smoke_input = smoke_workspace / "fixture-input.txt"
                descriptor = os.open(
                    smoke_input, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(self.smoke_fixture_input)
                    output.flush()
                    os.fsync(output.fileno())
                approval_path = self._write_smoke_approval(
                    plan, profile, isolation_receipt, smoke_workspace)
                smoke = _await(self.smoke_runner(
                    profile, smoke_workspace, approval_path, plan_digest=plan["plan_digest"]))
                if approval_path.exists() or approval_path.is_symlink():
                    raise HostRuntimeError("SMOKE_APPROVAL_NOT_CONSUMED")
                if (not isinstance(smoke, dict) or set(smoke) != SMOKE_ASSERTIONS
                        or any(value is not True for value in smoke.values())):
                    raise HostRuntimeError("MODEL_SMOKE_FAILED")
                smoke_receipt = self.receipt_recorder(
                    self.receipts_dir, profile, "smoke", smoke)
                profile["smoke_receipt"] = smoke_receipt
                if (not self.receipt_verifier(
                        self.receipts_dir, isolation_receipt, profile, "isolation")
                        or not self.receipt_verifier(
                            self.receipts_dir, smoke_receipt, profile, "smoke")):
                    raise HostRuntimeError("AGENT_PROOF_UNVERIFIED")

                self._require_current_plan(plan)
                if self._raw_config()[1] != immutable["config_sha256"]:
                    raise HostRuntimeError("HOST_CONFIG_CHANGED")
                current, _, current_sha = self._config_snapshot()
                if current_sha != immutable["config_sha256"]:
                    raise HostRuntimeError("HOST_CONFIG_CHANGED")
                if immutable["source_contract_digest"] != self.contract_digest():
                    raise HostRuntimeError("AGENT_SOURCE_CHANGED")
                if immutable["profile_id"] in current["agent_profiles"]:
                    raise HostRuntimeError("AGENT_PROFILE_EXISTS")
                candidate = copy.deepcopy(current)
                candidate["agent_profiles"][immutable["profile_id"]] = profile
                validate_payload(candidate, self.projects)
                save_config_cas(
                    self.config_path, candidate,
                    expected_sha256=immutable["config_sha256"])
                return {"status": "profile_enabled", "project": profile["project"],
                        "profile_id": immutable["profile_id"],
                        "model": profile["model"],
                        "reasoning_effort": profile["reasoning_effort"],
                        "isolation_receipt": isolation_receipt,
                        "smoke_receipt": smoke_receipt}
        except Exception as exc:
            safe_plan_id = (plan_id if isinstance(plan_id, str)
                            and PLAN_ID.fullmatch(plan_id) is not None else "invalid")
            self._failure(safe_plan_id, exc)
            raise
        finally:
            if approval_path is not None:
                try:
                    approval_path.unlink(missing_ok=True)
                except OSError:
                    pass
