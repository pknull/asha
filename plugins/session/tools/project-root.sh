#!/usr/bin/env bash
# project-root.sh — the ONE project-root resolver (workspace v1, issue #33).
# source-scoped library: no set flags at file scope (runs in the caller's shell)
#
# Before this file existed, the layered Memory-root algorithm lived in three
# divergent bash copies (hooks/handlers/common.sh, tools/save-session.sh,
# tools/save-preflight-env.sh) — no two byte-identical. Workspace detection
# added to one copy would not have propagated. Each caller now declares its
# HISTORICAL layer set, so per-consumer behavior stays byte-identical
# (pinned by tests/test-hooks.sh Test 9b) while the algorithm exists exactly
# once. Do not add a new independent fallback chain anywhere — extend this.
#
# asha_detect_project_root LAYERS HOME_GUARD [EXPLICIT]
#   LAYERS      comma-joined subset of: env,git,walk
#     env   CLAUDE_PROJECT_DIR taken VERBATIM if set — unvalidated, and it
#           STOPS the chain even when the dir lacks Memory/ (every historical
#           detector behaved this way; harnesses that set it are trusted)
#     git   `git rev-parse --show-toplevel` accepted iff it contains Memory/
#     walk  upward from $PWD for a directory containing Memory/
#   HOME_GUARD  1 = a result that canonicalizes to $HOME is discarded ($HOME
#               is the identity layer, never a project)
#   EXPLICIT    optional explicit dir (a caller's --project-dir layer); taken
#               verbatim when non-empty, before every other layer
#
# stdout: resolved dir, or empty when nothing (acceptable) resolved.
# rc: always 0 — failure POLICY (exit, error text, fallthrough) is the
#     caller's, and the historical callers differ on it deliberately.
asha_detect_project_root() {
    local layers="$1" home_guard="$2" explicit="${3:-}"
    local dir=""

    if [[ -n "$explicit" ]]; then
        dir="$explicit"
    elif [[ ",$layers," == *",env,"* && -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        dir="$CLAUDE_PROJECT_DIR"
    else
        # `command -v git` guard is load-bearing, not decoration: without it a
        # shell carrying a command_not_found_handle can fabricate stdout that
        # would be accepted as a project root.
        if [[ -z "$dir" && ",$layers," == *",git,"* ]] \
            && command -v git >/dev/null 2>&1; then
            local git_root
            git_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
            if [[ -n "$git_root" && -d "$git_root/Memory" ]]; then
                dir="$git_root"
            fi
        fi
        if [[ -z "$dir" && ",$layers," == *",walk,"* ]]; then
            local search
            search="$(pwd)"
            while [[ "$search" != "/" ]]; do
                if [[ -d "$search/Memory" ]]; then
                    dir="$search"
                    break
                fi
                search="$(dirname "$search")"
            done
        fi
    fi

    if [[ "$home_guard" == "1" && -n "$dir" ]] \
        && command -v readlink >/dev/null 2>&1 \
        && [[ "$(readlink -f "$dir")" == "$(readlink -f "$HOME")" ]]; then
        dir=""
    fi

    echo "$dir"
    return 0
}

# asha_find_workspace_manifest [START]
#   Pure-bash EXISTENCE walk for .asha/workspace.json — the cheap half of
#   workspace detection, for hot paths (PreToolUse gates) that must not pay
#   a python start when no workspace exists. Mirrors project_root.py
#   detect_workspace bounds exactly: $HOME and / are both EXCLUSIVE, with
#   canonical comparison. VALIDATION stays in python — a caller that finds a
#   manifest here must hand off to project_root.py / save_scope.py and fail
#   closed if that handoff is unavailable. Prints the manifest path, or
#   nothing; rc 0 always.
asha_find_workspace_manifest() {
    local search home_canon
    search="$(cd -P "${1:-$PWD}" 2>/dev/null && pwd)" || { echo ""; return 0; }
    home_canon="$(cd -P "$HOME" 2>/dev/null && pwd)" || home_canon=""
    while [[ -n "$search" && "$search" != "/" ]]; do
        if [[ -n "$home_canon" && "$search" == "$home_canon" ]]; then
            break
        fi
        if [[ -f "$search/.asha/workspace.json" ]]; then
            echo "$search/.asha/workspace.json"
            return 0
        fi
        search="$(dirname "$search")"
    done
    echo ""
    return 0
}
