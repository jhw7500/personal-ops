#!/bin/bash
# session-summary.sh - 평일 아침 Claude 세션 요약 자동화
# Managed by personal-ops (projects/personal-ops/session-summary/)
#
# crontab:
#   10 0 * * 1-5 /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh
#   05 0 * * 3   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh rotate
#
# 동작:
#   - 기본: episodic-memory로 전일 세션 검색 → 요약을 logs/session-summary.md에 누적 추가
#   - rotate: logs/session-summary.md를 archive/로 이동 후 새 파일 시작

set -euo pipefail

MODULE_DIR="/home/jhw/ai/opencode/projects/personal-ops/session-summary"
SUMMARY_FILE="${MODULE_DIR}/logs/session-summary.md"
LOGFILE="${MODULE_DIR}/logs/summary.log"
ARCHIVE_DIR="${MODULE_DIR}/archive"
DATE=$(date +%Y-%m-%d)
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)
DOW=$(date +%u)  # 1=Mon, 7=Sun

export PATH="/home/jhw/.nvm/versions/node/$(ls /home/jhw/.nvm/versions/node/ 2>/dev/null | tail -1)/bin:/home/jhw/.local/bin:/usr/bin:/bin"
export HOME="/home/jhw"

mkdir -p "$ARCHIVE_DIR" "$(dirname "$LOGFILE")"

# --- 로테이트 모드 ---
if [[ "${1:-}" == "rotate" ]]; then
    echo "[$DATE] ===== 로테이트 시작: $(date) =====" >> "$LOGFILE"
    if [[ -s "$SUMMARY_FILE" ]]; then
        # 수요일 00:05 로테이트 기준: 지난 수요일(7일 전) ~ 어제(화요일)
        FIRST_DATE=$(date -d "7 days ago" +%Y-%m-%d)
        LAST_DATE=$(date -d "yesterday" +%Y-%m-%d)
        ARCHIVE_NAME="summary-${FIRST_DATE}_${LAST_DATE}.md"
        mv "$SUMMARY_FILE" "${ARCHIVE_DIR}/${ARCHIVE_NAME}"
        echo "[$DATE] 아카이브 완료: ${ARCHIVE_NAME}" >> "$LOGFILE"
    else
        echo "[$DATE] 로테이트 대상 없음 (파일 비어있거나 없음)" >> "$LOGFILE"
    fi
    echo "[$DATE] ===== 로테이트 완료 =====" >> "$LOGFILE"
    exit 0
fi

# --- 요약 모드 ---

# 월요일이면 금요일부터 검색 (주말 포함)
if [[ "$DOW" == "1" ]]; then
    SEARCH_FROM=$(date -d "3 days ago" +%Y-%m-%d)
else
    SEARCH_FROM="$YESTERDAY"
fi

echo "[$DATE] ===== 세션 요약 시작: $(date) =====" >> "$LOGFILE"

PROMPT=$(cat <<PROMPT_END
당신은 업무 세션 정리 비서입니다. 오늘: ${DATE}, 검색 시작일: ${SEARCH_FROM}

## 작업

### 1단계: 세션 검색
episodic-memory search 도구로 다음을 검색하세요:
- "session" 키워드로 ${SEARCH_FROM} 이후 세션 검색
- "project" 키워드로 프로젝트 관련 세션 검색
- "implement", "fix", "design" 등 작업 키워드 검색

검색된 세션 ID가 있으면 episodic-memory read로 상세 내용을 읽으세요.

### 2단계: 요약 작성
검색된 세션 정보를 아래 형식으로 정리하세요.
세션이 없으면 아무 출력도 하지 마세요 (빈 출력).

중요: 구분선(---)부터 바로 시작하세요. 다른 설명, 인사, 상태 보고 없이 형식만 출력하세요.

## 출력 형식

---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

### 작업 내역
- **<프로젝트명>**: 작업 내용 1줄 요약
  - 주요 결정: 있으면 기록
  - 결과물: 생성/수정된 파일, PR, 커밋 등

(프로젝트별 반복)

### 미완료 항목
- [ ] 항목 — 프로젝트 — 우선순위(높/중/낮)

### 기술 메모
세션 중 발견된 중요한 기술 사항 (없으면 생략).
PROMPT_END
)

echo "[$DATE] Claude 실행 시작" >> "$LOGFILE"

# 임시 파일에 당일 요약 생성
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

claude --print \
    --dangerously-skip-permissions \
    --allowedTools "mcp__plugin_episodic-memory_episodic-memory__search,mcp__plugin_episodic-memory_episodic-memory__read" \
    --model sonnet \
    --output-format text \
    "$PROMPT" \
    > "$TMPFILE" 2>> "$LOGFILE"

# 빈 결과면 스킵
if [[ ! -s "$TMPFILE" ]] || ! grep -q '[^[:space:]]' "$TMPFILE"; then
    echo "[$DATE] 검색된 세션 없음 — 스킵" >> "$LOGFILE"
    echo "[$DATE] ===== 완료 =====" >> "$LOGFILE"
    exit 0
fi

# 헤더가 없으면 생성
if [[ ! -f "$SUMMARY_FILE" ]]; then
    cat > "$SUMMARY_FILE" <<EOF
# Claude 세션 요약

> 자동 생성 파일. 매주 수요일 00시 로테이트.
EOF
fi

# 당일 요약 누적 추가
echo "" >> "$SUMMARY_FILE"
cat "$TMPFILE" >> "$SUMMARY_FILE"

echo "[$DATE] 요약 추가 완료: $(date)" >> "$LOGFILE"
echo "[$DATE] ===== 전체 완료 =====" >> "$LOGFILE"
