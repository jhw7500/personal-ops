# Host agent readiness follow-up — 2026-10-14

CURRENT 2026-10-15: actual codex-tossapp-review readiness is false /
ISOLATION_UNVERIFIED. Both previously genuine receipts now fail their platform
binding because systemd changed from 249.11-0ubuntu3.22 to 249.11-0ubuntu3.23;
the other binding digests and receipt integrity match. No new probes or model
fixture were run in that readonly diagnosis. See
[systemd update diagnosis](2026-10-15-agent-proof-systemd-update.md).
The ready=true activation below is historical.

Latest state: after the user's explicit replacement approval, runtime
`f27541a01e22e5499614` is live and `codex-tossapp-review` is registered with
`ready=true`. The replacement real model fixture passed. The earlier missing
helper failure and source-only records below are historical; the final section
records the completed restricted activation and current limits.

This is the latest continuation of `2026-10-14-agent-delegation-rollout.md`.
The user completed the DB work in the web chat and requested the remaining personal-ops work here.
This run implements and verifies the restricted `review_only` agent path. It does not claim all C3 operations are active.

## Applied scope and current state

- Worktree: `/home/jhw/.local/state/personal-ops-worktrees/agent-readiness-20261014-7hcjb119`.
- Evidence: `/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/agent-readiness-r2zlz1gn`.
- Original HEAD: `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`; no commit/staging in this run.
- All integration uses original file SHA and mode comparisons against `baseline.json`; conflicting WIP must not be overwritten.
- Existing Docker/DB contents, host-runtime config, runtime key, admin token, user aliases and existing Codex/Claude sessions were not changed by this lane.
- Newly created free-test transient services and private fixtures were owned by this test and cleaned up. No delegated Codex model task was executed.
- Runtime switching and permanent profile/helper provisioning still require separate user approval. The previous approval covered release `adc2d2a1093abbe2862b` only.

| Item | Observed state |
| --- | --- |
| Live MCP | `adc2d2a1093abbe2862b`, PID 117524 |
| Live admin | same release, PID 117844 |
| Agent tools | all five present; actual `list_agent_profiles(tossApp)` succeeds |
| Agent profiles | `[]`; not active |
| Admin web | Loopback, LAN and Tailnet HTTP 200 |
| Agent API | unauthenticated 401; authenticated 200, zero tasks |
| Schema 013 | Ready; source checksum matches; 12 prerequisites match |
| Database identity | same container, started `2026-10-12T06:55:37.223222801Z` |
| API / Worker | Running; API Healthy and readiness Ready |
| Monetization | latest actual query `toss-demo`, superseding the earlier `disabled` observation |

The first new-tool call briefly returned initialization-required 409; sequential retry succeeded without a service change. Tool discovery is therefore resolved, not a pending blocker.
Actual API/DB observations above were refreshed at about `2026-10-14T05:02:53Z`. Other web-session deployment changes must not be attributed to this lane.

## Source changes

