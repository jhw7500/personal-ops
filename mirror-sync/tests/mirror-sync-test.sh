#!/usr/bin/env bash
#
# mirror-sync-test.sh — bin/mirror-sync.sh 의 mock 저장소 테스트 (personal-ops 이슈 #34)
#
# ┌─ 이 테스트가 지키는 것 ────────────────────────────────────────────────────┐
# │ 이슈 #34 의 완료 조건은 어서션이 각각 **되돌리면 실패**하는 것을 mock       │
# │ 저장소로 확인하는 것이다. exit 0 이 났다는 사실은 검증이 아니다.            │
# │ 그래서 어서션은 전부 **최소 쌍(minimal pair)** 으로 두 번 돌린다.           │
# │   위반 사실 하나를 심은 세계 → 그 어서션이 [FAIL] 이고 종료 코드 4, 커밋 없음│
# │   그 사실 하나만 되돌린 세계 → 같은 어서션이 [PASS] 이고 종료 코드 0, 커밋 1 │
# │ 두 세계의 차이는 그 사실 하나뿐이다. 한쪽만 보면 아무것도 검증하지 못한다.  │
# │ 나머지 어서션이 그 실행에서 [PASS] 인 것도 같이 확인한다 — 실패가 "무엇이든 │
# │ 하나 터졌다"가 아니라 **그 어서션이** 터진 것임을 못박기 위해서다.          │
# └────────────────────────────────────────────────────────────────────────────┘
#
# ┌─ A·D 의 최소 쌍이 "물려받은 상태"인 이유 (의도된 한계) ────────────────────┐
# │ A(반입 금지 유입)와 D(미러 전용 삭제)의 위반은 **싱크 자신이 만들 수 없다.**│
# │ 분류(classify_path)와 어서션이 같은 UPSTREAM_ONLY / MIRROR_ONLY 패턴을 쓰고,│
# │ 그 패턴에 걸린 경로는 각각 '미반입'·'미삭제'로 분류돼 복사도 삭제도 되지    │
# │ 않기 때문이다. 게다가 --apply 는 clean 한 미러에서만 시작하므로 남의 파일이 │
# │ 인덱스로 딸려 들어올 수도 없다. 그래서 A·D 의 위반 세계는 미러 base 커밋에  │
# │ 심을 수밖에 없고, 그 결과 "인덱스를 보는 구현"과 "HEAD 트리를 보는 구현"이  │
# │ A·D 에서는 결과가 같다(= 그 변이는 등가 변이다).                           │
# │ 인덱스를 봐야만 하는 것은 B·C·E 다 — 셋 다 방금 복사된 내용을 검사하므로    │
# │ HEAD 트리를 보는 구현으로 바꾸면 최소 쌍이 즉시 빨개진다.                   │
# │ "분류가 틀려 UPSTREAM_ONLY 파일이 복사되는" 시나리오는 A 가 아니라          │
# │ ASSERT_F(패턴이 아니라 미러 인덱스 실측을 오라클로 쓰는 어서션)가 잡는다.   │
# │ 아래 5-P 가 "싱크가 D 위반을 만들 수 없다"를 실제로 확인한다.               │
# └────────────────────────────────────────────────────────────────────────────┘
#
# 안전 경계
#   - 실제 미러 쌍(max9296 / max9296-gitlab)에 쓰지 않는다. 쓰기가 있는 모든 저장소는
#     mktemp -d 아래에 새로 만든 mock 이고 종료 시 trap 으로 지운다.
#     실제 쌍은 마지막 절에서 **dry-run(순수 읽기)** 으로만 건드린다.
#   - 저장소의 config/ 를 수정하지 않는다. 구현이 제공하는 주입점
#     MIRROR_SYNC_CONFIG_DIR 로 mock 전용 pairs.tsv / curation/mock.conf 를 먹인다.
#   - 이 테스트 자신도 git push 를 하지 않는다. mock origin 은 git clone --bare 로 만든다
#     (bare 를 push 로 채우면 "push 안 했음"을 검증하는 테스트가 push 를 하게 된다).
#
# mock 경로에는 공백·괄호·& ·= 가 들어간 파일을 일부러 넣었다. 실측(FACTS.md)상
# max9296 에 그런 경로가 3개 있고, 비인용 확장으로 짠 스크립트는 거기서 조용히 깨진다.
# 그 경로들이 복사·미반입·보존 세 분류와 어서션 양쪽을 모두 지나가게 배치했다.
#
# set -e 를 쓰지 않는 이유: 첫 실패에서 죽으면 마지막의 통과/실패 집계를 낼 수 없다.
# claude-token-sync/tests/test-token-sync.sh 와 같은 관행이다. 실패가 의미 있는 명령은
# 전부 if / 종료코드 캡처로 명시 검사한다.
set -uo pipefail

MODULE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$MODULE_DIR/bin/mirror-sync.sh"

REAL_GIT="$(command -v git)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

CHECKS=0
FAILURES=0
SKIPPED=0

# 마지막 mirror-sync 실행 결과 (run_sync 가 채운다)
RUN_OUT=""
RUN_RC=0

# build_world 가 채우는 현재 세계
W_UP=""
W_MI=""
W_CFG=""
W_FROM=""
W_TO=""

# ── 집계 ────────────────────────────────────────────────────────────────────
pass() {
    CHECKS=$(( CHECKS + 1 ))
    printf 'PASS %s\n' "$1"
}

fail() {
    CHECKS=$(( CHECKS + 1 ))
    FAILURES=$(( FAILURES + 1 ))
    printf 'FAIL %s\n' "$1"
    printf '       기대: %s\n' "$2"
    printf '       실제: %s\n' "$3"
}

# 조건이 갖춰지지 않아 못 돌린 검사. 조용히 빠지지 않도록 집계에 남긴다.
skip() {
    CHECKS=$(( CHECKS + 1 ))
    SKIPPED=$(( SKIPPED + 1 ))
    printf 'SKIP %s\n' "$1"
    printf '       사유: %s\n' "$2"
}

check_eq() {  # check_eq <이름> <기대> <실제>
    if [[ "$2" == "$3" ]]; then
        pass "$1"
    else
        fail "$1" "$2" "$3"
    fi
}

dump_tail() {  # 실패 진단용. 마지막 실행 출력의 꼬리를 들여쓰기해 보여준다
    printf '       ---- 마지막 mirror-sync 출력 (끝 20줄) ----\n'
    printf '%s\n' "$RUN_OUT" | tail -20 | sed 's/^/       | /'
}

check_out_has() {  # check_out_has <이름> <문자열>  — 마지막 실행 출력에 포함되는가
    if printf '%s\n' "$RUN_OUT" | grep -Fq -- "$2"; then
        pass "$1"
    else
        fail "$1" "출력에 \"$2\" 포함" "미포함"
        dump_tail
    fi
}

check_out_lacks() {  # check_out_lacks <이름> <문자열>
    if printf '%s\n' "$RUN_OUT" | grep -Fq -- "$2"; then
        fail "$1" "출력에 \"$2\" 없음" "포함됨"
        dump_tail
    else
        pass "$1"
    fi
}

check_row() {  # check_row <이름> <분류라벨> <경로>  — 같은 줄에 라벨과 경로가 있는가
    if printf '%s\n' "$RUN_OUT" | grep -F -- "$3" | grep -Fq -- "$2"; then
        pass "$1"
    else
        fail "$1" "'$2' 행에 $3" "해당 행 없음"
        dump_tail
    fi
}

# ── mock 저장소 유틸 ────────────────────────────────────────────────────────
git_init_repo() {  # git_init_repo <경로> <브랜치>
    local p="$1" b="$2"
    git init -q -- "$p" >/dev/null 2>&1
    git -C "$p" symbolic-ref HEAD "refs/heads/$b"
    git -C "$p" config user.name "mirror-sync test"
    git -C "$p" config user.email "mirror-sync-test@example.invalid"
    git -C "$p" config commit.gpgsign false
    git -C "$p" config core.autocrlf false
}

commit_all() {  # commit_all <경로> <메시지>
    git -C "$1" add -A
    git -C "$1" commit -q -m "$2"
}

wfile() {  # wfile <루트> <상대경로>  — 내용은 stdin(heredoc)
    local root="$1" rel="$2"
    mkdir -p -- "$(dirname -- "$root/$rel")"
    cat > "$root/$rel"
}

commit_count() { git -C "$1" rev-list --count HEAD; }

