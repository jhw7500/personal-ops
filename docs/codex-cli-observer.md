# Codex CLI 작업실 수집기

Codex CLI의 로컬 App Server 상태와 신뢰된 후크 메타데이터를 기존 RPG 작업실의
공통 이벤트로 연결한다. 새 추론 모델을 호출하거나 기존 대화의 실행 권한을 가져오지 않는다.
허용 목록의 호스트 로컬 디렉터리에 속하는 세션만 수집한다.

```text
Codex App Server -- read-only polling --+
Codex trusted hooks -- local reporter --+--> ProjectState --> admin --> RPG map
External MCP -- authenticated reports --+
```

## 수집 범위

| Input | Result | Evidence |
| --- | --- | --- |
| Loaded thread ID + status | One persistent agent; active / waiting / inactive | observed |
| Thread configured model + effort | Metadata explicitly labeled configuration | observed |
| Trusted Pre/PostToolUse | Same tool call starts / returns; action from tool name only | observed |
| Trusted turn/subagent hooks | Supplied actual IDs and lifecycle, no guessed success | observed |
| Optional single matching usage group | Estimated cumulative session counters supplied by backend | observed |
| Other MCP call reported through HTTP/MCP | Explicitly reported metadata | reported |

App Server에서는 `initialize`, `initialized`, `thread/loaded/list`,
`thread/read` (`includeTurns:false`)와 선택적인 `account/usage/read`만 사용한다.
`thread/resume`, 새 대화 생성, 원격 연결 변경, 후크 신뢰 변경은 수집기에 없다.
기존 Codex 공유 서버를 재시작할 필요가 없다.

`active`는 세션 활동 상태다. 실제 추론 시작 신호가 아니므로 생각하는 동작으로 바꾸지 않는다.
`waitingOnApproval`과 `waitingOnUserInput`만 대기로 분류한다.
세부 도구 신호가 있으면 일반 세션 상태보다 해당 작업 동작을 우선한다.
유휴·사라진 세션은 성공으로 만들지 않고 종료 상태 미확인으로 처리한다.
실행 중 신호는 기존 120초 만료 규칙을 따른다.

## 모델과 토큰

`model_source: configuration`은 **설정값**으로 표시한다. 후크의 모델에는
`model_source: event`를 붙인다. 모델 이름은 인물이 아니라 해당 에이전트의 메타데이터다.
설정된 추론 수준과 실제 추론 동작은 서로 다른 정보다.
추론 수준은 `reasoning_effort_source`를 별도로 기록하므로 모델 이름이 없어도 설정값임을 표시한다.
후크의 공통 `session_id`로 직계 부모를 추측하지 않는다. 부모 관계는 App Server가 실제
제공한 `parentThreadId`로 기록한다.

사용량 수집은 기본적으로 꺼져 있다. `--read-usage`를 지정하면 최소 60초 간격으로
조회하며, 다음 조건을 모두 충족한 `threadUsage.groups` 한 개만 사용한다.

- 응답 `threadId`가 조회한 세션과 일치한다.
- 그룹이 정확히 하나이며 모델·추론 수준이 해당 세션의 제공된 설정과 일치한다.
- 제공된 카운터가 유효한 음이 아닌 안전 정수다. null·누락은 버리고 0으로 바꾸지 않는다.

여러 그룹을 임의 선택하거나 더하지 않는다. 캐시 입력을 재가산하지 않으며 합계를 계산해
만들지도 않는다. `usage_scope: session`, `usage_estimated: true`로 표시하고,
새 폴링 값은 누적 스냅샷을 교체한다. 토큰 제공 여부와 실제 추론 구간은 별도로 검증해야 한다.
2026-10-12 현재 계정의 실제 조회는 `threadUsage:null`이었다.

## 개인정보 경계

