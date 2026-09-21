#!/usr/bin/env bash
# BIOS/ME 업데이트 착수 전 준비 상태 점검. 읽기 전용이다.
#
# 아무것도 바꾸지 않는다. 각 항목의 실측값을 출력하고 GO / NO-GO 를 판정한다.
# 판정은 측정에서만 나온다 — 이 스크립트는 "확인하지 못한 것"을 통과로 처리하지 않는다.
#
#   bash scripts/host-bios-preflight.sh
#   PREP_DIR=/다른/경로 bash scripts/host-bios-preflight.sh
#
# 관련: docs/host-bios-me-update.md (#56), docs/host-reboot-debugging-plan.md (#57)
set -uo pipefail

PREP_DIR="${PREP_DIR:-$HOME/host-bios-update-prep}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$HOME/host-incident-evidence}"
TARGET_BIOS="4003"
CURRENT_BIOS_EXPECTED="1008"
EXPECTED_ZIP_SHA256="771223DCF7832D633B69D69C6A62FB7A45BB89325E4269127D68C2C2CEE1E155"

pass=0; warn=0; fail=0; unknown=0

ok()      { printf '  [ OK ]   %s\n' "$*"; pass=$((pass+1)); }
warning() { printf '  [WARN]   %s\n' "$*"; warn=$((warn+1)); }
bad()     { printf '  [FAIL]   %s\n' "$*"; fail=$((fail+1)); }
unk()     { printf '  [ ?  ]   %s\n' "$*"; unknown=$((unknown+1)); }
note()    { printf '           %s\n' "$*"; }
section() { printf '\n── %s\n' "$*"; }

printf '════════════════════════════════════════════════════════════\n'
printf 'BIOS/ME 업데이트 프리플라이트 — %s\n' "$(date -Is)"
printf '준비 디렉터리: %s\n' "$PREP_DIR"
printf '════════════════════════════════════════════════════════════\n'

# ───────────────────────────────────────────── 1. 보드·펌웨어 현재 상태
section "1. 보드 및 현재 펌웨어"

board="$(cat /sys/class/dmi/id/board_name 2>/dev/null || echo '')"
if [ "$board" = "PRIME Z690-P D4" ]; then
    ok "보드: $board"
else
    bad "보드가 예상과 다르다: '${board:-읽기 실패}' (예상: PRIME Z690-P D4)"
    note "다른 모델용 BIOS 를 쓰면 보드가 망가진다. 여기서 중단한다."
fi

bios="$(cat /sys/class/dmi/id/bios_version 2>/dev/null || echo '')"
if [ "$bios" = "$CURRENT_BIOS_EXPECTED" ]; then
    ok "BIOS: $bios (업데이트 대상)"
elif [ "$bios" = "$TARGET_BIOS" ]; then
    warning "BIOS 가 이미 $TARGET_BIOS 다. 업데이트가 끝난 상태일 수 있다."
elif [ -n "$bios" ]; then
    warning "BIOS 가 예상($CURRENT_BIOS_EXPECTED)과 다르다: $bios"
else
    unk "BIOS 버전을 읽지 못했다"
fi

me="$(tr '\n' ' ' < /sys/class/mei/mei0/fw_ver 2>/dev/null || echo '')"
if [ -n "$me" ]; then
    ok "Intel ME: $me"
else
    unk "ME 버전을 읽지 못했다 (mei 모듈 미로드?)"
fi

uc="$(grep -m1 microcode /proc/cpuinfo 2>/dev/null | awk '{print $3}')"
uc_bios="$(journalctl -k -b 0 --no-pager 2>/dev/null \
    | grep -oP 'Updated early from: \K0x[0-9a-f]+' | tail -1)"
if [ -n "$uc" ]; then
    ok "마이크로코드: 실행 중 $uc / BIOS 제공분 ${uc_bios:-확인 불가}"
    note "BIOS 제공분이 이 업데이트로 바뀌는 값이다. 실행 중 값은 OS 가 덮어쓴다."
else
    unk "마이크로코드 리비전을 읽지 못했다"
fi

# ───────────────────────────────────────────── 2. 진행 중인 작업
section "2. 진행 중인 작업"

busy=0
for p in bitbake oe-worker runqueue-exec; do
    n="$(pgrep -c -x "$p" 2>/dev/null || true)"; n="${n:-0}"
    [ "$n" -gt 0 ] && { bad "$p 프로세스 ${n}개 실행 중"; busy=1; }
done
[ "$busy" -eq 0 ] && ok "BitBake 계열 프로세스 없음 (검사: bitbake, oe-worker, runqueue-exec)"

