# personal-ops 런타임·대시보드 종료 점검

2026-10-20 사용자의 남은 작업 확인·정리 요청에 따라 코드, 인계 문서, 실제 서비스·플러그인·관리 웹을 대조했다. 이번 통합 구현·배포의 필수 잔여 작업은 없다. 아래 사용 조건과 선택적 후속 검증은 완료로 바꾸지 않는다.

## 완료 범위와 현재 운영

- 운영 런타임은 `788381864215c1494db0`다. 실제 MCP PID1631490의 시작 시각·실행 경로가 전환 영수증과 같다. 터널은 oneshot 특성상 active/exited, 관리 웹 PID1631660과 관측기 PID3208094는 active/running이다. 세 unit의 NRestarts는 0이다.
- 소스 커밋은 `a27f87d`, 전환 기록은 `f3079d9`, 신규 플러그인 호출 확인은 `840ca05`다. 통합 코드 25파일이 동결본과 같다. native terminal·Claude 하위 에이전트·스킬 표시는 기존 배포에 포함돼 있다.
- 현재 대화에서 신규 런타임 승인 카드 도구와 UX019 조회 도구를 확인했다. UX019 실제 플러그인 조회는 provider_verified=true, found=true, 버전 20261019-27이다.
- 실제 API는 healthy/readiness ready, Worker와 PostgreSQL은 running, Schema013은 ready, 모드는 toss-demo다. API·Worker 이미지 ID는 기존 운영과 같다.
- 실제 LAN 관리 웹에서 wlan-package·personal-ops·pim-check·tossApp 선택과 live 상태를 확인했다. wlan-package의 1440/390/320 화면 폭에 가로 넘침·브라우저 오류가 없고 LAN/loopback의 JS·CSS 3종은 설치본 해시와 같다.
- 이번 실제 완료 전 검증을 메타데이터 1건으로 report_work_events에 보고했다. skill_id/skill_name을 포함한 실제 MCP 호출이 accepted=1 / provenance=reported로 성공했다. 실제 관리 웹에서 해당 항목을 클릭해 스킬 식별자와 ‘보고됨’ 표시를 확인했다. 과거 connector의 스킬 필드 거부 문제는 현재 연결에서 해소됐다. 스킬 인자·프롬프트·응답·추론 본문·추정 토큰은 보고하지 않았다.
- 진행 요약은 revision21의 2026-10-17 OAuth 대기 기록에 머물러 있었다. 이전 내용을 사용자 전용 증거 폴더에 보존하고 CAS를 사용해 revision22 / completed로 갱신했다. 외부 QA까지 완료한 상태로 표시하지 않았다.

2026-10-19 공식 preflight의 관련 884개·전체 1100개 및 UI 검사 5종의 종료 코드 0은 이전 전환의 근거다. 이번에는 코드 변경 없이 실제 운영·브라우저·MCP 왕복을 새로 검사했으며, 전체 테스트를 다시 실행했다고 주장하지 않는다.

## 사용 시 남는 조건

1. 호스트 권한은 active=false / EXPIRED이며 대기 요청도 없다. 실제 웹 호스트 실행을 재개할 때 카드에서 새 권한을 승인한다. 최대 8시간이며 Codex 계정 사용은 별도 옵션이다. 확인만을 위해 승인 카드나 전환 계획을 반복 생성하지 않았다.
2. 신규 런타임 승인 카드의 실제 ChatGPT 화면 표시·클릭·승인 영수증 확인은 남아 있다. 설치·검사와 실제 ChatGPT 화면 확인을 구분하며 다음 실제 영향 작업의 계획으로 확인한다.
3. tossApp의 `codex-tossapp-review` 격리 프로필은 ready=false / ISOLATION_UNVERIFIED다. 기존 isolation/smoke 영수증은 존재하지만 현재 binding의 platform과 source_contract_digest가 다르다. 격리 모드를 쓰려면 현재 조건으로 실제 격리 검증과 별도 승인된 모델 smoke가 필요하다. 영수증을 수정하거나 권한을 우회하지 않았다. 호스트 모드는 별도의 Codex 허용 호스트 권한을 요구하며 이번에는 발급·실행하지 않았다. personal-ops에 등록된 프로젝트 전용 격리 프로필은 없다.

## 별도 프로젝트와 선택적 후속 작업

- UX019의 test_bundle_ready/is_tested/deployed/remote_digest_verified는 모두 false다. TEST 준비·실기기 QA·원격 해시 확인은 tossApp의 제품 검증 범위다. 이번에 재업로드하거나 상태를 변경하지 않았다.
- 터널 health의 전체 tools_list 관측은 이전에 awaiting_discovery로 남았다. 실제 신규 도구 호출 성공과 관측 한계를 구분하며 이전 전환 영수증을 덮어쓰지 않는다. 전체 passive catalog 관측 개선은 별도 후속 항목이다.
- lifecycle 상세의 ‘시작 기록’을 ‘마지막 관측’으로 다듬는 문구, 추가 외형·장비·자유 AI 대화는 선택적 확장이다. 이번 종료 점검에서 새 기능·재배포 범위를 만들지 않았다.

## 보존과 인계

다른 세션의 미추적 파일 11개와 tmp/는 그대로 둔다. 어제와 달라진 Claude 설정, 호스트 명령 프로필 및 권한 기록은 현재 상태를 별도 기준으로 보존한다. Claude subagentStatusLine과 Stop/SubagentStop 수집기 등록 및 고정 helper 해시는 정상이다. 옛 설정으로 되돌리지 않는다.

이번 작업에서 서비스 재시작·호스트 설정 변경·격리 proof 발급·모델 호출·Docker/DB/스키마·AIT·BitBake 작업을 실행하지 않았다. 실제 검증 메타데이터와 프로젝트 진행 요약만 갱신한다. 일반 호스트 권한 만료는 로컬에서 승인된 이 문서 정리 작업을 막는 조건으로 사용하지 않는다.

증거: `/home/jhw/.local/state/personal-ops-closeout/20261020-47rnoerz/` (디렉터리0700, 파일0600).

- actual-mcp-results.json: 실제 플러그인 조회 및 이전 진행 요약.
- live-ui.json, live-wlan-*.png: 실제 운영 화면·응답 파일 검증.
- skill-display.json, skill-inspector.png: 실제 검증 작업의 스킬 필드 MCP 왕복·상세 화면.
- agent-proof-status.json: 기존 격리 영수증의 현재 binding 불일치.
- current-config-baseline.json, claude-observation-config.json: 현재 설정 보존 기준과 수집기 등록.

최신 통합 전환 상세는 [2026-10-19-host-integration-runtime-card-ux019.md](2026-10-19-host-integration-runtime-card-ux019.md)를 참조한다. 이 문서를 다음 세션의 현재 종료점으로 사용한다.
