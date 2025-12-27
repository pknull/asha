#!/bin/bash
# Common utilities for Asha hooks
# Source this file in hooks: source "$(dirname "$0")/common.sh"

# Detect project directory with multi-layered fallback
# Returns project directory path or exits with code 1 if not found
detect_project_dir() {
    # Layer 1: Use CLAUDE_PROJECT_DIR if set (Claude Code hook invocation)
    if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        echo "$CLAUDE_PROJECT_DIR"
        return 0
    fi

    # Layer 1.5: Use OPENCODE_PROJECT_DIR if set (OpenCode plugin invocation)
    if [[ -n "${OPENCODE_PROJECT_DIR:-}" ]]; then
        echo "$OPENCODE_PROJECT_DIR"
        return 0
    fi

    # Layer 2: Try git root (fallback when env var not set)
    if command -v git >/dev/null 2>&1; then
        local git_root
        git_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
        if [[ -n "$git_root" ]] && [[ -d "$git_root/Memory" ]]; then
            echo "$git_root"
            return 0
        fi
    fi

    # Layer 3: All detection methods failed
    return 1
}

# Find Asha directory (could be submodule or embedded)
detect_asha_dir() {
    local project_dir
    project_dir=$(detect_project_dir) || return 1

    # Check common locations (case-insensitive check for asha/Asha)
    if [[ -d "$project_dir/asha" ]]; then
        echo "$project_dir/asha"
        return 0
    elif [[ -d "$project_dir/Asha" ]]; then
        echo "$project_dir/Asha"
        return 0
    fi

    # Fallback: hooks are in asha/hooks/, go up one level
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "$script_dir/../CORE.md" ]]; then
        echo "$(cd "$script_dir/.." && pwd)"
        return 0
    fi

    return 1
}
