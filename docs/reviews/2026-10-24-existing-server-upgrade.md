# 기존 빌드서버 MCP 전환 검증

상태: 새 ABI 3 runtime 적용 및 서비스 복구 완료. HostAccess v2 전환은 기존 작업 예약 23개의 종료 증거 부재로 보류. 전체 완료로 해석하지 않는다.

## 범위와 보존 조건

사용자의 빌드서버 적용 지시에 따라 기존 설치를 설치 단위 HostAccess v2로 전환한다.
프로젝트·Workspace 정책·터널 연결 정보는 보존한다. 실제 장기 권한은 웹 승인 카드에서
사용자가 선택하며, 이 작업에서 신규 grant를 자동으로 발급하지 않는다.

새 설치용 bootstrap, main/WIP 변경, PIM 테스트·보드 조작, Yocto/BitBake 작업은 범위에 없다.

## 적용 전 관측

2026-10-24 읽기 전용 확인 결과:

- 기존 release: `03ff9fa73dcaa0aa5416574e82b77566693fe985852771ade5cc5875b31ae580`.
- Workspace epoch 16, 최소 ABI 2, policy v1.
- 등록 프로젝트 12개 모두 존재. 기본 경로 `/home/jhw/ai/opencode/projects`.
- 기존 artifact 규칙 2개와 protected path 3개. 필드 허용 범위를 완화하지 않는다.
- Workspace request/grant 0개. 6종류 worker와 locked lease 모두 0개.
- 실제 tunnel `healthy=true`, `process_running=true`, `ready=true`.
- Python 3.12.12, serena-agent 0.1.4, MCP 1.23.0.
- 실제 연결의 `get_host_access`: personal-ops `EXPIRED`, pim-check `NO_GRANT`.

## 백업과 복구 경계

private 백업은 기존 프로젝트/정책/터널 프로파일/두 서비스 설정/실행기만 포함한다.
파일마다 owner·regular type·0600을 검증했다. runtime-key와 admin-token은 비교용 digest만
기록했으며 복사하지 않았다. authority/grant/latch는 복구용 백업 대상에서 제외했다.

floor 3 전환 이후 ABI 2 런타임을 복구용으로 실행하지 않는다. 새 ABI 3 릴리스의 같은
전환 journal을 재개하고 정상 서비스 시작 절차를 사용한다. 과거 grant나 authority 파일을
복원하는 절차는 제공하지 않는다.

## 구현·검증 기록

- 계획: `docs/superpowers/plans/2026-10-24-existing-server-upgrade.md`.
- 새 runtime 설치·서비스 전환 완료. 최소 ABI는 2를 유지한다.
- owner upgrade 중단/재개 테스트 및 독립 코드 검토 통과.
- 전체 회귀 및 실제 connector 기본 호출 통과. ChatGPT 스키마 캐시 갱신은 미확인.
- 웹에서 최초 권한 승인 후 연속 일반 개발 작업: 사용자의 실제 승인 후 검증할 항목.

### 기존 설치 전환 구현

`workspace_upgrade.py`는 설치된 runtime에서만 실행하는 owner 명령이다. 새 ABI 3
runtime이 실제로 실행 중일 때 준비하고, tunnel/admin 중지와 worker/lease 배출을
확인한 다음 기존 authority·최소 ABI·빈 HostAccess v2 상태를 순서대로 전환한다.
중단 후 같은 transaction을 재개하며, 완료 표시는 실제 최종 상태 검증 후 기록한다.

실제 서버는 tunnel, MCP, Node Codex wrapper, native Codex, admin의 5개 프로세스를
사용한다. 읽기 전용 준비 검사에서 5개를 모두 확인했다. PID가 바뀌어 남은 Codex도
최종 독립 검사로 검출한다. Codex가 없는 MCP 구성도 지원한다.

- 새 전환 테스트 35개, 관련 authority/HostAccess/activation 테스트 합계 166개 통과.
- crash 지점별 재개, 오래된 journal 거부, floor 변경 출처, 서비스·worker·lease·잔존
  프로세스 거부, 실제 daemon 및 npm wrapper 구조를 검증했다.
- 독립 검토에서 발견한 실행 상태·역할 매칭·복구 증거·최종 journal 기록·새 PID 배출
  문제를 보완했다. 최종 검토 결과는 운영 적용 항목에 기록한다.

### 기존 Python에서의 검사 보완

기존 Python 3.12 사전 검사에서 targeted 1,274개는 통과했지만 전체 1,509개 중
bootstrap 테스트 13개가 의도된 Python 3.11 전제조건에서 중단되었다.
파일·설정 테스트의 모듈 내부 전제조건만 분리하고 실제 실행 파일은 유지했다.
지원하지 않는 3.10/3.12/3.13에서 owner 파일 쓰기 전에 거부하는 회귀 검사도 추가했다.
보완 후 bootstrap 16개가 통과했다. 제품의 Python 3.11 bootstrap 제한은 유지한다.
이는 Windows/WSL 새 설치의 실제 실행 결과를 의미하지 않는다.

## 최종 검증과 적용 결과

실제 운영 Python 3.12.12로 공식 `rollout-host-runtime.py --apply` 사전 검사를 실행했다.

