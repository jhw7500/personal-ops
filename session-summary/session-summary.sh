#!/bin/bash
# session-summary.sh - Claude 세션 요약 자동화 (매일)
# Managed by personal-ops (projects/personal-ops/session-summary/)
#
# crontab:
#   0 9 * * *    /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh
#   10 9 * * 3   /home/jhw/ai/opencode/projects/personal-ops/session-summary/session-summary.sh rotate
#
# 동작:
#   - 기본: 어제 1일치 세션을 episodic-memory + opencode SQLite에서 검색 → 요약을 logs/session-summary.md에 누적
#   - 빈 결과: 헤더만 기록 (작업 없음 표시)
#   - rotate: logs/session-summary.md를 archive/로 이동 후 새 파일 시작
#   - REMOTE_HOSTS가 있으면 SSH로 원격 ~/.claude/projects/ + opencode.db를 rsync해 합쳐 요약

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODULE_DIR="${SESSION_SUMMARY_MODULE_DIR:-/home/jhw/ai/opencode/projects/personal-ops/session-summary}"
SUMMARY_FILE="${MODULE_DIR}/logs/session-summary.md"
LOGFILE="${MODULE_DIR}/logs/summary.log"
ARCHIVE_DIR="${MODULE_DIR}/archive"
STATE_DIR="${MODULE_DIR}/state"
BACKOFF_FILE="${STATE_DIR}/claude-backoff"
LOCK_FILE="${SESSION_SUMMARY_LOCK_FILE:-/tmp/session_summary.lock}"
CLAUDE_BIN="${CLAUDE_BIN:-/home/jhw/.local/bin/claude}"
TIMEOUT_BIN="${TIMEOUT_BIN:-/usr/bin/timeout}"
CLAUDE_MODEL="${SESSION_SUMMARY_MODEL:-haiku}"
CLAUDE_EFFORT="${SESSION_SUMMARY_EFFORT:-low}"
MAX_INPUT_BYTES="${SESSION_SUMMARY_MAX_INPUT_BYTES:-65536}"
TIMEOUT_SECONDS="${SESSION_SUMMARY_TIMEOUT_SECONDS:-180}"
TIMEOUT_KILL_AFTER_SECONDS="${SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS:-5}"
QUOTA_BACKOFF_SECONDS="${SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS:-21600}"
AUTH_BACKOFF_SECONDS="${SESSION_SUMMARY_AUTH_BACKOFF_SECONDS:-86400}"
RESOLUTION_TRACKING="${SESSION_SUMMARY_RESOLUTION_TRACKING:-1}"
RESOLUTION_TRACKER="${SESSION_SUMMARY_RESOLUTION_TRACKER:-${SCRIPT_DIR}/resolution-tracker.py}"
UNRESOLVED_STATE_FILE="${STATE_DIR}/unresolved-items.json"
RUN_STARTED_EPOCH=$(date +%s)
DATE=$(date +%Y-%m-%d)
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)

# opencode 세션 포함 여부 (1=포함, 0=제외)
# DB 파일을 직접 read-only로 읽으므로 opencode 바이너리/토큰 의존 없음
INCLUDE_OPENCODE="${INCLUDE_OPENCODE:-1}"
OPENCODE_DB="${OPENCODE_DB:-/home/jhw/.local/share/opencode/opencode.db}"

# Claude Code 세션 포함 여부 (1=포함, 0=제외)
# ~/.claude/projects/*/*.jsonl을 직접 읽어 시간 기반으로 추출 (opencode와 대칭)
INCLUDE_CC="${INCLUDE_CC:-1}"
CC_PROJECTS_DIR="${CC_PROJECTS_DIR:-/home/jhw/.claude/projects}"

# git 커밋 컨텍스트 포함 여부 (1=포함, 0=제외)
# 세션에서 발견된 cwd들에서 해당 기간 커밋 추출 (로컬 경로만 — 원격은 [host] 라벨이라 자동 제외)
INCLUDE_COMMITS="${INCLUDE_COMMITS:-1}"

# 원격 호스트 (Tailscale MagicDNS 등) — 공백 구분
# 각 호스트의 ~/.claude/projects/ 와 opencode.db를 rsync로 가져와 합쳐 요약
# 비어있으면 로컬만 사용 (기존 동작과 동일)
REMOTE_HOSTS="${REMOTE_HOSTS-hwjo-1}"
REMOTE_USER="${REMOTE_USER:-jhw}"
STAGING_DIR="${MODULE_DIR}/staging"