- `host_runtime.py`: predecessor status recognizes successfully applied 006–013; expected post-migration indexes/013 tables are no longer false collisions. Plan eligibility remains strict. This is a source fix; the old live runtime still uses its old status semantics until switching.
- `agent-bootstrap.c`, `agent_bootstrap.py`: static bootstrap verifies actual HOME, accepts only eight environment keys, waits for the GO handshake, then executes with a sanitized environment. Binary/source hashes are bound to activation evidence. Compiler failures retain private bounded diagnostics.
- `agent_supervisor.py`: one never-reused transient user unit/cgroup per attempt, systemd 249 compatible. PID/start ticks/UID/boot/InvocationID/cgroup are bound; no parent slice or foreign process is stopped. Manager verification runs off the event loop.
- `agent_runner.py`: bounded start and runtime, pending-start cleanup, retained ownership when termination cannot be proved, close retry, private stderr, strict final result schema and supervisor-backed cancellation. Normal task execution and separately approved fixture execution have different entry points.
- `agent_tasks.py`: per-task flock, no stale recovery of another live owner's work, no foreign PID termination, safe bounded events and usage, manager-created broker nonce, actual `source_review` preserved separately from provider-reported `model_report`. A model's validation sentence is never treated as actual test evidence.
- `agent_proofs.py`: schema-v2 content-addressed private receipts require actual isolation/supervision assertions and separately approved smoke evidence, bound to profile, policy, source, helper/bootstrap, kernel and systemd. Old boolean receipts cannot activate a profile.
- `agent_profile_setup.py`, `configure-agent-profile.py`, `agent_smoke.py`: trusted host setup freezes config/source/runtime/model/effort/paths; no probe or model execution at plan creation. Installed immutable runtime plus exact confirmation and explicit smoke approval are required at apply. Nonempty host capabilities are rejected. Failed smoke never enables a profile.
- Every allowed path component is checked with `lstat`; nested parent aliases cannot create an unusable profile.
- `host_runtime_config.py`: common advisory config writer lock and SHA compare-and-swap. Profile setup and legacy TTY/web config writers share this boundary. Legitimate concurrent config changes are preserved. Arbitrary noncooperating same-UID manual replacement is outside the atomicity guarantee; trusted operators should use this writer boundary.
- `agent_host_broker.py`, `agent_broker_server.py`: typed v3-only broker core, live observation/persisted identity comparison, fixed host roots and private task context/lease validation. No context JSON, legacy grant, raw argv or SQL confers authority.
- Installer/trusted manifest now include 57 runtime/dashboard files and all new local imports. Rollout mandatory tests include proof, supervisor, setup, smoke and config CAS.
- `rollout_diagnostics.py`: failed unittest IDs survive in the private receipt; only bounded ASCII identifiers are retained, never raw stderr, exception messages or argv. Exact failure diagnosis no longer requires guessing from a count/hash alone.

## Validation and review

- Final free proof: `final-free-proof.json`. All 16 filesystem/network/control/PID-namespace assertions passed through the actual pinned Codex sandbox and static bootstrap, without contacting a model.
- A controlled live host peer was accessible before sandboxing, then its environ/cmdline/root aliases were inaccessible inside a different PID namespace. Own `/proc/self/environ` remains readable with sanitized environment; do not claim otherwise. The unsafe `/proc/**` glob was removed because the pinned CLI's glob scan failed; literal `/proc` plus actual peer isolation is tested.
- Actual transient-unit fixture: bootstrap environment safe; cgroup empty; descendant terminated; unrelated child alive at proof. The child was separately observed with SID equal to its PID before stop, proving it had called `setsid`.
- Security review initially found four high findings and later two medium findings; fixes and regression tests were reviewed. Final independent verdict: APPROVE for source integration and a restricted profile after a separately approved actual fixture; zero remaining findings in that review scope.
- Ruff and Python static checks passed. Runtime interpreter is the installed SDK Python 3.12, not system Python 3.10.
- Node 24 management UI regression: 24 tests passed.
- First rollout preflight: 459 targeted and 613 full tests passed, then correctly rejected `SOURCE_CHANGED_DURING_CHECK` as final source fixes landed. Receipt `rollouts/rollout-acp2fj6j/receipt.json` is retained.
- Second preflight: 463 targeted tests failed at a legacy no-write assertion because a rejected v3 upgrade created a lock file. Fixed by checking rejection before entering the common writer lock while preserving checks inside it. Related 72 tests passed. Receipt `rollouts/rollout-i_16gx3d/receipt.json` is retained.
- Final frozen preflight: `rollouts/rollout-r3tu7h2f/receipt.json`: **465 targeted + 619 full tests PASS**, `preflight_ok`, candidate `6a21226cd332b242bbfb`, 57 files. `installed=false`, `switched=false`; actual live runtime remains `adc2d2a1093abbe2862b`.

