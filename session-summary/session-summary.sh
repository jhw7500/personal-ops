#!/bin/bash
# session-summary.sh - Claude 세션 요약 자동화 (매일)
# Managed by personal-ops (projects/personal-ops/session-summary/)
#
# crontab:
#   10 0 * * *   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh
#   05 0 * * 3   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh rotate
#
# 동작:
#   - 기본: 어제 1일치 세션을 episodic-memory + opencode SQLite에서 검색 → 요약을 logs/session-summary.md에 누적
#   - 빈 결과: 헤더만 기록 (작업 없음 표시)
#   - rotate: logs/session-summary.md를 archive/로 이동 후 새 파일 시작

set -euo pipefail

MODULE_DIR="/home/jhw/ai/opencode/projects/personal-ops/session-summary"
SUMMARY_FILE="${MODULE_DIR}/logs/session-summary.md"
LOGFILE="${MODULE_DIR}/logs/summary.log"
ARCHIVE_DIR="${MODULE_DIR}/archive"
DATE=$(date +%Y-%m-%d)
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)

# opencode 세션 포함 여부 (1=포함, 0=제외)
# DB 파일을 직접 read-only로 읽으므로 opencode 바이너리/토큰 의존 없음
INCLUDE_OPENCODE="${INCLUDE_OPENCODE:-1}"
OPENCODE_DB="/home/jhw/.local/share/opencode/opencode.db"

# Claude Code 세션 포함 여부 (1=포함, 0=제외)
# ~/.claude/projects/*/*.jsonl을 직접 읽어 시간 기반으로 추출 (opencode와 대칭)
INCLUDE_CC="${INCLUDE_CC:-1}"
CC_PROJECTS_DIR="/home/jhw/.claude/projects"

# git 커밋 컨텍스트 포함 여부 (1=포함, 0=제외)
# 세션에서 발견된 cwd들에서 해당 기간 커밋 추출
INCLUDE_COMMITS="${INCLUDE_COMMITS:-1}"

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
        TARGET="${ARCHIVE_DIR}/${ARCHIVE_NAME}"
        # 동명 아카이브가 이미 있으면 타임스탬프 suffix로 충돌 회피 (데이터 손실 방지)
        if [[ -e "$TARGET" ]]; then
            ARCHIVE_NAME="summary-${FIRST_DATE}_${LAST_DATE}-$(date +%H%M%S).md"
            TARGET="${ARCHIVE_DIR}/${ARCHIVE_NAME}"
            echo "[$DATE] 주의: 동명 아카이브 존재, 새 이름 사용: ${ARCHIVE_NAME}" >> "$LOGFILE"
        fi
        mv "$SUMMARY_FILE" "$TARGET"
        echo "[$DATE] 아카이브 완료: ${ARCHIVE_NAME}" >> "$LOGFILE"
    else
        echo "[$DATE] 로테이트 대상 없음 (파일 비어있거나 없음)" >> "$LOGFILE"
    fi
    echo "[$DATE] ===== 로테이트 완료 =====" >> "$LOGFILE"
    exit 0
fi

# --- 요약 모드 ---

# 매일 어제 1일치만 검색 (cron이 매일 실행)
SEARCH_FROM="$YESTERDAY"

echo "[$DATE] ===== 세션 요약 시작: $(date) =====" >> "$LOGFILE"

