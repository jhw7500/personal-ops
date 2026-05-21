# session-summary

Claude Code episodic-memory + opencode SQLite에서 전일 세션을 긁어와 주간 요약을 누적하는 스크립트.

## 동작

- **요약 모드** (인자 없음): 어제 1일치 세션 검색 → 요약을 `logs/session-summary.md`에 append
  - 빈 결과(작업 없음)도 헤더만 기록하여 날짜 누락 방지 (`_(작업 없음)_`)
  - opencode 통합: `INCLUDE_OPENCODE=1` (기본값). DB는 read-only로 직접 읽음 — opencode 바이너리/토큰 미사용. `0`으로 끄면 Claude Code 세션만 사용
- **로테이트 모드** (`rotate` 인자): `logs/session-summary.md`를 `archive/summary-<지난수>_<이번화>.md`로 이동

## 크론

```cron
10 0 * * *   .../session-summary.sh           # 매일 00:10 요약 (어제 1일치)
05 0 * * 3   .../session-summary.sh rotate    # 수요일 00:05 로테이트
```

## 파일 레이아웃

```
session-summary/
├── session-summary.sh
├── logs/                          # gitignored
│   ├── session-summary.md         # 현재 주 누적 요약 (작업본)
│   └── summary.log                # 실행 로그
└── archive/                       # git 추적
    ├── summary-YYYY-MM-DD_YYYY-MM-DD.md  (주간 아카이브)
    └── .trash/                    # gitignored
```

## 아카이브 파일명 규칙

`summary-<FIRST>_<LAST>.md` — `FIRST`는 `date -d "7 days ago"` (지난 수요일), `LAST`는 `date -d "yesterday"` (이번주 화요일). 수요일 00:05 로테이트 기준이라 항상 7일 범위가 보장된다.

(예전 버전은 파일 내용의 grep으로 날짜를 추출해 1일짜리 파일명이 생기는 문제가 있었음 — 2026-04-22 개선)
