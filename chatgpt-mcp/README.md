# ChatGPT coding MCP

SSH 호스트에 설치된 Serena를 OpenAI Secure MCP Tunnel에 연결한다.
하나의 터널과 플러그인으로 허용 목록에 등록된 여러 저장소를 다룬다.
각 파일 도구의 `project` 인자가 작업 대상을 결정한다.

목적은 **일반 ChatGPT 채팅에서 판단·코드 작성·결과 설명을 하고, 호스트 MCP가
파일 작업과 명령 실행을 수행**하는 것이다. ChatGPT Work 프로젝트를 사용할 필요는 없다.
일반 파일·명령 작업은 별도 추론 API를 호출하지 않는다. 명시적으로 위임한 작업은
등록된 Codex CLI를 호출하며 해당 계정의 모델 사용량을 소비한다. Claude 위임은 제공하지 않는다.
터널 API 키는 연결에 사용한다. 필요한 작업만 Codex에 위임할 수 있지만
ChatGPT 자체의 사용량이 없어지거나 전체 토큰 절감량이 보장되는 것은 아니다.
1.2.0부터 격리된 셸·빌드·테스트 실행, 상태·로그·취소,
Git 전용 도구는 조회와 승인된 commit·push를 제공한다. PR 생성과 merge는 기존 네이티브
리뷰 워크플로우의 정책을 따른다. 기간제 호스트 실행도 해당 정책과 명시적인 작업 지시를 준수한다.
1.4.0부터 별도 로컬 관리 화면, MCP 호출 타임라인, 레포별 영속 메모리를 제공한다.
1.5.0부터 작업 목표·완료 항목·다음 작업, Git 변경 파일과 diff, 빌드·테스트 결과 및 새 채팅용 요약을 함께 제공한다.

```text
ChatGPT <-> OpenAI Tunnel <-> tunnel-client <-> repository router
                                              |
                                              +-> files -> Serena
                                              +-> command -> sandbox -> job/log
                                              +-> Git -> approval/native workflow
                                              +-> project memory / activity
                                              +-> host grant -> command / log
                                              +-> Codex task -> isolated / host grant

Browser <-> SSH local forwarding <-> admin HTTP (127.0.0.1:8765)
                                     |-> job/log/activity (read only)
                                     +-> project memory (read/write)
                                     +-> host grant / revoke / task stop
```

새 WSL 설치는 임시 아카이브를 옮기지 않고 Git clone에서 시작한다. Python 3.11,
고정 Serena commit, MCP 1.23.0의 검증된 제약과 read-only 계획 → 명시적 적용 절차는
[WSL clone-first setup](./WSL-SETUP.md)을 따른다. 초기 적용은 새 로컬 identity와
ABI 3 authority 및 설치 범위만 준비하며 host grant, 외부 credential, 서비스와
tunnel을 만들거나 시작하지 않는다.

## 로컬 MCP

```bash
python3 chatgpt-mcp/server.py
python3 chatgpt-mcp/server.py --config /absolute/path/projects.json
# 기존 단일 저장소 방식의 로컬 진단
python3 chatgpt-mcp/server.py --project /absolute/project/path
```

- 기존 `~/.local/share/uv/tools/serena-agent` 설치를 사용한다.
- MCP 프로세스는 stdio로 통신한다. 관리 HTTP 프로세스는 별도로 실행한다.
- 레포 목록 조회용 `list_projects`, 선택 UI를 명시적으로 여는
  `open_project_picker`, UI 선택을 검증하는 `select_project`와 기존 파일 도구 6개를 제공한다.
  파일 도구에는 매번 `project`가 필요하며 생략하거나 미등록 이름을 주면 거부한다.
- 백엔드 선택과 도구 실행을 직렬화하여 동시 요청의 작업 대상이 섞이지 않도록 한다.
  같은 레포의 요청은 백엔드를 재사용하고 레포가 바뀌면 해당 레포로 새로 실행한다.
- 명령 실행은 파일 백엔드와 분리된 작업 실행기를 사용한다.
  공유된 현재 레포는 없으며 모든 호출에 `project`를 명시한다.
- 실제 터널은 `install-runtime.py`로 작업 레포 밖에 설치한 코드 스냅샷을 실행한다.
  레포 코드 편집이 운영 서버·승인 helper를 바꾸지 않으며, 새 코드 적용은 별도 설치·재연결한다.
  설치 명령은 `python3 chatgpt-mcp/install-runtime.py`이고 반환된 `server` 경로를
  터널의 MCP 실행 명령에 사용한다. 현재 배포 위치는 사용자 홈의
  `.local/lib/chatgpt-coding-mcp/releases/<snapshot>/`이다.
- 텍스트 수정 기능에 필요한 Python 언어 서버를 사용한다.
  심볼 단위 편집 도구는 제공하지 않는다.
- 상태는 `$XDG_STATE_HOME/chatgpt-coding-mcp/` 또는
  `~/.local/state/chatgpt-coding-mcp/`에 프로젝트 경로별로 저장한다.
  기존 `~/.serena`와 저장소의 `.serena`는 사용하지 않는다.
- 라우터는 절대 경로, `..`, 레포 밖을 가리키는 심볼릭 링크를 거부한다.
  `.git`과 `.env` 계열 경로도 제외한다.
  심볼릭 링크가 포함된 하위 트리의 재귀 검색은 거부되므로 더 좁은 경로를 지정한다.
  이는 OS 샌드박스가 아니며 외부 프로세스의 동시 파일 교체까지 차단하지는 않는다.

## 등록형 호스트 명령

호스트에서 수행하는 Toss TEST 빌드·업로드 등을 위해 등록형 Host Command Runner를 제공하는 소스가 추가되어 있다. 기존 start_command는 네트워크/호스트 HOME을 차단하는 격리 실행기이며, 별도 호스트 실행 권한으로 자동 승격되지 않는다.

등록형 Host Command Runner는 관리자가 저장소 **밖**의 소유자 전용(0600) JSON 정책 파일에 고정한 실행 파일·SHA-256·인자·작업 폴더·입력 파일 SHA-256으로만 동작한다. 이 등록형 경로에서는 자유로운 셸 문자열·환경변수·Docker/DB 작업을 전송할 수 없다. 아래 기간제 호스트 권한 경로는 별도 기능이다.

새 소스의 사용 흐름은 list_host_command_profiles → prepare_host_command의 채팅 카드에서 **승인하고 실행** → get_host_command / cancel_host_command다. 앱 전용 confirm_and_execute_host_command가 기존 일회용 승인을 발급하고 같은 콜백에서 고정된 명령 실행을 접수하므로, 웹 모델의 두 번째 execute_host_command 호출이 필요 없다. 관리자 페이지나 관리자 토큰 입력도 필요하지 않다.

기존 계획은 open_host_command_approval로 다시 연다. 이미 승인된 계획을 열기만 해서는 실행하지 않는다. 예전 카드의 ‘이 계획 승인’은 승인만 하며, 사용자 실행 요청이 있을 때 기존 execute_host_command 경로로 이어간다. 새 소스의 계획은 생성부터 7일이며 카드 승인이 유효시간을 늘리지 않는다. 통신 오류로 결과가 불확실하면 get_host_command로 확인하고 자동 재실행하지 않는다. 외부 TEST 업로드 취소는 업로드를 되돌리지 않는다. 2026-10-20의 이 보완은 별도 서비스 전환 전까지 소스에만 적용된다.

2026-10-16에 업로드 카드 보완본 `8c7a5aff0c75198c6c5d`를 운영에 반영했다. 전체 892개 검사, 설치본 도구 76개 등록·프로토콜 검사 3개, 실제 터널 프로필 조회와 관리 웹 HTTP 검증을 통과했다. ChatGPT 연결을 새로고침한 뒤 실제 업로드 카드 표시를 확인해야 한다. CLI 보존 검사에서는 전환 직전 살아 있던 Claude 프로세스 1개의 사후 종료가 관측됐으며 원인은 미확인이다. [업로드 채팅 승인 인계](../docs/handoffs/2026-10-16-host-command-inline-approval.md)를 참조한다.

2026-10-15 기준 운영 런타임 `ce88340086af9f5e1bf2`에 다섯 도구가 등록되어 있고, `tossApp`에는 다음 두 프로필이 활성화되어 있다.

| Profile | Command | Scope |
| --- | --- | --- |
| `tossapp-deploy-test` | `tossApp-deploy test` | Build + TEST upload + SELF invitation |
| `tossapp-upload-test` | Pinned upload wrapper | Existing .ait upload only |

`tossapp-deploy-test`는 기존 호스트 스크립트 `~/.local/bin/tossApp-deploy`와 고정 인자 `test`를 그대로 실행한다. 승인 후 타입 검사·프런트 빌드·새 `.ait` 생성·업로드·본인 초대장 발송을 한 작업으로 진행한다. 호스트의 기존 토스 OAuth 연결을 사용하므로 웹 대화에 토스 콘솔 플러그인을 추가할 필요는 없다. 실행 파일 해시와 빌드 설정 입력 10개의 해시를 검증하며, 해당 설정을 바꾸면 관리자가 프로필 해시를 갱신해야 한다. 설정 입력은 그룹·다른 사용자의 쓰기 권한이 없어야 한다.

이전 고정 번들 프로필은 `tossapp-upload-test`로 보존했다. 이 프로필은 이미 만들어진 특정 `.ait`만 업로드하며 빌드·발송을 수행하지 않는다. 전체 실행 프로필은 사용자 실행 승인 후 실제 터널 MCP로 빌드·TEST 업로드·본인 초대장 발송까지 완료했다. 새 버전 `20261015-23`의 콘솔 컴파일 성공(`CREATED`)과 `isTested=true`, 종료 코드 0·프로세스 종료 확인을 검증했다. 상세 결과는 [전체 TEST 배포 결과](../docs/handoffs/2026-10-15-tossapp-full-test-deployment.md), [프로필 정정 인계](../docs/handoffs/2026-10-15-tossapp-deploy-profile-correction.md)와 [Host Command Runner 인계](../docs/handoffs/2026-10-14-host-command-runner.md)를 참조한다.

## 기간제 호스트 권한과 기존 Codex 호출

2026-10-21 기간 연장 변경은 소스에만 적용했다. 카드·작업 계획은 생성부터 7일,
호스트·workspace 권한은 승인 시점부터 기본·최대 30일이다. 기존 저장 기록의
만료는 자동 연장하지 않는다. 철회, 재부팅·런타임·정책·입력 변경에 따른 무효화는
계속 적용한다. 운영 반영과 실제 ChatGPT 연결 확인은 별도 단계다.

호스트 명령 기능은 운영 런타임에 반영했다. 런타임 설치와 사용자 권한 부여는 별도이며,
실제 허용 상태는 `get_host_access`로 확인한다.

`request_host_access(project)`는 권한 요청만 만든다. 기존 관리 화면에서 호스트·사용자·UID·그룹,
파일·네트워크·그룹 접근 범위, 유효 기간과 Codex 사용 동의를 확인하고 소유자가 승인한다.
새 소스의 기본·최대 기간은 30일이며 자동 갱신하지 않는다. 관리 화면의 승인은 loopback 또는
등록된 Tailscale HTTPS 주소에서 새 관리 토큰으로 인증한다. LAN HTTP 화면은 조회에 사용한다.
`get_host_access`에서 실제 활성 상태를 확인한다.

호스트 권한용 채팅 승인 카드는 운영 반영했으며 실제 ChatGPT에서 표시·승인을 확인했다.
`open_host_access_approval(project, duration_seconds?, allow_codex?)`가 카드를 열면
호스트·사용자·작업 시작 위치·접근 범위·기간·Codex 동의를 확인하고 **승인**을 누른다.
카드를 여는 요청이나 프롬프트의 “승인” 문장만으로 권한이 발급되지는 않는다.
새 소스의 기본·최대 기간은 30일이고 더 짧은 기간도 선택할 수 있다. Codex 동의는 기본 꺼짐이며 카드에서 변경할 수 있다.
카드에는 관리 토큰을 넣지 않는다. 기존 관리 웹은 복구 경로로 유지한다.

확인값은 결과 `_meta`로 신뢰한 MCP 호스트/컴포넌트에만 전달하며 모델용 응답·위젯 상태·
로그에는 저장하지 않는다. 앱 전용 확인 도구는 이 일회용 값과 프로젝트·런타임·재부팅·
사용자·작업 경로가 일치해야 동작한다. 새 소스의 카드 유효 기간은 7일이며 옵션 변경은 확인값을
교체하지만 유효 기간을 늘리지 않는다. 같은 프로젝트의 새 카드를 열면 이전 카드가
무효화된다. 취소는 해당 요청을 없애며 이미 활성화된 권한을 철회하지 않는다.
응답이 유실되면 자동 재승인하지 말고 `get_host_access`로 결과를 확인한다.

앱 전용 도구 표시 설정 자체는 인증이 아니다. 이 흐름은 인증된 터널의 MCP 호스트와
카드 컴포넌트를 신뢰하며, 이를 제어하는 호출자는 같은 신뢰 경계 안에 있다.
카드 클릭을 별도의 호스트 소유자 계정 인증으로 해석하지 않는다. 기존 관리 웹의
새 토큰 인증 정책은 그대로 유지한다.

권한은 선택 프로젝트를 사용하는 **이 MCP 설치의 모든 호출자**에 적용된다. 대화별 권한이 아니다.
`project`와 `cwd`는 시작 위치를 선택하며 파일시스템 격리가 아니다. 실제 호스트 계정의
파일·네트워크·그룹 접근 권한을 사용한다. Docker/sudo 그룹은 화면에 실제 값으로 표시하며,
sudo 그룹 가입이 비밀번호 없는 sudo를 보장하지 않는다. 같은 UID에 대한 공격 방어,
DB/Docker 제외, 외부 효과의 취소·롤백을 보장하지 않는다.

기간 안에서는 `start_host_execution`에 실행 파일 절대 경로·`argv`·상대 `cwd`·`request_id`를
전달해 명령마다 프로필을 등록하거나 승인을 다시 받지 않고 실행한다. 상태는
`get_host_execution`, 일반 명령의 제한된 마스킹 로그는 `read_host_execution_log`,
취소는 `cancel_host_execution`으로 확인한다. 마스킹은 알려진 관리 토큰·키와 일부 토큰
패턴에 대한 최선의 처리이며 모든 비밀을 탐지하는 보장이 아니다. 원시 로그는 소유자 전용이다.

