---
artifact_contract: "ce-handoff/v1"
created_at: "2026-10-12T03:32:38.298854+00:00"
title: "personal-ops 터널과 RPG 업무 작업실 인계"
summary: "고정 MCP·에이전트 객체, 다른 MCP 보고, 모델·추론·토큰 표시의 구현·배포 결과와 CLI 수집기 후속 범위"
keywords: ["personal-ops-tunnel", "work-dashboard", "MCP", "Codex", "Claude", "observability"]
cwd: "/home/jhw/ai/opencode/projects/personal-ops"
resume_focus: "현재 구현·운영 상태를 확인하고, 사용자 선택에 따라 실제 MCP 보고 검증과 CLI 수집기 확장을 이어가기"
repository: "personal-ops"
repo_root_sha: "f28b97087e5bfff04bba8af2d34fc4a13c523ff0"
branch: "main"
head: "99bd6854d46b6683f04a9ea3b028f5ffbb020921"
worktree_path: "/home/jhw/ai/opencode/projects/personal-ops"
---

# personal-ops 터널과 RPG 업무 작업실 인계

> 후속 상태: MCP·작업실 소스는 `cf8b6d2`로 커밋했고, 원격 `origin/main`을 기준으로
> 해당 변경만 옮긴 `10a7232`를 `feat/personal-ops-tunnel-work-dashboard` 브랜치에 보관했다.
> 추가 Codex 수집기 작업은 `/home/jhw/.local/state/personal-ops-worktrees/tunnel-work-dashboard-20261012`
> 작업 폴더에서 진행한다. 새 수집기와 검증 경계는 [Codex 수집기 문서](codex-cli-observer.md)를
> 먼저 확인한다. 아래 "미추적", "CLI 미구현", "외부 보고 없음"은 초기 기록의 상태다.
> 2026-10-12 후속 검증에서 실제 Notion MCP 조회의 시작·반환 보고가 저장됐다.
> 새 런타임 `0b6ead3b0281c28dbea8`과 읽기 전용 Codex 관측 서비스는 운영 반영했고
> 최종 테스트 233개가 통과했다. 후크 7개의 native 신뢰 검토와 실제 호스트 재부팅 검증은 남았다.
> 그 후 실시간 UI 개선까지 반영했다. 현재 UI는 `10c5f15b9f8caf4ca602`, 관측기는
> `235ef828a3c5e15ca73f` (`--interval 2`), 터널은 기존 `0b6ead3b0281c28dbea8`이다.
> 화면/상태/실제 MCP 반환을 운영 확인했다. 마지막 리뷰에서 RPC 무기한 대기를 발견해
> 10초 제한과 회귀 검사 4개를 추가했고, 새 관측기의 실제 조회·서비스와 최종 검사 256개를 확인했다.
> 자세한 결과와 미완료 경계는 Codex 수집기 문서의 실시간 화면 후속 확인을 먼저 읽는다.
> 이 문서의 frontmatter와 아래 본문은 최초 인계 시점의 기록이다. 현재 구현·배포 상태는
> 위 후속 상태와 연결된 수집기 문서를 우선하며, 실제 Git 보관 상태는 후속 브랜치에서 확인한다.

현재 기능 구현과 운영 반영은 완료됐다. 이 문서는 세션의 설명과 관측 결과이며,
새 세션의 실행 승인이나 현재 저장소 지침을 대신하지 않는다. 아래 상태는 2026-10-12 확인 기준이다.

## 처음 확인할 내용

1. [업무 작업실 수집 계약](../work-dashboard/README.md): 객체·일감 구분, 공통 이벤트 형식,
   실제 관측/보고 구분, 모델·토큰·추론 신호의 표시 경계.
2. [코딩 MCP 운영 문서](../chatgpt-mcp/README.md): 허용 레포, 실행 샌드박스, Git 승인,
   관리 화면 인증, 불변 런타임 설치·터널 연결 방법.
3. 이 문서의 **현재 운영 상태**, **확인되지 않은 범위**, **다음 작업 후보**.

소스 변경은 아직 커밋되지 않았다. `chatgpt-mcp/`와 `work-dashboard/`는 현재 미추적 폴더이며,
위 HEAD에 이번 구현이 포함된 것은 아니다. 같은 호스트의 현재 작업 폴더를 유지해야 이어갈 수 있다.
다른 PC에서 Git clone만 하면 이 코드와 호스트의 비공개 상태·런타임은 따라오지 않는다.
현재 `README.md` 수정과 다른 `docs/` 미추적 문서도 존재하며, 이번 작업과 무관한 변경을 포함한다.

