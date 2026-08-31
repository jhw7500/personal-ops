#!/bin/bash

set -uo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLI_INIT_SCRIPT="${REPO_ROOT}/cli-init/claude-init.sh"
SESSION_SUMMARY_SCRIPT="${REPO_ROOT}/session-summary/session-summary.sh"
TEST_ROOT=$(mktemp -d /tmp/personal-ops-quota-aware.XXXXXX)
STUB_CLAUDE="${TEST_ROOT}/claude-stub"
FAILURES=0

cleanup() {
    case "$TEST_ROOT" in
        /tmp/personal-ops-quota-aware.*) rm -rf -- "$TEST_ROOT" ;;
    esac
}
trap cleanup EXIT

if ! command -v bwrap >/dev/null 2>&1; then
    printf 'ERROR bwrap is required: refusing to run Claude stubs without network isolation\n' >&2
    exit 1
fi

cat > "$STUB_CLAUDE" <<'EOF'
#!/bin/bash
set -u

{
    printf 'call'
    printf '<%s>' "$@"
    printf '\n'
} >> "$CLAUDE_STUB_CALLS"

case "${CLAUDE_STUB_MODE:-success}" in
    auth)
        printf '%s\n' 'Authentication failed: please login' >&2
        exit 1
        ;;
    auth-401)
        printf '%s\n' 'Failed to authenticate. API Error: 401 Invalid authentication credentials' >&2
        exit 1
        ;;
    auth-api)
        printf '%s\n' 'authentication_error: OAuth token has expired. Please run /login' >&2
        exit 1
        ;;
    quota)
        printf '%s\n' "You've hit your usage limit" >&2
        exit 1
        ;;
    quota-weekly)
        printf '%s\n' "You've hit your weekly limit · resets 2pm (Asia/Seoul)" >&2
        exit 1
        ;;
    quota-api)
        printf '%s\n' 'rate_limit_error: Credit balance is too low' >&2
        exit 1
        ;;
    ignore-term)
        trap '' TERM
        sleep "${CLAUDE_STUB_SLEEP:-4}"
        ;;
    sleep)
        sleep "${CLAUDE_STUB_SLEEP:-3}"
        ;;
esac

if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
    printf '%s\n' '{"loggedIn":true,"authMethod":"claude.ai"}'
else
    printf '%s\n' '---' '' '## stub summary' '' '### 작업 내역' '- **stub**: 완료'
fi
EOF
chmod +x "$STUB_CLAUDE"

new_case() {
    local case_dir
    case_dir=$(mktemp -d "${TEST_ROOT}/case.XXXXXX")
    mkdir -p "${case_dir}/module/logs" "${case_dir}/module/archive" \
        "${case_dir}/module/state" "${case_dir}/tmp"
    printf '%s\n' "$case_dir"
}

sandbox_run() {
    local case_dir=$1
    local script=$2
    shift 2

    bwrap --die-with-parent --unshare-net \
        --ro-bind / / \
        --dev /dev \
        --proc /proc \
        --bind "$TEST_ROOT" /mnt \
        --bind "${case_dir}/tmp" /tmp \
        --chdir "$REPO_ROOT" \
        "$@" /bin/bash "$script"
}

run_cli_init() {
    local case_dir=$1
    local mode=${2:-success}
    local sleep_seconds=${3:-0}
    local timeout_seconds=${4:-1}
    local kill_after_seconds=${5:-1}
    local runtime_case="/mnt/${case_dir##*/}"
    local runtime_stub="/mnt/claude-stub"

    sandbox_run "$case_dir" \
        "$CLI_INIT_SCRIPT" \
        env \
        CLAUDE_STUB_CALLS="${runtime_case}/calls" \
        CLAUDE_STUB_MODE="$mode" \
        CLAUDE_STUB_SLEEP="$sleep_seconds" \
        CLAUDE_BIN="$runtime_stub" \
        CLAUDE_INIT_MODULE_DIR="${runtime_case}/module" \
        CLAUDE_INIT_LOCK_FILE="${runtime_case}/tmp/claude-init.lock" \
        CLAUDE_INIT_TIMEOUT_SECONDS="$timeout_seconds" \
        CLAUDE_INIT_TIMEOUT_KILL_AFTER_SECONDS="$kill_after_seconds"
}

