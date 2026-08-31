# Session Summary Resolution Back-links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist stable identities for session-summary open items, find later fixing commits from bounded patch evidence, and publish `[resolved by <sha>]` only after deterministic item/repository/SHA validation.

**Architecture:** Add one standard-library Python helper with `prepare` and `reconcile` commands. The shell script supplies only repositories observed in that day's local session contexts, adds the helper's bounded evidence cards to its existing single Claude prompt, and publishes only reconciled Markdown before atomically advancing state. All lookup and model failures keep the item open.

**Tech Stack:** Bash 4+, Python 3 standard library, local Git CLI, `unittest`, Bubblewrap, ShellCheck

**Spec:** `docs/superpowers/specs/2026-08-31-session-summary-resolution-backlinks-design.md`

## Global Constraints

- Follow repository `AGENTS.md`; every shell command in this plan is run through `rtk`.
- Use `apply_patch` for hand edits and preserve unrelated user changes.
- Use only Python's standard library. Do not fetch, call GitHub, scan the workspace, or add a second model call.
- Keep Claude at `haiku`/`low`, tools disabled, slash commands disabled, session persistence disabled, the existing hard timeout/backoff/lock behavior, and the 65,536-byte default whole-prompt limit.
- Treat model output, state JSON, repository paths, and Git output as untrusted data. Invoke Git with argument arrays and `shell=False`; never source or `eval` generated data.
- Resolution is fail-closed: an exact manifest item/SHA authorization is mandatory. Verification failures are fail-open: the work item remains unresolved.
- Preserve executable mode on `session-summary/session-summary.sh`; make `session-summary/resolution-tracker.py` executable.
- Do not change Redmine or `gstApp` code. The final Redmine current-Git verification remains authoritative.

## Public Interfaces and Data Contracts

The helper CLI is fixed:

```text
session-summary/resolution-tracker.py prepare \
  --summary PATH --state PATH --manifest PATH --context PATH \
  [--repo ABSOLUTE_GIT_ROOT ...]

session-summary/resolution-tracker.py reconcile \
  --generated PATH --manifest PATH --validated PATH --next-state PATH
```

`prepare` writes manifest schema 1 and a UTF-8 prompt-context file. The manifest is private runtime data and has this shape:

```json
{
  "schema": 1,
  "prepared_on": "2026-08-31",
  "repositories": [
    {"path": "/abs/gstApp", "name": "gstApp", "head": "<40-hex>"}
  ],
  "items": [
    {
      "id": "unresolved-5a83e991f24b",
      "text": "bps 배열 길이 불일치",
      "project": "gstApp",
      "priority": "중",
      "opened_on": "2026-05-09",
      "identity_repo_key": "/abs/gstApp",
      "repo_path": "/abs/gstApp",
      "baseline_head": "<40-hex>",
      "status": "open",
      "resolution": null,
      "verification": "ready",
      "candidates": [
        {
          "commit": "<40-hex>",
          "short": "<7-hex>",
          "subject": "chore: build cleanup",
          "matched_tokens": ["bps"],
          "path": "src/config.c",
          "excerpt": "- old.bps\n+ new.bps"
        }
      ]
    }
  ]
}
```

`next-state` contains `{"schema": 1, "items": [...]}` and each item contains exactly the state fields in the approved spec: `id`, `text`, `project`, `priority`, `opened_on`, `identity_repo_key`, `repo_path`, `baseline_head`, `status`, `resolution`, and `verification`.

Manifest `items` contains every summary-authoritative open and resolved record so `reconcile` can
retain the bounded resolved history. Only records with `status=open` receive candidates or appear in
the prompt context. A run with no open records writes a zero-byte context file, allowing the shell
to distinguish an empty model response with carry-forward work from a genuinely empty day.

The implementation may use internal dataclasses or dictionaries, but these behaviors are stable:

- `normalize_text(text)` performs Unicode NFC normalization, collapses whitespace, and trims it without changing numbers, units, case, or punctuation.
- `parse_summary(markdown)` associates canonical unresolved/resolved lines with the nearest `## YYYY-MM-DD` heading and an immediately following `<!-- unresolved-id:... -->` marker.
- `make_item_id(opened_on, project, normalized_text, identity_repo_key)` hashes the exact NUL-separated identity with SHA-256 and returns `unresolved-` plus 12 lowercase hex characters.
- `run_git(repo, args, timeout, max_output)` uses an argument array, disables color/external diff, rejects nonzero status, timeout, malformed UTF-8 replacement-sensitive output, or output beyond the bound, and returns no partial evidence.
- `prepare` never mutates the state file. `reconcile` never mutates the summary or state path. The shell owns publication ordering.

