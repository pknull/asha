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