# 저장소 전체 스냅샷 — 커밋 수·HEAD·워킹트리 상태·파일 크기/mtime/모드·내용 해시.
# dry-run 이 정말 아무것도 쓰지 않았는지 보려면 mtime 과 내용을 둘 다 봐야 한다.
# find 와 md5sum 이 **같은 서브셸(= 같은 cwd)** 안에 있어야 한다. 파이프를 서브셸
# 밖으로 빼면 md5sum 이 테스트 스크립트의 cwd 에서 상대 경로를 풀어 전부 실패하고,
# 실패는 stderr 로만 나가므로 스냅샷은 "해시 0줄"로 조용히 같아진다 — 검사가 통과해도
# 아무것도 검증하지 않게 된다. 아래 "스냅샷 자체 검사"가 이 상태를 잡는다.
snapshot_repo() {  # snapshot_repo <경로>
    local repo="$1"
    git -C "$repo" rev-list --count HEAD
    git -C "$repo" rev-parse HEAD
    git -C "$repo" status --porcelain
    (
        cd "$repo" || exit 1
        find . -path ./.git -prune -o -type f -printf '%P|%s|%T@|%m\n' | LC_ALL=C sort
        find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum
    )
}

# ── mirror-sync 실행 ────────────────────────────────────────────────────────
run_sync() {  # run_sync <인자...>  — 현재 세계(W_CFG)에 대해 실행
    RUN_OUT="$(MIRROR_SYNC_CONFIG_DIR="$W_CFG" bash "$SYNC" "$@" 2>&1)"
    RUN_RC=$?
}

# git 호출을 전부 기록하는 PATH 앞단 래퍼. "push 를 하지 않는다"를 결과(원격 ref)뿐
# 아니라 **행위**(실행된 git 명령) 쪽에서도 확인하기 위한 것이다.
run_sync_logged() {  # run_sync_logged <로그파일> <인자...>
    local log="$1"
    shift
    local shim="$TMP_ROOT/shim"
    mkdir -p "$shim"
    cat > "$shim/git" <<'SHIM'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${MS_TEST_GIT_LOG:-/dev/null}"
exec "${MS_TEST_REAL_GIT:-/usr/bin/git}" "$@"
SHIM
    chmod 755 "$shim/git"
    : > "$log"
    RUN_OUT="$(
        MIRROR_SYNC_CONFIG_DIR="$W_CFG" \
        MS_TEST_GIT_LOG="$log" \
        MS_TEST_REAL_GIT="$REAL_GIT" \
        PATH="$shim:$PATH" \
        bash "$SYNC" "$@" 2>&1
    )"
    RUN_RC=$?
}

# ── mock 세계 생성 ──────────────────────────────────────────────────────────
# variant 로 "위반 사실 하나"만 바꾼다. 최소 쌍의 다른 쪽은 항상 baseline 이거나
# 접미사 없는 대응 variant 다.
#
#   baseline   위반 없음. 정상 싱크 경로.
#   A_bad      미러에 internal/leaked.md (UPSTREAM_ONLY 매칭)가 이미 커밋돼 있다
#   A_ok       같은 파일이 docs/leaked-note.md (규칙 비매칭 경로)에 있다  ← 경로만 다름
#   B_bad      upstream 이 src/lib.sh 에 FORBID 문자열 "PR #12" 를 넣어 내려보낸다
#   B_ok       같은 변경인데 "이슈 12번" 이다                              ← 문자열만 다름
#   C_bad      upstream 이 360p 문서를 바꾸는데 그 판에는 "증적 위치" 문단이 없다
#   C_ok       같은 변경인데 "증적 위치" 문단을 그대로 담고 있다           ← 한 문단만 다름
#   D_bad      미러에 docs/mirror-only.md (MIRROR_ONLY)가 없다
#   D_ok       그 파일이 있다                                              ← 파일 유무만 다름
#   E_bad      미러 src/lib.sh 에 skip-worktree 가 걸려 있다 — git add -A 가 그 파일의
#              변경을 exit 0 인 채로 통째로 무시한다
#   E_ok       같은 세계인데 skip-worktree 가 없다                         ← 인덱스 플래그만 다름
#   G_bad      미러 .gitignore 가 *.dtb 를 무시하고 upstream 이 docs/board.dtb 를 추가한다
#   G_ok       같은 세계인데 .gitignore 가 *.log 를 무시한다               ← 무시 패턴만 다름
#   DELNOOP    미러에 docs/obsolete.md 가 애초에 없다 (삭제생략 집계 확인)
#   P_del      upstream 에도 docs/mirror-only.md 가 있었고 head 에서 지운다
#              (MIRROR_ONLY 로 분류된 D 는 삭제 전파하지 않는다는 확인)
#   TYPECHG    upstream 이 docs/notes 를 파일에서 디렉터리로 바꾼다
#              (삭제를 쓰기보다 먼저 하지 않으면 mkdir 이 그 파일과 부딪힌다)
#   SYMLINK    upstream head 가 심볼릭 링크(모드 120000)를 추가한다
#              (지원하지 않는 모드는 **한 파일도 쓰기 전에** 거부해야 한다)
#   DIRCLASH   미러의 docs/clash 는 디렉터리인데 upstream 에서는 파일이다
#              (적용 도중 die 5 로 죽는 경로 — 복구 안내가 나와야 한다)
build_world() {  # build_world <세계이름> <variant>
    local name="$1" variant="$2"
    local W="$TMP_ROOT/$name"
    local UP="$W/up" MI="$W/mi" CFG="$W/config"

    mkdir -p "$W" "$CFG/curation"
    git_init_repo "$UP" master
    git_init_repo "$MI" main

    # ── 설정 (mock 전용. 저장소의 config/ 는 건드리지 않는다) ──────────────
    {
        printf '# base\tgithub\tgitlab\tupstream_path\tmirror_path\tup_branch\tmi_branch\n'
        printf 'mock\thttps://example.invalid/up.git\thttp://example.invalid/mi\t%s\t%s\tmaster\tmain\n' \
            "$UP" "$MI"
        printf '# commented\thttps://example.invalid/x.git\thttp://example.invalid/x\t/x\t/x\tmaster\tmain\n'
    } > "$CFG/pairs.tsv"

    cat > "$CFG/curation/mock.conf" <<'CONF'
# mock 큐레이션 규칙 — 실제 max9296.conf 와 같은 형식(bash 배열)이다.
KEEP_MIRROR=(
  "README.md"
  "docs/prepare (board) gate & v1.md"
)
UPSTREAM_ONLY=(
  "internal/**"
  "docs/AND9230-D (AP1302 RR)_PointImage.pdf"
  "docs/GMSL2 DS & Reg Doc (rev3).pdf"
  "docs/x=R2_PointImage.pdf"
)
MIRROR_ONLY=(
  "docs/mirror-only.md"
)
FORBID=(
  "PR #"
  "docs/superpowers/"
)
MUST_SURVIVE=(
  "증적 위치"
  "대상 저장소는 여기 포함되지 않으므로"
)
CONF

    # ── upstream base 커밋 ─────────────────────────────────────────────────
    wfile "$UP" "README.md" <<'EOF'
# mock upstream
내부 계획문서 참조가 살아 있는 upstream 판 README.
EOF
    wfile "$UP" "src/app.sh" <<'EOF'
#!/usr/bin/env bash
echo base
EOF
    chmod 755 "$UP/src/app.sh"
    wfile "$UP" "src/lib.sh" <<'EOF'
lib_base() { :; }
EOF
    wfile "$UP" "docs/note (draft) & summary=v2.md" <<'EOF'
공백·괄호·& ·= 가 전부 들어간 경로다 (복사 대상). base 판.
EOF
    wfile "$UP" "docs/360p (readout) & validation.md" <<'EOF'
# 360p 검증 (upstream 판)
upstream 판에는 미러 전용 문단이 없다.
EOF
    wfile "$UP" "docs/prepare (board) gate & v1.md" <<'EOF'
보드 게이트 문서 upstream 판 — 보드 원시 캡처 경로 참조를 포함한다. base.
EOF
    wfile "$UP" "docs/obsolete.md" <<'EOF'
곧 upstream 에서 삭제될 문서. 삭제 전파 대상이다.
EOF
    wfile "$UP" "internal/plan.md" <<'EOF'
내부 계획 문서. 미러 반입 금지. PR #7 을 참조한다.
EOF
    wfile "$UP" "docs/AND9230-D (AP1302 RR)_PointImage.pdf" <<'EOF'
(mock pdf) 공백·괄호 포함 경로. base.
EOF
    wfile "$UP" "docs/GMSL2 DS & Reg Doc (rev3).pdf" <<'EOF'
(mock pdf) 공백·& ·괄호 포함 경로. base.
EOF
    wfile "$UP" "docs/x=R2_PointImage.pdf" <<'EOF'
(mock pdf) '=' 포함 경로. base.
EOF
    if [[ "$variant" == "P_del" ]]; then
        # upstream 에도 같은 이름의 파일이 있었던 세계. head 에서 upstream 이 지운다.
        wfile "$UP" "docs/mirror-only.md" <<'EOF'
upstream 판 패키지 안내. head 에서 지워진다.
EOF
    fi
    if [[ "$variant" == "TYPECHG" ]]; then
        # head 에서 파일 → 디렉터리로 바뀔 경로
        wfile "$UP" "docs/notes" <<'EOF'
파일이던 시절의 노트. head 에서 같은 이름의 디렉터리가 된다.
EOF
    fi
    commit_all "$UP" "upstream base"
    W_FROM="$(git -C "$UP" rev-parse HEAD)"

    # ── mirror base 커밋 (큐레이션된 사본) ─────────────────────────────────
    wfile "$MI" "README.md" <<'EOF'
# mock mirror
미러 판 README. 대상 저장소는 여기 포함되지 않으므로 범위를 좁혀 서술한다.
EOF
    wfile "$MI" "src/app.sh" <<'EOF'
#!/usr/bin/env bash
echo base
EOF
    chmod 755 "$MI/src/app.sh"
    wfile "$MI" "src/lib.sh" <<'EOF'
lib_base() { :; }
EOF
    wfile "$MI" "docs/note (draft) & summary=v2.md" <<'EOF'
공백·괄호·& ·= 가 전부 들어간 경로다 (복사 대상). base 판.
EOF
    wfile "$MI" "docs/360p (readout) & validation.md" <<'EOF'
# 360p 검증 (미러 판)
증적 위치: 원시 캡처는 이 저장소에 두지 않는다. 미러 전용 문단이다.
EOF
    wfile "$MI" "docs/prepare (board) gate & v1.md" <<'EOF'
보드 게이트 문서 미러 판 — 보드 원시 캡처 경로 참조를 걷어냈다. base.
EOF
    if [[ "$variant" != "DELNOOP" ]]; then
        wfile "$MI" "docs/obsolete.md" <<'EOF'
곧 upstream 에서 삭제될 문서. 삭제 전파 대상이다.
EOF
    fi
    if [[ "$variant" != "D_bad" ]]; then
        wfile "$MI" "docs/mirror-only.md" <<'EOF'
미러에만 있는 패키지 안내. upstream 에 대응 파일이 없다.
EOF
    fi
    case "$variant" in
        A_bad)
            # UPSTREAM_ONLY 규칙(internal/**)에 매칭되는 경로
            wfile "$MI" "internal/leaked.md" <<'EOF'
