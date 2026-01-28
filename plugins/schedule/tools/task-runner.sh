#!/usr/bin/env bash
set -euo pipefail

# Task runner wrapper for scheduled Claude executions
# Handles logging, timeout, status tracking, and audit trail
#
# Usage: task-runner.sh <task-id> <project-dir> <command> [allowed-tools] [timeout]

TASK_ID="${1:?Task ID required}"
PROJECT_DIR="${2:?Project directory required}"
COMMAND="${3:?Command required}"
ALLOWED_TOOLS="${4:-Read,Grep,Glob}"
TIMEOUT="${5:-300}"

# Setup paths
LOG_DIR="$PROJECT_DIR/.claude/logs"
LOG_FILE="$LOG_DIR/$TASK_ID.log"
STATUS_FILE="$LOG_DIR/$TASK_ID.status"
AUDIT_FILE="$LOG_DIR/scheduler-audit.log"

# Ensure directories exist
mkdir -p "$LOG_DIR"

# Log header
{
    echo "========================================"
    echo "Task: $TASK_ID"
    echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "Command: $COMMAND"
    echo "Allowed Tools: $ALLOWED_TOOLS"
    echo "Timeout: ${TIMEOUT}s"
    echo "Working Directory: $PROJECT_DIR"
    echo "========================================"
    echo ""
} >> "$LOG_FILE"

# Record start in audit log
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') RUN task=$TASK_ID status=started" >> "$AUDIT_FILE"

# Execute with timeout
START_TIME=$(date +%s)
cd "$PROJECT_DIR"

set +e
timeout "$TIMEOUT" claude -p "$COMMAND" --allowedTools "$ALLOWED_TOOLS" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# Determine status
if [ $EXIT_CODE -eq 0 ]; then
    STATUS="success"
elif [ $EXIT_CODE -eq 124 ]; then
    STATUS="timeout"
else
    STATUS="failure"
fi

# Log footer
{
    echo ""
    echo "========================================"
    echo "Finished: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "Duration: ${DURATION}s"
    echo "Status: $STATUS (exit code: $EXIT_CODE)"
    echo "========================================"
    echo ""
} >> "$LOG_FILE"

# Write status file (JSON)
cat > "$STATUS_FILE" << EOF
{
    "taskId": "$TASK_ID",
    "lastRun": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
    "status": "$STATUS",
    "exitCode": $EXIT_CODE,
    "duration": $DURATION
}
EOF

# Record completion in audit log
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') RUN task=$TASK_ID status=$STATUS duration=${DURATION}s exit=$EXIT_CODE" >> "$AUDIT_FILE"

# Update schedules.json lastRun and lastStatus if jq is available
if command -v jq &>/dev/null; then
    SCHEDULES_FILE="$PROJECT_DIR/.claude/schedules.json"
    if [ -f "$SCHEDULES_FILE" ]; then
        TEMP_FILE=$(mktemp)
        if jq --arg id "$TASK_ID" \
              --arg status "$STATUS" \
              --arg lastRun "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
              '(.tasks[] | select(.id == $id)) |= . + {lastRun: $lastRun, lastStatus: $status}' \
              "$SCHEDULES_FILE" > "$TEMP_FILE" 2>/dev/null; then
            mv "$TEMP_FILE" "$SCHEDULES_FILE"
        else
            rm -f "$TEMP_FILE"
        fi
    fi
fi

exit $EXIT_CODE
