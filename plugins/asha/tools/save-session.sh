#!/bin/bash
# save-session.sh - Portable session save logic for Asha Memory Bank (plugin version)
# Can be called manually, via /asha:save command, or automatically via session-end hook
# Now uses event_store.py for structured event management

set -euo pipefail

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Multi-layered project directory detection
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
    local search_dir
    search_dir="$(pwd)"
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
    return 1
}

PROJECT_DIR=$(detect_project_dir) || exit 1

# Get plugin root directory (script is in tools/)
get_plugin_root() {
    if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
        echo "$CLAUDE_PLUGIN_ROOT"
    else
        # Script is in tools/, go up one level
        cd "$(dirname "$0")/.." && pwd
    fi
}

PLUGIN_ROOT=$(get_plugin_root)
MEMORY_DIR="$PROJECT_DIR/Memory"
EVENTS_DIR="$MEMORY_DIR/events"
EVENTS_FILE="$EVENTS_DIR/events.jsonl"
ACTIVE_CONTEXT="$MEMORY_DIR/activeContext.md"

# Legacy markdown paths (for backward compatibility during transition)
WATCHING_FILE="$MEMORY_DIR/sessions/current-session.md"
ARCHIVE_DIR="$MEMORY_DIR/sessions/archive"

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
TIMESTAMP_FILE=$(date -u '+%Y-%m-%d_%H-%M')

# ==============================================================================
# MODE DETECTION
# ==============================================================================

MODE="${1:---interactive}"  # --interactive (default), --automatic, --synthesize, etc.

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

# Get Python command (project venv if available)
get_python_cmd() {
    if [[ -x "$PROJECT_DIR/.asha/.venv/bin/python3" ]]; then
        echo "$PROJECT_DIR/.asha/.venv/bin/python3"
    elif command -v python3 >/dev/null 2>&1; then
        echo "python3"
    fi
}

# Get event summary from event_store.py
get_event_summary() {
    local days="${1:-7}"
    EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"
    PYTHON_CMD=$(get_python_cmd)

    if [[ -f "$EVENT_STORE" && -n "$PYTHON_CMD" ]]; then
        "$PYTHON_CMD" "$EVENT_STORE" query --type event --limit 50 2>/dev/null | \
            "$PYTHON_CMD" -c "
import sys, json
data = json.load(sys.stdin)
events = data.get('events', [])
if not events:
    print('No recent events found.')
    sys.exit(0)

# Group by subtype
by_subtype = {}
for e in events:
    st = e.get('subtype', 'unknown')
    if st not in by_subtype:
        by_subtype[st] = []
    by_subtype[st].append(e)

for subtype, evts in sorted(by_subtype.items()):
    print(f'## {subtype.replace(\"_\", \" \").title()} ({len(evts)})')
    for e in evts[:10]:
        detail = e.get('payload', {}).get('detail', str(e.get('payload', {}))[:80])
        ts = e.get('timestamp', '')[:16]
        print(f'  - [{ts}] {detail}')
    print()
" 2>/dev/null || echo "Could not retrieve events"
    else
        echo "Event store not available"
    fi
}

# Synthesize activeContext from events
synthesize_from_events() {
    local days="${1:-7}"
    EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"
    PYTHON_CMD=$(get_python_cmd)

    if [[ -f "$EVENT_STORE" && -n "$PYTHON_CMD" ]]; then
        "$PYTHON_CMD" "$EVENT_STORE" synthesize --days "$days" 2>/dev/null
    else
        error "Event store not available at $EVENT_STORE"
    fi
}

