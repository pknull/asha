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
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
[[ -n "$TRANSCRIPT_PATH" ]] && export ASHA_TRANSCRIPT_PATH="$TRANSCRIPT_PATH"
[[ -n "$SESSION_ID" ]] && export CLAUDE_CODE_SESSION_ID="$SESSION_ID"

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
rm -f "$PROJECT_DIR/Work/markers/silence"
rm -f "$PROJECT_DIR/Work/markers/save-pending"

# Extract session end reason
REASON=$(echo "$INPUT" | jq -r '.reason // empty' 2>/dev/null || true)

# Only archive on clean logout/exit/idle (not on /clear which continues session)
if [[ "$REASON" == "logout" || "$REASON" == "prompt_input_exit" || "$REASON" == "idle" ]]; then
    # Use save script in automatic mode
    SAVE_SCRIPT="$PLUGIN_ROOT/tools/save-session.sh"
    if [[ -x "$SAVE_SCRIPT" ]]; then
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
