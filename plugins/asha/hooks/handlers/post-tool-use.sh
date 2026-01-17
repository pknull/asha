#!/bin/bash
# PostToolUse Hook - Captures file modifications and agent deployments
# Automatically appends to current-session.md for session watching

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

# Skip logging if silence mode active (master override)
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    echo "{}"
    exit 0
fi

# Skip logging during RP sessions
if [[ -f "$PROJECT_DIR/Work/markers/rp-active" ]]; then
    echo "{}"
    exit 0
fi

SESSION_FILE="$PROJECT_DIR/Memory/sessions/current-session.md"
TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')

# Ensure Memory directory structure exists
mkdir -p "$PROJECT_DIR/Memory/sessions"
mkdir -p "$PROJECT_DIR/Work/markers"

# Ensure session file exists
if [[ ! -f "$SESSION_FILE" ]]; then
    cat > "$SESSION_FILE" <<EOF
---
sessionStart: $(date -u '+%Y-%m-%d %H:%M UTC')
sessionID: $(head /dev/urandom | tr -dc a-f0-9 | head -c 8)
---

## Significant Operations
<!-- Auto-appended: agent deployments, file writes, panel sessions -->

## Decisions & Clarifications
<!-- Auto-appended: AskUserQuestion responses -->

## Errors & Anomalies
<!-- Auto-appended: tool failures -->
EOF
fi

# Read stdin JSON from Claude Code
INPUT=$(cat)

# Extract tool information (suppress jq errors for malformed input)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null)
TOOL_RESPONSE=$(echo "$INPUT" | jq -c '.tool_response // {}' 2>/dev/null)

# Check for errors in tool response
ERROR_MSG=$(echo "$TOOL_RESPONSE" | jq -r '.error // empty' 2>/dev/null)
if [[ -n "$ERROR_MSG" && "$ERROR_MSG" != "null" ]]; then
    # Truncate long errors to 200 chars for readability
    if [[ ${#ERROR_MSG} -gt 200 ]]; then
        ERROR_SHORT="${ERROR_MSG:0:200}..."
    else
        ERROR_SHORT="$ERROR_MSG"
    fi

    # Extract context from tool input if available
    CONTEXT=""
    case "$TOOL_NAME" in
        "Edit"|"Write")
            FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)
            if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
                REL_PATH=${FILE_PATH#$PROJECT_DIR/}
                CONTEXT="attempting to access $REL_PATH"
            fi
            ;;
        "Task")
            AGENT_TYPE=$(echo "$TOOL_INPUT" | jq -r '.subagent_type // empty' 2>/dev/null)
            if [[ -n "$AGENT_TYPE" && "$AGENT_TYPE" != "null" ]]; then
                CONTEXT="deploying $AGENT_TYPE agent"
            fi
            ;;
        "Bash")
            COMMAND=$(echo "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null | head -c 50)
            if [[ -n "$COMMAND" && "$COMMAND" != "null" ]]; then
                CONTEXT="running: $COMMAND"
            fi
            ;;
    esac

    # Log error to Errors & Anomalies section
    if [[ -n "$CONTEXT" ]]; then
        sed -i "/## Errors & Anomalies/a - [$TIMESTAMP] ERROR: $TOOL_NAME → $ERROR_SHORT | Context: $CONTEXT" "$SESSION_FILE"
    else
        sed -i "/## Errors & Anomalies/a - [$TIMESTAMP] ERROR: $TOOL_NAME → $ERROR_SHORT" "$SESSION_FILE"
    fi
fi

case "$TOOL_NAME" in
    "Edit")
        FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)
        if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
            if [[ "$FILE_PATH" =~ ^$PROJECT_DIR/ ]]; then
                REL_PATH=${FILE_PATH#$PROJECT_DIR/}
            else
                REL_PATH="$FILE_PATH"
            fi
            sed -i "/## Significant Operations/a - [$TIMESTAMP] Modified: $REL_PATH (Edit)" "$SESSION_FILE"
        fi
        ;;

    "Write")
        FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)
        if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
            if [[ "$FILE_PATH" =~ ^$PROJECT_DIR/ ]]; then
                REL_PATH=${FILE_PATH#$PROJECT_DIR/}
            else
                REL_PATH="$FILE_PATH"
            fi
            sed -i "/## Significant Operations/a - [$TIMESTAMP] Created: $REL_PATH (Write)" "$SESSION_FILE"
        fi
        ;;

    "NotebookEdit")
        NOTEBOOK_PATH=$(echo "$TOOL_INPUT" | jq -r '.notebook_path // empty' 2>/dev/null)
        if [[ -n "$NOTEBOOK_PATH" && "$NOTEBOOK_PATH" != "null" ]]; then
            REL_PATH=${NOTEBOOK_PATH#$PROJECT_DIR/}
            sed -i "/## Significant Operations/a - [$TIMESTAMP] Modified: $REL_PATH (Notebook)" "$SESSION_FILE"
        fi
        ;;

    "Task")
        AGENT_TYPE=$(echo "$TOOL_INPUT" | jq -r '.subagent_type // empty' 2>/dev/null)
        DESCRIPTION=$(echo "$TOOL_INPUT" | jq -r '.description // empty' 2>/dev/null)

        if [[ -n "$AGENT_TYPE" && "$AGENT_TYPE" != "null" ]]; then
            sed -i "/## Significant Operations/a - [$TIMESTAMP] Agent: $AGENT_TYPE → $DESCRIPTION" "$SESSION_FILE"

            # Track agent deployment in ReasoningBank (background, non-blocking)
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

    "AskUserQuestion")
        QUESTIONS=$(echo "$TOOL_INPUT" | jq -r '.questions[]? | .header // empty' 2>/dev/null | paste -sd ',' -)
        if [[ -n "$QUESTIONS" && "$QUESTIONS" != "null" ]]; then
            sed -i "/## Decisions & Clarifications/a - [$TIMESTAMP] Decision Point: $QUESTIONS" "$SESSION_FILE"
        fi
        ;;

    "SlashCommand")
        COMMAND=$(echo "$TOOL_INPUT" | jq -r '.command // empty' 2>/dev/null)
        if [[ "$COMMAND" == "/panel"* || "$COMMAND" == "/save"* || "$COMMAND" == "/asha"* ]]; then
            sed -i "/## Significant Operations/a - [$TIMESTAMP] Command: $COMMAND" "$SESSION_FILE"
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
                    ("$PYTHON_CMD" "$MEMORY_INDEX" ingest --changed >/dev/null 2>&1) &
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
