# tarororo-test BACKUP_INVALID · 호스트 진단/배포/006~013 적용 인계 (2026-10-14)

## 목표, 현황, 승인 경계

사용자는 다음 세 작업을 함께 요청했다: (Q1) 호스트 에이전트에서 실패 원인 진단·수정, (Q2) 백업 검증 코드의 실패 유형 세분화, (Q3) 문제가 실제로 해결된 이후 새 계획으로 006~013 적용 및 전체 후검증.

대상: `tossApp / tarororo-test`의 전용 TEST PostgreSQL. 별도 API/Worker 배포, 인증·광고 설정 변경, Docker 재시작은 본 범위가 아니다. 호스트의 불변 런타임 변경은 연결된 다른 Codex/Tunnel 세션을 끊을 수 있으므로 먼저 영향 검토 및 실제 호스트 TTY 승인이 필요하다.

마지막 마이그레이션 작업:
- operation_id: `8d60f5fe8091b87c8a0c0b0949965b40`
- 신규 계획은 `authorization.mode=target` / `ready_to_execute`였으며 개별 웹 승인 없이 실행 요청이 접수됨.
- 최종: `state=outcome_unknown`, `phase=BACKUP_AND_MIGRATE_006_012`, `code=BACKUP_INVALID`, `backup=null`, 종료 2026-10-13T18:37:07Z.
- 실패 직후 읽기 전용 조회: 001~005 이력 이름·SHA 일치, 006~013 모두 미적용, 신규 관계/인덱스/함수 없음, 예상 외 이력 없음. DB 컨테이너 세대 불변, API healthy/ready, Worker/DB running, monetization disabled.
- **실제 백업 파일을 호스트에서 검사한 결과는 아직 없다.** 구 오류만으로 산출물 크기·PGDMP 헤더·pg_restore 목록 오류 중 어느 원인인지 구분 불가. 이 문서는 원인을 확정하거나 재시도가 안전하다고 주장하지 않는다.

## Q2 소스 변경 (완료, 설치되지 않음)

`personal-ops/chatgpt-mcp/host_db_migration.py`:
- `BACKUP_INVALID` 분기 분리:
  - `BACKUP_DUMP_TOO_SMALL`: pg_dump 성공 종료했으나 출력 100B 미만.
  - `BACKUP_HEADER_INVALID`: 보관한 덤프의 최초 5바이트가 `PGDMP`가 아님.
  - `BACKUP_CATALOG_REJECTED`: 검증용 `pg_restore --list` 비정상 종료.
  - `BACKUP_CATALOG_TIMEOUT`: 목록 검증 시간 초과.
  - `BACKUP_CATALOG_EXEC_FAILED`: 목록 검증 프로세스 실행 불가.
  - `BACKUP_ARCHIVE_UNREADABLE`: 백업 파일 open 실패.
  - `BACKUP_DUMP_START_FAILED`, `BACKUP_DUMP_TIMEOUT`, `BACKUP_DUMP_IO_FAILED`: 백업 생성 시작·wait·스트림 실패 세분화.
- 기존 데이터/STDOUT/STDERR를 공개하지 않는 정책 유지, 제한된 공개 오류 코드만 반환.
- `pg_dump` stdout pipe 명시적 close 추가, 기존 DB 백업 → 아카이브 확인 → 격리 DB 복원 → DDL 순서는 변경하지 않음.
- 문서 머리말이 013 전용으로 오래된 것을 006~013 경로까지 반영.

신규 `personal-ops/chatgpt-mcp/tests/test_host_backup_validation.py`:
- 합성 pg_dump 스트리밍, 100B 미만, 비정상 종료, subprocess 시작 실패.
- PGDMP 헤더/파일 open/pg_restore 카탈로그 정상·비정상·타임아웃·기동 실패.
- 데이터·경로·stderr 등 비밀 출력 없는 제한된 오류 코드 검사.
- 신규 **10/10 PASS** (job `45c1984953234764b0f4d9bf982ebdac`, exit0).
- 기존 포함 백업·복원·006~012·006~013 전용 **33/33 PASS** (최신 job `c4ace69499c14fd98594b179ccafa658`, exit0; 이전 31/31도 성공).
- 전체 `test_host_*.py` 202건 일괄 실행(job `5c1451ee9f2d41fba1df2bdfc0606da2`)은 3개 router 테스트가 격리 Python에서 `mcp` 모듈을 못 읽어 **exit1** (errors=3). 전체 PASS 아님. 실제 호스트 SDK 검증/라우터 검사는 별도 필요.
- 모든 테스트는 MCP 격리 샌드박스에서 실행; **호스트 Docker/DB 접근 없음**.

