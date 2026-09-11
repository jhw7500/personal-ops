#!/bin/bash

# Never fall back to Claude Code's interactive login credentials.
load_ci_token() {
    local record
    if [ ! -f "$CRED_FILE" ] || [ -L "$CRED_FILE" ] ||
       [ "$(stat -c '%u:%a' "$CRED_FILE" 2>/dev/null)" != "$(id -u):600" ]; then
        log "[ERROR] CI token source must be a current-user-owned regular file with mode 0600: $CRED_FILE"
        return 1
    fi
    record=$(jq -ers '
        select(length == 1) | .[0] |
        select(.source == "claude-setup-token") |
        select(.accessToken | type == "string") |
        select(.accessToken | test("^sk-ant-oat01-[A-Za-z0-9_-]{20,}\\z")) |
        select(.expiresAt | type == "number") |
        select(.expiresAt > 0 and .expiresAt < 9007199254740991 and
               (.expiresAt | floor) == .expiresAt) |
        [.accessToken, .expiresAt] | @tsv
    ' "$CRED_FILE" 2>/dev/null) || {
        log "[ERROR] CI token source is invalid: $CRED_FILE"
        return 1
    }
    IFS=$'\t' read -r CURRENT_TOKEN CURRENT_EXPIRES <<< "$record"
    if [ "$CURRENT_EXPIRES" -le "$(($(date +%s) * 1000))" ]; then
        log "[ERROR] CI token has reached its renewal deadline; run claude setup-token and import a replacement"
        return 1
    fi
}

load_repos() {
    if [ ! -f "$REPO_FILE" ]; then
        log "[ERROR] repository config missing: $REPO_FILE"
        return 1
    fi

    mapfile -t REPOS < <(grep -vE '^[[:space:]]*(#|$)' "$REPO_FILE")
    if [ "${#REPOS[@]}" -eq 0 ]; then
        log "[ERROR] repository config is empty: $REPO_FILE"
        return 1
    fi

    local repo
    for repo in "${REPOS[@]}"; do
        if ! [[ "$repo" =~ ^[A-Za-z0-9._-]+$ ]]; then
            log "[ERROR] invalid repository name in $REPO_FILE"
            return 1
        fi
    done
}

token_id() {
    printf '%s' "$1" | sha256sum | awk '{print substr($1, 1, 12)}'
}

sync_state_sha() {
    local token="$1"
    {
        printf '%s\0' "$token"
        printf '%s\0' "${REPOS[@]}"
    } | sha256sum | awk '{print $1}'
}

write_sync_marker() {
    local state_sha="$1"
    local temp_file="${SHA_FILE}.tmp.$$"
    umask 077
    if ! printf '%s' "$state_sha" > "$temp_file"; then
        return 1
    fi
    if ! mv -f "$temp_file" "$SHA_FILE"; then
        unlink "$temp_file" 2>/dev/null || true
        return 1
    fi
}

sync_loaded_repos() {
    local token="$1"
    local label="$2"
    local fail=0
    local repo

    for repo in "${REPOS[@]}"; do
        if printf '%s' "$token" | gh secret set CLAUDE_CODE_OAUTH_TOKEN \
            --repo "jhw7500/$repo" 2>/dev/null; then
            log "[$label] $repo OK"
        else
            log "[$label] $repo FAIL"
            fail=$((fail + 1))
        fi
    done
    log "[$label] completed (${#REPOS[@]} repos, $fail failures)"

    if [ "$fail" -ne 0 ]; then
        return 1
    fi
    write_sync_marker "$(sync_state_sha "$token")"
}

sync_current_state() {
    local label="$1"
    local skip_if_current="${2:-false}"
    local lock_fd token current_sha previous_sha expires_date rc

    umask 077
    if ! exec {lock_fd}> "$LOCK_FILE"; then
        log "[$label] unable to open sync lock: $LOCK_FILE"
        return 1
    fi
    if ! flock -x "$lock_fd"; then
        log "[$label] unable to acquire sync lock: $LOCK_FILE"
        exec {lock_fd}>&-
        return 1
    fi

    # Validate before comparing markers: an unchanged token can have expired.
    if ! load_ci_token; then
        flock -u "$lock_fd"
        exec {lock_fd}>&-
        return 1
    fi
    token="$CURRENT_TOKEN"
    if [ "$label" = HEALTH-SYNC ] &&
       [ "$CURRENT_EXPIRES" -le "$((($(date +%s) + 14 * 86400) * 1000))" ]; then
        log "[WARN] CI token renewal deadline is within 14 days; issue and import a replacement"
    fi
    if ! load_repos; then
        flock -u "$lock_fd"
        exec {lock_fd}>&-
        return 1
    fi

    current_sha=$(sync_state_sha "$token")
    previous_sha=$(cat "$SHA_FILE" 2>/dev/null || echo "")
    if [ "$skip_if_current" = true ] && [ "$current_sha" = "$previous_sha" ]; then
        # shellcheck disable=SC2034 # output variable consumed by sourcing caller
        SYNCED_TOKEN="$token"
        flock -u "$lock_fd"
        exec {lock_fd}>&-
        return 0
    fi

    expires_date=$(date -d @$((CURRENT_EXPIRES / 1000)) '+%Y-%m-%d %H:%M:%S')
    log "[$label] source=ci sync state changed token_id=$(token_id "$token") renew_by=${expires_date}"

    if sync_loaded_repos "$token" "$label"; then
        # shellcheck disable=SC2034 # output variable consumed by sourcing caller
        SYNCED_TOKEN="$token"
        rc=0
    else
        rc=1
    fi
    flock -u "$lock_fd"
    exec {lock_fd}>&-
    return "$rc"
}
