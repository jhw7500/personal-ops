# personal-ops-tunnel 롤아웃 복구 기록

2026-10-13. 원래 인계: `2026-10-13-tunnel-rollout-agent-handoff.md`.

**09:25 KST 최종 상태:** 호스트 전환·관리 웹 검증 완료. MCP는 `c81522954a091864c42d`,
admin은 목록 API 수정만 포함한 `1dfc5fcaf2cc707701be`다.
현재 커넥터 도구 7/7과 Worker 입력 스키마 노출을 확인했고,
신규 `get_db_migration_status`의 실제 읽기 전용 호출도 성공했다.

## 확인된 원인

진단 당시 세 경로가 서로 달랐다. `healthy/ready`는 새 MCP 프로세스 실행을 증명하지 않는다.

| 대상 | 확인된 스냅샷 | 상태 |
| --- | --- | --- |
| 실제 MCP PID 701661 | `c57b5d4eae259396e71b` | 기존 호스트 도구 5개 |
| 터널 JSON 프로필 | `5ca41f9dd8d96104e53f` | 새 경로로 변경됐지만 프로세스는 그대로 |
| systemd tunnel ExecStart | `0b6ead3b0281c28dbea8` | 재시작 시 예전 코드로 돌아갈 수 있음 |
| 실제 admin PID 3539645 | `10c5f15b9f8caf4ca602` | 기존 화면, 승인 UI 없음 |
| 검증 대상 소스 | `c81522954a091864c42d` | 런타임 파일 33개, 아직 설치·활성화하지 않음 |

터널 PID는 701624, 터널 소유 Codex app-server PID는 701664다. tunnel unit은
oneshot/active-exited로 MainPID=0이며 실제 daemon은 다른 cgroup에 있다.
따라서 unit MainPID만 확인하거나 connect를 다시 호출하는 것으로 전환을 판정할 수 없다.
명시적 alias ExecStop과 실제 `/proc` PID·시작 tick·argv·manifest 검증이 필요하다.

기존 롤아웃은 connect 출력/오류를 버리고 건강 상태만 확인해 `switched=True`를
반환했다. admin 전환도 실제 Serena Python 경로를 거부했다.
이전 실패의 정확한 종료 분기와 터미널 창 종료 원인은 유실된 로그로 확정할 수 없다.
소스에는 부모 터미널을 종료하는 코드가 없었으며 subprocess 그룹 격리가 없었다.

## 준비한 수정

- `rollout-host-runtime.py`: 소스·회귀 검사 입력 고정, 두 서비스의 읽기 전용
  preflight, 실제 프로세스 검증, 전환 단계/결과/종료 코드 기록.
- `rollout_tunnel.py`: 기존 unit/profile의 MCP 명령만 변경. 별도 stop/start,
  이전 MCP·터널 소유 Codex 종료 확인, 새 PID/시작 tick/manifest 확인.
  설정·프로세스 세대가 바뀌면 다른 운영자의 변경을 보존한다.
  복구 대상은 설정 파일의 오래된 경로가 아니라 실제 이전 스냅샷이다.
- `rollout_admin.py`: 정확한 Serena interpreter와 기존 LAN/HTTPS 옵션 보존.
  실제 MainPID와 승인 화면 확인. 검증된 이전 화면으로 복구하거나,
  소유한 세대를 증명할 수 없으면 부분 전환과 수동 복구 필요를 명시한다.
  MainPID=0 복구는 안정된 inactive/failed 상태이며 pending systemd Job이 없을 때만 한다.
- `rollout_diagnostics.py`: 새 subprocess session, 출력/시간 제한, 생성한 그룹의
  자식 정리. PID 재사용을 막기 위해 cleanup 신호 전까지 leader를 unreaped로 유지한다.
  원문 argv/env/stdout/stderr/상태 로그/credential/tunnel ID를 저장하지 않는다.
  영구 receipt는 디렉터리 0700, 파일 0600. 기록 실패도 원래 오류/취소를 덮지 않는다.
- `rollout_catalog.py`: 문서화된 로컬 `/health/mcp`의 수동적 도구 발견 기록만 조회한다.
  완전한 실제 목록이 기대 도구와 다르면 transaction 안에서 실패·복구한다.
  미발견은 `awaiting_discovery`이며 성공으로 표시하지 않는다.
