# RPG 작업실 1차 구현 인계 (2026-10-16)

## 범위와 현재 상태

사용자는 재설계 검토안의 1차(화면 정리·직업 표시·작업 동작·기록 기반 대화)를 “진행”으로 승인했다.
코드 구현과 격리 브라우저 검증을 마친 후, 후속 “진행” 지시로 운영 배포·서비스 전환을 완료했다. 권한 부여·DB/Docker 변경은 수행하지 않았다. 구현 당시 기록과 이후 운영 반영 기록을 구분한다.
커밋·푸시·PR·Notion 저장도 하지 않았다. 원본의 다른 세션 WIP를 그대로 보존한다.

- 원본: `/home/jhw/ai/opencode/projects/personal-ops`, HEAD `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`.
- 검증한 격리 소스: `/home/jhw/.local/state/personal-ops-worktrees/dashboard-rpg-20261016`.
- 기준선: 수정하지 않은 `dashboard-bubbles-20261015` worktree, 직전 운영 Stage 8.
- 직전 운영 MCP/관리 웹: `41339f4dc09de5363e0d` (PID 3625495 / 3625641 관측). 현재 PID는 3942758 / 3942903.
- Codex observer: `235ef828a3c5e15ca73f`, PID 2753877 관측. 이번에 변경하지 않음.
- 현재 운영 버전: `cc1631a8718e788b9be5`, 총 73개 런타임 파일, 변경 6개. 이전 후보 기록은 그대로 보존한다.
- 후보의 backend·수집기·bridge·실행 권한 구현은 운영 버전과 동일하다.

## 구현한 화면

```text
Project -> Mission -> Workroom -> Character -> Talk / Result / Log
                     |
                     +-> Recent events -> Details / Help
```

1. 기본 탭을 작업실로 변경. 지도는 하나이며 항상 같은 프레임을 유지한다.
2. 모델·연결·작업 목록·안내는 지도 아래 접기 영역으로 이동. 미관측 ChatGPT 기본 카드는 제거.
3. 수동 작업 보고와 과거 결과는 기록·요약 탭으로 이동. 실패·중단 기록 수를 미해결 장애 수로 표시하지 않음.
4. 객체 identity를 유지하면서 개발자/설계자/운영자/도구 담당 표시 직업과 작업 동작 표시를 부여.
   직업은 객체 종류의 표시 기본값이며, 행동 변화로 바뀌지 않고 권한/프로필을 변경하지 않는다.
5. 캐릭터 선택 시 작업 목적·현재 상태·행동/도구·결과·출처를 먼저 표시하고 기술 세부 사항은 접어서 제공.
6. “지금 뭐 해?”, “왜 멈췄어?”, “마지막 결과는?”, “관련 기록 보여줘”에 실제 수집 기록으로 답변.
   추가 AI 호출/POST 실행 없음. 반환을 명령 성공으로 간주하거나 미관측 원인/스킬명을 생성하지 않는다.
7. 관련 작업과 기존 인증된 원본 로그 화면 연결. 폰에서는 선택한 상세로 이동하고 지도로 돌아갈 수 있음.
8. 실시간 갱신 중 로그/대화/관련 작업/상세 버튼의 키보드 포커스와 열린 상세 영역을 유지.

호출 기반 입장·방당 6명·오래된 호출 교체·기록 정렬·말풍선·움직임 중지·동작 줄이기·숨김 복귀는 유지한다.
프레임은 여전히 `sandbox="allow-scripts"`인 opaque origin이며 관리자 토큰/승인 DOM/일반 API 권한을 받지 않는다.

## 변경 파일과 원본 WIP 보존

운영 후보 파일 6개:
- `chatgpt-mcp/admin.html`, `admin.js`, `admin.css`
- `work-dashboard/model.js`, `visualizer.js`, `visualizer.css`

검증/문서:
- `work-dashboard/tests/character.test.js`
- `chatgpt-mcp/tests/check_admin_ui.py`, `test_operating_dashboard_ui.mjs`
- `DESIGN.md`, 해당 spec/plan, 이 인계 문서

원본 `admin.html`에는 다른 세션의 `runtime-rollout-admin.js`와 런타임 승인 폼 WIP가 있다.
원본에는 UI 수정 패치만 적용하여 이 스크립트/폼을 보존한다. 다른 파일은 시작 시 저장한 SHA-256과
현재 내용을 비교한 뒤 이번 소유 파일만 반영한다. 원본 전체를 설치 소스로 사용하면 Schema 014와
별도 런타임 롤아웃 WIP가 섞이므로, 승인 후 배포는 위의 검증된 격리 소스를 기준으로 해야 한다.

## 검증 근거

