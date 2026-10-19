# Toss Console 읽기 전용 MCP — 실제 호스트 SDK 및 공개 OAuth 사전 점검

검증일: 2026-10-17 KST
프로젝트: `personal-ops` (Toss 대상 `tossApp/tarororo-test`)
상태: **새 Toss 콘솔 read-only MCP 도구의 호스트 stdio 라우터 1/1 PASS, 전체 라우터 파일수정 회귀 1건 재현 실패로 롤아웃 차단**. 운영 변경 없음.

## 권한과 안전 범위

- 채팅 사용자 요청: `personal-ops` 호스트 액세스, Codex 사용 금지, 30분 동안 실제 MCP SDK 테스트 및 Toss 콘솔의 읽기 전용 사전 점검만 진행.
- 승인 카드 `b80523e1538fa5d41a3a0803c026b8ca`, 실제 grant `ac4db152c913d841ce934ceba22cac80`, `allow_codex=false`, `scope=installation`.
- **주의: 실제 승인 설정은 요청한 30분이 아닌 8시간**으로 발급됐으며 `expires_at=2026-10-17T15:30:35.622699Z` (2026-10-18 00:30:35 KST). 사용자에게 정확히 알림. 테스트 자체는 요청 범위로만 수행. 권한을 축소하거나 강제로 취소하는 명령은 실행하지 않음.
- 어떠한 OAuth 로그인, authorization code flow, 브라우저 로그인, secret/token 읽기·출력, 일반 MCP POST, AIT 업로드, API·Worker·DB 변경, 서비스 재시작, Codex 호출도 실행하지 않았음.
- 실제 호스트 SDK 테스트가 생성한 하위 MCP 프로세스는 `tempfile.TemporaryDirectory`의 임시 프로젝트만 사용. 설치된 `personal-ops-tunnel.service`나 Admin 서비스는 변경하지 않음.

## A. 실제 MCP SDK 라우터 성공 증거

- 최초 직접 실행 `start_host_execution` executable `~/.local/share/uv/tools/serena-agent/bin/python`, `-I` 실행은 실제 python venv symlink가 안전 검증 중 기본 interpreter로 resolve되어 `sys.prefix`가 base Python, `mcp_available=false`였음. Host op `080803a35fe17c14ecf22c5f185457a0`, exit4.
- Read-only 진단 op `dcb49a69b21c1773a0ac935551142ed7`: venv `~/.local/share/uv/tools/serena-agent/lib/python3.12/site-packages/mcp` 디렉토리 확인. 정상 경로 호출은 `/usr/bin/env` → 원래 venv python symlink 유지.
- venv sanity op `19a15650f397b74c61f49c5998a2cba9`: 실제 `sys.prefix=~/.local/share/uv/tools/serena-agent`, `mcp_available=true`.
- **실제 호스트 stdio MCP SDK 테스트**: op `670659cec5708ec95740c0393a764772`, `/usr/bin/env ~/.local/share/uv/tools/serena-agent/bin/python -m unittest -v tests.test_host_runtime_router` (cwd chatgpt-mcp); `succeeded/COMPLETE`, exit0, `Ran 1 test in 0.935s; OK`.
- 테스트가 확인하는 사항: 새로운 `get_toss_test_console_status`가 model-only readOnly 스키마로 등록되고 프로젝트/tarororo-test 고정, OAuth 부재에서 `CONSOLE_OAUTH_NOT_CONFIGURED`, `provider_verified=false`, `test_bundle_ready=false`. 기존 host tools 병존, cross-project 거절, 메타 데이터 보고.
- 이 테스트가 완료되었다고 원격 Toss 콘솔 로그인/상태를 검증했다고 주장하지 않음.

## B. 확장 통합 회귀 실패 (운영 롤아웃 차단)

- 6개 unittest 모듈 실행 op `2329f8453dd803a23c5c02215f6683ba`, 종료 code1, `Ran 52 tests in 13.418s, FAILED (errors=1)`.
- 실패: 기존 `tests.test_router.MultiProjectRouterTest.test_routes_each_call_and_contains_paths`; 임시 alpha/edit.txt `replace_content` 호출 후 expected `alpha version 2`가 아닌 기존 `alpha version 1` 유지.
- 원인 로그: **Serena Python language server manager 초기화 실패**, 언어 서버 프로세스가 초기화 중 비정상 종료; 도구 결과에 오류 텍스트가 포함돼도 `isError`가 false로 처리되어 테스트의 이후 파일 내용 비교에서 실패. 신규 Toss 콘솔 read-only 분기 자체의 실패는 아님.
- 단독 재현 op `737ee6510882c897000c3209817c9ae5`, `Ran 1 test in 9.673s; FAILED (errors=1)`, 동일 파일내용 불일치. **2회 재현**, 단순 일회성이라고 간주하지 않음.
- 이 문제의 수정, 언어 서버 재설치, 의존성 변경, 운영 롤아웃은 모두 실행하지 않았음.
- 선행 `2026-10-17` 격리 source-only 50/50 PASS job `6c303f0b86ad4aca8a79b8a5dd26b2fb`는 별도; 52 중 한 건 실패를 덮어쓰는 근거로 사용하지 않는다.

