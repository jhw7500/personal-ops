# tossApp fixed TEST upload profile — 2026-10-14

STATUS: actual MCP TEST upload succeeded on 2026-10-15 KST. Following the failed
attempt recorded below, the user said `업로드해야지`, authorizing a fresh upload.
Official AIT 3.2.0 packaging generated a new ID while preserving all 106 selected
frontend files and app metadata. The upload-only profile pins SHA 26e47f68...; actual
MCP execution exited 0 with verified termination. Toss reports CREATED/version
20261015-22, and the console MCP sent the requester a TEST push. Phone QA has
not been observed. See [fresh upload results](2026-10-15-tossapp-test-upload.md).

CURRENT PROFILE CORRECTION: `tossapp-deploy-test` now executes the existing
`tossApp-deploy test` command, including fresh build, TEST upload and SELF
invitation. The earlier fixed-artifact program is preserved as
`tossapp-upload-test`. A subsequent separately authorized full MCP run completed
build, TEST upload and SELF invitation for version 20261015-23. The newly built
AIT intentionally replaces ai-tarot/tarororo.ait; original fingerprints below
are historical, while archived frozen copies are preserved. See
[the correction](2026-10-15-tossapp-deploy-profile-correction.md) and
[full live result](2026-10-15-tossapp-full-test-deployment.md).

## Authorization and selected artifact

The user originally requested registration in
`/home/jhw/.config/chatgpt-coding-mcp/host-commands.json`. This authorizes the
fixed profile configuration and its preparation. Do not ask for that permission
again after the artifact selection arrives. That request did not authorize a real upload,
invitation, release, rebuild, Docker/DB operation or service restart.

The user answered `2번`, selecting the current build. On the follow-up asking what
actually differs, the AIT contents were compared after normalizing actual Vite
asset hash filenames/references. The only frontend code change is the card
action order: reviewed candidate `random -> reselect -> advance`, current build
`reselect -> random -> advance`. Handlers and disabled conditions are unchanged.
All other normalized frontend assets, CSS, API configuration and sample video
bytes match. Bundle JSON metadata differs only in deploymentId; its byteLength,
SDK and configuration are identical. The compressed .ait sizes differ by 12 bytes.
The current build also matches the newer button-order-v2 candidate's frontend
content. The recommendation is therefore corrected to **current-build** to
retain the requested button-order fix. Registration is completed for that
selected artifact; an actual upload remains a separate action. Exact read-only comparison
evidence is `artifact-functional-comparison.json` in the evidence directory.

| Choice | Original artifact | SHA-256 | Deployment ID |
| --- | --- | --- | --- |
| reviewed-candidate | `build/android-test/gone-20261014-api-aligned/tarororo.ait` | `3335a614b2a0de5aa15d2d66e6fe62c8b34412c53f0b6d9fe1b31c9c30878938` | `01a13881-9f25-7b38-b547-0fbad316f71c` |
| current-build | `ai-tarot/tarororo.ait` | `c53756ac323afe02952c19553aac2263068d780fa80384fd89a228681b6e21e9` | `01a138b1-5480-75f7-9a06-2cf2f8669c58` |

Both original files remain mode `0664` with unchanged hashes. Separate copies
were prepared under `tossApp/build/host-command-artifacts/<SHA>/tarororo.ait`,
mode `0400`. The initial policy selected the current-build copy. Its replacement
for a fresh deployment ID is documented in the 2026-10-15 upload results;
the original copies remain preserved.

## Fixed execution boundary

Profile ID: `tossapp-deploy-test`. The lowercase spelling is required by the
host profile ID validator; the project remains `tossApp`.

The existing `/home/jhw/.local/bin/tossApp-deploy test` runs a build and sends an
invitation. It was inspected and preserved, and is not the registered program.

The new standalone source is `chatgpt-mcp/toss_upload_command.py`, tested by
`chatgpt-mcp/tests/test_toss_upload_command.py`. It uses the official CLI's fixed
`deploy --profile tarororo --location <sealed-artifact> --scheme-only` flow.
There are no build/invite/review/release commands or chat-supplied arguments.

