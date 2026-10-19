# Schema014 전용 MCP 마이그레이션 기능 — 소스 구현 및 안전 게이트 인계

작성: 2026-10-15 KST
대상: `personal-ops/chatgpt-mcp` / `tossApp` → `tarororo-test`
상태: **검증 중인 소스 모듈 작성. 공개 MCP 실행 도구 연결/설치/공유 DB 적용 전혀 완료되지 않음.**

## 사용자 요청
직전 UX 개선(앱 내 이전/앞으로 버튼, 관계 직접입력과 상황 설명 별도 저장)이 코드와 격리 테스트를 통과했다. 실제 테스트 앱 반영을 위해 Schema014 → API-only → exact AIT 순서로 진행 요청. 기존 MCP에는 013 이하 전용 도구만 존재하고 임의 host command로 대체하면 DB/운영 권한을 우회하므로 **별도 typed + 승인된 Schema014 도구**가 필요하다. Codex/Claude agent는 호출하지 않는다.

## 현재 소스에 작성된 내용
- 신규 `chatgpt-mcp/host_schema014.py` (현재 **미등록**):
  - `FIXED_NAME = '014_person_relationship_detail.sql'`
  - pinned allowlisted source SHA256 `a7b470a05007543be8751aec6d2ea8cd88891b08aa736bc72112b7417d56dac2`.
  - `schema014_status(runtime, project, target)`: 001~014 fixed migration checksum, 예상 밖 history, 013 tables, 014 relationship_detail column/constraint, `users`/`persons`/`readings`의 **집계 건수만** read-only SQL로 확인. 데이터 행이나 토큰/민감값은 반환하지 않음. 001~013 정확·014 미설치·컬럼/제약 부재 상태에서만 `ready_to_prepare` 허용.
  - `migrate_schema014`: operation/pinned identities/생성 시점/SQL checksum 확인 → PGDMP streaming backup(기존 코어 함수 호출) → archive validation → disposable DB restore(기존 함수) + 13/13 체크섬 확인 → DB 세대·데이터 건수 재검증 → advisory-lock + lock_timeout + statement_timeout + generation/history/object-collision guard를 포함한 **한 개의 원자적 SQL 트랜잭션** → 새 history record 저장 → no service restart + 사후 검증. 자동 restore 또는 재시도 없음.
- `chatgpt-mcp/host_db_migration.py`:
  `_restore_check`의 유효 `expected_history`를 `(5, 12, 13)`으로 확장. 기존 005/012 restore 경로는 보존.
- 신규 `chatgpt-mcp/tests/test_host_schema014.py`: 가짜 Docker/DB 응답·합성 백업으로 read-only 검사, 고정 SHA, 014 컬럼 충돌, checksum mismatch, stale plan, backup→restore 순서, 실패 시 SQL 0회, DB generation/count drift 차단을 검사. **실제 Docker·DB 호출 없음.**

## 이 턴에 실행한 검사와 제한
- 격리 검사 첫 시도 `personalops-schema014-mock-tests-20261015` / job `02279195f933463abeaecfe2419f17d0`:
  `py_compile` 통과, 014 테스트 7개 중 6개 통과/1개 실패.
  실패 원인은 실행 SQL의 `attisdropped`를 테스트 assertion의 `'DROP' not in sql.upper()`가 실제 `DROP` 명령으로 오인한 **테스트 검사의 오탐**. SQL은 `DROP COLUMN`/ `DROP TABLE` 명령을 포함하지 않는다.
