#!/bin/bash
# wlan0 워치독 설치 — 지금 죽어있는 링크도 즉시 복구한다.
# 사용: sudo /home/jhw/ai/opencode/projects/personal-ops/scripts/wlan/wlan_watchdog_install.sh
# 제거: sudo systemctl disable --now wlan-watchdog && sudo rm /etc/systemd/system/wlan-watchdog.service

set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root 권한이 필요합니다: sudo $0" >&2
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/3  지금 죽어있는 wlan0 즉시 복구 =="
"$DIR/wlan_watchdog.sh" --once

echo
echo "== 2/3  systemd 서비스 설치 =="
# ExecStart 를 이 스크립트가 실제로 놓인 경로로 바꿔서 설치한다.
# 유닛 파일에 절대 경로를 박아 두면 저장소를 옮겼을 때 조용히 깨진다 —
# 실제로 그랬다. 저장소 사본은 /home/jhw/ai/opencode/scripts 를 가리킨 채였고
# 그 경로에는 파일이 없었다(설치본만 손으로 고쳐져 있었다). 그 상태로 이 스크립트를
# 다시 돌렸다면 동작 중인 워치독이 없는 파일을 가리키게 됐을 것이다.
# sed 대신 라인 치환을 쓰는 이유: 경로에 sed 구분자나 & 가 들어가도 깨지지 않는다.
UNIT_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP"' EXIT

while IFS= read -r line; do
    case "$line" in
        ExecStart=*) printf 'ExecStart=%s\n' "$DIR/wlan_watchdog.sh" ;;
        *)           printf '%s\n' "$line" ;;
    esac
done < "$DIR/wlan-watchdog.service" > "$UNIT_TMP"

if ! install -m 644 "$UNIT_TMP" /etc/systemd/system/wlan-watchdog.service; then
    echo "유닛 파일 설치 실패" >&2
    exit 1
fi

# 설치된 유닛이 실제로 실행 가능한 파일을 가리키는지 확인한다.
# 이 검사가 없으면 경로가 틀려도 systemctl enable 은 성공하고, 서비스는
# 시작 직후 status=203/EXEC 로 죽으면서 워치독만 조용히 사라진다.
INSTALLED_EXEC="$(sed -n 's/^ExecStart=//p' /etc/systemd/system/wlan-watchdog.service)"
if [ ! -x "$INSTALLED_EXEC" ]; then
    echo "설치된 ExecStart 가 실행 가능한 파일이 아니다: $INSTALLED_EXEC" >&2
    exit 1
fi
echo "   ExecStart=$INSTALLED_EXEC"

systemctl daemon-reload
systemctl enable --now wlan-watchdog.service
echo "   설치 + 시작 완료 (부팅 시 자동 시작)"

echo
echo "== 3/3  상태 확인 =="
sleep 2
systemctl is-active wlan-watchdog.service
ip -br addr show wlan0

echo
echo "로그 보기:  journalctl -u wlan-watchdog -f"
echo "중지:       sudo systemctl disable --now wlan-watchdog"
