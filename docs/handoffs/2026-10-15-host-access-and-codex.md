# 기간제 호스트 권한과 기존 Codex 호출 인계

2026-10-15. 사용자 배포 승인 후 운영 런타임 전환, tossApp 4시간 호스트 권한 발급,
실제 ChatGPT 터널 경유 Codex 호출 검증까지 완료했다. 새 도구의 ChatGPT 목록 반영은
연결 새로고침 후 별도로 확인해야 한다.

## 사용자 결정과 구현

기존 Codex 호출을 유지하고 그 위에 기간제 호스트 권한을 두어, 유효한 기간에는
작업마다 웹 승인을 다시 요구하지 않는다. 권한 부여만으로 Codex를 호출하지 않는다.

```text
Owner grant (up to 4h)
  +-> start_host_execution -> status / log / cancel
  +-> prepare_agent_task -> start_agent_task -> get_agent_task / cancel_agent_task
```

- 기간제 권한은 기본·최대 4시간, 선택 프로젝트를 쓰는 이 MCP 설치의 모든 호출자에게 적용된다.
  대화별 권한이 아니며, 프로젝트/cwd는 시작 위치이지 파일시스템 격리가 아니다.
- 실제 호스트 계정의 파일·네트워크·그룹 권한을 사용한다. 관리 화면에서 실제
  호스트·사용자·UID·그룹과 권한 범위를 먼저 표시한다. sudo 그룹이 비밀번호 없는
  sudo를 보장하지 않는다.
- 신규 도구 6개: request_host_access, get_host_access, start_host_execution,
  get_host_execution, read_host_execution_log, cancel_host_execution.
  요청·조회 도구는 권한을 발급하거나 연장하지 않는다.
- 기존 Codex 도구 5개를 유지한다. prepare의 execution_mode=auto는 실제 활성
  권한에 Codex 사용 동의가 포함될 때 host를 선택하고, 없으면 기존 격리 검토를 따른다.
  host는 실제 체크아웃에서 작업한다. isolated는 증명 실패 후 host로 전환하지 않는다.
- 기존 프로필의 모델·추론 수준·실행 파일을 고정한다. 현재 등록된 프로필은
  codex-tossapp-review, gpt-6.1-sol / high. 작업마다 새 명령 프로필을 등록하지 않는다.
  호스트 실행은 기존 격리 증명이나 CAS를 정상으로 바꾸지 않는다.
- Codex는 --no-daemon / exec / --ephemeral로 전용 프로세스를 시작한다.
  기존 사용자 세션을 resume하거나 종료하지 않는다. 권한은 전용 위임 도구를 허용하는
  설정이며, 범용 호스트 셸의 모든 모델 호출이나 과금을 기술적으로 제한하는 장치는 아니다.
- 만료·철회·재부팅·런타임 변경은 시작을 차단한다. GO 직전 재검증과 실행 중 검사를 수행하며,
  중단 요청은 소유한 프로세스 범위에만 적용한다. 이미 적용한 변경·업로드·외부 서비스는
  자동 롤백되지 않는다.

## 구현 위치와 검토 사항

주요 소스: chatgpt-mcp/host_access.py, host_execution.py, host_execution_tools.py,
host_agent.py, agent_tools.py, host_command_runner.py, host_command_entry.py,
admin_server.py, host-access-admin.js, admin.html, server.py.
설치/검증 목록, 호출 내역 기록, README와 기존 실행기 admission도 함께 수정했다.

- 기본 상태는 권한 없음이다. 기존 fresh_approval 인증/origin 검사를 그대로 사용한다.
- 작업 소유권 flock을 queued 저장 전에 확보한다. 별도 관리 웹 프로세스에서 보낸 취소는
  durable cancel_requested로 실제 소유자에게 전달된다. 정리 검증 실패 시 lease를 유지한다.
- 범용 실행은 공통 host.lock을 작업 전체에 보유한다. 기존 legacy/registered 실행기는
  같은 gate를 admission부터 durable queued 기록까지 보유한다. 경합 양쪽 순서를 테스트했다.
  수동 SSH 작업은 이 coordination 범위 밖이다.
- 관리 기록은 정확한 상태 파일만 읽으며 .approval.json은 상태로 취급하지 않는다.
  검증된 만료 approved 계획은 진행을 막지 않고, 잘못된 active 기록은 차단한다.
- 일반 명령 로그는 소유자 전용 파일에 보존하고 공개 페이지는 제한·마스킹한다.
  알려진 관리/런타임/Toss 키와 일부 패턴을 가리며 모든 비밀의 탐지를 보장하지 않는다.
  Codex 원시 JSON 로그는 공개하지 않고, 허용된 이벤트/공급자 사용량/모델 보고만 반환한다.
  JSON을 먼저 파싱하고 반환 문자열을 가려 최종 보고가 마스킹 때문에 유실되지 않게 했다.
