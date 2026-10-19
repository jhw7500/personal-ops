# 웹 롤아웃 통합 배포 및 검증

날짜: 2026-10-16 KST. 프로젝트: personal-ops.
범위: 최신 대시보드를 유지하면서 웹 롤아웃의 승인·장애 복구·오류 경계를 보완한다.
현재 상태: 별도 사용자 배포 승인을 받아 `5c725c7a6d4ef86b9759`/80파일 활성화 완료.
소스 준비와 격리 검증에 이어 터널·관리 웹을 전환하고 실제 화면을 검증했다.
DB·Docker·스키마 변경은 수행하지 않았다. 아래 준비 기록과 마지막 활성화 기록을 구분한다.

## 운영 기준선 재확인

시작할 때 참조한 배포본은 `dfc699ead12d7bcc378e`/75파일이었다. 준비 도중 실제 터널
상태와 MCP 프로세스를 재확인하니 `51178b015dbce69aa32d`/80파일이 이미 실행 중이었다.
이는 이번 작업에서 설치하거나 전환한 결과가 아니다. 새 설치본의 모든 manifest 파일은
시작 시 고정한 원본 checkout 소스와 일치했고, checkout은 그 사이 변경되지 않았다.

현재 설치본에는 웹 롤아웃 도구 3개, Schema 014 도구와 모바일 호스트 접근 링크가 있다.
따라서 처음의 “Schema 014 WIP 제외” 후보를 운영 기준으로 사용하지 않는다.
새 후보는 **현재 80파일 기능을 그대로 보존하고 필요한 수정만 추가**했다.
기존 work-dashboard 파일, 작업 목적 task_label, 호스트 권한 카드도 보존했다.
Schema 014 실행 코드가 보존됐다는 것은 실제 DB 적용·추가 승인을 뜻하지 않는다.

## 후보와 원본

- 격리 작업 공간: `/home/jhw/.codex/worktrees/1f41/personal-ops`
- 브랜치: `integrate/web-runtime-rollout-20261016`
- 운영 기준: `51178b015dbce69aa32d`/80파일
- 보완 후보: `5c725c7a6d4ef86b9759`/80파일
- 증거: `~/.local/state/personal-ops-rollouts/web-rollout-integration-20261016/`
- 커밋·push·PR 없음. 시작 해시를 비교한 뒤 원본에 14파일 선별 반영 완료.
  이외 파일과 Git HEAD/index는 보존했고 원본/격리 후보의 실행 소스·control digest가 같다.

## 보완 사항

1. `runtime_rollout_web.py`: 고정 HTTPS 승인 링크, worker claim 직전 승인 TTL 재검증,
   승인 영수증과 상태 저장의 중단 복구, 정확한 transient unit만 관측하는 오래된 작업 복구.
   claim되지 않은 실패와 결과 불확실성을 구분하며 `outcome_unknown` 자동 재시도를 차단한다.
2. `admin.js`와 `runtime-rollout-admin.js`: 로그인 뒤 승인 탭과 정확한 계획 ID를 복구한다.
   목록에 없는 계획도 정확한 ID로 조회한다. 누락·거부·ID 불일치 때 다른 계획을 대신
   승인하지 않는다. 만료, 실행 지연, 결과 검토 필요와 제한된 결과를 textContent로 표시한다.
   기존 모바일 `/host-access` 링크도 유지한다.
3. `server.py`: 세 웹 롤아웃 도구의 알려진 오류 코드는 보존하고, 예상 밖 예외는
   `RUNTIME_ROLLOUT_FAILED`로 정규화해 내부 경로나 원본 예외를 응답하지 않는다.
4. `rollout-host-runtime.py`: 실제 승인 브라우저 검사 7개를 필수 사전검사로 실행한다.
   신규 `tests/test_runtime_rollout_ui.py`는 기존 test 파일 glob에 포함되어 현재 설치된
   manager와 새 helper의 control digest 형식이 일치한다. 별도 브라우저 인터프리터에서만
   실행되므로 MCP 테스트 탐색은 Playwright 의존성을 요구하지 않는다.
5. `rollout_diagnostics.py`: 새 승인 브라우저 단계도 검사 수·실패 이름을 영구 영수증에
   남긴다. 단계 allowlist를 한곳에서 관리하며 원본 오류·토큰·traceback은 기록하지 않는다.

## 검증

- 보안 회귀: TTL, 영수증 복구, worker 소실·timeout·재부팅·불확실한 관측에 대해
  실패 재현 후 수정. manager 19개, 초기 focused 41개 통과.
