# 관리 대시보드 운영 화면 1차 개선 인계

2026-10-15. Personal Ops 관리 웹과 기존 2D 작업실의 실행 상태 연결·화면 구성을
개선했다. **사용자 승인 후 2026-10-15 14:34 KST경 운영 적용을 완료했다.**
사고 캐릭터를 추가한 3차는 **15:03 KST 운영 적용을 완료**했고 현재 런타임은
`1bf5d7d05cc5ca07d8e1`다. 아래 1~2차는 당시 기록이며, 최신 상태는 문서 마지막
3차 운영 적용 결과를 확인한다.
1~2차 소스 작업 당시 웹은 tossApp 작업을 계속했다. 해당 단계에서는 tossApp 소스, Docker, DB, 스키마,
서비스, 권한 설정을 변경하지 않았고 모델을 호출하지 않았다. 커밋·push도 하지 않았다.
업무 흐름 표시를 다듬은 2차 작업은 문서 마지막 절에 기록한다.

## 구현한 동작

- 기본 화면을 개요로 바꾸고, 개요 / 작업실 / 실행 / 접근·승인 / 저장소·보고의
  다섯 탭으로 정리했다. 기존 승인 폼의 ID·인증·Origin 검사·승인 링크를 유지한다.
  승인 링크는 접근·승인 탭으로 열리고 키보드 방향키·Home·End로 탭을 이동할 수 있다.
- 개요에서 현재 실행 수, 최근 실패, 권한 잔여 시간, 호스트 Codex 허용 여부와
  관측 상태를 확인한다. 호스트 Codex 허용과 기존 격리 프로필의 준비 상태를 구별한다.
- 기존 지도 인스턴스 하나를 개요/작업실에서 재사용한다. 다른 탭·숨긴 페이지·Follow
  일시정지에서는 지도 애니메이션도 기존 정책에 맞춰 멈춘다.
- 실제 `/api/host-executions`의 읽기 결과를 기존 jobs/activity/work-events와 연결한다.
  호스트 Codex의 프로필·operation ID를 기존 이벤트와 합쳐 동일 객체/작업의 중복을 막는다.
  일반 호스트 명령은 안정적인 `호스트 실행기` 객체로 표시한다.
- MCP의 `get_agent_task`를 채팅이 반복 호출하지 않아도 관리 웹의 관측으로 작업 상태를
  갱신한다. 시작 후 8분이 지난 실행도 새 관측이 있으면 실행 중으로 표시한다.
  마지막 성공 관측이 120초 이상 오래되면 미완료 작업은 상태 확인 필요로 바뀐다.
  실패한 읽기로 관측 시각을 새것으로 바꾸지 않으며, 알려진 완료 상태는 유지한다.
- `prepared`는 시작 전 대기로 표시하고 실행 수에 넣지 않는다. 더 최근의 이벤트를
  오래된 호스트 목록으로 덮지 않는다. 성공은 종료 시각·exit 0·종료 검증을 확인한다.
- 작업실의 호스트 작업에서 실제 호스트 실행 상세/로그로 이동한다. 기존 작업은 기존
  출력 검사기를 사용한다. 모델·추론 수준은 제공된 설정 출처로 표시하며, 미관측 추론,
  다른 MCP 내부 작업, 토큰 사용량이나 진행률을 만들어 표시하지 않는다.
- 일시정지 중에도 권한 만료 시 개요·접근 상세·철회 버튼을 함께 갱신한다. 이 갱신은
  API를 호출하지 않고 프로젝트 확인문·관리 토큰 입력을 보존한다.
- 320px/390px 화면에서 다섯 탭 모두 문서의 가로 넘침을 없앴다. 지도와 탭은 필요한
  경우 내부에서 스크롤하며 선택기·폼이 문서 폭을 넓히지 않는다.

이번 단계는 데이터와 조작 구조를 개선한 것이다. 새 스프라이트·방 그래픽과 더 풍부한
작업 액션은 후속 시각 개선으로 남긴다. 화면 계약은 루트 `DESIGN.md`에 기록했다.

## 변경 파일

| 범위 | 파일 |
| --- | --- |
| 관리 웹 | chatgpt-mcp/admin.html, admin.css, admin.js, host-access-admin.js |
| 작업실 | work-dashboard/model.js, visualizer.js |
| 기존 회귀 검사 | chatgpt-mcp/tests/check_admin_ui.py, test_host_access_ui.mjs |
| 새 회귀 검사 | chatgpt-mcp/tests/test_operating_dashboard_ui.mjs, work-dashboard/tests/host-executions.test.js |
| 설계·인계 | DESIGN.md, 이 문서 |

새 런타임 JS 파일이나 의존성을 추가하지 않았다. 기존 설치 자산에 있는 파일을 수정했으며
서버 Python·정책·설치기 소스는 이번 대시보드 변경 대상으로 삼지 않았다.

## 검증 결과

| 검사 | 관측 결과 |
| --- | --- |
| Node 전체 UI·작업실 검사 | 10개 파일, 113/113 통과 |
| 실제 Chromium 전체 관리 웹 검사 | 37/37 통과, 57.083초 |
| 브라우저 실행 중 소스 고정 | 대상 11개 파일의 전후 SHA-256 일치 |
| 관련 Python 서버·설치·호스트 권한 검사 | 37/37 통과 |
| JS 구문·Ruff·git diff --check | 통과 |
| 독립 코드 재검토 | 남은 필수 수정 없음, APPROVE |

브라우저 검사는 임시 프로젝트·상태와 HTTP 경계 fixture를 사용한다. 실제 모델/셸 작업을
시작하지 않는다. 장시간 Codex 실행→완료, 중복 제거, 호스트 로그 이동과 텍스트 안전성,
권한의 프로젝트 분리·만료·일시정지, 기존 편집/승인 흐름 및 모바일 폭을 검사했다.
최초 RED 재현과 수정 뒤 GREEN을 확인했다. 운영 권한의 실제 만료·철회 중 작업 중단까지
실환경에서 검증했다는 의미는 아니다.

실행 예시(구현 worktree 기준):

```sh
rtk proxy /home/jhw/.nvm/versions/node/v24.12.0/bin/node --test chatgpt-mcp/tests/*.mjs work-dashboard/tests/*.test.js
rtk proxy /usr/bin/python3 -B chatgpt-mcp/tests/check_admin_ui.py -q
rtk proxy /home/jhw/.local/bin/ruff check chatgpt-mcp/tests/check_admin_ui.py
rtk proxy git diff --check
```

검사 중 브라우저가 연결을 닫을 때 기존 HTTP 서버의 BrokenPipeError 진단이 일부 출력된다.
검사 실패나 페이지 JavaScript 오류는 없었으며, 이 별도 서버 진단 문제는 수정하지 않았다.
전체 770개 Python 검사와 롤아웃 preflight는 이전 권한 기능 배포에서 통과했으나
**이번 프런트엔드 단계에서는 전체 preflight를 다시 실행하지 않았다.**

증거 디렉터리:
`/home/jhw/.local/state/personal-ops-dashboard/dashboard-upgrade-review-20261015`

- `final-node-tests-2.log`: 최종 113개 결과.
- `browser-final-2.log`, `browser-final-2.json`: 최종 37개 결과와 소스/출력 해시.
- `observed-ui-ready.json`과 초기 스크린샷: 개선 전 실제 운영 화면 관측.
- `after/browser-observations.json`: 임시 데이터 화면의 320/390px 폭과 페이지 오류 검사.
  이 디렉터리 스크린샷은 최종 권한 표시 수정 이전의 중간 시각 자료이며 운영 화면이 아니다.

## 소스 반영과 보존 경계

구현 위치:
`/home/jhw/.local/state/personal-ops-worktrees/dashboard-upgrade-20261015`

원본:
`/home/jhw/ai/opencode/projects/personal-ops`

기준 HEAD는 `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`다. 기존 WIP를 복제해 구현하고,
원본 반영 전 기준선의 내용·모드를 비교한다. 다른 세션과 충돌한 파일은 덮어쓰지 않는다.
반영 결과와 변경 외 파일 보존 검증은 다음 private manifest를 확인한다:
`/home/jhw/.local/state/personal-ops-worktrees/dashboard-upgrade-20261015-integrated.json`

기준선:
`/home/jhw/.local/state/personal-ops-worktrees/dashboard-upgrade-20261015-baseline.json`

작업 중 운영 MCP PID 1852412/start_ticks 24934490, 터널 PID 1852395/start_ticks 24934485를
동일하게 관측했다. 실행 중 MCP는 여전히 다음 경로를 사용한다:
`/home/jhw/.local/lib/chatgpt-coding-mcp/releases/6cd2d5f148c7f65f4626/server.py`

`host-runtime.json`, `host-commands.json`, `projects.json`의 SHA-256 및 0600 모드를
기존 배포 기록과 대조해 일치를 확인했다. 운영 파일을 새 소스로 교체하거나 기존
Codex/Claude 세션을 종료하지 않았다.

## 웹에서 이어갈 때

1~2차 구현 직후에는 `:8765` 운영 화면에 적용하지 않은 상태였다. 아래는 당시 남겼던
운영 전환 절차다. 이후 별도 승인으로 전환했고, 현재 결과는 마지막 운영 적용 절을 따른다.

1. tossApp 작업과 기존 실행 현황을 먼저 확인한다. 새 변경이 추가되었으면 위 manifest와
   비교하며 이번 변경을 보존한다. 기존 WIP를 전체 덮어쓰거나 초기화하지 않는다.
2. 운영 적용 전 기존 배포 절차로 후보/preflight를 만들고 호스트 명령·일반 jobs·등록형
   작업·에이전트 등 활성 작업에 대한 영향을 검토한다. 실제 서비스 전환은 별도 승인 후 한다.
3. 런타임 해시에 호스트 권한이 묶여 있다. 새 전체 런타임으로 전환하면 기존 권한을
   계속 유효한 것으로 가정하지 말고 소유자의 기존 승인 경로로 필요한 권한을 다시 발급한다.
   새 admin만 바꾸고 기존 MCP와 다른 런타임 정체성을 섞지 않는다.
4. 적용 뒤 실제 운영 개요/작업실/로그·휴대폰 화면과 웹의 host 실행 관측을 확인한다.
   모델 호출·실행 시작은 관측 검사에 필수인 것으로 가정하지 않는다.
5. 이후 게임식 작업 액션/방 표현을 개선한다. 다른 MCP·CLI는 수집된 이벤트만 표시하고,
   추론 내용이나 관측되지 않은 모델 동작을 애니메이션의 사실로 표현하지 않는다.

웹에 전달할 문장:

