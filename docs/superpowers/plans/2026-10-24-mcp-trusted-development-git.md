# MCP Trusted Development and Git Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 반복 승인·profile·refresh를 줄이는 MCP를 Git clone만으로 가져와 새 WSL 환경에 설정할 수 있게 한다.

**Architecture:** 검증된 기존 소스만 최신 origin/main 위의 전용 브랜치에 추가한다. 기존 범용 실행 도구와 Workspace authority를 재사용하고, 설치 단위의 명시적 v2 grant 전환을 기존 소유자 관리 절차에 연결한다. 런타임 설치와 새 PC의 키·권한 발급은 Git 공개와 별개다.

**Tech Stack:** Python 3.12 / unittest, 기존 MCP SDK 및 Serena, JavaScript / node:test, 기존 Workspace launch controller. 새 제품 의존성은 추가하지 않는다.

**Spec:** `docs/superpowers/specs/2026-10-24-mcp-trusted-development-design.md`

## Global Constraints

- 사용자는 구현·검증·Git 공개를 요청했다. 원본 main 및 기존 worktree WIP를 보존한다. push 대상은 새 feature branch이며 main merge/운영 배포는 이번 작업에 포함하지 않는다.
- 실제 pim-check 개발·pytest/Ruff/타겟 QA는 웹에서 한다. 여기서는 MCP 검사와 가짜 임시 프로젝트만 사용한다. Yocto/BitBake 태스크·캐시 변경은 실행하지 않는다.
- v2는 `lifetime = timed | until_revoked`; null 만료는 명시적인 v2 until_revoked에서만 유효하다. pending card는 7일, timed grant 최대 30일, 일반 job 최대 1800초다.
- 기존 grant를 자동 승격하지 않는다. 소유자 설정 후 카드에서 새 권한을 발급한다. 설치 OS 계정 권한이며 cwd는 파일 격리가 아니다.
- v2 설정은 active Workspace authority, quiescence, admitted ABI 3 runtime 및 minimum ABI 3을 먼저 요구한다. 비활성/구버전 설정에서는 구체적인 owner setup 오류를 반환한다.
- 기존 v1 상태는 의미를 보존한다. v2 전환 표식 뒤에는 손상·누락·철회 시에도 v1 fallback 금지. 카드 option revision과 설치 grant-state revision을 구분한다.
- 권한 지속성과 runtime 파일 검증은 분리한다. admitted release 갱신·boot·Workspace epoch 변경만으로 v2 grant를 폐기하지 않는다. 관리되는 권한 계약 generation 변경은 무효화한다.
- artifact 정책 v2는 `content_mode = json_fields | text`; v1 의미 보존, text fields는 빈 배열, 파일 1 MiB·페이지 64 KiB 상한, 민감 경로·링크·특수 파일·중첩 namespace 거부 유지.
- 비밀 키·grant/state·호스트 설정·로그·venv·원본의 별도 WIP를 Git에 넣지 않는다. 커밋은 jhw-commit v1 한국어 계약을 지키며 확인된 관련 참조는 https://github.com/jhw7500/personal-ops/issues/66 이다. 이 Issue를 자동으로 닫거나 갱신하지 않는다.
- MCP 검사는 XDG/umask를 격리하는 `rollout_test_runner.py`로 실행한다. 명령의 `python`은 `/home/jhw/.local/share/uv/tools/serena-agent/bin/python`이며 cwd는 `chatgpt-mcp`다. 모든 shell 명령은 `rtk`로 시작한다. focused module 실행에는 `env PYTHONPATH=.:tests`와 `--require-excluded-count 0`을 쓴다.

## Review Focus

- 전환 중 crash 또는 손상된 v2 상태: 과거 grant와 callback이 다시 유효해지지 않아야 한다. Task 1 테스트.
- 기존 floor 2 설치를 읽는 새 ABI 3 controller: 선언 ABI 검사를 건너뛰지 않아야 한다. Task 1 테스트.
- UI 응답 유실·재시도 및 이미 활성인 grant: 새 카드/중복 실행 없이 기존 상태를 반환해야 한다. Task 2 테스트.
- 새 QA 디렉터리·UTF-8 경계·바이너리·민감 JSON: 생성 전 등록은 허용하되 읽기 검사는 유지해야 한다. Task 3 테스트.
- 다른 Linux 사용자/깨끗한 clone/기존 owner 설정: 하드코딩된 서버 경로나 덮어쓰기 없이 setup이 동작해야 한다. Task 4 테스트.

---

### Task 1: 설치 단위 grant 저장과 ABI 3 전환

