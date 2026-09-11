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
sha256sum | cut -d ' ' -f 1 >> "$HOME/gh.token-shas"
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
    local case_home="$1" token="$2"
    printf '{"source":"claude-setup-token","accessToken":"%s","expiresAt":1900000000000}\n' "$token" \
        > "$case_home/.claude/.ci-oauth-token.json"
    chmod 600 "$case_home/.claude/.ci-oauth-token.json"
}

IFS='|' read -r HOME_A BIN_A < <(make_home daemon)
printf '%s\n' repo-a > "$HOME_A/.claude/.token_sync_repos"
write_credentials "$HOME_A" 'sk-ant-oat01-TEST_A_abcdefghijklmnopqrstuvwxyz'

cat > "$BIN_A/sleep" <<'SH'
#!/bin/bash
count_file="$HOME/sleep.count"
count=$(cat "$count_file" 2>/dev/null || printf 0)
if [ "$count" -eq 0 ]; then
    printf 1 > "$count_file"
    printf '%s\n' repo-b >> "$HOME/.claude/.token_sync_repos"
    python3 - "$HOME/.claude/.ci-oauth-token.json" <<'PY'
import json,sys
p=sys.argv[1]
d=json.load(open(p))
d['accessToken']='sk-ant-oat01-TEST_B_abcdefghijklmnopqrstuvwxyz'
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

if grep -q 'sk-ant-oat01-TEST_[AB]_' "$HOME_A/.claude/token_sync.log" 2>/dev/null; then
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
write_credentials "$HOME_B" 'sk-ant-oat01-TEST_C_abcdefghijklmnopqrstuvwxyz'
HOME="$HOME_B" PATH="$BIN_B:$PATH" GH_FAIL_REPO=repo-b \
    bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
partial_rc=$?
if [ "$partial_rc" -ne 0 ] && [ ! -e "$HOME_B/.claude/.token_sync_health.sha" ]; then
    pass 'partial failure is observable and does not advance marker'
else
    fail 'partial failure is observable and does not advance marker'
fi

IFS='|' read -r HOME_C BIN_C < <(make_home missing-config)
write_credentials "$HOME_C" 'sk-ant-oat01-TEST_D_abcdefghijklmnopqrstuvwxyz'
HOME="$HOME_C" PATH="$BIN_C:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
missing_rc=$?
if [ "$missing_rc" -ne 0 ]; then
    pass 'missing repository config fails closed'
else
    fail 'missing repository config fails closed'
fi

IFS='|' read -r HOME_D BIN_D < <(make_home startup)
printf '%s\n' repo-a > "$HOME_D/.claude/.token_sync_repos"
write_credentials "$HOME_D" 'sk-ant-oat01-TEST_E_abcdefghijklmnopqrstuvwxyz'
HOME="$HOME_D" PATH="$BIN_D:$PATH" /usr/bin/timeout 0.25 \
    bash "$MODULE_DIR/bin/claude-token-sync.sh" >/dev/null 2>&1 || true
if grep -q 'jhw7500/repo-a' "$HOME_D/gh.calls" 2>/dev/null; then
    pass 'daemon startup applies an unsynced current state'
else
    fail 'daemon startup applies an unsynced current state'
fi

IFS='|' read -r HOME_E BIN_E < <(make_home concurrent)
printf '%s\n' repo-a > "$HOME_E/.claude/.token_sync_repos"
write_credentials "$HOME_E" 'sk-ant-oat01-TEST_F_abcdefghijklmnopqrstuvwxyz'
cat > "$BIN_E/gh" <<'SH'
#!/bin/bash
cat >/dev/null
if ! mkdir "$HOME/gh.active" 2>/dev/null; then
    printf 'overlap\n' >> "$HOME/gh.overlap"
