# Task 2 실행 보고서

## 결과

기존 승인 카드, 관리자 API/UI, 범용 호스트 실행을 Task 1의 설치 범위
`timed`/`until_revoked` 권한 계약에 연결했다. 적합한 활성 권한은 새 요청이나 카드를
만들지 않고 재사용하며, 더 강한 lifetime·Codex 권한·남은 시간은 새 승인을 요구한다.
카드 확인 토큰은 계속 외부 응답에 노출하지 않고, 승인은 권한 발급 전에 카드를 spent로
저장한다.

## 구현

- `HostAccess`가 v1/v2 저장소별 카드 요청 생성, 확인, 교체, 폐기를 각 저장소 잠금 아래
  수행한다. UI 계층은 설치 권한 상태를 별도로 저장하지 않는다.
- 승인 도구는 optional `lifetime`을 받으며 `timed`는 양의 정수 duration,
  `until_revoked`는 null duration만 허용한다. v1 레거시 카드는 timed 형식을 그대로
  유지한다.
- 이미 적합한 활성 권한이 있으면 승인 도구는 pending 카드 없이 승인된 권한을 반환한다.
  revoke 뒤의 오래된 카드와 요청, lifetime 변경 전의 요청은 거부한다.
- 관리자 화면과 승인 카드는 설치를 실행하는 OS 계정 범위와 lifetime을 표시한다.
  관리자 API는 레거시 body를 timed로 해석하면서 lifetime 포함 body도 정확히 검증한다.
- 범용 실행은 v1 timed 만료와 v2 timed/until_revoked nullable 만료를 구분한다. timed
  권한만 job timeout을 남은 권한 시간으로 줄이며, until_revoked도 각 job timeout은
  계속 유한하다.
- 서버 안내는 활성 권한 아래의 임의 argv 실행에 `start_host_execution`을 사용하고,
  고정 프로필 도구는 해당 전용 workflow에만 사용하도록 명시한다. 쓰기 도구 annotation은
  read-only로 표시하지 않는다.
- 격리 agent plan TTL과 카드 TTL은 계속 유한하다.

## TDD 증거

### RED

구현 전에 다음 focused suite를 실행했다.

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --modules test_host_access_installation \
  test_host_execution test_host_execution_tools test_admin_host_access \
  test_chat_host_access_router --require-excluded-count 0
```

결과: 55개 중 예상한 2 failures와 9 errors. lifetime 인자·schema 부재,
nullable expiry 거부, 관리자 API의 optional body 거부가 새 요구를 실패시켰다.

Node/browser assertion도 구현 전에 작성했지만 별도의 RED 실행을 보존하지 않았다. 이
부분은 Python RED와 이후 GREEN/전체 회귀 증거로 검증했으며 RED를 실행했다고 주장하지
않는다.

### GREEN

같은 focused 명령이 구현 뒤 55개 모두 통과했다. 다음 확장 focused suite도 통과했다.

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --modules test_host_access test_host_access_installation \
  test_host_execution test_host_execution_tools test_admin_host_access \
  test_chat_host_access_router test_router --require-excluded-count 0
```

결과: 88 tests OK.

첫 전체 suite는 1,475개 중 4개가 실패했다. 모두 Task 2 레거시 승인 UI 회귀였다.

- public v1 grant에 새 lifetime 필드를 추가해 기존 shape assertion 2개가 실패했다.
- v1 카드의 소비된 요청이 `HOST_ACCESS_CARD_REQUEST_CONSUMED`보다 먼저 stale로
  판정됐다.
- 저장 책임이 `HostAccess`로 이동했지만 fault injection test가 이전 UI 저장 함수를
  patch했다.

v1 public shape를 유지하고, v1 binding 검증과 v2 backing-request 검증을 분리하고,
fault injection을 실제 저장 경계로 옮겼다. 다음 회귀 묶음이 72개 모두 통과했다.

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --modules test_chat_host_access_approval \
  test_host_access_installation test_host_access --require-excluded-count 0
```

## 최종 검증

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --discover tests
```

결과: 1,475 tests in 250.091s, OK, skipped=1. 공식 Yocto 테스트 5개는 runner 정책에
따라 제외됐다. Yocto/BitBake 명령은 실행하지 않았다.

```text
rtk /home/jhw/.nvm/versions/node/v24.12.0/bin/node \
  --test tests/test_host_access_ui.mjs
```

결과: 18 tests passed.

```text
rtk env PLAYWRIGHT_BROWSERS_PATH=/home/jhw/.cache/ms-playwright \
  /usr/bin/python3 -B tests/check_host_access_approval_ui.py
```

결과: 14 tests passed.

```text
rtk bash -lc 'tmp=$(mktemp -d); trap '\''rm -rf -- "$tmp"'\'' EXIT; \
  mkdir -m 700 "$tmp/config" "$tmp/state" "$tmp/cache"; \
  env XDG_CONFIG_HOME="$tmp/config" XDG_STATE_HOME="$tmp/state" \
  XDG_CACHE_HOME="$tmp/cache" \
  PLAYWRIGHT_BROWSERS_PATH=/home/jhw/.cache/ms-playwright \
  /usr/bin/python3 -B tests/check_admin_ui.py \
  AdminBrowserTest.test_host_access_lifetime_controls_match_installation_scope \
  AdminBrowserTest.test_host_access_duration_allows_thirty_days \
  AdminBrowserTest.test_host_access_duration_blocks_over_thirty_days'
```

결과: 3 tests passed.

