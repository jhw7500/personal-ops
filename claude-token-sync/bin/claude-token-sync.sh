#!/bin/bash
# Claude OAuth accessToken → GitHub 레포 시크릿 자동 동기화 데몬
# credentials 파일이 없으면 60초 간격으로 대기 후 재시도
# 파일 존재 시 30초마다 access token 값을 읽어 변경 감지 (inotify는 신뢰 불가)

# Fine-grained PAT 대신 gh auth hosts.yml 토큰(repo/workflow scope) 사용
unset GITHUB_TOKEN

CRED_FILE="$HOME/.claude/.credentials.json"
LOG_FILE="$HOME/.claude/token_sync.log"
# shellcheck disable=SC2034 # consumed by sourced common helper
REPO_FILE="$HOME/.claude/.token_sync_repos"
# shellcheck disable=SC2034 # consumed by sourced common helper
SHA_FILE="$HOME/.claude/.token_sync_health.sha"
# shellcheck disable=SC2034 # consumed by sourced common helper
LOCK_FILE="$HOME/.claude/.token_sync.lock"
WAIT_INTERVAL=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
# shellcheck source=bin/claude-token-sync-common.sh
# shellcheck disable=SC1091 # runtime path is resolved from the installed symlink
if ! source "$(dirname "$SCRIPT_PATH")/claude-token-sync-common.sh"; then
    log "[ERROR] common helper could not be loaded"
    exit 1
fi

log "[START] claude-token-sync daemon (pid=$$)"

# Phase 1: credentials 파일이 생길 때까지 대기
while [ ! -f "$CRED_FILE" ]; do
    log "[WAIT] $CRED_FILE not found, retrying in ${WAIT_INTERVAL}s"
    sleep "$WAIT_INTERVAL"
done

# Phase 2: 유효한 accessToken이 나타날 때까지 대기
PREV_TOKEN=""
while true; do
    PREV_TOKEN=$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null)
    if [ -n "$PREV_TOKEN" ]; then
        break
    fi
    log "[WAIT] accessToken empty, retrying in ${WAIT_INTERVAL}s"
    sleep "$WAIT_INTERVAL"
done

EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null)
EXPIRES_DATE=$(date -d @$((EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
log "[READY] token_id=$(token_id "$PREV_TOKEN") expires=${EXPIRES_DATE}"

# Reconcile current token+repo state on startup. This is normally a marker
# no-op, but makes a daemon restart apply repository-list changes immediately.
if sync_current_state STARTUP true; then
    PREV_TOKEN="$SYNCED_TOKEN"
else
    PREV_TOKEN=""
    log "[WARN] startup sync incomplete; retrying on next poll"
fi

# Claude Code의 credentials atomic rename이 inotify 이벤트로 안정적으로 도착하지
# 않아(2026-05 측정: --include + create/close_write/moved_to/modify 다 걸어도 0건),
# 주기적 token 값 비교로 단순화.
# 30초 폴링 → 헬스(10분 백스톱) 대비 20배 빠르고 즉시성도 충분.
POLL_INTERVAL="${CLAUDE_TOKEN_SYNC_POLL_INTERVAL:-30}"

while true; do
    sleep "$POLL_INTERVAL"

    if [ ! -f "$CRED_FILE" ]; then
        log "[WARN] $CRED_FILE disappeared, waiting..."
        while [ ! -f "$CRED_FILE" ]; do sleep "$WAIT_INTERVAL"; done
        log "[RECOVERED] $CRED_FILE found again"
        continue
    fi

    CUR_TOKEN=$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null) || continue
    if [ -n "$CUR_TOKEN" ] && [ "$CUR_TOKEN" != "$PREV_TOKEN" ]; then
        if sync_current_state SYNC false; then
            PREV_TOKEN="$SYNCED_TOKEN"
        else
            log "[WARN] sync incomplete; retrying on next poll"
        fi
    fi
done