- 신규 테스트 assertion을 `'DROP TABLE' not ... and 'DROP COLUMN' not ...`으로 수정했고, `read_file`로 실제 저장을 확인했다.
- **수정 후 재실행 명령은 OpenAI 보안 검사에서 차단**됐으므로 7/7 테스트 통과, 기존 백업 테스트 통과를 주장하지 않는다.
- `host_runtime_tools.py`에 read-only 상태 도구를 잠시 추가했지만, 함께 필요한 공개 승인·실행 MCP 도구 등록 변경이 보안 검사에 의해 차단됐다. **불완전한 도구 목록을 남기지 않도록 상태 도구 항목도 원복**했다. 현재 tool spec/router 목록은 기존 상태이며 `prepare_schema014_migration`, `execute_schema014_migration`, `get_schema014_migration_status` 공개 연결이 안 되어 있다.
- 보안 검사 차단된 동일 실행 경로를 대체 명령·에이전트로 우회하지 않았다.
- `install-runtime.py` manifest FILES, `host_runtime_operations.py` / `host_runtime_web.py` 승인 계획 타입, `server.py` 라우팅, 실제 MCP 설치/롤아웃/host restart **모두 수정하지 않음**. 따라서 기존 시스템은 이 새 모듈을 실제 실행하지 않는다.

## 안전한 다음 구현 경계 (권한 있는 리뷰/승인 필요)
1. 운영자 검토: allowlisted 014 SQL과 source SHA를 재확인하고 새 모듈의 SQL 생성, DB/status 검증·백업 예외 경로를 리뷰. 증거 없는 재실행 성공 주장 금지.
2. 기존 001~013 target grant가 **014를 자동 승인하지 않게** 별도 `schema_014` 컴포넌트/새 관리 웹에서 명시적인 1회 계획승인을 요구하는 타입으로만 MCP 등록.
3. `host_runtime_operations.py` 상태 타입/소스 frozen inventory/DB generation/one-use receipt 및 backup metadata의 신규 schema014 경로 통합. `server.py` 공개 get/prepare/execute 라우트는 role/approval 제한과 무인계 실서비스 DB 변경 거부를 확인한 후만.
4. 백업 restore `expected_history=13` 과 정상·충돌·실패 시나리오, auth rejection, 014 후 기존 조회 도구와의 status compatibility를 fake DB 단위/Router MCP 세션 테스트로 재검증.
5. `install-runtime.py`와 검증 manifest에 추가 모듈 등록, owner-reviewed release immutable snapshot 검증 후 사용자 승인된 롤아웃. **설치 중 연결 끊김/다른 세션 영향 주의**. 소스 repo는 앞서 main ahead2/behind12 + 대량 dirty/WIP였으므로 reset/stash/commit/force push 금지.
6. 실제 `tossApp` TEST DB는 013까지 적용·API Healthy/toss-demo·Worker/DB running. 사용자 명시 승인을 포함한 source-pinned DB014 계획과 dry-run restore 결과가 나오기 전에는 신규 API 이미지를 배포하면 안 됨.
7. DB014만 정상 적용·검증 후 API-only toss-demo deploy, 마지막으로 기존 exact AIT 후보 `build/android-test/gone-ux-navigation-relationship014-20261015/tarororo.ait` SHA `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f` 업로드. 신규 앱 번들은 현재 Toss 콘솔에 미업로드 상태.

## 사용자에게 정확히 보고할 상태
- 완료: Schema014 코드의 독립된 안전 검증/백업-first prototype, 신규 unit tests, `_restore_check` 허용 이력 13 추가, 첫 검사 6/7 및 테스트 오탐 수정.
- 미완료: 수정 후 unit 재실행, 공개 MCP 서명/승인/실행/설치/롤아웃, DB014 실제 적용, API-only 배포, AIT TEST 업로드.
- 이유: 공개 실행 도구 등록과 테스트 재실행 요청이 OpenAI 보안 검사에서 차단됨. 이는 쉘 우회·권한 요청으로 처리하지 않는다.