초기 browser-runner 시도는 격리된 `XDG_CACHE_HOME`에서 Playwright browser를 찾지 못해
실행되지 않았다. 위 pinned browser 경로를 사용한 직접 bridge 검증은 통과했다.

```text
rtk /home/jhw/.local/bin/ruff check --select F,E9 <변경 Python 파일>
rtk git diff --check
```

결과: Ruff `All checks passed!`; diff whitespace 오류 없음.

## 자체검토

- 10개의 서로 다른 request ID가 한 권한을 재사용하고, 같은 request ID는 두 번 실행되지
  않는 테스트를 확인했다.
- `until_revoked`의 null expiry와 `timed`의 문자열 expiry 조합을 정확히 검증하며,
  malformed 조합을 허용하는 fallback을 추가하지 않았다.
- 활성 권한 재사용은 요청과 카드 파일을 만들지 않고, 더 강한 권한은 승인 흐름을 거친다.
- revoke 또는 lifetime 교체 뒤의 오래된 callback은 권한을 발급하지 못한다.
- UI에 설치 범위와 OS 계정을 표시하며, persistence와 권한 판정은 HostAccess 경계에
  남겼다.
- 계획 문서 변경은 root가 남긴 Task 1 완료 표시와 Task 2의 HostAccess helper 소유권
  설명만 포함한다.

## 범위와 제한

- 브라우저 검증은 실제 approval HTML과 JavaScript를 Chromium에서 실행한 bridge mock이다.
  live ChatGPT에서 관측한 end-to-end 결과가 아니다.
- live 설정, 기존 grant, 서비스, tunnel, target, PIM, Yocto 상태를 변경하지 않았다.
- 배포와 push를 수행하지 않았다.

## Fix round 1 — review I1/I2/I3

독립 리뷰의 세 open finding을 함께 수정했다.

- I1: `update_host_access_approval` schema와 secret-safe pre-validator가 HTML이 보내는
  optional lifetime을 허용하도록 연결했다. timed와 until_revoked의 정확한 HTML payload를
  `HostAccessApprovalTools.call`을 통해 전달하는 회귀 테스트와 schema assertion을 추가했다.
- I2: 정확한 v1 persisted card를 v2로 승격할 때 legacy authority를 뜻하는
  `grant_state_revision: null`을 채운다. update 뒤 재생성 manager load와 confirm/cancel을
  각각 검증한다.
- I3: 관리자 요청 선택 변경 시 lifetime 표시와 duration disabled 상태를 즉시 다시 그린다.
  timed→until_revoked와 until_revoked→timed 전환에서 화면 consent와 실제 POST body가
  일치하는지 검증한다.

### Fix round RED

다음 Python 명령은 새 테스트 22개 중 I1 1 failure와 I2 2 errors를 재현했다.

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --modules test_chat_host_access_approval \
  --require-excluded-count 0
```

관측 결과:

- update tool schema에 lifetime이 없었다.
- exact v1 card update 뒤 load가 `HOST_ACCESS_CARD_STATE_INVALID`로 실패해 confirm과
  cancel에 도달하지 못했다.

다음 Node 명령은 19개 중 mixed-request 테스트 1개가 실패했고, 선택 변경 뒤 화면이
`until_revoked` 대신 `timed`를 계속 표시했다.

```text
rtk /home/jhw/.nvm/versions/node/v24.12.0/bin/node \
  --test tests/test_host_access_ui.mjs
```

원래 Task 2의 Node/browser RED를 보존하지 못한 M1은 앞 절에 그대로 명시돼 있다. 이번
fix round의 새 cross-boundary와 mixed-lifetime RED만 새 증거로 기록한다.

### Fix round GREEN

동일 Python 명령 결과: 22 tests OK, excluded=0.

동일 Node 명령 결과: 19 tests passed.

관련 tool/card 모듈을 확장해 실행했다.

```text
rtk env PYTHONPATH=.:tests \
  /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B \
  rollout_test_runner.py --modules test_chat_host_access_approval \
  test_chat_host_access_router test_host_access_installation \
  --require-excluded-count 0
```

결과: 44 tests OK, excluded=0.

```text
rtk env PLAYWRIGHT_BROWSERS_PATH=/home/jhw/.cache/ms-playwright \
  /usr/bin/python3 -B tests/check_host_access_approval_ui.py
```

결과: 14 tests OK. 이 Chromium bridge도 backend tool schema를 직접 시행하지 않는 mock이다.
그 연결부는 위 `HostAccessApprovalTools.call` Python 회귀가 검증한다.

전체 admin Chromium suite 시도는 30초 tool 경계 안에 끝나지 않아 완료 출력을 확보하지
못했고, 남은 자체 시작 프로세스를 종료했다. 이 시도를 통과 증거로 사용하지 않는다.
I3는 실제 admin JavaScript를 synthetic DOM에서 실행하는 Node 테스트가 양방향 표시와
POST payload를 검증한다.

```text
rtk /home/jhw/.local/bin/ruff check --select F,E9 \
  chatgpt-mcp/host_access_ui.py \
  chatgpt-mcp/tests/test_chat_host_access_approval.py
rtk git diff --check
```

결과: Ruff `All checks passed!`; diff whitespace 오류 없음.

수정은 원래 Task 2의 네 파일에 한정했다. 전체 1,475개 suite는 bounded callback/UI
수정에 필요하지 않아 다시 실행하지 않았고, 앞 절의 최초 Task 2 전체 suite 증거와
구분한다.
