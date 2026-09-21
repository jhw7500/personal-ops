#!/usr/bin/env bash
# BIOS/ME 업데이트 직후 검증. 읽기 전용이다.
#
# 업데이트 전 캡처한 기준선(host-bios-preflight 와 같은 PREP_DIR)과 현재 상태를 대조한다.
# 아무것도 바꾸지 않는다. 복원이 필요한 항목을 찾아 알려줄 뿐이다.
#
#   bash scripts/host-bios-postcheck.sh
#   PREP_DIR=/다른/경로 bash scripts/host-bios-postcheck.sh
#
# 관련: docs/host-bios-me-update.md (#56), docs/host-reboot-debugging-plan.md (#57)
set -uo pipefail

PREP_DIR="${PREP_DIR:-$HOME/host-bios-update-prep}"
BASE="$PREP_DIR/baseline"
AFTER="$PREP_DIR/after-$(date +%Y%m%d-%H%M%S)"
TARGET_BIOS="4003"
TARGET_ME_PREFIX="16.1.40"

pass=0; warn=0; fail=0; unknown=0
ok()      { printf '  [ OK ]   %s\n' "$*"; pass=$((pass+1)); }
warning() { printf '  [WARN]   %s\n' "$*"; warn=$((warn+1)); }
bad()     { printf '  [FAIL]   %s\n' "$*"; fail=$((fail+1)); }
unk()     { printf '  [ ?  ]   %s\n' "$*"; unknown=$((unknown+1)); }
note()    { printf '           %s\n' "$*"; }
section() { printf '\n── %s\n' "$*"; }

printf '════════════════════════════════════════════════════════════\n'
printf 'BIOS/ME 업데이트 사후 검증 — %s\n' "$(date -Is)"
printf '════════════════════════════════════════════════════════════\n'

if [ ! -d "$BASE" ]; then
    printf '\n기준선 디렉터리가 없다: %s\n' "$BASE"
    printf '업데이트 전에 캡처한 기준선 없이는 대조할 수 없다.\n'
    printf '현재 상태만 기록하려면 아래를 직접 확인해라:\n'
    printf '  cat /sys/class/dmi/id/bios_version ; cat /sys/class/mei/mei0/fw_ver\n'
    exit 1
fi

mkdir -p "$AFTER"; chmod 700 "$AFTER"

# ───────────────────────────────────────────── 1. 펌웨어 버전
section "1. 펌웨어 버전"

bios_now="$(cat /sys/class/dmi/id/bios_version 2>/dev/null || echo '')"
bios_before="$(grep -oP '^bios_version : \K.*' "$BASE/firmware-state.txt" 2>/dev/null || echo '')"
if [ "$bios_now" = "$TARGET_BIOS" ]; then
    ok "BIOS: ${bios_before:-?} → $bios_now"
elif [ -n "$bios_now" ] && [ "$bios_now" = "$bios_before" ]; then
    bad "BIOS 가 바뀌지 않았다: $bios_now (목표 $TARGET_BIOS)"
    note "플래시가 적용되지 않았다. EZ Flash 가 오류를 냈는지 확인해라."
elif [ -n "$bios_now" ]; then
    warning "BIOS 가 목표와 다르다: $bios_now (목표 $TARGET_BIOS, 이전 ${bios_before:-?})"
else
    unk "BIOS 버전을 읽지 못했다"
fi

printf '           BIOS 날짜: %s\n' "$(cat /sys/class/dmi/id/bios_date 2>/dev/null || echo '?')"

me_now="$(tr '\n' ' ' < /sys/class/mei/mei0/fw_ver 2>/dev/null || echo '')"
if [ -z "$me_now" ]; then
    unk "ME 버전을 읽지 못했다 (mei 모듈 미로드?)"
elif printf '%s' "$me_now" | grep -q "$TARGET_ME_PREFIX"; then
    ok "Intel ME: $me_now"
else
    warning "ME 가 목표($TARGET_ME_PREFIX 계열)와 다르다: $me_now"
    note "BIOS 만 올라가고 ME 는 안 바뀐 경우일 수 있다."
fi

uc_bios_now="$(journalctl -k -b 0 --no-pager 2>/dev/null \
    | grep -oP 'Updated early from: \K0x[0-9a-f]+' | tail -1)"
