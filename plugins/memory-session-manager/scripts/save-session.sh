#!/bin/bash
# save-session.sh - Unified session save logic for REDACTED99 Memory Bank
# Can be called manually, via /save command, or automatically via session-end hook

set -euo pipefail

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Multi-layered project directory detection
# Priority: CLAUDE_PROJECT_DIR → git root → upward search → fail with error
detect_project_dir() {
    # Layer 1: Use CLAUDE_PROJECT_DIR if set (hook invocation)
    if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        echo "$CLAUDE_PROJECT_DIR"
        return 0
    fi

    # Layer 2: Try git root (manual invocation within git repo)
    if command -v git >/dev/null 2>&1; then
        local git_root
        git_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
        if [[ -n "$git_root" ]] && [[ -d "$git_root/Memory" ]]; then
            echo "$git_root"
            return 0
        fi
    fi

    # Layer 3: Search upward for Memory/ directory
    local search_dir="$(pwd)"
    while [[ "$search_dir" != "/" ]]; do
        if [[ -d "$search_dir/Memory" ]]; then
            echo "$search_dir"
            return 0
        fi
        search_dir="$(dirname "$search_dir")"
    done

    # Layer 4: All detection methods failed
    echo "[ERROR] Cannot detect project directory. Tried:" >&2
    echo "  1. CLAUDE_PROJECT_DIR environment variable (not set)" >&2
    echo "  2. Git root with Memory/ directory (not found)" >&2
    echo "  3. Upward search for Memory/ directory (not found)" >&2
    echo "" >&2
    echo "Solutions:" >&2
    echo "  - Run from within project directory" >&2
    echo "  - Set CLAUDE_PROJECT_DIR: export CLAUDE_PROJECT_DIR=/path/to/project" >&2
    echo "  - Or: CLAUDE_PROJECT_DIR=/path/to/project $0" >&2
    return 1
}

PROJECT_DIR=$(detect_project_dir) || exit 1

MEMORY_DIR="$PROJECT_DIR/Memory"
WORK_DIR="$PROJECT_DIR/Memory"
WATCHING_FILE="$WORK_DIR/sessions/current-session.md"
ARCHIVE_DIR="$WORK_DIR/sessions/archive"
ACTIVE_CONTEXT="$MEMORY_DIR/activeContext.md"

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
TIMESTAMP_FILE=$(date -u '+%Y-%m-%d_%H-%M')

# ==============================================================================
# MODE DETECTION
# ==============================================================================

MODE="${1:---interactive}"  # --interactive (default) or --automatic

# Interactive mode: Full Four Questions Protocol + user input
# Automatic mode: Lightweight archiving + optional Memory append

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

log() {
    echo "[save-session] $*" >&2
}

error() {
    echo "[save-session ERROR] $*" >&2
    exit 1
}

# Archive session watching file
archive_watching_file() {
    if [[ ! -f "$WATCHING_FILE" ]]; then
        log "No watching file to archive"
        return 0
    fi

    # Count non-trivial lines (exclude comments, headers, blank lines)
    CONTENT_LINES=$(grep -v '^<!--' "$WATCHING_FILE" | grep -v '^#' | grep -v '^---' | grep -v '^$' | wc -l)

    if [[ $CONTENT_LINES -lt 10 ]]; then
        log "Watching file has <10 content lines, skipping archive"
        return 0
    fi

    mkdir -p "$ARCHIVE_DIR"
    ARCHIVE_PATH="$ARCHIVE_DIR/session-$TIMESTAMP_FILE.md"
    cp "$WATCHING_FILE" "$ARCHIVE_PATH"
    log "Archived watching file: $ARCHIVE_PATH ($CONTENT_LINES events)"

    # Increment session count in systemMonitoring.md
    SYSTEM_MONITORING="$MEMORY_DIR/systemMonitoring.md"
    if [[ -f "$SYSTEM_MONITORING" ]]; then
        CURRENT_COUNT=$(grep "^\*\*Sessions Monitored\*\*:" "$SYSTEM_MONITORING" | grep -o '[0-9]\+' || echo "0")
        NEW_COUNT=$((CURRENT_COUNT + 1))
        sed -i "s/^\*\*Sessions Monitored\*\*: [0-9]\+/**Sessions Monitored**: $NEW_COUNT/" "$SYSTEM_MONITORING"

        # Update Last Updated timestamp
        sed -i "s/^\*\*Last Updated\*\*: .*/**Last Updated**: $(date -u '+%Y-%m-%d')/" "$SYSTEM_MONITORING"

        log "Updated systemMonitoring.md: Sessions Monitored = $NEW_COUNT"
    fi

    echo "$ARCHIVE_PATH"  # Return path for caller
}

