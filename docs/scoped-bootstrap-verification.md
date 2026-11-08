# Scoped bootstrap 준비와 별도 검증 기록

이 경로는 검토 자료와 관측 기록을 만든다. 준비 결과나 서버 관측만으로 owner baseline 검증, 호스트 명령 등록, ChatGPT 스키마 적용, 설치·서비스 전환 승인이 완료되지는 않는다.

## 소스 준비

`chatgpt-mcp/prepare-scoped-bootstrap.py`는 승인된 ZIP을 메모리에서 읽고 SHA256과 내부 manifest를 검사한다. 패키지 Python을 실행·import하거나 압축을 풀지 않는다. 고정된 생성기의 AST 상수만 읽어 검토용 patch를 만든다. adapter는 승인 패키지를 바탕으로 두 가지 정확한 수정을 적용한다. 정책 경로 조회 오류가 기본 소스로 돌아가지 않게 하고, 깊은 JSON의 `RecursionError`를 명시적인 정책 오류로 처리한다. 예상 코드 anchor가 다르면 실패한다.

테스트는 생성기 소스의 `tests/test_runtime_rollout_scoped_source.py`를 데이터로 읽어 사용한다. 해당 파일은 생성기에 고정한 SHA256과 일치해야 하며 group/other 쓰기가 허용되면 거부한다. 패키지의 독립 테스트가 요구하는 `owner_scoped_delivery`·`tests.test_scoped_delivery`를 별도로 설치하지 않아도 검증할 수 있게 하기 위해서다.

```bash
python -B chatgpt-mcp/prepare-scoped-bootstrap.py prepare \
  --package /absolute/path/personal_ops_runtime_rollout_scoped_integration_v1_reissued_20261108.zip \
  --source-root /absolute/path/reviewed-clean-personal-ops \
  --output /absolute/path/new-private-review-directory
```

승인 ZIP SHA256은 `7da79b4192df1ab16f5f7e35cc27c01d6a4ed0511be9ed02541a9b3cb2bebe4e`로 고정되어 있다. CLI에는 hash 변경·적용·설치·등록 옵션이 없다. 실행에는 프로젝트 Python 의존성이 필요하다.

소스는 저장소 최상위의 clean Git 작업 트리여야 한다. ignored 파일도 거부하므로 개발 환경의 `.venv`·캐시가 있는 작업 트리 대신 검토할 커밋의 별도 clean 작업 트리를 사용한다. 새 출력 디렉터리는 소스 밖의 기존 owner 소유 디렉터리 아래에 만든다. 출력 부모와 조상에는 group/other 쓰기를 허용하지 않는다. `/tmp` 같은 root 소유 sticky 조상만 예외다. 기존 디렉터리 권한을 바꾸지 않으며 부적합한 출력 경로를 거부한다. 검사한 부모의 디스크립터를 유지하고 쓰기 전후 경로가 같은 디렉터리인지 확인한다. 기존 출력은 덮어쓰지 않는다.

생성기와 고정 테스트 원본도 다른 사용자가 쓸 수 없는 개인용 소스 checkout에서 실행한다. 새 검증용 clone은 프로세스 umask `077`로 만들 수 있다. 도구는 기존 소스 파일의 권한을 자동 수정하지 않는다.

출력은 `0700` 디렉터리 안의 `0600` 파일 두 개다.

- `bootstrap.patch`: 기존 세 파일의 adapter 연결과 신규 adapter·회귀 테스트 두 파일을 담은 검토 자료.
- `preparation.json`: `PREPARED_NOT_INSTALLED`, `baseline_eligible=false`, 소스 commit/catalog, 소스의 runtime/control digest, 패키지 입력 hash, 준비된 파일 hash와 patch hash. `bootstrap_test`에는 저장소 테스트의 고정 출처·hash와 교체한 패키지 테스트의 hash를 구분한다. `bootstrap_adapter`에는 패키지 원본 hash와 두 가지 보완을 적용한 결과 hash를 구분한다.

