#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM=${0##*/}
MANAGED_FILE=60-user-resource-limit.conf
TASKS_MAX=4096

SYSTEMD_ROOT=${RESOURCE_LIMIT_SYSTEMD_ROOT:-/etc/systemd/system}
SYSTEMCTL=${RESOURCE_LIMIT_SYSTEMCTL:-systemctl}
SYSTEMD_ANALYZE=${RESOURCE_LIMIT_SYSTEMD_ANALYZE:-systemd-analyze}
DEVICE=${RESOURCE_LIMIT_DEVICE:-/dev/sda}
BASE_IOPS=${RESOURCE_LIMIT_BASE_IOPS:-80}

usage() {
    cat <<EOF
Usage:
  sudo ./$PROGRAM apply USER PERCENT
  ./$PROGRAM plan USER PERCENT
  ./$PROGRAM status USER
  sudo ./$PROGRAM remove USER

PERCENT must be an integer from 1 through 100. It controls the user's
share of total CPU, physical memory, swap, and the configured disk IOPS
baseline. The root and jhw accounts are protected.

Examples:
  sudo ./$PROGRAM apply devuser 50
  ./$PROGRAM status devuser
  sudo ./$PROGRAM remove devuser
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_root() {
    [[ $(id -u) -eq 0 ]] || die "this operation requires root; run it with sudo"
}

validate_percent() {
    local value=$1
    [[ $value =~ ^[0-9]+$ ]] || die "PERCENT must be an integer from 1 through 100"
    (( value >= 1 && value <= 100 )) || die "PERCENT must be between 1 and 100"
}

resolve_user() {
    local requested=$1 entry
    local -a passwd_fields
    [[ $requested =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*$ ]] || die "invalid username: $requested"
    entry=$(getent passwd "$requested") || die "user does not exist: $requested"

    IFS=: read -r -a passwd_fields <<<"$entry"
    TARGET_USER=${passwd_fields[0]}
    TARGET_UID=${passwd_fields[2]}
    TARGET_SHELL=${passwd_fields[6]}
    [[ $TARGET_USER == "$requested" ]] || die "user lookup mismatch for: $requested"
    [[ $TARGET_UID =~ ^[0-9]+$ ]] || die "invalid UID returned for: $requested"
    (( TARGET_UID >= 1000 && TARGET_UID < 65534 )) || die "system accounts are not supported: $requested"
    [[ $TARGET_SHELL != */nologin && $TARGET_SHELL != */false ]] || die "login-disabled account is not supported: $requested"

    TARGET_SLICE="user-${TARGET_UID}.slice"
    TARGET_DIR="$SYSTEMD_ROOT/${TARGET_SLICE}.d"
    TARGET_FILE="$TARGET_DIR/$MANAGED_FILE"
}

reject_protected_target() {
    case $TARGET_USER in
        root|jhw) die "protected user cannot be limited: $TARGET_USER" ;;
    esac
    (( TARGET_UID != 0 )) || die "UID 0 cannot be limited"
}

read_host_values() {
    CPU_COUNT=${RESOURCE_LIMIT_CPU_COUNT:-$(nproc)}
    MEM_BYTES=${RESOURCE_LIMIT_MEM_BYTES:-$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) * 1024 ))}
    SWAP_BYTES=${RESOURCE_LIMIT_SWAP_BYTES:-$(( $(awk '/^SwapTotal:/ {print $2}' /proc/meminfo) * 1024 ))}

    if [[ ! $CPU_COUNT =~ ^[0-9]+$ ]] || (( CPU_COUNT <= 0 )); then
        die "invalid online CPU count"
    fi
    if [[ ! $MEM_BYTES =~ ^[0-9]+$ ]] || (( MEM_BYTES <= 0 )); then
        die "invalid physical memory size"
    fi
    [[ $SWAP_BYTES =~ ^[0-9]+$ ]] || die "invalid swap size"
    if [[ ! $BASE_IOPS =~ ^[0-9]+$ ]] || (( BASE_IOPS <= 0 )); then
        die "invalid disk IOPS baseline"
    fi
}

calculate_policy() {
    local percent=$1
    CPU_QUOTA=$(( CPU_COUNT * percent ))
    MEMORY_HIGH=$(( percent * 9 / 10 ))
    (( MEMORY_HIGH >= 1 )) || MEMORY_HIGH=1
    SWAP_MAX=$(( SWAP_BYTES * percent / 100 ))
    DISK_IOPS=$(( BASE_IOPS * percent / 100 ))
    (( DISK_IOPS >= 1 )) || DISK_IOPS=1
}

print_policy() {
    cat <<EOF
User=$TARGET_USER
UID=$TARGET_UID
Slice=$TARGET_SLICE
Percent=$1
CPUQuota=${CPU_QUOTA}%
MemoryHigh=${MEMORY_HIGH}%
MemoryMax=$1%
MemorySwapMax=$SWAP_MAX
TasksMax=$TASKS_MAX
IOWeight=100
IOReadIOPSMax=$DEVICE $DISK_IOPS
IOWriteIOPSMax=$DEVICE $DISK_IOPS
EOF
}

