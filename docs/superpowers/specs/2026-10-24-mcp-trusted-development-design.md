# MCP 개발 작업의 승인·등록 단순화 설계

작성일: 2026-10-24. 상태: 소스 구현 및 격리 검증 완료, 실제 WSL/ChatGPT 적용 전. 이 문서는 운영 권한 발급·서비스 전환의 증거가 아니다.

현재 feature branch에는 설치 단위 v2 권한, ABI 3 admission, policy v2 text 산출물,
clone-first bootstrap이 구현되어 있다. bootstrap의 기본 동작은 read-only 계획이며
`--apply`만 새 owner-local 설정·runtime·controller·authority를 만들고 설치 범위를
활성화한다. 실제 Windows/WSL PC, native driver, live tunnel/ChatGPT, PIM/보드 QA는
아직 실행하지 않았다. 아래의 “확인된 구현과 원인 후보”는 설계 당시 역사적 관찰로 보존한다.

## 목적과 현재 합의

사용자는 웹채팅에서 일반적인 개발·파일 작업·QA를 이어가면서 권한 승인, 계획 승인, 작업별 프로파일/함수 등록, MCP 새로고침을 최소화하기를 원한다. Windows PC의 WSL에는 별도 personal-ops 인스턴스를 준비하고, pim-check의 실제 개발과 타겟 테스트는 웹에서 진행한다. 이 호스트에서는 환경 준비와 이관 자료를 만든다.

사용자가 확정한 것은 위 목표와 준비 진행이다. **장기 권한의 기본안을 `until_revoked`로 잡은 것은 작성자의 제안**이다. 기존 30일 권한이나 만료된 권한을 자동으로 영구 권한으로 바꾸는 승인은 받지 않았다. 실제 새 권한은 범위가 표시된 사용자 승인 카드 또는 기존 소유자 관리 화면에서 발급한다.

성공 기준은 일반 개발 작업 10개를 연속 요청해도 personal-ops 접근 승인은 최초 1회, 작업별 프로파일 등록은 0회, 작업별 MCP 새로고침은 0회가 되는 것이다. ChatGPT 자체의 확인은 별도의 제품 동작으로 측정한다.

## 확인된 구현과 원인 후보

| 확인된 사실 | 근거 | 영향 |
| --- | --- | --- |
| `start_host_execution`은 실행 파일·인자·cwd를 받고 활성 grant를 재사용한다 | `chatgpt-mcp/host_execution_tools.py`, `host_execution.py` | 일반 명령마다 새 프로파일이 필요한 것은 아니다 |
| 호스트 grant 기본·최대 30일, 요청 카드 7일 | `chatgpt-mcp/host_access.py` | 무기한 유지 기능은 아직 없다 |
| 응답 scope는 installation이지만 저장 레코드는 project별이다 | `HostAccess._record_path()` | 프로젝트 전환 때 별도 승인이 필요할 수 있다 |
| grant는 boot ID, UID, 프로젝트 경로, 런타임 identity에 묶인다 | `HostAccess._binding_error()` | 재부팅·많은 런타임 변경 때 재승인이 발생한다 |
| 런타임 identity에는 Workspace 정책 digest/epoch도 포함된다 | `HostAccess._runtime_identity()` | 정책 변경과 실행 권한 갱신이 결합돼 있다 |
| 범용 host 경로 안내 뒤에 모든 명령을 isolated로 일반화한 안내가 있다 | `chatgpt-mcp/server.py`의 Server instructions | 모델이 프로파일/격리 경로를 잘못 고를 수 있다 |
| 등록형 host command는 고정 profile과 별도 계획 승인으로 동작한다 | `host_command_profiles.py`, `host_command_ui.py` | 이 경로를 일반 개발에 사용하면 반복 절차가 생긴다 |
| refresh는 도구 목록 재조회 통지와 관찰이다 | `catalog_refresh.py` | 일반 명령 인자·스크립트 내용 변경에는 필요 없다 |

