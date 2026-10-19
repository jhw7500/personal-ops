"""Fixed-scope backup-first tarororo-test schema migrations (006-012, 013 or 006-013).

Invoked only within HostRuntimeOperations' cross-process target lock after a
source-pinned, target-authorized or separately approved plan. Never permits
arbitrary SQL, DB target, migration name, or backup path from a chat.
The retained archive is validated before a disposable restore and any DDL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import selectors
import signal
import subprocess
import time

from host_runtime import (_safe_env, _safe_file, PREDECESSOR_NAMES, PREDECESSOR_TABLES,
                          PREDECESSOR_INDEXES, PREDECESSOR_FUNCTIONS, SCHEMA_013_TABLES)
from host_runtime_config import HostRuntimeError, private_dir

LIMIT_BYTES = 512 * 1024 * 1024
DUMP_TIMEOUT_SECONDS = 300
FIXED_MIGRATION = "013_staged_reading_access.sql"
DUMP = ("docker", "exec", "-i", "tarororo-test-db-1", "pg_dump",
        "-Fc", "--no-owner", "--no-privileges", "-U", "tarororo", "-d", "tarororo_test")
CHECK = ("docker", "exec", "-i", "tarororo-test-db-1", "pg_restore", "--list")


def _run_backup(output):
    """Stream a capped pg_dump to an owner-only fd; never store DB rows in logs."""
    try:
        proc = subprocess.Popen(DUMP, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=_safe_env(),
                                start_new_session=True)
    except OSError:
        raise HostRuntimeError("BACKUP_DUMP_START_FAILED") from None
    selector = selectors.DefaultSelector()
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + DUMP_TIMEOUT_SECONDS
    try:
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostRuntimeError("BACKUP_TIMEOUT")
            for key, _ in selector.select(min(remaining, 0.25)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > LIMIT_BYTES:
                    raise HostRuntimeError("BACKUP_TOO_LARGE")
                output.write(chunk)
                digest.update(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or proc.wait(timeout=remaining) != 0:
            raise HostRuntimeError("BACKUP_FAILED")
        if total < 100:
            raise HostRuntimeError("BACKUP_DUMP_TOO_SMALL")
        output.flush()
        os.fsync(output.fileno())
        return total, digest.hexdigest()
    except subprocess.TimeoutExpired:
        raise HostRuntimeError("BACKUP_DUMP_TIMEOUT") from None
    except OSError:
        raise HostRuntimeError("BACKUP_DUMP_IO_FAILED") from None
    finally:
        selector.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def _validate_archive(path):
    """Validate readable custom dump catalog without exposing its contents."""
    try:
        # subprocess inherits the OS fd, not BufferedReader's logical cursor.
        # Header read-ahead must not leave that fd past PGDMP after seek(0).
        stream = path.open("rb", buffering=0)
    except OSError:
        raise HostRuntimeError("BACKUP_ARCHIVE_UNREADABLE") from None
    with stream:
        if stream.read(5) != b"PGDMP":
            raise HostRuntimeError("BACKUP_HEADER_INVALID")
        stream.seek(0)
        try:
            result = subprocess.run(CHECK, stdin=stream, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60,
                                    env=_safe_env(), start_new_session=True,
                                    check=False)
        except subprocess.TimeoutExpired:
            raise HostRuntimeError("BACKUP_CATALOG_TIMEOUT") from None
        except OSError:
            raise HostRuntimeError("BACKUP_CATALOG_EXEC_FAILED") from None
        if result.returncode != 0:
            raise HostRuntimeError("BACKUP_CATALOG_REJECTED")



def _restore_check(runtime, path, operation_id, *, expected_history=12,
                   expected_checksums=None):
    """Restore the captured archive to an isolated temporary database.

    This performs bounded writes ONLY to an operation-scoped database inside
    the pinned tarororo-test PostgreSQL container; never modifies tarororo_test.
    Cleanup must succeed before the real migration is allowed.
    """
    import re
    from host_runtime_config import private_file

    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise HostRuntimeError("INVALID_OPERATION_ID")
    if type(expected_history) is not int or expected_history not in (5, 12, 13):
        raise HostRuntimeError("BACKUP_RESTORE_INVALID")
    if expected_checksums is not None and (
            not isinstance(expected_checksums, dict)
            or len(expected_checksums) != expected_history
            or any(not isinstance(k, str)
                   or k not in PREDECESSOR_NAMES[:expected_history]
                   or not isinstance(v, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", v)
                   for k, v in expected_checksums.items())
            or set(expected_checksums) != set(PREDECESSOR_NAMES[:expected_history])):
        raise HostRuntimeError("BACKUP_RESTORE_INVALID")
    private_file(path)
    database = "tarororo_restore_" + operation_id
    prefix = ["docker", "exec", "-i", "tarororo-test-db-1"]
    created = False
    error = None
    try:
        runtime.run([*prefix, "createdb", "-U", "tarororo",
                     "--template=template0", database], timeout=15, capture=False)
        created = True
        with path.open("rb") as stream:
            result = subprocess.run(
                [*prefix, "pg_restore", "--format=custom", "--exit-on-error",
                 "--single-transaction", "--no-owner", "--no-privileges",
                 "-U", "tarororo", "-d", database],
                stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_safe_env(), timeout=240, check=False, start_new_session=True)
        if result.returncode != 0:
            raise HostRuntimeError("BACKUP_RESTORE_FAILED")
        sql = ("BEGIN READ ONLY; SELECT COUNT(*) FROM public.schema_migrations; COMMIT;"
               if expected_checksums is None else
               "BEGIN READ ONLY; SELECT json_object_agg(name,checksum ORDER BY name) "
               "FROM public.schema_migrations; COMMIT;")
        verified = runtime.run(
            [*prefix, "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
             "-U", "tarororo", "-d", database, "-c", sql],
            timeout=10).decode("ascii", errors="replace")
        if expected_checksums is None:
            if not any(line.strip() == str(expected_history)
                       for line in verified.splitlines()):
                raise HostRuntimeError("BACKUP_RESTORE_INVALID")
        else:
            try:
                actual = next(json.loads(line) for line in verified.splitlines()
                              if line.startswith("{"))
            except (ValueError, StopIteration) as exc:
                raise HostRuntimeError("BACKUP_RESTORE_INVALID") from exc
            if actual != expected_checksums:
                raise HostRuntimeError("BACKUP_RESTORE_INVALID")
    except subprocess.TimeoutExpired:
        error = HostRuntimeError("BACKUP_RESTORE_TIMEOUT")
    except (OSError, HostRuntimeError) as exc:
        error = exc if isinstance(exc, HostRuntimeError) else HostRuntimeError("BACKUP_RESTORE_FAILED")
    finally:
        if created:
            try:
                runtime.run([*prefix, "dropdb", "--if-exists", "--force",
                             "-U", "tarororo", database], timeout=20, capture=False)
            except (OSError, HostRuntimeError):
                # Never proceed with migration if a temporary database lingers.
                raise HostRuntimeError("BACKUP_RESTORE_CLEANUP_FAILED") from None
    if error is not None:
        raise error


def migrate_013(runtime, state_dir, immutable, context, operation_id, *,
                backup_fn=_run_backup, validate_fn=_validate_archive,
                restore_fn=_restore_check):
    """Backup, verify identities and apply ONE reviewed source SQL in one transaction."""
    expected = immutable["identities"]
    before = runtime.identities()
    runtime._verify_identity(before, health=True)
    for role in ("api", "worker", "db"):
        if any(before[role][key] != expected[role][key]
               for key in ("id", "started_at", "image_id")):
            raise HostRuntimeError("STALE_PLAN")
    status = runtime.migration_status("tossApp", "tarororo-test")
    if (status["state"] != "missing" or not status["prerequisites_ok"]
            or any(status["tables"].values())):
        raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")
    source = Path(context) / "server/migrations" / FIXED_MIGRATION
    if source.is_symlink() or not source.is_file():
        raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    sql = source.read_text(encoding="utf8")
    if hashlib.sha256(sql.encode()).hexdigest() != immutable["migration_checksum"]:
        raise HostRuntimeError("STALE_PLAN")
    if len(sql) > 60 * 1024 or not sql.startswith("-- Durable credit packs"):
        raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    archive_dir = private_dir(Path(state_dir) / "backups")
    archive = archive_dir / (operation_id + ".dump")
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            size, digest = backup_fn(output)
        validate_fn(archive)
        restore_fn(runtime, archive, operation_id)
        after_backup = runtime.identities()
        for role in ("api", "worker", "db"):
            if any(after_backup[role][key] != before[role][key]
                   for key in ("id", "started_at", "image_id")):
                raise HostRuntimeError("STALE_PLAN")
        current = runtime.migration_status("tossApp", "tarororo-test")
        if (current["state"] != "missing" or not current["prerequisites_ok"]
                or any(current["tables"].values())):
            raise HostRuntimeError("SCHEMA_CHANGED")
        # Source hash embedded only as 64 fixed hex characters, not model SQL.
        checksum = immutable["migration_checksum"]
        statement = (
            "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='45s';\n" +
            sql + "\nINSERT INTO schema_migrations(name,checksum) VALUES ('" +
            FIXED_MIGRATION + "','" + checksum + "'); COMMIT;"
        )
        runtime.run(["docker", "exec", "-i", "tarororo-test-db-1",
                     "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "tarororo",
                     "-d", "tarororo_test", "-c", statement],
                    timeout=90, capture=False)
        final = runtime.migration_status("tossApp", "tarororo-test")
        actual = runtime.identities()
        if (final["state"] != "ready"
                or any(any(actual[role][key] != before[role][key]
                           for key in ("id", "started_at", "image_id"))
                       for role in ("api", "worker", "db"))):
            raise HostRuntimeError("MIGRATION_POSTCHECK_FAILED")
        return {"backup_sha256": digest, "backup_bytes": size}
    except BaseException:
        # Retain even a partial backup for operator investigation; never auto-restore.
        raise

def _migrate_predecessor_range(runtime, state_dir, immutable, context, operation_id, *,
                               include_013, backup_fn, validate_fn, restore_fn):
    """Apply the fixed predecessor range, optionally including reviewed 013."""
    migration_checksums = immutable["migration_checksums"]
    if include_013:
        if (set(migration_checksums) != set(PREDECESSOR_NAMES)
                or migration_checksums[FIXED_MIGRATION] != immutable["migration_checksum"]):
            raise HostRuntimeError("STALE_PLAN")
        status_checksums = {
            name: migration_checksums[name] for name in PREDECESSOR_NAMES[5:12]
        }
    else:
        status_checksums = migration_checksums
    expected = immutable["identities"]
    before = runtime.identities()
    runtime._verify_identity(before, health=True)
    for role in ("api", "worker", "db"):
        if any(before[role][key] != expected[role][key]
               for key in ("id", "started_at", "image_id")):
            raise HostRuntimeError("STALE_PLAN")
    status = runtime.predecessor_status("tossApp", "tarororo-test")
    generation = immutable["database_generation"]
    if (not status["ready_to_prepare"] or status["generation"] != generation
            or status["checksums"] != status_checksums):
        raise HostRuntimeError("SCHEMA_NOT_SAFE_TO_MIGRATE")

    source = Path(context) / "server" / "migrations"
    if not source.is_dir() or source.is_symlink():
        raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
    reviewed = {}
    for name in PREDECESSOR_NAMES:
        path = source / name
        raw, _ = _safe_file(path)
        if len(raw) > 60 * 1024:
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = migration_checksums.get(name)
        if expected_digest is not None and expected_digest != digest:
            raise HostRuntimeError("STALE_PLAN")
        if include_013 and name == FIXED_MIGRATION and not raw.startswith(
                b"-- Durable credit packs"):
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
        try:
            reviewed[name] = (raw.decode("utf8"), digest)
        except UnicodeDecodeError as exc:
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE") from exc

    archive_dir = private_dir(Path(state_dir) / "backups")
    archive = archive_dir / (operation_id + ".dump")
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            size, digest = backup_fn(output)
        validate_fn(archive)
        restore_fn(runtime, archive, operation_id, expected_history=5,
                   expected_checksums={name: reviewed[name][1]
                                       for name in PREDECESSOR_NAMES[:5]})
        after_backup = runtime.identities()
        for role in ("api", "worker", "db"):
            if any(after_backup[role][key] != before[role][key]
                   for key in ("id", "started_at", "image_id")):
                raise HostRuntimeError("STALE_PLAN")
        current = runtime.predecessor_status("tossApp", "tarororo-test")
        if (not current["ready_to_prepare"] or current["generation"] != generation
                or current["checksums"] != status_checksums):
            raise HostRuntimeError("SCHEMA_CHANGED")
        epoch = generation["postmasterStartEpoch"]
        if not isinstance(epoch, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", epoch):
            raise HostRuntimeError("INVALID_DB_GENERATION")

        # Guard again *inside* the same advisory-locked transaction as the SQL
        # changes, to close the gap between read-only preflight and migration.
        expected_match = " OR ".join(
            "(name='" + name + "' AND checksum='" + reviewed[name][1] + "')"
            for name in PREDECESSOR_NAMES[:5]
        )
        collision_names = (*PREDECESSOR_TABLES, *PREDECESSOR_INDEXES, *SCHEMA_013_TABLES)
        collisions = " OR ".join("to_regclass('public." + name + "') IS NOT NULL"
                                 for name in collision_names)
        function_collisions = " OR ".join(
            "to_regprocedure('public." + name + "()') IS NOT NULL"
            for name in PREDECESSOR_FUNCTIONS
        )
        guard = (
            "DO $predecessor_guard$ BEGIN "
            "IF current_database()<>'tarororo_test' OR current_user<>'tarororo' "
            "OR extract(epoch from pg_postmaster_start_time())::text<>'" + epoch + "' "
            "THEN RAISE EXCEPTION 'database generation mismatch'; END IF; "
            "IF (SELECT COUNT(*) FROM public.schema_migrations)<>5 OR "
            "(SELECT COUNT(*) FROM public.schema_migrations WHERE " +
            expected_match + ")<>5 THEN "
            "RAISE EXCEPTION 'predecessor history mismatch'; END IF; "
            "IF " + collisions + " OR " + function_collisions + " THEN "
            "RAISE EXCEPTION 'predecessor schema collision'; END IF; "
            "END $predecessor_guard$;"
        )
        statements = [
            "BEGIN;",
            "SET LOCAL lock_timeout='5s';",
            "SET LOCAL statement_timeout='180s';",
            "SELECT pg_advisory_xact_lock(hashtext('tarororo_foundation_migrations'));",
            guard,
        ]
        migration_names = PREDECESSOR_NAMES[5:] if include_013 else PREDECESSOR_NAMES[5:12]
        for name in migration_names:
            sql, checksum = reviewed[name]
            statements.append(sql)
            statements.append(
                "INSERT INTO public.schema_migrations(name,checksum) VALUES ('" +
                name + "','" + checksum + "');"
            )
        statements.append("COMMIT;")
        statement = "\n".join(statements)
        if len(statement.encode("utf8")) > 256 * 1024:
            raise HostRuntimeError("UNSAFE_MIGRATION_SOURCE")
        runtime.run([
            "docker", "exec", "-i", "tarororo-test-db-1",
            "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "tarororo",
            "-d", "tarororo_test", "-c", statement,
        ], timeout=240, capture=False)

        final = runtime.predecessor_status("tossApp", "tarororo-test")
        after = runtime.identities()
        identity_changed = any(
            any(after[role][key] != before[role][key]
                for key in ("id", "started_at", "image_id"))
            for role in ("api", "worker", "db")
        )
        if include_013:
            migration = runtime.migration_status("tossApp", "tarororo-test")
            postcheck_failed = (
                final["generation"] != generation
                or any(final["history"][name] != "checksum_match"
                       for name in PREDECESSOR_NAMES)
                or final["unexpected_history_count"] != 0
                or not all(final["tables_present"].values())
                or set(final.get("indexes_present", {})) != set(PREDECESSOR_INDEXES)
                or not all(final.get("indexes_present", {}).values())
                or set(final.get("functions_present", {})) != set(PREDECESSOR_FUNCTIONS)
                or not all(final.get("functions_present", {}).values())
                or not all(final["schema_013_tables_present"].values())
                or migration["state"] != "ready"
                or not migration["prerequisites_ok"]
                or not all(migration["tables"].values())
                or identity_changed
            )
        else:
            postcheck_failed = (
                not final["complete"] or final["generation"] != generation
                or final["history"][PREDECESSOR_NAMES[12]] != "missing"
                or any(final["schema_013_tables_present"].values())
                or identity_changed
            )
        if postcheck_failed:
            raise HostRuntimeError("MIGRATION_POSTCHECK_FAILED")
        return {"backup_sha256": digest, "backup_bytes": size}
    except BaseException:
        # Failure/unknown COMMIT never auto-runs the migration again or restores.
        raise


def migrate_predecessors(runtime, state_dir, immutable, context, operation_id, *,
                         backup_fn=_run_backup, validate_fn=_validate_archive,
                         restore_fn=_restore_check):
    """One approved, fixed 006-012 transaction; 013 is never part of this command."""
    return _migrate_predecessor_range(
        runtime, state_dir, immutable, context, operation_id,
        include_013=False, backup_fn=backup_fn, validate_fn=validate_fn,
        restore_fn=restore_fn,
    )


def migrate_schema_update(runtime, state_dir, immutable, context, operation_id, *,
                          backup_fn=_run_backup, validate_fn=_validate_archive,
                          restore_fn=_restore_check):
    """One backup and transaction for the fixed reviewed 006-013 schema update."""
    return _migrate_predecessor_range(
        runtime, state_dir, immutable, context, operation_id,
        include_013=True, backup_fn=backup_fn, validate_fn=validate_fn,
        restore_fn=restore_fn,
    )