---

### Task 1: Parse summaries and establish stable state identities

**Files:**

- Create: `session-summary/resolution-tracker.py`
- Create: `tests/test-resolution-tracker.py`

- [ ] **Step 1: Write failing CLI tests for legacy import, hidden markers, and stable IDs**

Create a `unittest.TestCase` that invokes the helper through `subprocess.run`, using a fresh temporary directory per test. Start with these observable cases:

```python
def test_prepare_imports_legacy_open_item_with_deterministic_identity(self):
    self.summary.write_text(
        "# Claude 세션 요약\n\n"
        "## 2026-05-09 (2026-05-08 ~ 2026-05-09)\n\n"
        "### 미완료 항목\n"
        "- [ ] bps 배열 길이 불일치 — gstApp — 중\n",
        encoding="utf-8",
    )
    result = self.prepare()
    self.assertEqual(0, result.returncode, result.stderr)
    manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
    item = manifest["items"][0]
    self.assertEqual("2026-05-09", item["opened_on"])
    self.assertEqual("unmapped:1", item["identity_repo_key"])
    self.assertRegex(item["id"], r"^unresolved-[0-9a-f]{12}$")
```

Add separate tests proving that the same input produces the same ID on rerun, identical unmapped lines receive `unmapped:1` and `unmapped:2`, a valid hidden marker is preserved, and a malformed/foreign marker is rejected instead of becoming authority.

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
rtk python3 tests/test-resolution-tracker.py -v
```

Expected: failure because `session-summary/resolution-tracker.py` does not exist or `prepare` is unimplemented.

- [ ] **Step 3: Implement the minimal parser, schema validation, identity, and atomic artifact writers**

Create the executable helper with constants and a narrow dispatcher:

```python
SCHEMA = 1
OPEN_LIMIT = 100
RESOLVED_LIMIT = 200
ID_RE = re.compile(r"^unresolved-[0-9a-f]{12}$")
OPEN_RE = re.compile(
    r"^- \[ \] (?P<text>.+?) — (?P<project>[^—]+?) — "
    r"(?P<priority>높|중|낮)(?: \[검증 필요\])?$"
)
MARKER_RE = re.compile(
    r"^<!-- unresolved-id:(?P<id>unresolved-[0-9a-f]{12}) -->$"
)
```

Implement `normalize_text`, `make_item_id`, heading/section parsing, occurrence assignment, strict state-schema loading, and JSON/text writers that create parent directories only for requested output artifacts. State recovery rules for this task:

- summary markers and canonical lines define which records exist;
- a valid matching state record may restore provenance for the same ID;
- an unmarked legacy line receives an occurrence-based unmapped identity;
- unknown schema, duplicate ID, invalid field, or state entry absent from the summary cannot authorize a resolution;
- more than 100 recovered open items returns nonzero before writing a usable manifest;
- manifest and context files use mode `0600`, sorted JSON keys, UTF-8, and a trailing newline.

For now, imported items have no mapped repository, `baseline_head=null`, `verification=repo-unmapped`, and no candidates. Render one immutable context card per open item even when verification is unavailable.

- [ ] **Step 4: Add malformed-state and retention-bound tests, then make them pass**

Add behavior tests for absent state, malformed JSON recovered from valid summary markers, unknown schema recovered without trusting provenance, a state-only item being ignored, exactly 100 open items succeeding, and 101 open items failing without overwriting existing manifest/context sentinels.

Run:

```bash
rtk python3 tests/test-resolution-tracker.py -v
rtk python3 -m py_compile session-summary/resolution-tracker.py
```

Expected: all Task 1 tests pass and compilation succeeds.

- [ ] **Step 5: Commit the state foundation**

```bash
rtk git add session-summary/resolution-tracker.py tests/test-resolution-tracker.py
rtk git diff --cached --check
rtk git commit -m "feat(summary): track stable unresolved item identities"
```

---

### Task 2: Map exact repositories and prepare bounded patch candidates

**Files:**

- Modify: `session-summary/resolution-tracker.py`
- Modify: `tests/test-resolution-tracker.py`

- [ ] **Step 1: Add temporary-Git fixture helpers and a failing generic-subject test**

The test helper must create commits with controlled author/committer dates and return full SHAs:

```python
def commit(self, repo: Path, message: str, content: str, when: str) -> str:
    (repo / "src" / "config.c").parent.mkdir(parents=True, exist_ok=True)
    (repo / "src" / "config.c").write_text(content, encoding="utf-8")
    self.git(repo, "add", ".")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    self.git(repo, "commit", "-m", message, env=env)
    return self.git(repo, "rev-parse", "HEAD").stdout.strip()