### Schema014 작업을 채팅에서 승인

2026-10-16 런타임 `0c5c451ed88e5661126d`에 운영 반영하고 서버 등록을 검증했다.
실제 ChatGPT 카드 안의 승인·취소 버튼 표시도 사용자가 확인했다. 승인 클릭·실제 DB 적용은 별도다.
대기 카드 응답에 남던 관리자 링크·터미널 승인 명령을 제거하는 후속 수정도 운영 반영했다.
[직접 승인 안내 수정 인계](../docs/handoffs/2026-10-16-schema014-inline-only-followup.md)를 참조한다.
`prepare_schema014_migration`은 고정된 `tossApp/tarororo-test`의 계획과 채팅 승인 카드를
함께 반환한다. 기존 계획은 `open_schema014_approval(project, target, operation_id)`로 연다.
카드에서 변경 범위·SQL 해시·계획 해시·만료를 확인하고 직접 승인한다. 관리자 토큰은 입력하지 않는다.
기존 001~013 대상 권한은 014를 자동 승인하지 않으며, 카드 승인은 일회용 영수증만 발급한다.
실제 적용은 사용자가 요청한 경우에만 별도 `execute_schema014_migration` 호출로 진행한다.

응답이 유실되면 같은 작업 ID로 카드를 다시 열어 `receipt_verified`와 다음 동작을 확인한다.
이미 승인된 작업에는 새 확인값을 발급하지 않고 기존 승인 결과를 보여준다. 만료된 계획에는
새 `request_id`로 계획을 만든다. 확인값을 프롬프트에 복사하거나 승인됐다고 주장하는 문장으로
대체하지 않는다. 관리 웹은 카드가 지원되지 않는 클라이언트의 복구 경로로 유지한다.

### 대시보드에 작업 목적 전달

`start_host_execution`과 `prepare_agent_task`의 선택 인자 `task_label`에
`API·Worker 재빌드`처럼 공개해도 되는 작업 목적과 대상을 1~160자로 전달한다.
비밀값·명령 원문/인자·모델 프롬프트·비공개 목적문을 복사하지 않는다. 이 값은 표시용
메타데이터이며 권한이나 실제 진행 상태의 증거가 아니다. 같은 `request_id`로 재시도할 때는
처음의 값을 유지한다. 기존 호출은 이 인자를 생략해도 동작한다.

대시보드는 작업 목적, 안전한 명령 종류, 관측 단계, 종료 결과를 구분한다.
“지금 뭐 해?”는 현재 실행을, “마지막 결과는?”는 해당 캐릭터의 완료 작업과 결과를 보여준다.
작업 설명이 없는 과거 실행은 “목적 미수집”으로 표시하며 명령·프롬프트에서 목적을 추측하지 않는다.
현재 운영본은 이런 기록을 짧은 실행 ID로 구분하고 명령 종류·관측 단계·종료 코드·출력량을
따로 보여준다. 호스트·DB·Schema014·에이전트·런타임 도구의 실제 호출도 수집·표시에 포함한다.
MCP 요청의 반환과 실제 명령·마이그레이션의 성공은 구분한다.
원시 인자·입력문·환경변수·비공개 로그는 작업실 프레임에 전달하지 않는다.
Codex 실행 자체나 채팅의 행동 보고를 실제 내부 코딩·테스트·추론 관측으로 취급하지 않는다.

기본 작업 설명은 2026-10-16 runtime `dfc699ead12d7bcc378e`에서 운영 검증했고,
후속 상세 개선은 `ea340a2b1759c297a5b1`에서 검증했고,
직접 승인 안내·최대 8시간 설정은 `0c5c451ed88e5661126d`에서 운영 검증했다.
ChatGPT의 도구 정의 새로고침과 변경된 호스트 권한의 사용자 재승인은 별도 단계다.
최신 구현·검증·배포 상태는 [채팅 승인·작업 상세 인계](../docs/handoffs/2026-10-16-chat-schema014-dashboard-details.md)를 참조한다.

Codex는 기존 호출 순서를 유지한다:

```text
request_host_access -> owner grants access + Codex usage
prepare_agent_task -> start_agent_task -> get_agent_task
                                      -> cancel_agent_task
```

`prepare_agent_task.execution_mode`는 `auto`(기본), `host`, `isolated` 중 하나다.
`auto`는 유효한 Codex 위임 동의가 포함된 권한이 있을 때 호스트 실행을 선택하고,
없으면 기존 격리 검토 및 일회용 승인 흐름을 따른다. `host`는 실제 체크아웃을
편집·실행할 수 있으며 만료·철회된 권한으로 시작하지 않는다. `isolated`는
기존 격리 증명 검사에 실패해도 호스트 실행으로 전환하지 않는다.

모델과 추론 수준은 기존 호스트 소유 프로필에서 고정한다. 현재 등록값은
`codex-tossapp-review`의 `gpt-6.1-sol` / `high`이다. 프로필의 `ready`/`reason`은
격리 실행 상태이고, 권한이 활성화되면 추가되는 `host_ready`/`host_reason`은
별도의 호스트 실행 상태이다. 호스트 실행은 오래된 격리 증명이나 CAS를 정상으로 바꾸지 않는다.
전용 임시 CLI를 실행하고 기존 사용자 세션을 resume하거나 종료하지 않는다.

권한 부여만으로 모델을 호출하지 않는다. Codex 동의는 전용 위임 도구를 허용하는 설정이며
범용 호스트 셸의 모델 호출이나 사용량을 기술적으로 제한하는 장치는 아니다.
작업별 시간 제한은 사용량 상한이 아니다. 응답의 사용량은 공급자가 실제 보낸 값이고,
모델의 완료 보고·테스트 주장은 독립적인 기능 검증과 구분한다. Codex 원시 JSON 로그의
추론·프롬프트 내용은 공개 로그 도구로 반환하지 않는다.

권한 만료·철회는 새 시작을 차단하고 기록된 실행 범위의 중단을 요청한다. 재부팅이나
호스트 실행·승인·로그인·메시지 경계 코드의 변경 후에는 권한이 무효화된다. 전체 설치
manifest의 파일 무결성은 계속 검사하되, 격리된 작업실의 표시 자산 다섯 개만 변경한
배포에서는 기존 승인 ID·만료 시각·Codex 동의를 유지한다. 관리자 페이지는 해당 코드나
CSS를 직접 로드하지 않으며, 토큰 없는 opaque-origin sandbox iframe에 공개 작업 상태만
전달한다. 새 파일은 기본적으로 권한 식별에 포함한다. 이 정책의 최초 전환에는 한 번
재승인이 필요하며 기존 권한을 자동 이전하거나 연장하지 않는다.

이 정책은 소스 구현을 설명한다. 실제 적용 여부는
[대시보드 운영 인계](../docs/handoffs/2026-10-15-dashboard-operations-stage1.md)의 5차 기록을
확인한다. 이미 적용한 파일·DB 변경, 업로드 또는 별도로 시작한 서비스는 자동 복구되지
않는다. 기존 등록형 명령과 Docker/DB 도구는 각자의 승인 정책을 유지하므로 같은 작업을
두 경로에서 중복 실행하지 않는다.

## 셸·빌드·테스트 실행

`list_execution_profiles`는 루트와 바로 아래 디렉터리의 `package.json`에서
`build`, `test`, `test:e2e`, `typecheck`, `lint`를 발견한다. 현재 `tossApp`에서는
`ai-tarot:build`, `ai-tarot:test`, `ai-tarot:test:e2e`, `ai-tarot:typecheck`, `ai-tarot:lint`,
`server:test`, `server:typecheck`를 확인했다. 목록 조회는 실행을 시작하지 않는다.

`start_command`는 프리셋 또는 직접 `argv` 중 하나를 받아 작업 ID를 반환한다.
새 실행에는 새로운 `request_id`를, 동일 요청 재전송에는 같은 값을 쓴다.
동일 ID에 다른 명령을 보내면 거부한다.

```json
{"project": "tossApp", "request_id": "tarot-check-001", "preset": "ai-tarot:typecheck"}
```

`get_job`으로 상태·종료 코드를 확인하고 `read_job_log`의 `next_offset`으로
로그를 이어 읽는다. `list_jobs`는 최근 내역을 조회하고 `cancel_job`은
이 MCP가 실행 중인 격리 작업만 취소한다. 실행 중이라는 응답은 성공이 아니다.

```text
start_command -> queued -> running -> succeeded / failed / timed_out / canceled
                    |
                    +-> get_job + read_job_log
server restart -> unfinished records become interrupted
```

기본 실행 시간은 900초, 최대 3600초이다. 레포당 한 작업, 전체 4개를 동시 실행한다.
작업당 로그 1 MiB, 한 번 조회 16 KiB로 제한하고 초과 여부를 결과에 표시한다.
상태의 `jobs/` 디렉터리는 `0700`, 파일은 `0600`이다. 기록은 최대 1000개이며
이후에는 호스트에서 보관·정리해야 한다. 재시작 전 미완료 기록은 성공을 추측하거나
저장된 PID를 종료하지 않고 `interrupted`로 처리한다.

`bubblewrap`을 사용하며 일반 호스트 셸로의 대체 실행은 없다. 선택한 레포만
`/workspace`에 쓰기 가능하게 마운트한다. 시스템 파일은 읽기 전용이고 네트워크,
호스트 홈·SSH 키·토큰, `.git`, `.env` 계열은 제외한다. 셸도 격리 안에서 실행한다.
기존 의존성을 사용하는 로컬 검증에 적합하며 네트워크·DB·비밀값이 필요한 테스트와
의존성 다운로드는 실패할 수 있다.

명확한 Yocto 구조·캐시가 있는 레포, 직접 BitBake 명령, 시스템 PATH에 BitBake가
설치된 환경은 차단한다. BitBake 실행·캐시 변경은 이 도구에서 지원하지 않으며
호스트에서 명령·영향 설명과 매 실행 별도 승인을 받아 수행한다.

실행 중인 레포의 MCP 파일 수정·추가 실행·Git 변경은 잠근다. 외부 터미널·IDE의
동시 편집은 막지 못한다. 모델은 상태·로그에서 실제 관측한 내용을 설명해야 하며
ChatGPT 클라이언트의 모든 중간 메시지 표시를 보장하지 않는다.

프리셋은 `~/.config/chatgpt-coding-mcp/execution.json`에 추가한다. 허용 레포 밖의
현재 사용자 소유 일반 파일이고 모드는 `0600` 또는 `0644`여야 한다.
환경 변수·네트워크 허용 옵션은 제공하지 않는다.

1.2.1부터 호스트에 설치된 Node를 레포별로 지정할 수 있다. 격리 환경은 로그인 셸의
NVM 설정을 읽지 않으므로 지정하지 않으면 시스템 Node가 선택될 수 있다.
현재 `tossApp`은 `.nvmrc`와 두 패키지의 요구 조건에 맞춰 Node 24.12.0을 사용한다.
아래 `toolchains`를 같은 설정 파일에 넣는다. 전체 홈이나 `.nvm` 폴더 대신 선택한
Node 설치 경로만 `/opt/node`에 읽기 전용으로 연결하고 `/opt/node/bin`을 PATH 앞에 둔다.

```json
{
  "version": 1,
  "toolchains": {"tossApp": {"node": "/home/jhw/.nvm/versions/node/v24.12.0"}},
  "profiles": {}
}
```

Node 경로는 절대 경로이며 작업 레포 밖의 설치 경로여야 한다. 일반 명령 인자로
이 설정을 변경할 수 없다. 설정은 호출 때 다시 읽으므로 이후 등록된 Node 버전 변경에는
서버 재시작이 필요하지 않다. 홈·키·네트워크·다른 레포의 접근 제한은 유지한다.

1.2.2부터 같은 `toolchains`에 레포별 Chromium 설치 디렉터리를 지정할 수 있다.
현재 `tossApp`의 Playwright 1.58.0과 일치하는 기존 브라우저(revision 1208)를 사용한다.
브라우저의 실행 파일과 리소스가 있는 디렉터리 하나만 `/opt/chromium`에 읽기 전용으로
연결하고, `PLAYWRIGHT_CHROMIUM_PATH=/opt/chromium/chrome`을 자동 설정한다.
기존 앱의 `executablePath` 환경변수 설정을 그대로 사용하므로 앱 소스 수정은 필요 없다.

```json
{
  "version": 1,
  "toolchains": {"tossApp": {
    "node": "/home/jhw/.nvm/versions/node/v24.12.0",
    "chromium": "/home/jhw/.cache/ms-playwright/chromium-1208/chrome-linux64"
  }},
  "profiles": {}
}
```

Chromium 경로는 작업 레포 밖의 독립 설치 디렉터리여야 하며 `chrome`, `icudtl.dat`,
`resources.pak`을 포함해야 한다. 실행 파일은 실행 가능한 ELF 바이너리여야 하므로
`/snap/bin/chromium` 같은 서비스 래퍼는 사용할 수 없다. 전체 홈이나 브라우저 캐시는
공유하지 않는다. 브라우저와 테스트 웹서버는 같은 격리 작업 안에서 실행한다.
그 격리 환경의 `127.0.0.1` 접속은 가능하지만 호스트의 localhost 서비스나 외부 네트워크에는
접근하지 않는다. 브라우저를 새로 다운로드하거나 시스템 패키지를 설치하지 않는다.

`ai-tarot:test:e2e` 프리셋으로 실행하고 작업 상태·로그에서 실제 결과를 확인한다.
브라우저 실행 성공만으로 E2E 테스트가 통과한 것은 아니다.

1.3.0부터 설정된 실행 프로필 하나에 테스트 전용 임시 PostgreSQL을 연결할 수 있다.
일반 `argv` 실행이나 `test_database`가 없는 프로필은 데이터베이스를 시작하지 않는다.
PostgreSQL 설치 경로는 레포 밖의 전용 디렉터리여야 하고 `postgres`, `initdb`, `pg_ctl`,
`createdb` 실행 파일과 `postgres.bki` 리소스를 포함해야 한다. 이 경로는
`/opt/postgresql`에 읽기 전용으로 연결된다.