## 추가 소스 보안 검토 (2026-10-15, 후속 요청)
- MCP 공개 도구 목록을 재조회했으나 Schema014는 아직 등록되어 있지 않음. 기존 013 마이그레이션의 대상 승인/실행을 014에 재활용하지 않는다.
- `chatgpt-mcp/host_schema014.py`: SQL 파일의 내용 해시 외에도 `server/` 또는 `server/migrations/` 상위 디렉터리가 symlink인 경우를 차단하도록 `_migration_path()` 경로 검사를 추가. `checked_source()` 및 `_expected_checksums()`는 동일 검사를 거쳐 로드한다.
- `chatgpt-mcp/tests/test_host_schema014.py`: `test_rejects_symlinked_migration_directory_before_reading_any_sql` 회귀 테스트 소스를 추가. 모든 fake I/O가 비호스트 환경에서 수행되도록 설계했으나 **현재 턴에서 실행하지 못했으므로 PASS가 아님**.
- 실서비스 중 `users/persons/readings` 개수가 달라지면, 현재 prototype의 backup 전후 `counts` 완전일치 요구로 `SCHEMA_CHANGED` 또는 실제 COMMIT 후 `MIGRATION_POSTCHECK_FAILED`가 발생할 수 있음. 이는 안전한 실패 우선 설계지만 정상 유저 활동도 실패로 오판할 위험이 있으므로 승인 전 **쓰기 차단·작업 중 사용자 변경 허용 정책 및 사후 상태 조회/재시도 금지**를 검토해야 함.
- 동일하게 postcheck는 제약의 존재/검증 여부만 확인하고 제약 표현의 의미 자체를 재해시하지 않음. 고정 SQL 해시와 기존 객체 충돌 검사로 예방하지만 배포 전 code-review에서 추가 대조 필요.
- 현재 공개 schema 도구가 전혀 새로 등록되지 않아 설치된 runtime과 서비스 상태에는 영향 없음. 이전 안전 검사에 막힌 MCP 실행 API 등록이나 테스트 명령을 다른 shell/agent/작업 경로로 우회하지 않음.

## 2026-10-15 추가 점검 — 커밋 이후 데이터 변동에 따른 오검출 방지
- `personal-ops` 소스 및 공개 MCP catalog를 재조회: `get_schema014_migration_status`/`prepare_schema014_migration`/`execute_schema014_migration`는 **여전히 등록되지 않음**. `get_host_access` 상태도 active=false, reason=RUNTIME_CHANGED. 현재 tarororo-test PostgreSQL은 여전히 013 ready이며 API/Worker와 toss-demo 모드 정상.
- `host_schema014.migrate_schema014`이 `COMMIT` 이후에도 `final['counts'] == before_status['counts']`를 필수 성공 조건으로 사용하면, 정상적인 인연/리딩 동시 입력 때문에 이미 커밋된 DB 작업을 실패로 보고할 수 있다. SQL 014는 데이터 테이블의 레코드를 삭제/수정하지 않는 고정 DDL이며, 잠금·schema history·generation postcheck가 실제 완료 판정 기준이다.
- 따라서 **백업 복원 전 데이터 건수 변동 검사는 유지**하되(사전 불일치 시 SQL 적용 중단), 커밋 후에는 확인된 DB generation·011~014 schema status·컨테이너 세대를 성공 판정으로 사용하고 `postcommit_counts_changed`를 반환하는 것으로 소스를 변경. 결과 필드는 타 운영 매니저에 아직 연결되지 않았고 실제 DB 작업 역시 없음.
- `tests/test_host_schema014.py` fake runtime에 `postcommit_counts_change` 설정 및 `test_postcommit_user_activity_does_not_misreport_a_committed_migration` 회귀 사례 추가. 수정 전/후 차이를 재현하도록 구성했으나 **OpenAI 보안 검사로 차단됐던 실행 재시도는 하지 않았으므로 미실행**. 새로운 014 코드 등록·설치·롤아웃 전에 정상/실패 케이스와 함께 검증이 필요.
- 운영자 검토 시 또 다른 이력 호환성 주의: 기존 `host_runtime.predecessor_status`는 001~013만 유효 이력으로 취급하므로 014가 실제로 적용되면 `unexpected_history_count=1` 및 `complete=false`로 기록할 수 있다. 이는 014 적용 실패의 증거가 아니라 *기존 진단기의 지원 범위 한계*이므로 013 이전 단계의 안정성을 해치지 않는 별도 014-aware read-only 상태 도구로 판별해야 한다.
