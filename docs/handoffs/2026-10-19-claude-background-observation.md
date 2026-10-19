# Claude background-agent observation: activated 2026-10-19

## Result and operating state

The owner approved deployment while web work was stopped. MCP and the management web now run snapshot `880c076789c68bbf71e9`. The web can resume work. No further service restart is needed for this collector activation.

| Component | Verified deployment |
| --- | --- |
| MCP | `880c076789c68bbf71e9`, PID `130920` |
| Management web | `880c076789c68bbf71e9`, PID `131385` |
| Existing Codex observer | PID `3208094`, unchanged pinned runtime `46ca6ee31dca27798355` |
| Separate Claude collector | `~/.local/lib/personal-ops-claude-status/ba2951e2d9cf24b7bd1c/claude_status_report.py` |
| Collector full SHA-256 | `ba2951e2d9cf24b7bd1c39ef54c3aafff563f228843ccf710949e3e30113c484` |
| Collector trusted imports | Verified immutable runtime `46ca6ee31dca27798355` |

The canonical rollout changed only `work-dashboard/model.js`, `visualizer.js`, and `visualizer.css` from the live baseline. Execution-authority inputs were equal; existing host grants were preserved. Other-session runtime-rollout and Toss-console source changes were excluded. No Docker, database, schema, or BitBake operations ran.

The auxiliary collector is outside the MCP runtime manifest. Its configuration was activated at `2026-10-19T08:48:28Z`, without restarting MCP, admin, the observer, or independent CLI sessions. Preserve the immutable helper and pinned imports when maintaining host configuration; a source checkout alone does not install the callback.

## Cause and correction

One-shot Claude hooks and a recent raw-record window did not describe the whole background-agent lifetime. Expired lifecycle starts could leave sprites idle while the owner still had background work. The optional statusline callback alone also missed the wlan-package registry, although it produced other native callbacks.

Actual `SubagentStop` metadata included two running subagents and one shell task. Their exact child identities already had observed native hook records. The collector now accepts both official `subagentStatusLine` rows and optional `background_tasks` from `Stop`/`SubagentStop`, using only subagent ID, type, and state. Shell commands, descriptions, prompts, output, transcripts, start times, and reasoning are not collected.

Both input modes require an existing observed native start or tool record with the exact project/session/source/child identity before first association. A known parent alone cannot create a child. Registry activity refreshes the existing actor/task rather than creating a second character. Identical running observations emit at most once per 30 seconds; explicit transitions emit immediately. Return is not proof of assignment success. Missing/empty lists and callback silence never imply completion; observations still expire after 120 seconds.

The renderer distinguishes child activity from a concrete tool call, prefers a concrete active call in the actor, scene, and mission, and avoids counting an activity summary as another concurrent call. The status line separately shows distinct children with current observed activity. Completed waiting characters are not a count of currently running agents.

## Actual event verification

No captured payload was replayed into production and no synthetic work events were injected.

| wlan-package child in session `ab06a60d-7069-4651-af06-d8a17c8e7479` | Actual evidence |
| --- | --- |
| `a122201b76bd4413e` | Native association proved; cached `returned` at `08:41:54Z`, before final auxiliary activation. Not counted as a newly observed completion. |
| `ab8b169ae1e27169e` | Natural `running / observed` refreshes after activation at `08:48:48Z` and `08:49:18Z`, same `sub:<session>:<id>` actor and `agent:<session>:<id>` task; 120-second expiry advanced. |

Thus two running agents were observed during diagnosis, and one running child plus one returned child were verified later. Do not keep displaying two active children after the real state changes.

The original `statusLine` and all existing hook entries were preserved. Only the new `subagentStatusLine` setting and one separate registry callback each in `Stop` and `SubagentStop` remain. Temporary diagnostic probes were removed. Ten other protected settings/grant files and 33 other-session WIP files were independently verified byte-identical after activation; grant files remained mode `0600`. The canonical rollout also verified all seven existing CLI processes preserved.

## Validation

- Final collector, Claude hooks, and Codex observer: **69/69 passed**; final collector alone **18/18 passed**.
- Known parent plus unproven child: RED reproduced the unwanted association, then GREEN after enforcing exact-child proof in both modes.
- Ruff and Python compilation passed. Collector review: APPROVE, no remaining findings.
- Dashboard Node suite: **181/181 passed**. Admin browser suite: **118/118 passed**; three affected Claude browser cases reran after the final optional-tool-metadata correction and passed.
- Canonical rollout required checks: targeted **829** and full **1041** tests passed, plus the four required approval/UI suites. These preceded the final auxiliary fallback; the fresh 69-test collector run covers that later change.
- Production browser verification covers actual LAN UI, wlan-package/personal-ops/pim-check, desktop/mobile widths, no page errors or overflow, and matching loopback/LAN renderer assets with `no-store`.
- A real connector `read_project_progress(project='personal-ops')` round trip succeeded after the switch. The generic operator receipt remains `verification_required / awaiting_discovery` because it did not passively observe full connector catalog/schema discovery. That is a remaining discovery check, not a failed runtime switch. No new MCP tool schema was introduced here.

Private operator receipts, preservation hashes, tests, and actual UI screenshots are under `~/.local/state/personal-ops-claude-registry/20261019-6v4xr7tb/`. They contain no raw conversations in the monitoring evidence. Keep evidence private.

## Limits and follow-up

Statusline rows cover visible native UI rows; Stop/SubagentStop supplement them at event time. Neither guarantees uninterrupted observation of every hidden background task. On silence, retain honest expiry/unknown behavior. Do not introduce transcript scraping, process guesses, or fabricated progress to keep sprites moving.

Dashboard browser reload loads the new renderer. Plugin metadata refresh is not required for these unchanged tool schemas. Full passive connector catalog discovery is still separate from the successful tool round trip.

The older lifecycle inspector's `시작 기록` wording can refer to the latest lifecycle observation after a refresh. A later UI-only wording pass should label that time as an observation time rather than claim the real invocation start. This does not change lifecycle identity or execution permissions.
