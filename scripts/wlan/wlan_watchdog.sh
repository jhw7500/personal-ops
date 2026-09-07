#!/bin/bash
# wlan0 자동 복구 워치독 — rtw88(RTL8822CU) 펌웨어 행 대응
#
# [이것은 근본 치료가 아니다 — 완화책이다]
#   근본 원인: 커널 rtw88 USB 드라이버/펌웨어가 접속 15~25분 뒤 응답 불능이 됨.
#     첫 증상:  dmesg "failed to get tx report from firmware"  (펌웨어 C2H 응답 중단)
#     그 뒤:    모든 firmware 명령 실패, 드라이버는 자가복구 없이 매달림.
#   상류 미해결 버그(lwfinger/rtw88 #377, #72 / linux-wireless). 설정으로 없앨 방법 없음.
#   -> 감지해서 해당 USB 장치만 재인식시켜 자동 복구한다.
#
# [핵심: '펌웨어 행' 과 'AP 부재' 를 구분한다]
#   링크가 끊겼다는 사실만으로 리셋하면, AP가 꺼져 있거나 범위 밖일 때
#   멀쩡한 동글을 무한히 두들기게 된다. 둘을 이렇게 구분한다:
#
#     펌웨어 행  : 커널이 rtw_8822cu 펌웨어 오류를 계속 뱉는다.
#                  ("failed to get tx report" / "failed to report density after scan"
#                   — 스캔 때마다 실패하므로 약 36초 주기로 무한 반복됨)
#     AP 부재    : wpa_supplicant 스캔은 정상 성공. 커널 오류가 '한 줄도' 없다.
#
#   -> 링크 끊김 + 최근 커널 펌웨어 오류  ==> 리셋
#      링크 끊김 + 커널 오류 없음         ==> 리셋하지 않고 대기 (AP 복귀를 기다림)
#      association 정상 + IP만 없음       ==> networkd가 링크를 'failed'로 고정한 상태.
#                                            USB 리셋 없이 reconfigure만으로 복구 시도
#
# 기존 wlan_reset.sh 와의 차이:
#   wlan_reset.sh 는 usb1 '버스 전체'를 deauthorize 한다 -> 같은 버스의
#   USB 저장장치(sdb, 500GB)와 키보드까지 끊긴다. 저장장치 쓰는 중이면 손상 위험.
#   이 스크립트는 VID:PID 로 무선 동글만 찾아 그것만 리셋한다.
#
# 사용:
#   sudo ./wlan_watchdog.sh --once     # 즉시 1회 복구하고 종료 (수동 리셋 대체)
#   sudo ./wlan_watchdog.sh            # 데몬 모드 (systemd 가 호출)

set -uo pipefail

IFACE=wlan0
VIDPID="0bda:c812"          # Realtek RTL8822CU
IW=/usr/sbin/iw

POLL=7                      # 상태 확인 주기(초)
FAIL_THRESHOLD=2            # 연속 N회 이상이면 판정 (~14초). 일시적 로밍과 구분
FW_ERROR_WINDOW=120         # 최근 N초 커널 로그에서 펌웨어 오류를 찾는다
                            #   (행 상태면 ~36초마다 찍히므로 120초면 반드시 잡힘)
MIN_RESET_INTERVAL=120      # 리셋 간 최소 간격(초) — 연타 방지
RECONF_MAX_TRIES=2          # association 정상·IP 미할당 시 reconfigure 시도 횟수
                            #   (이걸로 안 붙으면 USB 리셋으로 넘어간다)
RECOVER_TIMEOUT=45          # 복구 후 재접속 대기(초)
BACKOFF_MAX=300             # 복구 연속 실패 시 최대 대기(초)

# 최후의 안전망: 커널 오류가 안 보이는데도 이 시간(초) 넘게 계속 끊겨 있으면
# 한 번 리셋해 본다 (wpa_supplicant가 스캔조차 멈춘 희귀 상황 대비).
# AP가 장시간 꺼져 있는 경우에도 걸리지만, 동글 하나만 재인식하므로 피해는 없다.
LAST_RESORT_DOWN=1800

# 로그는 journal 과 일반 파일 양쪽에 남긴다.
# 파일에도 남기는 이유: journal 은 adm/systemd-journal 그룹이 아니면 못 읽어서
# 상태 확인 때마다 sudo 가 필요하다. 파일은 누구나 읽을 수 있게 해 둔다.
LOGFILE=/var/log/wlan-watchdog.log
LOG_MAX_BYTES=$((1024 * 1024))

log() {
    local msg="$(date '+%F %T') $*"
    echo "$msg"                                   # -> journal
    echo "$msg" >> "$LOGFILE" 2>/dev/null         # -> 파일
}

