#!/usr/bin/env bash
# auto-commit-memory.sh — plane-aware Memory auto-commit: the writer seam for
# the AUTOMATIC save path (workspace v1; the #36 deferral, closed under #39).
#
# The PreToolUse commit gate never sees hook-context commits — a git call made
# inside a lifecycle handler is not a model tool call on ANY harness — so this
# seam is the auto path's ONLY protection. Posture mirrors save-commit-gate:
#   no manifest        -> legacy `git add Memory/ && git commit`, unchanged
#   workspace root     -> save_scope proof-bound, scope-staged commit in the
#                         shared repo; the proof is consumed after the attempt
#   declared/own child -> legacy commit in the CHILD repo only
#   unvalidatable      -> NO commit (fail closed), exit 0 so the session's
#                         hook chain never crashes on a refused save
#   inside shared repo but not the workspace root -> unattributable, skip
#
# stdout: one JSON line {committed, scope, commit_repo, reason}
# stderr: logs. Exit 0 for every non-usage outcome (skip is an outcome).
set -euo pipefail

TOOLS_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_DIR="${1:?usage: auto-commit-memory.sh PROJECT_DIR}"

log() { echo "[auto-commit-memory] $*" >&2; }

emit() {  # committed(bool-word) scope commit_repo reason
    if command -v jq >/dev/null 2>&1; then
        jq -cn --argjson c "$1" --arg s "$2" --arg r "$3" --arg why "$4" \
            '{committed:$c, scope:$s, commit_repo:$r, reason:$why}'
    else
        # Fallback for jq-less hosts: paths/reasons here are our own values
        # (no user input), so plain interpolation is acceptable for a log line.
        printf '{"committed":%s,"scope":"%s","commit_repo":"%s","reason":"%s"}\n' \
            "$1" "$2" "$3" "$4"
    fi
}

get_python_cmd() {
    if [[ -x "$PROJECT_DIR/.asha/.venv/bin/python3" ]]; then
        echo "$PROJECT_DIR/.asha/.venv/bin/python3"
    elif command -v python3 >/dev/null 2>&1; then
        echo "python3"
    fi
}

# legacy_commit REPO SCOPE LABEL — today's stanza, verbatim in behavior:
# stage Memory/ and commit; "nothing to commit" is a clean skip, not an error.
# Silence parity: the save pipeline checks the marker before ever reaching
# this seam, but a direct invocation must refuse what the gate would deny.
legacy_commit() {
    local repo="$1" scope="$2" label="$3"
    if [[ -f "$repo/Work/markers/silence" ]]; then
        log "silence marker active in $repo — Memory persistence disabled, skipping"
        emit false "$scope" "$repo" "silence marker active — Memory persistence disabled"
        return 0
    fi
    if git -C "$repo" add Memory/ >/dev/null 2>&1 \
        && git -C "$repo" commit -m "Session auto-save${label}: $(date -u '+%Y-%m-%d %H:%M UTC')" >/dev/null 2>&1; then
        log "committed Memory/ in $repo"
        emit true "$scope" "$repo" "committed"
    else
        log "nothing to commit in $repo (or git refused)"
        emit false "$scope" "$repo" "nothing to commit (or git refused)"
    fi
}

# ---- plane routing: same existence walk as the commit gate ----
WS_MANIFEST=""
if [[ -f "$TOOLS_DIR/project-root.sh" ]]; then
    # shellcheck disable=SC1091
    source "$TOOLS_DIR/project-root.sh"
    if declare -F asha_find_workspace_manifest >/dev/null 2>&1; then
        WS_MANIFEST="$(asha_find_workspace_manifest "$PROJECT_DIR")"
    fi
fi

if [[ -z "$WS_MANIFEST" ]]; then
    legacy_commit "$PROJECT_DIR" "repo" ""
    exit 0
fi

# Manifest present: validation is mandatory. A broken resolver means we
# cannot attribute the commit to a plane — fail closed (skip), never guess.
PYTHON_CMD="$(get_python_cmd)"
SS_TOOL="$TOOLS_DIR/save_scope.py"
MAPPING=""
if [[ -n "$PYTHON_CMD" && -f "$SS_TOOL" ]]; then
    MAPPING="$("$PYTHON_CMD" "$SS_TOOL" resolve --scope workspace --start "$PROJECT_DIR" 2>/dev/null)" || MAPPING=""
fi

json_field() {  # json key
    printf '%s' "$1" | "$PYTHON_CMD" -c \
        "import sys,json;print(json.load(sys.stdin).get('$2',''))" 2>/dev/null || true
}

