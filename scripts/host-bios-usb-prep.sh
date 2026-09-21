#!/usr/bin/env bash
# BIOS 업데이트용 USB 점검·준비.
#
# 기본 동작은 **점검만** 이다. 아무것도 바꾸지 않는다.
# 포맷과 복사는 각각 별도의 명시적 플래그를 요구한다.
#
#   bash scripts/host-bios-usb-prep.sh                    # 연결된 USB 목록과 판정
#   bash scripts/host-bios-usb-prep.sh --check /dev/sdX   # 한 장치 상세 점검
#   bash scripts/host-bios-usb-prep.sh --format /dev/sdX  # FAT32 단일 파티션으로 포맷 (파괴적)
#   bash scripts/host-bios-usb-prep.sh --copy main /dev/sdX     # 본작업용 CAP 복사
#   bash scripts/host-bios-usb-prep.sh --copy recovery /dev/sdX # 복구용 PZ690P.CAP 복사
#   bash scripts/host-bios-usb-prep.sh --verify /dev/sdX  # 복사 후 매체에서 해시 재확인
#
# 안전장치: USB 전송방식(TRAN=usb)이 아닌 장치, 시스템 마운트를 담은 장치,
# 용량 상한을 넘는 장치는 거부한다. 포맷은 장치명을 직접 입력해야 진행한다.
#
# 관련: docs/host-bios-me-update.md (#56)
set -uo pipefail

PREP_DIR="${PREP_DIR:-$HOME/host-bios-update-prep}"
MAX_GB="${MAX_GB:-128}"          # 이보다 큰 장치는 썸드라이브가 아니라고 보고 거부
RECOVERY_NAME="PZ690P.CAP"

die()  { printf '✗ %s\n' "$*" >&2; exit 1; }
ok()   { printf '  [ OK ]   %s\n' "$*"; }
warn() { printf '  [WARN]   %s\n' "$*"; }
bad()  { printf '  [FAIL]   %s\n' "$*"; }
note() { printf '           %s\n' "$*"; }

# ─────────────────────────────────────────── 장치 검증 (모든 파괴적 동작의 전제)
#
# 결과 용량은 전역 SAFE_SIZE_GB 로 돌려준다. 절대 `$(assert_safe_usb ...)` 형태로
# 부르지 마라 — 명령 치환은 서브셸이라 die() 의 exit 가 부모를 멈추지 못하고,
# 거부해야 할 장치에 대해 실행이 그대로 이어진다.
SAFE_SIZE_GB=""
assert_safe_usb() {
    local dev="$1" base tran size_b size_g

    [ -b "$dev" ] || die "블록 장치가 아니다: $dev"

    base="$(basename "$dev")"
    # 파티션이 아니라 디스크 전체를 요구한다
    if [ ! -e "/sys/block/$base" ]; then
        die "디스크 전체를 지정해라 (예: /dev/sdb). 파티션이 아니라: $dev"
    fi

    tran="$(lsblk -dno TRAN "$dev" 2>/dev/null | tr -d ' ')"
    if [ "$tran" != "usb" ]; then
        die "USB 장치가 아니다 (TRAN='${tran:-불명}'): $dev
   이 호스트의 /dev/sda 는 3.6TB 데이터 디스크다. 거부한다."
    fi

    # 이 장치에 시스템 마운트가 걸려 있으면 거부
    local m
    while read -r m; do
        case "$m" in
            /|/boot|/boot/efi|/home|/var|/opt/sda)
                die "이 장치에 시스템 마운트($m)가 있다: $dev" ;;
        esac
    done < <(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$')

    # blockdev --getsize64 는 root 를 요구한다. 실패를 0 으로 삼키면 용량 상한
    # 가드가 통째로 무력화되므로(1TB 외장 디스크가 "0GB"로 통과) lsblk 를 쓰고,
    # 크기를 못 읽으면 통과시키지 않고 중단한다.
    size_b="$(lsblk -bdno SIZE "$dev" 2>/dev/null | tr -d ' ')"
    if ! printf '%s' "$size_b" | grep -qE '^[0-9]+$' || [ "$size_b" -eq 0 ]; then
        die "장치 용량을 읽지 못했다: $dev
   용량을 확인할 수 없으면 안전 상한을 적용할 수 없으므로 진행하지 않는다."
    fi
    size_g=$(( size_b / 1000000000 ))
    if [ "$size_g" -gt "$MAX_GB" ]; then
        die "용량 ${size_g}GB 가 상한 ${MAX_GB}GB 를 넘는다: $dev
   외장 디스크를 잘못 지정했을 가능성이 크다. 정말 맞으면 MAX_GB 를 올려라."
    fi
    SAFE_SIZE_GB="$size_g"
}

