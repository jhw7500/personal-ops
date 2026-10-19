# 업로드 승인 카드 재검증과 CLI 롤아웃 관측 보강

날짜: 2026-10-19 KST. 프로젝트: personal-ops.

## 범위와 적용 상태

사용자의 “진행”에 따라 업로드 승인 카드의 현재 연결을 점검하고, 다음 승인된 서비스
전환에서 독립 Codex/Claude CLI의 관측 증거를 남기도록 보완했다. 기존 세션·공유 WIP를
보존하며 소스 수정과 검증만 수행한다. 실제 서비스 전환은 앞선 별도 승인 조건을 따른다.
실제 AIT 업로드·Docker·DB·스키마 변경이나 다른 CLI 종료·재실행은 수행하지 않는다.

- 원본: `/home/jhw/ai/opencode/projects/personal-ops`.
- 이번 격리 작업공간: `/home/jhw/.codex/worktrees/90ee/personal-ops`.
- 기준 HEAD: `cf8b6d2608c9cadaa939fdca8045891e848ec7ba`.
- 증거: `~/.local/state/personal-ops-rollouts/cli-observability-20261019-it8ccjcz/`.
- 소스 검증·원본 반영·운영 반영 결과는 아래 최종 결과 절에서 구분한다.

## 현재 연결과 업로드 카드

- 실제 ChatGPT 연결에서 `list_host_command_profiles(tossApp)` 성공. 등록 프로필은
  `tossapp-deploy-test`, `tossapp-upload-test`, `tossapp-upload-ux014`의 3개다.
- 현재 연결의 도구 정의에 `prepare_host_command`, `open_host_command_approval`가 있다.
  전체 ChatGPT 세션의 캐시나 모든 클라이언트의 UI까지 검증한 것은 아니다.
- `prepare_host_command(tossApp, tossapp-upload-ux014)`로 표시 검증용 새 계획만 준비했다.
  operation ID `020431bd36b0a2831ce2414c08ed8c7e`, 생성 09:23:16 KST, 만료 09:33:16 KST.
  준비 응답은 `state=pending`, `approval_required=true`, `executes_on_approval=false`이고
  관리자 페이지 대신 인라인 카드의 승인·취소 버튼을 안내한다.
- 실제 승인·`execute_host_command`는 호출하지 않았다. 이 계획은 표시 검증용이다.
  사용자 응답은 **지금 이 Codex 작업 대화에서 아무것도 표시되지 않음**이다.
  일반 ChatGPT 플러그인 채팅에서의 실제 표시는 미검증이다. 서버의 카드 데이터 반환이나
  이 Codex 화면의 결과를 일반 ChatGPT의 표시 성공/실패로 간주하지 않는다.
  확인 없이 표시 완료로 보고하지 않으며 만료된 계획을 재사용하지 않는다.
- 운영 snapshot `8c7a5aff0c75198c6c5d`의 실제 HTML로 Chromium 검사 4개 통과:
  320/390/1440px 자동 높이, 승인 시 실행 없음, 응답 유실·불일치 시 잠금, 승인/취소 경쟁.
  설치 HTML SHA-256이 해당 manifest와 일치한다. 이 검사는 브리지 fixture이며 실제
  ChatGPT 화면의 표시 증거를 대신하지 않는다.

## 원인과 소스 변경

공식 `rollout()`에는 독립 CLI 기준선/사후 관측이 없었다. 기존 영수증은 터널·MCP·터널
직속 Codex app-server의 PID와 start tick만 기록한다. 10월 16일의 독립 CLI 보존 검사는
별도 실행 스크립트의 증거였으므로 공식 CLI·웹 롤아웃 양쪽에 공통 관측을 넣는다.

- `rollout_diagnostics.py`: 동일 UID의 인식 가능한 대화형 Codex/Claude 프로세스를
  읽기 전용으로 수집한다. PID/start tick, PPID/PGID/SID, 숫자 TTY, 고정 상태 분류와
  cgroup SHA-256만 기록한다. 원시 cmdline·환경변수·comm·cgroup 경로는 저장하지 않는다.
- 전환 직전, 터널 전환 직후, 전체 전환의 `finally`에서 동일한 기준선 정체성을 비교한다.
  각 영수증 이벤트에 UTC 시각과 단조 시계 경과 시간을 자동 기록한다.
- PID 재사용, 소멸, 좀비/종료, 관측 불가를 구분한다. 종료 코드는 실제 `/proc` 좀비에서
  관측한 경우에만 기록한다. 이미 사라진 비자식의 종료 코드나 원인은 만들어 내지 않는다.
  읽기 도중의 PID 재사용도 별도 타입으로 식별하고 관측한 새 start tick을 보존한다.
  새 정체성과 이전 cgroup을 섞지 않으며, PID 폴더가 남아 있는 파일 누락은 종료로 간주하지 않는다.
- 최대 CLI 64개, proc 항목 8192개, 개별 파일 8192바이트로 제한한다. 상한·권한·메타데이터
  누락 시 관측 불완전으로 표시하고 전체 `ready` 대신 `verification_required`를 반환한다.
- 관측은 프로세스에 signal/wait/ptrace를 수행하지 않는다. 기존 승인·전환·rollback 범위는
  유지하며, 관측 영수증 저장 실패가 기존 전환 예외를 덮지 않도록 보존한다.
- 관측 불완전은 `CLI_OBSERVATION_INCOMPLETE`, 확인된 정체성 변화/종료/소멸은
  `CLI_SESSIONS_CHANGED`로 반환한다. 기존 웹 worker의 안전한 code 필드로도 전달된다.
  관측 불가는 확인된 변경 개수에 합산하지 않는다.
- 기존 0700/0600 비공개 디렉터리·파일과 원자적 영수증 저장을 유지한다.