fi
printf '%s\n' "$*" >> "$HOME/gh.calls"
/bin/sleep 0.12
rmdir "$HOME/gh.active" 2>/dev/null || true
SH
chmod +x "$BIN_E/gh"
HOME="$HOME_E" PATH="$BIN_E:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh" &
health_one=$!
HOME="$HOME_E" PATH="$BIN_E:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh" &
health_two=$!
wait "$health_one"; concurrent_one_rc=$?
wait "$health_two"; concurrent_two_rc=$?
concurrent_calls=$(wc -l < "$HOME_E/gh.calls" 2>/dev/null || printf 0)
if [ "$concurrent_one_rc" -eq 0 ] && [ "$concurrent_two_rc" -eq 0 ] && \
   [ "$concurrent_calls" -eq 1 ] && [ ! -e "$HOME_E/gh.overlap" ]; then
    pass 'concurrent health runs serialize and recheck state under lock'
else
    fail 'concurrent health runs serialize and recheck state under lock'
fi

missing_helper_dir="$TMP_ROOT/missing-helper"
mkdir -p "$missing_helper_dir"
cp "$MODULE_DIR/bin/claude-token-sync.sh" "$missing_helper_dir/daemon.sh"
HOME="$HOME_D" PATH="$BIN_D:$PATH" /usr/bin/timeout 0.2 \
    bash "$missing_helper_dir/daemon.sh" >/dev/null 2>&1
missing_helper_rc=$?
if [ "$missing_helper_rc" -ne 0 ] && [ "$missing_helper_rc" -ne 124 ]; then
    pass 'daemon exits when common helper cannot be loaded'
else
    fail 'daemon exits when common helper cannot be loaded'
fi


# The CLI login must never replace the dedicated CI token, even after rotation.
IFS='|' read -r HOME_F BIN_F < <(make_home ci-isolation)
printf '%s\n' repo-a > "$HOME_F/.claude/.token_sync_repos"
write_credentials "$HOME_F" 'sk-ant-oat01-TEST_CI_abcdefghijklmnopqrstuvwxyz'
printf '{"claudeAiOauth":{"accessToken":"SHORT_SESSION_TOKEN","expiresAt":1900000000000}}\n' > "$HOME_F/.claude/.credentials.json"
HOME="$HOME_F" PATH="$BIN_F:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
isolation_rc=$?
expected_sha=$(printf '%s' 'sk-ant-oat01-TEST_CI_abcdefghijklmnopqrstuvwxyz' | sha256sum | cut -d ' ' -f 1)
if [ "$isolation_rc" -eq 0 ] && [ "$(cat "$HOME_F/gh.token-shas" 2>/dev/null)" = "$expected_sha" ]; then
    pass 'health distributes only the dedicated CI token'
else
    fail 'health distributes only the dedicated CI token'
fi

# A matching marker must not hide an expired, corrupt, or exposed token file.
for invalid_case in expired malformed insecure symlink missing multi_json; do
    write_credentials "$HOME_F" 'sk-ant-oat01-TEST_CI_abcdefghijklmnopqrstuvwxyz'
    source_file="$HOME_F/.claude/.ci-oauth-token.json"
    case "$invalid_case" in
        expired) sed -i 's/1900000000000/1000/' "$source_file" ;;
        malformed) printf 'invalid JSON\n' > "$source_file" ;;
        multi_json) cp "$source_file" "$source_file.copy"; cat "$source_file.copy" >> "$source_file" ;;
        insecure) chmod 644 "$source_file" ;;
        symlink) mv "$source_file" "$source_file.target"; ln -s "$source_file.target" "$source_file" ;;
        missing) unlink "$source_file" ;;
    esac
    before=$(wc -l < "$HOME_F/gh.calls" 2>/dev/null || printf 0)
    HOME="$HOME_F" PATH="$BIN_F:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
    invalid_rc=$?
    after=$(wc -l < "$HOME_F/gh.calls" 2>/dev/null || printf 0)
    if [ "$invalid_rc" -ne 0 ] && [ "$before" -eq "$after" ]; then
        pass "$invalid_case CI source fails without falling back to CLI credentials"
    else
        fail "$invalid_case CI source fails without falling back to CLI credentials"
    fi
    [ ! -L "$source_file" ] || unlink "$source_file"
