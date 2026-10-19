# tossApp full TEST deployment - 2026-10-15

STATUS: the actual `tossapp-deploy-test` MCP profile successfully ran the
existing `tossApp-deploy test` script end to end: typecheck/build, new AIT
packaging, TEST upload/compilation and SELF invitation. Independent Toss console
lookup confirms version 20261015-23 CREATED/isTested=true. Phone receipt and QA
were not observed; this is a TEST deployment, not a public release.

## Authorization and observed execution

The user explicitly said `진행해서 배포까지해` after the complete command was
registered. This authorized execution of that existing TEST workflow including
its SELF invitation. No repeated permission request was needed. The host used
the real authenticated one-use management-web approval and actual tunnel MCP
execution; the approval mechanism was not bypassed or weakened.

| Field | Observed value |
| --- | --- |
| Project / profile | tossApp / tossapp-deploy-test |
| Executable / argv | /home/jhw/.local/bin/tossApp-deploy / test |
| Script SHA-256 | 2b7043727b73c0c1ff63e91761e346b400276acb431decde686a8c27762794dc |
| Operation ID | 4fefa8a43502714f01bd9d32f5801b8b |
| Plan digest | 013008d7de8174b02235b03921f8bc31990dc4e35d102220a21b37d6be1e3e92 |
| Source digest | 880402d52d94a76f39b4f6ce70ea21500da000f175aeb50cfb64b2ef96c7e6d3 |
| MCP outcome | succeeded / COMPLETE / exit 0 |
| Duration / termination | 15762 ms / verified |
| Captured numeric output sizes | stdout 3044 bytes / stderr 332 bytes |
| App / workspace / miniapp | tarororo / 93125 / 75257 |
| Deployment ID | 01a13cfa-90f6-7b6c-8b45-cd0f7fcc1da5 |
| Version / console build | 20261015-23 / CREATED |
| Console TEST preparation / public deployment | isTested=true / deployed=false |
| New AIT SHA-256 | dba4d2a4de4d901a9907a132f42755ee164542607b85ba82d912d2bd9e84250d |
| AIT size / mode | 5912599 bytes / 0664 |

Actual MCP queued at 2026-10-15 00:33:31.769718 UTC and completed at
00:33:47.632450 UTC. The script recorded upload at 00:33:47.315823 UTC and SELF
invitation at 00:33:47.618242 UTC. The console registration timestamp was the
raw array `[2026,10,15,9,33,36]`; the response did not specify its timezone.

## Complete path that was validated

```text
real MCP prepare -> authenticated one-use web approval -> real MCP execute
                                                               |
                                                               v
                                    npm typecheck + Vite + AIT build
                                                               |
                                                               v
                                    new .ait -> upload + compilation
                                                               |
                                                               v
                                    same deployment ID -> SELF invitation
                                                               |
                                                               v
                                    MCP succeeded + termination verified
```

The private frozen plan matched the registered script's exact SHA, `["test"]`
argv and ten pinned build configuration inputs. The host freshly read the admin
credential and sent the exact operation/plan/source digests and operation ID
confirmation to `POST /api/host-command-approve?project=tossApp`. HTTP 200 returned
approved=true, after which real execute_host_command queued the operation.
No token values were printed or saved in evidence.

The new AIT differs from the previous artifact hash/stamp. Its parsed AIT ID,
the actual CLI-returned link saved in last-test.json, and the independently
queried provider deployment ID all agree. The invitation timestamp belongs to
this same execution and ID; the provider independently reports isTested=true.
No extra console MCP test push was sent merely to validate the host's invitation.

The runner retains numeric output sizes, not raw logs. The 332 stderr bytes
were not inspected; they are not described as a particular warning or error.
The command exited 0 and provider compilation succeeded. No independent remote
artifact digest is available, so remote_digest_verified remains false.

## Phone entry

The following test link was returned by the actual CLI and saved by the script:
`intoss-private://tarororo?_deploymentId=01a13cfa-90f6-7b6c-8b45-cd0f7fcc1da5&host=appsInTossHost`.

The host SELF invitation was accepted; actual phone receipt/test behavior is
not observed. The script's status now reports this upload and invitation.
[Toss console bundle list](https://apps-in-toss.toss.im/workspace/93125/mini-app/75257/app-build).
No manually assembled version-specific QR URL is claimed.

## Preservation and future web use

All 22 protected files retain exact hashes/owners/modes, including both existing
scripts, command/runtime policy, admin/API credential files and all 16
agent-proof source files. All 277 inventoried frontend/public/script/contract/
server source files retain exact bytes/owners/modes. The same frozen command
profile passes post-execution validation. The authorized build intentionally
updated dist/build caches and ai-tarot/tarororo.ait plus the last-test record.
No source edits or Git stage/commit/push occurred. OAuth may refresh normally
through the host's existing invitation helper; no credential reset was requested.

MCP PID 125663 and admin PID 125838 still use immutable release
ce88340086af9f5e1bf2. Admin root and host-command-admin.js return HTTP 200.
No service restart, Docker/DB operation, migration, paid agent call, console
review or public release was performed.

This removes the earlier full-live-run validation gap in
[the correction handoff](2026-10-15-tossapp-deploy-profile-correction.md).
Web chat can prepare a fresh `tossapp-deploy-test` plan, obtain the existing
one-use web approval and execute the same whole command. It does not need a
separate web Toss console plugin: the host invitation helper uses existing
host OAuth. The consumed approval and completed operation must not be replayed.
Critical build-recipe changes still require an owner-reviewed profile hash
update. No E2E or phone QA completion is claimed from the build alone.

Private evidence directory:
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261015/tossapp-full-test-deploy-s9pu_l3x`
(0700; JSON reports regular owner-owned 0600). It includes protected/source/Git/
deployment baseline records, frozen-profile-before.json, reviewed-plan.json,
web-approval.json, mcp-console-evidence.json, protected-after.json,
source-after.json and final-status.json.