> personal-ops의 docs/handoffs/2026-10-15-dashboard-operations-stage1.md와 DESIGN.md를
> 읽고 대시보드 작업을 이어가줘. 구현·검사된 소스와 운영 적용 여부를 구별하고,
> 기존 WIP와 tossApp 작업을 보존해줘. 운영 전환은 영향 검토·별도 승인 후 진행해줘.

## 2차: 두뇌·현재 업무·지시와 실행의 연결

사용자가 작업실과 캐릭터 상세만으로 업무 흐름을 이해하기 어렵다고 지적했다.
작은 모델 문구, 잘리는 작업 ID 말풍선, 기술 식별자 위주 상세를 다음처럼 개선했다.

- 작업실 위쪽에 픽셀 두뇌 카드와 현재 업무판을 배치했다. 실제 Agent의 모델·추론 수준은
  출처를 구분하고, ChatGPT 정보가 없으면 활동·모델 미수신 역할 카드만 표시한다.
  이 설명 카드는 actor/task나 진행 수를 늘리지 않는다.
- 직접 관측된 유효한 `reason` 활동만 ‘생각 중’으로 표시한다. 모델 설정, 호스트 프로세스
  실행, 오래된 활동으로 추론을 합성하지 않는다. 일시정지·조회 실패·미확인도 구별한다.
- 업무판은 담당자·행동·업무명·현재 상태를 먼저 보여준다. 진행·실행 전·대기 계획을
  완료 기록보다 우선하고, 미확인을 현재 활동으로 부르지 않는다.
- 상세는 쉬운 업무 설명과 이름 기반 상위·하위 이동을 먼저 제공한다. 실제 parent 관계만
  연결한다. 원본 ID·모델 출처·토큰·시간은 접힌 기술 정보에 보존했다.
- 새 호스트 관측이 실제 canonical 업무명·행동·상위 관계를 덮던 병합도 수정했다.
  프런트엔드 모델의 `hostActionKnown`으로 유효한 상세 신호를 구별한다. 프로세스가
  실행 중이어도 만료된 추론/행동 신호는 되살리지 않는다.
- 세부 신호가 없는 호스트 Codex는 ‘Codex 실행 · 세부 행동 미수신’으로 표현한다.
  캐릭터 소품·재생·완료/최근 변화 아이콘도 일반 실행으로 맞췄다. 코드 수정으로 꾸미지 않는다.
- 캐릭터 말풍선은 행동/업무 두 줄로 표시하고, 클릭한 업무판 항목의 포커스를 보존한다.
  선택 상세도 조회 실패·관찰 일시정지에 맞춰 갱신한다. 움직임 일시정지는 두뇌 효과까지 멈춘다.

변경 파일은 `DESIGN.md`, 이 문서, `work-dashboard/model.js`, `visualizer.js`,
`visualizer.css`, `work-dashboard/tests/host-executions.test.js`,
`chatgpt-mcp/tests/check_admin_ui.py`다. 새 의존성·런타임 자산·백엔드 API는 없다.

검증:

| 검사 | 2차 결과 |
| --- | --- |
| Node 전체 UI·작업실 | 115/115 통과 |
| 실제 Chromium 전체 관리 웹 | 44/44 통과, 64.082초, 검사 중 소스 해시 일치 |
| JS 구문·Ruff·diff 형식 | 통과 |
| 독립 검토 | 발견된 표시·관측 오류 수정 후 APPROVE |
| 화면 관측 | 1440px 및 390/320px, 문서 가로 넘침 없음, 페이지 JS 오류 없음 |

새 브라우저 검사는 무관측 두뇌, 실제 추론의 종료, 관측된 상위/하위 연결,
Host Codex의 상세 업무 보존과 일반 실행 소품/아이콘, 준비 계획의 우선 표시,
완료/대기/미확인/일시정지 구분, 조회 실패 시 선택 상세 갱신,
XSS 형태 작업명의 텍스트 표시와 포커스·모바일 폭을 확인한다.
첫 전체 검사 40개 중 기존 raw parent ID 기대 한 건이 실패했고, 기술 상세에 ID를
복원했다. 이후 전체 43개가 통과했고, 마지막 관측/시각 회귀를 추가한 전체 44개도 통과했다.
이 단계에서는 전체 Python/롤아웃 preflight를 재실행하지 않았다.

증거:
`/home/jhw/.local/state/personal-ops-dashboard/dashboard-flow-review-20261015`

- `red-observations.json`: 첫 표시 누락 3건 재현.
- `model-regression-observations.json`: 상세 action/parent 병합 회귀와 만료 경계.
- `node-final-3.log`, `browser-final-3.log/json`: 최종 검사 결과·소스/출력 해시.
- `before/`, `after-final/`: 임시 fixture로 구성한 초기/중간 시각 자료.
- `verified-view/`: 최종 소스의 화면과 해시. 일반 실행 소품까지 확인했다.
  모든 시각 자료는 임시 데이터이며 운영/실제 모델 실행 화면이 아니다.
- `protected-runtime-before.json`: 운영 PID/start_ticks·설정 해시 기준선.

기존 구현 worktree를 재사용했다. 2차 원본 기준선과 반영 manifest는 각각
`/home/jhw/.local/state/personal-ops-worktrees/dashboard-flow-20261015-baseline.json`,
`/home/jhw/.local/state/personal-ops-worktrees/dashboard-flow-20261015-integrated.json`이다.
원본 반영 시 내용·모드를 비교하며, 충돌이 있으면 덮어쓰지 않는다.
운영 배포·서비스 재시작·권한 재발급·Docker/DB·tossApp 작업은 이 단계에서 수행하지 않는다.

사용자는 웹 검색·설계·검토·정리를 한 사고 모델에서 역할 캐릭터로 나눌 수 있는지도 물었다.
한 ChatGPT 두뇌 아래 여러 역할 아바타를 표시하는 것은 가능하지만, 독립 에이전트가
여럿 추론하는 것으로 표현해서는 안 된다. 이번에는 기존 담당 객체를 유지했다.
별도의 역할 아바타는 후속 표시 작업으로 남겼으며, 역할 신호의 출처와 같은 두뇌 소속
표시를 먼저 정해야 한다. 웹의 실제 ChatGPT 모델·내부 사고는 자동 관측하지 못한다.

## 운영 적용 완료: 2026-10-15 14:34 KST경

사용자가 웹의 배포가 완료됐다며 진행을 승인했다. 변경 범위가 화면 파일 7개임을 확인하고,
기존 전체 런타임 배포 절차를 사용한다고 알린 뒤 적용했다. UI만 별도로 배포하는 경로를
새로 구현하거나 기존 검증 스냅샷의 파일을 덮어쓰지는 않았다.

- 1~2차 운영 적용 당시 런타임: **6deb3588da559085127f**, 70개 파일. 이전 6cd2d5f148c7f65f4626 및
  과거 릴리스는 보존했다. 변경은 admin.html/css/js, host-access-admin.js,
  work-dashboard/model.js/visualizer.js/visualizer.css의 7개다. 백엔드 Python은 동일하다.
- 전환 직전 11개 프로젝트의 jobs·호스트 실행·에이전트·고정 명령·등록형 작업을 조회하고,
  저장된 실행 기록 515개도 확인했다. 실행 중인 작업은 없었다. ROLLOUT 입력 직전 다시 확인했다.
- 배포 도구의 targeted **608개**, full **770개** 검사가 통과했다. 프로필과 서비스의
  기존 MCP 명령 정합성 및 소스 불변성을 확인한 후 사용자 승인에 따라 ROLLOUT를 입력했다.
- 터널 PID 2322199/start_ticks 25745938, MCP PID 2322236/start_ticks 25745946,
  admin PID 2322857/start_ticks 25746074를 관측했다. 터널 healthy, admin_ready다.
- 릴리스 70개 파일의 해시·0600 모드, 실제 HTTP로 제공되는 변경 자산 7개의 해시가
  후보 manifest와 일치한다. loopback/LAN의 관리 웹은 HTTP 200이다.
- 실제 운영 브라우저에서 personal-ops와 tossApp의 새 작업실·두뇌·업무판을 확인했다.
  390/320px의 다섯 탭, 총 10개 경우에 문서 가로 넘침이 없었다. 페이지 JS 오류 및
  로그인 후 변경 요청은 없었다. 실제 파일 저장·명령·모델 호출로 검증하지 않았다.
- 설치된 동일 릴리스를 별도 MCP SDK/임시 상태로 초기화해 **60개 도구**와 호스트 접근
  6개·기존 에이전트 5개 등록을 확인했다. 실제 ChatGPT 플러그인의 get_host_access도
  성공했다. 이 SDK 검사는 운영 터널 child와 별도의 세션이다.
- 터널 health의 수동 카탈로그 관측은 여전히 awaiting_discovery이고
  connector_schemas_verified=false다. 원본 롤아웃 receipt의 verification_required/exit 2는
  그대로 보존했다. 이를 서비스 전환 실패 또는 스키마 캐시 재검증 완료로 바꿔 기록하지 않는다.
  이번에는 MCP 도구 스키마를 변경하지 않았으며, 관리 웹 새 화면은 페이지 새로고침으로 확인한다.

기간제 권한은 런타임 정체성에 묶여 있어 기존 tossApp 권한은 현재
**active=false / RUNTIME_CHANGED**다. 실제 플러그인과 관리 API에서 같은 상태를 확인했다.
임의로 권한을 재발급하거나 만료 시간을 연장하지 않았다. 새 호스트 작업이 필요하면
기존 request_host_access → 소유자 승인 경로로 갱신해야 한다. 화면 조회에는 이 권한이 필요 없다.

호스트 설정 3개(host-runtime.json/host-commands.json/projects.json), 관리자 토큰과
런타임 키의 내용·모드는 동일하다. 배포 자체는 원본 저장소 290개 파일을 변경하지 않았다.
그 후 적용 기록을 남기기 위해 DESIGN.md와 이 문서만 갱신했다. 기준선의 Codex/Claude
17개 프로세스 중 16개는 같은 PID/start_ticks를 유지했고 기존 app-server 1개가 교체됐다.
별도 사용자 CLI 세션을 종료하는 명령, Docker/DB/스키마 변경, tossApp 배포,
실제 모델 호출, 커밋·push는 수행하지 않았다.

실행한 배포 명령:

```sh
rtk proxy /usr/bin/script -q -e -c 'env PYTHONDONTWRITEBYTECODE=1 /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-dashboard/dashboard-activation-20261015/rollout' /dev/null
```

증거 디렉터리:
`/home/jhw/.local/state/personal-ops-dashboard/dashboard-activation-20261015`

