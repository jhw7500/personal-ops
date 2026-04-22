#!/bin/bash
# email-briefing.sh - 이메일 브리핑 + 회의 준비 자료 자동 생성 (수동 실행)
# Managed by personal-ops (projects/personal-ops/email-briefing/)
# 수동 실행: bash /home/jhw/ai/opencode/projects/personal-ops/email-briefing/email-briefing.sh
#
# 2단계 실행:
#   Step 1: 이메일 수집 → 브리핑 생성 (output/briefing.md)
#   Step 2: 브리핑 기반 → 회의 준비 자료 생성 (output/meetings/<회의명>/prep.md + 첨부파일)

set -euo pipefail

BRIEFING_DIR="/home/jhw/ai/opencode/projects/personal-ops/email-briefing/output"
MEETINGS_DIR="${BRIEFING_DIR}/meetings"
DOWNLOADS_DIR="${BRIEFING_DIR}/downloads"
BRIEFING_FILE="${BRIEFING_DIR}/briefing.md"
DATE=$(date +%Y-%m-%d)
LOGFILE="${BRIEFING_DIR}/briefing.log"

export PATH="/home/jhw/.local/bin:/home/jhw/.nvm/versions/node/$(ls /home/jhw/.nvm/versions/node/ 2>/dev/null | tail -1)/bin:$PATH"
export HOME="/home/jhw"

mkdir -p "$MEETINGS_DIR" "$DOWNLOADS_DIR"

# 현재 브리핑 내용 읽기
PREV_CONTENT=""
if [[ -f "$BRIEFING_FILE" ]]; then
    PREV_CONTENT=$(cat "$BRIEFING_FILE")
fi

echo "[$DATE] ===== 브리핑 시작: $(date) =====" >> "$LOGFILE"

# ──────────────────────────────────────────
# Step 1: 이메일 수집 & 브리핑 생성
# ──────────────────────────────────────────

if [[ -n "$PREV_CONTENT" ]]; then
    PREV_SECTION="
아래는 현재까지 누적된 브리핑입니다. 이것을 기반으로 업데이트하세요:
---
${PREV_CONTENT}
---"
else
    PREV_SECTION="이전 브리핑 없음 (첫 브리핑)"
fi

STEP1_PROMPT=$(cat <<PROMPT_END
당신은 이메일 비서입니다. 오늘: ${DATE}

## 작업
1. fetch_recent_emails로 최근 10개 이메일 전문을 가져오세요.
2. list_emails로 최근 50개 목록을 조회하세요.
3. search_emails로 회의/미팅/영상회의/meeting 키워드를 검색하세요.

## 이전 브리핑
${PREV_SECTION}

## 업데이트 규칙
- 완료/만료 항목 삭제 (날짜 지난 회의, 처리 완료 업무)
- 변경사항 반영 (일정 변경, 후속 답변 등)
- 신규 항목 추가, [NEW] 태그
- 진행 중 안건은 경과 누적 (예: "[03/31] 요청 → [04/01] 답변")
- 아직 유효한 기존 항목은 반드시 유지

## 출력 형식 (전체 완성본을 출력하세요)

# 이메일 브리핑
> 마지막 업데이트: ${DATE}

## 1. 긴급/중요
- 즉시 확인 필요 메일. [NEW]/[UPDATE] 태그.

## 2. 회의/일정
각 회의: 일시, 장소, 참석자, D-day, 안건 요약, 첨부파일 목록(파일명+email_id)

## 3. 업무 요청 (진행 중)
- 진행 중만 유지, 완료 삭제, 경과 추가

## 4. 공지/참고
- 유효한 공지만

## 5. 액션 아이템 체크리스트
- [ ] 미완료 (이월)
- [ ] 신규 [NEW]

각 메일: 발신자, 날짜, 핵심 2-3줄. 메일 ID도 표기.
PROMPT_END
)

echo "[$DATE] Step 1 시작: 이메일 브리핑 생성" >> "$LOGFILE"

# 백업
if [[ -f "$BRIEFING_FILE" ]]; then
    cp "$BRIEFING_FILE" "${BRIEFING_DIR}/briefing.prev.md"
fi

claude --print \
    --dangerously-skip-permissions \
    --allowedTools "mcp__cts-email__list_emails,mcp__cts-email__read_email,mcp__cts-email__fetch_recent_emails,mcp__cts-email__fetch_email_thread,mcp__cts-email__search_emails,mcp__cts-email__read_attachment_text,mcp__cts-email__read_document" \
    --model sonnet \
    --output-format text \
    "$STEP1_PROMPT" \
    > "$BRIEFING_FILE" 2>> "$LOGFILE"

