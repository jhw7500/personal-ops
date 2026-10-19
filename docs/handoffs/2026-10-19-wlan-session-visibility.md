# wlan-package session visibility investigation (2026-10-19)

## Finding and current boundary

The owner reported that wlan-package continues working while all characters appear idle. The prior synthetic lifecycle test proves renderer movement for supplied queued/running/returned events; it does not prove continuous visibility of a real Claude session between tool calls. The pending UI package alone does not address that collector limitation.

The initial investigation used read-only production activity requests and an isolated UI receiving actual sanitized wlan-package records. A local presentation correction was subsequently implemented and verified below. No installed runtime, service, hook configuration, grant, CLI session, build, Docker or database was changed. In particular no Yocto/BitBake task was run or interrupted. Operational activation remains deferred by the owner while web work continues.

## Observed production metadata

At 2026-10-19 07:30:56 UTC the latest 200 raw rows contained Claude events only. There were 107 distinct tool/prompt/subagent call identities, with 89 matched start/return pairs. Of those pairs, 74 were shorter than the three-second page polling interval and 56 shorter than one second. The median was 0.2908 seconds; the longest was 394.6033 seconds.

The unchanged model normalized that snapshot to active=0, done=105, unknown=3. Every scene sprite was idle in the waiting room. Thus the owner's reported display is reproducible from the actual API records, even though this snapshot cannot establish that the underlying session has finished.

Process metadata for the registered project showed Claude CLI-related processes. Process presence alone is not evidence of an executing tool or an active reasoning phase.

## Actual-data shadow observation

The private observer fetched production /api/activity?project=wlan-package with GET only, preserved original event/receipt times and provenance, and mirrored records into a separate private state/project/server. It injected no synthetic events into either production or the shadow stream.

During 42 seconds of foreground observation with motion enabled and reduced motion disabled:

- 21 mirror polls received two new real rows: one started and one returned call.
- 166 browser samples included walking in 101, working pose in 13, and recorded replay in 102.
- There were no page/mirror errors and no manual refresh.
- The actual final capture visibly labels this animation as recorded replay. A returned call was not promoted to ongoing command execution or workflow success.

The installed baseline 46ca6ee31dca27798355 and pending candidate 3a4ae820e4cb98153801 have byte-identical model.js, motion.js and pixels.js, and identical syncScene/paint bodies. The pending inspector/mission UI patches do not introduce the replay behavior observed here.

Consequently the renderer can show new wlan-package activity under the tested settings. The owner subsequently confirmed movement, but reported two background agents whose displayed identities/states differed. This narrows the remaining problem to observation fidelity; it does not establish their exact IDs or prove the browser settings.

## Collector limitations identified in source

Claude reporters are one-shot hooks, not a continuous session observer. UserPromptSubmit with a prompt ID starts a call; Stop returns that call. Without a prompt ID they emit a generic session running/unknown record. PreToolUse starts a tool call and PostToolUse returns it. SubagentStart/Stop provide separate child identities.

Active events expire after at most 120 seconds in both server sanitization and model normalization. A separate new tool call does not refresh an older prompt or a long-running Bash call. There is currently no Claude heartbeat or periodically reobserved live-session state. The raw latest-200 window also does not pin open tasks or retain lifecycle pairs.

Therefore an ongoing prompt or long tool invocation can lose a current observation, while tool-free processing intervals and already-returned short calls leave characters idle. This is distinct from dropping start events: actual Claude start events are present. The earlier Codex request/return-only explanation must not be applied wholesale to this Claude case.

Automatic short-call replay already exists; short duration alone is not a complete failure explanation. Its guards exclude initial history, paused/hidden/stale/reduced-motion views, previously seen task IDs, some late events and role sprites. Do not remove these guards or manufacture live execution merely to make characters move.

## Background-agent reconciliation

The 07:51:34 UTC snapshot contained 200 raw rows representing 26 actor identities, including many stop-only child lifecycle rows and one retained child start from 07:40:20. Its lifecycle observation expired at 07:42:20 while later tools were separately recorded for the same child. Starts outside the recent window cannot be reconstructed. The model counted one fresh active work item, 111 returned items and two unknown items; those are work-item counts, not a live background-agent count.

The adapter uses an explicit agent_id to attribute child hooks to sub:session:agent. Without it, tool events belong to the supplied session; do not guess ownership from tool names, process presence or private content. Previously completed called objects remain in the waiting room by the owner's earlier design. Thus map population does not equal the two running agents reported by the owner.

