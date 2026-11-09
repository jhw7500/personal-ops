# WSL 중심 personal-ops 소스 전달

수정은 WSL을 주 편집 환경으로 삼고 Git 커밋으로 전달한다. 빌드서버와 WSL의
작업트리, 의존성, 설치 런타임, 설정, 키, workspace authority와 상태는 각각 유지한다.
공유 폴더와 심볼릭 링크로 두 호스트의 소스 작업트리를 연결하지 않는다.

새 코딩 세션과 배포·검증 세션의 기본 소스 경로는 다음과 같다.

| 용도 | 호스트 | 기본 소스 경로 | 웹 MCP 연결 |
|---|---|---|---|
| 공통 소스 편집 | WSL PC | `/home/jhw/projects/.worktrees/personal-ops-common-source-wsl-20261109` | `personal-ops-coding-wsl-pc1` |
| 빌드서버 배포·검증 | 빌드서버 | `/home/jhw/ai/opencode/projects/.worktrees/personal-ops-buildserver-source-20261109` | `personal-ops-coding` |

이 경로들은 새 세션의 기본값이며 현재 웹 MCP 프로젝트 등록을 자동으로 변경하지 않는다.
세션을 시작할 때 해당 연결에서 프로젝트 목록과 등록된 실제 경로를 조회하고, 기본 소스
경로와 일치하는지 확인한다. 일치하지 않으면 다른 경로로 자동 전환하거나 기존 작업트리를
정리하지 않는다. WSL의 `/home/jhw/projects/personal-ops`와 빌드서버의
`/home/jhw/ai/opencode/projects/personal-ops`는 기존 미커밋 작업을 보존하며 새 세션의
기본 소스로 사용하지 않는다.

```text
WSL development commit
          |
          v
      Git delivery
       /       \
      v         v
WSL source   buildserver source
wsl-pc1      + selector-only commit: legacy
```

## 소스와 배포 대상

공통 개발 소스의 `chatgpt-mcp/rollout-target.json`은 `wsl-pc1`을 명시한다.
빌드서버 배포용 브랜치는 검토한 공통 소스 커밋에서 파생하고 이 파일의
`layout`만 `legacy`로 바꾼 별도 커밋을 둔다. 해당 커밋에는 다른 변경을 섞지 않는다.
새 공통 소스를 전달할 때도 공통 소스 커밋과 호스트 선택 커밋을 구분해 기록한다.

호스트별 선택 파일도 Git으로 추적한다. 양쪽 경로를 동시에 허용하거나 선택 파일을
Git에서 제외하지 않는다. 계획 준비 시 선택한 소스와 대상은 승인·실행 때 다시 검증된다.
이 선택 파일은 설치 런타임 목록에 포함되지 않으므로 두 호스트의 런타임 해시가
같을 수도 있다. 해시가 같다는 사실만으로 다른 호스트의 배포를 허용하지 않는다.

## 작업을 넘길 때

1. 실제 호스트, 프로젝트 경로, 브랜치, HEAD와 미커밋을 먼저 확인한다.
2. 미커밋이 있는 원본은 보존한다. 필요하면 별도 작업트리에서 통합하고 검증한다.
3. 검토한 커밋을 원격 Git 브랜치 또는 검증한 Git bundle로 전달한다.
   source patch, ZIP, 설정·인증·상태 디렉터리는 공유 인계 폴더에 올리지 않는다.
4. 받는 쪽의 비어 있는 작업트리에 해당 커밋을 반영한다. 기존 미커밋을
   reset·restore·checkout으로 지우거나 다른 세션의 브랜치를 강제로 옮기지 않는다.
5. `project-handoffs/personal-ops/`에 전달 커밋, 원격 반영 여부, 양쪽 작업 경로,
   남은 미커밋과 실제 검증 결과를 기록한다.

Git 전달은 소스 동기화다. 런타임 설치·서비스 전환과 웹 연결 검증은 해당 호스트에서
따로 수행하고 각각의 결과를 기록한다. 인계 문서나 다른 호스트의 성공이 운영 권한을
부여하지 않는다. 기존에 거부된 변경은 다른 호스트나 실행 경로로 재시도하지 않는다.

## 웹 작업 경로

빌드서버의 `personal-ops-coding`과 WSL의 `personal-ops-coding-wsl-pc1`은
서로 다른 연결이다. MCP가 등록한 실제 프로젝트 경로와 편집할 작업트리가 일치하는지
확인한다. 별도 통합 작업트리를 만들었다고 기존 웹 연결의 프로젝트 경로가 바뀌지는 않는다.
프로젝트 등록 경로나 실행 릴리스 변경이 필요하면 해당 호스트의 공식 반영 절차를 따른다.

웹에서 공통 소스를 편집할 때는 `personal-ops-coding-wsl-pc1`에서
`/home/jhw/projects/.worktrees/personal-ops-common-source-wsl-20261109`가 실제 프로젝트
경로로 등록됐는지 먼저 확인한다. 빌드서버 배포·검증 작업은 `personal-ops-coding`에서
`/home/jhw/ai/opencode/projects/.worktrees/personal-ops-buildserver-source-20261109`의
등록 여부를 별도로 확인한다. 한 연결의 확인 결과를 다른 연결에 적용하지 않는다.

소스·Git 작업 승인은 런타임, DB, 권한 변경이나 Yocto/BitBake 실행 승인을 대신하지 않는다.