이전 싱크가 흘려보낸 내부 문서.
EOF
            ;;
        A_ok)
            # 내용은 같고 경로만 규칙에 매칭되지 않는다
            wfile "$MI" "docs/leaked-note.md" <<'EOF'
이전 싱크가 흘려보낸 내부 문서.
EOF
            ;;
        G_bad)
            # 미러가 *.dtb 를 무시한다 → 복사해도 git add -A 가 스테이징하지 않는다
            wfile "$MI" ".gitignore" <<'EOF'
*.dtb
EOF
            ;;
        G_ok)
            # 같은 자리에 .gitignore 가 있지만 무시 패턴만 다르다
            wfile "$MI" ".gitignore" <<'EOF'
*.log
EOF
            ;;
        TYPECHG)
            wfile "$MI" "docs/notes" <<'EOF'
파일이던 시절의 노트. head 에서 같은 이름의 디렉터리가 된다.
EOF
            ;;
        DIRCLASH)
            # 미러에서는 docs/clash 가 디렉터리다 (upstream 에서는 파일이 된다)
            wfile "$MI" "docs/clash/inner.md" <<'EOF'
미러에서는 docs/clash 가 디렉터리다.
EOF
            ;;
    esac
    commit_all "$MI" "mirror base (curated)"

    if [[ "$variant" == "E_bad" ]]; then
        # skip-worktree 가 걸린 추적 파일: git add -A 가 그 파일의 변경을
        # **exit 0 인 채로** 무시한다. 화면에는 "쓰기" 가 찍히는데 커밋에는 없다.
        git -C "$MI" update-index --skip-worktree "src/lib.sh"
    fi

    # ── upstream head 커밋 (싱크 범위) ─────────────────────────────────────
    wfile "$UP" "src/app.sh" <<'EOF'
#!/usr/bin/env bash
echo head
EOF
    chmod 755 "$UP/src/app.sh"

    case "$variant" in
        B_bad)
            wfile "$UP" "src/lib.sh" <<'EOF'
lib_head() { :; }
# PR #12 에서 도입 — 미러로 넘어가면 안 되는 참조다.
EOF
            ;;
        *)
            wfile "$UP" "src/lib.sh" <<'EOF'
lib_head() { :; }
# 이슈 12번 에서 도입 — 미러로 넘어가도 무해한 참조다.
EOF
            ;;
    esac

    wfile "$UP" "docs/note (draft) & summary=v2.md" <<'EOF'
공백·괄호·& ·= 가 전부 들어간 경로다 (복사 대상). head 판으로 갱신됨.
EOF
    wfile "$UP" "docs/new-guide.md" <<'EOF'
upstream 에서 새로 생긴 문서. 미러로 복사돼야 한다.
EOF
    rm -f -- "$UP/docs/obsolete.md"
    wfile "$UP" "README.md" <<'EOF'
# mock upstream
내부 계획문서 참조가 살아 있는 upstream 판 README. head 에서 크게 고쳤다.
EOF
    wfile "$UP" "docs/prepare (board) gate & v1.md" <<'EOF'
보드 게이트 문서 upstream 판 — 보드 원시 캡처 경로 참조를 포함한다. head.
EOF
    wfile "$UP" "internal/plan.md" <<'EOF'
내부 계획 문서. 미러 반입 금지. PR #7 을 참조한다. head 에서 갱신.
EOF
    wfile "$UP" "internal/newplan.md" <<'EOF'
head 에서 새로 생긴 내부 계획 문서. 미러 반입 금지.
EOF
    wfile "$UP" "docs/AND9230-D (AP1302 RR)_PointImage.pdf" <<'EOF'
(mock pdf) 공백·괄호 포함 경로. head.
EOF
    wfile "$UP" "docs/GMSL2 DS & Reg Doc (rev3).pdf" <<'EOF'
(mock pdf) 공백·& ·괄호 포함 경로. head.
EOF
    wfile "$UP" "docs/x=R2_PointImage.pdf" <<'EOF'
(mock pdf) '=' 포함 경로. head.
EOF

    case "$variant" in
        C_bad)
            # 사고 재현: 미러 전용 "증적 위치" 문단이 없는 upstream 판이 그대로 복사된다
            wfile "$UP" "docs/360p (readout) & validation.md" <<'EOF'
# 360p 검증 (upstream 판)
upstream 판에는 미러 전용 문단이 없다. head 에서 본문을 늘렸다.
EOF
            ;;
        C_ok)
            # 최소 쌍의 반대편: 같은 변경인데 그 문단을 담고 있다
            wfile "$UP" "docs/360p (readout) & validation.md" <<'EOF'
# 360p 검증 (upstream 판)
증적 위치: 원시 캡처는 이 저장소에 두지 않는다. 미러 전용 문단이다.
upstream 판에는 미러 전용 문단이 없다. head 에서 본문을 늘렸다.
EOF
            ;;
        G_bad|G_ok)
            # 미러 .gitignore 와 부딪히는(또는 부딪히지 않는) 새 파일
            wfile "$UP" "docs/board.dtb" <<'EOF'
(mock dtb) upstream 이 새로 추가한 파일.
EOF
            ;;
        P_del)
            rm -f -- "$UP/docs/mirror-only.md"
            ;;
        TYPECHG)
            rm -f -- "$UP/docs/notes"
            wfile "$UP" "docs/notes/index.md" <<'EOF'
디렉터리가 된 노트의 첫 파일.
EOF
            ;;
        SYMLINK)
            ln -s "new-guide.md" "$UP/docs/guide-link.md"
            ;;
        DIRCLASH)
            wfile "$UP" "docs/clash" <<'EOF'
upstream 에서는 docs/clash 가 파일이다.
EOF
            ;;
    esac

    commit_all "$UP" "upstream head"
    W_TO="$(git -C "$UP" rev-parse HEAD)"

    W_UP="$UP"
    W_MI="$MI"
    W_CFG="$CFG"
}

