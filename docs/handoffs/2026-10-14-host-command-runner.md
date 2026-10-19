# Registered Host Command Runner — 2026-10-14 source handoff

## Objective

Provide safe calls from ChatGPT to host-owned exact command profiles for building and publishing TEST artifacts (for example, Apps in Toss). This is not an unrestricted shell. ChatGPT supplies only a project name and profile ID; the host owner configures exact executable path and digest, argv, cwd, effect, timeout, and input artifact hashes.

STATUS: immutable MCP/admin runtime remains ce88340086af9f5e1bf2, all five tools are available, and tossapp-deploy-test successfully uploaded a fresh TEST bundle through the actual tunnel on 2026-10-15 KST. Official AIT packaging preserved the selected option-2 frontend and generated a new ID; authenticated one-use web approval was consumed, MCP execution exited 0, and termination was verified. Toss independently reports CREATED/version 20261015-22; the requester received a TEST push. No DB/Docker mutation, service restart, paid agent invocation or Git commit/push occurred. See [fresh upload results](2026-10-15-tossapp-test-upload.md); the earlier failed attempt is retained in 2026-10-14-tossapp-test-upload-profile.md.

CURRENT PROFILE CORRECTION: the earlier upload-only implementation did not
fulfill registration of the complete `tossApp-deploy test` workflow. The current
`tossapp-deploy-test` profile now calls that exact existing host script with
`test`; the earlier pinned uploader is preserved as `tossapp-upload-test`.
After subsequent user authorization, the complete workflow ran successfully
through actual MCP: version 20261015-23, CREATED/isTested=true, command exit 0
and verified termination. See [the correction](2026-10-15-tossapp-deploy-profile-correction.md)
and [full live result](2026-10-15-tossapp-full-test-deployment.md).

## New modules and files

- chatgpt-mcp/host_command_profiles.py: load owner-only 0600 JSON outside user repositories; validate exact v1 schema, allowed project, executable SHA256/mode/owner, fixed argv, cwd, effect, input hashes; reject executables inside any registered writable project and symlinked ancestors through fd-relative O_NOFOLLOW. Allowed effects read_only, build, external_publish (not service_change).
- chatgpt-mcp/host_command_entry.py: isolated Python 3.10 compatible entry; securely open/hash the executable, copy verified bytes into a sealed memfd, execute that FD without a pathname reopen, and retain the project cwd through an FD. Preserve actual HOME and only the existing sanitized environment contract.
- chatgpt-mcp/host_command_runner.py and host_command_supervisor.py: reuse the unchanged systemd/cgroup supervisor and static bootstrap; save the exact execution identity before GO; enforce runtime/output caps and cancellation; verify the complete cgroup has terminated before reporting completion or releasing its lease. Never return raw stdout/stderr, identity paths or secrets.
- chatgpt-mcp/host_command_operations.py: per-project single active plan, 10-minute expiry, request-id idempotency, plan/source digest, private state and single-use receipt; separate admin web approval; recheck pins before and after execution; retain bounded numeric diagnostics on nonzero exit, postcheck or cleanup failure. Unverified cleanup retains the project lease. Interrupted external effects become outcome_unknown, not rollback.
- chatgpt-mcp/host_command_tools.py: five MCP methods list_host_command_profiles, prepare_host_command, execute_host_command, get_host_command, cancel_host_command. No argv/cwd/shell/approved fields accepted from chat.
- chatgpt-mcp/host-command-admin.js: safe textContent-only admin UI, typed operation ID and fresh admin token, approve without executing.

Integrated with chatgpt-mcp/server.py, admin_server.py, admin.html, install-runtime.py, host_runtime_operations.py, rollout-host-runtime.py, project_state.py and exact tool/runtime tests. Does not change existing agent_runner.py, agent_host_broker.py or registered Docker/DB manager.

## Required host-owned config

Expected owner-only regular mode 0600 file, outside all repositories:
~/.config/chatgpt-coding-mcp/host-commands.json

