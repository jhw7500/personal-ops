#!/bin/bash
# claude-token-sync 모듈 제거 — 심링크/유닛만 정리. 런타임(로그/마커)·credentials 는 보존.
set -uo pipefail

BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
CRED_DIR="$HOME/.claude"

systemctl --user disable --now claude-token-sync-health.timer 2>/dev/null || true
systemctl --user disable --now claude-token-sync.service 2>/dev/null || true

rm -f "$UNIT_DIR/claude-token-sync.service" \
      "$UNIT_DIR/claude-token-sync-health.service" \
      "$UNIT_DIR/claude-token-sync-health.timer" \
      "$BIN_DIR/claude-token-sync.sh" \
      "$BIN_DIR/claude-token-sync-health.sh" \
      "$CRED_DIR/.token_sync_repos"

systemctl --user daemon-reload

echo "[uninstall] 심링크·유닛 제거 완료."
echo "[uninstall] 보존: $CRED_DIR/token_sync.log, $CRED_DIR/.token_sync_health.sha, $CRED_DIR/.credentials.json"