# 어서션 최소 쌍 실행기.
#   위반 세계 → 종료 4 / 해당 어서션 [FAIL] / 나머지 [PASS] / 커밋 증가 없음
#   정상 세계 → 종료 0 / 해당 어서션 [PASS] / 커밋 1 증가
# --allow-new 를 붙이는 이유: mock 세계는 docs/new-guide.md 를 새로 반입하므로
# ASSERT_F 가 승인 없이는 거부한다. 여기서 확인하려는 것은 F 가 아니라 각 어서션의
# 최소 쌍이므로, F 는 승인해 두고 나머지 변수를 하나로 고정한다.
run_assert_pair() {  # run_assert_pair <라벨> <ASSERT이름> <bad variant> <ok variant> <나머지 어서션...>
    local label="$1" aname="$2" bad="$3" ok="$4"
    shift 4
    local others=("$@")
    local before after other

    # (1) 위반 방향 — 되돌리기 전. 여기서 반드시 빨개져야 한다.
    build_world "assert-${label}-bad" "$bad"
    before="$(commit_count "$W_MI")"
    run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
    after="$(commit_count "$W_MI")"
    check_eq "$label 위반: 종료 코드 4 (어서션 실패)" "4" "$RUN_RC"
    check_out_has "$label 위반: [FAIL] $aname" "[FAIL] $aname"
    for other in "${others[@]}"; do
        check_out_has "$label 위반: 나머지 $other 는 [PASS] (실패가 이 어서션에 한정)" "[PASS] $other"
    done
    check_eq "$label 위반: 커밋하지 않는다" "$before" "$after"
    if [[ -n "$(git -C "$W_MI" diff --cached --name-only)" ]]; then
        pass "$label 위반: 증거를 스테이징 상태로 남긴다 (자동 롤백 안 함)"
    else
        fail "$label 위반: 증거를 스테이징 상태로 남긴다 (자동 롤백 안 함)" \
             "git diff --cached 에 변경 있음" "비어 있음"
    fi

    # (2) 되돌린 방향 — 위반 사실 하나만 되돌린 세계. 여기서 반드시 초록이어야 한다.
    build_world "assert-${label}-ok" "$ok"
    before="$(commit_count "$W_MI")"
    run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
    after="$(commit_count "$W_MI")"
    check_eq "$label 정상: 종료 코드 0" "0" "$RUN_RC"
    check_out_has "$label 정상: [PASS] $aname" "[PASS] $aname"
    check_eq "$label 정상: 미러 커밋 1개 생성" "$(( before + 1 ))" "$after"
}

printf '=== mirror-sync 테스트 (mock 저장소) ===\n'
printf '대상: %s\n' "$SYNC"
printf '작업 디렉터리: %s\n\n' "$TMP_ROOT"

if [[ ! -x "$SYNC" ]]; then
    fail "bin/mirror-sync.sh 실행 가능" "실행 비트(+x)" "$(stat -c '%a' "$SYNC" 2>/dev/null || printf '파일 없음')"
fi

# ══════════════════════════════════════════════════════════════════════════
printf -- '--- 0. 스냅샷 자체 검사 (검사 도구가 무감각하지 않은가) ---\n'
# ══════════════════════════════════════════════════════════════════════════
# "dry-run 이 아무것도 쓰지 않았다"는 두 스냅샷이 같다는 사실로 주장한다. 그러니
# 스냅샷이 변화를 실제로 감지하는지부터 확인한다 — 감지하지 못하면 그 검사는
# 통과해도 아무것도 검증하지 않는다.
build_world snapshot-selftest baseline
SS_BASE="$(snapshot_repo "$W_MI")"
SS_FILES="$(git -C "$W_MI" ls-files | wc -l)"
SS_HASHES="$(printf '%s\n' "$SS_BASE" | grep -cE '^[0-9a-f]{32}  ')"
check_eq "스냅샷 자체 검사: 추적 파일 전부의 내용 해시가 실제로 계산된다" "$SS_FILES" "$SS_HASHES"

SS_TARGET="$W_MI/docs/note (draft) & summary=v2.md"
SS_MTIME_REF="$TMP_ROOT/snapshot-selftest/mtime-ref"
cp -p -- "$SS_TARGET" "$SS_MTIME_REF"
printf '내용만 바꾼다\n' >> "$SS_TARGET"
touch -r "$SS_MTIME_REF" -- "$SS_TARGET"   # mtime 은 되돌려 둔다
if [[ "$SS_BASE" != "$(snapshot_repo "$W_MI")" ]]; then
    pass "스냅샷 자체 검사: 내용 변경을 감지한다"
else
    fail "스냅샷 자체 검사: 내용 변경을 감지한다" "스냅샷이 달라짐" "동일 — 스냅샷이 무감각하다"
fi
cp -p -- "$SS_MTIME_REF" "$SS_TARGET"
check_eq "스냅샷 자체 검사: 원상복구하면 다시 같아진다 (무조건 달라지는 것이 아님)" \
    "$SS_BASE" "$(snapshot_repo "$W_MI")"
touch -d '2020-01-01T00:00:00' -- "$SS_TARGET"
if [[ "$SS_BASE" != "$(snapshot_repo "$W_MI")" ]]; then
    pass "스냅샷 자체 검사: mtime 변경을 감지한다"
else
    fail "스냅샷 자체 검사: mtime 변경을 감지한다" "스냅샷이 달라짐" "동일 — mtime 을 보지 않는다"
fi

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 1. dry-run: 4분류 출력 ---\n'
# ══════════════════════════════════════════════════════════════════════════
build_world dryrun baseline
UP_SNAP_BEFORE="$(snapshot_repo "$W_UP")"
MI_SNAP_BEFORE="$(snapshot_repo "$W_MI")"

run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "dry-run 종료 코드 0" "0" "$RUN_RC"

# 합계 두 줄이 4분류의 정본이다. 복사 5(쓰기 4 / 삭제 1) · 보존 2 · 미반입 5 · 미삭제 1
check_out_has "dry-run 합계: 복사 5 (쓰기 4 / 삭제 1)" "합계: 복사 5 (쓰기 4 / 삭제 1)"
check_out_has "dry-run 합계: 보존 2 · 미반입 5 · 미삭제 1" "보존 2 · 미반입 5 · 미삭제 1"
check_out_has "dry-run 변경 파일 12개" "upstream 변경 파일 12개"

check_row "dry-run 복사: src/app.sh" "복사" "src/app.sh"
check_row "dry-run 복사(신규): docs/new-guide.md" "복사" "docs/new-guide.md"
check_row "dry-run 복사(삭제 전파): docs/obsolete.md" "삭제" "docs/obsolete.md"
check_row "dry-run 복사(공백·괄호·&·= 경로)" "복사" "docs/note (draft) & summary=v2.md"
check_row "dry-run 보존: README.md" "보존" "README.md"
check_row "dry-run 보존(특수문자 경로)" "보존" "docs/prepare (board) gate & v1.md"
check_row "dry-run 미반입: internal/plan.md" "미반입" "internal/plan.md"
check_row "dry-run 미반입(공백·괄호 경로)" "미반입" "docs/AND9230-D (AP1302 RR)_PointImage.pdf"
check_row "dry-run 미반입(& 포함 경로)" "미반입" "docs/GMSL2 DS & Reg Doc (rev3).pdf"
check_row "dry-run 미반입(= 포함 경로)" "미반입" "docs/x=R2_PointImage.pdf"
check_row "dry-run 미삭제: docs/mirror-only.md" "미삭제" "docs/mirror-only.md"
check_out_has "dry-run 수동 이식 블록 (KEEP_MIRROR 2개)" "수동 이식 필요 — KEEP_MIRROR 2개"
check_out_has "dry-run 신규 반입 블록 (미러에 없던 파일 1개)" "신규 반입 — 미러에 없던 파일 1개"
check_out_has "dry-run 신규 반입 목록에 docs/new-guide.md" "+ docs/new-guide.md"
check_out_has "dry-run 종료 문구" "dry-run 종료 — 아무 파일도 쓰지 않았다."

# 합계의 '삭제' 는 실제로 지우는 건수여야 한다. 미러에 이미 없는 파일은 아무것도
# 지우지 않으므로 '삭제생략' 으로 따로 센다 — 표를 안 읽고 합계만 보는 사람에게
# "파일이 지워진다"로 읽히면 안 된다.
build_world delnoop DELNOOP
run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "삭제생략 세계: dry-run 종료 코드 0" "0" "$RUN_RC"
check_out_has "삭제생략: 합계가 실제 삭제 0건 / 생략 1건으로 갈린다" \
    "합계: 복사 5 (쓰기 4 / 삭제 0 / 삭제생략 1)"
check_row "삭제생략: 해당 행이 '삭제생략' 으로 표시된다" "삭제생략" "docs/obsolete.md"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 2. dry-run: 아무 파일도 쓰지 않는다 ---\n'
# ══════════════════════════════════════════════════════════════════════════
check_eq "dry-run 후 미러 스냅샷 불변 (커밋수·HEAD·내용·mtime·모드)" \
    "$MI_SNAP_BEFORE" "$(snapshot_repo "$TMP_ROOT/dryrun/mi")"
check_eq "dry-run 후 upstream 스냅샷 불변" \
    "$UP_SNAP_BEFORE" "$(snapshot_repo "$TMP_ROOT/dryrun/up")"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 3. --apply 정상 경로 ---\n'
# ══════════════════════════════════════════════════════════════════════════
build_world apply baseline
APPLY_COMMITS_BEFORE="$(commit_count "$W_MI")"
MI_README_BEFORE="$(cat "$W_MI/README.md")"
MI_GATE_BEFORE="$(cat "$W_MI/docs/prepare (board) gate & v1.md")"
UP_SNAP_BEFORE="$(snapshot_repo "$W_UP")"

