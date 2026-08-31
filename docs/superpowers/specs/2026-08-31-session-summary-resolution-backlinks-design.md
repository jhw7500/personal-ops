# Session Summary Resolution Back-links Design

Date: 2026-08-31
Status: approved in chat; implementation pending
Issue: https://github.com/jhw7500/personal-ops/issues/17

## 1. Decision

`session-summary` will track unresolved items in a local schema-versioned state file and will add a
resolution back-link only after a deterministic post-validator confirms that the model selected a
commit from the exact item/repository candidate set prepared before the model call.

The implementation will add one standard-library Python helper,
`session-summary/resolution-tracker.py`, with two operations:

- `prepare`: load and recover unresolved state, inspect bounded later git history, and produce the
  evidence context and manifest supplied to the existing single Claude call.
- `reconcile`: validate the generated unresolved/resolved lines against the manifest, carry forward
  omitted open items, downgrade unsupported resolutions, and stage the next state.

The visible resolved format remains:

```text
- [x] bps 배열 길이 불일치 — gstApp [resolved by dc06098]
```

The final Redmine report generator must continue to perform its own current-git cross-check. This
feature reduces stale source data; it does not make the session summary the final resolution
authority.

## 2. Motivation

`### 미완료 항목` currently contains model-generated point-in-time statements without stable
identity, repository provenance, or a later-resolution check. A later summary or weekly report can
therefore repeat an item after the repository has already fixed it.

The concrete incident was a `gstApp` `bps=4096` configuration problem recorded on 2026-05-08/09.
Commit `dc06098` fixed the array-length cause on 2026-05-11, but its generic `chore` subject did not
describe the fix. A subject-only search missed it; the changed code and symbols exposed it.

Prompt-only resolution is not sufficient because a model could omit an item, invent a SHA, attach
evidence from the wrong repository, or close an item after a git lookup failure. The design therefore
uses the model for the semantic judgment but restricts and validates the possible evidence
deterministically.

## 3. Goals

1. Give every open item a stable identity and repository provenance when that provenance is
   unambiguous.
2. Inspect commits after the item baseline using actual patches and code-like tokens, not only
   commit subjects.
3. Let the model close an item only with a prepared candidate commit for that exact item and
   repository.
4. Preserve open items when evidence is absent, ambiguous, unavailable, malformed, or omitted by
   the model.
5. Make repeated runs idempotent: a resolved item is not re-emitted and an open item is not
   duplicated.
6. Keep the existing one-model-call, prompt-size, timeout, lock, and backoff guarantees.
7. Test all resolution behavior with temporary git repositories and a Claude stub, without network
   or model API access.

## 4. Non-goals

- Removing the Redmine report generator's final current-git verification.
- Editing historical session summaries or already-published reports.
- Fetching remotes, searching GitHub, or resolving items against a remote-only checkout.
- Proving semantic correctness from an arbitrary natural-language item without human/model
  judgment.
- Scanning every repository under the workspace.
- Adding a database or third-party dependency.

## 5. Alternatives considered

### 5.1 Prompt-only carry-forward and resolution

This is the smallest change but cannot prevent fabricated SHAs, wrong-repository evidence, dropped
items, or false completion after git failure. Rejected.

### 5.2 Fully deterministic exact-symbol closure

This would be safe for narrowly structured bug statements but would miss fixes expressed through
renames, changed conditions, or several related lines. It also cannot decide that a matching symbol
change actually resolves the stated behavior. Rejected as the only mechanism.

### 5.3 Bounded candidates plus deterministic validation

The helper finds later commits whose patches contain item-specific code evidence, the model makes a
semantic choice among those candidates, and the reconciler accepts only a manifest-authorized
item/SHA pair. This preserves useful semantic matching while closing the fabricated-evidence path.
Selected.

## 6. Components and files

### 6.1 `session-summary/resolution-tracker.py` (new)

One focused helper owns state parsing, repository validation, candidate extraction, prompt-context
rendering, and generated-output reconciliation. It uses only Python's standard library and invokes
`git` through argument arrays with `shell=False`.

Its public CLI is:

```text
resolution-tracker.py prepare \
  --summary <path> --state <path> --manifest <path> --context <path> \
  [--prior-summary <path>] [--repo <absolute-git-root> ...]

resolution-tracker.py carryover \
  --summary <path> --state <path> --output <path>

resolution-tracker.py reconcile \
  --generated <path> --manifest <path> --validated <path> --next-state <path>
```

Both operations return nonzero for malformed inputs or internal failures. The caller treats that as
verification unavailable and never as evidence of completion.

