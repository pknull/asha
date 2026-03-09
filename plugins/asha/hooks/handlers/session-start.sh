#!/bin/bash
set -euo pipefail
# SessionStart Hook - Injects CORE.md context if Asha is initialized in project
# Only activates for projects with .asha/config.json present

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

# Only inject context if Asha is initialized in this project
if ! is_asha_initialized; then
    echo "{}"
    exit 0
fi

# ==============================================================================
# ORPHAN RECOVERY - Synthesize previous session if it didn't end cleanly
# ==============================================================================

# Generate new session ID
NEW_SESSION_ID="session_$(date -u '+%Y%m%d_%H%M%S')_$$"
SESSION_MARKER="$PROJECT_DIR/Work/markers/session-id"
mkdir -p "$(dirname "$SESSION_MARKER")"

# Check for orphaned session
PATTERN_ANALYZER="$PLUGIN_ROOT/tools/pattern_analyzer.py"
PYTHON_CMD=""

# Get Python command
if [[ -x "$PROJECT_DIR/.asha/.venv/bin/python3" ]]; then
    PYTHON_CMD="$PROJECT_DIR/.asha/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
fi

if [[ -f "$PATTERN_ANALYZER" && -n "$PYTHON_CMD" ]]; then
    # Check if there's an orphaned session
    ORPHAN_RESULT=$("$PYTHON_CMD" "$PATTERN_ANALYZER" check-orphan --current-session "$NEW_SESSION_ID" 2>/dev/null || echo '{}')
    ORPHAN_SESSION=$(echo "$ORPHAN_RESULT" | "$PYTHON_CMD" -c "import sys,json; print(json.load(sys.stdin).get('orphaned_session') or '')" 2>/dev/null || true)

    if [[ -n "$ORPHAN_SESSION" ]]; then
        # Recover orphaned session
        echo "<system-reminder>" >&2
        echo "Recovering orphaned session: $ORPHAN_SESSION" >&2
        "$PYTHON_CMD" "$PATTERN_ANALYZER" recover --session-id "$ORPHAN_SESSION" >/dev/null 2>&1 || true
        echo "Orphaned session recovered and synthesized." >&2
        echo "</system-reminder>" >&2
    fi
fi

# Store current session ID
echo "$NEW_SESSION_ID" > "$SESSION_MARKER"

# ==============================================================================
# CONTEXT INJECTION
# ==============================================================================

# Build context injection
# Include CORE.md, identity layer (~/.asha/), and module references
CORE_MD="$PLUGIN_ROOT/modules/CORE.md"
ASHA_DIR="$HOME/.asha"

# Identity files (soul.md + voice.md preferred, communicationStyle.md as legacy fallback)
SOUL_FILE="$ASHA_DIR/soul.md"
VOICE_FILE="$ASHA_DIR/voice.md"
LEGACY_IDENTITY_FILE="$ASHA_DIR/communicationStyle.md"
KEEPER_FILE="$ASHA_DIR/keeper.md"
LEARNINGS_FILE="$ASHA_DIR/learnings.md"

if [[ -f "$CORE_MD" ]]; then
    # Read CORE.md content
    CORE_CONTENT=$(cat "$CORE_MD")

    # Read identity layer files if they exist
    # Prefer soul.md + voice.md; fall back to communicationStyle.md
    SOUL_CONTENT=""
    VOICE_CONTENT=""
    LEGACY_IDENTITY_CONTENT=""
    KEEPER_CONTENT=""
    LEARNINGS_CONTENT=""

    if [[ -f "$SOUL_FILE" ]]; then
        SOUL_CONTENT=$(cat "$SOUL_FILE")
    fi

    if [[ -f "$VOICE_FILE" ]]; then
        VOICE_CONTENT=$(cat "$VOICE_FILE")
    fi

    # Legacy fallback only if soul.md doesn't exist
    if [[ -z "$SOUL_CONTENT" && -f "$LEGACY_IDENTITY_FILE" ]]; then
        LEGACY_IDENTITY_CONTENT=$(cat "$LEGACY_IDENTITY_FILE")
    fi

    if [[ -f "$KEEPER_FILE" ]]; then
        KEEPER_CONTENT=$(cat "$KEEPER_FILE")
    fi

    if [[ -f "$LEARNINGS_FILE" ]]; then
        LEARNINGS_CONTENT=$(cat "$LEARNINGS_FILE")
    fi

    # Output as system-reminder
    cat <<EOF
<system-reminder>
Asha is initialized in this project. Read and follow the bootstrap protocol below.

$CORE_CONTENT

Available modules (reference as needed):
- ${PLUGIN_ROOT}/modules/cognitive.md - ACE cycle, parallel execution, tool efficiency
- ${PLUGIN_ROOT}/modules/research.md - Research protocols
- ${PLUGIN_ROOT}/modules/memory-ops.md - Memory operation protocols
- ${PLUGIN_ROOT}/modules/high-stakes.md - High-stakes decision protocols
- ${PLUGIN_ROOT}/modules/verbalized-sampling.md - Verbalized sampling technique
</system-reminder>
EOF

    # Inject identity layer if present
    # Prefer soul.md + voice.md; fall back to legacy communicationStyle.md
    if [[ -n "$SOUL_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Soul loaded from ~/.asha/soul.md:

$SOUL_CONTENT
</system-reminder>
EOF
    fi

    if [[ -n "$VOICE_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Voice loaded from ~/.asha/voice.md:

$VOICE_CONTENT
</system-reminder>
EOF
    fi

    # Legacy fallback (only if soul.md doesn't exist)
    if [[ -n "$LEGACY_IDENTITY_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Identity layer loaded from ~/.asha/communicationStyle.md (legacy):

$LEGACY_IDENTITY_CONTENT
</system-reminder>
EOF
    fi

    if [[ -n "$KEEPER_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Keeper profile loaded from ~/.asha/keeper.md:

$KEEPER_CONTENT
</system-reminder>
EOF
    fi

    if [[ -n "$LEARNINGS_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Learnings loaded from ~/.asha/learnings.md:

$LEARNINGS_CONTENT
</system-reminder>
EOF
    fi

else
    echo "{}"
fi
