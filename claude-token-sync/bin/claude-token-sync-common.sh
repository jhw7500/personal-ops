#!/bin/bash

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

sync_repos() {
    local token="$1"
    local label="$2"
    local fail=0
    local repo

    load_repos || return 1
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
