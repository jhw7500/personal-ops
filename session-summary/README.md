# session-summary

Claude Code episodic-memory + opencode SQLite에서 전일 세션을 긁어와 주간 요약을 누적하는 스크립트.

## 동작

- **요약 모드** (인자 없음): 어제 1일치 세션 검색 → 요약을 `logs/session-summary.md`에 append
  - 빈 결과(작업 없음)도 헤더만 기록하여 날짜 누락 방지 (`_(작업 없음)_`)
  - opencode 통합: `INCLUDE_OPENCODE=1` (기본값). DB는 read-only로 직접 읽음 — opencode 바이너리/토큰 미사용. `0`으로 끄면 Claude Code 세션만 사용
- **로테이트 모드** (`rotate` 인자): `logs/session-summary.md`를 `archive/summary-<지난수>_<이번화>.md`로 이동

## AI 호출 예산과 backoff

- 실행당 모델 호출은 최대 1회이며 fallback 모델을 호출하지 않는다.
- 기본 모델/effort는 `haiku` / `low`다.
- 도구와 slash command를 비활성화하고 세션을 저장하지 않는다.
- 전체 프롬프트는 기본 65,536 bytes 이하로 제한한다. 실행은 180초 후 TERM,
  종료되지 않으면 5초 뒤 KILL해 하드 상한을 둔다.
- quota 오류는 6시간, 인증 오류는 24시간 `state/claude-backoff` marker로 재호출을 막는다.
- 실패는 `auth`, `quota`, `timeout`, `input-limit`, `other`로 로그와 요약 파일에 구분한다.
- `flock -n`으로 겹친 요약 실행은 모델을 추가 호출하지 않고 건너뛴다.

운영 기본값은 다음 환경변수로 명시적으로 덮어쓸 수 있다.

| 환경변수 | 기본값 |
|---|---:|
| `SESSION_SUMMARY_MODEL` | `haiku` |
| `SESSION_SUMMARY_EFFORT` | `low` |
| `SESSION_SUMMARY_MAX_INPUT_BYTES` | `65536` |
| `SESSION_SUMMARY_TIMEOUT_SECONDS` | `180` |
| `SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS` | `5` |
| `SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS` | `21600` |
| `SESSION_SUMMARY_AUTH_BACKOFF_SECONDS` | `86400` |

## 크론

```cron
0 9 * * *    .../session-summary.sh           # 매일 09:00 요약 (어제 1일치)
10 9 * * 3   .../session-summary.sh rotate    # 수요일 09:10 로테이트
```

## 파일 레이아웃

```
session-summary/
├── session-summary.sh
├── logs/                          # gitignored
│   ├── session-summary.md         # 현재 주 누적 요약 (작업본)
│   └── summary.log                # 실행 로그
├── state/                         # gitignored
│   └── claude-backoff             # quota/auth 다음 호출 허용 시각
└── archive/                       # git 추적
    ├── summary-YYYY-MM-DD_YYYY-MM-DD.md  (주간 아카이브)
    └── .trash/                    # gitignored
```

## 아카이브 파일명 규칙

`summary-<FIRST>_<LAST>.md` — `FIRST`는 `date -d "7 days ago"` (지난 수요일), `LAST`는 `date -d "yesterday"` (이번주 화요일). 수요일 09:10 로테이트 기준이라 항상 7일 범위가 보장된다.

(예전 버전은 파일 내용의 grep으로 날짜를 추출해 1일짜리 파일명이 생기는 문제가 있었음 — 2026-04-22 개선)
