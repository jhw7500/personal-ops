# Toss 콘솔 조회 연동 — 운영 전환 및 ChatGPT 실조회 확인

날짜: 2026-10-19 KST. 대상: personal-ops / tossApp / tarororo-test.

## 현재 결과

- 사용자가 별도 SSH 터미널에서 공식 Codex 클라이언트의 OAuth 로그인을 완료했다.
  이후 `ait-console`은 enabled=true / auth_status=`o_auth`이며 native 콘솔 도구가 노출됐다.
- 인증된 native `bundle_list`로 정확한 TEST deployment를 조회했다. 버전 `20261016-26`,
  `reviewStatus=CREATED`, `isTested=true`, `deployed=false`다.
  `isTested`는 TEST 환경 준비 표시이며 실기기 QA 완료를 증명하지 않는다.
- 같은 workspace/miniapp의 `miniapp_get_status` 응답은 `serviceStatus=PREPARE`다.
  이는 앱 공개 운영 상태이며 개별 번들 검수 완료를 의미하지 않는다.
- 실제 응답 파서를 보완한 뒤, 공식 Codex 클라이언트를 통해 조회하는 연결부와 서버 배선까지
  구현했다. 진단용 설정으로 **구현 코드 → 실제 콘솔 → MCP 결과 반환**이 성공했다.
  사용자 승인 후 owner 설정과 운영 MCP·관리 웹을 `d39917b42f6a1bb504da`로 전환했다.
  설치본과 실제 owner 설정을 사용한 콘솔 조회도 성공했다. 이후 일반 ChatGPT의 조회 결과와
  운영 서버의 신규 도구 호출·완료 기록을 대조해 웹 경로의 동작까지 확인했다.
  별도 personal-ops OAuth client를 만들거나
  Codex/Claude의 토큰을 추출하지 않고 Codex가 기존 OAuth 세션을 관리하게 한다.

## 로그인 전 확인 결과(이력)

- `codex mcp list --json`과 `codex mcp get ait-console --json`을 읽기 전용으로 실행했다.
  URL은 `https://mcp.toss.im/adapters/apps-in-toss-console/mcp`, enabled=true,
  auth_status=`not_logged_in`이다. 서버를 추가·재등록하거나 기존 세션을 종료할 필요가 있다고
  판단하지 않는다. 이 단계에서는 로그인이나 설정 변경을 실행하지 않았다.
- 설치된 Apps in Toss plugin 0.2.6의 `.claude-plugin/plugin.json`은 같은 URL과
  `oauth.clientId=mcp-gateway`를 선언한다. CLI의 공개 get/list 결과에는 client ID 필드가
  나타나지 않았으므로 client ID 설정이 없다고 단정하지 않는다.
- 로그인 전 도구 목록에는 Toss 문서 MCP만 있고 콘솔의 `bundle_list`는 없었다. 플러그인 검색에서도
  바로 사용할 Toss 콘솔 연결을 발견하지 못했으나 검색 결과가 모든 플러그인을 보장하지는 않는다.
- 호스트 Serena venv의 MCP Python SDK는 1.23.0이며 OAuthClientProvider와 TokenStorage,
  Streamable HTTP auth 인터페이스를 제공한다. SDK가 있다는 사실은 Toss가 임의 client나
  callback을 허용한다는 증거가 아니다.

## 공개 OAuth 계약 조사

2026-10-19 09:51:18 KST에 쿠키·Authorization 없이 두 공개 metadata GET이 HTTP 200이었다.