NVM_BIN_PREFIX=""
if [[ -d /home/jhw/.nvm/versions/node ]]; then
    NVM_VERSION=$(find /home/jhw/.nvm/versions/node \
        -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | sort -V | tail -1)
    [[ -n "$NVM_VERSION" ]] && NVM_BIN_PREFIX="/home/jhw/.nvm/versions/node/${NVM_VERSION}/bin:"
fi
PATH="${NVM_BIN_PREFIX}/home/jhw/.local/bin:/usr/bin:/bin"
export PATH

mkdir -p "$ARCHIVE_DIR" "$STATE_DIR" "$(dirname "$LOGFILE")"

ensure_header() {
    if [[ ! -f "$SUMMARY_FILE" ]]; then
        cat > "$SUMMARY_FILE" <<EOF
# Claude 세션 요약

> 자동 생성 파일. 매주 수요일 09:10 로테이트.
EOF
    fi
}

append_failure_summary() {
    local failure_class=$1
    ensure_header
    cat >> "$SUMMARY_FILE" <<EOF

---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

_(요약 실패: ${failure_class}, see logs/summary.log)_
EOF
}

write_backoff() {
    local reason=$1
    local duration_seconds=$2
    local until_epoch marker_tmp
    until_epoch=$(( RUN_STARTED_EPOCH + duration_seconds ))
    marker_tmp="${BACKOFF_FILE}.tmp.$$"
    printf 'reason=%s\nstarted_epoch=%s\nuntil_epoch=%s\n' \
        "$reason" "$RUN_STARTED_EPOCH" "$until_epoch" > "$marker_tmp"
    mv "$marker_tmp" "$BACKOFF_FILE"
}

classify_claude_failure() {
    local exit_code=$1
    local error_file=$2

    if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then
        printf 'timeout\n'
    elif grep -Eqi 'usage limit|hit your ([[:alpha:]]+[[:space:]]+)*limit|rate[_ -]?limit(_error)?|quota|too many requests|credit balance( is)? too low|insufficient (usage )?credits|(^|[^0-9])429([^0-9]|$)' "$error_file"; then
        printf 'quota\n'
    elif grep -Eqi 'failed to authenticate|invalid authentication credentials|authentication_error|oauth.*(expired|invalid)|auth(entication|orization)? (failed|required)|not logged in|please (run )?/login|please (log in|login)|unauthorized|invalid api key|(^|[^0-9])401([^0-9]|$)' "$error_file"; then
        printf 'auth\n'
    else
        printf 'other\n'
    fi
}

exec 8>"$LOCK_FILE"

# --- 로테이트 모드 ---
if [[ "${1:-}" == "rotate" ]]; then
    echo "[$DATE] 로테이트 대기: 실행 중인 요약 확인" >> "$LOGFILE"
    if ! flock 8; then
        echo "[$DATE] 로테이트 실패: lock 획득 실패" >> "$LOGFILE"
        exit 1
    fi
    echo "[$DATE] ===== 로테이트 시작: $(date) =====" >> "$LOGFILE"
    if [[ -s "$SUMMARY_FILE" ]]; then
        # 수요일 09:10 로테이트 기준: 지난 수요일(7일 전) ~ 어제(화요일)
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

if ! flock -n 8; then
    echo "[$DATE] skipped: already running" >> "$LOGFILE"
    exit 0
fi

validate_positive_config() {
    local config_name=$1
    local config_value=$2
    if [[ ! "$config_value" =~ ^[1-9][0-9]*$ ]]; then
        echo "[$DATE] Claude 호출 생략 class=other invalid_config=${config_name} value=${config_value}" >> "$LOGFILE"
        append_failure_summary other
        return 1
    fi
}

validate_boolean_config() {
    local config_name=$1
    local config_value=$2
    if [[ "$config_value" != "0" && "$config_value" != "1" ]]; then
        echo "[$DATE] Claude 호출 생략 class=other invalid_config=${config_name} value=${config_value}" >> "$LOGFILE"
        append_failure_summary other
        return 1
    fi
}

