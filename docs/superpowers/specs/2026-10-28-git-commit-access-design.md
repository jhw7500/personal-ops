# Git 로컬 커밋 24시간 승인 설계 및 개발 체크포인트

## 요청과 범위
2026-10-28 사용자가 반복되는 Git 커밋 승인을 줄이기 위해 personal-ops Git 권한 개선 진행을 요청했다. 대상 환경은 cantopsbuildserver의 personal-ops 소스이며, 최초 사용 대상은 tossApp 저장소의 현재 작업 브랜치다.

한 번의 명시적 사용자 승인으로 동일 저장소·동일 브랜치의 로컬 commit만 최대 86,400초 허용한다. push, force push, reset, amend, merge, checkout, 브랜치 삭제, 런타임 배포, DB 변경은 포함하지 않는다. 일반 host grant를 Git 승인으로 재사용하지 않는다.

## 접근 방식
선택: 기간 제한·철회 가능한 저장소/브랜치 범위 local-commit grant. 이후 커밋마다 변경 파일과 메시지를 고정한 계획은 계속 작성하되, 같은 범위의 추가 사람 확인만 재사용한다.
대안: 정확히 고정된 다중 커밋 배치. 변경된 작업을 계속 진행하려는 현재 요구에는 다시 승인이 잦으므로 우선하지 않는다.
금지: 임의 shell/git 실행 권한으로 기존 Git 승인 경계를 우회하거나 실행 중 불변 런타임/승인 레코드를 직접 수정하는 방식.

## 유지할 보호
- 기존 GitWorkflows의 HEAD/작업 트리 스냅샷, 메시지 Change Evidence Contract, workspace 접근 검사, 빈 index 및 staged bytes/mode 검사를 유지한다.
- project, canonical repo path 및 device/inode, Git directory 정체성, 정확한 브랜치, expected HEAD, UID, boot ID, installed runtime digest에 승인을 결속한다.
- main/master 및 신뢰된 호스트 설정의 보호 브랜치를 거부한다.
- 벽시계와 monotonic deadline 모두 승인 시 고정하며, 최대 24시간/자동 연장 없음이다. 시계 역행이나 runtime/boot 변경은 거부한다.
- 실행 전 매번 유효성을 확인한다. 성공한 승인 커밋의 실제 결과에 대해서만 expected HEAD를 감사 기록과 함께 갱신한다. 외부 HEAD 변경은 새 승인을 요구한다.
- 철회는 이후 실행 admission을 차단한다. 이미 admission된 제한시간 Git 프로세스가 종료될 수 있다는 점은 UI에 명시한다.

## 발급·UI·상태 저장
발급은 기존 인증된 app-only callback 또는 genuine owner TTY 경로만 허용한다. 카드 열기나 모델의 승인 문자열은 권한이 아니다. 확인 토큰은 private metadata에만 두고 모델 출력으로 노출하지 않는다.
UI는 일회용과 24시간 로컬 commit 옵션을 명확히 구별하고 저장소·브랜치·만료·금지 작업을 표시한다. 기본값은 기존 일회용이다.
승인 저장소는 모든 작업 저장소 외부에 두고 0700/0600, symlink/hardlink 거부, 원자적 저장, 잠금과 검증을 사용한다. 모델 입력 JSON을 승인 레코드나 실제 실행 context로 신뢰하지 않는다.

## 현재 구현·검증 범위
실행 중 서버나 기존 소스를 변경하지 않고 분석용 격리 컨테이너에서 pure policy 초안을 작성했다.
- 제안 파일: chatgpt-mcp/git_commit_access_policy.py
- 제안 테스트: chatgpt-mcp/tests/test_git_commit_access_policy.py
- evaluate_stored_commit_grant는 I/O·Git 실행·권한 발급/저장/갱신을 하지 않는 순수 판정 함수다. 반드시 신뢰된 host store와 executor context에서만 호출해야 한다.
- RED: 모듈 미구현 상태에서 31건의 예상 실패를 확인했다.
- GREEN: 31/31 단위 테스트 통과, 실패 0. Python 3.10 문법 파싱 통과.
- policy SHA-256: 72bbb31304394feaff765881b8d09edbb643ea37fcdd534e25613805eaed9e2f
- tests SHA-256: 20a583246488275e61e6898c0037eac339246535bde9e35a8a7fdd165cfdf344
- 개발 초안은 이 대화의 personal_ops_git_commit_access_draft.zip 첨부에 있다. 호스트 경로에 복사·설치된 파일이 아니다.
- bundle SHA-256: 8fba7592d1d2908ae13c71097c53921c8f93bfc522c6babb18b95a34032b7bfd

