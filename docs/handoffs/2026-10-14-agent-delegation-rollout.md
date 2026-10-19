# Approved agent delegation — C1/C2/C3 source implementation and host rollout handoff
Date: 2026-10-14 (KST)
Repository: personal-ops
Scope: ChatGPT personal-ops-tunnel isolated file edits and synthetic validation only.

## User's three requests
1. Implement the approved host-agent delegation features (C1 plan/approval, C2 isolated CLI runner, C3 result CAS, MCP and admin UI).
2. Safely deploy them to the current personal-ops tunnel.
3. Invoke a host agent to diagnose and fix tossApp/tarororo-test BACKUP_INVALID, then verify migrations 006-013.

## Observed implementation status — source only

New source:
- `chatgpt-mcp/agent_profiles.py`: exact host profile validation (Codex only), pinned binary SHA, model/effort/path/receipt requirements; isolation and smoke receipt must be valid owner-only host evidence; profile missing or disabled => no run.
- `chatgpt-mcp/agent_tasks.py`: source-pinned private 10-minute task plans, bounded objective SHA, single-use management web approval receipt, state and cancellation; real plan exists only for operator-registered and independently verified profiles.
- `chatgpt-mcp/agent_runner.py`: one dedicated ephemeral Codex exec with prompt on stdin, nominal strict named permissions and JSONL; existing Codex sessions never resumed; interrupts stop only owned child group.
- `chatgpt-mcp/agent_events.py`: whitelist events only, never surface raw command/stdout/stderr/reasoning.
- `chatgpt-mcp/agent_results.py`: exact allowed paths, original bytes/mode CAS, single ordinary-file opt-in application; concurrent changes, high-risk control inputs, symlinks, deletes, multi-file edits => review required. Even applying source does NOT prove tests passed: agent task returns review_required, not functional success.
- `chatgpt-mcp/agent_host_broker.py`: DENY-ONLY host boundary. It intentionally **cannot** yet run arbitrary Docker or SQL and cannot inherit the legacy tarororo target grant.
- `chatgpt-mcp/agent_tools.py`: five MCP descriptors: list_agent_profiles, prepare_agent_task, start_agent_task, get_agent_task, cancel_agent_task.
- `chatgpt-mcp/agent-admin.js`: authenticated, textContent-only pending-task review/one-use approval UI. Approval does not launch Codex.

Modified existing:
- `chatgpt-mcp/host_registry.py`: permits only exact-schema validated nonempty v3 agent_profiles; v1/v2 don't grant agent access.
- `chatgpt-mcp/server.py`: lazy agent descriptor/catalog and tool dispatch; no new user-session operations.
- `chatgpt-mcp/project_state.py`: agent tool activity name allowlist.
- `chatgpt-mcp/admin_server.py`: authenticated GET /api/agent-tasks and /api/agent-task; POST /api/agent-approve uses existing strict loopback/trusted HTTPS Origin, fresh token, exact body policy.
- `chatgpt-mcp/admin.html`: agent approval section; script agent-admin.js.
- `chatgpt-mcp/install-runtime.py` and `host_runtime_operations.py`: add all new Python+JS files to immutable release manifest and integrity verifier.
- `chatgpt-mcp/rollout-host-runtime.py`: five tools added to independently declared must-register tool set and test targets.
- `chatgpt-mcp/tests/test_runtime_install.py`, `test_rollout_host_runtime.py`, `test_router.py`: updated declared tool/package contract.

