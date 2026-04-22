#!/bin/bash
# Claude Code precise-fire — 정각(HH:00:00)에 첫 메시지를 보내
# 5시간 창 경계에 최대한 가깝게 맞춤.
#
# 사용법:
#   init-precise.sh <TARGET_HOUR> [LEAD_MS]
#   예: init-precise.sh 06        # 오늘 06:00:00에 fire
#       init-precise.sh 11 800    # 800ms 앞당겨 fire (claude CLI 부팅 보상)
#
# 환경변수 CLAUDE_LEAD_MS로도 lead 조정 가능 (기본 500ms).

set -u
exec 9>/tmp/claude_init_precise.lock

export PATH="/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/bin:/bin"
CLAUDE_BIN="/home/jhw/.local/bin/claude"
LOG="/home/jhw/ai/opencode/projects/personal-ops/cli-init/logs/claude-init.log"

flock -n 9 || {
  echo "[$(date '+%F %T')] precise: skipped (already running)" >> "$LOG"
  exit 0
}

TARGET_HOUR="${1:?TARGET_HOUR required (00-23)}"
LEAD_MS="${2:-${CLAUDE_LEAD_MS:-500}}"

# 오늘 HH:00:00을 epoch ns로 환산
TODAY=$(date +'%Y-%m-%d')
TARGET_EPOCH=$(date -d "$TODAY $TARGET_HOUR:00:00" +%s)
TARGET_NS=$(( TARGET_EPOCH * 1000000000 - LEAD_MS * 1000000 ))
NOW_NS=$(date +%s%N)
WAIT_NS=$(( TARGET_NS - NOW_NS ))

if [ "$WAIT_NS" -gt 0 ]; then
  WAIT_S=$(awk "BEGIN {printf \"%.6f\", $WAIT_NS / 1000000000}")
  sleep "$WAIT_S"
fi

FIRE_TS=$(date +'%Y-%m-%d %H:%M:%S.%3N')
printf '[%s] precise-fire target=%s:00 lead=%sms\n' \
  "$FIRE_TS" "$TARGET_HOUR" "$LEAD_MS" >> "$LOG"

/usr/bin/timeout 60s "$CLAUDE_BIN" -p 'ok' >> "$LOG" 2>&1
EXIT=$?

END_TS=$(date +'%Y-%m-%d %H:%M:%S.%3N')
printf '[%s] precise-fire done exit=%d\n' "$END_TS" "$EXIT" >> "$LOG"