la="$(awk '{print $1}' /proc/loadavg 2>/dev/null)"
nproc_n="$(nproc 2>/dev/null || echo 1)"
if [ -n "$la" ]; then
    if awk -v l="$la" -v c="$nproc_n" 'BEGIN{exit !(l > c/2)}'; then
        warning "1분 load average ${la} (코어 ${nproc_n}) — 부하가 있다. 무엇이 도는지 확인해라."
    else
        ok "1분 load average ${la} (코어 ${nproc_n})"
    fi
fi

# ───────────────────────────────────────────── 3. 최근 비정상 재부팅
section "3. 최근 비정상 재부팅 (#57 중단 조건)"

# 정상 종료 판정은 PID 1 의 종료 마커로만 한다.
if command -v journalctl >/dev/null 2>&1; then
    # 직전 부팅(-1)의 마지막 기록에 PID 1 종료 마커가 있는지 본다.
    if journalctl -b -1 --no-pager -o short-iso 2>/dev/null | tail -40 \
        | grep -qE 'systemd\[1\]: (Shutting down|Reached target (Shutdown|Power-Off|Reboot))'; then
        ok "직전 부팅은 정상 종료로 끝났다 (PID 1 종료 마커 확인)"
    else
        prev_end="$(journalctl -b -1 --no-pager -o short-iso 2>/dev/null | tail -1 | awk '{print $1}')"
        if [ -n "$prev_end" ]; then
            warning "직전 부팅에서 PID 1 종료 마커를 찾지 못했다 — 비정상 종료로 본다 (${prev_end})"
            prev_epoch="$(date -d "$prev_end" +%s 2>/dev/null || true)"
            if [ -n "$prev_epoch" ]; then
                elapsed_h=$(( ( $(date +%s) - prev_epoch ) / 3600 ))
                note "마지막 비정상 종료로부터 ${elapsed_h}시간 경과"
                note "#56 중단 조건 '원인이 계속 재현된다' 판단에 쓴다 — 경과 시간만으로"
                note "안정을 입증하지는 못한다. 직전 군집 앞에는 97일 무중단이 있었다."
            fi
        else
            unk "직전 부팅 로그를 읽지 못했다 (journal 보존 범위 밖일 수 있다)"
        fi
    fi
else
    unk "journalctl 을 쓸 수 없다"
fi

up_s="$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)"
printf '           현재 uptime: %d시간 %d분\n' "$((up_s/3600))" "$(((up_s%3600)/60))"
if [ "$up_s" -lt 86400 ]; then
    warning "uptime 이 24시간 미만이다. 최근 재부팅 원인을 확인했는지 점검해라."
fi

# BERT 는 비정상 종료 뒤 부팅에만 나타난다.
bert_n="$(journalctl -k -b 0 --no-pager 2>/dev/null | grep -c 'BERT: .*error records' || true)"
bert_n="${bert_n:-0}"
if [ "$bert_n" -gt 0 ]; then
    warning "현재 부팅에 BERT 하드웨어 오류 레코드가 있다 (${bert_n}건)"
    note "직전 종료가 비정상이었다는 뜻이다. 판독: bash scripts/host-bert-collect.sh"
else
    ok "현재 부팅에 BERT 오류 레코드 없음 (검색 범위: 현재 부팅 커널 로그)"
fi

# ───────────────────────────────────────────── 4. 준비된 파일
section "4. 준비된 BIOS 파일"