# --- opencode 세션 추출 (INCLUDE_OPENCODE=1일 때만) ---
# Python 표준 sqlite3로 DB 파일을 read-only 직접 읽음 (opencode 바이너리 미실행)
OPENCODE_CONTEXT=""
if [[ "$INCLUDE_OPENCODE" == "1" ]] && [[ -r "$OPENCODE_DB" ]]; then
    SEARCH_FROM_MS="$(date -d "$SEARCH_FROM 00:00:00" +%s)000"
    SEARCH_TO_MS="$(date -d "today 00:00:00" +%s)000"

    OPENCODE_RAW=$(python3 - <<PYEOF 2>>"$LOGFILE" || true
import sqlite3
db = sqlite3.connect("file:${OPENCODE_DB}?mode=ro", uri=True)
sql = """
SELECT
    datetime(s.time_updated/1000,'unixepoch','localtime') AS upd,
    s.directory,
    s.title,
    substr(replace(replace(coalesce((
        SELECT json_extract(p.data,'\$.text')
        FROM message m JOIN part p ON p.message_id=m.id
        WHERE m.session_id=s.id
          AND json_extract(p.data,'\$.type')='text'
          AND coalesce(json_extract(p.data,'\$.synthetic'),0)=0
        ORDER BY m.time_created, p.time_created LIMIT 1
    ),''),char(10),' '),char(9),' '),1,300) AS first_msg
FROM session s
WHERE s.time_updated >= ? AND s.time_updated < ?
ORDER BY s.time_updated DESC
"""
rows = db.execute(sql, (${SEARCH_FROM_MS}, ${SEARCH_TO_MS})).fetchall()
print("upd\tdirectory\ttitle\tfirst_msg")
for r in rows:
    print("\t".join((c or "") for c in r))
PYEOF
)

    OPENCODE_LINES=$(printf '%s\n' "$OPENCODE_RAW" | tail -n +2 | grep -c . || true)
    if [[ "$OPENCODE_LINES" -gt 0 ]]; then
        OPENCODE_CONTEXT="$OPENCODE_RAW"
        echo "[$DATE] opencode 세션 추출: ${OPENCODE_LINES}건" >> "$LOGFILE"
    else
        echo "[$DATE] opencode 세션 없음 (기간: $SEARCH_FROM ~ $DATE)" >> "$LOGFILE"
    fi
elif [[ "$INCLUDE_OPENCODE" == "1" ]]; then
    echo "[$DATE] opencode 통합 활성이지만 DB 없음: $OPENCODE_DB" >> "$LOGFILE"
fi

# --- Claude Code 세션 추출 (INCLUDE_CC=1일 때만) ---
# ~/.claude/projects/*/*.jsonl을 시간 기반으로 직접 스캔 (opencode와 대칭)
CC_CONTEXT=""
if [[ "$INCLUDE_CC" == "1" ]] && [[ -d "$CC_PROJECTS_DIR" ]]; then
    CC_RAW=$(python3 - <<PYEOF 2>>"$LOGFILE" || true
import os, json, glob, re
from datetime import datetime, timezone, timedelta

PROJECTS = "${CC_PROJECTS_DIR}"
SEARCH_FROM = "${SEARCH_FROM}"
SEARCH_TO   = "${DATE}"
TZ = timezone(timedelta(hours=9))  # Asia/Seoul

start_local = datetime.fromisoformat(f"{SEARCH_FROM}T00:00:00").replace(tzinfo=TZ)
end_local   = datetime.fromisoformat(f"{SEARCH_TO}T00:00:00").replace(tzinfo=TZ)
start_utc = start_local.astimezone(timezone.utc)
end_utc   = end_local.astimezone(timezone.utc)

# wrapper 노이즈 제거 (반복 적용)
NOISE = re.compile(r'^<(local-command-caveat|command-(?:name|message|args)|system-reminder|user-prompt-submit-hook)[^>]*>.*?</\1>\s*', re.DOTALL)
TAG_OPEN = re.compile(r'^<[a-zA-Z][a-zA-Z0-9_-]*[^>]*>\s*')

sessions = {}  # session_id -> {ts, local, cwd, first_msg}
mtime_floor = start_utc.timestamp() - 86400  # 하루 여유

for jsonl_path in glob.glob(os.path.join(PROJECTS, '*', '*.jsonl')):
    try:
        if os.path.getmtime(jsonl_path) < mtime_floor:
            continue
    except OSError:
        continue
    try:
        f = open(jsonl_path, 'r', encoding='utf-8', errors='replace')
    except OSError:
        continue
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('type') != 'user' or d.get('isMeta'):
                continue
            ts = d.get('timestamp')
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except Exception:
                continue
            if not (start_utc <= t < end_utc):
                continue
            sid = d.get('sessionId', '')
            if not sid:
                continue
            # 같은 세션의 더 늦은 메시지면 skip (가장 이른 user 메시지 = 시드)
            if sid in sessions and sessions[sid]['ts'] <= t:
                continue
            msg = d.get('message') or {}
            content = msg.get('content') if isinstance(msg, dict) else None
            text = ''
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        candidate = item.get('text', '') or ''
                        if candidate:
                            text = candidate
                            break
            # wrapper 스트립
            for _ in range(8):
                m = NOISE.match(text)
                if not m:
                    break
                text = text[m.end():]
            text = text.strip()
            if not text:
                continue
            preview = re.sub(r'\s+', ' ', text)[:300]
            cwd = d.get('cwd', '') or ''
            local_str = t.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')
            sessions[sid] = {'ts': t, 'local': local_str, 'cwd': cwd, 'first_msg': preview}

# 중복 시드 메시지 dedup (자동화 봇이 동일 프롬프트로 반복 실행되는 경우 압축)
# 키: (cwd, first_msg 앞 120자) — 거의 동일한 프롬프트는 1행 + count로 합산
groups = {}
for sid, s in sessions.items():
    key = (s['cwd'], s['first_msg'][:120])
    g = groups.get(key)
    if g is None or s['ts'] < g['ts']:
        groups[key] = {**s, 'sid': sid, 'count': (g['count'] if g else 0) + 1}
    else:
        g['count'] += 1

print("upd\tdirectory\tcount\tsession_id\tfirst_msg")
for g in sorted(groups.values(), key=lambda v: v['ts'], reverse=True):
    row = [g['local'], g['cwd'], f"x{g['count']}", g['sid'][:8], g['first_msg']]
    print('\t'.join(c.replace('\t', ' ').replace('\n', ' ') for c in row))
PYEOF
)

    CC_LINES=$(printf '%s\n' "$CC_RAW" | tail -n +2 | grep -c . || true)
    if [[ "$CC_LINES" -gt 0 ]]; then
        CC_CONTEXT="$CC_RAW"
        echo "[$DATE] Claude Code 세션 추출: ${CC_LINES}건" >> "$LOGFILE"
    else
        echo "[$DATE] Claude Code 세션 없음 (기간: $SEARCH_FROM ~ $DATE)" >> "$LOGFILE"
    fi
