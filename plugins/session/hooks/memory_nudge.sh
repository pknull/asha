#!/bin/bash
# Claude PreToolUse memory nudge. Awareness-only, bounded, and fail-open.
set -uo pipefail

[[ "${ASHA_NUDGE:-1}" != "0" ]] || exit 0
SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
TOOL="$SCRIPT_DIR/../tools/memory_nudge.py"
[[ -f "$TOOL" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

INDEX_ARGS=()
if [[ -n "${ASHA_NUDGE_INDEX:-}" ]]; then
    [[ -f "$ASHA_NUDGE_INDEX" ]] || exit 0
    INDEX_ARGS=(--index "$ASHA_NUDGE_INDEX")
fi

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || exit 0
# Python startup plus a tiny cached-index query must not hold up the tool call.
if command -v timeout >/dev/null 2>&1; then
    printf '%s' "$INPUT" | timeout 0.1s python3 "$TOOL" "${INDEX_ARGS[@]}" match 2>/dev/null || true
else
    printf '%s' "$INPUT" | python3 "$TOOL" "${INDEX_ARGS[@]}" match 2>/dev/null || true
fi
exit 0
