# Work dashboard

기존 관리 화면에 삽입하는 업무 작업실 UI다. 별도 서버·의존성·LLM 호출 없이
호스트가 전달한 MCP 호출·명령 작업과 다른 MCP·에이전트의 상태 보고를 표시한다.

- `model.js`: 순수 `snapshot({project, jobs, events, records, objects, now})` 함수. 브라우저 전역
  `WorkDashboardModel`과 CommonJS로 제공한다. 프로젝트 필터, 호출 ID별 최신 단계,
  명령 상태, 종료 신호가 없는 공통 이벤트의 2분 만료 상태를 정규화한다. `actors`는 고정 객체,
  `tasks`는 호출·명령·에이전트 일감이다. `counts`는 일감 개수이며 인물 수와 구별한다.
- `visualizer.js`: `WorkDashboard.create(container, {onJobSelect})`로 생성한다.
  반환 객체의 `update({project, jobs, events, records, following, error})`, `clear(project)`,
  `setLive(boolean)`, `destroy()`를 호출한다. API 요청이나 인증 정보 처리는 하지 않는다.
- `visualizer.css`: 방 배치, 객체 이동, 상태 색상, 모바일 및 모션 감소 지원.
- `motion.js`: 인물의 발 위치와 문·복도 경로를 관리하는 순수 모듈. 같은 ID의 자리를
  유지하고 관측 기록 재생과 실제 관측 상태를 구분한다.
- `pixels.js`: 바닥·벽·가구와 4방향 픽셀 인물을 Canvas에 그린다. 외부 이미지 없이
  정적 맵을 캐시하고 걷기·대기와 읽기·검색·코딩·빌드·검사·검토·실행 동작을 표시한다.
  명시적인 커밋·전송·추론 이벤트에는 해당 소품을 사용한다.

데이터는 `chatgpt-mcp`의 `/api/jobs`와 `/api/activity` 응답에서 가져온다.
호스트가 인증, 조회 주기, 프로젝트 전환과 오래된 응답 폐기를 담당한다.
이 모듈은 실제 ChatGPT 모델·추론, 다른 MCP, 대화별 업무 ID를 추정하지 않는다.
다른 MCP는 전달된 고정 식별자로 별도 객체를 만든다. 호스트 직접 기록은 `직접 관측`,
HTTP·채팅 도구로 받은 기록은 `보고됨`으로 표시하고 보고 객체의 머리 위에도 `보고`를 붙인다.
MCP 요청 반환과 명령 성공을 구별하며 명령 성공은 `succeeded` 및 종료 코드 0이 필요하다.
터널의 반복 호출은 `코딩 MCP` 한 객체에, 명령은 `명령 실행기` 한 객체에 묶인다.
현재 일감은 인물 위에 표시하고 완료 일감은 보관실·확인 필요 방에 문서로 남긴다.
관리 API는 알려진 실행 파일·하위 명령에서 `action` 요약만 추출하므로 이름이 `직접 명령 실행`인
빌드·검사도 구별할 수 있다. 원본 인자·환경 변수는 내보내지 않으며 셸 코드 문자열은 해석하지 않는다.
모르는 명령은 일반 실행, Git dry-run/help는 검토 동작으로 표시한다.
객체의 작업이 끝나면 같은 인물이 대기실로 돌아온다. 실패는 일감 상태이며 서버 고장을 뜻하지 않는다.
동시 일감은 하나의 객체에 여러 개 연결하며 진행 중 일감을 대기열보다 먼저 표시한다.
객체는 최대 24개, 방마다 인물·완료 문서는 각각 6개까지 그리고 방 버튼에서 나머지 일감을 조회한다.
최근 범위에서 기록이 밀려나도 현재 프로젝트에서 관측한 객체는 유지하며 `최근 범위 기록 없음`으로 표시한다.
이는 프로세스 생존이나 연결 상태를 확인했다는 뜻이 아니다. 프로젝트 전환·로그아웃은 객체 목록도 비운다.

