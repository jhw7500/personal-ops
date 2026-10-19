# Schema014 채팅 승인과 캐릭터 작업 상세 개선

날짜: 2026-10-16 KST. 프로젝트: personal-ops.
상태: 소스 구현·선별 반영, 공식 사전검사와 승인 후 운영 전환 완료. ChatGPT 도구 정의 갱신은 미확인이다.
현재 운영 MCP·관리 웹은 `ea340a2b1759c297a5b1`(런타임 파일 82개)이며 이전 버전은
`5c725c7a6d4ef86b9759`다. healthy/ready와 HTTP 200·자산 해시 일치를 확인했다.
사용자는 서비스 재시작 범위를 확인한 뒤 “진행”으로 별도 전환을 승인했다.
실제 Schema014 SQL·Docker·DB·기존 권한 기록은 변경하지 않았다. 호스트 변경은 승인된
MCP·관리 웹 런타임 전환뿐이다. 커밋·push도 하지 않았다.

## 원인과 수정

- 채팅의 기존 승인 카드는 HostAccess만 처리했고 Schema014는 관리 웹으로 보냈다.
  신규 `host_operation_ui.py`·`host-operation-approval.html`이 기존 일회용 승인 영수증을
  채팅 카드에서 발급한다. 기존 001~013 대상 권한이 014를 자동 승인하지는 않는다.
- `prepare_schema014_migration`이 계획과 카드를 반환한다. 기존 계획은
  `open_schema014_approval(project, target, operation_id)`로 연다. confirm/cancel은 앱 전용이며
  확인값을 모델 응답·URL·로그에 노출하지 않는다. 카드 클릭 자체는 DB를 실행하지 않는다.
- source/plan/SQL digest·런타임·UID·재부팅·만료를 검증하고 중복 클릭·응답 유실·관리자 승인과의
  경합을 처리한다. SDK 검증 전에 잘못된 확인 인자를 차단해 비밀값 반사를 막는다.
- 이미 승인된 미사용 영수증은 재발급하지 않고 검증 결과와 정확한 다음 실행 도구를 반환한다.
  이미 진행·종료된 작업에는 새 승인·재실행 동작을 제시하지 않고 상태 조회가 필요하다고 거부한다.
  과거 계획을 다시 열었을 때 승인 URL/터미널 명령이 승인 완료 안내와 함께 남지 않게 했다.
- 실제 최신 계획과 같은 형식의 승인 링크는 loopback·지정 HTTPS에서 모두 200이었다.
  사용자가 받은 실패 주소 자체는 확보하지 못했으므로 특정 쿼리 손상 원인은 확정하지 않았다.
  잘못된 링크는 엄격히 거부하되 JSON 대신 비밀값을 반사하지 않는 복구 안내를 표시한다.
  찾지 못한 계획 대신 다른 계획을 자동 선택하던 동작과 014 설명·후속 실행 도구도 수정했다.
- 실제 tossApp 호스트 실행 29건은 작업 목적이 모두 없었다. 과거 목적을 만들어 넣지 않고
  짧은 실행 ID·안전한 명령 종류·관측 단계·종료 코드·출력량을 구분한다.
  같은 실행의 명시적 업무 설명은 출처와 함께 보존하고 내부 Codex 행동 미관측을 분명히 표시한다.
- 추가 원인은 수집기의 `KNOWN_TOOLS`와 화면의 도구 목록 누락이었다. DB·Schema014·런타임과
  호스트·에이전트 도구를 명시적으로 등록했다. 실제 데이터 비교에서도 이전 파일 읽기 대신
  최신 호스트 작업 조회가 표시된다. MCP 요청 반환은 DB 적용 성공으로 취급하지 않는다.

## 검증과 증거

- JavaScript 전체 217개, 관리 웹 Chromium 전체 96개 통과.
- Schema014 unit + 실제 MCP stdio 14개, 카드 Node 4개·Chromium 4개 통과.
- 변경 Python AST·Ruff와 JS 구문 검사 통과. 독립 검토의 차단 사항을 모두 수정했다.
  마지막 목록 동기화 수정까지 포함한 Python 19개 파일의 AST·Ruff도 다시 통과했다.