run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "--apply 종료 코드 0" "0" "$RUN_RC"
check_eq "--apply 미러 커밋 1개 생성" \
    "$(( APPLY_COMMITS_BEFORE + 1 ))" "$(commit_count "$W_MI")"
check_eq "--apply 커밋 subject 형식" \
    "chore: sync curated mirror — upstream $(git -C "$W_UP" rev-parse --short "$W_FROM")..$(git -C "$W_UP" rev-parse --short "$W_TO")" \
    "$(git -C "$W_MI" log -1 --format=%s)"
check_eq "--apply 후 미러 워킹트리 clean" "" "$(git -C "$W_MI" status --porcelain)"
check_eq "--apply 는 upstream 을 건드리지 않는다" "$UP_SNAP_BEFORE" "$(snapshot_repo "$W_UP")"
check_out_has "--apply 어서션 A 통과" "[PASS] ASSERT_A_UPSTREAM_ONLY_INFLOW"
check_out_has "--apply 어서션 B 통과" "[PASS] ASSERT_B_FORBID_INFLOW"
check_out_has "--apply 어서션 C 통과" "[PASS] ASSERT_C_MUST_SURVIVE_LOST"
check_out_has "--apply 어서션 D 통과" "[PASS] ASSERT_D_MIRROR_ONLY_DELETED"
check_out_has "--apply 어서션 E 통과" "[PASS] ASSERT_E_STAGED_AS_REPORTED"
check_out_has "--apply 어서션 F 통과" "[PASS] ASSERT_F_NEW_FILE_INFLOW"
check_out_has "--apply 는 push 하지 않았음을 알린다" "push 는 하지 않았다"

# 복사 결과
check_eq "복사: src/app.sh 내용이 upstream head 와 일치" \
    "$(git -C "$W_UP" show "$W_TO:src/app.sh")" "$(cat "$W_MI/src/app.sh")"
check_eq "복사: 실행 비트(100755) 재현" "755" "$(stat -c '%a' "$W_MI/src/app.sh")"
check_eq "복사: 신규 파일 docs/new-guide.md 생성" \
    "$(git -C "$W_UP" show "$W_TO:docs/new-guide.md")" "$(cat "$W_MI/docs/new-guide.md")"
check_eq "복사: 공백·괄호·&·= 경로 내용 일치" \
    "$(git -C "$W_UP" show "$W_TO:docs/note (draft) & summary=v2.md")" \
    "$(cat "$W_MI/docs/note (draft) & summary=v2.md")"
if [[ -e "$W_MI/docs/obsolete.md" ]]; then
    fail "삭제 전파: docs/obsolete.md 가 미러에서 제거됨" "파일 없음" "아직 있음"
else
    pass "삭제 전파: docs/obsolete.md 가 미러에서 제거됨"
fi

# 보존 (KEEP_MIRROR) — 덮어쓰지 않는다
check_eq "보존: README.md 미러 내용 불변 (덮어쓰기 없음)" \
    "$MI_README_BEFORE" "$(cat "$W_MI/README.md")"
check_eq "보존: 특수문자 경로 KEEP_MIRROR 내용 불변" \
    "$MI_GATE_BEFORE" "$(cat "$W_MI/docs/prepare (board) gate & v1.md")"

# 미반입 (UPSTREAM_ONLY) — 미러에 들어오지 않는다
MI_INDEX="$(git -C "$W_MI" ls-files)"
for p in "internal/plan.md" "internal/newplan.md" \
         "docs/AND9230-D (AP1302 RR)_PointImage.pdf" \
         "docs/GMSL2 DS & Reg Doc (rev3).pdf" \
         "docs/x=R2_PointImage.pdf"; do
    if printf '%s\n' "$MI_INDEX" | grep -Fqx -- "$p"; then
        fail "미반입: $p 가 미러에 없다" "미러 인덱스에 없음" "존재함"
    else
        pass "미반입: $p 가 미러에 없다"
    fi
done

# 미삭제 (MIRROR_ONLY)
if [[ -e "$W_MI/docs/mirror-only.md" ]]; then
    pass "미삭제: docs/mirror-only.md 잔존"
else
    fail "미삭제: docs/mirror-only.md 잔존" "파일 존재" "삭제됨"
fi

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 4. push 를 하지 않는다 ---\n'
# ══════════════════════════════════════════════════════════════════════════
# origin 은 git clone --bare 로 만든다. 여기서 push 로 채우면 "push 안 함"을
# 검증하는 테스트 자신이 push 를 하게 된다.
build_world nopush baseline
ORIGIN="$TMP_ROOT/nopush/origin.git"
git clone -q --bare "$W_MI" "$ORIGIN" >/dev/null 2>&1
git -C "$W_MI" remote add origin "$ORIGIN"
ORIGIN_REF_BEFORE="$(git -C "$ORIGIN" rev-parse main)"
NOPUSH_COMMITS_BEFORE="$(commit_count "$W_MI")"
GIT_LOG="$TMP_ROOT/nopush/git-calls.log"

run_sync_logged "$GIT_LOG" --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "push 검증: --apply 자체는 성공" "0" "$RUN_RC"
check_eq "push 검증: 미러 로컬 HEAD 는 전진" \
    "$(( NOPUSH_COMMITS_BEFORE + 1 ))" "$(commit_count "$W_MI")"
check_eq "push 검증: origin 의 main ref 가 그대로다" \
    "$ORIGIN_REF_BEFORE" "$(git -C "$ORIGIN" rev-parse main)"
if grep -qE '(^| )push( |$)' "$GIT_LOG"; then
    fail "push 검증: git push 를 한 번도 실행하지 않았다" \
        "git 호출 로그에 push 없음" "$(grep -nE '(^| )push( |$)' "$GIT_LOG" | head -3)"
else
    pass "push 검증: git push 를 한 번도 실행하지 않았다 ($(wc -l < "$GIT_LOG")회 git 호출 관측)"
fi

run_sync --pair mock --from "$W_FROM" --to "$W_TO" --push
check_eq "push 검증: --push 플래그는 종료 코드 1 로 거부" "1" "$RUN_RC"
check_out_has "push 검증: 거부 사유를 알린다" "push 는 이 스크립트가 하지 않는다"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 5. 어서션 — 되돌리면 실패하는가 (최소 쌍) ---\n'
# ══════════════════════════════════════════════════════════════════════════
printf -- '  A. 반입 금지 유입 — 같은 파일, 경로만 internal/** 매칭 여부가 다르다\n'
run_assert_pair "어서션A(반입금지유입)" "ASSERT_A_UPSTREAM_ONLY_INFLOW" A_bad A_ok \
    "ASSERT_B_FORBID_INFLOW" "ASSERT_C_MUST_SURVIVE_LOST" "ASSERT_D_MIRROR_ONLY_DELETED" \
    "ASSERT_E_STAGED_AS_REPORTED" "ASSERT_F_NEW_FILE_INFLOW"

printf -- '  B. FORBID 문자열 유입 — 같은 변경, "PR #12" vs "이슈 12번" 만 다르다\n'
run_assert_pair "어서션B(FORBID유입)" "ASSERT_B_FORBID_INFLOW" B_bad B_ok \
    "ASSERT_A_UPSTREAM_ONLY_INFLOW" "ASSERT_C_MUST_SURVIVE_LOST" "ASSERT_D_MIRROR_ONLY_DELETED" \
    "ASSERT_E_STAGED_AS_REPORTED" "ASSERT_F_NEW_FILE_INFLOW"

printf -- '  C. MUST_SURVIVE 소실 — 2026-09-04 사고 재현. "증적 위치" 문단 유무만 다르다\n'
run_assert_pair "어서션C(MUST_SURVIVE소실)" "ASSERT_C_MUST_SURVIVE_LOST" C_bad C_ok \
    "ASSERT_A_UPSTREAM_ONLY_INFLOW" "ASSERT_B_FORBID_INFLOW" "ASSERT_D_MIRROR_ONLY_DELETED" \
    "ASSERT_E_STAGED_AS_REPORTED" "ASSERT_F_NEW_FILE_INFLOW"

printf -- '  D. MIRROR_ONLY 삭제 — docs/mirror-only.md 유무만 다르다\n'
run_assert_pair "어서션D(MIRROR_ONLY삭제)" "ASSERT_D_MIRROR_ONLY_DELETED" D_bad D_ok \
    "ASSERT_A_UPSTREAM_ONLY_INFLOW" "ASSERT_B_FORBID_INFLOW" "ASSERT_C_MUST_SURVIVE_LOST" \
    "ASSERT_E_STAGED_AS_REPORTED" "ASSERT_F_NEW_FILE_INFLOW"