## C. 공식 Toss OAuth 공개 메타데이터만 GET (인증 없음)

- 공식 harness: https://github.com/toss/apps-in-toss-harness
- 콘솔 MCP URL: `https://mcp.toss.im/adapters/apps-in-toss-console/mcp` (접속 엔드포인트)
- OAuth 공개 리소스 root 및 path metadata: TLS 확인 + HTTPS GET 200, JSON 객체 확인, op `688f97a93de339a776eb61bf75d38e25`.
- OAuth 공개 authorization server metadata: HTTPS GET 200, `issuer=https://mcp.toss.im`, `code_challenge_methods_supported`에 `S256`, `registration_endpoint` 없음, op `52be493a7a83bee9d23cfecdb4555c9b`.
- Sanitized public URL confirmation op `5560c671a5afac8d2831ff60f12d993e`:
  - `resource=https://mcp.toss.im` (MCP 접속 URL **전체와 다름**).
  - `authorization_servers=[https://mcp.toss.im]`.
  - `authorization_endpoint=https://business-accounts.toss.im/oauth2/authorize`.
  - `token_endpoint=https://business-accounts.toss.im/oauth2/token`.
  - Query/fragment 없음, public server-only metadata. **어떠한 authorization/token endpoint에도 요청하지 않았음**.
  - 공식 harness는 static OAuth client_id `mcp-gateway`가 필요하고 DCR은 제공하지 않는다고 안내.
- 콘솔 MCP 엔드포인트에 대한 비인증 `HEAD` 요청은 HTTP 404였으나, 서버가 `HEAD`를 지원하지 않을 수 있으므로 **MCP 서비스 부재라고 주장하지 않음**. `POST tools/list`·OAuth 인가는 요청 범위 밖이어서 실시하지 않음.
- 모델/인증정보 없이 원격 `bundle_list` 스키마나 실제 `CREATED/isTested`를 알 수 없음.

## D. Toss TEST 서버 재확인 및 기존 롤아웃

- 2026-10-17 16:41 KST 전용 읽기 전용 상태: API running/healthy, image `sha256:84aa9daaebc456014c0ac5d63c333905596a160cbb3397b3146e5fce191386e5`, `toss-demo`; Worker image `sha256:27bfa91feaf9f3355addece8ed337a533611e5a70f25ac187b67bdebb7610b23` running; DB running.
- Schema014 ready / checksum_match, `users=3,persons=17,readings=21`; 서비스 재시작 없음.
- UX014 AIT `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`, deploymentId `01a13e37-27f3-7f9c-923d-6dbd690a5a09`: 기존 CLI+로컬 영수증 success, provider-side 배포 상태 미확인.
- 이전 prepped rollout `71001541a10dc142305f3001f04cbd2f`, target_snapshot `7ed51c90eb1a34a631a6`. 적용 TTL 2026-10-17 16:07:13 KST 경과, get state `pending_host_approval`이지만 `started_at=null`, 실사용/승인/전환 기록 없음. 새 적용에는 새 계획·독립 검토·명시 승인 필요.

## 다음 단계

1. **Serena 언어 서버/기존 파일 수정 라우터 테스트 실패 원인 분리** (LSP stderr, temp 실행 환경, 기존 버전의 계약). 코드·패키지 변경은 별도 범위 승인 후; 전면 runtime rollout은 정식 테스트 PASS 전까지 보류.
2. 공식 Toss 콘솔 OAuth를 사용자가 직접 인가한 다음, trusted integration에서 `bundle_list`/`miniapp_get_status`의 실제 schemas를 확인. 다른 도구의 OAuth 토큰을 복사하거나 노출하지 않음.
3. 정확한 OAuth `resource`와 issuer/authorization host 분리, client id `mcp-gateway`, PKCE S256, no redirect/strict callback validation 등 검증해 read-only provider adapter 구현.
4. 신규 도구의 `provider=None`는 OAuth를 자동 연결하지 않음. 운영 런타임 교체를 먼저 해도 Toss 빌드 상태 조회가 가능해지지 않는다.
5. 검증 완료 후 owner-approved immutable rollout (개발 WIP 205 files 포함하는 것이 적절한지 별도 검토). 실제 Toss TEST provider CREATED/isTested와 Native WebView QA는 이 단계 뒤 별도.

안전 금지: OAuth 로그인 자동 진행/토큰 재사용, Codex 실행, AIT 재업로드, 실제 API·Worker·DB 변경, 개발 서비스 재시작, Git reset/stash/commit/push/강제 stage.
