#!/bin/bash
set -euo pipefail
# UserPromptSubmit Hook — INTERVENTION ONLY (capture moved to /save jsonl_reader)
#
# What this hook USED to do: emit context/decision events for every prompt
# >15 chars (or containing '?') to Memory/events/events.jsonl. That capture
# is now derived at /save time by jsonl_reader, parsing the host's native
# session transcript directly. The hook's emit path was redundant.
#
# What this hook STILL does:
#   - While Work/markers/rp-active exists (and Work/markers/rp-hook-off does
#     not), inject the per-turn RP routing directive so the main loop spawns
#     the isolated roleplay-gm orchestrator instead of voicing NPCs from its
#     accumulated context.
#
# What it no longer does: LanguageTool prompt refinement (removed 2026-07-18).
# The injection's audience is a language model, which normalizes typos
# natively; observed interventions were harmful (speller rewrote domain
# jargon: 'xslx' -> 'XSLT') while beneficial fires were zero. The statusline
# last-correction marker is dormant as a result.

# Source common utilities
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/harness-response.sh"

PROJECT_DIR=$(detect_project_dir)
if [[ -z "$PROJECT_DIR" ]]; then
    # Cannot detect project directory - exit silently (no error spam to user)
    user_prompt_submit_noop
    exit 0
fi

PLUGIN_ROOT=$(get_plugin_root)
if [[ -z "$PLUGIN_ROOT" ]]; then
    user_prompt_submit_noop
    exit 0
fi

# Only run if Asha is initialized
if ! is_asha_initialized; then
    user_prompt_submit_noop
    exit 0
fi

# Skip everything if silence mode active (master override)
if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
    user_prompt_submit_noop
    exit 0
fi

# During RP sessions: skip capture and LanguageTool refinement, but re-assert
# the RP routing directive every turn — the one-shot /rp setup scrolls out of
# the model's attention; this injection cannot. Kill-switch: touch
# Work/markers/rp-hook-off to suppress the directive without editing code.
if [[ -f "$PROJECT_DIR/Work/markers/rp-active" ]]; then
    if [[ -f "$PROJECT_DIR/Work/markers/rp-hook-off" ]]; then
        user_prompt_submit_noop
        exit 0
    fi

    INPUT=$(cat)
    PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null || true)

    # Malformed or empty stdin: no-op rather than risk clobbering the turn
    # with an empty {prompt: ""} passthrough.
    if [[ -z "$PROMPT" || "$PROMPT" == "null" ]]; then
        user_prompt_submit_noop
        exit 0
    fi

    user_prompt_submit_rp_routing
    if user_prompt_submit_stops_after_injection; then
        exit 0
    fi
    user_prompt_submit_final_prompt "$PROMPT"
    exit 0
fi

# Ensure marker directory exists (Memory/events no longer written here).
mkdir -p "$PROJECT_DIR/Work/markers"

# Read stdin JSON from Claude Code
INPUT=$(cat)

PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null || true)

user_prompt_submit_final_prompt "$PROMPT"
