# Dashboard RPG Stage 2 — 스킬 발동 표시

> 2026-10-20 후속 검증: 현재 MCP의 skill_id/skill_name 실제 보고가 accepted=1 / provenance=reported로 성공했고, 운영 대시보드에서 항목을 클릭해 스킬 식별자와 출처 표시까지 확인했다. 아래의 connector 캐시 차단 기록은 당시 이력이다. 최신 상태와 남은 범위는 [종료 점검](2026-10-20-personal-ops-closeout.md)을 참조한다.

작성: 2026-10-19. 기준 HEAD: `bcab942717c72c8a3108488570ae3f6ad8a5efbf`.

## 결과와 적용 범위

기존 캐릭터에 명시적인 스킬 호출의 이름·출처·상태·룬·발동 효과를 연결했다. 별도 캐릭터나 모델 호출을 만들지 않으며, 기존 방 정원·입장 순서·대기·기록 재생을 유지한다. 실행 캐릭터와 검토·검색·사고 역할 캐릭터에 동일하게 적용한다.

코드와 격리 검증은 완료했다. 운영 서비스 전환, 후크 설정 변경, 커밋·push는 수행하지 않았다. 원본에 병행 중인 Toss 콘솔 UX019 및 호스트 전원·재부팅 관련 작업이 있으므로 전체 checkout을 그대로 배포하지 않는다.

## 이벤트 계약