# Rotate old events to archive
rotate_events() {
    local days="${1:-30}"
    EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"
    PYTHON_CMD=$(get_python_cmd)

    if [[ -f "$EVENT_STORE" && -n "$PYTHON_CMD" ]]; then
        RESULT=$("$PYTHON_CMD" "$EVENT_STORE" rotate --days "$days" 2>/dev/null)
        ARCHIVED=$(echo "$RESULT" | "$PYTHON_CMD" -c "import sys,json; print(json.load(sys.stdin).get('archived',0))" 2>/dev/null || echo "0")
        log "Rotated events: $ARCHIVED archived"
    fi
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

# Legacy: Archive markdown watching file (for transition period)
archive_watching_file() {
    if [[ ! -f "$WATCHING_FILE" ]]; then
        return 0
    fi

    # Count non-trivial lines
    CONTENT_LINES=$(grep -cvE '^(<!--|#|---|$)' "$WATCHING_FILE" || echo 0)

    if [[ $CONTENT_LINES -lt 10 ]]; then
        return 0
    fi

    mkdir -p "$ARCHIVE_DIR"
    ARCHIVE_PATH="$ARCHIVE_DIR/session-$TIMESTAMP_FILE.md"
    cp "$WATCHING_FILE" "$ARCHIVE_PATH"
    log "Archived legacy watching file: $ARCHIVE_PATH"

    # Clear the legacy file
    rm -f "$WATCHING_FILE"
}

# ==============================================================================
# AUTOMATIC MODE (called by session-end hook)
# ==============================================================================

automatic_mode() {
    log "Running in AUTOMATIC mode (session-end hook)"

    # Archive legacy markdown if exists
    archive_watching_file

    # Rotate old events (keep last 30 days in active file)
    rotate_events 30

    # Output valid JSON for hook
    echo "{}"
}

# ==============================================================================
# SYNTHESIZE MODE (generate activeContext from events)
# ==============================================================================

synthesize_mode() {
    local days="${1:-7}"
    log "Running in SYNTHESIZE mode (generating activeContext from events)"

    # Generate synthesized content
    CONTENT=$(synthesize_from_events "$days")

    if [[ -z "$CONTENT" ]]; then
        error "Failed to synthesize content from events"
    fi

    # Output to stdout or write to file
    if [[ "${2:-}" == "--write" ]]; then
        echo "$CONTENT" > "$ACTIVE_CONTEXT"
        log "Written synthesized activeContext.md ($days days of events)"
    else
        echo "$CONTENT"
    fi
}

# ==============================================================================
# INTERACTIVE MODE (called by /asha:save command or manually)
# ==============================================================================

interactive_mode() {
    log "Running in INTERACTIVE mode (/asha:save command)"

    # Step 1: Show event summary
    echo ""
    echo "=== SESSION EVENT SUMMARY ==="
    echo ""
    get_event_summary 7
    echo "======================================"
    echo ""

    # Step 2: Four Questions Protocol
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

    # Step 3: ReasoningBank pattern recording prompt
    REASONING_BANK="$PLUGIN_ROOT/tools/reasoning_bank.py"
    PYTHON_CMD=$(get_python_cmd)
    if [[ -f "$REASONING_BANK" && -n "$PYTHON_CMD" ]]; then
        cat <<EOF

## REASONING BANK (Pattern Learning)

Record significant patterns from this session for cross-session learning:

**Workflow patterns** (what approaches worked/failed):
\`\`\`bash
$PYTHON_CMD "$REASONING_BANK" record --type workflow \\
    --context "situation description" \\
    --action "approach taken" \\
    --outcome "result" \\
    --score 0.9  # 0.0=failed, 1.0=perfect
\`\`\`

**Error resolutions** (for recurring issues):
\`\`\`bash
$PYTHON_CMD "$REASONING_BANK" error \\
    --type "ErrorType" \\
    --signature "error message pattern" \\
    --resolution "what fixed it" \\
    --prevention "how to avoid"
\`\`\`

**Query before acting** (check what worked before):
\`\`\`bash
$PYTHON_CMD "$REASONING_BANK" query --context "similar situation"
\`\`\`

Skip if session was routine. Record only when you learned something transferable.

EOF
    fi

    # Step 3b: Facet auto-ingestion
    FACET_INGEST="$PLUGIN_ROOT/tools/facet_ingest.py"
    if [[ -f "$FACET_INGEST" && -n "$PYTHON_CMD" ]]; then
        FACET_RESULT=$(CLAUDE_PROJECT_DIR="$PROJECT_DIR" "$PYTHON_CMD" "$FACET_INGEST" ingest 2>/dev/null || echo '{"ingested": 0}')
        INGESTED=$(echo "$FACET_RESULT" | "$PYTHON_CMD" -c "import sys,json; print(json.load(sys.stdin).get('ingested',0))" 2>/dev/null || echo "0")
        if [[ "$INGESTED" -gt 0 ]]; then
            echo ""
            echo "## FACET INGESTION"
            echo "Auto-ingested $INGESTED facet(s) from previous sessions into ReasoningBank."
            echo ""
        fi
    fi

    # Step 4: Memory cleanup check
    CLEANUP_NEEDED=$(check_memory_cleanup_needed)
    if [[ "$CLEANUP_NEEDED" == "true" ]]; then
        LINE_COUNT=$(wc -l < "$ACTIVE_CONTEXT")
        cat <<EOF

⚠️  MEMORY CLEANUP REQUIRED
Memory/activeContext.md: $LINE_COUNT lines (>500 threshold)

Consider regenerating from events:
  $0 --synthesize 7 --write

EOF
    fi

    # Step 5: Synthesis instructions
    cat <<EOF

## MEMORY UPDATE OPTIONS

**Option A: Event-based synthesis** (recommended)
Regenerate activeContext.md from events:
\`\`\`bash
$0 --synthesize 7 --write
\`\`\`

**Option B: Manual update** (for adding context not captured in events)
Edit Memory/activeContext.md directly, then:
\`\`\`bash
$0 --archive-only
\`\`\`

## GIT COMMIT

After Memory Bank updates:
1. Stage all changed files: \`git add Memory/\`
2. Commit with descriptive message
3. Push to remote: \`git push\`

EOF
}

# ==============================================================================
# ANALYZE MODE (ReAct pattern analysis)
# ==============================================================================

analyze_mode() {
    log "Running in ANALYZE mode (--react flag)"

    LOCAL_REACT="$PLUGIN_ROOT/tools/local_react_save.py"
    PYTHON_CMD=$(get_python_cmd)

    if [[ ! -f "$LOCAL_REACT" ]]; then
        log "local_react_save.py not found at $LOCAL_REACT"
        return 1
    fi

    if [[ -z "$PYTHON_CMD" ]]; then
        log "Python not available, cannot run ReAct analysis"
        return 1
    fi

    # Run the ReAct analysis with events
    EVENT_STORE="$PLUGIN_ROOT/tools/event_store.py"
    if [[ -f "$EVENT_STORE" ]]; then
        # Export recent events for analysis
        EVENTS_JSON=$("$PYTHON_CMD" "$EVENT_STORE" query --limit 100 2>/dev/null || echo '{"events":[]}')
        echo "$EVENTS_JSON" | "$PYTHON_CMD" "$LOCAL_REACT" - "$MEMORY_DIR"
    else
        log "Event store not available, cannot analyze"
        return 1
    fi
}

# ==============================================================================
# ARCHIVE-ONLY MODE (called after manual Memory updates)
# ==============================================================================

archive_only_mode() {
    log "Running in ARCHIVE-ONLY mode"

    # Archive legacy markdown if exists
    archive_watching_file

    # Rotate old events
    rotate_events 30

    # Refresh vector DB index if memory_index.py exists
    MEMORY_INDEX="$PLUGIN_ROOT/tools/memory_index.py"
    PYTHON_CMD=$(get_python_cmd)
    if [[ -f "$MEMORY_INDEX" && -n "$PYTHON_CMD" ]]; then
        log "Refreshing vector DB index (incremental)..."
        "$PYTHON_CMD" "$MEMORY_INDEX" ingest --changed 2>&1 | while read -r line; do
            log "  $line"
        done
        log "Vector DB index refresh complete"
    fi

    log "Archive and cleanup complete"
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
    --synthesize)
        DAYS="${2:-7}"
        WRITE_FLAG="${3:-}"
        synthesize_mode "$DAYS" "$WRITE_FLAG"
        ;;
    --archive-only)
        archive_only_mode
        ;;
    --analyze|--react)
        analyze_mode
        ;;
    *)
        error "Unknown mode: $MODE. Use --interactive, --automatic, --synthesize, --archive-only, or --analyze"
        ;;
esac
