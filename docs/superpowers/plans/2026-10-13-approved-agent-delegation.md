# Approved Agent Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP로 완료하지 못한 코딩 작업을 관리 웹에서 범위 승인 후 전용 Codex CLI 작업으로 실행하고 관찰 가능한 결과를 반환한다.

**Architecture:** A의 private plan/receipt·project snapshot 계약을 재사용한다. Codex profile·전용 runner·event parser·CAS 결과 반영을 분리하고 호스트 변경은 같은 승인 broker에 맡긴다. 실행 프로필의 실제 파일 격리를 검증하기 전에는 CLI 호출을 닫아 둔다.

**Tech Stack:** Python stdlib/unittest, 현재 Codex 0.162.1 CLI, 기존 MCP SDK·관리 웹·work-event telemetry. Claude adapter는 별도 검증 후 활성화하며 추가 SDK나 모델 서비스를 설치하지 않는다.

**Spec:** [설계안](../specs/2026-10-13-chat-host-operations-design.md), [전체 계획](2026-10-13-chat-host-operations.md), [A 계약](2026-10-13-registered-host-operations.md)

## Global Constraints

전체 계획의 Global Constraints를 적용한다. 호출은 추가 모델 사용이며 첫 smoke에도 구체적 사용자 승인이 필요하다. 모델 선택은 등록 profile의 명시적 ID만 사용한다. 단순 workspace-write·cwd·프롬프트는 읽기 격리의 증거가 아니다. 현재 사용자 CLI·폰 세션을 resume하거나 종료하지 않는다.

## Review Focus

- C1: 정책/승인 거부 또는 불확실한 이전 효과를 자동 위임으로 우회하지 않는다.
- C2: root/credentials/다른 레포 읽기와 legacy sandbox override를 실제 CLI에서 차단한다.
- C2: stdout JSONL 안의 reasoning·prompt·임의 text와 허위 usage/model 필드를 공개하지 않는다.
- C3: task 전용 PID generation과 session ID만 취소·재개 대상으로 삼는다.
- C3: 사용자 WIP/파일 mode/symlink/동시 변경을 CAS로 보호하고 모델 보고를 테스트 실행 증거와 구분한다.

---

## C1. Agent profile·작업 계획·웹 승인

**Files:** Create `chatgpt-mcp/agent_profiles.py`, `agent_tasks.py`, `agent_tools.py`, `tests/test_agent_profiles.py`, `tests/test_agent_tasks.py`. Modify A의 `host_registry.py`, `host_runtime_web.py`, `admin_server.py`와 관련 테스트.

**Interfaces:**
- `AgentProfileRegistry(host_registry)`의 `list_profiles(project: str) -> list[dict]`, `require(project: str, profile_id: str) -> dict`.
- `AgentTaskManager(projects, profiles, *, state_dir=None, runner=None)`.
- `prepare(project, *, profile_id, objective, reason, request_id, timeout_minutes=30) -> dict`, `approve_web(project, *, task_id, plan_digest, source_digest, confirmation) -> dict`.
- `async start(project, task_id) -> dict`, `get(project, task_id) -> dict`, `async cancel(project, task_id) -> dict`.

- [ ] **Step 1: 실패 테스트 작성.** disabled/unknown provider/profile/project, unknown fields, model/effort 자유 지정, timeout 범위, nonce/digest/replay, 공유 session ID 지정, 정책 거부·outcome_unknown을 위임 사유로 사용하는 입력을 거절한다.

```python
async def test_unapproved_task_never_starts_cli(self):
    task = self.tasks.prepare("tossApp", profile_id="codex-repo",
                              objective="Review and fix the failing unit test",
                              reason="WORKFLOW_COMPLEXITY", request_id="agent-1")
    with self.assertRaises(HostRuntimeError):
        await self.tasks.start("tossApp", task["task_id"])
    self.assertEqual(self.runner.start_count, 0)
```