- Node: 187/187 통과 (기존 181 + 기록 기반 직업/대화 6개).
- 관리 서버 Python: 25/25 통과.
- 브라우저: 최종 전체 87/87 통과 (117.242초). 직접 pause 상태와 포커스 회귀를 포함한다.
- 검토: 독립 code-reviewer가 반환/미확인/지연 의미, 프로젝트 격리, 실제 객체 identity, 6명 정원,
  iframe 권한 경계와 로그 연결을 확인. 지적한 키보드 포커스 유실은 재현(RED) 후 수정(GREEN).
- 원래 보고된 patch 지연은 최종 patch 재생성 및 `git apply --check --reverse`로 해소.
- JavaScript 구문 검사, Python Ruff, 변경 파일 `git diff --check` 통과.

Chromium은 실제 HTTP 관리 앱을 임시 프로젝트/state로 실행했다. 운영 UI에 신기능을 적용했다고 주장하지 않는다.
확인한 뷰포트: 1440x900, 390x844, 320x844. 지도 시작 y는 약 410/417/453px,
페이지 높이 1245/1604/1640px, 문서 가로 넘침 0, 브라우저 JS 오류 0.
폰은 고정 하단 시트 대신 같은 프레임의 상세로 스크롤/포커스하며, 별도 부모 메시지/API는 추가하지 않는다.

호스트 전용 근거: `~/.local/state/personal-ops-dashboard/dashboard-rpg-20261016/`
- `original-baseline.json`, `source-baseline.json`: 반영 전 파일 해시/모드
- `review.patch`, `integrate-admin.patch`: 최종 검토/원본 병합 범위
- `candidate.json`, `candidate-manifest.json`: 설치하지 않은 후보와 실행 중인 버전
- `browser-final.log`, `node-final.log`, `admin-python.log`, `focus-red.log`, `focus-green.log`
- `browser-observations.json`, `workspace-*.png`, `character-*.png`, `failure-desktop.png`

## 구현 당시 후속 계획 (운영 반영 결과는 아래 절 참조)

1. 운영 반영에는 별도 서비스 전환 승인이 필요하다. 후보는 위 73개 파일이며 변경은 화면 6개뿐이다.
   배포 직전에 현재 프로세스·manifest·동시 세션·권한 상태를 다시 확인하고 영향/롤백을 기록할 것.
   기존 번들 전환 방식은 MCP/관리 웹 연결을 잠시 끊을 수 있으므로 무승인 실행하지 않는다.
2. 승인 후 격리 소스 후보로 preflight/rollout을 수행하고 실제 HTTP 자산 해시와 LAN/폰 UI,
   프로젝트 변경·캐릭터 대화·원본 로그 연결을 확인한다. 현재는 신기능의 운영 검증 미실시.
3. 2차: 명시적 스킬 식별/시작/종료 이벤트 수집과 발동 효과. 지금 동작 표시는 도구/행동 수준이다.
4. 3차: 외형/직업 설정 저장, 실제 프로필/장비 변경, 추가 AI 대화/작업 요청 연결. 이번에 구현하지 않음.

## 최종 확인

- 최종 격리 소스: 브라우저 87/87, Node 187/187, 관리 서버 Python 25/25 통과.
- 원본 선택 반영: 13개 파일. 그 외 기존 304개 파일의 SHA-256·모드 동일, HEAD/index 유지.
- 원본의 별도 런타임 승인 스크립트·폼을 보존한 상태로 Node 187/187와 브라우저 핵심 6/6 재검증 통과.
- JS 구문·Ruff·diff 공백 검사 통과. 최종 review.patch 역방향 적용 검사 통과.
- 선택한 작업/로그 버튼의 포커스와 펼친 기술 세부 항목이 상태 변화 후 유지됨을 실제 브라우저로 검증.
- 남은 작업은 승인 후 운영 전환 및 실제 운영 데이터 UI 검증, 그리고 별도 2·3차 범위이다.
- 원본의 추가 WIP는 이번 후보에 포함하지 않았다. 서비스 전환·DB/Docker·권한 변경·커밋은 없음.


## 운영 적용 완료

적용 시각: 2026-10-16T01:49:57+09:00. 사용자 후속 “진행” 지시를 배포 승인으로 기록했다.
운영 MCP/관리 웹은 **cc1631a8718e788b9be5**다. 이전 41339f4dc09de5363e0d로 되돌릴 수 있도록 불변 snapshot을 보존했다.