- [Protected Resource Metadata](https://mcp.toss.im/.well-known/oauth-protected-resource/adapters/apps-in-toss-console/mcp):
  resource/authorization server `https://mcp.toss.im`, scopes_supported=`[openid]`, bearer=`header`.
- [Authorization Server Metadata](https://mcp.toss.im/.well-known/oauth-authorization-server):
  issuer `https://mcp.toss.im`, authorize `https://business-accounts.toss.im/oauth2/authorize`,
  token `https://business-accounts.toss.im/oauth2/token`, code + refresh_token, PKCE plain/S256.
  registration endpoint는 없다. Device flow 지원은 확인되지 않았다.
- [Toss 공식 콘솔 MCP 가이드](https://developers-apps-in-toss.toss.im/guide/console-mcp.md)와
  [harness 고정 SHA README](https://github.com/toss/apps-in-toss-harness/blob/255b636812d8b3a9b048d0a700644b9cbd8532c4/README.md)
  에서 정적 `mcp-gateway`와 DCR 미지원을 확인했다. 공식적으로 설명한 호스트에서 사용자가
  새 OAuth 세션을 인가하는 경로를 우선 사용한다.
- 공개 scope `openid`를 읽기 전용 또는 특정 workspace/miniapp 권한으로 해석하지 않는다.
  같은 콘솔 서버에는 쓰기 도구도 있으므로 adapter의 `bundle_list` 제한과 OAuth 권한은 다르다.
- 독립 personal-ops client의 `mcp-gateway` 사용 범위, callback allowlist, 실제 tool JSON
  schema는 공개 자료에서 확인되지 않았다. 임의 redirect URI나 schema pin을 만들지 않는다.

## 사용자 로그인 단계

설치된 CLI의 `codex mcp login --help`에서 `--no-browser`가 인증 URL 출력 및 callback URL
입력을 지원하는 것을 확인했다. 호스트의 **별도 SSH 터미널**에서 다음 명령을 사용한다.

```sh
codex mcp login ait-console --no-browser
```

브라우저에서 본인이 토스 계정으로 로그인·동의하고, CLI가 요구하는 callback 입력은 그
터미널에서 직접 한다. 인증코드·callback URL·토큰을 채팅에 보내거나 다른 저장소에 복사하지 않는다.
이는 공식 Codex 클라이언트를 인증하는 절차이며 personal-ops 서버 자체의 OAuth 연결 완료를
의미하지 않는다. 로그인 뒤에는 민감값이 없는 완료 여부만 알려주면 된다.

사용자가 이 단계를 완료했다고 알렸고 인증 상태와 실제 조회 성공으로 확인했다.
로그인 명령은 에이전트가 실행하지 않았으며,
기존 Codex/Claude credential 저장소를 읽어 개인 MCP 서버로 옮기거나 토큰을 재사용하지 않는다.

## 실제 읽기 전용 호출과 응답 계약

실행한 호출은 다음 두 개뿐이며 업로드·TEST 발송·검수 요청은 하지 않았다.

```json
{"tool":"bundle_list","arguments":{"workspaceId":93125,"miniAppId":75257,"page":0,"size":20,"search":"01a13e37-27f3-7f9c-923d-6dbd690a5a09"}}
{"tool":"miniapp_get_status","arguments":{"workspaceId":93125,"miniAppId":75257}}
```

- `bundle_list` 응답은 `contents` 배열과 `currentPage=0`, `totalPage=0`이었다.
  배열에는 정확한 deployment ID와 `miniAppId=75257`인 항목 하나가 있었다.
- 현재 도구 설명에 따르면 `totalPage`는 **마지막 페이지의 0-based 인덱스**다.
  따라서 `0/0`은 내용이 있는 단일 페이지일 수 있으며 빈 목록으로 해석하면 안 된다.
- native 도구의 생성된 TypeScript 선언에서 인자 이름과 페이지 규약을 확인했다.
  원본 JSON `inputSchema`와 `readOnlyHint`는 이 표면에서 제공되지 않았다.
  TypeScript 선언으로 JSON schema SHA-256을 대신 만들지 않았다.
- 응답에는 원격 AIT SHA-256 증거가 없다. 로컬 파일과 원격 번들의 바이트 동일성은 미검증이다.

## 1차 파서 수정과 검증(이력)

- `toss_console_evidence.py`: 실제 `contents` 응답과 canonical `miniAppId`를 검증한다.
  충돌하는 별칭·여러 목록 키·잘못된 페이지 메타데이터는 거부한다.
  여러 페이지가 남았거나 중간 페이지인 응답은 `INCOMPLETE_CONSOLE_PAGE`로 거부한다.
  전체 페이지 순회는 구현하지 않았다. 기존 오프라인 list/`bundles`/`items` 계약은 유지했다.
- 두 테스트 파일에 관측 응답, SDK structured/text JSON 경로, 페이지·scope 충돌 회귀 검사를 추가했다.
  SDK 입력 catalog는 **합성 오프라인 fixture**이며 실제 provider schema pin 검증 증거가 아니다.
- 수정 전 실제 형식 거부 및 canonical ID 검증 누락을 RED 테스트로 재현했다.
  수정 후 관련 54개 테스트와 변경 파일 Ruff 검사가 통과했다.
- 독립 코드 검토는 문제 0건 / APPROVE였다. 공식 SDK Python의 전체 963개 검사에서 실패 0건,
  명시적 opt-in인 실제 Codex 격리 probe 1개는 실행하지 않았다(962개 통과 / 1개 skip).
  검사 시간은 177.906초였다. 제품·테스트 소스의 나머지 218개 파일이 원본과 같은 작업트리에서 실행했다.
- 실제 호출의 비밀값을 제거한 응답을 수정된 파서로 재처리해 위 TEST 상태와 일치함을 확인했다.
  순수 파서의 반환만으로 OAuth provenance나 `provider_verified=true`를 만들지 않는다.

진단·테스트 근거는 사용자 전용 디렉터리에 보관했다(디렉터리 0700 / 파일 0600).
`/home/jhw/.local/state/personal-ops-rollouts/toss-console-live-20261019-edtvretp/`

## 2차 상시 연결 구현

설치된 Codex 0.163.0의 생성된 공식 RPC schema와
[App Server 문서](https://developers.openai.com/codex/app-server/)를 확인했다.
`mcpServer/tool/call`은 모델 `turn/start` 없이 MCP 도구를 직접 호출한다.
기존 daemon/socket/대화에 붙지 않고 별도 `--no-daemon app-server --listen stdio://`
프로세스와 신설 `ephemeral=true` / `sandbox=read-only` / `approvalPolicy=never` thread만 사용한다.

### 실제 확인한 계약

- own subprocess의 `mcpServerStatus/list`에서 server=`ait-console`, plugin=`ait@apps-in-toss`,
  httpOrigin=`https://mcp.toss.im`, authStatus=`oAuth` 및 **원본 JSON inputSchema**를 확보했다.
- 원본 schema SHA-256은
  `bd7021bac9ba68f043abdccedd16494539f2bfab9b895161512cebc6b3d5e614`다.
  `tests/fixtures/toss_bundle_list_input_schema.json`은 그 schema만 저장한 fixture다.
- 실제 provider는 `annotations/readOnlyHint`를 생략한다. 이를 provider의 `true` 표시로 꾸미지
  않는다. 소스에서 검토한 고정 `bundle_list` 이름·정확한 schema hash·앞서 기록한 고정 5개 인자가
  모두 일치하는 `pinned_bundle_list` 정책에서만 표시 생략을 허용한다.
  명시적인 false/비불리언 힌트와 잘못된 annotation 구조는 거부한다.
- Codex의 상태 API는 OAuth client ID를 내보내지 않는다. 관리형 provider는
  `authentication_source=codex_app_server`, `client_id=None`으로 표현한다.
  기존 직접 MCP provider는 `mcp-gateway` 검증 정책을 유지한다.
- live probe의 RPC는 `initialize`, `thread/start`, `mcpServerStatus/list`,
  `mcpServer/tool/call` 네 가지뿐이었다. 실제 bundle 결과도 1차 조회와 동일했다.

### 코드와 수명주기

- `toss_console_codex.py`: owner 전용 설정, 실행 파일 SHA-256, 프로젝트 외부 경로,
  public CLI의 정확한 MCP URL/인증 헤더 부재를 확인한다. 조회마다 OAuth 상태·plugin ID·원점·
  원본 catalog를 재검증하고 고정 호출만 전달한다. stdout/RPC 크기를 제한하고 raw 오류는 공개하지 않는다.
- Codex가 인증 저장소를 직접 관리한다. 이 연결부는 OAuth token 파일을 열거나 복사하지 않는다.
  애플리케이션 코드에서 모델 turn을 시작하는 RPC는 허용하지 않는다.
- `toss_console_tools.py`: 사용자 인자를 먼저 검증한 후 비동기 provider factory를 호출한다.
  연결이 없거나 끊어지면 차단하고 다음 호출에서 다시 검증할 수 있다.
- `server.py`: 등록된 tossApp에만 연결을 생성하고 종료 시 소유 연결을 정리한다.
  Console 종료가 실패해도 기존 작업·명령·에이전트 정리를 계속한다.
- 신규 모듈을 설치 파일·런타임 검증·롤아웃 검사 목록에 포함했다.
- 실제 native Codex는 296,865,480 bytes다. 이 모듈의 stable-FD hash 검사는 512 MiB로 제한한다.
  다른 호스트 명령 실행기의 128 MiB 제한은 변경하지 않았다.

### 적용한 운영 설정

실제 설정 경로는 `~/.config/chatgpt-coding-mcp/toss-console.json`이며 사용자 소유 0600이어야 한다.
필드는 version=1, enabled=true, codex_executable(정규 native 실행 파일 절대경로), codex_sha256이다.
설정이 없거나 비활성화이면 클라이언트·네트워크 연결을 시작하지 않는다.

검증에 사용한 설정 초안은 다음 사용자 전용 진단 디렉터리에 보존했다.
`/home/jhw/.local/state/personal-ops-rollouts/toss-console-connection-20261019-1pzun99o/toss-console.draft.json`

Codex 업데이트로 실행 파일이 바뀌면 `CONSOLE_CLIENT_CHANGED`로 차단한다.
기존 호스트 승인 기록·만료 시각을 수정하지 않았다. 다만 이번 배포는 서버 권한 코드가
바뀌므로 기존 런타임에 묶인 승인은 `RUNTIME_CHANGED`로 비활성화된다. 화면 자원만 바뀌는
배포와 구분한다. 아래 운영 전환 결과에 실제 관측을 기록했다.

### 활성화 전 검증 기록

최종 검증 결과:

- 관련 Console 테스트 76개 통과. 독립 코드 검토는 최종 문제 0건 / APPROVE.
- 최종 전체 986개 검사에서 실패 0건(985개 통과 / opt-in 실제 Codex 격리 probe 1개 skip).
  최종 실행 시간 196.202초. 초기 전체 검사 뒤 보완이 있어 최종 소스로 다시 검사했다.
- 설정 초안을 사용한 최종 native factory 조회도 성공했다(`live-factory-final.json`).
- 검토용 런타임은 `d39917b42f6a1bb504da` / 88개 파일이다. 공식 installer로 사용자 전용
  진단 디렉터리에만 생성했고 실제 서비스 설치 위치에는 반영하지 않았다.
- 이 패키지를 별도 stdio MCP 인스턴스로 실행해 `get_toss_test_console_status` 도구 등록,
  readOnlyHint=true 및 실제 콘솔 결과 `state=verified`를 확인했다(`staged-mcp-probe.json`).
  운영 터널을 대상으로 한 검증과 구분한다.
- CLI 검증용 control digest:
  `a91a3c02caba29c10d557b101ffdb6cc584b72becbf67c68851e20202db2c930`.

위 기록은 운영 전환 전 staging 검증이다. 다음 절의 운영 관측과 구분한다.

실기기 QA·원격 AIT SHA-256·업로드/발송/검수는 이 조회 연결의 완료 조건에 포함하지 않는다.

## 운영 전환 결과 — 2026-10-19 11:11 KST

사용자의 `진행` 승인으로 다음 작업을 수행했다.

- 설정 초안을 실제 `~/.config/chatgpt-coding-mcp/toss-console.json`에 배치했다.
  기존 파일은 없었으며 O_EXCL / O_NOFOLLOW로 생성했다. 사용자 소유 일반 파일, 0600,
  enabled=true를 확인했다. 설정 SHA-256은
  `73450be3ba027ad80316ca3e443767e7d534284d814fbd2fd3860cb0a784cb7a`다.
- 공식 `rollout-host-runtime.py --apply --no-pause-on-error`를 사용했다. 승인 UI 검사
  4종과 관련 820개 검사가 통과했다. 전체 986개 검사는 실패 0건이며 실제 Codex 격리
  opt-in probe 1개는 skip이다. 최종 전체 검사 시간은 207.670초다.
- 기존 `8c7a5aff0c75198c6c5d`에서 검토한 `d39917b42f6a1bb504da` / 88개 파일로
  설치·전환했다. 실행 중인 tunnel PID 2369908 / MCP PID 2369951의 시작 identity와
  설치본 manifest를 검증했다. 실제 interpreter는 Serena SDK venv의 Python이다.
- `personal-ops-admin.service`도 같은 snapshot으로 전환했다. 실제 MainPID의 명령과
  unit 일치, HTTP 200, `id="host-approval"` 존재를 확인했다. 전체 관리 GUI를 수동으로
  다시 조작한 검증은 아니며 HTTP·프로세스 검사와 승인 UI 자동 검사 결과다.
- 전환 직전 실행 중인 MCP 작업 기록은 없었다. 전환 전·터널 전환 후·관리 웹 전환 후의
  CLI 관측에서 10개 세션의 변경 0건, 관측 누락 0건이었다.
- 첫 ChatGPT `get_host_access` 호출은 재연결 중 `UNAVAILABLE / Connection timed out`으로
  실패했다. 이어진 같은 읽기 전용 호출은 정상 응답했다.
- 설치된 새 모듈과 실제 owner 설정으로 별도 소유 클라이언트를 실행해 콘솔을 조회했다.
  `state=verified`, `provider_verified=true`, 버전 `20261016-26`, `review_status=CREATED`,
  `is_tested=true`, `deployed=false`였다. 이 검증은 운영 터널의 기존 MCP child에 새 도구를
  호출한 것은 아니며, 모델 turn도 시작하지 않았다.

### 배포 직후의 도구 목록 확인 대기 기록

정식 배포 receipt는 당시 `verification_required`로 종료됐다. 실행 중인 런타임과 관리 웹은
검증됐지만, 운영 child의 passive health에는 새 `tools/list` 수신이 없어 `awaiting_discovery` /
`tools_registered=false` / `connector_schemas_verified=false`였다. 이 과거 receipt를 성공 값으로
덮어쓰지 않았다. 이후의 실제 웹 호출 검증은 다음 절과 별도 증거 파일에 추가했다.

일반 ChatGPT에서 personal-ops-tunnel 연결을 새로고침한 후 다음 읽기 전용 요청을 안내했다.

> tossApp의 TEST 콘솔 상태를 get_toss_test_console_status로 조회해줘.

기존 child의 stdin에 RPC를 주입하거나 다른 세션의 연결을 가로채지 않았다.

### ChatGPT 실조회 최종 확인 — 2026-10-19 12:09 KST

사용자가 일반 ChatGPT의 실제 조회 결과를 전달했다. 앱 tarororo, 버전 `20261016-26`,
`found=true`, `test_bundle_ready=true`, `review_status=CREATED`, `is_tested=true`,
`deployed=false`, `provider_verified=true`, `remote_digest_verified=false`로 설치본 조회와 일치했다.
이 반환값의 근거는 사용자 전달이며 이 Codex 대화에서 직접 수신한 도구 응답으로 표시하지 않는다.

운영 서버의 metadata 전용 `activity.jsonl` 및 이전 회전 파일을 읽어 다음 기록도 확인했다.

- `get_toss_test_console_status` / tossApp / call ID `7ad3adee81d84d628e4205a48a15d53d`:
  02:18:39.075058Z 시작, 02:18:40.244129Z 완료.
- 같은 도구 / tossApp / call ID `ba8950e62d654119a86671a4a0442932`:
  02:18:40.250465Z 시작, 02:18:40.446698Z 완료.

서버 기록은 도구 호출과 정상 반환을 증명하며, 세부 반환값은 사용자가 전달한 결과와 대조했다.
새 도구가 운영 서버에 적용되어 일반 ChatGPT에서 실행된 것을 이 두 증거로 확인했다.
현재 runtime과 관리 웹을 다시 읽기 전용으로 검사해 snapshot `d39917b42f6a1bb504da`,
동일한 MCP PID/시작 identity, 관리 웹 HTTP 200도 확인했다.

passive health의 `tools/list` 관측은 여전히 `awaiting_discovery`다. 이 Codex 대화의 연결 도구
목록도 이전 목록이며 신규 도구가 없다. 따라서 raw 도구 목록·connector schema의 직접 관측은
미완료로 남긴다. 실제 운영 호출 성공과 목록 관측 상태를 혼동하지 않는다.

이 조회 연결의 기능 확인은 완료했다. 실기기 QA, 정식 출시, 원격 AIT와 로컬 파일의 해시
동일성은 이번 확인으로 완료되지 않는다. 후속 확인 때문에 서비스를 다시 시작하지 않았다.

### 기존 호스트 실행 승인

배포 전 tossApp grant `ec28a09d30584c6c56377b34bf6f5e00`는 active=true였다.
배포 후 기록에는 같은 grant ID, `expires_at=2026-10-19T02:28:57.224291+00:00`,
`allow_codex=false`, `revoked_at=null`이 남아 있지만 실제 조회는
active=false / `reason=RUNTIME_CHANGED`다.

`host_access.py`는 화면 전용 파일을 제외한 검증된 코드 manifest에 권한을 묶는다.
이번에는 `server.py`, Console 연결부 등 권한 코드가 바뀌어 기존 코드에 대한 승인으로
실행할 수 없다. 승인 기록 삭제나 시간 초기화가 아니며 새 승인을 자동 발급하지 않았다.
이 읽기 전용 Console 조회와 호스트 명령 실행 권한은 별개다.

### 첫 실행의 TTY 오류와 복구

첫 시도는 모든 검사와 preflight 후 `HOST_TTY_REQUIRED`로 중단됐으며 서비스 전환은 없었다.
`tty=true`로 실행해도 RTK proxy 하위 프로세스의 stdin은 TTY, stdout/stderr는 pipe인 것을
별도 읽기 전용 probe로 확인했다.

재시도에서는 소유 실행 프로세스의 stdout/stderr를 실제 `/dev/tty`에 연결했다.
stdin/stdout/stderr의 isatty=true를 확인하고, 검사·source 검증·실제 `ROLLOUT` 확인을
포함한 정식 절차를 다시 실행했다. TTY 검사를 패치하거나 성공한 검사 receipt를 재사용해
단계를 건너뛰지 않았다. 최초 실패와 재시도 receipt는 모두 보존했다.

운영 전환 근거는 사용자 전용 디렉터리에 있다.

`/home/jhw/.local/state/personal-ops-rollouts/toss-console-activate-20261019-5753qv3j/`

- `config-activation.json`, `preflight.json`, `host-access-before.json`
- `receipts/rollout-mxkcth9f/receipt.json`: 첫 시도 TTY 실패
- `receipts/rollout-6jfbpa5_/receipt.json`: 실제 설치·전환 및 CLI 관측
- `runtime-verification-1.json`: 전환 후 실제 런타임·관리 웹·도구 관측
- `installed-console-read.json`: 실제 owner 설정을 사용한 설치본의 Console 조회
- `web-verification.json`: 후속 운영 상태와 사용자가 전달한 ChatGPT 반환값
- `web-call-activity.json`: 운영 서버의 신규 도구 호출·완료 metadata

## 적용 여부

- 파서·Codex 연결부·서버 wiring·패키징·회귀 테스트를 소스에서 수정했다.
  실제 owner 설정을 활성화하고 MCP·관리 웹의 실행 snapshot을 전환했다.
- OAuth grant는 사용자가 공식 Codex 로그인으로 생성했다. 에이전트는 grant/credential 저장소를
  직접 열거나 복사하지 않으며 공식 Codex가 OAuth 세션을 관리한다.
  AIT 업로드·발송·검수 요청, Docker·DB·스키마 변경은 수행하지 않았다.
- 직전 사전검사 `c1534ffc6fd144799014` / 87개 파일 / 전체 953개 실패 0은 이전 단계의
  검증 기록이다. 실제 OAuth나 Toss provider 조회 성공 증거로 사용하지 않는다.
- 운영 MCP·관리 웹은 `d39917b42f6a1bb504da`다. 일반 ChatGPT의 신규 도구 실행은
  사용자 반환값과 운영 서버 완료 기록으로 확인했다. passive 도구 목록·schema 관측은 별도 미검증이다.
- 2차 진단 근거: `/home/jhw/.local/state/personal-ops-rollouts/toss-console-connection-20261019-1pzun99o/`.
  성공한 protocol probe는 `probe4-8ovqg_vj/probe-summary.json`, 구현 코드 검증은
  `live-factory-result.json`과 `live-factory-final.json`에 기록했다.
  최종 전체 검사 근거는 `tests-final-full-result.json`과 `tests-final-full.log`다.
  진단 과정의 실패도 덮어쓰지 않고 보존했다.