# ─────────────────────────────────────────── 목록
list_usb() {
    printf '\n── 연결된 USB 저장장치\n'
    local found=0
    while read -r name size; do
        found=1
        printf '\n  /dev/%s  (%s)\n' "$name" "$size"
        lsblk -no NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "/dev/$name" 2>/dev/null \
            | sed 's/^/      /'
    done < <(lsblk -dno NAME,SIZE,TRAN 2>/dev/null | awk '$3=="usb"{print $1, $2}')

    if [ "$found" -eq 0 ]; then
        printf '  (없음 — USB 를 꽂고 다시 실행해라)\n'
        return 1
    fi
    printf '\n상세 점검: %s --check /dev/sdX\n' "$0"
}

# ─────────────────────────────────────────── 점검
check_usb() {
    local dev="$1" size_g parts fstype ptype
    assert_safe_usb "$dev"; size_g="$SAFE_SIZE_GB"

    printf '\n── %s 점검\n' "$dev"

    if [ "$size_g" -ge 2 ] && [ "$size_g" -le 32 ]; then
        ok "용량 ${size_g}GB (권장 범위 2~32GB)"
    elif [ "$size_g" -lt 2 ]; then
        warn "용량 ${size_g}GB — CAP 파일(수십 MB)에는 충분하지만 확인해라"
    else
        warn "용량 ${size_g}GB — 32GB 초과. 동작은 하겠지만 작은 매체를 권한다"
    fi

    ptype="$(lsblk -dno PTTYPE "$dev" 2>/dev/null | tr -d ' ')"
    case "$ptype" in
        dos) ok "파티션 테이블: MBR (EZ Flash 호환성 최상)" ;;
        gpt) warn "파티션 테이블: GPT — 대개 되지만 MBR 이 더 안전하다" ;;
        "")  warn "파티션 테이블 없음 — 포맷이 필요하다" ;;
        *)   warn "파티션 테이블: $ptype" ;;
    esac

    parts="$(lsblk -nro NAME,TYPE "$dev" 2>/dev/null | awk '$2=="part"' | wc -l)"
    if [ "$parts" -eq 1 ]; then
        ok "파티션 1개 (단일 파티션 요구 충족)"
    elif [ "$parts" -eq 0 ]; then
        bad "파티션이 없다 — 포맷이 필요하다"
    else
        bad "파티션 ${parts}개 — 단일 파티션으로 다시 만드는 것을 권한다"
    fi

    # 파티션이 1개가 아니면 첫 파티션의 파일시스템만 보고 판정하면 안 된다.
    # (boot=vfat + root=ext4 같은 구성을 "FAT32 라 포맷 불필요"로 오판한다.)
    if [ "$parts" -ne 1 ]; then
        bad "파일시스템 판정 보류 — 파티션이 ${parts}개다. 전체를 다시 만들어야 한다."
        note "현재 구성:"
        lsblk -no NAME,SIZE,FSTYPE,LABEL "$dev" 2>/dev/null | sed 's/^/             /'
        note "포맷: $0 --format $dev"
        return 0
    fi

    fstype="$(lsblk -nro FSTYPE "$dev" 2>/dev/null | grep -v '^$' | head -1)"
    case "$fstype" in
        vfat)
            ok "파일시스템: FAT32/vfat — **포맷 불필요**"
            local mnt
            mnt="$(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$' | head -1)"
            if [ -n "$mnt" ]; then
                note "마운트: $mnt"
                local n
                n="$(find "$mnt" -maxdepth 1 -type f 2>/dev/null | wc -l)"
                note "루트의 파일 ${n}개"
                find "$mnt" -maxdepth 1 -iname '*.CAP' -printf '           └ %f (%s 바이트)\n' 2>/dev/null
            else
                note "마운트되지 않았다 — 파일 관리자나 udisksctl 로 마운트해라"
            fi
            ;;
        ntfs|exfat)
            bad "파일시스템: $fstype — **FAT32 로 포맷해야 한다** (EZ Flash 3 요구사항)"
            note "포맷: $0 --format $dev" ;;
        "")
            bad "파일시스템 없음 — 포맷이 필요하다"
            note "포맷: $0 --format $dev" ;;
        *)
            bad "파일시스템: $fstype — FAT32 가 아니다"
            note "포맷: $0 --format $dev" ;;
    esac
}

