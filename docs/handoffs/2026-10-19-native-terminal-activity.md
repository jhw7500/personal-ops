# 네이티브 터미널 활동 수집과 채팅 승인 안내 — 2026-10-19

## 현재 상태

사용자가 승인한 운영 전환을 완료했다. MCP·관리 웹·Codex 관측기는 모두 46ca6ee31dca27798355에서 실행 중이다. 기존 스킬·관측기 복구는 b4178aac843010b8bc97859a2dc8d9a6e1c091f4, 네이티브 터미널 수집은 5d69fe5361236ccc3cd032ee723760287016e3d9로 커밋했다. 후속 승인 안내 수정과 이 결과 문서를 함께 저장한다.

- 실제 MCP PID 3206396, 터널 PID 3206351, 관리 웹 PID 3206745, 관측기 PID 3208094.
- 런타임 89개 파일의 설치 해시·개인 파일 권한 0600을 확인했다. 관리 웹·관측기 active/running, 재시작 횟수 0.
- canonical 전환이 관측한 기존 CLI 세션 10개 모두 유지됐다. Docker·DB·스키마·업로드 작업은 수행하지 않았다.
- Claude 설정의 reporter 경로 10곳만 신규 런타임으로 갱신했다. 다른 설정·후크·권한은 보존했다. 기존 Claude 세션은 종료하지 않았다.
- 다른 세션의 UX019 및 전원 관리 작업과 미커밋 변경을 보존했다. 원격 push는 수행하지 않는다.

## 원인과 변경

실제 운영 페이지에서 personal-ops의 API 세 가지가 모두 HTTP200, JavaScript 예외 0, 관측 지연 없음이었다. 루트와 하위 Codex 캐릭터도 존재했다. 그러나 수집되는 것은 task_id=session-activity인 주기적 생존 신호뿐으로, 개별 터미널 명령과 도구 정보가 없었다.

CodexActivityReader를 기존 관측기에 연결했다. 허용 프로젝트로 매핑된 로드 세션의 명시적인 Thread.path만 사용한다. Codex sessions 아래의 해당 UUID 일반 파일을 디렉터리 FD와 O_NOFOLLOW/O_NONBLOCK으로 읽으며, 최초·교체·재시작 때 EOF에서 시작한다. 파일당 256KiB, 행 64KiB, 호출당 이벤트 20개, 스레드 64개, 대기 호출 128개, 중복 키 512개로 제한한다. polling 사이에는 원문 버퍼를 남기지 않으며 위치·해시·정제된 호출 식별 정보만 보관한다.

- function_call/custom_tool_call: 요청됨(queued).
- 같은 call_id의 output: 반환됨(returned). 입력·출력 본문은 검사하거나 저장하지 않는다.
- item_completed의 CommandExecution/FileChange/McpToolCall: 실제 최종 상태와 유효한 명령 종료코드만 수집한다.
- 완료 기록으로 started/running을 만들지 않는다. 요청됨은 OS 명령의 실행 증거가 아니다.
- 스킬명·사고 단계·명령 목적을 원문에서 추론하지 않는다.
- --no-activity는 기존 세션 메타데이터 관측만 유지한다.

