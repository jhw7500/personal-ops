# MCP Git 공개 검증 기록

2026-10-24, feature branch `feat/mcp-trusted-development-wsl`.

## 결과

별도 소스 압축파일 없이 Git clone으로 MCP와 WSL 설정 가이드를 받을 수 있다.
최종 기능 커밋은 `69abcd53ff976f1adddbc2922214094d777d8d48`, 검증된 소스 tree는
`eeb32102fd07292763884493515058f05b2e1ad5`다. 이 문서와 계획 완료 표시는 기능 검증 후의
문서 변경이다. main 병합, PR 생성, 운영 서비스 전환이나 새 권한 발급은 수행하지 않았다.

## 검증

- 최종 기능 소스의 깨끗한 Git clone, Python 3.11.14: `rollout_test_runner.py --discover tests
  --require-excluded-count 5` 결과 1,509 tests, 237.618초, OK (skipped=1).
  공식 Yocto fixture 5개 제외. 실제 BitBake/캐시/PIM/타겟 작업은 실행하지 않았다.
- Node UI 테스트 89/89 통과. 최종 수정은 JavaScript를 변경하지 않았다.
- 최종 수정의 focused 테스트 157/157, 별도 리뷰어 회귀 검사 8/8 통과.
- 변경 Python 파일 Ruff F/E9, diff check 통과. 16개 커밋 gitleaks 검사에서 노출 발견 없음.
- Pyright의 host_access.py, git_workflows.py, bootstrap.py, workspace_control.py 검사에는
  92 errors가 남아 있다. 동일 명령으로 최종 수정 전후 clone을 비교한 결과 파일명·심각도·규칙·
  메시지의 다중집합이 같았으며 추가/제거 0건이었다. 전체 타입 검사가 통과했다는 의미는 아니다.
- 실제 설치된 ABI 3 runtime/controller를 통한 별도 프로세스·프로젝트 간 권한 재사용,
  호환 릴리스/정책 갱신 후 권한 유지와 삭제·변조 릴리스 거부를 임시 환경에서 검증했다.
- 새 bootstrap은 기본 읽기 전용, 명시적 apply, 동일 설정 재실행, 초기 grant 없음,
  설치된 launcher의 실제 server --help와 격리 import/argv/venv 동작을 검증했다.
- GitHub HTTPS에서 credential helper 없이 새 clone 성공, HEAD와 원격 feature ref 일치,
  bootstrap --help 성공, 포함된 검증기 blob
  `4c07bbd2d36da0e626e8ac8d82ef8336f0272f5f` 일치.

## 최종 리뷰 반영

프로젝트가 다른 대기 승인 요청의 잘못된 표시, 원래 서버의 Git 검증기/skill 경로 의존성,
WSL 컴파일러·static libc·systemd 사용자 관리자 안내 누락, 잘못된 프로젝트 경로의 부분 설치를
수정했다. 별도 재검토에서 모두 해결로 확인됐으며 새 Critical/Important 지적은 없었다.
검증기 정본은 기존 pin을 유지한 채 runtime에 포함했다. 내부 실행 보고서는 최종 Git tree에서
제외했다. 과거 커밋에서의 보고서를 삭제하는 history purge는 수행하지 않았다.

약 99,000줄의 가져온 기준 소스는 위험도가 높은 경로 중심으로 리뷰했다. 모든 기존 기능을 새로
줄 단위 감사한 것은 아니다. 초기 UI RED 출력 일부는 보존되지 않았으며 재구성하지 않았다.
수정 회차의 RED/GREEN 증거는 실제 실행 결과다.

## 아직 실행하지 않은 환경 검증

실제 새 Windows/WSL 장치, native 드라이버, 재부팅, live ChatGPT 승인/검색과 터널,
GitHub 인증·선택적 Codex/PR workflow 전체, 실제 pim-check/타겟 QA는 실행하지 않았다.
WSL 설정과 실행 전 점검은 `chatgpt-mcp/WSL-SETUP.md`를 따른다.

## 커밋 메시지 정리

공개 전에 네 메시지의 형식만 고쳤다. 전체 16개 커밋은 순서별 tree와 author 이름/이메일/일시가
동일했고 최종 tree도 같았다. 16개 최종 메시지가 고정 CEC v1과 저장소 CI 버전 commitlint를
통과했다. 기존 main은 `abe3bf01967efb61c4e4141aadfab5afeda70cc3` 그대로다.
문서에 남은 이전 검증 SHA와 공개 SHA의 대응은 아래와 같다.

| Validation SHA | Published SHA |
| --- | --- |
| `e80fb05ce19f1f13d9dac2f95db62990ea5ccec5` | `a9f5f909d01730f451f21d157b1fb2c10d94dbec` |
| `f9c466531ddfe21da55ae7a55a9a431bd6cbe2b2` | `9fe4d650c046890a3d8b6ee033777242a2dfbdb3` |
| `53b9b0d1edb97e234b4581042db105ed5f5f94d5` | `4ed74e9bb7cb7cf8e14fc0357874ffaf22f73c74` |
| `5e40a2ce4748d7802a6f5674c1d545e34b0e0c6e` | `dd91a537ba42facabdda58c0cdf4a1655a98dfb1` |
| `9645941b0fb1933a56066690ba06977e9e3ef010` | `a24212da972fd68530c6d1cf323c85a3cfa637c0` |
| `142e3d19d3348ea535e784ca343b7828eb1b99e2` | `0c4e552bc7fb1ad33ea9bff03c62f4a2a4cbf6df` |
| `012e52c6a305802faaa41926b61aa18cca325007` | `d764501cf9b77d1dcf917d3f37308e18c1d298f6` |
| `4e8742eb400106d3c43746d84768b1e1bfdf0a30` | `c88774d7665347475c3680cb29755ef01772c1d2` |
| `0196e75ef602918af56d38cdcc479eacae57e895` | `0d5550a72497f03d732483155021c7975a37aea2` |
| `8daa3d0c6c7c0cb40357742ff17f1568abff2687` | `e2448b2ed5402f1afddac875cbdc8246d4c433ca` |
| `e37848a0a5b81bea929594659d641e80575f05c7` | `e770a52a40fbdfa14c99d40d458a347b16bad182` |
| `6a4d989b1dca30145a46a837be6821d9a8b1e8b9` | `e85e4d8e929c61b9e5a270faf409c30c4ad02638` |
| `a7cc4b418cfd24e083acbdeda233c53b22d2ef40` | `e4b2b4173a4de23a948e551f33d1c1b2ca6421f0` |
| `cd74735b3d39f93ce365924605ab984ac3c6e4a6` | `2bafd02b5bef7f558c543f2666250d28e92585ba` |
| `de1d498ac0729c51f1cdea72acc081a7c2724526` | `c256485b93335688ae6def9437c03d079d30efc9` |
| `0f3e7df75b40a880b15b2c8480ccee21747849de` | `69abcd53ff976f1adddbc2922214094d777d8d48` |
