#!/bin/bash
set -euo pipefail
# UserPromptSubmit Hook — harness-appropriate prompt passthrough only.
#
# History of what this hook no longer does:
#   - Event capture (moved to /save jsonl_reader, which parses the host's
#     native session transcript directly; the hook's emit path was redundant).
#   - LanguageTool prompt refinement (removed 2026-07-18: the injection's
#     audience is a language model, which normalizes typos natively; observed
#     interventions were harmful while beneficial fires were zero).
#   - RP routing injection (moved 2026-07-25 to the declarative nudge engine:
#     hooks/nudges/rules.json row rp-routing, evaluated by
#     handlers/nudge-engine.sh, registered on this same event).
#
# What remains: the silence-mode noop and the prompt passthrough contract
# (Claude: {prompt: ...}; Codex: {} — see harness-response.sh).

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

# Ensure marker directory exists (Memory/events no longer written here).
mkdir -p "$PROJECT_DIR/Work/markers"

# Read stdin JSON from Claude Code
INPUT=$(cat)

PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null || true)

# Malformed or empty stdin: no-op rather than risk clobbering the turn with
# an empty {prompt: ""} passthrough.
if [[ -z "$PROMPT" || "$PROMPT" == "null" ]]; then
    user_prompt_submit_noop
    exit 0
fi

user_prompt_submit_final_prompt "$PROMPT"
