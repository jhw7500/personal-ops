# personal-ops-tunnel Host Runtime Management — 구현 인계 명세

- 작성일: 2026-10-12
- 상태: **P0 소스 구현 완료 / 격리 모의 검증 완료 / 실호스트 적용·MCP SDK 종단 간 검증 미완료**
- 대상 저장소: `personal-ops`, 주 구현 디렉터리 `chatgpt-mcp/`
- 1차 적용 대상: 허용 프로젝트 `tossApp`의 **`tarororo-test` Docker 테스트 스택**
- 이 문서의 목적: **담당 개발 세션이 과거 ChatGPT 대화 없이 구현 범위·기술 선택·안전 조건·테스트 완료 기준을 파악하도록 하는 단일 인계 자료**
- 산출물: MCP 조회·계획·승인된 호스트 배포 기능. 본 문서 작성 시점에는 코드 수정이 없었고, **후속 구현에서 personal-ops 소스만 변경했다.** 호스트 서비스·DB·터널 설정은 변경하지 않았다.

## 0. 인계 시 우선 확인할 사실

이 문서는 2026-10-12 저장소 파일을 읽고 작성했다. 구현을 시작하는 세션은 현재 체크아웃과 실행 중 설치 버전이 다를 수 있으므로 아래 파일을 **재확인**한다.

| 저장소 | 확인 파일 | 확인 목적 |
|---|---|---|
| personal-ops | `README.md`, `chatgpt-mcp/README.md` | 터널 운영 원칙과 명령 실행 경계 |
| personal-ops | `chatgpt-mcp/server.py`, `job_tools.py`, `jobs.py`, `execution.py` | 도구 등록·라우팅·격리 작업·동시성 |
| personal-ops | `chatgpt-mcp/git_workflows.py`, `approve-git-operation.py` | 기존 **호스트 터미널 1회 승인** 패턴 |
| personal-ops | `chatgpt-mcp/project_state.py`, `admin_server.py` | 상태·활동 로그·HTTP 관리 UI의 제한 |
| personal-ops | `chatgpt-mcp/install-runtime.py` | 저장소 밖 **불변 런타임 스냅샷** 설치 |
| tossApp | `deploy/pc-test/README.md`, `manage.py`, `compose.yaml`, `Dockerfile`, `.dockerignore` | 실제 테스트 서버 배포 절차와 빌드 입력 |
| tossApp | `docs/gone-implementation-kit/toss-demo-ad-release-2026-10-12.md` | 광고 모드, 테스트 단계와 남은 실제 적용 작업 |

2026-10-12 관찰 당시 `personal-ops`의 로컬 `main`은 `origin/main`보다 2커밋 앞서고 12커밋 뒤져 있었으며 별도 미추적 문서가 존재했다. `tossApp`에도 다수의 기존 미커밋 변경이 있었다. **강제 checkout/reset, 일괄 stage, 자동 커밋·push 금지.** 구현자는 변경 전 Git 상태를 재조회하고 다른 세션의 변경과 겹치면 보존한다.

## 1. 문제와 목표

### 1.1 현재 가능한 작업

현재 `personal-ops-tunnel`은 허용 저장소를 대상으로 Serena 파일 조회/수정, Git 조회 및 승인이 필요한 commit/push, `start_command`/`get_job`/`read_job_log`를 제공한다. 명령은 `bubblewrap` 샌드박스에서 실행되며 **선택 레포만 /workspace로 연결**되고 호스트 네트워크, Docker 데몬, 호스트 홈, `.env`, `.git`, 키에 접근할 수 없다. 레포 코드를 바꿔도 실제 터널은 불변 설치 스냅샷을 실행하므로 자동 업데이트되지 않는다.

### 1.2 해결할 실제 사용자 시나리오

`tossApp` 광고 기능은 샘플 영상(카드 2·3 각 5초, 종합 30초)이며 토스 광고 SDK 실광고가 아니다. 2026-10-12 코드 변경으로 테스트 사용자 UUID 사전 등록을 제거하고, 토스 테스트 환경에서 인증된 리딩 소유자가 샘플 광고를 사용할 수 있도록 변경했다. **소스 수정과 격리 테스트는 완료했지만 실제 호스트의 API 이미지 빌드·모드 활성화·컨테이너 교체는 하지 않았다.**

목표는 대화 안에서 아래 흐름을 **관찰 가능한 별도 관리 도구**로 수행하는 것이다.

1. 실제 테스트 스택 상태와 스키마 준비 여부를 읽기 전용으로 확인한다.
2. 빌드 입력·현재 소스·배포된 이미지의 차이 및 불확실성을 보고한다.
3. 정확히 정해진 API-only 테스트 배포를 계획하고, **호스트 실제 터미널**에서 계획 내용을 보고 1회 승인한다.
4. 승인된 입력만 빌드하고 API만 교체하며 건강 상태·설정·이미지 ID를 검증한다.
5. 가능 범위의 스모크 테스트를 실행하고, 토스 앱 실기기/실로그인 검증과 구별한다.

### 1.3 비목표 — 이번 1차 릴리스에서 구현하지 말 것

- 임의 호스트 셸, `argv`·`bash -c`·`sudo`·임의 `docker exec`·임의 SQL·파일 읽기/쓰기 도구.
- `start_command`의 샌드박스 제약 완화 또는 장애 시 호스트 셸로 fallback.
- 운영/프로덕션 배포, 범용 Compose 프로젝트 관리, Docker volume 삭제·`down -v`·`prune`.
- DB migration/seed/apply, 백업 복구, API/Worker/DB 전체 재시작, Tailscale Serve/Funnel 변경.
- 토스 콘솔 앱 업로드·초대권 발급·실제 광고 SDK의 보상 인증·실제 IAP 결제.
- 사용자 액세스 토큰을 채팅에 복사하거나 타인 리딩을 서버에서 대신 조회.
- 관리자 HTTP 페이지에 배포/명령 실행 버튼 또는 공개 네트워크 관리 API 노출.

## 2. 기술적 결정 — 별도 경로, 기본 비활성

**`HostRuntime`을 기존 `ExecutionPolicy`/`JobManager`와 별개의 권한 영역으로 만든다.** 저장소 대상 `project=tossApp`과 운영 대상 `target=tarororo-test`를 모두 검증한다. 일반 `project`를 선택했다는 사실은 호스트 권한을 부여하지 않는다.

