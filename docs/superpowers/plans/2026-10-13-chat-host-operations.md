# Chat Host Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일반 터널 채팅에서 등록 대상의 Docker·PostgreSQL 작업과 승인된 코딩 에이전트 위임을 계획·실행·검증할 수 있게 한다.

**Architecture:** 기존 파일·샌드박스·Git 도구를 유지한다. 공통 대상 registry와 일회용 웹 승인 계약 위에 Compose, PostgreSQL, CLI agent 경로를 추가한다. 각 경로는 기본 비활성이며 실제 설치·설정 활성화와 개별 실행 승인은 소스 구현과 분리한다.

**Tech Stack:** Python 3.12.12, 기존 MCP Python SDK·unittest·stdlib, 기존 bubblewrap, Docker Compose/PostgreSQL CLI, 관리 웹 JavaScript, 설치된 Codex CLI. 신규 의존성 없음.

**Spec:** [승인된 설계안](../specs/2026-10-13-chat-host-operations-design.md)

## Global Constraints

- 기존 Docker 컨테이너와 DB 데이터를 변경하지 않는다. Schema 013 적용 승인은 별도 단계로 남긴다.
- 기존 version 1·2 파일과 기존 7개 MCP 도구의 동작은 유지한다.
- 계획·승인은 시작 전까지 10분 동안 유효하다.
- DB 결과는 최대 100행·64 KiB, 이벤트 조회는 기본 50건·최대 200건·64 KiB로 제한한다.
- 명령 출력 수집은 기존 256 KiB 상한을 유지한다.
- 기본 시간 제한은 30분이며, 입력 계약은 1–60분만 허용한다.
- 개인 디렉터리는 0700, 개인 파일은 0600이며 소유자와 심볼릭 링크를 검사한다.
- 프롬프트·추론·원시 로그·자격 증명을 공개 이벤트나 추적 문서에 저장하지 않는다.
- `POLICY_DENIED`, `UNSUPPORTED_SCOPE`, 승인 누락, 비밀 접근 차단, `STALE_PLAN`은 위임으로 우회하지 않는다.
- Yocto/BitBake 실행과 캐시·실행 프로세스 조작은 별도 사용자 승인 대상이다.
- 코드·문서 작업은 기존 미커밋 변경을 보존한다. Task 선택은 이미 완료된 Task 없음 상태를 유지하며 Issue·Task·commit·push·PR를 자동 생성하지 않는다.
- 모든 셸 검증 명령은 `rtk`로 시작한다. 호스트 설정 변경·서비스 재시작·실제 모델 호출은 별도 승인 없이 실행하지 않는다.

## Review Focus

1. version 3으로 전환하면서 기존 API/Worker/013 권한이 암묵적으로 확대되거나 비활성화되는 경우: 정확한 legacy 설정의 호환 view만 사용한다. A1.
2. 같은 Docker 스택을 여러 target ID 또는 legacy/new 경로로 동시에 실행하는 경우: 중복 등록 거부와 공통 대상 lock으로 직렬화한다. A1·A2.
3. PostgreSQL 인용·중첩 주석·dollar-quoted 함수 본문에 숨은 client/transaction 명령: 지원하지 못하는 SQL은 쓰기 전에 거절한다. B1.
4. Codex의 cwd 또는 workspace-write만으로 호스트 읽기가 차단된 것으로 착각하는 경우: 실제 설치 버전에서 격리 시험이 실패하면 호출을 비활성으로 유지한다. C2.
5. 실행 중 다른 세션이 파일을 바꾸거나 CLI 실행이 끊긴 경우: 원본 덮어쓰기·중복 실행·성공 추정을 하지 않는다. A2·C3.

---

## 계획 구성과 의존성

```text
A: Registry + approval + Compose + MCP/UI
    |
    +--> B: PostgreSQL artifacts + backup + migration/restore
    |
    +--> C: Approved Codex task + isolation + result/broker
                    |
              Final review + candidate
                    |
              Host rollout approval
```

| Part | Plan | Independently testable result |
| --- | --- | --- |
| A | [Registered host operations](2026-10-13-registered-host-operations.md) | Registered Compose plans, approvals, execution and events |
| B | [PostgreSQL host operations](2026-10-13-postgres-host-operations.md) | Approved SQL/migration/backup/restore on registered test DB |
| C | [Approved agent delegation](2026-10-13-approved-agent-delegation.md) | Approved isolated Codex tasks and safe progress/results |

A를 먼저 구현한다. B와 C는 A의 공개 계약을 사용하지만 서로의 완료를 요구하지 않는다. 기본 실행 제안은 A → B → C를 이 세션에서 직접 구현하고, 마지막에 독립 코드 리뷰를 받는 방식이다. 단계별 변경·테스트 증거는 이행 기록에 남긴다.

## 변경 파일과 책임

| Responsibility | Main files |
| --- | --- |
| Registry and action schema | `host_registry.py`, `host_actions.py`, `host_runtime_config.py` |
| Frozen inputs and bounded process execution | `host_source_review.py`, `host_process.py` |
| Plan/receipt/state/events | `host_registered_operations.py`, existing `host_runtime_operations.py` helpers |
| Compose adapter | `host_compose_actions.py` |
| PostgreSQL adapters | `host_postgres_policy.py`, `host_postgres_backup.py`, `host_postgres_actions.py` |
| Agent profiles/tasks/runner | `agent_profiles.py`, `agent_tasks.py`, `agent_runner.py`, `agent_events.py` |
| Agent host bridge | `agent_host_broker.py` |
| MCP and web integration | `host_registered_tools.py`, `agent_tools.py`, `server.py`, `host_runtime_web.py`, `admin_server.py`, `admin.js`, `admin.html`, `admin.css` |
| Installed integrity and telemetry | `install-runtime.py`, `rollout-host-runtime.py`, `rollout_catalog.py`, `project_state.py` |