## Q1: 호스트 에이전트의 읽기 전용 실패 진단 (아직 수행 안 됨)

1. 구 operation의 owner-only 백업 아카이브 위치와 실제 불변 릴리스 digest를 호스트에서 확인. 기본 경로: `~/.local/state/chatgpt-coding-mcp/host-runtime/backups/8d60f5fe8091b87c8a0c0b0949965b40.dump` (state root가 별도 설정이면 실제 state root 사용). 디렉터리 symlink/경로·파일 owner·0600을 검사하고 DB 업무 행과 파일 원문은 **로그·채팅으로 전송 금지**.
2. 아카이브 존재 여부, 파일 길이(byte), 선두 5바이트가 PGDMP인지(bool), 소유권/권한, 파일이 정상 regular file인지 확인. 헤더 5바이트 외 내용 출력 금지. `stat`, `head -c 5 | od -An -tx1`의 제한적 정보만 사용 가능.
3. 올바른 custom 아카이브이면 호스트에서 동일한 읽기 전용 검증 `docker exec -i tarororo-test-db-1 pg_restore --list`에 그 파일을 표준입력으로 주고, stdout/stderr는 안전하게 폐기하여 **종료 코드만** 확인. `pg_restore --list`는 DB 변경 명령이 아님. 직접 임의 SQL/복원/마이그레이션 실행 금지.
4. 실패 유형 분류:
   - 파일 부재/크기<100B: pg_dump subprocess 및 파일 기록·fsync·디스크 공간·파일 시스템 권한을 조사.
   - PGDMP 헤더 불일치: 출력 경로 및 Docker stdin/stdout 프레이밍, 덤프 스트림 손상 여부 확인.
   - PGDMP 헤더는 정상이나 catalog nonzero: DB 컨테이너 내 pg_dump/pg_restore 버전·바이너리·stdin 호출, Docker exec exit status 확인. 숨겨진 stderr/비밀을 그대로 채팅에 출력하지 말고 안전한 실패 분류만 남길 것.
   - 정상 archive/catalog: 이전 설치된 실행기 코드/환경·순간적 I/O 오류 비교, **복원 검증이 통과했다는 뜻은 아님**.
5. DB 볼륨·기존 컨테이너·복구용 일회용 DB 잔존 여부를 읽기 전용으로 확인. 작업이 `outcome_unknown`으로 끝났으므로 실제 재실행 전에 가능한 부작용을 재확인한다. 이전 dump는 포렌식용으로 보존하고, 검사 명령에서 `pg_restore -d`를 사용하지 않는다.
6. 원인별 안전한 수정과 테스트를 수행한다. 검증 실패를 무시하거나 `pg_restore --list`를 삭제하여 적용을 강제하지 않는다. 일회용 복원 검사 성공이 필수다.

## 호스트 런타임 롤아웃

- 새로 수정한 파일은 **personal-ops 저장소의 미커밋 source**다. 현재 설치된 불변 HostRuntime에 적용됐다는 증거가 없다.
- 필수: 개인 터널/Codex app-server/관리 웹 활성 세션 영향, 변경된 personal-ops dashboard/observer 등 무관 WIP의 포함 위험, 배포 전후 runtime SHA/manifest, 도구 목록/관리 웹 인증을 검사.
- `rollout-host-runtime.py`의 무변경 preflight 및 안전성 회귀 수행. 호스트 TTY `ROLLOUT` 승인·기존 릴리스 복구 경로 없이 `--apply` 실행 금지. ChatGPT 터널의 `start_command`는 호스트 Docker/홈 접근 없는 격리 명령 실행기이며 우회 사용 금지.
- 설치 후 백업 진단을 실제 호스트에서 확인하고, 공개된 신규 오류코드가 해당 실제 상황에 맞게 분류되는지 검증. 실패가 그대로면 재시도 금지.

## Q3: 원인 해결 및 백업 복원 검증 완료 후만 실행