2026-10-24 실제 연결의 읽기 전용 조회에서는 personal-ops가 `EXPIRED`, pim-check가 `NO_GRANT`였다. 이는 조회 시점의 상태이며 이후 상태를 보장하지 않는다. 반복 승인 전체의 원인을 입증하는 웹 대화 추적은 아직 수집하지 않았다. 안내 충돌은 확인된 코드 사실이고, 잘못된 도구 선택의 원인이라는 판단은 가설이다.

## 접근 방식

| 방식 | 장점 | 한계 | 선택 |
| --- | --- | --- | --- |
| 기존 30일 grant와 범용 도구의 안내만 정리 | 작은 변경으로 프로파일 생성과 중복 실행 승인을 줄인다 | 재부팅·프로젝트 전환·권한 만료의 마찰이 남는다 | 먼저 독립적으로 적용 가능한 단계 |
| 설치 단위의 철회 가능한 장기 신뢰 권한 | 같은 PC/계정에서 일반 작업을 계속할 수 있다 | 권한 생명주기와 UI·작업 상태의 버전 변경이 필요하다 | 목표 설계, 실제 발급은 소유자 선택 |

기존 범용 실행·파일·상태·로그 도구를 재사용한다. 별도의 작업마다 MCP 함수, wrapper 서비스, 명령 프로파일을 새로 만들지 않는다. 고정 배포·DB 작업과 격리 실행은 그 목적에 맞는 기존 경로를 유지한다.

## 사용 흐름

```text
User request
    |
    v
Existing project context -> Read access status
                                |
                   +------------+-------------+
                   |                          |
                 Active                  No valid grant
                   |                          |
                   |                    One owner card
                   |                          |
                   +------------+-------------+
                                |
                         Start ordinary job
                                |
                         Status / logs / result
                                |
                         Next ordinary request
```

모델은 일반 개발 요청에 대해 현재 프로젝트를 재사용하고 `get_host_access`를 확인한다. 활성 grant이면 `start_host_execution`을 기본 실행 경로로 사용한다. 도구 내부의 실행 데이터 동결/prepare는 사용자에게 다시 승인받는 단계로 표현하지 않는다. 이미 실행을 요청한 사용자에게 같은 명령의 실행 여부를 다시 묻지 않는다.

격리 실행을 사용자가 선택했거나 호스트 권한이 없는 경우에는 격리 경로를 사용할 수 있다. 네트워크·호스트 파일이 필요한 작업이 격리 환경에서 실패했다고 임의로 권한을 확대하지 않는다. 응답은 필요한 권한과 실제 다음 동작을 한 번만 안내한다.

## 권한 계약

### 범위와 실제 권한

장기 신뢰 권한은 **한 MCP 설치와 실행 OS 계정**에 대한 권한이다. 프로젝트 이름은 작업 위치와 결과 표시를 결정한다. 프로젝트 폴더를 OS 샌드박스로 표현하지 않는다. 동일 연결을 사용할 수 있는 클라이언트에 적용되며, 채팅별 사용자 인증/격리를 제공한다고 주장하지 않는다.

승인 화면에는 호스트 이름, 실행 계정, 일반 파일·네트워크 접근, 관련 그룹, 권한 유지 방식, Codex 위임 허용 여부를 표시한다. `docker`, `sudo` 등의 그룹이 실제 권한을 넓힐 수 있으므로 단순히 '프로젝트 안의 일반 사용자 권한'으로 축소해서 설명하지 않는다. 비밀번호 없는 sudo 가능 여부는 별도로 확인하지 않았다면 미확인으로 표시한다.

같은 UID의 임의 프로그램 실행을 허용하면서 사용자 홈의 권한 파일·자격증명을 완전히 숨길 수는 없다. 0600 권한과 저장소 밖 상태 디렉터리는 다른 사용자/잘못된 파일 경로에 대한 방어이며, 동일 계정의 실행 코드에 대한 OS 격리는 아니다. 강제 격리가 필요한 환경은 별도 실행 계정/VM과 별도 권한의 관리 서비스가 필요하다. 이 설계는 그런 격리를 새로 구현했다고 주장하지 않는다.

