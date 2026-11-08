"""Bounded evidence for a completed server tools/list and post-install review.

This module proves only that one verified installed server process built and
returned a complete catalog. It deliberately cannot prove connector adoption,
metadata refresh, registration, or baseline eligibility.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from rollout_diagnostics import RolloutReceipt
from workspace_control import WorkspaceControlError, verify_release_manifest


HEX20 = re.compile(r"[0-9a-f]{20}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
MAX_TOOLS = 2048
MAX_JSON_BYTES = 8 * 1024 * 1024
PENDING_REASON = "connector_adoption_evidence_unavailable"


class RuntimeVerificationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: str
    pid: int
    start_ticks: int
    server_path: str
    uid: int


def _canonical(value) -> bytes:
    try:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError):
        raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID") from None
    if len(payload) > MAX_JSON_BYTES:
        raise RuntimeVerificationError("CATALOG_CONTRACT_OVERSIZED")
    return payload


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip().lower()
    except OSError:
        raise RuntimeVerificationError("BOOT_ID_UNAVAILABLE") from None
    if BOOT_ID.fullmatch(value) is None:
        raise RuntimeVerificationError("BOOT_ID_UNAVAILABLE")
    return value


def _open_directory(path: Path) -> int:
    absolute = Path(path).absolute()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in absolute.parts[1:]:
            current = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = current
        return descriptor
    except OSError:
        os.close(descriptor)
        raise RuntimeVerificationError("VERIFICATION_STATE_UNSAFE") from None


def _private_dir(path: Path) -> Path:
    path = Path(path).absolute()
    try:
        descriptor = _open_directory(path)
        info = os.fstat(descriptor)
        os.close(descriptor)
    except (OSError, RuntimeVerificationError):
        raise RuntimeVerificationError("VERIFICATION_STATE_UNSAFE") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeVerificationError("VERIFICATION_STATE_UNSAFE")
    return path


def _write_private_json(path: Path, value, *, exclusive=False) -> Path:
    parent = _private_dir(path.parent)
    payload = _canonical(value) + b"\n"
    directory = _open_directory(parent)
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            os.close(directory)
            raise RuntimeVerificationError("CATALOG_EVIDENCE_REPLAYED") from None
        except OSError:
            os.close(directory)
            raise RuntimeVerificationError("VERIFICATION_STATE_UNSAFE") from None
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        os.close(directory)
        return path
    temporary = "." + uuid.uuid4().hex
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError:
        raise RuntimeVerificationError("VERIFICATION_STATE_UNSAFE") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return path


def _read_private_json(path: Path, *, code="CATALOG_EVIDENCE_UNSAFE") -> tuple[dict, bytes]:
    directory = None
    descriptor = None
    try:
        directory = _open_directory(path.parent)
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        info = os.fstat(descriptor)
        opened_identity = (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_JSON_BYTES
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise OSError
        payload = b""
        while len(payload) <= MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > MAX_JSON_BYTES:
            raise OSError
        os.lseek(descriptor, 0, os.SEEK_SET)
        confirmed = b""
        while len(confirmed) <= MAX_JSON_BYTES:
            chunk = os.read(
                descriptor, min(65536, MAX_JSON_BYTES + 1 - len(confirmed)))
            if not chunk:
                break
            confirmed += chunk
        if confirmed != payload:
            raise OSError
        descriptor_after = os.fstat(descriptor)
        path_after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        descriptor_identity = (
            descriptor_after.st_dev, descriptor_after.st_ino, descriptor_after.st_size,
            descriptor_after.st_mtime_ns, descriptor_after.st_ctime_ns)
        path_identity = (
            path_after.st_dev, path_after.st_ino, path_after.st_size,
            path_after.st_mtime_ns, path_after.st_ctime_ns)
        if descriptor_identity != opened_identity or path_identity != opened_identity:
            raise OSError
        value = json.loads(payload)
    except (OSError, ValueError, TypeError, RecursionError, RuntimeVerificationError):
        raise RuntimeVerificationError(code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    if not isinstance(value, dict):
        raise RuntimeVerificationError(code)
    return value, payload


def _proc_identity(pid=None, *, boot_reader=_boot_id) -> ProcessIdentity:
    target_pid = os.getpid() if pid is None else pid
    if type(target_pid) is not int or target_pid <= 0:
        raise RuntimeVerificationError("RUNTIME_IDENTITY_INVALID")
    folder = Path("/proc") / str(target_pid)
    try:
        info = folder.stat()
        stat_payload = (folder / "stat").read_text()
        cmdline = (folder / "cmdline").read_bytes()
    except OSError:
        raise RuntimeVerificationError("RUNTIME_IDENTITY_UNAVAILABLE") from None
    if len(stat_payload) > 8192 or len(cmdline) > 65536 or info.st_uid != os.getuid():
        raise RuntimeVerificationError("RUNTIME_IDENTITY_INVALID")
    close = stat_payload.rfind(")")
    fields = stat_payload[close + 2:].split() if close > 0 else []
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        raise RuntimeVerificationError("RUNTIME_IDENTITY_INVALID") from None
    server_paths = []
    for raw in cmdline.rstrip(b"\0").split(b"\0"):
        try:
            argument = raw.decode()
        except UnicodeError:
            continue
        if argument.endswith("/server.py"):
            try:
                server_paths.append(str(Path(argument).resolve(strict=True)))
            except OSError:
                pass
    if len(server_paths) != 1:
        raise RuntimeVerificationError("RUNTIME_IDENTITY_INVALID")
    return ProcessIdentity(
        boot_id=boot_reader(), pid=target_pid, start_ticks=start_ticks,
        server_path=server_paths[0], uid=info.st_uid)


def _validated_identity(value: ProcessIdentity, release: Path, boot_id: str) -> ProcessIdentity:
    if (not isinstance(value, ProcessIdentity) or value.boot_id != boot_id
            or BOOT_ID.fullmatch(value.boot_id) is None or type(value.pid) is not int
            or value.pid <= 0 or type(value.start_ticks) is not int
            or value.start_ticks <= 0 or value.uid != os.getuid()
            or value.server_path != str(release / "server.py")):
        raise RuntimeVerificationError("RUNTIME_IDENTITY_INVALID")
    return value


def _tool_contract(tool) -> dict:
    if hasattr(tool, "model_dump"):
        try:
            value = tool.model_dump(by_alias=True)
        except (TypeError, ValueError):
            raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID") from None
    elif isinstance(tool, dict):
        value = tool
    else:
        raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID")
    if not isinstance(value, dict):
        raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID")
    contract = {
        "name": value.get("name"),
        "inputSchema": value.get("inputSchema"),
        "outputSchema": value.get("outputSchema"),
    }
    if (not isinstance(contract["name"], str) or not contract["name"]
            or len(contract["name"]) > 256
            or not isinstance(contract["inputSchema"], dict)
            or contract["outputSchema"] is not None
            and not isinstance(contract["outputSchema"], dict)):
        raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID")
    _canonical(contract)
    return contract


class CatalogEvidenceWriter:
    def __init__(self, release: Path, release_info: dict, evidence_dir: Path,
                 identity: ProcessIdentity):
        self.release = release
        self.release_info = release_info
        self.evidence_dir = evidence_dir
        self.identity = identity

    def __call__(self, tools, generation):
        if not isinstance(generation, str) or HEX64.fullmatch(generation) is None:
            raise RuntimeVerificationError("CATALOG_GENERATION_INVALID")
        if not isinstance(tools, (list, tuple)) or not 0 < len(tools) <= MAX_TOOLS:
            raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID")
        contracts = sorted((_tool_contract(tool) for tool in tools), key=lambda item: item["name"])
        names = [item["name"] for item in contracts]
        if len(names) != len(set(names)):
            raise RuntimeVerificationError("CATALOG_CONTRACT_INVALID")
        body = {
            "version": 1,
            "observed_at": _utc(),
            "release_path": str(self.release),
            "release_snapshot": self.release.name,
            "release_manifest_digest": self.release_info["release_digest"],
            "session_generation": generation,
            "process": asdict(self.identity),
            "tool_count": len(contracts),
            "tool_names": names,
            "tool_digests": {item["name"]: _digest(item) for item in contracts},
            "catalog_sha256": _digest(contracts),
        }
        evidence = {**body, "record_sha256": _digest(body)}
        return _write_private_json(
            self.evidence_dir / (generation + ".json"), evidence)


def catalog_observer_for_runtime(runtime_dir, state_dir, *, releases_root=None,
                                 boot_reader=_boot_id, process_reader=None):
    """Return an observer only for a manifest-verified installed release."""
    release = Path(runtime_dir).absolute()
    expected_root = Path(releases_root or
                         Path.home() / ".local/lib/chatgpt-coding-mcp/releases").absolute()
    try:
        if (release.parent != expected_root or HEX20.fullmatch(release.name) is None
                or release.is_symlink()):
            return None
        release_info = verify_release_manifest(release)
        if (not isinstance(release_info.get("release_digest"), str)
                or release_info["release_digest"][:20] != release.name
                or release_info.get("runtime") != str(release)):
            return None
        state = _private_dir(Path(state_dir))
        current_boot = boot_reader()
        reader = process_reader or (lambda pid=None: _proc_identity(pid, boot_reader=boot_reader))
        identity = _validated_identity(reader(None), release, current_boot)
        namespace = state / "runtime-verification"
        if not namespace.exists():
            namespace.mkdir(mode=0o700)
        _private_dir(namespace)
        evidence_dir = namespace / "catalog"
        if not evidence_dir.exists():
            evidence_dir.mkdir(mode=0o700)
        _private_dir(evidence_dir)
        return CatalogEvidenceWriter(release, release_info, evidence_dir, identity)
    except (RuntimeVerificationError, WorkspaceControlError, OSError, ValueError, TypeError):
        return None


def _timestamp(value, code):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise RuntimeVerificationError(code) from None


def _load_plan(operation_id: str, state_dir: Path):
    if not isinstance(operation_id, str) or HEX32.fullmatch(operation_id) is None:
        raise RuntimeVerificationError("INVALID_OPERATION_ID")
    path = state_dir / "runtime-rollout-web" / "plans" / (operation_id + ".json")
    plan, raw = _read_private_json(path, code="ROLLOUT_PLAN_INVALID")
    fixed = plan.get("immutable")
    result = plan.get("result")
    if (not isinstance(fixed, dict) or plan.get("project") != "personal-ops"
            or plan.get("operation_id") != operation_id
            or fixed.get("operation_id") != operation_id
            or plan.get("plan_digest") != _digest(fixed)):
        raise RuntimeVerificationError("ROLLOUT_PLAN_INVALID")
    if (not isinstance(result, dict)
            or plan.get("state") != "verification_required"
            or result.get("status") != "verification_required"
            or result.get("snapshot") != fixed.get("target_snapshot")
            or not isinstance(fixed.get("target_snapshot"), str)
            or HEX20.fullmatch(fixed["target_snapshot"]) is None):
        raise RuntimeVerificationError("ROLLOUT_PLAN_UNSUPPORTED")
    finished = _timestamp(plan.get("finished_at"), "ROLLOUT_PLAN_INVALID")
    return plan, raw, finished


def _validated_evidence(value: dict, path: Path):
    required = {
        "version", "observed_at", "release_path", "release_snapshot",
        "release_manifest_digest", "session_generation", "process", "tool_count",
        "tool_names", "tool_digests", "catalog_sha256", "record_sha256",
    }
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    process = value.get("process")
    generation = value.get("session_generation")
    catalog_digest = value.get("catalog_sha256")
    manifest_digest = value.get("release_manifest_digest")
    snapshot = value.get("release_snapshot")
    names = value.get("tool_names")
    digests = value.get("tool_digests")
    record_digest = value.get("record_sha256")
    try:
        calculated_record_digest = _digest(body)
    except RuntimeVerificationError:
        raise RuntimeVerificationError("CATALOG_EVIDENCE_TAMPERED") from None
    if (set(value) != required or type(value.get("version")) is not int
            or value["version"] != 1
            or not isinstance(record_digest, str)
            or HEX64.fullmatch(record_digest) is None
            or record_digest != calculated_record_digest
            or not isinstance(process, dict)
            or set(process) != set(ProcessIdentity.__dataclass_fields__)
            or not isinstance(generation, str) or HEX64.fullmatch(generation) is None
            or path.name != generation + ".json"
            or not isinstance(catalog_digest, str)
            or HEX64.fullmatch(catalog_digest) is None
            or not isinstance(manifest_digest, str)
            or HEX64.fullmatch(manifest_digest) is None
            or not isinstance(snapshot, str) or HEX20.fullmatch(snapshot) is None
            or not isinstance(value.get("release_path"), str)
            or type(value.get("tool_count")) is not int
            or not 0 < value["tool_count"] <= MAX_TOOLS
            or not isinstance(names, list)
            or not all(isinstance(name, str) and 0 < len(name) <= 256 for name in names)
            or names != sorted(set(names))
            or len(names) != value["tool_count"]
            or not isinstance(digests, dict)
            or set(names) != set(digests)
            or not all(isinstance(item, str) and HEX64.fullmatch(item) is not None
                       for item in digests.values())):
        raise RuntimeVerificationError("CATALOG_EVIDENCE_TAMPERED")
    try:
        identity = ProcessIdentity(**process)
    except TypeError:
        raise RuntimeVerificationError("CATALOG_EVIDENCE_TAMPERED") from None
    return identity


def verify_operation(operation_id, *, state_dir=None, releases_root=None,
                     receipt_root=None, boot_reader=_boot_id, process_reader=None):
    """Create a new verify-mode receipt without changing rollout history."""
    state = _private_dir(Path(state_dir or
                              Path.home() / ".local/state/chatgpt-coding-mcp"))
    releases = _private_dir(Path(releases_root or
                                 Path.home() / ".local/lib/chatgpt-coding-mcp/releases"))
    plan, plan_raw, finished_at = _load_plan(operation_id, state)
    snapshot = plan["immutable"]["target_snapshot"]
    release = releases / snapshot
    current_boot = boot_reader()
    evidence_dir = _private_dir(state / "runtime-verification" / "catalog")
    matches = []
    for path in sorted(evidence_dir.glob("*.json")):
        evidence, _raw = _read_private_json(path)
        identity = _validated_evidence(evidence, path)
        if evidence.get("release_snapshot") == snapshot:
            matches.append((path, evidence, identity))
    if not matches:
        raise RuntimeVerificationError("CATALOG_EVIDENCE_MISSING")
    path, evidence, identity = max(
        matches, key=lambda item: _timestamp(
            item[1].get("observed_at"), "CATALOG_EVIDENCE_TAMPERED"))
    observed_at = _timestamp(evidence.get("observed_at"), "CATALOG_EVIDENCE_TAMPERED")
    if observed_at < finished_at:
        raise RuntimeVerificationError("CATALOG_EVIDENCE_STALE")
    if observed_at > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise RuntimeVerificationError("CATALOG_EVIDENCE_FUTURE")
    if identity.boot_id != current_boot:
        raise RuntimeVerificationError("CATALOG_EVIDENCE_STALE")
    reader = process_reader or (lambda pid=None: _proc_identity(pid, boot_reader=boot_reader))
    before = _validated_identity(reader(identity.pid), release, current_boot)
    if before != identity:
        raise RuntimeVerificationError("RUNTIME_IDENTITY_CHANGED")
    try:
        release_info = verify_release_manifest(release)
    except WorkspaceControlError as exc:
        raise RuntimeVerificationError(exc.code) from exc
    if (not isinstance(release_info.get("release_digest"), str)
            or release_info["release_digest"][:20] != snapshot):
        raise RuntimeVerificationError("RUNTIME_RELEASE_INVALID")
    if (evidence.get("release_path") != str(release)
            or evidence.get("release_manifest_digest") != release_info["release_digest"]):
        raise RuntimeVerificationError("CATALOG_EVIDENCE_STALE")

    # Freeze all original inputs again before creating any new receipt.
    _plan_again, plan_raw_again, _finished_again = _load_plan(operation_id, state)
    evidence_again, _evidence_raw_again = _read_private_json(path)
    try:
        release_info_again = verify_release_manifest(release)
    except WorkspaceControlError as exc:
        raise RuntimeVerificationError(exc.code) from exc
    if (plan_raw_again != plan_raw or evidence_again != evidence
            or release_info_again != release_info):
        raise RuntimeVerificationError("VERIFICATION_INPUT_CHANGED")
    after = _validated_identity(reader(identity.pid), release, current_boot)
    if after != before:
        raise RuntimeVerificationError("RUNTIME_IDENTITY_CHANGED")

    claims = state / "runtime-verification" / "claims"
    if not claims.exists():
        claims.mkdir(mode=0o700)
    _private_dir(claims)
    claim = {
        "version": 1, "operation_id": operation_id,
        "session_generation": evidence["session_generation"],
        "catalog_record_sha256": evidence["record_sha256"],
        "claimed_at": _utc(),
    }
    _write_private_json(
        claims / (evidence["session_generation"] + ".json"), claim, exclusive=True)

    receipt = RolloutReceipt(root=receipt_root, mode="verify") if receipt_root else RolloutReceipt(mode="verify")
    verification = {
        "version": 1,
        "operation_id": operation_id,
        "status": "verification_required",
        "code": "CONNECTOR_ADOPTION_EVIDENCE_MISSING",
        "original_plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "release_snapshot": snapshot,
        "release_path": str(release),
        "release_manifest_digest": release_info["release_digest"],
        "boot_id": current_boot,
        "mcp_pid": identity.pid,
        "mcp_start_ticks": identity.start_ticks,
        "session_generation": evidence["session_generation"],
        "catalog_sha256": evidence["catalog_sha256"],
        "tool_count": evidence["tool_count"],
        "tool_digests": evidence["tool_digests"],
        "server_path": identity.server_path,
        "server_process_manifest_verified": True,
        "server_catalog_observed": True,
        "tools_registered": False,
        "connector_metadata_verified": False,
        "connector_schemas_verified": False,
        "baseline_eligible": False,
        "pending_reason": PENDING_REASON,
    }
    verification_path = _write_private_json(
        receipt.path.parent / "verification.json", verification)
    _verification_payload, verification_bytes = _read_private_json(
        verification_path, code="VERIFICATION_STATE_UNSAFE")
    receipt.finish({
        "status": "verification_required",
        "code": "CONNECTOR_ADOPTION_EVIDENCE_MISSING",
        "snapshot": snapshot,
        "runtime_verified": False,
        "server_process_manifest_verified": True,
        "tools_registered": False,
        "connector_schemas_verified": False,
        "server_path_sha256": hashlib.sha256(identity.server_path.encode()).hexdigest(),
        "verification_sha256": hashlib.sha256(verification_bytes).hexdigest(),
        "pid": identity.pid,
        "start_ticks": identity.start_ticks,
        "tool_count": evidence["tool_count"],
    }, 2)
    return {**verification, "verification_path": str(verification_path),
            "receipt_path": str(receipt.path), "catalog_evidence_path": str(path)}


__all__ = [
    "CatalogEvidenceWriter", "ProcessIdentity", "RuntimeVerificationError",
    "catalog_observer_for_runtime", "verify_operation",
]
