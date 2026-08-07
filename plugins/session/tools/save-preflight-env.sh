#!/usr/bin/env bash
# save-preflight-env.sh — single-entry preflight for the session-save pipeline.
#
# Run this BEFORE any save synthesis or Memory/ commit. Four phases:
#
#   1. ENV        Auto-detect and export ASHA_ROOT, PROJECT_DIR, PYTHON_CMD,
#                 ASHA_HARNESS, ASHA_SESSION_ID. Layered detection; every
#                 resolved root is VALIDATED (must actually contain the save
#                 tools) before it is trusted.
#   2. PLUGIN     Verify the save plugin's required tools exist on disk. If the
#                 plugin is absent/partial, point at the documented manual
#                 pipeline (docs/save-manual-pipeline.md) — or print the
#                 embedded copy when even the doc is unreachable — and exit 3.
#   3. DISK TRUTH Delegated to save_preflight.py gate_disk_truth: disk is
#                 ground truth over Memory notes; contradictions are flagged.
#   4. GATES      Run the continuity gates (save_preflight.py). On pass, write
#                 a hash-bound Work/markers/save-gates-ok marker that the
#                 save-commit-gate PreToolUse hook requires before ANY git
#                 commit touching Memory/ is allowed. On hard fail: no marker,
#                 exit 1 — the commit stays refused.
#
# Modes:
#   --guard       (default) gates enforce; marker written on pass
#   --report      dry run; no marker writes, gates in report mode
#   --print-env   emit "export K=V" lines on STDOUT (diagnostics on STDERR)
#                 so callers can `eval "$(save-preflight-env.sh --print-env)"`
#
# Exit codes:
#   0  all phases passed (or --report completed)
#   1  continuity gate hard failure — DO NOT COMMIT
#   2  environment resolution failure (no ASHA_ROOT / no project)
#   3  save plugin missing or partial — use the manual pipeline
# fail-open by design: no set -e — a handler crash must never block the session
set -uo pipefail

MODE="guard"
PRINT_ENV=0
ARG_PROJECT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --guard)  MODE="guard" ;;
        --report) MODE="report" ;;
        --print-env) PRINT_ENV=1 ;;
        --project-dir) ARG_PROJECT_DIR="${2:-}"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[save-preflight] unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

log() { echo "[save-preflight] $*" >&2; }

# ---------------------------------------------------------------------------
# Phase 1 — environment resolution
# ---------------------------------------------------------------------------

# A candidate ASHA_ROOT is only accepted if it actually contains the save
# engine. This is what the per-block jq snippets in save.md never checked:
# a stale config.json pointing at a moved repo passed silently and failed
# five steps later.
valid_asha_root() {
    [[ -n "$1" && -f "$1/plugins/session/tools/save_preflight.py" ]]
}

resolve_asha_root() {
    # Layer 1: existing env, validated
    if valid_asha_root "${ASHA_ROOT:-}"; then
        echo "$ASHA_ROOT"; return 0
    fi
    # Layer 2: ~/.asha/config.json asha_root, validated
    if command -v jq >/dev/null 2>&1 && [[ -f "$HOME/.asha/config.json" ]]; then
        local cfg_root
        cfg_root=$(jq -r '.asha_root // empty' "$HOME/.asha/config.json" 2>/dev/null || true)
        if valid_asha_root "$cfg_root"; then
            echo "$cfg_root"; return 0
        fi
        [[ -n "$cfg_root" ]] && log "config.json asha_root=$cfg_root is stale (save tools not found there); continuing detection"
    fi
    # Layer 3: this script's own location (survives symlink mounts — realpath
    # resolves back into the source tree; tools/ is three levels below root)
    local self_dir
    self_dir="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 && pwd)" || true
    if [[ -n "$self_dir" ]]; then
        local candidate="${self_dir%/plugins/session/tools}"
        if valid_asha_root "$candidate"; then
            echo "$candidate"; return 0
        fi
    fi
    return 1
}

# Shared project-root resolver (single source of truth for root detection).
# BASH_SOURCE, not $0 (correct when sourced or symlink-invoked); missing file
# is reported as the documented partial-install failure, not a bare
# command-not-found from the wrapper below.
if [[ -f "${BASH_SOURCE[0]%/*}/project-root.sh" ]]; then
    source "${BASH_SOURCE[0]%/*}/project-root.sh"
fi

# Full layer set (arg, env, git-with-Memory, upward walk) WITH the $HOME
# guard — the union detector, pinned by Test 9b. Empty output + rc 1 when
# nothing resolves; the consumer block exits 2 with remediation.
resolve_project_dir() {
    local dir
    if ! declare -F asha_detect_project_root >/dev/null 2>&1; then
        # Partial install: stay silent here so the PLUGIN phase below reports
        # the documented exit-3 remediation instead of an env-phase failure.
        return 0
    fi
    dir="$(asha_detect_project_root "env,git,walk" 1 "$ARG_PROJECT_DIR")"
    [[ -n "$dir" ]] && echo "$dir"
}

