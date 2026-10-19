# 모바일 호스트 접근 승인 요청 딥링크 — 2026-10-16

## 사용자 목표
채팅에서 저장소 접근 권한을 요청하고 선택 버튼 한 번으로 다음 승인 단계로 이동하고 싶음. 각 저장소마다 관리 웹 메뉴/요청 ID를 찾아 선택하고 토큰을 반복 입력하는 흐름이 불편함.

**보안 원칙:** LLM/채팅 메시지 자체는 소유자 인증이 아니므로 사용자 프롬프트만으로 실제 호스트 권한을 발행하지 않는다. 채팅에서는 `request_host_access`(요청 생성)까지 가능하고 실제 `approve_web`은 owner 인증/검증이 필요. 플랫폼 보증 없는 인앱 “승인” 가짜 버튼 금지.

## 준비된 소스 변경 — 운영에 미배포
- `chatgpt-mcp/admin_server.py`: `/host-access?project=<등록 프로젝트>&request_id=<32hex>` GET만 허용. 정확한 키 2개/등록 프로젝트 검증, 토큰 및 중복 키 거부, 공개 링크에는 자격증명 없음. HTML GET은 권한을 부여하지 않음.
- `chatgpt-mcp/admin.js`: `parseHostAccessFocus` 엄격 검증, 해당 프로젝트 자동 선택, 접근·승인 탭 활성화.
- `chatgpt-mcp/host-access-admin.js`: 요청 ID 정확히 선택, 요청이 만료됐거나 사라진 경우 다른 요청을 자동 선택하지 않음. 첫 접근 시 패널로 이동. 기존 새로운 관리자 토큰/프로젝트명 확인 및 Codex 별도 동의 유지.
- `chatgpt-mcp/tests/test_admin_server.py`, `tests/test_host_access_ui.mjs`, `tests/check_admin_ui.py` 테스트 추가. 기존 미커밋 변경과 타 세션 WIP 유지, git commit/push/reset/stash 없음.

## 검증한 증거
- 격리된 Python HTTP 테스트: `test_admin_server.py` 26/26, `test_admin_host_access.py` 5/5 PASS.
- `test_host_access_ui.mjs`: 격리 환경의 Node12는 `node:test`를 제공하지 않아 **임시 호환 어댑터(테스트 본문 변경 없이, 임시 파일)**로 실제 `host-access-admin.js` 실행, 14/14 PASS. 정확히 해당 요청 선택/만료 링크가 대체 요청으로 자동 선택되지 않음을 검증함.
- 실제 `admin.js`의 `parseHostAccessFocus` 추출 테스트: 유효 링크 1개 및 잘못된 링크 5개 PASS.
- Browser test `check_admin_ui.py`용 회귀 테스트는 추가했으나, 격리 환경에 Playwright Python 패키지 없음. 실제 모바일 화면 검증 미실행.
- 호스트 테스트를 하려 했지만 `get_host_access(personal-ops)`가 예상치 못하게 `RUNTIME_CHANGED`로 전환됨. 접근 승인 없이 실제 호스트 명령을 호출하거나 관리자 서비스를 재시작하지 않았음.

## 남은 작업
1. 소스 충돌·변경 여부 재검증, Node24 및 실제 브라우저 회귀 테스트. 이전 사용자 승인 grant가 `RUNTIME_CHANGED`로 무효화된 원인과 현재 설치 런타임 버전 읽기 전용 확인.
2. 안전한 공식 런타임/관리 웹 배포(독립적인 소유자 확인 절차 및 source fingerprint 적용); **현재 실제 모바일 관리 웹에는 아직 적용되지 않음**.
3. 새 딥링크 제공은 이후 채팅 `request_host_access` 응답의 `project`, `request_id`로 만들 수 있으나 반드시 배포 후에만 제공.
4. 프롬프트/ChatGPT 내 실제 최종 권한 부여는 플랫폼의 신뢰할 수 있는 사용자 확인/신원 검증 없이 지원 불가. 장기 개선으로 공식 승인 UI/패스키(WebAuthn) 등 검증 가능한 소유자 확인 통합을 검토.