zip_found=""
for z in "$PREP_DIR"/bios/*.ZIP "$PREP_DIR"/bios/*.zip; do
    [ -f "$z" ] && zip_found="$z" && break
done

if [ -n "$zip_found" ]; then
    actual="$(sha256sum "$zip_found" | awk '{print toupper($1)}')"
    expected="$(printf '%s' "$EXPECTED_ZIP_SHA256" | tr '[:lower:]' '[:upper:]')"
    if [ "$actual" = "$expected" ]; then
        ok "BIOS ZIP 해시 일치: $(basename "$zip_found")"
    else
        bad "BIOS ZIP 해시 불일치: $(basename "$zip_found")"
        note "실측: $actual"
        note "기대: $expected"
        note "ASUS 페이지의 게시 해시를 다시 확인해라. 불일치 상태로 플래시하지 마라."
    fi
else
    bad "BIOS ZIP 을 찾지 못했다: $PREP_DIR/bios/*.ZIP"
fi

# maxdepth 1 이어야 한다. rollback/ 까지 훑으면 롤백용 CAP 을 본작업용으로 집는다.
cap_main="$(find "$PREP_DIR/bios" -maxdepth 1 -iname '*.CAP' ! -iname 'PZ690P.CAP' ! -iname 'ASUS.CAP' 2>/dev/null | head -1)"
if [ -n "$cap_main" ]; then
    cap_base="$(basename "$cap_main")"
    if printf '%s' "$cap_base" | grep -q "$TARGET_BIOS"; then
        ok "본작업용 CAP: $cap_base ($(stat -c %s "$cap_main") 바이트)"
    else
        bad "본작업용 CAP 의 파일명에 목표 버전($TARGET_BIOS)이 없다: $cap_base"
        note "다른 버전의 CAP 을 플래시하려는 상황일 수 있다. 확인해라."
    fi
else
    bad "본작업용 CAP 파일을 찾지 못했다 (ZIP 을 풀었는가?)"
fi

cap_rec="$(find "$PREP_DIR/bios" -maxdepth 1 \( -iname 'PZ690P.CAP' -o -iname 'ASUS.CAP' \) 2>/dev/null | head -1)"
if [ -n "$cap_rec" ]; then
    ok "복구용 CAP: $(basename "$cap_rec")"
else
    bad "복구용 CAP(PZ690P.CAP 또는 ASUS.CAP)이 없다"
    note "이 보드는 BIOS FlashBack 미지원이다. CrashFree BIOS 3 가 유일한 복구 수단이고,"
    note "플래시 실패 후에는 이 PC 로 복구 USB 를 만들 수 없다."
fi

rb_dir="$PREP_DIR/bios/rollback"
rb_n="$(find "$rb_dir" -maxdepth 1 -iname '*.CAP' 2>/dev/null | wc -l)"
if [ "$rb_n" -gt 0 ]; then
    ok "롤백용 CAP ${rb_n}개: $rb_dir"
else
    warning "롤백용 CAP 이 없다: $rb_dir"
    note "4003 적용 후 문제가 생기면 그때는 내려받을 수 없다."
fi

# ───────────────────────────────────────────── 5. USB 준비 상태
section "5. USB 매체"

# TRAN 은 디스크 행에만 채워지고 파티션 행에서는 비므로, 한 번의 lsblk 출력에서
# 컬럼으로 읽으면 필드가 밀려 감지가 통째로 실패한다. 디스크를 먼저 고르고
# 그 하위 파티션을 따로 조회한다.
usb_found=0
while read -r disk; do
    [ -n "$disk" ] || continue
    usb_found=1
    dev="/dev/$disk"
    size="$(lsblk -dno SIZE "$dev" 2>/dev/null | tr -d ' ')"

    npart="$(lsblk -rno NAME,TYPE "$dev" 2>/dev/null | awk '$2=="part"' | wc -l)"
    if [ "$npart" -ne 1 ]; then
        warning "USB $dev ($size) 파티션이 ${npart}개다 — 단일 FAT32 파티션이어야 한다"
        continue
    fi

    pfs="$(lsblk -rno FSTYPE,TYPE "$dev" 2>/dev/null | awk '$2=="part"{print $1}' | head -1)"
    pmnt="$(lsblk -rno MOUNTPOINT,TYPE "$dev" 2>/dev/null | awk '$2=="part"{print $1}' | head -1)"

    if [ "$pfs" != "vfat" ]; then
        warning "USB $dev ($size) 파일시스템이 ${pfs:-미인식} 이다 (EZ Flash 는 FAT32 를 요구한다)"
        continue
    fi

    if [ -z "$pmnt" ]; then
        # 마운트돼 있지 않으면 내용을 볼 수 없다. 통과로 처리하지 않는다.
        unk "USB $dev ($size, FAT32) 가 마운트돼 있지 않아 CAP 배치를 확인하지 못했다"
        note "확인: bash scripts/host-bios-usb-prep.sh --check $dev"
        continue
    fi

    ncap="$(find "$pmnt" -maxdepth 1 -iname '*.CAP' 2>/dev/null | wc -l)"
    if [ "$ncap" -gt 0 ]; then
        ok "USB $dev ($size, FAT32) 마운트 $pmnt — CAP ${ncap}개"
        find "$pmnt" -maxdepth 1 -iname '*.CAP' -printf '           └ %f (%s 바이트)\n' 2>/dev/null
    else
        warning "USB $dev ($size, FAT32) 루트에 CAP 파일이 없다"
    fi
done < <(lsblk -dno NAME,TRAN 2>/dev/null | awk '$2=="usb"{print $1}')

[ "$usb_found" -eq 0 ] && warning "USB 저장장치가 연결돼 있지 않다 (당일에 꽂으면 된다)"

# ───────────────────────────────────────────── 6. 기준선 보존 여부
section "6. 기준선 및 증거"

for f in firmware-state.txt efibootmgr-before.txt lsblk-before.txt boots-before.txt; do
    if [ -s "$PREP_DIR/baseline/$f" ]; then
        ok "기준선 $f"
    else
        warning "기준선 누락: $PREP_DIR/baseline/$f"
    fi
done

bert_dirs="$(find "$EVIDENCE_DIR" -maxdepth 1 -type d -name 'bert-*' 2>/dev/null | wc -l)"
if [ "$bert_dirs" -gt 0 ]; then
    ok "BERT 증거 보관 ${bert_dirs}건: $EVIDENCE_DIR"
else
    warning "보관된 BERT 증거가 없다 — 업데이트 후 비교 기준이 사라진다"
fi

# ───────────────────────────────────────────── 7. 전원·온도·공간
section "7. 전원 / 온도 / 공간"

if command -v upsc >/dev/null 2>&1 && upsc - 2>/dev/null | head -1 >/dev/null; then
    ok "NUT(upsc) 사용 가능 — UPS 상태를 확인해라"
elif [ -d /sys/class/power_supply ] && \
     [ -n "$(find /sys/class/power_supply -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
    warning "power_supply 항목이 있다. UPS 인지 확인해라: $(find /sys/class/power_supply -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null)"
else
    unk "UPS 를 소프트웨어로 확인할 수 없다 — 물리적으로 확인해라 (#56 에서 필수 항목)"
fi

# CPU 패키지 온도는 coretemp 를 우선한다. /sys/class/thermal 항목은 심볼릭 링크라
# find 가 기본 설정으로는 따라가지 못하므로 hwmon 을 직접 읽는다.
cpu_temp=""
for h in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$h/name" 2>/dev/null)" = "coretemp" ] || continue
    cpu_temp="$(cat "$h/temp1_input" 2>/dev/null)"
    break
done
if [ -z "$cpu_temp" ]; then
    for h in /sys/class/hwmon/hwmon*; do
        [ "$(cat "$h/name" 2>/dev/null)" = "acpitz" ] || continue
        cpu_temp="$(cat "$h/temp1_input" 2>/dev/null)"
        break
    done
    [ -n "$cpu_temp" ] && note "coretemp 를 찾지 못해 acpitz 값을 쓴다 (CPU 코어 온도가 아니다)"
fi

if [ -n "$cpu_temp" ] && [ "$cpu_temp" -gt 0 ]; then
    c=$((cpu_temp/1000))
    if   [ "$c" -lt 60 ]; then ok "CPU 온도 ${c}°C"
    elif [ "$c" -lt 80 ]; then warning "CPU 온도 ${c}°C — 냉각 후 진행해라"
    else bad "CPU 온도 ${c}°C — 너무 뜨겁다"; fi
else
    unk "온도 센서를 읽지 못했다 (검색 범위: /sys/class/hwmon/*/temp1_input 의 coretemp·acpitz)"