표의 파일은 모두 `chatgpt-mcp/` 아래다. 새 모듈은 각 하위 계획에 명시한 책임만 가진다. 기존 대형 파일 전체를 재구성하지 않는다.

## 실행 시작 시 준비

- [ ] 선택된 실행 방식의 스킬과 `superpowers:using-git-worktrees`를 읽는다. 현재 원본 `main`과 다른 작업 세션을 변경하지 않는 새 작업트리를 만든다.
- [ ] 현재 `chatgpt-mcp/`, 관련 테스트·대시보드 자산·이 설계/계획의 허용 파일만 소유자·regular-file·symlink·내용 해시를 검증해 baseline에 포함한다. 미추적 HostRuntime 코드와 사용자의 미커밋 수정도 보존한다. Git HEAD만으로 최신 구현을 복원하지 않는다.
- [ ] 원본과 작업트리의 baseline 파일 해시를 개인 상태 디렉터리에 저장한다. 후속 원본 반영은 파일별 baseline 비교 후 수행하고, 충돌 시 기존 파일을 덮어쓰지 않는다.
- [ ] 작업트리에서 SDK를 갖춘 인터프리터로 기존 전체 unittest를 한 번 실행한다. 실패하면 새 기능과 별개인지 구분한 후 해결하거나 정확한 검증 제약을 기록한다.

검증 명령의 인터프리터는 `/home/jhw/.local/share/uv/tools/serena-agent/bin/python`이다. Node 문법/단위 검사는 `/home/jhw/.nvm/versions/node/v24.12.0/bin/node`를 사용한다. 2026-10-13 읽기 전용 확인에서 두 파일과 MCP SDK, `/home/jhw/.local/bin/ruff`가 존재했다. 구현 시작 시 다시 확인한다.

## 최종 검증과 실제 적용 경계

- [ ] 각 하위 계획의 대상 테스트를 통과한 뒤 다음 명령을 실행한다. Python 프로젝트의 타입 검증은 기존 lint·실제 SDK 라우팅/직렬화 시험으로 확인하며 새 타입 검사기를 설치하지 않는다.

```bash
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python -m unittest discover -s chatgpt-mcp/tests -p 'test_*.py'
rtk proxy /home/jhw/.local/bin/ruff check chatgpt-mcp
rtk proxy /home/jhw/.nvm/versions/node/v24.12.0/bin/node --check chatgpt-mcp/admin.js
rtk proxy /home/jhw/.nvm/versions/node/v24.12.0/bin/node --test chatgpt-mcp/tests/test_host_operations_ui.mjs chatgpt-mcp/tests/test_agent_tasks_ui.mjs
rtk proxy git diff --check
```

- [ ] 신선한 독립 `code-reviewer`에게 변경 범위·설계·실제 테스트 증거를 제공한다. 권한 우회·자료 유출·중복 실행·데이터 손실 지적은 해소한 뒤 재검증한다. 리뷰가 실제로 실행된 경우에만 결과를 보고한다.
- [ ] 원본 반영 가능한 파일별 차이, candidate manifest, 신규 도구 목록, settings 차이와 rollback 계획을 준비한다. 기존 활성 런타임·프로세스·컨테이너 세대는 읽기 전용으로 관찰한다.
- [ ] 호스트 runtime rollout·version 3 설정 활성화·실제 모델 smoke는 필요한 구체적 승인을 받은 뒤에만 실행한다. 승인 전 candidate를 정상 적용된 런타임으로 보고하지 않는다.
- [ ] 활성화 후 actual process/runtime manifest, connector 도구 metadata와 실제 읽기 전용 호출, 관리 웹 asset·API·승인 거부 경계를 확인한다. 실제 쓰기 작업은 여전히 개별 웹 승인, 013은 별도 승인이다.

## 문서 상태

자체 검토에서 사용한 설계 요구와 구현 task의 대응은 다음과 같다.

| Spec sections | Implementation tasks |
| --- | --- |
| 1-3: Goals and approach | A4, C1, plan index |
| 4: Registry and compatibility | A1 |
| 5: Tools and operation contract | A2, A4 |
| 6: Docker | A3 |
| 7: PostgreSQL and protected 013 | B1, B2, B3 |
| 8: Source review | A2, C3 |
| 9: Web approval | A4, C1 |
| 10: Recovery and observation | A2, C2, C3 |
| 11-12: Activation and verification | A4, B3, C3, plan index |
| 13: Agent delegation | C1, C2, C3 |
| 14: Evidence and handoff | All plans and final report |

설계는 사용자 확인을 받았고 이 구현 계획은 검토 대기다. 문서 작성·자체 검토는 완료 여부를 검사하지만 소스 구현·배포·DB 적용의 증거는 아니다. 실제 구현 전에 이 계획과 실행 방식을 사용자에게 확인받는다.
