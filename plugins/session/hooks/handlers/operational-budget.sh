#!/usr/bin/env bash
# operational-budget.sh — the one budget for Asha's operational layer.
#
# Sourced by session-start.sh (Claude's SessionStart seam) and by
# identity/operational-merge.sh (the file that Codex, Copilot and OpenCode
# read), so every harness receives byte-identical operational text. Budgets are
# UTF-8 bytes; truncation never splits a character and always says so.

# shellcheck disable=SC2034  # read by the scripts that source this file
ASHA_OPERATION_MAX_BYTES=4000
# shellcheck disable=SC2034
ASHA_LEARNINGS_MAX_BYTES=3000

# $1 content, $2 byte budget, $3 label. Prints the content, or its longest
# prefix within budget that is valid UTF-8 followed by a truncation notice.
asha_budget_truncate() {
  local content="$1" max_bytes="$2" label="$3" size prefix
  size="$(printf '%s' "$content" | wc -c | tr -d '[:space:]')"
  if (( size <= max_bytes )); then
    printf '%s' "$content"
    return 0
  fi
  prefix="$(printf '%s' "$content" | head -c "$max_bytes" | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null)"
  printf '%s\n\n[Truncated: %s exceeded %s bytes (%s total). Read the full file if needed.]' \
    "$prefix" "$label" "$max_bytes" "$size"
}

# Doctor line for $1 (default ~/.asha/operation.md). Over budget fails: the
# tail of the file is silently absent from every session otherwise.
asha_operation_budget_report() {
  local file="${1:-${ASHA_HOME:-$HOME/.asha}/operation.md}" size
  if [[ ! -f "$file" ]]; then
    echo "PASS  operation.md absent; the CORE.md fallback applies"
    return 0
  fi
  size="$(wc -c < "$file" | tr -d '[:space:]')"
  if (( size > ASHA_OPERATION_MAX_BYTES )); then
    echo "FAIL  operation.md is $size bytes; sessions receive only the first $ASHA_OPERATION_MAX_BYTES (trim $file)"
    return 1
  fi
  echo "PASS  operation.md is $size of $ASHA_OPERATION_MAX_BYTES bytes"
}
