#!/usr/bin/env bash
# 부팅 시 ACPI BERT 레코드가 있으면 자동으로 수집한다.
#
# 왜 필요한가:
#   BERT는 펌웨어가 부팅마다 새로 채우고, 다음 재부팅이 일어나면 직전 사건의
#   레코드는 사라진다. 수동 수집에 의존하면 사람이 장애를 알아채기 전에 재부팅이
#   한 번만 더 일어나도 증거를 잃는다. 실제로 2026-09-11·09-12 두 사건의 레코드가
#   그렇게 소실됐고, 09-21 사건은 수동 수집이 제때 닿아 겨우 확보했다.
#
# 수집 조건:
#   정상 종료 뒤 부팅에는 BERT 커널 메시지가 나오지 않는다
#   (실측: 비정상 4건 4/4 출현, 정상 2건 0/2). 따라서 메시지 유무를 조건으로 쓰면
#   사건이 있었던 부팅만 걸러진다. 매 부팅 수집이 아니므로 쓰레기가 쌓이지 않는다.
#
# 읽기 전용이다. /sys/firmware/acpi/tables/data/BERT 를 읽어도 레코드는 지워지지
# 않는다(커널이 부팅 시 block_status 만 이미 0 으로 지웠고 그 외는 그대로다).
#
# 관련: Issue #57, docs/host-reboot-debugging-plan.md

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 증거는 저장소 밖에 둔다. 원본이 물리 주소를 포함하므로 커밋하지 않는다.
OWNER="${BERT_OWNER:-jhw}"
BASE="${BERT_EVIDENCE_DIR:-/home/${OWNER}/host-incident-evidence}"

# BERT 커널 메시지에서 레코드 수를 읽는다.
records=""
if command -v journalctl >/dev/null 2>&1; then
    records=$(journalctl -k -b 0 --no-pager 2>/dev/null \
        | grep -oE 'BERT: Total records found: [0-9]+' | tail -1 \
        | grep -oE '[0-9]+$')
fi
if [ -z "$records" ]; then
    # journald 가 아직 준비되지 않았을 때를 위한 폴백.
    records=$(dmesg 2>/dev/null \
        | grep -oE 'BERT: Total records found: [0-9]+' | tail -1 \
        | grep -oE '[0-9]+$')
fi

if [ -z "$records" ]; then
    echo "BERT 커널 메시지를 찾지 못했다 — 이번 부팅은 수집 대상이 아니다."
    exit 0
fi
if [ "$records" -eq 0 ]; then
    echo "BERT 레코드 0건 — 정상 부팅으로 보고 수집하지 않는다."
    exit 0
fi

echo "BERT 레코드 ${records}건 발견 — 직전 종료가 비정상이었을 가능성이 높다. 수집한다."

OUT="${BASE}/bert-boot-$(date +%Y%m%d-%H%M%S)"

COLLECT="${HERE}/host-bert-collect.sh"
if [ ! -f "$COLLECT" ]; then
    echo "✗ 수집기를 찾지 못했다: $COLLECT"
    exit 1
fi

# 이 스크립트는 root 로 돌므로 수집기 내부의 sudo 는 암호 없이 통과한다.
bash "$COLLECT" "$OUT"
rc=$?

# root 가 만든 증거를 사용자가 읽을 수 있게 소유권을 넘긴다.
if [ -d "$OUT" ]; then
    chown -R "${OWNER}:${OWNER}" "$OUT" 2>/dev/null \
        || echo "  (소유권 변경 실패 — root 소유로 남는다)"
fi

if [ "$rc" -ne 0 ]; then
    echo "✗ 수집기가 ${rc} 로 끝났다. $OUT 을 확인한다."
    exit "$rc"
fi

echo "▶ 자동 수집 완료: $OUT"
exit 0