- `rollout/rollout-d69ntazw/receipt.json`: 필수 검사·후보 설치·전환·잔여 discovery 상태.
- `pre-switch-confirmation.json`, `activity-before.json`: 작업 없음과 전환 직전 재확인.
- `candidate-manifest.json`, `live-http-verification.json`: 후보·실제 제공 파일 일치.
- `live-browser-verification.json`, `live-workroom-*.png`: 실제 운영 브라우저 결과.
- `installed-sdk-verification.json`, `live-catalog-observation.json`: 각각 별도 SDK 등록과 수동 health 관측.
- `postdeploy-preservation.json`: 원본 파일·설정·키·프로세스 보존 확인(문서 갱신 전).
- `final-observations.json`: 문서 갱신 후 마지막 대조 결과.


## 3차: 지도 안 사고 역할 캐릭터 (소스 검증 완료)

사용자가 “사고는 분리해서 캐릭터로 표시못해?”라고 요청했다. 상단 카드와 별개로
지도 복도에 분홍 두뇌 표식을 가진 픽셀 인물을 표시한다. 기존 실행 캐릭터와는
같은 실제 담당자에 연결된 역할이며, actor/task/진행 수나 모델 호출을 추가하지 않는다.

- 실제 agent마다 사고 역할 하나를 유지하고, ChatGPT 관측 객체가 없으면 미관측
  ChatGPT 자리 하나를 표시한다. 프로젝트 변경 시 이전 역할·선택을 정리한다.
- 직접 관측된 유효한 active reason 작업만 ‘생각 중’과 사고 동작을 표시한다.
  reported 사고, 호스트 프로세스 실행·모델 설정, 미수신·대기·조회 지연·관찰 정지는 구별한다.
- 사고 역할을 누르면 담당자·실제 업무·출처·제공된 모델 정보를 확인하고 실제 업무로
  이동한다. 관측되지 않은 ChatGPT 내부 추론 내용이나 단계를 생성하지 않는다.
- 동시 code/reason 작업은 사고 역할에 reason을 우선 연결한다. 모델·추론 수준·토큰도
  선택한 일감의 telemetry를 사용해 다른 동시 작업의 값을 잘못 귀속하지 않는다.
- 모바일 선택 카메라는 실제 역할 위치를 따라간다. 복도를 지나는 실행 캐릭터는
  사고 역할보다 위에서 클릭할 수 있다. 미관측 자리만 있는 빈 지도는 렌더링 루프를
  유지하지 않는다. 수동 정지·Follow·reduced-motion·hidden 정책을 유지한다.

수정 범위는 work-dashboard/visualizer.js, visualizer.css,
chatgpt-mcp/tests/check_admin_ui.py, DESIGN.md 및 이 인계 문서다. 기존 격리 worktree를
재사용하고 원본 290개 파일의 내용·모드 기준선을 보관했다. 충돌 파일을 덮어쓰지 않는다.

검증: 브라우저 **50/50**(69.758초), Node **115/115**, Ruff·JS 구문·diff 검사 통과.
미관측 자리/상태 전이/보고 신호/Host Codex/동시 업무와 모델 분리/빈 지도 렌더링을
검사했다. 모바일 카메라·동시 업무·모델 귀속·빈 지도 루프는 실제 실패를 재현한 뒤
수정했다. 별도 읽기 전용 코드 검토 결과 APPROVE이며 필수 미해결 항목은 없다.

증거는 ~/.local/state/personal-ops-dashboard/dashboard-thinking-20261015/ 아래에 있다.
fixture-* 이미지는 임시 테스트 프로젝트 화면이며 실제 ChatGPT 사고 관측의 증거가 아니다.
라이브 이전 화면 live-before.png/json은 운영 6deb3588da559085127f에서 사고 캐릭터가
아직 없음을 확인한 기록이다. 원본 반영·운영 적용 결과는 아래에 후속 기록한다.

한계: 사고 역할 위치는 별도 구역에 고정되어 있으며 실행 객체의 방 이동과 함께 걷지
않는다. 웹 검색/설계/검토/정리 각각의 추가 역할 아바타와 ChatGPT 내부 사고 수집은
이번 단계에 포함하지 않았다. backend, 승인 정책, Docker/DB/tossApp은 수정하지 않았다.


### 3차 운영 적용 결과 (2026-10-15 15:03 KST)

앞서 승인된 대시보드 반영 범위에서 기존 전체 런타임 절차로 적용했다. UI를 별도로
배포하는 새 경로를 만들거나 검증된 이전 릴리스 파일을 덮어쓰지 않았다.

- 실제 런타임은 **1bf5d7d05cc5ca07d8e1**, 70개 파일이다. 이전
  6deb3588da559085127f는 보존했다. 기존 운영본과 다른 제품 파일은
  work-dashboard/visualizer.js 및 visualizer.css 두 개이며 backend/API/도구 스키마는 동일하다.
- 필수 targeted **608/608**, full **770/770** 검사 통과 후 프로필·서비스 정합성을
  확인했다. 전환 직전 11개 프로젝트의 jobs·호스트 실행·에이전트·고정 명령·등록형
  작업에서 활성 작업이 없고 조회 오류도 없었다. 활성 owner grant도 0개였다.
- 터널 PID **2441778**/start_ticks **25922940**, MCP PID **2441796**/**25922945**,
  admin PID **2441948**/**25923066**에서 새 릴리스 경로를 확인했다.
  릴리스 70개 파일의 해시·0600 모드, loopback/LAN HTTP 200 및 두 변경 자산의
  실제 HTTP 해시가 후보와 일치한다.
- 임시 fixture 화면과 별개로, 실제 personal-ops/tossApp의 초기 데이터 로딩이
  끝난 뒤 지도·사고 역할·선택 상세를 확인했다. 관측 당시 personal-ops는 실제
  agent 2개와 사고 역할 3개(ChatGPT 미관측 자리 포함), tossApp은 실제 agent 1개와
  사고 역할 2개였다. owner 중복은 없고 실제 actor/task 수에 역할을 추가하지 않는다.
- 운영 브라우저의 390/320px × 다섯 탭 **10개** 가로 넘침 검사 통과.
  모바일에서 선택한 사고 인물이 카메라 안에 남는다. JS 오류와 로그인 후 변경 요청은 0개다.
- 재연결 직후 실제 플러그인의 get_host_access 첫 조회는 Connection timed out이었다.
  동일 도구 재시도는 isError=false, project=tossApp, active=false,
  reason=RUNTIME_CHANGED로 정상 반환했다. 추가 서비스 재시작이나 권한 재발급은 하지 않았다.
- 배포 영수증은 passive catalog의 awaiting_discovery 때문에 exit 2 /
  verification_required를 유지한다. runtime_verified/installed/switched/tunnel_healthy는
  true, admin_ready다. 원본 영수증을 수정하지 않았다. 이번 변경에는 새 도구/스키마가 없고
  기존 도구 실제 호출까지 확인했다. 새 런타임의 전체 catalog 재발견을 확인한 것으로
  과장하지 않는다. 대시보드 자산은 새로고침으로 반영된다.
- 호스트 설정 3개와 admin-token/runtime-key의 내용·모드는 동일하다. 원본 반영 5개
  외 **285개 파일**과 HEAD를 보존했다. 커밋·push, Docker/DB/스키마·tossApp 변경은 없다.
  이전 CLI 관측 9개 중 8개는 PID/start_ticks가 동일하다. 나머지 app-server PID
  2322265에 대해서는 동일 명령 해시의 새 app-server 2441804가 확인됐다. 별도 사용자
  CLI 세션을 종료하는 명령은 수행하지 않았다.

최종 증거 디렉터리: ~/.local/state/personal-ops-dashboard/dashboard-thinking-20261015/
- baseline.json / integrated.json: 원본 기준선·반영 및 변경 외 보존
- browser-final-50.log / node-tests.log / validated-source.json: 검증 로그·검증 소스 해시
- fixture-visual.json / fixture-*.png: 임시 데이터의 사고 동작·모바일·복도 hit-test
- candidate.json / switch-gate.json / activity-at-switch.json: 운영 후보·전환 직전 확인
- rollout/rollout-rmvjayg1/receipt.json: 원본 배포 영수증
- live-verification.json / live-after-*.png: 초기 데이터 로딩 후 실제 운영 화면·HTTP·프로세스
- connector-verification.json / preservation-after.json: 실제 MCP 호출·설정/CLI 보존 관측

신호가 없는 ChatGPT 내부 사고는 계속 ‘활동 미수신’으로 표시한다. 사고 역할의 고정 위치,
추가 검색/설계/검토/정리 역할, 독립 UI 배포 및 passive catalog 재발견 확인은 후속 범위다.


## 4차 구현 계획: 역할·등장·퇴장 (진행 중)

목표: 사용자가 승인한 사고 역할 분리와 호출형 Codex 표시를 실제 이벤트·방 이동에 연결한다.
설계 정본은 DESIGN.md의 Stage 4 approved display contract다. 원본 290개 파일의
기준선을 dashboard-roles-20261015/baseline.json에 보관했다. 커밋·push·Docker/DB/tossApp
변경 및 외부 CLI hook 설정 변경 없이 기존 격리 worktree에서 검증한다.

- [x] 수집 계약: 선택적 activity_role/search_scope/tool_name을 schema·저장·hook에 보존.
  기존 이벤트 호환, WebSearch와 파일 검색 분리, 범용 web.run/임의 labels·입력 추정 금지.
- [x] 표시 모델: metadata를 task에 보존하고 동일 소유자의 역할 view를 만든다.
  실제 actor/task/count는 유지하고 모델·usage는 해당 task에 귀속한다.
- [x] 이동 엔진: 기존 sync/advance에 depart(ids,{animate})를 추가. 약 1초 결과 후
  복도·입구로 퇴장하고 소멸. 같은 ID 재시작은 퇴장을 취소, reduced-motion은 즉시 정리.
- [x] 지도: 고정 복도 station 제거. 역할/실행 객체를 같은 방 슬롯에서 표시.
  idle Codex와 무관측 역할 숨김, 신규 호출 입장, 완료 퇴장, 실패·미확인 attention 유지.
- [x] 검증: Python 저장·스키마·hook, Node 역할/이동/기존 회귀, 실제 브라우저 상태 전이·
  선택·overflow·320/390px 및 별도 코드 검토. 원본 충돌 비교 후 변경 파일만 반영.
- [ ] 적용: 운영 영향·활성 실행·권한·후보 hash를 확인하고 승인 범위의 기존 rollout 사용.
  새 선택 필드의 MCP 등록/보고 왕복과 실제 관리 웹을 검증하고 적용 여부를 기록한다.