```text
fixed profile + one-use approval
             |
             v
sealed wrapper + sealed Node
             |
             v
private CLI + hashed dependency manifest
             |
             v
sealed AIT -> upload-start -> PUT -> upload-complete -> status
```

- Installed program, mode `0700`:
  `/home/jhw/.local/share/personal-ops-host-commands/tossapp-deploy-test-b9924723afcc40bac4b3`.
- Program SHA-256:
  `b9924723afcc40bac4b3eaed4caf4fa2a8ca01cfa6cd2fd87eefe02f56a498ba`.
- Official `@apps-in-toss/cli` version `3.2.0`, private dependency tree:
  `/home/jhw/.local/share/personal-ops-host-commands/ait-3.2.0-c851e46bd6ffb05475df`.
- Manifest SHA-256:
  `c851e46bd6ffb05475df56a76b49ff76bf25646e3a0069b65be44a38b1f44335`.
- Dependency closure: 140 packages, 3,390 regular files, 28,949,315 bytes. Files
  are `0400`, directories `0700`, original nested package paths preserved.
  The absent optional peers `magicast` and `@babel/preset-env` are unused by
  this deploy path; actual CLI startup/upload-path proofs passed without them.
- Node path: `/home/jhw/.nvm/versions/node/v24.12.0/bin/node`; SHA-256:
  `16143bdaa79716e871d3d9b2f50ce680bca293eba7f0c3fc1d004ed2258fc839`.
- Credential source: existing `/home/jhw/.ait/credentials`, profile `tarororo`.
  No key is passed in argv/environment/output. Preflight `check` does not read
  credentials. A real `test` checks the private credential file before the CLI.
- Receipts: `/home/jhw/.local/state/personal-ops-host-commands/tossapp-test`,
  directory `0700`, each safe receipt `0600`. Raw CLI/provider messages are
  discarded; child stdout is bounded by `RLIMIT_FSIZE=4096`.
- CWD is the private toolchain for the child; environment is rebuilt without
  `NODE_OPTIONS`, proxy settings or repository configuration hooks.
- Wrapper and Node/AIT are sealed before execution. The child inherits the AIT
  descriptor and reads it through a private `.ait` symlink; metadata, stat and
  stream reopen the same fixed bytes even if the source file changes.
- The SDK Python lacks Python memfd/fcntl bindings. The narrow Linux libc and
  constants fallback is tested and still verifies the actual sealing flags.

An exit code zero is only `cli_reported_complete`. Receipts explicitly set
`remote_digest_verified=false`: the official CLI does not return an independent
provider digest. The private link derives from the pinned bundle identity; it
is not proof that the provider stored that bundle. Check the actual console
result after a separately authorized upload. Cancellation cannot undo uploads.

## Configuration and applied registration procedure