# ─────────────────────────────────────────── 포맷 (파괴적)
format_usb() {
    local dev="$1" size_g answer base
    assert_safe_usb "$dev"; size_g="$SAFE_SIZE_GB"
    base="$(basename "$dev")"

    printf '\n╔══════════════════════════════════════════════════════════╗\n'
    printf '║  경고: 아래 장치의 모든 데이터가 사라진다                 ║\n'
    printf '╚══════════════════════════════════════════════════════════╝\n\n'
    printf '  장치 : %s (%sGB)\n' "$dev" "$size_g"
    printf '  모델 : %s\n' "$(lsblk -dno MODEL "$dev" 2>/dev/null | sed 's/  */ /g')"
    printf '  현재 내용:\n'
    lsblk -no NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$dev" 2>/dev/null | sed 's/^/    /'
    printf '\n  진행하려면 장치명을 그대로 입력해라 (예: %s), 취소는 Enter: ' "$base"
    read -r answer
    [ "$answer" = "$base" ] || die "취소했다 (입력: '${answer}')"

    printf '\n마운트 해제 중...\n'
    while read -r p; do
        [ -n "$p" ] && sudo umount "$p" 2>/dev/null && printf '  umount %s\n' "$p"
    done < <(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$')

    printf 'MBR 파티션 테이블 생성...\n'
    sudo wipefs -a "$dev" >/dev/null 2>&1 || die "wipefs 실패"
    sudo parted -s "$dev" mklabel msdos || die "파티션 테이블 생성 실패"
    sudo parted -s "$dev" mkpart primary fat32 1MiB 100% || die "파티션 생성 실패"
    sudo parted -s "$dev" set 1 boot on 2>/dev/null || true
    sudo partprobe "$dev" 2>/dev/null || true
    sleep 2

    local part="${dev}1"
    [ -b "$part" ] || part="${dev}p1"
    [ -b "$part" ] || die "생성된 파티션을 찾지 못했다"

    printf 'FAT32 포맷 (%s)...\n' "$part"
    sudo mkfs.vfat -F 32 -n BIOSUPD "$part" || die "mkfs.vfat 실패"

    printf '\n✓ 완료. 결과:\n'
    lsblk -no NAME,SIZE,FSTYPE,LABEL "$dev" 2>/dev/null | sed 's/^/  /'
    printf '\n확인: %s --check %s\n' "$0" "$dev"
}

# ─────────────────────────────────────────── CAP 복사
copy_cap() {
    local kind="$1" dev="$2" mnt src dst
    assert_safe_usb "$dev"

    local fstype
    fstype="$(lsblk -nro FSTYPE "$dev" 2>/dev/null | grep -v '^$' | head -1)"
    [ "$fstype" = "vfat" ] || die "FAT32 가 아니다 (현재 ${fstype:-없음}). --format 을 먼저 해라"

    # 이 호스트에는 udisksctl/자동마운트가 없다. 마운트돼 있지 않으면 직접 붙였다가
    # 끝나고 뗀다. vfat 은 uid 를 넘겨야 일반 사용자가 쓸 수 있다.
    local part
    mnt="$(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -v '^$' | head -1)"
    if [ -z "$mnt" ]; then
        part="${dev}1"; [ -b "$part" ] || part="${dev}p1"
        [ -b "$part" ] || die "파티션을 찾지 못했다: ${dev}1 / ${dev}p1"
        mnt="$(mktemp -d)"
        printf '마운트: %s → %s (sudo 필요)\n' "$part" "$mnt"
        sudo mount -o "uid=$(id -u),gid=$(id -g)" "$part" "$mnt" \
            || { rmdir "$mnt" 2>/dev/null; die "마운트 실패: $part"; }
        # 성공·실패 어느 경로로 끝나도 반드시 뗀다 (die 도 EXIT 를 탄다).
        CLEANUP_MNT="$mnt"
        trap 'if [ -n "${CLEANUP_MNT:-}" ]; then
                  sync
                  sudo umount "$CLEANUP_MNT" 2>/dev/null && printf "언마운트: %s\n" "$CLEANUP_MNT"
                  rmdir "$CLEANUP_MNT" 2>/dev/null
              fi' EXIT
    fi

    # 버전명이 붙은 원본 CAP 을 고른다. 복구용 이름(PZ690P/ASUS)은 제외해야
    # 본작업 USB 에 복구용 이름이 올라가는 일이 없다.
    src="$(find "$PREP_DIR/bios" -maxdepth 1 -iname '*.CAP' \
             ! -iname "$RECOVERY_NAME" ! -iname 'ASUS.CAP' 2>/dev/null | head -1)"
    [ -n "$src" ] || die "버전명 CAP 파일이 없다: $PREP_DIR/bios/*.CAP
   BIOS ZIP 을 내려받아 풀었는가?"

    case "$kind" in
        main)     dst="$mnt/$(basename "$src")" ;;
        recovery) dst="$mnt/$RECOVERY_NAME" ;;
        *) die "kind 는 main 또는 recovery 여야 한다: $kind" ;;
    esac

    printf '\n복사: %s\n    → %s\n' "$src" "$dst"
    cp "$src" "$dst" || die "복사 실패"
    sync
    local a b
    a="$(sha256sum "$src" | awk '{print $1}')"
    b="$(sha256sum "$dst" | awk '{print $1}')"
    if [ "$a" = "$b" ]; then
        printf '✓ SHA-256 일치 — 복사본이 원본과 동일하다\n  %s\n' "$a"
    else
        die "SHA-256 불일치. 매체 불량일 수 있다.
   원본: $a
   복사: $b"
    fi
    printf '\n%s 루트 내용:\n' "$mnt"
    find "$mnt" -maxdepth 1 -type f -printf '  %f (%s 바이트)\n' 2>/dev/null
    [ "$kind" = "recovery" ] && \
        printf '\n복구용 USB 는 루트에 %s 만 두는 것이 안전하다\n(CrashFree BIOS 3 가 루트를 스캔한다).\n' "$RECOVERY_NAME"
}

