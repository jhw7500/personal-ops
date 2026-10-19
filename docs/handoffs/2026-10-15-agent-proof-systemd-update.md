# Agent readiness after systemd update - 2026-10-15

STATUS: actual personal-ops-tunnel list_agent_profiles(tossApp) reports
codex-tossapp-review ready=false / ISOLATION_UNVERIFIED. Readonly diagnosis
identified systemd version drift as the sole proof-binding difference. The
completed host-command TEST deployment is a separate execution path.

## Observed cause

| Binding | Recorded proof | Current host |
| --- | --- | --- |
| systemd | systemd 249 (249.11-0ubuntu3.22) | systemd 249 (249.11-0ubuntu3.23) |
| kernel | 6.8.0-138-generic | 6.8.0-138-generic |
| machine | x86_64 | x86_64 |
| uid | 1003 | 1003 |

Installed runtime ce88340086af9f5e1bf2 was used for proof_binding and
verify_receipt. Both current receipts have valid content-addressed hashes,
mode 0600/current-user ownership, a private 0700 receipt root and complete
true evidence assertions. Their profile, isolation contract, source contract
and bootstrap digests match current values. Only platform.systemd differs.

- Isolation receipt: ea36695e6924d134bc40fc1cf25621001a4efec12a5875b55841ce81a39683e5,
  recorded 2026-10-14T06:19:27.830642+00:00.
- Smoke receipt: 2f35465eb21b799733d02a72da6f47348cbca8689ddfdae861ab68c5c485994d,
  recorded 2026-10-14T06:19:54.191034+00:00.

verify_receipt currently returns false for both. AgentProfiles.readiness
checks isolation first, explaining why the tool shows ISOLATION_UNVERIFIED
rather than the additional stale smoke result. This is an observed binding
mismatch; no new isolation/supervision or model test was executed in this
diagnosis, and current isolation correctness is not claimed.

The profile still pins Codex 0.162.1, model gpt-6.1-sol/high, review_only,
allowed paths ai-tarot/src and server/src, with host_capabilities empty.
No receipt, configuration, executable or source was edited, no service was
restarted, and no paid model invocation or Docker/DB action was performed.

## Next boundary

Before agent delegation is used again, current-environment isolation and
supervision evidence plus current model-smoke evidence must be produced by
the trusted installed setup path. Existing receipts must not be edited or
rebound as if new checks had run. A new account-consuming model fixture needs
separate explicit authorization; previous single-fixture approval is consumed.
No new fixture approval was requested or assumed during this status diagnosis.

Existing host commands do not require these agent receipts. Their full TEST
build/upload/SELF invitation result remains documented in
[the live deployment handoff](2026-10-15-tossapp-full-test-deployment.md).

This diagnosis supersedes the historical ready=true statement in
[the earlier agent activation handoff](2026-10-14-agent-readiness-followup.md)
for the current host. It does not independently recheck the web-reported
API/Worker/schema/toss-demo status.