elif [[ "$INCLUDE_CC" == "1" ]]; then
    echo "[$DATE] Claude Code 통합 활성이지만 디렉토리 없음: $CC_PROJECTS_DIR" >> "$LOGFILE"
fi

# --- git 커밋 컨텍스트 (INCLUDE_COMMITS=1일 때만) ---
# 두 세션 컨텍스트에서 발견된 distinct cwd들에 한해 커밋 조회 (절대 경로만)
COMMIT_CONTEXT=""
if [[ "$INCLUDE_COMMITS" == "1" ]]; then
    DIRS=$( { printf '%s\n' "$OPENCODE_CONTEXT"; printf '%s\n' "$CC_CONTEXT"; } \
        | awk -F'\t' '$2 ~ /^\// {print $2}' \
        | sort -u )
    COMMIT_BLOCKS=""
    if [[ -n "$DIRS" ]]; then
        while IFS= read -r d; do
            [[ -z "$d" || ! -d "$d/.git" ]] && continue
            OUT=$(git -C "$d" log --all --no-merges \
                --since="$SEARCH_FROM 00:00" \
                --until="$DATE 00:00" \
                --pretty=format:"  %ad %h %s" \
                --date=format:'%H:%M' 2>/dev/null || true)
            if [[ -n "$OUT" ]]; then
                COMMIT_BLOCKS+=$'\n'"### ${d}"$'\n'"$OUT"$'\n'
            fi
        done <<< "$DIRS"
    fi
    if [[ -n "$COMMIT_BLOCKS" ]]; then
        COMMIT_CONTEXT="$COMMIT_BLOCKS"
        COMMIT_LINES=$(printf '%s\n' "$COMMIT_BLOCKS" | grep -c '^  ' || true)
        echo "[$DATE] git 커밋 추출: ${COMMIT_LINES}건" >> "$LOGFILE"
    else
        echo "[$DATE] git 커밋 없음 (기간: $SEARCH_FROM ~ $DATE)" >> "$LOGFILE"
    fi
fi