공식 App Server 문서도 thread/read가 이벤트 구독이나 resume을 하지 않는다고 설명한다. 관측을 위해 기존 세션을 resume하거나 제어하지 않았다.
[App Server 문서](https://learn.chatgpt.com/docs/app-server)

화면에서는 같은 담당자의 구체적 대기·진행 호출을 일반 생존 신호보다 우선 표시한다. 완료 기록만 있는 작업을 실행 중으로 재생하지 않고 캐릭터를 추가하지 않는다. 상세의 최근 터미널 활동에 최근 도구·상태·종료코드·작업 상세 링크를 표시한다. 동시 진행 개수에는 실제 호출이 있을 때 중복 생존 신호를 제외한다.

## 반복 승인·새로고침 진단과 수정

실제 MCP 조회에서 personal-ops는 EXPIRED, tossApp은 RUNTIME_CHANGED였다. 유효 권한이 명령마다 다시 승인되도록 강제된 증거는 없었다. 비활성 상태 응답에 승인 종류·사유·기존 채팅 카드로 가는 경로가 없어 호출자가 관리 페이지를 안내하는 누락을 확인했다.

get_host_access는 갱신 가능한 NO_GRANT/EXPIRED/REVOKED/RUNTIME_CHANGED/BOOT_CHANGED 상태에만 approval_kind, approval_reason 및 approval_card → open_host_access_approval(project)를 반환한다. 이 조회는 요청·권한을 발급하거나 연장하지 않는다. active 권한에는 승인 경로를 추가하지 않는다. UID·프로젝트 변경이나 신뢰할 수 없는 런타임은 재승인으로 해결하도록 안내하지 않는다.

request_host_access는 기존 관리자용 요청과 반환 형식을 유지한다. 새 채팅 승인에서는 이 도구를 거치지 않고 open_host_access_approval을 바로 사용한다. 이미 생성한 관리자 요청 뒤에 카드 열기를 안내하면 요청이 두 개 남을 수 있어 그 연결을 제거했다. 실제 카드 경로 검사에서 승인·취소 모두 pending 요청 수 0 → 1 → 0을 확인했다.

호스트 권한은 연결·채팅·MCP PID와 무관한 영속 기록이다. 같은 코드·부팅·사용자·프로젝트·상태 디렉터리에서 새 인스턴스로 연결해도 승인 ID·만료 시각·Codex 허용 여부를 그대로 유지한다. 새로고침은 기간을 늘리지 않는다. 최대 8시간과 기본 4시간 정책은 유지했다. 표시 전용 대시보드 파일 5종의 변경은 기존 권한을 유지하지만, 권한 코드·부팅·사용자·프로젝트 변경과 만료·철회는 계속 검증한다. 기존 무효 권한을 자동 복구하지 않았다.

```text
refresh/reconnect -> get_host_access -> active   -> continue
                                    -> inactive -> chat card -> owner approval
```

도구 설명에는 일반 호스트 명령이 유효 권한을 재사용하며, 사용자의 실행 요청과 필요한 승인이 이미 있으면 추가 실행 지시를 요구하지 않도록 명시했다. 별도 작업 계획의 승인 요건은 유지한다. 모든 승인 종류가 카드로 바뀐 것은 아니다. 기존 런타임 롤아웃·등록형 작업의 별도 승인 경로는 이 수정 대상이 아니다.

## 검증

- 신규 카드 안내 회귀 검사의 RED를 확인하고 최종 관련 검사 72개를 통과했다. 독립 검토 34개 검사·Ruff·diff 검사 통과, 남은 지적 0개 APPROVE.
- canonical 최종 후보 검사: 승인 UI 12개, Schema014 UI 4개, 호스트 명령 UI 4개, 런타임 롤아웃 UI 7개, 핵심 Python 829개, 전체 Python 1027개 실행 모두 종료 코드 0. 실제 Codex probe는 opt-in 비활성 상태로 유지했다.
- 동일 프런트엔드 소스에 대한 사전 검증은 JavaScript/bridge 244개 및 실제 HTTP Chromium 관리 UI 110개 통과였다. 페이지 종료 때 기존 BrokenPipe 로그가 있었으며 서버 로그 정리는 이번 변경에 포함하지 않았다.
- 고정 소스·테스트 227개가 검사 중 바뀌지 않았고 설치 파일 89개 해시가 일치했다. 추가 변경 소스·테스트 Gitleaks 탐지는 0개였다.
- 운영 반영 전 personal-ops의 네이티브 호출 기록은 0개였다. 반영 후 실제 운영 API·화면에서 18개를 확인했다. 합성 이벤트나 모델 호출을 사용하지 않았다.
- 실제 운영 LAN 주소에서 1440/390/320 화면을 캡처하고 육안 확인했다. 같은 담당자 요소 1개, CommandExecution 반환·종료 코드 0, 부모/iframe 가로 넘침 0, 페이지 JavaScript 오류 0. 제공되는 JS 두 파일은 신규 설치 스냅샷과 해시가 같았다.
- 최초 운영 화면 검사기는 작업 상세를 연 뒤 생존 신호 상세에만 있는 조건부 패널도 요구해 실패했다. 검사기의 선택 조건만 수정하고 실제 명령 상세·종료 코드로 재검증했다. 운영 코드는 다시 변경하지 않았다.
- 실제 관리 페이지를 새로고침했을 때 영속 권한 기록 2개의 바이트가 그대로였다. 운영에는 유효한 active 권한이 없어 활성 권한의 재연결·옵션·만료 불변성은 임시 상태 회귀 검사로 검증했다.
- 재연결 중 tossApp 조회 1회 오류가 있었으나 순차 재호출은 성공했다. 실제 MCP에서 EXPIRED/RUNTIME_CHANGED 각각의 사유와 정확한 채팅 카드 도구 경로가 반환됐다. 권한은 발급하지 않았다.

## 운영 전환 결과와 증거

비공개 증거 경로:

/home/jhw/.local/state/personal-ops-terminal-work/20261019-ujvos8i6/activation-z9g5zifg/final

combined-activation-plan.json, combined-frozen-source.json, combined-package.json, execution-preflight.json, live-runtime-result.json, path-activation-result.json, remote-card-routing-result.json, refresh-result.json, live-dashboard-result.json 및 live-dashboard-*.png를 참조한다. 설정 원본 백업과 로그는 비공개 경로에만 보관한다.

최종 canonical receipt는 combined-receipts/rollout-20keo4vy/receipt.json이다. 서비스 전환·런타임·관리 웹·기존 CLI 보존 검증은 성공했다. canonical 종료 코드 2, status=verification_required는 passive catalog가 awaiting_discovery이고 connector_schemas_verified=false이기 때문이다. 호스트 배포 실패로 해석하지 않는다. 실제 기존 MCP 도구에서 신규 응답은 확인했지만 ChatGPT 전체 도구 목록·설명 캐시 갱신을 확인한 것과 구분한다.

중간 후보 aadba2247845916143e2와 64cf5b994ac415a7e68c는 검토 보완을 위해 설치·전환 전에 검증 실행만 중단했다. 두 ROLLOUT_TERMINATED/143 receipt를 삭제하지 않았다. 실제 호스트는 a6a788b75b5b900a6ac1에서 최종 46ca6ee31dca27798355로만 전환했다.

## 웹 채팅에서 이어갈 때

일반 ChatGPT에서 get_host_access(tossApp)를 조회한다. active면 기존 요청한 일반 개발 작업을 그대로 이어간다. 현재 RUNTIME_CHANGED라면 반환된 approval_card의 open_host_access_approval을 사용해 채팅 안에서 소유자가 승인한다. 관리 페이지 링크나 request_host_access를 새 채팅 카드의 앞 단계로 사용하지 않는다. 실제 승인 뒤 같은 유효 권한을 재사용하고 새로고침 때문에 다시 승인하지 않는다. 유효한 별도 계획 승인과 기존 실행 요청이 있다면 추가 실행 지시를 반복 요구하지 않는다.

다른 세션의 기존 UX019 롤아웃 후보는 이번 확장을 포함하지 않고 기준 런타임도 이전 버전이다. 재사용해 최신 기능을 되돌리지 말고 현재 설치 버전과 소스 변경을 보존한 계획을 다시 검토해야 한다.

남은 확인은 일반 ChatGPT에서 전체 도구·설명 캐시 갱신 및 실제 소유자 카드 선택, 그리고 실행 중 Claude 세션에서 신규 reporter의 실제 후크 수신이다. 기존 Claude 세션을 강제 종료하지 않았다. 공식 문서상 설정 변경은 보통 파일 감시자가 반영하지만 ConfigChange 정책·실행 버전에 따라 다를 수 있어 실제 이벤트 전에는 적용 완료를 주장하지 않는다.
[Claude 후크 설정 변경](https://code.claude.com/docs/en/hooks#disable-or-remove-hooks)