`start_command`는 기존 격리 실행만 계속 허용한다. 새 호스트 경로는 신뢰 가능한 **저장소 밖 불변 런타임**에서 고정 동작(enum)만 실행한다. 외부 사용자 입력을 명령 인자, 경로, 컨테이너명, 환경변수명, SQL, 셸 텍스트로 이어 붙이지 않는다. Docker 그룹 접근 자체가 호스트 고권한에 준하므로 **명시적 활성화·1회 터미널 승인·이중 대상 검증**을 요구한다.

### 2.1 구성 파일 (제안)

허용 저장소 밖의 `~/.config/chatgpt-coding-mcp/host-runtime.json`, 소유자=현재 UID, mode=`0600`, 심볼릭 링크 금지. 기존 `execution.json`과 분리한다. 예시:

```json
{
  "version": 1,
  "enabled": false,
  "targets": {
    "tarororo-test": {
      "project": "tossApp",
      "compose_project": "tarororo-test",
      "api_container": "tarororo-test-api-1",
      "worker_container": "tarororo-test-worker-1",
      "db_container": "tarororo-test-db-1",
      "api_health_url": "http://127.0.0.1:3200/health/ready",
      "expected_origin": "https://tarororo.private-apps.tossmini.com",
      "expected_db_name": "tarororo_test",
      "allowed_component": "api"
    }
  }
}
```

예시는 **초기 비활성 상태**이며, 설정만 보고 즉시 호스트 배포하도록 만들지 않는다. `enabled` 활성화 및 실제 설치/재연결은 별도 운영 작업이다. 실제 프로젝트 경로는 `projects.json`의 `tossApp`에서만 가져오고 클라이언트에서 지정하지 않는다. 예시 값과 실제 Compose 정의가 불일치하면 실패한다.

### 2.2 신뢰 경계

```text
ChatGPT / 원격 MCP 사용자 (신뢰하지 않는 명령 입력)
                 |
                 v
MCP server.py → 타입 검증·프로젝트/타깃 고정·출력 마스킹
                 |
                 +→ 기존 start_command → bubblewrap (변경 금지)
                 |
                 +→ HostRuntime (독립 정책·기본 OFF)
                          |
                읽기 전용 상태 조회 / 배포 계획
                          |
                호스트 TTY 1회 승인 영수증
                          |
                승인·해시 재검증 → 호스트 제한 실행기
                          |
             Docker: tarororo-test API만 → 사후 검증
```

**중요:** `tossApp/deploy/pc-test/manage.py` 및 Dockerfile 자체는 수정 가능한 저장소 코드다. 단순히 “명령 문자열이 고정”이라는 이유로 안전해지지 않는다. 실행 전에 빌드 입력·관리 스크립트·Compose·Dockerfile의 **실제 바이트를 고정하고 승인 후에도 동일함을 검증**해야 한다. 소스가 변할 경우 `STALE_PLAN`으로 중단한다. 가능하면 검증된 입력의 별도 private staging snapshot에서 빌드하며, 실제 적용 시 사용하는 관리 코드의 동일성도 확인한다. 이는 이 설계의 핵심 보안 조건이다.

## 3. MCP 도구 계약 (신규)

모든 도구는 `project`와 `target`를 받지만 허용값은 각각 **`tossApp`**, **`tarororo-test`**뿐이다. `additionalProperties: false`. 오류는 임의 CLI traceback이나 환경변수 전체를 싣지 않고 제한된 `code`·`phase`·`message`만 반환한다.

### P0-A. `get_runtime_status` — 읽기 전용

**입력:** `{"project":"tossApp","target":"tarororo-test"}`

**출력 예시:**

```json
{
  "project": "tossApp",
  "target": "tarororo-test",
  "observed_at": "2026-10-12T10:00:00Z",
  "api": {
    "state": "running",
    "health": "healthy",
    "image_id": "sha256:...",
    "monetization_mode": "disabled"
  },
  "worker": {"state": "running", "image_id": "sha256:..."},
  "database": {
    "state": "running",
    "schema_013": "ready"
  },
  "health_endpoint": "ready",
  "warnings": []
}
```

- 실제 Docker `inspect`에서 컨테이너 상태·image ID·정해진 광고 모드 값만 노출. DB 주소·계정 ID·인증/키·전체 `Config.Env` 출력 금지.
- `/health/ready`는 API/DB 연결만 확인한다. 사용자 인증/Worker/Gemini/광고 성공으로 확대 해석하지 않는다.
- `schema_013`는 고정된 **읽기 전용** readiness 검사 결과만 `ready|missing|unknown`으로 요약한다. DB 연결 불가 또는 결과 불명은 `unknown`.
- 설정 OFF, Docker 미설치, 권한 부족도 명시적인 제한된 상태값으로 보고하며 임의 호스트 명령으로 해결하지 않는다.
- 조회 시 컨테이너 생성·기동·DB 수정·로그 원문 전송 없음.

### P0-B. `compare_deployment` — 읽기 전용

**입력:** `{"project":"tossApp","target":"tarororo-test"}`

**출력 필수 항목:** 관찰 시각, 현재 checkout HEAD, 작업 트리 dirty 여부/변경 파일 **경로만**, 승인 검토 대상 빌드 입력 목록과 지문, 현재 Compose 정의 지문, `api/worker` 이미지 ID, **알려진 빌드 출처 여부**, 의도치 않은 서비스 변경 위험, 미적용 코드 여부에 대한 결론.

**출처 검증 불가 시:** 기존 이미지에 검증 가능한 source/build attestation이 없다면 `provenance:"unknown"`과 `comparison:"UNKNOWN"`을 반환한다. 이미지 태그/`git status`만으로 `MATCH` 또는 “최신 적용”이라고 단정 금지.

Dockerfile `COPY`·`.dockerignore`·lockfile 및 **untracked 파일까지 포함한 실제 빌드 입력**을 고려한다. 빌드에 포함되지 않는 파일의 변경은 별도로 표시하되 전체 작업 트리가 깨끗하다고 주장하지 않는다. `server/src`, `server/migrations`, `contracts` 등 광범위 복사 경로가 존재한다. `Dockerfile`, `manage.py`, `compose.yaml`, `.dockerignore`, 패키지 lockfile, `run.mjs` 변경은 보안·동작 고위험으로 분류한다.

### P0-C. `prepare_test_deploy` — 계획 생성만

**입력:**

```json
{
  "project": "tossApp",
  "target": "tarororo-test",
  "component": "api",
  "mode": "toss-demo",
  "request_id": "toss-demo-api-20261012"
}
```