### 유지 방식

새 버전의 명시적 구분자는 `lifetime = timed | until_revoked`다.

- `timed`: `expires_at`은 UTC 시각이며 기존 유한 기간 옵션을 유지한다.
- `until_revoked`: `expires_at = null`을 사용한다. 임의의 먼 미래 날짜나 0초를 영구 권한의 대용으로 사용하지 않는다.
- legacy v1에서 null/누락된 만료 값은 계속 잘못된 상태다. v2 스키마와 lifetime이 일치하는 경우에만 null을 허용한다.
- 카드/요청 자체의 유효기간은 7일을 유지한다. 오래된 미승인 카드와 승인 완료된 장기 권한의 수명은 서로 다르다.
- 같은 범위의 활성 권한을 확인할 때는 새 카드·새 request를 만들지 않는다. 기존 카드가 있어도 이미 충족된 작업의 재승인을 요구하지 않는다.

### 설치 단위 저장과 생명주기

v2는 설치 단위의 단일 grant 레코드와 잠금을 사용한다. 공개 반환값의 `project`는 호출 문맥으로 유지하되 실제 scope와 저장 단위를 일치시킨다. 소유자가 등록한 프로젝트 간 이동은 같은 grant를 재사용한다. 프로젝트 등록은 여전히 소유자의 설정 행위이며, 신뢰 권한을 받은 모델이 자동으로 모든 디렉터리를 프로젝트에 등록하는 기능은 추가하지 않는다.

grant의 지속적인 바인딩은 설치 ID, 실행 계정 UID, 신뢰 정책 generation, 허용 capability 집합이다. 실행 요청의 boot/process 정보는 작업 추적에 사용하되 영속 grant 자체의 만료 조건으로 사용하지 않는다. Windows PC/WSL의 새 설치는 새로운 설치 ID와 새 키·상태를 만든다. 서버의 grant를 복사해서 재사용하지 않는다.

| 사건 | 장기 grant | 실행/확인 동작 |
| --- | --- | --- |
| 채팅/플러그인 새로고침, 새 대화 | 유지 | ChatGPT 자체 확인은 따로 발생할 수 있다 |
| 같은 설치·계정의 프로세스 재시작, 정상 WSL 재부팅 | 유지 | 이전 작업의 생존 여부는 PID가 아닌 기존 실행 identity로 다시 판정 |
| 소스 편집, 새 테스트·스크립트·리포트 | 유지 | 일반 작업 도구로 처리 |
| 기존 소유자 배포 절차로 허용된 호환 런타임 설치 | 유지 | 런타임 파일 무결성과 release admission은 계속 검증 |
| 설치 ID/UID 변경, 명시적 철회, 권한 계약 generation 변경 | 무효 | 신규/대기 실행 거부, 필요한 경우 새 카드 |
| 런타임 변조·상태 손상·검증 실패 | 실행 거부 | 자동 갱신 카드로 오류를 덮지 않고 복구 사유 표시 |
| 서버가 관리하는 capability 확대 또는 감지 대상 계정 바인딩 변경 | 재승인 필요 | 확장 내용을 보여주는 소유자 승인 |

현재 `_runtime_identity()`의 전체 코드/Workspace 정책 해시와 grant 수명을 그대로 결합하면 일반 업데이트 때마다 재승인이 반복된다. 구현에서는 **런타임 무결성·허용된 배포 검증**과 **사용자가 허용한 실행 권한 계약**을 분리한다. 코드가 자기 버전을 선언했다는 사실만으로 호환 배포가 승인되지는 않는다. 기존 소유자 rollout/Workspace activation 절차로 admission된 런타임만 실행 가능해야 한다.

