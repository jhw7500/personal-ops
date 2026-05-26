#!/bin/bash
# Claude OAuth accessToken → GitHub 레포 시크릿 자동 동기화 데몬
# credentials 파일이 없으면 60초 간격으로 대기 후 재시도
# 파일 존재 시 inotifywait로 변경 감지하여 즉시 동기화

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

# Claude Code가 credentials를 atomic rename(tempfile→rename)으로 갱신하면 inode가
# 바뀌어 단일 파일 watch가 끊긴다(2026-05 무음 정지 사례). 디렉터리를 watch하고
# .credentials.json 이벤트만 필터해서 atomic rename도 잡는다.
CRED_DIR_WATCH="$(dirname "$CRED_FILE")"
CRED_NAME="$(basename "$CRED_FILE")"

while true; do
    # close_write: 일반 write, moved_to: atomic rename으로 들어옴, create: 새로 생성.
    # -t 600: 타임아웃이어도 다음 루프에서 토큰 sha 비교로 변경 판정(놓친 케이스 안전망).
    inotifywait -qq --include "^${CRED_NAME}$" \
        -e close_write,moved_to,create -t 600 \
        "$CRED_DIR_WATCH" 2>/dev/null || true

    if [ ! -f "$CRED_FILE" ]; then
        log "[WARN] $CRED_FILE disappeared, waiting..."
        while [ ! -f "$CRED_FILE" ]; do sleep "$WAIT_INTERVAL"; done
        log "[RECOVERED] $CRED_FILE found again"
        continue
    fi

    sleep 0.5
    CUR_TOKEN=$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null) || continue
    if [ -n "$CUR_TOKEN" ] && [ "$CUR_TOKEN" != "$PREV_TOKEN" ]; then
        EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null)
        EXPIRES_DATE=$(date -d @$((EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
        log "[CHANGED] new_token=${CUR_TOKEN:0:20}... expires=${EXPIRES_DATE}"
        sync_repos "$CUR_TOKEN"
        PREV_TOKEN="$CUR_TOKEN"
    fi
done