done

# An unchanged token still needs an approaching-deadline warning.
write_credentials "$HOME_F" 'sk-ant-oat01-TEST_CI_abcdefghijklmnopqrstuvwxyz'
python3 - "$HOME_F/.claude/.ci-oauth-token.json" <<'PY'
import json, sys, time
path = sys.argv[1]
with open(path) as source:
    record = json.load(source)
record['expiresAt'] = int((time.time() + 7 * 86400) * 1000)
with open(path, 'w') as destination:
    json.dump(record, destination)
PY
before=$(wc -l < "$HOME_F/gh.calls")
HOME="$HOME_F" PATH="$BIN_F:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
warning_rc=$?
if [ "$warning_rc" -eq 0 ] && [ "$before" -eq "$(wc -l < "$HOME_F/gh.calls")" ] &&
    grep -q 'renewal deadline is within 14 days' "$HOME_F/.claude/token_sync.log"; then
    pass 'near-deadline warning is emitted even when sync marker matches'
else
    fail 'near-deadline warning is emitted even when sync marker matches'
fi

# Import consumes stdin, publishes a private file, and leaves it intact on error.
IFS='|' read -r HOME_G _ < <(make_home import)
if printf '%s\n' 'sk-ant-oat01-TEST_IMPORTED_abcdefghijklmnopqrstuvwxyz' | \
    HOME="$HOME_G" bash "$MODULE_DIR/bin/claude-token-sync-set-token.sh" --expires-at 2030-01-01T00:00:00Z >/dev/null 2>&1 &&
    [ "$(stat -c %a "$HOME_G/.claude/.ci-oauth-token.json" 2>/dev/null)" = 600 ]; then
    pass 'import publishes CI token with mode 0600'
else
    fail 'import publishes CI token with mode 0600'
fi
before=$(sha256sum "$HOME_G/.claude/.ci-oauth-token.json" 2>/dev/null || true)
if printf '%s\n' 'invalid-token' | HOME="$HOME_G" bash "$MODULE_DIR/bin/claude-token-sync-set-token.sh" --expires-at 2030-01-01T00:00:00Z >/dev/null 2>&1; then
    fail 'invalid import preserves existing token'
elif [ -n "$before" ] && [ "$before" = "$(sha256sum "$HOME_G/.claude/.ci-oauth-token.json" 2>/dev/null || true)" ]; then
    pass 'invalid import preserves existing token'
else
    fail 'invalid import preserves existing token'
fi

if printf '%s\n' 'sk-ant-oat01-TEST_IMPORTED_abcdefghijklmnopqrstuvwxyz' | HOME="$HOME_G" bash "$MODULE_DIR/bin/claude-token-sync-set-token.sh" --expires-at 2000-01-01T00:00:00Z >/dev/null 2>&1; then
    fail 'expired import preserves existing token'
elif [ "$before" = "$(sha256sum "$HOME_G/.claude/.ci-oauth-token.json" 2>/dev/null || true)" ]; then
    pass 'expired import preserves existing token'
else
    fail 'expired import preserves existing token'
fi

if printf '%s\n\n' 'sk-ant-oat01-TEST_IMPORTED_abcdefghijklmnopqrstuvwxyz' | HOME="$HOME_G" bash "$MODULE_DIR/bin/claude-token-sync-set-token.sh" --expires-at 2030-01-01T00:00:00Z >/dev/null 2>&1; then
    fail 'multiline token import is rejected'
else
    pass 'multiline token import is rejected'
fi

