#!/bin/bash
# Managed by personal-ops (projects/personal-ops/cli-init/)
set -u

exec 9>/tmp/codex_init.lock

export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v24.12.0/bin:/usr/bin:/bin"

LOG_DIR="/home/jhw/ai/opencode/projects/personal-ops/cli-init/logs"
LOG_FILE="$LOG_DIR/codex-init.log"
WORKDIR="/home/jhw/ai/codex"  # codex 세션 디렉터리 (이동 안 함)

flock -n 9 || {
  echo "[$(date '+%F %T')] skipped: already running" >> "$LOG_FILE"
  exit 0
}

mkdir -p "$LOG_DIR"

{
  echo "=================================================="
  echo "[$(date '+%F %T')] Codex Start ..."
  timeout 20s codex exec -C "$WORKDIR" --skip-git-repo-check --sandbox read-only -m gpt-5.4-mini "Reply with OK only."
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[$(date '+%F %T')] gpt-5.4-mini 실패 (exit=$rc) — 기본 모델로 폴백"
    timeout 20s codex exec -C "$WORKDIR" --skip-git-repo-check --sandbox read-only "Reply with OK only."
    rc=$?
  fi
  echo "[`date '+%F %T'`] codex exit=$rc"
} >> "$LOG_FILE" 2>&1