프롬프트, 메시지, turns, 도구 인자·결과, transcript, 내부 추론, 환경 변수 및 임의 필드는
저장하지 않는다. App Server 요약에 포함된 preview/name도 버린다.
경로는 허용 프로젝트를 찾는 데만 사용하며 공통 이벤트에 원본 cwd를 넣지 않는다.
후크 입력은 256 KiB로 제한하며 오류나 설정 누락에도 `{}`와 종료 코드 0을 반환한다.
설정·저장 오류는 입력이나 오류 원문 대신 고정된 단계 이름의 진단을 stderr로 남긴다.
관측기는 `JobManager`를 만들지 않아 실행 기록을 중단 상태로 바꾸지 않는다.

## 설치와 실행

기존 Serena Python으로 `chatgpt-mcp/install-runtime.py`를 실행하면 후크·관측기까지
포함한 불변 스냅샷 경로가 반환된다. 관리 화면과 터널에는 같은 경로를 지정한다.
관측기는 호스트에 이미 설치된 `/usr/bin/python3`와 `websockets` 16을 사용한다.
Serena Python 환경에는 WebSocket 패키지가 없으므로 관측기 실행에는 사용하지 않는다.
새 패키지를 자동으로 설치하지 않는다.

반환된 런타임의 `codex_observer.py --once`로 확인한 뒤 동일한 명령을
`personal-ops-codex-observer.service` 사용자 서비스에서 실행한다.
기본 조회 간격은 10초이며 `--projects`, `--state-dir`, `--codex-home`, `--interval`로
범위를 명시할 수 있다. 서비스는 실패 시 재시도하고 사용자 로그인/linger 설정에 따라 시작한다.
RPC 요청의 전송·응답 수신 전체와 `initialized` 알림의 전송에는 각각 10초 제한이 있다.
무관한 알림을 계속 받아도 요청 기한은 늘어나지 않는다. 시간 초과 시 비공개 내용을 포함하지
않는 오류로 종료하여 서비스의 실패 후 재시작 경로로 복구한다.
실시간 화면 개선에서는 운영 서비스에 `--interval 2`를 지정한다. 상태·설정·제공된 사용량이
바뀌면 즉시 기록하지만, 변화 없는 활성 세션은 30초 간격의 하트비트만 기록하고 비활성 세션은
재기록하지 않는다. 사용량을 조회하지 않은 하트비트와 사라진 세션 이벤트는 기존 값의 조회
시각을 갱신하지 않는다. 단순 반복 상태가 실제 작업 기록을 밀어내지 않도록 하기 위한 동작이다.

