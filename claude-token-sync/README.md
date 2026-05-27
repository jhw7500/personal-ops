# claude-token-sync

Claude OAuth accessToken을 GitHub 레포 시크릿(`CLAUDE_CODE_OAUTH_TOKEN`)에 자동 동기화하는 systemd 모듈.

Claude Code가 토큰을 갱신(만료 ~4.5분 전 자동)하면, 그 새 토큰을 등록된 GitHub 레포들에
일괄로 밀어넣어 GitHub Actions가 항상 유효한 토큰을 쓰도록 유지한다.

## 구성

| 경로 | 역할 |
|---|---|
| `bin/claude-token-sync.sh` | **데몬**. `~/.claude/.credentials.json`을 30초 주기로 stat 폴링, 토큰 변경 시 즉시 동기화 (빠른 경로) |
| `bin/claude-token-sync-health.sh` | **헬스체크 백스톱**. 10분 주기로 ①데몬 생존 확인→죽었으면 재시작 ②토큰이 마지막 동기화분과 다르면 강제 동기화 |
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
- `.token_sync_health.sha` — 헬스체크가 마지막으로 동기화한 토큰의 sha256 마커
- `.credentials.json` — Claude 토큰 원본 (Claude Code 관리)

## 설치 / 제거

```bash
./install.sh      # 심링크 + daemon-reload + enable --now + 데몬 재시작
./uninstall.sh    # 심링크/유닛 제거 (런타임·credentials 보존)
```

## 대상 레포 추가/삭제

`config/repos.txt` 한 곳만 편집하면 데몬·헬스체크 모두 반영된다 (한 줄에 레포 하나, `#` 주석 가능).
데몬은 다음 토큰 변경 시, 헬스체크는 다음 주기(최대 10분)에 새 목록을 사용한다.
즉시 반영하려면 `systemctl --user restart claude-token-sync`.

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

- **데몬(stat 폴링 30초)** = 빠른 경로. credentials를 30초마다 `stat`로 inode/mtime을
  비교해 토큰 변경 시 즉시 동기화. 초기 설계는 `inotifywait`였으나 Claude Code의
  credentials 갱신이 inotify 이벤트로 안정적으로 도착하지 않음을 측정(2026-05:
  `--include` 필터 + `create/close_write/moved_to/modify` 다 걸어도 0건 캡처,
  그러나 stat는 inode 변화 100% 포착). 그래서 폴링으로 전환.
- **헬스체크(타이머 10분)** = 프로세스 생존 + 토큰 sha 비교 백스톱. 데몬이 죽거나
  sync에 부분 실패한 경우를 잡는다. (systemd `WatchdogSec`는 프로세스가 살아 ping을
  보내면 이 케이스를 못 잡으므로 타이머 방식 채택.)