validate_positive_config SESSION_SUMMARY_MAX_INPUT_BYTES "$MAX_INPUT_BYTES" || exit 0
validate_positive_config SESSION_SUMMARY_TIMEOUT_SECONDS "$TIMEOUT_SECONDS" || exit 0
validate_positive_config SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS "$TIMEOUT_KILL_AFTER_SECONDS" || exit 0
validate_positive_config SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS "$QUOTA_BACKOFF_SECONDS" || exit 0
validate_positive_config SESSION_SUMMARY_AUTH_BACKOFF_SECONDS "$AUTH_BACKOFF_SECONDS" || exit 0
validate_boolean_config SESSION_SUMMARY_RESOLUTION_TRACKING "$RESOLUTION_TRACKING" || exit 0

if [[ -f "$BACKOFF_FILE" ]]; then
    BACKOFF_REASON=$(awk -F= '$1 == "reason" {print $2}' "$BACKOFF_FILE")
    BACKOFF_UNTIL=$(awk -F= '$1 == "until_epoch" {print $2}' "$BACKOFF_FILE")
    if [[ "$BACKOFF_UNTIL" =~ ^[0-9]+$ ]] && (( $(date +%s) < BACKOFF_UNTIL )); then
        echo "[$DATE] Claude 호출 보류 class=${BACKOFF_REASON:-other} until_epoch=${BACKOFF_UNTIL}" >> "$LOGFILE"
        exit 0
    fi
fi

# --- 원격 호스트 동기화 (REMOTE_HOSTS가 비어있지 않으면) ---
# 각 호스트의 ~/.claude/projects/ 와 opencode.db를 staging/{host}/ 로 rsync.
# 실패 시 해당 호스트는 스킵하고 로컬 또는 다른 원격만으로 진행.
ACTIVE_REMOTES=""
if [[ -n "$REMOTE_HOSTS" ]]; then
    mkdir -p "$STAGING_DIR"
    for host in $REMOTE_HOSTS; do
        HOST_STG="${STAGING_DIR}/${host}"
        mkdir -p "${HOST_STG}/projects"
        set +e
        rsync -az --timeout=30 --partial \
            -e "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
            "${REMOTE_USER}@${host}:.claude/projects/" \
            "${HOST_STG}/projects/" 2>>"$LOGFILE"
        RSYNC_PROJ=$?
        rsync -az --timeout=30 --partial \
            -e "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
            "${REMOTE_USER}@${host}:.local/share/opencode/opencode.db" \
            "${HOST_STG}/opencode.db" 2>>"$LOGFILE"
        RSYNC_DB=$?
        set -e
        if [[ $RSYNC_PROJ -eq 0 || $RSYNC_DB -eq 0 ]]; then
            ACTIVE_REMOTES+="${host} "
            echo "[$DATE] 원격 동기화 OK: ${host} (projects=${RSYNC_PROJ}, db=${RSYNC_DB})" >> "$LOGFILE"
        else
            echo "[$DATE] 원격 동기화 실패: ${host} (projects=${RSYNC_PROJ}, db=${RSYNC_DB}) - 스킵" >> "$LOGFILE"
        fi
    done
fi

# Python 추출기에 주입할 추가 소스 리터럴 빌드 (호스트별 라벨 부여)
EXTRA_OC_SOURCES_PY=""
EXTRA_CC_SOURCES_PY=""
for h in $ACTIVE_REMOTES; do
    OC="${STAGING_DIR}/${h}/opencode.db"
    CC="${STAGING_DIR}/${h}/projects"
    [[ -f "$OC" ]] && EXTRA_OC_SOURCES_PY+="    (\"${h}\", \"${OC}\"),"$'\n'
    [[ -d "$CC" ]] && EXTRA_CC_SOURCES_PY+="    (\"${h}\", \"${CC}\"),"$'\n'
done

