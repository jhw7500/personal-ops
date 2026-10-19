# 등록형 호스트 작업 승인 단순화 — 2026-10-13

사용자는 기능별 범위 활성화와 사전 커밋으로 계획 생성이 반복해서 막히는 문제를 줄이도록, 등록된 대상에서는 계획을 바로 만들고 관리 웹에서 고정된 소스를 최종 승인하는 흐름을 승인했다.

## 변경 계약

- 기존 활성 등록형 v3와 정확한 고정 legacy 대상을 보존한다. `source_review=reviewed_snapshot`인 등록 대상에서 기존 API·Worker·006–012·013 작업 계획을 허용한다.
- 관리 웹/호스트 TTY의 계획별 최종 승인과 1회 영수증을 유지한다. 계획 생성이나 승인만으로 실행하지 않는다.
- 미커밋·미추적 입력은 개인 스냅샷에 복제하고, 고위험 입력의 SHA/모드/변경 자료를 승인 화면에 표시한다. 소스·등록 설정·검토 자료의 SHA를 계획과 묶고 승인/실행 때 재검증한다.
- 기존 고정 legacy 및 `git_head` 정책은 기존 권한/HEAD 검증을 유지한다. 이미 저장된 계획의 계약을 자동으로 새 정책으로 바꾸지 않는다.
- 006–012 전용 배치에서 013을 실행하지 않는다. 013은 계속 별도 계획·별도 승인이다. 백업/복원 검증·DB 세대·컨테이너 정체성·만료·락 검사를 유지한다.
- 임의 SQL, generic PostgreSQL backup/restore, 에이전트 위임(B·C)은 이 변경에 포함하지 않는다. 등록형 Compose capability와 일반 편집/명령 도구도 그대로 유지한다.

## 작업 및 검증 기록

- 원본: `/home/jhw/ai/opencode/projects/personal-ops`, HEAD `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`.
- 분리 작업본: `/home/jhw/.local/state/personal-ops-worktrees/simplified-host-ops-20261013-1351w0hf`. 원본의 현재 미커밋 소스 100개를 복제하고 파일별 해시를 `.baseline-current-source.json`에 저장했다.
- 변경 전 호스트 모의 테스트: `test_host_*.py` 163개 통과. 기존 공유 DB·Docker는 시험 대상으로 사용하지 않았다.
- 증거 디렉터리: `/home/jhw/.local/state/personal-ops-rollout-recovery/20261013/simplified-host-approval-4mnx9ws0`(0700, 파일 0600).
- 변경 후 결과와 원본 반영/런타임 적용 상태는 최종 검증 후 아래에 추가한다.

## 실제 호스트 실행 경계

코드 구현·회귀 테스트·기준선 확인 후 원본 반영은 이번 요청 범위다. 불변 런타임 설치 및 MCP/관리 웹 재시작은 구체적인 새 snapshot과 영향을 제시한 뒤 별도 승인을 받는다. 실제 DB 006–012/013 적용과 API·Worker 배포는 웹 채팅이 계획별 승인 후 수행한다. 이 변경은 host-runtime.json을 자동으로 편집하거나 기존 Docker/DB를 변경하지 않는다.


## 최종 결과

- 원본 14개 파일을 기준선 SHA 비교 후 반영했다. 충돌 파일과 다른 세션의 WIP를 덮어쓰지 않았고 Git HEAD/commit은 변경하지 않았다.
- core 회귀는 실제 `freeze_source`와 generic-only `scripts/content/sample.test.mjs`를 써 4종 계획/승인과 live/frozen 변경 거부를 검증한다. cleanup 진단 유실도 회귀로 고정했다.
- 관리 웹은 실행 소스 digest와 검토 inventory digest를 분리해서 표시/검증한다. 원문 diff/SHA/실행비트는 실제 review artifact 필드를 쓰며 mismatch/fetch 실패/선택 경합에서는 승인을 막는다.
- Node UI 17/17, Playwright UI 32/32 통과(관리 웹 작업자 최종 실행). 변경 Python 9개 Ruff 통과(루트 실행). 독립 검토 미해결 항목 0.
- 최종 런타임 preflight: `preflight_ok`, exit 0. targeted 및 전체 unittest 단계가 모두 exit 0이며 source 40개/관리 프로필·서비스 정합성을 확인했다. 설치·서비스 전환은 하지 않았다.
- 실제 실행 snapshot: b564eca27518ae811641. 준비된 후보 snapshot: 14734d0dc510ef0a526a.
- 최종 증거: /home/jhw/.local/state/personal-ops-rollout-recovery/20261013/simplified-host-approval-4mnx9ws0/rollouts/rollout-26p_be4t/receipt.json, `preflight-final.log`, `source-integration.json`. 증거 파일은 사용자 소유 regular file/0600이다.
- host-runtime.json의 원본 SHA와 credential fingerprints는 그대로다. 공유 DB·Docker·스키마·광고 모드를 변경하지 않았다.