신뢰 정책 generation은 권한 확대/권한 계약 변경에만 소유자 경로로 갱신한다. 파일 읽기용 Workspace 정책의 사소한 변경은 호스트 grant를 자동 폐기하지 않지만 해당 파일 도구의 새 정책은 매번 적용한다. 이전 정책에서 허용되던 파일을 새 정책이 막으면 그 파일 작업은 거부된다.

같은 UID의 sudoers, ACL, 자격증명, 외부 서비스 권한 변화 전체를 자동으로 감지한다고 보장하지 않는다. 서버가 관리하는 capability 외의 변화는 해당 OS 계정을 신뢰하는 범위에 속한다. 그룹 표시는 승인 시점과 현재 상태의 참고 정보이며 완전한 권한 증명은 아니다.

### 철회와 진행 중인 작업

철회가 저장된 뒤 신규/대기 작업은 시작할 수 없다. 진행 중 작업은 현재 grant watcher와 소유 실행 unit 취소 경로로 취소 요청을 전달한다. 상태는 취소 요청, 프로세스 종료 확인, 결과 미확인을 구분한다. 철회를 롤백이나 타겟 복구 성공으로 표현하지 않는다.

권한 모드/저장 버전의 전환은 활성 실행이 없을 때 수행한다. 실행 중 grant를 바꿔 기존 작업을 우발적으로 끊는 자동 마이그레이션은 하지 않는다. v1 grant는 기존 수명과 의미 그대로 읽고, v2 장기 권한 발급은 새 명시적 승인으로만 한다. 구버전 런타임으로 rollback했을 때 v2를 v1 영구 권한으로 오해해서는 안 된다.

v1/v2 전환 계약은 다음과 같다.

1. v2 최초 발급을 준비할 때 설치 단위의 전환 표식과 증가하는 grant-state revision을 영속화한다. 전환 표식이 존재하면 v2가 revoked/expired/invalid이거나 파일이 없어도 v1로 fallback하지 않는다.
2. 전환 시 기존 v1 grant를 철회하고 모든 v1 승인 요청·pending card를 무효화한다. 모든 새 카드 callback은 발급 시의 grant-state revision에 묶인다. 승인·철회·교체 후 남은 옛 callback은 새 grant를 만들 수 없다.
3. 전환·revision 변경·신규 실행 시작은 설치 단위 잠금/기존 admission 경계로 직렬화한다. 중간 실패는 '권한 없음/복구 필요'로 남기며 과거 권한을 활성화하는 복구는 하지 않는다.
4. v2 권한을 발급하는 설치는 기존 관리 launch/rollout admission에서 이 전환 계약을 이해하는 최소 runtime ABI를 강제한다. 현재 ABI 2에서 이 기능을 넣는 릴리스는 ABI 3으로 올리고 floor를 유지한다. Workspace 비활성 설치도 v2 초기 설정에서 이 admission 구성을 먼저 갖추어야 한다. 호환되지 않는 옛 런타임으로의 rollback은 실행 전에 거부한다.
5. v2 권한이 한 번도 발급되지 않은 설치만 v1 경로를 계속 사용할 수 있다. rollback/복구 절차는 전환 표식·철회 이력·revision을 보존한다.

이 전환 표식 역시 동일 UID의 임의 코드에 대한 변조 방지 장치로 설명하지 않는다. 관리되는 실행·배포 경로의 일관성과 오래된 상태의 우발적 재활성화를 막는 계약이다.

## 도구와 계획 승인