printf -- '  E. 보고한 대로 스테이징 — skip-worktree 유무만 다르다\n'
printf -- '     (git add -A 가 exit 0 인 채로 변경을 통째로 무시하는 실제 경로다)\n'
run_assert_pair "어서션E(보고와인덱스불일치)" "ASSERT_E_STAGED_AS_REPORTED" E_bad E_ok \
    "ASSERT_A_UPSTREAM_ONLY_INFLOW" "ASSERT_B_FORBID_INFLOW" "ASSERT_C_MUST_SURVIVE_LOST" \
    "ASSERT_D_MIRROR_ONLY_DELETED" "ASSERT_F_NEW_FILE_INFLOW"

# C 위반은 "미러 전용 문단이 실제로 사라진" 상태여야 의미가 있다 — 증상까지 확인한다.
build_world c-symptom C_bad
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
if grep -Fq "증적 위치" "$W_MI/docs/360p (readout) & validation.md"; then
    fail "어서션C 증상: 미러 전용 문단이 실제로 덮어써졌다" "\"증적 위치\" 소실" "아직 남아 있음"
else
    pass "어서션C 증상: 미러 전용 문단이 실제로 덮어써졌다 (사고 재현)"
fi
check_out_has "어서션C 증상: 사라진 문자열을 지목한다" '- "증적 위치"'
check_out_has "어서션C: 복구 명령을 안내한다" "reset --hard HEAD"

# E 위반은 "커밋에 그 변경이 실제로 없는" 상태여야 의미가 있다 — 증상까지 확인한다.
build_world e-symptom E_bad
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_out_has "어서션E 증상: '쓰기 src/lib.sh' 로 보고는 했다" "쓰기  src/lib.sh"
check_out_has "어서션E 증상: 인덱스 내용이 upstream 판이 아님을 지목한다" \
    "인덱스 내용이 upstream 판이 아님: src/lib.sh"
check_eq "어서션E 증상: 인덱스의 src/lib.sh 가 upstream head 판이 아니다" \
    "$(git -C "$W_MI" rev-parse "HEAD:src/lib.sh")" "$(git -C "$W_MI" rev-parse ":src/lib.sh")"

printf -- '  F. 신규 반입 — 승인(--allow-new) 유무만 다르다\n'
# F 는 큐레이션 패턴이 아니라 **복사 전 미러 인덱스 실측**을 오라클로 쓴다.
# 그래서 "어느 축에도 안 걸려 기본값(복사)으로 흘러간 새 파일"을 A 와 달리 잡아낸다.
build_world assert-F-bad baseline
F_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply
check_eq "어서션F 위반(승인 없음): 종료 코드 4" "4" "$RUN_RC"
check_out_has "어서션F 위반: [FAIL] ASSERT_F_NEW_FILE_INFLOW" "[FAIL] ASSERT_F_NEW_FILE_INFLOW"
check_out_has "어서션F 위반: 새로 들어온 경로를 지목한다" "+ docs/new-guide.md"
check_out_has "어서션F 위반: 나머지 A 는 [PASS]" "[PASS] ASSERT_A_UPSTREAM_ONLY_INFLOW"
check_out_has "어서션F 위반: 나머지 E 는 [PASS]" "[PASS] ASSERT_E_STAGED_AS_REPORTED"
check_eq "어서션F 위반: 커밋하지 않는다" "$F_BEFORE" "$(commit_count "$W_MI")"

build_world assert-F-ok baseline
F_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "어서션F 정상(--allow-new): 종료 코드 0" "0" "$RUN_RC"
check_out_has "어서션F 정상: [PASS] ASSERT_F_NEW_FILE_INFLOW" "[PASS] ASSERT_F_NEW_FILE_INFLOW"
check_eq "어서션F 정상: 미러 커밋 1개 생성" "$(( F_BEFORE + 1 ))" "$(commit_count "$W_MI")"

printf -- '  P. MIRROR_ONLY 로 분류된 삭제(D)는 전파하지 않는다\n'
# A·D 위반을 싱크가 스스로 만들 수 없다는 주장(파일 상단 주석)의 실물 확인이다.
# upstream 이 MIRROR_ONLY 매칭 경로를 지워도 미러에서는 지우지 않아야 하고,
# 그래야 ASSERT_D 가 [PASS] 인 것이 "검사가 무딘 것"이 아니라 "위반이 없는 것"이다.
build_world protect-del P_del
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "미삭제(D): 종료 코드 0" "0" "$RUN_RC"
check_row "미삭제(D): 삭제가 아니라 '미삭제 보호' 로 분류된다" "미삭제" "docs/mirror-only.md"
check_out_lacks "미삭제(D): 삭제 동작으로 잡히지 않는다" "삭제  docs/mirror-only.md"
if [[ -e "$W_MI/docs/mirror-only.md" ]]; then
    pass "미삭제(D): upstream 이 지운 뒤에도 미러 파일이 남는다"
else
    fail "미삭제(D): upstream 이 지운 뒤에도 미러 파일이 남는다" "파일 존재" "삭제됨"
fi
check_out_has "미삭제(D): ASSERT_D 통과" "[PASS] ASSERT_D_MIRROR_ONLY_DELETED"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 6. 미러 .gitignore 와 부딪히는 복사 대상 ---\n'
# ══════════════════════════════════════════════════════════════════════════
# git add -A 는 미러 .gitignore 에 걸리는 새 파일을 조용히 건너뛴다. 그대로 두면
# 화면에는 "쓰기 + [PASS] + 커밋 완료" 가 나오고 커밋에는 그 파일이 없으며,
# 무시 파일이라 status 에도 안 잡혀 다음 실행의 clean 검사까지 통과한다.
# 그래서 **한 파일도 쓰기 전에** 거부한다.
build_world ignore-bad G_bad
IG_BEFORE="$(commit_count "$W_MI")"
IG_SNAP_BEFORE="$(snapshot_repo "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "무시 대상 복사: 종료 코드 3 으로 거부" "3" "$RUN_RC"
check_out_has "무시 대상 복사: 어느 파일이 문제인지 알린다" "docs/board.dtb"
check_eq "무시 대상 복사: 커밋하지 않는다" "$IG_BEFORE" "$(commit_count "$W_MI")"
check_eq "무시 대상 복사: 한 파일도 쓰지 않는다 (미러 스냅샷 불변)" \
    "$IG_SNAP_BEFORE" "$(snapshot_repo "$W_MI")"

build_world ignore-bad-dryrun G_bad
run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "무시 대상 복사: dry-run 은 0 으로 끝나되" "0" "$RUN_RC"
check_out_has "무시 대상 복사: dry-run 이 미리 경고한다" "미러 .gitignore 에 걸린다"

build_world ignore-ok G_ok
IG_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "무시 패턴만 바꾼 세계: 종료 코드 0" "0" "$RUN_RC"
check_eq "무시 패턴만 바꾼 세계: 커밋 1개 생성" "$(( IG_BEFORE + 1 ))" "$(commit_count "$W_MI")"
if git -C "$W_MI" ls-files --error-unmatch -- "docs/board.dtb" >/dev/null 2>&1; then
    pass "무시 패턴만 바꾼 세계: docs/board.dtb 가 실제로 커밋에 들어간다"
else
    fail "무시 패턴만 바꾼 세계: docs/board.dtb 가 실제로 커밋에 들어간다" "인덱스에 존재" "없음"
fi

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 6b. 적용 루프: 순서와 실패 처리 ---\n'
# ══════════════════════════════════════════════════════════════════════════
# upstream 이 파일을 같은 이름의 디렉터리로 바꾸면 diff 는 D <경로> + A <경로>/x 를
# 낸다. 쓰기를 삭제보다 먼저 하면 mkdir 이 아직 남아 있는 그 파일과 부딪혀
# set -e 로 죽는다(그것도 "사용법 오류"인 코드 1 로).
build_world typechange TYPECHG
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "파일→디렉터리 전환: 종료 코드 0" "0" "$RUN_RC"
if [[ -d "$W_MI/docs/notes" && -f "$W_MI/docs/notes/index.md" ]]; then
    pass "파일→디렉터리 전환: 미러에 디렉터리와 그 안의 파일이 생긴다"
else
    fail "파일→디렉터리 전환: 미러에 디렉터리와 그 안의 파일이 생긴다" \
         "docs/notes/ 디렉터리 + index.md" "$(ls -ld "$W_MI/docs/notes" 2>&1)"
fi

