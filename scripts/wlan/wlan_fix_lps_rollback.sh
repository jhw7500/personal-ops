#!/bin/bash
# wlan_fix_lps.sh 롤백 — 절전 설정을 커널 기본값으로 되돌린다.

set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root 권한이 필요합니다:  sudo $0" >&2
    exit 1
fi

rm -fv /etc/modprobe.d/rtw88-lps.conf
rm -fv /etc/udev/rules.d/70-wifi-powersave-off.rules
udevadm control --reload-rules

modprobe -r rtw88_8822cu rtw88_8822c rtw88_usb rtw88_core 2>/dev/null
sleep 1
modprobe rtw88_8822cu

# netplan apply 금지 — wpa_supplicant를 재시작시켜 캐리어 flap을 만들고
# networkd가 링크를 'failed'로 고정해 IP가 안 붙는다. association 후 reconfigure.
for i in $(seq 1 30); do
    /usr/sbin/iw dev wlan0 link 2>/dev/null | grep -q "^Connected" && break
    sleep 1
done
sleep 2
networkctl reconfigure wlan0
sleep 3

echo "롤백 완료. disable_lps_deep = $(cat /sys/module/rtw88_core/parameters/disable_lps_deep 2>/dev/null)  (N 이면 기본값 복귀)"
ip -br addr show wlan0
