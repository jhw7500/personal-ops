# ChatGPT coding MCP

SSH 호스트에 설치된 Serena를 OpenAI Secure MCP Tunnel에 연결한다.
하나의 터널과 플러그인으로 허용 목록에 등록된 여러 저장소를 다룬다.
각 파일 도구의 `project` 인자가 작업 대상을 결정한다.

목적은 **일반 ChatGPT 채팅에서 판단·코드 작성·결과 설명을 하고, 호스트 MCP가
파일 작업과 명령 실행을 수행**하는 것이다. ChatGPT Work 프로젝트를 사용할 필요는 없다.
이 서버는 Codex/Claude 에이전트나 별도 추론 API를 호출하지 않는다.
터널 API 키는 연결에 사용한다. 별도 에이전트의 중복 추론을 피할 수 있지만
ChatGPT 자체의 사용량이 없어지거나 전체 토큰 절감량이 보장되는 것은 아니다.
1.2.0부터 격리된 셸·빌드·테스트 실행, 상태·로그·취소,
Git 조회와 승인된 commit·push를 제공한다. PR 생성과 merge는 기존 네이티브
리뷰 워크플로우에 넘기며 이 MCP가 직접 실행하지 않는다.
1.4.0부터 별도 로컬 관리 화면, MCP 호출 타임라인, 레포별 영속 메모리를 제공한다.
1.5.0부터 작업 목표·완료 항목·다음 작업, Git 변경 파일과 diff, 빌드·테스트 결과 및 새 채팅용 요약을 함께 제공한다.

```text
ChatGPT <-> OpenAI Tunnel <-> tunnel-client <-> repository router
                                              |
                                              +-> files -> Serena
                                              +-> command -> sandbox -> job/log
                                              +-> Git -> approval/native workflow
                                              +-> project memory / activity

Browser <-> SSH local forwarding <-> admin HTTP (127.0.0.1:8765)
                                     |-> job/log/activity (read only)
                                     +-> project memory (read/write)
```

## 로컬 MCP

Codex CLI의 세션 상태·모델 설정·신뢰된 도구 후크를 작업실에 연결하는 별도 로컬
수집기도 제공한다. 설치·수집 범위·신뢰 절차는 [Codex 수집기](../docs/codex-cli-observer.md)를 참고한다.

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
항목을 누르면 전체 실행 영역에서 원본 출력을 확인할 수 있다. 호스트 터미널에서 직접 실행한 명령은
터널 실행 기록에 자동으로 포함되지 않는다. 이 관찰 결과가 작업 목표나 기능 완료 보고를 대신하지는 않는다.

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
Codex·Claude의 후크 수집기는 아직 활성화하지 않았다. 형식과 확장 경계는
[업무 작업실 수집 계약](../work-dashboard/README.md#다른-mcp와-에이전트-수집)을 따른다.

## 검증

```bash
~/.local/share/uv/tools/serena-agent/bin/python -m unittest discover \
  -s chatgpt-mcp/tests -v
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
