#!/usr/bin/env bash
#
# mirror-sync 모듈 설치 — 수동 CLI 하나를 PATH 에 올린다.
#
# claude-token-sync 와 달리 mirror-sync 는 **데몬이 아니라 사람이 직접 부르는 CLI** 다.
# 그래서 systemd 유닛도 타이머도 크론 엔트리도 없고, 실제로 설치하는 것은
# 심링크 1개 + 실행 비트뿐이다. 재실행 안전(idempotent).
#
# 설정(config/pairs.tsv, config/curation/*.conf)은 심링크하지 않는다.
# bin/mirror-sync.sh 가 readlink -f 로 자기 실체 경로를 풀어 모듈의 config/ 를 직접
# 읽으므로, 심링크로 실행해도 정본은 항상 이 모듈 디렉터리 한 곳이다.
#
# 제거: rm ~/.local/bin/mirror-sync.sh  (그래서 uninstall.sh 를 따로 두지 않았다)
#
# 환경변수
#   MIRROR_SYNC_BIN_DIR  심링크를 둘 디렉터리. 기본 ~/.local/bin

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${MIRROR_SYNC_BIN_DIR:-$HOME/.local/bin}"
SRC="$MODULE_DIR/bin/mirror-sync.sh"
LINK="$BIN_DIR/mirror-sync.sh"

if [[ ! -f "$SRC" ]]; then
    printf '[install] 실행 스크립트를 찾을 수 없다: %s\n' "$SRC" >&2
    exit 1
fi

mkdir -p "$BIN_DIR"
chmod +x "$SRC"

# 남의 파일을 조용히 덮어쓰지 않는다.
# 심링크가 아니거나 다른 곳을 가리키면 사람이 확인하도록 멈춘다.
if [[ -e "$LINK" || -L "$LINK" ]]; then
    if [[ ! -L "$LINK" ]]; then
        printf '[install] 중단: %s 가 심링크가 아니다. 직접 확인하고 치운 뒤 다시 실행해라.\n' "$LINK" >&2
        exit 1
    fi
    current="$(readlink -f -- "$LINK" || true)"
    want="$(readlink -f -- "$SRC")"
    if [[ "$current" != "$want" ]]; then
        printf '[install] 중단: %s 가 다른 곳을 가리킨다 -> %s\n' "$LINK" "${current:-(해석 실패)}" >&2
        printf '           의도한 것이면 직접 지운 뒤 다시 실행해라.\n' >&2
        exit 1
    fi
fi

ln -sfn -- "$SRC" "$LINK"

printf '[install] 완료. 정본 디렉터리: %s\n' "$MODULE_DIR"
printf '[install] 심링크: %s -> %s\n' "$LINK" "$SRC"

case ":$PATH:" in
    *":$BIN_DIR:"*)
        printf '[install] 확인: mirror-sync.sh --help\n'
        ;;
    *)
        printf '[install] 경고: %s 가 PATH 에 없다. PATH 에 추가하거나 절대 경로로 실행해라.\n' "$BIN_DIR"
        printf '[install] 확인: %s --help\n' "$SRC"
        ;;
esac
