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

# Only run if Asha is initialized
if ! is_asha_initialized; then
    echo "{}"
    exit 0
fi

# Read stdin JSON from Claude Code (required for hooks)
INPUT=$(cat)

# Clean up RP marker if it exists (in case user forgot to explicitly end RP session)
if [[ -f "$PROJECT_DIR/Work/markers/rp-active" ]]; then
    rm "$PROJECT_DIR/Work/markers/rp-active"
fi

# Extract session end reason
REASON=$(echo "$INPUT" | jq -r '.reason // empty')

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
