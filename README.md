# personal-ops

개인 자동화 도구 엄브렐라. 크론 기반 브리핑/요약/워밍업 스크립트와 출력물을 한곳에서 관리.

## 모듈

| 모듈 | 목적 | 실행 방식 |
|------|------|-----------|
| [`cli-init/`](./cli-init/) | Claude 인증 상태 확인 + Codex CLI 워밍업 | crontab (매일 06/11시) |
| [`session-summary/`](./session-summary/) | Claude 세션 요약 자동화 (Claude Code + opencode) | crontab (매일 09:00, 수요일 09:10 로테이트) |
| [`email-briefing/`](./email-briefing/) | 이메일 브리핑 + 회의 준비 자료 | 수동 실행 |
| [`mirror-sync/`](./mirror-sync/) | github → gitlab 큐레이션 미러 싱크 (4분류 + 어서션, push 제외) | 수동 실행 |

## 크론 엔트리 (현재 등록)

```cron
TZ=Asia/Seoul

# CLI 상태 확인 / 워밍업
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
#0 16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/claude-init.sh
0 6  * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
0 11 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh
#0 16 * * * /home/jhw/ai/opencode/projects/personal-ops/cli-init/codex-init.sh

# 세션 요약 (매일 요약, 수 로테이트)
0 9 * * *   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh
10 9 * * 3  /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh rotate
```

Claude 관련 예약 작업의 모델 호출 상한은 다음과 같다.

| 작업 | 실행당 모델 호출 | 하루 최대(현재 cron) | 보호 장치 |
|---|---:|---:|---|
| `claude-init.sh` | 0회 | 0회 | 비생성 auth status, flock, 20초 timeout + 5초 후 강제 종료 |
| `session-summary.sh` | 최대 1회 | 최대 1회 | Haiku/low, 64 KiB 입력, 180초 timeout + 5초 후 강제 종료, quota/auth backoff |

`session-summary.sh rotate`는 파일 이동만 수행하며 모델을 호출하지 않는다.

## 구조 규칙

- 모듈마다 `README.md` 필수
- 로그/출력물은 각 모듈의 `logs/` 또는 `output/` 하위 → `.gitignore`로 제외
- 아카이브(영구 보존 대상)만 git 추적 — 예: `session-summary/archive/`

## 새 모듈 추가

1. `<모듈명>/` 디렉터리 생성
2. 스크립트 + `README.md` 작성
3. 로그는 `<모듈명>/logs/`로 (gitignore 자동 적용)
4. 루트 README의 모듈 표/크론 섹션 갱신
