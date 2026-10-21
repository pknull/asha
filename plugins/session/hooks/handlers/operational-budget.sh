#!/usr/bin/env bash
# operational-budget.sh — the one budget for Asha's operational layer.
#
# Sourced by session-start.sh and operational-context.sh (Claude's SessionStart
# seams) and by identity/operational-merge.sh (the file that Codex, Copilot and OpenCode
# read), so every harness receives byte-identical operational text. Budgets are
# UTF-8 bytes; truncation never splits a character and always says so.

# shellcheck disable=SC2034  # read by the scripts that source this file
ASHA_OPERATION_MAX_BYTES=4000
# shellcheck disable=SC2034
ASHA_LEARNINGS_MAX_BYTES=4500
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

# Appends Claude's operational layer through the caller's append_context:
# operation.md (or CORE.md) and active learnings as separate blocks, so the
# ceiling keeps one when it cannot keep both. Expects PROJECT_DIR, PLUGIN_ROOT
# and PYTHON_CMD. Expiry runs first: it replays a pending learnings journal,
# which the read-only render refuses, whichever SessionStart entry runs first.
asha_append_operational_context() {
  local manager="$PLUGIN_ROOT/tools/learnings_manager.py" asha_dir="${ASHA_HOME:-$HOME/.asha}"
  local op_source="" text block active
  if [[ ! -f "$PROJECT_DIR/Work/markers/silence" && -n "$PYTHON_CMD" && -f "$manager" ]]; then
    "$PYTHON_CMD" "$manager" expire --project-dir "$PROJECT_DIR" >/dev/null 2>&1 || true
  fi
  if [[ -f "$asha_dir/operation.md" ]]; then
    op_source="$asha_dir/operation.md"
  elif [[ -f "$PLUGIN_ROOT/modules/CORE.md" ]]; then
    op_source="$PLUGIN_ROOT/modules/CORE.md"
  fi
  if [[ -n "$op_source" ]]; then
    text="$(asha_budget_truncate "$(cat "$op_source")" "$ASHA_OPERATION_MAX_BYTES" "$(basename "$op_source")")"
    printf -v block '<system-reminder>\nAsha-managed project. Operational guidelines loaded.\n\n%s\n</system-reminder>' "$text"
    append_context "operational guidelines ($op_source)" "$block"
  fi
  # Only active learnings acquire authority at start.
  [[ -n "$PYTHON_CMD" && -f "$manager" ]] || return 0
  if active="$("$PYTHON_CMD" "$manager" render-active --max-bytes "$ASHA_LEARNINGS_MAX_BYTES" 2>/dev/null)"; then
    [[ -n "$active" ]] || return 0
    printf -v block '<system-reminder>\nActive reviewed learnings:\n\n%s\n</system-reminder>' "$active"
    append_context "active learnings ($asha_dir/learnings/active/)" "$block"
  else
    append_context "learnings status" "<system-reminder>$ASHA_LEARNINGS_RENDER_FAILED</system-reminder>"
  fi
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
