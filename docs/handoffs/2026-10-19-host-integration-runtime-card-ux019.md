# 런타임 채팅 승인 카드·UX019 조회 통합

## 범위와 운영 기준

2026-10-19 사용자가 남은 호스트 작업의 진행을 요청했다. 원본 저장소의 다른 세션 변경을 보존하고, 커밋 `4b40694`와 남은 두 기능의 변경을 별도 후보에 고정했다.

- 원본: `/home/jhw/ai/opencode/projects/personal-ops`.
- 증거: `/home/jhw/.local/state/personal-ops-host-integration/20261019-qrte2md5/`.
- 최초 후보: 증거 폴더의 `candidate`; 검토 문제를 포함한 기준선으로 보존하며 배포하지 않는다.
- 보완 후보: 증거 폴더의 `candidate-fixed`.
- 전환 전 운영 기준: `880c076789c68bbf71e9`, MCP PID130920, 관리 웹 PID131385.
- 대시보드 5파일, 기존 observer와 별도 Claude 수집기의 고정 경로는 유지한다.
- API·Worker·PostgreSQL·스키마·AIT·기존 완료 업로드는 변경하지 않는다.

## 검토에서 발견한 문제

독립 코드 리뷰와 구조 리뷰에서 다음을 발견했다. 최초 리뷰 결과는 REQUEST CHANGES / BLOCK이며, 보완·재검토 전 배포 승인을 뜻하지 않는다.

1. 승인 후 발급 런타임 또는 부팅이 바뀌어도 승인된 카드를 다시 열거나 실행 대기열에 넣을 수 있었다. 새 immutable 계획 digest에 승인 context(버전·발급 권한 코드 식별자·부팅 UUID·UID)를 포함하고 승인·재열기·실행·worker claim에서 중앙 검증하도록 보완했다. 기존 4필드 영수증, 최초 업그레이드의 old issuer/worker/bootstrap grant는 유지한다. 실패한 claim은 PRECHECK 실패와 코드로 남기고 영수증을 보존한다. pending/approved의 오래된 context와 legacy 무바인딩 계획은 만료 처리하고, 잘못된 새 context나 읽기 실패는 승인으로 복구하지 않는다. 실행 중·완료된 전환의 context는 전환 자체 때문에 바뀔 수 있으므로 재조정하지 않는다.
2. UX019가 기존 UX014 직접 provider를 사용할 수 있었다. 직접 provider는 UX014로 한정하고 UX019 전용 factory를 요구하도록 수정했다. 회귀 2개 RED 확인 후 콘솔 관련 95개가 통과했으며, 독립 재검토에서 해당 finding은 CLOSED다.

## 실제 UX019 조회

2026-10-19 09:44:26 UTC에 고정 후보의 공식 어댑터로 기존 소유자 설정을 사용해 실제 읽기 전용 조회에 성공했다. 운영 MCP의 신규 도구 호출은 아직 아니다. 모델 turn·로그인·업로드를 요청하지 않았다.

- Deployment ID: `01a151bd-4015-75cf-8275-fa6b6aef9a47`.
- `provider_verified=true`, `found=true`, 버전 `20261019-27`.
- `review_status=CREATED`, `is_tested=false`, `test_bundle_ready=false`, `deployed=false`.
- `remote_digest_verified=false`. 원격 해시·실기기 QA 완료로 해석하지 않는다.
- 증거: `actual-ux019-console-read.json`.

## 실제 서비스 기준 조회

2026-10-19 09:52:19 UTC, 운영 MCP의 `get_runtime_status(tossApp, tarororo-test)`에서 API running/healthy, readiness ready, Worker running, PostgreSQL running, Schema013 ready, monetization mode toss-demo를 확인했다. 이 작업에서는 해당 대상에 쓰기를 수행하지 않았다.

## 전환 영향

통합 후보의 백엔드 변경은 HostAccess authority identity를 바꾸므로, 현재 정책상 전환 뒤 기존 호스트 권한은 `RUNTIME_CHANGED`로 재확인이 필요하다. 기존 승인 파일을 임의로 새 identity에 맞추지 않는다. 에이전트 proof의 source-contract 대상과 대시보드 파일은 운영본과 같으며, 이 변경 자체가 해당 proof 소스를 바꾸지는 않는다.