# 지원하지 않는 모드(심볼릭 링크)는 **한 파일도 쓰기 전에** 거부해야 한다.
# 복사 도중에 죽으면 반쯤 적용된 상태가 남고, 다음 --apply 는 그것을 "남의 변경"
# 으로 오귀속한다.
build_world symlink SYMLINK
SL_SNAP_BEFORE="$(snapshot_repo "$W_MI")"
SL_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "심볼릭 링크: 종료 코드 5" "5" "$RUN_RC"
check_out_has "심볼릭 링크: 이유를 알린다" "지원하지 않는 파일 모드"
check_eq "심볼릭 링크: 커밋하지 않는다" "$SL_BEFORE" "$(commit_count "$W_MI")"
check_eq "심볼릭 링크: 한 파일도 쓰지 않는다 (사전 검사에서 멈춘다)" \
    "$SL_SNAP_BEFORE" "$(snapshot_repo "$W_MI")"

# 적용 도중 죽는 경로(미러 쪽 타입이 어긋남)는 반쯤 적용된 상태를 남기므로
# 복구 명령을 반드시 화면에 낸다. 종료 코드도 "적용 중 오류"인 5 여야 한다.
build_world dirclash DIRCLASH
DC_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "미러 경로 타입 충돌: 종료 코드 5" "5" "$RUN_RC"
check_out_has "미러 경로 타입 충돌: 어느 경로인지 알린다" "미러의 'docs/clash' 가 디렉터리다"
check_out_has "미러 경로 타입 충돌: 복구 명령을 안내한다" "reset --hard HEAD"
check_eq "미러 경로 타입 충돌: 커밋하지 않는다" "$DC_BEFORE" "$(commit_count "$W_MI")"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 7. 대상 저장소 고정 ---\n'
# ══════════════════════════════════════════════════════════════════════════
# GIT_DIR/GIT_WORK_TREE 가 export 된 셸(git 훅, git rebase --exec, git bisect run)
# 안에서 실행하면 git -C 가 무시되고 환경이 가리키는 저장소가 대상이 된다.
build_world hijack baseline
HIJACK="$TMP_ROOT/hijack/other"
git_init_repo "$HIJACK" master
wfile "$HIJACK" "hijacked.txt" <<'EOF'
이 저장소는 pairs.tsv 어디에도 없다.
EOF
commit_all "$HIJACK" "other repo"
HJ_BEFORE="$(commit_count "$W_MI")"
HJ_OTHER_BEFORE="$(commit_count "$HIJACK")"
RUN_OUT="$(
    GIT_DIR="$HIJACK/.git" GIT_WORK_TREE="$HIJACK" \
    MIRROR_SYNC_CONFIG_DIR="$W_CFG" \
    bash "$SYNC" --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new 2>&1
)"
RUN_RC=$?
check_eq "GIT_DIR 하이재킹: 그래도 정상 종료" "0" "$RUN_RC"
check_eq "GIT_DIR 하이재킹: 설정상 미러에 커밋된다" "$(( HJ_BEFORE + 1 ))" "$(commit_count "$W_MI")"
check_eq "GIT_DIR 하이재킹: 환경이 가리킨 저장소는 건드리지 않는다" \
    "$HJ_OTHER_BEFORE" "$(commit_count "$HIJACK")"
check_eq "GIT_DIR 하이재킹: 환경 저장소 워킹트리도 그대로" "" "$(git -C "$HIJACK" status --porcelain)"

# mirror_path 가 다른 저장소의 하위 디렉터리를 가리키면 add -A 가 그 상위 저장소
# 전체를 스테이징해 엉뚱한 곳에 커밋한다. 최상위 동일성 검사로 막는다.
build_world subdir baseline
OUTER="$TMP_ROOT/subdir/outer"
# 상위 저장소의 브랜치를 일부러 mirror_branch 와 같은 'main' 으로 둔다. 그래야
# 브랜치 검사가 통과하고, 남는 방어선이 **최상위 동일성 검사 하나뿐**이 된다.
# (master 로 두면 브랜치 불일치가 먼저 걸려서 이 검사가 무엇을 막았는지 알 수 없다)
git_init_repo "$OUTER" main
wfile "$OUTER" "outer.txt" <<'EOF'
상위 저장소 파일.
EOF
commit_all "$OUTER" "outer base"
mkdir -p "$OUTER/mirror-clone"
{
    printf '# base\tgithub\tgitlab\tupstream_path\tmirror_path\tup_branch\tmi_branch\n'
    printf 'mock\thttps://example.invalid/up.git\thttp://example.invalid/mi\t%s\t%s\tmaster\tmain\n' \
        "$W_UP" "$OUTER/mirror-clone"
} > "$W_CFG/pairs.tsv"
OUTER_BEFORE="$(commit_count "$OUTER")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "하위 디렉터리 mirror_path: 종료 코드 3 으로 거부" "3" "$RUN_RC"
check_out_has "하위 디렉터리 mirror_path: 이유를 알린다" "저장소 최상위가 아니다"
check_eq "하위 디렉터리 mirror_path: 상위 저장소에 커밋하지 않는다" \
    "$OUTER_BEFORE" "$(commit_count "$OUTER")"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 8. 뒤집힌 범위 (--from 이 --to 의 조상이 아님) ---\n'
# ══════════════════════════════════════════════════════════════════════════
# 인자 순서를 뒤바꾸면 upstream 의 '추가'가 '삭제'로 나와 미러에 정상적으로 있는
# 파일들이 실제로 지워진다. 어서션 A~D 는 이 상황을 구조적으로 잡지 못한다.
build_world reversed baseline
REV_BEFORE="$(commit_count "$W_MI")"
REV_SNAP_BEFORE="$(snapshot_repo "$W_MI")"
run_sync --pair mock --from "$W_TO" --to "$W_FROM" --apply --allow-new
check_eq "뒤집힌 범위 + --apply: 종료 코드 3 으로 거부" "3" "$RUN_RC"
check_out_has "뒤집힌 범위: 이유를 알린다" "--from 이 --to 의 조상이 아니다"
check_eq "뒤집힌 범위: 커밋하지 않는다" "$REV_BEFORE" "$(commit_count "$W_MI")"
check_eq "뒤집힌 범위: 한 파일도 건드리지 않는다" "$REV_SNAP_BEFORE" "$(snapshot_repo "$W_MI")"

run_sync --pair mock --from "$W_TO" --to "$W_FROM"
check_eq "뒤집힌 범위 + dry-run: 경고만 하고 0 으로 끝난다" "0" "$RUN_RC"
check_out_has "뒤집힌 범위 + dry-run: 주의를 표시한다" "[주의] --from 이 --to 의 조상이 아니다"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 9. 인자·설정·상태 검증 ---\n'
# ══════════════════════════════════════════════════════════════════════════
build_world args baseline

run_sync --help
check_eq "--help 종료 코드 0" "0" "$RUN_RC"
check_out_has "--help 가 push 경계를 명시" "push 는 자동화하지 않는다"

run_sync --pair mock --to "$W_TO"
check_eq "--from 누락 → 종료 코드 1" "1" "$RUN_RC"

run_sync --pair mock --from "$W_FROM" --to "$W_TO" --nonsense
check_eq "알 수 없는 인자 → 종료 코드 1" "1" "$RUN_RC"

run_sync --pair does-not-exist --from "$W_FROM" --to "$W_TO"
check_eq "없는 pair → 종료 코드 2" "2" "$RUN_RC"
check_out_has "없는 pair: 사용 가능한 pair 를 알려준다" "사용 가능한 pair"

run_sync --pair mock --from deadbeefdeadbeefdeadbeefdeadbeefdeadbeef --to "$W_TO"
check_eq "해석 불가 --from → 종료 코드 3" "3" "$RUN_RC"

# 더러운 미러 워킹트리 + --apply
printf '사람이 손대던 중\n' >> "$W_MI/README.md"
DIRTY_COMMITS_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "더러운 미러 + --apply → 종료 코드 3" "3" "$RUN_RC"
check_eq "더러운 미러: 커밋하지 않는다" "$DIRTY_COMMITS_BEFORE" "$(commit_count "$W_MI")"
check_out_has "더러운 미러: 이유를 알린다" "미러 워킹트리가 clean 하지 않다"
check_out_has "더러운 미러: 직전 실행의 잔여물일 수 있음을 알린다" "직전 실행이 적용 도중 실패해 남긴 것일 수도 있다"
git -C "$W_MI" checkout -q -- README.md

# 미러 브랜치 불일치 + --apply
git -C "$W_MI" checkout -q -b other-branch
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "미러 브랜치 불일치 + --apply → 종료 코드 3" "3" "$RUN_RC"
git -C "$W_MI" checkout -q main

# 큐레이션 축이 비면 조용히 전량 복사되므로 거부해야 한다
build_world emptyaxis baseline
cat > "$W_CFG/curation/mock.conf" <<'CONF'
KEEP_MIRROR=()
UPSTREAM_ONLY=( "internal/**" )
MIRROR_ONLY=( "docs/mirror-only.md" )
FORBID=( "PR #" )
MUST_SURVIVE=( "증적 위치" )
CONF
run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "빈 큐레이션 축 → 종료 코드 2 (조용한 전량 복사 차단)" "2" "$RUN_RC"