구현 인터페이스: event는 snake_case(activity_role/search_scope/tool_name), model task는
camelCase(activityRole/searchScope)와 tool에 보존한다. Motion.depart는 로컬 표시 API이며
운영 작업을 실행하지 않는다. snapshot에서 퇴장 중인 항목에만 departing:true를 추가한다.


### 4차 구현·검증 결과 (운영 적용 전)

분석가·조사원·설계자·디버거·검토자·기록관 역할을 실제 작업 단계로 표시한다.
웹/파일 검색은 명시적인 search_scope로 구별한다. 같은 모델·소유자의 표시 역할이며
추가 모델/에이전트를 호출하지 않고 실제 actor/task/count 및 telemetry 귀속을 유지한다.
역할·기존 객체는 방당 여섯 자리를 공유하고 overflow 선택 시 해당 인물이 표시된다.
방의 숫자·필터는 역할의 표시 위치를 따르되 완료 기록은 기록 보관실에 보존한다.

Codex의 준비/대기/과거 성공만으로는 인물이 나오지 않는다. 실제 새 실행 때 입장하고
관측된 성공 후 약 1초 결과를 보인 뒤 복도·입구로 퇴장한다. 실패·의미 있는 미확인
작업은 확인 필요 방에 남는다. 단계 변경·즉시 완료·퇴장으로 인물이 사라져도 선택된
실제 업무 상세를 유지한다. 기록이 정리된 실제 MCP/실행기는 unobserved 대기 객체로
남기며 관측되지 않은 Agent를 생성하지 않는다. 3차의 고정 사고 station 한계는 해소했다.

검증: Node **136/136**, 실제 loopback HTTP/Chromium 브라우저 **58/58**(81.806초),
Ruff·JS 구문·diff 검사 통과. 명시 단계 방 배치, 알려진 객체의 기록 정리, 선택된
인물 삭제, 방별 숫자·완료 기록 보존 문제는 실패 재현 뒤 회귀 검증했다. 별도 코드
검토 APPROVE이며 필수 미해결 항목은 없다. fixture 화면은 임시 프로젝트 데이터로
여섯 역할·Codex 실행·390/320px 선택 카메라를 확인한 증거이며 실제 내부 사고의 관측
증거가 아니다. 웹/앱 외부 도구와 내부 추론은 여전히 자동으로 관측되지 않는다.

원본에는 별도 세션의 chatgpt-mcp/host_db_migration.py 변경이 발견됐다. 해당 파일은
덮어쓰지 않으며 검증한 격리 작업본에서 운영 후보를 만든다. 후보의 DB 코드는 기존
운영본과 같고 변경된 제품 파일은 metadata 저장/스키마/hook 4개와 표시/이동 4개뿐이다.
첫 rollout 시도는 UI 방 집계 수정 때문에 targeted 검사 도중 의도적으로 중단했다.
rollout-c5z8jjws 영수증(exit 130, ROLLOUT_INTERRUPTED)을 보존했으며 설치·전환은 없었다.
검증 완료 후 새 절차에서 필수 검사를 다시 통과해야 운영에 적용한다.

증거: ~/.local/state/personal-ops-dashboard/dashboard-roles-20261015/
- browser-red.log / browser-selection-red.log / browser-room-count-red.log: 회귀 실패 재현
- browser-room-count-regression.log / browser-room-count-green.log: 완료 기록/방 숫자 검증
- browser-final.log / node-tests.log: 최종 브라우저·Node 검사
- fixture-visual.json / fixture-*.png: 임시 데이터 화면·모바일 검사
- baseline.json / validated-source.json / candidate.json: 원본 기준선·검증 소스·운영 후보

운영 적용 및 원본 반영 결과는 아래에 기록한다. 새 metadata는 선택 필드이며 기존 v1
보고와 호환된다. 내부 추론 내용·검색 질의·프롬프트·도구 인자는 수집/추정하지 않는다.


### 4차 원본 반영·운영 전환 대기 (2026-10-15 16:10 KST)

원본에 변경 18개 파일을 기준선 충돌 확인 후 반영했다. 다른 273개 파일의 내용·모드를
반영 직전과 비교해 보존했고 HEAD는 cf8b6d2608c9cadaa939fdca8045891e848ec7ba 그대로다.
다른 세션의 host_db_migration.py 변경은 보존했으며 운영 후보에는 포함하지 않았다.
커밋·push·Docker/DB/Schema 013 적용은 수행하지 않았다.

기존 rollout 절차의 필수 targeted **608/608**(100.938초), full **773/773**(181.426초)
검사 통과. 후보는 **cbce33dec697808a616b**, 70개 파일이며 기존 1bf5d7d05cc5ca07d8e1과
다른 제품 파일은 앞서 검토한 여덟 개뿐이다. 검증된 격리 worktree를 배포 소스로 유지한다.
프로필·서비스 정합성 preflight는 통과했고 설치·서비스 전환 전 ROLLOUT 입력에서 대기한다.

전환 직전 11개 프로젝트의 실행·작업 조회에 오류나 활성 작업은 없고 보호 설정 다섯
파일도 동일했다. 다만 personal-ops에 새 host access grant가 활성화됐다(allow_codex=false,
2026-10-15 19:47:08 KST 만료). 현재 권한을 새 런타임으로 이전하면 안 되며 기존
host_access.py의 runtime_identity 바인딩에 따라 새 배포 시 무효화된다. 이전 배포 확인 때
없던 영향이므로 사용자에게 지금 전환/권한 유지 중 선택을 요청했다. 응답 전에는 전환하지
않는다. 이 문서는 권한 부여나 배포 승인의 대체물이 아니다.

현재 실제 운영 스냅샷은 **1bf5d7d05cc5ca07d8e1**이며 preflight에서 관측한 터널 PID
2579667/start_ticks 26209391, MCP PID 2579703/26209397이다. 신규 후보는 아직 설치·활성화하지
않았다. 운영의 새 표시·선택 필드 등록 및 actual connector 호출 검증은 전환 후 남은 작업이다.
새 업무 수집 신호가 없으면 역할을 생성하지 않는다는 한계도 그대로 유지한다.

추가 증거:
- integrated.json / integration-backups/: 18개 반영과 변경 외 보존
- rollout/rollout-cbsgnh82/receipt.json: 필수 검사·preflight 완료, 전환 입력 대기
- activity-at-switch.json / before-switch-preservation.json: 전환 직전 관측
- active-grant-blocker.json: 활성 권한의 공개 상태, 인증 토큰은 저장하지 않음

승인 후에도 활성 실행·보호 설정·소스·프로세스 정합성을 새로 확인한 뒤 전환해야 한다.
전환한 경우 이 단락 뒤에 실제 설치/실행/HTTP/UI/schema 검증 결과를 추가한다.


### 5차: UI 배포 시 호스트 권한 유지 (구현·보안 검토 완료, 운영 미적용)

사용자가 UI 변경마다 재승인하는 흐름의 개선을 요청했다. 기존 rollout-cbsgnh82에는
CANCEL을 입력해 종료했다(exit 1, ROLLOUT_DECLINED). 설치·서비스 전환은 없었다.
새 정책은 전체 manifest의 모든 파일 무결성을 검사한 뒤 작업실 표시 자산 다섯 개만
제외한 canonical digest에 권한을 묶는다. 새 백엔드·승인·로그인·bridge·미등록 자산 변경은
계속 권한을 무효화하며 만료·철회·재부팅·UID·프로젝트·Codex 동의는 유지한다.
이 정책 전환 자체에는 기존 방식에서 한 번 재승인이 필요하며 권한을 자동 이전하지 않는다.

격리 작업본에서 실제 임시 설치를 이용한 권한 회귀와 관련 검사 44개가 통과했다.
별도 보안 검토에서 확인한 관리자 문서의 renderer 실행 문제는 opaque-origin sandbox
iframe, 서버 CSP, 읽기 전용 snapshot 경계로 수정했다. 관리자만 인증 정보·API 조회·승인
DOM을 보유하며 작업실에는 제한한 공개 상태만 전달한다. 발신 window/null origin,
메시지 필드·길이·프로젝트·epoch·nonce 및 현재 알려진 작업 ID를 검사한다. iframe HTML과
bridge는 권한 식별에 계속 포함한다. 독립 최종 검토는 APPROVE, 필수 미해결 항목 0개다.

실제 Chromium 브라우저 **60/60**(80.393초), Node **144/144** 검사 통과. 변조한 renderer와
CSS로 부모의 로그인 hash·토큰·storage/cookie·승인 DOM/API 접근을 시도했지만 부모 접근은
SecurityError, 네트워크는 CSP로 차단됐고 실제 승인 POST는 0개였다. 임시 fixture 권한도
그대로였다. LAN HTTP의 randomUUID 미지원 환경과 390/320px 화면도 확인했다. 탭을 바꿔도
동일 iframe·선택·지도 상태를 유지한다. 레이아웃 순서를 복원한 뒤 모바일 검사 3개에서
화면 밖 iframe 내부를 먼저 스크롤하려던 Playwright 순서 문제가 발생했고, 외부 iframe을
먼저 보이게 한 뒤 기존 카메라 단언을 수행하도록 고쳤다. 시간 제한·단언은 완화하지 않았다.

필수 rollout의 첫 재실행 rollout-s115o7_o는 targeted **619/619** 통과 후 UI 수정 때문에
정확한 자식 실행만 중단했다(exit 143, ROLLOUT_TERMINATED). 다음 rollout-02091lw7는
targeted **619/619** 통과, full 784개 중 lease 정리 검사 1개 실패로 설치 전 종료했다
(exit 1, RUNTIME_TESTS_FAILED). 원인은 테스트가 outcome_unknown 저장을 worker의 finally
완료로 잘못 해석한 경합이었다. 제품 코드에서는 열린 lease를 retained 목록으로 옮기는
동안 잠금이 유지된다. fixture 장벽과 worker.join으로 완료를 기다리도록 테스트만 고쳤고,
실패 재현 후 단독 50/50 반복, 해당 모듈 9/9, Ruff·diff 검사를 통과했다. 필수 rollout 전체
검사를 다시 수행 중이며 결과를 아래에 추가한다. 실패 영수증은 덮어쓰지 않았다.

검증된 운영 후보는 **1d38d7a0e23adcd04f21**, 73개 파일이다. 기존 운영본과 다른 제품
파일은 17개이며 Schema 014 등 다른 세션의 DB 변경을 포함하지 않는다. 원본 반영 시
install-runtime.py와 host_runtime_operations.py는 frame 자산 세 항목만 병합하고 기존
Schema 014 등록·로직과 나머지 바이트를 보존한다. 설치·서비스 전환은 아직 하지 않았다.
원본 반영 및 필수 검사의 최종 상태는 아래 후속 기록을 기준으로 판단한다.

