#!/usr/bin/env python3
"""Read-only GitHub reminders for interactive SSH logins; no persistent cache."""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID


CONFIG = Path.home() / ".config/personal-ops/ssh-issue-reminder.json"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def read_settings(path):
    with path.open(encoding="utf-8") as stream:
        settings = json.loads(stream.read(65537))
    if not isinstance(settings, dict) or type(settings.get("enabled")) is not bool:
        raise ValueError("invalid settings")
    if not settings["enabled"]:
        return settings
    UUID(settings["activate_after_boot"])
    issues = settings["issues"]
    if not isinstance(issues, list) or not 1 <= len(issues) <= 8:
        raise ValueError("invalid issue list")
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("invalid issue")
        repo, number, label = issue["repo"], issue["number"], issue["label"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid repository")
        if type(number) is not int or not 1 <= number <= 1_000_000_000:
            raise ValueError("invalid number")
        if not isinstance(label, str) or not 1 <= len(label) <= 160 or not label.isprintable():
            raise ValueError("invalid label")
    return settings


def issue_state(issue):
    env = os.environ.copy()
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat", NO_COLOR="1")
    env.pop("GH_DEBUG", None)
    env.pop("GH_FORCE_TTY", None)
    try:
        result = subprocess.run(
            ["/usr/bin/gh", "issue", "view", str(issue["number"]),
             "--repo", "github.com/" + issue["repo"], "--json", "state"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if isinstance(payload, dict) and payload.get("state") in ("OPEN", "CLOSED"):
                return payload["state"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "UNKNOWN"


def show_reminders(config_path, boot_id_path, output, *, preview=False):
    try:
        settings = read_settings(config_path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("\n[SSH 작업 알림] 설정 확인 불가: 알림 설정 파일을 확인하세요.\n", file=output)
        return
    if not settings["enabled"]:
        return
    if not preview:
        try:
            boot_id = UUID(boot_id_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            print("\n[SSH 작업 알림] 부팅 정보 확인 불가: 작업 상태를 직접 확인하세요.\n", file=output)
            return
        if boot_id == UUID(settings["activate_after_boot"]):
            return

    issues = settings["issues"]
    with ThreadPoolExecutor(max_workers=len(issues)) as pool:
        states = list(pool.map(issue_state, issues))
    pending = [(issue, state) for issue, state in zip(issues, states) if state != "CLOSED"]
    if not pending:
        return
    print("\n[SSH 미완료 작업 알림]", file=output)
    for issue, state in pending:
        status = "미완료" if state == "OPEN" else "상태 확인 불가 (네트워크/gh 인증 확인 필요)"
        print(f"- [{status}] {issue['label']}", file=output)
        print(f"  https://github.com/{issue['repo']}/issues/{issue['number']}", file=output)
    print("다른 작업 종료 후 승인된 절차로 진행하세요. 이 알림은 작업을 실행하지 않습니다.\n", file=output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Check now without SSH/reboot gating")
    args = parser.parse_args()
    if not args.preview and not (
        os.environ.get("SSH_CONNECTION") and os.environ.get("SSH_TTY")
        and sys.stdin.isatty() and sys.stdout.isatty()
    ):
        return 0
    show_reminders(CONFIG, BOOT_ID, sys.stdout, preview=args.preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
