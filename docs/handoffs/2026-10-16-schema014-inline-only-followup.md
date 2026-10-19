# Schema014 카드의 외부 승인 안내 제거

날짜: 2026-10-16 KST. 상태: 승인 후 운영 전환·서버/관리 웹 검증 완료, 실제 ChatGPT 카드의 승인·취소 버튼 표시까지 사용자 확인 완료.
카드 버튼 클릭과 실제 Schema014 적용은 이 호스트 작업에서 검증하지 않았다.
현재 운영 버전은 `0c5c451ed88e5661126d`(82파일), 이전 버전은 `ea340a2b1759c297a5b1`다.
사용자는 수정 후 “진행”으로 서비스 전환을 승인했고, 이후 호스트 작업 권한만 최대 8시간으로
늘리도록 선택했다. 초기 수정 후보 `400496bd5d5959f56a3d`는 단독으로 배포하지 않았다.

## 확인한 원인

- 새 Schema014 HTML에는 `이 계획 승인`/`취소` 버튼과 앱 전용 confirm/cancel 호출만 있다.
  외부 승인 페이지로 이동하는 코드는 없다.
- 하지만 승인 대기 카드의 공개 응답에 포함된 `plan`에는 기존 `approval_url`과
  `approval_command`가 남아 있었다. 승인 완료 응답에서만 제거하던 구현 누락이다.
- 실제 운영의 최근 `prepare_schema014_migration`은 06:07:46~47 UTC에 완료됐고,
  해당 카드/계획은 pending/pending_host_approval였다. 직접 승인 호출은 관측되지 않았다.
- 이 세션의 도구 설명에는 예전 관리 웹 승인 안내가 남아 있었다. 다른 웹 세션의 캐시
  상태나 사용자가 누른 정확한 버튼까지 이 증거로 확정할 수는 없다.
- 기존 resource-read 로그는 known_uri만 남겨 어떤 카드가 열렸는지 구분할 수 없었다.

## 수정 범위

1. `host_operation_ui.py`: 모든 카드 응답에서 외부 링크·터미널 승인 명령을 제거한다.
   대기 응답은 카드 버튼 승인을 안내하고, 렌더링 불가를 보고한 뒤 사용자가 명시적으로
   요청한 경우에만 기존 인증된 관리자 복구 경로를 쓰도록 안내한다.
2. `server.py`: 초기 지침도 같은 흐름으로 맞췄다. resources/read에는 정확히 허용된
   공개 리소스 URI만 남긴다. 알 수 없는 URI는 문자열을 기록하지 않고 null로 남긴다.
3. 기존 관리자 복구 경로, 원본 계획/digest, 숨겨진 일회용 확인값, 권한과 승인 영수증,
   별도 DB 실행 단계는 유지한다. 모델의 문장이나 링크 클릭이 승인을 대신하지 않는다.

## 검증

- 수정 전에 unit/실제 MCP stdio 검사에서 대기 응답의 외부 링크·명령 노출과 안내 누락을 재현했다.
  stdio의 실패 assertion은 SDK TaskGroup의 ExceptionGroup으로 감싸져 보고됐다.
- 관련 Python 7모듈 26개 통과, 카드 Node 4개·Chromium 4개 통과.
- Python AST/Ruff 4파일 통과. 독립 코드 검토 차단 사항 없음.
- prepare와 reopen의 structured/text 응답 모두에 외부 승인 필드가 없고, 카드를 여는 것만으로
  영수증이나 DB 변경이 생기지 않음을 검사했다. 엔진의 명시적 관리자 복구 링크는 유지된다.
- 원본과 격리 소스의 4파일을 기준선 hash 비교 후 반영했다. 다른 WIP와 HEAD/index를 보존했다.
- 위 수치는 최초 수정 후보의 관련 검사다. 아래 승인 후 배포에서 공식 전체 검사를 수행했다.
  서버/합성 프로토콜 검사를 실제 ChatGPT 카드 표시 확인으로 대신하지 않는다.

증거: `~/.local/state/personal-ops-rollouts/chat-approval-details-20261016/inline-only-624ryjau/`.
`red.log`, `focused-green.json`, `static-node.json`, `candidate.json`,
`service-readonly-check.json` 및 `final-verification.json`을 참조한다.

## 최대 8시간과 초기 로딩 차단 복구

호스트 권한 발급기·MCP 입력 스키마·채팅 카드·관리 웹을 모두 최대 28,800초/480분으로 맞췄다.
기본값은 14,400초/240분이며, 채팅 카드 확인 창과 Schema014 계획 유효시간은 각각 10분 그대로다.
8시간 선택·만료 경계와 초과 입력 거부를 검증했다. 기존 권한을 자동 발급/연장하지 않는다.
관련 backend/Schema014 63개, Node 16개, 카드 Chromium 12개, 관리 화면 선별 2개와 독립 검토를 통과했다.

결합 후보 `21ceead0174fb157fdcd`의 최초 롤아웃은 검사 시작 전 `load_projects`에서 중단됐다.
등록된 `pim-check-camera13`의 `/home/jhw/ai/opencode/worktrees/jhw-control/wt-camera13-rework`가
이미 삭제됐고 Git worktree 목록에도 없었다. 다른 12개 프로젝트 경로는 존재했다.
`activation/pty-receipts/rollout-raauzjaz/receipt.json`은 `ROLLOUT_UNEXPECTED_ERROR`/exit 1 그대로 보존했다.
확인 문자열 입력·설치·서비스 전환은 전혀 시작하지 않았다.