```

Create a repo named `gstApp`, a 2026-05-09 baseline containing `arg.cam[i].bps = 4096`, and a 2026-05-11 commit with subject `chore: build directory cleanup` whose patch changes the `bps` handling. Prepare from the legacy 2026-05-09 item and assert that the later commit appears as the item's candidate despite the generic subject.

- [ ] **Step 2: Run the focused candidate test and confirm RED**

Run:

```bash
rtk python3 tests/test-resolution-tracker.py ResolutionTrackerGitTests.test_generic_subject_with_matching_patch_is_candidate -v
```

Expected: the item remains `repo-unmapped` or has no candidate.

- [ ] **Step 3: Implement repository validation, legacy baselines, and Git failure isolation**

For every `--repo` value:

1. require an absolute existing directory;
2. run `git -C PATH rev-parse --show-toplevel`;
3. normalize both paths with `Path.resolve()` and require equality;
4. get a 40-hex `HEAD` and repository basename;
5. deduplicate exact roots without collapsing separate same-basename roots.

Map only an exact single basename match. On legacy import, find the newest commit no later than `opened_on 23:59:59` with:

```text
git log --all --no-merges --until=<opened_on>T23:59:59 --format=%H -n 1
```

For a newly mapped state record with a missing baseline, use that dated lookup only when it was recovered from an older summary. A new item later created during `reconcile` uses the repository `HEAD` stored in the manifest. Preserve `identity_repo_key` and item ID when a previously unmapped state record later receives `repo_path`.

Implement a bounded `run_git` using `subprocess.Popen`, a monotonic deadline, and capped stdout/stderr reads. Defaults are 5 seconds and 1 MiB per command. Any timeout, cap breach, decode anomaly, nonzero exit, non-40-hex SHA, nonexistent baseline, or failed `merge-base --is-ancestor` discards all candidates for that item and assigns `git-unavailable` or `history-diverged`.

- [ ] **Step 4: Implement evidence-token extraction and bounded patch scanning**

Extract only evidence-bearing tokens from the item text:

- ASCII identifiers of at least three characters;
- code-shaped tokens containing `.`, `_`, `::`, brackets, digits, or camel-case boundaries;
- numeric literals of at least two digits;
- exclude the project name, priority terms, Korean-only prose, and a small constant set of workflow words.

Enumerate oldest-first, non-merge commits with a hard maximum of 200:

```text
git rev-list --reverse --no-merges --max-count=201 <baseline>..HEAD
```

Treat 201 results as a bounded-history failure rather than silently ignoring later commits. Read each patch with color/external diff disabled and no text conversion. A subject alone is never evidence. A candidate requires an exact token-boundary hit on a line beginning `+` or `-` but not `+++`/`---`. Record only the first relevant changed path and a bounded surrounding excerpt. Stop after 5 candidates. Render at most 4 KiB per item and 32 KiB total context; overflow makes the affected item verification-required instead of truncating away authorization facts.

- [ ] **Step 5: Add negative repository/evidence tests**

Add independent tests proving:

- a relevant-sounding subject with no token in the patch is not a candidate;
- an unrelated later commit does not close the `bps` item;
- the same text in two differently named repositories cannot use the other repository's SHA;
- two exact candidate roots with basename `gstApp` yield `repo-ambiguous`;
- a relative path, nested subdirectory, missing repo, invalid baseline, diverged history, Git command failure, 201 commits, oversized patch, and a candidate-context overflow all produce no candidates;
- candidate entries contain the full SHA, canonical seven-character short SHA, matched token, path, and bounded excerpt;
- no command scans outside the explicit `--repo` list.

- [ ] **Step 6: Run helper regression tests and commit candidate preparation**

```bash
rtk python3 tests/test-resolution-tracker.py -v
rtk python3 -m py_compile session-summary/resolution-tracker.py
rtk git add session-summary/resolution-tracker.py tests/test-resolution-tracker.py
rtk git diff --cached --check
rtk git commit -m "feat(summary): prepare bounded resolution evidence"
```

---

### Task 3: Reconcile untrusted model output and stage next state

**Files:**

- Modify: `session-summary/resolution-tracker.py`
- Modify: `tests/test-resolution-tracker.py`

- [ ] **Step 1: Write a failing supported-resolution CLI test**

Use Task 2's real temporary Git candidate. Write generated Markdown containing the exact prior item ID and candidate prefix:

```text
---

