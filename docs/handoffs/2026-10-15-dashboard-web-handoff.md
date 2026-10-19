---
artifact_contract: "ce-handoff/v1"
created_at: "2026-10-15T12:38:10.731974+00:00"
title: "대시보드 8차 적용 완료 및 웹 채팅 인계"
summary: "말풍선 운영 적용과 실환경 검증 완료; 비차단 관측 한계와 WIP 보존 범위"
keywords: ["personal-ops", "dashboard", "speech-bubbles", "web-handoff"]
cwd: "/home/jhw/ai/opencode/projects/personal-ops"
resume_focus: "대시보드 사용 확인 및 비차단 후속 사항 판단"
repository: "personal-ops"
branch: "main"
head: "cf8b6d2608c9cadaa939fdca8045891e848ec7ba"
worktree_path: "/home/jhw/.local/state/personal-ops-worktrees/dashboard-bubbles-20261015"
---

# 웹 채팅 인계: 대시보드 8차 완료

사용자의 최신 요청은 "이제 남은거있나 없으면 웹으로 넘기고"다.
대시보드와 말풍선의 필수 구현·테스트·소스 반영·운영 적용은 완료됐다.
새 기능을 계속 추가하라는 요청이나 커밋/DB 변경에 대한 승인은 아니다.
웹에서 현재 상태를 인계받고 후속 범위를 결정하면 된다. 이 문서만으로 재배포할 필요는 없다.

## 현재 상태와 검증

- 실제 운영 버전: `41339f4dc09de5363e0d`. 직전 `500107e94ce6308289ab` 대비
  model.js/visualizer.js/visualizer.css 세 표시 파일만 변경됐다.
- 사용자 "적용" 승인 후 2026-10-15 21:33 KST경 터널과 관리 웹을 전환했다.
  installed/switched/runtime_verified/tunnel_healthy=true, admin_status=admin_ready다.
  이 문서 생성 직전에도 해당 릴리스의 server.py와 admin_server.py 실행을 확인했다.
- 실제 personal-ops 새 작업 이벤트에서 '작업 시작!' 말풍선을 관측했다.
  완료·도구 응답·문제·시간 초과·취소·중단·결과 불명은 서로 다른 고정 문구/색이다.
  일반 4초, 확인 필요 6초, 전체 최대 3개/방별 최대 2개, 겹침 방지 정책을 쓴다.
  첫 로드의 과거 이력·반복 조회·백그라운드 복귀는 새 말풍선으로 재생하지 않는다.
  추가 모델 호출/토큰 소비는 없다. 관측하지 않은 실수/추론/실패 원인은 만들지 않는다.
- Node 181/181, 브라우저 82/82, 독립 코드 검토 APPROVE가 통과했다.
  운영 전환 직전 필수 Python 619/619와 전체 784/784를 다시 통과했다.
  설치 파일 73개 SHA/owner/모드, LAN/loopback HTTP 16건, 세 프로젝트
  personal-ops/tossApp/pim-check, 정원 6명, opaque frame, 모바일 320/390px
  다섯 탭 10건을 확인했다. JS 오류나 로그인 후 브라우저 변경 요청은 없었다.
- 대시보드 주소: http://192.168.2.70:8765/ . 새로고침 후 새 작업 이벤트로 확인한다.
  기존 2D 방/입퇴장/업무 요약/최신 기록 정렬과 페이지 복귀 동기화도 유지된다.

## 남은 사항과 한계

1. 비차단 관측 한계: passive 전체 MCP catalog는 awaiting_discovery이고 원본 롤아웃
   영수증은 exit 2/verification_required다. tools_registered=false와
   connector_schemas_verified=false를 ready로 바꿔 쓰지 않는다.
   실제 ChatGPT get_host_access 읽기는 새 런타임에서 성공했다. 이번 변경은 MCP
   정의를 바꾸지 않았으므로 이를 해결하려 재시작/권한 재발급을 반복하지 않는다.
2. 코드 정리/커밋은 별도 범위다. main/HEAD는 위 메타데이터와 같고 미커밋 변경이 많다
   (본 문서 작성 전 git status 187항목). 다른 세션의 Schema 014 WIP가 섞여 있다.
   이 세션은 커밋·push·PR·Notion 저장을 하지 않았다. 인계에 커밋은 필요하지 않다.