# Reset watching file to template
reset_watching_file() {
    # Generate dictionary-based session ID (2 random words from /usr/share/dict/words)
    if [[ -f /usr/share/dict/words ]]; then
        WORD1=$(grep -E '^[a-z]{4,8}$' /usr/share/dict/words | shuf -n 1)
        WORD2=$(grep -E '^[a-z]{4,8}$' /usr/share/dict/words | shuf -n 1)
        SESSION_ID="${WORD1}-${WORD2}"
    else
        # Fallback to hex if dictionary not available
        SESSION_ID=$(head /dev/urandom | tr -dc a-f0-9 | head -c 8)
    fi
    cat > "$WATCHING_FILE" <<EOF
---
sessionStart: $TIMESTAMP
sessionID: $SESSION_ID
---

## Significant Operations
<!-- Auto-appended: agent deployments, file writes, panel sessions -->

## Decisions & Clarifications
<!-- Auto-appended: AskUserQuestion responses -->

## Errors & Anomalies
<!-- Auto-appended: tool failures -->
EOF
    log "Reset watching file with new session ID: $SESSION_ID"
}

# Extract section from watching file
extract_section() {
    local section_name="$1"
    local file="$2"

    # Use sed to extract content between section header and next section
    # Then filter out comments and blank lines
    sed -n "/^## $section_name/,/^## /p" "$file" | \
        grep -v "^##" | \
        grep -v '^<!--' | \
        grep -v '^$' || true
}

# Check if Memory cleanup needed
check_memory_cleanup_needed() {
    if [[ ! -f "$ACTIVE_CONTEXT" ]]; then
        echo "false"
        return
    fi

    LINE_COUNT=$(wc -l < "$ACTIVE_CONTEXT")
    if [[ $LINE_COUNT -gt 500 ]]; then
        echo "true"
    else
        echo "false"
    fi
}

