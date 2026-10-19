# 대시보드 캐릭터의 실제 작업 설명 개선

날짜: 2026-10-16. 사용자 지시: 호스트 실행기 상세와 대화에 “호스트 실행”이 반복되어
실제 작업을 파악할 수 없는 문제를 수정. 상태: 소스 구현·독립 검토·원본 반영 후 사용자 “승인”으로
**2026-10-16 12:17:35 KST 운영 배포 완료**. 실제 runtime `dfc699ead12d7bcc378e`.
터널·관리 서버 및 운영 UI 검증 완료. ChatGPT 도구 정의 새로고침과 호스트 권한 재승인은 사용자 단계다.
아래 구현 당시 미반영 기록과 실제 운영 전환 결과를 구분한다.

## 구현

- `start_host_execution`, `prepare_agent_task`에 선택적 공개 `task_label` 추가.
  1~160자, 제어 문자 금지, 기존 비밀값 마스킹 사용. 목적과 대상을 직접 전달하며
  비공개 입력문·명령 인자·프롬프트를 복사하지 않는다. 표시용 설명은 권한/진행 증거가 아니다.
- 전달한 설명만 frozen/immutable 입력에 포함한다. 기존 미지정 호출의 digest와 재시도 호환성을
  유지하며 같은 request_id에 다른 설명을 넣으면 충돌한다. 호스트/격리 Codex 모두 전달한다.
- 공개 `work_context`에는 설명, 출처, 고정된 안전한 명령 종류, 기존 행동 분류만 제공한다.
  phase/code와 함께 인증된 부모 화면 및 allowlist 프레임 경계를 통과한다.
  원시 argv/input/env/private log나 중첩 임의 필드는 프레임에 보내지 않는다.
- 작업 요약은 목적·명령 종류·관측 단계·실행 상태·종료 수치·확인 시각을 구분한다.
  “지금 뭐 해?”는 현재 작업, “마지막 결과는?”는 같은 캐릭터의 완료 작업과 결과를 답한다.
  진행 작업을 완료 결과로 오인하지 않으며 기존 인증된 로그 화면에서도 설명과 원래 실행 ID를 유지한다.
- Codex 실행이라는 상위 분류나 reported 도구/행동을 직접 관측한 내부 활동으로 승격하지 않는다.
  상세 내부 행동은 observed 기록만 증거로 사용한다. 모델 내부 사고·하위 단계·진행률은 만들지 않는다.
- 과거 작업에 목적이 없으면 “목적 미수집”, 안전한 명령 분류만 표시한다. 소급 복구하지 않는다.

## 작업 위치와 보존

- 관리형 격리 worktree: `/home/jhw/.codex/worktrees/38e8/personal-ops`.
- 기준: 이전 승인 카드 소스 `/home/jhw/.local/state/personal-ops-worktrees/chat-host-access-ui-20261016`,
  운영 runtime `0be604c833196ab25f29`와 runtime 파일 75개 일치 후 수정.
- root HEAD `cf8b6d2608c9cadaa939fdca8045891e848ec7ba` 및 index 보존.
  원본의 Schema 014·runtime rollout·모바일 승인 링크 WIP를 전체 복사/배포하지 않는다.
  변경 파일만 시작 해시 비교 및 필요한 3-way 병합으로 반영한다.
- 원본에 21개 변경 파일을 반영했다. `host-access-admin.js`와 `check_admin_ui.py`는
  기존 모바일 승인 링크 WIP를 유지하며 3-way 병합했다. 다른 사전 존재 파일의 hash/mode,
  HEAD와 index는 모두 그대로다. 원본 반영 후 Node 206/206(기존 추가 회귀 포함),
  backend 61/61, 해당 Chromium 경로 및 기존 모바일 링크 6/6, Ruff/whitespace 통과.
- 커밋·push·Notion 저장·모델/에이전트 실행·실제 호스트 명령·DB/Docker/스키마 변경 없음.

## 검증

- TDD: backend 누락 설명/마스킹/digest, bridge allowlist, frontend 현재/이전 결과,
  부모 로그 선택기, coarse Codex 생명주기 및 mixed-provenance 회귀의 실패→통과 기록 보존.
