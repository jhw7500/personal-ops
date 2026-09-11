#!/bin/bash
# claude-token-sync 모듈 설치 — 스크립트/systemd 유닛/설정을 사용자 환경에 심링크로 배포.
# 정본은 이 모듈 디렉터리. ~/.local/bin, ~/.config/systemd/user, ~/.claude 로 심링크만 건다.
# 재실행 안전(idempotent).
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
CRED_DIR="$HOME/.claude"

# Validate the dedicated source before touching an existing installation.
# shellcheck disable=SC2034 # Read by load_ci_token in the sourced helper.
CRED_FILE="$CRED_DIR/.ci-oauth-token.json"
log() { printf '%s\n' "$*" >&2; }
# shellcheck source=bin/claude-token-sync-common.sh
# shellcheck disable=SC1091
source "$MODULE_DIR/bin/claude-token-sync-common.sh"
load_ci_token

mkdir -p "$BIN_DIR" "$UNIT_DIR" "$CRED_DIR"

# 1) 실행 스크립트 심링크
chmod +x "$MODULE_DIR"/bin/claude-token-sync.sh \
    "$MODULE_DIR"/bin/claude-token-sync-health.sh \
    "$MODULE_DIR"/bin/claude-token-sync-set-token.sh
ln -sf "$MODULE_DIR/bin/claude-token-sync.sh"        "$BIN_DIR/claude-token-sync.sh"
ln -sf "$MODULE_DIR/bin/claude-token-sync-health.sh" "$BIN_DIR/claude-token-sync-health.sh"
ln -sf "$MODULE_DIR/bin/claude-token-sync-set-token.sh" "$BIN_DIR/claude-token-sync-set-token.sh"

# 2) 공유 레포 목록 심링크 (데몬·헬스체크 단일 소스)
ln -sf "$MODULE_DIR/config/repos.txt" "$CRED_DIR/.token_sync_repos"

# 3) systemd 유닛 심링크
ln -sf "$MODULE_DIR/systemd/claude-token-sync.service"        "$UNIT_DIR/claude-token-sync.service"
ln -sf "$MODULE_DIR/systemd/claude-token-sync-health.service" "$UNIT_DIR/claude-token-sync-health.service"
ln -sf "$MODULE_DIR/systemd/claude-token-sync-health.timer"   "$UNIT_DIR/claude-token-sync-health.timer"

# 4) 장기 토큰은 10분 타이머만 사용한다. 재설치해도 상시 데몬을 켜지 않는다.
systemctl --user daemon-reload
systemctl --user stop claude-token-sync.service
systemctl --user disable claude-token-sync.service
# disable removes externally linked unit files too. Restore only the definition,
# leaving default.target.wants absent so the daemon stays out of automatic startup.
ln -sf "$MODULE_DIR/systemd/claude-token-sync.service" "$UNIT_DIR/claude-token-sync.service"
systemctl --user daemon-reload
systemctl --user enable --now claude-token-sync-health.timer
systemctl --user start claude-token-sync-health.service

echo "[install] 완료. 정본 디렉터리: $MODULE_DIR"
echo "[install] 상시 데몬: $(systemctl --user is-active claude-token-sync.service) / 10분 타이머: $(systemctl --user is-active claude-token-sync-health.timer)"