uc_bios_before="$(grep -oP 'Updated early from: \K0x[0-9a-f]+' "$BASE/firmware-state.txt" 2>/dev/null | tail -1)"
if [ -n "$uc_bios_now" ] && [ -n "$uc_bios_before" ]; then
    if [ "$uc_bios_now" != "$uc_bios_before" ]; then
        ok "BIOS 제공 마이크로코드: $uc_bios_before → $uc_bios_now"
    else
        warning "BIOS 제공 마이크로코드가 그대로다: $uc_bios_now"
        note "BIOS 가 실제로 갱신됐는지 다시 본다. (실행 중 값은 OS 가 덮어쓰므로 지표가 못 된다)"
    fi
else
    unk "마이크로코드 로드 메시지를 대조하지 못했다"
fi
printf '           실행 중 마이크로코드: %s (OS 가 로드 — 전후 동일할 수 있다)\n' \
    "$(grep -m1 microcode /proc/cpuinfo 2>/dev/null | awk '{print $3}')"

# ───────────────────────────────────────────── 2. 부팅 항목
section "2. EFI 부팅 항목"

efibootmgr -v > "$AFTER/efibootmgr-after.txt" 2>&1
if [ -s "$BASE/efibootmgr-before.txt" ]; then
    before_entries="$(grep -c '^Boot[0-9A-F]\{4\}' "$BASE/efibootmgr-before.txt" 2>/dev/null || echo 0)"
    after_entries="$(grep -c '^Boot[0-9A-F]\{4\}' "$AFTER/efibootmgr-after.txt" 2>/dev/null || echo 0)"
    if grep -q 'ubuntu' "$AFTER/efibootmgr-after.txt" 2>/dev/null; then
        ok "ubuntu 부팅 항목 존재 (항목 수 ${before_entries} → ${after_entries})"
    else
        bad "ubuntu 부팅 항목이 사라졌다 (항목 수 ${before_entries} → ${after_entries})"
        note "복원: 기준선 $BASE/efibootmgr-before.txt 를 보고 efibootmgr 로 재등록하거나,"
        note "Live USB 로 부팅해 grub-install 을 다시 실행한다."
    fi
    if diff -q "$BASE/efibootmgr-before.txt" "$AFTER/efibootmgr-after.txt" >/dev/null 2>&1; then
        ok "부팅 항목이 기준선과 완전히 동일"
    else
        warning "부팅 항목에 차이가 있다 — 아래 diff 를 확인해라"
        diff "$BASE/efibootmgr-before.txt" "$AFTER/efibootmgr-after.txt" 2>&1 | sed 's/^/           /'
    fi
else
    unk "기준선 efibootmgr 기록이 없어 대조하지 못했다"
fi

# ───────────────────────────────────────────── 3. 저장장치·장치 인식
section "3. 저장장치 및 장치 인식"

lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINT > "$AFTER/lsblk-after.txt" 2>&1
lspci -nn > "$AFTER/lspci-after.txt" 2>&1

for f in lsblk lspci; do
    b="$BASE/${f}-before.txt"; a="$AFTER/${f}-after.txt"
    if [ -s "$b" ]; then
        nb="$(wc -l < "$b")"; na="$(wc -l < "$a")"
        if diff -q "$b" "$a" >/dev/null 2>&1; then
            ok "$f 출력이 기준선과 동일 (${na}행)"
        else
            warning "$f 에 차이가 있다 (${nb}행 → ${na}행)"
            diff "$b" "$a" 2>&1 | head -20 | sed 's/^/           /'
        fi
    else
        unk "기준선 $f 기록이 없어 대조하지 못했다"
    fi
done

if mountpoint -q / 2>/dev/null; then
    ok "루트 파일시스템 마운트됨: $(findmnt -no SOURCE / 2>/dev/null)"
else
    unk "루트 마운트 상태를 확인하지 못했다"
fi

# ───────────────────────────────────────────── 4. 하드웨어 오류
section "4. 하드웨어 오류 (#57 재발 판정)"