- JavaScript 전체 204/204 통과. 관련 Python 61/61 및 스키마 설명 검사 12/12 통과.
- 독립 재검토 APPROVE: coarse code 및 reported run_tests/test가 observed 호스트 상태에
  합쳐져도 내부 행동으로 승격되지 않음을 확인. 브리지·모델·캐릭터 43/43,
  실제 Chromium 경로 3/3, 스키마 4/4, Ruff 통과. 최종 model SHA
  `ac40acd39f687e3b69c67433af93a14569146d18e1a4a2a7622198a85cd756fd`.
- 실제 HTTP 서버+Chromium을 임시 프로젝트/상태로 실행했다. 제목·명령·이전 결과·원래 로그 연결,
  비밀값 차단, 프로젝트 전환, 1440/390/320px 및 변경 요청 없음 검증.
  91개 회귀가 두 번 통과했고 최종 mixed-provenance 추가 후 전체 92/92도 통과했다.
  전체 브라우저 로그의 임시 서버 연결 종료 시 BrokenPipe 진단은 기존 fixture 정리 출력이며
  테스트 실패나 실제 운영 서버 오류가 아니다. 최종 페이지 JavaScript 오류는 없었다.
- 실제 소스 MCP stdio initialize/tools/list에서 도구 53개와 두 task_label 선택 스키마 등록 확인.
  call_tool은 0회다. 첫 임시 HOME 실행은 연결 종료로 실패했으며, 기존 설치가 사용하는 HOME과
  임시 프로젝트/상태를 사용한 재검증은 통과했다. 운영 커넥터 등록 증거로 해석하지 않는다.
- Ruff, JS 구문, whitespace 검사 통과. 데스크톱/폰 inspector 캡처를 직접 확인했다.
  화면은 실제 운영 작업을 가장한 데이터가 아닌 격리된 테스트 실행 응답이다.
- 최종 read-only rollout preflight: `preflight_ok`, exit 0. 승인 카드 UI 10개, targeted 647개,
  Python 전체 812개 검사 정상 종료. opt-in 실제 Codex 격리 probe는 기본 생략하며
  실제 모델을 호출하지 않았다. `--apply` 없이 검사했으므로 installed=false / switched=false.
  source/control 해시 고정 및 기존 profile/service 정합성 확인.
  receipt: `preflight/rollout-dq7_bb_l/receipt.json`.
  초기 preflight는 수정 필요 발견 후 해당 검사 프로세스만 중단했다(exit 130).
  이 이력과 receipt를 보존하며 성공으로 변경하지 않는다.

호스트 전용 증거:
`/home/jhw/.local/state/personal-ops-dashboard/dashboard-task-context-20261016/`.
상세 실행 로그와 캡처는 사용자 전용 파일이며 저장소에 원문을 복사하지 않는다.

## 운영 적용 전 기록

전환 전 운영 runtime은 `0be604c833196ab25f29`였으며 당시 위 새 코드는 실행 중이 아니었다.
preflight에서 터널/MCP의 실제 PID·시작 시각·불변 manifest를 확인했다. 운영 loopback/LAN
HTTP의 변경 대상 자산 4개씩, 총 8개가 기존 runtime의 바이트와 일치함을 확인했다.
승인 대상 후보는 `dfc699ead12d7bcc378e`, 전체 runtime 파일 75개 중 변경 9개다:
`host_execution.py`, `host_execution_tools.py`, `host_agent.py`, `agent_tasks.py`, `agent_tools.py`,
`workroom-bridge.js`, `host-access-admin.js`, `work-dashboard/model.js`, `work-dashboard/visualizer.js`.
이 격리 소스에서만 새 불변 snapshot을 설치·전환한다.
원본 checkout 전체는 미검토 WIP가 있으므로 배포 대상으로 사용하지 않는다.

