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
# Include CORE.md and module references
CORE_MD="$PLUGIN_ROOT/modules/CORE.md"

if [[ -f "$CORE_MD" ]]; then
    # Read CORE.md content
    CORE_CONTENT=$(cat "$CORE_MD")

    # Output as system-reminder
    cat <<EOF
<system-reminder>
Asha is initialized in this project. Read and follow the bootstrap protocol below.

$CORE_CONTENT

Available modules (reference as needed):
- ${PLUGIN_ROOT}/modules/code.md - Code writing protocols
- ${PLUGIN_ROOT}/modules/writing.md - Writing protocols
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
else
    echo "{}"
fi