- 선택 필드 `skill_id`: ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}`.
- 선택 필드 `skill_name`: 출력 가능한 1~80자 표시명. `skill_id`가 필요하다.
- 두 필드는 `task_kind=call`에만 허용한다. 일반 agent/job 작업의 성공을 스킬 완료로 표시하지 않는다.
- 기존 v1 이벤트와 도구 이름은 유지한다. 새로운 MCP 도구를 추가한 것이 아니라 `report_work_events` 입력 스키마를 확장했다.
- 동일 호출은 source/actor/task ID로 연결하며 다음 호출은 새 task ID를 사용한다.
- 같은 조회 범위의 시작 기록에만 스킬 메타데이터가 있어도 종료 기록에 상속한다. 시작 기록이 최근 200개 범위에서 사라지면 메타데이터를 무기한 복원하지 않는다.
- 같은 호출의 스킬 ID가 충돌하면 스킬 표시를 생략한다.
- 스킬 출처는 별도로 보존한다. 보고된 스킬 뒤에 일반 관측 기록이 와도 보고 표식을 유지한다.
- 인자·프롬프트·응답·스킬 본문·경로·내부 추론을 수집하지 않는다.

Claude의 정확한 `Skill` 도구와 유효한 `tool_input.skill`만 관측 식별자로 추출한다. PreToolUse는 호출 시작, PostToolUse는 호출 반환, 실패 후크는 실패/중단이다. 반환은 지시된 전체 업무의 완료를 뜻하지 않는다.

[Claude 후크 문서](https://code.claude.com/docs/en/hooks#userpromptexpansion)에 따르면 직접 입력한 /skill 경로는 Skill 도구 후크를 우회할 수 있다. 이 경로는 이번 수집에 포함하지 않는다. [스킬 권한 문서](https://code.claude.com/docs/en/skills#restrict-claudes-skill-access)의 입력 매개변수 형식을 근거로 식별자만 추출했다.

Codex 관리 세션 관측기는 개별 스킬을 수집하지 않는다. SKILL.md 읽기, 도구명, 카탈로그나 프롬프트 언급만으로 발동을 추정하지 않는다. ChatGPT/Codex의 명시적 report_work_events/HTTP 입력은 보고됨이다. 실제 설치된 CLI가 스킬 후크를 발생시키는지와 호스트 후크 활성화는 검증·적용하지 않았다.

## 표시와 보호 조건

새 호출의 발동 효과는 기존 말풍선 행이 실제 표시될 때 한 번만 실행한다. 입장 대기 중에 효과가 소모되지 않는다. fresh 활성 호출만 약한 룬 동작을 표시하며 반환·실패·시간 초과·취소·중단·미확인을 구분한다.

초기 이력, 반복 조회, heartbeat, 역행 이벤트, 숨김 복귀와 수동 기록 재생은 새 발동이 아니다. 정지·동작 감소·관측 오류·프로젝트 전환·destroy에서 효과를 정리한다. 스킬 작업을 선택하면 해당 호출의 이름·출처·상태·식별자와 기존 상하위 관계를 확인한다. 캐릭터와 업무 보드의 접근성 이름에도 스킬 이름과 출처를 넣는다.

인증은 부모에 남으며 opaque iframe과 source/origin/epoch 검증을 유지한다. bridge에는 두 선택 필드만 추가했다. 문자 출력은 textContent와 기존 텍스트 노드를 사용한다.

## 최종 검증

- Python: 1,004개 실행, 1,003개 통과, 실제 Codex 격리 probe 1개 opt-in 제외.
- JavaScript/bridge: 240개 통과.
- 실제 Chromium 관리 웹: 108개 통과. 모든 case의 pageerror 검사 포함.
- 실제 격리 MCP SDK의 tools/list 스키마와 report_work_events 저장 왕복 통과.
- call-only schema는 실제 SDK에서 받은 JSON Schema를 Draft202012Validator로 검증했다.
- 격리 HTTP 저장 → API → 허용 필드 bridge → iframe → 캐릭터/상세 경로 통과.
- PC 1440, 모바일 390/320에서 기록·룬·상세 확인, 부모/프레임 가로 overflow 없음.
- 정상 동작의 실제 WAAPI 발동과 CSS 룬 애니메이션, 입장 대기, 반복 조회, 역할별 표시, pause 정리 통과.
- Ruff, JavaScript 구문 검사, diff 검사, Gitleaks(탐지 0) 통과.
- 검증 중 225개 소스·검사 파일의 hash가 유지됐고, 후보 runtime 88개 파일의 hash·소유자·0600을 확인했다.

브라우저 입력은 임시 상태와 테스트 서버의 합성 메타데이터다. 실제 ChatGPT 클라이언트에서의 운영 스킬 발동이나 CLI의 실제 스킬 후크 발행을 증명하지 않는다. 테스트 HTTP의 종료 중 BrokenPipe 경고는 로그에 보존했으며, 검사는 통과하고 브라우저 pageerror는 없었다.

## 실패와 검토 기록

초기 신규 브라우저 검사는 기존 스킬 표시가 없어 실패했다. 독립 검토에서 call-only 범위 누락과 reported 스킬의 표시 출처 누락을 찾아 수정했다. 업무 보드 접근성 이름과 역할 캐릭터의 룬·발동 효과 누락도 실패 검사 후 수정했다.

초기 전체 Python 명령은 저장소 루트에서 실행해 test_admin_agent_approval의 기존 import 경로 조건으로 실패했다. 올바른 chatgpt-mcp cwd에서 전체 재실행해 위 최종 결과를 얻었다. README 검증 명령도 맞췄다.

입장 검사 초기 fixture의 호출 순서·선택자·말풍선 충돌 가정을 수정했다. 최종 실제 pending/visible/중복 방지 검사는 통과했다. 처음 appended fixture의 모바일 screenshot은 iframe 밖을 캡처하여 시각 검증 근거에서 제외했다. 실제 임시 HTTP 데이터로 캡처한 skills-http-*.png가 최종 근거다.

포장 helper의 결과 파일명과 실행기의 기록 파일명이 한 번 충돌했다. 포장은 완료돼 있었으며 별도의 package-final-verification에서 재검증했다. 실패 로그는 덮어쓰지 않았다.

검토 보고서의 최초 REQUEST CHANGES와 최종 재검토를 각각 별도 0600 파일로 보존한다.

## 배포 후보와 현재 운영

후보 snapshot: `a6a788b75b5b900a6ac1` (88 files).

후보 위치:
`/home/jhw/.local/state/personal-ops-dashboard-skills/20261019-k8181lsj/packaged/a6a788b75b5b900a6ac1`

검증한 소스:
`/home/jhw/.local/state/personal-ops-dashboard-skills/20261019-k8181lsj/candidate`

읽기 전용 live alignment 결과:

| Component | Snapshot | PID |
|---|---|---|
| Tunnel process | d39917b42f6a1bb504da | 2038 |
| MCP child | d39917b42f6a1bb504da | 2068 |
| Admin | d39917b42f6a1bb504da | 3330 |
| Observer | a5827f53701d19619a9c | 1412435 |

터널 profile/service 경로 정합성과 관리 웹 preflight가 통과했다. 터널 user unit은 background wrapper라 active/exited이며, 실제 tunnel/MCP 자식의 PID·시작 시각과 설치 manifest로 실행 버전을 확인했다. observer와 admin은 active/running, 재시작 횟수 0이었다.

현재 admin/tunnel manifest와 후보의 차이는 10개 파일이다. 스킬 관련 7개 runtime 파일과 앞서 승인·적용한 observer 복구 코드 외에, 기존 HEAD의 host_db_migration.py 끝 빈 줄 제거 및 runtime_rollout_worker.py 주석/noqa 변경 2개가 포함된다. 뒤의 두 차이는 실행 로직 변경이 아니다. DB 작업은 실행하지 않았다.

backend/schema/bridge가 바뀌므로 authority digest가 달라진다. 배포 시 기존 HostAccess를 자동 연장·재바인딩하지 않고 새 런타임에서 재승인한다. 이번 단계에서는 권한 레코드를 변경하지 않았다.

## 병행 WIP 보존과 운영 전환

원본 server.py에 다른 세션이 추가한 ux019_provider_factory 연결을 baseline 비교에서 감지했다. 원본 변경을 덮어쓰지 않고, 공통 기준 HEAD·현재 원본·이번 후보를 대상으로 충돌 없는 3-way 결과를 준비했다. 나머지 Toss 콘솔 파일, 테스트, 전원 관리 스크립트와 BIOS/재부팅 문서는 수정하지 않는다.

운영 후보는 이 UX019 WIP를 포함하지 않는다. 따라서 원본 전체 checkout에서 rollout 명령을 실행하면 이 문서의 검증 범위를 벗어난다. 승인 후에는 검증한 private candidate의 rollout-host-runtime.py --apply를 사용하고, target snapshot이 위 후보와 일치하는지 확인한다. canonical rollout의 필수 검사는 그 시점에도 생략하지 않는다.

실행 전에 live runtime이 위 d399 기준에서 바뀌었는지 재확인한다. 다른 세션이 새로운 runtime을 이미 적용했으면 이 후보로 되돌리지 말고, 새 운영 기준에 이번 변경을 재통합·검증한다.

서비스 전환은 사용자가 앞서 요구한 “실제 서비스 전환은 영향 검토 후 별도 승인” 조건으로 남긴다. 예상 변경은 검증 runtime 설치, tunnel profile/unit 경로 갱신, 기존 터널과 인식된 관리 웹의 재연결이다. observer 서비스, Codex/Claude 독립 세션, 토큰/키, Docker/DB·스키마·AIT·업로드·프로필은 변경하지 않는다. 호스트 후크 활성화도 별도 범위다.

## 근거 위치와 다음 작업

비공개 evidence:
`/home/jhw/.local/state/personal-ops-dashboard-skills/20261019-k8181lsj`

final-python.json/log, final-node.json/log, final-admin-browser.json/log, final-secrets.json, final-integrity.json, package-preflight.json, live-alignment.json, skills-http-captures.json, skill-backend-results.md, frontend-review-fixes.md, skill-backend-review*.md, shared-server-merge.json 및 integration 결과를 참고한다.

남은 작업은 별도 승인 후 운영 전환과 실제 사용하는 클라이언트·collector 경로의 스킬 표시 확인이다. 문서의 테스트 성공을 운영 적용 성공으로 바꾸어 보고하지 않는다.


## 원본 반영과 최종 검토 완료

17개 파일을 원본 checkout에 제한 반영했다. server.py는 UX019 연결을 유지하는 3-way 결과를 사용했다. 반영 직전/직후 342개 보호 파일이 유지됐고, 기존 observer 복구 수정·인덱스도 유지됐다. 다른 세션의 새 Toss 콘솔/전원 관리/재부팅 작업은 이번 변경으로 쓰지 않았다.

원본 반영 후 이벤트·Claude 후크 회귀, 전체 JavaScript 240개, 변경 Python Ruff/AST, JavaScript 구문, diff 검사가 통과했다. 원본은 병행 WIP를 포함하므로 위 전체 Python/Chromium 결과의 대상은 검증한 private candidate다. 원본 server.py가 이 후보와 다른 UX019 연결을 갖는 것은 의도된 병합 결과다.

최종 독립 검토는 APPROVE, 남은 지적 0건이다. 별도 보고서 skill-final-review.md SHA-256:
8a50104e499683a590f7a7bfebcf0bccc904ff4b998944f7d82a750b6e209e6b

통합 결과는 integration-result.json, 원본 확인은 root-integration-check.json/log에 보존했다. 이 문서의 완료 기록 추가는 별도 receipt로 남긴다. 운영 전환과 커밋은 여전히 미실행이다.


## 승인 후 운영 반영 — 2026-10-19T14:22:58.002531+09:00

최신 운영 runtime은 a6a788b75b5b900a6ac1이다. 사용자 승인을 받아 private candidate의 canonical rollout을 실행했다. 원본 checkout 전체를 배포하지 않았으며 다른 세션의 UX019·전원 관리 WIP를 포함하지 않았다.

첫 실행은 rtk proxy가 stdout을 캡처해 HOST_TTY_REQUIRED로 전환 전에 종료했다. 필수 검사는 통과했고 설치·서비스 전환은 없었다. 실패 receipt를 보존했다. util-linux script로 실제 내부 PTY를 할당한 뒤 stdin/stdout/stderr의 isatty와 입력 왕복을 먼저 확인하고 canonical 절차를 다시 실행했다. 터미널 검증이나 검사를 우회하지 않았다.

재실행의 필수 검사 6단계가 모두 통과했다. 실제 입력 직전에 target a6a788b75b5b900a6ac1, 기존 운영 d39917b42f6a1bb504da와 tossApp 실행 중 명령 작업 0개를 다시 확인한 후 ROLLOUT을 입력했다.

운영 증거:
- 설치된 88개 파일의 hash·소유자·0600과 실제 MCP 프로세스의 설치 경로를 확인했다.
- Tunnel PID 2177979, MCP PID 2177997, Admin PID 2178163. profile/service 경로가 새 runtime과 일치한다.
- Admin active/running, observer 기존 PID 1412435·unit 유지, 재시작 횟수 0이다.
- canonical 관측의 독립 CLI 8개 모두 유지, 변경 0, 관측 누락 없음.
- config 13개 내용 hash 유지. 원본 index 유지. 수행 중 달라진 test_server_shutdown.py는 다른 세션 WIP로 남겼고 이번 배포가 쓰거나 복원하지 않았다.
- 실제 loopback/LAN asset 18개 응답과 opaque iframe CSP를 검증했다.
- 실제 MCP read_project_progress 호출이 정상 응답했다.
- 실제 설치 snapshot을 별도 임시 stdio 세션에서 열어 skill_id/skill_name 등록 및 call-only JSON Schema를 확인했다. 실제 터널의 소유된 stdio에 임의 JSON-RPC를 보내지 않았다. 모델을 호출하지 않았다.
- 운영 LAN 관리 웹에서 1440/390/320 화면, 스킬 룬의 활성 애니메이션, 정확한 호출 상세·보고됨 출처를 확인했다. 가로 overflow와 브라우저 pageerror는 없었다.
- 이 에이전트가 실제 수행한 운영 배포 검증을 명시적 reported 이벤트로 기록했다. 현재 채팅의 새 필드 호출이 캐시에서 막혀, 기존 인증 HTTP metadata API로 시작·반환을 보고했고 API 왕복 2개 기록을 확인했다. 합성 업무를 운영에 주입하지 않았다.
- 새 브라우저가 기존 기록을 읽었으므로 운영의 일회성 시작 burst는 관측하지 않았다. 초기 이력을 새 발동으로 재생하지 않는 계약을 유지했다. burst 자체는 이전 격리 Chromium 검사에서 통과했다.

### 현재 남은 클라이언트 단계

canonical receipt는 exit 2 / verification_required / awaiting_discovery 그대로 보존했다. 설치·전환·런타임·관리 웹은 확인됐지만, 현재 채팅의 플랫폼 connector에는 이전 report_work_events 스키마가 남아 있다. 실제 호출에서 skill_id와 skill_name이 additionalProperties로 거부됐다. 호스트의 새 스키마 오류가 아니며, 연결 설정에서 플러그인 metadata를 새로고침한 뒤 새 필드 MCP 호출을 재확인해야 한다. 호스트 재배포나 재시작은 필요하지 않다.

플러그인 관리 도구와 tunnel-client 도움말에는 현재 채팅의 connector 스키마를 새로고침하는 기능이 노출되지 않았다. 권한 토글·삭제/재설치·무관한 플러그인 재발행으로 이를 강제로 갱신하지 않았다. 실패의 개인정보가 포함된 부가 metadata는 저장하지 않고 필드 경로와 오류만 기록했다.

현재 HostAccess는 active=false / EXPIRED다. 호스트 명령 실행이 필요하면 기존 승인 카드에서 새 권한을 승인한다. 이번 서비스 전환 승인을 HostAccess 발급·연장이나 Codex 허용 변경으로 해석하지 않았다.

Docker·DB·스키마·AIT 업로드·명령 프로필·키·호스트 후크·독립 Codex daemon은 변경하지 않았다. 커밋·push도 하지 않았다.

실행 근거:
/home/jhw/.local/state/personal-ops-dashboard-skills/20261019-k8181lsj/activation-x2vap4wt

activation-completion.json, post-runtime-verification.json, installed-schema-proof.json, live-assets.json, live-dashboard-http.json, http-skill-start.json, http-skill-returned.json, connector-schema-cache.json을 참고한다. 최초 실패 receipt와 최종 receipt는 receipts 하위에 각각 보존했다. canonical-tty.log에는 실제 터미널 기록을 남겼다.
