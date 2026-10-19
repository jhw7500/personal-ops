# Personal Ops — 웹 기반 MCP 런타임 승인/전환 구현 인계

날짜: 2026-10-15 (KST)
프로젝트: `personal-ops`; 대상 MCP `personal-ops-coding`.
상태: **소스 구현 + 격리 및 실제 MCP 도구 라우터 검증 완료. 운영 런타임에 미설치. 실제 터널·관리 서비스 재시작/DB/API/Toss 업로드 없음.**

## 목적
기존 `rollout-host-runtime.py --apply`의 **TTY에서 ROLLOUT 문자열 직접 입력**만 가능한 업데이트를 장기적으로 **관리 웹에서 승인 → ChatGPT에서 별도 실행**하는 방식으로 확장한다. TTY 확인을 모의하거나 임의 셸에서 호출 가능한 `--skip-approval` 옵션을 만들지 않는다. 기존 TTY는 유지한다.

## 신규 기능
- `chatgpt-mcp/runtime_rollout_web.py`: `RuntimeRolloutWeb` 고정 프로젝트(`personal-ops`) 전용 런타임 계획 생성/조회/일회성 웹 승인/승인된 실행 예약/독립 worker claim/결과 기록.
- `runtime_rollout_tools.py`: `prepare_runtime_rollout`(`request_id`), `get_runtime_rollout`(`operation_id`), `execute_runtime_rollout`(`operation_id`) 세 종류만 등록. `argv`/`shell`/`env`/`sql` 등 모델 입력 불가.
- `runtime_rollout_worker.py`: 현재 설치된 **검증된 불변 release**에서만 `python -I`로 기동, 승인된 32hex ID로 receipt 단 한 번 소비. 새 스냅샷의 **공식 rollout-host-runtime**을 동일한 테스트·컨테이너 식별·튜널 프리플라이트와 설치 검증을 거쳐 실행하며, 기존 서비스 교체 이후에도 종료 상태를 기록한다.
- `rollout-host-runtime.py`: `approved_web`는 **같은 프로세스의 worker가 얻은 WebRolloutGrant** 타입+PID+immutable plan ID/source/expected runtime digest 검증 시에만 TTY 체크 대신 수용. 원래 직접 CLI는 여전히 TTY 필수이며 `ROLLOUT` 입력해야 함. 두 경로는 1개 소유자 전용 flock을 공유해 중복/경쟁 전환을 거부.
- `admin_server.py`: 기존 trusted Origin/Host+same-origin+fresh admin token 조건과 동일한 `/api/runtime-rollout-approve?project=personal-ops` POST. 정확한 `operation_id/plan_digest/source_digest/confirmation/admin_token` 필수, 스코프 검사. 인증된 GET `/api/runtime-rollout-plans`, `/api/runtime-rollout`; 토큰 없는 안전한 딥링크 `/runtime-rollouts?operation_id=32hex`. **승인만으로 systemd 기동 X**.
- `admin.html`/신규 `runtime-rollout-admin.js`: '접근·승인'에서 대상 스냅샷 해시·전체 소스 digest·영향 서비스·소스 파일 수/크기·TTL을 textContent-only로 표시하며 계획 ID와 관리자 토큰을 재입력해 일회성 승인. 기존 부모 CSP에 새 JS를 명시 allowlist.
- `server.py`: 세 MCP 도구를 `personal-ops`에만 노출하고 prepare/execute를 project mutex 아래 처리; `tossApp`의 schema014 및 기존 도구는 변경 없는 별도 스코프.
- `install-runtime.py` 및 `host_runtime_operations.VERIFIED_RUNTIME_FILES`에 새 모듈/자산을 필수 manifest file로 포함. `rollout-host-runtime.py`의 검증된 TOOL_NAMES 및 TESTS 목록에 새 기능 등록.

