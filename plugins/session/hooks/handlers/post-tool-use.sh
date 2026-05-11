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
#   - Track agent deployments in ReasoningBank (Task tool only)
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

# ReasoningBank tracking for Task agent deployments (intervention — feeds
# tool-selection learning, not session capture).
case "$TOOL_NAME" in
    "Task")
        AGENT_TYPE=$(echo "$TOOL_INPUT" | jq -r '.subagent_type // empty' 2>/dev/null)
        DESCRIPTION=$(echo "$TOOL_INPUT" | jq -r '.description // empty' 2>/dev/null)
        if [[ -n "$AGENT_TYPE" && "$AGENT_TYPE" != "null" ]]; then
            REASONING_BANK="$PLUGIN_ROOT/tools/reasoning_bank.py"
            PYTHON_CMD=$(get_python_cmd)
            if [[ -f "$REASONING_BANK" && -n "$PYTHON_CMD" ]]; then
                ("$PYTHON_CMD" "$REASONING_BANK" tool \
                    --name "$AGENT_TYPE" \
                    --use-case "${DESCRIPTION:-unspecified}" \
                    --success >/dev/null 2>&1) &
            fi
        fi
        ;;
esac

# Vector DB refresh for indexed file changes (background, non-blocking)
# Only trigger for files in Memory/, asha/, or .claude/ directories
case "$TOOL_NAME" in
    "Edit"|"Write"|"NotebookEdit")
        FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // .notebook_path // empty' 2>/dev/null)
        if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
            # Check if file is in an indexed directory
            if [[ "$FILE_PATH" =~ Memory/.*\.md$ ]] || \
               [[ "$FILE_PATH" =~ \.claude/.*\.md$ ]]; then

                MEMORY_INDEX="$PLUGIN_ROOT/tools/memory_index.py"
                PYTHON_CMD=$(get_python_cmd)
                if [[ -f "$MEMORY_INDEX" && -n "$PYTHON_CMD" ]]; then
                    # Run incremental ingest in background (non-blocking)
                    # Skip if already running to prevent process accumulation
                    if ! pgrep -f "memory_index.py ingest" >/dev/null 2>&1; then
                        ("$PYTHON_CMD" "$MEMORY_INDEX" ingest --changed >/dev/null 2>&1) &
                    fi
                fi
            fi
        fi
        ;;
esac

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