증거: ~/.local/state/personal-ops-dashboard/dashboard-authority-20261015/
- baseline.json: 원본 295개 파일 기준선, 모드와 HEAD
- authority-red.log / path-red.log: UI 권한 무효화와 symlink-loop 실패 재현
- authority-integration.log: 권한·웹 승인·호스트 실행·Codex 관련 44개 검사 통과
- browser-verified.log / node-final.log: 최종 실제 브라우저·Node 검사
- browser-scroll-regression.log / offscreen-scroll-probe.json: 모바일 스크롤 순서 진단·회귀
- lease-diagnosis/: lease 검사 실패 재현·테스트 수정·50회 반복·모듈 검사
- candidate.json / validated-source.json / integration-merge-proof.json: 운영 후보·원본 병합 검증
- rollout/rollout-s115o7_o/receipt.json / rollout/rollout-02091lw7/receipt.json: 이전 시도 원본 영수증


### pim-check 대시보드 표시 완료 (2026-10-15 16:44 KST)

요청한 pim-check는 /home/jhw/ai/opencode/projects/pim-check의 로컬 QA 저장소다.
projects.json에는 이미 등록되어 있었지만 관리 서버와 Codex 관측기가 등록 이전부터
실행되어 예전 11개 목록을 유지했다. 설정 변경 없이 같은 운영 버전으로
personal-ops-admin.service와 personal-ops-codex-observer.service만 재시작했다.
실행 전 활성 작업 없음과 각 서비스 cgroup에 관측/관리 프로세스만 있음을 확인했다.
터널·Codex·Claude 작업 세션과 Docker/DB는 재시작하지 않았다.

관리 API에 12개 프로젝트 및 pim-check가 표시된다. LAN 실제 Chromium에서 레포
선택·작업실 로딩·390/320px 화면을 확인했고 JS 오류와 로그인 후 변경 요청은 0개다.
설정 다섯 파일의 hash/모드, 기존 CLI 프로세스 10개의 PID/start_ticks/argv hash는
전후 동일하다. 관리자 실행 경로는 1bf5d7d05cc5ca07d8e1 그대로이며 새 권한 정책은
배포하지 않았다. 관리 PID는 2753876, 관측기 PID는 2753877이다.
기존 브라우저는 새로고침한 뒤 레포 목록에서 pim-check를 선택한다.

관측기는 8개 로드된 세션을 읽고 있으나 확인 시 pim-check 개발 작업 이벤트는 0개였다.
추가로 thread/loaded/list와 thread/read(includeTurns=False)의 작업 디렉터리 메타데이터만
확인했다. personal-ops 4개·wlan-package 4개이고 pim-check에 매칭되는 로드된 세션은
0개다. 현재 수집 범위에서 pim-check 인물이 없는 것은 등록 실패의 근거가 아니다.
작업실은 실제 수집된 개발 활동만 표시하며 pim-check의 보드 QA 결과 보고서를
자동으로 읽거나 가상의 작업 인물을 생성하지 않는다.

추가 증거:
- dashboard-roles-20261015/pim-check-before-refresh-audit.json: 활성 작업 없음
- dashboard-roles-20261015/pim-check-*-refresh-protected.json: 설정/기존 CLI 보존
- dashboard-authority-20261015/pim-check-refresh.json: API 목록 12개 확인
- dashboard-authority-20261015/pim-check-live-browser.json: 실제 LAN/UI/모바일 검증


### 5차 최종 검사·원본 반영, 운영 미적용 (2026-10-15 17:28 KST)

rollout-82mayiu1에서 수정 후 필수 targeted **619/619**(102.356초), full **784/784**
(171.859초)를 모두 통과했다. preflight는 profile_aligned/service_aligned=true,
현재 운영본 1bf5d7d05cc5ca07d8e1과 후보 1d38d7a0e23adcd04f21(73개 파일)을 확인했다.
당시 MCP PID는 2951902/start_ticks 26685465, 터널은 2951883/26685461이다. 앞선 기록의
PID와 다르므로 후속 전환 직전에는 다시 확인한다. 관리 서버는 같은 운영본의 PID
2753876이며 pim-check 등 12개 프로젝트 조회에 오류·활성 작업은 없었다.

이 apply 시도는 확인 입력 전에 HOST_TTY_REQUIRED로 종료했다(exit 1). 실행 도구가
전달한 stdin/stdout이 실제 TTY 요건을 충족하지 못한 것으로, 검사·preflight 성공을
설치·전환 성공으로 해석하지 않는다. 승인 입력·설치·프로필 변경·서비스 전환은 없었다.
후속 배포는 TTY를 제공하는 기존 호스트 실행 경로에서 같은 필수 절차를 다시 수행한다.
TTY 검사나 승인 절차를 제거하거나 테스트를 생략하지 않는다.

원본에는 검증한 24개 파일을 기준선 비교 후 반영했다. install-runtime.py와
host_runtime_operations.py는 작업실 frame 자산 세 항목만 추가했다. 이 두 파일에서
추가 항목을 제거하면 원본 바이트가 정확히 복원되며 기존 Schema 014 등록·로직을
보존했다. 나머지 원본 275개 파일의 내용·모드와 HEAD도 보존했다. 원본에서 실행한
권한·설치·Schema 014 승인 관련 임시 fixture 검사는 아래 로그를 확인한다. 실제 DB나
Docker를 호출하지 않았으며 다른 세션의 DB 작업을 후보에 포함하지 않았다.

운영 권한은 여전히 personal-ops active=true, allow_codex=false,
2026-10-15 19:47:08 KST 만료다(17:24 관측). 새 정책 자체의 최초 전환은 이 권한을
한 번 무효화하므로 자동 이전하지 않고 별도 운영 승인을 남긴다. 최초 전환 이후 정확히
표시 자산 다섯 개만 변경한 배포가 권한 ID·만료·Codex 동의를 보존하는 동작은 실제 임시
설치 회귀에서 검증했다. 그 밖의 코드 변경은 계속 무효화한다. 운영의 4차 역할 신호와
5차 격리 화면·권한 정책은 아직 미적용이며, 새 선택 필드의 실제 MCP 등록·보고 왕복은
전환 후 확인할 항목이다. 커밋·push·Docker/DB/스키마·다른 CLI 세션 변경은 하지 않았다.

추가 증거(모두 dashboard-authority-20261015/):
- rollout/rollout-82mayiu1/receipt.json: 필수 검사 통과·preflight·TTY 실패 원본 영수증
- integrated.json / integration-backups/: 24개 원본 반영·275개 변경 외 파일 보존
- integration-merge-proof.json: Schema 014 WIP의 바이트 단위 병합 보존
- source-integration-tests.log / integration-validation.json: 원본 반영 후 검증
- live-access-before-policy.json: 기존 권한 공개 상태, 토큰·권한 ID 미기록

후속 담당자는 원본 checkout의 미완료 Schema 014 WIP를 통째로 배포하거나 오래된 격리
worktree를 원본에 덮어쓰지 않는다. 실제 설치 후보는 명시한 격리 worktree이며, 배포
승인 뒤에도 당시 운영본·활성 실행·보호 설정·CLI 상태를 새로 확인해야 한다. 이 문서와
검사 성공은 운영 전환 승인이나 DB/Schema 014 적용 승인의 대체물이 아니다.


### 6차 호출 캐릭터 수명·정원 교체 (2026-10-15)

사용자의 최신 지시를 따라 등록된 객체를 미리 세워두지 않고 실제 호출이 확인된
캐릭터만 표시한다. 호출 순서대로 입구에서 한 명씩 들어가 실제 작업을 하고, 완료한
동일 캐릭터는 대기실로 돌아간다. 완료한 Codex/역할 캐릭터를 없애던 4차 규칙을
대체한다. 처음 조회한 완료 이력은 대기 자격만 주며 가짜 실시간 작업을 재생하지 않는다.

방마다 역할 캐릭터를 포함해 최대 6명을 배정한다. 일곱 번째부터는 실제 호출이 가장
오래된 캐릭터가 문·복도·입구를 거쳐 밖으로 나가고 자리가 비면 새 호출을 들인다.
heartbeat·완료 시각·선택·폴링은 호출 나이를 갱신하지 않는다. 퇴장은 화면 표현이며
실행을 취소하지 않고 canonical 일감·카운트·상세 조회를 보존한다. 퇴장한 호출은
빈자리가 생겨도 자동 복귀하지 않으며 실제 새 호출에만 다시 입장할 수 있다.

동시 호출 A가 실행 중에 B가 끝나도 A의 작업 표시를 유지한다. 짧은 호출이 입장
대기 중에 끝나면 기록된 시작/종료 동선을 입장 후 재생하고 대기실로 보낸다. 조회
범위에서 완료 이력이 빠져도 이미 확인한 대기 캐릭터는 유지한다. 과거 running job에
started_at이 없는 형식도 관측된 실행 상태를 존중하되 queued/prepared만으로 입장시키지
않는다. 같은 시각의 호출은 최초 관측 순서를 보존한다. 한 캐릭터의 동시에 관측한
A/B 중 A가 사라져도 과거 B를 새 호출로 오인하지 않고, 퇴장 후 같은 시각에 새로
관측한 호출은 이름 정렬과 관계없이 입장을 허용한다.

pim-check/CLI 조사: 현재 personal-ops Codex 세션의 도구 시작/종료 이벤트와 실제
작업 애니메이션은 수집되고 있었다. generic execute가 한 방에서 반복되어 걷는 모습이
적은 상태였으며, 수집되지 않은 내부 사고를 만들어 표시하지 않는다. pim-check는
관리 목록에 포함됐지만 조사 당시 관리 세션 메타데이터에 해당 프로젝트의 CLI 세션과
활동 기록이 없었다. 별도 호스트/WSL의 수집 여부는 아직 확인되지 않았다. 이번 단계가
외부 환경에 수집기를 자동 설치하거나 임의로 다른 세션을 연결하지는 않는다.

검증·원본 반영·운영 적용 상태는 아래 최종 기록을 따른다.
증거 위치: ~/.local/state/personal-ops-dashboard/dashboard-calls-20261015/


6차 화면 검증 결과:
- 작업실 Node 115/115, 관리/bridge 포함 전체 Node 164/164 통과.
- 실제 Chromium의 opaque frame 브라우저 68/68 통과(97.484초).
- 방 이동 중 최대 6명, 호출 대기 중 완료, 과거 이력 이탈, 같은 시각의 과거/신규 호출,
  선택·heartbeat·목록 재정렬·모바일·중지·프로젝트 초기화·격리 경계를 포함한다.