## 검증과 한계

- 기준 관련 검사 52개 통과. 새 전환 경계 기록 회귀 2개가 수정 전 실패함을 확인했다.
- 관측 불가인데 `ready`를 반환하는 추가 회귀도 수정 전 실패함을 확인하고 보완했다.
- 최초 전체 검사 946개는 실패 없이 완료했고 1개는 명시적 옵트인 실제 Codex probe로 skip.
  이후 경계 사례 보완 후 관련 검사 88개 통과, 공식 선별 검사 787개 통과.
  최신 전체 검사 및 최종 적용 결과는 아래에 기록한다. 중복되는 검사 수를 합산하지 않는다.
- 독립 코드 재검토 APPROVE, 미해결 이슈 0개. PID 재사용 중간 race와 살아있는 PID의
  메타데이터 누락 두 지적을 회귀 테스트로 재현한 뒤 수정했다. 공식 SDK Python의
  두 모듈 검사 47개 및 AST 4개, diff 검사도 통과했다.
- 승인 카드 Node 계약 검사 9개 통과. 운영 HTML의 Chromium 검사 4개는 위와 같다.
- 실제 호스트의 읽기 전용 관측: CLI 10개, 이벤트 33개, 관측 완료, 정체성 변화 0개.
  영수증 `live-readonly/rollout-w1uul_4x/receipt.json`. 실제 전환을 실시한 검증은 아니다.
- 10월 16일 Claude 종료 원인은 계속 미확정이다. 이번 변경은 앞으로의 증거를 보강한다.
  경계별 표본만으로 중간의 일시 정지·신호·종료 원인을 확정할 수 없다.
- 인식 가능한 실행 파일 이름과 대화형 호출만 대상으로 삼는다. `codex app-server`,
  `codex exec`, `claude -p/--print` 및 다른 UID는 독립 대화형 세션에 포함하지 않는다.
- 관측 결과는 진단 자료이며 프로세스 소유권이나 실행 권한을 부여하지 않는다.
- 이벤트 `observed_at`은 이벤트 기록 시각이다. 프로세스 목록을 하나의 원자적 시점에
  읽는 것은 아니다. 서비스별 기준 cgroup hash와의 대조·신호 감사는 이번에 추가하지 않았다.
- 검토자가 OS Python 3.10.12에서 기존
  `test_failed_unittest_names_survive_without_failure_messages`의 6개 stage subtest 실패를
  재현했다. 원본 기준선에서도 동일하며 이번 변경 회귀가 아니다. 공식 rollout 지정
  Serena SDK Python에서 해당 검사를 포함해 통과했다. OS interpreter 호환성은 수정하지 않았다.

## 최종 결과

- 공식 사전검사 `preflight_ok`, exit 0. 영수증
  `official-preflight-receipts/rollout-8iqttpf8/receipt.json`.
  UI 12/4/4/7개, 선별 787개, 전체 953개 모두 exit 0. 실제 Codex 격리 probe는 옵트인 검사로
  이번에 별도로 실행하지 않았다. Ruff·AST·diff 검사 및 독립 재검토 APPROVE.
- 원본에 코드/테스트 4개와 이 인계만 반영했다. 최초 기준선 358개 중 그 4개 외의 바이트·모드,
  HEAD·index는 그대로 보존한다. 충돌 파일을 덮어쓰지 않는 해시 비교 후 복사한다.
  최종 확인은 증거 디렉터리의 `source-integration.json`을 참조한다.
- 운영 MCP·관리 웹 snapshot은 계속 `8c7a5aff0c75198c6c5d`다. 사전검사 결과도
  installed=false, switched=false, profile_aligned=true, service_aligned=true다.
- 이번 보완은 installer의 runtime manifest가 아닌 **CONTROL 파일**이다. 현재 설치된 웹
  롤아웃 관리기도 이 두 파일을 계획에 동결하고 worker가 동결 경로에서 읽는 것을 확인했다.
  따라서 원본의 제어 스크립트를 사용하거나 다음 웹 롤아웃을 새로 준비하면 보완이 포함된다.
  이미 만들어진 계획·실행에는 소급 적용되지 않는다. 보완 자체를 위해 서비스를 전환하지 않는다.
- 사전검사의 runtime 후보 `c1534ffc6fd144799014`(87개)은 이번 진단 파일 수정과 별개의
  기존 Toss 콘솔 WIP를 포함한다. 기존 운영 대비 Toss 콘솔 모듈 3개 추가 및
  `server.py`·`project_state.py`·`host_runtime_operations.py` 변경이며 이번에 배포하지 않았다.
  runtime hash만으로 제어 스크립트 보완 적용 여부를 판정하지 않는다. 이번 control SHA-256은
  `398e40ff1d1eeb1bbc2a0910f73a4196e25530578d06fafd9e0f9278926d0832`다.
- 실제 승인 카드의 일반 ChatGPT 표시·사용자 클릭·승인 영수증 확인은 남아 있다.
  이번 Codex 대화에서는 미표시가 사용자에게 확인됐고, 기능 지원 여부를 단정하지 않는다.
  공식 [UI 문서](https://developers.openai.com/plugins/build/chatgpt-ui#start-with-mcp-apps)는
  도구 호출과 UI 렌더링을 구분한다. 조사한 공식 문서에는 Codex/Work 위젯 지원표가 없어
  “Codex는 미지원”이나 “서버 결함”으로 결론 내리지 않는다.
- 실제 전환 중 관측/실제 CLI 종료 원인 검증은 다음 승인된 롤아웃의 증거로 확인해야 한다.
  서비스 전환·새 업로드 실행·DB/Docker 변경·Git commit/push·Notion 저장은 하지 않았다.
