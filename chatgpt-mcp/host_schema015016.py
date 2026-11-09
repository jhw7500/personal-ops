"""Read-only, source-pinned 015/016 schema evidence on tarororo-test.

This tool never runs DDL, backups, migration writers, provider requests or other
Docker operations. Its presence is NOT authorization to migrate or deploy.
It reports registered migration history and fixed object existence only; exact
constraint definitions and running app compatibility require separate reviews.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from host_runtime import ROLE_NAMES, _safe_file
from host_runtime_config import HostRuntimeError
from host_schema014 import _expected_checksums, _migration_path, EXPECTED_NAMES

DB = "tarororo_test"
DB_USER = "tarororo"
FIXED_SOURCES = {
    "015_public_demo_video_unlocks.sql": (
        "838f45aba60c0ae133336268b98ffc777eb8124c1e9d91651fc90269b68d64f7",
        b"-- Public free-video reading stage unlocks;"),
    "016_rewarded_ad_attempts.sql": (
        "f6acaf9a6e4ca88ec2b7f73369bb51968fd5f2f6b38689820074fda7c2b7beb0",
        b"-- Rewarded content access from client SDK events;"),
}
OBJECTS = (
    "reading_demo_video_unlocks",
    "reading_rewarded_ad_attempts",
    "reading_rewarded_stage_once",
    "reading_rewarded_attempt_rate",
)
EPOCH = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def expected_sources(root: Path) -> dict[str, str]:
    """Verify trusted SQL inventory and pinned bytes before opening the DB."""
    expected = _expected_checksums(root)
    if set(expected) != set(EXPECTED_NAMES):
        raise HostRuntimeError("SCHEMA015016_SOURCE_NOT_APPROVED")
    for name, (digest, prefix) in FIXED_SOURCES.items():
        raw, _mode = _safe_file(_migration_path(root, name))
        if (len(raw) > 8 * 1024 or not raw.startswith(prefix)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise HostRuntimeError("SCHEMA015016_SOURCE_NOT_APPROVED")
        expected[name] = digest
    return expected


def fixed_query(expected: dict[str, str]) -> str:
    if set(expected) != set(EXPECTED_NAMES) | set(FIXED_SOURCES):
        raise HostRuntimeError("SCHEMA015016_SOURCE_NOT_APPROVED")
    # Names and hashes arise only from fixed constants and checked source files.
    pairs = ",".join("('" + name + "','" + expected[name] + "')"
                     for name in (*EXPECTED_NAMES, *FIXED_SOURCES))
    names = ",".join("'" + name + "'" for name in (*EXPECTED_NAMES, *FIXED_SOURCES))
    objects = ",".join("'" + name + "',to_regclass('public." + name + "') IS NOT NULL"
                       for name in OBJECTS)
    return (
        "BEGIN READ ONLY; SET LOCAL statement_timeout='8s'; "
        "/* fixed_schema015016_status_v1 */ "
        "SELECT json_build_object("
        "'history',(SELECT json_object_agg(e.name,CASE "
        "WHEN a.name IS NULL THEN 'missing' "
        "WHEN a.checksum=e.checksum THEN 'checksum_match' "
        "ELSE 'checksum_mismatch' END) "
        "FROM (VALUES " + pairs + ") AS e(name,checksum) "
        "LEFT JOIN public.schema_migrations a ON a.name=e.name),"
        "'unexpected',(SELECT COUNT(*) FROM public.schema_migrations "
        "WHERE name NOT IN (" + names + ")),"
        "'objects',json_build_object(" + objects + "),"
        "'generation',json_build_object("
        "'database',current_database(),'user',current_user,"
        "'postmasterStartEpoch',extract(epoch from pg_postmaster_start_time())::text)"
        "); COMMIT;"
    )


def schema015016_status(runtime, project: str, target: str) -> dict:
    if (project, target) != ("tossApp", "tarororo-test"):
        raise HostRuntimeError("UNSUPPORTED_SCOPE")
    root = Path(runtime.guard(project, target)).resolve(strict=True)
    identity = runtime.identities()
    runtime._verify_identity(identity, health=True)
    expected = expected_sources(root)
    try:
        raw = runtime.run([
            "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB,
            "-c", fixed_query(expected),
        ], timeout=12).decode("ascii", errors="replace")
        evidence = next(json.loads(line) for line in raw.splitlines()
                        if line.startswith("{"))
    except (ValueError, StopIteration, AttributeError, TypeError) as exc:
        raise HostRuntimeError("SCHEMA015016_STATUS_UNKNOWN") from exc

    history = evidence.get("history") if type(evidence) is dict else None
    objects = evidence.get("objects") if type(evidence) is dict else None
    generation = evidence.get("generation") if type(evidence) is dict else None
    unexpected = evidence.get("unexpected") if type(evidence) is dict else None
    if (type(history) is not dict or set(history) != set(expected)
            or any(value not in ("missing", "checksum_match", "checksum_mismatch")
                   for value in history.values())
            or type(objects) is not dict or set(objects) != set(OBJECTS)
            or any(type(value) is not bool for value in objects.values())
            or type(unexpected) is not int or unexpected < 0
            or type(generation) is not dict
            or set(generation) != {"database", "user", "postmasterStartEpoch"}
            or generation["database"] != DB or generation["user"] != DB_USER
            or type(generation["postmasterStartEpoch"]) is not str
            or not EPOCH.fullmatch(generation["postmasterStartEpoch"])):
        raise HostRuntimeError("SCHEMA015016_STATUS_UNKNOWN")

    previous_ok = all(history[name] == "checksum_match" for name in EXPECTED_NAMES)
    new = {name: history[name] for name in FIXED_SOURCES}
    missing = all(value == "missing" for value in new.values())
    recorded = all(value == "checksum_match" for value in new.values())
    no_objects = not any(objects.values())
    has_objects = all(objects.values())
    # Missing and no object collision is only a precondition for a separate
    # migration plan; never imply approval, backup or restore readiness.
    ready_for_plan = previous_ok and unexpected == 0 and missing and no_objects
    observed_complete = previous_ok and unexpected == 0 and recorded and has_objects
    state = "missing" if ready_for_plan else "recorded_objects_present" if observed_complete else "inconsistent"
    return {
        "project": project, "target": target, "state": state,
        "ready_for_plan": ready_for_plan,
        "history_and_objects_match": observed_complete,
        "history": new, "objects_present": objects,
        "unexpected_history_count": unexpected,
        "predecessor_history_matches": previous_ok,
        "predecessor_checksums": {name: expected[name] for name in EXPECTED_NAMES},
        "expected_source_sha256": {name: expected[name] for name in FIXED_SOURCES},
        "generation": generation,
        "database_identity": {
            "container_id": identity["db"]["id"],
            "started_at": identity["db"]["started_at"],
        },
        "limitations": "History and object existence only; not DDL constraint or application-compatibility validation. No write.",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _same_identities(actual: dict, pinned: dict) -> bool:
    return all(
        all(actual[role][key] == pinned[role][key]
            for key in ("id", "started_at", "image_id"))
        for role in ("api", "worker", "db")
    )


def migrate_schema015016(runtime, state_dir, immutable, context, operation_id, *,
                        backup_fn=None, validate_fn=None, restore_fn=None) -> dict:
    """One separately approved, backup/restore-verified 015+016 TEST transaction.

    The tool manager must hold the host-operation and target locks. No caller
    can supply SQL, a database, target, table, or migration file. The approval
    is bound to an immutable reviewed snapshot and this fixed 015/016 pair.
    """
    from host_db_migration import _run_backup, _validate_archive, _restore_check
    from host_runtime_config import private_dir
    import os

    backup_fn = backup_fn or _run_backup
    validate_fn = validate_fn or _validate_archive
    restore_fn = restore_fn or _restore_check
    if immutable.get("authorization") != {"mode": "per_operation"}:
        raise HostRuntimeError("SCHEMA015016_APPROVAL_REQUIRED")
    if immutable.get("source_review", {}).get("policy") != "reviewed_snapshot":
        raise HostRuntimeError("SCHEMA015016_APPROVAL_REQUIRED")

    before = runtime.identities()
    runtime._verify_identity(before, health=True)
    if not _same_identities(before, immutable["identities"]):
        raise HostRuntimeError("STALE_PLAN")
    expected = expected_sources(Path(context))
    predecessors = {name: expected[name] for name in EXPECTED_NAMES}
    current_pair = {name: expected[name] for name in FIXED_SOURCES}
    if (immutable.get("migration_checksums") != predecessors
            or immutable.get("migration015016_checksums") != current_pair):
        raise HostRuntimeError("STALE_PLAN")

    status = schema015016_status(runtime, "tossApp", "tarororo-test")
    generation = immutable.get("database_generation")
    if (not status["ready_for_plan"] or status["generation"] != generation
            or status["expected_source_sha256"] != current_pair):
        raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
    epoch = generation.get("postmasterStartEpoch") if isinstance(generation, dict) else None
    if not isinstance(epoch, str) or not EPOCH.fullmatch(epoch):
        raise HostRuntimeError("INVALID_DB_GENERATION")

    backup_dir = private_dir(Path(state_dir) / "backups")
    archive = backup_dir / (operation_id + ".dump")
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        backup_bytes, backup_sha = backup_fn(output)
    validate_fn(archive)
    # A disposable restore is required before touching the live TEST database.
    # All 001-014 migration checksums must agree with the frozen plan.
    restore_fn(runtime, archive, operation_id, expected_history=14,
               expected_checksums=predecessors)

    after_backup = runtime.identities()
    latest = schema015016_status(runtime, "tossApp", "tarororo-test")
    if (not _same_identities(after_backup, before)
            or not latest["ready_for_plan"]
            or latest["generation"] != generation
            or latest["expected_source_sha256"] != current_pair):
        raise HostRuntimeError("SCHEMA_CHANGED")

    matching = " OR ".join(
        "(name='" + name + "' AND checksum='" + predecessors[name] + "')"
        for name in EXPECTED_NAMES
    )
    collisions = " OR ".join(
        "to_regclass('public." + name + "') IS NOT NULL" for name in OBJECTS
    )
    guard = (
        "DO $schema015016_guard$ BEGIN "
        "IF current_database()<>'tarororo_test' OR current_user<>'tarororo' "
        "OR extract(epoch from pg_postmaster_start_time())::text<>'" + epoch + "' "
        "THEN RAISE EXCEPTION 'database generation mismatch'; END IF; "
        "IF (SELECT COUNT(*) FROM public.schema_migrations)<>14 OR "
        "(SELECT COUNT(*) FROM public.schema_migrations WHERE " + matching + ")<>14 "
        "THEN RAISE EXCEPTION 'schema 014 history mismatch'; END IF; "
        "IF " + collisions + " "
        "THEN RAISE EXCEPTION 'schema 015/016 object collision'; END IF; "
        "END $schema015016_guard$;"
    )
    statements = [
        "BEGIN;",
        "SET LOCAL lock_timeout='5s';",
        "SET LOCAL statement_timeout='90s';",
        "SELECT pg_advisory_xact_lock(hashtext('tarororo_foundation_migrations'));",
        guard,
    ]
    for name in FIXED_SOURCES:
        raw, _mode = _safe_file(_migration_path(Path(context), name))
        if hashlib.sha256(raw).hexdigest() != current_pair[name]:
            raise HostRuntimeError("STALE_PLAN")
        statements += [
            raw.decode("utf8"),
            "INSERT INTO public.schema_migrations(name,checksum) VALUES ('"
            + name + "','" + current_pair[name] + "');",
        ]
    statements.append("COMMIT;")
    sql = "\n".join(statements)
    if len(sql.encode("utf8")) > 32 * 1024:
        raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    runtime.run([
        "docker", "exec", "-i", ROLE_NAMES["db"], "psql", "-X",
        "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB, "-c", sql,
    ], timeout=140, capture=False)

    after = runtime.identities()
    final = schema015016_status(runtime, "tossApp", "tarororo-test")
    if (not _same_identities(after, before)
            or not final["history_and_objects_match"]
            or final["generation"] != generation):
        raise HostRuntimeError("MIGRATION_POSTCHECK_FAILED")
    return {"backup_sha256": backup_sha, "backup_bytes": backup_bytes}