- 작은 pipe의 stdin backpressure에서도 취소를 관측하도록 stdin 전송과 출력 읽기를 병행한다.
  실제 pipe 크기를 줄인 실패 재현 후 시스템/SDK Python 회귀 검사를 통과했다.
- Codex 바이너리는 294415048바이트로 기존 128MiB 한도를 넘고 sibling helper가 필요하다.
  host mode는 핀한 런타임을 private 0700 폴더/0500 파일로 복사하고 검증한 실제 파일 fd로 실행한다.
  기존 고정 명령은 sealed memfd 방식을 유지한다. 악의적인 같은 UID를 격리한다고 주장하지 않는다.

독립 코드 검토의 경합·계정 범위 표시·bare 관리 토큰 노출·state-root 연결·문서 문제를
수정하고 재검토 APPROVE를 받았다. stdin 처리 변경도 별도 제한 검토에서 APPROVE를 받았다.

## 검증된 결과

| Item | Evidence |
| --- | --- |
| Targeted Python suite | 608 passed |
| Full Python suite | 770 passed |
| UI JavaScript suites | 34 passed |
| Ruff and JS syntax | passed |
| Rollout preflight | preflight_ok; profile/service aligned |
| Candidate snapshot | 6cd2d5f148c7f65f4626; 70 runtime files |
| Candidate MCP SDK | 60 tools; all 6 new names; auto/isolated/host; NO_GRANT |
| Candidate admin HTTP | page/script/authenticated access API 200; isolated state; no grant |
| Current live snapshot | 6cd2d5f148c7f65f4626; MCP PID 1852412; tunnel PID 1852395 |
| Current admin | HTTP 200; status ok; protocol version 1.5.0 |

후보는 releases 아래에 저장한 뒤 아래 운영 전환 절차로 활성화했다.
전환 전 후보 SDK/HTTP 점검은 분리된 임시 상태로 실행해 운영 작업·승인 기록을 변경하지 않았다.
770개 전체 검사 중 최초에는 test_router의 구 도구 목록 기대값 하나가 실패했다.
새 6개를 독립 기대 목록에 반영하고 라우팅 검사 및 전체 preflight 재실행이 통과했다.
실패 receipt도 보존했다.

증거 디렉터리:
/home/jhw/.local/state/personal-ops-rollout-recovery/20261015/host-access-preflight

- 통과 receipt: rollout-__nv85sh/receipt.json
- 최초 실패 receipt: rollout-gg5_opd1/receipt.json
- candidate-sdk-evidence.json, candidate-admin-evidence.json
- protected-config-baseline.json

기준선은 /home/jhw/.local/state/personal-ops-worktrees/host-access-20261015-baseline.json,
반영 파일은 host-access-20261015-integrated.json에 기록했다. 기존 파일의 내용·모드를
비교하고 충돌 없이 반영했으며, 변경 대상으로 잡지 않은 기존 170개 파일은 그대로다.
host-runtime.json / host-commands.json / projects.json 내용·모드는 동일하다.
Docker 컨테이너/DB 데이터/스키마/배포/실제 모델 사용량은 이번 준비에서 변경하지 않았다.
커밋·push는 하지 않았다.

## 운영 적용 결과 (2026-10-15 12:19-12:21 KST)

사용자의 `배포해줘`를 앞서 제시한 런타임 전환·tossApp 4시간 권한·실제 Codex 호출
1회 검증 범위의 승인으로 받아 실행했다. 추가 모델 호출, DB 작업, Docker 변경은 하지 않았다.

- 현재 운영 snapshot: **6cd2d5f148c7f65f4626**, 70개 파일. 이전
  ce88340086af9f5e1bf2와 기존 스냅샷은 보존했다. 터널 healthy, admin_ready.
- MCP PID 1852412/start_ticks 24934490, tunnel PID 1852395/start_ticks 24934485.
  실제 Codex 실행 전·도중·후 동일했다. 실제 연결의 get_agent_task가 running과
  succeeded를 차례로 반환해, 위임 호출마다 연결을 끊거나 서비스를 재시작하지 않음을 확인했다.
- 설치된 동일 런타임을 실제 MCP SDK로 초기화해 60개 도구와 신규 6개, prepare의
  auto/isolated/host schema를 관측했다. 관리 페이지·스크립트·인증 API는 HTTP 200,
  호스트 권한 패널/승인 endpoint와 실제 에이전트 완료 결과 조회를 확인했다.
- 실제 owner HTTP 승인 경로(새 관리 토큰, loopback Origin, 정확한 프로젝트·본문)로
  tossApp/Codex 권한을 발급했다. grant_id 3a9962c55430284bec9163bf2d225de2,
  **2026-10-15 16:19:34 KST 만료**. 기존 기간·철회·재부팅·런타임 변경 정책을 유지한다.
