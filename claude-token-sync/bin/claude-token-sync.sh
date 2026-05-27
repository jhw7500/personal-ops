#!/bin/bash
# Claude OAuth accessToken → GitHub 레포 시크릿 자동 동기화 데몬
# credentials 파일이 없으면 60초 간격으로 대기 후 재시도
# 파일 존재 시 30초마다 stat 폴링으로 토큰 변경 감지 (inotify는 신뢰 불가)

# Fine-grained PAT 대신 gh auth hosts.yml 토큰(repo/workflow scope) 사용
unset GITHUB_TOKEN

CRED_FILE="$HOME/.claude/.credentials.json"
LOG_FILE="$HOME/.claude/token_sync.log"
REPO_FILE="$HOME/.claude/.token_sync_repos"
# 대상 레포는 공유 파일(.token_sync_repos)을 단일 소스로 사용. 헬스체크와 동일 목록 보장.
if [ -f "$REPO_FILE" ]; then
    mapfile -t REPOS < <(grep -vE '^[[:space:]]*(#|$)' "$REPO_FILE")
else
    REPOS=(
        wlan-package wlan-driver max9296 gstApp wlan-bridge streamApp automation
        cts-email-mcp-server cts-ta-mcp-server cts-ta-webapp
        pim-check redmine sc16is7xx wpa-supplicant pim-package-jhw
    )
fi
WAIT_INTERVAL=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

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
log "[READY] token=${PREV_TOKEN:0:20}... expires=${EXPIRES_DATE}"

# Phase 3: 변경 감지 및 동기화
sync_repos() {
    local token="$1"
    local fail=0
    for repo in "${REPOS[@]}"; do
        if echo "$token" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "jhw7500/$repo" 2>/dev/null; then
            log "[SYNC] $repo OK"
        else
            log "[SYNC] $repo FAIL"
            fail=$((fail + 1))
        fi
    done
    log "[SYNC] completed (${#REPOS[@]} repos, $fail failures)"
}

# Claude Code의 credentials atomic rename이 inotify 이벤트로 안정적으로 도착하지
# 않아(2026-05 측정: --include + create/close_write/moved_to/modify 다 걸어도 0건),
# stat 폴링으로 단순화. inode/mtime 변화는 항상 잡힌다.
# 30초 폴링 → 헬스(10분 백스톱) 대비 20배 빠르고 즉시성도 충분.
POLL_INTERVAL=30

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
        EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null)
        EXPIRES_DATE=$(date -d @$((EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
        log "[CHANGED] new_token=${CUR_TOKEN:0:20}... expires=${EXPIRES_DATE}"
        sync_repos "$CUR_TOKEN"
        PREV_TOKEN="$CUR_TOKEN"
    fi
done
