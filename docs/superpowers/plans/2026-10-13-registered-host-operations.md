# Registered Host Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 등록된 test Compose 대상의 작업을 채팅에서 계획하고 기존 관리 웹의 일회용 승인 후 실행·검증한다.

**Architecture:** legacy HostRuntime을 유지하고 version 3 registry·generic operation manager·Compose adapter를 추가한다. 작업 상태의 private JSON·lock·digest·timestamp 유틸리티는 기존 코드를 재사용하며 CLI·DB adapter가 같은 공개 계약에 연결되도록 한다.

**Tech Stack:** 기존 Python MCP SDK, stdlib/unittest, Docker Compose CLI, 기존 관리 웹 JavaScript. 신규 의존성 없음.

**Spec:** [설계안](../specs/2026-10-13-chat-host-operations-design.md), [계획 전체](2026-10-13-chat-host-operations.md)

## Global Constraints

전체 계획의 Global Constraints를 적용한다. version 1·2의 기존 7개 도구와 exact-target 검사를 보존한다. 일반 `start_command`와 Git/Yocto 승인을 확장하지 않는다. 실제 호스트 설정·서비스·Docker 작업은 실행하지 않는다.

## Review Focus

- A1: v3 legacy view와 generic scope가 섞이지 않아야 한다.
- A1/A2: 같은 Compose project의 중복 등록과 legacy/new 병렬 실행을 차단해야 한다.
- A2: 승인 후 source/config/runtime/container 변동 및 중복 요청을 쓰기 전에 거절해야 한다.
- A3: 선택하지 않은 서비스·볼륨·secret mount와 이미지 태그 변동을 보호해야 한다.
- A4: LAN HTTP·foreign Origin·missing fresh token은 승인 영수증을 만들 수 없어야 한다.

---

## A1. Registry·정형 action과 legacy 호환

**Files:** Create `chatgpt-mcp/host_registry.py`, `host_actions.py`, `tests/host_operation_fixtures.py`, `tests/test_host_registry.py`, `tests/test_host_actions.py`. Modify `host_runtime_config.py`, `configure-host-runtime.py`.

**Interfaces:**
- `HostTargetRegistry(projects: dict[str, Path], config_path: Path)`와 순수 시험용 `HostTargetRegistry.from_payload(payload: dict, projects: dict[str, Path])`.
- `list_targets(project: str) -> list[dict]`, `require_target(project: str, target: str) -> dict`, `legacy_view() -> dict | None`, `config_digest() -> str`.
- `validate_action(target: dict, action: str, parameters: dict) -> dict`은 정규화된 action·parameters·impact를 반환한다.
- 시험 helper `fixture_v3(root: Path) -> dict`, `FakeHostRunner`는 `.calls`, `.effects`, `.responses`, `.used_project_migration_script`와 `run`, `fail_phase(phase: str)`, `last_command(subcommand: str)`를 제공한다. 각 테스트 파일의 local fixtures가 승인된 입력·원본/사후 상태를 만들며 실제 Docker를 호출하지 않는다.

- [ ] **Step 1: 실패 테스트 작성.** `test_v1_v2_do_not_enable_registered_writes`, `test_v3_legacy_view_is_exact`, `test_missing_or_unsafe_config_is_closed`, `test_duplicate_compose_target_denied`, `test_unknown_fields_and_production_denied`를 만든다. valid v2는 `TARGET_CONFIG_V2`의 정확한 기존 구조를 사용한다.

```python
def test_v2_does_not_enable_registered_writes(self):
    registry = HostTargetRegistry.from_payload(self.valid_v2, self.projects)
    self.assertEqual(registry.list_targets("tossApp"), [])
    self.assertEqual(registry.legacy_view(), self.valid_v2)
```

- [ ] **Step 2: RED 확인.** 아래 명령의 신규 import/signature 실패를 읽는다. 잘못된 테스트 runner로 0건 실행된 것은 RED로 인정하지 않는다.

```bash
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python chatgpt-mcp/tests/test_host_registry.py -v
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python chatgpt-mcp/tests/test_host_actions.py -v
```

