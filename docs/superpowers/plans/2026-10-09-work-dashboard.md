# Work Dashboard Implementation Plan

Goal: implement approved four useful work-management features in personal-ops-tunnel.
Spec: ../specs/2026-10-09-work-dashboard.md
Execution: ordinary native implementation with bounded state/MCP delegation; no commit. User already requested implementation and autonomous local work.

- [x] State/MCP slice: project_state.py, server.py, tests/test_project_state.py, tests/test_router.py. Implement exact progress API/schema in spec, revision conflict preservation, version1.5.0 and MCP instructions. No changes to other files. Target tests.
- [x] Root backend: workspace_view.py and tests/test_workspace_view.py. Hardened read-only Git/path policy, status -z, safe bounded diffs/untracked preview, no index refresh. Add authenticated progress/changes/diff/handoff HTTP routes and evidence-backed recommended project to /api/projects in admin_server.py; test in test_admin_server.py.
- [x] Root UI: admin.html/admin.css/admin.js. Prioritize goal/progress/change/diff/checks/handoff; preserve existing login/LAN/HTTPS, safe rendering, polling/draft/CAS/epoch invariants. browser checks in check_admin_ui.py.
- [x] Integration: runtime manifest includes workspace_view.py. README documents flow and memory limits/working-tree attribution. Full unittest+ruff+browser, independent review, fixes, validated runtime install, active-job guarded tunnel reload and dedicated admin restart, live LAN/HTTPS validation.

Global constraints: exact APIs/schema in spec; no dependencies/LLM/invented facts; preserve unrelated work, authorization/auth/job boundaries; no commit, Git mutation or Yocto execution. State dirs0700/files0600 outside writable repos. Token never in tool outputs. Existing source tree is untracked; preserve rather than reset.

## Verified result (2026-10-09)

- Full Python suite: 90/90 passed in 74.822s with the Serena Python environment.
- Real HTTP + Chromium browser checks: 10/10 passed in 9.982s. Ruff and Node24 syntax check passed.
- Regression checks reproduced and resolved staged deletion preview, parent-directory symlink swap, and failed logout while memory/progress GETs are pending.
- Independent architect review: both reported P2 issues resolved; no remaining findings in reviewed fix scope.
- Installed 16-file immutable runtime: `/home/jhw/.local/lib/chatgpt-coding-mcp/releases/df2b05e700bd8194acb1`; manifest hashes/type/owner/mode verified.
- Dedicated admin unit and helper updated. No active jobs/recent in-flight MCP calls before stop/connect. Tunnel healthy=true, ready=true, process_running=true, stale=false; actual MCP process uses the new snapshot.
- Actual browser checks on LAN `http://192.168.2.70:8765/` and private Serve `https://cantopsbuildserver.tail661d29.ts.net:8443/`: login/resume, tossApp checks/changes, personal-ops diff/handoff and remembered project passed. No page errors or 390px overflow.
- Host observation at verification: personal-ops Git available with45 files, tossApp Git available with199 files. These are point-in-time working-tree observations, including others' edits.
- ChatGPT client tool refresh remains client-controlled. The UI does not fabricate historical work goals; use the progress tools or UI editor to record the current goal.
