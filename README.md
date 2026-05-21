# personal-ops

개인 자동화 도구 엄브렐라. 크론 기반 브리핑/요약/워밍업 스크립트와 출력물을 한곳에서 관리.

## 모듈

| 모듈 | 목적 | 실행 방식 |
|------|------|-----------|
| [`cli-init/`](./cli-init/) | Claude/Codex CLI 워밍업 (5시간 세션 창 트리거) | crontab (매일 06/11/16시) |
| [`session-summary/`](./session-summary/) | Claude 세션 요약 자동화 (Claude Code + opencode) | crontab (매일 00:10, 수요일 00:05 로테이트) |
| [`email-briefing/`](./email-briefing/) | 이메일 브리핑 + 회의 준비 자료 | 수동 실행 |

## 크론 엔트리 (현재 등록)

```cron
TZ=Asia/Seoul

# CLI 워밍업 (5시간 창 3개)
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
0 16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh

# 세션 요약 (매일 요약, 수 로테이트)
10 0 * * *   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh
05 0 * * 3   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh rotate
```

## 구조 규칙

- 모듈마다 `README.md` 필수
- 로그/출력물은 각 모듈의 `logs/` 또는 `output/` 하위 → `.gitignore`로 제외
- 아카이브(영구 보존 대상)만 git 추적 — 예: `session-summary/archive/`

## 새 모듈 추가

1. `<모듈명>/` 디렉터리 생성
2. 스크립트 + `README.md` 작성
3. 로그는 `<모듈명>/logs/`로 (gitignore 자동 적용)
4. 루트 README의 모듈 표/크론 섹션 갱신