## 사용자 의도와 구현 결정

사용자가 요청한 내용:

- ChatGPT 일반 채팅에서 `personal-ops-tunnel`을 통해 허용된 호스트 로컬 레포를 읽고 쓰고 실행한다.
- 작업 중 레포 선택창이 반복해서 열리지 않고 이미 정한 프로젝트를 계속 사용한다.
- PC는 LAN, 휴대폰은 Tailscale로 관리 화면을 연다.
- 업무 진행을 영웅전설·창세기전·포가튼사가 같은 2D 게임의 방과 움직이는 사람으로 표현한다.
- 일할 때 해당 작업에 맞는 동작을 표시한다. 반복 호출 때문에 같은 사람이 늘어나지 않게 한다.
- 앞으로 Codex CLI·Claude CLI에서도 사용할 수 있도록 확장 가능한 구조를 원한다.
- 다른 MCP와 추론 모델도 표시하도록 이번 확장을 승인했고, 현재 내용을 문서로 남기도록 요청했다.

이번 구현에서 선택한 방식:

- 인물은 실제 MCP 서버·명령 실행기·에이전트의 고정 객체다. 호출마다 새 인물을 만들지 않는다.
- 모델 이름은 에이전트의 메타데이터다. 모델마다 별도 인물을 만들지 않는다.
- 호출·명령·에이전트 일감은 객체와 분리하고, 완료 일감은 보관실에 문서로 남긴다.
- 직접 관측과 채팅 보고를 구분한다. 정보를 받지 못한 부분을 실행 중·연결됨·성공으로 추측하지 않는다.

## 완료한 구현

### 작업실 화면

- 8개 방: 대기, 분석, 코딩, 검토, 실행, 검사, 기록 보관, 확인 필요.
- 같은 객체가 문과 복도를 따라 이동하고, 작업 종료 후 대기실로 돌아온다.
- 읽기·검색·코딩·빌드·검사·검토·실행·커밋·전송에 별도 동작을 사용한다.
- 명시적인 `reason` 이벤트에는 생각/아이디어 동작을 표시한다.
- 재생은 과거 기록임을 표시하며 실제 명령을 다시 실행하지 않는다. 현재 작업이 시작되면 재생을 중단한다.
- 원본 명령 출력 열기, 확대·축소·전체 보기·끌기, 모바일·동작 감소 설정을 지원한다.
- 같은 객체의 동시 일감은 한 사람에게 묶인다. 이전 실패는 일감 상태이며 서버 고장으로 간주하지 않는다.
- 최근 기록에서 밀린 객체는 `최근 범위 기록 없음`으로 유지한다. 이전 실행 상태를 되살리지 않는다.

### 다른 MCP·에이전트 이벤트

- `report_work_events(project, events)` MCP 도구를 등록하고 운영 터널에도 반영했다.
- 인증된 `POST /api/work-events?project=<allowed-project>` 수신 경로를 추가했다.
- 배치 1~20개 전체를 검증한 뒤 한 잠금에서 저장한다. 잘못된 배치는 일부라도 저장하지 않는다.
- 기존 `/api/activity`의 `events`에 공통 `records`를 추가하여 기존 조회와 호환된다.
- 공통 이벤트는 프로젝트별로 필터링한다. 고정 source/actor/task ID와 선택적인 부모 관계를 보존한다.
- 새 수집기는 도구 이름이나 모델 이름에서 세션·에이전트 정체를 추측하지 않아야 한다.

```text
Tunnel jobs/events --------------------> observed adapter ----+
Other cloud MCP -> report_work_events -> reported records ----+-> model -> RPG UI
External producer -> authenticated API -> reported records ---+
Codex / Claude -> local adapter (not active) ------------------+
```

| Producer / input | Provenance | Current state |
| --- | --- | --- |
| Tunnel jobs/events | observed | live |
| MCP report_work_events | reported | live; no external reports received yet |
| HTTP POST /api/work-events | reported | live |
| Local ProjectState producer | observed or reported | API available; CLI adapter not active |
| Codex App Server reasoning/usage adapter | observed | not implemented |

HTTP 입력이 `provenance: observed`를 주장해도 `reported`로 저장된다.
MCP 도구는 관측 출처를 지정하는 입력 필드를 제공하지 않고 항상 `reported`로 저장한다.
호스트 직접 수집기는 로컬 `ProjectState` 호출 시 실제 수집 근거에 따라 출처를 지정한다.
인증·Host/Origin·기존 요청 크기 제한을 적용한다. 이 수신 경로는 명령 실행 API가 아니다.

