# 채팅 내 호스트 권한 승인 카드

상태: 사용자 승인으로 운영 전환을 수행했다. 현재 런타임은 `0be604c833196ab25f29`.
ChatGPT에서 첫 카드가 표시된 것은 사용자가 확인했으며, 프레임 높이가 작다는 보고를 받아
내용 높이 자동 보고를 추가했다. 이후 사용자가 실제 ChatGPT에서 카드 열기·승인 클릭을
확인했으며, 실제 MCP 조회로 tossApp HostAccess 활성화를 확인했다.

## 요청과 동작

관리 웹으로 이동하거나 관리 토큰을 채팅에 넣지 않고, ChatGPT 안의 카드에서
호스트 권한·기간·Codex 옵션을 확인하고 직접 승인하는 경로를 추가했다.

| 도구 | 표시 범위 | 역할 |
| --- | --- | --- |
| `open_host_access_approval` | 모델 | 요청과 카드만 생성. 기본 4시간, Codex 꺼짐 |
| `update_host_access_approval` | 앱 | 옵션 변경과 확인값 교체. 카드 만료는 연장하지 않음 |
| `confirm_host_access_approval` | 앱 | 일회용 확인값을 소비하고 기존 승인 함수를 호출 |
| `cancel_host_access_approval` | 앱 | 해당 요청 제거. 이미 활성화된 권한은 철회하지 않음 |

카드에는 실제 호스트·사용자·UID·그룹, 프로젝트와 작업 디렉터리, 설치의 호출자와
대화가 권한을 공유한다는 범위, 기간과 Codex 동의를 표시한다. 프로젝트는 작업
문맥이며 파일시스템 격리가 아니다. 카드 열기·옵션 편집·“승인” 프롬프트만으로
권한이 생기지 않는다. 옵션을 편집하는 동안 승인 버튼을 잠근다.

기존 `request_host_access`, `get_host_access`, 기간제 실행과 Codex 위임 경로 및
관리 웹의 새 토큰 인증은 유지했다. 별도 호스트 명령이나 모델을 자동 실행하지 않는다.

## 확인 경계와 실패 처리

- 확인값은 256비트 난수이며 결과 `_meta`로만 전달한다. 공개 텍스트·structuredContent·
  위젯 상태·모델 문맥·URL·서버 기록에 원문을 넣지 않는다. 디스크에는 해시만 저장한다.
- 프로젝트별 최신 카드 하나가 유효하다. 최대 10분이며 프로젝트 경로·UID·런타임·
  부팅 식별자·표시 옵션과 결합한다. 옵션을 바꾸면 확인값을 교체한다.
- 확인값을 먼저 소비하고 기존 일회용 요청 승인 함수를 사용한다. 동시 클릭이나 응답
  유실로 권한이 다시 발급되거나 기간이 늘어나지 않는다. 모호한 응답은 재시도 버튼을
  잠그고 `get_host_access` 확인 또는 새 카드 열기를 안내한다.
- 취소는 기존 승인 기록의 잠금 안에서 요청 존재 확인·삭제·저장을 먼저 끝낸다.
  관리자 승인이 먼저 완료됐으면 취소 성공을 반환하지 않는다. 요청 삭제 저장이
  실패하면 카드를 terminal 상태로 만들지 않는다.
- MCP SDK가 입력 오류에 값을 포함할 수 있으므로, 새 도구는 SDK 검증 전에 고정
  오류 코드로 검증한다. 예상 밖 오류는 확인값이나 예외 문장 대신 도구명·예외 종류·
  errno·진단 ID만 stderr에 남긴다. 클라이언트 오류에도 같은 진단 ID를 반환한다.