- 같은 grant로 서로 다른 일반 명령 pwd/uname을 실행했다. 각각 실제 tossApp 경로와
  Linux를 반환했고, succeeded/exit 0/termination_verified=true를 확인했다.
  새 고정 명령 프로필이나 작업별 승인은 만들지 않았다.
- 실제 ChatGPT 연결의 기존 prepare/start/get 도구로 Codex 작업
  **4908ef9968e9847936d63635129aa41a**를 1분 제한으로 실행했다. auto가 host를 선택했고
  approval_required=false, succeeded, exit 0, duration 12315ms,
  termination_verified=true를 관측했다. 설정은 codex-tossapp-review / gpt-6.1-sol / high.
  모델 보고는 tossApp 경로와 `.nvmrc`의 `24.12.0`이며 파일 변경 없음이다.
  실제 Git status와 HEAD diff 해시도 실행 전후 일치했다. 이 점검은 빌드·DB 기능 검증은 아니다.
- 공급자가 보고한 사용량: input_tokens 53648(그중 cached_input_tokens 26624),
  output_tokens 279(reasoning_output_tokens 89). 추정치나 무료 호출로 표시하지 않는다.
- host-runtime.json / host-commands.json / projects.json 내용과 0600 모드는 그대로다.
  기준선의 Codex/Claude 9개 프로세스 중 8개는 동일 PID/start_ticks로 존재했다.
  125675는 전환 후 종료로 관측됐으며, 이 단순 기준선에는 당시 부모/명령 인자를 저장하지 않아
  해당 프로세스의 세부 역할을 이 기록만으로 확정하지 않는다. 별도 사용자 세션을 종료하는
  명령은 실행하지 않았다. Docker·DB·Schema 013·Toss 배포는 변경하지 않았다.
  커밋·push는 하지 않았다.

배포 도구는 두 차례 실행했다. 첫 실행에서도 608/770 검사가 통과했지만,
rtk proxy의 출력 파이프 때문에 HOST_TTY_REQUIRED로 **전환 전에** 중단됐다.
실제 PTY의 stdin/stdout 및 입력 전달을 독립 확인하고, 아래 명령으로 다시 실행했다:

```sh
rtk proxy /usr/bin/script -q -e -c 'env PYTHONDONTWRITEBYTECODE=1 /home/jhw/.local/share/uv/tools/serena-agent/bin/python -B chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261015/host-access-activation' /dev/null
```

두 번째 실행도 targeted 608/full 770 검사가 모두 통과했다. 사용자 승인에 근거해
확인 문구 ROLLOUT를 입력했고, 터널·관리 서비스 전환을 완료했다. PTY 자체를 승인 증거로
취급하지 않았다. UI 34개 검사는 준비 단계에서 통과했으며 실제 브라우저 렌더는 미검증이다.

증거는 아래 private 디렉터리에 보존했다:
/home/jhw/.local/state/personal-ops-rollout-recovery/20261015/host-access-activation

- rollout-2od9j42g/receipt.json: 첫 검사 통과/TTY 중단, 서비스 변경 없음.
- rollout-pls64nl5/receipt.json: 검사 통과, installed/switched/runtime_verified=true, admin_ready.
- host-postdeploy-verification.json: 60개 도구, 실제 grant, 일반 명령 2개.
- agent-real-smoke.json: 실제 ChatGPT 연결 prepare/start/get 결과 및 관측 사용량.
- final-observations.json: 같은 live PID, 관리 HTTP 결과, 설정/WIP 보존 비교.
- owner-host-grant.json / host-command-pwd.json / host-command-uname.json: 세부 영수증.

## 남은 확인과 별도 후속 작업

- 현재 ChatGPT 연결은 기존 도구 schema를 캐시하고 있다. **personal-ops-tunnel을 한 번
  새로고침**한 뒤 신규 request_host_access/get_host_access/start_host_execution 등의 노출을
  확인한다. 기존 Codex 도구의 실제 host 호출은 새로고침 전에도 위와 같이 동작했다.
  같은 터널 child의 health/tools_list는 아직 awaiting_discovery이므로 롤아웃 영수증은
  verification_required/exit 2로 남아 있다. 이는 서비스 전환 실패라는 뜻이 아니며,
  connector_schemas_verified=true 또는 최종 발견 완료로 바꾸어 기록하지 않았다.
- 기존 isolated 모드는 여전히 ISOLATION_UNVERIFIED이며 증명 재발급은 별도 유지보수다.
  host_ready=true를 격리 증명이 복구된 것으로 해석하지 않는다.
- 실제 브라우저 렌더, 운영 권한 만료·철회 중 실행 중단의 실환경 검증은 남아 있다.
  관련 회귀 검사는 통과했으며 현재 4시간 권한을 검증 때문에 조기에 철회하지 않았다.
- 서비스 수명 밖에서 수행하는 웹 self-rollout updater는 구현하지 않았다.