Evidence directory:
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/tossapp-upload-profile-h_1ymx3u`.

The two owner-only draft files are:

- `host-commands-reviewed-candidate.json`
- `host-commands-current-build.json`

Each draft has version 1, enabled true, one `tossapp-deploy-test` profile,
`effect=external_publish`, `cwd=.`, timeout 240 seconds, a pinned executable
SHA, fixed argv including Node/toolchain/artifact SHA and deployment ID, and
one exact repo-relative artifact input SHA. Both passed `freeze` and
`verify_frozen` with every registered project root included. Both installed
wrapper `check` invocations returned `preflight_ok`, without reading keys,
invoking the CLI or contacting a provider.

The following registration procedure was completed after the user selected 2:

1. Recheck the selected original/copy/program/manifest hashes and modes. Do not
   silently accept a newly rebuilt bundle under the same original filename.
2. Verify the real policy is still absent. If another session created it, read
   and preserve its profiles, using a digest comparison before replacement.
3. Install only the selected validated draft as the real policy, regular
   current-user-owned file `0600`, without overwriting a concurrent change.
4. Re-run installed-runtime `freeze`/`verify_frozen` and call actual connector
   `list_host_command_profiles(project="tossApp")`; expect enabled true and
   `tossapp-deploy-test`. No service restart is needed for the policy read.
5. Document activation. Do not execute `test` or create an external upload just
   to verify registration. Actual execution still requires the existing fresh
   one-use management-web approval of a prepared operation.

All five new host-command connector tools are now available in the actual
ChatGPT tool catalog. Before registration, a real `list_host_command_profiles`
call reported `HOST_COMMAND_CONFIG_UNAVAILABLE`. After registration it reports
`enabled=true` and the selected `tossapp-deploy-test` profile.
This resolves the previous client-discovery verification gap. The immutable
runtime `ce88340086af9f5e1bf2` was not replaced or restarted for this work.

## Verification and preservation

- New tests: 11/11 passed under both system Python 3.10 and SDK Python 3.12.
- Existing host-command focused tests: 72/72 passed under SDK Python; together
  with the new tests, 83 distinct focused tests passed.
- Changed-file Ruff and whitespace checks passed, including the recorded final
  check. No new dependencies were installed with a package manager.
- Code reviewer: APPROVE; architect: CLEAR; no remaining blocking findings.
- `native-cli-proof.py` is retained as a private regular `0600` file. It and
  `native-cli-proof-final.json` were independently compared. Both actual AIT
  candidates passed the official CLI 3.2.0 upload-path test.
- The proof uses fake credentials and a fetch transport stub. Child-only
  seccomp denies socket/connect/socketpair, with a real Node socket attempt
  returning EPERM. After the CLI reads metadata, the fixture source is replaced;
  the PUT stream still has the reviewed SHA and byte count. PUT has no API-key
  authorization header. No actual provider HTTP request occurred.
- The broad SDK suite reached the configured 180-second limit before completion;
  it has no completed full-suite pass for this stage. Its partial private log
  is retained as `full-sdk-suite.log`. Focused verification results are recorded
  in `test-results.json`; do not substitute the earlier rollout's full-suite
  pass for a fresh completion of this attempted run.
- `preservation-audit.json` verifies original script/artifact hashes and modes,
  existing protected configuration hashes/modes, and all 16 agent-proof source
  hashes. They are unchanged. Existing WIP, Git history/index, containers, DB
  data, keys and running production services were not edited by this work.

During preparation/registration, no real upload/invitation/rebuild, paid model
invocation, Docker/DB mutation, Git commit/push or service restart occurred.
Registration and artifact selection are completed; do not repeat their
permission questions. The subsequent real attempt is recorded below.

## Live registration verification

- Real policy: `/home/jhw/.config/chatgpt-coding-mcp/host-commands.json`, regular
  current-user-owned file `0600`, SHA-256
  `d16fb9822ba102d83a426d11587de74ed9a8789c46e0c52ccd18a8c4c5fa558c`.
- Publication used a fsynced temporary file and atomic no-overwrite hard link;
  a concurrent policy would have been preserved. The final file has one link.
- Only `tossapp-deploy-test` is registered, for `tossApp`, effect
  `external_publish`, timeout 240 seconds. Its 19 argv entries and one input
  pin the chosen `c53756ac...` artifact, deployment ID, wrapper, Node and CLI
  toolchain. The original default build remains unchanged.
- The actual running MCP PID 125663 points to the immutable
  `ce88340086af9f5e1bf2/server.py`. That installed runtime's
  `HostCommandProfiles.freeze` and `verify_frozen` passed against the real
  configuration. Installed wrapper `check` returned `preflight_ok` and
  `external_attempted=false`. No service restart was needed.
- During registration, existing credential directory/file owner and
  `0700`/`0600` metadata were checked; operating key contents were not read or
  used in that stage. They were later used by the authorized upload attempt
  and its read-only provider status check.
- Actual connector `list_host_command_profiles(project="tossApp")` returns
  `enabled=true`, profile ID `tossapp-deploy-test`, label
  `tossApp TEST upload current-build [c53756ac]`, effect `external_publish`.
- Actual connector `list_agent_profiles(project="tossApp")` still returns
  `codex-tossapp-review`, model `gpt-6.1-sol`, effort high, `ready=true`.
- Activation evidence is in `activation-current-build-7zr044q3/` beneath the
  evidence directory: `before-install.json`, `host-registration.json` and
  `connector-verification.json`. Earlier pending-state records remain as
  historical evidence and were not overwritten.

Next user-facing prompt for the tunnel chat:
`tossApp의 tossapp-deploy-test 프로필로 TEST 업로드 계획을 만들어줘`.
That prepares an approval-bound plan; it does not upload the bundle. Execute
only after separate actual-upload authorization and its fresh one-use approval.
The profile keeps this exact selected build; future rebuilt artifacts require
a separately verified profile update.

## Approved live attempt and existing TEST version

The user subsequently said `승인`, authorizing the actual TEST upload of option 2.
The host operator prepared a fresh plan through the real tunnel, rechecked the
frozen profile/input/config hashes, and applied only that plan's approval through
the existing authenticated loopback management web endpoint. No approval file
was fabricated or edited and no grant or approval policy was broadened.

- Operation ID: `f7664bea80281aaf1f6e5b7521a876ce`.
- Prepared 2026-10-14 14:35:21 UTC; one-use approval through
  `POST /api/host-command-approve?project=tossApp` included the exact plan/source
  digests, typed operation ID confirmation and freshly supplied host admin
  credential. Credential values were never printed or stored in evidence.
- Actual `execute_host_command` queued at 14:40:14.337701 UTC and finished at
  14:40:15.301108 UTC. `get_host_command` returned `outcome_unknown/COMPLETE`,
  `HOST_COMMAND_EXIT_NONZERO`, exit 1, duration 877 ms, stdout 461 bytes,
  stderr 0, `termination_verified=true`. Its one-use receipt was consumed.
- The wrapper's private receipt records the approved artifact SHA/size,
  `external_attempted=true`, CLI exit 1, `CLI_UPLOAD_FAILED`, CLI stdout 67
  bytes, `remote_digest_verified=false`. Its raw CLI error text was intentionally
  discarded, so the original provider error code is not available.
- Independent actual Toss `bundle_list`, workspace 93125 / miniapp 75257,
  finds the selected deployment ID, app `tarororo`, version `20261014-21`,
  `reviewStatus=CREATED`, `isTested=true`, `deployed=false`, SDK 3.2.0.
  Provider registration timestamp raw array: `[2026,10,14,13,35,8]` (no timezone
  was supplied; do not silently format it as a verified UTC/KST timestamp).
- A separate read-only GET using the same host API-key profile returned HTTP
  200 / SUCCESS / CREATED. It made no upload write and exposed no key values.
- Diagnosis: the existing deployment ID supports duplicate-registration
  rejection as the cause, consistent with the provider's documented UUID reuse
  restriction. This is an evidence-based interpretation; the exact discarded
  CLI/provider error code was not directly observed. Do not call the failed
  MCP attempt a successful upload or infer a remote SHA match from the ID.
- The existing TEST version is available. CLI-format entry link using its
  provider-verified identity:
  `intoss-private://tarororo?_deploymentId=01a138b1-5480-75f7-9a06-2cf2f8669c58&host=appsInTossHost`.
  This link was constructed from the official CLI format; no push or invitation
  was sent. Provider `isTested=true` is not direct observation of phone QA.

No blind retry, UUID rewrite, rebuild, invitation, production release,
Docker/DB change, service restart, paid agent invocation or Git write followed.
Original source bundles, deploy script, command policy, other protected config
and all 16 agent-proof source hashes remain unchanged. The profile continues to
pin c53756ac...; a genuinely new console upload needs a newly reviewed artifact
with a new build-generated deployment ID and a fresh approval-bound plan.

Private evidence: `real-test-upload-5jliodia/` under the evidence directory,
containing `reviewed-plan.json`, `web-approval.json`,
`mcp-and-console-result.json`, `same-host-credential-status.json` and
`final-status.json`, each regular owner-only `0600`.

Observability follow-up: preserve only whitelisted HTTP phase/status/provider
error codes in future wrapper receipts, so a failure can be diagnosed without
retaining secret-bearing raw messages. This improvement was not implemented or
rolled out as part of this already-registered fixed-artifact upload attempt.
