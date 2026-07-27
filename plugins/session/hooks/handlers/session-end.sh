#!/bin/bash
set -euo pipefail
# SessionEnd Hook - Archives session file on clean exit
# Delegates to tools/save-session.sh for consistent archiving logic

# Source common utilities
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

PROJECT_DIR=$(detect_project_dir)
if [[ -z "$PROJECT_DIR" ]]; then
    echo "{}"
    exit 0
fi

PLUGIN_ROOT=$(get_plugin_root)
if [[ -z "$PLUGIN_ROOT" ]]; then
    echo "{}"
    exit 0
fi

# Only run if Asha is initialized
if ! is_asha_initialized; then
    echo "{}"
    exit 0
fi

# Read stdin JSON from Claude Code (required for hooks)
INPUT=$(cat)

# Thread the authoritative session identity from the payload into the automatic
# save so synthesis reads THIS session's transcript, not a concurrent session's
# newest-by-mtime log. jsonl_reader/pattern_analyzer honor these env vars, and
# exec (below) preserves the exported environment.
#
# Copilot (verified live 2026-07-27, 1.0.75): payload is camelCase
# {sessionId, timestamp, cwd, reason} with NO transcript_path — jsonl_reader
# resolves ~/.copilot/session-state/<sid>/events.jsonl from ASHA_SESSION_ID.
# COPILOT_CLI=1 is stamped by copilot on its own hook processes and is
# authoritative there; an inherited ASHA_HARNESS (e.g. a copilot session
# launched from inside a Claude session) must not override it.
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || true)
[[ -n "$TRANSCRIPT_PATH" ]] && export ASHA_TRANSCRIPT_PATH="$TRANSCRIPT_PATH"
if [[ "${COPILOT_CLI:-}" == "1" ]]; then
    export ASHA_HARNESS="copilot"
    [[ -n "$SESSION_ID" ]] && export ASHA_SESSION_ID="$SESSION_ID"
else
    export ASHA_HARNESS="${ASHA_HARNESS:-claude}"
    [[ -n "$SESSION_ID" ]] && export ASHA_SESSION_ID="$SESSION_ID" && export CLAUDE_CODE_SESSION_ID="$SESSION_ID"
fi

# Silence is durable user policy, not ephemeral session state. Inspect it before
# cleanup and leave it in place until `/session:silence off` removes it.
SILENCED=0
[[ -f "$PROJECT_DIR/Work/markers/silence" ]] && SILENCED=1

# Clear this session's ephemeral policy state (session_state — not durable
# Memory); sweep state files leaked by prior unclean exits.
if [[ -f "$SCRIPT_DIR/state.sh" ]]; then
  source "$SCRIPT_DIR/state.sh" 2>/dev/null && {
    [[ -n "$SESSION_ID" ]] && state_clear "$SESSION_ID" 2>/dev/null || true
    state_sweep 2>/dev/null || true
  }
fi

# Clean up session markers (auto-removed at session-end)
rm -f "$PROJECT_DIR/Work/markers/rp-active"
rm -f "$PROJECT_DIR/Work/markers/save-pending"

# Extract session end reason
REASON=$(echo "$INPUT" | jq -r '.reason // empty' 2>/dev/null || true)

# Clean-exit reasons are harness-specific:
#   claude : logout | prompt_input_exit | idle  (skip: clear, crashes)
#   copilot: complete (non-interactive -p run) | user_exit (interactive /exit)
#            — both observed live 2026-07-27 on 1.0.75. Unknown reasons skip
#            the save; orphan recovery at the next session start covers them.
CLEAN_EXIT=0
if [[ "$ASHA_HARNESS" == "copilot" ]]; then
    [[ "$REASON" == "complete" || "$REASON" == "user_exit" ]] && CLEAN_EXIT=1
else
    [[ "$REASON" == "logout" || "$REASON" == "prompt_input_exit" || "$REASON" == "idle" ]] && CLEAN_EXIT=1
fi

# Only archive on clean exit (not on /clear which continues session)
if [[ "$SILENCED" == "1" ]]; then
    # A clean exit from a silenced session must not launch synthesis.
    echo "{}"

elif [[ "$CLEAN_EXIT" == "1" ]]; then
    # Use save script in automatic mode.
    #
    # DETACHED, not inline: the old `exec save-session.sh --automatic` ran the
    # full synthesis synchronously in the hook. On exit the harness cancels an
    # in-flight SessionEnd hook to quit promptly, so the synthesis was killed
    # mid-pipeline — after it rewrote activeContext.md, before it committed —
    # surfacing as "Hook cancelled" and a dirty working tree. setsid runs it in
    # a new session/process group that outlives teardown; detached-save.sh adds
    # an flock (against a relaunched session's save) and a disk log. The
    # exported env (ASHA_TRANSCRIPT_PATH/CLAUDE_CODE_SESSION_ID) is inherited.
    SAVE_SCRIPT="$PLUGIN_ROOT/tools/save-session.sh"
    DETACHED="$PLUGIN_ROOT/tools/detached-save.sh"
    LOG_FILE="$PROJECT_DIR/Work/logs/session-end-save.log"
    LOCK_FILE="$PROJECT_DIR/Work/markers/.save.lock"
    if [[ -x "$SAVE_SCRIPT" && -x "$DETACHED" ]] && command -v setsid >/dev/null 2>&1; then
        setsid "$DETACHED" "$SAVE_SCRIPT" "$LOG_FILE" "$LOCK_FILE" </dev/null >/dev/null 2>&1 &
        echo "{}"
    elif [[ -x "$SAVE_SCRIPT" ]]; then
        # Fallback (no setsid / wrapper missing): preserve old inline behavior
        # rather than skip the save entirely.
        exec "$SAVE_SCRIPT" --automatic
    else
        echo "{}"
    fi

elif [[ "$REASON" == "clear" ]]; then
    # /clear was called - session continues, don't archive
    echo "{}"

else
    # Other reasons (crashes, unexpected termination)
    # Don't archive automatically - preserve for recovery
    echo "{}"
fi
