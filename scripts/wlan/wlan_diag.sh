#!/bin/bash
# wlan0 진단 덤프 — 결과를 /tmp/wlan_diag.txt 에 저장 (읽기 가능하게)
# 사용: sudo /home/jhw/ai/opencode/projects/personal-ops/scripts/wlan/wlan_diag.sh

OUT=/tmp/wlan_diag.txt
exec > "$OUT" 2>&1

echo "########## 1. netplan wlan0 설정 (원본) ##########"
cat /etc/netplan/wlan0.yaml 2>&1 | sed -E 's/(password|psk).*/\1: ***REDACTED***/I'

echo; echo "########## 2. netplan이 생성한 networkd 설정 ##########"
cat /run/systemd/network/10-netplan-wlan0.network 2>&1

echo; echo "########## 3. systemd-networkd 저널 (wlan0 관련, 최근 80줄) ##########"
journalctl -u systemd-networkd --no-pager -n 200 2>&1 | grep -iE "wlan0|DHCP|fail|error|warn" | tail -80

echo; echo "########## 4. wpa_supplicant 저널 (최근 40줄) ##########"
journalctl -u netplan-wpa-wlan0 --no-pager -n 40 2>&1 | tail -40

echo; echo "########## 5. dmesg: rtw / wlan0 (최근 60줄) — LPS 에러 재발 여부 ##########"
dmesg -T 2>&1 | grep -iE "rtw|wlan0|usb 1-13" | tail -60

echo; echo "########## 6. networkctl status wlan0 ##########"
networkctl status wlan0 --no-pager 2>&1

echo; echo "########## 7. DHCP 수동 시도 (networkd와 별개로 서버 응답 확인) ##########"
if command -v dhclient >/dev/null; then
    timeout 20 dhclient -v -1 wlan0 2>&1 | tail -20
    echo "--- 시도 후 주소 ---"
    ip -br addr show wlan0
    # 수동 dhclient가 잡은 주소는 정리 (networkd와 충돌 방지)
    dhclient -r wlan0 2>/dev/null
else
    echo "dhclient 없음 -> 건너뜀"
fi

echo; echo "########## 8. 기존 리스 파일 ##########"
ls -l /run/systemd/netif/leases/ 2>&1
for f in /run/systemd/netif/leases/*; do echo "### $f"; cat "$f"; done 2>/dev/null

chmod 644 "$OUT"
echo "DONE"
