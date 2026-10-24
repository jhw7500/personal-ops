# WSL clone-first setup

This procedure creates a new MCP installation for one WSL Linux user. It does
not copy another machine's runtime, keys, Workspace authority, or host grants.
Work under the Linux home filesystem rather than `/mnt/c`.

## 0. Verify ordinary-command prerequisites

Configuration alone does not prove that ordinary host commands can run. Before
the first command, verify WSL 2 with WSL version 0.67.6 or newer, a functioning
systemd user manager for the intended Linux user, and a C toolchain that can
link the small static native launcher. These checks are read-only:

```bash
# Run the first two commands from Windows PowerShell.
wsl.exe --version
wsl.exe --list --verbose

# Run the remaining commands as the intended WSL Linux user.
cat /proc/1/comm
test -x /usr/bin/systemd-run
systemctl --user --no-pager status
test -x /usr/bin/cc
dpkg-query -W build-essential gcc libc6-dev
cat /sys/fs/cgroup/cgroup.controllers
```

PID 1 being systemd and the presence of cgroup v2 do not by themselves prove
that the user's manager is reachable or that application delegation works.
Do not use `systemd-run` as a readiness probe because it creates a unit.

On Ubuntu 24.04 and Debian 13, `build-essential` pulls in GCC and development
tools, while `libc6-dev` supplies the static C library and startup objects used
by the launcher build. Install missing packages separately using the chosen
distribution's package procedure. Enabling systemd requires merging
`[boot] systemd=true` into `/etc/wsl.conf` and restarting WSL. Be aware that
`wsl.exe --shutdown` stops all distributions and the WSL 2 VM, so schedule it
around other work. This repository does not perform either host-changing step.

References: [Microsoft WSL systemd](https://learn.microsoft.com/windows/wsl/systemd/),
[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html),
[systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html),
[Ubuntu build-essential](https://packages.ubuntu.com/noble/build-essential),
[Debian libc6-dev file list](https://packages.debian.org/trixie/amd64/libc6-dev/filelist),
and [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html).

## 1. Clone both repositories

Install Git, CA certificates, and `uv` using their official instructions, then
clone the delivery branch and the project it will operate on.

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
git clone --branch feat/mcp-trusted-development-wsl \
  https://github.com/jhw7500/personal-ops.git
git clone https://github.com/jhw7500/pim-check.git
```

Do not copy a temporary source archive. The complete Git clone supplies the
MCP runtime and dashboard inventory.

## 2. Install the pinned existing dependency set

The validated Serena source requires Python `>=3.11,<3.12` and MCP `1.23.0`.
The constraints file records the complete resolution validated with Python
3.11.14. It adds no dependency that is not already in that Serena resolution.

```bash
uv tool install --python 3.11 \
  --constraints "$HOME/projects/personal-ops/chatgpt-mcp/serena-python311-constraints.txt" \
  'git+https://github.com/oraios/serena@66cbe31045107ddcd3851886f8b5854f625e4638'
```

Do not use `--force` over an existing Serena tool installation. Inspect or
remove a conflicting environment deliberately before creating this new PC's
installation.

## 3. Review the plan, then apply it

The first command is read-only. It validates Python, both project directories,
and the complete runtime inventory, then prints the paths it would use.

```bash
cd "$HOME/projects/personal-ops"
"$HOME/.local/share/uv/tools/serena-agent/bin/python" -B \
  chatgpt-mcp/bootstrap.py
```

Review the JSON paths. Apply creates private owner files under
`$XDG_CONFIG_HOME/chatgpt-coding-mcp` and
`$XDG_STATE_HOME/chatgpt-coding-mcp` (with the standard home fallbacks), plus
the immutable runtime and stable controller under
`$HOME/.local/lib/chatgpt-coding-mcp`. It registers `reports`, `hw-results`,
and `events` in `pim-check` as explicit read/list UTF-8 text candidates.

```bash
"$HOME/.local/share/uv/tools/serena-agent/bin/python" -B \
  chatgpt-mcp/bootstrap.py --apply
```

Re-running the same command preserves an exactly matching setup. A different
or symlinked config, runtime, controller, or authority is refused; bootstrap
does not overwrite it. The apply step creates a fresh installation identity
and internal cursor key, admits only the installed ABI 3 runtime, and enables
installation-scoped approvals. It creates no host grant, pending approval,
tunnel credential, service, or tunnel process.

## 4. Use the managed launcher

Copy `runtime`, `launcher`, and `projects_config` from bootstrap output. Keep
`XDG_CONFIG_HOME` and `XDG_STATE_HOME` the same when launching. The command
shape for the MCP server is:

```bash
RUNTIME=/ABSOLUTE/RUNTIME/FROM/BOOTSTRAP
LAUNCHER=/ABSOLUTE/LAUNCHER/FROM/BOOTSTRAP
PROJECTS_CONFIG=/ABSOLUTE/PROJECTS_CONFIG/FROM/BOOTSTRAP
"$HOME/.local/share/uv/tools/serena-agent/bin/python" -B \
  "$LAUNCHER" \
  --release "$RUNTIME" \
  --interpreter "$HOME/.local/share/uv/tools/serena-agent/bin/python" \
  --isolated -- --config "$PROJECTS_CONFIG"
```

Use the same launcher with `--entrypoint admin_server.py` only when you choose
to start the loopback administration UI. Bind it to loopback and reach it by
local access or SSH forwarding. Bootstrap does not start it.

An optional Codex profile can be configured later with the installed
`configure-agent-profile.py` after you decide its account and delegation
scope. Bootstrap does not call Codex or create external credentials.

GitHub CLI, its authentication, and the current user's `jhw-pr` and
`pre-pr-tribunal` skills are optional prerequisites for later PR workflows.
The runtime reports them explicitly when absent; it does not download skills,
authenticate GitHub, or create a PR. See the
[GitHub CLI Linux installation guide](https://github.com/cli/cli/blob/trunk/docs/install_linux.md#debian)
and [GitHub CLI authentication guide](https://docs.github.com/en/github-cli/github-cli/authenticating-with-github-cli).
When supported by the installed version, `gh auth status --no-refresh` avoids
refreshing expiring tokens during a readiness check; never use `--show-token`.

## 5. Connect a distinct tunnel after local verification

Choose a PC-specific alias such as `personal-ops-coding-wsl-pc1`. Create a new
runtime key and tunnel ID through the official OpenAI flow; do not reuse a key
or grant from another host. Pass the managed launcher command above as the MCP
command to `tunnel-client runtimes connect`, then inspect:

```bash
tunnel-client runtimes status personal-ops-coding-wsl-pc1 --json
```

Bootstrap output covers only configuration state: private files, complete
manifest, ABI 3 admission, and an empty installation grant state. It does not
claim that systemd, the compiler/static link path, or command execution was
verified on the target. Windows
native drivers, WSL restart persistence, the live tunnel, ChatGPT discovery,
browser approval behavior, and real `pim-check` or target-board tests require
separate checks on the intended PC.

References: [uv tool installation](https://docs.astral.sh/uv/guides/tools/),
[Microsoft WSL filesystem guidance](https://learn.microsoft.com/windows/wsl/filesystems),
and [OpenAI Secure MCP Tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