## 2026-05-12 (2026-05-11 ~ 2026-05-12)

### 미완료 항목
- [x] bps 배열 길이 불일치 — gstApp [resolved by dc06098]
<!-- unresolved-id:unresolved-5a83e991f24b -->
```

Use the fixture's actual short SHA rather than hard-coding `dc06098`. Assert the validated file contains exactly one canonical `[x]` line and marker, and the staged state contains the full SHA plus `resolved_on=2026-05-12`.

- [ ] **Step 2: Run the supported-resolution test and confirm RED**

```bash
rtk python3 tests/test-resolution-tracker.py ResolutionTrackerReconcileTests.test_authorized_candidate_resolves_once -v
```

Expected: `reconcile` is missing or does not stage resolved state.

- [ ] **Step 3: Implement strict section parsing and canonical reconciliation**

Parse only the generated `### 미완료 항목` section. Preserve all other generated Markdown byte-for-byte except for inserting/replacing that section. Require a safe `## YYYY-MM-DD (...)` daily heading before assigning `resolved_on`.

For every prior manifest item:

- one exact hidden ID plus `[ ]` becomes the state-derived canonical open line;
- one exact hidden ID plus `[x]` resolves only when its hexadecimal prefix has at least 7 characters and maps to exactly one candidate for that item;
- the emitted SHA is always the candidate's canonical first 7 characters;
- no line, duplicate ID, wrong ID, malformed line, modified text/project/priority/number/unit/symbol, ambiguous SHA prefix, or unauthorized SHA produces a resolution;
- unsupported cases emit exactly one state-derived `[ ] ... [검증 필요]` line and keep status open.

New canonical `[ ] text — project — priority` lines are normalized, deduplicated, mapped against manifest repositories, assigned a stable ID, and given that repository's prepared `HEAD` as baseline. A new `[x]` is downgraded only if it includes a losslessly parseable text/project/priority triple; otherwise reconciliation returns nonzero rather than inventing a priority or dropping content.

When the generated section is absent, insert one before `### 기술 메모` or at the end. When a prior item is omitted, carry it forward. Resolved state records stay in next-state for deduplication but never appear in later open context. Retain the 200 newest resolved records ordered by `resolved_on` and ID; retain all open records up to the 100-item limit.

- [ ] **Step 4: Add adversarial reconciliation and recovery tests**

Cover each case with literal output and JSON assertions:

- fabricated SHA, wrong repository candidate, wrong hidden ID, 6-character prefix, and colliding prefix;
- changed `4096` to another number, `건` to `개`, priority mutation, and code-symbol mutation;
- duplicate lines, missing prior item, and missing incomplete section;
- unknown `[x]` downgraded without inventing fields, and unparseable unknown `[x]` rejected;
- valid new `[ ]` mapped with the prepared HEAD baseline;
- same open item on two runs is not duplicated;
- resolved item is retained in state but absent from the next prepared prompt;
- state loss after a published marker recovers the same ID; absent provenance is remapped and receives the dated baseline;
- malformed generated Markdown does not create validated or next-state artifacts.

- [ ] **Step 5: Run reconciliation and full helper tests**

```bash
rtk python3 tests/test-resolution-tracker.py ResolutionTrackerReconcileTests -v
rtk python3 tests/test-resolution-tracker.py -v
rtk python3 -m py_compile session-summary/resolution-tracker.py
```

Expected: all helper tests pass with no network or Claude executable involved.

- [ ] **Step 6: Commit deterministic reconciliation**

```bash
rtk git add session-summary/resolution-tracker.py tests/test-resolution-tracker.py
rtk git diff --cached --check
rtk git commit -m "feat(summary): validate resolution back-links"
```

---

