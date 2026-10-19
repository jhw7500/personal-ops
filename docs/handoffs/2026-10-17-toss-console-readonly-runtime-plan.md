# Toss TEST 콘솔 읽기 전용 MCP — 2026-10-17 런타임 적용 준비

프로젝트: personal-ops / tossApp (tarororo-test)
상태: SOURCE_ONLY, 실제 OAuth 미연결, Toss provider 미조회, 런타임 미전환

## 현재 적용 상태 (직접 조회)

- 2026-10-17 15:52 KST 읽기 전용 조회: tarororo-test API running/healthy, monetization_mode=toss-demo, Worker running, DB running. Schema 014 ready, checksum_match, users=3, persons=17, readings=21.
- AIT 검증 대상: tarororo, deploymentId `01a13e37-27f3-7f9c-923d-6dbd690a5a09`, artifact SHA256 `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`. 기존 CLI 업로드 성공 및 로컬 영수증 일치, 공식 Toss provider CREATED/isTested 상태는 **미확인**.
- 기존 personal-ops/ tossApp working tree는 다른 에이전트 작업을 포함해 dirty. Git reset, stash, stage, commit, push 안 함.

## 콘솔 MCP 공식 참조 확인

- 공식 harness: https://github.com/toss/apps-in-toss-harness
- MCP endpoint: `https://mcp.toss.im/adapters/apps-in-toss-console/mcp`.
- OAuth (RFC 9728), static client ID `mcp-gateway`, DCR 미지원. SSO 인증·권한 승인은 반드시 사용자 본인이 수행해야 함.
- 원격 읽기 전용 대상 도구: `bundle_list`, `miniapp_get_status`. 업로드/초대/검수/릴리즈 도구를 호출하거나 일반 콘솔 tool proxy를 노출하지 않음.
- **공식 응답 스키마/페이지네이션은 연결 후 tools/list/실제 인증 응답으로 별도 확인 필요.** 현 프로젝트에서 가정한 필드명이 실제와 일치한다고 단정하면 안 됨.
- Claude/Codex 기존 OAuth 토큰·브라우저 프로필·개인 API 키를 ChatGPT plugin 또는 다른 런타임에 복사하지 않음.

## 현재 소스 작업 상태

- `chatgpt-mcp/toss_console_evidence.py`: 정확한 workspace 93125 / miniapp 75257 / deployment ID 외 거부, 불완전·중복 응답은 fail closed.
- `chatgpt-mcp/toss_console_tools.py`: `get_toss_test_console_status(project=tossApp,target=tarororo-test)` 읽기 전용 모델, provider=None이면 `CONSOLE_OAUTH_NOT_CONFIGURED` / test_bundle_ready=false 반환. 실제 OAuth 구현·MCP 연결 미완료.
- `chatgpt-mcp/server.py`, `project_state.py`, `install-runtime.py`, `host_runtime_operations.py`, `rollout-host-runtime.py` 등 소스 배선. 설치된 immutable runtime은 변경하지 않음.
- 2026-10-17 isolated test job `6c303f0b86ad4aca8a79b8a5dd26b2fb`: 관련 단위 테스트/설치/롤아웃 회귀 `50/50 PASS`, exit_code=0, 2.555s.
- isolated Python SDK 환경 probe `507eb23f238443829a952f79a878a696`: /usr/bin/python3, `mcp_available=false`, `uv=null`. `test_host_runtime_router`의 실제 stdio MCP 통합 테스트는 미실행; 단위 테스트와 구별할 것.

## 준비한 런타임 **계획** (실행·승인하지 않음)

- `prepare_runtime_rollout` operation `71001541a10dc142305f3001f04cbd2f`
- frozen source_digest `97a503457c5db0a1a11077489f06136cd031edd9be407847b654663c4da3d3d8`
- target_snapshot `7ed51c90eb1a34a631a6`
- source_files 205, source_bytes 3,307,242. 이 스냅샷은 해당 시점의 **전체 공유 personal-ops WIP**를 포함하고 있으며 새 도구만 독립 배포하는 것은 아님.
- phase APPROVAL / `pending_host_approval` / `approval_required=true`, started_at=null / finished_at=null.
- expires 2026-10-17 16:07:13 KST. 만료 후에는 새로운 request_id로 다시 준비해야 함.
- 적용하면 `personal-ops-tunnel.service`, `personal-ops-admin.service`가 재시작되고 진행 중 다른 업무에 영향을 줄 수 있으므로, 먼저 독립 코드 리뷰 + 실제 호스트 SDK/보호장치 회귀 + 사용자 별도 승인 필요.
- **현재 신청한 승인 영수증 없음, execute_runtime_rollout 호출 없음, 시스템 재시작 없음.**

## 후속 우선순위 및 게이트

1. 현재 owner 관리 설정에서 OAuth 전용 연결을 준비하고, 사용자가 SSO/권한 승인한다. 승인 전에 어떤 토큰도 읽거나 웹 인증을 시도하지 않음.
2. 외부 공식 console MCP의 실제 list_tools/schema를 안전하게 수집하고, owner-reviewed snapshot hash·입력 스키마/페이지네이션 및 read-only `bundle_list` 결과 구조를 확인한다.
3. 검증된 인증 컨텍스트의 원본과 audience를 보장하는 trusted Streamable HTTP provider adapter를 구현한다. 현재 source tool의 `VerifiedProviderContext` 불리언만으로 실 인증을 증명했다고 주장하지 않는다.
4. 호스트 측 실제 MCP SDK Python interpreter로 `test_host_runtime_router`와 full official regression/preflight를 돌리고, 스냅샷 전체 WIP 영향 확인. 필요한 경우 새로운 롤아웃 계획으로 교체.
5. 별도 사용자 승인 후 immutable runtime 반영, plugin catalog refresh/new chat, `get_toss_test_console_status`는 provider 未연결일 때 차단되는지 확인.
6. 정식 Toss 콘솔에서 정확한 deployment ID의 CREATED/isTested/deployed 상태 확인 후 Native Toss WebView 직접 QA. Provider remote SHA가 지원되지 않으면 `remote_digest_verified=false` 유지.

금지 작업: API/Worker/DB 재시작·쓰기, AIT 재업로드, Codex 실행, OAuth 토큰 재사용/노출, 실광고·결제·유료 Gemini, 운영 검수·출시, 다른 WIP Git 정리.
