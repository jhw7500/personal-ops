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
    auth-slow)
        sleep "${CLAUDE_STUB_SLEEP:-2}"
        printf '%s\n' 'Failed to authenticate. API Error: 401 Invalid authentication credentials' >&2
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
elif [[ -n "${CLAUDE_STUB_OUTPUT_FILE:-}" && -f "$CLAUDE_STUB_OUTPUT_FILE" ]]; then
    /bin/cat "$CLAUDE_STUB_OUTPUT_FILE"
else
    today=$(date +%Y-%m-%d)
    yesterday=$(date -d yesterday +%Y-%m-%d)
    printf '%s\n' \
        '---' '' \
        "## ${today} (${yesterday} ~ ${today})" '' \
        '### 작업 내역' '- **stub**: 완료' '' \
        '### 미완료 항목'
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

    if [[ -n "${SANDBOX_SCRIPT_ARG:-}" ]]; then
        bwrap --die-with-parent --unshare-net \
            --ro-bind / / \
            --dev /dev \
            --proc /proc \
            --bind "$TEST_ROOT" /mnt \
            --bind "${case_dir}/tmp" /tmp \
            --chdir "$REPO_ROOT" \
            "$@" /bin/bash "$script" "$SANDBOX_SCRIPT_ARG"
    else
        bwrap --die-with-parent --unshare-net \
            --ro-bind / / \
            --dev /dev \
            --proc /proc \
            --bind "$TEST_ROOT" /mnt \
            --bind "${case_dir}/tmp" /tmp \
            --chdir "$REPO_ROOT" \
            "$@" /bin/bash "$script"
    fi
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
    local runtime_tracker="${REPO_ROOT}/session-summary/resolution-tracker.py"
    if [[ -n "${TEST_TRACKER_BASENAME:-}" ]]; then
        runtime_tracker="${runtime_case}/${TEST_TRACKER_BASENAME}"
    fi

    sandbox_run "$case_dir" \
        "$SESSION_SUMMARY_SCRIPT" \
        env \
        CLAUDE_STUB_CALLS="${runtime_case}/calls" \
        CLAUDE_STUB_MODE="$mode" \
        CLAUDE_STUB_SLEEP="$sleep_seconds" \
        CLAUDE_STUB_OUTPUT_FILE="${runtime_case}/stub-output" \
        CLAUDE_BIN="$runtime_stub" \
        SESSION_SUMMARY_MODULE_DIR="${runtime_case}/module" \
        SESSION_SUMMARY_LOCK_FILE="${runtime_case}/tmp/session-summary.lock" \
        SESSION_SUMMARY_RESOLUTION_TRACKER="$runtime_tracker" \
        SESSION_SUMMARY_RESOLUTION_TRACKING="${TEST_RESOLUTION_TRACKING:-1}" \
        SESSION_SUMMARY_MAX_INPUT_BYTES="$max_input_bytes" \
        SESSION_SUMMARY_TIMEOUT_SECONDS="$timeout_seconds" \
        SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS="$kill_after_seconds" \
        SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS="${TEST_QUOTA_BACKOFF_SECONDS:-21600}" \
        SESSION_SUMMARY_AUTH_BACKOFF_SECONDS="${TEST_AUTH_BACKOFF_SECONDS:-86400}" \
        INCLUDE_OPENCODE=0 \
        OPENCODE_DB="${runtime_case}/opencode.db" \
        INCLUDE_CC="${TEST_INCLUDE_CC:-0}" \
        CC_PROJECTS_DIR="${runtime_case}/cc-projects" \
        INCLUDE_COMMITS="${TEST_INCLUDE_COMMITS:-0}" \
        REMOTE_HOSTS=" "
}

