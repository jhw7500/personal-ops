# claude-token-sync

Claude OAuth accessToken을 GitHub 레포 시크릿(`CLAUDE_CODE_OAUTH_TOKEN`)에 자동 동기화하는 systemd 모듈.

Claude Code가 토큰을 갱신(만료 ~4.5분 전 자동)하면, 그 새 토큰을 등록된 GitHub 레포들에
일괄로 밀어넣어 GitHub Actions가 항상 유효한 토큰을 쓰도록 유지한다.

## 구성

| 경로 | 역할 |
|---|---|
| `bin/claude-token-sync.sh` | **데몬**. `~/.claude/.credentials.json`을 30초 주기로 폴링하고 토큰 변경 시 최신 repo 목록을 다시 읽어 동기화 |
| `bin/claude-token-sync-health.sh` | **헬스체크 백스톱**. 10분 주기로 ①데몬 생존 확인 ②토큰 또는 repo 목록이 마지막 성공 상태와 다르면 강제 동기화 |
| `bin/claude-token-sync-common.sh` | 공용 `flock`, repo 검증, GitHub secret 갱신, 성공 marker 기록을 daemon/health가 공유 |
| `systemd/claude-token-sync.service` | 데몬 user 서비스 (`Restart=on-failure`) |
| `systemd/claude-token-sync-health.service` | 헬스체크 oneshot 서비스 |
| `systemd/claude-token-sync-health.timer` | 헬스체크 10분 타이머 |
| `config/repos.txt` | **대상 레포 단일 소스**. 데몬·헬스체크가 함께 읽음 |
| `install.sh` / `uninstall.sh` | 심링크 기반 설치/제거 |

### 설치 후 심링크 (정본 → 사용자 환경)
```
~/.local/bin/claude-token-sync.sh         -> bin/claude-token-sync.sh
~/.local/bin/claude-token-sync-health.sh  -> bin/claude-token-sync-health.sh
~/.claude/.token_sync_repos               -> config/repos.txt
~/.config/systemd/user/claude-token-sync.service        -> systemd/...
~/.config/systemd/user/claude-token-sync-health.service -> systemd/...
~/.config/systemd/user/claude-token-sync-health.timer   -> systemd/...
```

### 런타임 파일 (git 비추적, `~/.claude/`)
- `token_sync.log` — 동기화 로그
- `.token_sync_health.sha` — 마지막으로 전부 성공한 토큰+repo 목록 상태의 sha256 마커
- `.token_sync.lock` — daemon과 health의 동기화를 직렬화하는 공용 lock
- `.credentials.json` — Claude 토큰 원본 (Claude Code 관리)

## 설치 / 제거

```bash
./install.sh      # 심링크 + daemon-reload + enable --now + 데몬 재시작
./uninstall.sh    # 심링크/유닛 제거 (런타임·credentials 보존)
```

## 대상 레포 추가/삭제

`config/repos.txt` 한 곳만 편집하면 데몬·헬스체크 모두 반영된다 (한 줄에 레포 하나, `#` 주석 가능).
데몬은 다음 토큰 변경 시 최신 목록을 다시 읽고, 토큰이 그대로여도 헬스체크가
목록 상태 변화를 감지해 다음 주기(최대 10분)에 동기화한다. 파일이 없거나 비었거나
잘못된 repo 이름이 있으면 오래된 내장 목록으로 진행하지 않고 실패한다.
즉시 반영하려면 `systemctl --user restart claude-token-sync` 또는 health 스크립트를
수동 실행한다. daemon은 시작할 때도 현재 token+repo 상태를 marker와 대조한다.

## 운영

```bash
systemctl --user status claude-token-sync                 # 데몬 상태
systemctl --user list-timers claude-token-sync-health.timer  # 다음 헬스체크 시각
tail -f ~/.claude/token_sync.log                          # 로그
~/.local/bin/claude-token-sync-health.sh                  # 헬스체크 수동 1회
```

## 인증

`gh` CLI의 `hosts.yml` 토큰(`repo`, `workflow` scope)을 사용한다 (`GITHUB_TOKEN` 환경변수는 unset).
`gh auth status`로 scope 확인 가능.

## 이중화 설계 메모

- **데몬(token 폴링 30초)** = 빠른 경로. credentials JSON의 access token을 30초마다
  읽고 이전 값과 비교해 변경 시 동기화. 초기 설계는 `inotifywait`였으나 Claude Code의
  credentials 갱신이 inotify 이벤트로 안정적으로 도착하지 않음을 측정(2026-05:
  `--include` 필터 + `create/close_write/moved_to/modify` 다 걸어도 0건 캡처,
  그러나 주기적 파일 읽기는 변경을 포착). 그래서 단순 폴링으로 전환.
- **헬스체크(타이머 10분)** = 프로세스 생존 + 토큰 sha 비교 백스톱. 데몬이 죽거나
  sync에 부분 실패한 경우를 잡는다. marker에는 repo 목록도 포함되므로 대상 추가·삭제도
  감지한다. 부분 실패 시 non-zero로 종료하고 marker를 갱신하지 않아 다음 타이머에서
  재시도한다. daemon과 health는 공용 `flock`을 획득한 뒤 token과 repo 목록을 다시
  읽으므로 서로 다른 상태를 동시에 쓰지 않는다. (systemd `WatchdogSec`는 프로세스가 살아 ping을 보내면 이 케이스를 못
  잡으므로 타이머 방식 채택.)

로그에는 OAuth token 원문이나 prefix를 기록하지 않고 SHA-256 기반 12자 `token_id`만
남긴다.