**Files:** Modify `chatgpt-mcp/host_access.py`, `workspace_control.py`, `workspace_authority.py`, `install-runtime.py`; create `host_access_installation.py` only if separating the storage contract keeps HostAccess readable. Tests: `tests/test_host_access.py`, new `tests/test_host_access_installation.py`, `tests/test_workspace_control.py`, `tests/test_workspace_authority.py`, ABI compatibility fixtures in `tests/test_workspace_activation_gate.py` and `tests/test_workspace_activation_transaction.py`.

**Interfaces:**
- Consumes: existing `WorkspaceTools`, `workspace_host.publication_lane`, `workspace_control.verify_launch` and authority private-file helpers.
- Produces: `HostAccess.enable_installation_scope() -> dict` owner-only Python API, `HostAccess.installation_scope_enabled() -> bool`, `HostAccess.request(project, *, lifetime="timed")`, `approve_web(..., lifetime="timed")`; `get/require/guard` return v2 `version`, `lifetime`, `grant_state_revision` with nullable `expires_at`.
- `WorkspaceAuthority.initialize(policy_digest, allowed_releases, *, minimum_abi=1)` preserves legacy default; new setup can explicitly request 3. Trust generation is a declared versioned capability contract in the owner-admitted runtime; code integrity/admission must be checked independently on each v2 access.

- [x] **Step 1: Write behavior regressions before implementation.** Cross-project grant reuse, restart/boot/compatible runtime/policy epoch persistence, timed expiry, strict malformed legacy null rejection; active work blocks mode transition; v2 missing/corrupt/revoked state never reads v1; old callbacks fail after approval/revoke; unknown/future ABI and floor 2 validation remain strict. Name tests after these observable behaviors.
- [x] **Step 2: Run focused RED tests.** `python -B rollout_test_runner.py --modules test_host_access test_host_access_installation test_workspace_control test_workspace_authority --require-excluded-count 0`. Expected new behavior assertions fail before implementation; preserve output in the task report.
- [x] **Step 3: Implement storage, admission and migration.** One installation lock serializes v1/v2 starts and state changes in the new runtime. Owner enable holds quiescent publication lane, verifies ABI/admission, persists the no-fallback marker before migration, invalidates every v1 grant/request, creates exact v2 state/revision, and never issues a grant. State reads fail closed. New request/approval/revoke operations bind installation ID/UID/capability generation and monotonic state revision. Opening a card must not silently migrate or revoke v1 state.
- [x] **Step 4: Run focused GREEN tests and the MCP suite.** Use `rollout_test_runner.py --discover tests --require-excluded-count 5` with the installed Serena Python; expected no new failures. Existing baseline failures, if any, must be named, diagnosed and fixed before publication.
- [x] **Step 5: Commit this task using jhw-commit v1 and record actual evidence.** No push from a child agent.

### Task 2: 승인 UI·범용 실행·도구 안내 연결

**Files:** Modify `chatgpt-mcp/host_access_ui.py`, `host_access.py` only for version-aware card request lifecycle helpers, `host-access-approval.html`, `host-access-admin.js`, `admin.html`, `admin_server.py`, `host_execution.py`, `host_execution_tools.py`, `server.py`; relevant host-access, host-execution, router Python and UI Node/browser tests.

**Interfaces:**
- Consumes: Task 1 lifetime/version/revision contract and explicit owner enable boundary.
- Produces: existing approval tools/admin API accept optional `lifetime`; active suitable grant returns existing authorization without a pending card. Existing execution APIs preserve request-ID dedupe and job timeout/cancel behavior with nullable v2 expiry.

- [x] **Step 1: Write tests before implementation.** Assert active grant causes zero extra requests/cards, stronger capability request still requires approval, stale option/card callbacks fail after revoke, until_revoked has no date parsing/clamp, timed grants still clamp, ten jobs reuse one grant with distinct request IDs, same ID never executes twice, UI accurately displays installation/OS account scope and lifetime.
- [x] **Step 2: Run focused RED tests.** Host-access approval/router/admin, host-execution and Node UI tests; expected new assertions fail for missing behavior.
- [x] **Step 3: Implement existing-tool integration.** Preserve spent-before-issue semantics and hidden confirmation tokens. Keep finite TTL for cards and isolated agent plans. Do not convert unrelated `agent_tasks.expires_at` into nullable grant expiry. Correct server instructions to use generic host execution when authorized and fixed profiles only for their dedicated workflow. Do not mislabel write tools as read-only.
- [x] **Step 4: Run focused GREEN, Node UI tests and the MCP suite.** Record browser bridge mock coverage separately from actual ChatGPT end-to-end observations.
- [x] **Step 5: Commit this task using jhw-commit v1.** No runtime deployment or existing-user grant change.

