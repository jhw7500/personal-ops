# 웹 승인형 MCP 런타임 전환 재개 기록 — 2026-10-15 23시 KST

## 목표 및 출처
사용자 요청: "재개해줘". 이전 작업은 TTY 전용 롤아웃을 관리 웹의 계획별 1회 승인으로 확장한 뒤, **설치 전** 일시중단했다.
대상: `personal-ops` MCP, 이후 `tossApp/tarororo-test` Schema 014→API-only→새 AIT 순차 배포.
**현재는 실제 설치·런타임 스위치·DB/API/AIT 배포를 실행하지 않았다.**

## 현재 독립 확인
- MCP `get_host_access(personal-ops)`: 종전 설치 권한 `EXPIRED`, 새로운 사용자 승인 요청 `bab0c0795fd5daf118d73c91bf9aeeda` 생성. 요청 자체는 권한 부여가 아니며, 유효기간 내 소유자 웹 승인 필요.
- `tossApp` 런타임 read-only: API running/healthy/ready, mode `toss-demo`, Worker running, PostgreSQL running, Schema013 Ready. Schema014 실제 DB 마이그레이션 0건.
- 현 `personal-ops` Git `main` 공유 WIP. 수정 19개 추적 항목, untracked 168 항목; reset/stash/commit/push 없음.
- 이전 공식 preflight 호스트 작업 `df03b78d71dad049fe71b7175475570d` exit0, `preflight_ok`, 당시 source 78 파일·snapshot `0ffc614adbb1ac921c8b`, installed=false/switched=false. 단, 당시 스냅샷은 **현재 소스와 다름**.
- 현재 `runtime_rollout_web._expected_snapshots`: source manifest digest **`700fdf80887b6aea2858`**; 공식 preflight 이전 이후 최신 시각으로 갱신된 manifest 파일은 `work-dashboard/motion.js` (09:25 UTC), `work-dashboard/model.js`, `work-dashboard/visualizer.js`, `work-dashboard/visualizer.css` (12:07 UTC) **4개뿐**. 타 세션의 프런트엔드 작업일 수 있으므로 해당 자산은 수정·되돌림 없이 보존함.
- 새 source snapshot `700fdf80887b6aea2858`은 **공식 실 호스트 preflight 통과 지문이 아님**. 기존 preflight 결과를 재사용해 바로 설치하지 말 것.

## 이번 재개에서 직접 수행한 검사
- sandbox `runtime-web-restart-validation-20261015-2257`: `runtime_rollout_web.py`, worker/tools, server, admin, installer 등 py_compile + Node syntax PASS. 웹 승인 상태/가짜 실행 9/9, 관리자 HTTP 승인 5/5, 기존 rollout 14/14, immutable installer 9/9 = **37/37 PASS**, exit0.
- sandbox `runtime-web-rebaseline-dashboard-and-admin-tests-20261015`: 관리 서버 25/25, installer 9/9, rollout 14/14, web state 9/9, HTTP 승인 5/5 = **62/62 PASS**, Node syntax PASS, exit0.
- 실제 리포지토리 소스 파일 덮어쓰기 없음, 관리자 token / Toss OAuth / DB 값 조회 없음.
- 개인 호스트 grant 만료가 임박하여 새 설치 권한 요청 생성. 운영 시스템 서비스 재시작 없음, 외부 업로드 없음.

## 남은 실행 순서 및 안전 게이트
1. 소유자는 관리 웹 `접근·승인`의 `personal-ops` 설치 접근 요청 `bab0c0795fd5daf118d73c91bf9aeeda`를 확인하고 별도 승인해야 함. 이 상태만으로 런타임/DB를 변경할 수 없음.
2. 새로운 소스 스냅샷을 기준으로 **공식** `rollout-host-runtime.py` read-only host preflight를 Node24 + systemd user bus 환경에서 재실행. targeted, full, service/profile, source digest 모두 검사. 변경 중인 타 세션 파일이 있다면 지문 변동을 기록하고 중단.
3. 새 웹 승인 코드가 아직 운영 MCP에 설치되지 않은 **최초 부트스트랩 1회**는 기존 공식 `--apply` 명령의 소유자 TTY `ROLLOUT` 확인이 필요. 호스트 비대화형 툴로 TTY 위장/우회 금지. 설치 완료 후 MCP runtime/admin 연결 재검증.
4. 새 활성 MCP에 `prepare_runtime_rollout`, `get_runtime_rollout`, `execute_runtime_rollout` 3개 도구 노출 확인; 이후 릴리스부터는 ChatGPT plan → 관리 웹 새 token·계획 ID/digest 1회 승인 → 별도 systemd user unit 통한 업데이트 가능.
5. tossApp의 Schema014는 **다른 별도 승인**으로 `get_schema014_migration_status` 검증, `prepare_schema014_migration` 원자적 계획, 관리 웹 승인, `execute_schema014_migration` DB-only 적용 순서. 기존 013 grant 자동 재사용 불가.
6. DB014 ready 확인 이후 API-only `toss-demo` 배포, Worker/DB 세대 보존; 마지막에 고정 SHA `d14d4a67fad63cf7aa623e5c1bc385a7a356add6a73620d3dfe4fead1380305f` 새 UX AIT TEST 업로드와 실제 Toss WebView QA.
7. 어떠한 확증 없이 `ready`, `installed`, `DB014 complete`, `AIT deployed`라고 보고하지 말 것. Codex 불호출 및 타 사용자 세션 변경/kill 금지.

자세한 웹 승인 구현/호스트 안전성: `docs/handoffs/2026-10-15-schema014-typed-mcp-rollout-blocker.md`; 소스 `chatgpt-mcp/runtime_rollout_web.py`, `runtime_rollout_worker.py`, `runtime_rollout_tools.py` 및 관리자 웹.