후크는 기존 `~/.codex/hooks.json`을 보존한 채 다음 이벤트에 로컬
`codex_hook_report.py` 명령을 추가한다:
`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `Interrupt`,
`SubagentStart`, `SubagentStop`.
각 명령은 `async:true`로 실행한다. 새 정의는 Codex의 **`/hooks` 검토·신뢰 절차**를
거쳐야 활성화된다. 수집기는 신뢰 해시를 만들거나 그 절차를 우회하지 않는다.
신뢰되기 전에도 읽기 전용 관측기는 세션 상태와 모델 설정을 수집할 수 있다.

Claude CLI도 같은 source/actor/task 계약과 비공개 데이터 경계를 사용한다.
별도 네이티브 후크 어댑터의 설치와 검증 범위는 [Claude 수집기](claude-cli-observer.md)를 참고한다.

## 2026-10-12 최초 수집기 배포 확인 (당시 기록)

- 런타임: `0b6ead3b0281c28dbea8` (24개 파일의 manifest·소유자·0600 모드 검증).
- 관리 화면·터널·관측기 서비스 모두 active / enabled. 사용자 linger도 켜져 있다.
- 현재 root 세션이 한 에이전트로 표시되고 모델 `gpt-6.1-sol`, 추론 수준 `max`가
  설정값·직접 관측으로 표시된다. 실제 운영 Chromium 확인에서 페이지 오류는 없었다.
- 실제 Notion MCP 조회의 시작·반환을 보고 경로로 저장하고 UI의 `보고됨` 표시를 확인했다.
  다른 MCP를 자동 도청하는 기능은 아니다.
- 후크 7개는 `hooks/list`에서 enabled / untrusted, 설정 오류 0개로 확인했다.
  **호스트 Codex CLI에서 `/hooks`로 `codex_hook_report.py` 정의를 검토·신뢰하는 단계가 남았다.**
  아직 실제 도구 후크 실행을 완료했다고 주장하지 않는다.
- 최종 Python 139개, JavaScript 65개, 관리 화면 Chromium 29개 테스트가 통과했다.
  Ruff·구문·공백 검사와 비밀값 검사도 통과했다. 브라우저 테스트의 종료 시점에 기존
  `BrokenPipeError` 로그가 남지만 29개 테스트는 모두 통과했다.
- Codex 공유 서버 PID와 기존 세션은 유지했다. 실제 호스트 재부팅 검증은 수행하지 않았다.
- 기존 MCP·작업실 커밋은 원격 브랜치에 보관했고 이번 수집기 변경은 해당 작업 폴더에서
  아직 미커밋이다. PR·merge는 수행하지 않았다.

관측기 재시작과 상태 확인:

```bash
rtk proxy systemctl --user restart personal-ops-codex-observer.service
rtk proxy systemctl --user status personal-ops-codex-observer.service --no-pager
```

이 배포의 비공개 설정 백업·검증 기록은
`~/.local/state/personal-ops-codex-observer-rollout/20261012/`에 보관했다.

## 2026-10-12 실시간 화면 후속 확인

- 관리 화면 런타임은 `10c5f15b9f8caf4ca602`, 관측기 런타임은 `3acb3d8089d143114c35`이다.
  MCP 서버 코드와 계약은 동일하므로 터널은 기존 `0b6ead3b0281c28dbea8`을 유지했다.
  후크 정의·신뢰 상태와 기존 Codex 공유 서버 PID도 변경하지 않았다.
- 운영 관측 서비스는 2초 간격으로 상태를 확인한다. 실제 변화는 바로 기록하고,
  변하지 않는 활성 상태는 30초 하트비트만 기록한다. 사용량·모델의 오래된 값을 새 조회처럼
  표시하지 않으며, 비활성 상태의 반복 기록도 억제한다.
- 작업실 조회를 느린 상세 조회와 분리했다. 실제 Chromium에서 활동 조회 간격은
  약 1.05~1.08초였다. 최근 변화·방 강조·작업 효과는 의미 있는 상태/동작 변화에만 표시한다.
- 실제 터널의 `git_status` 시작·반환을 수행해 운영 화면의 `Git 상태 · 요청 반환 · 직접 관측`
  알림과 검토실 강조를 확인했다. 과거 최초 조회·하트비트는 새 알림으로 만들지 않는다.
- 데스크톱과 390px 모바일 뷰포트, 동작 감소 설정, root 인물 한 명 유지, 페이지 오류 0개를 확인했다.
  휴대폰 실제 앱을 직접 조작한 검증은 아니다.
- 최종 Python 144개, JavaScript 72개, 관리 화면 Chromium 32개, 갱신 Chromium 4개 검사 통과
  (합계 252개). 기존 테스트 종료 시점의 `BrokenPipeError` 로그는 비공개 검증 로그에 보존했다.
- 세부 네이티브 도구 후크의 `/hooks` 신뢰 검토, 토큰 제공 여부와 실제 재부팅 검증은
  이전과 같이 남아 있다. 이번 UI 작업은 그 절차를 우회하지 않는다.
- 후속 소스는 `feat/personal-ops-tunnel-work-dashboard` 브랜치에서 관리한다.
  실제 커밋과 원격 반영 여부는 해당 작업 폴더의 `git log`와 브랜치 상태로 확인한다.

비공개 운영 확인·설정 백업·스크린샷은
`~/.local/state/personal-ops-ui-live-review/20261012/`에 보관했다.
현재 화면 설계 기준은 저장소의 [DESIGN.md](../DESIGN.md)다.

## 2026-10-12 커밋 전 안정성 확인

마지막 코드 리뷰에서 응답 없는 RPC가 관측기를 무기한 멈출 수 있는 문제를 발견했다.
응답 없음·요청 전송 정지·무관한 알림 연속 수신·초기화 알림 전송 정지의 회귀 검사 4개를
먼저 실패시킨 뒤 10초 제한을 적용했다. 호스트 Python 3.10의 관측기 검사 21개와
Python 전체 148개, JavaScript 72개, 관리 화면 32개, 갱신 4개 검사(총 256개)가 통과했다.
수정 사항의 후속 코드 리뷰도 승인됐다.

현재 관측 서비스는 런타임 `235ef828a3c5e15ca73f`를 2초 간격으로 실행한다.
새 런타임의 실제 `--once` 조회가 성공했고 서비스의 실행 경로·활성 상태를 확인했다.
관리 화면 `10c5f15b9f8caf4ca602`, 터널 `0b6ead3b0281c28dbea8`, 후크 정의와
기존 Codex 공유 서버 PID는 유지했다. 비공개 설정 백업과 검증 기록은
`~/.local/state/personal-ops-commit-review/20261012/`에 보관했다.

## 2026-10-12 후크 활성화 확인

사용자의 진행 요청에 따라 등록된 수집 후크 7개의 명령·출처·비동기 설정·3초 제한과
불변 런타임을 검토했다. 실행 중인 Codex 0.162.1의 `hooks/list`가 반환한 정의는 모두
검토한 소스와 일치했고 설정 오류는 없었다.

해당 버전의 `/hooks` 화면과 같은 `config/batchWrite` 경로로 검토한 7개 항목의
`key`와 `currentHash`를 그대로 사용했다. `hooks.state`를 `upsert`하고
`reloadUserConfig:true`로 로드된 세션에 적용했다. 사용자 설정의 현재 version으로
동시 변경을 검사했으며, 별도 해시 계산·신뢰 우회 플래그·직접 설정 파일 편집은 하지 않았다.
재조회에서 7개 모두 `trusted / enabled`로 확인했다. 다른 후크·설정 값·후크 정의와
기존 Codex 공유 서버 PID는 유지됐다.

실제 이 세션의 쉘 도구 `Bash` 호출에서 동일한 도구 ID의 `started / returned` 기록을
직접 관측했다. 운영 Chromium에서는 root Codex 인물이 한 명으로 표시되고 실행실의
작업 효과와 Bash 상세가 나타났으며 페이지 오류는 0개였다. 네이티브 후크 활성화와
쉘 호출 경로는 확인했고, 턴·중단·자식 에이전트 후크의 실제 사건별 검증은 이후 해당
사건이 발생할 때 확인해야 한다. 토큰 제공 여부와 실제 재부팅 검증도 남아 있다.

비공개 설정 백업·네이티브 정의·활성화 결과·화면 검증은
`~/.local/state/personal-ops-hook-activation/20261012/`에 보관했다.
소스 근거는 고정 버전의
[TUI 후크 신뢰 저장 경로](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/tui/src/hooks_rpc.rs#L58-L91)다.

## 근거

동작은 설치된 Codex 0.162.1과 다음 공식 자료를 기준으로 확인했다.

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks): 이벤트·입력 필드·정의 신뢰 절차.
- [Codex App Server](https://learn.chatgpt.com/docs/app-server): 읽기 전용 thread 조회와 상태.
- [Pinned thread metadata](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs#L244-L249): 모델·effort의 설정값 의미.
- [Pinned usage response](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/app-server-protocol/src/protocol/v2/thread_usage.rs#L9-L29): 그룹별 추정 누적 사용량과 nullable 카운터.