- 실제 tossApp 데이터를 사용해 설치본/후보 자산을 비교했다. 1440·390·320px,
  JS 오류 0·로그인 외 쓰기 요청 0이었다. 후보 자산 치환 검증이며 운영 배포 증거는 아니다.
  합성 승인 카드도 같은 세 화면 크기에서 확인했다. 미리보기 캡처는 클릭하지 않았으며,
  승인 동작 테스트는 임시 합성 상태에서만 수행했다. 실제 Schema014 승인·DB 실행은 하지 않았다.
- 첫 사전검사는 검토에서 발견한 진행 상태 오안내를 수정하기 위해 정확한 검사 프로세스만
  중단했다. `ROLLOUT_INTERRUPTED`/130 영수증을 보존했다.
- 다음 사전검사는 대상 703개 중 웹/터미널 control inventory 일치 검사 1개에서 실패했다.
  새 독립 카드 브라우저 검사의 웹 배포 목록 누락을 수정하고 해당 검사를 단독 통과했다.
  실패 영수증을 삭제하거나 성공으로 바꾸지 않았다. 수정 후 공식 사전검사의 화면 검사
  10·4·7개, 대상 Python 703개, 전체 Python 869개가 모두 exit 0으로 완료됐다.
  최종 영수증은 `preflight-verified/rollout-waf9fail/receipt.json`이며
  `installed=false`, `switched=false`, `profile_aligned=true`, `service_aligned=true`다.
  control digest: `31679698a3ac8f158f4cd54aebd32f6e89b8ff5a112192d64424942ba8ddc714`.
- 관리 웹 회귀 중 임시 연결이 닫혀 BrokenPipe 진단이 남았으며 최종 96개는 모두 통과했다.

비공개 증거: `~/.local/state/personal-ops-rollouts/chat-approval-details-20261016/run-1cd0tg7e/`.
주요 파일: `baseline.json`, `source-integration.json`, `live-detail-comparison.json`,
`admin-browser-final.json`, `static-and-node-final.json`, `live-runtime-unchanged.json`,
`preflight/`, `preflight-final/`, `preflight-verified/`, `final-result.json`.
런타임·테스트·문서 30개 경로를 원본과 격리 소스에 일치시켰고 이 인계 문서를 추가했다.
기준선의 나머지 파일 hash/mode와 원본 HEAD/index를 보존했다. `git diff --check`도 통과했다.

## 승인 후 수행한 런타임 서비스 전환

승인된 소스/control 해시를 재확인하고 저장소 루트에서 다음 공식 명령을 실제 PTY로 실행했다.

```bash
/home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  chatgpt-mcp/rollout-host-runtime.py --apply
```

RTK의 출력 파이프 때문에 TTY가 사라지는 기존 문제를 피하도록 소유자 전용 PTY launcher를
별도 transient systemd user service에서 실행했다. 터널 중단으로 launcher가 함께 종료되지
않도록 분리한 것이며 공식 테스트·TTY·`ROLLOUT` 검사를 생략하거나 가짜 grant를 만들지 않았다.
실제 확인 prompt에서 소스/control을 재검증하고 active/startable 기록 0건을 확인한 뒤
이번 사용자 승인에 해당하는 `ROLLOUT`을 전달했다. 전체 출력과 원본 영수증을 보존했다.
기존 터널 unit/profile과 관리 웹의 런타임 경로만 정렬하고 두 서비스를 전환했다.

배포 영수증: `activation/pty-receipts/rollout-hip0nkys/receipt.json`.
실행 중 승인 UI 10·4·7개, 대상 703개, 전체 869개 검사가 다시 exit 0으로 완료됐다.
`installed=true`, `switched=true`, `runtime_verified=true`, `tunnel_healthy=true`,
`admin_status=admin_ready`다. **원본 종료 코드 2 / `verification_required`를 그대로 보존한다.**
이는 서비스 전환 실패가 아니라 실제 같은 ChatGPT 연결의 전체 도구 목록 갱신이 아직
관측되지 않았기 때문이다. passive catalog는 `awaiting_discovery`이며 별도 stdio 결과로 대체하지 않았다.

## 운영 검증과 관측 한계

- 실제 MCP PID `1805851`, tunnel PID `1805819`, 관리 웹 PID `1806260`가 새 snapshot을 실행한다.
  manifest 82개 모두 hash 일치. loopback·LAN·신뢰 HTTPS 자산 12개도 HTTP 200·hash 일치다.
