"""MCP tool descriptors for exact-target HostRuntime operations."""
from __future__ import annotations

_PROJECT = {"type": "string", "enum": ["tossApp"]}
_TARGET = {"type": "string", "enum": ["tarororo-test"]}
_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,80}$"}


def _tool(name, title, description, fields=None, required=(), *, readonly=True):
    return {
        "name": name, "title": title, "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT, "target": _TARGET, **(fields or {})},
            "required": ["project", "target", *required], "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": readonly,
            "destructiveHint": not readonly,
            "idempotentHint": readonly,
            "openWorldHint": False,
        },
    }


HOST_RUNTIME_TOOL_SPECS = [
    _tool("get_runtime_status", "호스트 테스트 스택 조회",
          "Read only the dedicated tarororo-test Docker API, worker, DB identities, "
          "readiness and approved environment fields. No credentials, logs, or SQL writes."),
    _tool("get_db_migration_status", "테스트 DB 마이그레이션 진단",
          "Inspect only test database migration 013 history, checksum and required table "
          "presence via read-only SQL; no row data or writes."),
    _tool("get_predecessor_migration_status", "DB 006~012 선행 이력 진단",
          "Read-only 001-013 exact checksum/history classification and fixed object presence "
          "in tarororo-test. No business rows or SQL writes."),
    _tool("get_schema014_migration_status", "DB 014 마이그레이션 진단",
          "Read only the fixed Schema 014 history, 001-013 prerequisites, column and constraint "
          "presence and aggregate counts in tarororo-test. Never returns record content."),
    _tool("compare_deployment", "테스트 배포 상태 비교",
          "Read approved build inputs and image identities. Never claim source parity "
          "unless matching prior deployment attestation can be verified."),
    _tool("prepare_test_deploy", "테스트 API 배포 계획",
          "Create a source-pinned, time-limited API-or-Worker deployment plan. "
          "An active fixed-target authorization permits a later explicit execute call without "
          "per-plan approval; otherwise separate management-web approval is required. "
          "The ready_to_execute response includes approval_required=false and the next execute "
          "call. Planning does not build, restart services, or alter the database. Registered "
          "reviewed_snapshot targets accept uncommitted inputs; no Git commit is required.",
          {"component": {"type": "string", "enum": ["api", "worker"]},
           "mode": {"type": "string", "enum": ["toss-demo", "disabled"]},
           "request_id": _REQUEST},
          ("component", "mode", "request_id"), readonly=False),
    _tool("prepare_db_migration", "테스트 DB 스키마 적용 계획",
          "Create a source-pinned, backup-first schema plan for the isolated tarororo-test "
          "database only. With an active fixed-target authorization and a verified 001-005 "
          "baseline, missing 006-012 and 013 are pinned into one 006-013 plan; otherwise the "
          "existing separate 013/per-operation approval policy remains. Planning never writes SQL. "
          "A ready_to_execute response with approval_required=false needs only the user's "
          "explicit execution request; call its next_action without another web approval. "
          "Changing any fixed 001-013 SQL bytes invalidates the target grant until reauthorized. "
          "No arbitrary SQL or restoration.",
          {"request_id": _REQUEST}, ("request_id",), readonly=False),
    _tool("prepare_schema014_migration", "DB Schema 014 전용 승인 계획",
          "Prepare one immutable, exact-SHA Schema 014 plan on tarororo-test only. Requires "
          "reviewed_snapshot and returns an inline one-use user confirmation card for that exact "
          "plan; the authenticated management web is fallback only when the card is unavailable. "
          "Existing fixed-target grants never authorize this new migration. Approval only issues "
          "a receipt and never writes the database or starts execution.",
          {"request_id": _REQUEST}, ("request_id",), readonly=False),
    _tool("prepare_predecessor_migration", "DB 006~012 전용 계획",
          "Prepare only exact 006-012 migrations with 005-matched baseline, source pinning, "
          "backup+disposable restore check. Excludes 013. An active fixed-target authorization "
          "permits a later explicit execute call; otherwise a new separate host approval is required. "
          "Changing any fixed 001-013 SQL bytes requires target reauthorization. "
          "Registered reviewed_snapshot targets need no extra scope-enable step or Git commit; "
          "Without a target grant, review and approve the frozen source in the management web. "
          "Does not execute SQL writes, backup, Docker deploy or service restart.",
          {"request_id": _REQUEST}, ("request_id",), readonly=False),
    _tool("execute_predecessor_migration", "승인된 DB 006~012 적용",
          "Explicitly start a backup-first, all-or-nothing 006-012 migration on tarororo-test "
          "under its still-active pinned target grant or local single-use approval; never include "
          "013. No service restart.",
          {"operation_id": _ID}, ("operation_id",), readonly=False),
    _tool("execute_db_migration", "승인된 테스트 DB 스키마 적용",
          "Explicitly execute a prepared exact 013 or combined 006-013 plan. Combined execution "
          "requires the same still-active fixed-target grant pinned by the plan; old and "
          "per-operation plans still require their separate single-use approval.",
          {"operation_id": _ID}, ("operation_id",), readonly=False),
    _tool("execute_schema014_migration", "승인된 테스트 DB 014 적용",
          "Execute only a prepared and separately approved exact-source Schema 014 migration "
          "after backup, disposable restore and database generation verification. No service restart.",
          {"operation_id": _ID}, ("operation_id",), readonly=False),
    _tool("execute_test_deploy", "승인된 테스트 API 배포",
          "Start only a previously planned API-or-Worker deploy under its still-valid pinned "
          "target grant or separate one-use host approval. "
          "Never accept a model-supplied approval token or arbitrary command.",
          {"operation_id": _ID}, ("operation_id",), readonly=False),
    _tool("get_host_operation", "호스트 배포 작업 상태",
          "Read deployment operation state and safe metadata. A ready_to_execute plan with "
          "approval_required=false is already target-authorized: use its next_action only if "
          "the user requested execution; do not request another web approval. Expired plans "
          "need a new request_id, not approval. For a pending Schema 014 plan, open its exact "
          "inline card with open_schema014_approval; use the admin page only as fallback. "
          "Queued, running or unknown "
          "outcomes are never reported as success.",
          {"operation_id": _ID}, ("operation_id",)),
]