### 6.2 `session-summary/session-summary.sh` (modified)

The shell script will:

1. Before rotation, stage a validated carryover summary containing open items with their original
   `opened_on` and stable IDs; abort rotation if this cannot be produced.
2. Select the latest archive by the canonical filename period and collision suffix, never by mtime.
3. Build an exact local repository list from the already-extracted local session directories.
4. Create private temporary manifest, context, validated-output, and next-state files.
5. Run `prepare` before constructing the prompt, with the latest archive as explicit prior evidence.
6. Include the bounded resolution context and explicit output contract in the existing prompt.
7. Run `reconcile` after a successful model response.
8. Append only validated output to the summary.
9. Move the staged state into place after the summary append succeeds.

No additional Claude call, git fetch, remote lookup, or fallback model is introduced.

### 6.3 `tests/test-resolution-tracker.py` (new)

Python `unittest` fixtures will exercise the helper against temporary repositories. These tests
cover state, git evidence, ambiguity, validation, recovery, and idempotency without invoking Claude.

### 6.4 `tests/test-quota-aware-cron.sh` (modified)

The existing network-isolated shell suite will verify integration with the Claude stub, prompt
flags, single-call behavior, unsupported-resolution downgrade, and state publication ordering.

### 6.5 Documentation (modified)

`session-summary/README.md` will describe the state file, visible formats, verification-required
behavior, limits, and the fact that Redmine still performs the final check.

## 7. State model

The runtime file is `session-summary/state/unresolved-items.json`, under the already-gitignored
`state/` directory. It contains UTF-8 JSON with sorted keys and a trailing newline:

```json
{
  "schema": 1,
  "items": [
    {
      "id": "unresolved-5a83e991f24b",
      "text": "bps 배열 길이 불일치",
      "project": "gstApp",
      "priority": "중",
      "opened_on": "2026-05-09",
      "identity_repo_key": "/absolute/local/path/gstApp",
      "repo_path": "/absolute/local/path/gstApp",
      "baseline_head": "0123456789abcdef0123456789abcdef01234567",
      "status": "open",
      "resolution": null,
      "verification": "ready"
    }
  ]
}
```

Allowed values are deliberately narrow:

- `schema`: exactly `1`.
- `id`: `unresolved-` plus 12 lowercase hex characters.
- `opened_on`: ISO date from the summary section that first introduced the item.
- `identity_repo_key`: an immutable validated git root, or `unmapped:<positive occurrence>` when
  the item could not be mapped at creation time.
- `repo_path`: an absolute local git root or `null`.
- `baseline_head`: the exact 40-hex repository HEAD recorded when the item is first mapped, or
  `null` when unavailable.
- `status`: `open` or `resolved`.
- `resolution`: `null`, or `{ "commit": <40-hex>, "resolved_on": <ISO date> }`.
- `verification`: `ready`, `repo-unmapped`, `repo-ambiguous`, `git-unavailable`, or
  `history-diverged`.

The item ID is the first 12 hex characters of SHA-256 over this NUL-separated identity:

```text
opened_on \0 project \0 normalized_text \0 repository_identity
```

`repository_identity` is the immutable `identity_repo_key`. The occurrence suffix distinguishes
otherwise identical unmapped lines without pretending they belong to different repositories. If a
later run fills `repo_path`, it does not recompute the ID or change `identity_repo_key`. The state
JSON never appears in the model prompt or committed archive. The summary and its archive retain
only the opaque hidden item ID so state can be reconstructed if the state-file move fails:

```text
<!-- unresolved-id:unresolved-5a83e991f24b -->
```

Resolved records remain in state for deduplication but are never supplied as open work in later
prompts. The helper bounds state to 100 open plus 200 most-recent resolved items. Exceeding the open
limit makes `prepare` fail closed, skips the model call for that run, and leaves the prior
summary/state unchanged except for an explicit tracker failure entry in the log.

## 8. Repository mapping

Only local absolute directories already present in the day's Claude Code or opencode contexts are
eligible. A directory is a candidate only when `git -C <path> rev-parse --show-toplevel` succeeds
and returns the exact normalized path. No workspace scan occurs.

A new item uses the `project` field from the canonical output line:

```text
- [ ] <text> — <project> — <priority>
```

The project must equal exactly one candidate repository basename. Zero matches stores
`repo-unmapped`; multiple matches stores `repo-ambiguous`. Neither state permits resolution. A
later run may fill a previously null mapping only when the same project has exactly one validated
local repository candidate. That update preserves the immutable item ID and `identity_repo_key`.
An existing non-null mapping is never silently changed to another path.