1. 고정 대상 승인 grant 유효 여부, migration 001~005 checksum match, 006~013 missing, source snapshot/SQL sha, DB postmaster generation, API/Worker/DB identity 및 readiness 재확인.
2. 대상 grant 아래 `prepare_db_migration(tossApp, tarororo-test, fresh request_id)`은 기존 001~005 baseline 상태라면 **`component=schema_update` 006~013 통합 단일 트랜잭션 계획**을 반환하는 코드 경로가 있다. 계획의 `migration_range=006~013`, 포함 SQL, source/DB identity, 백업·일회용 복원·TTL, `authorization.mode=target` 및 `approval_required=false`를 정확히 확인. 실제 결과가 이 계약과 다르면 강제 실행 금지.
3. 사용자가 요청한 적용 범위에 부합하는 승인된 유효 계획에 대해 **`execute_db_migration`만** 사용. 다른 operation_id, 기존 `outcome_unknown` 작업 재사용 금지. `queued`·`running`은 성공 아님; `get_host_operation`으로 `succeeded / COMPLETE`와 backup SHA/size 확인.
4. 반드시 `get_predecessor_migration_status` 및 `get_db_migration_status`로 001~013 **13/13 checksum_match**, 006~012 테이블 14개/인덱스14개/함수5개와 013 테이블 4개 모두 존재, 예상 외 이력 0/충돌 0, 013 `ready`/prerequisites_ok true 검증.
5. `get_runtime_status`로 API healthy/ready, Worker·DB running, DB container generation/볼륨 보존, monetization disabled, API/Worker 불필요한 재시작 없음 확인. 필요한 경우 안전한 카탈로그/기존 사용자·리딩 건수 보존 점검을 별도 읽기 전용 계획에 포함.
6. 실패나 `outcome_unknown` 발생 시 자동 복원·무한 재시도·보호 검사 우회 절대 금지. 남은 위험을 기록하고 다시 호스트 진단.

## ChatGPT 단독 처리 범위

호스트 백업 아카이브·상세 stderr·불변 런타임 설치 프로세스를 읽거나 직접 조작할 수 있는 personal-ops-tunnel MCP 기능이 현재 제공되지 않아 Q1의 호스트 실측과 불변 릴리스 배포는 미완료다. 따라서 Q3 조건(백업 문제 해결)은 성립하지 않았으며 **006~013 DB 변경은 이번 단계에서 실행하지 않았다**.

## 호스트 실측과 안전한 소스 수정 — 2026-10-14

위의 “실측 미완료” 상태를 후속 호스트 세션에서 갱신했다. 기존 덤프와 실패
작업 상태는 보존했다. 이 작업의 실제 component는 `predecessor_migration`이며
006~012 전용이다. 006~013 통합 작업이라고 해석하거나 구 작업을 재실행하지 않는다.

- 덤프: UID 1003 소유 regular file, mode 0600, 134,716 bytes, `PGDMP` 헤더 정상.
  SHA256: `1ba2d965b579a46f6883ac7bbfc5324e66b6094dca2ab0bfa345838303b0d951`.
- 컨테이너의 pg_dump/pg_restore는 모두 PostgreSQL 18.6이다.
- buffered `read(5); seek(0)` 후 stdin을 넘기면 Python 논리 위치는 0이나
  실제 FD 위치는 4096이며 `pg_restore --list`는 exit 1이다.
- 헤더를 읽지 않은 새 파일 또는 unbuffered 파일은 FD 위치 0으로 목록 검증
  exit 0이다. stdout 목록 및 stderr 원문은 채팅/보고서에 공개하지 않았다.
- 원인은 Python BufferedReader의 read-ahead와 subprocess FD 상속 사이 위치
  불일치다. `_validate_archive`의 open을 `buffering=0`으로 바꿨다. 보호 검사나
  백업/복원 순서는 제거하지 않았다.
- 실제 subprocess가 첫 5바이트를 읽는 회귀를 RED→GREEN으로 검증했고,
  백업·복원·선행·통합 경로 34개가 통과했다. 테스트의 복원은 모의 DB다.
- 수정된 함수로 실제 보관 덤프의 읽기 전용 목록 검증도 통과했고 덤프 SHA는
  동일하다. read-only pg_database 조회는 테스트 DB 1개, 임시 복원 DB 0개를 확인했다.