실제 전환 대상은 `personal-ops-tunnel.service`와 `personal-ops-admin.service`뿐이다. 사용자의 별도 서비스 전환 승인 후 2026-10-19 10:18:13 UTC에 두 서비스의 전환과 운영 런타임 검증을 완료했다. ChatGPT 클라이언트의 신규 도구 발견은 아래와 같이 별도 확인 대상으로 남긴다.

## 보완 후 검토와 검사

- 코드 리뷰: 이전 HIGH와 MEDIUM 모두 CLOSED, 코드 리뷰 lane APPROVE.
- 구조 리뷰: 이전 HIGH CLOSED, 최종 WATCH. 통합 판정은 COMMENT이며 전체 merge-ready 승인으로 확대하지 않는다.
- 보완된 승인 backend/card/bootstrap/admin 59/59, MCP router 3/3, 콘솔 95/95 통과. 묶음은 중복되므로 합산하지 않는다.
- Python 3.10의 router 검사에는 asyncio.timeout이 없어 환경 오류가 있었으며, 운영 공식 선택 인터프리터 Python 3.12.12에서 다시 실행한 검사를 근거로 한다.
- Ruff·Python compile·diff 공백 검사 통과. 최종 공식 검사에서 관련 884개·전체 1100개를 집계했고, 승인 UI 검사 5종을 포함한 모든 검사 명령의 종료 코드는 0이다. 중복 묶음을 합산하거나 별도 보존되지 않은 skipped 개수를 주장하지 않는다.
- 최초 기준 후보 `90beee2b64ac50c57530`의 공식 검사(대상 870개·전체 1086개 및 승인 UI 5종)는 성공했지만, 검토 문제를 포함하므로 배포 후보로 사용하지 않는다.
- 최종 후보: `788381864215c1494db0`, runtime 91파일, 운영 대비 기존 8개 변경·2개 추가·삭제 없음. 대시보드 5개는 운영과 바이트가 같다.
- 최종 source digest: `364c433b528faf37b06d297cafa8d9a68cc04acd29b74f4aa5a607c9b4e144fd`.
- 최종 control digest: `a97a76ca147a49aa46158a2a236b93fec8ea07c64a73a6faf0efe7579c4ce02c`.
- 공식 실행 영수증: `final-rollout-receipts/rollout-0htvj2rd/receipt.json`. 2026-10-19 10:08:25 UTC에 최종 사전 검사를 완료했고, 운영 기준 snapshot/PID 및 터널·관리 웹 정합성을 확인했다. 사용자의 별도 승인과 전환 직전 재검증 후 TTY에 ROLLOUT을 입력해 설치·전환했다.
- 독립 전환 전 보존 검증은 PASS다. 전환 직전 10:16:09 UTC에 jobs 652개·호스트 실행 176개·호스트 명령 9개·에이전트 작업 0개를 메타데이터로 조사했으며 활성 작업과 조사 오류가 없었다. source/control digest 및 기존 운영 snapshot/PID도 재확인했다.

## 구조 리뷰 WATCH의 정확한 범위

계획은 승인한 정확한 target을 고정하며, 승인 당시 active alias를 선행 조건으로 고정하는 정책은 아니다. issuer identity는 승인 발급·검증 모듈의 권한 코드 식별자다. worker claim 후 다른 승인된 전환이 먼저 끝났다면, worker가 새로 관측한 live baseline 위에 승인된 target을 적용할 수 있다. 전역 전환 lock과 마지막 관측 baseline의 PID·설정 CAS는 기존대로 유지된다. 이것을 모든 active alias 변경 시 자동 승인 취소를 보장하는 기능이라고 표현하지 않는다.

이 경계는 기존 exact-target-only 계약의 WATCH이며 이번 두 기능의 blocker는 아니다. 이번 터미널 최초 전환은 ROLLOUT 입력 직전에 실제 live baseline이 여전히 `880c076789c68bbf71e9`인지 별도로 확인한 뒤 수행했다.

## 실제 전환과 검증 결과