- `configure-host-runtime.py --enable --extended`: 별도 TTY 확인으로 v2 범위를
  활성화할 수 있다. 설정만 바꾸며 Docker/DB 실행이나 Schema 013 적용은 하지 않는다.

이전에 전달한 Codex/Claude 수집기와 대시보드 변경을 포함해 기존 기능을 보존했다.
원래 저장소의 사용자 변경과 병합했으며 reset/checkout/commit/push는 하지 않았다.
작업 중간 백업과 SHA 비교 기록은 아래 private proof 디렉터리에 있다.

## 검증된 사전 결과

- 원래 저장소에서 대상 검사 **141/141**, 전체 검사 **277/277** 통과.
  실제 호스트 읽기 전용 사전검사도 통과했으며 실행 종료 코드는 0이다.
- 최종 receipt:
  `/home/jhw/.local/state/chatgpt-coding-mcp/rollouts/rollout-9b8572ju/receipt.json`
  (소유자 확인된 일반 파일, mode 0600). `installed=false`, `switched=false`다.
- 독립 코드 리뷰 `APPROVE`, 아키텍처 리뷰 `CLEAR`. 두 판정은 적용 코드에 한정되며
  사용자 적용 승인이나 운영 커넥터·웹 검증을 대신하지 않는다.
- 2026-10-13 약 02:58 KST 읽기 전용 확인: 저장소 job 363개가 모두 종료 상태이며
  실행 중 job 및 호스트 operation은 없다. 적용 직전 다시 확인한다.
- 당시 loopback/LAN/Tailscale HTTPS 모두 HTTP 200이나 승인 UI는 표시되지 않았다.
  당시 실제 MCP와 admin PID·시작 tick은 위 기존 버전 그대로였다.

## 승인된 적용

사용자가 운영 적용을 승인했다. 새 릴리스 설치, tunnel stop/start,
admin restart, v2 설정 활성화를 진행했으며 DB/컨테이너 변경은 하지 않았다.
아래 적용 명령은 승인된 범위에서 실행했고 실제 진행 상태는 뒤에 기록한다.

```sh
rtk proxy script -q -e -c \
  '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply' /dev/null
```

이 명령은 TTY에서 `ROLLOUT` 확인을 요구한다. immutable snapshot을 추가하고 기존
profile/unit의 MCP 명령을 맞춘 뒤 tunnel stop/start 및 admin restart를 수행한다.
터널 소유 Codex app-server도 연결이 끊긴다. 현재 진단 세션의 별도 공유 Codex
PID 233826과 observer PID 4103034는 제어 대상이 아니다.

v2 설정까지 승인한 경우에만 별도로 실행한다.

```sh
rtk proxy script -q -e -c \
  '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/configure-host-runtime.py --enable --extended' /dev/null
```

확인 문구는 `ENABLE EXTENDED`. 변경 전 호스트 설정은 private 백업하며 credential,
네트워크, Tailscale, Docker 컨테이너/이미지/볼륨/DB 데이터를 변경하지 않는다.
적용 직전 진행 중인 MCP jobs/호스트 작업과 frozen PID/config를 다시 검사한다.

2026-10-13 사용자가 런타임·웹·v2 설정 적용을 승인했다. RTK의 직접 proxy는
stdin은 TTY지만 stdout은 pipe이므로 `HOST_TTY_REQUIRED`에서 중단된다.
첫 승인 실행 receipt `rollout-2bn9kh1u/receipt.json`에 이 실패가 보존됐으며,
설치·서비스 전환 전 실패였다. 위 `script`는 실제 TTY 입출력을 유지하고
종료 코드를 전달한다. 타입 확인과 사용자 승인을 우회하지 않는다.
설정 사전 백업은 private proof 아래 `activation-spy9mm16/`에 저장했다.

### 운영 전환 및 후속 수정

- `rollout-zvq0xov4/receipt.json`: MCP `c81522954a091864c42d`로 전환 성공.
  실제 MCP PID 3922666 / start tick 4376401, tunnel PID 3922612 / tick 4376397.
  profile/unit 모두 실제 MCP와 정렬됐으며 기존 tunnel/MCP/Codex 세대는 종료됐다.
