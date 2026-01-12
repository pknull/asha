---
description: "Show current session status and captured activity"
argument-hint: ""
allowed-tools: ["Bash", "Read"]
---

# Session Status

Display current session information and captured activity.

## Protocol

### Step 1: Check for Session Watching File

```bash
WATCHING_FILE="${CLAUDE_PROJECT_DIR}/Memory/sessions/current-session.md"

if [[ ! -f "$WATCHING_FILE" ]]; then
    echo "No active session found."
    echo ""
    echo "Run /asha:init to initialize Asha in this project."
    exit 0
fi
```

### Step 2: Extract Session Metadata

Read the frontmatter to get session start time and ID:

```bash
SESSION_START=$(grep '^sessionStart:' "$WATCHING_FILE" | cut -d' ' -f2-)
SESSION_ID=$(grep '^sessionID:' "$WATCHING_FILE" | cut -d' ' -f2)
FILE_SIZE=$(du -h "$WATCHING_FILE" | cut -f1)
LAST_MODIFIED=$(stat -c '%y' "$WATCHING_FILE" 2>/dev/null || stat -f '%Sm' "$WATCHING_FILE" 2>/dev/null)
```

### Step 3: Count Captured Activity

Count entries in each section (lines that aren't comments/headers/blank):

```bash
# Extract and count each section
count_section() {
    local section="$1"
    sed -n "/^## $section/,/^## /p" "$WATCHING_FILE" | \
        grep -v '^##' | grep -v '^<!--' | grep -v '^---' | grep -v '^$' | wc -l
}

OPS_COUNT=$(count_section "Significant Operations")
DECISIONS_COUNT=$(count_section "Decisions & Clarifications")
ERRORS_COUNT=$(count_section "Errors & Anomalies")
```

### Step 4: Calculate Duration

```bash
# Parse session start and calculate duration
if [[ -n "$SESSION_START" ]]; then
    START_EPOCH=$(date -d "$SESSION_START" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M UTC" "$SESSION_START" +%s 2>/dev/null)
    NOW_EPOCH=$(date +%s)
    DURATION_SECS=$((NOW_EPOCH - START_EPOCH))
    DURATION_MINS=$((DURATION_SECS / 60))
    DURATION_HRS=$((DURATION_MINS / 60))
    REMAINING_MINS=$((DURATION_MINS % 60))

    if [[ $DURATION_HRS -gt 0 ]]; then
        DURATION="${DURATION_HRS}h ${REMAINING_MINS}m"
    else
        DURATION="${DURATION_MINS}m"
    fi
fi
```

### Step 5: Display Status Report

Output the status in a clean format:

```
## Current Session Status

**Session ID**: $SESSION_ID
**Started**: $SESSION_START
**Duration**: $DURATION
**File size**: $FILE_SIZE

### Captured Activity

| Section | Count |
|---------|-------|
| Significant Operations | $OPS_COUNT |
| Decisions & Clarifications | $DECISIONS_COUNT |
| Errors & Anomalies | $ERRORS_COUNT |

**Last activity**: $LAST_MODIFIED
```

### Step 6: Show Recent Entries (Optional)

If there's captured activity, show the last few entries from Significant Operations:

```bash
if [[ $OPS_COUNT -gt 0 ]]; then
    echo ""
    echo "### Recent Operations (last 5)"
    sed -n '/^## Significant Operations/,/^## /p' "$WATCHING_FILE" | \
        grep -v '^##' | grep -v '^<!--' | grep -v '^$' | tail -5
fi
```

## Tips

- Run before `/asha:save` to preview what will be synthesized
- If counts are 0, hooks may not be capturing (check `.claude/hooks/`)
- Use `/asha:save` when ready to archive and update Memory Bank
