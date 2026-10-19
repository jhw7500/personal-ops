"""Exact-target, read-only Docker inspection and source inventory for HostRuntime.

No general command interface, no secret values returned to ChatGPT.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request

from host_runtime_config import (
    API_HEALTH, ORIGIN, PROJECT, TARGET, HostRuntimeError, load_config, validate_scope,
)

MAX_OUTPUT = 256 * 1024
MAX_FILES = 5000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 128 * 1024 * 1024
SANITIZED_PATH = "/usr/local/bin:/usr/bin:/bin"
SOURCE_SINGLE = (
    ".dockerignore", "deploy/pc-test/Dockerfile",
    "deploy/pc-test/compose.yaml", "deploy/pc-test/manage.py",
    "deploy/pc-test/run.mjs",
    "server/package.json", "server/package-lock.json",
    "server/config/commerce-policy.json", "server/config/credit-policy.json",
    "contracts/package.json",
    "content/drafts/full-deck-gemini-20260928/interpretations.csv",
    "content/drafts/full-deck-gemini-20260928/provenance.json",
    "content/drafts/full-deck-gemini-20260928/import/snapshot.json",
    "content/drafts/full-deck-gemini-20260928/import/version-id-allocations.json",
)
SOURCE_DIRS = ("server/src", "server/migrations", "contracts", "scripts/content")
SOURCE_GLOB = {
    "server/migrations": "*.sql",
    "contracts": "*.ts",
    "scripts/content": "*.mjs",
}
CONTEXT_IGNORED = {"node_modules", ".git", "__pycache__", ".pytest_cache"}
HIGH_RISK_INPUTS = {".dockerignore", "deploy/pc-test/Dockerfile",
                    "deploy/pc-test/compose.yaml", "deploy/pc-test/manage.py",
                    "deploy/pc-test/run.mjs", "server/package.json",
                    "server/package-lock.json",
                    "server/config/commerce-policy.json",
                    "server/config/credit-policy.json",
                    "server/migrations/013_staged_reading_access.sql"}
DB_READINESS_QUERY = (
    "BEGIN READ ONLY; SELECT CASE WHEN "
    "EXISTS (SELECT 1 FROM schema_migrations WHERE name='013_staged_reading_access.sql') "
    "AND to_regclass('public.reading_ad_attempts') IS NOT NULL "
    "AND to_regclass('public.reading_section_unlocks') IS NOT NULL "
    "AND to_regclass('public.reading_credit_packs') IS NOT NULL "
    "AND to_regclass('public.reading_credit_redemptions') IS NOT NULL "
    "THEN 'ready' ELSE 'missing' END; COMMIT;"
)
PREDECESSOR_NAMES = (
    "001_foundation.sql", "002_interpretation_candidates.sql",
    "003_reading_flow.sql", "004_subject_identity_rotation.sql",
    "005_full_tarot_catalog.sql", "006_reading_memos.sql",
    "007_reading_purchase_verification.sql", "008_toss_login_challenges.sql",
    "009_account_recovery_lifecycle.sql", "010_commerce_policy_snapshots.sql",
    "011_checkout_fulfillment.sql", "012_checkout_review_intake.sql",
    "013_staged_reading_access.sql",
)
PREDECESSOR_TABLES = (
    "reading_memos", "toss_login_identities", "reading_purchase_orders",
    "reading_purchase_observations", "reading_entitlement_candidates",
    "toss_login_challenges", "toss_account_revocations", "toss_login_code_uses",
    "toss_account_restore_requests", "toss_restored_sessions",
    "reading_commerce_releases", "reading_checkout_attempts",
    "reading_access_grants", "reading_checkout_review_cases",
)
PREDECESSOR_INDEXES = (
    "reading_memos_owner", "readings_owner_archive", "reading_purchase_one_open",
    "reading_purchase_owner", "reading_candidate_owner",
    "toss_login_challenges_owner", "toss_login_single_verification",
    "toss_restore_source", "one_live_checkout_per_order",
    "one_unresolved_checkout_per_user", "checkout_owner_lookup",
    "reading_grant_owner", "checkout_review_owner", "checkout_review_intake_queue",
)
PREDECESSOR_FUNCTIONS = (
    "reject_commerce_release_rewrite", "reject_order_commerce_rewrite",
    "protect_checkout_identity", "protect_reading_grant",
    "protect_checkout_review_intake",
)
SCHEMA_013_TABLES = (
    "reading_ad_attempts", "reading_section_unlocks",
    "reading_credit_packs", "reading_credit_redemptions",
)
PREDECESSOR_SQL_INPUTS = frozenset(
    "server/migrations/" + name for name in PREDECESSOR_NAMES[5:12]
)


ROLE_NAMES = {"api": "tarororo-test-api-1",
              "worker": "tarororo-test-worker-1",
              "db": "tarororo-test-db-1"}


def _safe_env(mode="disabled"):
    """Never import COMPOSE_FILE, credentials, proxy or caller-defined paths."""
    return {
        "PATH": SANITIZED_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(Path.home()),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        "TARORORO_UID": str(os.getuid()), "TARORORO_GID": str(os.getgid()),
        "TARORORO_CONFIG_DIR": str(Path.home() / ".config/tarororo-test"),
        "TARORORO_READING_MONETIZATION_TEST_MODE": mode,
    }


def _default_run(argv, *, cwd=None, mode="disabled", timeout=7, capture=True):
    """Run fixed host commands with a strict live output cap and process-group timeout.

    Reading subprocess.run(... PIPE) fully and then checking its length can
    exhaust host memory before the length check. All error strings are sanitized.
    """
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(part, str) or not part or "\0" in part for part in argv)):
        raise HostRuntimeError("INVALID_COMMAND")
    process = None
    selector = None
    output = bytearray()
    failure = None
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=_safe_env(mode), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        if capture:
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "HOST_COMMAND_TIMEOUT"
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        failure = "HOST_OUTPUT_TOO_LARGE"
                        break
                if failure:
                    break
        if not failure:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "HOST_COMMAND_TIMEOUT"
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = "HOST_COMMAND_TIMEOUT"
        if failure:
            raise HostRuntimeError(failure, "EXECUTE")
        if process.returncode != 0:
            raise HostRuntimeError("HOST_COMMAND_FAILED", "EXECUTE")
        return bytes(output) if capture else b""
    except OSError as exc:
        raise HostRuntimeError("EXECUTOR_UNAVAILABLE", "EXECUTE") from exc
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def _parse_json(data):
    try:
        return json.loads(data)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise HostRuntimeError("HOST_RESPONSE_INVALID") from exc


def _safe_file(path: Path):
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostRuntimeError("SOURCE_INCOMPLETE") from exc
    if (not stat.S_ISREG(metadata.st_mode) or path.is_symlink()
            or metadata.st_size > MAX_FILE_BYTES):
        raise HostRuntimeError("UNSAFE_BUILD_INPUT")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise HostRuntimeError("SOURCE_CHANGED")
        data = b""
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(fd)
        if (len(data) != metadata.st_size or len(data) > MAX_FILE_BYTES
                or after.st_mtime_ns != metadata.st_mtime_ns
                or after.st_ctime_ns != metadata.st_ctime_ns
                or after.st_size != metadata.st_size):
            raise HostRuntimeError("SOURCE_CHANGED")
        # The private context is owner-only. Normalize group/other bits to
        # preserve executable identity without exposing staged code to peers.
        mode = 0o700 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o600
        return data, mode
    finally:
        os.close(fd)


def _iter_sources(root: Path):
    """Strict, bounded build-input union. Never copy arbitrary repository trees."""
    result = set(SOURCE_SINGLE)
    for folder in SOURCE_DIRS:
        base = root / folder
        if base.is_symlink() or not base.is_dir():
            raise HostRuntimeError("SOURCE_INCOMPLETE")
        for child in base.rglob("*"):
            relative = child.relative_to(root)
            if (any(part in CONTEXT_IGNORED or part == ".env" or part.startswith(".env.")
                    for part in relative.parts) or child.is_symlink()):
                raise HostRuntimeError("UNSAFE_BUILD_INPUT")
            if child.is_dir():
                continue
            if folder == "scripts/content" and child.name.endswith(".test.mjs"):
                continue  # Explicitly excluded by the existing .dockerignore.
            pattern = SOURCE_GLOB.get(folder)
            if pattern and (child.parent != base or not child.match(pattern)):
                if not (folder == "contracts" and child.parent == base and child.name == "package.json"):
                    raise HostRuntimeError("UNSAFE_BUILD_INPUT")
            result.add(relative.as_posix())
            if len(result) > MAX_FILES:
                raise HostRuntimeError("BUILD_INPUT_TOO_LARGE")
    return sorted(result)


def source_inventory(root: Path, *, copy_to: Path | None = None):
    """Fingerprint files/modes and optionally materialize exact private build context.

    Any symlink, changed byte, unexplained path, missing required file or oversize
    fails closed. Caller compares a second inventory after materialization to
    close common source-edit races; the build executes only from private copy.
    """
    root = root.resolve(strict=True)
    files = []
    total = 0
    items = _iter_sources(root)
    for name in items:
        path = root / name
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root))):
            raise HostRuntimeError("UNSAFE_BUILD_INPUT")
        data, mode = _safe_file(path)
        total += len(data)
        if total > MAX_CONTEXT_BYTES:
            raise HostRuntimeError("BUILD_INPUT_TOO_LARGE")
        digest = hashlib.sha256(data).hexdigest()
        files.append({"path": name, "mode": mode, "size": len(data), "sha256": digest})
        if copy_to is not None:
            dest = copy_to / name
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(dest, mode & 0o700)
    if not any(x["path"].endswith("013_staged_reading_access.sql") for x in files):
        raise HostRuntimeError("SOURCE_INCOMPLETE")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"digest": digest, "file_count": len(files), "total_bytes": total, "files": files}


def _git(root: Path, *argv):
    raw = _default_run(["git", "-C", str(root), *argv], timeout=7)
    return raw


def git_state(root: Path):
    commit = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise HostRuntimeError("SOURCE_INCOMPLETE")
    data = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths = []
    fields = data.split(b"\0")
    i = 0
    while i < len(fields):
        part = fields[i]
        i += 1
        if not part:
            continue
        if len(part) < 4 or part[2:3] != b" ":
            raise HostRuntimeError("SOURCE_INCOMPLETE")
        paths.append(part[3:].decode("utf8", errors="replace"))
        if part[0:1] in (b"R", b"C") or part[1:2] in (b"R", b"C"):
            i += 1
        if len(paths) > MAX_FILES:
            raise HostRuntimeError("SOURCE_INCOMPLETE")
    return {"head": commit, "dirty": bool(paths), "changed_paths": sorted(paths)}


def verify_reviewed_high_risk_inputs(root: Path, paths=None, *, include_predecessors=False) -> None:
    """Pin executable build-policy files to reviewed HEAD bytes and index flags.

    'git status' alone can hide modified files via assume-unchanged and
    skip-worktree flags; the container build must not accept those bypasses.
    Only fixed paths are read (default: legacy HIGH_RISK_INPUTS; registered
    targets pass their own list). No Git hooks, checkout, filters or writes.
    """
    root = Path(root).resolve(strict=True)
    if type(include_predecessors) is not bool:
        raise HostRuntimeError("REVIEW_REQUIRED")
    paths = set(HIGH_RISK_INPUTS if paths is None else paths)
    if include_predecessors:
        paths |= PREDECESSOR_SQL_INPUTS
    paths = sorted(paths)
    if not paths:
        return  # An empty Git pathspec would select every file.
    tracked = _git(root, "ls-files", "-v", "-z", "--", *paths).split(b"\0")
    observed = {}
    for line in tracked:
        if not line:
            continue
        if len(line) < 3 or line[1:2] != b" ":
            raise HostRuntimeError("REVIEW_REQUIRED")
        try:
            name = line[2:].decode("utf8", "strict")
        except UnicodeDecodeError as exc:
            raise HostRuntimeError("REVIEW_REQUIRED") from exc
        if line[0:1] != b"H" or name in observed:
            raise HostRuntimeError("REVIEW_REQUIRED")
        observed[name] = True
    if set(observed) != set(paths):
        raise HostRuntimeError("REVIEW_REQUIRED")

    modes = {}
    output = _git(root, "ls-tree", "-z", "--full-tree", "HEAD", "--", *paths)
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
            name = raw_path.decode("utf8", "strict")
        except (ValueError, UnicodeError) as exc:
            raise HostRuntimeError("REVIEW_REQUIRED") from exc
        if (name in modes or mode not in {"100644", "100755"}
                or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)):
            raise HostRuntimeError("REVIEW_REQUIRED")
        modes[name] = mode
    if set(modes) != set(paths):
        raise HostRuntimeError("REVIEW_REQUIRED")

    for name in paths:
        path = root / name
        if (path.is_symlink() or not path.resolve(strict=True).is_relative_to(root)):
            raise HostRuntimeError("REVIEW_REQUIRED")
        current, owner_only_mode = _safe_file(path)
        # Git commit snapshots have canonical 0644/0755 executable flags;
        # the saved private build context normalizes to owner-only modes.
        if bool(owner_only_mode & 0o111) != (modes[name] == "100755"):
            raise HostRuntimeError("REVIEW_REQUIRED")
        reviewed = _git(root, "cat-file", "blob", f"HEAD:{name}")
        if current != reviewed:
            raise HostRuntimeError("REVIEW_REQUIRED")


class HostRuntime:
    def __init__(self, projects, *, config_path=None, runner=None):
        self.projects = projects
        self.config_path = config_path
        self.runner = runner or _default_run

    def guard(self, project, target):
        root = validate_scope(project, target, self.projects)
        enabled = load_config() if self.config_path is None else load_config(self.config_path)
        if not enabled:
            raise HostRuntimeError("HOST_RUNTIME_DISABLED")
        return root

    def run(self, argv, *, cwd=None, mode="disabled", timeout=7, capture=True):
        return self.runner(argv, cwd=cwd, mode=mode, timeout=timeout, capture=capture)

    def inspect(self, role):
        raw = _parse_json(self.run(["docker", "inspect", ROLE_NAMES[role]]))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise HostRuntimeError("HOST_RESPONSE_INVALID")
        item = raw[0]
        config = item.get("Config") or {}
        state = item.get("State") or {}
        labels = config.get("Labels") or {}
        if (item.get("Name") != "/" + ROLE_NAMES[role]
                or labels.get("com.docker.compose.project") != TARGET
                or labels.get("com.docker.compose.service") != role):
            raise HostRuntimeError("WRONG_DOCKER_TARGET")
        image = item.get("Image")
        cid = item.get("Id")
        started = state.get("StartedAt")
        if (not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
                or not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{64}", cid)
                or not isinstance(started, str) or not started):
            raise HostRuntimeError("HOST_RESPONSE_INVALID")
        env = {}
        for entry in config.get("Env", []):
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                if key in {"APP_ENV", "AUTH_MODE", "ALLOWED_ORIGINS",
                           "READING_MONETIZATION_TEST_MODE", "POSTGRES_DB",
                           "READING_IAP_MODE", "READING_FULFILLMENT_MODE",
                           "READING_CHECKOUT_MODE", "READING_REVIEW_MODE"}:
                    env[key] = value
        return {
            "id": cid, "image_id": image, "started_at": started,
            "state": state.get("Status", "unknown"),
            "health": (state.get("Health") or {}).get("Status", "unknown"),
            "environment": env,
            "ports": (item.get("NetworkSettings") or {}).get("Ports") or {},
            "mounts": item.get("Mounts") or [],
            "networks": (item.get("NetworkSettings") or {}).get("Networks") or {},
        }

    def identities(self):
        return {role: self.inspect(role) for role in ("db", "worker", "api")}

    @staticmethod
    def _verify_identity(snapshot, *, expected_mode=None, health=True):
        api, db, worker = snapshot["api"], snapshot["db"], snapshot["worker"]
        env = api["environment"]
        if (env.get("APP_ENV") != "test" or env.get("AUTH_MODE") != "toss"
                or env.get("ALLOWED_ORIGINS") != ORIGIN
                or env.get("READING_MONETIZATION_TEST_MODE", "disabled") not in {"disabled", "toss-demo"}
                or (expected_mode is not None and
                    env.get("READING_MONETIZATION_TEST_MODE", "disabled") != expected_mode)
                or db["environment"].get("POSTGRES_DB") != "tarororo_test"
                or any(env.get(name, "disabled") != "disabled" for name in (
                    "READING_IAP_MODE", "READING_FULFILLMENT_MODE",
                    "READING_CHECKOUT_MODE", "READING_REVIEW_MODE"))
                or api["ports"].get("3000/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "3200"}]
                or not any(m.get("Type") == "volume" and m.get("Name") == "tarororo-test_database"
                           for m in db["mounts"])
                or not {"tarororo-test_default"}.issubset(db["networks"])
                or not {"tarororo-test_default"}.issubset(api["networks"])
                or not {"tarororo-test_default"}.issubset(worker["networks"])):
            raise HostRuntimeError("WRONG_DOCKER_TARGET")
        if health and (api["state"] != "running" or api["health"] != "healthy"
                       or db["state"] != "running" or worker["state"] != "running"):
            raise HostRuntimeError("SERVICE_NOT_READY")

    def check_deploy_secrets(self, *, base=None):
        """Metadata-only check of the exact existing test deployment secret set.

        No secret bytes are returned, copied, logged or passed to a model.
        Docker/Compose mounts each secret by its exact source name.
        """
        folder = Path(base) if base is not None else Path.home() / ".config/tarororo-test"
        try:
            root_info = folder.lstat()
            if (not stat.S_ISDIR(root_info.st_mode) or folder.is_symlink()
                    or root_info.st_uid != os.getuid()
                    or stat.S_IMODE(root_info.st_mode) != 0o700):
                raise HostRuntimeError("UNSAFE_SECRET_DIR")
            for name in ("db-password", "subject-secret", "gemini-api-key",
                         "toss-client.crt", "toss-client.key"):
                file = folder / name
                info = file.lstat()
                if (not stat.S_ISREG(info.st_mode) or file.is_symlink()
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_size < 1 or info.st_size > 64 * 1024):
                    raise HostRuntimeError("UNSAFE_SECRET_FILE")
        except OSError as exc:
            raise HostRuntimeError("SECRET_NOT_READY") from exc

    def migration_status(self, project, target):
        """Inspect only the fixed 013 migration; never return application rows."""
        root = self.guard(project, target)
        snapshot = self.identities()
        self._verify_identity(snapshot, health=True)
        # Compare all twelve predecessor migration checksums, not just names.
        prefix = (
            "001_foundation", "002_interpretation_candidates", "003_reading_flow",
            "004_subject_identity_rotation", "005_full_tarot_catalog", "006_reading_memos",
            "007_reading_purchase_verification", "008_toss_login_challenges",
            "009_account_recovery_lifecycle", "010_commerce_policy_snapshots",
            "011_checkout_fulfillment", "012_checkout_review_intake",
        )
        pairs = []
        for item in prefix:
            filename = item + ".sql"
            path = root / "server/migrations" / filename
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
            pairs.append("('" + filename + "','" + hashlib.sha256(path.read_bytes()).hexdigest() + "')")
        matching_prior = (
            "(SELECT COUNT(*) FROM (VALUES " + ",".join(pairs) +
            ") AS expected(name,checksum) JOIN schema_migrations actual "
            "ON actual.name=expected.name AND actual.checksum=expected.checksum)"
        )
        query = (
            "BEGIN READ ONLY; SELECT json_build_object("
            "'prerequisites_count'," + matching_prior + ","
            "'checksum',COALESCE((SELECT checksum FROM schema_migrations "
            "WHERE name='013_staged_reading_access.sql'),'missing'),"
            "'ad_attempts',to_regclass('public.reading_ad_attempts') IS NOT NULL,"
            "'section_unlocks',to_regclass('public.reading_section_unlocks') IS NOT NULL,"
            "'credit_packs',to_regclass('public.reading_credit_packs') IS NOT NULL,"
            "'credit_redemptions',to_regclass('public.reading_credit_redemptions') IS NOT NULL"
            "); COMMIT;"
        )
        source = root / "server/migrations/013_staged_reading_access.sql"
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        raw = self.run([
            "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", "tarororo", "-d", "tarororo_test",
            "-c", query,
        ], timeout=7).decode("ascii", errors="replace")
        try:
            data = next(json.loads(line) for line in raw.splitlines() if line.startswith("{"))
        except (ValueError, StopIteration) as exc:
            raise HostRuntimeError("MIGRATION_STATUS_UNKNOWN") from exc
        names = ("ad_attempts", "section_unlocks", "credit_packs", "credit_redemptions")
        if (set(data) != {"checksum", "prerequisites_count", *names}
                or any(type(data[key]) is not bool for key in names)
                or type(data["prerequisites_count"]) is not int
                or not 0 <= data["prerequisites_count"] <= 12):
            raise HostRuntimeError("MIGRATION_STATUS_UNKNOWN")
        observed = data["checksum"]
        if not isinstance(observed, str) or not (observed == "missing" or
                re.fullmatch(r"[0-9a-f]{64}", observed)):
            raise HostRuntimeError("MIGRATION_STATUS_UNKNOWN")
        ready = observed == expected and all(data[key] for key in names)
        return {"project": project, "target": target, "migration": "013_staged_reading_access.sql",
                "state": "ready" if ready else "missing" if observed == "missing" else
                         "checksum_mismatch" if observed != expected else "incomplete",
                "history_present": observed != "missing",
                "prerequisites_ok": data["prerequisites_count"] == 12,
                "prerequisites_matched": data["prerequisites_count"],
                "checksum_matches_source": observed == expected,
                "tables": {key: data[key] for key in names},
                "database_identity": {"container_id": snapshot["db"]["id"],
                                      "started_at": snapshot["db"]["started_at"]},
                "observed_at": datetime.now(timezone.utc).isoformat()}

    def predecessor_status(self, project, target):
        """Read-only, exact-name/checksum inventory for 001-013; no business rows."""
        root = self.guard(project, target)
        snapshot = self.identities()
        self._verify_identity(snapshot, health=True)
        expected = {}
        for name in PREDECESSOR_NAMES:
            path = root / "server/migrations" / name
            if not path.resolve().is_relative_to(root):
                raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
            raw, _ = _safe_file(path)
            if len(raw) > 60 * 1024:
                raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
            expected[name] = hashlib.sha256(raw).hexdigest()
        pairs = ",".join("('" + name + "','" + checksum + "')"
                         for name, checksum in expected.items())
        names = ",".join("'" + name + "'" for name in PREDECESSOR_NAMES)
        objects = tuple(dict.fromkeys((*PREDECESSOR_TABLES, *PREDECESSOR_INDEXES,
                                       *SCHEMA_013_TABLES)))
        tables_sql = ",".join("'" + name + "',to_regclass('public." + name +
                              "') IS NOT NULL" for name in objects)
        functions_sql = ",".join("'" + name + "',to_regprocedure('public." +
                                 name + "()') IS NOT NULL"
                                 for name in PREDECESSOR_FUNCTIONS)
        query = (
            "BEGIN READ ONLY; /* fixed_predecessor_status_v1 */ "
            "SELECT json_build_object("
            "'history',(SELECT json_object_agg(e.name,CASE "
            "WHEN a.name IS NULL THEN 'missing' "
            "WHEN a.checksum=e.checksum THEN 'checksum_match' "
            "ELSE 'checksum_mismatch' END) "
            "FROM (VALUES " + pairs + ") AS e(name,checksum) "
            "LEFT JOIN public.schema_migrations a ON a.name=e.name),"
            "'unexpected_history_count',(SELECT COUNT(*) FROM public.schema_migrations "
            "WHERE name NOT IN (" + names + ")),"
            "'objects',json_build_object(" + tables_sql + "),"
            "'functions',json_build_object(" + functions_sql + "),"
            "'generation',json_build_object("
            "'database',current_database(),'user',current_user,"
            "'postmasterStartEpoch',"
            "extract(epoch from pg_postmaster_start_time())::text)"
            "); COMMIT;"
        )
        raw = self.run([
            "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", "tarororo", "-d", "tarororo_test",
            "-c", query,
        ], timeout=10).decode("ascii", errors="replace")
        try:
            result = next(json.loads(line) for line in raw.splitlines()
                          if line.startswith("{"))
        except (ValueError, StopIteration) as exc:
            raise HostRuntimeError("PREDECESSOR_STATUS_UNKNOWN") from exc
        generation = result.get("generation") if isinstance(result, dict) else None
        observed = result.get("history") if isinstance(result, dict) else None
        objects_present = result.get("objects") if isinstance(result, dict) else None
        functions_present = result.get("functions") if isinstance(result, dict) else None
        count = result.get("unexpected_history_count") if isinstance(result, dict) else None
        if (not isinstance(observed, dict) or set(observed) != set(expected)
                or any(value not in ("missing", "checksum_match", "checksum_mismatch")
                       for value in observed.values())
                or not isinstance(objects_present, dict)
                or set(objects_present) != set(objects)
                or any(type(v) is not bool for v in objects_present.values())
                or not isinstance(functions_present, dict)
                or set(functions_present) != set(PREDECESSOR_FUNCTIONS)
                or any(type(v) is not bool for v in functions_present.values())
                or type(count) is not int or count < 0
                or not isinstance(generation, dict)
                or set(generation) != {"database", "user", "postmasterStartEpoch"}
                or generation["database"] != "tarororo_test"
                or generation["user"] != "tarororo"
                or not isinstance(generation["postmasterStartEpoch"], str)
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",
                                    generation["postmasterStartEpoch"])):
            raise HostRuntimeError("PREDECESSOR_STATUS_UNKNOWN")
        baseline_ok = all(observed[name] == "checksum_match" for name in PREDECESSOR_NAMES[:5])
        migrations_missing = all(observed[name] == "missing" for name in PREDECESSOR_NAMES[5:])
        clean = not any(objects_present.values()) and not any(functions_present.values())
        predecessor_history_complete = all(
            observed[name] == "checksum_match" for name in PREDECESSOR_NAMES[:12]
        )
        schema_013_history_complete = observed[PREDECESSOR_NAMES[12]] == "checksum_match"
        tables_ready = all(objects_present[name] for name in PREDECESSOR_TABLES)
        indexes_ready = all(objects_present[name] for name in PREDECESSOR_INDEXES)
        functions_ready = all(functions_present.values())
        schema_013_absent = not any(objects_present[name] for name in SCHEMA_013_TABLES)
        schema_013_tables_ready = all(objects_present[name] for name in SCHEMA_013_TABLES)
        predecessor_complete = (predecessor_history_complete and tables_ready
                                and indexes_ready and functions_ready and count == 0)
        predecessor_only_complete = (
            predecessor_complete and observed[PREDECESSOR_NAMES[12]] == "missing"
            and schema_013_absent
        )
        combined_complete = (predecessor_complete and schema_013_history_complete
                             and schema_013_tables_ready)
        completion_scope = ("006-013" if combined_complete else
                            "006-012" if predecessor_only_complete else None)

        def expected_object(name):
            if name in SCHEMA_013_TABLES:
                return schema_013_history_complete
            return predecessor_history_complete

        return {
            "project": project, "target": target,
            "history": observed,
            "checksums": {name: expected[name] for name in PREDECESSOR_NAMES[5:12]},
            "generation": generation,
            "unexpected_history_count": count,
            "object_collisions": [name for name in objects
                                   if objects_present[name]
                                   and not expected_object(name)],
            "function_collisions": [name for name in PREDECESSOR_FUNCTIONS
                                    if functions_present[name]
                                    and not predecessor_history_complete],
            "tables_present": {name: objects_present[name] for name in PREDECESSOR_TABLES},
            "indexes_present": {name: objects_present[name] for name in PREDECESSOR_INDEXES},
            "functions_present": dict(functions_present),
            "schema_013_tables_present": {name: objects_present[name]
                                          for name in SCHEMA_013_TABLES},
            "ready_to_prepare": (baseline_ok and migrations_missing and clean and count == 0),
            "complete": completion_scope is not None,
            "completion_scope": completion_scope,
            "database_identity": {"container_id": snapshot["db"]["id"],
                                  "started_at": snapshot["db"]["started_at"]},
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def readiness(self):
        try:
            response = self.run([
                "docker", "exec", "-i", ROLE_NAMES["db"],
                "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                "-U", "tarororo", "-d", "tarororo_test", "-c", DB_READINESS_QUERY,
            ], timeout=5).decode("ascii", errors="replace")
        except HostRuntimeError:
            return "unknown"
        parts = [line.strip() for line in response.splitlines()]
        return "ready" if "ready" in parts else "missing" if "missing" in parts else "unknown"

    @staticmethod
    def http_health():
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(urllib.request.Request(API_HEALTH, method="GET"), timeout=3) as result:
                if result.status != 200:
                    return "unavailable"
                payload = result.read(129)
                if len(payload) > 128 or _parse_json(payload) != {"status": "ready"}:
                    return "unavailable"
                return "ready"
        except (OSError, ValueError, urllib.error.URLError):
            return "unavailable"

    def get_status(self, project, target):
        try:
            self.guard(project, target)
        except HostRuntimeError as exc:
            if exc.code == "HOST_RUNTIME_DISABLED":
                return {"project": PROJECT, "target": TARGET, "availability": "disabled",
                        "observed_at": datetime.now(timezone.utc).isoformat()}
            raise
        output = {"project": PROJECT, "target": TARGET, "availability": "enabled",
                  "observed_at": datetime.now(timezone.utc).isoformat(), "warnings": []}
        try:
            snapshot = self.identities()
            self._verify_identity(snapshot, health=False)
            output["api"] = {key: snapshot["api"][key] for key in ("state", "health", "image_id")}
            output["api"]["monetization_mode"] = snapshot["api"]["environment"].get(
                "READING_MONETIZATION_TEST_MODE", "disabled")
            output["worker"] = {key: snapshot["worker"][key] for key in ("state", "image_id")}
            output["database"] = {"state": snapshot["db"]["state"], "schema_013": self.readiness()}
            output["health_endpoint"] = self.http_health()
            if snapshot["api"]["health"] != "healthy":
                output["warnings"].append("API_NOT_HEALTHY")
        except HostRuntimeError as exc:
            output["warnings"].append(exc.code)
            output["api"] = {"state": "unknown", "health": "unknown", "image_id": None}
            output["worker"] = {"state": "unknown", "image_id": None}
            output["database"] = {"state": "unknown", "schema_013": "unknown"}
            output["health_endpoint"] = "unknown"
        return output

    def compose_config(self, root, mode, *, context=None):
        root = Path(root)
        if context is not None:
            root = Path(context)
        config_file = root / "deploy/pc-test/compose.yaml"
        data = _parse_json(self.run([
            "docker", "compose", "--project-name", TARGET,
            "--env-file", "/dev/null", "--file", str(config_file),
            "config", "--format", "json",
        ], cwd=root, mode=mode, timeout=12))
        services = data.get("services") if isinstance(data, dict) else None
        # Compose omits services behind inactive profiles from the default
        # resolved model. "migrate" and "seed" are tools, not API dependencies.
        if not isinstance(services, dict) or set(services) != {"api", "worker", "db"}:
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        api, db, worker = services["api"], services["db"], services["worker"]
        # The Compose file is writable project input: never trust its build
        # context, Dockerfile, image, or environment without this preflight.
        root = root.resolve(strict=True)
        dockerfile_allowed = {"deploy/pc-test/Dockerfile",
                              str(root / "deploy/pc-test/Dockerfile")}
        for service in ("api", "worker"):
            item = services[service]
            build = item.get("build")
            if (not isinstance(build, dict)
                    or build.get("context") != str(root)
                    or build.get("dockerfile") not in dockerfile_allowed
                    or set(build) != {"context", "dockerfile"}
                    or item.get("image") != "tarororo-pc-test:local"):
                raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        allowed_environment = {
            "api": {"APP_ENV", "AUTH_MODE", "HOST", "PORT", "ALLOWED_ORIGINS",
                    "TOSS_MTLS_CERT_PATH", "TOSS_MTLS_KEY_PATH",
                    "READING_MONETIZATION_TEST_MODE"},
            "worker": {"AI_PROVIDER", "AI_MODEL", "AI_GENERATION_TIMEOUT_MS"},
            "db": {"POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD_FILE"},
        }
        if any(not isinstance(services[name].get("environment"), dict)
               or set(services[name]["environment"]) != allowed
               for name, allowed in allowed_environment.items()):
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        if (api.get("user") != f"{os.getuid()}:{os.getgid()}"
                or worker.get("user") != f"{os.getuid()}:{os.getgid()}"
                or api.get("command") not in (["api"], "api")
                or worker.get("command") not in (["worker"], "worker")
                or not isinstance(db.get("image"), str)
                or not db["image"].startswith("postgres:18-alpine@sha256:")):
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        ports = api.get("ports")
        port_ok = isinstance(ports, list) and len(ports) == 1 and all(
            p.get(key) == value for key, value in
            {"host_ip": "127.0.0.1", "target": 3000, "published": "3200"}.items()
            for p in ports)
        if (not port_ok or db.get("ports") or worker.get("ports")
                or api.get("environment", {}).get("APP_ENV") != "test"
                or api.get("environment", {}).get("AUTH_MODE") != "toss"
                or api.get("environment", {}).get("ALLOWED_ORIGINS") != ORIGIN
                or api.get("environment", {}).get("READING_MONETIZATION_TEST_MODE") != mode
                or db.get("environment", {}).get("POSTGRES_DB") != "tarororo_test"
                or any(api.get("environment", {}).get(name, "disabled") != "disabled" for name in (
                    "READING_IAP_MODE", "READING_FULFILLMENT_MODE",
                    "READING_CHECKOUT_MODE", "READING_REVIEW_MODE"))):
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        for key in ("api", "worker", "db"):
            item = services[key]
            if (item.get("privileged") or item.get("network_mode") == "host"
                    or item.get("pid") == "host" or item.get("ipc") == "host"
                    or item.get("devices") or item.get("env_file")):
                raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        if (api.get("read_only") is not True or worker.get("read_only") is not True
                or set(api.get("cap_drop", [])) != {"ALL"}
                or set(worker.get("cap_drop", [])) != {"ALL"}
                or api.get("volumes") or worker.get("volumes")):
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        for key, service, expected in (
            ("api", "api", {"db-password", "subject-secret", "toss-client.crt", "toss-client.key"}),
            ("worker", "worker", {"db-password", "gemini-api-key"}),
        ):
            if {x.get("source") for x in services[service].get("secrets", [])} != expected:
                raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        if (not isinstance(db.get("volumes"), list) or len(db["volumes"]) != 1
                or db["volumes"][0].get("type") != "volume"
                or db["volumes"][0].get("source") != "database"
                or db["volumes"][0].get("target") != "/var/lib/postgresql"
                or data.get("volumes", {}).get("database", {}).get("name") != "tarororo-test_database"
                or data.get("networks", {}).get("default", {}).get("name") != "tarororo-test_default"
                or data.get("networks", {}).get("content-internal", {}).get("name") != "tarororo-test-content-internal"
                or data.get("networks", {}).get("content-internal", {}).get("internal") is not True
                or set(api.get("networks", {})) != {"default"}
                or set(worker.get("networks", {})) != {"default"}
                or set(db.get("networks", {})) != {"default", "content-internal"}):
            raise HostRuntimeError("WRONG_COMPOSE_CONFIG")
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def compare(self, project, target, *, attestation=None):
        root = self.guard(project, target)
        source = source_inventory(root)
        git = git_state(root)
        snapshot = self.identities()
        self._verify_identity(snapshot, health=False)
        compose_digest = self.compose_config(root, "disabled")
        origin = "unknown"
        comparison = "UNKNOWN"
        if isinstance(attestation, dict):
            if (attestation.get("image_id") == snapshot["api"]["image_id"]
                    and attestation.get("source_digest") == source["digest"]):
                origin = "attested"
                comparison = "MATCH"
            elif attestation.get("image_id") == snapshot["api"]["image_id"]:
                origin = "attested"
                comparison = "DIFFERS"
        return {
            "project": project, "target": target, "git_head": git["head"],
            "dirty": git["dirty"], "changed_paths": git["changed_paths"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_digest": source["digest"], "source_file_count": source["file_count"],
            "compose_digest": compose_digest,
            "source_files": [x["path"] for x in source["files"]],
            "high_risk_changes": [p for p in git["changed_paths"] if p in HIGH_RISK_INPUTS],
            "api_image_id": snapshot["api"]["image_id"],
            "worker_image_id": snapshot["worker"]["image_id"],
            "provenance": origin, "comparison": comparison,
            "unexpected_service_changes": "not_checked_without_prior_attestation",
        }
