#!/bin/bash
# 2차 진단 — "절전 껐는데도 죽었다" 이후, 실패의 '첫 커널 메시지'를 잡는다.
# 이번엔 필터를 최소화해서(rtw만 grep 하지 않고) USB/xhci/mac80211 까지 통째로 본다.
#
# 사용: sudo /home/jhw/ai/opencode/projects/personal-ops/scripts/wlan/wlan_diag2.sh
# 결과: /tmp/wlan_diag2.txt

OUT=/tmp/wlan_diag2.txt
exec > "$OUT" 2>&1

echo "########## 1. dmesg — 드라이버 재적재(17:26) 이후 전체, 필터 없음 ##########"
echo "#  찾는 것: 17:49~17:50 사이의 '첫' 에러가 무엇인가?"
echo "#    - 'failed to get tx report' / h2c  -> 펌웨어 통신 사망"
echo "#    - 'USB write, ret=-110/-71'        -> USB 전송 실패"
echo "#    - 'Connection to AP ... lost' / beacon -> 무선 링크 상실"
echo "#    - 'xhci' / 'reset ... USB device'  -> USB 컨트롤러/전원"
echo
dmesg -T | tail -250

echo; echo "########## 2. 커널 저널 — 17:45 이후 (시간 정밀) ##########"
journalctl -k --no-pager --since "17:45" 2>&1 | tail -80

echo; echo "########## 3. wpa_supplicant — 17:26 이후 (끊김 사유 코드) ##########"
journalctl -u netplan-wpa-wlan0 --no-pager --since "17:26" 2>&1 | tail -50

echo; echo "########## 4. systemd-networkd — 17:45 이후 ##########"
journalctl -u systemd-networkd --no-pager --since "17:45" 2>&1 | tail -30

echo; echo "########## 5. 펌웨어/드라이버 버전 ##########"
modinfo rtw88_8822c 2>/dev/null | grep -E "^firmware|^version|^vermagic"
ls -l /lib/firmware/rtw88/rtw8822c_fw.bin /lib/firmware/rtw88/rtw8822c_wow_fw.bin 2>&1

echo; echo "########## 6. 현재 USB/링크 상태 ##########"
lsusb -t 2>&1 | grep -A1 -B1 rtw
cat /sys/module/rtw88_core/parameters/disable_lps_deep 2>&1
iw dev wlan0 link 2>&1
uptime

chmod 644 "$OUT"
echo "DONE"