- `component`: 상수 `api`만 허용.
- `mode`: 정확히 `toss-demo` 또는 `disabled`만 허용; 별도 테스터 UUID 없음.
- `request_id`: 1~80자의 안정적인 ASCII 식별자. 같은 ID/같은 입력은 기존 계획 반환, 같은 ID/다른 입력 거절.
- 사전 검사: `project`/설정 활성 여부, Compose target·서비스·Origin·호스트 loopback 3200 바인딩·`APP_ENV=test`·`AUTH_MODE=toss`·`DATABASE_URL`의 전용 DB 정체성(비밀은 출력하지 않음), 실결제 비활성, 현재 DB 존재/013 준비 여부, 기존 API/Worker/DB 세대·이미지 ID, 변경 파일과 빌드 입력 지문, Git 상태, 기존 호스트 작업 여부.
- **타깃이 프로덕션이거나 정책 검사에 실패하면 계획 생성 자체를 거절.** DB schema가 미준비면 `SCHEMA_NOT_READY`: P0는 DB migration을 자동 수행하지 않음.
- 계획은 저장소 밖 0700 디렉터리에 0600 원자 파일로 생성, `operation_id`, 강한 무작위 nonce, 계획 TTL(제안: 10분), `plan_digest`, 변경 입력 지문 및 대상 컨테이너의 ID·시작 시각·image ID를 고정한다.
- 수정 가능한 소스를 검토 가능한 파일 목록/다이제스트로 묶는다. `.git`의 commit만 고정하면 안 된다. 실행이 시작되기 전에 **실제 빌드 컨텍스트 스냅샷을 동일하게 고정**하도록 구현한다.
- 반환: `status:"pending_host_approval"`, 계획 요약, 계획 해시, 만료 시각, 실행 영향(`api` 교체/`db`·`worker` 불변), 예상 검증, **호스트 터미널에서만 실행할** `approval_command`. 이 함수 자체는 Docker 이미지 빌드·컨테이너 교체·DB 변경 없음.
- API 외 구성이나 Worker 호환성을 확정할 수 없으면 `UNSUPPORTED_SCOPE` 또는 `REVIEW_REQUIRED`로 중단하고 테스트 API만 바꿀 수 있다고 추측하지 않는다.

### P0-D. `execute_test_deploy` — 승인된 작업만 비동기 실행

**입력:** `{"project":"tossApp","target":"tarororo-test","operation_id":"..."}`

- 실행 직전 실제 호스트 TTY에서 생성된 1회 승인 영수증을 검사한다(아래 `4`). 채팅 문자열이나 `approved:true` 등으로 대체 금지.
- 계획 TTL, 승인 1회 사용, 소스/Compose/컨테이너 및 DB identity, 모드·비밀 경계 **재검증**. 변경 감지 시 아무것도 실행하지 않고 `STALE_PLAN`.
- 단일 대상에 호스트 간 교차 프로세스 lock(예: `flock`)을 확보. 동일 요청 재전송은 기존 operation 상태를 반환하며 **두 번 빌드·재생성하지 않는다**.
- 코드와 설정 스냅샷이 일치하는 경우에만 `manage.py check` 및 **승인된 입력을 이용한** `manage.py build`(또는 그와 동등한 검증된 빌드 절차)를 수행한다.
- 기존 Compose의 API만 `up -d --no-deps --force-recreate --wait --wait-timeout 60 api`에 준하는 고정 절차로 재생성한다. Worker/DB 컨테이너는 그대로 둔다.
- `TARORORO_READING_MONETIZATION_TEST_MODE`에 승인된 `mode` 문자열만 전달. 사용자 임의 환경변수·비밀 값 전달 금지.
- 사후 검증: API image ID가 승인/빌드 산출물과 일치, API running+healthy, `/health/ready` 성공, API 모드가 요청값과 일치, DB/Worker container ID 및 시작 시각 불변, API Origin·Compose 격리·DB 정체성 유지.
- 실행 응답은 `operation_id`, `state`, `phase`, `started_at`, `finished_at`, 안전한 오류 코드 등 **비동기 작업 메타데이터**를 반환한다. `running`/`queued`는 성공이 아니다.

### P0-E. `get_host_operation` — 비동기 상태 읽기

**입력:** `{"project":"tossApp","target":"tarororo-test","operation_id":"..."}`

상태값: `pending_host_approval|approved|queued|running|succeeded|failed|rejected|expired|outcome_unknown`. 실행 도중 MCP 프로세스/네트워크 연결이 끊기거나 Docker 반환 상태가 불확실하면 **`outcome_unknown`**; 재시작 시 성공/실패 임의 판정 금지, 동일 작업 자동 재실행 금지. 관찰 가능한 상태만 반환. `get_runtime_status`로 실제 세대를 다시 확인해야 한다.

### P1. 별도 후속 도구 (P0 완료 후)

- `get_runtime_logs`: 미리 정의한 `api|worker`의 **안전한 이벤트 메타데이터만** 반환(커서·바이트 제한·민감정보 제거). API가 자유 형식 원문을 출력하면 기본 거부/마스킹 실패로 처리한다. 모든 `docker logs`를 채팅에 전달하지 않는다.
- `run_test_smoke`: 명시한 호스트 테스트 서비스의 `/health/ready`, 인증 없는 API의 401, 모드 조회 가능한 서버/DB 진단 및 사후 불변성 확인. 개인정보/새 사용자 생성/테스트 리딩 삽입/광고 보상 지급은 기본 금지. 진짜 Toss 로그인 토큰이 없는 경우 **카드 해제 실증은 NOT_VERIFIED**로 보고.
- `verify_toss_app`(P2): 별도 ADB·실기기·사용자 인증 및 토스 콘솔 연결이 필요하며 P0/P1 결과에 합산하지 않는다. 샘플 영상 재생 검증과 실제 광고 네트워크 연동 검증을 명확히 구별.

## 4. 승인 체계 — 기존 Git 패턴 재사용, 별도 권한

기존 `git_workflows.py`의 `prepare_git_operation` → 실제 호스트 터미널의 `approve-git-operation.py` → `execute_git_operation` 패턴을 참고하되 **Git 승인 영수증과 배포 승인 영수증은 상호 대체할 수 없도록 별도 namespace**로 만든다.

제안 파일: `chatgpt-mcp/approve-host-operation.py`. 사용자에게 반환할 명령은 **설치된 불변 런타임 경로**를 사용해야 한다(수정 가능한 `tossApp`/`personal-ops` 안의 스크립트 실행 금지).

승인 CLI는 TTY 여부(예: `isatty`)를 확인하고 다음을 보여 준다.

