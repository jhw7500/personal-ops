# session-summary

Claude Code episodic-memory + opencode SQLite에서 전일 세션을 긁어와 주간 요약을 누적하는 스크립트.

## 동작

- **요약 모드** (인자 없음): 어제 1일치 세션 검색 → 요약을 `logs/session-summary.md`에 append
  - 빈 결과(작업 없음)도 헤더만 기록하여 날짜 누락 방지 (`_(작업 없음)_`)
  - opencode 통합: `INCLUDE_OPENCODE=1` (기본값). DB는 read-only로 직접 읽음 — opencode 바이너리/토큰 미사용. `0`으로 끄면 Claude Code 세션만 사용
  - 이전 `미완료 항목`은 stable ID와 로컬 Git patch 근거를 대조하고, 검증된 경우에만
    resolution commit을 표시
- **로테이트 모드** (`rotate` 인자): `logs/session-summary.md`를 `archive/summary-<지난수>_<이번화>.md`로 이동
  - 다음 요약은 최신 archive도 검증 입력으로 읽어 이전 주의 open item을 active summary와 state에 이월

## 미완료 항목 resolution 추적

`resolution-tracker.py`는 이전 미완료 항목을 `state/unresolved-items.json`에 저장하고 다음
실행에서 후속 commit의 실제 patch를 확인한다. 모델은 준비된 후보 중 의미상 해결 commit을
고르지만, 최종 출력은 item ID·repository·SHA가 manifest와 정확히 일치할 때만 허용된다.
커밋 제목만 비슷하거나 Git 검증이 실패한 경우에는 해결 처리하지 않는다.

표시 형식은 다음과 같다.

```text
- [ ] bps 배열 길이 불일치 — gstApp — 중
<!-- unresolved-id:unresolved-5a83e991f24b -->

- [ ] bps 배열 길이 불일치 — gstApp — 중 [검증 필요]
<!-- unresolved-id:unresolved-5a83e991f24b -->

- [x] bps 배열 길이 불일치 — gstApp [resolved by dc06098]
<!-- unresolved-id:unresolved-5a83e991f24b -->
```

HTML comment의 stable ID는 모델에 의미를 맡기지 않는 opaque 식별자이며 archive에도 남는다.
상태 파일이 summary append 뒤 교체되지 못하면 다음 실행이 이 marker와 Git 후보를 이용해
상태를 복구한다. 모델이 항목을 누락하거나 원문·숫자·단위·조수사·코드 심볼을 바꾸거나
등록되지 않은 SHA를 쓰면 원본 open 항목으로 되돌리고 `[검증 필요]`를 붙인다.
추적 도입 전의 marker 없는 항목과 이후 marker가 붙은 동일 항목이 함께 남아 있어도 stable ID로
한 번만 복구한다.

repository 후보는 당일 Claude Code/opencode context에 실제로 등장한 로컬 절대경로만
사용한다. `git rev-parse --show-toplevel`과 정확히 일치하고 project명이 repository basename과
유일하게 일치해야 한다. workspace 전체, 부모 디렉토리, remote repository는 검색하지 않는다.
후보 commit은 제목이 아니라 추가·삭제된 patch line의 item-specific token으로 선별한다.
따라서 제목이 일반적인 `chore`였던 `dc06098` 유형도 `bps` 같은 실제 변경 심볼로 찾을 수 있다.

실패는 모두 work item에 대해 fail-open, resolution에 대해 fail-closed다.

- repository 없음/중복, baseline divergence, Git timeout·오류·출력 초과: unresolved 유지
- manifest/state/model 출력 손상: unsupported completion 게시 금지
- prepare 실패: 모델을 호출하지 않고 기존 summary/state 유지
- reconcile 실패: 모델 출력은 버리고 명시적인 `other` 실패 summary만 기록

한 실행의 상한은 open item 100개, item당 후속 commit 200개, 후보 5개, item context 4 KiB,
전체 resolution context 32 KiB다. resolved record는 중복 방지를 위해 최근 200개까지 상태에
보관하되 이후 open prompt에는 넣지 않는다. 이 context도 기존 전체 prompt 65,536-byte 상한에
포함되고 Claude 호출 횟수는 여전히 최대 1회다.