### Task 4: Integrate prepare/reconcile into the single-call shell workflow

**Files:**

- Modify: `session-summary/session-summary.sh`
- Modify: `tests/test-quota-aware-cron.sh`

- [ ] **Step 1: Add failing network-isolated shell scenarios**

Extend the Claude stub to print a case-specific file when `CLAUDE_STUB_OUTPUT_FILE` is set. Let `run_session_summary` opt into test CC fixtures and tracker settings through test variables while preserving today's existing defaults:

```bash
SESSION_SUMMARY_RESOLUTION_TRACKING="${TEST_RESOLUTION_TRACKING:-1}" \
CC_PROJECTS_DIR="${TEST_CC_PROJECTS_DIR:-/nonexistent}" \
INCLUDE_CC="${TEST_INCLUDE_CC:-0}" \
CLAUDE_STUB_OUTPUT_FILE="${runtime_case}/stub-output" \
SESSION_SUMMARY_RESOLUTION_TRACKER="${TEST_TRACKER_BIN:-${REPO_ROOT}/session-summary/resolution-tracker.py}"
```

Add fixtures that create a local repo under the Bubblewrap-mounted case directory, seed a prior summary/state item and later symbol patch, and create a CC JSONL row whose local `cwd` is that exact runtime repo root. Add failing tests for:

1. a supported stub `[x]` being appended once and advancing state;
2. an unsupported SHA being downgraded and staying open;
3. tracker `prepare` failure causing zero model calls and no unvalidated append;
4. tracker `reconcile` failure causing exactly one model call and only an explicit `other` failure summary;
5. the prompt containing the immutable item card, exact candidate, no-number/unit/symbol-mutation rules, and no second call;
6. `SESSION_SUMMARY_RESOLUTION_TRACKING=0` retaining the pre-feature direct-append behavior.

- [ ] **Step 2: Run the shell suite and confirm RED**

```bash
rtk bash tests/test-quota-aware-cron.sh
```

Expected: new resolution integration scenarios fail while the original 16 cases remain green.

- [ ] **Step 3: Make source paths and observed local repositories reusable**

In `session-summary.sh`:

- derive `SCRIPT_DIR` from `BASH_SOURCE[0]`;
- default `RESOLUTION_TRACKER` to `${SCRIPT_DIR}/resolution-tracker.py` while allowing `SESSION_SUMMARY_RESOLUTION_TRACKER` override;
- make `CC_PROJECTS_DIR` and `OPENCODE_DB` environment-overridable for isolated fixtures;
- validate `SESSION_SUMMARY_RESOLUTION_TRACKING` as exactly `0` or `1`;
- compute `SESSION_DIRS` once from column 2 of the already-extracted local CC/opencode contexts, independent of `INCLUDE_COMMITS`;
- for every absolute session directory, resolve `git rev-parse --show-toplevel` and pass only exact roots, deduplicated, as `--repo` arguments;
- continue excluding `[host] /path` remote labels and never scan a parent/workspace directory.

Refactor the existing commit-context loop to consume the same validated roots without changing its date range or output format.

- [ ] **Step 4: Stage tracker artifacts before constructing the prompt**

Create a private same-filesystem runtime directory under `STATE_DIR` with mode `0700`, and trap cleanup of only that exact directory. Stage:

```text
manifest.json
resolution-context.txt
generated.md
validated.md
next-state.json
claude.err
```

When tracking is enabled, call `prepare` with `SUMMARY_FILE`, `state/unresolved-items.json`, and the exact repository array. If it fails, log `resolution tracker prepare failed`, make no Claude call, do not append a summary, and leave state untouched. This is the open-limit/malformed-authority stop condition.

Add the prepared context as section D and add an explicit contract stating that every prior item is emitted once, only its listed candidate may resolve it, subjects alone are insufficient, and original text/project/priority/numbers/units/code symbols must not change. Include this section in the existing whole-prompt byte check.

- [ ] **Step 5: Reconcile before publication and preserve publication order**

After the single successful Claude call:

- if tracking is disabled, preserve the current empty-output/direct-append path exactly;
- if tracking is enabled and model output is empty while prior open items exist, create a minimal daily skeleton so reconciliation can carry them forward;
- invoke `reconcile` into `validated.md` and `next-state.json`;
- on reconcile failure, call `append_failure_summary other`, log the tracker failure, do not append generated output, and do not replace state;
- on success, `ensure_header`, append only `validated.md`, then `mv` the staged next-state file to `state/unresolved-items.json`;
- log and exit nonzero if the state move fails after append so the next run recovers from the published hidden IDs;
- preserve file mode `0600` for tracker state and private artifacts.