실제 적용 전에는 사용자가 새 snapshot 설치와 `personal-ops-tunnel.service`/`personal-ops-admin.service` 재시작 영향을 승인해야 한다. 승인 후에도 Schema 006–012와 013 DB 변경은 웹 채팅에서 새 계획별 관리 웹 승인으로 별도 실행한다.

승인 후 호스트 실행 명령(이 단계에서는 실행하지 않음):

```bash
rtk proxy script -q -e -c '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261013/simplified-host-approval-4mnx9ws0/rollouts' /dev/null
```


## 런타임 적용 및 실제 플러그인 검증 완료

사용자의 후속 `진행` 승인으로 14734d0dc510ef0a526a를 설치하고 MCP/관리 웹 두 서비스를 전환했다. 실제 MCP PID 1423212와 관리 웹 PID 1423673 모두 이 불변 release를 실행한다. 현재 Codex PID 233826은 제어 대상 터널의 하위 프로세스가 아니며 종료하지 않았다.

롤아웃 원본 영수증은 catalog의 awaiting_discovery 때문에 exit 2 / verification_required였다. 설치 자체는 성공(runtime_verified/tunnel_healthy/admin_ready)했고, 이후 실제 ChatGPT 플러그인의 조회 및 006–012 prepare 호출로 기능을 검증했다. 원본 영수증은 수정하지 않았고 별도 activation-verification.json에 후속 증거를 저장했다. 현재 커넥터 메타데이터에 호스트 도구 16개가 노출된다(전체 메타데이터 40개). 이것을 새 child가 tools/list 전체 스키마를 다시 제공한 증거와 혼동하지 않는다.

- 실제 prepare_predecessor_migration 성공: 검증 ID 51410e76bc2f2f54b4af230455b05b45, reviewed_snapshot, 미추적 SQL 포함, source/config/review 해시 고정, 006–012만 포함.
- 실제 관리 웹 loopback/LAN/Tailscale HTTPS 3개 origin에서 해당 계획 자동 선택, 커밋 불필요 정책, 고위험 원문/SHA/실행비트 표시, 추가 scope 폼 숨김과 승인 버튼 활성화를 확인했다. 승인 버튼은 클릭하지 않았고 execute 도구도 호출하지 않았다.
- 검증 계획은 10분 만료 후 expired / PLAN_EXPIRED가 됐다. started_at/backup은 null이고 실제 DB 적용은 0회다. 웹 채팅에서는 새로운 request_id로 새 계획을 만든다. 만료된 검증 ID를 실행용으로 재사용하지 않는다.
- Docker 3개 컨테이너의 ID/image/started_at/state/health, host-runtime.json 해시와 권한을 유지했다. 013은 missing, 선행 일치 5/12, 광고 모드는 disabled다.
- 기존 source/WIP와 Git HEAD를 보존했으며 커밋하지 않았다. 일반 SQL/DB restore/에이전트 위임(B·C)은 여전히 후속 범위다.

최종 증거: activation-before.json, activation-connector-before.json, activation-after-host.json, activation-terminal.log, rollout-kcdar_xq/receipt.json, live-management-ui-verified.json, activation-verification.json. 첫 브라우저 probe는 잘못된 submit selector로 실패했고, 실제 button selector를 고쳐 3개 origin 모두 통과했다(제품 코드 수정이나 재시작 추가 없음).