- [ ] **Step 3: 최소 구현.** v3의 정확한 top keys는 `version`, `enabled`, `legacy`, `targets`, `agent_profiles`다. `legacy`는 `null` 또는 정확한 기존 v1/v2 payload이며 top-level disabled이면 모든 쓰기를 닫는다. 기존 `load_config`/`extended_enabled`는 이 legacy view만 해석한다. v3는 최대 64 KiB, v1/v2는 기존 8 KiB 상한을 유지한다. agent_profiles는 기본 빈 map이다.
- [ ] **Step 4: 대상과 action 검사 구현.** 대상은 등록된 project, `environment=test`, exact Compose project/path/services, 허용 네트워크·포트·마운트·볼륨 policy, capabilities, `source_review=git_head|reviewed_snapshot`, optional postgres를 가진다. 중복 project/Compose identity를 거절한다. Docker action은 `compose.build|apply|up|restart|stop|down|exec`만 지원하고 exec는 등록 profile ID를 받는다. target/service/flags/env/path의 자유 입력과 volume 삭제·전역 prune은 거절한다. 설정 CLI는 v3 candidate의 읽기 전용 검증·차이 출력만 구현하며 자동 적용하지 않는다.
- [ ] **Step 5: GREEN·기존 회귀 확인.** 위 두 신규 파일과 `test_host_runtime.py`, `test_configure_host_runtime.py`를 실행해 모두 통과한다. 변경 파일·실제 명령·결과를 기록한다. commit은 요청 시 별도 작성 계약으로 수행한다.

## A2. 입력 스냅샷·generic 승인 상태·유실 없는 이벤트

**Files:** Create `host_source_review.py`, `host_process.py`, `host_registered_operations.py`, `tests/test_host_source_review.py`, `tests/test_host_process.py`, `tests/test_host_registered_operations.py`. Existing `host_runtime_operations.py`의 private JSON/lock/digest 유틸리티를 재사용한다.

**Interfaces:**
- `freeze_source(root: Path, destination: Path, *, review_policy: str) -> dict`: source_digest, file manifest, Git HEAD, changed_paths, high_risk_changes, private snapshot 참조.
- `run_registered_command(argv: list[str], *, cwd: Path | None, timeout: int, stdin_data: bytes | None = None, credential_env: dict[str, str] | None = None) -> bytes`: backend 내부용 bounded stdout. shell=False, start_new_session=True.
- `HostCommandError(HostRuntimeError)`는 제한된 `returncode: int | None`, `sqlstate: str | None`을 제공한다. 원문 stdout/stderr를 public error에 넣지 않는다.
- `RegisteredHostOperations(projects, registry, *, state_dir=None, adapter_factory=None)`.
- `prepare(project, target, *, action, parameters, request_id) -> dict`, `get(project, target, operation_id) -> dict`, `async execute(project, target, operation_id) -> dict`, `pending(project) -> dict`.
- `approve_web(project, target, *, operation_id, plan_digest, source_digest, confirmation) -> dict`.
- `events(project, target, operation_id, *, cursor=0, limit=50) -> dict`; `get_for_adapter(operation_id) -> dict`는 검증된 backend 내부 plan만 반환한다.
- adapter 계약은 `preflight(target, spec, context) -> dict`, `execute(target, immutable, context, emit) -> dict`, `postcheck(target, immutable, result) -> dict`다. `emit(phase: str, code: str, fields: dict) -> None`은 허용 메타데이터만 받는다.

- [ ] **Step 1: 실패 테스트 작성.** private ownership/mode, symlink·.env·.git·경로 탈출, dirty/untracked source 포함, reviewed_snapshot와 legacy Git HEAD 분리, nonce/digest/expiry/replay, 요청 ID 충돌, lock 충돌, restart/outcome_unknown, 256 KiB 출력·timeout을 검사한다.

