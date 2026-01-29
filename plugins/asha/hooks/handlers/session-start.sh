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
IDENTITY_FILE="$ASHA_DIR/communicationStyle.md"
KEEPER_FILE="$ASHA_DIR/keeper.md"

if [[ -f "$CORE_MD" ]]; then
    # Read CORE.md content
    CORE_CONTENT=$(cat "$CORE_MD")

    # Read identity layer files if they exist
    IDENTITY_CONTENT=""
    KEEPER_CONTENT=""

    if [[ -f "$IDENTITY_FILE" ]]; then
        IDENTITY_CONTENT=$(cat "$IDENTITY_FILE")
    fi

    if [[ -f "$KEEPER_FILE" ]]; then
        KEEPER_CONTENT=$(cat "$KEEPER_FILE")
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

Tools available:
- Semantic search: "${PLUGIN_ROOT}/tools/run-python.sh" "${PLUGIN_ROOT}/tools/memory_index.py" search "query"
- ReasoningBank query: "${PLUGIN_ROOT}/tools/run-python.sh" "${PLUGIN_ROOT}/tools/reasoning_bank.py" query --context "situation"
- Memory search wrapper: "${PLUGIN_ROOT}/tools/memory-search" "query"
</system-reminder>
EOF

    # Inject identity layer if present
    if [[ -n "$IDENTITY_CONTENT" ]]; then
        cat <<EOF
<system-reminder>
Identity layer loaded from ~/.asha/communicationStyle.md:

$IDENTITY_CONTENT
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
else
    echo "{}"
fi