- 일반 명령: 기존 `start_host_execution`, `get_host_execution`, `read_host_execution_log`, `cancel_host_execution`을 사용한다. argv·입력 파일 변경 때문에 새 도구를 만들지 않는다.
- 파일 작업: 기존 파일 도구를 사용한다. 새 WSL의 기본 작업 영역은 해당 사용자의 `~/projects`로 작성한다. 일반 소스와 승인 범위 안의 새 리포트/JSON을 읽는 데 JSON 키별 등록을 요구하지 않는다. 기존 서버에서 명시적으로 보호한 산출물/필드 정책은 자동 삭제하지 않는다.
- 명시적으로 보호된 경로·산출물에서 거부된 작업을 범용 명령으로 재시도하는 fallback은 만들지 않는다. 더 넓은 읽기 범위가 필요하면 소유자 정책 변경의 구체적인 대상으로 제시한다.
- Codex: 사용자가 위임을 요청한 경우에만 실행한다. 장기 host 권한과 `allow_codex`를 재사용하고, 설치에 맞는 재사용 가능한 agent profile을 초기 설정한다. 목표/테스트마다 profile을 새로 만들지 않는다. `allow_codex`는 관리되는 위임 기능의 허용값이며 임의 호스트 프로그램의 외부 모델 호출까지 OS 차원에서 차단하는 장치로 설명하지 않는다.
- 고정 배포·DB·Git 작업: 기존 작업별 승인/리뷰 규칙을 따른다. 이미 유효한 승인이 있는 계획이면 새 승인 대신 해당 계획의 상태와 next action을 재사용한다.
- Yocto/BitBake 태스크·캐시 변경, 타겟 보드 점유·복구 규칙은 기존 사용자/프로젝트 지침을 따른다. 임의 Python/shell 실행을 허용한 상태에서 단순 명령 문자열 필터만으로 이 규칙이 강제된다고 주장하지 않는다.

승인 부재와 도구 미지원·실행 실패를 구분한다. 프로파일이 없는 일반 작업을 곧바로 '호스트에서 프로파일부터 만들어 달라'고 반환하지 않는다. 실제 미지원 기능을 범용 도구로 안전하게 표현할 수 있는지 먼저 판단한다. 별도 승인 계약이 있는 작업을 이 규칙으로 우회하지 않는다.

### 일반 QA 산출물의 디렉터리 단위 읽기

현재 `WorkspaceTools.ordinary_path()`는 Git-ignored 파일을 일반 파일 도구로 읽지 못하게 하며, 등록 산출물 읽기는 지정된 JSON 키만 투영한다. pim-check의 `reports/`, `hw-results/`, `events/`가 이 대상이다. host grant만 바꾸어서는 새 리포트를 자유롭게 읽을 수 없으므로 아래 정책 변경을 구현 범위에 포함한다.

- Workspace policy v2의 artifact 규칙에 `content_mode = json_fields | text`를 추가한다. 기존 v1 규칙은 `json_fields`의 기존 의미로 읽는다.
- `json_fields`는 기존 필드 제한을 그대로 적용한다. `text`는 소유자가 명시적으로 정한 디렉터리/파일 아래 UTF-8 내용을 읽으며 JSON 키 목록으로 투영하지 않는다. `text`의 `fields`는 빈 배열이어야 한다.
- 초기 WSL 정책은 pim-check의 위 세 결과 디렉터리를 읽기/목록 전용 `text` 후보로 표시한다. 실제 적용 때 한 번 범위를 확정하고, 이후 그 안의 새 파일/JSON 키는 추가 등록하지 않는다. 아직 생성되지 않은 결과 디렉터리는 프로젝트 루트에 대한 안전한 상대 경로로 등록하고 읽기 시 존재 여부를 확인한다.
- 기존 `read_registered_artifact`와 목록 도구를 재사용한다. 새 MCP 함수나 파일별 실행 profile은 만들지 않는다. 반환은 기존 페이지 응답과 cursor를 사용한다.
- 읽기 상한은 파일당 1 MiB, 페이지당 64 KiB를 유지한다. 더 큰 파일/바이너리는 지원 범위를 명확히 반환하며 일반 명령으로 우회 읽기하지 않는다. 대형 로그 스트리밍/이미지 첨부는 별도 범위다.
- 보호 경로·민감 파일명·심볼릭 링크/특수 파일 거부와 기존 민감 내용 검사를 유지한다. 내용 검사는 모든 비밀을 완벽히 판별하는 보장으로 표현하지 않는다.
- 기존 artifact namespace 중첩 거부를 유지한다. 제한된 기존 namespace를 넓은 `text` 규칙으로 덮지 않는다. 현 서버의 과거 실험 결과 필드 정책을 이관/모드 전환으로 자동 완화하지 않는다.

