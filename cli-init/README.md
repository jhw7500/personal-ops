# cli-init

Claude Code / Codex CLI를 주기적으로 워밍업하여 5시간 세션 창 경계를 원하는 시각(06/11/16)에 맞춘다.

## 스크립트

| 파일 | 설명 |
|------|------|
| `claude-init.sh` | 크론에서 호출 — `claude -p 'OK only?'` 로 세션 시작 |
| `claude-init-precise.sh <HH> [LEAD_MS]` | 정각에 정확히 fire (HH:00:00에서 LEAD_MS 앞당김). 현재 크론 주석처리됨 |
| `claude-calibrate-lead.sh` | `claude` CLI의 첫 API I/O 지연을 측정. 결과 중앙값을 `init-precise.sh`의 LEAD_MS로 사용 |
| `codex-init.sh` | 크론에서 호출 — `codex exec -C /home/jhw/ai/codex` 로 세션 시작 |

## 로그

- `logs/claude-init.log` — `claude-init.sh` + `claude-init-precise.sh` 공유
- `logs/codex-init.log` — `codex-init.sh`

## 크론

```cron
0 6,11,16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 6,11,16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
```

## 참고

- `codex-init.sh`의 `WORKDIR=/home/jhw/ai/codex`는 codex 세션 저장 디렉터리라 이동하지 않음 (스크립트 위치와 독립).
- `/tmp/*_init*.lock`은 락 파일 (OS 수준 경합 방지용).
