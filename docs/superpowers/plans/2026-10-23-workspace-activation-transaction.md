# Workspace First Activation and Recovery Implementation Plan

> For agentic workers: continue the existing approved Workspace design with serial, test-first native executor units. No commits, push or PR. User selected nextsteps2/3/4; owner policy fields and registration decisions remain explicit data inputs.

**Goal:** Implement a real first-activation/recovery CLI and its host/legacy proof adapters, resolve evidenced blockers, then verify T14 through the active policy without regenerating experiments.

**Architecture:** Reuse existing safe paths, WorkspaceAuthority, immutable release verification and the stable two-file controller. Add a durable owner transaction that observes actual process/service/file state; reports and Boolean flags never substitute for evidence. Stage complete first authority before permanent latch/no-clobber commit. Preserve all historical uncertain outcomes and never restore revoked grants or remove latch on failure.

**Tech Stack:** Existing Linux/Python3.12, standard library, MCP SDK, unittest; no dependencies.

**Spec:** /home/jhw/ai/opencode/projects/personal-ops/.omx/plans/2026-10-21-workspace-policy-readiness.md, especially A/D/T14/T15. Existing implementation plan Task5 defines the blocked owner CLI seam. Latest source baseline is runtime1cbb5a83c200e3fbe98d.

## Constraints and review focus

- Keep exact owner policy/projects/release/controller/unit/profile digest pins and original modes. Working branch feat/workspace-policy-20261021; do not stage existing WIP.
- Actual protected results/summary remain unread until formal policy/authority/MCP admission. Exact JSON keys must be owner-confirmed. No unrelated experiments, model calls, DB/Docker/Yocto work.
- Real drain includes known job/agent/command/host/rollout workers and leases, PID/startticks/boot/cgroup. Unknown historical outcome is not itself a live reservation.
- Legacy invalidation occurs under native store locks using existing schema/revocation semantics; terminal forensic records remain byte-identical. No generic JSON rewriting or restoration of grant backups.
- Every irreversible step has durable intent and independently observed postcondition. Changed/corrupt/missing evidence fails closed. Unsupported rollback leaves services stopped; no return to legacy_allowed after latch.
- Stable controller imports no candidate code before admission and remains the existing two-file bundle. Lazy owner-CLI imports must not weaken launcher checks.
- Runtime apply is only after exact target/rollback semantic verification, independent review and complete owner inputs. T14 uses existing output only; prior41-case analysis is already recorded complete.

## Unit 1: Crash-safe first authority

Files: workspace_authority.py; tests/test_workspace_initialization.py.

1. Write real-filesystem failing tests for preparation, latch-before-rename crash, rename-before-return crash, no-clobber, tampered/missing/both state, no reset.
2. Observe RED with the installed SDK interpreter.
3. Add owner-only prepare_initial/commit_initial, unique private stage and exact manifest, permanent final latch before no-clobber publish and parentfsync. Keep initialize API behavior.
4. Run new tests plus existing authority/control tests and lint.

## Unit 2: Actual host and legacy evidence

Files: new workspace_activation_host.py and workspace_activation_legacy.py; focused test modules; minimal existing rollout/store helper extractions only where required.

1. Pin real service/profile/process identities and known record stores; write failing tests using real filesystem/process fixtures.
2. Add bounded stop/observe/wiring/start/verify adapter for only the recognized two services and known workers. Never infer whole drain from tunnel PID alone.
3. Add store-specific revoke/cancel/CAS adapters and preservation of verified terminal historical uncertainty.
4. Test PID reuse/live lease/changed unit/record/receipt failures and original validators rejecting old grants/pending. No live mutation during fixtures.

## Unit 3: Owner activation transaction and CLI

Files: new workspace_activation.py; workspace_control.py owner CLI; tests/test_workspace_activation.py; packaging and official targeted-test registration.

1. Write RED transaction/resume tests that use the actual authority and file adapters, not a Boolean-success mock chain.
2. Implement prepare/status/apply/resume with owner-only journal and exact current observations at every effect. Canonical journal root is the private state sibling workspace-activation; transaction names are 32 lowercase hexadecimal characters, directory mode0700 and single-link regular files mode0600. Each write has a fixed-inode flock and SHA CAS, atomic replacement and parentfsync. Pin coordinator source, target/rollback manifests, minimum enforcement ABI, policy/projects bytes and paths, controller manifest and host selection before any mutation.
3. Order: immutable pins -> durable maintenance intent and exclusive admission gate -> drain -> original store invalidation -> managed controller/wiring -> policy/staged authority -> re-observe proofs -> permanent commit -> clear maintenance only against actual committed binding -> managed start/health. The full prepare_initial descriptor must be fsynced in the journal before commit_initial can create the permanent latch. Test recovery from a fresh subprocess, including latch-before-rename and rename-before-journal-ack windows.
4. Leave plain initialize/migrate Boolean declarations denied. Enter transaction only from verified owner plan/local CLI. Resume uses the same pinned code and postconditions.
5. Test crash matrix/CAS/unsupported rollback and full component integration. Run official filtered regressions and independent review; fix findings.

### Admission, invalidation and publication contracts

- Deploy the new admission-capable release while still in legacy mode before actual first activation. Maintenance takes the exact owner rollout lock, then the exclusive activation lock; normal creation takes shared activation admission across fresh record and worker creation. Order is rollout -> activation -> native store -> authority. Existing processes are drained by physical PID/startticks/boot/cgroup/lease observations; a marker alone is not drain proof.
- Durable maintenance intent survives CLI crashes and denies launch and new jobs, agents, commands, registered/runtime operations and rollout dispatch even when authority is absent. Resume requires the exact transaction and coordinator source digest. Only an actual matching committed authority binding permits clearing intent. Unknown, replaced or corrupt lock/intent fails closed.
- Fsync the complete original legacy inventory, with every record/receipt mutation intent, before invalidation. Preflight the complete inventory before the first mutation; each native-lock operation rechecks its original pin or the exact already-invalidated native state. Resume at every Nth mutation re-observes this same inventory. Terminal forensic bytes remain exact; a partially removed receipt never recreates authority.
- Policy publication uses original-byte/inode/owner/mode pins, a private backup, an exclusive create or SHA CAS and fsync. Projects are separately pinned and never rewritten during apply. Controller installation occurs only from the pinned source and is independently reverified.
- A target/rollback allowlist entry is insufficient by itself: each release must pass manifest validation, owner-pinned minimum enforcement ABI and a staged-authority semantic launch check. Commit records installation_id/epoch/policy_digest. Startup compares policy digest, actual authority binding, both managed commands and process health. Unsupported rollback keeps both services stopped behind the permanent latch; it never restores grants or legacy commands.

## Unit 4: Owner blockers and T14

1. Correct only runtime ancestor0775 to0700 with pinned inode and validator evidence.
2. Follow the owner's selected backup-and-exclude decision for only missing pim-check-camera13; preserve pim-check and every other mapping. Pin the corrected mapping for activation.
3. Obtain actual allowed JSON keys from owner without assistant bypassing policy; exact candidate only.
4. Pin reviewed target and compatible rollback. Prepare concrete policy/epoch/service transition; actual apply remains blocked on unresolved inputs or findings.
5. Through actual active MCP policy, read exact existing Toss output with cursor/hash and bounded fields; record limitations and zero additional model/DB activity.

## Stop conditions

Source/config/record drift, unowned live execution, missing owner schema, unsafe path/type/link, unsupported rollback, unproven drain, or lost/corrupt authority stops the dependent effect. Continue independent authorized implementation and preserve evidence under .omx/plans/2026-10-23-workspace-activation-execution-evidence.
