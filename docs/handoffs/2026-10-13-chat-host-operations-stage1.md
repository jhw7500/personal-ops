# 인계 — chat host operations 1단계(A1–A4) 완료

- 작성: 2026-10-13 KST, 세션 7205 (Claude Code). Task 없음(계획서 결정).
- 근거 문서: [설계](../superpowers/specs/2026-10-13-chat-host-operations-design.md), [총괄 계획](../superpowers/plans/2026-10-13-chat-host-operations.md), [A 계획](../superpowers/plans/2026-10-13-registered-host-operations.md)
- 최초 범위 결정(사용자, 2026-10-13): A4와 필수 최종 검증까지만 진행한다. 보안·데이터 결함은 해결하고 그 밖의 개선은 후속 항목으로 둔다. 당시에는 B·C·런타임 롤아웃을 시작하지 않았다.
- Codex 재개 결과(2026-10-13): 활성화 전 결함 3건을 수정하고, 사용자 승인에 따라 원본의 기존 변경을 보존하며 통합했다. **현재 작업본은 원본 checkout이며 커밋은 하지 않았다.** 소스 통합 증거는 §8을 따른다.
- 후속 승인(사용자, 2026-10-13): 런타임 설치·터널 전환·관리 웹 재시작을 승인했다. 적용 결과는 §9이며, **B(PostgreSQL)·C(에이전트 위임)·신규 대상 권한·실제 Docker/DB 변경·Schema 013 적용은 제외**한다.
- 추가 승인(사용자, 2026-10-13): §10의 `tossApp / tarororo-test` v3 등록 후보 적용을 승인했다. 설정만 적용했으며 실제 Docker/DB 작업은 실행하지 않았다.

## 1. 현재 상태 (한눈에)

| 항목 | 상태 |
|---|---|
| A1–A4 소스 구현 | 완료. scratch `eef7f22`에 수정 3건과 원본 호환 병합을 더해 원본 checkout에 반영 |
| 작업별 리뷰 | A1–A3: 리뷰 → 수정 → 재리뷰 통과. A4: 사용자 지시로 최종 리뷰에 합침 |
| 최종 전체 리뷰(sonnet 1회) | 차단 결함 없음. Codex 수정·통합 검토도 통과 |
| 필수 최종 검증 | 최신 통합 검증은 §8. Ruff 기존 4건으로 exit 1 |
| **원본 checkout 반영** | **완료.** 소스 41개 파일(신규 20개)을 반영하고 기존 변경을 보존 |
| 런타임 설치·터널/관리 웹 전환 | 승인 후 완료. 실제 MCP/admin 모두 `b564eca27518ae811641` |
| ChatGPT 새 도구 메타데이터 | 사용자 제공 새 대화의 `list_host_targets` 호출 성공 확인. 나머지 신규 도구 스키마·호출은 미검증 |
| v3 등록 대상 설정 | 승인된 tossApp / tarororo-test 1개 적용 완료. 기존 v2 payload는 legacy에 정확히 보존 |
| 실제 Docker/DB·모델 호출 | 안 함. 설정 적용 뒤 서비스 재시작도 없음 |
| B1–B3, C1–C3 | 미착수 |

## 2. 코드 위치

- 현재 작업본: `/home/jhw/ai/opencode/projects/personal-ops`. HEAD `cf8b6d2` 그대로이며 반영 내용은 미커밋 상태다.
- Claude 최초 작업본: `/home/jhw/.local/state/personal-ops-worktrees/chat-host-ops-20261013`
  - `git init`으로 만든 별도 scratch 저장소다. personal-ops 이력이 아니며 push된 적 없다.
  - Codex 수정 3건은 이 scratch에도 미커밋으로 남아 있다. 원본의 기존 변경까지 보존한 최종 통합본은 scratch 루트와 다르므로, scratch 전체를 원본에 덮어쓰지 않는다.
- 기준선: 커밋 `928bef3`. personal-ops main `cf8b6d2`에 그 시점의 미커밋 `chatgpt-mcp/`·`work-dashboard/`·계획 문서 92개 파일을 더한 상태다.
  - 원본·사본의 해시는 `~/.local/state/personal-ops-worktrees/.baselines/chat-host-ops-20261013.json`(0600)에 있다.