bert_n="$(journalctl -k -b 0 --no-pager 2>/dev/null | grep -c 'BERT: .*error records' || true)"
bert_n="${bert_n:-0}"
if [ "$bert_n" -gt 0 ]; then
    bad "BERT 하드웨어 오류 레코드가 있다 (${bert_n}건)"
    note "직전 종료가 비정상이었다는 뜻이다. 업데이트 과정의 정상 재부팅이었다면 나오지 않아야 한다."
    note "즉시 수집: bash scripts/host-bert-collect.sh"
    note "이전 기록과 섹션 구성·크기·유형을 비교해 같은 실패 모드인지 본다."
else
    ok "BERT 오류 레코드 없음 (검색 범위: 현재 부팅 커널 로그)"
fi

for pat in 'mce:' 'Machine Check' 'Hardware Error' 'PCIe Bus Error'; do
    n="$(journalctl -k -b 0 --no-pager 2>/dev/null | grep -cF "$pat" || true)"; n="${n:-0}"
    if [ "$n" -gt 0 ]; then
        warning "'$pat' ${n}건"
        journalctl -k -b 0 --no-pager 2>/dev/null | grep -F "$pat" | head -3 | sed 's/^/           /'
    else
        ok "'$pat' 0건"
    fi
done

xhci_died="$(journalctl -k -b 0 --no-pager 2>/dev/null | grep -c 'HC died' || true)"
xhci_died="${xhci_died:-0}"
if [ "$xhci_died" -gt 0 ]; then
    bad "xHCI 'HC died' ${xhci_died}건"
else
    ok "xHCI 'HC died' 0건"
fi

# ───────────────────────────────────────────── 5. 온도
section "5. 온도"

for h in /sys/class/hwmon/hwmon*; do
    n="$(cat "$h/name" 2>/dev/null)"
    [ "$n" = "coretemp" ] || continue
    t="$(cat "$h/temp1_input" 2>/dev/null)"
    if [ -n "$t" ]; then
        c=$((t/1000))
        if   [ "$c" -lt 60 ]; then ok "CPU 패키지 온도 ${c}°C"
        elif [ "$c" -lt 85 ]; then warning "CPU 패키지 온도 ${c}°C"
        else bad "CPU 패키지 온도 ${c}°C"; fi
    fi
    break
done

# ───────────────────────────────────────────── 기록 보존
section "기록"

{
    echo "# 사후 검증 기록"
    echo "# 시각: $(date -Is)"
    echo "bios_version : $(cat /sys/class/dmi/id/bios_version 2>/dev/null)"
    echo "bios_date    : $(cat /sys/class/dmi/id/bios_date 2>/dev/null)"
    echo "ME fw_ver    :"; sed 's/^/  /' /sys/class/mei/mei0/fw_ver 2>/dev/null
    echo "microcode    : $(grep -m1 microcode /proc/cpuinfo 2>/dev/null | awk '{print $3}')"
    journalctl -k -b 0 --no-pager 2>/dev/null | grep -i microcode | sed 's/^/  /'
} > "$AFTER/firmware-state.txt"
chmod 600 "$AFTER"/* 2>/dev/null
ok "저장: $AFTER"

# ───────────────────────────────────────────── 판정
printf '\n════════════════════════════════════════════════════════════\n'
printf '  OK %d · WARN %d · FAIL %d · 확인불가 %d\n' "$pass" "$warn" "$fail" "$unknown"
printf '════════════════════════════════════════════════════════════\n'

if [ "$fail" -gt 0 ]; then
    printf '판정: 문제 있음 — FAIL %d건을 확인해라.\n' "$fail"
    exit 1
fi

printf '판정: 즉시 차단할 문제 없음.\n'
printf '\n남은 일:\n'
printf '  · BIOS 설정(부팅 순서, SATA/VMD, XMP, VT-x/d, 팬·전력) 복원 여부 육안 확인\n'
printf '  · Restore on AC Power Loss 값을 기록해 뒀는지 확인 (#57 증거)\n'
printf '  · 재발 판정은 시간이 필요하다. 48~72시간 무사고로는 해결을 입증하지 못한다 —\n'
printf '    직전 군집 앞에는 97일 무중단이 있었다.\n'
[ "$unknown" -gt 0 ] && printf '  · 확인불가 %d건은 통과가 아니다. 직접 확인해라.\n' "$unknown"
exit 0
