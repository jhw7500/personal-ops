# Existing Server Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Task 1 검증 완료. Task 2 runtime 적용 완료, v2 전환은 기존 예약 23개의 종료 증거 부재로 보류. 서비스는 새 runtime으로 복구했다.

**Goal:** 기존 빌드서버의 프로젝트와 정책을 보존하며 설치 단위 HostAccess v2를 사용할 수 있게 한다.

**Architecture:** 먼저 기존 rollout으로 ABI 3 런타임을 floor 2에서 검증한다. 전용 owner-local 명령은 두 MCP 서비스를 중지한 상태에서만 실행하며, 기존 rollout lock과 ActivationMaintenance를 재사용해 authority epoch, ABI floor, 빈 HostAccess v2 상태를 중단 후 재개 가능한 순서로 전환한다. 신규 권한은 발급하지 않는다.

**Tech Stack:** 기존 Python 표준 라이브러리, unittest, systemd user services, immutable runtime installer.

**Spec:** `docs/superpowers/specs/2026-10-24-mcp-trusted-development-design.md`의 설치 단위 저장·생명주기 및 v1/v2 전환 계약. 사용자 지시는 이 빌드서버에 적용하는 작업까지 진행하는 것이다.

## Global Constraints

- 설치 ID, 등록된 12개 프로젝트, policy v1의 기존 필드 제한, 터널 ID·자격증명·네트워크 설정을 보존한다.
- 새 설치용 bootstrap을 기존 설치에 실행하지 않는다. authority/latch의 수동 편집·초기화·백업 복원을 우회 수단으로 쓰지 않는다.
- floor 3 이후 ABI 2 rollback은 금지한다. 같은 검증된 ABI 3 릴리스의 재개/재시작이 복구 경로다.
- 새 grant를 발급하거나 과거 grant/card를 되살리지 않는다. 실제 권한은 웹 승인 카드에서 사용자가 선택한다.
- 기존 main/WIP 및 Codex observer 서비스, Yocto/BitBake·타겟 보드는 변경하지 않는다.
- 새 제품 의존성을 추가하지 않는다. 현재 Python 3.12 설치는 새 코드의 실제 검증을 통과한 경우 유지한다.

## Review Focus

- authority 기록 후 floor 기록 전 crash: 같은 transaction에서 epoch를 다시 올리지 않고 재개한다.
- marker 기록 후 빈 v2 상태 기록 전 crash: 최초 부재를 고정한 journal과 실제 maintenance 소유권이 있어야 복구한다.
- 재개 직전 새 작업·lease·서비스가 살아남음: 전환 및 maintenance 해제를 거부한다.
- source/controller/config/journal/binding 변경: 오래된 준비 결과로 적용하지 않는다.
- floor 전환 이후 실패: ABI 2 또는 과거 grant 복원 없이 ABI 3만 재개한다.

### Task 1: 기존 설치의 stopped-only owner upgrade

**Files:**
- Create: `chatgpt-mcp/workspace_upgrade.py`, `chatgpt-mcp/tests/test_workspace_upgrade.py`
- Modify as needed: `chatgpt-mcp/workspace_authority.py`, `chatgpt-mcp/workspace_control.py`, `chatgpt-mcp/install-runtime.py`, `chatgpt-mcp/README.md`
- Reuse: `workspace_activation_cli`의 inert reader/worker inventory, `workspace_activation_host`의 서비스·터널 관측, 기존 private 파일·manifest 검증.

**Interfaces:** owner CLI `workspace_upgrade.py prepare|status|apply|resume`, 모든 명령은 설치된 immutable release에서 실행한다. prepare/status는 서비스·권한을 변경하지 않는다. apply/resume는 exact journal/target/binding을 검증하고, 서비스 stop/start/wiring은 수행하지 않는다. floor 변경 primitive가 필요하면 authority exclusive lock 내부에 별도 메서드를 추가하며 일반 advance 의미는 바꾸지 않는다.

- [x] 전환·거부·중단 재개 테스트를 먼저 작성하고 실패를 확인한다.
- [x] journal에 target manifest/config/controller pin, 원래/최종 binding, 원래 marker/state 부재 및 전환 intent를 고정한다.
- [x] rollout lock → maintenance → 새로운 drain 관측 → authority state 먼저 저장 → floor 3 저장 → 빈 v2 초기화 → 검증 → maintenance 해제 순서를 구현한다.
- [x] 각 durable 쓰기 직후 crash, 반복 resume, tampered inputs, active workers/leases, old ABI rollback 거부를 테스트한다.
- [x] immutable installer에 명령을 포함하고 기존 ABI 2 필수 파일 목록은 유지한다.
- [x] targeted tests, 실제 런타임 Python으로 전체 isolated regression, UI 검증, 독립 코드 검토를 통과한다.

### Task 2: 빌드서버 적용과 관측

**Files:** private owner rollout receipts/backups and `docs/reviews/2026-10-24-existing-server-upgrade.md`.

**Interfaces:** Task 1의 설치된 owner CLI와 기존 install/controller/advance/rollout 명령을 사용한다.

- [x] 기존 설정과 controller만 private 백업하고 변경 전 digest를 기록한다. grant/authority 복원은 복구 절차에 포함하지 않는다.
- [x] 검증된 새 release를 설치·admit하고 controller를 검증된 버전으로 갱신한다.
- [x] 기존 rollout으로 ABI 3를 floor 2에서 기동하고 터널/admin 및 실제 connector 호출을 검증한다. catalog refresh 알림은 성공했으나 ChatGPT schema cache 갱신은 미확인이다.
- [x] 작업/lease 부재를 다시 확인하고 정확히 MCP tunnel/admin 서비스만 중지한다.
- [ ] owner upgrade를 적용하고 동일 릴리스로 재시작한다. 실패 시 journal에 따라 재개한다.
- [ ] 실제 connector의 catalog, 정책 읽기, 12개 프로젝트 HostAccess v2/no-grant 일치, 기존 credential/config digest 보존을 검증한다.
- [ ] 변경 코드·문서와 검증 결과를 feature branch에 기록한다. main/PR merge는 수행하지 않는다.
- [ ] 최종 응답에 적용 버전, 검증 결과, 웹에서 필요한 최초 승인 및 실제 사용자 채팅 검증 한계를 밝힌다.

전환 보류와 안전한 서비스 복구, 정확한 재개 조건은 `docs/reviews/2026-10-24-existing-server-upgrade.md`에 기록했다.