- 커밋: `19bf735`·`b35bbe7`(A1), `9f252d4`·`49f794b`·`a8921d1`(A2), `08b74b4`·`9fb2375`(A3), `eef7f22`(A4).
- 진행 기록(정본): `…/chat-host-ops-20261013/.superpowers/sdd/2026-10-13-chat-host-operations/`
  - `progress.md`: ledger, 판정 R0–R19, 연기 항목
  - `task-A{1..4}-report.md`: 구현 보고, TDD 증거
  - `review-*.diff`: 리뷰 패키지
  - `final-verify-eef7f22/`: 최종 검증 로그

## 3. Claude 최초 변경 내용 (`chatgpt-mcp/` 아래 39개 파일, +9,710 / −121)

신규 모듈 7개:
- `host_registry.py`: v3 대상 registry
- `host_actions.py`: compose.* 정형 action과 영향 계산
- `host_process.py`: 제한된 프로세스 실행기
- `host_source_review.py`: 입력 스냅샷과 reviewed_snapshot 검토 자료
- `host_registered_operations.py`: 계획·승인 영수증·상태 머신·이벤트
- `host_compose_actions.py`: Compose 어댑터(렌더 정책, 정체성 pin, image-only apply)
- `host_registered_tools.py`: MCP 도구와 라우팅

수정 파일:
- `server.py`: 신규 도구 5개 등록. legacy `get_host_operation`을 라우팅하고, 두 저장소에 같은 ID가 있으면 `AMBIGUOUS_OPERATION_ID`를 낸다.
- `admin_server.py`, `host_runtime_web.py`
  - 승인 POST들이 신뢰 검사 helper 하나를 함께 쓴다.
  - registered 조회/승인/거절 엔드포인트를 추가했다.
  - v3 설정에서는 `/api/host-scope-enable`을 거절한다.
- `admin.js`, `admin.html`, `admin.css`: 승인 대기·실행·완료·만료·오류·빈 상태, 이벤트, 검토 자료를 표시한다. 문자열은 textContent로만 넣는다.
- `host_runtime_config.py`: v3 envelope를 추가했다. legacy view만 해석하며, v3가 무효이면 legacy도 닫는다.
- `host_runtime.py`, `host_runtime_operations.py`: lock 같은 공용 helper를 모듈 수준으로 올렸다. 검증 대상 모듈 목록을 확장했다.
- `install-runtime.py`, `rollout-host-runtime.py`, `project_state.py`: 설치 manifest와 known-tool 목록. **실행하지 않았다.**
- `configure-host-runtime.py`: v3 후보를 읽기 전용으로 검증하고 diff만 출력한다.
- `README.md`

테스트:
- 신규 test 파일 10개와 Node UI 테스트 1개
- 실제 compose 렌더 fixture(`tests/fixtures/pc_test_compose_config.json`). secret 값과 호스트 경로는 없다.
- 기존 테스트 7개 갱신. `test_admin_host_approval.py`의 import 경로 결함도 고쳤다.

신규 MCP 도구: `list_host_targets`, `inspect_host_target`, `prepare_host_operation`, `execute_host_operation`, `read_host_operation_events`. Codex 통합본은 원본의 기존 호스트 도구 11개(006–012 선행 마이그레이션 도구 포함)와 신규 5개를 함께 등록한다. 실행 중인 런타임에는 아직 적용하지 않았다.

## 4. Claude 최초 테스트·검증 증거 (최신 결과는 §8)

| 검증 | 결과 | 로그 |
|---|---|---|
| unittest discover (PYTHONPATH 없음, 계획서 명령) | **415 OK**, exit 0. 기준선은 277건, 오류 1 | `final-verify-eef7f22/unittest.log` |
| 같은 명령, PYTHONPATH 지정 (구현자 실행) | 415 OK | `task-A4-report.md` |
| `ruff check chatgpt-mcp` | exit 1. **기존 3건**뿐이다: `host_db_migration.py:14,134`, `tests/test_host_db_restore.py:2`. 두 파일은 이번 변경 범위 밖 | `ruff.log` |
| `node --check admin.js` | exit 0 | `node-check.log` |
| `node --test tests/test_host_operations_ui.mjs` | 10/10 pass | `node-test.log` |
| `git diff --check 928bef3..eef7f22` | exit 0 | `diff-check.log` |
| 브라우저(`tests/check_admin_ui.py`, Playwright) | 32/32. **A4 구현 중 실행했고, 마지막 수정 뒤에는 다시 돌리지 않았다** | `task-A4-report.md` |
| 최종 전체 리뷰(sonnet) | 차단 결함 없음 | ledger |