- 동일 시각의 과거 호출 재입장과 실제 신규 호출 누락을 각각 RED로 재현하고 수정 후
  최종 관련 브라우저 4/4 통과(2.974초). 몸체·역할 모두 최초 관측 순서를 적용한다.
- 독립 코드 검토 APPROVE, 남은 지적 0건. JavaScript 구문, Ruff, diff 검사 통과.
- candidate.json의 최종 후보는 c3d26b0bc2e9c317be52/73개 파일이다. 5차 후보 대비
  model.js/motion.js/visualizer.js/visualizer.css 네 표시 자산만 추가 변경했다.
  운영 1bf5d7d05cc5ca07d8e1 대비 17개 변경에는 아직 미적용인 앞 단계도 포함된다.

최종 증거: browser-final-candidate.log, browser-ranks-final.log, browser-equal-old-red.log,
browser-new-equal-red.log, node-all-final.log, review-summary.json, candidate.json.


6차 최종 사전 검증·운영 미적용:
rollout-tiorrun1은 apply 옵션 없는 preflight로 targeted 619/619(101.880초), full
784/784(181.411초)를 통과했다. profile_aligned/service_aligned=true,
installed=false/switched=false이며 실제 운영본은 계속 1bf5d7d05cc5ca07d8e1이다.
후보 c3d26b0bc2e9c317be52의 73개 파일을 검사 전후 동일하게 확인했다. 기존 터널
PID 2951883/start 26685461, MCP PID 2951902/start 26685465를 관측했다.
앞선 rollout-0h9s3_nk는 같은 시각 호출 회귀가 발견되어 해당 진단 실행만 중단한
ROLLOUT_INTERRUPTED 기록이며, 운영 터널·CLI·서비스 중단 기록이 아니다.

원본 반영은 6차 baseline.json의 HEAD·대상 SHA/mode 비교와 파일별 백업을 사용한다.
그 결과와 변경 외 파일 보존 증거는 integrated.json/integration-backups/에 기록한다.
다른 세션의 Schema 014 WIP와 원본 install-runtime.py/host_runtime_operations.py/
server.py는 이번 반영 대상이 아니다. 실제 배포 소스는 검증한 격리 worktree이며,
원본 전체를 통째로 배포하지 않는다.

아직 별도 운영 전환 승인을 받지 않았다. 앞서 정한 서비스 전환 별도 승인 조건과
5차 권한 정책 최초 전환의 영향을 유지한다. 승인 시 기존 bundled rollout을 사용해
터널·그 app-server를 잠시 재연결하고 관리 웹을 재시작하며, 기존 임시 HostAccess는
한 번 무효화된다. 권한 자동 이전·갱신이나 Codex 추가 동의는 하지 않는다. 이후
표시 자산만 바뀌는 배포의 권한 보존은 5차 회귀로 검증했다. 승인 직전 활성 실행·
운영본·보호 설정·CLI를 다시 확인한다. DB·Docker·Schema 014 적용 승인은 별개다.


6차 원본 반영 확정: 10개 파일을 반영하고 변경 외 기준선 298개 파일의 내용·모드와
HEAD를 보존했다. 원본에서도 Node 164/164(135.992ms), Ruff, diff 검사가 통과했다.
보호 설정·키 5개와 Codex/Claude CLI 프로세스 9개의 PID/start/argv hash는 전후
동일하다. 관리 API의 12개 프로젝트 조회에 오류·활성 실행은 없었다. 커밋·push,
실제 설치·서비스 재시작·Docker·DB·스키마 변경은 수행하지 않았다.
원본 반영 증거는 integrated.json, root-node-final.log와 integration-validation.json,
보호 상태는 dashboard-roles-20261015/called-lifecycle-protected-before.json 및
called-lifecycle-protected-after.json, 활성 상태는 called-lifecycle-active-after.json이다.


### 6차 운영 적용 확정 (2026-10-15 18:48 KST)

사용자가 별도 운영 전환을 승인했다. 검증한 격리 worktree의 후보
c3d26b0bc2e9c317be52/73개 파일만 기존 bundled rollout으로 설치·전환했다.
원본 checkout의 다른 세션 Schema 014 WIP는 배포에 포함하지 않았다.

rollout-ai_xfdug에서 targeted 619/619, full 784/784를 다시 통과했고 실제 TTY의
기존 ROLLOUT 확인을 거쳐 installed=true/switched=true/runtime_verified=true,
tunnel_healthy=true/admin_status=admin_ready를 확인했다. 직전에도 관리 대상 12개
프로젝트의 활성 실행·조회 오류는 없었다. 현재 터널 PID 3302908/start 27270706,
MCP PID 3302926/start 27270715, 관리 웹 PID 3303105/start 27270824다.
기존 observer는 변경 없는 설치본에서 계속 동작한다. 실제 관리 API는 재시작 후
기존 설정의 13개 프로젝트를 읽었고 오류·활성 실행은 없었다. 설정을 추가한 것은 아니다.

실제 검증:
- 설치된 73개 파일의 소유자·0600 모드·내용 SHA와 manifest를 검증했다.
- loopback/LAN HTTP 16개 검사에서 새 frame/bridge와 표시 자산이 후보 SHA와 일치했다.
- LAN Chromium에서 personal-ops/tossApp/pim-check의 실제 데이터·캐릭터를 확인했다.
  이동 장면도 관측됐고 각 방의 표시 인원이 6명을 넘지 않았다. iframe은 opaque origin을
  유지하고 부모 DOM 접근이 차단됐다. JavaScript 오류·로그인 후 변경 요청은 없었다.
- 390/320px의 5개 탭, 총 10개 반응형 검사를 통과했다. 실제 화면 PNG를 보관했다.
- 실제 ChatGPT 플러그인의 get_host_access(personal-ops) 호출이 새 런타임에서 성공했다.
  앞서 active=true였던 임시 권한은 예고한 최초 정책 전환에 따라
  active=false/reason=RUNTIME_CHANGED다. 기존 권한 파일 2개의 SHA/mode는 그대로이며
  자동 이전·재발급·Codex 동의 확대는 하지 않았다.
- 실제 프로젝트 설정을 사용한 독립 SDK stdio에서 도구 60개와 report_work_events의
  activity_role/search_scope/tool_name 등록을 확인했다. 별도의 격리 fixture 서버는
  도구 49개 범위에서 새 필드 두 건의 저장까지 검증했다. 이 두 검사에서 실제 프로젝트
  쓰기·DB 호출·호스트 명령·모델 호출은 없었다. 독립 SDK 결과를 동일 터널 child의
  ChatGPT 전체 도구 재탐색 완료로 해석하지 않는다.

배포 명령은 exit 2/verification_required로 마쳤다. 이는 설치 실패가 아니라 동일
터널 child의 전체 tools/list 재탐색이 아직 awaiting_discovery이기 때문이다. 이후
실제 플러그인 읽기 호출은 성공했으나 수동 메타데이터 새로고침 자체는 이 실행 환경에서
하지 못했다. ChatGPT 플러그인을 한 번 새로고침해 새 선택 필드를 노출하고, 관리 웹도
새로고침하면 된다. 기존 도구 이름은 같으므로 기존 읽기 호출은 이미 정상 동작한다.
기존 임시 호스트 권한이 필요한 실행은 원래 소유자 승인 경로로 새 권한을 받아야 한다.
후속 작업에서 원본 영수증을 성공으로 덮어쓰거나 이 확인을 위해 재시작을 반복하지 않는다.

기존 독립 CLI 프로세스 7개는 PID/start/argv hash를 보존했다. 교체된 Codex 프로세스
2개는 기존 터널 client의 직접/간접 자식임을 PPID와 시작 시각으로 증명했다(MCP 서버와
app-server는 형제 프로세스다). 보호 설정·키 5개와 원본 소스 309개 파일의 내용·모드,
HEAD도 배포 전후 같았다. 아래 기록 추가 외 다른 소스는 변경하지 않았다. 커밋·push,
Docker·실제 DB·스키마 변경은 수행하지 않았다.

최종 증거(모두 dashboard-calls-20261015/activation/):
- rollout/rollout-ai_xfdug/receipt.json: 원본 배포 영수증과 검사·전환·남은 재탐색 상태
- pre-switch-confirmation.json / preservation-before.json / postdeploy-preservation.json
- authorized-child-replacement.json / grant-records-before.json
- connector-access-verification.json: 실제 플러그인 호출과 권한 전환 공개 상태
- installed-sdk-catalog-verification.json: 실제 설정·독립 SDK의 60개 도구/새 필드
- installed-metadata-verification.json: 격리 fixture 두 건 저장·모델 호출 0
- live-verification.json / live-*.png: 실제 LAN 화면·방 정원·모바일·격리 경계
- live-catalog-observation.json: 동일 child의 수동 재탐색 미확인 상태
- final-observations.json: 최종 운영 결과와 명시적인 잔여 확인


### 후속 권한 복구·백그라운드 표시 확인 (2026-10-15 19:01 KST)

사용자가 호스트 권한 승인을 명시적으로 요청했다. 기존 범위인 personal-ops에 대해
새 요청을 만든 뒤 정상 소유자 승인 API와 새 관리자 인증으로 4시간 승인했다.
Codex 추가 실행 동의는 기존 false를 유지했다. 실제 플러그인 get_host_access로
active=true/reason=null, 2026-10-15 23:01:47 KST 만료를 확인했다. 다른 프로젝트의
권한 레코드는 그대로이며 명령 실행·DB 작업·서비스 재시작은 하지 않았다.
증거: activation/host-access-restored.json, host-access-connector-restored.json.

배포본 admin.js의 mapShouldAnimate/pollingEnabled와 visibilitychange 처리,
visualizer.js의 canMove/motionState를 읽어 백그라운드 동작을 확인했다. 탭이 숨겨지면
화면용 폴링과 애니메이션을 중지하고 다시 보일 때 최신 상태를 조회한다. 서버의 실제
MCP/에이전트/빌드 실행과 로그 수집은 이에 연결되지 않으므로 계속된다. 캐릭터 위치와
보행 시간은 각 브라우저의 표현 상태이며 서버에서 계속 진행되는 공용 게임 시계는
아니다. 사용자는 동작을 질문했으며 이 단계에서 애니메이션 정책을 변경하지 않았다.


### 7차 최신 기록·작업 요약·화면 복귀 동기화 (2026-10-15)

사용자가 후속 표시 개선을 승인하고, 기록/보관실의 최신 작업을 맨 앞에 배치하면서
기존 작업을 한 칸씩 미루는 동작과 작업·캐릭터 클릭 요약을 요청했다. 별도 격리
worktree dashboard-history-20261015에서 검증한 운영본 c3d26b0bc2e9c317be52를
기준으로 구현했다. 원본의 다른 세션 Schema 014 WIP는 가져오거나 배포하지 않는다.

