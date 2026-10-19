# Codex 관측기 복구

날짜: 2026-10-19 KST. personal-ops. 기준 HEAD: `bcab942717c72c8a3108488570ae3f6ad8a5efbf`.

현재 상태: 사용자 승인 후 관측기 전용 운영 전환 완료. 아래 승인 전 기록과 최종 적용 결과를 구분한다.

## 범위와 승인 전 적용 상태

사용자는 웹에서 tossApp 작업을 계속하고, 이 세션에서 personal-ops 후속 작업을 진행하도록
지시했다. 조사 중 실제 실패 중인 Codex 대시보드 관측기를 발견해 우선 복구한다.
새 모델 호출, 대화 재개·종료, 프로젝트 등록 변경, 웹 터널·관리 웹 재시작,
Docker·DB·스키마 변경, 업로드는 이 작업에 포함하지 않는다.

호스트 설정과 서비스 전환은 기존 별도 승인 조건을 유지한다. 이 문서의 조사 결과와
격리 검증은 운영 반영 완료를 뜻하지 않는다.

## 관측된 장애와 원인

- `personal-ops-codex-observer.service`는 `/usr/bin/python3`으로 immutable release
  `235ef828a3c5e15ca73f/codex_observer.py --interval 2`를 실행한다.
  종료 코드 1, `auto-restart`, 재시작 카운터 250회를 관측했다.
- journal에는 `codex-observer error=RuntimeError`만 남아 실제 실패 단계를 알 수 없다.
- 프로젝트 설정의 13개 항목 중 `pim-check-camera13`이 가리키는
  `/home/jhw/ai/opencode/worktrees/jhw-control/wt-camera13-rework`는 존재하지 않는다.
  관측기의 `_load_projects`는 이 한 항목 때문에 전체 로딩을 중단한다.
  같은 저장소의 `server.load_projects`는 누락된 디렉터리를 경고 후 제외한다.
  나머지 12개 프로젝트 디렉터리는 존재한다. 기존 등록 파일은 수정하지 않았다.
- 관측기가 요구하는 `~/.codex/app-server-daemon/app-server.pid`는 없다.
  그러나 `~/.codex/app-server-control/app-server-control.sock`은 현재 수신 중인
  `/tmp/codex-daemon-1003/` 아래 사용자 소유 Unix 소켓을 가리킨다.
  제어 디렉터리는 사용자 소유 0700, 실제 소켓은 사용자 소유 0600이다.