- 실행 전후 HEAD는 `eef7f22`로 같았고 작업 트리도 깨끗했다(`run.txt`).
- 계획서 최종 검증의 `test_agent_tasks_ui.mjs`는 C 단계 파일이라 범위 밖이다.

## 5. 미해결·후속 항목 (비차단)

최종 리뷰가 보안·데이터 결함으로 보지 않은 항목이다. 대부분 fail-closed이거나 가용성·정리 성격이다.

**A. 활성화 전에 정리를 권장하는 항목**
1. **해결:** MCP와 관리 웹의 `state_dir` 불일치. 기본 상태 경로는 `XDG_STATE_HOME`을 따르고, 관리 웹의 두 작업 관리자는 명시적으로 같은 `host-runtime` 경로를 받는다.
2. **해결:** 상대 승인 링크. `TRUSTED_APPROVAL_ORIGIN`을 공유하며 신규 작업의 승인 링크는 고정된 신뢰 HTTPS 주소의 절대 URL이다. 로그인 토큰은 링크에 넣지 않는다.
3. **해결:** 신규 모듈 누락 시 관리 웹 전체 기동 실패. 선택 모듈이 없으면 기본 웹은 유지하고 해당 기능만 닫는다. 내부 import 결함과 다른 의존성 오류는 숨기지 않는다.
4. **검증 완료:** 최종 통합본의 브라우저 검사 32건을 다시 실행해 통과했다. 실제 설치 런타임의 UI 확인은 활성화 이후 단계다.

**B. 보안 강화(hardening)**
5. registered compose 파일의 최상위 `include`, `extends.file`, project `.env`는 두 렌더 모두에서 평탄화된다. 그래서 검토된 compose 변경이 호스트 dotenv 값을 허용 필드로 보간해도 보이지 않는다. `include`/`extends`를 거절하거나 `${VAR}` 참조를 `compose.env` 키로 제한해야 한다.
6. `user`에 제한이 없어 root/0도 통과한다.
7. git 호출이 저장소 로컬 설정(`core.fsmonitor`, hooks)을 따른다. 현재는 `.git`을 샌드박스와 파일 도구에서 제외해 막고 있다. `-c core.fsmonitor=false -c core.hooksPath=/dev/null`로 강화할 수 있다. legacy와 공통이다.
8. 해석 렌더가 raw `env_file` 검사보다 먼저 실행되어, 거절 전에 호스트 env_file이 어댑터 메모리로 읽힌다. 외부로 노출되지는 않는다. 두 검사 순서를 바꾸면 된다.
9. `secret_dir`의 상위 경로 symlink와 쓰기 권한을 검사하지 않는다(legacy와 같은 수준). 스냅샷 밖 파일은 변경 시점에 다시 읽힌다(TOCTOU).
10. env 거부 목록에 `OTEL_*`, `SSL_CERT_*`, `TMPDIR`, `GODEBUG`가 없다. 값은 호스트에서 등록한다.

**C. 가용성·정리**
11. 스냅샷과 작업 기록을 정리하지 않는다. 1000건이 쌓이면 `HISTORY_CAPACITY_REACHED`가 난다.
12. 손상된 op 파일 하나가 있으면 `pending()` 전체가 `PLAN_INVALID`가 된다. `diagnostics.json`이 손상되면 진단 기록이 조용히 멈춘다.
13. 전역 operations lock을 잡은 채 preflight·inventory·git을 실행한다. 그동안 다른 대상의 조회와 이벤트가 지연된다.
14. 승인한 계획을 철회할 수 없다. 만료는 생성 시각 기준이다.
15. secret 검사가 stop/down/exec/build에도 걸린다. secret 디렉터리가 고장 나면 스택을 정지할 수 없다.
16. null dir이 dot 디렉터리 아래까지 탐색한다. `.cache` 안 symlink 하나가 모든 계획을 막을 수 있다.
17. high-risk 파일이 256 KiB를 넘으면 git_head 정책에서 실패한다. 현재 `package-lock.json`은 44 KiB다.