- admin 첫 전환은 systemd의 cached DropInPaths 때문에 `ADMIN_UNIT_CHANGED`였다.
  복구가 성공했다. reload 전에는 정확한 기존/이미 보이는 설정을 검사하고,
  reload 후 새 argv/context를 검증하도록 helper를 수정했다.
  cache 교체·삭제 회귀 검사 2개를 추가했고 관련 45개 검사와 두 독립 리뷰를 통과했다.
- `rollout-i01oop6l/receipt.json`: admin-only 후속 전환 성공, actual PID 4079799,
  release `c81522954a091864c42d`. 기존 LAN/HTTPS 옵션을 유지했다.
- host-runtime 설정은 version 2, enabled=true, mode 0600이다. 013 실행은 하지 않았다.
- 실제 브라우저 로그인과 승인 패널 노출은 확인했지만
  `/api/host-operations?project=tossApp` GET 경로가 빠져 목록 조회가 404였다.
  인증된 tossApp의 GET만 허용하는 분기를 추가해 수정했다. 다른 허용 프로젝트는
  manager 호출 없이 404, 비인증 401, POST 405다. 승인 Origin 검사는 유지했다.
- 컨테이너 ID·이미지·시작 시각 및 migration metadata가 baseline과 동일함을 확인했다.
  current Codex PID 233826과 observer PID 4103034의 시작 tick도 그대로다.

### 최종 운영 검증

| 대상 | 실제 런타임 | PID | 결과 |
| --- | --- | --- | --- |
| MCP | `c81522954a091864c42d` | 3922666 | argv/시작 tick/manifest 검증, healthy |
| tunnel | MCP와 같은 profile/unit 명령 | 3922612 | 프로필의 MCP 외 모든 값과 base unit 보존 |
| admin | `1dfc5fcaf2cc707701be` | 4112023 | 실제 argv와 33개 manifest 검증, active |
| 공유 Codex / observer | 기존 세대 | 233826 / 4103034 | 시작 tick 동일, 재시작하지 않음 |

- 최종 소스 대상 검사 **144/144**, 전체 검사 **280/280**, Ruff, diff check 통과.
  receipt: `rollout-6cag8uwp/receipt.json`. 최종 두 변경은 독립 코드/아키텍처 리뷰 통과.
- admin-only 설치/전환 receipt `rollout-ek6n0b6u/receipt.json`, exit 0.
  이전 MCP 스냅샷 대비 런타임 차이는 `admin_server.py` 한 파일뿐이다.
  정상 MCP를 재시작하지 않도록 admin만 새 릴리스에 연결했다.
- 실제 Chromium으로 loopback/LAN/Tailscale HTTPS 각각 로그인, tossApp 선택,
  승인 패널·대기 목록 HTTP 200, 배포 계획 0건, 승인 버튼 disabled,
  정적 JS hash 일치, page error 0, password input 초기화 확인.
- 실제 HTTP 경계: 비인증 GET 401, 다른 프로젝트 GET 404,
  LAN 승인 POST 403, 외부 Origin 승인 POST 403. 승인/배포 실행은 하지 않았다.
- 최종 컨테이너 ID·이미지·시작 시각과 migration metadata가 baseline과 동일하다.
  Docker mutation/DB write는 수행하지 않았으며 013은 여전히 missing, 선행 일치 5/12다.
- 03:35에는 이 세션의 신규 DB 도구가 없었으나, 09:25 현재 도구 7/7과
  `prepare_test_deploy.component = api|worker` 입력 스키마가 노출됐다.
  실제 get_db_migration_status 호출 결과 013 missing, history 없음,
  필수 테이블 4개 없음, 선행 일치 5/12, prerequisites_ok=false를 확인했다.
  Docker/DB 변경이나 계획 생성·실행은 하지 않았다.
- `/health/mcp`는 여전히 `awaiting_discovery`다. 수동적 발견 기록의 tool_count=0은
  운영 도구 0개를 뜻하지 않는다. 실제 커넥터 스키마 노출과 성공한 신규 도구 호출이
  등록 검증의 근거다. passive health 대기 때문에 호스트 재시작을 반복하지 않는다.