3. 표시에는 수집 가능한 관측 한계가 있다. ChatGPT 비공개 추론·외부 웹검색·다른 MCP
   내부 동작은 자동 관측되지 않는다. 관측/채팅 보고/과거 재생의 출처를 구별한다.
   이번 실환경에서 성공/실패 이벤트를 강제로 발생시키지는 않았다. 해당 말풍선은
   격리 브라우저 검사로 검증했고 실환경은 실제 시작 이벤트로 확인했다.

## 권한과 다른 세션 보존

- 기존 personal-ops HostAccess는 인계 직전 실제 조회에서 active=true,
  allow_codex=false, 만료 2026-10-15 23:01:47 KST다. 이후 만료 여부는 다시 조회한다.
  이 문서나 일반 작업 승인을 권한 갱신/호스트 Codex 허용으로 간주하지 않는다.
- 기존 권한 두 레코드와 보호 설정/키 다섯 개의 SHA/모드를 유지했다.
  전환 시점의 독립 CLI 세 개와 원본 311개 파일을 보존한 뒤 기존 문서 두 개만 갱신했다.
  승인 범위의 터널 소유 app-server 두 개만 교체했다.
- 다른 세션의 tossApp 작업·Docker·DB·Schema 014는 이 대시보드 변경에 포함하지 않았다.
  새 서비스 전환/DB 작업은 그 요청의 범위와 필요한 승인을 확인한다.
- 원본 전체를 그대로 배포하거나 다른 세션 WIP를 덮어쓰지 않는다. 현재 Git HEAD만으로
  검증 소스를 복원할 수 없다. 아래 호스트 전용 worktree/증거는 아직 필요하다.

## 먼저 볼 참조

- `DESIGN.md`, Stage 8 requested event speech 및 Current operational activation:
  현재 표시 정책과 운영 적용 상태다.
- `docs/handoffs/2026-10-15-dashboard-operations-stage1.md`, 마지막
  '8차 운영 적용 완료 (2026-10-15 21:33 KST)' 절:
  전체 단계의 역사와 검증·보존 근거다. 문서 앞쪽의 '현재 버전'은 당시 기록이므로
  마지막 적용 결과와 이 인계의 최신 버전을 기준으로 한다.
- `work-dashboard/model.js`의 speechEvents:
  실제 상태 변화만 고정 문구로 바꾸는 모델이다.
- `work-dashboard/visualizer.js` 및 visualizer.css:
  기존 캐릭터/역할에 연결한 말풍선 배치·표시 제한·독립 만료·관찰 중지 정리다.
- `work-dashboard/tests/bubbles.test.js`,
  `chatgpt-mcp/tests/check_admin_ui.py`의 test_event_speech_*:
  모델 9건과 실제 프레임의 말풍선 회귀 검증이다.

호스트 전용 경로(웹 MCP의 일반 파일 읽기 범위 밖일 수 있음):
- `/home/jhw/.local/state/personal-ops-worktrees/dashboard-bubbles-20261015`:
  검증한 격리 배포 소스. 정리/삭제하거나 원본 WIP와 섞지 않는다.
- `/home/jhw/.local/state/personal-ops-dashboard/dashboard-bubbles-20261015/activation/`:
  final-observations.json은 최종 사실, live-verification.json은 실제 3개 프로젝트 화면,
  live-speech-verification.json/live-speech-1.png는 실제 말풍선 증거다.
  rollout/rollout-e92cw_gj/receipt.json은 원본 exit 2 상태를 보존한 배포 영수증이다.
  이 private 증거에 접근이 막힌 것을 대시보드 적용 실패로 해석하지 않는다.

이 문서는 personal-ops 저장소 안에 있으며 플러그인의 read_file로 읽을 수 있다.
Git에는 아직 커밋하지 않았다. 현재 호스트 checkout을 공유하는 웹 채팅에서 접근하며,
다른 PC/새 clone에는 미커밋 파일이 자동 전달되지 않는다.
