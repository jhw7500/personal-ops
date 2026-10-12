# Design

## Source of truth

Active, 2026-10-12. Surface: authenticated personal-ops admin and RPG work map.
Evidence: user requested moving people and rooms inspired by classic 2D RPGs, stable MCP/agent objects,
and more immediate, dynamic feedback. Current map screenshot, work-dashboard README, October 9 spec,
and Codex collector contract were inspected. This file guides the current live-screen improvement.

## Brand

A pixel-art work guild: warm room materials, readable labels, distinct people and tools.
Trust comes from real source identities, supplied timestamps, and explicit observed/reported evidence.
Avoid decorative people presented as real workers, inferred success, or invented reasoning activity.

## Product goals

Show actual work changes promptly and make starts, returns, waits and failures easy to spot.
The map must update while slower Git/progress/log reads are still pending. Target a visible-page map
query every approximately one second, and local Codex status sampling every two seconds.
These are polling targets, not hard real-time guarantees. Unchanged metadata must not create activity alerts.

## Personas and jobs

The operator watches host-local projects from a LAN PC or a phone through Tailscale.
They need to see which object works where, which signal is new, and what was actually reported or observed.

## Information architecture

Keep project selection, existing work/progress editors and eight map rooms.
Map order: heading and status; agent/model and source summary; compact recent-change strip;
live-view controls; world and inspector. Click a recent change to inspect its current task.
The strip is bounded and never runs commands.

## Design principles

One person per actual source/actor ID; model names remain metadata.
Separate view freshness from producer signal timestamps. Catch-up history is not a new live event.
Preserve selection and keyboard focus across identical polls. Failed requests retain last known data
and visibly mark observation delay. Pruned history is not proof of completion.

## Visual language

Reuse existing green, timber, stone and gold palette, fonts and pixel canvases.
Active rooms have a lamp/occupancy indication. Working actors get a subtle halo and action particles.
Fresh real transitions briefly emphasize their room and appear in the recent-change strip.
Existing corridor routes, work props, terminal archives and labeled replay remain intact.

## Components

Existing: room, actor, task document, inspector, pipeline, camera and motion controls.
Add: view-freshness badge and bounded recent-change buttons. Model computes state changes;
visualizer owns DOM effects and CSS owns decoration. No additional framework or asset dependency.

## Accessibility

Keep keyboard-accessible buttons, text labels, focus rings and source labels.
Reduced motion, animation pause, observation pause, stale data and hidden tabs stop decorative animation.
Recent changes remain readable without motion. Avoid flashing, automatic camera jumps and noisy announcements.

## Responsive behavior

Keep current mobile camera, drag, zoom, full-map view and inspector stacking.
Recent changes wrap within the available width and do not increase map horizontal overflow.
Controls remain touch-accessible; semantic work state does not depend on hover or animation.

## Interaction states

Live: fresh successful map queries and actual state changes. Idle: await signals without synthetic work.
Paused/hidden: stop automatic polling and animate no new activity; refresh promptly on resume.
Error: last known records plus observation-delay state. Initial load: historic records without live bursts.
Return is request return; job success requires observed valid successful completion.

## Content voice

Use short Korean action/status labels. Translate generic Codex session captions into readable Korean.
Show configuration and estimated usage explicitly. Keep prompts, arguments, results and reasoning text out of events.

## Implementation constraints

Vanilla JS/CSS, existing private ProjectState, immutable runtime deployment, same authentication and allowlist.
One nonoverlapping fast map loop; slow detail reads do not block it; Git remains at least ten seconds apart.
Observer records real changes immediately, unchanged active heartbeat at thirty seconds, unchanged idle once.
Do not refresh cached token timestamps on unread polls. No trust bypass, new packages or command execution API.
Validate with pure transition tests, real browser timing/lifecycle and motion/accessibility checks.

## Open questions

- [ ] Operator: native Codex `/hooks` trust review still pending; affects detailed tool signals, not passive status.
- [ ] Later scope: Claude collector and additional external producers require their own truthful adapters.
- [ ] Maintenance window: actual host reboot verification remains separate from this UI task.
