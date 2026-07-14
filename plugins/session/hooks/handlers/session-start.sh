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
MARKER_DIR="$PROJECT_DIR/Work/markers"
mkdir -p "$MARKER_DIR"

# Clean up stale markers from previous sessions
rm -f "$MARKER_DIR/tool-count"
rm -f "$MARKER_DIR/compact-suggested"
rm -f "$MARKER_DIR/last-correction"

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
        "$PYTHON_CMD" "$PATTERN_ANALYZER" recover --project-dir "$PROJECT_DIR" --session-id "$ORPHAN_SESSION" >/dev/null 2>&1 || true
        echo "Orphaned session recovered and synthesized." >&2
        echo "</system-reminder>" >&2
    fi
fi

# Store current session ID
echo "$NEW_SESSION_ID" > "$SESSION_MARKER"

# Build the compact description-only memory nudge index. This is Claude-only
# runtime behavior, non-blocking, and skipped entirely by the kill switch.
MEMORY_NUDGE="$PLUGIN_ROOT/tools/memory_nudge.py"
if [[ "${ASHA_HARNESS:-claude}" == "claude" && "${ASHA_NUDGE:-1}" != "0" \
      && -f "$MEMORY_NUDGE" && -n "$PYTHON_CMD" ]]; then
    "$PYTHON_CMD" "$MEMORY_NUDGE" build --project-dir "$PROJECT_DIR" >/dev/null 2>&1 || true
fi

# ==============================================================================
# CONTEXT INJECTION
# ==============================================================================

# Two-tier loading:
#   - Operational layer (operation.md + learnings.md): ALWAYS loaded
#   - Persona layer (soul.md + voice.md + keeper.md): ONLY when ASHA_PERSONA=1
#
# The `asha` wrapper (~/bin/asha) sets ASHA_PERSONA=1 and injects persona
# via --append-system-prompt-file. The hook handles operational + learnings.

CORE_MD="$PLUGIN_ROOT/modules/CORE.md"
ASHA_DIR="$HOME/.asha"

# Operational files (always loaded)
OPERATION_FILE="$ASHA_DIR/operation.md"
LEARNINGS_FILE="$ASHA_DIR/learnings.md"          # legacy flat file (pre-migration fallback)
LEARNINGS_DIR="$ASHA_DIR/learnings"              # OKF concept bundle (current)
LEARNINGS_MANAGER="$PLUGIN_ROOT/tools/learnings_manager.py"

# Persona files (only loaded when ASHA_PERSONA=1)
SOUL_FILE="$ASHA_DIR/soul.md"
VOICE_FILE="$ASHA_DIR/voice.md"
LEGACY_IDENTITY_FILE="$ASHA_DIR/communicationStyle.md"
KEEPER_FILE="$ASHA_DIR/keeper.md"

# ==============================================================================
# TRUNCATION - Cap file injection to prevent context window bloat
# ==============================================================================

truncate_content() {
    local content="$1"
    local max_chars="$2"
    local label="$3"
    local length=${#content}

    if [[ $length -le $max_chars ]]; then
        echo "$content"
    else
        echo "${content:0:$max_chars}"
        echo ""
        echo "[Truncated: ${label} exceeded ${max_chars} chars (${length} total). Read full file if needed.]"
    fi
}

# Character limits per file
OPERATION_MAX=4000
LEARNINGS_MAX=3000
SOUL_MAX=2000
VOICE_MAX=2000
KEEPER_MAX=2000

# ==============================================================================
# OPERATIONAL LAYER (always loaded)
# ==============================================================================

OPERATION_CONTENT=""
LEARNINGS_CONTENT=""

if [[ -f "$OPERATION_FILE" ]]; then
    OPERATION_CONTENT=$(truncate_content "$(cat "$OPERATION_FILE")" $OPERATION_MAX "operation.md")
fi

# Learnings: render the hot tier from the OKF bundle (confidence-ranked, budgeted).
# Falls back to the legacy flat file for projects not yet migrated to the bundle.
if [[ -d "$LEARNINGS_DIR" && -f "$LEARNINGS_MANAGER" && -n "$PYTHON_CMD" ]]; then
    RENDERED_HOT=$("$PYTHON_CMD" "$LEARNINGS_MANAGER" render-hot --max-bytes "$LEARNINGS_MAX" 2>/dev/null || true)
    if [[ -n "$RENDERED_HOT" ]]; then
        LEARNINGS_CONTENT=$(truncate_content "$RENDERED_HOT" $LEARNINGS_MAX "learnings hot tier")
    fi
elif [[ -f "$LEARNINGS_FILE" ]]; then
    LEARNINGS_CONTENT=$(truncate_content "$(cat "$LEARNINGS_FILE")" $LEARNINGS_MAX "learnings.md")
fi

# Fall back to CORE.md if operation.md doesn't exist yet
if [[ -z "$OPERATION_CONTENT" && -f "$CORE_MD" ]]; then
    OPERATION_CONTENT=$(truncate_content "$(cat "$CORE_MD")" $OPERATION_MAX "CORE.md")
fi

if [[ -n "$OPERATION_CONTENT" ]]; then
    cat <<EOF
<system-reminder>
Asha-managed project. Operational guidelines loaded.

$OPERATION_CONTENT

Available modules (reference as needed):
- ${PLUGIN_ROOT}/modules/cognitive.md - ACE cycle, parallel execution, tool efficiency
- ${PLUGIN_ROOT}/modules/research.md - Research protocols
- ${PLUGIN_ROOT}/modules/memory-ops.md - Memory operation protocols
- ${PLUGIN_ROOT}/modules/high-stakes.md - High-stakes decision protocols
- ${PLUGIN_ROOT}/modules/verbalized-sampling.md - Verbalized sampling technique
</system-reminder>
EOF
fi

if [[ -n "$LEARNINGS_CONTENT" ]]; then
    cat <<EOF
<system-reminder>
Learnings (hot tier) loaded from ~/.asha/learnings/:

$LEARNINGS_CONTENT
</system-reminder>
EOF
fi

# ==============================================================================
# PERSONA LAYER (only when ASHA_PERSONA=1)
# ==============================================================================

if [[ "${ASHA_PERSONA:-0}" == "1" ]]; then
    SOUL_CONTENT=""
    VOICE_CONTENT=""
    LEGACY_IDENTITY_CONTENT=""
    KEEPER_CONTENT=""

    if [[ -f "$SOUL_FILE" ]]; then
        SOUL_CONTENT=$(truncate_content "$(cat "$SOUL_FILE")" $SOUL_MAX "soul.md")
    fi

    if [[ -f "$VOICE_FILE" ]]; then
        VOICE_CONTENT=$(truncate_content "$(cat "$VOICE_FILE")" $VOICE_MAX "voice.md")
    fi

    if [[ -z "$SOUL_CONTENT" && -f "$LEGACY_IDENTITY_FILE" ]]; then
        LEGACY_IDENTITY_CONTENT=$(truncate_content "$(cat "$LEGACY_IDENTITY_FILE")" $VOICE_MAX "communicationStyle.md")
    fi

    if [[ -f "$KEEPER_FILE" ]]; then
        KEEPER_CONTENT=$(truncate_content "$(cat "$KEEPER_FILE")" $KEEPER_MAX "keeper.md")
    fi

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
fi

# If nothing loaded at all, output empty
if [[ -z "$OPERATION_CONTENT" && -z "$LEARNINGS_CONTENT" ]]; then
    echo "{}"
fi
