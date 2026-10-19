# tossApp UX014 콘솔 MCP 읽기 전용 도구 — 소스 배선 완료 / 운영 미설치

Date: 2026-10-16 KST
Target: `tossApp` / `tarororo-test` / `tarororo`
Status: **소스 수준 라우터+오프라인 검증 완료. 실제 OAuth 연결·공식 콘솔 상태 조회·운영 런타임 롤아웃 미실행.**

## 현재 검증된 TEST 환경

- Schema 014: `ready`, 정확한 체크섬 일치; TEST API `healthy`, `toss-demo`. Worker·DB running.
- 업로드된 후보 AIT SHA256 `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`.
- 검증 대상 Deployment ID `01a13e37-27f3-7f9c-923d-6dbd690a5a09`.
- 공식 Toss 원격 콘솔 `reviewStatus=CREATED`, `isTested=true`, `deployed=false`에 대해서는 아직 인증된 응답을 받지 못함. 로컬 업로드 CLI/영수증 성공만 확인됐고 provider-side SHA는 미확인.
- 현재 `get_host_access(tossApp)`: `active=false`, reason `EXPIRED`. 새 권한이나 Codex 권한 요청/발급 없음.

## 공식 근거

- Toss 앱인토스 공식 harness: https://github.com/toss/apps-in-toss-harness
- OAuth 콘솔 MCP: `https://mcp.toss.im/adapters/apps-in-toss-console/mcp`
- Client ID: `mcp-gateway` (별도 사람의 OAuth 인가 필요, Dynamic Client Registration 미지원 안내).
- 원하는 공식 조회 도구: `bundle_list`, `miniapp_get_status`; 업로드·발행 도구 사용 금지.
- 실제 provider tool inputSchema/responseSchema 및 OAuth 스코프는 연결된 세션에서 검증 전까지 불확실. 절대 모델이 임의로 인자·결과를 꾸며 호출하지 않는다.

## 새 파일

- `chatgpt-mcp/toss_console_evidence.py`: 원격 번들 응답을 파싱하는 순수 함수. 고정 workspace=93125, miniapp=75257, deploymentId를 강제하며 다른 IDs, 중복, 미검증 값, 오류를 성공으로 오판하지 않음. 현재 parser의 `bundles/items` 등의 필드 가정은 **공식 응답 검증 전** 상태.
- `chatgpt-mcp/toss_console_tools.py`: source-only MCP `get_toss_test_console_status(project=tossApp,target=tarororo-test)` 하나의 스펙 및 클래스. 공개 도구 인자로 raw payload·OAuth secret·arbitrary endpoint·tool name·deploymentId를 받지 않음.
  - 서버 기동 시 `provider=None`: `state=blocked`, `code=CONSOLE_OAUTH_NOT_CONFIGURED`, `test_bundle_ready=false`, `remote_digest_verified=false`. **네트워크 호출·인증 시도하지 않음**.
  - 별도 신뢰된 owner-managed OAuth provider가 주입되더라도 고정 `CONSOLE_MCP_ORIGIN`, `mcp-gateway`, 실제 OAuth verified 및 reviewed tool schema를 요구. 이 불리언/계약은 현재 추상 인터페이스이므로 별도 실제 통합 구현과 인증 검토 없이는 사용자 데이터로 증명할 수 없음.
  - `asyncio.wait_for` 10초, 원시 provider 오류·비밀을 반환하지 않음. 호출 가능한 provider 포트는 읽기 전용 `bundle_list`뿐. 알 수 없는 응답은 fail-closed.
- `chatgpt-mcp/tests/test_toss_console_tools.py`: 잘못된 프로젝트/타깃, 추가 모델 인자, OAuth/스키마 미검증, 엉뚱한 endpoint, 오류, timeout, 비밀 누출 차단, 테스트 상태 검증. 기존 `test_toss_console_evidence.py`와 합쳐 25개.

