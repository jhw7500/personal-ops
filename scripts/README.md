# scripts

호스트 운영용 셸 유틸. 다른 모듈과 달리 **크론으로 돌지 않는다** — 사람이 필요할 때 직접
실행하거나(진단·설정), systemd 서비스로 상주한다(워치독).

전부 `sudo` 가 필요하고 **실기 상태를 바꾼다.** 읽고 나서 실행해라.

## 구성

| 경로 | 역할 | 실행 방식 |
|---|---|---|
| `user-resource-limit.sh` | systemd user slice 로 특정 계정의 CPU·메모리·스왑·디스크 IOPS 를 퍼센트로 제한 | 수동 (`apply`/`plan`/`status`/`remove`) |
| `wlan/wlan_watchdog.sh` | wlan0 자동 복구 워치독 (rtw88 펌웨어 행 대응) | systemd 상주 |
| `wlan/wlan_watchdog_install.sh` | 위 워치독 설치 + 지금 죽어 있는 링크 즉시 복구 | 수동 1회 |
| `wlan/wlan-watchdog.service` | 워치독 유닛 파일 | install 스크립트가 배포 |
| `wlan/wlan_diag.sh` · `wlan_diag2.sh` | wlan0 진단 덤프 (`/tmp/wlan_diag*.txt`) | 수동 |
| `wlan/wlan_fix_lps.sh` · `wlan_fix_lps_rollback.sh` | RTL8822CU 절전(LPS) 펌웨어 hang 근본 수정과 롤백 | 수동 |
| `wlan/wlan_reset.sh` | wlan0 인터페이스 재기동 | 수동 |

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