PROMPT=$(cat <<PROMPT_END
당신은 업무 세션 정리 비서입니다. 오늘: ${DATE}, 검색 시작일: ${SEARCH_FROM}

세 종류의 사실 컨텍스트가 사전 추출되어 아래에 제공됩니다. 추가 도구 호출 없이 이 컨텍스트만으로 종합 요약하세요.

### A. Claude Code 세션 (이미 추출됨)
컬럼: 시작시각 / 작업디렉토리 / count(동일시드프롬프트반복횟수) / 세션ID(8자) / 첫_user_메시지(300자)
count가 \`x2\` 이상이면 같은 프롬프트로 반복 실행된 자동화 세션. 작업 단위로는 1건으로 보세요.
\`\`\`
${CC_CONTEXT:-(Claude Code 세션 없음 또는 비활성)}
\`\`\`

### B. opencode 세션 (이미 추출됨)
컬럼: 갱신시각 / 작업디렉토리 / 자동요약제목 / 첫_user_메시지(300자)
title은 LLM 자동 요약이라 정확도 한계가 있으니 first_msg를 함께 보고 판단하세요.
\`\`\`
${OPENCODE_CONTEXT:-(opencode 세션 없음 또는 비활성)}
\`\`\`

### C. git 커밋 (해당 기간, 세션이 발생한 디렉토리 한정)
디렉토리별로 그룹화. 각 줄: \`  HH:MM 해시 메시지\`
\`결과물\` 항목은 추측하지 말고 이 블록의 실제 커밋 해시/메시지만 인용하세요.
\`\`\`
${COMMIT_CONTEXT:-(커밋 없음 또는 비활성)}
\`\`\`

## 요약 작성 규칙
- A·B를 종합해 같은 프로젝트(=같은 디렉토리/주제)는 한 항목으로 통합. 도구 구분이 필요하면 \`[cc]\`/\`[oc]\` 태그를 붙이세요.
- A·B·C 모두 비어있으면 아무 출력도 하지 마세요 (빈 출력).
- 중요: 구분선(---)부터 바로 시작. 다른 설명/인사/상태 보고 없이 형식만 출력.

## 출력 형식

---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

### 작업 내역
- **<프로젝트명>**: 작업 내용 1줄 요약
  - 주요 결정: 있으면 기록
  - 결과물: 커밋 해시/메시지 (블록 C에 있는 것만)

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

# claude 호출 실패와 "작업 없음(빈 출력)"을 구분하기 위해 exit code 분리 캡처
set +e
claude --print \
    --dangerously-skip-permissions \
    --model sonnet \
    --output-format text \
    "$PROMPT" \
    < /dev/null \
    > "$TMPFILE" 2>> "$LOGFILE"
CLAUDE_EXIT=$?
set -e

# 헤더 보장 헬퍼 (실패/빈출력 양쪽에서 사용)
ensure_header() {
    if [[ ! -f "$SUMMARY_FILE" ]]; then
        cat > "$SUMMARY_FILE" <<EOF
# Claude 세션 요약

> 자동 생성 파일. 매주 수요일 00시 로테이트.
EOF
    fi
}

# claude 자체가 실패한 경우: 빈 출력과 구분해서 명시적으로 기록
if [[ $CLAUDE_EXIT -ne 0 ]]; then
    echo "[$DATE] claude 호출 실패 (exit=${CLAUDE_EXIT})" >> "$LOGFILE"
    ensure_header
    cat >> "$SUMMARY_FILE" <<EOF

---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

_(요약 실패: claude exit=${CLAUDE_EXIT}, see logs/summary.log)_
EOF
    echo "[$DATE] ===== 완료 (실패 헤더) =====" >> "$LOGFILE"
    exit 0
fi

# 빈 결과면 헤더만 기록 (날짜 누락 방지 + 작업 없음 가시화)
if [[ ! -s "$TMPFILE" ]] || ! grep -q '[^[:space:]]' "$TMPFILE"; then
    echo "[$DATE] 검색된 세션 없음 — 헤더만 기록" >> "$LOGFILE"
    ensure_header
    cat >> "$SUMMARY_FILE" <<EOF

---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

_(작업 없음)_
EOF
    echo "[$DATE] ===== 완료 (빈 헤더) =====" >> "$LOGFILE"
    exit 0
fi

# 헤더가 없으면 생성
ensure_header

# 당일 요약 누적 추가
echo "" >> "$SUMMARY_FILE"
cat "$TMPFILE" >> "$SUMMARY_FILE"

echo "[$DATE] 요약 추가 완료: $(date)" >> "$LOGFILE"
echo "[$DATE] ===== 전체 완료 =====" >> "$LOGFILE"
