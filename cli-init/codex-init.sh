#!/bin/bash
# Managed by personal-ops (projects/personal-ops/cli-init/)
set -u

export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v24.12.0/bin:/usr/bin:/bin"

MODULE_DIR="${CODEX_INIT_MODULE_DIR:-/home/jhw/ai/opencode/projects/personal-ops/cli-init}"
LOG_DIR="${CODEX_INIT_LOG_DIR:-${MODULE_DIR}/logs}"
LOG_FILE="${CODEX_INIT_LOG:-${LOG_DIR}/codex-init.log}"
LOCK_FILE="${CODEX_INIT_LOCK_FILE:-/tmp/codex_init.lock}"
CODEX_BIN="${CODEX_BIN:-codex}"
WORKDIR="${CODEX_INIT_WORKDIR:-/home/jhw/ai/codex}"  # codex 세션 디렉터리 (이동 안 함)
TIMEOUT_SECONDS="${CODEX_INIT_TIMEOUT_SECONDS:-20}"
MAX_LINES="${CODEX_INIT_LOG_MAX_LINES:-2000}"

mkdir -p "$LOG_DIR"

if [[ ! "$MAX_LINES" =~ ^[1-9][0-9]*$ ]]; then
  echo "[$(date '+%F %T')] invalid CODEX_INIT_LOG_MAX_LINES=${MAX_LINES} — 기본값 2000 사용" >> "$LOG_FILE"
  MAX_LINES=2000
fi

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date '+%F %T')] skipped: already running" >> "$LOG_FILE"
  exit 0
}

# 로그가 MAX_LINES를 넘으면 최근 MAX_LINES줄만 남긴다.
# cat 리다이렉트로 제자리 절삭 → inode·권한이 유지된다 (flock 보유 중에만 호출).
trim_log() {
  local lines tmp
  [ -f "$LOG_FILE" ] || return 0
  lines=$(wc -l < "$LOG_FILE" 2>/dev/null) || return 0
  [ "$lines" -gt "$MAX_LINES" ] || return 0
  tmp="${LOG_FILE}.trim.$$"
  if tail -n "$MAX_LINES" "$LOG_FILE" > "$tmp" 2>/dev/null; then
    cat "$tmp" > "$LOG_FILE"
  fi
  rm -f "$tmp"
}

OUT_FILE="${LOG_FILE}.out.$$"
cleanup() {
  rm -f "$OUT_FILE"
  trim_log
}
trap cleanup EXIT

run_codex() {
  timeout "${TIMEOUT_SECONDS}s" "$CODEX_BIN" exec -C "$WORKDIR" \
    --skip-git-repo-check --sandbox read-only "$@" "Reply with OK only."
}

# codex 세션 출력 전문은 성공 시 남기지 않는다 (실행당 수십 줄).
# 대신 마지막 비어있지 않은 응답 줄을 증거로 요약에 싣는다 —
# codex exec 는 stdout 에 에러를 찍고도 exit=0 을 반환한 전례가 있어
# exit code 만으로는 성공을 판정할 수 없다.
last_reply() {
  grep -v '^[[:space:]]*$' "$OUT_FILE" 2>/dev/null \
    | tail -n 1 \
    | sed -e 's/\x1b\[[0-9;]*[a-zA-Z]//g' -e 's/[[:cntrl:]]//g' \
    | cut -c1-120
}

echo "[$(date '+%F %T')] Codex Start ..." >> "$LOG_FILE"

rc=0
run_codex -m gpt-5.4-mini > "$OUT_FILE" 2>&1 || rc=$?

if [ "$rc" -ne 0 ]; then
  echo "[$(date '+%F %T')] gpt-5.4-mini 실패 (exit=$rc) — 기본 모델로 폴백" >> "$LOG_FILE"
  cat "$OUT_FILE" >> "$LOG_FILE"
  rc=0
  run_codex > "$OUT_FILE" 2>&1 || rc=$?
fi

reply=$(last_reply)

if [ "$rc" -ne 0 ]; then
  cat "$OUT_FILE" >> "$LOG_FILE"
fi

echo "[$(date '+%F %T')] codex exit=$rc reply=\"${reply}\"" >> "$LOG_FILE"
exit "$rc"
