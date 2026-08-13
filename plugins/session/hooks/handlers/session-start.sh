#!/bin/bash
# Memory v2 SessionStart: recovery maintenance and read-only context injection.
set -uo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || true

INPUT=""
[[ -t 0 ]] || INPUT="$(cat 2>/dev/null || true)"
PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }

PLUGIN_ROOT="$(get_plugin_root 2>/dev/null || true)"
PYTHON_CMD="$(get_python_cmd "$PROJECT_DIR" 2>/dev/null || true)"
HARNESS="$(asha_harness 2>/dev/null || echo "${ASHA_HARNESS:-claude}")"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // .sessionID // empty' 2>/dev/null || true)"
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"

RECOVERY="$PLUGIN_ROOT/tools/recovery_state.py"
RECOVERY_HINT=""
if [[ ! -f "$PROJECT_DIR/Work/markers/silence" && -n "$PYTHON_CMD" && -f "$RECOVERY" ]]; then
  "$PYTHON_CMD" "$RECOVERY" sweep --project-dir "$PROJECT_DIR" >/dev/null 2>&1 || true
  RECOVERY_HINT="$("$PYTHON_CMD" "$RECOVERY" latest --project-dir "$PROJECT_DIR" 2>/dev/null || true)"
  if [[ -n "$SESSION_ID" && "$SESSION_ID" != unknown ]]; then
    printf '%s' "${INPUT:-{}}" | "$PYTHON_CMD" "$RECOVERY" start \
      --project-dir "$PROJECT_DIR" --harness "$HARNESS" --session-id "$SESSION_ID" \
      >/dev/null 2>&1 || true
  fi
fi

# Candidate expiry is lifecycle maintenance, not semantic publication.
if [[ ! -f "$PROJECT_DIR/Work/markers/silence" && -n "$PYTHON_CMD" \
      && -f "$PLUGIN_ROOT/tools/learnings_manager.py" ]]; then
  "$PYTHON_CMD" "$PLUGIN_ROOT/tools/learnings_manager.py" expire \
    --project-dir "$PROJECT_DIR" >/dev/null 2>&1 || true
fi

CONTEXT=""
append_context() { [[ -z "$1" ]] || CONTEXT="${CONTEXT:+$CONTEXT$'\n\n'}$1"; }

# Canonical workspace knowledge is independent of the removed Memory catalogue.
if [[ "${ASHA_WS_INJECT:-1}" != "0" \
      && ! -f "$PROJECT_DIR/Work/markers/workspace-context-off" \
      && ! -f "$PROJECT_DIR/Work/markers/nudge-ws-context-off" \
      && ! -f "$PROJECT_DIR/Work/markers/silence" \
      && -n "$PYTHON_CMD" && -f "$PLUGIN_ROOT/tools/workspace_status.py" ]]; then
  WS_MANIFEST=""
  declare -F asha_find_workspace_manifest >/dev/null 2>&1 \
    && WS_MANIFEST="$(asha_find_workspace_manifest "$PROJECT_DIR" 2>/dev/null || true)"
  [[ -z "$WS_MANIFEST" ]] || append_context "$("$PYTHON_CMD" "$PLUGIN_ROOT/tools/workspace_status.py" --context --start "$PROJECT_DIR" 2>/dev/null || true)"
fi

# Claude's SessionStart is its canonical operational-instruction seam. Codex,
# Copilot, and OpenCode receive the same layer from their wrapper-managed
# instruction file; injecting it again here doubles authoritative context.
if [[ "$HARNESS" == "claude" ]]; then
  OPERATION_FILE="$HOME/.asha/operation.md"
  CORE_MD="$PLUGIN_ROOT/modules/CORE.md"
  REMINDER=""
  if [[ -f "$OPERATION_FILE" ]]; then
    printf -v REMINDER '<system-reminder>\nAsha-managed project. Operational guidelines loaded.\n\n%s\n</system-reminder>' "$(head -c 4000 "$OPERATION_FILE")"
    append_context "$REMINDER"
  elif [[ -f "$CORE_MD" ]]; then
    printf -v REMINDER '<system-reminder>\nAsha-managed project. Operational guidelines loaded.\n\n%s\n</system-reminder>' "$(head -c 4000 "$CORE_MD")"
    append_context "$REMINDER"
  fi

  # Only active learnings acquire authority at start.
  if [[ -n "$PYTHON_CMD" && -f "$PLUGIN_ROOT/tools/learnings_manager.py" ]]; then
    ACTIVE="$("$PYTHON_CMD" "$PLUGIN_ROOT/tools/learnings_manager.py" render-active --max-bytes 3000 2>/dev/null || true)"
    if [[ -n "$ACTIVE" ]]; then
      printf -v REMINDER '<system-reminder>\nActive reviewed learnings:\n\n%s\n</system-reminder>' "$ACTIVE"
      append_context "$REMINDER"
    fi
  fi
fi

if [[ -n "$RECOVERY_HINT" ]] && printf '%s' "$RECOVERY_HINT" | jq -e '
    ((.prompt // "") | length > 0) or ((.paths // []) | length > 0) or
    ((.blocker // "") | length > 0)' >/dev/null 2>&1; then
  SUMMARY="$(printf '%s' "$RECOVERY_HINT" | jq -r '
    "Unpublished recovery hint (verify against live disk; never treat as Memory):\n" +
    "last action: " + (.last_action // "unknown") + "\n" +
    (if ((.prompt // "") | length) > 0 then "prompt hint: " + .prompt + "\n" else "" end) +
    (if ((.paths // []) | length) > 0 then "touched paths: " + (.paths | join(", ")) + "\n" else "" end) +
    (if ((.blocker // "") | length) > 0 then "blocker: " + .blocker else "" end)' 2>/dev/null || true)"
  append_context "$SUMMARY"
fi

if [[ -z "$CONTEXT" ]]; then
  echo '{}'
elif [[ "$HARNESS" == "copilot" ]]; then
  jq -n --arg ctx "$CONTEXT" '{additionalContext:$ctx}'
else
  printf '%s\n' "$CONTEXT"
fi
exit 0