# --- opencode 세션 추출 (로컬 + 원격 staging 모두) ---
# Python 표준 sqlite3로 DB 파일을 read-only 직접 읽음 (opencode 바이너리 미실행)
OPENCODE_CONTEXT=""
if [[ "$INCLUDE_OPENCODE" == "1" ]]; then
    SEARCH_FROM_MS="$(date -d "$SEARCH_FROM 00:00:00" +%s)000"
    SEARCH_TO_MS="$(date -d "today 00:00:00" +%s)000"

    OPENCODE_RAW=$(python3 - <<PYEOF 2>>"$LOGFILE" || true
import sqlite3, os, sys
SOURCES = [
    ("", "${OPENCODE_DB}"),
${EXTRA_OC_SOURCES_PY}]
SOURCES = [(lbl, p) for (lbl, p) in SOURCES if os.path.exists(p) and os.access(p, os.R_OK)]
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
print("upd\tdirectory\ttitle\tfirst_msg")
for label, path in SOURCES:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = db.execute(sql, (${SEARCH_FROM_MS}, ${SEARCH_TO_MS})).fetchall()
    except Exception as e:
        print(f"# opencode skip {path}: {e}", file=sys.stderr)
        continue
    prefix = f"[{label}] " if label else ""
    for r in rows:
        cells = [(c or "") for c in r]
        cells[1] = (prefix + cells[1]) if cells[1] else prefix.rstrip()
        print("\t".join(cells))
PYEOF
)

    OPENCODE_LINES=$(printf '%s\n' "$OPENCODE_RAW" | tail -n +2 | grep -c . || true)
    if [[ "$OPENCODE_LINES" -gt 0 ]]; then
        OPENCODE_CONTEXT="$OPENCODE_RAW"
        echo "[$DATE] opencode 세션 추출: ${OPENCODE_LINES}건" >> "$LOGFILE"
    else
        echo "[$DATE] opencode 세션 없음 (기간: $SEARCH_FROM ~ $DATE)" >> "$LOGFILE"
    fi
fi