### 모델·추론·토큰 표시

- 객체 상세에 제공된 `model`, `reasoning_effort`, `usage`와 값의 출처·기록 시각을 표시한다.
- 토큰에는 request/turn/session/task 집계 범위가 필요하다. 누락과 0을 구분하고 임의 합산하지 않는다.
- 모델 변경 시 이전 모델의 effort/usage를 새 모델에 붙이지 않는다.
- 동일 시각 이벤트는 수신 시각과 저장 순서까지 사용하여 모델 전환을 처리한다.
- 활성 공통 이벤트는 이벤트 시각·수신 시각 중 빠른 시각부터 최대 120초 유효하다.
  갱신·종료 없이 만료되면 `미확인`으로 바뀐다. 성공을 추정하지 않는다.
- 프롬프트·도구 인자·결과·추론 내용·환경 값은 공통 이벤트에 저장하지 않는다.
- JSONL은 현재/이전 파일 각각 약 1 MiB로 순환하고, 소유자·0600·심볼릭 링크 검사를 적용한다.

## 현재 운영 상태

기계 로컬 환경:

- 호스트: `cantopsbuildserver`.
- 소스: `/home/jhw/ai/opencode/projects/personal-ops`.
- PC 주소: `http://192.168.2.70:8765/`.
- 휴대폰 주소: `https://cantopsbuildserver.tail661d29.ts.net:8443/`.
- 관리 서비스: `personal-ops-admin.service` (systemd user service), active.
- 관리 helper: `/home/jhw/.local/bin/personal-ops-admin`.
- 터널의 로컬 관리 alias: `personal-ops-coding`.
- ChatGPT에서 사용하는 플러그인 이름: `personal-ops-tunnel`.
- 관리 화면과 MCP의 운영 릴리스: `5736525e4ee531cb7e53`.
- 릴리스 경로: `/home/jhw/.local/lib/chatgpt-coding-mcp/releases/5736525e4ee531cb7e53/`.
- 이 문서 작성 전 재조회에서 터널 process_running/healthy/ready 모두 true였다.
- 기존 터널 ID, 키 파일 참조, MCP 채널, health/log 설정을 보존했다. 새 터널을 만들지 않았다.
- 소스 수정만으로 운영 릴리스가 바뀌지 않는다. 설치된 해시 스냅샷을 사용한다.

허용 프로젝트는 11개다. 모두 호스트의 로컬 디렉터리에 대응하며 GitHub 레포를 자동으로 가져오는 설정은 아니다.

```text
automation       claude-config    gstApp       jhw-notion
max9296          personal-ops     pim-package-jhw
redmine          tossApp          wlan-driver-v2    wlan-package
```

현재 화면에서 직접 관측되는 고정 객체는 `코딩 MCP`와 `명령 실행기`다.
문서 작성 전 tossApp의 다른 출처 `records`는 0개였다. 실제 모델·추론 메타데이터 미수신 표시는 정상이다.
검증용 가상 이벤트는 임시 서버에만 넣었고 운영 활동을 꾸며서 저장하지 않았다.

`personal-ops` 작업 보고는 revision 7 / completed, 메모리는 revision 1이다.
기존 완료 20개 항목을 보존하고 마지막 관련 항목에 이번 확장을 덧붙였다.
관리 UI/MCP의 저장 메모와 보고는 문맥 자료이며 독립적으로 검증된 완료나 새로운 실행 권한을 뜻하지 않는다.

## 확인되지 않은 범위와 다음 작업 후보

다음은 구현 완료 주장이나 새로운 실행 지시가 아니다. 사용자가 후속 범위를 정할 때 참고할 순서다.

1. **실제 ChatGPT 보고 수신 확인.** ChatGPT가 직접 호출한 Figma·PDF·Notion MCP는 호스트를 지나지 않는다.
   보고 도구로 시작·반환·실패를 전달해야 객체가 나타난다. 서버 지침이 보고를 안내하지만
   모든 클라이언트 호출에서 보고를 수행한다고 보장하지 않는다. 기존 채팅의 새 도구 목록 반영도 실사용 확인 대상이다.