PLANE_BASE=""; COMMIT_REPO=""; MEM_REL=""
if [[ -n "$MAPPING" && -n "$PYTHON_CMD" ]]; then
    PLANE_BASE="$(json_field "$MAPPING" plane_base)"
    COMMIT_REPO="$(json_field "$MAPPING" commit_repo)"
    MEM_REL="$(json_field "$MAPPING" memory_rel)"
fi

if [[ -z "$PLANE_BASE" || -z "$COMMIT_REPO" ]]; then
    log "workspace manifest at ${WS_MANIFEST%/.asha/workspace.json} cannot be validated — fail closed, skipping auto-commit"
    emit false "none" "" "manifest present but unvalidatable — fail closed (check: asha workspace status)"
    exit 0
fi

canon() { cd -P "$1" >/dev/null 2>&1 && pwd; }
PD_CANON="$(canon "$PROJECT_DIR" || echo "$PROJECT_DIR")"
PB_CANON="$(canon "$PLANE_BASE" || echo "$PLANE_BASE")"

if [[ "$PD_CANON" == "$PB_CANON" ]]; then
    # The session ran AT the workspace root: its Memory/ IS the workspace
    # operational plane. Proof-bind and scope-stage the commit.
    if [[ -f "$PLANE_BASE/Work/markers/silence" ]]; then
        log "workspace silence marker active — Memory persistence disabled, skipping"
        emit false "workspace" "$COMMIT_REPO" "silence marker active for the workspace plane"
        exit 0
    fi
    MEM_ROOT="$(json_field "$MAPPING" memory_root)"
    REL="${MEM_REL:-Memory}"
    if [[ -f "$MEM_ROOT/activeContext.md" ]]; then
        # Bind the proof to the post-synthesis state, verify, then commit.
        # Verify failure here means a race mutated activeContext between the
        # proof and now — refuse rather than commit unproven bytes.
        if ! "$PYTHON_CMD" "$SS_TOOL" write-proof --scope workspace --start "$PROJECT_DIR" >/dev/null 2>&1; then
            log "could not write the workspace save proof — skipping"
            emit false "workspace" "$COMMIT_REPO" "save proof could not be written — fail closed"
            exit 0
        fi
        if ! "$PYTHON_CMD" "$SS_TOOL" verify --scope workspace --start "$PROJECT_DIR" >/dev/null 2>&1; then
            rm -f "$PLANE_BASE/Work/markers/save-gates-ok" 2>/dev/null || true
            log "workspace save proof failed verification — skipping"
            emit false "workspace" "$COMMIT_REPO" "save proof failed verification — fail closed"
            exit 0
        fi
    else
        log "first-init: no activeContext.md at the workspace plane yet (proof skipped, mirrors the gate)"
    fi
    if git -C "$COMMIT_REPO" add "$REL/" >/dev/null 2>&1 \
        && git -C "$COMMIT_REPO" commit -m "Session auto-save (workspace): $(date -u '+%Y-%m-%d %H:%M UTC')" >/dev/null 2>&1; then
        RESULT_LINE="$(emit true "workspace" "$COMMIT_REPO" "committed")"
        log "committed $REL/ in $COMMIT_REPO"
    else
        RESULT_LINE="$(emit false "workspace" "$COMMIT_REPO" "nothing to commit (or git refused)")"
        log "nothing to commit in $COMMIT_REPO (or git refused)"
    fi
    # Consume-on-use, same as the gate: an auto proof must never linger to
    # authorize a later ad-hoc commit while activeContext is unchanged.
    rm -f "$PLANE_BASE/Work/markers/save-gates-ok" 2>/dev/null || true
    printf '%s\n' "$RESULT_LINE"
    exit 0
fi

# Not the workspace root: only a repo of its OWN below the workspace may take
# the legacy child commit. A directory inside the SHARED repo would land the
# commit in the workspace plane unattributed — refuse.
CHILD_TOP="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
CR_CANON="$(canon "$COMMIT_REPO" || echo "$COMMIT_REPO")"
CT_CANON=""
[[ -n "$CHILD_TOP" ]] && CT_CANON="$(canon "$CHILD_TOP" || echo "$CHILD_TOP")"

if [[ -n "$CT_CANON" && "$CT_CANON" != "$CR_CANON" ]]; then
    legacy_commit "$CHILD_TOP" "repo" ""
    exit 0
fi

log "$PROJECT_DIR is inside the shared workspace repo but is not the workspace root — unattributable, skipping auto-commit"
emit false "none" "" "inside the shared workspace repo but not at the workspace root — unattributable, fail closed"
exit 0
