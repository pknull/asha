#!/bin/bash
# save-commit-gate.sh — PreToolUse enforcement: no git commit may touch
# Memory/ until the save preflight gates have passed.
#
# The old contract was convention ("save.md says STOP on hard fail") plus a
# post-commit Stop-hook net that gives up after 3 attempts. This handler makes
# the refusal mechanical: a `git commit` whose command references Memory/ (or
# whose staged set includes Memory/ paths) is DENIED unless
# Work/markers/save-gates-ok exists AND its stored activeContext.md sha256
# matches disk. The marker is written only by save-preflight-env.sh after all
# hard gates pass, and any later mutation of activeContext.md invalidates it
# automatically — you cannot pass the gates and then commit something else.
#
# FAIL-OPEN on internal errors (missing jq, unparseable input, no project):
# a guard that fails closed bricks every commit, strictly worse than the gap.
# Override escape hatch: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1.
set -uo pipefail   # deliberately NOT -e: we own every exit code

SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
[[ -f "$SELF_DIR/harness-response.sh" ]] && source "$SELF_DIR/harness-response.sh" 2>/dev/null || exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)"
[[ "$TOOL_NAME" == "Bash" ]] || exit 0

CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[[ -n "$CMD" ]] || exit 0

# Only git-commit commands are in scope.
printf '%s' "$CMD" | grep -Eq 'git([[:space:]]+--?[A-Za-z][^|;&]*)?[[:space:]]+commit\b' || exit 0

# Override escape hatch (mirrors policy-guard convention).
[[ "${ASHA_ALLOW_UNGATED_MEMORY_COMMIT:-}" == "1" ]] && exit 0

CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
PROJECT_DIR="${CWD:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"
[[ -d "$PROJECT_DIR" ]] || exit 0

# Signal (a) inspects ONLY the portion of the command before the `commit`
# subcommand. A commit MESSAGE that merely mentions Memory/ — as every commit
# describing this very gate does — is not a staging operation and must never
# trigger; grepping the whole command string (message included) is friendly
# fire. `git add Memory/ && git commit …` still matches: the add precedes the
# commit token.
CMD_HEAD="${CMD%%commit*}"

