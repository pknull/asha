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
# Claude Code replaces any hook output string over 10,000 characters with a
# file path and a 2,000-character preview it never asks the model to read, and
# Codex spills hook output above 2,500 tokens (bytes/4). Bytes bound both.
ASHA_HOOK_CONTEXT_MAX_BYTES=9500
# shellcheck disable=SC2034
ASHA_LEARNINGS_RENDER_FAILED="Active learnings could not be rendered (an invalid or pending learning record); run /session:status."

# Joins the labelled blocks in ASHA_CONTEXT_LABELS / ASHA_CONTEXT_BLOCKS in
# order, admitting each block that keeps the total within the hook ceiling.
# Anything left out is named first, so the notice survives any preview. The
# notice reserves its own size and each retry shrinks the budget; if no
# arrangement fits beside its notice, every block is named instead.
asha_hook_context_join() {
  local budget="$ASHA_HOOK_CONTEXT_MAX_BYTES" attempt context omitted candidate notice reserve i
  for attempt in 1 2 3 4 5 6 7 8; do
    context="" omitted=""
    for i in "${!ASHA_CONTEXT_BLOCKS[@]}"; do
      candidate="${context:+$context$'\n\n'}${ASHA_CONTEXT_BLOCKS[$i]}"
      if (( $(printf '%s' "$candidate" | wc -c) <= budget )); then
        context="$candidate"
      else
        omitted="${omitted:+$omitted; }${ASHA_CONTEXT_LABELS[$i]}"
      fi
    done
    [[ -n "$omitted" ]] || { printf '%s' "$context"; return 0; }
    notice="<system-reminder>Omitted from this SessionStart to stay within the hook output cap: $omitted. Read them from disk if the task needs them.</system-reminder>"
    reserve=$(( $(printf '%s' "$notice" | wc -c) + 2 ))
    if (( $(printf '%s' "$context" | wc -c) + reserve <= ASHA_HOOK_CONTEXT_MAX_BYTES )); then
      printf '%s%s' "$notice" "${context:+$'\n\n'$context}"
      return 0
    fi
    (( ASHA_HOOK_CONTEXT_MAX_BYTES - reserve < budget )) || break
    budget=$((ASHA_HOOK_CONTEXT_MAX_BYTES - reserve))
  done
  omitted=""
  for i in "${!ASHA_CONTEXT_LABELS[@]}"; do
    omitted="${omitted:+$omitted; }${ASHA_CONTEXT_LABELS[$i]}"
  done
  printf '<system-reminder>Omitted from this SessionStart to stay within the hook output cap: %s. Read them from disk if the task needs them.</system-reminder>' "$omitted" \
    | head -c "$ASHA_HOOK_CONTEXT_MAX_BYTES" | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null
}

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