필요한 파일은 `workspace_policy.py`, `workspace_tools.py`, `workspace_files.py`, 정책 버전을 소비하는 activation/rollout 코드와 관련 검사다. 정책 v2도 위 runtime ABI 3의 admission과 함께 배포한다.

## 새로고침과 재시도

새 테스트, 함수, 스크립트, 명령 인자, 결과 파일은 MCP 도구 계약이 바뀌지 않으므로 refresh하지 않는다. 최초 플러그인 설치와 도구 이름/입력 스키마/서버 안내 변경 시에만 refresh가 필요하다. 현재 project enum 기반 카탈로그에서는 새 프로젝트 등록이 스키마 변경을 만들 수 있다. 첫 릴리스는 기존 구조를 보존하며, 프로젝트 자동 등록/동적 schema 재설계는 이 범위에 넣지 않는다.

`refresh_mcp_catalog` 성공은 통지/서버 측 재조회 관찰을 의미한다. ChatGPT 모델 캐시나 승인 기억이 갱신됐다고 보고하지 않는다. 쓰기 도구를 read-only로 잘못 표시해 ChatGPT 확인을 피하는 변경은 하지 않는다.

전송 오류 후 같은 request ID는 같은 작업을 조회/재사용한다. grant 만료 표현을 nullable로 바꾸면서 기존 실행의 idempotency나 상태 로딩을 깨뜨려서는 안 된다. 작업 완료가 불확실한 경우 새 ID로 재실행하지 않는다.

## 구현 범위와 순서

1. `server.py`, `host_execution_tools.py`, 관련 README의 경로 안내 충돌을 정리한다. 실제 실행 의미/권한 확대 없이 독립 검증할 수 있는 첫 변경이다.
2. `host_access.py`에 v2 설치 grant와 수명·generation을 추가하고 v1 읽기 호환성을 유지한다. `host_access_ui.py`, 승인 HTML/관리 JS, `admin_server.py`에 같은 구분을 반영한다.
3. `host_execution.py`, `host_agent.py`, `agent_tasks.py` 등 만료를 문자열로 가정하는 소비자를 수정한다. 실행 동결·재시도·취소·구버전 상태 처리까지 검증한다.
4. 일반 QA 산출물의 `text` 읽기 모드와 정책 v1 호환성을 추가한다. Git-ignored 결과 파일, 새 JSON 키, 민감 경로, 기존 제한 namespace를 실제 파일 fixture로 검증한다.
5. 런타임 admission과 grant 생명주기를 분리하되 기존 `workspace_control.py`, activation/rollout의 소유자 검증을 유지한다. ABI 3 floor와 v2 전환 표식으로 오래된 권한/런타임의 재활성화를 막는다. 저장소 코드 편집만으로 런타임 권한이 활성화되지 않도록 한다.
6. 승인 UI와 실제 MCP 프로토콜로 반복 작업·재시작·철회 시나리오를 검증한다. 그 뒤 검토된 새 스냅샷을 WSL 이관본의 기준으로 갱신한다.

새 종속성 도입이나 전체 MCP 재작성은 필요하지 않다. 이번 단계의 산출물은 설계와 검증된 기존 소스 이관본이다. 구현·운영 적용이 끝났다고 표시하지 않는다.

## 필수 검증

