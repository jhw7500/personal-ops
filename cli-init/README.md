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
