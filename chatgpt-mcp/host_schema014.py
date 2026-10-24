"""Exact-SHA, backup-first Schema 014 migration for tarororo-test only.

No arbitrary SQL, migration file or DB identifiers are accepted from MCP callers.
Real writes require a separate reviewed-snapshot, per-operation admin approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from host_db_migration import _run_backup, _validate_archive, _restore_check
from host_runtime import PREDECESSOR_NAMES, SCHEMA_013_TABLES, ROLE_NAMES, _safe_file
from host_runtime_config import HostRuntimeError, private_dir

FIXED_NAME = "014_person_relationship_detail.sql"
# The specific SQL reviewed in tossApp, not a caller-supplied statement/hash.
FIXED_SHA256 = "a7b470a05007543be8751aec6d2ea8cd88891b08aa736bc72112b7417d56dac2"
FIXED_PREFIX = b"-- Preserve a user's free-text relationship label independently of the situation description."
EXPECTED_NAMES = (*PREDECESSOR_NAMES, FIXED_NAME)
DB = "tarororo_test"
DB_USER = "tarororo"
EPOCH = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _migration_path(root: Path, name: str) -> Path:
    """Reject a directory symlink before resolving any reviewed migration file."""
    root = Path(root).resolve(strict=True)
    for parent in (root / "server", root / "server" / "migrations"):
        if (parent.is_symlink() or not parent.is_dir()
                or not parent.resolve(strict=True).is_relative_to(root)):
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    return root / "server" / "migrations" / name


def checked_source(root: Path) -> tuple[bytes, str]:
    """Read only one reviewed, pinned SQL file from a non-symlinked source tree."""
    source = _migration_path(root, FIXED_NAME)
    raw, _mode = _safe_file(source)
    digest = hashlib.sha256(raw).hexdigest()
    if (not raw.startswith(FIXED_PREFIX) or len(raw) > 8 * 1024
            or digest != FIXED_SHA256):
        raise HostRuntimeError("SCHEMA014_SOURCE_NOT_APPROVED")
    return raw, digest


def _expected_checksums(root: Path) -> dict[str, str]:
    values = {}
    for name in EXPECTED_NAMES:
        if name == FIXED_NAME:
            _raw, values[name] = checked_source(root)
            continue
        raw, _mode = _safe_file(_migration_path(root, name))
        if len(raw) > 60 * 1024:
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
        values[name] = hashlib.sha256(raw).hexdigest()
    return values


def _query(expected: dict[str, str]) -> str:
    pairs = ",".join("('" + name + "','" + sha + "')" for name, sha in expected.items())
    names = ",".join("'" + name + "'" for name in expected)
    checks = ",".join("'" + name + "',to_regclass('public." + name + "') IS NOT NULL"
                      for name in SCHEMA_013_TABLES)
    return (
        "BEGIN READ ONLY; /* fixed_schema014_status_v1 */ "
        "SELECT json_build_object("
        "'history',(SELECT json_object_agg(e.name,CASE "
        "WHEN a.name IS NULL THEN 'missing' "
        "WHEN a.checksum=e.checksum THEN 'checksum_match' ELSE 'checksum_mismatch' END) "
        "FROM (VALUES " + pairs + ") AS e(name,checksum) "
        "LEFT JOIN public.schema_migrations a ON a.name=e.name),"
        "'unexpected',(SELECT COUNT(*) FROM public.schema_migrations "
        "WHERE name NOT IN (" + names + ")),"
        "'schema013',json_build_object(" + checks + "),"
        "'column',EXISTS(SELECT 1 FROM pg_attribute WHERE "
        "attrelid='public.persons'::regclass AND attname='relationship_detail' "
        "AND atttypid='pg_catalog.text'::regtype AND NOT attisdropped AND NOT attnotnull),"
        "'constraint',EXISTS(SELECT 1 FROM pg_constraint WHERE "
        "conname='persons_relationship_detail_valid' AND "
        "conrelid='public.persons'::regclass AND contype='c' AND convalidated),"
        "'counts',json_build_object("
        "'users',(SELECT COUNT(*) FROM public.users),"
        "'persons',(SELECT COUNT(*) FROM public.persons),"
        "'readings',(SELECT COUNT(*) FROM public.readings)),"
        "'generation',json_build_object("
        "'database',current_database(),'user',current_user,"
        "'postmasterStartEpoch',extract(epoch from pg_postmaster_start_time())::text)"
        "); COMMIT;"
    )


def schema014_status(runtime, project, target) -> dict:
    """Read-only 001–014 history, fixed column/constraint and aggregate counts.

    Read-only SQL never returns a user/Person ID, name, or reading content.
    """
    root = Path(runtime.guard(project, target)).resolve(strict=True)
    snapshot = runtime.identities()
    runtime._verify_identity(snapshot, health=True)
    expected = _expected_checksums(root)
    try:
        raw = runtime.run([
            "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB,
            "-c", _query(expected),
        ], timeout=12).decode("ascii", errors="replace")
        data = next(json.loads(line) for line in raw.splitlines() if line.startswith("{"))
    except (ValueError, StopIteration, AttributeError, TypeError) as exc:
        raise HostRuntimeError("SCHEMA014_STATUS_UNKNOWN") from exc

    histories = data.get("history") if isinstance(data, dict) else None
    tables = data.get("schema013") if isinstance(data, dict) else None
    generation = data.get("generation") if isinstance(data, dict) else None
    counts = data.get("counts") if isinstance(data, dict) else None
    extra = data.get("unexpected") if isinstance(data, dict) else None
    column = data.get("column") if isinstance(data, dict) else None
    constraint = data.get("constraint") if isinstance(data, dict) else None
    if (not isinstance(histories, dict) or set(histories) != set(EXPECTED_NAMES)
            or any(value not in ("missing", "checksum_match", "checksum_mismatch")
                   for value in histories.values())
            or not isinstance(tables, dict) or set(tables) != set(SCHEMA_013_TABLES)
            or any(type(value) is not bool for value in tables.values())
            or type(column) is not bool or type(constraint) is not bool
            or type(extra) is not int or extra < 0
            or not isinstance(counts, dict) or set(counts) != {"users", "persons", "readings"}
            or any(type(value) is not int or value < 0 for value in counts.values())
            or not isinstance(generation, dict)
            or set(generation) != {"database", "user", "postmasterStartEpoch"}
            or generation["database"] != DB or generation["user"] != DB_USER
            or not isinstance(generation["postmasterStartEpoch"], str)
            or not EPOCH.fullmatch(generation["postmasterStartEpoch"])):
        raise HostRuntimeError("SCHEMA014_STATUS_UNKNOWN")

    base_ready = (extra == 0 and all(tables.values())
                  and all(histories[name] == "checksum_match" for name in PREDECESSOR_NAMES))
    missing = histories[FIXED_NAME] == "missing"
    ready_to_prepare = base_ready and missing and not column and not constraint
    complete = base_ready and histories[FIXED_NAME] == "checksum_match" and column and constraint
    status = ("ready" if complete else
              "missing" if ready_to_prepare else
              "checksum_mismatch" if histories[FIXED_NAME] == "checksum_mismatch" else
              "incomplete")
    return {
        "project": project, "target": target, "migration": FIXED_NAME,
        "state": status, "ready_to_prepare": ready_to_prepare, "complete": complete,
        "history": histories, "checksum": expected[FIXED_NAME],
        "predecessor_checksums": {name: expected[name] for name in PREDECESSOR_NAMES},
        "unexpected_history_count": extra,
        "column_present": column, "constraint_present": constraint,
        "schema_013_tables_present": tables, "counts": counts,
        "generation": generation,
        "database_identity": {"container_id": snapshot["db"]["id"],
                              "started_at": snapshot["db"]["started_at"]},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _identity_matches(current: dict, expected: dict) -> bool:
    return all(all(current[role][name] == expected[role][name]
                   for name in ("id", "started_at", "image_id"))
               for role in ("api", "worker", "db"))


def migrate_schema014(runtime, state_dir, immutable, context, operation_id, *,
                      backup_fn=_run_backup, validate_fn=_validate_archive,
                      restore_fn=_restore_check):
    """Pinned backup -> disposable restore -> exact 014 SQL, with no service restart."""
    expected_identity = immutable["identities"]
    before = runtime.identities()
    runtime._verify_identity(before, health=True)
    if not _identity_matches(before, expected_identity):
        raise HostRuntimeError("STALE_PLAN")
    root = Path(context)
    source, digest = checked_source(root)
    if digest != immutable["migration_checksum"]:
        raise HostRuntimeError("STALE_PLAN")
    before_status = schema014_status(runtime, "tossApp", "tarororo-test")
    expected_generation = immutable["database_generation"]
    expected_history = immutable["migration_checksums"]
    if (not before_status["ready_to_prepare"]
            or before_status["generation"] != expected_generation
            or before_status["predecessor_checksums"] != expected_history):
        raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")

    backup_dir = private_dir(Path(state_dir) / "backups")
    archive = backup_dir / (operation_id + ".dump")
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        backup_bytes, backup_sha = backup_fn(output)
    validate_fn(archive)
    restore_fn(runtime, archive, operation_id, expected_history=13,
               expected_checksums=expected_history)

    after_backup = runtime.identities()
    latest = schema014_status(runtime, "tossApp", "tarororo-test")
    if (not _identity_matches(after_backup, before) or not latest["ready_to_prepare"]
            or latest["generation"] != expected_generation
            or latest["predecessor_checksums"] != expected_history
            or latest["counts"] != before_status["counts"]):
        raise HostRuntimeError("SCHEMA_CHANGED")

    epoch = expected_generation["postmasterStartEpoch"]
    if not isinstance(epoch, str) or not EPOCH.fullmatch(epoch):
        raise HostRuntimeError("INVALID_DB_GENERATION")
    matching = " OR ".join(
        "(name='" + name + "' AND checksum='" + expected_history[name] + "')"
        for name in PREDECESSOR_NAMES
    )
    guard = (
        "DO $schema014_guard$ BEGIN "
        "IF current_database()<>'tarororo_test' OR current_user<>'tarororo' "
        "OR extract(epoch from pg_postmaster_start_time())::text<>'" + epoch + "' "
        "THEN RAISE EXCEPTION 'database generation mismatch'; END IF; "
        "IF (SELECT COUNT(*) FROM public.schema_migrations)<>13 OR "
        "(SELECT COUNT(*) FROM public.schema_migrations WHERE " + matching + ")<>13 "
        "THEN RAISE EXCEPTION 'schema 013 history mismatch'; END IF; "
        "IF EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid='public.persons'::regclass "
        "AND attname='relationship_detail' AND NOT attisdropped) OR "
        "EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname='persons_relationship_detail_valid' "
        "AND conrelid='public.persons'::regclass) "
        "THEN RAISE EXCEPTION 'schema 014 object collision'; END IF; "
        "END $schema014_guard$;"
    )
    statements = [
        "BEGIN;",
        "SET LOCAL lock_timeout='5s';",
        "SET LOCAL statement_timeout='90s';",
        "SELECT pg_advisory_xact_lock(hashtext('tarororo_foundation_migrations'));",
        guard, source.decode("utf8"),
        "INSERT INTO public.schema_migrations(name,checksum) VALUES ('"
        + FIXED_NAME + "','" + digest + "');",
        "COMMIT;",
    ]
    sql = "\n".join(statements)
    if len(sql.encode("utf8")) > 32 * 1024:
        raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    runtime.run([
        "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X",
        "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB,
        "-c", sql,
    ], timeout=140, capture=False)

    after = runtime.identities()
    final = schema014_status(runtime, "tossApp", "tarororo-test")
    # A committed DDL migration does not mutate Person or Reading rows. A live
    # test application may create/delete records *after* COMMIT, so equal row
    # counts are not a reliable post-commit success gate. The pre-DDL check
    # above still aborts on count drift while the backup is being validated.
    if (not _identity_matches(after, before)
            or not final["complete"] or final["generation"] != expected_generation):
        raise HostRuntimeError("MIGRATION_POSTCHECK_FAILED")
    return {"backup_sha256": backup_sha, "backup_bytes": backup_bytes,
            "postcommit_counts_changed": final["counts"] != before_status["counts"]}