**D. 품질·테스트**
18. unittest 출력에 asyncio slow-callback 디버그 줄 6개가 섞인다(`Executing <Task …> took 0.4 seconds`).
19. Ruff 기존 4건(원본의 선행 마이그레이션 테스트 경고 1건 포함, 이번 범위 밖). 위치는 §8.
20. `project_state` 도구 목록에 legacy `get_db_migration_status`·`prepare_db_migration`이 이전부터 빠져 있다. known-tools 목록 일치를 강제하는 장치도 없다.
21. 분리 후보 파일: `host_registered_operations.py`(약 920줄), `_validate_target`(약 70줄).
22. 테스트 공백: CREATING 경로 e2e, volume 재확인, readiness 전체 deadline, `HEALTH_STATES` "none".
23. 기타 세부 Minor는 ledger의 `minor (deferred)` 줄에 있다.

**E. 이번 1단계 밖 (사용자 지시로 미착수)**
- B1–B3 PostgreSQL. 폐기용 PostgreSQL 컨테이너 통합 시험 승인(R7b)은 받아 두었지만 사용하지 않았다.
- C1–C3 Codex 위임
- 런타임 설치·전환은 §9, v3 등록 대상 활성화는 §10에서 후속 승인으로 완료했다. 실제 모델 smoke, 실제 Docker·DB 작업, 013 적용은 미착수다.