run_session_summary() {
    local case_dir=$1
    local mode=${2:-success}
    local max_input_bytes=${3:-65536}
    local timeout_seconds=${4:-1}
    local sleep_seconds=${5:-3}
    local kill_after_seconds=${6:-1}
    local runtime_case="/mnt/${case_dir##*/}"
    local runtime_stub="/mnt/claude-stub"

    sandbox_run "$case_dir" \
        "$SESSION_SUMMARY_SCRIPT" \
        env \
        CLAUDE_STUB_CALLS="${runtime_case}/calls" \
        CLAUDE_STUB_MODE="$mode" \
        CLAUDE_STUB_SLEEP="$sleep_seconds" \
        CLAUDE_BIN="$runtime_stub" \
        SESSION_SUMMARY_MODULE_DIR="${runtime_case}/module" \
        SESSION_SUMMARY_LOCK_FILE="${runtime_case}/tmp/session-summary.lock" \
        SESSION_SUMMARY_MAX_INPUT_BYTES="$max_input_bytes" \
        SESSION_SUMMARY_TIMEOUT_SECONDS="$timeout_seconds" \
        SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS="$kill_after_seconds" \
        SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS="${TEST_QUOTA_BACKOFF_SECONDS:-21600}" \
        SESSION_SUMMARY_AUTH_BACKOFF_SECONDS="${TEST_AUTH_BACKOFF_SECONDS:-86400}" \
        INCLUDE_OPENCODE=0 \
        INCLUDE_CC=0 \
        INCLUDE_COMMITS=0 \
        REMOTE_HOSTS=" "
}

call_count() {
    local calls_file=$1
    if [[ -f "$calls_file" ]]; then
        grep -c '^call' "$calls_file" || true
    else
        printf '0\n'
    fi
}

assert_equals() {
    local want=$1
    local got=$2
    local context=$3
    if [[ "$want" != "$got" ]]; then
        printf '  expected %s, got %s (%s)\n' "$want" "$got" "$context" >&2
        return 1
    fi
}

assert_contains() {
    local file=$1
    local literal=$2
    if ! grep -Fq -- "$literal" "$file"; then
        printf '  missing %q in %s\n' "$literal" "$file" >&2
        return 1
    fi
}

test_cli_init_uses_non_generative_auth_status() {
    local case_dir calls
    case_dir=$(new_case)
    run_cli_init "$case_dir" >/dev/null 2>&1 || return 1
    calls="${case_dir}/calls"

    assert_equals 1 "$(call_count "$calls")" "health command count" || return 1
    assert_contains "$calls" 'call<auth><status><--json>' || return 1
    if grep -Eq -- '<(-p|--print|--model)>' "$calls"; then
        printf '  health check invoked a generative flag\n' >&2
        return 1
    fi
}

test_cli_init_serializes_overlapping_runs() {
    local case_dir first_pid
    case_dir=$(new_case)

    run_cli_init "$case_dir" sleep 1 3 >/dev/null 2>&1 &
    first_pid=$!
    for _ in $(seq 1 50); do
        [[ -s "${case_dir}/calls" ]] && break
        sleep 0.02
    done
    run_cli_init "$case_dir" success >/dev/null 2>&1 || true
    wait "$first_pid" || return 1

    assert_equals 1 "$(call_count "${case_dir}/calls")" "overlapping health runs" || return 1
    assert_contains "${case_dir}/module/logs/claude-init.log" 'skipped: already running'
}

