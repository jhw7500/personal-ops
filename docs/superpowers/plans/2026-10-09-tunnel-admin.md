# Tunnel Admin Implementation Plan

**Goal:** Add a local management UI, observable tool activity and persistent project memory to personal-ops-tunnel.

**Architecture:** Independent loopback HTTP process shares private state with the stdio MCP process. It reads job files without recovery side effects. Memory uses atomic writes and cross-process locking.

**Tech Stack:** Existing Python MCP SDK, Python standard library, HTML/CSS/JavaScript.

**Spec:** ../specs/2026-10-09-tunnel-admin.md

**Execution:** User authorized option 1 and autonomous local work. Ordinary native implementation with bounded parallel slices and final independent review; preserve unrelated untracked work, do not commit.

## Global Constraints

- Bind 127.0.0.1:8765; no new dependencies or host command APIs.
- Every project operation validates the existing allowlist; UI selection never changes MCP project state.
- Memory content limit 32768 UTF-8 bytes; expected_revision is an integer, initially 0.
- Private state is outside writable projects; directories 0700, files 0600, no symbolic links.
- Logs/notes are untrusted text. Never put credentials in output or activity records.

## Review Focus

- Admin startup while a command runs must not change its persisted state.
- Concurrent memory saves must preserve the winning revision and reject stale writers.
- Browser polling must not replace edited text or show results for a previously selected project.
- Local hostile websites must not authenticate/mutate API via Origin, Host or CORS tricks.
- Runtime snapshot must contain all Python modules and static assets used by either process.

### Task 1: Shared state and MCP integration

Files: chatgpt-mcp/project_state.py, server.py, tests/test_project_state.py, tests/test_router.py.

Interfaces: `ProjectState(projects: dict[str, Path], state_dir: Path)` where state_dir is the common chatgpt-coding-mcp directory. `read_memory(project)` returns `{project, content, revision, updated_at}`. `update_memory(project, content, expected_revision)` returns the same; raises `MemoryConflict(ValueError)` on stale revision. `record_activity(project_or_none, tool, phase, call_id)` records only those fields plus timestamp; `list_activity(project, limit=100)` returns `{project, events:[{project,tool,phase,call_id,time}]}` newest first. All project values validate; None is for projectless tools only. Rotate activity at 1 MiB retaining one previous file, lock reads/writes. Unknown tools do not become arbitrary activity text.

- [x] Add tests for persistence, canonical-root separation, CAS, permissions/links, bounds and metadata-only activity.
- [x] Implement store and verify targeted tests.
- [x] Add explicit memory tools with appropriate read/write annotations and server instructions; wrap dispatch with sanitized activity events. Activity failures must not mask successful operations or cause retry of completed mutations; emit a generic stderr diagnostic.
- [x] Update router test for version 1.4.0, memory roundtrip and activity; keep legacy tools unchanged.

### Task 2: Local admin HTTP and read-only jobs

Files: chatgpt-mcp/admin_server.py, tests/test_admin_server.py.

Interfaces: consumes ProjectState above. `create_server(projects, state_dir, token_path, port=8765)` returns ThreadingHTTPServer bound to 127.0.0.1, port=0 permitted for tests. CLI `--config`, `--port`, optional `--print-login-url` (print URL and exit; generate token if needed). Default token is ~/.config/chatgpt-coding-mcp/admin-token. Fixed assets `/`, `/admin.js`, `/admin.css` adjacent to script. JSON error `{error: string}` with bounded descriptions.

API: GET `/api/status` -> `{status:'ok',version:'1.4.0'}`; GET `/api/projects` -> `{projects:[{project,path}]}`; GET `/api/jobs?project=X` -> `{project,jobs:[...]}`; GET `/api/log?project=X&job_id=ID&offset=N` -> existing JobManager read_log shape; GET `/api/activity?project=X` -> store list_activity; GET `/api/memory?project=X` and PUT same endpoint `{content,expected_revision}` -> memory shape. Unknown project/invalid input 400, unauthenticated 401, forbidden origin/host 403, memory conflict 409, oversized 413, missing 404. Limit PUT body to 200 KiB to accommodate JSON escaping within decoded memory limit.

- [x] Tests pin auth/Host/Origin, read-only active-job behavior, no path traversal/symlinks, malformed/oversized bodies and memory conflict.
- [x] Implement no-CORS HTTP with bearer authentication, exact route/static allowlist, CSP, no-store/nosniff, safe generic errors, no sensitive access logs.
- [x] Read persisted jobs safely on each request; never instantiate JobManager. Exclude fingerprint/argv from list response; log endpoint explicitly returns command output after auth. Test byte cursors and missing queued logs.