- [ ] **Step 2: RED 확인.** SDK 인터프리터로 `test_agent_profiles.py -v`, `test_agent_tasks.py -v`를 실행한다.
- [ ] **Step 3: registry·명세 구현.** version 3의 `agent_profiles`는 project, provider, host-pinned executable/version, 명시적 model ID/effort, allowed_paths, host capabilities, apply_policy, isolation receipt 참조를 가진다. provider는 첫 구현에서 codex만 enable 가능하다. objective는 최소 작업 명세만 저장하며 기존 채팅·추론을 가져오지 않는다. timeout은 정수 1..60, 기본 30분이다. reason은 `MISSING_TOOL|WORKFLOW_COMPLEXITY|EXECUTION_ENV_UNSUPPORTED`이며 정책/범위 거부나 unknown 효과를 감출 수 없다.
- [ ] **Step 4: task·receipt·승인 UI 구현.** source/profile/executable digest·scope·model·시간·사용량 안내를 고정해 600초 plan을 만든다. 웹의 동일 fresh auth/Origin/trusted HTTPS 경계에서 task ID를 typed-confirm하고 1회 영수증을 만든다. `approved=true`와 model/session ID는 권한을 부여하지 않는다. CLI 실행은 start 도구의 별도 동작이다.
- [ ] **Step 5: GREEN 확인.** 인증 실패·LAN HTTP·만료·변조·중복 start에서 효과 0회 또는 기존 단일 작업 조회임을 검사한다. 기존 host approval과 agent approval을 섞으면 거절해야 한다.

## C2. 실제 파일 격리·Codex 실행·안전한 stream

**Files:** Create `agent_runner.py`, `agent_events.py`, `tests/test_agent_runner.py`, `tests/test_agent_events.py`, `tests/test_agent_isolation_probe.py`. Modify `agent_profiles.py`의 readiness 평가.

**Interfaces:**
- `compile_codex_profile(profile: dict, workspace: Path) -> list[str]`: trusted config override argv를 반환한다.
- `probe_codex_isolation(executable: Path, profile: dict, fixture_root: Path) -> dict`: models를 호출하지 않는 실제 sandbox probe receipt.
- `CodexAgentRunner`의 `async run(task: dict, workspace: Path, emit) -> dict`, `async stop(identity: dict) -> None`.
- `parse_codex_event(payload: dict) -> dict | None`: safe event, usage, observed model 또는 None.

- [ ] **Step 1: 실패 테스트 작성.** legacy `--sandbox`/sandbox_mode/bypass flags 부재, executable/profile digest drift, noisy/malformed/큰 JSONL·reasoning·prompt 필드 제거, PID 재사용·timeout·child group 종료, reported vs observed model/usage를 검사한다.

```python
def test_reasoning_and_raw_command_output_are_not_public(self):
    self.assertIsNone(parse_codex_event({"type": "reasoning", "text": "private"}))
    event = parse_codex_event(self.command_completed_with_private_stdout)
    self.assertNotIn("stdout", event)
    self.assertNotIn("command", event)
```

