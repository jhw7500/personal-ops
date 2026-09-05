#!/usr/bin/env bash
#
# mirror-sync.sh — github upstream → gitlab 큐레이션 미러 싱크 (personal-ops 이슈 #34)
#
# ┌─ 설계 경계: push 는 자동화하지 않는다 ─────────────────────────────────────┐
# │ 이 스크립트가 자동으로 하는 최대 범위는 **미러 저장소의 로컬 커밋**이다.    │
# │ 원격 push 를 하지 않으며 --push 플래그도, push 하는 서브커맨드도 없다.      │
# │ 커밋은 로컬에서 되돌리기 쉽지만 push 는 다른 사람·CI·미러가 즉시 보게 되는  │
# │ 외부 공개 행위이고 되돌리는 비용이 급격히 커진다. push 는 사람이 직접       │
# │ 승인하고 직접 실행한다.                                                    │
# └────────────────────────────────────────────────────────────────────────────┘
#
# ┌─ 보고한 것과 커밋된 것이 어긋나지 않게 하는 장치 ──────────────────────────┐
# │ '복사/쓰기' 로 보고한 파일이 커밋에 들어가지 않는 경로가 실재한다:          │
# │ `git add -A` 는 **미러의 .gitignore** 에 걸리는 새 파일을 조용히 건너뛴다.  │
# │ 그러면 화면에는 "쓰기 <경로>" 와 [PASS] 만 남고 파일은 미러 원격에 영원히   │
# │ 없으며, 무시 파일이라 `git status --porcelain` 에도 안 잡혀 다음 실행의     │
# │ clean 검사까지 통과한다. 그래서 두 겹으로 막는다.                          │
# │   1) 복사 **전에** `git check-ignore` 로 무시 대상을 찾아 거부(코드 3).     │
# │      쓰기 전에 멈추므로 워킹트리에 잔여물도 남지 않는다.                    │
# │   2) `git add -A` **후에** ASSERT_E 로 '쓰기 로 보고한 경로가 전부 인덱스에 │
# │      있는가'를 확인한다. 무시 규칙 말고 다른 이유로 빠져도 잡힌다.          │
# └────────────────────────────────────────────────────────────────────────────┘
#
# ┌─ 적용 중 실패·어서션 실패 시 미러 워킹트리를 어떻게 남기는가 ──────────────┐
# │ **자동 롤백하지 않고 복사·스테이징된 상태 그대로 남긴다.**                  │
# │ 이유: 어서션이 잡아내는 것이 바로 "조용히 사라진 큐레이션"이다. 사람이      │
# │ `git diff --cached` 로 무엇이 유입·소실됐는지 직접 봐야 하는데 자동 청소는  │
# │ 그 증거를 지운다.                                                          │
# │ 되돌릴 수 없는 상태가 아닌 근거: --apply 는 시작 전에 미러 워킹트리가       │
# │ clean 한지 확인하고 아니면 거부한다. 따라서 직전 상태는 정확히 HEAD 이고    │
# │ 복구는 항상 아래 한 줄로 끝난다.                                           │
# │     git -C <mirror> reset --hard HEAD && git -C <mirror> clean -fd          │
# │ 이 복구 명령은 **적용 단계에서 0 이 아닌 코드로 끝나는 모든 경로**에서      │
# │ 화면에 출력한다(어서션 실패뿐 아니라 복사 실패·중단도 포함).                │
# │ 무시 파일은 `-x` 가 없어 남지만, 위의 check-ignore 게이트가 무시 대상       │
# │ 복사를 애초에 거부하므로 이 스크립트가 무시 파일을 만들어 두는 일은 없다.   │
# └────────────────────────────────────────────────────────────────────────────┘
#
# 종료 코드
#   0  성공 (--help 포함)
#   1  사용법·인자 오류
#   2  설정 오류 (pairs.tsv / curation conf — 축 누락·타입 오류·빈 배열 포함)
#   3  저장소 상태 오류 (경로·git repo 동일성·sha·조상관계·dirty 워킹트리·
#                        브랜치 불일치·미러 .gitignore 와 복사 대상 충돌)
#   4  어서션 실패 (--apply 시. 커밋하지 않는다)
#   5  적용 중 오류 (지원하지 않는 파일 모드, 복사/삭제/커밋 실패)
#
# 환경변수
#   MIRROR_SYNC_CONFIG_DIR  설정 디렉터리 경로 재지정. 기본값은 <모듈>/config.
#                           pairs.tsv 와 curation/<base>.conf 를 여기서 찾는다.
#                           (테스트가 mock 저장소를 가리킬 때 쓴다)

set -euo pipefail

# 주변 git 환경이 대상 저장소를 가로채지 못하게 한다.
# GIT_DIR/GIT_WORK_TREE 가 export 된 셸(git 훅, `git rebase --exec`, `git bisect run`)
# 안에서 실행하면 `git -C <path>` 가 -C 를 무시하고 환경이 가리키는 저장소를 쓴다.
# 그러면 pairs.tsv 가 지정한 곳이 아닌 저장소를 --apply 대상으로 삼게 된다.
unset -v GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
         GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE GIT_PREFIX

readonly SCRIPT_NAME="mirror-sync.sh"

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
MODULE_DIR="$(dirname "$(dirname "$SCRIPT_PATH")")"
CONFIG_DIR="${MIRROR_SYNC_CONFIG_DIR:-$MODULE_DIR/config}"
PAIRS_FILE="$CONFIG_DIR/pairs.tsv"
CURATION_DIR="$CONFIG_DIR/curation"

# ── 표시 라벨 (한글 폭 2칸을 감안해 각 열의 표시 폭을 맞춰 둔 문자열) ────────
readonly CLASS_COPY="복사  "     # 표시 6칸
readonly CLASS_KEEP="보존  "
readonly CLASS_SKIP="미반입"
readonly CLASS_PROTECT="미삭제"
readonly ACT_WRITE="쓰기    "    # 표시 8칸
readonly ACT_DELETE="삭제    "
readonly ACT_DELETE_NOOP="삭제생략"
readonly ACT_MANUAL="수동이식"
readonly ACT_SKIP="건너뜀  "
readonly ACT_PROTECT="보호    "
readonly ACT_PROTECT_WARN="보호경고"

# ── 큐레이션 축 ──────────────────────────────────────────────────────────────
# 여기서 미리 선언하지 **않는다**. 미리 `declare -a X=()` 로 선언해 두면 conf 가
# `X="a b c"` 처럼 스칼라로 대입해도 그 값이 X[0] 에 들어가 변수는 계속 배열로
# 보이고, 길이 검사(${#X[@]} == 1)도 타입 검사(declare -p → 'declare -a')도 전부
# 통과한다. 그러면 다섯 축이 "아무것도 매칭하지 않는 패턴 1개"로 퇴화한 채 조용히
# 전량 복사된다 — 이슈 #34 가 지적한 그 실패 방식이다.
# load_curation() 이 source 직전에 unset 하고 source 직후에 타입·길이를 검사한다.
readonly CURATION_AXES=(KEEP_MIRROR UPSTREAM_ONLY MIRROR_ONLY FORBID MUST_SURVIVE)

# 컴파일된 정규식 (compile_patterns 가 채운다)
declare -a KEEP_MIRROR_RE=()
declare -a UPSTREAM_ONLY_RE=()
declare -a MIRROR_ONLY_RE=()

# ── 쌍 메타데이터 (load_pair 가 채운다) ──────────────────────────────────────
PAIR_BASE=""
PAIR_GH_REMOTE=""
PAIR_GL_REMOTE=""
UP_REPO=""
MI_REPO=""
UP_BRANCH=""
MI_BRANCH=""