# Synthesize errors from watching file to systemMonitoring.md
synthesize_errors_to_monitoring() {
    if [[ ! -f "$WATCHING_FILE" ]]; then
        log "No watching file to extract errors from"
        return 0
    fi

    SYSTEM_MONITORING="$MEMORY_DIR/systemMonitoring.md"

    # Extract errors section
    ERRORS=$(extract_section "Errors & Anomalies" "$WATCHING_FILE")

    if [[ -z "$ERRORS" ]]; then
        log "No errors/anomalies found in session watching file"
        return 0
    fi

    # Parse errors into categories
    TOOL_FAILURES=$(echo "$ERRORS" | grep "ERROR:" | grep -v "MODE-CONFUSION" | grep -v "AUTHORITY-OVERRIDE" || true)
    MODE_CONFUSION=$(echo "$ERRORS" | grep "MODE-CONFUSION:" || true)
    AUTHORITY_OVERRIDES=$(echo "$ERRORS" | grep "AUTHORITY-OVERRIDE:" || true)

    # Update systemMonitoring.md (append to appropriate sections)
    if [[ -n "$TOOL_FAILURES" ]]; then
        FAILURE_COUNT=$(echo "$TOOL_FAILURES" | wc -l)
        log "Found $FAILURE_COUNT tool failure(s) to log"

        # Append each failure after "### Tool Failures" header
        while IFS= read -r failure_line; do
            sed -i "/### Tool Failures/a\\
$failure_line" "$SYSTEM_MONITORING"
        done <<< "$TOOL_FAILURES"
    fi

    if [[ -n "$MODE_CONFUSION" ]]; then
        CONFUSION_COUNT=$(echo "$MODE_CONFUSION" | wc -l)
        log "Found $CONFUSION_COUNT mode confusion incident(s) to log"

        # Append each incident after "### Mode Confusion Incidents" header
        while IFS= read -r confusion_line; do
            sed -i "/## Mode Confusion Incidents/a\\
$confusion_line" "$SYSTEM_MONITORING"
        done <<< "$MODE_CONFUSION"
    fi

    if [[ -n "$AUTHORITY_OVERRIDES" ]]; then
        OVERRIDE_COUNT=$(echo "$AUTHORITY_OVERRIDES" | wc -l)
        log "Found $OVERRIDE_COUNT authority override(s) to log"

        # Append each override after "### Authority Override Events" header
        while IFS= read -r override_line; do
            sed -i "/## Authority Override Events/a\\
$override_line" "$SYSTEM_MONITORING"
        done <<< "$AUTHORITY_OVERRIDES"
    fi

    # Report synthesis results
    if [[ -n "$TOOL_FAILURES" || -n "$MODE_CONFUSION" || -n "$AUTHORITY_OVERRIDES" ]]; then
        echo ""
        echo "✓ ERRORS/ANOMALIES SYNTHESIZED TO systemMonitoring.md"
        echo ""

        if [[ -n "$TOOL_FAILURES" ]]; then
            echo "  - Tool Failures: $FAILURE_COUNT entries added"
        fi

        if [[ -n "$MODE_CONFUSION" ]]; then
            echo "  - Mode Confusion: $CONFUSION_COUNT incidents added"
        fi

        if [[ -n "$AUTHORITY_OVERRIDES" ]]; then
            echo "  - Authority Overrides: $OVERRIDE_COUNT events added"
        fi

        # Update total error count in systemMonitoring.md
        TOTAL_ERRORS=$(grep -c "^\- \[" "$SYSTEM_MONITORING" 2>/dev/null || echo "0")
        sed -i "s/^\\*\\*Total Errors\\*\\*: [0-9]\\+/**Total Errors**: $TOTAL_ERRORS/" "$SYSTEM_MONITORING"

        echo ""
        echo "Total errors tracked: $TOTAL_ERRORS"
        echo ""
    fi
}

# ==============================================================================
# AUTOMATIC MODE (called by session-end hook)
# ==============================================================================

automatic_mode() {
    log "Running in AUTOMATIC mode (session-end hook)"

    # Archive watching file
    ARCHIVE_PATH=$(archive_watching_file)

    if [[ -n "$ARCHIVE_PATH" ]]; then
        # Reset watching file for next session
        reset_watching_file

        # Output JSON for hook
        cat <<EOF
{
  "hookSpecificOutput": "📋 Session watching file archived to Work/sessions/archive/\n\n**Reminder:** Run \`/save\` to synthesize session context to activeContext.md\n\nArchived: $(basename "$ARCHIVE_PATH")"
}
EOF
    else
        echo "{}"
    fi
}

# ==============================================================================
# INTERACTIVE MODE (called by /save command or manually)
# ==============================================================================