### Task 3: Management page

Files: chatgpt-mcp/admin.html, admin.js, admin.css; optional focused browser check.

Consumes exact Task 2 APIs. Korean page with token login, repo selector, status, jobs list and selectable log pane, tool activity, Markdown memory textarea/save with revision. Two-second follow polling; pause/resume. Token only in JS memory; consume `#token=...` then history.replaceState to strip fragment before network calls. No remote assets, inline script/style, HTML rendering of data, auto memory writes, or execution controls.

- [x] Implement usable responsive UI and empty/error/loading states.
- [x] Preserve unsaved memory during polling; on 409 keep typed content and offer explicit reload. Never overwrite dirty notes on project switch without warning.
- [x] Discard late responses after project/job changes; avoid overlapping polls and duplicated log chunks; reset byte cursor on job switch. Bound displayed log length.
- [x] Verify browser authentication, repo isolation, log append, conflict and literal HTML rendering using local available Playwright.

### Task 4: Integration, runtime and documentation

Files: chatgpt-mcp/install-runtime.py, README.md, tests/test_runtime_install.py, optional service installer.

- [x] Include project_state.py, admin_server.py and all three assets in runtime manifest; document SSH forwarding, token login, service startup, shared memory semantics and log limitations.
- [x] Run complete unittest suite in Serena Python, ruff, browser smoke and independent security/correctness review. Fix material findings and rerun affected checks.
- [x] Install immutable runtime; configure a dedicated systemd user admin service and verify loopback auth. Only reload exact tunnel runtime if no active jobs; confirm expected snapshot/process/readiness.
- [x] Record final evidence and concrete user access command, keeping token out of chat/tool output.

## Completion evidence

- Task 1: project state tests 7 passed; MCP router integration passed. Escaped 32KiB memory read limit corrected after independent review; FIFO/descriptor validation included.
- Task 2: HTTP tests 5 passed. Reads preserve active job bytes; unauthenticated requests return 401; project boundaries/CAS/log cursor/link checks passed.
- Task 3: Chromium tests 4 passed against real isolated HTTP server. Initial load/reload race corrected after independent review and covered by delayed-response regression.
- Integration: all 71 unittest tests passed in 68.120s; ruff passed; browser 4 passed in 3.091s. Independent read-only review cleared both findings.
- Deployment: immutable 15-file snapshot `6e4f2873d411045055e7` matches current source. Dedicated `personal-ops-admin.service` active/enabled, loopback port8765; user Linger=yes. Helper `~/.local/bin/personal-ops-admin` installed.
- Live HTTP: version1.4.0, 11 allowed projects, 68 tossApp job records; no-auth401. No active jobs before exact managed tunnel restart. Tunnel healthy/ready/process_running true, stale false, new snapshot process confirmed.
- No commits, third-party installation, app source changes, or Git policy changes. Existing ChatGPT client-side tool refresh/end-user display remains unverified.
- Implementation allocation: native executor owned state/MCP; root owned HTTP/UI/install/docs. Existing read-only specialist agents reviewed. Thread capacity prevented additional executor lanes; no extra runtime launched.

## Access follow-up evidence (2026-10-09)

- HTTP9, browser6, runtime2 tests passed; ruff passed. Tests cover session issuance, expiry, cookie tamper/CSRF rejection, fixed-URL refresh, logout and delayed logout editing lock, exact HTTPS origin and Secure cookie.
- Independent read-only review cleared changes; shared-host cookie scope documented.
- Admin-only runtime `bb0498dc0dbbd03ab075` installed, service and helper point to configured Tailscale8443 HTTPS origin. MCP runtime remains unchanged.
- Tailscale Serve setup is pending: unprivileged command denied; sudo -n reports password required. No elevated configuration changes made. Existing443 service preserved. User needs one interactive `sudo tailscale serve --bg --https=8443 http://127.0.0.1:8765` execution; live HTTPS/client verification remains pending.

- Follow-up verification: user activated Serve8443. Host HTTPS returns200; real Chromium login, 11-project list, removed URL fragment and cookie session resume all passed. Android peer online with fresh handshake; registered Windows peer hwjo offline. Client-browser failure detail is still pending.

- LAN follow-up: user chose PC LAN access. HTTP10, browser6, runtime2 tests passed, ruff passed. Deployed admin snapshot5c3408eee447e0d66a25 with explicit loopback+192.168.2.70 sockets. Host-side real Chromium verified LAN HTTP and Tailscale HTTPS login/resume, 11 projects each. HTTPS Secure cookie retained; LAN HTTP cookie is not Secure. PC-side browser/ingress firewall remains unverified. No MCP runtime restart or Tailscale policy changes.
