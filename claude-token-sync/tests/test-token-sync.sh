#!/bin/bash
set -uo pipefail

MODULE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'find "$TMP_ROOT" -depth -delete' EXIT
FAILURES=0

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

make_home() {
    local name="$1"
    local home="$TMP_ROOT/$name/home"
    local bin="$TMP_ROOT/$name/bin"
    mkdir -p "$home/.claude" "$bin"
    cat > "$bin/gh" <<'SH'
#!/bin/bash
cat >/dev/null
printf '%s\n' "$*" >> "$HOME/gh.calls"
if [ -n "${GH_FAIL_REPO:-}" ] && [[ " $* " == *" jhw7500/$GH_FAIL_REPO "* ]]; then
    exit 1
fi
SH
    cat > "$bin/pgrep" <<'SH'
#!/bin/bash
exit 0
SH
    chmod +x "$bin/gh" "$bin/pgrep"
    printf '%s\n' "$home|$bin"
}

write_credentials() {
    local home="$1" token="$2"
    printf '{"claudeAiOauth":{"accessToken":"%s","expiresAt":1900000000000}}\n' "$token" \
        > "$home/.claude/.credentials.json"
}

IFS='|' read -r HOME_A BIN_A < <(make_home daemon)
printf '%s\n' repo-a > "$HOME_A/.claude/.token_sync_repos"
write_credentials "$HOME_A" 'TEST_TOKEN_A_abcdefghijklmnopqrstuvwxyz'

cat > "$BIN_A/sleep" <<'SH'
#!/bin/bash
count_file="$HOME/sleep.count"
count=$(cat "$count_file" 2>/dev/null || printf 0)
if [ "$count" -eq 0 ]; then
    printf 1 > "$count_file"
    printf '%s\n' repo-b >> "$HOME/.claude/.token_sync_repos"
    python3 - "$HOME/.claude/.credentials.json" <<'PY'
import json,sys
p=sys.argv[1]
d=json.load(open(p))
d['claudeAiOauth']['accessToken']='TEST_TOKEN_B_abcdefghijklmnopqrstuvwxyz'
json.dump(d,open(p,'w'))
PY
fi
/bin/sleep 0.03
SH
chmod +x "$BIN_A/sleep"

HOME="$HOME_A" PATH="$BIN_A:$PATH" /usr/bin/timeout 0.5 \
    bash "$MODULE_DIR/bin/claude-token-sync.sh" >/dev/null 2>&1 || true

if grep -q 'jhw7500/repo-a' "$HOME_A/gh.calls" 2>/dev/null && \
   grep -q 'jhw7500/repo-b' "$HOME_A/gh.calls" 2>/dev/null; then
    pass 'daemon reloads repository list before sync'
else
    fail 'daemon reloads repository list before sync'
fi

if [ -s "$HOME_A/.claude/.token_sync_health.sha" ]; then
    pass 'daemon advances success marker'
else
    fail 'daemon advances success marker'
fi

if grep -q 'TEST_TOKEN_[AB]_' "$HOME_A/.claude/token_sync.log" 2>/dev/null; then
    fail 'logs redact token prefixes'
else
    pass 'logs redact token prefixes'
fi

before=$(wc -l < "$HOME_A/gh.calls" 2>/dev/null || printf 0)
HOME="$HOME_A" PATH="$BIN_A:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
health_rc=$?
after=$(wc -l < "$HOME_A/gh.calls" 2>/dev/null || printf 0)
if [ "$health_rc" -eq 0 ] && [ "$before" -eq "$after" ]; then
    pass 'health skips token already synced by daemon'
else
    fail 'health skips token already synced by daemon'
fi

printf '%s\n' repo-c >> "$HOME_A/.claude/.token_sync_repos"
before=$(wc -l < "$HOME_A/gh.calls" 2>/dev/null || printf 0)
HOME="$HOME_A" PATH="$BIN_A:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
repo_change_rc=$?
after=$(wc -l < "$HOME_A/gh.calls" 2>/dev/null || printf 0)
if [ "$repo_change_rc" -eq 0 ] && [ $((after - before)) -eq 3 ] && \
   tail -3 "$HOME_A/gh.calls" | grep -q 'jhw7500/repo-c'; then
    pass 'health syncs when repository list changes'
else
    fail 'health syncs when repository list changes'
fi

IFS='|' read -r HOME_B BIN_B < <(make_home partial)
printf '%s\n' repo-a repo-b > "$HOME_B/.claude/.token_sync_repos"
write_credentials "$HOME_B" 'TEST_TOKEN_C_abcdefghijklmnopqrstuvwxyz'
HOME="$HOME_B" PATH="$BIN_B:$PATH" GH_FAIL_REPO=repo-b \
    bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
partial_rc=$?
if [ "$partial_rc" -ne 0 ] && [ ! -e "$HOME_B/.claude/.token_sync_health.sha" ]; then
    pass 'partial failure is observable and does not advance marker'
else
    fail 'partial failure is observable and does not advance marker'
fi

IFS='|' read -r HOME_C BIN_C < <(make_home missing-config)
write_credentials "$HOME_C" 'TEST_TOKEN_D_abcdefghijklmnopqrstuvwxyz'
HOME="$HOME_C" PATH="$BIN_C:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
missing_rc=$?
if [ "$missing_rc" -ne 0 ]; then
    pass 'missing repository config fails closed'
else
    fail 'missing repository config fails closed'
fi

if [ "$FAILURES" -ne 0 ]; then
    printf 'FAILED %d check(s)\n' "$FAILURES"
    exit 1
fi
printf 'ALL TOKEN SYNC TESTS PASSED\n'
