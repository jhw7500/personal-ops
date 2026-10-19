# personal-ops 장기 세션 리뷰와 Notion 대조

- 날짜: 2026-10-13 KST
- 요청: @jhw-review --match
- 상태: 사용자 OK 승인 후 Notion 신규 3건·기존 보강 5건 저장 완료, 중복 1건 제외. 8개 페이지의 전체 본문을 다시 읽어 저장 내용을 확인했다.
- report: etc. 이 세션의 기존 사용자 지정 분류를 유지한다.

## 기존에 저장된 앞부분

초기 Secure MCP Tunnel/11개 로컬 레포/선택 UI, 격리 실행·Git 승인·Node/Chromium/임시 PostgreSQL, tossApp 개발 서버·LAN·토스 테스트 배포·본인 초대, 실행 기록/작업 보고 구분, RPG 작업실·고정 객체·외부 보고, 폰/CLI 공유·재부팅 복구를 기존 Notion 본문에서 확인했다. 이 내용을 새 성과로 복제하지 않는다.

기존 확인 자료: [personal-ops](https://app.notion.com/p/personal-ops-34a8a230a04e81a6a3b6c9cac75e2511), [tossApp](https://app.notion.com/p/tossApp-3d88a230a04e81acb8a6cf6954fed140), [원격 공유](https://app.notion.com/p/Codex-3f38a230a04e81e1ba80fecd7a5cfb6b), [터널 자동 시작](https://app.notion.com/p/personal-ops-tunnel-user-systemd-3f78a230a04e81e2bd3df010535b32f5), [일반 Chat 선호](https://app.notion.com/p/Chat-3f38a230a04e816c9946fa32b935af33).

## 이번 후반의 완료와 미완료

완료: 롤아웃 실제 실행본 불일치 진단·수정·승인된 활성화, 터미널/로그 진단성 개선, admin cache/목록 GET 수정, 실제 connector 7개 도구·읽기 전용 DB 진단·관리 웹 검증, DB 선행 상태 분류와 인계, 범용 작업/CLI 위임 설계와 구현 계획 작성.

구현 지시 상태: 사용자가 직접 Claude에게 구현 작업을 지시했다고 확인했다. 이 Codex 세션에서는 중복 구현이나 추가 CLI 모델 호출을 시작하지 않는다.

결과 미검증/승인 대기: 범용 registry/Compose·DB adapter 및 agent task 호출 구현 결과, 에이전트 실제 파일 격리와 유료/계정 모델 smoke, 신규 runtime·version 3 설정 적용, 006–012 실제 적용, 별도 013 적용. Claude의 실제 구현·테스트·운영 반영 결과는 아직 검증하지 않았다.

미검증 유지: 수정 뒤 두 번째 재부팅, WSL 비교 실험, 최종 tossApp-deploy test의 실서비스 전체 재실행·폰 초대 수신/실행·공개 출시, 모든 외부 MCP/내부 추론 및 CLI consumer의 실제 관측. 앞선 Notion 기록의 검증 경계를 이번 리뷰로 확대하지 않는다.

후속 변경은 다른 세션의 WIP·기존 컨테이너/DB를 보존하고 정확한 소스·입력·대상·검증·승인 범위로 수행한다. Notion 저장은 host rollout/DB 적용/모델 호출/Task·Issue·PR mutation 승인이 아니다.

## 승인된 Notion 저장 내역
## [1] personal-ops-tunnel 런타임 롤아웃·신규 도구·관리 웹 복구 완료

- DB: projects
- report: etc
- 가치: 상
- 대조: AUGMENT
- 근거: 기존 구축·작업실 기록에 실제 런타임 복구와 최종 검증 결과를 추가한다.
- 대상/참고: https://app.notion.com/p/personal-ops-34a8a230a04e81a6a3b6c9cac75e2511
- 본문 paragraph 최대: 331자

## 2026-10-13 런타임 복구 결과

이 세션의 09:25 KST 최종 운영 검증에서 actual MCP는 c81522954a091864c42d, admin은 1dfc5fcaf2cc707701be였다. 실제 MCP PID의 argv·시작 tick·manifest, 터널 profile·systemd startup 명령의 정렬을 확인했다. 이 값은 당시 관측이며 Notion 리뷰에서 새로 서비스를 재시작하거나 운영 검증을 반복한 결과가 아니다.

진단 시 actual MCP, 터널 JSON profile, systemd startup 명령이 서로 다른 스냅샷을 가리켰다. 기존 healthy/ready 검사만으로는 새 코드 활성화를 증명하지 못했다. 소스·설정·프로세스 세대를 고정하고 실제 실행본을 검증하도록 롤아웃을 수정했으며, 다른 세션의 변경·공유 Codex·observer를 보존했다.

## 적용한 코드와 승인된 운영 변경

rollout_tunnel.py, rollout_admin.py, rollout_diagnostics.py, rollout_catalog.py와 rollout-host-runtime.py를 보강했다. admin의 cached DropInPaths 검증 순서를 수정했고 authenticated GET /api/host-operations 누락을 해결했다. 사용자 승인 후 immutable runtime 설치·기존 터널 stop/start·관리 서비스 전환과 v2 범위 활성화를 수행했다. Docker build/up/restart, DB 쓰기·복원, Schema 013 적용은 하지 않았다.

## 검증 증거

최종 변경 대상 검사 144/144, 전체 검사 280/280, Ruff와 diff check가 통과했고 독립 코드/아키텍처 리뷰를 통과했다. 실제 connector에서 호스트 도구 7/7 및 Worker component schema를 확인하고 get_db_migration_status를 읽기 전용으로 호출했다. Chromium에서 loopback/LAN/Tailscale HTTPS 관리 화면·목록 API·정적 자산 hash·page error 0을 확인했다. 비인증 조회 401, 다른 프로젝트 404, LAN HTTP 승인 및 foreign Origin 승인 403을 확인했다.

## 보존한 경계와 현재 다음 단계

당시 API/Worker/PostgreSQL의 container ID·image·시작 시각과 DB migration metadata가 baseline과 같았다. 013은 missing이며 앞선 checksum 일치가 5/12였다. 수집기 소스를 기존 기능으로 보존했고, 이 Codex 세션에서는 새 CLI 에이전트 호출 도구와 범용 Docker/DB registry를 구현하지 않았다.

범용 작업·승인형 CLI 위임 설계는 사용자 확인을 받았고 구현 계획을 작성했다. 사용자가 직접 Claude에게 구현 작업을 지시했다고 확인했다. Claude의 실제 구현·테스트·운영 반영 결과는 아직 검증하지 않았다. 이 Codex 세션에서는 중복 구현이나 추가 CLI 모델 호출을 시작하지 않는다. 이번 복구 및 후속 설계 변경을 새로 commit/push/PR하지 않았으며 다른 세션의 기존 변경을 보존한다.

## [2] tossApp tarororo-test 선행 migration 읽기 전용 진단·인계

- DB: projects
- report: etc
- 가치: 상
- 대조: AUGMENT
- 근거: 기존 개발 환경·배포 자동화와 구분되는 공유 테스트 DB의 실제 이력과 후속 적용 경계를 추가한다.
- 대상/참고: https://app.notion.com/p/tossApp-3d88a230a04e81acb8a6cf6954fed140
- 본문 paragraph 최대: 229자

## 2026-10-13 tarororo-test DB 선행 상태

실제 tarororo-test DB에서 읽기 전용 transaction으로 migration history/checksum과 catalog만 확인했다. 001–005는 존재하고 소스 SHA-256과 모두 일치했다. 006–012는 이력과 관련 신규 테이블 14개가 없었다. 013도 이력과 신규 테이블 4개가 없었으며 checksum mismatch는 0건이었다. 애플리케이션 사용자 데이터는 조회하지 않았다.

## source 등록과 적용 범위

server/src/db/migrate.ts에는 001–013이 모두 등록돼 있으며 일반 runner는 누락된 파일 전체를 순서대로 실행한다. 따라서 일반 db:migrate를 006–012 전용 실행 경로로 안내하지 않는다. 기존 MCP는 정확한 013 전용 적용을 제공하므로 006–012 배치 기능은 후속 구현 대상이다.

SQL 등록·폐기 가능한 DB에서의 과거 검증은 공유 DB 적용 증거가 아니다. 현재 소스의 미커밋·미추적 고위험 입력도 별도 검토 대상이며 이력 보충·자동 커밋·HEAD 검사 우회로 해결하지 않는다.

## 보존·인계·후속

진단 전후 기존 컨테이너의 ID/image/시작 시각을 보존했고 실제 DB 변경·백업 복원·Docker 배포를 하지 않았다. 인계 문서 /home/jhw/ai/opencode/projects/tossApp/docs/handoffs/tarororo-test-schema-preflight.md를 작성하고 실제 personal-ops-tunnel read_file로 읽을 수 있음을 확인했다.

후속은 006–012만 고정한 실행 계획과 백업/복원 검증·승인 경로다. 013은 선행 이력 검증과 별도 사용자 승인 단계로 유지한다. 이번 진단으로 tossApp 전체 기능·실결제·광고·공개 배포를 완료 처리하지 않는다.

## [3] 터널 롤아웃 실제 실행본 검증·안전 복구·TTY와 로그 보존

- DB: knowledgeBase
- report: etc
- 가치: 상
- 대조: AUGMENT
- 근거: 같은 터널 운영 기록에 후속 롤아웃 사례와 실제 프로세스/cgroup 판정 및 로그 보존 방법을 보강한다.
- 대상/참고: https://app.notion.com/p/personal-ops-tunnel-user-systemd-3f78a230a04e81e2bd3df010535b32f5
- 본문 paragraph 최대: 263자

## 실제 실행본과 설정의 일치 검증

새 source/profile 경로와 healthy/ready만으로 새 MCP 활성화를 단정하지 않는다. actual PID·시작 tick·argv·immutable manifest, 터널 profile, systemd startup 명령을 함께 비교한다. 2026-10-13에는 세 경로가 서로 다른 릴리스여서 기존 MCP 도구가 계속 실행되는 상태를 확인하고 실제 실행본을 정렬했다.

Type=oneshot/active-exited인 unit의 MainPID=0은 daemon 생존이나 소유를 증명하지 않는다. 이번 실제 관측에서 daemon은 unit과 다른 cgroup에 있었으므로 이전 기록의 cgroup 설명을 현재 제어 대상의 근거로 사용하지 않는다. 정확한 alias·프로세스 세대·설정 identity를 freeze하고, concurrent 변경이나 PID 재사용이면 종료/복구 범위를 넓히지 않는다.

## TTY·process group·durable receipt

RTK proxy는 stdin이 TTY여도 stdout이 pipe일 수 있어 host TTY 확인이 실패한다. script -q -e -c로 실제 TTY 입출력과 종료 코드를 보존하되 확인 절차를 우회하지 않는다. subprocess를 새 session/group으로 시작하고 자신이 생성한 그룹만 정리해 부모 터미널·다른 Codex 작업에 영향을 주지 않는다.

롤아웃 receipt는 개인 디렉터리 0700/파일 0600에 phase·typed code/cause·return code·출력 크기/hash·actual runtime identity를 지속 기록한다. 원문 argv/env/stdout/stderr·credentials·터널 ID는 기록하지 않는다. 오류/검증 대기 시 터미널에 결과를 남기는 pause 경로를 사용한다. 과거 창 종료의 최초 원인은 유실된 로그 때문에 확정하지 못했으며 예방·진단성 개선과 구분한다.

## systemd cache와 connector/UI 검증

cached DropInPaths가 남아 있는 reload 전과 새 설정이 로드된 reload 후를 구분해 검사한다. 실패 시 검증된 실제 이전 런타임으로 복구하며 다른 운영자의 변경을 덮지 않는다.

수동 health의 awaiting_discovery/tool_count=0은 connector 도구 0개라는 뜻이 아니다. actual schema 노출과 읽기 전용 도구 호출을 검증하고 admin의 실제 argv/manifest·정적 자산·API·브라우저와 승인 거부 경계를 확인한다. 관리 웹의 LAN 조회 허용과 loopback/지정 HTTPS 승인 허용은 서로 다른 경계다.

## [4] 마이그레이션 소스 등록·격리 테스트와 실제 DB 적용 구분

- DB: knowledgeBase
- report: etc
- 가치: 상
- 대조: SIMILAR
- 근거: 같은 tossApp DB 검증 분야지만 환경 파일 복구와 독립적으로 검색할 실제 migration 판정 지식이다.
- 대상/참고: https://app.notion.com/p/tossApp-server-env-DB-3f48a230a04e81458751e90db84df05b
- 본문 paragraph 최대: 257자

## 판정 원칙

마이그레이션 파일 존재·runner 등록·폐기 가능한 DB 테스트 통과는 공유/실제 DB 적용 증거가 아니다. 정확한 DB identity에서 읽기 전용으로 history의 파일 이름/checksum과 예상 relation/catalog를 대조한다. missing history와 checksum mismatch를 구분하며 애플리케이션 row를 조회하지 않아도 준비 여부를 확인할 수 있다.

## 세션 사례

2026-10-13 tarororo-test 진단에서는 001–005 history/checksum이 일치했고 006–012 history 및 신규 테이블 14개, 013 history 및 신규 테이블 4개가 없었다. mismatch는 0건이었다. 소스에는 001–013이 모두 등록돼 있었으므로 일반 db:migrate를 실행하면 013까지 시도할 수 있다. 006–012만 진행하려면 정확한 파일·순서·SHA·현재 이력을 고정한 별도 실행 경로가 필요하다.

## 적용 승인과 검증

013 SQL 자체의 직접 FK 의존과 기존 MCP의 001–012 전체 checksum 선행 정책은 구분한다. 이력을 임의 삽입하거나 범위가 넓은 runner로 정책을 우회하지 않는다. 미커밋 고위험 source 검토, 백업/격리 복원 시험, transaction·동시성 lock·사후 history/catalog 검증은 실제 변경 계획에 포함한다.

이번에는 읽기 전용 진단과 인계까지만 수행했고 공유 DB 적용·restore·Docker 변경은 하지 않았다. 문서: tossApp/docs/handoffs/tarororo-test-schema-preflight.md.

(참고: https://app.notion.com/p/tossApp-server-env-DB-3f48a230a04e81458751e90db84df05b)

## [5] personal-ops-tunnel 범용 호스트 작업·승인형 CLI 위임 설계

- DB: decisionLog
- report: etc
- 가치: 상
- 대조: NEW
- 근거: 등록 대상·웹 최종 승인·선택적 에이전트 위임의 구체적인 신규 설계 결정이다.
- 본문 paragraph 최대: 267자

## 확정한 설계

일반 ChatGPT 채팅에서 등록된 로컬 프로젝트를 선택하고 파일/격리 작업, Docker·PostgreSQL 작업 계획과 승인 후 실행·결과 확인을 이어간다. 최종 승인은 기존 관리 웹에서 받는다. 호스트 target registry version 3과 source-pinned 일회용 작업 계약을 추가하면서 기존 version 1·2와 7개 도구의 동작을 보존한다.

기본 경로는 기존 MCP 직접 실행이다. 필요한 도구·실행 환경·복잡한 workflow 때문에 완료하지 못하면 목적·범위·프로젝트·모델·시간·추가 사용량을 제시하고 웹 승인 후 전용 CLI task로 위임한다. 정책/승인/비밀 접근 차단, stale plan, 불확실한 이전 효과를 위임으로 우회하지 않는다.

## 권한과 데이터 경계

임의 host shell과 모델의 approved=true는 권한 근거가 아니다. Docker/DB effects는 대상·입력·현재 상태를 고정한 별도 web receipt 후 실행한다. 기존 컨테이너/DB 데이터 보존과 Schema 013 별도 승인, backup/restore 검사, replay 방지, unknown 결과의 읽기 전용 재확인을 유지한다.

Codex를 첫 provider로 계획하고 Claude adapter는 동일 계약의 검증 후 활성화한다. CLI 설치 사실만으로 auth/격리를 확인한 것으로 보지 않는다. workspace-write/cwd만으로 호스트 읽기 차단을 주장하지 않고 실제 profile·binary의 차단 시험과 승인된 model smoke를 요구한다. 추가 모델 사용량과 로그인 방식별 한도/과금 경로를 표시한다.

## 결정과 구현 상태 구분

사용자가 설계에 진행 의사를 표시해 설계는 확인됐고 구현 계획을 작성했다. 이어 사용자가 직접 Claude에게 구현 작업을 지시했다고 확인했다. 이 Codex 세션에서는 범용 registry·DB adapter·agent task 호출 구현이나 그에 따른 host 설정·서비스·DB 변경을 하지 않았다. Claude의 실제 구현·테스트·운영 반영 결과는 아직 검증하지 않았다. 구현 담당 변경만으로 런타임 CLI provider나 운영 변경 승인까지 확정한 것으로 보지 않는다.

문서: personal-ops/docs/superpowers/specs/2026-10-13-chat-host-operations-design.md 및 docs/superpowers/plans/2026-10-13-chat-host-operations.md.

## [6] 일반 Chat 작업의 최종 웹 승인과 선택적 에이전트 위임 선호

- DB: preferences
- report: etc
- 가치: 상
- 대조: AUGMENT
- 근거: 기존 일반 Chat·허용 레포·진행 표시 선호에 이번에 직접 선택한 승인 위치와 위임 조건을 추가한다.
- 대상/참고: https://app.notion.com/p/Chat-3f38a230a04e816c9946fa32b935af33
- 본문 paragraph 최대: 183자

## 최종 승인과 기능 부족 시 위임

사용자는 파일 편집·일반 검사·계획·승인 후 실행을 일반 personal-ops-tunnel 채팅에서 이어가고, 확대된 Docker·DB·스키마 작업의 최종 승인은 기존 관리 웹에서 받는 방식을 선택했다. 채팅 안 승인 화면은 이번 우선 경로로 선택하지 않았다.

MCP의 기능 한계로 작업을 완료하지 못하면 승인에 의한 코딩 에이전트 호출을 추가하기 원한다. 기본 실행을 모두 에이전트로 바꾸기보다 필요할 때 범위·프로젝트·모델·시간·추가 사용량을 확인해 위임하는 흐름이다. 기존 데이터·서비스 변경과 Schema 013은 각 승인 경계를 유지한다.

사용자가 직접 Claude에게 구현 작업을 지시했다고 확인했다. 코드를 작성하는 담당 모델과 플러그인이 실제 호출할 CLI provider는 별개다. Claude의 실제 구현·테스트 결과와 호출 provider 설정 변경은 아직 검증하지 않았다. 이 Codex 세션에서는 중복 구현이나 추가 CLI 모델 호출을 시작하지 않는다.

기존 허용 로컬 레포 변경, 불필요한 레포 선택 반복 제거, 실제 변경/검사/진행을 관찰하는 대시보드 요구를 유지한다. 호스트의 LAN 조회와 신뢰된 HTTPS 승인 경계도 별도로 안내한다.

## [7] 2026-10-13 런타임 복구·DB preflight 후속 인계 문서

- DB: references
- report: etc
- 가치: 중
- 대조: AUGMENT
- 근거: 기존 작업실 Handoff 작성 뒤의 실제 롤아웃 복구·DB 진단을 최신 이어가기 문서로 연결한다.
- 대상/참고: https://app.notion.com/p/personal-ops-Handoff-2026-10-12-3f78a230a04e81588f24cfc52f18a7f5
- 본문 paragraph 최대: 239자

## 2026-10-13 후속 인계

기존 2026-10-12 작업실 Handoff 이후 런타임 전환 진단·코드 수정·승인된 운영 복구와 실제 connector/UI 검증을 완료했다. 새 기준 문서는 personal-ops/docs/handoffs/2026-10-13-tunnel-rollout-recovery.md다. 실패 원인, actual runtime, typed durable receipt, 승인 경계, 144/280 검증 및 연결된 DB 진단을 담는다.

tossApp 쪽 후속 문서는 /home/jhw/ai/opencode/projects/tossApp/docs/handoffs/tarororo-test-schema-preflight.md다. 실제 connector read_file 성공을 확인했고 001–005 일치/006–012 및 013 missing, source runner 전체 범위, 미커밋 고위험 입력·별도 승인·데이터 보존을 기록했다.

앞선 agent-handoff 문서는 진단 시작점이며 최신 완료 상태는 recovery 기록을 우선한다. 과거 작업실 Handoff의 미구현/미검증 표시를 새 운영 결과까지 반영된 것으로 가정하지 않는다. CLI 수집기 소스와 새 CLI 작업 호출 기능도 구분한다.

이번 참조는 로컬 파일로, 신규 원격 commit/PR URL은 없다. 자격 증명·원문 실행 로그·모델 프롬프트를 복사하지 않는다.

## [8] 범용 호스트 작업·PostgreSQL·승인 에이전트 구현 문서 묶음

- DB: references
- report: etc
- 가치: 상
- 대조: SIMILAR
- 근거: 기존 작업실 인계와 같은 프로젝트이지만 새 운영 기능 구현을 위해 별도로 검색할 설계·계획 묶음이다.
- 대상/참고: https://app.notion.com/p/personal-ops-Handoff-2026-10-12-3f78a230a04e81588f24cfc52f18a7f5
- 본문 paragraph 최대: 321자

## 정본 로컬 문서

저장소: /home/jhw/ai/opencode/projects/personal-ops.

- docs/superpowers/specs/2026-10-13-chat-host-operations-design.md
- docs/superpowers/plans/2026-10-13-chat-host-operations.md
- docs/superpowers/plans/2026-10-13-registered-host-operations.md
- docs/superpowers/plans/2026-10-13-postgres-host-operations.md
- docs/superpowers/plans/2026-10-13-approved-agent-delegation.md

## 이어가기 상태와 순서

설계는 사용자 확인 완료이고 구현 계획을 작성했다. 사용자가 직접 Claude에게 구현 작업을 지시했다고 확인했으며 실제 구현·테스트·운영 반영 결과는 아직 검증하지 않았다. 계획 순서는 A의 registry/approval/Compose/MCP·웹 계약을 먼저 구현하고 B PostgreSQL, C 승인된 Codex 위임을 연결하는 것이다. 각 task에는 정확한 파일·함수 계약, 실패 테스트·검증 단계·보호 조건이 있다. 원본의 사용자 WIP를 보존하는 별도 worktree에서 구현하도록 기록했다.

실제 설정·서비스·Docker·DB는 이 문서 작성 과정에서 변경하지 않았다. 013은 별도 승인이다. 에이전트는 Linux 읽기/쓰기 격리와 frontend/tool auth 경계를 실제 검증하기 전까지 비활성으로 유지한다. 소스 구현 담당을 Claude로 지정한 것은 Codex provider 계획을 자동 변경하거나 운영 변경 승인을 대신하지 않는다.

## 문서 검증

5개 문서의 소유자/regular file, UTF-8, 줄끝·trailing whitespace, 내부 링크, placeholder·secret token 부재와 plan header/checklist를 검사했고 git diff --check를 통과했다. 이 검사는 신규 기능 소스 테스트나 실제 모델/호스트 운영 검증이 아니다.

(참고: https://app.notion.com/p/personal-ops-Handoff-2026-10-12-3f78a230a04e81588f24cfc52f18a7f5)

## [9] Codex 원격 공유·writer 충돌·재부팅 연결 복구

- DB: knowledgeBase
- report: etc
- 가치: 상
- 대조: DUPLICATE
- 근거: 실제 writer 점유, 옵션 없는 resume 성공, bootstrap --remote-control/영구 설정 및 재부팅 미검증 경계가 기존 본문에 이미 기록돼 있다.
- 대상/참고: https://app.notion.com/p/Codex-3f38a230a04e81e1ba80fecd7a5cfb6b
- 본문 paragraph 최대: 89자

이전 단계의 원격 세션 공유·writer 충돌·재부팅 연결 복구는 기존 Knowledge Base 본문과 같은 사실·검증 경계를 가지므로 중복 저장하지 않는다.

## 저장 결과

사용자가 OK로 1–8번을 승인했다. 신규 3건(NEW 1건·SIMILAR 2건), 기존 보강 5건, 중복 제외 1건이다. 신규 문서는 report=etc로 저장했고 보강은 기존 properties를 변경하지 않고 2026-10-13 보강 제목과 delta 본문만 추가했다. 9번 DUPLICATE는 저장하지 않았다.

저장 후 8개 페이지를 전체 조회했다. 본문 잘림 없이 승인된 모든 paragraph와 Claude 작업 지시 상태가 저장됐음을 확인했다.

- [1] [personal-ops-tunnel 런타임 롤아웃·신규 도구·관리 웹 복구 완료](https://app.notion.com/p/personal-ops-34a8a230a04e81a6a3b6c9cac75e2511) — 기존 보강·본문 재조회 확인
- [2] [tossApp tarororo-test 선행 migration 읽기 전용 진단·인계](https://app.notion.com/p/tossApp-3d88a230a04e81acb8a6cf6954fed140) — 기존 보강·본문 재조회 확인
- [3] [터널 롤아웃 실제 실행본 검증·안전 복구·TTY와 로그 보존](https://app.notion.com/p/personal-ops-tunnel-user-systemd-3f78a230a04e81e2bd3df010535b32f5) — 기존 보강·본문 재조회 확인
- [4] [마이그레이션 소스 등록·격리 테스트와 실제 DB 적용 구분](https://app.notion.com/p/DB-3f88a230a04e81f4be89ccb239becefd) — 신규 저장·본문 재조회 확인
- [5] [personal-ops-tunnel 범용 호스트 작업·승인형 CLI 위임 설계](https://app.notion.com/p/personal-ops-tunnel-CLI-3f88a230a04e814980bdfea77bc4b93b) — 신규 저장·본문 재조회 확인
- [6] [일반 Chat 작업의 최종 웹 승인과 선택적 에이전트 위임 선호](https://app.notion.com/p/Chat-3f38a230a04e816c9946fa32b935af33) — 기존 보강·본문 재조회 확인
- [7] [2026-10-13 런타임 복구·DB preflight 후속 인계 문서](https://app.notion.com/p/personal-ops-Handoff-2026-10-12-3f78a230a04e81588f24cfc52f18a7f5) — 기존 보강·본문 재조회 확인
- [8] [범용 호스트 작업·PostgreSQL·승인 에이전트 구현 문서 묶음](https://app.notion.com/p/PostgreSQL-3f88a230a04e81418f50cd904bc8754a) — 신규 저장·본문 재조회 확인

이 저장은 Claude의 구현 완료나 host 설정·서비스·Docker·DB·Schema 013 변경 승인을 뜻하지 않는다.
