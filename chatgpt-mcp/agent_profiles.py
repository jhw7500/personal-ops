"""Host-pinned Codex delegation profiles. No profile or receipt means no agent run.

The v3 host config is private operator input, not a chat-supplied permission grant.
Receipt identifiers refer to real owner-only host proof; this module cannot create them.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from host_runtime_config import HostRuntimeError, CONFIG_PATH, read_config, _no_parent_links

PROFILE_KEYS = frozenset({
    "project", "provider", "executable", "executable_sha256", "version",
    "model", "reasoning_effort", "allowed_paths", "host_capabilities",
    "apply_policy", "isolation_receipt", "smoke_receipt", "enabled",
})
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
MODEL_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z")
RELATIVE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
REASONING = frozenset({"low", "medium", "high", "xhigh", "max"})
MAX_PROFILES = 16
SENSITIVE_SOURCE_PARTS = frozenset({
    ".aws", ".claude", ".codex", ".config", ".env", ".git", ".ssh",
})

# This string is part of the receipt contract. Any runner permission, helper,
# or process-isolation change must rotate it and therefore invalidate old proof.
ISOLATION_CONTRACT_VERSION = "codex-0.162.1-inline-v4:transient-cgroup:static-bootstrap:private-proc:code-mode-host"
CODEX_PERMISSION_TEMPLATE = (
    'permissions.delegated={filesystem={":root"="deny",":minimal"="read",'
    '":tmpdir"="deny",":slash_tmp"="deny",__RUNTIME_PATHS__,glob_scan_max_depth=3,'
    '":workspace_roots"={"."="write",".git/**"="deny",".env"="deny",'
    '"**/.env"="deny",".codex/**"="deny",".aws/**"="deny",'
    '".claude/**"="deny",".config/**"="deny",".ssh/**"="deny",'
    '".agent-control/**"="deny"}},'
    'network={enabled=false}}'
)


def _valid_relative(name: object) -> bool:
    return (isinstance(name, str) and RELATIVE.fullmatch(name) is not None
            and all(part not in {".", ".."} | SENSITIVE_SOURCE_PARTS
                    and not part.startswith(".env.") for part in name.split("/")))


def validate_agent_profiles(profiles: object, projects: dict | None = None) -> None:
    """Exact-key, non-executable config validation shared with the v3 registry."""
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise HostRuntimeError("INVALID_HOST_CONFIG")
    for identifier, profile in profiles.items():
        if (not isinstance(identifier, str) or not PROFILE_NAME.fullmatch(identifier)
                or not isinstance(profile, dict) or set(profile) != PROFILE_KEYS):
            raise HostRuntimeError("INVALID_HOST_CONFIG")
        paths = profile["allowed_paths"]
        executable = profile["executable"]
        if (not isinstance(profile["project"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", profile["project"])
                or projects is not None and profile["project"] not in projects
                or profile["provider"] != "codex"
                or not isinstance(executable, str)
                or not executable.startswith("/")
                or len(executable) > 512
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/_-." for c in executable)
                or any(p in ("", ".", "..") for p in executable[1:].split("/"))
                or not isinstance(profile["executable_sha256"], str)
                or not HEX64.fullmatch(profile["executable_sha256"])
                or not isinstance(profile["version"], str)
                or not VERSION.fullmatch(profile["version"])
                or not isinstance(profile["model"], str)
                or not MODEL_ID.fullmatch(profile["model"])
                or profile["reasoning_effort"] not in REASONING
                or type(profile["enabled"]) is not bool
                or profile["apply_policy"] not in {"review_only", "cas"}
                or not isinstance(paths, list) or not (1 <= len(paths) <= 16)
                or any(not _valid_relative(p) for p in paths)
                or len(set(paths)) != len(paths)
                or any(a != b and b.startswith(a + "/") for a in paths for b in paths)
                # Until a host broker is independently verified, NO Docker/DB capability.
                or profile["host_capabilities"] != []):
            raise HostRuntimeError("INVALID_HOST_CONFIG")
        for key in ("isolation_receipt", "smoke_receipt"):
            value = profile[key]
            if value is not None and (not isinstance(value, str) or not HEX64.fullmatch(value)):
                raise HostRuntimeError("INVALID_HOST_CONFIG")


def profile_digest(profile: dict) -> str:
    """Bind receipts to exact model/effort/path/runtime/policy without secrets."""
    payload = {k: v for k, v in profile.items() if k not in {"isolation_receipt", "smoke_receipt"}}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def sandbox_helper(profile: dict) -> Path:
    executable = Path(profile["executable"])
    helper = executable.parent / "codex-linux-sandbox"
    if helper.is_symlink() or not helper.is_file():
        raise HostRuntimeError("SANDBOX_HELPER_UNVERIFIED")
    return helper


def code_mode_host_digest(profile: dict) -> str:
    """Verify the exact bundled helper; neither PATH nor old receipts are proof."""
    from agent_probe import PINNED_CODE_MODE_HOST_SHA256, ProbeError, _runtime_file
    helper = Path(profile["executable"]).parent / "codex-code-mode-host"
    try:
        _no_parent_links(helper)
        parent = helper.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise HostRuntimeError("CODE_MODE_HOST_UNVERIFIED")
        return _runtime_file(helper, PINNED_CODE_MODE_HOST_SHA256)
    except (OSError, ProbeError, HostRuntimeError):
        raise HostRuntimeError("CODE_MODE_HOST_UNVERIFIED") from None


def codex_permission_policy(profile: dict) -> str:
    executable = Path(profile["executable"])
    helper = sandbox_helper(profile)
    uid = os.getuid()
    runtime_paths = (f'"{helper.parent}"="read","{executable}"="read",'
                     f'"/run/user/{uid}"="deny","/sys/fs/cgroup"="deny",'
                     '"/proc"="deny"')
    return CODEX_PERMISSION_TEMPLATE.replace("__RUNTIME_PATHS__", runtime_paths)


def isolation_contract_digest(profile: dict) -> str:
    """Bind proof to the exact runner policy and relevant pinned profile inputs."""
    payload = {
        "contract": ISOLATION_CONTRACT_VERSION,
        "permission_policy": codex_permission_policy(profile),
        "profile_digest": profile_digest(profile),
        "executable_sha256": profile.get("executable_sha256"),
        "version": profile.get("version"),
        "model": profile.get("model"),
        "reasoning_effort": profile.get("reasoning_effort"),
        "sandbox_helper_sha256": hashlib.sha256(sandbox_helper(profile).read_bytes()).hexdigest(),
        "code_mode_helper_sha256": code_mode_host_digest(profile),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class AgentProfileRegistry:
    def __init__(self, projects: dict, *, config_path=CONFIG_PATH, receipts_dir=None):
        self.projects = {name: Path(root).resolve(strict=True) for name, root in projects.items()}
        self.config_path = Path(config_path)
        self.receipts_dir = (Path(receipts_dir) if receipts_dir is not None else
                             Path.home() / ".local/state/chatgpt-coding-mcp/agent-probes")

    def _profiles(self) -> dict:
        config = read_config(self.config_path)
        if (not isinstance(config, dict) or config.get("version") != 3
                or not config.get("enabled")):
            return {}
        profiles = config.get("agent_profiles", {})
        validate_agent_profiles(profiles, self.projects)
        return profiles

    def _proof(self, receipt_id, profile, kind) -> bool:
        from agent_proofs import verify_receipt
        return verify_receipt(self.receipts_dir, receipt_id, profile, kind)

    def readiness(self, profile: dict) -> str | None:
        if not profile["enabled"]:
            return "PROFILE_DISABLED"
        # CAS remains a config-compatible future policy. It cannot become runnable
        # until process descendants and filesystem compare-and-swap are proven.
        if profile["apply_policy"] == "cas":
            return "CAS_UNVERIFIED"
        binary = Path(profile["executable"])
        try:
            if binary.is_symlink() or not binary.is_file():
                return "EXECUTABLE_UNVERIFIED"
            if hashlib.sha256(binary.read_bytes()).hexdigest() != profile["executable_sha256"]:
                return "EXECUTABLE_CHANGED"
            try:
                sandbox_helper(profile)
            except HostRuntimeError:
                return "SANDBOX_HELPER_UNVERIFIED"
            try:
                code_mode_host_digest(profile)
            except HostRuntimeError:
                return "CODE_MODE_HOST_UNVERIFIED"
            from agent_bootstrap import BootstrapError, validate_bootstrap
            try:
                validate_bootstrap(binary.parent / 'personal-ops-agent-bootstrap')
            except BootstrapError:
                return 'BOOTSTRAP_UNVERIFIED'
            if not self._proof(profile["isolation_receipt"], profile, "isolation"):
                return "ISOLATION_UNVERIFIED"
            if not self._proof(profile["smoke_receipt"], profile, "smoke"):
                return "SMOKE_UNVERIFIED"
        except (OSError, ValueError):
            return "EXECUTABLE_UNVERIFIED"
        return None

    def list_profiles(self, project: str) -> dict:
        if project not in self.projects:
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        profiles = self._profiles()
        result = []
        for profile_id, profile in sorted(profiles.items()):
            if profile["project"] != project:
                continue
            reason = self.readiness(profile)
            result.append({"profile_id": profile_id, "provider": profile["provider"],
                           "model": profile["model"], "reasoning_effort": profile["reasoning_effort"],
                           "allowed_paths": list(profile["allowed_paths"]),
                           "ready": reason is None, "reason": reason})
        return {"project": project, "profiles": result}

    def require(self, project: str, profile_id: str) -> dict:
        if project not in self.projects or not isinstance(profile_id, str):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        profile = self._profiles().get(profile_id)
        if profile is None or profile["project"] != project:
            raise HostRuntimeError("PROFILE_NOT_FOUND")
        reason = self.readiness(profile)
        if reason is not None:
            raise HostRuntimeError(reason)
        return dict(profile)
