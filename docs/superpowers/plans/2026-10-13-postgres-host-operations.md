# PostgreSQL Host Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 등록된 test PostgreSQL에 파일로 고정한 query/DML/migration/backup/restore를 승인 후 실행하며 013 분리를 강제한다.

**Architecture:** A의 registry·operation manager·bounded runner에 PostgreSQL adapter를 연결한다. SQL/역할 사전 검증, backup/restore rehearsal, 실행과 pre-commit 검사를 책임별로 분리하며 legacy `migrate_013`은 고정 경로로 유지한다.

**Tech Stack:** Python stdlib/unittest, 등록된 PostgreSQL `psql`·`pg_dump`·`pg_restore`, 기존 Docker CLI. DB client 라이브러리 추가 없음.

**Spec:** [설계안](../specs/2026-10-13-chat-host-operations-design.md), [전체 계획](2026-10-13-chat-host-operations.md), [A 계약](2026-10-13-registered-host-operations.md)

## Global Constraints

전체 계획의 Global Constraints를 적용한다. 기존 `tarororo-test` 컨테이너/DB는 구현·회귀 시험 대상이 아니다. generic DML/migration 역할은 non-superuser여야 한다. 역할 생성·grant·credential 설정은 자동 실행하지 않는다. 006–012에 013을 포함하지 않는다.

## Review Focus

- B1: SQL client/transaction/role 변경 명령과 인용·주석 경계 우회를 실행 전에 거절한다.
- B1: 실제 역할이 과도한 권한을 갖거나 자격 증명을 사용할 수 없으면 해당 capability를 닫는다.
- B2: backup·restore rehearsal·cleanup 실패는 DB 변경으로 이어지지 않는다.
- B3: history mismatch·부분 적용·동시 migrate·보호된 013 변경은 transaction commit 전에 차단한다.
- B3: 승인한 query 결과만 상한 내 반환하며 데이터·SQL·credentials를 event/log에 기록하지 않는다.

---

## B1. SQL artifact·역할·출력 정책

**Files:** Create `chatgpt-mcp/host_postgres_policy.py`, `tests/test_host_postgres_policy.py`. Modify A의 `host_actions.py`, `host_registry.py`와 해당 테스트.

**Interfaces:**
- `SQLArtifact`: `sha256: str`, `statements: tuple[str, ...]`, `mode: str`를 가진 immutable record.
- `parse_sql_artifact(payload: bytes, *, mode: str) -> SQLArtifact`; mode는 `query|dml|migration`.
- `validate_postgres_role(observed: dict, expected: dict, *, capability: str) -> None`.
- `validate_query_output(rows: list[dict], *, columns: tuple[str, ...]) -> dict`: 승인 column만 담은 rows, row_count, truncated를 반환한다.

- [ ] **Step 1: 실패 테스트 작성.** NUL/invalid UTF-8/256 KiB 초과, psql metacommand, BEGIN/COMMIT/ROLLBACK/SAVEPOINT/SET ROLE, nested comments, E-string·인용 identifier·dollar quote, unterminated 입력을 검사한다. query는 단일 SELECT/WITH만 허용한다. DML은 등록된 역할의 허용 테이블에서 INSERT/UPDATE/DELETE만 수행하며 DDL은 migration 전용이다.

```python
def test_client_or_transaction_escape_is_denied(self):
    for sql in (b"\\! touch /tmp/x", b"COMMIT; DELETE FROM items", b"SET ROLE admin"):
        with self.subTest(sql=sql), self.assertRaises(HostRuntimeError):
            parse_sql_artifact(sql, mode="dml")
```

