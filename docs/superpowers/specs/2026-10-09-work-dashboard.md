# Practical tunnel work dashboard

User approved implementing current work summary, changed files/diffs, observed verification results and next-chat handoff in the existing authenticated admin UI.

Keep all current auth, LAN/Tailscale listeners, repo-per-call isolation, job ownership and Git mutation approval rules. No new dependencies, model/API calls, commit/push or command execution API. Normal authorized local implementation and review continue without repeating approval requests.

## Shared progress

ProjectState gains read_progress(project) and update_progress(project, title, status, summary, completed, next_steps, expected_revision). Payload is {project,title,status,summary,completed,next_steps,revision,updated_at}; default title/summary empty, status not_recorded, arrays empty, revision0. Updates require title 1..200 UTF-8 bytes, status planned/in_progress/blocked/completed, summary <=4096 bytes, each array <=20 strings <=1000 bytes, total serialized bounded. Canonical-root key, cross-process CAS, private atomic storage and symlink rejection match memory. MemoryConflict denotes stale progress too. Add read_project_progress/update_project_progress MCP tools and instructions to read/start/update progress alongside memory at verified stages. Saved progress is an agent/user report, not automatically verified completion.

## Observed workspace

Add workspace_view.py with WorkspaceView(projects).changes(project) returns {project,available,branch,head,files:[{path,previous_path,status}],observed_at,error?,truncated}. Parse Git porcelain-v1 -z including renames, staged/worktree and untracked files; exclude .git/.env/.env.* at any depth and symlink paths. Read-only Git execution reuses existing hardened helpers but disables optional Git index refresh. Validate exact allowlisted repository root before reading. Non-Git roots produce available=false without breaking the dashboard.

diff(project,path,staged=False) returns {project,path,staged,text,kind,truncated,observed_at}. Enforce literal safe file paths. Tracked diffs use --no-ext-diff/--no-textconv and bounded output; untracked previews use bounded private-safe file reads and a synthetic added-file diff. Deleted tracked files allowed. Binary files receive explicit no-text-preview result. No hooks, external diff/textconv, symlink following or secrets. Changed-file selection is explicit; no bulk contents dump. Surface per-part errors and truncation honestly. Git changes are working-tree facts, may include changes made outside ChatGPT, and refresh only every 10 seconds/manual.

## HTTP and UI

GET /api/progress?project=X and PUT same endpoint with exact update fields consumed by ProjectState. GET /api/changes?project=X and GET /api/diff?project=X&path=P&staged=true|false consume WorkspaceView. GET /api/handoff?project=X returns {project,text,generated_at} with selected local path, recorded progress, explicit recent command outcomes, current changed file names, saved notes, and next-chat instruction to reread current instructions/state. No raw logs/diffs, secrets or fabricated summaries in handoff. Reuse existing authenticated API; GET failures must not trigger job recovery/mutations. Command outcomes from persisted jobs are displayed with preset/label, state, exit code/time and selectable raw log. Validate job labels/types and avoid marking running/queued canceled or completed.

Dashboard: saved work goal and state, completed and next-step lists, last-update time and clear no-record state; UI editor for these fields with CAS preserving draft on conflict. Changed-file list with staged/worktree selector and bounded literal-text diff. Verification panel highlights test/typecheck/lint/build profiles (other commands remain in jobs). Handoff textarea with copy button using HTTPS clipboard API and HTTP fallback/manual selection. Job/log/activity/memory remain available but visually secondary. Initial selection remembers repository name only in localStorage (no credentials) or prefers most recently active project; no shared MCP project switch. Switching projects discards stale responses and confirms dirty progress/memory. Polls preserve drafts; fetch Git every >=10s, no overlapping poll cycles. All untrusted strings render as text.

## Completion

Prove progress CAS/isolation/restart, MCP roundtrip, Git read-only/exact-root/secret/link/rename/untracked/binary and truncation behavior, HTTP allowlist/auth/conflicts/handoff, and browser dirty drafts/stale responses/diff/verification/copy. Run relevant full suite once after integration, ruff and browser tests. Independent review. Install verified immutable snapshot; update admin service/helper and exact tunnel runtime only after no active jobs. Verify live LAN/HTTPS UI and actual repository observations; state ChatGPT-client gaps honestly.
