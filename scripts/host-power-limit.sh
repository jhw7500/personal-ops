#!/usr/bin/env bash
# CPU 패키지 전력 제한(RAPL PL1/PL2)을 Intel 규격값으로 건다.
#
# 왜 필요한가:
#   이 보드(ASUS PRIME Z690-P D4, BIOS 1008)는 기본값으로 PL1/PL2 를 4095 W(무제한)로
#   둔다. 12900KS 에서 그 상태로 에이전트 세션·임베딩·테스트 버스트가 오면 패키지가
#   1초 만에 44→93°C 로 뛰고 열 스로틀이 분당 수십 번 걸린다(2026-10-19 실측).
#   비정상 재부팅 6건(#57)의 원인 가설 중 하나가 "무제한 전력 버스트 → VR/PSU 과도
#   → 내부 리셋"이고, 이 스크립트는 그 가설을 가르는 실험 장치다. 규격값으로 제한한
#   상태에서 재발 여부를 본다.
#
# 무엇을 하는가:
#   /sys/class/powercap 의 package 도메인(intel-rapl:0, 있으면 intel-rapl-mmio:0 도)에
#   long_term(PL1)·short_term(PL2) 전력 제한을 쓰고, 되읽어 검증한다.
#   되읽은 값이 목표와 다르면(BIOS 가 MSR 을 잠근 경우 등) 실패(1)로 끝낸다.
#   "썼다"가 아니라 "읽혀 나온 값이 목표다"만 성공으로 친다.
#
# 되돌리기:
#   재부팅하면 BIOS 값(무제한)으로 돌아간다. 즉시 되돌리려면
#   PL1_W=4095 PL2_W=4095 host-power-limit.sh --apply 를 root 로 실행한다.
#
# 사용:
#   host-power-limit.sh            # 현재값과 목표를 보여 준다 (읽기 전용)
#   host-power-limit.sh --apply    # 적용 + 검증 (root 필요). systemd 유닛이 이걸 부른다.
#   PL1_W=200 PL2_W=241 host-power-limit.sh --apply   # 값 변경
#
# 관련: Issue #57, #56, docs/host-reboot-debugging-plan.md

set -uo pipefail

PL1_W="${PL1_W:-150}"   # 12900KS Processor Base Power
PL2_W="${PL2_W:-241}"   # 12900KS Maximum Turbo Power
MODE="${1:-status}"

ok()   { printf '  [ OK ]   %s\n' "$*"; }
bad()  { printf '  [FAIL]   %s\n' "$*"; }
note() { printf '           %s\n' "$*"; }

# package 도메인 열거: 이름이 package-N 인 최상위 존만 (코어·언코어 서브존 제외)
zones=()
for z in /sys/class/powercap/intel-rapl:[0-9]* /sys/class/powercap/intel-rapl-mmio:[0-9]*; do
    [ -f "$z/name" ] || continue
    case "$(cat "$z/name")" in package-*) zones+=("$z");; esac
done
if [ ${#zones[@]} -eq 0 ]; then
    bad "RAPL package 도메인을 찾지 못했다 (/sys/class/powercap). intel_rapl 모듈 확인."
    exit 2
fi

# 존 안에서 이름으로 constraint 인덱스를 찾는다 (0=long_term 가정 금지)
find_constraint() {  # $1=zone $2=name -> prints index or empty
    local z="$1" want="$2" f
    for f in "$z"/constraint_*_name; do
        [ -f "$f" ] || continue
        if [ "$(cat "$f")" = "$want" ]; then
            f=${f##*/constraint_}; printf '%s' "${f%%_name}"; return 0
        fi
    done
    return 1
}

pkg_temp() {
    local z
    for z in /sys/class/thermal/thermal_zone*; do
        [ "$(cat "$z/type" 2>/dev/null)" = "x86_pkg_temp" ] && { echo $(( $(cat "$z/temp") / 1000 )); return; }
    done
    echo "?"
}

echo "host-power-limit — $(date -Is)  목표 PL1=${PL1_W}W PL2=${PL2_W}W  mode=${MODE}"
echo "  패키지 온도 ${c:=$(pkg_temp)}°C · cpu0 패키지 스로틀 누적 $(cat /sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count 2>/dev/null || echo '?') · load $(cut -d' ' -f1 /proc/loadavg)"

fail=0
for z in "${zones[@]}"; do
    name=$(cat "$z/name"); zn=${z##*/}
    i1=$(find_constraint "$z" long_term)  || { bad "$zn: long_term constraint 없음"; fail=1; continue; }
    i2=$(find_constraint "$z" short_term) || { bad "$zn: short_term constraint 없음"; fail=1; continue; }
    f1="$z/constraint_${i1}_power_limit_uw"; f2="$z/constraint_${i2}_power_limit_uw"
    cur1=$(( $(cat "$f1") / 1000000 )); cur2=$(( $(cat "$f2") / 1000000 ))
    en=$(cat "$z/enabled" 2>/dev/null || echo "?")
    note "$zn ($name): 현재 PL1=${cur1}W PL2=${cur2}W enabled=${en}"

    [ "$MODE" = "--apply" ] || continue
    if [ "$(id -u)" -ne 0 ]; then bad "--apply 는 root 가 필요하다"; exit 2; fi

    # 쓰기. 실패해도 멈추지 말고 되읽기로 판정한다.
    printf '%s' "$(( PL1_W * 1000000 ))" > "$f1" 2>/dev/null || note "$zn: PL1 쓰기 거부(EACCES/EIO) — 되읽기로 판정"
    printf '%s' "$(( PL2_W * 1000000 ))" > "$f2" 2>/dev/null || note "$zn: PL2 쓰기 거부 — 되읽기로 판정"
    [ "$en" = "0" ] && { printf 1 > "$z/enabled" 2>/dev/null || note "$zn: enabled=1 쓰기 거부"; }

    new1=$(( $(cat "$f1") / 1000000 )); new2=$(( $(cat "$f2") / 1000000 )); en2=$(cat "$z/enabled" 2>/dev/null || echo "?")
    if [ "$new1" -eq "$PL1_W" ] && [ "$new2" -eq "$PL2_W" ]; then
        ok "$zn: 적용·검증 PL1=${new1}W PL2=${new2}W enabled=${en2}"
    else
        bad "$zn: 되읽은 값이 목표와 다르다 PL1=${new1}W PL2=${new2}W (BIOS 가 MSR 을 잠갔거나 값이 거부됨)"
        fail=1
    fi
done

if [ "$MODE" = "--apply" ]; then
    [ "$fail" -eq 0 ] && echo "판정: 적용됨 (재부팅 시 BIOS 값으로 원복 — systemd 유닛이 부팅마다 다시 건다)" \
                      || echo "판정: 실패 — 제한이 걸리지 않았다. 실험 조건 불성립."
fi
exit "$fail"