- MCP 오류 경계: 실제 stdio 라우터에서 내부 경로 노출 3건을 재현한 뒤 수정.
  prepare/get/execute의 알려진 오류·예상 밖 오류 6분기 검증.
- 승인 UI: 실제 임시 HTTP+Chromium RED 6개를 관측한 뒤 신규 7개 통과.
  원본 기준 재통합 후 관리 화면 전체 93개 통과. 1440·390·320px 화면 확인.
- 최종 focused Python 64개, Node 206개, 변경 Python AST·Ruff·JS 구문·diff 검사 통과.
- 운영 설치본과 후보의 별도 stdio 도구 발견: 각각 70개. 새 웹 도구 3개, 기존 Schema 014
  도구 3개 및 두 선택적 task_label 스키마를 확인했다. 도구 실행은 하지 않았다.
- 기존 설치본 manager → 후보 helper의 승인 증명 검증을 임시 상태에서 통과했다.
  브라우저 검사 소스 변조도 거부했다. 실제 systemd 실행은 가짜 launcher로 대체했다.
- 초기 공식 read-only preflight: 종료 코드 0, `preflight_ok`. 승인 카드 10개, 런타임 승인
  브라우저 7개, 대상 688개, 전체 853개 통과. 운영 기준선과 profile/service 정합성도 확인.
  `installed=false`, `switched=false`.
  receipt: `preflight/rollout-vl7doavs/receipt.json`.
- 이후 새 브라우저 단계의 진단 수집 누락을 실패 재현 후 보완했다. 관련 30개 통과 및
  독립 재검토에서 차단 사항 없음. **최종 재사전검사도 종료 코드 0, `preflight_ok`**:
  승인 카드 10개, 런타임 승인 브라우저 7개, 대상 688개, 전체 853개 통과.
  새 브라우저 검사 수 7개도 실제 영수증에서 확인했다.
  최종 receipt: `preflight-final/rollout-bbty937e/receipt.json`.
  최종 control SHA-256: `ec1166ca73f2db728509e67e8d08374451b9ca8f91d9670eca8323b4ec5035ac`.
- 재사용·품질·효율 독립 검토 3건에서 정확성 차단 사항 없음. 선택적 정리 7건은
  승인 시점의 최신 조회, 테스트 소스 격리, 고정된 안전 경계와 검증 범위를 유지하며 보류했다.
  이후 진단 단계 allowlist 중복 1건은 위 진단 수정에 통합했다.

기존 관리 화면 검사 종료 시 임시 HTTP 연결의 BrokenPipe 진단이 기록됐으나 93개 모두
통과했다. 진단 로그를 삭제하거나 숨기지 않았다.

운영 MCP/tunnel PID는 896915/896900, 관리 웹 PID는 897074로 유지됐다.
실제 HTTP의 admin.js·runtime-rollout-admin.js·visualizer.js는 모두 200이며 운영 설치본
해시와 일치했다. 따라서 원본 소스 반영을 운영 UI 활성화로 혼동하지 않는다.
보호 설정·권한 기록 13개 중 12개는 관측 구간 내 동일했고, 호스트 접근 기록 1건의
내용 변화가 동시에 관측됐다. 갱신 주체는 이 조사에서 확정하지 않았으며 현재 값을
그대로 보존했다. 이 작업에서는 실제 HostAccess 승인·재바인딩·취소를 호출하지 않았다.

## 적용 경계와 후속

현재 설치본의 도구 발견과 **실제 ChatGPT 연결의 도구 목록 갱신**은 다르다.
passive tunnel catalog는 재확인 시 `awaiting_discovery`였다. 새로고침을 관측한 것처럼
표시하거나 기존 rollout receipt를 성공으로 고치지 않는다.

보완본의 설치와 터널·관리 웹 재시작은 아래 별도 승인 기록대로 완료했다.
독립 worker를 통한 실제 서비스 교체 후 생존 검증은 아직 미수행이며, 다음 승인된
웹 업데이트에서 확인해야 한다. 이번 배포는 기존 호스트 SSH/TTY 경로를 사용했다.
현재 웹 도구는 이미 설치돼 있어 최초 bootstrap이 무조건 필요하다는 이전 안내는
현재 상태에 해당하지 않는다. 기존 SSH/TTY ROLLOUT 경로도 유지한다.
MCP/관리 웹의 잠시 끊김과 권한 fingerprint 재검증 가능성을 적용 전에 검토한다.

