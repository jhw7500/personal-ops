# Claude CLI 작업실 수집기

Claude Code의 네이티브 후크가 제공한 메타데이터를 기존 RPG 작업실의 공통 이벤트로 기록한다.
Claude 모델을 새로 호출하거나 기존 세션의 실행 권한을 가져오지 않는다. 등록된 호스트 로컬
프로젝트 디렉터리와 그 하위 디렉터리에서 발생한 이벤트만 수집한다.

```text
Claude native hooks --> local reporter --> ProjectState --> admin --> RPG map
                       claude-local        observed         Claude CLI actor
```

## 동작과 식별자

`session_id`가 root 객체의 식별자다. 실제 `agent_id`가 함께 제공된 자식 객체는
`sub:<session_id>:<agent_id>`로 분리한다. 호출 시작과 종료에는 같은 `tool_use_id`를 사용한다.
즉시 부모 정보가 없는 입력으로 부모 관계를 만들지 않는다.

- `SessionStart`와 `SessionEnd`는 존재 확인으로 기록한다. 세션을 열어 둔 동안 일하는 동작을
  계속 표시하지 않으며, 세션 종료를 작업 성공으로 만들지 않는다.
- `UserPromptSubmit`, `Stop`, `StopFailure`에 실제 `prompt_id`가 있으면 같은 호출의
  시작·반환·실패를 기록한다. 없으면 일반 세션 활동의 실행 중·미확인·실패 상태만 기록한다.
- `PreToolUse`, `PostToolUse`, `PostToolUseFailure`는 도구 호출을 시작·반환·실패로 기록한다.
  실패 입력의 `is_interrupt:true`만 중단으로 표시한다. 도구 반환은 테스트 성공 증거가 아니다.
- `SubagentStart`와 `SubagentStop`은 제공된 자식 ID의 호출 시작·반환으로 기록한다.
- `Read`, `Glob/Grep`, `Edit/Write`, `Bash` 등의 도구 이름으로 읽기·탐색·코딩·실행 동작을 고른다.
  쉘 인자를 분석하지 않으므로 Bash로 실행한 테스트를 검사실 작업으로 추측하지 않는다.

실행 중 신호는 기존 120초 만료 규칙을 따른다. Claude 전용 폴링이나 하트비트는 없으므로
긴 호출은 종료 신호가 도착할 때까지 미확인으로 바뀔 수 있다. 후크의 `SessionStart.model`과
`effort.level`이 실제 제공된 경우만 메타데이터를 기록하며, 자식 모델이나 사용 토큰을 추측하지 않는다.
실제 추론 구간을 알려 주는 신호가 없으므로 추론 동작도 만들지 않는다.

## 개인정보와 실행 영향

프롬프트·메시지·도구 인자와 결과·오류 원문·환경 변수·transcript·내부 추론을 저장하지 않는다.
원본 cwd는 허용 프로젝트를 찾는 데만 사용한다. 로컬 기록의 출처는 `observed`다.

후크 입력은 256 KiB로 제한한다. 후크 프로세스는 내부 1초 제한을 적용해 입력이나 저장 잠금이
막혀도 종료 코드 0과 빈 stdout을 반환한다. 오류 진단에는 고정된 단계 이름만 사용한다.
비동기 후크의 외부 timeout 설정만으로 실행 시간을 보장하지 않는다. Claude의 승인 결정이나
추가 컨텍스트를 반환하지 않으며 새 의존성을 설치하지 않는다.

## 설치

```bash
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python \
  chatgpt-mcp/install-runtime.py
```

반환된 불변 런타임의 `claude_hook_report.py`를 `/usr/bin/python3`로 실행한다. 런타임은 허용
프로젝트 바깥에 설치하며 파일 manifest·소유자·0600 모드를 검증한다. 기존 관리 화면과 터널은
공통 이벤트 계약을 이미 지원하므로 이 어댑터를 연결하기 위해 재시작할 필요가 없다.

`~/.claude/settings.json`의 기존 설정과 후크를 보존한 채 다음 이벤트에 같은 로컬 명령을
`async:true`, `timeout:1`로 추가한다:
`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, `StopFailure`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`, `SubagentStop`.
등록 전에는 0600 백업과 동시 변경 검사를 수행한다. Codex 후크의 신뢰 상태는 수정하지 않는다.
파일 변경은 보통 실행 중인 Claude의 설정 감시기로 반영된다. 실제 세션의 `/hooks` 목록에
표시되지 않으면 해당 세션을 다시 시작해 설정을 읽도록 한다. 기존 세션을 강제로 종료하지 않는다.

## 2026-10-12 운영 연결 확인

- 검토한 후크 10개를 기존 사용자 설정에 추가했다. 기존 모든 설정 값과 후크 정의의 보존,
  사용자 소유·일반 파일·0600 모드와 변경 전 비공개 백업을 확인했다.
- 후크 명령은 불변 런타임 `9413d24e559a5901561c`의 reporter를 사용한다. 26개 파일의
  manifest와 소유자·권한을 검증했다. 관리 화면·터널·Codex 관측 서비스는 재시작하지 않았다.
- 실제 Claude Code `--init-only`에 격리 설정과 빈 MCP 설정을 지정했다. 모델 프롬프트를
  보내지 않았고 종료 코드 0, stdout/stderr 0바이트와 로컬 초기화 후크 기록 2개를 확인했다.
- 운영 Chromium에서 해당 세션의 Claude 객체는 한 명, 대기실·미확인·작업 효과 없음·직접 관측으로
  표시됐다. 상세의 `claude-local` 출처와 페이지 오류 0개를 확인했다.
- 어댑터 집중 테스트 16개와 런타임 설치 테스트 6개가 통과했고 코드 리뷰도 승인됐다.
  전체 Python 회귀 검사 166개가 통과했다. 후속 종료 처리 보완은 별도 검사 3개로 확인했다.
- 실제 Claude 도구 호출·턴 반환·실패·자식 작업, 모델/추론 수준 제공 여부와 재부팅은 아직
  사건별 운영 검증 전이다. 긴 호출의 만료 동작도 실사용에서 확인해야 한다.

설정 백업·등록 확인·초기화 기록·스크린샷은
`~/.local/state/personal-ops-claude-rollout/20261012/`에 0700/0600으로 보관했다.

## 검증 경계와 근거

파서와 별도 후크 프로세스 검사는 실제 CLI 이벤트를 발생시킨 검증과 구분한다. 시작 전용
`--init-only` 검사는 네이티브 `SessionStart` 연결을 확인하며 도구 실행·모델 응답·자식 작업을
검증하지 않는다. 해당 사건은 실제 Claude 작업에서 발생할 때 확인해야 한다.

구현은 설치된 Claude Code 2.1.296과 다음 공식 자료를 기준으로 한다. 문서에만 있는 선택적
필드는 설치된 버전에서 제공되지 않으면 생략한다.

- [후크 공통 입력](https://code.claude.com/docs/en/hooks#common-input-fields)
- [비동기 후크](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
- [후크 관리](https://code.claude.com/docs/en/hooks-guide#manage-your-hooks)
- [CLI 옵션](https://code.claude.com/docs/en/cli-reference)