- 소스 커밋: `a27f87defbe2d4bf6fbe4ee64200185003cb1482`.
- 운영 snapshot: `788381864215c1494db0`, MCP PID1631490. 영수증의 installed/switched/runtime_verified/tunnel_healthy는 모두 true, 관리 웹은 admin_ready다.
- 기존 CLI 세션 9개는 동일 프로세스로 보존됐고 변경된 세션은 0개다. observer PID3208094와 고정된 Claude 수집기 및 그 설정은 유지됐다.
- 정식 실행기는 exit 2 / verification_required를 반환했다. 설치 실패가 아니라 ChatGPT의 새 도구 목록을 기다리는 awaiting_discovery 상태다. 10:19:51 UTC의 수동 개입 없는 터널 상태 조회에서도 발견 대기 상태가 유지됐다. 영수증을 임의로 성공으로 덮어쓰지 않았다.
- 설치된 실제 server.py를 별도 MCP SDK 세션과 격리된 상태 디렉터리로 실행해 도구 81개를 확인했다. 운영 터널의 stdio에는 접근하지 않았다. `get_toss_test_console_status`, `get_toss_ux019_test_console_status`, `open_runtime_rollout_approval`, `confirm_runtime_rollout_approval`, `cancel_runtime_rollout_approval`의 등록을 확인했다. 카드 열기는 model 전용, 승인·취소는 app 전용이며 HTML 리소스가 설치본과 일치했다. 증거: `installed-protocol-smoke.json`.
- 같은 SDK 세션에서 UX019 신규 도구를 실제 호출했다. provider_verified=true이며 고정 deployment ID와 버전 20261019-27을 확인했다. test_bundle_ready=false, is_tested=false, deployed=false, remote_digest_verified=false가 유지됐다. 업로드나 모델 실행은 요청하지 않았다.
- 실제 운영 터널의 기존 `get_runtime_status` 호출도 성공했다. API·Worker 이미지 ID는 전환 전과 같고 API healthy/readiness ready, Worker/DB running, Schema013 ready, toss-demo 상태를 유지했다.
- 실제 관리 웹의 LAN 주소에서 wlan-package·personal-ops·pim-check·tossApp 선택과 live 상태를 검사했다. 앞의 세 프로젝트는 실제 수집 기록을 읽었고 tossApp의 이 activity 조회 결과는 0개였다. 이벤트를 주입하지 않았다. wlan-package 화면을 1440/390/320 폭에서 검사·캡처했고 가로 넘침과 브라우저 오류가 없었다. LAN/loopback의 JS·CSS 3종은 설치본 해시와 같았다. 증거: `live-ui.json`, `live-wlan-*.png`.
- 보호한 호스트 설정·승인 파일 11개와 별도 WIP 11개는 바이트·모드가 유지됐다. 통합 코드 25파일도 동결 후보와 같았다. 증거: `post-switch-preservation.json`.

## 웹 채팅에서 이어갈 일

1. personal-ops-tunnel의 플러그인 연결을 한 번 새로고침해 신규 도구 목록을 갱신한다. 설치본 등록 검증과 ChatGPT 화면에서의 카드 표시 검증은 구분한다. 현재 대화의 도구 목록에는 아직 신규 도구가 없어 실제 ChatGPT 카드 표시·클릭 검증은 남아 있다.
2. 기존 운영 도구 `get_host_access(tossApp)`로 active=false / RUNTIME_CHANGED를 확인했다. 백엔드 권한 코드 변경으로 이번에는 호스트 권한 카드에서 한 번 재승인해야 한다. 저장된 승인 파일을 수정하거나 권한을 임의로 되살리지 않았다. 단순 채팅·플러그인 새로고침은 자체적으로 유효한 권한을 만료시키거나 연장하지 않는다.
3. 이후 UX019는 `get_toss_ux019_test_console_status`로 조회한다. 기존 UX014 조회는 기존 도구를 사용한다. 현재 결과는 번들 준비 완료·실기기 QA·심사 승인·원격 해시 일치를 뜻하지 않는다.
4. 실제 런타임 전환이 필요할 때만 유효한 정확한 계획을 만들고 `open_runtime_rollout_approval`로 채팅 승인 카드를 연다. 이번 검증에서는 추가 계획 승인·실행, Docker/DB/스키마/AIT/업로드 변경을 수행하지 않았다.