A newly generated item records the repository's current HEAD as `baseline_head`. An older canonical
line imported from a summary without state uses the newest commit at or before the end of its
`opened_on` date as a conservative historical baseline. If that date lookup is unavailable or the
result is not an ancestor of current HEAD, the item remains verification-required. This import rule
allows a later generic-subject commit such as `dc06098` to be considered without scanning commits
that predate the recorded open item.

Remote-host-labelled directories are not local git candidates. Their items remain open with
verification required until a local checkout is observed and mapped unambiguously.

## 9. Candidate evidence

For each ready open item, `prepare` first verifies that `baseline_head` exists and is an ancestor of
the current repository HEAD. If not, it records `history-diverged` and produces no candidate.

It then enumerates at most 200 non-merge commits from `baseline_head..HEAD`, oldest first. For each
commit it reads a bounded, no-external-diff patch. Commit subjects are included as context but never
make a commit eligible by themselves.

Evidence tokens are extracted from the original item text:

- code-like identifiers containing `_`, `.`, `::`, brackets, digits, or camel-case transitions;
- standalone identifiers of at least three ASCII alphanumeric characters, such as `bps`;
- numeric literals of at least two digits, such as `4096`.

Korean prose, priority words, common workflow words, and the project name are not evidence tokens.
A commit becomes a candidate only when an exact token boundary match appears in an added or removed
patch line. The manifest records the full commit SHA, short SHA, repository identity, matched tokens,
changed path, and a bounded surrounding diff excerpt.

Limits per run:

- at most 100 open items;
- at most 200 inspected commits per item;
- at most 5 candidates per item;
- at most 4 KiB of rendered evidence per item;
- at most 32 KiB of total resolution context.

If any individual git command fails, times out, exceeds its output bound, or returns malformed data,
that item receives no candidates and is marked verification-required. Partial evidence from the
failed lookup is discarded.

## 10. Model contract

The prompt contains an immutable card for every prior open item:

```text
ITEM unresolved-5a83e991f24b
ORIGINAL - [ ] bps 배열 길이 불일치 — gstApp — 중
STATUS ready
CANDIDATE dc06098 ... matched symbols and diff excerpt ...
```

The model must emit every prior item exactly once:

- unresolved: the original `[ ]` line and hidden unresolved ID;
- resolved: the original text/project plus `[x]` and one candidate short SHA in
  `[resolved by <short-sha>]`, followed by the same hidden unresolved ID.

The prompt explicitly prohibits:

- resolving from a commit subject alone;
- using a SHA not listed for that item;
- moving evidence between repositories or item IDs;
- changing the original text, project, priority, numbers, units, or code symbols;
- dropping an item when verification is unavailable;
- creating a second line for the same item.

New unresolved items use the existing canonical `[ ]` format and have their hidden ID inserted by
the reconciler.

## 11. Deterministic reconciliation

`reconcile` parses only the generated `### 미완료 항목` section and treats all model text as
untrusted.

For each prior open item:

1. Find lines bearing its exact hidden ID.
2. If there is exactly one unresolved line, rewrite it from the state record into canonical form.
3. If there is exactly one resolved line and its SHA is an exact manifest candidate for that item,
   rewrite it into canonical resolved form and mark the state record resolved.
4. Otherwise append or rewrite one canonical unresolved line with `[검증 필요]` and keep the state
   record open.

Candidate validation uses the manifest's full SHA and accepts only an unambiguous short prefix of at
least seven hex characters. The visible output uses the manifest's canonical seven-character short
SHA. A prefix collision is unsupported and leaves the item open.

New `[ ]` lines are normalized, mapped, assigned IDs, and deduplicated by identity. New `[x]` lines
without a known prior item ID are unsupported and are rewritten to `[ ] ... [검증 필요]`; the model
cannot introduce an already-resolved historical claim.

If the model omits the section or any prior item, the reconciler inserts the missing canonical open
lines. If generated Markdown cannot be parsed safely, reconciliation fails and the caller appends a
failure summary instead of the unvalidated model output.

## 12. Publication and recovery ordering

The shell caller stages all generated artifacts with mode `0600` under a private temporary
directory. Publication order is:

1. reconcile model output into a validated daily-summary file and a complete next-state file;
2. append the validated daily summary to `logs/session-summary.md`;
3. atomically replace `state/unresolved-items.json` with the next-state file.