Schema is an object containing version=1, enabled=true and profiles map. Each profile has exactly project, label, executable (trusted absolute regular path), executable_sha256 (64 hex), argv (fixed array), cwd (relative to registered project), timeout_seconds, effect and inputs (relative path -> exact SHA256).

No config present means the host command feature remains fail-closed. At the initial source-only handoff no command policy existed. The subsequently approved registration and real upload are recorded in the current STATUS and linked results. A real shell alias/function may not be a suitable executable; use a reviewed regular script outside the writable repository.

Only inspect candidate config and script before enabling. The previous tarororo AIT path/build may be newer than a package actually uploaded. If a wrapper implicitly rebuilds or selects its own .ait, a pin of an unrelated bundle does NOT prove the bytes uploaded. Require the wrapper to select and attest the exact approved candidate and return provider-side proof.

## Normal execution contract

1. list_host_command_profiles(project=tossApp) returns safe profile IDs/effects; not raw commands, file paths or credentials.
2. prepare_host_command(project=tossApp, profile_id=HOST_REGISTERED_ID, request_id=UNIQUE_ID) creates a ten-minute pending_host_approval plan; NO command runs.
3. Management web approval link /host-commands?operation_id=32HEX uses existing trusted loopback/tailnet Host+Origin, authenticated cookie/Bearer, a newly retyped administrator token, and exact typed operation ID. It issues a one-use receipt; it does NOT execute.
4. execute_host_command requires receipt and fresh file/policy hashes, then returns queued/running; get_host_command confirms final state and termination_verified. cancel_host_command stops only a freshly verified recorded execution scope; effects such as external provider uploads cannot be undone by cancellation.
5. Numeric exit/status/elapsed/output sizes are returned; raw logs and secrets are not returned. Exit code 0 does NOT constitute independently verified provider-console upload, and no QR/invite is sent by these tools alone.

## Security, isolation and restrictions

- This runner is NOT a new OS sandbox; a host-owner registered program can read host HOME credentials and access the network. Never configure a general-purpose interpreter/shell -c wrapper, arbitrary Docker/migration/DB script, or a high-privilege host operator function without independent review.
- service_change effect is refused. Use the existing registered Compose/API/DB workflows for infrastructure mutation.
- Trust is from the immutable installed runtime manifest and host-owned policy, not from ChatGPT prompt approvals or existing Docker target/agent grants.
- Script dependencies, executable paths and selected upload artifacts must be reviewed for time-of-check/time-of-use drift and unintended side effects. The pinned input file remains checked before and after execution but external provider receipts are not yet automatically verified.
- On process/service interruption or uncertain effects, do not blindly rerun publishing commands.
- On restart, recovery first acquires the free project lease; another live manager's task is never adopted or stopped. Durable QUEUED/PRECHECK states with no identity prove launch never started. Recorded scopes require full same-boot binding and cgroup proof; a fully validated prior-boot identity proves those prior processes terminated without touching a reused current unit. Read-only status recovery never stops a unit.
- EXECUTE with no durable identity remains blocked: launch might have started, so neither status nor cancel invents termination proof. Legacy interrupted states already rewritten without their original phase also require operator investigation. A failed queue-state write consumes the one-use approval safely but cannot launch a command; retry approval after the old plan expires.
- Sealed script execution exposes an FD pathname as $0/__file__. Registered wrappers must use explicit trusted absolute dependency paths. Sealing the entry executable does not pin its interpreter, dependencies, or prove which artifact an upload provider consumed.
- The existing HostRuntime, active external Codex/Claude CLI sessions and test API/Worker/DB are unchanged by this source implementation.

## Prior isolated checks and observed limitations