```python
async def test_no_approval_means_no_host_effect(self):
    plan = self.ops.prepare("tossApp", "tarororo-test",
                            action="compose.restart", parameters={"services": ["api"]},
                            request_id="restart-1")
    self.assertEqual(plan["state"], "pending_host_approval")
    with self.assertRaises(HostRuntimeError):
        await self.ops.execute("tossApp", "tarororo-test", plan["operation_id"])
    self.assertEqual(self.runner.effects, [])
```

- [ ] **Step 2: RED 확인.** 신규 세 파일을 각각 위 SDK 인터프리터로 `-v` 실행한다. 실패가 미구현 인터페이스 때문인지 읽는다.
- [ ] **Step 3: snapshot·process 구현.** 기존 `source_inventory`·`git_state`·고위험 검사와 개인 파일 유틸리티를 재사용한다. 실제 입력·mode·symlink를 고정하고 mutable 소스를 호스트 Python/셸로 직접 실행하지 않는다. stdin 전송과 stdout/stderr 수집은 timeout·상한을 적용하며 동시에 처리해 pipe deadlock을 막는다. timeout 시 해당 전용 process group만 종료한다. 임의 환경변수 전달은 허용하지 않는다. `credential_env`는 PostgreSQL backend가 등록된 host credential reference에서 만든 PG 변수만 허용하고 secret 값을 argv에 넣지 않는다. `stdin_data`는 승인한 SQL/명세 전송에만 사용하고 로그에 기록하지 않는다. PostgreSQL stderr에서 검증된 5자리 SQLSTATE만 추출하고 원문은 공개하지 않는다.
- [ ] **Step 4: 상태 manager 구현.** 기존 helpers로 `host-runtime/registered/`의 private 상태와 단조 증가 이벤트 cursor를 저장한다. 만료 600초, ID는 기존 32 hex, request ID는 기존 ASCII 1–80자다. adapter 실행 전에 receipt 소비·소스/설정/실행기/대상 재검증한다. `tarororo-test`는 legacy `target.lock`과 같은 lock을 사용한다. 그 밖의 대상은 검증된 identity별 lock을 사용한다. 중복 실행은 기존 상태 반환이며 실제 효과는 1회다. source checkout의 가짜 approval script만으로 immutable runtime 검사를 우회할 수 없어야 한다.
- [ ] **Step 5: 검증.** source/config/container drift·tampered receipt·출력 초과·프로세스 중단에서 변경 명령 0회 또는 outcome_unknown인지 확인한다. 이벤트는 기본 50/최대 200건·64 KiB이고 원문 stdout/stderr를 저장하지 않는다. 기존 HostRuntime process/approval 회귀도 통과해야 한다.

## A3. Compose 실행·이미지 적용 어댑터

**Files:** Create `host_compose_actions.py`, `tests/test_host_compose_actions.py`. A2 계약과 A1 policy를 사용한다.

**Interfaces:** `ComposeActionAdapter(runner)`는 A2의 `preflight/execute/postcheck`를 구현한다. `inspect_target(target: dict) -> dict`는 상태·health·labels·image ID의 whitelist만 반환한다.

- [ ] **Step 1: 실패 테스트 작성.** build만으로 container 교체하지 않음, apply가 선택 서비스만 변경, unknown 서비스·privileged/root/socket mount 거부, down의 volume 보존, exec profile 제한, immutable image tag 교체·unexpected generation 거부를 확인한다.

```python
def test_apply_preserves_db_worker_and_never_pulls(self):
    result = self.apply_api_fixture()
    self.assertEqual(result["verified_services"], ["api"])
    up = self.runner.last_command("up")
    self.assertTrue({"--no-deps", "--no-build", "--pull", "never"} <= set(up))
    self.assertEqual(self.after["db"], self.before["db"])
    self.assertEqual(self.after["worker"], self.before["worker"])
```

