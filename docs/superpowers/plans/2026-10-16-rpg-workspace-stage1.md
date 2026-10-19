# RPG workspace Stage 1 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Show current project work at a glance, with meaningful character jobs, recorded action effects and factual dialogue that leads to existing logs.
**Architecture:** Reuse the opaque workroom iframe, normalized model and authenticated parent log selection. No new backend/API/permission capabilities.
**Tech Stack:** Existing vanilla JavaScript/CSS, pixel canvas, Python Playwright and Node tests.
**Spec:** docs/superpowers/specs/2026-10-16-rpg-workspace-design.md (Stage 1 approved by user: 진행).

## Global Constraints
- Start from frozen Stage 8 source in an isolated worktree. Preserve original and other sessions' WIP with file hashes.
- No commits, service changes, DB/Docker/schema actions or dependency installation. Integration is selective with compare-before-write.
- Preserve stable source/actor identity, six-seat invocation lifecycle, pause/reduced motion/resume, safe log routing and approval deep links.
- Display classes are factual defaults from object kind/source. They do not grant permissions or create agents; actions do not reclassify an actor.
- No invented skill names, internal reasoning, outcomes or explanations. Actual skill collection and real chat/equipment changes remain later stages.

## Review Focus
1. Returned calls, unknown/stale/paused work must not be described as successful or currently running (Task 1/2).
2. Project switch or new selection must not keep a previous actor's answer/log target (Task 2).
3. Map and inspector must be reachable at 1440x900, 390x844 and 320px, with keyboard and reduced motion (Task 2/3).
4. Folding old controls must preserve approval links, host/legacy logs and opaque iframe boundaries (Task 3).
5. Display jobs/effects must keep actual identity and six-person/oldest-call eviction semantics (Task 1/2).

## Task 1: Factual character presentation
Files: work-dashboard/model.js, work-dashboard/tests/character.test.js.
Interfaces: normalized actor/task in; stable job descriptor and recorded dialogue answer out.
- [x] Add failing tests for stable class, current/result/stop/history answers, reported/unknown/returned/stale and no-task cases.
- [x] Implement small pure helpers on existing model exports; no new asset/module dependency.
- [x] Run targeted and complete Node tests.

## Task 2: Workroom focus and readable interaction
Files: work-dashboard/visualizer.js, visualizer.css, chatgpt-mcp/tests/check_admin_ui.py.
Interfaces: Task 1 helpers; existing snapshot, callbacks, motion and speech remain unchanged.
- [x] Add browser regressions for compact mission/map, factual talk, class/action labels, selection/project reset and related log routing.
- [x] Fold secondary connection/work/help information, remove placeholder brain, put recent events after map, add compact mission HUD.
- [x] Show character job/tool badges and recorded action effect; add factual talk with related records and existing original-log button.
- [x] Keep mobile detail reachable immediately after selection and keyboard focus usable; preserve room-capacity/motion contracts.
- [x] Run browser tests and inspect desktop/mobile screenshots.

## Task 3: Parent navigation and first viewport
Files: chatgpt-mcp/admin.html, admin.js, admin.css, tests/check_admin_ui.py and static UI tests as needed.
Interfaces: existing view IDs/auth forms/deep links retained; workroom becomes default; overview becomes report/history only.
- [x] Add failing viewport/default-navigation tests and update expectations for intentionally moved panels.
- [x] Compact toolbar, remove duplicate workroom intro, place reports in record tab, preserve management routes and IDs.
- [x] Run complete UI/Node suites, Python relevant static/server tests, JS syntax and diff checks.

## Task 4: Review, integration and handoff
Files: DESIGN.md, spec/plan, docs/handoffs/2026-10-16-dashboard-rpg-stage1.md.
- [x] Fresh reviewer checks uncommitted diff against frozen baseline and review focus.
- [x] Fix material findings with regression checks. Capture actual isolated browser evidence (not a live rollout claim).
- [x] Selectively integrate only owned files/hunks whose original hashes still match; preserve other WIP.
- [x] Record results, known limits and deployment pending in handoff and DESIGN.md. No service switch.

Completed source-only 2026-10-16: Node 187, full browser 87, admin Python 25; integrated source Node 187 and browser 6. Final review focus loss reproduced and fixed. See handoff for deployment pending and evidence.