`source_runtime_snapshot`과 `source_control_sha256`은 변경 전 소스의 값이다. patch 적용 후 설치할 runtime의 검증 결과가 아니다. 준비 자료를 독립 검토한 다음 실제 bootstrap 경로에서 소스를 다시 검증해야 한다. 이 도구는 소스 적합성을 owner trust로 등록하지 않는다.

패키지에 포함된 `inspect_host_command_blockers` inventory 변경은 clean baseline에 없는 도구를 요구한다. 해당 변경과 이전에 거부된 네 파일 후보는 준비 patch에서 제외한다. 후보 전달은 bootstrap 이후 별도 승인 경로를 따른다.

통합된 소스에서 `RuntimeRolloutWeb`는 owner 정책이 없으면 기존 프로젝트 루트를 사용한다. 정책이 있으면 소유자·권한·baseline·candidate·attestation을 검사한 선택기만 사용하고, 준비/승인/claim마다 source mode와 정책 hash를 재검증한다. 채팅 인자로 임의 소스 경로를 선택하는 기능은 없다. 이 연결을 소스에 추가해도 실행 중인 서비스나 owner 정책은 갱신되지 않는다.

준비 실패는 종료 코드 `3`과 `FAILED_CLOSED: <code>`로 표시한다. 출력 쓰기 중 실패하면 일부 파일이 남을 수 있다. 성공 결과를 출력하지 않으며 기존 경로를 지우거나 다시 사용하지 않는다.

## 설치 후 관측과 검증

설치 파일 목록에는 `runtime_verification.py`와 `verify-runtime-rollout.py`가 포함된다. 소스 수정만으로 실행 중인 MCP가 갱신되지는 않는다. 설치된 불변 릴리스는 직접 수정하지 않는다.

새 코드로 별도 승인된 설치를 마친 서버는 manifest와 실제 프로세스가 일치할 때만 외부 `tools/list`에서 생성한 도구 이름·input/output schema의 canonical digest를 기록한다. 내부 SDK cache fill과 실패한 catalog 생성은 성공 증거를 만들지 않는다. 관측 저장 실패는 정상 도구 조회를 실패시키지 않는다.

별도 검증 CLI는 임의 `--verified` 값이나 외부 증거 경로를 받지 않는다.

```bash
python -B /absolute/path/verified-installed-release/verify-runtime-rollout.py \
  --operation-id <existing-verification-required-operation-id>
```

CLI는 기존 operation과 설치 manifest·실제 프로세스·세션 관측을 대조하고 새 verify receipt를 쓴다. `server_process_manifest_verified`는 해당 서버 프로세스의 설치 바이트만 확인한다. 별도 stdio 프로세스도 이 관측을 만들 수 있으므로 활성 서비스·터널 검증으로 쓰지 않는다. 기존 `runtime_verified`와 `tools_registered`는 false다. 기존 실행 결과를 성공으로 바꾸지 않는다. 검증 실패는 종료 코드 `1`, 관측이 맞아도 connector의 스키마 채택 증거가 없으면 종료 코드 `2`와 `verification_required`를 반환한다.

현재 이력 형식에는 plan에서 원본 apply 영수증을 유일하게 찾을 경로·hash·operation 연결이 없다. 새 기록은 원본 terminal plan SHA에 연결하며 과거 apply 영수증까지 연결했다고 주장하지 않는다. 이 한계는 과거 이력 조사나 임의 파일 선택으로 메우지 않는다.

`server_catalog_observed=true`는 서버가 외부 요청의 catalog 생성을 완료했다는 뜻이다. 응답 수신·ChatGPT cache 반영 증거가 아니므로 `connector_metadata_verified`, `connector_schemas_verified`, `baseline_eligible`은 계속 false다. 이 관측으로 기존 rollout의 ready 조건을 완화하지 않는다.

## 검증 범위

fixture 테스트는 ZIP·소스 drift·경로/권한·세션/프로세스 결합·원본 기록 보존을 검사한다. 별도 승인된 실제 owner bootstrap/등록, 서비스 전환, 웹 connector 채택과 board E2E는 이 테스트의 완료 주장에 포함하지 않는다.
