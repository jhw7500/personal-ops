#!/usr/bin/env bash
# BERT(부팅 오류 레코드) 원문을 읽어 해석한다.
#
# 읽기 전용이다. /sys/firmware/acpi/tables/data/BERT 는 예약 물리 메모리를
# 매핑해 보여주는 경로이며, 읽어도 레코드가 지워지지 않는다. (커널은 이미
# 부팅 시점에 block_status 만 0 으로 지웠고, 그 외 데이터는 그대로다.)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 기본 출력은 저장소 밖이다. BERT 원본은 물리 주소를 포함하므로 커밋하지 않는다.
# 첫 번째 인자로 다른 경로를 줄 수 있다.
OUT="${1:-$HOME/host-incident-evidence/bert-$(date +%Y%m%d-%H%M%S)}"

mkdir -p "$OUT"
chmod 700 "$OUT"

echo "▶ 증거 보관 위치: $OUT"
echo

if [ ! -e /sys/firmware/acpi/tables/data/BERT ]; then
    echo "✗ /sys/firmware/acpi/tables/data/BERT 가 없다."
    echo "  커널이 BERT 영역을 노출하지 않았다는 뜻이므로 여기서 중단한다."
    exit 1
fi

echo "▶ BERT 원문을 읽는다 (sudo 비밀번호 필요)"
# shellcheck disable=SC2024 # 리다이렉트를 사용자 셸이 자기 소유 디렉터리에 수행하는 것이
# 의도다. `sudo tee` 로 바꾸면 증거 파일이 root 소유로 생겨 이후 판독·정리가 불편해진다.
if ! sudo cat /sys/firmware/acpi/tables/data/BERT > "$OUT/bert-region.bin"; then
    echo "✗ 읽기 실패 — 아래 해석은 진행하지 않는다."
    exit 1
fi
# shellcheck disable=SC2024 # 위와 같은 이유.
sudo cat /sys/firmware/acpi/tables/BERT > "$OUT/bert-table.bin" 2>/dev/null \
    || echo "  (BERT 테이블 헤더는 읽지 못했다 — 영역 데이터만으로 진행)"

chmod 600 "$OUT"/*.bin

REGION_SIZE=$(stat -c %s "$OUT/bert-region.bin")
echo "  bert-region.bin : ${REGION_SIZE} 바이트"
if [ "$REGION_SIZE" -eq 0 ]; then
    echo "✗ 0바이트를 읽었다. 해석을 진행하지 않는다."
    exit 1
fi
echo

# 대조용 커널 메시지와 환경도 같이 남긴다
{
    echo "# 수집 시각: $(date -Is)"
    echo "# uname: $(uname -r)"
    echo "# BIOS: $(cat /sys/class/dmi/id/bios_version 2>/dev/null) / $(cat /sys/class/dmi/id/bios_date 2>/dev/null)"
    echo "# ME: $(cat /sys/class/mei/mei0/fw_ver 2>/dev/null | tr '\n' ' ')"
    echo "# board: $(cat /sys/class/dmi/id/board_name 2>/dev/null)"
    echo
    echo "## 현재 부팅 BERT 커널 메시지"
    journalctl -k -b 0 --no-pager 2>/dev/null | grep -iE 'BERT|Error records' || echo "(no match)"
    echo
    echo "## 부팅 목록"
    journalctl --list-boots --no-pager 2>/dev/null || echo "(unavailable)"
} > "$OUT/context.txt"
chmod 600 "$OUT/context.txt"

echo "▶ 해석"
echo
DECODER="$HERE/host-bert-decode.py"
if [ ! -x "$DECODER" ]; then
    echo "✗ 판독기를 찾지 못했다: $DECODER"
    echo "  원본은 $OUT/bert-region.bin 에 보존됐다. 판독만 별도로 다시 실행하면 된다."
    exit 1
fi
python3 "$DECODER" "$OUT/bert-region.bin" | tee "$OUT/decoded.txt"
chmod 600 "$OUT/decoded.txt"

echo
echo "▶ 보관물"
ls -la "$OUT"
echo
echo "  SHA-256:"
sha256sum "$OUT"/* 2>/dev/null | sed 's/^/    /'
