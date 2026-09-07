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

# --- 도달성 프로브 (무증상 행 감지) ---
# 실측 2026-09-07: 데이터 경로가 죽은 뒤 커널이 rtw88 오류를 뱉기까지 17분이 걸렸다.
#   11:18:14 데이터 사망 -> (17분 무반응) -> 11:35:20 "펌웨어 행 확인" -> 11:35:35 복구.
# 그 17분 동안 operstate=up · iw Connected · inet 존재가 모두 참이라 아래 판정은 전부
# '정상'이었고, 최후 안전망(LAST_RESORT_DOWN)도 "계속 끊겨 있으면"이 조건이라 걸리지 않았다.
# 링크 상태가 아니라 '프레임이 실제로 오가는가'를 봐야 이 구간을 덮는다.
PROBE_TARGET=""             # 비우면 IFACE 서브넷의 .1 을 자동 사용
PROBE_FAIL_THRESHOLD=4      # 연속 N회 실패해야 판정(POLL=7 -> 약 28초). 일시 혼잡과 구분
PROBE_TIMEOUT=2             # 프로브 1회 대기(초)
PROBE_VALIDATE_TRIES=5      # 자가검증 재시도 — 1회라도 성공하면 활성. 런타임 임계
                            #   (PROBE_FAIL_THRESHOLD)보다 크게 둔다. 검증이 더 엄격하면
                            #   일시 손실 한 번으로 기능이 통째로 죽는다.
PROBE_REVALIDATE_AFTER=300  # 검증 실패는 '비활성'이 아니라 '보류'다. 이 시간 뒤 다시 시도한다.
                            #   영구 비활성으로 두면, 하필 무증상 행 상태에서 데몬을 재시작한
                            #   경우처럼(재시작하는 시점이 대개 문제 있을 때다) 기능이 영영 죽는다.
probe_ok=0                  # 검증 통과 여부. 0 이면 프로브를 판정에 쓰지 않는다(종전 동작).
probe_next_validate=0       # 다음 검증 시도 가능 시각(epoch)
probe_target_seen=""        # 검증에 성공한 대상. 주소·서브넷이 바뀌면 재검증한다.
probe_reset_armed=1         # 프로브 사유 USB 리셋 허용. 리셋했는데 도달성이 안 돌아오면 내려서
                            #   반복 리셋을 막고, 도달성이 회복되면 다시 올린다.
                            #   내리는 시점은 '리셋을 실제로 실행할 때'다 — rate-limit 에 걸려
                            #   실행되지 않은 판정으로 arm 을 소비하면 안 된다.
probe_disarm_logged=0       # disarm 관망 로그는 1회만
probe_reason=0              # 이번 틱의 리셋 사유가 프로브인가(arm 소비 대상 판별)

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