진단 시 `SESSION_SUMMARY_RESOLUTION_TRACKING=0`으로 tracker만 끌 수 있다. 이 모드는 기존
direct-append 동작을 사용하며 상태를 삭제하지 않는다. 주간보고를 만들 때는 session-summary의
resolved 표시만 신뢰하지 말고 Redmine 생성기의 최신 Git 대조를 최종 권위로 유지한다.

## AI 호출 예산과 backoff

- 실행당 모델 호출은 최대 1회이며 fallback 모델을 호출하지 않는다.
- 기본 모델/effort는 `haiku` / `low`다.
- 도구와 slash command를 비활성화하고 세션을 저장하지 않는다.
- 전체 프롬프트는 기본 65,536 bytes 이하로 제한한다. 실행은 180초 후 TERM,
  종료되지 않으면 5초 뒤 KILL해 하드 상한을 둔다.
- quota 오류는 6시간, 인증 오류는 24시간 `state/claude-backoff` marker로 재호출을 막는다.
  만료 시각은 오류가 출력된 시점이 아니라 예약 실행 시작 시각을 기준으로 계산해 다음 날
  같은 시각의 cron을 불필요하게 건너뛰지 않는다.
- 실패는 `auth`, `quota`, `timeout`, `input-limit`, `other`로 로그와 요약 파일에 구분한다.
- `flock -n`으로 겹친 요약 실행은 모델을 추가 호출하지 않고 건너뛴다. 수요일 rotate는
  같은 lock을 기다린 뒤 실행해 진행 중인 요약 결과까지 해당 주 archive에 포함한다.

운영 기본값은 다음 환경변수로 명시적으로 덮어쓸 수 있다.

| 환경변수 | 기본값 |
|---|---:|
| `SESSION_SUMMARY_MODEL` | `haiku` |
| `SESSION_SUMMARY_EFFORT` | `low` |
| `SESSION_SUMMARY_MAX_INPUT_BYTES` | `65536` |
| `SESSION_SUMMARY_TIMEOUT_SECONDS` | `180` |
| `SESSION_SUMMARY_TIMEOUT_KILL_AFTER_SECONDS` | `5` |
| `SESSION_SUMMARY_QUOTA_BACKOFF_SECONDS` | `21600` |
| `SESSION_SUMMARY_AUTH_BACKOFF_SECONDS` | `86400` |
| `SESSION_SUMMARY_RESOLUTION_TRACKING` | `1` |
| `SESSION_SUMMARY_RESOLUTION_TRACKER` | `session-summary/resolution-tracker.py` |

## 크론

```cron
0 9 * * *    .../session-summary.sh           # 매일 09:00 요약 (어제 1일치)
10 9 * * 3   .../session-summary.sh rotate    # 수요일 09:10 로테이트
```

## 파일 레이아웃

```
session-summary/
├── session-summary.sh
├── resolution-tracker.py           # prepare/reconcile 검증 helper
├── logs/                          # gitignored
│   ├── session-summary.md         # 현재 주 누적 요약 (작업본)
│   └── summary.log                # 실행 로그
├── state/                         # gitignored
│   ├── claude-backoff             # quota/auth 다음 호출 허용 시각
│   └── unresolved-items.json      # stable ID, repo/baseline, resolution 상태
└── archive/                       # git 추적
    ├── summary-YYYY-MM-DD_YYYY-MM-DD.md  (주간 아카이브)
    └── .trash/                    # gitignored
```

## 아카이브 파일명 규칙

`summary-<FIRST>_<LAST>.md` — `FIRST`는 `date -d "7 days ago"` (지난 수요일), `LAST`는 `date -d "yesterday"` (이번주 화요일). 수요일 09:10 로테이트 기준이라 항상 7일 범위가 보장된다.

(예전 버전은 파일 내용의 grep으로 날짜를 추출해 1일짜리 파일명이 생기는 문제가 있었음 — 2026-04-22 개선)
