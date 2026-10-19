# 호스트 비정상 재부팅 디버깅 구성안

- 작성: 2026-09-12 / **갱신: 2026-10-19**, Asia/Seoul (KST)
- 상태: **Backlog — 다른 작업 종료 후 별도 유지보수 승인으로 진행**
- 추적 Issue: [#57](https://github.com/jhw7500/personal-ops/issues/57)
- 이번 범위: 구성안 문서화, Issue 등록, 그리고 **읽기 전용 증거 수집**(부팅 이력 판정과
  BERT 레코드 판독)까지. 설치·설정 변경·재부팅은 여전히 실행하지 않는다.
- 관련 작업: [BIOS / Intel ME 업데이트 #56](https://github.com/jhw7500/personal-ops/issues/56)
- 관련 로컬 문서: `docs/host-bios-me-update.md`

## 목적과 현재 결론

다음 비정상 재부팅에서 커널 패닉, 하드웨어 오류, 자원 부족, 전원 단절을 구분할
증거를 확보한다. 현재 원인은 미확정이며, 이 구성 자체가 재부팅 문제의 해결책은 아니다.

확인 당시 환경은 Ubuntu 22.04.5 LTS, `6.8.0-138-generic`,
Intel Core i9-12900KS / ASUS PRIME Z690-P D4, BIOS 1008이다.
아래 종료 시각은 journal의 마지막 기록이므로 실제 장애 시각의 **하한**이다
(`SyncIntervalSec` 기본 5분). 다만 다음 부팅까지 29~60초라 손실 구간은 좁다.

**정상·비정상 판정은 PID 1의 종료 마커(`systemd[1]: Shutting down.`,
`Unmounting ...`, `systemd-shutdown`) 유무만을 근거로 한다.** 재부팅 간격
(정상 21초 / 비정상 29·33·43초)은 둘을 구분하지 못하므로 판정 근거로 쓰지 않는다.
사용자 세션 systemd(`systemd[2891885]` 등)가 남긴 `Reached target Shutdown`은
종료와 무관한 시각의 기록이므로 PID 1로 한정해서 본다.

| Boot | 시작 (KST) | 종료 (KST) | 판정 | 근거 |
| --- | --- | --- | --- | --- |
| -4 | 06-04 08:59:58 | 09-09 09:40:40 | 정상 | PID 1 종료 마커 존재. **97일 무중단** |
| -3 | 09-09 09:41:01 | 09-11 16:50:47 | **비정상** | PID 1 마커 0건. 1초 전 `xhci_hcd: HC died` |
| -2 | 09-11 16:51:20 | 09-12 15:58:22 | **비정상** | PID 1 마커 0건. 커널 로그는 15:19:27이 마지막 |
| -1 | 09-12 15:59:05 | 09-12 21:09:33 | **비정상** | PID 1 마커 0건. 1초 전 `xhci_hcd: HC died` |
| — | 09-12 21:10:02 | 09-18 13:38:55 | 정상 | PID 1 종료 마커 존재. BERT 없음 |
| — | 09-18 13:39:17 | 09-18 14:05:30 | 정상 | PID 1 종료 마커 존재. BERT 없음 |
| — | 09-18 14:05:49 | 09-21 11:00:11 | **비정상** | PID 1 마커 0건. BERT 1건. USB 오류 0건 |
| — | 09-21 11:00:51 | 10-12 15:02:30 | **비정상** | PID 1 마커 0건. BERT 1건. 마지막 기록은 CI 테스트의 Docker 컨테이너 정리 |
| — | 10-12 15:03:30 | 10-19 12:11:19 | **비정상** | PID 1 마커 0건. BERT 1건. 크래시 전 31분 커널·err 로그 0건 |
| 0 | 10-19 12:11:50 | (진행 중) | — | 2026-10-19 12:15 기준 |

- **비정상 재부팅은 현재까지 6회다** (09-11 16:50, 09-12 15:58, 09-12 21:09, 09-21 11:00,
  10-12 15:02, 10-19 12:11). 앞의 3건은 약 28시간 안에 몰려 있었고, 이후 간격은 9일·21일·7일로
  주기성이 없다. **09-12 이후 6일간 무사고였다가 09-21에 재발했다.**
  따라서 이것은 단기 군집이 아니라 **간헐적으로 지속되는 현상**이며, 재현 대기 방식의 조사는
  비용이 크다. 개입 실험의 효과 판정에도 최소 수 주가 필요하다는 뜻이다.
  그 앞 부팅은 97일 무중단 후 정상 종료했으므로 만성적 배경 빈도도 아니다.
  2026-09-12 21:09 사건은 이 문서의 최초 작성 시점에 누락돼 있었다.
- **10-12 15:02와 10-19 12:11 사건(5·6번째)도 같은 판정이다.** 두 건 모두 PID 1 마커 0건,
  BERT Fatal 1건, 크래시 전 30분 커널 메시지·err 로그 0건이다. 10-12는 마지막 기록이 CI 테스트의
  Docker 컨테이너 정리(veth 해제·overlay 마운트 해제)였고 직전 atop 구간(15:00) CPU 사용률 약 10%,
  여유 메모리 3.9G였다. 10-19는 마지막 기록이 2초 주기 하트비트 로그였고 직전 atop 구간(12:10)
  idle 93%, 여유 메모리 1.7G, PSI 0이었다. 둘 다 자원 압박이나 소프트웨어 오류가 선행하지 않았다.
  10-12 이후 패키지 변경은 정기 보안 업데이트뿐이고 커널 6.8.0-138·BIOS 1008·ME 16.0.15는 그대로다.
  PID 1 마커 검색의 양성 대조로, 정상 종료한 09-18 14:05 부팅에서 같은 검색이 3줄을 검출함을 확인했다.
- **관측(함의 아님): 6건 모두 11:00~21:09 KST 사이에 발생했고 야간 발생은 없다**
  (금 16:50, 토 15:58, 토 21:09, 월 11:00, 월 15:02, 월 12:11). 표본 6건이라 통계적 결론은 내리지
  않지만, 사람이 있는 시간에만 달라지는 요소(USB 장치 탈착, 타겟 보드 전원, 건물 전원 품질,
  실내 온도)는 BIOS 작업 전 점검 목록에 올릴 가치가 있다.
- 조사한 로그에서 정상 종료 절차와 원인을 확정할 panic/OOM 기록을 찾지 못했다.
  `pstore`는 백엔드(`efi_pstore`)가 정상 등록됐는데도 비어 있었고
  (systemd가 `ConditionDirectoryNotEmpty=/sys/fs/pstore was not met`로 skip),
  `/var/crash`와 `/var/lib/systemd/pstore`도 비어 있다.
  즉 **커널 패닉 기록이 남지 않았다.**
- `kernel.panic = 0`이라 커널 패닉은 영구 정지를 일으키지 재부팅시키지 않는데
  기기는 29~60초 만에 복귀했다. `/dev/watchdog` 장치도 없어 소프트웨어 워치독
  리셋도 배제된다. journald·rsyslog·atop 세 채널이 동시에 끊겼다.
- **BERT 레코드가 6건 모두에서 일관되게 나타나는 유일한 신호다** (아래 절 참조).
- 09-10 06:09 이후 09-12까지 dpkg 로그의 변경은 glibc·perl·python 정기 업데이트뿐이고,
  커널 패키지는 09-05가 마지막이며 그마저 미사용 5.15 계열이다.
  **소프트웨어 변경으로 설명되지 않는다.**
- 9월 11일 마지막 atop 구간에는 메모리·I/O 압력이 있었지만,
  9월 12일 마지막 구간에서는 같은 수준의 압력이 보이지 않았다.
  특히 후자는 15:50 기록이라 최종 journal보다 약 8분 앞선다.
- **xHCI 사망은 원인이 아니라 증상 쪽으로 본다.** 3건 중 2건에만 나타나고
  (09-12 15:58 사건에는 없음), USB 컨트롤러가 죽는다고 보드가 리셋되지는 않는다.
- 커널 버전, 과거 예약 작업과 재부팅의 인과관계는 입증되지 않았다.
  예약 작업 정리와 재부팅 원인 규명은 별개다.

## BERT 판독 결과 (2026-09-14)

커널의 `Skipped 1 error records`는 레코드가 손상·미지원이어서가 아니라
**영역이 3444바이트로 커널의 인라인 출력 한계(`ACPI_BERT_PRINT_MAX_LEN`, 1024바이트)를
넘어서** 콘솔 출력 대신 `/sys/firmware/acpi/tables/data/BERT`로 원본을 노출한 것이다.
`/sys/firmware/acpi/tables/BERT`(48바이트)는 포인터일 뿐이므로 그쪽을 읽으면 안 된다.

판독 결과 3444바이트 전체가 **하나의 Fatal 레코드**이고 세 섹션을 담고 있다.

| 섹션 | 크기 | 유형 |
| --- | --- | --- |
| #1 | 2592 B | Firmware Error Record — SOC Firmware Error Record Type2 |
| #2 | 544 B | 〃 |
| #3 | 72 B | 〃 |

- 섹션 GUID는 셋 다 `8f87f311-c998-4d9e-a0c4-6065518c4f6d`이고 레코드 타입 바이트
  `0x02`/리비전 `0x02`와 일치한다. **Intel SoC CrashLog 텔레메트리**다.
- **DIMM 오류도, CPU 코어 MCE도, PCIe AER도 아니다.** 펌웨어가 이 사건을 그 어느
  것으로도 귀속하지 않고 SoC 내부 크래시 텔레메트리를 덤프했다.
- 파싱 정합성 교차 근거 3건: BERT 헤더의 영역 길이 `0x0d74`(3444)가 파일 크기와 일치,
  각 섹션 페이로드의 dword[1]이 선언 크기와 일치(#2는 128 dword=512 B, 섹션 544−32=512),
  커널이 센 `Total records found: 1`이 판독된 status block 1개와 일치.
- CPER 타임스탬프 필드는 세 섹션 모두 0이라(펌웨어가 채우지 않음) 레코드 자체로는
  어느 사건인지 알 수 없다. 대신 **부팅별 출현 패턴**이 답을 준다 —
  정상 종료 뒤 부팅(-4, -3)에는 BERT가 없고 비정상 종료 뒤 부팅(-2, -1, 0)에 3/3으로
  나타난다. 즉 **사건마다 새로 캡처된 것이며 오래된 레코드의 재보고가 아니다.**
  지금 보존된 것은 가장 최근 사건(09-12 21:09)의 기록이다.
- `block_status`가 0인 것은 커널이 부팅 시 읽고 지운 흔적이지 레코드 부재가 아니다.
  판독 시 이 값으로 중단하면 "레코드 없음"으로 오판한다.

### 마이크로코드 현황 — BIOS 업데이트 기대 이익에 직접 영향

```text
microcode: Current revision: 0x0000003e
microcode: Updated early from: 0x00000018
```

- BIOS 1008이 공급하는 마이크로코드는 `0x18`이고, Ubuntu의
  `intel-microcode 3.20260210`이 부팅마다 `0x3e`로 덮어쓰고 있다.
- **세 번의 크래시는 전부 `0x3e`가 적용된 상태에서 발생했다.**
- 따라서 "BIOS 4003이 마이크로코드를 갱신하므로 이 증상을 고친다"는 추론은
  이 호스트에 대해서는 근거가 약하다. 그 이점은 이미 OS 경로로 받고 있다.
  #56의 기대 이익 산정은 이 사실을 반영해야 한다.
- 다만 **CrashLog를 기록하는 주체는 PMC(전력관리 컨트롤러)이고, PMC 펌웨어와 SoC
  전력관리 참조 코드는 `intel-microcode`가 아니라 BIOS/FSP에서 온다.** Intel ME
  (16.0.15, 2022년) 역시 BIOS로만 갱신된다. BIOS 업데이트의 남은 이점은 이 두 계층이며,
  공교롭게도 그것이 이번 장애를 기록한 계층이다. 인과가 입증된 것은 아니고 가설이다.

### 미확인 항목

- **CrashLog 페이로드의 실제 내용.** 어느 IP 블록이 정지했는지 이름을 대려면 Intel의
  비공개 CrashLog 디스크립터가 필요하다. 확인된 것은 구조(타입·크기·정합성)까지다.
  OS에서 상시 읽는 대안 경로(`/sys/class/intel_pmt/crashlog*`)도 2026-09-21에 시험했으나
  **원리적으로 막혀 있다.** 아래 "PMT CrashLog 경로" 절을 보고 같은 시도를 반복하지 않는다.
- **BIOS `Restore on AC Power Loss` 설정값.** 순간 정전 후 자동 복전인지 내부 리셋인지
  구분하려면 이 값이 필요하다. BIOS 진입 시 **Optimized Defaults 적용 전에** 기록한다.
- ~~**BERT 레코드 3건이 각각 다른 내용인지.**~~ → **2026-09-21 해결.** 09-21 사건의 레코드를
  수집해 09-12 것과 대조했다. 해시가 다르므로 **사건마다 새로 캡처된다**는 기존 가설이
  확증됐고, 동시에 내용의 98.8%가 일치해 **동일 실패 모드**로 판단한다.
  아래 "BERT 레코드 대조" 절 참조. 앞선 2건(09-11, 09-12 15:58)은 수동 수집에 의존한 탓에
  이미 소실됐으며, 이것이 부팅 시 자동 수집을 도입한 이유다.
- 검색 범위 밖(원격 로그, BMC/IPMI SEL, BIOS 이벤트 로그)은 조회하지 않았으므로
  "다른 곳에도 기록이 없다"고는 말하지 않는다.

### BERT 레코드 대조 (2026-09-21)

09-21 11:00 사건의 레코드를 수집해 09-12 21:09 것과 바이트 단위로 비교했다.
둘 다 3,444 B / Fatal / 3섹션(2592·544·72 B) / 동일 GUID로 **구조는 완전히 같고,
내용은 451 B(13%)가 다르다.**

| 섹션 | 크기 | 차이 | 비율 |
| --- | --- | --- | --- |
| Payload #1 | 2,592 B | 32 B | 1.2% |
| Payload #2 | 544 B | 419 B | 77% |
| Payload #3 | 72 B | 0 B | 0% (`0xDEADBEEF` 마커 포함 동일) |
| 섹션 디스크립터 ×3 | — | 0 B | 0% |

Payload #1의 32 B 차이는 산재하지 않고 6개 클러스터
(`0x0028-002d`, `0x0040-0042`, `0x01e5-01ec`, `0x0231-0238`, `0x08b9-08c0`,
`0x09ab`/`0x09e8-09eb`)에 한정된다. 4~6 B 덩어리로 타임스탬프·카운터 필드의 형태이며,
나머지 2,560 B(98.8%)는 9일 간격의 두 사건에서 완전히 일치한다.

- **확정**: 해시가 다르므로 레코드는 사건마다 새로 캡처된다. 오래된 레코드의 재보고가 아니다.
- **추정**: Payload #1은 크래시 시점의 SoC 상태 스냅샷, #2는 이벤트/트레이스 버퍼로 보인다.
  상태 스냅샷의 98.8% 일치는 두 사건이 동일 실패 모드임을 시사한다. 다만 섹션의 의미론적
  역할은 비공개 디스크립터 없이 확정할 수 없다 — 확인된 범위는 바이트 일치까지다.
- Payload #2는 8 B 단위(타임스탬프 4 B + 이벤트 코드 4 B) 슬롯 64개로 보이며, 두 사건 모두
  `0x20xx` 계열에서 `0x28xx` 계열로 전이하며 끝난다. 관찰 수준이고 의미는 알 수 없다.

### BERT 레코드 대조 — 5·6번째 사건 (2026-10-19)

10-12 15:02 사건과 10-19 12:11 사건의 레코드는 `host-bert-autocollect.service`가 부팅 시 자동
수집했다(09-21 설치 후 첫 실전 가동 2회, 소실 0건). 네 레코드(09-12·09-21·10-12·10-19) 모두
3,444 B / Fatal / 3섹션(2592·544·72 B) / 동일 GUID로 구조가 같고, 해시는 전부 다르다.
헤더·디스크립터 3개·Payload #3은 모든 쌍에서 0 B 차이다.

| 비교 | 전체 차이 | Payload #1 (2,592 B) | Payload #2 (544 B) |
| --- | --- | --- | --- |
| 10-19 vs 10-12 | 374 B | 30 B (1.2%) | 344 B (63%) |
| 10-19 vs 09-21 | 404 B | 33 B (1.3%) | 371 B (68%) |
| 10-19 vs 09-12 | 436 B | 32 B (1.2%) | 404 B (74%) |
| 10-12 vs 09-21 | 375 B | 31 B (1.2%) | 344 B (63%) |
| 10-12 vs 09-12 | 445 B | 34 B (1.3%) | 411 B (76%) |

Payload #1의 차이 바이트는 여섯 쌍 모두 위에 기록한 클러스터(`0x0028-002d`, `0x0040-0042`,
`0x01e5-01ec`, `0x0231-0238`, `0x08b9-08c0`, `0x09ab-09ac`/`0x09e8-09eb`)와 `0x0036` 1바이트
안에만 있다(범위 밖 0바이트, 쌍별로 확인). 방법은 `cmp -l`로 바이트 오프셋을 뽑아 섹션 경계
(헤더 20 B, 디스크립터 72 B ×3)로 구간을 나눈 것이다.

- **확정**: 상태 스냅샷(Payload #1)의 98.7% 이상이 4건에서 일치한다. 09-21에 세운 "동일 실패 모드"
  판단은 6건 전체로 확장된다. BERT 출현 상관은 **비정상 6/6 출현, 정상 2/2 미출현**으로 유지된다.
- **OS 쪽 증거 수집은 포화됐다.** 같은 시그니처가 6번 반복됐고 자동 수집이 소실 없이 동작하므로
  추가 로깅으로 새로 얻을 정보는 거의 없다. 남은 수율 높은 개입은 펌웨어 계층(#56)과
  전원·환경 점검이며, "미확인 항목"의 `Restore on AC Power Loss` 기록은 그대로 유효하다.

### 개입 실험 1 — 전력 제한 (2026-10-19 13:47 KST 시작)

OS 쪽 증거가 포화된 뒤 처음 시작한 **변수 조작 실험**이다. 다음 세션은 이 상태가 실험 조건임을
알고 읽어야 한다.

- **발견**: RAPL PL1/PL2가 BIOS 기본값 4095 W(무제한)였다. 그 상태에서 load average 3 수준에도
  패키지가 1초 만에 44→93°C로 뛰고 열 스로틀이 분당 약 28~50회 걸렸다(부팅 후 92분 누적 2,619회,
  커널 로그는 8초 미만 에피소드를 남기지 않아 0건). 버스트 주체는 `chroma-mcp`(claude-mem 임베딩,
  스레드 103개·코어 제한 없음, 부팅 후 CPU 1위)와 claude·codex 세션, pytest·node 테스트 러너다.
  10-12·10-19 크래시 직전 10분의 CPU 1위도 `chroma-mcp`였다. atop 10분 평균으로는 보이지 않으며,
  이 문서 앞부분의 "자원 압박 없음"은 **지속 부하 기준**으로만 읽어야 한다.
- **가설(미입증)**: 무제한 전력의 다중 코어 버스트 → VR/PSU 과도 응답 실패 → IERR 또는 PWRGOOD 상실
  → PMC 글로벌 리셋(CrashLog 기록). 낮 시간대 집중·OS 흔적 부재·SoC CrashLog 형태와 부합한다.
- **조치**: `scripts/host-power-limit.service`가 부팅마다 PL1 150 W / PL2 241 W(12900KS Intel 규격)를
  쓰고 되읽어 검증한다. `scripts/host-power-telemetry.timer`가 1분마다 온도·스로틀 누적·현재 PL을
  journal(`-t host-power-telemetry`)에 남긴다. 적용 전 기준: 스로틀 분당 약 28회(부팅 후 평균).
- **판정 기준**: 제한이 걸린 상태(텔레메트리로 확인)로 **3주 무사고**면 전력·열 경로 유력, 그 뒤 #56을
  낮은 위험으로 진행. 제한이 걸린 채 재발하면 가설 약화 → PSU 점검과 #56이 다음 순서. 재발 시
  `journalctl -b -1 -t host-power-telemetry | tail`로 크래시 직전 PL 값과 스로틀 증가를 먼저 본다.
- **같이 바꾸지 않은 것**: `chroma-mcp` 제한(`CLAUDE_MEM_CHROMA_ENABLED=false` 또는 E코어 고정)과
  BIOS 설정은 변수 분리를 위해 보류했다. 쿨러 물리 점검은 소프트웨어 변수가 아니므로 병행 가능하다.
- **드리프트 (13:52, 적용 5분 뒤)**: PL1만 4095 W로 되돌아가고 PL2 241 W는 유지된 채 15:07까지 73분 지속됐다.
  이 호스트의 전력 데몬은 thermald(`--adaptive`)뿐이고 `/etc/thermald/thermal-cpu-cdev-order.xml`의 냉각
  1순위가 `rapl_controller`다. 과열 때 PL1을 낮췄다가 식으면 시작 때 기억한 최대값(4095 W)으로 복구하는
  동작이 "온도 스파이크 직후 PL1만 4095" 패턴과 맞는다 — **추정**이며, 아래 조치 뒤 DRIFT가 멈추면 확정한다.
  `host-power-telemetry.sh`에 드리프트 감시를 넣어 목표와 다르면 `DRIFT`를 기록하고 root 틱에서 즉시
  재적용한다(15:07:32 첫 자동 복구 확인). 근본 조치(sudo)는 cdev-order에서 `rapl_controller` 한 줄 제거 후
  thermald 재시작, 그래도 드리프트면 thermald 비활성. **실험 시작 시각은 PL1이 안정적으로 유지된 시점부터
  다시 센다** — `journalctl -t host-power-telemetry | grep -c DRIFT`로 확인.
- **원인 확정·실험 시작 (15:46)**: `thermal-cpu-cdev-order.xml`에서 `rapl_controller`를 빼고 thermald를
  재시작한 뒤 20분(15:46:30~16:06:30) DRIFT 0건 — 수정 전 36분 동안 8건이었다. PL1을 되돌리던 주체는
  thermald로 확정한다. 같은 부하(load 9~11)에서 스로틀은 분당 153회(드리프트 상태) → **분당 1회**(제한
  유지), 패키지 최고 93°C → 78°C. **실험 시작 시각은 2026-10-19 15:46 KST**이고 3주 판정은 11-09다.
  남은 스로틀(20분 23회)은 단일 코어 부스트 몫으로 PL2로는 막히지 않는 설계상 잔여다.
- **쿨러 한계**: PL2 241 W만 걸린 14:43에도 1분에 스로틀 2,690회(chroma-mcp 버스트, load 18)가 났다.
  이 쿨러는 241 W도 받아 내지 못하므로 PL1 150 W 유지와 쿨러 물리 점검이 더 중요해졌다.
- **빌드 영향**: 56초 이상 전 코어 부하는 150 W로 제한돼 Yocto 빌드 시간이 최대 10% 안팎 늘 수 있다.
  부담이면 유닛의 `Environment=PL1_W=`만 200~241로 올린다. 재부팅 시 BIOS 값으로 원복되므로 유닛이
  꺼져 있으면 실험도 끝난 것이다.

### PMT CrashLog 경로 — 원리적으로 막힘 (재시도 불필요)

BERT는 부팅당 1회 스냅샷이고 다음 재부팅에 덮어써진다. 같은 SoC CrashLog를 OS에서 상시
읽는 경로가 있는지 2026-09-21에 확인했다. **결론은 막혀 있고, 현재 공개된 정보로는 열 수 없다.**

펌웨어는 정상적으로 광고하고 있다 (`sudo lspci -vvv -s 00:0a.0`):

```
[100] DVSEC Vendor=8086 ID=0002   ← VSEC_ID_TELEMETRY (커널이 사용 → telem1, telem2)
[110] DVSEC Vendor=8086 ID=0003   ← VSEC_ID_WATCHER   (커널이 무시)
[120] DVSEC Vendor=8086 ID=0004   ← VSEC_ID_CRASHLOG  (커널이 무시)
```

관문은 둘이고 첫째만 열린다.

| # | 관문 | 위치 | 결과 |
| --- | --- | --- | --- |
| 1 | 커널 `caps` 허용 목록 | `vsec.c` — ADL(0x467d) → `tgl_info.caps = VSEC_CAP_TELEMETRY` | **열 수 있음 (검증함)** |
| 2 | 드라이버 하드웨어 타입 검사 | `crashlog.c` — `pmt_crashlog_supported()` | **열 수 없음** |

관문 1은 실제로 열어서 확인했다. `tgl_info.caps |= VSEC_CAP_CRASHLOG` 한 비트를 추가해
upstream v6.8.12 소스로 `intel_vsec` 모듈을 빌드·로드하자 `intel_vsec.crashlog.1` auxiliary
장치가 생성되고 `pmt_crashlog` 드라이버가 바인딩까지 됐으나, sysfs 장치는 생기지 않았다.
(Secure Boot disabled / `sig_enforce=N` 이라 out-of-tree 모듈 로드는 가능했다. 시험 후 원복함.)

관문 2가 이유다. `pmt_crashlog`는 **OOBMSM**(Out-Of-Band Management Services Module —
서버·데이터센터 플랫폼, 최근 Arc GPU) 타입만 지원하며, 그 외에는 entry를 만들지 않고
조용히 반환한다. 소비자용 ADL의 PMC crashlog는 다른 타입이고 포맷이 공개되어 있지 않다.
드라이버가 죽은 코드인 것이 아니라(BMG 지원 추가 등 활발히 유지보수된다) **대상 하드웨어가
우리 것이 아니다.**

- **커널 업그레이드는 해결책이 아니다.** 최신 mainline은 `caps` 하드코딩 대신
  `CONFIG_INTEL_PMT_CRASHLOG` 기반 판정과 DVSEC ID 자동 유도로 바뀌어 관문 1이 완화됐지만,
  타입 검사는 그대로 OOBMSM 전용이다(`version == 0 || version == 2`로 넓어진 것이 BMG 지원).
  ADL은 최신에서도 여전히 `tgl_info`를 쓴다.
- **타입 검사 우회는 권하지 않는다.** `crashlog.c`는 `disable`/`trigger`/`clear` 제어를 위해
  `writel()`로 하드웨어 레지스터에 쓴다(3곳). 레이아웃이 다를 수 있는 상태에서 제어 레지스터에
  쓰는 것은 BERT 읽기와 전혀 다른 수준의 위험이다.

### 증거 보존 위치

원본 덤프는 물리 주소를 포함하므로 **공개 저장소에 커밋하지 않는다.**
저장소 밖 `~/host-incident-evidence/`에 `0700`/`0600`으로 보관한다.

| 경로 | 내용 |
| --- | --- |
| `~/host-incident-evidence/bert-20260912-2109/bert-region.bin` | BERT 영역 원본 3444 B |
| `~/host-incident-evidence/bert-20260912-2109/bert-table.bin` | BERT 테이블 헤더 48 B |
| `~/host-incident-evidence/bert-20260912-2109/decoded.txt` | 판독 결과 |
| `~/host-incident-evidence/bert-20260912-2109/context.txt` | 수집 시점 BIOS/ME/부팅 목록 |
| `~/host-incident-evidence/bert-20260921-121024/` | 09-21 11:00 사건 일습 (region 3444 B·table·decoded·context) |
| `~/host-incident-evidence/bert-boot-20260921-134344/` | 위와 동일 레코드. 자동 수집 서비스 설치 검증 때 재수집한 사본 (`cmp` 일치) |
| `~/host-incident-evidence/bert-boot-20261012-150428/` | 10-12 15:02 사건 일습 (자동 수집) |
| `~/host-incident-evidence/bert-boot-20261019-121221/` | 10-19 12:11 사건 일습 (자동 수집) |

판독 도구는 저장소에서 관리한다 (증거와 달리 커밋 대상이다).

| 경로 | 내용 |
| --- | --- |
| `scripts/host-bert-collect.sh` | 수집+판독 실행 래퍼 (수동) |
| `scripts/host-bert-decode.py` | CPER/BERT 판독기 |
| `scripts/host-bert-autocollect.sh` | 부팅 시 BERT 레코드가 있을 때만 자동 수집 |
| `scripts/host-bert-autocollect.service` | 위 스크립트의 systemd oneshot 유닛 |

재수집·재판독은 `bash scripts/host-bert-collect.sh` 한 번이면 된다
(sudo 비밀번호 필요, 읽기 전용). 기본 출력은 저장소 밖
`~/host-incident-evidence/bert-<타임스탬프>/`이고, 첫 인자로 다른 경로를 줄 수 있다.
사용법과 판독 시 주의점은 `scripts/README.md`의 "BERT 하드웨어 오류 판독" 절에 있다.

## 수집 현황과 목표

| Component | Observed baseline | Proposed target | 이 시그니처 수율 |
| --- | --- | --- | --- |
| **BERT 판독** | 수동 수집 (2건 소실) | **부팅 시 자동 수집** (`host-bert-autocollect.service`) | **높음 — 6/6 상관** |
| rasdaemon | Not installed | Supported RAS events + persistent DB | 중간 |
| journald | Persistent; default sync 5 min; 2.4 GiB | Explicit persistent; sync 30 s | 중간 |
| atop | Active; interval 600 s; retention 28 days | Interval 60 s; retain 28 days initially | 중간 |
| pstore | EFI backend; archive service present | Preserve and verify existing archive path | 낮음 — 3회 모두 공백 |
| Remote logging | Not configured by this work | Optional wired netconsole receiver | 중간 |
| sysstat | Collection timer inactive | Optional; no duplicate collector by default | 낮음 |
| **kdump** | Not installed; crash size 0; loaded 0 | **보류 — 아래 재평가 참조** | **낮음 — 패닉이 없다** |

현황의 근거는 로컬 journal, `/proc/cmdline`, `/sys/kernel/kexec_crash_*`,
패키지 설치 상태, `/etc/default/atop`, `systemctl cat atop.service`,
systemd-pstore 서비스와 설치된 systemd 249 매뉴얼이다. 적용 시점에 다시 확인한다.

**계측 항목은 완성도 순이 아니라 관측된 실패 시그니처에 대한 수율 순으로 적용한다.**
이 호스트의 시그니처는 "커널 패닉 없음 + pstore 공백 + 29~60초 내 복귀 + Fatal BERT"이며,
패닉을 전제로 하는 수단은 여기서 아무것도 남기지 못한다.

## 착수 조건과 금지 사항

- [ ] 사용자가 다른 작업의 종료를 확인하고 유지보수 시간과 시스템 변경을 승인한다.
- [ ] 실행 중인 빌드·배치 작업, 콘솔 접근, 부팅 복구 수단, 디스크 여유를 확인한다.
- [ ] 아래 변경별 명령·영향·롤백을 실행 전에 제시한다. 이 Issue는 실행 승인이 아니다.
- [ ] BIOS/ME 작업은 #56의 전원·온도·보드 확인과 별도 승인 조건을 따른다.

BitBake 실행·중단·재시작, 태스크·캐시 정리, 스트레스 테스트, 커널 교체,
강제 패닉과 BIOS 플래시는 이 문서 등록으로 승인되지 않는다.
예약 실행이나 Task/Claim도 이번에는 만들지 않는다.

## 1. 변경 전 증거 보존

- [ ] 두 사건의 해당 boot ID를 `journalctl --list-boots`로 지정해 전체 journal을 보존한다.
  이후 재부팅으로 바뀌는 `-1`, `-2` 번호를 영구 식별자로 사용하지 않는다.
- [ ] `/var/log/atop/atop_20260911`, `/var/log/atop/atop_20260912`,
  현재 BERT/MCE/EDAC/AER/USB 로그, 기존 pstore 보관물과 `/var/crash`를 보존한다.
- [ ] BIOS/ME/커널 버전, 부팅 인자, Secure Boot 상태, watchdog/panic sysctl,
  수집기 설정·서비스 상태와 설치 버전을 함께 기록한다.
- [ ] 사건별 비공개 보관 디렉터리는 `0700`, 내보낸 로그·덤프는 `0600`으로 관리하고
  소유자·파일 유형·권한·SHA-256과 복사 완료 여부를 확인한다.

공개 저장소와 Issue에는 분석 요약만 올린다. 원본 로그, 프로세스 전체 인자,
인증 정보, 내부 주소와 메모리 덤프는 올리지 않는다. 로그에 비밀값이 들어갈 수 있으므로
단순 확장자 검사만으로 공개 가능하다고 판단하지 않는다.

## 2. 로컬 로그와 성능 이력 보강

### journald

변경 예정 파일: `/etc/systemd/journald.conf.d/60-reboot-debug.conf`.
기존 drop-in 충돌과 사건 기록 백업을 확인한 뒤 아래 후보를 적용한다.

```ini
[Journal]
Storage=persistent
SyncIntervalSec=30s
SystemMaxUse=4G
SystemKeepFree=20G
```

- [ ] 적용 전 기존 설정을 백업하고, 용량 제한 때문에 과거 journal이 회전·삭제될 수 있음을 확인한다.
- [ ] 승인된 시간에 journald 재시작과 필요한 flush를 수행하고 유효 설정·서비스 오류를 확인한다.
- [ ] 식별 가능한 테스트 로그를 남긴 후 유지보수 재부팅 뒤에도 조회되는지 확인한다.

현재도 영구 저장은 사용 중이다. 30초 동기화는 일반 로그의 미저장 구간을 줄이기 위한
제안이며, 전원 단절이나 저장장치 장애에서 마지막 30초 보존을 보장하지 않는다.
CRIT/ALERT/EMERG는 기본적으로 즉시 동기화된다.
[journald 설정](https://www.freedesktop.org/software/systemd/man/252/journald.conf.html)
및 이 호스트의 `man journald.conf`를 함께 확인한다.

### atop 및 온도

변경 예정 파일: `/etc/default/atop`. 기존 `LOGOPTS`, `LOGPATH`를 보존한다.

```ini
LOGINTERVAL=60
LOGGENERATIONS=28
```

- [ ] 사건 로그를 백업한 뒤 수집 서비스만 승인된 시간에 재시작한다.
- [ ] 연속 3개 이상의 새 샘플 시각이 약 60초 간격인지, CPU·메모리·swap·I/O·PSI를
  실제로 읽을 수 있는지 확인한다.
- [ ] 24시간 동안 파일 증가량과 수집 오버헤드를 측정해 보존 기간·공간 예산을 재평가한다.
- [ ] 기존 센서가 제공하는 CPU 온도·팬 값을 수집할 필요가 있으면 60초 간격 후보로 검토한다.
  센서 탐색/스트레스 테스트를 자동 실행하거나, 소프트웨어 값만으로 PSU 정상 여부를 판정하지 않는다.

**처음부터 보존 기간을 7일로 줄이지 않는다.** 설치된 `atop.service`의
`ExecStartPost`는 `LOGGENERATIONS`보다 오래된 `atop_*`를 삭제한다.
기간을 줄이고 재시작하면 기존 조사 자료도 삭제될 수 있다.
7일 보존은 필요한 과거 기록의 별도 보관과 명시적 정리 승인 후 선택한다.

sysstat는 타이머가 비활성이라 정상 수집 중이라고 가정하지 않는다.
기본 구성은 atop 중심이며, sysstat 복구는 중복 수집 필요성이 확인될 때 추가한다.

## 3. RAS 오류 기록과 기존 pstore 확인

- [ ] 승인 후 배포판 `rasdaemon` 패키지를 설치하고 서비스의 실제 기록 옵션과
  DB 경로를 확인해 영구 기록을 활성화한다. upstream 빌드를 새로 하지 않는다.
- [ ] 서비스 기동, DB 생성·접근권한, 활성 trace event 목록을 확인한다.
  `ras-mc-ctl --status`와 `--errors` 결과에 지원하지 않는 장치/기능을 구분해 기록한다.
- [ ] EFI pstore와 systemd-pstore의 기존 설정을 확인하고,
  `/sys/fs/pstore`와 `/var/lib/systemd/pstore`를 모두 회수 경로에 포함한다.

rasdaemon은 커널이 보고하는 지원 RAS 이벤트를 기록한다.
미지원 센서·비ECC 메모리 등에서는 오류 0건이 하드웨어 정상의 증거가 아니다.
[rasdaemon upstream](https://github.com/mchehab/rasdaemon)

pstore는 이미 사용 가능한 경로가 있으므로 무조건 새로 활성화하지 않는다.
systemd-pstore가 부팅 시 기록을 다른 위치로 옮긴 뒤 원본을 지울 수 있어
`/sys/fs/pstore`가 비어 있다는 사실만으로 수집 실패를 판단하지 않는다.
설치된 `man systemd-pstore` / `man pstore.conf` 기준으로 보관물을 먼저 확인한다.
EFI 공간이 작고 펌웨어 의존성이 있으므로 일반 종료 때마다 기록하도록 강제하지 않는다.

## 4. kdump 준비와 유지보수 재부팅

> **2026-09-14 재평가 — 이 절은 보류한다.**
>
> kdump는 커널이 패닉해야 덤프를 뜬다. 그런데 관측된 시그니처는 패닉이 아니다:
> `pstore`가 3회 모두 비어 있고, `kernel.panic = 0`이라 패닉이면 재부팅이 아니라
> 정지했어야 하는데 기기는 29~43초 만에 복귀했으며, 펌웨어가 남긴 것은
> SoC CrashLog(BERT)뿐이다. **이 실패 모드가 재현돼도 kdump는 아무것도 남기지 않을
> 가능성이 높다.**
>
> 반면 비용은 이 계획에서 가장 크다 — 예약 RAM(일반 빌드 가용 메모리 감소),
> GRUB/initramfs 변경, 대상 파일시스템 최소 64 GiB 여유(현재 루트 82% 사용, 162 GiB 남음),
> 유지보수 재부팅. **수율 최저 × 비용 최고이므로 순서에서 뒤로 보낸다.**
>
> 아래 절차는 폐기하지 않고 보존한다. 다음 조건 중 하나가 성립하면 다시 검토한다:
> (a) 이후 사건에서 커널 패닉이나 pstore 기록이 실제로 관측된다,
> (b) BERT·rasdaemon·netconsole로 원인을 좁히지 못하고 다른 수단이 소진됐다.

검토할 파일은 `/etc/default/kdump-tools`, 설치 패키지가 제공하는 GRUB 설정,
기존 `/etc/default/grub` 및 `/etc/default/grub.d/`이다.
패키지 제공 설정을 먼저 찾아 사용하고 동일한 `crashkernel` 옵션을 중복 추가하지 않는다.

- [ ] 먼저 읽기 전용 설치 시뮬레이션으로 의존성을 확인한다.

```bash
rtk proxy apt-get -s install kdump-tools rasdaemon
```

- [ ] 예상하지 못한 커널 교체·패키지 제거가 있으면 중단하고 계획을 수정한다.
  `linux-crashdump` 메타패키지를 무조건 추가하지 않는다.
- [ ] 승인 후 `kdump-tools` 설치, `USE_KDUMP=1`, 저장 경로와 필터링 설정을 확인한다.
  약 31 GiB RAM 환경에서 덤프 크기는 가변적이다. 이번 운영 기준으로 대상 파일시스템에
  최소 64 GiB 여유를 확보하고, 저장장치·LVM을 crash kernel에서도 읽고 쓸 수 있게 준비한다.
- [ ] 설치 버전의 Ubuntu 지침과 필요한 드라이버를 기준으로 예약 RAM 값을 결정해 기록한다.
  최신 문서의 예시 값이나 `crashkernel=auto`를 22.04에 검증 없이 복사하지 않는다.
  예약 RAM만큼 일반 빌드에 쓸 메모리가 줄어든다.
- [ ] Secure Boot/kexec 제약, initramfs와 GRUB 반영을 확인하고 콘솔 복구 수단을 확보한다.
  변경한 파일·예약값·명령 결과를 기록한 뒤 승인된 유지보수 재부팅을 한다.
- [ ] BIOS 업데이트를 같은 유지보수에 수행한다면 #56 절차를 따르고,
  펌웨어 단계를 건너뛰는 kexec 재부팅을 사용하지 않는다. 변경 전후 버전을 각각 기록한다.
- [ ] 재부팅 후 아래 결과를 확인한다.

```bash
rtk proxy sudo kdump-config show
rtk proxy cat /sys/kernel/kexec_crash_loaded
rtk proxy cat /sys/kernel/kexec_crash_size
rtk proxy cat /proc/cmdline
```

완료 기준은 `kdump-config show`의 준비 상태, `kexec_crash_loaded=1`,
`kexec_crash_size>0`, 의도한 부팅 인자다. 이는 준비 상태 검증이지 실제 덤프 생성 검증은 아니다.
강제 패닉 시험은 서비스 중단·데이터 손실 위험이 있으므로 별도 다운타임 승인이 필요하다.
이 문서에는 즉시 실행할 수 있는 강제 패닉 명령을 싣지 않는다.
[Ubuntu kdump 안내](https://ubuntu.com/server/docs/how-to/software/kernel-crash-dump/),
[22.04 kdump-config](https://manpages.ubuntu.com/manpages/jammy/man8/kdump-config.8.html),
[22.04 kdump-tools 설정](https://manpages.ubuntu.com/manpages/jammy/man5/kdump-tools.5.html)

현재 `nmi_watchdog=1`이지만 `hardlockup_panic`, `softlockup_panic`, `panic_on_oops`,
`panic`은 0이다. kdump와 장애 회수 경로 검증 전에 이를 일괄 변경하지 않는다.
lockup을 패닉으로 전환하는 정책은 가용성에 영향을 주므로 별도 검토·승인 항목이다.

## 5. 선택 구성: 외부 수집

- [ ] 사용자가 별도 수신 호스트·보관 위치·통신 범위를 선택한 경우에만 구성한다.
- [ ] 커널 메시지는 netpoll 지원 유선 Ethernet의 netconsole을 검토한다.
  의심 중인 USB 장치나 Wi-Fi를 기본 경로로 선택하지 않는다.
- [ ] 일반 시스템 로그 전달이 필요하면 rsyslog의 인증된 TCP/TLS 전송을 별도로 검토한다.
  일반 로그 전달은 userspace가 멈췄을 때의 커널 출력 경로를 대체하지 않는다.
- [ ] 수신기 방화벽과 송신 대상 제한, 시각 동기화, 접근권한을 설정하고
  무해한 식별 메시지의 송신·수신 시각을 대조한다.

netconsole은 UDP 기반이라 전달·기밀성을 보장하지 않는다. 신뢰할 수 있는 관리망에서만
사용하고 6.8 커널과 NIC에서 지원하는 옵션만 사용한다.
[Linux netconsole 문서](https://docs.kernel.org/networking/netconsole.html)

전원 단절·심각한 하드웨어 정지에서는 kdump와 외부 로그 모두 남지 않을 수 있다.
덤프 부재만으로 전원 문제를 확정하지 않는다. UPS 또는 사용 가능한 외부 전원 기록은
별도 보완 증거이며, 새 장비 구매·배선 변경은 이 계획에 포함하지 않는다.

## 다음 비정상 재부팅 시 회수 순서

1. **BERT부터 확인한다.** `host-bert-autocollect.service`가 설치돼 있으면 부팅 직후
   `~/host-incident-evidence/bert-boot-<시각>/`에 **이미 자동 수집돼 있다.** 먼저 그것을 본다.
   없으면 즉시 `bash scripts/host-bert-collect.sh`를 수동 실행한다(읽기 전용, sudo 비밀번호 필요).
   **BERT는 부팅마다 펌웨어가 새로 채우므로, 다음 재부팅이 일어나면 이번 사건의 레코드는
   사라진다.** 다른 무엇보다 먼저 확보한다.
   확보한 뒤 기존 레코드(`bert-20260912-2109`, `bert-20260921-121024`)와 대조해 섹션 구성·크기·
   유형이 같은지, 그리고 **어느 바이트가 다른지**까지 본다. 앞선 두 사건의 대조 기준은
   "구조 동일 + Payload #1 98.8% 일치"이며, 이에서 벗어나면 새로운 실패 모드일 수 있다.
2. 인지 시각·실제 관찰 증상·진행 중이던 작업을 기록한다. 로그를 먼저 지우지 않는다.
3. boot 목록으로 장애 직전 boot ID를 확정하고 전체 journal 및 현재 부팅의 커널 로그를 보관한다.
   정상·비정상 판정은 **PID 1의 종료 마커 유무**로 한다(재부팅 간격이나 사용자 세션
   systemd의 `Reached target Shutdown`을 근거로 쓰지 않는다).
4. `/var/crash`, `/var/lib/systemd/pstore`, `/sys/fs/pstore`, rasdaemon DB,
   해당 날짜와 전날의 atop 파일, 구성했다면 외부 수신 로그를 함께 보관한다.
5. BERT/MCE/EDAC/AER, panic/lockup/OOM, 저장장치·USB 오류를
   **시각을 맞춰** 비교한다. 키워드 검색 결과만 남기고 원본을 버리지 않는다.
6. 마이크로코드 리비전(`journalctl -k -b 0 | grep microcode`)과 BIOS/ME 버전을 기록한다.
   BIOS 업데이트 전후를 가르는 기준선이 된다.
7. 개인·인증 정보를 제외한 사건 요약과 수집 성공/실패 항목만 Issue에 추가한다.
   **BERT 원본 덤프는 물리 주소를 포함하므로 Issue·저장소에 올리지 않는다.**

## 검증·보관·롤백

- [ ] 적용 전후 설정 차이, 패키지·BIOS·커널 버전, 재부팅 시각과 검증 결과를 기록한다.
- [ ] 유지보수 재부팅 뒤 journal 테스트 로그가 남고 atop의 약 60초 간격이 유지된다.
- [ ] rasdaemon의 지원 이벤트·DB 상태, pstore 아카이브 설정, kdump 준비 상태가 확인된다.
- [ ] 강제 패닉 시험 미실시 여부와 외부 수집 선택/미선택을 명시한다.
- [ ] 적용 후 24시간 수집량·오버헤드와 48~72시간 재발 여부를 확인한다.
  이 기간 재발이 없더라도 원인 해결로 판정하지 않는다.
- [ ] 하루 한 번 저장 공간을 확인하고 32 GiB 미만이면 보관 정책을 재검토한다.
  덤프는 우선 최근 2건 보관을 목표로 하되, 사건 백업·삭제 승인 없이 과거 증거를 지우지 않는다.
- [ ] 설정이 부담을 주면 이번 작업의 journald drop-in만 제거/복원하고,
  atop은 백업한 간격으로 복원한다. 서비스 반영 시 보존 기간 삭제 동작을 다시 확인한다.
- [ ] kdump 철회 시 추가한 설정만 원복하고 GRUB/initramfs 및 다음 승인된 재부팅으로
  예약 RAM을 해제한다. 기존 덤프와 다른 서비스 설정은 삭제하지 않는다.

착수 시 각 단계를 따로 기록한다. BIOS와 수집 구성을 같은 유지보수에 바꾸면
재발 변화가 어느 변경 때문인지 단독으로 입증할 수 없다는 한계도 남긴다.