1. 대상: `tossApp / tarororo-test / api` 및 변경 후 모드 `toss-demo|disabled`
2. 정확한 소스/빌드 컨텍스트 해시, 변경 파일 경로·요약, 대상 Docker/DB 세대, 현재·예정 이미지
3. 영향: **API만 재생성**, DB/Worker 유지; 다른 호스트 서비스에 영향 불확실성이 있으면 중단
4. 검증과 되돌리기 방법, 계획 ID·만료 시각

사용자가 계획 ID(또는 별도 일회성 확인 문구)를 **직접 터미널에 입력**했을 때만 승인 영수증을 0600으로 기록한다. 영수증은 `operation_id + plan_digest + nonce + 승인 시각`에 묶고 하나의 실행에만 사용한다. 승인 메시지를 ChatGPT에서 작성하거나 모델이 승인 플래그를 설정하는 방식은 **권한 없음**. TTY 부재, 만료, 파일 모드 오류, 심볼릭 링크, 다이제스트 드리프트는 거절한다.

**배포 복구도 별도 계획/승인이다.** 실패 시 자동 재시도·이미지 복귀·임의 DB 롤백을 하지 않는다. `mode=disabled`로 기능을 끄는 것이 우선적 안전한 되돌리기 경로이나, 서비스가 불안정하면 기존 image ID 복귀를 위한 별도 사전 검증·승인이 필요하다(기능 범위 밖이면 수동 경로를 명시).

## 5. 보안 불변조건

| 항목 | 통과 조건 |
|---|---|
| 환경 격리 | `test` + Toss 인증 + 전용 Compose·DB·Origin·loopback 포트 정확히 일치; `production` 거부 |
| 명령 인자 | 정적 명령/enum만 허용; shell=True, 외부 입력 문자열 접합, 임의 파일 경로/SQL 금지 |
| 승인 | 읽기 전용 도구 외 모든 빌드·재시작은 호스트 TTY 1회 승인 필수 |
| 소스 무결성 | Git HEAD만이 아니라 dirty/untracked 및 실제 Docker 빌드 입력, 관리 스크립트의 바이트 해시 승인·재검증 |
| 런타임 무결성 | HostRuntime 코드는 프로젝트 디렉터리가 아닌 `install-runtime.py` 불변 설치본에서 로드 |
| 비밀 보호 | Compose ENV 전체, DB URL/암호, mTLS 인증서/키, access token, 사용자 UUID, 해석 본문 출력 금지 |
| DB 보호 | 읽기 전용 스키마 검사 외 DB 쓰기 없음; `migrate/seed`, restore, volume 삭제 금지 |
| 서비스 경계 | 승인한 API 컨테이너만 재생성; worker/db ID와 시작 시각 사후 동일해야 성공 |
| 불확정 결과 | 명령 오류·연결 끊김·중도 재시작은 자동 재시도 불가. `outcome_unknown` 수집 |
| 감사 | 계획·승인·실행·결과·소스 digest·시각·사용 대상·요약 기록(0700/0600), 비밀/원문 로그 없음 |
| 기본값 | 설정 미존재/disabled/오류 시 호스트 실행 불가; 일반 파일/MCP/Git 기능 정상 유지 |
| 최소 권한 | sudo 자동 호출 금지; Docker 권한 없으면 `EXECUTOR_UNAVAILABLE`; 관리자 웹에 실행 API 추가 금지 |

읽기 도구도 사용량 제한·짧은 timeout·출력 크기 제한·고정 컨테이너명을 적용한다. Docker daemon 접근이 가능하다는 이유로 다른 컨테이너/이미지/볼륨을 목록화해 전송하지 않는다. `/health/ready`의 리디렉션은 따라가지 않고 고정 `127.0.0.1:3200`만 사용한다. 새 호스트 실행기가 원격 사용자 입력에 의해 `PATH`·Python import 경로·Git hook·Docker Compose override 등을 신뢰하지 않도록 환경을 최소화한다.

### 5.1 타깃별 모드 수명

현재 Compose는 `TARORORO_READING_MONETIZATION_TEST_MODE`가 없으면 `disabled`다. `toss-demo`로 설정 후 **현재 컨테이너가 활성화되었다는 사실과 향후 재배포에서도 자동 유지된다는 사실은 다르다.** HostRuntime은 호스트 전용 설정의 **요청 모드/관찰 모드**를 구분해야 한다. 다른 관리자가 모드를 꺼놓거나 외부 재생성을 하면 drift로 보고, 자동으로 다시 켜지 않는다.

### 5.2 빌드·배포에서의 변조 방어

- 실제 `Dockerfile`은 `server/src`, `server/migrations`, `contracts` 등 넓은 경로를 `COPY`한다. 한 파일만 승인하고 전체 컨텍스트를 빌드하면 **승인하지 않은 코드가 이미지에 포함될 수 있다**.
- 기본 1차 구현은 정확한 Docker 컨텍스트 파일 목록/파일 모드·SHA-256을 승인 지문에 포함한다. 빌드 전에 스냅샷 검증을 반복하고 재검증 동안의 외부 파일 변경 위험은 **private immutable staging**으로 제거한다. 이 조건을 구현하지 못하면 **배포 실행 기능을 출시하지 않고 조회/계획만 제공**한다.
- `manage.py`, Dockerfile, Compose, package lifecycle script, lockfile 변경은 고위험으로 표시하고 승인 화면에 드러낸다. Docker build가 네트워크·패키지 설치를 필요로 할 수 있다는 위험을 검토한다. 빌드 실행의 셸/네트워크/비밀 환경이 승인 밖의 코드를 불러오지 않도록 별도 시험이 필요하다.
- 이미지 태그는 변경 가능한 별칭이다. 실행 결과는 **Docker image ID + 빌드 출처 attestation**으로 확인한다. 태그만 맞다고 성공 처리하지 않는다.

## 6. 구현 파일 지도 (제안)

