# scripts

호스트 운영용 셸 유틸. 다른 모듈과 달리 **크론으로 돌지 않는다** — 사람이 필요할 때 직접
실행하거나(진단·설정), systemd 서비스로 상주한다(워치독).

리소스 제한·무선 복구 도구는 대체로 `sudo`가 필요하고 **실기 상태를 바꾼다.**
SSH 이슈 알림은 일반 사용자로 상태만 조회한다. 도구별 영향을 읽고 실행해라.

## 구성

| 경로 | 역할 | 실행 방식 |
|---|---|---|
| `host-bert-collect.sh` · `host-bert-decode.py` | 비정상 재부팅 후 ACPI BERT 하드웨어 오류 레코드 수집·판독 | 수동, 읽기 전용 (`sudo` 읽기 1회) |
| `host-bert-autocollect.sh` · `host-bert-autocollect.service` | 부팅 시 BERT 레코드가 있을 때만 자동 수집 (정상 부팅은 건너뜀) | systemd oneshot, 부팅마다 |
| `host-power-limit.sh` · `host-power-limit.service` | 부팅마다 RAPL PL1 150 W / PL2 241 W 적용 + 되읽기 검증 (#57 전력 가설 실험, 2026-10-19~). 값은 유닛 `Environment=`로 변경 | systemd oneshot, 부팅마다 |
| `host-power-telemetry.sh` · `.service` · `.timer` | 1분마다 패키지 온도·스로틀 누적·현재 PL을 journal(`-t host-power-telemetry`)에 기록. 재발 시 `journalctl -b -1 -t host-power-telemetry` | systemd timer, 1분 |
| `host-bios-preflight.sh` | BIOS/ME 업데이트 착수 전 준비 상태 GO/NO-GO 점검 | 수동, 읽기 전용 |
| `host-bios-postcheck.sh` | BIOS/ME 업데이트 직후 기준선 대조 검증 | 수동, 읽기 전용 |
| `ssh-issue-reminder.py` · `ssh-issue-reminder-profile.sh` | 다음 재부팅 이후 매 대화형 SSH 로그인에 미완료 GitHub 이슈 표시 | 사용자 `.profile`, 읽기 전용 조회 |
| `user-resource-limit.sh` | systemd user slice 로 특정 계정의 CPU·메모리·스왑·디스크 IOPS 를 퍼센트로 제한 | 수동 (`apply`/`plan`/`status`/`remove`) |
| `wlan/wlan_watchdog.sh` | wlan0 자동 복구 워치독 (rtw88 펌웨어 행 대응) | systemd 상주 |
| `wlan/wlan_watchdog_install.sh` | 위 워치독 설치 + 지금 죽어 있는 링크 즉시 복구 | 수동 1회 |
| `wlan/wlan-watchdog.service` | 워치독 유닛 파일 | install 스크립트가 배포 |
| `wlan/wlan_diag.sh` · `wlan_diag2.sh` | wlan0 진단 덤프 (`/tmp/wlan_diag*.txt`) | 수동 |
| `wlan/wlan_fix_lps.sh` · `wlan_fix_lps_rollback.sh` | RTL8822CU 절전(LPS) 펌웨어 hang 근본 수정과 롤백 | 수동 |
| `wlan/wlan_reset.sh` | wlan0 인터페이스 재기동 | 수동 |

## BIOS/ME 업데이트 점검

[#56](https://github.com/jhw7500/personal-ops/issues/56) 절차용. 둘 다 **읽기 전용**이며
아무것도 바꾸지 않는다. 상세 절차는 `docs/host-bios-me-update.md`에 있다.

```bash
# 착수 전 — 준비 상태 GO/NO-GO
bash scripts/host-bios-preflight.sh

# 업데이트 직후 — 기준선 대조
bash scripts/host-bios-postcheck.sh
```

기본 작업 디렉터리는 저장소 밖 `~/host-bios-update-prep/`이고 `PREP_DIR` 환경변수로
바꿀 수 있다. 구조는 다음과 같다.

| 경로 | 내용 |
| --- | --- |
| `baseline/` | 업데이트 전 캡처 (펌웨어 상태, `efibootmgr -v`, lsblk, lspci, 부팅 목록) |
| `bios/` | BIOS ZIP 과 추출한 CAP, 복구용 `PZ690P.CAP` |
| `bios/rollback/` | 롤백용 이전 버전 CAP |
| `after-<타임스탬프>/` | postcheck 이 남기는 사후 기록 |

**판정 규칙 두 가지를 알고 읽어라.**

- **확인불가(`[ ? ]`)는 통과가 아니다.** 스크립트는 측정하지 못한 항목을 통과로 처리하지
  않고 따로 센다. UPS 연결 여부처럼 소프트웨어로 볼 수 없는 것은 사람이 확인해야 한다.
- **BIOS 갱신 여부의 지표는 `Updated early from:` 값이다.** `/proc/cpuinfo`의 마이크로코드는
  OS(`intel-microcode`)가 부팅마다 덮어쓰므로 업데이트 전후가 같을 수 있다. BIOS가 공급하는
  값이 바뀌었는지를 봐야 한다.

preflight 는 `#57`의 중단 조건도 함께 본다 — 직전 부팅이 **PID 1 종료 마커** 기준으로
정상 종료였는지, 현재 부팅에 BERT 오류 레코드가 있는지, 마지막 비정상 종료로부터 몇 시간이
지났는지. 경과 시간만으로는 안정을 입증하지 못한다는 점도 함께 출력한다.

## BERT 하드웨어 오류 판독

비정상 재부팅 뒤 펌웨어가 ACPI BERT(Boot Error Record Table)에 남긴 오류 레코드를
수집·판독한다. [#57](https://github.com/jhw7500/personal-ops/issues/57) 조사용이며
상세 맥락은 `docs/host-reboot-debugging-plan.md`에 있다.

```bash
# 수집 + 판독 (sudo 비밀번호 1회)
bash scripts/host-bert-collect.sh

# 이미 받아 둔 덤프만 다시 판독 (sudo 불필요)
python3 scripts/host-bert-decode.py <bert-region.bin>
```

**읽기 전용이다.** `/sys/firmware/acpi/tables/data/BERT`는 예약 물리 메모리를 매핑해
보여주는 경로이고, 읽어도 레코드가 지워지지 않는다.

기본 출력은 **저장소 밖** `~/host-incident-evidence/bert-<타임스탬프>/`이며 디렉터리는
0700, 파일은 0600이다. 첫 인자로 다른 경로를 줄 수 있다.
**원본 덤프는 물리 주소를 포함하므로 저장소나 Issue에 올리지 않는다.**

읽을 때 주의할 점 두 가지:

- 커널이 `Skipped N error records`라고 해도 레코드가 손상·미지원인 것이 아니다.
  영역이 `ACPI_BERT_PRINT_MAX_LEN`(1024바이트)을 넘으면 콘솔 출력 대신
  `/sys/firmware/acpi/tables/data/BERT`로 원본을 노출한다.
  `/sys/firmware/acpi/tables/BERT`(48바이트)는 포인터일 뿐이므로 그쪽을 읽으면 안 된다.
- 판독 결과의 `block_status`가 0인 것은 커널이 부팅 시 읽고 지운 흔적이지
  레코드 부재가 아니다. 이 값으로 순회를 중단하면 "레코드 없음"으로 오판한다.
  판독기는 `data_length` 기준으로 순회한다.

**BERT는 부팅마다 펌웨어가 새로 채운다. 다음 재부팅이 일어나면 직전 사건의 레코드는
사라지므로, 사건 인지 후 가장 먼저 수집한다.**

판독기는 Platform Memory(DIMM 위치·ECC 유형), IA32/X64 Processor(APIC·CPUID·
Cache/TLB/Bus 오류), PCI Express(BDF·vendor:device) 섹션을 해석하고,
그 외 섹션은 원시 덤프로 출력한다. CPER v3.0 타임스탬프가 있으면 함께 표시한다.

## SSH 이슈 알림

`.profile`에서 `ssh-issue-reminder-profile.sh`를 source한다. Bash의 대화형 로그인,
SSH_CONNECTION/SSH_TTY, 실제 입출력 TTY가 모두 있어야 실행한다.
일반 로컬 셸, SCP/SFTP와 비대화형 SSH 명령에는 출력하지 않는다.
저장소 경로를 옮기면 `.profile`의 source 경로도 갱신해야 한다.
`.bash_profile` 또는 `.bash_login`을 새로 만들 때에도 `.profile`을 읽도록 유지한다.

설정 파일은 사용자 홈의 `.config/personal-ops/ssh-issue-reminder.json`이다.
설정 디렉터리는 0700, 파일은 0600으로 관리한다.

- enabled: false이면 조회와 표시를 중지한다.
- activate_after_boot: 설치 당시 boot ID. 이 부팅에는 알리지 않고 다음 부팅부터 접속마다 조회한다.
- issues: repo, 양의 정수 number, 제어문자 없는 짧은 label 목록이다.

2026-09-12 설정 대상:

| Repository | Issue | Purpose |
| --- | --- | --- |
| jhw7500/personal-ops | [#57](https://github.com/jhw7500/personal-ops/issues/57) | Reboot diagnostics |
| jhw7500/jhw-notion | [#153](https://github.com/jhw7500/jhw-notion/issues/153) | Safe Notion deployment |

매 접속마다 GitHub CLI로 상태를 새로 읽는다. CLOSED만 숨기며, 재개된 이슈는 다시 표시한다.
인증 실패, 시간 초과, 잘못된 응답은 완료가 아니라 '상태 확인 불가'로 표시한다.
API 응답이나 인증 오류 원문은 출력하지 않는다.
[GitHub CLI 조회 문서](https://cli.github.com/manual/gh_issue_view)

이슈별 조회 제한은 3초이며 병렬로 실행한다. 셸 훅은 전체 실행을 5초로 제한하고,
필요하면 1초 뒤 강제 종료한다. 장애 시 프롬프트가 최대 약 6초 늦어질 수 있다.
토큰을 설정 파일에 복사하지 않고 기존 gh 인증을 사용한다.
재부팅 후 SSH 환경에서 gh 인증이 불가능하면 상태 확인 불가가 표시될 수 있다.
외부 상태 변경, Task/Claim 조작, 실제 배포, 패키지 설치나 재부팅은 하지 않는다.

현재 부팅에서 실제 이슈 상태를 미리 확인하는 명령:

    rtk proxy python3 scripts/ssh-issue-reminder.py --preview

검증 명령:

    rtk proxy python3 -B tests/test-ssh-issue-reminder.py
    rtk proxy bash -n scripts/ssh-issue-reminder-profile.sh
    rtk proxy shellcheck scripts/ssh-issue-reminder-profile.sh

해제하려면 설정의 enabled를 false로 변경한다. 로그인 연결까지 제거하려면
`.profile`의 personal-ops:ssh-issue-reminder 시작/끝 마커 사이 블록만 제거한다.
다른 로그인 설정이나 이슈 자체는 삭제하지 않는다.

## user-resource-limit.sh

```bash
./user-resource-limit.sh plan   devuser 50     # 적용될 값만 계산해 보여준다 (root 불필요)
sudo ./user-resource-limit.sh apply  devuser 50
./user-resource-limit.sh status devuser
sudo ./user-resource-limit.sh remove devuser
```

`PERCENT` 는 1~100 정수이고 CPU·물리 메모리·스왑·디스크 IOPS 기준선에 대한 그 계정의 몫이다.
`root` 와 `jhw` 는 보호 대상이라 제한할 수 없다.

`/etc/systemd/system/user-<uid>.slice.d/60-user-resource-limit.conf` 를 쓰고 `systemctl` 로
반영한다. 테스트를 위해 `RESOURCE_LIMIT_SYSTEMD_ROOT` · `RESOURCE_LIMIT_SYSTEMCTL` ·
`RESOURCE_LIMIT_DEVICE` · `RESOURCE_LIMIT_BASE_IOPS` 로 대상 경로와 명령을 바꿀 수 있다.

## wlan 워치독

RTL8822CU(`rtw88_8822cu`) 가 접속 15~25분 뒤 펌웨어 응답 불능이 되는 문제의 **완화책**이다.
근본 치료가 아니라는 점이 `wlan_watchdog.sh` 헤더에 적혀 있다.

```bash
sudo ./wlan/wlan_watchdog_install.sh          # 설치 + 즉시 복구
journalctl -u wlan-watchdog -f                # 로그
sudo systemctl disable --now wlan-watchdog    # 중지
```

### 유닛 파일의 경로는 설치 시점에 정해진다

`wlan-watchdog.service` 의 `ExecStart` 는 저장소 기준 경로를 적어 두지만, install 스크립트가
**자기가 놓인 실제 경로로 치환해서** `/etc/systemd/system/` 에 배포하고, 설치된 경로가 실행
가능한 파일인지 확인한 뒤에만 `systemctl enable` 로 넘어간다.

유닛 파일에 절대 경로를 박아 두면 저장소를 옮겼을 때 조용히 깨지기 때문이다. 실제로 한 번
그랬다 — 스크립트는 `projects/personal-ops/scripts/wlan/` 로 옮겨졌는데 저장소의 유닛 파일은
옛 경로를 가리킨 채였고, 설치본만 손으로 고쳐져 있었다. 그 상태로 install 을 다시 돌렸다면
동작 중이던 워치독이 없는 파일을 가리키게 됐을 것이다. 경로가 틀려도 `systemctl enable` 은
성공하고 서비스만 `status=203/EXEC` 로 죽으므로, 설치 후 검사가 없으면 조용히 사라진다.

## 진단·수정 스크립트

```bash
sudo ./wlan/wlan_diag.sh      # 기본 덤프  → /tmp/wlan_diag.txt
sudo ./wlan/wlan_diag2.sh     # 2차 덤프 (USB/xhci/mac80211 포함) → /tmp/wlan_diag2.txt
sudo ./wlan/wlan_fix_lps.sh   # 절전 비활성 (롤백: wlan_fix_lps_rollback.sh)
```

`wlan_diag.sh` 는 netplan 설정을 덤프할 때 `password`·`psk` 를 `***REDACTED***` 로 가린다.
덤프 파일을 공유하기 전에 나머지 내용도 한 번 훑어라.