# gate_check_plane DIR — the original single-plane gate, byte-for-byte (Test
# 9c golden corpus pins it). Used for the no-workspace path AND for the child
# plane under a workspace.
gate_check_plane() {
    local dir="$1" forced="${2:-0}"
    # Does this commit touch Memory/? Two signals, either suffices:
    #   (a) the command head references Memory/
    #   (b) the staged set already contains Memory/ paths
    # A caller that ALREADY established candidacy (workspace branch: dirty
    # state + -a/pathspec) passes forced=1 — re-deriving from staged-only
    # here would re-open the commit -a / pathspec bypass (pass-2).
    local touches="$forced"
    [[ $touches -eq 0 ]] && printf '%s' "$CMD_HEAD" | grep -q 'Memory/' && touches=1
    if [[ $touches -eq 0 ]]; then
        if git -C "$dir" diff --cached --name-only 2>/dev/null | grep -q '^Memory/'; then
            touches=1
        fi
    fi
    [[ $touches -eq 1 ]] || return 0

    # Nothing to gate before the project has an activeContext (e.g.
    # /session:init's very first Memory commit).
    local ac_file="$dir/Memory/activeContext.md"
    [[ -f "$ac_file" ]] || return 0

    # Silence marker: Memory persistence is disabled — a Memory commit under
    # silence is a policy violation regardless of gates.
    if [[ -f "$dir/Work/markers/silence" ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Memory/ commit refused: Work/markers/silence is active (Memory persistence disabled). Run /session:restore first." \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        return $?
    fi

    local marker="$dir/Work/markers/save-gates-ok"
    local remedy="Run: \"\$ASHA_ROOT/plugins/session/tools/save-preflight-env.sh\" — it resolves the environment, verifies the save plugin, checks Memory notes against disk, and opens this gate only when all continuity gates pass."

    if [[ ! -f "$marker" ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Memory/ commit refused: save preflight gates have not passed (no Work/markers/save-gates-ok). $remedy" \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        return $?
    fi

    local marker_sha disk_sha
    marker_sha="$(jq -r '.ac_sha256 // empty' "$marker" 2>/dev/null || true)"
    disk_sha="$(sha256sum "$ac_file" 2>/dev/null | cut -d' ' -f1 || true)"
    if [[ -z "$marker_sha" || -z "$disk_sha" || "$marker_sha" != "$disk_sha" ]]; then
        rm -f "$marker" 2>/dev/null || true
        pretooluse_policy_deny "save-commit-gate" \
            "Memory/ commit refused: activeContext.md changed AFTER the gates passed (marker hash ${marker_sha:0:12}… != disk ${disk_sha:0:12}…) — gates are stale. $remedy" \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        return $?
    fi

    # Gates passed against exactly this activeContext — allow the commit. The
    # marker is left in place for the turn; the Stop-hook net still runs after.
    return 0
}

# ---- plane routing (workspace v1, issue #36 — state-based, never parsed) ----
# A pure-bash EXISTENCE walk decides legacy-vs-workspace: no python on the
# hot path for non-workspace users, byte-identical legacy behavior (golden
# corpus). When a manifest EXISTS, validation is mandatory and a broken
# resolver DENIES — a manifest-present machine silently falling back to
# child-only gating is exactly the bypass the design memo forbids.
WS_MANIFEST=""
LIB="$SELF_DIR/../../tools/project-root.sh"
if [[ -f "$LIB" ]]; then
    # shellcheck disable=SC1090
    source "$LIB" 2>/dev/null || true
    if declare -F asha_find_workspace_manifest >/dev/null 2>&1; then
        WS_MANIFEST="$(asha_find_workspace_manifest "$PROJECT_DIR")"
    fi
fi

if [[ -z "$WS_MANIFEST" ]]; then
    gate_check_plane "$PROJECT_DIR"
    exit $?
fi

# Workspace branch. Candidate planes are selected by REPOSITORY STATE (what
# is actually staged where), never by parsing -C out of the command — a
# spoofed flag is irrelevant when state decides (design memo, PR #30 lesson).
SS_TOOL="$SELF_DIR/../../tools/save_scope.py"
MAPPING=""
if command -v python3 >/dev/null 2>&1 && [[ -f "$SS_TOOL" ]]; then
    MAPPING="$(python3 "$SS_TOOL" resolve --scope workspace --start "$PROJECT_DIR" 2>/dev/null)" || MAPPING=""
fi
PLANE_BASE="$(printf '%s' "$MAPPING" | jq -r '.plane_base // empty' 2>/dev/null || true)"
COMMIT_REPO="$(printf '%s' "$MAPPING" | jq -r '.commit_repo // empty' 2>/dev/null || true)"
MEM_ROOT="$(printf '%s' "$MAPPING" | jq -r '.memory_root // empty' 2>/dev/null || true)"

CHILD_TOP="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
HEAD_REF=0
printf '%s' "$CMD_HEAD" | grep -q 'Memory/' && HEAD_REF=1
WS_GUESS="${WS_MANIFEST%/.asha/workspace.json}"

# _mem_hit REPO REL MODE — does the repo have Memory-plane paths in the given
# git listing? Literal prefix comparison, never regex (a REL with metachars
# must not false-match). Empty REL means the whole repo IS the plane.
_mem_hit() {
    local repo="$1" rel="$2" mode="$3" p
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if [[ -z "$rel" || "$p" == "$rel"/* ]]; then return 0; fi
    done < <(
        if [[ "$mode" == staged ]]; then
            git -C "$repo" diff --cached --name-only 2>/dev/null
        else
            # tracked modifications only (-uno): commit -a commits these
            git -C "$repo" status --porcelain -uno 2>/dev/null | cut -c4-
        fi
    )
    return 1
}
# any Memory state at all (staged, dirty-tracked, or untracked) — used for
# the compound rule's cleanliness requirement, where an about-to-be-added
# NEW file matters too.
_mem_any() {
    local repo="$1" rel="$2" p
    _mem_hit "$repo" "$rel" staged && return 0
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if [[ -z "$rel" || "$p" == "$rel"/* ]]; then return 0; fi
    done < <(git -C "$repo" status --porcelain 2>/dev/null | cut -c4-)
    return 1
}

if [[ -z "$PLANE_BASE" || -z "$COMMIT_REPO" || -z "$MEM_ROOT" ]]; then
    # Manifest present but not validatable (invalid manifest, or
    # python3/save_scope missing). Fail closed for anything with a visible
    # Memory signal IN EITHER PLANE — the workspace root is known from the
    # walk even when the resolver is not (pass-2: checking only the child
    # left workspace-staged Memory ungated exactly when the gate was blind).
    SIGNAL=0
    [[ $HEAD_REF -eq 1 ]] && SIGNAL=1
    [[ -n "$CHILD_TOP" ]] && _mem_hit "$CHILD_TOP" "Memory" staged && SIGNAL=1
    [[ -n "$CHILD_TOP" ]] && _mem_hit "$CHILD_TOP" "Memory" dirty && SIGNAL=1
    _mem_hit "$WS_GUESS" "Memory" staged && SIGNAL=1
    _mem_hit "$WS_GUESS" "Memory" dirty && SIGNAL=1
    if [[ $SIGNAL -eq 1 ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Memory/ commit refused: a workspace manifest exists at $WS_GUESS but cannot be validated (invalid manifest, or python3/save_scope.py unavailable) — failing closed. Check: asha workspace status" \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        exit $?
    fi
    exit 0
fi

REL_MEM="$(printf '%s' "$MAPPING" | jq -r '.memory_rel // "Memory"' 2>/dev/null || echo Memory)"

# Commit-tail flags, with quoted spans stripped so a commit MESSAGE that
# mentions Memory/ or -a never counts (same principle as CMD_HEAD).
COMMIT_TAIL="${CMD#*commit}"
TAIL_STRIPPED="$(printf '%s' "$COMMIT_TAIL" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g" 2>/dev/null || true)"
HAS_ALL=0
printf '%s' "$TAIL_STRIPPED" | grep -Eq -- '(^|[[:space:]])--all([[:space:]]|$)|(^|[[:space:]])-[a-z]*a[a-z]*([[:space:]]|$)' && HAS_ALL=1
PATHSPEC_MEM=0
printf '%s' "$TAIL_STRIPPED" | grep -q 'Memory/' && PATHSPEC_MEM=1

# Candidate planes by STATE: staged always counts; dirty-tracked counts when
# the commit form can pick it up directly (-a/--all, or an unquoted Memory
# pathspec after the commit token — pass-2: staged-only let `commit -a` and
# `git commit Memory/x` bypass the gate entirely).
CAND_CHILD=0
CAND_WS=0
if [[ -n "$CHILD_TOP" && "$CHILD_TOP" != "$COMMIT_REPO" ]]; then
    _mem_hit "$CHILD_TOP" "Memory" staged && CAND_CHILD=1
    [[ $HAS_ALL -eq 1 || $PATHSPEC_MEM -eq 1 ]] \
        && _mem_hit "$CHILD_TOP" "Memory" dirty && CAND_CHILD=1
fi
_mem_hit "$COMMIT_REPO" "$REL_MEM" staged && CAND_WS=1
[[ $HAS_ALL -eq 1 || $PATHSPEC_MEM -eq 1 ]] \
    && _mem_hit "$COMMIT_REPO" "$REL_MEM" dirty && CAND_WS=1

ws_plane_check() {   # verify + CONSUME the workspace proof
    if [[ -f "$PLANE_BASE/Work/markers/silence" ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Workspace Memory commit refused: the workspace's Work/markers/silence is active." \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        return $?
    fi
    [[ -f "$MEM_ROOT/activeContext.md" ]] || return 0   # first-init
    local reason ok=0
    reason="$(python3 "$SS_TOOL" verify --scope workspace --start "$PROJECT_DIR" 2>/dev/null)" || ok=$?
    if [[ $ok -ne 0 ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Workspace Memory commit refused: ${reason:-no valid workspace save proof}. Run /session:save --scope workspace (it writes the proof via save_scope.py before committing)." \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        return $?
    fi
    # Consume-on-use: a proof authorizes exactly one commit (pass-2: an
    # unconsumed proof replayed indefinitely while activeContext was
    # unchanged). If the commit itself then fails, /save re-proves.
    rm -f "$PLANE_BASE/Work/markers/save-gates-ok" 2>/dev/null || true
    return 0
}

# _legacy_marker_valid DIR — silent validity probe of the single-plane
# marker (used by the compound rule; gate_check_plane does the deny texts).
_legacy_marker_valid() {
    local dir="$1" m="$1/Work/markers/save-gates-ok" ms ds
    [[ -f "$m" ]] || return 1
    ms="$(jq -r '.ac_sha256 // empty' "$m" 2>/dev/null || true)"
    ds="$(sha256sum "$dir/Memory/activeContext.md" 2>/dev/null | cut -d' ' -f1 || true)"
    [[ -n "$ms" && -n "$ds" && "$ms" == "$ds" ]]
}

# Compound add-of-Memory-then-commit: PreToolUse runs BEFORE the add, so
# staged state cannot attribute the plane, and pass-2 proved cwd attribution
# is launderable via -C. Rule: allow only when EXACTLY ONE plane holds a
# valid proof AND the other plane has no Memory state at all (including
# untracked — the add would stage it); anything else denies.
if printf '%s' "$CMD_HEAD" | grep -Eq 'add[^|;&]*Memory/'; then
    CHILD_READY=0
    WS_READY=0
    if [[ -n "$CHILD_TOP" && "$CHILD_TOP" != "$COMMIT_REPO" ]] \
        && _legacy_marker_valid "$CHILD_TOP" \
        && ! _mem_any "$COMMIT_REPO" "$REL_MEM"; then
        CHILD_READY=1
    fi
    if python3 "$SS_TOOL" verify --scope workspace --start "$PROJECT_DIR" >/dev/null 2>&1; then
        if [[ -z "$CHILD_TOP" || "$CHILD_TOP" == "$COMMIT_REPO" ]] \
            || ! _mem_any "$CHILD_TOP" "Memory"; then
            WS_READY=1
        fi
    fi
    if [[ $CHILD_READY -eq 1 && $WS_READY -eq 0 ]]; then
        gate_check_plane "$CHILD_TOP"
        exit $?
    fi
    if [[ $WS_READY -eq 1 && $CHILD_READY -eq 0 ]]; then
        ws_plane_check
        exit $?
    fi
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: a compound add-and-commit under a workspace cannot be attributed to one plane (proofs: child=$CHILD_READY workspace=$WS_READY). Stage first and commit separately, or use /session:save (child) / /session:save --scope workspace." \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

if [[ $CAND_CHILD -eq 1 && $CAND_WS -eq 1 ]]; then
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: BOTH the child repository and the workspace plane have Memory changes in flight — one commit cannot be proven for two planes. Commit them separately (child: /session:save; workspace: /session:save --scope workspace)." \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

if [[ $CAND_CHILD -eq 1 ]]; then
    gate_check_plane "$CHILD_TOP" 1
    exit $?
fi

if [[ $CAND_WS -eq 1 ]]; then
    ws_plane_check
    exit $?
fi

exit 0
