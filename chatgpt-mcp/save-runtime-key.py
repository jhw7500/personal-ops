#!/usr/bin/env python3
"""Save a tunnel runtime key without echoing it or putting it in shell history."""

import getpass
import os
from pathlib import Path
import stat


def main():
    folder = Path.home() / ".config/chatgpt-coding-mcp"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = getpass.getpass("OpenAI tunnel runtime API key: ").strip()
    if not key.startswith("sk-") or any(char.isspace() for char in key):
        raise SystemExit("Expected an OpenAI API key without whitespace; nothing saved")
    target = folder / "runtime-key"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SystemExit("Key target must be a private, user-owned regular file")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(key + "\n")
    finally:
        os.close(fd)
    print("Saved runtime key to", target, "(mode 0600)")


if __name__ == "__main__":
    main()