- [ ] **Step 2: RED 확인.** `rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python chatgpt-mcp/tests/test_host_compose_actions.py -v`.
- [ ] **Step 3: 구현.** 등록한 Compose files/project/services를 immutable context에서 render하고 policy를 검증한다. Docker label/container generation과 image ID를 pin한다. build 산출물은 operation 전용 tag, apply는 image-only override와 `--no-deps --no-build --pull never`를 사용한다. restart/stop/up/down/exec는 각 정형 profile의 인자만 사용한다. API 등의 readiness는 등록된 endpoint만 조회하며 임의 URL/redirect를 따르지 않는다.
- [ ] **Step 4: GREEN 확인.** 각 action의 fake runner 호출과 효과를 assertion으로 검사한다. 선택한 서비스 외 컨테이너·볼륨·secret identity가 불변인지 postcheck한다. 실제 Docker 명령은 실행하지 않는다.

## A4. MCP·관리 웹·불변 패키징

**Files:** Create `host_registered_tools.py`, `tests/test_host_registered_router.py`, `tests/test_admin_registered_approval.py`, `tests/test_host_operations_ui.mjs`. Modify `server.py`, `host_runtime_web.py`, `admin_server.py`, `admin.js`, `admin.html`, `admin.css`, `project_state.py`, `install-runtime.py`, `rollout-host-runtime.py`, `rollout_catalog.py`, `README.md`와 관련 기존 테스트.

**Interfaces:** 신규 tool은 설계의 `list_host_targets`, `inspect_host_target`, `prepare_host_operation`, `execute_host_operation`, `read_host_operation_events`다. legacy `get_host_operation`은 ID의 검증된 backend kind로 조회만 분기한다. 서로 다른 저장소에 같은 ID가 발견되면 `AMBIGUOUS_OPERATION_ID`로 거절한다.

- [ ] **Step 1: 실패 테스트 작성.** actual MCP SDK list/call·unknown fields·legacy coexistence, authenticated GET, missing/fresh token·Origin·LAN HTTP approval rejection, approved receipt 1회, HTML textContent/XSS·빈 상태·만료 표시·링크의 token 부재를 검사한다. 각 거부에서 `runner.effects == []`를 assertion한다.
- [ ] **Step 2: RED 확인.** 신규 Python 두 파일과 아래 UI 명령을 실행한다.

```bash
rtk proxy /home/jhw/.nvm/versions/node/v24.12.0/bin/node --test chatgpt-mcp/tests/test_host_operations_ui.mjs
```

- [ ] **Step 3: 통합 구현.** 도구에는 additionalProperties=false와 action별 field 검사, project registry 검증을 적용한다. disabled이면 조회에 제한 사유를 반환하고 prepare/execute는 효과 없이 거절한다. 기존 GET `/api/host-operations`와 POST `/api/host-approve`의 legacy shape를 보존하고 v3 조회에는 target ID를 명시한다. 웹 승인은 동일 fresh-auth/Origin/HTTPS trust 경계에서 manager로 위임한다. 활성화 설정을 승인 없이 바꾸는 endpoint는 추가하지 않는다.
- [ ] **Step 4: UI·telemetry·패키징 구현.** 계획·영향·변경 검토·만료·작업 phase/events와 명시적 승인/거절을 표시한다. 승인 버튼은 실행하지 않는다. 실행기·신규 모듈·자산을 설치 manifest/검증 inventory에 포함하고 신규 도구를 known-tool 목록에 추가한다. public error는 code/phase/diagnostic ID만 제공한다.
- [ ] **Step 5: GREEN·종단 확인.** mock 대상으로 MCP plan → fresh web approve → execute → postcheck → events를 실행해 효과 1회를 확인한다. 기존 `test_host_runtime_router.py`, `test_admin_host_approval.py`, runtime install/rollout 회귀와 Node 문법 검사를 통과한다. 브라우저에서는 fixture admin의 승인 대기·완료·실패·빈 상태를 확인하고 실제 대상은 변경하지 않는다.

## 완료 기준

구현·모의 종단 검증된 A는 일반 Compose 운영 기능을 독립적으로 제공한다. 활성 호스트 서비스는 아직 기존 runtime이며 실제 설치·version 3 활성화는 전체 계획의 별도 승인 단계다. B와 C는 이 문서의 registry/operation 계약을 소비한다.
