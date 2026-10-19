# 작업 인계 — personal-ops-tunnel 런타임 롤아웃 진단/복구

작성일: 2026-10-13 KST
대상 저장소: /home/jhw/ai/opencode/projects/personal-ops
연계 저장소: /home/jhw/ai/opencode/projects/tossApp
런타임 alias: personal-ops-coding
대상 서버: tossApp / tarororo-test

## 사용자 요청
- ChatGPT에서 안전한 DB migration 013 진단·적용 계획, Worker 배포, 웹 승인 기능을 사용하고 싶다.
- 반복적인 터미널 명령은 피하고 싶으며, 어려우면 호스트 에이전트 세션으로 이관해도 된다고 명시했다.
- `python3 /home/jhw/ai/opencode/projects/personal-ops/chatgpt-mcp/rollout-host-runtime.py --apply` 실행 이후 터미널 창이 종료됐다고 보고했다.
- ChatGPT 플러그인 새로고침 후에도 신규 MCP 도구가 보이지 않았다.

## 확인된 사실(도구 조회)
- 현재 ChatGPT에서 `mcp__personal_ops_tunnel__get_runtime_status` 호출 성공.
- 2026-10-13 약 01:00 KST 응답: `availability=enabled`, `api.state=running`, `api.health=healthy`, `worker.state=running`, `database.state=running`, `database.schema_013=missing`, `health_endpoint=ready`.
- 현재 제공되는 MCP 카탈로그는 기존 5개 호스트 도구만 노출:
  `get_runtime_status`, `compare_deployment`, `prepare_test_deploy` (API only), `execute_test_deploy`, `get_host_operation`.
- 신규 `get_db_migration_status` 및 `prepare_db_migration`는 현재 모델의 가용 도구 목록에서 보이지 않는다.
- 따라서 *터널 자체 다운은 확인되지 않지만*, 신규 스냅샷이 실제로 실행 중인지, ChatGPT 카탈로그가 오래됐는지는 미확인.
- DB 013을 아직 실 DB에 적용했다는 증거는 없으며 readiness=missing이다.

## 소스 변경(저장소에 존재; 운영 반영 미검증)
- `chatgpt-mcp/host_runtime.py`: 013 존재/체크섬/12 선행 migration 일치 개수 읽기 전용 진단
- `chatgpt-mcp/host_db_migration.py`: 고정 SQL 013만; 백업·아카이브 검사·임시 DB 복원 시험·시스템 대상 재검증 이후 적용
- `chatgpt-mcp/host_runtime_operations.py`: API/Worker/013 계획, 소스 pinning, 만료, approval receipt
- `chatgpt-mcp/admin_server.py`, `admin.html`, `admin.js`: 웹 재인증 승인
- `chatgpt-mcp/host_runtime_config.py`: v1 API-only, 명시적 v2 Worker/013 범위
- `chatgpt-mcp/install-runtime.py`: 불변 스냅샷
- `chatgpt-mcp/rollout-host-runtime.py`: 전체 검사/스냅샷 설치/터널 connect/관리 서비스 전환을 한 명령으로 수행하려는 도구
- `chatgpt-mcp/rollout_admin.py`: 인식 가능한 systemd user 서비스만 교체하는 보조 모듈
- 소스 측 단위/모의 테스트 통과 기록: 관련 76/76, tossApp disposable PostgreSQL 통합 107/107 (이전 대화 도구 로그). **실제 host runtime/DB/웹 통합 성공은 확인되지 않음**.