fi

for m in / /boot /boot/efi; do
    avail="$(df -BM --output=avail "$m" 2>/dev/null | tail -1 | tr -dc '0-9')"
    if [ -n "$avail" ]; then
        if [ "$m" = "/boot" ] && [ "$avail" -lt 100 ]; then
            warning "$m 여유 ${avail}M"
        else
            ok "$m 여유 ${avail}M"
        fi
    else
        unk "$m 여유 공간을 읽지 못했다"
    fi
done

# ───────────────────────────────────────────── 판정
printf '\n════════════════════════════════════════════════════════════\n'
printf '  OK %d · WARN %d · FAIL %d · 확인불가 %d\n' "$pass" "$warn" "$fail" "$unknown"
printf '════════════════════════════════════════════════════════════\n'

if [ "$fail" -gt 0 ]; then
    printf '판정: NO-GO — FAIL %d건을 해소해라.\n' "$fail"
    exit 1
fi

printf '판정: 차단 항목 없음.\n'
printf '\n다만 아래는 소프트웨어로 확인할 수 없으므로 사람이 직접 확인한다:\n'
printf '  · UPS 또는 안정 전원이 실제로 연결돼 있는가\n'
printf '  · 복구용 USB 가 물리적으로 준비돼 있는가 (PZ690P.CAP)\n'
printf '  · #56 의 (A) 증거 우선 / (B) BIOS 우선 중 무엇을 선택했는가\n'
printf '  · 롤백이 사실상 불가하다는 점을 받아들였는가\n'
printf '    (ASUS 가 BIOS 2014 에서 롤백 금지를 선언했고 현재 1008 은 그 아래다.\n'
printf '     ME 는 어떤 경우에도 되돌아가지 않는다. 실질 롤백 지점은 3811.)\n'
printf '  · BIOS 진입 후 Optimized Defaults 적용 전에\n'
printf '    Restore on AC Power Loss 값을 기록할 것 (#57 증거)\n'
[ "$warn" -gt 0 ] && printf '\nWARN %d건은 판정을 막지 않지만 내용을 읽고 넘어가라.\n' "$warn"
[ "$unknown" -gt 0 ] && printf '확인불가 %d건은 통과가 아니다. 직접 확인해라.\n' "$unknown"
exit 0
