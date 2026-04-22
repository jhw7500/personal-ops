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
/usr/bin/timeout 20s "$CLAUDE_BIN" -p 'OK only?' >> "$LOG" 2>&1
echo "claude exit=$?" >> "$LOG"
