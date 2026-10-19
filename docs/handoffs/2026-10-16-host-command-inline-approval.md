# 업로드 계획을 채팅 카드에서 승인

날짜: 2026-10-16 KST. 상태: 승인 후 운영 전환·서버/프로토콜 검증 완료.
실제 ChatGPT 업로드 카드 표시와 Claude 프로세스 1개의 사후 종료 원인은 별도 미확인이다.
현재 운영 버전은 `8c7a5aff0c75198c6c5d`(84파일), 이전 버전은 `0c5c451ed88e5661126d`다.

## 확인한 원인

- Schema014와 호스트 권한에는 채팅 카드가 있지만, 등록형 업로드 명령은 별도 경로였다.
  `prepare_host_command`의 설명이 관리 웹에서만 일회용 승인을 받도록 명시하고 있었다.
- 엔진은 `/host-commands?operation_id=<id>` 상대 URL을 반환한다. 이 경로는
  `operation_id` 하나만 받는 별도 관리자 경로이며, Schema014의 `/host-operations`와 다르다.
  클릭한 실제 URL이나 브라우저 오류가 없어 이번 접속 실패의 정확한 URL 변형은 확인하지 못했다.
- 실제 최신 계획은 `tossApp` / `tossapp-upload-ux014` /
  `6d9c1369e3194b857390b6fd4ccb8424`다. 17:41:55 KST 생성, 17:51:55 만료다.
  18:03 조사 시 저장 상태는 pending이지만 시간상 이미 만료됐고, 실행 호출은 없었다.
  이 조사에서 계획 상태를 변경하거나 새로운 실제 업로드 계획을 만들지 않았다.

## 보완 범위와 승인 경계

- `prepare_host_command`가 정확한 프로필 계획의 채팅 카드를 반환한다.
  기존 계획은 `open_host_command_approval`로 연다.
- `confirm_host_command_approval` / `cancel_host_command_approval`은 앱 전용이다.
  숨겨진 일회용 capability는 `_meta`에만 전달하며 프롬프트, 공개 결과나 오류에 넣지 않는다.
- 소유자·부팅·설치 런타임·작업 ID·계획/소스 해시·만료를 고정한다.
  기존 실행 파일·인자·설정·입력 아티팩트 검증과 일회용 영수증을 그대로 사용한다.
- 승인은 영수증 발급만 한다. 실제 빌드·업로드·초대장 발송은 사용자가 요청한 경우의
  별도 `execute_host_command`로만 실행한다. 호스트 기간제 권한과 이 승인을 합치지 않는다.
- 승인된 계획을 다시 열면 기존 영수증과 다음 동작을 반환하며 또 승인하라고 요청하지 않는다.
  관리자 승인과 취소가 경합하면 이미 발급된 유효한 승인을 취소됐다고 표시하지 않는다.
- MCP 공개 응답에서 기존 상대 승인 URL을 제거한다. 기존 인증된 관리 웹은 사용자가
  명시적으로 요청하는 복구 경로로 남긴다. 프로필 등록·권한 정책·유효시간 10분은 바꾸지 않는다.
- 새 카드 자산과 Python 모듈을 설치 목록과 런타임 무결성 검증 목록 양쪽에 포함한다.
  공식 롤아웃의 브라우저 게이트와 진단 기록에도 새 카드를 포함한다.

## 검증과 적용

- 실제 MCP 프로토콜에서 기존 prepare 응답에 카드 리소스가 없음을 먼저 재현했다.
- 구현 후 새 백엔드·기존 명령/관리 웹 관련 45개, 진단 기록 14개 검사가 통과했다.
  임시 입력 파일의 권한을 올바르게 설정하지 않아 생긴 첫 fixture 오류는 수정하고 기록을 보존했다.
- 독립 검토에서 발견한 설치/신뢰 검증 목록 불일치를 수정했다. 새 파일의 바이트가 바뀌면
  설치 런타임 신뢰가 거부되는 기존 회귀검사를 그대로 확장했다.
- 프런트엔드 독립 검사는 Node 9개, Chromium 4개 및 320/390/1440px 높이 조절을 통과했다.
  실제 프로토콜 테스트의 실행은 임시 프로필과 fake runner에만 한정한다.