- targeted 1,274개, full 1,545개: exit 0. 공식 Yocto fixture 제외 5개를 유지했다.
- 승인 UI·브라우저·JavaScript 7개 단계: 모두 exit 0.
- 독립 코드 검토: 최초 지적 및 수정 중 발견한 문제 모두 해결, APPROVE.
- 적용 snapshot: `6b5ac261d15caa480a61` (113 files).
- 전체 release digest: `6b5ac261d15caa480a612a033404973f14d99e9663eaf266a3e029416474f54d`.
- controller를 지원되는 설치 명령으로 갱신했고, 기존 generated Python cache는 private 백업으로 이동했다.
- rollout 결과: 새 runtime·tunnel 정상, `admin_ready`, 관측한 기존 CLI 세션 9개 보존.
- actual connector: 등록 프로젝트 12개, bounded README 읽기, HostAccess 조회 성공.
- `refresh_mcp_catalog`: `notification_sent=true`; `catalog_refetched=false`,
  `chatgpt_metadata_verified=false`. 서버가 ChatGPT 캐시 갱신을 확인한 것은 아니다.
- installation ID·policy digest·project mapping·두 원본 service unit·runtime key·admin token
  보존. 터널 프로파일은 MCP 실행 명령 이외의 값이 동일하다. Codex observer PID도 동일하다.

private rollout receipt:
`/home/jhw/.local/state/chatgpt-coding-mcp/rollouts/rollout-tv1lt65f/receipt.json`.
보존 비교와 전환 기록:
`/home/jhw/.local/state/chatgpt-coding-mcp/rollouts/existing-upgrade-20261024T094251Z/`.
이 경로의 자격증명은 출력하거나 Git에 포함하지 않았다.

## 전환을 막은 기존 예약과 서비스 복구

설치된 owner CLI의 `prepare`는 새 ABI 3 runtime에 대해 성공했다. tunnel/admin을
중지한 후 `apply`가 `WORKSPACE_HOST_LANE_BUSY`로 거부했다. 이 시점은 authority·floor·
HostAccess 상태를 변경하기 전이며, maintenance intent만 생성된 상태였다.

`workspace-authority/.host-active`에 과거 예약 23개가 있다: registered 13개,
host-runtime 8개, host-command 2개. 생성 시각은 2026-10-24 06:00:12–47 UTC로
이번 적용보다 앞선다. 대응하는 canonical 작업 기록은 없고, 현재 worker·locked lease·
해당 transient unit도 없다. 그러나 현재 boot는 예약보다 이전이므로 이것만으로 각 작업의
종료를 입증할 수 없다. 제한된 임시 산출물·당일 세션 로그 검색에서도 해당 ID의 실행 또는
종료 증거를 찾지 못했다. 테스트 잔여물이라는 추정만으로 삭제하지 않았다.

지원되는 maintenance API로 복구했다. rollout/journal 잠금을 보유한 상태에서 정확한
원래 authority fingerprint, floor 2, 초기 HostAccess 상태와 설정이 모두 동일함을 재확인한
후 `ActivationMaintenance.finish(original_binding)`를 실행하고 같은 ABI 3 runtime의
두 서비스만 다시 시작했다. authority/grant 파일 복원이나 수동 초기화는 하지 않았다.

현재 확인 결과:

- tunnel active/exited, admin active/running, observer active/running.
- Workspace epoch 19, 최소 ABI 2. HostAccess v2는 활성화되지 않았다.
- `pim-check` 실제 HostAccess 조회는 `NO_GRANT`다. 새 grant는 발급하지 않았다.
- 서비스 복구 후 실제 connector README 읽기 및 refresh 알림 호출 성공.
- 예전 준비 transaction은 원래 프로세스를 가리키므로 그대로 재사용하지 않는다.

## 남은 단계와 재개 조건

예약별 종료 증거를 복구하거나, 예약 시점보다 이후의 재부팅 경계를 확인해야 한다.
확인 전에는 예약을 직접 지우거나 `termination_verified=True`를 임의로 선언하지 않는다.
서버 재부팅은 다른 작업·CLI 세션에 영향을 주므로 이 작업에서는 실행하지 않았다.
조회 시 BitBake 프로세스는 없었지만, 재부팅 실행 직전에 다시 확인해야 하며 별도 사용자
승인이 필요하다.

종료 증거 확보 후에는 private `operational-evidence.json`에 고정한 정확한 ID·kind와
파일 정체성을 대조하고, `complete_host_operation`으로 확인된 예약만 종료 처리한다.
그다음 설치된 owner CLI로 새 transaction을 준비하고, 두 서비스 중지 → apply → 재시작 →
실제 connector 검증을 수행한다. floor 3 및 빈 HostAccess v2 상태까지 확인한 뒤에만
전환 완료로 기록한다. 실제 장기 권한은 이후 웹에서 사용자가 최초 승인한다.

## 작업 판단과 한계

- 기존 진행 지시를 이 서버 적용까지의 승인으로 해석했다. 해석이 다르면 서버 적용 범위를
  다시 조정해야 한다. 무기한 실행 권한 발급의 승인으로 확대하지 않았다.
- 하나의 전환 구현은 독립 작업자에게 맡기고, 운영 명령과 검증은 root가 맡았다.
  경계가 잘못되면 검증 범위 재점검이 필요하다.
- 기존 feature worktree와 Task 없음 결정을 유지했다. 별도 main 변경·Task 등록은 하지 않았다.
  작업 소유 범위가 달랐다면 추적 자료를 다시 정리해야 한다.
- source를 동결한 diff로 검토하고, 실제 검증 후 커밋하는 순서를 택했다.
  동결 이후 source가 바뀌었다면 검토와 검증을 다시 해야 한다.
