#!/bin/bash
# Common utilities for Asha hooks (plugin version)
# Source this file in hooks: source "$(dirname "$0")/common.sh"

# Shared project-root resolver (single source of truth for root detection).
# Parameter expansion, not `dirname`: a hook must not acquire a dependency on
# an external binary before it can even resolve a project (a stripped PATH
# previously degraded to "no project"; it must not become exit 127). Sourced
# defensively so a partial install degrades instead of erroring.
if [[ -f "${BASH_SOURCE[0]%/*}/../../tools/project-root.sh" ]]; then
    source "${BASH_SOURCE[0]%/*}/../../tools/project-root.sh"
fi

# Detect project directory
# Returns project directory path on stdout, or empty string if not found
# Always returns 0 (safe under set -e)
# Hook detector layer set: env verbatim, else git-with-Memory; NO upward
# walk; $HOME guard. Semantics pinned by Test 9/9b — do not widen here.
detect_project_dir() {
    # Fail-open on a partial install: hooks must never hard-error.
    if ! declare -F asha_detect_project_root >/dev/null 2>&1; then
        echo ""
        return 0
    fi
    asha_detect_project_root "env,git" 1
}

# Resolve a hook payload's authoritative cwd to its initialized project root.
# A supplied cwd/project_dir owns the decision: walk upward from it, bounded by
# HOME and /, and never fall through to stale ambient harness state. Only when
# the payload carries no location at all may the historical env/git resolver
# participate.
resolve_hook_project_dir() {
    local input="${1:-}" authoritative=0 start="" search="" home_canon=""
    if command -v jq >/dev/null 2>&1 && [[ -n "$input" ]] \
        && printf '%s' "$input" | jq -e 'type == "object" and (has("cwd") or has("project_dir"))' >/dev/null 2>&1; then
        authoritative=1
        start="$(printf '%s' "$input" | jq -r '.cwd // .project_dir // empty | select(type == "string")' 2>/dev/null || true)"
    fi

    if [[ $authoritative -eq 0 ]]; then
        detect_project_dir 2>/dev/null || true
        return 0
    fi

    [[ -n "$start" && -d "$start" ]] || { echo ""; return 0; }
    search="$(cd -P "$start" 2>/dev/null && pwd)" || { echo ""; return 0; }
    home_canon="$(cd -P "${HOME:-}" 2>/dev/null && pwd)" || home_canon=""
    while [[ -n "$search" && "$search" != "/" ]]; do
        [[ -n "$home_canon" && "$search" == "$home_canon" ]] && break
        if [[ -f "$search/.asha/config.json" ]]; then
            echo "$search"
            return 0
        fi
        search="${search%/*}"
        [[ -n "$search" ]] || search="/"
    done
    echo ""
    return 0
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
    local project_dir="${1:-}"
    [[ -n "$project_dir" ]] || project_dir=$(detect_project_dir)

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
