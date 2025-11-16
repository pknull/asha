#!/bin/bash
# Common utilities for memory-session-manager hooks
# Source this file in hooks: source "$(dirname "$0")/common.sh"

# Detect project directory with multi-layered fallback
# Returns project directory path or exits with code 1 if not found
detect_project_dir() {
    # Layer 1: Use CLAUDE_PROJECT_DIR if set (hook invocation)
    if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        echo "$CLAUDE_PROJECT_DIR"
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
