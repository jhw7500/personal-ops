# Apps in Toss 콘솔 read-only 조회 브리지 — 1차 구현 및 안전 게이트

작성: 2026-10-16 KST
리포지토리: personal-ops (tossApp 조회 도구용)
현재 상태: **응답 검증 로직 / 단위 테스트만 구현 완료. 운영 MCP 등록 및 실제 Toss OAuth 연결은 하지 않음.**

## 목표와 현재 증거

- 사용자 대상: `tossApp`, `tarororo`, workspace ID `93125`, miniapp ID `75257`.
- 반드시 검증할 신규 AIT deployment ID: `01a13e37-27f3-7f9c-923d-6dbd690a5a09`.
- 기존 AIT SHA256: `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`.
- 업로드 기록: `b7d87e927399d2da9e4d82758a3a1194` succeeded / COMPLETE / exit0 / 종료 확인. 로컬 receipt는 cli_reported_complete, 원격 digest 미확인.
- 2026-10-16 20:42 KST 실제 `get_schema014_migration_status`: ready, 014 checksum_match, 기존 data users=3/persons=17/readings=21.
- `get_runtime_status`: API healthy/toss-demo; Worker/DB running.
- 외부 콘솔에서 정확한 번들이 CREATED/isTested인지에 대한 OAuth 인증 응답은 아직 없다.

## 확인한 공식 연결

- 공식 Apps in Toss harness: https://github.com/toss/apps-in-toss-harness
- console MCP endpoint: `https://mcp.toss.im/adapters/apps-in-toss-console/mcp`
- 공식 문서에서 확인된 조회 도구 이름: `bundle_list`, `miniapp_get_status`.
- 인증: OAuth (RFC 9728), static client id `mcp-gateway`; 사람의 인증·인가가 필요하다.
- 현재 ChatGPT 연결의 `personal-ops-tunnel` 도구 목록에는 위 콘솔 조회 도구가 없으며, 등록형 Toss 콘솔 OAuth connection도 독립적으로 확인되지 않았다.

## 이번에 신규 생성한 코드 — 운영 미반영

1. `chatgpt-mcp/toss_console_evidence.py`
   - 순수 Python, 네트워크/OAuth/파일 I/O/DB 호출 없음. **절대 원격 API를 호출하지 않는다.**
   - 주입된 *신뢰할 수 있는 별도 authenticated provider call* 응답을 안전하게 요약하기 위한 내부 함수 `project_bundle_list`만 구현.
   - toolName `bundle_list`, 정확한 workspace/miniapp/deployment ID 이외에는 거절.
   - 중복 deployment ID, 불완전/알 수 없는 구조, 잘못된 앱명, 잘못된 형식의 상태값은 fail closed.
   - 명시적 `reviewStatus=CREATED` + `isTested=true` + `deployed=false`일 때만 `test_bundle_ready=true`를 반환.
   - 공급자가 remote binary SHA를 제공하지 않으면 `remote_digest_verified=false` 유지.
   - provider 응답의 비밀/개인 데이터나 원시 요청을 ChatGPT로 전달하지 않는다.
   - **주의:** 이 순수 함수를 호출하는 것만으로 provider 인증이나 원격 출처가 입증되지 않는다. 런타임은 반드시 trusted OAuth MCP ClientSession의 실제 결과만 전달해야 한다.
2. `chatgpt-mcp/tests/test_toss_console_evidence.py`
   - 11개 단위 테스트, 정상/잘못된 ID/중복/BUILD_FAILED/배포됨/boolean 위조/비밀 필드 차단 포함.
   - 실행: `python3 -m unittest discover -s tests -p test_toss_console_evidence.py -v` from `chatgpt-mcp`.
   - 2026-10-16 isolated final job `cc856e76e763434194f605a2ffe0ec69` exit 0, **12/12 PASS** (provider 오류를 번들 미발견으로 오판하지 않도록 추가 테스트).
   - 테스트는 모의 provider payload에 대한 로직 검증이지 실제 콘솔 조회 테스트가 아님.

## 후속 통합 게이트 (미실행)

1. *사람이* Toss SSO/OAuth를 승인하고 서버가 진짜 `apps-in-toss-console` MCP로 로그인된 상태를 확보. 토큰을 코드, 채팅, 저장소 또는 로그에 노출하지 않는다. Claude/Codex 세션에 저장된 별도 OAuth 토큰을 무단 복사하지 않는다.
2. 인증된 MCP `tools/list` 결과로 `bundle_list`·`miniapp_get_status`의 **실제 스키마/리턴 구조**를 읽고, 현 정규화 코드의 가정(`bundles`, `reviewStatus`, `isTested`, `deployed`, `versionName`)을 실제 응답에 맞게 검증한다. 무단 스키마 변환이나 임의 fallback 금지.
3. trusted transport는 고정 HTTPS origin, TLS 검증, OAuth audience, workspace/miniapp/ID allowlist, no redirect, 응답 크기 제한, timeout, API tool allowlist 2개만 허용. 읽기 도구 외 `bundle_upload`, `bundle_upload_complete`, 검수, 릴리즈는 노출하지 않는다.
4. MCP router의 읽기 전용 `get_toss_test_console_status(project=tossApp,target=tarororo-test)` 도구 연결은 source review/격리 테스트/실제 installed SDK roundtrip 후 별도 사용자 승인하에 immutable rollout. 기존 런타임을 직접 덮어쓰거나 다른 CLI 세션을 중단하지 않는다.
5. `bundle_list`에서 위 정확한 deployment ID만 조회, `miniapp_get_status`와 교차 검증. 원격 해시가 없다면 `remote_digest_verified=false` 유지하고, test ready와 phone QA를 별개로 보고한다.
6. 새 관리/콘솔 연결이 승인되지 않으면 `CONSOLE_OAUTH_NOT_CONFIGURED` 또는 `PROVIDER_STATUS_UNVERIFIED`처럼 명확한 차단 상태를 반환. CLI 성공을 콘솔 성공으로 승격시키지 않는다.

## 공유 DB 실 API 검증 방침

- 현재 `server/src/routes/persons.ts`는 POST·GET·PATCH를 제공하며 인물 DELETE API가 없다. 따라서 실서버에 테스트 인물을 생성하면 API만으로 원복할 수 없다.
- 본인 명의 독립 TEST 계정, 테스트 전후 정리 권한 및 별도 승인 없이 `POST/PATCH`를 실행하지 않는다.
- 기존 격리 PostgreSQL/Fastify 통합 검증 115/115 PASS, UI E2E 96/96 PASS, 실 API GET /health/ready 200 및 미인증 GET /v1/persons 401 검증까지 완료.
- 실기기 Toss Native WebView 동작은 실제 기기/동일 deployment ID에서 확인해야 하며 브라우저 E2E PASS로 대체하지 않는다.

## 명확한 범위

- 실제 Toss Provider/MCP OAuth 호출: **없음**
- 신규 MCP 도구를 운영 환경에 등록/설치/재시작: **없음**
- 실제 Toss AIT 재업로드/초대/심사/정식 출시: **없음**
- API·Worker·DB 변경/재시작, Git commit/push, Codex 호출: **없음**
- 이번 작업의 수정 파일은 `chatgpt-mcp/toss_console_evidence.py` 및 테스트 1개와 이 문서뿐.