**F. 다른 저장소로 이관한 항목**
- 과잉 리뷰·범위 확장 재발 방지 규칙: [claude-config#205](https://github.com/jhw7500/claude-config/issues/205)

## 6. 다음 세션이 결정할 것

1. **원본 checkout 반영은 완료.** 원본에서 계속 작업하며 scratch 전체를 덮어쓰지 않는다. 추가 편집 전 기존 미커밋 변경을 보존한다. 커밋은 별도 요청이 있을 때만 한다.
2. 위 §5 항목을 personal-ops GitHub 이슈로 등록할지(1~3개로 묶기 권장).
3. 후속 활성화 순서(설계 §11 4–6단계)
   - 런타임 설치·서비스 전환은 완료했다. metadata 대기만으로 재시작을 반복하지 않는다.
   - ChatGPT의 기존 플러그인 연결에서 Refresh 후 새 대화의 신규 도구 노출을 검증한다.
   - v3 등록 대상 후보의 프로젝트·서비스·허용 작업·영향을 구체화하고 별도 설정 승인을 받는다. 기존 v2 권한을 명시적으로 보존한다.
   - 대상 등록 이후 실제 작업마다 웹 승인을 받는다.
4. B·C 착수 여부. 시작한다면 claude-config#205의 예산 규칙을 먼저 적용한다.

## 7. 재개 방법

- 이 문서의 §9와 §8을 먼저 읽고 원본 checkout에서 이어간다. Claude 최초 구현의 판정·연기 항목은 `progress.md` ledger와 scratch 저장소의 `git log`에 남아 있다.
- 원본 checkout에서 전체 Python 검증을 재현하는 명령:
  `rtk proxy /home/jhw/.local/share/uv/tools/serena-agent/bin/python -m unittest discover -s chatgpt-mcp/tests -p 'test_*.py'`

## 8. Codex 수정·원본 통합 결과 (2026-10-13)

- 활성화 전 결함 3건은 scratch에서 수정하고 회귀 테스트 5건으로 검증했다.
- 원본과 기준선의 해시 비교에서 충돌 13개 파일을 발견했다. 사용자 승인 후 원본의 006–012 도구·승인 경로·실행 구성요소 검증을 보존하며 병합했다. 수동 충돌 6개 파일을 해결하고 병합 부분의 독립 검토를 통과했다.
- 세 키의 고정 predecessor v3와 다섯 키의 registered v3를 정확한 구조로 구분한다. 등록 대상만으로 선행 DB 권한을 얻지 않으며, registered 설정을 기존 범위 활성화 버튼으로 덮어쓰지 않는다. 이 호환성 회귀 테스트 7건을 추가했다.
- 반영 직전 원본에 추가 변경이 없음을 확인했다. 소스 41개 파일만 반영했고, 기존 파일 권한을 유지했다. 반영 후 소스 100개 파일의 해시·권한을 대조했다. `work-dashboard/`의 기존 변경은 그대로다.

| 검증 | 최신 결과 |
|---|---|
| 최종 통합 후보 전체 Python | 439건 통과 |
| 최종 통합 후보 Playwright | 32건 통과. 요청 취소에 따른 기존 BrokenPipe trace 2건은 별도 기록 |
| 원본 반영 후 활성화·병합·라우터·승인·롤아웃 회귀 | 56건 통과 |
| 원본 반영 후 Node UI | 10건 통과 |
| Python AST / Node 문법 | 통과 |
| 원본 `git diff --check` | 통과 |
| 변경된 Python 35개 파일 Ruff | 통과 |
| 수정·병합 부분 리뷰 | 지적 사항 없음, 승인 |
| 원본 Ruff | 기존 4건: `host_db_migration.py:16,163`, `tests/test_host_db_restore.py:2`, `tests/test_host_predecessor_migration.py:4` |

검증 로그·반영 영수증·기존 파일 백업은 scratch의 다음 비공개 경로에 있다:
`.superpowers/sdd/2026-10-13-chat-host-operations/codex-stage1-fixes/` (디렉터리 0700, 보고·로그·백업 파일 0600).

- `integrated-unittest.log`, `integrated-browser.log`, `original-integration-regressions.log`
- `integration/integrated-apply-receipt.json`, `integration/apply-backups/`
- `integration/integration-outcome.md`

**§8 시점의 작업은 소스 통합까지였다.** 이 단계에서는 커밋·push·서비스 재시작·런타임 설치·v3 설정 활성화·Docker/DB/모델 호출·Schema 013 적용을 하지 않았다. 이후 사용자가 런타임 적용을 승인했으며 결과는 §9에 기록한다.

## 9. 승인된 런타임 적용 결과 (2026-10-13 16:47 KST)

실제 호스트의 원본 소스 40개 파일과 제어 입력을 고정하고 관련 296건·전체 439건의 테스트를 통과했다. 현재 제어 Codex PID 233826은 터널 재시작 대상과 별개였으며, 실행 중인 MCP job·호스트 작업이 없는 상태에서 적용했다.

실행 명령:

```sh
rtk proxy script -q -e -c '/home/jhw/.local/share/uv/tools/serena-agent/bin/python /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error --diagnostics-dir /home/jhw/.local/state/personal-ops-rollout-recovery/20261013/stage1-activation-e00xilxi/rollouts' /dev/null
```

사용자 승인에 따라 실제 TTY의 `ROLLOUT` 확인을 완료했다. 이전 릴리스를 보존하며 기존 profile/unit의 MCP 명령과 admin drop-in만 전환했다. credential·LAN·HTTPS 인자는 보존했고 다른 Codex 세션·observer 서비스는 제어하지 않았다. `--no-pause-on-error`는 자동 검증 대기에서 터미널을 붙잡지 않기 위한 옵션이며 승인·검사를 생략하지 않는다.

| 대상 | 이전 런타임 | 적용 후 실제 런타임 | PID |
|---|---|---|---|
| MCP | `c81522954a091864c42d` | `b564eca27518ae811641` | 3736958 |
| tunnel | 이전 MCP 명령 | 새 MCP 명령과 정렬 | 3736904 |
| admin | `1dfc5fcaf2cc707701be` | `b564eca27518ae811641` | 3737336 |

- 실제 argv·프로세스 시작 tick·설치 manifest 40개 파일의 해시/권한을 검증했다. MCP와 admin이 같은 불변 스냅샷을 실행한다.
- 설치본을 **별도 격리 상태 디렉터리의 MCP SDK stdio 프로세스**로 검사해 전체 도구 44개 중 호스트 도구 16개(기존 11 + 신규 5)를 확인했다. 이는 운영 터널의 같은 child에서 수행한 tools/list 또는 ChatGPT 메타데이터 검증을 대신하지 않는다.
- 실제 운영 커넥터의 `get_runtime_status`, `get_db_migration_status` 호출은 전환 후 성공했다.
- 실제 Chromium으로 loopback/LAN/Tailscale HTTPS 3곳에서 로그인·tossApp 선택·기존 승인 패널·신규 승인 링크 경로·미등록 안내를 확인했다. 정적 asset은 설치 manifest와 일치하며 page error는 0이었다. 계획 생성·승인 POST·실제 작업 실행은 하지 않았다.
- 컨테이너 3개의 ID·이미지·시작 시각·상태, 읽기 전용 DB schema metadata, runtime/admin 키 파일의 해시·권한 및 host-runtime.json의 해시·권한이 적용 전과 같았다. 013은 여전히 missing이며 선행 일치 5/12, 광고·결제 모드는 disabled다.

**§9 적용 직후 남았던 두 설정/검증 항목 (후속 결과 포함):**

1. 롤아웃 종료 코드는 **2 / `verification_required`**다. `runtime_verified=true`, `admin_ready`, `tunnel_healthy=true`지만 `/health/mcp`는 `awaiting_discovery`다. 현재 대화의 ChatGPT 도구 메타데이터 31개에는 신규 5개가 없다. 서버의 도구가 0개라는 뜻이 아니며 설치 실패로 취급하거나 재시작을 반복하지 않는다. 기존 ChatGPT 연결의 Refresh 후 새 대화에서 실제 신규 도구 목록/스키마와 읽기 호출을 확인해야 한다.
   - 후속 사용자 확인: Refresh 후 새 ChatGPT 대화에서 `list_host_targets(project=tossApp)`의 `enabled=false`, `reason=REGISTRY_NOT_CONFIGURED`, `targets=[]` 결과를 전달했다. 해당 신규 조회 도구의 클라이언트 노출·호출은 확인됐으며, 비활성 사유는 v3 대상 설정 부재다. 이 Codex 대화의 오래된 도구 목록과 구분한다. 나머지 신규 4개 스키마·호출은 확인하지 않았다.
2. **기존 v2 설정을 유지했다.** 범용 registered 대상은 0개이고 `REGISTRY_NOT_CONFIGURED`다. 일반 화면에서 신규 등록 대상 패널이 숨겨지는 것은 이 상태의 정상 동작이다. 신규 대상/허용 작업을 정의한 v3 설정 후보와 그 차이·영향에 대한 별도 승인 전에는 범용 compose 작업 권한을 부여하지 않는다. 006–012 권한도 자동 활성화하지 않았다.
   - 이후 §10의 후보 준비·사용자 승인·설정 적용을 완료했다. 현재 대상은 1개이며 기존 v2는 legacy에 보존했다. 006–012 추가 권한은 여전히 비활성이다.

비공개 증거 디렉터리(0700):
`/home/jhw/.local/state/personal-ops-rollout-recovery/20261013/stage1-activation-e00xilxi/`

- 롤아웃 진단: `rollouts/rollout-hyh826v0/receipt.json` (0600)
- 적용 전/후: `before.json`, `after.json`, `source-inventory.json`
- 프로토콜 검사: `installed-protocol-tools.json` (`isolated_installed_snapshot`로 구분)
- 실제 웹 검사: `management-ui-after.json`
- DB/컨테이너/키 보존 기준선: `connector-before.json`, `containers-before.json`, `credential-fingerprints-before.json`
- 결과 보고: `activation-outcome.md`

커밋·push·기존 DB 백업/복원·Docker build/up/restart·Schema 013 적용·새 모델 호출은 수행하지 않았다. B·C는 계속 미착수다.

## 10. 등록 대상 설정 준비·승인·적용 완료 (2026-10-13 17:12 KST)

후속 사용자 결과에 따라 `tossApp / tarororo-test` 1개만 등록하는 후보를 준비했다. 후보·권한 범위를 제시한 뒤 사용자에게 적용 승인을 받았다. **실제 host-runtime.json은 이제 registered v3이며 기존 v2 payload를 legacy에 정확히 보존했다.**

- 후보: `/home/jhw/.local/state/personal-ops-rollout-recovery/20261013/stage1-activation-e00xilxi/registry-candidate/host-runtime-v3.candidate.json` (0600)
- 정규화 설정 digest: `46b89a265d43d758967813c76bca753d55896ef61fc2fb1a2599f286e882caa1`
- 등록 project: `tossApp`; 로컬 경로: `/home/jhw/ai/opencode/projects/tossApp`; Compose project: `tarororo-test`; environment: `test`.
- 기존 v2 payload를 `legacy`에 정확하게 복사해 기존 API/Worker/013 계획·개별 승인 범위를 보존했다. 006–012 권한은 추가하지 않는다.
- 추가 capabilities: `compose.build`, `compose.apply`, `compose.up`, `compose.restart`, `compose.stop`. Compose 전체 모델 검사에는 api/db/worker를 등록하지만, `db_service`·`compose.down` 권한과 exec 프로필은 추가하지 않는다. DB apply/up/restart/stop/exec 및 전체 스택 down은 허용하지 않는다. 현재 db에는 build 정의가 없다.
- API/worker 코드가 DB에 연결하므로 향후 앱 작업의 DB 쓰기까지 금지한다는 보장은 아니다. 실제 작업은 개별 웹 승인과 입력·영향 검토를 거쳐야 하며, 이번 설정 후보 준비에서는 아무 작업도 실행하지 않았다.
- source_review는 `reviewed_snapshot`: 현재 미커밋·미추적 소스를 입력 범위대로 고정하고 고위험 입력(006–013 SQL 포함)을 작업별 웹 검토 자료에 담는다. migrate/seed tools profile, 임의 shell/exec, 새 PostgreSQL 설정, agent profile은 추가하지 않는다.
- 실제 Compose의 네트워크·loopback 3200 포트·기존 DB named volume·secret 파일 경계를 등록했다. 값이 아닌 secret 소유자·권한·크기만 확인했다.

검증:

- 실제 `docker compose config --format json`과 raw/no-env-resolution 렌더의 정책 검사 통과. 이는 읽기 전용이며 컨테이너·이미지·볼륨을 변경하지 않았다.
- 등록 스키마·프로젝트 매핑·입력 경로·secret metadata 검사 통과.
- `configure-host-runtime.py --validate-v3 CANDIDATE` exit 0, valid version 3 / 1 target, validation only.
- 검증 로그: `registry-candidate/validate-v3.log`; 근거: `evidence.json`; 기존 설정 백업: `host-runtime-v2.before.json` (모두 0600).

승인된 적용·검증:

- 적용 직전 후보 digest와 기존 v2 설정 SHA를 다시 비교했다. 실행 중·승인된 호스트 작업이 없는 상태에서 기존 target lock을 잡고 0600 설정을 원자적으로 교체한 뒤 파일·부모 디렉터리를 fsync했다. 기존 v2 바이트 백업은 보존했다.
- 설정 원문 SHA: 이전 `e55a50e30e6454b43735ab629ef8631a2e4d3ac81a223b674da8c088cc0ae7ce`, 적용 후 `1e34d840110e6be293a4248f9b1a0dd28e541299596757d8df42d34f9b61e9bc`.
- 실제 관리 웹의 loopback/LAN/Tailscale HTTPS 3곳에서 `enabled=true`, `reason=null`, target 1개를 확인했다. 등록 작업 목록과 기존 승인 목록 GET도 모두 200이었다.
- 실제 Chromium에서 3곳 모두 tossApp 선택 → 등록 대상 패널 표시 → `tarororo-test` 선택을 확인했다. 작업은 0건이고 승인/거절 버튼은 disabled, page error는 0이었다. 승인 POST·계획 생성·실행은 하지 않았다.
- 실제 터널의 기존 런타임·013 schema 조회는 적용 전과 같은 결과였다. 컨테이너 3개의 ID·이미지·시작 시각·상태, schema metadata, 키 파일 해시·권한은 같았다. 013은 missing, 선행 일치 5/12이며 광고·결제 모드는 disabled다.
- MCP PID 3736958, tunnel PID 3736904, admin PID 3737336과 시작 세대가 유지됐다. registry reload로 반영됐으며 서비스 재시작은 하지 않았다.
- 현재 기존 extended 범위는 true, predecessor 범위는 false다. source code·커밋·실제 Docker/DB 작업·B/C 구현은 변경하지 않았다.

추가 증거(같은 0700 candidate 디렉터리, 파일은 0600): `apply-before.json`, `apply-receipt.json`, `apply-after.json`, `live-http-after.json`, `live-browser-after.json`, `apply-outcome.md`.

후속 ChatGPT 조회 결과로 사용자가 Host Registry 활성화·Target 정상 등록, Docker 서비스 실행 중, API Ready를 확인해 전달했다. 등록 후 클라이언트 조회 성공까지 사용자 제공 응답으로 확인됐다. Schema 013 missing과 Monetization Mode disabled는 이번 승인 범위에서 변경하지 않은 상태다. 이 결과만으로 광고 기능의 전체 흐름을 검증했다고 보지 않는다. 나머지 신규 도구의 전체 스키마와 실제 변경 작업은 검증하지 않았다.

나머지 실제 Compose 작업은 `prepare_host_operation` → 관리 웹의 개별 1회 승인 → `execute_host_operation` 순서로 진행한다. 광고·결제 흐름을 검증하려면 선행 스키마·013 및 필요한 테스트 설정을 별도 업무/승인으로 다룬다. DB/SQL·에이전트 위임의 B/C 단계는 여전히 미착수다.
