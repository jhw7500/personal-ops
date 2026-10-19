# 네이티브 터미널 활동 수집 확장 — 2026-10-19

## 현재 상태

기존 스킬 표시·관측기 복구 변경 20개 파일은 b4178aac843010b8bc97859a2dc8d9a6e1c091f4로 커밋했다. 이번 추가 확장은 격리 작업 경로에서 구현·검증했으며 운영 전환은 아직 실행하지 않았다.

- 실행 중인 MCP/Admin: a6a788b75b5b900a6ac1.
- 실행 중인 Codex 관측기: a5827f53701d19619a9c.
- 배포 후보: aadba2247845916143e2, 런타임 89개 파일.
- Claude 설정의 기존 reporter 10곳: 9413d24e559a5901561c에 고정되어 있으며 이 버전은 Skill 식별을 지원하지 않는다.
- 다른 세션의 UX019 소스·전원 관리 작업·업로드·DB는 보존했다. 원격 push는 수행하지 않는다.

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

## 검증

- 전체 Python unittest: 1018개 실행, 1017개 통과, opt-in 실제 Codex probe 1개 생략.
- 최종 파일 디스크립터 정리 수정 후 native reader 회귀 검사 13개를 새로 실행해 통과했다.
- 독립 검토: 대상 Python 59개와 모델 20개 통과, 보안·코드 지적 0개, APPROVE.
- 전체 JavaScript/bridge: 244개 통과.
- 실제 HTTP Chromium 관리 UI: 110개 통과. 페이지 종료 시 기존 서버의 BrokenPipe 로그가 발생했으나 검사 실패나 페이지 JavaScript 예외는 없었다. 해당 서버 로그 처리는 이번 수정에 포함하지 않았다.
- 운영 기록을 읽는 임시 수집기에서 실제 네이티브 세션 4개의 호출 26개를 관측했다. 추가 관측에서는 파일 변경과 종료코드 1인 명령 실패도 확인했다. 합성 업무나 모델 호출은 사용하지 않았고 운영 저장소에 이벤트를 주입하지 않았다.
- 이 실제 기록을 격리된 후보 관리 UI에 연결해 1440/390/320 화면을 캡처·확인했다. CommandExecution, 반환 상태, 종료코드가 표시됐고 부모/iframe 가로 넘침과 페이지 예외는 0이었다.
- 소스·테스트 227개 파일을 고정하고 패키지 89개 파일을 해시로 검증했다.
- 패키지 Gitleaks의 4개 탐지는 모두 생성된 manifest.json의 파일 해시였다. manifest 전체가 실제 89개 파일에서 계산한 SHA-256 맵과 정확히 같음을 검증했다. 실제 소스 검사 결과와 구분해 원본 보고서를 보존했다.

## 운영 반영 대기

후보와 계획은 다음 비공개 경로에 있다.

/home/jhw/.local/state/personal-ops-terminal-work/20261019-ujvos8i6

package.json, activation-plan.json, frozen-source.json, package-integrity.json, native-ui-result.json 및 검사 로그를 참고한다. 원본 설정이나 토큰을 저장소에 넣지 않는다.

승인 후 수행할 범위:
1. canonical rollout으로 정확한 aadba2247845916143e2를 설치·전환하고 터널/Admin을 검증한다.
2. 관측기 unit의 런타임 경로만 갱신하고 관측기 서비스를 재시작한다.
3. Claude settings의 기존 reporter 명령 10곳에서 실행 파일 경로만 신규 런타임으로 바꾼다. 다른 후크·설정·파일 권한은 보존하고 원본 해시가 달라졌으면 재검토한다.
4. 실제 운영 API·화면에서 새 네이티브 호출과 종료 결과를 확인한다. 기존 독립 Codex/Claude 세션, Docker와 DB, 키·호스트 권한은 유지한다.

Claude 공식 문서상 설정 파일의 후크 변경은 보통 파일 감시자가 자동 반영한다. /hooks는 조회 메뉴이며 강제 재시작을 요구하지 않는다. 다만 ConfigChange 정책이나 실행 버전에 따라 달라질 수 있어 새 실제 후크 이벤트가 도착하기 전 활성 세션의 반영 완료를 주장하지 않는다.
[Claude 후크 설정 변경](https://code.claude.com/docs/en/hooks#disable-or-remove-hooks)

현재 관리되는 다른 UX019 고정 롤아웃 계획은 이번 확장을 포함하지 않는다. 다른 세션의 소스를 덮어쓰거나 그 계획을 갱신하지 않았다. 배포 직전 실제 기준 런타임이 달라졌으면 정합성을 재확인하고, 이전 후보로 새 기능을 되돌리지 않는다.

이번 소스 변경·검증·커밋은 서비스 전환 승인이 아니다. 실제 서비스 재시작과 호스트 설정 변경은 앞서 사용자가 지정한 별도 승인 후에만 수행한다.
