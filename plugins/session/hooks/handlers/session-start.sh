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

# Consume the hook payload (skip on a tty so manual debug runs don't hang).
INPUT=""
[[ -t 0 ]] || INPUT=$(cat 2>/dev/null || true)

# Generate new session ID. Under copilot (COPILOT_CLI=1 stamped on its own
# hook processes; payload verified live 2026-07-27 on 1.0.75:
# {sessionId, timestamp, cwd, source, initialPrompt}) use the harness's own
# session uuid — it is the id transcript-derived events are stamped with, so
# orphan detection and recovery resolve the right native transcript.
NEW_SESSION_ID="session_$(date -u '+%Y%m%d_%H%M%S')_$$"
if [[ "${COPILOT_CLI:-}" == "1" ]]; then
    # COPILOT_CLI is stamped by copilot on its own hook processes and is
    # authoritative; an inherited ASHA_HARNESS (e.g. copilot launched from
    # inside a Claude session) would send orphan recovery hunting for a
    # Claude transcript that does not exist.
    export ASHA_HARNESS="copilot"
    if command -v jq >/dev/null 2>&1; then
        COPILOT_SID=$(echo "$INPUT" | jq -r '.sessionId // empty' 2>/dev/null || true)
        if [[ -n "$COPILOT_SID" ]]; then
            NEW_SESSION_ID="$COPILOT_SID"
        fi
    fi
fi
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

# Copilot has no per-tool event capture (retired 2026-05-10; save derives
# events from the native transcript on demand), so a crashed copilot session
# would leave NO trace in Memory/events/events.jsonl and orphan detection
# could never see it. Append one identity breadcrumb stamped with the harness
# session uuid: a crash leaves it as the last event, the next session start
# flags it, and recovery re-synthesizes from the surviving native transcript
# (~/.copilot/session-state/<sid>/events.jsonl). Clean saves replace the
# events file wholesale, so the breadcrumb never accumulates.
if [[ "${COPILOT_CLI:-}" == "1" ]] && command -v jq >/dev/null 2>&1; then
    mkdir -p "$PROJECT_DIR/Memory/events"
    jq -nc --arg sid "$NEW_SESSION_ID" --arg pd "$PROJECT_DIR" \
        --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')" \
        --arg id "evt_$(date -u '+%Y%m%d_%H%M%S')_sessionstart" '{
          id: $id, timestamp: $ts, session_id: $sid,
          type: "event", subtype: "session_started",
          payload: {detail: "copilot session started (identity breadcrumb)"},
          metadata: {source: "session-start-hook", project_dir: $pd, tool_name: null}
        }' >> "$PROJECT_DIR/Memory/events/events.jsonl" 2>/dev/null || true
fi

# Build the compact description-only memory nudge index. This is Claude-only
# runtime behavior, non-blocking, and skipped entirely by the kill switch.
# COPILOT_CLI is stamped by copilot on its own hook processes (this handler
# also runs as a copilot sessionStart hook, where ASHA_HARNESS may be unset).
MEMORY_NUDGE="$PLUGIN_ROOT/tools/memory_nudge.py"
if [[ "${ASHA_HARNESS:-claude}" == "claude" && "${COPILOT_CLI:-}" != "1" \
      && "${ASHA_NUDGE:-1}" != "0" \
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
# The `asha` wrapper (~/.local/bin/asha) sets ASHA_PERSONA=1 and injects persona
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

# Learnings: index-first injection (default) — one capped line per concept
# across the WHOLE bundle, bodies read on demand (the memory-lexical nudge
# points at them at tool-time). ASHA_LEARNINGS_INJECT=hot reverts to the
# legacy top-10 full-body hot tier without a code change.
# Falls back to the legacy flat file for projects not yet migrated to the bundle.
LEARNINGS_LABEL="Learnings index (one line per concept; Read the concept file before acting when the line is insufficient)"
if [[ -d "$LEARNINGS_DIR" && -f "$LEARNINGS_MANAGER" && -n "$PYTHON_CMD" ]]; then
    if [[ "${ASHA_LEARNINGS_INJECT:-index}" == "hot" ]]; then
        RENDERED=$("$PYTHON_CMD" "$LEARNINGS_MANAGER" render-hot --max-bytes "$LEARNINGS_MAX" 2>/dev/null || true)
        LEARNINGS_LABEL="Learnings (hot tier)"
    else
        RENDERED=$("$PYTHON_CMD" "$LEARNINGS_MANAGER" render-index --max-bytes "$LEARNINGS_MAX" 2>/dev/null || true)
    fi
    if [[ -n "$RENDERED" ]]; then
        LEARNINGS_CONTENT=$(truncate_content "$RENDERED" $LEARNINGS_MAX "learnings injection")
    fi
elif [[ -f "$LEARNINGS_FILE" ]]; then
    LEARNINGS_CONTENT=$(truncate_content "$(cat "$LEARNINGS_FILE")" $LEARNINGS_MAX "learnings.md")
    LEARNINGS_LABEL="Learnings (legacy flat file)"
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
$LEARNINGS_LABEL loaded from ~/.asha/learnings/:

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