| 파일 | 할 일 |
|---|---|
| `chatgpt-mcp/host_runtime_config.py` (신규) | 호스트 전용 설정 검증, 타깃/권한/경로 제한, default OFF |
| `chatgpt-mcp/host_runtime.py` (신규) | 안전한 Docker/Compose 상태 조회, 대상 identity 확인, 비교/사후 검증 |
| `chatgpt-mcp/host_runtime_operations.py` (신규) | 계획 snapshot/digest, 승인 검증, 상태 머신, flock, 비동기 실행, 중복 요청 보호 |
| `chatgpt-mcp/host_runtime_tools.py` (신규) | P0 5개 MCP 도구의 입력/출력 JSON schema 및 annotations |
| `chatgpt-mcp/approve-host-operation.py` (신규) | 실제 호스트 TTY 승인 및 단일 영수증 작성 |
| `chatgpt-mcp/server.py` | 새 도구 등록/dispatch, project 검증, 기존 작업 lock·activity 연동 |
| `chatgpt-mcp/project_state.py` | 새 도구명을 활동 기록 허용 목록에 추가(기존 내용/보안 유지) |
| `chatgpt-mcp/install-runtime.py` | **모든 신규 필수 Python 파일**을 불변 runtime manifest에 추가 |
| `chatgpt-mcp/tests/test_host_runtime*.py` (신규) | fake Docker/파일시스템/TTY/승인·드리프트·중단·취소 테스트 |
| `chatgpt-mcp/tests/test_router.py`, `test_runtime_install.py` | 도구 노출, 프로젝트 격리, 스냅샷 포장 회귀 |
| `chatgpt-mcp/README.md` | 기능 활성화/승인/상태조회/재배포·실패·되돌리기 운영 가이드 |

`tossApp` 코드 변경은 1차 범위에서 **원칙적으로 하지 않는다.** 필요한 `manage.py` 조회용 진단 기능이 없거나 동작이 불안전하다면 해당 저장소 수정 계획을 별도 명시하고 테스트 후 적용한다. 강제 DB migration·seed를 구현 편의로 추가하지 않는다.

## 7. 단계별 담당 세션 작업 순서

### Phase 0 — 재확인 및 설계 동결 (코드 수정 전)

- [ ] `personal-ops`와 `tossApp`의 Git status 확인; 이미 변경된 파일 목록 보존.
- [ ] 현재 MCP 노출 도구와 `server.py` dispatch, 상태 경로/lock, `install-runtime.py` manifest 재확인.
- [ ] `tossApp` Compose/Dockerfile/manage.py의 **실제 현재 동작**과 API-only 재생성 방법 검증. 코드 추측 금지.
- [ ] 테스트 host 운영 권한이 존재하는지 **읽기 전용으로** 조사, 없다면 mock 테스트만 진행.
- [ ] 정확한 Docker build-context snapshot/승인 구조를 구현 가능하게 구체화한다. 안전하게 고정할 수 없으면 배포 실행을 보류한다.

### Phase 1 — 읽기 전용 P0 도구

- [ ] 설정 parser 및 타깃 allowlist 기본 OFF.
- [ ] `get_runtime_status` 구현: Docker/헬스/스키마 안전한 필드만 반환.
- [ ] `compare_deployment` 구현: 실제 image ID 및 소스/빌드 출처 지문 확인. 모르면 `UNKNOWN`.
- [ ] 타깃 위장, 접근 불능, 악성 config, 민감정보 출력, 잘못된 Docker 응답, 제한 초과 등 테스트.

### Phase 2 — 계획·호스트 승인

- [ ] `prepare_test_deploy`의 작업 ID, 동일 요청 멱등성, 빌드 입력 스냅샷, 컨테이너 identity, 계획 TTL, digest 구현.
- [ ] `approve-host-operation.py`의 독립 TTY 승인, 영수증 권한/nonce/단일 사용 구현.
- [ ] 미승인, 중복 승인, 만료·스냅샷 변경, 심볼릭 링크, 다른 프로젝트·타깃 접근을 모두 거절하는 테스트.

### Phase 3 — 승인된 API-only 배포

- [ ] `execute_test_deploy` + `get_host_operation` 상태 머신과 다중 프로세스 flock 구현.
- [ ] 정확한 image build/모드 지정/API-only recreate/사후 검사 연결.
- [ ] build 실패, API health 실패, worker/db drift, 부분 실패, 프로세스 재시작, timeout·취소·중복 호출 테스트.
- [ ] **실제 Docker 호스트 실행은 별도 사용자 승인 전까지 하지 않고**, fake subprocess·임시 디렉터리·격리 fixture로 테스트.

### Phase 4 — 통합 검증·인계

- [ ] MCP 도구 discovery와 JSON schema, 기존 `start_command` sandbox 및 Git 승인 동작 회귀 통과.
- [ ] `install-runtime.py` 스냅샷 파일 누락/변조 검사 통과.
- [ ] Python unittest/ruff 및 관련 admin UI read-only 회귀 통과.
- [ ] README에 host 설정·보안·작업 상태·승인·회복 절차 기록.
- [ ] 신규 runtime **설치·터널 재연결·Docker 실제 배포는 각각 별도 승인**. 소스 수정만으로 적용 완료라고 주장하지 않음.
- [ ] 완료 보고에 구현 도구·테스트 수/결과·실제 적용 여부·미해결 위험과 다음 수동 절차 기재.

### Phase 5 — P1/P2(선택적 후속, P0 종료 기준과 분리)

- [ ] P1 안전 로그/기반시설 스모크 테스트 구현 및 실기기 미검증 상태 명시.
- [ ] P2 실제 Toss WebView 재생 자동화 여부 별도 승인·환경 준비 후 설계.

## 8. 필수 부정 테스트·완료 판정

### 8.1 공격/실패 시나리오

1. `project=personal-ops` 또는 임의 프로젝트, 타깃/서비스/URL override, `mode=production`/`simulated`/`*`: 모두 거부.
2. 호스트 설정 누락/`enabled=false`, 설정 경로 symlink/퍼미션 완화, 현재 UID 소유 아님: 호스트 실행 거부.
3. 승인 이전 `execute`, ChatGPT가 만든 승인 플래그, 승인 토큰 복사, 다른 계획의 receipt, TTL 만료: 거부.
4. 승인 후 소스 변경(기존 dirty/untracked 포함), Compose/대상 컨테이너 세대/DB 세대 변동: 실행 없이 `STALE_PLAN`.
5. 같은 `request_id`+동일 요청: 같은 계획/operation 반환. 같은 ID+다른 요청: 오류. 성공한 작업 재실행 금지.
6. 두 MCP 세션에서 동시 실행: 단일 타깃 lock으로 한 건만 실행.
7. Docker unavailable·permission denied·timeout, API health failure, 중단 후 상태 불명: 적절한 `failed`/`outcome_unknown`, **성공 오판·자동 재실행 없음**.
8. Docker stdout/stderr에 비밀 문자열/인증서/사용자 ID가 섞여도 모델 응답·활동 기록·승인 계획에 유출되지 않아야 함.
9. 작업 전후 DB/Worker의 ID·시작 시각이 변하면 `succeeded` 불가.
10. 새 도구가 활성화되어도 `start_command`로 호스트 Docker 소켓/`~/.config`/`.env`/`sudo`에 접근 불가.
11. 관리자 HTTP는 여전히 **읽기 전용 작업 관측**이며, 인증된 사용자라도 배포 실행 endpoint가 생기지 않음.