test_cli_init_classifies_timeout_without_generation() {
    local case_dir
    case_dir=$(new_case)
    run_cli_init "$case_dir" sleep 3 >/dev/null 2>&1 || true

    assert_equals 1 "$(call_count "${case_dir}/calls")" "timed out health calls" || return 1
    assert_contains "${case_dir}/module/logs/claude-init.log" 'class=timeout' || return 1
    if grep -Eq -- '<(-p|--print|--model)>' "${case_dir}/calls"; then
        printf '  timed health check invoked a generative flag\n' >&2
        return 1
    fi
}

test_cli_init_hard_kills_term_ignoring_process() {
    local case_dir started elapsed
    case_dir=$(new_case)
    started=$(date +%s)
    run_cli_init "$case_dir" ignore-term 4 1 1 >/dev/null 2>&1 || true
    elapsed=$(( $(date +%s) - started ))

    if (( elapsed >= 4 )); then
        printf '  cli init exceeded hard timeout: %ss\n' "$elapsed" >&2
        return 1
    fi
    assert_contains "${case_dir}/module/logs/claude-init.log" 'class=timeout'
}

test_summary_uses_one_bounded_low_effort_call() {
    local case_dir calls
    case_dir=$(new_case)
    run_session_summary "$case_dir" >/dev/null 2>&1 || return 1
    calls="${case_dir}/calls"

    assert_equals 1 "$(call_count "$calls")" "summary model calls" || return 1
    assert_contains "$calls" '<--print>' || return 1
    assert_contains "$calls" '<--model><haiku>' || return 1
    assert_contains "$calls" '<--effort><low>' || return 1
    assert_contains "$calls" '<--tools><>' || return 1
    assert_contains "$calls" '<--disable-slash-commands>' || return 1
    assert_contains "$calls" '<--no-session-persistence>'
}

test_summary_serializes_overlapping_runs() {
    local case_dir first_pid
    case_dir=$(new_case)

    run_session_summary "$case_dir" sleep 65536 3 1 1 >/dev/null 2>&1 &
    first_pid=$!
    for _ in $(seq 1 50); do
        [[ -s "${case_dir}/calls" ]] && break
        sleep 0.02
    done
    run_session_summary "$case_dir" success >/dev/null 2>&1 || true
    wait "$first_pid" || return 1

    assert_equals 1 "$(call_count "${case_dir}/calls")" "overlapping summary runs" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'skipped: already running'
}

test_summary_quota_creates_six_hour_backoff() {
    local case_dir marker now until
    case_dir=$(new_case)
    now=$(date +%s)
    run_session_summary "$case_dir" quota >/dev/null 2>&1 || return 1
    run_session_summary "$case_dir" success >/dev/null 2>&1 || return 1
    marker="${case_dir}/module/state/claude-backoff"

    assert_equals 1 "$(call_count "${case_dir}/calls")" "quota backoff call count" || return 1
    assert_contains "$marker" 'reason=quota' || return 1
    until=$(awk -F= '$1 == "until_epoch" {print $2}' "$marker")
    if (( until < now + 21590 || until > now + 21630 )); then
        printf '  quota backoff is not six hours: %s\n' "$until" >&2
        return 1
    fi
}

test_summary_auth_creates_twenty_four_hour_backoff() {
    local case_dir marker now until
    case_dir=$(new_case)
    now=$(date +%s)
    run_session_summary "$case_dir" auth >/dev/null 2>&1 || return 1
    run_session_summary "$case_dir" success >/dev/null 2>&1 || return 1
    marker="${case_dir}/module/state/claude-backoff"

    assert_equals 1 "$(call_count "${case_dir}/calls")" "auth backoff call count" || return 1
    assert_contains "$marker" 'reason=auth' || return 1
    until=$(awk -F= '$1 == "until_epoch" {print $2}' "$marker")
    if (( until < now + 86390 || until > now + 86430 )); then
        printf '  auth backoff is not twenty-four hours: %s\n' "$until" >&2
        return 1
    fi
}