# Installation must keep the sourced helper non-executable while enabling entrypoints.
IFS='|' read -r HOME_INSTALL BIN_INSTALL < <(make_home install-mode)
INSTALL_MODULE="$TMP_ROOT/install-mode/module"
mkdir -p "$INSTALL_MODULE"
cp -R "$MODULE_DIR"/. "$INSTALL_MODULE"/
chmod 644 "$INSTALL_MODULE"/bin/*.sh
write_credentials "$HOME_INSTALL" 'sk-ant-oat01-TEST_INSTALL_abcdefghijklmnopqrstuvwxyz'
cat > "$BIN_INSTALL/systemctl" <<'SH'
#!/bin/bash
if [ "${1:-}" = --user ]; then
    shift
fi
if [ "${1:-}" = is-active ]; then
    if [ "${2:-}" = claude-token-sync-health.timer ]; then
        printf 'active\n'
    else
        printf 'inactive\n'
    fi
fi
SH
chmod +x "$BIN_INSTALL/systemctl"
if HOME="$HOME_INSTALL" PATH="$BIN_INSTALL:$PATH" bash "$INSTALL_MODULE/install.sh" >/dev/null 2>&1 &&
    [ "$(stat -c %a "$INSTALL_MODULE/bin/claude-token-sync-common.sh")" = 644 ] &&
    [ "$(stat -c %a "$INSTALL_MODULE/bin/claude-token-sync.sh")" = 755 ] &&
    [ "$(stat -c %a "$INSTALL_MODULE/bin/claude-token-sync-health.sh")" = 755 ] &&
    [ "$(stat -c %a "$INSTALL_MODULE/bin/claude-token-sync-set-token.sh")" = 755 ]; then
    pass 'installation preserves helper mode and enables entrypoint modes'
else
    fail 'installation preserves helper mode and enables entrypoint modes'
fi

IFS='|' read -r HOME_H BIN_H < <(make_home install-preflight)
mkdir -p "$HOME_H/.local/bin"
printf '%s\n' existing-installation > "$HOME_H/.local/bin/claude-token-sync.sh"
cat > "$BIN_H/systemctl" <<'SH'
#!/bin/bash
printf 'called\n' >> "$HOME/systemctl.calls"
SH
chmod +x "$BIN_H/systemctl"
if HOME="$HOME_H" PATH="$BIN_H:$PATH" bash "$MODULE_DIR/install.sh" >/dev/null 2>&1; then
    fail 'installation rejects a missing CI source before changing runtime'
elif [ ! -f "$HOME_H/systemctl.calls" ] && [ ! -L "$HOME_H/.local/bin/claude-token-sync.sh" ] &&
    [ "$(cat "$HOME_H/.local/bin/claude-token-sync.sh")" = existing-installation ]; then
    pass 'installation rejects a missing CI source before changing runtime'
else
    fail 'installation rejects a missing CI source before changing runtime'
fi

# A periodic check must distribute a new token without reviving a stopped daemon.
IFS='|' read -r HOME_I BIN_I < <(make_home timer-only)
printf '%s\n' repo-a > "$HOME_I/.claude/.token_sync_repos"
write_credentials "$HOME_I" 'sk-ant-oat01-TEST_TIMER_abcdefghijklmnopqrstuvwxyz'
cat > "$BIN_I/pgrep" <<'SH'
#!/bin/bash
[ -f "$HOME/daemon.running" ]
SH
cat > "$BIN_I/systemctl" <<'SH'
#!/bin/bash
touch "$HOME/daemon.running"
SH
chmod +x "$BIN_I/pgrep" "$BIN_I/systemctl"
HOME="$HOME_I" PATH="$BIN_I:$PATH" bash "$MODULE_DIR/bin/claude-token-sync-health.sh"
timer_rc=$?
if [ "$timer_rc" -eq 0 ] && [ -s "$HOME_I/.claude/.token_sync_health.sha" ] &&
    grep -q 'jhw7500/repo-a' "$HOME_I/gh.calls" && [ ! -f "$HOME_I/daemon.running" ]; then
    pass 'periodic health sync works while leaving the daemon stopped'
else
    fail 'periodic health sync works while leaving the daemon stopped'
fi

if [ "$FAILURES" -ne 0 ]; then
    printf 'FAILED %d check(s)\n' "$FAILURES"
    exit 1
fi
printf 'ALL TOKEN SYNC TESTS PASSED\n'