DB 프로필은 격리된 네트워크 안의 `127.0.0.1:5432`에 일회성 클러스터를 만들고,
허용된 `tarororo_test` 또는 `tarororo_ci` 데이터베이스만 생성한다. 무작위 비밀번호는
MCP 래퍼와 작업 메타데이터에서 출력하거나 저장하지 않고 해당 자식 명령의
`TEST_DATABASE_URL`에만 전달한다. 자식 명령도 이 URL이나 비밀번호를 출력하지 않아야 한다.
정상 종료와 일반 실패에서는 `pg_ctl`로 PostgreSQL을 중지한 뒤 임시 데이터를 제거한다.
취소와 시간초과에서는 작업의 PID·마운트 네임스페이스를 종료하여 PostgreSQL 프로세스와
`/tmp` 데이터를 함께 폐기한다. 호스트 DB, 외부 DB, 다른 작업의 DB에는 연결하지 않는다.

```json
{
  "version": 1,
  "toolchains": {"tossApp": {
    "node": "/home/jhw/.nvm/versions/node/v24.12.0",
    "chromium": "/home/jhw/.cache/ms-playwright/chromium-1208/chrome-linux64",
    "postgresql": "/home/jhw/.local/lib/chatgpt-coding-mcp/toolchains/postgresql-18.6"
  }},
  "profiles": {"tossApp": {"server:test:integration": {
    "argv": ["npm", "run", "test:integration"],
    "relative_cwd": "server",
    "timeout_seconds": 900,
    "test_database": "tarororo_test"
  }}}
}
```

`TEST_DATABASE_URL`이나 PostgreSQL 비밀번호는 설정 파일에 적지 않는다. 통합 테스트용
스키마 생성과 마이그레이션은 위 프로필의 자식 명령 안에서 수행한다.

```json
{"version": 1, "profiles": {"tossApp": {"unit": {
  "argv": ["npm", "run", "test:unit"], "relative_cwd": "ai-tarot", "timeout_seconds": 300
}}}}
```

## 허용할 저장소

