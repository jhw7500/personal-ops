#!/bin/bash
# RTL8822CU (rtw88_8822cu) 절전 펌웨어 hang 근본 수정
#
# 증상: 유휴 상태 수십 분 후 dmesg에
#         rtw_8822cu: firmware failed to leave lps state
#       가 찍히고, 이후 모든 firmware 명령(tx report / scan density)이 실패하며
#       링크가 영구 사망. USB 재인식 전까지 복구되지 않음.
#
# 원인: 펌웨어가 Deep power-save 상태에서 깨어나지 못함.
#       (disable_lps_deep=N, mac80211 power_save=on 이면 이 경로가 열려 있음)
#
# 수정: 절전 진입 자체를 차단한다.
#         1) rtw88_core disable_lps_deep=1   -> Deep PS 비활성화
#         2) udev로 wlan* power_save off     -> mac80211 절전 비활성화
#
# 롤백: sudo ./wlan_fix_lps_rollback.sh

set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root 권한이 필요합니다:  sudo $0" >&2
    exit 1
fi

MODCONF=/etc/modprobe.d/rtw88-lps.conf
UDEVRULE=/etc/udev/rules.d/70-wifi-powersave-off.rules
IW=/usr/sbin/iw

echo "== 1/5  Deep LPS 비활성화 (모듈 파라미터) =="
cat > "$MODCONF" <<'EOF'
# RTL8822CU: 펌웨어가 deep power-save 해제에 실패해 hang 되는 문제 회피.
# dmesg "firmware failed to leave lps state" 이후 링크 영구 사망 -> 절전 진입 차단.
options rtw88_core disable_lps_deep=1
EOF
echo "   작성: $MODCONF"

echo "== 2/5  mac80211 power_save off (udev, 재부팅/재인식 후에도 유지) =="
cat > "$UDEVRULE" <<EOF
ACTION=="add", SUBSYSTEM=="net", KERNEL=="wlan*", RUN+="$IW dev \$name set power_save off"
EOF
udevadm control --reload-rules
echo "   작성: $UDEVRULE (룰 리로드 완료)"

echo "== 3/5  드라이버 재적재 (USB 버스 전체 리셋 없이) =="
modprobe -r rtw88_8822cu rtw88_8822c rtw88_usb rtw88_core 2>/dev/null
sleep 1
if lsmod | grep -q '^rtw88_core'; then
    echo "   ! 모듈 언로드 실패 -> 해당 USB 장치만 재인식으로 대체"
    echo 0 > /sys/bus/usb/devices/1-13.1/authorized
    sleep 2
    echo 1 > /sys/bus/usb/devices/1-13.1/authorized
else
    modprobe rtw88_8822cu
fi
sleep 4

echo "== 4/5  네트워크 재적용 =="
# 주의: 여기서 'netplan apply' 를 쓰면 안 된다.
#   이미 붙어 있는 wpa_supplicant를 재시작시켜 캐리어를 flap 시키고,
#   그 사이 systemd-networkd가 주소 설정에 실패해 링크를 'failed' 로 고정한다.
#   (networkd는 failed 상태에서 스스로 재시도하지 않음 -> IP 영구 미할당)
# 올바른 순서: 무선 association 이 안정된 뒤에 networkd 를 재설정한다.
echo -n "   association 대기 "
for i in $(seq 1 30); do
    if "$IW" dev wlan0 link 2>/dev/null | grep -q "^Connected"; then
        echo " -> 접속됨 (${i}s)"
        break
    fi
    echo -n "."
    sleep 1
done
sleep 2   # 캐리어 안정화
"$IW" dev wlan0 set power_save off 2>/dev/null
networkctl reconfigure wlan0
sleep 3

echo "== 5/5  적용 확인 =="
printf "   disable_lps_deep : %s  (Y 여야 정상)\n" \
    "$(cat /sys/module/rtw88_core/parameters/disable_lps_deep 2>/dev/null || echo '읽기실패')"
printf "   power_save       : %s\n" \
    "$("$IW" dev wlan0 get power_save 2>/dev/null | sed 's/.*: //' || echo '읽기실패')"
printf "   operstate        : %s\n" "$(cat /sys/class/net/wlan0/operstate 2>/dev/null)"
printf "   networkd state   : %s\n" "$(networkctl list --no-pager 2>/dev/null | awk '$2=="wlan0"{print $4" ("$5")"}')"
printf "   IP               : %s\n" "$(ip -br addr show wlan0 2>/dev/null | awk '{$1=$2=""; print $0}')"
echo "   link:"
"$IW" dev wlan0 link 2>/dev/null | sed 's/^/     /'

echo
echo "성공 기준:  disable_lps_deep=Y / power_save=off / operstate=up / IP에 192.168.0.2/24 표시"
echo "IP가 안 보이면:  sudo networkctl reconfigure wlan0"
