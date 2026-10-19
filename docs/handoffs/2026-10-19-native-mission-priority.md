# Native mission and character consistency (2026-10-19)

## Source result; activation deferred

The owner explicitly deferred deployment while working through web chat. This follow-up stays local. No service restart, host configuration change, grant renewal, runtime switch, Docker/database operation, upload, host agent job, live Codex/Claude CLI probe or production event injection was performed.

The previous [terminal character history change](2026-10-19-native-terminal-history.md) is included. The combined private candidate is now `3a4ae820e4cb98153801`, superseding the earlier prepared `0de70682ccad91cfb400`. Its 89-file manifest differs from verified baseline `46ca6ee31dca27798355` only in the two workroom UI assets. All three manifests have equal execution-authority identity inputs. The private copy remains untrusted for host execution; comparing identity inputs does not install it or grant authority.

## Reproduced issue and minimal correction

The upper mission and work board shared a comparator that treated a running Codex session heartbeat as active work. A newer heartbeat hid a queued call, or an older running call, although clicking that character correctly selected its actual call. The same mismatch occurred when the heartbeat belonged to a different owner.

The corrected display order is:

| Priority | Recorded item |
| --- | --- |
| 0 | Active concrete work |
| 1 | Prepared concrete work |
| 2 | Queued concrete work |
| 3 | Fresh active/queued session liveness |
| 4 | Unknown work |
| 5 | Terminal records |

The mission heading distinguishes pending work, current work and session status. Clicking it still selects the exact recorded task. Once the actual call returns, session liveness can lead again while the returned call remains in recent terminal activity. A queued request stays queued; a returned request does not become command success. The model, actor allocation, execution state, event storage, frame boundary and collector remain unchanged.

An earlier sparse-title hypothesis was rejected rather than implemented. `ProjectState.record_work_events` rejects missing task_label through the real server sanitizer. The private `rejected-title-hypothesis.json` records that result. No new title inheritance or mixed-provenance behavior was added for invalid input.

## Verification

Private evidence: `/home/jhw/.local/state/personal-ops-task-context/20261019-lcm3wi67`.

- Three HTTP/Chromium regressions reproduced the wrong mission with valid server records, failed before the source correction, then passed. They cover queued versus heartbeat, running-to-returned lifecycle, exact selection and a different owner's heartbeat. See `mission_red` and `mission_green` receipts/logs.
- The complete HTTP/Chromium admin UI suite passed all 115 tests (`browser_full.json`, `browser_full.log`). Existing server BrokenPipe traces during fixture/page teardown remain in the log; no test or browser-page error was reported, and server error handling was not changed in this UI correction.
- Workroom/bridge JavaScript tests: 170 passed (`node_full.json`).
- Display-only authority and runtime installer regression checks: 11 passed (`authority_tests.json`).
- Independent review: APPROVE, no findings; reviewer separately passed the three browser regressions, syntax, Ruff and whitespace checks.
- Private fixture-based HTTP preview at widths 1440, 390 and 320: no page errors, horizontal overflow or post-login HTTP write requests. See `mission-preview-result.json` and `mission-*.png`. These captures use disposable validated fixtures, not production work or a real executed command.
- JavaScript syntax, Ruff and diff whitespace checks passed.
- `package.json` records the two-asset comparison with baseline 46 and unchanged authority identity inputs. Private packages cannot pass the canonical installed-location trust check; that expected boundary was retained.

## Integration and later activation

Only visualizer.js, the browser regression file and this follow-up's documentation belong in the commit. Compare existing root destination files to `baseline.json` before integrating, preserving the other session's backend/UX019 WIP and staging.

Keep activation deferred. When the owner is ready, recheck the actual live runtime and any intervening web rollout before preparing the final candidate and obtaining the separate operational transition approval. Do not install an older package over a newer concurrent release. Use the canonical rollout, then verify actual running assets and mission/character consistency. Neither source tests nor this private preview establish that the new UI is live.
