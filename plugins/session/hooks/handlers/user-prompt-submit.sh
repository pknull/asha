#!/bin/bash
# Memory v2 prompt recovery + direct RP routing. Fail open.
set -uo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || true
INPUT="$(cat 2>/dev/null || true)"
PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }

HARNESS="$(asha_harness 2>/dev/null || echo "${ASHA_HARNESS:-claude}")"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // .sessionID // empty' 2>/dev/null || true)"
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"
if [[ ! -f "$PROJECT_DIR/Work/markers/silence" ]]; then
  PLUGIN_ROOT="$(get_plugin_root 2>/dev/null || true)"
  PYTHON_CMD="$(get_python_cmd "$PROJECT_DIR" 2>/dev/null || true)"
  if [[ -n "$SESSION_ID" && "$SESSION_ID" != unknown && -n "$PLUGIN_ROOT" && -n "$PYTHON_CMD" && -f "$PLUGIN_ROOT/tools/recovery_state.py" ]]; then
    printf '%s' "$INPUT" | "$PYTHON_CMD" "$PLUGIN_ROOT/tools/recovery_state.py" update \
      --project-dir "$PROJECT_DIR" --harness "$HARNESS" --session-id "$SESSION_ID" \
      >/dev/null 2>&1 || true
  fi
fi

RP_CONTEXT=""
PLUGIN_ROOT="$(get_plugin_root 2>/dev/null || true)"
RP_FILE="$PLUGIN_ROOT/hooks/handlers/rp-routing.md"
if [[ -f "$PROJECT_DIR/Work/markers/rp-active" \
      && ! -f "$PROJECT_DIR/Work/markers/rp-hook-off" \
      && ! -f "$PROJECT_DIR/Work/markers/silence" \
      && -f "$RP_FILE" ]]; then
  RP_CONTEXT="$(cat "$RP_FILE" 2>/dev/null || true)"
fi

# The generic nudge engine was retired with Memory v1. The unrelated
# rp-priced-stakes safeguard remains a direct, marker-gated RP prompt handler.
PRICED_HANDLER="$PLUGIN_ROOT/../rp/hooks/handlers/priced-stakes.sh"
if [[ -x "$PRICED_HANDLER" ]]; then
  PRICED_CONTEXT="$(printf '%s' "$INPUT" | "$PRICED_HANDLER" "$PROJECT_DIR" 2>/dev/null || true)"
  [[ -z "$PRICED_CONTEXT" ]] || RP_CONTEXT="${RP_CONTEXT:+$RP_CONTEXT$'\n\n'}$PRICED_CONTEXT"
fi

if [[ -z "$RP_CONTEXT" ]]; then
  echo '{}'
elif [[ "$HARNESS" == "copilot" ]]; then
  jq -n --arg ctx "$RP_CONTEXT" '{additionalContext:$ctx}'
else
  printf '%s\n' "$RP_CONTEXT"
fi
exit 0
