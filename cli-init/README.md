# cli-init

Claude CLI 인증 상태를 비생성 방식으로 확인하고, Codex CLI 워밍업을 관리한다.

## 스크립트

| 파일 | 설명 |
|------|------|
| `claude-init.sh` | 크론에서 호출 — `claude auth status --json`으로 인증 상태만 확인(모델 API 호출 0회) |
| `claude-init-precise.sh <HH> [LEAD_MS]` | 정각에 정확히 fire (HH:00:00에서 LEAD_MS 앞당김). 현재 크론 주석처리됨 |
| `claude-calibrate-lead.sh` | `claude` CLI의 첫 API I/O 지연을 측정. 결과 중앙값을 `init-precise.sh`의 LEAD_MS로 사용 |
| `codex-init.sh` | 크론에서 호출 — `codex exec -C /home/jhw/ai/codex` 로 세션 시작 |

## 로그

- `logs/claude-init.log` — `claude-init.sh` + `claude-init-precise.sh` 공유
- `logs/codex-init.log` — `codex-init.sh`

둘 다 `.gitignore`의 `**/logs/`로 git에 추적되지 않는다.

### 보존

각 스크립트는 실행이 끝날 때 로그가 2,000줄을 넘으면 최근 2,000줄만 남긴다. `cat` 리다이렉트로
제자리를 덮어써 inode와 권한을 유지하며, `flock`을 쥔 동안에만 수행해 append와 겹치지 않는다.
상한은 `CLAUDE_INIT_LOG_MAX_LINES` / `CODEX_INIT_LOG_MAX_LINES`로 조정하고, 양의 정수가 아니면
경고를 남기고 2000으로 폴백한다. `claude-init-precise.sh`는 자체 절삭을 하지 않지만 공유 로그에
쓰므로 다음 `claude-init.sh` 실행(하루 2회) 때 같이 절삭된다.

### codex 요약 기록

`codex-init.sh`는 성공 시 codex 세션 출력 전문(배너·hook 로그·토큰 집계로 실행당 수십 줄)을
남기지 않고 `codex exit=<rc> reply="<마지막 응답 줄>"` 한 줄로 요약한다. `codex exec`는 stdout에
에러를 찍고도 exit=0을 반환한 전례가 있어(`Not inside a trusted directory ...`, 2026-03 로그)
exit code만으로는 성공을 판정할 수 없으므로, 마지막 비어있지 않은 응답 줄을 증거로 함께 남긴다.
실패(rc≠0)와 모델 폴백 시에는 진단을 위해 출력 전문을 그대로 남긴다.

## 크론

```cron
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
```

`claude-init.sh`는 실행당 비생성 auth 명령을 최대 1회 실행하고 모델 API는 호출하지 않는다.
하루 2회 크론 기준 모델 호출 가능 횟수도 0회다. `flock -n`으로 겹친 실행은 즉시
건너뛰며, auth 명령은 기본 20초 후 TERM, 무시하면 5초 후 KILL 처리한다. 이전의
Haiku → Sonnet → 기본 모델 fallback은 제거됐다.

## 참고

- `codex-init.sh`의 `WORKDIR=/home/jhw/ai/codex`는 codex 세션 저장 디렉터리라 이동하지 않음 (스크립트 위치와 독립).
- `/tmp/*_init*.lock`은 락 파일 (OS 수준 경합 방지용).
- `claude-init-precise.sh`와 `claude-calibrate-lead.sh`는 현재 크론에서 비활성인 수동 도구이며,
  생성형 호출이 필요할 수 있다.
