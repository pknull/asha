#!/bin/bash
set -euo pipefail
# PostToolUse Hook - Captures file modifications and agent deployments
# Emits structured events to Memory/events/events.jsonl

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

# Ensure directory structure exists
mkdir -p "$PROJECT_DIR/Memory/events"
mkdir -p "$PROJECT_DIR/Work/markers"

# Helper function to emit events to event_store.py
emit_event() {
    local event_type="$1"
    local subtype="$2"
    local payload="$3"
    local tool_name="${4:-}"

    EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"
    PYTHON_CMD=$(get_python_cmd)

    if [[ -f "$EVENT_STORE" && -n "$PYTHON_CMD" ]]; then
        # Run in background to avoid blocking
        if [[ -n "$tool_name" ]]; then
            ("$PYTHON_CMD" "$EVENT_STORE" emit \
                --type "$event_type" \
                --subtype "$subtype" \
                --payload "$payload" \
                --source "hook" \
                --tool "$tool_name" >/dev/null 2>&1) &
        else
            ("$PYTHON_CMD" "$EVENT_STORE" emit \
                --type "$event_type" \
                --subtype "$subtype" \
                --payload "$payload" \
                --source "hook" >/dev/null 2>&1) &
        fi
    fi
}

# Read stdin JSON from Claude Code
INPUT=$(cat)

# Extract tool information (suppress jq errors for malformed input)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || true)
TOOL_RESPONSE=$(echo "$INPUT" | jq -c '.tool_response // {}' 2>/dev/null || true)

# Check for errors in tool response
ERROR_MSG=$(echo "$TOOL_RESPONSE" | jq -r '.error // empty' 2>/dev/null || true)
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
                REL_PATH=${FILE_PATH#"$PROJECT_DIR"/}
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

    # Emit error event
    PAYLOAD=$(jq -nc --arg error "$ERROR_SHORT" --arg context "$CONTEXT" \
        '{error: $error, context: $context}')
    emit_event "event" "error" "$PAYLOAD" "$TOOL_NAME"
fi

case "$TOOL_NAME" in
    "Edit")
        FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)
        if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
            if [[ "$FILE_PATH" =~ ^$PROJECT_DIR/ ]]; then
                REL_PATH=${FILE_PATH#"$PROJECT_DIR"/}
            else
                REL_PATH="$FILE_PATH"
            fi
            PAYLOAD=$(jq -nc --arg file_path "$REL_PATH" --arg detail "Modified: $REL_PATH" \
                '{file_path: $file_path, detail: $detail}')
            emit_event "event" "file_modified" "$PAYLOAD" "$TOOL_NAME"
        fi
        ;;

    "Write")
        FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null)
        if [[ -n "$FILE_PATH" && "$FILE_PATH" != "null" ]]; then
            if [[ "$FILE_PATH" =~ ^$PROJECT_DIR/ ]]; then
                REL_PATH=${FILE_PATH#"$PROJECT_DIR"/}
            else
                REL_PATH="$FILE_PATH"
            fi
            PAYLOAD=$(jq -nc --arg file_path "$REL_PATH" --arg detail "Created: $REL_PATH" \
                '{file_path: $file_path, detail: $detail}')
            emit_event "event" "file_created" "$PAYLOAD" "$TOOL_NAME"
        fi
        ;;

    "NotebookEdit")
        NOTEBOOK_PATH=$(echo "$TOOL_INPUT" | jq -r '.notebook_path // empty' 2>/dev/null)
        if [[ -n "$NOTEBOOK_PATH" && "$NOTEBOOK_PATH" != "null" ]]; then
            REL_PATH=${NOTEBOOK_PATH#"$PROJECT_DIR"/}
            PAYLOAD=$(jq -nc --arg file_path "$REL_PATH" --arg detail "Modified notebook: $REL_PATH" \
                '{file_path: $file_path, detail: $detail}')
            emit_event "event" "file_modified" "$PAYLOAD" "$TOOL_NAME"
        fi
        ;;

    "Task")
        AGENT_TYPE=$(echo "$TOOL_INPUT" | jq -r '.subagent_type // empty' 2>/dev/null)
        DESCRIPTION=$(echo "$TOOL_INPUT" | jq -r '.description // empty' 2>/dev/null)

        if [[ -n "$AGENT_TYPE" && "$AGENT_TYPE" != "null" ]]; then
            PAYLOAD=$(jq -nc --arg agent_type "$AGENT_TYPE" --arg description "$DESCRIPTION" \
                '{agent_type: $agent_type, description: $description, detail: "Agent: \($agent_type) → \($description)"}')
            emit_event "event" "agent_deployed" "$PAYLOAD" "$TOOL_NAME"

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
            PAYLOAD=$(jq -nc --arg questions "$QUESTIONS" \
                '{questions: $questions, detail: "Decision Point: \($questions)"}')
            emit_event "event" "decision_point" "$PAYLOAD" "$TOOL_NAME"
        fi
        ;;

    "Skill")
        COMMAND=$(echo "$TOOL_INPUT" | jq -r '.skill // empty' 2>/dev/null)
        if [[ "$COMMAND" == "panel"* || "$COMMAND" == "save"* || "$COMMAND" == *":"* ]]; then
            PAYLOAD=$(jq -nc --arg command "$COMMAND" \
                '{command: $command, detail: "Skill: \($command)"}')
            emit_event "event" "command" "$PAYLOAD" "$TOOL_NAME"
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