- [ ] **Step 2: RED 확인.** `rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python chatgpt-mcp/tests/test_host_postgres_policy.py -v`.
- [ ] **Step 3: 구현.** stdlib lexer로 quoted strings/identifiers·nested comments·dollar tags와 statement 경계를 구분한다. 지원하지 못하는 구문은 fail-closed로 처리한다. SQL 텍스트 정규식만을 권한 검증으로 사용하지 않는다. connection/session/search_path와 standard_conforming_strings를 trusted runner가 고정한다.
- [ ] **Step 4: 역할·범위·결과 검사 구현.** superuser/createdb/createrole/replication/bypassrls 및 서버 파일/프로그램 권한의 직접·상속 membership을 generic 역할에서 거절한다. 역할 이름·DB identity·허용 schema/table grant를 registry와 비교한다. DML 역할에는 DDL·schema_migrations 쓰기를 주지 않는다. `db.inspect|query|execute|migrate|backup|restore`를 action whitelist에 추가하고 client의 raw SQL/DSN/backup 절대경로는 거절한다. query 출력은 최대 100행·64 KiB, 초과는 truncated 또는 RESULT_TOO_LARGE로 명확히 구분한다.
- [ ] **Step 5: GREEN 확인.** 인용 안의 정상 semicolon/BEGIN 문자열은 허용하고 실제 top-level escape는 거절한다. 역할 거부·미등록 column·큰 셀에서 SQL write 0회와 private output leak 부재를 확인한다.

## B2. 신뢰된 backup ID·복원 검증과 복구 계획

**Files:** Create `host_postgres_backup.py`, `tests/test_host_postgres_backup.py`. Existing `host_db_migration.py`와 `test_host_db_restore.py`의 exact legacy 동작을 보존한다.

**Interfaces:**
- `PostgresBackupStore(state_dir: Path, runner)`.
- `create(target: dict, operation_id: str, *, expected_catalog: dict) -> dict`: backup_id, sha256, bytes, catalog_digest, rehearsal_verified 반환.
- `require(backup_id: str, target: dict) -> dict`: owned regular file·내용 hash·대상 identity·검증 receipt 확인.
- `restore_rehearsal(target: dict, backup: dict, operation_id: str) -> dict`와 `restore_target(target: dict, immutable: dict, backup: dict, emit) -> dict`.

- [ ] **Step 1: 실패 테스트 작성.** backup 생성/검증/복원/사후 검사 순서, archive symlink·바이트 변경·잘못된 target/backup ID, rehearsal·cleanup 실패, 013 보호 fingerprint mismatch를 검사한다.

```python
def test_failed_rehearsal_prevents_target_restore(self):
    self.runner.fail_phase("RESTORE_CHECK")
    with self.assertRaises(HostRuntimeError):
        self.backups.create(self.target, self.operation_id,
                            expected_catalog=self.catalog)
    self.assertNotIn("TARGET_RESTORE", self.runner.effects)
```

- [ ] **Step 2: RED 확인.** SDK 인터프리터로 신규 `test_host_postgres_backup.py -v`를 실행한다.
- [ ] **Step 3: backup·rehearsal 구현.** fixed registry container/DB/role/credential reference로 pg_dump custom archive를 개인 상태 영역에 stream한다. 기존 backup cap·private archive 검증을 재사용하며 backend 전용 backup 역할은 SQL 실행 역할과 분리한다. 이름 충돌 없는 `ops_rehearsal_<32hex>` DB에만 복원 검사하고 cleanup 성공까지 검증한다. 이 임시 DB 생성·삭제는 실제 실행 시 승인 계획의 영향에 표시한다.
- [ ] **Step 4: 복구 구현.** target identity·archive SHA·backup receipt·역할·보호 catalog를 execution 직전 재검증한다. 고정 DB에 `pg_restore --single-transaction --exit-on-error --clean --if-exists --no-owner --no-privileges`로 승인한 archive만 복원한다. source/target 013 보호 상태가 다르거나 DB 소유권/연결 조건이 불충족이면 실행 전에 거절한다. 자동 session kill·container restart·다른 DB drop·실패 후 자동 restore를 하지 않는다.
- [ ] **Step 5: GREEN 및 legacy 회귀.** 신규 파일과 `test_host_db_restore.py`를 실행한다. 공개 결과는 backup ID/hash/크기/검증 결과만 포함하고 원본 archive·credential·row는 없음을 검사한다. 실제 DB는 아직 사용하지 않는다.

## B3. Query·DML·정확한 migration batch와 통합