run_session_rotate() {
    local case_dir=$1
    local runtime_case="/mnt/${case_dir##*/}"

    SANDBOX_SCRIPT_ARG=rotate sandbox_run "$case_dir" \
        "$SESSION_SUMMARY_SCRIPT" \
        env \
        SESSION_SUMMARY_MODULE_DIR="${runtime_case}/module" \
        SESSION_SUMMARY_LOCK_FILE="${runtime_case}/tmp/session-summary.lock"
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

assert_not_contains() {
    local file=$1
    local literal=$2
    if grep -Fq -- "$literal" "$file"; then
        printf '  unexpected %q in %s\n' "$literal" "$file" >&2
        return 1
    fi
}

seed_resolution_case() {
    local case_dir=$1
    local repo="${case_dir}/gstApp"
    local runtime_case="/mnt/${case_dir##*/}"
    local runtime_repo="${runtime_case}/gstApp"
    local open_date fix_date today session_time
    open_date=$(date -d '3 days ago' +%Y-%m-%d)
    fix_date=$(date -d yesterday +%Y-%m-%d)
    today=$(date +%Y-%m-%d)
    session_time="${fix_date}T12:00:00+09:00"

    mkdir -p "${repo}/src" "${case_dir}/cc-projects/project"
    git -C "$repo" init -q
    git -C "$repo" config user.name 'Resolution Shell Test'
    git -C "$repo" config user.email 'resolution-shell@example.test'
    printf '%s\n' 'void configure(void) { arg.cam[i].bps = 4096; }' \
        > "${repo}/src/config.c"
    git -C "$repo" add src/config.c
    GIT_AUTHOR_DATE="${open_date}T12:00:00+0900" \
        GIT_COMMITTER_DATE="${open_date}T12:00:00+0900" \
        git -C "$repo" commit -q -m 'feat: initial camera config'
    printf '%s\n' \
        'void configure(void) { arg.cam[i].bps = json_array_length(node); }' \
        > "${repo}/src/config.c"
    git -C "$repo" add src/config.c
    GIT_AUTHOR_DATE="${fix_date}T12:00:00+0900" \
        GIT_COMMITTER_DATE="${fix_date}T12:00:00+0900" \
        git -C "$repo" commit -q -m 'chore: build directory cleanup'
    SEEDED_FIX_SHA=$(git -C "$repo" rev-parse HEAD)
    SEEDED_ITEM_ID=$(python3 -c \
        'import hashlib,sys; print("unresolved-" + hashlib.sha256("\0".join(sys.argv[1:]).encode()).hexdigest()[:12])' \
        "$open_date" gstApp 'bps 배열 길이 불일치' "$runtime_repo")

    printf '%s\n' \
        '# Claude 세션 요약' '' \
        '> 자동 생성 파일.' '' \
        "## ${open_date} (${open_date} ~ ${open_date})" '' \
        '### 미완료 항목' \
        '- [ ] bps 배열 길이 불일치 — gstApp — 중' \
        > "${case_dir}/module/logs/session-summary.md"

    printf '{"type":"user","timestamp":"%s","sessionId":"resolution-shell-session","cwd":"%s","message":{"content":"bps 설정 수정"}}\n' \
        "$session_time" "$runtime_repo" \
        > "${case_dir}/cc-projects/project/session.jsonl"

    printf '%s\n' \
        '---' '' \
        "## ${today} (${fix_date} ~ ${today})" '' \
        '### 작업 내역' \
        '- **gstApp**: 설정 검증 수정' '' \
        '### 미완료 항목' \
        "- [x] bps 배열 길이 불일치 — gstApp [resolved by ${SEEDED_FIX_SHA:0:7}]" \
        "<!-- unresolved-id:${SEEDED_ITEM_ID} -->" \
        > "${case_dir}/stub-output"
}

make_tracker_stub() {
    local case_dir=$1
    local mode=$2
    local target="${case_dir}/tracker-stub"
    printf '%s\n' '#!/bin/bash' 'set -u' > "$target"
    case "$mode" in
        prepare-fail)
            printf '%s\n' 'exit 1' >> "$target"
            ;;
        reconcile-fail)
            printf '%s\n' \
                "if [[ \"\${1:-}\" == \"reconcile\" ]]; then exit 1; fi" \
                "exec '${REPO_ROOT}/session-summary/resolution-tracker.py' \"\$@\"" \
                >> "$target"
            ;;
        always-fail)
            printf '%s\n' 'exit 1' >> "$target"
            ;;
    esac
    chmod +x "$target"
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

