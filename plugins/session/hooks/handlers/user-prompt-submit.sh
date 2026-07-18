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
#   - Optionally refine the user's prompt via LanguageTool (localhost:8081),
#     and inject a <system-reminder> when the correction is ≥10% diff. This is
#     intervention (modifies the model's input) and has no transcript-tail
#     equivalent.
#   - While Work/markers/rp-active exists (and Work/markers/rp-hook-off does
#     not), inject the per-turn RP routing directive so the main loop spawns
#     the isolated roleplay-gm orchestrator instead of voicing NPCs from its
#     accumulated context. LanguageTool refinement stays skipped during RP —
#     the Keeper's in-character prose must not be rewritten.

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

# Extract user prompt (used by LanguageTool refinement below).
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null || true)

# ============================================================================
# AUTOMATIC PROMPT REFINEMENT (LanguageTool integration)
# Always runs, but only injects system-reminder if correction ≥10% change
# ============================================================================

# Clear correction indicator by default (will be set again if correction ≥10%)
rm -f "$PROJECT_DIR/Work/markers/last-correction" 2>/dev/null || true

# Always attempt correction if prompt is substantial and not in special modes
if [[ -n "$PROMPT" && "$PROMPT" != "null" ]]; then
    # Count words in prompt (skip refinement for very short prompts)
    WORD_COUNT=$(echo "$PROMPT" | wc -w)

    if [[ $WORD_COUNT -ge 5 ]]; then
        # Call LanguageTool API (local server on localhost:8081)
        # Silently fail if server unavailable
        LT_RESPONSE=$(curl -s -X POST http://localhost:8081/v2/check \
            --data-urlencode "text=$PROMPT" \
            --data "language=en-US" \
            --max-time 3 2>/dev/null || true)

        # Check if we got matches (server available and found corrections)
        if [[ -n "$LT_RESPONSE" && "$LT_RESPONSE" != "null" ]]; then
            # Extract matches and apply corrections
            # Pass data via environment variables to avoid shell injection
            CORRECTION_RESULT=$(LT_RESPONSE_DATA="$LT_RESPONSE" LT_ORIGINAL_TEXT="$PROMPT" python3 -c "
import json
import os
import sys

try:
    response = json.loads(os.environ['LT_RESPONSE_DATA'])
    original_text = os.environ['LT_ORIGINAL_TEXT']

    matches = response.get('matches', [])

    # Never apply spelling suggestions: the model reads through typos, and
    # the speller's first guess on domain jargon is confidently wrong
    # (observed: 'xslx' -> 'XSLT' while the user meant the .xlsx file).
    # Grammar/punctuation/style matches remain eligible.
    matches = [m for m in matches
               if m.get('rule', {}).get('issueType') != 'misspelling']

    if not matches:
        print('UNCHANGED')
        sys.exit(0)

    matches.sort(key=lambda m: m['offset'], reverse=True)

    corrected_text = original_text
    for match in matches:
        offset = match['offset']
        length = match['length']
        replacements = match.get('replacements', [])

        if replacements:
            replacement = replacements[0]['value']
            corrected_text = corrected_text[:offset] + replacement + corrected_text[offset+length:]

    if corrected_text == original_text:
        print('UNCHANGED')
        sys.exit(0)

    def levenshtein(s1, s2):
        if len(s1) < len(s2):
            return levenshtein(s2, s1)
        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    edit_distance = levenshtein(original_text, corrected_text)
    original_chars = len(original_text)
    diff_percent = (edit_distance / original_chars * 100) if original_chars > 0 else 0

    print(f'{diff_percent:.1f}|{corrected_text}')

except Exception as e:
    print('UNCHANGED')
" 2>/dev/null || true)

            # Parse correction result
            if [[ "$CORRECTION_RESULT" != "UNCHANGED" && -n "$CORRECTION_RESULT" ]]; then
                DIFF_PERCENT=$(echo "$CORRECTION_RESULT" | cut -d'|' -f1)
                REFINED=$(echo "$CORRECTION_RESULT" | cut -d'|' -f2-)

                # Only inject system-reminder if difference ≥ 10%
                DIFF_INT=${DIFF_PERCENT%.*}  # Convert to integer for comparison
                if [[ $DIFF_INT -ge 10 ]]; then
                    # Signal statusline: last prompt was corrected
                    touch "$PROJECT_DIR/Work/markers/last-correction" 2>/dev/null || true

                    # (capture-side emit_event removed — significant prompt
                    # corrections are now derivable by diffing transcript
                    # prompts against the marker file at /save time.)

                    # Inject correction as system-reminder (via stdout).
                    # Codex accepts raw prompt fragments for UserPromptSubmit,
                    # but rejects Claude's {prompt: ...} response shape as
                    # invalid JSON. Emit the fragment and stop before the
                    # final Claude-only response below.
                    user_prompt_submit_correction "$REFINED"
                    if user_prompt_submit_stops_after_injection; then
                        exit 0
                    fi
                fi
                # If < 10%, marker stays cleared (removed at start of hook)
            fi
            # If no correction, marker stays cleared (removed at start of hook)
        fi
        # If server unavailable, marker stays cleared (removed at start of hook)
    fi
fi

user_prompt_submit_final_prompt "$PROMPT"