A bounded key-only inspection found no files under ~/.claude/tasks at depth two. Three recent wlan-package .omc HUD files had empty backgroundTasks arrays. Those are generated by a separate helper whose fallback completion logic is not authoritative, so they were not adopted as proof of zero active agents.

Official current Claude documentation describes optional background_tasks on Stop/SubagentStop and a periodic subagentStatusLine surface. These were researched as possible future metadata sources, not installed or assumed present. Feature/version detection and explicit metadata-only capture are needed first. The current hook adapter ignores registry fields. SubagentStop itself can be blocked or continued, so its receipt is not universal proof of process termination. References: https://code.claude.com/docs/en/hooks#stop-input, https://code.claude.com/docs/en/hooks#subagentstop, https://code.claude.com/docs/en/statusline#subagent-status-lines.

## Local source correction

model.js now derives a separate observationStatus only from an expired observed Claude prompt or exact matching child lifecycle. Actual observed starts establish invocation order; a terminal-only or reported-only start cannot supersede another unresolved request. Matching returned lifecycle records or a newer observed invocation clear the older badge. Generic session metadata and arbitrary tool names do not create this status.

The character shows an amber termination-unknown badge, accessible text and inspector section with the start time and exact record link. Physical idle posture, task states/counts, recorded replay, animation guards and capacity stay unchanged. Fresh child tools retain their real state and do not revive the parent. Cached idle sprites clear the badge when supporting records disappear. Claude tools now use the same bounded recent-call panel as Codex. Help text explains why visible people are not a running-agent count.

This corrects a misleading presentation of missing observations; it does not implement continuous Claude/background-agent monitoring or certify that the owner's two live agents are fully represented.

## Validation and prepared artifact

- Eight new model regressions cover expired prompt/tool separation, matching returns, newer prompts, pruned late results, reported-only anchors, exact child identity, project/source isolation, generic metadata exclusion, equal timestamps and missing evidence. Initial tests failed; final full Node suite passed 178/178.
- Two new actual HTTP/opaque-frame browser regressions failed before the change and now pass. They cover two child badges, exact recent-tool ownership, stop handling, recorded replay and cached-evidence removal.
- Full browser suite passed 117/117. Independent review found one pruned-start precedence defect; after its correction, all 178 Node tests and the two affected browser cases passed again. The reviewer approved the corrected source.
- Runtime install/key tests passed 9+2. Ruff, syntax and whitespace checks passed. Test-server teardown emitted BrokenPipe diagnostics during the full browser suite; its assertions and exit status passed. No unrelated server change was made.
- Two frozen actual metadata snapshots preserve their canonical tasks, counts and scene state/room/action projection. They add observation badges without inventing live work.
- A separate browser check used actual sanitized 07:51:34 metadata, explicitly paused the isolated view, and verified desktop/390/320px with no page overflow or JavaScript errors. The available evidence yields one child observation badge, not a claimed count of one or two running agents. Desktop and 320px captures were visually inspected.

Private prepared package: c8e613c6f4bdf3a30c37, 89 files. Against production baseline 46ca6ee31dca27798355 only work-dashboard/model.js, visualizer.js and visualizer.css differ; the authority manifest inputs are equal. This artifact is not a trusted installation and has not been activated. It includes the previously prepared native history/mission UI patches, but excludes concurrent backend/UX019/runtime-approval WIP. Reconcile that WIP before any later combined rollout; do not deploy an older backend over another session's completed changes.

## Remaining work

1. After concurrent web work is ready, prepare a combined reviewed artifact and follow the separately authorized activation procedure. The current source work does not authorize a service transition or hook change.
2. Design a metadata-only Claude task-registry observer after confirming installed support, freshness, identity and completion semantics. Never extend unmatched starts indefinitely or infer execution from a live CLI process.
3. Validate the actual two reported agent IDs against fresh explicit registry observations. Missing/pruned registry evidence must remain unknown, including when the registry field is absent.

Private evidence: /home/jhw/.local/state/personal-ops-wlan-display/20261019-_5h861qq.

Key files: initial-diagnosis.json, observed-activity.json, background-observation.json, shadow-result.json, shadow-trace.json, shadow-new-records.json, motion-comparison.json, actual-metadata-regression.json, actual-wlan-browser.json, actual-wlan-{1440,390,320}.png, package-verification.json and named check logs/receipts. Metadata and captures remain private; raw command arguments, conversation, output and reasoning were not collected.