## 원인 후보(확정 아님)
1. `rollout-host-runtime.py`가 테스트 실패, `TUNNEL_ID_UNAVAILABLE`, 키/설정 문제 등에서 조기 종료했을 수 있음. 관련 오류 출력이 창 종료로 유실됐을 수 있다.
2. `tunnel-client runtimes connect`가 세션 또는 연결된 프로세스에 영향을 주었을 수 있다. CLI 응답 stdout/stderr를 `subprocess.DEVNULL`로 폐기하여 이 구간 진단성이 낮음.
3. 런타임 전환은 됐지만 ChatGPT 플러그인 도구 스키마 캐시 또는 다른 설치 스냅샷을 보고 있을 수 있음.
4. `rollout_admin.parse_execstart`는 매우 제한된 ExecStart만 인식한다. 실제 user service가 래퍼 `~/.local/bin/personal-ops-admin`을 쓰면 admin 업데이트는 `manual_review_required`가 될 수 있다.
5. `--mcp-command 'python3 <new_release>/server.py'`가 실제 호스트에서 MCP 의존성 패키지를 찾지 못할 가능성 있음. **실제 원인은 호스트 상태 조사로 판별할 것**.

## 호스트 에이전트가 먼저 해야 할 일 (읽기 전용)
1. 현행 tunnel-client runtime alias 상태, 실제 MCP subprocess PID·시작 인자·설치 경로와 현재 release manifest SHA 확인. 터널 ID와 키 값 자체는 로깅/출력하지 말 것.
2. `~/.local/lib/chatgpt-coding-mcp/releases/` 내 가장 최근 설치된 소스 스냅샷과 live process image/argv 일치 여부 확인.
3. `personal-ops-admin.service` 현재 ExecStart, status/최근 systemd journal에서 token·secret 노출 없는 핵심 상태만 확인.
4. `rollout-host-runtime.py` 실패 구간을 재실행 없이 확인할 수 있는 private diagnostic receipt 및 단계별 코드 기록 기능 추가. subprocess 오류는 민감 출력 없이 sanitization; 호스트 터미널 그룹 손실 방지.
5. 소스 `server.py`와 설치된 release의 도구 목록 직접 검사; MCP 패키지/파이썬 환경 검증. 의존성 문제면 정확한 Python interpreter 경로로 수정.
6. 격리 테스트 + 실제 호스트 사전검증 완료 후, **로컬 호스트에서 사용자 승인** 받은 새 release 설치/alias 전환. 기존 스냅샷 보존, 실패하면 기존 alias로 복구 가능하게.
7. 새 채팅/플러그인 새로고침 후 신규 두 진단·계획 도구 및 웹 UI 노출 실증.

## 안전 제약
- 새로운 실 DB SQL 쓰기·migration·Docker restart/배포는 사용자 승인 없이는 진행하지 말 것.
- `tarororo_test` DB/Worker/API identity, 기존 Tailscale 443/8443 Funnel/Serve, credential 파일을 임의 변경하지 말 것.
- `personal-ops` 및 `tossApp`에 다수 미커밋 변경과 기존 사용자 작업이 있다. 무차별 reset/checkout/commit/push 금지.
- 토큰/비밀번호/DB 사용자 데이터/터널 ID는 대화, 작업 로그 또는 커밋에 출력하지 말 것.
- 새 코드를 설치하는 것과 DB 013을 적용하는 것은 별도 단계와 승인.
- 스키마 013 실 DB 적용 전에 DB 이전 12개 마이그레이션 체크섬 검증, 백업 파일 격리/복원 검증 필요.

## 완료 조건
- 실제 PID의 MCP argv와 새 불변 runtime snapshot digest 일치.
- 터널 프로세스 running/healthy/ready 확인.
- 모델에서 `get_db_migration_status`가 조회되고 013 세부 상태 확인 가능.
- `prepare_db_migration` 및 Worker mode 관리 도구가 정확한 스키마로 노출.
- 지정된 HTTPS 관리 UI에서 승인 화면이 표시되고, 인증·Origin·단일 사용 승인의 회귀 테스트 통과.
- 실제 DB는 여전히 원상태(별도 DB migration 승인 전), API/Worker 정상.
- 결과 요약, 복구 방법, 실패 시 로그 위치와 종료 코드 남기기.