render_policy() {
    local percent=$1
    cat <<EOF
# Managed by $PROGRAM. Remove with: $PROGRAM remove $TARGET_USER
# Managed-User=$TARGET_USER
# Managed-UID=$TARGET_UID
# Managed-Percent=$percent
[Slice]
CPUAccounting=yes
CPUQuota=${CPU_QUOTA}%
MemoryAccounting=yes
MemoryHigh=${MEMORY_HIGH}%
MemoryMax=$percent%
MemorySwapMax=$SWAP_MAX
TasksAccounting=yes
TasksMax=$TASKS_MAX
IOAccounting=yes
IOWeight=100
IOReadIOPSMax=$DEVICE $DISK_IOPS
IOWriteIOPSMax=$DEVICE $DISK_IOPS
EOF
}

verify_environment() {
    [[ $(stat -fc %T /sys/fs/cgroup 2>/dev/null) == cgroup2fs ]] || die "cgroup v2 is required"
    [[ -b $DEVICE ]] || die "configured I/O device is not a block device: $DEVICE"
    require_command "$SYSTEMCTL"
    require_command "$SYSTEMD_ANALYZE"
}

apply_policy() {
    local percent=$1 temp backup=
    require_root
    verify_environment
    mkdir -p "$TARGET_DIR"
    temp=$(mktemp "$TARGET_DIR/.${MANAGED_FILE}.XXXXXX")
    trap 'rm -f "${temp:-}" "${backup:-}"' RETURN
    render_policy "$percent" >"$temp"
    chmod 0644 "$temp"

    if [[ -e $TARGET_FILE ]]; then
        backup=$(mktemp "$TARGET_DIR/.${MANAGED_FILE}.backup.XXXXXX")
        cp -a "$TARGET_FILE" "$backup"
    fi
    mv -f "$temp" "$TARGET_FILE"
    temp=

    if ! "$SYSTEMD_ANALYZE" verify "$TARGET_SLICE" >/dev/null; then
        if [[ -n $backup ]]; then
            mv -f "$backup" "$TARGET_FILE"
            backup=
        else
            rm -f "$TARGET_FILE"
        fi
        die "systemd rejected the generated policy; previous configuration restored"
    fi

    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" show "$TARGET_SLICE" \
        -p ActiveState -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax \
        -p MemorySwapMax -p TasksMax -p IOWeight >/dev/null || true
    backup=
    trap - RETURN

    printf 'Applied %s%% resource policy to %s (%s).\n' "$percent" "$TARGET_USER" "$TARGET_SLICE"
    print_policy "$percent"
}

show_status() {
    printf 'User: %s (UID %s)\nSlice: %s\n' "$TARGET_USER" "$TARGET_UID" "$TARGET_SLICE"
    if [[ -f $TARGET_FILE ]]; then
        printf 'Managed policy: present\nPolicy file: %s\n' "$TARGET_FILE"
        sed -n '/^\[Slice\]/,$p' "$TARGET_FILE"
    else
        printf 'Managed policy: absent\n'
    fi
    printf '\nEffective systemd state:\n'
    "$SYSTEMCTL" show "$TARGET_SLICE" \
        -p ActiveState -p TasksCurrent -p MemoryCurrent \
        -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax \
        -p MemorySwapMax -p TasksMax -p IOWeight \
        -p IOReadIOPSMax -p IOWriteIOPSMax 2>/dev/null || \
        printf 'Unit is currently inactive; a managed policy will apply at next login.\n'
}

remove_policy() {
    require_root
    require_command "$SYSTEMCTL"
    if [[ ! -e $TARGET_FILE ]]; then
        printf 'No managed policy exists for %s (%s).\n' "$TARGET_USER" "$TARGET_SLICE"
        return 0
    fi

    rm -f -- "$TARGET_FILE"
    rmdir --ignore-fail-on-non-empty "$TARGET_DIR" 2>/dev/null || true
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" show "$TARGET_SLICE" \
        -p CPUQuotaPerSecUSec -p MemoryHigh -p MemoryMax \
        -p MemorySwapMax -p TasksMax -p IOWeight >/dev/null 2>&1 || true
    printf 'Removed the managed resource policy from %s (%s).\n' "$TARGET_USER" "$TARGET_SLICE"
}

main() {
    local action=${1-} user=${2-} percent=${3-}
    case $action in
        -h|--help|help)
            usage
            return 0
            ;;
        apply|plan)
            [[ $# -eq 3 ]] || { usage >&2; die "$action requires USER and PERCENT"; }
            validate_percent "$percent"
            resolve_user "$user"
            reject_protected_target
            read_host_values
            calculate_policy "$percent"
            if [[ $action == plan ]]; then
                print_policy "$percent"
            else
                apply_policy "$percent"
            fi
            ;;
        status)
            [[ $# -eq 2 ]] || { usage >&2; die "status requires USER"; }
            resolve_user "$user"
            show_status
            ;;
        remove)
            [[ $# -eq 2 ]] || { usage >&2; die "remove requires USER"; }
            resolve_user "$user"
            remove_policy
            ;;
        *)
            usage >&2
            die "unknown command: ${action:-<none>}"
            ;;
    esac
}

main "$@"
