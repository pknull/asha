#!/bin/bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# nudge-builtins.sh — allowlisted dynamic payloads for nudge-engine.sh.
#
# A builtin computes a fragment the declarative registry cannot express
# (index queries, stateful counters). Contract: read the engine's globals
# (NUDGE_INPUT: raw stdin JSON, NUDGE_PROJECT_DIR: may be empty,
# NUDGE_HANDLERS_DIR: handlers/ dir), print the fragment text to stdout, and
# print nothing to fire nothing. Builtins must fail open: return 0 on every
# path. Dispatch is a case allowlist — an unknown handler name in a rule row
# is a silent skip, never an arbitrary call.

nudge_dispatch() {
    case "${1:-}" in
        memory_lexical)  nudge_builtin_memory_lexical ;;
        suggest_compact) nudge_builtin_suggest_compact ;;
        *) return 0 ;;
    esac
}

# Lexical memory-index nudge (row memory-lexical). Port of the retired
# hooks/memory_nudge.sh wrapper: bounded python query against the cached
# nudge index; the fragment is the additionalContext string the tool emits.
nudge_builtin_memory_lexical() {
    local tool="$NUDGE_HANDLERS_DIR/../../tools/memory_nudge.py"
    [[ -f "$tool" ]] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    [[ -n "$NUDGE_INPUT" ]] || return 0

    local -a index_args=()
    if [[ -n "${ASHA_NUDGE_INDEX:-}" ]]; then
        [[ -f "$ASHA_NUDGE_INDEX" ]] || return 0
        index_args=(--index "$ASHA_NUDGE_INDEX")
    fi

    local out=""
    # Python startup plus a tiny cached-index query must not hold up the call.
    if command -v timeout >/dev/null 2>&1; then
        out="$(printf '%s' "$NUDGE_INPUT" | timeout 0.1s python3 "$tool" "${index_args[@]}" match 2>/dev/null || true)"
    else
        out="$(printf '%s' "$NUDGE_INPUT" | python3 "$tool" "${index_args[@]}" match 2>/dev/null || true)"
    fi
    [[ -n "$out" ]] || return 0
    printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null || true
    return 0
}

# Session-activity compaction suggestion (row suggest-compact). Port of the
# retired handlers/suggest-compact.sh minus the gates the engine now owns
# (silence, init, 2h cooldown). The per-session tool-call counter stays here:
# it must increment on every eligible PostToolUse, not only on fires.
nudge_builtin_suggest_compact() {
    local pdir="$NUDGE_PROJECT_DIR"
    [[ -n "$pdir" ]] || return 0

    local marker_dir="$pdir/Work/markers"
    local tool_count_file="$marker_dir/tool-count"
    local events_file="$pdir/Memory/events/events.jsonl"
    local tool_threshold=100
    local event_threshold=200

    mkdir -p "$marker_dir" 2>/dev/null || return 0

    local tool_count=0
    [[ -f "$tool_count_file" ]] && tool_count="$(cat "$tool_count_file" 2>/dev/null || echo 0)"
    [[ "$tool_count" =~ ^[0-9]+$ ]] || tool_count=0
    tool_count=$((tool_count + 1))
    echo "$tool_count" > "$tool_count_file" 2>/dev/null

    local should=0 reason=""
    if (( tool_count >= tool_threshold )); then
        should=1
        reason="$tool_count tool calls this session"
    fi
    if [[ -f "$events_file" ]]; then
        local event_count
        event_count="$(wc -l < "$events_file" 2>/dev/null || echo 0)"
        [[ "$event_count" =~ ^[0-9]+$ ]] || event_count=0
        if (( event_count >= event_threshold )); then
            should=1
            reason="$event_count events in log"
        fi
    fi
    (( should == 1 )) || return 0

    echo "0" > "$tool_count_file" 2>/dev/null
    cat <<EOF
<system-reminder>
Context check: This session has significant activity ($reason).

If you notice degraded performance or the conversation getting long, consider:
- Using /save to checkpoint progress
- Starting a fresh session for new tasks
- Delegating exploration to subagents (Task tool) to preserve main context

This is informational only - continue if the current task is progressing well.
</system-reminder>
EOF
    return 0
}