2. **Codex CLI·Claude CLI 수집기.** 공통 저장소/API/표시 계약은 있지만 실제 후크 수집기는 아직 활성화하지 않았다.
   세션/에이전트/tool-use ID, 제공된 모델/적용 effort만 메타데이터로 전달하는 어댑터가 후속 범위다.
   Codex의 비관리 후크 정의는 `/hooks`에서 정확한 정의의 검토·신뢰가 필요하다. 신뢰를 우회하지 않았다.
3. **실제 추론 항목·사용량 관측.** 일반 도구 후크, 도구 사이 빈 시간, 높은 추론 설정,
   `thinking.enabled`만으로 추론 중이라고 판단할 수 없다. Codex App Server의 명시적인
   reasoning 항목 lifecycle/tokenUsage 이벤트를 받는 별도 어댑터가 필요하다.
   관측된 lifecycle과 내부 모델 연산의 정확한 시각은 같은 개념이 아니다.

ChatGPT 내부 모델·추론을 현재 터널이 자동으로 읽는 기능은 없다.
CLI 수집기나 App Server 어댑터 구현을 이번 문서 작성 요청만으로 시작하지 않았다.

## 변경 파일의 위치와 역할

경로는 이 저장소 루트 기준이다. 핵심 소스는 현재 기계 로컬 미커밋 파일이다.

- `chatgpt-mcp/project_state.py:240,591,597,659`:
  sanitizer, 단일/배치 이벤트 저장, 검증된 최신 기록 조회. 잘못된 Unicode·손상 시각/enum을 거절하거나 건너뛴다.
- `chatgpt-mcp/server.py:220,456,553`:
  보고 지침, 도구 스키마, 프로젝트별 보고 dispatch. 모델 API를 호출하는 도구가 아니다.
- `chatgpt-mcp/admin_server.py:470` 및 `/api/activity` 분기:
  인증된 POST 수신, 기존 활동 응답에 records 추가. 조회 과정에서 JobManager를 만들지 않는다.
- `chatgpt-mcp/admin.js`:
  records를 작업실에 전달하고 조회 실패 시 마지막 성공 캐시를 유지한다. 프로젝트 전환/로그아웃 시 비운다.
- `work-dashboard/model.js:214,230`:
  모델 메타데이터 병합 순서, 공통 이벤트 정규화·만료, 고정 객체/일감 연결.
- `work-dashboard/visualizer.js:170`:
  상세 모델·추론 수준·범위별 토큰·출처, 보고 캡션, 부모 일감 조회.
- `work-dashboard/motion.js:40`, `work-dashboard/pixels.js:449,530`:
  명시적 reason 재생/작업 동작. 걷기·대기 중 작업 소품을 표시하지 않는다.
- `chatgpt-mcp/install-runtime.py`:
  코어 16개와 화면 자산 5개의 검증된 비공개 스냅샷 설치. 현재 릴리스 원본/자산 21개를 검증했다.

계획의 출발점은 `docs/superpowers/plans/2026-10-09-work-dashboard.md`와
`docs/superpowers/specs/2026-10-09-work-dashboard.md`다. 이후 객체/메타데이터 확장이 추가됐으므로
현재 상태는 소스·운영 문서·검증 자료와 함께 판단해야 한다.

## 검증 결과

직전 구현 세션에서 완료한 검증:

- JavaScript: 61/61. 객체 유지, lifecycle, 모델 전환 순서, expiry, 누락/0 토큰, 동작·재생을 포함한다.
- Python 백엔드: 113/113. 실제 MCP 도구 호출, 프로젝트 격리, 배치 원자성, 잠금·회전·0600 검사를 포함한다.
- 실제 Chromium 관리 UI: 28/28. API를 통한 보고 수신, 모델/토큰/출처 표시, 기존 조회·편집·프로젝트 전환을 포함한다.
- Ruff, JavaScript 구문, Python AST 검증 통과. 독립 코드 리뷰 최종 APPROVE.
- 설치된 릴리스의 실제 MCP 초기화/도구 목록/보고 호출을 임시 상태에서 검증했다. 도구 목록은 28개였다.
- LAN/Tailscale 모두 운영 자산 해시 일치, JavaScript 오류 0, 390/320px 문서 가로 넘침 없음.
- 새 MCP 프로세스와 managed alias healthy/ready를 확인했다.

재실행 명령은 아래와 같다. 문서 작성 중에는 전체 테스트를 다시 돌리지 않았고 위 검증 기록과 현재 운영 상태를 확인했다.