- 현재 호스트 CLI는 `codex-cli 0.163.0`이다. 로컬 CLI의 `--listen unix://` 설명과
  [공식 App Server 문서](https://learn.chatgpt.com/docs/app-server)는 제어 소켓의
  WebSocket 전송을 지원한다. 관측 때문에 daemon을 새로 시작할 필요가 없다.

## 변경 전 실제 읽기 전용 확인

기존 제어 소켓에 직접 읽기 전용 연결을 열고, 메모리 저장소로 기존 정규화를 실행했다.
`initialize` 1회, `thread/loaded/list` 1회, `thread/read(includeTurns=false)` 4회만 호출했다.
personal-ops의 로드된 세션 4개를 관측했으며 상태는 running 2개, unknown 2개였다.
unknown은 실패나 완료로 바꾸지 않았다. 모델·계정 사용량 호출, 대화 resume/start,
실제 대시보드 상태 기록은 없었다.

## 수정·검증 및 운영 전환

삭제된 디렉터리 제외, 안전한 현재 제어 소켓 탐색, 비밀값을 출력하지 않는 오류 코드의
회귀 검사를 먼저 작성한다. 기존 PID 기반 탐색은 호환 경로로 유지한다. 변조되거나
불명확한 현재 제어 경로를 임의의 다른 소켓·프로세스로 대체하지 않는다.

수정된 관측기는 다음 경계를 유지한다.

- 경로 해석의 `FileNotFoundError`인 프로젝트만 경고 후 제외한다. 잘못된 설정, 상대 경로,
  일반 파일, 실제 권한 오류는 거부하고 사용 가능한 프로젝트가 하나도 없으면 시작하지 않는다.
- 현재 제어 디렉터리는 사용자 소유의 실제 디렉터리여야 하며 그룹·다른 사용자 쓰기가
  허용되지 않아야 한다. 제어 항목과 실제 소켓의 소유자·종류·쓰기 모드를 검증하고,
  링크 대상은 고정 Codex 소켓 prefix로 제한한다. 고정 제어 경로의 직접 소켓도 지원한다.
- 디렉터리·제어 항목·소켓의 정체성과 링크 대상을 재확인한다. 검증 중 변경되거나
  안전하지 않은 현재 제어 경로는 다른 경로로 우회하지 않는다. 제어 경로가 없을 때만
  기존 PID 기반 탐색을 사용한다. 프로세스·소켓·PID 파일을 만들거나 수리하지 않는다.
- 오류는 예외 종류와 고정 공개 코드만 출력한다. 임의 예외 메시지·경로·인자·비밀값은
  로그로 전달하지 않는다. 누락된 프로젝트 경고에도 원시 디렉터리 경로를 넣지 않는다.

기존 관측기 검사 21개는 수정 전 통과했다. 삭제된 작업 디렉터리·PID 없이 유효한 현재
제어 소켓·실패 코드 표시의 회귀 3개가 수정 전 실패하는 것을 확인했다.

- SDK Python 전체 검사: 1000개 실행, 실패 0, 실제 Codex probe 옵트인 1개 skip
  (999개 통과, 191.686초). 관련 없는 실제 agent/model 호출은 하지 않았다.
- 최종 관측기 검사: OS Python 3.10과 SDK Python 각각 36/36 통과.
  전체 검사 이후 독립 검토에서 제안한 빈 제어 디렉터리의 기존 PID 호환 사례도 추가했다.
  검사 수는 중복 합산하지 않는다. 제품 코드는 전체 검사 후 바뀌지 않았다.
- 독립 코드 검토 APPROVE. 필수 수정 항목은 없었고, 선택적 테스트 보강 항목을 완료했다.
- Ruff 전체 Python 검사, Python 구문, diff/공백 검사 통과. 변경 코드·테스트·문서의
  Gitleaks 검사 결과 비밀값 발견 0개.
- 초기 회귀 실패·테스트 fixture 오류·수정 후 통과 로그는 별도 파일로 보존한다.

수정본을 운영 경로와 분리한 비공개 설치 디렉터리에 묶었다. 후보 snapshot은
`a5827f53701d19619a9c`, manifest의 88개 파일 내용·해시를 확인했다.
실제 서비스와 동일한 `/usr/bin/python3`으로 그 설치본의 `--once`를 실행했고 exit 0이었다.
임시 `--state-dir`에만 상태를 저장했으며 `recorded=4 tracked=4`를 확인했다.
서버 쪽 대화 resume·모델 호출·사용량 조회·전역 대시보드 상태 기록은 하지 않았다.

### 승인 후 적용할 정확한 범위

운영에는 아직 설치·전환하지 않았다. 후보의 승인 대상은 다음뿐이다.

1. 검증한 88개 파일을 새 immutable release
   `~/.local/lib/chatgpt-coding-mcp/releases/a5827f53701d19619a9c/`에 설치한다.
2. `personal-ops-codex-observer.service`의 기존 `ExecStart`에서 관측기 Python 파일의
   release 경로만 후보로 변경한다. Python·2초 간격·나머지 unit 설정은 그대로다.
3. user systemd 설정을 다시 읽고 **관측기 서비스 하나만** 재시작한다. 재시작 반복이
   멈추는지와 실제 대시보드의 관측 이벤트를 확인한다.

단계 2의 정확한 unit 초안은 증거 디렉터리의 `activation-draft/`에 있다.
`systemd-analyze --user verify`는 exit 0이었다. 기존 unit·프로젝트 설정의 SHA-256을
보존하고, 실제 적용 직전 다른 세션에서 변경되지 않았는지 다시 확인한다.
터널·관리 웹·Codex daemon·기존 대화·호스트 실행 권한은 전환 대상이 아니다.
호스트 설치와 unit 변경·서비스 재시작은 기존 별도 승인 조건에 따라 승인 전 실행하지 않는다.

### 승인 전 원본 반영과 남은 단계

원본 반영 범위는 `chatgpt-mcp/codex_observer.py`,
`chatgpt-mcp/tests/test_codex_observer.py`, 이 인계 문서뿐이다.
기준선 해시를 비교하고 다른 세션의 충돌 파일은 덮어쓰지 않는다. 반영·보존 결과는
증거 디렉터리의 `source-integration.json`을 기준으로 판단한다.
이번 복구 변경의 Git commit/push는 수행하지 않는다.

승인 전에는 운영 서비스가 이전 관측기 경로를 사용했다. 소스 수정·비공개 설치 검증을
실제 운영 복구와 혼동하지 않는다. 별도 승인 후 위 관측기 전용 전환과 실제 관리 웹
이벤트 갱신 확인이 남아 있다. 웹의 tossApp 빌드·업로드 및 호스트 명령 작업은 별도 세션 범위다.

호스트 전용 증거: `~/.local/state/personal-ops-observer-recovery/20261019-xkfy12ra/`.
격리 소스는 그 아래 `candidate/`이며, 원본의 다른 세션 문서 변경과 미추적 보고서 7개를 보존한다.

## 승인 후 운영 적용 결과 (2026-10-19 13:30 KST)

사용자가 관측기 전용 적용을 승인했다. 위 미적용·승인 대기 내용은 승인 전 기록이며,
현재는 아래 범위의 운영 전환을 완료했다.

- 승인한 snapshot `a5827f53701d19619a9c`를 호스트 immutable release에 설치했다.
  manifest의 88개 파일 해시를 다시 확인했다. 기존 release는 삭제·수정하지 않았다.
- 기존 관측기 unit의 `ExecStart` release 경로만 바꿨다. 이전 unit은 사용자 전용
  증거 디렉터리에 0600으로 보관했다. `systemctl --user daemon-reload` 후
  `systemctl --user restart personal-ops-codex-observer.service`만 실행했다.
- 실제 PID 1412435, `/usr/bin/python3`, 새 release의 `codex_observer.py` 실행을
  `/proc`와 systemd 양쪽에서 확인했다. active/running, 종료 상태 0,
  NRestarts=0이 확인됐고 후속 UI 검증 후에도 같은 PID로 유지됐다.
- 최초 관측은 `recorded=4 tracked=4`, 이후 2초 간격 조회는 `tracked=4`로 유지됐다.
  누락된 프로젝트 경고는 한 번 남고 정상 관측을 계속한다. 값이 바뀌지 않은 주기의
  `recorded=0`은 장애가 아니며, 활동 중 세션의 heartbeat는 기존 30초 정책을 따른다.
- 관리 웹 PID 3330과 start tick은 그대로다. 터널·관리 웹 runtime은
  `d39917b42f6a1bb504da`를 유지했다. 새 release에 포함된 MCP server를 활성화한 것은 아니다.
  CLI `runtimes status`는 이 경로를 반환했지만 상태 필드는 unknown이었으므로
  그 출력만으로 터널 health를 판정하지 않았다.
- 독립 대화형 CLI 10개의 PID/start tick을 전후 비교했고 변경 0, 관측 누락 0이었다.
  Codex daemon이나 대화를 재시작·재개·종료하지 않았다. 프로젝트 등록 설정의
  SHA-256·모드도 유지했다. Docker·DB·업로드·모델 호출은 수행하지 않았다.

### 실제 관리 웹 확인

LAN `http://192.168.2.70:8765/`의 personal-ops 작업실을 실제 Chromium으로 확인했다.
1440px 데스크톱과 390px 휴대폰 크기에서 실행 중 Codex 캐릭터 1개, 작업 요약과
기본 아바타 애니메이션 변화를 확인했다. 브라우저 오류·휴대폰 가로 넘침은 없었다.
인증 세션 생성 외의 POST/PUT/DELETE 요청은 없었고 승인·실행 버튼을 누르지 않았다.
관리자 토큰이나 인증 URL을 증거 파일·콘솔에 출력하지 않았다.

관리 API의 최신 이벤트는 약 6.67초 전이었다. 최신 actor별 누적 기록은 11개
(running 1, unknown 10)였으며, 이는 현재 daemon에서 추적하는 로드된 세션 4개와
집계 범위가 다르다. 과거 기록을 현재 작업 수로 합산하거나 unknown을 완료로 바꾸지 않았다.
초기 UI 검사는 검증 스크립트가 이벤트 키를 `work_events`로 잘못 읽어 실패했다.
실제 응답의 `records` 키를 사용한 최종 검사는 통과했다. 제품 코드는 이 과정에서 바뀌지 않았고
실패·통과 보고서를 모두 보존했다.

이번 복구는 현재 세션 활동 상태의 수집 경로를 복원한다. 개별 스킬 발동·세부 도구 행동이나
추론 본문은 수집하지 않는다. 정확한 스킬 이벤트와 발동 효과는 다음 대시보드 단계다.
이번 소스·테스트·문서는 여전히 미커밋 상태이며 push는 하지 않았다.

운영 적용 증거: 위 증거 디렉터리의 `activation-tldnuem4/`.

- `preflight.json`, `original-observer.service`, `installed.json`
- `service-samples.json`, `cli-before.json`, `cli-after.json`, `result.json`
- `dashboard-live-verification.json`: 초기 검증 스크립트 실패
- `dashboard-live-verification-final.json`: 최종 LAN UI 검증 통과
- `dashboard-desktop.png`, `dashboard-mobile.png`
- `tunnel-after.json`: 터널 경로만 읽기 전용 확인