터널/MCP와 관리 서버 전환은 별도 승인이 필요하다. 진행 중인 호스트/위임 작업 여부를
읽기 전용으로 재확인하고 영향 검토 후 승인된 시점에만 수행한다. 마지막 raw state 조회에서
진행/시작 가능한 호스트 실행은 없었으나 이는 시점의 기록이며 CLI/웹 작업 전체가 중단됐다는 뜻은 아니다.
이번 변경에는 backend와 브리지/부모 경계가 포함되어 기존 기간제 HostAccess는 정책상 무효화된다. 권한을 자동 연장하거나
새로 부여하지 않으며 전환 후 사용자가 채팅 승인 카드로 다시 승인한다.

승인 대상 작업 위치와 최초 실행 명령은 다음과 같았다. RTK proxy는 child TTY를 전달하지 않아
이 직접 실행은 검사 후 `HOST_TTY_REQUIRED`로 중단됐고 설치·전환은 없었다. 아래 실제 전환은
별도 PTY wrapper로 진행했으며 이 명령을 RTK 아래에서 그대로 반복하면 안 된다.

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error \
  --diagnostics-dir /home/jhw/.local/state/personal-ops-dashboard/dashboard-task-context-20261016/activation
```

helper는 검사를 다시 수행한 뒤 기존 `personal-ops-tunnel.service`와
`personal-ops-admin.service`의 승인된 전환만 수행한다. 터널 소유 app-server도 재연결되므로
그 시점의 세션 영향을 확인한다. 기존 사용자 Codex/Claude 세션, Docker/DB 데이터는 변경하지 않는다.

전환 후 실제 runtime/HTTP 자산/관리 UI와 ChatGPT 도구 정의 새로고침을 검증한다.
새 작업에는 공개 task_label을 전달해야 목적이 표시된다. 기존 설명 없는 기록은 그대로 남는다.
passive full catalog discovery가 awaiting_discovery이면 ready로 꾸미지 않는다.

## 승인 후 실제 운영 전환

사용자의 명시적인 “승인” 이후 검증한 후보 해시와 소스/control 해시가 일치함을 다시 확인했다.
원본 checkout 전체가 아닌 위 관리형 격리 worktree에서만 설치했다.

1. 최초 직접 실행의 승인 카드 10개·targeted 647개·전체 812개 검사는 모두 통과했으나,
   RTK가 stdin/stdout TTY를 전달하지 않아 `HOST_TTY_REQUIRED`, exit 1로 중단됐다.
   `activation/receipts/rollout-638xdaxf/receipt.json`을 실패 그대로 보존한다. snapshot_install이나
   tunnel_switch 단계는 시작되지 않았다.
2. OS PTY의 실제 stdin/stdout를 검사한 후, 이미 받은 승인에 한정된 소유자 전용 launcher
   `activation/run-approved-rollout.py`를 사용했다. 원본 helper의 테스트·TTY·확인 절차를
   우회하거나 monkeypatch하지 않았다. 실제 `Type ROLLOUT` prompt에서 소스/control과
   진행 중인 실행 기록을 재검증한 뒤 승인 문자열을 전달했다. 전체 출력과 결과는 파일로 보존했다.
3. 검사 중 새 personal-ops 호스트 명령 1건이 관측됐으나 중단하지 않았다. 자연 종료를 확인했으며,
   전환 직전 active/startable 0건을 확인했다. launcher는 실행이 남아 있으면 전환을 기다린다.
4. 재실행에서도 카드 10개·targeted 647개·전체 812개 검사가 통과했다. `dfc699ead12d7bcc378e`
   설치 후 기존 터널과 관리 서버만 전환했다. 완료 시각 `2026-10-16T03:17:35.211604+00:00`.

원본 receipt `activation/pty-receipts/rollout-y4s4qpcj/receipt.json`의 결과는 다음 그대로다:

- installed=true, switched=true, runtime_verified=true, tunnel_healthy=true, admin_status=admin_ready.
- live_snapshot=`dfc699ead12d7bcc378e`, previous_snapshot=`0be604c833196ab25f29`.
- status=verification_required, exit 2, catalog_status=awaiting_discovery,
  tools_registered=false, connector_schemas_verified=false. 이는 수동 도구 정의 갱신이 미확인인
  상태이며 실제 서비스 실패와 구분한다. 이 receipt를 ready/exit 0으로 바꾸지 않았다.

## 운영 검증 결과

- 실제 터널/MCP PID·시작 시각·실행 경로와 불변 manifest 75개 전체 hash를 확인했다.
  운영 loopback/LAN에서 변경 대상 UI 자산 4개씩 총 8개의 HTTP 바이트가 새 runtime과 일치한다.
- 설치된 runtime의 별도 MCP stdio에서 도구 53개 및 두 선택적 공개 task_label 스키마 등록을
  확인했다. initialize/tools/list만 실행했고 도구 실행·모델 호출은 0회다. 이것은 ChatGPT의
  기존 캐시 정의가 갱신됐다는 증거와 구분한다.
- 실제 ChatGPT personal-ops-tunnel의 `get_host_access(tossApp)` 조회가 성공했다.
  active=false / reason=RUNTIME_CHANGED / scope=installation이다. 기존 권한 파일의 바이트와
  유효 기간을 변경하지 않았으며 자동 재승인·연장하지 않았다.
- 실제 운영 데이터로 personal-ops(호스트 실행 94개), tossApp(29개), pim-check(0개)를 열었다.
  PC 1440px·모바일 크기 390/320px의 총 9개 화면 검사가 통과했고 페이지 수평 overflow와
  JavaScript 오류가 없었다. 로그인 이후 수정 API 요청은 0건이다.
- personal-ops/tossApp의 실제 호스트 실행기 상세, 현재 작업/마지막 결과 대화 및 기존 실행
  선택기에서 명령 설명·단계·종료 수치를 확인했다. pim-check에는 호스트 실행 기록이 없어
  해당 캐릭터가 없는 것이 관측됐으며 가짜 작업이나 캐릭터를 삽입하지 않았다.
- 설치된 admin 모듈과 자산을 사용한 임시 HTTP/Chromium 검사 3/3도 통과했다:
  공개 목적+현재/이전 결과와 로그 경로, 과거 기록/비밀값 경계, reported 내부 행동 승격 방지.
  이는 실제 운영 작업을 새로 실행하거나 목적을 만들어 넣은 검사가 아니다.
- 실제 운영 캡처를 직접 확인했다. 기존 작업에는 설명을 저장하지 않았으므로 “목적 미수집”과
  보수적인 명령 종류가 보인다. 등록된 종류로 판단할 수 없는 실행 파일은 Host command로
  남는다. 새 작업에서 공개 task_label을 제공해야 API·Worker 재빌드 같은 목적이 표시된다.

전환 시작 전과 종료 후를 비교해 독립 Codex/Claude 프로세스 8개 모두 동일 PID·시작 시각으로
생존, 원본 저장소 파일 326개·HEAD·index·설정/권한 파일 13개가 보존됐음을 확인했다.
이후 변경은 이 인계·DESIGN·README·이전 RPG 인계의 운영 기록 4개뿐이다.
Docker·DB·스키마 작업, 실행 권한 재발급, 커밋·push·Notion 저장은 하지 않았다.

## 사용자에게 넘길 단계

1. 관리 대시보드 페이지를 새로고침한다.
2. ChatGPT에서 personal-ops-tunnel 도구 정의를 새로고침한다. 이 승인 대화에 로드된 기존
   도구 정의에는 task_label이 없었으므로 새 runtime 등록 성공만으로 캐시 갱신을 주장하지 않는다.
3. 필요한 프로젝트의 호스트 권한 승인 카드를 열어 사용자가 다시 승인한다.
4. 웹 채팅에는 “앞으로 호스트 명령과 Codex 위임에 공개 작업 제목(task_label)을 함께 보내줘”라고
   전달한다. 비밀값·프롬프트·명령 원문을 제목에 넣지 않는다. 과거 목적은 소급 생성하지 않는다.

최종 증거는 `activation/installed-verification.json`, `live-browser.json`,
`installed-browser-targeted.json`, `pre-switch-guard.json`, `pty-runner-result.json` 및 원본 receipt다.