# ─────────────────────────────────────────── 복사 후 검증
#
# 복사 시점의 해시 일치만으로는 부족하다. 언마운트·sync 를 거쳐 매체에 실제로
# 남았는지는 다시 붙여서 읽어봐야 안다 (불량 USB 는 여기서 갈린다).
verify_usb() {
    local dev="$1" mnt part n bad_n=0
    assert_safe_usb "$dev"

    local fstype
    fstype="$(lsblk -rno FSTYPE,TYPE "$dev" 2>/dev/null | awk '$2=="part"{print $1}' | head -1)"
    [ "$fstype" = "vfat" ] || die "FAT32 가 아니다 (현재 ${fstype:-없음})"

    mnt="$(lsblk -rno MOUNTPOINT,TYPE "$dev" 2>/dev/null | awk '$2=="part"{print $1}' | head -1)"
    if [ -z "$mnt" ]; then
        part="${dev}1"; [ -b "$part" ] || part="${dev}p1"
        [ -b "$part" ] || die "파티션을 찾지 못했다: ${dev}1 / ${dev}p1"
        mnt="$(mktemp -d)"
        printf '읽기전용 마운트: %s → %s (sudo 필요)\n' "$part" "$mnt"
        sudo mount -o ro "$part" "$mnt" \
            || { rmdir "$mnt" 2>/dev/null; die "마운트 실패: $part"; }
        CLEANUP_MNT="$mnt"
        trap 'if [ -n "${CLEANUP_MNT:-}" ]; then
                  sudo umount "$CLEANUP_MNT" 2>/dev/null
                  rmdir "$CLEANUP_MNT" 2>/dev/null
              fi' EXIT
    fi

    printf '\n── %s 검증 (마운트 %s)\n' "$dev" "$mnt"

    n="$(find "$mnt" -maxdepth 1 -iname '*.CAP' 2>/dev/null | wc -l)"
    if [ "$n" -eq 0 ]; then
        bad "루트에 CAP 파일이 없다"
        printf '\n루트 내용:\n'
        find "$mnt" -maxdepth 1 -mindepth 1 -printf '  %f\n' 2>/dev/null
        return 1
    fi

    while read -r f; do
        local base h_usb h_src src
        base="$(basename "$f")"
        h_usb="$(sha256sum "$f" | awk '{print $1}')"

        # 준비 디렉터리에서 같은 해시의 원본을 찾는다
        src=""
        while read -r cand; do
            h_src="$(sha256sum "$cand" | awk '{print $1}')"
            [ "$h_src" = "$h_usb" ] && { src="$cand"; break; }
        done < <(find "$PREP_DIR/bios" -iname '*.CAP' 2>/dev/null)

        if [ -n "$src" ]; then
            ok "$base ($(stat -c %s "$f") 바이트)"
            note "SHA-256 일치 → 원본: ${src#"$PREP_DIR/bios/"}"
        else
            bad "$base — 준비 디렉터리의 어떤 CAP 과도 해시가 다르다"
            note "USB : $h_usb"
            note "매체 불량이거나 다른 파일이다. 다시 복사해라."
            bad_n=$((bad_n+1))
        fi
    done < <(find "$mnt" -maxdepth 1 -iname '*.CAP' 2>/dev/null)

    printf '\n루트 전체:\n'
    find "$mnt" -maxdepth 1 -mindepth 1 -printf '  %f (%s 바이트)\n' 2>/dev/null

    [ "$bad_n" -eq 0 ] || return 1
    return 0
}

# ─────────────────────────────────────────── 진입점
case "${1:-}" in
    ""|--list) list_usb ;;
    --check)   [ -n "${2:-}" ] || die "장치를 지정해라: --check /dev/sdX"; check_usb "$2" ;;
    --format)  [ -n "${2:-}" ] || die "장치를 지정해라: --format /dev/sdX"; format_usb "$2" ;;
    --verify)  [ -n "${2:-}" ] || die "장치를 지정해라: --verify /dev/sdX"; verify_usb "$2" ;;
    --copy)    [ -n "${2:-}" ] && [ -n "${3:-}" ] || die "사용법: --copy main|recovery /dev/sdX"
               copy_cap "$2" "$3" ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//' ;;
    *)         die "알 수 없는 옵션: $1  (--help 참조)" ;;
esac
