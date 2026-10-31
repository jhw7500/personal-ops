# Git Commit Access Implementation Plan

Goal: one explicit owner approval permits local commits in an exact repository and branch for at most 86,400 seconds; no push or history rewriting.
Architecture: pure fail-closed policy + trusted private store + existing GitWorkflows + authenticated MCP App approval UI.
Tech Stack: Python standard library, existing MCP App UI and native Git workflow.
Spec: docs/superpowers/specs/2026-10-28-git-commit-access-design.md

## Global constraints
Preserve unrelated WIP, existing source pinning, message contract, workspace/path checks, staged-byte checks and empty-index validation. Never derive Git permission from generic host access. Do not modify installed immutable runtime, private live approvals or grants. Deployment uses the existing separate owner-reviewed rollout. No Codex invocation.

## Review focus
Malformed or client-supplied grant data must not become authority. A different repo inode/git dir, branch or runtime must not inherit approval. Expiry and revocation prevent future admission without denying that an admitted bounded process may finish. Unexpected HEAD movement invalidates a grant. Any failure must retain an accurate Git result and avoid contaminating unrelated staged/WIP files.

## Task 1: pure policy draft
Files: proposed chatgpt-mcp/git_commit_access_policy.py, tests/test_git_commit_access_policy.py.
Interface: evaluate_stored_commit_grant(grant, context, action, now_epoch_seconds, now_monotonic_ns) -> immutable GrantDecision. Input records/context MUST be obtained from the trusted host adapter. It cannot issue, persist, revoke or renew permission.
- [x] Write tests and observe 31 expected failures before the module exists.
- [x] Implement commit-only scope, strict schema, 24-hour maximum, two fixed clocks, repo/git-dir identities, branch, HEAD, UID, boot and runtime binding.
- [x] Run isolated analysis-container tests: 31/31 PASS; Python 3.10 grammar parsing PASS.
- [ ] Review and integrate proposed files into a dedicated host development workspace. Current code is attached to this conversation, not installed on the host.

## Task 2: trusted grant store and owner issuance
- [ ] Add external-to-repo 0700/0600 state, safe path/link checks, atomic writes and project locks.
- [ ] Issue only through an authenticated app-only callback or real owner TTY.
- [ ] Add fixed expiry, explicit revocation, audit metadata and verified post-commit HEAD advancement.
- [ ] Test malformed state, forged/replayed confirmation, future time, symlink/hardlink paths, concurrent revoke and process interruption.

## Task 3: Git executor integration
- [ ] In GitWorkflows.execute, preserve all current snapshot checks before considering a commit grant.
- [ ] Only action=commit can use a valid grant. Existing push approval remains unchanged.
- [ ] Hold the established project/authority boundary during admission; do not silently grant on an error.
- [ ] Persist accurate success/error and expected HEAD after a verified commit, without claiming rollback if the commit already exists.
- [ ] Test two sequential toy-repo commits with one grant, expiry, revocation, different scope, source/index drift and failures.

## Task 4: owner UI and rollout
- [ ] Show explicit one-use vs 24-hour local-commit options with project, branch, expiry and excluded operations.
- [ ] Keep confirmation tokens private and confirmation tools app-only.
- [ ] Run focused Python/UI tests and full target-host regression.
- [ ] Obtain approval and execute existing immutable runtime rollout; verify actual connected tools/UI.
- [ ] Open the real tossApp scoped Git grant card only after installation.

## Execution ledger
Target: cantopsbuildserver / personal-ops source main with unrelated WIP. No branch switching, Git commit/push or runtime writes performed.
Source inspection completed; local draft core passes 31 unit tests. Host get_host_access is inactive with RUNTIME_CHANGED; host card 950dc795e271789b6aa5b0a98364d015 is pending. Isolated runner baseline returned Job history capacity reached. No unauthorized fallback, quota clearing or log deletion attempted.
Future host work must first recheck actual approval, source hashes and concurrent WIP. Pure local tests are not host integration or deployed feature verification.
