# tossApp fresh TEST upload - 2026-10-15

STATUS: uploaded through actual personal-ops-tunnel MCP; Toss compilation
succeeded. Version 20261015-22 is a TEST bundle. Phone QA is not yet observed.

SUBSEQUENT PROFILE CORRECTION: this successful upload used the fixed-artifact
program and a separate console TEST push. The complete `tossApp-deploy test`
command is now registered as `tossapp-deploy-test`; the fixed uploader moved
to `tossapp-upload-test`. No second upload occurred during that registration.
See [current profile mapping](2026-10-15-tossapp-deploy-profile-correction.md).
A subsequent user-authorized execution of the full command successfully built,
uploaded and invited version 20261015-23; see
[the full live result](2026-10-15-tossapp-full-test-deployment.md).

## Authorization and result

The user said `업로드해야지` after the previously approved upload failed with
an existing deployment ID. This authorized a fresh TEST package and its upload,
preserving the chosen option-2 frontend. The original failure remains recorded
in [the profile handoff](2026-10-14-tossapp-test-upload-profile.md); its exact
discarded provider error is still unknown. New-ID success supports the previous
duplicate-ID interpretation but does not recover that original error code.

| Field | Observed value |
| --- | --- |
| App / workspace / miniapp | tarororo / 93125 / 75257 |
| Profile | tossapp-deploy-test |
| New deployment ID | 01a13b30-b63a-71ac-9e95-5a55e3c9ce3e |
| New artifact SHA-256 | 26e47f68e38b9599fe720b101629d44f65f78a06673e44ffe93d30f2d049c9b7 |
| Artifact bytes / mode | 5912594 / 0400 |
| Operation | 0ae26b311fee1a86b2149fac75767af4 |
| MCP final state | succeeded / COMPLETE |
| Exit / duration / termination | 0 / 11023 ms / verified |
| Console version / build | 20261015-22 / CREATED |
| Console TEST preparation | isTested=true after requester-only test push |
| Production deployment | deployed=false |
| Runtime | ce88340086af9f5e1bf2; no switch or restart |

MCP queued at 2026-10-14 16:15:56.593867 UTC and completed at
16:16:07.711428 UTC (2026-10-15 KST). The console's raw registration timestamp
is `[2026,10,15,1,15,57]`; that response does not specify its timezone.

## Exact package preservation

The selected frozen artifact c53756ac... was checksum-verified and parsed using
the pinned official AIT format package. It had no signature. Its 106 frontend
files were extracted to an isolated private staging directory after entry-name,
size and SHA validation. Its exact project-package.json and app config were
retained. A staging-only SDK manifest supplied the original name/version as
metadata; no SDK, project scripts, Vite or dirty working-tree code was executed.
Pinned Node 24.12.0 ran official `@apps-in-toss/cli` 3.2.0 `build` against that
existing web output. The CLI generated the new UUID7 itself.

All 106 frontend files and project-package.json are byte-identical (107 entries).
The only bundle.json change is deploymentId. App metadata, permissions, SDK,
API configuration and total entry count (108) are identical. Every new index
SHA and uncompressed size was verified. Packaging timestamps and ZIP envelope
may differ; the whole-artifact SHA is intentionally new.

New frozen artifact:
`/home/jhw/ai/opencode/projects/tossApp/build/host-command-artifacts/26e47f68e38b9599fe720b101629d44f65f78a06673e44ffe93d30f2d049c9b7/tarororo.ait`.

## Actual host workflow

```text
selected frontend -> official AIT build -> new frozen artifact
                                         |
                                         v
policy pins -> real MCP prepare -> authenticated one-use web approval
                                         |
                                         v
real MCP execute -> succeeded -> independent Toss CREATED
                                         |
                                         v
requester-only console TEST push
```

Only the selected profile's artifact path/SHA, deployment ID, input map and
label changed. The policy replacement used the shared host-config writer lock,
expected digest comparison, private temporary file, atomic rename and fsync.
Policy mode remains 0600. Installed wrapper preflight and installed-runtime
freeze/verify_frozen passed before activation. Old policy SHA d16fb982... was
replaced by 44b9b33787b7b8fd91686cd65a801efae4e0448fa70582a07f37a0a3e895e730.
Executable, Node, dependency manifest and credential pins were preserved.

Actual connector list/prepare/execute/get calls exercised the new profile.
The host used the existing authenticated loopback
`POST /api/host-command-approve?project=tossApp` with a fresh host credential,
exact operation/plan/source digests and operation ID confirmation. No approval
receipt was fabricated, no policy grant widened, and no key was printed.

Wrapper receipt:
`/home/jhw/.local/state/personal-ops-host-commands/tossapp-test/01a13b30-b63a-71ac-9e95-5a55e3c9ce3e-1e718473cbdd332d.json`.
It records the pinned artifact SHA/size, CLI exit 0 and cli_reported_complete.
Independent console bundle_list confirms the same new deployment/version.
The provider does not expose a remote artifact digest, so
remote_digest_verified remains false; it is not upgraded by the ID match.

## Phone entry and remaining limits

The `ait:test-on-device` workflow's console MCP `bundle_test_push` succeeded
for the requester only. These URLs are returned by the tool, not assembled:

- Private link:
  `intoss-private://tarororo?_deploymentId=01a13b30-b63a-71ac-9e95-5a55e3c9ce3e&host=appsInTossHost`.
- [TEST QR in the console](https://apps-in-toss.toss.im/workspace/93125/mini-app/75257/app-build?testDeploymentId=01a13b30-b63a-71ac-9e95-5a55e3c9ce3e&testVersionName=20261015-22).

isTested=true means TEST preparation, not observed phone QA. No review, release,
rollback, other-recipient invitation, Docker/DB action or paid agent run was
performed. The profile now pins an already uploaded ID; a later fresh upload
needs a newly generated and verified package, profile update and fresh plan.
Do not blindly replay this completed operation or reuse its consumed approval.
Whitelisted error-code retention remains a follow-up from the earlier failure.

## Verification and evidence

22 protected files retain exact hashes/owners/modes, including all 16
agent-proof source files, host-runtime config, credentials/admin token, original
AIT, legacy deploy script and installed wrapper. Actual list_agent_profiles
still reports codex-tossapp-review ready=true, gpt-6.1-sol/high and the same two
allowed source paths. MCP PID 125663 and admin PID 125838 still use the same
immutable release. Admin root, the exact operation approval link and
host-command-admin.js return HTTP 200. A link without its required operation_id
correctly returns HTTP 400; it is not a valid UI smoke-test URL.

Private evidence directory (0700; JSON reports are regular owner-only 0600):
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261015/tossapp-fresh-test-upload-IBbYV9`.
It includes official-build.json, package-verification.json, upload-preflight.json,
before/after command policies, reviewed-plan.json, web-approval.json,
mcp-console-evidence.json, preservation-before/after.json, agent-profile-after.json
and final-status.json. Historical evidence is retained.

No production code changed in this upload pass. Prior focused test evidence is
in the profile handoff; no new full-suite pass is claimed. No Git stage, commit
or push was performed.