- [ ] **Step 2: RED 확인.** SDK 인터프리터로 신규 runner/events 테스트를 실행한다. fake executable만 사용하며 모델 호출은 하지 않는다.
- [ ] **Step 3: permission profile 구현.** 공식 beta profile의 `default_permissions`, `permissions.<id>.filesystem`, `extends=":workspace"`, `":root"="deny"`, `":minimal"="read"`, workspace write, 정확한 runtime read, credentials deny, sandboxed network disabled를 생성한다. `filesystem` 키를 사용하며 `file_system`은 사용하지 않는다. `--sandbox`/sandbox_mode와 혼용하지 않는다. 사용자 config를 무시하고 스냅샷에서 `.codex`·hooks·plugin 설정을 제외한다. managed 설정 때문에 profile을 적용할 수 없으면 닫는다. 저장 인증은 CLI frontend가 사용하며 모델 shell에 노출하지 않는다.
- [ ] **Step 4: 무료 offline 격리 probe 구현.** 0.162.1 로컬 help의 실제 형식은 `codex sandbox [OPTIONS] [COMMAND]...`다. `codex sandbox linux`를 사용하지 않는다. `-P <profile> -C <fixture-workspace>`와 고정 config overrides로 Python fixture probe를 실행해 workspace 읽기/쓰기 허용, 외부 sentinel·가짜 auth·symlink alias 읽기/쓰기·직접 network 차단을 확인한다. 실제 auth 파일 내용은 읽거나 출력하지 않는다. profile/version/runtime/platform/fixture assertions receipt가 없거나 stale이면 agent readiness는 `ISOLATION_UNVERIFIED`다. 정상 작업의 활성화는 승인된 실제 에이전트 종단 smoke receipt까지 요구한다. smoke는 호스트 테스트 harness가 정확히 승인된 fixture task만 실행하며 일반 MCP 인자로 검증 bypass를 요청할 수 없다.
- [ ] **Step 5: 전용 Codex runner 구현.** 검증한 absolute executable을 shell=False와 독립 session/process group으로 시작한다. `--no-daemon`, approval policy never, `exec --ignore-user-config --strict-config --ephemeral --json --output-schema --output-last-message`와 생성한 named permissions를 사용하며 해당 설치 버전의 parser 지원을 검증한다. `HOME`/`CODEX_HOME`을 다른 경로로 repurpose하지 않는다. 모델 인증 환경은 명시적으로 검증하고 불필요한 secrets를 상속하지 않는다. CLI session은 새 전용 작업으로만 생성한다.
- [ ] **Step 6: stream·budget·종료 구현.** JSONL은 line/미처리 buffer 256 KiB 상한과 safe-field whitelist를 적용하고 처리한 원문을 버린다. 실행 전체의 원문을 누적 저장하지 않는다. 단계·상대 파일 경로·actual usage만 저장하고 조회에는 이벤트 200건·64 KiB 상한을 적용한다. 실제 model을 알 수 없으면 unknown이다. 최종 schema는 summary/changed_files/validations/remaining/approval_required이며 모델 성공 보고는 별도 label로 남긴다. wall deadline은 승인한 timeout×60초, terminal 결과는 실제 exit/evidence를 따른다. token/금액 hard limit이나 다른 provider/model 자동 전환을 보장하지 않는다.
- [ ] **Step 7: GREEN·격리 확인.** fake runner/event 테스트, controlled sentinel의 actual offline probe를 통과한다. profile이 해당 binary에서 지원되지 않으면 새 host config·CLI 업데이트 없이 제한 상태를 보고하고 코드 시험은 그 fail-closed 경로를 증명한다. 실제 계정으로의 agent smoke는 승인 전 실행하지 않는다.

## C3. 결과 CAS·host broker·대시보드·패키징

**Files:** Create `agent_host_broker.py`, `tests/test_agent_results.py`, `tests/test_agent_host_broker.py`, `tests/test_agent_router.py`, `tests/test_agent_tasks_ui.mjs`. Modify `agent_tasks.py`, `agent_tools.py`, `server.py`, `admin_server.py`, `admin.js`, `admin.html`, `admin.css`, `project_state.py`, `install-runtime.py`와 관련 runtime/router 테스트.

**Interfaces:**
- `apply_agent_result(task: dict, workspace: Path, project_root: Path) -> dict`: applied_paths, review_required_paths, conflicts, validation_evidence 반환.
- `AgentHostBroker(tasks, host_operations)`는 `inspect`, `prepare`, `execute`, `get`만 제공하며 A의 project/target/action 계약을 사용한다. server-verified task scope와 실제 operation receipts가 권한 근거다.
- MCP tools는 `list_agent_profiles`, `prepare_agent_task`, `start_agent_task`, `get_agent_task`, `cancel_agent_task`이며 미등록 인자와 기존 session ID를 받지 않는다.

