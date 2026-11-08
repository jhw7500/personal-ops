# Scoped bootstrap preparation and post-install evidence

Scope: source preparation and fixture verification on `feat/scoped-bootstrap-verification-20261108`, based on clean commit `08a42b9`. This work does not install a release, register a profile, alter owner state, or apply the separately denied four-file candidate.

## Acceptance criteria

- A preparation-only CLI verifies the approved ZIP and a clean source tree, then writes an exclusive private review directory containing a textual bootstrap patch and pinned manifest. It never executes ZIP code or applies the patch.
- The bootstrap patch adds the adapter integration and its regression test. It excludes the four candidate changes and defers `inspect_host_command_blockers`, which is absent from the clean baseline.
- Wrong package hashes, unsafe archive entries, changed anchors, dirty/drifting source trees, and existing output paths fail before publishing a prepared result.
- Actual external `tools/list` builds can record bounded canonical schema evidence tied to the installed release, boot, process identity, and session generation. Internal cache fills and failed builds cannot create proof.
- A separate verification CLI writes a new private receipt bound to the original rollout record and fresh observations. It preserves historical plan/receipt bytes and never converts server-side observations into verified ChatGPT metadata or baseline eligibility.
- Existing catalog refresh and runtime approval behavior remains covered by tests. Installation, owner registration, connector adoption, and board checks remain explicitly unexecuted.

## Implementation

1. Add failing fixture tests for ZIP validation, bootstrap patch generation and exclusive review publication; implement `scoped_bootstrap.py` and `prepare-scoped-bootstrap.py`.
2. Add failing session/evidence tests; implement `runtime_verification.py`, the verification CLI, and a narrow catalog/server observation hook.
3. Include new runtime modules in the immutable installer inventory and focused test list. Run targeted tests, the official Python suite, static checks, and an independent review.
4. Record actual changes, verification results, remaining connector/owner boundaries, source branch and uncommitted state in shared handoff storage.

## Evidence and constraints

- `chatgpt-mcp/catalog_refresh.py:151` observes a successful server catalog build, not client receipt or cache adoption.
- `chatgpt-mcp/rollout_catalog.py:84` deliberately keeps connector schema verification false for name-only evidence.
- `chatgpt-mcp/rollout_diagnostics.py:136` already supports separate `mode="verify"` receipts.
- `chatgpt-mcp/workspace_control.py:935` provides pure installed-manifest verification without launch admission.
- The supplied ZIP's inventory amendment assumes the later four-file candidate. Bootstrap preparation must defer that amendment.
- Initial focused baseline: 52 tests passed using the official private test-process umask. A first run without that environment failed the existing private-directory guards; no production change was used to bypass them.