## 소스 수준 배선 (현재 운영 런타임에 미반영)

- `chatgpt-mcp/server.py`: `toss_console_tools` import, tossApp 존재 시 도구 스펙 등록, 단일 `get_toss_test_console_status` dispatch 분기. 실제 provider는 **항상 None**으로 인스턴스화.
- `chatgpt-mcp/project_state.py`: 새 도구 작업 관측 이름 추가.
- `chatgpt-mcp/install-runtime.py` 및 `host_runtime_operations.py`: 신규 모듈 둘을 immutable install manifest / runtime verification 목록에 포함. 설치 자체는 실행하지 않음.
- `chatgpt-mcp/rollout-host-runtime.py`: 새 도구 expected name과 두 오프라인 테스트를 rollout preflight inventory에 포함.
- `chatgpt-mcp/tests/test_rollout_host_runtime.py`, `tests/test_runtime_install.py`, `tests/test_host_runtime_router.py`: 새 도구의 정책·패키징·차단 동작 기대값 반영.
- 기존 다른 WIP 파일은 변경·정리·커밋·스태시하지 않음.

## 실제 실행한 회귀

- `d119f6195fb0439e9703e33de69a6e80`: evidence+source-tool 오프라인 25/25 PASS.
- `c07a5050bd11478880937229f9f8b935`: evidence+source-tool+runtime-install+rollout tests **50/50 PASS**, exit0.
- `4a1e4e2bd9084c7d879cc4e6a2992d4f`: 관련 Python 소스 12개 AST syntax PASS, server import PASS, installer/verified manifest 포함 및 known tool 계약 검사 PASS.
- 선행 혼합 테스트 `358b46606ba247fdb7c5e021d939a878`: 51개 중 50개 PASS + `test_host_runtime_router` import 실패. 원인은 격리 sandbox Python 3.10에서 `mcp` 모듈 부재(`ModuleNotFoundError: No module named 'mcp'`). **이는 라우터 테스트 PASS가 아니며**, 실제 MCP SDK stdio tests/운영 preflight를 아직 검증하지 않았음을 의미.
- Tool SDK/runtime source 전환, TLS/OAuth 실제 요청, Toss 콘솔 provider status 조회, Docker/DB/API/Worker 재배포, AIT 재업로드/초대, Codex 호출 0회.

## 다음 승인 게이트

1. 소유자 승인된 콘솔 OAuth 인가 세션 개설. 공식 제공자를 통해 별도 인가한다. 기존 Claude/Codex 토큰 복사, 인증파일 공유, 사용자의 로그인 자동 대리 시도 금지.
2. authenticated MCP `tools/list`에서 `bundle_list`, `miniapp_get_status`의 실제 schema, pagination 및 authorized workspace를 확인하고 고정 호출 인자/응답 모델을 명시적으로 검증. 현재 가정 필드가 다르면 파서를 수정하고 회귀 추가.
3. 별도 reviewed provider adapter에서 실제 Streamable HTTP OAuth와 session/issuer/origin 검증, 비밀 저장소·리디렉트 검증, tool allowlist, timeout/응답 상한을 구현. `oauth_verified`/schema_verified` 속성만 설정해 신뢰한다고 주장하지 말 것.
4. 기존 세션과 무관한 real host SDK venv에서 router 테스트/정식 full rollout preflight + 독립 소스 리뷰 PASS 후 사용자 **별도** 승인에 따라 immutable rollout. 기존 운영 release를 직접 덮어쓰지 않는다.
5. ChatGPT plugin Refresh/새 채팅에서 `get_toss_test_console_status` 노출과 blocked 상태/실제 읽기 전용 결과를 검증. 정확한 TEST deployment ID 조회; remote digest 없으면 끝까지 `remote_digest_verified=false`.
6. 그 다음 실제 Toss Native WebView 사용자 테스트. 서버 쓰기형 Person POST/PATCH는 별도 승인 TEST 계정·정리 계획 필요.