- 실제 필수 619/619, 전체 784/784 통과 후 기존 번들 전환을 수행했다.
- 실제 server.py PID 3942758, admin_server.py PID 3942903이며, 터널 healthy / admin_ready / runtime_verified=true.
- 설치된 73개 파일의 owner·0600·SHA와 loopback/LAN HTTP 20건이 후보 manifest와 일치한다.
- 실제 personal-ops / tossApp / pim-check에서 기록 기반 말 걸기 12건과 원본 출력 3건을 확인했다.
- 실제 1440/390/320px의 다섯 탭 15건에서 문서 가로 넘침 0, JS 오류 0, 로그인 후 변경 요청 0. 데이터 조회 완료 후 캡처한 화면도 직접 시각 검토했다.
- 실제 ChatGPT personal-ops-tunnel read_file 호출이 성공했다. 이번에는 새 MCP 도구 정의를 추가하지 않았다.
- 원본 317개 파일·HEAD/index, 보호 설정 5개, 최신 액세스 기록 2개와 독립 CLI 세션 3개를 서비스 전환 전후에 보존했다.
- 전환 직전 13개 프로젝트의 활성 호스트 작업/조회 오류는 0이었다. 정확히 터널이 소유한 Codex app-server들만 서비스와 함께 교체했다.
- 이번 문서 3개만 별도 해시 비교/백업 후 갱신하며, 다른 세션 WIP와 DB/Docker/스키마는 변경하지 않았다.

### 실행 중 관측 및 복구

첫 번째 적용 시도는 검사가 모두 통과한 뒤 HOST_TTY_REQUIRED로 중단됐으며 서비스 변화는 없었다.
RTK proxy 아래 Python의 stdin은 TTY지만 stdout/stderr는 파이프인 것을 직접 확인했다.
`rtk proxy script -q -e -c '<기존 Python rollout --apply 명령>' /dev/null`로 실제 PTY와 입력 왕복을
확인한 뒤 동일한 배포 도구로 재실행했다. 테스트/승인/원본 receipt를 우회하거나 덮어쓰지 않았다.

검사 도중 다른 경로에서 personal-ops 호스트 액세스가 새로 승인되었다. 초기 guard가 이 변경을 탐지해
전환 전에 멈췄다. 그 외 소스·설정·프로세스가 동일함과 갱신된 승인 기록의 유효한 형식을 확인하고,
새 기준선을 별도 파일로 고정했다. 초기 기준선과 변경 탐지 기록은 보존한다. 배포가 승인 기록을
갱신·재바인딩·확대하지는 않았다.

### 남은 운영상 조건

현재 personal-ops의 만료형 호스트 실행 액세스는 **active=false / RUNTIME_CHANGED**다.
관리 화면 admin.html/js/css는 기존 정책상 권한 identity에 포함되므로, 이번 변경 후 실행 권한은
기존 관리 화면의 소유자 승인 절차로 다시 허용해야 한다. 기록 기반 대시보드와 일반 MCP 파일 조회는
정상 동작한다. 이번 배포가 권한 갱신까지 승인한 것으로 간주하지 않았고, 자동 재발급하지 않았다.

Passive MCP catalog는 계속 awaiting_discovery다. 원본 receipt의 exit 2 / verification_required 및
 tools_registered=false를 ready로 덮어쓰지 않는다. 실제 runtime/admin/connector read 성공과 전체
catalog/schema 관측을 구분한다. 도구 정의가 바뀌지 않아 이를 이유로 서비스 전환을 반복할 필요는 없다.
기존 대시보드 주소에서 새로고침하면 새 화면이 열린다.

앞선 “후속 작업” 중 1·2(승인 후 배포·실검증)는 완료했다. 이후 범위는 필요 시 호스트 실행 액세스 재승인,
2차 스킬 이벤트 수집, 3차 외형/장비 설정과 실제 AI 대화다.

### 운영 증거

`~/.local/state/personal-ops-dashboard/dashboard-rpg-20261016/activation/`:
- approval-scope.json, guard-before.json, concurrent-grant-observation.json
- guard-before-latest.json / guard-confirm-latest.json / guard-after-latest.json
- rollout/rollout-ryrxj8l0/receipt.json (첫 TTY 실패 원본)
- rollout/rollout-c9_pzknn/receipt.json (실제 적용 원본)
- tty-recovery.json, live-verification.json, live-connector-read.json, access-after.json
- live-viewport-*.png, live-character-*.png, documentation-backups/, final-observations.json


## 호스트 실행 액세스 재승인 (2026-10-16 09:09 KST)

사용자의 후속 “승인” 지시로 personal-ops 호스트 액세스를 현재 런타임에 다시 승인했다.
기존 승인과 같은 4시간·Codex 허용 설정이며, 기존 request_host_access 도구로 새 요청을 만든 뒤
loopback의 기존 host-access-approve API에 정확한 요청 ID·프로젝트 확인·새 관리자 토큰 인증을 전달했다.
서버 정책이나 grant 파일을 직접 수정하거나 우회하지 않았다.

