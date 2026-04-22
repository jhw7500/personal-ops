# email-briefing

HiWorks 이메일을 읽어 브리핑을 누적 업데이트하고, 예정된 회의별 준비 자료(prep.md + 첨부파일)까지 생성하는 수동 실행 스크립트.

## 실행

```bash
bash /home/jhw/ai/opencode/projects/personal-ops/email-briefing/email-briefing.sh
```

(과거 크론 자동 실행은 중단 — 수동 트리거만 사용 중)

## 2단계 동작

1. **Step 1**: `fetch_recent_emails` + `list_emails` + `search_emails` → `output/briefing.md` 갱신 (기존 내용 기반 누적)
2. **Step 2**: 브리핑에서 미래 회의 추출 → 각 회의별 스레드 복원, 첨부 다운로드/압축해제/문서분석, 웹 리서치 → `output/meetings/<회의명>/prep.md`

## 디렉터리 (모두 gitignored — 재생성 가능)

```
email-briefing/
├── email-briefing.sh
└── output/                      # gitignored
    ├── briefing.md              # 누적 브리핑
    ├── briefing.prev.md         # 직전 백업
    ├── briefing.log             # 실행 로그
    ├── meetings/<회의명>/prep.md
    └── downloads/               # 첨부파일 원본
```

## 의존 MCP

- `cts-email` — 메일 조회/첨부 다운로드
- `brave-search`, `tavily-mcp` — 회의 배경 리서치
