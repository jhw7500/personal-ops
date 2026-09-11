#!/bin/bash
# CI 토큰 만료/권한 확인, 14일 전 경고, 마지막 성공 상태와 다르면 동기화.
# 상시 데몬 없이 단독 실행하며, 중지된 데몬을 다시 시작하지 않는다.
# systemd user timer가 주기적으로 호출한다 (claude-token-sync-health.timer).

# Fine-grained PAT 대신 gh auth hosts.yml 토큰 사용
unset GITHUB_TOKEN

# shellcheck disable=SC2034 # consumed by sourced common helper
CRED_FILE="$HOME/.claude/.ci-oauth-token.json"
LOG_FILE="$HOME/.claude/token_sync.log"
# shellcheck disable=SC2034 # consumed by sourced common helper
SHA_FILE="$HOME/.claude/.token_sync_health.sha"
# shellcheck disable=SC2034 # consumed by sourced common helper
REPO_FILE="$HOME/.claude/.token_sync_repos"
# shellcheck disable=SC2034 # consumed by sourced common helper
LOCK_FILE="$HOME/.claude/.token_sync.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
# shellcheck source=bin/claude-token-sync-common.sh
# shellcheck disable=SC1091 # runtime path is resolved from the installed symlink
if ! source "$(dirname "$SCRIPT_PATH")/claude-token-sync-common.sh"; then
    log "[ERROR] common helper could not be loaded"
    exit 1
fi

# Missing/expired CI credentials fail even if the marker matches.
sync_current_state HEALTH-SYNC true
