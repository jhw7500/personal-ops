# claude-token-sync

CI 전용 장기 OAuth 토큰을 GitHub 저장소 Secret `CLAUDE_CODE_OAUTH_TOKEN`에 동기화한다.
원본은 `~/.claude/.ci-oauth-token.json` 하나이며, Claude Code의 대화용
`~/.claude/.credentials.json`은 읽거나 수정하지 않는다.

`claude setup-token`은 Claude 구독용 CI 토큰을 발급하며 기본 유효기간은 1년이다.
브라우저 인증이 필요하고 토큰은 터미널에 한 번 표시된다.
[공식 문서](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)

## 발급 및 전환

1. 사용자 터미널에서 `claude setup-token`을 실행하고 브라우저 인증을 완료한다.
2. 출력된 유효기간보다 이른 로컬 교체 기한을 정한다. 기본 1년 토큰은 발급 시각에서
   364일 뒤를 사용하면 하루의 여유가 있다. 실제 출력이 더 짧다면 그에 맞춰 앞당긴다.
3. 토큰은 명령 인자/셸 기록에 넣지 않고 숨김 입력으로 가져온다.

```bash
read -rsp 'CI token: ' ci_token; echo
printf '%s' "$ci_token" | ./bin/claude-token-sync-set-token.sh \
  --expires-at "$(date -u -d '+364 days' +%FT%TZ)"
unset ci_token
./install.sh
```

`set-token`은 stdin을 검증하고 공용 lock 안에서 mode `0600` 파일로 원자 교체한다.
만료되었거나 잘못된 입력은 기존 파일을 보존한다. 토큰은 출력하지 않는다.
`install.sh`는 CI 파일을 먼저 검증하므로 발급 전에 실행해도 기존 설치를 바꾸지 않는다.

전환 후 CI 파일이 없어지거나 잘못되어도 대화용 단기 토큰으로 돌아가지 않는다.
새로 발급한 토큰도 같은 명령으로 교체하면 다음 10분 타이머 주기에 배포된다.
즉시 배포하려면 `~/.local/bin/claude-token-sync-health.sh`를 실행한다.
`setup-token` 결과의 실제 수명이나 폐기 여부를 로컬 검사로 확인할 수는 없다.
JSON의 `expiresAt`은 운영자가 정한 보수적인 교체 기한이다.

## 동작

- 상시 데몬은 기본 비활성이다. 설치·재설치 시에도 중지하고 자동 시작을 해제한다.
- 10분 타이머가 헬스체크를 한 번씩 실행해 CI 토큰과 저장소 목록을 확인한다.
  헬스체크는 데몬을 다시 켜지 않는다. 설치 직후에도 한 번 실행한다.
- 마지막 전체 성공 상태와 같으면 GitHub API를 호출하지 않는다.
- 일부 저장소 갱신이 실패하면 성공 marker를 갱신하지 않고 다음 주기에 재시도한다.
- 파일 누락, 파싱 오류, 잘못된 형식, 다른 소유자, symlink, `0600` 이외 권한,
  교체 기한 경과는 실패다. 헬스체크도 non-zero로 종료한다.
- 헬스체크는 교체 기한 14일 전부터 `token_sync.log`에 경고한다.
  자동 재발급은 하지 않으며 브라우저 인증으로 새 토큰을 발급해야 한다.
- OAuth 원문 대신 SHA-256 기반 12자 `token_id`만 로그에 기록한다.

```text
claude setup-token
    |
~/.claude/.ci-oauth-token.json
    |
10-minute timer -> claude-token-sync-health.sh
    +-- repository secret A
    +-- repository secret B
    +-- repository secret ...
```

## 구성 및 런타임

`bin/claude-token-sync-common.sh`가 원본 검증, 공용 lock, GitHub 배포, 성공 marker를
공유한다. `config/repos.txt`는 `jhw7500/*` 대상 저장소의 단일 소스다.
목록 변경도 다음 타이머 주기 또는 수동 헬스체크에서 반영된다. 파일이 없거나 비어 있거나
잘못된 이름이 있으면 실패한다.

사용자 환경의 스크립트와 systemd 유닛은 모듈 정본에 대한 symlink로 설치된다.
런타임 파일은 `~/.claude/`에 있고 Git에 포함하지 않는다.

- `.ci-oauth-token.json`: CI 전용 원본 (`source`, `accessToken`, `expiresAt`)
- `.token_sync_repos`: `config/repos.txt` symlink
- `.token_sync_health.sha`: 마지막 전체 성공 상태의 token+repos hash
- `.token_sync.lock`: importer/daemon/health 공유 lock
- `token_sync.log`: 성공·실패·교체 기한 경고

```bash
systemctl --user is-active claude-token-sync.service  # inactive
systemctl --user list-timers claude-token-sync-health.timer
~/.local/bin/claude-token-sync-health.sh
bash tests/test-token-sync.sh
./uninstall.sh
```

제거 시 유닛과 symlink만 정리하고 CI 토큰, CLI 로그인, 로그와 marker는 보존한다.
GitHub 인증에는 `gh`의 로그인 토큰을 사용한다 (`GITHUB_TOKEN`은 unset).