읽은 소스 기준: git_workflows.py SHA-256 eedf96a6db15dc544417f179ecdb6a11c776c04bde726316f158b1b59bd0a1ac, git_approval_ui.py SHA-256 64fe14585a5a2586a06edd9ab6a18dd347af20917c1c1ac12716d29b256bd846. 구현 재개 시 현재 내용과 비교한다.

## 아직 하지 않은 작업
private grant store, UI confirmation/revocation, 실제 GitWorkflows executor 연결, 후속 커밋 HEAD 갱신, 전체 회귀, 독립 보안 리뷰, 불변 런타임 배포 및 실제 사용자 grant 발급은 미완료다. 순수 모듈 31건 통과는 전체 기능 또는 호스트 배포 성공이 아니다.
현재 Git 실행기는 여전히 커밋별 일회용 승인을 요구한다.

## 실행 차단 및 승인
현재 get_host_access(personal-ops)는 active=false, reason=RUNTIME_CHANGED이다. start_command의 기존 Git 승인 테스트 실행도 Job history capacity reached; host maintenance is required로 거부되었다. 이를 우회하거나 이력을 임의 삭제하지 않았다.
24시간/Codex 비허용 개발용 호스트 승인 카드 950dc795e271789b6aa5b0a98364d015를 채팅에 열었다. 요청 49c3da234d84758620f4f7d31c47c894는 아직 pending이다. 이 카드는 Git 24시간 grant가 아니라 개발·호스트 검증을 위한 일반 host access다.

## 추가 관측: host access 범위 표시와 실제 승인 기록
동일 연결의 최신 get_host_access(tossApp)는 grant b61ae4c6a6bb8fa3d599ffec81b4acf5, scope=installation, active=true이고 2026-10-29T05:29:19Z까지 유효하다. get_host_access(personal-ops)는 active=false/RUNTIME_CHANGED이다. 모든 호스트 승인이 만료됐다는 뜻은 아니다.
읽은 writable source host_access.py(SHA-256 64dafb807c3a498c7e4e1303c1d2ee98e5cc0782a242892efe2f363c3eec7edf)는 _record_path(project)가 project 해시별 JSON을 사용하고 require/get 또한 해당 프로젝트 기록을 검사한다. 설치 전체 접근이라고 표시하면서 승인 조회·유효성은 프로젝트별 기록에 묶인 구조가 관측 결과를 설명할 수 있다. 이 소스가 설치된 전체 런타임과 바이트 단위로 같다는 점은 아직 검증하지 않았다.
따라서 tossApp의 유효한 grant를 personal-ops grant로 복제하거나 project 인자를 바꿔 거부를 우회하지 않는다. 현재 personal-ops 전용 개발 승인 카드는 그대로 pending이며, 새 Git grant UI는 scope를 저장소·브랜치 단위로 정확히 표시해야 한다.

## 다음 구현 순서
1. 사용자 host access 승인 후 실제 상태 active를 확인한다. 기존 main의 대량 WIP를 보존하고 별도 작업 공간을 구성한다.
2. pure policy 초안과 테스트를 검토·반영하고 private store/owner-only 발급/철회를 구현한다.
3. 기존 one-use 경로를 보존하면서 commit에만 추가 grant 경로를 연결한다. push 등의 경로에는 연결하지 않는다.
4. 정상 연속 commit, 만료, 철회, 다른 repo/branch, runtime 변경, 외부 HEAD 이동, 소스 변경, stale 카드, replay, symlink, 실패 후 index 보존을 회귀 검증한다.
5. 기존 별도 승인형 immutable runtime rollout으로 설치하고 실제 채팅 UI/tools 등록을 검증한다.
6. 설치된 기능에서 사용자에게 실제 tossApp commit-only 24시간 승인 카드를 제공한다.

Git 커밋/Push, live 권한 레코드 변경, DB migration 및 서비스 재시작은 이번 체크포인트에서 수행하지 않았다.