Tests (all disposable/synthetic, never real Codex or DB):
- `tests/test_agent_tasks.py`: configuration, source checks, idempotent prepare, one-shot local approval, no-unapproved exec, CAS, sanitization, broker fail-closed.
- `tests/test_admin_agent_approval.py`: actual ephemeral localhost HTTP test server; bad host/Origin/token/body rejection, authenticated listing, approval without agent run.
- `tests/test_agent_results.py`: approved source CAS, concurrent WIP conflict, high-risk files, path traversal, symlink, deletion and multifile safety.
- Python agent/registry/rollout/install/admin subset: 57/57 PASS in isolated sandbox (job 0433c0e21115475ebde72a4e94dad3fb, exit 0).
- The installed SDK is absent from the isolated Python, so actual MCP SDK router/stdio verification is NOT complete. Old sandbox Node may not support syntax in unrelated admin.js. Only verified static syntax of new agent-admin.js is relevant.
- A prior 48/48 PASS checked installer/rollout/client core in isolated mocks.

## NOT done / gates for Q2

- No actual host file/home/tunnel control from ChatGPT `start_command`; it's sandboxed and must not be used to bypass the configured host approval.
- The host v3 `agent_profiles` configuration currently contains no approved profiles. No real `Codex 0.162.1` executable/version+SHA has been verified.
- The exact beta CLI permission configuration and workspace boundary require a real offline process isolation probe: workspace read/write allowed; outside sentinel, auth paths, symlink alias and network denied. None was performed. No verified isolation receipt or live-model smoke receipt has been issued. `AgentProfileRegistry.require` correctly fails closed until both receipts exist. A source-level mock receipt in a disposable test is not evidence of real isolation.
- No actual Codex CLI paid task or model call was made.
- Host broker currently deny-only; real host CLI task C3 host operations are not implemented. A host agent cannot diagnose protected backup dump via this interface yet.
- No immutable release installed, no systemd restart or tunnel refresh, and no Git commit/push or Docker/DB mutation was done. **Do not claim the new five tools are currently live in ChatGPT**.

## Host agent rollout sequence (requires host TTY / real control, not sandbox)

1. Inspect personal-ops Git WIP and active Codex/Claude sessions and host runtime. Protect dashboard, observer, admin, and other concurrent edits; never wholesale stage/reset.
2. Review the new modules for permission profile syntax on the exact pinned host CLI; run a local **offline** sandbox probe before accepting any profile receipt. Missing/failed probe => leave profile disabled, do not issue receipts. Separately authorize a paid disposable smoke run with explicit model/effort and no real project, then verify its output/usage before enabling an actual project.
3. Audit source CAS, process identity, cancellation and host broker with independent review. Never assume working-directory setting implies isolation.
4. Run required install/rollout regression and actual MCP stdio tool-list/router tests in the host's MCP SDK Python, including 5 new agents, existing 16 host operations, and existing projects. Failures stop rollout.
5. Run `rollout-host-runtime.py` in preflight mode on the real host; compare current live release digest, pending WIP, Codex app-server connection impact, admin/Tailnet URL and previous snapshot. No real target DB/Docker operations.
6. Only after review and proper scope authorization, run trusted local TTY `rollout-host-runtime.py --apply` and *manually type ROLLOUT*. Preserve old tunnel ID, runtime key and admin port; rollback if health/tool list mismatches. Do not pipe approval text or forge receipts.
7. Verify new immutable release, plugin tools/list all five new tools, management web trust/auth, and first read-only list_agent_profiles. Refresh the ChatGPT plugin to pick up the new catalog. Mark live deployment complete only after explicit health/tool evidence.

## Q3 pending work: controlled host agent task

After actual profile registration/approved model/verified isolation+smoke, create a new agent task scoped to `tossApp` with objective:
"Diagnose BACKUP_INVALID for old tarororo-test operation 8d60f5fe8091b87c8a0c0b0949965b40. Read only backup archive metadata (size, first 5-byte PGDMP bool, pg_restore --list exit status), runtime manifest, and non-secret docker/pg tool status. Do not disclose DB rows or secrets. Find root cause, propose safe fix with tests and source snapshot; do not silently bypass backup/restore checks, execute SQL, restart API/Worker, or reuse failed outcome_unknown operation."

