# Serena Python LSP / MCP 라우터 회귀 — PATH 보정으로 해결

검증 시각: 2026-10-17 KST
프로젝트: personal-ops / tossApp 콘솔 조회 MCP 연계
상태: **Serena 기존 라우터 회귀 실패 해결, 관련 SDK 52/52 PASS**. 운영 런타임 배포/OAuth/Toss API 변경 없음.

## 증상 및 초기 증거

- 기존 호스트 SDK 회귀 `tests.test_router.MultiProjectRouterTest.test_routes_each_call_and_contains_paths` 실패. Fixture 안의 `alpha/edit.txt`는 `alpha version 1` 그대로, 기대값은 `alpha version 2`.
- Serena backend `replace_content`가 `LanguageServerManagerInitialisationError`, `LanguageServerTerminatedException: Language server stdout read process terminated unexpectedly`를 기록. 이전 혼합 회귀 op `2329f8453dd803a23c5c02215f6683ba` (52개 중 1 error), 단독 op `737ee6510882c897000c3209817c9ae5`도 같은 오류.
- 백엔드 `chatgpt-mcp/serena_backend.py`는 `project.yml`에 `languages: [python]`을 설정. Serena 공식 Python(Pyright) LSP는 `uv`/`uvx` 런처를 필요로 할 수 있음: https://oraios.github.io/serena/02-usage/050_configuration.html.
- 실패했던 일반 호스트 명령은 SDK 자체가 아니라 하위 LSP 실행 환경 문제이며 `replace_content`가 도구 결과 내용에서 예외 메시지를 반환했어도 결과의 `isError`가 false처럼 보여 테스트가 파일 내용을 비교하며 실패하는 보조 증상도 있음.

## 실제 호스트 원인 점검

호스트 명령 op `cae2f70dedd356829f49c7f405ce82a3`: 읽기 전용 메타데이터 감사, exit0.
- 해당 호스트 실행 환경 `shutil.which("uv")` → 없음, `shutil.which("uvx")` → 없음.
- `$HOME/.local/bin/uv`와 `$HOME/.local/bin/uvx`는 설치돼 있고 실행 권한 정상.
- Node.js는 PATH에서 발견, `$HOME/.nvm/versions/node/v24.12.0/bin/node`도 존재·실행 가능.
- Serena venv 안에 `pyright-langserver` 실행 파일도 존재.
- 이 상황에서 PATH에 uvx를 추가하지 않으면 Pyright LSP startup이 실패한 기록과 일치. 실제 패키지 의존성 신규 설치는 필요 없었음.

## 임시 테스트 환경 보정 — 소스/서비스 무변경

테스트 서브프로세스에만 아래 환경을 전달:

```sh
env \
  PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v24.12.0/bin:/usr/local/bin:/usr/bin:/bin" \
  UV_OFFLINE=1 \
  "$HOME/.local/share/uv/tools/serena-agent/bin/python" \
  -m unittest -v tests.test_router
```

- `UV_OFFLINE=1`: 외부 패키지 다운로드 금지.
- 실제 SDK venv를 Python으로 실행하고 `uv`/`uvx` 검색만 보정. 다른 프로젝트 코드·환경·기존 시스템 설정은 바꾸지 않음.
- op `c9124ee328ebc8fa4e607af243f52d9c`: **Ran 1 test in 49.398s, OK**, exit0, termination_verified=true. `replace_content` 성공까지 확인.
- 동일 PATH/UV_OFFLINE으로 6개 모듈 `tests.test_toss_console_evidence`, `tests.test_toss_console_tools`, `tests.test_runtime_install`, `tests.test_rollout_host_runtime`, `tests.test_host_runtime_router`, `tests.test_router` 재실행.
- op `dedea66e2d1085107ef185a9470c0481`: **Ran 52 tests in 53.182s, OK**, exit0, termination_verified=true.
- 실패한 이전 테스트 두 차례와 달리 이번에는 오류 0. **공식 전체 롤아웃 프리플라이트와 다수 다른 WIP 테스트 전체를 실행한 결과는 아님.**

## 후속 계획

1. 운영 코드/Serena 기본 언어 서버를 `python_jedi` 등으로 강제 교체하지 말 것. 이번 오류는 PATH 보정 + offline test만으로 해결됐고, 실제 실행 환경의 PATH가 달라 생긴 오류임.
2. 공식 runtime rollout preflight를 수행할 때는 이미 있는 `uvx`를 실행기 PATH에서 찾을 수 있는지 우선 확인하고, 비권한 상승용 오프라인 테스트 환경을 사용. 실제 터널/관리 웹의 PATH는 별도 관측 없이 정상/비정상으로 단정하지 말 것.
3. 현재 Toss console `get_toss_test_console_status` 도구는 source-only/ `provider=None`, 반환 `CONSOLE_OAUTH_NOT_CONFIGURED`. 읽기 전용 공식 OAuth 사용자인증 및 실제 `bundle_list`/ `miniapp_get_status` 툴 스키마를 확인하지 않았음.
4. `personal-ops` 전체 WIP에 대한 공식 전체 프리플라이트, 소스 정합성 리뷰, 별도 사용자 승인 후 immutable runtime rollout. 이전 `71001541a10dc142305f3001f04cbd2f` 계획은 기간 만료되어 재승인이 아니라 새 계획이 필요.
5. 정확한 Toss TEST 대상 deployment ID `01a13e37-27f3-7f9c-923d-6dbd690a5a09`의 공식 provider `CREATED/isTested`와 실기기 WebView QA는 계속 미검증.

## 변경/미변경 경계

- 본 점검은 호스트 실행기의 임시 환경 PATH만 변경. 소스·운영 설정·Docker·API·Worker·PostgreSQL·AIT 번들·기존 OAuth 설정 변경 없음.
- Codex 호출, OAuth 로그인/토큰 접근, 서버 재시작, 런타임 설치·전환, Git commit/push/reset/stash 모두 없음.
- 작업 후 `get_runtime_status(tossApp)`: API healthy/toss-demo, Worker와 DB running, Schema014 ready/checksum_match, users=3/persons=17/readings=21.