# --- Claude Code 세션 추출 (로컬 + 원격 staging 모두) ---
# ~/.claude/projects/*/*.jsonl을 시간 기반으로 직접 스캔
CC_CONTEXT=""
if [[ "$INCLUDE_CC" == "1" ]]; then
    CC_RAW=$(python3 - <<PYEOF 2>>"$LOGFILE" || true
import os, json, glob, re
from datetime import datetime, timezone, timedelta

SOURCES = [
    ("", "${CC_PROJECTS_DIR}"),
${EXTRA_CC_SOURCES_PY}]
SOURCES = [(lbl, p) for (lbl, p) in SOURCES if os.path.isdir(p)]
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

sessions = {}  # (label, session_id) -> {ts, local, cwd, first_msg}
mtime_floor = start_utc.timestamp() - 86400  # 하루 여유

for label, projects_dir in SOURCES:
    prefix = f"[{label}] " if label else ""
    for jsonl_path in glob.glob(os.path.join(projects_dir, '*', '*.jsonl')):
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
                key = (label, sid)
                # 같은 (호스트,세션)의 더 늦은 메시지면 skip (가장 이른 user 메시지 = 시드)
                if key in sessions and sessions[key]['ts'] <= t:
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
                labeled_cwd = (prefix + cwd) if cwd else prefix.rstrip()
                local_str = t.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')
                sessions[key] = {'ts': t, 'local': local_str, 'cwd': labeled_cwd, 'first_msg': preview}

# 중복 시드 메시지 dedup (자동화 봇이 동일 프롬프트로 반복 실행되는 경우 압축)
# 키: (labeled_cwd, first_msg 앞 120자) — 라벨이 다르면 별개 그룹 (호스트 구분 유지)
groups = {}
for key, s in sessions.items():
    gkey = (s['cwd'], s['first_msg'][:120])
    g = groups.get(gkey)
    if g is None or s['ts'] < g['ts']:
        groups[gkey] = {**s, 'sid': key[1], 'count': (g['count'] if g else 0) + 1}
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
fi

# --- 세션에서 관측된 로컬 git root ---
# 원격 cwd는 `[host] /path` 형태라 제외한다. workspace/부모 디렉토리는 스캔하지 않는다.
SESSION_DIRS=$( { printf '%s\n' "$OPENCODE_CONTEXT"; printf '%s\n' "$CC_CONTEXT"; } \
    | awk -F'\t' '$2 ~ /^\// {print $2}' \
    | sort -u )
REPO_ROOTS=()
declare -A SEEN_REPO_ROOTS=()
if [[ -n "$SESSION_DIRS" ]]; then
    while IFS= read -r session_dir; do
        [[ -z "$session_dir" || ! -d "$session_dir" ]] && continue
        repo_root=$(git -C "$session_dir" rev-parse --show-toplevel 2>/dev/null || true)
        [[ "$repo_root" != /* || ! -d "$repo_root" ]] && continue
        if [[ -z "${SEEN_REPO_ROOTS[$repo_root]+present}" ]]; then
            SEEN_REPO_ROOTS["$repo_root"]=1
            REPO_ROOTS+=("$repo_root")
        fi
    done <<< "$SESSION_DIRS"
fi

# --- git 커밋 컨텍스트 (INCLUDE_COMMITS=1일 때만) ---
# 두 세션 컨텍스트에서 발견된 distinct cwd들에 한해 커밋 조회 (로컬 절대 경로만)
# 원격 cwd는 `[host] /path` 형태라 `/`로 시작 안 함 → awk 필터에서 자동 제외
COMMIT_CONTEXT=""
if [[ "$INCLUDE_COMMITS" == "1" ]]; then
    COMMIT_BLOCKS=""
    if (( ${#REPO_ROOTS[@]} > 0 )); then
        for repo_root in "${REPO_ROOTS[@]}"; do
            OUT=$(git -C "$repo_root" log --all --no-merges \
                --since="$SEARCH_FROM 00:00" \
                --until="$DATE 00:00" \
                --pretty=format:"  %ad %h %s" \
                --date=format:'%H:%M' 2>/dev/null || true)
            if [[ -n "$OUT" ]]; then
                COMMIT_BLOCKS+=$'\n'"### ${repo_root}"$'\n'"$OUT"$'\n'
            fi
        done
    fi
    if [[ -n "$COMMIT_BLOCKS" ]]; then
        COMMIT_CONTEXT="$COMMIT_BLOCKS"
        COMMIT_LINES=$(printf '%s\n' "$COMMIT_BLOCKS" | grep -c '^  ' || true)
        echo "[$DATE] git 커밋 추출: ${COMMIT_LINES}건" >> "$LOGFILE"
    else
        echo "[$DATE] git 커밋 없음 (기간: $SEARCH_FROM ~ $DATE)" >> "$LOGFILE"
    fi
fi

RUN_DIR=$(mktemp -d "${STATE_DIR}/.resolution-run.XXXXXX")
chmod 700 "$RUN_DIR"
TMPFILE="${RUN_DIR}/generated.md"
ERRFILE="${RUN_DIR}/claude.err"
MANIFEST_FILE="${RUN_DIR}/manifest.json"
RESOLUTION_CONTEXT_FILE="${RUN_DIR}/resolution-context.txt"
VALIDATED_FILE="${RUN_DIR}/validated.md"
NEXT_STATE_FILE="${RUN_DIR}/next-state.json"

cleanup_run_dir() {
    if [[ -n "${RUN_DIR:-}" && "$RUN_DIR" == "${STATE_DIR}/.resolution-run."* ]]; then
        rm -rf -- "$RUN_DIR"
    fi
}
trap cleanup_run_dir EXIT

RESOLUTION_CONTEXT=""
if [[ "$RESOLUTION_TRACKING" == "1" ]]; then
    REPO_ARGS=()
    PRIOR_SUMMARY_ARGS=()
    for repo_root in "${REPO_ROOTS[@]}"; do
        REPO_ARGS+=(--repo "$repo_root")
    done
    LATEST_ARCHIVE=""
    for archive_candidate in "${ARCHIVE_DIR}"/summary-*.md; do
        if [[ -f "$archive_candidate" ]] && \
            { [[ -z "$LATEST_ARCHIVE" ]] || [[ "$archive_candidate" -nt "$LATEST_ARCHIVE" ]]; }; then
            LATEST_ARCHIVE="$archive_candidate"
        fi
    done
    if [[ -n "$LATEST_ARCHIVE" ]]; then
        PRIOR_SUMMARY_ARGS=(--prior-summary "$LATEST_ARCHIVE")
    fi
    if ! "$RESOLUTION_TRACKER" prepare \
        --summary "$SUMMARY_FILE" \
        "${PRIOR_SUMMARY_ARGS[@]}" \
        --state "$UNRESOLVED_STATE_FILE" \
        --manifest "$MANIFEST_FILE" \
        --context "$RESOLUTION_CONTEXT_FILE" \
        "${REPO_ARGS[@]}" >> "$LOGFILE" 2>&1; then
        echo "[$DATE] resolution tracker prepare failed" >> "$LOGFILE"
        echo "[$DATE] ===== 완료 (resolution prepare 실패) =====" >> "$LOGFILE"
        exit 0
    fi
    if [[ -s "$RESOLUTION_CONTEXT_FILE" ]]; then
        RESOLUTION_CONTEXT=$(<"$RESOLUTION_CONTEXT_FILE")
    fi
fi

PROMPT=$(cat <<PROMPT_END
당신은 업무 세션 정리 비서입니다. 오늘: ${DATE}, 검색 시작일: ${SEARCH_FROM}

네 종류의 사실 컨텍스트가 사전 추출되어 아래에 제공됩니다. 추가 도구 호출 없이 이 컨텍스트만으로 종합 요약하세요.

### A. Claude Code 세션 (이미 추출됨)
컬럼: 시작시각 / 작업디렉토리 / count(동일시드프롬프트반복횟수) / 세션ID(8자) / 첫_user_메시지(300자)
count가 \`x2\` 이상이면 같은 프롬프트로 반복 실행된 자동화 세션. 작업 단위로는 1건으로 보세요.
디렉토리에 \`[hostname] \` 접두어가 있으면 다른 PC에서 한 작업입니다. 같은 프로젝트(리포)면 통합하되 호스트 차이는 짧게 명시하세요 (예: "(hwjo-1에서도 작업)").
\`\`\`
${CC_CONTEXT:-(Claude Code 세션 없음 또는 비활성)}
\`\`\`

### B. opencode 세션 (이미 추출됨)
컬럼: 갱신시각 / 작업디렉토리 / 자동요약제목 / 첫_user_메시지(300자)
title은 LLM 자동 요약이라 정확도 한계가 있으니 first_msg를 함께 보고 판단하세요.
디렉토리에 \`[hostname] \` 접두어가 있으면 다른 PC 작업입니다 (위와 동일 규칙).
\`\`\`
${OPENCODE_CONTEXT:-(opencode 세션 없음 또는 비활성)}
\`\`\`

### C. git 커밋 (해당 기간, 세션이 발생한 로컬 디렉토리 한정)
디렉토리별로 그룹화. 각 줄: \`  HH:MM 해시 메시지\`
\`결과물\` 항목은 추측하지 말고 이 블록의 실제 커밋 해시/메시지만 인용하세요.
원격 PC 커밋은 여기 포함되지 않습니다.
\`\`\`
${COMMIT_CONTEXT:-(커밋 없음 또는 비활성)}
\`\`\`

### D. 이전 미완료 항목과 resolution 후보 (사전 검증됨)
각 ITEM은 반드시 정확히 한 번 출력하세요. CANDIDATE가 없거나 STATUS가 ready가 아니면 해결 처리하지 마세요.
\`\`\`
${RESOLUTION_CONTEXT:-(이전 미완료 항목 없음 또는 추적 비활성)}
\`\`\`

## 요약 작성 규칙
- A·B를 종합해 같은 프로젝트(=같은 디렉토리/주제)는 한 항목으로 통합. 도구 구분이 필요하면 \`[cc]\`/\`[oc]\` 태그를 붙이세요.
- 같은 git repo를 두 PC에서 작업했으면 한 항목으로 통합하고 호스트 차이만 짧게 메모.
- A·B·C가 모두 비어 있고 D에 ITEM도 없으면 아무 출력도 하지 마세요 (빈 출력).
- D의 기존 ITEM은 누락·중복 없이 정확히 한 번 출력하고 숨은 unresolved-id를 그대로 다음 줄에 복사하세요.
- 해결 표시는 해당 ITEM에 나열된 CANDIDATE SHA만 사용하세요. 커밋 제목만으로 해결 처리 금지이며 diff 근거가 실제 항목을 해결한다고 판단될 때만 \`[x]\`로 바꾸세요.
- 기존 ITEM의 원문·프로젝트·우선순위·숫자·단위·코드 심볼은 절대 변경하지 마세요. 숫자 집계·환산·반올림과 조수사(개/건/회) 변경도 금지합니다.
- 검증 불가, 후보 없음, 판단 불충분이면 원본 \`[ ]\` 줄과 unresolved-id를 그대로 출력하세요.
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
<!-- unresolved-id:unresolved-12자리hex -->

해결된 기존 ITEM만 다음 형식을 사용:
- [x] 원본 항목 — 원본 프로젝트 [resolved by D에 제시된 SHA]
<!-- unresolved-id:기존 ITEM의 동일 ID -->

### 기술 메모
세션 중 발견된 중요한 기술 사항 (없으면 생략).
PROMPT_END
)

PROMPT_BYTES=$(LC_ALL=C printf '%s' "$PROMPT" | wc -c | tr -d ' ')
if (( PROMPT_BYTES > MAX_INPUT_BYTES )); then
    echo "[$DATE] Claude 호출 생략 class=input-limit input_bytes=${PROMPT_BYTES} max_input_bytes=${MAX_INPUT_BYTES}" >> "$LOGFILE"
    append_failure_summary input-limit
    echo "[$DATE] ===== 완료 (입력 상한) =====" >> "$LOGFILE"
    exit 0
fi

echo "[$DATE] Claude 실행 시작 model=${CLAUDE_MODEL} effort=${CLAUDE_EFFORT} input_bytes=${PROMPT_BYTES} max_input_bytes=${MAX_INPUT_BYTES} timeout_seconds=${TIMEOUT_SECONDS} kill_after_seconds=${TIMEOUT_KILL_AFTER_SECONDS}" >> "$LOGFILE"

# claude 호출 실패와 "작업 없음(빈 출력)"을 구분하기 위해 exit code 분리 캡처
set +e
"$TIMEOUT_BIN" --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" "${TIMEOUT_SECONDS}s" \
    "$CLAUDE_BIN" --print \
    --model "$CLAUDE_MODEL" \
    --effort "$CLAUDE_EFFORT" \
    --tools "" \
    --disable-slash-commands \
    --no-session-persistence \
    --output-format text \
    "$PROMPT" \
    < /dev/null \
    > "$TMPFILE" 2> "$ERRFILE"
CLAUDE_EXIT=$?
set -e
cat "$ERRFILE" >> "$LOGFILE"

# claude 자체가 실패한 경우: 빈 출력과 구분해서 명시적으로 기록
if [[ $CLAUDE_EXIT -ne 0 ]]; then
    FAILURE_CLASS=$(classify_claude_failure "$CLAUDE_EXIT" "$ERRFILE")
    echo "[$DATE] Claude 호출 실패 class=${FAILURE_CLASS} exit=${CLAUDE_EXIT}" >> "$LOGFILE"
    case "$FAILURE_CLASS" in
        quota) write_backoff quota "$QUOTA_BACKOFF_SECONDS" ;;
        auth) write_backoff auth "$AUTH_BACKOFF_SECONDS" ;;
    esac
    append_failure_summary "$FAILURE_CLASS"
    echo "[$DATE] ===== 완료 (실패 헤더) =====" >> "$LOGFILE"
    exit 0
fi

# 진단 토글에서는 기존 direct-append 동작을 그대로 유지한다.
if [[ "$RESOLUTION_TRACKING" == "0" ]]; then
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

    ensure_header
    echo "" >> "$SUMMARY_FILE"
    cat "$TMPFILE" >> "$SUMMARY_FILE"
    echo "[$DATE] 요약 추가 완료: $(date)" >> "$LOGFILE"
    echo "[$DATE] ===== 전체 완료 =====" >> "$LOGFILE"
    exit 0
fi

# 빈 모델 출력도 이전 open item/state를 잃지 않도록 검증 가능한 일일 골격으로 바꾼다.
if [[ ! -s "$TMPFILE" ]] || ! grep -q '[^[:space:]]' "$TMPFILE"; then
    cat > "$TMPFILE" <<EOF
---

## ${DATE} (${SEARCH_FROM} ~ ${DATE})

_(작업 없음)_

### 미완료 항목
EOF
fi

if ! "$RESOLUTION_TRACKER" reconcile \
    --generated "$TMPFILE" \
    --manifest "$MANIFEST_FILE" \
    --validated "$VALIDATED_FILE" \
    --next-state "$NEXT_STATE_FILE" >> "$LOGFILE" 2>&1; then
    echo "[$DATE] resolution tracker reconcile failed" >> "$LOGFILE"
    append_failure_summary other
    echo "[$DATE] ===== 완료 (resolution reconcile 실패) =====" >> "$LOGFILE"
    exit 0
fi

ensure_header
echo "" >> "$SUMMARY_FILE"
cat "$VALIDATED_FILE" >> "$SUMMARY_FILE"

if ! mv -fT "$NEXT_STATE_FILE" "$UNRESOLVED_STATE_FILE"; then
    echo "[$DATE] resolution state replace failed after summary append" >> "$LOGFILE"
    exit 1
fi

echo "[$DATE] 요약 추가 완료: $(date)" >> "$LOGFILE"
echo "[$DATE] ===== 전체 완료 =====" >> "$LOGFILE"
