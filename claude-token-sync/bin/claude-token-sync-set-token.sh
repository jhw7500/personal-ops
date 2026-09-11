#!/bin/bash
# Import from stdin: never place a token in arguments or terminal output.
set -euo pipefail
umask 077

if [ "$#" -ne 2 ] || [ "$1" != --expires-at ]; then
    echo "Usage: $0 --expires-at ISO_TIMESTAMP < token-file" >&2
    exit 2
fi
deadline=$(date -d "$2" +%s 2>/dev/null) || {
    echo "Invalid renewal deadline" >&2
    exit 2
}
if [ "$deadline" -le "$(date +%s)" ]; then
    echo "Renewal deadline must be in the future" >&2
    exit 2
fi

TOKEN_DIR="$HOME/.claude"
mkdir -p "$TOKEN_DIR"
DESTINATION="$TOKEN_DIR/.ci-oauth-token.json"
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
log() { printf '%s\n' "$*" >&2; }
# shellcheck source=bin/claude-token-sync-common.sh
# shellcheck disable=SC1091
source "$(dirname "$SCRIPT_PATH")/claude-token-sync-common.sh"

temp_file=$(mktemp "$DESTINATION.tmp.XXXXXX")
trap 'rm -f -- "$temp_file"' EXIT
chmod 600 "$temp_file"
jq -eRs --argjson expiresAt "$((deadline * 1000))" '
    rtrimstr("\n") |
    {source: "claude-setup-token", accessToken: ., expiresAt: $expiresAt}
' > "$temp_file"
# shellcheck disable=SC2034 # Read by load_ci_token in the sourced helper.
CRED_FILE="$temp_file"
load_ci_token

# Serialize replacement with readers and ongoing GitHub distribution.
exec {lock_fd}> "$TOKEN_DIR/.token_sync.lock"
flock -x "$lock_fd"
mv -fT -- "$temp_file" "$DESTINATION"
flock -u "$lock_fd"
exec {lock_fd}>&-
printf 'CI token imported (mode 0600); renewal deadline: %s\n' "$2"