init_logfile() {
    # 1MB 넘으면 잘라낸다 (로그 무한 증식 방지)
    if [ -f "$LOGFILE" ] && [ "$(stat -c %s "$LOGFILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
        tail -n 500 "$LOGFILE" > "$LOGFILE.tmp" 2>/dev/null && mv "$LOGFILE.tmp" "$LOGFILE"
    fi
    touch "$LOGFILE" 2>/dev/null
    chmod 644 "$LOGFILE" 2>/dev/null
}

# 무선 동글의 USB 경로를 VID:PID 로 찾는다 (포트가 바뀌어도 따라간다)
find_usb_path() {
    local vid="${VIDPID%%:*}" pid="${VIDPID##*:}" d
    for d in /sys/bus/usb/devices/*/; do
        [ -f "$d/idVendor" ] || continue
        if [ "$(cat "$d/idVendor" 2>/dev/null)" = "$vid" ] &&
           [ "$(cat "$d/idProduct" 2>/dev/null)" = "$pid" ]; then
            basename "$d"
            return 0
        fi
    done
    return 1
}

link_is_up() {
    [ "$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null)" = "up" ] &&
    "$IW" dev "$IFACE" link 2>/dev/null | grep -q "^Connected"
}

has_ip() {
    ip -4 addr show "$IFACE" 2>/dev/null | grep -q "inet "
}

# 최근 FW_ERROR_WINDOW 초 안에 rtw88 펌웨어 오류가 찍혔는가?
# = 펌웨어가 굳었다는 결정적 증거. (AP 부재일 때는 이런 로그가 안 나온다)
firmware_wedged() {
    journalctl -k --since "-${FW_ERROR_WINDOW}s" --no-pager 2>/dev/null |
        grep -qE "rtw_8822cu.*(failed to get tx report|failed to report density|failed to send h2c|firmware failed|timed out to flush)"
}

recover() {
    local path i
    if ! path=$(find_usb_path); then
        log "복구 불가: USB 트리에 $VIDPID 없음 (동글이 물리적으로 빠졌나?)"
        return 1
    fi

    log "복구 시작 — $path ($VIDPID) 만 타겟 재인식 (USB 버스 전체 아님)"
    echo 0 > "/sys/bus/usb/devices/$path/authorized" 2>/dev/null
    sleep 2
    echo 1 > "/sys/bus/usb/devices/$path/authorized" 2>/dev/null

    for ((i = 1; i <= RECOVER_TIMEOUT; i++)); do
        link_is_up && { log "재접속 완료 (${i}초)"; break; }
        sleep 1
    done

    if ! link_is_up; then
        log "복구 미완: ${RECOVER_TIMEOUT}초 안에 재접속 못 함 (AP가 없는 상태일 수도 있음)"
        return 1
    fi

    # 중요: 여기서 'netplan apply' 를 쓰면 안 된다. wpa_supplicant 를 재시작시켜
    # 캐리어를 flap 시키고, systemd-networkd 가 링크를 'failed' 로 고정해
    # 정적 IP(192.168.0.2)가 영영 안 붙는다. association 이 끝난 뒤 reconfigure 한다.
    sleep 2
    "$IW" dev "$IFACE" set power_save off 2>/dev/null
    networkctl reconfigure "$IFACE" >/dev/null 2>&1
    sleep 3

    if ! has_ip; then
        log "링크는 붙었으나 IP 미할당 — reconfigure 재시도"
        networkctl reconfigure "$IFACE" >/dev/null 2>&1
        sleep 3
    fi

    if has_ip; then
        log "복구 성공 — IP: $(ip -4 -br addr show "$IFACE" | awk '{print $3}')"
        return 0
    fi
    log "복구 실패: IP 미할당"
    return 1
}

if [ "$(id -u)" -ne 0 ]; then
    echo "root 권한이 필요합니다: sudo $0 $*" >&2
    exit 1
fi

init_logfile

if [ "${1:-}" = "--once" ]; then
    recover
    exit $?
fi

log "워치독 시작 — ${POLL}초 주기 / 펌웨어 오류가 확인될 때만 리셋 (AP 부재 시엔 대기)"

# 자가 점검 — firmware_wedged() 는 journalctl -k 로 커널 로그를 읽어서 판정한다.
# 이게 서비스 컨텍스트에서 동작하지 않으면 펌웨어 행을 '영영 감지하지 못한 채'
# 조용히 무력화된다. 그 상태를 모르고 지나가지 않도록 시작 시 반드시 확인한다.
# 주의: 권한이 없으면 journalctl 은 '내용 없음'이 아니라 안내 문구를 출력한다.
# 따라서 [ -n "$(...)" ] 로는 검출되지 않는다. 실제 커널 로그 줄('kernel:')을 찾아야 한다.
if journalctl -k -n 1 --no-pager 2>/dev/null | grep -q "kernel:"; then
    log "자가점검 OK — 커널 저널 읽기 가능. 펌웨어 행 감지 정상 동작."
else
    log "!! 자가점검 실패 — 커널 저널(journalctl -k)을 읽지 못함."
    log "!! 펌웨어 행 감지가 동작하지 않는다. ${LAST_RESORT_DOWN}초 최후 안전망으로만 복구됨."
    log "!! 확인:  sudo journalctl -k -n 1"
fi

# 복구 경로에 필요한 것들이 실제로 있는지도 확인 (없으면 장애 때 조용히 실패한다)
[ -x "$IW" ]                  || log "!! $IW 없음 — 링크 판정/복구 불가"
command -v networkctl >/dev/null || log "!! networkctl 없음 — 복구 후 IP 할당 불가"
find_usb_path >/dev/null      || log "!! USB 트리에 $VIDPID 없음 — 복구 대상 장치를 못 찾음"

fails=0
backoff=0
recover_count=0
last_reset=0
down_since=0
ap_absent_logged=0
reconf_tries=0
last_beat=$(date +%s)

while true; do
    now=$(date +%s)

    # 1시간마다 생존 신호 — 조용한 게 '죽은 것'인지 '정상인 것'인지 구분되게 한다
    if [ $(( now - last_beat )) -ge 3600 ]; then
        log "정상 가동 중 — 누적 복구 ${recover_count}회 / 현재 IP: $(ip -4 -br addr show "$IFACE" 2>/dev/null | awk '{print $3}')"
        last_beat=$now
    fi

    if link_is_up && has_ip; then
        if [ "$down_since" -ne 0 ]; then
            log "정상 복귀 — IP: $(ip -4 -br addr show "$IFACE" | awk '{print $3}')"
        fi
        fails=0; backoff=0; down_since=0; ap_absent_logged=0; reconf_tries=0
        sleep "$POLL"
        continue
    fi

    # --- 여기부터: 링크가 정상이 아님 ---
    [ "$down_since" -eq 0 ] && down_since=$now
    down_for=$(( now - down_since ))
    fails=$(( fails + 1 ))

    if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
        sleep "$POLL"
        continue
    fi

    # association은 됐는데 IP만 없는 경우 — networkd가 링크를 'failed'로 고정한 상태
    # (recover() 안의 netplan apply 금지 주석 참고). 동글은 멀쩡하므로 USB 리셋 없이
    # reconfigure만으로 붙는다. 예전엔 이걸 'AP 부재'로 오분류해 최후 안전망(30분)까지
    # 기다렸다 — 로그의 '최후 안전망' 리셋 대부분이 실제로는 이 케이스였음.
    if link_is_up && ! has_ip && [ "$reconf_tries" -lt "$RECONF_MAX_TRIES" ]; then
        reconf_tries=$(( reconf_tries + 1 ))
        log "association 정상·IP 미할당 — networkctl reconfigure 시도 (${reconf_tries}/${RECONF_MAX_TRIES})"
        networkctl reconfigure "$IFACE" >/dev/null 2>&1
        sleep 5
        if has_ip; then
            log "reconfigure 복구 성공 — IP: $(ip -4 -br addr show "$IFACE" | awk '{print $3}')"
            fails=0; backoff=0; down_since=0; ap_absent_logged=0; reconf_tries=0
        fi
        sleep "$POLL"
        continue
    fi

    # 펌웨어 행인가, 아니면 그냥 AP가 없는 것인가?
    if firmware_wedged; then
        reason="펌웨어 행 확인 (커널 rtw_8822cu 오류 검출)"
    elif link_is_up && ! has_ip; then
        reason="association 정상·IP 미할당 — reconfigure ${RECONF_MAX_TRIES}회로 복구 안 됨"
    elif [ "$down_for" -ge "$LAST_RESORT_DOWN" ]; then
        reason="최후 안전망 — 커널 오류는 없지만 ${down_for}초째 끊김"
    else
        # 커널 오류 없음 = 동글은 멀쩡. AP가 없거나 범위 밖. 리셋하지 않는다.
        if [ "$ap_absent_logged" -eq 0 ]; then
            log "링크 끊김이지만 커널 펌웨어 오류 없음 -> 동글은 정상. AP 부재로 판단, 리셋 안 함 (재접속 대기)"
            ap_absent_logged=1
        fi
        sleep "$POLL"
        continue
    fi

    # 리셋 연타 방지
    if [ $(( now - last_reset )) -lt "$MIN_RESET_INTERVAL" ]; then
        sleep "$POLL"
        continue
    fi

    recover_count=$(( recover_count + 1 ))
    log "복구 트리거 [#${recover_count}] — $reason (끊긴 지 ${down_for}초)"
    last_reset=$now

    if recover; then
        fails=0; backoff=0; down_since=0; ap_absent_logged=0; reconf_tries=0
    else
        backoff=$(( backoff == 0 ? 30 : backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff=$BACKOFF_MAX
        log "복구 실패 — ${backoff}초 대기 후 재평가"
        sleep "$backoff"
        fails=0
    fi

    sleep "$POLL"
done
