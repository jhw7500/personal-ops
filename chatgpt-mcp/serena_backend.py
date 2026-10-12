#!/usr/bin/env python3
"""Run one Serena backend with dedicated state and a fixed tool set."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def write_initial_config(path, value):
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
    except FileExistsError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        parser.error("--project must name a directory")
    binary = Path.home() / ".local/share/uv/tools/serena-agent/bin/serena-mcp-server"
    if not os.access(binary, os.X_OK):
        parser.error("The existing serena-agent installation was not found")

    os.umask(0o077)
    identity = hashlib.sha256(os.fsencode(project)).hexdigest()[:16]
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    serena_home = state / "chatgpt-coding-mcp" / identity
    project_data = serena_home / "project-data"
    project_data.mkdir(parents=True, exist_ok=True)
    write_initial_config(serena_home / "serena_config.yml", {
        "projects": [],
        "web_dashboard": False,
        "web_dashboard_open_on_launch": False,
        "gui_log_window": False,
        "project_serena_folder_location": str(project_data),
        "base_modes": [],
        "default_modes": [],
    })
    write_initial_config(project_data / "project.yml", {
        "project_name": project.name,
        "languages": ["python"],
        "read_only": False,
        "ignore_all_files_in_gitignore": True,
        "ignored_paths": [".git", ".env", ".env.*", "**/.env", "**/.env.*"],
    })

    env = os.environ.copy()
    env["SERENA_HOME"] = str(serena_home)
    context = Path(__file__).resolve().with_name("context.yml")
    os.chdir(project)
    os.execve(binary, [str(binary), "--project", str(project), "--context", str(context),
                      "--transport", "stdio", "--enable-web-dashboard", "false",
                      "--open-web-dashboard", "false", "--enable-gui-log-window", "false"], env)


if __name__ == "__main__":
    main()