echo "[$DATE] Step 1 완료: $(date)" >> "$LOGFILE"

# ──────────────────────────────────────────
# Step 2: 회의 준비 자료 생성
# ──────────────────────────────────────────

BRIEFING_CONTENT=$(cat "$BRIEFING_FILE")

STEP2_PROMPT=$(cat <<PROMPT_END
당신은 회의 준비 어시스턴트입니다. 오늘: ${DATE}
회의 자료 디렉토리: ${MEETINGS_DIR}

## 브리핑 내용 (Step 1에서 생성됨)
---
${BRIEFING_CONTENT}
---

## 작업: 각 회의별 준비 자료 생성
위 브리핑에서 아직 지나지 않은 회의를 모두 찾아, 각 회의마다 다음을 수행하세요.
회의가 없으면 "준비할 회의가 없습니다"만 출력하세요.

### 1단계: 자료 수집
1. fetch_email_thread로 회의 관련 전체 메일 스레드를 복원하세요.
2. 스레드의 각 메일에 대해 반드시 read_email로 메일 본문과 첨부파일 목록을 확인하세요.
3. 첨부파일이 1개라도 있으면 반드시 다음을 수행하세요:
   a. Bash로 ls ${DOWNLOADS_DIR}/ 를 실행하여 이미 다운로드된 파일 목록을 확인하세요.
   b. 같은 파일명이 이미 존재하면 다운로드를 건너뛰세요.
   c. 새 첨부파일은 반드시 download_attachment로 ${DOWNLOADS_DIR}/ 에 저장하세요.
   d. 압축파일(.zip, .tar.gz, .7z, .rar)이면 Bash로 ${DOWNLOADS_DIR}/ 에 압축 해제하세요. (unzip -o -d, tar xzf 등)
   e. 문서 파일(.pdf, .doc, .docx, .xls, .xlsx, .ppt, .pptx, .hwp, .hwpx, .txt, .csv, .md, .html)은 read_document로 내용 분석하세요.
   f. 바이너리/이미지/실행파일 등 비문서 파일은 파일명만 기록하고 읽지 마세요.
3. brave_web_search로 회의 주제, 고객사, 관련 기술 검색
4. tavily_research로 심층 리서치 (기술 동향, 시장 정보)

### 2단계: prep.md 작성
${MEETINGS_DIR}/<회의명>/prep.md 파일을 Write 도구로 생성하세요.
기존 prep.md가 있으면 Read로 읽고 새 정보를 추가 보강하세요.

prep.md 내용:

# <회의명> 준비 자료
> 회의일시: YYYY-MM-DD HH:MM | 장소: ... | D-N
> 마지막 업데이트: ${DATE}

## 1. 회의 배경 및 목적
프로젝트 경위, 이전 회의, 이번 목적

## 2. 참석자
우리 측 / 상대 측 참석자 및 역할

## 3. 안건별 상세 분석
### 안건 1: ...
현황, 데이터, 첨부파일 분석 결과, 우리 측 입장

### 안건 2: ...
(반복)

## 4. 첨부파일 분석 요약
| 파일명 | 유형 | 핵심 내용 | 저장 경로 |
|--------|------|----------|-----------|

## 5. 배경 조사 (인터넷 검색)
관련 기술 동향, 고객사 정보, 시장 상황

## 6. 우리 측 응답 포인트
1. ...

## 7. 예상 질문 & 답변
**Q1. ...**
> A: ...

## 8. 추가 필요 자료
- [ ] 자료명 — 목적 — 요청 대상
- [x] 확보 완료된 자료

완료 후 stdout에 처리 결과 요약을 출력하세요.
PROMPT_END
)

echo "[$DATE] Step 2 시작: 회의 준비 자료 생성" >> "$LOGFILE"

claude --print \
    --dangerously-skip-permissions \
    --allowedTools "Bash,Read,Write,Glob,mcp__cts-email__read_email,mcp__cts-email__fetch_email_thread,mcp__cts-email__search_emails,mcp__cts-email__download_attachment,mcp__cts-email__read_attachment_text,mcp__cts-email__read_document,mcp__brave-search__brave_web_search,mcp__brave-search__brave_local_search,mcp__tavily-mcp__tavily_search,mcp__tavily-mcp__tavily_research" \
    --model sonnet \
    --output-format text \
    "$STEP2_PROMPT" \
    >> "$LOGFILE" 2>&1

echo "[$DATE] Step 2 완료: $(date)" >> "$LOGFILE"
echo "[$DATE] ===== 전체 완료 =====" >> "$LOGFILE"
