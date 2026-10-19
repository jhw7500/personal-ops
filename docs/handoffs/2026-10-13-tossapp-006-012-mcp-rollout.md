# tossApp 006~012 MCP HostRuntime 갱신 인계 — 2026-10-13

## 작업 목표와 승인 범위

사용자는 기존 채팅에서 **MCP 불변 런타임 설치 및 기존 개인 터널·관리 웹 갱신만** 다음 단계로 진행하도록 했다.
기존 작업 레포는 `personal-ops`이며 대상 서비스는 `personal-ops-coding` 및 대응 admin UI다.
필수 목적은 새 **읽기 전용** `get_predecessor_migration_status` 및 별도 계획/실행 MCP 도구를
등록하는 것이지 DB를 바꾸는 것이 아니다.

**이번 범위에서 금지:** `tarororo-test` 실제 DB 백업/DDL/마이그레이션,
`006~012` 실제 적용, Schema `013` 적용, Docker build/up/restart, API/Worker 재시작,
광고·결제 테스트 모드 변경, Toss 업로드, 무차별 Git stage/commit/reset,
기존 다른 세션의 원격 에이전트/CodeX CLI 세션 종료·resume.

주의: 현 롤아웃 코드 자체는 `personal-ops-tunnel.service`의 정지/시작 및
`personal-ops-admin.service` 재시작을 수행할 수 있고, 이 과정에서 해당 터널
종속 `codex app-server` 연결이 종료될 수 있다. 이 영향 범위를 작업 전 명확히
검증하고 사용자의 승인 범위를 넘는 활성 세션 중단이 예상되면 **진행을 중단**한다.

## 이미 완료된 소스 변경

- `personal-ops/chatgpt-mcp/host_runtime.py`: `001~013` 이력·SHA·신규 객체의
  읽기 전용 진단(업무 행 조회 없음).
- `host_runtime_operations.py`, `host_db_migration.py`: source-pinned
  `006~012` 별도 준비·작업별 승인·실행 경로. 원자적 DDL 전 백업/격리 복원 점검.
- `host_runtime_config.py` 및 `host_runtime_web.py`: 기존 v2
  Worker/013 범위를 재사용하지 않고 독립 v3 `ENABLE DB 006-012` 승인 조건 추가.
  권한 활성화도 이번 갱신 작업에서 **수행하지 않는다**.
- `admin_server.py`, `admin.js`, `admin.html`: v3 권한 별도 승인 UI 및
  작업 계획의 DB 세대·파일 SHA 표시. 실제 작업마다 별도 호스트 승인.
- `rollout-host-runtime.py`: **기존 7개 대신 11개 정확한 MCP 도구**의
  존재를 검증하도록 갱신했으며 `tests.test_host_predecessor_migration`을 필수
  체크 목록에 추가했다. `test_rollout_host_runtime.py`와
  `test_rollout_catalog.py`에도 회귀 테스트 추가.
- `tossApp/server/src/db/migrate.ts`, `migrate-predecessors.ts`: 006~012
  전용 Runner; 일반 `npm run db:migrate`는 여전히 013도 포함하므로 **실행 금지**.

검증 근거:
- 개인 호스트 롤아웃·승인·DB 모의 회귀 테스트 **157/157 통과**, exit 0,
  `personal-ops` 격리 job `83ef2b0f60f94b8a868d2e517d12c9bd`.
- 최신 편집본의 후보 스냅샷 digest는 **`3270e343658cf0991aa5`**,
  런타임 파일 33개, 롤아웃 제어 입력 36개다
  (격리 파일 읽기 전용 작업 `49c559d11b5248d78620b857ad5fe5f3`, exit 0).
  **후보일 뿐 실제 설치 증거가 아니다.** 다른 세션의 파일 수정이
  발생하면 digest가 달라져야 하므로 실행 직전 재계산해서 비교한다.
- 기초 호스트 검사 61/61, tossApp 격리 PostgreSQL 113/113, 서버 204/204,
  타입 검사 PASS는 이전 작업 기록이다. 현 턴에서 DB 통합은 재실행하지 않았다.
- 실제 MCP stdio 핸드셰이크/라우터 통합은 격리 Python에 `mcp` 패키지가 없어
  호스트 런타임에서 확인해야 한다.

## 실행 전 필수 preflight — 호스트 에이전트가 수행

1. `personal-ops`와 `tossApp` 현재 Git status/수정 파일/미추적 목록을
   재조회하고 다른 세션의 WIP를 보존한다.
   `personal-ops`는 원격 main 대비 ahead 2/behind 12였고 관리 UI/
   Codex observer/dashboard 파일 변경도 존재한다. **모든 WIP를 이번
   HostRuntime에 무검토로 포함하지 말 것.**
2. 현재 실행 중인 터널/관리 웹의 불변 릴리스, 서비스 정의,
   터널 ID·런타임 키 파일의 **존재/권한(값 노출 금지)** 및 서비스 상태,
   활성 `codex app-server` 세션 영향을 점검한다.