웹 채팅 후속: 새 request_id로 tarororo-test의 006–012 계획만 생성하고 발급된 approval_url의 관리 웹에서 소스를 검토/최종 승인한다. 이후 해당 operation_id로 execute_predecessor_migration을 호출한다. 013은 001–012 일치 확인 후 별도 계획/별도 승인이다.

## 2026-10-14 — 대상별 사전 허용과 006~013 통합 적용

사용자는 위의 작업별 승인 방식 대신 `tossApp / tarororo-test`를 관리 웹에서
한 번 허용하고 채팅으로 계속 작업하는 설계에 `진행`으로 승인했다. 이 후속
정책은 사전 허용을 켠 뒤 만드는 새 계획에 적용한다. 실제 권한 부여와 런타임
서비스 전환은 아래의 운영 경계에 따른다.

- 관리 웹에 `GET/POST /api/host-target-authorization` 및 허용/철회 UI를 추가했다.
  허용 대상 문구는 `tossApp/tarororo-test`이며 기존 loopback/Tailscale HTTPS
  Origin·관리자 토큰 재입력 조건을 유지한다. LAN은 조회만 가능하다.
- 권한은 개인 상태의 user-owned regular `0600` 파일에 저장한다. 등록형 설정
  digest와 현재 고정 001~013 SQL SHA-256을 함께 묶는다. 철회·설정 변경·SQL
  바이트 변경 시 무효이며 다시 허용하면 새 ID를 발급한다. 같은 내용의 반복
  허용은 멱등적이다. 일반 API/Worker 소스 변경은 계획별 스냅샷으로 처리한다.
- 새 계획은 권한 ID를 고정하고 작업별 영수증 없이 실행할 수 있다. 허용 전의
  계획을 자동 승격하지 않는다. 철회와 실행 수락을 같은 짧은 lock으로 직렬화해
  철회 완료 뒤 새 작업이 수락되지 않는다. 이미 수락한 작업을 강제 종료하지
  않으며 실행 직전에도 권한을 재검증한다.
- `prepare_db_migration`은 유효 권한과 정확한 001~005 이력·006~013 미적용 상태에서
  내부 `schema_update` 계획을 만든다. 모든 001~013 해시와 DB 세대를 고정하고
  `execute_db_migration`으로 006~013을 백업·복원 검증 1회와 한 트랜잭션으로
  적용한다. 전체 이력·체크섬·테이블·선행 인덱스·함수·DB 세대·컨테이너를 검증한다.
  기존 predecessor 전용 계획은 계속 006~012이며, 001~012가 준비됐으면 기존
  013 전용 실행을 사용한다. 불일치/부분 적용/불확실한 COMMIT을 자동 재시도하지 않는다.
- UI는 현재 활성 권한 ID가 계획 ID와 연결된 권한에 일치할 때만 실행 가능으로
  표시한다. 철회·SQL 변경·재허용 뒤의 오래된 계획에는 새 계획 생성을 안내한다.
- 등록형 Compose 작업, 임의 SQL/DB 복원/에이전트 위임의 범위는 확장하지 않았다.

검증은 fixture의 모의 Docker/DB 및 임시 HTTP 서버에서 수행했다. 원자 수락
경합·SQL 변경/철회·006~013 트랜잭션·인덱스/함수 누락·MCP 실행 경로·오래된 UI
표시를 RED/GREEN 회귀로 검증했다. 독립 검토 미해결 0건(APPROVE), 집중 검토
Python 40개, Node UI 24개 및 실제 Playwright 허용/철회 플로우가 통과했다.
루트 Ruff는 변경 Python 15개 모두 통과했다. 최종 필수 대상/전체 테스트와
후보 snapshot 결과는 같은 증거 디렉터리의 rollout 영수증을 따른다.

증거: `/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/target-authorization-z1380r6y`.
분리 작업본은 기존 `simplified-host-ops-20261013-1351w0hf`를 재사용했다. 시작 전에
원본과 101개 파일이 동일함을 확인하고 `.baseline-target-authorization.json`을
저장했다. 원본 반영은 이 기준선 SHA를 비교하여 충돌 파일을 덮어쓰지 않는다.

