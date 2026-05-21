#!/bin/bash
# claude-token-sync 헬스체크 백스톱 — 데몬이 조용히 멈추는 상황 대비
#  1) 데몬 프로세스가 죽었으면 재시작
#  2) credentials 토큰이 마지막 동기화분과 다르면(=데몬이 변경을 놓침) 강제 1회 동기화
# systemd user timer가 주기적으로 호출한다 (claude-token-sync-health.timer).

# Fine-grained PAT 대신 gh auth hosts.yml 토큰 사용
unset GITHUB_TOKEN

CRED_FILE="$HOME/.claude/.credentials.json"
LOG_FILE="$HOME/.claude/token_sync.log"
SHA_FILE="$HOME/.claude/.token_sync_health.sha"
REPO_FILE="$HOME/.claude/.token_sync_repos"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

# 1) 데몬 생존 확인 — 죽었으면 재시작
if ! pgrep -f 'claude-token-sync\.sh' >/dev/null 2>&1; then
    log "[HEALTH] daemon not running -> restart"
    systemctl --user restart claude-token-sync 2>/dev/null
fi

# 2) credentials/토큰 존재 확인
[ -f "$CRED_FILE" ] || { log "[HEALTH] cred file missing, skip"; exit 0; }
TOK=$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null)
[ -n "$TOK" ] || { log "[HEALTH] token empty, skip"; exit 0; }

CUR_SHA=$(printf '%s' "$TOK" | sha256sum | awk '{print $1}')
PREV_SHA=$(cat "$SHA_FILE" 2>/dev/null || echo "")

# 이미 최신이면 조용히 종료 (no-op)
[ "$CUR_SHA" = "$PREV_SHA" ] && exit 0

# 토큰이 마지막 동기화분과 다름 → 강제 동기화
if [ -f "$REPO_FILE" ]; then
    mapfile -t REPOS < <(grep -vE '^[[:space:]]*(#|$)' "$REPO_FILE")
else
    REPOS=(wlan-package wlan-driver max9296 gstApp wlan-bridge streamApp automation \
           cts-email-mcp-server cts-ta-mcp-server cts-ta-webapp \
           pim-check redmine sc16is7xx wpa-supplicant pim-package-jhw)
fi

EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null)
EXPIRES_DATE=$(date -d @$((EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
log "[HEALTH-SYNC] token changed (daemon missed) token=${TOK:0:20}... expires=${EXPIRES_DATE}"

fail=0
for repo in "${REPOS[@]}"; do
    if echo "$TOK" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "jhw7500/$repo" 2>/dev/null; then
        log "[HEALTH-SYNC] $repo OK"
    else
        log "[HEALTH-SYNC] $repo FAIL"
        fail=$((fail + 1))
    fi
done
log "[HEALTH-SYNC] completed (${#REPOS[@]} repos, $fail failures)"

# 전부 성공해야 마커 갱신 (부분 실패면 다음 주기에 재시도)
[ "$fail" -eq 0 ] && printf '%s' "$CUR_SHA" > "$SHA_FILE"
exit 0
