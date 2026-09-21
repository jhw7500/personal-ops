#!/bin/bash
# Source from ~/.profile, not ~/.bashrc: non-login shells must remain quiet.
# A subshell keeps helper variables and errors out of the user's login shell.
(
    case $- in
        *i*) ;;
        *) exit 0 ;;
    esac
    shopt -q login_shell || exit 0
    [[ -n ${SSH_CONNECTION:-} && -n ${SSH_TTY:-} && -t 0 && -t 1 ]] || exit 0
    reminder_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 0
    # Bound the entire invocation as well as the per-request GitHub timeouts.
    if ! /usr/bin/timeout --kill-after=1s 5s /usr/bin/python3 \
        "$reminder_dir/ssh-issue-reminder.py" 2>/dev/null; then
        printf '\n[SSH 작업 알림] 알림 조회를 마치지 못했습니다. 설정/연결 상태를 확인하세요.\n\n'
    fi
    exit 0
)