2초 폴링 사이에 끝난 짧은 호출은 시작·종료 쌍을 확인한 뒤 이동을 **관측 기록 재생**으로 표시한다.
첫 조회에서는 인물을 현재 작업실 또는 대기실에 놓고 완료 일감은 문서로 표시한다.
최근 활동 재생 버튼은 대기 중인 기존 객체가 최근 일감을 처리하고 보관한 뒤 돌아오는 장면을 보여준다.
작업 동작은 약 1초 유지하며 새 실제 작업이 들어오면 과거 기록 재생을 중단한다.
재생은 실제 명령을 실행하지 않고 일감 상태나 진행 개수를 변경하지 않는다.
새 활성 작업과 관측된 상태 변경은 문과 공용 복도를 따라 이동한다. 실시간 작업 포즈는 활성 기록에만
사용하며, 관측 기록 재생에서는 과거 처리 장면을 보여준다.
조회 실패 시 마지막 성공 데이터를 유지하고 움직임을 멈춘다. 프로젝트 전환·로그아웃은 캐시를 비운다.

지도는 확대·축소·전체 보기, 마우스 끌기·스크롤·방향키로 둘러본다. 모바일 기본 확대는 80%이며
최근 작업이 있는 방을 먼저 보여준다. 동작 감소 설정에서는 위치를 즉시 갱신한다.
이동 중에는 프레임마다 갱신하고 정지 상태에서는 약 10fps로 낮추며 숨긴 탭·일시정지에는 갱신하지 않는다.
재생 중복 확인 ID는 최대 512개로 제한한다.

## 다른 MCP와 에이전트 수집

기존 터널은 `jobs/events`를 직접 수집한다. ChatGPT가 별도 연결로 호출한 Figma·PDF·Notion 등의
클라우드 MCP는 호스트를 지나지 않으므로 자동 관측할 수 없다. 새 `report_work_events(project, events)`
도구로 실제 호출의 시작·반환·실패를 보고하면 작업실에 해당 MCP가 나타난다.
서버 지침이 보고를 안내하지만 클라이언트의 모든 호출에 보고가 수행된다고 보장하지 않는다.

외부 수집기는 인증된 `POST /api/work-events?project=tossApp`에 `{"events":[...]}`를 보낼 수 있다.
한 번에 1~20개를 받고 전체 검증 후 하나의 잠금 안에서 저장한다. 잘못된 배치는 저장하지 않는다.
허용 프로젝트·인증·Host/Origin 검사와 기존 200 KiB 요청 제한을 적용하며 실행 명령은 받지 않는다.
원격 입력이 `provenance: observed`를 보내도 서버는 `reported`로 저장한다.
직접 관측하는 호스트 수집기는 로컬 `ProjectState.record_work_events(..., provenance='observed')`를 사용한다.
조회는 기존 `/api/activity`의 `records`에 최근 200개를 추가하므로 기존 `events` 호출자는 호환된다.

Codex CLI·Claude CLI의 후크 수집기는 아직 활성화하지 않았다. 다음 수집기는 도구 목록이나 모델 이름으로
인물을 추측하지 않고, 실제 세션·에이전트 식별자를 아래 `records` 형식으로 전달하면 된다.
같은 로컬 에이전트 ID를 여러 세션에서 재사용한다면 `actor_id`에 세션 ID도 포함한다.

```js
{
  schema_version: 1,
  project: 'tossApp',
  source_id: 'codex-local',       // 수집기/설치 인스턴스의 고정 ID
  source_kind: 'codex-cli',      // mcp | chatgpt | codex-cli | claude-cli
  actor_id: 'session-1-agent-1',  // 실제 객체의 고정 ID
  actor_kind: 'agent',           // mcp | executor | agent
  actor_label: 'Codex',
  task_id: 'tool-call-1',         // 객체 안에서 고유한 일감 ID
  task_kind: 'agent',            // call | job | agent
  task_label: '파일 수정',
  action: 'code',
  state: 'running',
  time: '2026-10-12T01:00:00Z'
}
```

`action`: read, search, reason, code, build, test, review, execute, commit, push.
`state`: started, running, queued, canceling, returned, succeeded, failed, timed_out,
canceled, interrupted, unknown. 호출 반환은 `returned`; 명령 성공은 `succeeded`와
`exit_code: 0`을 함께 보내야 한다. CLI 일감의 `succeeded`는 수집기가 실제 완료를 확인한 경우에만 보낸다.
종료를 확인할 수 없으면 `unknown`을 전달한다. 작업명·프로필에 따른 동작 분류는 표시용이며
실제 명령 인자나 완료 비율을 추정하지 않는다.