```bash
rtk proxy /home/jhw/.nvm/versions/node/v24.12.0/bin/node --test work-dashboard/tests/*.test.js
rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python -m unittest discover -s chatgpt-mcp/tests -p 'test_*.py'
rtk proxy /usr/bin/python3 chatgpt-mcp/tests/check_admin_ui.py
```

브라우저 테스트는 설치된 Playwright의 기본 Chromium을 사용했다.
`/usr/bin/chromium`은 없어서 그 경로를 지정한 추가 확인은 실패했다. 기본 실행으로 확인했다.
일부 브라우저 종료 시 기존 BrokenPipeError 로그가 나왔지만 28개 assertion은 통과했다.
새 Python 테스트는 독립 실행에도 import 경로를 잡도록 수정했으며,
MCP 통합 테스트는 asyncio.timeout과 mcp 패키지가 있는 Serena 환경 Python 3.12에서 실행했다.

## 재배포·복구 시 이미 확인한 주의점

이번 세션에서 alias와 mcp-command만 전달한 `runtimes connect`는 `OPENAI_ADMIN_KEY`를 요구하며 실패했다.
기존 profile의 `control_plane.tunnel_id`, `control_plane.api_key` 참조와 base_url을 전달해
같은 터널에 연결하는 방법으로 복구했다. 이 사례 때문에 새 터널 생성이나 새 키 발급이 필요한 것은 아니다.

아래는 당시 성공한 방식의 **인자 형태**다. placeholder가 들어 있어 그대로 실행하는 완성 명령은 아니다.
현재 값은 소유자가 관리하는 기존 profile을 검증해 재사용해야 한다.

```text
tunnel-client runtimes connect
  --alias personal-ops-coding
  --profile personal-ops-coding
  --profile-dir /home/jhw/.config/tunnel-client
  --tunnel-id <existing-tunnel-id>
  --runtime-api-key <existing-file-or-env-key-reference>
  --control-plane-base-url <existing-base-url>
  --mcp-command "python3 <verified-release>/server.py"
```

터널 재연결 직전에는 11개 허용 레포의 queued/running/canceling 명령과 최근 미완료 MCP 호출이 없는 것을 확인했다.
향후 재연결 때도 현재 상태를 재확인해야 한다. 당시 실행 중인 작업을 중단하지 않았고, 해시 릴리스의 소스·자산을 직접 수정하지 않았다.
원본 alias/profile 전체나 로그인 토큰 URL을 출력·문서화하지 않았다.

## 기계 로컬 자료와 보존 범위

같은 호스트에서만 접근할 수 있는 자료:

- `/home/jhw/.local/state/personal-ops-dashboard/observers-2026-10-12/`:
  이번 확장의 검증 JSON, LAN/Tailscale/임시 fixture 이미지, 런타임 확인, 배포 전 백업.
- 같은 폴더의 `systemd.before` / `helper.before`:
  관리 UI의 이전 릴리스 `1ad66495eae709f7a5ac`을 가리키는 원본.
- 같은 폴더의 `tunnel.before`:
  이전 MCP 릴리스 `df2b05e700bd8194acb1`과 기존 터널/키 파일 참조를 유지하는 비공개 원본.
- `/home/jhw/.local/state/personal-ops-dashboard/entity-2026-10-12/`:
  이번 확장 이전의 고정 객체·일감 분리 작업 검증 자료.
- `/home/jhw/.config/tunnel-client/personal-ops-coding.yaml`:
  기존 터널 profile. 확장자는 yaml이지만 실제 내용은 JSON이다.
- `/home/jhw/.local/state/chatgpt-coding-mcp/`:
  기존 명령/활동, 프로젝트 메모·보고, 공통 work-events 상태.

검증 폴더는 0700, 증거·백업 파일은 0600이다. 원본 로그/설정에는 민감한 운영 정보가 있을 수 있으므로
공유용 자료는 필요한 상태 값만 추출해야 한다. 이 문서에는 인증값·토큰 로그인 URL·대화 원문을 넣지 않았다.
이번 구현에서는 커밋·push·PR·Notion 저장·후크 신뢰 변경·LLM API 요청을 수행하지 않았다.

## 이어서 시작할 때의 기준

이 문서와 수집 계약을 읽으면 현재 구현과 아직 없는 수집기를 구분할 수 있다.
실사용 보고를 확인할지, CLI 수집기를 구현할지의 후속 범위는 새 세션의 사용자 요청에 따른다.
현재 작업 폴더와 호스트 상태가 문서 작성 이후 달라졌다면 현재 지침·파일·관측 결과를 우선한다.