- Source-only new MCP, web, profile, manager and runner tests: 41/41 PASS, job e9dc3d61f981444c8a2a38166f9e8b2d.
- Compiler-independent new+existing regression: 133/133 PASS, job a75da816b84e4f07b76a647f229312a0. Existing admin HTTP approval, registered operations, process runner, packaging and rollout contract included.
- First broader regression failed (job 797d3c53b0a44fdb85f26e13bf7ce087) due to one missing JS newline, now fixed; and existing agent_tasks tests failed to start because sandbox lacked /usr/bin/cc (34 errors). This is not evidence of a model/agent-runtime regression; host compiler/SDK environment must rerun them before rollout.
- Targeted Python AST syntax PASS. Isolated Node의 구버전에서 UI optional chaining을 지원하지 않아 첫 정적 검사가 실패했지만 해당 구문을 제거하고 node --check 성공. 최종 136 tests PASS (new + compiler-independent existing), job 710711f225e246b7ae44821819e361e2, exit0. 실제 외부 서비스/유료 모델은 미호출.
- No real MCP SDK stdio full registration check in the isolated default Python. Run preflight under the actual host Python SDK before installation.
- Independent code/security review and approved immutable runtime preflight/TTY rollout remain required. No effective runtime exposure until such a separately approved rollout.

## Host review and hardening follow-up

The host review found executable pathname replacement, surviving descendants, abandoned runtime state, and lost numeric failure diagnostics. The approved source-fix pass also corrected pre-launch/reboot recovery, queue-write failure registration, shutdown cleanup after a command-manager error, and packaging of the two new modules.