detect_harness() {
    if   [[ -n "${ASHA_HARNESS:-}" ]];   then echo "$ASHA_HARNESS"
    elif [[ -n "${CLAUDECODE:-}" ]];     then echo "claude"
    elif [[ -n "${COPILOT_CLI:-}" ]];    then echo "copilot"
    elif [[ -n "${CODEX_THREAD_ID:-}" || -n "${CODEX_MANAGED_BY_NPM:-}" ]]; then echo "codex"
    else echo "unknown"
    fi
}

detect_session_id() {
    case "$1" in
        claude)   echo "${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}" ;;
        copilot)  echo "${ASHA_SESSION_ID:-${COPILOT_SESSION_ID:-}}" ;;
        codex)    echo "${ASHA_SESSION_ID:-${CODEX_THREAD_ID:-}}" ;;
        *)        echo "${ASHA_SESSION_ID:-}" ;;
    esac
}

ASHA_ROOT="$(resolve_asha_root)" || {
    log "FAIL(env): cannot resolve a valid ASHA_ROOT. Tried: \$ASHA_ROOT, ~/.asha/config.json asha_root, script location."
    log "Remediation: run ./install.sh from the asha checkout, or launch via the asha wrapper."
    exit 2
}
export ASHA_ROOT

PROJECT_DIR="$(resolve_project_dir)" || true
if [[ -z "${PROJECT_DIR:-}" ]]; then
    log "FAIL(env): no project directory (no CLAUDE_PROJECT_DIR, no git root or ancestor with Memory/)."
    log "Remediation: run from inside an initialized project, or pass --project-dir."
    exit 2
fi
export PROJECT_DIR CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PROJECT_DIR}"

if [[ -x "$PROJECT_DIR/.asha/.venv/bin/python3" ]]; then
    PYTHON_CMD="$PROJECT_DIR/.asha/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
else
    log "FAIL(env): no python3 available (needed by every synthesis/gate tool)."
    exit 2
fi
export PYTHON_CMD

ASHA_HARNESS_DETECTED="$(detect_harness)"
# Never export the "unknown" placeholder: resolve_identity rejects it as an
# invalid harness instead of falling back to its own detection, so a poisoned
# export turns a recoverable unknown into a confusing hard fail downstream.
if [[ "$ASHA_HARNESS_DETECTED" != "unknown" ]]; then
    export ASHA_HARNESS="${ASHA_HARNESS:-$ASHA_HARNESS_DETECTED}"
fi
ASHA_SESSION_ID_DETECTED="$(detect_session_id "$ASHA_HARNESS_DETECTED")"
[[ -n "$ASHA_SESSION_ID_DETECTED" ]] && export ASHA_SESSION_ID="${ASHA_SESSION_ID:-$ASHA_SESSION_ID_DETECTED}"

log "env: ASHA_ROOT=$ASHA_ROOT"
log "env: PROJECT_DIR=$PROJECT_DIR"
log "env: PYTHON_CMD=$PYTHON_CMD harness=$ASHA_HARNESS_DETECTED session_id=${ASHA_SESSION_ID:-<unknown>}"

if [[ "$PRINT_ENV" == "1" ]]; then
    printf 'export ASHA_ROOT=%q\n'    "$ASHA_ROOT"
    printf 'export PROJECT_DIR=%q\n'  "$PROJECT_DIR"
    printf 'export PYTHON_CMD=%q\n'   "$PYTHON_CMD"
    [[ -n "${ASHA_HARNESS:-}" ]] && printf 'export ASHA_HARNESS=%q\n' "$ASHA_HARNESS"
    [[ -n "${ASHA_SESSION_ID:-}" ]] && printf 'export ASHA_SESSION_ID=%q\n' "$ASHA_SESSION_ID"
fi

# ---------------------------------------------------------------------------
# Phase 2 — save plugin verification (manual fallback if absent)
# ---------------------------------------------------------------------------

