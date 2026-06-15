#!/bin/bash
# Managed by personal-ops (projects/personal-ops/cli-init/)
exec 9>/tmp/claude_init.lock
#flock -n 9 || {
#  echo "[$(date '+%F %T')] skipped: already running" >> "$LOG"
#  exit 0
#}

export PATH="/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/bin:/bin"
CLAUDE_BIN="/home/jhw/.local/bin/claude"
LOG="/home/jhw/ai/opencode/projects/personal-ops/cli-init/logs/claude-init.log"
timestamp=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$timestamp] Claude Code Start ... " >> "$LOG"
/usr/bin/timeout 20s "$CLAUDE_BIN" --model haiku -p 'OK only?' >> "$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "[$(date '+%F %T')] haiku 실패 (exit=$rc) — sonnet으로 폴백" >> "$LOG"
  /usr/bin/timeout 20s "$CLAUDE_BIN" --model sonnet -p 'OK only?' >> "$LOG" 2>&1
  rc=$?
fi
if [ "$rc" -ne 0 ]; then
  echo "[$(date '+%F %T')] sonnet 실패 (exit=$rc) — 기본 모델로 폴백" >> "$LOG"
  /usr/bin/timeout 20s "$CLAUDE_BIN" -p 'OK only?' >> "$LOG" 2>&1
  rc=$?
fi
echo "claude exit=$rc" >> "$LOG"