- 첫 공식 사전검사는 브라우저 12/4/4/7개를 통과한 뒤 대상 Python 726개 중 웹 롤아웃의
  스냅샷 일치 검사 1개에서 중단됐다. 웹 경로의 제어 해시 목록에도 새 브라우저 검사 파일을
  추가해야 했다. `WEB_ROLLOUT_SOURCE_CHANGED`를 단독 재현하고 목록만 확장한 뒤,
  관련 웹 롤아웃 19개를 통과했다. 권한·영수증 검증은 완화하지 않았다.
  첫 실패 영수증 `preflight-receipts/rollout-8x32vxak/receipt.json`을 보존하고 재검사한다.
- 두 번째 공식 사전검사는 대상 726개를 통과한 뒤 전체 892개 중 기존 다중 프로젝트
  라우터 검사 1개에서 중단됐다. 이 검사의 명시적 도구/리소스 허용 목록에 새 카드 3도구와
  리소스 1개를 반영하고 해당 라우터 검사를 단독 통과했다. 두 번째 실패 영수증
  `preflight-retry-receipts/rollout-pfx7q5fg/receipt.json`도 그대로 보존했다.
- 원본 체크아웃에서 최종 공식 preflight가 exit 0 / `preflight_ok`다.
  호스트 권한 UI 12개, Schema014 UI 4개, 업로드 승인 UI 4개, 런타임 UI 7개,
  대상 Python 726개, 전체 Python 892개가 모두 통과했다. Node 9개·AST·Ruff·diff 검사도 통과했다.
  최종 영수증은 `preflight-final-receipts/rollout-6fcgnguh/receipt.json`이다.
- profile_aligned/service_aligned 모두 true, source_files 84, 사전검사 당시 MCP PID 2751660과
  tunnel PID 2751644가 이전 운영 snapshot에서 실행 중임을 확인했다.
  그 사전검사의 installed=false / switched=false 기록은 그대로 보존한다.
- 독립 보안·코드 검토의 남은 차단 사항은 없다. 모바일/PC 카드 스크린샷도 확인했다.
  소스 19파일을 기준선 해시/모드 비교 후 원본에 반영했고, 관련 없는 WIP·HEAD/index를 보존했다.
- 실제 `tossapp-upload-ux014`의 실행 파일·입력 파일 해시 검증이 성공했다. 새 실제 계획이나
  영수증을 만들지 않았다. 호스트 loopback의 관리 루트·JS·형식이 올바른 기존 승인 URL은
  모두 HTTP 200이며, 이것이 휴대폰/PC의 접속 성공까지 증명하지는 않는다.
- 위 기록은 서비스 전환 전 단계다. 업로드의 실제 승인·실행, DB/Docker 변경,
  다른 CLI에 대한 종료 명령, 커밋/push는 이 호스트 작업에서 하지 않았다.

기존 영수증 발급기의 receipt 쓰기 성공과 approved 상태 저장 사이에 디스크 오류가 나면
실행을 허용하지 않는 고아 영수증이 남을 가능성은 기존 가용성 제한이다. 이번 구현이
임의 실행을 허용하는 경로는 아니며, 승인 상태가 불확실하면 자동 실행/자동 재승인하지 않는다.

증거: `~/.local/state/personal-ops-rollouts/host-command-inline-20261016/repair-bhugv0nv/`.
프런트엔드 증거: `~/.local/state/personal-ops-rollouts/host-command-ui-NXoD6s/`.

## 승인 후 실제 운영 반영

사용자가 “보완본 서비스 전환 승인”을 선택해 후보 `8c7a5aff0c75198c6c5d`를 승인했다.
기존 공식 롤아웃과 실제 PTY를 별도 systemd user unit에서 실행해 터널·관리 웹을 전환했다.
확인 문자열 입력 직전에 소스/제어 해시를 재검증하고 실행 중인 호스트 작업 0건을 확인했다.
터널의 관리 Codex app-server도 전환 범위였으며, 진단 기록은 독립 실행 범위에 보존했다.
등록 프로필·권한·인증정보·Docker·DB·스키마·실제 업로드는 변경/실행 대상이 아니다.

