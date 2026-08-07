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
    local dir="$1"
    # Does this commit touch Memory/? Two signals, either suffices:
    #   (a) the command head references Memory/
    #   (b) the staged set already contains Memory/ paths
    local touches=0
    printf '%s' "$CMD_HEAD" | grep -q 'Memory/' && touches=1
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

if [[ -z "$PLANE_BASE" || -z "$COMMIT_REPO" || -z "$MEM_ROOT" ]]; then
    # Manifest present but not validatable (invalid manifest, or
    # python3/save_scope missing). Fail closed for anything with a visible
    # Memory signal; a commit with no Memory signal anywhere stays allowed.
    STAGED_ANY=0
    [[ -n "$CHILD_TOP" ]] \
        && git -C "$CHILD_TOP" diff --cached --name-only 2>/dev/null | grep -q '^Memory/' \
        && STAGED_ANY=1
    if [[ $HEAD_REF -eq 1 || $STAGED_ANY -eq 1 ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Memory/ commit refused: a workspace manifest exists at ${WS_MANIFEST%/.asha/workspace.json} but cannot be validated (invalid manifest, or python3/save_scope.py unavailable) — failing closed. Check: asha workspace status" \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        exit $?
    fi
    exit 0
fi

REL_MEM="${MEM_ROOT#"$COMMIT_REPO"/}"
STAGED_WS=0
git -C "$COMMIT_REPO" diff --cached --name-only 2>/dev/null | grep -q "^$REL_MEM/" && STAGED_WS=1
STAGED_CHILD=0
if [[ -n "$CHILD_TOP" && "$CHILD_TOP" != "$COMMIT_REPO" ]]; then
    git -C "$CHILD_TOP" diff --cached --name-only 2>/dev/null | grep -q '^Memory/' && STAGED_CHILD=1
fi

# A bare Memory/ reference in the command head attributes to the payload-cwd
# repo (the default git target). A compound that -C's into ANOTHER repo is
# deliberately not parsed; if its plane lacks staged state now, its own
# commit-stage state check or proof requirement catches it — conservative,
# never permissive.
CAND_CHILD=$STAGED_CHILD
CAND_WS=$STAGED_WS
if [[ $HEAD_REF -eq 1 ]]; then
    if [[ -n "$CHILD_TOP" && "$CHILD_TOP" != "$COMMIT_REPO" ]]; then
        CAND_CHILD=1
    else
        CAND_WS=1
    fi
fi

if [[ $CAND_CHILD -eq 1 && $CAND_WS -eq 1 ]]; then
    pretooluse_policy_deny "save-commit-gate" \
        "Memory/ commit refused: BOTH the child repository and the workspace plane have Memory changes in flight — one commit cannot be proven for two planes. Commit them separately (child: /session:save; workspace: /session:save --scope workspace)." \
        " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
    exit $?
fi

if [[ $CAND_CHILD -eq 1 ]]; then
    gate_check_plane "$CHILD_TOP"
    exit $?
fi

if [[ $CAND_WS -eq 1 ]]; then
    if [[ -f "$PLANE_BASE/Work/markers/silence" ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Workspace Memory commit refused: the workspace's Work/markers/silence is active." \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        exit $?
    fi
    # First-init exception, same as the single-plane gate.
    [[ -f "$MEM_ROOT/activeContext.md" ]] || exit 0
    WS_REASON="$(python3 "$SS_TOOL" verify --scope workspace --start "$PROJECT_DIR" 2>/dev/null)" && WS_OK=0 || WS_OK=$?
    if [[ $WS_OK -ne 0 ]]; then
        pretooluse_policy_deny "save-commit-gate" \
            "Workspace Memory commit refused: ${WS_REASON:-no valid workspace save proof}. Run /session:save --scope workspace (it writes the proof via save_scope.py before committing)." \
            " (override: ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1)"
        exit $?
    fi
    exit 0
fi

exit 0