## 보안과 장애 시 처리
- 준비 시 `chatgpt-mcp`의 파이썬/JS/HTML/CSS/JSON/기타 고정 허용 코드/테스트와 `work-dashboard` 자산을 **저장소 밖 private state**에 snapshot. symlink, 너무 큰 파일/예상 밖 파일 수 거부. 공식 설치 manifest와 별도 rollout control source를 SHA256으로 교차 검증. 준비 이후 수정 시 승인·실행 및 worker 기동 차단.
- 관리자 웹은 기존 새 관리자 토큰 재입력·정확한 Origin/Host·CSRF 강제. 10분 TTL, 1회 승인 영수증, frozen digest/nonce 확인, 실행 반복 차단. 승인 상태 `approved`와 서비스 전환 `queued/running/succeeded` 분리.
- 실제 실행은 **별도의 transient systemd --user unit** `personal-ops-runtime-rollout-<id>`이며, 기존 MCP/관리자 서비스가 재시작되어도 worker가 유지되는 설계. 명령 실행 대상은 runtime_rollout_worker 하나; Node24 PATH와 systemd user-bus, XDG_STATE_HOME를 고정. 전체 official targeted+full regression가 PASS되지 않으면 설치 금지.
- 실행 작업 시작 여부가 애매하거나 일부 서비스 변경 후 오류 시 `outcome_unknown`/검토 필요 표시하고 자동 재시작·중복 rollout 금지. worker의 독립 실행 상태와 정확한 단일 unit을 별도 확인해야 함.
- 소스 소유자 확인: 기존 **호스트 TTY 승인 모드는 계속 지원**, 새 웹 모드는 process-local consumed receipt proof여야만 예외 허용.
- **첫 도입은 TTY 1회 필요**: 현재 live release에는 새 웹 계획·승인·worker 기능이 없다. 사용자가 사전검사와 서비스 일시 종료 영향을 확인한 뒤 기존 공식 `--apply`을 real TTY에서 ROLLOUT으로 최초 활성화해야 한다. 이 절차를 web mode로 역으로 부트스트랩할 수 있다고 주장 금지. 그 이후부터 웹 승인·ChatGPT execute 가능.

## 검증 (실제 실행 증거)
- 새 런타임 계획/소스 snapshot·실행 계약 fake unit 회귀 9/9 PASS: 격리 job `8e4e15b35a084be7ae8fe1a457b0c66e`; 승인 전 실행 거부, 1회 영수증, 승인 후 1회만 systemd 기동, 타입·권한 혼동 거부, 소스 변경·symlink 차단, 큐 이후 SHA 변경 시 실패, 불확실한 결과 재실행 차단.
- 실제 HTTP(로컬 loopback, 가짜 manager) 새 관리자 승인 5/5 PASS: job `e5e834e734c54f41b0f911397b77549c`; Origin·호스트·토큰·작업 ID·digest·정확한 body 검증 및 CSP.
- 기존 installer+롤아웃 TTY 회귀 9+14 PASS, job `99f1f2b10c9a4f1da04626d379c32af0`.
- 실제 Serena MCP SDK 도구 발견 및 prepare/get/execute 라우팅 1/1 PASS(가짜 manager), host operation `f306c94545000f577bf074c4c894630b`.
- 기존 서버 종료·sandbox 정리 4/4 PASS 및 위 라우터 1/1 PASS, host operation `ff4dafe03f92fc136dccb7e1629697b0`.
- 이전에 공식 `rollout-host-runtime.py` preflight는 Node v24+systemd user-bus에서 대상 621/621 + 전체 786/786 PASS(이 작업 전 baseline). **이번 새 웹 기능 포함 재사전검사는 작업 중이므로 별도 최종 결과 확인 필수**.
- sandbox Python3.10에서 asyncio.Task.cancelling 없음+MCP SDK 부재 때문에 `test_server_shutdown` 실패는 호스트 Python3.12+Serena 환경에서 정상 4/4 PASS로 구분; 소스 오류나 우회된 PASS가 아님.

## 후속 게이트
1. 이번 소스 전체의 공식 preflight 신규 테스트 포함 PASS, 신뢰 버전의 설치 fingerprint 확인.
2. **최초 1회 실제 SSH/TTY `--apply`/ROLLOUT 승인** 또는 기존 live runtime에 설치된 신뢰할 수 있는 전용 *정식* 승인 경로가 생길 때까지 미반영 유지.
3. 활성화 이후 새 ChatGPT plugin refresh → `prepare_runtime_rollout` exact plan → 관리 웹 token+ID 승인 → `execute_runtime_rollout` → `get_runtime_rollout`으로 상태 검증. 서비스 중단/복구와 시스템 버스 확인.
4. 별개 `tossApp` Schema014 마이그레이션도 **독립 one-use 승인**이며 런타임 web 승인과 절대 혼동하지 말 것. DB 014 → API toss-demo → exact AIT TEST 순서 유지.
5. 실제 `systemd-run` self-update 완전한 라이브 end-to-end는 **최초 설치 전에는 검증할 수 없으므로 별도 실검증**. 성공/종료/불확실성을 사실에 맞게 기록.

## 환경
호스트 프리플라이트에서 `PATH` 앞에 `~/.nvm/versions/node/v24.12.0/bin` 필요 (기본 Node12는 Serena Python Pyright LSP 실패), `XDG_RUNTIME_DIR=/run/user/1003`, `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1003/bus` 필요. 권한된 host-access가 있어도 기존 TTY 프로세스를 모의하지 않았다.

Git reset/stash/add/commit/push 없음. 소스 WIP 및 기존 릴리스·터널·DB·API·Worker·AIT는 현재 유지.