### 8.2 P0 Definition of Done

- [ ] 다섯 P0 도구가 MCP로 노출되며 문서 계약대로만 동작.
- [ ] 읽기 전용 상태/차이 조회는 실제 증빙과 `UNKNOWN`을 구별.
- [ ] 배포 계획만으로 API/DB/Worker가 바뀌지 않음.
- [ ] 임의 CLI/프로젝트·타깃·프로덕션 권한 우회 불가.
- [ ] 실호스트 TTY의 1회 승인 없이는 **빌드·교체가 절대 실행되지 않음**.
- [ ] 승인 지문·현재 소스/Compose/container ID 재검증 후 API만 교체; 사후 검증 전 성공 선언 불가.
- [ ] 안전한 멱등성·lock·중단 상태·로그 제한, 단위/통합/보안 회귀 테스트 통과.
- [ ] 기존 파일/Git/격리 실행/관리 UI와 불변 설치/프로젝트 선택 기능 회귀 없음.
- [ ] 실제 호스트 배포가 미실행이면 **미검증**으로 보고하고 코드를 완료로만 표시. 실호스트 배포 승인과 서비스 운영 적용을 묵시적으로 포함하지 않음.

## 9. 실제 토스 테스트 서버 적용 시 운영 게이트 (구현 이후, 별도 승인)

테스트 스택은 `tarororo-test`이며 API는 호스트 loopback `127.0.0.1:3200`, test DB는 `tarororo_test`. 검증 가능한 승인된 최신 코드로 이미지 빌드 후 **API만** 교체한다. `TARORORO_READING_MONETIZATION_TEST_MODE=toss-demo`를 요청한 경우 실제 API 프로세스의 모드 값을 확인한다. 설정값만 바꿔서는 실행 중 컨테이너가 바뀌지 않는다.

스모크 확인 항목: API 이미지 ID, `/health/ready`, DB 013 준비, 계정별 접근 격리, Worker/DB 동일성. 리딩 `SUCCEEDED` 상태에서 카드 2·3·종합 해제와 영상의 5/5/30초 재생은 **토스 앱에서 인증된 사용자가 별도 직접 확인**해야 한다. 이 확인 전에는 앱 광고 동작 “완료”라고 보고하지 않는다. 영상은 테스트 샘플이며 실제 Toss 광고 SDK/정산을 검증하지 않는다.

문제 발생 시 무조건 반복 재배포하지 않는다. 현재 상태를 재조회하고 `disabled` 모드로 되돌리는 **새 계획·새 1회 승인**을 마련한다. DB volume·유저·리딩·결제 데이터 삭제 금지.

## 10. 담당 세션에 전달할 단일 지시

> `personal-ops` 저장소의 `docs/superpowers/specs/2026-10-12-host-runtime-management.md`를 읽고 **Phase 0~4의 P0 기능만** 구현·테스트해줘. 과거 대화 내용을 전제로 하지 말고 현재 코드를 검증해. 기존 미커밋 변경은 보존하고 임의 host shell·Docker 일반 실행을 추가하지 마. 실행은 정확한 `tossApp/tarororo-test/api` 대상과 실제 호스트 TTY 1회 승인으로만 허용해. 이번 작업에서는 실제 호스트 Docker 배포, 새 MCP 런타임 설치·연결 변경, DB 수정, 토스 콘솔 업로드를 하지 마. 완료 후 변경 파일·검증 결과·호스트 운영 적용에 필요한 별도 승인 절차만 보고해.

## 11. 근거와 확인하지 않은 사항

**현재 소스에서 확인된 것:** `chatgpt-mcp/server.py`의 MCP 라우터, `job_tools.py`와 `jobs.py`의 격리 JobManager, `execution.py` bubblewrap, `git_workflows.py`와 `approve-git-operation.py`의 1회 승인, `install-runtime.py`의 불변 스냅샷 설치, `tossApp/deploy/pc-test/*` 테스트 Docker 정의 및 광고 모드 문서.

**확인하지 않은 것:** 실제 호스트의 현재 Docker 상태·권한·실행 중 API image provenance, 호스트의 토스 테스트 모드, 테스트 DB 013 실제 적용, 현재 토스 앱 업로드 버전, 실기기 광고 영상 재생. 관련 도구로 관찰하기 전까지 결론을 내리지 않는다.

**필수 제품 결정 없이 이미 정한 범위:** 1차 대상은 테스트 API 한정, 인증된 자기 리딩만 허용, 테스트 광고 모드는 opt-in, 운영 차단, 실제 Docker 변경은 호스트 TTY 승인, DB/Worker/실결제/프런트 업로드 범위 제외.

## 12. 후속 P0 구현 기록 (2026-10-12)

이 절은 최초 설계 체크리스트를 대체하지 않으며 **실제 호스트 Docker와 토스 앱 검증을 완료했다는 뜻이 아니다.**

- **수정/생성:** `chatgpt-mcp/host_runtime_config.py`, `host_runtime.py`, `host_runtime_operations.py`, `host_runtime_tools.py`, `approve-host-operation.py`, `server.py`, `project_state.py`, `install-runtime.py`, `tests/test_host_runtime.py`, `README.md`.
- **구현:** exact `tossApp/tarororo-test/api` 도구 다섯 개, 호스트 전용 기본 OFF 설정, 제한된 Docker 상태/스키마 조회, dirty/untracked 포함 컨텍스트 지문·private 복제, 10분 TTL 계획·호스트 TTY 1회 승인, 승인 후 재검증, API-only 재생성, 이미지·DB/Worker 불변 확인, 중복 요청/중단·불확정 결과 분리.
- **검증:** fake Docker 기반 HostRuntime 15/15, 불변 설치 6/6 모두 통과. 접근 가능한 기존 MCP/관리 UI/격리실행/Git 등 선택 회귀 **125건 실행, 실패 0건(2건 skip)**. 이와 별개로 전체 자동 검색 123건 중 환경 의존성 오류가 3건 있었음: `mcp` 모듈 미설치로 `test_mcp`·`test_router` import 실패, Git 검증기 실행 환경이 없어 `test_pinned_canonical_commit_validator_accepts_v1_message` 실패. 이는 성공으로 계산하지 않는다.
- **실제 호스트 검증 미실행:** Docker 실상태, API 빌드·교체, 토스 QR/영상 재생, 새 불변 런타임 설치·재연결, 사용자 등록 없이 실제 광고 화면 확인.
- **운영 전 필수:** 호스트 환경에서 누락된 SDK/검증기를 갖추고 MCP router 전체 회귀를 통과한 후, 신규 코드를 독립 보안 검토하고 실제 API 스테이징을 별도 승인으로 검증한다. 현재 테스트 스냅샷은 Docker/Compose의 실제 렌더링 동작까지 검증하지 않으므로 호스트 opt-in과 배포 실행을 자동으로 진행하지 않는다. 구성 파일 `host-runtime.json` 활성화와 런타임 설치는 별도 승인 없이는 하지 않는다.