# 프로브 대상 — 명시값이 없으면 IFACE 서브넷의 .1 (라우터/AP 관례).
# 이 인터페이스에는 default 라우트가 없어서(default 는 유선으로 나간다) 게이트웨이를
# 라우팅 테이블에서 얻을 수 없다. 그래서 서브넷 관례를 쓰고, 틀린 환경이면 아래
# 자가점검이 프로브를 통째로 끈다.
probe_target() {
    if [ -n "$PROBE_TARGET" ]; then echo "$PROBE_TARGET"; return 0; fi
    local cidr addr
    cidr=$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)
    [ -n "$cidr" ] || return 1
    addr=${cidr%/*}
    echo "${addr%.*}.1"
}

# 링크가 붙어 있어도 프레임이 실제로 오가는가.
# 이 호스트에 arping 이 없어 ICMP 로 본다 — 대상이 ICMP 를 막는 환경이면 자가점검이 걸러낸다.
reachable() {
    local t
    t=$(probe_target) || return 0        # 대상을 정할 수 없으면 판정 보류(정상 취급)
    ping -I "$IFACE" -c 1 -W "$PROBE_TIMEOUT" -n -q "$t" >/dev/null 2>&1
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

# 도달성 프로브 자가검증은 여기서 하지 않는다 — 시작 시점에는 부팅 중이라 IP 가 아직 없을 수
# 있고(유닛은 network.target 뒤에 올 뿐 IP 할당을 보장하지 않는다), 그 상태를 '대상 없음'으로
# 읽으면 프로브가 데몬 수명 내내 꺼진 채로 남는다. 검증은 메인 루프에서 링크·IP 가 처음
# 정상이 된 시점에, 재시도와 함께 1회 수행한다.
log "도달성 프로브 — 링크·IP 가 정상이 되는 시점에 대상 도달성을 확인한다(무응답이면 ${PROBE_REVALIDATE_AFTER}초 보류 후 재시도)"

fails=0
backoff=0
recover_count=0
last_reset=0
down_since=0
ap_absent_logged=0
reconf_tries=0
probe_fails=0
last_beat=$(date +%s)

while true; do
    now=$(date +%s)

    # 1시간마다 생존 신호 — 조용한 게 '죽은 것'인지 '정상인 것'인지 구분되게 한다
    if [ $(( now - last_beat )) -ge 3600 ]; then
        log "정상 가동 중 — 누적 복구 ${recover_count}회 / 현재 IP: $(ip -4 -br addr show "$IFACE" 2>/dev/null | awk '{print $3}')"
        last_beat=$now
    fi

    # 링크·IP 가 정상이어도 실제 도달성이 없으면 '무증상 행'이다(상단 PROBE 주석 참고).
    # 임계 미만이면 아직 정상으로 취급한다 — 일시 혼잡·로밍으로 한두 번 빠지는 것과 구분.
    healthy=0
    if link_is_up && has_ip; then
        # 자가검증 1회 — 링크·IP 가 처음 정상이 된 지금 한다. 시작 시점에 하면 부팅 중
        # IP 미할당을 '대상 없음'으로 읽어 프로브가 영영 꺼진다. 재시도를 주는 이유는
        # 아래 판정이 4회 연속 실패를 요구하는데 검증만 1회로 끊으면 연결 직후의 일시
        # 손실 하나로 기능이 통째로 꺼지기 때문이다(기준 불일치).
        if [ "$probe_ok" -eq 0 ] && [ "$now" -ge "$probe_next_validate" ]; then
            _pt=$(probe_target 2>/dev/null || true)
            if [ -z "$_pt" ]; then
                probe_next_validate=$(( now + PROBE_REVALIDATE_AFTER ))
                log "도달성 프로브 보류 — 대상을 정할 수 없음(${IFACE} 주소 없음). ${PROBE_REVALIDATE_AFTER}초 뒤 재시도"
            else
                _ok=0
                for _i in $(seq 1 "$PROBE_VALIDATE_TRIES"); do
                    if reachable; then _ok=1; break; fi
                    sleep 1
                done
                # 시도한 대상은 성공·실패 무관하게 기록한다. 보류(pending) 중에 주소가
                # 바뀌어도 아래 재검증 감지가 걸리도록 하기 위해서다.
                probe_target_seen="$_pt"
                if [ "$_ok" -eq 1 ]; then
                    probe_ok=1
                    log "도달성 프로브 활성 — 대상 ${_pt} / 연속 ${PROBE_FAIL_THRESHOLD}회 실패 시 무증상 행으로 판정"
                else
                    probe_next_validate=$(( now + PROBE_REVALIDATE_AFTER ))
                    log "도달성 프로브 보류 — ${_pt} 가 ${PROBE_VALIDATE_TRIES}회 무응답. ${PROBE_REVALIDATE_AFTER}초 뒤 재시도(그때까지는 종전 판정만 쓴다)"
                fi
            fi
        fi

        # 주소·서브넷이 바뀌면 옛 대상 기준으로 리셋하지 않도록 다시 검증한다.
        if [ -n "$probe_target_seen" ]; then
            _pt=$(probe_target 2>/dev/null || true)
            if [ "$_pt" != "$probe_target_seen" ]; then
                log "도달성 프로브 재검증 — 대상이 ${probe_target_seen} → ${_pt:-?} 로 바뀜"
                # 보류 중이었더라도 남은 대기시간을 버리고 즉시 재검증한다.
                probe_ok=0; probe_fails=0; probe_next_validate=0; probe_target_seen=""
            fi
        fi

        if [ "$probe_ok" -eq 0 ] || reachable; then
            healthy=1
            probe_fails=0
            probe_reset_armed=1          # 도달성 회복 → 프로브 사유 리셋을 다시 허용
            probe_disarm_logged=0
        else
            probe_fails=$(( probe_fails + 1 ))
            if [ "$probe_fails" -eq 1 ]; then
                log "도달성 프로브 실패 — ${probe_target_seen} 무응답 (링크·IP 는 정상)"
            fi
            if [ "$probe_fails" -lt "$PROBE_FAIL_THRESHOLD" ]; then
                healthy=1
            elif [ "$probe_reset_armed" -eq 0 ]; then
                # 이미 프로브 사유로 리셋했는데 도달성이 안 돌아왔다. 동글이 아니라 대상
                # 장비 쪽 문제일 가능성이 크다. 여기서 down 으로 흘리면 down_since 가 쌓여
                # 30분 뒤 최후 안전망이 다시 리셋하고("회복까지 1회"가 실제로는 "30분마다"가
                # 된다), 링크·IP 가 정상인데 'AP 부재' 로 오분류된 로그까지 남는다.
                # 그래서 관망한다 — 진짜 펌웨어 행이면 커널 오류 경로가 잡고, 도달성이
                # 돌아오면 위에서 재무장한다.
                healthy=1
                if [ "$probe_disarm_logged" -eq 0 ]; then
                    probe_disarm_logged=1
                    log "프로브 사유 리셋 1회 후에도 도달성 미회복 — 대상(${probe_target_seen}) 장애로 보고 관망(커널 오류가 나면 그 경로로 복구)"
                fi
            else
                # 프로브가 자체 임계를 채웠다. 공통 카운터에서 한 틱 더 기다리면 설정값과
                # 실제 동작이 어긋나므로(4회 설정인데 5회째 진입) 여기서 맞춰 둔다.
                fails=$(( FAIL_THRESHOLD - 1 ))
            fi
        fi
    else
        # 링크·IP 가 정상이 아니면 프로브 카운터를 즉시 버린다. 남겨두면 AP 가 사라진
        # 상황에서 stale 카운터가 'AP 부재면 리셋하지 않는다' 보호를 우회한다.
        probe_fails=0
    fi

    if [ "$healthy" -eq 1 ]; then
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
            fails=0; backoff=0; down_since=0; ap_absent_logged=0; reconf_tries=0; probe_fails=0
        fi
        sleep "$POLL"
        continue
    fi

    # 펌웨어 행인가, 아니면 그냥 AP가 없는 것인가?
    probe_reason=0
    if firmware_wedged; then
        reason="펌웨어 행 확인 (커널 rtw_8822cu 오류 검출)"
    elif [ "$probe_fails" -ge "$PROBE_FAIL_THRESHOLD" ] && link_is_up && has_ip \
         && [ "$probe_reset_armed" -eq 1 ]; then
        # 커널 오류가 아직 안 찍혔지만 프레임이 오가지 않는 상태 — 실측상 이 구간이
        # 17분까지 갔다. 펌웨어 오류를 기다리지 않고 여기서 끊는다.
        # 링크·IP 를 여기서 다시 보는 이유: 그 사이 AP 가 사라졌다면 이건 무증상 행이 아니라
        # AP 부재이고, 그때 리셋하면 멀쩡한 동글을 두들기게 된다.
        reason="도달성 상실 — 링크·IP 정상인데 ${probe_target_seen} 무응답 ${probe_fails}회 (무증상 행)"
        # arm 은 여기서 소비하지 않는다 — 아래 rate-limit(MIN_RESET_INTERVAL)에 걸려 리셋이
        # 실행되지 않을 수 있고, 그때 arm 만 잃으면 도달성이 죽은 채로 프로브 복구 경로가
        # 막힌다. 실제로 recover 를 부르는 지점에서 소비한다.
        probe_reason=1
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
    # rate-limit 을 통과해 실제로 리셋하는 지금 arm 을 소비한다.
    [ "$probe_reason" -eq 1 ] && probe_reset_armed=0

    if recover; then
        fails=0; backoff=0; down_since=0; ap_absent_logged=0; reconf_tries=0; probe_fails=0
        # 복구 직후에는 보류 대기시간을 버리고 즉시 재검증한다 — 보류에 빠진 원인이
        # 이번 복구로 해소됐을 수 있는데 최대 300초를 더 기다릴 이유가 없다.
        probe_next_validate=0
    else
        backoff=$(( backoff == 0 ? 30 : backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff=$BACKOFF_MAX
        log "복구 실패 — ${backoff}초 대기 후 재평가"
        sleep "$backoff"
        fails=0
    fi

    sleep "$POLL"
done