# ── 분류 결과 (main 이 채우고 어서션이 읽는다) ───────────────────────────────
declare -a copy_write_path=() copy_write_status=() copy_write_mode=()
declare -a copy_del_path=() copy_del_present=()
declare -a keep_path=() keep_status=()
declare -a skip_path=() skip_status=()
declare -a protect_path=() protect_status=()
declare -a mi_files=()          # 복사 **전** 미러 인덱스 (신규 반입 판정의 기준선)
declare -a new_write_path=()    # 복사 대상 중 미러에 아직 없는 것

# upstream --to 트리의 (모드, blob sha). load_upstream_tree 가 한 번에 채운다.
declare -A UP_TREE_MODE=()
declare -A UP_TREE_SHA=()

# ── 인자 ────────────────────────────────────────────────────────────────────
ARG_PAIR=""
ARG_FROM=""
ARG_TO=""
ARG_APPLY=0
ARG_ALLOW_NEW=0

# ── 적용 단계 상태 (복구 안내를 낼지 판단한다) ───────────────────────────────
APPLY_STARTED=0
RECOVERY_HINT_SHOWN=0

msg() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }

print_recovery_hint() {
    RECOVERY_HINT_SHOWN=1
    err "        미러 워킹트리에 이번 실행이 쓴 파일이 남아 있을 수 있다. 확인·복구:"
    err "            git -C $MI_REPO status --short"
    err "            git -C $MI_REPO reset --hard HEAD && git -C $MI_REPO clean -fd"
}

# 적용 도중 0 이 아닌 코드로 끝나면 어느 경로든 복구 안내를 낸다.
# (die 5, set -e 로 인한 중단, 예기치 못한 신호 전부 포함)
on_exit() {
    local rc=$?
    if (( rc != 0 && APPLY_STARTED == 1 && RECOVERY_HINT_SHOWN == 0 )); then
        err ""
        err "[ERROR] 적용 도중 중단됐다 (종료 코드 $rc) — 반쯤 적용된 상태일 수 있다."
        print_recovery_hint
    fi
}
trap on_exit EXIT

die() {
    local code="$1"
    shift
    err "[ERROR] $*"
    exit "$code"
}

usage() {
    cat <<EOF
$SCRIPT_NAME — 큐레이션 미러 싱크 (personal-ops 이슈 #34)

사용법
  $SCRIPT_NAME --pair <base> --from <sha> --to <sha>            # dry-run (기본)
  $SCRIPT_NAME --pair <base> --from <sha> --to <sha> --apply    # 복사 + 어서션 + 로컬 커밋
  $SCRIPT_NAME --help

옵션
  --pair <base>   config/pairs.tsv 의 base 값. curation/<base>.conf 를 함께 읽는다.
  --from <sha>    **upstream 저장소**의 시작 커밋 (이 커밋은 범위에 포함되지 않는다)
  --to   <sha>    **upstream 저장소**의 끝 커밋 (복사할 파일 내용을 이 커밋에서 읽는다)
  --apply         실제 적용. 없으면 dry-run 이며 어떤 파일도 쓰지 않는다.
  --allow-new     미러에 **없던 파일**이 이번 싱크로 새로 들어오는 것을 승인한다.
                  없으면 --apply 가 ASSERT_F 로 거부한다(dry-run 에는 영향 없음).
  -h, --help      이 도움말.

동작
  --from..--to 사이에 upstream 에서 변경된 파일을 큐레이션 축으로 4분류한다.
    복사    미러에 그대로 반영 (upstream 에서 삭제됐으면 미러에서도 삭제)
    보존    KEEP_MIRROR. 의도적으로 내용이 다르므로 덮어쓰지 않고 사람이 수동 이식
    미반입  UPSTREAM_ONLY. 미러로 넘기지 않음
    미삭제  MIRROR_ONLY. upstream 에 없다고 미러에서 지우지 않음
  "보존" 파일은 표 아래에 다시 한 번 모아서 출력한다 — 이슈 #34 의 사고가 바로
  이 목록을 놓쳐서 생겼다.

  --apply 는 복사 후 아래 어서션 6종을 미러 인덱스(git add -A 이후)에 대해 실행한다.
    ASSERT_A_UPSTREAM_ONLY_INFLOW   반입 금지 파일이 미러에 생겼는가
    ASSERT_B_FORBID_INFLOW          FORBID 문자열이 미러 추적 파일에 나타났는가
    ASSERT_C_MUST_SURVIVE_LOST      MUST_SURVIVE 문자열이 미러에서 사라졌는가
    ASSERT_D_MIRROR_ONLY_DELETED    MIRROR_ONLY 파일이 사라졌는가
    ASSERT_E_STAGED_AS_REPORTED     '쓰기/삭제' 로 보고한 대로 인덱스가 바뀌었는가
    ASSERT_F_NEW_FILE_INFLOW        미러에 없던 파일이 승인 없이 새로 들어왔는가
  하나라도 실패하면 커밋하지 않고 종료 코드 4 로 끝낸다. 경고로 넘기지 않는다.

  A~D 는 큐레이션 규칙을, E~F 는 **규칙과 독립된 오라클**을 쓴다.
  A 는 UPSTREAM_ONLY 패턴으로 검사하므로 "어느 패턴에도 안 걸려 기본값(복사)으로
  흘러간 경로"를 구조적으로 잡지 못한다. 미러는 upstream 의 부분집합이라 그 기본값이
  틀린 경우가 다수이므로, F 가 패턴이 아니라 **미러 인덱스 실측**을 기준으로
  "없던 파일이 새로 들어왔다"를 잡고 사람의 승인(--allow-new)을 요구한다.

경계 — push 는 자동화하지 않는다
  이 스크립트가 자동으로 하는 최대 범위는 **미러의 로컬 커밋**이다.
  원격 push 는 하지 않으며 --push 플래그도, push 하는 서브커맨드도 없다.
  push 는 되돌리는 비용이 급격히 커지는 외부 공개 행위라 사람이 직접 승인해 실행한다.

적용 실패 시 워킹트리
  자동 롤백하지 않고 스테이징된 채로 남긴다(사람이 git diff --cached 로 원인을 봐야 하므로).
  --apply 는 clean 한 미러에서만 시작하므로 복구는 항상 아래 한 줄이다.
      git -C <mirror> reset --hard HEAD && git -C <mirror> clean -fd

종료 코드
  0 성공 / 1 사용법 오류 / 2 설정 오류 / 3 저장소 상태 오류 / 4 어서션 실패 / 5 적용 오류

환경변수
  MIRROR_SYNC_CONFIG_DIR  설정 디렉터리 재지정 (기본: $MODULE_DIR/config)
EOF
}

# ── glob → ERE 변환 ─────────────────────────────────────────────────────────
# 매칭 규칙(config/curation/<base>.conf 주석과 문자 그대로 같아야 한다):
#   **  '/' 를 포함한 임의 문자열      → .*
#   *   '/' 를 넘지 않는 임의 문자열   → [^/]*
#   ?   '/' 가 아닌 한 글자            → [^/]
#   그 외는 전부 리터럴. ERE 메타문자만 이스케이프하고 공백·괄호·& ·= 는 그대로 둔다
#   (이 저장소에는 그런 문자가 실제로 들어 있는 경로가 3개 있다).
#   패턴은 경로 전체에 앵커된다(^...$). 부분 문자열 매칭이 아니다.
glob_to_ere() {
    local pattern="$1"
    local out="" i ch next
    for (( i = 0; i < ${#pattern}; i++ )); do
        ch="${pattern:i:1}"
        case "$ch" in
            '*')
                next="${pattern:i+1:1}"
                if [[ "$next" == '*' ]]; then
                    out+='.*'
                    i=$(( i + 1 ))
                else
                    out+='[^/]*'
                fi
                ;;
            '?')
                out+='[^/]'
                ;;
            '.'|'['|']'|'('|')'|'{'|'}'|'+'|'^'|'$'|'|'|\\)
                out+="\\$ch"
                ;;
            *)
                out+="$ch"
                ;;
        esac
    done
    printf '^%s$' "$out"
}