선택적인 `parent_task_id`, `parent_actor_id`, `parent_source_id`는 다른 에이전트·MCP로 전달한 일감 관계를 보존한다.
생략한 부모 객체·수집기 ID는 같은 객체·수집기를 뜻한다. `source_id/actor_id/task_id`를 각각 이스케이프하여
다른 수집기·에이전트의 같은 이름이나 로컬 일감 ID가 충돌하지 않도록 한다.
같은 일감의 상태 이벤트는 최신 기록 하나로 합친다. 모르는 버전·출처 종류·액션·잘못된 ID는 제외한다.
호출에 `succeeded`를 보내거나 명령·에이전트 일감에 `returned`를 보내는 조합도 제외한다.
같은 시각에 성공·실패 기록이 충돌하면 실패를 우선한다. 표시 한도 밖 객체의 일감도 상세 조회할 수 있다.
인자·환경 변수 등 미등록 필드는 결과에 복사하지 않는다. 인증은 호스트가 담당하며 이 형식은 실행 권한을 주지 않는다.

서버는 `received_at`과 `provenance`를 직접 부여한다. 활성 이벤트는 이벤트 시각과 수신 시각 중
빠른 시각에서 최대 120초까지만 유효하며, 더 짧은 `expires_at`을 지정할 수 있다. 계속 진행되는
작업은 같은 task ID로 새 상태를 보고해야 한다. 종료 기록 없이 만료되면 `미확인`으로 바뀌며 성공을 추정하지 않는다.
클럭 오차는 최대 5분 미래까지 허용하되 미래 시각으로 유효 기간을 연장할 수 없다.

선택적인 모델 메타데이터는 `model`, `reasoning_effort`, `usage`, `usage_scope`다.
`usage`에는 제공된 `input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`만 포함한다.
집계 범위는 `request`, `turn`, `session`, `task` 중 하나를 반드시 지정한다. 누락과 0은 구분하며
별도 요청의 토큰을 합산하거나 추론 토큰을 출력 토큰에 더하지 않는다. 객체를 선택하면 각 값의
출처와 기록 시각을 확인할 수 있다. 모델이 바뀌면 이전 모델의 추론 수준·사용량을 새 모델에 붙이지 않는다.
MCP 서버에 별도의 추론 모델이 있다고 가정하지 않으며 모델 이름마다 인물을 만들지 않는다.

`reason`은 명시적인 추론 항목 시작·종료 신호를 받은 경우에만 사용한다. 일반 도구 호출 사이의
빈 시간, 높은 추론 설정, `thinking.enabled`는 추론 중이라는 근거가 아니다. 추론 내용은 수집하지 않는다.
[Codex 후크](https://learn.chatgpt.com/docs/hooks#common-input-fields)는 세션·모델·도구 호출 메타데이터를,
[Claude 후크](https://code.claude.com/docs/en/hooks#common-input-fields)는 일부 적용 effort를 제공하지만
일반 후크에 추론 시작·종료가 모두 있는 것은 아니다. Codex의 명시적인 추론 항목과 토큰 이벤트는
별도 [App Server 이벤트](https://learn.chatgpt.com/docs/app-server#events) 어댑터 대상이다.
Codex 후크를 추가할 때는 사용자가 [`/hooks`에서 정의를 검토하고 신뢰](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)해야 한다.

`objects`는 같은 프로젝트에서 이전에 관측한 `actors`의 식별 메타데이터를 전달하는 옵션이다.
상태·일감은 해당 조회의 기록에서만 계산하며 이전 `objects`의 실행 상태를 되살리지 않는다.
관리 UI가 프로젝트별 목록을 유지하므로 일반 호출자는 이 옵션을 관리할 필요가 없다.

```text
Tunnel jobs/events -> adapter ---------------+
Other MCP reports -> tool / authenticated API +-> private records -> snapshot -> RPG UI
Codex / Claude    -> adapter (future) --------+
```

```bash
rtk proxy node --test work-dashboard/tests/*.test.js
rtk proxy python3 chatgpt-mcp/tests/check_admin_ui.py
```

JavaScript 검증에는 Node.js 24.12 이상을 사용한다.

배포는 `chatgpt-mcp/install-runtime.py`가 정해진 JS/CSS 다섯 파일을 비공개 스냅샷에 복사한다.
작업 가능한 저장소 경로에서 라이브 UI를 직접 실행하지 않는다.
