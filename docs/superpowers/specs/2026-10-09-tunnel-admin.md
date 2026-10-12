# Tunnel management UI, activity and project memory

The user selected implementation in the existing personal-ops-tunnel. Provide an independent local management page for observed work and persistent project notes. Use ordinary authorized local implementation and verification; no commit, external publication, additional approval workflow, or third-party installation is requested.

## Behavior

- Serve a Korean management page on 127.0.0.1:8765 in a separate process. Access remotely using SSH local forwarding. This does not add a ChatGPT settings page or control its response rendering.
- The allowed-project selector filters the page only. Every MCP/API operation remains explicitly scoped to a configured project.
- Show persisted jobs, states, exit codes and incremental output, polling every two seconds while following. Opening the page never changes a job state. Show observed MCP tool activity separately from command completion.
- Store one UTF-8 Markdown memory document per canonical project path, outside writable repositories. A read returns revision zero when empty; updates require the expected revision. Concurrent stale updates fail without losing text. Limit content to 32768 UTF-8 bytes.
- Expose read_project_memory and update_project_memory through MCP and the same store through HTTP. Notes survive process restarts and may be read in a new chat; this is not automatic restoration of a conversation. No additional model or inference call is introduced.
- Record tool name, project, call ID, time and started/completed/failed/canceled status. Do not record arguments, file contents, credentials or raw exception messages. Bounded activity storage retains recent events only.

## Boundaries

Use Python standard library and plain static web assets; no new package dependencies. Preserve sandbox, project allowlist, Git receipts, legacy single-project file tools and disabled Yocto execution. Admin APIs provide read-only job/log/activity access and memory editing, not command execution, cancellation, Git approval or configuration edits.

Bind loopback only; require a separate current-user-owned 0600 bearer token for every API. Reject untrusted Host/Origin headers, do not enable CORS, constrain request sizes, use CSP and text rendering. Store directories as 0700 and regular files as 0600; reject symbolic links. Do not put the bearer token in logs, URL queries, repository files or chat output. A host CLI may show a login URL with token fragment on the user's own terminal. Browser removes fragments immediately and keeps authentication in memory.

The read-only job reader must not instantiate JobManager: its startup recovery would mutate active records. Missing/malformed records should produce bounded errors or skips without changing files.

## Completion evidence

Tests prove memory isolation/CAS/restart persistence, rejected links, bounded metadata-only activity; HTTP auth/origin/path/request limits and immutable running jobs; browser project switching, incremental logs, conflicts and literal text rendering. Existing MCP/execution/Git/runtime tests pass. Install a verified immutable runtime and a user admin service, then confirm HTTP behavior and tunnel readiness without interrupting active jobs. Report any client-side verification gap.

## Follow-up: simpler access (2026-10-09)

User clarified that the initial URL worked only in the host browser and requested access from both local PC and phone; both already use the same Tailscale network. Add a signed HttpOnly browser session (optional 30-day persistence; default selected), exact configured HTTPS reverse-proxy origin, Secure cookies for that origin, and keep the app bound to loopback. The fixed bookmark URL has no token. Session creation still requires the existing bearer; invalid/expired sessions fail. Logout clears the browser cookie. This replaces the original memory-only authentication requirement for convenience. Preserve existing Tailscale443 service; prepare private Serve8443. Actual Serve activation requires an interactive sudo password unavailable to this session.

## Follow-up: explicit LAN access (2026-10-09)

User clarified phone succeeds on Tailscale while PC uses LAN without Tailscale, and explicitly chose adding a LAN-only PC address. Bind an additional socket only on 192.168.2.70:8765, retain loopback for existing HTTPS proxy, reject wildcard/public bind addresses. LAN uses HTTP (explained to user), exact LAN Host/Origin and existing session authentication; no public HTTPS origin is trusted by the LAN handler. Add --print-lan-login-url for first login; preserve phone HTTPS sessions. This intentionally supersedes loopback-only binding for the explicit opt-in LAN listener.