# 경로가 주어진 ERE 목록 중 하나라도 매칭하면 0
matches_any_re() {
    local path="$1"
    shift
    local re
    for re in "$@"; do
        if [[ "$path" =~ $re ]]; then
            return 0
        fi
    done
    return 1
}

compile_patterns() {
    local p
    KEEP_MIRROR_RE=()
    UPSTREAM_ONLY_RE=()
    MIRROR_ONLY_RE=()
    for p in "${KEEP_MIRROR[@]}"; do KEEP_MIRROR_RE+=("$(glob_to_ere "$p")"); done
    for p in "${UPSTREAM_ONLY[@]}"; do UPSTREAM_ONLY_RE+=("$(glob_to_ere "$p")"); done
    for p in "${MIRROR_ONLY[@]}"; do MIRROR_ONLY_RE+=("$(glob_to_ere "$p")"); done
}

# ── 설정 로드 ───────────────────────────────────────────────────────────────
load_pair() {
    local want="$1"
    local base gh gl up mi upbr mibr rest
    local -a available=()
    local found=0

    [[ -f "$PAIRS_FILE" ]] || die 2 "pairs.tsv 를 찾을 수 없다: $PAIRS_FILE"

    while IFS=$'\t' read -r base gh gl up mi upbr mibr rest || [[ -n "${base:-}" ]]; do
        if [[ -z "${base//[[:space:]]/}" ]]; then
            continue
        fi
        if [[ "$base" == \#* ]]; then
            continue
        fi
        available+=("$base")
        if [[ "$base" != "$want" ]]; then
            continue
        fi
        if [[ -z "$gh" || -z "$gl" || -z "$up" || -z "$mi" || -z "$upbr" || -z "$mibr" ]]; then
            die 2 "pairs.tsv 의 '$base' 행에 빈 컬럼이 있다 (탭 7컬럼이어야 한다)"
        fi
        PAIR_BASE="$base"
        PAIR_GH_REMOTE="$gh"
        PAIR_GL_REMOTE="$gl"
        UP_REPO="$up"
        MI_REPO="$mi"
        UP_BRANCH="$upbr"
        MI_BRANCH="$mibr"
        found=1
    done < "$PAIRS_FILE"

    if (( found == 0 )); then
        err "[ERROR] pairs.tsv 에 '$want' 쌍이 없다: $PAIRS_FILE"
        if (( ${#available[@]} > 0 )); then
            err "        사용 가능한 pair: ${available[*]}"
        else
            err "        사용 가능한 pair 가 하나도 없다 (전부 주석 처리됨)"
        fi
        exit 2
    fi
}

# 변수의 선언 타입을 한 글자로 돌려준다: 'a'(인덱스 배열) 'A'(연관 배열)
# '-'(스칼라 등 그 밖) 'unset'(선언 안 됨).
var_kind() {
    local decl flags
    decl="$(declare -p "$1" 2>/dev/null)" || { printf 'unset'; return 0; }
    decl="${decl#declare }"
    flags="${decl%% *}"
    case "$flags" in
        -*A*) printf 'A' ;;
        -*a*) printf 'a' ;;
        *)    printf '-' ;;
    esac
}

load_curation() {
    local base="$1"
    local conf="$CURATION_DIR/$base.conf"
    local ax kind n

    [[ -f "$conf" ]] || die 2 "큐레이션 규칙 파일이 없다: $conf"

    # source 전에 반드시 지운다. 남아 있으면 conf 가 스칼라로 대입해도 배열로 보인다
    # (자세한 이유는 위 CURATION_AXES 선언부 주석 참조).
    unset -v "${CURATION_AXES[@]}" MUST_SURVIVE_IN

    # shellcheck source=/dev/null
    if ! source "$conf"; then
        die 2 "큐레이션 규칙 파일을 읽지 못했다: $conf"
    fi

    for ax in "${CURATION_AXES[@]}"; do
        kind="$(var_kind "$ax")"
        case "$kind" in
            a) ;;
            unset)
                die 2 "$conf: $ax 축이 정의되지 않았다 — 다섯 축을 전부 bash 배열로 채워야 한다"
                ;;
            *)
                die 2 "$conf: $ax 축이 bash 배열이 아니다(선언 타입 '$kind'). \
$ax=( \"a\" \"b\" ) 형식이어야 한다 — 공백 구분 문자열 하나로 쓰면 항목 1개짜리 축이 되어 \
아무것도 매칭하지 않은 채 조용히 전량 복사된다"
                ;;
        esac
    done

    # 축이 통째로 비면 "미반입 0건 / 보존 0건" 으로 조용히 전량 복사된다.
    # 그것이 이슈 #34 가 지적한 실패 방식이므로 여기서 막는다.
    (( ${#KEEP_MIRROR[@]}   > 0 )) || die 2 "$conf: KEEP_MIRROR 가 비었다"
    (( ${#UPSTREAM_ONLY[@]} > 0 )) || die 2 "$conf: UPSTREAM_ONLY 가 비었다"
    (( ${#MIRROR_ONLY[@]}   > 0 )) || die 2 "$conf: MIRROR_ONLY 가 비었다"
    (( ${#FORBID[@]}        > 0 )) || die 2 "$conf: FORBID 가 비었다"
    (( ${#MUST_SURVIVE[@]}  > 0 )) || die 2 "$conf: MUST_SURVIVE 가 비었다"

    # MUST_SURVIVE_IN 은 선택 축이다. MUST_SURVIVE 와 **같은 길이의 병렬 배열**로,
    # i 번째 원소는 MUST_SURVIVE[i] 문자열이 반드시 살아 있어야 하는 경로다.
    # 빈 문자열이면 저장소 전체에서 찾는다(예전 동작). 경로가 묶여 있으면 ASSERT_C 가
    # 그 파일만 보므로, 같은 문구가 다른 파일에 생겨도 검사가 무뎌지지 않는다.
    # (연관 배열을 쓰지 않는 이유: conf 는 함수 안에서 source 되므로 `declare -A` 는
    #  지역 변수가 되어 조용히 사라진다. 일반 대입은 전역이라 다섯 축과 형식이 같다.)
    kind="$(var_kind MUST_SURVIVE_IN)"
    case "$kind" in
        a)
            (( ${#MUST_SURVIVE_IN[@]} == ${#MUST_SURVIVE[@]} )) || die 2 \
                "$conf: MUST_SURVIVE_IN 의 길이(${#MUST_SURVIVE_IN[@]})가 MUST_SURVIVE(${#MUST_SURVIVE[@]})와 다르다 — 인덱스로 짝지으므로 길이가 같아야 한다"
            ;;
        unset)
            MUST_SURVIVE_IN=()
            for (( n = 0; n < ${#MUST_SURVIVE[@]}; n++ )); do
                MUST_SURVIVE_IN+=("")
            done
            ;;
        *)
            die 2 "$conf: MUST_SURVIVE_IN 은 bash 배열이어야 한다(선언 타입 '$kind') — MUST_SURVIVE 와 같은 길이의 병렬 배열"
            ;;
    esac
}

# 어느 트리에서도 매칭되지 않는 경로 패턴을 경고한다.
# 한 글자 오타(artifacts/** → artifact/**)는 에러 없이 "그 패턴만 죽은" 상태를 만들고,
# 그러면 그 축이 덮던 파일들이 조용히 기본값(복사)으로 흘러간다.
warn_unmatched_patterns() {  # $1 = upstream tree-ish
    local treeish="$1"
    local -a pool=() unmatched=()
    local p ax idx re hit
    while IFS= read -r -d '' p; do pool+=("$p"); done \
        < <(git -C "$UP_REPO" ls-tree -r -z --name-only "$treeish")
    pool+=(${mi_files[@]+"${mi_files[@]}"})

    for ax in KEEP_MIRROR UPSTREAM_ONLY MIRROR_ONLY; do
        declare -n _pats="$ax"
        declare -n _res="${ax}_RE"
        for idx in "${!_pats[@]}"; do
            re="${_res[$idx]}"
            hit=0
            for p in "${pool[@]}"; do
                if [[ "$p" =~ $re ]]; then hit=1; break; fi
            done
            if (( hit == 0 )); then
                unmatched+=("$ax: ${_pats[$idx]}")
            fi
        done
        unset -n _pats _res
    done

    if (( ${#unmatched[@]} > 0 )); then
        msg "  [주의] 어느 트리(upstream $treeish / mirror 인덱스)와도 매칭하지 않는 패턴 ${#unmatched[@]}개:"
        for p in "${unmatched[@]}"; do
            msg "         $p"
        done
        msg "         오타이거나 이미 없어진 경로다. 죽은 패턴은 그 축을 조용히 무력화한다."
    fi
}

# ── 저장소 검증 ─────────────────────────────────────────────────────────────
# 경로가 git 저장소인지'만' 보면 부족하다: rev-parse --git-dir 은 어떤 저장소의
# **하위 디렉터리**에서도 성공한다. 미러 클론의 .git 이 사라졌거나 클론을 다른
# 체크아웃 안에 두면 --apply 가 엉뚱한 상위 저장소에 커밋한다.
# 그래서 워크트리 최상위가 설정 경로와 **정확히 같은지**까지 확인한다.
require_repo() {
    local role="$1" path="$2"
    local top real_path real_top
    [[ -d "$path" ]] || die 3 "$role 로컬 경로가 없다: $path"
    if ! top="$(git -C "$path" rev-parse --show-toplevel 2>/dev/null)"; then
        die 3 "$role 경로가 git 워크트리가 아니다: $path"
    fi
    real_path="$(readlink -f "$path")"
    real_top="$(readlink -f "$top")"
    if [[ "$real_path" != "$real_top" ]]; then
        die 3 "$role 경로가 저장소 최상위가 아니다: $path (최상위: $real_top) — pairs.tsv 를 확인해라"
    fi
}

resolve_commit() {
    local repo="$1" ref="$2"
    git -C "$repo" rev-parse --verify --quiet "${ref}^{commit}"
}

# upstream --to 트리 전체를 한 번에 읽어 경로 → (모드, blob sha) 로 만든다.
# 경로마다 ls-tree 를 부르면 수백 번의 프로세스 기동이 되고, 무엇보다 blob sha 를
# 미리 갖고 있어야 ASSERT_E 가 "인덱스에 들어간 내용이 정말 upstream 판인가"를
# 확인할 수 있다.
load_upstream_tree() {  # $1 = tree-ish
    local rec meta path
    UP_TREE_MODE=()
    UP_TREE_SHA=()
    while IFS= read -r -d '' rec; do
        meta="${rec%%$'\t'*}"          # "<mode> <type> <sha>"
        path="${rec#*$'\t'}"
        UP_TREE_MODE["$path"]="${meta%% *}"
        UP_TREE_SHA["$path"]="${meta##* }"
    done < <(git -C "$UP_REPO" ls-tree -r -z "$1")
}

# ── 분류 ────────────────────────────────────────────────────────────────────
# 우선순위: UPSTREAM_ONLY > MIRROR_ONLY > KEEP_MIRROR > 복사
classify_path() {
    local path="$1"
    if matches_any_re "$path" "${UPSTREAM_ONLY_RE[@]}"; then
        printf 'SKIP'
    elif matches_any_re "$path" "${MIRROR_ONLY_RE[@]}"; then
        printf 'PROTECT'
    elif matches_any_re "$path" "${KEEP_MIRROR_RE[@]}"; then
        printf 'KEEP'
    else
        printf 'COPY'
    fi
}

print_row() {
    printf '  %s  %s  %-4s  %s\n' "$1" "$2" "$3" "$4"
}

# 복사 대상 중 미러 .gitignore 에 걸리는 것을 찾는다.
# check-ignore 는 **추적 중인 경로를 보고하지 않는다**(--no-index 를 주지 않았다).
# 이미 추적되는 파일은 무시 규칙과 무관하게 git add -A 가 스테이징하므로, 여기서
# 잡아야 하는 것은 정확히 "아직 추적되지 않으면서 무시되는" 경로다.
list_ignored_copy_targets() {
    (( ${#copy_write_path[@]} > 0 )) || return 0
    printf '%s\0' "${copy_write_path[@]}" \
        | git -C "$MI_REPO" check-ignore -z --stdin 2>/dev/null || true
}

# ── 어서션 ──────────────────────────────────────────────────────────────────
# 미러 인덱스(git add -A 이후)를 대상으로 실행한다. 새로 복사된 파일도 인덱스에
# 들어가 있으므로 --cached 검사로 전부 커버된다.
run_assertions() {
    local failed=0
    local -a index_paths=()
    local -A index_set=()
    local -A index_mode=()
    local -A index_sha=()
    local -A before_set=()
    local p s hits bound rec meta

    # ls-files -s 는 "<mode> <sha> <stage>\t<path>" 를 준다. 경로뿐 아니라 모드·내용
    # 해시까지 한 번에 받아 두면 ASSERT_E 가 추가 프로세스 없이 내용까지 대조한다.
    while IFS= read -r -d '' rec; do
        meta="${rec%%$'\t'*}"
        p="${rec#*$'\t'}"
        index_paths+=("$p")
        index_set["$p"]=1
        index_mode["$p"]="${meta%% *}"
        meta="${meta#* }"              # "<sha> <stage>"
        index_sha["$p"]="${meta%% *}"
    done < <(git -C "$MI_REPO" ls-files -s -z)
    for p in ${mi_files[@]+"${mi_files[@]}"}; do
        before_set["$p"]=1
    done

    msg ""
    msg "어서션 6종 (대상: 미러 인덱스, 파일 ${#index_paths[@]}개)"

    # A. 반입 금지 유입 — UPSTREAM_ONLY 매칭 파일이 미러에 생겼는가
    local -a a_hits=()
    for p in "${index_paths[@]}"; do
        if matches_any_re "$p" "${UPSTREAM_ONLY_RE[@]}"; then
            a_hits+=("$p")
        fi
    done
    if (( ${#a_hits[@]} == 0 )); then
        msg "  [PASS] ASSERT_A_UPSTREAM_ONLY_INFLOW   반입 금지 파일 유입 없음"
    else
        msg "  [FAIL] ASSERT_A_UPSTREAM_ONLY_INFLOW   반입 금지 파일 ${#a_hits[@]}개가 미러에 있다"
        for p in "${a_hits[@]}"; do msg "         - $p"; done
        failed=$(( failed + 1 ))
    fi

    # B. FORBID 문자열 유입
    local -a b_hits=()
    for s in "${FORBID[@]}"; do
        if hits="$(git -C "$MI_REPO" grep --cached -l -F -e "$s" 2>/dev/null)"; then
            b_hits+=("$s")
            msg "  [FAIL] ASSERT_B_FORBID_INFLOW          금지 문자열 발견: \"$s\""
            while IFS= read -r p; do
                [[ -n "$p" ]] && msg "         - $p"
            done <<< "$hits"
        fi
    done
    if (( ${#b_hits[@]} == 0 )); then
        msg "  [PASS] ASSERT_B_FORBID_INFLOW          금지 문자열 유입 없음"
    else
        failed=$(( failed + 1 ))
    fi

    # C. MUST_SURVIVE 소실 — 이번 사고를 직접 막는 어서션.
    #    MUST_SURVIVE_IN 에 경로가 묶여 있으면 그 파일 안에서만 찾는다. 같은 문구가
    #    다른 파일에 생겼다고 해서 정작 그 문단의 소실을 놓치지 않기 위함이다.
    local -a c_lost=()
    local ci
    for ci in "${!MUST_SURVIVE[@]}"; do
        s="${MUST_SURVIVE[$ci]}"
        bound="${MUST_SURVIVE_IN[$ci]:-}"
        if [[ -n "$bound" ]]; then
            if ! git -C "$MI_REPO" grep --cached -q -F -e "$s" -- ":(literal)$bound" 2>/dev/null; then
                c_lost+=("$s   (요구 위치: $bound)")
            fi
        else
            if ! git -C "$MI_REPO" grep --cached -q -F -e "$s" 2>/dev/null; then
                c_lost+=("$s")
            fi
        fi
    done
    if (( ${#c_lost[@]} == 0 )); then
        msg "  [PASS] ASSERT_C_MUST_SURVIVE_LOST      필수 문자열 ${#MUST_SURVIVE[@]}종 모두 잔존"
    else
        msg "  [FAIL] ASSERT_C_MUST_SURVIVE_LOST      필수 문자열 ${#c_lost[@]}종이 미러에서 사라졌다"
        for s in "${c_lost[@]}"; do msg "         - \"$s\""; done
        failed=$(( failed + 1 ))
    fi

    # D. MIRROR_ONLY 삭제 — 패턴마다 인덱스에 매칭 파일이 최소 1개 있어야 한다
    local -a d_lost=()
    local re idx found
    for idx in "${!MIRROR_ONLY[@]}"; do
        re="${MIRROR_ONLY_RE[$idx]}"
        found=0
        for p in "${index_paths[@]}"; do
            if [[ "$p" =~ $re ]]; then
                found=1
                break
            fi
        done
        if (( found == 0 )); then
            d_lost+=("${MIRROR_ONLY[$idx]}")
        fi
    done
    if (( ${#d_lost[@]} == 0 )); then
        msg "  [PASS] ASSERT_D_MIRROR_ONLY_DELETED    미러 전용 파일 ${#MIRROR_ONLY[@]}종 모두 잔존"
    else
        msg "  [FAIL] ASSERT_D_MIRROR_ONLY_DELETED    미러 전용 파일 ${#d_lost[@]}종이 사라졌다"
        for s in "${d_lost[@]}"; do msg "         - $s"; done
        failed=$(( failed + 1 ))
    fi

    # E. 보고한 대로 스테이징됐는가 — 존재 여부가 아니라 **내용**까지 본다.
    #    '쓰기' 로 보고한 경로는 인덱스에 upstream --to 판의 blob·모드로 들어가 있어야
    #    하고, 실제로 지운 경로는 인덱스에서 빠져 있어야 한다.
    #    git add -A 가 보고와 다르게 동작하는 경로가 실재한다:
    #      - 미러 .gitignore 에 걸리는 새 파일 → 스테이징하지 않는다(앞의 게이트가 먼저 막는다)
    #      - skip-worktree 가 걸린 추적 파일 → **exit 0 인 채로 변경을 통째로 무시한다**
    #    둘 다 화면에는 "쓰기 <경로>" + [PASS] + "커밋 완료" 가 나오고 커밋에는 그
    #    변경이 없다. 이 모듈이 막으려는 조용한 실패와 같은 종류라 어서션으로 잡는다.
    local -a e_missing=() e_stale=() e_lingering=()
    for idx in "${!copy_write_path[@]}"; do
        p="${copy_write_path[$idx]}"
        if [[ -z "${index_set[$p]:-}" ]]; then
            e_missing+=("$p")
        elif [[ "${index_sha[$p]}" != "${UP_TREE_SHA[$p]:-}" || "${index_mode[$p]}" != "${UP_TREE_MODE[$p]:-}" ]]; then
            e_stale+=("$p")
        fi
    done
    for idx in "${!copy_del_path[@]}"; do
        [[ "${copy_del_present[$idx]}" == "1" ]] || continue
        p="${copy_del_path[$idx]}"
        [[ -z "${index_set[$p]:-}" ]] || e_lingering+=("$p")
    done
    if (( ${#e_missing[@]} == 0 && ${#e_stale[@]} == 0 && ${#e_lingering[@]} == 0 )); then
        msg "  [PASS] ASSERT_E_STAGED_AS_REPORTED     보고한 쓰기 ${#copy_write_path[@]}건이 전부 upstream 판 그대로 인덱스에 있음"
    else
        msg "  [FAIL] ASSERT_E_STAGED_AS_REPORTED     보고와 인덱스가 어긋난다 (누락 ${#e_missing[@]} / 내용불일치 ${#e_stale[@]} / 삭제잔존 ${#e_lingering[@]})"
        for p in ${e_missing[@]+"${e_missing[@]}"}; do msg "         - 쓰기로 보고했으나 인덱스에 없음: $p"; done
        for p in ${e_stale[@]+"${e_stale[@]}"}; do msg "         - 쓰기로 보고했으나 인덱스 내용이 upstream 판이 아님: $p"; done
        for p in ${e_lingering[@]+"${e_lingering[@]}"}; do msg "         - 삭제했으나 인덱스에 남음: $p"; done
        failed=$(( failed + 1 ))
    fi

    # F. 신규 반입 — 미러에 없던 파일이 이번 싱크로 들어왔는가.
    #    A 와 달리 큐레이션 패턴을 쓰지 않는다. 기준선은 복사 전 미러 인덱스 실측이라
    #    "어느 패턴에도 안 걸려 기본값(복사)으로 흘러간 경로"까지 잡힌다.
    local -a f_new=()
    for p in "${index_paths[@]}"; do
        [[ -n "${before_set[$p]:-}" ]] || f_new+=("$p")
    done
    if (( ${#f_new[@]} == 0 )); then
        msg "  [PASS] ASSERT_F_NEW_FILE_INFLOW        미러에 없던 파일의 신규 반입 없음"
    elif (( ARG_ALLOW_NEW == 1 )); then
        msg "  [PASS] ASSERT_F_NEW_FILE_INFLOW        신규 반입 ${#f_new[@]}개 — --allow-new 로 승인됨"
        for p in "${f_new[@]}"; do msg "         + $p"; done
    else
        msg "  [FAIL] ASSERT_F_NEW_FILE_INFLOW        미러에 없던 파일 ${#f_new[@]}개가 승인 없이 들어왔다"
        for p in "${f_new[@]}"; do msg "         + $p"; done
        msg "         목록을 확인하고 반입해도 되면 --allow-new 를 붙여 다시 실행해라."
        msg "         반입하면 안 되는 것이 섞여 있으면 conf 의 UPSTREAM_ONLY 에 먼저 추가해라."
        failed=$(( failed + 1 ))
    fi

    return "$failed"
}

# ── 메인 ────────────────────────────────────────────────────────────────────
main() {
    while (( $# > 0 )); do
        case "$1" in
            --pair)
                [[ $# -ge 2 ]] || die 1 "--pair 에 값이 필요하다"
                ARG_PAIR="$2"
                shift 2
                ;;
            --from)
                [[ $# -ge 2 ]] || die 1 "--from 에 값이 필요하다"
                ARG_FROM="$2"
                shift 2
                ;;
            --to)
                [[ $# -ge 2 ]] || die 1 "--to 에 값이 필요하다"
                ARG_TO="$2"
                shift 2
                ;;
            --apply)
                ARG_APPLY=1
                shift
                ;;
            --allow-new)
                ARG_ALLOW_NEW=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --push|--push=*)
                die 1 "push 는 이 스크립트가 하지 않는다. 최대 범위는 미러의 로컬 커밋이다 (--help 참조)"
                ;;
            *)
                die 1 "알 수 없는 인자: $1  (--help 참조)"
                ;;
        esac
    done

    [[ -n "$ARG_PAIR" ]] || die 1 "--pair 가 필요하다 (--help 참조)"
    [[ -n "$ARG_FROM" ]] || die 1 "--from 이 필요하다 (--help 참조)"
    [[ -n "$ARG_TO" ]]   || die 1 "--to 가 필요하다 (--help 참조)"

    load_pair "$ARG_PAIR"
    load_curation "$PAIR_BASE"
    compile_patterns

    require_repo "upstream" "$UP_REPO"
    require_repo "mirror" "$MI_REPO"

    local from_sha to_sha from_short to_short
    if ! from_sha="$(resolve_commit "$UP_REPO" "$ARG_FROM")"; then
        die 3 "--from '$ARG_FROM' 를 upstream 저장소에서 찾을 수 없다: $UP_REPO"
    fi
    if ! to_sha="$(resolve_commit "$UP_REPO" "$ARG_TO")"; then
        die 3 "--to '$ARG_TO' 를 upstream 저장소에서 찾을 수 없다: $UP_REPO"
    fi
    from_short="$(git -C "$UP_REPO" rev-parse --short "$from_sha")"
    to_short="$(git -C "$UP_REPO" rev-parse --short "$to_sha")"

    local mode_label="dry-run — 아무 파일도 쓰지 않는다"
    if (( ARG_APPLY == 1 )); then
        mode_label="apply — 복사 + 어서션 + 미러 로컬 커밋 (push 없음)"
    fi

    msg "mirror-sync — 큐레이션 미러 싱크"
    msg "  쌍       : $PAIR_BASE"
    msg "  upstream : $UP_REPO ($UP_BRANCH)"
    msg "             $PAIR_GH_REMOTE"
    msg "  mirror   : $MI_REPO ($MI_BRANCH)"
    msg "             $PAIR_GL_REMOTE"
    msg "  범위     : $from_short .. $to_short  (upstream 커밋)"
    msg "  모드     : $mode_label"

    # --from 이 --to 의 조상이 아니면 diff 방향이 뒤집힌다. 그 상태로 --apply 하면
    # upstream 의 '추가'가 '삭제'로 나와 미러에 정상적으로 있는 파일들이 실제로
    # 지워지고 커밋된다. 어서션 A~D 는 이 상황을 구조적으로 잡지 못하므로
    # (A/B 는 유입만, C 는 KEEP_MIRROR 안의 문자열만, D 는 MIRROR_ONLY 만 본다)
    # --apply 에서는 경고가 아니라 거부한다.
    if ! git -C "$UP_REPO" merge-base --is-ancestor "$from_sha" "$to_sha" 2>/dev/null; then
        if (( ARG_APPLY == 1 )); then
            err "[ERROR] --from 이 --to 의 조상이 아니다: $from_short .. $to_short"
            err "        이 상태의 diff 는 방향이 뒤집혀 upstream 의 추가가 삭제로 나온다."
            err "        --apply 는 미러 파일을 실제로 지우므로 진행하지 않는다."
            err "        인자 순서를 확인해라 (--from 이 과거, --to 가 최신)."
            exit 3
        fi
        msg "  [주의] --from 이 --to 의 조상이 아니다. diff 방향을 확인해라."
        msg "         (--apply 는 이 상태를 거부한다 — 뒤집힌 범위는 대량 삭제가 된다)"
    fi

    # --apply 사전 검증: 미러가 clean 하지 않으면 남의 변경을 커밋에 끌어들인다
    if (( ARG_APPLY == 1 )); then
        local cur_branch
        cur_branch="$(git -C "$MI_REPO" symbolic-ref --short -q HEAD || true)"
        if [[ "$cur_branch" != "$MI_BRANCH" ]]; then
            die 3 "미러 브랜치가 '$MI_BRANCH' 가 아니다 (현재: ${cur_branch:-detached HEAD}): $MI_REPO"
        fi
        if [[ -n "$(git -C "$MI_REPO" status --porcelain)" ]]; then
            err "[ERROR] 미러 워킹트리가 clean 하지 않다: $MI_REPO"
            err "        남의 변경을 싱크 커밋에 끌어들일 수 없으므로 --apply 를 거부한다."
            err "        (직전 실행이 적용 도중 실패해 남긴 것일 수도 있다 — 그 경우"
            err "         git -C $MI_REPO reset --hard HEAD && git -C $MI_REPO clean -fd 로 지운다)"
            err "        아래 변경을 먼저 정리해라:"
            git -C "$MI_REPO" status --porcelain >&2
            exit 3
        fi
    fi

    # ── upstream diff 수집 ─────────────────────────────────────────────────
    # --no-renames 로 R/C 상태를 없애 'STATUS\0PATH\0' 쌍 파싱을 결정적으로 만든다.
    local -a diff_status=() diff_path=()
    local st pth
    while IFS= read -r -d '' st && IFS= read -r -d '' pth; do
        diff_status+=("$st")
        diff_path+=("$pth")
    done < <(git -C "$UP_REPO" diff --no-renames --name-status -z "$from_sha" "$to_sha" --)

    # 복사 **전** 미러 인덱스. 신규 반입 판정(ASSERT_F)의 기준선이자 미삭제 정보성
    # 행의 원본이다. 패턴이 아니라 실측이므로 분류 규칙과 독립된 오라클이 된다.
    local mp
    mi_files=()
    while IFS= read -r -d '' mp; do
        mi_files+=("$mp")
    done < <(git -C "$MI_REPO" ls-files -z)
    local -A mi_set=()
    for mp in ${mi_files[@]+"${mi_files[@]}"}; do
        mi_set["$mp"]=1
    done

    # ── 분류 ───────────────────────────────────────────────────────────────
    copy_write_path=(); copy_write_status=(); copy_write_mode=()
    copy_del_path=(); copy_del_present=()
    keep_path=(); keep_status=()
    skip_path=(); skip_status=()
    protect_path=(); protect_status=()
    new_write_path=()
    local i cls

    for i in "${!diff_path[@]}"; do
        pth="${diff_path[$i]}"
        st="${diff_status[$i]}"
        cls="$(classify_path "$pth")"
        case "$cls" in
            SKIP)
                skip_path+=("$pth")
                skip_status+=("$st")
                ;;
            PROTECT)
                protect_path+=("$pth")
                protect_status+=("$st")
                ;;
            KEEP)
                keep_path+=("$pth")
                keep_status+=("$st")
                ;;
            COPY)
                if [[ "$st" == D* ]]; then
                    copy_del_path+=("$pth")
                    if [[ -e "$MI_REPO/$pth" ]]; then
                        copy_del_present+=("1")
                    else
                        copy_del_present+=("0")
                    fi
                else
                    copy_write_path+=("$pth")
                    copy_write_status+=("$st")
                    if [[ -z "${mi_set[$pth]:-}" ]]; then
                        new_write_path+=("$pth")
                    fi
                fi
                ;;
        esac
    done

    # 미삭제 행은 diff 에서는 거의 나오지 않는다(MIRROR_ONLY 는 upstream 에 없으므로).
    # 그래서 미러에 실제로 존재하는 MIRROR_ONLY 파일을 정보성으로 항상 같이 보여준다.
    # 단, 이미 diff 에서 미삭제로 잡힌 경로는 중복 출력하지 않는다.
    local -a mo_present=() mo_missing=()
    local re idx found dup dp
    for idx in "${!MIRROR_ONLY[@]}"; do
        re="${MIRROR_ONLY_RE[$idx]}"
        found=0
        for mp in ${mi_files[@]+"${mi_files[@]}"}; do
            if [[ "$mp" =~ $re ]]; then
                found=1
                dup=0
                for dp in ${protect_path[@]+"${protect_path[@]}"}; do
                    if [[ "$dp" == "$mp" ]]; then
                        dup=1
                        break
                    fi
                done
                if (( dup == 0 )); then
                    mo_present+=("$mp")
                fi
            fi
        done
        if (( found == 0 )); then
            mo_missing+=("${MIRROR_ONLY[$idx]}")
        fi
    done

    warn_unmatched_patterns "$to_sha"

    # ── 표 출력 ────────────────────────────────────────────────────────────
    msg ""
    msg "분류 표 — upstream 변경 파일 ${#diff_path[@]}개"
    msg "  분류    동작      상태  경로"
    msg "  ------  --------  ----  --------------------------------------------------"

    for i in "${!copy_write_path[@]}"; do
        print_row "$CLASS_COPY" "$ACT_WRITE" "${copy_write_status[$i]}" "${copy_write_path[$i]}"
    done
    local n_del_real=0 n_del_noop=0
    for i in "${!copy_del_path[@]}"; do
        if [[ "${copy_del_present[$i]}" == "1" ]]; then
            print_row "$CLASS_COPY" "$ACT_DELETE" "D" "${copy_del_path[$i]}"
            n_del_real=$(( n_del_real + 1 ))
        else
            print_row "$CLASS_COPY" "$ACT_DELETE_NOOP" "D" "${copy_del_path[$i]} (미러에 이미 없음)"
            n_del_noop=$(( n_del_noop + 1 ))
        fi
    done
    for i in "${!keep_path[@]}"; do
        print_row "$CLASS_KEEP" "$ACT_MANUAL" "${keep_status[$i]}" "${keep_path[$i]}"
    done
    for i in "${!skip_path[@]}"; do
        print_row "$CLASS_SKIP" "$ACT_SKIP" "${skip_status[$i]}" "${skip_path[$i]}"
    done
    for i in "${!protect_path[@]}"; do
        print_row "$CLASS_PROTECT" "$ACT_PROTECT" "${protect_status[$i]}" "${protect_path[$i]}"
    done
    for mp in ${mo_present[@]+"${mo_present[@]}"}; do
        print_row "$CLASS_PROTECT" "$ACT_PROTECT" "-" "$mp"
    done
    for mp in ${mo_missing[@]+"${mo_missing[@]}"}; do
        print_row "$CLASS_PROTECT" "$ACT_PROTECT_WARN" "-" "$mp (미러에 없다 — MIRROR_ONLY 규칙 확인 필요)"
    done

    # 합계의 '삭제' 는 **실제로 지우는 건수**다. 미러에 이미 없어서 아무것도 하지
    # 않는 '삭제생략' 행은 따로 센다 — 표를 다 읽지 않고 합계만 보는 사람에게
    # "파일이 지워진다"로 읽히면 안 된다. 커밋 본문도 같은 값을 쓴다.
    local n_copy=$(( ${#copy_write_path[@]} + ${#copy_del_path[@]} ))
    local n_protect=$(( ${#protect_path[@]} + ${#mo_present[@]} + ${#mo_missing[@]} ))
    local del_summary="삭제 $n_del_real"
    if (( n_del_noop > 0 )); then
        del_summary="삭제 $n_del_real / 삭제생략 $n_del_noop"
    fi
    msg "  ------  --------  ----  --------------------------------------------------"
    msg "  합계: 복사 $n_copy (쓰기 ${#copy_write_path[@]} / $del_summary)"
    msg "        보존 ${#keep_path[@]} · 미반입 ${#skip_path[@]} · 미삭제 $n_protect"

    # ── 수동 이식 목록 (맨 끝에 다시 모아서) ───────────────────────────────
    print_manual_block() {
        msg ""
        if (( ${#keep_path[@]} == 0 )); then
            msg "수동 이식 필요 (KEEP_MIRROR): 없음"
            return 0
        fi
        msg "==============================================================================="
        msg " 수동 이식 필요 — KEEP_MIRROR ${#keep_path[@]}개"
        msg "-------------------------------------------------------------------------------"
        msg " 이 파일들은 미러와 upstream 이 **의도적으로 다르다**. 스크립트가 덮어쓰지"
        msg " 않았으므로 upstream 변경분을 사람이 읽고 직접 이식해야 한다."
        msg " 이슈 #34 의 사고가 바로 이 목록을 놓쳐서 생겼다."
        msg ""
        for i in "${!keep_path[@]}"; do
            msg "   [${keep_status[$i]}] ${keep_path[$i]}"
            msg "       git -C $UP_REPO diff $from_short $to_short -- \"${keep_path[$i]}\""
        done
        msg "==============================================================================="
    }

    # 미러에 없던 파일이 새로 들어오는 목록. --apply 는 --allow-new 없이 거부한다.
    print_new_inflow_block() {
        (( ${#new_write_path[@]} > 0 )) || return 0
        msg ""
        msg "==============================================================================="
        msg " 신규 반입 — 미러에 없던 파일 ${#new_write_path[@]}개"
        msg "-------------------------------------------------------------------------------"
        msg " 어느 큐레이션 축에도 걸리지 않아 기본값(복사)으로 분류된 **새 파일**이다."
        msg " 미러는 upstream 의 부분집합이므로 이 기본값이 틀린 경우가 많다 — 내부 문서가"
        msg " 공개 미러로 새어나가는 경로가 정확히 여기다. 목록을 눈으로 확인해라."
        msg ""
        for i in "${!new_write_path[@]}"; do
            msg "   + ${new_write_path[$i]}"
        done
        msg ""
        msg " 반입해도 되면 --apply 에 --allow-new 를 함께 준다."
        msg " 반입하면 안 되면 conf 의 UPSTREAM_ONLY 에 먼저 추가한다."
        msg "==============================================================================="
    }

    # 미러 .gitignore 에 걸리는 복사 대상 — 있으면 조용한 유실이 된다.
    local -a ignored_targets=()
    local ig
    while IFS= read -r -d '' ig; do
        ignored_targets+=("$ig")
    done < <(list_ignored_copy_targets)

    if (( ARG_APPLY == 0 )); then
        print_manual_block
        print_new_inflow_block
        if (( ${#ignored_targets[@]} > 0 )); then
            msg ""
            msg "[주의] 복사 대상 ${#ignored_targets[@]}개가 미러 .gitignore 에 걸린다 — --apply 는 거부한다:"
            for ig in "${ignored_targets[@]}"; do
                msg "       - $ig"
            done
        fi
        msg ""
        msg "dry-run 종료 — 아무 파일도 쓰지 않았다. 적용하려면 --apply 를 붙여라."
        return 0
    fi

    # ── 사전 검사 (한 파일도 쓰기 전에 끝낸다) ─────────────────────────────
    # 여기서 걸리면 워킹트리는 손대지 않은 상태 그대로다. 복사 도중에 죽으면
    # 반쯤 적용된 상태가 남으므로, 확인 가능한 것은 전부 미리 확인한다.
    if (( ${#ignored_targets[@]} > 0 )); then
        err "[ERROR] 복사 대상 ${#ignored_targets[@]}개가 미러 .gitignore 에 걸린다: $MI_REPO"
        for ig in "${ignored_targets[@]}"; do
            err "        - $ig"
        done
        err "        복사해도 git add -A 가 스테이징하지 않아 커밋에서 조용히 빠진다."
        err "        (무시 파일이라 git status 에도 안 잡혀 다음 실행의 clean 검사까지 통과한다)"
        err "        미러에 두지 않을 파일이면 conf 의 UPSTREAM_ONLY 에 추가하고,"
        err "        두어야 할 파일이면 미러의 .gitignore 를 먼저 손봐라."
        exit 3
    fi

    local mode
    load_upstream_tree "$to_sha"
    copy_write_mode=()
    for i in "${!copy_write_path[@]}"; do
        pth="${copy_write_path[$i]}"
        mode="${UP_TREE_MODE[$pth]:-}"
        case "$mode" in
            100644|100755) ;;
            "") die 5 "upstream $to_short 트리에 없는 경로다: $pth" ;;
            *)  die 5 "지원하지 않는 파일 모드($mode): $pth — 심볼릭 링크·서브모듈은 수동 처리해라" ;;
        esac
        copy_write_mode+=("$mode")
    done

    # ── 적용 ───────────────────────────────────────────────────────────────
    msg ""
    msg "적용 중 ..."
    APPLY_STARTED=1

    # 삭제를 먼저 한다. upstream 이 파일을 같은 이름의 디렉터리로 바꾼 정상적인
    # 변경(D <경로> + A <경로>/x)에서, 삭제가 뒤에 오면 mkdir 이 그 파일과 부딪힌다.
    local target
    for i in "${!copy_del_path[@]}"; do
        if [[ "${copy_del_present[$i]}" != "1" ]]; then
            continue
        fi
        pth="${copy_del_path[$i]}"
        target="$MI_REPO/$pth"
        if [[ -d "$target" && ! -L "$target" ]]; then
            die 5 "미러의 '$pth' 가 디렉터리다 — upstream 에서는 파일이 지워졌다. 수동 처리해라"
        fi
        rm -f -- "$target" || die 5 "미러에서 파일을 지우지 못했다: $pth"
        msg "  삭제  $pth"
    done

    local dest dest_dir
    for i in "${!copy_write_path[@]}"; do
        pth="${copy_write_path[$i]}"
        mode="${copy_write_mode[$i]}"
        dest="$MI_REPO/$pth"
        dest_dir="$(dirname -- "$dest")"
        if [[ -d "$dest" && ! -L "$dest" ]]; then
            die 5 "미러의 '$pth' 가 디렉터리다 — upstream 에서는 파일이다. 수동 처리해라"
        fi
        mkdir -p -- "$dest_dir" || die 5 "디렉터리를 만들지 못했다: $dest_dir (같은 이름의 파일이 있는지 확인해라)"
        if ! git -C "$UP_REPO" cat-file blob "$to_sha:$pth" > "$dest"; then
            die 5 "파일 내용을 복사하지 못했다: $pth"
        fi
        if [[ "$mode" == "100755" ]]; then
            chmod 755 -- "$dest" || die 5 "실행 비트를 설정하지 못했다: $pth"
        else
            chmod 644 -- "$dest" || die 5 "파일 모드를 설정하지 못했다: $pth"
        fi
        msg "  쓰기  $pth"
    done

    if ! git -C "$MI_REPO" add -A; then
        die 5 "미러 인덱스 갱신(git add -A)에 실패했다: $MI_REPO"
    fi

    local assert_failed=0
    run_assertions || assert_failed=$?

    if (( assert_failed > 0 )); then
        err ""
        err "[ERROR] 어서션 $assert_failed 종 실패 — 커밋하지 않는다."
        err "        미러 워킹트리는 **복사·스테이징된 상태 그대로 남겨 둔다**."
        err "        무엇이 유입·소실됐는지 사람이 직접 봐야 하므로 자동 롤백하지 않는다:"
        err "            git -C $MI_REPO diff --cached"
        err "        확인 후 되돌리려면 (--apply 는 clean 한 미러에서만 시작하므로 안전하다):"
        err "            git -C $MI_REPO reset --hard HEAD && git -C $MI_REPO clean -fd"
        RECOVERY_HINT_SHOWN=1
        exit 4
    fi

    if git -C "$MI_REPO" diff --cached --quiet; then
        msg ""
        msg "스테이지된 변경이 없다 — 커밋을 생략한다."
        print_manual_block
        return 0
    fi

    local manual_body="없음"
    if (( ${#keep_path[@]} > 0 )); then
        manual_body="$(printf '  - %s\n' "${keep_path[@]}")"
    fi
    local new_body="없음"
    if (( ${#new_write_path[@]} > 0 )); then
        new_body="$(printf '  + %s\n' "${new_write_path[@]}")"
    fi

    if ! git -C "$MI_REPO" commit -q -F - <<EOF
chore: sync curated mirror — upstream $from_short..$to_short

mirror-sync.sh 자동 싱크 (personal-ops mirror-sync, 이슈 #34).

범위: $PAIR_BASE  upstream $from_short..$to_short
복사 $n_copy (쓰기 ${#copy_write_path[@]} / $del_summary) ·
보존 ${#keep_path[@]} · 미반입 ${#skip_path[@]} · 미삭제 $n_protect

어서션 6종 통과:
  ASSERT_A_UPSTREAM_ONLY_INFLOW
  ASSERT_B_FORBID_INFLOW
  ASSERT_C_MUST_SURVIVE_LOST
  ASSERT_D_MIRROR_ONLY_DELETED
  ASSERT_E_STAGED_AS_REPORTED
  ASSERT_F_NEW_FILE_INFLOW

신규 반입 (미러에 없던 파일 — --allow-new 로 승인됨):
$new_body

수동 이식 대기 (KEEP_MIRROR — 이 커밋에 포함되지 않음):
$manual_body
EOF
    then
        die 5 "미러 커밋에 실패했다: $MI_REPO"
    fi

    local new_sha
    new_sha="$(git -C "$MI_REPO" rev-parse --short HEAD)"
    msg ""
    msg "미러 로컬 커밋 완료: $new_sha  ($MI_REPO, 브랜치 $MI_BRANCH)"
    msg "push 는 하지 않았다 — 이 스크립트의 최대 범위는 로컬 커밋이다."
    msg "내용을 확인한 뒤 사람이 직접 push 해라:  git -C $MI_REPO push"

    print_manual_block
    return 0
}

main "$@"
