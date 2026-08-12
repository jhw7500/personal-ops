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

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
# shellcheck source=bin/claude-token-sync-common.sh
source "$(dirname "$SCRIPT_PATH")/claude-token-sync-common.sh"

# 1) 데몬 생존 확인 — 죽었으면 재시작
if ! pgrep -f 'claude-token-sync\.sh' >/dev/null 2>&1; then
    log "[HEALTH] daemon not running -> restart"
    systemctl --user restart claude-token-sync 2>/dev/null
fi

# 2) credentials/토큰 존재 확인
[ -f "$CRED_FILE" ] || { log "[HEALTH] cred file missing, skip"; exit 0; }
TOK=$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null)
[ -n "$TOK" ] || { log "[HEALTH] token empty, skip"; exit 0; }

load_repos || exit 1
CUR_SHA=$(sync_state_sha "$TOK")
PREV_SHA=$(cat "$SHA_FILE" 2>/dev/null || echo "")

# 이미 최신이면 조용히 종료 (no-op)
[ "$CUR_SHA" = "$PREV_SHA" ] && exit 0

EXPIRES=$(jq -r '.claudeAiOauth.expiresAt // 0' "$CRED_FILE" 2>/dev/null)
EXPIRES_DATE=$(date -d @$((EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
log "[HEALTH-SYNC] sync state changed token_id=$(token_id "$TOK") expires=${EXPIRES_DATE}"

sync_repos "$TOK" HEALTH-SYNC