3. 현재 설치된 릴리스와 변경 예정 파일의 digest/diff를 검토한다.
   `install-runtime.py`는 HostRuntime 외에 대시보드와 Codex/Claude
   관측 파일도 함께 스냅샷에 포함한다. 무관한 WIP를 무심코 롤아웃하지
   않도록 전체 파일 목록과 필요 변경을 명시한다.
4. 기존 `rollout-host-runtime.py`의 preflight 모드(**--apply 없음**)를
   정상 설치된 호스트 Python/도구 체계로 실행해 불변 소스 inventory,
   테스트, 호스트 서비스/프로세스 기대 구조 및 이전 스냅샷을 확인한다.
   사전 점검도 민감한 값을 채팅에 출력하지 않는다.
5. 그 결과와 영향/이전 릴리스/예상 11개 도구를 호스트 사용자에게 간결히 보고한다.

## 실제 설치/전환 — 인간 승인과 보호 조건

- ChatGPT 터널 `start_command`는 격리용이며 호스트 런타임 홈/자격증명에
  접근할 수 없다. **그 경로로 무리하게 우회 실행하지 않는다.**
- 호스트 작업 세션에 정상적인 TTY가 있고 출처·테스트·영향 검토가 완료된 경우만,
  기존 `rollout-host-runtime.py --apply`의 승인 흐름을 사용한다.
  스크립트는 `ROLLOUT`을 **TTY에서 직접 입력**받도록 되어 있다.
  에이전트가 승인 문구를 파이프로 주입하거나, 승인 영수증을 직접 만들거나,
  검사 실패를 우회해서는 안 된다.
- 새 불변 릴리스 설치와 터널/관리 웹 전환은 한정된 승인 범위 안에서만 진행한다.
  기존 터널 ID/키 참조/관리 포트/인증을 보존한다.
  변경 전 릴리스·서비스 구성을 안전하게 보관한다.
- 실패 시 `succeeded`나 `ready`로 기록하지 않고, 이전 프로세스
  세대 및 스냅샷과 현재 상태를 확인한 뒤 안전 복구를 진행한다.
  불확실한 상태의 자동 재시도/무제한 재시작을 금지한다.

## 검증 및 완료 기준

- 터널 프로세스 `process_running=true`, `healthy=true`,
  `ready=true`; 실제 MCP child의 릴리스 digest가 새 설치 snapshot과 일치.
- admin UI가 새 릴리스로 서비스되고 로컬/Tailnet 연결, read-only LAN 제약을 유지.
- MCP catalog에서 다음 **11개**가 존재: `get_runtime_status`,
  `get_db_migration_status`, `get_predecessor_migration_status`,
  `compare_deployment`, `prepare_test_deploy`, `prepare_db_migration`,
  `prepare_predecessor_migration`, `execute_test_deploy`,
  `execute_predecessor_migration`, `execute_db_migration`,
  `get_host_operation`.
- `tools/list` 완전성이 passive health로 확인되지 않으면
  `verification_required`로 기록. ChatGPT의 기존 플러그인을 Refresh하고
  새 대화에서 **읽기 전용** `get_predecessor_migration_status`를 호출해
  운영용 DB의 각 이력과 객체 존재만 검사한다.
- 마무리 때 `get_runtime_status`, `get_db_migration_status`를 조회해
  API/Worker/DB가 이전처럼 running이고 013 미적용, 광고/결제 disabled,
  기존 DB 컨테이너 세대가 변경되지 않았는지 확인한다.
  이 단계에서도 **013이나 006~012 실제 적용을 실행하지 않는다**.

## 확인되지 않은/다음 작업

- tossApp의 SQL 006~012 및 Docker 배포 고위험 파일은 현재 미커밋/미추적
  상태가 남아 있어 `prepare_predecessor_migration`이
  `REVIEW_REQUIRED`를 반환하는 것이 정상이다.
  이를 우회하기 위해 SQL checksum 또는 Git 검증을 바꾸지 않는다.
- 이후 `006~012`만의 소스 검토/커밋과 실제 DB 백업·적용은
  **새로운 작업별 명시 승인**을 요청한다. 013은 그 후 별도 단계.
- 설치 결과(릴리스 SHA, 검사 종료 코드, 실제 도구 목록과 실패 원인,
  호스트 서비스 상태)를 `tossApp/docs/handoffs/tarororo-test-schema-preflight.md`
  또는 프로젝트 진행 기록에 반영한다.

## 직접 실행 불가 경계

이 인계 문서는 **작업 실행 완료 증거가 아니다**. 현재 ChatGPT 대화의
터널은 파일/격리 명령/승인형 DB 013 도구만 제공한다.
호스트 운영 스냅샷 설치/서비스 전환을 실행하는 MCP 도구는 없다.
따라서 호스트 TTY 필요 단계는 실제 승인 가능한 호스트 작업 세션이 처리해야 한다.