- 설치본의 별도 실제 MCP stdio에서 도구 73개, Schema014 opener와 앱 전용 confirm/cancel,
  카드 HTML 리소스·CSP·두 task_label 스키마를 확인했다. initialize/list/read-resource만 수행했으며
  작업·승인 도구를 실행하지 않았다. 실제 ChatGPT 카드 표시 증거와는 구분한다.
- 실제 플러그인의 `get_host_access(tossApp)` 조회가 성공했다. 현재 권한은 `EXPIRED`다.
  관리 웹에서는 personal-ops=`RUNTIME_CHANGED`, pim-check=`NO_GRANT`도 확인했다.
  기존 권한 파일은 바이트·모드 그대로이며 승인·연장하지 않았다.
- 실제 운영 UI에서 personal-ops/tossApp/pim-check, 런타임 승인 deep link, Schema014의
  잘못된 링크 복구·없는 계획 자동 선택 방지와 캐릭터 상세를 검증했다. 총 18회 화면 크기 검사,
  JavaScript 오류 0·로그인 외 쓰기 요청 0이다. PC와 모바일 캡처를 직접 확인했다.
- 첫 운영 UI 검사는 검증 스크립트의 버튼 선택자 오타(`#host-approve`)로 실패했다.
  원본·진단 실패 기록을 보존하고 실제 `#host-approve-submit`으로 수정해 통과했다.
  제품 코드 수정이나 추가 서비스 재시작은 없었다.
- 문서 갱신 전 저장소 335개 파일·HEAD/index와 호스트 설정/권한 파일을 보존했다.
  승인된 두 서비스의 `20-runtime.conf`만 변경됐다. 컨테이너 11개의 상태·시작 시각도 그대로다.
- 기준선 CLI 9개 중 7개는 동일 PID/시작 시각으로 생존했다. Claude PID `1674091`, `1675509`는
  검증 시 종료 상태였다. 종료 시점·원인은 수집된 자료만으로 확정하지 못했으므로 전체 CLI가
  보존됐다고 주장하지 않는다. 임의 CLI를 종료하는 명령은 실행하지 않았다.
- DB SQL·Docker 변경·실제 Schema014 적용·호스트 권한 재발급·커밋·push는 하지 않았다.

최종 증거는 `activation/activation-result.json`, `installed-verification.json`,
`installed-catalog.json`, `actual-connector.json`, `live-ui.json`,
`installed-detail-verification.json`, `pre-switch-guard.json`, `pty-output.log`에 있다.
모든 기록은 비공개 디렉터리와 소유자 전용 파일로 보관했다.

## 운영 전환 뒤 웹에서 이어갈 순서

1. 관리 대시보드를 새로고침한다. 운영 전환과 서버 등록·실제 UI 검증은 위와 같이 완료됐다.
2. ChatGPT 연결의 도구 메타데이터를 새로고침하고 실제 채팅에서 카드를 확인한다.
   현재 도구로 ChatGPT의 새로고침 버튼을 대신 누를 수는 없다.
3. tossApp/tarororo-test의 Schema014 상태를 읽고, 만료된 기존 계획은 재사용하지 않는다.
   새 계획의 카드에서 변경 내용을 확인한 뒤 사용자가 직접 승인한다.
   범용 호스트 실행이 필요하면 만료된 HostAccess도 기존 채팅 승인 카드에서 별도로 확인한다.
4. 승인 영수증 검증 후 사용자가 실제 적용을 요청한 경우에만 정확한
   `execute_schema014_migration`을 호출한다. 이후 상태로 DB 적용 결과를 확인한다.
5. API 배포와 TEST AIT 업로드는 tossApp 웹 세션의 기존 작업 범위에서 별도로 진행한다.
   실패·결과 불명 상태를 자동 재시도하지 않는다. 승인 응답 유실은 같은 작업 카드를 다시 열어 확인한다.

실제 ChatGPT에서의 신규 카드 표시·사용자 승인과 실제 DB 적용은 아직 미검증/미수행이다.
실제 detached 웹 업데이트의 재시작 후 생존 검증도 기존 인계와 마찬가지로 다음 승인된 업데이트에서 확인한다.