test_summary_classifies_real_quota_error_corpus() {
    local mode case_dir marker
    for mode in quota-weekly quota-api; do
        case_dir=$(new_case)
        run_session_summary "$case_dir" "$mode" >/dev/null 2>&1 || return 1
        marker="${case_dir}/module/state/claude-backoff"
        assert_contains "$marker" 'reason=quota' || return 1
    done
}

test_summary_classifies_real_auth_error_corpus() {
    local mode case_dir marker
    for mode in auth-401 auth-api; do
        case_dir=$(new_case)
        run_session_summary "$case_dir" "$mode" >/dev/null 2>&1 || return 1
        marker="${case_dir}/module/state/claude-backoff"
        assert_contains "$marker" 'reason=auth' || return 1
    done
}

test_summary_classifies_timeout() {
    local case_dir
    case_dir=$(new_case)
    run_session_summary "$case_dir" sleep 65536 1 >/dev/null 2>&1 || return 1

    assert_equals 1 "$(call_count "${case_dir}/calls")" "timeout model calls" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'class=timeout'
}

test_summary_hard_kills_term_ignoring_process() {
    local case_dir started elapsed
    case_dir=$(new_case)
    started=$(date +%s)
    run_session_summary "$case_dir" ignore-term 65536 1 4 1 >/dev/null 2>&1 || return 1
    elapsed=$(( $(date +%s) - started ))

    if (( elapsed >= 4 )); then
        printf '  summary exceeded hard timeout: %ss\n' "$elapsed" >&2
        return 1
    fi
    assert_contains "${case_dir}/module/logs/summary.log" 'class=timeout'
}

test_summary_rejects_oversized_prompt_without_model_call() {
    local case_dir
    case_dir=$(new_case)
    run_session_summary "$case_dir" success 64 >/dev/null 2>&1 || return 1

    assert_equals 0 "$(call_count "${case_dir}/calls")" "input-limit model calls" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'class=input-limit'
}

test_summary_rejects_invalid_numeric_config_before_model_call() {
    local case_dir
    case_dir=$(new_case)
    TEST_QUOTA_BACKOFF_SECONDS=bad run_session_summary "$case_dir" success >/dev/null 2>&1 || return 1

    assert_equals 0 "$(call_count "${case_dir}/calls")" "invalid-config model calls" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'class=other invalid_config=SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS'

    case_dir=$(new_case)
    run_session_summary "$case_dir" success bad >/dev/null 2>&1 || return 1
    assert_equals 0 "$(call_count "${case_dir}/calls")" "invalid input config model calls" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'class=other invalid_config=SESSION_SUMMARY_MAX_INPUT_BYTES'
}

run_test() {
    local name=$1
    if "$name"; then
        printf 'PASS %s\n' "$name"
    else
        printf 'FAIL %s\n' "$name" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

run_test test_cli_init_uses_non_generative_auth_status
run_test test_cli_init_serializes_overlapping_runs
run_test test_cli_init_classifies_timeout_without_generation
run_test test_cli_init_hard_kills_term_ignoring_process
run_test test_summary_uses_one_bounded_low_effort_call
run_test test_summary_serializes_overlapping_runs
run_test test_summary_quota_creates_six_hour_backoff
run_test test_summary_auth_creates_twenty_four_hour_backoff
run_test test_summary_classifies_real_quota_error_corpus
run_test test_summary_classifies_real_auth_error_corpus
run_test test_summary_classifies_timeout
run_test test_summary_hard_kills_term_ignoring_process
run_test test_summary_rejects_oversized_prompt_without_model_call
run_test test_summary_rejects_invalid_numeric_config_before_model_call

if (( FAILURES > 0 )); then
    printf '%s test(s) failed\n' "$FAILURES" >&2
    exit 1
fi

printf 'ALL QUOTA-AWARE CRON TESTS PASSED\n'