WSL 새 설치의 기본 목록은 `personal-ops`, `pim-check`, `tossApp`이다.
`bootstrap.py --project NAME=ABSOLUTE_PATH`를 반복하면 원하는 프로젝트 목록 전체를
선택할 수 있으며, 특정 프로젝트 이름은 필수가 아니다. 설치 후에는 기존 목록을 보존해
항목을 추가한다. [WSL 프로젝트 추가 절차](./WSL-SETUP.md#7-add-projects-after-installation)와
[tossApp 환경 준비](./WSL-SETUP.md#6-prepare-tossapp-dependencies-on-the-wsl-pc)를 참고한다.

기본 설정은 `~/.config/chatgpt-coding-mcp/projects.json`이다.
사용자가 허용한 레포만 명시적으로 등록한다. 폴더 안의 레포를 자동 추가하지 않는다.

```json
{
  "projects": {
    "personal-ops": "/home/jhw/ai/opencode/projects/personal-ops",
    "other-repo": "/absolute/path/to/other-repo"
  }
}
```

위 두 번째 경로는 예시이며 실제 디렉터리로 교체해야 한다.
호스트에서 목록을 변경한 뒤 터널 런타임을 재시작하고 ChatGPT 플러그인의 도구 목록을
새로고침한다. 터널과 플러그인을 다시 만들 필요는 없다.

ChatGPT에는 `personal-ops의 README.md를 읽어줘`처럼 작업 레포를 직접 지정할 수 있다.
허용 목록만 확인하려면 `list_projects로 목록을 확인해줘`라고 요청한다. 이 목록 조회는
레포 선택 카드를 다시 열지 않는다. 실제 읽기 도구 인자는 다음과 같다.

```json
{"project": "personal-ops", "relative_path": "README.md"}
```

## Git 워크플로우

`git_status`와 `git_diff`로 변경을 조회한다. diff는 환경 비밀 파일을 제외하고
외부 diff·textconv를 차단한다. `prepare_git_operation`으로 계획을 만들고
`get_git_operation`으로 상태를 확인한다. `execute_git_operation`은 commit·push만 실행한다.

commit은 파일을 각각 지정하고 기존 stage가 비어 있어야 한다. 메시지는 설치된
Change Evidence Contract v1 검증기를 통과해야 하며 실제 GitHub Issue/PR 참조를 확인한다.
검증하지 않은 결과를 메시지에 작성하지 않는다. 승인한 파일 내용·실행 비트, HEAD,
변경 상태와 실제 stage 결과를 대조한다. 실패하면 선택 파일의 stage를 복원한다.
commit hooks와 로컬 설정의 외부 실행은 허용하지 않는다.

push는 현재 브랜치와 새 원격 브랜치 여부를 지정한다. 실제 `origin` push URL은
`jhw7500` 소유 GitHub HTTPS/SSH만 허용하고 원격 HEAD를 확인한다. `main`·`master`
직접 push, force push, 태그·브랜치 삭제는 제공하지 않는다. URL 재작성·외부 credential
helper 등의 저장소 설정이 있으면 거부한다. SSH 키/에이전트는 전용 Git 경로에서만 사용한다.
HTTPS 인증은 고정된 호스트 GitHub CLI helper와 기존 로그인을 사용한다.

계획의 `approval_command`를 **호스트의 실제 터미널**에서 실행하면 대상, diff,
commit 메시지를 보고 작업 ID를 직접 입력해 한 번 승인할 수 있다. 이후 ChatGPT에서
`execute_git_operation`을 호출한다. 영수증은 허용 레포 밖에 `0600`으로 저장하고 사용하면
소모한다. 변경 내용이나 HEAD가 달라지면 다시 준비해야 한다. 모델이 작성한 승인 플래그나
도구 표시는 이 승인을 대신하지 않는다.

PR은 제목·본문·base를, merge는 정확한 PR URL을 받아 비공개 핸드오프 자료를 만든다.
결과는 `waiting_native_workflow` / `NATIVE_WORKFLOW_REQUIRED`이며 완료가 아니다.
네이티브 Codex에서 `jhw-pr` 본문 검증과 `pre-pr-tribunal` 독립 리뷰를 통과한 뒤
정해진 생성 절차를 사용해야 한다. MCP는 이 리뷰를 대신하거나 직접 생성·merge하지 않는다.

메시지·제목·본문은 각각 최대 64 KiB, 경로는 최대 128개, 작업 기록은 레포당 1000개이다.
선택 파일·미리보기·Git 출력은 512 KiB로 제한하고 각 하위 명령은 시간 제한을 둔다.
MCP의 내부 잠금은 같은 UID 외부 프로세스의 Git index 변경까지 완전히 막지는 못한다.

## UI에서 작업 레포 선택

여기서 레포는 **호스트 PC에 있는 로컬 디렉터리**다. GitHub URL이 아니다.
GitHub 레포를 clone한 디렉터리를 등록할 수 있으며, 파일 편집만으로 원격에
반영되지는 않는다. 원격 반영에는 위 Git 워크플로우의 별도 승인·실행이 필요하다.

1. UI를 추가한 서버로 재연결한 뒤 ChatGPT 플러그인 상세 화면에서 도구를 새로고침한다.
2. 일반 ChatGPT 채팅에서 플러그인을 선택하고 처음 한 번
   **레포 선택 화면을 열어줘**라고 요청한다. `open_project_picker` 도구의 결과로
   **작업 레포** 카드가 대화 안에 표시된다.
3. 드롭다운에서 허용된 레포를 고르고 **이 대화에서 사용**을 누른다.
4. 선택이 전달되었다는 메시지를 확인한 뒤 채팅에서 작업을 요청한다.
   레포를 바꿀 때는 같은 카드에서 다시 선택한다. 터널 재시작은 필요 없다.

일반 Chat의 기본 설정에 레포 선택 메뉴를 추가하는 기능은 아니다. 처음 카드를
표시하려면 `open_project_picker` 호출이 필요하며, 이후 선택은 카드 UI에서 처리한다.
Work 전용 대화 탭·사이드바 진입점은 사용하지 않는다.

한 번 선택한 뒤에는 해당 대화의 선택 정보를 계속 사용한다. 파일 작업 도중
`list_projects`로 허용 목록을 확인해도 선택 카드를 새로 열지 않으며, 모델은 매 호출에
같은 `project` 값을 명시한다. 선택 화면은 사용자가 화면을 열어 달라고 명시적으로
요청했을 때만 다시 연다. 다른 레포 이름을 직접 지정하면 선택창 없이 그 레포를 사용한다.
다른 대화의 선택을 가져오거나 서버 전역 선택으로 저장하지 않는다.

이 UI는 서버의 전역 작업 디렉터리를 변경하지 않는다. 서버가 선택을 검증한 뒤
`ui/update-model-context`로 해당 UI가 속한 대화에 선택을 전달한다.
해당 기능이 없는 ChatGPT 호스트에서는 `setWidgetState`의 `modelContent`로
선택을 전달한다. 표준 UI 초기화를 지원하지 않으면 제공되는 `window.openai`
호환 브리지의 `toolOutput`·`callTool`을 사용한다. 제품명이 아닌 기능 지원 여부로 분기한다.
모델은 그 정보를 파일 도구의 `project` 인자로 사용하며, 서버는 매 호출마다
허용 목록과 경로를 독립적으로 검사한다. 선택 정보는 접근 권한을 부여하는 토큰이 아니다.

`select_project`는 UI 전용 읽기 도구이고, 허용 목록 추가·삭제 기능은 없다.
파일 도구 6개는 `model` 전용으로 선언하여 선택 UI에서 직접 호출할 수 없게 한다.
UI를 통해 새로운 경로를 자동 허용하지 않는다. 허용 목록은 위 호스트 설정에서 관리한다.
**목록 새로고침**은 실행 중인 서버의 목록을 다시 읽는다. 설정 파일 변경은 런타임
재시작 후 반영된다.

위젯 상태 보존을 지원하는 호스트에서는 이전 선택을 다시 표시할 수 있으나,
영구 저장이나 다른 대화·기기와의 동기화를 보장하지 않는다. 카드를 다시 열었을 때는
선택을 확인하고 적용한다. 선택 전달 방식을 모두 지원하지 않는 화면에서는 적용 버튼을
비활성화하고 채팅에 레포 이름을 직접 지정하도록 안내한다.

UI는 단일 HTML MCP 리소스이며 외부 스크립트·서버·새 패키지를 사용하지 않는다.
실제 ChatGPT 화면의 노출은 계정과 클라이언트에서 추가 확인해야 한다.

[OpenAI UI 문서](https://developers.openai.com/plugins/build/chatgpt-ui) ·
[확장 진입점 문서](https://developers.openai.com/plugins/build/extensions)

### 플러그인 설정 화면 지원 여부 확인

서버 1.1.3은 `initialize.capabilities.experimental["openai/settings"]`에
설정 조회·업데이트 도구를 선언한다. 설정 지원 호스트에서는 플러그인 상세 화면에
**로컬 레포 연결 확인** 그룹의 **연결 확인: connected** 항목과
**설정 UI 지원 확인** 버튼이 표시될 수 있다. 연결 확인 값은 단일 선택지로 고정되어 있다.
이 버튼은 연결 성공과 등록 레포 수만 반환한다. 파일·허용 목록·대화의 작업 레포를
변경하지 않으며, 설정 업데이트는 빈 변경 또는 같은 고정 값만 허용한다.
새 필드나 다른 값을 보내면 거부한다.

실제 클라이언트에서 1.1.2의 설정 조회 도구가 호출됐지만 버튼은 보이지 않았다는
사용자 확인이 있었다. 1.1.3의 고정 필드는 빈 속성 목록이 표시 여부에 영향을 주는지
구분하기 위한 진단이며, 화면 표시 문제의 해결이 확인된 것은 아니다.

2026-10-08 확인 결과: 사용자는 ChatGPT 웹과 데스크톱 앱 모두에서 버튼이 보이지
않는다고 보고했다. 1.1.3 반영 후에도 항목과 버튼이 모두 보이지 않는다고 확인했다.
서버 로그에는 1.1.3 초기화와 도구·UI 리소스 조회가 남았지만, 이 재확인 구간의
설정 조회 호출은 관찰되지 않았다. 따라서 새 설정 값이 클라이언트에 전달됐다고
단정하거나, 제품 전체가 설정 확장을 지원하지 않는다고 일반화하지 않는다.
당시 1.1.3에서 실제 표시가 확인된 레포 선택 경로는 일반 채팅에서
`list_projects`를 호출해 여는 인라인 카드였다. 1.1.4부터 목록 조회와 UI 열기를
분리하여 `open_project_picker`가 카드를 열고 `list_projects`는 목록만 반환한다.
플러그인 상세/설정에서 여는 경로는 사용 가능하다고 안내하지 않는다.

이는 일반 Chat의 설정 UI 지원 여부를 실제 계정에서 확인하기 위한 진단 기능이다.
버튼이 표시된다는 사실만으로 설정 화면에서 선택한 레포가 대화에 적용된다고
간주하지 않는다. 설정 화면 노출과 호출 여부는 실제 클라이언트 확인이 필요하다.
서버 로그의 `repository-ui` 이벤트는 도구 이름과 리소스 조회 여부만 기록하며
파일 내용이나 도구 인자를 기록하지 않는다.

## 작업 진행 표시

1.1.5부터 파일 도구에 한국어 표시 이름과 OpenAI의
`openai/toolInvocation/invoking`·`invoked` 상태 문구를 제공한다.
지원하는 클라이언트에서는 파일 읽기·디렉터리 조회·검색·저장·수정을 구분할 수 있다.
완료 문구는 도구 응답이 도착했다는 뜻이며 작업 성공을 보장하지 않는다.
오류 여부와 실제 변경 내용은 도구 결과를 확인해야 한다.

서버 지침에는 여러 단계의 작업을 시작하기 전 레포와 다음 작업을 안내하고,
주요 단계가 끝나면 실제 확인·수정한 파일과 결과를 간단히 설명하도록 명시한다.
마지막에는 완료 내용, 오류, 검증 여부를 일반 텍스트로 정리하도록 요청한다.
이는 모델에 제공하는 지침이므로 모든 중간 메시지의 생성이나 표시를 보장하지 않는다.

상태 문구는 도구별 고정 메타데이터이며 파일명이나 진행률을 실시간으로 전송하는
기능이 아니다. ChatGPT의 전체 진행 제목이나 Figma·PDF 등 다른 플러그인의 내부
작업도 이 서버가 제어하지 않는다. 별도의 누적 작업 카드와 진행률 알림은 제공하지 않는다.
실제 표시 형태는 ChatGPT 클라이언트의 지원에 따라 달라진다.

[OpenAI 도구 상태 메타데이터](https://developers.openai.com/plugins/reference#_meta-fields-on-tool-descriptor)

## tossApp DB 006~012 전용 마이그레이션

2026-10-13: 기존 013 전용 HostRuntime과 별도로 006~012 전용 읽기·계획·실행
경로를 구현했다. 아래 범위 활성화·Git HEAD 조건은 고정 legacy 설정의 계약이다.
등록형 v3의 `reviewed_snapshot` 흐름은 하단의 단순화 설명을 따른다.
현재 ChatGPT 플러그인의 실행 중 도구 목록에는 소스 변경이 자동 반영되지 않는다.
런타임 설치 이력은 `docs/handoffs/2026-10-13-chat-host-operations-stage1.md`에서 확인한다.

- `get_predecessor_migration_status`: 001~013 이름/파일 SHA-256의
  `missing | checksum_match | checksum_mismatch`, 14개 선행 테이블과
  충돌 후보 객체의 존재 여부만 `BEGIN READ ONLY`로 진단한다.
  사용자·주문/기타 업무 행은 조회하지 않는다.
- `prepare_predecessor_migration`: 001~005 정확한 체크섬, 006~013 모두
  미적용, 기존 006~012 객체 충돌 없음, 추가 이력 없음, DB 세대와 소스 고정,
  검토된 high-risk 입력이라는 조건을 충족할 때만 7일 유효 계획을 만든다.
  백업·DDL·재시작·서비스 배포를 수행하지 않는다.
- `execute_predecessor_migration`: 기본 작업별 승인 정책에서는 **별도 로컬 관리자 인증 후 작업 ID
  입력으로 발급받은 1회 승인 영수증**이 있어야 실행할 수 있다.
  고정 테스트 DB에서 백업 → 일회용 DB 복원 검증(기존 이력 5개)
  → 잠금 및 이력/객체/DB 세대 재검증 → 006~012 단일 트랜잭션
  → 결과 검증을 수행한다. 실패 시 자동 복원·재시도하지 않으며 백업을 보존한다.
- `execute_db_migration`: 기본 정책에서는 **013 전용 별도 계획·별도 승인**을 요구한다.
  아래의 대상별 사전 허용 정책에서는 006~013 통합 계획도 실행한다.
  `execute_test_deploy`는 API/Worker로 제한한다. 도구 간 작업 ID 혼용을
  `WRONG_OPERATION_TYPE`으로 거부한다.

tossApp의 `server/src/db/migrate-predecessors.ts`는 전용 Node 진입점이다.
일반 `npm run db:migrate`는 013까지 시도할 수 있으므로 선행 적용에 사용하면 안 된다.
호스트는 변경 가능한 Node/Compose 스크립트를 실행하지 않고, 검토된 SQL 파일의
SHA를 고정한 불변 호스트 실행기를 사용한다. **006~012 SQL 7개는 전용
고위험 검증 대상**이다. 고정 legacy/`git_head` 정책에서는 Git HEAD에 실제로
추적되고 내용이 일치해야 한다. 등록형 `reviewed_snapshot`에서는 미추적 SQL도
개인 스냅샷에 고정하고 관리 웹에서 그 바이트와 변경 자료를 검토·승인한다.
다른 세션의 미커밋 소스를 강제로 stage/commit/reset하는 행위는 금지한다.
고정 legacy의 기본 v2 권한(Worker·013)으로 006~012를 활성화하지 않는다. 고정 v3 권한은 관리 화면의
별도 `ENABLE DB 006-012`과 관리자 재인증 후에만 활성화되며,
v3 활성화 자체로 DB를 변경하지 않는다. 실제 적용은 또 다른 단일 작업 승인이다.
백업을 임시 DB에 복원할 때는 001~005 이력의 개수뿐 아니라 이름·SHA-256까지
일치해야 한다. 승인 화면은 각 006~012 SQL 체크섬과 DB 세대를 표시한다.
고정 legacy/`git_head`에서는 소스가 미커밋이거나 고위험 입력이 변경 중이면
계획 생성이 거부된다. 등록형 `reviewed_snapshot`에서는 계획을 만들 수 있지만,
실제 실행은 기본적으로 관리 웹 또는 호스트 TTY의 별도 단일 작업 승인이 필요하다.
대상별 사전 허용을 켠 경우에는 아래의 정책을 따른다.
커밋/설치/터널 갱신은 별도 승인과 검증이 필요하다.

검증: tossApp 일회용 PostgreSQL 전체 통합 **113/113**(종료 0),
서버 단위 **204/204**(종료 0) 및 타입 검사 통과.
HostRuntime·승인·백업 모의 테스트 **55/55**(종료 0) 통과.
JS 정적 검사는 격리 시스템의 오래된 Node가 기존 `??` 문법을 파싱하지 못해 미검증이다.
MCP 라우터 실제 stdio 통합 검증은 격리 Python에 `mcp` 패키지가 없어 미실행이다.
실호스트 DB의 001~005/006~012 실제 적용·동작 검증과 불변 런타임
도구 목록/웹 승인 UI 확인은 미실행이다.

## 터널 연결

1. [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels)에서
   터널을 만들고 사용할 ChatGPT 워크스페이스를 연결한다.
2. Tunnels Read + Use 권한이 있는 런타임 API 키를 발급한다.
   Admin 키를 런타임에 사용하지 않는다.
3. 키를 채팅이나 명령 인자에 붙여넣지 않고 호스트에서 저장한다.

   ```bash
   python3 chatgpt-mcp/save-runtime-key.py
   ```

4. 실제 터널 ID를 사용하여 관리되는 로컬 런타임을 연결한다.

   ```bash
   tunnel-client runtimes connect \
     --alias personal-ops-coding \
     --tunnel-id YOUR_TUNNEL_ID \
     --runtime-api-key "file:$HOME/.config/chatgpt-coding-mcp/runtime-key" \
     --mcp-command "python3 /ABSOLUTE/RUNTIME/server.py"
   tunnel-client runtimes status personal-ops-coding --json
   ```

   `/ABSOLUTE/RUNTIME/server.py`는 `install-runtime.py`가 반환한 `server` 경로로
   교체한다. 작업 대상은 허용 목록과 각 호출의 `project` 인자로 지정한다. 경로에 공백이 있으면
   `--mcp-command` 내부 경로도 셸 인용한다.
   `process_running`, `healthy`, `ready`를 확인한 뒤 ChatGPT에 등록한다.
   API 키나 터널 ID가 없는 상태에서는 연결을 시작하지 않는다.

5. ChatGPT 웹에서 `Plugins > + > Add custom MCP server > Connection: Tunnel`로
   등록하고 해당 터널을 선택한다. 이 stdio MCP는 별도 OAuth를 제공하지 않는다.
   생성·설치 후 채팅에서 플러그인을 선택해 README 읽기부터 확인한다.

터널이 실행 중이어야 플러그인 등록과 도구 호출이 가능하다.
SSH 종료 후 생존 여부와 재부팅 후 자동 시작은 실제 연결 시 각각 검증한다.
클라이언트 설치 파일은 `~/.local/share/openai-tunnel/`에 있고,
`~/.local/bin/tunnel-client`가 해당 실행 파일을 가리킨다.
릴리스·다운로드 URL·검증한 SHA-256은
`~/.local/share/openai-tunnel/<version>/release.json`에 기록된다.

[공식 연결 문서](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 로컬 관리 화면과 레포 메모리

관리 화면은 ChatGPT 플러그인 설정에 삽입되는 UI가 아닌 **호스트의 별도 웹 페이지**다.
작업 보고·실행 상태·종료 코드·실제 출력과 MCP 호출 타임라인을 2초마다 갱신한다.
Git 변경 목록과 선택한 파일의 diff는 10초 간격 또는 수동으로 조회한다.
`자동 갱신 일시정지`와 수동 새로고침을 사용할 수 있다. 레포 선택은 화면 조회에만 적용되므로,
채팅에는 계속 `tossApp 프로젝트 진행`처럼 작업 대상을 지정한다.

화면은 다음 내용을 우선 표시한다.

상단의 **업무 작업실**은 분석·코딩·실행·검사·검토·반환/종료·확인 필요 방에
실제 MCP 요청과 명령 작업을 픽셀 인물로 배치한다. 문서 가방은 MCP 호출, 공구 가방은 명령이다.
같은 요청·작업은 ID를 유지한 채 출입문과 공용 복도를 따라 이동한다. 인물을 누르면
상태·시각·ID를 볼 수 있고 명령은 원본 출력으로 연결된다. 방의 개수로 숨겨진 표시 기록을 조회한다.
확대·축소·전체 보기·지도 이동과 움직임 일시정지, 시스템의 동작 감소 설정을 지원한다.
모바일에서는 최근 작업이 있는 방을 먼저 보여준다. 기존 자동 갱신을 멈추면 이동도 멈춘다.

조회 사이에 시작·종료가 모두 기록된 짧은 호출도 이동을 확인할 수 있도록 **관측 기록 재생**으로
표시한다. 최초 접속의 과거 기록은 정지 상태로 배치하고, **최근 활동 재생**을 누르면 확인한
시작·종료 기록의 이동만 다시 보여준다. 명령을 다시 실행하지 않으며 관측 상태·성공 여부도 바꾸지 않는다.
일시적인 조회 실패에는 마지막 확인 기록을 유지하고 관측 지연과 함께 움직임을 멈춘다.

객체는 별도로 실행한 AI 에이전트가 아니다. ChatGPT 모델·내부 추론과 터널의 연결 상태는
이 화면에 전달되지 않으므로 **미수신**으로 표시한다. 요청 반환과 명령 성공을 구분하며,
시작만 기록되고 2분 이상 종료를 확인하지 못한 요청은 종료 상태 미확인으로 표시한다.
그림은 기존 jobs/activity API의 최근 기록을 이용하므로 여러 채팅의 업무를 자동으로 나누지는 않는다.
화면 모듈은 저장소의 `work-dashboard/`에 분리했고 설치 시 같은 비공개 런타임 스냅샷에 포함한다.

| 영역 | 표시하는 근거 | 사용 방법 |
|---|---|---|
| 현재 작업 | 채팅 또는 사용자가 저장한 목표·상태·완료 항목·다음 작업 | 채팅에서 작업 보고를 기록하거나 화면에서 직접 편집 |
| 변경 파일과 변경 내용 | 호스트의 현재 Git 작업 트리·스테이징 영역 | 파일을 선택하고 변경 범위를 전환 |
| 빌드·테스트·검사 | 이름에 test/typecheck/lint/build/e2e/check 등이 있는 최근 실행 기록 | 상태·종료 코드 확인, 클릭해서 원본 출력 조회 |
| 새 채팅에서 이어가기 | 저장된 작업 보고·메모와 현재 변경 파일·최근 명령 결과 | 요약 만들기 → 복사 → 새 채팅에서 플러그인 선택 후 붙여넣기 |

직접 명령처럼 검사 이름이 없는 실행도 아래 **전체 실행 작업과 출력**에서 확인한다.
최근 레포 선택은 브라우저에 레포 이름만 저장한다. 저장된 선택이 없으면 최근 작업 보고·실행·호출이
있는 레포를 우선 선택한다. 이 선택이 다른 채팅의 작업 대상을 변경하지는 않는다.
작업 보고의 완료 표시는 기록한 사람의 보고다. 명령 성공은 별도의 상태와 종료 코드로 판단한다.
기존 채팅에서 저장하지 않은 목표는 자동 추정하지 않으므로 처음에는 기록 없음으로 표시할 수 있다.
작업 보고가 없어도 상단의 **실제 명령 실행 현황**에는 최근 실행의 상태별 개수와 명령 5개가 자동으로 표시된다.
항목을 누르면 전체 실행 영역에서 원본 출력을 확인할 수 있다. 일반 SSH 셸에서 직접 실행한 명령은
터널 실행 작업(Jobs)에 자동으로 포함되지 않는다. 등록된 Codex CLI의 도구 호출은 아래의 별도
활동 수집 경로로 표시하며 원본 명령·출력은 제공하지 않는다. 관찰 기록이 작업 목표나 기능 완료
보고를 대신하지는 않는다.

현재 채팅에 다음과 같이 요청하면 새 도구를 통해 기록할 수 있다.

```text
tossApp의 현재 목표와 완료 항목, 다음 작업을 작업 보고에 기록하고, 작업 단계마다 갱신해줘.
```

Git 변경에는 다른 터미널·IDE에서 만든 변경도 포함된다. 조회는 index 갱신이나 Git 변경 명령을 실행하지 않는다.
`.env`·`.env.*`·`.git` 경로와 심볼릭 링크는 제외하며, 선택한 파일만 최대 64 KiB로 미리보기 한다.
바이너리 파일·목록/미리보기 한도 초과·Git 조회 실패는 별도로 표시한다. 이 제외 규칙이 다른 파일에
적힌 모든 비밀값까지 찾아 제거하는 것은 아니므로 요약에는 비밀값을 저장하지 않는다.
이어가기 요약에는 원본 명령 인자·로그·diff를 넣지 않는다. 생성 시점의 저장본이므로 미저장 초안은 포함하지 않는다.
요약을 만들기만 해도 기존 메모가 바뀌지는 않는다. HTTPS 복사가 지원되지 않는 LAN HTTP에서는
선택된 텍스트를 복사할 수 있도록 대체 경로를 제공한다.

설치된 런타임 경로를 사용해 호스트에서 별도 실행한다.

```bash
~/.local/share/uv/tools/serena-agent/bin/python /ABSOLUTE/RUNTIME/admin_server.py
```

SSH로 접속하는 로컬 PC의 터미널에서 동일한 포트로 포워딩한다.

```bash
ssh -N -L 8765:127.0.0.1:8765 jhw@HOST
```

호스트 터미널에서 로그인 URL을 확인하고 로컬 PC 브라우저에서 연다.
URL에는 관리 토큰이 있으므로 채팅·이슈·로그에 붙여넣지 않는다.

```bash
~/.local/share/uv/tools/serena-agent/bin/python /ABSOLUTE/RUNTIME/admin_server.py --print-login-url
```

현재 호스트에는 같은 런타임을 호출하는 `~/.local/bin/personal-ops-admin` 명령도 설치되어 있다.
`~/.local/bin/personal-ops-admin --print-login-url`로 로그인 URL을 확인할 수 있다.

관리 토큰은 `~/.config/chatgpt-coding-mcp/admin-token`(0600)에 자동 생성되며
OpenAI API 키와 별개다. 페이지는 URL fragment를 즉시 제거한 뒤 토큰을 서명된 브라우저 세션으로
교환하고 원본 토큰을 메모리에서 비운다. 세션은 HttpOnly·SameSite=Strict 쿠키로 저장한다.
`이 브라우저에서 30일 유지`가 기본 선택되어 있으며, 해제하면 브라우저 세션 동안(서버 만료 24시간)
유지한다. 연결 해제 시 해당 브라우저 쿠키를 지운다. 원본 토큰 교체 후 서비스 재시작은 모든 기존
세션을 무효화한다. 세션 쿠키는 원본 관리 토큰을 포함하지 않는다.
API에는 유효한 토큰 또는 세션이 필요하고, 허용하지 않은 Host/Origin 및 CORS 접근을 거부한다.
기본 리슨 주소는 `127.0.0.1`이다. 다른 포트를 원하면 서버의 `--port`와 SSH 양쪽 포트를 모두 맞춘다.

### PC에서 Tailscale 없이 LAN 직접 접속

사용자 요청으로 현재 호스트에는 `--lan-host 192.168.2.70`을 추가했다.
관리 서버는 `127.0.0.1:8765`와 `192.168.2.70:8765`의 개별 소켓에서만 접속을 받는다.
와일드카드/공인 IP 바인딩은 허용하지 않는다. 휴대폰용 Tailscale HTTPS 경로도 유지한다.

PC의 고정 주소는 `http://192.168.2.70:8765/`다. 처음 한 번 호스트에서 다음 명령을 실행하고,
출력된 로그인 주소를 PC 브라우저에서 연다. 이후 토큰 없는 고정 주소를 즐겨찾기에 저장한다.

```bash
~/.local/bin/personal-ops-admin --print-lan-login-url
```

LAN 경로는 HTTP로, 로그인 정보와 출력이 TLS로 암호화되지는 않는다. 신뢰하는 로컬망에서만 사용한다.
인증·Host/Origin 검사는 계속 적용되며, LAN 인터페이스에서는 해당 LAN origin만 허용한다.
휴대폰용 HTTPS 로그인 주소는 기존 `--print-login-url`로 확인한다. 두 주소의 브라우저 로그인은 별개다.
호스트의 LAN IP가 바뀌면 서비스와 helper의 `--lan-host`도 새 고정 IP로 변경해야 한다.

### Tailscale로 접속

같은 tailnet에 연결된 PC와 휴대폰은 SSH 포워딩 대신 Tailscale Serve의 HTTPS 주소를 사용한다.
관리 서비스에는 정확한 HTTPS origin만 `--public-origin`으로 등록한다. 예:

```bash
python3 /ABSOLUTE/RUNTIME/admin_server.py \
  --public-origin https://cantopsbuildserver.tail661d29.ts.net:8443
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8765
```

현재 호스트의 관리 서비스와 `personal-ops-admin` helper에는 위 origin이 반영되어 있다.
**2026-10-09: 사용자가 Serve 활성화를 완료했다.** 호스트에서 실제 HTTPS 페이지·로그인·
새로고침 후 세션 유지까지 검증했다. 기존 443번 서비스는 유지된다.
Funnel 활성화는 이 설정에 포함하지 않으며, Serve는 tailnet 내부 접속이다.

첫 접속에는 `~/.local/bin/personal-ops-admin --print-login-url`의 HTTPS 로그인 URL을 사용한다.
이후 각 브라우저에서 `https://cantopsbuildserver.tail661d29.ts.net:8443/`를 즐겨찾기/홈 화면에 추가한다.
로그인 URL에는 비밀 토큰이 있으므로 공유하지 말고, 토큰 없는 고정 주소만 즐겨찾기에 저장한다.
HTTPS origin에서 발급하는 쿠키에는 Secure도 적용한다. 쿠키는 포트별로 격리되지 않으므로 같은
호스트명의 다른 서비스도 같은 신뢰 범위여야 한다. 비신뢰 서비스를 함께 운영한다면 호스트명을 분리한다.

[공식 Tailscale Serve 문서](https://tailscale.com/docs/reference/tailscale-cli/serve)에 HTTPS와
`--bg` 재부팅 지속 동작이 설명되어 있다.

사용자 서비스 `personal-ops-admin.service`로 설치한 환경에서는 다음 명령으로 확인한다.
서비스의 ExecStart에는 배포 시 검증한 불변 런타임 경로를 넣는다.

```bash
systemctl --user status personal-ops-admin.service
systemctl --user restart personal-ops-admin.service
```

관리 화면만 재시작해도 MCP 작업은 중단되지 않는다. 로그인 이후 서비스 자동 시작은 enabled 상태에
따르며, 로그인 전 부팅 자동 시작은 사용자 lingering 설정에도 달려 있다. 소스 수정만으로 배포된
스냅샷이 바뀌지 않으므로 업데이트 시 런타임 설치와 서비스 경로 갱신이 필요하다.

메모리는 허용된 **호스트 로컬 디렉터리의 정규 경로**별로 저장된다. GitHub 저장소 이름으로
자동 동기화하지 않는다. 관리 화면과 MCP가 같은 상태 저장소를 사용한다.

| MCP 도구 | 인자 | 동작 |
|---|---|---|
| `read_project_memory` | `project` | 현재 메모·revision·저장 시각 읽기 |
| `update_project_memory` | `project`, `content`, `expected_revision` | 읽었던 revision과 일치할 때 저장 |
| `read_project_progress` | `project` | 저장된 작업 보고·revision·저장 시각 읽기 |
| `update_project_progress` | `project`, `title`, `status`, `summary`, `completed`, `next_steps`, `expected_revision` | 읽었던 revision과 일치할 때 작업 보고 저장 |
| `report_work_events` | `project`, `events` (1~20개) | 다른 MCP·에이전트의 상태 메타데이터를 `보고됨`으로 저장 |

메모는 UTF-8 최대 32 KiB이며 빈 문자열을 저장하면 내용을 비운다. 다른 채팅이나 화면에서 먼저
저장하면 덮어쓰지 않고 충돌을 반환한다. 입력 중인 내용은 화면에 남으므로 복사해 두고 최신 저장본과
합친다. 새 채팅에서는 `tossApp의 저장된 프로젝트 메모리를 읽고 이어서 진행해줘`로 이어갈 수 있다.
서버 지침도 작업 시작 시 메모를 읽도록 안내하지만 **전체 대화나 미저장 진행 상황이 자동 복구되는 것은 아니다**.
메모의 지시는 상위 지침이나 사용자 승인을 대체하지 않는다. 별도 모델 호출은 없다.

작업 보고도 레포 정규 경로별로 저장하며 충돌 시 입력을 보존한다. 상태는 planned/in_progress/blocked/completed이며,
목표는 1~200 UTF-8 bytes, 상황 설명은 최대 4096 bytes, 완료·다음 작업 목록은 각각 최대 20개·항목당 1000 bytes다.
새 채팅은 작업 보고와 메모를 함께 읽어야 한다. 기존 채팅에서는 새 MCP 도구가 나타나도록 플러그인 도구 목록을
새로고침하거나 새 채팅을 열어야 할 수 있다. 서버 지침은 시작 및 의미 있는 작업 단계에서 기록하도록 안내하지만
ChatGPT 클라이언트의 모든 응답이 이를 실행한다고 보장하지 않는다.

상태는 `${XDG_STATE_HOME:-~/.local/state}/chatgpt-coding-mcp/`에 저장된다.
`project-memory/`는 메모, `project-progress/`는 작업 보고, `project-state-locks/`는 프로세스 간 잠금,
`activity.jsonl`과 `activity.previous.jsonl`은 최근 호출 메타데이터다.
`work-events.jsonl`과 `work-events.previous.jsonl`은 공통 v1 이벤트이며 같은 0600·소유자·심볼릭 링크
검사를 적용한다. 각각 약 1 MiB로 순환하고 `/api/activity`는 프로젝트별 최근 200개 `records`를 함께 반환한다.
활동 파일은 각각 최대 약 1 MiB로 순환한다. 호출 인자·파일 본문·환경 변수·예외 원문은 기록하지 않는다.
명령 출력은 기존 job log에 저장되므로 실행한 프로그램이 출력한 비밀값까지 자동 제거하지는 않는다.
UI는 최근 100개 작업과 최근 호출만 표시하며, 개별 명령 로그는 기존 1 MiB 제한을 따른다.

화면에는 명령 실행·취소, Git 승인, 허용 레포 설정 변경 기능을 추가하지 않았다.
작업 실행은 기존 MCP와 승인 경로를 사용한다. 타임라인의 호출 완료는 `start_command` 등의
요청이 반환됐다는 뜻이며, 명령의 최종 성공 여부는 작업 상태와 종료 코드로 확인한다.
활동 저장 오류는 stderr에 일반 진단을 남기며 성공한 작업을 실패로 바꾸거나 재실행하지 않는다.

다른 MCP가 ChatGPT에서 직접 호출되면 호스트 터널에 자동 기록되지 않는다. 해당 채팅에서
`다른 MCP를 사용할 때 시작·반환·실패를 report_work_events로 보고해줘`라고 요청하면 서버별 객체를
표시할 수 있다. 시작과 종료에 같은 `source_id/actor_id/task_id`를 사용하고, MCP 반환은 `returned`,
에이전트 완료는 `succeeded`로 기록한다. 이 보고는 실행의 독립적인 검증을 대신하지 않는다.
파일 본문·도구 인자·결과·프롬프트·추론 내용은 전송하지 않는다. 모델·적용 추론 수준·토큰은 실제
제공된 값만 보내고 토큰에는 request/turn/session/task 집계 범위를 붙인다.

외부 수집기의 `POST /api/work-events?project=<허용 레포>`도 기존 토큰 또는 세션 인증과
Host/Origin 검사를 받는다. 본문은 `{"events":[...]}`이며 원격 입력의 출처는 항상 `reported`다.
활성 보고는 최대 120초 유효하고 종료 신호나 갱신이 없으면 미확인으로 표시한다.
명시적인 `reason` 이벤트에만 추론 동작을 표시한다. ChatGPT 모델·추론을 자동으로 읽는 기능은 없으며
CLI 후크의 활성 여부는 호스트 설정에 달려 있다. Codex 관리 세션 관측은 개별 스킬 호출을 포함하지 않는다.
형식과 확장 경계는
[업무 작업실 수집 계약](../work-dashboard/README.md#다른-mcp와-에이전트-수집)을 따른다.

### Codex 터미널 도구 관측

수집기는 기존 관리 서버의 로드된 세션 메타데이터를 조회하며 세션을 재개하거나 제어하지 않는다.
허용 프로젝트로 매핑된 세션의 `Thread.path`만 사용해 Codex sessions 아래의 해당 UUID 로그를
증분 관측한다. 최초 접속·파일 교체·재시작은 파일 끝에서 시작하므로 과거 업무를 새 활동으로
재생하지 않는다. 읽기 크기·행 크기·추적 세션·호출 수에 상한이 있고, 도구명·시각·상태·종료코드만
작업 이벤트로 저장한다. 프롬프트·명령·인자·출력·추론 본문을 저장하거나 화면으로 전송하지 않는다.

실제 `function_call`/`custom_tool_call`은 **요청됨**, 대응하는 반환은 **반환됨**이다.
요청 기록은 OS 명령이 실행 중이라는 증거가 아니다. `item_completed`의 명령·파일 변경·MCP
최종 상태는 별도로 표시하며, 완료 기록만으로 시작·실행 중 상태나 성공을 만들지 않는다.
개별 호출은 기존 Codex 캐릭터에 연결된다. 일반 세션 생존 신호보다 실제 대기·진행 호출을
우선 표시하고, 상세 화면의 **최근 터미널 활동**에서 도구·결과·종료코드와 작업 상세를 확인한다.
도구명에서 스킬 호출이나 숨겨진 사고 단계를 추론하지 않는다.

`--no-activity`로 실행하면 기존 세션 메타데이터만 관측한다. 이 소스의 도구 수집·화면 변경은
새 런타임과 관측기 서비스를 운영에 반영한 뒤 활성화된다. 현재 실행 중인 서비스나 Claude 후크
설정은 파일 편집만으로 변경되지 않는다.

### 스킬 호출 표시 (RPG Stage 2)

공통 v1 작업 이벤트에 선택 필드 `skill_id`와 `skill_name`을 추가한다.
`skill_id`는 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}` 형식이고,
`skill_name`은 출력 가능한 1~80자 표시명이다. 표시명만 보내는 것은 허용하지 않는다.
기존 이벤트에는 두 필드가 필요하지 않다. `task_kind: "call"`에만 허용하며,
일반 agent/job 성공을 스킬 완료로 표시하지 않는다. 스킬 경로·본문·인자·프롬프트는 수집하지 않는다.

한 호출의 시작과 반환에는 같은 `source_id/actor_id/task_id`를 사용하고,
다음 호출은 새로운 `task_id`를 사용한다. 예를 들어 기존 이벤트 필드에
`"tool_name":"Skill", "skill_id":"plugin:review", "skill_name":"코드 검토"`를 넣는다.
같은 호출의 후속 기록이 스킬 필드를 생략해도 화면은 앞선 메타데이터를 유지한다.
서로 다른 스킬 ID가 같은 호출 ID에 섞이면 스킬 표시는 생략한다.

| 입력 경로 | 스킬 식별 근거 | 화면의 출처 |
|---|---|---|
| Claude의 `Skill` 도구 후크 | 유효한 `tool_input.skill`만 추출 | 직접 관측 |
| ChatGPT·Codex 등의 `report_work_events` 또는 HTTP 보고 | 호출자가 명시한 `skill_id` | 보고됨 |
| Codex 관리 세션 관측 | 개별 스킬 호출 신호 없음 | 스킬 발동으로 표시하지 않음 |
| `SKILL.md` 읽기·프롬프트의 스킬 언급·도구명만 존재 | 호출 식별 근거 부족 | 스킬 발동으로 표시하지 않음 |

Claude의 `PreToolUse`는 호출 시작, `PostToolUse`는 호출 반환이다.
**반환은 스킬이 지시한 전체 업무가 완료됐다는 의미가 아니다.** 실패·중단은 해당 실패 후크로만
표시한다. 직접 입력한 `/skillname`은 `Skill` 도구 후크를 거치지 않을 수 있으므로 이 수집 범위에
포함하지 않는다. [Claude 후크 문서](https://code.claude.com/docs/en/hooks#userpromptexpansion)와
[스킬 권한의 입력 매개변수](https://code.claude.com/docs/en/skills#restrict-claudes-skill-access)를 참조한다.

캐릭터의 스킬 표식, 말풍선, 작업 요약에서 이름과 상태를 확인한다. 새 캐릭터를 만들지 않으며
같은 호출을 반복 조회해도 발동 효과를 재생하지 않는다. 초기 이력·숨김 복귀·기록 재생에는 새 발동
효과가 없고, 관측 지연·일시정지·동작 감소 설정을 따른다. 작업 목록에서 해당 호출을 선택하면
소유자·출처·호출 ID와 상태를 확인할 수 있다. 별도의 AI 호출은 발생하지 않는다.

이 소스 변경만으로 운영 서비스나 호스트 후크 설정은 바뀌지 않는다. MCP 입력 스키마와 부모 프레임
브리지가 바뀌므로 운영 반영 시 런타임 전환과 기존 호스트 권한의 재승인이 필요할 수 있다.

## Host Runtime Management — Toss 테스트 API 전용 (P0, 코드 구현)

설계 원본: [2026-10-12 Host Runtime Management 명세](../docs/superpowers/specs/2026-10-12-host-runtime-management.md).

**현재 저장소 코드만으로 실제 Docker가 변경되지는 않는다.** 새 MCP 도구 5개는
기존 격리 `start_command`와 독립된 호스트 관리 경로다.

| 도구 | 주요 동작 |
|---|---|
| `get_runtime_status` | tarororo-test의 API·Worker·DB 및 안전하게 분류된 설정·스키마 조회 |
| `compare_deployment` | 빌드 입력 digest·Git 상태·이미지 ID 비교; 증빙 없으면 `UNKNOWN` |
| `prepare_test_deploy` | 소스와 실제 Docker 대상 고정, 만료 7일의 승인 계획 생성 |
| `execute_test_deploy` | 호스트 **실제 TTY**에서 생성한 1회 영수증을 확인한 후 API만 빌드/재생성 |
| `get_host_operation` | 작업 단계·상태 조회; 중단/결과 불명은 `outcome_unknown` |

기본값은 **OFF**. 별도의 사용자 소유 일반 파일
`~/.config/chatgpt-coding-mcp/host-runtime.json`(0600)에 정확한 구성을 작성하고
`enabled=true`로 바꾼 뒤에만 도구가 호스트를 조회·관리한다. 실제 설정은
[명세 2.1](../docs/superpowers/specs/2026-10-12-host-runtime-management.md#21-구성-파일-제안)의
타깃을 그대로 사용한다. `project=tossApp`, `target=tarororo-test`만 지원한다.
`enabled=false`, 파일 부재/불일치, 심볼릭 링크, 다른 프로젝트 요청은 거절된다.
이 구성 파일은 저장소에 추가하지 않는다.

`prepare_test_deploy`는 **실제 소스 바이트·파일 모드·untracked 파일·Compose 정의와
현재 API/Worker/DB의 ID**를 고정하고, 쓰기 가능한 레포가 아닌 전용 0700
개인 작업 디렉터리에 Docker context를 복제한다. 일반 Git HEAD가 같다는 이유로
배포를 생략하거나 수정되지 않았다고 판정하지 않는다. 승인 후에는 원본과 복제본
두 벌의 fingerprint, Compose, 대상 컨테이너 ID·시작 시각을 다시 검사한다.
소스 변경은 `STALE_PLAN`으로 종료한다. Dockerfile·Compose·배포 관리 스크립트·
패키지/잠금 파일·결제 정책 JSON 등 고위험 빌드 입력은 `git status`만 믿지 않고
고정 legacy/`git_head`에서는 Git HEAD의 실제 blob 및 index flag와 비교한다. 미커밋·`assume-unchanged`·
`skip-worktree`로 은폐한 변경이 있으면 `REVIEW_REQUIRED`로 배포 계획을 거부한다.

계획 ID와 소스 해시를 **관리 웹에서 사람이 검토·승인**하거나, 계획 결과의
`approval_command`를 실제 호스트 TTY에서 직접 실행하고 계획 ID를 입력해야 한다.
채팅의 승인 문구, `approved=true`, Git 승인 영수증은
배포 권한이 아니다. 별도 `approve-host-operation.py`는 설치된 불변 런타임에서만
실행하며 단일 nonce와 plan digest를 소모한다. API 외 재생성, 실결제 활성화,
DB migration/seed, Docker 정리, Tailscale 제어, 비밀키/사용자 데이터 반환은
허용하지 않는다. 불확정 결과는 자동 재시도하지 않는다.

테스트 코드는 `chatgpt-mcp/tests/test_host_runtime.py`에 있으며 fake Docker
runner/임시 private context를 사용한다. 실제 Docker, 토스 콘솔, Gemini API를
호출하지 않는다. 실행은 `python3 -m unittest -v tests.test_host_runtime`
(`chatgpt-mcp/`에서)로 검증한다. 기존 MCP 전체 회귀 및 불변 runtime 파일
목록 검증을 별도로 수행해야 한다.

**소스 편집과 운영 반영은 별개의 승인을 요구한다.** 실제 터널은
`install-runtime.py`가 생성한 불변 스냅샷을 사용하므로 소스 수정만으로
신규 도구가 나타나지 않는다. 승인 후에만 스냅샷 설치·터널 재연결과
`host-runtime.json` opt-in을 진행한다. 이 P0 코드는 **실호스트 배포 결과가
검증되었다는 근거가 아니며**, 토스 앱의 광고 5/5/30초 재생은 별도 실기기
확인을 거쳐야 한다.


### Host Runtime 최초 활성화 (호스트에서 1회)

설정 파일이 없어 `get_runtime_status`가 `availability: disabled`를 반환한다면,
저장소 루트에서 다음 명령을 **호스트의 대화형 터미널에서만** 실행한다.

```sh
python3 chatgpt-mcp/configure-host-runtime.py --enable
```

대상 `tossApp/tarororo-test`를 확인한 뒤 `ENABLE`을 직접 입력한다.
설정은 `~/.config/chatgpt-coding-mcp/host-runtime.json`에 소유자 전용(0600)으로
생성하며 다른 Docker 타깃, 일반 명령 실행, DB 쓰기를 허용하지 않는다.
MCP 격리 명령, 파이프나 비대화형 호출에서 활성화할 수 없다.
기존 설정이 잘못되었거나 권한이 안전하지 않으면 변경하지 않고 거부한다.
설정은 호출할 때 읽으므로 도구가 이미 등록된 경우 별도 코드 재설치는 필요 없다.
단, 기존 설치 스냅샷에 해당 도구가 없는 경우에는 검토와 재설치·터널 재연결이 필요하다.
API 배포는 이후에도 각 작업에 별도 실제 호스트 TTY 승인이 필요하다.
로컬 승인 UI/원격 승인 프로토콜은 아직 구현되지 않았으며 채팅 메시지를 호스트 승인으로 간주하지 않는다.

### Host Runtime 확장 — DB 013, Worker 및 로컬 웹 승인 (추가 구현)

현재 저장소에는 다음 기능을 추가했다. **소스 구현/모의 테스트 결과와 실제
호스트 설치·Docker 변경은 구분한다.** 설치된 런타임 snapshot과 독립
admin HTTP 프로세스는 소스 파일 저장만으로 바뀌지 않는다.

- `get_db_migration_status(project=tossApp,target=tarororo-test)`는 001~012
  선행 체크섬 일치 개수, 013 이력/원본 해시, 필수 테이블 네 개의 존재 및
  대상 DB 컨테이너 정체성만 반환한다. 읽기 전용 SQL이며 행 데이터/비밀번호는 반환하지 않는다.
- `prepare_db_migration(request_id=...)`는 **013 하나만** 대상으로 하며
  선행 12개 마이그레이션 체크섬 완전 일치, 013 미적용 및 필수 테이블 부재를 요구한다.
  소스/도커 정체성과 7일 승인 계획을 고정한다. 승인된 `execute_test_deploy`
  실행에서 별도 private 디렉터리에 `pg_dump -Fc`를 스트리밍 백업(최대 512MiB),
  `pg_restore --list` 검사에 이어 기존 `tarororo-test` PostgreSQL 인스턴스 안에
  작업 ID 전용 임시 DB(`tarororo_restore_<id>`)를 생성해 백업을 실제 복원한다.
  `--exit-on-error --single-transaction` 복원과 12개 이전 마이그레이션 이력을 확인한 후
  임시 DB 삭제까지 성공해야 동일 DB 컨테이너 재검증 및 013 트랜잭션을 진행한다.
  임시 DB에는 실제 데이터가 잠시 복원되므로 해당 테스트 DB 호스트의 디스크·CPU 여유를
  검토해야 한다. 생성한 임시 DB의 제거가 실패하면 013 적용을 중지하고 조사한다.
  **기존 테스트 DB 자동 복원/재실행은 없다.** 모의 단위 테스트만으로 실호스트의
  `pg_dump`/`pg_restore` 동작을 검증한 것은 아니다.
- `prepare_test_deploy(component=worker,mode=disabled,...)`는 승인 이후
  전용 Worker 이미지와 컨테이너만 교체한다. API/DB 정체성이 바뀌면 실패한다.
  Worker 계획은 현재 API가 disabled 모드일 때만 지원한다.
- 기존 v1 `host-runtime.json`은 **API 전용**으로 남는다.
  관리 UI에서 별도 `ENABLE WORKER AND DB 013` 문구와 관리자 토큰
  재인증으로 정확한 v2 설정으로 승격해야 Worker/013 계획을 만들 수 있다.
  실제 개별 작업은 각자 별도 승인 영수증을 요구한다.
- 로컬 관리 화면 `http://127.0.0.1:8765/`의 “테스트 배포 승인” 영역은
  계획 ID/모드/소스 fingerprint/영향 서비스/만료시각을 표시한다.
  개별 승인 요청은 localhost 또는 현재 호스트에 고정 등록된
  `https://cantopsbuildserver.tail661d29.ts.net:8443` (Tailscale Serve)
  Origin만 허용한다. 같은 Origin의 브라우저 POST, 관리자 토큰 재인증,
  정확한 작업 ID 재입력을 모두 요구하고 단일-use 영수증만 저장한다.
  임의 도메인/HTTP/다른 LAN 리스너 및 기타 공개 프록시에서는 승인 불가하다.
  **배포 실행은 별도 `execute_test_deploy` 호출이 필요하다.**
  Tailnet에서만 접근 가능한 HTTPS 관리 화면을 대상으로 한다.

실제 적용 전 `python3 -m unittest -v tests.test_host_runtime
tests.test_admin_host_approval` 및 전체 MCP 회귀, 코드 보안 리뷰,
백업 복원 테스트, Compose/DB 대상 확인을 실행한다.
`install-runtime.py`로 불변 snapshot을 새로 설치하고 터널 실행 경로·관리 HTTP
프로세스를 **새 설치 경로로 전환·재시작**해야 신규 도구·UI가 사용 가능하다.
원본 레포 변경만으로 실행 중 서버가 재시작되지 않는다.

### 터널 런타임 단계적 전환 — 호스트 1회 명령

저장소에서 소스 검토 후, 로컬 호스트의 대화형 터미널에서 다음 명령 하나로
테스트→스냅샷 검증→터널 연결 갱신을 수행한다.

```sh
python3 chatgpt-mcp/rollout-host-runtime.py --apply
```

프로그램은 `personal-ops-coding` alias의 `tunnel-client runtimes status --json`에서
기존 터널 ID를 읽고, `~/.config/chatgpt-coding-mcp/runtime-key`의 존재·권한만
확인한다. 키 본문·터널 ID를 출력하지 않는다. 소스 파일 digest를 검증하고
관련/전체 unittest가 모두 통과해야 불변 릴리스를 설치한다.
호스트에서 `ROLLOUT` 확인 후 현재 터널 alias를 새 `server.py`로 전환한다.
아무런 Docker 재시작, DB 적용 또는 배포 승인은 수행하지 않는다.

`--apply`가 없으면 진단·테스트만 한다. 안전 검사 실패 시 설치/연결을
진행하지 않는다. 구 CLI가 ID를 출력하지 않는 경우에는 자동 전환을
중단한다. 필요할 때만 `--tunnel-id`로 기존 ID를 전달할 수 있으며
`status`에서 발견한 ID와 다르면 거부한다. 전환 뒤 `status`가
`ready`가 아니면 성공으로 보고하지 않는다.

**관리 대시보드는 별도 HTTP 프로세스다.** 원스텝 rollout은 이미 설치된
`personal-ops-admin.service`의 `ExecStart`가 인지된 Python
`admin_server.py` 명령인 경우에만 원래 port/config/origin/LAN 인자를
보존하는 systemd 사용자 drop-in을 적용한다. `daemon-reload`→restart→
`is-active`→루프백 HTML의 신규 승인 UI 확인까지 성공해야
`admin_ready`로 보고한다. 재시작·검증 실패 시 기존 drop-in을 복원하고
이전 서비스를 재시작하며, 식별 불가능한 서비스는 변경하지 않는다.
따라서 rollout 결과 `verification_required` 또는
`admin_status=manual_review_required`는 관리 화면이 새 UI로
교체됐다는 뜻이 아니다. 새로운 MCP 도구 목록도 플러그인 새로고침 후
재조회해야 한다.

### 관리 웹에서 승인하는 런타임 업데이트

`prepare_runtime_rollout(project="personal-ops", request_id=...)`로 소스·검사 파일과
대상 런타임을 고정한다. 반환된 HTTPS 승인 링크를 열고 관리 웹에 로그인하면
해당 계획을 조회한다. 계획 ID와 관리 토큰을 다시 입력해 승인한 뒤,
채팅에서 `execute_runtime_rollout(project="personal-ops", operation_id=...)`을
호출한다. 승인을 누르는 것만으로 설치나 재시작이 시작되지는 않는다.
진행과 결과는 `get_runtime_rollout`으로 확인한다.

승인은 계획 생성부터 7일 안에 한 번만 사용할 수 있다. 소스가 달라지면 기존 승인은 사용할 수 없다.
실행기는 별도 사용자 systemd unit에서 동작하며, 터널과 관리 웹만 전환한다.
DB·Docker·API·Worker·Toss 업로드는 이 승인의 대상이 아니다.
중단 후 결과를 확정할 수 없는 작업은 `outcome_unknown`으로 남고 자동 재실행하지 않는다.

새 웹 도구가 없는 설치본은 기존 SSH/TTY `ROLLOUT` 절차로 먼저 설치해야 한다.
2026-10-16 별도 승인 후 통합 보완본 `5c725c7a6d4ef86b9759`/80파일을 활성화했고,
실제 관리 웹과 플러그인 읽기 연결을 검증했다. ChatGPT 도구 목록 갱신은 별도 확인 대상이다.
기존 권한 기록은 보존하며, `RUNTIME_CHANGED`인 호스트 권한은 승인 카드에서 갱신한다.
승인 링크·로그인·중단 복구 보완본의 검증과 적용 기록은
[통합 배포 인계](../docs/handoffs/2026-10-16-runtime-rollout-integration.md)를 따른다.
보완본은 승인 화면의 실제 HTTP/Chromium 검사도 공식 배포 사전검사에 포함한다.
`tests/test_runtime_rollout_ui.py`는 브라우저가 설치된 `/usr/bin/python3`로 직접 실행하며,
MCP Python의 테스트 탐색에서는 Playwright를 불러오지 않는다.

### 등록 대상 호스트 작업 (version 3) — MCP 도구와 관리 웹 승인

설계: [채팅 기반 호스트 작업 확장](../docs/superpowers/specs/2026-10-13-chat-host-operations-design.md) §5·§9·§10.
**소스 구현과 모의 검증일 뿐 실제 대상 설정이나 Docker 상태를 바꾸지 않는다.**

기존 006–012 전용 설정도 version 3이지만 `version/enabled/targets`의 고정 형식이다.
등록 대상 설정은 여기에 `legacy/agent_profiles`를 포함하는 별도 형식이며,
기존 고정 설정만으로 등록 대상 권한이 생기지 않는다. 웹의 고정 범위 활성화
버튼은 등록 대상 설정을 고정 형식으로 덮어쓰지 않는다.

#### 등록형 reviewed_snapshot의 단순화된 승인

활성 등록형 v3에 정확한 고정 legacy 대상이 보존돼 있고, 해당 대상의
`source_review`가 `reviewed_snapshot`이면 기존 API·Worker 배포와
006–012·013 전용 계획은 기능별 범위 활성화나 사전 커밋 없이 만들 수 있다.
미커밋·미추적 입력을 개인 스냅샷에 고정하며 고위험 파일의 HEAD/현재 바이트
해시·모드·변경 자료를 인증된 관리 웹에 표시한다. 소스·등록 설정·검토 자료의
해시를 작업 계획과 함께 승인하고 실행 직전에 다시 확인한다.

기본 흐름은 `계획 생성 -> 관리 웹 검토/승인 -> MCP 실행`이다. 웹에서 승인할 때
실행은 시작하지 않는다. 소스나 설정이 바뀌면 새 계획을 만들고 다시 승인한다.
`prepare_predecessor_migration`의 006–012 전용 계획은 013을 실행하지 않으며,
기본 정책에서 013은 별도 계획·별도 승인이다.
고정 legacy와 등록형 `git_head`의 기존 범위·HEAD 검증 조건은 유지한다.
`GET /api/host-operation-review?project=tossApp&operation_id=`는 인증된 관리자만
고정 대상 계획의 검토 자료를 읽는 경로다. MCP 응답에는 원문 diff나 개인 경로를 담지 않는다.

이 변경은 기존 전용 배포·마이그레이션 작업의 승인 조건을 통합한다.
임의 SQL·일반 PostgreSQL backup/restore·에이전트 위임(B·C)은 후속 구현이며,
등록된 Compose capability와 일반 파일/명령 도구의 범위를 추가로 넓히지 않는다.

#### 테스트 대상의 사전 허용

등록형 `reviewed_snapshot`의 `tossApp / tarororo-test`에서는 관리 웹의
“채팅에서 자동 실행 허용”으로 기존 API·Worker 배포와 고정 스키마 적용을
한 번 허용할 수 있다. 기본값은 꺼짐이다. 관리 웹에서 대상 문구
`tossApp/tarororo-test`와 관리자 토큰을 다시 입력해 허용하거나 철회한다.
허용 버튼은 권한만 저장하고 Docker·SQL 작업을 시작하지 않는다.

허용 뒤에도 채팅은 계획을 만들고 명시적으로 실행 도구를 호출해야 한다.
새 계획에는 `authorization.mode=target`과 허용 ID가 고정되므로 작업별 승인
영수증을 다시 요구하지 않는다. 허용 전에 만든 계획은 기존 승인 방식을 유지한다.
유효한 새 계획의 MCP 응답은 `state=ready_to_execute`, `approval_required=false`와
호출할 실행 도구·인자를 담은 `next_action`을 반환한다. 사용자가 실행을 요청했다면
채팅에서 그 도구를 바로 호출하며 관리 웹의 추가 승인을 요구하지 않는다. 계획
조회만 요청한 경우에는 실행하지 않는다. 7일이 지나 `PLAN_EXPIRED`가 된 계획은
관리 웹의 대기 목록에서 빠진다. 새 `request_id`로 계획을 다시 만들면 되며,
만료된 계획을 승인하거나 재사용하지 않는다.
허용은 재부팅 뒤에도 보존되며 철회·대상 설정 변경·허용된 SQL 바이트 변경 시
자동 실행이 차단된다. SQL 변경 후에는 관리 웹에서 다시 허용한다. API·Worker의
일반 소스 변경은 계속 새로운 실행 스냅샷에 고정한다.

`prepare_db_migration`은 유효한 대상 권한이 있고 001~005만 정상 적용된 상태라면
고정 006~013 통합 계획(`component=schema_update`, `migration_range=[006,013]`)을
만든다. `execute_db_migration`은 백업 및 일회용 복원 검증을 한 번 수행하고
006~013을 한 트랜잭션으로 순차 적용한 뒤 전체 이력·체크섬·테이블을 검증한다.
001~012가 이미 정상 적용됐다면 기존 013 전용 계획을 사용한다. 이력 불일치나
일부만 적용된 상태는 자동으로 덮어쓰지 않는다.

소스·Compose·컨테이너·DB 상태·만료·잠금 검사는 계속 실행한다. 권한은 허용 레포
밖의 개인 상태 디렉터리에 `0600`으로 저장하며 MCP에 권한 변경 도구를 노출하지
않는다. `GET /api/host-target-authorization?project=tossApp&target=tarororo-test`는
인증된 상태 조회이며, 같은 주소의 POST는 정확한 `enabled`, `confirmation`,
`admin_token` 필드와 기존 loopback/Tailscale HTTPS 관리자 재인증 경계를 요구한다.
LAN에서는 상태 조회만 가능하다. 등록형 Compose 작업의 개별 승인 정책은 유지한다.

실행 중인 불변 런타임에는 새 소스가 자동 반영되지 않는다. 변경을 사용하려면
검증된 snapshot 설치와 MCP·관리 웹 서비스 전환이 필요하다.

| 도구 | 동작 |
|---|---|
| `list_host_targets(project)` | 이 프로젝트에 등록된 대상, 허용 동작·실행 프로필, 제한 사유(`enabled`/`reason`) 조회 |
| `inspect_host_target(project, target, area=docker)` | 등록 서비스의 컨테이너·이미지 ID·상태·health만 조회 (로그·env·mount 없음) |
| `prepare_host_operation(project, target, action, parameters, request_id)` | 입력·대상 정체성을 고정한 7일 계획과 `approval_url` 반환. Docker·SQL 실행 없음 |
| `execute_host_operation(project, target, operation_id)` | 웹 승인 영수증을 1회 소비하고 비동기 실행. 재호출은 기존 상태만 조회 |
| `read_host_operation_events(project, target, operation_id, cursor, limit)` | 상태·단계와 안전한 이벤트를 cursor로 조회 (기본 50, 최대 200, 64 KiB) |

- 입력 schema는 모든 단계에서 `additionalProperties: false`이며 서버가 registry·`validate_action`으로
  다시 검사한다. 미등록 필드·대상·동작은 효과 없이 거절된다. 오류는 정제된 코드뿐이다.
- 기존 7개 도구의 schema는 그대로다. `get_host_operation(tossApp, tarororo-test, operation_id)`는
  ID가 저장된 backend(legacy 또는 registered)를 확인해 조회하고, 두 곳에 모두 있으면
  `AMBIGUOUS_OPERATION_ID`로 거절한다.
- 실제 실행은 **설치된 불변 runtime**에서만 가능하다. 저장소 checkout에서는 `UNSAFE_RUNTIME`으로 거절된다.

**웹 승인 흐름.** `approval_url`은 신뢰된 관리 웹 HTTPS origin에
`/host-operations?project=..&target=..&operation_id=..`를 붙인 절대 URL이며
토큰·nonce를 담지 않는다(다른 파라미터가 있으면 400). 관리 화면에 로그인하면 해당 작업이 선택된
“등록 대상 호스트 작업” 영역이 열리고 계획·영향(재생성/중단/중지/보존, DB 연결, 볼륨 보존)·검증 항목·
변경 경로·고위험 변경 검토 자료(`reviewed_snapshot`)·만료 카운트다운·단계와 최근 이벤트를 보여 준다.
작업 ID를 직접 입력하고 관리자 토큰을 다시 입력해 **승인** 또는 **거절**한다. 승인은 1회 영수증만
발급하며 실행하지 않는다. 실행은 채팅의 `execute_host_operation` 호출로만 시작한다.

| Endpoint | 내용 |
|---|---|
| `GET /api/registered-targets?project=` | `list_host_targets`와 같은 공개 대상 목록·제한 사유 |
| `GET /api/registered-operations?project=&target=` | 대상의 최근 작업 20개(모든 상태) |
| `GET /api/registered-operation?project=&target=&operation_id=&cursor=` | 계획·검토 자료·이벤트 한 페이지 |
| `POST /api/registered-approve?project=&target=` | `{operation_id, plan_digest, source_digest, confirmation, admin_token}` |
| `POST /api/registered-reject?project=&target=` | `{operation_id, confirmation, admin_token}` |

두 POST는 기존 `/api/host-approve`와 **같은 검사 함수**를 쓴다: loopback에 바인딩된 서버, 같은 Origin의
loopback 또는 등록된 Tailscale HTTPS Origin, `Sec-Fetch-Site: same-origin`, forwarding header 없음,
정확한 body key, 새로 입력한 관리자 토큰. LAN HTTP 리스너·다른 Origin·토큰 누락은 403이며 영수증이 생기지 않는다.
프로젝트·대상 범위는 매 요청 새로 읽은 registry로 검사한다. 거절 코드는 `{"code", "diagnostic_id"}`로만 보여 준다.
기존 `GET /api/host-operations`·`POST /api/host-approve`의 형식은 바뀌지 않았다.

**활성화는 별도 호스트 단계다.** 이 화면과 MCP 도구에는 대상 설정(version 3 `host-runtime.json`)을 바꾸는
endpoint가 없다. 후보 설정은 `python3 chatgpt-mcp/configure-host-runtime.py --validate-v3 CANDIDATE`로
검증만 하며 파일을 쓰지 않는다. 실제 설정 적용, 불변 runtime 설치(`install-runtime.py` /
`rollout-host-runtime.py --apply`), 서비스 재시작은 구체적인 차이와 영향에 대한 별도 승인 후 호스트에서 진행한다.
설치 manifest와 신뢰 검사 목록에는 `host_registry.py`, `host_actions.py`, `host_process.py`,
`host_source_review.py`, `host_registered_operations.py`, `host_compose_actions.py`,
`host_registered_tools.py`와 관리 웹·대시보드 asset이 포함되며, 한 byte라도 바뀌면 runtime을 신뢰하지 않는다.

관련 회귀: `tests/test_host_registered_router.py`(실제 MCP SDK stdio, 모의 runner 종단 흐름),
`tests/test_admin_registered_approval.py`(HTTP 승인 경계), `tests/test_runtime_install.py`(패키징·변조),
`node --test chatgpt-mcp/tests/test_host_operations_ui.mjs`(textContent·빈/만료/오류 상태·링크).

## MCP 도구 목록 갱신 요청

`personal-ops` 프로젝트가 등록된 서버는 다음 두 모델용 도구를 제공한다.

| Tool | Effect |
| --- | --- |
| `refresh_mcp_catalog(project="personal-ops")` | Send a tool-list change notification on this connection |
| `get_mcp_catalog_refresh_status(project="personal-ops")` | Read same-session notification/refetch evidence |

```text
refresh_mcp_catalog
  -> notification_sent
  -> client tools/list
  -> catalog_refetched
```

`catalog_refetched`는 같은 연결에서 요청 뒤 도구 목록 조회가 성공했다는 뜻이다.
새 연결의 상태는 `idle`부터 시작한다. 상태는 메모리에만 있으며 서비스 재시작이나
권한 발급을 수행하지 않는다. 전송 실패는 `CATALOG_REFRESH_NOTIFICATION_FAILED`로
보고한다.

`chatgpt_metadata_verified`는 항상 `false`다. MCP 알림에는 적용 완료 ACK가 없고,
ChatGPT 도구 캐시나 승인 카드가 바뀌었는지는 이 서버가 확인할 수 없다.
클라이언트가 알림을 처리하지 않거나 새 도구가 처음 등록되는 경우에는
[ChatGPT 공식 metadata Refresh 절차](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)와
새 대화 확인이 필요하다. 설치 SDK 1.23.0의 2025-06-18 MCP 알림 API를 사용한다.

## 선택형 workspace 정책 (소스 구현, 운영 미활성)

현재 운영 설정에 이 정책을 자동 생성하거나 적용하지 않는다. 기본 작업 영역은
`/home/jhw/ai/opencode/projects/`이며, 등록 project의 일반 소스에 적용한다.
영역 밖 project를 등록해도 다른 project의 정책 로딩은 중단하지 않지만,
영역 밖 파일은 정확한 경로와 작업이 일치하는 별도 접근 승인을 요구한다.

정책은 `$XDG_CONFIG_HOME/chatgpt-coding-mcp/workspace-policy.json`
(기본 `~/.config/chatgpt-coding-mcp/workspace-policy.json`)에 둔다. 부모는
현재 사용자 소유 `0700`, 파일은 단일 링크 일반 파일 `0600`이어야 하며,
모든 project 밖에 있어야 한다. 아래는 임시 project를 사용한 형식 예시다.
실제 산출물의 경로·필드 허용 목록은 소유자가 별도로 확정한다.

```json
{
  "version": 1,
  "base_root": "/home/jhw/ai/opencode/projects",
  "artifacts": [{
    "project": "alpha",
    "path": "build/results",
    "kind": "directory",
    "operations": ["list", "read"],
    "fields": ["text", "usage"],
    "recursive": false,
    "max_file_bytes": 1048576
  }],
  "protected_paths": []
}
```

`.gitignore`와 일반 소스 검색의 제외 규칙은 유지한다. 등록한 JSON만 별도 도구로
읽으며, 지정한 최상위 필드만 반환한다. 파일 상한은 1 MiB, 응답 페이지는 64 KiB다.
커서는 설치 ID·권한 세대·정책 digest·경로·원본 및 반환 내용 hash에 묶인다.
중첩된 알려진 인증 키와 명백한 키/토큰 패턴은 거절하지만, 임의 문장의 모든 비밀을
판별하는 기능으로 사용하지 않는다. 일반 read/search, Git diff와 명령 snapshot으로
등록 산출물의 원문을 우회해서 읽을 수 없다.

정책을 설정한 서버에서 다음 도구가 추가된다.

| Tool | Contract |
| --- | --- |
| `read_registered_artifact` | Exact owner rule, projected JSON, signed pages |
| `list_registered_artifacts` | Registered directory metadata; no implicit read |
| `read_workspace_file` | Bounded source read; exact external read grant |
| `list_workspace_files` | Bounded metadata; separate list permission |
| `write_workspace_file` | One-file create or SHA-256 CAS replacement |
| `open_workspace_access_approval` | Exact path, operations, purpose and duration |
| `confirm_workspace_access_approval` | App-only, private one-use capability |
| `cancel_workspace_access_approval` | App-only, private one-use capability |
| `read_workspace_output` | Frozen completed output, signed pages |
| `publish_workspace_output` | One verified output -> one target + receipt |

승인은 현재 MCP 설치 범위이며 사람별 인증을 주장하지 않는다. 기본/최대 30일이고,
목록·읽기·쓰기를 각각 검사한다. 승인 카드는 7일간 유효하며 모델 응답에 없는 private capability로
확정하며, 모델에는 사용할 수 있는 공개 `grant_id`와 만료 시각을 전달한다.
경로 승인은 명령·네트워크·Codex·배포·DB 실행 권한을 만들지 않는다.
`.env`, 키·인증 디렉터리, 정책·권한 상태, 설치 runtime/controller는 경로 승인으로도
읽거나 변경할 수 없다. 모든 파일 접근은 상위 디렉터리까지 no-follow FD로 열고,
링크·특수 파일·범위 이탈과 stale 입력을 거절한다. 기존 파일 모드는 보존한다.

정책이 없고 활성화 latch도 없는 설치의 기존 도구 계약은 유지한다. 활성화 후에는
정책과 권한 디렉터리를 잃어도 독립 latch가 남아 legacy 접근으로 복귀하지 않는다.
상태와 latch까지 과거 디스크 이미지로 복원하는 동일 UID 소유자의 행위를 로컬
counter만으로 탐지한다고 주장하지 않는다.

### 명령 실행과 결과 반영의 변경

활성 정책에서 `start_command`는 호스트 원본을 직접 쓰지 않는다. 여과한 읽기 전용
`/workspace`와 job별 `/scratch`, `/output`을 사용한다. 쓰기 가능한 cwd가 필요한
명령은 `writable_cwd=true`로 사본에서 실행한다. 실제 결과는
`WORKSPACE_OUTPUT_DIR=/output`에 저장한다. `workspace_inputs`에는 최대 16개의
`{path, grant_id}`를 지정할 수 있다. 매 파일에 유효한 읽기 승인이 필요하며,
원본 디렉터리를 mount하지 않고 `/inputs/<index>/input`으로 동결한다.
입력 승인의 만료·철회는 해당 입력을 사용하는 작업을 중단시킨다.

프로세스 종료와 출력 pipe 종료를 확인한 뒤 export 파일을 읽기 전용 사본으로
고정한다. export는 최대 2,000개 항목/16 MiB이며 파일 하나는 1 MiB로 제한한다.
`read_workspace_output`은 이 고정 사본을 읽는다. 성공한 작업만
`publish_workspace_output`으로 파일 하나를 반영할 수 있다. 원래 job의
`request_id`를 사용하고, 새 파일은 부재 조건, 교체는 `expected_sha256` CAS를
요구한다. 반영 중 중단되면 pending 영수증의 고정 hash와 실제 목적지 hash로
판정하며 다른 staging 내용을 재실행하지 않는다. 여러 파일의 원자적 반영은 지원하지 않는다.

협력하는 MCP 파일 writer·Git·publisher는 설치 전체의 동일 writer lock을 사용한다.
외부 승인 목적지가 여러 project에 걸쳐 같은 파일을 가리킬 수 있기 때문이다.
광범위 host 작업은 shared lane과 durable 실행 예약을 유지하고, publication은
exclusive lane 아래 예약이 없을 때만 진행한다. 소유 서비스가 죽어도 예약은 남는다.
종료를 검증하지 못한 작업은 자동 해제하지 않으며, 기존 검증된 cancel/recovery 또는
소유자 유지보수가 필요하다. 같은 UID로 별도 승인한 임의 host 코드를 OS 차원에서
이 상태로부터 격리하는 계약은 아니다.

활성 정책의 Git push는 전송할 모든 commit과 parent 경로를 검사한다. 보호·ignored·
등록 산출물 경로가 하나라도 있거나 commit/path 상한(각 128)을 넘으면 승인을 만들지
않는다. 중간 commit에 추가했다 삭제한 내용도 검사하며 실행 직전 재검증한다.
현재 legacy isolated-agent snapshot/CAS 경로는 활성 정책에서
`WORKSPACE_ISOLATED_AGENT_UNSUPPORTED`로 거절한다. 기존 별도 승인 host-agent
경로는 기존 gate를 유지한다. 이 호환 제한도 운영 적용 전에 검토해야 한다.

### 실행·복구와 활성화 경계

권한 상태는 `$XDG_STATE_HOME/chatgpt-coding-mcp/workspace-authority`에,
독립 enforcement latch는 그 부모에 둔다. 정책·권한 세대와 grant/pending/cursor/job이
결합되며, managed target start와 rollback 시도마다 권한 세대를 durable 증가시킨다.
실패한 target 뒤 rollback도 다시 증가한다. 권한 상태와 latch는 release 백업·복원에
포함하지 않는다. 모든 복구 target은 전체 manifest digest와 enforcement ABI를
소유자가 미리 검토한 호환 build여야 한다. pre-epoch 구 릴리스는 시작 전에 거절한다.

stable controller는 release 밖의
`~/.local/lib/chatgpt-coding-mcp/workspace-controller`에서 MCP/admin/rollout worker를
시작하기 전에 검사한다. 사용자 정의 controller 경로도 프로젝트 밖이어야 하며
`controller-manifest.json`이 있는 설치 경로는 일반 경로 도구에서 보호한다.
서버/admin/worker 자체의 startup 검사도 유지한다.

**첫 활성화에는 owner transaction 검증이 필요하다.** 이 소스의 owner CLI
`initialize`/`migrate`는 `ACTIVATION_EVIDENCE_REQUIRED`로 중단한다. 두 Boolean
옵션만으로 구 프로세스 배출, legacy pending 취소·grant 철회, 서비스 unit의 stable
launcher 전환을 증명할 수 없기 때문이다. Python `initialize`는 임시 검증과 신뢰된
owner primitive이며 이 CLI 차단을 우회하는 운영 절차로 제안하지 않는다.
실제 적용에는 검증 가능한 migration evidence 경로, 호환 rollback build, 정확한
정책 diff와 산출물 필드 검토, 별도 운영 승인 및 실제 connector/smoke 검증이 필요하다.

첫 활성화와 중단 후 재개는 검토한 불변 release의 `workspace_activation_cli.py`로
준비한다. `workspace_control.py activation --release <절대 release 경로> -- ...`도
전체 manifest를 검증한 뒤 같은 owner CLI를 실행한다. 웹채팅 도구로 owner 정책이나
권한 저장소를 활성화하지 않는다.

- `prepare`: 정확한 정책 후보·목적지·projects 파일, target/rollback release와 ABI
  최저치를 고정한다. ABI 2의 선언과 실제 시작 차단 동작을 검증하며, 선언이 없는
  구버전을 인자로 재분류하지 않는다. 산출물의 허용 JSON 필드가 비어 있으면 중단한다.
- `status --transaction-id <ID>`: private journal을 읽는다. 프로젝트 설정이나 경로가
  사라져도 저장된 상태를 조회하며 서비스나 저장소를 만들지 않는다.
- `apply --transaction-id <ID>`: 새 작업을 차단하고 실제 worker/lease 배출, 기존 승인
  철회, launcher 전환, 정책·권한 공개와 서비스 시작을 순서대로 검증한다.
- `resume --transaction-id <같은 ID>`: 같은 고정 소스와 실제 파일·프로세스 관측값으로
  중단 지점부터 재개한다. 변경되거나 사라진 증거는 성공으로 간주하지 않는다.

Journal은 `$XDG_STATE_HOME/chatgpt-coding-mcp/workspace-activation`의 owner0700
디렉터리와 owner0600 파일에 보관한다. 별도로 고정한 계획 digest와 다른 journal은
재개하지 않는다. 준비된 authority descriptor는 latch 생성 전에
fsync하며, 부분 준비는 private quarantine으로 보존한다. 첫 활성화 전에 새 admission
gate가 포함된 release를 배포하고 기존 실행을 별도로 배출해야 한다. 실패 시 latch나
철회된 승인을 복원하지 않으며, 정확히 확인한 새 서비스만 정지한다.
과거 `outcome_unknown` 기록은 종료·lease 관측과 분리해 원문을 보존한다.

권한 공개 뒤 서비스 시작 중에도 별도의 durable barrier가 새 작업을 거절한다.
검증된 launcher만 시작할 수 있으며, 두 서비스와 정책 binding의 확인이 끝난 뒤
차단을 해제한다. 공개 직후 중단된 transaction은 기존 승인 무효화·배출·wiring을
다시 확인한다. 재부팅 뒤에는 같은 공개 권한과 고정 설정이 일치하고 현재 서비스·
worker·lease가 모두 없는 경우에만 새 boot의 관측값을 journal에 고정해 재개한다.
실제 MCP 연결의 정책 동작과 기존 산출물 조회(T14)는 서비스 시작 뒤 별도 smoke로
확인하며, 프로세스 실행만으로 이 검증까지 완료됐다고 보고하지 않는다.

ABI 2로 이미 활성화된 설치는 먼저 일반 rollout으로 같은 설치에 admission된 ABI 3
release를 floor 2에서 실행·검증한 뒤, 그 설치된 release의 `workspace_upgrade.py`를
사용한다. 이 명령은 서비스를 중지하거나 시작하거나 unit wiring을 바꾸지 않는다.

```bash
python -B workspace_upgrade.py prepare \
  --transaction-id <32자 hex> --target-release <현재 설치 release 절대 경로>
# 별도 owner 절차로 personal-ops-tunnel/admin 두 서비스를 중지한다.
python -B workspace_upgrade.py apply --transaction-id <같은 ID>
# 중단됐다면 같은 release, ID, 설정에서만 재개한다.
python -B workspace_upgrade.py resume --transaction-id <같은 ID>
python -B workspace_upgrade.py status --transaction-id <같은 ID>
```

`apply`/`resume`은 rollout lock과 activation maintenance를 유지한 채 두 서비스와
실제 tunnel/MCP/admin 프로세스, 여섯 worker 종류, lease가 모두 비었는지 새로
확인한다. 기존 installation ID와 policy digest를 보존하고 epoch를 정확히 한 번
증가시키며, 허용 release를 해당 ABI 3 하나로 제한한 뒤 ABI floor를 3으로 올린다.
HostAccess v2는 revision 1, 요청 없음, grant 없음으로 시작한다. 웹 승인 전에는 host
권한이 발급되지 않는다. Journal은
`$XDG_STATE_HOME/chatgpt-coding-mcp/workspace-upgrade`에 별도로 보관한다.

## 검증

```bash
cd chatgpt-mcp
~/.local/share/uv/tools/serena-agent/bin/python -B rollout_test_runner.py \
  --discover tests --require-excluded-count 5
```

임시 프로젝트와 임시 상태 디렉터리에서 실제 MCP 초기화, 도구 목록,
파일 생성·읽기·수정, 동시·교차 레포 요청 분리, 경로 이탈 거부를 확인한다.
UI 도구·리소스와 선택 검증도 포함한다. 임시 레포에서 실제 bubblewrap으로 접근 격리,
중복 요청, 로그·시간 제한·취소·종료 복구를 검증한다. Git은 임시 레포와 로컬 원격 fixture로
승인·stale 거부, 파일·index 범위, 외부 실행 차단, 보호 브랜치, 핸드오프를 확인한다.
OpenAI API는 호출하지 않는다.

이미 호스트에 설치된 Playwright와 Chromium으로 UI를 검증한다.

```bash
python3 chatgpt-mcp/tests/check_project_picker_ui.py
```

브라우저에서 MCP Apps 호스트 브리지를 모사하여 명시적 선택, 두 대화의 분리,
선택 거부·컨텍스트 갱신 실패, `window.openai` 호환 브리지와 상태 전달,
미지원 호스트, 빈 목록과 안전한 텍스트 렌더링을 확인한다.
이는 실제 ChatGPT 클라이언트에서의 종단 간 검증을 대신하지 않는다.

관리 화면은 `python3 chatgpt-mcp/tests/check_admin_ui.py`로 실제 로컬 HTTP API와 Chromium을 사용해 검증한다.
