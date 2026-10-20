# 호스트 명령 카드의 승인·실행 단순화

2026-10-20 KST. 사용자 승인 후 서비스 전환과 설치본·실제 관리 웹 검증을 완료했다.
현재 운영은 `1f93d474c9b8f944fdc2`, 이전 운영은 `788381864215c1494db0`다.
구현 커밋은 `11577c8`이다. 실제 ChatGPT의 새 카드 클릭은 다음 유효한 작업 계획에서 확인한다.

## 확인한 문제

웹에서 승인된 `tossapp-deploy-test` 계획의 후속 `execute_host_command` 호출이
`user rejected MCP tool call`로 끝났다고 보고됐다. 이 문자열만으로 사용자 취소와
ChatGPT 클라이언트 취소를 구분할 수는 없다. 해당 호스트 기록은 승인 영수증은 있으나
`started_at=null`이고, 생성부터 10분이 지나 만료됐다. 승인은 만료 84초 전에 처리됐다.
이번 작업에서 그 계획을 되살리거나 실제 업로드를 재시도하지 않았다.

## 새 흐름

```text
prepare_host_command
  -> [approve and execute]
  -> confirm_and_execute_host_command (app only)
  -> one-use receipt -> locked command queue
  -> get_host_command
```

새 카드의 **승인하고 실행** 버튼은 한 번의 앱 콜백에서 기존 승인 영수증을 발급하고
기존 실행기의 대기열에 정확한 명령을 넣는다. 웹 모델의 두 번째 실행 호출이 필요 없다.
카드에 고정된 프로젝트·프로필·영향 범위·입력 해시와 남은 유효시간을 먼저 표시한다.

- 예전 **이 계획 승인** 콜백은 승인만 한다. 기존 카드와 관리자 복구 경로를 보존한다.
- 카드를 열거나 승인된 계획을 다시 여는 것만으로 실행하지 않는다.
- 중복 클릭·동시 콜백은 같은 명령을 두 번 시작하지 못한다.
- 카드와 응답의 모든 불변 필드를 대조한다. 다른 계획의 응답·통신 오류는 잠그고 상태를 조회한다.
- `execution_requested=true`면 결과 조회로 이어가며 `execute_host_command`를 다시 호출하지 않는다.
- 승인 직후 만료된 계획은 실행하지 않았다고 표시한다. 만료를 자동 연장하거나 재승인하지 않는다.
- 확인용 capability는 비공개 메타데이터에만 둔다. 모델 문맥에는 실행 접수 여부와 공개 계획 상태만 보낸다.
- 등록 프로필·일반 호스트 권한·유효시간 정책을 변경하지 않았다. 일반 호스트 권한 최대 8시간과
  등록 명령 계획의 생성 후 10분은 별개다. 플러그인의 Allow all actions를 이 두 승인으로 간주하지 않는다.

## 검증과 실제 적용

독립 검토에서 찾은 실행 결과의 관리 링크 필드, 새 도구 허용 목록 누락, 오래된 위젯 설명,
카드 응답의 불변 필드 대조를 보완했다. 마지막 검토의 미해결 지적은 없다.
개별 회귀검사는 가짜 실행기와 임시 프로필만 사용했다. 서버가 실제로 생성한 합성 응답을
Chromium 카드에 넣어 실행 시작 표시·모델 문맥·후속 실행 호출 없음도 확인했다.
이는 실제 ChatGPT 화면 클릭이나 실제 업로드 성공을 뜻하지 않는다.

공식 최종 preflight는 exit 0 / `preflight_ok`다. 관련 Python 891개·전체 Python 1107개,
브라우저 5종 게이트가 모두 종료 코드 0이다. 별도 Node UI 13개, 서버 생성 합성 응답의
Chromium 확인 1개, Ruff·AST·diff 검사도 통과했다. 관련 검사 수를 전체 검사 수에 더해
서로 다른 검사 개수로 계산하지 않는다. 최종 영수증은
`ready-preflight-receipts/rollout-yn51hvh3/receipt.json`이다.
앞선 두 사전검사는 검토 보완을 새 동결본에 반영하기 위해 직접 중단했으며 원본 종료
영수증을 보존했다. 최종 검사에서는 더 수정하거나 검증을 건너뛰지 않았다.