The summary is the recovery authority because it contains hidden item IDs. If step 3 fails after the
summary append, the next `prepare` rebuilds missing state entries from the summary and does not call
the model merely to repair state. Repository provenance absent from the summary is not invented: it
must be remapped by the exact repository rule and receive the conservative dated baseline above
before resolution can resume. A state entry that is not represented in the summary is never allowed
to prove resolution by itself.

The existing summary lock covers prepare, model execution, reconcile, summary append, and state
replace. Rotate continues to wait on the same lock, so the archive and state cannot observe a
half-published daily summary. Rotate stages its carryover before moving the archive and keeps each
open item's original date in the new active summary, so a later state record cannot move the
historical baseline earlier than summary evidence.

## 13. Failure behavior

| Failure | Behavior |
|---|---|
| State absent | Rebuild from summary markers and canonical open lines. |
| State malformed or schema unknown | Recover from summary if possible; otherwise skip the model and leave prior items open. |
| Repository missing/ambiguous | Do not inspect git and do not resolve. |
| Git lookup failure or history divergence | Discard partial candidates and do not resolve. |
| Candidate context exceeds per-item limit | Do not resolve affected items; existing global prompt limit still applies. |
| Open-item count exceeds 100 | Skip the model and preserve the prior summary/state. |
| Model invents/changes item, SHA, number, unit, or symbol | Canonicalize from state and keep open unless exact candidate authorization exists. |
| Model omits prior item | Reinsert it as open. |
| Reconcile cannot parse generated Markdown | Append an explicit `other` failure summary; never append unvalidated output. |
| State replace fails after summary append | Log the failure; reconstruct from summary on the next run. |

All failures are fail-open with respect to the work item: the item stays unresolved. They are
fail-closed with respect to resolution: no unsupported completion is published.

## 14. Security and resource bounds

- No network operations are introduced.
- Repository paths are passed as subprocess arguments, never through shell parsing.
- Git commands disable external diff and color and use explicit output/time limits.
- State and manifests are data, not shell input; no `eval`, sourcing, or command interpolation is
  permitted.
- Generated model output cannot add a repository path or candidate SHA to the manifest.
- Resolution context counts toward the existing 65,536-byte prompt limit.
- The existing one-call, Haiku/low, no-tools, no-persistence, timeout, kill-after, backoff, and flock
  constraints remain unchanged.

## 15. Tests and acceptance mapping

### Helper tests

1. A fixture open item and a later patch changing its exact symbol produces an authorized candidate
   and reconciles to `[resolved by <short-sha>]`.
2. A generic `chore` subject with a matching patch still produces a candidate, covering the
   `dc06098` class of fix.
3. A relevant-sounding subject without an item token in the patch does not produce a candidate.
4. The same item text in two repositories cannot consume the other repository's SHA.
5. Two repositories with the same basename remain ambiguous and unresolved.
6. An unknown repository, invalid baseline, git error, timeout, or oversized patch leaves the item
   open with verification required.
7. A fabricated SHA, wrong item ID, modified original number/unit/symbol, duplicate line, or omitted
   line is rewritten to one canonical open item.
8. Repeated prepare/reconcile runs do not duplicate an open or resolved item.
9. A resolved state record is not emitted as open work in later context.
10. Missing state is reconstructed from summary hidden markers.

### Shell integration tests

1. The prompt contains only bounded prepared candidates and the exact resolution constraints.
2. A supported stub resolution is appended once and advances state.
3. An unsupported stub resolution is downgraded and remains open.
4. Tracker failure never causes a second model call or unvalidated output publication.
5. Existing quota/auth/timeout/input/locking/rotation tests remain green.
6. All tests run inside the existing `bwrap --unshare-net` harness or use local temporary git
   repositories; no real model API is reachable.

These tests directly cover every completion condition in issue #17.

## 16. Rollout

The first deployed run starts with no state file. It parses existing canonical open lines from the
active summary. Items without hidden IDs receive deterministic IDs. Mapped legacy items receive the
dated historical baseline defined in section 8; items whose repository cannot be inferred from that
day's exact local repo set remain open and verification-required. No existing summary text is
rewritten in place.

Operators can disable resolution processing for diagnosis with
`SESSION_SUMMARY_RESOLUTION_TRACKING=0`. Disabled mode retains the existing summary behavior and
does not delete state. Re-enabling resumes from the summary plus state reconciliation.

The feature is ready to roll out when helper and shell suites pass, ShellCheck reports no errors,
the scripts preserve executable permissions, and a dry fixture run demonstrates a generic-subject
symbol patch resolving exactly once.