## Next separately approved operation

Proposed first profile: `codex-tossapp-review`, Codex 0.162.1, model `gpt-6.1-sol`, effort `high`, paths `ai-tarot/src` and `server/src`, `review_only`, `host_capabilities=[]`.
The paths exist and current read-only inventory has 175 files / 861,878 bytes. Model work uses a private copy; no original source apply occurs.

Approval should cover installing/switching the exact new runtime candidate and permanently staging this pinned helper/runtime, then **one disposable Codex fixture task with a 60-second runtime budget**. One task can make multiple internal model requests and consumes the account's model usage; it is not a zero-token operation or a guarantee of one API request. Register the profile only after genuine proof and smoke success. No SQL, Docker restart/deployment or automatic source application is included.

Canonical interpreter for every operator command:
`/home/jhw/.local/share/uv/tools/serena-agent/bin/python`.

After the separately approved runtime rollout (existing TTY/ROLLOUT flow), invoke the installed release's setup CLI:

```sh
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python <installed-release>/configure-agent-profile.py prepare --profile-id codex-tossapp-review --project tossApp --model gpt-6.1-sol --reasoning-effort high --allowed-path ai-tarot/src --allowed-path server/src
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python <installed-release>/configure-agent-profile.py apply --plan-id <plan-id> --confirmation <plan-id> --approve-model-smoke
```

Plans expire in 10 minutes; expired plans must be recreated and rechecked. Do not copy/fabricate receipts or bypass the installed-runtime/TTY gate. After activation, prove the actual plugin reports `ready=true`, then prepare/approve/start a normal task using the existing one-use admin flow.

## Explicit remaining work

- New runtime switch, permanent helpers, actual Codex model fixture and registered profile have not been applied in this run.
- Nonempty agent host capabilities and runner-to-broker wiring remain disabled; this source review does not authorize privileged agent execution. Existing direct registered MCP host tools remain a separate path.
- Automatic CAS application remains `CAS_UNVERIFIED`; a review-only agent never writes the original repo. Multifile atomicity or rollback is not claimed.
- Structured result checks do not prove project builds/tests passed. The first profile's source paths alone do not include build dependencies or package configuration.
- Retained task history is bounded and not automatically pruned; a full history needs a separately designed retention/archive operation, not silent deletion.


## Source integration completed

43 source/test/handoff files were integrated into the original checkout with zero baseline SHA/mode conflicts. Original bytes were backed up under the private evidence folder. `source-integration.json` records before/after hashes, modes and unchanged protected-config fingerprints. HEAD is unchanged and no commit was created. The original checkout manifest must equal candidate `6a21226cd332b242bbfb`; this is a source integration result, not a runtime installation or enabled agent.

Stop condition for this run: source, actual free proofs, independent review, all mandatory checks and conflict-safe integration are complete. The remaining runtime switch and actual account-consuming fixture/profile registration are the separately approved next operation described above.

## Approved runtime switch and first model fixture — 2026-10-14

The user's subsequent `진행` approved the exact 57-file runtime
`6a21226cd332b242bbfb`, pinned Codex 0.162.1, and one disposable fixture task with
60 seconds of model runtime. The profile scope stayed `codex-tossapp-review`,
`gpt-6.1-sol`, `high`, paths `ai-tarot/src` and `server/src`, `review_only`, and
empty host capabilities. No Docker/DB operation or original-source application
was included.

- Initial operator attempt: 465 targeted + 619 full tests passed, then
  `HOST_TTY_REQUIRED`. RTK's proxy pipes the child output even when the outer
  tool has a PTY. This attempt did not switch or install anything.
- Canonical retry used `script -q -e -f -a -c '<operator command>'` and a
  pre-created private log, verified the actual TTY prompt, then entered
  `ROLLOUT`. Operator session 35 remained outside the affected service cgroup.
  All 11 projects had zero active recorded jobs before switching.