공식 실행은 원본의 `chatgpt-mcp/rollout-host-runtime.py --apply --no-pause-on-error`다.
테스트/TTY/소스 검증을 우회하지 않았다. 적용 과정에서도 UI 12/4/4/7개, 대상 726개와 전체
892개 검사가 통과했다. 원본 `activation/pty-receipts/rollout-ym9ujy28/receipt.json`은
exit 2 / `verification_required` 그대로 보존한다. installed/switched/runtime_verified/
tunnel_healthy는 true, admin_status는 admin_ready다. exit 2는 같은 ChatGPT 연결의 전체
도구/스키마 재발견이 아직 확인되지 않아 생긴 상태이며 서비스 전환 실패를 뜻하지 않는다.

- 실제 MCP PID 93891, tunnel PID 93873, 관리 웹 PID 94041이 운영 snapshot을 실행한다.
  manifest 84파일 모두 일치한다.
- 설치본의 별도 MCP stdio에서 도구 76개, 새 승인 카드 리소스와 앱 전용 confirm/cancel을
  확인했다. `tossapp-upload-ux014`를 포함한 세 프로필 조회도 성공했다.
- 실제 설치 서버의 임시 합성 상태 프로토콜 3개를 통과했다. 승인/재개봉과 별도 실행을
  fake runner로만 검증했으며, 실제 업로드 계획·영수증·호스트 권한을 만들지 않았다.
- 실제 ChatGPT 터널의 `list_host_command_profiles(tossApp)`와 `get_host_access(tossApp)`
  읽기 호출도 성공했다. 일반 호스트 권한은 EXPIRED이며 자동 갱신하지 않았다.
  등록형 업로드 계획의 1회 승인은 이 기간제 권한과 별도다.
- 관리 웹은 loopback/LAN/Tailscale HTTPS의 자산 15개 응답과 설치본 해시를 검증했다.
  채팅 카드의 실제 ChatGPT 표시는 별도 사용자 확인을 요청한 상태다.
- 문서 후속 갱신 전 저장소 344파일·HEAD/index, 레지스트리/인증정보/권한 파일이 보존됐다.
  승인된 변경은 기존 터널 YAML과 두 서비스 `20-runtime.conf`의 실행 버전 경로 3개뿐이다.
  컨테이너 11개의 상태와 시작 시각도 동일하다.

## CLI 보존 검사에서 남은 관측

CLI 기준선 11개 중 2개는 서비스 전환 직전에 이미 종료돼 있었다. 직전 생존한 9개 중
8개는 사후에도 동일 PID/시작 시각으로 생존했고, Claude PID 54163 하나는 사후 검사에서
없어졌다. 처음 엄격한 보존 검사는 이 차이로 실패했으며, 그 검증기를 수정하거나
전원 보존 성공으로 처리하지 않았다.

전환 직전 생존 시각은 18:43:40.956 KST이고, 터널 전환 로그는
18:43:41.744~18:43:42.854다. PID의 종료 로그·OOM/충돌 증거가 없고, 기준선에
PPID/cgroup/종료 코드가 없어 자연/사용자 종료와 서비스 전환 영향을 구분할 수 없다.
공식 롤아웃 완료 시각 자체는 CLI 사후 검사 시각이 아니므로 종료 구간을 3초로 단정하지 않는다.
원인은 미확인이며 서비스 전환 영향을 배제할 수 없다. 다른 CLI를 재실행/종료하지 않았다.
다음 전환에서는 PPID·SID·TTY·cgroup과 가능한 종료 상태까지 함께 기록해야 한다.

별도 `activation/installed-verification-observation.json`은 런타임/파일/설정/HTTP 검증과
CLI 보존 assertion 실패를 함께 기록한다. `cli-exit-audit.json`, 원래 엄격한 검증기,
`pre-switch-guard.json`, `preservation-baseline.json` 및 원본 적용 영수증을 참조한다.

## 웹에서 이어갈 때

운영 전환은 끝났다. 웹 연결의 새 도구 정의를 반영한 후, 새 `request_id`로 `tossapp-upload-ux014`의
업로드 계획을 준비한다. 이번에 관측한 계획은 이미 만료됐으므로 재사용하지 않는다.
채팅 카드에서 정확한 프로젝트·프로필·계획을 확인하고 사용자가 승인한다.
원래 업로드 요청이 유효하다면, 웹 모델이 승인 확인 후 별도 실행과 결과 조회를 이어간다.
이 문서 자체나 호스트 작업의 성공을 실제 업로드 승인으로 취급하지 않는다.