앱 전용 표시 자체는 인증이 아니다. 인증된 터널의 MCP 호스트와 컴포넌트를 신뢰하는
설계이며, 이를 제어하는 호출자는 같은 신뢰 경계 안에 있다. 위젯 세션 ID나 모델의
승인 주장을 소유자 인증으로 쓰지 않는다. UI 계약은
[OpenAI plugin reference](https://developers.openai.com/plugins/reference)의
앱 전용 도구·결과 `_meta`·MCP App 리소스를 사용한다.

## 구현과 원본 보존

- 핵심: `chatgpt-mcp/host_access_ui.py`, `host-access-approval.html`, `server.py`.
- installer와 VERIFIED_RUNTIME_FILES에 두 신규 권한 파일을 포함했다. 표시 전용
  작업실 자산 예외에 넣지 않았다. 배포 계약에 신규 도구 4개를 추가했다.
- rollout은 카드 브라우저 검사를 `checks_approval_ui` 선행 필수 단계로 실행한다.
  `/usr/bin/python3`와 설치된 Playwright가 지정한 Chromium을 사용하고, 실행 파일의
  소유자·유형·쓰기 모드·실행 가능 여부를 확인한다. 검사 파일도 control inventory에
  포함하며 실패 이름·개수는 기존 비공개 rollout receipt에 남긴다.
- 이 인계 문서를 포함한 원본 18개 경로에 이번 변경만 반영했고 기존 307개 파일을 바이트·모드 그대로
  보존했다. 세 곳의 추가 줄 충돌은 양쪽을 모두 유지했다. 기존 Schema 014와
  runtime rollout 관련 WIP를 덮어쓰거나 배포하지 않았다. HEAD와 index는 변경하지 않았다.
- 소스 반영 단계에서는 서비스와 설정을 변경하지 않았다. 이후 사용자 승인으로 아래 운영
  전환을 수행했다. 커밋·실제 운영 grant 발급·모델 호출·Docker/DB 작업은 하지 않았다.

## 검증 기록

- RED 단계: 관리자 웹 승인과 취소 충돌, 취소 저장 실패, 비밀 없는 오류 진단,
  브라우저 검사 배포 차단, UI 실패 이름/개수 기록 누락을 재현한 뒤 수정했다.
- 실제 MCP stdio에서 도구/리소스 등록, 앱 전용 표시, `_meta` 전달, 오류 반사 차단,
  일회용 승인·재사용 거부를 임시 상태와 주입한 런타임 신원으로 검증했다.
- 원본 반영 후 관련 Python 검사 59개 통과. 기존 runtime rollout 라우터 검사도 포함한다.
- Chromium 14개 통과: 신규 승인 카드 8개와 기존 프로젝트 선택 카드 6개.
- Node 회귀 187개 통과. 변경 Python 파일 Ruff와 원본 diff 공백 검사 통과.
- 독립 보안·코드 검토의 초기 3건을 수정했고 재검토에서 남은 must-fix 없음(APPROVE).
- 최종 분리 소스 전체 Python 검사 804개: 803개 통과, 1개 건너뜀.
  별도 opt-in인 `RealCodexIsolationProbeTest.test_real_pinned_codex_offline_isolation`은
  실행하지 않았다. 이번 승인 카드 기능은 실제 Codex 실행을 검증 범위에 넣지 않았다.

브라우저 검사는 표준/레거시 MCP 호스트 fixture에서 실행했다. 실제 ChatGPT에서 최초
카드 표시는 사용자가 확인했지만 높이 잘림 문제가 있었다. 수정 후 사용자는 카드가
열려 승인 버튼을 눌렀다고 확인했고, 실제 HostAccess 활성화도 조회했다. 화면 픽셀 높이와
옵션 펼침 애니메이션은 실제 클라이언트에서 독립 관측하지 않았다. 테스트나 검증을 위해
에이전트가 운영 권한을 발급하거나 호스트 명령·유료 모델·DB 작업을 실행하지 않았다.

## 운영 전환과 표시 오류 수정

- 최초 전환: `cc1631a8718e788b9be5` → `d2fdb644784c1c4e2f3a`.
  MCP·터널 및 관리 웹 전환 후 실제 연결의 `get_host_access` 응답을 확인했다.
  신규 권한 코드로 기존 grant는 `RUNTIME_CHANGED`였으며 자동 이전·연장은 하지 않았다.
- 최초 rollout 실행은 RTK가 자식의 stdin/stdout을 파이프로 연결하여 `HOST_TTY_REQUIRED`로
  중단됐다. 검사만 실행됐고 설치·서비스 변경 전이었다. `exec_command tty:true`만으로는
  해결되지 않으므로 `rtk proxy script -q -e -c '<기존 rollout 명령>' /dev/null`로 실제 PTY를
  제공했다. 검사와 승인 게이트를 생략하거나 isatty를 위조하지 않았다.
- 성공한 첫 전환 receipt는 `activation/receipts/rollout-5i7l7t9z/receipt.json`이다.
  프로세스·75개 파일 무결성·관리 웹 28개 자산·원본과 비공개 설정 보존을 확인했다.
  관리 웹은 실제 브라우저에서 3개 프로젝트 × 5개 화면 × 2개 폭, 총 30개 조합을 확인했다.
  이는 관리 웹 검사이며 ChatGPT 카드 렌더링 증거로 대신 쓰지 않았다.
- 사용자가 실제 ChatGPT에서 카드 표시를 확인했으나 프레임이 너무 낮다고 보고했다.
  원인은 표준 MCP Apps `ui/notifications/size-changed` 보고 누락이었다. 기존 fixture도
  부모가 높이 알림을 적용하는 상황을 검증하지 않아 이 결함을 놓쳤다.
- 높이 80px에서 시작하는 부모 프레임으로 RED를 확인한 뒤, 초기화 완료 후 최초 높이를
  보고하고 ResizeObserver + requestAnimationFrame으로 변경을 묶어 보고하도록 수정했다.
  공식 SDK와 같이 html 높이를 잠시 `max-content`로 측정하고 원래 스타일을 복구한다.
  이전 크기와 같으면 보고하지 않으며 teardown 시 observer·예약 프레임을 해제한다.
- 알림은 id가 없는 notification이며 `height`, `width` 숫자만 포함한다. 확인값·카드·승인
  상태를 전달하지 않고 기존 승인·취소·일회성 규칙도 바꾸지 않았다. 레거시 도구 호출
  경로를 유지했다. 레거시 notifyIntrinsicHeight의 공식 인자 계약은 확인되지 않아 호출을
  추측해 추가하지 않았다. 높이 자동 보고 검증 범위는 표준 MCP Apps 호스트이다.
- 캐시를 피하도록 리소스를 `ui://host-access/approval-v2.html`로 올렸다.
  분리 소스에서 이번 표시 수정 4개 파일만 원본에 비교 후 반영했다.
- 표시 수정 전환: `d2fdb644784c1c4e2f3a` → `0be604c833196ab25f29`.
  전환 receipt: `height-fix/receipts/rollout-yzgdb1mf/receipt.json`.
  runtime 파일 75개 중 직전 버전과 다른 것은 `host_access_ui.py`와
  `host-access-approval.html` 두 개다. 다른 세션의 Schema 014·runtime rollout WIP는
  운영 입력에 포함하지 않았다. 기존 Docker 컨테이너·DB 데이터·스키마는 변경하지 않았다.

공식 크기 보고 계약:
[MCP Apps notification](https://apps.extensions.modelcontextprotocol.io/api/interfaces/app.McpUiSizeChangedNotification.html),
[공식 SDK 측정 구현](https://github.com/modelcontextprotocol/ext-apps/blob/82221c0c8ce7661efa6771c9d461511b1650495f/src/app.ts#L1917-L1965).

## 높이 수정 검증과 실제 승인 확인

- 브라우저 10개 통과: 기존 승인·취소·오류·비밀 격리 8개 + 높이/옵션/화면 폭/반복 방지 +
  teardown 중단. 좁은 폭에서 901px, 넓은 폭에서 839px로 부모 프레임이 늘고 scrollHeight와
  일치함을 확인했다. 카드 이미지도 열어 글자·경고·승인 버튼이 보이는지 확인했다.
- 관련 backend 18개와 Ruff, 원본 diff 공백 검사 통과. 독립 검토에서 must-fix 없음(APPROVE).
- rollout이 브라우저 10개, targeted 639개, 전체 Python 804개를 다시 검사했다.
  전체는 803개 통과·기존 opt-in Codex 실제 격리 probe 1개 건너뜀이다.
- 설치된 immutable runtime의 별도 stdio 프로세스에서 신규 4개 도구와 v2 리소스 등록,
  model/app 가시성·CSP·확인값 은닉·요청 생성/취소를 임시 상태로 확인했다.
  실제 운영 권한이나 호스트 명령·모델은 실행하지 않았다.
- 전환 후 실제 ChatGPT 연결에서 `get_host_access(personal-ops)`가 정상 응답했다.
  결과는 `active:false`, `reason:RUNTIME_CHANGED`이며 자동 승인하지 않았다.
  기존에 별도 신원을 확인한 CLI 3개와 app-server 3개의 프로세스 신원은 유지됐다.
- rollout의 `verification_required`/종료 2는 호스트 전환 실패와 구별해야 한다.
  같은 실행 중인 MCP 자식의 도구 발견과 실제 connector schema 검증은 별도 증거이며,
  이름만 보거나 fixture가 통과했다는 이유로 완료 값을 만들지 않는다. 원 receipt를 보존했다.
- 실제 ChatGPT에서는 연결을 새로고침한 뒤 새 메시지로
  “tossApp 호스트 권한 승인 카드 열어줘”를 요청해 v2 카드를 연다.
  이전 메시지의 v1 카드가 자동 교체된다고 가정하지 않는다.
- 이후 사용자가 실제 ChatGPT에서 “열려서 승인눌렀다”라고 보고했다. 실제 연결에서
  `get_host_access(tossApp)`를 조회해 `active:true`, `scope:installation`,
  `allow_codex:false`를 확인했다. 만료는 `2026-10-16T05:13:42.939431+00:00`
  (한국시간 2026-10-16 14:13:42)이다. 런타임은 `0be604c833196ab25f29` 그대로다.
  승인 클릭이 실제 권한 활성화로 이어지는 흐름까지 확인했으며, 검증 명목의 명령·모델
  실행이나 권한 재발급·연장은 하지 않았다. Codex 호출 동의는 활성화하지 않았다.
- 실제 클라이언트의 픽셀 높이와 옵션 펼침/닫힘은 별도 화면 관측으로 검증하지 않았다.
  사용자 확인과 실제 조회 결과는 `height-fix/user-confirmation.json`에 기록했다.

검증한 분리 소스:
`/home/jhw/.local/state/personal-ops-worktrees/chat-host-access-ui-20261016`.

비공개 증거:
`/home/jhw/.local/state/personal-ops-dashboard/chat-host-access-ui-20261016/`.
초기 구현·반영 기록과 첫 전환은 기존 `candidate.json`, `integration.json`, `activation/`에,
표시 오류 재현·수정·두 번째 전환은 `height-fix/`에 분리했다. 초기 후보의 installed:false
기록이나 첫 전환 receipt를 사후 성공 값으로 덮어쓰지 않았다.
