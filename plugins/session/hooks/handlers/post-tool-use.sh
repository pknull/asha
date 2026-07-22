#!/bin/bash
set -euo pipefail
# PostToolUse Hook — INTERVENTION ONLY (capture moved to /save jsonl_reader)
#
# What this hook USED to do: emit structured events to
# Memory/events/events.jsonl on every tool call (file_modified, file_created,
# agent_deployed, decision_point, command, error). All of that is now
# regenerated on demand at /save time by jsonl_reader, parsing the host's
# native session transcript directly. The hook's capture path was redundant.
#
# What this hook STILL does:
#   - Trigger vector DB incremental ingest for Memory/.claude .md edits
#   - Run violation checker on Write/Edit/Bash
# These are intervention/side-effect concerns that have no transcript-tail
# equivalent and stay here.

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

# Skip if silence mode active (master override)
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    echo "{}"
    exit 0
fi

# Skip during RP sessions
if [[ -f "$PROJECT_DIR/Work/markers/rp-active" ]]; then
    echo "{}"
    exit 0
fi

# Ensure marker directory exists (Memory/events no longer written here).
mkdir -p "$PROJECT_DIR/Work/markers"

# Read stdin JSON from Claude Code
INPUT=$(cat)

# Extract tool information (used by intervention paths below).
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || true)

# If a Read follows a memory nudge, mark that nudge acted-on. State and metrics
# live in the cache rather than dirtying the project's generated events file.
if [[ "$TOOL_NAME" == "Read" && "${ASHA_NUDGE:-1}" != "0" ]]; then
    READ_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null || true)
    NUDGE_SID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    NUDGE_SID=$(printf '%s' "$NUDGE_SID" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-120)
    NUDGE_BASE="${XDG_RUNTIME_DIR:-/tmp}/asha-memory-nudge-$(id -u)"
    NUDGE_STATE="$NUDGE_BASE/${NUDGE_SID:-unknown}.json"
    # Avoid a Python process on ordinary Reads. Only a path present in this
    # session's fired-nudge state can become an acted-on event.
    if [[ -n "$READ_PATH" && -f "$NUDGE_STATE" ]] \
       && grep -Fq -- "$READ_PATH" "$NUDGE_STATE" 2>/dev/null; then
        MEMORY_NUDGE="$PLUGIN_ROOT/tools/memory_nudge.py"
        PYTHON_CMD=$(get_python_cmd)
        if [[ -f "$MEMORY_NUDGE" && -n "$PYTHON_CMD" ]]; then
            printf '%s' "$INPUT" | "$PYTHON_CMD" "$MEMORY_NUDGE" acted >/dev/null 2>&1 || true
        fi
    fi
fi

# Run violation checker (non-blocking, logs to session file)
# Only runs for Write/Edit/Bash operations that might violate rules
case "$TOOL_NAME" in
    "Write"|"Edit"|"Bash")
        VIOLATION_CHECKER="$SCRIPT_DIR/violation-checker.sh"
        if [[ -x "$VIOLATION_CHECKER" ]]; then
            ("$VIOLATION_CHECKER" "$TOOL_NAME" "$TOOL_INPUT" >/dev/null 2>&1) &
        fi
        ;;
esac

# Return success (no blocking, no output to user)
echo "{}"