**Files:** Create `host_postgres_actions.py`, `tests/test_host_postgres_actions.py`, `tests/test_postgres_registered_integration.py`. Modify A의 `host_registered_tools.py`, adapter factory, `server.py`, `admin.js`, `project_state.py`, `install-runtime.py`와 관련 테스트.

**Interfaces:** `PostgresActionAdapter(runner, backup_store)`는 A2의 `preflight/execute/postcheck`와 `inspect_target(target) -> dict`를 구현한다. `migration_manifest(target: dict, profile_id: str, context: Path) -> dict`는 정확한 files/order/SHA, prerequisites, protected_catalog, expected_postcheck를 반환한다.

- [ ] **Step 1: 실패 테스트 작성.** readonly query result 제한, DML의 backup 선행·role/schema drift 거부, checksum mismatch·이미 적용됨·부분 배치 rollback, shadow validation failure, 013 포함·rename/다른 profile 우회 거부, target lock 충돌을 검사한다.

```python
def test_006_012_never_runs_013_or_whole_project_runner(self):
    result = self.run_approved_batch("foundation-006-012")
    self.assertEqual(result["applied_migrations"], self.names_006_012)
    self.assertNotIn("013_staged_reading_access.sql", result["applied_migrations"])
    self.assertFalse(self.runner.used_project_migration_script)
```

- [ ] **Step 2: RED 확인.** `rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python chatgpt-mcp/tests/test_host_postgres_actions.py -v`.
- [ ] **Step 3: query·DML 구현.** query는 readonly transaction·5초 lock timeout·30초 statement timeout, server row limit 101과 승인 column whitelist를 사용한다. DML은 검증된 backup/rehearsal 후 5초 lock timeout·60초 statement timeout의 단일 transaction에서 실행한다. SQL 본문은 trusted stdin 전송 경로를 사용하며 임의 `psql -f`·project script·raw SQL tool을 추가하지 않는다. SQLSTATE·실패 artifact 경로·영향 건수만 events에 남긴다.
- [ ] **Step 4: 배치 구현.** `foundation-006-012`는 host 등록 파일/SHA·순서 006..012·현재 001..005 checksum과 보호된 013 상태를 고정한다. 격리 복원본에서 전체 배치를 실행·검증하고 실제 DB에서는 하나의 transaction, 5초 lock timeout·180초 statement timeout, `pg_advisory_xact_lock(hashtext('tarororo_foundation_migrations'))`을 사용한다. 각 SQL 성공 후 같은 transaction에 이력을 기록하고 expected schema/history·013 보호 상태 확인 후 commit한다. mismatch·검증 실패는 rollback이며 이력만 보충하지 않는다. 기존 전체 `db:migrate`를 실행하지 않는다.
- [ ] **Step 5: 도구·웹·설치 통합.** B의 추가 action을 A의 같은 prepare/approve/execute/events 경로에 연결한다. 웹에 SQL 파일/SHA·배치/백업/복원 영향·역할 제한과 보호 범위를 표시한다. legacy 013 계획·승인·실행·테스트는 그대로 통과해야 한다. 신규 모듈을 immutable manifest에 포함한다.
- [ ] **Step 6: GREEN·실DB 격리 검증.** fake 테스트와 전체 SDK 회귀를 통과한다. `test_postgres_registered_integration.py`는 test harness가 생성한 전용 폐기 DB·정확한 role/identity/allowlist receipt를 확인한 경우만 실행한다. fixtures는 001..005 상태를 만들고 006..012 적용·중간 SQL 실패 rollback·backup/restore·013 불변성을 실제로 확인한다. 환경이 없으면 skip 사유를 최종 보고에 남기고 실제 DB 검증 완료라고 주장하지 않는다. 기존 DB URL이나 `tarororo-test`를 받아 실행하는 경로는 거절한다.

## 완료 기준

구현·격리 검증된 B는 A의 웹 승인 계약으로 SQL·migration·backup/restore를 제공한다. 현재 호스트의 generic 역할이나 credentials가 불충족하면 UI는 해당 제한을 표시하며 역할을 자동 생성하지 않는다. 실제 006–012/013 적용과 DB 복구는 여전히 개별 승인 단계다.