REQUIRED_TOOLS=(
    "plugins/session/tools/save-session.sh"
    "plugins/session/tools/pattern_analyzer.py"
    "plugins/session/tools/save_preflight.py"
    "plugins/session/tools/save_guardrail.py"
    "plugins/session/tools/push_retry.py"
    "plugins/session/tools/jsonl_reader.py"
    "plugins/session/tools/event_store.py"
    # Shared resolvers — every save path depends on them since the 2026-08-07
    # detector consolidation. Absent, the pipeline must report the documented
    # partial-install failure (exit 3 + manual fallback), not a bare
    # command-not-found from the resolver wrapper.
    "plugins/session/tools/project-root.sh"
    "plugins/session/tools/project_root.py"
    "plugins/session/tools/workspace_manifest.py"
)
MISSING=()
for rel in "${REQUIRED_TOOLS[@]}"; do
    [[ -f "$ASHA_ROOT/$rel" ]] || MISSING+=("$rel")
done

print_embedded_manual_pipeline() {
    cat >&2 <<'MANUAL'
[save-preflight] MANUAL SAVE PIPELINE (plugin unavailable)
  1. Write Memory/activeContext.md by hand:
     - Lead section: "## What Was Accomplished (YYYY-MM-DD — topic)" with
       "<!-- wwa-session: <session-id> -->" as its first body line, then a
       concrete narrative (file paths, decisions, blockers).
     - "## Next Steps": actionable cold-start items, never "Review and plan
       next session".
  2. Verify against DISK (disk is ground truth): every file path the notes
     reference must exist; remove or correct claims that contradict disk.
  3. Update the frontmatter lastUpdated to the current UTC time.
  4. git add Memory/ && git commit -m "Session save (manual): <summary>"
  5. git push, or record the unpushed HEAD in your next session's notes.
MANUAL
}

if [[ ${#MISSING[@]} -gt 0 ]]; then
    log "FAIL(plugin): save plugin incomplete under $ASHA_ROOT — missing:"
    for m in "${MISSING[@]}"; do log "  - $m"; done
    if [[ -f "$ASHA_ROOT/docs/save-manual-pipeline.md" ]]; then
        log "Fallback: follow the documented manual pipeline: $ASHA_ROOT/docs/save-manual-pipeline.md"
    else
        print_embedded_manual_pipeline
    fi
    exit 3
fi
log "plugin: all ${#REQUIRED_TOOLS[@]} required save tools present"

# ---------------------------------------------------------------------------
# Silence marker — durable user policy; nothing to gate when persistence is off
# ---------------------------------------------------------------------------
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    log "silence marker active — Memory persistence is disabled; no gates run, no marker written."
    log "A Memory/ commit under silence is a policy violation; the commit gate stays closed."
    exit 0
fi

# ---------------------------------------------------------------------------
# Phases 3+4 — disk-truth + continuity gates (engine: save_preflight.py)
# ---------------------------------------------------------------------------

GATE_MODE="guard"
[[ "$MODE" == "report" ]] && GATE_MODE="report"

GATE_ARGS=(--mode "$GATE_MODE" --skip-push --project-dir "$PROJECT_DIR")
[[ -n "${ASHA_SESSION_ID:-}" ]] && GATE_ARGS+=(--session-id "$ASHA_SESSION_ID")
[[ -n "${ASHA_TRANSCRIPT_PATH:-}" ]] && GATE_ARGS+=(--transcript "$ASHA_TRANSCRIPT_PATH")

if ! "$PYTHON_CMD" "$ASHA_ROOT/plugins/session/tools/save_preflight.py" "${GATE_ARGS[@]}" >&2; then
    log "FAIL(gates): continuity gate hard failure — commit is REFUSED until gates pass."
    log "Fix the flagged issue (see table above and Memory/events/save-preflight.jsonl), then re-run."
    rm -f "$PROJECT_DIR/Work/markers/save-gates-ok" 2>/dev/null || true
    exit 1
fi

if [[ "$MODE" == "report" ]]; then
    log "report mode: gates evaluated, no marker written."
    exit 0
fi

# ---------------------------------------------------------------------------
# Marker — hash-bound proof that gates passed against THIS activeContext.
# Consumed by hooks/handlers/save-commit-gate.sh; a later mutation of
# activeContext.md invalidates the marker automatically (hash mismatch).
# ---------------------------------------------------------------------------
MARKER_DIR="$PROJECT_DIR/Work/markers"
mkdir -p "$MARKER_DIR"
AC_FILE="$PROJECT_DIR/Memory/activeContext.md"
AC_SHA=""
[[ -f "$AC_FILE" ]] && AC_SHA=$(sha256sum "$AC_FILE" 2>/dev/null | cut -d' ' -f1)
printf '{"created":"%s","session_id":"%s","ac_sha256":"%s"}\n' \
    "$(date -u +%FT%TZ)" "${ASHA_SESSION_ID:-unknown}" "$AC_SHA" \
    > "$MARKER_DIR/save-gates-ok"
log "PASS: all gates passed; commit gate opened (Work/markers/save-gates-ok, bound to activeContext sha256=${AC_SHA:0:12}…)"
exit 0
