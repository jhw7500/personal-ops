# WSL clone-first setup

This procedure creates a new MCP installation for one WSL Linux user. It does
not copy another machine's runtime, keys, Workspace authority, or host grants.
Work under the Linux home filesystem rather than `/mnt/c`.

The default web workspace includes `personal-ops`, `pim-check`, and `tossApp`.
Other repositories use the same MCP installation and ordinary file, Git, and
host-command tools. Project-specific dependencies still belong to each project.
Folders are registered explicitly; cloning a repository alone does not expose it.

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

## 1. Clone the default repositories

Install Git, CA certificates, and `uv` using their official instructions, then
clone the delivery branch and the projects it will operate on. Existing clones
can be reused; do not replace their working changes.

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
git clone --branch feat/mcp-trusted-development-wsl \
  https://github.com/jhw7500/personal-ops.git
git clone https://github.com/jhw7500/pim-check.git
git clone https://github.com/jhw7500/tossApp.git
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

The first command is read-only. It validates Python, all selected project directories,
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

### Select a different project set for a new installation

Repeat `--project NAME=ABSOLUTE_PATH` to supply the **complete** project list.
Explicit entries replace the three defaults; they are not extra entries appended
to the defaults. No project name is mandatory. For example, a PC used only for
`personal-ops` and `tossApp` can omit the `pim-check` clone:

```bash
"$HOME/.local/share/uv/tools/serena-agent/bin/python" -B \
  chatgpt-mcp/bootstrap.py \
  --project "personal-ops=$HOME/projects/personal-ops" \
  --project "tossApp=$HOME/projects/tossApp"
# After reviewing the plan, repeat the same arguments with --apply.
```

For another repository, clone it under `~/projects` and include another
`--project "my-project=$HOME/projects/my-project"` in both commands. Each path
must exist, be below `~/projects`, and be separate from every other project root.
Do not register the parent `~/projects` directory or nested worktrees as separate
projects. Only a selected `pim-check` receives the three default artifact rules;
other projects do not inherit camera QA result paths.

Bootstrap is for a new installation or an exactly matching repeat. It is not an
updater for an existing installation. Use section 7 for later project additions.

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
browser approval behavior, and real `pim-check`, `tossApp`, or target-board tests require
separate checks on the intended PC.

References: [uv tool installation](https://docs.astral.sh/uv/guides/tools/),
[Microsoft WSL filesystem guidance](https://learn.microsoft.com/windows/wsl/filesystems),
and [OpenAI Secure MCP Tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## 6. Prepare tossApp dependencies on the WSL PC

Keep `tossApp` at `~/projects/tossApp` and register that repository root once.
Its `ai-tarot`, `server`, and `contracts` directories are parts of the same project.
Select `tossApp` in web chat and set each command's working directory to the
relevant package; do not register each package as a separate MCP project.

As checked on 2026-10-26, the repository's `.nvmrc` selects Node **24.12.0**.
Use the version recorded in the checkout being installed. Root, `ai-tarot`, and
`server` have separate npm lockfiles, so installing only at the root is insufficient.
After reviewing the checkout, run on the WSL PC:

```bash
cd "$HOME/projects/tossApp"
# With nvm already installed, use the repository's declared version.
nvm install
nvm use
npm ci
npm --prefix ai-tarot ci
npm --prefix server ci
```

This prepares dependencies; it does not start a server or run QA. Subsequent web
development can use the existing host-command tools with `project=tossApp`, for
example `npm run typecheck` or `npm run test:unit` from `ai-tarot`. No new MCP
function or command profile is needed for each ordinary development command.

Read the selected checkout's READMEs before integrated development. Local API
work also needs PostgreSQL and private server environment values; real Gemini
calls need a key and may incur costs. Playwright E2E needs a working browser in
WSL. SDK mock development, real Toss authentication, and console upload have
different prerequisites. Do not copy build-server `.env`, certificates, absolute
paths, deployment wrappers, or host profiles onto the PC as default settings.
Do not run migration, seed, upload, or deployment as part of MCP setup.

## 7. Add projects after installation

The installed MCP reads its project map at startup. Adding a repository is a
configuration change and does not require a new installation, tunnel, MCP
function, or per-command profile:

1. Clone the desired repository into a distinct directory under `~/projects`.
   Read its environment instructions and prepare its own dependencies.
2. Wait for this WSL installation's jobs to finish, then stop its MCP tunnel and
   admin processes using the startup mechanism configured on that PC.
3. Back up `${XDG_CONFIG_HOME:-$HOME/.config}/chatgpt-coding-mcp/projects.json`
   privately. Preserve every existing entry and add the new name and absolute
   path inside its `projects` object. Keep the owner-only file mode `0600`.
   For example, add `"tossApp": "/home/YOUR_WSL_USER/projects/tossApp"` when
   extending an older two-project installation. Replace the example user with
   the actual WSL user; do not write `$HOME` literally into JSON.
4. Preserve the policy, authority, installed runtime, keys, and grant state.
   An ordinary source repository below the existing base root needs no new
   artifact rule. If ignored output files need access, register only the intended
   result directories through the existing Workspace policy process; do not
   blanket-allow ignored files or credentials.
5. Restart this installation with its existing managed launcher. When
   `personal-ops` is registered, request `refresh_mcp_catalog` for that project.
   A configuration without `personal-ops` has no refresh tool; reconnect or
   refresh the connector's metadata in ChatGPT instead. Check `list_projects`
   and read the new project's README. If ChatGPT still uses the old project enum,
   refresh its connector metadata and retry in a new chat. A sent refresh
   notification alone does not prove that ChatGPT has fetched the new schema.

Project addition preserves an applicable installation-wide host grant. A grant
that is missing, expired, revoked, or bound to another installation still needs
the normal web approval. Do not copy grants from the build server. Repository
registration does not make Linux/WSL support universal: Windows-only drivers,
hardware access, credentials, browsers, and external services remain project
environment requirements.

For Codex running **on the WSL PC**, use this setup prompt:

```text
personal-ops의 chatgpt-mcp/WSL-SETUP.md를 따라 웹 작업 환경을 준비해줘.
기본 프로젝트는 ~/projects/personal-ops, ~/projects/pim-check,
~/projects/tossApp이며 이후 다른 저장소도 명시적으로 추가할 수 있게 해줘.
먼저 기존 설치와 현재 경로를 확인하고, 새 설치에만 bootstrap을 사용해줘.
기존 설치라면 프로젝트 목록을 백업하고 기존 항목을 보존해 누락된 프로젝트를 추가해줘.
설치 ID·키·권한·정책을 초기화하거나 빌드서버 설정을 복사하지 마.
tossApp은 저장소의 .nvmrc와 패키지별 lockfile에 맞춰 환경만 준비해줘.
실제 앱 개발·테스트·타겟 QA·DB 변경·배포는 나중에 웹에서 할 거야.
목록·파일 접근·실행 환경을 검증하고, 검증하지 못한 연결이나 권한은 따로 알려줘.
```
