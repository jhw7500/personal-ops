# Expiring host access and existing Codex delegation

> **For agentic workers:** Use superpowers:executing-plans for integration; bounded performer slices may run in parallel under the supplied AGENTS.md. Track checks without creating commits.

**Goal:** One owner-approved access window permits general host commands and explicit existing Codex task calls without per-task web approval.

**Architecture:** Owner-only grant state is shared by the MCP and existing admin web. Dynamic host execution reuses the verified systemd supervisor and pinned executable entry. Existing Codex tools dispatch either to the unchanged isolated review manager or the explicit host mode under the active grant.

**Tech Stack:** Python stdlib, existing MCP SDK, vanilla browser JavaScript, current systemd bootstrap; no new dependencies.

**Spec:** /home/jhw/ai/opencode/projects/personal-ops/.omx/plans/2026-10-15-web-host-execution.md (latest user decision overrides the earlier outline).

## Global constraints

- No production grants, model calls, service changes, Docker/DB operations or Git commits during implementation/testing.
- Preserve all original checkout WIP; integrate only files whose baseline hash and mode still match.
- Grants are per selected project, installation-wide for its MCP callers, 4 hours by default (maximum 4 hours), explicitly revocable. Project is context, not host filesystem confinement.
- Host mode has effective host-user filesystem/network/group access. Do not claim rollback, isolation, strict Docker/DB exclusion or tamperproof same-UID state.
- Codex usage opt-in belongs to the grant; no automatic model invocation on approval. Existing enabled profile pins binary/model/effort.
- Never resume/stop other Codex or Claude sessions. Keep legacy profiles and v3 targets unchanged.
- Actual service switch and initial real-model validation remain a final concrete approval step.

## Review focus

- An expired/revoked grant prepared earlier must fail immediately before GO.
- A forced isolated Codex request must never fall back to host mode after failed proof validation.
- Duplicate request IDs, competing starts and cleanup failures must not cause a second execution or release an unverified scope lease.
- Logs, objective text and provider reports must stay bounded; known credentials are masked in MCP excerpts, and reports are not independent verification.
- Unsafe Origin/token/body/project changes must not grant or extend host authority.

### Task 1: Owner grant state

**Files:** Create `chatgpt-mcp/host_access.py`, `chatgpt-mcp/tests/test_host_access.py`.
**Interfaces:** `HostAccess(projects, state_dir, *, trusted_runtime=None)`; `request(project)`, `get(project)`, `approve_web(project, *, request_id, duration_seconds, allow_codex, confirmation)`, `require(project, *, grant_id=None, codex=False)`, `guard(project, grant_id, *, codex=False)` context manager, `revoke_web(project, *, grant_id, confirmation)`.

- [x] Write/run failing tests for no grant, request-only, wrong confirmation, expiry/revoke, runtime/boot changes, Codex opt-in, private state, replay/stale request and guard/revoke ordering.
- [x] Implement server-owned grant binding to boot/runtime, private records/locking and 10-minute requests. Returned grant has `grant_id`, `expires_at`, `allow_codex`, project and scope.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s chatgpt-mcp/tests -p test_host_access.py`; all pass without real host changes.

### Task 2: Dynamic commands, logs and native lifecycle

**Files:** Create `chatgpt-mcp/host_execution.py`, `chatgpt-mcp/host_execution_tools.py`, their tests. Modify `chatgpt-mcp/host_command_runner.py` only for additive optional GO guard, bounded output sink and stdin data.
**Interfaces:** `HostExecution(projects, state_dir, *, access, runner=None, trusted_runtime=None)`; `prepare(project, *, executable, argv, cwd, request_id, timeout_seconds=1800, input_text=None, agent_metadata=None)`, async `execute(project, operation_id)`, `get`, `list`, `owns(project, operation_id, kind=None)`, `read_log(project, operation_id, *, stream='stdout', cursor=0, max_bytes=16384)`, `cancel`, async `close`. Calls return `operation_id`, state/phase/result/grant binding; execution stores private bounded stream logs. Agent metadata has profile_id/model/reasoning_effort/profile_digest, execution kind `codex`.

- [x] Write/run failing tests for two distinct commands under one grant, duplicate/conflicting request IDs, grant revocation before GO and while running, timeout/output bounds, private redacted paged logs, queue cancellation and failed cleanup leases.
- [x] Reuse `run_host_profile` with additive keyword-only `go_guard`, `on_output`, `input_bytes`; fixed-profile callers retain their old defaults and output behavior. Native start remains identity-before-GO; finite runtime cap and verified cgroup cleanup remain mandatory.
- [x] Freeze resolved safely owned executable hash/cwd and grant ID. One dynamic host-wide lease; reject observed conflicting managed work and share the existing fixed-command project lock. Do not claim concurrency control over manually launched SSH jobs.
- [x] Add request/get access plus start/get/read/cancel execution MCP tools; exact schema keys and request validation.
- [x] Run new focused tests plus existing host-command runner/entry/operations/tools suites.

### Task 3: Existing Codex flow and admin integration

**Files:** Create `chatgpt-mcp/host_agent.py`, `chatgpt-mcp/host-access-admin.js`; modify `agent_tools.py`, `server.py`, `admin_server.py`, `admin.html`, installer/verification lists, docs and focused tests.
**Interfaces:** Host adapter uses Task 2 prepare/execute/get/log/cancel; `AgentTools(..., host_execution=None)` shares the same manager as ordinary host tools. Optional agent prepare `execution_mode` defaults `auto`, forced isolated retains legacy behavior. Owner approval/revoke POST uses existing `fresh_approval` only.

- [x] Write/run failing tests for absent grant legacy routing, Codex usage denied, auto host dispatch despite stale isolated proofs without changing those proofs, explicit isolated fail-closed, exact pinned model/effort, no resume/daemon session, bounded observed events and distinct model report provenance.
- [x] Implement Codex ephemeral exec using actual registered profile and host runner stdin; do not use fake isolation proof or unverified automatic CAS.
- [x] Add authenticated admin grant approval/revoke/status/log/cancel UI with expiry and Codex usage checkbox. Poll current status without stale project rendering.
- [x] Add packaging and verified runtime modules plus SDK router assertions; run real HTTP owner approval/origin denial fixtures and JS syntax/UI tests.

### Task 4: Review, integration and rollout preflight

- [x] Independent review against the spec, grant races, routing and process lifecycle; fix material findings with regression checks.
- [x] Verify targeted Python suites with system and SDK Python; ruff changed Python files, JS syntax and appropriate UI/router/runtime-install tests.
- [x] Compare baseline hashes/modes before copying exact new/modified files into the original checkout; do not overwrite conflicts or unrelated WIP.
- [x] Build immutable candidate and run read-only rollout preflight; record actual current runtime versus candidate, registered tool names, legacy profile preservation, model/DB/service non-effects, and remaining real-run verification.
- [x] Present final concrete activation impact for approval. No claim of live feature availability from code-only tests.

Activation result (2026-10-15): live snapshot6cd2d5f148c7f65f4626; owner-approved tossApp/Codex grant until16:19:34 KST; actual connector Codex host task4908ef9968e9847936d63635129aa41a succeeded, MCP/tunnel PIDs unchanged. Connector metadata Refresh and actual browser rendering remain unverified. See docs/handoffs/2026-10-15-host-access-and-codex.md for exact evidence and limits.
