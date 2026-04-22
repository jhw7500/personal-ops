#!/bin/bash
# LEAD_MS 보정값 측정 — claude CLI가 script start 후 몇 ms 만에
# 첫 네트워크 I/O(SYN → Anthropic)를 보내는지 근사 측정.
#
# 사용: ./calibrate-lead.sh
# 결과의 중앙값을 init-precise.sh의 LEAD_MS로 설정하면 정각에 근접해서 fire됨.

set -u
export PATH="/home/jhw/.nvm/versions/node/v24.12.0/bin:/home/jhw/.local/bin:/usr/bin:/bin"
CLAUDE_BIN="/home/jhw/.local/bin/claude"

if ! command -v strace >/dev/null 2>&1; then
  echo "strace 미설치 — 총 왕복 시간으로 대체 측정합니다." >&2
  for i in 1 2 3 4 5; do
    S=$(date +%s%N)
    /usr/bin/timeout 20s "$CLAUDE_BIN" -p 'ok' >/dev/null 2>&1
    E=$(date +%s%N)
    echo "run $i total=$(( (E - S) / 1000000 ))ms"
  done
  echo "※ 총 왕복 중 첫 API 요청은 보통 1/5~1/10 지점."
  exit 0
fi

TMP=$(mktemp)
for i in 1 2 3 4 5; do
  S=$(date +%s%N)
  /usr/bin/timeout 20s strace -f -e trace=connect -o "$TMP" \
    "$CLAUDE_BIN" -p 'ok' >/dev/null 2>&1
  FIRST=$(grep -m1 'anthropic\|api\|443' "$TMP" | head -1)
  E=$(date +%s%N)
  TOTAL_MS=$(( (E - S) / 1000000 ))
  echo "run $i total=${TOTAL_MS}ms first-connect=${FIRST:0:80}"
done
rm -f "$TMP"