Host-only backup `~/.local/state/chatgpt-coding-mcp/host-runtime/backups/8d60f5fe8091b87c8a0c0b0949965b40.dump` must be inspected through a separately registered/read-only broker capability with fresh host approval (not via arbitrary shell in agent, not available in current broker).

After real host backup and disposable restore validation success, fixed target approval/source/DB generation must be rechecked. Fresh `prepare_db_migration` for the `006~013` combined schema_update, explicit execution under still-valid grant, then `get_predecessor_migration_status` and `get_db_migration_status` verify 001-013 13/13, 14 predecessor tables / 14 indexes / 5 functions and 4 013 tables, DB identity unchanged, API healthy/ready, Worker/DB running, monetization disabled. No retries on outcome_unknown and no unapproved Docker deployment/restart.

Detailed prior BACKUP_INVALID host incident: `docs/handoffs/2026-10-14-tarororo-backup-invalid-recovery.md` in personal-ops; `tossApp/docs/handoffs/tarororo-test-2026-10-14-backup-invalid.md`.

## Host audit continuation — 2026-10-14 (supersedes completion assumptions)

The current user requested security review, real offline CLI isolation, readonly backup
diagnosis and no-change rollout preflight. Runtime switching, Docker changes and actual
DB restore/migration still require separate approval. No paid model run is authorized here.

- Source/WIP baseline: 129 files copied without Git commit/staging to detached worktree
  `/home/jhw/.local/state/personal-ops-worktrees/agent-delegation-20261014-d9wkbyov`;
  original HEAD `cf8b6d2608c9cadaa939fdca8045891e848ec7ba` preserved.
- Exact host Codex: 0.162.1 standalone binary SHA256
  `628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1`.
- Shipped dotted `-c` permission settings fail parsing. Supported inline-table profile
  and `/**` deny patterns pass real offline workspace read/write, outside read/write,
  fake credential roots, symlink aliases and AF_INET socket denial checks.
- A separate trusted static helper alias/layout is needed: the installed OpenAI release
  has no sibling `codex-linux-sandbox`. Tests use disposable private artifacts and do
  not provision a host helper or issue isolation/smoke receipts. Profiles remain empty.
- Review found source is a prototype, not evidence of completed C1-C3. Blocking defects
  include queued-cancel/spawn races, shared daemon usage, omitted reasoning effort,
  generic errors, weak receipt/runtime binding and snapshot quota. These are being fixed
  with regressions. Automatic original-source CAS is held inactive pending stronger
  process containment/descriptor-relative application and writer coordination proof.
- The planned host broker is still deny-only. Full planned PID/start-ticks persistence,
  output-schema/final artifact and work-event/broker approval integration are deferred;
  no claim of functioning privileged host-agent delegation is made.
- Host SDK verification: existing agent/admin/results and source/DB/router subset 28
  passed; new real stdio tests verify five tool registrations, readonly empty-profile
  listing, unregistered execution denial and fail-closed required module import (2 pass).
  Fixture trust is synthetic; this is not live plugin activation or a paid smoke receipt.
- Live connector has no five agent tool descriptors yet; current installed runtime is
  unchanged. No services, protected CLI sessions, host config or DB data were changed.

Local evidence: `/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/delegation-backup-review-e6k3459i`
and `/home/jhw/.local/state/personal-ops-rollout-recovery/20261014/codex-isolation-probe-oKEJIj89`.
Backup root cause/fix and actual retained-archive catalog success are in the companion
backup handoff. Rollout candidate/final test/review state will be appended after validation.

## Final safe candidate / explicit activation boundary

- Final source candidate: `adc2d2a1093abbe2862b`, 48 installed files. The only runtime
  differences from current release are agent integration/protection and backup validation;
  unrelated dashboard/observer WIP is not newly bundled.
