#!/bin/bash
# Dedicated CI OAuth token -> GitHub repository secrets. No CLI-login fallback.
unset GITHUB_TOKEN

# shellcheck disable=SC2034 # consumed by sourced common helper
CRED_FILE="$HOME/.claude/.ci-oauth-token.json"
LOG_FILE="$HOME/.claude/token_sync.log"
# shellcheck disable=SC2034
REPO_FILE="$HOME/.claude/.token_sync_repos"
# shellcheck disable=SC2034
SHA_FILE="$HOME/.claude/.token_sync_health.sha"
# shellcheck disable=SC2034
LOCK_FILE="$HOME/.claude/.token_sync.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
# shellcheck source=bin/claude-token-sync-common.sh
# shellcheck disable=SC1091
if ! source "$(dirname "$SCRIPT_PATH")/claude-token-sync-common.sh"; then
    log "[ERROR] common helper could not be loaded"
    exit 1
fi

POLL_INTERVAL="${CLAUDE_TOKEN_SYNC_POLL_INTERVAL:-30}"
log "[START] claude-token-sync daemon source=ci (pid=$$)"
LABEL=STARTUP
while true; do
    if ! sync_current_state "$LABEL" true; then
        log "[WARN] CI sync incomplete; retrying on next poll"
    fi
    LABEL=SYNC
    sleep "$POLL_INTERVAL"
done