구현 내용:
- 기록 카드는 작업 종류가 아니라 실제 완료/반환 시각의 내림차순으로 배치한다.
  같은 시각은 최초 기록 관측 순서를 보존한다. 신규 기록은 앞 슬롯에 들어가며 기존
  버튼을 재사용해 한 칸씩 이동한다. 키보드 순서와 포커스도 보존한다. 여섯 칸을 넘는
  기록은 기존 방별 상세 목록에서 볼 수 있다. 중지/동작 줄이기 상태에서는 이동 전환을
  적용하지 않으며, 기존 여섯 명 정원과 실제 캐릭터 퇴장 동작은 유지한다.
- 작업·캐릭터·사고 역할을 클릭하면 작업명, 담당 객체/이름, 관측한 동작, 상태/결과,
  기록 시각/출처를 먼저 보여준다. 이미 전달되는 검증된 종료 코드·실행 시간·출력 크기만
  추가로 사용한다. 요청 반환은 실행 성공과 구분하고, 수집 범위를 벗어난 이전 캐릭터의
  요약은 이전 기록임을 명시한다. 원본 출력과 상·하위 업무 링크도 유지한다.
- 백그라운드/관찰 정지에서 복귀하면 실제 새 조회가 성공할 때까지 동기화를 기다린다.
  캐시된 복귀 메시지나 숨겨진 동안 늦게 도착한 응답을 새 조회로 오인하지 않는다.
  새 관측 이후 인물을 최신 작업/대기 위치로 맞추고, 숨긴 동안 확인된 신규 업무·성공·
  요청 반환·확인 필요를 요약한다. 사라진 기록은 성공으로 간주하지 않는다. 기존 수동
  재생은 유지한다. 오류/프로젝트 전환/파괴 시 처리도 검증한다.

서버가 보내지 않은 작업 설명, 내부 사고, 로그 또는 모델 결과를 만들어내지 않는다.
추가 모델 호출·의존성·새 런타임 파일은 없다. 실제 실행/수집 속도는 바꾸지 않는다.
표시용 model.js/visualizer.js/visualizer.css 세 파일만 런타임 변경 대상이다.
부모 관리자·opaque frame/bridge·호스트 권한 정책·실행 코드는 기존 운영본과 같다.
전체 설치 무결성 검사는 유지되며 권한 식별 digest도 동일하다. 기존 권한 만료 시각과
Codex 추가 동의는 연장하거나 변경하지 않는다.

검증 결과·원본 반영·최종 사전 검증/운영 적용 여부는 다음 최종 기록을 따른다.
증거: ~/.local/state/personal-ops-dashboard/dashboard-history-20261015/

7차 화면 최종 검증:
- 전체 Node 172/172, 실제 opaque frame Chromium 75/75(103.692초) 통과.
- 독립 검토에서 정지 중 늦게 도착한 응답의 캐시 재전송 경합을 발견했다. 새 회귀를
  RED로 확인하고 정지 중 관측 시각의 상한을 갱신하도록 수정했다. 기존 요약 기준선은
  유지한다. 수정 후 복귀 관련 4/4와 위 전체 브라우저 75/75를 통과했다.
- 최신 카드의 종류 간 정렬·한 칸 이동·같은 시각 순서·키보드 포커스, 작업/캐릭터/역할
  요약, 요청 반환 구분, 조회 실패·소실·캐시 캐릭터·프로젝트 초기화·파괴를 포함한다.
- 독립 코드 검토 APPROVE, 남은 지적 0건. JavaScript 구문, Ruff, diff 검사 통과.
- 격리된 임시 API/Chromium에서 1280/390/320px 요약과 역할 선택을 확인했다.
  가로 넘침·JavaScript 오류가 없고 실제 호스트 쓰기·모델 호출은 0건이다.
  preview-verification.json과 preview-summary-1280.png/390.png에 기록했다.
- 최종 후보는 500107e94ce6308289ab/73개 파일이며 운영본 대비 표시 파일 세 개만
  변경된다. candidate.json의 authority_unchanged=true다. 이 비교는 권한 자동 승인이나
  만료 연장이 아니며 실제 운영 전환과도 구분한다.

초기 진단 전용 rollout-64mo34kv는 위 경합 발견 후 정확한 PID/start/cwd와 --apply
부재를 확인하고 중단했다. ROLLOUT_INTERRUPTED/exit 130 원본 영수증을 보존했다.
이는 기존 터널·서비스·사용자 CLI를 중단한 것이 아니다. 수정 후보의 최종 사전 검증은
별도 rollout-s1mun1q5에서 수행한다. 최종 운영 전환은 아직 승인받거나 실행하지 않았다.

7차 최종 사전 검증 완료:
rollout-s1mun1q5는 --apply 없는 preflight이며 필수 619/619(103.188초), 전체
784/784(180.933초), exit 0/preflight_ok다. 검사 전후 후보 73개 파일의 SHA가 같고
profile_aligned/service_aligned=true다. installed=false/switched=false이며 실제
운영본은 계속 c3d26b0bc2e9c317be52다. 터널 PID 3302908/start 27270706과 MCP
PID 3302926/start 27270715도 그대로다. 관리 API의 13개 프로젝트에 조회 오류나
활성 실행이 없었다. 보호 설정·키 5개와 독립/터널 CLI 9개의 PID/start/argv hash가
전후 동일하다. 최종 receipt.json을 수정하거나 성공 상태를 덮어쓰지 않았다.

원본 반영은 baseline.json의 HEAD/index/파일별 SHA·모드 비교와 백업을 사용한다.
반영 범위는 위 표시 파일 세 개, history.test.js/host-executions.test.js,
check_admin_ui.py, DESIGN.md, 이 handoff의 여덟 파일이다. 기존 다른 302개 파일과
Schema 014 WIP를 보존해야 하며 충돌이 있으면 덮어쓰지 않는다. 실제 반영 결과는
integrated.json과 integration-validation.json을 따른다. 커밋·push·서비스 재시작·
실제 Docker/DB/스키마 변경은 이 단계에 포함하지 않는다.


7차 원본 반영 완료: 지정한 여덟 파일을 해시·모드 비교와 개별 백업으로 반영했고,
나머지 기준선 302개 파일 및 HEAD/index를 보존했다. 원본에서도 Node 172/172,
Ruff, JavaScript 구문, diff 검사가 통과했다. 보호 설정 5개와 CLI 9개도 같다.
실제 플러그인 읽기로 personal-ops 권한 active=true, allow_codex=false, 만료
2026-10-15 23:01:47 KST가 기존과 같음을 확인했다. 운영 전환은 하지 않았다.
최종 후보 500107e94ce6308289ab를 적용하려면 앞서 정한 서비스 전환 별도 승인을
거쳐야 한다. 승인 후 관리 웹과 터널의 짧은 재연결이 필요하며, 화면 자산만 바뀌므로
권한 식별자는 유지된다. 다른 세션 WIP를 포함한 원본 전체를 배포하지 않는다.


### 7차 운영 적용 확정 (2026-10-15 20:23 KST)

사용자가 운영 전환을 승인했다. 검증한 격리 worktree의 후보
500107e94ce6308289ab/73개 파일을 기존 bundled rollout으로 설치·전환했다.
운영본 c3d26b0bc2e9c317be52 대비 model.js/visualizer.js/visualizer.css 세 표시
파일만 변경됐다. 원본의 다른 세션 Schema 014 WIP는 배포에 포함하지 않았다.

rollout-37jr0coc에서 필수 619/619(110.805초), 전체 784/784(181.191초)를 다시
통과했고, 실제 TTY의 기존 ROLLOUT 확인 전에 13개 프로젝트의 활성 실행·조회 오류
없음을 재검증했다. installed=true/switched=true/runtime_verified=true,
tunnel_healthy=true/admin_status=admin_ready다. 새 터널 PID 3484497/start
27843002, MCP PID 3484513/start 27843010, 관리 웹 PID 3484677/start
27843119다. 운영은 현재 500107e94ce6308289ab다.

실제 운영 검증:
- 설치 73개 파일의 소유자·0600 모드·SHA/manifest와 loopback/LAN HTTP 16개 검사를
  통과했다. 실제 프로세스도 새 설치본을 사용한다.
- personal-ops/tossApp/pim-check 각각의 실제 기록 카드 여섯 개가 최신 순서/연속
  슬롯으로 배치되는 것을 확인했다. 세 프로젝트 모두 작업·캐릭터 클릭 요약을
  확인했고 실제 관찰 정지/재개 후 복귀 동기화 완료를 확인했다.
- 방별 실제 표시 인원은 최대 여섯 명이다. opaque origin과 부모 DOM 접근 차단을
  유지한다. 390/320px의 다섯 탭, 총 열 개 반응형 검사를 통과했다.
- JavaScript 오류와 로그인 이후 변경 요청은 0건이다. 실제 호스트 작업·모델 호출로
  테스트 데이터를 만들지 않았다. 실제 화면 PNG와 검사 결과 JSON을 보관했다.
- 실제 ChatGPT 플러그인 get_host_access(personal-ops)가 새 런타임에서 성공했다.
  active=true/allow_codex=false/만료 2026-10-15 23:01:47 KST가 배포 전과 같다.
  권한 레코드 파일도 내용·모드가 그대로이며 승인 재발급이나 만료 연장을 하지 않았다.
- 보호 설정·키 다섯 개와 독립 CLI 일곱 개의 PID/start/argv hash를 보존했다.
  교체된 두 Codex app-server 프로세스는 이전 터널의 직접/간접 자식임을 확인했다.
  배포 전후 원본 310개 파일·HEAD/index도 동일했다. 이후 이 기록과 DESIGN.md의
  운영 상태만 추가/갱신했으며 그 외 308개 파일은 그대로다. 커밋·push·Docker·실제
  DB·스키마 변경은 수행하지 않았다.

배포 영수증의 exit 2/verification_required는 그대로 보존한다. 설치/전환 실패가
아니라 동일 터널 child의 수동 tools/list 관측이 awaiting_discovery이기 때문이다.
실제 플러그인 읽기는 정상이나 전체 도구 재탐색 완료를 주장하지 않는다. 이번 7차는
MCP 도구/스키마를 바꾸지 않았으므로 작업실 표시 변경에는 관리 웹 새로고침만 필요하다.
이 상태 확인을 위해 재시작하거나 권한을 다시 승인하지 않는다.