Private evidence root:
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/host-command-hardening-d6d35642`

- Isolated worktree: `/home/jhw/.local/state/personal-ops-worktrees/host-command-hardening-20261014-d6d35642`; original baseline HEAD `cf8b6d2608c9cadaa939fdca8045891e848ec7ba` and per-file hashes/modes are in `baseline.json`.
- Source fixes were developed with failing regression tests first. Failure cases include executable replacement/write, no-GO-before-durable-identity, live descendants, failed termination proof, disk write failure, prelaunch recovery, reboot and numeric diagnostic retention.
- Real systemd 249/cgroup proof: normal parent exit, timeout and cancellation all terminated an actual fork/setsid descendant; an unrelated sentinel stayed alive. `native-cgroup-proof-v2.json` records PASS and termination checks made immediately on runner return, before fallback cleanup. Only disposable test units/processes were used; no model/API/upload call occurred.
- Python 3.10 entry/profile tests: 29 PASS. Scoped Ruff and Node 24 syntax checks passed. Existing proof-bound agent source remains byte-identical to the original checkout and live release `f27541a01e22e5499614`.
- Final independent code review and architecture review: both APPROVE, zero outstanding findings. `final-reviews.json` records their verdicts. The architecture reviewer directly reran 43 targeted tests.
- Actual host SDK/stdio fixture: all five host-command tools registered with exact schemas; source checkout execution and chat-supplied approval fields were denied. This is fixture registration evidence, not live tunnel activation; see `sdk-registration.json`.
- Scope integration: initially 20 files copied after baseline hash/mode checks; the reviewed code-mode fixture correction brings the total to 21 files, with no conflicts and 176 other baseline files preserved. No staging or commit. `source-integration.json`, `source-integration-additional.json` and `post-integration-audit.json` record fingerprints.
- Canonical preflight targeted checks: 560 PASS. The initial broad invocation from repository root hit the pre-existing test_admin_agent_approval import-path error; the rollout's canonical chatgpt-mcp working directory is the full-suite validation surface. The initial result remains in `full-tests.log` for diagnosis.
- The first canonical full check ran 711 tests and stopped on an existing code-mode fixture race. Premature helper exit produced CODE_MODE_EXIT_FAILED in 84/100 repetitions and CODE_MODE_FRAME_INVALID in 16/100; all were bounded failures. `existing-code-mode-race.json` records the diagnosis. The test now verifies exact nonzero status after a complete exchange and keeps early-exit bounded failure as a separate test. The seven code-mode tests pass and this fixture-only correction received independent APPROVE. Production agent code and proof records were not changed.
- Final canonical preflight: targeted 561 tests and full 712 tests both returned exit 0; status=preflight_ok, 65 runtime files, profile_aligned=true, service_aligned=true, installed=false and switched=false. Durable receipt: `preflight-final/rollout-oyuvyot4/receipt.json`. The earlier failed checks remain retained rather than overwritten.
- Full-repository Ruff still reports two pre-existing unused imports in tests/test_host_db_restore.py and tests/test_host_predecessor_migration.py; these untouched WIP files were preserved. All changed Python files pass scoped Ruff.
- Preservation check: codex-tossapp-review remains ready=true, gpt-6.1-sol/high, review_only, host_capabilities=[], with only ai-tarot/src and server/src allowed. No command policy file exists. `preservation-audit.json` records the check.
- Read-only management UI check: existing page and admin.js return HTTP 200; host-command-admin.js returns 404 on the old live release, so its activation is still pending. `live-ui-readonly.json` records the check.
- This pass authorizes source fixes and checks only. Runtime switching, host policy registration and actual external publication remain separate approval steps. It does not authorize Docker/DB mutation, service restart, paid agent invocation, Git commit or push.

## Approved runtime activation packet (historical)

| Item | Snapshot | State |
| --- | --- | --- |
| Live | f27541a01e22e5499614 | unchanged |
| Candidate | ce88340086af9f5e1bf2 | preflight_ok |

At approval time the candidate included the five host-command tools and their admin approval UI, while the live tunnel served the old release. The approved switch is recorded below. The host command policy remains absent/disabled until separately approved and configured.

After explicit service-switch approval, run the existing rollout command in an operator TTY:

```bash
/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/host-command-hardening-d6d35642/activation
```

The helper reruns mandatory checks and pins all source/control files before switching. It retains the old snapshot, stops/starts the existing tunnel (including its managed Codex app-server) and restarts the recognized admin service. Remote connections can briefly disconnect. No Docker/DB/schema, credentials, network/listening configuration or unrelated sessions may be changed. Do not issue a real build/upload/invite command as part of runtime activation. After installation, verify the actual running snapshot, new tool discovery and admin UI; ChatGPT plugin Refresh may still be required for client metadata.

## Next owner/operator steps

1. Verify current WIP and host profile readiness, review these exact files and cross-service effects; do not reset, stage or commit other agents' work wholesale.
2. Use the recorded host SDK tests, actual SDK stdio fixture and independent final review results; resolve any new blocker before deployment. Preflight pins both source and test/control files and does not install or switch services.
3. Confirm the Toss upload wrapper is an owner-approved script, with a reproducible input artifact SHA and remote release attestation. Never assume its test command uploads the same .ait from ChatGPT's sandbox build.
4. Only after explicit runtime activation approval, run existing rollout-host-runtime preflight, compare signed input digests, then perform interactive TTY rollout with old release retained. Plugin Refresh afterwards should expose five new MCP tools.
5. Enable an owner-only command policy only after explicit approval and try a read_only/build profile first. Any external test upload, invite, DB change or production release requires separate user authorization.

## Approved host activation and verification

The user explicitly approved the runtime deployment after source integration, independent review and successful preflight. Only immutable runtime installation, the existing tunnel stop/start and recognized admin restart were authorized. Command policy registration, real builds/uploads/invites, Docker/DB/schema changes, paid agent tasks and Git writes were excluded.

- Before switching: the reviewed snapshot/source/control hashes were unchanged; both mandatory checks were rerun (561 targeted and 712 full, exit 0); active MCP operations were 0. The operator shell was outside the tunnel's process ancestry. Existing protected configuration, test container identities and CLI process generations were recorded.
- The rollout ran in a real operator TTY through `script`; ROLLOUT was entered after those checks under the user's approval. Terminal output and the sanitized receipt are retained privately, including nonzero verification status.
- Original receipt: `activation/rollout-5ufz45pn/receipt.json`. installed=true, switched=true, runtime_verified=true, tunnel_healthy=true, admin_status=admin_ready. Exit 2/status=verification_required records awaiting connector discovery, not a failed service switch. The receipt has not been rewritten.

| Component | Snapshot | PID | State |
| --- | --- | --- | --- |
| MCP | ce88340086af9f5e1bf2 | 125663 | verified |
| Tunnel | ce88340086af9f5e1bf2 | 125648 | healthy |
| Admin | ce88340086af9f5e1bf2 | 125838 | admin_ready |
| Previous release | f27541a01e22e5499614 | n/a | retained |

- Post-switch verification proved actual argv/start ticks, profile/unit alignment, 65 installed file hashes and private modes. The management page, admin.js and host-command-admin.js return HTTP 200 and exactly match installed bytes. Authenticated host-command-plans for tossApp returns an empty list.
- The actual installed MCP SDK/stdio server registers 54 tools including all five new command methods, with exact project enums and no extra approval fields. list_host_command_profiles returns enabled=false, HOST_COMMAND_CONFIG_UNAVAILABLE and profiles=[]; a chat-supplied approved field is denied. No command was executed.
- The SDK probe uses its own XDG state to preserve real task histories. Its empty activation-receipt directory correctly makes the copied profile view ISOLATION_UNVERIFIED. An initial verification assertion expected ready=true there and was corrected after inspecting that state-root distinction; production behavior was not changed. The real personal-ops-tunnel list_agent_profiles call separately proves codex-tossapp-review is ready=true, gpt-6.1-sol/high, with the original two source paths.
- Actual Chromium browser verification passed: connected host, tossApp selection, new host-command approval section visible, empty pending plans, disabled approval button, empty token inputs, zero page errors and zero unexpected mutation requests. The SDK Python lacked Playwright; the existing tossApp JavaScript Playwright installation was used without dependency installation or project edits.
- Protected host runtime/admin token/runtime key/project configuration files are byte/mode-identical. All three tarororo-test containers retained ID/image/start time and mounts. No DB/schema or Docker mutation command was issued.
- CLI observation: ten baseline processes retained their generation, including shared Codex PIDs 233826 and 233914; two baseline PIDs were no longer running at the post-check. Their cause is not inferred from snapshots. No broad CLI stop, resume or session restart was performed; only the approved tunnel/admin services were controlled. The per-process observations remain in activation-verification.json.
- Evidence: activation-before.json, activation-approval-confirmed.json, activation-terminal.log, activation-verification.json, activation-connector-readonly.json and activation-browser-ui.json, under the private evidence root above. All are current-user-owned regular files with mode 0600.

### Remaining client and policy steps

The connector's passive catalog still reports awaiting_discovery and the current ChatGPT tool metadata lacks the five new command descriptors. The host operator cannot refresh this chat's plugin metadata from the terminal. A user Refresh request is pending; after it, check the actual connector's command tool schemas and read-only profile result. Never substitute isolated SDK registration for that final client proof.

The owner-only command policy remains absent. Runtime activation does not authorize arbitrary host shell access, a real upload or policy registration. Prepare/review the exact trusted wrapper and artifacts before requesting that separate authorization. Historical source/preflight failures and receipts above remain preserved.

### Later user request: fixed tossApp TEST profile registration

The user subsequently requested registering a pinned `tossApp-deploy test`
profile in the owner-only policy. Registration is now authorized; a real upload
is not. The legacy test script rebuilds and invites, so a separate reviewed
fixed-artifact wrapper was installed. Two configuration drafts and immutable
artifact copies passed profile freezing and offline preflight. A direct bundle
comparison narrowed the UI difference to the requested card-button order fix.
The user chose option 2/current build, SHA c53756ac..., and the real owner-only
policy now enables tossapp-deploy-test. Actual connector discovery, installed
runtime freezing and wrapper check all passed. Registration/selection permission
is complete; no real upload was performed.

Actual connector tool discovery is now verified. Preparation, precise hashes,
native CLI proof, validation limits, preserved state and remaining activation
steps are in `2026-10-14-tossapp-test-upload-profile.md`. The live runtime and
original deployment script were not changed for this preparation.

The user later authorized one real TEST upload. That plan received a genuine
one-use approval through the authenticated management web API and was executed
by the actual tunnel. It exited 1; its owned cgroup termination was verified.
Both the independent Toss console connector and a read-only same-host-key API
request report the chosen deployment ID already CREATED/version 20261014-21.
No retry or artifact/UUID change occurred. See the detailed handoff for the
failed-attempt versus existing-provider-state distinction and remaining safe
error-code observability improvement.