## 13. P0 후속 보안 검토 및 호스트 적용 게이트 (2026-10-12)

### 소스 보안 검토 및 조치

1. **호스트 명령 stdout 메모리 무제한 축적**: `subprocess.run(stdout=PIPE)` 후 크기를 검사하는 방식은 상한 적용 전에 메모리를 초과할 수 있었다. `Popen + selectors`로 읽기 중 256 KiB 상한, timeout 시 process group 종료, stdout/stderr 비공개 정책을 적용했다.
2. **다중 MCP 프로세스에서 작업 상태 오판**: 다른 프로세스에서 조회할 때 로컬 `_tasks`만 확인해 실제 진행 중인 `queued/running`을 `outcome_unknown`으로 바꾸는 문제가 있었다. 타깃 `flock`을 큐 등록 전부터 최종 처리까지 보유하고, 작업 조회·새 계획은 잠금의 실제 소유 여부를 먼저 검사하도록 변경했다.
3. **수정 가능한 프로젝트 `manage.py`를 호스트 권한으로 실행**: 소스 해시 승인만으로 임의 Python의 호스트 파일 접근을 허용하지 않도록 직접 실행을 제거했다. 고정 Docker Compose `config/build/up` 및 권한이 제한된 test secret 메타데이터 점검으로 교체했다.
4. **Docker 공용 이미지 태그 재지정 위험**: 이미지 ID를 확인한 후에도 공용 `:local` 태그가 교체될 수 있어 작업별 고유 이미지 태그 및 API `image`만 바꾸는 private Compose override를 추가했다. 두 Compose 렌더링 차이가 API 이미지 외에 존재하면 `UNSAFE_IMAGE_OVERRIDE`로 거부한다. `up` 시 `--no-build --pull never --no-deps`를 사용한다.
5. **변경된 배포 스크립트/설정 반영 위험**: `.dockerignore`, Dockerfile, Compose, `manage.py`, `run.mjs`, package/lockfile 등 고위험 입력이 미커밋 상태면 `REVIEW_REQUIRED`로 계획 생성을 거절한다. 위험 파일은 별도 Git 리뷰·승인 후 다시 계획한다.
6. **10분 계획 만료와 긴 Docker 빌드 충돌**: 계획 유효기간은 승인/시작까지 적용하고, 시작 후 빌드가 지연되더라도 원본 바이트·실행 타깃·환경은 계속 검증하도록 처리했다.

이 검토는 이 채팅에서 수행한 소스 중심 보안 검토다. **별도 인원/모델의 독립 리뷰 결과를 확보했다고 주장하지 않는다.**

### 실제 Docker Compose CLI 검증 — 호스트 데몬/데이터 미사용

선택한 `tossApp`을 격리 실행 환경으로 마운트하고 **Docker Compose v2.35.1의 `config --format json`만 실행**했다. 이때 인증자료 대신 `/tmp`에 만든 빈 시험 파일 경로를 사용했다.

- 실제 출력 기본 서비스는 `api`, `db`, `worker` 3개. `migrate`·`seed`는 profile로 인해 기본 렌더링에서 제외되므로 초기 validator의 잘못된 5개 서비스 가정을 수정했다.
- DB 볼륨 참조는 `source=database`, 상위 볼륨의 실제 name은 `tarororo-test_database`. 초기 validator의 잘못된 볼륨 참조 가정을 수정했다.
- 별도 이미지 override를 추가해 렌더링 결과가 **API image 문자열 하나만 바뀌고 다른 부분은 동일함**을 확인했다(`REAL_COMPOSE_OVERRIDE_MATCH=PASS`).
- 이것은 **Compose 파싱/병합 검증**이며 실제 Docker 데몬 연결·이미지 빌드·컨테이너 재생성·API/DB 접근이 아니다.

### 최신 검증·적용 상태

- 호스트 런타임 소스 Python 문법 검사 성공.
- 접근 가능한 unittest **총 131건 실행, 실패 0건, skip 2건**.
- 격리 실행 환경에 `mcp` Python SDK가 없어 `test_mcp`, `test_router`를 import할 수 없고, Git pinned Change Evidence validator가 없어 해당 검증 1건이 실패한다. 이 **3건은 회귀 합격으로 계산하지 않았다.**
- 실제 소스 26개를 `/home/sandbox`의 일회용 불변 릴리스로 설치/매니페스트 해시 검증하는 smoke 통과(`IMMUTABLE_INSTALLED_RELEASE_CHECK=PASS`). **실제 사용자 홈의 설치본을 업데이트한 것은 아니다.** 최신 소스 기준 격리 패키징 스냅샷은 `679c283d53288b75460b`(26개 파일, 매니페스트 검사 PASS)다. 이전 격리 스냅샷 식별자는 후속 소스 수정으로 최신 상태가 아니다.
- 현재 ChatGPT에 연결된 `personal-ops-tunnel` 도구 목록에는 신규 HostRuntime 5개가 **아직 없으므로** 실제 호스트 API 배포는 여기서 실행할 수 없다.
- `tossApp`의 현 작업 트리에는 `.dockerignore`, `deploy/pc-test/Dockerfile`, `compose.yaml`, `manage.py` 등 고위험 미커밋 수정이 있다. 현재 안전 정책상 **`prepare_test_deploy` 실행 시 `REVIEW_REQUIRED`가 발생하는 상태**다. 이 변경은 독립 리뷰 후 명시적 파일 한정 커밋·승인 없이 우회하지 않는다.
- `personal-ops`도 미커밋 소스가 남아 있으며 원격 main과 이력이 달라져 있다. 자동 reset/merge/push를 하지 않는다.

### 실제 호스트 설치 및 테스트 API 적용 재개 절차 (별도 호스트 실행이 가능한 세션용)

**게이트 A — 실제 호스트 Python SDK 테스트**

```bash
cd ~/ai/opencode/projects/personal-ops
~/.local/share/uv/tools/serena-agent/bin/python -m unittest discover \
  -s chatgpt-mcp/tests -p 'test_*.py' -v
```