동일 UID는 기존 신뢰 경계다. 승인 뒤 소스·frozen snapshot의 digest를 다시 검증하지만
같은 UID의 동시 변조를 커널 수준 불변성으로 차단한다고 주장하지 않는다.
실제 DB·Docker·API·Worker·Toss 배포나 권한 자동 갱신을 이 작업과 묶지 않는다.

## 별도 승인 후 운영 활성화 완료

사용자는 준비 결과와 두 서비스의 재시작 영향을 확인한 뒤 **“승인”**했다.
2026-10-16 KST, 승인된 후보와 control digest를 다시 검증하고 공식
`rollout-host-runtime.py --apply`를 실행했다. RTK의 출력 파이프 때문에 child TTY가
사라지는 문제를 피하도록 `/usr/bin/script`의 실제 OS PTY를 사용했다.
stdin/stdout의 isatty와 입력 전달을 사전 확인했고, 소스의 TTY/ROLLOUT 검사는 유지했다.
테스트 종료 후 실행 중 호스트 작업 0건, 기존 런타임 identity와 source/control 해시를
재확인하고 승인된 `ROLLOUT` 입력을 전달했다. 임의 skip 플래그·가짜 grant는 사용하지 않았다.

- 실제 실행 중 snapshot: `5c725c7a6d4ef86b9759`, manifest 80파일 모두 SHA-256 일치.
- MCP PID `1135927`, tunnel PID `1135896`, 관리 웹 PID `1136092`.
- `runtime_verified=true`, `tunnel_healthy=true`, `admin_status=admin_ready`.
- 배포 명령 안에서 승인 카드 10개, 런타임 승인 UI 7개, 대상 688개, 전체 853개 재통과.
- 실제 설치본의 별도 MCP stdio tools/list 70개와 웹 롤아웃 3개·Schema 014 도구 3개·
  두 선택적 task_label 스키마 확인. 도구 실행은 하지 않았다.
- 실제 플러그인의 `get_host_access(tossApp)` 읽기 호출 성공으로 터널 연결 복구 확인.
- 실제 관리 웹 로그인 뒤 정확한 `/runtime-rollouts` 승인 탭 진입, 없는 계획의
  승인 비활성화, personal-ops/tossApp/pim-check의 실제 데이터와 화면 확인.
  1440/390/320px 총 12개 화면 크기 검사, JS 오류 0, 인증 외 쓰기 요청 0.
- loopback/LAN 자산 12개 HTTP 200 및 설치본 해시 일치. 고정 HTTPS 승인 origin에서도
  인증 없는 승인 페이지·새 JS가 TLS 검증과 함께 200 및 설치본 해시 일치.
- 문서 갱신 전 원본 328파일·Git HEAD/index 보존. 독립 CLI 8/8, 컨테이너 11개의
  상태/시작 시간, 선별 설정·권한 파일 13개의 바이트·모드가 모두 그대로였다.
- 커밋·push·PR, 실제 DB SQL, Docker 변경, 권한 자동 갱신 없음.

**원본 배포 영수증은 종료 코드 2 / `verification_required` 그대로 보존한다.**
이는 설치·전환 실패가 아니다. 설치/실행 identity와 관리 웹은 검증됐지만 실제 같은
ChatGPT 연결의 전체 tools/list 및 새 스키마 갱신은 아직 관측되지 않았다.
passive catalog는 재확인 후에도 `awaiting_discovery`, `tools_registered=false`,
`connector_schemas_verified=false`다. 별도 stdio의 70개 발견 결과로 이를 덮어쓰지 않는다.
ChatGPT 연결 새로고침 후 새 웹 롤아웃 도구를 확인해야 한다.

권한 기록 자체는 바꾸지 않았다. 실제 조회 결과 personal-ops·tossApp은
`active=false / RUNTIME_CHANGED / installation`, pim-check는 `NO_GRANT`다.
호스트 실행이 필요하면 해당 채팅의 기존 호스트 권한 승인 카드에서 새 런타임 기준으로
승인한다. 이 배포 승인은 범용 호스트 작업 권한 승인을 대신하지 않는다.

활성화 증거는 위 증거 디렉터리의 `activation/`에 있다:
`receipts/rollout-38v3wa8a/receipt.json`, `tty-output.log`, `pre-switch-guard.json`,
`installed-verification.json`, `installed-catalog.json`, `actual-connector.json`,
`live-ui.json`, `live-catalog-and-admin.json`, `trusted-https-ui.json`.
최종 문서 업데이트는 README·DESIGN·이 인계 파일에만 적용한다.