- Preflight `rollout-ms53kzbn/receipt.json`: exit 0, `preflight_ok`, targeted 389 and full
  539 tests passed. Profile/service alignment passed. `installed=false`, `switched=false`.
  Current live release stays `d944e6138731d4b031b7` (MCP695527 / tunnel695511).
- Initial preflight `rollout-ol0wwxmj/receipt.json` failed 389 targeted tests with one
  failure and one error. New agent imports had pulled optional host_registry into server
  startup. Lazy source-review imports retain legacy readonly availability and close
  delegation with REGISTRY_NOT_CONFIGURED. The two old regressions plus agent SDK/task
  tests passed (30), then full preflight passed. Preserve the failed receipt and
  `targeted-failure-detail.log`; do not rewrite history as all-green.
- Root Ruff passed 11 changed Python files. Independent review passed 63 scoped tests,
  Python compile and agent-admin.js syntax. The verdict is REQUEST CHANGES for real
  agent activation, acceptable only for a deliberately inactive tool/catalog rollout.
- Corrected safeguards: inline CLI permissions, no shared daemon, actual effort override,
  queued/spawn cancellation and PID generation checks, full-lifecycle timeout, bounded
  private stderr, safe failure codes, policy/binary/helper-bound receipts, source/history
  caps (16MiB per task, 256MiB retained sources, 1000 retained task records), original mode.

Real agents are still unavailable by design: host profiles are empty; actual helper
provisioning is missing; production supervision remains AGENT_SUPERVISION_UNVERIFIED;
automatic CAS is CAS_UNVERIFIED. No paid call or valid smoke/isolation receipt exists.
The broker remains deny-only, and full result/progress/usage integration and safe history
cleanup remain future work. Registering five MCP tools must not be described as completed
C1-C3 or permission to perform Docker/DB host operations.

Runtime switch approval can install this candidate and restart only the managed tunnel
and admin services, retaining existing host config/credentials/grant, old releases and
other CLI sessions. It does not provision/enable an agent, execute SQL, restore DB data,
restart Docker containers or deploy API/Worker. Review current running jobs before switch.

