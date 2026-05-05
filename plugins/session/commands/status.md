---
name: session-status
description: "Show current session status and captured activity"
argument-hint: ""
allowed-tools: ["Bash", "Read"]
---

# Session Status

Display current session information and captured activity from the event store.

## Protocol

### Step 1: Check for Asha Initialization

```bash
if [[ ! -f "${CLAUDE_PROJECT_DIR}/.asha/config.json" ]]; then
    echo "Asha not initialized in this project."
    echo ""
    echo "Run /asha:init to initialize."
    exit 0
fi
```

### Step 2: Get Session ID and Metadata

```bash
SESSION_MARKER="${CLAUDE_PROJECT_DIR}/Work/markers/session-id"
EVENTS_FILE="${CLAUDE_PROJECT_DIR}/Memory/events/events.jsonl"

if [[ -f "$SESSION_MARKER" ]]; then
    SESSION_ID=$(cat "$SESSION_MARKER")
else
    SESSION_ID="unknown"
fi
```

### Step 3: Query Event Store for Session Activity

```bash
PLUGIN_ROOT="/home/pknull/life/asha/plugins/session"
PYTHON_CMD="python3"

if [[ -x "${CLAUDE_PROJECT_DIR}/.asha/.venv/bin/python3" ]]; then
    PYTHON_CMD="${CLAUDE_PROJECT_DIR}/.asha/.venv/bin/python3"
fi

EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"

# Get session stats
"$PYTHON_CMD" "$EVENT_STORE" stats 2>/dev/null
```

### Step 4: Count Events by Type

```bash
if [[ -f "$EVENTS_FILE" ]]; then
    TOTAL_EVENTS=$(wc -l < "$EVENTS_FILE")
    FILE_EVENTS=$(grep -c '"file_modified"\|"file_created"' "$EVENTS_FILE" 2>/dev/null || echo 0)
    AGENT_EVENTS=$(grep -c '"agent_deployed"' "$EVENTS_FILE" 2>/dev/null || echo 0)
    ERROR_EVENTS=$(grep -c '"error"' "$EVENTS_FILE" 2>/dev/null || echo 0)
    DECISION_EVENTS=$(grep -c '"decision"' "$EVENTS_FILE" 2>/dev/null || echo 0)

    EVENTS_SIZE=$(du -h "$EVENTS_FILE" | cut -f1)
else
    TOTAL_EVENTS=0
    EVENTS_SIZE="0"
fi
```

### Step 5: Check Markers

```bash
MARKER_DIR="${CLAUDE_PROJECT_DIR}/Work/markers"

SILENCE_STATUS="off"
[[ -f "$MARKER_DIR/silence" ]] && SILENCE_STATUS="ON"

RP_STATUS="off"
[[ -f "$MARKER_DIR/rp-active" ]] && RP_STATUS="ON"

TOOL_COUNT=0
[[ -f "$MARKER_DIR/tool-count" ]] && TOOL_COUNT=$(cat "$MARKER_DIR/tool-count")
```

### Step 6: Display Status Report

Output the status:

```
## Current Session Status

**Session ID**: $SESSION_ID
**Events file**: $EVENTS_SIZE ($TOTAL_EVENTS events)

### Captured Activity

| Type | Count |
|------|-------|
| File changes | $FILE_EVENTS |
| Agent deployments | $AGENT_EVENTS |
| Errors | $ERROR_EVENTS |
| Decisions | $DECISION_EVENTS |

### Markers

| Marker | Status |
|--------|--------|
| Silence mode | $SILENCE_STATUS |
| RP mode | $RP_STATUS |
| Tool calls this session | $TOOL_COUNT |
```

## Tips

- Run before `/asha:save` to preview what will be synthesized
- If event counts are 0, hooks may not be capturing (check plugin installation)
- Use `/asha:save` when ready to synthesize and archive