운영 경계: 이 코드 작업에서 실제 grant·SQL 적용·API/Worker 배포를 실행하지
않는다. 현재 실행 런타임은 `14734d0dc510ef0a526a`이고 live grant 파일은 0개다.
새 후보 snapshot 설치와 `personal-ops-tunnel.service`/`personal-ops-admin.service`
재시작은 구체적인 최종 후보·검증 결과를 제시하고 별도 승인 후 수행한다.
그 뒤 사용자가 관리 웹에서 대상 권한을 한 번 켜면 웹 채팅에서 새 request_id로
`prepare_db_migration`을 호출하고 통합 계획을 확인한 뒤 `execute_db_migration`으로
진행할 수 있다. 허용 버튼 자체는 SQL/Docker 실행을 시작하지 않는다.

최종 적용 전 점검: `rollout-e_anjzh1/receipt.json`은 `preflight_ok`, exit 0이다.
필수 대상 334개와 전체 477개 테스트가 각각 exit 0이며 source 40개의 후보
snapshot은 `62b41aa689fc009e7393`이다. 실제 MCP PID 1423212와 터널 PID
1423186은 계속 `14734d0dc510ef0a526a`를 실행한다. profile/service 정합성도
통과했다. `installed=false`, `switched=false`이며 실제 grant 파일은 0개다.

