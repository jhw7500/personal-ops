#!/bin/bash
# Managed by personal-ops (projects/personal-ops/cli-init/)

set -u

export PATH="/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/bin:/bin"
MODULE_DIR="${CLAUDE_INIT_MODULE_DIR:-/home/jhw/ai/opencode/projects/personal-ops/cli-init}"
CLAUDE_BIN="${CLAUDE_BIN:-/home/jhw/.local/bin/claude}"
LOG="${CLAUDE_INIT_LOG:-${MODULE_DIR}/logs/claude-init.log}"
LOCK_FILE="${CLAUDE_INIT_LOCK_FILE:-/tmp/claude_init.lock}"
TIMEOUT_BIN="${TIMEOUT_BIN:-/usr/bin/timeout}"
TIMEOUT_SECONDS="${CLAUDE_INIT_TIMEOUT_SECONDS:-20}"
TIMEOUT_KILL_AFTER_SECONDS="${CLAUDE_INIT_TIMEOUT_KILL_AFTER_SECONDS:-5}"

mkdir -p "$(dirname "$LOG")"
exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date '+%F %T')] skipped: already running" >> "$LOG"
  exit 0
}

timestamp=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$timestamp] Claude auth health check start" >> "$LOG"

if [[ ! "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "$TIMEOUT_KILL_AFTER_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[$(date '+%F %T')] Claude auth health check failed class=health-error invalid timeout config" >> "$LOG"
  exit 2
fi

rc=0
"$TIMEOUT_BIN" --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" "${TIMEOUT_SECONDS}s" \
  "$CLAUDE_BIN" auth status --json \
  > /dev/null 2>> "$LOG" || rc=$?

if [[ $rc -eq 0 ]]; then
  echo "[$(date '+%F %T')] Claude auth health check ok" >> "$LOG"
  exit 0
fi

if [[ $rc -eq 124 || $rc -eq 137 ]]; then
  failure_class="timeout"
else
  failure_class="health-error"
fi
echo "[$(date '+%F %T')] Claude auth health check failed class=${failure_class} exit=${rc}" >> "$LOG"
exit "$rc"