Concrete switch command (NOT executed; separate approval required):

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 PATH=/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin script -q -e -f -c '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/delegation-backup-review-e6k3459i/rollouts' /home/jhw/.local/state/personal-ops-rollout-recovery/20261014/delegation-backup-review-e6k3459i/activation-terminal.log
```

After approved switching: prove actual live snapshot, readonly list_agent_profiles and
all five tool registrations, management UI auth, unchanged containers/config/grant, and
safe backup validation against the retained archive. Plugin metadata may need refresh.
Actual DB restoration/migration still needs its separate explicit execution approval.


## 승인된 런타임 전환 결과 — 2026-10-14

관측 시각: 2026-10-14T11:09:00.644512+09:00. 이 절은 앞의 “후보 미설치/미전환” 상태를 갱신한다.
사용자가 후보 설치 및 터널·관리웹 전환을 명시적으로 승인했고, 그 범위만 적용했다.

- 실제 live snapshot: `adc2d2a1093abbe2862b`; 이전 `d944e6138731d4b031b7` release 보존.
  설치 manifest 48개 파일의 SHA256과 실제 MCP PID 117524 /
  tunnel PID 117496의 실행 경로·세대를 확인했다.
- 배포 직전 필수 389개 / 전체 539개 테스트 모두 exit 0. 승인한 source/control
  digest 일치 및 queued/running 작업 0개를 재확인한 뒤 전환했다.
- 원본 receipt: `rollouts/rollout-wnjdx1kh/receipt.json`.
  `installed=true`, `switched=true`, `runtime_verified=true`, `admin_status=admin_ready`.
  명령 exit 2 / `verification_required`는 서비스 전환 실패가 아니다.
  `catalog_status=awaiting_discovery`, `connector_schemas_verified=false`이며
  ChatGPT 연결의 도구 목록 새로고침·후속 검증이 남았음을 그대로 보존한다.
- 설치된 immutable runtime을 실제 MCP SDK/stdio로 호출했다. 신뢰 검사 우회나
  모의 profile 없이 실제 host config와 별도 일회용 XDG state를 사용했다.
  전체 49개 도구 중 신규 5개와 기존 필수 host 도구들이 등록되어 있고,
  `list_agent_profiles(tossApp)`는 `profiles=[]`였다. 미등록 프로필 계획은
  PROFILE_NOT_FOUND, 모델이 넣은 추가 승인 인자는 오류로 거부했다.
  실제 live main 채널의 신규 도구 discovery와 ChatGPT schema 캐시는 아직 미검증이다.
- 관리웹 PID 117844도 같은 release다. Loopback / LAN / Tailnet HTTPS
  페이지 HTTP 200, 신규 agent-admin.js SHA 일치, 에이전트 승인 영역 존재를 확인했다.
  인증 없는 agent API는 401, 정상 관리자 인증 조회는 200 / 작업 0개다.
  관리웹이 표시되는 사실은 실제 에이전트 실행 준비 완료를 뜻하지 않는다.
- 실제 ChatGPT 커넥터의 기존 조회 도구는 재연결되어 API Running/Healthy/Ready,
  Worker와 PostgreSQL Running, monetization disabled를 반환했다.
  001~005 checksum_match / 006~013 missing, ready_to_prepare=true, complete=false.
  DB postmaster epoch `1791788137.513100`와 Docker
  3개의 ID/image/started_at/state/health는 전환 전과 동일하다.
- 설치된 backup validator로 기존 134,716-byte 덤프의 읽기 전용 catalog 검증 PASS,
  SHA256 동일. 실제 disposable restore, DB migration, Docker restart/deploy,
  유료 모델 호출은 수행하지 않았다. 실패 작업 `8d60f5fe8091b87c8a0c0b0949965b40`의
  outcome_unknown / BACKUP_INVALID 상태를 보존했고 재사용하지 않았다.
- host registry envelope / runtime key / admin token / 기존 target grant의 bytes
  fingerprint 동일. source/control WIP 및 Git HEAD cf8b6d2도 유지했다. 커밋 없음.
- 전환 전 외부 coding process 15개 중 11개는 같은 PID/start-ticks로 유지됐다.
  독립 bun supervisor 2134546(session-34.scope)의 Claude 자식 4개는 구간 중
  종료된 것으로 관측됐다. 종료 원인은 수집하지 않았으므로 모두 보존됐다고
  주장하지 않는다. 이 작업은 두 승인 서비스 외 PID에 signal을 보내지 않았고,
  현재 호스트 CLI는 별도 session-35.scope에서 계속 동작한다.
- 프로필 0개, helper/supervision 미완료, broker deny-only, CAS 비활성은 유지한다.
  신규 도구 설치를 C1-C3 실제 에이전트 활성화 완료로 보고하지 않는다.

추가 증거는 기존 private evidence directory의 `activation-before.json`,
`activation-terminal.log`, `activation-verification.json`,
`activation-connector-readonly.json`, `activation-final-audit.json`에 있다.
모든 파일은 current-user 소유 regular 0600이며, receipt 원문은 수정하지 않았다.

ChatGPT 인계: personal-ops-tunnel 연결을 새로고침한 뒤 신규 도구 목록과
`list_agent_profiles(project="tossApp")`의 빈 목록을 확인한다. 새 대화에서
이 문서와 backup recovery 문서의 최신 전환 결과를 읽고 이어간다.
프롬프트 승인만으로 agent_profiles를 만들거나 host isolation/smoke receipt를
발급하지 않는다. 실제 에이전트 활성화/유료 smoke는 후속 설계·별도 승인 대상이다.

## ChatGPT 플러그인 Refresh 확인 및 tossApp 스키마 적용 — 2026-10-14 11:27 KST

- Refresh한 ChatGPT 클라이언트의 실제 연결된 MCP 도구 목록에 `list_agent_profiles`, `prepare_agent_task`, `start_agent_task`, `get_agent_task`, `cancel_agent_task` 다섯 도구가 존재함을 직접 확인했다. 따라서 과거 '도구 미노출' 상태는 **해소**됐다.
- `list_agent_profiles(tossApp)` 및 `list_agent_profiles(personal-ops)`는 모두 `profiles=[]`. 신규 에이전트 작업/모델 실행/별도 관리자 승인은 여전히 수행하지 않았으며 helper, supervision, broker deny-only 제약은 남아 있다.
- 별도의 기존 승인 대상 전용 마이그레이션 도구 `prepare_db_migration`은 `operation_id=50ea927a042d42a274d02daa5ca3d198`, `component=schema_update`, `006~013` 범위, `ready_to_execute`, `approval_required=false` 계획을 생성했다.
- 사용자의 계속 진행 요청에 따라 `execute_db_migration`을 실행했으며, 내부 backup/disposable restore/원자적 schema 적용/postcheck를 거쳐 `succeeded / COMPLETE`. 백업 134716 bytes, SHA256 `2ecb98c488ceaef0c5761db1c37ee728d04a3fbc4a12f364250ca5f32561d1e9`.
- 독립 조회: 001~013 13/13 `checksum_match`, 14 predecessor tables/14 indexes/5 protection functions 및 013의 4 tables 모두 존재, Schema 013 `ready`/prerequisites true, 예상 외 history 0. API Healthy/Ready, Worker+DB Running, DB/컨테이너 세대 보존, monetization disabled.
- 에이전트 프로필 미등록은 남아 있으나 DB schema 적용에 에이전트 실행은 불필요했다. 과거 실패 `8d60f5fe8091b87c8a0c0b0949965b40`는 재사용하지 않았다. 자세한 사후 검증은 `tossApp/docs/handoffs/tarororo-test-schema-006013-applied-2026-10-14.md`.
- `get_predecessor_migration_status.complete=false`는 013이 누락된 중간단계에서만 true가 되는 기존 preflight 설계이며, 통합 성공 실패를 의미하지 않는다. 같은 API의 post-migration `object_collisions`도 정상 신규 인덱스/013 테이블을 나열한다. 향후 읽기 전용 API 개선 후보.
- 이 턴의 실행에서 API/Worker 재배포·재시작, Git commit/push, Toss 앱 업로드, agent model invoke는 하지 않았다.


## Latest continuation: restricted agent readiness

See [2026-10-14-agent-readiness-followup.md](2026-10-14-agent-readiness-followup.md) for the subsequent source fixes, real free isolation/supervision proof, security re-review, config CAS, failure-test diagnostics and exact remaining activation scope. It supersedes the old boolean-supervision and deny-only-broker implementation notes above, while keeping nonempty host capabilities, CAS and actual model activation disabled pending their stated gates.

The later approved rollout made `6a21226cd332b242bbfb` live. Its first real model
fixture failed on a missing `codex-code-mode-host`; no profile was enabled.
The follow-up now records the failure evidence and a reviewed, model-free-tested
58-file repair candidate `f27541a01e22e5499614`. The corrected runtime switch
and one replacement model fixture remain a separate approval; the prior one-task
model allowance is already consumed. Continue from the follow-up's latest section.

Subsequent explicit approval completed the corrected rollout and replacement
fixture: live runtime `f27541a01e22e5499614`,
`codex-tossapp-review` (`gpt-6.1-sol/high`) reports `ready=true` in the actual
connected MCP. The real fixture output and isolation/smoke receipts verified,
management web/API are healthy, and existing registry targets/DB state were
preserved. Read the follow-up's final **Corrected runtime and restricted profile
activated** section for proof and the remaining normal-task approval,
review-only source copies, disabled CAS and disabled privileged-agent limits.