Task 2 evidence note: initial Node/browser RED output was not retained; Python RED and final GREEN were recorded. Fix round 1 separately captured failing Python and Node regressions before correction.

### Task 3: QA 산출물 디렉터리 단위 읽기

**Files:** Modify `chatgpt-mcp/workspace_policy.py`, `workspace_tools.py`, `workspace_files.py` only where required, and policy-version consumers in activation validation; tests `test_workspace_policy.py`, `test_workspace_tools.py`, `test_workspace_files.py`, `test_workspace_activation.py` and router tests as needed.

**Interfaces:**
- Consumes: ABI 3 admission and unchanged WorkspaceAuthority binding/cursors.
- Produces: policy version 2 artifact `content_mode` plus unchanged `read_registered_artifact`/listing tool names and signed-page shape. Version 1 remains exact legacy schema.

- [ ] **Step 1: Write tests before implementation.** Register a not-yet-created reports directory, create ignored UTF-8 JSON/text with new keys, then read without new registrations. Assert json_fields projections stay restricted; text requires empty fields; mixed overlapping namespaces, oversized/binary/symlink/secret-bearing files remain denied; pagination respects UTF-8 boundaries and invalidates stale cursors.
- [ ] **Step 2: Run focused RED tests.** `python -B rollout_test_runner.py --modules test_workspace_policy test_workspace_tools test_workspace_files --require-excluded-count 0`; expected new-mode assertions fail.
- [ ] **Step 3: Implement exact version-aware schema/read behavior.** Keep existing read/list policies, protected-path checks, file race defenses and byte limits. Text reads remain explicit owner-directory rules; no shell fallback and no automatic broadening of existing installations.
- [ ] **Step 4: Run focused GREEN and full MCP suite; update tool descriptions with accurate supported content.** Runtime admission/activation tests cover v2 policy acceptance only in compatible managed release.
- [ ] **Step 5: Commit this task using jhw-commit v1.**

### Task 4: Git clone 기반 WSL 초기 설정과 배포 검증

**Files:** Create `chatgpt-mcp/bootstrap.py`, `chatgpt-mcp/tests/test_bootstrap.py`, `chatgpt-mcp/WSL-SETUP.md`; update `chatgpt-mcp/README.md`, root `README.md`, selected `.gitignore`, and design/review status notes. Include dashboard tests/support documentation only after checking their provenance and dependencies.

**Interfaces:**
- Consumes: installed runtime manifest, Task 1 owner enable API and minimum ABI 3, Task 3 policy v2. Bootstrap creates fresh local configuration only through an explicit `--apply` flag; default is a read-only plan.
- Produces: portable setup for actual Linux home/projects with per-PC keys/state omitted from Git, immutable runtime/controller paths and next steps for tunnel creation. Repeat application preserves matching files and refuses differing existing owner config. No automatic tunnel credentials, owner grants, service switch, PIM tests or board contact.

- [ ] **Step 1: Write tests before implementation.** Use isolated HOME/config/state and fake projects to assert default performs no writes; explicit apply creates private new settings at actual user paths; repeat is idempotent; existing/different/symlinked config fails without overwrite; install manifest completeness is validated; no credentials or grants are created by configuration.
- [ ] **Step 2: Run focused RED tests.** `python -B rollout_test_runner.py --modules test_bootstrap --require-excluded-count 0`; expected new bootstrap behavior fails.
- [ ] **Step 3: Implement minimal stdlib bootstrap and clone-first guide.** Document required existing Serena/MCP dependency versions and official installation references, optional Codex profile readiness, loopback admin, PC alias, fresh identity/key flow, managed launcher usage and environment-only completion checks. Remove any requirement to transfer the temporary source archive. Add the module entry to root README without overwriting other remote changes.
- [ ] **Step 4: Validate focused GREEN, full MCP suite, UI/static checks, secret scan and a fresh local Git clone.** Verify the clean clone imports/discovers the complete MCP and install inventory, and default setup uses no original checkout path. Record actual Linux validation separately from unrun Windows/live ChatGPT checks.
- [ ] **Step 5: Commit using jhw-commit v1, run independent whole-branch review, fix material findings, then push only `feat/mcp-trusted-development-wsl` to origin.** Confirm remote commit and provide exact `git clone --branch` and setup commands. Existing issue #66 is a related source-versioning reference; no automatic closure.