test_summary_auth_backoff_is_anchored_to_run_start() {
    local case_dir marker started until
    case_dir=$(new_case)
    run_session_summary "$case_dir" auth-slow 65536 5 2 >/dev/null 2>&1 || return 1
    marker="${case_dir}/module/state/claude-backoff"

    started=$(awk -F= '$1 == "started_epoch" {print $2}' "$marker")
    until=$(awk -F= '$1 == "until_epoch" {print $2}' "$marker")
    assert_equals 86400 "$(( until - started ))" "auth backoff anchor" || return 1
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

test_summary_rotate_waits_for_running_summary() {
    local case_dir summary_pid rotate_pid archive_file
    case_dir=$(new_case)

    run_session_summary "$case_dir" sleep 65536 3 1 1 >/dev/null 2>&1 &
    summary_pid=$!
    for _ in $(seq 1 50); do
        [[ -s "${case_dir}/calls" ]] && break
        sleep 0.02
    done
    run_session_rotate "$case_dir" >/dev/null 2>&1 &
    rotate_pid=$!

    wait "$summary_pid" || return 1
    wait "$rotate_pid" || return 1
    archive_file=$(find "${case_dir}/module/archive" -type f -name 'summary-*.md' -print -quit)
    if [[ -z "$archive_file" ]]; then
        printf '  rotate did not archive the completed summary\n' >&2
        return 1
    fi
    assert_contains "$archive_file" '- **stub**: 완료' || return 1
    if [[ -e "${case_dir}/module/logs/session-summary.md" ]]; then
        printf '  rotate left the completed summary in the active file\n' >&2
        return 1
    fi
}

test_summary_recovers_open_item_from_latest_archive_after_rotate() {
    local case_dir marker summary state
    case_dir=$(new_case)
    marker='unresolved-555555555555'
    summary="${case_dir}/module/logs/session-summary.md"
    state="${case_dir}/module/state/unresolved-items.json"
    printf '%s\n' \
        '# Claude 세션 요약' '' \
        '## 2026-05-09 (2026-05-08 ~ 2026-05-09)' '' \
        '### 미완료 항목' \
        '- [ ] 주간 이월 확인 — unknown — 중' \
        "<!-- unresolved-id:${marker} -->" \
        > "$summary"
    printf '%s\n' \
        "{\"schema\":1,\"items\":[{\"id\":\"${marker}\",\"text\":\"주간 이월 확인\",\"project\":\"unknown\",\"priority\":\"중\",\"opened_on\":\"2026-05-09\",\"identity_repo_key\":\"unmapped:1\",\"repo_path\":null,\"baseline_head\":null,\"status\":\"open\",\"resolution\":null,\"verification\":\"repo-unmapped\"}]}" \
        > "$state"

    run_session_rotate "$case_dir" >/dev/null 2>&1 || return 1

    assert_contains "$summary" '- [ ] 주간 이월 확인 — unknown — 중' || return 1
    assert_contains "$summary" "<!-- unresolved-id:${marker} -->" || return 1
    run_session_summary "$case_dir" >/dev/null 2>&1 || return 1

    assert_contains "$summary" '- [ ] 주간 이월 확인 — unknown — 중 [검증 필요]' || return 1
    assert_contains "$summary" "<!-- unresolved-id:${marker} -->" || return 1
    python3 - "$state" "$marker" <<'PY' || return 1
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(state["items"]) == 1
assert state["items"][0]["id"] == sys.argv[2]
assert state["items"][0]["status"] == "open"
PY
}

test_summary_selects_prior_archive_by_period_not_mtime() {
    local case_dir marker summary state old_archive latest_archive
    case_dir=$(new_case)
    marker='unresolved-666666666666'
    summary="${case_dir}/module/logs/session-summary.md"
    state="${case_dir}/module/state/unresolved-items.json"
    old_archive="${case_dir}/module/archive/summary-2026-08-13_2026-08-19.md"
    latest_archive="${case_dir}/module/archive/summary-2026-08-20_2026-08-26.md"
    printf '%s\n' '# Claude 세션 요약' > "$summary"
    printf '%s\n' \
        '# Claude 세션 요약' '' \
        '## 2026-08-19 (2026-08-18 ~ 2026-08-19)' '' \
        '### 미완료 항목' \
        > "$old_archive"
    printf '%s\n' \
        '# Claude 세션 요약' '' \
        '## 2026-08-25 (2026-08-24 ~ 2026-08-25)' '' \
        '### 미완료 항목' \
        '- [ ] archive period 확인 — unknown — 중' \
        "<!-- unresolved-id:${marker} -->" \
        > "$latest_archive"
    touch -d '2030-01-01 00:00:00' "$old_archive"
    touch -d '2020-01-01 00:00:00' "$latest_archive"
    printf '%s\n' \
        "{\"schema\":1,\"items\":[{\"id\":\"${marker}\",\"text\":\"archive period 확인\",\"project\":\"unknown\",\"priority\":\"중\",\"opened_on\":\"2026-08-25\",\"identity_repo_key\":\"unmapped:1\",\"repo_path\":null,\"baseline_head\":null,\"status\":\"open\",\"resolution\":null,\"verification\":\"repo-unmapped\"}]}" \
        > "$state"

    run_session_summary "$case_dir" >/dev/null 2>&1 || return 1

    assert_contains "$summary" '- [ ] archive period 확인 — unknown — 중 [검증 필요]' || return 1
    assert_contains "$summary" "<!-- unresolved-id:${marker} -->"
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

test_summary_publishes_supported_resolution_and_state_once() {
    local case_dir summary state calls
    case_dir=$(new_case)
    seed_resolution_case "$case_dir"

    TEST_INCLUDE_CC=1 run_session_summary "$case_dir" >/dev/null 2>&1 || return 1

    summary="${case_dir}/module/logs/session-summary.md"
    state="${case_dir}/module/state/unresolved-items.json"
    calls="${case_dir}/calls"
    assert_equals 1 "$(call_count "$calls")" "supported resolution calls" || return 1
    assert_equals 1 "$(grep -Fc -- "[resolved by ${SEEDED_FIX_SHA:0:7}]" "$summary")" \
        "supported resolution publication" || return 1
    assert_contains "$summary" "<!-- unresolved-id:${SEEDED_ITEM_ID} -->" || return 1
    python3 - "$state" "$SEEDED_FIX_SHA" <<'PY' || return 1
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(state["items"]) == 1
assert state["items"][0]["status"] == "resolved"
assert state["items"][0]["resolution"]["commit"] == sys.argv[2]
PY
    assert_contains "$calls" "ITEM ${SEEDED_ITEM_ID}" || return 1
    assert_contains "$calls" "CANDIDATE ${SEEDED_FIX_SHA:0:7}" || return 1
    assert_contains "$calls" '커밋 제목만으로 해결 처리 금지' || return 1
    assert_contains "$calls" '숫자·단위·코드 심볼' || return 1
}

test_summary_downgrades_unsupported_resolution() {
    local case_dir summary state
    case_dir=$(new_case)
    seed_resolution_case "$case_dir"
    sed -i "s/${SEEDED_FIX_SHA:0:7}/deadbee/" "${case_dir}/stub-output"

    TEST_INCLUDE_CC=1 run_session_summary "$case_dir" >/dev/null 2>&1 || return 1

    summary="${case_dir}/module/logs/session-summary.md"
    state="${case_dir}/module/state/unresolved-items.json"
    assert_equals 1 "$(call_count "${case_dir}/calls")" "unsupported resolution calls" || return 1
    assert_contains "$summary" '- [ ] bps 배열 길이 불일치 — gstApp — 중 [검증 필요]' || return 1
    assert_not_contains "$summary" '[resolved by deadbee]' || return 1
    python3 - "$state" <<'PY' || return 1
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(state["items"]) == 1
assert state["items"][0]["status"] == "open"
PY
}

test_summary_prepare_failure_skips_model_and_publication() {
    local case_dir
    case_dir=$(new_case)
    make_tracker_stub "$case_dir" prepare-fail

    TEST_TRACKER_BASENAME=tracker-stub run_session_summary "$case_dir" >/dev/null 2>&1 || true

    assert_equals 0 "$(call_count "${case_dir}/calls")" "prepare failure calls" || return 1
    assert_contains "${case_dir}/module/logs/summary.log" 'resolution tracker prepare failed' || return 1
    if [[ -e "${case_dir}/module/logs/session-summary.md" ]]; then
        printf '  prepare failure published a summary\n' >&2
        return 1
    fi
}

test_summary_reconcile_failure_never_publishes_generated_output() {
    local case_dir summary
    case_dir=$(new_case)
    make_tracker_stub "$case_dir" reconcile-fail

    TEST_TRACKER_BASENAME=tracker-stub run_session_summary "$case_dir" >/dev/null 2>&1 || true

    summary="${case_dir}/module/logs/session-summary.md"
    assert_equals 1 "$(call_count "${case_dir}/calls")" "reconcile failure calls" || return 1
    assert_contains "$summary" '_(요약 실패: other, see logs/summary.log)_' || return 1
    assert_not_contains "$summary" '- **stub**: 완료' || return 1
    if [[ -e "${case_dir}/module/state/unresolved-items.json" ]]; then
        printf '  reconcile failure replaced state\n' >&2
        return 1
    fi
}

test_summary_tracking_toggle_preserves_direct_append() {
    local case_dir summary
    case_dir=$(new_case)
    make_tracker_stub "$case_dir" always-fail
    printf '%s\n' '---' '' '## direct append sentinel' > "${case_dir}/stub-output"

    TEST_TRACKER_BASENAME=tracker-stub TEST_RESOLUTION_TRACKING=0 \
        run_session_summary "$case_dir" >/dev/null 2>&1 || return 1

    summary="${case_dir}/module/logs/session-summary.md"
    assert_equals 1 "$(call_count "${case_dir}/calls")" "disabled tracking calls" || return 1
    assert_contains "$summary" '## direct append sentinel' || return 1
    if [[ -e "${case_dir}/module/state/unresolved-items.json" ]]; then
        printf '  disabled tracking created resolution state\n' >&2
        return 1
    fi
}

test_summary_state_replace_failure_is_not_silent() {
    local case_dir exit_code summary
    case_dir=$(new_case)
    mkdir "${case_dir}/module/state/unresolved-items.json"

    run_session_summary "$case_dir" >/dev/null 2>&1
    exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        printf '  state replace failure returned success\n' >&2
        return 1
    fi
    summary="${case_dir}/module/logs/session-summary.md"
    assert_equals 1 "$(call_count "${case_dir}/calls")" "state replace failure calls" || return 1
    assert_contains "$summary" '- **stub**: 완료' || return 1
    assert_contains "${case_dir}/module/logs/summary.log" \
        'resolution state replace failed after summary append' || return 1
    if [[ ! -d "${case_dir}/module/state/unresolved-items.json" ]]; then
        printf '  damaged state target was unexpectedly replaced\n' >&2
        return 1
    fi
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
run_test test_summary_auth_backoff_is_anchored_to_run_start
run_test test_summary_classifies_real_quota_error_corpus
run_test test_summary_classifies_real_auth_error_corpus
run_test test_summary_classifies_timeout
run_test test_summary_hard_kills_term_ignoring_process
run_test test_summary_rotate_waits_for_running_summary
run_test test_summary_recovers_open_item_from_latest_archive_after_rotate
run_test test_summary_selects_prior_archive_by_period_not_mtime
run_test test_summary_rejects_oversized_prompt_without_model_call
run_test test_summary_rejects_invalid_numeric_config_before_model_call
run_test test_summary_publishes_supported_resolution_and_state_once
run_test test_summary_downgrades_unsupported_resolution
run_test test_summary_prepare_failure_skips_model_and_publication
run_test test_summary_reconcile_failure_never_publishes_generated_output
run_test test_summary_tracking_toggle_preserves_direct_append
run_test test_summary_state_replace_failure_is_not_silent

if (( FAILURES > 0 )); then
    printf '%s test(s) failed\n' "$FAILURES" >&2
    exit 1
fi

printf 'ALL QUOTA-AWARE CRON TESTS PASSED\n'
