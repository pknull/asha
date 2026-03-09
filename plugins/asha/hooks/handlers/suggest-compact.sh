#!/bin/bash
set -euo pipefail
# Suggest-compact hook - monitors session activity and suggests compaction
# Runs on PostToolUse, checks activity level, suggests /compact when threshold exceeded

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

# Skip if silence mode active
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    echo "{}"
    exit 0
fi

# Read stdin (required for hooks)
INPUT=$(cat)

# Paths
EVENTS_FILE="$PROJECT_DIR/Memory/events/events.jsonl"
MARKER_DIR="$PROJECT_DIR/Work/markers"
COMPACT_SUGGESTED="$MARKER_DIR/compact-suggested"
TOOL_COUNT_FILE="$MARKER_DIR/tool-count"

mkdir -p "$MARKER_DIR"

# Thresholds
TOOL_THRESHOLD=100        # Suggest after 100 tool calls in session
EVENT_THRESHOLD=200       # Suggest if events.jsonl exceeds 200 lines
COOLDOWN_HOURS=2          # Don't suggest again within 2 hours

# Check if we already suggested recently
if [[ -f "$COMPACT_SUGGESTED" ]]; then
    LAST_SUGGESTED=$(cat "$COMPACT_SUGGESTED")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_SUGGESTED))
    COOLDOWN_SECONDS=$((COOLDOWN_HOURS * 3600))

    if [[ $DIFF -lt $COOLDOWN_SECONDS ]]; then
        echo "{}"
        exit 0
    fi
fi

# Count tool calls this session
TOOL_COUNT=0
if [[ -f "$TOOL_COUNT_FILE" ]]; then
    TOOL_COUNT=$(cat "$TOOL_COUNT_FILE")
fi
TOOL_COUNT=$((TOOL_COUNT + 1))
echo "$TOOL_COUNT" > "$TOOL_COUNT_FILE"

# Check thresholds
SHOULD_SUGGEST=false
REASON=""

# Check tool call count
if [[ $TOOL_COUNT -ge $TOOL_THRESHOLD ]]; then
    SHOULD_SUGGEST=true
    REASON="$TOOL_COUNT tool calls this session"
fi

# Check events file size
if [[ -f "$EVENTS_FILE" ]]; then
    EVENT_COUNT=$(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0)
    if [[ $EVENT_COUNT -ge $EVENT_THRESHOLD ]]; then
        SHOULD_SUGGEST=true
        REASON="$EVENT_COUNT events in log"
    fi
fi

# Suggest compaction if threshold exceeded
if [[ "$SHOULD_SUGGEST" == "true" ]]; then
    # Mark as suggested
    date +%s > "$COMPACT_SUGGESTED"

    # Reset tool count
    echo "0" > "$TOOL_COUNT_FILE"

    # Output suggestion as system-reminder
    cat <<EOF
<system-reminder>
Context check: This session has significant activity ($REASON).

If you notice degraded performance or the conversation getting long, consider:
- Using /save to checkpoint progress
- Starting a fresh session for new tasks
- Delegating exploration to subagents (Task tool) to preserve main context

This is informational only - continue if the current task is progressing well.
</system-reminder>
EOF
fi

# Pass through original input
echo "$INPUT"