interactive_mode() {
    log "Running in INTERACTIVE mode (/save command)"

    # Step 1: Check for session watching file
    if [[ -f "$WATCHING_FILE" ]]; then
        log "Found session watching file, extracting context..."

        # Extract sections for Four Questions
        SIGNIFICANT_OPS=$(extract_section "Significant Operations" "$WATCHING_FILE")
        DECISIONS=$(extract_section "Decisions & Clarifications" "$WATCHING_FILE")

        echo ""
        echo "=== SESSION WATCHING FILE SUMMARY ==="
        echo ""

        if [[ -n "$SIGNIFICANT_OPS" ]]; then
            echo "## Significant Operations"
            echo "$SIGNIFICANT_OPS"
            echo ""
        fi

        if [[ -n "$DECISIONS" ]]; then
            echo "## Decisions & Clarifications"
            echo "$DECISIONS"
            echo ""
        fi

        echo "======================================"
        echo ""

        # Extract and display errors/anomalies for synthesis
        synthesize_errors_to_monitoring
    fi

    # Step 2: Four Questions Protocol (output guidance for Claude/user)
    cat <<'EOF'

## THE FOUR QUESTIONS PROTOCOL

Please answer these questions to guide Memory Bank updates:

### 1. What was the goal?
- Restate original objective from conversation start or Memory/activeContext.md Next Steps
- Verify alignment between stated goal and actual work performed

### 2. What did we accomplish?
- List concrete deliverables completed this session
- Identify partial vs full completions
- Note unexpected outcomes or discoveries

### 3. What did we learn?
- Validated Patterns: Techniques that worked well
- Pitfalls Encountered: Mistakes made and prevention strategies
- Knowledge Gaps Discovered: What we don't know
- Assumptions Challenged: What we thought was true but wasn't

### 4. What comes next?
- Immediate next steps (next session priorities)
- Blocked items requiring external input
- Deferred decisions with rationale

EOF

    # Step 3: Memory cleanup check
    CLEANUP_NEEDED=$(check_memory_cleanup_needed)
    if [[ "$CLEANUP_NEEDED" == "true" ]]; then
        LINE_COUNT=$(wc -l < "$ACTIVE_CONTEXT")
        cat <<EOF

⚠️  MEMORY CLEANUP REQUIRED
Memory/activeContext.md: $LINE_COUNT lines (>500 threshold)

After synthesis, trim to operational size:
- Preserve: Frontmatter, Current Status, Last 2-3 activities, Critical Reference, Next Steps
- Archive: Older activities (>2-3 sessions old)
- Target: ~150-300 lines for optimal bootstrap

EOF
    fi

    # Step 4: Next steps instructions
    cat <<'EOF'

## MEMORY UPDATE INSTRUCTIONS

Based on Four Questions answers, update Memory Bank files:

1. **Memory/activeContext.md** (always update):
   - Add session summary with timestamp
   - Record accomplishments (Q2)
   - Note key learnings (Q3)
   - Update Next Steps (Q4)
   - Increment version number

2. **Memory/workflowProtocols.md** (if Q3 reveals patterns):
   - Add validated techniques
   - Document pitfalls with prevention

3. **Memory/progress.md** (if significant milestones):
   - Record phase completion
   - Update project status

## GIT COMMIT

After Memory Bank updates:
1. Archive watching file (if exists)
2. Stage all changed files: `git add Memory/`
3. Commit with descriptive message documenting session updates
4. Push to remote: `git push`

Commit message format:
- Brief session activities description
- Note significant decisions/changes
- If memory trimmed: "Memory cleanup: [old]→[new] lines"
- Include AI collaboration footer

EOF

    # Step 5: Archive watching file after synthesis
    if [[ -f "$WATCHING_FILE" ]]; then
        log ""
        log "After Memory Bank updates complete, run:"
        log "  $0 --archive-only"
        log ""
        log "This will:"
        log "  - Archive current watching file to archive/"
        log "  - Create fresh watching file for next session"
    fi
}

# ==============================================================================
# ARCHIVE-ONLY MODE (called after manual Memory updates)
# ==============================================================================

archive_only_mode() {
    log "Running in ARCHIVE-ONLY mode"

    ARCHIVE_PATH=$(archive_watching_file)

    if [[ -n "$ARCHIVE_PATH" ]]; then
        log "Session watching file archived and reset"
    else
        log "No archiving needed (file empty or missing)"
    fi

    # Always reset watching file to ensure continuity
    # (mid-session /save should not break session tracking)
    reset_watching_file
}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

case "$MODE" in
    --automatic)
        automatic_mode
        ;;
    --interactive)
        interactive_mode
        ;;
    --archive-only)
        archive_only_mode
        ;;
    *)
        error "Unknown mode: $MODE. Use --interactive, --automatic, or --archive-only"
        ;;
esac