MCP/router 및 Git pinned validator 테스트까지 **실제 실행**하고 실패 원인을 검토한다. 테스트가 녹색이 아니면 설치하지 않는다. 독립 보안 리뷰도 이 시점에 완료한다.

**게이트 B — 불변 런타임 설치/연결 (실제 호스트 관리자 작업)**

```bash
cd ~/ai/opencode/projects/personal-ops
python3 chatgpt-mcp/install-runtime.py
# 반환된 server 경로를 확인하고 기존 터널의 alias·tunnel ID를 읽기 전용으로 조사
tunnel-client runtimes status personal-ops-coding --json
```

해당 결과에서 확인한 기존 런타임 식별자와 **호스트에 이미 존재하는** API 키 파일을 사용해 실제 `tunnel-client runtimes connect`를 갱신한다. 추측으로 새 터널을 생성하거나 운영 중인 서비스와 별도로 중복 연결하지 않는다. 연결 후 `healthy/ready/process_running`, MCP 도구 목록에서 신규 5개 노출을 확인한다. **위 명령은 이 세션에서 실제 호스트에 실행하지 않았다.**

**게이트 C — 호스트 대상 활성화·조회**

호스트에 `~/.config/chatgpt-coding-mcp/host-runtime.json`을 0600 모드로 만들고 이 문서 2.1의 **정확한 `tossApp/tarororo-test` 구성**만 기재한다. `enabled=true`는 별도 호스트 승인 후 변경한다. 이후 새 `get_runtime_status`와 `compare_deployment`로 실제 API/Worker/DB 정체성, 전용 테스트 모드, 013 migration 준비를 관찰한다. 민감정보/DB URL을 채팅에 출력하지 않는다.

**게이트 D — 실제 배포**

`tossApp`의 고위험 미커밋 배포 파일을 검토하여 `REVIEW_REQUIRED`를 해소하고, 새 계획을 생성한다. 반환된 `approval_command`를 **실제 호스트 TTY에서 직접 실행**하여 1회 승인한 뒤 `execute_test_deploy`를 실행한다. API 이미지 ID·건강 상태·모드 및 DB/Worker 불변성까지 확인되기 전에는 완료라고 보고하지 않는다. Toss 실기기의 샘플 영상 재생 5/5/30초 검사는 별도 최종 게이트다.

**현재 중단 경계:** 코드 및 Compose 파싱 검증은 수행했지만 게이트 A의 전체 SDK 검증, 게이트 B의 실제 터널 재설치, 게이트 C/D의 호스트 Docker/DB 접근은 이 대화에 제공된 도구 권한으로 실행할 수 없어 **미실행**이다. 임의 샌드박스 탈출, 기존 Git 승인 도구 오용, DB 삭제·강제 재시작으로 대체하지 않는다.

## 14. 추가 보안 하드닝 및 재검증 (2026-10-12)

### 변경 사항

- **불변 실행기 우회 차단:** 이전에는 `HostRuntimeOperations(..., approval_script=...)`에 값만 제공하면 불변 설치 검사가 생략될 수 있었다. 이제 실제 승인 CLI처럼 `approval_script`만 지정한 경우에는 불변 설치 검증이 필수다. 모의 테스트에서 가짜 실행기·승인 스크립트를 **함께** 주입할 때만 테스트 경로를 허용한다. MCP 서버에서는 이를 주입하지 않는다.
- **Git 숨김 플래그 검증:** `git status`에서 숨길 수 있는 `assume-unchanged` / `skip-worktree`로 인한 고위험 Docker 입력 파일의 검토 누락을 방지했다. `git ls-files -v`, `git ls-tree`, `git cat-file blob HEAD:<고정경로>`로 Git index 플래그·파일 종류·실제 바이트·실행 비트를 검증한다. 차이가 있으면 `REVIEW_REQUIRED`로 거부한다.
- **정책 JSON 보호:** `server/config/commerce-policy.json`, `server/config/credit-policy.json`도 고위험 Git HEAD 고정 대상에 포함했다.
- **호스트 승인 정보 보강:** 호스트 TTY 승인 화면에 `Git HEAD`도 표시하며 기존 source SHA-256, 예상 영향·plan digest·만료 시각은 유지한다.
- **프로젝트 영향 확인:** `tossApp`의 현재 미커밋 고위험 입력은 `.dockerignore`, `deploy/pc-test/Dockerfile`, `compose.yaml`, `manage.py`, `server/package.json`이다. 이 범위는 별도 검토·커밋·호스트 승인이 필요하다. **기존 사용자 작업을 일괄 커밋하거나 초기화하지 않았다.**

### 실행 증빙

- 새로운 고위험 Git 검증과 승인 우회 방지 테스트를 포함해 **HostRuntime 단위 23건 PASS**, 기존 불변 런타임 6건 PASS.
- 전체 자동 발견 136건 중 **실행 가능 133건: 오류 0건, 2건 Skip**. 기존 3건(MCP SDK 미설치 2건, pinned Git validator 실행환경 1건)은 격리 환경에서 실행/검증하지 못했다.
- Python 문법 검사 성공.
- 최신 26개 파일을 **격리 환경에서만** 불변 스냅샷 패키징·해시 검증했고, `approve-host-operation.py --help`가 설치본 Python으로 로드되는 것까지 확인. 최신 격리 스냅샷 ID: `c57b5d4eae259396e71b`.
- **실제 호스트**에서 SDK를 갖춘 전체 테스트, Docker 데몬/API 조회, 터널 설치/재연결, HostRuntime 활성화, API 교체, 토스 실기기 검증은 **전부 미실행**. 격리 스냅샷 ID는 호스트 설치 경로/작업 완료 증거가 아니다.

### 재개 시 가장 중요한 선행 조건

호스트의 `~/.local/share/uv/tools/serena-agent/bin/python`으로 MCP/router 전체 테스트를 통과하고, 독립 코드/보안 리뷰가 완료되어야 한다. 그 다음 실제 불변 런타임을 설치하고 기존 터널 연결에 반영할 수 있다. 현재 대화에서 노출된 `personal-ops-tunnel` 도구는 격리 명령 실행만 제공하므로 **호스트 Docker/터널 갱신을 대신 실행할 수 없다**.

특히 `tossApp`의 고위험 미커밋 변경과 서브트리 작업을 한꺼번에 커밋하지 말고, 담당 세션이 파일별 영향 검토와 범위 제한 승인 후 `REVIEW_REQUIRED`를 해소해야 한다.