호스트 승인 후 실행할 구체적인 전환 명령(아직 실행하지 않음):

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 PATH=/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin script -q -e -f -c '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/target-authorization-z1380r6y/rollouts' /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/target-authorization-z1380r6y/activation-terminal.log
```

전환은 기존 alias/profile/unit의 MCP 실행 경로를 검증된 후보 release로 갱신하고
관리되는 터널 및 관리 웹 서비스를 재시작한다. 실행 중인 다른 Codex 세션이나
Docker/DB 컨테이너는 종료하지 않는다. 현재 승인 단계는 이 런타임 전환만을
대상으로 하며 DB 변경·실제 대상 권한 부여는 포함하지 않는다.

## 2026-10-14 — 승인된 런타임 전환 완료

사용자의 `승인`으로 후보 `62b41aa689fc009e7393`을 설치하고 기존 터널·관리 웹
두 서비스를 전환했다. 적용 과정의 필수 대상 334개와 전체 477개 테스트도
각각 exit 0이다. 실제 MCP PID 3296912, 터널 PID 3296896, 관리 웹 PID
3297116은 모두 이 release를 실행한다. 현재 Codex 실행은 제어 대상 프로세스
밖임을 확인했고 재시작 직전 실행 중인 호스트 작업은 0개였다.

원본 `rollout-2y98g7m2/receipt.json`은 수동적인 catalog 관찰이
`awaiting_discovery`라 exit 2 / `verification_required`를 기록했다. 설치·전환·
`runtime_verified`·`tunnel_healthy`·`admin_ready`는 성공했다. 원본 영수증을
수정하지 않고 `activation-verification.json`에 후속 실제 검증을 저장했다.
fresh child의 전체 tools/list 스키마 재검증까지 완료했다는 의미는 아니다.

- 실제 플러그인 `get_predecessor_migration_status`가 새 인덱스 14개/함수 5개
  상태 필드를 반환하고 006~013 계획의 `ready_to_prepare=true`를 확인했다.
  `get_runtime_status`는 API healthy/Ready, Worker·PostgreSQL running을 확인했다.
  병렬 조회 한 번의 연결 오류는 순차 재호출로 복구됐다.
- 실제 관리 웹 loopback/LAN/Tailscale HTTPS 3개 주소에서 로그인 후 tossApp을
  선택했을 때 새 권한 UI가 보이고 `supported=true`, `enabled=false`,
  `reason=NOT_GRANTED`임을 확인했다. 허용/철회 버튼 상태, JS 오류 0개, 토큰
  미노출을 검증했다. 권한 허용 버튼은 클릭하지 않았다.
- Docker 3개 컨테이너의 ID/image/started_at/state/health, host-runtime.json과
  runtime-key/admin-token fingerprint는 전환 직전과 동일하다. Schema 013은
  missing이고 선행 이력은 001~005만 적용됐다. 실제 grant 파일은 0개다.
- 전환 stdout/오류는 `activation-terminal.log`에 보존했고, 모든 증거 파일의
  사용자 소유 regular type/0600을 확인했다. Git commit/staging은 하지 않았다.

이제 Tailscale HTTPS 관리 웹에서 tossApp을 선택하고 “채팅에서 자동 실행 허용”에
`tossApp/tarororo-test`와 관리자 토큰을 입력해 한 번 허용한다. 그 뒤 터널
채팅에서 새 request_id의 `prepare_db_migration`으로 006~013 통합 계획을 확인하고
`execute_db_migration`으로 진행한다. 이 런타임 전환 승인은 DB 적용 승인이 아니다.

## 2026-10-14 — 채팅의 반복 승인 안내 진단과 응답 수정

사용자가 관리 웹에서 대상 권한을 켰음을 실제 authenticated GET으로 확인했다.
`tossApp / tarororo-test`의 활성 grant는 `703fa067e0a0db9645b9d886cf95cbd4`이며,
API·Worker·migration·predecessor_migration이 대상 허용 범위다.

웹 채팅의 계획 `6e910fcc35af11cff6c75064d4b4b8ba`는 이미 target 권한을 고정했지만
응답의 state/phase가 `pending_host_approval / APPROVAL`이었다. 실행을 진행하지
않아 2026-10-13 17:05:01 UTC에 만료됐고, 실제 MCP 조회는 `expired / PLAN_EXPIRED`를
반환했다. 관리 웹의 빈 대기 목록은 이 10분 만료 필터로 설명된다. 연결 메타데이터의
prepare/execute 설명도 과거의 별도 승인 안내를 그대로 노출했다. 현재 런타임의
승인 로직 자체는 target 계획에서 작업별 영수증을 소비하지 않는다.

실제 MCP 진단으로 새 `prepare_db_migration` 요청이 `schema_update / 006~013` 계획을
정상 생성하고 같은 grant를 고정함을 확인했다. 검증 계획 ID는
`db5edae6591c19e8e8fbee5f438de3e8`, request ID는
`chat-approval-diagnostic-20261014-0225`, 만료는 2026-10-13 17:31:55 UTC다.
승인 링크/명령은 없으며 DB 실행 도구는 호출하지 않았다. 이 검증 ID를 만료 후
실행용으로 재사용하지 않는다.

후속 수정은 저장된 상태와 승인 영수증 형식을 보존하고 MCP 응답에서만 현재
권한·만료를 구분한다. 유효한 target 계획은 `ready_to_execute / READY`,
`approval_required=false`, 정확한 실행 도구/인자의 `next_action`을 제공한다.
철회·권한 회전·설정/SQL 변경은 실행 안내 없이 `blocked`이며, 같은 request ID로
만료 계획을 다시 조회해도 승인 대기 대신 `expired / PLAN_EXPIRED`를 안내한다.
명시적 실행 요청이 있을 때만 다음 도구를 호출하도록 서버 지침/설명도 보완했다.

원본/분리 작업본의 새 기준선은 `.baseline-chat-approval.json`이며 증거 디렉터리는
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/chat-approval-response-hhwlyu77`다.
현재 실행 snapshot은 여전히 `62b41aa689fc009e7393`이며, 이 응답 수정은 검증 및
기준선 비교 후 반영한다. 설치·서비스 재시작은 구체적인 후보와 검증을 제시하고
기존 사용자 지시대로 승인 후 진행한다. grant/config/DB/Docker는 변경하지 않았다.

수정본 검증: Python 전체 484개(136.907초), 새 회귀 7개, Ruff 4개 파일과 구문
검사 모두 통과했다. 독립 검토는 집중 테스트 19개 통과 및 미해결 0건(APPROVE)다.
새 기준선 SHA/모드를 비교해 6개 파일을 원본에 반영했고 다른 WIP 및 Git HEAD는
유지했다. 실제 설치 입력 40개와 전체 Python 테스트 입력이 검증 작업본과 일치한다.
후보 snapshot은 `d944e6138731d4b031b7`이며 아직 설치하지 않았다. 읽기 전용
서비스 preflight는 실제 실행 `62b41aa689fc009e7393`의 MCP/터널 PID와 기존
profile/service 정합성을 확인했다. source-integration.json과 live-preflight.json에
증거를 저장했다. CLI 통합 스크립트의 첫 import는 jobs 검색 경로 누락으로
실패했으나 소스 반영은 완료됐으며, 검색 경로를 고쳐 반영 파일/백업 SHA와 전체
입력 일치를 다시 확인했다. 서비스나 DB에는 영향이 없었다.