등록 파일이나 다른 worktree를 수정하는 대신 `server.load_projects`에서 없는 절대 경로만
공개 프로젝트 이름과 `DIRECTORY_MISSING` 진단을 남기고 제외한다. 상대/비정상 경로·일반 파일·
권한 오류는 계속 실패하며, 사용 가능한 프로젝트가 하나도 없으면 시작하지 않는다.
디렉터리 생성·경로 재지정·등록 삭제는 하지 않았다. 관련 회귀 3개를 공식 대상 검사에도 포함했고,
라우팅/배포 관련 41개와 독립 검토를 통과했다. `pim-check` 기본 프로젝트는 계속 제공한다.

## 승인 후 실제 운영 전환과 검증

별도 transient systemd user service의 실제 PTY launcher로 공식 `--apply`를 실행했다.
수정된 소스/control을 고정하고 실제 `ROLLOUT` prompt에서 다시 검증했으며, 실행 중 기록 0건을
확인한 뒤 이미 받은 승인에 해당하는 문자열을 전달했다. 테스트/TTY 검사를 우회하지 않았다.

- 최종 snapshot `0c5c451ed88e5661126d`, MCP PID `2751660`, tunnel PID `2751644`, 관리 웹 PID `2751993`.
  실제 프로세스 경로와 manifest 82개 모두 일치하고 터널 healthy·관리 웹 ready다.
- 배포 안에서 승인 UI 12개·Schema014 UI 4개·런타임 UI 7개, 대상 Python 710개,
  전체 Python 876개가 exit 0으로 완료됐다.
- 원본 `activation-retry/pty-receipts/rollout-lv0ywc59/receipt.json`은 exit 2 /
  `verification_required` 그대로다. installed/switched/runtime_verified/tunnel_healthy는 true,
  admin_status는 admin_ready다. 실제 같은 ChatGPT 연결의 전체 도구 목록 갱신은 아직 미확인이다.
- 설치본의 별도 MCP stdio에서 도구 73개, Schema014 카드 리소스·앱 전용 callback,
  호스트 권한 스키마 최대 28,800초와 기본 14,400초를 확인했다.
- 설치된 실제 서버 코드를 사용하는 임시 합성 상태의 MCP 프로토콜 2개가 통과했다.
  대기/재개봉 카드의 structured/text 응답에서 외부 링크/명령 제거, 공개 리소스 로그 구분,
  8시간 카드 열기와 확인을 검증했다. 실제 호스트 권한이나 DB 계획을 만들지는 않았다.
- 실제 플러그인의 `get_host_access(tossApp)` 읽기 호출이 성공했다. 현재는 `EXPIRED` 상태다.
- 배포 후 실제 터널에서 Schema014 카드 리소스 조회가 관측됐고, 사용자가
  “카드 안에 승인·취소 버튼이 보임”이라고 답했다. 실제 ChatGPT 카드 표시의 사용자 확인이며,
  승인 버튼 클릭·영수증 발급·DB 적용이 확인됐다는 뜻은 아니다. 전체 도구 목록 갱신은 별도 미확인이다.
- 실제 관리 UI에서 호스트 권한 최대 480분·기본 240분, 세 프로젝트와 승인 링크 복구를
  15회 PC/모바일 화면 크기로 검사했다. JS 오류 0, 로그인 외 쓰기 요청 0이다.
  loopback/LAN/신뢰 HTTPS의 자산과 HTML도 검증했다.
- 문서 갱신 전 저장소 337파일·HEAD/index와 레지스트리/인증정보/권한 파일을 보존했다.
  승인된 터널 profile과 두 user service의 런타임 경로만 전환했다.
  전환 직전 생존한 독립 CLI 7개는 이후에도 동일 PID/시작 시각으로 생존했고,
  기준선의 다른 CLI 2개는 서비스 전환 전에 이미 종료돼 있었다. 임의 CLI 중단 명령은 없었다.
  컨테이너 11개의 상태·시작 시각도 그대로다.

최종 증거는 앞 증거 디렉터리의 `8hour-rollout/activation-retry/` 아래
`installed-verification.json`, `installed-catalog.json`, `installed-protocol.json`, `live-ui.json`,
`pre-switch-guard.json`, 원본 receipt/PTY 로그와 최종 결과 파일이다. 최초 실패 기록도 보존했다.

## 웹에서 이어갈 단계

1. 실제 ChatGPT 카드의 `이 계획 승인 / 취소` 버튼 표시는 사용자 확인을 마쳤다.
   전체 도구 목록 갱신을 확인한 것으로 확대하지 않으며, 원본 롤아웃 영수증도 변경하지 않는다.
2. 실제 Schema014 작업을 이어갈 때 유효한 계획의 변경 범위와 해시를 확인하고 사용자가 승인한다.
   계획이 만료됐으면 새 계획이 필요하다. 버튼 클릭·승인 영수증 확인은 아직 별도다.
3. 호스트 실행이 필요하면 권한 카드에서 원하는 기간(최대 8시간)을 선택하고 사용자가 승인한다.
4. 실제 Schema014 승인/DB 적용은 사용자의 웹 tossApp 작업 범위에서 진행한다.
   이 호스트 작업에서는 실제 DB SQL·Docker 변경·권한 발급/연장·커밋·push를 하지 않았다.