- 실제 MCP는 001~005 checksum_match, 006~013 missing, 선행 테이블/인덱스/함수
  전부 없음, ready_to_prepare=true를 반환했다. DB 세대는 기존과 같다.

수정은 현재 분리 작업본
`/home/jhw/.local/state/personal-ops-worktrees/agent-delegation-20261014-d9wkbyov`에만
있으며 실행 중인 불변 runtime에는 아직 적용하지 않았다. 실제 DB 복원 리허설,
마이그레이션, Docker 재시작/배포는 하지 않았다. catalog exit 0은 복원 검증 통과와
다르므로 실제 적용은 수정 runtime 설치 승인과 새 실행 계획 승인 이후다.

기계 로컬 증거(0700 directory / regular 0600 files):
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/delegation-backup-review-e6k3459i`의
`backup-diagnosis.json`, `backup-root-cause.json`, `backup-fix-live-readonly.json`.

## 최종 검증과 ChatGPT 후속 작업

백업 수정 포함 후보 `adc2d2a1093abbe2862b`의 무변경 preflight는 exit 0,
`preflight_ok`다. 필수 389개와 전체 539개 검사, 변경 Python 11개 Ruff가 통과했다.
현재 live는 `d944e6138731d4b031b7`이며 후보 설치·서비스 전환·DB 변경은 하지 않았다.
첫 preflight의 선택 모듈 호환성 실패와 후속 수정/통과 이력은 companion agent
handoff 및 `rollout-ol0wwxmj`/`rollout-ms53kzbn` 원본 영수증에 보존했다.

에이전트 C1-C3는 동작 완료 상태가 아니다. 신규 도구 5개의 실제 SDK/stdio 등록과
조회·거부 경로는 검증했지만 live 등록은 서비스 전환 후 확인한다. helper 제공과
프로세스 감독/host broker가 부족해 실제 에이전트 및 자동 원본 적용은 비활성이다.
이 백업 오류는 호스트 CLI에서 직접 진단·수정했으며 에이전트를 호출했다고 하지 않는다.

ChatGPT 재개 순서:

1. 이 문서와 agent handoff의 최신 절을 읽고 후보 runtime 설치 승인 여부 및
   실제 실행 snapshot을 확인한다. 소스만 고쳐졌다면 DB 적용을 재시도하지 않는다.
2. 승인된 runtime 전환 이후에도 구 `outcome_unknown` operation은 재사용하지 않는다.
3. 실제 DB 복원 리허설/마이그레이션은 사용자에게 실행 승인을 받은 다음 새
   request ID의 `prepare_db_migration`으로 현재 baseline에 맞는 006~013 통합
   계획을 확인한다. 기존 대상 grant와 SQL/컨테이너/DB 세대/만료 검사를 유지한다.
4. `execute_db_migration` 실행은 보관 백업 검증→일회용 복원·정리→단일 DDL
   트랜잭션 순서를 거친다. 이번 catalog 성공은 실제 복원 성공을 보장하지 않는다.
5. 성공 상태와 실제 001~013 checksum_match, 선행 객체 및 013 표, DB 세대/
   컨테이너/readiness를 후검증하고 같은 문서에 기록한다. 실패 시 자동 재시도/복원은 없다.


## 승인된 런타임 전환 결과 — 2026-10-14

관측 시각: 2026-10-14T11:09:00.644512+09:00. 이 절은 앞의 “후보 미설치/미전환” 상태를 갱신한다.
사용자가 후보 설치 및 터널·관리웹 전환을 명시적으로 승인했고, 그 범위만 적용했다.

- 실제 live snapshot: `adc2d2a1093abbe2862b`; 이전 `d944e6138731d4b031b7` release 보존.
  설치 manifest 48개 파일의 SHA256과 실제 MCP PID 117524 /
  tunnel PID 117496의 실행 경로·세대를 확인했다.
- 배포 직전 필수 389개 / 전체 539개 테스트 모두 exit 0. 승인한 source/control
  digest 일치 및 queued/running 작업 0개를 재확인한 뒤 전환했다.
- 원본 receipt: `rollouts/rollout-wnjdx1kh/receipt.json`.
  `installed=true`, `switched=true`, `runtime_verified=true`, `admin_status=admin_ready`.
  명령 exit 2 / `verification_required`는 서비스 전환 실패가 아니다.
  `catalog_status=awaiting_discovery`, `connector_schemas_verified=false`이며
  ChatGPT 연결의 도구 목록 새로고침·후속 검증이 남았음을 그대로 보존한다.
- 설치된 immutable runtime을 실제 MCP SDK/stdio로 호출했다. 신뢰 검사 우회나
  모의 profile 없이 실제 host config와 별도 일회용 XDG state를 사용했다.
  전체 49개 도구 중 신규 5개와 기존 필수 host 도구들이 등록되어 있고,
  `list_agent_profiles(tossApp)`는 `profiles=[]`였다. 미등록 프로필 계획은
  PROFILE_NOT_FOUND, 모델이 넣은 추가 승인 인자는 오류로 거부했다.
  실제 live main 채널의 신규 도구 discovery와 ChatGPT schema 캐시는 아직 미검증이다.
- 관리웹 PID 117844도 같은 release다. Loopback / LAN / Tailnet HTTPS
  페이지 HTTP 200, 신규 agent-admin.js SHA 일치, 에이전트 승인 영역 존재를 확인했다.
  인증 없는 agent API는 401, 정상 관리자 인증 조회는 200 / 작업 0개다.
  관리웹이 표시되는 사실은 실제 에이전트 실행 준비 완료를 뜻하지 않는다.
- 실제 ChatGPT 커넥터의 기존 조회 도구는 재연결되어 API Running/Healthy/Ready,
  Worker와 PostgreSQL Running, monetization disabled를 반환했다.
  001~005 checksum_match / 006~013 missing, ready_to_prepare=true, complete=false.
  DB postmaster epoch `1791788137.513100`와 Docker
  3개의 ID/image/started_at/state/health는 전환 전과 동일하다.
- 설치된 backup validator로 기존 134,716-byte 덤프의 읽기 전용 catalog 검증 PASS,
  SHA256 동일. 실제 disposable restore, DB migration, Docker restart/deploy,
  유료 모델 호출은 수행하지 않았다. 실패 작업 `8d60f5fe8091b87c8a0c0b0949965b40`의
  outcome_unknown / BACKUP_INVALID 상태를 보존했고 재사용하지 않았다.
- host registry envelope / runtime key / admin token / 기존 target grant의 bytes
  fingerprint 동일. source/control WIP 및 Git HEAD cf8b6d2도 유지했다. 커밋 없음.
- 전환 전 외부 coding process 15개 중 11개는 같은 PID/start-ticks로 유지됐다.
  독립 bun supervisor 2134546(session-34.scope)의 Claude 자식 4개는 구간 중
  종료된 것으로 관측됐다. 종료 원인은 수집하지 않았으므로 모두 보존됐다고
  주장하지 않는다. 이 작업은 두 승인 서비스 외 PID에 signal을 보내지 않았고,
  현재 호스트 CLI는 별도 session-35.scope에서 계속 동작한다.
- 프로필 0개, helper/supervision 미완료, broker deny-only, CAS 비활성은 유지한다.
  신규 도구 설치를 C1-C3 실제 에이전트 활성화 완료로 보고하지 않는다.

추가 증거는 기존 private evidence directory의 `activation-before.json`,
`activation-terminal.log`, `activation-verification.json`,
`activation-connector-readonly.json`, `activation-final-audit.json`에 있다.
모든 파일은 current-user 소유 regular 0600이며, receipt 원문은 수정하지 않았다.

ChatGPT DB 작업 인계: 백업 수정은 이제 실제 운영 runtime에 반영되어 있다.
구 실패 작업을 재실행하지 않는다. 우선 현재 상태 조회와 새로운 request ID의
`prepare_db_migration`으로 006~013 계획을 만들 수 있다. 실제 실행은 사용자의
별도 DB 실행 승인과 유효한 기존 대상 권한·source pin·DB 세대 검증 이후다.
`execute_db_migration`의 backup → 일회용 실제 restore·cleanup → 단일 DDL
transaction 보호를 유지하고, 성공 후 실제 001~013 및 객체·readiness를 확인한다.
이번 승인에는 복원·마이그레이션·API/Worker 배포가 포함되지 않았다.