- 관리 UI의 로컬 전용 설명 문구는 지정된 tailnet HTTPS 승인 허용을 설명하지 못한다.
  실제 서버 정책은 loopback/지정 HTTPS만 허용하고 LAN 승인은 거부한다.

## 실제 커넥터 검증

정상 tunnel health가 도구 import를 뜻하지는 않는다. 도구 7개와
`prepare_test_deploy.component = api|worker`는 격리된 실제 MCP 연결에서 검증했다.
09:25 현재 운영 actual PID/snapshot을 재확인했고, 커넥터의 호스트 도구 7/7,
두 신규 DB 도구, Worker component 입력 스키마와 실제 읽기 전용 DB 진단 호출도 검증했다.

공식 절차는 [ChatGPT Plugins의 기존 연결에서 Refresh 후 새 대화](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)다.
지원되는 기존 ChatGPT 인증 세션의 raw `tools/list` API는 확인되지 않았다.
stdio를 가로채거나 별도 테스트 프로세스를 운영 프로세스라고 주장하지 않는다.
수동적 health 정보에는 input schema가 없으므로
`connector_schemas_verified=False`와 CLI 종료 코드 2(`verification_required`)가
남을 수 있다. 이는 호스트 전환 실패와 도구 발견 대기를 구분하기 위한 것이다.

## DB 보존 및 별도 작업

읽기 전용 확인 결과 API/Worker/DB가 running이며 API health는 healthy/ready다.
Schema 013은 `missing`, 현재 소스와 일치하는 선행 checksum은 **5/12**다.
후속 읽기 전용 조사에서 001~005가 모두 일치하고, 006~012는 이력이 없으며
관련 신규 테이블 14개도 없음을 확인했다. checksum 불일치 기록은 없다.
013 이력과 신규 테이블 4개도 없다. 현재 기본 runner는 001~013 등록 목록을
모두 순회하므로 006~012만 적용하려고 일반 db:migrate를 실행하면 안 된다.
013 적용 승인에는 선행 이력 검증, 백업/복원 검증이 필요하다.
이번 작업에서는 migration, DB restore, Docker build/up/restart를 수행하지 않는다.

다음 personal-ops-tunnel 채팅용 인계 스냅샷은 tossApp 로컬 저장소의
`/home/jhw/ai/opencode/projects/tossApp/docs/handoffs/tarororo-test-schema-preflight.md`다.
MCP read_file(project=tossApp)로 실제 읽기 성공을 확인했다.
현재 고정 도구는 006~012 적용을 지원하지 않는다는 점, 고위험 source/HEAD 검증,
별도 승인과 DB 보존 조건, 이후 검토할 실행 범위를 포함한다.

## 로그와 이어서 할 일

- 영구 롤아웃 로그: `~/.local/state/chatgpt-coding-mcp/rollouts/rollout-*/receipt.json`
  — 단계, typed error/cause, 반환 코드, 출력 크기/hash, 실제 PID/snapshot, 최종 종료 코드.
- `--apply`의 오류/검증 대기 시 기본 Enter 대기로 창을 유지한다.
  자동 실행이 필요하면 `--no-pause-on-error`를 사용한다. 취소는 대기하지 않는다.
- 진단·소스 백업/DB metadata baseline:
  `/home/jhw/.local/state/personal-ops-rollout-recovery/20261013/recovery-30m731kf/`
  `live-runtime-final.json`, `docker-db-after-admin-ui.json`,
  `activation-spy9mm16/browser-ui-deployed-result.json`,
  `activation-spy9mm16/http-boundary-final.json`, `connector-verification-20261013.json` 및
  토큰 입력이 비어 있는 승인 패널 스크린샷을 0600으로 보관했다.
- 런타임 롤아웃·도구 등록·관리 웹 검증과 선행 이력 분석은 완료됐다.
  별도 DB 업무는 006~012에 한정된 실행 경로·백업/복원·승인 계획의 검토다.
  Schema 013은 선행 12개 모두 일치, 백업/복원 검증 및 별도 사용자 승인 전까지 적용하지 않는다.
