# Native terminal character history follow-up (2026-10-19)

## Result and activation boundary

The native Codex character inspector now keeps a compact list of current calls and recent results when a concrete call is selected. Implementation, independent review, regression tests and an isolated browser preview using real production metadata are complete. This follow-up has not been activated on the host.

The prior, separately approved activation is documented in [native terminal activity](2026-10-19-native-terminal-activity.md). Its verified runtime was `46ca6ee31dca27798355`. The new private package is `0de70682ccad91cfb400`; its 89-file manifest differs from that baseline only in `work-dashboard/visualizer.js` and `work-dashboard/visualizer.css`.

The authority manifest and authority identity compare equal between these packages. A still-valid host access grant therefore does not require renewal solely for these two UI asset changes. This does not renew an expired grant or override separate bindings on one-use operation plans. Recheck the live baseline before any later activation.

No service restart, installed runtime replacement, host configuration change, permission renewal, MCP schema change, model call, Docker operation, database change or upload was performed for this follow-up. Other sessions' uncommitted Toss Console/UX019 work must remain separate. Any operational service transition still requires the owner's separate approval.

## Observed problem and change

Previously the terminal panel showed just the latest concrete call, and only while a session heartbeat was selected. Clicking a real call removed the panel. A real browser regression probe reproduced the missing panel before the change and passed after it.

The inspector now uses the existing normalized calls for the exact same source/project/actor identity. Session heartbeats are excluded. It shows up to three current calls first, then fills the remaining slots with recent terminal or unknown results, with six entries total. Repeated lifecycle records are represented by the existing model's latest state for each call. Pending calls that expire under the existing two-minute freshness rule remain unknown; neither a returned tool request nor an unknown result becomes command success.

Each entry provides the tool, supplied task title, human-readable state, observation provenance, event time and received exit code when present. Its existing task link opens that exact call. The list persists after selection and marks the selected entry. Paused, stale or resume-pending observations label the current section as last-observed calls. Other actors and projects are excluded. Existing character allocation, motion, rooms and technical detail navigation are unchanged.

Raw command arguments, outputs and reasoning are not displayed or used to infer a work purpose. The collector and data model are unchanged. The README also corrects an outdated description that predated the already deployed native metadata collector.

## Verification evidence

Private evidence root: `/home/jhw/.local/state/personal-ops-native-detail/20261019-6lp7zx22`.

| Check | Observed result | Evidence |
| --- | --- | --- |
| Existing workroom and bridge JavaScript tests | 170 passed | `node_full.json`, `node_full.log` |
| Complete HTTP/Chromium admin UI suite | 113 passed | `browser_final.json`, `browser_final.log` |
| Native terminal browser cases | 5 passed, including selection persistence, actor isolation, ordering/cap, expired queue and narrow viewports | `targeted_browser_green.json`, `targeted_browser_green.log` |
| Runtime installer and display-only authority regression checks | 11 passed | `authority_tests.json`, `authority_tests.log` |
| JavaScript syntax, Ruff on changed browser tests, diff whitespace | Passed | Targeted checks during implementation |
| Independent code review | Approved; zero findings across the three code/test files | Native `terminal_activity_review` review |
| Package comparison | Only the two UI files differ; authority identity equal; not installed | `package.json` |

The final full browser run supersedes an earlier run that compiled a selector matching both a list row and its nested button. The selector was narrowed to the row, and both targeted and full suites passed. Original RED/failure logs are retained. Existing browser-server BrokenPipe traces on page teardown did not fail tests and were not changed in this UI patch.

## Actual-data browser preview

The preview fetched sanitized native metadata from the production activity API and copied it into a private, isolated state directory. It did not inject synthetic work into production or mutate production event state. It ran only a loopback candidate UI and shut that preview down afterward.

`actual-preview-result.json` records 188 metadata rows, one character for the selected owner, six terminal entries (one current and five recent), and persistence after selecting a concrete call. Chromium reported no page errors or horizontal overflow at widths 1440, 390 and 320. Captures are `native-detail-1440.png`, `native-detail-390.png` and `native-detail-320.png` under the evidence root. These captures prove the candidate UI against observed metadata; they do not prove this UI is already active in production.

## Continue safely

1. Preserve the integration boundary: only this follow-up's UI, browser regression tests and documentation belong in its commit. Existing destination files are compared to the recorded baseline before copying; do not overwrite other sessions' changes.
2. Retain the prepared private package and evidence. Before activation, compare the current production version and any concurrent rollout, and review effects on active web jobs.
3. Obtain the separate service-transition approval, then use the canonical host rollout procedure. Do not edit installed snapshot files in place.
4. Verify the actual running runtime, UI assets and real native activity after activation. Keep client discovery or Claude hook-event checks from the previous handoff separate; this UI-only patch does not establish them.