- [ ] **Step 1: 실패 테스트 작성.** untracked WIP 포함, 다른 세션 파일 변경·mode drift·symlink·삭제/생성 충돌, 고위험 제어 파일 자동 반영 거부, task scope 탈출·host receipt 재사용·legacy013 우회·broker 임의 argv 거부, actor/task ID 중복 방지를 검사한다.

```python
def test_concurrent_user_edit_is_preserved(self):
    self.project_file.write_text("user edit\n")
    result = apply_agent_result(self.task, self.workspace, self.project_root)
    self.assertEqual(self.project_file.read_text(), "user edit\n")
    self.assertIn("src/view.ts", result["conflicts"])
```

- [ ] **Step 2: RED 확인.** 신규 Python 결과/broker/router 파일과 Node `test_agent_tasks_ui.mjs`를 실행한다.
- [ ] **Step 3: 입력·결과 반영 구현.** A의 private source snapshot에 현재 허용 WIP를 포함한다. .git/.env/외부 symlink를 제외하고 실행 중 source scope를 고정한다. 승인한 일반 코드의 변경·신규·삭제는 byte/mode/type CAS와 project lock 하에서 반영한다. 충돌 또는 고위험 제어 파일은 private 검토 artifact와 상태를 반환하고 덮어쓰거나 자동 적용하지 않는다. agent diff/성공 선언만으로 실제 테스트 성공을 주장하지 않는다.
- [ ] **Step 4: broker 구현.** CLI는 host에서 pin한 stdio MCP broker 하나만 명시적으로 로드한다. 사용자/프로젝트의 외부 MCP나 plugin 설정을 자동 상속하지 않는다. broker는 실제 승인된 task의 lease와 범위를 검증하되 thread/session/cwd/marker만으로 권한을 추정하지 않는다. Docker/DB effect는 A/B의 fresh operation approval 후 execute한다. 미지원 동작은 adapter/config 추가 필요를 보고한다. 추가 host 승인이 필요하면 task는 `waiting_host_approval`로 보고하며 무승인으로 계속하지 않는다. 첫 릴리스에는 기존 사용자 session resume나 자동 재개를 추가하지 않는다.
- [ ] **Step 5: 관찰·취소·통합 구현.** task 상태는 pending_host_approval/queued/running/waiting_host_approval/review_required/succeeded/failed/cancelled/outcome_unknown이다. 전용 PID/start ticks/PGID와 task receipt를 확인해 취소한다. Docker/DB child operation은 별도 ID로 관찰하며 취소=원복으로 보고하지 않는다. events는 stable source/actor/task ID, source_kind codex-cli, phase/usage/result로 기존 work-event API에 연결한다. 관리 웹에 모델 설정/관측, 사용량, 추가 승인·검토·충돌 상태를 표시한다.
- [ ] **Step 6: GREEN·패키징·최종 종단.** MCP → 웹 승인 → fake CLI → source CAS → 안전한 결과의 1회 흐름을 검증한다. install manifest와 verifier에 새 runner/broker/modules를 포함하고 Tampered broker 검사를 추가한다. 전체 계획의 SDK/Node/lint/독립 리뷰를 통과한다. 실제 Codex smoke는 선택 모델·작업·사용량 승인 후 폐기 가능한 프로젝트에서만 수행하고, 이후 실제 등록 프로젝트 접근·결과 반영을 검증한다.

## 완료 기준과 제한

신규 호출 도구가 등록돼도 격리·인증·profile readiness가 불충족이면 에이전트 작업은 시작하지 않는다. Codex 정상 호출의 증거와 Claude 미활성 상태를 구분해 보고한다. 복잡한 작업을 위임할 수 있지만 승인된 범위 밖의 호스트 동작을 해결했다고 주장하지 않는다. actual smoke·호스트 rollout이 남으면 소스 완료와 실제 운영 가능 상태를 구분한다.

공식 근거: [Permissions](https://learn.chatgpt.com/docs/permissions), [CLI reference](https://learn.chatgpt.com/docs/cli/reference), [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode). beta/profile 동작은 현재 설치 binary에서 실제로 검증해야 한다.