- Receipt `activation/rollouts/rollout-2xxh5loe/receipt.json` records
  `installed=true`, `switched=true`, `runtime_verified=true`,
  `tunnel_healthy=true`, `admin_ready`, live version `6a21226cd332b242bbfb`.
  Previous snapshots were retained. No DB/schema application occurred here.
- Exit 2 represented passive discovery still awaiting a fresh `tools/list`,
  not an unsuccessful runtime switch. The existing connected MCP subsequently
  answered actual profile, runtime, schema and predecessor status calls.
  Full connector schema refresh is not claimed from these calls alone.
- Management pages returned HTTP 200 through loopback, LAN and Tailnet HTTPS.
- Pinned CLI authentication was confirmed as ChatGPT login, without exposing
  credentials or creating an API key.
- Profile plan `9b1df87845cdc037c12c443e0bc27eea` was prepared and applied with
  exact confirmation and `--approve-model-smoke`. Free isolation/supervision
  passed. One real model task responded, but its shell tool could not start
  because staged `codex-code-mode-host` was missing. No fixture output file was
  created, so the trusted validator returned `SMOKE_FIXTURE_FAILED` and left
  `agent_profiles={}`. A model's response never substitutes for the required
  executed-file proof.
- The failed plan, provisioned runtime, private failure JSON, stderr and
  structured last-message evidence are retained. Do not reuse that plan or
  destination, delete the evidence, fabricate a smoke receipt, or enable the
  profile manually. The one-task model authorization is consumed; another
  model fixture needs a new explicit approval.

