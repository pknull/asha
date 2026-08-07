#!/bin/bash
# Common utilities for Asha hooks (plugin version)
# Source this file in hooks: source "$(dirname "$0")/common.sh"

# Shared project-root resolver (single source of truth for root detection)
source "$(dirname "${BASH_SOURCE[0]}")/../../tools/project-root.sh"

# Detect project directory
# Returns project directory path on stdout, or empty string if not found
# Always returns 0 (safe under set -e)
# Hook detector layer set: env verbatim, else git-with-Memory; NO upward
# walk; $HOME guard. Semantics pinned by Test 9/9b — do not widen here.
detect_project_dir() {
    asha_detect_project_root "env,git" 1
}

# Get plugin root directory (where asha plugin is installed)
# Returns plugin directory path on stdout, or empty string if not found
# Always returns 0 (safe under set -e)
get_plugin_root() {
    # Use CLAUDE_PLUGIN_ROOT if set
    if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
        echo "$CLAUDE_PLUGIN_ROOT"
        return 0
    fi

    # Fallback: derive from script location
    # handlers are in hooks/handlers/, plugin root is two levels up
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "$script_dir/../../modules/CORE.md" ]]; then
        cd "$script_dir/../.." && pwd
        return 0
    fi

    # Not found — return empty string, not error
    echo ""
    return 0
}

# Check if Asha is initialized in current project
# Returns 0 if initialized, 1 otherwise
is_asha_initialized() {
    local project_dir
    project_dir=$(detect_project_dir)
    [[ -n "$project_dir" ]] && [[ -f "$project_dir/.asha/config.json" ]]
}

# Get Python command (venv if available, else system)
# Returns python path on stdout, or empty string if not found
# Always returns 0 (safe under set -e)
get_python_cmd() {
    local project_dir
    project_dir=$(detect_project_dir)

    # Check project's .asha/.venv first
    if [[ -n "$project_dir" ]] && [[ -x "$project_dir/.asha/.venv/bin/python3" ]]; then
        echo "$project_dir/.asha/.venv/bin/python3"
        return 0
    fi

    # Fallback to system python
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return 0
    fi

    # Not found — return empty string, not error
    echo ""
    return 0
}