| 검증 사례 | 기대 결과 |
| --- | --- |
| 활성 권한으로 서로 다른 일반 명령 10회 | MCP 접근 카드 0회 추가, profile 생성 0회, refresh 0회 |
| 등록 프로젝트 A에서 B로 이동 | v2 installation grant 재사용, project/cwd는 정확히 B |
| timed 만료 / until_revoked의 null 만료 / malformed legacy null | 각각 만료 거부 / 지속 허용 / 상태 오류 |
| 재시작·재부팅 후 동일 설치/계정 | grant 유지, 기존 작업을 새 PID만 보고 재실행하지 않음 |
| 허용된 호환 rollout / 변조된 runtime | grant 유지하며 실행 / 실행 거부 |
| UID·설치 ID·generation 변경 또는 revoke | 신규·대기 실행 거부 |
| 철회와 작업 시작 경합 | 철회 이후의 시작 차단, 실행 소유권·취소 상태 검증 |
| 동일 request ID의 transport retry | 중복 실행 0회, 기존 결과/진행 상태 반환 |
| 이미 승인된 고정 계획 | 계획의 기존 계약 안에서 실행하며 중복 카드 없음 |
| 보호된 결과/경로에 접근 거부 | host command fallback 없음 |
| 신규 일반 리포트 JSON에 필드 추가 | 별도 MCP 함수·필드 프로파일 등록 없이 승인 범위에서 조회 |
| 오래된 pending card 또는 재사용 callback | stale/소비된 카드 거부, 기존 active grant를 덮지 않음 |
| v1 활성 -> v2 발급 -> v2 철회/손상 -> v1 파일 잔존 | v1 fallback 없음, 실행 거부 |
| v2 전환 도중 실패 / ABI 2 runtime rollback | 과거 권한 비활성 유지 / admission 거부 |
| 승인·철회 뒤 이전 revision의 callback | 새 grant 발급 거부 |
| ignored 리포트 text / 기존 json_fields / 중첩 namespace | 전체 허용 내용 / 지정 필드만 / 정책 거부 |
| 보고서 크기 초과·바이너리·링크·민감 경로 | 명확한 제한 오류, 우회 읽기 없음 |
| WSL 새 설치 | 서버 토큰·grant·상태 없이 새 identity로 연결 |

관련 기존 검사는 `tests/test_host_access.py`, `test_host_execution.py`, `test_host_execution_tools.py`, `test_chat_host_access_approval.py`, `test_chat_host_access_router.py`, `test_host_access_ui.mjs`, `test_agent_tasks.py`, `test_workspace_authority.py`, `test_rollout_workspace_authority.py`에 있다. 사용자 시나리오를 검증하는 회귀 검사를 이 파일군에 추가한다. 설계 문서 작성 단계에서는 이 검사를 실행했다고 주장하지 않는다.

## Windows/WSL과 pim-check의 적용 범위

WSL2 Linux 홈의 `~/projects/personal-ops`와 `~/projects/pim-check`를 사용한다. 빌드서버와 PC는 별도 설치·터널·키·상태를 갖는다. Windows 네이티브 파일/드라이버 작업은 WSL 프로세스 실행·권한·취소 동작을 확인한 뒤 별도 helper 필요성을 판단한다. WSL의 sudo를 Windows 관리자 권한으로 취급하지 않는다.

pim-check의 실제 타겟 테스트는 이번 호스트 준비에 포함하지 않는다. 보드 점유 wrapper의 BUSY exit 4, 타겟 식별, 백업·복구 규칙을 웹 실행에서도 유지한다. 현재 범용 host job의 1800초 상한과 장기 테스트의 복구/취소 시간은 별도 검증 대상이다. 무기한 접근 권한은 무기한 실행 시간을 뜻하지 않는다.

## 공식 제품 동작과 남은 제한

ChatGPT의 도구 확인 기억과 personal-ops grant는 별개다. OpenAI 문서는 도구별 승인 선택을 대화 동안 기억할 수 있고, 새 대화/같은 대화 새로고침 후 다시 확인할 수 있다고 설명한다. 서버에서 플랫폼의 확인을 없앴다고 보장할 수 없다.

- [OpenAI custom MCP 연결·확인·refresh 안내](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [WSL에서 Codex 사용](https://learn.chatgpt.com/docs/windows/wsl)

독립 검토 결과는 `docs/reviews/2026-10-24-mcp-trusted-development-design-review.md`에 기록한다. 최종 이관본에는 구현된 기존 기능과 이 문서의 향후 변경안을 구분해서 포함한다.