# 축을 "공백 구분 문자열"로 쓰면 항목 1개짜리 축이 되어 아무것도 매칭하지 않는다.
# 길이 검사만으로는 통과하므로(1 > 0) 선언 타입을 봐야 한다. 이슈 #34 본문의
# 예시 문법이 정확히 이 모양이라 새 conf 를 쓰는 사람이 그대로 밟는다.
build_world scalaraxis baseline
cat > "$W_CFG/curation/mock.conf" <<'CONF'
KEEP_MIRROR="README.md docs/prepare (board) gate & v1.md"
UPSTREAM_ONLY="internal/** docs/x=R2_PointImage.pdf"
MIRROR_ONLY="docs/mirror-only.md"
FORBID="PR # docs/superpowers/"
MUST_SURVIVE="증적 위치"
CONF
SCALAR_BEFORE="$(commit_count "$W_MI")"
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "스칼라 축(공백 구분 문자열) → 종료 코드 2" "2" "$RUN_RC"
check_out_has "스칼라 축: 배열이어야 한다고 알린다" "bash 배열이 아니다"
check_eq "스칼라 축: 커밋하지 않는다 (조용한 전량 복사 차단)" \
    "$SCALAR_BEFORE" "$(commit_count "$W_MI")"

# MUST_SURVIVE_IN 은 MUST_SURVIVE 와 인덱스로 짝지으므로 길이가 같아야 한다
build_world msinlen baseline
cat >> "$W_CFG/curation/mock.conf" <<'CONF'
MUST_SURVIVE_IN=( "docs/360p (readout) & validation.md" )
CONF
run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "MUST_SURVIVE_IN 길이 불일치 → 종료 코드 2" "2" "$RUN_RC"

# MUST_SURVIVE_IN 으로 경로를 묶으면 그 파일 밖의 같은 문구는 인정하지 않는다
build_world msinbind baseline
cat >> "$W_CFG/curation/mock.conf" <<'CONF'
MUST_SURVIVE_IN=( "docs/이-파일은-없다.md" "" )
CONF
run_sync --pair mock --from "$W_FROM" --to "$W_TO" --apply --allow-new
check_eq "MUST_SURVIVE_IN 경로 구속: 다른 파일에 있어도 [FAIL]" "4" "$RUN_RC"
check_out_has "MUST_SURVIVE_IN 경로 구속: 요구 위치를 함께 보여준다" "요구 위치: docs/이-파일은-없다.md"

rm -f "$W_CFG/curation/mock.conf"
run_sync --pair mock --from "$W_FROM" --to "$W_TO"
check_eq "큐레이션 conf 없음 → 종료 코드 2" "2" "$RUN_RC"

# ══════════════════════════════════════════════════════════════════════════
printf -- '\n--- 10. 배포 설정(config/curation/*.conf) 스모크 ---\n'
# ══════════════════════════════════════════════════════════════════════════
# 위 검사는 전부 mock 전용 conf 를 주입한다. 그래서 **실제로 배포되는 conf** 의
# 오타·패턴 누락은 하나도 걸러지지 않는다(패턴 한 글자만 틀려도 그 축이 조용히
# 죽는다). 실제 저장소가 있는 호스트에서만 도는 조건부 스모크로 그 공백을 메운다.
# **dry-run 만 쓴다 — 실제 쌍에는 쓰기를 하지 않는다.**
#
# pairs.tsv 의 유효 행마다 한 벌씩 돈다. 경로는 pairs.tsv 에서 읽으므로 여기에
# 다시 적지 않는다 — 그래야 pairs.tsv 의 경로가 틀리면 이 스모크가 먼저 걸린다.
#
# **새 쌍을 pairs.tsv 에 추가하면 여기에도 기대값 한 줄을 추가해야 한다.**
# 그러지 않으면 그 conf 는 회귀 검사 밖에 남고, 패턴이 죽어도 스위트는 전부 통과한다.
smoke_real_pair() {
    local base="$1" from="$2" to="$3" want_copy="$4" want_rest="$5" leak_re="$6"
    local up mi up_head mi_head up_st mi_st leaks

    up="$(awk -F'\t' -v b="$base" '$1==b {print $4; exit}' "$MODULE_DIR/config/pairs.tsv")"
    mi="$(awk -F'\t' -v b="$base" '$1==b {print $5; exit}' "$MODULE_DIR/config/pairs.tsv")"

    if [[ -z "$up" || -z "$mi" ]]; then
        skip "배포 conf 스모크 ($base)" "pairs.tsv 에 유효한 $base 행이 없다"
        return
    fi
    if [[ ! -d "$up/.git" || ! -d "$mi/.git" ]] \
       || ! git -C "$up" rev-parse --verify -q "$from^{commit}" >/dev/null \
       || ! git -C "$up" rev-parse --verify -q "$to^{commit}" >/dev/null; then
        skip "배포 conf 스모크 ($base)" \
             "실제 미러 쌍 또는 기준 커밋($from..$to)이 이 호스트에 없다"
        return
    fi

    up_head="$(git -C "$up" rev-parse HEAD)"
    mi_head="$(git -C "$mi" rev-parse HEAD)"
    up_st="$(git -C "$up" status --porcelain)"
    mi_st="$(git -C "$mi" status --porcelain)"

    RUN_OUT="$(MIRROR_SYNC_CONFIG_DIR="$MODULE_DIR/config" \
        bash "$SYNC" --pair "$base" --from "$from" --to "$to" 2>&1)"
    RUN_RC=$?

    check_eq "배포 conf($base): dry-run 종료 코드 0" "0" "$RUN_RC"
    check_out_has "배포 conf($base): $want_copy" "$want_copy"
    check_out_has "배포 conf($base): $want_rest" "$want_rest"
    check_out_lacks "배포 conf($base): 죽은 패턴 경고가 없다" "매칭하지 않는 패턴"
    check_out_lacks "배포 conf($base): 신규 반입이 없다" "신규 반입 — 미러에 없던 파일"

    # UPSTREAM_ONLY 로 막아 둔 경로가 '복사' 로 분류되면 공개 미러로 새어나간다.
    # 패턴 한 글자 오타가 정확히 이 결과를 낸다.
    leaks="$(printf '%s\n' "$RUN_OUT" | grep '^  복사' | grep -cE "$leak_re")"
    check_eq "배포 conf($base): 반입 금지 경로가 복사로 분류되지 않는다" "0" "$leaks"

    check_eq "배포 conf($base): upstream HEAD 불변" "$up_head" "$(git -C "$up" rev-parse HEAD)"
    check_eq "배포 conf($base): mirror HEAD 불변" "$mi_head" "$(git -C "$mi" rev-parse HEAD)"
    check_eq "배포 conf($base): upstream 워킹트리 불변" "$up_st" "$(git -C "$up" status --porcelain)"
    check_eq "배포 conf($base): mirror 워킹트리 불변" "$mi_st" "$(git -C "$mi" status --porcelain)"
}

# base / from / to / 기대 복사줄 / 기대 보존·미반입·미삭제 줄 / 유출 검사 ERE
# from·to 는 저장소가 전진해도 결과가 변하지 않도록 sha 로 못박는다(2026-09-05 실측).
smoke_real_pair max9296 3f5915f 4fa9881 \
    "합계: 복사 58 (쓰기 58 / 삭제 0)" \
    "보존 6 · 미반입 278 · 미삭제 1" \
    'artifacts/|docs/superpowers/|\.github/'

smoke_real_pair gstApp 46fd6fa 77a2635 \
    "합계: 복사 84 (쓰기 75 / 삭제 0 / 삭제생략 9)" \
    "보존 1 · 미반입 54 · 미삭제 1" \
    '\.github/|docs/superpowers/|todos/|\.vscode/'

smoke_real_pair imx-vpu d8e9590 87deeb7 \
    "합계: 복사 6 (쓰기 6 / 삭제 0)" \
    "보존 5 · 미반입 18 · 미삭제 1" \
    '\.github/|make-for-imx8\.sh'

smoke_real_pair sc16is7xx 9f71cb9 093e069 \
    "합계: 복사 4 (쓰기 3 / 삭제 0 / 삭제생략 1)" \
    "보존 3 · 미반입 14 · 미삭제 1" \
    '\.clangd|\.github/|sc16is7xx-ext-ko-provenance'

# ══════════════════════════════════════════════════════════════════════════
printf '\n=== 집계 ===\n'
printf '검사 %d건 — 통과 %d / 실패 %d / 건너뜀 %d\n' \
    "$CHECKS" "$(( CHECKS - FAILURES - SKIPPED ))" "$FAILURES" "$SKIPPED"
if (( FAILURES != 0 )); then
    printf 'FAILED %d check(s)\n' "$FAILURES"
    exit 1
fi
printf 'ALL MIRROR SYNC TESTS PASSED\n'