호스트 전환 승인 대상 명령(아직 실행하지 않음):

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 PATH=/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin script -q -e -f -c '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/chat-approval-response-hhwlyu77/rollouts' /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/chat-approval-response-hhwlyu77/activation-terminal.log
```

영향은 새 불변 runtime 설치와 기존 터널·관리 웹 두 서비스 재시작이다. 실제
호스트 작업이 진행 중이면 중단하지 않고 기다린다. 허용 권한/설정·Docker/DB와
다른 Codex 세션은 보존한다. 설치 후 실제 MCP 응답의 새 상태/next_action과
관리 웹 조회를 다시 확인하며 DB 실행은 웹 채팅의 명시적 실행 요청에 맡긴다.

## 2026-10-14 — 반복 승인 안내 수정의 런타임 적용 완료

사용자의 후속 `승인`으로 `d944e6138731d4b031b7`을 설치하고 터널·관리 웹 두
서비스를 전환했다. 적용 전 필수 334개와 전체 484개 검사가 각각 exit 0이었다.
실제 실행 MCP PID는 695527, 터널 PID는 695511, 관리 웹 PID는 695680이며
모두 새 release를 실행한다. 전환 직전 실행 중인 호스트 작업은 0개였다.

웹 채팅에서 만든 대기 계획 `f141b7fec1689b5e81ef1c3744da6b3e`는 실행이 시작되지
않은 상태였다. 계획의 immutable 내용과 plan digest, 기존 grant를 보존했다.
전환 후 실제 플러그인의 `get_host_operation` 응답에서 다음을 검증했다.

- `state=ready_to_execute`, `phase=READY`, `approval_required=false`.
- `next_action.tool=execute_predecessor_migration` 및 정확한 project/target/operation ID.
- target이 이미 허용됐으므로 명시적 실행 요청 때 추가 관리 웹 승인 없이 바로
  호출하라는 guidance. 이 계획은 계속 006~012만 포함하며 범위를 자동 확대하지 않는다.

원본 영수증 `rollout-sxqd5yzf/receipt.json`은 수동 catalog 관찰의
`awaiting_discovery` 때문에 exit 2 / `verification_required`를 기록했다.
설치·전환·runtime_verified·tunnel_healthy·admin_ready는 성공했다. 원본을 바꾸지
않고 `activation-verification.json`에 위 실제 MCP 응답과 후속 검증을 기록했다.
커넥터에 캐시된 설명은 여전히 과거 안내를 노출하며 전체 tools/list 스키마의
재발견까지 확인한 것은 아니다. 응답 자체는 현재 권한과 실행 도구를 분명히 안내한다.

- 실제 runtime 조회는 API healthy/Ready, Worker·PostgreSQL running을 확인했다.
- loopback/LAN/Tailscale HTTPS 3개 관리 웹에서 로그인·tossApp 선택·자동 실행
  허용 상태와 버튼 상태를 확인했다. JS 오류 0개, 권한 변경 버튼 클릭 0회다.
- Docker 3개 컨테이너의 ID/image/started_at/state/health, host-runtime.json과
  runtime-key/admin-token 및 grant 파일 SHA가 전환 전과 동일하다. Schema 013은
  계속 missing이며 CLI에서 SQL 실행이나 배포를 수행하지 않았다.
- 로그는 `activation-terminal.log`에 보존했고 모든 증거 파일의 user-owned
  regular type/0600을 확인했다. 커밋·staging과 다른 세션 종료는 하지 않았다.

이제 기존 터널 채팅에서 “계획 상태를 다시 확인하고 실행해줘. 만료됐으면 새
계획을 만들어 진행해줘”라고 요청한다. 위 대기 계획의 만료는 2026-10-13
18:25:07 UTC이며 만료 후에는 새 request ID가 필요하다. 006~013 전체 적용을
원하면 유효한 권한 아래 `prepare_db_migration`이 현재 DB 이력에 맞게 통합 계획
또는 013 전용 계획을 선택한다. 대상 권한·소스·만료 검증은 실제 실행 때도 유지한다.