Private evidence root for this operation:
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/agent-readiness-r2zlz1gn/activation`.
Reports/logs are current-user-owned regular files, mode 0600, in mode-0700
directories. Tokens, credentials, SQL row data and raw model reasoning are not
included in the public handoff.

## Missing helper repair and verified source candidate

- Codex 0.162.1 resolves the code-mode host next to its executable; PATH alone
  does not repair a missing sibling. The installed upstream package already
  contains a static PIE helper (74,036,112 bytes, no PT_INTERP), SHA256
  `ec71830690a7c0024916ee0c99fbdc635bc6e747ff665ea4f03a8389bbcb6b30`.
  No download, package installation or version upgrade was required.
  Resolver/process-group behavior was checked against upstream commit
  `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`
  ([resolver](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/install-context/src/lib.rs#L176-L205)).
- `agent_probe.py` validates/copies the pinned sibling from an owned regular
  descriptor, detects changes, installs mode 0700 and verifies static ELF and
  exact hash. Missing, linked or changed helpers fail before model execution.
  Partial staging removes only its newly created destination.
- `agent_code_mode.py` runs a fixed, bounded, model-free stdio exchange:
  hello, fresh session, JavaScript echo, exact controlled delegate, expected
  result, orderly shutdown and exit 0. It cannot execute arbitrary delegates.
  Environment, frame bytes/count, deadlines and owned-process cleanup are
  bounded. This is startup/delegation proof, not a claim of model completion
  or Codex parent shell authorization.
- Bounded upstream binding review confirmed a fresh raw V8 context with
  delegated tools and no Node/Deno filesystem/process/network API, and an
  import resolver that rejects imports
  ([globals](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/code-mode-runtime/src/runtime/globals.rs#L15-L54),
  [imports](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/code-mode-runtime/src/runtime/module_loader.rs#L166-L236)).
  Parent-side delegated-tool authorization and the existing OS sandbox remain
  necessary; this source-level check is not an OS isolation proof by itself.
- The offline isolation probe combines the existing actual CLI sandbox checks
  with the new helper execution checks, before creating a model-smoke approval.
  Profile setup also requires the staged helper path and pinned metadata.
- Profile schema remains unchanged. Contract v4 binds the actual helper hash;
  missing/hash/mode/link drift blocks readiness even with older receipts.
  The helper-transport module is included in all three installation,
  immutable-runtime verification and source-contract manifests.
- Actual integrated proof `code-mode-fix/integrated-free-proof.json`: all
  18 assertions passed, including the real pinned CLI's filesystem/network/
  control-socket/private-PID-namespace checks, helper JavaScript/delegation/
  shutdown, and real supervisor termination/descendant/unrelated-process checks.
  Model calls: 0. Disposable native binaries were removed on fixture exit.
- Independent review: APPROVE for this bounded staging/transport/readiness/
  setup/receipt/manifest scope; no remaining findings. Ruff passed. The runtime
  remains pinned to SDK Python 3.12; system Python 3.10 is not the deployment
  interpreter.
- Corrected candidate: `f27541a01e22e5499614`, 58 files. Final preflight evidence
  is `code-mode-fix/preflight/rollout-iv7inqh9/receipt.json`.
  It passed 479 targeted and 633 full tests (`preflight_ok`,
  `installed=false`, `switched=false`), with profile/service command alignment
  verified against the live `6a21226cd332b242bbfb` runtime.
  This candidate is not the live runtime until separately switched.

Final runtime/UI check `code-mode-fix/runtime-admin-final.json` verified the live
snapshot and PID/start-tick identity; loopback/LAN/Tailnet management pages were
HTTP 200 and included the agent UI asset. Agent API was 401 without credentials
and 200 with the private host token, with zero tasks. The connected tool surface
lists all five agent tools and actual `list_agent_profiles` succeeds with an
empty list. The passive tunnel health catalog still reports `awaiting_discovery`;
do not claim a freshly verified full connector schema from cached tool metadata.

Fresh connected-MCP observation at `2026-10-14T05:50:00Z`: API Healthy/Ready,
Worker and DB Running, Schema 013 Ready, 001–013 checksums match, predecessor
`complete=true` for 006–013, zero object/function collisions. DB container ID
and start time are unchanged. Monetization mode is `toss-demo`. These read-only
observations did not alter the completed web-session DB work.

After approving the corrected runtime and one replacement fixture, use a new
profile plan from that immutable installed release. A normal ChatGPT agent
task should be attempted only after actual `list_agent_profiles(tossApp)`
reports `ready=true`. Nonempty agent host capabilities, automatic source CAS,
normal-task approval and broader project build/test access remain outside this
restricted first-profile activation.


### Corrected source integration completed

16 source/test/handoff files were copied into the original checkout with fresh
baseline SHA/mode comparisons and zero conflicts. Old source bytes are retained
under `code-mode-fix/source-backup`; `source-integration.json` records the final
files and protected configuration fingerprints. Original and worktree runtime
manifests both equal `f27541a01e22e5499614` (58 files). No commit/staging occurred.
The live runtime remains `6a21226cd332b242bbfb`; this source integration does not
switch a service or grant another model run.

## Corrected runtime and restricted profile activated — 2026-10-14 15:20 KST

The user explicitly approved the corrected runtime rollout and one replacement
model fixture with a 60-second runtime limit. This approval did not include a
normal project model task, Docker/DB changes, original-source application, or
expanded host capabilities.

- The original source matched the approved 58-file candidate
  `f27541a01e22e5499614`; Git HEAD stayed
  `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`. All 11 projects had zero active
  recorded jobs before the switch. The operator remained in session-35.scope,
  outside the affected service cgroups.
- The canonical real-PTY rollout repeated the mandatory checks: 479 targeted
  and 633 full tests passed (one explicit actual-probe skip; actual native
  proofs ran separately). The actual `ROLLOUT` prompt and exact approved
  candidate were verified before confirmation.
- Receipt `retry-20261014-0611/rollouts/rollout-z4nj6fx1/receipt.json` records
  `installed=true`, `switched=true`, `runtime_verified=true`,
  `tunnel_healthy=true`, `admin_ready`, current runtime
  `f27541a01e22e5499614`, previous runtime `6a21226cd332b242bbfb` retained.
  The tunnel PID/start ticks were `2831177/17367229` and the MCP child
  `2831202/17367232` at final verification.
- Exit 2/`verification_required` refers to passive health discovery awaiting
  a fresh tools-list observation. It does not mean the host runtime failed.
  Do not fabricate a complete connector-schema verification. The actual
  connected tool surface has all five agent tools, and a real
  `list_agent_profiles(tossApp)` call reports the enabled profile ready.
- New plan `2747fb7d288a503d8689c6bf45045307` was prepared and applied from the
  verified immutable release with exact confirmation and explicit model-smoke
  approval. Exactly one replacement model fixture was invoked; no automatic
  paid retry or normal source task was started.
- Apply exited 0 with `profile_enabled`. Actual `fixture-result.txt` contains
  exactly `approved fixture executed\n`. The input was unchanged, structured
  final output named the actual changed file, and process termination was
  verified. The profile was registered only after these actual observations.
- Private isolation receipt
  `ea36695e6924d134bc40fc1cf25621001a4efec12a5875b55841ce81a39683e5`
  and smoke receipt
  `2f35465eb21b799733d02a72da6f47348cbca8689ddfdae861ab68c5c485994d`
  independently verified against the current profile/policy/source/helper/
  bootstrap/platform bindings. All 18 isolation assertions, all supervisor
  assertions and all five smoke assertions passed.
- Current profile: `codex-tossapp-review`, Codex 0.162.1,
  `gpt-6.1-sol`, effort `high`, paths `ai-tarot/src` and `server/src`,
  `review_only`, `host_capabilities=[]`, `ready=true`, `reason=null`.
- Only `agent_profiles` changed in the v3 configuration. Every other envelope
  component fingerprint and the admin token were preserved. Existing WIP and
  old runtime/failure evidence were retained; no commit/staging occurred.
- Management pages returned HTTP 200 on loopback, LAN and Tailnet HTTPS and
  included the agent UI asset. Agent API returned 401 without credentials and
  200 with the private host token; no normal project tasks have been started.
- Actual connected MCP reads at `2026-10-14T06:19:59Z`: API Healthy/Ready,
  Worker and DB Running, Schema 013 Ready, all 12 prerequisites match and
  source checksum matches. DB container ID/start time and API/worker image
  IDs match the previous observation. Monetization remains `toss-demo`.
  No Docker/DB operation was executed by this activation run.

Evidence is under
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/agent-readiness-r2zlz1gn/activation/code-mode-fix/retry-20261014-0611`.
It includes the actual TTY logs, approval scope, before-registry component
hashes, fixture invocation, proof/UI verification, connected MCP results,
live process identity and final audit. All reports/logs are current-user-owned
regular 0600 files in 0700 directories; no credential values or raw reasoning
are in this public document.

### Continue in ChatGPT

The restricted agent is now available through `personal-ops-tunnel`. Ask it to
prepare an agent task for `tossApp` using profile `codex-tossapp-review` and a
concrete objective. The existing normal-task flow is still prepare, one-use
management-web approval, start, and status/result retrieval. A profile setup
fixture is not approval for a new normal task.

Normal tasks work on private copies of the two allowed source directories and
return reviewable changes/results. Original-source CAS remains
`CAS_UNVERIFIED`; nonempty agent host capabilities and runner-to-broker wiring
remain disabled. Existing registered Docker/DB MCP operations use their own
host workflow. Project build/test success and arbitrary host operations are
not established by this small activation fixture.


Independent final verifier: PASS. It independently checked 58/58 manifest hashes,
42 trusted guard files, current profile settings, both bound receipts, actual
fixture bytes, all 18 isolation/5 supervisor/5 smoke assertions, preserved v3
registry components, and matching live PID/start ticks. The connected readiness
result was corroborated from the private root-call artifact; the verifier did
not make another model/API call. The historical rollout exit 2 remains recorded
accurately and full passive connector-schema discovery is not claimed.