- 실제 플러그인 get_host_access에서 active=true, reason=null, allow_codex=true 확인.
- 만료: 2026-10-16 13:09:11 KST. 이 시각 이후에는 만료 정책을 따른다.
- 런타임 cc1631a8718e788b9be5 및 MCP/관리 웹 PID 3942758/3942903 유지.
- 보호 설정 5개와 다른 프로젝트의 승인 기록을 보존했다. 명령·모델 호출은 시작하지 않았다.
- 앞선 RUNTIME_CHANGED/EXPIRED 관측은 이전 승인에 대한 기록이다. 이번 재승인으로 해소됐다.
- 호스트 전용 증거: /home/jhw/.local/state/personal-ops-dashboard/dashboard-rpg-20261016/host-access-renewal-20261016T000848Z/before.json, approved.json.

## 추가 사용자 피드백: 작업 내용을 설명하지 못하는 상세 화면

2026-10-16 사용자는 호스트 실행기 캐릭터를 클릭해도 작업 요약·명령 실행·“지금 뭐 해?”·
“마지막 결과는?”에 “호스트 실행”과 일반 실행 상태가 반복되어 실제 작업을 파악하기 어렵다고 보고했다.
1차 기능 구현·배포 완료와 별개로, 실제 업무를 설명하는 품질은 미완료다.
스킬 발동 효과·외형/장비·자유 AI 대화보다 먼저 개선할 최우선 후속 항목으로 기록한다.

코드 확인 결과:
- `chatgpt-mcp/host_execution.py`의 `_public`은 실행 목적이나 명령 설명을 제공하지 않는다.
- `work-dashboard/model.js`의 `normalizeHostExecutions`는 “호스트 실행 <ID>”를 제목으로 만든다.
- `workSummary`와 `recordedAnswer`는 그 일반 제목을 재사용한다. iframe 브리지도 승인된
  표시 필드만 전달하므로 프론트엔드 문구 변경만으로 실제 목적을 복구할 수 없다.

개선 기준:
1. 캐릭터 정체성(호스트 실행기)과 업무 제목·대상 프로젝트·현재 동작·종료 결과를 구분한다.
2. 작업 요약은 목적과 대상, “지금 뭐 해?”는 관측된 동작/단계와 확인 시각,
   “마지막 결과는?”는 어떤 완료 작업의 어떤 결과인지와 원본 로그 진입을 보여준다.
3. 예시 표시: “tossApp API·Worker 재빌드” / “빌드 실행 중” / “이전 typecheck 성공, 종료 코드 0”.
   이는 표시 설계 예시이며 실제 현재 작업에 대한 관측 결과가 아니다.
4. 실행 목적·안전한 명령 설명의 수집/전달이 필요하다. 원시 인자·환경변수·프롬프트·비공개 로그를
   지도 프레임에 전달하지 않는다. 없는 목적/단계/원인은 미수집으로 밝힌다.
5. 서로 다른 실제 업무가 서로 다른 설명으로 나타나는지, 현재 실행과 직전 완료 결과를 구분하는지,
   비밀값·프로젝트 격리·폰/데스크톱을 함께 검증한다.

이번에는 요구사항과 코드 조사 결과만 기록했다. 제품 코드·실행 권한·서비스를 변경하거나
진행 중인 호스트 작업을 조작하지 않았다. 설계 기준은 `DESIGN.md`의 Priority follow-up에 있다.

### 작업 설명 개선 후속 구현

이후 사용자 “진행” 지시로 공개 작업 목적·안전한 명령 설명·관측 단계와 현재/이전 결과를
구분하는 소스 수정을 진행했다. 위 조사 기록은 당시 상태이며, 새 구현의 검증·적용 여부는
[작업 설명 개선 인계](2026-10-16-dashboard-task-context.md)를 참조한다.
이 후속 작업은 기존 호스트 작업을 조작하거나 서비스를 재시작하는 승인을 포함하지 않는다.

후속 운영 반영: 이후 명시적인 사용자 “승인”으로 2026-10-16 12:17:35 KST에 runtime
`dfc699ead12d7bcc378e`를 설치·전환했다. 실제 UI·새 설명 인자 등록·기존 세션/설정 보존을
검증했다. 이전 설명 없는 기록의 목적은 미수집으로 남으며 새 작업에는 task_label을 제공해야 한다.
ChatGPT 도구 정의 새로고침과 HostAccess 재승인은 사용자 단계다. 상세 receipt와 실패/복구
기록은 위 작업 설명 개선 인계의 “승인 후 실제 운영 전환” 절을 참조한다.
