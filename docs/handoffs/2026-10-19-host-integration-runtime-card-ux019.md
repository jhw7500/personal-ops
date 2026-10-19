# 런타임 채팅 승인 카드·UX019 조회 통합

## 범위와 운영 기준

2026-10-19 사용자가 남은 호스트 작업의 진행을 요청했다. 원본 저장소의 다른 세션 변경을 보존하고, 커밋 `4b40694`와 남은 두 기능의 변경을 별도 후보에 고정했다.

- 원본: `/home/jhw/ai/opencode/projects/personal-ops`.
- 증거: `/home/jhw/.local/state/personal-ops-host-integration/20261019-qrte2md5/`.
- 최초 후보: 증거 폴더의 `candidate`; 검토 문제를 포함한 기준선으로 보존하며 배포하지 않는다.
- 보완 후보: 증거 폴더의 `candidate-fixed`.
- 운영 기준: `880c076789c68bbf71e9`, MCP PID130920, 관리 웹 PID131385.
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

실제 전환 대상은 `personal-ops-tunnel.service`와 `personal-ops-admin.service`뿐이다. 현재 문서는 준비 중인 상태이며 실제 서비스 전환 완료를 주장하지 않는다.

## 보완 후 검토와 검사

- 코드 리뷰: 이전 HIGH와 MEDIUM 모두 CLOSED, 코드 리뷰 lane APPROVE.
- 구조 리뷰: 이전 HIGH CLOSED, 최종 WATCH. 통합 판정은 COMMENT이며 전체 merge-ready 승인으로 확대하지 않는다.
- 보완된 승인 backend/card/bootstrap/admin 59/59, MCP router 3/3, 콘솔 95/95 통과. 묶음은 중복되므로 합산하지 않는다.
- Python 3.10의 router 검사에는 asyncio.timeout이 없어 환경 오류가 있었으며, 운영 공식 선택 인터프리터 Python 3.12.12에서 다시 실행한 검사를 근거로 한다.
- Ruff·Python compile·diff 공백 검사 통과. 최종 공식 검사는 완료됐으며 승인 입력을 기다린다. 관련 884개·전체 1100개를 집계했고, 승인 UI 검사 5종을 포함한 모든 검사 명령의 종료 코드는 0이다. 중복 묶음을 합산하거나 별도 보존되지 않은 skipped 개수를 주장하지 않는다.
- 최초 기준 후보 `90beee2b64ac50c57530`의 공식 검사(대상 870개·전체 1086개 및 승인 UI 5종)는 성공했지만, 검토 문제를 포함하므로 배포 후보로 사용하지 않는다.
- 최종 후보: `788381864215c1494db0`, runtime 91파일, 운영 대비 기존 8개 변경·2개 추가·삭제 없음. 대시보드 5개는 운영과 바이트가 같다.
- 최종 source digest: `364c433b528faf37b06d297cafa8d9a68cc04acd29b74f4aa5a607c9b4e144fd`.
- 최종 control digest: `a97a76ca147a49aa46158a2a236b93fec8ea07c64a73a6faf0efe7579c4ce02c`.
- 공식 실행 영수증: `final-rollout-receipts/rollout-0htvj2rd/receipt.json`. 2026-10-19 10:08:25 UTC에 최종 사전 검사를 완료했고, 운영 기준 snapshot/PID 및 터널·관리 웹 정합성을 확인했다. 명령의 apply 모드는 검사 후 TTY의 ROLLOUT 입력으로 설치·전환을 구분한다. 현재 해당 입력을 보내지 않아 설치·전환은 하지 않았다.
- 독립 전환 전 보존 검증과 최종 서비스 전환 승인이 남아 있다. 10:03:17 UTC의 메타데이터 조사에서는 jobs 652개·호스트 실행 176개·호스트 명령 9개 중 queued/running/canceling 작업은 없었다. 실제 전환 직전 다시 확인한다.

## 구조 리뷰 WATCH의 정확한 범위

계획은 승인한 정확한 target을 고정하며, 승인 당시 active alias를 선행 조건으로 고정하는 정책은 아니다. issuer identity는 승인 발급·검증 모듈의 권한 코드 식별자다. worker claim 후 다른 승인된 전환이 먼저 끝났다면, worker가 새로 관측한 live baseline 위에 승인된 target을 적용할 수 있다. 전역 전환 lock과 마지막 관측 baseline의 PID·설정 CAS는 기존대로 유지된다. 이것을 모든 active alias 변경 시 자동 승인 취소를 보장하는 기능이라고 표현하지 않는다.

이 경계는 기존 exact-target-only 계약의 WATCH이며 이번 두 기능의 blocker는 아니다. 현재 터미널 최초 전환은 ROLLOUT 입력 직전에 실제 live baseline이 여전히 `880c076789c68bbf71e9`인지 별도로 확인하고, 달라졌으면 중단하고 다시 대조한다.
