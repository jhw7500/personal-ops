#!/usr/bin/env bash
# 1분마다 한 줄: 패키지 온도 · 최고 코어 온도 · 패키지 열 스로틀 누적 · 현재 PL1/PL2 · load1
#
# 왜 필요한가:
#   atop 10분 평균으로는 1초짜리 전력 버스트가 보이지 않는다(#57, 2026-10-19).
#   스로틀 누적 카운터는 그 사이의 모든 에피소드를 적분하므로 1분 간격으로 남겨도
#   "직전 몇 분간 버스트가 얼마나 있었나"와 "크래시 시점에 전력 제한이 걸려 있었나"를
#   다음 부팅의 journal(-b -1)에서 읽을 수 있다. 읽기 전용이다.
set -uo pipefail
pkg=""; maxc=0
for h in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$h/name" 2>/dev/null)" = "coretemp" ] || continue
    for t in "$h"/temp*_input; do
        v=$(( $(cat "$t") / 1000 )); l=$(cat "${t%_input}_label" 2>/dev/null)
        case "$l" in Package*) pkg=$v;; *) [ "$v" -gt "$maxc" ] && maxc=$v;; esac
    done
done
thr=$(cat /sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count 2>/dev/null || echo '?')
z=/sys/class/powercap/intel-rapl:0
pl1=$(( $(cat $z/constraint_0_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
pl2=$(( $(cat $z/constraint_1_power_limit_uw 2>/dev/null || echo 0) / 1000000 ))
printf 'pkg=%sC maxcore=%sC pkg_throttle=%s PL1=%sW PL2=%sW load1=%s\n' "${pkg:-?}" "$maxc" "$thr" "$pl1" "$pl2" "$(cut -d' ' -f1 /proc/loadavg)"