The existing `flock` must continue to cover prepare, Claude, reconcile, append, and state move. Rotate keeps waiting on the same lock.

- [ ] **Step 6: Run shell regression, syntax, and static checks**

```bash
rtk bash tests/test-quota-aware-cron.sh
rtk bash -n session-summary/session-summary.sh tests/test-quota-aware-cron.sh
rtk shellcheck session-summary/session-summary.sh tests/test-quota-aware-cron.sh
rtk python3 tests/test-resolution-tracker.py -v
```

Expected: original and new shell scenarios pass; syntax and ShellCheck report no errors; helper tests remain green.

- [ ] **Step 7: Commit shell integration**

```bash
rtk git add session-summary/session-summary.sh tests/test-quota-aware-cron.sh
rtk git diff --cached --check
rtk git commit -m "feat(summary): publish verified resolution links"
```

---

### Task 5: Document operations, exercise the incident class, and complete verification

**Files:**

- Modify: `session-summary/README.md`
- Modify: `tests/test-resolution-tracker.py`

- [ ] **Step 1: Add operator documentation**

Document:

- visible unresolved, verification-required, and resolved formats;
- hidden stable IDs and `state/unresolved-items.json` schema purpose;
- exact-repository mapping from observed local sessions only;
- patch/symbol evidence requirement and the generic-subject `dc06098` incident class;
- limits: 100 open, 200 commits/item, 5 candidates/item, 4 KiB/item, 32 KiB total;
- failure behavior: missing/ambiguous repo and Git failure remain unresolved;
- diagnosis toggle `SESSION_SUMMARY_RESOLUTION_TRACKING=0`;
- one Claude call and existing quota/auth/backoff/timeout bounds;
- Redmine's current-Git check remains the final reporting authority;
- new file layout entry for `resolution-tracker.py` and `state/unresolved-items.json`.

- [ ] **Step 2: Run an explicit local incident-class acceptance fixture**

The helper test suite must expose a named test that starts with a 2026-05-09 `bps=4096` open item, applies a 2026-05-11 generic `chore` commit whose patch changes the `bps` array-length path, prepares the candidate, reconciles it once, and proves a second prepare does not return the item as open.

Run:

```bash
rtk python3 tests/test-resolution-tracker.py ResolutionTrackerAcceptanceTests.test_generic_chore_symbol_fix_resolves_exactly_once -v
```

Expected: one canonical `[resolved by <7-hex>]` line, full SHA in state, no open card on the next prepare.

- [ ] **Step 3: Run the complete verification matrix**

```bash
rtk python3 tests/test-resolution-tracker.py -v
rtk bash tests/test-quota-aware-cron.sh
rtk bash claude-token-sync/tests/test-token-sync.sh
rtk python3 -m py_compile session-summary/resolution-tracker.py
rtk bash -n session-summary/session-summary.sh tests/test-quota-aware-cron.sh
rtk shellcheck session-summary/session-summary.sh tests/test-quota-aware-cron.sh
rtk git diff --check
rtk git status --short
```

Expected evidence:

- helper suite covers every issue #17 acceptance condition without network/model API;
- shell suite preserves all quota-aware behavior and adds resolution integration coverage;
- token-sync suite remains 10/10 green;
- no syntax, ShellCheck, whitespace, or unexpected worktree changes;
- `resolution-tracker.py` and `session-summary.sh` retain executable mode.

- [ ] **Step 4: Commit documentation and any acceptance-only test naming**

```bash
rtk git add session-summary/README.md tests/test-resolution-tracker.py tests/test-quota-aware-cron.sh
rtk git diff --cached --check
rtk git commit -m "docs(summary): explain resolution verification"
```

- [ ] **Step 5: Review final branch scope before shipping**

```bash
rtk git log --oneline origin/main..HEAD
rtk git diff --stat origin/main...HEAD
rtk git diff --name-only origin/main...HEAD
rtk git status --short --branch
```

Stop if any file outside the design, plan, `session-summary/`, and the two named test files changed, or if any verification command is not green. Do not modify `gstApp` or Redmine as part of this branch.