최종 증거(모두 dashboard-history-20261015/activation/):
- rollout/rollout-37jr0coc/receipt.json: 원본 필수 검사·설치·전환·남은 재탐색 상태
- approval-scope.json / pre-switch-confirmation.json / authorized-child-replacement.json
- source-before.json / grant-records-before.json / postdeploy-preservation.json
- live-verification.json / live-history-verification.json / live-*.png
- live-catalog-observation.json / final-observations.json


### 8차 캐릭터 이벤트 말풍선 (2026-10-15)

사용자가 실제 작업 지시·완료·문제 발생 등에 맞춘 캐릭터 말풍선을 요청했다.
검증한 운영본 500107e94ce6308289ab에서 별도 dashboard-bubbles-20261015
worktree를 만들었다. 원본의 Schema 014 WIP와 다른 세션 변경은 가져오지 않는다.

호출된 실제 캐릭터/사고 역할의 새로운 시작·행동 변경·종료 상태에 짧은 말풍선을
표시한다. 성공은 '작업 끝!', 도구 반환은 '응답 보냈어요'로 구분한다. 실패는
'앗, 문제가 생겼어요!'로 알리며 시간 초과·취소·중단·결과 불명도 각각 구분한다.
실수·재시도·원인을 관측 없이 추측하지 않는다. 채팅 보고는 '보고' 접두사를 유지한다.
고정 문구와 수신된 업무 제목을 사용하므로 추가 모델/토큰 호출은 없다.

표시 정책:
- 실제 등장한 인물/역할에만 연결하고 방 정원·입퇴장·작업 실행에는 영향을 주지 않는다.
- 한 인물에 한 개, 전체 최대 세 개/방별 최대 두 개이며 문제 알림이 우선이다.
  방 경계를 벗어나지 않게 배치하고 겹치는 말풍선은 잠시 대기시킨다.
- 일반 알림 4초, 확인 필요 6초, 표시 대기 최대 10초다. 반복 조회로 연장하지 않는다.
  애니메이션 중지/동작 줄이기에서도 독립 타이머로 사라진다.
- 첫 캐시 응답은 기준선으로 인정하지 않는다. 첫 실제 조회는 과거 기록으로 받아
  알리지 않고 그 다음의 새 이벤트만 말한다. 관찰 중지/숨김/오류/복귀/프로젝트 전환/
  파괴 시 정리하고, 과거 이력을 재생해 새 알림처럼 말하지 않는다.
- 현재 슬롯/메시지를 재사용하고 화면 갱신마다 live region을 숨겼다 다시 보이지 않는다.
  말풍선은 클릭을 가로채지 않으며 기존 업무 요약·기록 목록은 유지한다.

런타임 변경은 기존 model.js/visualizer.js/visualizer.css 세 표시 파일뿐이다.
부모 관리자·frame/bridge·MCP 도구·승인/실행 코드는 유지한다. 기존 권한 만료나
Codex 동의를 자동 변경하지 않는다. 코드/검사/원본 반영과 실제 운영 적용을 구분하며
이번 기능의 서비스 전환은 아직 별도 승인받지 않았다. 현재 운영은 계속
500107e94ce6308289ab다. 검증·반영 결과는 후속 최종 기록을 따른다.

증거: ~/.local/state/personal-ops-dashboard/dashboard-bubbles-20261015/

8차 표시 검증:
- 말풍선 모델 9/9, 전체 Node 181/181, 실제 opaque frame 브라우저
  82/82(111.431초)가 통과했다. 독립 코드 검토 APPROVE, 남은 지적 0건이다.
- 과거 시각으로 되돌아간 상태 알림, 반복 repaint의 hidden 토글, 초기 빈 캐시를
  실제 기록으로 오인하는 알림, 인접 방 경계 말풍선 겹침을 RED로 확인하고 수정했다.
  초기 캐시 검증은 자동 대기 후 사라진 상태가 아니라 즉시 count==0으로 확인한다.
  readiness gate만 제거한 RED를 별도로 확인하고 finally에서 원래 바이트로 복원했다.
- 작업 시작·완료·요청 반환·보고된 오류, 역할별 연결, 반복 조회/시간 만료,
  애니메이션 중지/동작 줄이기, 관찰 중지·오류·숨김·복귀·프로젝트/파괴, 클릭/원본 출력과
  기존 방 정원/모바일/격리 경계를 포함한다. JavaScript 구문·Ruff·diff도 통과했다.
- 격리 임시 API/실제 Chromium에서 시작·완료·문제 세 색상 말풍선이 동시에 보이는
  1440/390/320px 미리보기를 확인했다. 겹침·가로 넘침·JS 오류·모델 호출·실제 호스트
  쓰기는 없었다. preview-bubbles-desktop.png와 preview-verification.json에 기록했다.
- 후보는 41339f4dc09de5363e0d/73개 파일이다. 운영본 대비 세 표시 파일만 바뀌며
  authority_unchanged=true다. 후보 상태와 실제 운영 상태를 혼동하지 않는다.

최종 사전 검증은 --apply 없는 rollout-y74gzx1u에서 수행한다. 그 원본 receipt와
integrated.json/integration-validation.json이 실제 검사/반영 결과다. 운영 전환은
여전히 별도 승인이 필요하며, 이 검증을 위해 권한을 재발급하거나 실제 서비스를
재시작하지 않는다.


8차 최종 사전 검증 완료:
rollout-y74gzx1u에서 필수 619/619(112.102초), 전체 784/784(183.284초),
exit 0/preflight_ok를 확인했다. 검사 전후 후보 73개 파일의 SHA가 같으며
profile_aligned/service_aligned=true다. installed=false/switched=false이고
실제 운영은 계속 500107e94ce6308289ab다. 기존 터널 PID 3484497/start
27843002와 MCP PID 3484513/start 27843010도 그대로다. 보호 설정·키 다섯 개와
CLI 아홉 개의 PID/start/argv hash를 보존했다. 관리 API의 열세 프로젝트에 조회
오류·활성 실행은 없었다. 원본 영수증은 보존하고 임의로 적용 상태를 덮어쓰지 않는다.

원본 반영은 baseline.json의 HEAD/index 및 파일별 SHA/모드 비교와 개별 백업을
사용한다. 대상은 세 표시 파일, tests/bubbles.test.js, check_admin_ui.py,
DESIGN.md와 이 handoff의 일곱 파일뿐이다. 나머지 기준선 304개 파일과 Schema 014
WIP는 보존한다. 실제 반영 결과는 integrated.json/integration-validation.json을
따르며, 서비스 재시작·DB/Docker/스키마·커밋·push는 수행하지 않는다.


8차 원본 반영 완료: 위 일곱 파일을 해시·모드 비교와 개별 백업으로 반영했고,
다른 기준선 304개 파일 및 HEAD/index를 보존했다. 원본에서도 Node 181/181,
Ruff·JavaScript 구문·diff 검사가 통과했다. 실제 플러그인 읽기로 기존 personal-ops
권한 active=true, allow_codex=false, 만료 2026-10-15 23:01:47 KST가 그대로임을
확인했다. 운영에는 아직 적용하지 않았다. 별도 서비스 전환 승인 후 검증한 격리
worktree의 후보 41339f4dc09de5363e0d를 기존 bundled rollout으로 적용해야 한다.
원본 전체를 배포하거나 다른 세션 WIP를 덮어쓰지 않는다.


8차 운영 적용 완료 (2026-10-15 21:33 KST):
사용자의 "적용" 승인으로 격리 worktree의 41339f4dc09de5363e0d를 적용했다.
직전 운영 500107e94ce6308289ab 대비 세 표시 파일만 달라지고 권한 identity는 같다.
원본의 다른 WIP/Schema 014를 배포하지 않았다.

- 원본 영수증: activation/rollout/rollout-e92cw_gj/receipt.json.
  필수 619/619(109.946초), 전체 784/784(181.741초)가 다시 통과했다.
  installed=true, switched=true, runtime_verified=true, tunnel_healthy=true,
  admin_status=admin_ready, 실제 live_snapshot=41339f4dc09de5363e0d다.
  실제 MCP PID 3625495/start 28250398, 터널 PID 3625478/start
  28250391이며 관리자 새 런타임 경로도 직접 확인했다.
- 설치된 73개 파일의 SHA/owner/0600, loopback/LAN HTTP 16건, 실제
  personal-ops/tossApp/pim-check 화면과 6명 정원, opaque frame 격리를 확인했다.
  390/320px의 다섯 탭 10건에 가로 넘침·JavaScript 오류·로그인 후 변경 요청이 없다.
- 실제 personal-ops 새 작업 이벤트에 '작업 시작!' 말풍선이 뜨는 것을 실 브라우저로
  관측하고 live-speech-1.png로 확인했다. 가상 호스트 작업/실패를 생성하지 않았다.
  나머지 완료·응답·문제 등의 표시는 앞선 브라우저 82/82와 Node 181/181이 검증했다.
- 실제 ChatGPT get_host_access 읽기가 성공했다. 기존 active=true,
  allow_codex=false, 만료 2026-10-15 23:01:47 KST가 변하지 않았다.
  권한 두 레코드 SHA/모드와 보호 설정·키 다섯 개를 유지했고 재발급/갱신하지 않았다.
- 전환 직전 관리 API의 13개 프로젝트에 활성 호스트 작업/조회 오류가 없었다.
  이번 기준선의 독립 CLI 세 개는 PID/start/argv hash가 동일하다. 정확히 확인된
  이전 터널 소유 app-server 두 개만 정상 교체했고 독립 세션은 종료하지 않았다.
- 문서 갱신 전 원본 311개 파일·HEAD/index를 보존했다. DESIGN.md와 이 handoff만
  해시 비교·백업 후 갱신하고 다른 309개 파일/Schema 014 WIP는 그대로 둔다.
  DB/Docker/스키마 작업·커밋·push·Notion 저장은 수행하지 않았다.

남은 관측 한계: passive MCP 전체 catalog는 여전히 awaiting_discovery다.
따라서 원본 영수증의 exit 2/verification_required와 tools_registered=false를
ready로 덮어쓰지 않는다. 실제 설치/실행/연결 도구 읽기/화면 검증 성공과 구분한다.
이번 변경에는 새 MCP 정의가 없어 이 이유로 재시작·권한 승인·플러그인 새로고침을
반복할 필요가 없다. 관리 웹을 새로고침하면 새 표시 파일을 받는다. 초기 과거 기록은
말하지 않고 새로 관측한 작업 이벤트부터 표시한다.

증거(모두 dashboard-bubbles-20261015/activation/):
- approval-scope.json / guard-before.json / guard-confirm.json / guard-after.json
- rollout/rollout-e92cw_gj/receipt.json (원본 상태 보존)
- live-verification.json / live-speech-verification.json / live-*.png
- live-catalog-observation.json / final-observations.json / documentation-backups/