운영 manifest 91파일이 실제 파일과 같고 변경 대상은 `host-command-approval.html`,
`host_command_ui.py`, `server.py` 세 파일이다. 대시보드 자산은 동일하다.
사전검사에서 실제 MCP PID1631490 / 운영 버전 `788381864215c1494db0`,
profile_aligned/service_aligned=true, installed/switched=false를 확인했다.
관리 웹은 HTTP 200이며 Codex 관측기 PID3208094도 기존 프로세스로 실행 중이다.
실제 MCP의 일반 호스트 권한 조회는 EXPIRED / 요청 없음이다. 확인용 새 권한 카드는 만들지 않았다.
다른 세션의 미추적 파일 11개와 호스트 설정 12개의 해시·파일 모드도 기준선과 같다.
활성 호스트 작업은 조회 시 0건이며 전환 직전에 다시 확인한다.

사용자의 “진행” 승인에 따라 동결본의 공식 `rollout-host-runtime.py --apply --no-pause-on-error`를
별도 SSH PTY에서 실행했다. 공식 검사에서도 관련 891개·전체 1107개와 브라우저 게이트가
모두 통과했다. 확인 문자열 입력 직전 활성 호스트 작업 0건, 소스·제어 해시·설정·WIP 일치를
다시 확인한 뒤 터널과 관리 웹만 전환했다.

추가 운영자 검사에서 콘솔 파일의 미배출 버퍼 때문에 잠시 멈췄다. 실제 PTY 출력과 공식
사전검사 영수증은 이미 존재했다. 해당 `/usr/bin/script` 프로세스만 PID fd로 확인하고,
로컬 매뉴얼의 SIGUSR1 로그 배출 기능으로 콘솔을 저장한 뒤 검사를 통과했다.
서버 승인·유효기간·소스 검사·공식 TTY 절차를 우회하지 않았으며 실패 기록도 보존했다.

- 실제 MCP PID801172 / 시작 ticks8013415가 새 운영 `server.py`를 실행한다. 설치 manifest
  91파일이 실제 파일과 같다. 관리 웹 PID801719는 active/running, 터널은 정상 oneshot의
  active/exited다. 세 서비스의 NRestarts는 0이며 관측기 PID3208094도 그대로다.
- 공식 결과는 exit 2 / `verification_required`이고 설치·전환·런타임 검증·터널 건강은 true,
  admin_status는 admin_ready다. passive catalog가 awaiting_discovery / tool_count=0이라
  남은 상태이며 실제 서비스 전환 실패로 해석하지 않는다. 원본 영수증은
  `apply-receipts/rollout-jrt45jaj/receipt.json`이다.
- 새 설치 서버의 별도 stdio에서 도구 82개, 앱 전용 `confirm_and_execute_host_command`와
  정확한 입력 스키마·카드 리소스 해시를 확인했다. 이 SDK 발견 증거를 passive 영수증에
  덮어쓰지 않는다. 실제 터널의 `list_host_command_profiles(tossApp)`도 enabled=true와
  기존 프로필 4개를 반환했다.
- 설치본을 사용하는 임시 프로필·가짜 실행기로 새 결합 경로와 기존 승인 전용 경로의
  MCP 통신 검사 2개를 통과했다. 실제 업로드 계획·승인·명령은 생성하거나 실행하지 않았다.
- 실제 LAN 관리 화면에서 호스트 연결과 personal-ops 작업실 live를 확인했다. 브라우저 오류와
  로그인 후 변경 요청은 없다. loopback/LAN의 JS·CSS 6개 응답이 설치본 해시와 같다.
- 관측한 기존 CLI 8개는 변경 0개로 보존됐다. 다른 호스트 설정 12개, 다른 세션의 미추적
  파일 11개 및 tmp/를 보존했다. 승인된 설정 변경은 터널 YAML과 두 서비스 override의
  런타임 경로 3개뿐이며, 나머지 바이트·권한은 정규화 해시로 대조했다.
- 컨테이너 12개의 ID·이미지·시작 시각·상태가 기준선과 같다. DB·스키마·AIT·실제 업로드·
  BitBake 작업은 수행하지 않았다.

일반 ChatGPT에서 플러그인 연결을 한 번 새로고침하고, 실제 작업이 필요할 때 새 유효
계획의 **승인하고 실행** 버튼을 사용한다. 과거 만료 계획은 재사용하지 않는다.
실제 ChatGPT 카드 클릭은 호스트 합성 검증과 구분한다. 업로드 자체는 해당 계획의
사용자 승인 없이 실행하지 않는다.

증거: `/home/jhw/.local/state/personal-ops-command-oneclick/20261020-t8w1q8tr/`.
UI RED/GREEN: `/home/jhw/.local/state/personal-ops-ui-red-20261020-01/`.
두 디렉터리는 0700, 증거 파일은 0600이다. 후보 소스는 원래 파일 모드를 보존한다.
