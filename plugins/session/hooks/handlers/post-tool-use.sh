#!/bin/bash
# Memory v2 PostToolUse recovery writer. Mechanical state only; fail open.
set -uo pipefail
SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || true
INPUT="$(cat 2>/dev/null || true)"
PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }
[[ ! -f "$PROJECT_DIR/Work/markers/silence" ]] || { echo '{}'; exit 0; }

PLUGIN_ROOT="$(get_plugin_root 2>/dev/null || true)"
PYTHON_CMD="$(get_python_cmd "$PROJECT_DIR" 2>/dev/null || true)"
HARNESS="$(asha_harness 2>/dev/null || echo "${ASHA_HARNESS:-claude}")"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionId // .sessionID // empty' 2>/dev/null || true)"
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"
if [[ -n "$SESSION_ID" && "$SESSION_ID" != unknown && -n "$PLUGIN_ROOT" && -n "$PYTHON_CMD" && -f "$PLUGIN_ROOT/tools/recovery_state.py" ]]; then
  printf '%s' "$INPUT" | "$PYTHON_CMD" "$PLUGIN_ROOT/tools/recovery_state.py" update \
    --project-dir "$PROJECT_DIR" --harness "$HARNESS" --session-id "$SESSION_ID" \
    >/dev/null 2>&1 || true
fi
echo '{}'
exit 0
