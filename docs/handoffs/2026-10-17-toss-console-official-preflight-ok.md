# Toss 콘솔 MCP — 2026-10-17 공식 전체 프리플라이트 결과

프로젝트: `personal-ops` / 대상: `tossApp` TEST (`tarororo-test`).
판정: **공식 전체 사전 검증 PASS; 운영 적용은 하지 않음. 실제 Toss 콘솔 OAuth 및 원격 번들 상태는 여전히 미검증.**

## 실제 확인한 선행 환경

- 사용자가 기존에 승인한 personal-ops host grant `ac4db152c913d841ce934ceba22cac80`, `allow_codex=false` 사용. 실제 만료는 `2026-10-17T15:30:35.622699Z` (사용자 요청 30분과 달리 8시간으로 표시된 점을 이미 사용자에게 알림). 설치 전체 사용자 권한이나 실행기 사용자 버스에 대한 접근이 프로젝트 경계로 격리되지 않음을 전제로 테스트 범위를 최소화함.
- 2026-10-17 19:10 KST 실서버 조회: API running/healthy, `toss-demo`, Worker 및 PostgreSQL running, Schema 014 ready, `014_person_relationship_detail.sql` checksum_match, users=3/persons=17/readings=21.
- 기존 업로드 AIT SHA256 `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f`, deploymentId `01a13e37-27f3-7f9c-923d-6dbd690a5a09`. CLI와 로컬 영수증 성공은 이전에 검증했으나 Toss provider `CREATED/isTested`는 인증된 응답이 없어 미검증.

## 실패 1차: 읽기 전용 서비스 점검의 실행 환경

- 첫 공식 preflight 호스트 operation `64d51a274657d4ce72dd2ceecf55aff0`, 2026-10-17 19:10:48–19:16:27 KST, exit1. `--apply` 없음.
- 인증 UI 단계와 `checks_targeted` 751건, `checks_full` 917건은 PASS. `tunnel_status`까지 성공.
- 영수증: `~/.local/state/chatgpt-coding-mcp/rollouts/rollout-u6x64e6r/receipt.json` (owner-only, preflight). `tunnel_cat`에서 `systemctl --user cat personal-ops-tunnel.service` exit1, `ROLLOUT_SERVICE_COMMAND_FAILED`.
- 후속 **읽기 전용** 진단 op `1efaeac52147de60fc2a5cb5629ea001`: /run/user/1003/bus 존재하지만 호스트 작업 기본 환경에 `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` 둘 다 없음. 기본 systemctl exit1, `XDG_RUNTIME_DIR=/run/user/1003`과 `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1003/bus`를 명령 실행 환경에만 부여하면 exit0. systemd 단위 파일이나 서비스는 전혀 수정하지 않음.

## 공식 재검증: PASS

- 실행 operation `6499243ed6caab1bdc3c61a805039fcc` / 2026-10-17 19:18:29–19:23:36 KST / elapsed 306,732ms / `succeeded/COMPLETE`, exit0, termination_verified=true.
- 실행: `env`로 기존 `~/.local/bin` 및 Node 24 경로 포함한 PATH, `UV_OFFLINE=1`, 위 두 사용자 버스 환경변수만 지정해 설치된 Serena MCP SDK Python으로 `rollout-host-runtime.py --no-pause-on-error` 실행. **`--apply` 생략.** 외부 패키지 다운로드·OAuth 로그인 금지.
- 최종 owner-only 진단 영수증 `~/.local/state/chatgpt-coding-mcp/rollouts/rollout-_qauezkn/receipt.json`, 보조 검증 op `81c2a8275957311afea7a6e73a770c70` exit0.
- 승인 UI 검사: host access 12, Schema014 4, host command 4, runtime rollout UI 7 (합계 27), 전부 exit0.
- `checks_targeted`: 751개, exit0 (114,546ms). `checks_full`: 917개, exit0 (177,010ms). 중복 가능성이 있어 단순 합산을 서로 다른 고유 테스트 수라고 주장하지 않음.
- 호스트 live preflight의 `tunnel_status`, `tunnel_cat`, `admin_cat`, `admin_show` 전부 exit0. `profile_aligned=true`, `service_aligned=true`.
- 결과 status=`preflight_ok`, installed=`false`, switched=`false`, tunnel_id_available=`true`, runtime file count=86.
- target immutable snapshot=`7ed51c90eb1a34a631a6`, observed live snapshot=`8c7a5aff0c75198c6c5d`. 이전 진행 문서의 과거 live snapshot과 달라도 현재 관측값을 우선한다. snapshot 차이는 **아직 전환하지 않았음**을 의미하며 이상이나 오류라고 단정하지 않음.
- stderr의 `pim-check-camera13 DIRECTORY_MISSING`은 `load_projects` 건너뛰기 경고이며 결과에 영향 없음. 사용자가 지정한 프로젝트가 아니므로 별도로 생성하거나 수정하지 않음.

## 후속 차단 조건

1. **현재 소스상 `get_toss_test_console_status`는 provider=None**으로 인스턴스화되므로 프리플라이트 PASS만으로 원격 콘솔을 조회할 수 없음. 실제 OAuth 승인 전까진 `CONSOLE_OAUTH_NOT_CONFIGURED`.
2. 공식 Apps in Toss OAuth MCP endpoint `https://mcp.toss.im/adapters/apps-in-toss-console/mcp`, static client ID `mcp-gateway`, 별도 사용자의 명시적 인가 필요. 메타데이터에서 resource/issuer가 `https://mcp.toss.im`이며 실제 로그인 서버는 `business-accounts.toss.im`. 현재 다른 에이전트의 토큰·비밀정보를 복사하지 않음.
3. 인증된 official `tools/list`에서 `bundle_list`, `miniapp_get_status`의 **실제 schema와 pagination** 및 정확한 workspace 93125/miniapp 75257 권한 확인 뒤에 trusted read-only OAuth transport를 구현·리뷰할 것. 업로드·invite·review·release tool 호출 금지.
4. 새 runtime rollout 계획은 전체 공유 WIP(과거 205 source catalog files, 현재 runtime manifest 86 files)를 포함할 수 있으므로 독립 영향 검토와 사용자 별도 1회 승인이 필수. 예전 plan `71001541a10dc142305f3001f04cbd2f` 만료. 이번 turn에는 **새 rollout plan 미생성**, 승인/execute/stop/start/restart 전부 0회.
5. 정확한 Deployment ID를 authenticated Toss console에서 검증하고, 실제 Toss 앱 Native WebView QA를 진행해야 TEST 검증 완료.

## 무변경 검증

- 프리플라이트 전후 dedicated `get_runtime_status(tossApp,tarororo-test)` image/API mode가 동일하게 running/healthy/toss-demo, Worker/DB running, Schema014 checksum_match와 counts 보존.
- Codex / OAuth 로그인 / AIT 재업로드 / Toss API·Worker·DB 변경 / 사용자 systemd 서비스 재시작 / Git stage·commit·push·reset·stash **없음**. 프리플라이트가 쓴 별도 owner-only 영수증과 테스트 자체의 격리 임시 파일만 제외.
